"""업로드 ingest (Phase 4 분리 — 구 service.ingest_webreport).

manifest+parquet 수신 → 해시(analysis_key/content_hash) → 저장(storage 포트) →
DB 세션 생성 → 편집값 시드 → 감사 기록 → 백그라운드 프리웜. 외부 진입점은
여전히 service.ingest_webreport (재노출) — upload_webreport.py 는 무변경.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from pathlib import Path

from . import cache
from . import cache_policy
from . import disk_cache
from . import edits
from . import runtime
from .validation import (
    canon as _canon,
    client_identity as _client_identity,
    validate_meta as _validate_meta,
    validate_mode as _validate_mode,
)

_log = logging.getLogger(__name__)


def _seed_client_dist_blobs(dist_blobs, analysis_key, content_hash, mode,
                            upload_root: Path) -> list[str]:
    """Honey 가 업로드에 첨부한 프리컴퓨트 dist blob(전체/bin1)을 dist 캐시에 시딩.

    클라가 서버와 같은 dist_blob.compute_dist_compact 로 만든 gzip 이라 값이 동일하다 —
    시딩되면 서버 콜드 dist 빌드(수십 초 CPU + RAM 스파이크)가 아예 발생하지 않는다.
    검증(gzip CRC + 포맷 프리픽스) 실패나 미첨부는 조용히 건너뛰고 기존 서버 계산
    폴백(prewarm/첫 조회)이 그대로 동작한다. 반환: 시딩된 변형 이름 리스트.
    """
    from .dist_blob import validate_dist_blob

    seeded: list[str] = []
    pseudo_session = {"analysis_key": analysis_key, "content_hash": content_hash,
                      "mode": mode}
    for variant, bin1 in (("all", False), ("bin1", True)):
        blob = (dist_blobs or {}).get(variant)
        if not blob:
            continue
        try:
            raw_size = validate_dist_blob(blob)
        except ValueError as exc:
            _log.warning("client dist blob(%s) rejected akey=%.12s: %s",
                         variant, str(analysis_key), exc)
            continue
        key = cache_policy.dist_key(pseudo_session, bin1=bin1)
        disk_cache.save_dist(Path(upload_root), key, blob)
        cache.dist_cache_put(key, blob)   # 개수+바이트 이중 상한 (cache.py)
        seeded.append(variant)
        _log.info("client dist blob(%s) seeded akey=%.12s gz=%.1fMB raw=%.1fMB",
                  variant, str(analysis_key), len(blob) / 1048576, raw_size / 1048576)
    return seeded


def ingest_webreport(manifest: dict, files: list[dict], *, report_db, upload_root: Path,
                     client_ip: str = "", user_agent: str = "",
                     dist_blobs: dict | None = None,
                     request_started: float | None = None) -> dict:
    """request_started: 라우트에서 잰 time.perf_counter() 시작값 (선택).
    주면 파일 수신까지 포함한 업로드 소요시간을 감사 로그에 남긴다."""
    from .honeyform import decode_split_honeyform_parquet

    if request_started is None:
        request_started = time.perf_counter()

    meta = _validate_meta(manifest.get("meta") or {})
    mode = _validate_mode(manifest.get("mode"))
    uploaded_by, client_host = _client_identity(manifest)
    sources_manifest = manifest.get("sources") or []
    selected_items = manifest.get("selected_items") or []

    # Compare 모드는 정확히 2개 파일만 허용 (Honey Compare Mode 관례: after/before 2개).
    if mode == "Compare" and len(files) != 2:
        raise ValueError(
            f"Compare 모드는 입력 파일이 2개일 때만 가능합니다 (현재 {len(files)}개)")

    file_hashes = []
    decoded = []
    for idx, item in enumerate(files):
        data = item["data"]
        file_hashes.append(hashlib.sha256(data).hexdigest())
        source_info = sources_manifest[idx] if idx < len(sources_manifest) else {}
        source_name = str(source_info.get("name") or item.get("name") or f"source_{idx + 1}")
        file_name = str(source_info.get("file_name") or item.get("filename") or source_name)
        # 검증 겸 decode+split — 이 tables 를 아래에서 TABLES_CACHE 에 시딩해
        # prewarm 의 재디코드(파일당 ~1s)를 없앤다. 원본 bytes 는 이미 손에 있으므로
        # df(재인코딩용 전체 프레임)는 만들지 않는다 (읽기 캐시 규약과 동일 슬림 형태).
        table = decode_split_honeyform_parquet(data, source=source_name, file_name=file_name,
                                               keep_df=False)
        decoded.append({
            "source": source_name,
            "file_name": file_name,
            "table": table,
            "bytes": data,
            "hash": file_hashes[-1],
        })
    if not decoded:
        raise ValueError("no webreport parquet files received")

    key_meta = {k: meta[k] for k in ("product_type", "product", "lot_id")}
    h = hashlib.sha256()
    h.update(_canon({"files": file_hashes, "meta": key_meta, "selected_items": selected_items}))
    analysis_key = h.hexdigest()
    content_hash = hashlib.sha256(_canon({"files": file_hashes})).hexdigest()
    session_id = f"{int(time.time())}_{secrets.token_hex(3)}"

    storage_result = runtime.storage().save_webreport_sources(
        analysis_key, content_hash, [item["bytes"] for item in decoded], manifest,
        upload_root=upload_root)
    cache.manifest_cache_put(analysis_key, manifest)
    # ingest 가 이미 디코드한 tables 를 loader 와 같은 키로 시딩 — prewarm/첫 조회의
    # storage 재다운로드+재디코드 생략. (캐시엔 원본 저장, 소비자는 loader 가 클론 반환.)
    cache.tables_cache_put((analysis_key, content_hash),
                           [item["table"] for item in decoded])
    # 클라 프리컴퓨트 dist blob(전체/bin1) 시딩 — 첨부 시 서버 콜드 dist 빌드 소멸.
    dist_seeded = _seed_client_dist_blobs(
        dist_blobs, analysis_key, content_hash, mode, upload_root)

    session_dir = Path(upload_root) / "web_report" / analysis_key
    # 선택된 product(part_id/sub_part_id) → product_info.db 기준정보 lookup 후 세션에 저장.
    # product_info 는 config 급 정적 참조 데이터 로더(server/ sys.path). 기준정보는 위
    # key_meta/analysis_key 산출에 미포함이므로 dedup 키는 불변(규칙 #3).
    from product_info import lookup as _product_info_lookup
    report_db.create_session(
        session_id=session_id,
        file_name=meta["file_name"],
        file_path=str(session_dir),
        product_type=meta["product_type"],
        family_product=meta["family_product"],
        process=meta["process"],
        product=meta["product"],
        revision=meta["revision"],
        edm_link=meta["edm_link"],
        lot_id=meta["lot_id"],
        password=meta["password"],
        source="web_report",
        uploaded_by=uploaded_by or None,
        client_host=client_host or None,
        mode=mode,
        product_info=_product_info_lookup(meta["product"]),
    )
    report_db.update_session(
        session_id, analysis_key=analysis_key, content_hash=content_hash, status="done")

    # F10 웹리포트 옵션(Distribution source 색)을 세션에 영속화 — 조회 시 동일 재현용.
    # analysis_key 는 여러 세션이 공유(dedup)할 수 있으나 옵션은 세션 단위이므로 DB 세션행에
    # 저장한다. {"colors":[...]} 형태이며 조회 시 distribution source 색으로 적용된다.
    options = manifest.get("options")
    if isinstance(options, dict) and options:
        try:
            report_db.update_session(
                session_id, webreport_options=json.dumps(options, sort_keys=True))
        except Exception:
            _log.warning("webreport options 저장 실패 (session=%s)",
                         session_id, exc_info=True)

    # manifest 에 편집값(comment/override)이 실려 오면 세션 편집 DB 로 시드 —
    # 이후 manifest 는 불변 스냅샷이고 편집 진실은 DB(세션 단위)다.
    try:
        edits.seed_from_manifest(report_db, session_id, manifest,
                                 updated_by=uploaded_by or None)
    except Exception:
        _log.warning("web_report 편집값 시드 실패 — 업로드 코멘트/override 유실 "
                     "(session=%s)", session_id, exc_info=True)

    # Issue Table 코멘트(시드 포함) → eval_analyzer 스키마 DB 적재 (백그라운드,
    # 실패 무해 — docs/13). 방금 시딩한 TABLES_CACHE 를 그대로 쓴다.
    try:
        from . import eval_export
        eval_export.export_async(session_id, report_db=report_db,
                                 upload_root=Path(upload_root))
    except Exception:
        pass

    # 업로드 소요시간·크기를 감사 행에 남긴다 — ingest 가 느려지는 추세를 관리자 화면
    # (User Action Monitoring)에서 볼 수 있는 유일한 경로다.
    elapsed = round(time.perf_counter() - request_started, 1)
    total_mb = round(sum(len(item["bytes"]) for item in decoded) / (1024 * 1024), 1)
    try:
        report_db.log_audit(
            "upload", session_id=session_id, analysis_key=analysis_key,
            product_type=meta["product_type"], product=meta["product"],
            lot_id=meta["lot_id"], file_name=meta["file_name"],
            changed_fields=f"ingest {elapsed}s / {len(decoded)}파일 {total_mb}MB",
            client_ip=client_ip, user_agent=user_agent,
            client_user=uploaded_by or None, client_host=client_host or None)
    except Exception:
        pass
    _log.info("[ingest] session=%s elapsed=%.1fs files=%d size=%.1fMB",
              session_id, elapsed, len(decoded), total_mb)

    # 캐시 프리웜: 업로더가 곧바로 여는 첫 조회(cold: parquet decode + payload + dist compact
    # ~10s)를 없애기 위해 미리 계산해 둔다. 부모 데몬 스레드에서 실행되어 위에서 시딩한
    # TABLES_CACHE 를 그대로 쓰고(재디코드 0회), 동시성은 세마포어(워커 수)로 상한된다
    # (compute.prewarm docstring 참조). 실패해도 무해 — 조회 시 다시 계산될 뿐이다.
    from . import compute
    compute.prewarm(session_id, str(upload_root), dist_seeded=bool(dist_seeded))

    return {
        "session_id": session_id,
        "analysis_key": analysis_key,
        "status": "done",
        "mode": mode,
        "web_report_url": f"/pe/report/view/{session_id}",
        "sources": [item["source"] for item in decoded],
        "item_count": len({str(v) for v in selected_items if str(v)}),
        "storage": storage_result["storage"],
        # 클라 첨부 dist blob 중 시딩된 변형(["all","bin1"]) — 구 클라는 빈 리스트.
        "dist_blob_seeded": dist_seeded,
    }

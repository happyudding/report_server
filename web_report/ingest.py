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
from contextlib import nullcontext
from pathlib import Path

from . import cache
from . import cache_policy
from . import disk_cache
from . import dist_pack as _dist_pack
from . import dist_pack_store
from . import edits
from . import runtime
from .validation import (
    canon as _canon,
    client_identity as _client_identity,
    validate_meta as _validate_meta,
    validate_mode as _validate_mode,
)

_log = logging.getLogger(__name__)


def _no_stage(name, source=""):
    """trace 미지정 시 기본 계측 — 아무것도 하지 않는다.

    호출부(server/upload_webreport.py)가 `with trace(이름, 파일):` 하나만 요구하는
    함수를 넘긴다. 계측 구현을 인자로 받는 이유는 web_report 가 server 를 import 하지
    않기 위해서다(의존 방향 단방향 유지)."""
    return nullcontext()


def seed_client_dist_blobs(dist_blobs, analysis_key, content_hash, mode,
                           upload_root: Path) -> list[str]:
    """Honey 가 첨부한 프리컴퓨트 dist blob(전체/bin1)을 dist 캐시에 시딩.

    업로드(ingest)와 Excel 왕복 반영(rawedit.replace_sources) 두 경로가 공유한다.

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


def save_client_dist_pack(dist_pack: dict | None, analysis_key, content_hash, mode,
                          upload_root: Path, selected_items=None) -> bool:
    """Honey 가 첨부한 Distribution pack(index + chunk gzip)을 **영구** 저장한다.

    pack 은 ECDF 계산 중 비싼 앞단(정렬·중복 묶기)이 끝난 상태라, 서버는 조회 때 덧셈만
    하면 된다(service._pack_items). dist blob 시딩과 달리 캐시가 아니라 dist_pack_store
    영역이라 총량 축출·재시작에도 살아남아 **세션 재조회에도 재정렬이 없다**.

    검증은 dist blob 과 같은 수준(index 포맷 + chunk gzip CRC + 포맷 프리픽스)에 더해,
    pack 이 담은 항목이 이 업로드의 selected_items 범위 안인지 **교차검증**한다(아래).
    수십 MB JSON 을 파싱하지는 않으므로 값 자체의 정합성은 여전히 클라가 서버와 같은
    dist_pack 코드(같은 세대)를 쓰는 것으로 보장한다 — 포맷 버전 거부가 그 세대 방어다.
    어떤 이유로든 거부되면 조용히 False 를 돌려 기존 계산 경로로 폴백한다(구 Honey·손상
    첨부·항목 불일치 모두 동일).

    selected_items: 이 업로드가 선택한 항목 목록(있으면 교차검증에 사용). 원본 전체 교체
    (Excel 왕복)처럼 선택 맥락이 없는 호출은 None 으로 두어 교차검증을 건너뛴다 —
    포맷 버전·CRC 검증은 그 경우에도 그대로 적용된다.
    """
    if not dist_pack:
        return False
    index_text = dist_pack.get("index")
    chunks = dist_pack.get("chunks") or {}
    if not index_text or not chunks:
        return False
    try:
        index = _dist_pack.parse_pack_index(index_text)
    except ValueError as exc:
        _log.warning("client dist pack index rejected akey=%.12s: %s",
                     str(analysis_key), exc)
        return False

    # item 교차검증: pack 이 담은 항목이 selected_items 를 벗어나면, 클라가 다른 선택/다른
    # 세대 코드로 만든 pack 이다 → 거부하고 서버 폴백 계산(틀린 분포 영구 저장 방지).
    # pack 항목은 selected_items 로 필터 + 무데이터 제외를 거친 것이라 항상 부분집합이어야 한다.
    wanted = {str(v) for v in (selected_items or []) if str(v)}
    if wanted:
        pack_items = set(_dist_pack.item_chunk_map(index))
        extra = pack_items - wanted
        if extra:
            _log.warning("client dist pack item 집합이 selected_items 를 벗어남 "
                         "akey=%.12s extra=%d 예=%s — 거부(폴백)",
                         str(analysis_key), len(extra), sorted(extra)[:3])
            return False

    expected = {int(e["id"]) for e in index.get("chunks") or ()}
    got = {int(k) for k in chunks}
    if expected != got:
        _log.warning("client dist pack chunk 개수 불일치 akey=%.12s index=%d files=%d",
                     str(analysis_key), len(expected), len(got))
        return False

    raw_total = 0
    for chunk_id, blob in chunks.items():
        try:
            raw_total += _dist_pack.validate_pack_chunk(blob)
        except ValueError as exc:
            _log.warning("client dist pack chunk(%s) rejected akey=%.12s: %s",
                         chunk_id, str(analysis_key), exc)
            return False

    if not dist_pack_store.save(upload_root, analysis_key, content_hash, mode,
                                index_text, chunks):
        return False
    gz_total = sum(len(b) for b in chunks.values())
    _log.info("client dist pack saved akey=%.12s chunks=%d gz=%.1fMB raw=%.1fMB",
              str(analysis_key), len(chunks), gz_total / 1048576, raw_total / 1048576)
    return True


def _source_names_changed(analysis_key, new_names, *, report_db, upload_root: Path) -> bool:
    """이 akey 에 이미 저장된 manifest 의 source 이름이 이번 업로드와 다른가.

    `analysis_key`/`content_hash` 산출식에는 source 이름이 없다(규칙 #3 — files 해시 +
    meta + selected_items). 그래서 **같은 parquet 을 이름만 바꿔 재업로드**하면 두 키가
    그대로여서 dedup 으로 묶이고, manifest 는 새 이름으로 덮어써지는데 먼저 만들어진
    형제 세션의 payload 캐시는 키가 하나도 안 바뀌어 **옛 이름을 계속 서빙**한다.
    그러면 갤러리(payload)와 Item Detail(`/scatter`, manifest 실시간)의 source 이름이
    갈려 legend 색이 죽고(distColorMap 미스), 이름으로 매칭하는 Temperature 그룹 필터·
    Bin1(RT만)·CT/HT 의 RT limit 참조가 **에러 없이** 어긋난다.

    신규 akey(=이 akey 로 저장된 산출물이 아직 없음)면 storage 를 건드리지 않고 False —
    대부분의 업로드는 여기서 끝나 manifest GET 비용이 없다. 판단이 불가능한 예외는
    전부 False(기존 동작 유지) — 이 함수는 캐시 회수용 best-effort 다.
    """
    want = [str(n) for n in (new_names or [])]
    try:
        if not report_db.get_all_object_infos(analysis_key):
            return False
        old = runtime.storage().load_webreport_manifest(analysis_key, upload_root=upload_root)
        old_names = [str((s or {}).get("name") or "")
                     for s in ((old or {}).get("sources") or [])]
    except Exception:
        _log.debug("webreport ingest: previous manifest unreadable: %s",
                   str(analysis_key), exc_info=True)
        return False
    return bool(old_names) and old_names != want


def ingest_webreport(manifest: dict, files: list[dict], *, report_db, upload_root: Path,
                     client_ip: str = "", user_agent: str = "",
                     dist_blobs: dict | None = None,
                     dist_pack: dict | None = None,
                     request_started: float | None = None,
                     trace=None) -> dict:
    """request_started: 라우트에서 잰 time.perf_counter() 시작값 (선택).
    주면 파일 수신까지 포함한 업로드 소요시간을 감사 로그에 남긴다.

    trace: `with trace(단계이름, 파일명):` 로 쓰는 계측 훅 (선택). 주지 않으면 no-op 라
    기존 호출부·테스트는 종전과 동일하게 돈다. 업로드가 응답을 못 준 채 멎었을 때
    **어느 단계인지**를 관리자 화면·stuck 사건이 읽는 유일한 경로다."""
    from .honeyform import decode_split_honeyform_parquet

    if request_started is None:
        request_started = time.perf_counter()
    if trace is None:
        trace = _no_stage

    meta = _validate_meta(manifest.get("meta") or {})
    mode = _validate_mode(manifest.get("mode"))
    uploaded_by, client_host = _client_identity(manifest)
    sources_manifest = manifest.get("sources") or []
    selected_items = manifest.get("selected_items") or []

    # Compare 모드는 source 2개 이상(상한 없음) — Before/After 두 그룹으로 나눠 비교한다
    # (배치는 Honey 가 options.compare 로 실어 보낸다). files = 업로드된 parquet = source 1:1
    # 이므로 입력 파일 개수가 아니라 source 개수 기준이다.
    if mode == "Compare" and len(files) < 2:
        raise ValueError(
            f"Compare 모드는 source 가 2개 이상일 때만 가능합니다 (현재 {len(files)}개)")

    file_hashes = []
    decoded = []
    for idx, item in enumerate(files):
        data = item["data"]
        source_info = sources_manifest[idx] if idx < len(sources_manifest) else {}
        source_name = str(source_info.get("name") or item.get("name") or f"source_{idx + 1}")
        file_name = str(source_info.get("file_name") or item.get("filename") or source_name)
        # 검증 겸 decode+split — 이 tables 를 아래에서 TABLES_CACHE 에 시딩해
        # prewarm 의 재디코드(파일당 ~1s)를 없앤다. 원본 bytes 는 이미 손에 있으므로
        # df(재인코딩용 전체 프레임)는 만들지 않는다 (읽기 캐시 규약과 동일 슬림 형태).
        # 계측은 **파일 단위**다 — source 가 7~21개라 "decode 가 느리다"만으로는 부족하고,
        # 멎었을 때 몇 번째 파일이었는지가 곧 단서다(총 소요는 같은 이름으로 누적된다).
        with trace("decode", f"{idx + 1}/{len(files)} {file_name}"):
            file_hashes.append(hashlib.sha256(data).hexdigest())
            table = decode_split_honeyform_parquet(data, source=source_name,
                                                   file_name=file_name, keep_df=False)
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

    # 저장(=manifest 덮어쓰기) **전에** 판정해야 옛 이름을 볼 수 있다.
    renamed = _source_names_changed(
        analysis_key, [item["source"] for item in decoded],
        report_db=report_db, upload_root=upload_root)

    # S3 는 connect 5s / read 8s / retry 3 이고 실패하면 로컬 폴백으로 **전량 재저장**
    # 하므로, 저장소가 응답하지 않으면 여기 혼자서 수 분을 먹을 수 있다. storage_gateway
    # 는 동결 영역이라 내부를 쪼갤 수 없어 이 호출 전체를 한 단계로 잰다.
    with trace("storage_save"):
        storage_result = runtime.storage().save_webreport_sources(
            analysis_key, content_hash, [item["bytes"] for item in decoded], manifest,
            upload_root=upload_root)
    cache.manifest_cache_put(analysis_key, manifest)
    # source 이름만 바뀐 재업로드 — 키가 안 갈리므로 형제 세션의 캐시를 명시적으로 회수한다
    # (_source_names_changed 참조). 이름이 그대로면 아무 일도 하지 않아 기존 업로드 경로는
    # 종전과 완전히 동일하다. 회수 범위는 이 akey 뿐이라 전 세션 콜드 폭풍이 아니다.
    if renamed:
        cache.evict_akey_caches(analysis_key)
        dropped_cache = disk_cache.drop_analysis(upload_root, analysis_key)
        _log.info("webreport ingest: source names changed for akey %s — "
                  "invalidated caches (disk files=%d)", str(analysis_key), dropped_cache)
    # ingest 가 이미 디코드한 tables 를 loader 와 같은 키로 시딩 — prewarm/첫 조회의
    # storage 재다운로드+재디코드 생략. (캐시엔 원본 저장, 소비자는 loader 가 클론 반환.)
    # 키는 cache_policy 빌더로 만든다(즉석 조립 금지) — loader 도 tables_key 로 조회하므로
    # 미래에 키 포맷이 바뀌어도 시드/조회가 함께 움직인다(전처리 없는 업로드 시점이라 prep="").
    pseudo_session = {"analysis_key": analysis_key, "content_hash": content_hash}
    with trace("seed_cache"):
        cache.tables_cache_put(cache_policy.tables_key(pseudo_session),
                               [item["table"] for item in decoded])
    # 클라 프리컴퓨트 dist blob(전체/bin1) 시딩 — 첨부 시 서버 콜드 dist 빌드 소멸.
    with trace("dist_seed"):
        dist_seeded = seed_client_dist_blobs(
            dist_blobs, analysis_key, content_hash, mode, upload_root)
    # 클라 Distribution pack(정렬 완료) 영구 저장 — 첨부 시 조회·재조회 모두 재정렬 없음.
    with trace("dist_pack_save"):
        pack_saved = save_client_dist_pack(
            dist_pack, analysis_key, content_hash, mode, upload_root,
            selected_items=selected_items)

    session_dir = Path(upload_root) / "web_report" / analysis_key
    # 선택된 product(part_id/sub_part_id) → product_info.db 기준정보 lookup 후 세션에 저장.
    # product_info 는 config 급 정적 참조 데이터 로더(server/ sys.path). 기준정보는 위
    # key_meta/analysis_key 산출에 미포함이므로 dedup 키는 불변(규칙 #3).
    from product_info import lookup as _product_info_lookup
    # SQLite 는 WAL 이라 조회는 멀쩡한데 **쓰기만** 잠길 수 있다(busy_timeout 5초가
    # create/update×2 연쇄로 누적). "업로드만 멎고 웹은 정상"인 증상과 맞아떨어지는
    # 후보라 세션 생성 구간을 따로 잰다.
    with trace("create_session"):
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
    # 업로드 창의 STEP 도 같은 옵션 JSON 에 실어 둔다 — 세션 단위 값이고, report 캐시
    # 키(cache_policy.report_key)가 webreport_options 를 이미 물고 있어 나중에 STEP 을
    # 고치면 payload 가 자동으로 다시 만들어진다. 빈 값이면 넣지 않는다(종전 세션과 동일).
    if meta.get("step"):
        options = dict(options) if isinstance(options, dict) else {}
        options["step"] = meta["step"]
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
        with trace("seed_edits"):
            edits.seed_from_manifest(report_db, session_id, manifest,
                                     updated_by=uploaded_by or None)
    except Exception:
        _log.warning("web_report 편집값 시드 실패 — 업로드 코멘트/override 유실 "
                     "(session=%s)", session_id, exc_info=True)

    # Issue Table 코멘트(시드 포함) → eval_analyzer 스키마 DB 적재 (백그라운드,
    # 실패 무해 — docs/13). 방금 시딩한 TABLES_CACHE 를 그대로 쓴다.
    try:
        from . import eval_export
        # 둘 다 큐 enqueue 만이라 기대값은 ~0ms 다 — 계측을 붙여 두는 것은 그 가정이
        # 깨지는 순간(큐 락 경합 등)을 놓치지 않기 위해서다.
        with trace("eval_queue"):
            eval_export.export_async(session_id, report_db=report_db,
                                     upload_root=Path(upload_root))
            # 평가 판단 근거(L1~L4) 스냅샷도 같은 큐에 올린다 — 조회 경로는 persist=False 라
            # 매번 계산하고 버리므로, 룰 채점·표본 검수의 재료가 여기서만 쌓인다(docs/17).
            # AI Comment 옵션과 무관하게 전 web_report 세션 대상이며 실패는 무해하다.
            eval_export.collect_async(session_id, report_db=report_db,
                                      upload_root=Path(upload_root))
    except Exception:
        _log.warning("eval export 시작 실패 — 코멘트 eval DB 적재 누락 (session=%s)",
                     session_id, exc_info=True)

    # 업로드 소요시간·크기를 감사 행에 남긴다 — ingest 가 느려지는 추세를 관리자 화면
    # (User Action Monitoring)에서 볼 수 있는 유일한 경로다.
    elapsed = round(time.perf_counter() - request_started, 1)
    total_mb = round(sum(len(item["bytes"]) for item in decoded) / (1024 * 1024), 1)
    try:
        with trace("audit"):
            report_db.log_audit(
                "upload", session_id=session_id, analysis_key=analysis_key,
                product_type=meta["product_type"], product=meta["product"],
                lot_id=meta["lot_id"], file_name=meta["file_name"],
                changed_fields=f"ingest {elapsed}s / {len(decoded)}파일 {total_mb}MB",
                client_ip=client_ip, user_agent=user_agent,
                client_user=uploaded_by or None, client_host=client_host or None)
    except Exception:
        pass
    _log.info("[ingest] session=%s elapsed=%.1fs files=%d size=%.1fMB storage=%s",
              session_id, elapsed, len(decoded), total_mb,
              storage_result.get("storage") if isinstance(storage_result, dict) else "?")

    # 캐시 프리웜: 업로더가 곧바로 여는 첫 조회(cold: parquet decode + payload + dist compact
    # ~10s)를 없애기 위해 미리 계산해 둔다. 부모 데몬 스레드에서 실행되어 위에서 시딩한
    # TABLES_CACHE 를 그대로 쓰고(재디코드 0회), 동시성은 세마포어(워커 수)로 상한된다
    # (compute.prewarm docstring 참조). 실패해도 무해 — 조회 시 다시 계산될 뿐이다.
    from . import compute
    with trace("prewarm"):
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
        # 클라 첨부 Distribution pack 영구 저장 여부 — 구 클라/거부 시 False.
        "dist_pack_saved": pack_saved,
    }

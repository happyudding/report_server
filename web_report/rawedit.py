"""Honey 클라 Excel 편집용 raw data export / replace.

세션의 web_report parquet 원본을 zip 으로 내보내고(Honey 가 Excel 로 열어 편집),
Honey 가 재인코딩한 parquet 전체를 받아 기존 analysis_key 원본을 통째로 덮어쓴다.
덮어쓰기 직전 현재 원본을 1세대 백업한다(backup_current_sources) — 앱 내 undo 는 없고
복구는 운영자 수동. service.edit_raw_data 와 동일한 백업·content_hash 산출·캐시 무효화·
audit 패턴을 그대로 따른다 (여기는 셀 단위가 아니라 source 전체 교체라는 점만 다름).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import time
import zipfile
from pathlib import Path

from . import cache
from . import edits
from . import runtime
from .honeyform import validate_parquet_bytes
from .validation import canon

_log = logging.getLogger(__name__)


def _save_dist_pack(dist_pack, analysis_key, content_hash, mode, upload_root) -> bool:
    """클라 첨부 Distribution pack 저장 (업로드 경로와 같은 구현).

    실패는 무해 — 서버가 조회 때 폴백 계산한다(느릴 뿐 결과는 같다)."""
    if not dist_pack:
        return False
    try:
        from .ingest import save_client_dist_pack
        from .validation import validate_mode

        return save_client_dist_pack(dist_pack, analysis_key, content_hash,
                                     validate_mode(mode), Path(upload_root))
    except Exception:
        _log.warning("rawdata_replace dist pack 저장 실패 akey=%.12s",
                     str(analysis_key), exc_info=True)
        return False


def export_etag(session) -> str:
    """rawdata_export 응답의 ETag — 원본 parquet 내용 해시(content_hash) 그대로.

    Honey 가 temp 에 받아둔 zip 을 재사용할 수 있는지 판정하는 유일한 기준이다:
    content_hash 는 raw parquet 이 바뀔 때만(셀 편집·Excel 왕복) 바뀌므로, 같으면
    내려줄 내용이 100% 동일하다. 값이 없는 legacy 세션은 빈 문자열(=캐시 불가).
    """
    return str((session or {}).get("content_hash") or "")


def export_sources_zip(session_id, *, report_db, upload_root) -> bytes:
    """세션의 모든 source parquet + manifest 를 zip(ZIP_STORED) bytes 로 반환.

    parquet 은 이미 zstd 압축이라 재압축하지 않는다. 없으면 KeyError/FileNotFoundError.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    sources, manifest = runtime.storage().load_webreport_sources(analysis_key, upload_root)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for idx, data in enumerate(sources):
            zf.writestr(f"source_{idx}.parquet", data)
    return buf.getvalue()


def backup_current_sources(analysis_key, upload_root, old_content_hash="") -> str:
    """덮어쓰기 직전 현재 parquet 원본+manifest 를 로컬 백업으로 남긴다 (1세대 유지).

    `<upload_root>/webreport_backup/<analysis_key>/<UTC ts>_<old_hash 12자>/` 에
    source_<idx>.parquet + manifest.json 을 쓰고, 성공 후 같은 analysis_key 의 이전
    세대 디렉토리를 지워 항상 1세대만 유지한다(akey당 자체 용량 상한). 실패는 예외
    전파 — 백업 없이 덮어쓰지 않는다(호출부가 편집을 거부, 원본 무손상). 복구는
    수동(파일 복사) 전제라 복원 API 는 없다. 반환은 백업 디렉토리 이름(감사 로그용).
    """
    sources, manifest = runtime.storage().load_webreport_sources(analysis_key, upload_root)
    root = Path(upload_root) / "webreport_backup" / str(analysis_key)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"{stamp}_{str(old_content_hash or '')[:12] or 'nohash'}"
    dest = root / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for idx, data in enumerate(sources):
        (dest / f"source_{idx}.parquet").write_bytes(data)
    # 백업 완료 후에만 이전 세대 제거 — 도중 실패 시 남는 부분 디렉토리는 다음
    # 성공 백업이 지운다(덮어쓰기 자체가 거부되므로 원본 유실과 무관).
    for sibling in root.iterdir():
        if sibling.name != name and sibling.is_dir():
            shutil.rmtree(sibling, ignore_errors=True)
    return name


def remove_backups(analysis_key, upload_root) -> bool:
    """`webreport_backup/<analysis_key>/` 백업 디렉토리 제거 — 마지막 참조 세션 삭제 시.

    산출물(parquet/이미지)은 storage_gateway 가 지우지만 이 백업은 여기서 만들므로 삭제도
    여기서 제공한다. 있었으면 True. best-effort(rmtree ignore_errors) — 삭제 실패가 세션
    삭제를 막지 않는다.
    """
    root = Path(upload_root) / "webreport_backup" / str(analysis_key)
    if not root.is_dir():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return True


def _validate_kept_indices(kept_indices, existing, uploaded):
    """kept_indices(남긴 source 의 원본 idx) 검증 — 위반은 전부 ValueError → 400.

    오름차순·중복 없음·범위 내를 강제하고, 업로드 개수와 길이가 일치해야 한다
    (kept_indices[i] 가 webreport_i 의 원본 idx). 개수가 늘어나는 요청(시트 추가)은 거부한다.
    전체 유지(= range(existing))는 통과시킨다 — 클라는 이 경우 필드를 보내지 않지만 받아도
    삭제 없는 교체와 결과가 같다.
    """
    if not kept_indices:
        raise ValueError("source_indices 가 비어 있습니다 — source 를 전부 지울 수 없습니다.")
    if len(kept_indices) != uploaded:
        raise ValueError(
            f"source_indices 개수 불일치: 업로드 {uploaded}, indices {len(kept_indices)}")
    if len(kept_indices) > existing:
        raise ValueError(
            f"source 추가 불가: 기존 {existing}, 요청 {len(kept_indices)}")
    if len(set(kept_indices)) != len(kept_indices):
        raise ValueError("source_indices 에 중복이 있습니다.")
    if list(kept_indices) != sorted(kept_indices):
        raise ValueError("source_indices 는 오름차순이어야 합니다.")
    if any(not 0 <= i < existing for i in kept_indices):
        raise ValueError(
            f"source_indices 범위 밖: 기존 source {existing}개, 요청 {list(kept_indices)}")


def replace_sources(session_id, *, report_db, upload_root, sources_bytes,
                    kept_indices=None, client_ip: str = "", user_agent: str = "",
                    client_user: str = "", dist_pack=None) -> dict:
    """Honey 가 Excel 편집 후 재인코딩한 parquet 전체로 세션 원본을 덮어쓴다.

    kept_indices: 남긴 source 의 원본 idx 리스트(오름차순). None 이면 전체 교체 —
    개수가 기존과 일치해야 한다(구클라 하위호환). 개수가 줄어드는 요청(Excel 시트 삭제)은
    kept_indices 가 필수이며, 빠진 source 를 물리 제거하고 manifest 의 sources 목록도
    함께 축소해 재저장한다 (manifest 불변 스냅샷 규칙의 유일한 예외 — 남기면 idx 와
    parquet 대응이 어긋난다). 삭제 전 원본은 backup_current_sources 로 1세대 백업된다.

    그 밖의 검증은 각 parquet 가 유효한 honeyform 인지 뿐 — 통과하면 무조건 덮어쓴다.

    dist_pack: Honey 가 재인코딩한 parquet 으로 미리 만든 Distribution pack
    ({"index": json str, "chunks": {id: gzip bytes}}). 있으면 새 content_hash 로 영구
    저장해 서버의 콜드 dist 정렬(수십 초 CPU)을 없앤다 — 업로드 경로와 같은 구조다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    # (1) 각 parquet 가 유효한 honeyform 인지 검증 (실패 시 ValueError → 400).
    # 뼈대(스키마+메타 6행)만 읽는다 — 클라가 encode 시 이미 같은 규칙으로 검증했고,
    # 여기서 전량 디코드하면 수백만 셀 to_numeric 을 하고 결과를 버리게 된다.
    for i, data in enumerate(sources_bytes):
        try:
            validate_parquet_bytes(data)
        except ValueError as exc:
            raise ValueError(f"source_{i}: {exc}") from exc

    # 같은 analysis_key 원본의 read-modify-write 직렬화 — service.edit_raw_data 와 같은
    # 락 키로 동시 편집 lost update 방지 (단일 프로세스 전제, in-process 락).
    with cache.keyed_lock_ctx(("rawedit", analysis_key)):
        existing = sum(
            1 for o in report_db.get_all_object_infos(analysis_key)
            if str(o.get("object_type", "")).startswith("web_report_source_")
        )
        manifest = runtime.storage().load_webreport_manifest(analysis_key, upload_root)
        old_sources = list(manifest.get("sources") or [])
        if not existing:
            # legacy 무기록 세션 — object_info 대신 manifest 로 기존 개수를 잡는다
            existing = len(old_sources)

        # (2) source 개수 검사 / kept_indices 검증
        removed_names = []
        if kept_indices is None:
            if existing and len(sources_bytes) != existing:
                raise ValueError(
                    f"source 개수 불일치: 기존 {existing}, 업로드 {len(sources_bytes)}")
        else:
            _validate_kept_indices(kept_indices, existing, len(sources_bytes))
            if len(kept_indices) < existing:
                keep = set(kept_indices)
                removed_names = [
                    str((old_sources[i] if i < len(old_sources) else {}).get("name")
                        or f"source_{i}")
                    for i in range(existing) if i not in keep]
                manifest = dict(manifest)
                manifest["sources"] = [old_sources[i] for i in kept_indices
                                       if i < len(old_sources)]

        content_hash = hashlib.sha256(
            canon({"files": [hashlib.sha256(b).hexdigest() for b in sources_bytes]})
        ).hexdigest()

        # 덮어쓰기 직전 현재 원본 1세대 백업 — 실패 시 예외로 편집 거부(원본 무손상).
        backup_name = backup_current_sources(
            analysis_key, upload_root,
            old_content_hash=session.get("content_hash") or "")

        storage_result = runtime.storage().save_webreport_sources(
            analysis_key, content_hash, sources_bytes, manifest, upload_root=upload_root)

        # dedup 형제 세션까지 갱신 — 물리 원본이 바뀌었으므로 같은 analysis_key 를 쓰는
        # 다른 세션이 옛 hash 로 stale disk_cache payload 를 서빙하면 안 된다.
        report_db.update_content_hash_for_analysis_key(analysis_key, content_hash)
        # 구 content_hash 키 엔트리는 더 이상 조회되지 않으므로 메모리 회수용으로만 정리
        # (edit_raw_data 와 동일한 무효화 로직).
        cache.evict_akey_caches(analysis_key)
        # 구 세대 Distribution pack 회수 — 새 chash 로는 조회되지 않지만(디렉토리명에 chash)
        # 남겨두면 용량만 먹는다. 이후 조회는 pack 없이 기존 계산 폴백으로 동작한다.
        from . import dist_pack_store

        dist_pack_store.delete_stale(Path(upload_root), analysis_key, content_hash)
        # 클라가 새 parquet 으로 만들어 보낸 pack 을 새 chash 로 저장 — 있으면 이후 조회가
        # 정렬 없이 덧셈만 한다(업로드 직후와 같은 상태). delete_stale 뒤에 저장할 것.
        pack_saved = _save_dist_pack(dist_pack, analysis_key, content_hash,
                                     session.get("mode"), upload_root)
        # 행 위치 기반 전처리 셀 패치는 원본이 바뀌면 엉뚱한 행을 가리킨다 — 형제까지 해제
        # (service.edit_raw_data 와 같은 헬퍼 · 같은 판단).
        dropped = edits.drop_preprocess_edits_for_akey(report_db, analysis_key, user_agent)
    try:
        removed_note = f", removed={removed_names}" if removed_names else ""
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=(f"raw_data(excel, {len(sources_bytes)} sources"
                            f"{removed_note}, backup={backup_name}"
                            + (f", quick_edits_cleared={dropped}" if dropped else "") + ")"),
            client_ip=client_ip, user_agent=user_agent,
            client_user=client_user or None)
    except Exception:
        pass

    # 캐시를 통째로 비웠으므로 다음 조회자가 콜드 리빌드 전부를 부담한다 — 업로드 경로와
    # 같이 프리웜을 걸어 그 계산을 컴퓨트 워커로 넘긴다(요청 스레드 GIL 비점유, 즉시 반환).
    try:
        from . import compute
        compute.prewarm(session_id, str(upload_root))
    except Exception:
        _log.warning("rawdata_replace 프리웜 시작 실패 (session=%s)", session_id,
                     exc_info=True)

    return {"ok": True, "sources": len(sources_bytes), "removed": len(removed_names),
            "storage": storage_result["storage"], "dist_pack_saved": pack_saved}

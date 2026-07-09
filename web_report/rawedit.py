"""Honey 클라 Excel 편집용 raw data export / replace.

세션의 web_report parquet 원본을 zip 으로 내보내고(Honey 가 Excel 로 열어 편집),
Honey 가 재인코딩한 parquet 전체를 받아 기존 analysis_key 원본을 통째로 덮어쓴다.
버전관리/undo 없음 — service.edit_raw_data 와 동일한 content_hash 산출·캐시 무효화·
audit 패턴을 그대로 따른다 (여기는 셀 단위가 아니라 source 전체 교체라는 점만 다름).
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

from . import service
from .honeyform import decode_honeyform_parquet


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

    import storage_gateway
    sources, manifest = storage_gateway.load_webreport_sources(analysis_key, upload_root)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for idx, data in enumerate(sources):
            zf.writestr(f"source_{idx}.parquet", data)
    return buf.getvalue()


def replace_sources(session_id, *, report_db, upload_root, sources_bytes,
                    client_ip: str = "", user_agent: str = "") -> dict:
    """Honey 가 Excel 편집 후 재인코딩한 parquet 전체로 세션 원본을 덮어쓴다.

    검증은 (1) 각 parquet 가 유효한 honeyform 인지, (2) source 개수가 기존과 일치하는지
    두 가지뿐 — 그 외에는 무조건 덮어쓴다. manifest 는 불변이라 기존 것을 그대로 재저장한다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    # (1) 각 parquet 가 유효한 honeyform 인지 검증 (실패 시 ValueError → 400)
    for i, data in enumerate(sources_bytes):
        try:
            decode_honeyform_parquet(data)
        except ValueError as exc:
            raise ValueError(f"source_{i}: {exc}") from exc

    # (2) source 개수 일치 검사
    existing = sum(
        1 for o in report_db.get_all_object_infos(analysis_key)
        if str(o.get("object_type", "")).startswith("web_report_source_")
    )
    if existing and len(sources_bytes) != existing:
        raise ValueError(
            f"source 개수 불일치: 기존 {existing}, 업로드 {len(sources_bytes)}")

    import storage_gateway
    manifest = storage_gateway.load_webreport_manifest(analysis_key, upload_root)

    content_hash = hashlib.sha256(
        service._canon({"files": [hashlib.sha256(b).hexdigest() for b in sources_bytes]})
    ).hexdigest()

    storage_result = storage_gateway.save_webreport_sources(
        analysis_key, content_hash, sources_bytes, manifest, upload_root=upload_root)

    report_db.update_session(session_id, content_hash=content_hash)
    # 구 content_hash 키 엔트리는 더 이상 조회되지 않으므로 메모리 회수용으로만 정리
    # (edit_raw_data 와 동일한 무효화 로직).
    with service._TABLES_CACHE_LOCK:
        for cache in service._AKEY_CACHES:
            for key in [k for k in cache if k[0] == analysis_key]:
                cache.pop(key, None)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"raw_data(excel, {len(sources_bytes)} sources)",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    return {"ok": True, "sources": len(sources_bytes), "storage": storage_result["storage"]}

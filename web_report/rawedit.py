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
import shutil
import time
import zipfile
from pathlib import Path

from . import cache
from . import runtime
from .honeyform import decode_honeyform_parquet
from .validation import canon


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


def replace_sources(session_id, *, report_db, upload_root, sources_bytes,
                    client_ip: str = "", user_agent: str = "",
                    client_user: str = "") -> dict:
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

    # 같은 analysis_key 원본의 read-modify-write 직렬화 — service.edit_raw_data 와 같은
    # 락 키로 동시 편집 lost update 방지 (단일 프로세스 전제, in-process 락).
    with cache.keyed_lock(("rawedit", analysis_key)):
        # (2) source 개수 일치 검사
        existing = sum(
            1 for o in report_db.get_all_object_infos(analysis_key)
            if str(o.get("object_type", "")).startswith("web_report_source_")
        )
        if existing and len(sources_bytes) != existing:
            raise ValueError(
                f"source 개수 불일치: 기존 {existing}, 업로드 {len(sources_bytes)}")

        manifest = runtime.storage().load_webreport_manifest(analysis_key, upload_root)

        content_hash = hashlib.sha256(
            canon({"files": [hashlib.sha256(b).hexdigest() for b in sources_bytes]})
        ).hexdigest()

        # 덮어쓰기 직전 현재 원본 1세대 백업 — 실패 시 예외로 편집 거부(원본 무손상).
        backup_name = backup_current_sources(
            analysis_key, upload_root,
            old_content_hash=session.get("content_hash") or "")

        storage_result = runtime.storage().save_webreport_sources(
            analysis_key, content_hash, sources_bytes, manifest, upload_root=upload_root)

        report_db.update_session(session_id, content_hash=content_hash)
        # 구 content_hash 키 엔트리는 더 이상 조회되지 않으므로 메모리 회수용으로만 정리
        # (edit_raw_data 와 동일한 무효화 로직).
        cache.evict_akey_caches(analysis_key)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"raw_data(excel, {len(sources_bytes)} sources, backup={backup_name})",
            client_ip=client_ip, user_agent=user_agent,
            client_user=client_user or None)
    except Exception:
        pass

    return {"ok": True, "sources": len(sources_bytes), "storage": storage_result["storage"]}

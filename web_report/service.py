"""Service layer for /pe/report/upload_webreport and web report rendering."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from werkzeug.utils import secure_filename

from .honeyform import decode_honeyform_parquet, encode_honeyform_parquet, split_honeyform
from .metrics import build_report_payload
from .tabs import raw_data as raw_data_tab


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _validate_meta(meta: dict) -> dict:
    return {
        "product_type": str(meta.get("product_type") or "").strip(),
        "product": str(meta.get("product") or "").strip(),
        "lot_id": str(meta.get("lot_id") or "").strip(),
        "revision": str(meta.get("revision") or "").strip()[:80],
        "process": str(meta.get("process") or "").strip()[:80],
        "edm_link": str(meta.get("edm_link") or "").strip()[:500],
        "password": str(meta.get("password") or "").strip(),
        "file_name": secure_filename(str(meta.get("file_name") or "web_report")) or "web_report",
    }


def ingest_webreport(manifest: dict, files: list[dict], *, report_db, upload_root: Path,
                     client_ip: str = "", user_agent: str = "") -> dict:
    meta = _validate_meta(manifest.get("meta") or {})
    sources_manifest = manifest.get("sources") or []
    selected_items = manifest.get("selected_items") or []
    sheets = manifest.get("sheets") or []

    file_hashes = []
    decoded = []
    for idx, item in enumerate(files):
        data = item["data"]
        file_hashes.append(hashlib.sha256(data).hexdigest())
        source_info = sources_manifest[idx] if idx < len(sources_manifest) else {}
        source_name = str(source_info.get("name") or item.get("name") or f"source_{idx + 1}")
        file_name = str(source_info.get("file_name") or item.get("filename") or source_name)
        df = decode_honeyform_parquet(data)
        decoded.append({
            "source": source_name,
            "file_name": file_name,
            "df": df,
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

    import storage_gateway
    storage_result = storage_gateway.save_webreport_sources(
        analysis_key, content_hash, [item["bytes"] for item in decoded], manifest,
        upload_root=upload_root)

    session_dir = Path(upload_root) / "web_report" / analysis_key
    report_db.create_session(
        session_id=session_id,
        file_name=meta["file_name"],
        file_path=str(session_dir),
        product_type=meta["product_type"],
        process=meta["process"],
        product=meta["product"],
        revision=meta["revision"],
        edm_link=meta["edm_link"],
        lot_id=meta["lot_id"],
        password=meta["password"],
        source="web_report",
    )
    report_db.update_session(
        session_id, analysis_key=analysis_key, content_hash=content_hash, status="done")

    try:
        report_db.log_audit(
            "upload", session_id=session_id, analysis_key=analysis_key,
            product_type=meta["product_type"], product=meta["product"],
            lot_id=meta["lot_id"], file_name=meta["file_name"],
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    return {
        "session_id": session_id,
        "analysis_key": analysis_key,
        "status": "done",
        "web_report_url": f"/pe/report/view/{session_id}",
        "sources": [item["source"] for item in decoded],
        "item_count": len({str(v) for v in selected_items if str(v)}),
        "storage": storage_result["storage"],
    }


def _load_tables(session_id: str, *, report_db, upload_root: Path):
    """세션 → analysis_key → parquet 원본 디코드 → HoneyformTable 리스트.

    manifest.selected_items 필터는 적용하지 않는다 (build_report_payload 가 이후 그 필터를
    in-place 로 적용하므로, 이 헬퍼는 raw data 조회처럼 전체 item 컬럼이 필요한 호출자에도
    안전하게 재사용된다).
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    import storage_gateway
    sources, manifest = storage_gateway.load_webreport_sources(analysis_key, upload_root=upload_root)

    sources_manifest = manifest.get("sources") or []
    tables = []
    for idx, data in enumerate(sources):
        df = decode_honeyform_parquet(data)
        source_info = sources_manifest[idx] if idx < len(sources_manifest) else {}
        source_name = str(source_info.get("name") or f"source_{idx + 1}")
        file_name = str(source_info.get("file_name") or source_name)
        tables.append(split_honeyform(df, source=source_name, file_name=file_name))
    return session, tables, manifest


def load_webreport(session_id: str, *, report_db, upload_root: Path) -> tuple[dict, dict]:
    """세션 재계산: parquet 원본을 다시 받아 build_report_payload 를 매번 새로 실행한다."""
    session, tables, manifest = _load_tables(session_id, report_db=report_db, upload_root=upload_root)

    report = build_report_payload(
        tables,
        selected_items=manifest.get("selected_items") or [],
        sheets=manifest.get("sheets") or [],
        etc_items=manifest.get("etc_items") or [],
    )
    public = dict(session)
    public["has_password"] = bool(public.get("password"))
    public.pop("password", None)
    return public, report


def get_raw_data_columns(session_id: str, *, report_db, upload_root: Path) -> dict:
    """Raw Data 탭 컬럼 선택 UI용: item 메타 + source 목록 + 전체 die 수."""
    _, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return raw_data_tab.build_raw_data_columns(tables)


def query_raw_data(session_id: str, *, report_db, upload_root: Path, columns,
                   search="", bin_filter="", source_filter="") -> dict:
    """Raw Data 탭 조회: 선택된 columns + 필터로 행을 반환 (60개 컬럼 상한, ValueError 로 초과 통지)."""
    _, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return raw_data_tab.query_raw_data(
        tables, columns=columns, search=search, bin_filter=bin_filter, source_filter=source_filter)


def edit_raw_data(session_id: str, *, report_db, upload_root: Path, edits: list,
                  client_ip: str = "", user_agent: str = "") -> dict:
    """Raw Data 셀 편집을 저장된 parquet 원본에 그대로 반영한다.

    버전관리·undo 없음 — 편집된 source 는 df 기준으로 재인코딩해 기존 analysis_key 의
    web_report_source_<idx> 를 덮어쓴다 (Honey 재업로드 전까지 이전 값은 복구 불가).
    """
    session, tables, manifest = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    updated_tables = raw_data_tab.apply_raw_data_edits(tables, edits)
    sources_bytes = [encode_honeyform_parquet(t.df) for t in updated_tables]

    content_hash = hashlib.sha256(
        _canon({"files": [hashlib.sha256(b).hexdigest() for b in sources_bytes]})
    ).hexdigest()

    import storage_gateway
    storage_result = storage_gateway.save_webreport_sources(
        analysis_key, content_hash, sources_bytes, manifest, upload_root=upload_root)

    report_db.update_session(session_id, content_hash=content_hash)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"raw_data({len(edits)} cells)",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    return {"ok": True, "edited_cells": len(edits), "storage": storage_result["storage"]}


def update_issue_etc_items(session_id: str, *, report_db, upload_root: Path,
                           add: str = "", remove: str = "",
                           client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table ETC 섹션에 ENGR 가 임의로 추가/삭제한 item 이름을 manifest.etc_items 에 반영한다.

    Bin/TNO/Distribution 값 자체는 저장하지 않는다 — item 이름만 기억해두고, 조회할 때마다
    build_issue_table_rows 가 tables/yield_rows 에서 그때그때 다시 채운다(rule #4 의
    analysis_key 불변 원칙과 무관하게, 여기선 sources 원본이 그대로이므로 content_hash 도
    사실상 동일하게 재계산될 뿐이다).
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    import storage_gateway
    sources, manifest = storage_gateway.load_webreport_sources(analysis_key, upload_root=upload_root)

    all_items = set()
    for data in sources:
        df = decode_honeyform_parquet(data)
        table = split_honeyform(df, source="_", file_name="_")
        all_items.update(table.item_columns)

    etc_items = list(manifest.get("etc_items") or [])
    add = str(add or "").strip()
    remove = str(remove or "").strip()
    if add:
        if add not in all_items:
            raise ValueError(f"unknown item: {add}")
        if add not in etc_items:
            etc_items.append(add)
    if remove:
        etc_items = [it for it in etc_items if it != remove]
    manifest["etc_items"] = etc_items

    content_hash = hashlib.sha256(
        _canon({"files": [hashlib.sha256(b).hexdigest() for b in sources]})
    ).hexdigest()
    storage_result = storage_gateway.save_webreport_sources(
        analysis_key, content_hash, sources, manifest, upload_root=upload_root)

    report_db.update_session(session_id, content_hash=content_hash)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"issue_table_etc_items(add={add!r},remove={remove!r})",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    return {"ok": True, "etc_items": etc_items, "storage": storage_result["storage"]}

"""Service layer for /pe/report/upload_webreport and web report rendering."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from werkzeug.utils import secure_filename

from .honeyform import decode_honeyform_parquet, split_honeyform
from .metrics import build_report_payload


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

    session_dir = upload_root / "web_report" / analysis_key
    session_dir.mkdir(parents=True, exist_ok=True)
    for idx, item in enumerate(decoded):
        (session_dir / f"source_{idx}.parquet").write_bytes(item["bytes"])
    (session_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

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
        session_id, analysis_key=analysis_key, content_hash=content_hash, status="uploading")

    tables = [
        split_honeyform(item["df"], source=item["source"], file_name=item["file_name"])
        for item in decoded
    ]
    report = build_report_payload(tables, selected_items=selected_items, sheets=sheets)

    sheets_payload = report.get("sheets", {})
    report_db.upsert_sheet_data(analysis_key, "web_report", report)
    report_db.upsert_sheet_data(analysis_key, "yield", sheets_payload.get("Yield", []))
    report_db.upsert_sheet_data(
        analysis_key, "issue_table", sheets_payload.get("Issue Table", []))
    report_db.update_session(session_id, status="done")

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
        "sources": [t.source for t in tables],
        "item_count": len(report.get("selected_items", [])),
        "storage": "local_pending_s3",
    }


def load_webreport(session_id: str, *, report_db) -> tuple[dict, dict]:
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    report = report_db.get_sheet_data(session.get("analysis_key"), "web_report")
    if report is None:
        raise FileNotFoundError(session_id)
    public = dict(session)
    public["has_password"] = bool(public.get("password"))
    public.pop("password", None)
    return public, report

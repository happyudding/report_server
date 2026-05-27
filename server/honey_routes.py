"""Honey 클라이언트 자동 업데이트 채널.

- GET /honey/version : releases/version.json 그대로 반환
- GET /honey/download : 최신 exe 파일 서빙 (Content-Disposition attachment)

version.json 예시:
  {
    "version": "0.1.0",
    "file": "Honey-0.1.0.exe",
    "sha256": "<hex>",
    "released_at": "2026-05-27T10:00:00",
    "notes": "initial release"
  }
"""
import json

from flask import Blueprint, abort, jsonify, send_file

from config import HONEY_RELEASES_DIR, HONEY_VERSION_JSON

honey_bp = Blueprint("honey", __name__, url_prefix="/honey")


@honey_bp.get("/version")
def get_version():
    if not HONEY_VERSION_JSON.exists():
        return jsonify({"error": "version.json not found", "version": None}), 404
    try:
        data = json.loads(HONEY_VERSION_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({"error": f"version.json invalid: {exc}"}), 500
    return jsonify(data)


@honey_bp.get("/download")
def download_exe():
    if not HONEY_VERSION_JSON.exists():
        abort(404, "no release published")
    try:
        manifest = json.loads(HONEY_VERSION_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        abort(500, "version.json invalid")

    filename = manifest.get("file")
    if not filename:
        abort(500, "version.json missing 'file' field")
    if "/" in filename or "\\" in filename or filename.startswith("."):
        abort(400, "invalid filename in version.json")

    exe_path = HONEY_RELEASES_DIR / filename
    if not exe_path.exists():
        abort(404, f"release file not found: {filename}")

    return send_file(
        str(exe_path),
        as_attachment=True,
        download_name=filename,
        mimetype="application/octet-stream",
    )

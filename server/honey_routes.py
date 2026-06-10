"""Honey client update channel.

- GET /honey/version: return releases/version.json as-is.
- GET /honey/download: serve the release ZIP named by version.json.file.
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
def download_release():
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

    release_path = HONEY_RELEASES_DIR / filename
    if not release_path.exists():
        abort(404, f"release file not found: {filename}")

    mimetype = "application/zip" if filename.lower().endswith(".zip") else "application/octet-stream"
    return send_file(
        str(release_path),
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype,
    )

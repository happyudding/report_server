from pathlib import Path

from flask import Blueprint, abort, jsonify, redirect, send_from_directory

from config import DATASETS_DIR

bp = Blueprint("cumulative", __name__)
DEFAULT_DATASET = "current"


def _safe(id):
    return bool(id) and len(id) <= 80 and all(c.isalnum() or c in "-_" for c in id)


def _send(id, *parts):
    if not _safe(id):
        abort(400)
    dir_ = DATASETS_DIR / id / Path(*parts[:-1]) if len(parts) > 1 else DATASETS_DIR / id
    name = parts[-1]
    if not (dir_ / name).exists():
        abort(404)
    resp = send_from_directory(dir_, name)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/")
def index():
    return redirect(f"/view/{DEFAULT_DATASET}", code=302)


@bp.get("/view/<id>")
def view(id):
    return _send(id, "cumulative.html")


@bp.get("/api/<id>/chart/<int:sid>")
def chart(id, sid):
    return _send(id, "charts", f"{sid}.json")


@bp.get("/api/<id>/thumb/<int:sid>")
def thumb(id, sid):
    return _send(id, "thumbs", f"{sid}.svg")


@bp.get("/api/<id>/build_version")
def build_version(id):
    if not _safe(id):
        abort(400)
    path = DATASETS_DIR / id / "build_version.txt"
    if not path.exists():
        abort(404)
    return jsonify({"version": path.read_text(encoding="utf-8").strip(), "dataset_id": id})

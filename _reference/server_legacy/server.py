import json
import secrets
import threading
import time
from io import BytesIO
from pathlib import Path

from flask import Blueprint, abort, jsonify, redirect, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from analysis import dataset_builder
from config import DATASETS_DIR
from server.dash_dashboard import send_fail_png
from analysis.table_builder import build_raw_xlsx
from server.xlsx_export import build_report_xlsx

bp = Blueprint("cumulative", __name__)
DEFAULT_DATASET = "current"

_build_status = {}
_build_lock = threading.Lock()

# ── XLSX async build jobs ─────────────────────────────────────────────────────
# job_id → {dataset_id, percent, stage, done, data: bytes|None, error, created_at}
_XLSX_JOBS: dict[str, dict] = {}
_XLSX_LOCK = threading.Lock()
_XLSX_JOB_TTL_SEC = 600  # 다운로드 안 받아도 10분 후 GC


def _xlsx_gc_expired():
    """오래된 job 결과를 메모리에서 제거."""
    now = time.time()
    with _XLSX_LOCK:
        expired = [jid for jid, j in _XLSX_JOBS.items()
                   if now - j.get("created_at", now) > _XLSX_JOB_TTL_SEC]
        for jid in expired:
            _XLSX_JOBS.pop(jid, None)


def _xlsx_set_progress(job_id: str, percent: int, stage: str):
    with _XLSX_LOCK:
        j = _XLSX_JOBS.get(job_id)
        if j is not None:
            j["percent"] = int(percent)
            j["stage"] = stage


def _xlsx_set_done(job_id: str, *, data: bytes | None = None, error: str | None = None):
    with _XLSX_LOCK:
        j = _XLSX_JOBS.get(job_id)
        if j is not None:
            j["done"] = True
            j["data"] = data
            j["error"] = error
            if error is None:
                j["percent"] = 100
                j["stage"] = "완료"


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


def _set_status(s):
    with _build_lock:
        _build_status[s["dataset_id"]] = s


def _bg_build(dataset_id, inputs):
    try:
        r = dataset_builder.build_dataset(dataset_id, inputs, progress_cb=_set_status)
        _set_status({
            "dataset_id": dataset_id, "stage": "done",
            "current": r["n_subjects"], "total": r["n_subjects"],
            "elapsed_s": r["elapsed_s"], "result": r,
        })
    except Exception as e:
        _set_status({"dataset_id": dataset_id, "stage": "error", "error": str(e)})


@bp.get("/")
def index():
    return redirect(f"/view/{DEFAULT_DATASET}", code=302)


@bp.post("/upload")
def upload():
    files = request.files.getlist("files")
    if not files:
        abort(400, "No files")
    dataset_id = f"{int(time.time())}_{secrets.token_hex(3)}"
    inputs = {}
    for f in files:
        name = secure_filename(f.filename or "")
        if name:
            inputs[name] = f.read()
    if not inputs:
        abort(400, "No valid filenames")
    _set_status({"dataset_id": dataset_id, "stage": "queued", "current": 0, "total": 0, "elapsed_s": 0})
    threading.Thread(target=_bg_build, args=(dataset_id, inputs), daemon=True).start()
    return jsonify({"dataset_id": dataset_id, "url": f"/view/{dataset_id}"})


@bp.get("/view/<id>")
def view(id):
    if not _safe(id):
        abort(400)
    if (DATASETS_DIR / id / "cumulative.html").exists():
        return _send(id, "cumulative.html")
    # 빌드가 진행 중이면 placeholder 가 폴링하도록, 아니면 업로드 페이지로 보낸다.
    with _build_lock:
        in_progress = _build_status.get(id, {}).get("stage") in ("queued", "save_inputs", "load_csv", "table_json", "cdf_svg", "write_page")
    if in_progress:
        return _placeholder_html(id), 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}
    return redirect("/pe/report/", code=302)


@bp.get("/api/<id>/chart/<int:sid>")
def chart(id, sid):
    return _send(id, "charts", f"{sid}.json")


@bp.get("/view_histogram/<id>")
def view_histogram(id):
    if not _safe(id):
        abort(400)
    from analysis.histogram_page import build_histogram_html
    from analysis.table_builder import read_table_json

    meta = read_table_json(id, "meta")
    if not meta:
        return _placeholder_html(id), 200, {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-cache",
        }
    subjects = [s["subject"] for s in (meta.get("subjects") or [])]
    sources = meta.get("sources") or []
    from analysis.dataset_builder import COLOR_PALETTE
    schools = [
        {"name": n, "color": COLOR_PALETTE[i % len(COLOR_PALETTE)]}
        for i, n in enumerate(sources)
    ]
    html = build_histogram_html(id, subjects, schools)
    return html, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-cache",
    }


@bp.get("/api/<id>/histogram_chart/<int:sid>")
def histogram_chart(id, sid):
    if not _safe(id):
        abort(400)
    from analysis.histogram_chart import build_histogram_payload
    try:
        payload = build_histogram_payload(id, sid)
    except Exception as exc:
        abort(500, f"histogram build failed: {exc}")
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.get("/api/<id>/histogram_thumb/<int:sid>")
def histogram_thumb(id, sid):
    from flask import Response
    if not _safe(id):
        abort(400)
    from analysis.histogram_chart import build_histogram_svg
    try:
        svg = build_histogram_svg(id, sid)
    except Exception as exc:
        abort(500, f"histogram thumb failed: {exc}")
    resp = Response(svg, mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
    return resp


@bp.get("/api/<id>/thumb/<int:sid>")
def thumb(id, sid):
    if not _safe(id):
        abort(400)
    dir_ = DATASETS_DIR / id / "thumbs"
    name = f"{sid}.svg"
    if not (dir_ / name).exists():
        abort(404)
    resp = send_from_directory(dir_, name)
    resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
    return resp


@bp.get("/api/<id>/fail_png/<int:sid>")
def fail_png(id, sid):
    if not _safe(id):
        abort(400)
    return send_fail_png(id, sid)


@bp.get("/api/<id>/raw_xlsx")
def raw_xlsx(id):
    if not _safe(id):
        abort(400)
    if not (DATASETS_DIR / id / "input").exists():
        abort(404)
    try:
        data = build_raw_xlsx(id)
    except FileNotFoundError:
        abort(404)
    resp = send_file(
        BytesIO(data),
        as_attachment=True,
        download_name=f"{id}_raw.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/api/<id>/report_xlsx")
def report_xlsx(id):
    """동기 빌드 — 기존 사용처(예: 외부 스크립트) 호환용. UI 는 async 경로 사용."""
    if not _safe(id):
        abort(400)
    if not (DATASETS_DIR / id / "tables" / "meta.json").exists():
        abort(404)
    include_raw = request.args.get("include_raw") in ("1", "true", "yes")
    try:
        data = build_report_xlsx(id, include_raw=include_raw)
    except Exception as exc:
        abort(500, f"Report generation failed: {exc}")
    suffix = "_with_raw" if include_raw else ""
    resp = send_file(
        BytesIO(data),
        as_attachment=True,
        download_name=f"{id}_report{suffix}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ── XLSX async build: start / progress / download ────────────────────────────

@bp.post("/api/<id>/report_xlsx_start")
def report_xlsx_start(id):
    """백그라운드 빌드 시작. body: {include_raw: bool}. 응답: {job_id}."""
    if not _safe(id):
        abort(400)
    if not (DATASETS_DIR / id / "tables" / "meta.json").exists():
        abort(404)

    _xlsx_gc_expired()

    body = request.get_json(silent=True) or {}
    include_raw = bool(body.get("include_raw"))

    job_id = secrets.token_hex(8)
    with _XLSX_LOCK:
        _XLSX_JOBS[job_id] = {
            "dataset_id": id,
            "percent": 0,
            "stage": "대기 중",
            "done": False,
            "data": None,
            "error": None,
            "created_at": time.time(),
            "include_raw": include_raw,
        }

    def _run():
        try:
            def _cb(pct, stage):
                _xlsx_set_progress(job_id, pct, stage)
            data = build_report_xlsx(id, include_raw=include_raw, progress_cb=_cb)
            _xlsx_set_done(job_id, data=data)
        except Exception as exc:
            _xlsx_set_done(job_id, error=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "include_raw": include_raw})


@bp.get("/api/<id>/report_xlsx_progress/<job_id>")
def report_xlsx_progress(id, job_id):
    if not _safe(id) or not _safe(job_id):
        abort(400)
    with _XLSX_LOCK:
        j = _XLSX_JOBS.get(job_id)
        if j is None or j.get("dataset_id") != id:
            abort(404)
        return jsonify({
            "percent": j.get("percent", 0),
            "stage": j.get("stage", ""),
            "done": bool(j.get("done")),
            "error": j.get("error"),
            "include_raw": bool(j.get("include_raw")),
        })


@bp.get("/api/<id>/report_xlsx_download/<job_id>")
def report_xlsx_download(id, job_id):
    if not _safe(id) or not _safe(job_id):
        abort(400)
    with _XLSX_LOCK:
        j = _XLSX_JOBS.get(job_id)
        if j is None or j.get("dataset_id") != id:
            abort(404)
        if not j.get("done"):
            abort(409, "build not finished")
        if j.get("error"):
            abort(500, j["error"])
        data = j.get("data")
        include_raw = bool(j.get("include_raw"))
        # 다운로드 직후 결과를 메모리에서 제거.
        _XLSX_JOBS.pop(job_id, None)
    if not data:
        abort(500, "no data")
    suffix = "_with_raw" if include_raw else ""
    resp = send_file(
        BytesIO(data),
        as_attachment=True,
        download_name=f"{id}_report{suffix}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/api/<id>/yield_comments")
def yield_comments_get(id):
    if not _safe(id):
        abort(400)
    path = DATASETS_DIR / id / "tables" / "yield_comments.json"
    if not path.exists():
        return jsonify({})
    try:
        return jsonify(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return jsonify({})


@bp.post("/api/<id>/yield_comments")
def yield_comments_post(id):
    if not _safe(id):
        abort(400)
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload, dict):
        abort(400, "payload must be a dict")
    safe_payload = {str(k): str(v) for k, v in payload.items() if v not in (None, "")}
    path = DATASETS_DIR / id / "tables" / "yield_comments.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return jsonify({"ok": True, "count": len(safe_payload)})


@bp.get("/api/<id>/build_version")
def build_version(id):
    if not _safe(id):
        abort(400)
    path = DATASETS_DIR / id / "build_version.txt"
    if not path.exists():
        abort(404)
    return jsonify({"version": path.read_text(encoding="utf-8").strip(), "dataset_id": id})


@bp.get("/api/<id>/build_status")
def build_status_endpoint(id):
    if not _safe(id):
        abort(400)
    with _build_lock:
        s = _build_status.get(id)
    if s:
        return jsonify(s)
    if (DATASETS_DIR / id / "cumulative.html").exists():
        return jsonify({"dataset_id": id, "stage": "done", "current": 1, "total": 1, "elapsed_s": 0})
    abort(404)


def _placeholder_html(dataset_id):
    return f"""<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><title>Building {dataset_id}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 40px 16px; background: #fafafa; color: #333; }}
  .card {{ max-width: 640px; margin: 0 auto; background: #fff; padding: 32px; border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); }}
  h1 {{ font-size: 18px; margin: 0 0 6px; font-weight: 600; }}
  .id {{ font-family: monospace; color: #999; font-size: 12px; margin-bottom: 24px; }}
  .bar {{ height: 12px; background: #eef; border-radius: 6px; overflow: hidden; margin: 18px 0 10px; }}
  .fill {{ height: 100%; background: linear-gradient(90deg, #4a90e2, #3a7cc5); width: 0%; transition: width 0.4s ease; }}
  .status {{ font-size: 13px; color: #555; font-family: monospace; white-space: pre; line-height: 1.5; }}
  .err {{ color: #c00; font-weight: 600; margin-top: 8px; }}
</style></head><body>
<div class="card">
  <h1>데이터셋 빌드 중...</h1>
  <div class="id">dataset_id: {dataset_id}</div>
  <div class="bar"><div class="fill" id="fill"></div></div>
  <div class="status" id="status">starting...</div>
</div>
<script>
const ID = {dataset_id!r};
const STAGES = {{
  queued: "대기 중",
  save_inputs: "1/5 CSV 저장",
  load_csv: "2/5 CSV 파싱",
  table_json: "3/5 테이블 JSON 생성",
  cdf_svg: "4/5 CDF 계산 + SVG 생성",
  write_page: "5/5 HTML 생성",
  done: "완료",
  error: "에러",
}};
async function poll() {{
  try {{
    const r = await fetch(`/api/${{ID}}/build_status`, {{cache: 'no-store'}});
    if (!r.ok) {{ setTimeout(poll, 2000); return; }}
    const s = await r.json();
    const label = STAGES[s.stage] || s.stage;
    const pct = s.total > 0 ? Math.round(s.current / s.total * 100) : (s.stage === 'done' ? 100 : 5);
    document.getElementById('fill').style.width = pct + '%';
    const eta = s.current > 0 && s.total > s.current
      ? Math.round((s.elapsed_s / s.current) * (s.total - s.current)) + 's'
      : '-';
    const text = `${{label}}\\n${{s.current||0}}/${{s.total||0}} (${{pct}}%)  elapsed ${{s.elapsed_s||0}}s  ETA ${{eta}}`;
    document.getElementById('status').textContent = text;
    if (s.stage === 'error') {{
      const e = document.createElement('div');
      e.className = 'err';
      e.textContent = 'ERROR: ' + (s.error || '');
      document.getElementById('status').after(e);
      return;
    }}
    if (s.stage === 'done') {{ setTimeout(() => window.location.reload(), 600); return; }}
  }} catch (e) {{ /* swallow */ }}
  setTimeout(poll, 1000);
}}
poll();
</script>
</body></html>"""

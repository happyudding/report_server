"""eval 패널 blueprint — 얇은 HTTP 핸들러. 구현은 rules_io / web_report.eval_debug.

접근 게이트: admin 과 같은 비밀번호로 발급하는 별도 쿠키(pe_admin_gate_eval).
변경요청(비-GET)은 admin_panel 과 같은 X-Admin-Request: 1 헤더를 요구한다.
"""
import hashlib
import hmac
import logging
import re
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, Response, abort, jsonify, request  # noqa: F401 (Response=CSV)

import config
from admin_panel import GATE_COOKIE_EVAL, GATE_COOKIE_EVAL_PATH, eval_gate_token
from database import report_db
from report.static_pages import send_html_gzip

# web_report 패키지는 repo 루트에 있다 (report_routes.py 와 같은 가드).
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from eval_panel import rules_io, trace_store          # noqa: E402
from web_report import eval_debug                     # noqa: E402

_log = logging.getLogger(__name__)

eval_panel_bp = Blueprint("eval_panel", __name__)

_PANEL_HTML = Path(__file__).resolve().parent / "eval_panel.html"
_LOGIN_HTML = Path(__file__).resolve().parent / "eval_login.html"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# 트레이스는 콜드 세션이면 수 초 CPU — 관리자 전용이라 동시 1건으로 묶는다.
_trace_lock = threading.Lock()
_TRACE_DEFAULT_MAX_CASES = 400
_LOGIN_PAGE_CACHE = None


def _login_page():
    global _LOGIN_PAGE_CACHE
    if _LOGIN_PAGE_CACHE is None:
        _LOGIN_PAGE_CACHE = _LOGIN_HTML.read_text(encoding="utf-8")
    return Response(_LOGIN_PAGE_CACHE, status=401, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


@eval_panel_bp.before_request
def _auth_gate():
    if request.endpoint == "eval_panel.login":
        return None
    if hmac.compare_digest(request.cookies.get(GATE_COOKIE_EVAL, ""), eval_gate_token()):
        return None
    if request.endpoint == "eval_panel.panel_page":
        return _login_page()
    abort(401, "eval panel login required")


@eval_panel_bp.before_request
def _guard_mutations():
    if request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.headers.get("X-Admin-Request") != "1":
        abort(403, "X-Admin-Request header required")


@eval_panel_bp.post("/login")
def login():
    body = request.get_json(force=True, silent=True) or {}
    if (body.get("password") or "").strip() != config.REPORT_ADMIN_PASSWORD:
        return jsonify({"ok": False}), 401
    resp = jsonify({"ok": True})
    resp.set_cookie(GATE_COOKIE_EVAL, eval_gate_token(), max_age=12 * 3600,
                    httponly=True, samesite="Lax", secure=request.is_secure,
                    path=GATE_COOKIE_EVAL_PATH)
    return resp


@eval_panel_bp.get("/")
def panel_page():
    return send_html_gzip(_PANEL_HTML)


def _audit(action, changed_fields=None, result="ok"):
    """룰 변경 감사 (best-effort). client_user='eval-panel' 로 구분."""
    try:
        fwd = request.headers.get("X-Forwarded-For")
        ip = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
        report_db.log_audit(action, changed_fields=changed_fields, client_ip=ip,
                            user_agent=str(request.user_agent),
                            client_user="eval-panel", result=result)
    except Exception:
        pass


def _rule_error(exc):
    return jsonify({"ok": False, "error": str(exc)}), 400


# ── 메타 ─────────────────────────────────────────────────────────────────────

@eval_panel_bp.get("/api/meta")
def api_meta():
    files = eval_debug.rules_files()
    return jsonify({
        "taxonomy": eval_debug.taxonomy(),
        "threshold_keys": sorted(eval_debug.default_thresholds()),
        "default": eval_debug.default_thresholds(),
        "signature_ids": [s.get("id") for s in eval_debug.signatures_raw()],
        "rules_rev": eval_debug.rules_rev(),
        "rules_dir": str(eval_debug.rules_dir()),
        "files": {k: _file_info(v) for k, v in files.items()},
    })


def _file_info(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return {"path": str(path), "exists": False}
    return {"path": str(path), "exists": True, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()[:16],
            "mtime": int(path.stat().st_mtime)}


# ── thresholds ───────────────────────────────────────────────────────────────

@eval_panel_bp.get("/api/thresholds")
def api_thresholds():
    pt = (request.args.get("pt") or "").strip()
    family = (request.args.get("family") or "").strip() or None
    try:
        return jsonify(rules_io.read_thresholds(pt, family))
    except rules_io.RuleError as exc:
        return _rule_error(exc)


@eval_panel_bp.put("/api/thresholds")
def api_thresholds_save():
    body = request.get_json(force=True, silent=True) or {}
    pt = str(body.get("pt") or "").strip()
    family = str(body.get("family") or "").strip() or None
    try:
        result = rules_io.save_thresholds(pt, family, body.get("overrides") or {})
    except rules_io.RuleError as exc:
        _audit("eval_rules_edit", changed_fields=[f"thresholds:{pt}/{family or '_default'}"],
               result="error")
        return _rule_error(exc)
    _audit("eval_rules_edit",
           changed_fields=[f"thresholds:{pt}/{family or '_default'}",
                           f"keys={sorted(result['saved'])}", f"rev={result['rules_rev']}"])
    result["ok"] = True
    return jsonify(result)


# ── signatures ───────────────────────────────────────────────────────────────

@eval_panel_bp.get("/api/signatures")
def api_signatures():
    return jsonify(rules_io.read_signatures())


@eval_panel_bp.post("/api/signatures/enabled")
def api_signatures_bulk_enabled():
    """선택한 signature 여러 개를 한 번에 활성/비활성."""
    body = request.get_json(force=True, silent=True) or {}
    enabled = bool(body.get("enabled"))
    try:
        result = rules_io.set_signatures_enabled(body.get("ids") or [], enabled)
    except rules_io.RuleError as exc:
        return _rule_error(exc)
    if result["changed"]:
        _audit("eval_rules_edit",
               changed_fields=[f"signatures_enabled={enabled}",
                               f"ids={result['changed']}", f"rev={result['rules_rev']}"])
    result["ok"] = True
    return jsonify(result)


@eval_panel_bp.put("/api/signatures/<sig_id>")
def api_signature_save(sig_id):
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = rules_io.save_signature(sig_id, body)
    except rules_io.RuleError as exc:
        _audit("eval_rules_edit", changed_fields=[f"signature:{sig_id}"], result="error")
        return _rule_error(exc)
    _audit("eval_rules_edit",
           changed_fields=[f"signature:{sig_id}", f"fields={result['updated']}",
                           f"rev={result['rules_rev']}"])
    result["ok"] = True
    return jsonify(result)


# ── 유지보수 ─────────────────────────────────────────────────────────────────

@eval_panel_bp.post("/api/reload")
def api_reload():
    eval_debug.reload_rules()
    rev = eval_debug.bump_rules_rev()
    _audit("eval_rules_edit", changed_fields=["reload", f"rev={rev}"])
    return jsonify({"ok": True, "rules_rev": rev})


@eval_panel_bp.get("/api/validate")
def api_validate():
    return jsonify(rules_io.validate_all())


@eval_panel_bp.get("/api/backups")
def api_backups():
    return jsonify({"backups": rules_io.list_backups()})


@eval_panel_bp.post("/api/backups/restore")
def api_backup_restore():
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = rules_io.restore_backup(str(body.get("name") or ""))
    except rules_io.RuleError as exc:
        return _rule_error(exc)
    _audit("eval_rules_edit", changed_fields=[f"restore:{body.get('name')}",
                                              f"rev={result['rules_rev']}"])
    result["ok"] = True
    return jsonify(result)


# ── Eval DB (Issue Table 코멘트 export — web_report/eval_export.py) ──────────
# 2026-08-03 admin_panel 에서 이관. 구현은 그대로 admin_panel/eval_admin.py 를 쓴다
# (모듈 위치는 admin 이지만 eval DB 전용 헬퍼라 옮기지 않았다 — import 만 한다).

_CASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")


@eval_panel_bp.get("/api/eval/overview")
def api_eval_overview():
    from admin_panel import eval_admin
    return jsonify(eval_admin.overview())


@eval_panel_bp.get("/api/eval/labels")
def api_eval_labels():
    from admin_panel import eval_admin
    return jsonify(eval_admin.list_labels(
        q=(request.args.get("q") or "").strip() or None,
        limit=request.args.get("limit", 100),
        offset=request.args.get("offset", 0),
    ))


@eval_panel_bp.get("/api/eval/labels.csv")
def api_eval_labels_csv():
    """코멘트 라벨 전체 → db_input 단순 5컬럼 CSV (수정 후 run_import.bat 재적재용)."""
    from admin_panel import eval_admin
    return Response(eval_admin.labels_csv_iter(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=eval_labels.csv",
                             "Cache-Control": "no-store"})


@eval_panel_bp.post("/api/eval/cases/delete")
def api_eval_cases_delete():
    from admin_panel import eval_admin
    body = request.get_json(force=True, silent=True) or {}
    cids = body.get("case_ids")
    if not isinstance(cids, list) or not cids or len(cids) > 200:
        abort(400, "case_ids: 1~200개 리스트 필요")
    for cid in cids:
        if not isinstance(cid, str) or not _CASE_ID_RE.match(cid):
            abort(400, f"invalid case_id: {cid!r}")
    result = eval_admin.delete_cases(cids)
    _audit("delete", changed_fields=[f"eval_cases({result.get('deleted', 0)})"])
    return jsonify(result)


@eval_panel_bp.post("/api/eval/items/value_type")
def api_eval_set_value_type():
    """Unit 그룹(value_type) 수동 지정 — 선례검색 하드필터라 오분류 교정용."""
    from admin_panel import eval_admin
    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("item_ids")
    value_type = body.get("value_type")
    if not isinstance(ids, list) or not ids or len(ids) > 200:
        abort(400, "item_ids: 1~200개 리스트 필요")
    if any(not isinstance(i, int) for i in ids):
        abort(400, "item_ids: 정수만 허용")
    if value_type not in eval_admin.VALUE_TYPES:
        abort(400, f"value_type: {eval_admin.VALUE_TYPES} 중 하나여야 함")
    result = eval_admin.set_item_value_type(ids, value_type)
    _audit("edit", changed_fields=[f"eval_value_type({value_type}x{result.get('updated', 0)})"])
    return jsonify(result)


@eval_panel_bp.post("/api/eval/items/remap_units")
def api_eval_remap_units():
    """저장된 unit 원문에 별칭 규칙(VOLT/AMP/HERTZ)을 일괄 재적용."""
    from admin_panel import eval_admin
    body = request.get_json(force=True, silent=True) or {}
    dry_run = bool(body.get("dry_run"))
    result = eval_admin.remap_unit_aliases(dry_run=dry_run)
    if not dry_run:
        _audit("edit", changed_fields=[f"eval_remap_units({result.get('changed', 0)})"])
    return jsonify(result)


@eval_panel_bp.post("/api/eval/session/<session_id>/reexport")
def api_eval_reexport(session_id):
    from admin_panel import eval_admin
    if not _SESSION_ID_RE.match(session_id):
        abort(400, "invalid session_id")
    if not report_db.get_session(session_id):
        abort(404, "session not found")
    result = eval_admin.reexport(session_id)
    _audit("edit", changed_fields=[f"eval_reexport({session_id}:{result})"])
    return jsonify(result)


# ── 정답 라벨 / 채점 ─────────────────────────────────────────────────────────

_STATUS_VOCAB = ("OK", "MONITOR", "MINOR", "MAJOR", "CRITICAL")


@eval_panel_bp.post("/api/eval/label")
def api_eval_label():
    """트레이스 케이스 1건에 대한 정답 라벨 저장 — (엔진 판정, 사람 정답) 쌍.

    트레이스 토큰에서 케이스 스냅샷을 읽어 관리자가 화면에서 본 판정 그대로를
    evaluation 으로 영속화하고, label.eval_id 로 연결한다 (채점의 원재료).
    """
    from web_report import eval_export
    body = request.get_json(force=True, silent=True) or {}
    token = str(body.get("token") or "")
    index = body.get("index")
    result = trace_store.get(token)
    if result is None:
        return jsonify({"ok": False,
                        "error": "트레이스 결과가 만료됐습니다 — 다시 실행하세요"}), 404
    cases = result.get("cases") or []
    if not isinstance(index, int) or index < 0 or index >= len(cases):
        return jsonify({"ok": False, "error": "케이스 번호 범위 밖"}), 400
    case = cases[index]

    accepted = bool(body.get("accepted"))
    human_status = str(body.get("human_status") or "").strip()
    if not accepted and human_status not in _STATUS_VOCAB:
        return jsonify({"ok": False,
                        "error": f"정정 status 는 {_STATUS_VOCAB} 중 하나여야 합니다"}), 400
    human_comment = str(body.get("human_comment") or "").strip()[:2000]
    root_cause = str(body.get("root_cause") or "").strip()[:100]

    session = report_db.get_session(result["session_id"])
    if not session:
        return jsonify({"ok": False, "error": "세션 없음"}), 404
    try:
        saved = eval_export.save_human_label(
            dict(session),
            item=str(case.get("item_raw") or ""), bin_=case.get("bin"),
            item_class=str(case.get("item_class") or ""),
            engine={"engine_version": result.get("engine_version"),
                    "status": case.get("status"),
                    "confidence": case.get("confidence"),
                    "data_completeness": case.get("data_completeness"),
                    "comment": case.get("comment"),
                    "primary_signature": case.get("primary_signature"),
                    "secondary_signatures": case.get("secondary_signatures")},
            human={"accepted": accepted, "human_status": human_status,
                   "human_comment": human_comment, "root_cause_category": root_cause})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        _log.exception("eval label 저장 실패 sid=%s", result.get("session_id"))
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
    _audit("eval_label",
           changed_fields=[f"item={case.get('item_raw')}", f"bin={case.get('bin')}",
                           f"engine={case.get('status')}",
                           f"human={saved['human_status']}",
                           f"accepted={saved['accepted']}"])
    saved["ok"] = True
    return jsonify(saved)


@eval_panel_bp.get("/api/eval/scoring")
def api_eval_scoring():
    from admin_panel import eval_admin
    return jsonify(eval_admin.scoring())


# ── 트레이스 ─────────────────────────────────────────────────────────────────

@eval_panel_bp.get("/api/sessions")
def api_sessions():
    """트레이스 대상 후보 — web_report 세션만 (AI Comment 평가 경로가 이것뿐)."""
    from admin_panel import sessions_admin
    rows = sessions_admin.list_sessions(q=(request.args.get("q") or "").strip() or None,
                                        trashed="0", limit=200)
    items = [r for r in rows.get("rows") or [] if r.get("source") == "web_report"]
    return jsonify({"sessions": [
        {"session_id": r.get("session_id"), "file_name": r.get("file_name"),
         "product_type": r.get("product_type"), "product": r.get("product"),
         "lot_id": r.get("lot_id"), "created_at": r.get("created_at"),
         "mode": r.get("mode")}
        for r in items]})


def _summary_row(index, case):
    return {"idx": index, "source": case.get("source"),
            "item_raw": case.get("item_raw"), "bin": case.get("bin"),
            "item_class": case.get("item_class"), "status": case.get("status"),
            "value_type": case.get("value_type"),
            "primary_signature": case.get("primary_signature"),
            "fired_count": sum(1 for r in case["signature_matrix"] if r["fired"]),
            "stored": case.get("stored"),
            "cpk": (case.get("raw_metrics") or {}).get("cpk"),
            "yield": (case.get("raw_metrics") or {}).get("yield"),
            "n_dut": (case.get("features") or {}).get("n_dut")}


@eval_panel_bp.post("/api/trace")
def api_trace():
    body = request.get_json(force=True, silent=True) or {}
    session_id = str(body.get("session_id") or "").strip()
    if not _SESSION_ID_RE.match(session_id):
        return jsonify({"ok": False, "error": "session_id 형식 오류"}), 400
    # all=true 면 케이스 상한 없음(전체). 분포 원본값은 eval_debug 가 런 단위 예산으로 묶는다.
    max_cases = None if body.get("all") else _TRACE_DEFAULT_MAX_CASES
    if not _trace_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "다른 트레이스가 실행 중입니다"}), 409
    t0 = time.perf_counter()
    try:
        result = eval_debug.trace_session(
            session_id, report_db=report_db,
            upload_root=Path(config.REPORT_UPLOAD_DIR), max_cases=max_cases)
    except (KeyError, FileNotFoundError):
        return jsonify({"ok": False, "error": f"세션 없음: {session_id}"}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        _log.exception("eval trace 실패 sid=%s", session_id)
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
    finally:
        _trace_lock.release()

    token = f"{session_id}-{int(time.time())}"
    trace_store.put(token, result)
    return jsonify({
        "ok": True, "token": token, "session_id": session_id,
        "mode": result["mode"], "product_type": result["product_type"],
        "family_product": result["family_product"],
        "engine_version": result["engine_version"], "rules_rev": result["rules_rev"],
        "sources": result["sources"], "truncated": result["truncated"],
        "max_cases": result["max_cases"],
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "cases": [_summary_row(i, c) for i, c in enumerate(result["cases"])],
    })


@eval_panel_bp.get("/api/trace/<token>/case/<int:index>")
def api_trace_case(token, index):
    result = trace_store.get(token)
    if result is None:
        return jsonify({"ok": False, "error": "트레이스 결과가 만료됐습니다 — 다시 실행하세요"}), 404
    cases = result.get("cases") or []
    if index < 0 or index >= len(cases):
        return jsonify({"ok": False, "error": "케이스 번호 범위 밖"}), 404
    return jsonify({"ok": True, "case": cases[index]})

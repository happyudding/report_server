"""eval 패널 blueprint — 얇은 HTTP 핸들러. 구현은 rules_io / web_report.eval_debug.

접근 게이트: admin 과 같은 비밀번호로 발급하는 별도 쿠키(pe_admin_gate_eval).
변경요청(비-GET)은 admin_panel 과 같은 X-Admin-Request: 1 헤더를 요구한다.
"""
import hashlib
import hmac
import logging
import re
import sqlite3
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

from eval_panel import golden_io, review, rules_io, trace_store  # noqa: E402
from tools.eval_golden import golden_check            # noqa: E402  (CLI 와 같은 대조 로직)
from web_report import eval_debug                     # noqa: E402

_log = logging.getLogger(__name__)

eval_panel_bp = Blueprint("eval_panel", __name__)

_PANEL_HTML = Path(__file__).resolve().parent / "eval_panel.html"
_LOGIN_HTML = Path(__file__).resolve().parent / "eval_login.html"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# 트레이스는 콜드 세션이면 수 초 CPU — 관리자 전용이라 동시 1건으로 묶는다.
_trace_lock = threading.Lock()
_TRACE_DEFAULT_MAX_CASES = 400
# 룰 저장 직렬화 — rev 검사와 파일 쓰기 사이의 TOCTOU 를 봉합한다(저장은 수십 ms).
_rules_lock = threading.Lock()
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


def _rev_guard(body):
    """낙관적 잠금 — 화면이 들고 있던 rules_rev 와 현재 값이 다르면 저장을 막는다.

    base_rules_rev 미포함도 충돌로 본다: 이 패널의 클라이언트는 eval_panel.html 하나뿐이고
    HTML 과 라우트는 같이 배포되므로, 필드가 없는 요청 = 배포 전에 열어 둔 구버전 화면
    = 정의상 stale 이다. 미포함/불일치를 한 경로로 묶어 분기를 줄인다.
    """
    current = eval_debug.rules_rev()
    if str(body.get("base_rules_rev", "\x00")) != current:
        return jsonify({"ok": False, "conflict": True, "rules_rev": current,
                        "error": "룰이 다른 곳에서 변경됐거나 구버전 화면입니다 "
                                 "— 새로고침 후 다시 시도하세요"}), 409
    return None


def _reason(body):
    """변경 사유(선택 입력) — 감사 로그 changed_fields 에 붙일 항목 목록."""
    text = str(body.get("reason") or "").strip()[:200]
    return [f"reason={text}"] if text else []


# ── 메타 ─────────────────────────────────────────────────────────────────────

@eval_panel_bp.get("/api/meta")
def api_meta():
    files = eval_debug.rules_files()
    return jsonify({
        "taxonomy": eval_debug.taxonomy(),
        "threshold_keys": sorted(eval_debug.default_thresholds()),
        "default": eval_debug.default_thresholds(),
        "signature_ids": [s.get("id") for s in eval_debug.signatures_raw()],
        "subpop_gap_id": eval_debug.subpop_gap_id(),
        "rules_rev": eval_debug.rules_rev(),
        "eval_fail_only": eval_debug.fail_only_default(),
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
    with _rules_lock:
        conflict = _rev_guard(body)
        if conflict:
            return conflict
        try:
            result = rules_io.save_thresholds(pt, family, body.get("overrides") or {})
        except rules_io.RuleError as exc:
            _audit("eval_rules_edit",
                   changed_fields=[f"thresholds:{pt}/{family or '_default'}"] + _reason(body),
                   result="error")
            return _rule_error(exc)
    if not result.get("no_op"):
        _audit("eval_rules_edit",
               changed_fields=[f"thresholds:{pt}/{family or '_default'}",
                               f"keys={sorted(result['saved'])}",
                               f"rev={result['rules_rev']}"] + _reason(body))
    result["ok"] = True
    return jsonify(result)


# ── signatures ───────────────────────────────────────────────────────────────

def _scope_args(body=None):
    """제품군/family 스코프 — GET 은 쿼리스트링, 변경요청은 body 에서 받는다."""
    src = body if body is not None else request.args
    pt = str(src.get("pt") or "").strip()
    family = str(src.get("family") or "").strip() or None
    return pt, (family if pt else None)


@eval_panel_bp.get("/api/signatures")
def api_signatures():
    pt, family = _scope_args()
    try:
        return jsonify(rules_io.read_signatures(pt, family))
    except rules_io.RuleError as exc:
        return _rule_error(exc)


@eval_panel_bp.post("/api/signatures/enabled")
def api_signatures_bulk_enabled():
    """선택한 signature 여러 개를 한 번에 활성/비활성 (제품군 지정 시 그 범위만)."""
    body = request.get_json(force=True, silent=True) or {}
    enabled = bool(body.get("enabled"))
    pt, family = _scope_args(body)
    with _rules_lock:
        conflict = _rev_guard(body)
        if conflict:
            return conflict
        try:
            result = rules_io.set_signatures_enabled(body.get("ids") or [], enabled, pt, family)
        except rules_io.RuleError as exc:
            return _rule_error(exc)
    if result["changed"]:
        _audit("eval_rules_edit",
               changed_fields=[f"signatures_enabled={enabled}",
                               f"scope={pt or '기준값'}/{family or '_default'}",
                               f"ids={result['changed']}",
                               f"rev={result['rules_rev']}"] + _reason(body))
    result["ok"] = True
    return jsonify(result)


@eval_panel_bp.put("/api/signatures/<sig_id>")
def api_signature_save(sig_id):
    body = request.get_json(force=True, silent=True) or {}
    pt, family = _scope_args(body)
    with _rules_lock:
        conflict = _rev_guard(body)
        if conflict:
            return conflict
        try:
            result = rules_io.save_signature(sig_id, body, pt, family)
        except rules_io.RuleError as exc:
            _audit("eval_rules_edit", changed_fields=[f"signature:{sig_id}"] + _reason(body),
                   result="error")
            return _rule_error(exc)
    if not result.get("no_op"):
        _audit("eval_rules_edit",
               changed_fields=[f"signature:{sig_id}",
                               f"scope={pt or '기준값'}/{family or '_default'}",
                               f"fields={result['updated']}",
                               f"rev={result['rules_rev']}"] + _reason(body))
    result["ok"] = True
    return jsonify(result)


@eval_panel_bp.post("/api/signatures/<sig_id>/reset")
def api_signature_reset(sig_id):
    """이 제품군 전용 설정을 지우고 상속값으로 되돌린다."""
    body = request.get_json(force=True, silent=True) or {}
    pt, family = _scope_args(body)
    if not pt:
        return jsonify({"ok": False, "error": "제품군을 먼저 고르세요"}), 400
    with _rules_lock:
        conflict = _rev_guard(body)
        if conflict:
            return conflict
        try:
            result = rules_io.reset_signature(sig_id, pt, family)
        except rules_io.RuleError as exc:
            return _rule_error(exc)
    if result["removed"]:
        _audit("eval_rules_edit",
               changed_fields=[f"signature_reset:{sig_id}", f"scope={pt}/{family or '_default'}",
                               f"rev={result['rules_rev']}"] + _reason(body))
    result["ok"] = True
    return jsonify(result)


# ── 평가 제외 목록 ────────────────────────────────────────────────────────────

@eval_panel_bp.get("/api/exclusions")
def api_exclusions():
    return jsonify(rules_io.read_exclusions())


@eval_panel_bp.put("/api/exclusions")
def api_exclusions_save():
    body = request.get_json(force=True, silent=True) or {}
    with _rules_lock:
        conflict = _rev_guard(body)
        if conflict:
            return conflict
        try:
            result = rules_io.save_exclusions(body)
        except rules_io.RuleError as exc:
            _audit("eval_rules_edit", changed_fields=["exclusions"], result="error")
            return _rule_error(exc)
    if not result.get("no_op"):
        _audit("eval_rules_edit",
               changed_fields=["exclusions",
                               f"item_contains={result['saved']['item_contains']}",
                               f"units={result['saved']['units']}",
                               f"rev={result['rules_rev']}"] + _reason(body))
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


def _fired_set(case):
    return {r["id"] for r in (case.get("signature_matrix") or []) if r.get("fired")}


def _summary_row(index, case):
    return {"idx": index, "source": case.get("source"),
            "source_index": case.get("source_index"),
            "item_raw": case.get("item_raw"), "bin": case.get("bin"),
            "item_class": case.get("item_class"), "status": case.get("status"),
            "value_type": case.get("value_type"),
            "primary_signature": case.get("primary_signature"),
            "fired_count": sum(1 for r in case["signature_matrix"] if r["fired"]),
            "fired_ids": sorted(_fired_set(case)),
            "stored": case.get("stored"),
            "cpk": (case.get("raw_metrics") or {}).get("cpk"),
            "yield": (case.get("raw_metrics") or {}).get("yield"),
            "n_dut": (case.get("features") or {}).get("n_dut")}


def _case_key(case):
    return (case.get("source_index"), str(case.get("item_raw") or ""), case.get("bin"))


def _by_key(cases):
    """케이스 → {키: (idx, case)}. 같은 키가 둘 이상이면 **양쪽 다 버린다** —
    어느 쪽과 비교해야 하는지 알 수 없어 오보가 나느니 비교에서 빼는 편이 낫다."""
    out, dup = {}, set()
    for idx, case in enumerate(cases):
        key = _case_key(case)
        if key in out:
            dup.add(key)
        out[key] = (idx, case)
    for key in dup:
        out.pop(key, None)
    return out


def _snapshot(case):
    return {"status": case.get("status"), "primary": case.get("primary_signature"),
            "stored": bool(case.get("stored")), "fired": sorted(_fired_set(case))}


def _trace_diff(prev_cases, new_cases):
    """직전 run 대비 변화만 추린다 (룰 수정 전후 비교).

    changed = status/primary/stored/발화집합 중 하나라도 다른 케이스.
    added/removed = 케이스 집합 자체의 증감(항목 제외 규칙을 고친 경우 등).
    """
    prev, new = _by_key(prev_cases), _by_key(new_cases)
    changed, added, removed = [], [], []
    for key, (idx, case) in new.items():
        if key not in prev:
            added.append({"idx": idx, "source_index": key[0], "item_raw": key[1],
                          "bin": key[2], "status": case.get("status")})
            continue
        old, cur = _snapshot(prev[key][1]), _snapshot(case)
        if old == cur:
            continue
        old_fired, cur_fired = set(old["fired"]), set(cur["fired"])
        changed.append({"idx": idx, "source_index": key[0], "item_raw": key[1], "bin": key[2],
                        "old": old, "new": cur,
                        "fired_added": sorted(cur_fired - old_fired),
                        "fired_removed": sorted(old_fired - cur_fired)})
    for key, (_, case) in prev.items():
        if key not in new:
            removed.append({"source_index": key[0], "item_raw": key[1], "bin": key[2],
                            "old_status": case.get("status")})
    return {"changed": changed, "added": added, "removed": removed,
            "compared": len(new)}


@eval_panel_bp.post("/api/trace")
def api_trace():
    body = request.get_json(force=True, silent=True) or {}
    session_id = str(body.get("session_id") or "").strip()
    if not _SESSION_ID_RE.match(session_id):
        return jsonify({"ok": False, "error": "session_id 형식 오류"}), 400
    # all=true 면 케이스 상한 없음(전체). 분포 원본값은 eval_debug 가 런 단위 예산으로 묶는다.
    max_cases = None if body.get("all") else _TRACE_DEFAULT_MAX_CASES
    # scope: fail=fail item 만 / all=전체 item / 미지정=서버 기본(env)
    fail_only = {"fail": True, "all": False}.get(str(body.get("scope") or "").strip())
    if not _trace_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "다른 트레이스가 실행 중입니다"}), 409
    t0 = time.perf_counter()
    try:
        result = eval_debug.trace_session(
            session_id, report_db=report_db,
            upload_root=Path(config.REPORT_UPLOAD_DIR), max_cases=max_cases,
            fail_only=fail_only)
    except (KeyError, FileNotFoundError):
        return jsonify({"ok": False, "error": f"세션 없음: {session_id}"}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        _log.exception("eval trace 실패 sid=%s", session_id)
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
    finally:
        _trace_lock.release()

    # 직전 run 과의 diff — put() **전에** 조회해야 방금 만든 결과를 자기 자신과 비교하지
    # 않는다. 보관은 LRU 4런/30분이라 없을 수 있다(best-effort — 없으면 diff=None).
    diff = None
    prev = trace_store.latest_for_session(session_id)
    if prev is not None:
        prev_token, prev_result = prev
        if prev_result.get("max_cases") != result.get("max_cases"):
            diff = {"skipped": "직전 트레이스와 케이스 상한이 달라 비교를 생략했습니다 "
                               "(전체/기본 을 맞춰 다시 실행하세요)"}
        elif prev_result.get("fail_only") != result.get("fail_only"):
            # 모집단(평가 item 집합) 자체가 다르면 빠진 item 이 전부 removed 로 잡혀
            # 룰 회귀처럼 보인다 — 상한 불일치와 같은 이유로 비교를 건너뛴다.
            diff = {"skipped": "직전 트레이스와 평가 범위(fail/전체)가 달라 비교를 "
                               "생략했습니다 (범위를 맞춰 다시 실행하세요)"}
        else:
            diff = {"prev_token": prev_token, "prev_rules_rev": prev_result.get("rules_rev"),
                    **_trace_diff(prev_result.get("cases") or [], result["cases"])}

    token = f"{session_id}-{int(time.time())}"
    trace_store.put(token, result)
    return jsonify({
        "ok": True, "token": token, "session_id": session_id, "diff": diff,
        "mode": result["mode"], "product_type": result["product_type"],
        "family_product": result["family_product"],
        "engine_version": result["engine_version"], "rules_rev": result["rules_rev"],
        "sources": result["sources"], "truncated": result["truncated"],
        "max_cases": result["max_cases"],
        "fail_only": result["fail_only"], "item_scope": result["item_scope"],
        "coverage": result["coverage"],
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
    case = cases[index]
    # 이미 검수한 케이스면 라벨 폼을 그 값으로 채운다. 조회 실패가 상세 열람 자체를
    # 막으면 안 되므로 best-effort (eval DB 미생성·스키마 차이 등).
    label = None
    try:
        from web_report import eval_export
        session = report_db.get_session(result["session_id"])
        if session:
            label = eval_export.get_panel_label(dict(session),
                                                item=str(case.get("item_raw") or ""),
                                                bin_=case.get("bin"))
    except Exception:
        _log.warning("기존 라벨 조회 실패 sid=%s", result.get("session_id"), exc_info=True)
    return jsonify({"ok": True, "case": case, "label": label})


# ── 골든셋 (기대 발화 회귀 — tools/eval_golden) ───────────────────────────────

@eval_panel_bp.get("/api/golden")
def api_golden():
    doc = golden_io.read_golden()
    return jsonify({"ok": True, "path": str(golden_io.GOLDEN_FILE),
                    "sessions": doc["sessions"],
                    "total_expect": sum(len(s.get("expect") or [])
                                        for s in doc["sessions"])})


@eval_panel_bp.post("/api/golden/add")
def api_golden_add():
    """트레이스 케이스의 현재 발화 상태를 골든셋 기대값으로 기록."""
    body = request.get_json(force=True, silent=True) or {}
    result = trace_store.get(str(body.get("token") or ""))
    if result is None:
        return jsonify({"ok": False,
                        "error": "트레이스 결과가 만료됐습니다 — 다시 실행하세요"}), 404
    cases = result.get("cases") or []
    index = body.get("index")
    if not isinstance(index, int) or index < 0 or index >= len(cases):
        return jsonify({"ok": False, "error": "케이스 번호 범위 밖"}), 400
    case = cases[index]
    session = report_db.get_session(result["session_id"]) or {}
    note = " · ".join(x for x in [
        f"{result.get('product_type') or '-'}/{result.get('family_product') or '-'}",
        str(session.get("product") or ""), str(session.get("lot_id") or "")] if x)
    try:
        saved = golden_io.add_case(result["session_id"], note, case)
    except rules_io.RuleError as exc:
        return _rule_error(exc)
    _audit("eval_golden",
           changed_fields=[f"add:{result['session_id']}", f"item={case.get('item_raw')}",
                           f"bin={case.get('bin')}",
                           f"fire={saved['entry'].get('fire') or []}",
                           f"replaced={saved['replaced']}"])
    saved["ok"] = True
    return jsonify(saved)


# ── Eval 표본함 (룰별 소수 표본 검수 → 승인형 임계값 추천) ────────────────────

@eval_panel_bp.get("/api/review/queue")
def api_review_queue():
    """활성 룰별 미검수 표본(룰당 최대 8건) + 무판정 목록 + 검수 진행."""
    try:
        return jsonify({"ok": True, **review.queue(
            (request.args.get("product_type") or "").strip() or None,
            (request.args.get("family_product") or "").strip() or None)})
    except review.ReviewError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        _log.exception("표본함 조회 실패")
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@eval_panel_bp.post("/api/review/label")
def api_review_label():
    """검수 1건 저장 — 맞음/과다발화. "맞음" 은 골든셋에도 굳힌다.

    골든셋 자동 등록이 핵심이다: 임계값 강화는 recall 을 반드시 떨어뜨리는데, 그걸 막는
    가드가 골든 회귀뿐인데 골든셋이 비어 있으면 항상 통과해 무효가 된다. 사람이 "맞다" 고
    한 발화가 곧 "유지돼야 한다" 이므로 그대로 기대값이 된다.
    """
    body = request.get_json(force=True, silent=True) or {}
    eval_id = body.get("eval_id")
    if not isinstance(eval_id, int):
        return jsonify({"ok": False, "error": "eval_id(정수) 필요"}), 400
    if not isinstance(body.get("correct"), bool):
        return jsonify({"ok": False, "error": "correct(true=맞음 / false=과다발화) 필요"}), 400
    correct = bool(body["correct"])
    try:
        saved = review.save_review_label(
            eval_id, correct=correct, comment=str(body.get("comment") or ""),
            reviewer=str(request.headers.get("X-Admin-User") or "eval-panel"))
    except review.ReviewError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    saved["golden"] = None
    if correct:
        # 골든 등록 실패가 검수 저장을 되돌리면 안 된다 — 라벨은 이미 저장됐다.
        try:
            entry = review.golden_entry_for(eval_id)
            if entry:
                saved["golden"] = golden_io.add_case(
                    entry["session_id"], "표본 검수에서 '맞음' 으로 확정", entry["case"])
        except Exception:
            _log.warning("골든셋 자동 등록 실패 (eval_id=%s)", eval_id, exc_info=True)
    _audit("eval_review_label",
           changed_fields=[f"eval_id={eval_id}", f"correct={correct}",
                           f"golden={bool(saved['golden'])}"])
    saved["ok"] = True
    return jsonify(saved)


@eval_panel_bp.post("/api/review/collect-session")
def api_review_collect():
    """지정 세션의 평가 스냅샷 수집 — 기존 세션은 자동 백필하지 않으므로 여기서 채운다.

    큐에 올리고 바로 돌려준다(수집은 콜드 tables 로드를 포함해 수 초). eval_export 의
    단일 소비자 큐를 그대로 쓰므로 코멘트 export 와 같은 DB 파일에 순차로 쓴다.
    """
    body = request.get_json(force=True, silent=True) or {}
    session_id = str(body.get("session_id") or "").strip()
    if not _SESSION_ID_RE.match(session_id):
        return jsonify({"ok": False, "error": "session_id 형식 오류"}), 400
    if report_db.get_session(session_id) is None:
        return jsonify({"ok": False, "error": f"세션 없음: {session_id}"}), 404
    from web_report import eval_export
    if body.get("wait"):          # 소수 세션을 손으로 채울 때 결과를 바로 보고 싶은 경우
        result = eval_export.safe_collect_snapshot(
            session_id, report_db=report_db,
            upload_root=Path(config.REPORT_UPLOAD_DIR), force=bool(body.get("force")))
    else:
        eval_export.collect_async(session_id, report_db=report_db,
                                  upload_root=Path(config.REPORT_UPLOAD_DIR))
        result = {"queued": True}
    _audit("eval_review_collect", changed_fields=[f"session={session_id}", repr(result)[:200]])
    return jsonify({"ok": True, "session_id": session_id, **result})


# ── ENGR 확정 Signature (Issue Table) ────────────────────────────────────────

@eval_panel_bp.get("/api/engr-signatures")
def api_engr_signatures():
    """ENGR 이 Issue Table 에서 확정한 정답 signature 목록.

    ``?only=UNKNOWN`` 이면 "기존 룰로 설명이 안 된다"고 지목된 케이스만 — 새 불량유형을
    정의할 때 볼 재료다. 세션은 label→evaluation→ingest_run 으로 역참조한다(case_id 에는
    세션이 없다).
    """
    only = str(request.args.get("only") or "").strip().upper()
    limit = max(1, min(500, int(request.args.get("limit") or 200)))
    from web_report import eval_export
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return jsonify({"ok": True, "rows": [], "note": "eval DB 없음"})
    try:
        sql = """SELECT ls.signature, ls.rank, l.created_at, l.label_id,
                        fc.bin, fc.lot_id, fc.product_name,
                        im.item_name_raw AS item, pm.family_product,
                        ir.session_id, ir.source_file
                 FROM label_signature ls
                 JOIN label l ON l.label_id = ls.label_id
                 JOIN fail_case fc ON fc.case_id = l.case_id
                 LEFT JOIN item_master im ON im.item_id = fc.item_id
                 LEFT JOIN product_master pm ON pm.product_name = fc.product_name
                 LEFT JOIN evaluation ev ON ev.eval_id = l.eval_id
                 LEFT JOIN ingest_run ir ON ir.run_id = ev.run_id
                 WHERE l.labeler = ?"""
        params = [eval_export._SIGNATURE_LABELER]
        if only:
            sql += " AND ls.signature = ?"
            params.append(only)
        sql += " ORDER BY l.label_id DESC, ls.rank LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError as exc:      # v7 이전 DB (label_signature 없음)
        return jsonify({"ok": True, "rows": [], "note": f"스키마 미갱신: {exc}"})
    finally:
        conn.close()
    return jsonify({"ok": True, "rows": rows, "count": len(rows)})


@eval_panel_bp.post("/api/engr-signatures/resync")
def api_engr_signatures_resync():
    """지정 세션의 확정 signature 를 편집 DB → eval DB 로 재동기화 (멱등).

    편집 DB 가 진실이므로 언제 돌려도 안전하다 — 동기화가 실패했거나 서버를 내렸다
    올린 뒤 복구 수단.
    """
    body = request.get_json(force=True, silent=True) or {}
    session_id = str(body.get("session_id") or "").strip()
    if not _SESSION_ID_RE.match(session_id):
        return jsonify({"ok": False, "error": "session_id 형식 오류"}), 400
    if report_db.get_session(session_id) is None:
        return jsonify({"ok": False, "error": f"세션 없음: {session_id}"}), 404
    from web_report import eval_export
    result = eval_export.safe_sync_signatures(
        session_id, report_db=report_db, upload_root=Path(config.REPORT_UPLOAD_DIR))
    _audit("eval_signature_resync",
           changed_fields=[f"session={session_id}", repr(result)[:200]])
    return jsonify({"ok": True, "session_id": session_id, **result})


@eval_panel_bp.post("/api/review/proposal")
def api_review_proposal():
    """검수 라벨 기반 임계값 **강화안** + 전후 영향도. 계산만 하고 적용하지 않는다."""
    body = request.get_json(force=True, silent=True) or {}
    signature = str(body.get("signature") or "").strip()
    if not signature:
        return jsonify({"ok": False, "error": "signature 필요"}), 400
    try:
        result = review.proposal(
            signature,
            str(body.get("product_type") or "").strip() or None,
            str(body.get("family_product") or "").strip() or None)
    except review.ReviewError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    # 골든셋이 비어 있으면 "회귀 실패 시 차단" 가드가 무효라 적용을 막는다.
    if not result.get("blocked"):
        total = sum(len(s.get("expect") or []) for s in golden_io.read_golden()["sessions"])
        result["golden_expect"] = total
        if total == 0:
            result["apply_blocked"] = ("골든셋이 비어 있어 적용을 막습니다 — 회귀 가드가 "
                                       "항상 통과해 무의미해집니다. 표본을 '맞음' 으로 "
                                       "검수하면 자동으로 쌓입니다.")
    return jsonify({"ok": True, **result})


@eval_panel_bp.post("/api/golden/check")
def api_golden_check():
    """골든셋 회귀 실행 — 세션마다 트레이스 1회라 CPU 를 쓴다(동기 실행).

    현재 골든 세션은 손으로 늘리는 소수 건이라 요청 안에서 끝낸다. 10건 이상/1분 이상이
    되면 trace_store 처럼 토큰 폴링으로 바꿀 것. 그동안 수동 트레이스는 409 를 받는다.
    """
    body = request.get_json(force=True, silent=True) or {}
    only = str(body.get("session_id") or "").strip()
    entries = golden_io.read_golden()["sessions"]
    if only:
        entries = [e for e in entries if str(e.get("session_id") or "") == only]
    if not entries:
        return jsonify({"ok": True, "sessions": [], "total_checked": 0, "total_findings": 0,
                        "rules_rev": eval_debug.rules_rev(),
                        "note": "골든셋이 비어 있습니다 — 트레이스 케이스에서 "
                                "'골든셋에 추가' 로 채우세요"})
    if not _trace_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "다른 트레이스가 실행 중입니다"}), 409
    t0 = time.perf_counter()
    rows, total_checked, total_findings = [], 0, 0
    try:
        for entry in entries:
            sid = str(entry.get("session_id") or "")
            row = {"session_id": sid, "note": entry.get("note"), "checked": 0, "findings": []}
            try:
                findings, checked = golden_check.check_session(entry)
            except Exception as exc:                       # 세션 삭제·parquet 유실 등
                row["error"] = f"{type(exc).__name__}: {exc}"
                total_findings += 1
                rows.append(row)
                continue
            row["checked"], row["findings"] = checked, findings
            total_checked += checked
            total_findings += len(findings)
            rows.append(row)
    finally:
        _trace_lock.release()
    return jsonify({"ok": True, "sessions": rows, "total_checked": total_checked,
                    "total_findings": total_findings,
                    "rules_rev": eval_debug.rules_rev(),
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000)})

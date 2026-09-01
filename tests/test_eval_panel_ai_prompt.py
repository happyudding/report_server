# -*- coding: utf-8 -*-
"""/pe/eval "AI 지시문" 탭 — 지시문·금지 문구 저장 계층 + 라우트.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_eval_panel_ai_prompt.py

AI Comment [제안] 이 "사례를 버리는 문장"을 쓰지 못하게 하는 조건은 앞으로도 계속
늘어난다. 그래서 코드가 아니라 관리자 화면에서 관리하는데, 그 저장 경로가 깨지면
증상은 "저장했는데 안 바뀐다"로만 나타난다(에러가 아니다).

검증 항목:
  (a) read/save round-trip + 파일이 실제로 엔진 rules 디렉토리에 쓰이는지
  (b) 검증 — 잘못된 id / 빈 문장 / 깨진 정규식 / 개수 상한 → RuleError
  (c) no_op — 같은 내용 재저장은 rev 를 올리지 않는다(올리면 저장된 [제안] 이 헛되이 폐기)
  (d) rev 증가 + 백업 파일 생성
  (e) 라우트 — 401(쿠키 없음) / 403(X-Admin-Request 없음) / 409(rev 충돌) / 400 / 200
  (f) 엔진 반영 — 저장한 지시문이 `_rules.ai_prompt_instructions()` 로 즉시 읽힌다
      (mtime 캐시라 재기동 불필요)

⚠ **격리**: `EVAL_RULES_DIR` 를 임시 폴더로 돌려 운영 룰 파일과 `.rules_rev` 를 건드리지
않는다. 실제 rules 디렉토리에 쓰면 개발 PC 의 모든 ai 세션 캐시가 무효화된다.
pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="eval_panel_aip_"))

# ── 격리 (import 보다 먼저) ──────────────────────────────────────────────────
_RULES_DIR = _TMP / "rules"
_RULES_DIR.mkdir(parents=True, exist_ok=True)
shutil.copy2(_ROOT / "eval_analyzer" / "eval_engine" / "rules" / "ai_prompt.yaml",
             _RULES_DIR / "ai_prompt.yaml")
os.environ["EVAL_RULES_DIR"] = str(_RULES_DIR)
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ.setdefault("REPORT_ADMIN_SECRET", "pte")

sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "server"))

from flask import Flask                                              # noqa: E402

import config                                                        # noqa: E402
from admin_panel import GATE_COOKIE_EVAL, eval_gate_token            # noqa: E402
from database import report_db                                       # noqa: E402
from eval_panel import URL_PREFIX, register_eval_panel, rules_io     # noqa: E402
from web_report import eval_debug                                    # noqa: E402

app = Flask(__name__)
register_eval_panel(app)          # url_prefix 는 여기서 붙는다(blueprint 자체엔 없다)
report_db.init_report_db()
client = app.test_client()

AIP_PATH = _RULES_DIR / "ai_prompt.yaml"
_MUT = {"X-Admin-Request": "1", "Content-Type": "application/json"}


def _login():
    client.set_cookie(GATE_COOKIE_EVAL, eval_gate_token(), path=URL_PREFIX)


def _logout():
    client.delete_cookie(GATE_COOKIE_EVAL, path=URL_PREFIX)


def _payload(**over):
    body = {"instructions": [{"id": "keep_precedents", "enabled": True,
                              "text": "사례를 버리지 마라."}],
            "deny_patterns": [{"id": "precedent_denial", "enabled": True,
                               "only_with_precedents": True,
                               "regex": "사례가 없", "note": "메모"}]}
    body.update(over)
    return body


def _expect_error(payload, hint):
    try:
        rules_io.save_ai_prompt(payload)
    except rules_io.RuleError:
        return
    raise AssertionError(f"검증이 통과시켜서는 안 됨: {hint}")


# ── (a) round-trip ───────────────────────────────────────────────────────────

def test_roundtrip():
    assert eval_debug.rules_dir() == _RULES_DIR, \
        f"격리 실패 — 운영 rules 를 가리킨다: {eval_debug.rules_dir()}"
    before = rules_io.read_ai_prompt()
    assert before["instructions"] and before["deny_patterns"], before   # 배포 기본값
    assert any(r["id"] == "precedent_denial" for r in before["deny_patterns"])

    out = rules_io.save_ai_prompt(_payload())
    assert out["no_op"] is False and out["ok"] if "ok" in out else True
    got = rules_io.read_ai_prompt()
    assert got["instructions"] == [{"id": "keep_precedents", "enabled": True,
                                    "text": "사례를 버리지 마라."}], got
    assert got["deny_patterns"][0]["regex"] == "사례가 없"
    assert got["deny_patterns"][0]["only_with_precedents"] is True
    # 파일이 실제로 그 자리에 있고 머리말 주석이 보존된다
    text = AIP_PATH.read_text(encoding="utf-8")
    assert text.startswith("#") and "instructions:" in text
    print("  (a) read/save round-trip + 파일 위치·머리말 보존 OK")


# ── (b) 검증 ─────────────────────────────────────────────────────────────────

def test_validation():
    _expect_error({"instructions": "x"}, "instructions 가 배열이 아님")
    _expect_error(_payload(instructions=[{"id": "BAD ID", "enabled": True, "text": "x"}]),
                  "대문자·공백 id")
    _expect_error(_payload(instructions=[{"id": "a", "text": "x"},
                                         {"id": "a", "text": "y"}]), "id 중복")
    _expect_error(_payload(instructions=[{"id": "a", "text": "   "}]), "빈 지시문")
    _expect_error(_payload(instructions=[{"id": "a", "text": "x" * 501}]), "길이 상한")
    _expect_error(_payload(instructions=[{"id": f"i{i}", "text": "x"}
                                         for i in range(31)]), "개수 상한")
    _expect_error(_payload(deny_patterns=[{"id": "p", "regex": "(["}]), "깨진 정규식")
    _expect_error(_payload(deny_patterns=[{"id": "p", "regex": ""}]), "빈 정규식")
    _expect_error(_payload(deny_patterns=[{"id": "p", "regex": "x", "note": "n" * 301}]),
                  "메모 길이")
    # 저장은 안 됐어야 한다(검증 실패가 파일을 건드리면 안 된다)
    assert rules_io.read_ai_prompt()["instructions"][0]["id"] == "keep_precedents"
    print("  (b) 검증 9종(id/중복/빈값/길이/개수/정규식) OK")


# ── (c)(d) no_op · rev · 백업 ────────────────────────────────────────────────

def test_no_op_and_rev():
    rev0 = eval_debug.rules_rev()
    same = rules_io.save_ai_prompt(_payload())
    assert same["no_op"] is True and same["backup"] is None, same
    assert eval_debug.rules_rev() == rev0, "no_op 인데 rev 가 올랐다 — [제안] 이 헛되이 폐기된다"

    changed = rules_io.save_ai_prompt(
        _payload(instructions=[{"id": "keep_precedents", "enabled": False,
                                "text": "사례를 버리지 마라."}]))
    assert changed["no_op"] is False
    assert eval_debug.rules_rev() != rev0, "내용이 바뀌었는데 rev 가 그대로 — 캐시가 안 갈린다"
    assert changed["backup"], "백업 파일명이 없다"
    assert (_RULES_DIR / rules_io.BACKUP_DIRNAME / changed["backup"]).is_file()
    # enabled:false 는 보존되고, 엔진에는 나가지 않아야 한다
    assert rules_io.read_ai_prompt()["instructions"][0]["enabled"] is False
    print("  (c,d) no_op rev 불변 · 변경 시 rev+1 · 백업 생성 OK")


# ── (f) 엔진 즉시 반영 ───────────────────────────────────────────────────────

def test_engine_sees_saved():
    rules_io.save_ai_prompt(_payload(instructions=[
        {"id": "keep_precedents", "enabled": True, "text": "사례를 버리지 마라."},
        {"id": "off_one", "enabled": False, "text": "꺼진 문장"},
    ]))
    eval_debug._eval_path()
    from eval_engine.pipeline import _rules
    got = _rules.ai_prompt_instructions()          # mtime 캐시 — 재기동 없이 반영
    assert got == ["사례를 버리지 마라."], got
    # 서버 쪽 창구도 같은 파일을 본다
    rules = eval_debug.ai_prompt_rules()
    assert [r["id"] for r in rules["instructions"]] == ["keep_precedents", "off_one"]
    print("  (f) 저장 → 엔진/서버 즉시 반영(mtime 캐시) OK")


# ── (e) 라우트 ───────────────────────────────────────────────────────────────

def test_routes():
    _logout()
    assert client.get(URL_PREFIX + "/api/ai_prompt").status_code == 401
    _login()
    r = client.get(URL_PREFIX + "/api/ai_prompt")
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert "instructions" in body and "rules_rev" in body

    # X-Admin-Request 없음 → 403 (CSRF 가드)
    assert client.put(URL_PREFIX + "/api/ai_prompt", json=_payload()).status_code == 403

    # rev 불일치 → 409 (낙관적 잠금)
    r = client.put(URL_PREFIX + "/api/ai_prompt", headers=_MUT,
                   data=json.dumps(dict(_payload(), base_rules_rev="999999")))
    assert r.status_code == 409 and r.get_json()["conflict"] is True, r.get_json()

    # 검증 실패 → 400 (메시지 그대로)
    r = client.put(URL_PREFIX + "/api/ai_prompt", headers=_MUT, data=json.dumps(
        dict(_payload(deny_patterns=[{"id": "p", "regex": "(["}]),
             base_rules_rev=body["rules_rev"])))
    assert r.status_code == 400 and "정규식" in r.get_json()["error"], r.get_json()

    # 정상 저장 → 200 + rev 증가 + 감사 로그
    r = client.put(URL_PREFIX + "/api/ai_prompt", headers=_MUT, data=json.dumps(dict(
        _payload(instructions=[{"id": "keep_precedents", "enabled": True,
                                "text": "사례를 버리지 마라. 반드시."}]),
        base_rules_rev=body["rules_rev"], reason="테스트")))
    assert r.status_code == 200, (r.status_code, r.get_json())
    saved = r.get_json()
    assert saved["ok"] is True and saved["no_op"] is False
    assert saved["rules_rev"] != body["rules_rev"]
    logs = report_db.get_audit_logs(action="eval_rules_edit")
    fields = " ".join(str(x["changed_fields"]) for x in logs)
    assert "ai_prompt" in fields and "keep_precedents" in fields, fields
    assert "테스트" in fields, "변경 사유가 감사에 안 남았다"
    print("  (e) 라우트 401/403/409/400/200 + 감사 OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    assert config.REPORT_ADMIN_PASSWORD, "REPORT_ADMIN_PASSWORD 가 비어 있다"
    test_roundtrip()
    test_validation()
    test_no_op_and_rev()
    test_engine_sees_saved()
    test_routes()
    print("test_eval_panel_ai_prompt: 전부 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

"""챗봇 top-level 이름 충돌 방어 검증.

실행:
    python tests/test_chatbot_module_collision.py

운영 챗봇 엔진은 `server/chatbot` 이고, 라우트는 `from chatbot import agent` 로 가져온다.
그런데 `chatbot` 은 흔한 이름이라 같은 이름의 다른 top-level 패키지가 `sys.path` 앞에 있거나
`sys.modules` 를 선점하면 **answer_web 이 없는 다른 모듈**이 잡힌다. 2026-08-10 운영에서
`AttributeError: module 'chatbot.agent' has no attribute 'answer_web'` 로 실제 터졌다
(당시 범인은 `eval_analyzer/chatbot` — 이후 `chatbot_prototype` 으로 개명했다).

개명으로 그 특정 범인은 사라졌지만 **방어는 남긴다**: 이름이 흔해서 같은 일이 또 생길 수 있고
방어 비용이 0 이다. 그래서 이 테스트는 eval_analyzer 에 의존하지 않고 **가짜 충돌 패키지를
직접 만들어** 선점시킨다 — 어떤 폴더가 범인이든 같은 상황을 재현한다.

pytest 미사용 — `sys.modules` 를 오염시키므로 반드시 **단독 프로세스**로 돌려야 한다.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

_TMP = Path(tempfile.mkdtemp(prefix="chatbot_collision_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["REPORT_ADMIN_PASSWORD"] = "collision-test"

import config  # noqa: E402

config.REPORT_ADMIN_SECRET = "pte"
config.REPORT_ADMIN_PASSWORD = "collision-test"

_SERVER_CHATBOT = Path(_ROOT, "server", "chatbot")


def poison():
    """`chatbot` 이름을 가로채는 가짜 패키지를 만들어 sys.modules 를 선점한다."""
    fake_root = _TMP / "fake_pkg_root"
    pkg = fake_root / "chatbot"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('"""이름을 가로채는 가짜 chatbot."""\n', encoding="utf-8")
    # answer_web 이 **없는** agent — 운영에서 잡혔던 모듈과 같은 성격
    (pkg / "agent.py").write_text(
        '"""answer_web 이 없는 agent (충돌 재현용)."""\n\n'
        "def answer(*a, **k):\n"
        "    raise AssertionError('가짜 모듈이 호출됐다 — 방어가 뚫렸다')\n",
        encoding="utf-8")

    sys.path.insert(0, str(fake_root))
    import chatbot

    assert Path(chatbot.__file__).parent == pkg, chatbot.__file__
    from chatbot import agent as wrong

    assert not hasattr(wrong, "answer_web"), "재현 전제 붕괴 — 이 모듈엔 answer_web 이 없어야 한다"
    print("[OK] 재현 — `chatbot` 이 가짜 패키지에 선점됨 (answer_web 없음)")


def test_route_resolves_correct_engine():
    from report import routes_chat

    module = routes_chat._agent()
    assert Path(module.__file__).resolve().parent == _SERVER_CHATBOT, module.__file__
    assert hasattr(module, "answer_web")
    print("[OK] routes_chat 이 server/chatbot/agent.py 를 적재 (선점 무시)")


def test_chat_answers(client):
    """충돌 상태에서도 라우트가 200 을 돌려주는가 (하위 모듈 상대 import 까지 성립하는가)."""
    c, csrf = client
    r = c.post("/pe/report/api/chat", headers={"X-CSRF-Token": csrf},
               json={"question": "S3222 보고서 찾아줘"})
    assert r.status_code == 200, (r.status_code, r.get_json())
    assert r.get_json()["plan"]["intent"] == "session_find", r.get_json()["plan"]

    # planner·tools_eval 등 하위 모듈이 별칭 패키지 안에서도 정상 import 되는지
    r2 = c.post("/pe/report/api/chat", headers={"X-CSRF-Token": csrf},
                json={"question": "SGM 항목 이력 알려줘"})
    assert r2.status_code == 200, r2.get_json()
    assert r2.get_json()["plan"]["intent"] == "item_history", r2.get_json()["plan"]
    print("[OK] 충돌 상태에서 챗 응답 200 + 하위 모듈 상대 import 정상")


def _client():
    import admin_panel
    from database import report_db
    from flask import Flask
    from plugin import register_report_server

    app = Flask(__name__)
    register_report_server(app)
    report_db.init_report_db()
    c = app.test_client()
    c.set_cookie("pe_master_gate", admin_panel.issue_master_value(), domain="localhost")
    c.get("/pe/report/api/history?limit=5&offset=0")
    csrf = None
    for key, val in getattr(c, "_cookies", {}).items():
        if "report_csrf" in str(key):
            csrf = val.value if hasattr(val, "value") else val
    assert csrf, "CSRF 쿠키를 얻지 못했다"
    return c, csrf


def main():
    poison()
    test_route_resolves_correct_engine()
    test_chat_answers(_client())
    print("\n전부 통과")


if __name__ == "__main__":
    main()

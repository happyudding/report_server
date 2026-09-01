# -*- coding: utf-8 -*-
"""AI Comment "제안 제외" 옵션 — 클라 위젯 배선 ↔ 서버 소비의 **짝**을 고정한다.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_ai_no_suggest_option.py

이 옵션은 클라(체크박스 → options 키) → 서버(리더 → 프롬프트 생략 + 셀 텍스트)로
**여러 파일을 건너 흐른다**. 한 곳만 빠져도 에러가 아니라 "체크했는데 그대로 [제안]이
나온다" 또는 "LLM 을 안 부르는데 워커가 계속 폴링한다" 로 나타난다(둘 다 조용한 실패).
honey_main.py 는 PyQt6 메인 윈도우라 단위 테스트로 띄울 수 없어 **소스 텍스트로** 배선을
검사한다 — 위젯 렌더가 아니라 "키가 실리는가/조건이 맞는가" 가 이 파일의 관심사다.

검증:
  (a) 서버 리더 `webreport_ai_no_suggest` — 키 없음/타입 이상/깨진 JSON 전부 안전한 False
  (b) 클라 옵션 조립 — 기본값이면 **키를 싣지 않는다**(캐시 키 바이트 불변 규약) +
      제안 제외면 `ai_model` 을 싣지 않는다(워커 헛폴링 방지)
  (c) 클라 위젯 배선 — 체크박스 생성·레이아웃 등록·표시 토글·설정 영속
  (d) 서버 소비 — 프롬프트 생략(LLM 미호출) + [제안] 섹션 제거
  (e) ⚙ 아이콘이 좌측 툴바 Options 톱니바퀴와 **같은 방식**(_emoji_icon)으로 그려진다

pytest 미사용 (tests/ 관례 — 자체 실행 + assert). PyQt6·서버 불필요(소스 검사 + 순수 함수).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_MAIN = (_ROOT / "client" / "honey_main.py").read_text(encoding="utf-8")


# ── (a) 서버 리더 ────────────────────────────────────────────────────────────

def test_reader():
    from web_report.validation import webreport_ai_no_suggest as f
    assert f(json.dumps({"ai_no_suggest": True})) is True
    # 키가 없으면 False — 기존 세션의 옵션 원문이 그대로여야 캐시 키가 안 갈린다
    assert f(json.dumps({"ai_comment": True, "ai_comment_optin": True})) is False
    assert f(json.dumps({"ai_no_suggest": False})) is False
    assert f("") is False and f("{broken") is False and f(json.dumps([1, 2])) is False
    print("  (a) 서버 리더(키 없음·깨진 JSON → False) OK")


# ── (b) 클라 옵션 조립 ───────────────────────────────────────────────────────

def test_client_options_wiring():
    # 기본값(체크 안 함)일 때 키를 싣지 않는다 — 조건부 대입이어야 한다
    assert 'options["ai_no_suggest"] = True' in _MAIN, \
        "제안 제외 옵션 키를 업로드에 싣는 코드가 없습니다"
    assert re.search(r"if no_suggest:\s*\n\s*options\[\"ai_no_suggest\"\] = True", _MAIN), \
        "ai_no_suggest 를 무조건 싣고 있습니다 — 기본값 세션의 캐시 키가 갈립니다"
    # 제안 제외면 ai_model 을 싣지 않는다(elif) — 실으면 워커가 뜨고 헛폴링한다
    assert re.search(r"if no_suggest:.*?elif ai_on and str\(self\.cbo_ai_model", _MAIN, re.S), \
        "제안 제외인데 ai_model 이 함께 실립니다 — 클라 워커가 헛되이 폴링합니다"
    print("  (b) 클라 옵션 조립(조건부 키·ai_model 배제) OK")


# ── (c) 클라 위젯 배선 ───────────────────────────────────────────────────────

def test_client_widget_wiring():
    """체크박스는 **AI Comment 민감도 설정창** 안에 있다 (2026-09-02 사용자 요청으로 이동).

    메인 창(honey_main)은 위젯을 갖지 않고 설정값 `ai_no_suggest` 만 읽는다 — 그래야
    창을 닫아도 값이 하나뿐이고, 두 곳에 체크박스가 생겨 서로 어긋나지 않는다.
    저장 키는 옮기기 전과 같아야 한다(바꾸면 이미 켜 둔 PC 의 설정이 조용히 풀린다).
    """
    dlg = (_ROOT / "client" / "honey_ui" / "dialogs.py").read_text(encoding="utf-8")
    assert "self.chk_no_suggest = QCheckBox(" in dlg, \
        "'제안 제외' 체크박스가 민감도 설정창에 없습니다"
    assert "root.addWidget(self.chk_no_suggest)" in dlg, \
        "체크박스가 레이아웃에 안 붙어 화면에 안 보입니다"
    assert 'self.chk_no_suggest.setChecked(bool(app_settings.get_setting("ai_no_suggest")))' in dlg, \
        "창을 열 때 저장된 설정을 안 읽습니다"
    # 저장은 OK 시점에만 — 취소하면 되돌아가야 한다.
    assert re.search(r'def _on_ok\(self\):[\s\S]*?'
                     r'app_settings\.set_setting\("ai_no_suggest"', dlg), \
        "OK 시점에 설정이 저장되지 않습니다"
    # 메인 창은 설정값만 읽는다(위젯 없음).
    assert "chk_ai_no_suggest" not in _MAIN, \
        "메인 창에 옛 체크박스가 남아 있습니다 — 설정이 두 곳으로 갈립니다"
    assert 'def _ai_no_suggest(self)' in _MAIN and \
        'app_settings.get_setting("ai_no_suggest")' in _MAIN, \
        "메인 창이 저장된 설정을 안 읽습니다"
    # 제안 제외면 LLM 위젯(신호등·AI Model)을 숨기고 연결 확인도 하지 않는다
    assert "_sync_ai_suggest_widgets" in _MAIN
    assert re.search(r"if on and not self\._ai_no_suggest\(\):\s*\n\s*"
                     r"self\._check_ai_health\(\)", _MAIN), \
        "제안 제외인데 Claude 연결 확인을 돕니다(LLM 을 안 쓰는 세션입니다)"
    # 민감도 창을 닫은 뒤 표시 상태가 따라와야 한다(그 창 안에서 껐다 켤 수 있으므로).
    assert re.search(r"EvalSensitivityDialog\(self\)\.exec\(\):[\s\S]{0,400}?"
                     r"self\._sync_ai_suggest_widgets\(\)", _MAIN), \
        "민감도 창을 닫아도 신호등·AI Model 표시가 갱신되지 않습니다"
    print("  (c) 클라 위젯(민감도 창 이동·영속·표시토글) OK")


# ── (d) 서버 소비 ────────────────────────────────────────────────────────────

def test_server_consumption():
    from web_report import ai_comment as A
    case = {"status": "MAJOR", "signatures": [], "comment":
            "[현상] - LOW_CPK: 현상\n[사례] ①(P1/L1) 사례 원문 \n [제안] - LOW_CPK: 조치"}
    # [제안] 은 **토큰까지** 사라지고 사례는 남는다(빈 라벨이 뜨면 "만들다 만" 것으로 보인다)
    out = A._cell_text(case, no_suggest=True)
    assert "[제안]" not in out and "조치" not in out, out
    assert "[사례] ①(P1/L1) 사례 원문" in out and out.startswith("[MAJOR] [현상]"), out
    # 프롬프트 생략 배선 — no_suggest 면 build_prompts 를 부르지 않는다
    src = (_ROOT / "web_report" / "ai_comment.py").read_text(encoding="utf-8")
    assert re.search(r'out\["prompts"\] = \(\{\} if no_suggest', src), \
        "제안 제외인데 프롬프트를 만듭니다 — 클라가 LLM 을 호출해 토큰을 씁니다"
    # 사례 목록(payload 건수·상세)은 **그대로 나가야** 한다 — 그게 이 모드의 유일한 내용이다
    assert 'out["precedent_counts"]' in src and 'out["precedents"]' in src
    print("  (d) 서버 소비([제안] 제거·프롬프트 생략·사례 유지) OK")


# ── (e) ⚙ 아이콘 통일 ────────────────────────────────────────────────────────

def test_gear_icon_matches_options():
    """AI Comment 옆 ⚙ 가 좌측 툴바 Options 톱니바퀴와 같은 방식으로 그려진다.

    종전에는 `QPushButton("⚙️")` 텍스트라 폰트에 따라 툴바 아이콘과 다른 모양·크기로
    보였다(2026-09-02 사용자 요청). 둘 다 `_emoji_icon` 픽스맵을 쓰면 같은 그림이 된다.
    """
    assert 'self.btn_ai_sens.setIcon(self._emoji_icon("⚙️"' in _MAIN, \
        "⚙ 버튼이 _emoji_icon 을 쓰지 않습니다 — 툴바 Options 와 모양이 갈립니다"
    assert 'self.btn_ai_sens = QPushButton("")' in _MAIN, \
        "아이콘과 텍스트 이모지가 함께 그려집니다(중복)"
    # 툴바 Options 쪽도 같은 이모지·같은 함수를 쓰는지 확인(한쪽만 바뀌는 드리프트 방지)
    assert '("⚙️", "Options"' in _MAIN, "툴바 Options 아이콘이 바뀌었습니다"
    assert "def _emoji_icon" in _MAIN
    print("  (e) ⚙ 아이콘 = 툴바 Options 와 같은 _emoji_icon OK")


def test_cache_key_separation():
    """(f) "제안 제외" 세션이 **남의 평가 캐시를 보지 않는다** — 조용한 오답 차단.

    `ai_comment_key` 는 dedup 이익을 위해 session_id 를 일부러 뺀다(perf_guard S10).
    이 옵션은 `webreport_options` 에만 있고 analysis_key·content_hash 에는 안 들어가므로,
    꼬리표가 없으면 같은 rawdata 를 제안 제외로 올린 세션이 **먼저 올라간 세션의 [제안]
    문장을 그대로** 보게 된다(`_eval_sensitivity_suffix` 가 막는 것과 같은 부류).
    """
    from web_report import cache_policy as C
    opts_on = json.dumps({"ai_comment": True, "ai_comment_optin": True})
    base = {"analysis_key": "AK", "content_hash": "C" * 64, "webreport_options": opts_on}
    nosug = dict(base, webreport_options=json.dumps(
        {"ai_comment": True, "ai_comment_optin": True, "ai_no_suggest": True}))
    assert C.ai_comment_key(base) != C.ai_comment_key(nosug), \
        "제안 제외 세션이 일반 세션과 같은 AI 캐시 키를 씁니다 — 남의 [제안] 을 봅니다"
    assert "nosugg" in C.ai_comment_key(nosug)
    # 옵션이 없는 기존 세션의 키는 **바이트 그대로** — 붙으면 전 세션 콜드 재빌드가 된다
    assert "nosugg" not in C.ai_comment_key(base)
    plain = {"analysis_key": "AK", "content_hash": "C" * 64, "webreport_options": ""}
    assert "nosugg" not in C.ai_comment_key(plain)
    # report_key 는 옵션 원문을 통째로 물고 있어 이미 갈린다(추가 작업 불필요 확인)
    assert C.report_key(base, "S1", 0) != C.report_key(nosug, "S1", 0)
    print("  (f) 캐시 키 분리(제안 제외 ↔ 일반) + 기존 세션 키 불변 OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    test_reader()
    test_client_options_wiring()
    test_client_widget_wiring()
    test_server_consumption()
    test_gear_icon_matches_options()
    test_cache_key_separation()
    print("test_ai_no_suggest_option: 전부 통과")


if __name__ == "__main__":
    main()

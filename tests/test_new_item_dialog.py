# -*- coding: utf-8 -*-
"""신규 Item(수식) 추가 UI — 수식 에디터 커밋 규칙 + 허브 페이지 배선 (2026-08-24).

⚠ pytest 로 돌리지 말고 **단독 실행**할 것 (PyQt6 offscreen 필요):
    python tests/test_new_item_dialog.py

수식 입력은 이 기능에서 유일하게 "모호할 수 있는" 부분이다. 커밋 규칙이 흔들리면
  · item 이름 안의 `-` 가 뺄셈 연산자로 커밋돼 엉뚱한 수식이 되고,
  · 한글 항목을 검색하다 Enter 를 누르면 조합 확정과 동시에 잘못된 토큰이 들어가며,
  · 함수 버튼이 여는 괄호를 같이 넣지 않으면 사용자가 문법 오류를 만들 수 있다.
전부 조용히 잘못된 수식으로 이어지므로 규칙을 기계로 고정한다.

검사 대상 (client/honey_ui/formula_editor.py · rawdata_hub_dialog.py):
- 빈 입력에서 연산자 키 → 토큰 / 글자가 있을 때 같은 키 → 검색어 문자
- `>` + `=` → `>=` 단일 토큰으로 합침 (`<`+`>` → `<>`)
- 빈 입력 Backspace → 마지막 토큰 pop
- Enter → 후보 선택 / 숫자 / 거절 3분기, 자유 텍스트 item 금지
- `@` → 후보 전체 펼침 (검색어에 `@` 가 들어가지 않는다)
- **IME 조합 중에는 키를 가로채지 않는다**
- 함수 버튼이 fn + lp 를 함께 넣는다
- 칩 클릭으로 그 토큰만 삭제
- 허브: 페이지가 Options 바로 다음이고, 미리보기 전에는 [원본에 추가] 가 잠겨 있다
"""
import os
import sys
from pathlib import Path


def key_event(text="", key=None):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QKeyEvent

    if key is None:
        key = Qt.Key.Key_A
    return QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text)


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "client"))
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    app = QApplication([])  # noqa: F841 — 위젯 생성에 필요

    from honey_ui.formula_editor import FormulaEditor

    def fresh(items=("VDD_A", "VDD-B", "전압_한글")):
        editor = FormulaEditor()
        editor.set_items(list(items))
        return editor

    def kinds(editor):
        return [t.get("v") if t.get("t") in ("op", "fn") else
                (t.get("item") if t.get("t") == "item" else
                 (t.get("v") if t.get("t") == "num" else t.get("t")))
                for t in editor.tokens()]

    # ── 빈 입력 연산자 키 → 토큰 ─────────────────────────────────────────────
    ed = fresh()
    for ch in "+-*/(),":
        ed.input.setText("")
        ed._on_key(key_event(ch))
    assert kinds(ed) == ["+", "-", "*", "/", "lp", "rp", "comma"], kinds(ed)
    print("  [ok] 빈 입력 + 연산자·괄호·쉼표 키 -> 토큰 7종")

    # ── 글자가 있을 때 같은 키는 검색어 문자 (item 명의 '-' 보호) ───────────
    ed = fresh()
    ed.input.setText("VDD")
    handled = ed._on_key(key_event("-"))
    assert handled is False, "글자가 있는데 '-' 를 연산자로 먹었다"
    assert ed.tokens() == [], ed.tokens()
    print("  [ok] 글자가 있을 때 '-' 는 검색어 문자 (VDD-B 같은 이름 보호)")

    # ── 2글자 비교 연산자 합침 ───────────────────────────────────────────────
    for first, second, want in ((">", "=", ">="), ("<", "=", "<="), ("<", ">", "<>")):
        ed = fresh()
        ed.input.setText("")
        ed._on_key(key_event(first))
        ed.input.setText("")
        ed._on_key(key_event(second))
        assert kinds(ed) == [want], f"{first}{second}: {kinds(ed)}"
    print("  [ok] '>'+'=' -> '>=' · '<'+'=' -> '<=' · '<'+'>' -> '<>' (단일 토큰)")

    # ── 빈 입력 Backspace = pop ──────────────────────────────────────────────
    ed = fresh()
    ed.input.setText("")
    ed._on_key(key_event("+"))
    ed._on_key(key_event("*"))
    assert len(ed.tokens()) == 2
    ed.input.setText("")
    ed._on_key(key_event("", Qt.Key.Key_Backspace))
    assert kinds(ed) == ["+"], kinds(ed)
    ed.input.setText("abc")
    handled = ed._on_key(key_event("", Qt.Key.Key_Backspace))
    assert handled is False and kinds(ed) == ["+"], "글자가 있는데 토큰을 지웠다"
    print("  [ok] 빈 입력 Backspace = 마지막 토큰 pop / 글자가 있으면 글자 삭제")

    # ── @ 는 후보를 펼치고 검색어에 들어가지 않는다 ──────────────────────────
    ed = fresh()
    ed.input.setText("")
    handled = ed._on_key(key_event("@"))
    assert handled is True and ed._suggest_open(), "@ 가 후보를 펼치지 않았다"
    assert ed.suggest.count() == 3, ed.suggest.count()
    assert ed.tokens() == [], "@ 가 토큰이 됐다"
    print(f"  [ok] '@' -> 후보 {ed.suggest.count()}개 펼침 (토큰·검색어 아님)")

    # ── Enter 3분기 ──────────────────────────────────────────────────────────
    ed = fresh()
    ed.input.setText("")
    ed._on_key(key_event("@"))
    ed._update_suggest("한글")
    assert ed.suggest.count() == 1, ed.suggest.count()
    ed._on_key(key_event("", Qt.Key.Key_Return))
    assert kinds(ed) == ["전압_한글"], kinds(ed)
    assert ed.input.text() == "" and not ed._suggest_open()

    ed.input.setText("3.5")
    ed._on_key(key_event("", Qt.Key.Key_Return))
    assert kinds(ed) == ["전압_한글", 3.5], kinds(ed)

    ed.input.setText("없는항목이름")
    ed._on_key(key_event("", Qt.Key.Key_Return))
    assert kinds(ed) == ["전압_한글", 3.5], "자유 텍스트가 item 토큰이 됐다"
    print("  [ok] Enter 3분기 - 후보=item / 숫자=num / 그 외 거절(자유 텍스트 금지)")

    # ── IME 조합 중에는 키를 가로채지 않는다 ─────────────────────────────────
    ed = fresh()
    ed.input.setText("")
    ed._on_key(key_event("+"))
    ed.input._composing = True
    before = ed.tokens()
    ed.input.keyPressEvent(key_event("", Qt.Key.Key_Return))
    ed.input.keyPressEvent(key_event("", Qt.Key.Key_Backspace))
    assert ed.tokens() == before, "조합 중에 토큰이 바뀌었다"
    ed.input._composing = False
    print("  [ok] IME 조합 중 Enter·Backspace 가 토큰을 건드리지 않는다")

    # ── 함수 버튼은 fn + lp 를 함께 넣는다 ───────────────────────────────────
    ed = fresh()
    ed._push_func("IF")
    assert [t["t"] for t in ed.tokens()] == ["fn", "lp"], ed.tokens()
    print("  [ok] 함수 버튼 -> fn + lp 동시 삽입 ('fn 뒤 괄호 없음' 상태 원천 차단)")

    # ── 칩 클릭 = 그 토큰만 삭제 ─────────────────────────────────────────────
    ed = fresh()
    ed._push({"t": "item", "item": "VDD_A"})
    ed._push({"t": "op", "v": "+"})
    ed._push({"t": "num", "v": 2.0})
    ed._on_chip_clicked("tok:1")
    assert kinds(ed) == ["VDD_A", 2.0], kinds(ed)
    print("  [ok] 칩 클릭 -> 그 인덱스 토큰만 삭제")

    # ── validate: 정상/오류 index ────────────────────────────────────────────
    ed = fresh()
    for tok in ({"t": "item", "item": "VDD_A"}, {"t": "op", "v": "+"}):
        ed._push(tok)
    tokens, error, index = ed.validate()
    assert tokens is None and error and index is not None, (tokens, error, index)
    ed._push({"t": "num", "v": 1.0})
    tokens, error, index = ed.validate()
    assert tokens and not error, (tokens, error)
    print("  [ok] validate - 말미 연산자 거부(문제 토큰 index 포함) / 완성 수식 통과")

    # ── 허브 페이지 배선 ─────────────────────────────────────────────────────
    import honey_ui.rawdata_hub_dialog as hub_mod

    hub_mod.RawdataHubDialog._load = lambda self: None      # 서버 조회 차단

    # 모달은 offscreen 에서도 블록한다 — 기록형 가짜로 바꾼다
    # (test_source_group_dropdown.py 와 같은 관례).
    class FakeMsgBox:
        StandardButton = hub_mod.QMessageBox.StandardButton
        shown = []

        @staticmethod
        def information(parent, title, text, *a, **kw):
            FakeMsgBox.shown.append(("info", title, text))

        @staticmethod
        def warning(parent, title, text, *a, **kw):
            FakeMsgBox.shown.append(("warn", title, text))

        @staticmethod
        def question(parent, title, text, *a, **kw):
            FakeMsgBox.shown.append(("question", title, text))
            return FakeMsgBox.StandardButton.Yes

    hub_mod.QMessageBox = FakeMsgBox
    hub = hub_mod.RawdataHubDialog(None, "sid_test", "http://127.0.0.1:1")
    titles = [b.text() for b in hub.nav_buttons]
    assert titles[:3] == ["현재 상태", "Options", "신규 Item(수식) 추가"], titles
    assert hub.pages.count() == len(titles), (hub.pages.count(), len(titles))
    assert hub.btn_add_item.isEnabled() is False, "미리보기 전에 [원본에 추가] 가 열려 있다"
    assert hub.ni_expr.isEnabled() is False, "rawdata 로드 전에 수식 칸이 열려 있다"
    assert hub.width() >= 1000, hub.width()
    print(f"  [ok] 허브 - 탭 순서 {titles[:3]} · 창 폭 {hub.width()} · 버튼 초기 잠금")

    # 미리보기를 통과하지 않으면 add_item_spec 이 만들어지지 않는다
    hub._start_add_item()
    assert hub.add_item_spec is None and hub.action == "", (hub.add_item_spec, hub.action)
    assert FakeMsgBox.shown and "미리보기" in FakeMsgBox.shown[-1][2], FakeMsgBox.shown
    print("  [ok] 허브 - 미리보기 없이 [원본에 추가] 를 눌러도 넘어가지 않는다")

    # 미리보기를 통과했더라도 수식이 비면 넘어가지 않는다 (방어 2중)
    hub._ni_previewed = True
    hub._start_add_item()
    assert hub.add_item_spec is None and hub.action == "", (hub.add_item_spec, hub.action)
    print("  [ok] 허브 - 빈 수식으로는 넘어가지 않는다")

    print("[통과] 수식 에디터 커밋 규칙 + 허브 페이지 배선 정상")


if __name__ == "__main__":
    main()

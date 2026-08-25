# -*- coding: utf-8 -*-
"""신규 Item(수식) 추가 UI — 줄글 입력 해독 + 허브 페이지 배선 (2026-08-25 개편).

⚠ pytest 로 돌리지 말고 **단독 실행**할 것 (PyQt6 offscreen 필요):
    python tests/test_new_item_dialog.py

수식 입력은 이 기능에서 유일하게 "모호할 수 있는" 부분이다. 규칙이 흔들리면
  · item 이름 안의 `-` 가 뺄셈으로 읽혀 엉뚱한 수식이 되고,
  · 목록에 없는 글자가 항목으로 통과해 **보지 않은 값이 원본 parquet 에 영구히 박히며**,
  · 한글 항목을 검색하다 Enter 를 누르면 조합 확정과 동시에 엉뚱한 항목이 들어간다.
전부 조용히 잘못된 수식으로 이어지므로 규칙을 기계로 고정한다.

검사 대상 (client/honey_ui/formula_editor.py · rawdata_hub_dialog.py):
- 항목은 `@"이름"` 인용으로만 들어온다 — 자유 텍스트는 항목이 될 수 없다
- `@` → 후보 펼침 / 이어 타이핑 → 필터 / Enter → `@"정식이름"` 삽입 (`"` 는 `""` escape)
- 이름을 고치면 그 순간 항목이 아니게 되어 오류 + 빨간 밑줄
- 함수명·항목 조회는 대소문자 무관, 이름에 `- ( ) 공백 "` 이 들어 있어도 안전
- 쓸 수 없는 함수·문자·`!=` 거부, 파서 오류의 토큰 index → 문자 위치 매핑
- **줄바꿈이 문서에 들어가지 않는다** (오프셋 계산이 블록 1개 전제)
- IME 조합 중에는 Enter 가 후보를 확정하지 않는다
- 허브: 페이지가 Options 바로 다음이고, 미리보기 전에는 [원본에 추가] 가 잠겨 있다
"""
import os
import sys
from pathlib import Path

ITEMS = ["VDD_A", "VDD_B", "VDD-VSS", "IDD (1.8V)", 'A"B', "SUM", "전압_한글"]


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

    from PyQt6.QtCore import Qt, QMimeData
    from PyQt6.QtGui import QTextCursor
    from PyQt6.QtWidgets import QApplication

    app = QApplication([])  # noqa: F841 — 위젯 생성에 필요

    from honey_ui.formula_editor import FormulaEditor

    def fresh(items=ITEMS):
        editor = FormulaEditor()
        editor.set_items(list(items))
        return editor

    def kinds(editor):
        return [t.get("v") if t.get("t") in ("op", "fn") else
                (t.get("item") if t.get("t") == "item" else
                 (t.get("v") if t.get("t") == "num" else t.get("t")))
                for t in editor.tokens()]

    # ── 줄글을 그대로 해독한다 (함수명 대소문자 무관) ────────────────────────
    ed = fresh()
    ed.set_text('if(@"VDD_A" > min(@"VDD_B", 2), 0, 1)')
    tokens, error, index = ed.validate()
    assert tokens and not error, (error, index)
    assert kinds(ed) == ["IF", "lp", "VDD_A", ">", "MIN", "lp", "VDD_B", "comma",
                         2.0, "rp", "comma", 0.0, "comma", 1.0, "rp"], kinds(ed)
    print("  [ok] 소문자 if/min 을 그대로 해독 -> 토큰 15개")

    # ── 항목 조회도 대소문자 무관 (단, 토큰에는 목록의 원본 이름이 들어간다) ─
    ed = fresh()
    ed.set_text('@"vdd_a" + 1')
    tokens, error, _ = ed.validate()
    assert tokens and not error, error
    assert tokens[0]["item"] == "VDD_A", tokens[0]
    print("  [ok] @\"vdd_a\" -> 목록의 원본 이름 'VDD_A' 로 토큰화 (소문자화 금지)")

    # ── 자유 텍스트는 항목이 될 수 없다 ──────────────────────────────────────
    ed = fresh()
    ed.set_text("VDD_A + 1")
    tokens, error, _ = ed.validate()
    assert tokens is None and "쓸 수 없는 함수" in error, (tokens, error)
    print("  [ok] 인용 없는 'VDD_A' 는 항목이 아니다 (자유 텍스트 item 금지)")

    # ── @ → 후보 펼침 / 필터 / Enter 로 `@"이름"` 삽입 ───────────────────────
    ed = fresh()
    ed.input.insertPlainText("@")
    assert ed._suggest_open() and ed.suggest.count() == len(ITEMS), ed.suggest.count()
    assert ed.tokens() == [], "@ 가 토큰이 됐다"
    ed.input.insertPlainText("vdd_")
    rows = [ed.suggest.item(i).text() for i in range(ed.suggest.count())]
    assert rows == ["VDD_A", "VDD_B"], rows
    ed.input.keyPressEvent(key_event("", Qt.Key.Key_Return))
    assert ed.text() == '@"VDD_A"', repr(ed.text())
    assert not ed._suggest_open(), "확정 후에도 후보가 열려 있다"
    print("  [ok] '@' -> 후보 전체 / 이어 타이핑 -> 필터 / Enter -> @\"VDD_A\" 삽입")

    # ── 이름 안의 `"` 는 `""` 로 escape 되어 왕복한다 ────────────────────────
    ed = fresh()
    ed._insert_item('A"B')
    assert ed.text() == '@"A""B"', repr(ed.text())
    assert kinds(ed) == ['A"B'], kinds(ed)
    print('  [ok] 이름 안의 " 는 "" 로 escape 되어 그대로 왕복한다')

    # ── 이름에 연산자·공백·괄호가 있어도 안전 ────────────────────────────────
    ed = fresh()
    ed.set_text('@"VDD-VSS" * 2 + @"IDD (1.8V)"')
    tokens, error, _ = ed.validate()
    assert tokens and not error, error
    assert kinds(ed) == ["VDD-VSS", "*", 2.0, "+", "IDD (1.8V)"], kinds(ed)
    print("  [ok] 'VDD-VSS' 의 '-' 와 'IDD (1.8V)' 의 괄호·공백이 연산자로 새지 않는다")

    # ── 함수명과 같은 이름의 항목도 구분된다 ────────────────────────────────
    ed = fresh()
    ed.set_text('SUM(@"SUM", 1)')
    tokens, error, _ = ed.validate()
    assert tokens and not error, error
    assert kinds(ed) == ["SUM", "lp", "SUM", "comma", 1.0, "rp"], kinds(ed)
    assert tokens[0]["t"] == "fn" and tokens[2]["t"] == "item"
    print("  [ok] SUM(...) 는 함수 / @\"SUM\" 은 항목 — 이름 충돌 없음")

    # ── 이름을 고치면 그 순간 항목이 아니게 된다 (+ 빨간 밑줄) ───────────────
    ed = fresh()
    ed.set_text('@"VDD_" + 1')
    tokens, error, _ = ed.validate()
    assert tokens is None and "항목이 없습니다" in error, (tokens, error)
    assert "VDD_A" in error, "근접 후보를 알려주지 않는다: " + error
    assert ed._error_span == (0, 7), ed._error_span
    assert ed.marks(), "오류 밑줄 마크가 없다"
    print("  [ok] 이름 훼손 -> '항목이 없습니다 (혹시 VDD_A?)' + 그 자리 span")

    # ── 쓸 수 없는 함수·문자·별칭 거부 ──────────────────────────────────────
    for text, needle in (('vlookup(@"VDD_A")', "쓸 수 없는 함수"),
                         ('@"VDD_A" ※ 1', "쓸 수 없는 문자"),
                         ('@"VDD_A" != 1', "<> 로 씁니다"),
                         ('@"VDD_A" == 1', "= 하나로 씁니다"),
                         ('@VDD_A', '@"항목명"'),
                         ('@"VDD_A', '닫는 " 가 없습니다')):
        ed = fresh()
        ed.set_text(text)
        tokens, error, _ = ed.validate()
        assert tokens is None and needle in error, (text, tokens, error)
    print("  [ok] vlookup/※/!=/==/@맨이름/닫는따옴표누락 6종 거부 + 안내 문구")

    # ── 파서 오류: 토큰 index 가 문자 위치로 매핑된다 ────────────────────────
    ed = fresh()
    ed.set_text('IF(@"VDD_A" > MIN(@"VDD_B", 1), 0, 1')
    tokens, error, _ = ed.validate()
    assert tokens is None and "닫는 괄호" in error, (tokens, error)
    start, end = ed._error_span
    assert 0 <= start < end <= len(ed.text()), ed._error_span
    print(f"  [ok] 닫는 괄호 누락 -> 문자 위치 {ed._error_span} 로 매핑 (위치 없는 오류 없음)")

    # ── 빈 입력 계약 ────────────────────────────────────────────────────────
    ed = fresh()
    assert ed.validate() == (None, "", None), ed.validate()
    print("  [ok] 빈 수식 -> (None, '', None)")

    # ── 줄바꿈은 문서에 들어가지 않는다 ─────────────────────────────────────
    ed = fresh()
    ed.set_text('@"VDD_A"')
    ed.input.moveCursor(QTextCursor.MoveOperation.End)
    ed.input.keyPressEvent(key_event("\r", Qt.Key.Key_Return))
    mime = QMimeData()
    mime.setText("+\n1")
    ed.input.insertFromMimeData(mime)
    assert "\n" not in ed.text() and "\r" not in ed.text(), repr(ed.text())
    assert ed.text() == '@"VDD_A"+ 1', repr(ed.text())
    print("  [ok] Enter·붙여넣기 어느 쪽으로도 줄바꿈이 들어가지 않는다")

    # ── IME 조합 중에는 Enter 가 후보를 확정하지 않는다 ─────────────────────
    ed = fresh()
    ed.input.insertPlainText("@")
    assert ed._suggest_open()
    ed.input._composing = True
    before = ed.text()
    ed.input.keyPressEvent(key_event("", Qt.Key.Key_Return))
    assert ed.text() == before, ("조합 중에 항목이 확정됐다", ed.text())
    ed.input._composing = False
    print("  [ok] IME 조합 중 Enter 는 후보를 확정하지도, 줄바꿈을 넣지도 않는다")

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

    # 수식을 고치면 미리보기 통과 상태가 즉시 풀린다 (보지 않은 값 방지)
    hub.ni_expr.set_items(ITEMS)
    hub._ni_previewed = True
    hub.btn_add_item.setEnabled(True)
    hub.ni_expr.set_text('@"VDD_A" + 1')
    assert hub._ni_previewed is False and hub.btn_add_item.isEnabled() is False
    print("  [ok] 허브 - 수식 변경 즉시 미리보기 무효화 + 버튼 재잠금")

    print("[통과] 줄글 수식 해독 + 허브 페이지 배선 정상")


if __name__ == "__main__":
    main()

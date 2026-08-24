"""FormulaEditor — 항목·연산자·함수를 칩으로 조립하는 수식 입력 위젯 (2026-08-24).

Rawdata 허브의 `신규 Item(수식) 추가` 페이지가 쓴다. 웹의 Gap Chart 수식 편집기와 같은
설계를 Qt 로 옮긴 것이다.

**수식은 평문이 아니라 토큰 배열이다.** item 이름에는 공백·`( )`·`+ - * /`·따옴표가 전부
합법이라(honeyform 은 중복·메타명 충돌만 검사한다) 사용자가 친 글자를 매 입력마다 다시
렉싱하는 방식은 원리적으로 불가능하다. 그래서 위쪽 칩 스트립은 **읽기 전용**이고 입력은
평범한 QLineEdit 하나다 — 한글 IME 조합·캐럿 복원·붙여넣기 살균 문제가 구조적으로 없다.

커밋 규칙을 모호성 0 으로 둔다:
  · 입력창이 **비어 있을 때** `+ - * / ( ) , < > =` 키  → 그 연산자 토큰
  · 입력창에 **글자가 있을 때** 같은 키                 → 그냥 검색어 문자 (item 명의 `-` 보호)
  · 빈 입력 Backspace                                   → 마지막 토큰 pop
  · Enter → ① 하이라이트된 후보 = item 토큰 ② 숫자로 파싱되면 num 토큰 ③ 아니면 거절
  · `@`  → 후보 목록을 전체로 펼친다(글자가 있을 때는 그냥 검색어 문자)
  · 연산자·함수는 버튼으로도 항상 커밋 가능 (키 규칙을 몰라도 된다)

2글자 비교 연산자(`>=` `<=` `<>`)는 **타이머 없이** 합친다 — 마지막 토큰이 `>`/`<` 인
상태에서 다음 키가 `=`(또는 `<` 뒤 `>`)이면 그건 언제나 2글자 연산자 의도다. 그 자리에
피연산자가 아닌 `=` 가 올 이유가 없다. 시간 창을 두면 "빨리 치면 되고 천천히 치면 안 되는"
설명 불가능한 동작이 된다.

**자유 텍스트 item 은 만들 수 없다** — 반드시 후보 목록에서 고른 것만 item 토큰이 된다.
v1 은 끝에만 삽입한다(중간 편집은 칩을 눌러 지우고 다시 넣는다).
"""
from __future__ import annotations

from html import escape

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from web_report import formula

# 칩 색 — 웹 Gap Chart(.gc-tok-*)와 같은 팔레트. 항목은 파란 기울임이 사용자 요청 서식이다.
_COLOR = {
    "item": "#1D4ED8",
    "num": "#111827",
    "op": "#6B7280",
    "cmp": "#0F766E",
    "fn": "#7C3AED",
    "paren": "#374151",
}

# 입력창이 비어 있을 때 그 자리에서 토큰이 되는 키.
_OP_KEYS = {"+": "+", "-": "-", "*": "*", "/": "/",
            ">": ">", "<": "<", "=": "="}
_PAREN_KEYS = {"(": "lp", ")": "rp", ",": "comma"}

# `>`/`<` 뒤에 이 키가 오면 2글자 연산자로 합친다.
_JOIN = {(">", "="): ">=", ("<", "="): "<=", ("<", ">"): "<>"}

_SUGGEST_MAX = 30


class _ImeLineEdit(QLineEdit):
    """한글 조합 중에는 키를 가로채지 않는 입력창.

    조합 중(preedit 이 남아 있는 동안) Enter 는 "토큰 커밋"이 아니라 "조합 확정"이다.
    이 가드가 없으면 한글 항목을 검색하다 Enter 를 누를 때 조합이 확정되면서 동시에 엉뚱한
    토큰이 들어간다. Backspace·↑↓ 도 같은 이유로 조합 중에는 Qt 에 맡긴다.
    """

    def __init__(self, on_key, parent=None):
        super().__init__(parent)
        self._composing = False
        self._on_key = on_key

    def inputMethodEvent(self, event):
        self._composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if self._composing or not self._on_key(event):
            super().keyPressEvent(event)


class FormulaEditor(QWidget):
    """수식 토큰 편집기. `tokens()` 가 정본, `text()` 는 표시 전용이다."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tokens = []
        self._items = []
        self._suggest_rows = []

        self.chips = QLabel()
        self.chips.setTextFormat(Qt.TextFormat.RichText)
        self.chips.setWordWrap(True)
        self.chips.setOpenExternalLinks(False)
        self.chips.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.chips.setStyleSheet("padding: 6px 8px;")
        self.chips.linkActivated.connect(self._on_chip_clicked)
        chip_area = QScrollArea()
        chip_area.setWidget(self.chips)
        chip_area.setWidgetResizable(True)
        chip_area.setFixedHeight(64)
        chip_area.setStyleSheet("QScrollArea { background: #ffffff; border: 1px solid #d4d4d8;"
                                " border-radius: 5px; }")

        self.input = _ImeLineEdit(self._on_key)
        self.input.setPlaceholderText("@ 항목 검색 또는 숫자 입력...  (Enter 로 확정)")
        self.input.textEdited.connect(self._on_text_edited)

        self.suggest = QListWidget()
        self.suggest.setFixedHeight(132)
        self.suggest.setVisible(False)
        self.suggest.itemActivated.connect(self._on_suggest_activated)
        self.suggest.itemClicked.connect(self._on_suggest_activated)

        ops = QHBoxLayout()
        ops.setSpacing(4)
        for key in ("+", "-", "*", "/"):
            ops.addWidget(self._op_button(formula._ARITH_TEXT[key], key))
        for text, kind in (("(", "lp"), (")", "rp"), (",", "comma")):
            ops.addWidget(self._plain_button(text, lambda k=kind: self._push({"t": k})))
        ops.addSpacing(12)
        for key in (">", ">=", "<", "<=", "=", "<>"):
            ops.addWidget(self._op_button(formula._CMP_TEXT[key], key))
        ops.addStretch(1)
        ops.addWidget(self._plain_button("지우기", self._pop))
        ops.addWidget(self._plain_button("전체 지우기", self.clear))

        funcs = QHBoxLayout()
        funcs.setSpacing(4)
        for name in ("IF", "MIN", "MAX", "SUM", "AVERAGE", "ABS", "ROUND", "SQRT",
                     "AND", "OR", "NOT"):
            funcs.addWidget(self._plain_button(name, lambda n=name: self._push_func(n)))
        funcs.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(chip_area)
        layout.addWidget(self.input)
        layout.addWidget(self.suggest)
        layout.addLayout(ops)
        layout.addLayout(funcs)
        self._render_chips()

    # ── 공개 API ─────────────────────────────────────────────────────────────

    def set_items(self, names):
        """자동완성 후보 (전 source item 이름 합집합)."""
        self._items = [str(n) for n in names or []]

    def tokens(self) -> list:
        return [dict(t) for t in self._tokens]

    def text(self) -> str:
        """표시용 평문. **재파싱하지 않는다.**"""
        return formula.render_formula(self._tokens) if self._tokens else ""

    def clear(self):
        self._tokens = []
        self.input.clear()
        self._hide_suggest()
        self._render_chips()
        self.changed.emit()

    def validate(self):
        """(tokens|None, 에러 메시지, 문제 토큰 index). 비어 있으면 (None, "", None)."""
        if not self._tokens:
            return None, "", None
        try:
            return formula.normalize_tokens(self.tokens()), "", None
        except formula.FormulaError as exc:
            return None, str(exc), exc.index

    # ── 칩 렌더 ──────────────────────────────────────────────────────────────

    def _chip_html(self, index, tok, bad):
        kind = tok.get("t")
        text = escape(formula.token_text(tok))
        if kind == "item":
            style = f"color:{_COLOR['item']};font-style:italic;font-weight:600"
        elif kind == "num":
            style = f"color:{_COLOR['num']}"
        elif kind == "fn":
            style = f"color:{_COLOR['fn']};font-weight:700"
        elif kind == "op":
            key = "cmp" if tok.get("v") in formula._CMP else "op"
            style = f"color:{_COLOR[key]};font-weight:700"
        else:
            style = f"color:{_COLOR['paren']};font-weight:700"
        if bad:
            style += ";background:#fee2e2"
        return (f'<a href="tok:{index}" style="text-decoration:none">'
                f'<span style="{style}">{text}</span></a>')

    def _render_chips(self, bad_index=None):
        if not self._tokens:
            self.chips.setText('<span style="color:#9aa1ab">'
                               '항목·숫자·연산자를 넣어 수식을 만드세요 '
                               '(칩을 누르면 지워집니다)</span>')
            return
        parts = []
        prev = None
        for i, tok in enumerate(self._tokens):
            kind = tok.get("t")
            tight = (prev is not None
                     and (kind in ("rp", "comma") or prev == "lp"
                          or (kind == "lp" and prev == "fn")))
            if prev is not None and not tight:
                parts.append("&nbsp;")
            parts.append(self._chip_html(i, tok, bad_index == i))
            prev = kind
        self.chips.setText("".join(parts))

    def mark_error(self, index):
        self._render_chips(bad_index=index)

    def _on_chip_clicked(self, href):
        if not href.startswith("tok:"):
            return
        try:
            index = int(href[4:])
        except ValueError:
            return
        if 0 <= index < len(self._tokens):
            del self._tokens[index]
            self._render_chips()
            self.changed.emit()

    # ── 토큰 조작 ────────────────────────────────────────────────────────────

    def _push(self, token):
        self._tokens.append(token)
        self._render_chips()
        self.changed.emit()

    def _push_func(self, name):
        # 함수 버튼은 fn + lp 를 **함께** 넣는다 — "fn 뒤에 여는 괄호 없음" 상태를
        # UI 로는 만들 수 없게 해서 그 문법 오류를 원천 차단한다.
        self._tokens.append({"t": "fn", "v": name})
        self._tokens.append({"t": "lp"})
        self._render_chips()
        self.changed.emit()

    def _push_op(self, op):
        # `>` 뒤 `=` 처럼 2글자 연산자가 되는 조합이면 마지막 토큰을 합친다.
        last = self._tokens[-1] if self._tokens else None
        if last and last.get("t") == "op":
            joined = _JOIN.get((last.get("v"), op))
            if joined:
                last["v"] = joined
                self._render_chips()
                self.changed.emit()
                return
        self._push({"t": "op", "v": op})

    def _pop(self):
        if self._tokens:
            self._tokens.pop()
            self._render_chips()
            self.changed.emit()

    def _op_button(self, label, op):
        return self._plain_button(label, lambda: self._push_op(op))

    def _plain_button(self, label, slot):
        button = QPushButton(label)
        button.setFixedHeight(26)
        button.setMinimumWidth(30)
        button.clicked.connect(slot)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # 포커스는 입력창에 남긴다
        return button

    # ── 자동완성 ─────────────────────────────────────────────────────────────

    def _on_text_edited(self, text):
        self._update_suggest(text)

    def _update_suggest(self, term, force_all=False):
        query = str(term or "").strip().lower()
        if not query and not force_all:
            self._hide_suggest()
            return
        rows = [n for n in self._items if not query or query in n.lower()][:_SUGGEST_MAX]
        self._suggest_rows = rows
        self.suggest.clear()
        for name in rows:
            self.suggest.addItem(QListWidgetItem(name))
        if rows:
            self.suggest.setCurrentRow(0)
        self.suggest.setVisible(bool(rows))

    def _suggest_open(self) -> bool:
        """후보 목록이 열려 있는가.

        `QWidget.isVisible()` 은 창이 아직 show 되기 전이면 항상 False 라 판정 기준으로
        쓸 수 없다(다이얼로그가 뜨기 전 키 입력·테스트에서 조용히 어긋난다). 후보가
        있느냐 없느냐가 곧 열림 여부이므로 그 상태를 직접 본다.
        """
        return bool(self._suggest_rows)

    def _hide_suggest(self):
        self._suggest_rows = []
        self.suggest.clear()
        self.suggest.setVisible(False)

    def _on_suggest_activated(self, item):
        if item is not None:
            self._commit_item(item.text())

    def _commit_item(self, name):
        self._push({"t": "item", "item": str(name)})
        self.input.clear()
        self._hide_suggest()

    # ── 키 처리 ──────────────────────────────────────────────────────────────

    def _on_key(self, event) -> bool:
        """True 를 돌려주면 이 위젯이 처리한 것 (QLineEdit 기본 동작을 막는다)."""
        key = event.key()
        text = event.text()
        empty = not self.input.text()

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit_input()
            return True
        if key == Qt.Key.Key_Escape and self._suggest_open():
            self._hide_suggest()
            return True
        if key == Qt.Key.Key_Backspace and empty:
            self._pop()
            return True
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self._suggest_open():
            row = self.suggest.currentRow()
            step = 1 if key == Qt.Key.Key_Down else -1
            self.suggest.setCurrentRow(
                max(0, min(self.suggest.count() - 1, row + step)))
            return True
        if empty and text:
            if text == "@":
                # 웹 Issue Table 멘션과 같은 감각 — @ 자체는 검색어에 넣지 않는다.
                self._update_suggest("", force_all=True)
                return True
            if text in _OP_KEYS:
                self._push_op(_OP_KEYS[text])
                return True
            if text in _PAREN_KEYS:
                self._push({"t": _PAREN_KEYS[text]})
                return True
        return False

    def _commit_input(self):
        raw = self.input.text().strip()
        if self._suggest_open() and self.suggest.currentItem() is not None:
            self._commit_item(self.suggest.currentItem().text())
            return
        if not raw:
            return
        value = formula.num(raw)
        if value is not None:
            self._push({"t": "num", "v": float(value)})
            self.input.clear()
            self._hide_suggest()
            return
        # 자유 텍스트 item 은 만들 수 없다 — 이름에 뭐든 들어갈 수 있어서 사용자가 친 글자가
        # 실제 항목인지 확인할 방법이 없다. 목록에서 고르게 한다.
        self._update_suggest(raw)
        if not self._suggest_rows:
            self.input.setStyleSheet("border: 1px solid #dc2626;")
        else:
            self.input.setStyleSheet("")

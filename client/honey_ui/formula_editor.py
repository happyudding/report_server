"""FormulaEditor — 수식을 줄글로 치면 해독해 주는 입력 위젯 (2026-08-25 개편).

Rawdata 허브의 `신규 Item(수식) 추가` 페이지가 쓴다.

**항목은 `@` 로만 넣는다.** `@` 를 치면 항목 후보가 뜨고, 고르면 `@"VDD_A"` 인용 표기가
자동으로 박힌다(이름 안의 `"` 는 `""` 로 escape). 그 밖의 것 — 숫자·연산자·괄호·함수 —
은 전부 그냥 타이핑한다. 함수명과 항목 조회는 대소문자를 가리지 않는다.

인용 표기를 쓰는 이유는 item 이름에 공백·`( )`·`+ - * /`·따옴표가 전부 합법이기 때문이다
(honeyform 은 중복·메타명 충돌만 검사한다). `VDD-VSS + 1` 이라는 글자만으로는
`[VDD-VSS][+][1]` 인지 `[VDD][-][VSS][+][1]` 인지 가릴 수 없다 — 항목을 글자에서
*찾아내지 않고* 명시적 구분자로 받으면 그 모호성이 통째로 사라진다.
`SUM(...)`=함수 / `@"SUM"`=항목 충돌도 같이 사라진다.
렉서·문법 검사는 서버와 같은 순수 모듈(`web_report.formula`)이라 값이 갈릴 수 없다.

정본은 **텍스트**다. 파란 기울임·빨간 물결 밑줄은 렉싱 결과를 보고 QSyntaxHighlighter 가
얹는 **표시**일 뿐이며 문서에 저장되지 않는다 — 그래서 undo 스택이 오염되지 않고,
사용자가 수식을 복사해 두었다 붙여넣어도 그대로 살아난다.
항목 이름의 글자를 지우면 그 순간 목록에 없는 이름이 되어 빨간 밑줄 + 오류가 된다.

개행은 넣지 않는다(Enter 는 후보 확정 전용, 붙여넣기의 줄바꿈은 공백으로 치환) —
문서를 항상 블록 1개로 유지해 문자 오프셋 계산을 단순하게 둔다.
"""
from __future__ import annotations

from html import escape

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from web_report import formula

# 칩·본문 색 — 웹 Gap Chart(.gc-tok-*)와 같은 팔레트. 항목은 파란 기울임이 사용자 요청 서식이다.
_COLOR = {
    "item": "#1D4ED8",
    "num": "#111827",
    "op": "#6B7280",
    "cmp": "#0F766E",
    "fn": "#7C3AED",
    "paren": "#374151",
}
_BAD = "#DC2626"

_SUGGEST_MAX = 30
# 후보 팝업을 살려 둘 `@` 뒤 최대 길이 — 이보다 길면 `@` 를 치다 만 흔적으로 보고 닫는다.
_MENTION_MAX = 60


class _ImeTextEdit(QPlainTextEdit):
    """한글 조합 중에는 키를 가로채지 않는 한 줄짜리 입력창.

    조합 중(preedit 이 남아 있는 동안) Enter 는 "후보 확정"이 아니라 "조합 확정"이다.
    이 가드가 없으면 한글 항목을 검색하다 Enter 를 누를 때 조합이 확정되면서 동시에 엉뚱한
    항목이 들어간다. ↑↓ 도 같은 이유로 조합 중에는 Qt 에 맡긴다.
    """

    def __init__(self, on_key, parent=None):
        super().__init__(parent)
        self._composing = False
        self._on_key = on_key

    def inputMethodEvent(self, event):
        self._composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # 조합 중이든 아니든 **줄바꿈은 넣지 않는다**(문서를 블록 1개로 유지).
            # 조합 중에는 후보 확정도 하지 않는다 — 그 Enter 는 조합을 끝내라는 뜻이고,
            # 확정은 IME 가 inputMethodEvent 로 해 준다.
            if not self._composing:
                self._on_key(event)
            return
        if self._composing or not self._on_key(event):
            super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        # 서식·줄바꿈을 벗겨 평문만 받는다 (문서를 블록 1개로 유지한다).
        text = (source.text() or "").replace("\r", " ").replace("\n", " ")
        self.insertPlainText(text)


class _Highlighter(QSyntaxHighlighter):
    """렉싱 결과를 보고 색·밑줄만 얹는다. 문서 내용·undo 스택은 건드리지 않는다."""

    def __init__(self, document, owner):
        super().__init__(document)
        self._owner = owner

    def highlightBlock(self, text):
        if self.currentBlock().position() != 0:   # 개행을 막으므로 블록은 늘 1개다
            return
        for (start, end), fmt in self._owner.marks():
            if 0 <= start < end <= len(text):
                self.setFormat(start, end - start, fmt)


def _fmt(color, *, italic=False, bold=False):
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    if italic:
        fmt.setFontItalic(True)
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    return fmt


def _bad_fmt():
    fmt = QTextCharFormat()
    fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
    fmt.setUnderlineColor(QColor(_BAD))
    return fmt


class FormulaEditor(QWidget):
    """수식 입력기. `tokens()` 가 정본 산출물, 입력 정본은 `text()` 다."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._tokens = []
        self._spans = []
        self._error = ""
        self._error_index = None
        self._error_span = None
        self._marks = []
        self._suggest_rows = []
        self._busy = False

        self.chips = QLabel()
        self.chips.setTextFormat(Qt.TextFormat.RichText)
        self.chips.setWordWrap(True)
        self.chips.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.chips.setStyleSheet("padding: 6px 8px;")
        chip_area = QScrollArea()
        chip_area.setWidget(self.chips)
        chip_area.setWidgetResizable(True)
        chip_area.setFixedHeight(52)
        chip_area.setStyleSheet("QScrollArea { background: #f8fafc; border: 1px solid #d4d4d8;"
                                " border-radius: 5px; }")

        self.input = _ImeTextEdit(self._on_key)
        self.input.setPlaceholderText(
            '수식을 그대로 적으세요.  항목은 @ 로 넣습니다 — '
            '예: if(@"VDD_A" > min(@"VDD_B", 1), 0, 1)')
        self.input.setFixedHeight(64)
        self.input.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.input.textChanged.connect(self._on_text_changed)
        self._hl = _Highlighter(self.input.document(), self)

        self.suggest = QListWidget()
        self.suggest.setFixedHeight(132)
        self.suggest.setVisible(False)
        self.suggest.itemActivated.connect(self._on_suggest_activated)
        self.suggest.itemClicked.connect(self._on_suggest_activated)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(chip_area)
        layout.addWidget(self.input)
        layout.addWidget(self.suggest)
        self._render_chips()

    # ── 공개 API ─────────────────────────────────────────────────────────────

    def set_items(self, names):
        """항목 후보 (전 source item 이름 합집합). 렉서의 조회 목록이기도 하다."""
        self._items = [str(n) for n in names or []]
        self._relex()

    def tokens(self) -> list:
        return [dict(t) for t in self._tokens]

    def text(self) -> str:
        """사용자가 친 원문 — 이쪽이 입력 정본이다."""
        return self.input.toPlainText()

    def set_text(self, value):
        self.input.setPlainText(str(value or ""))

    def clear(self):
        self.input.clear()
        self._hide_suggest()

    def validate(self):
        """(tokens|None, 에러 메시지, 문제 토큰 index). 비어 있으면 (None, "", None)."""
        if not self.text().strip():
            return None, "", None
        if self._error:
            return None, self._error, self._error_index
        return [dict(t) for t in self._tokens], "", None

    def mark_error(self, index):
        """허브가 부르는 하위호환 진입점 — 표시는 이미 렉싱 때 갱신돼 있다."""
        self._render_chips(bad_index=index if index is not None else self._error_index)

    def error_html(self) -> str:
        """경고창용 RichText — 수식 원문에서 문제 구간을 빨갛게 칠해 보여 준다.

        캐럿(`^^^`) 정렬은 한글·전각 문자 폭 때문에 어긋나므로 쓰지 않는다.
        """
        if not self._error:
            return ""
        text = self.text()
        span = self._error_span
        if span and 0 <= span[0] < span[1] <= len(text):
            start, end = span
            body = (escape(text[:start])
                    + '<span style="background:#fecaca;color:#991b1b;font-weight:700">'
                    + escape(text[start:end]) + "</span>"
                    + escape(text[end:]))
        else:
            body = escape(text)
        return ('<div style="font-family:Consolas,monospace;white-space:pre-wrap;'
                'background:#f8fafc;padding:8px;border:1px solid #e4e4e7">'
                f'{body}</div>'
                f'<div style="margin-top:10px">⚠ {escape(self._error)}</div>')

    def marks(self):
        """highlighter 가 읽는 (span, QTextCharFormat) 목록."""
        return self._marks

    # ── 렉싱 ─────────────────────────────────────────────────────────────────

    def _on_text_changed(self):
        # 재진입 가드 — `rehighlight()` 가 문자 서식을 고치면 문서가 다시
        # `textChanged` 를 쏜다. 막지 않으면 그대로 무한 재귀(스택 넘침)다.
        if self._busy:
            return
        self._busy = True
        try:
            self._relex()
            query, _ = self._mention_query()
            if query is None:
                self._hide_suggest()
            else:
                self._update_suggest(query, force_all=not query)
            self.changed.emit()
        finally:
            self._busy = False

    def _relex(self):
        """텍스트 → 토큰·표시 마크·오류. 이 위젯의 모든 상태가 여기서 정해진다."""
        text = self.text()
        self._error = ""
        self._error_index = None
        self._error_span = None
        if not text.strip():
            # 아직 아무것도 안 쳤다 — "수식이 비어 있습니다" 를 오류로 띄우지 않는다.
            self._tokens, self._spans = [], []
            self._rebuild_marks()
            self._render_chips()
            return
        try:
            tokens, spans = formula.lex(text, self._items)
        except formula.FormulaError as exc:
            # 오류 지점까지 읽은 토큰은 그대로 칠한다 — 한 글자 잘못 쳤다고 앞부분
            # 색이 통째로 사라지면 무엇이 잘못됐는지 더 알기 어려워진다.
            tokens = list(getattr(exc, "tokens", None) or [])
            spans = list(getattr(exc, "spans", None) or [])
            self._error = str(exc)
            self._error_span = exc.span
        else:
            try:
                formula.normalize_tokens(tokens)
            except formula.FormulaError as exc:
                self._error = str(exc)
                self._error_index = exc.index
                self._error_span = formula.error_span(spans, exc.index, len(text))
        self._tokens = tokens
        self._spans = spans
        self._rebuild_marks()
        self._render_chips(bad_index=self._error_index)

    def _rebuild_marks(self):
        marks = []
        for tok, span in zip(self._tokens, self._spans):
            kind = tok.get("t")
            if kind == "item":
                marks.append((span, _fmt(_COLOR["item"], italic=True, bold=True)))
            elif kind == "fn":
                marks.append((span, _fmt(_COLOR["fn"], bold=True)))
            elif kind == "num":
                marks.append((span, _fmt(_COLOR["num"])))
            elif kind == "op":
                key = "cmp" if tok.get("v") in formula._CMP else "op"
                marks.append((span, _fmt(_COLOR[key], bold=True)))
            else:
                marks.append((span, _fmt(_COLOR["paren"], bold=True)))
        if self._error_span:
            marks.append((self._error_span, _bad_fmt()))
        self._marks = marks
        self._hl.rehighlight()

    # ── 칩 (읽기 전용 결과) ──────────────────────────────────────────────────

    def _chip_html(self, tok, bad):
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
        return f'<span style="{style}">{text}</span>'

    def _render_chips(self, bad_index=None):
        if not self._tokens:
            hint = ("읽은 결과가 여기 보입니다 — 항목은 <b>@</b> 로 넣고 나머지는 그대로 "
                    "타이핑하세요 (대소문자 무관)")
            self.chips.setText(f'<span style="color:#9aa1ab">{hint}</span>')
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
            parts.append(self._chip_html(tok, bad_index == i))
            prev = kind
        self.chips.setText("".join(parts))

    # ── 자동완성 (@) ─────────────────────────────────────────────────────────

    def _mention_query(self):
        """커서 앞의 `@` 검색어 → (query, `@` 위치). 검색 중이 아니면 (None, -1).

        완성된 `@"이름"` 은 조각에 `"` 가 들어 있으므로 자연히 걸러진다.
        """
        pos = self.input.textCursor().position()
        head = self.text()[:pos]
        at = head.rfind("@")
        if at < 0:
            return None, -1
        frag = head[at + 1:]
        if '"' in frag or len(frag) > _MENTION_MAX:
            return None, -1
        return frag, at

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
            self._insert_item(item.text())

    def _insert_item(self, name):
        """검색 중이던 `@질의어` 를 `@"정식이름"` 으로 바꾼다."""
        query, at = self._mention_query()
        cursor = self.input.textCursor()
        if at >= 0:
            cursor.setPosition(at)
            cursor.setPosition(at + 1 + len(query), QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(formula.quote_item(name))
        self.input.setTextCursor(cursor)
        self._hide_suggest()

    # ── 키 처리 ──────────────────────────────────────────────────────────────

    def _on_key(self, event) -> bool:
        """True 를 돌려주면 이 위젯이 처리한 것 (기본 동작을 막는다)."""
        key = event.key()

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._suggest_open() and self.suggest.currentItem() is not None:
                self._insert_item(self.suggest.currentItem().text())
            return True                       # 줄바꿈은 넣지 않는다
        if key == Qt.Key.Key_Escape and self._suggest_open():
            self._hide_suggest()
            return True
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self._suggest_open():
            row = self.suggest.currentRow()
            step = 1 if key == Qt.Key.Key_Down else -1
            self.suggest.setCurrentRow(
                max(0, min(self.suggest.count() - 1, row + step)))
            return True
        return False

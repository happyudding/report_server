"""상태바(하단 노란 bar) 메시지 히스토리.

QStatusBar.showMessage() 는 한 줄만 보여주고 이전 메시지를 지운다 — 사용자가
"방금 뭐라고 지나갔지" 를 되짚을 방법이 없었다. HistoryStatusBar 가 showMessage
를 가로채 시각과 함께 쌓아 두고, 상태바를 클릭하면 StatusHistoryDialog 가
세로 스크롤 목록으로 전부 보여준다.

실행 로그(txt_summary)와는 별개다 — 그쪽은 분석/업로드 단계 로그이고 여기는
상태바로 지나간 문구만 담는다.
"""
import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QStatusBar,
    QVBoxLayout,
)

MAX_ENTRIES = 500          # 오래된 것부터 버린다 (장시간 실행 시 메모리 상한)


class HistoryStatusBar(QStatusBar):
    """showMessage 를 가로채 (시각, 문구) 로 쌓는 상태바.

    호출부가 20곳 넘게 흩어져 있고 _ElapsedProgress 에 showMessage 를 콜백으로
    넘기는 자리도 있어서, 호출부를 고치는 대신 여기서 한 번에 잡는다.
    """
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._history = []
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("클릭하면 지금까지의 상태 메시지를 모두 볼 수 있습니다.")

    def showMessage(self, message, timeout=0):
        text = str(message or "").strip()
        # 같은 문구가 연속으로 오면(진행률 갱신 등) 시각만 최신으로 바꾼다.
        if text and (not self._history or self._history[-1][1] != text):
            self._history.append((time.strftime("%H:%M:%S"), text))
            if len(self._history) > MAX_ENTRIES:
                del self._history[:len(self._history) - MAX_ENTRIES]
        elif text:
            self._history[-1] = (time.strftime("%H:%M:%S"), text)
        super().showMessage(message, timeout)

    def history(self):
        return list(self._history)

    def clear_history(self):
        self._history.clear()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class StatusHistoryDialog(QDialog):
    """상태 메시지 히스토리를 세로 스크롤로 보여주는 팝업."""

    def __init__(self, parent, status_bar):
        super().__init__(parent)
        self._bar = status_bar
        self.setWindowTitle("상태 메시지 기록")
        self.resize(760, 460)

        v = QVBoxLayout(self)
        self._lbl_count = QLabel()
        v.addWidget(self._lbl_count)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        v.addWidget(self._view, 1)

        row = QHBoxLayout()
        btn_copy = QPushButton("전체 복사")
        btn_copy.clicked.connect(self._copy)
        btn_clear = QPushButton("기록 지우기")
        btn_clear.clicked.connect(self._clear)
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_copy)
        row.addWidget(btn_clear)
        row.addStretch(1)
        row.addWidget(btn_close)
        v.addLayout(row)

        self._reload()

    def _text(self):
        return "\n".join(f"[{ts}] {msg}" for ts, msg in self._bar.history())

    def _reload(self):
        entries = self._bar.history()
        self._lbl_count.setText(f"총 {len(entries)}건 (최근 {MAX_ENTRIES}건까지 보관)")
        self._view.setPlainText(self._text())
        # 최신 메시지가 보이도록 맨 아래로.
        bar = self._view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _copy(self):
        QGuiApplication.clipboard().setText(self._text())

    def _clear(self):
        self._bar.clear_history()
        self._reload()

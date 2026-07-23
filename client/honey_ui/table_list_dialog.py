"""긴 목록을 표로 보여주는 공용 위젯/다이얼로그.

QMessageBox·QTextBrowser 본문에 목록을 통째로 넣으면 (1) 줄 수가 많으면 잘리거나 창이
화면을 넘어가고, (2) 한 줄이 길면 오른쪽에서 접혀 읽기가 어렵고, (3) 정렬·검색·복사가
안 된다. 열이 고정된 표는 셋 다 해결하고 수만 행도 스크롤로 다룰 수 있다.

쓰는 곳:
  - ChangeReviewDialog  — Rawdata Excel 왕복의 셀 변경 목록 (수천 건)
  - honey_main._warn_duplicate_items — 항목명 중복 자동 개명 내역

QStandardItemModel 은 5만 행이면 항목 객체를 수십만 개 만들어 느리므로, 파이썬 리스트
위에 얹는 가벼운 모델을 쓴다.
"""
from __future__ import annotations

import csv

from PyQt6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
)
from PyQt6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

_MAX_SCREEN_RATIO = 0.7
_MAX_COL_WIDTH = 320


def fit_dialog_to_screen(dialog, width, height):
    """화면의 70% 를 넘지 않게 크기를 잡는다 — 버튼이 화면 밖으로 나가지 않도록."""
    screen = dialog.screen() or QGuiApplication.primaryScreen()
    avail = screen.availableGeometry() if screen else None
    if avail is None:
        dialog.resize(width, height)
        return
    max_w = int(avail.width() * _MAX_SCREEN_RATIO)
    max_h = int(avail.height() * _MAX_SCREEN_RATIO)
    dialog.setMaximumSize(max_w, max_h)
    dialog.resize(min(width, max_w), min(height, max_h))


def _sort_key(text):
    """숫자로 읽히면 숫자로, 아니면 문자로 정렬한다 ('10' 이 '9' 앞에 오지 않게).

    타입이 섞여도 비교가 터지지 않도록 (그룹, 값) 튜플로 돌려준다."""
    text = "" if text is None else str(text)
    try:
        return (0, float(text), "")
    except ValueError:
        return (1, 0.0, text.lower())


class _RowsModel(QAbstractTableModel):
    """헤더 + 행 리스트만 보관하는 읽기 전용 모델 (행 = 문자열 시퀀스)."""

    def __init__(self, headers, rows, parent=None):
        super().__init__(parent)
        self._headers = list(headers)
        self._rows = [tuple(r) for r in rows]
        self._haystack = None      # 검색용 행 문자열 (첫 검색 때 만든다)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = self._rows[index.row()]
        col = index.column()
        return str(row[col]) if col < len(row) else ""

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        return section + 1

    def haystack(self, row) -> str:
        """검색 대상 — 한 행을 소문자 한 줄로 합친 것. 기본 필터는 행마다 열 수만큼
        data() 를 파이썬으로 되묻느라 5만 행에서 1초를 넘긴다."""
        if self._haystack is None:
            self._haystack = [
                "\t".join("" if v is None else str(v) for v in r).lower()
                for r in self._rows]
        return self._haystack[row]

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        """행 리스트를 직접 재정렬한다.

        프록시의 lessThan 으로 정렬하면 5만 행에서 파이썬 비교가 80만 번 돌아 몇 초가
        걸린다. 여기서는 키를 행당 1번만 만들고 나머지는 파이썬 내장 정렬(C)이 맡는다.
        """
        if column < 0 or column >= len(self._headers):
            return
        reverse = order == Qt.SortOrder.DescendingOrder
        self.beginResetModel()
        self._rows.sort(reverse=reverse,
                        key=lambda r: _sort_key(r[column] if column < len(r) else ""))
        self._haystack = None      # 행 순서가 바뀌었으므로 다시 만든다
        self.endResetModel()


class _SortFilterProxy(QSortFilterProxyModel):
    """정렬·필터를 모두 소스 모델의 파이썬 리스트 위에서 처리한다 (위 주석 참조)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._needle = ""

    def set_needle(self, text):
        self._needle = (text or "").strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        return not self._needle or self._needle in self.sourceModel().haystack(row)

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sourceModel().sort(column, order)


class TableListView(QWidget):
    """검색 + 정렬 + 복사 + CSV 저장이 되는 표 한 장.

    rows 는 헤더와 같은 길이의 시퀀스 리스트. note 는 표 위에 붙는 안내(상한 초과 등).
    """

    def __init__(self, headers, rows, parent=None, note=""):
        super().__init__(parent)
        self._headers = list(headers)

        self._model = _RowsModel(self._headers, rows, self)
        self._proxy = _SortFilterProxy(self)
        self._proxy.setSourceModel(self._model)
        self.view = QTableView(self)
        self.view.setModel(self._proxy)
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setWordWrap(False)
        self.view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.verticalHeader().setVisible(False)
        header = self.view.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # 전 행을 재어 열 폭을 맞추면 5만 행에서 수 초가 걸린다 — 앞쪽 표본만 본다.
        header.setResizeContentsPrecision(200)

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("검색 (항목명·좌표·값 등 — 모든 열에서 찾습니다)")
        self._search.setClearButtonEnabled(True)
        # 큰 표에서는 한 글자마다 거르면 입력이 끊긴다 — 잠깐 멈췄을 때만 적용한다.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._apply_search)
        self._search.textChanged.connect(self._search_timer.start)
        self._count = QLabel(self)

        top = QHBoxLayout()
        top.addWidget(self._search, 1)
        top.addWidget(self._count)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if note:
            hint = QLabel(note, self)
            hint.setWordWrap(True)
            hint.setStyleSheet("color:#b45309;")
            layout.addWidget(hint)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)

        # Ctrl+C = 선택 영역 TSV 복사 (Excel 에 그대로 붙는다). 표에 포커스가 있을 때만 —
        # 창 전체로 잡으면 같은 창의 QTextBrowser(개요 탭) 복사를 가로챈다.
        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, self.view)
        copy_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_sc.activated.connect(self.copy_selection)
        self._update_count()
        self._autosize_columns()

    # ── 내부 ────────────────────────────────────────────────────────────────
    def _apply_search(self):
        # 정규식이 아니라 부분 문자열 검색 (사용자가 넣은 특수문자를 글자 그대로 찾는다)
        self._proxy.set_needle(self._search.text())
        self._update_count()

    def _update_count(self):
        shown, total = self._proxy.rowCount(), self._model.rowCount()
        self._count.setText(f"{shown:,} / {total:,} 행"
                            if shown != total else f"{total:,} 행")

    def _autosize_columns(self):
        """내용에 맞추되 한 열이 창을 다 먹지 않게 상한을 둔다."""
        self.view.resizeColumnsToContents()
        header = self.view.horizontalHeader()
        for col in range(self._model.columnCount()):
            if header.sectionSize(col) > _MAX_COL_WIDTH:
                header.resizeSection(col, _MAX_COL_WIDTH)

    def _visible_rows(self):
        """현재 표에 보이는 순서 그대로의 행 (검색·정렬 반영)."""
        out = []
        for r in range(self._proxy.rowCount()):
            out.append([self._proxy.index(r, c).data() or ""
                        for c in range(self._proxy.columnCount())])
        return out

    # ── 공개 ────────────────────────────────────────────────────────────────
    def copy_selection(self):
        """선택 셀을 TSV 로 클립보드에 넣는다. 선택이 없으면 보이는 전량."""
        indexes = self.view.selectionModel().selectedIndexes()
        if not indexes:
            rows = self._visible_rows()
        else:
            cells = {}
            for idx in indexes:
                cells.setdefault(idx.row(), {})[idx.column()] = idx.data() or ""
            rows = [[cols[c] for c in sorted(cols)] for _, cols in sorted(cells.items())]
        QGuiApplication.clipboard().setText(
            "\n".join("\t".join(str(v) for v in row) for row in rows))

    def save_csv(self, path):
        """보이는 행을 CSV 로 저장. 한국어 Windows Excel 이 cp949 로 읽지 않게 BOM 을 붙인다."""
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(self._headers)
            writer.writerows(self._visible_rows())

    def row_count(self):
        return self._model.rowCount()


class TableListDialog(QDialog):
    """표 한 장짜리 안내 다이얼로그 — 요약 한 줄 + 표 + [확인] [CSV 저장…]."""

    def __init__(self, parent, title, summary, headers, rows, *, note="",
                 csv_name="list.csv"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setSizeGripEnabled(True)
        self._csv_name = csv_name

        layout = QVBoxLayout(self)
        if summary:
            head = QLabel(summary, self)
            head.setWordWrap(True)
            head.setStyleSheet("font-size: 11pt; font-weight: 600; padding: 2px 0;")
            layout.addWidget(head)

        self.table = TableListView(headers, rows, self, note=note)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox()
        ok_btn = QPushButton("확인")
        save_btn = QPushButton("CSV 저장…")
        buttons.addButton(ok_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(save_btn, QDialogButtonBox.ButtonRole.ActionRole)
        ok_btn.clicked.connect(self.accept)
        save_btn.clicked.connect(self.save_csv)
        layout.addWidget(buttons)
        ok_btn.setDefault(True)

        fit_dialog_to_screen(self, 900, 620)

    def save_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "CSV 저장", self._csv_name,
                                              "CSV (*.csv)")
        if not path:
            return
        try:
            self.table.save_csv(path)
        except OSError as exc:
            QMessageBox.warning(self, "저장 실패", str(exc))

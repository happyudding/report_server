"""RawdataHubDialog — Rawdata edit 진입 허브 (Item Select / Outlier 제거 / Rawdata Edit).

종전에는 `Rawdata edit` 을 누르면 곧바로 Excel 이 떴다. 업로드가 끝난 세션에는 항목 선택도
outlier 제거도 걸 수 없었는데, 그 둘은 원본을 고치지 않고 **조회 시점에만 적용되는 옵션**
(서버 web_report/preprocess.py)이라 Excel 왕복 없이 처리할 수 있다.

  - Item Select  : 리포트에 표시할 측정 항목을 좌/우 리스트로 고른다 (되돌리기 가능)
  - Outlier 제거 : 항목별 mean ± k·stdev 밖 **측정값만** 결측 처리 (BIN/좌표/행 불변 →
                   수율·wafer map 은 그대로, CPK/Distribution 만 달라진다)
  - Rawdata Edit : 기존과 동일하게 Excel 로 열어 원본을 직접 편집한다

두 옵션은 세션 편집 DB 에 저장되고 원본 parquet 은 건드리지 않는다 — 비우면 즉시 원래
값으로 돌아온다. 그래서 Excel 편집(원본 대상)과 달리 확인 없이 저장해도 안전하다.
"""
from __future__ import annotations

from urllib.parse import quote

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

# 허브가 돌려주는 사용자 선택 — honey_main 이 이 값으로 다음 동작을 정한다.
ACTION_EXCEL = "excel"

_TIMEOUT = (10, 60)


def _headers(extra=None):
    """서버 신원 토큰 (excel_session._honey_headers 와 동일 규칙)."""
    try:
        import client_identity
        user = client_identity.collect().get("user", "")
    except Exception:
        user = ""
    headers = {"User-Agent": f"python-requests HoneyUser/{quote(user, safe='')}"} if user else {}
    headers.update(extra or {})
    return headers


class _ItemSelectPage(QWidget):
    """좌(제외) / 우(표시) 2리스트 — ReportSettingsDialog 의 항목 선택과 같은 조작 규칙.

    리스트 항목은 UserRole 에 원본 순서를 담아, 옮긴 뒤에도 원래 순서로 다시 정렬한다.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.list_excluded = QListWidget()
        self.list_shown = QListWidget()
        for lw in (self.list_excluded, self.list_shown):
            lw.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            lw.setUniformItemSizes(True)
        self.list_excluded.itemDoubleClicked.connect(
            lambda it: self._move(self.list_excluded, self.list_shown, [it]))
        self.list_shown.itemDoubleClicked.connect(
            lambda it: self._move(self.list_shown, self.list_excluded, [it]))

        btn_all_right = QPushButton(">>")
        btn_sel_right = QPushButton(">")
        btn_sel_left = QPushButton("<")
        btn_all_left = QPushButton("<<")
        btn_all_right.clicked.connect(lambda: self._move_all(self.list_excluded, self.list_shown))
        btn_all_left.clicked.connect(lambda: self._move_all(self.list_shown, self.list_excluded))
        btn_sel_right.clicked.connect(
            lambda: self._move(self.list_excluded, self.list_shown,
                               self.list_excluded.selectedItems()))
        btn_sel_left.clicked.connect(
            lambda: self._move(self.list_shown, self.list_excluded,
                               self.list_shown.selectedItems()))

        mid = QVBoxLayout()
        mid.addStretch(1)
        for b in (btn_all_right, btn_sel_right, btn_sel_left, btn_all_left):
            b.setFixedWidth(44)
            mid.addWidget(b)
        mid.addStretch(1)

        grid = QGridLayout(self)
        grid.addWidget(QLabel("제외 (리포트에 표시 안 함)"), 0, 0)
        grid.addWidget(QLabel("표시"), 0, 2)
        grid.addWidget(self.list_excluded, 1, 0)
        grid.addLayout(mid, 1, 1)
        grid.addWidget(self.list_shown, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)

    def populate(self, items, excluded):
        """items: [{"name","unit",...}] (전체 항목), excluded: 제외 중인 이름 집합."""
        self.list_excluded.clear()
        self.list_shown.clear()
        excluded = set(excluded or ())
        for idx, item in enumerate(items):
            name = str(item.get("name") or "")
            if not name:
                continue
            target = self.list_excluded if name in excluded else self.list_shown
            it = QListWidgetItem(name)
            it.setData(Qt.ItemDataRole.UserRole, idx)
            unit = str(item.get("unit") or "")
            if unit:
                it.setToolTip(f"{name} [{unit}]")
            target.addItem(it)

    def excluded_items(self):
        return [self.list_excluded.item(i).text()
                for i in range(self.list_excluded.count())]

    def shown_count(self):
        return self.list_shown.count()

    def _move(self, src, dst, items):
        for it in list(items):
            row = src.row(it)
            if row >= 0:
                dst.addItem(src.takeItem(row))
        self._resort(dst)

    def _move_all(self, src, dst):
        self._move(src, dst, [src.item(i) for i in range(src.count())])

    @staticmethod
    def _resort(lw):
        items = [lw.takeItem(0) for _ in range(lw.count())]
        items.sort(key=lambda it: it.data(Qt.ItemDataRole.UserRole))
        for it in items:
            lw.addItem(it)


class _OutlierPage(QWidget):
    """k(σ 배수) 한 칸. 비우면 해제 — 값 규칙은 서버 preprocess.normalize 가 정본."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit_k = QLineEdit()
        self.edit_k.setPlaceholderText("예: 50")
        self.edit_k.setMaximumWidth(120)

        row = QHBoxLayout()
        row.addWidget(QLabel("mean ±"))
        row.addWidget(self.edit_k)
        row.addWidget(QLabel("× stdev 밖의 측정값을 제거"))
        row.addStretch(1)

        box = QGroupBox("Outlier 제거 기준")
        box.setLayout(row)

        note = QLabel(
            "· 항목마다 평균과 표준편차를 구해 그 범위를 벗어난 <b>측정값만</b> 결측 처리합니다.<br>"
            "· die(행)·BIN·좌표는 그대로라 <b>수율과 Wafer Map 은 바뀌지 않고</b>, "
            "CPK·Distribution 의 n·평균·σ 만 달라집니다.<br>"
            "· 원본 데이터는 그대로 보관되므로 <b>칸을 비우고 저장하면 즉시 원래대로</b> 돌아옵니다.<br>"
            "· 표준편차가 0 인 항목(값이 모두 같음)은 대상이 없습니다.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.RichText)

        layout = QVBoxLayout(self)
        layout.addWidget(box)
        layout.addWidget(note)
        layout.addStretch(1)

    def value(self):
        """입력값 → k(float) 또는 None(해제). 형식 오류는 ValueError."""
        text = self.edit_k.text().strip()
        if not text:
            return None
        try:
            k = float(text)
        except ValueError:
            raise ValueError(f"숫자를 입력하세요 (입력값: {text})") from None
        if k <= 0:
            raise ValueError("0 보다 큰 값을 입력하세요 (비우면 해제됩니다).")
        return k

    def set_value(self, k):
        self.edit_k.setText("" if not k else (str(int(k)) if float(k).is_integer()
                                              else f"{float(k):g}"))


class RawdataHubDialog(QDialog):
    """세 동작의 진입 허브. exec() 후 self.action 이 ACTION_EXCEL 이면 Excel 편집으로 간다."""

    def __init__(self, parent, session_id, server_base):
        super().__init__(parent)
        self.session_id = session_id
        self.base = str(server_base).rstrip("/")
        self.action = ""
        self.changed = False          # 전처리 옵션을 저장했는가 (호출부가 새로고침 판단)
        self._items = []
        self._spec = {}

        self.setWindowTitle("Rawdata")
        self.resize(720, 520)

        self.btn_items = QPushButton("Item Select")
        self.btn_outlier = QPushButton("Outlier 제거")
        self.btn_excel = QPushButton("Rawdata Edit")
        for b in (self.btn_items, self.btn_outlier, self.btn_excel):
            b.setMinimumHeight(40)
            b.setCheckable(b is not self.btn_excel)
        self.btn_items.clicked.connect(lambda: self._show_page(0))
        self.btn_outlier.clicked.connect(lambda: self._show_page(1))
        self.btn_excel.clicked.connect(self._start_excel)

        top = QHBoxLayout()
        top.addWidget(self.btn_items, 1)
        top.addWidget(self.btn_outlier, 1)
        top.addWidget(self.btn_excel, 1)

        self.page_items = _ItemSelectPage()
        self.page_outlier = _OutlierPage()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.page_items)
        self.stack.addWidget(self.page_outlier)

        self.lbl_state = QLabel("")
        self.lbl_state.setWordWrap(True)

        self.buttons = QDialogButtonBox()
        self.btn_save = QPushButton("저장")
        self.btn_close = QPushButton("닫기")
        self.buttons.addButton(self.btn_save, QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton(self.btn_close, QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_save.clicked.connect(self._save)
        self.btn_close.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(QLabel(
            "Item Select / Outlier 는 <b>원본을 고치지 않고</b> 리포트 표시에만 적용됩니다 "
            "(Excel 편집에는 원본 전체가 그대로 열립니다)."))
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.lbl_state)
        layout.addWidget(self.buttons)

        self._load()
        self._show_page(0)

    # ── 서버 통신 ────────────────────────────────────────────────────────────
    def _load(self):
        """현재 항목 목록 + 저장된 전처리 옵션을 읽어 화면을 채운다."""
        import requests

        try:
            r = requests.get(
                f"{self.base}/pe/report/session/{self.session_id}/web_report/preprocess",
                headers=_headers(), timeout=_TIMEOUT)
            r.raise_for_status()
            self._spec = (r.json() or {}).get("spec") or {}

            r = requests.get(
                f"{self.base}/pe/report/session/{self.session_id}/web_report/raw_data/columns",
                headers=_headers(), timeout=_TIMEOUT)
            r.raise_for_status()
            self._items = (r.json() or {}).get("items") or []
        except Exception as exc:
            QMessageBox.warning(self, "Rawdata", f"세션 정보를 가져오지 못했습니다.\n{exc}")
            self._items, self._spec = [], {}

        self.page_items.populate(self._items, self._spec.get("exclude_items") or [])
        self.page_outlier.set_value((self._spec.get("outlier") or {}).get("k"))
        self._refresh_state()

    def _refresh_state(self):
        excluded = self.page_items.excluded_items()
        k = (self._spec.get("outlier") or {}).get("k")
        parts = [f"전체 항목 {len(self._items)}개",
                 f"표시 {self.page_items.shown_count()}개 / 제외 {len(excluded)}개"]
        parts.append(f"outlier ±{k:g}σ 적용 중" if k else "outlier 미적용")
        self.lbl_state.setText(" · ".join(parts))

    def _save(self):
        """전처리 옵션 저장 — 두 페이지의 현재 값을 함께 보낸다(부분 저장 없음)."""
        import requests

        try:
            k = self.page_outlier.value()
        except ValueError as exc:
            QMessageBox.warning(self, "Outlier 제거", str(exc))
            return
        spec = {"exclude_items": self.page_items.excluded_items()}
        if k:
            spec["outlier"] = {"mode": "stdev", "k": k}

        if not self.page_items.shown_count():
            QMessageBox.warning(self, "Item Select", "표시할 항목을 1개 이상 남겨 주세요.")
            return
        try:
            r = requests.post(
                f"{self.base}/pe/report/session/{self.session_id}/web_report/preprocess",
                json=spec, headers=_headers({"X-Honey-Agent": "1"}), timeout=_TIMEOUT)
            if r.status_code != 200:
                detail = ""
                try:
                    detail = (r.json() or {}).get("error") or ""
                except Exception:
                    detail = r.text[:200]
                raise RuntimeError(f"({r.status_code}) {detail}")
            result = r.json() or {}
        except Exception as exc:
            QMessageBox.warning(self, "Rawdata", f"저장하지 못했습니다.\n{exc}")
            return

        self._spec = result.get("spec") or {}
        self.changed = True
        self._refresh_state()
        QMessageBox.information(
            self, "Rawdata",
            "저장했습니다 — " + (result.get("summary") or "전처리 해제") +
            "\n리포트를 새로고침하면 반영됩니다.")

    def _start_excel(self):
        self.action = ACTION_EXCEL
        self.accept()

    # ── UI ───────────────────────────────────────────────────────────────────
    def _show_page(self, index):
        self.stack.setCurrentIndex(index)
        self.btn_items.setChecked(index == 0)
        self.btn_outlier.setChecked(index == 1)
        self._refresh_state()

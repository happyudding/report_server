"""RawdataHubDialog — Rawdata 진입 허브 (Item Select / Outlier 제거 / Rawdata 원본 수정).

종전에는 `Rawdata edit` 을 누르면 곧바로 Excel 이 떴다. 업로드가 끝난 세션에는 항목 선택도
outlier 제거도 걸 수 없었는데, 그 둘은 원본을 고치지 않고 **조회 시점에만 적용되는 필터**
(서버 web_report/preprocess.py)라 Excel 왕복 없이 처리할 수 있다.

레이아웃은 세로 grid 1장 — 페이지 전환 없이 전부 한눈에 보인다:

    [Item Select]        | Item List (제외 ↔ 표시 2리스트)
    [Outlier 제거]       | mean ± [stdev] × σ
    [Rawdata 원본 수정]  | (주황 — Excel 로 원본을 직접 고치는 유일한 버튼)
    ------------------------------------------------------
    [ ] Yield 계산 기준 - Test data 개수
                                            [저장] [닫기]

체크박스는 수율 **분모**를 고른다: 해제(기본)면 제품 기준정보 Gross Die, 체크면 종전처럼
그 소스의 rawdata 개수. Gross Die 가 비어 있으면 서버가 자동으로 rawdata 개수로 폴백한다.
저장 위치는 위 두 필터와 같은 세션 편집 DB(kind='yield_basis')라 다음에 열 때도 적용된다.

왼쪽 열의 Item Select / Outlier 제거 버튼은 [저장] 과 같이 **화면에 보이는 상태를 저장**한다
(행별 부분 저장은 다른 행을 되돌려야 해서 작업이 조용히 사라진다 — `_save` 참조).
저장하면 서버가 Summary/Yield/CPK/Issue Table/Distribution/Trim/Map 을 그 기준으로 다시
계산하고, 필터는 세션 DB 에 남아 다음에 열 때도 그대로 적용된다.

Item Select / Outlier 는 원본 parquet 을 건드리지 않으므로 비우고 저장하면 원상복구된다 —
그래서 Excel 편집(원본 대상, 되돌릴 수 없음)과 달리 확인창이 없다.
"""
from __future__ import annotations

from urllib.parse import quote

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# 허브가 돌려주는 사용자 선택 — honey_main 이 이 값으로 다음 동작을 정한다.
ACTION_EXCEL = "excel"

_TIMEOUT = (10, 60)
_ROW_BTN_W = 150

# 원본을 실제로 고치는 유일한 버튼 — 되돌릴 수 없으므로 주황으로 구분한다
# (나머지 둘은 조회 필터라 언제든 해제 가능).
_DANGER_BTN_QSS = """
QPushButton {
  background: #f97316; color: #ffffff; font-weight: 700;
  border: 1px solid #c2410c; border-radius: 5px; padding: 8px 14px;
}
QPushButton:hover { background: #fb923c; }
QPushButton:pressed { background: #ea580c; }
"""


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


class _ItemListWidget(QWidget):
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
            b.setFixedWidth(36)
            mid.addWidget(b)
        mid.addStretch(1)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(QLabel("제외"), 0, 0)
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
        self._gross_die = None        # 서버가 알려준 제품 기준정보 Gross Die (없으면 None)
        self._gross_die = None        # 세션 제품 기준정보 Gross Die (없으면 None)

        self.setWindowTitle("Rawdata")
        self.resize(720, 560)

        # ── 행 1: Item Select | Item List ────────────────────────────────────
        self.btn_items = QPushButton("Item Select")
        self.btn_items.setToolTip("선택한 항목만 남기고 저장 (원본은 그대로, 언제든 되돌릴 수 있음)")
        self.btn_items.clicked.connect(self._apply_items)
        self.item_list = _ItemListWidget()

        # ── 행 2: Outlier 제거 | mean ± [stdev] × σ ─────────────────────────
        self.btn_outlier = QPushButton("Outlier 제거")
        self.btn_outlier.setToolTip("mean ± (입력값)×stdev 밖의 측정값만 결측 처리 — 비우면 해제")
        self.btn_outlier.clicked.connect(self._apply_outlier)
        self.edit_k = QLineEdit()
        self.edit_k.setPlaceholderText("예: 50")
        self.edit_k.setFixedWidth(90)
        self.edit_k.returnPressed.connect(self._apply_outlier)
        outlier_row = QHBoxLayout()
        outlier_row.setContentsMargins(0, 0, 0, 0)
        outlier_row.addWidget(QLabel("mean ±"))
        outlier_row.addWidget(self.edit_k)
        outlier_row.addWidget(QLabel("× stdev 밖의 측정값 제거 (비우면 해제)"))
        outlier_row.addStretch(1)
        outlier_box = QWidget()
        outlier_box.setLayout(outlier_row)

        # ── 행 3: Rawdata 원본 수정 (Excel) ─────────────────────────────────
        self.btn_excel = QPushButton("Rawdata 원본 수정")
        self.btn_excel.setStyleSheet(_DANGER_BTN_QSS)
        self.btn_excel.setToolTip("Excel 로 원본 데이터를 직접 편집합니다 (되돌릴 수 없음)")
        self.btn_excel.clicked.connect(self._start_excel)

        for b in (self.btn_items, self.btn_outlier, self.btn_excel):
            b.setMinimumHeight(38)
            b.setMinimumWidth(_ROW_BTN_W)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.addWidget(self.btn_items, 0, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.item_list, 0, 1)
        grid.addWidget(self.btn_outlier, 1, 0)
        grid.addWidget(outlier_box, 1, 1)
        grid.addWidget(self.btn_excel, 2, 0)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)

        # ── 하단: Yield 분모 기준 체크박스 ───────────────────────────────────
        # 체크 = 종전대로 그 소스의 rawdata(test data) 개수를 분모로. 해제(기본) = 제품
        # 기준정보의 Gross Die 를 분모로 — Gross Die 가 비어 있으면 서버가 자동으로
        # rawdata 개수로 폴백한다. 값은 세션 DB 에 남아 다음에 열 때도 적용된다.
        self.chk_test_basis = QCheckBox("Yield 계산 기준 - Test data 개수")
        self.chk_test_basis.setToolTip(
            "체크: 수율 분모 = 그 소스의 rawdata 개수 (종전 동작)\n"
            "해제: 수율 분모 = 제품 기준정보 Gross Die (없으면 rawdata 개수로 폴백)")

        # ── 하단: 저장 / 닫기 ────────────────────────────────────────────────
        self.lbl_state = QLabel("")
        self.lbl_state.setWordWrap(True)
        buttons = QDialogButtonBox()
        self.btn_save = QPushButton("저장")
        self.btn_close = QPushButton("닫기")
        buttons.addButton(self.btn_save, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.btn_close, QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_save.clicked.connect(self._save)
        self.btn_close.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(grid, 1)
        layout.addWidget(self.chk_test_basis)
        layout.addWidget(self.lbl_state)
        layout.addWidget(buttons)

        self._load()

    # ── 서버 통신 ────────────────────────────────────────────────────────────
    def _load(self):
        """현재 항목 목록 + 저장된 필터를 읽어 화면을 채운다."""
        import requests

        try:
            r = requests.get(
                f"{self.base}/pe/report/session/{self.session_id}/web_report/preprocess",
                headers=_headers(), timeout=_TIMEOUT)
            r.raise_for_status()
            info = r.json() or {}
            self._spec = info.get("spec") or {}
            self._set_basis(info)

            r = requests.get(
                f"{self.base}/pe/report/session/{self.session_id}/web_report/raw_data/columns",
                headers=_headers(), timeout=_TIMEOUT)
            r.raise_for_status()
            self._items = (r.json() or {}).get("items") or []
        except Exception as exc:
            QMessageBox.warning(self, "Rawdata", f"세션 정보를 가져오지 못했습니다.\n{exc}")
            self._items, self._spec = [], {}

        self.item_list.populate(self._items, self._spec.get("exclude_items") or [])
        self._set_k((self._spec.get("outlier") or {}).get("k"))
        self._refresh_state()

    def _set_basis(self, info):
        """서버 응답(yield_basis/gross_die)을 체크박스 상태로 반영."""
        self._gross_die = (info or {}).get("gross_die")
        self.chk_test_basis.setChecked(str((info or {}).get("yield_basis") or "") == "test")

    def _set_k(self, k):
        self.edit_k.setText("" if not k else (str(int(k)) if float(k).is_integer()
                                              else f"{float(k):g}"))

    def _k_value(self):
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

    def _refresh_state(self):
        excluded = self.item_list.excluded_items()
        k = (self._spec.get("outlier") or {}).get("k")
        parts = [f"전체 {len(self._items)}개",
                 f"표시 {self.item_list.shown_count()} / 제외 {len(excluded)}"]
        parts.append(f"outlier ±{k:g}σ 적용 중" if k else "outlier 미적용")
        if self.chk_test_basis.isChecked():
            parts.append("Yield 분모 = Test data 개수")
        elif self._gross_die:
            parts.append(f"Yield 분모 = Gross Die {self._gross_die}")
        else:
            parts.append("Yield 분모 = Gross Die 정보 없음 → Test data 개수")
        self.lbl_state.setText(" · ".join(parts))

    def _apply_items(self):
        """Item Select 행 적용 — 화면에 보이는 상태 그대로 저장한다."""
        self._save()

    def _apply_outlier(self):
        """Outlier 행 적용 — 화면에 보이는 상태 그대로 저장한다."""
        self._save()

    def _save(self):
        """두 필터를 함께 저장 — **화면에 보이는 상태 그대로**.

        행 버튼(Item Select / Outlier 제거)도 여기로 온다. 행별로 '그 행만' 저장하면
        다른 행을 저장된 값으로 되돌려야 하는데, 그러면 옮겨 둔 항목이나 입력한 값이
        경고 없이 사라진다 — 저장 대상은 언제나 화면 상태 하나뿐이다.
        """
        try:
            k = self._k_value()
        except ValueError as exc:
            QMessageBox.warning(self, "Outlier 제거", str(exc))
            return
        spec = {"exclude_items": self.item_list.excluded_items(),
                "yield_basis": "test" if self.chk_test_basis.isChecked() else "gross"}
        if k:
            spec["outlier"] = {"mode": "stdev", "k": k}
        self._post(spec)

    def _post(self, spec):
        """필터 저장 — 서버가 전 탭(Summary/Yield/CPK/Issue/Distribution/Trim/Map)을
        이 기준으로 다시 계산하고, 세션 DB 에 남아 다음에 열 때도 그대로 적용된다."""
        import requests

        if not self.item_list.shown_count():
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
        self.item_list.populate(self._items, self._spec.get("exclude_items") or [])
        self._set_k((self._spec.get("outlier") or {}).get("k"))
        self._set_basis(result)
        self._refresh_state()
        QMessageBox.information(
            self, "Rawdata",
            "저장했습니다 — " + (result.get("summary") or "필터 해제") +
            "\n전 탭이 이 기준으로 다시 계산됩니다.")

    def _start_excel(self):
        self.action = ACTION_EXCEL
        self.accept()

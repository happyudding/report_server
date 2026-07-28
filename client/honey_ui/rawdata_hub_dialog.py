"""RawdataHubDialog — Rawdata 진입 허브 (현재 상태 / Item Select / Outlier / 빠른 수정 / Excel).

종전에는 `Rawdata edit` 을 누르면 곧바로 Excel 이 떴다. 업로드가 끝난 세션에는 항목 선택도
outlier 제거도 걸 수 없었는데, 그 둘은 원본을 고치지 않고 **조회 시점에만 적용되는 필터**
(서버 web_report/preprocess.py)라 Excel 왕복 없이 처리할 수 있다.

레이아웃은 **좌측 기능 버튼 + 우측 활성 패널**이다 (한 화면에 다 늘어놓으면 Item Select 의
2-리스트가 창을 다 먹는다):

    [현재 상태]        |  지금 적용 중인 전처리 목록 + 항목별 [해제] / [전체 해제]
    [Item Select]      |  Item List (제외 ↔ 표시 2리스트) + 검색
    [Outlier 제거]     |  mean ± [stdev] × σ
    [빠른 수정]        |  → 별도 다이얼로그 (표에서 고치기 / 조건 일괄 수정)
    [Rawdata 원본 수정]|  → Excel 왕복 (주황 — 원본을 직접 고치는 유일한 버튼)
    ---------------------------------------------------------------------
    [ ] Yield 계산 기준 - Test data 개수            [저장] [닫기]

체크박스는 수율 **분모**를 고른다: 해제(기본)면 제품 기준정보 Gross Die, 체크면 종전처럼
그 소스의 rawdata 개수. Gross Die 가 비어 있으면 서버가 자동으로 rawdata 개수로 폴백한다.
저장 위치는 위 두 필터와 같은 세션 편집 DB(kind='yield_basis')라 다음에 열 때도 적용된다.

[저장]은 **화면에 보이는 상태를 저장**한다 — 페이지가 나뉘어도 다이얼로그가 들고 있는 상태는
하나라 부분 저장 함정이 없다(`_save` 참조). 저장하면 서버가 Summary/Yield/CPK/Issue Table/
Distribution/Trim/Map 을 그 기준으로 다시 계산하고, 필터는 세션 DB 에 남아 다음에도 적용된다.

빠른 수정이 만든 셀 패치·조건 규칙도 같은 전처리 spec 에 들어간다. 이 다이얼로그는 그 두 키를
**건드리지 않고 그대로 유지**한다 — 서버가 edits/rules 를 "키 부재 = 유지"로 처리하기 때문
(레거시 키인 exclude_items/outlier 만 화면 상태로 덮어쓴다).

Item Select / Outlier / 빠른 수정은 원본 parquet 을 건드리지 않으므로 비우고 저장하면
원상복구된다 — 그래서 Excel 편집(원본 대상, 되돌릴 수 없음)과 달리 확인창이 없다.
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
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

# 허브가 돌려주는 사용자 선택 — honey_main 이 이 값으로 다음 동작을 정한다.
ACTION_EXCEL = "excel"
ACTION_QUICK = "quick"

_TIMEOUT = (10, 60)
_ROW_BTN_W = 170

# 원본을 실제로 고치는 유일한 버튼 — 되돌릴 수 없으므로 주황으로 구분한다
# (나머지는 조회 필터·패치라 언제든 해제 가능).
_DANGER_BTN_QSS = """
QPushButton {
  background: #f97316; color: #ffffff; font-weight: 700;
  border: 1px solid #c2410c; border-radius: 5px; padding: 8px 14px;
}
QPushButton:hover { background: #fb923c; }
QPushButton:pressed { background: #ea580c; }
"""

# 좌측 네비 — 선택된 기능만 눌린 상태로 보이게 한다(checkable + autoExclusive).
_NAV_BTN_QSS = """
QPushButton { text-align: left; padding: 9px 12px; border: 1px solid #d4d4d8;
              border-radius: 5px; background: #fafafa; }
QPushButton:hover { background: #f0f0f2; }
QPushButton:checked { background: #dbeafe; border-color: #3b82f6; font-weight: 700; }
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

        # 항목이 수백 개인 세션에서는 스크롤보다 타이핑이 빠르다 (웹 Raw Data 와 같은 UX).
        self.search = QLineEdit()
        self.search.setPlaceholderText("항목 검색 — 일치하지 않는 항목은 숨깁니다")
        self.search.textChanged.connect(self._filter)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.search, 0, 0, 1, 3)
        grid.addWidget(QLabel("제외"), 1, 0)
        grid.addWidget(QLabel("표시"), 1, 2)
        grid.addWidget(self.list_excluded, 2, 0)
        grid.addLayout(mid, 2, 1)
        grid.addWidget(self.list_shown, 2, 2)
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
        self._filter(self.search.text())

    def _filter(self, text):
        """검색어에 안 맞는 항목을 **숨기기만** 한다 — 목록에서 빼면 저장 대상이 달라진다."""
        query = str(text or "").strip().lower()
        for lw in (self.list_excluded, self.list_shown):
            for i in range(lw.count()):
                it = lw.item(i)
                it.setHidden(bool(query) and query not in it.text().lower())

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
    """Rawdata 진입 허브. exec() 후 self.action 으로 다음 동작을 정한다
    (ACTION_EXCEL=Excel 왕복 / ACTION_QUICK=빠른 수정 다이얼로그)."""

    def __init__(self, parent, session_id, server_base):
        super().__init__(parent)
        self.session_id = session_id
        self.base = str(server_base).rstrip("/")
        self.action = ""
        self.changed = False          # 전처리 옵션을 저장했는가 (호출부가 새로고침 판단)
        self._items = []
        self._spec = {}
        self._edits = []              # 빠른 수정 셀 패치 (여기선 표시·해제만)
        self._rules = []              # 조건 일괄 규칙 (여기선 표시·해제만)
        self._gross_die = None        # 세션 제품 기준정보 Gross Die (없으면 None)

        self.setWindowTitle("Rawdata")
        self.resize(860, 600)

        self.pages = QStackedWidget()
        self.nav_buttons = []
        nav = QVBoxLayout()
        nav.setSpacing(6)

        # ── 페이지 0: 현재 상태 (기본) ───────────────────────────────────────
        self._add_page(nav, "현재 상태", self._build_state_page(),
                       "지금 이 리포트에 적용 중인 전처리 목록 — 여기서 개별/전체 해제")

        # ── 페이지 1: Item Select ────────────────────────────────────────────
        self.item_list = _ItemListWidget()
        self._add_page(nav, "Item Select", self.item_list,
                       "선택한 항목만 남기고 저장 (원본은 그대로, 언제든 되돌릴 수 있음)")

        # ── 페이지 2: Outlier 제거 ───────────────────────────────────────────
        self.edit_k = QLineEdit()
        self.edit_k.setPlaceholderText("예: 50")
        self.edit_k.setFixedWidth(90)
        self.edit_k.returnPressed.connect(self._save)
        outlier_page = QWidget()
        outlier_layout = QVBoxLayout(outlier_page)
        outlier_row = QHBoxLayout()
        outlier_row.addWidget(QLabel("mean ±"))
        outlier_row.addWidget(self.edit_k)
        outlier_row.addWidget(QLabel("× stdev 밖의 측정값 제거 (비우면 해제)"))
        outlier_row.addStretch(1)
        outlier_layout.addLayout(outlier_row)
        outlier_layout.addWidget(QLabel(
            "항목별로 평균 ± (입력값)×표준편차 밖의 **측정값만** 결측 처리합니다.\n"
            "BIN·좌표·die 는 손대지 않으므로 수율·Wafer Map 은 그대로이고, "
            "CPK/Distribution 의 n·평균·σ 만 달라집니다."))
        outlier_layout.addStretch(1)
        self._add_page(nav, "Outlier 제거", outlier_page,
                       "mean ± (입력값)×stdev 밖의 측정값만 결측 처리 — 비우면 해제")

        # ── 페이지 3: 빠른 수정 (별도 다이얼로그로 이동) ─────────────────────
        quick_page = QWidget()
        quick_layout = QVBoxLayout(quick_page)
        quick_layout.addWidget(QLabel(
            "표에서 필요한 행만 조회해 값을 고치거나, 조건으로 한 번에 수정합니다.\n"
            "· 셀 직접 수정 / 붙여넣기 / 선택 영역 값 지정·빈값·오프셋·배율 / 찾아 바꾸기\n"
            "· 조건 일괄 수정 (예: DUT 3 의 VREF > 4.5 인 die 를 빈값으로)\n"
            "· 빠른 동작 — Bin1 only, Spec Out 빈값\n"
            "· 저장 전에 수율·CPK 변화를 미리 봅니다\n\n"
            "Excel 을 열지 않고 원본도 바꾸지 않습니다 — 언제든 되돌릴 수 있습니다."))
        btn_quick_go = QPushButton("빠른 수정 열기")
        btn_quick_go.setMinimumHeight(38)
        btn_quick_go.clicked.connect(self._start_quick)
        quick_layout.addWidget(btn_quick_go)
        quick_layout.addStretch(1)
        self._add_page(nav, "빠른 수정", quick_page,
                       "Excel 없이 표·조건으로 고칩니다 (원본 불변, 되돌릴 수 있음)")

        # ── 페이지 4: Rawdata 원본 수정 (Excel) ──────────────────────────────
        excel_page = QWidget()
        excel_layout = QVBoxLayout(excel_page)
        excel_layout.addWidget(QLabel(
            "전체 rawdata 를 Excel 로 내려받아 직접 편집한 뒤 서버에 반영합니다.\n\n"
            "· 시트(=source) 삭제, 복잡한 수식 작업처럼 표로는 안 되는 일에 씁니다.\n"
            "· 데이터가 크면 Excel 을 여는 것만으로도 오래 걸립니다 — 값 수정·조건 일괄\n"
            "  수정은 [빠른 수정] 이 훨씬 빠릅니다.\n"
            "· **원본을 실제로 바꿉니다. 되돌릴 수 없습니다.**"))
        self.btn_excel = QPushButton("Rawdata 원본 수정 (Excel 열기)")
        self.btn_excel.setStyleSheet(_DANGER_BTN_QSS)
        self.btn_excel.setMinimumHeight(38)
        self.btn_excel.clicked.connect(self._start_excel)
        excel_layout.addWidget(self.btn_excel)
        excel_layout.addStretch(1)
        self._add_page(nav, "Rawdata 원본 수정", excel_page,
                       "Excel 로 원본 데이터를 직접 편집합니다 (되돌릴 수 없음)")
        self.nav_buttons[-1].setStyleSheet(_NAV_BTN_QSS + """
            QPushButton { color: #c2410c; }
            QPushButton:checked { background: #ffedd5; border-color: #f97316; }
        """)

        nav.addStretch(1)
        nav_holder = QWidget()
        nav_holder.setLayout(nav)
        nav_holder.setFixedWidth(_ROW_BTN_W + 20)

        body = QHBoxLayout()
        body.addWidget(nav_holder)
        body.addWidget(self.pages, 1)

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
        layout.addLayout(body, 1)
        layout.addWidget(self.chk_test_basis)
        layout.addWidget(self.lbl_state)
        layout.addWidget(buttons)

        self.nav_buttons[0].setChecked(True)
        self._load()

    def _add_page(self, nav_layout, title, widget, tooltip):
        """좌측 네비 버튼 1개 + 우측 페이지 1개를 짝지어 등록한다."""
        button = QPushButton(title)
        button.setCheckable(True)
        button.setAutoExclusive(True)
        button.setStyleSheet(_NAV_BTN_QSS)
        button.setMinimumHeight(40)
        button.setToolTip(tooltip)
        index = self.pages.count()
        button.clicked.connect(lambda: self.pages.setCurrentIndex(index))
        nav_layout.addWidget(button)
        self.nav_buttons.append(button)
        self.pages.addWidget(widget)
        return button

    def _build_state_page(self):
        """현재 적용 중인 전처리 목록 + 개별/전체 해제.

        "지금 리포트가 왜 이렇게 보이는가"를 한 화면에서 답하는 것이 이 페이지의 목적이다.
        해제는 화면 상태만 바꾸고, 서버 반영은 [저장] 이 한다(다른 페이지와 같은 규칙)."""
        page = QWidget()
        layout = QVBoxLayout(page)
        self.list_state = QListWidget()
        self.list_state.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        btn_drop = QPushButton("선택 해제")
        btn_drop.clicked.connect(self._drop_selected_state)
        btn_drop_all = QPushButton("전체 해제")
        btn_drop_all.clicked.connect(self._drop_all_state)
        row = QHBoxLayout()
        row.addWidget(btn_drop)
        row.addWidget(btn_drop_all)
        row.addStretch(1)
        layout.addWidget(QLabel("이 리포트에 적용 중인 전처리 (원본은 그대로입니다)"))
        layout.addWidget(self.list_state, 1)
        layout.addLayout(row)
        layout.addWidget(QLabel("해제한 뒤 아래 [저장] 을 눌러야 서버에 반영됩니다."))
        return page

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
        # 빠른 수정이 만든 셀 패치·규칙 — 이 화면에서는 목록 표시와 해제만 한다.
        self._edits = list(self._spec.get("edits") or [])
        self._rules = list(self._spec.get("rules") or [])
        self._render_state_list()
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
        if self._edits or self._rules:
            parts.append(f"빠른 수정 셀 {len(self._edits)} / 규칙 {len(self._rules)}")
        if self.chk_test_basis.isChecked():
            parts.append("Yield 분모 = Test data 개수")
        elif self._gross_die:
            parts.append(f"Yield 분모 = Gross Die {self._gross_die}")
        else:
            parts.append("Yield 분모 = Gross Die 정보 없음 → Test data 개수")
        self.lbl_state.setText(" · ".join(parts))

    # ── 현재 상태 목록 ───────────────────────────────────────────────────────
    def _state_entries(self):
        """(표시 문구, 종류) 목록 — 종류는 해제 시 어떤 화면 상태를 지울지 가른다."""
        from web_report import preprocess

        entries = []
        excluded = self.item_list.excluded_items()
        if excluded:
            entries.append((f"항목 제외 {len(excluded)}개 — {', '.join(excluded[:6])}"
                            + (" …" if len(excluded) > 6 else ""), "items"))
        k = self._k_text()
        if k:
            entries.append((f"Outlier 제거 — mean ± {k}σ 밖 측정값 결측", "outlier"))
        if self._edits:
            entries.append((f"빠른 수정 — 셀 {len(self._edits)}건", "edits"))
        for idx, rule in enumerate(self._rules):
            entries.append((f"일괄 규칙 — {preprocess.describe_rule(rule)}", f"rule:{idx}"))
        return entries

    def _k_text(self):
        try:
            k = self._k_value()
        except ValueError:
            return ""
        return "" if not k else (str(int(k)) if float(k).is_integer() else f"{k:g}")

    def _render_state_list(self):
        self.list_state.clear()
        entries = self._state_entries()
        if not entries:
            it = QListWidgetItem("적용 중인 전처리가 없습니다 (업로드 원본 그대로).")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_state.addItem(it)
            return
        for text, kind in entries:
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, kind)
            self.list_state.addItem(it)

    def _drop_selected_state(self):
        it = self.list_state.currentItem()
        kind = it.data(Qt.ItemDataRole.UserRole) if it else None
        if not kind:
            QMessageBox.information(self, "현재 상태", "해제할 항목을 고르세요.")
            return
        if kind == "items":
            self.item_list.populate(self._items, [])
        elif kind == "outlier":
            self.edit_k.clear()
        elif kind == "edits":
            self._edits = []
        elif kind.startswith("rule:"):
            self._rules.pop(int(kind.split(":", 1)[1]))
        self._render_state_list()
        self._refresh_state()

    def _drop_all_state(self):
        if QMessageBox.question(
                self, "전체 해제",
                "적용 중인 전처리를 전부 해제합니다 (원본 그대로로 돌아갑니다).\n"
                "계속할까요? — [저장] 을 눌러야 서버에 반영됩니다."
        ) != QMessageBox.StandardButton.Yes:
            return
        self.item_list.populate(self._items, [])
        self.edit_k.clear()
        self._edits, self._rules = [], []
        self._render_state_list()
        self._refresh_state()

    # ── 저장 ─────────────────────────────────────────────────────────────────
    def _save(self):
        """**화면에 보이는 상태 그대로** 저장한다 (페이지가 나뉘어도 상태는 하나다).

        빠른 수정이 만든 edits/rules 는 이 화면에서 만들지 않지만, 현재 상태 페이지에서
        해제할 수 있으므로 **항상 명시해 보낸다** — 서버의 "키 부재 = 유지" 규약에만 기대면
        여기서 해제한 것이 반영되지 않는다.
        """
        try:
            k = self._k_value()
        except ValueError as exc:
            QMessageBox.warning(self, "Outlier 제거", str(exc))
            return
        spec = {"exclude_items": self.item_list.excluded_items(),
                "yield_basis": "test" if self.chk_test_basis.isChecked() else "gross",
                "edits": list(self._edits), "rules": list(self._rules)}
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
        self._edits = list(self._spec.get("edits") or [])
        self._rules = list(self._spec.get("rules") or [])
        self._set_basis(result)
        self._render_state_list()
        self._refresh_state()
        QMessageBox.information(
            self, "Rawdata",
            "저장했습니다 — " + (result.get("summary") or "필터 해제") +
            "\n전 탭이 이 기준으로 다시 계산됩니다.")

    def _start_quick(self):
        self.action = ACTION_QUICK
        self.accept()

    def _start_excel(self):
        """Excel 왕복 진입 — 셀 패치가 있으면 해제된다는 사실을 먼저 알린다.

        Excel 편집은 행을 지우거나 순서를 바꿀 수 있어 행 위치 기반 셀 패치가 무효가 되고,
        서버가 반영 시점에 그 패치를 해제한다(web_report/edits.drop_preprocess_edits).
        나중에 알면 "고쳐둔 게 사라졌다" 가 되므로 들어가기 전에 확인받는다."""
        if self._edits and QMessageBox.question(
                self, "Rawdata 원본 수정",
                f"빠른 수정으로 저장해 둔 셀 {len(self._edits)}건이 있습니다.\n\n"
                "Excel 편집을 서버에 반영하면 행 위치가 달라질 수 있어 그 셀 수정은 "
                "자동으로 해제됩니다 (조건 일괄 규칙은 유지됩니다).\n\n계속할까요?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.action = ACTION_EXCEL
        self.accept()

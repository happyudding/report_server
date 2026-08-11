"""RawdataHubDialog — Rawdata 진입 허브 (현재 상태 / Options / Item Select / Outlier /
Yield 계산 / Excel).

종전에는 `Rawdata edit` 을 누르면 곧바로 Excel 이 떴다. 업로드가 끝난 세션에는 항목 선택도
outlier 제거도 걸 수 없었는데, 그 둘은 원본을 고치지 않고 **조회 시점에만 적용되는 필터**
(서버 web_report/preprocess.py)라 Excel 왕복 없이 처리할 수 있다.

레이아웃은 **좌측 기능 버튼 + 우측 활성 패널**이다 (한 화면에 다 늘어놓으면 Item Select 의
2-리스트가 창을 다 먹는다):

    [현재 상태]        |  지금 적용 중인 전처리 목록 + 항목별 [해제] / [전체 해제]
    [Options]          |  Bin1 only · Outlier 제거 — 조건을 짤 필요 없는 한 줄 옵션
    [Item Select]      |  Item List (제외 ↔ 표시 2리스트) + 검색
    [Yield 계산]       |  소스별 수율 **분모** (자동 / Gross Die / Test data) + 실시간 수율
    [Rawdata 원본 수정]|  고칠 source 선택 → Excel 왕복 (주황 — 원본을 직접 고치는 유일한 버튼)
    ---------------------------------------------------------------------
                                                     [저장] [닫기]

서버 조회는 **창을 띄운 뒤 스레드**에서 한다(`_HubLoadWorker`) — 생성자에서 동기 GET 을 돌면
데이터가 큰 세션에서 버튼을 누른 뒤 창이 뜨기까지 UI 가 멈춘다.

[저장]은 **화면에 보이는 상태를 저장**한다 — 페이지가 나뉘어도 다이얼로그가 들고 있는 상태는
하나라 부분 저장 함정이 없다(`_save` 참조). 저장하면 서버가 Summary/Yield/CPK/Issue Table/
Distribution/Trim/Map 을 그 기준으로 다시 계산하고, 필터는 세션 DB 에 남아 다음에도 적용된다.

빠른 수정이 만든 셀 패치·조건 규칙도 같은 전처리 spec 에 들어간다. 빠른 수정 화면은 현재
비활성이지만(아래 페이지 등록부 주석), 이미 저장된 셀 패치·규칙은 [현재 상태] 에서 그대로
보이고 해제할 수 있다.

Item Select / Outlier / Options / Yield 계산은 원본 parquet 을 건드리지 않으므로 비우고
저장하면 원상복구된다 — 그래서 Excel 편집(원본 대상, 되돌릴 수 없음)과 달리 확인창이 없다.
"""
from __future__ import annotations

from urllib.parse import quote

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# 허브가 돌려주는 사용자 선택 — honey_main 이 이 값으로 다음 동작을 정한다.
ACTION_EXCEL = "excel"
ACTION_QUICK = "quick"

_TIMEOUT = (10, 60)
_ROW_BTN_W = 170

# 수율 분모 자동 판정 사유 (서버 yield_tab.auto_basis 의 reason 코드) — 사람이 읽는 문구.
_YIELD_REASON = {
    "no_gross": "기준정보에 Gross die 없음",
    "gross_lt_tested": "Gross die < Test die — 수율 100% 초과",
    "tested_short": "Test die 가 100 개 이상 적음",
}

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


class _HubLoadWorker(QThread):
    """허브가 필요한 서버 조회 3건 — 창을 띄운 뒤 백그라운드로 돈다.

    항목이 수천 개인 세션에서는 raw_data/columns 만으로도 수 초가 걸린다. 생성자에서
    동기 호출하면 [Rawdata edit] 를 누른 뒤 창이 뜨기까지 UI 가 통째로 멈춘다.
    yield_basis 는 실패해도 나머지 화면은 쓸 수 있어야 하므로 따로 감싼다.
    """

    done = pyqtSignal(object, str)      # (info dict, error)

    def __init__(self, base, session_id, parent=None):
        super().__init__(parent)
        self.base, self.session_id = base, session_id

    def _get(self, path):
        import requests

        r = requests.get(
            f"{self.base}/pe/report/session/{self.session_id}/web_report/{path}",
            headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json() or {}

    def run(self):
        try:
            info = {"preprocess": self._get("preprocess"),
                    "columns": self._get("raw_data/columns")}
        except Exception as exc:                     # noqa: BLE001 (UI 로 그대로 전달)
            self.done.emit(None, str(exc))
            return
        try:
            info["yield"] = self._get("yield_basis")
        except Exception:
            info["yield"] = {}
        self.done.emit(info, "")


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
        self.excel_indices = None     # Excel 왕복에 넘길 source idx (None = 전체)
        self._items = []
        self._sources = []            # source 이름 (원본 idx 순서)
        self._spec = {}
        self._edits = []              # 빠른 수정 셀 패치 (여기선 표시·해제만)
        self._rules = []              # 조건 일괄 규칙 (여기선 표시·해제만)
        self._gross_die = None        # 세션 제품 기준정보 Gross Die (없으면 None)
        self._yield_rows = []         # 소스별 수율 분모 판정 (서버 yield_basis 응답)
        self._basis_combos = []       # Yield 계산 표의 기준 콤보 (행 순서 = _yield_rows)
        self._loader = None

        self.setWindowTitle("Rawdata")
        self.resize(860, 600)

        self.pages = QStackedWidget()
        self.nav_buttons = []
        nav = QVBoxLayout()
        nav.setSpacing(6)

        # ── 페이지 0: 현재 상태 (기본) ───────────────────────────────────────
        self._add_page(nav, "현재 상태", self._build_state_page(),
                       "지금 이 리포트에 적용 중인 전처리 목록 — 여기서 개별/전체 해제")

        # ── 페이지 1: Options (자주 쓰는 한 줄 옵션 — Outlier 제거 포함) ─────
        self._add_page(nav, "Options", self._build_options_page(),
                       "Bin1 only · Outlier 제거 — 조건을 짜지 않고 켜고 끄는 옵션")

        # ── 페이지 2: Item Select ────────────────────────────────────────────
        self.item_list = _ItemListWidget()
        self._add_page(nav, "Item Select", self.item_list,
                       "선택한 항목만 남기고 저장 (원본은 그대로, 언제든 되돌릴 수 있음)")

        # ── 페이지 3: Yield 계산 (소스별 수율 분모) ──────────────────────────
        self._add_page(nav, "Yield 계산", self._build_yield_page(),
                       "소스별 수율 분모 — 자동 / Gross Die / Test data 개수")

        # ── [빠른 수정] 페이지는 잠시 비활성 (2026-07-28, 사용자 요청) ───────
        # 되살릴 때: 아래 3줄 주석을 풀면 된다 (다이얼로그·호출부는 그대로 살아 있다 —
        # honey_main._run_quick_edit / honey_ui/rawdata_quick_dialog.py).
        # 이미 저장된 셀 패치·조건 규칙은 계속 적용되고 [현재 상태] 에서 해제할 수 있다.
        #   quick_page = self._build_quick_page()
        #   self._add_page(nav, "빠른 수정", quick_page,
        #                  "Excel 없이 표·조건으로 고칩니다 (원본 불변, 되돌릴 수 있음)")

        # ── 페이지 4: Rawdata 원본 수정 (Excel) ──────────────────────────────
        excel_page = QWidget()
        excel_layout = QVBoxLayout(excel_page)
        excel_layout.addWidget(QLabel(
            "source 만 Excel 로 받아 직접 편집한 뒤 서버에 반영합니다 — "
            "**원본을 실제로 바꿉니다.**"))
        excel_layout.addWidget(QLabel("Excel 로 열 Source (체크한 것만)"))
        self.list_excel_source = QListWidget()
        # 체크박스만으로는 선택 여부가 눈에 안 띈다는 피드백 (2026-07-28) — 체크 표시를
        # 키우고, 체크된 행은 색·굵기로도 구분한다 (_style_excel_item).
        self.list_excel_source.setStyleSheet(
            "QListWidget::item { padding: 5px 6px; }"
            "QListWidget::indicator { width: 18px; height: 18px; }")
        self.list_excel_source.setMinimumHeight(260)
        self.list_excel_source.itemChanged.connect(self._style_excel_item)
        excel_layout.addWidget(self.list_excel_source, 1)
        src_row = QHBoxLayout()
        btn_src_all = QPushButton("전체 선택")
        btn_src_none = QPushButton("전체 해제")
        btn_src_all.clicked.connect(lambda: self._check_excel_sources(True))
        btn_src_none.clicked.connect(lambda: self._check_excel_sources(False))
        src_row.addWidget(btn_src_all)
        src_row.addWidget(btn_src_none)
        src_row.addStretch(1)
        excel_layout.addLayout(src_row)
        self.btn_excel = QPushButton("Rawdata 원본 수정 (Excel 열기)")
        self.btn_excel.setStyleSheet(_DANGER_BTN_QSS)
        self.btn_excel.setMinimumHeight(38)
        self.btn_excel.clicked.connect(self._start_excel)
        excel_layout.addWidget(self.btn_excel)
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
        layout.addWidget(self.lbl_state)
        layout.addWidget(buttons)

        self.nav_buttons[0].setChecked(True)
        self._load()   # 창은 바로 뜨고 서버 조회는 스레드에서 (_HubLoadWorker)

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

    def _build_options_page(self):
        """조건을 짤 필요 없는 한 줄 옵션 — 내부적으로는 조건 규칙(rules)을 만든다.

        빠른 수정 다이얼로그(표·필터가 있는 큰 화면)를 열지 않아도 켜고 끌 수 있어야 하는
        것들이라 허브에 둔다. 만들어진 규칙은 [현재 상태] 페이지에 그대로 나타난다."""
        page = QWidget()
        layout = QVBoxLayout(page)

        self.chk_bin1_only = QCheckBox("Bin1 only — Pass(BIN 1) die 만 남기기")
        self.chk_bin1_only.setToolTip("BIN 이 1 이 아닌 die 를 리포트에서 제외합니다.")
        self.chk_bin1_only.toggled.connect(self._toggle_bin1_only)
        layout.addWidget(self.chk_bin1_only)
        layout.addWidget(QLabel(
            "    fail die 를 빼고 양품만으로 분포·CPK 를 봅니다."))
        layout.addSpacing(14)

        # Outlier 제거 — 종전 별도 페이지에서 Options 로 이동 (2026-07-28, 사용자 요청).
        self.edit_k = QLineEdit()
        self.edit_k.setPlaceholderText("예: 50")
        self.edit_k.setFixedWidth(90)
        self.edit_k.returnPressed.connect(self._save)
        outlier_row = QHBoxLayout()
        outlier_row.addWidget(QLabel("Outlier 제거 — mean ±"))
        outlier_row.addWidget(self.edit_k)
        outlier_row.addWidget(QLabel("× stdev 밖의 측정값 제거 (비우면 해제)"))
        outlier_row.addStretch(1)
        layout.addLayout(outlier_row)
        layout.addWidget(QLabel(
            "    항목별로 평균 ± (입력값)×표준편차 밖의 측정값만 결측 처리합니다. "
            "BIN·좌표·die 는 손대지 않으므로\n    수율·Wafer Map 은 그대로이고, "
            "CPK/Distribution 의 n·평균·σ 만 달라집니다."))
        layout.addSpacing(14)

        # DUT 제외 — source 를 골라 특정 DUT 의 die 를 통째로 뺀다 (2026-08-11 요청).
        # 조건 규칙(where.source + DUT in [...] → exclude_rows)을 만드는 한 줄 옵션이라
        # 빠른 수정 화면을 열지 않아도 되고, [현재 상태] 에서 그대로 해제할 수 있다.
        self.cmb_dut_source = QComboBox()
        self.cmb_dut_source.setMinimumWidth(200)
        self.edit_dut = QLineEdit()
        self.edit_dut.setPlaceholderText("예: 3  또는  3,4")
        self.edit_dut.setFixedWidth(120)
        self.edit_dut.returnPressed.connect(self._add_dut_exclude)
        btn_dut = QPushButton("추가")
        btn_dut.clicked.connect(self._add_dut_exclude)
        dut_row = QHBoxLayout()
        dut_row.addWidget(QLabel("DUT 제외 —"))
        dut_row.addWidget(self.cmb_dut_source)
        dut_row.addWidget(QLabel("의 DUT"))
        dut_row.addWidget(self.edit_dut)
        dut_row.addWidget(btn_dut)
        dut_row.addStretch(1)
        layout.addLayout(dut_row)
        layout.addWidget(QLabel(
            "    고른 source 에서 그 DUT 의 die 를 통째로 뺍니다 (수율 분자·분모, Wafer Map, "
            "Distribution 전부 반영).\n    원본은 그대로이고 [현재 상태] 에서 언제든 해제할 수 "
            "있습니다."))
        layout.addSpacing(14)

        # [Spec Out 빈값] 은 잠시 비활성 (2026-07-28, 사용자 요청). 위젯과 _add_spec_out_option
        # 은 그대로 두고 **레이아웃에만 붙이지 않는다** — 되살릴 때 아래 3줄만 복구하면 된다.
        # 이미 저장된 spec_out 규칙은 계속 적용되고 [현재 상태] 에서 해제할 수 있다.
        self.cmb_specout = QComboBox()      # _sync_options 가 항목을 채운다 (화면엔 없음)
        self.cmb_specout.setMinimumWidth(240)
        #   layout.addWidget(QLabel("Spec Out 빈값 — 규격(LOLIM~HILIM) 밖 측정값을 결측 처리"))
        #   spec_row = ... (self.cmb_specout + [추가] btn → self._add_spec_out_option)
        #   layout.addWidget(QLabel("    그 항목의 규격 밖 값만 빈칸이 됩니다 ..."))

        layout.addStretch(1)
        layout.addWidget(QLabel("추가한 옵션은 [현재 상태] 에서 확인·해제할 수 있습니다."))
        return page

    def _build_yield_page(self):
        """소스별 수율 **분모** 선택 + 실시간 수율.

        기본은 [자동] 이다 — 제품 기준정보 Gross Die 를 쓰되, 그 값이 그 소스의 측정 die 수
        보다 작거나(그대로 쓰면 수율이 100% 를 넘는다) 100 개 이상 크면 Test data 개수로
        내려간다. 판정 규칙의 정본은 서버(web_report/tabs/yield_tab.resolve_source_basis)이고,
        여기서는 서버가 준 pass/tested/gross 로 **화면 값만** 즉시 다시 계산한다(왕복 없음).
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel(
            "수율(Yield) 의 **분모**를 source 마다 고릅니다. Yield 탭·Issue Table·Summary 가\n"
            "모두 여기서 고른 기준으로 계산됩니다."))
        self.tbl_yield = QTableWidget(0, 5)
        self.tbl_yield.setHorizontalHeaderLabels(
            ["Source", "Test die", "Gross die", "분모 기준", "Yield"])
        self.tbl_yield.verticalHeader().setVisible(False)
        self.tbl_yield.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_yield.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        # Source 를 Stretch 로 두면 남는 폭을 다 먹어 표가 벙벙해진다 — 내용 폭만 쓰고,
        # 남는 폭은 마지막 Yield 열이 가져간다.
        header = self.tbl_yield.horizontalHeader()
        for col in range(0, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tbl_yield, 1)

        row = QHBoxLayout()
        for text, basis in (("전체 자동", ""), ("전체 Gross die", "gross"),
                            ("전체 Test die", "test")):
            btn = QPushButton(text)
            btn.clicked.connect(lambda _=False, b=basis: self._set_all_basis(b))
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)

        # [자동] 판정 사유는 콤보 문구에 넣지 않고 여기서 한 번만 설명한다 (콤보가 길어져
        # 분모 기준 열이 벙벙해지던 문제 — 2026-07-28).
        layout.addWidget(QLabel(
            "[자동] 은 기본으로 Gross die 를 분모로 쓰고, 아래의 경우에는 Test die 로 내려갑니다.\n"
            "  · 기준정보에 Gross die 가 없음\n"
            "  · Gross die < Test die (그대로 쓰면 수율이 100% 를 넘음)\n"
            "  · Test die 가 Gross die 보다 100 개 이상 적음 (대량 미측정)"))

        self.lbl_yield_sum = QLabel("")
        self.lbl_yield_sum.setWordWrap(True)
        layout.addWidget(self.lbl_yield_sum)
        return page

    def _build_quick_page(self):
        """[빠른 수정] 페이지 — 현재 비활성(생성자의 등록부 주석 참조)."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel(
            "표에서 필요한 행만 조회해 값을 고치거나, 조건으로 한 번에 수정합니다.\n"
            "· 셀 직접 수정 / 붙여넣기 / 선택 영역 값 지정·빈값·오프셋·배율 / 찾아 바꾸기\n"
            "· 조건 일괄 수정 (예: DUT 3 의 VREF > 4.5 인 die 를 빈값으로)\n"
            "· 저장 전에 수율·CPK 변화를 미리 봅니다\n\n"
            "Excel 을 열지 않고 원본도 바꾸지 않습니다 — 언제든 되돌릴 수 있습니다."))
        btn_quick_go = QPushButton("빠른 수정 열기")
        btn_quick_go.setMinimumHeight(38)
        btn_quick_go.clicked.connect(self._start_quick)
        layout.addWidget(btn_quick_go)
        layout.addStretch(1)
        return page

    # ── Options 페이지 동작 ──────────────────────────────────────────────────
    @staticmethod
    def _bin1_only_rule():
        return {"where": {"conds": [{"field": "BIN", "op": "not_in", "values": ["1"]}]},
                "action": {"op": "exclude_rows"}}

    def _find_rule(self, rule):
        """같은 뜻의 규칙이 이미 있으면 그 위치, 없으면 -1 (정규형끼리 비교)."""
        from web_report import preprocess

        target = preprocess.normalize({"rules": [rule]}).get("rules") or []
        if not target:
            return -1
        for idx, existing in enumerate(self._rules):
            if (preprocess.normalize({"rules": [existing]}).get("rules") or []) == target:
                return idx
        return -1

    def _toggle_bin1_only(self, checked):
        """체크박스 ↔ 규칙 목록 동기화. _sync_options 가 부를 땐 신호를 막아 재진입이 없다."""
        rule = self._bin1_only_rule()
        idx = self._find_rule(rule)
        if checked and idx < 0:
            self._rules.append(rule)
        elif not checked and idx >= 0:
            self._rules.pop(idx)
        else:
            return
        self._render_state_list()
        self._refresh_state()

    def _add_spec_out_option(self):
        item = self.cmb_specout.currentData()
        if not item:
            QMessageBox.warning(self, "Spec Out", "항목을 고르세요.")
            return
        rule = {"where": {"conds": [{"field": "item", "item": item, "op": "spec_out"}]},
                "action": {"op": "clear", "target": item}}
        if self._find_rule(rule) >= 0:
            QMessageBox.information(self, "Spec Out", f"'{item}' 은 이미 적용 중입니다.")
            return
        self._rules.append(rule)
        self._render_state_list()
        self._refresh_state()
        QMessageBox.information(
            self, "Spec Out",
            f"'{item}' 규격 밖 값을 빈값 처리하는 옵션을 추가했습니다.\n"
            "아래 [저장] 을 눌러야 서버에 반영됩니다.")

    def _add_dut_exclude(self):
        """[DUT 제외] 한 줄 옵션 → 조건 규칙 1건.

        source 를 (전체) 로 두면 where.source 를 넣지 않는다 — preprocess.normalize 규약상
        source 가 없으면 모든 소스에 걸린다.
        """
        values = [v.strip() for v in (self.edit_dut.text() or "").replace(" ", ",").split(",")]
        values = [v for v in values if v]
        if not values:
            QMessageBox.warning(self, "DUT 제외", "뺄 DUT 번호를 입력하세요 (예: 3 또는 3,4).")
            return
        source = self.cmb_dut_source.currentData() or ""
        where = {"conds": [{"field": "DUT", "op": "in", "values": values}]}
        if source:
            where["source"] = source
        rule = {"where": where, "action": {"op": "exclude_rows"}}
        if self._find_rule(rule) >= 0:
            QMessageBox.information(self, "DUT 제외", "같은 조건이 이미 적용 중입니다.")
            return
        self._rules.append(rule)
        self.edit_dut.clear()
        self._render_state_list()
        self._refresh_state()
        from web_report import preprocess

        QMessageBox.information(
            self, "DUT 제외",
            f"{preprocess.describe_rule(rule)}\n\n아래 [저장] 을 눌러야 서버에 반영됩니다.")

    def _sync_options(self):
        """저장된 규칙 → Options 화면 상태 (항목·source 콤보 채우기 + Bin1 only 체크)."""
        current_src = self.cmb_dut_source.currentData()
        self.cmb_dut_source.clear()
        self.cmb_dut_source.addItem("(전체 source)", "")
        for name in self._sources:
            self.cmb_dut_source.addItem(name, name)
        if current_src:
            pos = self.cmb_dut_source.findData(current_src)
            if pos >= 0:
                self.cmb_dut_source.setCurrentIndex(pos)
        current = self.cmb_specout.currentData()
        self.cmb_specout.clear()
        self.cmb_specout.addItem("(선택)", "")
        for item in self._items:
            name = str(item.get("name") or "")
            if name:
                self.cmb_specout.addItem(name, name)
        if current:
            pos = self.cmb_specout.findData(current)
            if pos >= 0:
                self.cmb_specout.setCurrentIndex(pos)
        checked = self._find_rule(self._bin1_only_rule()) >= 0
        if self.chk_bin1_only.isChecked() != checked:
            self.chk_bin1_only.blockSignals(True)
            self.chk_bin1_only.setChecked(checked)
            self.chk_bin1_only.blockSignals(False)

    # ── 서버 통신 ────────────────────────────────────────────────────────────
    def _load(self):
        """현재 항목 목록 + 저장된 필터·수율 기준을 **스레드로** 읽어 화면을 채운다."""
        self._set_busy(True, "세션 정보를 불러오는 중...")
        self._loader = _HubLoadWorker(self.base, self.session_id, self)
        self._loader.done.connect(self._on_loaded)
        self._loader.start()

    def _on_loaded(self, info, error):
        """_HubLoadWorker 결과 반영 (메인스레드)."""
        self._set_busy(False)
        if error or info is None:
            QMessageBox.warning(self, "Rawdata", f"세션 정보를 가져오지 못했습니다.\n{error}")
            self._items, self._sources, self._spec = [], [], {}
        else:
            self._spec = (info.get("preprocess") or {}).get("spec") or {}
            columns = info.get("columns") or {}
            self._items = columns.get("items") or []
            self._sources = [str(s) for s in (columns.get("sources") or [])]
            self._set_yield_info(info.get("yield") or info.get("preprocess") or {})

        self.item_list.populate(self._items, self._spec.get("exclude_items") or [])
        self._set_k((self._spec.get("outlier") or {}).get("k"))
        # 빠른 수정이 만든 셀 패치·규칙 — 셀 패치는 목록 표시와 해제만, 규칙은 Options
        # 페이지에서 켜고 끌 수도 있다.
        self._edits = list(self._spec.get("edits") or [])
        self._rules = list(self._spec.get("rules") or [])
        self._populate_excel_sources()
        self._render_state_list()
        self._sync_options()
        self._refresh_state()

    def _set_busy(self, busy, message=""):
        """로드 중에는 저장·Excel 진입을 막는다 (화면이 빈 상태로 저장되는 사고 방지)."""
        for widget in (self.btn_save, self.btn_excel):
            widget.setEnabled(not busy)
        if message:
            self.lbl_state.setText(message)

    # ── Yield 계산 페이지 ────────────────────────────────────────────────────
    def _set_yield_info(self, info):
        """서버 yield_basis 응답 → 소스별 표. gross_die 는 안내 문구에도 쓴다."""
        self._gross_die = (info or {}).get("gross_die")
        self._yield_rows = [dict(r) for r in ((info or {}).get("sources") or [])]
        self._render_yield_table()

    def _render_yield_table(self):
        """소스 1행 = [이름 / test die / gross die / 기준 콤보 / 수율]."""
        self._basis_combos = []
        self.tbl_yield.setRowCount(len(self._yield_rows))
        for row, entry in enumerate(self._yield_rows):
            self.tbl_yield.setItem(row, 0, QTableWidgetItem(str(entry.get("source") or "")))
            self.tbl_yield.setItem(row, 1, QTableWidgetItem(f"{entry.get('tested') or 0:,}"))
            gross = entry.get("gross")
            self.tbl_yield.setItem(row, 2, QTableWidgetItem(f"{gross:,}" if gross else "—"))

            combo = QComboBox()
            combo.addItem(self._auto_label(entry), "")
            combo.addItem("Gross die", "gross")
            combo.addItem("Test die", "test")
            reason = _YIELD_REASON.get(entry.get("reason") or "", "")
            if reason:
                combo.setToolTip(f"자동 판정 사유: {reason}")
            if not entry.get("gross_allowed"):
                # 규칙: 수율은 100% 를 넘을 수 없다 — Gross 를 고를 수 없는 소스는 막는다.
                item = combo.model().item(1)
                item.setEnabled(False)
                item.setToolTip(_YIELD_REASON.get(entry.get("reason") or "", "")
                                or "Gross die 를 분모로 쓸 수 없습니다.")
            override = str(entry.get("override") or "")
            if override == "gross" and not entry.get("gross_allowed"):
                override = ""      # 고를 수 없는 선택 — [자동] 로 표시(서버도 test 로 내린다)
            pos = combo.findData(override)
            combo.setCurrentIndex(pos if pos >= 0 else 0)
            combo.currentIndexChanged.connect(self._on_basis_changed)
            self._basis_combos.append(combo)
            self.tbl_yield.setCellWidget(row, 3, combo)
            self.tbl_yield.setItem(row, 4, QTableWidgetItem(""))
        self._refresh_yield_values()

    @staticmethod
    def _auto_label(entry):
        """사유는 콤보에 넣지 않는다 — 열이 벙벙해진다. 사유는 표 아래 설명 + 툴팁."""
        basis = "Gross die" if entry.get("auto") == "gross" else "Test die"
        return f"자동 → {basis}"

    def _basis_of(self, row_idx):
        """그 행에 지금 적용될 기준 — 콤보가 [자동] 이면 서버가 준 auto 판정."""
        entry = self._yield_rows[row_idx]
        combo = self._basis_combos[row_idx]
        basis = str(combo.currentData() or "") or str(entry.get("auto") or "test")
        # 서버와 같은 안전장치: Gross 를 못 쓰는 소스는 어떤 선택이든 test 로 내린다.
        return "test" if basis == "gross" and not entry.get("gross_allowed") else basis

    def _on_basis_changed(self):
        self._refresh_yield_values()
        self._refresh_state()

    def _refresh_yield_values(self):
        """분모 선택 → 소스별 수율·전체 수율을 즉시 다시 계산한다 (서버 왕복 없음)."""
        passed = total = 0
        for row, entry in enumerate(self._yield_rows):
            basis = self._basis_of(row)
            denom = int(entry.get("gross") or 0) if basis == "gross" \
                else int(entry.get("tested") or 0)
            n_pass = int(entry.get("pass") or 0)
            pct = (n_pass / denom * 100.0) if denom else 0.0
            # 고를 수 있는 기준일 때만 "다른 기준" 값을 병기한다 — 막힌 Gross 값을 보여주면
            # 고르면 그 수율이 될 것처럼 읽힌다.
            other = (int(entry.get("tested") or 0) if basis == "gross"
                     else (int(entry.get("gross") or 0) if entry.get("gross_allowed") else 0))
            other_txt = (f"  (다른 기준 {n_pass / other * 100.0:.2f}%)" if other else "")
            cell = QTableWidgetItem(f"{pct:.2f}%   {n_pass:,} / {denom:,}{other_txt}")
            self.tbl_yield.setItem(row, 4, cell)
            passed += n_pass
            total += denom
        if not self._yield_rows:
            self.lbl_yield_sum.setText(
                "소스 정보를 읽지 못했습니다 — 수율 분모는 서버 기본값(자동)으로 계산됩니다.")
            return
        pooled = (passed / total * 100.0) if total else 0.0
        self.lbl_yield_sum.setText(
            f"전체 Yield {pooled:.2f}%  ({passed:,} / {total:,})   ·   "
            f"Gross die 기준 {sum(1 for i in range(len(self._yield_rows)) if self._basis_of(i) == 'gross')}"
            f" / Test die 기준 {sum(1 for i in range(len(self._yield_rows)) if self._basis_of(i) == 'test')}"
            "   ·   [저장] 을 눌러야 리포트에 반영됩니다.")

    def _set_all_basis(self, basis):
        for row, combo in enumerate(self._basis_combos):
            if basis == "gross" and not self._yield_rows[row].get("gross_allowed"):
                continue                      # 못 쓰는 소스는 [자동] 그대로 둔다
            pos = combo.findData(basis)
            if pos >= 0:
                combo.setCurrentIndex(pos)

    def _basis_overrides(self):
        """저장 payload 의 sources — [자동] 인 소스는 넣지 않는다(= 서버 자동 판정)."""
        out = {}
        for row, entry in enumerate(self._yield_rows):
            chosen = str(self._basis_combos[row].currentData() or "")
            if chosen:
                out[str(entry.get("source") or "")] = chosen
        return out

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
        parts.append(self._basis_summary())
        self.lbl_state.setText(" · ".join(parts))

    def _basis_summary(self):
        """하단 한 줄 요약용 수율 분모 상태."""
        if not self._yield_rows:
            return (f"Yield 분모 = Gross Die {self._gross_die}" if self._gross_die
                    else "Yield 분모 = 자동")
        n_gross = sum(1 for i in range(len(self._yield_rows)) if self._basis_of(i) == "gross")
        if n_gross == len(self._yield_rows):
            return f"Yield 분모 = Gross die {self._gross_die}"
        if n_gross == 0:
            return "Yield 분모 = Test die (소스별 측정 die 수)"
        return f"Yield 분모 = 소스별 (Gross {n_gross} / Test {len(self._yield_rows) - n_gross})"

    # ── Excel 원본 수정 source 선택 ──────────────────────────────────────────
    def _populate_excel_sources(self):
        """Excel 로 열 source 체크리스트 — 기본은 전체 선택(종전 동작과 같음)."""
        self.list_excel_source.clear()
        for name in self._sources:
            it = QListWidgetItem(str(name))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.list_excel_source.addItem(it)
            self._style_excel_item(it)

    def _style_excel_item(self, it):
        """체크 여부를 체크박스 외에 색·굵기로도 보여준다 — 체크 [선택됨] / 미체크 회색.

        setFont/setBackground 도 itemChanged 를 다시 쏘므로 스타일링 동안 신호를 막는다."""
        checked = it.checkState() == Qt.CheckState.Checked
        self.list_excel_source.blockSignals(True)
        try:
            font = it.font()
            font.setBold(checked)
            it.setFont(font)
            base = str(it.text()).replace("   [선택됨]", "")
            it.setText(base + ("   [선택됨]" if checked else ""))
            it.setBackground(QBrush(QColor("#dcfce7")) if checked else QBrush())
            it.setForeground(QBrush(QColor("#166534")) if checked
                             else QBrush(QColor("#9ca3af")))
        finally:
            self.list_excel_source.blockSignals(False)

    def _check_excel_sources(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.list_excel_source.count()):
            self.list_excel_source.item(i).setCheckState(state)

    def _checked_excel_indices(self):
        return [i for i in range(self.list_excel_source.count())
                if self.list_excel_source.item(i).checkState() == Qt.CheckState.Checked]

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
        self._sync_options()          # Options 의 Bin1 only 체크도 함께 풀린다
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
        self._sync_options()
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
        # yield_basis: mode 는 항상 auto 로 두고 **소스별 선택만** 명시한다 — [자동] 인
        # 소스는 서버가 매번 규칙(Gross die 우선 + 100% 초과·대량 미측정 회피)으로 정한다.
        spec = {"exclude_items": self.item_list.excluded_items(),
                "yield_basis": {"mode": "auto", "sources": self._basis_overrides()},
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
        # 수율 표는 다시 그리지 않는다 — 화면 선택이 곧 방금 저장한 값이고, POST 응답에는
        # 표를 채울 수치(pass/tested)가 없다. gross_die 안내값만 최신으로 맞춘다.
        self._gross_die = result.get("gross_die", self._gross_die)
        self._render_state_list()
        self._sync_options()
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
        나중에 알면 "고쳐둔 게 사라졌다" 가 되므로 들어가기 전에 확인받는다.

        체크한 source 만 Excel 로 연다 — 나머지는 내려받지도 않고 서버 원본 그대로 남는다."""
        indices = self._checked_excel_indices()
        if not indices:
            QMessageBox.warning(self, "Rawdata 원본 수정", "Source 를 1개 이상 선택하세요.")
            return
        if self._edits and QMessageBox.question(
                self, "Rawdata 원본 수정",
                f"빠른 수정으로 저장해 둔 셀 {len(self._edits)}건이 있습니다.\n\n"
                "Excel 편집을 서버에 반영하면 행 위치가 달라질 수 있어 그 셀 수정은 "
                "자동으로 해제됩니다 (조건 일괄 규칙은 유지됩니다).\n\n계속할까요?"
        ) != QMessageBox.StandardButton.Yes:
            return
        # 전부 선택이면 None — 종전(전체 교체) 경로를 그대로 타게 한다.
        self.excel_indices = (None if len(indices) == self.list_excel_source.count()
                              else indices)
        self.action = ACTION_EXCEL
        self.accept()

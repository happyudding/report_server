"""RawdataQuickDialog — Rawdata 빠른 수정 (Excel 없이, 원본을 바꾸지 않고).

Excel 왕복은 전 source·전 항목을 xlsx 로 써서 Excel 로 여는 작업이라 데이터가 크면
그것만으로 수 분이 걸린다. 실제 분석 업무의 대부분은 "조건 걸어 몇 군데 고치고 수율·CPK
변화를 본다"의 빠른 반복이라, 그 반복을 여기서 끝낸다.

**원본 parquet 은 건드리지 않는다.** 고친 내용은 전처리 spec(서버 web_report/preprocess.py,
세션 편집 DB kind='preprocess')의 두 키로 저장된다:

  - edits : 표에서 직접 고친 셀 (source, 행 위치, 컬럼, 값)
  - rules : 조건 일괄 수정 (조건 AND + 동작 set/clear/offset/scale/exclude_rows)

되돌리기는 그 키를 비우면 끝이고, 원본이 그대로라 export zip 의 ETag 캐시가 계속 살아
있어 두 번째부터는 서버가 304 만 응답한다(서버 부하 ≈ 0).

값 검증(rawvalues)·조건 판정(preprocess.match_rows)·규칙 적용(preprocess.apply_tables)은
전부 **서버와 같은 모듈**을 그대로 돌린다 — 화면에서 본 "대상 N행"과 저장 후 실제로 바뀌는
행이 어긋나지 않는 것이 이 다이얼로그의 핵심 계약이다.

화면 흐름:
  1) source 선택 (체크박스) — 체크한 source 만 디코드한다(여는 속도가 여기서 갈린다)
  2) 편집 화면 — 필터 조회 → 표에서 셀 수정 / 선택 영역 일괄 동작 / 조건 규칙 추가
                 → 수율·CPK 미리보기 → [저장]
"""
from __future__ import annotations

from urllib.parse import quote

from PyQt6.QtCore import QAbstractTableModel, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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
    QSplitter,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from web_report import preprocess, rawvalues
from web_report.honeyform import META_COLUMNS

_TIMEOUT = (10, 300)
_MAX_ROWS = 20000          # 표에 올릴 최대 행수 (서버 raw_data 조회 상한과 같은 값)
_MAX_ITEM_COLS = 60        # 표에 올릴 최대 item 컬럼수 (같은 이유)
_EDITED_BG = QColor("#fff3cd")
_META_SET = set(META_COLUMNS)
_SIDE_MIN_W = 340          # 우측(항목/규칙/미리보기) 패널 최소 폭

# 이 창은 표·필터·동작 버튼이 한 화면에 모여 있어 기본 폰트로는 글자만 커 보인다.
# 창 전체를 한 단계 작게 깔고, 설명 라벨은 더 작고 흐리게 둔다.
_DIALOG_QSS = """
QDialog, QLabel, QPushButton, QToolButton, QComboBox, QLineEdit,
QListWidget, QTableView, QGroupBox { font-size: 11px; }
QGroupBox { font-weight: 600; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QLabel[hint="1"] { color: #6b7280; font-size: 10px; }
QToolButton#sectionHeader { font-weight: 600; border: none; padding: 2px 0; }
"""

# 표시용 컬럼 라벨 — 웹 Raw Data 표와 같은 표기(X/Y/TNO).
_META_LABELS = {"XPOS": "X", "YPOS": "Y", "FAILTNO": "TNO"}

_CMP_LABELS = [(">", ">"), (">=", "≥"), ("<", "<"), ("<=", "≤"), ("spec_out", "규격 밖")]


def _headers(extra=None):
    """서버 신원 토큰 (rawdata_hub_dialog._headers 와 동일 규칙)."""
    try:
        import client_identity
        user = client_identity.collect().get("user", "")
    except Exception:
        user = ""
    headers = {"User-Agent": f"python-requests HoneyUser/{quote(user, safe='')}"} if user else {}
    headers.update(extra or {})
    return headers


def _fmt(value) -> str:
    from web_report.tabs.common import fmt_type

    return fmt_type(value)


def _fmt_item(value) -> str:
    """측정값 표시 — 결측은 빈칸, 정수는 정수로 (표가 1.0 으로 도배되지 않게)."""
    from web_report.tabs.common import round_num

    num = round_num(value)
    if num is None:
        return ""
    return str(int(num)) if float(num).is_integer() else f"{num:g}"


class _LoadWorker(QThread):
    """rawdata 다운로드·디코드 (수 초~수십 초) — UI 스레드를 막지 않는다."""

    progress = pyqtSignal(str)
    done = pyqtSignal(object, str)      # (tables, error)

    def __init__(self, base, session_id, indices, parent=None):
        super().__init__(parent)
        self.base, self.session_id, self.indices = base, session_id, indices

    def run(self):
        try:
            from excel_edit.excel_session import fetch_rawdata_tables

            tables, _manifest, _names = fetch_rawdata_tables(
                self.base, self.session_id, self.indices,
                status_cb=lambda msg: self.progress.emit(msg))
            self.done.emit(tables, "")
        except Exception as exc:                     # noqa: BLE001 (UI 로 그대로 전달)
            self.done.emit(None, str(exc))


class _Section(QWidget):
    """접었다 펼 수 있는 구역 — 제목 줄을 누르면 본문이 접힌다.

    조회 조건·수정 동작은 한 번 정하고 나면 계속 볼 필요가 없는데, 펼친 채로 두면
    정작 봐야 할 표가 눌린다. 접힘 상태에서는 제목 옆에 요약(예: 대상 행수)만 남긴다.
    """

    def __init__(self, title, parent=None, expanded=True):
        super().__init__(parent)
        self.header = QToolButton()
        self.header.setObjectName("sectionHeader")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.header.clicked.connect(self._toggle)
        self.summary = QLabel("")
        self.summary.setProperty("hint", "1")

        self.body = QWidget()
        self.body.setVisible(expanded)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.header)
        head.addWidget(self.summary, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(head)
        layout.addWidget(self.body)

    def _toggle(self, checked):
        self.body.setVisible(checked)
        self.header.setArrowType(Qt.ArrowType.DownArrow if checked
                                 else Qt.ArrowType.RightArrow)

    def set_summary(self, text):
        self.summary.setText(text)


class _PreviewWorker(QThread):
    """수율·CPK 미리보기 계산 — 항목 수천 개 세션에서 수 초가 걸려 UI 스레드를 막는다."""

    done = pyqtSignal(object, str)      # ((before, after, stats), error)

    def __init__(self, tables, saved_spec, new_spec, parent=None):
        super().__init__(parent)
        self.tables, self.saved_spec, self.new_spec = tables, saved_spec, new_spec

    def run(self):
        try:
            base_tables, _ = preprocess.apply_tables(self.tables, self.saved_spec)
            new_tables, stats = preprocess.apply_tables(self.tables, self.new_spec)
            self.done.emit((_metrics(base_tables), _metrics(new_tables), stats), "")
        except Exception as exc:                     # noqa: BLE001 (UI 로 그대로 전달)
            self.done.emit(None, str(exc))


def _metrics(tables) -> dict:
    """미리보기용 수율(test 기준)·worst CPK — 서버 tabs 계산기를 그대로 쓴다.

    수율 분모는 측정 die 수로 고정한다(Gross Die 기준은 허브 체크박스 소관) — 미리보기의
    목적은 절대값이 아니라 **변화폭**이라 분모를 단순화해도 판단이 흐려지지 않는다.
    """
    from web_report.tabs.common import PASS_BIN, bin_types
    from web_report.tabs.cpk import build_cpk_rows, worst_cpk_by_subject

    dies = passed = 0
    for table in tables:
        bins = bin_types(table)
        dies += len(bins)
        passed += sum(1 for b in bins if b == PASS_BIN)
    items = []
    for table in tables:
        for name in table.item_columns:
            if name not in items:
                items.append(name)
    worst_map = sorted(worst_cpk_by_subject(build_cpk_rows(tables, items)).items(),
                       key=lambda kv: kv[1])
    return {"dies": dies, "yield": (passed / dies * 100.0) if dies else 0.0,
            "worst": worst_map[0] if worst_map else None, "worst_map": worst_map}


class _RawModel(QAbstractTableModel):
    """조회된 행만 담는 표 모델. 셀 편집은 pending 패치에 쌓이고 원본 프레임은 불변.

    rows 는 (table, row_pos) 목록이다 — row_pos 는 table.data 의 0-base 위치이자
    서버에 보낼 ``row_idx`` 그대로다(원본이 불변이라 안정 식별자).
    """

    def __init__(self, rows, columns, pending, parent=None):
        super().__init__(parent)
        self.rows = rows
        self.columns = columns          # 컬럼명 리스트 (메타 + 선택 item)
        self.pending = pending          # {(source, row_idx, column): value}
        self.last_error = ""            # 마지막 patch 실패 사유 (호출부가 사용자에게 알림)

    def rowCount(self, parent=None):
        return 0 if (parent is not None and parent.isValid()) else len(self.rows)

    def columnCount(self, parent=None):
        return 0 if (parent is not None and parent.isValid()) else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            name = self.columns[section]
            return _META_LABELS.get(name, name)
        table, pos = self.rows[section]
        return str(pos)                 # 원본 행 위치를 그대로 보여준다(패치 키와 동일)

    def _key(self, index):
        table, pos = self.rows[index.row()]
        return (table.source, pos, self.columns[index.column()])

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        table, pos = self.rows[index.row()]
        column = self.columns[index.column()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            patched = self.pending.get((table.source, pos, column))
            if patched is not None:
                return patched
            value = table.data[column].iloc[pos]
            return _fmt(value) if column in _META_SET else _fmt_item(value)
        if role == Qt.ItemDataRole.BackgroundRole:
            if (table.source, pos, column) in self.pending:
                return QBrush(_EDITED_BG)
        return None

    def flags(self, index):
        base = super().flags(index)
        return (base | Qt.ItemFlag.ItemIsEditable) if index.isValid() else base

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        return self.patch(index, value)

    def patch(self, index, value) -> bool:
        """셀 1개에 값을 넣는다. 값 규칙 위반이면 False (호출부가 사유를 알린다)."""
        table, pos = self.rows[index.row()]
        column = self.columns[index.column()]
        is_item = column not in _META_SET
        text = "" if value is None else str(value).strip()
        self.last_error = rawvalues.check_cell_value(column, text, is_item=is_item)
        if self.last_error:
            return False
        self.pending[(table.source, pos, column)] = rawvalues.normalize_cell_value(
            column, text, is_item=is_item)
        self.dataChanged.emit(index, index)
        return True


class RawdataQuickDialog(QDialog):
    """빠른 수정 — 표에서 고치거나 조건으로 일괄 수정하고 전처리 spec 으로 저장한다."""

    def __init__(self, parent, session_id, server_base):
        super().__init__(parent)
        self.session_id = session_id
        self.base = str(server_base).rstrip("/")
        self.changed = False            # 저장했는가 (호출부가 새로고침 판단)

        self._meta = {}                 # raw_data/columns 응답 (items/sources)
        self._tables = []               # 디코드된 HoneyformTable (선택 source 만)
        self._spec = {}                 # 서버에 저장된 현재 전처리 spec
        self._pending = {}              # 미저장 셀 패치 {(source,row_idx,column): value}
        self._rules = []                # 미저장 포함 규칙 목록
        self._model = None
        self._worker = None
        self._preview_worker = None

        self.setWindowTitle("Rawdata 빠른 수정")
        self.resize(1180, 760)

        self._build_ui()
        self._load_meta()

    # ── UI 구성 ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.setStyleSheet(_DIALOG_QSS)
        root = QVBoxLayout(self)

        # (1) source 선택 — 체크한 것만 디코드한다
        self.box_source = QGroupBox("① 고칠 Source 선택 (체크한 것만 불러옵니다)")
        src_layout = QVBoxLayout(self.box_source)
        self.list_source = QListWidget()
        self.list_source.setMaximumHeight(110)
        btn_all = QPushButton("전체 선택")
        btn_none = QPushButton("전체 해제")
        self.btn_load = QPushButton("불러오기")
        btn_all.clicked.connect(lambda: self._check_all(True))
        btn_none.clicked.connect(lambda: self._check_all(False))
        self.btn_load.clicked.connect(self._start_load)
        row = QHBoxLayout()
        row.addWidget(btn_all)
        row.addWidget(btn_none)
        row.addStretch(1)
        row.addWidget(self.btn_load)
        src_layout.addWidget(self.list_source)
        src_layout.addLayout(row)
        root.addWidget(self.box_source)

        # (2) 편집 화면 — 불러오기 전에는 숨긴다
        self.body = QWidget()
        self.body.setVisible(False)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)

        # 조회 조건·수정 동작은 접을 수 있게 — 펼친 채로 두면 정작 봐야 할 표가 눌린다.
        self.sec_filter = _Section("② 조회 조건 — 필요한 행만 표에 올립니다"
                                   " (이 조건이 곧 일괄 수정 조건)")
        self._build_filter_box(self.sec_filter.body)
        body_layout.addWidget(self.sec_filter)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        side = self._build_side_panel()
        side.setMinimumWidth(_SIDE_MIN_W)
        splitter.addWidget(self.table)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([700, _SIDE_MIN_W + 40])
        body_layout.addWidget(splitter, 1)

        self.sec_action = _Section("③ 수정 — 표에서 직접 고치거나, 선택 영역·조회 조건에 일괄 적용")
        self._build_action_box(self.sec_action.body)
        body_layout.addWidget(self.sec_action)
        root.addWidget(self.body, 1)

        # (3) 하단 — 상태 + 저장/닫기
        self.lbl_state = QLabel("")
        self.lbl_state.setWordWrap(True)
        buttons = QDialogButtonBox()
        self.btn_save = QPushButton("저장")
        self.btn_close = QPushButton("닫기")
        buttons.addButton(self.btn_save, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.btn_close, QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_save.clicked.connect(self._save)
        self.btn_close.clicked.connect(self.reject)
        root.addWidget(self.lbl_state)
        root.addWidget(buttons)

    def _build_filter_box(self, box):
        grid = QGridLayout(box)
        grid.setContentsMargins(4, 2, 4, 2)
        grid.setVerticalSpacing(4)
        self.cmb_source = QComboBox()
        self.f_serial = QLineEdit()
        self.f_serial.setPlaceholderText("부분일치")
        self.f_serial.setToolTip("조회는 부분일치입니다. 규칙(일괄 수정)으로 만들면 "
                                 "쉼표로 나눈 값과 정확히 일치하는 die 만 대상이 됩니다.")
        self.f_dut, self.f_shot = QLineEdit(), QLineEdit()
        self.f_bin, self.f_tno = QLineEdit(), QLineEdit()
        self.f_x, self.f_y = QLineEdit(), QLineEdit()
        for w in (self.f_dut, self.f_shot, self.f_bin, self.f_tno, self.f_x, self.f_y):
            w.setPlaceholderText("쉼표로 여러 개")
            w.setMaximumWidth(110)
        pairs = [("Source", self.cmb_source), ("SERIAL", self.f_serial),
                 ("DUT", self.f_dut), ("SHOT", self.f_shot), ("BIN", self.f_bin),
                 ("TNO", self.f_tno), ("X", self.f_x), ("Y", self.f_y)]
        for col, (label, widget) in enumerate(pairs):
            grid.addWidget(QLabel(label), 0, col * 2)
            grid.addWidget(widget, 0, col * 2 + 1)

        self.cmb_item = QComboBox()
        self.cmb_item.setMinimumWidth(220)
        self.cmb_op = QComboBox()
        for op, label in _CMP_LABELS:
            self.cmb_op.addItem(label, op)
        self.f_value = QLineEdit()
        self.f_value.setMaximumWidth(110)
        self.btn_query = QPushButton("조회")
        self.btn_query.clicked.connect(self._run_query)
        self.lbl_count = QLabel("")
        cond = QHBoxLayout()
        cond.addWidget(QLabel("항목 조건"))
        cond.addWidget(self.cmb_item)
        cond.addWidget(self.cmb_op)
        cond.addWidget(self.f_value)
        cond.addWidget(self.btn_query)
        cond.addWidget(self.lbl_count, 1)
        holder = QWidget()
        holder.setLayout(cond)
        grid.addWidget(holder, 1, 0, 1, len(pairs) * 2)

    def _build_side_panel(self):
        """우측 패널 — **항목 목록이 주인공**이라 세로 공간의 대부분을 준다.

        규칙 목록·미리보기는 접을 수 있는 구역으로 둬서, 항목을 고르는 동안에는 접어 두고
        목록을 넓게 볼 수 있게 한다."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        item_box = QGroupBox(f"표에 볼 항목 (최대 {_MAX_ITEM_COLS}개)")
        item_layout = QVBoxLayout(item_box)
        item_layout.setContentsMargins(6, 4, 6, 6)
        self.item_search = QLineEdit()
        self.item_search.setPlaceholderText("항목 검색")
        self.item_search.textChanged.connect(self._render_items)
        self.list_items = QListWidget()
        self.list_items.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list_items.setMinimumHeight(220)
        item_layout.addWidget(self.item_search)
        item_layout.addWidget(self.list_items, 1)
        layout.addWidget(item_box, 1)

        self.sec_rules = _Section("적용 대기")
        rule_layout = QVBoxLayout(self.sec_rules.body)
        rule_layout.setContentsMargins(4, 2, 4, 2)
        self.list_rules = QListWidget()
        self.list_rules.setMaximumHeight(90)
        btn_del_rule = QPushButton("선택 규칙 삭제")
        btn_del_rule.clicked.connect(self._remove_rule)
        btn_clear = QPushButton("전체 비우기")
        btn_clear.clicked.connect(self._clear_pending)
        rrow = QHBoxLayout()
        rrow.addWidget(btn_del_rule)
        rrow.addWidget(btn_clear)
        rule_layout.addWidget(self.list_rules)
        rule_layout.addLayout(rrow)
        layout.addWidget(self.sec_rules)

        self.sec_preview = _Section("미리보기 (저장 전, 로컬 계산)")
        prev_layout = QVBoxLayout(self.sec_preview.body)
        prev_layout.setContentsMargins(4, 2, 4, 2)
        self.lbl_preview = QLabel("변경을 넣고 [다시 계산] 을 누르세요.")
        self.lbl_preview.setWordWrap(True)
        btn_preview = QPushButton("다시 계산")
        btn_preview.clicked.connect(self._run_preview)
        prev_layout.addWidget(self.lbl_preview)
        prev_layout.addWidget(btn_preview)
        layout.addWidget(self.sec_preview)
        return panel

    def _build_action_box(self, box):
        layout = QVBoxLayout(box)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        self.edit_value = QLineEdit()
        self.edit_value.setPlaceholderText("적용할 값 / 오프셋 / 배율")
        self.edit_value.setMaximumWidth(160)
        sel = QHBoxLayout()
        sel.addWidget(QLabel("선택 영역:"))
        sel.addWidget(self.edit_value)
        for label, handler, tip in (
                ("값 지정", lambda: self._apply_selection("set"), "선택한 셀을 같은 값으로"),
                ("빈값", lambda: self._apply_selection("clear"), "선택한 셀을 결측으로"),
                ("+ 오프셋", lambda: self._apply_selection("offset"), "선택한 측정값에 더하기"),
                ("× 배율", lambda: self._apply_selection("scale"), "선택한 측정값에 곱하기")):
            btn = QPushButton(label)
            btn.setToolTip(tip)
            btn.clicked.connect(handler)
            sel.addWidget(btn)
        btn_paste = QPushButton("붙여넣기")
        btn_paste.setToolTip("클립보드의 표(탭 구분)를 선택 위치부터 채웁니다 — Excel 에서 복사")
        btn_paste.clicked.connect(self._paste_clipboard)
        sel.addWidget(btn_paste)
        btn_replace = QPushButton("찾아 바꾸기")
        btn_replace.setToolTip("선택 영역에서 값이 정확히 일치하는 셀만 바꿉니다")
        btn_replace.clicked.connect(self._find_replace)
        sel.addWidget(btn_replace)
        sel.addStretch(1)
        layout.addLayout(sel)

        bulk = QHBoxLayout()
        bulk.addWidget(QLabel("조회 조건 전체:"))
        self.cmb_bulk = QComboBox()
        for label, op in (("die 삭제(제외)", "exclude_rows"), ("측정값 빈값", "clear"),
                          ("측정값 지정", "set"), ("측정값 + 오프셋", "offset"),
                          ("측정값 × 배율", "scale"), ("BIN 지정", "set_bin"),
                          ("TNO 지정", "set_failtno")):
            self.cmb_bulk.addItem(label, op)
        btn_bulk = QPushButton("규칙으로 추가")
        btn_bulk.setToolTip("지금 조회한 조건에 이 동작을 거는 규칙을 만듭니다 (되돌릴 수 있음)")
        btn_bulk.clicked.connect(self._add_rule_from_filter)
        bulk.addWidget(self.cmb_bulk)
        bulk.addWidget(btn_bulk)
        bulk.addStretch(1)
        # Bin1 only · Spec Out 빈값은 조건을 짤 필요가 없어 Rawdata 허브 [Options] 로 옮겼다
        # (이 창을 열지 않고 켜고 끌 수 있어야 하는 옵션이라).
        hint = QLabel("Bin1 only · Spec Out 빈값은 Rawdata 허브 [Options] 에 있습니다.")
        hint.setProperty("hint", "1")
        bulk.addWidget(hint)
        layout.addLayout(bulk)

    # ── 로드 ─────────────────────────────────────────────────────────────────
    def _load_meta(self):
        """항목/소스 목록 + 저장된 전처리 spec (작은 GET 2회)."""
        import requests

        try:
            r = requests.get(
                f"{self.base}/pe/report/session/{self.session_id}/web_report/raw_data/columns",
                headers=_headers(), timeout=_TIMEOUT)
            r.raise_for_status()
            self._meta = r.json() or {}
            r = requests.get(
                f"{self.base}/pe/report/session/{self.session_id}/web_report/preprocess",
                headers=_headers(), timeout=_TIMEOUT)
            r.raise_for_status()
            self._spec = (r.json() or {}).get("spec") or {}
        except Exception as exc:
            QMessageBox.warning(self, "빠른 수정", f"세션 정보를 가져오지 못했습니다.\n{exc}")
            self.btn_load.setEnabled(False)
            return

        self.list_source.clear()
        for name in self._meta.get("sources") or []:
            it = QListWidgetItem(str(name))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.list_source.addItem(it)
        self._rules = list(self._spec.get("rules") or [])
        self._pending = {(e["source"], e["row_idx"], e["column"]): e["value"]
                         for e in self._spec.get("edits") or []}
        self._render_rules()
        self._refresh_state()

    def _check_all(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.list_source.count()):
            self.list_source.item(i).setCheckState(state)

    def _checked_indices(self):
        return [i for i in range(self.list_source.count())
                if self.list_source.item(i).checkState() == Qt.CheckState.Checked]

    def _start_load(self):
        indices = self._checked_indices()
        if not indices:
            QMessageBox.warning(self, "빠른 수정", "Source 를 1개 이상 선택하세요.")
            return
        self.btn_load.setEnabled(False)
        self._worker = _LoadWorker(self.base, self.session_id, indices, self)
        self._worker.progress.connect(lambda msg: self.lbl_state.setText(msg))
        self._worker.done.connect(self._on_loaded)
        self._worker.start()

    def _on_loaded(self, tables, error):
        self.btn_load.setEnabled(True)
        if error or not tables:
            QMessageBox.warning(self, "빠른 수정",
                                f"rawdata 를 불러오지 못했습니다.\n{error}")
            self._refresh_state()
            return
        self._tables = tables
        self.box_source.setMaximumHeight(150)   # 불러온 뒤엔 편집 화면이 주인공
        self.body.setVisible(True)

        self.cmb_source.clear()
        self.cmb_source.addItem("(전체)", "")
        for t in tables:
            self.cmb_source.addItem(t.source, t.source)
        names = []
        for t in tables:
            for name in t.item_columns:
                if name not in names:
                    names.append(name)
        self._item_names = names
        self.cmb_item.clear()
        self.cmb_item.addItem("(사용 안 함)", "")
        for name in names:
            self.cmb_item.addItem(name, name)
        self._render_items()
        self._run_query()
        self._refresh_state()

    # ── 항목 선택 ────────────────────────────────────────────────────────────
    def _render_items(self):
        query = (self.item_search.text() or "").strip().lower()
        checked = self._selected_items()
        self.list_items.clear()
        for name in getattr(self, "_item_names", []):
            if query and query not in name.lower():
                continue
            it = QListWidgetItem(name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if name in checked
                             else Qt.CheckState.Unchecked)
            self.list_items.addItem(it)
        if not checked and self.list_items.count():
            self.list_items.item(0).setCheckState(Qt.CheckState.Checked)

    def _selected_items(self):
        """선택 항목 목록. 화면(검색 결과)에 보이는 항목의 체크 상태만 반영하고,
        검색으로 가려진 선택은 그대로 유지한다."""
        out = getattr(self, "_checked_items", None)
        if out is None:
            out = self._checked_items = []
        for i in range(self.list_items.count()):
            it = self.list_items.item(i)
            checked = it.checkState() == Qt.CheckState.Checked
            if checked and it.text() not in out:
                out.append(it.text())
            elif not checked and it.text() in out:
                out.remove(it.text())
        return out

    # ── 조회 ─────────────────────────────────────────────────────────────────
    def _filter_where(self):
        """필터 입력 → preprocess 조건 묶음(where). 조건이 하나도 없으면 None."""
        conds = []
        for widget, field in ((self.f_serial, "SERIAL"), (self.f_dut, "DUT"),
                              (self.f_shot, "SHOT"), (self.f_bin, "BIN"),
                              (self.f_tno, "FAILTNO"), (self.f_x, "XPOS"),
                              (self.f_y, "YPOS")):
            text = (widget.text() or "").strip()
            if not text:
                continue
            values = [v.strip() for v in text.split(",") if v.strip()]
            if values:
                conds.append({"field": field, "op": "in", "values": values})
        item = self.cmb_item.currentData()
        if item:
            op = self.cmb_op.currentData()
            if op == "spec_out":
                conds.append({"field": "item", "item": item, "op": "spec_out"})
            else:
                try:
                    value = float((self.f_value.text() or "").strip())
                except ValueError:
                    QMessageBox.warning(self, "조회", "항목 조건의 값에 숫자를 입력하세요.")
                    return False
                conds.append({"field": "item", "item": item, "op": op, "value": value})
        if not conds:
            return None
        where = {"conds": conds}
        source = self.cmb_source.currentData()
        if source:
            where["source"] = source
        return where

    def _run_query(self):
        """SERIAL 은 부분일치라 필터 전용 처리 — 나머지는 preprocess.match_rows 로 판정."""
        where = self._filter_where()
        if where is False:
            return
        serial_text = (self.f_serial.text() or "").strip().lower()
        source_only = self.cmb_source.currentData()

        rows, matched = [], 0
        truncated = False
        for table in self._tables:
            if source_only and table.source != source_only:
                continue
            mask = None
            if where:
                # SERIAL 부분일치는 조건 문법(정확 일치)에 없으므로 여기서 따로 건다.
                probe = {k: v for k, v in where.items() if k != "source"}
                probe["conds"] = [c for c in where["conds"] if c["field"] != "SERIAL"]
                if probe["conds"]:
                    mask = preprocess.match_rows(table, probe)
                    if mask is None:
                        continue
            serials = ([str(v).lower() for v in table.data["SERIAL"].tolist()]
                       if serial_text else None)
            for pos in range(len(table.data)):
                if mask is not None and not mask[pos]:
                    continue
                if serials is not None and serial_text not in serials[pos]:
                    continue
                matched += 1
                if len(rows) >= _MAX_ROWS:
                    truncated = True
                    continue
                rows.append((table, pos))

        total = sum(len(t.data) for t in self._tables)
        pct = (matched / total * 100.0) if total else 0.0
        note = f" — 앞 {_MAX_ROWS:,}행만 표시" if truncated else ""
        summary = f"대상 {matched:,}행 / 전체 {total:,}행 ({pct:.1f}%){note}"
        self.lbl_count.setText(summary)
        # 접었을 때도 무엇으로 조회했는지 보이도록 제목 옆에 요약을 남긴다.
        self.sec_filter.set_summary(summary)

        items = self._selected_items()[:_MAX_ITEM_COLS]
        columns = list(META_COLUMNS) + items
        # parent 를 주지 않는다 — 조회할 때마다 새 모델이라 Qt 부모에 매달면 계속 쌓인다.
        self._model = _RawModel(rows, columns, self._pending)
        self.table.setModel(self._model)
        self.table.resizeColumnsToContents()

    # ── 선택 영역 일괄 동작 ──────────────────────────────────────────────────
    def _selected_indexes(self):
        if self._model is None:
            return []
        return self.table.selectionModel().selectedIndexes() \
            if self.table.selectionModel() else []

    def _apply_selection(self, op):
        indexes = self._selected_indexes()
        if not indexes:
            QMessageBox.information(self, "선택 영역", "표에서 셀을 먼저 선택하세요.")
            return
        text = (self.edit_value.text() or "").strip()
        if op in ("offset", "scale"):
            try:
                delta = float(text)
            except ValueError:
                QMessageBox.warning(self, "선택 영역", "숫자를 입력하세요.")
                return
        elif op == "set" and text == "":
            QMessageBox.warning(self, "선택 영역", "적용할 값을 입력하세요.")
            return

        applied, failed = 0, ""
        for index in indexes:
            column = self._model.columns[index.column()]
            if op in ("offset", "scale") and column in _META_SET:
                continue                      # 산술은 측정값에만
            if op in ("offset", "scale"):
                current = self._model.data(index, Qt.ItemDataRole.EditRole)
                try:
                    base = float(current)
                except (TypeError, ValueError):
                    continue                  # 결측은 건너뛴다
                value = f"{base + delta:g}" if op == "offset" else f"{base * delta:g}"
            else:
                value = "" if op == "clear" else text
            if self._model.patch(index, value):
                applied += 1
            elif not failed:
                failed = self._model.last_error
        self._after_change(f"선택 영역 {applied:,}개 셀 수정", failed)

    def _paste_clipboard(self):
        """클립보드 표(탭 구분)를 선택한 셀부터 오른쪽·아래로 채운다 (Excel 복사 붙여넣기)."""
        indexes = self._selected_indexes()
        if not indexes:
            QMessageBox.information(self, "붙여넣기", "붙여넣을 시작 셀을 선택하세요.")
            return
        text = QGuiApplication.clipboard().text()
        if not text.strip():
            QMessageBox.information(self, "붙여넣기", "클립보드가 비어 있습니다.")
            return
        grid = [line.split("\t") for line in text.replace("\r\n", "\n").split("\n") if line]
        top = min(i.row() for i in indexes)
        left = min(i.column() for i in indexes)
        applied, failed = 0, ""
        for dr, line in enumerate(grid):
            for dc, value in enumerate(line):
                index = self._model.index(top + dr, left + dc)
                if not index.isValid():
                    continue
                if self._model.patch(index, value):
                    applied += 1
                elif not failed:
                    failed = self._model.last_error
        self._after_change(f"붙여넣기 {applied:,}개 셀 수정", failed)

    def _find_replace(self):
        indexes = self._selected_indexes()
        if not indexes:
            QMessageBox.information(self, "찾아 바꾸기", "표에서 대상 영역을 선택하세요.")
            return
        from PyQt6.QtWidgets import QInputDialog

        find, ok = QInputDialog.getText(self, "찾아 바꾸기", "찾을 값 (정확히 일치):")
        if not ok:
            return
        repl, ok = QInputDialog.getText(self, "찾아 바꾸기", "바꿀 값:")
        if not ok:
            return
        find = find.strip()
        applied, failed = 0, ""
        for index in indexes:
            current = str(self._model.data(index, Qt.ItemDataRole.EditRole) or "").strip()
            if current != find:
                continue
            if self._model.patch(index, repl.strip()):
                applied += 1
            elif not failed:
                failed = self._model.last_error
        self._after_change(f"찾아 바꾸기 {applied:,}개 셀 수정", failed)

    # ── 조건 일괄 규칙 ───────────────────────────────────────────────────────
    def _add_rule(self, where, action):
        """규칙 추가 — **대상 건수를 먼저 보여주고 확인받는다**.

        건수 판정은 서버 저장 후 실제로 적용되는 것과 같은 함수(preprocess.match_rows)라
        여기 보이는 숫자가 곧 반영될 숫자다. SERIAL 은 조회에서만 부분일치이고 규칙에서는
        정확히 일치하므로, 표에 보이던 행수와 다를 수 있어 더더욱 확인이 필요하다.
        """
        rule = {"where": where, "action": action}
        text = preprocess.describe_rule(rule)
        if not text:
            QMessageBox.warning(self, "일괄 수정", "규칙을 만들 수 없습니다 — 조건·동작을 확인하세요.")
            return
        hits = sum(int(m.sum()) for m in
                   (preprocess.match_rows(t, where) for t in self._tables) if m is not None)
        if not hits:
            QMessageBox.warning(self, "일괄 수정", "조건에 맞는 die 가 없습니다.")
            return
        total = sum(len(t.data) for t in self._tables)
        pct = (hits / total * 100.0) if total else 0.0
        if QMessageBox.question(
                self, "일괄 수정 확인",
                f"{text}\n\n대상 {hits:,}행 / 불러온 {total:,}행 ({pct:.1f}%)\n\n"
                "이 규칙을 추가할까요? (저장 전이며 언제든 지울 수 있습니다)"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._rules.append(rule)
        self._render_rules()
        self._after_change(f"규칙 추가 — 대상 {hits:,}행", "")

    def _add_rule_from_filter(self):
        where = self._filter_where()
        if where is False:
            return
        if not where:
            QMessageBox.warning(self, "일괄 수정",
                                "조회 조건을 1개 이상 입력하세요 (조건 없는 일괄 수정은 막습니다).")
            return
        op = self.cmb_bulk.currentData()
        text = (self.edit_value.text() or "").strip()
        item = self.cmb_item.currentData()
        if op == "exclude_rows":
            action = {"op": "exclude_rows"}
        elif op in ("set_bin", "set_failtno"):
            if not text:
                QMessageBox.warning(self, "일괄 수정", "적용할 값을 입력하세요.")
                return
            action = {"op": "set", "value": text,
                      "target": "BIN" if op == "set_bin" else "FAILTNO"}
        else:
            if not item:
                QMessageBox.warning(self, "일괄 수정", "대상 항목을 '항목 조건' 에서 고르세요.")
                return
            if op == "clear":
                action = {"op": "clear", "target": item}
            else:
                try:
                    action = {"op": op, "target": item,
                              "value": text if op == "set" else float(text)}
                except ValueError:
                    QMessageBox.warning(self, "일괄 수정", "숫자를 입력하세요.")
                    return
        self._add_rule(where, action)

    def _render_rules(self):
        self.list_rules.clear()
        for rule in self._rules:
            self.list_rules.addItem(preprocess.describe_rule(rule) or "(해석 불가 규칙)")

    def _remove_rule(self):
        row = self.list_rules.currentRow()
        if row < 0:
            QMessageBox.information(self, "규칙", "삭제할 규칙을 고르세요.")
            return
        self._rules.pop(row)
        self._render_rules()
        self._after_change("규칙 삭제", "")

    def _clear_pending(self):
        if not (self._pending or self._rules):
            return
        if QMessageBox.question(
                self, "전체 비우기",
                "셀 수정과 규칙을 전부 비웁니다. 계속할까요?\n"
                "(저장을 눌러야 서버에 반영됩니다.)") != QMessageBox.StandardButton.Yes:
            return
        self._pending.clear()
        self._rules.clear()
        self._render_rules()
        self._run_query()
        self._after_change("전체 비움", "")

    # ── 미리보기 ─────────────────────────────────────────────────────────────
    def _current_spec(self):
        """저장할 spec 전체.

        레거시 키(exclude_items/outlier)는 허브 소관이지만 **저장된 값을 그대로 되돌려 보낸다** —
        서버의 레거시 키 저장 규약이 "부재 = 해제"라, 빼고 보내면 허브에서 걸어 둔 항목 제외·
        outlier 가 빠른 수정 저장 한 번에 조용히 풀린다.
        """
        spec = {"edits": [{"source": s, "row_idx": r, "column": c, "value": v}
                          for (s, r, c), v in sorted(self._pending.items())],
                "rules": list(self._rules)}
        for key in ("exclude_items", "outlier"):
            if self._spec.get(key):
                spec[key] = self._spec[key]
        return spec

    def _run_preview(self):
        """수율·worst CPK 변화를 로컬에서 계산 — 서버와 같은 모듈이라 값이 일치한다.

        비교 기준선은 '원본'이 아니라 **지금 서버에 저장된 상태**다 — 사용자가 알고 싶은 것은
        "저장하면 무엇이 달라지나" 이지 "업로드 시점 대비" 가 아니다.
        """
        if not self._tables or self._preview_worker is not None:
            return
        self.lbl_preview.setText("계산 중...")
        self._preview_worker = _PreviewWorker(
            self._tables, dict(self._spec), self._current_spec(), self)
        self._preview_worker.done.connect(self._on_preview)
        self._preview_worker.start()

    def _on_preview(self, result, error):
        self._preview_worker = None
        if error or not result:
            self.lbl_preview.setText(f"미리보기 계산 실패: {error}")
            return
        before, after, stats = result
        lines = [f"수율 {before['yield']:.2f}% → {after['yield']:.2f}% "
                 f"(die {before['dies']:,} → {after['dies']:,})"]
        if after["worst"]:
            name, value = after["worst"]
            was = dict(before["worst_map"]).get(name)
            lines.append(f"worst CPK {name}: "
                         + (f"{was:.2f} → {value:.2f}" if was is not None else f"{value:.2f}"))
        lines.append(f"셀 수정 {stats['edited_cells']:,} · 규칙 적중 {stats['rule_hits']:,} · "
                     f"die 제외 {stats['excluded_dies']:,}")
        self.lbl_preview.setText("\n".join(lines))

    # ── 저장 ─────────────────────────────────────────────────────────────────
    def _after_change(self, message, error):
        self._refresh_state(message)
        if error:
            QMessageBox.warning(self, "값 확인",
                                f"일부 셀은 값 규칙에 맞지 않아 반영하지 않았습니다.\n{error}")

    def _refresh_state(self, message=""):
        parts = []
        if message:
            parts.append(message)
        parts.append(f"셀 수정 {len(self._pending):,}건")
        parts.append(f"규칙 {len(self._rules):,}건")
        if self._tables:
            parts.append(f"불러온 source {len(self._tables)}개")
        parts.append("원본은 바뀌지 않습니다 — 언제든 되돌릴 수 있습니다.")
        self.lbl_state.setText(" · ".join(parts))
        # 접힌 상태에서도 대기 중인 변경량이 보이게 한다.
        self.sec_rules.set_summary(f"셀 {len(self._pending):,} · 규칙 {len(self._rules):,}")

    def _save(self):
        import requests

        spec = self._current_spec()
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
            QMessageBox.warning(self, "빠른 수정", f"저장하지 못했습니다.\n{exc}")
            return

        self.changed = True
        self._spec = result.get("spec") or {}
        QMessageBox.information(
            self, "빠른 수정",
            "저장했습니다 — " + (result.get("summary") or "변경 없음") +
            "\n전 탭이 이 기준으로 다시 계산됩니다. (원본은 그대로입니다)")
        self.accept()

    def reject(self):
        saved_edits = {(e["source"], e["row_idx"], e["column"]): e["value"]
                       for e in self._spec.get("edits") or []}
        dirty = (self._pending != saved_edits
                 or self._rules != list(self._spec.get("rules") or []))
        if dirty and QMessageBox.question(
                self, "빠른 수정",
                "저장하지 않은 변경이 있습니다. 닫을까요?") != QMessageBox.StandardButton.Yes:
            return
        super().reject()

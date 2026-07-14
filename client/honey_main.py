"""Honey 클라이언트 (PyQt6).

UI 레이아웃은 .ui (Qt Designer 편집 가능) 에 정의, 런타임에 uic.loadUi 로 로드.
- honey_main.ui   : 메인 화면 (d1_storage 검색 → 분석 → 자동 저장 → 업로드)
- upload_dialog.ui: 서버 업로드용 메타(Product Type 라디오/Product/LOT/Revision/PW) 팝업
- d1_browser.ui   : d1_storage(가상 서버 스토리지) 파일 검색/선택 팝업

워크플로우: d1_storage 에서 CSV 검색·선택 → 출력 시트 선택 → '분석 실행' 시
입력 폴더에 xlsx 자동 저장(xlwings) → '서버에 업로드' 클릭 시 메타 팝업 입력 후 전송.
"""
import concurrent.futures
import contextlib
import os
import queue
import shutil
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

import requests

from PyQt6 import uic
from PyQt6.QtCore import Qt, QTimer, QEvent, QPropertyAnimation, QEasingCurve, QPoint, QRect, QUrl
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHeaderView,
    QMainWindow, QMessageBox, QPushButton, QTableWidgetItem, QWidget,
)

from transport.config import CURRENT_VERSION, SERVER_BASE_URL
from transport import update_policy, updater, uploader, version_check
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from d1 import D1BrowserDialog
from honey_ui import (
    ColorEditorDialog,
    ElapsedProgress as _ElapsedProgress,
    FileOrderDialog,
    OptionsDialog,
    ReportSettingsDialog,
    SHEET_OPTIONS,
    UploadDialog,
    wait_for_future as _wait_for_future,
)
from report_flow import (
    build_output_path as _build_output_path,
    prepare_report_webreport as _prepare_report_webreport,
    suggest_base_name as _suggest_base_name,
)
from web_report.honeyform import encode_honeyform_parquet, read_honeyform_file
import app_settings
import chart_colors
import client_identity

# 로컬 리포트 엔진 (pandas/xlwings 의존). 미설치 시 화면 비활성.
try:
    import report_generator as rg
    from report_generator import xlsx_writer
    import map_report
    _RG_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    rg = None
    xlsx_writer = None
    map_report = None
    _RG_IMPORT_ERROR = exc

PRODUCT_TYPES = ["MDDI", "PDDI", "PMIC", "SECURITY", "TCON"]
_FLOW_PROFILE_ON = bool(os.environ.get("HONEY_FLOW_PROFILE"))

# 프리징(onedir) 시 _MEIPASS, 아니면 스크립트 폴더에서 .ui 탐색
_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
UI_PATH = os.path.join(_BASE_DIR, "honey_main.ui")


@contextlib.contextmanager
def _flow_time(label):
    if not _FLOW_PROFILE_ON:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        print(f"[flow-profile] honey_main.{label}: {elapsed:.3f}s", file=sys.stderr, flush=True)


def _init_com_for_worker():
    """Excel COM/xlwings 를 워커 스레드에서 쓸 수 있으면 초기화한다."""
    try:
        import pythoncom  # type: ignore
    except Exception:
        return None
    try:
        pythoncom.CoInitialize()
        return pythoncom
    except Exception:
        return None


def _co_uninitialize(com_module):
    if com_module is None:
        return
    try:
        com_module.CoUninitialize()
    except Exception:
        pass


def _upload_progress_channel(progress, label_fmt, value_map=None):
    """업로드 진행률 콜백 쌍 (worker_cb, drain_cb) 생성 — _run_web_report/_do_upload 공용.

    worker_cb 는 워커 스레드에서 (bytes_read, total) 을 큐에 넣고, drain_cb 는 메인
    스레드(_wait_for_future poll)에서 마지막 값만 꺼내 progress 에 반영한다.
    label_fmt 는 "{pct}" 플레이스홀더를 포함한 문자열, value_map 은 pct(0~100)를
    progressbar value 로 바꾸는 함수 (없으면 pct 그대로).
    """
    q = queue.Queue()

    def worker_cb(bytes_read, total_bytes):
        q.put((bytes_read, total_bytes))

    def drain_cb():
        last = None
        while True:
            try:
                last = q.get_nowait()
            except queue.Empty:
                break
        if last is None:
            return
        bytes_read, total_bytes = last
        pct = int(bytes_read * 100 / total_bytes) if total_bytes else 0
        msg = label_fmt.format(pct=pct)
        progress.set(msg, value=value_map(pct) if value_map else pct, status=msg)

    return worker_cb, drain_cb


class SlideInPanel(QWidget):
    """왼쪽→오른쪽으로 슬라이드되어 나오는 프레임리스 최상위 패널.

    QWebEngineView 는 네이티브 윈도우라 일반 위젯 오버레이가 가려지므로,
    이 패널은 최상위(Qt.Tool) 창으로 만들어 브라우저 위로 슬라이드한다.
    anchor_widget(브라우저)의 왼쪽 가장자리를 기준으로 위치·높이를 잡는다."""

    def __init__(self, anchor_widget, content, title, width=460):
        super().__init__(anchor_widget.window(),
                         Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        from PyQt6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QVBoxLayout
        self._anchor = anchor_widget
        self._width = width

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QWidget()
        frame.setObjectName("slideFrame")
        outer.addWidget(frame)

        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QWidget()
        header.setObjectName("slideHeader")
        h = QHBoxLayout(header)
        h.setContentsMargins(12, 8, 8, 8)
        lbl = QLabel(title)
        lbl.setObjectName("slideTitle")
        btn_close = QToolButton()
        btn_close.setText("◀")
        btn_close.setObjectName("slideClose")
        btn_close.setToolTip("왼쪽으로 밀어 넣기")
        btn_close.clicked.connect(self.hide_animated)
        h.addWidget(lbl)
        h.addStretch(1)
        h.addWidget(btn_close)
        v.addWidget(header)
        v.addWidget(content, 1)

        self.setStyleSheet("""
            QWidget#slideFrame {
                background: #fffef7;
                border-right: 1px solid #d1d5db;
            }
            QWidget#slideHeader {
                background: #1f2937;
            }
            QLabel#slideTitle {
                color: #f9fafb; font-weight: 600; font-size: 11pt;
            }
            QToolButton#slideClose {
                color: #e5e7eb; font-size: 12pt; border: none;
                padding: 2px 8px; border-radius: 4px;
            }
            QToolButton#slideClose:hover { background: #374151; }
        """)

        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.hide()

    def _rects(self):
        """(숨김 위치, 표시 위치) 사각형 — anchor 왼쪽 가장자리 기준."""
        origin = self._anchor.mapToGlobal(QPoint(0, 0))
        h = self._anchor.height()
        shown = QRect(origin.x(), origin.y(), self._width, h)
        hidden = QRect(origin.x() - self._width, origin.y(), self._width, h)
        return hidden, shown

    def _fade_to(self, end_opacity, on_finished=None):
        self._anim.stop()
        try:
            self._anim.finished.disconnect()
        except TypeError:
            pass
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(end_opacity)
        if on_finished is not None:
            self._anim.finished.connect(on_finished)
        self._anim.start()

    def show_animated(self):
        # 창을 화면 밖으로 이동시키지 않고 항상 펼침 위치에 둔 채 투명도만 페이드한다
        # (좌측 인접 모니터로 슬라이드가 넘어가는 문제 회피 — 모니터 배치 무관).
        _, shown = self._rects()
        self.setGeometry(shown)
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
        self.raise_()
        self.activateWindow()
        self._fade_to(1.0)

    def hide_animated(self):
        self._fade_to(0.0, on_finished=self.hide)

    def reposition(self):
        """메인 창 이동/리사이즈 시 표시 중이면 위치 재정렬."""
        if self.isVisible():
            _, shown = self._rects()
            self.setGeometry(shown)


class HoneyMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        uic.loadUi(UI_PATH, self)
        self.status = self.statusbar
        self.setWindowTitle(f"Honey  v{CURRENT_VERSION}")
        self.setWindowIcon(self._honey_icon(64))   # 꿀단지 실행/창 아이콘
        self.status.showMessage(f"Server: {SERVER_BASE_URL}")
        self.progress_status.hide()
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setUndoRedoEnabled(False)
        self._apply_main_ui_tweaks()

        self.csv_paths = []
        self.group = None          # df_honey_group
        self.last_result = None    # AnalysisResult
        self.out_path = None       # 생성된 xlsx 경로
        self._last_upload = None   # 마지막 업로드 메타 (팝업 프리필용)

        self._pt_radios = {
            "MDDI": self.rb_pt_MDDI, "PDDI": self.rb_pt_PDDI,
            "PMIC": self.rb_pt_PMIC, "SECURITY": self.rb_pt_SECURITY,
            "TCON": self.rb_pt_TCON,
        }
        # 지난 실행에서 고른 Product Type 복원 (사용자별 settings.json)
        saved_pt = app_settings.get_setting("product_type")
        if saved_pt in self._pt_radios:
            self._pt_radios[saved_pt].setChecked(True)
        self._setup_csv_table()
        self._connect_signals()
        self.btn_open_local.setText("📁  LOCAL FILE OPEN")
        self.btn_pick_csv.setText("🐬  Dolphin (D1)에서 불러오기")
        self._build_chrome()

        if rg is None:
            self._disable_engine()
        QTimer.singleShot(500, self.check_for_update)

    def _apply_main_ui_tweaks(self):
        """메인 화면 상단 배치와 주요 파일 선택 버튼 가독성을 조정한다."""
        self.horizontalLayout_top.setStretch(0, 1)
        self.horizontalLayout_top.setStretch(1, 1)

        for button in (self.btn_open_local, self.btn_pick_csv):
            font = QFont(button.font())
            point_size = font.pointSize()
            if point_size > 0:
                font.setPointSize(point_size + 3)
            button.setFont(font)

    def _init_run_log(self, title):
        self._run_log_started = time.perf_counter()
        self._run_log_step = 0
        self._run_log_total = 0
        self.txt_summary.clear()
        if title:
            self._append_run_log(title)

    def _set_run_log_total(self, total):
        self._run_log_total = max(int(total or 0), 0)

    def _elapsed_run_log(self):
        started = getattr(self, "_run_log_started", None)
        secs = int(time.perf_counter() - started) if started is not None else 0
        return f"{secs // 60:02d}:{secs % 60:02d}"

    def _append_run_log(self, message, advance=False):
        if advance:
            self._run_log_step = int(getattr(self, "_run_log_step", 0)) + 1
        step = int(getattr(self, "_run_log_step", 0))
        total = int(getattr(self, "_run_log_total", 0))
        if total:
            prefix = f"[{self._elapsed_run_log()}] [{step:02d}/{total:02d}]"
        else:
            prefix = f"[{self._elapsed_run_log()}]"
        self.txt_summary.append(f"{prefix} {message}")
        bar = self.txt_summary.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _log_profile_event(self, event):
        if not getattr(rg, "DEBUG_RUN_TIMING_LOG", False):
            return
        label = str(event.get("label") or "")
        skip = {
            "select_items",
            "split_for_diff",
            "subjects_meta",
            "subjects_meta_common",
            "build_yield",
            "build_fail_items",
            "build_issue_summary",
            "build_summary_rows",
            "build_major_fail_subjects",
            "build_cpk",
            "build_cpk_common",
            "build_distributions",
            "build_distributions_common",
            "combined_df_yield",
            "fill_cpk",
            "fill_fail_item",
            "fail_values.title",
            "fail_values.borders",
            "fill_fail_item.style",
            "normalize_sheet_names",
            "zoom_gridlines",
        }
        if label in skip:
            return
        status = event.get("status")
        elapsed = event.get("elapsed")
        error = event.get("error")
        if status == "start":
            return
        elif status == "info":
            msg = event.get("message") or label
            if msg:
                self._append_run_log(str(msg))
        elif status == "done":
            self._append_run_log(f"{label} done: {elapsed:.2f}s" if elapsed is not None
                                 else f"{label} done", advance=True)
        elif status == "error":
            msg = f"{label} ERROR"
            if elapsed is not None:
                msg += f" after {elapsed:.2f}s"
            if error:
                msg += f" - {error}"
            self._append_run_log(msg, advance=True)

    def _estimate_run_log_steps(self, work_group, sheets, raw_data):
        sources = len(work_group.names()) if work_group is not None else 0
        table_sheets = {"summary", "yield", "cpk", "fail_item", "issue_table"}
        selected_tables = [s for s in sheets if s in table_sheets]
        steps = 0
        if raw_data:
            steps += 1  # raw_frames
        steps += 1 + sources  # analysis table builders + fail_detail per source
        steps += 1  # workbook_init
        steps += sum(1 for s in selected_tables if s != "fail_item")
        if "cpk" in selected_tables:
            steps += 3  # fill_cpk expands into four substeps
        if "fail_item" in selected_tables:
            steps += 2 + sources  # top table + FAIL_VALUES + source chunks
        if raw_data:
            steps += sources
        steps += 2  # finalize + save
        if "distribution" in sheets:
            steps += 1
        if "histogram" in sheets:
            steps += 1
        return max(steps, 1)

    def _setup_csv_table(self):
        """list_csv (QTableWidget) 를 '파일 경로 | 확장자' 2열로 구성하고,
        파일 리스트 영역에 한정한 드래그앤드롭(외부 파일)을 활성화한다."""
        t = self.list_csv
        t.setColumnCount(3)
        t.setHorizontalHeaderLabels(["파일 경로", "확장자", ""])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)       # 긴 경로는 가로 스크롤
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # 확장자 좁게(오른쪽)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)             # 행별 ✕ 삭제 버튼(좁게 고정)
        t.setColumnWidth(2, 32)
        hh.setStretchLastSection(False)
        # 드롭은 리스트 영역에서만 받는다 (메인 창엔 setAcceptDrops 를 걸지 않음).
        t.setTextElideMode(Qt.TextElideMode.ElideNone)
        t.setWordWrap(False)
        t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        t.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        t.verticalHeader().setDefaultSectionSize(20)
        t.setAcceptDrops(True)
        t.viewport().installEventFilter(self)

    # ── 드래그앤드롭 (파일 리스트 영역 한정) ─────────────────────────────────
    def eventFilter(self, obj, event):
        if obj is self.list_csv.viewport():
            etype = event.type()
            if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                if event.mimeData().hasUrls():
                    event.acceptProposedAction()
                    return True
            elif etype == QEvent.Type.Drop:
                self._handle_csv_drop(event)
                return True
        elif obj is getattr(self, "progress_status", None):
            # 진행바가 보이면 하단 dock 을 펴고, 숨겨지면 접는다 (관찰만 — 이벤트 통과).
            dock = getattr(self, "dock_log", None)
            if dock is not None:
                if event.type() == QEvent.Type.Show:
                    dock.show()
                elif event.type() == QEvent.Type.Hide:
                    dock.hide()
        return super().eventFilter(obj, event)

    def _handle_csv_drop(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self._intake(paths)   # 기존 인테이크 흐름 재사용(2개↑면 순서 팝업)

    def _connect_signals(self):
        self.btn_open_local.clicked.connect(self.on_open_local)
        self.btn_pick_csv.clicked.connect(self.on_browse_d1)
        # 입력 파일: 선택 후 ▲▼ 로 순서 변경 (맨 위 파일이 기준), Clear 로 전체 비우기
        # 개별 파일 삭제는 각 행의 ✕ 버튼(_refill_csv_list)이 담당한다.
        self.btn_csv_up.clicked.connect(lambda: self._move_file(-1))
        self.btn_csv_down.clicked.connect(lambda: self._move_file(1))
        self.btn_csv_clear.clicked.connect(self._clear_files)
        # Start: 파일 전처리 후 설정 팝업(Select Items/Option/색/Auto Upload) 열기
        self.btn_start.clicked.connect(self.on_start)
        self.btn_web_report.clicked.connect(self.on_web_report)
        self.btn_upload_local.clicked.connect(self.on_upload_local)
        # Product Type 선택 변경 시 사용자별 settings.json 에 즉시 저장
        for rb in self._pt_radios.values():
            rb.toggled.connect(self._save_product_type)

    # ── 실험: 내장 브라우저 + 메뉴바 + 아이콘 사이드바 ───────────────────────
    def _build_chrome(self):
        """메인 화면을 재구성한다 (실험, .ui 무변경):
        - 중앙 대부분을 서버 리포트 브라우저가 차지
        - 상단 메뉴바(F10) / 왼쪽 아이콘 사이드바로 기존 버튼 액션 이관
        - 입력 컨트롤(Product Type·파일 리스트·저장명)은 File Open 시 뜨는
          별도 창(dock)으로, 기본은 숨김
        - Status/Log 는 하단 창(dock)으로 이동

        PyQtWebEngine 미설치 시 조기 return — 기존 화면/동작 100% 유지."""
        try:
            import embedded_browser
        except ImportError as exc:
            self._status(f"내장 브라우저 비활성 (PyQtWebEngine 필요): {exc}")
            return
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import (
            QDockWidget, QHBoxLayout, QToolBar, QVBoxLayout, QWidget,
        )

        url = SERVER_BASE_URL.rstrip("/") + "/pe/report/"
        # .ui 로 만든 기존 central 은 버리되(참조는 유지해 버튼 위젯 살려둠),
        # 필요한 위젯만 새 dock 컨테이너로 옮긴다.
        self._legacy_central = self.takeCentralWidget()
        # 파일 열기/D1/Start/Web Report/도움말 버튼은 입력 창(패널) 안으로 이관해 다시 보인다.
        # Server Upload 버튼만 화면에서 제거(기능은 on_upload_local 로 보존).
        self.btn_upload_local.setVisible(False)

        # 중앙: 웹 브라우저가 전체를 차지
        self.browser_panel = embedded_browser.BrowserPanel(url, navigate=True)
        self.setCentralWidget(self.browser_panel)
        # Rawdata(Excel) 편집 중 세션 이탈(다른 페이지 이동)을 확인 다이얼로그로 가로챈다.
        try:
            self.browser_panel.view.page().leave_guard = self._browser_leave_guard
        except Exception:
            pass

        self._build_controls_panel(QWidget, QVBoxLayout, QHBoxLayout)
        self._build_log_dock(QDockWidget, QWidget)
        self._menu_bar_actions()
        self._icon_sidebar(QAction, QToolBar)

    def _build_controls_panel(self, QWidget, QVBoxLayout, QHBoxLayout):
        """'새 리포트' 입력 창 — 기본 숨김, 사이드바 🆕 로 왼쪽에서 슬라이드.
        파일 열기·D1·Product Type·파일 리스트·저장명·분석 모드·Start/Web Report 를 담는다."""
        from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QGroupBox,
                                     QRadioButton, QSizePolicy)

        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)

        # 파일 열기 / D1 불러오기
        self.btn_help.setVisible(False)  # 물음표 도움말 버튼 제거 (도움말은 메뉴바로 이동)
        open_row = QHBoxLayout()
        open_row.addWidget(self.btn_open_local, 1)
        open_row.addWidget(self.btn_pick_csv, 1)
        v.addLayout(open_row)

        v.addWidget(self.groupBox_pt)

        file_row = QHBoxLayout()
        # 파일 리스트는 세로로 확장하지 않도록 고정 — 남는 세로 공간이 아래 실행 그리드를
        # 끝까지 밀지 않고, 그리드가 위로 붙은 뒤 그 아래가 빈 공간이 되게 한다.
        self.list_csv.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        file_row.addWidget(self.list_csv)
        move_col = QVBoxLayout()
        move_col.addWidget(self.btn_csv_up)
        move_col.addWidget(self.btn_csv_down)
        move_col.addWidget(self.btn_csv_clear)
        move_col.addStretch(1)
        file_row.addLayout(move_col)
        v.addLayout(file_row)

        name_row = QHBoxLayout()
        name_row.addWidget(self.lbl_outname)
        name_row.addWidget(self.le_outname)
        name_row.addWidget(self.lbl_xlsx_ext)
        v.addLayout(name_row)

        # 실행 영역: Web Report(분석 모드 라디오 포함) 와 Excel Report 를 그리드 2개로 분리.
        # 분석 모드는 Web Report 에만 해당하므로 Web Report 그룹 안에 둔다.
        web_box = QGroupBox("Web Report")
        web_v = QVBoxLayout(web_box)
        web_v.setContentsMargins(8, 6, 8, 6)
        web_v.setSpacing(6)

        # 분석 모드 — 라디오. 파일 개수와 안 맞는 모드는 실행 시 경고.
        mode_row = QHBoxLayout()
        self._mode_radios = {}
        self._mode_group = QButtonGroup(self)
        for key in ("Normal", "Compare", "DUT"):
            rb = QRadioButton(key)
            if key == "Normal":
                rb.setChecked(True)
            self._mode_group.addButton(rb)
            self._mode_radios[key] = rb
            mode_row.addWidget(rb)
        mode_row.addStretch(1)
        web_v.addLayout(mode_row)

        # AI Comment — 서버 eval_analyzer 분석 결과를 Issue Table 에 표시할지 여부.
        # 값은 settings.json 에 영속(webreport_ai_comment). 서버 파이프라인 검증
        # 전까지 비활성 노출 — 활성화는 setEnabled(True) 한 줄.
        self.chk_ai_comment = QCheckBox("AI Comment")
        self.chk_ai_comment.setChecked(
            bool(app_settings.get_setting("webreport_ai_comment", False)))
        self.chk_ai_comment.toggled.connect(
            lambda v: app_settings.set_setting("webreport_ai_comment", bool(v)))
        self.chk_ai_comment.setEnabled(False)
        web_v.addWidget(self.chk_ai_comment)
        web_v.addWidget(self.btn_web_report)

        # Excel Report — 로컬 xlsx 생성/분석 (기존 Start 버튼).
        self.btn_start.setText("Excel Report")
        excel_box = QGroupBox("Excel Report")
        excel_v = QVBoxLayout(excel_box)
        excel_v.setContentsMargins(8, 6, 8, 6)
        excel_v.addStretch(1)
        excel_v.addWidget(self.btn_start)

        # 각 버튼이 그리드 칸 가로를 꽉 채우도록 (.ui 는 Fixed → Expanding 으로 완화)
        for _b in (self.btn_web_report, self.btn_start):
            _b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # 좌=Web Report / 우=Excel Report (기존 Start·Web Report 위치 스왑)
        run_row = QHBoxLayout()
        run_row.addWidget(web_box, 1)
        run_row.addWidget(excel_box, 1)
        v.addLayout(run_row)
        # 실행 그리드 아래는 빈 공간으로 (그리드를 위로 붙인다).
        v.addStretch(1)

        self.slide_controls = SlideInPanel(
            self.browser_panel, container, "입력 파일 / 설정", width=620)

    def _build_log_dock(self, QDockWidget, QWidget):
        """하단 창(dock): 진행바만 표시(경과시간·상태 메시지). 제목표시줄 없음.
        Log(txt_summary)·Status 라벨은 화면에서 제거하되 위젯은 숨겨 코드 참조를 유지한다."""
        from PyQt6.QtWidgets import QHBoxLayout

        self.txt_summary.hide()
        self.lbl_progress_status.hide()

        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(8, 2, 8, 2)
        row.addWidget(self.progress_status, 1, Qt.AlignmentFlag.AlignVCenter)

        dock = QDockWidget(self)
        dock.setTitleBarWidget(QWidget())   # 제목표시줄 제거
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        dock.setWidget(container)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.dock_log = dock
        self.resizeDocks([dock], [40], Qt.Orientation.Vertical)
        # 진행바가 숨겨진 평소엔 dock 을 접어 하단 흰 빈칸을 없앤다.
        # progress_status 의 Show/Hide 이벤트를 받아 dock 가시성을 함께 토글한다.
        self.progress_status.installEventFilter(self)
        dock.hide()

    def _show_controls(self):
        """입력 창을 슬라이드로 띄운다 (File Open 시 호출)."""
        panel = getattr(self, "slide_controls", None)
        if panel is not None:
            panel.show_animated()

    def _toggle_controls(self):
        """입력/설정 창을 접었다 폈다 토글 (사이드바 🆕)."""
        panel = getattr(self, "slide_controls", None)
        if panel is None:
            return
        if panel.isVisible():
            panel.hide_animated()
        else:
            panel.show_animated()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        panel = getattr(self, "slide_controls", None)
        if panel is not None:
            panel.reposition()

    def moveEvent(self, event):
        super().moveEvent(event)
        panel = getattr(self, "slide_controls", None)
        if panel is not None:
            panel.reposition()

    def _act_open_local(self):
        self._show_controls()
        self.on_open_local()

    def _act_browse_d1(self):
        self._show_controls()
        self.on_browse_d1()

    def _menu_bar_actions(self):
        """상단 메뉴바 구성 — 기존 슬롯을 그대로 호출 (로직 복제 없음)."""
        mb = self.menuBar()

        m_file = mb.addMenu("파일(&F)")
        m_file.addAction("LOCAL FILE OPEN", self._act_open_local)
        m_file.addAction("Dolphin (D1)에서 불러오기", self._act_browse_d1)

        m_run = mb.addMenu("실행(&R)")
        m_run.addAction("새 리포트 생성", self._show_controls)
        m_run.addAction("Rawdata 편집", self.on_rawdata_edit)
        m_run.addAction("Excel Download", self.on_excel_download)

        m_view = mb.addMenu("보기(&V)")
        m_view.addAction("입력 / 설정 창 열기", self._show_controls)
        m_view.addAction("입력 / 설정 창 닫기",
                         lambda: self.slide_controls.hide_animated())
        act_l = self.dock_log.toggleViewAction()
        act_l.setText("진행 상태 창")
        m_view.addAction(act_l)
        m_view.addSeparator()
        m_view.addAction("검색결과 홈", lambda: self.browser_panel.go_home())
        m_view.addAction("새로고침", lambda: self.browser_panel.view.reload())

        m_settings = mb.addMenu("설정(&S)")
        m_settings.addAction("Options...", self.on_options)

        m_help = mb.addMenu("도움말(&H)")
        m_help.addAction("HONEY 도움말", self.on_help_honey)
        m_help.addAction("VOC", self.on_voc)

    def _icon_sidebar(self, QAction, QToolBar):
        """왼쪽 아이콘 사이드바 — 자주 쓰는 액션(이모지 아이콘 + 밑 라벨 + tooltip)."""
        from PyQt6.QtCore import QSize
        from PyQt6.QtGui import QIcon
        tb = QToolBar("Quick")
        tb.setMovable(False)
        tb.setOrientation(Qt.Orientation.Vertical)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)  # 아이콘 위·라벨 밑
        tb.setIconSize(QSize(28, 28))
        tb.setStyleSheet("""
            QToolBar {
                background: #1f2937;
                border: none;
                padding: 6px 2px;
                spacing: 4px;
            }
            QToolBar QToolButton {
                color: #e5e7eb;
                font-size: 8pt;
                min-width: 60px;
                min-height: 52px;
                border-radius: 8px;
            }
            QToolBar QToolButton:hover { background: #374151; }
            QToolBar QToolButton:pressed { background: #4b5563; }
        """)
        quick = [
            ("🆕", "New Report",   "새 리포트 (입력 / 설정 창 접기·펴기)", self._toggle_controls),
            ("📝", "Rawdata edit", "Rawdata 수정 (Excel)",             self.on_rawdata_edit),
            (self._excel_icon(), "Excel Down", "Excel Download",       self.on_excel_download),
            ("📤", "Excel Upload", "로컬 xlsx 업로드 (Raw Data → web report 세션)", self.on_upload_local),
            ("⚙️", "Options",      "옵션 (색·기본값 설정)",             self.on_options),
        ]
        for icon, label, tip, slot in quick:
            qicon = icon if isinstance(icon, QIcon) else self._emoji_icon(icon)
            act = QAction(qicon, label, self)
            act.setToolTip(tip)
            act.triggered.connect(slot)
            tb.addAction(act)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, tb)

    @staticmethod
    def _emoji_icon(emoji, px=28):
        """이모지 문자를 투명 배경 QPixmap 에 그려 QIcon 으로 반환."""
        from PyQt6.QtGui import QPixmap, QPainter, QIcon
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        f = p.font()
        f.setPointSize(int(px * 0.7))
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, emoji)
        p.end()
        return QIcon(pm)

    @staticmethod
    def _honey_icon(px=64):
        """Honey 이미지(Honey_img.png) 아이콘 — 실행/창 아이콘용.

        repo 루트(dev)/_MEIPASS(frozen)의 Honey_img.png 를 우선 사용하고, 파일이
        없거나 로드 실패 시 벡터 꿀단지로 폴백한다(어떤 환경에서도 아이콘이 보이도록).
        """
        from PyQt6.QtGui import QPixmap, QPainter, QIcon, QColor
        from PyQt6.QtCore import QRectF
        for base in (_BASE_DIR, _REPO_ROOT):
            path = os.path.join(base, "Honey_img.png")
            if os.path.exists(path):
                icon = QIcon(path)
                if not icon.isNull():
                    return icon
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        u = px / 64.0
        p.setBrush(QColor("#C6820E"))                                   # 뚜껑
        p.drawRoundedRect(QRectF(15 * u, 7 * u, 34 * u, 11 * u), 4 * u, 4 * u)
        p.setBrush(QColor("#F2A81C"))                                   # 항아리 본체
        p.drawRoundedRect(QRectF(11 * u, 16 * u, 42 * u, 41 * u), 13 * u, 13 * u)
        p.setBrush(QColor("#FFF3D0"))                                   # 라벨 밴드
        p.drawRoundedRect(QRectF(16 * u, 30 * u, 32 * u, 16 * u), 3 * u, 3 * u)
        p.setBrush(QColor("#E38E0C"))                                   # 꿀방울
        p.drawEllipse(QRectF(28 * u, 32 * u, 8 * u, 11 * u))
        p.end()
        return QIcon(pm)

    @staticmethod
    def _excel_icon(px=28):
        """Excel 로고풍 아이콘 — 초록 라운드 사각형에 흰색 'X'."""
        from PyQt6.QtGui import QPixmap, QPainter, QIcon, QColor, QFont
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#16a34a"))
        p.drawRoundedRect(2, 1, px - 4, px - 2, 4, 4)
        f = QFont(p.font())
        f.setPointSize(int(px * 0.5))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "X")
        p.end()
        return QIcon(pm)

    def _disable_engine(self):
        # 분석 관련 기능만 비활성. 로컬 파일 직접 업로드는 엔진 없이도 동작하므로 유지.
        for name in ("btn_open_local", "btn_pick_csv", "btn_start", "btn_web_report"):
            getattr(self, name).setEnabled(False)
        self.txt_summary.setPlainText(
            "report_generator 모듈을 불러오지 못했습니다 — "
            f"{_RG_IMPORT_ERROR}\n분석/생성에는 pandas / numpy / xlwings + MS Excel 이 필요합니다."
            "\n(로컬 파일 직접 업로드는 가능합니다.)"
        )

    def _status(self, msg):
        self.status.showMessage(msg)

    # ── Rawdata 수정: 내장 브라우저에 열린 세션을 Excel 로 편집 ────────────────
    def _current_session_id(self):
        """내장 브라우저 주소에서 web_report 세션 id 추출 (없으면 "")."""
        panel = getattr(self, "browser_panel", None)
        if panel is None:
            return ""
        try:
            url = panel.view.url().toString()
        except Exception:
            return ""
        import re
        m = re.search(r"/pe/report/view/([A-Za-z0-9_-]+)", url)
        return m.group(1) if m else ""

    def on_rawdata_edit(self):
        """현재 열린 세션의 rawdata 를 Excel 창으로 열어 편집 → 저장·닫으면 서버 반영."""
        sid = self._current_session_id()
        if not sid:
            QMessageBox.information(
                self, "Rawdata 수정",
                "먼저 세션(검색결과에서 리포트)을 연 뒤 눌러 주세요.")
            return
        worker = getattr(self, "_excel_worker", None)
        if worker is not None and worker.isRunning():
            QMessageBox.information(self, "Rawdata 수정", "이미 Excel 편집이 진행 중입니다.")
            return

        from excel_edit.worker import ExcelEditWorker
        self._excel_worker = ExcelEditWorker(sid, SERVER_BASE_URL, self)
        w = self._excel_worker
        w.status.connect(self._on_excel_edit_status)
        w.done.connect(self._on_excel_edit_done)
        w.failed.connect(self._on_excel_edit_failed)
        self._append_run_log(f"Rawdata 수정 시작 (session {sid}) — Excel 을 엽니다...")
        w.start()

    def _on_excel_edit_status(self, state, message):
        self._status(message)
        self._append_run_log(f"[Rawdata] {message}")

    def _on_excel_edit_done(self, changed, message):
        if changed:
            self._status("Rawdata 수정 완료 — 페이지 새로고침")
            self._append_run_log("[Rawdata] 완료 — 서버 반영됨. 페이지 새로고침.")
            try:
                self.browser_panel.view.reload()
            except Exception:
                pass
        elif message == "취소됨":
            self._status("Rawdata 수정 취소됨")
            self._append_run_log("[Rawdata] 취소됨 — Excel 을 닫고 편집을 중단했습니다.")
        else:
            self._status("Rawdata 변경 없음")
            self._append_run_log("[Rawdata] 변경 없음 — 업로드 건너뜀.")

    # ── Rawdata 편집 중 이탈 가드 (브라우저 네비게이션 / 앱 종료 공용) ──────────
    def _excel_edit_running(self):
        worker = getattr(self, "_excel_worker", None)
        return worker is not None and worker.isRunning()

    def _confirm_cancel_edit(self):
        """Rawdata 편집 중 이탈 확인. 예=취소하고 나감(True)+Excel 종료, 아니오=머무름(False)."""
        reply = QMessageBox.question(
            self, "Rawdata 수정",
            "rawdata 수정이 완료 되지 않았습니다.\n수정을 취소 하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return False
        worker = getattr(self, "_excel_worker", None)
        if worker is not None and worker.isRunning():
            worker.cancel()   # 워커가 다음 폴링에서 Excel 강제 종료 후 done 시그널
            self._append_run_log("[Rawdata] 사용자 취소 — Excel 을 닫고 편집을 중단합니다.")
        return True

    def _browser_leave_guard(self, _url):
        """내장 브라우저 네비게이션 가드. Rawdata 편집 중이면 목적지와 무관하게 확인.
        반환 True=이동 허용, False=차단(현재 세션 유지)."""
        if not self._excel_edit_running():
            return True
        return self._confirm_cancel_edit()

    def closeEvent(self, event):
        if self._excel_edit_running():
            if not self._confirm_cancel_edit():
                event.ignore()
                return
            # 취소 승인됨 → 워커가 Excel 닫고 종료하길 잠시 대기 (QThread 파괴 경고 방지).
            worker = getattr(self, "_excel_worker", None)
            if worker is not None:
                worker.wait(6000)
        super().closeEvent(event)

    def _on_excel_edit_failed(self, message):
        self._status(f"Rawdata 수정 실패: {message}")
        self._append_run_log(f"[Rawdata] 실패: {message}")
        QMessageBox.warning(self, "Rawdata 수정 실패", message)

    # ── Excel Download: 열린 세션의 web report 를 xlsx 로 저장 ────────────────
    def on_excel_download(self):
        """현재 열린 web_report 세션을 xlsx 로 저장 (시트+차트 PNG, 클라이언트 생성)."""
        sid = self._current_session_id()
        if not sid:
            QMessageBox.information(
                self, "Excel Download",
                "먼저 세션(검색결과에서 리포트)을 연 뒤 눌러 주세요.")
            return
        worker = getattr(self, "_excel_dl_worker", None)
        if worker is not None and worker.isRunning():
            QMessageBox.information(self, "Excel Download", "이미 Excel Download 가 진행 중입니다.")
            return

        from excel_download._fetch import fetch_session_meta
        meta = fetch_session_meta(SERVER_BASE_URL, sid)
        base = "_".join(
            p for p in (str(meta.get(k) or "").strip() for k in ("product", "lot_id"))
            if p) or "webreport"
        default_path = os.path.join(os.path.expanduser("~"), "Documents",
                                    f"{base}_{sid}.xlsx")
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Excel 저장", default_path, "Excel (*.xlsx)")
        if not out_path:
            return

        from excel_download.worker import ExcelDownloadWorker
        self._excel_dl_worker = ExcelDownloadWorker(sid, SERVER_BASE_URL, out_path, self)
        w = self._excel_dl_worker
        w.status.connect(self._on_excel_dl_status)
        w.done.connect(self._on_excel_dl_done)
        w.failed.connect(self._on_excel_dl_failed)
        # 진행바(하단 dock) — 단계가 병렬이라 % 대신 경과시간 + 단계 메시지(indeterminate).
        # 워커는 단계별 status 만 보내므로 QTimer 로 경과시간 표시를 계속 갱신한다.
        self._excel_dl_progress = _ElapsedProgress(
            self.progress_status, "Excel Download 준비 중...", self._status,
            busy=True, minimum=0, maximum=0)
        self._excel_dl_timer = QTimer(self)
        self._excel_dl_timer.timeout.connect(
            lambda: self._excel_dl_progress.update())
        self._excel_dl_timer.start(500)
        self._append_run_log(f"Excel Download 시작 (session {sid}) → {out_path}")
        w.start()

    def _stop_excel_dl_timer(self):
        timer = getattr(self, "_excel_dl_timer", None)
        if timer is not None:
            timer.stop()
            self._excel_dl_timer = None

    def _on_excel_dl_status(self, state, message):
        self._status(message)
        self._append_run_log(f"[ExcelDL] {message}")
        progress = getattr(self, "_excel_dl_progress", None)
        if progress is not None:
            progress.set(message)

    def _on_excel_dl_done(self, out_path, elapsed):
        self._stop_excel_dl_timer()
        progress = getattr(self, "_excel_dl_progress", None)
        if progress is not None:
            progress.success(f"완료: Excel Download ({elapsed:.1f}s)")
        self._status(f"Excel Download 완료 ({elapsed:.1f}s)")
        self._append_run_log(f"[ExcelDL] 완료 ({elapsed:.1f}s): {out_path}")
        QMessageBox.information(
            self, "Excel Download", f"저장 완료 ({elapsed:.1f}초)\n{out_path}")

    def _on_excel_dl_failed(self, message):
        self._stop_excel_dl_timer()
        progress = getattr(self, "_excel_dl_progress", None)
        if progress is not None:
            progress.fail(f"실패: Excel Download - {message}")
        self._status(f"Excel Download 실패: {message}")
        self._append_run_log(f"[ExcelDL] 실패: {message}")
        QMessageBox.warning(self, "Excel Download 실패", message)

    # ── 입력 선택: 로컬 파일 열기 / d1_storage 검색 ─────────────────────────
    def on_open_local(self):
        # 현재 윈도우(네이티브) 파일 열기 대화상자
        paths, _ = QFileDialog.getOpenFileNames(
            self, "파일 열기 (여러 개 가능)", "",
            "모든 파일 (*.*)")
        self._intake(paths)

    def on_browse_d1(self):
        dlg = D1BrowserDialog(self)
        if not dlg.exec():
            return
        paths = dlg.selected_paths()
        if not paths:
            QMessageBox.warning(self, "선택 없음", "가져올 파일을 선택하세요.")
            return
        self._intake(paths)

    def on_help_honey(self):
        """도움말(&H) → HONEY 도움말: 목차형 통합 도움말 창."""
        from honey_ui.help_dialog import show_help
        show_help(self)

    def on_voc(self):
        """도움말(&H) → VOC: 고객의 소리 접수 페이지를 기본 브라우저로 연다."""
        from config import VOC_URL
        webbrowser.open(VOC_URL)

    def _intake(self, paths):
        """선택된 파일들 → (2개 이상이면) 순서 지정 팝업 → 메인 창에 로드."""
        paths = list(paths or [])
        if not paths:
            return
        if len(paths) > 1:
            dlg = FileOrderDialog(self, paths)
            if not dlg.exec():
                return
            paths = dlg.ordered_paths()
        self._load_paths(paths)

    def _refill_csv_list(self):
        """self.csv_paths 순서대로 list_csv(테이블) 다시 채우기.
        0열=파일 절대경로, 1열=확장자(오른쪽, 좁게)."""
        self.list_csv.setRowCount(len(self.csv_paths))
        for r, p in enumerate(self.csv_paths):
            full_path = str(Path(p).resolve())
            ext = Path(full_path).suffix.lstrip(".").lower()
            ext_item = QTableWidgetItem(ext)
            path_item = QTableWidgetItem(full_path)
            path_item.setData(Qt.ItemDataRole.UserRole, full_path)
            path_item.setToolTip(full_path)
            self.list_csv.setItem(r, 0, path_item)
            self.list_csv.setItem(r, 1, ext_item)
            # 행별 ✕ 삭제 버튼 — 그 행의 파일만 리스트에서 제거(경로로 식별해 행 이동에 안전).
            del_btn = QPushButton("✕")
            del_btn.setToolTip("이 파일을 리스트에서 삭제")
            del_btn.setFixedSize(24, 20)
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setStyleSheet(
                "QPushButton { border:none; background:transparent; color:#d11a2a;"
                " font-size:14px; font-weight:800; padding:0px; }"
                "QPushButton:hover { color:#ff0000; background:#ffe1e1; border-radius:4px; }")
            del_btn.clicked.connect(
                lambda _checked=False, path=full_path: self._delete_file_by_path(path))
            self.list_csv.setCellWidget(r, 2, del_btn)
            self.list_csv.setRowHeight(r, 20)
        if self.csv_paths:
            fm = self.list_csv.fontMetrics()
            width = max(fm.horizontalAdvance(str(Path(p).resolve())) for p in self.csv_paths)
            self.list_csv.setColumnWidth(0, max(420, width + 36))
            # 파일 리스트를 채우면 긴 경로의 파일명(오른쪽)이 보이도록 가로 스크롤을 끝까지.
            # 스크롤바 range 는 레이아웃 후 갱신되므로 다음 이벤트 루프에서 최대로 민다.
            bar = self.list_csv.horizontalScrollBar()
            QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def _clear_files(self):
        """파일 리스트를 전체 비운다 (Clear 버튼)."""
        self.csv_paths = []
        self._refill_csv_list()
        self.le_outname.clear()
        self.group = None
        self.out_path = None
        self._status("파일 리스트를 비웠습니다.")

    def _delete_file_by_path(self, path):
        """행별 ✕ 버튼: 그 행의 파일 1개만 리스트에서 제거한다.
        행 삭제 후 인덱스가 바뀌므로 클릭 시점의 행 번호가 아니라 경로로 대상을 찾는다."""
        target = str(Path(path).resolve())
        idx = next((i for i, p in enumerate(self.csv_paths)
                    if str(Path(p).resolve()) == target), -1)
        if idx < 0:
            return
        removed = Path(self.csv_paths[idx]).name
        del self.csv_paths[idx]
        self._refill_csv_list()
        # 파일 구성이 바뀌었으니 그룹은 무효화 — Start 시 재구성된다.
        self.group = None
        self.out_path = None
        if not self.csv_paths:
            self.le_outname.clear()
        self._status(f"'{removed}' 을(를) 리스트에서 제거했습니다.")

    def _load_paths(self, paths):
        """선택된 입력 파일들 → 기존 리스트에 이어붙이기(중복 경로 제외) + 저장 파일명 제안.
        새로 File open(또는 D1/드래그드롭)을 해도 기존 리스트를 지우지 않고 추가한다.
        전체 비우기는 Clear 버튼, 개별 제거는 각 행의 ✕ 버튼이 담당한다."""
        new_paths = [str(Path(p).resolve()) for p in paths]
        merged = list(self.csv_paths or [])
        seen = set(merged)
        for p in new_paths:
            if p not in seen:
                merged.append(p)
                seen.add(p)
        self.csv_paths = merged
        self._refill_csv_list()
        self.le_outname.setText(_suggest_base_name(self.csv_paths))
        self.group = None
        self.out_path = None
        self.txt_summary.setPlainText("")
        self._status(f"{len(self.csv_paths)}개 파일 선택됨. 순서 확인 후 Start 를 누르세요.")

    def _move_file(self, delta):
        """선택한 입력 파일을 위(-1)/아래(+1)로 이동 (전처리는 Start 까지 보류)."""
        row = self.list_csv.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < len(self.csv_paths)):
            return
        self.csv_paths[row], self.csv_paths[new] = self.csv_paths[new], self.csv_paths[row]
        self._refill_csv_list()
        self.list_csv.selectRow(new)

    def _rebuild_group(self, warn=False):
        """현재 self.csv_paths 순서로 그룹 재구성 + 항목 갱신.

        맨 위(첫) 파일이 units/항목명/Lower·Upper limit 의 기준이 된다 — 서로 다른
        유형의 파일이 섞여 들어와도 첫 파일 스키마를 기준으로 데이터가 처리된다.
        """
        with _flow_time("_rebuild_group.total"):
            paths = self.csv_paths
            if not paths:
                return False

            n_files = len(paths)
            # CSV 로딩을 백그라운드 스레드(1개)에서 파일 단위로 수행한다. 동기로 돌리면
            # 무거운 pandas 읽기 동안 Qt 이벤트 루프가 멈춰 Windows 가 창을 "응답 없음"
            # 으로 표시한다. 메인 스레드는 짧게 폴링하며 processEvents() 로 UI 를 살려
            # "(진행중)" 을 보여주고, 한 파일이 60초를 넘기면 라벨만 바꾼다(중단 없음).
            _SLOW_FILE_SEC = 60
            progress = _ElapsedProgress(
                self.progress_status, "파일 로딩 준비 중...", self._status,
                busy=True, minimum=0, maximum=n_files)
            QApplication.processEvents()

            results = []
            last_status = None
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    for i, p in enumerate(paths):
                        filename = Path(p).name
                        file_start_perf = time.perf_counter()
                        fut = ex.submit(rg.df_honey.from_csv, p, product_type=self.product_type())
                        file_start = time.monotonic()
                        while True:
                            done_set, _ = concurrent.futures.wait([fut], timeout=0.1)
                            elapsed = int(time.monotonic() - file_start)
                            if elapsed >= _SLOW_FILE_SEC:
                                label = (
                                    f"파일 전처리 중... ({i + 1}/{n_files})  {filename}  "
                                    f"(계속 진행중)"
                                )
                            else:
                                label = f"파일 전처리 중... ({i + 1}/{n_files})  {filename}"
                            # 라벨이 바뀔 때만 하단 status 바에도 반영(진행중임을 명확히 표시).
                            # 진행바 경과시간은 매 폴링마다 계속 갱신된다.
                            if label != last_status:
                                progress.set(label, value=i, status=label)
                                last_status = label
                            else:
                                progress.set(label, value=i)
                            if done_set:
                                break
                        results.append(fut.result())  # 로드 실패 시 여기서 예외 전파
                        if _FLOW_PROFILE_ON:
                            print(
                                f"[flow-profile] honey_main.load_file[{filename}]: "
                                f"{time.perf_counter() - file_start_perf:.3f}s",
                                file=sys.stderr,
                                flush=True,
                            )
                with _flow_time("df_honey_group.construct"):
                    self.group = rg.df_honey_group(results)
            except Exception as exc:
                progress.fail(f"실패: 파일 로드 실패 - {exc}")
                QMessageBox.critical(self, "파일 로드 실패", str(exc))
                self._status("파일 로드 실패")
                self.group = None
                return False

            progress.success(f"완료: {n_files}개 파일 전처리 완료", value=n_files)

            if warn:
                with _flow_time("group.validate"):
                    issues = {name: v for name, v in self.group.validate().items() if v}
                if issues:
                    msg = "\n".join(f"- {name}: {', '.join(v)}" for name, v in issues.items())
                    QMessageBox.warning(self, "스키마 경고", f"일부 파일에 문제가 있습니다:\n{msg}")

            self.out_path = None
            self._status(f"{len(paths)}개 파일 전처리 완료 (기준: {Path(paths[0]).name}).")
            return True

    def _apply_modes(self, group, mode_bin1, mode_dut):
        """선택된 데이터 정리 모드를 그룹에 적용. 문제 시 ValueError."""
        work = group
        if mode_bin1:
            work = work.filter_rows_by_bin("1")
            if not work.subjects() or all(len(md.scores) == 0
                                          for md in work.mass_data_map.values()):
                raise ValueError("Bin1 Only: Bin 이 1(Pass)인 데이터가 없습니다.")
        if mode_dut:
            if len(self.csv_paths) != 1:
                raise ValueError("DUT 정리는 입력 파일이 1개일 때만 가능합니다.")
            work = work.split_by_dut()
        return work

    # ── Start: 전처리 → 설정 팝업 → Confirm 시 분석 실행 ─────────────────────
    def _prepare_run_context(self):
        if not self.csv_paths:
            QMessageBox.warning(self, "입력 누락", "먼저 파일을 가져오세요.")
            return None
        # 파일 전처리(그룹 로드/검증) 를 이 시점에 수행
        if not self._rebuild_group(warn=True) or self.group is None:
            return None

        dlg = ReportSettingsDialog(
            self, self.group, len(self.csv_paths), product_type=self.product_type())
        if not dlg.exec():
            self._status("설정 취소됨 — 다시 Start 로 진행할 수 있습니다.")
            return None

        selected = dlg.selected_items()
        sheets = dlg.selected_sheets()
        # Filename(legend) 사용자 지정 시 source 명 교체 (DUT 정리 모드는 자체 명명 사용)
        overrides = dlg.filename_overrides()
        if overrides is not None and not dlg.mode_dut():
            self.group.rename_sources(overrides)
        # 데이터 정리 모드 적용 (Bin1 Only → DUT 정리 순서로 그룹 변환)
        try:
            work_group = self._apply_modes(self.group, dlg.mode_bin1(), dlg.mode_dut())
        except ValueError as exc:
            QMessageBox.warning(self, "모드 적용 불가", str(exc))
            return None
        return {
            "work_group": work_group,
            "selected": selected,
            "sheets": sheets,
            "raw_data": dlg.raw_data(),
            "compare_mode": dlg.mode_compare(),
        }

    def _set_run_buttons_enabled(self, enabled):
        """Web Report / Excel Report 실행 버튼을 함께 활성/비활성한다.

        한 리포트가 진행되는 동안(전처리·분석·업로드 전 구간) 두 버튼을 모두 잠가
        중복 실행을 막는다. 재활성은 호출부(on_start/on_web_report)의 finally 에서 보장한다.
        """
        self.btn_start.setEnabled(enabled)
        self.btn_web_report.setEnabled(enabled)

    def on_start(self):
        # 느린 파일 전처리(_prepare_run_context → _rebuild_group)가 시작되기 전에
        # 두 실행 버튼을 잠가, 진행 중 중복 클릭을 막는다.
        self._set_run_buttons_enabled(False)
        try:
            ctx = self._prepare_run_context()
            if ctx is None:
                return
            self._run_analysis(
                ctx["work_group"], ctx["selected"], ctx["sheets"],
                ctx["raw_data"], compare_mode=ctx["compare_mode"])
        finally:
            self._set_run_buttons_enabled(True)

    def on_webreport_colors(self):
        """F10 메뉴 → Distribution 색(Legend/source 팔레트) 설정.

        기존 ColorEditorDialog 재사용 — OK 시 chart_colors.json 에 저장되어 다음 Web Report
        생성 때 디폴트로 실린다. 색 번호 i = distribution source i 의 색.
        """
        dlg = ColorEditorDialog(self)
        if dlg.exec():
            self._status("Distribution 색 저장됨")

    def on_options(self):
        """Options — 기본 Product Type + Distribution 색.

        OK 시 선택한 Product Type 을 메인 UI 라디오에도 즉시 반영한다
        (라디오 toggled → _save_product_type 로 settings.json 에도 저장됨).
        """
        dlg = OptionsDialog(self)
        if dlg.exec():
            pt = dlg.selected_product_type()
            if pt in self._pt_radios:
                self._pt_radios[pt].setChecked(True)
            self._status("옵션 저장됨")

    def on_web_report(self):
        # 느린 파일 전처리(_prepare_web_report_context → _rebuild_group)가 시작되기 전에
        # 두 실행 버튼을 잠가, 진행 중 중복 클릭을 막는다.
        self._set_run_buttons_enabled(False)
        try:
            ctx = self._prepare_web_report_context()
            if ctx is None:
                return
            self._run_web_report(ctx["work_group"], ctx["selected"], ctx["sheets"],
                                 compare_mode=ctx["compare_mode"], options=ctx["options"],
                                 mode=ctx["mode"])
        finally:
            self._set_run_buttons_enabled(True)

    def _ask_source_names(self):
        """Web Report 생성 직전 source 별 legend 이름을 매번 확인·변경.

        빈 입력/취소는 기존 이름 유지. 반환값을 rename_sources 에 넘긴다 (없으면 None).
        """
        from PyQt6.QtWidgets import QInputDialog
        current = list(self.group.names())
        text, ok = QInputDialog.getText(
            self, "SourceName 변경",
            "각 입력 파일의 Legend 이름을 쉼표(,)로 구분해 입력하세요.\n"
            "빈칸은 기존 이름을 유지합니다.",
            text=", ".join(current))
        if not ok:
            return None
        parts = [p.strip() for p in text.split(",")]
        while len(parts) < len(current):
            parts.append("")
        overrides = []
        seen = {}
        for i, part in enumerate(parts[:len(current)]):
            base = part or current[i]
            if base in seen:
                seen[base] += 1
                base = f"{base}_{seen[base]}"
            else:
                seen[base] = 1
            overrides.append(base)
        return overrides

    def _selected_web_mode(self):
        """패널 라디오에서 선택된 Web Report 분석 모드 (기본 Normal)."""
        radios = getattr(self, "_mode_radios", None) or {}
        for key, rb in radios.items():
            if rb.isChecked():
                return key
        return "Normal"

    def _validate_web_mode(self, mode):
        """선택 모드가 입력 파일 개수에 맞는지 검사. 문제 시 경고 후 False.

        - Normal: 제한 없음
        - Compare: 입력 2개 (after/before 비교 — Honey Compare Mode 관례)
        - DUT: 입력 1개 (DUT/site 별 분할)
        - Commonality: 입력 1개 (강조 chip 을 웹에서 선택)
        """
        n = len(self.csv_paths)
        if mode in ("DUT", "Commonality") and n != 1:
            QMessageBox.warning(self, "모드 적용 불가",
                                f"{mode} 모드는 입력 파일이 1개일 때만 가능합니다. (현재 {n}개)")
            return False
        if mode == "Compare" and n != 2:
            QMessageBox.warning(self, "모드 적용 불가",
                                f"Compare 모드는 입력 파일이 2개일 때만 가능합니다. (현재 {n}개)")
            return False
        return True

    def _prepare_web_report_context(self):
        if not self.csv_paths:
            QMessageBox.warning(self, "입력 누락", "먼저 파일을 가져오세요.")
            return None
        # 분석 모드는 파일 전처리 전에 검증 (개수만 필요)
        mode = self._selected_web_mode()
        if not self._validate_web_mode(mode):
            self._status("모드 적용 불가")
            return None
        if not self._rebuild_group(warn=True) or self.group is None:
            return None
        # F10 에서 지정한 Distribution 색(chart_colors.json)을 웹리포트에 실어 보낸다.
        # 색 번호 i = distribution source i 의 색. 미지정이면 기본 팔레트가 실린다.
        # ai_comment: Issue Table AI Comment 컬럼 표시 여부 — 서버가 세션
        # webreport_options 에 고정 저장한다 (업로드 후 토글 불가).
        options = {"colors": chart_colors.load_colors(),
                   "ai_comment": bool(self.chk_ai_comment.isChecked())}
        # SourceName(legend) 은 파일마다 달라 매번 확인·변경 후 생성.
        # DUT 모드는 서버가 업로드된 단일 honeyform 의 DUT 컬럼으로 분할·명명(DUT <값>)하므로
        # 클라에서는 분할하지 않고 rename 도 건너뛴다 (df_honey→honeyform 포맷 변환 회피).
        if mode != "DUT":
            overrides = self._ask_source_names()
            if overrides is not None:
                self.group.rename_sources(overrides)
        return {
            "work_group": self.group,
            "selected": list(self.group.subjects()),
            "sheets": list(SHEET_OPTIONS),
            "compare_mode": (mode == "Compare"),
            "mode": mode,
            "options": options,
        }

    def _source_file_name(self, md, fallback):
        try:
            src = getattr(getattr(md, "report_meta", None), "source_path", "") or ""
            if src:
                return Path(src).name
        except Exception:
            pass
        return f"{fallback}.parquet"

    def _lot_id_from_sources(self, work_group):
        """첫 source 파일명의 head('_' 앞 토큰)를 LOT ID 로 반환. 없으면 빈 문자열.

        예: 'N4XA123_up_a.parquet' → 'N4XA123'. 파싱 실패는 best-effort 로 '' 반환.
        """
        try:
            names = work_group.names()
            if not names:
                return ""
            md = work_group.mass_data_map[names[0]]
            stem = Path(self._source_file_name(md, names[0])).stem
            return stem.split("_")[0].strip()
        except Exception:
            return ""

    def _build_webreport_parquets(self, work_group):
        items = []
        sources = []
        names = work_group.names()
        for idx, name in enumerate(names):
            md = work_group.mass_data_map[name]
            source_path = getattr(getattr(md, "report_meta", None), "source_path", "") or ""
            df = read_honeyform_file(source_path) if source_path else (
                md.to_df() if hasattr(md, "to_df") else md.df)
            data = encode_honeyform_parquet(df)
            file_name = self._source_file_name(md, name)
            items.append({
                "index": idx,
                "name": name,
                "file_name": f"{Path(file_name).stem or name}.parquet",
                "data": data,
            })
            sources.append({
                "index": idx,
                "name": name,
                "file_name": file_name,
            })
        return sources, items

    def _run_web_report(self, work_group, selected, sheets, compare_mode=False, options=None,
                        mode="Normal"):
        # 실행 버튼 잠금/해제는 호출부(on_web_report)의 try/finally 가 전담한다.
        self._init_run_log("Web Report 생성")
        progress = _ElapsedProgress(
            self.progress_status, "Web Report 준비 중...", self._status,
            busy=True, minimum=0, maximum=100)
        QApplication.processEvents()

        # 분석·인코딩(수 초)을 업로드 메타 입력과 병렬로 미리 시작한다 — 같은 워커 1개에서
        # 순차 실행이라 work_group 동시 접근이 없고, 사용자가 대화상자를 입력하는 동안
        # 대부분 끝난다. 취소 시 결과는 버린다 (읽기 전용 계산이라 부작용 없음).
        prep_ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut_analyze = prep_ex.submit(
            rg.analyze,
            work_group,
            meta=rg.ReportMeta(),
            selector=rg.ItemSelector(selected_items=selected),
            compare_mode=compare_mode,
        )
        fut_encode = prep_ex.submit(self._build_webreport_parquets, work_group)

        defaults = dict(self._last_upload or {})
        defaults["product_type"] = self.product_type()
        # source 파일명 head('_' 앞) 를 LOT ID 로 자동 채움 (직전 업로드 값보다 우선,
        # 사용자가 다이얼로그에서 수정 가능). 뽑히지 않으면 기존 defaults 유지.
        _lot_id = self._lot_id_from_sources(work_group)
        if _lot_id:
            defaults["lot_id"] = _lot_id
        # Web Report 업로드는 PIN 입력을 요구하지 않는다 (비밀번호 행 숨김).
        dlg = UploadDialog(self, defaults=defaults, show_password=False)
        if not dlg.exec():
            progress.fail("취소됨: 업로드 메타 입력 취소")
            prep_ex.shutdown(wait=False, cancel_futures=True)
            return
        meta = dlg.values()
        self._last_upload = meta
        meta["file_name"] = self.le_outname.text().strip() or _suggest_base_name(
            self.csv_paths, work_group)

        try:
            progress.set("데이터 분석 중... (Web Report)", value=10, status="데이터 분석 중...")
            self.last_result = _wait_for_future(fut_analyze, progress)
            self._show_summary(self.last_result)
        except Exception as exc:
            progress.fail(f"실패: 분석 실패 - {exc}")
            prep_ex.shutdown(wait=False, cancel_futures=True)
            QMessageBox.critical(self, "분석 실패", str(exc))
            self._status("Web Report 분석 실패")
            return

        try:
            progress.set("parquet 인코딩 중...", value=35, status="parquet 인코딩 중...")
            sources, parquet_items = _wait_for_future(fut_encode, progress)
        except Exception as exc:
            progress.fail(f"실패: parquet 인코딩 실패 - {exc}")
            prep_ex.shutdown(wait=False)
            QMessageBox.critical(self, "Web Report 실패", str(exc))
            self._status("parquet 인코딩 실패")
            return
        prep_ex.shutdown(wait=False)

        manifest = {
            "sources": sources,
            "meta": meta,
            "client": client_identity.collect(),
            "selected_items": list(selected or []),
            "sheets": list(sheets or []),
            "options": options or {},
            "mode": mode or "Normal",
        }

        _on_upload_progress, _drain_upload_progress = _upload_progress_channel(
            progress, "Web Report 업로드 중... ({pct}%)",
            value_map=lambda pct: 40 + int(pct * 0.6))

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(
                    uploader.post_webreport,
                    manifest,
                    parquet_items,
                    progress_cb=_on_upload_progress,
                )
                result = _wait_for_future(fut, progress, poll_cb=_drain_upload_progress)
        except Exception as exc:
            progress.fail(f"실패: Web Report 업로드 실패 - {exc}")
            QMessageBox.critical(self, "Web Report 업로드 실패", str(exc))
            self._status("Web Report 업로드 실패")
            return

        sid = result.get("session_id", "?")
        url = result.get("web_report_url")
        if url and str(url).startswith("/"):
            url = SERVER_BASE_URL.rstrip("/") + str(url)
        elif not url:
            url = f"{SERVER_BASE_URL.rstrip('/')}/pe/report/web_report/{sid}"

        progress.success(f"Web Report 완료: session_id {sid}", value=100)
        self._append_run_log(f"Web Report URL: {url}")
        self._status(f"Web Report 완료: {sid}")
        # 완료 팝업 없이 바로 내장 브라우저(웹 화면)로 전환하고 입력/설정 창을 닫는다.
        self._open_in_embedded(url)
        panel = getattr(self, "slide_controls", None)
        if panel is not None:
            panel.hide_animated()

    def _open_in_embedded(self, url):
        """내장 브라우저(있으면)로 url 이동. 없으면 외부 브라우저 폴백."""
        panel = getattr(self, "browser_panel", None)
        if panel is not None:
            panel.view.load(QUrl(url))
            return
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _run_analysis(self, work_group, selected, sheets, raw_data=False,
                      compare_mode=False, mode_map=False):
        # 실행 버튼 잠금/해제는 호출부(on_start)의 try/finally 가 전담한다.
        show_timing_log = bool(getattr(rg, "DEBUG_RUN_TIMING_LOG", False))
        overall_t0 = time.perf_counter()
        self._init_run_log("")
        self._set_run_log_total(
            self._estimate_run_log_steps(work_group, sheets, raw_data)
            if show_timing_log else 0
        )
        profile_events = queue.Queue()

        def _profile_cb(event):
            profile_events.put(event)

        profile_cb = _profile_cb if show_timing_log else None

        def _drain_profile_events():
            if profile_cb is None:
                return
            while True:
                try:
                    event = profile_events.get_nowait()
                except queue.Empty:
                    break
                self._log_profile_event(event)

        # Raw Data 시트용 원본 프레임 (체크 시) — source별 df_honey 적재 포맷 그대로
        raw = None
        if raw_data:
            try:
                raw_t0 = time.perf_counter()
                with _flow_time("raw_frames"):
                    raw = work_group.raw_frames()
                if show_timing_log:
                    self._append_run_log(
                        f"raw_frames done: {time.perf_counter() - raw_t0:.2f}s",
                        advance=True)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Raw Data 생략",
                                    f"원본 데이터 시트를 만들지 못해 건너뜁니다:\n{exc}")
                self._append_run_log(f"raw_frames ERROR - {exc}", advance=True)
                raw = None
        # 진행 단계: 준비(1) → 분석(1) → 요약(1) → 시트별(N, +Raw N) → 저장 마무리(1)
        total = len(sheets) + 4 + (len(raw) if raw else 0)
        progress = _ElapsedProgress(
            self.progress_status, "분석 준비 중...", self._status,
            busy=True, minimum=0, maximum=total)
        QApplication.processEvents()

        def _step(value, label):
            progress.set(label, value=value, status=label)

        # 1) 데이터 검증/준비
        _step(1, "데이터 검증/준비 중...")

        # 2) 데이터 분석 (통계 · Bin 집계)
        progress.set("데이터 분석 중... (통계 · Bin 집계)", status="데이터 분석 중...")
        try:
            analyze_t0 = time.perf_counter()
            with _flow_time("rg.analyze.total"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(
                        rg.analyze,
                        work_group,
                        meta=rg.ReportMeta(),
                        selector=rg.ItemSelector(selected_items=selected),
                        profile_cb=profile_cb,
                        compare_mode=compare_mode,
                    )
                    self.last_result = _wait_for_future(fut, progress, poll_cb=_drain_profile_events)
            _drain_profile_events()
            if show_timing_log:
                self._append_run_log(f"Analysis total: {time.perf_counter() - analyze_t0:.2f}s")
        except Exception as exc:
            _drain_profile_events()
            self._append_run_log(f"Analysis ERROR - {exc}")
            progress.fail(f"실패: 분석 실패 - {exc}")
            QMessageBox.critical(self, "분석 실패", str(exc))
            self._status("분석 실패")
            return
        progress.set("데이터 분석 완료", value=2)

        # 3) 요약 작성
        progress.set("요약 작성 중...", status="요약 작성 중...")
        self._show_summary(self.last_result)
        progress.set("요약 작성 완료", value=3)

        base = self.le_outname.text().strip() or _suggest_base_name(self.csv_paths, self.group)
        out = _build_output_path(Path(self.csv_paths[0]).parent, base)

        # 4) 시트/차트 생성 (시트 1개당 1스텝, offset 3)
        progress_events = queue.Queue()

        def _sheet_progress(done, total_s, name):
            progress_events.put(("sheet", done, total_s, name))

        _dist_state = {"base": 0, "n": 0}

        def _dist_progress(done, n_charts):
            progress_events.put(("dist", done, n_charts, None))

        _attach_state = {"base": 0, "last_log": {}}

        def _attach_progress(event, sheet_name, subject, done=None, total=None):
            payload = {
                "event": event,
                "sheet_name": sheet_name,
                "subject": subject,
                "done": done,
                "total": total,
            }
            progress_events.put(("attach", payload, None, None))

        def _drain_progress_events():
            while True:
                try:
                    kind, a, b, c = progress_events.get_nowait()
                except queue.Empty:
                    break
                if kind == "sheet":
                    done, total_s, name = a, b, c
                    if name == "distribution":
                        continue  # _dist_progress 가 처리
                    progress.set(
                        f"시트/차트 생성 중... ({name})   {done}/{total_s}",
                        value=3 + done,
                        status=f"시트 생성 중... ({name})  {done}/{total_s}",
                    )
                elif kind == "dist":
                    done, n_charts = a, b
                    if _dist_state["n"] == 0 and n_charts:
                        _dist_state["base"] = progress.value()
                        _dist_state["n"] = n_charts
                        progress.set_maximum(progress.maximum() + n_charts - 1)
                    pct = done * 100 // n_charts if n_charts else 100
                    value = _dist_state["base"] + done if n_charts else progress.value()
                    progress.set(
                        f"Distribution 차트 생성 중... ({done}/{n_charts} - {pct}%)",
                        value=value,
                        status=f"Distribution {pct}%  ({done}/{n_charts})",
                    )
                elif kind == "attach":
                    payload = a or {}
                    event = payload.get("event")
                    sheet_name = payload.get("sheet_name") or ""
                    subject = payload.get("subject") or ""
                    done = int(payload.get("done") or 0)
                    total_a = int(payload.get("total") or 0)
                    if event == "start":
                        if total_a:
                            _attach_state["base"] = progress.value()
                            progress.set_maximum(progress.maximum() + total_a)
                        _attach_state["last_log"][sheet_name] = 0
                        continue
                    if event == "progress":
                        pct = done * 100 // total_a if total_a else 100
                        value = _attach_state["base"] + done if total_a else progress.value()
                        msg = f"PNG 붙이는 중... ({sheet_name} {done}/{total_a} - {pct}%)"
                        progress.set(msg, value=value, status=msg)
                        continue
                    if event == "done":
                        continue
                    if event == "copy_picture":
                        msg = "Chart 복사 붙여넣기 진행중 잠시 기다려주세요"
                        progress.set(f"{msg} ({sheet_name}: {subject})", status=msg)

        progress.set(
            f"Excel 시트/차트 생성 중...  → {Path(out).name}",
            status=f"xlsx 생성 중... (Excel)  → {Path(out).name}",
        )
        # Map 옵션: 입력 파일별 wafer bin map PNG 생성 (matplotlib, COM 비의존).
        map_pngs, map_tmpdir = [], None
        if mode_map and map_report is not None:
            try:
                map_pngs, map_tmpdir = map_report.build_map_pngs(
                    work_group.mass_data_map, log_cb=self._append_run_log)
            except Exception as exc:  # noqa: BLE001
                self._append_run_log(f"Map 생성 ERROR - {exc}")
                map_pngs, map_tmpdir = [], None
        try:
            colors = chart_colors.load_colors()

            def _write_job():
                com_module = _init_com_for_worker()
                try:
                    with _flow_time("xlsx_writer.write.total"):
                        out_path = xlsx_writer.write(
                            self.last_result, out, sheets=sheets,
                            colors=colors,
                            progress_cb=_sheet_progress, raw_sheets=raw,
                            dist_progress_cb=_dist_progress,
                            attach_progress_cb=_attach_progress,
                            profile_cb=profile_cb,
                        )
                    # Map 옵션: xlsx 생성 완료 후 별도 xlwings 세션으로 Map 시트 부착.
                    # (report_generator 는 map 무관 — 구서버 교체 대비. 같은 COM-init 스레드.)
                    if map_pngs and map_report is not None:
                        with _flow_time("map_report.attach_map_sheet"):
                            map_report.attach_map_sheet(out_path, map_pngs)
                    return out_path
                finally:
                    _co_uninitialize(com_module)

            write_t0 = time.perf_counter()
            with _flow_time("xlsx_generation.total_wait"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(_write_job)
                    _wait_for_future(
                        fut,
                        progress,
                        poll_cb=lambda: (_drain_profile_events(), _drain_progress_events()),
                    )
            _drain_profile_events()
            if show_timing_log:
                self._append_run_log(f"XLSX write total: {time.perf_counter() - write_t0:.2f}s")
        except Exception as exc:
            _drain_profile_events()
            self._append_run_log(f"XLSX write ERROR - {exc}")
            progress.fail(f"실패: xlsx 생성 실패 - {exc}")
            QMessageBox.critical(self, "생성 실패", str(exc))
            self._status("xlsx 생성 실패")
            return
        finally:
            if map_tmpdir:
                shutil.rmtree(map_tmpdir, ignore_errors=True)
        _drain_progress_events()

        # 5) Excel 파일 저장 마무리
        _step(progress.maximum(), "Excel 파일 저장 마무리 중...")
        self.out_path = out
        if show_timing_log:
            self._append_run_log(f"Overall total: {time.perf_counter() - overall_t0:.2f}s")
        self._append_run_log(f"저장됨: {out}")
        progress.success(f"완료: {Path(out).name} 저장됨", value=progress.maximum())
        self._status(f"완료: {Path(out).name}  ('서버에 업로드' 가능)")

    def _show_summary(self, r):
        feat = r.summary_feature()
        lines = [
            "",
            "=== Summary ===",
            f"Sources: {', '.join(r.sources)}",
            f"Total: {r.total_dut}    Pass(Bin1): {feat['Pass (Bin 1)']}  ({r.pass_yield}%)",
            "",
            "[Major Fail Bins]",
        ]
        for i, b in enumerate(r.major_fail_bins(), start=1):
            lines.append(f"  {i}. bin {b.get('bin')}  -  {b.get('Main Fail subject')}  ({b.get('avg')}%)")
        current = self.txt_summary.toPlainText()
        if current.strip():
            self.txt_summary.append("\n".join(lines))
        else:
            self.txt_summary.setPlainText("\n".join(lines))
        bar = self.txt_summary.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ── 서버 업로드 ─────────────────────────────────────────────────────────
    def on_upload_local(self):
        """로컬에 있는 임의의 xlsx 를 직접 업로드 (분석 엔진 불필요).

        최신 Windows 탐색기(네이티브) 파일 열기 대화상자 사용 — DontUseNativeDialog
        를 주지 않아 OS 기본 다이얼로그가 뜬다.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "업로드할 파일 선택", "",
            "Excel (*.xlsx);;모든 파일 (*.*)")
        if path:
            self._do_upload(path)

    def product_type(self):
        """메인 UI 에서 선택된 Product Type (라디오). 기본 MDDI."""
        for key, rb in self._pt_radios.items():
            if rb.isChecked():
                return key
        return "MDDI"

    def _save_product_type(self, *_):
        """Product Type 선택을 사용자별 설정에 저장 (다음 실행 때 복원)."""
        app_settings.set_setting("product_type", self.product_type())

    def _do_upload(self, path):
        """report_generator 보고서 xlsx → Raw Data 복원 → web_report 세션 생성.

        Excel COM 으로 Raw Data 시트를 7-meta honeyform parquet 으로 복원하고
        Summary/Issue_table 코멘트를 추출해, 기존 web_report 업로드 경로
        (post_webreport)로 전송한다 — 일반 web_report 세션과 동일한 세션이 만들어진다.
        """
        defaults = dict(self._last_upload or {})
        defaults["product_type"] = self.product_type()
        # web_report 세션은 PIN 을 쓰지 않는다 (비밀번호 행 숨김).
        dlg = UploadDialog(self, defaults=defaults, show_password=False)
        if not dlg.exec():
            return
        meta = dlg.values()
        self._last_upload = meta
        meta["file_name"] = Path(path).stem

        self.btn_upload_local.setEnabled(False)

        # ── xlsx 전처리: Excel COM 으로 Raw Data → honeyform + 코멘트 추출 ──────
        # 대형/DRM xlsx 는 수 초~수십 초 걸리므로 워커 스레드에서 실행한다
        # (prepare_report_webreport 이 자체적으로 CoInitialize 하므로 스레드 안전).
        prep_progress = _ElapsedProgress(
            self.progress_status, f"xlsx 전처리 중... {Path(path).name}",
            self._status, busy=True, maximum=0)
        QApplication.processEvents()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_prepare_report_webreport, path)
                sources, parquet_items, seed, all_items = _wait_for_future(
                    fut, prep_progress)
        except ValueError as exc:
            prep_progress.fail("실패: 파일 오류")
            QMessageBox.critical(self, "파일 오류", str(exc))
            self.btn_upload_local.setEnabled(True)
            return
        except Exception as exc:
            prep_progress.fail("실패: xlsx 전처리 오류")
            QMessageBox.critical(
                self, "전처리 실패",
                f"xlsx 전처리(Excel COM) 중 오류가 발생했습니다:\n{exc}")
            self.btn_upload_local.setEnabled(True)
            return

        manifest = {
            "sources": sources,
            "meta": meta,
            "client": client_identity.collect(),
            "selected_items": all_items,
            "sheets": list(SHEET_OPTIONS),
            "options": {"colors": chart_colors.load_colors(), "ai_comment": False},
            "mode": "Normal",
        }
        # Summary/Issue_table 코멘트 시드 (issue_comments/etc_items/summary_engr).
        # ingest 가 seed_from_manifest 로 세션 편집 DB 에 복사한다.
        manifest.update(seed)

        # ── 서버 업로드 ───────────────────────────────────────────────────
        self._append_run_log(f"{Path(path).name} 파일 Upload 진행중입니다...")

        progress = _ElapsedProgress(
            self.progress_status, f"서버 업로드 중... {Path(path).name}",
            self._status, busy=False, minimum=0, maximum=100)
        QApplication.processEvents()

        _on_upload_progress, _drain_upload_progress = _upload_progress_channel(
            progress, f"서버 업로드 중... {Path(path).name} ({{pct}}%)")

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(
                    uploader.post_webreport,
                    manifest,
                    parquet_items,
                    progress_cb=_on_upload_progress,
                )
                result = _wait_for_future(fut, progress, poll_cb=_drain_upload_progress)
        except Exception as exc:
            progress.fail(f"실패: 업로드 실패 - {exc}")
            QMessageBox.critical(self, "업로드 실패", str(exc))
            self._status("업로드 실패")
            self.btn_upload_local.setEnabled(True)
            return

        sid = result.get("session_id", "?")
        url = result.get("web_report_url")
        if url and str(url).startswith("/"):
            url = SERVER_BASE_URL.rstrip("/") + str(url)
        elif not url:
            url = f"{SERVER_BASE_URL.rstrip('/')}/pe/report/view/{sid}"

        progress.success(f"업로드 완료: session_id {sid}")
        self._append_run_log(f"Web Report URL: {url}")
        self._status(f"업로드 완료: {sid}")
        # 완료 팝업 없이 내장 브라우저(웹 화면)로 바로 전환한다 (_run_web_report 와 동일).
        self._open_in_embedded(url)
        self.btn_upload_local.setEnabled(True)

    # ── version check (사용자가 자동/수동 설치 선택) ────────────────────────
    def check_for_update(self):
        try:
            manifest = version_check.fetch_latest()
        except requests.exceptions.RequestException:
            # 연결 불가/타임아웃 = 서버 오프라인으로 간주, 상태바에 명확히 표시
            self.status.showMessage(
                f"⚠ 서버 오프라인 — {SERVER_BASE_URL} 에 연결할 수 없습니다")
            return
        except Exception as exc:
            self.status.showMessage(f"버전 체크 실패: {exc}")
            return

        remote = manifest.get("version") or ""
        if not version_check.is_newer(remote, CURRENT_VERSION):
            self.status.showMessage(
                f"버전 체크 OK — 최신 ({CURRENT_VERSION}). Server: {SERVER_BASE_URL}")
            return

        # 설치 방법 선택: [자동 설치] / [ZIP 다운로드] / [나중에]
        can_auto = updater.can_write_app_dir()
        box = QMessageBox(self)
        box.setWindowTitle("업데이트 사용 가능")
        box.setIcon(QMessageBox.Icon.Question)
        ask_text = (
            f"신규 버전 {remote} 이(가) 있습니다.\n"
            f"현재: {CURRENT_VERSION}\n\n설치 방법을 선택하세요.\n\n"
            "· 자동 설치: 다운로드 후 앱을 교체하고 재실행합니다.\n"
            "· ZIP 다운로드: ZIP 만 다운로드 폴더에 저장합니다 (수동 설치).")
        if not can_auto:
            ask_text += "\n\n(설치 폴더에 쓰기 권한이 없어 자동 설치는 사용할 수 없습니다.)"
        box.setText(ask_text)
        btn_auto = box.addButton("자동 설치", QMessageBox.ButtonRole.AcceptRole)
        btn_manual = box.addButton("ZIP 다운로드", QMessageBox.ButtonRole.ActionRole)
        box.addButton("나중에", QMessageBox.ButtonRole.RejectRole)
        if not can_auto:
            btn_auto.setEnabled(False)
        box.setDefaultButton(btn_auto if can_auto else btn_manual)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_auto:
            mode = update_policy.MODE_AUTO
        elif clicked is btn_manual:
            mode = update_policy.MODE_MANUAL
        else:  # "나중에" 또는 창 닫기
            return

        url = manifest.get("url") or "/honey/download"
        expected = manifest.get("sha256") or None
        package_name = manifest.get("file") or f"Honey-{remote}.zip"
        if mode == update_policy.MODE_MANUAL:
            dest = update_policy.unique_dest(
                update_policy.downloads_dir(), package_name)
        else:
            dest = Path(tempfile.gettempdir()) / package_name

        # 다운로드 진행 상태는 메인 UI Status bar 에 표시한다.
        progress = _ElapsedProgress(
            self.progress_status, "업데이트 다운로드 중...",
            self.status.showMessage, busy=True, minimum=0, maximum=100)
        download_events = queue.Queue()

        def _cb(done, total):
            download_events.put((done, total))
            return True

        def _drain_download_events():
            while True:
                try:
                    done, total = download_events.get_nowait()
                except queue.Empty:
                    break
                label = f"업데이트 다운로드 중... ({done // (1024 * 1024)}MB"
                label += f" / {total // (1024 * 1024)}MB)" if total else ")"
                progress.set(label, value=int(done * 100 / total) if total else 0)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(
                    version_check.download_to,
                    dest,
                    url,
                    expected_sha256=expected,
                    progress_cb=_cb,
                )
                _wait_for_future(fut, progress, poll_cb=_drain_download_events)
        except version_check.DownloadCancelled:
            progress.fail("실패: 업데이트 다운로드 취소됨")
            self.status.showMessage("업데이트 취소됨")
            return
        except Exception as exc:
            progress.fail(f"실패: 업데이트 다운로드 실패 - {exc}")
            QMessageBox.critical(self, "다운로드 실패", str(exc))
            self.status.showMessage("업데이트 실패")
            return
        progress.success("완료: 업데이트 다운로드 완료", value=100)

        if mode == update_policy.MODE_MANUAL:
            update_policy.open_folder_select(dest)
            QMessageBox.information(
                self, "수동 업데이트 안내",
                f"업데이트 ZIP 을 저장했습니다:\n{dest}\n\n"
                "설치 방법:\n"
                "1) Honey 를 종료\n"
                "2) ZIP 압축 해제\n"
                "3) 압축 푼 Honey 폴더 내용을 설치 폴더에 덮어쓰기\n"
                "4) Honey.exe 다시 실행",
            )
            self.status.showMessage(f"업데이트 ZIP 저장됨: {dest}")
            return

        if not updater.is_frozen():
            QMessageBox.information(
                self, "다운로드 완료 (개발 모드)",
                f"스크립트 실행 중이라 설치를 진행하지 않습니다.\n"
                f"업데이트 ZIP만 다운로드 완료:\n{dest}\n\n"
                f"(자동 업데이트는 빌드된 exe 에서 동작합니다.)",
            )
            progress.success("다운로드 완료 (개발 모드)", value=100)
            self.status.showMessage("다운로드 완료 (개발 모드)")
            return

        QMessageBox.information(
            self, "업데이트 설치",
            f"새 버전 {remote} 을(를) 설치합니다.\n\n"
            "업데이트하는 동안 앱이 잠시 종료되며, 완료되면 자동으로 다시 실행됩니다.\n"
            "잠시만 기다려 주세요.",
        )
        try:
            updater.apply_update_zip(dest)
        except Exception as exc:
            progress.fail(f"실패: 업데이트 실행 실패 - {exc}")
            QMessageBox.critical(self, "업데이트 실행 실패", str(exc))
            self.status.showMessage("업데이트 실패")
            return
        progress.success("업데이트 적용 중... 앱을 종료합니다.", value=100)
        self.status.showMessage("업데이트 적용 중... 앱을 종료합니다.")
        QApplication.quit()


def _install_excepthook():
    """슬롯에서 발생한 미처리 예외로 앱이 조용히 죽지 않도록, 메시지로 표시.

    PyQt6 는 슬롯의 미처리 예외 시 기본 excepthook 이면 abort 한다. 후킹하면
    앱을 유지하면서 오류를 보여줄 수 있다.
    """
    import traceback

    def hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        print(text, file=sys.stderr)  # tee 된 stderr → 로그 파일에 traceback 기록
        try:
            QMessageBox.critical(None, "오류가 발생했습니다", text[-3000:])
        except Exception:
            pass
        sys.__excepthook__(etype, value, tb)

    sys.excepthook = hook


HONEY_QSS = """
    QMainWindow, QDialog, QDockWidget { background: #FFF8E1; }
    QLabel { background: transparent; color: #5D4711; }
    QGroupBox {
        background: #FFFDF5; border: 1px solid #E8D9A8; border-radius: 6px;
        margin-top: 10px; color: #6B4E16;
    }
    QGroupBox::title {
        subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #8A6D1D;
    }
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
        background: #FFFFFF; border: 1px solid #E0CE93; border-radius: 5px;
        padding: 3px 6px; color: #4A3B1A; selection-background-color: #FFD65A;
        selection-color: #3A2E12;
    }
    QPushButton {
        background: #F6C445; color: #4A3B1A; border: 1px solid #E0A81E;
        border-radius: 6px; padding: 5px 12px; font-weight: 600;
    }
    QPushButton:hover { background: #FFD65A; }
    QPushButton:pressed { background: #E9B21E; }
    QPushButton:disabled { background: #EDE6CF; color: #A89C77; border-color: #E0D9C0; }
    QRadioButton, QCheckBox { background: transparent; color: #4A3B1A; spacing: 6px; }
    QRadioButton::indicator, QCheckBox::indicator {
        width: 15px; height: 15px; border: 2px solid #B98A2E; background: #FFFFFF;
    }
    QRadioButton::indicator { border-radius: 9px; }
    QCheckBox::indicator { border-radius: 3px; }
    QRadioButton::indicator:checked, QCheckBox::indicator:checked {
        background: #E9A100; border-color: #9A6B12;
    }
    QRadioButton::indicator:hover, QCheckBox::indicator:hover { border-color: #8A6D1D; }
    QRadioButton::indicator:disabled, QCheckBox::indicator:disabled {
        border-color: #D8CBA0; background: #F0ECDD;
    }
    QTableWidget, QTableView, QListWidget, QTreeWidget {
        background: #FFFFFF; border: 1px solid #E8D9A8;
        gridline-color: #F0E6C8; selection-background-color: #FFE29A;
        selection-color: #3A2E12; alternate-background-color: #FFFBEA;
    }
    QHeaderView::section {
        background: #F3E5B8; color: #6B4E16; border: none;
        border-right: 1px solid #E8D9A8; padding: 4px 6px; font-weight: 600;
    }
    QMenuBar { background: #F3E5B8; color: #6B4E16; }
    QMenuBar::item:selected { background: #FFD65A; }
    QMenu { background: #FFFDF5; border: 1px solid #E8D9A8; color: #4A3B1A; }
    /* QMenu 에 스타일시트가 걸리면 item 패딩을 명시하지 않을 때 Qt 가 우측 글자를 잘라
       긴 메뉴명이 다 안 보인다 — 좌우 여백을 넉넉히 줘 폭을 확보한다. */
    QMenu::item { padding: 5px 30px 5px 22px; }
    QMenu::item:selected { background: #FFE29A; }
    QToolBar { background: #F3E5B8; border: none; }
    QProgressBar {
        background: #FBF3D6; border: 1px solid #E0CE93; border-radius: 5px;
        text-align: center; color: #5D4711;
    }
    QProgressBar::chunk { background: #F5A623; border-radius: 4px; }
    QTabBar::tab { background: #F3E5B8; color: #6B4E16; padding: 5px 12px; }
    QTabBar::tab:selected { background: #FFD65A; }
    QScrollBar:vertical { background: #FBF3D6; width: 12px; margin: 0; }
    QScrollBar::handle:vertical { background: #E7CE86; border-radius: 6px; min-height: 24px; }
    QScrollBar:horizontal { background: #FBF3D6; height: 12px; margin: 0; }
    QScrollBar::handle:horizontal { background: #E7CE86; border-radius: 6px; min-width: 24px; }
    QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
    QStatusBar { background: #F3E5B8; color: #6B4E16; }
"""


def _apply_honey_theme(app):
    """앱 전역을 연노란색 '꿀단지' 톤으로 통일 (개별 위젯 인라인 스타일은 유지된다)."""
    app.setStyleSheet(HONEY_QSS)


def _apply_cute_font(app):
    """앱 전역 글씨체를 귀여운(둥근) 느낌으로. 설치된 첫 후보를 사용."""
    from PyQt6.QtGui import QFontDatabase
    available = set(QFontDatabase.families())
    # 귀여운/둥근 계열 우선순위 (설치돼 있는 첫 폰트 선택)
    candidates = ["Comic Sans MS", "Segoe Print", "Comic Neue",
                  "HY엽서L", "HY견고딕", "맑은 고딕"]
    family = next((c for c in candidates if c in available), None)
    font = QFont(family) if family else app.font()
    font.setPointSize(10)
    app.setFont(font)


def main():
    import run_log
    run_log.setup_run_logging()
    # QtWebEngine(내장 브라우저)을 앱 생성 후 lazy import 하려면 필수 (없어도 무해)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    app.setWindowIcon(HoneyMainWindow._honey_icon(64))   # 작업표시줄 꿀단지 아이콘
    _apply_honey_theme(app)
    _apply_cute_font(app)
    _install_excepthook()
    win = HoneyMainWindow()
    win.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()  # PyInstaller + ProcessPoolExecutor(excel_download) 필수
    main()

"""Honey 클라이언트 (PyQt5).

UI 레이아웃은 .ui (Qt Designer 편집 가능) 에 정의, 런타임에 uic.loadUi 로 로드.
- honey_main.ui   : 메인 화면 (d1_storage 검색 → 분석 → 자동 저장 → 업로드)
- upload_dialog.ui: 서버 업로드용 메타(Product Type 라디오/Product/LOT/Revision/PW) 팝업
- d1_browser.ui   : d1_storage(가상 서버 스토리지) 파일 검색/선택 팝업

워크플로우: d1_storage 에서 CSV 검색·선택 → 출력 시트 선택 → '분석 실행' 시
입력 폴더에 xlsx 자동 저장(xlwings) → '서버에 업로드' 클릭 시 메타 팝업 입력 후 전송.
"""
import datetime
import os
import re
import sys
import tempfile
from pathlib import Path

from PyQt5 import uic
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QIntValidator
from PyQt5.QtWidgets import (
    QApplication, QColorDialog, QDialog, QDialogButtonBox, QFileDialog,
    QGridLayout, QHBoxLayout, QLabel, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressDialog, QPushButton, QVBoxLayout,
)

from config import CURRENT_VERSION, SERVER_BASE_URL, D1_STORAGE_DIR
import app_settings
import chart_colors
import chart_export
import updater
import uploader
import version_check

# 로컬 리포트 엔진 (pandas/xlwings 의존). 미설치 시 화면 비활성.
try:
    import report_generator as rg
    from report_generator import xlsx_writer
    _RG_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    rg = None
    xlsx_writer = None
    _RG_IMPORT_ERROR = exc

SHEET_OPTIONS = ["summary", "yield", "cpk", "fail_item", "issue_table", "distribution"]
PRODUCT_TYPES = ["MD", "PD", "PM", "SE"]
# 파일명 끝 시간 접미사 패턴 (_YYMMDD_HHMM) — 중복 부착 방지용
_TS_RE = re.compile(r"_\d{6}_\d{4}$")

# 프리징(onedir) 시 _MEIPASS, 아니면 스크립트 폴더에서 .ui 탐색
_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
UI_PATH = os.path.join(_BASE_DIR, "honey_main.ui")
UPLOAD_UI_PATH = os.path.join(_BASE_DIR, "upload_dialog.ui")
D1_UI_PATH = os.path.join(_BASE_DIR, "d1_browser.ui")
ORDER_UI_PATH = os.path.join(_BASE_DIR, "file_order.ui")
SETTINGS_UI_PATH = os.path.join(_BASE_DIR, "report_settings.ui")


def _validate_meta(product, lot_id, password):
    """공통 메타/PIN 검증. 문제 메시지(str) 반환, 정상이면 None."""
    if not product or not lot_id:
        return "Product 와 LOT ID 를 모두 입력하세요."
    if len(password) != 4 or not password.isdigit():
        return ("비밀번호는 숫자 4자리로 입력하세요.\n"
                "(서버에서 수정/삭제 시 사용됩니다.)")
    return None


def _common_base(stems):
    """입력 파일 stem 들에서 저장 파일명 base 추측 (공통 접두/접미사 우선)."""
    if not stems:
        return "report"
    if len(stems) == 1:
        return stems[0]
    pre = os.path.commonprefix(stems).strip(" _-")
    suf = os.path.commonprefix([s[::-1] for s in stems])[::-1].strip(" _-")
    cand = max((pre, suf), key=len)
    return cand if len(cand) >= 3 else stems[0]


def _timestamp():
    """파일명용 현재 시각: 260601_0949 (YYMMDD_HHMM)."""
    return datetime.datetime.now().strftime("%y%m%d_%H%M")


def _suggest_base_name(csv_paths):
    """입력 파일명들로부터 결과물 이름을 rough 하게 유추 (시간 접미사 포함해서 표시)."""
    stems = [Path(p).stem for p in csv_paths]
    base = _common_base(stems)
    base = base.strip(" _-") or "report"
    return f"{base}_report_{_timestamp()}"


def _build_output_path(out_dir, base):
    """base 이름으로 최종 저장 경로 생성. 시간 접미사가 없으면 현재 시각을 붙인다."""
    base = base.strip()
    if base.lower().endswith(".xlsx"):
        base = base[:-5]
    base = base.strip(" _-") or "report"
    if not _TS_RE.search(base):
        base = f"{base}_{_timestamp()}"
    return str(Path(out_dir) / f"{base}.xlsx")


# ───────────────────────────────────────────────────────────────────────────
# 서버 업로드 메타 입력 팝업 (Product Type 라디오 상단 + Product/LOT/Revision/PW)

class UploadDialog(QDialog):
    def __init__(self, parent=None, defaults=None):
        super().__init__(parent)
        uic.loadUi(UPLOAD_UI_PATH, self)
        self.le_password.setValidator(QIntValidator(0, 9999))
        self._pt_radios = {
            "MD": self.rb_pt_MD, "PD": self.rb_pt_PD,
            "PM": self.rb_pt_PM, "SE": self.rb_pt_SE,
        }
        self.buttonBox.accepted.connect(self._on_ok)
        self.buttonBox.rejected.connect(self.reject)
        if defaults:
            self._pt_radios.get(defaults.get("product_type", "MD"),
                                self.rb_pt_MD).setChecked(True)
            self.le_product.setText(defaults.get("product", ""))
            self.le_lot_id.setText(defaults.get("lot_id", ""))
            self.le_revision.setText(defaults.get("revision", ""))

    def product_type(self):
        for key, rb in self._pt_radios.items():
            if rb.isChecked():
                return key
        return "MD"

    def _on_ok(self):
        err = _validate_meta(self.le_product.text().strip(),
                             self.le_lot_id.text().strip(),
                             self.le_password.text().strip())
        if err:
            QMessageBox.warning(self, "입력 오류", err)
            return
        self.accept()

    def values(self):
        return {
            "product_type": self.product_type(),
            "product": self.le_product.text().strip(),
            "lot_id": self.le_lot_id.text().strip(),
            "revision": self.le_revision.text().strip(),
            "password": self.le_password.text().strip(),
        }


# ───────────────────────────────────────────────────────────────────────────
# distribution 차트 색 편집 팝업 (8x6 = 48색, 클릭 시 팔레트로 변경)

def _is_light(hex_color):
    s = str(hex_color).lstrip("#")
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except Exception:
        return True
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150


class ColorEditorDialog(QDialog):
    COLS, ROWS = 8, 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chart 색 편집 (Legend 1~48)")
        self._colors = chart_colors.load_colors()

        root = QVBoxLayout(self)
        info = QLabel(
            "각 색을 클릭하면 팔레트가 열립니다. 번호는 distribution Legend(소스) 순서와 같습니다.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#555;")
        root.addWidget(info)

        grid = QGridLayout()
        grid.setSpacing(6)
        self._btns = []
        for i in range(chart_colors.N_COLORS):
            b = QPushButton(str(i + 1))
            b.setFixedSize(60, 40)
            b.clicked.connect(lambda _c, idx=i: self._pick(idx))
            self._btns.append(b)
            grid.addWidget(b, i // self.COLS, i % self.COLS)
        root.addLayout(grid)

        row = QHBoxLayout()
        btn_reset = QPushButton("기본값 복원")
        btn_reset.clicked.connect(self._reset)
        row.addWidget(btn_reset)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        root.addLayout(row)

        self._refresh()

    def _refresh(self):
        for i, b in enumerate(self._btns):
            c = self._colors[i]
            fg = "#000" if _is_light(c) else "#fff"
            b.setStyleSheet(
                f"background-color:{c}; color:{fg}; font-weight:600;"
                "border:1px solid #999; border-radius:4px;")

    def _pick(self, idx):
        col = QColorDialog.getColor(QColor(self._colors[idx]), self,
                                    f"{idx + 1}번 색 선택")
        if col.isValid():
            self._colors[idx] = col.name().upper()
            self._refresh()

    def _reset(self):
        self._colors = chart_colors.generate_default_colors()
        self._refresh()

    def _on_ok(self):
        try:
            chart_colors.save_colors(self._colors)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "저장 실패", f"색 저장에 실패했습니다:\n{exc}")
            return
        self.accept()


# ───────────────────────────────────────────────────────────────────────────
# 파일 순서 지정 팝업 (열기/검색 후, 메인 창에 로드하기 전에 순서 확정)

class FileOrderDialog(QDialog):
    def __init__(self, parent, paths):
        super().__init__(parent)
        uic.loadUi(ORDER_UI_PATH, self)
        for p in paths:
            it = QListWidgetItem(Path(p).name)
            it.setData(Qt.UserRole, p)
            it.setToolTip(p)
            self.list_order.addItem(it)
        self.list_order.setCurrentRow(0)
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)

    def _move(self, delta):
        row = self.list_order.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < self.list_order.count()):
            return
        it = self.list_order.takeItem(row)
        self.list_order.insertItem(new, it)
        self.list_order.setCurrentRow(new)

    def ordered_paths(self):
        return [self.list_order.item(i).data(Qt.UserRole)
                for i in range(self.list_order.count())]


# ───────────────────────────────────────────────────────────────────────────
# d1_storage 파일 검색/선택 팝업

class D1BrowserDialog(QDialog):
    def __init__(self, parent, storage_dir):
        super().__init__(parent)
        uic.loadUi(D1_UI_PATH, self)
        self.storage_dir = storage_dir
        self.lbl_path.setText(f"D1: {storage_dir}")
        # 검색 전에는 비어 있다가, 키워드 입력 후 [검색] 시에만 조회
        self.btn_refresh.clicked.connect(self._search)
        self.le_search.returnPressed.connect(self._search)
        self.list_files.itemDoubleClicked.connect(lambda _i: self.accept())
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)
        self.le_search.setFocus()

    def _scan(self):
        p = Path(self.storage_dir)
        if not p.exists():
            return []
        files = [f for f in p.rglob("*")
                 if f.is_file() and f.suffix.lower() in (".csv", ".xlsx")]
        return sorted(files, key=lambda x: str(x).lower())

    def _search(self):
        """현재 키워드로 D1 을 조회해 결과를 채운다 (매번 디스크 재스캔)."""
        q = self.le_search.text().strip().lower()
        self.list_files.clear()
        for f in self._scan():
            rel = str(f.relative_to(self.storage_dir))
            if q and q not in rel.lower():
                continue
            it = QListWidgetItem(rel)
            it.setData(Qt.UserRole, str(f))
            self.list_files.addItem(it)
        if self.list_files.count() == 0:
            self.lbl_hint.setText(f"'{self.le_search.text().strip()}' 검색 결과가 없습니다.")
        else:
            self.lbl_hint.setText(
                f"{self.list_files.count()}개 결과 — Ctrl/Shift 로 여러 파일 선택 가능")

    def selected_paths(self):
        return [it.data(Qt.UserRole) for it in self.list_files.selectedItems()]


# ───────────────────────────────────────────────────────────────────────────
# 리포트 설정 팝업 (Start 시 전처리 끝난 그룹으로 열림):
# 분석 항목(Select Items) · 출력 옵션(Option) · 차트 색 · Auto Upload · Confirm

class ReportSettingsDialog(QDialog):
    def __init__(self, parent, group, csv_count):
        super().__init__(parent)
        uic.loadUi(SETTINGS_UI_PATH, self)
        self.group = group
        self.csv_count = csv_count
        self.sheet_checks = {
            name: getattr(self, f"cb_sheet_{name}") for name in SHEET_OPTIONS
        }

        # 분석 항목: 좌(제외) ↔ 우(선택) 이동
        self.btn_all_right.clicked.connect(self._move_all_right)
        self.btn_sel_right.clicked.connect(self._move_selected_right)
        self.btn_sel_left.clicked.connect(self._move_selected_left)
        self.btn_all_left.clicked.connect(self._move_all_left)
        self.btn_sel_fail.clicked.connect(self._select_fail_only)
        self.list_items_avail.itemDoubleClicked.connect(
            lambda it: self._move(self.list_items_avail, self.list_items_sel, [it]))
        self.list_items_sel.itemDoubleClicked.connect(
            lambda it: self._move(self.list_items_sel, self.list_items_avail, [it]))
        # yield 미선택 시 fail_item / issue_table 도 선택 불가
        self.cb_sheet_yield.toggled.connect(self._sync_yield_dependents)
        self.btn_chart_colors.clicked.connect(self.on_edit_chart_colors)
        self.buttonBox.accepted.connect(self._on_confirm)
        self.buttonBox.rejected.connect(self.reject)
        ok_btn = self.buttonBox.button(QDialogButtonBox.Ok)
        if ok_btn is not None:
            ok_btn.setText("Confirm")

        self._populate_items()
        self._sync_yield_dependents()
        self._update_dut_mode_availability()

    # ── 분석 항목 선택 ────────────────────────────────────────────────────────
    def _make_item(self, idx, text):
        it = QListWidgetItem(text)
        it.setData(Qt.UserRole, idx)   # 원본 subject 순서 보존용
        return it

    def _populate_items(self):
        self.list_items_avail.clear()
        self.list_items_sel.clear()
        # default: 전체 선택 → 모두 우측(선택)
        for idx, subj in enumerate(self.group.subjects()):
            self.list_items_sel.addItem(self._make_item(idx, subj))

    def _resort(self, lw):
        """원본 subject 순서(UserRole)대로 리스트 정렬."""
        pairs = sorted((lw.item(i).data(Qt.UserRole), lw.item(i).text())
                       for i in range(lw.count()))
        lw.clear()
        for idx, text in pairs:
            lw.addItem(self._make_item(idx, text))

    def _move(self, src, dst, items):
        for it in items:
            dst.addItem(src.takeItem(src.row(it)))
        self._resort(dst)
        self._resort(src)

    def _move_all_right(self):
        self._move(self.list_items_avail, self.list_items_sel,
                   [self.list_items_avail.item(i) for i in range(self.list_items_avail.count())])

    def _move_all_left(self):
        self._move(self.list_items_sel, self.list_items_avail,
                   [self.list_items_sel.item(i) for i in range(self.list_items_sel.count())])

    def _move_selected_right(self):
        self._move(self.list_items_avail, self.list_items_sel,
                   list(self.list_items_avail.selectedItems()))

    def _move_selected_left(self):
        self._move(self.list_items_sel, self.list_items_avail,
                   list(self.list_items_sel.selectedItems()))

    def _select_fail_only(self):
        """Fail 발생 항목만 우측(선택), 나머지는 좌측(제외)."""
        if self.group is None:
            return
        subjects = self.group.subjects()
        fail_ids = set(self.group.fail_subject_ids())
        self.list_items_avail.clear()
        self.list_items_sel.clear()
        for idx, subj in enumerate(subjects):
            target = self.list_items_sel if idx in fail_ids else self.list_items_avail
            target.addItem(self._make_item(idx, subj))

    # ── 출력 옵션 ─────────────────────────────────────────────────────────────
    def _sync_yield_dependents(self, *_):
        """yield 시트 미선택이면 fail_item / issue_table 선택 불가(해제+비활성)."""
        enabled = self.cb_sheet_yield.isChecked()
        for name in ("fail_item", "issue_table"):
            cb = self.sheet_checks[name]
            if not enabled:
                cb.setChecked(False)
            cb.setEnabled(enabled)

    def _update_dut_mode_availability(self):
        """DUT 정리는 입력 파일이 정확히 1개일 때만 가능."""
        ok = self.csv_count == 1
        if not ok:
            self.cb_mode_dut.setChecked(False)
        self.cb_mode_dut.setEnabled(ok)

    # ── 차트 색 편집 ──────────────────────────────────────────────────────────
    def on_edit_chart_colors(self):
        dlg = ColorEditorDialog(self)
        dlg.exec_()

    # ── 외부에서 읽는 값 ──────────────────────────────────────────────────────
    def selected_items(self):
        return [self.list_items_sel.item(i).text()
                for i in range(self.list_items_sel.count())]

    def selected_sheets(self):
        return [n for n, cb in self.sheet_checks.items() if cb.isChecked()]

    def mode_bin1(self):
        return self.cb_mode_bin1.isChecked()

    def mode_dut(self):
        return self.cb_mode_dut.isChecked()

    def auto_upload(self):
        return self.cb_auto_upload.isChecked()

    def raw_data(self):
        """체크 시 df_honey 원본 데이터를 'Raw Data' 시트로 추가."""
        return self.cb_raw_data.isChecked()

    def _on_confirm(self):
        if not self.selected_items():
            QMessageBox.warning(self, "항목 누락", "분석할 항목을 1개 이상 선택하세요.")
            return
        if not self.selected_sheets():
            QMessageBox.warning(self, "시트 누락", "출력할 시트를 1개 이상 선택하세요.")
            return
        self.accept()


# ───────────────────────────────────────────────────────────────────────────

class HoneyMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        uic.loadUi(UI_PATH, self)
        self.status = self.statusbar
        self.setWindowTitle(f"Honey  v{CURRENT_VERSION}")
        self.status.showMessage(f"Server: {SERVER_BASE_URL}")

        self.csv_paths = []
        self.group = None          # df_honey_group
        self.last_result = None    # AnalysisResult
        self.out_path = None       # 생성된 xlsx 경로
        self._last_upload = None   # 마지막 업로드 메타 (팝업 프리필용)

        self._pt_radios = {
            "MD": self.rb_pt_MD, "PD": self.rb_pt_PD,
            "PM": self.rb_pt_PM, "SE": self.rb_pt_SE,
        }
        # 지난 실행에서 고른 Product Type 복원 (사용자별 settings.json)
        saved_pt = app_settings.get_setting("product_type")
        if saved_pt in self._pt_radios:
            self._pt_radios[saved_pt].setChecked(True)
        self._connect_signals()

        if rg is None:
            self._disable_engine()
        QTimer.singleShot(500, self.check_for_update)

    def _connect_signals(self):
        self.btn_open_local.clicked.connect(self.on_open_local)
        self.btn_pick_csv.clicked.connect(self.on_browse_d1)
        # 입력 파일: 선택 후 ▲▼ 로 순서 변경 (맨 위 파일이 기준)
        self.btn_csv_up.clicked.connect(lambda: self._move_file(-1))
        self.btn_csv_down.clicked.connect(lambda: self._move_file(1))
        # Start: 파일 전처리 후 설정 팝업(Select Items/Option/색/Auto Upload) 열기
        self.btn_start.clicked.connect(self.on_start)
        self.btn_upload_local.clicked.connect(self.on_upload_local)
        # Product Type 선택 변경 시 사용자별 settings.json 에 즉시 저장
        for rb in self._pt_radios.values():
            rb.toggled.connect(self._save_product_type)

    def _disable_engine(self):
        # 분석 관련 기능만 비활성. 로컬 파일 직접 업로드는 엔진 없이도 동작하므로 유지.
        for name in ("btn_open_local", "btn_pick_csv", "btn_start"):
            getattr(self, name).setEnabled(False)
        self.lbl_out.setStyleSheet("color: #b00;")
        self.lbl_out.setText(
            "report_generator 모듈을 불러오지 못했습니다 — "
            f"{_RG_IMPORT_ERROR}\n분석/생성에는 pandas / numpy / xlwings + MS Excel 이 필요합니다."
            "\n(로컬 파일 직접 업로드는 가능합니다.)"
        )

    def _status(self, msg):
        self.status.showMessage(msg)

    # ── 입력 선택: 로컬 파일 열기 / d1_storage 검색 ─────────────────────────
    def on_open_local(self):
        # 현재 윈도우(네이티브) 파일 열기 대화상자
        paths, _ = QFileDialog.getOpenFileNames(
            self, "CSV/XLSX 파일 열기 (여러 개 가능)", "",
            "데이터 파일 (*.csv *.xlsx);;CSV (*.csv);;Excel (*.xlsx);;모든 파일 (*.*)")
        self._intake(paths)

    def on_browse_d1(self):
        os.makedirs(D1_STORAGE_DIR, exist_ok=True)
        dlg = D1BrowserDialog(self, D1_STORAGE_DIR)
        if not dlg.exec_():
            return
        paths = dlg.selected_paths()
        if not paths:
            QMessageBox.warning(self, "선택 없음", "가져올 파일을 선택하세요.")
            return
        self._intake(paths)

    def _intake(self, paths):
        """선택된 파일들 → (2개 이상이면) 순서 지정 팝업 → 메인 창에 로드."""
        paths = list(paths or [])
        if not paths:
            return
        if len(paths) > 1:
            dlg = FileOrderDialog(self, paths)
            if not dlg.exec_():
                return
            paths = dlg.ordered_paths()
        self._load_paths(paths)

    def _refill_csv_list(self):
        """self.csv_paths 순서대로 list_csv 다시 채우기 (절대 경로로 표시)."""
        self.list_csv.clear()
        for p in self.csv_paths:
            it = QListWidgetItem(str(p))
            it.setData(Qt.UserRole, p)
            it.setToolTip(str(p))
            self.list_csv.addItem(it)

    def _load_paths(self, paths):
        """선택된 입력 파일들 → 리스트 채우기 + 저장 파일명 제안 (전처리는 Start 까지 보류)."""
        self.csv_paths = list(paths)
        self._refill_csv_list()
        self.le_outname.setText(_suggest_base_name(self.csv_paths))
        self.group = None
        self.out_path = None
        self.lbl_out.setText("")
        self._status(f"{len(self.csv_paths)}개 파일 선택됨. 순서 확인 후 Start 를 누르세요.")

    def _move_file(self, delta):
        """선택한 입력 파일을 위(-1)/아래(+1)로 이동 (전처리는 Start 까지 보류)."""
        row = self.list_csv.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < len(self.csv_paths)):
            return
        self.csv_paths[row], self.csv_paths[new] = self.csv_paths[new], self.csv_paths[row]
        self._refill_csv_list()
        self.list_csv.setCurrentRow(new)

    def _rebuild_group(self, warn=False):
        """현재 self.csv_paths 순서로 그룹 재구성 + 항목 갱신.

        맨 위(첫) 파일이 units/항목명/Lower·Upper limit 의 기준이 된다 — 서로 다른
        유형의 파일이 섞여 들어와도 첫 파일 스키마를 기준으로 데이터가 처리된다.
        """
        paths = self.csv_paths
        if not paths:
            return False
        self._status("파일 로딩/검증 중...")
        QApplication.processEvents()
        try:
            self.group = rg.df_honey_group.from_csvs(paths)
        except Exception as exc:
            QMessageBox.critical(self, "파일 로드 실패", str(exc))
            self._status("파일 로드 실패")
            self.group = None
            return False

        if warn:
            issues = {n: v for n, v in self.group.validate().items() if v}
            if issues:
                msg = "\n".join(f"- {n}: {', '.join(v)}" for n, v in issues.items())
                QMessageBox.warning(self, "스키마 경고", f"일부 파일에 문제가 있습니다:\n{msg}")

        self.out_path = None
        self.lbl_out.setText("")
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
    def on_start(self):
        if not self.csv_paths:
            QMessageBox.warning(self, "입력 누락", "먼저 파일을 가져오세요.")
            return
        # 파일 전처리(그룹 로드/검증) 를 이 시점에 수행
        if not self._rebuild_group(warn=True) or self.group is None:
            return

        dlg = ReportSettingsDialog(self, self.group, len(self.csv_paths))
        if not dlg.exec_():
            self._status("설정 취소됨 — 다시 Start 로 진행할 수 있습니다.")
            return

        selected = dlg.selected_items()
        sheets = dlg.selected_sheets()
        # 데이터 정리 모드 적용 (Bin1 Only → DUT 정리 순서로 그룹 변환)
        try:
            work_group = self._apply_modes(self.group, dlg.mode_bin1(), dlg.mode_dut())
        except ValueError as exc:
            QMessageBox.warning(self, "모드 적용 불가", str(exc))
            return
        self._run_analysis(work_group, selected, sheets, dlg.auto_upload(),
                           dlg.raw_data())

    def _run_analysis(self, work_group, selected, sheets, auto_upload, raw_data=False):
        self.btn_start.setEnabled(False)
        # Raw Data 시트용 원본 테이블 (체크 시) — df_honey 적재 데이터 그대로
        raw = None
        if raw_data:
            try:
                raw = work_group.raw_table()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Raw Data 생략",
                                    f"원본 데이터 시트를 만들지 못해 건너뜁니다:\n{exc}")
                raw = None
        # 진행 단계: 준비(1) → 분석(1) → 요약(1) → 시트별(N, +Raw) → 저장 마무리(1)
        total = len(sheets) + 4 + (1 if raw is not None else 0)
        prog = QProgressDialog("분석 준비 중...", None, 0, total, self)
        prog.setWindowTitle("분석 실행")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setCancelButton(None)
        prog.setValue(0)
        QApplication.processEvents()

        def _step(value, label):
            prog.setLabelText(label)
            prog.setValue(value)
            self._status(label)
            QApplication.processEvents()

        # 1) 데이터 검증/준비
        _step(1, "데이터 검증/준비 중...")

        # 2) 데이터 분석 (통계 · Bin 집계)
        prog.setLabelText("데이터 분석 중... (통계 · Bin 집계)")
        self._status("데이터 분석 중...")
        QApplication.processEvents()
        try:
            self.last_result = rg.analyze(
                work_group, meta=rg.ReportMeta(),
                selector=rg.ItemSelector(selected_items=selected),
            )
        except Exception as exc:
            prog.close()
            QMessageBox.critical(self, "분석 실패", str(exc))
            self._status("분석 실패")
            self.btn_start.setEnabled(True)
            return
        prog.setValue(2)
        QApplication.processEvents()

        # 3) 요약 작성
        prog.setLabelText("요약 작성 중...")
        QApplication.processEvents()
        self._show_summary(self.last_result)
        prog.setValue(3)
        QApplication.processEvents()

        base = self.le_outname.text().strip() or _suggest_base_name(self.csv_paths)
        out = _build_output_path(Path(self.csv_paths[0]).parent, base)

        # 4) 시트/차트 생성 (시트 1개당 1스텝, offset 3)
        def _sheet_progress(done, total_s, name):
            prog.setLabelText(f"시트/차트 생성 중... ({name})   {done}/{total_s}")
            prog.setValue(3 + done)
            self._status(f"시트 생성 중... ({name})  {done}/{total_s}")
            QApplication.processEvents()

        prog.setLabelText("Excel 시트/차트 생성 중...")
        self._status(f"xlsx 생성 중... (Excel)  → {Path(out).name}")
        QApplication.processEvents()
        try:
            xlsx_writer.write(self.last_result, out, sheets=sheets,
                              colors=chart_colors.load_colors(),
                              progress_cb=_sheet_progress, raw_data=raw)
        except Exception as exc:
            prog.close()
            QMessageBox.critical(self, "생성 실패", str(exc))
            self._status("xlsx 생성 실패")
            self.btn_start.setEnabled(True)
            return

        # 5) Excel 파일 저장 마무리
        _step(total, "Excel 파일 저장 마무리 중...")
        prog.close()
        self.out_path = out
        self.btn_start.setEnabled(True)
        self.lbl_out.setText(f"저장됨: {out}")
        self._status(f"완료: {Path(out).name}  ('서버에 업로드' 가능)")

        # 자동 업로드 옵션
        if auto_upload:
            self._do_upload(self.out_path)

    def _show_summary(self, r):
        feat = r.summary_feature()
        lines = [
            f"Sources: {', '.join(r.sources)}",
            f"Total DUT: {r.total_dut}    Pass(Bin1): {feat['Pass (Bin 1)']}  ({r.pass_yield}%)",
            f"분석 항목: {len(r.subjects)}개   |   Fail Types: {feat['Fail Types']}",
            "",
            "[Major Fail Bins]",
        ]
        for i, b in enumerate(r.major_fail_bins(), start=1):
            lines.append(f"  {i}. bin {b.get('bin')}  -  {b.get('Main Fail subject')}  ({b.get('avg')}%)")
        lines += ["", f"issue(most-fail item) bins: {len(r.issue_rows)}건",
                  f"distribution 차트: {len(r.distributions)}개"]
        self.txt_summary.setPlainText("\n".join(lines))

    # ── 서버 업로드 ─────────────────────────────────────────────────────────
    def on_upload_local(self):
        """로컬에 있는 임의의 xlsx 를 직접 업로드 (분석 엔진 불필요)."""
        path, _ = QFileDialog.getOpenFileName(
            self, "업로드할 파일 선택", "", "Excel (*.xlsx);;모든 파일 (*.*)",
            options=QFileDialog.DontUseNativeDialog)
        if path:
            self._do_upload(path)

    def product_type(self):
        """메인 UI 에서 선택된 Product Type (라디오). 기본 MD."""
        for key, rb in self._pt_radios.items():
            if rb.isChecked():
                return key
        return "MD"

    def _save_product_type(self, *_):
        """Product Type 선택을 사용자별 설정에 저장 (다음 실행 때 복원)."""
        app_settings.set_setting("product_type", self.product_type())

    def _do_upload(self, path):
        """메타 팝업 입력 → 차트 PNG 렌더 → post_xlsx (소스 공통)."""
        # 메인 UI 의 Product Type 선택을 업로드 팝업 기본값으로 사용
        defaults = dict(self._last_upload or {})
        defaults["product_type"] = self.product_type()
        dlg = UploadDialog(self, defaults=defaults)
        if not dlg.exec_():
            return
        v = dlg.values()
        self._last_upload = v

        self.btn_upload_local.setEnabled(False)
        self._status("차트 변환 중... (Excel)")
        QApplication.processEvents()
        try:
            chart_pngs = chart_export.export_chart_pngs(path)
        except Exception:
            chart_pngs = []

        self._status(f"업로드 중... {Path(path).name} (차트 {len(chart_pngs)}장)")
        QApplication.processEvents()
        try:
            result = uploader.post_xlsx(
                path,
                product_type=v["product_type"],
                product=v["product"],
                lot_id=v["lot_id"],
                password=v["password"],
                chart_pngs=chart_pngs,
            )
        except Exception as exc:
            QMessageBox.critical(self, "업로드 실패", str(exc))
            self._status("업로드 실패")
            self.btn_upload_local.setEnabled(True)
            return

        sid = result.get("session_id", "?")
        charts = result.get("charts_saved", 0)
        QMessageBox.information(
            self, "업로드 완료",
            f"session_id: {sid}\n차트: {charts}장\n\n"
            f"브라우저에서 확인:\n{SERVER_BASE_URL}/pe/report/view/{sid}",
        )
        self._status(f"업로드 완료 (차트 {charts}장)")
        self.btn_upload_local.setEnabled(True)

    # ── version check (기존 로직 무변경) ────────────────────────────────────
    def check_for_update(self):
        try:
            manifest = version_check.fetch_latest()
        except Exception as exc:
            self.status.showMessage(f"버전 체크 실패: {exc}")
            return

        remote = manifest.get("version") or ""
        if not version_check.is_newer(remote, CURRENT_VERSION):
            self.status.showMessage(
                f"버전 체크 OK — 최신 ({CURRENT_VERSION}). Server: {SERVER_BASE_URL}")
            return

        reply = QMessageBox.question(
            self, "업데이트 사용 가능",
            f"신규 버전 {remote} 이(가) 있습니다.\n"
            f"현재: {CURRENT_VERSION}\n\n지금 다운로드 하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        url = manifest.get("url") or "/honey/download"
        expected = manifest.get("sha256") or None
        setup_name = manifest.get("file") or f"HoneySetup-{remote}.exe"
        dest = Path(tempfile.gettempdir()) / setup_name

        # 다운로드 진행바
        dlg = QProgressDialog("업데이트 다운로드 중...", "취소", 0, 100, self)
        dlg.setWindowTitle("Honey 업데이트")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        def _cb(done, total):
            if dlg.wasCanceled():
                return False
            dlg.setLabelText(f"업데이트 다운로드 중... ({done // (1024*1024)}MB"
                             + (f" / {total // (1024*1024)}MB)" if total else ")"))
            dlg.setValue(int(done * 100 / total) if total else 0)
            QApplication.processEvents()
            return True

        try:
            version_check.download_to(dest, url, expected_sha256=expected, progress_cb=_cb)
        except version_check.DownloadCancelled:
            dlg.close()
            self.status.showMessage("업데이트 취소됨")
            return
        except Exception as exc:
            dlg.close()
            QMessageBox.critical(self, "다운로드 실패", str(exc))
            self.status.showMessage("업데이트 실패")
            return
        dlg.setValue(100)
        dlg.close()

        if not updater.is_frozen():
            QMessageBox.information(
                self, "다운로드 완료 (개발 모드)",
                f"스크립트 실행 중이라 설치를 진행하지 않습니다.\n"
                f"설치본만 다운로드 완료:\n{dest}\n\n"
                f"(자동 설치는 빌드된 exe 에서 동작합니다.)",
            )
            self.status.showMessage("다운로드 완료 (개발 모드)")
            return

        QMessageBox.information(
            self, "업데이트 설치",
            f"새 버전 {remote} 을(를) 설치합니다.\n\n"
            "설치하는 동안 앱이 잠시 종료되며, 설치가 끝나면 자동으로 다시 실행됩니다.\n"
            "잠시만 기다려 주세요.",
        )
        try:
            updater.run_installer(dest)
        except Exception as exc:
            QMessageBox.critical(self, "설치 실행 실패", str(exc))
            self.status.showMessage("업데이트 실패")
            return
        self.status.showMessage("업데이트 설치 중... 앱을 종료합니다.")
        QApplication.quit()


def _install_excepthook():
    """슬롯에서 발생한 미처리 예외로 앱이 조용히 죽지 않도록, 메시지로 표시.

    PyQt5 는 슬롯의 미처리 예외 시 기본 excepthook 이면 abort 한다. 후킹하면
    앱을 유지하면서 오류를 보여줄 수 있다.
    """
    import traceback

    def hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        try:
            QMessageBox.critical(None, "오류가 발생했습니다", text[-3000:])
        except Exception:
            pass
        sys.__excepthook__(etype, value, tb)

    sys.excepthook = hook


def _apply_cute_font(app):
    """앱 전역 글씨체를 귀여운(둥근) 느낌으로. 설치된 첫 후보를 사용."""
    from PyQt5.QtGui import QFontDatabase
    available = set(QFontDatabase().families())
    # 귀여운/둥근 계열 우선순위 (설치돼 있는 첫 폰트 선택)
    candidates = ["Comic Sans MS", "Segoe Print", "Comic Neue",
                  "HY엽서L", "HY견고딕", "맑은 고딕"]
    family = next((c for c in candidates if c in available), None)
    font = QFont(family) if family else app.font()
    font.setPointSize(10)
    app.setFont(font)


def main():
    app = QApplication(sys.argv)
    _apply_cute_font(app)
    _install_excepthook()
    win = HoneyMainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

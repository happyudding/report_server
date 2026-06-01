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
import sys
import tempfile
from pathlib import Path

from PyQt5 import uic
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIntValidator
from PyQt5.QtWidgets import (
    QApplication, QDialog, QFileDialog, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressDialog,
)

from config import CURRENT_VERSION, SERVER_BASE_URL, D1_STORAGE_DIR
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

# 프리징(onedir) 시 _MEIPASS, 아니면 스크립트 폴더에서 .ui 탐색
_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
UI_PATH = os.path.join(_BASE_DIR, "honey_main.ui")
UPLOAD_UI_PATH = os.path.join(_BASE_DIR, "upload_dialog.ui")
D1_UI_PATH = os.path.join(_BASE_DIR, "d1_browser.ui")


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


def _suggest_base_name(csv_paths):
    """입력 파일명들로부터 결과물 base 이름을 rough 하게 유추 (시간 접미사 제외)."""
    stems = [Path(p).stem for p in csv_paths]
    base = _common_base(stems)
    base = base.strip(" _-") or "report"
    return f"{base}_report"


def _timestamp():
    """파일명용 현재 시각: 260601_0949 (YYMMDD_HHMM)."""
    return datetime.datetime.now().strftime("%y%m%d_%H%M")


def _build_output_path(out_dir, base):
    """base 이름 + 현재 시각 접미사로 최종 저장 경로 생성."""
    base = base.strip()
    if base.lower().endswith(".xlsx"):
        base = base[:-5]
    base = base.strip(" _-") or "report"
    return str(Path(out_dir) / f"{base}_{_timestamp()}.xlsx")


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

class HoneyMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        uic.loadUi(UI_PATH, self)
        self.status = self.statusbar
        self.setWindowTitle(f"Honey  v{CURRENT_VERSION}")
        self.status.showMessage(f"Server: {SERVER_BASE_URL}")

        self.csv_paths = []
        self._suppress_reorder = False  # list_csv 프로그램 변경 시 rowsMoved 무시
        self.group = None          # DfHoneyGroup
        self.last_result = None    # AnalysisResult
        self.out_path = None       # 생성된 xlsx 경로
        self._last_upload = None   # 마지막 업로드 메타 (팝업 프리필용)

        self.sheet_checks = {
            name: getattr(self, f"cb_sheet_{name}") for name in SHEET_OPTIONS
        }
        self._connect_signals()
        self._sync_yield_dependents()
        self._update_dut_mode_availability()

        if rg is None:
            self._disable_engine()
        QTimer.singleShot(500, self.check_for_update)

    def _connect_signals(self):
        self.btn_open_local.clicked.connect(self.on_open_local)
        self.btn_pick_csv.clicked.connect(self.on_browse_d1)
        # 입력 파일 드래그로 순서 변경 → 그룹 재구성 (맨 위 파일이 기준)
        self.list_csv.model().rowsMoved.connect(self._on_files_reordered)
        self.btn_sel_all.clicked.connect(lambda: self._check_all(True))
        self.btn_sel_none.clicked.connect(lambda: self._check_all(False))
        self.btn_sel_fail.clicked.connect(self._check_fail_only)
        # yield 미선택 시 fail_item / issue_table 도 선택 불가
        self.cb_sheet_yield.toggled.connect(self._sync_yield_dependents)
        self.btn_analyze.clicked.connect(self.on_analyze)
        self.btn_upload.clicked.connect(self.on_upload)
        self.btn_upload_local.clicked.connect(self.on_upload_local)

    def _disable_engine(self):
        # 분석 관련 기능만 비활성. 로컬 파일 직접 업로드는 엔진 없이도 동작하므로 유지.
        for name in ("btn_open_local", "btn_pick_csv", "btn_analyze", "btn_upload"):
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
        # 네이티브 대화상자는 frozen 앱에서 셸 확장/COM 충돌로 크래시할 수 있어 Qt 다이얼로그 사용
        paths, _ = QFileDialog.getOpenFileNames(
            self, "CSV/XLSX 파일 열기 (여러 개 가능)", "",
            "데이터 파일 (*.csv *.xlsx);;CSV (*.csv);;Excel (*.xlsx);;모든 파일 (*.*)",
            options=QFileDialog.DontUseNativeDialog)
        if paths:
            self._load_paths(paths)

    def on_browse_d1(self):
        os.makedirs(D1_STORAGE_DIR, exist_ok=True)
        dlg = D1BrowserDialog(self, D1_STORAGE_DIR)
        if not dlg.exec_():
            return
        paths = dlg.selected_paths()
        if not paths:
            QMessageBox.warning(self, "선택 없음", "가져올 파일을 선택하세요.")
            return
        self._load_paths(paths)

    def _load_paths(self, paths):
        """선택된 입력 파일들 → 리스트 채우기 + 그룹 로드 (신규 로드)."""
        self.csv_paths = list(paths)
        self._suppress_reorder = True
        try:
            self.list_csv.clear()
            for p in self.csv_paths:
                it = QListWidgetItem(Path(p).name)
                it.setData(Qt.UserRole, p)
                it.setToolTip(p)
                self.list_csv.addItem(it)
        finally:
            self._suppress_reorder = False
        self.le_outname.setText(_suggest_base_name(self.csv_paths))
        self._rebuild_group(warn=True)

    def _current_list_order(self):
        """list_csv 표시 순서대로의 경로 목록."""
        paths = [self.list_csv.item(i).data(Qt.UserRole)
                 for i in range(self.list_csv.count())]
        return [p for p in paths if p]

    def _rebuild_group(self, warn=False):
        """현재 self.csv_paths 순서로 그룹 재구성 + 항목 갱신.

        맨 위(첫) 파일이 units/항목명/Lower·Upper limit 의 기준이 된다 — 서로 다른
        유형의 파일이 섞여 들어와도 첫 파일 스키마를 기준으로 데이터가 처리된다.
        """
        paths = self.csv_paths
        if not paths:
            return
        self.lbl_csv.setText(f"{len(paths)}개 파일  ({Path(paths[0]).parent})")
        self._status("파일 로딩/검증 중...")
        QApplication.processEvents()
        try:
            self.group = rg.DfHoneyGroup.from_csvs(paths)
        except Exception as exc:
            QMessageBox.critical(self, "파일 로드 실패", str(exc))
            self._status("파일 로드 실패")
            self.group = None
            return

        if warn:
            issues = {n: v for n, v in self.group.validate().items() if v}
            if issues:
                msg = "\n".join(f"- {n}: {', '.join(v)}" for n, v in issues.items())
                QMessageBox.warning(self, "스키마 경고", f"일부 파일에 문제가 있습니다:\n{msg}")

        self._populate_items()
        self._update_dut_mode_availability()
        self.btn_upload.setEnabled(False)
        self.out_path = None
        self.lbl_out.setText("")
        self._status(f"{len(paths)}개 파일 (기준: {Path(paths[0]).name}). 항목/시트 선택 후 분석을 실행하세요.")

    def _on_files_reordered(self, *_):
        """list_csv 드래그 순서 변경 → csv_paths 갱신 후 그룹 재구성."""
        if self._suppress_reorder:
            return
        order = self._current_list_order()
        if not order or order == self.csv_paths:
            return
        self.csv_paths = order
        self._rebuild_group(warn=False)

    def _populate_items(self):
        self.list_items.clear()
        subjects = self.group.subjects()
        # default: 전체 선택
        for subj in subjects:
            item = QListWidgetItem(subj)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.list_items.addItem(item)

    def _check_all(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.list_items.count()):
            self.list_items.item(i).setCheckState(state)

    def _check_fail_only(self):
        if self.group is None:
            return
        fail_ids = set(self.group.fail_subject_ids())
        for i in range(self.list_items.count()):
            self.list_items.item(i).setCheckState(Qt.Checked if i in fail_ids else Qt.Unchecked)

    def _selected_items(self):
        return [self.list_items.item(i).text()
                for i in range(self.list_items.count())
                if self.list_items.item(i).checkState() == Qt.Checked]

    def _selected_sheets(self):
        return [n for n, cb in self.sheet_checks.items() if cb.isChecked()]

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
        ok = len(self.csv_paths) == 1
        if not ok:
            self.cb_mode_dut.setChecked(False)
        self.cb_mode_dut.setEnabled(ok)

    def _apply_modes(self, group):
        """선택된 데이터 정리 모드를 그룹에 적용. 문제 시 ValueError."""
        work = group
        if self.cb_mode_bin1.isChecked():
            work = work.filter_rows_by_bin("1")
            if not work.subjects() or all(len(md.scores) == 0
                                          for md in work.mass_data_map.values()):
                raise ValueError("Bin1 Only: Bin 이 1(Pass)인 데이터가 없습니다.")
        if self.cb_mode_dut.isChecked():
            if len(self.csv_paths) != 1:
                raise ValueError("DUT 정리는 입력 파일이 1개일 때만 가능합니다.")
            work = work.split_by_dut()
        return work

    # ── 분석 실행 → 자동 저장 ────────────────────────────────────────────────
    def on_analyze(self):
        if self.group is None:
            QMessageBox.warning(self, "입력 누락", "먼저 파일을 가져오세요.")
            return
        # 드래그 순서 변경 신호를 놓쳤어도 분석 직전에 현재 순서로 맞춘다 (기준 파일 보장)
        order = self._current_list_order()
        if order and order != self.csv_paths:
            self.csv_paths = order
            self._rebuild_group(warn=False)
            if self.group is None:
                return
        selected = self._selected_items()
        if not selected:
            QMessageBox.warning(self, "항목 누락", "분석할 항목을 1개 이상 선택하세요.")
            return
        sheets = self._selected_sheets()
        if not sheets:
            QMessageBox.warning(self, "시트 누락", "출력할 시트를 1개 이상 선택하세요.")
            return

        # 데이터 정리 모드 적용 (Bin1 Only → DUT 정리 순서로 그룹 변환)
        try:
            work_group = self._apply_modes(self.group)
        except ValueError as exc:
            QMessageBox.warning(self, "모드 적용 불가", str(exc))
            return

        self.btn_analyze.setEnabled(False)
        self.btn_upload.setEnabled(False)
        self._status("분석 중...")
        QApplication.processEvents()
        try:
            self.last_result = rg.analyze(
                work_group, meta=rg.ReportMeta(),
                selector=rg.ItemSelector(selected_items=selected),
            )
        except Exception as exc:
            QMessageBox.critical(self, "분석 실패", str(exc))
            self._status("분석 실패")
            self.btn_analyze.setEnabled(True)
            return

        self._show_summary(self.last_result)

        base = self.le_outname.text().strip() or _suggest_base_name(self.csv_paths)
        out = _build_output_path(Path(self.csv_paths[0]).parent, base)
        self._status(f"xlsx 생성/저장 중... (Excel)  → {Path(out).name}")
        QApplication.processEvents()
        try:
            xlsx_writer.write(self.last_result, out, sheets=sheets)
        except Exception as exc:
            QMessageBox.critical(self, "생성 실패", str(exc))
            self._status("xlsx 생성 실패")
            self.btn_analyze.setEnabled(True)
            return

        self.out_path = out
        self.btn_analyze.setEnabled(True)
        self.btn_upload.setEnabled(True)
        self.lbl_out.setText(f"저장됨: {out}")
        self._status(f"완료: {Path(out).name}  ('서버에 업로드' 가능)")

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
    def on_upload(self):
        """생성한 리포트(self.out_path) 업로드."""
        if not self.out_path or not Path(self.out_path).exists():
            QMessageBox.warning(self, "파일 없음", "먼저 분석을 실행해 xlsx 를 생성하세요.")
            return
        self._do_upload(self.out_path)

    def on_upload_local(self):
        """로컬에 있는 임의의 xlsx 를 직접 업로드 (분석 엔진 불필요)."""
        path, _ = QFileDialog.getOpenFileName(
            self, "업로드할 파일 선택", "", "Excel (*.xlsx);;모든 파일 (*.*)",
            options=QFileDialog.DontUseNativeDialog)
        if path:
            self._do_upload(path)

    def _do_upload(self, path):
        """메타 팝업 입력 → 차트 PNG 렌더 → post_xlsx (소스 공통)."""
        dlg = UploadDialog(self, defaults=self._last_upload)
        if not dlg.exec_():
            return
        v = dlg.values()
        self._last_upload = v

        self.btn_upload.setEnabled(False)
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
            self.btn_upload.setEnabled(self.out_path is not None)
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
        self.btn_upload.setEnabled(self.out_path is not None)
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


def main():
    app = QApplication(sys.argv)
    _install_excepthook()
    win = HoneyMainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

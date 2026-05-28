"""Honey 클라이언트 (PyQt5).

로컬 리포트 생성 워크플로우 단일 화면:
CSV 여러 개 → 로컬 분석(df_honey) → 출력 시트 선택 → '분석 실행' 시 입력 폴더에
xlsx 자동 저장(xlwings) → '서버에 업로드' 로 전송.

시작 시 server 버전 체크는 유지.
"""
import os
import sys
import tempfile
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIntValidator
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QStatusBar, QTextEdit, QVBoxLayout, QWidget,
)

from config import CURRENT_VERSION, SERVER_BASE_URL
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

PRODUCT_TYPES = ["MD", "PD", "PM", "SE"]
SHEET_OPTIONS = ["summary", "yield", "cpk", "fail_item", "issue_table", "distribution"]


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


def _derive_output_path(csv_paths):
    """입력 파일들이 있는 폴더에 추측한 파일명으로 저장 경로 생성."""
    stems = [Path(p).stem for p in csv_paths]
    base = _common_base(stems)
    out_dir = Path(csv_paths[0]).parent
    return str(out_dir / f"{base}_report.xlsx")


class HoneyMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Honey  v{CURRENT_VERSION}")
        self.resize(820, 960)
        self.csv_paths = []
        self.group = None          # DfHoneyGroup
        self.last_result = None    # AnalysisResult
        self.out_path = None       # 생성된 xlsx 경로
        self._build_ui()
        QTimer.singleShot(500, self.check_for_update)

    def _status(self, msg):
        self.status.showMessage(msg)

    # ── UI ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage(f"Server: {SERVER_BASE_URL}")

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)

        title = QLabel("로컬 리포트 생성")
        title.setStyleSheet("font-size: 16pt; font-weight: 600;")
        root.addWidget(title)

        if rg is None:
            warn = QLabel(
                "report_generator 모듈을 불러오지 못했습니다.\n"
                f"({_RG_IMPORT_ERROR})\n"
                "pandas / numpy / xlwings 설치 및 MS Excel 이 필요합니다."
            )
            warn.setStyleSheet("color: #b00;")
            warn.setWordWrap(True)
            root.addWidget(warn)
            root.addStretch(1)
            return

        # CSV 선택
        csv_row = QHBoxLayout()
        self.btn_pick_csv = QPushButton("CSV 파일 선택…")
        self.btn_pick_csv.clicked.connect(self.on_pick_csv)
        csv_row.addWidget(self.btn_pick_csv)
        self.lbl_csv = QLabel("선택된 파일 없음")
        self.lbl_csv.setStyleSheet("color: #555;")
        csv_row.addWidget(self.lbl_csv, 1)
        root.addLayout(csv_row)

        self.list_csv = QListWidget()
        self.list_csv.setMaximumHeight(70)
        root.addWidget(self.list_csv)

        # 메타 입력
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.cb_product_type = QComboBox()
        self.cb_product_type.addItems(PRODUCT_TYPES)
        self.le_product = QLineEdit(); self.le_product.setPlaceholderText("예: A1")
        self.le_lot_id = QLineEdit(); self.le_lot_id.setPlaceholderText("예: L001")
        self.le_revision = QLineEdit(); self.le_revision.setPlaceholderText("예: r1")
        self.le_process = QLineEdit(); self.le_process.setPlaceholderText("예: P1")
        self.le_password = QLineEdit()
        self.le_password.setPlaceholderText("숫자 4자리")
        self.le_password.setEchoMode(QLineEdit.Password)
        self.le_password.setMaxLength(4)
        self.le_password.setValidator(QIntValidator(0, 9999))
        form.addRow("Product Type:", self.cb_product_type)
        form.addRow("Product:", self.le_product)
        form.addRow("LOT ID:", self.le_lot_id)
        form.addRow("Revision:", self.le_revision)
        form.addRow("Process:", self.le_process)
        form.addRow("비밀번호:", self.le_password)
        root.addLayout(form)

        # 출력 시트 선택
        sheet_box = QGroupBox("출력 시트")
        sheet_h = QHBoxLayout(sheet_box)
        self.sheet_checks = {}
        for name in SHEET_OPTIONS:
            cb = QCheckBox(name)
            cb.setChecked(True)
            self.sheet_checks[name] = cb
            sheet_h.addWidget(cb)
        sheet_h.addStretch(1)
        root.addWidget(sheet_box)

        # item select (subject)
        items_box = QGroupBox("분석 항목 (item select)")
        items_v = QVBoxLayout(items_box)
        sel_row = QHBoxLayout()
        self.btn_sel_all = QPushButton("전체"); self.btn_sel_all.clicked.connect(lambda: self._check_all(True))
        self.btn_sel_none = QPushButton("해제"); self.btn_sel_none.clicked.connect(lambda: self._check_all(False))
        self.btn_sel_fail = QPushButton("Fail 항목만"); self.btn_sel_fail.clicked.connect(self._check_fail_only)
        sel_row.addWidget(self.btn_sel_all); sel_row.addWidget(self.btn_sel_none)
        sel_row.addWidget(self.btn_sel_fail); sel_row.addStretch(1)
        items_v.addLayout(sel_row)
        self.list_items = QListWidget()
        self.list_items.setMinimumHeight(180)
        items_v.addWidget(self.list_items)
        root.addWidget(items_box, 1)

        # 액션 버튼
        act_row = QHBoxLayout()
        self.btn_analyze = QPushButton("분석 실행 (자동 저장)")
        self.btn_analyze.setStyleSheet(
            "QPushButton { padding: 10px 18px; font-weight: 600;"
            " background: #4a90e2; color: white; border-radius: 6px; }"
            " QPushButton:hover { background: #357abd; }"
            " QPushButton:disabled { background: #aaa; }"
        )
        self.btn_analyze.clicked.connect(self.on_analyze)
        self.btn_upload = QPushButton("서버에 업로드")
        self.btn_upload.clicked.connect(self.on_upload)
        self.btn_upload.setEnabled(False)
        act_row.addWidget(self.btn_analyze, 1)
        act_row.addWidget(self.btn_upload, 1)
        root.addLayout(act_row)

        self.lbl_out = QLabel("")
        self.lbl_out.setStyleSheet("color: #2a6;")
        self.lbl_out.setWordWrap(True)
        root.addWidget(self.lbl_out)

        # 결과 요약
        self.txt_summary = QTextEdit()
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setPlaceholderText("분석 결과 요약이 여기에 표시됩니다.")
        self.txt_summary.setMinimumHeight(150)
        root.addWidget(self.txt_summary, 1)

    # ── CSV 선택 → 그룹 로드 → 항목 채우기 ──────────────────────────────────
    def on_pick_csv(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "CSV 파일 선택 (여러 개 가능)", "", "CSV/Excel (*.csv *.xlsx)")
        if not paths:
            return
        self.csv_paths = paths
        self.list_csv.clear()
        for p in paths:
            self.list_csv.addItem(Path(p).name)
        self.lbl_csv.setText(f"{len(paths)}개 파일  ({Path(paths[0]).parent})")

        self._status("CSV 로딩/검증 중...")
        QApplication.processEvents()
        try:
            self.group = rg.DfHoneyGroup.from_csvs(paths)
        except Exception as exc:
            QMessageBox.critical(self, "CSV 로드 실패", str(exc))
            self._status("CSV 로드 실패")
            self.group = None
            return

        issues = {n: v for n, v in self.group.validate().items() if v}
        if issues:
            msg = "\n".join(f"- {n}: {', '.join(v)}" for n, v in issues.items())
            QMessageBox.warning(self, "스키마 경고", f"일부 파일에 문제가 있습니다:\n{msg}")

        self._populate_items()
        self.btn_upload.setEnabled(False)
        self.out_path = None
        self.lbl_out.setText("")
        self._status(f"{len(paths)}개 CSV 로드 완료. 항목/시트를 선택하고 분석을 실행하세요.")

    def _populate_items(self):
        self.list_items.clear()
        subjects = self.group.subjects()
        fail_ids = set(self.group.fail_subject_ids())
        for idx, subj in enumerate(subjects):
            item = QListWidgetItem(subj)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if idx in fail_ids else Qt.Unchecked)
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

    def _meta(self):
        return rg.ReportMeta(
            product_type=self.cb_product_type.currentText(),
            product=self.le_product.text().strip(),
            lot_id=self.le_lot_id.text().strip(),
            revision=self.le_revision.text().strip(),
            process=self.le_process.text().strip(),
        )

    # ── 분석 실행 → 자동 저장 ────────────────────────────────────────────────
    def on_analyze(self):
        if self.group is None:
            QMessageBox.warning(self, "입력 누락", "먼저 CSV 파일을 선택하세요.")
            return
        selected = self._selected_items()
        if not selected:
            QMessageBox.warning(self, "항목 누락", "분석할 항목을 1개 이상 선택하세요.")
            return
        sheets = self._selected_sheets()
        if not sheets:
            QMessageBox.warning(self, "시트 누락", "출력할 시트를 1개 이상 선택하세요.")
            return

        self.btn_analyze.setEnabled(False)
        self.btn_upload.setEnabled(False)
        self._status("분석 중...")
        QApplication.processEvents()
        try:
            self.last_result = rg.analyze(
                self.group, meta=self._meta(),
                selector=rg.ItemSelector(selected_items=selected),
            )
        except Exception as exc:
            QMessageBox.critical(self, "분석 실패", str(exc))
            self._status("분석 실패")
            self.btn_analyze.setEnabled(True)
            return

        self._show_summary(self.last_result)

        out = _derive_output_path(self.csv_paths)
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

    # ── 서버 업로드 (기존 uploader 재사용) ──────────────────────────────────
    def on_upload(self):
        if not self.out_path or not Path(self.out_path).exists():
            QMessageBox.warning(self, "파일 없음", "먼저 분석을 실행해 xlsx 를 생성하세요.")
            return
        product = self.le_product.text().strip()
        lot_id = self.le_lot_id.text().strip()
        password = self.le_password.text().strip()
        err = _validate_meta(product, lot_id, password)
        if err:
            QMessageBox.warning(self, "입력 오류", err)
            return

        self.btn_upload.setEnabled(False)
        self._status("차트 변환 중... (Excel)")
        QApplication.processEvents()
        try:
            chart_pngs = chart_export.export_chart_pngs(self.out_path)
        except Exception:
            chart_pngs = []

        self._status(f"업로드 중... {Path(self.out_path).name} (차트 {len(chart_pngs)}장)")
        QApplication.processEvents()
        try:
            result = uploader.post_xlsx(
                self.out_path,
                product_type=self.cb_product_type.currentText(),
                product=product,
                lot_id=lot_id,
                password=password,
                chart_pngs=chart_pngs,
            )
        except Exception as exc:
            QMessageBox.critical(self, "업로드 실패", str(exc))
            self._status("업로드 실패")
            self.btn_upload.setEnabled(True)
            return

        sid = result.get("session_id", "?")
        charts = result.get("charts_saved", 0)
        QMessageBox.information(
            self, "업로드 완료",
            f"session_id: {sid}\n차트: {charts}장\n\n"
            f"브라우저에서 확인:\n{SERVER_BASE_URL}/pe/report/view/{sid}",
        )
        self._status(f"업로드 완료 (차트 {charts}장)")
        self.btn_upload.setEnabled(True)

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

        self.status.showMessage(f"새 버전 {remote} 다운로드 중...")
        QApplication.processEvents()

        if updater.is_frozen():
            target = updater.current_exe_path()
            staged = updater.staging_path(target)
            try:
                version_check.download_to(staged, url, expected_sha256=expected)
            except Exception as exc:
                QMessageBox.critical(self, "다운로드 실패", str(exc))
                self.status.showMessage("업데이트 실패")
                return
            try:
                updater.apply_update(staged, target)
            except Exception as exc:
                QMessageBox.critical(self, "업데이트 적용 실패", str(exc))
                self.status.showMessage("업데이트 실패")
                return

            QMessageBox.information(
                self, "업데이트 적용",
                f"새 버전 {remote} 으로 교체 후 자동 재시작됩니다.\n앱을 종료합니다.",
            )
            QApplication.quit()
            return

        target = Path(tempfile.gettempdir()) / (manifest.get("file") or f"Honey-{remote}.exe")
        try:
            version_check.download_to(target, url, expected_sha256=expected)
        except Exception as exc:
            QMessageBox.critical(self, "다운로드 실패", str(exc))
            self.status.showMessage("업데이트 실패")
            return
        QMessageBox.information(
            self, "다운로드 완료 (개발 모드)",
            f"스크립트 실행 중이라 교체 대상 exe 가 없습니다.\n"
            f"다운로드만 완료:\n{target}\n\n"
            f"(자동 교체는 빌드된 exe 에서 동작합니다.)",
        )
        self.status.showMessage("다운로드 완료 (개발 모드)")


def main():
    app = QApplication(sys.argv)
    win = HoneyMainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

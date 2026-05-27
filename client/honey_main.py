"""Honey 클라이언트 (PyQt5 스켈레톤).

UI 가 단순한 이유: 외부 report generator 툴이 .xlsx 까지 만들어두고, 이 앱은
- 시작 시 server 와 버전 체크
- 사용자가 (product_type, product, lot_id) + .xlsx 파일을 골라 업로드
만 담당하기 때문.

추후 외부 report generator 코드를 이 앱에 merge 하면 UI 가 확장될 예정.
"""
import sys
import tempfile
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QStatusBar, QVBoxLayout,
    QWidget,
)

from config import CURRENT_VERSION, SERVER_BASE_URL
import uploader
import version_check

PRODUCT_TYPES = ["MD", "PD", "PM", "SE"]


class HoneyMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Honey  v{CURRENT_VERSION}")
        self.resize(440, 280)
        self._build_ui()
        QTimer.singleShot(500, self.check_for_update)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Report 업로드")
        title.setStyleSheet("font-size: 16pt; font-weight: 600;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.cb_product_type = QComboBox()
        self.cb_product_type.addItems(PRODUCT_TYPES)
        self.le_product = QLineEdit()
        self.le_product.setPlaceholderText("예: A1")
        self.le_lot_id = QLineEdit()
        self.le_lot_id.setPlaceholderText("예: L001")
        form.addRow("Product Type:", self.cb_product_type)
        form.addRow("Product:", self.le_product)
        form.addRow("LOT ID:", self.le_lot_id)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_upload = QPushButton("xlsx 선택 후 업로드")
        self.btn_upload.setStyleSheet(
            "QPushButton { padding: 10px 18px; font-weight: 600;"
            " background: #4a90e2; color: white; border-radius: 6px; }"
            " QPushButton:hover { background: #357abd; }"
            " QPushButton:disabled { background: #aaa; }"
        )
        self.btn_upload.clicked.connect(self.on_upload_clicked)
        btn_row.addWidget(self.btn_upload)
        layout.addLayout(btn_row)

        layout.addStretch(1)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage(f"Server: {SERVER_BASE_URL}")

    # ── upload ─────────────────────────────────────────────────────────────
    def on_upload_clicked(self):
        product = self.le_product.text().strip()
        lot_id = self.le_lot_id.text().strip()
        if not product or not lot_id:
            QMessageBox.warning(self, "입력 누락", "Product 와 LOT ID 를 모두 입력하세요.")
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "xlsx 파일 선택", "", "Excel files (*.xlsx)")
        if not path:
            return

        self.btn_upload.setEnabled(False)
        self.status.showMessage(f"업로드 중... {Path(path).name}")
        QApplication.processEvents()
        try:
            result = uploader.post_xlsx(
                path,
                product_type=self.cb_product_type.currentText(),
                product=product,
                lot_id=lot_id,
            )
        except Exception as exc:
            QMessageBox.critical(self, "업로드 실패", str(exc))
            self.status.showMessage("업로드 실패")
            self.btn_upload.setEnabled(True)
            return

        sid = result.get("session_id", "?")
        akey = result.get("analysis_key", "?")
        rows = result.get("rows_saved", 0)
        QMessageBox.information(
            self, "업로드 완료",
            f"session_id: {sid}\nanalysis_key: {akey[:16]}...\nyield rows: {rows}\n\n"
            f"브라우저에서 확인:\n{SERVER_BASE_URL}/pe/report/view/{sid}",
        )
        self.status.showMessage("업로드 완료")
        self.btn_upload.setEnabled(True)

    # ── version check ──────────────────────────────────────────────────────
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
        target = Path(tempfile.gettempdir()) / (manifest.get("file") or f"Honey-{remote}.exe")
        try:
            version_check.download_to(target, url, expected_sha256=expected or None)
        except Exception as exc:
            QMessageBox.critical(self, "다운로드 실패", str(exc))
            return

        QMessageBox.information(
            self, "다운로드 완료",
            f"새 버전이 다운로드되었습니다:\n{target}\n\n"
            f"이 앱을 종료 후 수동으로 교체해주세요.\n"
            f"(자동 교체는 다음 릴리즈에서 추가 예정)",
        )


def main():
    app = QApplication(sys.argv)
    win = HoneyMainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

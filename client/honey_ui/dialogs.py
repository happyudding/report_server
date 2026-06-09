"""Honey dialogs split from the main window module."""
import sqlite3
import sys
from pathlib import Path

from PyQt5 import uic
from PyQt5.QtCore import Qt, QStringListModel
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QColorDialog,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

import chart_colors
import config as _client_config

SHEET_OPTIONS = ["summary", "yield", "cpk", "fail_item", "issue_table", "distribution"]

_BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
UPLOAD_UI_PATH = _BASE_DIR / "upload_dialog.ui"
ORDER_UI_PATH = _BASE_DIR / "file_order.ui"
SETTINGS_UI_PATH = _BASE_DIR / "report_settings.ui"


def _load_part_ids(db_path: str) -> list:
    """stdinfo DB에서 part_id 목록을 로드. 실패하면 빈 리스트."""
    if not db_path:
        return []
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute("SELECT part_id FROM products ORDER BY part_id").fetchall()
        con.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def _validate_meta(product, lot_id, password):
    """Return validation error string for upload metadata, or None."""
    if not product or not lot_id:
        return "Product 와 LOT ID 를 모두 입력하세요."
    if password and (len(password) != 4 or not password.isdigit()):
        return "비밀번호는 숫자 4자리 또는 빈칸(미설정)으로 입력하세요."
    return None


class UploadDialog(QDialog):
    def __init__(self, parent=None, defaults=None):
        super().__init__(parent)
        uic.loadUi(str(UPLOAD_UI_PATH), self)
        self._part_ids = _load_part_ids(_client_config.STDINFO_DB_PATH)
        if self._part_ids:
            _model = QStringListModel(self._part_ids, self)
            _comp = QCompleter(_model, self)
            _comp.setFilterMode(Qt.MatchContains)
            _comp.setCaseSensitivity(Qt.CaseInsensitive)
            self.le_product.setCompleter(_comp)
        self._pt_radios = {
            "MDDI": self.rb_pt_MDDI, "PDDI": self.rb_pt_PDDI,
            "PMIC": self.rb_pt_PMIC, "SECURITY": self.rb_pt_SECURITY,
        }
        self.buttonBox.accepted.connect(self._on_ok)
        self.buttonBox.rejected.connect(self.reject)
        if defaults:
            self._pt_radios.get(defaults.get("product_type", "MDDI"),
                                self.rb_pt_MDDI).setChecked(True)
            self.le_product.setText(defaults.get("product", ""))
            self.le_lot_id.setText(defaults.get("lot_id", ""))
            self.le_revision.setText(defaults.get("revision", ""))
            self.le_process.setText(defaults.get("process", ""))
            self.le_edm_link.setText(defaults.get("edm_link", ""))

    def product_type(self):
        for key, rb in self._pt_radios.items():
            if rb.isChecked():
                return key
        return "MDDI"

    def _on_ok(self):
        product = self.le_product.text().strip()
        err = _validate_meta(product,
                             self.le_lot_id.text().strip(),
                             self.le_password.text().strip())
        if err:
            QMessageBox.warning(self, "입력 오류", err)
            return
        if self._part_ids and product not in self._part_ids:
            QMessageBox.warning(self, "입력 오류",
                f"'{product}'은(는) 등록된 Part ID가 아닙니다.\n"
                "목록에서 선택하거나 검색어를 확인하세요.")
            return
        self.accept()

    def values(self):
        return {
            "product_type": self.product_type(),
            "product": self.le_product.text().strip(),
            "lot_id": self.le_lot_id.text().strip(),
            "revision": self.le_revision.text().strip(),
            "process": self.le_process.text().strip(),
            "edm_link": self.le_edm_link.text().strip(),
            "password": self.le_password.text().strip(),
        }


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
        self.setWindowTitle("Chart 색상 편집 (Legend 1~48)")
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
                                    f"{idx + 1}번 색상 선택")
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
            QMessageBox.warning(self, "저장 실패", f"색상 저장에 실패했습니다:\n{exc}")
            return
        self.accept()


class FileOrderDialog(QDialog):
    def __init__(self, parent, paths):
        super().__init__(parent)
        uic.loadUi(str(ORDER_UI_PATH), self)
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


class ReportSettingsDialog(QDialog):
    def __init__(self, parent, group, csv_count, product_type=None):
        super().__init__(parent)
        uic.loadUi(str(SETTINGS_UI_PATH), self)
        self.group = group
        self.csv_count = csv_count
        self.product_type = product_type or ""
        self._fail_item_blocked = self.product_type == "MDDI"
        self._filename_overrides = None
        self.sheet_checks = {
            name: getattr(self, f"cb_sheet_{name}") for name in SHEET_OPTIONS
        }

        self.btn_all_right.clicked.connect(self._move_all_right)
        self.btn_sel_right.clicked.connect(self._move_selected_right)
        self.btn_sel_left.clicked.connect(self._move_selected_left)
        self.btn_all_left.clicked.connect(self._move_all_left)
        self.btn_sel_fail.clicked.connect(self._select_fail_only)
        self.list_items_avail.itemDoubleClicked.connect(
            lambda it: self._move(self.list_items_avail, self.list_items_sel, [it]))
        self.list_items_sel.itemDoubleClicked.connect(
            lambda it: self._move(self.list_items_sel, self.list_items_avail, [it]))
        self.cb_sheet_yield.toggled.connect(self._sync_yield_dependents)
        self.btn_filename_change.clicked.connect(self.on_edit_filenames)
        self.btn_chart_colors.clicked.connect(self.on_edit_chart_colors)
        self.btn_confirm.clicked.connect(self._on_confirm)
        self.btn_confirm.setMinimumHeight(36)
        self.btn_confirm.setDefault(True)
        self.btn_confirm.setStyleSheet(
            "QPushButton { font-size: 16pt; font-weight: 700; "
            "padding: 7px 21px; background: #2f7de1; color: white; "
            "border: 1px solid #1f5fb5; border-radius: 5px; }"
            "QPushButton:hover { background: #3f8cf0; }"
            "QPushButton:pressed { background: #1f65c8; }"
        )
        self.cb_raw_data.setChecked(False)
        self.cb_raw_data.setToolTip("체크하면 입력 원본 데이터를 Raw Data 시트로 추가합니다.")
        default_sheets = {"yield", "cpk", "distribution"}
        for name, cb in self.sheet_checks.items():
            cb.setChecked(name in default_sheets)
        self.cb_raw_data.toggled.connect(self._update_dut_mode_availability)
        self.cb_mode_dut.toggled.connect(lambda checked: (
            self.cb_raw_data.setEnabled(not checked),
            self.cb_raw_data.setChecked(False) if checked else None,
        ))
        self._populate_items()
        self._sync_yield_dependents()
        self._update_dut_mode_availability()

    def _make_item(self, idx, text):
        it = QListWidgetItem(text)
        it.setData(Qt.UserRole, idx)
        return it

    def _populate_items(self):
        self.list_items_avail.clear()
        self.list_items_sel.clear()
        for i, s in enumerate(self.group.subjects()):
            self.list_items_sel.addItem(self._make_item(i, s))

    def _resort(self, lw):
        items = [lw.takeItem(0) for _ in range(lw.count())]
        items.sort(key=lambda it: it.data(Qt.UserRole))
        for it in items:
            lw.addItem(it)

    def _move(self, src, dst, items):
        for it in items:
            row = src.row(it)
            if row >= 0:
                dst.addItem(src.takeItem(row))
        self._resort(dst)

    def _move_all_right(self):
        items = [self.list_items_avail.item(i) for i in range(self.list_items_avail.count())]
        self._move(self.list_items_avail, self.list_items_sel, items)

    def _move_all_left(self):
        items = [self.list_items_sel.item(i) for i in range(self.list_items_sel.count())]
        self._move(self.list_items_sel, self.list_items_avail, items)

    def _move_selected_right(self):
        self._move(self.list_items_avail, self.list_items_sel,
                   list(self.list_items_avail.selectedItems()))

    def _move_selected_left(self):
        self._move(self.list_items_sel, self.list_items_avail,
                   list(self.list_items_sel.selectedItems()))

    def _select_fail_only(self):
        if self.group is None:
            return
        subjects = self.group.subjects()
        fail = set(self.group.fail_subject_names())
        self.list_items_avail.clear()
        self.list_items_sel.clear()
        for idx, subj in enumerate(subjects):
            target = self.list_items_sel if subj in fail else self.list_items_avail
            target.addItem(self._make_item(idx, subj))

    def _sync_yield_dependents(self, *_):
        enabled = self.cb_sheet_yield.isChecked()
        for name in ("fail_item", "issue_table"):
            cb = self.sheet_checks[name]
            if not enabled:
                cb.setChecked(False)
            cb.setEnabled(enabled)

    def _update_dut_mode_availability(self):
        ok = self.csv_count == 1
        if not ok:
            self.cb_mode_dut.setChecked(False)
        self.cb_mode_dut.setEnabled(ok)

    def _current_filenames(self):
        if self._filename_overrides is not None:
            return list(self._filename_overrides)
        return self.group.names() if self.group is not None else []

    def on_edit_filenames(self):
        names = self._current_filenames()
        if not names:
            QMessageBox.information(self, "Filename", "입력 파일이 없습니다.")
            return
        text, ok = QInputDialog.getText(
            self, "Name Change",
            "각 입력 파일의 Filename(legend)을 콤마(,)로 구분해 입력하세요.\n"
            f"(파일 {len(names)}개)",
            text=",".join(names))
        if not ok:
            return
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != len(names):
            QMessageBox.warning(
                self, "개수 불일치",
                f"입력 파일은 {len(names)}개인데 {len(parts)}개를 입력했습니다.\n"
                "앞에서부터 매칭하며, 빈 항목은 기존 파일명을 사용합니다.")
        self._filename_overrides = [
            (parts[i] if i < len(parts) else "") or names[i]
            for i in range(len(names))
        ]

    def filename_overrides(self):
        return self._filename_overrides

    def on_edit_chart_colors(self):
        dlg = ColorEditorDialog(self)
        dlg.exec_()

    def selected_items(self):
        return [self.list_items_sel.item(i).text()
                for i in range(self.list_items_sel.count())]

    def selected_sheets(self):
        return [name for name, cb in self.sheet_checks.items() if cb.isChecked()]

    def mode_bin1(self):
        return self.cb_mode_bin1.isChecked()

    def mode_dut(self):
        return self.cb_mode_dut.isChecked()

    def auto_upload(self):
        return self.cb_auto_upload.isChecked()

    def raw_data(self):
        """Return whether original df_honey data should be added as Raw Data sheets."""
        return self.cb_raw_data.isChecked()

    def _on_confirm(self):
        if not self.selected_items():
            QMessageBox.warning(self, "항목 누락", "분석할 항목을 1개 이상 선택하세요.")
            return
        if not self.selected_sheets():
            QMessageBox.warning(self, "시트 누락", "출력할 시트를 1개 이상 선택하세요.")
            return
        self.accept()


        fail = set()
        try:
            for row in self.group.fail_item_rows():
                name = row.get("item") or row.get("Main Fail subject") or row.get("subject")
                if name:
                    fail.add(str(name))
        except Exception:
            fail = set()
        self._move_all_left()
        for i in reversed(range(self.list_items_avail.count())):
            it = self.list_items_avail.item(i)
            if it.text() in fail:
                self.list_items_sel.addItem(self.list_items_avail.takeItem(i))
        self._resort(self.list_items_sel)

    def _sync_yield_dependents(self, *_):
        yield_enabled = self.cb_sheet_yield.isChecked()
        self.cb_sheet_fail_item.setEnabled(yield_enabled and not self._fail_item_blocked)
        if not yield_enabled or self._fail_item_blocked:
            self.cb_sheet_fail_item.setChecked(False)
        self.cb_sheet_issue_table.setEnabled(yield_enabled)
        if not yield_enabled:
            self.cb_sheet_issue_table.setChecked(False)

    def _update_dut_mode_availability(self):
        raw_on = self.cb_raw_data.isChecked()
        one_file = self.csv_count == 1
        self.cb_mode_dut.setEnabled(one_file and not raw_on)
        if not self.cb_mode_dut.isEnabled():
            self.cb_mode_dut.setChecked(False)

    def _current_filenames(self):
        names = []
        for i in range(self.csv_count):
            try:
                names.append(self.group.names()[i])
            except Exception:
                names.append("")
        return names

    def on_edit_filenames(self):
        current = self._current_filenames()
        text, ok = QInputDialog.getText(
            self,
            "FileName Change",
            "입력 파일별 Legend 이름을 쉼표(,)로 구분해 입력하세요.\n"
            "빈칸은 기존 이름을 유지합니다.",
            text=", ".join(current),
        )
        if not ok:
            return
        parts = [p.strip() for p in text.split(",")]
        while len(parts) < self.csv_count:
            parts.append("")
        overrides = []
        seen = {}
        for i, part in enumerate(parts[:self.csv_count]):
            base = part or current[i]
            key = base
            if key in seen:
                seen[key] += 1
                base = f"{key}_{seen[key]}"
            else:
                seen[key] = 1
            overrides.append(base)
        self._filename_overrides = overrides

    def filename_overrides(self):
        return self._filename_overrides

    def on_edit_chart_colors(self):
        dlg = ColorEditorDialog(self)
        dlg.exec_()

    def selected_items(self):
        return [self.list_items_sel.item(i).text()
                for i in range(self.list_items_sel.count())]

    def selected_sheets(self):
        return [name for name, cb in self.sheet_checks.items() if cb.isChecked()]

    def mode_bin1(self):
        return self.cb_mode_bin1.isChecked()

    def mode_dut(self):
        return self.cb_mode_dut.isChecked()

    def auto_upload(self):
        return self.cb_auto_upload.isChecked()

    def raw_data(self):
        """Return whether original df_honey data should be added as Raw Data sheets."""
        return self.cb_raw_data.isChecked()

    def _on_confirm(self):
        if not self.selected_items():
            QMessageBox.warning(self, "항목 누락", "분석할 항목을 1개 이상 선택하세요.")
            return
        if not self.selected_sheets():
            QMessageBox.warning(self, "시트 누락", "출력할 시트를 1개 이상 선택하세요.")
            return
        self.accept()

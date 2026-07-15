"""Honey dialogs split from the main window module."""
import sys
import threading
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtCore import Qt, QStringListModel, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QColorDialog,
    QComboBox,
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

import app_settings
import chart_colors
from transport import uploader

SHEET_OPTIONS = ["summary", "yield", "cpk", "fail_item", "issue_table", "distribution",
                 "histogram"]

# product_type → family_product 허용 목록. 정본은
# eval_analyzer/eval_engine/rules/product_taxonomy.yaml (eval.db 검증 기준) — 값 변경 시
# 그 yaml 과 반드시 동기화할 것(서버 eval _validate_product_meta 가 이 값으로 강제 검증).
FAMILY_PRODUCTS = {
    "MDDI":     ["MX", "AQUA", "CHINA", "MDDI_ETC"],
    "PMIC":     ["SOC", "MEMORY", "DISPLAY", "IF", "PMIC_ETC"],
    "SECURITY": ["NFC_ESE", "ESE", "Contactless", "SECU_ETC"],
    "PDDI":     ["LCD", "PDDI_IT", "QDOLED", "PDDI_ETC"],
    "TCON":     ["TV", "TCON_IT", "TCON_ETC"],
}

_BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
UPLOAD_UI_PATH = _BASE_DIR / "upload_dialog.ui"
ORDER_UI_PATH = _BASE_DIR / "file_order.ui"
SETTINGS_UI_PATH = _BASE_DIR / "report_settings.ui"


def _validate_meta(product, lot_id, password):
    """Return validation error string for upload metadata, or None."""
    if not product or not lot_id:
        return "Product 와 LOT ID 를 모두 입력하세요."
    if password and (len(password) != 4 or not password.isdigit()):
        return "비밀번호는 숫자 4자리 또는 빈칸(미설정)으로 입력하세요."
    return None


class UploadDialog(QDialog):
    # 백그라운드 fetch 결과 전달 (ids, ok) — cross-thread 라 자동 queued connection
    _part_ids_ready = pyqtSignal(list, bool)

    def __init__(self, parent=None, defaults=None, show_password=True):
        super().__init__(parent)
        uic.loadUi(str(UPLOAD_UI_PATH), self)
        # Web Report 업로드는 PIN 을 쓰지 않는다(편집/삭제는 로그인·업로더 일치로 인증).
        # show_password=False 이면 비밀번호 행을 숨겨 입력을 요구하지 않는다 → values()의
        # password 는 빈 문자열이 되어 서버로 전송되지 않는다.
        if not show_password:
            self.label_pw.setVisible(False)
            self.le_password.setVisible(False)
        # Part ID 목록은 서버 GET(타임아웃 10s)이라 생성자에서 동기 호출하면 팝업이
        # 그만큼 늦게 뜬다 — 백그라운드 스레드로 받고 도착하면 completer 를 붙인다.
        self._part_ids = []
        self.le_product.setPlaceholderText("Part ID 목록 불러오는 중...")
        self._part_ids_ready.connect(self._apply_part_ids)
        threading.Thread(target=self._fetch_part_ids_bg, daemon=True,
                         name="upload-dialog-part-ids").start()
        self.buttonBox.accepted.connect(self._on_ok)
        self.buttonBox.rejected.connect(self.reject)
        # Product Type 은 메인창에서 이미 선택된 값을 그대로 재사용한다 (팝업엔 표시 안 함).
        defaults = defaults or {}
        self._product_type = defaults.get("product_type", "MDDI")
        # Family Product 드롭다운을 폼 최상단에 추가 (현재 product_type 의 허용 목록).
        # 기본 선택: 직전 업로드값 → 옵션 저장값(product_type 별) → 첫 항목.
        self.cbo_family = QComboBox()
        _families = FAMILY_PRODUCTS.get(self._product_type, [])
        self.cbo_family.addItems(_families)
        _saved_families = app_settings.get_setting("family_product")
        _opt_family = (_saved_families.get(self._product_type)
                       if isinstance(_saved_families, dict) else None)
        _default_family = defaults.get("family_product") or _opt_family
        if _default_family in _families:
            self.cbo_family.setCurrentText(_default_family)
        self.formLayout.insertRow(0, "Family*:", self.cbo_family)
        self.le_product.setText(defaults.get("product", ""))
        self.le_lot_id.setText(defaults.get("lot_id", ""))
        self.le_process.setText(defaults.get("process", ""))

    def _fetch_part_ids_bg(self):
        try:
            ids = list(uploader.fetch_part_ids() or [])
            ok = True
        except Exception:  # noqa: BLE001
            ids, ok = [], False
        try:
            self._part_ids_ready.emit(ids, ok)
        except RuntimeError:
            pass   # 다이얼로그가 이미 닫혀 C++ 객체가 파괴된 경우

    def _apply_part_ids(self, ids, ok):
        self._part_ids = list(ids)
        if self._part_ids:
            _model = QStringListModel(self._part_ids, self)
            _comp = QCompleter(_model, self)
            _comp.setFilterMode(Qt.MatchFlag.MatchContains)
            _comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            self.le_product.setCompleter(_comp)
            self.le_product.setPlaceholderText("")
        else:
            self.le_product.setPlaceholderText("Part ID 목록을 불러오지 못했습니다 (서버 확인)")
            if not ok and self.isVisible():
                QMessageBox.warning(
                    self, "Part ID 로드 실패",
                    "서버에서 Part ID 목록을 불러오지 못했습니다.\n"
                    "네트워크/서버 상태를 확인하세요. Product 검색이 비활성화됩니다.")

    def product_type(self):
        return self._product_type

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
            "family_product": self.cbo_family.currentText(),
            "product": self.le_product.text().strip(),
            "lot_id": self.le_lot_id.text().strip(),
            "revision": "",
            "process": self.le_process.text().strip(),
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
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
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


class OptionsDialog(QDialog):
    """Honey 통합 옵션 — 기본 Product Type + Distribution 차트 색.

    색 편집은 기존 ColorEditorDialog 를 그대로 재사용(버튼 → 모달)한다.
    """
    # honey_main._pt_radios 와 동일 집합 — 두 곳이 어긋나지 않도록 함께 관리할 것.
    PRODUCT_TYPES = ["MDDI", "PDDI", "PMIC", "SECURITY", "TCON"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Options")
        root = QVBoxLayout(self)

        # (1) 기본 Product Type + Family — 다음 실행 때 자동 선택될 값 (나란히 배치)
        root.addWidget(QLabel("기본 Product Type / Family (다음 실행 때 자동 선택)"))
        # product_type 별 family 선택을 기억한다 (Honey 꺼져도 settings.json 에 영속).
        saved_family = app_settings.get_setting("family_product")
        self._family_sel = dict(saved_family) if isinstance(saved_family, dict) else {}
        self._pt_prev = None

        pt_row = QHBoxLayout()
        self.cbo_pt = QComboBox()
        self.cbo_pt.addItems(self.PRODUCT_TYPES)
        self.cbo_family = QComboBox()
        pt_row.addWidget(self.cbo_pt)
        pt_row.addWidget(self.cbo_family)
        root.addLayout(pt_row)

        cur = app_settings.get_setting("product_type")
        if cur in self.PRODUCT_TYPES:
            self.cbo_pt.setCurrentText(cur)
        self._populate_family(self.cbo_pt.currentText())
        self.cbo_pt.currentTextChanged.connect(self._on_pt_changed)

        # (2) Distribution 색 — 기존 ColorEditorDialog 재사용
        btn_colors = QPushButton("Distribution 색 편집...")
        btn_colors.clicked.connect(lambda: ColorEditorDialog(self).exec())
        root.addWidget(btn_colors)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _populate_family(self, pt):
        """pt 의 family 목록으로 콤보를 재구성하고 저장된 선택을 복원(없으면 첫 항목)."""
        families = FAMILY_PRODUCTS.get(pt, [])
        self.cbo_family.blockSignals(True)
        self.cbo_family.clear()
        self.cbo_family.addItems(families)
        saved = self._family_sel.get(pt)
        if saved in families:
            self.cbo_family.setCurrentText(saved)
        self.cbo_family.blockSignals(False)
        self._pt_prev = pt

    def _on_pt_changed(self, pt):
        # PT 전환 직전 이전 PT 의 family 선택을 기억 → 여러 PT 를 오가며 각각 유지.
        if self._pt_prev and self.cbo_family.count():
            self._family_sel[self._pt_prev] = self.cbo_family.currentText()
        self._populate_family(pt)

    def selected_product_type(self):
        return self.cbo_pt.currentText()

    def _on_ok(self):
        pt = self.cbo_pt.currentText()
        if self.cbo_family.count():
            self._family_sel[pt] = self.cbo_family.currentText()
        app_settings.set_setting("product_type", pt)
        app_settings.set_setting("family_product", self._family_sel)
        self.accept()


class FileOrderDialog(QDialog):
    def __init__(self, parent, paths):
        super().__init__(parent)
        uic.loadUi(str(ORDER_UI_PATH), self)
        for p in paths:
            it = QListWidgetItem(Path(p).name)
            it.setData(Qt.ItemDataRole.UserRole, p)
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
        return [self.list_order.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.list_order.count())]


class ReportSettingsDialog(QDialog):
    # 접기/펴기 그룹: (헤더 QToolButton, 본문 QWidget, 표시 라벨)
    _COLLAPSIBLE = [
        ("hdr_chart", "body_chart", "Chart"),
        ("hdr_stats", "body_stats", "Statistics"),
        ("hdr_summary", "body_summary", "Summary"),
        ("hdr_others", "body_others", "Others"),
        ("hdr_extra", "body_extra", "추가파일생성"),
    ]
    # 그룹별 all 체크박스 → 그 그룹의 자식 체크박스 이름들
    _GROUP_ALL = {
        "cb_all_chart": ["cb_sheet_distribution", "cb_sheet_histogram", "cb_hist_report"],
        "cb_all_stats": ["cb_sheet_yield", "cb_sheet_cpk"],
        "cb_all_summary": ["cb_sheet_summary", "cb_sheet_issue_table", "cb_sheet_fail_item"],
        "cb_all_others": ["cb_raw_data", "cb_mode_bin1", "cb_mode_compare",
                          "cb_mode_dut", "cb_outlier"],
    }

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
        # 접기/펴기 그룹 + 그룹별 all 토글 배선, Item 리스트 세로 간격 축소
        self._wire_collapsible()
        self._wire_group_all()
        for lw in (self.list_items_avail, self.list_items_sel):
            lw.setSpacing(0)
            # Item 명 폰트를 1pt 작게 → 항목 세로 간격도 함께 좁아진다(uniform item sizes).
            f = lw.font()
            ps = f.pointSizeF()
            if ps > 0:
                f.setPointSizeF(max(ps - 1.0, 6.0))
            else:
                px = f.pixelSize()
                if px > 0:
                    f.setPixelSize(max(px - 1, 8))
            lw.setFont(f)
            lw.setStyleSheet("QListWidget::item { padding: 0px 2px; margin: 0px; }")
            lw.setUniformItemSizes(True)
        self._populate_items()
        self._sync_yield_dependents()
        self._update_dut_mode_availability()
        self._update_compare_mode_availability()

    def _wire_collapsible(self):
        """각 그룹 헤더(QToolButton) 클릭 시 본문 높이만 접었다 폈다 하고 ▾/▸ 화살표를 갱신.
        본문을 숨기지(setVisible) 않고 높이만 0 으로 접어 가로폭을 그대로 유지한다 →
        한 열을 접어도 Chart/Statistics/Summary/Others 헤더가 좌우로 밀리지 않는다."""
        for hdr_name, body_name, label in self._COLLAPSIBLE:
            hdr = getattr(self, hdr_name)
            body = getattr(self, body_name)
            self._set_body_collapsed(body, not hdr.isChecked())
            self._set_hdr_text(hdr, label)
            hdr.toggled.connect(
                lambda checked, h=hdr, b=body, lbl=label:
                    (self._set_body_collapsed(b, not checked), self._set_hdr_text(h, lbl)))

    @staticmethod
    def _set_body_collapsed(body, collapsed):
        """본문 높이만 0 으로 접는다(가로폭 유지). 접어도 열 폭이 그대로라 헤더가 안 움직인다."""
        body.setMaximumHeight(0 if collapsed else 16777215)

    @staticmethod
    def _set_hdr_text(hdr, label):
        hdr.setText(("▾  " if hdr.isChecked() else "▸  ") + label)

    def _wire_group_all(self):
        """그룹별 'all' 체크박스 ↔ 그 그룹 자식 체크박스 동기화."""
        for all_name, child_names in self._GROUP_ALL.items():
            all_cb = getattr(self, all_name)
            children = [getattr(self, c) for c in child_names]
            all_cb.clicked.connect(
                lambda checked, cs=children:
                    [c.setChecked(checked) for c in cs if c.isEnabled()])
            for child in children:
                child.toggled.connect(
                    lambda _checked=False, a=all_cb, cs=children: self._sync_all_cb(a, cs))
            self._sync_all_cb(all_cb, children)

    @staticmethod
    def _sync_all_cb(all_cb, children):
        all_cb.blockSignals(True)
        all_cb.setChecked(all(c.isChecked() for c in children))
        all_cb.blockSignals(False)

    def _make_item(self, idx, text):
        it = QListWidgetItem(text)
        it.setData(Qt.ItemDataRole.UserRole, idx)
        return it

    def _populate_items(self):
        self.list_items_avail.clear()
        self.list_items_sel.clear()
        for i, s in enumerate(self.group.subjects()):
            self.list_items_sel.addItem(self._make_item(i, s))

    def _resort(self, lw):
        items = [lw.takeItem(0) for _ in range(lw.count())]
        items.sort(key=lambda it: it.data(Qt.ItemDataRole.UserRole))
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

    def _update_compare_mode_availability(self):
        """입력 파일이 정확히 2개일 때만 Compare Mode 활성화."""
        ok = self.csv_count == 2
        if not ok:
            self.cb_mode_compare.setChecked(False)
        self.cb_mode_compare.setEnabled(ok)

    def mode_compare(self):
        return self.cb_mode_compare.isChecked()

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

    def selected_items(self):
        return [self.list_items_sel.item(i).text()
                for i in range(self.list_items_sel.count())]

    def selected_sheets(self):
        return [name for name, cb in self.sheet_checks.items() if cb.isChecked()]

    def mode_bin1(self):
        return self.cb_mode_bin1.isChecked()

    def mode_dut(self):
        return self.cb_mode_dut.isChecked()

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

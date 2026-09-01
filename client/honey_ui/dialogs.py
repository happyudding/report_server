"""Honey dialogs split from the main window module."""
import sys
import threading
from pathlib import Path

from PyQt6 import uic
from PyQt6.QtCore import Qt, QPoint, QRect, QStringListModel, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import app_settings
import chart_colors
import eval_sensitivity
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
        # Save Name(세션 이름) 칸 — 서버 report_session.file_name 이 되는 값. 호출부가
        # defaults["file_name"] 으로 메인창 Save Name(또는 자동 제안값)을 넣어 주면 업로드
        # 직전에 한 번 더 고칠 수 있다. SessionMetaDialog 의 Session Name 과 같은 필드를
        # 공유한다(라벨만 다름).
        self._name_label = "Save Name"
        self.le_session_name = QLineEdit(str(defaults.get("file_name") or ""))
        self.le_session_name.setToolTip(
            "검색결과 목록과 세션 상단바(Session_name)에 표시되는 이름")
        self.formLayout.insertRow(0, f"{self._name_label}*:", self.le_session_name)
        self.le_product.setText(defaults.get("product", ""))
        self.le_lot_id.setText(defaults.get("lot_id", ""))
        self.le_process.setText(defaults.get("process", ""))
        # STEP 은 .ui 기본값 L2 — 직전 업로드값/세션값이 있으면 그것을 우선한다.
        if defaults.get("step"):
            self.le_step.setText(str(defaults["step"]))

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
            # 후보 '개수'와 '실제 값 예시'를 함께 노출한다. 자동완성이 안 뜰 때 ① 목록을
            # 못 받았다 ② 입력 형식이 목록과 다르다 를 사용자가 스스로 구분할 수 있고,
            # 예시가 실제 part_id 표기법을 그대로 알려준다.
            _n = len(self._part_ids)
            self.le_product.setPlaceholderText(f"Part ID {_n}건 (예: {self._part_ids[0]})")
            self.le_product.setToolTip(
                f"서버 Part ID 후보 {_n}건 — 일부를 입력하면 목록이 뜹니다.\n"
                "예: " + ", ".join(self._part_ids[:3]))
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
        if not self.le_session_name.text().strip():
            QMessageBox.warning(self, "입력 오류", f"{self._name_label} 을 입력하세요.")
            return
        product = self.le_product.text().strip()
        err = _validate_meta(product,
                             self.le_lot_id.text().strip(),
                             self.le_password.text().strip())
        if err:
            QMessageBox.warning(self, "입력 오류", err)
            return
        if not self._confirm_unknown_part_id(product):
            return
        self.accept()

    def _confirm_unknown_part_id(self, product):
        """목록에 없는 Product 를 차단하지 않고 확인만 받는다. 진행하면 True.

        _validate_meta 는 '무조건 막는' 하드 검증이고 이쪽은 넘어갈 수 있는 소프트 경고라
        계약이 달라 분리했다. 미등록 값이어도 업로드는 성공하지만 서버의 product_info
        lookup(web_report/ingest.py)이 비어 세션 기준정보 컬럼이 NULL 이 되고 web report
        상단바(WF Size/Gross Die/...)가 빈칸이 된다 — 그 결과를 알리고 고르게 한다.
        목록을 못 받았을 때(_part_ids 가 빔)는 검사 자체를 건너뛴다(기존 동작).
        """
        if not self._part_ids or product in self._part_ids:
            return True
        reply = QMessageBox.question(
            self, "등록되지 않은 Part ID",
            f"'{product}'은(는) 서버 Part ID 목록({len(self._part_ids)}건)에 없습니다.\n"
            "이대로 업로드하면 기준정보(Wafer Size / Gross Die / Package 등)가\n"
            "Web Report 상단에 표시되지 않습니다.\n\n"
            "그래도 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    def values(self):
        return {
            "file_name": self.le_session_name.text().strip(),
            "product_type": self.product_type(),
            "family_product": self.cbo_family.currentText(),
            "product": self.le_product.text().strip(),
            "lot_id": self.le_lot_id.text().strip(),
            "revision": "",
            "process": self.le_process.text().strip(),
            # Web Report 의 STEP 표시(P2)를 대신할 값 — 서버가 세션 옵션에 실어 두고
            # 조회 시점에 바꾼다(web_report/metrics._apply_step_label). 원본은 불변.
            "step": self.le_step.text().strip(),
            "password": self.le_password.text().strip(),
        }


class SessionMetaDialog(UploadDialog):
    """업로드한 세션의 메타를 나중에 고치는 창 — 업로드 다이얼로그를 그대로 재사용한다.

    Part ID 백그라운드 조회·자동완성·미등록 Part ID 확인 경고·Family 콤보·맨 위 이름 칸이
    전부 부모 것이다. 다른 점은 셋뿐: (1) 이름 칸 라벨이 Session Name(값 = 서버
    report_session.file_name — 세션 안 Filename(원본 소스 파일명)과는 별개), (2) 비밀번호 행
    숨김, (3) Product Type 은 세션 값 고정(편집 대상 아님 — 부모가 defaults 에서 받은 값을
    그대로 쓴다).
    """

    def __init__(self, parent, session):
        super().__init__(parent, defaults={
            "product_type": (session.get("product_type") or "MDDI"),
            "family_product": session.get("family_product") or "",
            "product": session.get("product") or "",
            "lot_id": session.get("lot_id") or "",
            "process": session.get("process") or "",
            "step": session.get("webreport_step") or "",
            "file_name": session.get("file_name") or "",
        }, show_password=False)
        self.setWindowTitle("세션 정보 수정")
        self._name_label = "Session Name"
        self.formLayout.labelForField(self.le_session_name).setText(
            f"{self._name_label}*:")


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


class GaugeSlider(QWidget):
    """1~5 + 사용자설정 6단계 눈금 슬라이더 (○─○─○─○─○─○).

    QSlider 를 쓰지 않는 이유: 마지막 칸이 숫자 단계가 아니라 **'사용자설정'** 이라
    연속 값이 아니고, 그 칸은 사용자가 직접 고를 수 없는 *표시 전용* 상태다(값을 직접
    입력해야 그리로 간다). 그래서 눈금 6개를 직접 그리고 클릭한 칸을 고른다.

    value: 1~5 = 그 단계, 0 = 사용자설정(마지막 칸).
    """

    CUSTOM = 0
    LEVELS = 5
    STOPS = 6
    _PAD = 40          # 좌우 여백 — 양끝 라벨('1'·'사용자설정' 76px 박스)이 위젯 밖으로 안 나가게
    _RADIUS = 6

    changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = eval_sensitivity.DEFAULT_LEVEL
        self._enabled = True
        self.setFixedHeight(36)
        # 눈금 6개 + '사용자설정' 라벨이 겹치지 않는 최소 폭. 고정폭인 이유는 행마다
        # 눈금 위치가 같아야 세로로 훑을 때 단계가 한눈에 비교되기 때문이다.
        # 라벨 10pt 로 키운 만큼 넓혔다 — 좁히면 '사용자설정' 이 이웃 눈금과 겹친다.
        # 2026-09-01 340→440: 마지막 칸 '사용자설정' 이 잘려 보여 창 전체와 함께 1.3배.
        self.setFixedWidth(440)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = value if value in range(0, self.LEVELS + 1) else self.CUSTOM
        self.update()

    def setGaugeEnabled(self, on):
        """게이지 고정 그룹(LOW_CPK)은 클릭을 막고 흐리게 그린다."""
        self._enabled = bool(on)
        self.setCursor(Qt.CursorShape.PointingHandCursor if on
                       else Qt.CursorShape.ArrowCursor)
        self.update()

    def _stop_x(self, index):
        span = max(1, self.width() - 2 * self._PAD)
        return self._PAD + span * index / (self.STOPS - 1)

    def _index_of_value(self):
        return self.STOPS - 1 if self._value == self.CUSTOM else self._value - 1

    def mousePressEvent(self, event):
        if not self._enabled:
            return
        x = event.position().x()
        idx = min(range(self.STOPS), key=lambda i: abs(self._stop_x(i) - x))
        # 마지막 칸(사용자설정)은 클릭으로 못 간다 — 값을 직접 입력해야 도달하는 상태다.
        # 클릭을 무시하지 않고 안내하면 "왜 안 눌리지" 를 없앨 수 있지만, 창이 조용한 편이
        # 나아 여기서는 그냥 무시한다(라벨이 회색이라 눌리지 않음이 보인다).
        if idx >= self.LEVELS:
            return
        self._value = idx + 1
        self.update()
        self.changed.emit(self._value)

    def paintEvent(self, _event):
        from PyQt6.QtGui import QPainter, QPen, QBrush
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = 12
        line = QColor("#c3ccd8") if self._enabled else QColor("#e2e6ec")
        p.setPen(QPen(line, 2))
        p.drawLine(int(self._stop_x(0)), y, int(self._stop_x(self.STOPS - 1)), y)

        active = self._index_of_value()
        for i in range(self.STOPS):
            x = int(self._stop_x(i))
            on = (i == active)
            custom_stop = (i == self.STOPS - 1)
            if not self._enabled:
                fill, edge = QColor("#f2f4f7"), QColor("#d5dae1")
            elif on and custom_stop:
                fill, edge = QColor("#b8860b"), QColor("#8a6508")   # 사용자설정 = 다른 색
            elif on:
                fill, edge = QColor("#2f6fd0"), QColor("#2559a8")
            else:
                fill, edge = QColor("#ffffff"), QColor("#b6c0cc")
            p.setPen(QPen(edge, 1.5))
            p.setBrush(QBrush(fill))
            r = self._RADIUS + (2 if on else 0)
            p.drawEllipse(QPoint(x, y), r, r)

            label = "사용자설정" if custom_stop else str(i + 1)
            if self._enabled and on:
                p.setPen(QPen(QColor("#8a6508") if custom_stop else QColor("#2559a8")))
            else:
                p.setPen(QPen(QColor("#98a2b0") if self._enabled else QColor("#c6ccd4")))
            f = p.font()
            f.setPointSize(10)
            f.setBold(on)
            p.setFont(f)
            rect = QRect(x - 38, y + 9, 76, 18)
            p.drawText(rect, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                       label)
        p.end()


class EvalSensitivityDialog(QDialog):
    """AI Comment 민감도 게이지 — signature 그룹별 rough(1)~tight(5) + 값 직접 입력.

    한 그룹 = 한 행이다: [SIGNATURE 영문] [6단계 슬라이더] [threshold 를 **세로로** 나열].
    그룹 8개가 한 화면에 다 보여야 하므로(사용자 지정) 접이식이 아니라 큰 창이고,
    threshold 가 세로라 창은 가로보다 **세로로** 길다.

    상호작용 3가지:
      · 전체 게이지를 움직이면 전 그룹이 그 단계로 따라간다(직접 입력도 해제).
      · 그룹 게이지를 움직이면 그 줄의 값 입력란이 **즉시** 그 단계 값으로 바뀐다.
      · 값을 직접 고치면 그 줄이 '사용자설정' 칸으로 옮겨간다(게이지 선택 해제).

    ⚠ 단계표(레벨별 숫자)를 여기 복제하지 않는다 — 정본은 서버
    `eval_analyzer/eval_engine/rules/sensitivity.yaml` 이고 카탈로그로 받아온다.
    사본을 두면 사용자가 고른 "3단계" 와 서버가 아는 "3단계" 가 갈린다.
    """

    LEVELS = 5
    NAME_WIDTH = 190          # SIGNATURE 영문 원문이 안 잘리는 폭 (글자 13px 기준)
    KEY_WIDTH = 240           # 가장 긴 키(subpop_density_gap_strong) 기준 (12px)
    # 그룹 1행 최소 높이 — 키가 1~2개인 그룹도 게이지 높이(36)에 딱 붙지 않게 벌린다
    # (2026-09-01 "행이 너무 짧다"). 다만 **고정값이면 1080p 에서 8행이 화면을 넘겨
    # 스크롤이 생긴다**(사용자 요구: 세로·가로 어느 쪽도 스크롤 금지). 그래서 상한/하한만
    # 두고 실제 값은 `_row_min_height()` 가 화면 높이에서 역산한다.
    ROW_MIN_MAX = 92          # 큰 화면에서의 상한 (이 이상 벌려도 허전하기만 하다)
    ROW_MIN_FLOOR = 38        # 하한 — 게이지(36)에 딱 붙는 값. 여기까지 내려가는 것은
                              # 1080p 미만의 작은 화면뿐이고, 그마저 스크롤보다는 낫다
    _KEY_LINE_H = 28          # threshold 한 줄(입력란 높이와 같게 유지)
    # threshold 줄 사이. 키 5개 그룹(TAIL·BIMODALITY)은 이 값이 행 높이를 그대로 정하고,
    # 그 행은 최소높이로 못 줄이는 **고정 지출**이라 1080p 예산을 여기서 잡아먹는다.
    _KEY_GAP = 3
    _SAFETY = 24              # 실기 폰트·DPI 편차 흡수용 여유 (스크롤 재발 방지)
    _catalog_ready = pyqtSignal(object, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI Comment 민감도")
        # 폭은 (이름 190 + 게이지 440 + 키 240 + 값 90 + 기본값 힌트) 합이 들어가는 값이다 —
        # 모자라면 가로 스크롤바가 생겨 값 입력란이 화면 밖으로 밀린다.
        # 높이는 내용에 맞춰 `_fit_height()` 가 정한다(그룹 수·키 수는 카탈로그가 정하므로
        # 고정값으로 두면 아래가 텅 비거나 잘린다).
        # 2026-09-01 1060→1380(1.3배): 게이지 '1~사용자설정' 이 잘리던 것.
        # 실제 하한은 `_fit_width()` 가 내용에서 재서 건다(창을 좁히면 가로 스크롤이 아니라
        # 그냥 잘리는 구조라, 좁힐 수 없게 막는 편이 안전하다).
        self.resize(1380, 640)
        self._settings = eval_sensitivity.load_settings()
        self._catalog = eval_sensitivity.load_cached_catalog()
        self._rows = {}          # group_id → {"steps": [...], "inputs": {key: QLineEdit}}
        self._loading = QLabel("서버에서 민감도 기준을 불러오는 중…")

        # 1080p(작업영역 ~1040)에서 그룹 8개가 스크롤 없이 들어가야 한다(사용자 지정).
        # 스크롤영역 밖 여백(chrome)이 그만큼 행에서 뺏어가므로 여백·안내문을 조인다.
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(11, 7, 11, 7)
        info = QLabel(
            "AI Comment 가 이슈를 얼마나 민감하게 잡을지 정합니다.  "
            "1 rough(굵직한 것만) ← 3 기본 → 5 tight(꼼꼼히).  "
            "값을 직접 입력하면 그 줄은 '사용자설정'이 됩니다.")
        # **1줄 고정**(wordWrap off + 고정 높이). 켜 두면 폭에 따라 2줄이 되는데, 그 hint
        # 54px 가 행 예산에서 그대로 빠져 아래 그룹이 잘린다(실제 렌더는 26px 인데도).
        # 마우스오버 안내는 아래 설명바가 이미 하므로 문구에서 뺐다.
        info.setWordWrap(False)
        info.setFixedHeight(24)
        info.setStyleSheet("color:#555; font-size:12px;")
        root.addWidget(info)

        all_row = QHBoxLayout()
        lbl_all = QLabel("ALL")
        lbl_all.setFixedWidth(self.NAME_WIDTH)
        lbl_all.setStyleSheet("font-weight:700; font-size:13px;")
        all_row.addWidget(lbl_all)
        self._all_gauge = GaugeSlider()
        self._all_gauge.changed.connect(self._on_all)
        all_row.addWidget(self._all_gauge)
        btn_reset = QPushButton("초기화")
        btn_reset.setFixedWidth(70)
        btn_reset.clicked.connect(lambda: self._on_all(eval_sensitivity.DEFAULT_LEVEL))
        all_row.addWidget(btn_reset)
        all_row.addStretch(1)
        root.addLayout(all_row)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color:#dfe3e9;")
        root.addWidget(line)

        # 그룹이 8개 + threshold 세로 나열이라 세로가 길다 — 화면보다 길어지면 스크롤한다.
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(14)
        inner = QWidget()
        inner.setLayout(self._grid)
        self._scroll = QScrollArea()
        self._scroll.setWidget(inner)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 가로 스크롤은 끄고 세로만 쓴다 — 가로로 밀리면 값 입력란이 화면 밖으로 나간다.
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self._scroll, 1)
        root.addWidget(self._loading)

        self._help = QLabel(" ")
        # wordWrap 을 끈다 — 켜 두면 긴 설명이 2~3줄로 늘어나며 그만큼 행 예산을 먹고,
        # 늘어난 만큼 아래 그룹이 잘려 스크롤이 생긴다(높이가 내용따라 출렁이는 것도 산만).
        # 넘치는 문구는 말줄임으로 처리하고 전문은 툴팁으로 준다.
        self._help.setWordWrap(False)
        self._help.setFixedHeight(26)
        self._help.setStyleSheet(
            "color:#445; background:#f4f7fb; border:1px solid #dfe6ef;"
            "border-radius:6px; padding:3px 8px; font-size:12px;")
        root.addWidget(self._help)

        # "제안 제외" — 2026-09-02 사용자 요청으로 메인 창 AI Comment 줄에서 이리로 옮겼다
        # (민감도와 함께 "AI Comment 를 어떻게 뽑을지" 설정이라 한자리에 모은다).
        # 저장 키는 종전 그대로 `ai_no_suggest` 다 — 키를 바꾸면 이미 켜 둔 PC 의 설정이
        # 조용히 풀린다. 저장은 OK 시점(`_on_ok`)에만 한다(취소하면 되돌아가야 한다).
        self.chk_no_suggest = QCheckBox("제안 제외 — [제안] 문장을 만들지 않는다")
        self.chk_no_suggest.setToolTip(
            "체크하면 [제안] 문장을 만들지 않고 과거 사례만 AI Comment 에 표시합니다.\n"
            "LLM 을 호출하지 않아 업로드 후 대기 시간이 없습니다.")
        self.chk_no_suggest.setChecked(bool(app_settings.get_setting("ai_no_suggest")))
        root.addWidget(self.chk_no_suggest)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._catalog_ready.connect(self._apply_catalog)
        if self._catalog:
            self._apply_catalog(self._catalog, True)     # 캐시본으로 먼저 그린다
        # 캐시가 있어도 최신본을 받아 둔다(단계표가 서버에서 바뀌었을 수 있다).
        threading.Thread(target=self._fetch_catalog_bg, daemon=True).start()

    # ── 카탈로그 로드 ───────────────────────────────────────────────────────
    def _fetch_catalog_bg(self):
        try:
            catalog = uploader.fetch_eval_sensitivity_catalog()
            ok = True
        except Exception:  # noqa: BLE001
            catalog, ok = None, False
        try:
            self._catalog_ready.emit(catalog, ok)
        except RuntimeError:
            pass   # 다이얼로그가 이미 닫혀 C++ 객체가 파괴된 경우

    def _apply_catalog(self, catalog, ok):
        if catalog:
            self._catalog = catalog
            eval_sensitivity.save_cached_catalog(catalog)
        if not self._catalog:
            self._loading.setText(
                "서버에서 민감도 기준을 불러오지 못했습니다. 네트워크를 확인한 뒤 다시 열어 주세요.")
            return
        self._loading.setVisible(False)
        self._build_rows()

    # ── 행 구성 ─────────────────────────────────────────────────────────────
    def _signature_label(self, group) -> str:
        """행 이름 = **SIGNATURE 영문 원문**. 한 그룹이 여러 룰이면 줄바꿈해 전부 보인다.

        한글 설명을 빼는 이유(2026-08-28 사용자 지시): 화면 어디서나 signature 는 영문
        원문으로 나오는데(Issue Table Signature 컬럼·`/pe/eval` 트레이스) 여기만 한글이면
        같은 것을 가리키는지 알기 어렵다.
        """
        sigs = [str(s) for s in (group.get("signatures") or []) if s]
        return "<br>".join(sigs) or str(group.get("id") or "")

    def _fit_rows_to_screen(self, n_rows):
        """행 최소 높이를 **화면 예산에 맞춰 조인다** — 세로 스크롤이 안 생기는 최대치.

        고정값(종전 90)은 큰 화면에선 맞지만 1080p 에선 8행이 작업영역을 넘겨 스크롤을
        만든다. 사용자 요구는 "1080p 한 화면 + 스크롤 없음"이므로, 남는 세로를 키가 적은
        그룹들에게 나눠 주되 예산을 넘지 않는 선까지만 준다.

        행이 내용만으로 갖는 높이는 **카탈로그에서 직접 계산**한다(`_natural_row_height`).
        Qt 에 묻는 방법을 두 번 시도했다가 둘 다 틀렸다:
          · `inner.sizeHint()` 반복 측정 ✗ — show() 뒤 캐시돼 두 번째 적용부터 옛 값.
          · 행 위젯의 `sizeHint()` ✗ — 재구성 직후 위젯이 아직 창에 붙기 전이라 **전부 0**
            (이 창은 캐시본→서버본으로 두 번 그리는데, 그 두 번째가 사용자가 보는 화면이다).
        위젯 구조는 이 클래스가 직접 만든 것이라 계산이 안전하고 타이밍도 안 탄다.
        """
        if self._grid is None or n_rows <= 0:
            return
        groups = list((self._catalog or {}).get("groups") or [])[:n_rows]
        if len(groups) < n_rows:
            return
        # 여유 SAFETY: 실기의 폰트·DPI·테마가 여기 계산보다 몇 px 크게 나올 수 있다.
        # 딱 맞게 채우면 그 몇 px 때문에 스크롤바가 되살아난다(사용자 요구는 "스크롤 없음").
        budget = (int(self._avail_height() * 0.97)
                  - self._chrome_height() - 8 - self._SAFETY)

        # 각 행이 **내용만으로** 갖는 높이 = 그 행 세 칸(이름·게이지·threshold)의 sizeHint
        # 중 최대. 위젯에 직접 물으므로 배치 전/후와 무관하게 같은 값이 나온다.
        #   · `inner.sizeHint()` 를 후보마다 재는 방식 ✗ — show() 뒤 캐시돼 **두 번째
        #     적용부터 옛 값**이 온다(이 창은 캐시본→서버본으로 두 번 그린다).
        #   · `cellRect()` ✗ — 아직 배치 안 된 첫 호출에서 실제와 다른 값이 나온다
        #     (해상도가 달라도 같은 값이 나오는 것으로 들통났다).
        natural = [self._natural_row_height(g) for g in groups]
        m = self._grid.contentsMargins()
        fixed = (self._grid.verticalSpacing() * max(0, n_rows - 1)
                 + m.top() + m.bottom())

        best = self.ROW_MIN_FLOOR
        for h in range(self.ROW_MIN_FLOOR, self.ROW_MIN_MAX + 1, 2):
            if sum(max(nat, h) for nat in natural) + fixed <= budget:
                best = h
            else:
                break
        for r in range(n_rows):
            self._grid.setRowMinimumHeight(r, best)
        self._grid.invalidate()
        self._grid.activate()

    def _natural_row_height(self, group) -> int:
        """행이 **내용만으로** 갖는 높이 — 세 칸(이름·게이지·threshold) 중 가장 큰 것.

        실측 대조값(Malgun Gothic 9pt): 키 1개 28 / 2개 55 / 5개 136,
        signature 5줄 75, 게이지 36. → 줄 하나가 24, 줄간격이 `_KEY_GAP`,
        아래 여백 4 로 두면 1/2/5개가 28/55/136 으로 실측과 맞는다.

        ⚠ 이 값이 실측보다 **작으면** 예산을 넘겨 스크롤이 생긴다(크면 여유가 남을 뿐이라
        안전 측). 줄 높이·간격 상수를 바꾸면 여기 계수도 같이 확인할 것.
        """
        n_keys = len(group.get("keys") or [])
        # threshold 칸: 줄(입력란 24) × 개수 + 줄간격 + 아래 여백(4).
        line = self._KEY_LINE_H - 4
        keys_h = (n_keys * line
                  + max(0, n_keys - 1) * self._KEY_GAP + 4) if n_keys else 0
        # 이름 칸: signature 를 <br> 로 이어 붙이므로 줄 수만큼 높아진다(줄당 15px).
        n_lines = max(1, len([s for s in (group.get("signatures") or []) if s]))
        name_h = n_lines * 15
        return max(36, keys_h, name_h)

    def _avail_height(self) -> int:
        screen = self.screen() if hasattr(self, "screen") else None
        avail = screen.availableGeometry() if screen else None
        return avail.height() if avail else 900

    def _chrome_height(self) -> int:
        """스크롤영역 밖(안내문·ALL·구분선·설명바·버튼·여백)이 쓰는 세로.

        레이아웃 hint 로 재는 이유: 실제 height() 는 창이 아직 안 그려졌을 때 0 이거나
        직전 크기라 첫 렌더에서 틀린 값이 나온다.

        ⚠ 숨김 판정은 `isHidden()` 이어야 한다 — `not isVisible()` 은 **창을 show() 하기
        전에 모든 자식이 True** 라, 첫 렌더(=행 높이를 정하는 순간)에 안내문·ALL·버튼이
        전부 없는 것으로 계산돼 chrome 이 54 로 나온다(실제 174). 그만큼 예산이 부풀어
        행이 안 조여지고 1080p 에서 스크롤이 남았다.
        """
        root = self.layout()
        if root is None:
            return 200
        total = 0
        count = 0
        for i in range(root.count()):
            item = root.itemAt(i)
            w = item.widget()
            if w is self._scroll:
                continue
            if w is not None and w.isHidden():
                continue          # 숨긴 _loading 은 자리를 차지하지 않는다
            total += item.sizeHint().height()
            count += 1
        m = root.contentsMargins()
        return total + root.spacing() * count + m.top() + m.bottom()

    def _build_rows(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._rows = {}
        groups = list(self._catalog.get("groups") or [])
        for r, group in enumerate(groups):
            gid = str(group.get("id") or "")
            fixed = bool(group.get("gauge_fixed"))

            name = QLabel(self._signature_label(group))
            name.setFixedWidth(self.NAME_WIDTH)
            name.setStyleSheet("font-weight:700; font-size:13px;")
            name.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            name.setToolTip(str(group.get("label_ko") or gid))
            self._grid.addWidget(name, r, 0)

            gauge = GaugeSlider()
            gauge.setGaugeEnabled(not fixed)
            gauge.changed.connect(lambda lv, g=gid: self._on_group(g, lv))
            gauge.setToolTip("게이지 고정 — 값 직접 입력만 가능합니다." if fixed
                             else "1 rough(덜 발화) ← 3 기본 → 5 tight(더 발화)")
            self._grid.addWidget(gauge, r, 1,
                                 Qt.AlignmentFlag.AlignTop)

            # threshold 는 **세로로** 쌓는다 — 키 이름이 길어(subpop_density_gap_strong 등)
            # 가로로 늘어놓으면 창 밖으로 밀리고, 키가 5개인 그룹(TAIL·BIMODALITY)은 줄이 접힌다.
            values = QVBoxLayout()
            values.setSpacing(self._KEY_GAP)
            values.setContentsMargins(0, 0, 0, 4)
            inputs = {}
            for entry in group.get("keys") or []:
                key = str(entry.get("key") or "")
                help_text = self._help_text(key)
                one = QHBoxLayout()
                one.setSpacing(6)
                label = QLabel(key)
                label.setFixedWidth(self.KEY_WIDTH)
                label.setStyleSheet("font-family:Consolas,monospace; font-size:12px;"
                                    "color:#445;")
                label.setToolTip(help_text)
                edit = QLineEdit()
                edit.setFixedWidth(90)
                edit.setFixedHeight(self._KEY_LINE_H - 4)
                edit.setStyleSheet("font-size:13px;")
                edit.setAlignment(Qt.AlignmentFlag.AlignRight)
                edit.setToolTip(help_text)
                edit.editingFinished.connect(
                    lambda k=key, g=gid: self._on_value_edited(g, k))
                # 클릭(포커스)·마우스오버 시 하단 설명 바에 그 기준의 뜻을 띄운다.
                edit.installEventFilter(self)
                edit.setProperty("threshold_key", key)
                label.installEventFilter(self)
                label.setProperty("threshold_key", key)
                inputs[key] = edit
                one.addWidget(label)
                one.addWidget(edit)
                default = entry.get("default")
                hint = QLabel(f"기본 {default}" if default is not None else "")
                hint.setStyleSheet("color:#8a95a3; font-size:12px;")
                one.addWidget(hint)
                one.addStretch(1)
                values.addLayout(one)
            vholder = QWidget()
            vholder.setLayout(values)
            self._grid.addWidget(vholder, r, 2)

            self._rows[gid] = {"gauge": gauge, "inputs": inputs, "fixed": fixed}
        # 키가 적은 그룹은 행이 게이지 높이(36)만 해서 너무 촘촘했다 — 남는 세로를 나눠
        # 벌리되 화면 예산 안에서만(스크롤 금지). 행을 다 만든 뒤라야 sizeHint 를 잰다.
        self._fit_rows_to_screen(len(groups))
        self._fit_width()
        self._fit_height()

    def _fit_width(self):
        """내용이 잘리지 않는 **최소 폭**을 창에 건다.

        가로 스크롤바를 꺼 둔 창이라(값 입력란이 화면 밖으로 나가면 안 되므로) 폭이
        모자라면 스크롤이 아니라 **그냥 잘린다**. 사용자가 창을 좁히면 값 입력란과
        '기본 N' 힌트가 소리 없이 사라지므로, 좁힐 수 없게 하한을 둔다.
        """
        inner = self._scroll.widget()
        if inner is None:
            return
        inner.adjustSize()
        # 내용 + 세로 스크롤바(작은 화면에선 뜬다) + 창 좌우 여백
        bar = self._scroll.verticalScrollBar().sizeHint().width()
        m = self.layout().contentsMargins()
        need = inner.sizeHint().width() + bar + m.left() + m.right() + 4
        self.setMinimumWidth(need)
        if self.width() < need:
            self.resize(need, self.height())

    def _fit_height(self):
        """내용 높이에 창을 맞춘다 — 화면보다 크면 화면에 맞추고 스크롤한다.

        고정 높이로 두면 그룹·키 개수가 바뀔 때(카탈로그가 정한다) 아래가 텅 비거나
        잘린다. 실제로 첫 렌더에서 아래 1/3 이 빈 공간이었다.

        cap 은 **화면 작업영역 전체**다(사용자 지정 — 모든 Signature 가 스크롤 없이
        보여야 한다). 여백을 남기면 그룹 8개가 잘려 다시 스크롤이 생긴다.
        `_row_min_height()` 가 같은 예산으로 행을 줄여 두므로 정상적으로는 여기서
        cap 에 걸리지 않는다(걸린다면 그 화면이 8행을 담기엔 너무 낮다는 뜻).
        """
        inner = self._scroll.widget()
        if inner is None:
            return
        inner.adjustSize()
        need = inner.sizeHint().height() + 8
        screen = self.screen() if hasattr(self, "screen") else None
        avail = screen.availableGeometry() if screen else None
        cap = int(avail.height() * 0.97) if avail else 900
        # chrome 은 **레이아웃 hint 로** 잰다 — `height() - scroll.height()` 는 창이 아직
        # 안 그려진 첫 렌더에서 직전 크기를 반영해 과소평가되고, 그만큼 창이 짧게 잡혀
        # 세로 스크롤이 남는다(2026-09-01 1080p 회귀의 직접 원인).
        chrome = self._chrome_height()
        self.resize(self.width(), min(cap, need + max(chrome, 0)))
        # 세로로 커진 창이 화면 아래로 삐져나가면 그만큼 다시 안 보인다 — 작업영역
        # 안으로 끌어올린다(리사이즈만으로는 위치가 그대로다).
        if avail is not None:
            g = self.frameGeometry()
            g.moveCenter(avail.center())
            self.move(max(avail.left(), g.left()), max(avail.top(), g.top()))
        self._refresh()

    def _help_text(self, key) -> str:
        """threshold 한 줄 설명 — 문구 정본은 서버 threshold_help.yaml (클라 하드코딩 없음)."""
        info = ((self._catalog or {}).get("help") or {}).get(key) or {}
        what = str(info.get("what") or "").splitlines()
        effect = str(info.get("effect") or "").splitlines()
        parts = [p[0].strip() for p in (what, effect) if p and p[0].strip()]
        return " / ".join(parts) or key

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.Enter):
            key = obj.property("threshold_key")
            if key:
                text = self._help_text(key)
                self._help.setText(text)
                # 설명바는 1줄 고정(높이가 출렁이면 아래 그룹이 밀린다) — 길어서 잘린
                # 문구는 툴팁으로 전문을 준다.
                self._help.setToolTip(text)
        return super().eventFilter(obj, event)

    # ── 상호작용 ────────────────────────────────────────────────────────────
    def _on_all(self, level):
        """전체 게이지 — 전 그룹을 같은 단계로. 직접 입력은 해제한다.

        해제하지 않으면 "전체 3단계" 로 되돌려도 손으로 넣은 값이 남아, 화면이 말하는
        단계와 실제 적용값이 어긋난다.
        """
        self._settings["global"] = level
        self._settings["groups"] = {gid: level for gid in self._rows}
        self._settings["manual"] = {}
        self._refresh()

    def _on_group(self, group_id, level):
        self._settings.setdefault("groups", {})[group_id] = level
        # 그 그룹의 직접 입력은 게이지 선택으로 대체된다(둘이 공존하면 뭐가 적용됐는지 모른다).
        row = self._rows.get(group_id) or {}
        for key in (row.get("inputs") or {}):
            self._settings.get("manual", {}).pop(key, None)
        self._sync_global()
        self._refresh()

    def _on_value_edited(self, group_id, key):
        row = self._rows.get(group_id) or {}
        edit = (row.get("inputs") or {}).get(key)
        if edit is None:
            return
        text = edit.text().strip()
        gauge_val = self._gauge_value(group_id, key)
        if not text:
            self._settings.get("manual", {}).pop(key, None)
            self._refresh()
            return
        try:
            value = float(text)
        except ValueError:
            QMessageBox.warning(self, "값 오류", f"{key} 는 숫자여야 합니다.")
            self._refresh()
            return
        # 서버가 받아주지 않을 값이면 **여기서** 알린다(2026-09-02 사용자 요청) — 그냥
        # 두면 나중에 업로드가 400 으로 거절돼 세션 자체가 안 만들어진다. 판정 재료는
        # 서버 카탈로그가 준다(클라에 규칙 사본 없음 — eval_sensitivity.check_value).
        problem = eval_sensitivity.check_value(
            self._catalog, key, value, self._effective_values())
        if problem:
            QMessageBox.warning(
                self, "적용할 수 없는 값",
                f"{problem}\n\n이대로 두면 이 설정으로는 Web Report 세션을 만들 수 없습니다.\n"
                "값을 고치거나, 비워 두면 게이지 값으로 되돌아갑니다.")
            self._refresh()      # 입력란을 직전 유효값으로 되돌린다
            return
        # 게이지 값과 같아지면 직접 입력을 해제한다 — 같은 값을 두 방식으로 들고 있을
        # 이유가 없고, 남기면 저장 payload 만 커진다.
        if gauge_val is not None and value == float(gauge_val):
            self._settings.get("manual", {}).pop(key, None)
        else:
            self._settings.setdefault("manual", {})[key] = value
        self._sync_global()
        self._refresh()

    def _effective_values(self) -> dict:
        """지금 화면이 적용 중인 값 전체 {key: value} — 관계식 검증의 상대편 재료.

        직접 입력(manual)이 있으면 그 값, 없으면 그 그룹 게이지 단계의 값이다. 서버
        `_check_values` 가 기본값에 overrides 를 덮어 보는 것과 같은 그림이라, 여기서
        통과한 값은 서버에서도 통과한다(기본값은 게이지 3단계와 같다).
        """
        manual = self._settings.get("manual") or {}
        out = {}
        for gid, row in self._rows.items():
            for key in row.get("inputs") or {}:
                value = manual.get(key, self._gauge_value(gid, key))
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    out[key] = value
        return out

    def _sync_global(self):
        """그룹 단계가 제각각이거나 직접 입력이 있으면 전체는 '사용자설정'(0)."""
        levels = {self._settings["groups"].get(gid, eval_sensitivity.DEFAULT_LEVEL)
                  for gid in self._rows}
        self._settings["global"] = (levels.pop() if len(levels) == 1 else 0)
        if self._settings.get("manual"):
            self._settings["global"] = 0

    def _group_of(self, group_id):
        for group in (self._catalog or {}).get("groups") or []:
            if str(group.get("id")) == group_id:
                return group
        return None

    def _gauge_value(self, group_id, key):
        group = self._group_of(group_id)
        if not group:
            return None
        level = self._settings["groups"].get(group_id, eval_sensitivity.DEFAULT_LEVEL)
        return eval_sensitivity.gauge_value(group, key, level)

    def _refresh(self):
        """설정 → 화면. 게이지 이동이 값 입력란에 **실시간**으로 반영되는 지점이다."""
        manual = self._settings.get("manual") or {}
        self._all_gauge.setValue(
            self._settings.get("global", eval_sensitivity.DEFAULT_LEVEL))
        for gid, row in self._rows.items():
            level = self._settings["groups"].get(gid, eval_sensitivity.DEFAULT_LEVEL)
            has_manual = any(k in manual for k in row["inputs"])
            # 직접 입력이 하나라도 있으면 그 행은 마지막 칸(사용자설정)에 선다.
            row["gauge"].setValue(GaugeSlider.CUSTOM if has_manual else level)
            for key, edit in row["inputs"].items():
                value = manual.get(key, self._gauge_value(gid, key))
                edit.blockSignals(True)
                edit.setText("" if value is None else str(value))
                edit.blockSignals(False)
                # font-size 를 여기에도 넣는다 — setStyleSheet 는 덮어쓰기라
                # 생성 시점 크기가 강조/해제 때마다 날아간다.
                edit.setStyleSheet("font-size:13px;" + (
                    "font-weight:600; color:#2f6fd0;" if key in manual else ""))

    def _on_ok(self):
        # 저장 직전 **전체를 한 번 더** 본다. 값을 하나씩 고칠 때는 각각 성립했어도,
        # 나중에 고친 값이 앞선 값과의 관계식(cpk_bad <= cpk_warn 등)을 깨뜨릴 수 있다.
        effective = self._effective_values()
        manual = self._settings.get("manual") or {}
        for key in sorted(manual):
            problem = eval_sensitivity.check_value(
                self._catalog, key, manual[key], effective)
            if problem:
                QMessageBox.warning(
                    self, "적용할 수 없는 값",
                    f"{problem}\n\n이대로 저장하면 이 설정으로는 Web Report 세션을 만들 수 "
                    "없습니다.\n값을 고친 뒤 다시 확인해 주세요.")
                return                       # 창을 닫지 않는다 — 고칠 자리를 남긴다
        eval_sensitivity.save_settings(self._settings)
        # "제안 제외" 는 민감도 spec 이 아니라 별도 설정 키다(업로드 옵션에서도 따로 실린다).
        app_settings.set_setting("ai_no_suggest", bool(self.chk_no_suggest.isChecked()))
        self.accept()


class OptionsDialog(QDialog):
    """Honey 통합 옵션 — 기본 Product Type + Distribution 차트 색 + AI Comment 민감도.

    색 편집·민감도는 별도 다이얼로그를 버튼으로 연다(항목이 많아 한 창에 못 담는다).
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

        # (3) AI Comment 민감도 — 그룹 8개를 한 화면에 펼쳐야 해서 별도 큰 창으로 연다.
        btn_sens = QPushButton("AI Comment 민감도 설정...")
        btn_sens.clicked.connect(lambda: EvalSensitivityDialog(self).exec())
        root.addWidget(btn_sens)

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

    def __init__(self, parent, group, source_count, product_type=None):
        # source_count = honey_parse 가 돌려준 df(=source) 개수. 입력 파일 개수가 아니다
        # (여러 파일이 하나로 병합되거나 한 파일이 여러 source 로 나뉠 수 있다).
        super().__init__(parent)
        uic.loadUi(str(SETTINGS_UI_PATH), self)
        self.group = group
        self.source_count = source_count
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
        one_source = self.source_count == 1
        self.cb_mode_dut.setEnabled(one_source and not raw_on)
        if not self.cb_mode_dut.isEnabled():
            self.cb_mode_dut.setChecked(False)

    def _update_compare_mode_availability(self):
        """source(honey_parse 반환 df)가 정확히 2개일 때만 Compare Mode 활성화."""
        ok = self.source_count == 2
        if not ok:
            self.cb_mode_compare.setChecked(False)
        self.cb_mode_compare.setEnabled(ok)

    def mode_compare(self):
        return self.cb_mode_compare.isChecked()

    def _current_filenames(self):
        names = []
        for i in range(self.source_count):
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
            "source 별 Legend 이름을 쉼표(,)로 구분해 입력하세요.\n"
            "빈칸은 기존 이름을 유지합니다.",
            text=", ".join(current),
        )
        if not ok:
            return
        parts = [p.strip() for p in text.split(",")]
        while len(parts) < self.source_count:
            parts.append("")
        overrides = []
        seen = {}
        for i, part in enumerate(parts[:self.source_count]):
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

"""CompareArrangeDialog — Compare 모드 Before / After 배치 다이얼로그.

Compare 모드는 종전에 source 가 정확히 2개일 때만 쓸 수 있었고, 어느 쪽이 Before/After
인지는 **업로드 순서로 암묵 결정**됐다. 이제 source 가 몇 개든 두 그룹에 직접 나눠 담고
그룹 안 순서까지 정한다.

    Before                     After
    ┌──────────┐   >>  >       ┌──────────┐   ↑
    │ ■ WF1    │   <   <<      │ ■ WF3    │   ↓
    │ ■ WF2    │               │ ■ WF4    │
    └──────────┘               └──────────┘
      항목 더블클릭 = Legend 이름 변경           [Confirm] [취소]

**순서가 의미를 갖는다** — After 최상단 source 가 웹 리포트 전체의 limit(HiLIM/LoLIM)
기준이고, Log 비교(goodlog)의 after/before 대표이기도 하다. 그래서 좌/우 리스트 조작은
RawdataHubDialog 의 Item Select(``_ItemListWidget``)와 같은 규칙을 쓰되 **이동 후 원본
순서로 되돌리는 재정렬은 하지 않는다**.

항목 앞 사각형이 그 source 의 리포트 색이다. 색은 이름이 아니라 **업로드 순서
(After → Before) 위치 i** 에 붙으므로(서버 ``dist_colors[i]``), 항목을 옮기면 색이 그
자리에 남는다 — ``SourceNameDialog`` 의 색 열과 같은 규칙이라 두 창의 의미가 어긋나지
않는다. 그래서 이동·정렬 뒤에는 반드시 ``_refresh_colors()`` 를 돌린다.

Compare 모드에서는 이 창이 공통 ``SourceNameDialog``(표 방식)를 대신한다 — 이름 변경은
항목 더블클릭, 색 변경은 [색 변경…] 버튼으로 한다(더블클릭은 이름이 먼저 쓴다).

상단 라디오로 **Normal Compare**(위 설명 그대로)와 **Para Conversion**(2026-08-27)을
고른다. Para 는 Single Mass Data 1개 vs Para Mass Data 1개 고정이고, Confirm 후
호출부(``honey_main._prepare_para_conversion``)가 Para 파일을 DUT 별로 펼쳐
``Single`` + ``DUT<라벨>`` N개 source 로 업로드한다. 그래서 Para 에서는 이 창의
이름·색이 최종 source 와 1:1 이 아니라 이름 변경·색 지정을 쓰지 않는다(DUT 모드와 같은 이유).
"""
from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QBrush, QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from honey_ui.source_name_dialog import load_palette

_WIDTH = 1820                 # 기본 가로 폭 (파일명이 잘린다는 요청으로 1400→1820)
_MOVE_BTN_W = 56               # ">>" / "<<" 가 36px 에서 잘려 보였다(2026-08-25 요청)
_SWATCH = 14                  # 항목 앞 색 사각형 한 변(px)
_HEAD_BG = "#DCFCE7"          # After 최상단(= limit 기준) 강조 배경

# Para Conversion 모드의 그룹 라벨 — Before/After 자리를 그대로 쓰되 표기만 바꾼다.
_PARA_LABELS = ("Single Mass Data", "Para Mass Data")
_NORMAL_LABELS = ("Before", "After")


def dedupe_names(names) -> list:
    """중복 이름에 _2, _3 … 접미사를 붙여 유일하게 만든다.

    temperature_pairing.dedupe_names 와 **같은 규칙** — 첫 번째는 원래 이름 그대로 두고
    두 번째부터 접미사가 붙는다.
    """
    out, seen = [], {}
    for name in names:
        base = str(name)
        if base in seen:
            seen[base] += 1
            base = f"{base}_{seen[base]}"
        else:
            seen[base] = 1
        out.append(base)
    return out


class CompareArrangeDialog(QDialog):
    """exec() 가 참을 돌려주면 result() 로 배치 결과를 읽는다."""

    def __init__(self, parent, names, colors=None):
        super().__init__(parent)
        self.setWindowTitle("Compare — Before / After 배치 / 색")
        # source(파일)명이 길면 QListWidget 기본 ElideRight 로 잘려 어느 파일인지 구분이
        # 안 된다(2026-08-20 요청 → 2026-08-25 재확대: 1020 → 1400 → 2026-08-27: 1820).
        # 세로는 그대로. 넓힌 만큼 작은 화면을 넘을 수 있어 가용 폭으로 가둔다.
        self.resize(min(_WIDTH, self._avail_width()), 460)
        self._original = [str(n) for n in names]
        self._colors = list(colors) if colors else load_palette()
        self._colors_changed = False

        self.list_before = QListWidget()
        self.list_after = QListWidget()
        for lw in (self.list_before, self.list_after):
            lw.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            lw.setUniformItemSizes(True)
        self.list_before.itemDoubleClicked.connect(self._rename)
        self.list_after.itemDoubleClicked.connect(self._rename)

        # 초기 배치: 2개면 [0]=Before / [1]=After (가장 흔한 before→after 시간순이라 바로
        # Confirm 할 수 있다). 3개 이상은 판단 근거가 없어 전부 Before 에 두고 사용자가 옮긴다.
        for idx, name in enumerate(self._original):
            target = self.list_after if (len(self._original) == 2 and idx == 1) else self.list_before
            it = QListWidgetItem(name)
            # 원본 source 순번 — rename_sources 가 **원본 순서** 리스트를 받으므로 필요하다.
            it.setData(Qt.ItemDataRole.UserRole, idx)
            target.addItem(it)

        btn_all_right = QPushButton(">>")
        btn_sel_right = QPushButton(">")
        btn_sel_left = QPushButton("<")
        btn_all_left = QPushButton("<<")
        btn_all_right.clicked.connect(lambda: self._move_all(self.list_before, self.list_after))
        btn_all_left.clicked.connect(lambda: self._move_all(self.list_after, self.list_before))
        btn_sel_right.clicked.connect(
            lambda: self._move(self.list_before, self.list_after, self.list_before.selectedItems()))
        btn_sel_left.clicked.connect(
            lambda: self._move(self.list_after, self.list_before, self.list_after.selectedItems()))
        self._refresh_colors()

        mid = QVBoxLayout()
        mid.addStretch(1)
        for b in (btn_all_right, btn_sel_right, btn_sel_left, btn_all_left):
            # 고정폭이 글자보다 좁으면 Qt 가 텍스트를 잘라낸다(고DPI·큰 글꼴 PC).
            # sizeHint 이상을 보장해 어떤 환경에서도 ">>"/"<<" 가 온전히 보이게 한다.
            b.setFixedWidth(max(_MOVE_BTN_W, b.sizeHint().width()))
            mid.addWidget(b)
        mid.addStretch(1)

        # 그룹 안 순서 — 최상단이 그룹 대표라 반드시 조정할 수 있어야 한다.
        btn_up = QPushButton("↑")
        btn_down = QPushButton("↓")
        btn_up.setToolTip("선택 항목을 위로 (최상단 = 그룹 대표)")
        btn_down.setToolTip("선택 항목을 아래로")
        btn_up.clicked.connect(lambda: self._shift(-1))
        btn_down.clicked.connect(lambda: self._shift(1))
        right = QVBoxLayout()
        right.addStretch(1)
        for b in (btn_up, btn_down):
            # 고정폭이 글자보다 좁으면 Qt 가 텍스트를 잘라낸다(고DPI·큰 글꼴 PC).
            # sizeHint 이상을 보장해 어떤 환경에서도 ">>"/"<<" 가 온전히 보이게 한다.
            b.setFixedWidth(max(_MOVE_BTN_W, b.sizeHint().width()))
            right.addWidget(b)
        right.addStretch(1)

        # 모드 선택 — Normal 은 종전과 완전히 같고, Para Conversion 은 Para 쪽 파일을
        # 업로드 직전에 DUT 별로 펼친다(honey_main._prepare_para_conversion).
        self.rb_normal = QRadioButton("Normal Compare")
        self.rb_para = QRadioButton("Para Conversion")
        self.rb_normal.setChecked(True)
        self.rb_normal.setToolTip("기존 Compare — 배치한 source 를 그대로 비교합니다.")
        self.rb_para.setToolTip(
            "Single Mass Data 1개 vs Para Mass Data 1개.\n"
            "Para 파일은 DUT 별로 펼쳐 DUT1~N source 가 됩니다.")
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.rb_normal)
        self._mode_group.addButton(self.rb_para)
        self.rb_normal.toggled.connect(self._sync_mode)
        mode_bar = QHBoxLayout()
        mode_bar.addWidget(self.rb_normal)
        mode_bar.addWidget(self.rb_para)
        mode_bar.addStretch(1)

        self.lbl_before = QLabel(_NORMAL_LABELS[0])
        self.lbl_after = QLabel(_NORMAL_LABELS[1])

        grid = QGridLayout()
        grid.addWidget(self.lbl_before, 0, 0)
        grid.addWidget(self.lbl_after, 0, 2)
        grid.addWidget(self.list_before, 1, 0)
        grid.addLayout(mid, 1, 1)
        grid.addWidget(self.list_after, 1, 2)
        grid.addLayout(right, 1, 3)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)

        self.btn_color = QPushButton("색 변경…")
        self.btn_color.setToolTip("선택한 항목의 리포트 색을 바꿉니다 (옵션(F10) 팔레트보다 우선).")
        self.btn_color.clicked.connect(self._pick_color)
        self.btn_palette = QPushButton("전체 팔레트 편집…")
        self.btn_palette.setToolTip("48색 팔레트를 편집해 옵션(F10)에 저장합니다.")
        self.btn_palette.clicked.connect(self._edit_palette)
        tools = QHBoxLayout()
        tools.addWidget(self.btn_color)
        tools.addStretch(1)
        tools.addWidget(self.btn_palette)

        self.hint = QLabel()
        self.hint.setStyleSheet("color:#64748b;")
        self._sync_mode()

        buttons = QDialogButtonBox()
        buttons.addButton("Confirm", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("취소", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(mode_bar)
        root.addLayout(grid)
        root.addLayout(tools)
        root.addWidget(self.hint)
        root.addWidget(buttons)

    # ── 모드 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _avail_width() -> int:
        """이 창을 띄울 화면의 가용 폭 (없으면 기본값 — 클램프 생략)."""
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        return screen.availableGeometry().width() if screen else _WIDTH

    def is_para(self) -> bool:
        return self.rb_para.isChecked()

    def _sync_mode(self, *_a):
        """모드에 따라 그룹 라벨·안내문·색 버튼 상태를 맞춘다."""
        para = self.is_para()
        before, after = _PARA_LABELS if para else _NORMAL_LABELS
        self.lbl_before.setText(before)
        self.lbl_after.setText(after)
        # Para 는 업로드 source 가 DUT 분할 결과라 이 창의 순번과 개수가 달라진다 —
        # 여기서 고른 색이 엉뚱한 source 에 붙으므로 색 지정을 막는다(DUT 모드와 같은 이유).
        for b in (self.btn_color, self.btn_palette):
            b.setEnabled(not para)
        if para:
            self.hint.setText(
                "· Single Mass Data 와 Para Mass Data 에 각각 파일 1개씩만 배치하세요.\n"
                "· Confirm 하면 Para 파일이 DUT 별로 나뉘어 DUT1~N source 가 됩니다"
                " (Single 쪽은 'Single').\n"
                "· 이름·색은 자동 부여됩니다 — 이 창에서 지정하지 않습니다.\n"
                "· Log 비교의 Value 는 각 DUT 의 첫 데이터 값으로 채워집니다.")
        else:
            self.hint.setText(
                "· 항목 더블클릭 = Legend 이름 변경 / 항목 선택 후 [색 변경…] = 색 지정\n"
                "· After 최상단 source 가 limit(HiLIM/LoLIM) 기준이고 Log 비교의 대표입니다 (초록 바탕).\n"
                "· 업로드 순서는 After → Before 순이 되며 웹 리포트의 컬럼·범례 순서와 같습니다.\n"
                "· 색은 그 업로드 순서(1,2,3…)에 붙습니다 — 항목을 옮기면 색도 그 자리에 남습니다.")

    # ── 조작 ────────────────────────────────────────────────────────────────
    def _move(self, src, dst, items):
        for it in list(items):
            row = src.row(it)
            if row >= 0:
                dst.addItem(src.takeItem(row))   # 재정렬 없음 — 순서가 곧 의미
        self._refresh_colors()

    def _move_all(self, src, dst):
        self._move(src, dst, [src.item(i) for i in range(src.count())])

    def _shift(self, delta):
        """선택 항목을 그 리스트 안에서 한 칸 이동. 선택은 유지한다."""
        for lw in (self.list_before, self.list_after):
            rows = sorted((lw.row(it) for it in lw.selectedItems()),
                          reverse=(delta > 0))
            for row in rows:
                new = row + delta
                if new < 0 or new >= lw.count():
                    continue
                it = lw.takeItem(row)
                lw.insertItem(new, it)
                it.setSelected(True)
        self._refresh_colors()

    # ── 색 ──────────────────────────────────────────────────────────────────
    def _ordered_items(self):
        """업로드 순서(After → Before)의 항목 목록 — 색 번호가 붙는 순서다."""
        return [lw.item(i) for lw in (self.list_after, self.list_before)
                for i in range(lw.count())]

    def _color_at(self, i):
        return self._colors[i] if i < len(self._colors) else "#888888"

    def _refresh_colors(self):
        """항목 앞 색 사각형을 업로드 순서 기준으로 다시 칠한다."""
        for i, it in enumerate(self._ordered_items()):
            color = self._color_at(i)
            pix = QPixmap(_SWATCH, _SWATCH)
            pix.fill(QColor(color))
            it.setIcon(QIcon(pix))
            # 이름을 첫 줄에 둔다 — 폭보다 긴 이름은 ElideRight 로 잘리므로
            # 툴팁이 전체 이름을 볼 수 있는 유일한 경로다.
            it.setToolTip(f"{it.text()}\n{i + 1}번 (업로드 순서) — 색 {color}")
        for lw in (self.list_before, self.list_after):
            lw.setIconSize(QSize(_SWATCH, _SWATCH))
        self._refresh_after_head()

    def _refresh_after_head(self):
        """After 최상단 항목만 초록 배경 — 그 source 가 limit 기준이자 Log 비교 대표다.

        의미가 이름이 아니라 **위치**에 있어서 이동·정렬 뒤 매번 다시 칠해야 한다.
        """
        for lw in (self.list_before, self.list_after):
            for i in range(lw.count()):
                head = (lw is self.list_after and i == 0)
                lw.item(i).setBackground(QBrush(QColor(_HEAD_BG)) if head else QBrush())

    def _pick_color(self):
        selected = [it for lw in (self.list_before, self.list_after)
                    for it in lw.selectedItems()]
        if len(selected) != 1:
            QMessageBox.information(self, "색 변경",
                                    "색을 바꿀 항목 하나를 선택해 주세요.")
            return
        ordered = self._ordered_items()
        i = ordered.index(selected[0])
        chosen = QColorDialog.getColor(QColor(self._color_at(i)), self,
                                       f"{selected[0].text()} 색상 선택")
        if not chosen.isValid():
            return
        while len(self._colors) <= i:
            self._colors.append("#888888")
        self._colors[i] = chosen.name().upper()
        self._colors_changed = True
        self._refresh_colors()

    def _edit_palette(self):
        """48색 팔레트 편집(옵션 F10 과 같은 창) — 저장되면 색을 다시 읽는다."""
        from honey_ui.dialogs import ColorEditorDialog
        if ColorEditorDialog(self).exec():
            self._colors = load_palette()
            self._colors_changed = True
            self._refresh_colors()

    def _rename(self, item):
        text, ok = QInputDialog.getText(self, "SourceName 변경",
                                        "Legend 이름:", text=item.text())
        text = (text or "").strip()
        if ok and text:
            item.setText(text)

    # ── 결과 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _entries(lw):
        """[(원본 index, 현재 이름)] — 리스트에 보이는 순서 그대로."""
        return [(lw.item(i).data(Qt.ItemDataRole.UserRole), lw.item(i).text())
                for i in range(lw.count())]

    def _accept(self):
        if self.is_para():
            # Para 는 "Single 1개 vs Para 1개" 고정이다 — Para 쪽을 DUT 로 펼치는 것이
            # 비교 축이라 파일이 더 있으면 DUT 이름이 겹치고 축이 성립하지 않는다.
            if self.list_before.count() != 1 or self.list_after.count() != 1:
                QMessageBox.warning(
                    self, "Para Conversion",
                    f"{_PARA_LABELS[0]} 와 {_PARA_LABELS[1]} 에 각각 파일 1개씩만 "
                    "배치해 주세요.")
                return
        elif not self.list_before.count() or not self.list_after.count():
            QMessageBox.warning(self, "Compare 배치",
                                "Before 와 After 에 각각 1개 이상 배치해 주세요.")
            return
        self.accept()

    def result_groups(self) -> dict:
        """Confirm 결과.

        - ``names``  : **원본 source 순서**의 새 이름 목록 —
          ``df_honey_group.rename_sources`` 가 원본 순서 기준이라 그대로 넘긴다.
        - ``order``  : 업로드 순서 (After 먼저 → Before) 의 새 이름 목록.
        - ``before`` / ``after`` : 그룹별 새 이름 목록 (그룹 안 순서 유지).
        - ``colors`` : 창에서 바꿨을 때만 48색 목록, 아니면 None (옵션 팔레트 유지).
        - ``para``   : Para Conversion 모드 여부. 참이면 호출부가 Para(after) 쪽 파일을
          DUT 별로 펼쳐 업로드한다 — 이 창의 이름·순서는 그 분할 전 값이다.

        이름은 source 키라 전체에서 유일해야 한다. dedupe 는 **원본 순서 기준**으로 한 번만
        수행하고(rename_sources 와 같은 규칙), 그 결과를 그룹 목록에도 그대로 반영한다.
        """
        after_entries = self._entries(self.list_after)
        before_entries = self._entries(self.list_before)
        raw_by_idx = {i: n for i, n in after_entries + before_entries}
        deduped = dedupe_names([raw_by_idx[i] for i in range(len(self._original))])
        name_by_idx = {i: deduped[i] for i in range(len(self._original))}

        after = [name_by_idx[i] for i, _ in after_entries]
        before = [name_by_idx[i] for i, _ in before_entries]
        return {"names": deduped, "order": after + before,
                "after": after, "before": before,
                "para": self.is_para(),
                "colors": list(self._colors) if self._colors_changed else None}

"""CompareArrangeDialog — Compare 모드 Before / After 배치 다이얼로그.

Compare 모드는 종전에 source 가 정확히 2개일 때만 쓸 수 있었고, 어느 쪽이 Before/After
인지는 **업로드 순서로 암묵 결정**됐다. 이제 source 가 몇 개든 두 그룹에 직접 나눠 담고
그룹 안 순서까지 정한다.

    Before                     After
    ┌──────────┐   >>  >       ┌──────────┐   ↑
    │ WF1      │   <   <<      │ WF3      │   ↓
    │ WF2      │               │ WF4      │
    └──────────┘               └──────────┘
      항목 더블클릭 = Legend 이름 변경           [Confirm] [취소]

**순서가 의미를 갖는다** — After 최상단 source 가 웹 리포트 전체의 limit(HiLIM/LoLIM)
기준이고, Log 비교(goodlog)의 after/before 대표이기도 하다. 그래서 좌/우 리스트 조작은
RawdataHubDialog 의 Item Select(``_ItemListWidget``)와 같은 규칙을 쓰되 **이동 후 원본
순서로 되돌리는 재정렬은 하지 않는다**.

Compare 모드에서는 이 창이 공통 ``SourceNameDialog``(표 방식)를 대신한다 — 이름 변경은
항목 더블클릭으로 한다.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

_MOVE_BTN_W = 36


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

    def __init__(self, parent, names):
        super().__init__(parent)
        self.setWindowTitle("Compare — Before / After 배치")
        self.resize(680, 460)
        self._original = [str(n) for n in names]

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

        mid = QVBoxLayout()
        mid.addStretch(1)
        for b in (btn_all_right, btn_sel_right, btn_sel_left, btn_all_left):
            b.setFixedWidth(_MOVE_BTN_W)
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
            b.setFixedWidth(_MOVE_BTN_W)
            right.addWidget(b)
        right.addStretch(1)

        grid = QGridLayout()
        grid.addWidget(QLabel("Before"), 0, 0)
        grid.addWidget(QLabel("After"), 0, 2)
        grid.addWidget(self.list_before, 1, 0)
        grid.addLayout(mid, 1, 1)
        grid.addWidget(self.list_after, 1, 2)
        grid.addLayout(right, 1, 3)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)

        hint = QLabel("· 항목 더블클릭 = Legend 이름 변경\n"
                      "· After 최상단 source 가 limit(HiLIM/LoLIM) 기준이고 Log 비교의 대표입니다.\n"
                      "· 업로드 순서는 After → Before 순이 되며 웹 리포트의 컬럼·범례 순서와 같습니다.")
        hint.setStyleSheet("color:#64748b;")

        buttons = QDialogButtonBox()
        buttons.addButton("Confirm", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("취소", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(grid)
        root.addWidget(hint)
        root.addWidget(buttons)

    # ── 조작 ────────────────────────────────────────────────────────────────
    def _move(self, src, dst, items):
        for it in list(items):
            row = src.row(it)
            if row >= 0:
                dst.addItem(src.takeItem(row))   # 재정렬 없음 — 순서가 곧 의미

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
        if not self.list_before.count() or not self.list_after.count():
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
                "after": after, "before": before}

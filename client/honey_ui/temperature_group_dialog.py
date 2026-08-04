"""TemperatureGroupDialog — Temperature 모드 RT/CT/HT 그룹 배치 다이얼로그 (PMIC 전용).

PMIC 는 같은 웨이퍼를 RT(상온)/CT(저온)/HT(고온) 로 나눠 측정한다. 파일이 21개면
RT/CT/HT 쌍이 7그룹이 될 수도 있고, 그룹마다 **자기 RT** 가 Limit 판정 기준이 된다.
그래서 이 창은 source 를 그룹 × 역할(RT/CT/HT) 자리에 **끌어다 놓아** 배치한다.

    미배정 source          ┌ Group 1 ─────────────────────────────┐
    ┌──────────────┐       │  RT(기준)      CT          HT        │
    │ WF1_RT       │  ──▶  │ ┌─────────┐ ┌────────┐ ┌────────┐   │
    │ WF1_CT       │       │ │ WF1_RT  │ │ WF1_CT │ │ WF1_HT │   │
    └──────────────┘       │ └─────────┘ └────────┘ └────────┘   │
                           └──────────────────────────────────────┘
      .lt / .pds  (버튼 또는 드래그앤드랍)        [Start] [취소]

RT 자리는 음영으로 강조한다 — 그 source 의 HILIM/LOLIM 이 같은 그룹 CT/HT 의 Pass/Fail
재판정 기준이기 때문이다. CT/HT 는 없어도 된다(RT 단독 그룹 허용).

Start 를 누르면 honey_main 이 업로드 직전에 ``web_report.temperature.clean_frames`` 로
rawdata 를 정리한다(RT pass 좌표만 남기고 RT limit 으로 재판정).
"""
from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

ROLES = ("RT", "CT", "HT")
LIMIT_FILTER = "Limit Table (*.lt *.pds)"

# 파일명에서 온도 역할 토큰을 찾는다 (자동 그룹 제안 전용 — 못 찾으면 사용자가 끌어다 놓는다).
_ROLE_TOKEN = re.compile(r"(?:^|[^A-Za-z0-9])(RT|CT|HT)(?:[^A-Za-z0-9]|$)", re.IGNORECASE)


def suggest_groups(names) -> list:
    """파일명 기반 자동 그룹 제안 → [{"RT": name, "CT": name, "HT": name}, ...].

    이름에서 RT/CT/HT 토큰을 떼어낸 나머지(stem)가 같은 것끼리 한 그룹으로 묶는다.
    토큰이 없거나 같은 stem·역할이 겹치면 그 source 는 제안에서 빠진다(미배정으로 남는다).
    """
    buckets: dict = {}
    order: list = []
    for name in names:
        m = _ROLE_TOKEN.search(str(name))
        if not m:
            continue
        role = m.group(1).upper()
        stem = (str(name)[:m.start(1)] + str(name)[m.end(1):]).strip(" _-.")
        if stem not in buckets:
            buckets[stem] = {}
            order.append(stem)
        if role not in buckets[stem]:
            buckets[stem][role] = name
    return [buckets[s] for s in order if "RT" in buckets[s]]


class _DropList(QListWidget):
    """source 를 끌어다 놓는 리스트. capacity=1 이면 역할 1칸(RT/CT/HT) 자리다.

    Qt 기본 drag&drop 직렬화는 UserRole 데이터를 보존하지 않으므로, 아이템을 직접
    take/add 로 옮긴다 (텍스트=source 이름이 곧 키라 그대로 옮겨도 정보 손실이 없다).
    """

    def __init__(self, capacity=None, pool=None):
        super().__init__()
        self._capacity = capacity
        self._pool = pool                 # capacity 초과분을 되돌릴 미배정 리스트
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setUniformItemSizes(True)

    def _accept_drag(self, event):
        if isinstance(event.source(), _DropList) and event.source() is not None:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragEnterEvent(self, event):
        self._accept_drag(event)

    def dragMoveEvent(self, event):
        self._accept_drag(event)

    def dropEvent(self, event):
        src = event.source()
        if not isinstance(src, _DropList) or src is self:
            event.ignore()
            return
        items = src.selectedItems()
        if not items:
            event.ignore()
            return
        if self._capacity is not None:
            items = items[:self._capacity]
            # 이미 차 있으면 기존 항목을 미배정으로 되돌린다 (자리 교체).
            while self.count() >= self._capacity and self.count():
                self._return_to_pool(self.takeItem(0))
        for it in items:
            self.addItem(src.takeItem(src.row(it)))
        event.accept()

    def _return_to_pool(self, item):
        (self._pool or self).addItem(item)

    def names(self) -> list:
        return [self.item(i).text() for i in range(self.count())]


class _LimitsDropArea(QFrame):
    """.lt / .pds 파일을 끌어다 놓는 영역 (버튼으로도 고를 수 있다)."""

    def __init__(self, on_files):
        super().__init__()
        self._on_files = on_files
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame { border: 1px dashed #94a3b8; border-radius: 6px; background: #f8fafc; }")
        self.setMinimumHeight(52)

    def _paths(self, mime):
        return [u.toLocalFile() for u in mime.urls()
                if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in (".lt", ".pds")]

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and self._paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        paths = self._paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self._on_files(paths)
        else:
            event.ignore()


class TemperatureGroupDialog(QDialog):
    """exec() 가 참을 돌려주면 result_groups() 로 배치 결과를 읽는다."""

    def __init__(self, parent, names):
        super().__init__(parent)
        self.setWindowTitle("Temperature — RT / CT / HT 그룹 배치")
        self.resize(880, 620)
        self._original = [str(n) for n in names]
        self._bin_map = None
        self._limits_file = None
        self._rows: list = []              # [{role: _DropList}, ...] — 그룹 행

        self.pool = _DropList()
        self.pool.setMaximumHeight(150)

        self._groups_box = QVBoxLayout()
        self._groups_box.setSpacing(6)
        self._groups_box.addStretch(1)
        inner = QWidget()
        inner.setLayout(self._groups_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)

        btn_add = QPushButton("그룹 추가")
        btn_add.clicked.connect(lambda: self._add_group())
        btn_auto = QPushButton("파일명으로 자동 배치")
        btn_auto.setToolTip("이름의 RT/CT/HT 토큰을 보고 그룹을 제안합니다.")
        btn_auto.clicked.connect(self._auto_arrange)
        btn_reset = QPushButton("전체 초기화")
        btn_reset.clicked.connect(self._reset)

        top_btns = QHBoxLayout()
        for b in (btn_auto, btn_add, btn_reset):
            top_btns.addWidget(b)
        top_btns.addStretch(1)

        self.lbl_limits = QLabel("불러온 파일 없음 — bin 매칭은 RT 에서 죽은 bin → 999 순으로 처리합니다.")
        self.lbl_limits.setStyleSheet("color:#64748b;")
        btn_pick = QPushButton("파일 선택…")
        btn_pick.clicked.connect(self._pick_limits)
        drop = _LimitsDropArea(self._load_limits)
        drop_layout = QHBoxLayout(drop)
        drop_layout.addWidget(QLabel(".lt / .pds 파일을 여기에 끌어다 놓으세요"))
        drop_layout.addStretch(1)
        drop_layout.addWidget(btn_pick)

        hint = QLabel(
            "· source 를 끌어다 각 그룹의 RT / CT / HT 자리에 놓으세요 (자리끼리도 이동 가능).\n"
            "· 음영 표시된 **RT 가 그 그룹의 Limit(HILIM/LOLIM) 기준**입니다. CT/HT 는 없어도 됩니다.\n"
            "· Start 를 누르면 CT/HT 는 RT 의 Bin1 좌표만 남기고 RT limit 으로 다시 판정합니다.")
        hint.setStyleSheet("color:#64748b;")

        buttons = QDialogButtonBox()
        buttons.addButton("Start", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("취소", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("미배정 source"))
        root.addWidget(self.pool)
        root.addLayout(top_btns)
        root.addWidget(scroll, 1)
        root.addWidget(QLabel("Limit 파일 (.lt / .pds) — 재판정 fail 의 bin 매칭에 사용"))
        root.addWidget(drop)
        root.addWidget(self.lbl_limits)
        root.addWidget(hint)
        root.addWidget(buttons)

        self._reset()
        self._auto_arrange()

    # ── 그룹 행 ──────────────────────────────────────────────────────────────
    def _add_group(self):
        idx = len(self._rows)
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        grid = QGridLayout(frame)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.addWidget(QLabel(f"<b>Group {idx + 1}</b>"), 0, 0)

        slots = {}
        for col, role in enumerate(ROLES):
            label = QLabel("RT (Limit 기준)" if role == "RT" else role)
            slot = _DropList(capacity=1, pool=self.pool)
            slot.setFixedHeight(46)
            if role == "RT":
                label.setStyleSheet("color:#b45309; font-weight:700;")
                slot.setStyleSheet("QListWidget { background: #fef3c7; }")
            grid.addWidget(label, 0, col + 1)
            grid.addWidget(slot, 1, col + 1)
            grid.setColumnStretch(col + 1, 1)
            slots[role] = slot

        btn_del = QPushButton("삭제")
        btn_del.clicked.connect(lambda _=False, f=frame: self._remove_group(f))
        grid.addWidget(btn_del, 1, 0)

        self._groups_box.insertWidget(self._groups_box.count() - 1, frame)
        self._rows.append({"frame": frame, "slots": slots})
        return slots

    def _remove_group(self, frame):
        for row in list(self._rows):
            if row["frame"] is frame:
                for slot in row["slots"].values():
                    while slot.count():
                        self.pool.addItem(slot.takeItem(0))
                self._rows.remove(row)
                break
        frame.setParent(None)
        self._renumber()

    def _renumber(self):
        for i, row in enumerate(self._rows):
            row["frame"].layout().itemAtPosition(0, 0).widget().setText(f"<b>Group {i + 1}</b>")

    def _reset(self):
        for row in list(self._rows):
            row["frame"].setParent(None)
        self._rows = []
        self.pool.clear()
        for name in self._original:
            self.pool.addItem(QListWidgetItem(name))
        self._add_group()

    def _take_from_pool(self, name):
        for i in range(self.pool.count()):
            if self.pool.item(i).text() == name:
                return self.pool.takeItem(i)
        return None

    def _auto_arrange(self):
        """파일명 토큰으로 그룹을 제안한다. 배치 못 한 source 는 미배정으로 남는다."""
        groups = suggest_groups(self._original)
        if not groups:
            return
        self._reset()
        for gi, mapping in enumerate(groups):
            slots = self._rows[gi]["slots"] if gi < len(self._rows) else self._add_group()
            for role, name in mapping.items():
                item = self._take_from_pool(name)
                if item is not None:
                    slots[role].addItem(item)

    # ── Limit 파일 ───────────────────────────────────────────────────────────
    def _pick_limits(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Limit 파일 선택", "", LIMIT_FILTER)
        if paths:
            self._load_limits(paths)

    def _load_limits(self, paths):
        """.lt/.pds 를 즉시 파싱해 항목→bin 매핑을 만든다. 실패는 경고 후 무시."""
        from web_report.temperature import load_limits_file

        merged, loaded = {}, []
        for path in paths:
            try:
                mapping, kind = load_limits_file(path)
            except Exception as exc:
                QMessageBox.warning(self, "Limit 파일 읽기 실패",
                                    f"{Path(path).name}\n{exc}")
                continue
            if not mapping:
                QMessageBox.warning(self, "Limit 파일 읽기 실패",
                                    f"{Path(path).name}\n항목을 하나도 찾지 못했습니다.")
                continue
            merged.update(mapping)
            loaded.append((Path(path).name, kind, len(mapping)))
        if not loaded:
            return
        self._bin_map = merged
        self._limits_file = {"name": loaded[0][0], "type": loaded[0][1]}
        self.lbl_limits.setText(
            " / ".join(f"{n} ({k}) — 항목 {c}건" for n, k, c in loaded))
        self.lbl_limits.setStyleSheet("color:#166534;")

    # ── 결과 ────────────────────────────────────────────────────────────────
    def _accept(self):
        if self.pool.count():
            QMessageBox.warning(self, "Temperature 배치",
                                "미배정 source 가 남아 있습니다. 모든 source 를 그룹에 배치해 주세요.\n"
                                f"({', '.join(self.pool.names())})")
            return
        rows = [r for r in self._rows
                if any(r["slots"][role].count() for role in ROLES)]
        if not rows:
            QMessageBox.warning(self, "Temperature 배치", "그룹을 1개 이상 구성해 주세요.")
            return
        missing = [i + 1 for i, r in enumerate(rows) if not r["slots"]["RT"].count()]
        if missing:
            QMessageBox.warning(self, "Temperature 배치",
                                f"Group {', '.join(map(str, missing))} 에 RT 가 없습니다.\n"
                                "RT 는 Limit 판정 기준이라 그룹마다 반드시 1개 필요합니다.")
            return
        self.accept()

    def result_groups(self) -> dict:
        """Start 결과.

        - ``groups`` : ``[{"rt": 이름, "members": [CT, HT]}, ...]`` — manifest.options.temperature
        - ``order``  : 업로드 순서 (그룹마다 RT → CT → HT). 서버 tables 순서가 이 순서다.
        - ``bin_map``: .lt/.pds 파싱 결과 (없으면 None)
        - ``limits_file``: 세션에 기록할 파일 정보 (없으면 None)
        """
        groups, order = [], []
        for row in self._rows:
            rt = row["slots"]["RT"].names()
            if not rt:
                continue
            members = [n for role in ("CT", "HT") for n in row["slots"][role].names()]
            groups.append({"rt": rt[0], "members": members})
            order += [rt[0]] + members
        return {"groups": groups, "order": order,
                "bin_map": self._bin_map, "limits_file": self._limits_file}

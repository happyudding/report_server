# -*- coding: utf-8 -*-
"""Temperature Source 배치 창 — **Group 칸 이름 입력 = 같은 그룹 전원 일괄 개명** (2026-08-25).

⚠ pytest 로 돌리지 말고 **단독 실행**할 것 (PyQt6 offscreen 필요):
    python tests/test_source_group_rename.py

고정하는 계약:
  1. Group 칸에 이름을 적으면 **그 그룹의 모든 행**의 legend 앞부분이 그 이름으로 바뀐다
     (역할 접미사 `_RT`/`_CT`/`_HT` 는 유지). 그룹 21개짜리 세션에서 source 이름을 하나씩
     고치지 않게 해 주는 기능이라 없어지면 실사용이 무너진다.
  2. 다른 그룹의 이름을 적으면 **그 그룹으로 이동**한다 (드롭다운 선택과 같은 뜻).
  3. 미지정 행에 새 이름을 적으면 **새 그룹**이 생긴다.
  4. 드롭다운(▼) 선택은 그 행만 그 그룹으로 옮긴다.

**왜 기계로 고정하는가 (2026-08-24 회귀의 재발 방지)**: Group 칸을 QComboBox(편집 가능)로
바꾸면서 `combo.lineEdit()` — **C++ 소유 객체의 임시 파이썬 래퍼** — 에 콜백을 걸었는데,
그 래퍼가 GC 되는 순간 연결이 함께 사라졌다. `receivers()` 는 2 그대로라 **예외도 로그도
없이** "이름을 적어도 아무 일이 안 일어난다" 로만 나타났고, 같은 창의 드롭다운·Role 콤보는
sender 가 파이썬이 만든 위젯이라 멀쩡해 원인을 더 가렸다. 그래서 이 테스트는 **gc.collect()
를 강제로 돌린 뒤** 동작을 확인한다 — 실기에서는 GC 시점이 무작위라 이걸 안 하면 통과한다.
"""
import gc
import os
import sys
from pathlib import Path

NAMES = ["WF1_RT", "WF1_CT", "WF1_HT", "WF2_RT", "WF2_CT", "WF2_HT"]


def make_dialog(cls, names=NAMES):
    """자동 배치까지 끝난 Temperature 배치 창 (역할은 파일명 토큰에서)."""
    entries = [(n, rf"C:\data\{n}.stdf") for n in names]
    return cls(None, entries, mode="Temperature",
               roles={n: n.split("_")[1] for n in names})


def group_edit(dlg, row_index):
    """그 행 Group 칸의 QLineEdit (드롭다운 안의 입력칸)."""
    return dlg.table.cellWidget(row_index, 2).lineEdit()


def type_group_name(dlg, row_index, text):
    """실사용과 같은 경로 — 타이핑 후 Enter (editingFinished)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    edit = group_edit(dlg, row_index)
    edit.setFocus()
    QTest.keyClicks(edit, text)
    QTest.keyClick(edit, Qt.Key.Key_Return)


def legends_of(dlg, gid):
    return [row.legend for row in dlg._rows if row.group == gid]


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "client"))
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    from PyQt6.QtWidgets import QApplication

    app = QApplication([])  # noqa: F841 — 위젯 생성에 필요

    from honey_ui.source_name_dialog import SourceNameDialog

    # ── 1. 이름 입력 → 같은 그룹 전원 일괄 개명 (GC 후에도) ──────────────────
    dlg = make_dialog(SourceNameDialog)
    assert legends_of(dlg, 1) == ["WF1_RT", "WF1_CT", "WF1_HT"], legends_of(dlg, 1)
    gc.collect()                     # ← 연결이 GC 로 죽던 회귀를 재현하는 지점
    type_group_name(dlg, 0, "ABC")
    app.processEvents()
    assert legends_of(dlg, 1) == ["ABC_RT", "ABC_CT", "ABC_HT"], legends_of(dlg, 1)
    assert legends_of(dlg, 2) == ["WF2_RT", "WF2_CT", "WF2_HT"], legends_of(dlg, 2)
    assert dlg._group_names.get(1) == "ABC", dlg._group_names
    print("  [ok] 그룹명 입력 -> 같은 그룹 3행 일괄 개명 (다른 그룹 무영향, gc 후)")

    # 두 번째 그룹도 같은 방식으로 (렌더가 다시 돈 뒤에도 연결이 산다)
    row2 = next(i for i, r in enumerate(dlg._rows) if r.group == 2)
    gc.collect()
    type_group_name(dlg, row2, "XYZ")
    app.processEvents()
    assert legends_of(dlg, 2) == ["XYZ_RT", "XYZ_CT", "XYZ_HT"], legends_of(dlg, 2)
    print("  [ok] 재렌더 뒤에도 동작 (연결이 렌더마다 새로 살아난다)")

    # Enter 없이 다른 곳을 눌러도(포커스 아웃) 확정된다 — 이름을 적고 바로 OK 를 누르는
    # 실사용 경로다. 포커스 이벤트가 필요하므로 창을 실제로 띄운다.
    dlg2 = make_dialog(SourceNameDialog)
    dlg2.show()
    app.processEvents()
    gc.collect()
    edit = group_edit(dlg2, 0)
    edit.setFocus()
    app.processEvents()
    from PyQt6.QtTest import QTest as _QTest
    _QTest.keyClicks(edit, "QWE")
    dlg2.table.setFocus()                    # 포커스 아웃 = editingFinished
    app.processEvents()
    assert legends_of(dlg2, 1) == ["QWE_RT", "QWE_CT", "QWE_HT"], legends_of(dlg2, 1)
    print("  [ok] Enter 없이 포커스 아웃만으로도 확정 (적고 바로 OK)")

    # 개명 결과가 그대로 업로드 배치로 나간다
    out = dlg.result_arrangement()
    assert out["names"][:3] == ["ABC_RT", "ABC_CT", "ABC_HT"], out["names"]
    assert out["groups"][0]["rt"] == "ABC_RT", out["groups"]
    assert sorted(out["groups"][0]["members"]) == ["ABC_CT", "ABC_HT"], out["groups"]
    print("  [ok] result_arrangement 에 개명 결과 반영 (groups/names)")

    # ── 2. 다른 그룹 이름을 적으면 그 그룹으로 이동 ─────────────────────────
    dlg = make_dialog(SourceNameDialog)
    type_group_name(dlg, 0, "AAA")            # 그룹1 = AAA
    row2 = next(i for i, r in enumerate(dlg._rows) if r.group == 2)
    gc.collect()
    type_group_name(dlg, row2, "AAA")         # 그룹2 의 한 행 → 그룹1 로 이동
    app.processEvents()
    moved = dlg._rows[row2] if dlg._rows[row2].group == 1 else \
        next(r for r in dlg._rows if r.legend.startswith("AAA") and r.role == "RT")
    assert moved.group == 1, [(r.legend, r.group) for r in dlg._rows]
    assert len(legends_of(dlg, 1)) == 4, legends_of(dlg, 1)
    print("  [ok] 다른 그룹 이름 입력 -> 그 그룹으로 이동")

    # ── 3. 미지정 행에 새 이름 → 새 그룹 생성 ───────────────────────────────
    dlg = make_dialog(SourceNameDialog)
    dlg._rows[5].group = 0                    # 마지막 행을 미지정으로 만든다
    dlg._render()
    gc.collect()
    type_group_name(dlg, 5, "NEW")
    app.processEvents()
    assert dlg._rows[5].group not in (0, 1, 2), [(r.legend, r.group) for r in dlg._rows]
    assert dlg._rows[5].legend.startswith("NEW"), dlg._rows[5].legend
    print("  [ok] 미지정 행에 새 이름 -> 새 그룹 생성")

    # ── 4. 드롭다운 선택 = 그 행만 그룹 이동 (2026-08-24 추가 기능 유지) ────
    dlg = make_dialog(SourceNameDialog)
    combo = dlg.table.cellWidget(3, 2)         # WF2_RT 행
    idx = next(i for i in range(combo.count()) if combo.itemData(i) == 1)
    gc.collect()
    combo.setCurrentIndex(idx)
    combo.activated.emit(idx)                  # 사용자가 목록에서 고른 것과 같은 신호
    app.processEvents()
    assert dlg._rows[3].group == 1, [(r.legend, r.group) for r in dlg._rows]
    assert dlg._rows[4].group == 2, "같은 그룹의 다른 행까지 따라 옮겨졌다"
    print("  [ok] 드롭다운 선택 -> 그 행만 그룹 이동")

    print("[통과] Group 칸 이름 일괄 개명 + 이동 + 드롭다운 정상")


if __name__ == "__main__":
    main()

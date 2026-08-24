# -*- coding: utf-8 -*-
"""SourceNameDialog Temperature 그룹 드롭다운·Role 짝 검증 (2026-08-24).

⚠ pytest 로 돌리지 말고 **단독 실행**할 것 (PyQt6 offscreen 필요):
    python tests/test_source_group_dropdown.py

검사 대상 (client/honey_ui/source_name_dialog.py):
- Group 칸 드롭다운으로 행을 다른 그룹으로 이동 / 다른 그룹 이름 타이핑도 이동
- 그룹 이름 일괄 개명(종전 동작) 유지 + 드롭다운 표시 문자열 잔존 가드
- _accept: 같은 그룹 CT/HT 중복 차단 + 그룹별 Role 구성 불일치 확인 질문
- 이동 후 result_arrangement groups 정합
"""
import os
import sys
from pathlib import Path


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))

    from PyQt6.QtWidgets import QApplication, QComboBox

    app = QApplication([])  # noqa: F841 — 위젯 생성에 필요

    import honey_ui.source_name_dialog as snd
    from honey_ui.source_name_dialog import SourceNameDialog

    # QMessageBox 를 기록형 가짜로 바꾼다 — 모달이 뜨면 하니스가 멈춘다.
    class FakeMsgBox:
        class StandardButton:
            Ok = snd.QMessageBox.StandardButton.Ok
            Cancel = snd.QMessageBox.StandardButton.Cancel

        warnings = []
        questions = []
        question_reply = StandardButton.Ok

        @classmethod
        def warning(cls, *a, **k):
            cls.warnings.append(a[2] if len(a) > 2 else "")

        @classmethod
        def question(cls, *a, **k):
            cls.questions.append(a[2] if len(a) > 2 else "")
            return cls.question_reply

        @classmethod
        def information(cls, *a, **k):
            pass

        @classmethod
        def reset(cls):
            cls.warnings, cls.questions = [], []
            cls.question_reply = cls.StandardButton.Ok

    snd.QMessageBox = FakeMsgBox

    entries = [("LOTA_RT", r"C:\x\LOTA_RT.csv"), ("LOTA_CT", r"C:\x\LOTA_CT.csv"),
               ("LOTA_HT", r"C:\x\LOTA_HT.csv"), ("LOTB_RT", r"C:\x\LOTB_RT.csv"),
               ("LOTB_CT", r"C:\x\LOTB_CT.csv"), ("LOTB_HT", r"C:\x\LOTB_HT.csv")]

    def make(ent=entries):
        FakeMsgBox.reset()
        return SourceNameDialog(None, ent, mode="Temperature")

    def rowmap(dlg):
        return {row.legend: (row.group, row.role) for row in dlg._rows}

    def combo_at(dlg, r):
        w = dlg.table.cellWidget(r, 2)
        assert isinstance(w, QComboBox), f"row {r} Group 칸이 QComboBox 가 아님: {type(w)}"
        return w

    def idx_of_gid(combo, gid):
        for i in range(combo.count()):
            if int(combo.itemData(i) or 0) == gid:
                return i
        raise AssertionError(f"gid {gid} 항목 없음")

    def row_of(dlg, legend):
        return next(i for i, row in enumerate(dlg._rows) if row.legend == legend)

    # (a) 자동 배치: 2그룹 × RT/CT/HT
    dlg = make()
    rm = rowmap(dlg)
    assert len(dlg._rows) == 6 and {g for g, _ in rm.values()} == {1, 2}, rm
    for lot in ("LOTA", "LOTB"):
        assert len({rm[f"{lot}_{ro}"][0] for ro in ("RT", "CT", "HT")}) == 1, rm
    assert all(rm[n][1] == n.split("_")[1] for n in rm), rm
    print("[ok] (a) 자동 배치 2그룹 x RT/CT/HT")

    # (b) 드롭다운 구성: (미지정) + 그룹1 + 그룹2, 기본 표시는 placeholder
    c0 = combo_at(dlg, 0)
    items = [(c0.itemText(i), int(c0.itemData(i) or 0)) for i in range(c0.count())]
    assert items == [("(미지정)", 0), ("그룹 1", 1), ("그룹 2", 2)], items
    assert c0.lineEdit().text() == "" and c0.lineEdit().placeholderText() == "그룹 1"
    print("[ok] (b) 드롭다운 항목/placeholder")

    # (c) 드롭다운 선택 = 그룹 이동
    r = row_of(dlg, "LOTB_CT")
    dlg._on_group_pick(r, idx_of_gid(combo_at(dlg, r), 1), combo_at(dlg, r))
    rm = rowmap(dlg)
    assert rm["LOTB_CT"][0] == 1 and rm["LOTB_RT"][0] == 2 and rm["LOTB_HT"][0] == 2, rm
    print("[ok] (c) 드롭다운 선택으로 그룹 이동")

    # (d) 다른 그룹 '기본 표시명' 타이핑 = 이동 (원복)
    r = row_of(dlg, "LOTB_CT")
    c = combo_at(dlg, r)
    c.lineEdit().setText("그룹 2")
    dlg._on_group_text(r, c.lineEdit())
    rm = rowmap(dlg)
    assert rm["LOTB_CT"][0] == rm["LOTB_RT"][0], rm
    print("[ok] (d) 다른 그룹 표시명 타이핑 = 이동")

    # (e) 그룹 이름 일괄 변경 유지 + 드롭다운에 이름 반영
    r = next(i for i, row in enumerate(dlg._rows) if row.group == 1)
    c = combo_at(dlg, r)
    c.lineEdit().setText("ABC")
    dlg._on_group_text(r, c.lineEdit())
    legends1 = [row.legend for row in dlg._rows if row.group == 1]
    assert all(lg.startswith("ABC_") for lg in legends1), legends1
    c2 = combo_at(dlg, next(i for i, row in enumerate(dlg._rows) if row.group == 2))
    names = [c2.itemText(i) for i in range(c2.count())]
    assert "ABC" in names and "그룹 2" in names, names
    print("[ok] (e) 이름 일괄 변경 + 드롭다운 반영")

    # (f) 자기 그룹 표시 문자열 잔존 가드 — 이름 생성 금지
    r = next(i for i, row in enumerate(dlg._rows) if row.group == 2)
    c = combo_at(dlg, r)
    c.lineEdit().setText("그룹 2")
    dlg._on_group_text(r, c.lineEdit())
    assert 2 not in dlg._group_names, dlg._group_names
    print("[ok] (f) 표시 문자열 가드 (이름 미생성)")

    # (f2) 다른 그룹의 사용자 이름 타이핑 = 이동 (구 '중복 거부' 폐지)
    dlg2 = make()
    r = next(i for i, row in enumerate(dlg2._rows) if row.group == 1)
    c = combo_at(dlg2, r)
    c.lineEdit().setText("ABC")
    dlg2._on_group_text(r, c.lineEdit())
    r = row_of(dlg2, "LOTB_HT")
    c = combo_at(dlg2, r)
    c.lineEdit().setText("ABC")
    dlg2._on_group_text(r, c.lineEdit())
    rm2 = rowmap(dlg2)
    assert rm2["ABC_HT"][0] == 1 and not FakeMsgBox.warnings, (rm2, FakeMsgBox.warnings)
    print("[ok] (f2) 사용자 이름 타이핑 = 이동 (거부 없음)")

    # (g) _accept: 같은 그룹 CT 중복 차단
    dlg3 = make()
    r = row_of(dlg3, "LOTB_CT")
    dlg3._on_group_pick(r, idx_of_gid(combo_at(dlg3, r), 1), combo_at(dlg3, r))
    FakeMsgBox.reset()
    dlg3._accept()
    assert FakeMsgBox.warnings and "CT" in FakeMsgBox.warnings[0], FakeMsgBox.warnings
    assert not dlg3.result(), "중복인데 accept 됨"
    print("[ok] (g) CT 중복 차단")

    # (h) _accept: 그룹별 Role 구성 불일치 → 확인 질문 (5개 입력 = 그룹2 에 HT 없음)
    dlg4 = make(entries[:5])
    assert {g for g, _ in rowmap(dlg4).values()} == {1, 2}, rowmap(dlg4)
    FakeMsgBox.question_reply = FakeMsgBox.StandardButton.Cancel
    dlg4._accept()
    assert FakeMsgBox.questions and "구성" in FakeMsgBox.questions[0], FakeMsgBox.questions
    assert not dlg4.result(), "Cancel 인데 accept 됨"
    FakeMsgBox.reset()
    dlg4._accept()   # Ok 응답 → 구성 확인 + 최종 생성 확인 2질문 후 accept
    assert len(FakeMsgBox.questions) == 2 and dlg4.result(), FakeMsgBox.questions
    print("[ok] (h) 구성 불일치 확인 질문 (Cancel 차단 / Ok 진행)")

    # (i) 균형 배치는 경고 없이 최종 생성 확인만
    dlg5 = make()
    dlg5._accept()
    assert len(FakeMsgBox.questions) == 1 and "생성" in FakeMsgBox.questions[0]
    assert not FakeMsgBox.warnings and dlg5.result()
    print("[ok] (i) 균형 배치 = 경고 없음")

    # (j) 이동 후 result_arrangement groups 정합
    dlg6 = make()
    r = row_of(dlg6, "LOTB_CT")
    dlg6._on_group_pick(r, idx_of_gid(combo_at(dlg6, r), 1), combo_at(dlg6, r))
    res = dlg6.result_arrangement()
    g1, g2 = res["groups"]
    assert g1["rt"] == "LOTA_RT" and set(g1["members"]) == {"LOTA_CT", "LOTA_HT", "LOTB_CT"}
    assert g2["members"] == ["LOTB_HT"], res["groups"]
    print("[ok] (j) result_arrangement 그룹 정합")

    print("\n전체 통과")


if __name__ == "__main__":
    main()

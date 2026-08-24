"""source legend 기본값 규칙(product_type 별 파일명 파싱) 회귀 테스트.

실행:
    python tests/test_source_naming.py

고정하는 계약:
  - product_type 별 규칙은 ``_SOURCE_NAME_RULES`` 표 하나가 정본이다. MDDI 는 00M/00P/00F
    마커, PDDI 는 stdf_ 고정 위치, PMIC/SECURITY/TCON 은 LOT+WF 로 **같은 함수**를 쓴다.
  - 규칙에 안 맞으면 None → 빈 문자열 → rename_sources 가 "기존명 유지"로 해석한다.
    MDDI 의 2차 규칙(xlsx 시트명)은 honey_parse 소관이라 여기서 만들지 않는 게 **의도**다.
  - 입력 파일 개수 ≠ source 개수(CLAUDE.md #9)인데 rename_sources 는 positional 이다.
    resolve_source_names 가 raw → collapse → None 순으로 대조해 오배치를 막는다.
  - collapse 는 인접(run)이 아니라 **첫 등장** 기준이고, 빈 이름이 섞이면 포기한다.
  - guess(파싱 전) 가 값을 주면 suggest(파싱 후)와 **원소별로 같아야** 한다 — 어긋나면
    Temperature 배치 창이 두 번 뜬다.

PyQt6 위젯을 만들지 않는 **순수 함수만** 검사한다(헤드리스에서 QApplication 불필요).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

from honey_ui.source_naming import (apply_role_suffix,  # noqa: E402
                                    collapse_merged_names,
                                    guess_source_names,
                                    lot_id_for,
                                    lot_wf_lot_id,
                                    lot_wf_source_name,
                                    resolve_source_names,
                                    role_of_name,
                                    source_name_for,
                                    suggest_source_names)


def test_lot_wf_rule_unchanged():
    """회귀 가드 — 기존 PMIC 규칙의 docstring 예시가 그대로 나온다."""
    assert lot_wf_source_name("awjkelfjwkalef_602XX2_3_jkqewjklqjetk.std") == "602XX2_3"
    assert lot_wf_source_name("T2K_6Z1234_W03_260505.csv") == "6Z1234_W03"
    # 'final' 은 WF 가 아니다 (_WF_RE 전체 일치)
    assert lot_wf_source_name("602XX2_final.std") == "602XX2"
    # 파일명 맨 앞 LOT 도 인정
    assert lot_wf_source_name("602XX2_3.csv") == "602XX2_3"
    # LOT 과 무관한 위치의 'wow' 를 W tail 로 잡지 않는다
    assert lot_wf_source_name("awj_602XX2_3_wow.csv") == "602XX2_3"
    # 대소문자를 무시하지 않는다 — 소문자 LOT 은 규칙 밖(기존명 유지)
    assert lot_wf_source_name("t2k_6z1234_w03.csv") is None


def test_lot_header_6a_added():
    """6A 만 추가 — 8 계열과 기존 6 계열은 그대로, 확장이 새지 않는다."""
    assert lot_wf_source_name("T2K_6A1234_W03.csv") == "6A1234_W03"
    assert lot_wf_source_name("6A99_3.csv") == "6A99_3"
    # 기존 인정 토큰 유지
    for head in ("60", "61", "62", "68", "6Z", "80", "81", "82", "8Z"):
        assert lot_wf_source_name(f"T2K_{head}1234_W03.csv") == f"{head}1234_W03"
    # 확장이 새지 않았다 — 비-csv 에선 6 계열 다른 문자와 8 계열 A 는 여전히 LOT 이 아니다
    # (.csv 는 2026-08-24 확장 폴백 대상 — test_lot_header_csv_fallback)
    for head in ("63", "6B", "6Y", "8A", "88"):
        assert lot_wf_source_name(f"T2K_{head}1234_W03.std") is None


def test_lot_header_csv_fallback():
    """.csv 만 화이트리스트 실패 시 6x/8x 전 조합을 헤더로 인정한다(2026-08-24)."""
    for head in ("63", "6B", "6Y", "8A", "88"):
        assert lot_wf_source_name(f"T2K_{head}1234_W03.csv") == f"{head}1234_W03"
        assert lot_wf_lot_id(f"T2K_{head}1234_W03.csv") == f"{head}1234"
    # 파일명 맨 앞 토큰도 인정
    assert lot_wf_source_name("8A99_3.csv") == "8A99_3"
    # 화이트리스트가 먼저다 — 확장 토큰(8A)이 앞에 있어도 화이트리스트 토큰(6Z)이 이긴다
    assert lot_wf_source_name("8A_6Z1234_W03.csv") == "6Z1234_W03"
    # 소문자 배제·비-csv 확장자 배제는 그대로
    assert lot_wf_source_name("t2k_8a1234_w03.csv") is None
    assert lot_wf_source_name("T2K_8A1234_W03.std") is None


def test_lot_wf_rule_covers_three_product_types():
    """PMIC/SECURITY/TCON 이 같은 규칙을 쓴다 (2026-08-11 — 종전 PMIC 전용)."""
    paths = ["awj_602XX2_3.std", "T2K_6Z1234_W03.csv"]
    expected = ["602XX2_3", "6Z1234_W03"]
    for pt in ("PMIC", "SECURITY", "TCON"):
        assert suggest_source_names(paths, pt) == expected, pt
    # 소문자 product_type 도 같게 정규화된다
    assert suggest_source_names(paths, "pmic") == expected


def test_mddi_marker_rule():
    """00M/00P/00F 3종이 같은 legend 를 낸다 (= honey_parse 병합 대상)."""
    for marker in ("00M", "00P", "00F"):
        assert source_name_for(f"NH0D3-{marker}.W03", "MDDI") == "NH0D3_03"
    assert source_name_for("NH0D3-00F.W05", "MDDI") == "NH0D3_05"
    # 구분자가 '_' 여도 동작하고, 경로가 붙어도 파일명만 본다
    assert source_name_for("NH0D3_00M.W03", "MDDI") == "NH0D3_03"
    assert source_name_for(r"C:\data\lot\NH0D3-00M.W03", "MDDI") == "NH0D3_03"
    # LOT 은 마커 앞부분 **전체** — LOT 안의 '-' 가 보존된다
    assert source_name_for("NH-0D3-00M.W03", "MDDI") == "NH-0D3_03"
    # LOT 이 없으면 규칙 미적용
    assert source_name_for("00M.W03", "MDDI") is None
    # 마커가 없으면 규칙 미적용 = 규칙 2(xlsx 시트명)는 honey_parse 소관
    assert source_name_for("NH0D3_wafer03.csv", "MDDI") is None


def test_mddi_blank_keeps_existing_name():
    """마커가 하나도 없으면 None(기존명 유지), 섞이면 그 자리만 빈 문자열."""
    assert suggest_source_names(["a.csv", "b.csv"], "MDDI") is None
    assert suggest_source_names(["a.csv", "NH0D3-00M.W03"], "MDDI") == ["", "NH0D3_03"]


def test_pddi_fixed_position():
    """stdf_[LOTID]_[STEP]_[WFNO]_[PARTID] 고정 위치 — STEP 이 달라도 같은 legend."""
    assert source_name_for("stdf_ABC123_L1_03_PARTX.stdf", "PDDI") == "ABC123_03"
    assert source_name_for("stdf_ABC123_L2_03_PARTX.stdf", "PDDI") == "ABC123_03"
    # .stem 으로 확장자를 먼저 뗀다 — 안 그러면 WFNO 가 '03.stdf' 가 된다
    assert source_name_for("stdf_ABC123_L1_03.stdf", "PDDI") == "ABC123_03"
    # 토큰이 모자라면 규칙 미적용
    assert source_name_for("stdf_ABC123_L1.stdf", "PDDI") is None
    # stdf_ 접두 필수 — 무관한 파일이 legend 를 오염시키지 않는다
    assert source_name_for("other_A_B_C_D.txt", "PDDI") is None
    assert source_name_for("log_2026_08_11_run.txt", "PDDI") is None
    # 접두 대소문자는 무시
    assert source_name_for("STDF_ABC123_L1_03_P.stdf", "PDDI") == "ABC123_03"


def test_collapse_first_appearance():
    """첫 등장 순서로 접는다 — 인접(run) 기준이 아니다."""
    assert collapse_merged_names(["X_03"] * 3 + ["X_05"] * 3) == ["X_03", "X_05"]
    # 교차 배치(타입 순 입력)에서도 웨이퍼 개수로 접혀야 한다 — run 방식이면 실패한다
    assert collapse_merged_names(["X_03", "X_05", "X_03", "X_05"]) == ["X_03", "X_05"]
    # 접을 게 없으면 None (호출부가 raw 를 쓰게 둔다)
    assert collapse_merged_names(["X_03", "X_05"]) is None
    assert collapse_merged_names([]) is None
    # 빈 이름이 섞이면 포기 — 길이만 우연히 맞고 배치는 틀린다
    assert collapse_merged_names(["", "X_03", "X_03"]) is None


def test_resolve_prefers_raw_length():
    """raw → collapsed → None. 확신이 없으면 기존명을 유지한다."""
    paths = [f"NH0D3-00{m}.W{w}" for w in ("03", "05") for m in ("M", "P", "F")]
    # 6파일/6source (이 저장소의 현재 배선) → raw. 중복은 rename_sources 가 _2/_3 로 가른다
    assert resolve_source_names(paths, "MDDI", 6) == ["NH0D3_03"] * 3 + ["NH0D3_05"] * 3
    # 6파일/2source (honey_parse 병합) → collapsed
    assert resolve_source_names(paths, "MDDI", 2) == ["NH0D3_03", "NH0D3_05"]
    # 어느 쪽도 아니면 None — 조용한 오배치보다 기존명 유지
    assert resolve_source_names(paths, "MDDI", 4) is None
    # 1:1 product_type 은 raw 경로만 탄다
    pmic = ["awj_602XX2_3.std", "T2K_6Z1234_W03.csv"]
    assert resolve_source_names(pmic, "PMIC", 2) == ["602XX2_3", "6Z1234_W03"]
    assert resolve_source_names(pmic, "PMIC", 1) is None


def test_resolve_none_when_no_rule():
    """규칙이 없는 product_type·빈 입력·전원 미매치 → None(기존명 유지)."""
    assert resolve_source_names(["602XX2_3.csv"], "UNKNOWN", 1) is None
    assert resolve_source_names(["602XX2_3.csv"], "", 1) is None
    assert resolve_source_names(["602XX2_3.csv"], None, 1) is None
    assert resolve_source_names([], "PMIC", 0) is None
    assert resolve_source_names(["a.csv", "b.csv"], "PMIC", 2) is None


def test_guess_blocked_for_merging_types():
    """MDDI/PDDI 는 파싱 전에 source 개수를 모르므로 항상 None."""
    assert guess_source_names(["NH0D3-00M.W03", "NH0D3-00P.W03"], "MDDI") is None
    assert guess_source_names(["stdf_ABC123_L1_03_P.stdf"], "PDDI") is None
    # 1:1 product_type 은 전원 일치할 때만 값
    pmic = ["awj_602XX2_3.std", "T2K_6Z1234_W03.csv"]
    assert guess_source_names(pmic, "PMIC") == ["602XX2_3", "6Z1234_W03"]
    assert guess_source_names(pmic, "SECURITY") == ["602XX2_3", "6Z1234_W03"]
    assert guess_source_names(pmic + ["nomatch.csv"], "PMIC") is None


def test_guess_matches_suggest():
    """guess 가 값을 주면 suggest 와 원소별 완전 일치 — 어긋나면 창이 두 번 뜬다."""
    cases = [
        ["awj_602XX2_3.std", "T2K_6Z1234_W03.csv"],
        ["602XX2_3.csv", "6A1234_W03.csv", "68ABCD_W1.csv"],
        ["a.csv", "602XX2_3.csv"],           # 일부만 일치 → guess 는 None
    ]
    for pt in ("PMIC", "SECURITY", "TCON"):
        for paths in cases:
            guess = guess_source_names(paths, pt)
            if guess is not None:
                assert guess == suggest_source_names(paths, pt), (pt, paths)


def test_lot_id_by_product_type():
    """업로드 메타 lot_id 기본값도 같은 표를 조회한다."""
    assert lot_id_for("awjkelf_602XX2_3.std", "PMIC") == "602XX2"
    assert lot_id_for("T2K_6A1234_W03.csv", "TCON") == "6A1234"
    # PDDI 버그 회귀 가드 — 종전엔 head 폴백이 'stdf' 를 LOT 으로 잡았다
    assert lot_id_for("stdf_ABC123_L1_03_P.stdf", "PDDI") == "ABC123"
    assert lot_id_for("NH0D3-00M.W03", "MDDI") == "NH0D3"
    # 규칙 미적용 시 None → honey_main 이 종전 head 폴백을 탄다
    assert lot_id_for("stdf_ABC123.stdf", "PDDI") is None
    assert lot_id_for("602XX2_3.csv", "UNKNOWN") is None


def test_role_suffix_helpers_unchanged():
    """다이얼로그가 import 하는 유일 심볼 — 재구성 중 깨지지 않았는지 확인."""
    assert role_of_name("WF1_RT") == "RT"
    assert role_of_name("WF1_ht") == "HT"
    assert role_of_name("WF1") == ""
    assert apply_role_suffix("WF1", "CT") == "WF1_CT"
    assert apply_role_suffix("WF1_RT", "RT") == "WF1_RT"      # 중복 부착 금지
    assert apply_role_suffix("WF1_RT", "HT") == "WF1_HT"      # 교체
    assert apply_role_suffix("WF1_RT", "") == "WF1"           # 제거


def main():
    checks = 0
    for fn in (test_lot_wf_rule_unchanged,
               test_lot_header_6a_added,
               test_lot_wf_rule_covers_three_product_types,
               test_mddi_marker_rule,
               test_mddi_blank_keeps_existing_name,
               test_pddi_fixed_position,
               test_collapse_first_appearance,
               test_resolve_prefers_raw_length,
               test_resolve_none_when_no_rule,
               test_guess_blocked_for_merging_types,
               test_guess_matches_suggest,
               test_lot_id_by_product_type,
               test_role_suffix_helpers_unchanged):
        fn()
        checks += 1
    print(f"PASS: test_source_naming ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

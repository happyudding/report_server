"""Temperature 모드 .lt / .pds limit 테이블 파서 회귀 테스트.

실행:
    python tests/test_temperature_limits.py

고정하는 계약:
  - .lt 일반 case: 대괄호 **앞** 5번째 필드가 bin (LSL/USL 위반 같은 bin)
  - .lt 특수 case: 대괄호 안 ``20:19`` → LSL 위반 20 / USL 위반 19
  - .pds: [Datasheet Variable Map] **다음** 대괄호부터 Test Item, LSL_BIN/USL_BIN 방향별
  - 항목명 매칭: 정확 일치 + 선행 TNO 접두(T001_) 제거 일치

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.temperature import (bin_lookup, match_item,  # noqa: E402
                                    parse_lt_text, parse_pds_text)

LT_TEXT = """LimitTable T_ABC123_LT
{
#T_CONTACT
110, "T002_VBAT_NOS", ",4", "0V", 19, [-0.9, -0.2, 20:19];
120, "T003_VDD_NOS", ",4", "0V", 21, [-1.0, -0.1];
#T_LEAKAGE
1200, "T001_1GDM_IIH", ".4", "uA", 11, [-9.5,9.5];
}
"""

PDS_TEXT = """[Datasheet Preferences]
Something=1
[Datasheet Variable Map]
Var1=Foo
Var2=Bar
[T_CONTACT]
"",1,1,"G_SCHIN_NOS",-0.9,-0.2,9.4,"V","20","19"
"",2,2,"G_SCHIN_POS",-0.5,0.5,9.4,"V","22","23"
[T_LEAKAGE]
"",1200,1200,"T001_1GDM_IIH",-9.5,9.5,0.4,"uA","11","12"
"""


def test_lt_normal_case():
    """대부분 case — 대괄호 앞 bin 이 LSL/USL 양쪽 bin."""
    table = parse_lt_text(LT_TEXT)
    entry = table["T001_1GDM_IIH"]
    assert (entry["lsl_bin"], entry["usl_bin"]) == ("11", "11"), entry
    assert (entry["lsl"], entry["usl"]) == (-9.5, 9.5), entry
    assert entry["tno"] == "1200", entry

    entry = table["T003_VDD_NOS"]
    assert (entry["lsl_bin"], entry["usl_bin"]) == ("21", "21"), entry


def test_lt_special_case_direction_bins():
    """특수 case — 대괄호 안 20:19 가 LSL/USL 방향별 bin 으로 덮어쓴다."""
    entry = parse_lt_text(LT_TEXT)["T002_VBAT_NOS"]
    assert (entry["lsl_bin"], entry["usl_bin"]) == ("20", "19"), entry
    assert (entry["lsl"], entry["usl"]) == (-0.9, -0.2), entry


def test_lt_ignores_comments_and_braces():
    """주석(#)·중괄호 줄은 항목으로 잡히지 않는다."""
    table = parse_lt_text(LT_TEXT)
    assert set(table) == {"T002_VBAT_NOS", "T003_VDD_NOS", "T001_1GDM_IIH"}, sorted(table)


def test_pds_items_after_variable_map():
    """[Datasheet Variable Map] 이전 섹션은 무시하고, 이후 대괄호부터 Test Item."""
    table = parse_pds_text(PDS_TEXT)
    assert set(table) == {"G_SCHIN_NOS", "G_SCHIN_POS", "T001_1GDM_IIH"}, sorted(table)

    entry = table["G_SCHIN_NOS"]
    assert (entry["lsl_bin"], entry["usl_bin"]) == ("20", "19"), entry
    assert (entry["lsl"], entry["usl"]) == (-0.9, -0.2), entry
    assert entry["tno"] == "1", entry


def test_match_item_exact_and_tno_prefix():
    """항목명 매칭 — 정확 일치, 대소문자 무시, 선행 TNO 접두 제거."""
    index = bin_lookup(parse_pds_text(PDS_TEXT))
    assert match_item("G_SCHIN_NOS", index)["usl_bin"] == "19"
    assert match_item("g_schin_nos", index)["usl_bin"] == "19"
    # honeyform 컬럼이 T012_ 접두를 달고 있어도 접두 제거 후 매칭된다
    assert match_item("T012_G_SCHIN_NOS", index)["lsl_bin"] == "20"
    assert match_item("NOT_IN_TABLE", index) is None
    assert match_item("anything", {}) is None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_lt_normal_case, test_lt_special_case_direction_bins,
               test_lt_ignores_comments_and_braces, test_pds_items_after_variable_map,
               test_match_item_exact_and_tno_prefix):
        fn()
        checks += 1
    print(f"PASS: test_temperature_limits ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

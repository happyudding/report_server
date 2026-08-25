"""Temperature 모드 서버 payload 회귀 테스트.

실행:
    python tests/test_temperature_payload.py

고정하는 계약:
  1. 비RT(CT/HT) 소스의 수율 분모 = 남은 die 수(test) 강제 — Gross Die 가 있어도 무시.
     RT 는 기존 판정 규칙(Gross/test 자동 + 사용자 선택) 그대로.
  2. payload.sources[] 에 temp_role/temp_group/temp_corner, payload.temperature 에 그룹 구성.
  3. webreport_temperature_groups 는 깨진 옵션에 None(=Normal 렌더 폴백)을 돌려준다.
  4. **Normal 모드 payload 는 이 변경 전과 완전히 동일하다** (회귀 가드).
  5. Yield 계열(Yield 시트·Issue Table)은 **RT source 만** 본다 — CT/HT 컬럼 자체가 없다
     (2026-08-05). CT/HT 는 sheets["Issue Table Temp"] 로 나가고, 그 표의 소스 컬럼은
     CT/HT 만이며 분모는 강제된 test die 수다.

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402
from web_report.tabs.yield_tab import resolve_source_basis  # noqa: E402
from web_report.validation import (validate_mode,  # noqa: E402
                                   webreport_temperature_groups)


def make_table(source, n_dies, n_fail=1):
    """합성 honeyform 테이블 (test_yield_basis_auto.make_table 과 같은 형식)."""
    cols = META_COLUMNS + ["ItemA"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1],
        ["TNO", "", "", "", "", "", "", 100],
        ["STEP", "", "", "", "", "", "", "P1"],
        ["UNIT", "", "", "", "", "", "", "V"],
        ["HILIM", "", "", "", "", "", "", 10],
        ["LOLIM", "", "", "", "", "", "", 0],
    ]
    for i in range(n_dies):
        fail = i >= n_dies - n_fail
        rows.append([f"{source}-{i}", 1, 1, i % 10, i // 10,
                     4 if fail else 1, 100 if fail else "", 15 if fail else 5])
    return split_honeyform(pd.DataFrame(rows, columns=cols), source=source, file_name=source)


GROUPS = {"groups": [{"rt": "WF1_RT", "members": ["WF1_CT", "WF1_HT"]},
                     {"rt": "WF2_RT", "members": []}]}


def temp_tables():
    """RT 는 gross 200 에 근접(195), CT/HT 는 RT pass 좌표만 남아 100 행."""
    return [make_table("WF1_RT", 195, n_fail=5), make_table("WF1_CT", 100, n_fail=10),
            make_table("WF1_HT", 100, n_fail=20), make_table("WF2_RT", 190, n_fail=0)]


def test_mode_accepted():
    """Temperature 가 허용 모드 — 안 그러면 조용히 Normal 로 강등된다."""
    assert validate_mode("Temperature") == "Temperature"
    assert validate_mode("Nope") == "Normal"


def test_force_test_basis_for_members():
    """resolve_source_basis(force_test=...) — 비RT 는 Gross 선택도 무시하고 test 분모."""
    tables = temp_tables()
    basis_map = {"mode": "gross", "sources": {"WF1_CT": "gross"}}
    info = resolve_source_basis(tables, gross_die=200, basis_map=basis_map,
                                force_test={"WF1_CT", "WF1_HT"})
    for name in ("WF1_CT", "WF1_HT"):
        assert info[name]["basis"] == "test", info[name]
        assert info[name]["total"] == 100 and info[name]["forced"] is True, info[name]
        assert info[name]["gross_allowed"] is False, info[name]
    # RT 는 기존 규칙 — gross 200, 부족분 5 (<100) 이라 gross 분모
    assert info["WF1_RT"]["basis"] == "gross" and info["WF1_RT"]["total"] == 200, info["WF1_RT"]

    # force_test 없이 호출하면 종전과 완전히 동일
    assert resolve_source_basis(tables, gross_die=200, basis_map=basis_map) == \
        resolve_source_basis(tables, gross_die=200, basis_map=basis_map, force_test=None)


def test_payload_marks_roles_and_forces_denominator():
    payload = build_report_payload(temp_tables(), mode="Temperature", gross_die=200,
                                   temperature_groups=GROUPS)
    roles = {s["name"]: (s.get("temp_role"), s.get("temp_group")) for s in payload["sources"]}
    assert roles["WF1_RT"] == ("rt", 0) and roles["WF1_CT"] == ("member", 0), roles
    assert roles["WF2_RT"] == ("rt", 1), roles
    assert payload["temperature"] == GROUPS, payload["temperature"]

    by_src = {b["source"]: b for b in payload["yield_basis"]["by_source"]}
    assert by_src["WF1_CT"]["total"] == 100 and by_src["WF1_CT"]["basis"] == "test", by_src
    assert by_src["WF1_RT"]["total"] == 200, by_src["WF1_RT"]

    # Yield 시트는 RT source 만 — CT/HT 컬럼이 아예 없다 (2026-08-05)
    pass_row = payload["sheets"]["Yield"][0]
    assert "WF1_CT_yield" not in pass_row and "WF1_HT_yield" not in pass_row, pass_row
    assert "WF1_RT_yield" in pass_row and "WF2_RT_yield" in pass_row, pass_row
    # RT 만의 수율 요약 (WF1_RT 190/200=95%, WF2_RT 190/200=95%)
    assert [s["source"] for s in payload["yield_summary"]["by_source"]] == \
        ["WF1_RT", "WF2_RT"], payload["yield_summary"]["by_source"]


def test_option_parsing_and_fallbacks():
    names = ["WF1_RT", "WF1_CT", "WF1_HT", "WF2_RT"]
    opts = json.dumps({"temperature": GROUPS})
    assert webreport_temperature_groups(opts, names) == GROUPS

    # Excel 왕복으로 CT 가 사라지면 그 이름만 빠진다 (rt 는 살아있으므로 그룹 유지)
    parsed = webreport_temperature_groups(opts, ["WF1_RT", "WF1_HT", "WF2_RT"])
    assert parsed["groups"][0] == {"rt": "WF1_RT", "members": ["WF1_HT"]}, parsed

    # RT 가 사라진 그룹은 통째로 버린다
    parsed = webreport_temperature_groups(opts, ["WF1_CT", "WF2_RT"])
    assert parsed == {"groups": [{"rt": "WF2_RT", "members": []}]}, parsed

    # 깨진/없는 옵션 → None (Normal 과 동일하게 렌더)
    assert webreport_temperature_groups("", names) is None
    assert webreport_temperature_groups("{bad json", names) is None
    assert webreport_temperature_groups(json.dumps({"colors": ["#fff"]}), names) is None
    assert webreport_temperature_groups(opts, ["ZZZ"]) is None


def test_broken_groups_fall_back_and_are_detectable(caplog_list=None):
    """그룹 이름이 현재 source 와 어긋나면 **전체 source 로 계산**되고, 그걸 화면이
    감지할 수 있어야 한다 (2026-08-25 "전체를 RT로 인식" 신고).

    옵션의 RT 이름이 안 맞으면 webreport_temperature_groups 가 None 을 주고,
    metrics._temperature_context 가 `yield_tables = tables`(전체)로 폴백한다 —
    Yield/Issue Table 이 CT/HT 를 포함한 채 계산되는데 에러가 없다.

    **여기서 고정하는 계약은 `temp_corner` 부재다.** 프런트 경고 배지
    (distribution.js `tempGroupsBroken`)가 "Temperature 모드인데 어느 source 에도
    temp_corner 가 없다"로 판정하므로, 실패 시 이 필드가 붙어버리면 배지가 안 뜨고
    사용자는 다시 틀린 숫자를 말없이 보게 된다. payload 에 별도 경고 키를 두지 않은
    이유는 스키마 bump = 전 세션 콜드 폭풍이기 때문(cache_policy 주석).
    """
    tables = temp_tables()
    # 옵션은 살아 있지만 이름이 전부 어긋난 상태 (드리프트 재현)
    broken = webreport_temperature_groups(json.dumps({"temperature": GROUPS}), ["ZZZ"])
    assert broken is None, broken

    payload = build_report_payload(tables, mode="Temperature", temperature_groups=broken)

    # (a) 프런트 판정 근거 — temp_corner 가 하나도 없어야 한다
    assert all("temp_corner" not in s for s in payload["sources"]), payload["sources"]

    # (b) 현재 폴백 동작을 명시적으로 고정 — Yield 가 CT/HT 까지 포함한다.
    #     (자가 복구를 넣게 되면 이 단언이 깨지며 변경을 알린다 — 의도된 감지선이다.)
    yield_cols = set().union(*(set(r) for r in payload["sheets"]["Yield"])) \
        if payload["sheets"].get("Yield") else set()
    assert any("WF1_CT" in c for c in yield_cols), sorted(yield_cols)

    # (c) 정상 그룹이면 temp_corner 가 붙어 배지가 뜨지 않는다 (거짓 경고 방지)
    ok = build_report_payload(tables, mode="Temperature", temperature_groups=GROUPS)
    assert any(s.get("temp_corner") for s in ok["sources"]), ok["sources"]


def test_temp_sheet_sources_and_denominator():
    """Temp 시트 — 컬럼은 CT/HT 만, 분모는 강제된 test die 수, 구 corner 키는 없다."""
    payload = build_report_payload(temp_tables(), mode="Temperature", gross_die=200,
                                   temperature_groups=GROUPS)
    assert "yield_corner_groups" not in payload, sorted(payload)
    rows = payload["sheets"]["Issue Table Temp"]
    assert rows and str(rows[0]["Category"]) == "TEMP", rows[:1]
    data = [r for r in rows if str(r.get("Item") or "")]
    assert data, rows
    keys = set(data[0])
    assert "WF1_CT_yield" in keys and "WF1_HT_yield" in keys, sorted(keys)
    assert "WF1_RT_yield" not in keys and "WF2_RT_yield" not in keys, sorted(keys)
    # 분모는 남은 die 수(100) — CT fail 10 → 10%, HT fail 20 → 20%
    item_a = next(r for r in data if r["Item"] == "ItemA")
    assert (item_a["WF1_CT_yield"], item_a["WF1_HT_yield"]) == (10.0, 20.0), item_a
    assert item_a["avg"] == 15.0, item_a
    # Bin 은 limits 매핑이 없으면 관측 bin 폴백 (합성 데이터의 fail bin = 4)
    assert item_a["Bin"] == "4", item_a


def test_temp_sheet_absent_for_other_modes():
    """Temperature 아니면 Temp 시트는 빈 배열 — 프런트가 탭을 숨긴다."""
    payload = build_report_payload(temp_tables(), gross_die=200)
    assert payload["sheets"]["Issue Table Temp"] == [], payload["sheets"]["Issue Table Temp"]


def test_member_roles_and_temp_corner():
    """member_roles 가 있으면 그대로, 없으면 members 순서로 CT→HT 추정."""
    groups_with_roles = {"groups": [{"rt": "WF1_RT", "members": ["WF1_CT", "WF1_HT"],
                                     "member_roles": ["CT", "HT"]},
                                    {"rt": "WF2_RT", "members": []}]}
    payload = build_report_payload(temp_tables(), mode="Temperature", gross_die=200,
                                   temperature_groups=groups_with_roles)
    corner_of = {s["name"]: s.get("temp_corner") for s in payload["sources"]}
    assert corner_of == {"WF1_RT": "RT", "WF1_CT": "CT",
                         "WF1_HT": "HT", "WF2_RT": "RT"}, corner_of

    # member_roles 없는 옛 세션 — members 순서로 추정 (결과는 같다)
    payload = build_report_payload(temp_tables(), mode="Temperature", gross_die=200,
                                   temperature_groups=GROUPS)
    corner_of = {s["name"]: s.get("temp_corner") for s in payload["sources"]}
    assert corner_of["WF1_CT"] == "CT" and corner_of["WF1_HT"] == "HT", corner_of

    # 옵션 파싱이 member_roles 를 members 와 짝 맞춰 걸러낸다 (CT 가 사라지면 HT 만 남음)
    opts = json.dumps({"temperature": groups_with_roles})
    parsed = webreport_temperature_groups(opts, ["WF1_RT", "WF1_HT", "WF2_RT"])
    assert parsed["groups"][0] == {"rt": "WF1_RT", "members": ["WF1_HT"],
                                   "member_roles": ["HT"]}, parsed


def _issue_sections(rows):
    """Issue Table 행 → 섹션 라벨 순서 (프런트 rowSection 과 같은 규칙)."""
    return [str(r.get("Category")) for r in rows if str(r.get("Category") or "")]


def test_issue_table_has_no_temp_section():
    """Issue Table 은 Yield → CPK → ETC 뿐 — TEMP 는 별도 시트로 빠졌다."""
    payload = build_report_payload(temp_tables(), mode="Temperature", gross_die=200,
                                   temperature_groups=GROUPS, issue_comments={})
    rows = payload["sheets"]["Issue Table"]
    assert _issue_sections(rows) == ["Yield", "CPK", "ETC"], _issue_sections(rows)


def test_issue_table_is_rt_only():
    """Temperature 에서 Issue Table 은 RT 소스 컬럼만 갖는다 (CT/HT 컬럼 부재)."""
    payload = build_report_payload(temp_tables(), mode="Temperature", gross_die=200,
                                   temperature_groups=GROUPS)
    rows = payload["sheets"]["Issue Table"]
    pass_row = rows[0]
    assert str(pass_row["Bin"]) == "1" and str(pass_row["Category"]) == "Yield", pass_row
    assert pass_row["WF1_RT_yield"] not in ("", None), pass_row
    assert "WF1_CT_yield" not in pass_row and "WF1_HT_yield" not in pass_row, pass_row


def test_issue_table_row_key_roundtrip():
    """TEMP row_key 를 서버 3사본이 모두 같은 규약으로 해석한다."""
    from server.chatbot import rowkey as chatbot_rowkey
    from web_report.eval_export import _parse_row_key, _status_key

    assert _parse_row_key("TEMP|ItemA") == (None, "ItemA")
    assert _status_key("TEMP|ItemA") == "TEMP|ItemA"
    parsed = chatbot_rowkey.parse("TEMP|ItemA")
    assert parsed is not None and parsed.category == "TEMP" and parsed.item == "ItemA", parsed
    assert "TEMP" in chatbot_rowkey.CATEGORIES


def test_normal_mode_payload_unchanged():
    """회귀 가드 — Temperature 인자를 안 주면 payload 가 종전과 한 글자도 다르지 않다."""
    tables = temp_tables()
    base = build_report_payload(tables, gross_die=200)
    with_arg = build_report_payload(temp_tables(), gross_die=200, temperature_groups=GROUPS)
    dump = json.dumps(base, sort_keys=True, default=str)
    assert dump == json.dumps(with_arg, sort_keys=True, default=str), \
        "Normal 모드에서는 temperature_groups 가 payload 에 영향을 주면 안 된다"
    assert "temperature" not in base and \
        all("temp_role" not in s for s in base["sources"]), base["sources"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_mode_accepted, test_force_test_basis_for_members,
               test_payload_marks_roles_and_forces_denominator,
               test_option_parsing_and_fallbacks,
               test_broken_groups_fall_back_and_are_detectable,
               test_temp_sheet_sources_and_denominator,
               test_temp_sheet_absent_for_other_modes,
               test_member_roles_and_temp_corner,
               test_issue_table_has_no_temp_section,
               test_issue_table_is_rt_only,
               test_issue_table_row_key_roundtrip,
               test_normal_mode_payload_unchanged):
        fn()
        checks += 1
    print(f"PASS: test_temperature_payload ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

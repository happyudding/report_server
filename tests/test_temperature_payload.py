"""Temperature 모드 서버 payload 회귀 테스트.

실행:
    python tests/test_temperature_payload.py

고정하는 계약:
  1. 비RT(CT/HT) 소스의 수율 분모 = 남은 die 수(test) 강제 — Gross Die 가 있어도 무시.
     RT 는 기존 판정 규칙(Gross/test 자동 + 사용자 선택) 그대로.
  2. payload.sources[] 에 temp_role/temp_group/temp_corner, payload.temperature 에 그룹 구성.
  3. webreport_temperature_groups 는 깨진 옵션에 None(=Normal 렌더 폴백)을 돌려준다.
  4. **Normal 모드 payload 는 이 변경 전과 완전히 동일하다** (회귀 가드).
  5. payload.yield_corner_groups = RT Corner / Temp Corner 2표. 같은 소스의 fail% 는
     Corner 표와 전체 Yield 시트에서 **정확히 일치**한다(분모가 subset 에서도 불변).

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

    # 수율 표가 그 분모를 그대로 쓴다 (CT: 90/100 = 90%, HT: 80/100 = 80%)
    pass_row = payload["sheets"]["Yield"][0]
    assert (pass_row["WF1_CT_yield"], pass_row["WF1_HT_yield"]) == (90.0, 80.0), pass_row


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


def test_yield_corner_groups_split():
    """Corner 2표 — RT Corner 는 RT 소스만, Temp Corner 는 CT/HT 만."""
    payload = build_report_payload(temp_tables(), mode="Temperature", gross_die=200,
                                   temperature_groups=GROUPS)
    corners = payload["yield_corner_groups"]
    assert [c["corner"] for c in corners] == ["RT", "TEMP"], corners
    assert corners[0]["sources"] == ["WF1_RT", "WF2_RT"], corners[0]["sources"]
    assert corners[1]["sources"] == ["WF1_CT", "WF1_HT"], corners[1]["sources"]
    # 각 Corner 는 자기 소스 컬럼만 갖는다 (RT 표에 CT 컬럼이 새지 않는다)
    rt_keys = set(corners[0]["rows"][0])
    assert "WF1_CT_yield" not in rt_keys and "WF1_RT_yield" in rt_keys, sorted(rt_keys)
    # 표시용 STEP 분리와 누적 수율도 Corner 별로 따로 계산된다
    assert corners[1]["step_groups"] and corners[1]["by_step"], corners[1]


def test_corner_percentages_match_full_yield_sheet():
    """분모 불변 증명 — subset 재계산이어도 소스별 fail% 는 전체 Yield 시트와 같다."""
    payload = build_report_payload(temp_tables(), mode="Temperature", gross_die=200,
                                   temperature_groups=GROUPS)
    full = {(str(r.get("bin")), str(r.get("Item"))): r for r in payload["sheets"]["Yield"]}
    for corner in payload["yield_corner_groups"]:
        for row in corner["rows"]:
            ref = full[(str(row.get("bin")), str(row.get("Item")))]
            for src in corner["sources"]:
                for suffix in ("_yield", "_count"):
                    key = f"{src}{suffix}"
                    assert row[key] == ref[key], (corner["corner"], key, row[key], ref[key])


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


def test_issue_table_temp_section():
    """TEMP 섹션이 CPK 와 ETC 사이, 불량률 내림차순, row_key 는 TEMP|<item>."""
    payload = build_report_payload(temp_tables(), mode="Temperature", gross_die=200,
                                   temperature_groups=GROUPS, issue_comments={})
    rows = payload["sheets"]["Issue Table"]
    assert _issue_sections(rows) == ["Yield", "CPK", "TEMP", "ETC"], _issue_sections(rows)

    ti = next(i for i, r in enumerate(rows) if str(r.get("Category")) == "TEMP")
    ei = next(i for i, r in enumerate(rows) if str(r.get("Category")) == "ETC")
    temp_data = [r for r in rows[ti + 1:ei] if str(r.get("Item") or "")]
    assert temp_data, rows[ti:ei]
    # TEMP 행은 CT/HT 컬럼만 값이 있고, RT 컬럼은 비어 있다(Bin 도 item 단위라 공란)
    for r in temp_data:
        assert r["Bin"] == "", r
        assert r["WF1_CT_yield"] != "" and r["WF1_RT_yield"] == "", r
    # 불량률(집계 count) 내림차순
    counts = [sum(int(r.get(f"{s}_count") or 0) for s in ("WF1_CT", "WF1_HT"))
              for r in temp_data]
    assert counts == sorted(counts, reverse=True), counts


def test_issue_table_yield_cpk_are_rt_only():
    """Temperature 에서 Yield 섹션 Pass 행은 RT 소스 값만 갖는다 (CT/HT 는 공란)."""
    payload = build_report_payload(temp_tables(), mode="Temperature", gross_die=200,
                                   temperature_groups=GROUPS)
    rows = payload["sheets"]["Issue Table"]
    pass_row = rows[0]
    assert str(pass_row["Bin"]) == "1" and str(pass_row["Category"]) == "Yield", pass_row
    assert pass_row["WF1_RT_yield"] not in ("", None), pass_row
    assert pass_row["WF1_CT_yield"] == "" and pass_row["WF1_HT_yield"] == "", pass_row


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
               test_yield_corner_groups_split,
               test_corner_percentages_match_full_yield_sheet,
               test_member_roles_and_temp_corner,
               test_issue_table_temp_section,
               test_issue_table_yield_cpk_are_rt_only,
               test_issue_table_row_key_roundtrip,
               test_normal_mode_payload_unchanged):
        fn()
        checks += 1
    print(f"PASS: test_temperature_payload ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

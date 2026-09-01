"""AI Comment 평가 범위 = fail item ∪ Issue Table CPK 섹션 후보 (2026-09-01).

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_eval_scope_cpk.py

고정하는 계약:
  1. ``ai_comment.eval_fail_scope`` 는 fail item 에 더해 **Issue Table CPK 섹션에 행이 생기는
     item**(fail 없음 + worst Bin1 cpk<1.33, Pass/Fail unit·OTP 제외)을 포함한다 — 그래야
     CPK 행에 엔진 case(Signature/AI Comment)가 생긴다.
  2. 스코프의 CPK 쪽 절반은 Issue Table 의 행 멤버십 함수(``cpk_issue_subjects``)와 **같은
     집합**이다 — 갈리면 "평가는 됐는데 행이 없다 / 행은 있는데 미분류" 가 된다.
  3. Temperature 세션은 RT source 만으로 cpk 후보를 잰다(Issue Table CPK 섹션과 같은 테이블).
  4. ``_to_row_keys`` 는 fail bin 이 없는 case 의 발화를 ``CPK|<item>`` 키에 싣는다(Yield 키 없음).
  5. 숨긴 CPK 행의 item 이 룰 발화로 ETC 자동 행에 되살아나지 않는다(seen 은 숨김 전 목록).

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례). pytest 로 수집해도 동작한다.
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report import ai_comment  # noqa: E402
from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402
from web_report.tabs.cpk import build_cpk_rows  # noqa: E402
from web_report.tabs.issue_table import (  # noqa: E402
    build_issue_table_rows, cpk_issue_subjects)

# Bin1 값 — TIGHT 는 cpk 높음(≥1.33), WIDE 는 cpk<1.33 (LOLIM 8 / HILIM 12).
_TIGHT = [9.8, 9.9, 10.0, 10.1, 10.2] * 4
_WIDE = [8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 11.5] + [10.0] * 13
_ITEMS = ["ItemA", "ItemB", "ItemC", "OTP_X", "ItemPF"]
_TNO = {"ItemA": 100, "ItemB": 200, "ItemC": 300, "OTP_X": 400, "ItemPF": 500}


def make_table(source="src0", values=None, fail_item="ItemA"):
    """합성 honeyform 1개 — 양품 20 die + fail 1 die(fail_item 귀속).

    기본값: ItemA 는 fail 이 있고(Yield 섹션), ItemB·OTP_X 는 cpk<1.33(fail 없음),
    ItemC 는 cpk 높음, ItemPF 는 Pass/Fail unit(값은 넓게 — cpk 로는 걸리지만 제외 대상).
    """
    values = values or {"ItemA": _TIGHT, "ItemB": _WIDE, "ItemC": _TIGHT,
                        "OTP_X": _WIDE, "ItemPF": _WIDE}
    cols = META_COLUMNS + _ITEMS
    rows = [
        ["TSEQ", "", "", "", "", "", ""] + list(range(1, len(_ITEMS) + 1)),
        ["TNO", "", "", "", "", "", ""] + [_TNO[i] for i in _ITEMS],
        ["STEP", "", "", "", "", "", ""] + ["P1"] * len(_ITEMS),
        ["UNIT", "", "", "", "", "", ""] + ["V", "V", "V", "V", "P/F"],
        ["HILIM", "", "", "", "", "", ""] + [12] * len(_ITEMS),
        ["LOLIM", "", "", "", "", "", ""] + [8] * len(_ITEMS),
    ]
    for i in range(20):
        rows.append([f"p{i}", 1, 1, i, 0, 1, ""] + [values[it][i] for it in _ITEMS])
    rows.append(["f1", 1, 1, 20, 0, 5, _TNO[fail_item]] + [10.0] * len(_ITEMS))
    return split_honeyform(pd.DataFrame(rows, columns=cols), source=source, file_name=source)


def issue_cpk_items(rows):
    start = next(i for i, r in enumerate(rows) if r.get("Category") == "CPK")
    out = []
    for r in rows[start + 1:]:
        if r.get("Category") == "ETC":
            break
        if r.get("Item"):
            out.append(r["Item"])
    return out


def etc_items_of(rows):
    start = next(i for i, r in enumerate(rows) if r.get("Category") == "ETC")
    return [r["Item"] for r in rows[start + 1:] if r.get("Item")]


def test_scope_is_fail_union_cpk_section():
    tables = [make_table()]
    scope = ai_comment.eval_fail_scope(tables)
    # ItemA = fail, ItemB = cpk<1.33. ItemC(cpk 높음)·OTP_X(제외 토큰)·ItemPF(P/F unit) 는 밖.
    assert scope == {"ItemA", "ItemB"}, scope
    # session/selected 를 넘겨도(Normal 세션) 같다.
    session = {"mode": "Normal", "webreport_options": ""}
    assert ai_comment.eval_fail_scope(tables, session, set(_ITEMS)) == scope
    # selected_items 가 CPK 후보를 빼면 스코프에서도 빠진다(미선택 item 은 표에 없다).
    assert ai_comment.eval_fail_scope(tables, session, {"ItemA", "ItemC"}) == {"ItemA"}


def test_scope_cpk_half_matches_issue_table_membership():
    """스코프 − fail == Issue Table CPK 섹션의 item 집합 (같은 함수를 쓴다)."""
    tables = [make_table()]
    payload = build_report_payload(tables)
    section = issue_cpk_items(payload["sheets"]["Issue Table"])
    assert section == ["ItemB"], section
    scope = ai_comment.eval_fail_scope(tables)
    assert scope - {"ItemA"} == set(section), (scope, section)
    # 멤버십 함수 단독: 숨김·Yield 중복 제외 **전** 목록이라 fail 인 item 도 cpk 가 낮으면 든다.
    wide_a = {"ItemA": _WIDE, "ItemB": _WIDE, "ItemC": _TIGHT, "OTP_X": _WIDE, "ItemPF": _WIDE}
    t2 = make_table(values=wide_a)
    rows = build_cpk_rows([t2], _ITEMS)
    subjects = [s for s, _ in cpk_issue_subjects(rows, ["src0"])]
    assert set(subjects) == {"ItemA", "ItemB"}, subjects
    # Issue Table 에서는 ItemA 가 Yield 섹션에 있어 CPK 섹션에서 빠진다(기존 규칙 유지).
    assert issue_cpk_items(build_report_payload([t2])["sheets"]["Issue Table"]) == ["ItemB"]


def test_temperature_scope_uses_rt_only():
    """CT 에서만 cpk 가 낮은 item 은 스코프 밖 — Issue Table CPK 섹션도 RT 만 본다."""
    rt = make_table("WF1_RT", values={"ItemA": _TIGHT, "ItemB": _TIGHT, "ItemC": _TIGHT,
                                       "OTP_X": _TIGHT, "ItemPF": _TIGHT})
    ct = make_table("WF1_CT", values={"ItemA": _TIGHT, "ItemB": _WIDE, "ItemC": _WIDE,
                                       "OTP_X": _TIGHT, "ItemPF": _TIGHT})
    opts = json.dumps({"temperature": {"groups": [
        {"rt": "WF1_RT", "members": ["WF1_CT"], "member_roles": ["CT"]}]}})
    session = {"mode": "Temperature", "webreport_options": opts}
    scope = ai_comment.eval_fail_scope([rt, ct], session)
    assert scope == {"ItemA"}, scope
    # session 없이(구 호출부) 부르면 전 테이블 기준이라 CT 의 낮은 cpk 가 든다 — 대비용.
    assert ai_comment.eval_fail_scope([rt, ct]) == {"ItemA", "ItemB", "ItemC"}


def test_row_keys_fill_cpk_key_without_fail_bin():
    case = {"item_raw": "ItemB", "status": "MAJOR", "bin": None,
            "signatures": [{"id": "LOW_CPK", "role": "primary"},
                           {"id": "OUTLIER", "role": "secondary"}]}
    out = ai_comment._to_row_keys({"ItemB": case}, fail_bins={}, with_comments=False)
    sigs = out["row_signatures"]
    assert sigs.get("CPK|ItemB") == ["LOW_CPK", "OUTLIER"], sigs
    assert not [k for k in sigs if k.startswith("Yield|")], sigs
    # fail bin 행이 없으니 etc_auto 후보다 — Issue Table 이 CPK 섹션 item 으로 걸러낸다(아래).
    assert out["etc_auto_items"] == ["ItemB"], out["etc_auto_items"]


def test_hidden_cpk_row_does_not_resurface_as_etc():
    tables = [make_table()]
    payload = build_report_payload(tables)
    yield_rows = payload["sheets"]["Yield"]
    cpk_rows = build_cpk_rows(tables, _ITEMS)
    common = dict(etc_items=[], ai_comments={}, etc_auto_items=["ItemB", "ItemC"],
                  signatures={"engine": {}, "engr": {}})
    shown = build_issue_table_rows(tables, yield_rows, cpk_rows, **common)
    assert issue_cpk_items(shown) == ["ItemB"], issue_cpk_items(shown)
    # ItemC 는 cpk 정상 + 룰만 위반 → ETC 자동 행. ItemB 는 CPK 섹션에 있으니 ETC 에 없다.
    assert etc_items_of(shown) == ["ItemC"], etc_items_of(shown)
    hidden = build_issue_table_rows(tables, yield_rows, cpk_rows,
                                    hidden_keys=["CPK|ItemB"], **common)
    assert issue_cpk_items(hidden) == [], issue_cpk_items(hidden)
    assert etc_items_of(hidden) == ["ItemC"], etc_items_of(hidden)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_scope_is_fail_union_cpk_section,
               test_scope_cpk_half_matches_issue_table_membership,
               test_temperature_scope_uses_rt_only,
               test_row_keys_fill_cpk_key_without_fail_bin,
               test_hidden_cpk_row_does_not_resurface_as_etc):
        fn()
        checks += 1
    print(f"PASS: test_eval_scope_cpk ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

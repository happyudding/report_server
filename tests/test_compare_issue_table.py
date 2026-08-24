"""Issue Table Compare 탭 회귀 테스트 (2026-08-20).

실행:
    python tests/test_compare_issue_table.py

고정하는 계약:
  1. Compare 모드 payload 에만 sheets["Issue Table Compare"] 가 생긴다.
     compare 가 pending(compare_deferred + 미주입)이면 시트 키 자체가 없다.
     Compare 가 아닌 모드의 payload 는 이 시트가 없어 **종전과 동일**하다.
  2. Distribution 섹션 = dist_shift 의 focus 행만 + new_items(구분="신규") 행.
     focus 가 아닌 항목은 안 실린다. 지표는 dist_shift 값을 **그대로** 옮긴다(재계산 금지).
  3. row_key 규약 — Distribution "CMPDIST|<item>", ETC "CMPETC|<item>" (저장 키, 불변).
     comment/Status 는 그 키로 채워지고, 숨김 키가 오면 행이 빠진다.
  4. 접두 화이트리스트 — CMPDIST/CMPETC 는 comment·Status 저장이 허용되고,
     숨김은 CMPDIST 만(ETC 계열은 항목 삭제로 지운다). 종전 접두 4종은 그대로 동작한다.
  5. eval export — CMPDIST/CMPETC 코멘트는 test_condition='COMPARE' 로 파싱돼
     같은 item 의 일반 코멘트(condition='')와 **다른 case 로 갈린다**.
  6. ETC scope 분리 — scope="compare" 로 추가한 항목은 kind=cmp_etc_item 에 저장되고
     메인 Issue Table 의 etc_items 에는 나타나지 않는다(그 반대도 같다).

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례). pytest 로 수집해도 동작한다.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report import edits, service  # noqa: E402
from web_report.eval_export import _parse_row_key  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402
from web_report.tabs.compare_issue import build_compare_issue_rows  # noqa: E402

from test_compare_equivalence import _make_table, _shift, _TIGHT  # noqa: E402

SHEET = "Issue Table Compare"

# limit 은 _make_table 이 0~20 으로 박는다(LOLIM/HILIM).
#   _TIGHT  : σ 가 아주 작아 양쪽 Cpk > 100 → focus **제외**(여유 과대)
#   _LOWCPK : σ≈3.3 이라 Cpk ≈ 1.0 < 1.33 → focus **포함**(절대 품질 조건, 게이트 없음)
_LOWCPK = [5.0, 7.0, 9.0, 10.0, 11.0, 13.0, 15.0] * 5


def _pair_with_new_item():
    """Before/After 2 source — focus 1건(Cpk 낮음) + 비focus 1건 + 신규 item 1건."""
    before = {"CALM": list(_TIGHT), "LOW_CPK": list(_LOWCPK)}
    after = {"CALM": list(_TIGHT), "LOW_CPK": _shift(_LOWCPK, 1.02),
             "BRAND_NEW": list(_TIGHT)}
    return [_make_table("WF_A", after), _make_table("WF_B", before)]


def _payload(tables, **kw):
    return build_report_payload(tables, mode="Compare", **kw)


def _rows_by_item(rows):
    return {r["Item"]: r for r in rows if r.get("Item")}


def test_sheet_only_in_compare_mode():
    tables = _pair_with_new_item()
    payload = _payload(tables)
    assert SHEET in payload["sheets"], "Compare 모드인데 시트가 없다"

    normal = build_report_payload(tables, mode="Normal")
    assert SHEET not in normal["sheets"], "Compare 가 아닌 모드에 시트가 생겼다"

    pending = build_report_payload(tables, mode="Compare", compare_deferred=True)
    assert pending.get("compare_pending") is True
    assert SHEET not in pending["sheets"], "pending 인데 시트가 생겼다"
    print("  [ok] 시트는 Compare 모드 + compare 계산 완료일 때만 생긴다")


def test_distribution_rows_follow_focus():
    tables = _pair_with_new_item()
    payload = _payload(tables)
    dist_rows = payload["compare"]["dist_shift"]["rows"]
    focus_items = {r["subject"] for r in dist_rows if r["focus"]}
    assert focus_items, "합성 데이터가 focus 를 하나도 안 낸다 — 픽스처 재검토"

    rows = payload["sheets"][SHEET]
    by_item = _rows_by_item(rows)
    listed = {item for item, r in by_item.items() if r["구분"] == "산포"}
    assert listed == focus_items, f"산포 행 목록이 focus 와 다르다: {listed} != {focus_items}"

    non_focus = {r["subject"] for r in dist_rows if not r["focus"]}
    assert not (non_focus & set(by_item)), "focus 아닌 항목이 표에 실렸다"

    # 지표는 dist_shift 값 그대로 (재계산 금지 — CLAUDE.md 규칙 13)
    src = {r["subject"]: r for r in dist_rows}
    for item in focus_items:
        row, ref = by_item[item], src[item]
        assert row["stdev_delta_pct"] == ref["stdev_delta_pct"]
        assert row["meanshift_sigma"] == ref["meanshift_sigma"]
        assert row["cpk_ratio_pct"] == ref["cpk_ratio_pct"]
        assert row["before_cpk"] == ref["before"]["cpk"]
        assert row["after_cpk"] == ref["after"]["cpk"]
    print(f"  [ok] Distribution 행 = focus {len(focus_items)}건, 지표는 dist_shift 값 그대로")


def test_new_item_row():
    tables = _pair_with_new_item()
    payload = _payload(tables)
    assert payload["compare"]["new_items"] == ["BRAND_NEW"]

    by_item = _rows_by_item(payload["sheets"][SHEET])
    row = by_item.get("BRAND_NEW")
    assert row is not None, "신규 item 행이 없다"
    assert row["구분"] == "신규"
    # Before 통계·비교 지표는 존재하지 않는다(비교 대상이 없다).
    assert row["before_avg"] == "" and row["before_cpk"] == ""
    assert row["meanshift_sigma"] == "" and row["stdev_delta_pct"] == ""
    # After 통계는 cpk_rows 재사용으로 채워진다.
    assert row["after_cpk"] is not None, "신규 item 의 After cpk 가 비었다"
    print("  [ok] 신규 item 행 — Before/지표 빈칸 + After 통계는 cpk_rows 재사용")


def test_row_keys_comment_status_hidden():
    tables = _pair_with_new_item()
    payload = _payload(tables)
    focus_item = next(r["subject"] for r in payload["compare"]["dist_shift"]["rows"]
                      if r["focus"])

    comments = {f"CMPDIST|{focus_item}": {"PTE comment": "산포 커짐"},
                "CMPETC|자유항목": {"개발 comment": "확인 필요"}}
    statuses = {f"CMPDIST|{focus_item}": "Close"}
    rows = build_compare_issue_rows(
        payload["compare"], tables=tables, cpk_rows=None,
        cmp_etc_items=["자유항목"], issue_comments=comments, statuses=statuses)
    by_item = _rows_by_item(rows)
    assert by_item[focus_item]["PTE comment"] == "산포 커짐"
    assert by_item[focus_item]["Status"] == "Close"
    assert by_item["자유항목"]["개발 comment"] == "확인 필요"
    assert by_item["자유항목"]["Status"] == "Open", "Status 부재는 Open"

    hidden = build_compare_issue_rows(
        payload["compare"], tables=tables,
        hidden_keys=[f"CMPDIST|{focus_item}"])
    assert focus_item not in _rows_by_item(hidden), "숨긴 행이 그대로 나온다"
    print("  [ok] CMPDIST|/CMPETC| 키로 comment·Status·숨김이 붙는다")


def test_key_prefix_whitelist():
    # comment/Status 는 6종 전부 허용
    for prefix in ("Yield|", "CPK|", "TEMP|", "ETC|", "CMPDIST|", "CMPETC|"):
        assert (prefix + "Item").startswith(service._ISSUE_KEY_PREFIXES)
    # 숨김은 ETC 계열 제외
    assert "CMPDIST|Item".startswith(service._ISSUE_HIDABLE_PREFIXES)
    assert not "CMPETC|Item".startswith(service._ISSUE_HIDABLE_PREFIXES)
    assert not "ETC|Item".startswith(service._ISSUE_HIDABLE_PREFIXES)
    # 종전 3종은 그대로 숨김 가능 — [:3] 슬라이스를 이름 튜플로 바꾼 뒤에도 불변
    for prefix in ("Yield|", "CPK|", "TEMP|"):
        assert (prefix + "Item").startswith(service._ISSUE_HIDABLE_PREFIXES)
    print("  [ok] 접두 화이트리스트 — comment/Status 6종, 숨김은 ETC 계열 제외")


def test_eval_export_condition():
    assert _parse_row_key("CMPDIST|ItemA") == (None, "ItemA", "COMPARE")
    assert _parse_row_key("CMPETC|ItemB") == (None, "ItemB", "COMPARE")
    # 같은 item 이라도 조건이 달라 case 가 갈린다 (일반 코멘트를 덮지 않는다)
    assert _parse_row_key("CPK|ItemA") == (None, "ItemA", "")
    assert _parse_row_key("TEMP|ItemA") == (None, "ItemA", "TEMP")
    # 접두만 있고 item 이 없으면 대상 아님
    assert _parse_row_key("CMPDIST|") is None
    assert _parse_row_key("CMPETC|") is None
    print("  [ok] eval export — CMPDIST/CMPETC → test_condition 'COMPARE'")


def test_etc_scope_separation():
    """scope 별 kind 분리 — 같은 세션에서 두 목록이 섞이지 않는다."""
    assert edits.KIND_CMP_ETC_ITEM != edits.KIND_ETC_ITEM

    class _DB:
        """load_edit_state 가 요구하는 최소 인터페이스."""
        def __init__(self, rows):
            self._rows = rows

        def get_webreport_edits(self, session_id, kinds=None, exclude_kinds=None):
            out = []
            for kind, item_key, value in self._rows:
                if kinds and kind not in kinds:
                    continue
                if exclude_kinds and kind in exclude_kinds:
                    continue
                out.append({"kind": kind, "item_key": item_key, "value": value})
            return out

    db = _DB([(edits.KIND_ETC_ITEM, "메인항목", ""),
              (edits.KIND_CMP_ETC_ITEM, "컴페어항목", "")])
    state = edits.load_edit_state(db, "sid")
    assert state["etc_items"] == ["메인항목"]
    assert state["cmp_etc_items"] == ["컴페어항목"]
    # manifest 폴백(legacy 세션)에도 키가 있어야 콜드 빌드에서 KeyError 가 안 난다
    assert edits.state_from_manifest({})["cmp_etc_items"] == []
    print("  [ok] ETC scope 분리 — etc_item / cmp_etc_item 목록이 안 섞인다")


def main():
    tests = [
        test_sheet_only_in_compare_mode,
        test_distribution_rows_follow_focus,
        test_new_item_row,
        test_row_keys_comment_status_hidden,
        test_key_prefix_whitelist,
        test_eval_export_condition,
        test_etc_scope_separation,
    ]
    for fn in tests:
        print(f"[{fn.__name__}]")
        fn()
    print(f"\n전체 {len(tests)}개 통과")


if __name__ == "__main__":
    main()

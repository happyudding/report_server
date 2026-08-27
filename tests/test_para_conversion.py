"""Para Conversion Compare 회귀 테스트 (2026-08-27).

실행:
    python tests/test_para_conversion.py

Para Conversion 은 Compare 의 변형이다 — Before=Single Mass Data 1개,
After=같은 웨이퍼를 DUT 로 펼친 N개(클라이언트가 업로드 전에 분할). 세션 mode 는 계속
"Compare" 이고 구분은 ``options.compare.para`` 플래그 하나다.

고정하는 계약:
  1. split_honeyform_df_by_dut — 라벨 순서(수치 오름차순)·행 전량 보존·메타 6행 복사.
     DUT 종류가 1개 이하면 빈 목록(분할 불가).
  2. goodlog Value — para 는 각 source 의 **첫 데이터 행** 값이며 After 쪽은 DUT 별
     한 칸씩(row["after_values"], "para_duts" 순서). Single 도 같은 기준.
     after_value/gap 은 첫 DUT 기준으로 계속 채워 Excel·구 렌더가 그대로 동작한다.
  3. **비para 경로 불변** — para 인자 없이 부른 build_goodlog / build_compare_payload /
     build_map_analysis_rows 의 산출이 종전과 정준 JSON 으로 동일하다(회귀 방지의 핵심).
  4. common_map / bin_matrix 는 para 일 때만 [All DUT, Single] 2-source. DUT 끼리는
     좌표가 서로소라 전-source 공통 좌표가 공집합이 되기 때문이다.
  5. Map Analysis — para_after 로 지정한 source 만 'All DUT' 로 병합하고 Single 은 그대로.
     die 총량 보존. 경량 메타(include_dies=False)는 strip_dies(전량)와 정준 JSON 일치
     (규칙 11 — 시딩 산출 == 조회 산출).
  6. map_key 는 para 세션에만 마커가 붙고 그 외 세션 키는 바이트 불변(콜드 폭풍 회피).

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례). pytest 로 수집해도 동작한다.
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report import cache_policy  # noqa: E402
from web_report.honeyform import (META_COLUMNS, split_honeyform,  # noqa: E402
                                  split_honeyform_df_by_dut)
from web_report.tabs.Map_analysis import (DUT_POOL_LABEL,  # noqa: E402
                                          build_map_analysis_rows, strip_dies)
from web_report.tabs.common import fmt_type  # noqa: E402
from web_report.tabs.compare import build_compare_payload, build_goodlog  # noqa: E402
from web_report.validation import (webreport_compare_groups,  # noqa: E402
                                   webreport_compare_para)

_VALS = [5.0, 7.0, 9.0, 10.0, 11.0, 13.0, 15.0, 8.0, 12.0, 6.0]


def _frame(items, *, duts, source="SRC"):
    """합성 honeyform df — duts 는 데이터 행마다의 DUT 값(길이 = 데이터 행 수)."""
    cols = META_COLUMNS + list(items)
    n_it = len(items)
    rows = [
        ["TSEQ", "", "", "", "", "", ""] + [i + 1 for i in range(n_it)],
        ["TNO", "", "", "", "", "", ""] + [(i + 1) * 100 for i in range(n_it)],
        ["STEP", "", "", "", "", "", ""] + ["P2"] * n_it,
        ["UNIT", "", "", "", "", "", ""] + ["V"] * n_it,
        ["HILIM", "", "", "", "", "", ""] + [20] * n_it,
        ["LOLIM", "", "", "", "", "", ""] + [0] * n_it,
    ]
    for i, dut in enumerate(duts):
        rows.append([f"{source}_p{i}", 1, dut, i + 1, 1, 1, ""]
                    + [items[c][i] for c in items])
    return pd.DataFrame(rows, columns=cols)


def _table(df, source):
    return split_honeyform(df, source=source, file_name=f"{source}.csv")


def _single_and_duts():
    """Single 1개 + DUT1/DUT2 2개 (Para 업로드 후의 서버측 tables 구성)."""
    items = {"ITEM_A": list(_VALS), "ITEM_B": [v + 1 for v in _VALS]}
    single = _table(_frame(items, duts=[1] * len(_VALS), source="SGL"), "Single")
    para_df = _frame(items, duts=[1, 2] * (len(_VALS) // 2), source="PARA")
    duts = [_table(sub, f"DUT{label}")
            for label, sub in split_honeyform_df_by_dut(para_df)]
    # 업로드 순서 = [After…, Before] (Compare 관례 — tables[0] 이 limit 기준)
    return duts + [single], duts, single


def _groups(duts, para=True):
    g = {"before": ["Single"], "after": [t.source for t in duts]}
    if para:
        g["para"] = True
    return g


def _canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


# ── 1. 분할 헬퍼 ──────────────────────────────────────────────────────────────

def test_split_df_by_dut():
    items = {"ITEM_A": list(_VALS)}
    df = _frame(items, duts=[2, 1, 10, 1, 2, 1, 10, 2, 1, 2])
    out = split_honeyform_df_by_dut(df)

    assert [label for label, _ in out] == ["1", "2", "10"], "수치 오름차순 라벨"
    # 행 전량 보존 + 메타 6행이 각 분할본에 복사된다.
    total = sum(len(sub) - 6 for _, sub in out)
    assert total == len(_VALS), f"행 보존 실패: {total} != {len(_VALS)}"
    for label, sub in out:
        assert list(sub.columns) == list(df.columns)
        assert sub.iloc[0, 0] == "TSEQ" and sub.iloc[5, 0] == "LOLIM", "메타 6행 복사"
        assert {str(v) for v in sub.iloc[6:]["DUT"]} == {label}, "한 DUT 만"

    # DUT 종류 1개 이하 → 분할 불가(빈 목록).
    assert split_honeyform_df_by_dut(_frame(items, duts=[1] * len(_VALS))) == []
    print("OK  test_split_df_by_dut")


# ── 2. goodlog Value ─────────────────────────────────────────────────────────

def test_goodlog_para_values():
    tables, duts, single = _single_and_duts()
    gl = build_goodlog(duts[0], single, para_after_tables=duts)

    assert gl["para_duts"] == ["DUT1", "DUT2"]
    row = next(r for r in gl["rows"] if r["after_item_name"] == "ITEM_A")
    assert len(row["after_values"]) == 2, "DUT 수만큼 Value 칸"

    # 각 DUT 의 **첫 데이터 행** 값. DUT1 = 원본 행0, DUT2 = 원본 행1.
    assert row["after_values"][0] == fmt_type(_VALS[0]), row["after_values"]
    assert row["after_values"][1] == fmt_type(_VALS[1]), row["after_values"]
    # Single 도 첫 데이터 행 기준(공통 좌표/Bin1 reference 가 아니다).
    assert row["before_value"] == fmt_type(_VALS[0])
    # 기존 키는 첫 DUT 기준으로 계속 채워진다(Excel·구 렌더 호환).
    assert row["after_value"] == row["after_values"][0]

    # after 가 없는 행(before 에만 있는 항목)도 키가 있어야 렌더가 어긋나지 않는다.
    only_before = build_goodlog(
        _table(_frame({"ONLY_A": list(_VALS)}, duts=[1, 2] * 5), "DUT1"),
        _table(_frame({"ONLY_B": list(_VALS)}, duts=[1] * 10), "Single"),
        para_after_tables=duts)
    for r in only_before["rows"]:
        assert isinstance(r.get("after_values"), list), r
    print("OK  test_goodlog_para_values")


def test_goodlog_normal_unchanged():
    """para 인자 없이 부르면 종전과 완전히 같다 — 키가 늘지도 않는다."""
    _tables, duts, single = _single_and_duts()
    gl = build_goodlog(duts[0], single)
    assert "para_duts" not in gl
    for r in gl["rows"]:
        assert "after_values" not in r, "비para 행에 새 키가 생기면 안 된다"
    print("OK  test_goodlog_normal_unchanged")


# ── 3. compare payload (common_map / bin_matrix 2-source) ────────────────────

def test_compare_payload_para_two_source_maps():
    tables, duts, _single = _single_and_duts()
    items = ["ITEM_A", "ITEM_B"]
    pay = build_compare_payload(tables, items, [], compare_groups=_groups(duts))

    assert pay["para"] == {"duts": ["DUT1", "DUT2"]}
    # 일반 탭이 쓰는 최상위 source 목록은 전 source 그대로.
    assert pay["sources"] == ["DUT1", "DUT2", "Single"]

    cm = pay["common_map"]
    assert cm["sources"] == [DUT_POOL_LABEL, "Single"], cm["sources"]
    assert cm["counts"]["common_dies"] > 0, "DUT 합본이면 Single 과 좌표가 겹쳐야 한다"
    assert cm["groups"] == {DUT_POOL_LABEL: "after", "Single": "before"}

    bm = pay["bin_matrix"]
    assert bm["sources"] == [DUT_POOL_LABEL, "Single"], bm["sources"]
    assert bm["counts"]["common_dies"] > 0

    # goodlog 은 para 경로를 탔다.
    assert pay["goodlog"]["para_duts"] == ["DUT1", "DUT2"]
    print("OK  test_compare_payload_para_two_source_maps")


def test_compare_payload_normal_unchanged():
    """para 플래그가 없으면 전 source 공통 좌표 기준 — 종전 동작."""
    tables, duts, _single = _single_and_duts()
    items = ["ITEM_A", "ITEM_B"]
    pay = build_compare_payload(tables, items, [],
                                compare_groups=_groups(duts, para=False))
    assert "para" not in pay
    assert pay["common_map"]["sources"] == ["DUT1", "DUT2", "Single"]
    assert pay["bin_matrix"]["sources"] == ["DUT1", "DUT2", "Single"]
    assert "para_duts" not in pay["goodlog"]
    print("OK  test_compare_payload_normal_unchanged")


# ── 4. Map Analysis 부분 병합 ────────────────────────────────────────────────

def test_map_para_merges_only_duts():
    tables, duts, _single = _single_and_duts()
    names = [t.source for t in duts]
    rows = build_map_analysis_rows(tables, "", "", "Compare", para_after=names)

    assert [r["source"] for r in rows] == [DUT_POOL_LABEL, "Single"], \
        [r["source"] for r in rows]
    merged = rows[0]
    assert merged["duts"] == ["1", "2"], merged.get("duts")
    # die 총량 보존 — 병합이 die 를 버리지 않는다(규칙 #6).
    assert len(merged["dies"]) == len(_VALS)
    assert {d.get("dut") for d in merged["dies"]} == {"1", "2"}
    # Single 은 병합 대상이 아니라 dut 태그가 없다.
    assert all("dut" not in d for d in rows[1]["dies"])

    # 경량 메타 == strip_dies(전량) — 규칙 11 정준 JSON 일치.
    light = build_map_analysis_rows(tables, "", "", "Compare",
                                    include_dies=False, para_after=names)
    assert _canon(light) == _canon(strip_dies(rows))
    print("OK  test_map_para_merges_only_duts")


def test_map_normal_unchanged():
    """para_after 없이 부르면 source 별 맵 그대로 — 병합도 dut 태그도 없다."""
    tables, _duts, _single = _single_and_duts()
    rows = build_map_analysis_rows(tables, "", "", "Compare")
    assert [r["source"] for r in rows] == ["DUT1", "DUT2", "Single"]
    for r in rows:
        assert "duts" not in r and "_dut" not in r
        assert all("dut" not in d for d in r["dies"])
    print("OK  test_map_normal_unchanged")


# ── 5. 옵션 전파 + 캐시 키 ───────────────────────────────────────────────────

def test_options_and_cache_key():
    names = ["DUT1", "DUT2", "Single"]
    para_opts = json.dumps({"compare": {"before": ["Single"], "after": ["DUT1", "DUT2"],
                                        "para": True}})
    norm_opts = json.dumps({"compare": {"before": ["Single"], "after": ["DUT1"]}})

    assert webreport_compare_groups(para_opts, names)["para"] is True
    assert "para" not in webreport_compare_groups(norm_opts, names)
    assert webreport_compare_para(para_opts) is True
    assert webreport_compare_para(norm_opts) is False
    assert webreport_compare_para("") is False
    assert webreport_compare_para("not json") is False

    base = {"analysis_key": "AK", "content_hash": "CH", "mode": "Compare"}
    para_key = cache_policy.map_key(dict(base, webreport_options=para_opts))
    norm_key = cache_policy.map_key(dict(base, webreport_options=norm_opts))
    assert para_key != norm_key, "para 세션은 map 캐시가 갈려야 한다"
    assert para_key[:-1] == norm_key, "비para 키는 종전 그대로(마커만 덧붙는다)"
    print("OK  test_options_and_cache_key")


if __name__ == "__main__":
    test_split_df_by_dut()
    test_goodlog_para_values()
    test_goodlog_normal_unchanged()
    test_compare_payload_para_two_source_maps()
    test_compare_payload_normal_unchanged()
    test_map_para_merges_only_duts()
    test_map_normal_unchanged()
    test_options_and_cache_key()
    print("\nALL PASS  tests/test_para_conversion.py")

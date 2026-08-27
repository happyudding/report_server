"""Para Conversion 클라이언트 분할 배선 회귀 (2026-08-27).

실행:
    python tests/test_para_client_split.py

Para 는 업로드 **전에 클라이언트가** Para 파일을 DUT 별로 나눠 Single + DUT<라벨> N개
source 로 만든다(honey_main._prepare_para_conversion → _build_webreport_parquets).
Qt 없이 그 두 단계의 순수 로직만 재현해 다음을 고정한다:

  1. 이름 — Single 쪽은 "Single", Para 쪽은 "DUT<실제 라벨>"(공백 없음, 수치 오름차순).
     기존 DUT 모드의 "DUT <값>"(공백 있음)과 표기가 다르다.
  2. options.compare = {before:["Single"], after:[DUT…], para:True} 이고
     업로드 순서(source_order)는 After 먼저 = [DUT…, "Single"] (tables[0] limit 기준 관례).
  3. rename_sources 후에도 Para 원본 이름으로 mass_data_map 조회가 되어야 한다
     (분할본이 그 md 의 메타 = 파일 정보를 쓴다).
  4. 분할본 parquet 이 서버 왕복(encode→decode)에서 DUT 별로 갈려 나오고 행 전량이
     보존된다 — 서버가 이 source 들을 일반 Compare source 로 그대로 소비한다.

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "client"))

from report_generator.df_honey import df_honey  # noqa: E402
from report_generator.df_honey_group import df_honey_group  # noqa: E402
from web_report.honeyform import (META_COLUMNS, decode_honeyform_parquet,  # noqa: E402
                                  encode_honeyform_parquet,
                                  split_honeyform_df_by_dut)

# DUT 값 3종(1/2/10) — 수치 오름차순 정렬이 문자순(1,10,2)과 갈리는 조합을 일부러 쓴다.
_DUTS = [1, 2, 10, 1, 2, 10, 1, 2, 10]


def _df(dut_values, *, items=("ITEM_A", "ITEM_B")):
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
    for i, dut in enumerate(dut_values):
        rows.append([f"s{i}", 1, dut, i + 1, 1, 1, ""] + [float(i + j) for j in range(n_it)])
    return pd.DataFrame(rows, columns=cols)


def _group(para_duts=_DUTS):
    single = df_honey(_df([1] * len(para_duts)), name="single_file")
    para = df_honey(_df(para_duts), name="para_file")
    return df_honey_group([single, para])


def _prepare(group, arranged):
    """honey_main._prepare_para_conversion 의 순수 부분 재현 (Qt·안내창 제외).

    실제 코드와 **같은 순서·같은 함수**를 써야 회귀를 잡는다: 분할 → rename → 이름 조립.
    """
    para_src, single_src = arranged["after"][0], arranged["before"][0]
    md = group.mass_data_map[para_src]
    df = md.to_df() if hasattr(md, "to_df") else md.df
    parts = split_honeyform_df_by_dut(df)
    if not parts:
        return None
    group.rename_sources(["Single" if n == single_src else n for n in group.names()])
    dut_names = [f"DUT{label}" for label, _ in parts]
    options = {}
    options["compare"] = {"before": ["Single"], "after": dut_names, "para": True}
    return {
        "options": options,
        "source_order": dut_names + ["Single"],
        "para": {"alias": {n: para_src for n in dut_names},
                 "frames": dict(zip(dut_names, (sub for _, sub in parts)))},
    }


def _build(group, prepared):
    """_build_webreport_parquets 의 para 부분 재현 — 이름별 (md, df) 해석 + 인코딩."""
    alias = prepared["para"]["alias"]
    frames = prepared["para"]["frames"]
    out = []
    for idx, name in enumerate(prepared["source_order"]):
        md = group.mass_data_map[alias.get(name, name)]
        df = frames.get(name)
        if df is None:
            df = md.to_df() if hasattr(md, "to_df") else md.df
        out.append({"index": idx, "name": name, "md_name": md.name,
                    "data": encode_honeyform_parquet(df)})
    return out


def test_names_and_options():
    """(1)(2) 이름·옵션·업로드 순서."""
    group = _group()
    arranged = {"after": ["para_file"], "before": ["single_file"], "para": True}
    prepared = _prepare(group, arranged)
    assert prepared is not None

    cmp_opt = prepared["options"]["compare"]
    assert cmp_opt["after"] == ["DUT1", "DUT2", "DUT10"], cmp_opt["after"]
    assert cmp_opt["before"] == ["Single"]
    assert cmp_opt["para"] is True
    # 업로드 순서 = After 먼저 (서버 tables[0] 이 limit 기준이라는 Compare 관례).
    assert prepared["source_order"] == ["DUT1", "DUT2", "DUT10", "Single"]
    # 기존 DUT 모드('DUT 1')와 표기가 다르다 — 공백 없음.
    assert all(" " not in n for n in cmp_opt["after"]), cmp_opt["after"]
    print("OK  test_names_and_options")


def test_rename_keeps_para_lookup():
    """(3) rename 후에도 Para 원본 이름으로 md 를 찾을 수 있어야 한다."""
    group = _group()
    prepared = _prepare(group, {"after": ["para_file"], "before": ["single_file"],
                                "para": True})
    assert sorted(group.names()) == ["Single", "para_file"], list(group.names())
    built = _build(group, prepared)
    by_name = {b["name"]: b for b in built}
    # DUT 분할본은 전부 원본 Para source 의 md 를 메타로 쓴다.
    assert {by_name[n]["md_name"] for n in ("DUT1", "DUT2", "DUT10")} == {"para_file"}
    assert by_name["Single"]["md_name"] == "Single"
    print("OK  test_rename_keeps_para_lookup")


def test_encoded_parquets_split_correctly():
    """(4) 서버가 받을 parquet 이 DUT 별로 갈리고 행 전량이 보존된다."""
    group = _group()
    prepared = _prepare(group, {"after": ["para_file"], "before": ["single_file"],
                                "para": True})
    built = _build(group, prepared)
    assert [b["name"] for b in built] == ["DUT1", "DUT2", "DUT10", "Single"]

    total = 0
    for b in built:
        df = decode_honeyform_parquet(b["data"])
        data = df.iloc[6:]
        if b["name"] == "Single":
            continue
        labels = {str(v).split(".")[0] for v in data["DUT"]}
        assert labels == {b["name"][3:]}, f'{b["name"]} -> {labels}'
        total += len(data)
    assert total == len(_DUTS), f"행 보존 실패: {total} != {len(_DUTS)}"
    print("OK  test_encoded_parquets_split_correctly")


def test_single_dut_blocked():
    """DUT 종류가 1개면 분할 불가 → 호출부가 중단(안내)한다."""
    group = _group(para_duts=[1] * 6)
    assert _prepare(group, {"after": ["para_file"], "before": ["single_file"],
                            "para": True}) is None
    print("OK  test_single_dut_blocked")


if __name__ == "__main__":
    test_names_and_options()
    test_rename_keeps_para_lookup()
    test_encoded_parquets_split_correctly()
    test_single_dut_blocked()
    print("\nALL PASS  tests/test_para_client_split.py")

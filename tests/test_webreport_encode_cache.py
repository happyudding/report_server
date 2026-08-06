"""web_report 업로드 선행 parquet 인코딩(배치창 시간 활용) 회귀 테스트.

실행:
    python tests/test_webreport_encode_cache.py
    (PyQt6 가 있는 파이썬으로 — honey_main 이 모듈 레벨에서 import 한다. server/.venv 에는
     PyQt6 가 없으므로 전역 python 을 쓴다. tests/test_temperature_pairing.py 와 같은 조건.)

배경: source 이름/배치 다이얼로그가 떠 있는 동안 parquet 인코딩을 미리 돌린다. 인코딩
결과는 source 이름·순서와 무관하므로(honeyform 에 이름 컬럼이 없다) 배치를 어떻게 바꿔도
그대로 재사용된다 — 그것이 이 최적화가 성립하는 근거다.

고정하는 계약:
  - **캐시 유/무 두 경로의 산출물이 bytes 까지 완전히 같다** (핵심 회귀 게이트)
  - 선행 인코딩 바이트 == 정식 경로(dedupe → encode) 바이트
  - Temperature 정리본(cleaned)은 raw 캐시를 오적중하지 않는다 — 풀이 분리돼 있다
  - _temp_invalid_members 는 **원본 index** 로 비교한다: 표시 순서·그룹 번호·CT↔HT 역할
    스왑·역할 접미사 개명은 재계산 대상이 아니고, RT 파트너 교체와 limit 파일 유입만이다

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "client"))
sys.path.insert(0, _ROOT)

import honey_main  # noqa: E402
from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, dedupe_item_columns, encode_honeyform_parquet)


# ── 픽스처 ──────────────────────────────────────────────────────────────────
def make_df(items, base=0.0):
    """items 이름 목록으로 최소 honeyform 프레임 (메타 6행 + 데이터 2행)."""
    n = len(items)
    pad = [""] * 6                      # SHOT..FAILTNO
    rows = [
        ["TSEQ"] + pad + list(range(1, n + 1)),
        ["TNO"] + pad + [100 + i for i in range(n)],
        ["STEP"] + pad + ["P1"] * n,
        ["UNIT"] + pad + ["V"] * n,
        ["HILIM"] + pad + [10] * n,
        ["LOLIM"] + pad + [0] * n,
        ["s1", 1, 1, 0, 0, 1, ""] + [base + 1.0 + i for i in range(n)],
        ["s2", 1, 1, 1, 0, 1, ""] + [base + 2.0 + i for i in range(n)],
    ]
    return pd.DataFrame(rows, columns=list(META_COLUMNS) + list(items))


class FakeMd:
    """df_honey 대역 — 선행 인코딩이 읽는 것은 df 하나뿐이다."""

    def __init__(self, name, df):
        self.name = name
        self.df = df


class FakeGroup:
    def __init__(self, mds):
        self._map = {md.name: md for md in mds}

    @property
    def mass_data_map(self):
        return self._map

    def names(self):
        return list(self._map.keys())


class FakeWin:
    """HoneyMainWindow 대역 — _build_webreport_parquets 를 QApplication 없이 돌린다."""

    _build_webreport_parquets = honey_main.HoneyMainWindow._build_webreport_parquets

    def __init__(self, cleaned=None):
        self._cleaned = cleaned or {}

    def _source_file_name(self, md, fallback):
        return f"{fallback}.csv"

    def _clean_temperature_frames(self, work_group, names, temperature):
        # 실제 정리 규칙은 web_report/temperature.py 소관 — 여기서는 "어떤 source 가
        # 정리됐는가" 만 흉내 내 캐시 풀 선택을 검사한다.
        self._temperature_clean_log = []
        return dict(self._cleaned) if temperature else {}


def build(win, group, order=None, temperature=None, cache=None):
    return win._build_webreport_parquets(group, order, temperature, cache=cache)


# ── 선행 인코딩 ─────────────────────────────────────────────────────────────
def test_prefetch_bytes_match_direct_encode():
    """선행 인코딩 바이트 == 정식 경로(dedupe → encode) 바이트."""
    df = make_df(["A", "B", "A"])          # 중복 항목명 포함
    md = FakeMd("s1", df)
    out = honey_main._encode_sources_worker([(md, df)])

    frame, renames = dedupe_item_columns(df)
    assert out[id(md)][0] == encode_honeyform_parquet(frame)
    assert out[id(md)][1] == renames
    assert renames, "중복 항목명 픽스처인데 개명이 없다"


def test_prefetch_skips_failing_source():
    """인코딩 실패 source 는 캐시에 넣지 않는다 — 조립이 다시 시도해 예외를 보여준다."""
    bad = pd.DataFrame([[1, 2]], columns=["X", "Y"])        # honeyform 아님
    md_bad, md_ok = FakeMd("bad", bad), FakeMd("ok", make_df(["A"]))
    out = honey_main._encode_sources_worker([(md_bad, bad), (md_ok, md_ok.df)])
    assert id(md_bad) not in out
    assert id(md_ok) in out


def test_prefetch_stops_on_cancel():
    """취소 플래그가 서 있으면 아무것도 만들지 않는다 (부분 캐시도 정상)."""
    import threading
    md = FakeMd("s1", make_df(["A"]))
    evt = threading.Event()
    evt.set()
    assert honey_main._encode_sources_worker([(md, md.df)], evt) == {}


# ── _EncodePrefetch ─────────────────────────────────────────────────────────
def _finished(value=None, exc=None):
    import concurrent.futures
    fut = concurrent.futures.Future()
    if exc is not None:
        fut.set_exception(exc)
    else:
        fut.set_result(value)
    return fut


def _prefetch_with(fut):
    import concurrent.futures
    import threading
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    return honey_main._EncodePrefetch(ex, fut, threading.Event(), None)


def test_prefetch_start_returns_two_pools():
    """start → cache_or_empty 가 raw/cleaned 두 풀 형태를 돌려준다."""
    mds = [FakeMd("s1", make_df(["A"])), FakeMd("s2", make_df(["A"], base=5))]
    pf = honey_main._EncodePrefetch.start([(md, md.df) for md in mds])
    try:
        cache = pf.cache_or_empty()
        assert set(cache) == {"raw", "cleaned"}
        assert len(cache["raw"]) == 2 and cache["cleaned"] == {}
    finally:
        pf.abort()
    assert honey_main._EncodePrefetch.start([]) is None


def test_drop_cleaned_touches_only_cleaned_pool():
    """무효 판정은 정리분만 버린다 — raw 는 배치와 무관하므로 살아남는다."""
    pf = _prefetch_with(_finished({"raw": {1: ("R", [])},
                                   "cleaned": {1: ("C", []), 2: ("D", [])}}))
    try:
        pf.drop_cleaned({1})
        cache = pf.cache_or_empty()
        assert cache["cleaned"] == {2: ("D", [])}
        assert cache["raw"] == {1: ("R", [])}
    finally:
        pf.abort()


def test_cache_or_empty_swallows_worker_failure():
    """선행 job 이 터져도 빈 캐시로 수렴한다 — 조립이 종전대로 전량 인코딩한다."""
    pf = _prefetch_with(_finished(exc=RuntimeError("boom")))
    try:
        assert pf.cache_or_empty() == {}
    finally:
        pf.abort()


def test_abort_is_idempotent():
    """on_web_report 의 finally 방어와 _run_web_report 정리가 겹쳐도 안전해야 한다."""
    pf = _prefetch_with(_finished({"raw": {}, "cleaned": {}}))
    pf.abort()
    pf.abort()


# ── 조립: 캐시 유/무 동일성 (핵심 회귀 게이트) ──────────────────────────────
def test_cached_and_uncached_outputs_are_identical():
    """캐시를 써도 (sources, items) 가 bytes 까지 완전히 같다."""
    mds = [FakeMd("s1", make_df(["A", "B"])), FakeMd("s2", make_df(["A", "B"], base=5))]
    group = FakeGroup(mds)
    cache = {"raw": honey_main._encode_sources_worker([(md, md.df) for md in mds]),
             "cleaned": {}}

    plain = build(FakeWin(), group)
    cached = build(FakeWin(), group, cache=cache)
    assert plain == cached
    assert len(cached[1]) == 2 and all(it["data"] for it in cached[1])


def test_cache_survives_reorder():
    """순서를 바꿔도 캐시가 그대로 적중한다 — 이름·순서는 메타에만 쓰인다."""
    mds = [FakeMd("s1", make_df(["A"])), FakeMd("s2", make_df(["A"], base=5))]
    group = FakeGroup(mds)
    cache = {"raw": honey_main._encode_sources_worker([(md, md.df) for md in mds]),
             "cleaned": {}}

    order = ["s2", "s1"]
    plain = build(FakeWin(), group, order=order)
    cached = build(FakeWin(), group, order=order, cache=cache)
    assert plain == cached
    assert [s["name"] for s in cached[0]] == order
    assert [s["index"] for s in cached[0]] == [0, 1]


def test_partial_cache_falls_back_per_source():
    """일부만 캐시돼 있어도 나머지는 그 자리에서 인코딩한다."""
    mds = [FakeMd("s1", make_df(["A"])), FakeMd("s2", make_df(["A"], base=5))]
    group = FakeGroup(mds)
    cache = {"raw": honey_main._encode_sources_worker([(mds[0], mds[0].df)]),
             "cleaned": {}}
    assert build(FakeWin(), group) == build(FakeWin(), group, cache=cache)


def test_empty_and_broken_cache_are_noop():
    """빈 캐시·None 은 종전 경로 그대로 (폴백이 조용히 성립해야 한다)."""
    group = FakeGroup([FakeMd("s1", make_df(["A"]))])
    plain = build(FakeWin(), group)
    assert plain == build(FakeWin(), group, cache={})
    assert plain == build(FakeWin(), group, cache={"raw": {}, "cleaned": {}})


# ── Temperature: raw / cleaned 풀 분리 ──────────────────────────────────────
def test_cleaned_source_never_uses_raw_cache():
    """정리된 source 는 raw 캐시를 오적중하지 않는다 — 값이 통째로 달라진다."""
    raw_df = make_df(["A"])
    clean_df = make_df(["A"], base=99)              # 정리로 값이 바뀐 프레임
    md = FakeMd("CT", raw_df)
    group = FakeGroup([md])
    cache = {"raw": honey_main._encode_sources_worker([(md, raw_df)]), "cleaned": {}}

    win = FakeWin(cleaned={"CT": clean_df})
    _srcs, items = build(win, group, temperature={"groups": [{"rt": "RT"}]}, cache=cache)
    assert items[0]["data"] == encode_honeyform_parquet(dedupe_item_columns(clean_df)[0])
    assert items[0]["data"] != cache["raw"][id(md)][0]


def test_cleaned_pool_hits():
    """정리본 선행 인코딩은 cleaned 풀에서 적중한다."""
    raw_df = make_df(["A"])
    clean_df = make_df(["A"], base=99)
    md = FakeMd("CT", raw_df)
    group = FakeGroup([md])
    cache = {"raw": {}, "cleaned": honey_main._encode_sources_worker([(md, clean_df)])}

    win = FakeWin(cleaned={"CT": clean_df})
    temp = {"groups": [{"rt": "RT"}]}
    assert (build(win, group, temperature=temp, cache=cache)
            == build(FakeWin(cleaned={"CT": clean_df}), group, temperature=temp))


# ── Temperature 배치 무효 판정 ──────────────────────────────────────────────
GUESS_NAMES = ["A_RT", "A_CT", "A_HT", "B_RT", "B_CT"]
GUESS_GROUPS = [
    {"rt": "A_RT", "members": ["A_CT", "A_HT"], "member_roles": ["CT", "HT"]},
    {"rt": "B_RT", "members": ["B_CT"], "member_roles": ["CT"]},
]
# 자동 배치가 legend 에 역할 접미사를 붙이므로 최종 이름은 추정 이름과 항상 다르다.
FINAL_NAMES = ["A_RT(RT)", "A_CT(CT)", "A_HT(HT)", "B_RT(RT)", "B_CT(CT)"]


def arranged(groups, bin_map=None):
    return {"names": list(FINAL_NAMES), "groups": groups, "bin_map": bin_map}


def invalid(groups, bin_map=None):
    return honey_main._temp_invalid_members(GUESS_GROUPS, GUESS_NAMES,
                                            arranged(groups, bin_map))


SAME = [{"rt": "A_RT(RT)", "members": ["A_CT(CT)", "A_HT(HT)"], "member_roles": ["CT", "HT"]},
        {"rt": "B_RT(RT)", "members": ["B_CT(CT)"], "member_roles": ["CT"]}]


def test_rename_alone_is_not_a_change():
    """역할 접미사로 이름이 전부 바뀌어도 배치가 같으면 재계산 없음(이름 비교였다면 오탐)."""
    assert invalid(SAME) == set()


def test_group_number_swap_is_not_a_change():
    """그룹 번호만 뒤바뀐 것은 정리 결과가 같다."""
    assert invalid(list(reversed(SAME))) == set()


def test_ct_ht_role_swap_is_not_a_change():
    """clean_group 은 CT/HT 역할을 구분하지 않는다 — 스왑은 재계산 대상이 아니다."""
    swapped = [{"rt": "A_RT(RT)", "members": ["A_HT(HT)", "A_CT(CT)"],
                "member_roles": ["CT", "HT"]}, SAME[1]]
    assert invalid(swapped) == set()


def test_rt_partner_change_invalidates_only_that_member():
    """A_CT 를 B 그룹으로 옮기면 그 member(원본 index 1) 만 무효다."""
    moved = [{"rt": "A_RT(RT)", "members": ["A_HT(HT)"], "member_roles": ["HT"]},
             {"rt": "B_RT(RT)", "members": ["B_CT(CT)", "A_CT(CT)"],
              "member_roles": ["CT", "CT"]}]
    assert invalid(moved) == {1}


def test_new_group_member_is_invalid():
    """추정에 없던 member 가 생기면 무효다."""
    added = [{"rt": "A_RT(RT)", "members": ["A_CT(CT)", "A_HT(HT)", "B_CT(CT)"],
              "member_roles": ["CT", "HT", "CT"]}]
    assert invalid(added) == {4}


def test_limit_file_invalidates_every_member():
    """limit 파일이 들어오면 bin 매칭이 통째로 달라져 전 member 가 무효다."""
    assert invalid(SAME, bin_map={"ITEM": 5}) == {1, 2, 4}


# ── 추정 그룹 산출 ──────────────────────────────────────────────────────────
def test_guess_groups_shape_matches_dialog_result():
    """result_arrangement()["groups"] 와 같은 형태여야 clean_frames 에 그대로 넣는다."""
    got = honey_main._guess_temperature_groups(["W1_RT", "W1_CT", "W1_HT"], {})
    assert got == [{"rt": "W1_RT", "members": ["W1_CT", "W1_HT"],
                    "member_roles": ["CT", "HT"]}]


def test_guess_groups_drops_rt_only_group():
    """member 없는 RT 단독은 정리할 게 없어 그룹으로 만들지 않는다."""
    assert honey_main._guess_temperature_groups(["W1_RT"], {}) == []


def test_guess_groups_uses_folder_roles():
    """폴더에서 역할을 알면 그 경로(suggest_groups_by_role)를 쓴다 — 창과 같은 규칙."""
    names = ["w1", "w2"]
    got = honey_main._guess_temperature_groups(names, {"w1": "RT", "w2": "CT"})
    assert got == [{"rt": "w1", "members": ["w2"], "member_roles": ["CT"]}]


def test_guess_groups_survives_unnamed_sources():
    """규칙에 안 맞는 이름이면 빈 목록 — 호출부가 raw 선행만 하고 넘어간다."""
    assert honey_main._guess_temperature_groups(["aaa", "bbb"], {}) == []


if __name__ == "__main__":
    checks = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in checks:
        fn()
    print(f"PASS: test_webreport_encode_cache ({len(checks)} checks)")

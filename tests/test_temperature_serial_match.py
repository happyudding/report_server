"""Temperature 모드 — **좌표 없는 rawdata** 의 SERIAL 순서 매칭 회귀 테스트.

실행:
    python tests/test_temperature_serial_match.py

고정하는 계약 (2026-08-24 사용자 요청):
  1. 좌표(XPOS/YPOS)가 비어 있는 source 는 ``has_coords`` 가 False 로 판정한다
     (한 행이라도 둘 다 채워져 있으면 True — 부분 결측은 좌표 있음).
  2. ``clean_frames(..., serial_match=False)`` 는 **종전 동작 그대로**다 — 좌표가 없어도
     좌표 매칭을 하며, 그 결과 아무 행도 걸러지지 않는다(사용자에게 묻는 이유).
  3. ``serial_match=True`` 면 좌표 없는 pair 만 **SERIAL 오름차순 i 번째끼리** 짝지어
     RT 가 BIN==1 인 die 만 남긴다. 양쪽에 좌표가 있는 pair 는 좌표 매칭 그대로다.
  4. 개수가 다르면 **적은 쪽 기준**으로 앞에서부터만 짝짓고, 통계에 rt_rows/member_rows/
     paired 를 남긴다(클라가 "가장 적은 raw data 기준" 안내를 띄우는 근거).
  5. 남은 행의 재판정(RT limit·bin 매칭)은 좌표 경로와 완전히 동일하다.

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS  # noqa: E402
from web_report.temperature import (clean_frames, clean_group, data_row_count,  # noqa: E402
                                    format_stats, has_coords, sources_without_coords)

ITEMS = ["ItemA", "ItemB"]
GROUPS = [{"rt": "RT", "members": ["CT"]}]


def make_df(rows, *, hilim=(10, 10), lolim=(0, 0)):
    """합성 7-meta honeyform 프레임 — rows: [(serial, x, y, bin, failtno, A, B), ...]."""
    frame = [
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P1"],
        ["UNIT", "", "", "", "", "", "", "V", "V"],
        ["HILIM", "", "", "", "", "", "", *hilim],
        ["LOLIM", "", "", "", "", "", "", *lolim],
    ]
    for serial, x, y, b, ft, a, bb in rows:
        frame.append([serial, 1, 1, x, y, b, ft, a, bb])
    return pd.DataFrame(frame, columns=META_COLUMNS + ITEMS)


def rt_noxy():
    """좌표 없는 RT — SERIAL 2·4 는 pass, 6 은 ItemA HILIM 위반 fail(bin 7), 8 은 pass."""
    return make_df([
        (2, "", "", 1, "", 5, 5),
        (6, "", "", 7, 100, 15, 5),      # fail — 파일 순서를 일부러 섞어 둔다
        (4, "", "", 1, "", 5, 5),
        (8, "", "", 1, "", 5, 5),
    ])


def ct_noxy(rows=None):
    """좌표 없는 CT — 기본은 RT 와 같은 SERIAL 4개(파일 순서는 다르다)."""
    return make_df(rows if rows is not None else [
        (8, "", "", 1, "", 5, 5),
        (4, "", "", 1, "", 5, 5),
        (2, "", "", 1, "", 5, 5),
        (6, "", "", 1, "", 5, 5),
    ])


def serials_of(df):
    return [str(v) for v in df.iloc[6:, 0].tolist()]


def test_has_coords():
    assert has_coords(make_df([(1, 0, 0, 1, "", 5, 5)])) is True
    assert has_coords(make_df([(1, 3, 4, 1, "", 5, 5)])) is True
    assert has_coords(rt_noxy()) is False
    # NaN 좌표도 없음 — _fmt 가 "" 를 내는 값과 같은 기준
    assert has_coords(make_df([(1, float("nan"), float("nan"), 1, "", 5, 5)])) is False
    # 한쪽만 있으면 짝지을 좌표가 아니다
    assert has_coords(make_df([(1, 3, "", 1, "", 5, 5)])) is False
    # 부분 결측(한 행이라도 둘 다 있음)은 좌표 있음 — 종전 좌표 매칭 유지
    assert has_coords(make_df([(1, "", "", 1, "", 5, 5), (2, 3, 4, 1, "", 5, 5)])) is True
    # 데이터 행이 없는 프레임
    assert has_coords(make_df([])) is False
    # 앞부분(_COORD_PROBE 200행)에 좌표가 없고 뒤에만 있으면 전량 스캔이 잡아낸다
    late = [(i, "", "", 1, "", 5, 5) for i in range(250)]
    late[240] = (240, 3, 4, 1, "", 5, 5)
    assert has_coords(make_df(late)) is True
    frames = {"RT": rt_noxy(), "OK": make_df([(1, 0, 0, 1, "", 5, 5)])}
    assert sources_without_coords(frames) == ["RT"]
    assert data_row_count(rt_noxy()) == 4


def test_default_is_unchanged_silent_passthrough():
    """serial_match 기본값(False)은 종전 동작 — 좌표가 없으면 한 행도 걸러지지 않는다."""
    out, stats = clean_group({"RT": rt_noxy(), "CT": ct_noxy()}, "RT", ["CT"])
    assert data_row_count(out["CT"]) == 4, serials_of(out["CT"])
    assert stats["CT"]["dropped"] == 0
    assert "serial" not in stats["CT"]


def test_serial_match_keeps_rt_pass_partners():
    """SERIAL 순서로 짝지어 RT pass(2·4·8)만 남고 RT fail(6)은 빠진다."""
    out, stats = clean_group({"RT": rt_noxy(), "CT": ct_noxy()}, "RT", ["CT"],
                             serial_match=True)
    assert sorted(serials_of(out["CT"])) == ["2", "4", "8"], serials_of(out["CT"])
    assert stats["CT"]["serial"] is True
    assert (stats["CT"]["rt_rows"], stats["CT"]["member_rows"]) == (4, 4)
    assert stats["CT"]["paired"] == 4
    assert stats["CT"]["dropped"] == 1
    assert stats["CT"]["kept"] == 3
    # 남은 행 순서는 원본 파일 순서 그대로다 (행 필터일 뿐 재정렬이 아니다)
    assert serials_of(out["CT"]) == ["8", "4", "2"]
    # RT 프레임은 손대지 않는다
    assert serials_of(out["RT"]) == ["2", "6", "4", "8"]


def test_serial_match_position_not_value():
    """짝은 SERIAL **값 일치**가 아니라 **순번**이다 — 값이 달라도 i 번째끼리 짝짓는다."""
    ct = ct_noxy([
        (11, "", "", 1, "", 5, 5),
        (33, "", "", 1, "", 5, 5),
        (22, "", "", 1, "", 5, 5),
        (44, "", "", 1, "", 5, 5),
    ])
    out, _stats = clean_group({"RT": rt_noxy(), "CT": ct}, "RT", ["CT"], serial_match=True)
    # RT 순서 2,4,6,8 ↔ CT 순서 11,22,33,44 → RT 6(fail)의 짝은 CT 33
    kept = sorted(serials_of(out["CT"]), key=int)
    assert kept == ["11", "22", "44"], kept


def test_count_mismatch_uses_min_basis():
    """개수가 다르면 적은 쪽(RT 4행) 기준으로 앞 4개만 짝짓고 나머지는 버린다."""
    ct = ct_noxy([
        (2, "", "", 1, "", 5, 5),
        (4, "", "", 1, "", 5, 5),
        (6, "", "", 1, "", 5, 5),
        (8, "", "", 1, "", 5, 5),
        (10, "", "", 1, "", 5, 5),
        (12, "", "", 1, "", 5, 5),
    ])
    out, stats = clean_group({"RT": rt_noxy(), "CT": ct}, "RT", ["CT"], serial_match=True)
    assert (stats["CT"]["rt_rows"], stats["CT"]["member_rows"]) == (4, 6)
    assert stats["CT"]["paired"] == 4
    # SERIAL 2,4,8 이 RT pass 의 짝 (6 은 RT fail, 10·12 는 짝이 없어 제외)
    assert serials_of(out["CT"]) == ["2", "4", "8"]
    assert stats["CT"]["dropped"] == 3

    # 반대 방향 — member 가 더 적으면 그 개수만큼만 짝짓는다
    out2, stats2 = clean_group({"RT": rt_noxy(), "CT": ct_noxy([(2, "", "", 1, "", 5, 5)])},
                               "RT", ["CT"], serial_match=True)
    assert stats2["CT"]["paired"] == 1
    assert serials_of(out2["CT"]) == ["2"]


def test_serial_match_rejudges_with_rt_limits():
    """남은 행은 RT limit 으로 재판정한다 — bin 은 RT 관측 fail bin(7)."""
    ct = ct_noxy([
        (2, "", "", 1, "", 5, 5),        # pass
        (4, "", "", 1, "", 99, 5),       # RT HILIM(10) 위반 → fail
        (6, "", "", 1, "", 5, 5),        # RT fail 의 짝 → 제외
        (8, "", "", 1, "", 5, 5),        # pass
    ])
    out, stats = clean_group({"RT": rt_noxy(), "CT": ct}, "RT", ["CT"], serial_match=True)
    data = out["CT"].iloc[6:]
    bins = dict(zip([str(v) for v in data.iloc[:, 0]], [str(v) for v in data.iloc[:, 5]]))
    tnos = dict(zip([str(v) for v in data.iloc[:, 0]], [str(v) for v in data.iloc[:, 6]]))
    assert bins == {"2": "1", "4": "7", "8": "1"}, bins
    assert tnos["4"] == "100" and tnos["2"] == ""
    assert stats["CT"]["fail"] == 1 and stats["CT"]["pass"] == 2
    # CT 자신의 limit 메타행은 원본 그대로 (화면 표시용 보존)
    assert list(out["CT"].iloc[4, 7:]) == [10, 10]


def test_pair_with_coords_keeps_coord_matching():
    """serial_match=True 여도 **양쪽에 좌표가 있는 pair** 는 좌표 매칭 그대로다."""
    rt = make_df([
        (2, 0, 0, 1, "", 5, 5),
        (4, 1, 0, 7, 100, 15, 5),        # fail 좌표 (1,0)
    ])
    ct = make_df([
        (2, 0, 0, 1, "", 5, 5),          # 유지
        (4, 1, 0, 1, "", 5, 5),          # 제외 — RT fail 좌표
        (6, 9, 9, 1, "", 5, 5),          # 제외 — RT 에 없는 좌표
    ])
    out, stats = clean_group({"RT": rt, "CT": ct}, "RT", ["CT"], serial_match=True)
    assert serials_of(out["CT"]) == ["2"]
    assert "serial" not in stats["CT"]


def test_mixed_groups_decided_per_pair():
    """그룹이 여러 개면 pair 마다 따로 판정한다 — 좌표 있는 그룹은 영향 없음."""
    rt2 = make_df([(2, 0, 0, 1, "", 5, 5), (4, 1, 0, 7, 100, 15, 5)])
    ct2 = make_df([(2, 0, 0, 1, "", 5, 5), (4, 1, 0, 1, "", 5, 5)])
    frames = {"RT": rt_noxy(), "CT": ct_noxy(), "RT2": rt2, "CT2": ct2}
    groups = [{"rt": "RT", "members": ["CT"]}, {"rt": "RT2", "members": ["CT2"]}]
    out, stats = clean_frames(frames, groups, None, True)
    assert stats["CT"].get("serial") is True
    assert "serial" not in stats["CT2"]
    assert serials_of(out["CT2"]) == ["2"]
    assert sorted(serials_of(out["CT"])) == ["2", "4", "8"]
    lines = format_stats(stats)
    assert any("SERIAL 순서 매칭" in line for line in lines), lines
    assert any(line.startswith("CT2: RT pass 좌표 필터") for line in lines), lines


def main():
    fns = [test_has_coords,
           test_default_is_unchanged_silent_passthrough,
           test_serial_match_keeps_rt_pass_partners,
           test_serial_match_position_not_value,
           test_count_mismatch_uses_min_basis,
           test_serial_match_rejudges_with_rt_limits,
           test_pair_with_coords_keeps_coord_matching,
           test_mixed_groups_decided_per_pair]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    main()

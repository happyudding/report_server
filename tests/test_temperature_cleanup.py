"""Temperature 모드 rawdata 정리(좌표 필터 + RT limit 재판정) 회귀 테스트.

실행:
    python tests/test_temperature_cleanup.py

고정하는 계약 (사용자 확정):
  1. CT/HT 는 RT 의 BIN==1 좌표(XPOS,YPOS)만 남긴다. RT 프레임은 손대지 않는다.
  2. 남은 CT/HT 행은 **RT 의 HILIM/LOLIM** 으로 재판정한다 — CT/HT 자신의 limit 메타행은
     원본 그대로 둔다.
  3. 재판정 fail 의 BIN 은 ① .lt/.pds 매핑(LSL/USL 방향별) → ② RT 에서 죽은 bin →
     ③ 999 순으로 정해진다. pass 행은 BIN=1 / FAILTNO 공백.
  4. 첫 fail 항목 판정 순서는 **RT 의 TSEQ** 순이다.
  5. 정리 결과는 encode/decode parquet 왕복을 통과한다 (업로드 가능한 형태).

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import (META_COLUMNS, decode_split_honeyform_parquet,  # noqa: E402
                                  encode_honeyform_parquet)
from web_report.temperature import (clean_frames, clean_group,  # noqa: E402
                                    rt_fail_bin_map, rt_limits, rt_pass_coords)

ITEMS = ["ItemA", "ItemB"]


def make_df(rows, *, hilim=(10, 10), lolim=(0, 0), tseq=(1, 2), tno=(100, 200)):
    """합성 7-meta honeyform 프레임.

    rows: [(xpos, ypos, bin, failtno, itemA 값, itemB 값), ...]
    """
    cols = META_COLUMNS + ITEMS
    frame = [
        ["TSEQ", "", "", "", "", "", "", *tseq],
        ["TNO", "", "", "", "", "", "", *tno],
        ["STEP", "", "", "", "", "", "", "P1", "P1"],
        ["UNIT", "", "", "", "", "", "", "V", "V"],
        ["HILIM", "", "", "", "", "", "", *hilim],
        ["LOLIM", "", "", "", "", "", "", *lolim],
    ]
    for i, (x, y, b, ft, a, bb) in enumerate(rows):
        frame.append([f"S{i}", 1, 1, x, y, b, ft, a, bb])
    return pd.DataFrame(frame, columns=cols)


def rt_frame():
    """RT: (0,0)·(1,0)·(2,0) pass, (3,0) 은 ItemA HILIM 위반으로 bin 7 fail."""
    return make_df([
        (0, 0, 1, "", 5, 5),
        (1, 0, 1, "", 5, 5),
        (2, 0, 1, "", 5, 5),
        (3, 0, 7, 100, 15, 5),
    ])


def test_rt_helpers():
    rt = rt_frame()
    assert rt_pass_coords(rt) == {("0", "0"), ("1", "0"), ("2", "0")}
    assert rt_limits(rt) == {"ItemA": (0.0, 10.0), "ItemB": (0.0, 10.0)}
    # RT 에서 ItemA 가 HILIM 위반(15 > 10)으로 죽은 bin = 7
    fail_bins = rt_fail_bin_map(rt)
    assert fail_bins[("ItemA", "hi")] == "7", fail_bins
    assert fail_bins[("ItemA", "")] == "7", fail_bins


def test_coord_filter_and_rt_untouched():
    """RT pass 좌표만 남고, RT 프레임 자체는 원본 그대로."""
    rt = rt_frame()
    ct = make_df([
        (0, 0, 1, "", 5, 5),      # 유지 — RT pass
        (3, 0, 1, "", 5, 5),      # 제외 — RT 에서 fail 한 좌표
        (9, 9, 1, "", 5, 5),      # 제외 — RT 에 없는 좌표
        (2, 0, 1, "", 5, 5),      # 유지
    ])
    out, stats = clean_group({"RT": rt, "CT": ct}, "RT", ["CT"])

    assert out["RT"] is rt, "RT 프레임은 정리 대상이 아니다"
    kept = out["CT"].iloc[6:]
    assert len(kept) == 2, kept
    assert [str(v) for v in kept.iloc[:, 3]] == ["0", "2"], kept.iloc[:, 3].tolist()
    assert stats["CT"]["dropped"] == 2 and stats["CT"]["kept"] == 2, stats


def test_rejudge_against_rt_limits_keeps_member_limits():
    """CT 자신의 limit 이 느슨해도 RT limit 으로 fail 판정. CT limit 메타행은 불변."""
    rt = rt_frame()
    ct = make_df([
        (0, 0, 1, "", 5, 5),       # pass
        (1, 0, 1, "", 15, 5),      # RT HILIM(10) 위반 → fail (CT limit 100 이면 pass 였음)
        (2, 0, 1, "", -5, 5),      # RT LOLIM(0) 위반 → fail
    ], hilim=(100, 100), lolim=(-100, -100))
    out, stats = clean_group({"RT": rt, "CT": ct}, "RT", ["CT"])

    data = out["CT"].iloc[6:]
    assert [str(v) for v in data.iloc[:, 5]] != ["1", "1", "1"], "재판정이 안 됐다"
    assert str(data.iloc[0, 5]) == "1" and str(data.iloc[0, 6]) == "", data.iloc[0].tolist()
    # fail 행은 FAILTNO 가 그 항목의 TNO(100)
    assert str(data.iloc[1, 6]) == "100", data.iloc[1].tolist()
    assert stats["CT"]["fail"] == 2 and stats["CT"]["pass"] == 1, stats

    # CT 의 HILIM/LOLIM 메타행은 원본 유지 (확정 사항 3)
    assert out["CT"].iloc[4, 7] == 100 and out["CT"].iloc[5, 7] == -100, out["CT"].iloc[4:6]


def test_bin_priority_map_then_rt_then_999():
    """bin 우선순위: .lt/.pds 매핑 → RT 관측 bin → 999."""
    rt = rt_frame()
    ct = make_df([
        (0, 0, 1, "", 15, 5),      # ItemA HILIM 위반
        (1, 0, 1, "", -5, 5),      # ItemA LOLIM 위반
        (2, 0, 1, "", 5, 15),      # ItemB HILIM 위반 — RT 관측·매핑 모두 없음
    ])

    # ① 매핑 있음 — 방향별 bin 이 그대로 쓰인다
    bin_map = {"ItemA": {"lsl_bin": "20", "usl_bin": "19"}}
    out, stats = clean_group({"RT": rt, "CT": ct}, "RT", ["CT"], bin_map)
    bins = [str(v) for v in out["CT"].iloc[6:].iloc[:, 5]]
    assert bins == ["19", "20", "999"], bins
    assert stats["CT"]["unknown_bin_items"] == ["ItemB"], stats

    # ② 매핑 없음 — RT 에서 ItemA 가 죽은 bin(7) 으로 폴백, ItemB 는 999
    out, _ = clean_group({"RT": rt, "CT": ct}, "RT", ["CT"])
    bins = [str(v) for v in out["CT"].iloc[6:].iloc[:, 5]]
    assert bins == ["7", "7", "999"], bins


def test_first_fail_follows_rt_tseq_order():
    """두 항목이 동시에 위반이면 RT TSEQ 가 앞선 항목이 fail 항목이 된다."""
    rt = make_df([(0, 0, 1, "", 5, 5)], tseq=(2, 1), tno=(100, 200))   # ItemB 가 먼저
    ct = make_df([(0, 0, 1, "", 15, 15)])                              # 둘 다 HILIM 위반
    out, _ = clean_group({"RT": rt, "CT": ct}, "RT", ["CT"],
                         {"ItemA": {"usl_bin": "31"}, "ItemB": {"usl_bin": "32"}})
    row = out["CT"].iloc[6]
    assert str(row.iloc[5]) == "32", row.tolist()      # ItemB 의 bin
    assert str(row.iloc[6]) == "200", row.tolist()     # ItemB 의 TNO


def test_rt_only_group_and_unlisted_source_pass_through():
    """CT/HT 가 없는 RT 단독 그룹, 그룹 밖 source 는 원본 그대로."""
    rt, other = rt_frame(), rt_frame()
    out, stats = clean_frames({"RT": rt, "X": other}, [{"rt": "RT", "members": []}])
    assert out["RT"] is rt and out["X"] is other, out
    assert stats == {}, stats


def test_cleaned_frame_survives_parquet_roundtrip():
    """정리 결과가 업로드 인코딩·서버 디코딩을 통과한다."""
    rt = rt_frame()
    ct = make_df([(0, 0, 1, "", 15, 5), (1, 0, 1, "", 5, 5)])
    out, _ = clean_group({"RT": rt, "CT": ct}, "RT", ["CT"], {"ItemA": {"usl_bin": "19"}})

    table = decode_split_honeyform_parquet(
        encode_honeyform_parquet(out["CT"]), source="CT", file_name="ct.csv")
    assert list(table.data["BIN"]) == ["19", "1"], list(table.data["BIN"])
    assert list(table.data["FAILTNO"]) == ["100", ""], list(table.data["FAILTNO"])
    # 메타행은 parquet 저장 규약대로 문자열로 되돌아온다 (RT limit 로 덮어쓰지 않았음)
    assert table.hilim == {"ItemA": "10", "ItemB": "10"}, table.hilim


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_rt_helpers, test_coord_filter_and_rt_untouched,
               test_rejudge_against_rt_limits_keeps_member_limits,
               test_bin_priority_map_then_rt_then_999,
               test_first_fail_follows_rt_tseq_order,
               test_rt_only_group_and_unlisted_source_pass_through,
               test_cleaned_frame_survives_parquet_roundtrip):
        fn()
        checks += 1
    print(f"PASS: test_temperature_cleanup ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

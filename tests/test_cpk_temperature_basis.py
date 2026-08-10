"""Temperature 모드 CPK 기준 회귀 테스트 (2026-08-10).

실행:
    python tests/test_cpk_temperature_basis.py

고정하는 계약 ([web_report/tabs/cpk.py](../web_report/tabs/cpk.py) 모듈 docstring):
  1. CT/HT 의 CPK 모집단은 **RT 에서 Bin1 이던 die 전부**다 — 자기 BIN 으로 거르지 않는다.
     (CT/HT 프레임은 업로드 전 정리에서 이미 RT pass 좌표만 남아 있으므로 프레임 전 행.)
  2. CT/HT 의 CPK 는 **RT limit** 으로 계산하고, 표시 limit(lower/upper_limit)도 RT 것이다
     — 계산에 쓴 규격과 화면 규격이 어긋나면 CPK 탭의 한계값 역산이 맞지 않는다.
  3. RT 소스와 Temperature 가 아닌 모드는 **종전 그대로** 자기 Bin1 × 자기 limit 이다.

CT 프레임은 손으로 만들지 않고 실제 정리 함수(``temperature.clean_frames``)를 돌려 만든다
— 그래야 "이미 RT pass 좌표만 남아 있다" 는 1번 전제가 테스트 안에서도 사실이 된다.

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례). pytest 로 수집해도 동작한다.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402
from web_report.tabs.common import bin_types  # noqa: E402
from web_report.tabs.cpk import build_cpk_rows  # noqa: E402
from web_report.temperature import clean_frames  # noqa: E402

# RT: LOLIM 8 / HILIM 12. 앞 4 die 는 Bin1, 뒤 2 die 는 fail(극단값).
_RT_BINS = [1, 1, 1, 1, 4, 4]
_RT_VALUES = [9.5, 10.0, 10.5, 10.0, 20.0, 20.0]
_RT_BIN1 = [9.5, 10.0, 10.5, 10.0]

# CT: 자기 limit 은 0~100 으로 아주 느슨해 자기 BIN 은 전부 1이다. RT pass 좌표(앞 4개)에
# 13.0 을 하나 두어 **RT limit(12) 로는 fail** 이 되게 한다 — 옛 기준(자기 Bin1)이면 이
# die 가 빠지고, 새 기준이면 남는다. 뒤 2 die 는 RT 에서 죽은 좌표라 정리에서 사라진다.
_CT_BINS = [1, 1, 1, 1, 1, 1]
_CT_VALUES = [9.0, 10.0, 11.0, 13.0, 5.0, 5.0]
_CT_AFTER_CLEAN = [9.0, 10.0, 11.0, 13.0]      # RT pass 좌표만 남은 뒤의 전 행
_CT_SELF_BIN1 = [9.0, 10.0, 11.0]              # 옛 기준(자기 BIN==1)이면 이것만 남는다

GROUPS = [{"rt": "WF1_RT", "members": ["WF1_CT"], "member_roles": ["CT"]}]


def raw_df(tag, bins, values, lolim, hilim):
    cols = META_COLUMNS + ["ItemA"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1],
        ["TNO", "", "", "", "", "", "", 100],
        ["STEP", "", "", "", "", "", "", "P1"],
        ["UNIT", "", "", "", "", "", "", "V"],
        ["HILIM", "", "", "", "", "", "", hilim],
        ["LOLIM", "", "", "", "", "", "", lolim],
    ]
    for i, (b, v) in enumerate(zip(bins, values)):
        rows.append([f"{tag}-{i}", 1, 1, i, 0, b, 100 if b != 1 else "", v])
    return pd.DataFrame(rows, columns=cols)


def temp_tables():
    """정리(clean_frames)를 실제로 거친 [RT table, CT table]."""
    frames = {"WF1_RT": raw_df("rt", _RT_BINS, _RT_VALUES, 8, 12),
              "WF1_CT": raw_df("ct", _CT_BINS, _CT_VALUES, 0, 100)}
    cleaned, _stats = clean_frames(frames, GROUPS)
    return [split_honeyform(cleaned[name], source=name, file_name=name)
            for name in ("WF1_RT", "WF1_CT")]


def expected(values, lo, hi):
    """값 목록으로 직접 계산한 (n, average, cpk) — 구현과 독립된 기대값."""
    s = pd.Series(values, dtype="float64")
    avg = s.mean()
    std = s.std(ddof=1)
    cpk = min((avg - lo) / (3.0 * std), (hi - avg) / (3.0 * std))
    return len(values), round(avg, 4), round(cpk, 3)


def cpk_row(rows, source):
    for r in rows:
        if r["source"] == source and r["subject"] == "ItemA":
            return r
    raise AssertionError(f"CPK 행 없음: {source}")


def test_clean_leaves_rt_pass_coords_only():
    """전제 확인 — 정리된 CT 프레임 = RT Bin1 좌표 4행, 그중 1개는 재판정 fail."""
    _rt, ct = temp_tables()
    values = [float(v) for v in ct.data["ItemA"]]
    assert values == _CT_AFTER_CLEAN, values
    bins = list(bin_types(ct))
    assert bins.count("1") == 3 and len(bins) == 4, bins


def test_member_uses_rt_bin1_dies_and_rt_limits():
    """CT = RT Bin1 die 전부(4행) × RT limit(8~12) — 자기 Bin1(3행)·자기 limit 이 아니다."""
    tables = temp_tables()
    rows = build_cpk_rows(tables, ["ItemA"], GROUPS)

    n, avg, cpk = expected(_CT_AFTER_CLEAN, 8.0, 12.0)
    ct = cpk_row(rows, "WF1_CT")
    assert ct["n"] == n, (ct["n"], n)
    assert ct["average"] == avg, (ct["average"], avg)
    assert ct["cpk"] == cpk, (ct["cpk"], cpk)
    # 표시 limit 도 RT 것 — CT 자신의 0/100 이면 CPK 값과 화면 규격이 어긋난다.
    assert (ct["lower_limit"], ct["upper_limit"]) == (8, 12), ct

    # 옛 기준(자기 Bin1 × 자기 limit)과 실제로 값이 다르다 — 테스트가 무의미해지지 않게 고정.
    old_n, old_avg, _old_cpk = expected(_CT_SELF_BIN1, 0.0, 100.0)
    assert (ct["n"], ct["average"]) != (old_n, old_avg), ct


def test_rt_source_unchanged():
    """RT 는 종전 그대로 자기 Bin1 × 자기 limit — fail die 의 극단값이 섞이지 않는다."""
    tables = temp_tables()
    rows = build_cpk_rows(tables, ["ItemA"], GROUPS)
    n, avg, cpk = expected(_RT_BIN1, 8.0, 12.0)
    rt = cpk_row(rows, "WF1_RT")
    assert (rt["n"], rt["average"], rt["cpk"]) == (n, avg, cpk), rt
    assert (rt["lower_limit"], rt["upper_limit"]) == (8, 12), rt


def test_without_groups_is_legacy_behaviour():
    """temperature_groups 를 주지 않으면 전 소스가 종전 기준(자기 Bin1 × 자기 limit)."""
    tables = temp_tables()
    rows = build_cpk_rows(tables, ["ItemA"])
    n, avg, cpk = expected(_CT_SELF_BIN1, 0.0, 100.0)
    ct = cpk_row(rows, "WF1_CT")
    assert (ct["n"], ct["average"], ct["cpk"]) == (n, avg, cpk), ct
    assert (ct["lower_limit"], ct["upper_limit"]) == (0, 100), ct
    # RT 는 어느 경로에서도 같은 값이어야 한다.
    assert cpk_row(rows, "WF1_RT") == cpk_row(build_cpk_rows(tables, ["ItemA"], GROUPS),
                                              "WF1_RT")


def test_payload_wires_groups_through():
    """build_report_payload(mode="Temperature") 가 CPK 시트까지 기준을 실어 보낸다."""
    tables = temp_tables()
    payload = build_report_payload(tables, mode="Temperature",
                                   temperature_groups={"groups": GROUPS})
    n, avg, cpk = expected(_CT_AFTER_CLEAN, 8.0, 12.0)
    ct = cpk_row(payload["sheets"]["CPK"], "WF1_CT")
    assert (ct["n"], ct["average"], ct["cpk"]) == (n, avg, cpk), ct

    # 같은 tables 를 Normal 로 그리면 종전 기준 — 모드 분기가 CPK 에도 걸린다.
    normal = build_report_payload(temp_tables())
    old_n, old_avg, old_cpk = expected(_CT_SELF_BIN1, 0.0, 100.0)
    ct_normal = cpk_row(normal["sheets"]["CPK"], "WF1_CT")
    assert (ct_normal["n"], ct_normal["average"], ct_normal["cpk"]) == \
        (old_n, old_avg, old_cpk), ct_normal


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_clean_leaves_rt_pass_coords_only,
               test_member_uses_rt_bin1_dies_and_rt_limits,
               test_rt_source_unchanged,
               test_without_groups_is_legacy_behaviour,
               test_payload_wires_groups_through):
        fn()
        checks += 1
    print(f"PASS: test_cpk_temperature_basis ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

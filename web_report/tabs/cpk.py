"""CPK tab payload builder."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import PASS_BIN, bin_types, json_safe, num, round_num

# 이슈 판단 공용 임계값 — Issue Table(CPK 섹션)·Distribution(status 분류)이 공유한다.
CPK_THRESHOLD = 1.33


def worst_cpk_by_subject(cpk_rows) -> dict:
    """subject 별 모든 source 행 중 최저(worst-case) cpk (None 제외).

    dict 삽입 순서 = cpk_rows 에서 subject 가 처음 등장한 순서."""
    worst: dict = {}
    for r in cpk_rows or []:
        cpk = r.get("cpk")
        if cpk is None:
            continue
        subject = r.get("subject")
        if subject not in worst or cpk < worst[subject]:
            worst[subject] = cpk
    return worst


def _stats(series, lo, hi):
    s = pd.to_numeric(series, errors="coerce").dropna()
    n = int(len(s))
    avg = s.mean() if n else None
    stdev = s.std(ddof=1) if n > 1 else None
    lo_n = num(lo)
    hi_n = num(hi)
    can = (
        n > 1
        and stdev not in (None, 0)
        and num(stdev) is not None
        and lo_n is not None
        and hi_n is not None
    )
    cp = cpl = cpu = cpk = None
    if can:
        cp = (hi_n - lo_n) / (6.0 * stdev)
        cpl = (avg - lo_n) / (3.0 * stdev)
        cpu = (hi_n - avg) / (3.0 * stdev)
        cpk = min(cpl, cpu)
    return {
        "n": n,
        "min": round_num(s.min() if n else None),
        "median": round_num(s.median() if n else None),
        "max": round_num(s.max() if n else None),
        "average": round_num(avg, 4),
        "stdev": round_num(stdev, 3),
        "cp": round_num(cp, 3),
        "cpl": round_num(cpl, 3),
        "cpu": round_num(cpu, 3),
        "cpk": round_num(cpk, 3),
    }


def build_cpk_rows(tables, all_items):
    rows = []
    # BIN 마스크는 item 과 무관 — 테이블당 1회만 계산 (item 루프 안에서 재계산 금지)
    bin1_masks = [np.array([b == PASS_BIN for b in bin_types(table)], dtype=bool)
                  for table in tables]
    for item in all_items:
        for table, bin1_mask in zip(tables, bin1_masks):
            if item not in table.item_columns:
                continue
            lo = table.lolim.get(item)
            hi = table.hilim.get(item)
            # 전체(모든 die) 기준 통계는 기존 필드 그대로 — Issue Table·Distribution 이
            # 계속 소비(하위호환). Bin1(BIN==PASS_BIN, 양품) 기준은 *_bin1 로 병기하여
            # CPK 탭 토글이 클라이언트에서 표시 필드만 바꾸도록 한다.
            numeric = pd.to_numeric(table.data[item], errors="coerce")
            bin1_stats = _stats(numeric[bin1_mask], lo, hi)
            rows.append({
                "subject": item,
                "source": table.source,
                "units": json_safe(table.units.get(item)) or "",
                "lower_limit": round_num(lo),
                "upper_limit": round_num(hi),
                **_stats(numeric, lo, hi),
                **{f"{k}_bin1": v for k, v in bin1_stats.items()},
            })
    return rows


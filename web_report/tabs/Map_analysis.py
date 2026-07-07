"""Map Analysis (wafer map) tab payload builder.

소스별로 die 리스트({x, y, bin})와 격자 범위, bin 별 집계를 만든다.
BIN == PASS_BIN("1") 이 Pass, 그 외는 Fail.
규칙 #6: die 는 전량 표현한다 (다운샘플링 금지).
"""
from __future__ import annotations

from collections import Counter

from .common import PASS_BIN, fmt_type


def _to_int(value):
    """좌표 문자열/실수 → int. 변환 불가 시 None."""
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _bin_sort_key(bin_value):
    try:
        return (0, float(bin_value))
    except ValueError:
        return (1, bin_value)


def build_map_analysis_rows(tables):
    rows = []
    for table in tables:
        data = table.data
        dies = []
        bin_counter = Counter()
        xs, ys = [], []
        for _, row in data.iterrows():
            x = _to_int(row.get("XPOS"))
            y = _to_int(row.get("YPOS"))
            if x is None or y is None:
                continue
            bin_value = fmt_type(row.get("BIN"))
            dies.append({"x": x, "y": y, "bin": bin_value})
            bin_counter[bin_value] += 1
            xs.append(x)
            ys.append(y)

        total = len(dies)
        pass_first = sorted(
            bin_counter.items(),
            key=lambda kv: (0, _bin_sort_key(kv[0])) if kv[0] == PASS_BIN
            else (1, -kv[1], _bin_sort_key(kv[0])),
        )
        bin_counts = [
            {
                "bin": bin_value,
                "count": count,
                "pct": round(count / total * 100.0, 2) if total else 0.0,
                "is_pass": bin_value == PASS_BIN,
            }
            for bin_value, count in pass_first
        ]

        rows.append({
            "source": table.source,
            "file_name": table.file_name,
            "total": total,
            "x_min": min(xs) if xs else None,
            "x_max": max(xs) if xs else None,
            "y_min": min(ys) if ys else None,
            "y_max": max(ys) if ys else None,
            "dies": dies,
            "bin_counts": bin_counts,
        })
    return rows

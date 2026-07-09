"""Map Analysis (wafer map) tab payload builder.

소스별로 die 리스트({x, y, bin})와 격자 범위, bin 별 집계를 만든다.
BIN == PASS_BIN("1") 이 Pass, 그 외는 Fail.
규칙 #6: die 는 전량 표현한다 (다운샘플링 금지).
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from .common import PASS_BIN, bin_sort_key, fmt_type
from ..wafer_frame import frame_for


def build_map_analysis_rows(tables, product_type="", product=""):
    # 제품 기준정보(die pitch+wafer 크기)가 있으면 고정 프레임으로 격자 틀을 덮어쓴다.
    # 없으면 frame=None → 현행(데이터 좌표 min/max) 유지.
    frame = frame_for(product_type, product)
    rows = []
    for table in tables:
        data = table.data
        # 행 단위 iterrows 대신 좌표를 일괄 숫자 변환하고 유효 행만 추린다
        # (변환 불가/결측 좌표 행 제외 — 기존 _to_int 의 None 처리와 동일).
        x_num = pd.to_numeric(data["XPOS"], errors="coerce")
        y_num = pd.to_numeric(data["YPOS"], errors="coerce")
        mask = x_num.notna() & y_num.notna()
        xs = [int(v) for v in x_num[mask].round().tolist()]
        ys = [int(v) for v in y_num[mask].round().tolist()]
        bins = [fmt_type(v) for v in data.loc[mask, "BIN"].tolist()]

        dies = [{"x": x, "y": y, "bin": b} for x, y, b in zip(xs, ys, bins)]
        bin_counter = Counter(bins)

        total = len(dies)
        pass_first = sorted(
            bin_counter.items(),
            key=lambda kv: (0, bin_sort_key(kv[0])) if kv[0] == PASS_BIN
            else (1, -kv[1], bin_sort_key(kv[0])),
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

        if frame is not None:
            x_min, x_max = frame["x_min"], frame["x_max"]
            y_min, y_max = frame["y_min"], frame["y_max"]
        else:
            x_min = min(xs) if xs else None
            x_max = max(xs) if xs else None
            y_min = min(ys) if ys else None
            y_max = max(ys) if ys else None

        rows.append({
            "source": table.source,
            "file_name": table.file_name,
            "total": total,
            "x_min": x_min,
            "x_max": x_max,
            "y_min": y_min,
            "y_max": y_max,
            "dies": dies,
            "bin_counts": bin_counts,
        })
    return rows

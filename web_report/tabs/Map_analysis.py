"""Map Analysis (wafer map) tab payload builder.

소스별로 die 리스트({x, y, bin})와 격자 범위, bin 별 집계를 만든다.
BIN == PASS_BIN("1") 이 Pass, 그 외는 Fail.
STEP 이 2종 이상이면 step 당 맵 1개로 분리한다 — 테스트 순서는 P1→P2→P3 이고
앞 step 에서 Fail 된 칩은 뒤 step 을 테스트하지 않으므로, FAILTNO→항목 TNO→항목
STEP 으로 fail step 을 찾아 그 이전 step 맵에는 Pass, 해당 step 맵에는 실제 BIN,
이후 step 맵에는 미표시(빈칸)로 그린다. fail step 불명(BIN≠1 인데 FAILTNO
없음/미매칭)은 첫 step 맵에 Fail 표시 (2026-07-09 사용자 확정).
규칙 #6: die 는 전량 표현한다 (다운샘플링 금지).
"""
from __future__ import annotations

import re
from collections import Counter

import pandas as pd

from .common import PASS_BIN, bin_sort_key, bin_types, fmt_type
from .yield_tab import _tno_norm, failtno_norms
from ..wafer_frame import frame_for


def _step_sort_key(step):
    # P1 < P2 < P3 < P10 자연 정렬 (접두 문자 + 숫자). 패턴 밖 값은 문자열 사전순.
    m = re.fullmatch(r"(\D*)(\d+)", step)
    if m:
        return (m.group(1), int(m.group(2)))
    return (step, -1)


def _bin_count_rows(bins):
    bin_counter = Counter(bins)
    total = len(bins)
    pass_first = sorted(
        bin_counter.items(),
        key=lambda kv: (0, bin_sort_key(kv[0])) if kv[0] == PASS_BIN
        else (1, -kv[1], bin_sort_key(kv[0])),
    )
    return [
        {
            "bin": bin_value,
            "count": count,
            "pct": round(count / total * 100.0, 2) if total else 0.0,
            "is_pass": bin_value == PASS_BIN,
        }
        for bin_value, count in pass_first
    ]


def _fail_step_indexes(table, bins, mask, step_index):
    """유효 좌표 칩별 fail step index 리스트. None = 전 step Pass.

    FAILTNO 를 항목 TNO 에 매칭해 그 항목의 STEP index 를 얻는다 (yield_tab 의
    FAILTNO→항목 귀속과 동일 방식). 같은 TNO 가 여러 step 항목에 걸리면 가장
    이른 step. fail 판정은 BIN 기준, fail step 판정은 FAILTNO 기준.
    """
    tno_to_idx = {}
    for item, tno in table.tno.items():
        norm = _tno_norm(tno)
        if norm is None:
            continue
        idx = step_index.get(fmt_type(table.step.get(item)))
        if idx is None:
            continue
        if norm not in tno_to_idx or idx < tno_to_idx[norm]:
            tno_to_idx[norm] = idx

    # 전체 FAILTNO 변환(캐시)에서 유효 좌표 행만 추린다 — data.loc[mask] 재변환과 동일 결과
    fails = [f for f, m in zip(failtno_norms(table), mask.tolist()) if m]
    out = []
    for b, f in zip(bins, fails):
        if b == PASS_BIN:
            out.append(None)
        elif f is not None and f in tno_to_idx:
            out.append(tno_to_idx[f])
        else:
            out.append(0)   # fail step 불명 → 첫 step 에서 Fail 처리
    return out


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
        bins = [b for b, m in zip(bin_types(table), mask.tolist()) if m]

        if frame is not None:
            x_min, x_max = frame["x_min"], frame["x_max"]
            y_min, y_max = frame["y_min"], frame["y_max"]
        else:
            # step 분리 맵도 전체 칩 좌표 기준 틀을 공유해 맵 크기가 서로 어긋나지 않게 한다.
            x_min = min(xs) if xs else None
            x_max = max(xs) if xs else None
            y_min = min(ys) if ys else None
            y_max = max(ys) if ys else None

        base = {
            "source": table.source,
            "file_name": table.file_name,
            "x_min": x_min,
            "x_max": x_max,
            "y_min": y_min,
            "y_max": y_max,
        }

        steps = sorted({fmt_type(v) for v in table.step.values() if fmt_type(v)},
                       key=_step_sort_key)
        if len(steps) <= 1:
            # STEP 단일(또는 없음) → 현행 그대로 소스당 맵 1개 (row 에 step 키 없음).
            dies = [{"x": x, "y": y, "bin": b} for x, y, b in zip(xs, ys, bins)]
            rows.append(dict(base, total=len(dies), dies=dies,
                             bin_counts=_bin_count_rows(bins)))
            continue

        # STEP 2종 이상 → step 당 맵 1개. fail step 이전 = Pass, 해당 step = 실제 BIN,
        # 이후 = 미표시(테스트 안 함 → 빈칸).
        step_index = {s: i for i, s in enumerate(steps)}
        fail_idx = _fail_step_indexes(table, bins, mask, step_index)
        for k, step_name in enumerate(steps):
            dies = []
            for x, y, b, fi in zip(xs, ys, bins, fail_idx):
                if fi is None or fi > k:
                    dies.append({"x": x, "y": y, "bin": PASS_BIN})
                elif fi == k:
                    dies.append({"x": x, "y": y, "bin": b})
            rows.append(dict(base, step=step_name, total=len(dies), dies=dies,
                             bin_counts=_bin_count_rows([d["bin"] for d in dies])))
    return rows

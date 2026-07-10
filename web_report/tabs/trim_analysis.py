"""Trim Analysis tab payload builder (lazy 전용 — /full payload 에는 싣지 않는다).

- ``build_trim_payload``: 선택 source 항목명 매칭(web_report.trim_match) + 그룹 슬롯별
  통계(cpk._stats 재사용) + initial shift 판정. ``GET .../web_report/trim_analysis`` 가 소비.
- ``build_trim_chart``: 그룹 1개의 chip-to-chip 차트 데이터 — base 슬롯(PMIC4=INIT,
  TV2=TRIM; 없으면 target) 실측값 오름차순으로 **서버 정렬**해 전 die 를 반환.
  다운샘플 없음(불변 규칙 #6). ``GET .../web_report/trim_chart`` 가 소비.

initial shift 판정 (규칙셋별 base 분포 vs target 평균):
- base 표본 n < SHIFT_MIN_N 또는 target 평균 없음 → 판정 불가(eligible=False)
- p20/p80 은 pos=q*(n-1) 선형보간, span=p80-p20
- span>0: position=(target_mean-p20)/span, shift = target_mean 이 밴드 밖
- span==0: target_mean==p20 → shift=False(position=0.5 중앙), 아니면 shift=True
  (position 은 target_mean>p80 이면 1.0, 아니면 0.0)
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..trim_match import BASE_TARGET, phases_for, rule_set_for, build_groups
from .common import fmt_type, json_safe, round_num
from .cpk import CPK_THRESHOLD, _stats
from .distribution import to_numeric_clean

SHIFT_BAND_LO = 0.2
SHIFT_BAND_HI = 0.8
SHIFT_MIN_N = 5


def _percentile(xs, q):
    """정렬된 값 배열의 q 분위수 — pos=q*(n-1) 선형보간 (스펙 고정식, np.percentile 미사용).

    표본 1개면 그 값, pos 가 마지막 인덱스를 넘으면 마지막 값."""
    n = len(xs)
    if n == 0:
        return None
    if n == 1:
        return float(xs[0])
    pos = q * (n - 1)
    lo_i = int(math.floor(pos))
    if lo_i >= n - 1:
        return float(xs[-1])
    frac = pos - lo_i
    return float(xs[lo_i] + (xs[lo_i + 1] - xs[lo_i]) * frac)


def _initial_shift(base_values, target_mean) -> dict:
    """base 실측 분포 p20~p80 밴드 vs target 평균 위치 → shift 판정."""
    n = int(base_values.size)
    out = {"eligible": False, "reason": None, "n_base": n, "p20": None, "p80": None,
           "span": None, "target_mean": round_num(target_mean, 6),
           "position": None, "is_shift": None}
    if target_mean is None:
        out["reason"] = "no_target_mean"
        return out
    if n < SHIFT_MIN_N:
        out["reason"] = "base_n_lt_min"
        return out
    xs = np.sort(base_values)
    p20 = _percentile(xs, SHIFT_BAND_LO)
    p80 = _percentile(xs, SHIFT_BAND_HI)
    span = p80 - p20
    if span == 0:
        if target_mean == p20:
            shift, position = False, 0.5
        else:
            shift = True
            position = 1.0 if target_mean > p80 else 0.0
    else:
        position = (target_mean - p20) / span
        shift = (target_mean < p20) or (target_mean > p80)
    out.update({"eligible": True, "p20": round_num(p20, 6), "p80": round_num(p80, 6),
                "span": round_num(span, 6), "position": round_num(position, 4),
                "is_shift": bool(shift)})
    return out


def _select_table(tables, source):
    """source 이름으로 테이블 선택 — "" 이면 첫 테이블. 없으면 KeyError(라우트 404)."""
    if not tables:
        raise KeyError("no sources")
    if not source:
        return tables[0]
    for table in tables:
        if table.source == source:
            return table
    raise KeyError(source)


def _slot_cpk_warn(slot_stats) -> bool:
    """그룹 슬롯 항목들 cpk 중 최저값 < CPK_THRESHOLD (전부 None 이면 False)."""
    cpks = [st["cpk"] for st in slot_stats.values() if st and st["cpk"] is not None]
    return bool(cpks) and min(cpks) < CPK_THRESHOLD


def build_trim_payload(tables, source, overrides, product_type) -> dict:
    """탭 진입 payload — 매칭 결과(items/groups) + 그룹 통계/shift/cpk_warn."""
    table = _select_table(tables, source)
    rule_set = rule_set_for(product_type)
    match = build_groups(table.item_columns, overrides=overrides,
                         rule_set=rule_set, product_type=product_type)
    base, target = match["base"], match["target"]

    items = []
    for info in match["items"]:
        name = info["name"]
        items.append({**info,
                      "tno": fmt_type(table.tno.get(name)),
                      "step": fmt_type(table.step.get(name)),
                      "units": json_safe(table.units.get(name)) or "",
                      "lo": round_num(table.lolim.get(name)),
                      "hi": round_num(table.hilim.get(name))})

    groups = []
    for group in match["groups"]:
        stats = {}
        for phase, item in group["slots"].items():
            stats[phase] = _stats(table.data[item], table.lolim.get(item),
                                  table.hilim.get(item)) if item else None
        base_item = group["slots"].get(base)
        target_stat = stats.get(target)
        target_mean = target_stat["average"] if target_stat else None
        base_values = (to_numeric_clean(table.data[base_item])
                       if base_item else np.empty(0))
        groups.append({**group, "stats": stats,
                       "shift": _initial_shift(base_values, target_mean),
                       "cpk_warn": _slot_cpk_warn(stats)})

    return {
        "rule_set": rule_set,
        "phases": match["phases"],
        "base": base,
        "target": target,
        "source": table.source,
        "sources": [{"name": t.source, "file_name": t.file_name} for t in tables],
        "constants": {"cpk_threshold": CPK_THRESHOLD,
                      "shift_band": [SHIFT_BAND_LO, SHIFT_BAND_HI],
                      "shift_min_n": SHIFT_MIN_N},
        "items": items,
        "groups": groups,
        "overrides": dict(overrides or {}),
        "invalid_overrides": match["invalid_overrides"],
    }


def build_trim_chart(table, group: dict, rule_set: str) -> dict:
    """그룹 1개의 chip-to-chip 차트 payload (전 die, 서버 정렬, 다운샘플 없음).

    chip 순번 x 는 배열 인덱스 — base(없으면 target, 둘 다 없으면 첫 슬롯) 실측값
    오름차순이며 NaN chip 은 맨뒤에 배치한다(제외하지 않음). 각 phase 의 y 배열은
    동일 chip 순서로 정렬되고 결측은 null(Plotly gap 처리).
    """
    base, target = BASE_TARGET[rule_set]
    slots = group["slots"]
    order_slot = None
    for phase in (base, target, *phases_for(rule_set)):
        if slots.get(phase):
            order_slot = phase
            break
    if order_slot is None:
        raise KeyError(str(group.get("id")))

    numeric = {phase: pd.to_numeric(table.data[item], errors="coerce").to_numpy(dtype=float)
               for phase, item in slots.items() if item}
    order = np.argsort(numeric[order_slot], kind="stable")   # float NaN 은 정렬 맨뒤

    data = table.data
    meta = {c: [fmt_type(v) for v in data[c].to_numpy()[order]]
            for c in ("SERIAL", "XPOS", "YPOS")}

    phases_out = {}
    slot_stats = {}
    for phase, arr in numeric.items():
        item = slots[phase]
        arr_sorted = np.round(arr[order], 6)
        phases_out[phase] = {
            "item": item,
            "units": json_safe(table.units.get(item)) or "",
            "lo": round_num(table.lolim.get(item)),
            "hi": round_num(table.hilim.get(item)),
            "y": [float(v) if math.isfinite(v) else None for v in arr_sorted],
        }
        slot_stats[phase] = _stats(data[item], table.lolim.get(item), table.hilim.get(item))

    base_band = None
    base_item = slots.get(base)
    if base_item:
        xs = np.sort(to_numeric_clean(data[base_item]))
        if xs.size:
            base_band = {"p20": round_num(_percentile(xs, SHIFT_BAND_LO), 6),
                         "p80": round_num(_percentile(xs, SHIFT_BAND_HI), 6)}
    target_stat = slot_stats.get(target)
    target_mean = target_stat["average"] if target_stat else None

    return {
        "group": group["id"],
        "source": table.source,
        "rule_set": rule_set,
        "base": base,
        "target": target,
        "order_by": order_slot,
        "n": int(order.size),
        "serial": meta["SERIAL"],
        "xpos": meta["XPOS"],
        "ypos": meta["YPOS"],
        "phases": phases_out,
        "base_band": base_band,
        "target_mean": round_num(target_mean, 6),
        "cpk_warn": _slot_cpk_warn(slot_stats),
    }

"""Distribution tab payload builder.

두 계층을 제공한다:
- ECDF 컴팩트(``build_distribution_compact``): lazy 엔드포인트 ``GET .../web_report/distribution``
  전용 columnar 페이로드. 갤러리 미니셀 + Issue Table 산포 카드가 ``distDataCache`` 로 소비한다.
  다운샘플 없음(불변 규칙 #6).
- 산포 탭 인덱스/상세(``build_distribution_index`` / ``fail_items`` / ``scatter_item``): 갤러리
  카드의 status/cpk 분류와, 카드 클릭 시 지연 로드하는 항목별 전체 측정값(상세 CDF+히스토그램)을
  공급한다. cpk 는 이미 계산된 ``cpk_rows`` 를 재사용하고, fail 은 FAILTNO==항목 TNO 로 귀속한다.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

from .common import fmt_type, json_safe, round_num
from .cpk import _stats
from .yield_tab import _tno_norm

CPK_THRESHOLD = 1.33


def to_numeric_clean(series):
    """Series → float64 배열 (유한값만, NaN·inf 제거)."""
    arr = pd.to_numeric(series, errors="coerce")
    return arr[np.isfinite(arr)].to_numpy()


def cumulative_distribution_full(values):
    """고유값별 누적 분포(ECDF) 계산. 반환: (unique_vals, cumulative_percent)."""
    if values.size == 0:
        return np.empty(0), np.empty(0)
    unique_vals, counts = np.unique(np.sort(values), return_counts=True)
    cum = np.cumsum(counts) / values.size * 100.0
    return unique_vals, cum


def build_distribution_compact(tables, all_items) -> dict:
    """ECDF 전량(다운샘플 없음, 불변 규칙 #6)을 columnar 포맷으로 반환.

    행마다 반복되던 subject/source/units/limits 키를 제거한 컴팩트 표현으로,
    lazy 엔드포인트 ``GET .../web_report/distribution`` 전용이다 (208MB → 수십 MB).
    """
    items = {}
    for item in all_items:
        sources = {}
        units = ""
        lo = hi = None
        first = True
        for table in tables:
            if item not in table.item_columns:
                continue
            if first:
                units = json_safe(table.units.get(item)) or ""
                lo = round_num(table.lolim.get(item))
                hi = round_num(table.hilim.get(item))
                first = False
            values = to_numeric_clean(table.data[item])
            unique_vals, cum = cumulative_distribution_full(values)
            sources[table.source] = {
                "x": [round_num(v) for v in unique_vals],
                "y": [round_num(p, 3) for p in cum],
            }
        if sources:
            items[item] = {"units": units, "lo": lo, "hi": hi, "sources": sources}
    return {"format": "ecdf-columnar-v1", "items": items}


def fail_items(tables) -> set:
    """FAILTNO 가 항목의 TNO 와 일치하는 칩이 하나라도 있으면 그 항목을 fail 로 본다.

    yield_tab 의 fail 귀속(``FAILTNO`` == ``TNO``)과 동일한 규칙을 재사용한다. 소스 합집합.
    (칩 자체의 fail 정의는 FAILTNO 존재 또는 BIN≠1 이지만, 특정 항목에 귀속되는 것은 FAILTNO 뿐.)
    """
    failed: set = set()
    for table in tables:
        tno_to_item = defaultdict(list)
        for item, tno in table.tno.items():
            norm = _tno_norm(tno)
            if norm is not None:
                tno_to_item[norm].append(item)
        for value in table.data["FAILTNO"].tolist():
            norm = _tno_norm(value)
            if norm is None:
                continue
            for item in tno_to_item.get(norm, []):
                failed.add(item)
    return failed


def _worst_cpk(cpk_rows) -> dict:
    """subject 별 소스 최저(worst-case) cpk (None 제외)."""
    worst: dict = {}
    for r in cpk_rows or []:
        cpk = r.get("cpk")
        if cpk is None:
            continue
        subject = r.get("subject")
        if subject not in worst or cpk < worst[subject]:
            worst[subject] = cpk
    return worst


def _status(is_fail, cpk) -> str:
    if is_fail:
        return "fail"
    if cpk is not None and cpk < CPK_THRESHOLD:
        return "cpk_low"
    return "ok"


def _first_table_for(tables, item):
    for table in tables:
        if item in table.item_columns:
            return table
    return None


def build_distribution_index(tables, cpk_rows) -> list:
    """갤러리/툴바/타입어헤드용 항목 인덱스. subject 당 1행 (경량, 점 배열 없음).

    cpk 는 ``cpk_rows`` 재사용(재계산 없음), fail 은 ``fail_items`` 로 귀속.
    """
    worst = _worst_cpk(cpk_rows)
    failed = fail_items(tables)
    all_items = sorted({c for t in tables for c in t.item_columns})

    rows = []
    for item in all_items:
        meta_t = _first_table_for(tables, item)
        n = 0
        for table in tables:
            if item in table.item_columns:
                n += int(to_numeric_clean(table.data[item]).size)
        cpk = worst.get(item)
        is_fail = item in failed
        rows.append({
            "subject": item,
            "test_num": fmt_type(meta_t.tno.get(item)) if meta_t else "",
            "units": (json_safe(meta_t.units.get(item)) if meta_t else None) or "",
            "lower_limit": round_num(meta_t.lolim.get(item)) if meta_t else None,
            "upper_limit": round_num(meta_t.hilim.get(item)) if meta_t else None,
            "n": n,
            "cpk": round_num(cpk, 3),
            "is_fail": is_fail,
            "status": _status(is_fail, cpk),
        })
    return rows


def scatter_item(tables, subject) -> dict:
    """상세용: 항목의 소스별 전체 측정값(다운샘플 없음) + cpk/status.

    항목이 어떤 소스에도 없으면 ``KeyError`` (라우트가 404 처리).
    """
    matched = [t for t in tables if subject in t.item_columns]
    if not matched:
        raise KeyError(subject)
    meta_t = matched[0]

    sources = []
    cpks = []
    for table in matched:
        values = to_numeric_clean(table.data[subject])
        sources.append({"name": table.source, "values": values.round(6).tolist()})
        st = _stats(table.data[subject], table.lolim.get(subject), table.hilim.get(subject))
        if st["cpk"] is not None:
            cpks.append(st["cpk"])

    cpk = min(cpks) if cpks else None
    is_fail = subject in fail_items(matched)
    return {
        "subject": subject,
        "units": json_safe(meta_t.units.get(subject)) or "",
        "lower_limit": round_num(meta_t.lolim.get(subject)),
        "upper_limit": round_num(meta_t.hilim.get(subject)),
        "cpk": round_num(cpk, 3),
        "status": _status(is_fail, cpk),
        "sources": sources,
    }

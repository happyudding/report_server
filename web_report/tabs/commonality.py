"""Commonality 모드 payload 빌더 — 사용자가 고른 chip 을 wafer 위치로 강조하고,
각 항목 분포(ECDF)에서 그 chip 이 어느 누적 %에 있는지 계산한다.

Commonality 모드는 입력 source 1개일 때만 허용된다(클라 제약). 여기 함수들은 그래도
여러 table 이 와도 안전하게 순회한다(첫 매칭 chip 사용).

chip 선택은 프런트가 raw data 표에서 serial/xpos/ypos 로 검색·행 선택한다:
  - search_chips: 검색어(serial/xpos/ypos/dut 부분일치)로 chip 후보 목록.
  - chip_percentiles: 선택 chip 의 항목별 값 + 누적%(ECDF 위치) — 프런트가 분포를
    그 누적% 기준으로 색 분리하고 wafer map 에 위치를 강조하는 데 쓴다.
다운샘플 없음(규칙 #6) — 누적%는 전체 die 대상으로 계산한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import fmt_type, json_safe, round_num
from .distribution import to_numeric_clean
from ..honeyform import META_COLUMNS as _META

_SEARCH_COLS = ("SERIAL", "XPOS", "YPOS", "DUT")


def _meta_lists(data):
    return {c: [fmt_type(v) for v in data[c].tolist()] for c in _META}


def _index_entry(table, *, with_sorted=True):
    """테이블 1개의 사전 계산 엔트리: 메타 리스트(+검색용 소문자본) [+ item별 정렬 numeric 배열]."""
    meta = _meta_lists(table.data)
    entry = {
        "meta": meta,
        "meta_lower": {c: [v.lower() for v in meta[c]] for c in _SEARCH_COLS},
    }
    if with_sorted:
        entry["sorted"] = {
            item: np.sort(to_numeric_clean(table.data[item]))
            for item in table.item_columns
        }
    return entry


def build_index(tables) -> list:
    """세션 단위 사전 계산 인덱스 — service 가 (analysis_key, content_hash) 키로 캐시한다.

    chip 검색(키스트로크마다)·백분위 계산(chip 클릭마다)이 매번 전 컬럼을 fmt_type/
    to_numeric_clean 으로 재변환하지 않도록, 메타 리스트와 item별 정렬 배열을 미리 만든다.
    다운샘플 없음(규칙 #6) — 정렬 배열은 전체 유한값이며 누적%는 여기서 전량 기준으로 계산된다.
    """
    return [_index_entry(table) for table in tables]


def search_chips(tables, q="", limit=300, index=None) -> dict:
    """검색어로 chip 후보를 찾는다. serial/xpos/ypos/dut 부분일치(대소문자 무시).

    q 가 비면 앞에서부터 limit 개. 결과가 limit 을 넘으면 truncated=True.
    index 는 build_index 결과(없으면 즉석 계산 — 메타만).
    """
    q = str(q or "").strip().lower()
    out = []
    for ti, table in enumerate(tables):
        entry = index[ti] if index else _index_entry(table, with_sorted=False)
        meta, meta_lower = entry["meta"], entry["meta_lower"]
        n = len(meta["SERIAL"])
        for i in range(n):
            if q and not (
                q in meta_lower["SERIAL"][i]
                or q in meta_lower["XPOS"][i]
                or q in meta_lower["YPOS"][i]
                or q in meta_lower["DUT"][i]
            ):
                continue
            out.append({
                "source": table.source,
                "serial": meta["SERIAL"][i], "shot": meta["SHOT"][i],
                "dut": meta["DUT"][i], "xpos": meta["XPOS"][i], "ypos": meta["YPOS"][i],
                "bin": meta["BIN"][i],
            })
            if len(out) >= limit:
                return {"chips": out, "truncated": True}
    return {"chips": out, "truncated": False}


def _locate(meta, serial, xpos, ypos):
    """메타 리스트에서 (serial/xpos/ypos 부분조건) 에 맞는 첫 행 위치를 찾는다. 없으면 None."""
    ser_q = fmt_type(serial) if serial != "" else ""
    x_q = fmt_type(xpos) if xpos != "" else ""
    y_q = fmt_type(ypos) if ypos != "" else ""
    for i in range(len(meta["SERIAL"])):
        if ser_q and meta["SERIAL"][i] != ser_q:
            continue
        if x_q and meta["XPOS"][i] != x_q:
            continue
        if y_q and meta["YPOS"][i] != y_q:
            continue
        return i
    return None


def chip_percentiles(tables, *, serial="", xpos="", ypos="", source="", index=None) -> dict:
    """선택 chip 의 항목별 값 + 누적%(ECDF 위치)를 계산한다.

    누적% = (chip 값 이하인 die 수 / 유효 die 수) × 100. 항목 값이 없거나(비수치)
    분포가 비면 value/cum_pct=None. chip 을 못 찾으면 KeyError(라우트가 404).
    index 는 build_index 결과(없으면 즉석 계산).
    """
    if index is None:
        index = build_index(tables)
    target = None
    for ti, table in enumerate(tables):
        if source and table.source != source:
            continue
        found = _locate(index[ti]["meta"], serial, xpos, ypos)
        if found is not None:
            target = (table, found, index[ti])
            break
    if target is None and not source:
        for ti, table in enumerate(tables):   # source 지정 없이 다시 전체 탐색(위 루프가 source 필터로 걸렀을 때 대비)
            found = _locate(index[ti]["meta"], serial, xpos, ypos)
            if found is not None:
                target = (table, found, index[ti])
                break
    if target is None:
        raise KeyError("chip not found")

    table, idx, entry = target
    meta = entry["meta"]
    data = table.data
    chip = {
        "source": table.source,
        "serial": meta["SERIAL"][idx], "shot": meta["SHOT"][idx], "dut": meta["DUT"][idx],
        "xpos": meta["XPOS"][idx], "ypos": meta["YPOS"][idx], "bin": meta["BIN"][idx],
    }
    try:
        chip["x"] = int(float(meta["XPOS"][idx]))
        chip["y"] = int(float(meta["YPOS"][idx]))
    except (TypeError, ValueError):
        chip["x"] = chip["y"] = None

    sorted_vals = entry.get("sorted") or {}
    items = []
    for item in sorted(table.item_columns):
        arr = sorted_vals.get(item)
        if arr is None:
            arr = np.sort(to_numeric_clean(data[item]))   # 유한값만
        raw = pd.to_numeric(pd.Series([data[item].iloc[idx]]), errors="coerce").iloc[0]
        n = int(arr.size)
        row = {
            "subject": item,
            "units": json_safe(table.units.get(item)) or "",
            "lower_limit": round_num(table.lolim.get(item)),
            "upper_limit": round_num(table.hilim.get(item)),
            "n": n,
            "value": None,
            "cum_pct": None,
        }
        if n and np.isfinite(raw):
            # 정렬 배열에서 searchsorted(right) == (chip 값 이하 die 수) — count_nonzero 와 동일
            cum = float(np.searchsorted(arr, raw, side="right")) / n * 100.0
            row["value"] = round_num(float(raw))
            row["cum_pct"] = round(cum, 2)
        items.append(row)

    return {"chip": chip, "items": items}

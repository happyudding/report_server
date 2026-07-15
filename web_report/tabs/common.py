"""Shared helpers for web_report tab builders."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

PASS_BIN = "1"

# Pass/Fail 단위 정규화 집합 — 공백·슬래시 제거 후 대문자로 비교하므로
# "P/F"·"PF"·"pF"·"Pf"·"PassFail"·"Pass/Fail" 등을 모두 포괄한다 (대소문자 무시).
_PASSFAIL_UNITS = {"PF", "PASSFAIL"}


def json_safe(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def fmt_type(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    try:
        f = float(value)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def num(value):
    try:
        if value is None:
            return None
        f = float(value)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def round_num(value, digits=6):
    value = num(value)
    return None if value is None else round(value, digits)


def bin_sort_key(value):
    """BIN 정렬 키: 숫자로 해석 가능하면 숫자 우선, 아니면 문자열 사전순."""
    text = str(value)
    try:
        return (0, float(text))
    except ValueError:
        return (1, text)


def to_coord(x, y):
    """(XPOS, YPOS) 값 쌍 → (int, int) die 좌표. 결측("")/비수치는 None.

    compare(공통 map/bin transition)·commonality 가 같은 규칙으로 좌표를 정규화한다."""
    if x == "" or y == "":
        return None
    try:
        return (int(float(x)), int(float(y)))
    except (TypeError, ValueError):
        return None


def item_meta(tables) -> dict:
    """item → {"step", "tno"}(fmt_type 적용) 맵 — yield/issue_table 공용.

    키는 원본 전체 메타(table.step) 기준 — selected_items 로 축소된 item_columns 가
    아니라 전체 item 의 STEP/TNO 를 참조한다. tno_to_item_map(fail 집계)이 전체
    table.tno 를 쓰므로, 미선택 fail 항목의 STEP/TNO 귀속을 일치시킨다
    (불일치 시 미선택 fail 행이 빈 STEP 그룹으로 떨어지는 버그 방지).
    항목이 여러 테이블에 있으면 첫 테이블 값 우선 (setdefault).
    """
    out = {}
    for table in tables or []:
        for item in table.step:      # 전체 메타 키 (tno.keys() 와 동일 집합)
            out.setdefault(item, {
                "step": fmt_type(table.step.get(item)),
                "tno": fmt_type(table.tno.get(item)),
            })
    return out


def bin_types(table) -> list:
    """table.data["BIN"] 전체의 fmt_type 변환 리스트 — 테이블 인스턴스 단위 lazy 캐시.

    한 요청에서 yield/cpk/compare/map 빌더가 같은 BIN 컬럼을 각자 재변환하지 않도록
    HoneyformTable 인스턴스에 결과를 붙여 재사용한다. tables 는 요청마다 새 클론이므로
    (loader.clone_table) 캐시 무효화가 필요 없다.
    """
    cached = getattr(table, "_bin_types_cache", None)
    if cached is None:
        cached = [fmt_type(v) for v in table.data["BIN"].tolist()]
        table._bin_types_cache = cached
    return cached


def _is_passfail_unit(unit) -> bool:
    """unit 이 Pass/Fail 계열(P/F·PF·PassFail 등)인지 판정 (대소문자 무시)."""
    if unit is None:
        return False
    try:
        if pd.isna(unit):
            return False
    except (TypeError, ValueError):
        pass
    norm = str(unit).strip().upper().replace(" ", "").replace("/", "")
    return norm in _PASSFAIL_UNITS


def _item_has_data(tables, item) -> bool:
    """item 의 data 부분(측정 행)에 유한 numeric 값이 한 소스라도 있으면 True.

    to_numeric_clean 과 동일한 유한값 기준(np.isfinite)을 쓴다 — 전부 NaN/비수치이면
    측정값이 하나도 없는 것으로 본다."""
    for t in tables:
        if item not in t.item_columns:
            continue
        col = t.data[item]
        if getattr(col.dtype, "kind", "") in "if":
            arr = col.to_numpy()
        else:
            arr = pd.to_numeric(col, errors="coerce").to_numpy()
        if np.isfinite(arr).any():
            return True
    return False


def passfail_or_empty_items(tables) -> set:
    """cpk·distribution 계산에서 제외할 item 집합.

    다음 중 하나라도 해당하면 제외한다:
    - unit 이 Pass/Fail 계열(P/F·PF·PassFail, 대소문자 무시) — ``_is_passfail_unit``.
    - 모든 소스의 data 부분에 유한 numeric 측정값이 하나도 없음 — ``_item_has_data``.

    unit 은 항목이 처음 등장하는 테이블 기준(표시 unit 규칙과 동일)으로 판정한다.
    """
    excluded: set = set()
    for item in {c for t in tables for c in t.item_columns}:
        passfail = False
        for t in tables:
            if item in t.item_columns:
                passfail = _is_passfail_unit(t.units.get(item))
                break
        if passfail or not _item_has_data(tables, item):
            excluded.add(item)
    return excluded


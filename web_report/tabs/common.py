"""Shared helpers for web_report tab builders."""
from __future__ import annotations

import math

import pandas as pd

PASS_BIN = "1"


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

    항목이 여러 테이블에 있으면 첫 테이블 값 우선 (setdefault).
    """
    out = {}
    for table in tables or []:
        for item in table.item_columns:
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


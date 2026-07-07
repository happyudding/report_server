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


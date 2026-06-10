"""distribution / histogram 차트 공용 헬퍼 — x축 범위 계산 + 시트 제목 배너.

차트 종류(ECDF/히스토그램)와 무관한 순수 계산/레이아웃 헬퍼만 모은다.
_xlsx_distribution_chart(J) 와 _xlsx_histogram_chart(M) 양쪽에서 import.
"""
from __future__ import annotations

import math

from ._xlsx_style import _TITLE_ROW_MAX_COL, _XL_CENTER, _XL_LEFT

# 제목 배너 서식 (차트 시트 1행)
_TITLE_FILL = (191, 227, 255)
_TITLE_FONT_SIZE = 20
_TITLE_ROW_HEIGHT = 30


# ── x축 범위 계산 헬퍼 ───────────────────────────────────────────────────────

def _isnum(v):
    if v is None:
        return False
    try:
        f = float(v)
        return not math.isnan(f) and not math.isinf(f)
    except (TypeError, ValueError):
        return False


def _fmt_lim(v):
    """limit 표시값: nan/None → '-', 정수 → int, 그 외 → 간결한 실수."""
    if not _isnum(v):
        return "-"
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _limit_caption(d):
    """차트 item 명 아래 줄: '(LO ~ HI units)'."""
    unit = (d.unit or "").strip()
    body = f"{_fmt_lim(d.lower_limit)} ~ {_fmt_lim(d.upper_limit)}"
    return f"({body} {unit})" if unit else f"({body})"


def _decimals(v):
    """숫자의 유효 소수 자릿수 (정수면 0)."""
    if v is None:
        return 0
    s = repr(float(v))
    if "e" in s or "E" in s or "." not in s:
        return 0
    return len(s.split(".")[1].rstrip("0"))


def _floor_dec(x, dec):
    f = 10 ** dec
    return math.floor(x * f) / f


def _ceil_dec(x, dec):
    f = 10 ** dec
    return math.ceil(x * f) / f


def _x_axis_range(lo, hi, dmin, dmax, is_fail, dmed):
    """x축 [min,max].
    - 양쪽 LIM 있음/없음: 기존 규칙(Pass=LIM 그대로, Fail=±5% 가드밴드 후 LIM 자릿수로
      floor/ceil; LIM None/nan 이면 data min/max).
    - 한쪽만 있음: 열린 쪽 끝을 median 대칭(median±(median-LIM))으로, 닫힌 쪽은 기존
      규칙. 데이터를 잘라내지 않도록 clamp(규칙 #6)."""
    lo_n = float(lo) if _isnum(lo) else None
    hi_n = float(hi) if _isnum(hi) else None

    # one-sided spec: 열린 쪽 축 끝을 median 대칭으로 확장
    if (lo_n is None) != (hi_n is None) and _isnum(dmed):
        if lo_n is not None:                 # LSL-only → 위쪽(max) 열림
            dec = _decimals(lo_n)
            xmin = lo_n
            if is_fail and dmin < lo_n:
                xmin = dmin - (lo_n - dmin) * 0.05
            xmax = max(dmed + (dmed - lo_n), dmax)   # clamp: 데이터 전부 포함
            return _floor_dec(xmin, dec), _ceil_dec(xmax, dec)
        else:                                # USL-only → 아래쪽(min) 열림
            dec = _decimals(hi_n)
            xmax = hi_n
            if is_fail and dmax > hi_n:
                xmax = dmax + (dmax - hi_n) * 0.05
            xmin = min(dmed - (hi_n - dmed), dmin)   # clamp: 데이터 전부 포함
            return _floor_dec(xmin, dec), _ceil_dec(xmax, dec)

    xmin = lo_n if lo_n is not None else dmin
    xmax = hi_n if hi_n is not None else dmax
    if not is_fail:
        return xmin, xmax
    if lo_n is not None and dmin < lo_n:
        xmin = dmin - (lo_n - dmin) * 0.05
    if hi_n is not None and dmax > hi_n:
        xmax = dmax + (dmax - hi_n) * 0.05
    dec = max(_decimals(lo_n), _decimals(hi_n))
    return _floor_dec(xmin, dec), _ceil_dec(xmax, dec)


# ── 차트 시트 제목 배너 (xlwings) ────────────────────────────────────────────

def _put_title(sh, ncols, text):
    """1행에 시트 제목 배너 — 하늘색 배경, 검은 bold, 큰 글씨, 가로 병합."""
    span = max(_TITLE_ROW_MAX_COL, ncols)
    try:
        sh.range((1, 1), (1, span)).merge()
    except Exception:
        pass
    c = sh.range((1, 1))
    c.value = text
    try:
        c.color = _TITLE_FILL
        f = c.api.Font
        f.Bold = True
        f.Size = _TITLE_FONT_SIZE
        f.Color = 0  # black
        c.api.HorizontalAlignment = _XL_LEFT
        c.api.VerticalAlignment = _XL_CENTER
        sh.range((1, 1), (1, span)).color = _TITLE_FILL
    except Exception:
        pass


def _finalize_title_row(sh):
    """제목 행 높이 보정."""
    try:
        sh.range((1, 1)).row_height = _TITLE_ROW_HEIGHT
    except Exception:
        pass

"""xlsx 출력 공용 스타일/레이아웃 상수 + Range 단위 스타일 헬퍼.

색=ARGB 8자리 문자열(_rgb_int 가 뒤 6자리 RRGGBB 만 사용), 폰트=dict{name,size,bold,color}.
스타일 변경 시 이 모듈 상단 상수만 수정하면 모든 시트/차트에 반영된다. 셀 단위 COM
왕복을 피하기 위해 스타일은 항상 Range 단위 1회 적용한다.
"""
from __future__ import annotations


def _col_letter(n):
    """1-based 열 인덱스 → 엑셀 열문자 ('A'..). openpyxl get_column_letter 대체."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


ALL_SHEETS = ["summary", "yield", "cpk", "fail_item", "issue_table", "distribution",
              "histogram"]

# ── 셀 스타일 상수 (색=ARGB 8자리 문자열, 폰트=dict{name,size,bold}) ─────────────
# 색상은 ARGB 8자리(뒤 6자리 RRGGBB 만 _rgb_int 가 사용). 스타일 변경 시 이 상수만 수정.
_HDR_FILL_RGB   = "FFD9E1F2"   # 헤더 연청색
_DATA_FILL_RGB  = "FFFFFFFF"   # 데이터 흰색
_TITLE_FILL_RGB = "FFBDD7EE"   # 제목 연파랑
_HDR_FONT    = {"name": "Calibri", "bold": False, "size": 11}
_DATA_FONT   = {"name": "Calibri", "size": 10}
_TITLE_FONT  = {"name": "Calibri", "bold": False, "size": 20}
_TITLE_ROW_MAX_COL = 26
_SUMMARY_TITLE_FILL_RGB = "FFBFE3FF"
_SUMMARY_HDR_FILL_RGB = "FFE2E8F0"
_SUMMARY_TITLE_FONT = {"name": "Tahoma", "bold": False, "size": 22}
_SUMMARY_SECTION_FONT = {"name": "Tahoma", "bold": False, "size": 20}
_SUMMARY_HDR_FONT = {"name": "Tahoma", "bold": False, "size": 10}
_SUMMARY_DATA_FONT = {"name": "Tahoma", "size": 10}

# ── Excel COM 상수 (서식) ─────────────────────────────────────────────────────
_XL_CENTER = -4108     # xlCenter (H/V align)
_XL_LEFT   = -4131     # xlLeft
_XL_CALC_MANUAL = -4135
_XL_CALC_AUTO   = -4105
_XL_BORDERS = (7, 8, 9, 10, 11, 12)   # xlEdge* + xlInsideVertical/Horizontal
_XL_CONTINUOUS = 1     # xlContinuous
_XL_THIN = 2           # xlThin (Borders.Weight)

# ── table 시트의 표 시작 위치 + 행높이/열너비 레이아웃 상수 ────────────────────
# (A열 비움, 제목 A1, 헤더 3행, 데이터 4행~)
_HEADER_ROW = 3
_START_COL = 2  # B열
_FAIL_ITEM_ROW_HEIGHT = 135   # fail_item 데이터 행 높이(pt) — Distribution 차트 셀 맞춤
_ISSUE_TABLE_ROW_HEIGHT = 78  # issue_table 데이터 행 높이(pt) — Distribution 차트 셀 맞춤
_YIELD_TABLE_ROW_HEIGHT = 22
_YIELD_HEADER_ROW_HEIGHT = 40
_NARROW_COL_WIDTH = 6.5    # bin / count / yield / avg / comment 등 짧은 데이터
_DIST_COL_WIDTH   = 27.1   # Distribution 열 (썸네일 이미지 크기 기준)
_ITEM_COL_WIDTH   = 20.0   # Item / Category 열 (긴 텍스트)
_CPK_TEST_NAME_COL_WIDTH = 60
_CPK_SERIES_COL_WIDTH = 15
_CPK_N_COL_WIDTH = _NARROW_COL_WIDTH * 1.05
_FAIL_VALUES_COLS  = ["DUT", "XCoord", "YCoord", "Bin", "Item", "Value"]
_FAIL_VALUES_NCOLS = 6     # source 블록당 열 수
_FAIL_VALUES_GAP   = 1     # source 블록 간 빈 열 수


def _rgb_int(argb):
    """ARGB 8자리(또는 RRGGBB) → Excel COM 색 정수 (R + G*256 + B*65536)."""
    h = str(argb)[-6:]
    return int(h[0:2], 16) + int(h[2:4], 16) * 256 + int(h[4:6], 16) * 65536


def _apply_font(api, font):
    """COM Range/Cell .api 에 폰트 dict 적용."""
    if not font:
        return
    if font.get("name"):
        api.Font.Name = font["name"]
    if font.get("size") is not None:
        api.Font.Size = font["size"]
    if font.get("bold") is not None:
        api.Font.Bold = bool(font["bold"])
    if font.get("color") is not None:
        api.Font.Color = _rgb_int(font["color"])


def _style_range(rng, *, fill=None, font=None, halign=None, valign=None,
                 wrap=None, border=False):
    """xlwings Range 에 스타일을 **범위 단위 1회** 적용 (셀 단위 COM 왕복 회피)."""
    api = rng.api
    if fill is not None:
        api.Interior.Color = _rgb_int(fill)
    _apply_font(api, font)
    if halign is not None:
        api.HorizontalAlignment = halign
    if valign is not None:
        api.VerticalAlignment = valign
    if wrap is not None:
        api.WrapText = wrap
    if border:
        for e in _XL_BORDERS:
            b = api.Borders(e)
            b.LineStyle = _XL_CONTINUOUS
            b.Weight = _XL_THIN


def _rng(ws, r1, c1, r2=None, c2=None):
    """(r1,c1)[~(r2,c2)] → xlwings Range. 단일 셀이면 r2/c2 생략."""
    if r2 is None:
        return ws.range((r1, c1))
    return ws.range((r1, c1), (r2, c2))


def _hdr_range(ws, r, c1, c2):
    """헤더 스타일(연청색 fill, _HDR_FONT, 중앙, wrap, thin border)을 범위 1회 적용."""
    rng = ws.range((r, c1), (r, c2))
    _style_range(rng, fill=_HDR_FILL_RGB, font=_HDR_FONT, halign=_XL_CENTER,
                 valign=_XL_CENTER, wrap=True, border=True)
    return rng


def _data_range(ws, r1, c1, r2, c2):
    """데이터 스타일(흰 fill, _DATA_FONT, 중앙, wrap, thin border)을 범위 1회 적용."""
    rng = ws.range((r1, c1), (r2, c2))
    _style_range(rng, fill=_DATA_FILL_RGB, font=_DATA_FONT, halign=_XL_CENTER,
                 valign=_XL_CENTER, wrap=True, border=True)
    return rng

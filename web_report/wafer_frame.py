"""제품별 wafer 기준정보 → 고정 map 프레임(die-index bounds) 계산.

Map Analysis 는 기본적으로 들어온 rawdata 의 XPOS/YPOS min/max 로 격자 틀을 잡는다
(그러면 부분 데이터가 전체 웨이퍼처럼 보임). 여기서 제품별 die pitch(+wafer 크기)가
입력돼 있으면 그 값으로 gross die 격자(고정 프레임)를 계산해 Map_analysis 가 틀을
덮어쓴다. **die pitch 가 없으면 프레임을 계산하지 않고(None) 현행 동작을 유지한다.**

기준정보는 사람이 채우는 정적 테이블(PRODUCT_WAFER_REF)이다. 세션별 UI 입력이 아니라
제품 단위 상수이며, 변경 시 서버 재시작(또는 report 캐시 evict) 후 반영된다.
"""
from __future__ import annotations

import math

# inch → wafer 지름(mm). 표준 명목치, 없으면 inch*25.4 로 근사.
_WAFER_MM = {4: 100.0, 5: 125.0, 6: 150.0, 8: 200.0, 12: 300.0}

_DEFAULT_WAFER_INCH = 8      # wafer 크기 미입력 시 기본 8인치
_DEFAULT_EDGE_MM = 0.0       # edge exclusion 미입력 시 0 (die 중심이 반지름 안이면 포함)
_DEFAULT_ORIGIN = "center"   # "center": x∈[-n,n] 대칭 / "corner": x∈[0,2n]

# 제품별 기준정보. die pitch(x,y, mm)가 게이팅 입력 — 넣은 제품만 고정 프레임이 적용된다.
# key 는 product(우선) 또는 product_type. 값 dict:
#   die_pitch_x_mm, die_pitch_y_mm (필수),
#   wafer_inch(기본 8), edge_mm(기본 0), origin("center"|"corner", 기본 center)
# 예)
#   PRODUCT_WAFER_REF = {
#       "MYPROD": {"die_pitch_x_mm": 2.5, "die_pitch_y_mm": 2.5, "wafer_inch": 8},
#   }
PRODUCT_WAFER_REF: dict[str, dict] = {}


def _lookup(product_type: str, product: str) -> dict | None:
    """product 우선, 없으면 product_type 으로 기준정보를 찾는다."""
    for key in (str(product or "").strip(), str(product_type or "").strip()):
        if key and key in PRODUCT_WAFER_REF:
            return PRODUCT_WAFER_REF[key]
    return None


def frame_for(product_type: str, product: str) -> dict | None:
    """제품 기준정보 → 고정 프레임 {x_min,x_max,y_min,y_max}. die pitch 없으면 None.

    die 중심이 usable 반지름(=wafer_R - edge) 안에 드는 격자 범위를 축별로 잡는다.
    """
    ref = _lookup(product_type, product)
    if not ref:
        return None
    try:
        pitch_x = float(ref["die_pitch_x_mm"])
        pitch_y = float(ref["die_pitch_y_mm"])
    except (KeyError, TypeError, ValueError):
        return None
    if pitch_x <= 0 or pitch_y <= 0:
        return None

    inch = ref.get("wafer_inch", _DEFAULT_WAFER_INCH)
    wafer_mm = _WAFER_MM.get(inch, float(inch) * 25.4)
    edge = float(ref.get("edge_mm", _DEFAULT_EDGE_MM))
    usable_r = wafer_mm / 2.0 - edge
    if usable_r <= 0:
        return None

    nx = int(math.floor(usable_r / pitch_x))
    ny = int(math.floor(usable_r / pitch_y))
    if nx < 1 or ny < 1:
        return None

    origin = str(ref.get("origin", _DEFAULT_ORIGIN)).lower()
    if origin == "corner":
        return {"x_min": 0, "x_max": 2 * nx, "y_min": 0, "y_max": 2 * ny}
    return {"x_min": -nx, "x_max": nx, "y_min": -ny, "y_max": ny}

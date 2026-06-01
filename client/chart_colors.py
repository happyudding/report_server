"""distribution 차트 Legend 별 색 팔레트 (48색) 정의 / 저장 / 로드.

기본값: 1~7번은 진한 원색(파랑·초록·분홍·자주·빨강·주황·청록), 8번부터는
황금비로 hue 를 고르게 분산하되 채도를 높게(원색에 가깝게) 유지하고 명도를
4단계로 순환시켜, 색끼리 겹쳐 그려도 눈으로 구분이 잘 되도록 했다. 사용자가
편집하면 %APPDATA%/Honey/chart_colors.json 에 저장되어 다음 분석 때 적용된다.
"""
from __future__ import annotations

import colorsys
import json
from pathlib import Path

from config import CHART_COLORS_PATH

N_COLORS = 48

# 1~7번: 진한 원색 hue (파랑, 초록, 분홍, 자주, 빨강, 주황, 청록)
_DEEP_HUES = [0.60, 0.33, 0.92, 0.80, 0.00, 0.09, 0.50]
# 8번 이후 hue 분산용 황금비 (인접 색 hue 가 최대한 떨어지게)
_GOLDEN = 0.61803398875
# 채도/명도 순환표 — 길이를 서로 다르게(3, 4) 두어 12색마다 조합이 반복되도록.
# 모두 채도 0.78 이상으로 유지해 파스텔로 흐려지지 않고, 명도는 어둡↔밝 교차.
_S_CYCLE = [1.00, 0.82, 0.92]
_V_CYCLE = [0.92, 0.68, 0.80, 0.55]


def _hsv_hex(h, s, v) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


def generate_default_colors(n: int = N_COLORS) -> list:
    """진한 원색(1~7) + 채도 높은 분산색(8~) 의 구분 잘 되는 기본 48색."""
    cols = [_hsv_hex(h, 1.0, 0.85) for h in _DEEP_HUES]
    remaining = n - len(cols)
    for j in range(remaining):
        h = (0.07 + (j + 1) * _GOLDEN) % 1.0    # 황금비로 hue 분산 (겹침 최소)
        s = _S_CYCLE[j % len(_S_CYCLE)]         # 채도: 0.82~1.0 유지 (원색 톤)
        v = _V_CYCLE[j % len(_V_CYCLE)]         # 명도: 4단계 순환 (어둡↔밝 교차)
        cols.append(_hsv_hex(h, s, v))
    return cols[:n]


def _norm(c) -> str:
    s = str(c).strip().upper()
    if not s.startswith("#"):
        s = "#" + s
    return s if (len(s) == 7) else "#000000"


def load_colors() -> list:
    """저장된 팔레트(없거나 손상 시 기본값) 반환 — 항상 길이 N_COLORS."""
    try:
        data = json.loads(Path(CHART_COLORS_PATH).read_text(encoding="utf-8"))
        cols = data.get("colors")
        if isinstance(cols, list) and len(cols) >= 1:
            cols = [_norm(c) for c in cols][:N_COLORS]
            if len(cols) < N_COLORS:  # 부족분은 기본값으로 보충
                cols += generate_default_colors()[len(cols):]
            return cols
    except Exception:
        pass
    return generate_default_colors()


def save_colors(colors) -> None:
    cols = [_norm(c) for c in colors][:N_COLORS]
    p = Path(CHART_COLORS_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"colors": cols}, indent=2), encoding="utf-8")


def hex_to_excel_rgb(hex_color: str) -> int:
    """'#RRGGBB' → Excel COM 용 RGB 정수 (BGR 순: R + G*256 + B*65536)."""
    s = _norm(hex_color).lstrip("#")
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    return r + (g << 8) + (b << 16)

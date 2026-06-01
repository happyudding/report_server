"""클라이언트 사용자별 설정 저장 / 로드 (%APPDATA%/Honey/settings.json).

product_type 등 다음 실행 때 그대로 복원하고 싶은 값을 보관한다. 차트 색은
별도(chart_colors.json)로 관리하므로 여기엔 넣지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path

from config import CONFIG_DIR

SETTINGS_PATH = str(Path(CONFIG_DIR) / "settings.json")


def load_settings() -> dict:
    """저장된 설정 dict 반환 (없거나 손상 시 빈 dict)."""
    try:
        data = json.loads(Path(SETTINGS_PATH).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    p = Path(SETTINGS_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_setting(key: str, default=None):
    return load_settings().get(key, default)


def set_setting(key: str, value) -> None:
    """단일 키만 갱신 (기존 다른 값 보존)."""
    data = load_settings()
    data[key] = value
    save_settings(data)

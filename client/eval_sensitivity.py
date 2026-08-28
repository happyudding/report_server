"""AI Comment 민감도 게이지 — 클라이언트 설정 영속 + 게이지 → 임계값 해석.

설정은 `%APPDATA%/Honey/settings.json` 의 `eval_sensitivity` 키 하나에 담긴다:
    {"global": 3, "groups": {"OUTLIER": 4, ...}, "manual": {"cpk_warn": 1.5}}
게이지 단계만 저장하고 **구체적인 임계값 숫자는 저장하지 않는다**(직접 입력분 제외) —
서버가 단계표를 손보면 다음 업로드부터 새 값이 자동으로 반영돼야 하기 때문이다.

업로드 시점에 `resolve()` 가 카탈로그와 설정을 합쳐 최종 숫자로 굳혀 manifest 에 싣는다.
그래야 세션이 "그때 무슨 기준으로 판정됐나" 를 자기 안에 갖는다(단계표를 나중에 튜닝해도
기존 세션의 판정 기준은 안 변한다).

카탈로그는 서버에서 받아 같은 파일에 캐시한다 — 서버가 잠깐 안 되어도 설정 창은 열려야 한다.
"""
from __future__ import annotations

import app_settings

SETTINGS_KEY = "eval_sensitivity"
CATALOG_KEY = "eval_sensitivity_catalog"
DEFAULT_LEVEL = 3


def load_settings() -> dict:
    """저장된 게이지 설정 (없으면 기본값). 항상 세 키를 갖춘 dict 를 돌려준다."""
    raw = app_settings.get_setting(SETTINGS_KEY)
    if not isinstance(raw, dict):
        raw = {}
    level = raw.get("global")
    groups = raw.get("groups")
    manual = raw.get("manual")
    return {
        "global": level if isinstance(level, int) and 1 <= level <= 5 else DEFAULT_LEVEL,
        "groups": {str(k): v for k, v in (groups or {}).items()
                   if isinstance(v, int) and 1 <= v <= 5},
        "manual": {str(k): v for k, v in (manual or {}).items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)},
    }


def save_settings(settings: dict) -> None:
    app_settings.set_setting(SETTINGS_KEY, {
        "global": settings.get("global", DEFAULT_LEVEL),
        "groups": settings.get("groups") or {},
        "manual": settings.get("manual") or {},
    })


def load_cached_catalog():
    """마지막으로 받아 둔 카탈로그 (없으면 None) — 서버 조회 실패 시 폴백."""
    raw = app_settings.get_setting(CATALOG_KEY)
    return raw if isinstance(raw, dict) and raw.get("groups") else None


def save_cached_catalog(catalog: dict) -> None:
    if isinstance(catalog, dict) and catalog.get("groups"):
        app_settings.set_setting(CATALOG_KEY, catalog)


def group_level(settings: dict, group_id: str) -> int:
    return (settings.get("groups") or {}).get(group_id, DEFAULT_LEVEL)


def gauge_value(group: dict, key: str, level: int):
    """단계표에서 그 그룹·그 단계의 키 값. 키가 없으면 None."""
    for entry in group.get("keys") or []:
        if entry.get("key") == key:
            levels = entry.get("levels") or []
            if len(levels) == 5 and 1 <= level <= 5:
                return levels[level - 1]
    return None


def resolve(catalog, settings) -> dict | None:
    """카탈로그 + 설정 → 업로드에 실을 spec. 전부 기본이면 None.

    ⚠ **게이지가 3단계인 키는 싣지 않는다** — 값 비교가 아니라 레벨 기준이다.
    3단계 값은 서버 기본값과 같지만, 그 자리에 절대값을 실으면 `/pe/eval` 의 제품군
    오버레이(예: MDDI bimodality_warn 0.33)를 세션값이 덮어 조용히 무력화한다.
    직접 입력(manual)한 키만 3단계에서도 실린다 — 그건 사용자가 명시적으로 정한 값이다.

    None 을 돌려주는 것도 중요하다: 기본 설정 세션이 옵션에 이 키를 실으면 옵션 원문이
    바뀌어 기존 세션과 캐시 키가 갈리고, 전 세션 콜드 재빌드가 된다.
    """
    if not isinstance(catalog, dict) or not catalog.get("groups"):
        return None
    settings = settings or load_settings()
    manual = settings.get("manual") or {}
    overrides, used_manual, levels = {}, {}, {}
    for group in catalog["groups"]:
        gid = str(group.get("id") or "")
        level = group_level(settings, gid)
        levels[gid] = level
        for entry in group.get("keys") or []:
            key = str(entry.get("key") or "")
            if key in manual:
                overrides[key] = manual[key]
                used_manual[key] = manual[key]
            elif level != DEFAULT_LEVEL:
                value = gauge_value(group, key, level)
                if value is not None:
                    overrides[key] = value
    if not overrides:
        return None
    spec = {"v": 1, "global": settings.get("global", DEFAULT_LEVEL),
            "groups": levels, "overrides": overrides}
    if used_manual:
        spec["manual"] = used_manual
    return spec

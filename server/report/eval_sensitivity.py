"""AI Comment 민감도 게이지 설정의 **검증·정규화** (서버 층).

업로드(upload_webreport)와 사후 변경(PATCH .../web_report/eval_sensitivity)이 같은 규칙을
써야 해서 한 곳에 모았다. web_report 패키지가 아니라 server/report 에 두는 이유는
`eval_panel.rules_io`(검증 표)를 쓰기 때문이다 — web_report → eval_panel 역방향 결합을
만들지 않는다(web_report/CLAUDE.md 연결점 표).

들어오는 값은 클라이언트가 이미 **구체값으로 굳혀서** 보낸다(단계표 해석은 저장 시점에
끝난다). 여기서 하는 일은 "그 숫자가 임계값으로 성립하는가" 뿐이다 — 잘못된 값이 세션에
굳으면 그 세션의 평가가 조용히 이상해지고, 되돌릴 방법이 재업로드뿐이라 입구에서 막는다.
"""
from __future__ import annotations

MAX_GAUGE = 5
MIN_GAUGE = 1
_MAX_KEYS = 64          # 게이지 대상 키는 20개 미만 — 상한은 악의적 payload 방어용


class SensitivityError(ValueError):
    """설정이 임계값으로 성립하지 않음. 메시지는 사용자에게 그대로 보인다."""


def _catalog():
    from web_report import eval_debug
    return eval_debug.sensitivity_catalog()


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalize(spec, *, rules_rev: str = "") -> dict | None:
    """클라가 보낸 eval_sensitivity 를 검증 후 저장 형태로 정규화. 기본이면 None.

    반환 None = "옵션에 이 키를 싣지 마라" 는 뜻이다. 기본 설정(전 게이지 3·직접 수정
    없음)까지 실으면 `webreport_options` 원문이 바뀌어 **기존 세션과 캐시 키가 갈리고**
    전 세션 콜드 재빌드가 된다(docs/12 콜드 폭풍).

    잘못된 값은 조용히 버리지 않고 SensitivityError 를 던진다 — 사용자가 조정한 값이
    말없이 무시되면 "설정했는데 안 먹는다" 가 되고, 그건 값이 틀린 것보다 나쁘다.
    """
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise SensitivityError("eval_sensitivity 는 객체여야 합니다")

    catalog = _catalog()
    allowed = set(catalog.get("allowed_keys") or [])
    if not allowed:
        # 단계표를 못 읽는 서버에서는 게이지 자체가 없는 것과 같다. 400 으로 업로드를
        # 막지는 않는다 — 민감도는 부가 기능이고, 막으면 리포트를 아예 못 올린다.
        return None

    global_level = _gauge(spec.get("global"), "global")
    groups = {}
    known_groups = {g["id"] for g in catalog.get("groups") or []}
    for name, level in (spec.get("groups") or {}).items():
        gid = str(name)
        if gid not in known_groups:
            raise SensitivityError(f"알 수 없는 민감도 그룹: {gid}")
        groups[gid] = _gauge(level, gid)

    manual = _numbers(spec.get("manual"), allowed, "manual")
    overrides = _numbers(spec.get("overrides"), allowed, "overrides")
    if not overrides:
        return None                       # 적용할 값이 없으면 기본 세션과 같다

    _check_values(overrides)

    out = {"v": 1, "global": global_level, "groups": groups, "overrides": overrides}
    if manual:
        out["manual"] = manual
    rev = str(rules_rev or "").strip()
    if rev:
        # 업로드 당시 룰 버전 — 나중에 "그때 기준이 뭐였나" 를 세션만 보고 알 수 있게.
        out["rules_rev"] = rev
    return out


def _gauge(value, where: str) -> int:
    """게이지 단계 1~5. 없으면 기본 3."""
    if value is None:
        return 3
    if not isinstance(value, int) or isinstance(value, bool):
        raise SensitivityError(f"{where} 단계는 정수여야 합니다 (받은 값 {value!r})")
    if not MIN_GAUGE <= value <= MAX_GAUGE:
        raise SensitivityError(f"{where} 단계는 {MIN_GAUGE}~{MAX_GAUGE} 여야 합니다 "
                               f"(받은 값 {value})")
    return value


def _numbers(raw, allowed: set, where: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SensitivityError(f"{where} 는 객체여야 합니다")
    if len(raw) > _MAX_KEYS:
        raise SensitivityError(f"{where} 항목이 너무 많습니다 ({len(raw)})")
    out = {}
    for key, value in raw.items():
        name = str(key).strip()
        if name not in allowed:
            raise SensitivityError(f"조절할 수 없는 임계값입니다: {name}")
        if not _is_num(value):
            raise SensitivityError(f"{name}: 숫자여야 합니다 (받은 값 {value!r})")
        if value != value or value in (float("inf"), float("-inf")):
            raise SensitivityError(f"{name}: 유한한 숫자여야 합니다")
        out[name] = value
    return out


def _check_values(overrides: dict) -> None:
    """값 범위·키 사이 관계식 검증 — `/pe/eval` 저장이 쓰는 표를 그대로 재사용한다.

    두 화면이 서로 다른 기준으로 같은 임계값을 받으면, 패널에서는 거부되는 값이 업로드로는
    들어오는 구멍이 된다.
    """
    from eval_panel import rules_io
    from web_report import eval_debug
    effective = dict(eval_debug.default_thresholds())
    effective.update(overrides)
    problems = rules_io._check_threshold_values(effective, set(overrides))
    if problems:
        raise SensitivityError("; ".join(problems))


def describe(spec) -> dict:
    """저장된 설정 → 화면용 상세 (조회 모달·PATCH 응답 공용).

    반환 {"applied": bool, "global": n, "groups": {...},
          "items": [{key, value, default, source, signatures, label, help}...]}.
    `source` 는 "manual"(직접 입력) 또는 "gauge"(단계표) — 사용자가 자기가 손으로 넣은
    값과 게이지가 정한 값을 구분할 수 있어야 한다.
    """
    catalog = _catalog()
    defaults = {}
    group_of, label_of = {}, {}
    for group in catalog.get("groups") or []:
        for entry in group.get("keys") or []:
            defaults[entry["key"]] = entry.get("default")
            group_of[entry["key"]] = group
    help_map = {}
    usage = {}
    try:
        from eval_panel import rules_io
        help_map = rules_io.threshold_help()
        usage = rules_io.threshold_usage()
    except Exception:
        pass

    spec = spec if isinstance(spec, dict) else {}
    overrides = spec.get("overrides") if isinstance(spec.get("overrides"), dict) else {}
    manual = spec.get("manual") if isinstance(spec.get("manual"), dict) else {}
    items = []
    for key in sorted(overrides):
        group = group_of.get(key) or {}
        info = help_map.get(key) or {}
        items.append({
            "key": key,
            "value": overrides[key],
            "default": defaults.get(key),
            "source": "manual" if key in manual else "gauge",
            "group": group.get("id"),
            "group_label": group.get("label_ko"),
            "signatures": [u.get("id") for u in (usage.get(key) or []) if u.get("id")],
            "what": info.get("what") or "",
            "effect": info.get("effect") or "",
        })
    return {"applied": bool(overrides),
            "global": spec.get("global", 3),
            "groups": spec.get("groups") or {},
            "rules_rev": spec.get("rules_rev") or "",
            "items": items}

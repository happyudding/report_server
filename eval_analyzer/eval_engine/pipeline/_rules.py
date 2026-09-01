"""rules/*.yaml 공용 로더 — features/signatures 가 같은 임계값 병합 규칙을 공유.

스코프 우선순위: default → product_type override → 제품군/family 오버레이 트리
→ item_class override (구체값 우선).
signature 선언도 같은 규칙의 오버레이 트리를 갖는다(signatures_for) — 제품군마다
다른 심각도·문구·조건·on/off 를 쓰기 위함.
임계값은 여기(yaml)에서만 읽는다 — 코드에 숫자 하드코딩 금지(불변 규칙 5).

캐시는 (경로, mtime) 키라 yaml 을 고치면 어느 프로세스에서든 다음 호출에서 자동 반영된다
(서버 재시작·프로세스 간 신호 전파 불필요).
"""
import functools
import os
from contextlib import contextmanager

import yaml

from .. import config

# family 오버레이 트리 루트 — rules/thresholds/<PRODUCT_TYPE>/<FAMILY|_default>.yaml
THRESHOLDS_TREE_DIRNAME = "thresholds"
# signature 오버레이 트리 루트 — rules/signatures/<PRODUCT_TYPE>/<FAMILY|_default>.yaml
SIGNATURES_TREE_DIRNAME = "signatures"
TREE_PT_DEFAULT = "_default"


@functools.lru_cache(maxsize=64)
def _load_yaml_cached(path_str: str, mtime_ns: int):
    """(경로, mtime) 키 lru_cache 실체 — mtime 을 인자로 받아야 파일 수정이 캐시 미스가 된다."""
    with open(path_str, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── run 단위 룰 스냅샷 ───────────────────────────────────────────────────────
# `evaluate()` 한 번 동안은 룰을 1회만 읽는다. 종전에는 mtime 을 캐시 키로 쓰느라
# **호출마다 os.stat** 했고, `thresholds_for` 는 case 당 3~4회 불리며 매번 파일 3개
# (기준 + _default 오버레이 + family 오버레이)를 stat 했다 — 실측 11,367회로 콜드
# 평가 시간의 18% 였다(계산이 아니라 파일 I/O). 없는 오버레이는 FileNotFoundError 라
# lru_cache 에 남지도 않아 case 마다 그대로 재시도했다.
#
# 스코프 **밖에서는 종전 mtime 경로 그대로**다(cross_source·eval_debug 등 상시 호출부의
# "yaml 고치면 다음 호출에서 반영" 계약 무변경). 스코프 안이어도 run 이 끝나면 다시
# stat 하므로 `/pe/eval` 룰 편집은 다음 평가부터 반영된다 — 종전과 같다. 오히려 한 run
# 중간에 파일이 바뀌어 case 마다 다른 임계값이 적용되던 틈이 없어진다.
_scope = None
_MISS = object()


@contextmanager
def rules_scope():
    """이 블록 동안 룰 파일·병합 결과를 1회만 만든다 (run 단위 스냅샷). 중첩 안전.

    스레드 경합은 무해하다 — 같은 입력이면 같은 값이고 dict 대입은 원자적이다
    (features._shared 와 같은 논리). 중첩 호출은 바깥 스코프를 그대로 쓴다.
    """
    global _scope
    outer = _scope
    if outer is None:
        _scope = {}
    try:
        yield
    finally:
        _scope = outer


def load_yaml(path_str: str):
    """yaml 파싱 결과(캐시). 파일이 바뀌면 mtime 이 달라져 자동 재파싱된다.

    `rules_scope()` 안에서는 stat 없이 스코프 캐시를 쓴다. **예외(파일 없음)도 캐시**해야
    존재하지 않는 오버레이를 case 마다 다시 stat 하지 않는다 — 그게 이 캐시의 최대 절감분이다.
    되던지는 예외는 traceback 을 지워 재사용한다(같은 객체에 tb 가 누적되지 않게).
    """
    scope = _scope
    if scope is None:
        return _load_yaml_cached(path_str, os.stat(path_str).st_mtime_ns)
    key = ("yaml", path_str)
    hit = scope.get(key, _MISS)
    if hit is _MISS:
        hit = _load_yaml_cached(path_str, os.stat(path_str).st_mtime_ns)
        scope[key] = hit
    return hit


# calibrate.py 등 기존 호출부 호환 (load_yaml.cache_clear()).
load_yaml.cache_clear = _load_yaml_cached.cache_clear
load_yaml.cache_info = _load_yaml_cached.cache_info


def reload_rules() -> None:
    """룰 yaml 캐시 명시 클리어. mtime 키라 평시엔 불필요하지만 강제 리로드용."""
    _load_yaml_cached.cache_clear()
    if _scope is not None:                     # 활성 run 스냅샷도 함께 버린다
        _scope.clear()


def threshold_overlay_path(product_type, family_product=None):
    """오버레이 파일 경로. family_product 가 없으면 제품군 공통(_default) 경로."""
    name = str(family_product) if family_product else TREE_PT_DEFAULT
    return (config.RULES_DIR / THRESHOLDS_TREE_DIRNAME
            / str(product_type) / f"{name}.yaml")


def _overlay(path):
    """오버레이 yaml 을 flat dict 로. 없거나 dict 가 아니면 None."""
    try:
        doc = load_yaml(str(path))
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    return doc if isinstance(doc, dict) else None


def thresholds_for(case_ctx: dict) -> dict:
    """case 의 product_type/family_product/item_class 에 맞춰 병합된 임계값 dict 반환.

    ⚠ **반환 dict 는 읽기 전용이다.** `rules_scope()` 안에서는 스코프가 같은
    (product_type, family_product, item_class) 조합의 case 들에 **같은 객체**를 돌려주므로,
    호출부가 값을 고치면 다른 case 로 번진다. 현재 파이프라인은 전부 읽기만 한다.

    case 에 `_th_override`(세션 단위 임계값, api.evaluate 가 스탬프)가 붙어 있으면 병합 맨
    뒤에 얹힌다. 캐시 키에 그 digest 를 함께 넣어야 같은 run 안에서 override 유무가 섞이지
    않는다.
    """
    scope = _scope
    if scope is None:
        return _thresholds_merged(case_ctx)
    key = ("th", case_ctx.get("product_type"), case_ctx.get("family_product"),
           case_ctx.get("item_class"), case_ctx.get("_th_override_digest"))
    hit = scope.get(key)
    if hit is None:
        hit = _thresholds_merged(case_ctx)
        scope[key] = hit
    return hit


def _thresholds_merged(case_ctx: dict) -> dict:
    """thresholds_for 의 병합 실체 (스코프 캐시 미스에서만 실행)."""
    doc = load_yaml(str(config.THRESHOLDS_FILE))
    merged = dict(doc.get("default", {}))
    product_type = case_ctx.get("product_type")
    pt = doc.get("product_type", {}).get(product_type)
    if pt:
        merged.update(pt)
    if product_type:
        # 제품군 공통(_default) → family 순. 파일이 없으면 각각 skip = 트리 없으면 종전 동작
        families = [None]
        if case_ctx.get("family_product"):
            families.append(case_ctx["family_product"])
        for family in families:
            ov = _overlay(threshold_overlay_path(product_type, family))
            if ov:
                merged.update(ov)
    ic = doc.get("item_class", {}).get(case_ctx.get("item_class"))
    if ic:
        merged.update(ic)
    # 세션 단위 오버라이드(민감도 게이지)가 가장 구체적인 스코프 — 파일 스코프 전체를 덮는다.
    ovr = case_ctx.get("_th_override")
    if ovr:
        merged.update(ovr)
    return merged


def signatures_doc() -> dict:
    """rules/signatures.yaml 전체 문서(캐시). signature 선언의 **기준값** 출처."""
    return load_yaml(str(config.SIGNATURES_FILE))


def signature_overlay_path(product_type, family_product=None):
    """signature 오버레이 파일 경로. family_product 가 없으면 제품군 공통(_default)."""
    name = str(family_product) if family_product else TREE_PT_DEFAULT
    return (config.RULES_DIR / SIGNATURES_TREE_DIRNAME
            / str(product_type) / f"{name}.yaml")


def signature_overrides(product_type, family_product=None) -> dict:
    """{signature_id: {필드: 값}} — 제품군 공통(_default) → family 순으로 병합.

    오버레이 yaml 형태:
        signatures:
          LOW_CPK:
            enabled: true
            status_hint: MAJOR
    선언한 필드만 기준값을 덮는다(필드 단위 교체). 파일이 없으면 빈 dict = 종전 동작.
    """
    if not product_type:
        return {}
    merged = {}
    families = [None] + ([family_product] if family_product else [])
    for family in families:
        doc = _overlay(signature_overlay_path(product_type, family)) or {}
        for sig_id, fields in (doc.get("signatures") or {}).items():
            if isinstance(fields, dict):
                merged.setdefault(str(sig_id), {}).update(fields)
    return merged


def signatures_for(case_ctx: dict) -> list:
    """case 의 product_type/family_product 에 맞춰 오버레이를 얹은 signature 목록.

    스코프 우선순위는 thresholds 와 같다:
        signatures.yaml → signatures/<PT>/_default.yaml → signatures/<PT>/<FAMILY>.yaml

    ⚠ 반환 목록·항목 dict 는 `thresholds_for` 와 같은 이유로 **읽기 전용**이다.
    """
    scope = _scope
    if scope is None:
        return _signatures_merged(case_ctx)
    ctx = case_ctx or {}
    key = ("sig", ctx.get("product_type"), ctx.get("family_product"))
    hit = scope.get(key)
    if hit is None:
        hit = _signatures_merged(case_ctx)
        scope[key] = hit
    return hit


def _signatures_merged(case_ctx: dict) -> list:
    """signatures_for 의 병합 실체 (스코프 캐시 미스에서만 실행)."""
    base = signatures_doc().get("signatures") or []
    overrides = signature_overrides((case_ctx or {}).get("product_type"),
                                    (case_ctx or {}).get("family_product"))
    if not overrides:
        return list(base)
    return [{**s, **overrides.get(s.get("id"), {})} for s in base]


def issue_category_for(signature_id, case_ctx=None) -> str:
    """primary signature id → report_server Issue Table 버킷 'YIELD'|'CPK'|'ETC'.

    signatures.yaml 의 issue_category 선언을 읽고, 미지정/None/미매칭은 'ETC'(기본).
    report_generator 가 signature 택소노미를 몰라도 카테고리 분류가 되게 하는 편의 필드.
    case_ctx 를 주면 그 제품군 오버레이가 반영된 값을 쓴다.
    """
    if not signature_id:
        return "ETC"
    sigs = signatures_for(case_ctx) if case_ctx else (signatures_doc().get("signatures") or [])
    for s in sigs:
        if s.get("id") == signature_id:
            return s.get("issue_category") or "ETC"
    return "ETC"


def exclusions_doc() -> dict:
    """rules/exclusions.yaml — 평가 제외 목록. 파일이 없으면 빈 규칙(전부 평가)."""
    try:
        doc = load_yaml(str(config.EXCLUSIONS_FILE))
    except (FileNotFoundError, NotADirectoryError, OSError):
        return {}
    return doc if isinstance(doc, dict) else {}


def exclusion_reason(case_ctx: dict):
    """case 가 제외 목록에 걸리면 사유 문자열, 아니면 None.

    item_contains 는 item 명(원문) 부분일치, units 는 UNIT 원문 정확일치 — 둘 다
    대소문자 무시. 매칭되면 L3 가 signature 를 하나도 발화시키지 않고
    L6 저장 게이트(present.should_store)도 통과하지 못한다(코멘트 미생성).
    """
    doc = exclusions_doc()
    item = str(case_ctx.get("item_raw") or "").upper()
    if item:
        for token in doc.get("item_contains") or []:
            t = str(token).strip()
            if t and t.upper() in item:
                return f"item명에 '{t}' 포함"
    unit = str(case_ctx.get("unit") or "").strip().upper()
    if unit:
        for u in doc.get("units") or []:
            if str(u).strip().upper() == unit:
                return f"unit '{str(u).strip()}' 일치"
    return None


def ai_prompt_doc() -> dict:
    """rules/ai_prompt.yaml — AI Comment [제안] 지시문·금지 문구. 파일이 없으면 {}.

    엔진이 쓰는 것은 `instructions` 뿐이다(`deny_patterns` 는 서버가 클라 push 를 받을 때
    쓰는 금지 문구라 여기서는 읽지 않는다). exclusions_doc 과 같은 관용 로딩 —
    파일이 없으면 종전 프롬프트가 그대로 나간다.
    """
    try:
        doc = load_yaml(str(config.AI_PROMPT_FILE))
    except (FileNotFoundError, NotADirectoryError, OSError):
        return {}
    return doc if isinstance(doc, dict) else {}


def ai_prompt_instructions(doc=None) -> list:
    """프롬프트에 덧붙일 지시 문장 목록 (enabled 만, 선언 순서 유지).

    빈 text·비-dict 항목은 조용히 건너뛴다 — 관리자 화면이 검증하지만, 손으로 고친
    yaml 때문에 평가가 죽어서는 안 된다(프롬프트 지시는 부가 재료다).
    """
    doc = ai_prompt_doc() if doc is None else doc
    out = []
    for item in (doc or {}).get("instructions") or []:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        text = str(item.get("text") or "").strip()
        if text:
            out.append(text)
    return out


def outcome_taxonomy() -> dict:
    """rules/outcome_taxonomy.yaml 전체 문서(캐시) — action/result 허용 어휘의 정본."""
    return load_yaml(str(config.OUTCOME_TAXONOMY_FILE))


def outcome_label(kind: str, code: str) -> dict:
    """kind='action'|'result', code → {'ko':.., 'group':..}. 미정의/None → {}."""
    if not code:
        return {}
    return (outcome_taxonomy().get(kind) or {}).get(code, {})


def validate_outcome(action, result) -> None:
    """action/result 를 어휘로 강제 검증(None 은 통과). 미정의면 ValueError.
    _validate_product_meta(ingest.py) 와 동일 패턴."""
    tax = outcome_taxonomy()
    for kind, code in (("action", action), ("result", result)):
        if code is not None and code not in (tax.get(kind) or {}):
            raise ValueError(
                f"outcome.{kind} '{code}' 은 허용 어휘 "
                f"{list((tax.get(kind) or {}).keys())} 에 없음")


def bin_taxonomy_for(product_type, bin_number):
    """rules/bin_taxonomy.yaml entries 에서 (product_type, bin_number) 매칭 1건. 없으면 None."""
    try:
        doc = load_yaml(str(config.BIN_TAXONOMY_FILE))
    except FileNotFoundError:
        return None
    for e in (doc or {}).get("entries") or []:
        if e.get("product_type") == product_type and e.get("bin_number") == bin_number:
            return e
    return None

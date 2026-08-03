"""rules/*.yaml 공용 로더 — features/signatures 가 같은 임계값 병합 규칙을 공유.

스코프 우선순위: default → product_type override → 제품군/family 오버레이 트리
→ item_class override (구체값 우선).
임계값은 여기(yaml)에서만 읽는다 — 코드에 숫자 하드코딩 금지(불변 규칙 5).

캐시는 (경로, mtime) 키라 yaml 을 고치면 어느 프로세스에서든 다음 호출에서 자동 반영된다
(서버 재시작·프로세스 간 신호 전파 불필요).
"""
import functools
import os

import yaml

from .. import config

# family 오버레이 트리 루트 — rules/thresholds/<PRODUCT_TYPE>/<FAMILY|_default>.yaml
THRESHOLDS_TREE_DIRNAME = "thresholds"
TREE_PT_DEFAULT = "_default"


@functools.lru_cache(maxsize=64)
def _load_yaml_cached(path_str: str, mtime_ns: int):
    with open(path_str, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_yaml(path_str: str):
    """yaml 파싱 결과(캐시). 파일이 바뀌면 mtime 이 달라져 자동 재파싱된다."""
    return _load_yaml_cached(path_str, os.stat(path_str).st_mtime_ns)


# calibrate.py 등 기존 호출부 호환 (load_yaml.cache_clear()).
load_yaml.cache_clear = _load_yaml_cached.cache_clear
load_yaml.cache_info = _load_yaml_cached.cache_info


def reload_rules() -> None:
    """룰 yaml 캐시 명시 클리어. mtime 키라 평시엔 불필요하지만 강제 리로드용."""
    _load_yaml_cached.cache_clear()


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
    """case 의 product_type/family_product/item_class 에 맞춰 병합된 임계값 dict 반환."""
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
    return merged


def signatures_doc() -> dict:
    return load_yaml(str(config.SIGNATURES_FILE))


def issue_category_for(signature_id) -> str:
    """primary signature id → report_server Issue Table 버킷 'YIELD'|'CPK'|'ETC'.

    signatures.yaml 의 issue_category 선언을 읽고, 미지정/None/미매칭은 'ETC'(기본).
    report_generator 가 signature 택소노미를 몰라도 카테고리 분류가 되게 하는 편의 필드.
    """
    if not signature_id:
        return "ETC"
    for s in signatures_doc().get("signatures", []):
        if s.get("id") == signature_id:
            return s.get("issue_category") or "ETC"
    return "ETC"


def outcome_taxonomy() -> dict:
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

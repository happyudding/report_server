"""L3 Signature — feature 조합 → 발화 signature. 선언형 rules/signatures.yaml + thresholds.yaml.

할 일:
  1. thresholds.yaml 로드(item_class/product_type override). 임계값은 _rules 에서만.
  2. signatures.yaml when_metric 평가 → 발화 signature 목록 + reason_codes(=eval_evidence 후보).
  3. 결측(feature None) → 해당 룰 applies=False (양호로 오판 금지).
  4. n_dut < n_min → 고차모멘트(skewness/kurtosis) 의존 signature 비활성화.
  5. bin_taxonomy 로 bin_class/severity_bias 조회 → status 변조 컨텍스트 첨부.
반환: {"signatures":[{id,status_hint,score,evidence,action_ko}], "reason_codes":[...],
       "bin_class":..., "severity_bias":..., "applies":{...},
       "raw_metrics_snapshot":{cpk,yield}}  ← status.decide 의 trump 판단용
"""
import re

from ._rules import (thresholds_for, signatures_doc, signatures_for,  # noqa: F401
                     bin_taxonomy_for, exclusion_reason)

# 고차모멘트(표본 부족 시 비활성) 의존 metric
_HIGH_MOMENT_METRICS = {"skewness", "kurtosis", "bimodality_score"}
_SUBPOP_GAP_ID = "SUBPOP_GAP"

def _evaluate_subpop_gap(features : dict):
    """SUBPOP_GAP(이봉·분리) 전용 평가 — features 의 modality_v2 판정을 그대로 발화 근거로 쓴다.

    다른 signature 처럼 when_metric 조건식 하나로 줄일 수 없어서 별도 경로를 둔다.
    modality_v2 가 없으면(표본 부족 등) 발화하지 않는다.
    evidence 4종: MODALITY_V2 / N_MODES / DENSITY_GAP(밀도 골) / VALUE_GAP(값축 빈 구간).
    ⚠ DENSITY_GAP 과 VALUE_GAP 은 서로 다른 지표다 — 예전엔 DENSITY_GAP 라벨에 cdf_gap
    값을 싣던 오라벨이 있었고 2026-08-03 에 분리했다(VERIFY_CHECKLIST §2-1).
    """
    modality_v2 = features.get("modality_v2")
    if modality_v2 is None:
        return None
    evidence = [
        {"signal_code" : "MODALITY_V2", "value" : None, "note" : f"modality_v2 {modality_v2}"},
        {"signal_code" : "N_MODES", "value" : features.get("n_modes"),
            "note" : f"n_modes {features.get('n_modes')}"},
        # signal_code ↔ 값 일치 필수 — eval_evidence PK(eval_id, signal_code)로 영구 저장됨.
        {"signal_code" : "DENSITY_GAP", "value" : features.get("density_gap"),
            "note" : f"density_gap {features.get('density_gap')}"},
        {"signal_code" : "VALUE_GAP", "value" : features.get("value_gap_ratio"),
            "note" : f"value_gap_ratio {features.get('value_gap_ratio')}"},
    ]
    return {"modality_v2" : modality_v2, "evidence" : evidence}

def scope_matches(sig: dict, case_ctx: dict) -> bool:
    """signature 의 적용 범위(scope) 검사 — 제품군/family 별로 룰을 갈라 쓰기 위한 필터.

    yaml 형태:
        scope:
          product_type: [PMIC, MDDI]     # 생략/빈 목록 = 전 제품군
          family_product: [SOC]          # 생략/빈 목록 = 전 family
    둘 다 지정하면 AND. scope 키 자체가 없으면 종전대로 전 제품 공통이다(기존 yaml 무영향).
    """
    scope = sig.get("scope") or {}
    if not scope:
        return True
    for key in ("product_type", "family_product"):
        allowed = scope.get(key) or []
        if allowed and case_ctx.get(key) not in allowed:
            return False
    return True


def _eval_condition(op_str, actual_value, thresholds):
    """'>key' / '<key' / 'abs>key' / '>0.5' 형태 해석. 결측이면 False."""
    if actual_value is None:
        return False
    m = re.match(r"(abs)?\s*([<>]=?)\s*(.+)", str(op_str).strip())
    if not m:
        return False
    abs_flag, op, ref = m.group(1), m.group(2), m.group(3).strip()
    target = thresholds[ref] if ref in thresholds else float(ref)
    lhs = abs(actual_value) if abs_flag else actual_value
    return {">": lhs > target, ">=": lhs >= target,
            "<": lhs < target, "<=": lhs <= target}[op]


def _format_evidence(template, ctx_values):
    """'spread_norm {spread_norm}' → {signal_code, value, note}."""
    keys = re.findall(r"\{(\w+)\}", template)

    def repl(mo):
        """`{key}` 자리를 ctx_values 값으로 치환. 수치는 유효숫자 4자리(%.4g)."""
        k = mo.group(1)
        val = ctx_values.get(k)
        return f"{val:.4g}" if isinstance(val, (int, float)) else str(val)

    note = re.sub(r"\{(\w+)\}", repl, template)
    primary_key = keys[0] if keys else template
    value = ctx_values.get(primary_key) if keys else None
    value = round(value, 4) if isinstance(value, (int, float)) else None
    return {"signal_code": primary_key.upper(), "value": value, "note": note}


def build_ctx_values(case_ctx: dict, features: dict, raw_metrics: dict) -> dict:
    """when_metric 평가 대상 값 = raw_metrics + features + 파생값(DB 저장 안 함).

    evaluate() 가 쓰는 조립 로직 그대로 — 관리자 트레이스가 파생값을 재구현하지
    않도록 공개 함수로 분리했다(출력 불변).
    """
    ctx_values = {**raw_metrics, **features}  # cpk/yield(raw) + spread_norm 등(features)
    # 방향무관 spec 근접도(파생값, DB 저장 안 함) — TAIL_RISK 양방향 커버용
    _sml, _smh = features.get("spec_margin_low"), features.get("spec_margin_high")
    _margins = [m for m in (_sml, _smh) if m is not None]
    if _margins:
        ctx_values["spec_margin_min"] = min(_margins)
    # center_bias: 중심 이탈도([-1,1], 0=센터, 부호=치우친 방향) — MEAN_SHIFT용(양쪽 spec 필요)
    if _sml is not None and _smh is not None and (_sml + _smh) > 0:
        ctx_values["center_bias"] = (_smh - _sml) / (_sml + _smh)

    _outlier_ratio, _n_dut = features.get("outlier_ratio"), features.get("n_dut")
    if _outlier_ratio is not None and _n_dut:
        ctx_values["outlier_count"] = round(_outlier_ratio * _n_dut)
    ctx_values["limit_missing"] = int(case_ctx.get("lsl") is None or case_ctx.get("usl") is None)

    _g = [features.get(k) for k in
          ("radial_gradient_norm" , "x_gradient_norm", "y_gradient_norm")]
    _g = [abs(v) for v in _g if v is not None]
    if _g:
        ctx_values["gradient_norm_abs_max"] = max(_g)
    return ctx_values


def evaluate(case_ctx: dict, features: dict, raw_metrics: dict) -> dict:
    """L3 진입점 — signatures.yaml 의 when_metric 을 전부 평가해 발화 signature 목록 산출.

    룰 선언은 `signatures_for(case_ctx)` 로 읽는다 — 제품군/family 오버레이 트리
    (rules/signatures/<PT>/<FAMILY|_default>.yaml)가 얹힌 결과다(트리 없으면 기준값 그대로).

    비활성 경로 4종: yaml `enabled: false`(룰 끄기), `scope`(제품군/family 밖), 표본
    부족(n_dut < n_min)일 때 고차모멘트 의존 signature, feature 결측(값 None → 조건
    False). 뒤 둘은 **결측을 양호로 읽지 않기 위한** 장치다.
    SUBPOP_GAP 만 조건식으로 못 줄여 `_evaluate_subpop_gap` 별도 경로로 뺀다.
    `applies` 는 "그 조건을 판정할 데이터가 있었나"를 남기는 트레이스용 기록(발화 여부와 별개).
    반환 키: signatures / reason_codes / bin_class / severity_bias / applies /
    raw_metrics_snapshot(status.decide 의 trump 판단용 cpk·yield).
    """
    # 평가 제외 목록(rules/exclusions.yaml) — item명 문구/unit 매칭 시 signature 전체 미평가.
    # "excluded" 키는 present.should_store(저장 차단)와 트레이스(제외 사유 표시)가 읽는다.
    excluded = exclusion_reason(case_ctx)
    if excluded:
        bt = bin_taxonomy_for(case_ctx.get("product_type"), case_ctx.get("bin"))
        return {
            "signatures": [], "reason_codes": [],
            "bin_class": bt.get("bin_class") if bt else None,
            "severity_bias": (bt.get("severity_bias") if bt else 0.0) or 0.0,
            "applies": {}, "excluded": excluded,
            "raw_metrics_snapshot": {"cpk": raw_metrics.get("cpk"),
                                     "yield": raw_metrics.get("yield")},
        }

    th = thresholds_for(case_ctx)
    ctx_values = build_ctx_values(case_ctx, features, raw_metrics)

    n_dut = features.get("n_dut") or 0
    high_moment_ok = n_dut >= th["n_min"]

    fired, reason_codes, applies = [], [], {}
    subpop_doc = None
    for sig in signatures_for(case_ctx):
        # yaml 의 enabled:false 는 룰 비활성 (키 부재 = 활성 — 기존 yaml 무영향)
        if sig.get("enabled") is False:
            continue
        # scope 는 enabled 다음, 특수분기보다 앞 — "이 제품군에서 안 쓰는 룰" 은 SUBPOP 도 예외 아님
        if not scope_matches(sig, case_ctx):
            continue
        if sig["id"] == _SUBPOP_GAP_ID:
            subpop_doc = sig
            continue
        when = sig.get("when_metric", {}) or {}
        # 고차모멘트 의존 signature 인데 표본 부족 → 비활성
        if not high_moment_ok and (set(when) & _HIGH_MOMENT_METRICS):
            continue
        ok = bool(when)
        for metric, cond in when.items():
            actual = ctx_values.get(metric)
            applies[f"{sig['id']}.{metric}"] = actual is not None
            ok = ok and _eval_condition(cond, actual, th)
        if ok:
            evidence = [_format_evidence(t, ctx_values) for t in sig.get("evidence", [])]
            fired.append({"id": sig["id"], "status_hint": sig["status_hint"],
                          "score": None, "evidence": evidence,
                          "action_ko": sig.get("action_ko")})
            reason_codes.extend(e["signal_code"] for e in evidence)

    if subpop_doc is not None:
        subpop = _evaluate_subpop_gap(features)
        applies[f"{_SUBPOP_GAP_ID}.modality_v2"] = features.get("modality_v2") is not None
        if subpop is not None:
            fired.append({"id": _SUBPOP_GAP_ID, "status_hint" : subpop_doc["status_hint"],
                          "score":None, "evidence" : subpop["evidence"], "action_ko":subpop_doc.get("action_ko"), "modality_v2":subpop["modality_v2"]})
            reason_codes.extend(e["signal_code"] for e in subpop["evidence"])

    bt = bin_taxonomy_for(case_ctx.get("product_type"), case_ctx.get("bin"))
    bin_class = bt.get("bin_class") if bt else None
    severity_bias = bt.get("severity_bias") if bt else 0.0

    return {
        "signatures": fired, "reason_codes": reason_codes,
        "bin_class": bin_class, "severity_bias": severity_bias or 0.0,
        "applies": applies,
        "raw_metrics_snapshot": {"cpk": raw_metrics.get("cpk"),
                                 "yield": raw_metrics.get("yield")},
    }

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

from ._rules import thresholds_for, signatures_doc, bin_taxonomy_for

# 고차모멘트(표본 부족 시 비활성) 의존 metric
_HIGH_MOMENT_METRICS = {"skewness", "kurtosis", "bimodality_score"}


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
        k = mo.group(1)
        val = ctx_values.get(k)
        return f"{val:.4g}" if isinstance(val, (int, float)) else str(val)

    note = re.sub(r"\{(\w+)\}", repl, template)
    primary_key = keys[0] if keys else template
    value = ctx_values.get(primary_key) if keys else None
    value = round(value, 4) if isinstance(value, (int, float)) else None
    return {"signal_code": primary_key.upper(), "value": value, "note": note}


def evaluate(case_ctx: dict, features: dict, raw_metrics: dict) -> dict:
    th = thresholds_for(case_ctx)
    ctx_values = {**raw_metrics, **features}  # cpk/yield(raw) + spread_norm 등(features)
    # 방향무관 spec 근접도(파생값, DB 저장 안 함) — TAIL_RISK 양방향 커버용
    _sml, _smh = features.get("spec_margin_low"), features.get("spec_margin_high")
    _margins = [m for m in (_sml, _smh) if m is not None]
    if _margins:
        ctx_values["spec_margin_min"] = min(_margins)
    # center_bias: 중심 이탈도([-1,1], 0=센터, 부호=치우친 방향) — MEAN_SHIFT용(양쪽 spec 필요)
    if _sml is not None and _smh is not None and (_sml + _smh) > 0:
        ctx_values["center_bias"] = (_smh - _sml) / (_sml + _smh)

    n_dut = features.get("n_dut") or 0
    high_moment_ok = n_dut >= th["n_min"]

    fired, reason_codes, applies = [], [], {}
    for sig in signatures_doc()["signatures"]:
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

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
_BIMODALITY_ID = "BIMODALITY"      # 2026-08-12 개명 (구 SUBPOP_GAP)
_UNKNOWN_ID = "UNKNOWN"


def fail_count_of(case_ctx: dict):
    """이 case 의 fail chip 수 — ingest 가 넣은 fail_count, 없으면 fail_mask 로 폴백.

    입력 경로마다 채워지는 키가 달라서(raw_df 는 fail_count, 레거시 raw_table 은
    fail_mask 만) status.decide 가 쓰던 폴백과 같은 식을 공용 함수로 뺐다.
    둘 다 없으면 None — "fail 이 0" 이 아니라 "모른다" 이므로 UNKNOWN 발화도 하지 않는다.
    """
    fail_count = case_ctx.get("fail_count")
    if fail_count is None and "fail_mask" in case_ctx:
        fail_count = sum(1 for f in case_ctx.get("fail_mask") or [] if f)
    return fail_count


def _unknown_reason(case_ctx: dict, features: dict, raw_metrics: dict, thresholds: dict):
    """UNKNOWN 발화 사유 1개 — "왜 어떤 룰도 못 떴나". 줄이려면 무엇을 고쳐야 하는지가 다르다.

    NO_STATS_PF : value_type=PF → L1/L2 가 통계를 전부 비워 모든 when_metric 이 결측→False.
                  대부분 UNIT 표기가 엔진 정확일치 표에 없어서 생긴 오분류다(단위표 등록으로 해결).
    NO_LIMIT    : LSL/USL 이 둘 다 없어 cpk·spec margin 계열이 산출 불가(limit mapping 문제).
    LOW_SAMPLE  : n_dut < n_min — 고차모멘트 룰이 min-n 가드로 빠진다(표본 확보 문제).
    NO_MATCH    : 통계는 멀쩡한데 어떤 조건에도 안 걸림 → 임계값 조정이나 새 룰이 필요한 부류.
    """
    if str(case_ctx.get("value_type")) == "PF":
        return ("NO_STATS_PF",
                f"value_type=PF (UNIT {case_ctx.get('unit')!r}) — 통계가 비어 룰 평가 불가")
    if case_ctx.get("lsl") is None and case_ctx.get("usl") is None:
        return ("NO_LIMIT", "LSL/USL 없음 — cpk·spec margin 계열 산출 불가")
    n_dut = features.get("n_dut") or 0
    if n_dut < thresholds["n_min"]:
        return ("LOW_SAMPLE", f"n_dut {n_dut} < n_min {thresholds['n_min']}")
    return ("NO_MATCH", "통계는 산출됐으나 활성 룰의 조건에 해당 없음")


def _evaluate_unknown(case_ctx, features, raw_metrics, thresholds, doc):
    """fail 인데 발화 0건 → UNKNOWN 합성. fail 이 없거나 모르면 발화하지 않는다.

    "모든 fail 은 signature 로 설명된다" 를 강제하기 위한 명시 발화다. 이게 없으면 fail
    케이스가 발화 0건 + 결측 없음 조건에서 `status=OK`(정상 확정)로 나가버려, 설명하지
    못한 fail 과 정상이 화면에서 구분되지 않는다.
    """
    code, note = _unknown_reason(case_ctx, features, raw_metrics, thresholds)
    return {"id": _UNKNOWN_ID, "status_hint": doc["status_hint"], "score": None,
            "evidence": [{"signal_code": f"UNKNOWN_{code}", "value": None, "note": note}],
            "action_ko": doc.get("action_ko"), "unknown_reason": code}

def _evaluate_subpop_gap(features : dict):
    """BIMODALITY(이봉·분리) 전용 평가 — features 의 modality_v2 판정을 그대로 발화 근거로 쓴다.

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


def _suppressor_ids(sig: dict) -> list:
    """signature 선언의 `suppressed_by` 정규화 — 문자열 1개도 목록으로 받는다."""
    raw = sig.get("suppressed_by") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(v) for v in raw if v]


def _apply_suppression(fired: list, suppressors: dict):
    """포함관계인 룰의 중복 발화 제거 — "A 가 뜨면 B 는 군더더기" 를 선언으로 처리한다.

    왜 필요한가: `SEVERE_OUTLIER`(outlier_ratio > outlier_ratio_bad)가 뜨면
    `OUTLIER_WARN`(> outlier_ratio_warn)은 **조건상 항상** 함께 뜬다(임계값 관계 검증이
    warn <= bad 를 강제한다). 같은 현상의 약한 표현이 secondary 를 채우고 primary
    specificity 경쟁까지 흐린다. 임계값은 건드리지 않고 중복 의미만 걷어낸다.

    판정은 **억제 적용 전(원본) 발화 집합 기준 1패스**다 — 전이(A→B→C)나 상호 참조로
    체인이 도는 것을 원천 차단하기 위해서다. 그래서 "B 가 A 에 의해 지워졌더라도 B 가
    지목한 C 는 여전히 지워진다" 가 아니라, C 는 B 의 원본 발화 여부만 본다.
    (순환·미존재 id 검사는 룰 저장 시점 검증의 몫이다.)
    반환: (살아남은 fired, [{"id":..,"by":..}, ...])
    """
    if not any(suppressors.values()):
        return fired, []
    fired_ids = {s["id"] for s in fired}
    kept, suppressed = [], []
    for s in fired:
        by = [sid for sid in suppressors.get(s["id"], []) if sid in fired_ids]
        if by:
            suppressed.append({"id": s["id"], "by": sorted(by)})
        else:
            kept.append(s)
    return kept, suppressed


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
    조건식으로 못 줄이는 특수분기 2개는 루프 밖으로 뺀다 — `BIMODALITY`(modality_v2 로
    판정)와 `UNKNOWN`(다른 룰이 하나도 안 떴을 때 fail 을 설명 없음으로 명시 발화).
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
            "applies": {}, "suppressed": [], "excluded": excluded,
            "raw_metrics_snapshot": {"cpk": raw_metrics.get("cpk"),
                                     "yield": raw_metrics.get("yield")},
        }

    th = thresholds_for(case_ctx)
    ctx_values = build_ctx_values(case_ctx, features, raw_metrics)

    n_dut = features.get("n_dut") or 0
    high_moment_ok = n_dut >= th["n_min"]

    fired, applies = [], {}
    suppressors = {}                     # {id: [나를 지우는 id, ...]} — 발화분만 모은다
    subpop_doc = unknown_doc = None
    for sig in signatures_for(case_ctx):
        # yaml 의 enabled:false 는 룰 비활성 (키 부재 = 활성 — 기존 yaml 무영향)
        if sig.get("enabled") is False:
            continue
        # scope 는 enabled 다음, 특수분기보다 앞 — "이 제품군에서 안 쓰는 룰" 은 BIMODALITY 도 예외 아님
        if not scope_matches(sig, case_ctx):
            continue
        if sig["id"] == _BIMODALITY_ID:
            subpop_doc = sig
            continue
        if sig["id"] == _UNKNOWN_ID:
            # 다른 룰의 발화 결과를 봐야 하므로 루프가 끝난 뒤(억제 적용 후) 판정한다.
            unknown_doc = sig
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
            suppressors[sig["id"]] = _suppressor_ids(sig)

    if subpop_doc is not None:
        subpop = _evaluate_subpop_gap(features)
        applies[f"{_BIMODALITY_ID}.modality_v2"] = features.get("modality_v2") is not None
        if subpop is not None:
            fired.append({"id": _BIMODALITY_ID, "status_hint" : subpop_doc["status_hint"],
                          "score":None, "evidence" : subpop["evidence"], "action_ko":subpop_doc.get("action_ko"), "modality_v2":subpop["modality_v2"]})
            suppressors[_BIMODALITY_ID] = _suppressor_ids(subpop_doc)

    fired, suppressed = _apply_suppression(fired, suppressors)

    # UNKNOWN 은 **억제까지 끝난 최종 발화 집합**을 보고 판정한다 — 억제로 발화가 0이 된
    # 경우는 "설명이 있었는데 중복이라 지운 것" 이므로 여기 해당하지 않는다(억제는 같은
    # 현상의 약한 표현만 지우므로 최소 1건은 남는다).
    fail_count = fail_count_of(case_ctx)
    if unknown_doc is not None and not fired and fail_count:
        fired.append(_evaluate_unknown(case_ctx, features, raw_metrics, th, unknown_doc))
    applies[f"{_UNKNOWN_ID}.fail_count"] = fail_count is not None

    reason_codes = [e["signal_code"] for s in fired for e in s.get("evidence", [])]

    bt = bin_taxonomy_for(case_ctx.get("product_type"), case_ctx.get("bin"))
    bin_class = bt.get("bin_class") if bt else None
    severity_bias = bt.get("severity_bias") if bt else 0.0

    return {
        "signatures": fired, "reason_codes": reason_codes,
        "bin_class": bin_class, "severity_bias": severity_bias or 0.0,
        "applies": applies, "suppressed": suppressed,
        "raw_metrics_snapshot": {"cpk": raw_metrics.get("cpk"),
                                 "yield": raw_metrics.get("yield")},
    }

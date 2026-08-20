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

# 표본 부족 시 비활성 — 고차모멘트와 **극단 분위**. 둘 다 소표본에서 값이 널뛴다
# (분위는 n 이 작으면 P99.5 가 사실상 최대값이라 점 하나에 좌우된다).
_HIGH_MOMENT_METRICS = {"skewness", "kurtosis", "bimodality_score",
                        "tail_extent_high", "tail_extent_low"}
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


def _id_list(sig: dict, key: str) -> list:
    """signature 선언의 id 목록 필드 정규화 — 문자열 1개도 목록으로 받는다.

    `suppressed_by`(primary 양보) / `hidden_by`(목록에서 제거) / `replaces`(대체 발화)가
    같은 표기를 쓴다.
    """
    raw = sig.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(v) for v in raw if v]


def _suppressor_ids(sig: dict) -> list:
    """signature 선언의 `suppressed_by` 정규화 — 문자열 1개도 목록으로 받는다."""
    return _id_list(sig, "suppressed_by")


def _apply_exclusive(fired: list, docs: dict):
    """`exclusive: true` 선언 — 이 룰이 뜨면 **다른 발화를 전부 지우고 혼자 남는다**.

    관계 3종(양보/제거/대체)과 달리 상대를 지목하지 않는다. "이 item 은 측정값 항목이
    아니라서 산포·꼬리·cpk 같은 통계 해석이 통째로 성립하지 않는다" 는 **해석의 선점**이기
    때문이다 — 지목할 상대가 특정 룰이 아니라 나머지 전부다. 배포 룰에서는 `FUNC_FAIL`
    하나가 쓴다(2026-08-20 사용자 요청: "다른 signature 발화하지 말고 FUNC_FAIL 만").

    적용은 **대체보다 먼저**다. 나중에 하면 `replaces` 가 합성한 발화(BIDIR_TAIL)가
    exclusive 를 통과해 버린다.
    ⚠ 제거된 발화는 `hidden_by` 와 마찬가지로 화면 어디에도 남지 않는다(트레이스에만).

    판정은 원본 발화 집합 기준 1패스. exclusive 가 여럿이면 그것들만 함께 남긴다
    (순서 의존을 만들지 않기 위해 — 실제로는 배포 룰에 하나뿐이다).
    반환: (남은 fired, [{"id": 남은 id, "of": [지워진 id …]}, …])
    """
    keep = [s for s in fired if docs.get(s["id"], {}).get("exclusive") is True]
    if not keep or len(keep) == len(fired):
        return fired, []
    dropped = sorted(s["id"] for s in fired if s not in keep)
    return keep, [{"id": s["id"], "of": dropped} for s in keep]


def _apply_replacement(fired: list, docs: dict, ctx_values: dict):
    """`replaces` 선언 — 나열한 signature 가 **모두** 발화하면 그것들을 목록에서 지우고
    선언한 쪽이 **대신** 발화한다(자기 when_metric 이 성립하지 않아도 발화한다).

    `suppressed_by`(양보) · `hidden_by`(제거)와 다른 세 번째 관계다: 두 발화가 사실은
    **한 현상의 반쪽**이라 합쳐야 말이 되는 경우를 위한 것이다. 배포 룰에서는
    `BIDIR_TAIL ← [USL_TAIL, LSL_TAIL]` 하나가 쓴다 — 양쪽 꼬리가 함께 두꺼우면
    "USL 문제 + LSL 문제" 두 건이 아니라 분포가 양방향으로 퍼진 한 건이고, 한쪽 방향
    조치로는 해결되지 않는다(2026-08-19 사용자 요청).

    판정은 **원본 발화 집합 기준 1패스**다 — 억제와 같은 이유로 체인이 도는 것을 막는다.
    반환: (fired, [{"id": 대체한 id, "of": [대체된 id …]}, …])
    """
    fired_ids = {s["id"] for s in fired}
    replaced = []
    for sig_id, doc in docs.items():
        targets = _id_list(doc, "replaces")
        if not targets or not set(targets) <= fired_ids:
            continue
        drop = set(targets)
        fired = [s for s in fired if s["id"] not in drop]
        replaced.append({"id": sig_id, "of": sorted(drop)})
        if sig_id not in {s["id"] for s in fired}:
            fired.append({"id": sig_id, "status_hint": doc["status_hint"], "score": None,
                          "evidence": [_format_evidence(t, ctx_values)
                                       for t in doc.get("evidence", [])],
                          "action_ko": doc.get("action_ko"), "replaced": sorted(drop)})
    return fired, replaced


def _apply_hidden(fired: list, docs: dict):
    """`hidden_by` 선언 — 지정한 signature 가 함께 발화하면 **목록에서 통째로 제거**한다.

    `suppressed_by` 와 의도가 다르다. 억제는 "둘 다 사실이니 목록에는 남기고 대표만
    양보" 인데(2026-08-13 사용자 요구), 이쪽은 "구조적으로 늘 함께 뜨는데 같은 사실을 두 번
    말하는 것이라 아예 보이지 않아야 한다" 이다. 배포 룰에서는 `SPOT_FAIL ← [CENTER_FAIL]`
    하나가 쓴다(2026-08-19 사용자 결정).
    ⚠ 새 선언을 추가할 때는 **정말로 정보가 0 인가**를 먼저 보라 — 제거된 발화는 화면 어디에도
    남지 않아 사용자가 "왜 안 뜨나" 를 알 수 없다(트레이스에만 사유가 남는다).

    판정은 원본 발화 집합 기준 1패스. 반환: (남은 fired, [{"id":.., "by":[..]}, …])
    """
    fired_ids = {s["id"] for s in fired}
    kept, hidden = [], []
    for s in fired:
        by = [sid for sid in _id_list(docs.get(s["id"], {}), "hidden_by") if sid in fired_ids]
        if by:
            hidden.append({"id": s["id"], "by": sorted(by)})
        else:
            kept.append(s)
    return kept, hidden


def _apply_suppression(fired: list, suppressors: dict):
    """`suppressed_by` 선언을 **primary 양보**로 적용한다 (2026-08-13 의미 변경).

    "A 가 뜨면 B 는 군더더기" 라는 선언은 그대로 두되, **목록에서 지우지 않는다**.
    지우던 시절에는 "cpk 도 낮고 outlier 도 있다" 같은 케이스에서 한쪽이 통째로 사라져
    사용자가 볼 수 없었다(실사용 피드백: "여러 개 걸리면 중복해서 잘 안 나온다").
    지금은 발화 항목에 `demoted_by` 를 달기만 하고, `status.decide` 가 같은 severity 안에서
    **primary 후보에서만** 제외한다 — 결과 지표(cpk)가 원인 룰의 자리를 뺏지 않으면서
    두 현상이 모두 화면에 남는다.

    판정은 **원본 발화 집합 기준 1패스**다 — 전이(A→B→C)나 상호 참조로 체인이 도는 것을
    원천 차단하기 위해서다. (순환·미존재 id 검사는 룰 저장 시점 검증의 몫이다.)
    반환: (fired 전체, [{"id":..,"by":..}, ...]) — 두 번째 값은 트레이스·검증 표시용이며
    첫 번째 값에서 빠지지 않는다.
    """
    if not any(suppressors.values()):
        return fired, []
    fired_ids = {s["id"] for s in fired}
    demoted = []
    for s in fired:
        by = [sid for sid in suppressors.get(s["id"], []) if sid in fired_ids]
        if by:
            s["demoted_by"] = sorted(by)
            demoted.append({"id": s["id"], "by": sorted(by)})
    return fired, demoted


def _eval_condition(op_str, actual_value, thresholds):
    """'>key' / '<key' / 'abs>key' / '>0.5' 형태 해석. 결측이면 False.

    **목록을 주면 전부 만족해야 한다**(AND) — 같은 지표에 상·하한을 함께 거는 밴드용
    (`tail_mass_3s: [">=heavy_tail_mass_min", "<=heavy_tail_mass_max"]`). when_metric 은
    dict 라 한 지표에 조건 하나만 쓸 수 있었는데, "너무 작지도 크지도 않을 것" 이 판정
    기준인 룰(HEAVY_TAIL 의 꼬리 질량)이 생겨 확장했다.
    """
    if isinstance(op_str, (list, tuple)):
        return all(_eval_condition(one, actual_value, thresholds) for one in op_str)
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

    # 꼬리 질량의 방향 **비중**(파생값, DB 저장 안 함) — USL_TAIL/LSL_TAIL 이 이것으로
    # 갈린다. 밴드는 총 질량(tail_mass_3s)에 그대로 걸리므로 판정 범위는 안 바뀌고,
    # 이 값은 "그 질량이 어느 쪽에 실렸나" 만 말한다. 꼬리가 없으면(합 0) 미정의 → 미발화.
    _tmh, _tml = features.get("tail_mass_3s_high"), features.get("tail_mass_3s_low")
    if _tmh is not None and _tml is not None:
        # 꼬리가 아예 없으면(합 0) 어느 쪽도 아니다 → 0.0. **키는 항상 채운다** — 결측이면
        # 조건이 조용히 False 가 되어 룰이 침묵하는데, 그건 "꼬리 없음" 과 구분이 안 된다.
        _tot = _tmh + _tml
        ctx_values["tail_side_share_high"] = (_tmh / _tot) if _tot > 0 else 0.0
        ctx_values["tail_side_share_low"] = (_tml / _tot) if _tot > 0 else 0.0

    _outlier_ratio, _n_dut = features.get("outlier_ratio"), features.get("n_dut")
    if _outlier_ratio is not None and _n_dut:
        ctx_values["outlier_count"] = round(_outlier_ratio * _n_dut)
    ctx_values["limit_missing"] = int(case_ctx.get("lsl") is None or case_ctx.get("usl") is None)
    # limit 이 **점**인가(LSL==USL) — FUNC_FAIL 판정용. 기능성 item 은 "0~0 에서 0 이어야
    # 통과" 처럼 폭이 0 인 limit 을 쓴다. 이때 산포 통계는 전부 무의미해지므로(spread_norm
    # 은 분모 0 이라 None, cpk 는 음수) 그 지표들로 만든 룰이 엉뚱하게 뜬다.
    # limit_missing 과 같은 이유로 **키는 항상 채운다** — 결측이면 조건이 조용히 False 가 된다.
    _lsl, _usl = case_ctx.get("lsl"), case_ctx.get("usl")
    ctx_values["limit_is_point"] = int(
        _lsl is not None and _usl is not None and _lsl == _usl)

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
            "applies": {}, "suppressed": [], "hidden": [], "replaced": [],
            "exclusive": [], "excluded": excluded,
            "raw_metrics_snapshot": {"cpk": raw_metrics.get("cpk"),
                                     "yield": raw_metrics.get("yield")},
        }

    th = thresholds_for(case_ctx)
    ctx_values = build_ctx_values(case_ctx, features, raw_metrics)

    n_dut = features.get("n_dut") or 0
    high_moment_ok = n_dut >= th["n_min"]

    fired, applies = [], {}
    docs = {}                            # {id: 룰 선언} — 관계 선언(억제/제거/대체) 조회용
    subpop_doc = unknown_doc = None
    for sig in signatures_for(case_ctx):
        # yaml 의 enabled:false 는 룰 비활성 (키 부재 = 활성 — 기존 yaml 무영향)
        if sig.get("enabled") is False:
            continue
        # scope 는 enabled 다음, 특수분기보다 앞 — "이 제품군에서 안 쓰는 룰" 은 BIMODALITY 도 예외 아님
        if not scope_matches(sig, case_ctx):
            continue
        docs[sig["id"]] = sig
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

    if subpop_doc is not None:
        subpop = _evaluate_subpop_gap(features)
        applies[f"{_BIMODALITY_ID}.modality_v2"] = features.get("modality_v2") is not None
        if subpop is not None:
            fired.append({"id": _BIMODALITY_ID, "status_hint" : subpop_doc["status_hint"],
                          "score":None, "evidence" : subpop["evidence"], "action_ko":subpop_doc.get("action_ko"), "modality_v2":subpop["modality_v2"]})

    # 관계 4종은 **순서가 의미를 가진다**: 해석을 선점한 것이 있으면 나머지를 통째로 지우고
    # (단독) → 합칠 것을 합치고(대체) → 감출 것을 감춘 뒤(제거) → 남은 것들 사이에서 대표를
    # 정한다(양보). 순서를 바꾸면 이미 사라진 발화가 남은 발화를 눌러 아무도 primary 가
    # 아닌 상태가 생기고, 단독을 대체 뒤로 미루면 합성된 발화가 단독을 통과해 버린다.
    fired, exclusive = _apply_exclusive(fired, docs)
    fired, replaced = _apply_replacement(fired, docs, ctx_values)
    fired, hidden = _apply_hidden(fired, docs)
    fired, suppressed = _apply_suppression(
        fired, {s["id"]: _suppressor_ids(docs.get(s["id"], {})) for s in fired})

    # UNKNOWN 은 최종 발화 집합이 비었을 때만 붙는다. (양보는 목록에서 지우지 않으므로
    # 이 시점의 fired 는 조건을 만족한 룰 전부다 — 2026-08-13 의미 변경 후에도 동일.)
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
        "hidden": hidden, "replaced": replaced, "exclusive": exclusive,
        "raw_metrics_snapshot": {"cpk": raw_metrics.get("cpk"),
                                 "yield": raw_metrics.get("yield")},
    }

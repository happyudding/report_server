"""L3 signatures + L4 status 단독 테스트.

signatures.evaluate 는 DB 미접근(bin_taxonomy 는 rules yaml 조회) — DB fixture 불필요.
"""
from eval_engine.pipeline import features, metrics, present, signatures, status


def _case(**kw):
    """발화가 없는 중립 case_ctx. kw 로 필요한 축만 바꿔 그 signature 하나만 겨냥한다."""
    # lsl/usl 은 MISSING_LIMIT 비발화용 — 없으면 모든 case 에 MINOR 가 하나 깔린다.
    c = {"product_type": None, "item_class": None, "bin": 99, "lsl": 0.0, "usl": 10.0}
    c.update(kw)
    return c


def _full_features(**kw):
    """공간 feature 포함(full completeness) 기본 features."""
    f = {"spread_norm": 0.05, "skewness": 0.1, "kurtosis": 0.0, "outlier_ratio": 0.0,
         "spec_margin_low": 5.0, "spec_margin_high": 5.0, "site_cpk_delta": 0.0,
         "edge_fail_ratio": 1.0, "n_dut": 100}
    f.update(kw)
    return f


def test_gross_fail_fires_on_low_yield():
    case = _case()
    feats = _full_features()
    raw = {"yield": 0.3, "cpk": 1.5}
    sig = signatures.evaluate(case, feats, raw)
    ids = [s["id"] for s in sig["signatures"]]
    assert "GROSS_FAIL" in ids
    verdict = status.decide(case, feats, sig)
    assert verdict["status"] == "CRITICAL"


def test_outlier_needs_both_distance_and_gap():
    """현행 OUTLIER 는 **거리 AND 끊김** 두 조건이다 (끊김 지표 교체 2026-08-14).

    거리만 보면 "꼬리가 길어 규격을 넘은 것"(HEAVY_TAIL)까지 outlier 로 잡힌다 —
    실측에서 더 멀리 나간 항목이 오히려 heavy tail 이었다. 몸통과 끊겼는지를 함께 본다.
    끊김은 `fail_body_jump_ratio`(같은 쪽에서 몸통~최근접 fail 구간의 최대 빈 폭 비율)로
    잰다. 구 `fail_pass_gap_sigma` 는 양쪽 꼬리 |z| 를 섞어 재서 판정에서 뺐다.
    """
    case = _case()
    raw = {"yield": 0.95, "cpk": 1.5}
    fired = lambda f: [s["id"] for s in signatures.evaluate(case, f, raw)["signatures"]]

    assert "OUTLIER" in fired(_full_features(fail_mad_min=10.0, fail_body_jump_ratio=0.9))
    # 멀지만 꼬리가 이어져 있다 → outlier 아님(HEAVY_TAIL 축)
    assert "OUTLIER" not in fired(_full_features(fail_mad_min=16.0, fail_body_jump_ratio=0.2))
    # 끊겼지만 limit 바로 밖이라 가깝다 → outlier 아님(공정능력 축)
    assert "OUTLIER" not in fired(_full_features(fail_mad_min=3.0, fail_body_jump_ratio=0.9))
    # 구 지표는 이제 판정에 관여하지 않는다 — 값이 커도 끊김 비율이 낮으면 미발화
    assert "OUTLIER" not in fired(_full_features(fail_mad_min=10.0, fail_pass_gap_sigma=9.0,
                                                 fail_body_jump_ratio=0.1))


def test_outlier_keeps_low_cpk_and_tail_in_list():
    """동반 발화는 **목록에 남는다** — primary 만 원인 룰에 양보한다 (2026-08-13).

    지우던 시절에는 "cpk 도 낮고 outlier 도 있다" 가 한 줄로만 보여, 사용자가 다른 하나를
    영영 볼 수 없었다. 지금은 둘 다 보이고 대표만 OUTLIER 다.
    """
    case = _case()
    feats = _full_features(fail_mad_min=10.0, fail_body_jump_ratio=0.9,
                           kurtosis=15.0, tail_mass_3s=0.02, tail_mass_3s_high=0.02,
                           tail_mass_3s_low=0.0, n_dut=100)
    sig = signatures.evaluate(case, feats, {"yield": 0.95, "cpk": 0.8})
    ids = [s["id"] for s in sig["signatures"]]
    assert {"OUTLIER", "LOW_CPK", "USL_TAIL"} <= set(ids)
    # 양보 표시는 남되 목록에서 빠지지는 않는다
    assert {"LOW_CPK", "USL_TAIL"} <= {s["id"] for s in sig["suppressed"]}
    assert status.decide(case, feats, sig)["primary_signature"] == "OUTLIER"


def test_tail_risk_disabled_when_few_samples():
    case = _case()
    # skewness 큼 + spec margin 작음 → 정상이면 TAIL_RISK 발화. 단 n_dut < n_min 이면 비활성
    feats = _full_features(skewness=2.0, spec_margin_low=0.5, n_dut=5)
    raw = {"yield": 0.95, "cpk": 1.5}
    sig = signatures.evaluate(case, feats, raw)
    assert "TAIL_RISK" not in [s["id"] for s in sig["signatures"]]


def test_tail_risk_fires_with_enough_samples():
    case = _case()
    # 2026-08-12: TAIL_RISK 의 지표가 비모수 왜도(`skewness`, 상한 1.0 이라 임계 1.0 을
    # 넘을 수 없었다) → 모멘트 왜도(`skewness_moment`, 상한 없음)로 교체됐다.
    feats = _full_features(skewness_moment=2.0, spec_margin_low=0.5, n_dut=100)
    raw = {"yield": 0.95, "cpk": 1.5}
    sig = signatures.evaluate(case, feats, raw)
    assert "TAIL_RISK" in [s["id"] for s in sig["signatures"]]


def test_specificity_picks_equipment_over_general():
    case = _case()
    # LOW_CPK(일반) + EQUIPMENT_SUSPECT(구체) 동시 발화 → primary=EQUIPMENT
    feats = _full_features(site_cpk_delta=0.8)
    raw = {"yield": 0.95, "cpk": 0.9}
    sig = signatures.evaluate(case, feats, raw)
    ids = [s["id"] for s in sig["signatures"]]
    assert "EQUIPMENT_SUSPECT" in ids and "LOW_CPK" in ids
    verdict = status.decide(case, feats, sig)
    assert verdict["primary_signature"] == "EQUIPMENT_SUSPECT"


def test_trump_low_cpk_low_yield_forces_critical():
    case = _case()
    feats = _full_features()  # 발화 signature 없음 → 기본 MONITOR
    raw = {"yield": 0.6, "cpk": 0.5}  # cpk<cpk_bad(1.0) AND yield<floor(0.7)
    sig = signatures.evaluate(case, feats, raw)
    verdict = status.decide(case, feats, sig)
    assert verdict["status"] == "CRITICAL"


def test_data_completeness_levels():
    case = _case()
    raw = {"yield": 0.95, "cpk": 1.5}
    # full: 공간 있고 n_dut 충분
    v_full = status.decide(case, _full_features(n_dut=100), signatures.evaluate(
        case, _full_features(n_dut=100), raw))
    assert v_full["data_completeness"] == "full"
    # partial: 공간 없음
    feats_p = _full_features(n_dut=100, edge_fail_ratio=None)
    v_part = status.decide(case, feats_p, signatures.evaluate(case, feats_p, raw))
    assert v_part["data_completeness"] == "partial"
    # low: n_dut=0
    feats_l = _full_features(n_dut=0, edge_fail_ratio=None)
    v_low = status.decide(case, feats_l, signatures.evaluate(case, feats_l, raw))
    assert v_low["data_completeness"] == "low"


def test_no_signature_full_data_gives_ok():
    case = _case()
    feats = _full_features()  # 공간 포함 full completeness
    raw = {"yield": 0.99, "cpk": 2.0}
    sig = signatures.evaluate(case, feats, raw)
    assert sig["signatures"] == []
    verdict = status.decide(case, feats, sig)
    assert verdict["status"] == "OK"          # 정상 확정 (signature 0건 + full)
    assert verdict["primary_signature"] is None


def test_no_signature_incomplete_data_keeps_monitor():
    case = _case()
    raw = {"yield": 0.99, "cpk": 2.0}
    # partial(공간 결측) — 결측을 양호로 오판하지 않음
    feats_p = _full_features(edge_fail_ratio=None)
    v_p = status.decide(case, feats_p, signatures.evaluate(case, feats_p, raw))
    assert v_p["status"] == "MONITOR"
    # low(n_dut=0)
    feats_l = _full_features(n_dut=0, edge_fail_ratio=None)
    v_l = status.decide(case, feats_l, signatures.evaluate(case, feats_l, raw))
    assert v_l["status"] == "MONITOR"


def test_missing_limit_fires_without_spec():
    case = _case(lsl=None, usl=None)
    feats = _full_features()
    raw = {"yield": 0.99, "cpk": 2.0}
    sig = signatures.evaluate(case, feats, raw)
    assert "MISSING_LIMIT" in [s["id"] for s in sig["signatures"]]
    assert status.decide(case, feats, sig)["status"] == "MINOR"


def test_spot_fail_fires_on_tight_fail_group():
    """fail 좌표가 좁게 뭉치면 위치·모양과 무관하게 발화한다 (구 SPOT_CLUSTER)."""
    case = _case()
    raw = {"yield": 0.95, "cpk": 1.5}
    tight = _full_features(fail_spread_norm=0.10, fail_count=40)
    assert "SPOT_FAIL" in [s["id"] for s in signatures.evaluate(case, tight, raw)["signatures"]]
    # 웨이퍼 전면에 흩어지면 몰림이 아니다
    spread = _full_features(fail_spread_norm=0.60, fail_count=40)
    assert "SPOT_FAIL" not in [s["id"]
                               for s in signatures.evaluate(case, spread, raw)["signatures"]]
    # fail 이 적으면 우연히 몰릴 수 있어 판정하지 않는다(spatial_fail_count_min 가드)
    few = _full_features(fail_spread_norm=0.10, fail_count=4)
    assert "SPOT_FAIL" not in [s["id"]
                               for s in signatures.evaluate(case, few, raw)["signatures"]]


def test_spot_fail_hidden_when_center_fires():
    """중심부 뭉침에서는 CENTER_FAIL 만 남는다 — SPOT_FAIL 은 **목록에서 제거**된다.

    `suppressed_by`(목록 유지·primary 양보)와 다른 경로다(yaml `hidden_by`, 2026-08-19
    사용자 결정). 중심에 뭉친 fail 은 두 룰이 구조적으로 함께 뜨는데 같은 사실을 두 번
    말하는 셈이라, 조치가 분명한 CENTER 쪽만 보이게 한다.
    """
    case = _case()
    raw = {"yield": 0.95, "cpk": 1.5}
    both = _full_features(fail_spread_norm=0.10, fail_count=40, center_fail_share=1.0)
    sig = signatures.evaluate(case, both, raw)
    ids = [s["id"] for s in sig["signatures"]]
    assert "CENTER_FAIL" in ids
    assert "SPOT_FAIL" not in ids                       # 양보가 아니라 제거다
    assert sig["hidden"] == [{"id": "SPOT_FAIL", "by": ["CENTER_FAIL"]}]
    assert status.decide(case, both, sig)["primary_signature"] == "CENTER_FAIL"


def test_code_rail_fires_on_code_edge_hit():
    case = _case()
    feats = _full_features(code_edge_hit=0.10, limit_hit_ratio=0.10)
    raw = {"yield": 0.95, "cpk": 1.5}
    sig = signatures.evaluate(case, feats, raw)
    assert "CODE_RAIL" in [s["id"] for s in sig["signatures"]]


def test_code_rail_not_fires_when_feature_missing():
    case = _case()
    # code_edge_hit 는 CODE item 에만 계산 — None(비 CODE)이면 applies=False
    feats = _full_features(limit_hit_ratio=0.10)
    raw = {"yield": 0.95, "cpk": 1.5}
    sig = signatures.evaluate(case, feats, raw)
    assert "CODE_RAIL" not in [s["id"] for s in sig["signatures"]]


def test_directional_tail_fires_with_enough_samples():
    """구 HEAVY_TAIL 은 방향으로 갈렸다 — 꼬리 질량을 **어느 쪽에서 쟀나**로 룰이 나뉜다."""
    case = _case()
    raw = {"yield": 0.95, "cpk": 1.5}
    # 발화 조건은 구 HEAVY_TAIL 그대로 — kurtosis > 10 **AND 총 꼬리 질량 1~5%**.
    # 방향은 그 질량이 어느 쪽에 실렸나(tail_side_share_*)로만 가른다.
    high = _full_features(kurtosis=12.0, tail_mass_3s=0.02,
                          tail_mass_3s_high=0.02, tail_mass_3s_low=0.0, n_dut=100)
    ids = [s["id"] for s in signatures.evaluate(case, high, raw)["signatures"]]
    assert "USL_TAIL" in ids and "LSL_TAIL" not in ids
    low = _full_features(kurtosis=12.0, tail_mass_3s=0.02,
                         tail_mass_3s_high=0.0, tail_mass_3s_low=0.02, n_dut=100)
    ids = [s["id"] for s in signatures.evaluate(case, low, raw)["signatures"]]
    assert "LSL_TAIL" in ids and "USL_TAIL" not in ids
    # 반대쪽에 점 한둘이 섞인 정도(15%)는 여전히 한쪽 꼬리다
    mostly = _full_features(kurtosis=12.0, tail_mass_3s=0.02,
                            tail_mass_3s_high=0.017, tail_mass_3s_low=0.003, n_dut=100)
    ids = [s["id"] for s in signatures.evaluate(case, mostly, raw)["signatures"]]
    assert "USL_TAIL" in ids and "LSL_TAIL" not in ids


def test_both_tails_merge_into_bidir():
    """양쪽 꼬리가 함께 두꺼우면 **BIDIR_TAIL 하나**로 접힌다 (yaml `replaces`).

    "USL 문제 + LSL 문제" 두 건이 아니라 분포가 양방향으로 퍼진 한 건이고, 한쪽 방향
    조치로는 해결되지 않는다(2026-08-19 사용자 요청). BIDIR_TAIL 자신의 when_metric
    (양쪽 spec margin 부족)이 성립하지 않아도 대신 발화한다 — 여기 margin 은 5σ 다.
    """
    case = _case()
    feats = _full_features(kurtosis=12.0, tail_mass_3s=0.04,
                           tail_mass_3s_high=0.02, tail_mass_3s_low=0.02, n_dut=100)
    # 총 질량은 밴드(1~5%) 안이고 양쪽이 반씩 가졌다
    sig = signatures.evaluate(case, feats, {"yield": 0.95, "cpk": 1.5})
    ids = [s["id"] for s in sig["signatures"]]
    assert "BIDIR_TAIL" in ids
    assert "USL_TAIL" not in ids and "LSL_TAIL" not in ids
    assert sig["replaced"] == [{"id": "BIDIR_TAIL", "of": ["LSL_TAIL", "USL_TAIL"]}]
    assert status.decide(case, feats, sig)["primary_signature"] == "BIDIR_TAIL"


def test_directional_tail_disabled_when_few_samples():
    case = _case()
    # 고차모멘트 min-n 가드
    feats = _full_features(kurtosis=12.0, tail_mass_3s=0.02, tail_mass_3s_high=0.02,
                           tail_mass_3s_low=0.0, n_dut=5)
    raw = {"yield": 0.95, "cpk": 1.5}
    sig = signatures.evaluate(case, feats, raw)
    assert "USL_TAIL" not in [s["id"] for s in sig["signatures"]]


def test_pf_trump_low_yield_forces_critical():
    """PF 는 cpk 가 없어 기존 trump 불가 — yield 단독(gross_yield_bad)으로 CRITICAL."""
    case = _case(value_type="PF")
    feats = _full_features()
    raw = {"yield": 0.3, "cpk": None}
    sig = signatures.evaluate(case, feats, raw)
    verdict = status.decide(case, feats, sig)
    assert verdict["status"] == "CRITICAL"
    # 수율 양호한 PF 는 승격되지 않는다
    raw_ok = {"yield": 0.95, "cpk": None}
    v_ok = status.decide(case, feats, signatures.evaluate(case, feats, raw_ok))
    assert v_ok["status"] != "CRITICAL"


def test_ok_reachable_when_no_fail_and_no_spatial():
    """fail 0 인 케이스는 공간 feature 부재가 결측이 아니다 → full/OK 도달 가능.

    fail 정보가 아예 없으면(모름) 종전대로 partial 유지(양호 오판 금지).
    """
    raw = {"yield": 1.0, "cpk": 2.0}
    feats = _full_features(edge_fail_ratio=None)
    # fail_count=0 명시 → full → OK
    case0 = _case(fail_count=0, fail_mask=[])
    v0 = status.decide(case0, feats, signatures.evaluate(case0, feats, raw))
    assert v0["data_completeness"] == "full"
    assert v0["status"] == "OK"
    # fail 있음 + 공간 없음 → 종전대로 partial/MONITOR
    case1 = _case(fail_mask=[True, False])
    v1 = status.decide(case1, feats, signatures.evaluate(case1, feats, raw))
    assert v1["data_completeness"] == "partial"
    assert v1["status"] == "MONITOR"


def test_subpop_evidence_signal_codes_match_values():
    """DENSITY_GAP evidence 에는 density_gap 값이 실려야 한다 (구: cdf_gap 오라벨)."""
    feats = {"modality_v2": "bimodal", "n_modes": 2, "density_gap": 0.6,
             "cdf_gap": 42.0, "value_gap_ratio": 0.5}
    sub = signatures._evaluate_subpop_gap(feats)
    by_code = {e["signal_code"]: e for e in sub["evidence"]}
    assert by_code["DENSITY_GAP"]["value"] == 0.6
    assert "density_gap" in by_code["DENSITY_GAP"]["note"]
    assert by_code["VALUE_GAP"]["value"] == 0.5


# --- BIMODALITY: 측정값 → features → signatures 전 구간 ------------------------------
#
# 위 test_subpop_evidence_signal_codes_match_values 는 features dict 를 손으로 주입해
# `_evaluate_subpop_gap` 하나만 본다. 아래 두 건은 실제 측정값에서 출발해 배선까지 본다 —
# feature 키 이름이 바뀌거나 compute 가 파생값을 안 실어주면 여기서만 깨진다.

def _measured_case(values):
    """측정값만으로 만든 case_ctx(공간·site 결측). limit 은 값 범위를 감싸 MISSING_LIMIT 회피."""
    n = len(values)
    return {"values": values, "lsl": 0.0, "usl": 10.0, "value_type": "V", "bin": 99,
            "x_pos": [None] * n, "y_pos": [None] * n, "site": [None] * n,
            "fail_mask": [False] * n, "product_type": None, "item_class": None}


def _bump(center, step, weights):
    """center 를 봉우리로 하는 이산 무리. 히스토그램 내부에 봉우리가 생기도록 폭을 준다."""
    return [x for k, w in enumerate(weights)
            for x in [center + (k - (len(weights) - 1) // 2) * step] * w]


_BUMP_W = [2, 4, 8, 12, 8, 4, 2]   # 무리 1개당 40개


def test_subpop_gap_fires_end_to_end_on_two_clusters():
    """분리된 두 무리는 compute→evaluate 를 거쳐 BIMODALITY 로 발화한다.

    evidence 의 값이 features 실값과 같은지까지 확인한다 — DENSITY_GAP 라벨에 다른 지표를
    싣던 과거 오라벨(2026-08-03 수정)이 재발하면 여기서 잡힌다.
    """
    values = _bump(2.0, 0.25, _BUMP_W) + _bump(8.0, 0.25, _BUMP_W)
    case = _measured_case(values)
    raw = metrics.compute(case)
    feats = features.compute(case, raw, "ev1")
    assert feats["modality_v2"] == "bimodal"

    sig = signatures.evaluate(case, feats, raw)
    fired = {s["id"]: s for s in sig["signatures"]}
    assert "BIMODALITY" in fired
    assert fired["BIMODALITY"]["modality_v2"] == "bimodal"
    by_code = {e["signal_code"]: e["value"] for e in fired["BIMODALITY"]["evidence"]}
    assert by_code["DENSITY_GAP"] == feats["density_gap"]
    assert by_code["VALUE_GAP"] == feats["value_gap_ratio"]
    assert by_code["N_MODES"] == feats["n_modes"]


def test_subpop_gap_silent_on_quantized_values():
    """이산(양자화) 값은 발화하지 않는다 — 구 cdf_gap 지표의 오발화 회귀 방지선.

    [0,1,2] 반복은 cdf_gap 이 30% 를 넘어(test_features 참조) 예전 판정으로는 분리로
    읽히던 입력이다. 지금은 modality_v2 가 서지 않아 applies 도 False 로 남는다.
    """
    case = _measured_case([0.0, 1.0, 2.0] * 20)
    raw = metrics.compute(case)
    feats = features.compute(case, raw, "ev1")
    assert feats["modality_v2"] is None

    sig = signatures.evaluate(case, feats, raw)
    assert "BIMODALITY" not in [s["id"] for s in sig["signatures"]]
    assert sig["applies"]["BIMODALITY.modality_v2"] is False


def test_build_ctx_values_covers_every_referenced_metric():
    """when_metric 이 참조하는 이름은 전부 ctx_values 로 조립돼야 판정이 성립한다.

    build_ctx_values 는 관리자 트레이스(/pe/eval)가 파생값 재구현을 피하려고 공개한
    함수다. 여기서 빠진 키는 "결측 → 조건 False" 로 조용히 흘러 룰이 영구 침묵한다.
    파생값(spec_margin_min/center_bias/outlier_count/limit_missing/gradient_norm_abs_max)
    이 features 에 없고 여기서만 만들어지므로 특히 중요하다.
    """
    values = _bump(2.0, 0.25, _BUMP_W) + _bump(8.0, 0.25, _BUMP_W)
    case = _measured_case(values)
    case.update(x_pos=[i % 10 for i in range(len(values))],
                y_pos=[i // 10 for i in range(len(values))],
                fail_mask=[i % 20 == 0 for i in range(len(values))])
    raw = metrics.compute(case)
    feats = features.compute(case, raw, "ev1")
    ctx = signatures.build_ctx_values(case, feats, raw)

    referenced = {m for sig in signatures.signatures_doc()["signatures"]
                  for m in (sig.get("when_metric") or {})}
    assert referenced - set(ctx) == set()


def test_should_store_covers_rule_only_case():
    """수율·cpk 는 정상인데 signature 만 발화한 케이스도 저장(=코멘트 생성) 대상.

    이게 없으면 분포만 이상한 item(BIMODALITY 등)은 코멘트가 아예 안 만들어져
    룰 디버깅이 불가능하다. report_server 는 이 부류를 Issue Table ETC 로 올린다.
    """
    case, m = _case(), {"fail_count": 0, "cpk": 2.0}
    assert not present.should_store(case, m, {"signatures": []})
    assert present.should_store(case, m, {"signatures": [{"id": "SUBPOP_GAP"}]})
    # 종전 조건 2개는 그대로 — 발화가 없어도 저장된다.
    assert present.should_store(case, {"fail_count": 3, "cpk": 2.0}, {"signatures": []})
    assert present.should_store(case, {"fail_count": 0, "cpk": 0.9}, {"signatures": []})

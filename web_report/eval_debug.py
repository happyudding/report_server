"""eval_analyzer 룰 리로드 + L0~L6 트레이스 — eval_engine 통합 접점 3/3.

report_server → eval_analyzer 는 단방향 의존이며 eval_engine import 는
ai_comment.py(운영 평가) / eval_export.py(코멘트 export) / **이 모듈(관리자 디버그)**
3곳에서만 허용된다 (docs/13_eval_analyzer_integration.md §2).

이 모듈은 `/pe/eval` 관리자 패널 전용이며 운영 조회 경로에서는 rules_rev() 만
쓰인다(cache_policy.report_key — 룰 편집 시 ai_comment 세션 캐시 무효화).

엔진 사설 API 핀(엔진 변경 시 함께 확인):
  pipeline.signatures.build_ctx_values / _eval_condition / _HIGH_MOMENT_METRICS
  pipeline.signatures._BIMODALITY_ID  ← subpop_gap_id() 로 패널에 노출(특수분기 표시)
  pipeline.signatures._UNKNOWN_ID / fail_count_of / _evaluate_unknown 의 evidence 포맷
      ← unknown_id() 노출 + _coverage 사유별 집계(UNKNOWN_<사유> signal_code)
  pipeline.signatures.signatures_for  ← 트레이스가 평가와 같은 스코프 병합 결과를 봐야 한다
  pipeline._rules.thresholds_for / signatures_doc / reload_rules / threshold_overlay_path
  pipeline._rules.signatures_for / signature_overrides / signature_overlay_path
  pipeline.status.SPECIFICITY_ORDER
  pipeline.features._classify_modality_v2  ← _subpop_conditions 가 AND 체인을 미러링한다
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_EVAL_DIR = Path(__file__).resolve().parent.parent / "eval_analyzer"

# 룰 편집 카운터 — 캐시 키 토큰. 파일이 없으면 "" (패널 미사용 서버 = 종전 키 유지).
REV_FILENAME = ".rules_rev"


def _eval_path():
    """eval_analyzer 를 sys.path 에 추가 (ai_comment._evaluate_fn 과 같은 후순위 append)."""
    path = str(_EVAL_DIR)
    if path not in sys.path:
        sys.path.append(path)


def rules_dir() -> Path:
    """엔진이 실제로 읽는 rules 디렉토리 (env EVAL_RULES_DIR 반영)."""
    _eval_path()
    from eval_engine import config
    return Path(config.RULES_DIR)


def rules_files() -> dict:
    """패널이 다루는 룰 파일 경로 모음."""
    _eval_path()
    from eval_engine import config
    return {"thresholds": Path(config.THRESHOLDS_FILE),
            "signatures": Path(config.SIGNATURES_FILE),
            "product_taxonomy": Path(config.PRODUCT_TAXONOMY_FILE),
            "exclusions": Path(config.EXCLUSIONS_FILE)}


def overlay_path(product_type, family_product=None) -> Path:
    """thresholds 오버레이 파일 경로 — 엔진 로더와 같은 규칙으로 산출."""
    _eval_path()
    from eval_engine.pipeline import _rules
    return Path(_rules.threshold_overlay_path(product_type, family_product))


def rules_rev() -> str:
    """룰 편집 rev 문자열. 파일이 없으면 "" — 캐시 키가 종전과 동일해진다.

    운영 조회 경로(cache_policy.report_key)에서 요청당 1회 호출되므로 예외는 삼킨다.
    """
    try:
        return (rules_dir() / REV_FILENAME).read_text(encoding="utf-8").strip()
    except (OSError, ValueError, ImportError):
        return ""


def bump_rules_rev() -> str:
    """rev +1 후 새 값 반환. 룰 yaml 을 고친 직후 호출한다."""
    path = rules_dir() / REV_FILENAME
    try:
        current = int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        current = 0
    new = str(current + 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new + "\n", encoding="utf-8")
    return new


def reload_rules() -> None:
    """엔진 룰 yaml 캐시 클리어. mtime 키라 평시엔 불필요하지만 강제 리로드용."""
    _eval_path()
    from eval_engine.pipeline import _rules
    _rules.reload_rules()


def taxonomy() -> dict:
    """product_taxonomy.yaml → {product_type: [family_product, ...]} (엔진 검증표와 동일)."""
    _eval_path()
    from eval_engine import config
    from eval_engine.pipeline._rules import load_yaml
    doc = load_yaml(str(config.PRODUCT_TAXONOMY_FILE)) or {}
    families = doc.get("family_product") or {}
    return {str(pt): [str(v) for v in (families.get(pt) or [])]
            for pt in (doc.get("product_types") or [])}


def exclusions() -> dict:
    """rules/exclusions.yaml 정규화 — {"item_contains": [...], "units": [...]}."""
    _eval_path()
    from eval_engine.pipeline._rules import exclusions_doc
    doc = exclusions_doc()
    return {"item_contains": [str(v) for v in (doc.get("item_contains") or [])],
            "units": [str(v) for v in (doc.get("units") or [])]}


def signatures_raw() -> list:
    """signatures.yaml 의 signature 목록(파싱 결과 그대로 — 오버레이 미적용 기준값)."""
    _eval_path()
    from eval_engine.pipeline._rules import signatures_doc
    return list(signatures_doc().get("signatures") or [])


def signature_overlay_path(product_type, family_product=None) -> Path:
    """signature 오버레이 파일 경로 — 엔진 로더와 같은 규칙으로 산출."""
    _eval_path()
    from eval_engine.pipeline import _rules
    return Path(_rules.signature_overlay_path(product_type, family_product))


def signature_overrides(product_type, family_product=None) -> dict:
    """이 범위까지 병합된 signature 오버라이드 {id: {필드: 값}}."""
    _eval_path()
    from eval_engine.pipeline._rules import signature_overrides as _fn
    return dict(_fn(product_type, family_product))


def signatures_scoped(product_type, family_product=None) -> list:
    """엔진이 이 제품군/family 에서 실제로 평가할 signature 목록 (오버레이 병합 결과)."""
    _eval_path()
    from eval_engine.pipeline._rules import signatures_for
    return list(signatures_for({"product_type": product_type,
                                "family_product": family_product}))


def subpop_gap_id() -> str:
    """when_metric 을 쓰지 않는 특수분기 signature id (엔진 값 — 패널 하드코딩 방지).

    이 룰만 features.modality_v2 판정을 그대로 발화 근거로 쓰므로, 편집 화면이
    when_metric/evidence 를 "효력 없음" 으로 표시해야 한다(_subpop_conditions 참조).
    """
    _eval_path()
    from eval_engine.pipeline import signatures
    return str(signatures._BIMODALITY_ID)


def unknown_id() -> str:
    """미분류 명시 발화 signature id (엔진 값 — 패널 하드코딩 방지).

    이 룰도 when_metric 을 쓰지 않는 특수분기다 — 다른 룰이 하나도 안 떴을 때만 발화하므로
    편집 화면은 조건을 "효력 없음" 으로 표시해야 한다(subpop_gap_id 와 같은 취급).
    """
    _eval_path()
    from eval_engine.pipeline import signatures
    return str(signatures._UNKNOWN_ID)


def specificity_order() -> list:
    """status.py SPECIFICITY_ORDER — signature 목록과의 정합 검증용."""
    _eval_path()
    from eval_engine.pipeline import status
    return list(status.SPECIFICITY_ORDER)


def default_thresholds() -> dict:
    """thresholds.yaml 의 default 섹션 (오버레이 편집 가능 키의 정본)."""
    _eval_path()
    from eval_engine import config
    from eval_engine.pipeline._rules import load_yaml
    doc = load_yaml(str(config.THRESHOLDS_FILE)) or {}
    return dict(doc.get("default") or {})


def thresholds_doc() -> dict:
    """thresholds.yaml 전체 (default/product_type/calibration/item_class)."""
    _eval_path()
    from eval_engine import config
    from eval_engine.pipeline._rules import load_yaml
    return dict(load_yaml(str(config.THRESHOLDS_FILE)) or {})


def effective_thresholds(product_type, family_product=None, item_class=None) -> dict:
    """엔진이 실제 병합한 임계값 — 패널 '적용값' 표시용."""
    _eval_path()
    from eval_engine.pipeline._rules import thresholds_for
    return dict(thresholds_for({"product_type": product_type,
                                "family_product": family_product,
                                "item_class": item_class}))


# ── L0~L6 트레이스 ────────────────────────────────────────────────────────────

_COND_RE = re.compile(r"(abs)?\s*([<>]=?)\s*(.+)")


def _cond_detail(metric, cond, ctx_values, thresholds, eval_condition):
    """when_metric 조건 1개를 '실제값 vs 임계값' 으로 분해."""
    actual = ctx_values.get(metric)
    m = _COND_RE.match(str(cond).strip())
    ref = m.group(3).strip() if m else None
    ref_key, ref_value = None, None
    if ref is not None:
        if ref in thresholds:
            ref_key, ref_value = ref, thresholds[ref]
        else:
            try:
                ref_value = float(ref)
            except ValueError:
                ref_value = None
    return {"metric": metric, "cond": str(cond),
            "op": (m.group(2) if m else None), "abs": bool(m and m.group(1)),
            "actual": actual, "ref_key": ref_key, "ref_value": ref_value,
            "applies": actual is not None,
            "passed": bool(eval_condition(cond, actual, thresholds))}


def _subpop_cond(metric, op, actual, ref_key, ref_value, passed):
    """_cond_detail 과 같은 dict 모양 — 패널 condHtml 이 그대로 렌더한다."""
    return {"metric": metric, "cond": f"{op}{ref_key or ref_value}",
            "op": op, "abs": False, "actual": actual,
            "ref_key": ref_key, "ref_value": ref_value,
            "applies": actual is not None, "passed": bool(passed)}


def _subpop_conditions(features, thresholds):
    """BIMODALITY(특수분기)의 AND 체인을 조건행으로 분해 — "왜 안 잡혔나" 진단용.

    엔진 pipeline.features._classify_modality_v2 를 1:1 미러링한다(임계값은 키 이름으로만
    읽어 하드코딩하지 않는다). 엔진이 분기 구조를 바꾸면 여기도 함께 고쳐야 한다.
    """
    n_dut = features.get("n_dut")
    outlier = features.get("outlier_ratio")
    n_modes = features.get("n_modes")
    bc = features.get("bimodality_score")
    dgap = features.get("density_gap")
    vgap = features.get("value_gap_ratio")
    vmass = features.get("value_gap_minor_mass")

    def th(key):
        return thresholds.get(key)

    def ge(value, key):
        ref = th(key)
        return value is not None and ref is not None and value >= ref

    rows = [
        # 게이트 — 둘 중 하나라도 실패하면 modality_v2 는 무조건 None.
        _subpop_cond("n_dut (게이트)", ">=", n_dut, "subpop_n_min",
                     th("subpop_n_min"), ge(n_dut, "subpop_n_min")),
        # 엔진은 outlier_ratio 가 결측이면 게이트를 통과시킨다(None 은 차단하지 않음).
        _subpop_cond("outlier_ratio (게이트)", "<", outlier, "subpop_outlier_ratio_max",
                     th("subpop_outlier_ratio_max"),
                     outlier is None or (th("subpop_outlier_ratio_max") is not None
                                         and outlier < th("subpop_outlier_ratio_max"))),
        _subpop_cond("n_modes [multimodal]", ">=", n_modes, None, 3,
                     n_modes is not None and n_modes >= 3),
        _subpop_cond("density_gap [multimodal]", ">=", dgap, "subpop_density_gap_warn",
                     th("subpop_density_gap_warn"), ge(dgap, "subpop_density_gap_warn")),
        _subpop_cond("n_modes [bimodal]", "==", n_modes, None, 2, n_modes == 2),
        _subpop_cond("bimodality_score [bimodal]", ">=", bc, "bimodality_warn",
                     th("bimodality_warn"), ge(bc, "bimodality_warn")),
        _subpop_cond("density_gap [bimodal]", ">=", dgap, "subpop_density_gap_warn",
                     th("subpop_density_gap_warn"), ge(dgap, "subpop_density_gap_warn")),
        _subpop_cond("density_gap [separated]", ">=", dgap, "subpop_density_gap_strong",
                     th("subpop_density_gap_strong"), ge(dgap, "subpop_density_gap_strong")),
        # 2026-08-03 separated 판정이 cdf_gap(동일값 질량) → value_gap(값축 빈 구간)으로 교체됨
        _subpop_cond("value_gap_ratio [separated]", ">=", vgap, "subpop_value_gap_warn",
                     th("subpop_value_gap_warn"), ge(vgap, "subpop_value_gap_warn")),
        _subpop_cond("minor_mass [separated]", ">=", vmass, "subpop_minor_mass_min",
                     th("subpop_minor_mass_min"), ge(vmass, "subpop_minor_mass_min")),
    ]
    return rows


def condition_details(when_metric, ctx_values, thresholds):
    """when_metric 조건 전부를 '실제값 vs 임계값' 행으로 분해 — 트레이스와 같은 계산.

    `_cond_detail` 은 엔진 내부 함수(`signatures._eval_condition`)를 인자로 받으므로 밖에서
    부를 수 없다. 리포트의 Signature 근거 팝업(server/eval_panel/signature_reason.py)도 같은
    분해를 써야 `/pe/eval` 트레이스와 화면이 갈리지 않아 여기에 공개 래퍼를 둔다.
    """
    _eval_path()
    from eval_engine.pipeline import signatures as sig_mod
    return [_cond_detail(metric, cond, ctx_values, thresholds, sig_mod._eval_condition)
            for metric, cond in (when_metric or {}).items()]


def subpop_conditions(features, thresholds):
    """BIMODALITY 특수분기(when_metric 을 안 쓰는 유일한 룰)의 조건행 — 위와 같은 이유."""
    return _subpop_conditions(features, thresholds)


def _signature_matrix(case_ctx, features, ctx_values, thresholds, sig_result, sig_mod):
    """signature 21개 × (활성/스킵사유/조건분해/발화) 매트릭스."""
    fired_ids = {s["id"] for s in (sig_result.get("signatures") or [])}
    # 조건은 만족했지만 상위 룰(포함관계)에 가려진 것 — 조건만 보면 "떠야 하는데 안 떴다"
    # 로 읽히므로 사유를 함께 찍는다.
    suppressed_by = {row["id"]: row["by"] for row in (sig_result.get("suppressed") or [])}
    excluded = sig_result.get("excluded")
    n_dut = features.get("n_dut") or 0
    high_moment_ok = n_dut >= thresholds.get("n_min", 0)
    rows = []
    # 평가와 같은 목록을 봐야 한다 — 제품군 오버레이가 얹힌 결과(signatures_for).
    for sig in sig_mod.signatures_for(case_ctx):
        sig_id = sig.get("id")
        when = sig.get("when_metric") or {}
        enabled = sig.get("enabled") is not False
        # skip_reason = 평가에서 제외된 사유(조건 없음) / branch_note = 평가는 하되
        # when_metric 이 아닌 경로를 타는 사유(조건 있음). BIMODALITY 만 후자다.
        skip, branch = None, None
        if excluded:
            # 제외 목록 매칭 — 엔진(signatures.evaluate)이 룰 평가 자체를 건너뛴다.
            skip = f"평가 제외 목록 매칭 — {excluded} (모든 signature 미평가)"
        elif not enabled:
            # 엔진도 enabled:false 를 SUBPOP 특수분기보다 먼저 걸러낸다(signatures.py).
            skip = "disabled (yaml enabled:false)"
        elif not sig_mod.scope_matches(sig, case_ctx):
            scope = sig.get("scope") or {}
            skip = (f"scope 밖 (이 룰은 product_type={scope.get('product_type') or '전체'} / "
                    f"family_product={scope.get('family_product') or '전체'} 에만 적용 — "
                    f"이 세션은 {case_ctx.get('product_type')}/{case_ctx.get('family_product')})")
        elif sig_id == sig_mod._BIMODALITY_ID:
            branch = ("특수분기 (when_metric 미사용 — features.modality_v2 로 판정) → "
                      f"modality_v2={features.get('modality_v2') or '—'}")
        elif sig_id == sig_mod._UNKNOWN_ID:
            # 다른 룰이 하나도 안 떴을 때만 발화하는 특수분기 — 조건행이 없으므로
            # "왜 떴나/안 떴나" 를 여기서 문장으로 준다.
            fail_count = sig_mod.fail_count_of(case_ctx)
            branch = ("특수분기 (when_metric 미사용 — 다른 룰의 발화 0건 + fail 존재 시 발화) → "
                      + (f"fail {fail_count}chip" if fail_count
                         else "fail 없음(미발화)" if fail_count == 0 else "fail 정보 없음(미발화)"))
        elif not high_moment_ok and (set(when) & sig_mod._HIGH_MOMENT_METRICS):
            skip = f"min-n 가드 (n_dut {n_dut} < n_min {thresholds.get('n_min')})"
        elif sig_id in suppressed_by:
            branch = (f"{', '.join(suppressed_by[sig_id])} 발화 시 primary 를 양보한다 "
                      "(suppressed_by — 목록에는 남는다. 원인 룰이 대표가 되게 하는 장치)")
        # 조건행은 그 룰이 실제로 쓰는 판정 경로로 그린다 — BIMODALITY 만 when_metric 이
        # 아니라 modality_v2 체인이고, 나머지는(양보된 것 포함) when_metric 그대로다.
        conds = []
        if sig_id == sig_mod._BIMODALITY_ID and skip is None:
            conds = _subpop_conditions(features, thresholds)
        elif skip is None:
            conds = [_cond_detail(metric, cond, ctx_values, thresholds,
                                  sig_mod._eval_condition)
                     for metric, cond in when.items()]
        rows.append({"id": sig_id, "enabled": enabled, "skip_reason": skip,
                     "branch_note": branch, "scope": sig.get("scope") or {},
                     "status_hint": sig.get("status_hint"),
                     "issue_category": sig.get("issue_category") or "ETC",
                     "suppressed_by": suppressed_by.get(sig_id) or [],
                     "conditions": conds, "fired": sig_id in fired_ids})
    return rows


_ECDF_POINTS = 400            # 카드 1장이 그릴 ECDF 점 수 (300px 폭 — 1px 당 1점 이상)
_DIST_BINS = 60               # 배경 도수 막대
# 한 트레이스가 ECDF 점으로 쓸 수 있는 총량. 케이스 상한을 없앨 수 있게 되면서(전체 트레이스)
# "케이스당 상한" 만으로는 총 메모리가 케이스 수에 비례해 늘어난다 — trace_store 는 4런을
# 들고 있으므로 런 단위 예산을 둔다. 기본 400 케이스 × 400점이라 기본 트레이스는 전량 점을
# 받고, 예산을 넘긴 케이스는 막대만 싣는다(표시 품질만 하락).
_DIST_POINTS_BUDGET = 160_000


def _downsample_ecdf(values, cap):   # perf-guard: allow R01-dist-downsample (/pe/eval 트레이스 카드 전용 표시용 축약 — 리포트 조회·판정 경로와 무관, 전량은 Item Detail 링크로 넘긴다)
    """정렬된 values → 표시용 ECDF 점 (xs, ys). cap 초과분만 균등 stride 로 솎는다.

    첫/끝 점은 항상 포함하므로 양 꼬리와 ys[-1]=100% 가 보존된다.
    """
    n = len(values)
    if n <= cap:
        idx = range(n)
    else:
        step = n / cap
        idx = sorted({int(k * step) for k in range(cap)} | {n - 1})
    return [values[i] for i in idx], [(i + 1) / n * 100.0 for i in idx]


def _dist_payload(case, metrics, budget=None):
    """케이스 상세 미니차트용 분포 데이터 (관리자 디버그 표시 전용).

    수치 표(cpk/outlier_ratio/bimodality_score)만 보고 룰을 고치면 "왜 이 값이
    나왔는지"를 눈으로 확인할 수 없다. 값 분포를 ECDF 산점(선 없는 점)과 도수 막대로
    내려보내되, 카드 1장이 그릴 수 있는 점 수(`_ECDF_POINTS`)로 솎는다 — 전량 확인은
    카드의 링크가 여는 web_report Item Detail 의 몫이다. 판정에는 쓰이지 않는다.
    `budget` 은 [남은 점 예산] 1칸 리스트 — 소진되면 이후 케이스는 막대만 싣는다.
    """
    values = sorted(v for v in (case.get("values") or []) if v is not None)
    out = {"n": len(values), "lsl": case.get("lsl"), "usl": case.get("usl"),
           "mean": metrics.get("mean"), "median": None,
           "unit": case.get("unit"), "x": None, "y": None,
           "sampled": False, "hist": None}
    if not values:
        return out
    n = len(values)
    mid = n // 2
    out["median"] = (values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2)
    # 도수 막대는 크기와 무관하게 항상 싣는다 (프런트 폴백 계산 제거).
    lo, hi = values[0], values[-1]
    width = (hi - lo) / _DIST_BINS if hi > lo else 1.0
    counts = [0] * _DIST_BINS
    for v in values:
        counts[min(int((v - lo) / width), _DIST_BINS - 1)] += 1
    out["hist"] = {"lo": lo, "width": width, "counts": counts}

    want = min(n, _ECDF_POINTS)
    if budget is not None:
        if budget[0] < want:
            return out                       # 예산 소진 — 막대만
        budget[0] -= want
    out["x"], out["y"] = _downsample_ecdf(values, _ECDF_POINTS)
    out["sampled"] = n > _ECDF_POINTS
    return out


def _metrics_note(case):
    """L1 이 통계를 비운 이유 — "아무 판정도 안 받았다" 신고의 1순위 원인 설명.

    value_type=PF(양불)로 분류되면 L1/L2 가 cpk·stdev·spread_norm 을 전부 None 으로
    비우고, 그러면 모든 when_metric 조건이 결측→False 라 어떤 signature 도 발화하지
    못한다. 대부분은 UNIT 원문이 엔진 정확일치 표(ingest.UNIT_TO_VALUE_TYPE)에 없어
    PF 로 떨어진 오분류다.
    """
    if case.get("value_type") != "PF":
        return None
    return (f"value_type=PF(양불) — L1/L2 가 통계를 전부 비웁니다(cpk·stdev·산포 없음). "
            f"UNIT 원문 {case.get('unit')!r} 이 엔진 단위표에 없으면 여기로 떨어집니다. "
            f"측정값이 있는 항목이면 UNIT_TO_VALUE_TYPE 에 그 표기를 등록해야 판정됩니다.")


def _trace_case(case, engine_version, mods, dist_budget=None):
    """case 1건 L1~L6 — 게이팅 탈락 케이스도 그대로 담는다."""
    metrics, features, sig_mod, status_mod, present, recommend, rules = mods
    m = metrics.compute(case)
    f = features.compute(case, m, engine_version)
    th = rules.thresholds_for(case)
    ctx_values = sig_mod.build_ctx_values(case, f, m)
    sig = sig_mod.evaluate(case, f, m)
    verdict = status_mod.decide(case, f, sig)
    stored = bool(present.should_store(case, m, sig))

    precedents, comment = [], None
    if stored:
        try:
            precedents = recommend.find_precedents(case, sig) or []
            comment = recommend.make_comment(case, verdict, sig, precedents)
        except Exception:                                    # 선례 DB 부재 등
            logger.warning("trace: recommend 실패 (case=%s)", case.get("item_raw"),
                           exc_info=True)
    return {
        "item_raw": case.get("item_raw"), "item_canonical": case.get("item_canonical"),
        "item_class": case.get("item_class"), "bin": case.get("bin"),
        "value_type": case.get("value_type"), "unit": case.get("unit"),
        "metrics_note": _metrics_note(case),
        "status": verdict.get("status"),
        "primary_signature": verdict.get("primary_signature"),
        "secondary_signatures": verdict.get("secondary_signatures") or [],
        "confidence": verdict.get("confidence"),
        "data_completeness": verdict.get("data_completeness"),
        "stored": stored,
        "excluded": sig.get("excluded"),
        "gate_reason": None if stored else (
            f"평가 제외 목록 매칭 — {sig.get('excluded')} (signature 미평가·코멘트 미생성)"
            if sig.get("excluded") else
            f"should_store=False (fail_count={m.get('fail_count')}, "
            f"cpk={m.get('cpk')} >= cpk_warn={th.get('cpk_warn')}, "
            f"발화 signature 0건)"),
        "comment": comment,
        "raw_metrics": m, "features": f, "thresholds": th,
        "dist": _dist_payload(case, m, dist_budget),
        "ctx_values": {k: v for k, v in ctx_values.items() if not hasattr(v, "__len__")},
        "signature_matrix": _signature_matrix(case, f, ctx_values, th, sig, sig_mod),
        "evidence": verdict.get("evidence") or [],
        "precedents": precedents,
    }


def fail_only_default() -> bool:
    """서버 기본 평가 범위 (eval_panel 이 ai_comment 를 직접 import 하지 않도록 재노출)."""
    from . import ai_comment
    return ai_comment.fail_only_enabled()


def trace_session(session_id: str, *, report_db, upload_root: Path,
                  max_cases: int | None = 400, fail_only: bool | None = None) -> dict:
    """세션의 AI Comment 평가를 L0~L6 단계별로 재현한다 (관리자 디버그 전용).

    운영 조회(service.load_webreport)와 같은 변형 경로(loader.load_tables →
    mode_tables → ai_comment._table_to_raw_df)를 거치므로 결과가 Issue Table 의
    AI Comment 셀과 일치한다. evaluate() 대신 단계 함수를 직접 호출해
    raw_metrics/features/조건분해를 노출하고, **게이팅 탈락 케이스도 포함**한다.
    `max_cases=None` 이면 전 케이스를 담는다 — 그때도 분포 점은 런 단위 예산
    (_DIST_POINTS_BUDGET)으로 묶여 메모리가 케이스 수에 비례해 늘지 않는다.

    `fail_only=None` 이면 서버 기본(env WEB_REPORT_EVAL_FAIL_ONLY)을 따라 운영 경로와
    같은 item 집합을 본다. True/False 로 강제하면 범위를 바꿔 비교할 수 있다 — 그때
    반환 `fail_only` 가 달라지므로 호출측(패널)은 직전 run 과의 diff 를 건너뛰어야 한다
    (모집단이 달라 added/removed 가 오보가 된다).
    """
    from . import ai_comment
    from .loader import load_tables
    from .validation import mode_tables

    _eval_path()
    from eval_engine import config as eval_config
    from eval_engine.pipeline import (ingest, metrics, features, signatures, status,
                                      recommend, present, _rules)
    mods = (metrics, features, signatures, status, present, recommend, _rules)

    session, tables, manifest = load_tables(session_id, report_db=report_db,
                                            upload_root=upload_root)
    tables = mode_tables(tables, str(session.get("mode") or "Normal"))
    selected = {str(v) for v in (manifest.get("selected_items") or []) if str(v)}

    if fail_only is None:
        fail_only = ai_comment.fail_only_enabled()
    fail_set = ai_comment.eval_fail_scope(tables) if fail_only else None

    engine_version = eval_config.ENGINE_VERSION
    sources, cases, truncated = [], [], False
    item_total = 0
    dist_budget = [_DIST_POINTS_BUDGET]
    for idx, table in enumerate(tables):
        meta = ai_comment._session_meta(session, idx + 1)
        if meta is None:
            raise ValueError(f"product_type={session.get('product_type')!r} 는 평가 대상이 아님")
        items = ai_comment._eval_items(table, selected, fail_set)
        item_total += len(table.item_columns)
        sources.append({"index": idx, "source": table.source, "meta": meta,
                        "item_count": len(items),
                        "item_total": len(table.item_columns)})
        if not items:
            continue
        raw_df = ai_comment._table_to_raw_df(table, items)
        ingested = ingest.ingest({"meta": meta, "raw_df": raw_df}, persist=False)
        for case in ingested.get("cases") or []:
            if max_cases is not None and len(cases) >= max_cases:
                truncated = True
                break
            detail = _trace_case(case, engine_version, mods, dist_budget)
            detail["source"] = table.source
            detail["source_index"] = idx
            cases.append(detail)
        if truncated:
            break

    return {"session_id": session_id, "mode": session.get("mode") or "Normal",
            "product_type": session.get("product_type"),
            "family_product": session.get("family_product"),
            "engine_version": engine_version, "rules_rev": rules_rev(),
            "sources": sources, "cases": cases, "truncated": truncated,
            "max_cases": max_cases,
            "fail_only": bool(fail_only),
            "item_scope": {"evaluated": sum(s["item_count"] for s in sources),
                           "total": item_total},
            "coverage": _coverage(cases)}


def _coverage(cases) -> dict:
    """fail case 중 진단 signature 가 하나도 안 뜬 비율 — "현재 룰로 커버되나" 지표.

    엔진이 미분류 fail 에 `UNKNOWN` 을 명시 발화하므로 primary_signature 는 항상 채워진다.
    **UNKNOWN 은 커버로 세지 않는다** — 자동 발화를 성과로 세면 커버율이 가짜로 100% 가
    된다(docs/13 §6-3). 대신 `reasons` 로 "왜 못 떴나"(단위 미등록/limit 없음/표본 부족/
    조건 미달)를 함께 세서 무엇을 고쳐야 unknown 이 줄어드는지 보이게 한다.
    """
    unknown = unknown_id()
    fail_cases = [c for c in cases if (c.get("raw_metrics") or {}).get("fail_count")]
    unclassified = [c for c in fail_cases
                    if not c.get("primary_signature") or c["primary_signature"] == unknown]
    reasons = {}
    for c in unclassified:
        code = _unknown_reason_code(c, unknown) or "NO_MATCH"
        reasons[code] = reasons.get(code, 0) + 1
    return {"fail_cases": len(fail_cases), "fired": len(fail_cases) - len(unclassified),
            "unclassified": len(unclassified), "reasons": reasons}


def _unknown_reason_code(case, unknown):
    """UNKNOWN 발화 evidence 의 `UNKNOWN_<사유>` signal_code 에서 사유만 뽑는다.

    엔진 사설 계약 핀 — `signatures._evaluate_unknown` 의 evidence signal_code 포맷.
    포맷이 바뀌면 사유 없이 NO_MATCH 로 뭉뚱그려지므로 집계만 무뎌지고 화면은 산다.
    """
    for ev in case.get("evidence") or []:
        code = str(ev.get("signal_code") or "")
        if code.startswith("UNKNOWN_"):
            return code[len("UNKNOWN_"):]
    return None

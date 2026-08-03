"""eval_analyzer 룰 리로드 + L0~L6 트레이스 — eval_engine 통합 접점 3/3.

report_server → eval_analyzer 는 단방향 의존이며 eval_engine import 는
ai_comment.py(운영 평가) / eval_export.py(코멘트 export) / **이 모듈(관리자 디버그)**
3곳에서만 허용된다 (docs/13_eval_analyzer_integration.md §2).

이 모듈은 `/pe/eval` 관리자 패널 전용이며 운영 조회 경로에서는 rules_rev() 만
쓰인다(cache_policy.report_key — 룰 편집 시 ai_comment 세션 캐시 무효화).

엔진 사설 API 핀(엔진 변경 시 함께 확인):
  pipeline.signatures.build_ctx_values / _eval_condition / _HIGH_MOMENT_METRICS
  pipeline._rules.thresholds_for / signatures_doc / reload_rules / threshold_overlay_path
  pipeline.status.SPECIFICITY_ORDER
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
            "product_taxonomy": Path(config.PRODUCT_TAXONOMY_FILE)}


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


def signatures_raw() -> list:
    """signatures.yaml 의 signature 목록(파싱 결과 그대로)."""
    _eval_path()
    from eval_engine.pipeline._rules import signatures_doc
    return list(signatures_doc().get("signatures") or [])


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


def _signature_matrix(case_ctx, features, ctx_values, thresholds, sig_result, sig_mod):
    """signature 21개 × (활성/스킵사유/조건분해/발화) 매트릭스."""
    fired_ids = {s["id"] for s in (sig_result.get("signatures") or [])}
    n_dut = features.get("n_dut") or 0
    high_moment_ok = n_dut >= thresholds.get("n_min", 0)
    rows = []
    for sig in signatures_raw():
        sig_id = sig.get("id")
        when = sig.get("when_metric") or {}
        enabled = sig.get("enabled") is not False
        skip = None
        if not enabled:
            skip = "disabled (yaml enabled:false)"
        elif sig_id == sig_mod._SUBPOP_GAP_ID:
            skip = "특수분기 (when_metric 미사용 — features.modality_v2 로 판정)"
        elif not high_moment_ok and (set(when) & sig_mod._HIGH_MOMENT_METRICS):
            skip = f"min-n 가드 (n_dut {n_dut} < n_min {thresholds.get('n_min')})"
        conds = []
        if skip is None:
            conds = [_cond_detail(metric, cond, ctx_values, thresholds,
                                  sig_mod._eval_condition)
                     for metric, cond in when.items()]
        rows.append({"id": sig_id, "enabled": enabled, "skip_reason": skip,
                     "status_hint": sig.get("status_hint"),
                     "issue_category": sig.get("issue_category") or "ETC",
                     "conditions": conds, "fired": sig_id in fired_ids})
    return rows


def _trace_case(case, engine_version, mods):
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
        "status": verdict.get("status"),
        "primary_signature": verdict.get("primary_signature"),
        "secondary_signatures": verdict.get("secondary_signatures") or [],
        "confidence": verdict.get("confidence"),
        "data_completeness": verdict.get("data_completeness"),
        "stored": stored,
        "gate_reason": None if stored else
                       f"should_store=False (fail_count={m.get('fail_count')}, "
                       f"cpk={m.get('cpk')} >= cpk_warn={th.get('cpk_warn')})",
        "comment": comment,
        "raw_metrics": m, "features": f, "thresholds": th,
        "ctx_values": {k: v for k, v in ctx_values.items() if not hasattr(v, "__len__")},
        "signature_matrix": _signature_matrix(case, f, ctx_values, th, sig, sig_mod),
        "evidence": verdict.get("evidence") or [],
        "precedents": precedents,
    }


def trace_session(session_id: str, *, report_db, upload_root: Path,
                  max_cases: int = 400) -> dict:
    """세션의 AI Comment 평가를 L0~L6 단계별로 재현한다 (관리자 디버그 전용).

    운영 조회(service.load_webreport)와 같은 변형 경로(loader.load_tables →
    mode_tables → ai_comment._table_to_raw_df)를 거치므로 결과가 Issue Table 의
    AI Comment 셀과 일치한다. evaluate() 대신 단계 함수를 직접 호출해
    raw_metrics/features/조건분해를 노출하고, **게이팅 탈락 케이스도 포함**한다.
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

    engine_version = eval_config.ENGINE_VERSION
    sources, cases, truncated = [], [], False
    for idx, table in enumerate(tables):
        meta = ai_comment._session_meta(session, idx + 1)
        if meta is None:
            raise ValueError(f"product_type={session.get('product_type')!r} 는 평가 대상이 아님")
        items = [c for c in table.item_columns if not selected or c in selected]
        sources.append({"index": idx, "source": table.source, "meta": meta,
                        "item_count": len(items)})
        if not items:
            continue
        raw_df = ai_comment._table_to_raw_df(table, items)
        ingested = ingest.ingest({"meta": meta, "raw_df": raw_df}, persist=False)
        for case in ingested.get("cases") or []:
            if len(cases) >= max_cases:
                truncated = True
                break
            detail = _trace_case(case, engine_version, mods)
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
            "max_cases": max_cases}

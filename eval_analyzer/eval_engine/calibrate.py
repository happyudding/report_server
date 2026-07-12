"""오프라인 보정 — 누적 데이터에서 임계값(분위수) 산출 + thresholds.yaml 갱신.

docs(설계 본문):
  1. 분위수 보정(구현): features 를 item_class 별로 모아 분위수 →
     rules/thresholds.yaml 의 item_class override 갱신(estimator 표준값은 cold-start 시드).
     보정 대상 키·분위수는 thresholds.yaml 의 `calibration:` 섹션이 정본(코드 하드코딩 금지).
  2. comment 채굴: label.human_comment + case_outcome 군집 → signature 후보/키워드 사전. (후속)
  3. 검증: 룰 high-severity 판정 vs 실제 label/outcome 비교(precision/recall 유사). (후속)
출력: thresholds.yaml(item_class 섹션 재작성, 기존 수동 항목은 병합 보존) +
     engine_version_registry 신규 버전 등록(rules 파일 ref+sha256).

주의: features 는 (case, run, engine_version) grain — 재업로드/재계산 행도 표본에 포함된다.
"""
import hashlib
import time

import numpy as np
import yaml

from . import store, config
from .pipeline._rules import load_yaml


def _file_sha256(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _collect_by_item_class(product_type):
    """features ⨝ fail_case (+ product_type 필터) → {item_class: [Row, ...]}."""
    sql = """SELECT fc.item_class, f.* FROM features f
             JOIN fail_case fc ON fc.case_id = f.case_id"""
    params = ()
    if product_type:
        sql += """ JOIN product_master pm ON pm.product_name = fc.product_name
                   WHERE pm.product_type = ?"""
        params = (product_type,)
    groups = {}
    with store.get_conn() as conn:
        for r in conn.execute(sql, params):
            if r["item_class"]:
                groups.setdefault(r["item_class"], []).append(r)
    return groups


def _quantile_overrides(groups, spec, min_n):
    """item_class 별 분위수 계산. 표본(min_n) 미달 (item_class, feature) 는 skip.

    반환: (overrides, warnings). spec 의 feature 명이 features 컬럼에 없으면
    해당 키만 skip + warning (오타 하나로 전체 보정이 죽지 않도록).
    NaN 은 필터 불필요 — SQLite 가 NaN REAL 을 NULL 로 저장해 None 으로만 돌아온다.
    """
    overrides, warnings = {}, set()
    for ic, rows in sorted(groups.items()):
        vals_by_key = {}
        for key, cfg in spec.items():
            try:
                vals = [r[cfg["feature"]] for r in rows if r[cfg["feature"]] is not None]
            except IndexError:
                warnings.add(f"calibration.quantiles.{key}: features 에 없는 컬럼 "
                             f"{cfg['feature']!r} — skip")
                continue
            if len(vals) < min_n:
                continue
            arr = np.asarray(vals, dtype=float)
            if cfg.get("abs"):
                arr = np.abs(arr)
            vals_by_key[key] = round(float(np.quantile(arr, float(cfg["q"]))), 4)
        if vals_by_key:
            overrides[ic] = vals_by_key
    return overrides, sorted(warnings)


def _rewrite_item_class_section(path, item_class_map):
    """thresholds.yaml 의 item_class 섹션(파일 마지막 섹션 전제)만 재작성 — 위 주석/섹션 보존."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)
    idx = next((i for i, ln in enumerate(lines) if ln.startswith("item_class:")), None)
    if idx is not None:
        # 마지막 섹션 전제 검증 — 뒤에 다른 최상위 섹션이 있으면 소실 대신 실패
        trailing = [ln.split(":")[0] for ln in lines[idx + 1:]
                    if ln[:1].strip() and ln[:1] != "#" and ":" in ln]
        if trailing:
            raise ValueError(
                f"thresholds.yaml: item_class 뒤에 다른 최상위 섹션 {trailing} 발견 — "
                "item_class 가 마지막 섹션이어야 재작성 시 소실되지 않습니다.")
    head = "".join(lines if idx is None else lines[:idx])
    if idx is None and head and not head.endswith("\n"):
        head += "\n"
    block = yaml.safe_dump({"item_class": item_class_map or {}}, allow_unicode=True,
                           default_flow_style=False, sort_keys=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + block)


def recalibrate(*, product_type=None) -> dict:
    doc = load_yaml(str(config.THRESHOLDS_FILE))
    cal = doc.get("calibration") or {}
    spec = cal.get("quantiles") or {}
    min_n = int(cal.get("min_n", 30))
    if not spec:
        return {"engine_version": None, "item_class": {},
                "note": "thresholds.yaml 에 calibration.quantiles 스펙 없음"}
    if not config.DB_PATH.exists():
        return {"engine_version": None, "item_class": {},
                "note": f"eval.db 없음: {config.DB_PATH}"}

    groups = _collect_by_item_class(product_type)
    computed, warnings = _quantile_overrides(groups, spec, min_n)

    # 기존 수동/이전 보정 항목과 병합(신규 계산값 우선) — 표본 부족 항목을 지우지 않는다.
    existing = doc.get("item_class") or {}
    merged = {ic: dict(kv) for ic, kv in existing.items()}
    for ic, kv in computed.items():
        merged[ic] = {**merged.get(ic, {}), **kv}

    _rewrite_item_class_section(config.THRESHOLDS_FILE, merged)
    load_yaml.cache_clear()  # 재작성된 yaml 이 같은 프로세스에서 즉시 반영되도록

    engine_version = f"{config.ENGINE_VERSION}-cal{time.strftime('%Y%m%d')}"
    store.upsert_engine_version_registry(
        engine_version,
        thresholds_ref=str(config.THRESHOLDS_FILE),
        thresholds_hash=_file_sha256(config.THRESHOLDS_FILE),
        signatures_ref=str(config.SIGNATURES_FILE),
        signatures_hash=_file_sha256(config.SIGNATURES_FILE),
        taxonomy_ref=str(config.BIN_TAXONOMY_FILE),
        taxonomy_hash=_file_sha256(config.BIN_TAXONOMY_FILE))

    result = {"engine_version": engine_version, "min_n": min_n,
              "product_type": product_type,
              "n_item_class_sampled": len(groups), "item_class": computed,
              "thresholds_file": str(config.THRESHOLDS_FILE)}
    if warnings:
        result["warnings"] = warnings
    return result

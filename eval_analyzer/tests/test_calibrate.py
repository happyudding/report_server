"""calibrate.recalibrate — 누적 features 분위수 → thresholds.yaml item_class 갱신 + 버전 등록.

운영 rules/thresholds.yaml 을 절대 건드리지 않도록 tmp 복사본으로 격리한다.
"""
import shutil

import numpy as np
import pytest

from eval_engine import calibrate, config, store
from eval_engine.pipeline._rules import load_yaml, thresholds_for


def _tmp_thresholds(tmp_path, monkeypatch):
    """운영 thresholds.yaml 을 tmp 로 복사하고 config 가 복사본을 보게 함(원본 보호)."""
    dst = tmp_path / "thresholds.yaml"
    shutil.copy(config.THRESHOLDS_FILE, dst)
    monkeypatch.setattr(config, "THRESHOLDS_FILE", dst)
    load_yaml.cache_clear()
    return dst


def _seed_features(n, item_class="TRIM|V|18"):
    """fail_case + features n건 시드. spread_norm = 0.1 + i*0.01 (분위수 검증용 결정적 값)."""
    with store.get_conn() as conn:
        store.upsert_product_master({"product_name": "P1", "family_product": "SOC",
                                     "product_type": "PMIC"}, conn=conn)
        item_id = store.upsert_item_master("vref_trim", "VREF_TRIM", None, None, "TRIM",
                                           None, "V", None, conn=conn)
        run_id = store.create_ingest_run({"ingested_by": "test"}, conn=conn)
        for i in range(n):
            case_id = store.make_case_id("P1", f"L{i}", 1, item_id, 18, 0.0)
            store.upsert_fail_case(case_id, "P1", f"L{i}", 1, item_id, 18, 0.0,
                                   item_class, conn=conn)
            store.save_features(case_id, run_id, "ev1",
                                {"spread_norm": 0.1 + i * 0.01, "outlier_ratio": 0.01,
                                 "skewness": -0.5, "kurtosis": 1.0}, conn=conn)


def test_recalibrate_writes_item_class_overrides(fresh_db, tmp_path, monkeypatch):
    dst = _tmp_thresholds(tmp_path, monkeypatch)
    _seed_features(40)  # calibration.min_n(30) 이상
    result = calibrate.recalibrate()

    assert result["engine_version"].startswith(config.ENGINE_VERSION + "-cal")
    ov = result["item_class"]["TRIM|V|18"]
    arr = np.array([0.1 + i * 0.01 for i in range(40)])
    assert ov["spread_norm_warn"] == round(float(np.quantile(arr, 0.9)), 4)
    assert ov["skew_warn"] == 0.5  # abs: true — |-0.5| 의 분위수

    # 파일 반영: item_class override 가 로더 병합에서 우선 적용
    th = thresholds_for({"item_class": "TRIM|V|18", "product_type": None})
    assert th["spread_norm_warn"] == ov["spread_norm_warn"]
    # default 시드는 보존
    assert thresholds_for({})["cpk_warn"] == 1.33
    # item_class 위 주석/섹션(calibration 스펙 포함) 보존
    text = dst.read_text(encoding="utf-8")
    assert "calibration:" in text and "cold-start" in text

    with store.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM engine_version_registry WHERE engine_version=?",
            (result["engine_version"],)).fetchone()
    assert row is not None
    assert len(row["thresholds_hash"]) == 64  # sha256


def test_recalibrate_skips_small_samples(fresh_db, tmp_path, monkeypatch):
    _tmp_thresholds(tmp_path, monkeypatch)
    _seed_features(5)  # min_n 미달 → override 없음
    result = calibrate.recalibrate()
    assert result["item_class"] == {}


def test_recalibrate_preserves_existing_manual_overrides(fresh_db, tmp_path, monkeypatch):
    dst = _tmp_thresholds(tmp_path, monkeypatch)
    with open(dst, encoding="utf-8") as f:
        text = f.read()
    with open(dst, "w", encoding="utf-8") as f:  # 수동 override 를 미리 심어둠
        f.write(text.replace("item_class: {}",
                             'item_class:\n  "NON_TRIM|A|3": {cpk_warn: 1.5}\n'))
    load_yaml.cache_clear()
    _seed_features(40)
    result = calibrate.recalibrate()
    assert "TRIM|V|18" in result["item_class"]
    th = thresholds_for({"item_class": "NON_TRIM|A|3"})
    assert th["cpk_warn"] == 1.5  # 표본 없는 수동 항목은 병합 보존


def test_recalibrate_nan_features_do_not_poison_thresholds(fresh_db, tmp_path, monkeypatch):
    """NaN feature 행이 섞여도 분위수가 NaN 으로 오염되지 않는다.
    (SQLite 가 NaN REAL 을 NULL 로 저장 → None 필터로 걸러지는 불변식의 회귀 가드)"""
    _tmp_thresholds(tmp_path, monkeypatch)
    _seed_features(40)
    with store.get_conn() as conn:
        item_id = store.upsert_item_master("vref_trim", "VREF_TRIM", None, None, "TRIM",
                                           None, "V", None, conn=conn)
        run_id = store.create_ingest_run({"ingested_by": "test"}, conn=conn)
        for i in range(5):
            case_id = store.make_case_id("P1", f"NAN{i}", 1, item_id, 18, 0.0)
            store.upsert_fail_case(case_id, "P1", f"NAN{i}", 1, item_id, 18, 0.0,
                                   "TRIM|V|18", conn=conn)
            store.save_features(case_id, run_id, "ev1",
                                {"spread_norm": float("nan"), "outlier_ratio": float("nan"),
                                 "skewness": float("nan"), "kurtosis": float("nan")},
                                conn=conn)
    result = calibrate.recalibrate()
    ov = result["item_class"]["TRIM|V|18"]
    arr = np.array([0.1 + i * 0.01 for i in range(40)])
    assert ov["spread_norm_warn"] == round(float(np.quantile(arr, 0.9)), 4)  # NaN 행 무시
    assert all(not np.isnan(v) for v in ov.values())


def test_recalibrate_skips_unknown_feature_with_warning(fresh_db, tmp_path, monkeypatch):
    """calibration 스펙의 feature 명 오타 → 해당 키만 skip + warnings, 나머지는 정상 보정."""
    dst = _tmp_thresholds(tmp_path, monkeypatch)
    text = dst.read_text(encoding="utf-8")
    dst.write_text(text.replace(
        "  quantiles:",
        "  quantiles:\n    bogus_warn: {feature: no_such_col, q: 0.90}"), encoding="utf-8")
    load_yaml.cache_clear()
    _seed_features(40)
    result = calibrate.recalibrate()
    ov = result["item_class"]["TRIM|V|18"]
    assert "bogus_warn" not in ov
    assert "spread_norm_warn" in ov  # 오타 키 외에는 영향 없음
    assert any("no_such_col" in w for w in result["warnings"])


def test_rewrite_item_class_rejects_trailing_sections(tmp_path):
    """item_class 뒤에 다른 최상위 섹션이 있으면 소실 대신 명시적 에러."""
    dst = tmp_path / "t.yaml"
    dst.write_text("default:\n  cpk_warn: 1.33\nitem_class: {}\nextra:\n  a: 1\n",
                   encoding="utf-8")
    with pytest.raises(ValueError, match="extra"):
        calibrate._rewrite_item_class_section(dst, {"X|Y|1": {"cpk_warn": 1.0}})


def test_recalibrate_without_db_returns_note(tmp_path, monkeypatch):
    _tmp_thresholds(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "none.db")
    result = calibrate.recalibrate()
    assert result["engine_version"] is None
    assert "eval.db" in result["note"]

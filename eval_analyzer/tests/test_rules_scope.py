"""thresholds family 오버레이 트리 + signature enabled 플래그.

운영 rules/ 를 건드리지 않도록 tmp 복사본으로 격리한다(test_calibrate 와 같은 방식).
트리·플래그가 **없을 때 종전과 동일**하다는 것이 이 파일의 첫 번째 계약이다.
"""
import shutil

import pytest
import yaml

from eval_engine import config
from eval_engine.pipeline import signatures
from eval_engine.pipeline._rules import load_yaml, thresholds_for, threshold_overlay_path


def _tmp_rules(tmp_path, monkeypatch):
    """rules/ 전체를 tmp 로 복사하고 config 가 복사본을 보게 함(원본 보호)."""
    dst = tmp_path / "rules"
    shutil.copytree(config.RULES_DIR, dst)
    monkeypatch.setattr(config, "RULES_DIR", dst)
    monkeypatch.setattr(config, "THRESHOLDS_FILE", dst / "thresholds.yaml")
    monkeypatch.setattr(config, "SIGNATURES_FILE", dst / "signatures.yaml")
    load_yaml.cache_clear()
    return dst


def _write_overlay(rules_dir, product_type, family, values):
    path = threshold_overlay_path(product_type, family)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return path


def _case(**kw):
    c = {"product_type": "PMIC", "family_product": "SOC", "item_class": None, "bin": 99,
         "lsl": 0.0, "usl": 10.0}
    c.update(kw)
    return c


def test_no_tree_keeps_default(tmp_path, monkeypatch):
    """오버레이 트리가 없으면 default 병합 그대로 (기존 동작 보존)."""
    _tmp_rules(tmp_path, monkeypatch)
    base = yaml.safe_load(config.THRESHOLDS_FILE.read_text(encoding="utf-8"))["default"]
    assert thresholds_for(_case()) == base


def test_family_overlay_overrides_pt_default(tmp_path, monkeypatch):
    """_default.yaml → <FAMILY>.yaml 순으로 덮인다."""
    rules = _tmp_rules(tmp_path, monkeypatch)
    _write_overlay(rules, "PMIC", None, {"cpk_warn": 1.20, "n_min": 40})
    _write_overlay(rules, "PMIC", "SOC", {"cpk_warn": 1.10})

    th = thresholds_for(_case())
    assert th["cpk_warn"] == 1.10      # family 가 최우선
    assert th["n_min"] == 40           # family 에 없는 키는 _default 유지

    # 다른 family 는 _default 만 적용
    assert thresholds_for(_case(family_product="MEMORY"))["cpk_warn"] == 1.20
    # 다른 제품군은 무영향
    assert thresholds_for(_case(product_type="MDDI", family_product="MX"))["cpk_warn"] == 1.33


def test_item_class_still_wins_over_family(tmp_path, monkeypatch):
    """calibrate 가 쓰는 item_class 가 family 보다 우선 (기존 계약 보존)."""
    rules = _tmp_rules(tmp_path, monkeypatch)
    _write_overlay(rules, "PMIC", "SOC", {"cpk_warn": 1.10})
    doc = yaml.safe_load(config.THRESHOLDS_FILE.read_text(encoding="utf-8"))
    doc["item_class"] = {"TRIM|V|18": {"cpk_warn": 0.90}}
    config.THRESHOLDS_FILE.write_text(yaml.safe_dump(doc), encoding="utf-8")
    load_yaml.cache_clear()

    assert thresholds_for(_case(item_class="TRIM|V|18"))["cpk_warn"] == 0.90


def test_overlay_edit_reflected_without_cache_clear(tmp_path, monkeypatch):
    """파일을 고치면 cache_clear 없이도 다음 호출에 반영된다(mtime 키 캐시)."""
    rules = _tmp_rules(tmp_path, monkeypatch)
    path = _write_overlay(rules, "PMIC", "SOC", {"cpk_warn": 1.10})
    assert thresholds_for(_case())["cpk_warn"] == 1.10

    # mtime_ns 해상도가 낮은 파일시스템에서도 키가 달라지도록 mtime 을 명시 이동
    import os
    st = os.stat(path)
    path.write_text(yaml.safe_dump({"cpk_warn": 1.05}), encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    assert thresholds_for(_case())["cpk_warn"] == 1.05


def _full_features(**kw):
    f = {"spread_norm": 0.05, "skewness": 0.1, "kurtosis": 0.0, "outlier_ratio": 0.0,
         "spec_margin_low": 5.0, "spec_margin_high": 5.0, "site_cpk_delta": 0.0,
         "edge_fail_ratio": 1.0, "n_dut": 100}
    f.update(kw)
    return f


@pytest.mark.rules_as_deployed
def test_signature_enabled_false_does_not_fire(tmp_path, monkeypatch):
    """enabled:false 인 signature 는 발화도 applies 기록도 하지 않는다."""
    _tmp_rules(tmp_path, monkeypatch)
    doc = yaml.safe_load(config.SIGNATURES_FILE.read_text(encoding="utf-8"))
    for s in doc["signatures"]:
        if s["id"] == "SEVERE_OUTLIER":
            s["enabled"] = False
    config.SIGNATURES_FILE.write_text(yaml.safe_dump(doc, allow_unicode=True),
                                      encoding="utf-8")
    load_yaml.cache_clear()

    case, feats, raw = _case(), _full_features(outlier_ratio=0.10), {"yield": 0.95, "cpk": 1.5}
    sig = signatures.evaluate(case, feats, raw)
    ids = [s["id"] for s in sig["signatures"]]
    assert "SEVERE_OUTLIER" not in ids
    assert "OUTLIER_WARN" in ids       # 다른 룰은 그대로 발화
    assert not any(k.startswith("SEVERE_OUTLIER.") for k in sig["applies"])

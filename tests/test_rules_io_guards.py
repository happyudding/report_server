"""eval 룰 저장 안전장치 검증 — 관계·타입 검증 + no-op 저장 스킵.

실행:
    python tests/test_rules_io_guards.py

검증:
  (a) _check_threshold_values — 관계 4종/타입 3종 위반 검출, touched 한정(상속 층에
      이미 있던 위반이 무관한 키 저장을 막지 않는다)
  (b) 현행 thresholds.yaml default 전 키가 검증을 통과 (THRESHOLD_KINDS 오기입 방어)
  (c) no-op 저장 — 같은 값 재저장 시 백업 미생성 + rules_rev 불변
      (rev 를 올리면 ai_comment 세션 리포트 캐시가 통째로 무효화된다)
  (d) 관계 위반 저장은 RuleError 로 거부

실제 rules 트리를 tmp 로 복사해 EVAL_RULES_DIR 로 물린다 — 운영 룰 파일은 건드리지 않는다.
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "server"))

_TMP = Path(tempfile.mkdtemp(prefix="rules_guard_test_"))
shutil.copytree(_ROOT / "eval_analyzer" / "eval_engine" / "rules", _TMP / "rules",
                ignore=shutil.ignore_patterns("_backup"))
os.environ["EVAL_RULES_DIR"] = str(_TMP / "rules")     # eval_engine.config import 전에

from eval_panel import rules_io          # noqa: E402
from web_report import eval_debug        # noqa: E402

_PT = "MDDI"


def _check(effective, touched):
    return rules_io._check_threshold_values(effective, set(touched))


def main():
    # (a) 관계·타입 ─────────────────────────────────────────────────────────
    # ⚠ 관계 쌍은 **현행 THRESHOLD_RELATIONS 에 실제로 있는 것만** 쓴다. 종전에는
    # 이미 삭제된 룰의 키(outlier_ratio_warn/bad)로 관계 위반을 기대해 이 테스트가
    # 계속 실패하고 있었고, subpop_density_gap 쌍은 2026-08-19 에 관계 선언에서 빠졌다
    # (이름의 강약이 값의 대소를 함의하지 않는다 — rules_io 주석 참조).
    ok = {"cpk_bad": 1.0, "cpk_warn": 1.33,
          "center_region_pct": 0.3, "edge_region_pct": 0.8,
          "heavy_tail_mass_min": 0.01, "heavy_tail_mass_max": 0.05}
    assert _check(ok, ok) == [], _check(ok, ok)

    bad_pairs = [
        ({**ok, "cpk_bad": 1.5}, "cpk_bad"),
        ({**ok, "center_region_pct": 0.9}, "center_region_pct"),
        ({**ok, "heavy_tail_mass_max": 0.005}, "heavy_tail_mass_max"),
    ]
    for eff, key in bad_pairs:
        got = _check(eff, {key})
        assert any("보다" in m for m in got), f"관계 위반 미검출: {key} → {got}"

    # 쌍의 반대쪽만 건드려도 잡힌다 (검사는 effective 기준)
    assert _check({**ok, "cpk_bad": 1.5}, {"cpk_warn"}), "쌍 반대편 touched 미검출"
    # touched 밖이면 통과 — 상위 층의 기존 위반이 무관한 키 저장을 막지 않는다
    assert _check({**ok, "cpk_bad": 1.5}, {"n_min"}) == [], "touched 한정이 안 됨"

    assert _check({"heavy_tail_mass_min": 1.4}, {"heavy_tail_mass_min"}), "ratio 상한 미검출"
    assert _check({"n_min": 0}, {"n_min"}), "count 하한 미검출"
    assert _check({"n_min": 2.5}, {"n_min"}), "count 정수 검사 누락"
    assert _check({"outlier_sigma": 0}, {"outlier_sigma"}), "positive 미검출"
    assert _check({"subpop_outlier_sigma": 0}, {"subpop_outlier_sigma"}),         "게이트 전용 sigma positive 미검출"
    # 표에 없는 키는 검사하지 않는다(opt-in)
    assert _check({"spread_norm_warn": 99}, {"spread_norm_warn"}) == [], "opt-in 위반"

    # (b) 현행 default 전 키 통과 — THRESHOLD_KINDS 오기입이면 여기서 터진다
    default = eval_debug.default_thresholds()
    problems = _check(default, set(default))
    assert problems == [], f"배포 default 가 자체 검증에 걸림: {problems}"
    unknown = set(rules_io.THRESHOLD_KINDS) - set(default)
    assert not unknown, f"THRESHOLD_KINDS 에 없는 키: {unknown}"

    # (c) no-op — 같은 값 재저장 ─────────────────────────────────────────────
    backup_dir = _TMP / "rules" / rules_io.BACKUP_DIRNAME
    first = rules_io.save_thresholds(_PT, None, {"cpk_warn": 1.2})
    assert first["no_op"] is False and first["saved"]["cpk_warn"] == 1.2, first
    rev_after_write = eval_debug.rules_rev()
    n_backup = len(list(backup_dir.glob("*.bak"))) if backup_dir.is_dir() else 0

    again = rules_io.save_thresholds(_PT, None, {"cpk_warn": 1.2})
    assert again["no_op"] is True and again["backup"] is None, again
    assert eval_debug.rules_rev() == rev_after_write, "no-op 인데 rev 가 올랐다"
    n_after = len(list(backup_dir.glob("*.bak"))) if backup_dir.is_dir() else 0
    assert n_after == n_backup, "no-op 인데 백업이 생겼다"

    # 값이 실제로 바뀌면 rev 가 오른다
    changed = rules_io.save_thresholds(_PT, None, {"cpk_warn": 1.25})
    assert changed["no_op"] is False and changed["rules_rev"] != rev_after_write, changed

    # 상속값과 같은 값을 지워도(None) 오버레이가 줄면 no-op 이 아니다
    cleared = rules_io.save_thresholds(_PT, None, {"cpk_warn": None})
    assert cleared["no_op"] is False and "cpk_warn" not in cleared["saved"], cleared

    # (d) 관계 위반 저장 거부 ────────────────────────────────────────────────
    try:
        rules_io.save_thresholds(_PT, None, {"cpk_bad": 2.0})   # > cpk_warn(1.33)
    except rules_io.RuleError as exc:
        assert "cpk_bad" in str(exc), exc
    else:
        raise AssertionError("cpk_bad > cpk_warn 이 통과됐다")

    # 제외 목록도 no-op 을 탄다
    current = eval_debug.exclusions()
    same = rules_io.save_exclusions({k: list(v) for k, v in current.items()})
    assert same["no_op"] is True and same["backup"] is None, same

    print("PASS: test_rules_io_guards (a/b/c/d)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

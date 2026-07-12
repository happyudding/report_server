"""L1 Metric — per fail item raw(메모리)에서 표준 통계 계산. raw 자체는 저장 안 함.

공식: docs/CODE_TO_PORT §2 (cpk/cpl/cpu/cp/mean/stdev/min/max), yield/fail_count/total_count,
bimodality(Sarle's coefficient). 결측(n<=1, limit 없음)이면 cpk 류 None.
반환: raw_metrics dict (DB_SCHEMA §4 컬럼).
"""
import numpy as np


def cpk_summary(values, lsl, usl):
    """CODE_TO_PORT §2 그대로. 유한값만 사용, 표본 표준편차(ddof=1)."""
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], dtype=float)
    n = v.size
    if n == 0:
        return dict(n=0, min=None, max=None, median=None, mean=None, stdev=None,
                    cp=None, cpl=None, cpu=None, cpk=None)
    mean = float(v.mean())
    std = float(v.std(ddof=1)) if n > 1 else float("nan")
    out = dict(n=n, min=float(v.min()), max=float(v.max()), median=float(np.median(v)),
               mean=mean, stdev=std if np.isfinite(std) else None,
               cp=None, cpl=None, cpu=None, cpk=None)
    can = n > 1 and np.isfinite(std) and std != 0 and lsl is not None and usl is not None
    if can:
        out["cp"] = (usl - lsl) / (6 * std)
        out["cpl"] = (mean - lsl) / (3 * std)
        out["cpu"] = (usl - mean) / (3 * std)
        out["cpk"] = min(out["cpl"], out["cpu"])
    return out


def _bimodality_coefficient(values):
    """Sarle's BC = (skew^2 + 1) / kurtosis. n<4 또는 kurtosis=0 이면 None."""
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], dtype=float)
    if v.size < 4:
        return None
    mean, std = v.mean(), v.std(ddof=1)
    if std == 0:
        return None
    z = (v - mean) / std
    skew = float(np.mean(z ** 3))
    kurt = float(np.mean(z ** 4))
    if kurt == 0:
        return None
    return float((skew ** 2 + 1) / kurt)


def compute(case_ctx: dict) -> dict:
    values = case_ctx.get("values") or []
    summary = cpk_summary(values, case_ctx.get("lsl"), case_ctx.get("usl"))
    if values:
        total = len(values)
        fail = sum(1 for f in case_ctx.get("fail_mask", []) if f)
        yield_ = 1 - fail / total if total else None
    else:
        # degrade 모드 — case_ctx 에 이미 yield/fail_count/total_count 가 들어있음
        fail = case_ctx.get("fail_count")
        total = case_ctx.get("total_count")
        yield_ = case_ctx.get("yield")
    return {
        "cpk": summary["cpk"], "cpl": summary["cpl"], "cpu": summary["cpu"],
        "cp": summary["cp"], "mean": summary["mean"], "stdev": summary["stdev"],
        "min": summary["min"], "max": summary["max"],
        "yield": yield_, "fail_count": fail, "total_count": total,
        "bimodality": _bimodality_coefficient(values) if values else None,
    }

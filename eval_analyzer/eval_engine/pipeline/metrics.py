"""L1 Metric — per fail item raw(메모리)에서 표준 통계 계산. raw 자체는 저장 안 함.

공식: docs/CODE_TO_PORT §2 (cpk/cpl/cpu/cp/mean/stdev/min/max), yield/fail_count/total_count,
bimodality(Sarle's coefficient). 결측(n<=1, limit 없음)이면 cpk 류 None.
반환: raw_metrics dict (DB_SCHEMA §4 컬럼).
"""
import numpy as np


def _finite(values):
    """유한값만 float ndarray 로 — 종전 [x for x in values if x is not None and
    np.isfinite(x)] 와 원소·순서 동일(벡터화 — 2026-08-13 콜드 빌드 최적화).

    np.asarray(dtype=float) 가 None→nan 으로 바꾸고, isfinite 마스크가 nan(±inf 포함)을
    거르므로 걸러지는 집합이 종전 스칼라 필터와 정확히 같다. 종전엔 case 마다 N 회의
    파이썬 레벨 np.isfinite 호출이 있었다(콜드 빌드 최대 상수 비용).
    """
    a = np.asarray(values, dtype=float)
    return a[np.isfinite(a)]


def cpk_summary(values, lsl, usl, v=None):
    """CODE_TO_PORT §2 그대로. 유한값만 사용, 표본 표준편차(ddof=1).

    `v` 를 주면 `_finite(values)` 를 건너뛴다 — 같은 배열을 여러 번 변환하지 않기 위한
    재사용구다(호출부가 `_finite` 결과를 그대로 넘긴다). 값은 동일.
    """
    v = _finite(values) if v is None else v
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


def _bimodality_coefficient(values, v=None):
    """Sarle's BC = (skew^2 + 1) / kurtosis. n<4 또는 kurtosis=0 이면 None.

    `v` 는 `cpk_summary` 와 같은 재사용구 — 같은 유한값 배열을 두 번 만들지 않는다.
    """
    v = _finite(values) if v is None else v
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
    """L1 진입점 — case 1건의 측정값(메모리)에서 raw_metrics 산출. raw 는 저장하지 않는다.

    PF(양불) item 은 측정값이 없어 통계량을 전부 None 으로 비우고 yield 계열만 채운다.
    cpk 로 판정할 수 없는 부류라, status.decide 의 PF trump(수율 단독 CRITICAL)가 그
    공백을 메운다. values 가 비어 있는 경로는 degrade 입력 — 요약통계가 case_ctx 에 이미
    들어 있으므로 그대로 옮긴다.
    반환: DB_SCHEMA §4 raw_metrics 컬럼(cpk/cpl/cpu/cp/mean/stdev/min/max/yield/
    fail_count/total_count/bimodality).
    """
    values = case_ctx.get("values") or []
    is_pf = case_ctx.get("value_type") == "PF"
    # item 단위 공유(_shared, ingest 가 같은 item 의 case(bin)들에 같은 dict 를 붙임) —
    # cpk_summary/bimodality 는 values+limit 만의 함수라 fail bin 이 달라도 동일하다.
    # 같은 배열을 bin 수만큼 재계산하던 비용 제거(2026-08-13). 값은 1-튜플로 감싸
    # "계산 결과가 None" 과 "아직 미계산" 을 구분한다. 동시 계산 경합은 무해하다
    # (같은 입력 → 같은 값, dict 대입은 GIL 원자적).
    shared = case_ctx.get("_shared")

    def _finite_shared():
        """유한값 배열 1벌 — cpk_summary 와 bimodality 가 각자 만들던 것을 공유한다
        (2026-08-19, 값 동일). item 단위 메모라 bin 수만큼 반복되지도 않는다."""
        if shared is None:
            return _finite(values)
        hit = shared.get("finite")
        if hit is None:
            hit = (_finite(values),)
            shared["finite"] = hit
        return hit[0]

    summary = None
    if shared is not None:
        hit = shared.get("cpk_summary")
        if hit is not None:
            summary = hit[0]
    if summary is None:
        summary = cpk_summary(values, case_ctx.get("lsl"), case_ctx.get("usl"),
                              v=_finite_shared())
        if shared is not None:
            shared["cpk_summary"] = (summary,)
    if values:
        # 분모는 전체 DUT 수(ingest 가 넣은 total_count) 우선 — len(values)(파싱 성공분)로
        # 재면 item 마다 분모가 달라 yield 비교·trump 판정이 왜곡된다. 구 경로 폴백 유지.
        total = case_ctx.get("total_count") or len(values)
        fail = case_ctx.get("fail_count")
        if fail is None:
            fail = sum(1 for f in case_ctx.get("fail_mask", []) if f)
        yield_ = 1 - fail / total if total else None
    else:
        # degrade 모드 — case_ctx 에 이미 yield/fail_count/total_count 가 들어있음
        fail = case_ctx.get("fail_count")
        total = case_ctx.get("total_count")
        yield_ = case_ctx.get("yield")
    if is_pf:
        return {
            "cpk": None, "cpl": None, "cpu": None,
            "cp": None, "mean": None, "stdev": None,
            "min": None, "max": None,
            "yield": yield_, "fail_count": fail, "total_count": total,
            "bimodality":None,
        }
    bimod = None
    if values:
        hit = shared.get("bimodality") if shared is not None else None
        if hit is not None:
            bimod = hit[0]
        else:
            bimod = _bimodality_coefficient(values, v=_finite_shared())
            if shared is not None:
                shared["bimodality"] = (bimod,)
    return {
        "cpk": summary["cpk"], "cpl": summary["cpl"], "cpu": summary["cpu"],
        "cp": summary["cp"], "mean": summary["mean"], "stdev": summary["stdev"],
        "min": summary["min"], "max": summary["max"],
        "yield": yield_, "fail_count": fail, "total_count": total,
        "bimodality": bimod,
    }

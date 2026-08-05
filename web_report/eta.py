"""콜드 빌드 예상시간 추정 — 세션 로드 오버레이 안내 문구용 (2026-08-05).

`build_status` 는 사실(계산 중 / 경과초)만 주고 남은 시간은 주지 않았다. 첫 조회가
10초 넘게 걸리는 세션에서 사용자는 "얼마나 더?" 를 알 수 없어 멈춘 것으로 오해했다.
여기서 **입력 규모 기반 예상초**를 만들어 안내 문구에 덧붙인다 — 진행바(%)는 여전히
추정 creep 이고, 이 값도 안내일 뿐 진척률이 아니다.

추정식 (tests/bench_webreport.py 픽스처로 7규모 실측 → 최소제곱):

    t ≈ A + B·Mcells + C·kcols
        Mcells = Σ(항목수 × 행수) / 1e6      전체 셀 수 (백만)
        kcols  = Σ 항목수 / 1e3              전체 항목 컬럼 수 (천)

**두 변수가 모두 필요하다.** 총 바이트(MB) 하나로는 같은 용량이라도 항목 수에 따라
±50% 어긋난다(항목당 고정 비용이 커서 — 10소스×2000항목 60MB 가 6소스×300항목 39MB
보다 3배 느리다). 두 변수를 쓰면 평균 오차 2.3% / 최대 7.1% 다.

계수는 **개발 PC(16코어) 인라인 실측**이라 운영 사양·워커 구성에서는 그대로 맞지 않는다.
그래서 build_log 에 남은 실제 콜드 빌드(같은 두 변수 기록)와 비교해 **배율 하나만**
학습해 곱한다(`calibration_factor`). 기록이 부족하면 배율 1.0 = 벤치 계수 그대로.

관측·안내 전용 — 이 모듈의 실패는 절대 조회를 막지 않는다(호출부는 None 을 안내 생략으로
처리한다).
"""
from __future__ import annotations

import os
import statistics
import threading
import time
from pathlib import Path

from . import build_log
from .honeyform import META_COLUMNS, META_ROW_LABELS

ENABLED = (os.getenv("WEB_REPORT_ETA_ENABLED", "1") or "1").strip() not in ("0", "false", "no")

# 벤치 회귀 계수 (2026-08-05, 개발PC 16코어 / workers=0 인라인 / 7규모 3~118MB)
_A, _B, _C = 0.064, 0.155, 0.147

# 이보다 짧으면 안내하지 않는다 — 문구가 뜨자마자 사라져 깜빡이기만 한다.
MIN_ANNOUNCE_SEC = 1.5

# 학습 배율의 안전 범위. 밖으로 나가면 기록이 오염된 것으로 보고 무시한다.
_FACTOR_MIN, _FACTOR_MAX = 0.2, 20.0
# 표본이 이만큼 모이면 학습값을 100% 쓴다. 그 전에는 1.0 쪽으로 비례 축소해 섞는다 —
# 운영 조건은 벤치보다 느리므로(동시 폴링·GIL 경합만으로 같은 세션이 1.6배) 기록이
# 5건 쌓일 때까지 벤치 계수를 그대로 쓰면 그동안 계속 짧게 예상한다.
_FACTOR_FULL_SAMPLES = 5
_FACTOR_TTL_SEC = 300.0
_FACTOR_HOURS = 24 * 14        # build_log 보관기간(기본 14일)과 맞춘다

_shape_cache: dict[str, tuple[float, float]] = {}
_shape_lock = threading.Lock()
_SHAPE_CACHE_MAX = 512

_factor_cache: tuple[float, float] = (0.0, 1.0)   # (계산시각, 배율)
_factor_lock = threading.Lock()


# ── 입력 규모 ────────────────────────────────────────────────────────────────

def shape_from_tables(tables) -> tuple[float, float]:
    """디코드된 HoneyformTable 목록 → (Mcells, kcols). 빌드 후 기록용 (정확)."""
    cells = sum(len(t.item_columns) * len(t.data) for t in tables)
    cols = sum(len(t.item_columns) for t in tables)
    return round(cells / 1e6, 4), round(cols / 1e3, 4)


def shape_from_storage(upload_root, analysis_key: str) -> tuple[float, float] | None:
    """저장된 parquet **footer 만** 읽어 (Mcells, kcols). 빌드 전 예측용.

    디코드 없이 파일 끝 메타데이터만 읽으므로 소스당 ~1ms 다. honeyform 규약상
    parquet 은 메타 6행 + 데이터, 컬럼은 META 7개 + 항목이므로 거기서 역산한다.
    로컬에 없으면(S3 저장 세션·삭제) None — 안내를 생략할 뿐이다.
    """
    session_dir = Path(upload_root) / "web_report" / str(analysis_key)
    try:
        paths = sorted(session_dir.glob("source_*.parquet"))
    except OSError:
        return None
    if not paths:
        return None
    import pyarrow.parquet as pq
    cells = cols = 0
    for path in paths:
        try:
            md = pq.ParquetFile(path).metadata
        except Exception:
            return None
        items = max(0, md.num_columns - len(META_COLUMNS))
        rows = max(0, md.num_rows - len(META_ROW_LABELS))
        cells += items * rows
        cols += items
    if not cols:
        return None
    return round(cells / 1e6, 4), round(cols / 1e3, 4)


def _cached_shape(upload_root, analysis_key: str, content_hash: str):
    """세션 규모 캐시 — 폴링이 2초마다 footer 를 다시 읽지 않도록.

    키에 content_hash 를 넣어 rawdata 편집(소스 교체·시트 삭제)이 규모를 바꾸면
    자동으로 다시 잰다.
    """
    key = f"{analysis_key}:{content_hash}"
    with _shape_lock:
        hit = _shape_cache.get(key)
    if hit is not None:
        return hit
    shape = shape_from_storage(upload_root, analysis_key)
    if shape is None:
        return None
    with _shape_lock:
        if len(_shape_cache) >= _SHAPE_CACHE_MAX:
            _shape_cache.clear()      # 단순 전량 비움 — 재계산이 ms 라 LRU 가 과하다
        _shape_cache[key] = shape
    return shape


# ── 배율 학습 ────────────────────────────────────────────────────────────────

def _raw_estimate(mcells: float, kcols: float) -> float:
    return _A + _B * float(mcells) + _C * float(kcols)


def calibration_factor() -> float:
    """운영 실측 / 벤치 예측 의 중앙값 (표본이 적으면 1.0 쪽으로 축소). 기록이 없으면 1.0.

    큐 대기(pool_wait)는 부하에 따라 요동쳐 예측 불가라 **계산 시간만** 본다
    (오프로드 레코드의 `build`, 인라인은 `total`).
    """
    global _factor_cache
    now = time.monotonic()
    with _factor_lock:
        ts, value = _factor_cache
        if now - ts < _FACTOR_TTL_SEC:
            return value
    factor = 1.0
    try:
        ratios = []
        for rec in build_log.history(hours=_FACTOR_HOURS, limit=500):
            if rec.get("kind") != "report" or rec.get("result") != "ok":
                continue
            mcells, kcols = rec.get("mcells"), rec.get("kcols")
            if mcells is None or kcols is None:
                continue
            actual = rec.get("build")
            if actual is None:
                actual = rec.get("total")
            pred = _raw_estimate(mcells, kcols)
            if not actual or pred <= 0:
                continue
            ratio = float(actual) / pred
            if _FACTOR_MIN <= ratio <= _FACTOR_MAX:
                ratios.append(ratio)
        if ratios:
            n = min(len(ratios), _FACTOR_FULL_SAMPLES)
            factor = round((n * statistics.median(ratios)
                            + (_FACTOR_FULL_SAMPLES - n)) / _FACTOR_FULL_SAMPLES, 3)
    except Exception:
        factor = 1.0
    with _factor_lock:
        _factor_cache = (now, factor)
    return factor


# ── 공개 API ─────────────────────────────────────────────────────────────────

def session_eta(session, upload_root) -> float | None:
    """세션 콜드 빌드 예상초. 알 수 없거나 너무 짧으면 None (안내 생략).

    session: report_db 세션 행(Mapping) — analysis_key/content_hash 만 쓴다.
    """
    if not ENABLED:
        return None
    try:
        analysis_key = session.get("analysis_key") or ""
        if not analysis_key:
            return None
        shape = _cached_shape(upload_root, analysis_key,
                              str(session.get("content_hash") or ""))
        if shape is None:
            return None
        eta = _raw_estimate(*shape) * calibration_factor()
        if eta < MIN_ANNOUNCE_SEC:
            return None
        return round(eta, 1)
    except Exception:
        return None

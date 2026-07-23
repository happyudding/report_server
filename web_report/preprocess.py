"""세션 전처리 (항목 제외 + outlier 마스킹) — 조회 경로 전용 순수 모듈.

Honey 클라의 Rawdata 허브 다이얼로그가 세션 단위로 저장하는 "전처리 옵션"을 조회
시점에 tables 에 적용한다. **원본 parquet 은 건드리지 않는다** — 옵션을 비우면 즉시
원래 값으로 돌아온다 (되돌릴 수 있는 편집).

spec 형태 (세션 편집 DB kind='preprocess', item_key='spec' 의 JSON):
    {"exclude_items": ["ITEM_A", ...],
     "outlier": {"mode": "stdev", "k": 50.0}}

두 규칙 모두 **측정 항목(item)만** 대상이다:
  - exclude_items : 그 item 컬럼을 리포트 전 탭에서 제거 (die/행은 그대로)
  - outlier       : 항목별 mean ± k·stdev 밖의 **측정값만 결측(NaN)** 처리.
                    BIN/좌표/행은 손대지 않으므로 수율·wafer map 은 불변이고
                    CPK/Distribution 의 n·평균·σ 만 달라진다.

캐시·저장소·flask·xlwings 무의존 — dist_blob.py / rawvalues.py 와 같은 클라 공유
모듈이다 (Honey 가 dist blob 프리컴퓨트·미리보기에서 같은 코드를 돌려 값 일치를
구조적으로 보장한다).
"""
from __future__ import annotations

import hashlib

from .validation import canon

OUTLIER_MODE_STDEV = "stdev"
_DIGEST_LEN = 12


def normalize(spec) -> dict:
    """저장/전달된 spec 을 정규형으로. 의미 없는 값은 전부 떨어져 **빈 dict** 가 된다.

    빈 dict = 전처리 없음 = 기존 동작(캐시 키·코드 경로 완전 동일)이라, "옵션이 실질적으로
    없는 상태"를 여기 한 곳에서 판정하는 것이 중요하다.
    """
    if not isinstance(spec, dict):
        return {}
    out = {}

    excluded = spec.get("exclude_items")
    if isinstance(excluded, (list, tuple, set)):
        names = sorted({str(v).strip() for v in excluded if str(v).strip()})
        if names:
            out["exclude_items"] = names

    outlier = spec.get("outlier")
    if isinstance(outlier, dict):
        mode = str(outlier.get("mode") or OUTLIER_MODE_STDEV).strip()
        try:
            k = float(outlier.get("k"))
        except (TypeError, ValueError):
            k = 0.0
        # k<=0 / NaN / inf 는 "해제"로 본다 (빈칸 입력 = 옵션 끄기).
        if mode == OUTLIER_MODE_STDEV and k > 0 and k == k and k != float("inf"):
            out["outlier"] = {"mode": OUTLIER_MODE_STDEV, "k": k}

    return out


def digest(spec) -> str:
    """정규화된 spec 의 캐시 키 조각. **전처리가 없으면 빈 문자열**.

    빈 문자열이면 cache_policy 빌더가 키에 아무것도 덧붙이지 않아 기존 캐시가 그대로
    유효하다 (무회귀의 핵심).
    """
    norm = normalize(spec)
    if not norm:
        return ""
    return hashlib.sha256(canon(norm)).hexdigest()[:_DIGEST_LEN]


def session_digest(report_db, session_id: str) -> str:
    """세션의 저장된 전처리 spec → 캐시 키 조각 (없으면 빈 문자열).

    캐시 키를 만들기 전에 tables 를 로드할 수 없는 호출부(dist/map/scatter gzip 캐시)가
    쓰는 편의 함수 — 작은 인덱스 SELECT 1회다."""
    from .edits import load_preprocess

    return digest(load_preprocess(report_db, session_id))


def describe(spec) -> str:
    """사용자에게 보여줄 한 줄 요약 (리포트 배지·다이얼로그 공용). 없으면 빈 문자열."""
    norm = normalize(spec)
    parts = []
    if norm.get("exclude_items"):
        parts.append(f"항목 {len(norm['exclude_items'])}개 제외")
    outlier = norm.get("outlier")
    if outlier:
        parts.append(f"outlier ±{_fmt_k(outlier['k'])}σ 제거")
    return " · ".join(parts)


def _fmt_k(k) -> str:
    f = float(k)
    return str(int(f)) if f.is_integer() else f"{f:g}"


# ── 적용 ─────────────────────────────────────────────────────────────────────
def apply_tables(tables, spec):
    """tables 에 전처리를 적용한 새 리스트를 반환. 반환 (tables, stats).

    stats = {"removed": {item: 제거 건수}, "removed_total": int, "excluded": [item, ...]}
    — 클라 미리보기와 로그용이다. spec 이 비면 **입력 tables 를 그대로** 돌려준다
    (객체 동일성까지 유지 — 무전처리 경로에 비용 0).

    호출 전제: 표시(조회) 경로 전용. 재인코딩/편집 경로에서 부르지 말 것 —
    전처리된 테이블의 `df`(재인코딩용 전체 프레임)는 None 으로 지워 원본과 다른 값이
    parquet 으로 되돌아가는 사고를 구조적으로 막는다.
    """
    norm = normalize(spec)
    stats = {"removed": {}, "removed_total": 0, "excluded": []}
    if not norm or not tables:
        return tables, stats

    excluded = set(norm.get("exclude_items") or ())
    outlier = norm.get("outlier")

    out = []
    for table in tables:
        new_table = _apply_one(table, excluded, outlier, stats)
        out.append(new_table)
    stats["excluded"] = sorted(excluded)
    return out, stats


def _apply_one(table, excluded, outlier, stats):
    from .honeyform import HoneyformTable

    item_columns = [c for c in table.item_columns if c not in excluded]
    data = table.data
    if excluded:
        drop = [c for c in data.columns if c in excluded]
        if drop:
            data = data.drop(columns=drop)

    if outlier and item_columns:
        data = _mask_outliers(data, item_columns, float(outlier["k"]), stats)

    def _meta(d):
        return {k: v for k, v in d.items() if k not in excluded} if excluded else dict(d)

    return HoneyformTable(
        source=table.source,
        file_name=table.file_name,
        # 전처리 결과는 표시 전용 — 재인코딩 경로가 실수로 쓰지 못하게 df 를 지운다.
        df=None,
        item_columns=item_columns,
        tseq=_meta(table.tseq),
        tno=_meta(table.tno),
        step=_meta(table.step),
        units=_meta(table.units),
        hilim=_meta(table.hilim),
        lolim=_meta(table.lolim),
        data=data,
    )


def _mask_outliers(data, item_columns, k, stats):
    """항목별 mean ± k·stdev 밖 값을 NaN 으로. 바뀐 컬럼이 없으면 원본 프레임 그대로.

    항목 블록 전체를 한 번에 float64 배열로 만들면 (행 × 항목수) 크기의 복사본이 생겨
    mass data 에서 메모리가 터진다 — 컬럼 단위로 돌고, 실제로 바뀐 컬럼만 모아 concat
    1회로 합친다 (컬럼별 대입 반복은 프레임 재배치로 느리다).
    """
    import numpy as np
    import pandas as pd

    changed = {}
    for name in item_columns:
        if name not in data.columns:
            continue
        col = data[name]
        vals = pd.to_numeric(col, errors="coerce").to_numpy(dtype="float64", copy=True)
        finite = np.isfinite(vals)
        n = int(finite.sum())
        if n < 2:
            continue                      # 표본 1개 이하 — 표준편차 정의 불가
        mean = float(vals[finite].mean())
        std = float(vals[finite].std(ddof=1))
        if not np.isfinite(std) or std <= 0:
            continue                      # 전부 같은 값 — 제거할 outlier 가 없다
        mask = finite & (np.abs(vals - mean) > k * std)
        removed = int(mask.sum())
        if not removed:
            continue
        vals[mask] = np.nan
        changed[name] = pd.Series(vals, index=data.index, name=name)
        stats["removed"][name] = removed
        stats["removed_total"] += removed

    if not changed:
        return data
    order = list(data.columns)
    keep = [c for c in order if c not in changed]
    merged = pd.concat([data[keep], pd.DataFrame(changed, index=data.index)], axis=1)
    return merged[order]

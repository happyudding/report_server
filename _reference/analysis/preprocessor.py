from __future__ import annotations

import pandas as pd


def preprocess_file_to_df(file_path) -> pd.DataFrame:
    """전처리 진입점.

    1) content-hash 디스크 캐시 확인.
    2) miss 시 preprocessor_fromhoney.csvfile_to_df 호출
       (모든 파일 형식 dispatch + Structure A 표준 포맷 정규화 — 다른 PC 의 정식 함수로 교체 가능).
    3) downstream code 가 iloc positional 접근으로 읽으므로 정수 컬럼 인덱스로 reset.
    4) 캐시 저장.
    """
    from analysis.df_cache import compute_file_hash, load_cached_df, store_df
    try:
        h = compute_file_hash(file_path)
    except OSError:
        h = None
    if h:
        cached = load_cached_df(h)
        if cached is not None:
            return cached

    from analysis.preprocessor_fromhoney import csvfile_to_df
    df = csvfile_to_df(file_path)
    canonical = _to_canonical(df)
    if h and not canonical.empty:
        try:
            store_df(h, canonical)
        except Exception:
            pass
    return canonical


def _to_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """preprocessor_fromhoney 의 Structure A 출력 (named columns) 을
    downstream 의 positional 접근이 기대하는 정수 컬럼 인덱스로 reset."""
    if df.empty:
        return df
    out = df.copy()
    out.columns = range(df.shape[1])
    return out

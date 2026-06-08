"""honey_parse dummy — 실제 구현 교체 전 임시 폴백.

반환 계약: (df, df_yield)  ← file_to_df.py 계약과 동일
  df       : df_honey 포맷 정규화 DataFrame
  df_yield : 빈 DataFrame (컬럼만 유지)

실제 honey_parse 프로젝트 폴더로 교체하면 이 파일은 덮어씌워진다.
"""
import pandas as pd

DF_YIELD_COLUMNS: list[str] = [
    "step", "Bin", "Tno", "item", "sheetname_cnt", "sheetname"
]


def file_to_df(path, product_type=None, all_paths=None):
    from pathlib import Path
    # csv_loader 가 이미 로드된 시점에 호출되므로 순환참조 없음
    from report_generator.csv_loader import _read_raw, normalize_raw
    raw = _read_raw(Path(path))
    df = normalize_raw(raw)
    df_yield = pd.DataFrame(columns=DF_YIELD_COLUMNS)
    return df, df_yield

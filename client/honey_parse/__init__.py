"""honey_parse dummy — 실제 구현 교체 전 임시 폴백.

반환 계약: (df, df_yield)  ← file_to_df.py 계약과 동일
  df       : df_honey 포맷 정규화 DataFrame
  df_yield : Step,Bin,TNO,Item,<file_label>,<file_label>_cnt wide 포맷.
             테스트 CSV 에 TNO 가 없으므로 임시로 Step="P2" 고정, TNO=Bin 값으로
             채운다 (Bin 별 DUT 개수/yield% 집계).

실제 honey_parse 프로젝트 폴더로 교체하면 이 파일은 덮어씌워진다.
"""
import pandas as pd

from report_generator.constants import DATA_START_ROW, META_COLUMNS

DF_YIELD_COLUMNS: list[str] = ["Step", "Bin", "TNO", "Item"]


def file_to_df(path, product_type=None, all_paths=None):
    from pathlib import Path
    # csv_loader 가 이미 로드된 시점에 호출되므로 순환참조 없음
    from report_generator.csv_loader import _read_raw, normalize_raw
    from report_generator._builders import _fmt_type, _type_sort_key

    p = Path(path)
    raw = _read_raw(p)
    df = normalize_raw(raw)

    bin_idx = META_COLUMNS.index("Bin")
    bins = df.iloc[DATA_START_ROW:, bin_idx].map(_fmt_type)
    total = len(bins)
    if total == 0:
        return df, pd.DataFrame(columns=DF_YIELD_COLUMNS)

    counts = bins.value_counts()
    counts = counts.reindex(sorted(counts.index, key=_type_sort_key))

    label = p.stem[:10]
    df_yield = pd.DataFrame({
        "Step": "P2",
        "Bin": counts.index,
        "TNO": counts.index,
        "Item": "",
        label: (counts / total * 100).round(2).values,
        f"{label}_cnt": counts.values,
    })
    return df, df_yield

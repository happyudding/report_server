"""csvfile_to_df 스텁 — 실제 구현은 honey_parser 에서 import.

반환 계약: (df, df_yield)
  df       : df_honey 포맷 정규화 DataFrame
               행 0=subject명, 1=Units, 2=LowerLimit, 3=UpperLimit,
               4~5=limit 중복행, 6~=측정 데이터
               열 0~4=meta(DUT/XCoord/YCoord/Bin/Serial), 5~=subject 측정값
  df_yield : per-(step, Bin, Tno, item) yield 집계 DataFrame
               컬럼: step, Bin, Tno, item, sheetname_cnt, sheetname

honey_parser 가 설치되면 아래 try 블록이 실제 구현을 로드하고,
미설치 시에는 호출 시점에 ImportError 를 발생시킨다.
"""
from __future__ import annotations

import pandas as pd

DF_YIELD_COLUMNS: list[str] = [
    "step", "Bin", "Tno", "item", "sheetname_cnt", "sheetname"
]

try:
    from honey_parse import csvfile_to_df  # type: ignore[import]
except ImportError:
    def csvfile_to_df(path) -> tuple[pd.DataFrame, pd.DataFrame]:  # type: ignore[misc]
        raise ImportError(
            "honey_parse 패키지가 없습니다. client/honey_parse/ 폴더를 확인하세요."
        )

"""file_to_df 스텁 — 실제 구현은 honey_parser 에서 import.

반환 계약: (df, df_yield)  ※ 불변 구조 — 헤더는 df.columns 로만, row0 은 Units
  df       : df_honey 포맷 정규화 DataFrame
               열(df.columns) = DUT/XCoord/YCoord/Bin/Serial, subject…  (헤더는 컬럼으로만)
               행 0=Units, 1=LowerLimit, 2=UpperLimit, 3~4=limit 중복행, 5~=측정 데이터
               (row0 에 헤더를 중복으로 두지 말 것 — constants.UNITS_ROW=0/DATA_START_ROW=5 와 정합)
  df_yield : per-(Step, Bin, TNO, Item) yield 집계 DataFrame (wide 포맷)
               고정 컬럼: Step, Bin, TNO, Item
               가변 컬럼(파일별 2개): <file_label>, <file_label>_cnt
                 <file_label>      = 해당 bin yield% (= bin 개수 / 전체 개수 × 100)
                 <file_label>_cnt  = 해당 bin DUT 개수
               (DF_YIELD_COLUMNS 는 고정 키 컬럼만 — 빈 DataFrame 폴백용.)

honey_parser 가 설치되면 아래 try 블록이 실제 구현을 로드하고,
미설치 시에는 호출 시점에 ImportError 를 발생시킨다.
"""
from __future__ import annotations

import pandas as pd

DF_YIELD_COLUMNS: list[str] = ["Step", "Bin", "TNO", "Item"]

try:
    from honey_parse import file_to_df  # type: ignore[import]
except ImportError:
    def file_to_df(path, product_type=None, all_paths=None) -> tuple[pd.DataFrame, pd.DataFrame]:  # type: ignore[misc]
        raise ImportError(
            "honey_parse 패키지가 없습니다. client/honey_parse/ 폴더를 확인하세요."
        )

"""report_generator 공용 상수.

_reference 의 config.py 의존을 끊고 client 자립용으로 정의한다.
정규화된 표준 입력 포맷은 5-meta (normalize_raw 가 row 0 을 df.columns 로 승격 후 제거):

    columns : DUT, XCoord, YCoord, Bin, Serial, item1, item2, ...
    row 0   : Units
    row 1   : Lower Limit
    row 2   : Upper Limit
    row 3   : Lower Limit (중복)
    row 4   : Upper Limit (중복)
    row 5+  : 측정 데이터

**불변 규칙**: 헤더(컬럼명)는 오직 df.columns 로만 존재하고 row0 은 Units 다.
row0 에 헤더를 중복으로 남기면 모든 행 인덱스가 1칸씩 밀려(units=subject명, limit 오배정,
가짜 DUT 1행 포함) yield/cpk/fail/distribution 이 전부 깨진다. csvfile_to_df / normalize_raw 가
이 구조를 보장한다(csv_loader._ensure_canonical).
"""

META_COLUMNS = ["DUT", "XCoord", "YCoord", "Bin", "Serial"]
N_META_COLUMNS = len(META_COLUMNS)

UNITS_ROW = 0
LOWER_LIMIT_ROW = 1
UPPER_LIMIT_ROW = 2
DATA_START_ROW = 5

PASS_BIN = "1"

"""report_generator 공용 상수.

_reference 의 config.py 의존을 끊고 client 자립용으로 정의한다.
정규화된 표준 입력 포맷은 5-meta (normalize_raw 가 row 0 을 df.columns 로 승격):

    columns : DUT, XCoord, YCoord, Bin, Serial, item1, item2, ...
    row 0   : Units
    row 1   : Lower Limit
    row 2   : Upper Limit
    row 3   : Lower Limit (중복)
    row 4   : Upper Limit (중복)
    row 5+  : 측정 데이터
"""

META_COLUMNS = ["DUT", "XCoord", "YCoord", "Bin", "Serial"]
N_META_COLUMNS = len(META_COLUMNS)

UNITS_ROW = 0
LOWER_LIMIT_ROW = 1
UPPER_LIMIT_ROW = 2
DATA_START_ROW = 5

PASS_BIN = "1"

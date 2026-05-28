"""report_generator 공용 상수.

_reference 의 config.py 의존을 끊고 client 자립용으로 정의한다.
정규화된 표준 입력 포맷은 5-meta (preprocessor 가 4-meta CSV 에 Serial 삽입):

    columns : DUT, XCoord, YCoord, Bin, Serial, item1, item2, ...
    row 0   : 헤더(컬럼명)
    row 1   : Units
    row 2   : Lower Limit
    row 3   : Upper Limit
    row 4   : Lower Limit (중복)
    row 5   : Upper Limit (중복)
    row 6+  : 측정 데이터
"""

META_COLUMNS = ["DUT", "XCoord", "YCoord", "Bin", "Serial"]
N_META_COLUMNS = len(META_COLUMNS)

SUBJECT_NAME_ROW = 0
UNITS_ROW = 1
LOWER_LIMIT_ROW = 2
UPPER_LIMIT_ROW = 3
DATA_START_ROW = 6

PASS_BIN = "1"

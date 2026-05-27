from __future__ import annotations

from pathlib import Path

import pandas as pd


def csvfile_to_df(file_path) -> pd.DataFrame:
    """모든 입력 파일을 표준 5-meta 포맷의 DataFrame 으로 변환.

    이 파일을 다른 PC 의 정식 함수로 통째 교체 가능. 호출자는 시그니처와 반환형만 의존.

    표준 출력 포맷 (Structure A — named columns, Row 0 가 동일 헤더값):
        df.columns  = ['DUT', 'XCoord', 'YCoord', 'Bin', 'Serial', 'item1', 'item2', ...]
        df.iloc[0]  = ['DUT', 'XCoord', 'YCoord', 'Bin', 'Serial', 'item1', 'item2', ...]
        df.iloc[1]  = ['Units',       None, None, None, None, units...]
        df.iloc[2]  = ['Lower Limit', None, None, None, None, lo...]
        df.iloc[3]  = ['Upper Limit', None, None, None, None, hi...]
        df.iloc[4]  = ['Lower Limit', None, None, None, None, lo...]  (Row 2 와 동일)
        df.iloc[5]  = ['Upper Limit', None, None, None, None, hi...]  (Row 3 과 동일)
        df.iloc[6:] = 측정 데이터 (rows)

    내부 dispatch:
      - row 0 col 0 == 'DUT' (대소문자 무시) → 표준 *school.csv 류
        · 5번째 컬럼이 'Serial' 이 아니면 4-meta 옛 포맷 → Serial 컬럼 자동 삽입
        · Row 4/5 가 누락이면 Row 2/3 와 동일하게 채움
      - col 0 어디든 'Site #' 등장        → test_RP 외부 보고서 포맷
      - 그 외는 표준 경로로 fallback (조용히)

    실패 시 빈 DataFrame 반환.
    """
    path = Path(file_path)
    try:
        if path.suffix.lower() == ".xlsx":
            raw = pd.read_excel(path, header=None, dtype=object)
        else:
            raw = pd.read_csv(
                path, header=None, dtype=object, keep_default_na=False,
            )
    except Exception:
        try:
            raw = pd.read_csv(
                path, header=None, dtype=object, keep_default_na=False,
                engine="python", on_bad_lines="skip",
            )
        except Exception:
            return pd.DataFrame()

    if raw.empty or raw.shape[1] < 5:
        return pd.DataFrame()

    fmt = _detect_format(raw)
    if fmt == "test_rp":
        return _normalize_test_rp(raw)
    return _normalize_standard(raw)


def _detect_format(raw: pd.DataFrame) -> str:
    n = min(100, raw.shape[0])
    for i in range(n):
        v = raw.iat[i, 0]
        if v is None:
            continue
        first = str(v).strip().lower()
        if i == 0 and first == "dut":
            return "standard"
        if first == "site #":
            return "test_rp"
    return "standard"


def _normalize_standard(raw: pd.DataFrame) -> pd.DataFrame:
    """*school.csv 류 표준 포맷 → Structure A 출력."""
    df = raw.copy()

    # 4-meta → 5-meta 자동 변환 (Serial 컬럼 자리에 None 삽입)
    if str(df.iat[0, 4]).strip().lower() != "serial":
        left = df.iloc[:, :4].reset_index(drop=True)
        right = df.iloc[:, 4:].reset_index(drop=True)
        serial = pd.DataFrame({"_s": [None] * len(df)})
        df = pd.concat([left, serial, right], axis=1, ignore_index=True)
        df.iat[0, 4] = "Serial"

    # Row 0~5 의 첫 컬럼 라벨을 사용자 명시 형태로 정규화 (Units / Lower Limit / Upper Limit)
    _canonicalize_row_labels(df)

    # Row 4/5 가 비어있으면 Row 2/3 와 동일 값으로 채움 (a_school 관례 따라가기)
    if df.shape[0] >= 6:
        _fill_duplicate_limit_rows(df)

    # Structure A: 컬럼명을 row 0 값으로 설정
    df.columns = df.iloc[0].tolist()
    return df


def _normalize_test_rp(raw: pd.DataFrame) -> pd.DataFrame:
    """test_RP 외부 보고서 → Structure A 출력.

    Anchor row (col 0 토큰, 대소문자 무시):
      'Test Name'    : subject 이름 (col 5+ 에 값이 있는 첫 매칭)
      'Lower Limit'  : lo
      'Upper Limit'  : hi
      'units'        : units
      'Site #'       : 메타컬럼 헤더 (Site #, Shot, Bin, XCoord, YCoord)
    데이터: 'Site #' 행 다음부터 EOF.
    메타컬럼 매핑: Site#→DUT, XCoord, YCoord, Bin, Shot→Serial.
    """
    def _find(token_lower, require_data=False):
        for i in range(raw.shape[0]):
            v = str(raw.iat[i, 0]).strip().lower()
            if v != token_lower:
                continue
            if require_data:
                tail = [str(x).strip() for x in raw.iloc[i, 5:]]
                if not any(tail):
                    continue
            return i
        return None

    r_subject = _find("test name",   require_data=True)
    r_lower   = _find("lower limit", require_data=True)
    r_upper   = _find("upper limit", require_data=True)
    r_units   = _find("units",       require_data=True)
    r_metahdr = _find("site #",      require_data=False)
    if None in (r_subject, r_lower, r_upper, r_units, r_metahdr):
        return pd.DataFrame()

    metahdr = [str(x).strip().lower() for x in raw.iloc[r_metahdr, :5]]
    name_to_col = {name: i for i, name in enumerate(metahdr)}
    try:
        src_order = [name_to_col[k] for k in ("site #", "xcoord", "ycoord", "bin", "shot")]
    except KeyError:
        return pd.DataFrame()

    subjects = raw.iloc[r_subject, 5:].tolist()
    units    = raw.iloc[r_units,   5:].tolist()
    lo       = raw.iloc[r_lower,   5:].tolist()
    hi       = raw.iloc[r_upper,   5:].tolist()
    header_rows = [
        ["DUT", "XCoord", "YCoord", "Bin", "Serial", *subjects],
        ["Units",       None, None, None, None, *units],
        ["Lower Limit", None, None, None, None, *lo],
        ["Upper Limit", None, None, None, None, *hi],
        ["Lower Limit", None, None, None, None, *lo],
        ["Upper Limit", None, None, None, None, *hi],
    ]

    data_block = raw.iloc[r_metahdr + 1:].reset_index(drop=True)
    meta_part = data_block.iloc[:, :5].iloc[:, src_order].reset_index(drop=True)
    score_part = data_block.iloc[:, 5:].reset_index(drop=True)
    data_part = pd.concat([meta_part, score_part], axis=1)
    data_part.columns = range(data_part.shape[1])

    head_df = pd.DataFrame(header_rows, dtype=object)
    out = pd.concat([head_df, data_part], ignore_index=True)
    out.columns = out.iloc[0].tolist()
    return out


def _canonicalize_row_labels(df: pd.DataFrame) -> None:
    """Row 1~5 의 col 0 라벨을 표준화 + col 1~4 (메타 빈 셀) 를 None 으로 통일.

    'units'/'lo'/'up' 같은 raw 라벨을 'Units'/'Lower Limit'/'Upper Limit' 로 정규화.
    Row 0 (헤더) 와 Row 6+ (데이터) 는 손대지 않음.
    """
    label_map = {
        1: "Units",
        2: "Lower Limit",
        3: "Upper Limit",
        4: "Lower Limit",
        5: "Upper Limit",
    }
    for row_idx, label in label_map.items():
        if row_idx >= df.shape[0]:
            continue
        df.iat[row_idx, 0] = label
        for col in range(1, 5):
            df.iat[row_idx, col] = None


def _fill_duplicate_limit_rows(df: pd.DataFrame) -> None:
    """Row 4/5 가 모두 None 또는 빈 문자열이면 Row 2/3 의 값을 복제.

    a_school_renamed.csv 는 이미 Row 4/5 에 lo/up 중복을 갖지만, 다른 파일이
    Row 4/5 가 비어있는 경우에도 동일한 출력 형태가 되도록 보장.
    """
    for src, dst in ((2, 4), (3, 5)):
        tail_dst = df.iloc[dst, 5:]
        if all((v is None) or (isinstance(v, float) and pd.isna(v)) or (str(v).strip() == "")
               for v in tail_dst):
            df.iloc[dst, :] = df.iloc[src, :].values

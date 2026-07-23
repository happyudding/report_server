"""Raw Data 편집 값 검증 — 편집 경로 전용 순수 모듈.

honeyform.validate_honeyform_df 는 '표의 뼈대'만 본다(컬럼/행 라벨·중복·최소 행).
그 함수는 _decode_parts 에서 **저장된 parquet 을 읽을 때마다** 호출되므로, 값 검증을
거기 넣으면 기존 세션이 열리지 않는다. 값 규칙은 반드시 이 모듈에만 둔다.

두 편집 채널이 공유한다:
  - 웹 셀 편집  : check_cell_value 로 편집한 셀만 검사 → 위반 시 400(하드 거부)
  - Excel 왕복  : sanitize_excel_frame 로 자동 교정 + inspect_edited_frame 로 경고
                  (셀 단위 하드 거부는 하지 않는다 — Excel 은 자유 편집 도구다)

캐시·저장소·flask·xlwings 무의존. client/excel_edit/excel_session.py 가 import 한다.
셀 단위 함수는 **pandas 도 import 하지 않는 순수 파이썬**이다 — 요청당 최대 500회
호출되고, pandas 없는 환경에서도 규칙 테스트를 돌릴 수 있어야 하기 때문.
프레임 단위 함수만 내부에서 pandas/numpy 를 지연 import 한다.
"""
from __future__ import annotations

import re

# 허용하는 숫자 표기 — 파이썬 float() 과 JS Number() 는 받아들이는 문자열이 서로 다르다
# (float 은 '1_000'·전각숫자·'infinity' 를, Number 는 '0x10'·'0b101' 을 받는다). 양쪽이
# 반드시 같은 판정을 해야 하므로(다르면 사용자가 통과시킨 값이 400 으로 튕긴다) 파싱 전에
# 이 정규식으로 표기를 먼저 좁힌다. raw_data.js 의 RAW_NUM_RE 와 **문자 그대로 동일**하게 유지할 것.
# \d 대신 [0-9] 를 쓰는 이유: 파이썬 re 의 \d 는 전각('１２')·아랍숫자('٣')까지 매칭하는데
# JS 의 \d 는 ASCII 전용이라, \d 로 두면 그 문자들에서 양쪽 판정이 갈린다.
_NUM_RE = re.compile(r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$")

# ── 값 규칙 (단일 진실 — 프런트는 rules_spec() 을 받아 해석한다) ──────────────────
# 키는 honeyform.META_COLUMNS 와 같은 집합이어야 한다(테스트가 고정). 여기서 다시
# 적는 것은 '스키마'가 아니라 '컬럼별 값 정책'이기 때문 — pandas 무의존을 지키려고
# honeyform 을 모듈 최상단에서 import 하지 않는다.
META_VALUE_KIND = {
    "SERIAL": "text",
    "SHOT": "int",
    "DUT": "int",
    "XPOS": "int",
    "YPOS": "int",
    "BIN": "int",
    "FAILTNO": "int",
}
REQUIRED_META = ("SERIAL", "BIN")      # 빈값 금지 (나머지 메타와 item 은 빈값 허용)
MAX_TEXT_LEN = 200
_SHOW_LEN = 40                          # 메시지에 되비출 입력값 최대 길이

MESSAGES = {
    "int": "정수만 입력할 수 있습니다 (예: 1, -3). 입력값: {value}",
    "number": "숫자만 입력할 수 있습니다 (예: 1.234, -0.5). 비우면 결측 처리됩니다. 입력값: {value}",
    "required": "비울 수 없습니다.",
    "too_long": f"값이 너무 깁니다 (최대 {MAX_TEXT_LEN}자).",
    "newline": "줄바꿈 문자는 넣을 수 없습니다.",
}

# 차단하지는 않지만 결과가 달라진다고 알려야 하는 것들 (프런트 diff 모달 문안)
WARN_MESSAGES = {
    "bin_change": "pass/fail 판정이 바뀌어 수율이 달라집니다.",
    "coord_blank": "좌표가 비어 이 die 는 Wafer Map 에서 빠집니다 (수율 분모에는 남습니다).",
    "item_blank": "결측 처리되어 CPK 의 n·평균·σ 가 달라집니다.",
    "serial_change": "SERIAL 을 바꾸면 Commonality 조회에서 다른 die 로 인식됩니다.",
}

# 이 셀 수를 넘으면 Excel 채널의 셀 단위 비교·정수 복원을 건너뛴다(확인창에 명시 보고).
# 그보다 큰 입력은 애초에 xlwings used_range.value(리스트-오브-리스트)가 먼저 죽으므로,
# 이 상한은 '우리가 추가하는 비용'만 제어하면 충분하다.
EXCEL_SCAN_CELL_BUDGET = 20_000_000

# 평문 셀 목록(cells)의 상한. 구조화 행(cell_rows)은 표 UI 가 전량을 다루므로 호출부가
# 넘긴 cell_limit 을 따르고, 문자열은 구 평문 빌더/전문 저장용이라 여기서 짧게 끊는다.
_CELL_TEXT_LIMIT = 200


def rules_spec() -> dict:
    """프런트(raw_data.js)가 같은 규칙으로 사전 검증하도록 내려주는 스펙.

    규칙 '테이블'과 문안은 서버가 단일 진실이고, JS 는 판정 프리미티브만 복제한다.
    build_raw_data_columns 응답의 value_rules 필드로 실린다.
    """
    return {
        "meta_kind": dict(META_VALUE_KIND),
        "required_meta": list(REQUIRED_META),
        "max_text_len": MAX_TEXT_LEN,
        "messages": dict(MESSAGES),
        "warnings": dict(WARN_MESSAGES),
    }


# ── 셀 단위 (순수 파이썬) ────────────────────────────────────────────────────────
def _parse_number(text):
    """숫자 문자열 → float. 표기가 _NUM_RE 를 벗어나거나 NaN/Inf 면 None.

    _NUM_RE 가 'nan'/'inf'/'0x10'/'1_000' 을 먼저 걸러낸다 — NaN/Inf 를 통과시키면
    CPK 의 평균·σ 가 오염되고 json_safe 가 inf 를 None 으로 떨궈 화면엔 빈칸만 보인다.
    """
    if not isinstance(text, str) or not _NUM_RE.match(text.strip()):
        return None
    try:
        f = float(text)
    except (TypeError, ValueError):     # 지수부 과다 등 극단 표기 방어
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None                     # '1e999' → inf
    return f


def _parse_int(text):
    """정수 문자열 → int. '5.0'(Excel 왕복 산물)은 수용, '5.5' 는 None."""
    f = _parse_number(text)
    if f is None or not f.is_integer() or abs(f) >= 2 ** 53:
        return None
    return int(f)


def _as_text(value) -> str:
    return "" if value is None else str(value)


def _show(text: str) -> str:
    text = text.strip()
    if not text:
        return "(빈값)"
    return text if len(text) <= _SHOW_LEN else text[:_SHOW_LEN] + "…"


def check_cell_value(column: str, value, *, is_item: bool):
    """편집된 셀 1개의 값 검증. 통과하면 None, 위반하면 한국어 사유 문자열.

    빈값 정책: SERIAL/BIN 은 금지, 그 밖의 메타와 item 은 허용(item 빈값 = 결측).
    XPOS/YPOS 빈값은 여기서 통과시킨다 — 좌표 미상 die 를 넣을 수 없게 만드는 대신
    프런트 diff 모달이 '맵에서 빠진다' 경고를 띄운다.
    """
    text = _as_text(value)
    if "\n" in text or "\r" in text:
        return MESSAGES["newline"]
    stripped = text.strip()

    if is_item:
        if stripped == "":
            return None                      # 결측 허용
        if _parse_number(stripped) is None:
            return MESSAGES["number"].format(value=_show(stripped))
        return None

    kind = META_VALUE_KIND.get(column)
    if kind is None:
        return None                          # 호출부(apply_raw_data_edits)가 이미 걸러냄
    if stripped == "":
        return MESSAGES["required"] if column in REQUIRED_META else None
    if kind == "int":
        if _parse_int(stripped) is None:
            return MESSAGES["int"].format(value=_show(stripped))
        return None
    if len(stripped) > MAX_TEXT_LEN:
        return MESSAGES["too_long"]
    return None


def normalize_cell_value(column: str, value, *, is_item: bool) -> str:
    """검증을 통과한 값을 저장 정규형으로.

    정수 메타 컬럼은 '01'/'1.0'/' 1 ' → '1'. fmt_type("1.0")=="1" 인데
    fmt_type("abc")=="abc" 인 비대칭(같은 BIN 인데 표기에 따라 pass/fail 이 갈리던 문제)을
    저장 시점에 하나로 굳혀 없앤다. item 은 정밀도가 의미이므로 strip 만 한다
    (SERIAL 도 strip 만 — 선행 0 이 식별자의 일부일 수 있다).
    """
    stripped = _as_text(value).strip()
    if is_item or stripped == "":
        return stripped
    if META_VALUE_KIND.get(column) == "int":
        parsed = _parse_int(stripped)
        if parsed is not None:
            return str(parsed)
    return stripped


# ── Excel 프레임 단위 (pandas 지연 import) ───────────────────────────────────────
def sanitize_excel_frame(df):
    """xlwings used_range 프레임의 '조용히 교정 가능한' 오염을 제거한다.

    반환 (df, fixes) — fixes 는 확인창에 보고할 한국어 문자열 리스트(조용한 폴백 금지).
      (a) 데이터 영역의 전 셀 None 인 유령 행 제거 (메타 6행은 보호)
      (b) 헤더가 빈 문자열이고 전 셀이 빈 '후행' 컬럼 제거
      (c) 메타 7컬럼 라벨을 canonical 대문자로 정규화
    정수 dtype 복원은 원본 dtype 정보가 필요해 restore_int_columns 로 분리했다.
    입력 df 는 변형하지 않는다.
    """
    import numpy as np

    from .honeyform import DATA_START_ROW, META_COLUMNS

    fixes = []
    n_meta = len(META_COLUMNS)
    if df is None or df.shape[0] <= DATA_START_ROW or df.shape[1] <= n_meta:
        return df, fixes                       # 뼈대 자체가 깨진 건 validate 가 잡는다

    # (a) 유령 행 — used_range 가 아래로 확장되면 전 셀 None 인 행이 '유효 die' 로 저장돼
    #     수율을 희석한다. isna 만으로 판정한다: object 배열에 대한 `== ""` 비교는 원소별
    #     파이썬 호출로 떨어져 한 자릿수 느린 반면, 실제 used_range 유령은 항상 None 이다
    #     (사용자가 공백을 친 행은 아래 SERIAL/BIN 빈값 경고로 잡힌다).
    body_na = df.iloc[DATA_START_ROW:].isna().to_numpy()
    ghost = body_na.all(axis=1)
    n_ghost = int(ghost.sum())
    if n_ghost:
        keep = np.concatenate([np.ones(DATA_START_ROW, dtype=bool), ~ghost])
        df = df.loc[keep].reset_index(drop=True)
        fixes.append(f"빈 행 {n_ghost}개를 제거했습니다 (Excel 이 데이터 아래 빈 셀까지 범위로 잡음).")

    # (b) 유령 컬럼 — 뒤에서부터, 이름 없고 전부 빈 컬럼만. 후행만 보므로 비용은 제거
    #     개수에 비례한다(전 컬럼 스캔 아님).
    drop = []
    for j in range(df.shape[1] - 1, n_meta - 1, -1):
        if str(df.columns[j]).strip() != "":
            break
        if not df.iloc[:, j].isna().all():
            break
        drop.append(j)
    if drop and df.shape[1] - len(drop) > n_meta:      # item 컬럼이 0개가 되지 않도록
        df = df.drop(df.columns[drop], axis=1)
        fixes.append(f"이름 없는 빈 컬럼 {len(drop)}개를 제거했습니다.")

    # (c) 메타 컬럼명 케이스 — 'Bin' 으로 저장되면 저장은 성공하고 조회만 500 이 난다.
    got = [str(c).strip().lstrip("﻿") for c in df.columns[:n_meta]]
    if got != list(META_COLUMNS) and [g.upper() for g in got] == list(META_COLUMNS):
        df = df.copy(deep=False)
        df.columns = list(META_COLUMNS) + list(df.columns[n_meta:])
        fixes.append(f"메타 컬럼 이름 대소문자를 되돌렸습니다: {got} → {list(META_COLUMNS)}")
    return df, fixes


def restore_int_columns(df, int_columns):
    """xlwings 가 float 로 돌려준 셀을 '원래 정수였던' 컬럼에 한해 int 로 되돌린다.

    int_columns 는 **다운로드 시점 parquet 의 dtype.kind=='i' 인 item 컬럼**이어야 한다 —
    '값이 전부 정수면 int' 로 판정하면 원래 float64 였던 컬럼이 int64 로 뒤집혀 회귀 기준
    (정수 컬럼 int64 보존)을 반대 방향으로 깬다. 메타 코드 컬럼(SHOT..FAILTNO)은 항상 정수라
    int_columns 와 무관하게 복원한다. 반환 (df, 복원 컬럼 수).
    """
    import pandas as pd

    from .honeyform import DATA_START_ROW, META_COLUMNS

    if df is None or df.shape[0] <= DATA_START_ROW:
        return df, 0
    int_columns = set(int_columns or ())
    targets = [c for c in META_COLUMNS[1:] if c in df.columns]        # SERIAL 제외
    targets += [c for c in df.columns[len(META_COLUMNS):] if c in int_columns]

    restored = 0
    index = df.index[DATA_START_ROW:]
    for c in targets:
        col = df[c].iloc[DATA_START_ROW:]
        num = pd.to_numeric(col, errors="coerce")
        if not num.notna().any():
            continue
        try:
            # Int64 캐스팅은 비정수 float 에서 예외를 낸다 — '모두 정수' 판정과 변환을
            # 1 pass 로 합친다(사용자가 5.5 를 넣었으면 정직하게 float 로 남긴다).
            ints = num.astype("Int64")
        except (TypeError, ValueError):
            continue
        # 숫자로 못 읽은 셀(사용자 오타 등)은 원본을 그대로 둔다 — None 으로 덮으면
        # 뒤따르는 inspect 가 '값이 지워졌다' 로 보고해 진짜 오타 경고를 가린다.
        new = ints.astype(object).where(num.notna(), col)
        # object 컬럼끼리는 5 == 5.0 이라 equals() 가 int↔float 변화를 못 잡는다
        # (그래서 복원이 통째로 건너뛰어졌다). 값이 아니라 타입으로 비교한다.
        if any(type(a) is not type(b) for a, b in zip(new, col)):
            df.loc[index, c] = new.to_numpy()
            restored += 1
    return df, restored


def _meta_row_maps(df, item_cols):
    """메타 6행(TSEQ..LOLIM)을 {item: 값} dict 6개로 — inspect 전용 경량 추출."""
    from .honeyform import META_ROW_LABELS

    head = df.iloc[:len(META_ROW_LABELS)][item_cols].to_numpy(dtype=object)
    return {label: dict(zip(item_cols, head[i])) for i, label in enumerate(META_ROW_LABELS)}


def _meta_row_warnings(df, item_cols, *, cap=8):
    """메타 6행 값 경고 — 규격 뒤집힘/비수치, TNO 빈값·0·중복. (하드 거부는 하지 않는다)"""
    rows = _meta_row_maps(df, item_cols)
    hilim, lolim, tno = rows["HILIM"], rows["LOLIM"], rows["TNO"]

    swapped, bad_limit, blank_tno = [], [], []
    seen, dup = {}, []
    for c in item_cols:
        hi, lo = _parse_number(_as_text(hilim.get(c)).strip()), _parse_number(_as_text(lolim.get(c)).strip())
        hi_txt, lo_txt = _as_text(hilim.get(c)).strip(), _as_text(lolim.get(c)).strip()
        if (hi_txt and hi is None) or (lo_txt and lo is None):
            bad_limit.append(c)
        elif hi is not None and lo is not None and lo > hi:
            swapped.append(c)
        t = _as_text(tno.get(c)).strip()
        t_num = _parse_number(t)
        if not t or t_num == 0:
            blank_tno.append(c)
        elif t_num is not None:
            key = str(int(t_num)) if float(t_num).is_integer() else t
            if key in seen:
                dup.append(f"{seen[key]}↔{c}")
            else:
                seen[key] = c

    def _list(names):
        head = ", ".join(names[:cap])
        return head + (f" 외 {len(names) - cap}개" if len(names) > cap else "")

    out = []
    if swapped:
        out.append(f"규격 상하한이 뒤집힌 항목 {len(swapped)}개 [{_list(swapped)}] — CPK 가 "
                   f"음수로 계산돼 Issue Table CPK 섹션에 항상 이슈로 올라옵니다.")
    if bad_limit:
        out.append(f"규격을 숫자로 읽을 수 없는 항목 {len(bad_limit)}개 [{_list(bad_limit)}] — "
                   f"CPK 가 계산되지 않습니다.")
    if blank_tno:
        out.append(f"TNO 가 비었거나 0 인 항목 {len(blank_tno)}개 [{_list(blank_tno)}] — 이 항목의 "
                   f"fail die 는 Yield 표 어느 행에도 집계되지 않습니다.")
    if dup:
        out.append(f"같은 TNO 를 쓰는 항목 {len(dup)}쌍 [{_list(dup)}] — TEST SEQ 가 가장 앞선 "
                   f"1개에만 fail 이 집계되고 나머지는 0 이 됩니다.")
    return out


def _value_warnings(df, item_cols, *, cap=8):
    """데이터 영역 값 경고 — 비수치 측정값, BIN 비정수, SERIAL 빈값, XY 비좌표."""
    import pandas as pd

    from .honeyform import DATA_START_ROW

    body = df.iloc[DATA_START_ROW:]
    out = []

    bad_items = []
    total_bad = 0
    for c in item_cols:
        col = body[c]
        filled = col.notna() & (col.astype("string").str.strip() != "")
        if not filled.any():
            continue
        bad = int((pd.to_numeric(col, errors="coerce").isna() & filled).sum())
        if bad:
            total_bad += bad
            bad_items.append(f"{c} {bad}건")
    if total_bad:
        head = ", ".join(bad_items[:cap])
        more = f" 외 {len(bad_items) - cap}개 항목" if len(bad_items) > cap else ""
        out.append(f"숫자로 읽을 수 없는 측정값 {total_bad}개 (항목 {len(bad_items)}개) [{head}{more}] — "
                   f"결측으로 처리되어 CPK 의 n·평균·σ 가 달라집니다. 항목 전체가 비수치면 그 항목이 "
                   f"CPK/Distribution 에서 사라집니다.")

    # 메타 4컬럼은 행 수만큼 크므로 원소별 lambda 대신 to_numeric 으로 벡터 판정한다
    # (1e6 행에서 파이썬 호출이면 수 초, 이 방식이면 밀리초).
    def _txt(col):
        return body[col].astype("string").fillna("").str.strip()

    bin_num = pd.to_numeric(_txt("BIN"), errors="coerce")
    bad_bin = int((bin_num.isna() | (bin_num % 1 != 0)).sum())
    if bad_bin:
        out.append(f"BIN 이 비었거나 정수가 아닌 die {bad_bin}개 — pass(1)로 인식되지 않아 fail 로 "
                   f"집계됩니다(수율 변동).")
    blank_serial = int((_txt("SERIAL") == "").sum())
    if blank_serial:
        out.append(f"SERIAL 이 빈 die {blank_serial}개.")
    bad_xy = int((pd.to_numeric(_txt("XPOS"), errors="coerce").isna()
                  | pd.to_numeric(_txt("YPOS"), errors="coerce").isna()).sum())
    if bad_xy:
        out.append(f"XPOS/YPOS 를 좌표로 읽을 수 없는 die {bad_xy}개 — Wafer Map 에서 빠집니다 "
                   f"(수율 분모에는 남아 map die 수와 수율 total 이 어긋납니다).")
    return out


def _cell_getter(body):
    """(컬럼, 행위치) → 문자열 접근자. 컬럼 Series 만 캐시하고 값은 그때그때 읽는다.

    행마다 `body.iloc[pos]` 로 Series 를 만들면 수만 건 diff 에서 느리고, 반대로 전 컬럼을
    문자열 리스트로 펼치면 대형 프레임에서 메모리가 터진다. 그 사이를 취한다.
    """
    cache = {}

    def get(col, pos) -> str:
        s = cache.get(col)
        if s is None:
            if col not in body.columns:
                return ""
            s = cache[col] = body[col]
        return _as_text(s.iat[pos])

    return get


def _row_loc(get, pos) -> dict:
    """데이터 행 위치(0-base) → 위치 메타 값 (표의 SHOT/DUT/X/Y/BIN 열)."""
    return {
        "row": pos + 1,
        "shot": get("SHOT", pos).strip(),
        "dut": get("DUT", pos).strip(),
        "x": get("XPOS", pos).strip(),
        "y": get("YPOS", pos).strip(),
        "bin": get("BIN", pos).strip(),
    }


def _row_label(loc) -> str:
    """위치 메타 → 사람이 읽는 위치 문자열 (웹 diff 표와 같은 조합)."""
    parts = []
    for label, key in (("SHOT", "shot"), ("DUT", "dut")):
        if loc[key]:
            parts.append(f"{label} {loc[key]}")
    if loc["x"] or loc["y"]:
        parts.append(f"(X,Y)=({loc['x']},{loc['y']})")
    if loc["bin"]:
        parts.append(f"BIN {loc['bin']}")
    return " · ".join(parts) or f"{loc['row']}번째 행"


def _cell_diff(old_df, new_df, *, cell_limit):
    """형태가 같은 두 프레임의 셀 단위 차이 → (표시용 문자열, 구조화 행, 전체 건수).

    구조화 행은 확인창의 표(열 = 위치/항목/이전/이후)용이고, 문자열은 같은 행에서
    파생시킨다 — 두 표현이 갈라지지 않게 조립 지점을 한 곳에 둔다. 문자열은 구 평문
    빌더·전문 저장용이라 `_CELL_TEXT_LIMIT` 에서 끊고, 행은 cell_limit 까지 담는다.
    """
    import numpy as np
    import pandas as pd

    from .honeyform import DATA_START_ROW, META_COLUMNS

    old_body = old_df.iloc[DATA_START_ROW:].reset_index(drop=True)
    new_body = new_df.iloc[DATA_START_ROW:].reset_index(drop=True)
    cols = list(new_df.columns)
    item_cols = cols[len(META_COLUMNS):]

    def _num_block(frame):
        return frame[item_cols].apply(
            lambda s: pd.to_numeric(s, errors="coerce")).to_numpy(dtype="float64")

    blocks = []
    if item_cols:
        a, b = _num_block(old_body), _num_block(new_body)
        # NaN != NaN 이므로 '양쪽 다 결측'을 변경으로 세지 않도록 뺀다
        blocks.append((item_cols, (a != b) & ~(np.isnan(a) & np.isnan(b))))
    meta_cols = cols[:len(META_COLUMNS)]
    a_m = old_body[meta_cols].astype("string").fillna("").apply(lambda s: s.str.strip()).to_numpy()
    b_m = new_body[meta_cols].astype("string").fillna("").apply(lambda s: s.str.strip()).to_numpy()
    blocks.append((meta_cols, a_m != b_m))

    total = sum(int(mask.sum()) for _, mask in blocks)
    old_get, new_get = _cell_getter(old_body), _cell_getter(new_body)
    lines, rows = [], []
    for names, mask in blocks:
        if len(rows) >= cell_limit or not mask.any():
            continue
        flat = np.flatnonzero(mask.ravel())[:cell_limit - len(rows)]
        n_cols = len(names)
        for f in flat:
            r, c = int(f) // n_cols, int(f) % n_cols
            col = str(names[c])
            # 위치 라벨은 원본(old) 기준 — 편집으로 좌표가 바뀌어도 '어느 행이었는지'가 남는다.
            loc = _row_loc(old_get, r)
            old_text = _show(old_get(col, r))
            new_text = _show(new_get(col, r))
            rows.append({**loc, "item": col, "old": old_text, "new": new_text})
            if len(lines) < _CELL_TEXT_LIMIT:
                lines.append(f"{_row_label(loc)} → [{col}] {old_text} → {new_text}")
    return lines, rows, total


def inspect_edited_frame(old_df, new_df, *, source_name="", cell_limit=20,
                         cell_budget=EXCEL_SCAN_CELL_BUDGET) -> dict:
    """원본 대비 편집 프레임을 비교해 확인창용 요약을 만든다. **절대 raise 하지 않는다**
    (하드 거부는 encode_honeyform_parquet 이 담당하고 재편집 루프가 받는다).

    cell_limit 은 **구조화 행(cell_rows)** 의 상한이다 — 표 UI 가 전량을 다루므로 호출부가
    크게 잡는다. 평문 목록(cells)은 `_CELL_TEXT_LIMIT` 에서 따로 끊는다.

    반환 {"source", "structure":[], "meta_warnings":[], "value_warnings":[],
          "cells":[], "cell_rows":[], "cell_total":int, "skipped_cell_diff":bool}
    """
    from .honeyform import DATA_START_ROW, META_COLUMNS

    out = {"source": source_name, "structure": [], "meta_warnings": [],
           "value_warnings": [], "cells": [], "cell_rows": [], "cell_total": 0,
           "skipped_cell_diff": False}
    # 뼈대가 깨진 입력(메타 6행 부족 등)은 여기서 판단하지 않는다 — encode 가 거부한다.
    if new_df is None or len(new_df) <= DATA_START_ROW or len(new_df.columns) <= len(META_COLUMNS):
        out["skipped_cell_diff"] = True
        return out
    item_cols = [str(c) for c in new_df.columns[len(META_COLUMNS):]]

    # 구조 비교 (항상, O(컬럼수))
    if old_df is not None:
        old_items = [str(c) for c in old_df.columns[len(META_COLUMNS):]]
        added = [c for c in item_cols if c not in set(old_items)]
        removed = [c for c in old_items if c not in set(item_cols)]
        if len(added) == 1 and len(removed) == 1:
            out["structure"].append(
                f"측정 항목 이름이 '{removed[0]}' → '{added[0]}' 로 바뀐 것 같습니다 — 이 항목은 "
                f"Report 전 탭에서 빠지고, Issue Table 의 코멘트/Status/숨김 연결도 끊깁니다.")
        else:
            if added:
                out["structure"].append(
                    f"새로 생긴 측정 항목 {len(added)}개 [{', '.join(added[:8])}] — 업로드 당시 선택된 "
                    f"항목이 아니라 Report 에 나타나지 않습니다.")
            if removed:
                out["structure"].append(
                    f"사라진 측정 항목 {len(removed)}개 [{', '.join(removed[:8])}] — Report 전 탭에서 "
                    f"빠지고 Issue Table 의 코멘트/Status/숨김 연결도 끊깁니다.")
        old_rows, new_rows = len(old_df) - DATA_START_ROW, len(new_df) - DATA_START_ROW
        if old_rows != new_rows:
            out["structure"].append(
                f"측정 행이 {old_rows:,}행 → {new_rows:,}행 ({new_rows - old_rows:+,}행) 바뀌었습니다.")

    # 값 경고는 메타 7컬럼이 제자리에 있을 때만 — 사용자가 컬럼명을 통째로 바꿨으면 여기서
    # KeyError 를 내는 대신 조용히 건너뛰고, 곧이어 encode_honeyform_parquet 이 한국어
    # ValueError 로 거부해 재편집 루프로 보낸다(그 루프는 ValueError 만 잡는다).
    if list(new_df.columns[:len(META_COLUMNS)]) == list(META_COLUMNS):
        if item_cols:
            out["meta_warnings"] = _meta_row_warnings(new_df, item_cols)
        out["value_warnings"] = _value_warnings(new_df, item_cols)
    else:
        out["skipped_cell_diff"] = True
        return out

    n_cells = max(len(new_df) - DATA_START_ROW, 0) * max(len(new_df.columns), 1)
    same_shape = (old_df is not None
                  and list(old_df.columns) == list(new_df.columns)
                  and len(old_df) == len(new_df))
    if not same_shape or n_cells > cell_budget:
        out["skipped_cell_diff"] = True
        return out
    out["cells"], out["cell_rows"], out["cell_total"] = _cell_diff(
        old_df, new_df, cell_limit=cell_limit)
    return out


def build_confirm_sections(reports, removed_names=(), fixes_by_source=None) -> dict:
    """확인 UI 용 **구조화** 요약 — 줄 수 상한 없이 source 별 섹션으로 돌려준다.

    build_confirm_message 는 QMessageBox 한 칸에 다 넣느라 40줄에서 잘라야 했다(잘린
    나머지는 사용자가 볼 방법이 없었고, 그 전에 창이 화면을 넘어가 버튼이 사라졌다).
    스크롤 가능한 다이얼로그는 전량을 보여줄 수 있으므로 자르지 않는다.

    반환 {"totals": {...}, "sections": [{"name", "structure", "fixes", "cells",
    "cell_rows", "cell_total", "skipped_cell_diff", "warnings"}], "removed": [...]}
    — 표시 문안(불릿 접두어·순서)은 UI 소관이고 여기서는 **재료만** 준다.
    변경이 전혀 없으면 sections/removed 가 모두 비고 totals 가 0 이다(호출부가 확인창 생략).
    """
    fixes_by_source = fixes_by_source or {}
    sections = []
    totals = {"sources": 0, "cells": 0, "warnings": 0, "fixes": 0, "removed": 0}
    for rep in reports or []:
        name = rep.get("source") or ""
        warnings = list(rep.get("meta_warnings") or []) + list(rep.get("value_warnings") or [])
        fixes = list(fixes_by_source.get(name) or [])
        section = {
            "name": name,
            "structure": list(rep.get("structure") or []),
            "fixes": fixes,
            "cells": list(rep.get("cells") or []),
            "cell_rows": list(rep.get("cell_rows") or []),
            "cell_total": int(rep.get("cell_total") or 0),
            "skipped_cell_diff": bool(rep.get("skipped_cell_diff")),
            "warnings": warnings,
        }
        if not (section["structure"] or fixes or section["cell_total"]
                or section["skipped_cell_diff"] or warnings):
            continue                      # 이 source 는 변경 없음 — 섹션을 만들지 않는다
        sections.append(section)
        totals["sources"] += 1
        totals["cells"] += section["cell_total"]
        totals["warnings"] += len(warnings)
        totals["fixes"] += len(fixes)

    removed = [str(n) for n in (removed_names or [])]
    totals["removed"] = len(removed)
    return {"totals": totals, "sections": sections, "removed": removed}


def build_confirm_message(reports, removed_names=(), fixes_by_source=None,
                          *, max_lines=40) -> str:
    """source 별 inspect 결과 + 자동 교정 + 시트 삭제 목록 → 확인 다이얼로그 본문(한국어).

    변경이 전혀 없으면 빈 문자열을 돌려준다(호출부가 확인창을 건너뛴다).
    max_lines 를 넘으면 잘라낸다 — QMessageBox 가 길면 읽히지 않는다.
    """
    fixes_by_source = fixes_by_source or {}
    lines = []
    for rep in reports or []:
        name = rep.get("source") or ""
        body = []
        for text in rep.get("structure", []):
            body.append(f"· {text}")
        for text in fixes_by_source.get(name, []):
            body.append(f"· [자동 교정] {text}")
        if rep.get("cell_total"):
            body.append(f"· 셀 {rep['cell_total']:,}개가 바뀌었습니다:")
            body.extend(f"    - {c}" for c in rep.get("cells", []))
            if rep["cell_total"] > len(rep.get("cells", [])):
                body.append(f"    - … 외 {rep['cell_total'] - len(rep.get('cells', [])):,}건")
        elif rep.get("skipped_cell_diff"):
            body.append("· 셀 단위 비교를 생략했습니다 (구조가 바뀌었거나 데이터가 큽니다).")
        for text in rep.get("meta_warnings", []):
            body.append(f"· [경고] {text}")
        for text in rep.get("value_warnings", []):
            body.append(f"· [경고] {text}")
        if body:
            lines.append(f"[{name}]" if name else "[source]")
            lines.extend(body)
            lines.append("")

    if removed_names:
        lines.append(f"[시트 삭제 감지] {', '.join(str(n) for n in removed_names)}")
        lines.append("해당 source 데이터가 리포트에서 제거되고 전체 탭이 재계산됩니다. "
                     "서버에서 되돌릴 수 없습니다.")
        lines.append("")

    if not lines:
        return ""
    if len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = lines[:max_lines] + [f"… 외 {hidden}줄 생략", ""]
    lines.append("위 내용으로 서버에 반영할까요?")
    return "\n".join(lines)

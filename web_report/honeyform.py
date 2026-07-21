"""7-meta honeyform DataFrame helpers.

Canonical layout:
columns: SERIAL, SHOT, DUT, XPOS, YPOS, BIN, FAILTNO, item...
row0..5: TSEQ, TNO, STEP, UNIT, HILIM, LOLIM
row6+  : measurement data
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pandas as pd

_log = logging.getLogger(__name__)

META_COLUMNS = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO"]
META_ROW_LABELS = ["TSEQ", "TNO", "STEP", "UNIT", "HILIM", "LOLIM"]
DATA_START_ROW = len(META_ROW_LABELS)
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"


@dataclass
class HoneyformTable:
    source: str
    file_name: str
    # 전체 프레임(메타 6행 + 데이터, object dtype) — 편집/재인코딩 경로 전용.
    # 읽기 경로(decode_split keep_df=False)는 None — data/meta dict 만으로 충분하고
    # object dtype 프레임이 캐시 메모리의 절반 이상을 차지하기 때문 (Phase 5).
    df: pd.DataFrame | None
    item_columns: list[str]
    tseq: dict[str, object]
    tno: dict[str, object]
    step: dict[str, object]
    units: dict[str, object]
    hilim: dict[str, object]
    lolim: dict[str, object]
    data: pd.DataFrame


def _norm(value) -> str:
    return str(value).strip().lstrip("\ufeff").upper()


def kor_issue(issue: str) -> str:
    """validate_honeyform_df \uc601\ubb38 \uc774\uc288 1\uac74 \u2192 \ud55c\uad6d\uc5b4 \uc124\uba85 (\uc5b4\ub514\uac00 \uc548 \ub9de\ub294\uc9c0).

    \uc601\ubb38 \uba54\uc2dc\uc9c0\ub294 \ub85c\uadf8\u00b7\ubd84\uae30\uc6a9 '\uc548\uc815 \ud0a4'\ub85c \uc720\uc9c0\ud558\uace0, \uc0ac\uc6a9\uc790\uc5d0\uac8c \ubcf4\uc774\ub294 \uc9c0\uc810(\uc6f9 \ud3b8\uc9d1 400
    \ubc30\ub108 \u00b7 Excel \uc7ac\ud3b8\uc9d1 \ub8e8\ud504)\ub9cc \uc774 \ud568\uc218\ub97c \ud1b5\uacfc\uc2dc\ud0a8\ub2e4. \uc54c \uc218 \uc5c6\ub294 \uc774\uc288\ub294 \uc6d0\ubb38\uc744 \uadf8\ub300\ub85c
    \ub3cc\ub824\uc8fc\ubbc0\ub85c \uc2e0\uaddc \uc774\uc288\uac00 \ub298\uc5b4\ub3c4 \uc548\uc804\ud558\ub2e4.
    """
    def _after(sep):
        return issue.split(sep, 1)[1].strip() if sep in issue else ""

    if issue.startswith("first 7 columns must be"):
        return f"\uc55e 7\uac1c \uceec\ub7fc\uc774 \uaddc\uaca9\uacfc \ub2e4\ub985\ub2c8\ub2e4 \u2192 \uc2e4\uc81c: {_after('got')}"
    if issue.startswith("metadata row labels must be"):
        return f"\uba54\ud0c0 \ud589 \ub808\uc774\ube14(A\uc5f4)\uc774 \uaddc\uaca9\uacfc \ub2e4\ub985\ub2c8\ub2e4 \u2192 \uc2e4\uc81c: {_after('got')}"
    if issue.startswith("duplicate item columns"):
        return f"\uce21\uc815 \ud56d\ubaa9 \uceec\ub7fc \uc774\ub984\uc774 \uc911\ubcf5\ub429\ub2c8\ub2e4: {_after(':')}"
    if issue.startswith("item columns collide with meta columns"):
        return (f"\uce21\uc815 \ud56d\ubaa9 \uc774\ub984\uc774 \uba54\ud0c0 \uceec\ub7fc \uc774\ub984\uacfc \uacb9\uce69\ub2c8\ub2e4: {_after(':')} \u2014 \ud56d\ubaa9 \uc774\ub984\uc744 "
                f"\ubc14\uafd4 \uc8fc\uc138\uc694 ({'/'.join(META_COLUMNS)} \uc740 \uc608\uc57d\uc5b4\uc785\ub2c8\ub2e4)")
    if "item column are required" in issue:
        return "\uceec\ub7fc \ubd80\uc871: \uc55e 7\uac1c \uba54\ud0c0 \uceec\ub7fc\uacfc \uce21\uc815 \ud56d\ubaa9 1\uac1c \uc774\uc0c1\uc774 \ud544\uc694\ud569\ub2c8\ub2e4"
    if "metadata rows are required" in issue:
        return "\uba54\ud0c0\ub370\uc774\ud130 \ud589(TSEQ/TNO/STEP/UNIT/HILIM/LOLIM 6\ud589)\uc774 \ubd80\uc871\ud569\ub2c8\ub2e4"
    if "data row is required" in issue:
        return "\uce21\uc815 \ub370\uc774\ud130\uac00 \ucd5c\uc18c 1\ud589 \ud544\uc694\ud569\ub2c8\ub2e4"
    return issue    # \uc54c \uc218 \uc5c6\ub294 \uc774\uc288\ub294 \uc6d0\ubb38 \ub178\ucd9c


def kor_issues(issues: list) -> str:
    """\uc774\uc288 \ubaa9\ub85d \u2192 \uc0ac\uc6a9\uc790\uc6a9 \uc5ec\ub7ec \uc904 \ud55c\uad6d\uc5b4 \uba54\uc2dc\uc9c0 (raise ValueError \uc6a9)."""
    return "\n".join(f"\u00b7 {kor_issue(i)}" for i in issues)


def canonicalize_meta_columns(df: pd.DataFrame) -> pd.DataFrame:
    """\uc55e 7\uceec\ub7fc \ub77c\ubca8\uc744 canonical META_COLUMNS \ub85c \ub418\ub3cc\ub9b0 \uc595\uc740 \ubcf5\uc0ac\ubcf8.

    validate_honeyform_df \uac00 _norm(strip+BOM+upper) \uae30\uc900 \uc77c\uce58\ub97c \uc774\ubbf8 \ubcf4\uc7a5\ud55c \ub4a4\uc5d0\ub9cc
    \ud638\ucd9c\ud558\ubbc0\ub85c \uc774 \uce58\ud658\uc740 **\uc815\ubcf4\ub97c \uc783\uc9c0 \uc54a\ub294\ub2e4**('Bin'\u2192'BIN', BOM \uc81c\uac70).
    \ud558\ub958(common.py data["BIN"] \u00b7 raw_data.py data["SERIAL"] \u00b7 Map_analysis data["XPOS"])\ub294
    \uc804\ubd80 \ub300\ubb38\uc790 \ud558\ub4dc\ucf54\ub529\uc774\ub77c, \uc815\uaddc\ud654\uac00 \uc5c6\uc73c\uba74 \uc800\uc7a5\uc740 \uc131\uacf5\ud558\uace0 \uc870\ud68c\ub9cc KeyError\u2192500 \uc774 \ub41c\ub2e4.
    """
    out = df.copy(deep=False)
    out.columns = list(META_COLUMNS) + [str(c) for c in df.columns[len(META_COLUMNS):]]
    return out


def dedupe_item_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """item 컬럼명 중복을 등장 순서대로 _2, _3 … 접미사로 개명한 얕은 복사본.

    같은 측정 항목명이 소스에 물리적으로 2번 들어있는 파일(pandas read_csv 의 dedup 을
    거치지 않고 컬럼 라벨을 직접 대입한 로더 산물)을 거부하지 않고 통과시키기 위한 것.
    개명 없이 두면 split 의 `df.at[0, c]` 라벨 조회가 Series 를 반환하고 tseq/tno/units
    dict 키가 뭉개져 메타가 유실된다.

    **첫 등장은 원본 이름을 그대로 둔다** — 중복이 아닌 항목명과 중복 그룹의 첫 항목은
    이름이 안 바뀌어야 편집 DB 의 item_key(edits.py)가 보존되고, 이미 개명된 프레임을
    다시 넣어도 이름이 또 밀리지 않는다(Excel 왕복 재인코딩이 이 멱등성에 의존한다).
    이름 비교는 strip() 후 문자 그대로 — 대소문자로만 구분되는 별개 측정 항목은 합치지 않는다.

    메타 7컬럼 라벨은 건드리지 않고(canonicalize_meta_columns 담당), item 이름이 메타명과
    겹치는 경우도 개명하지 않는다 — validate 의 collide 검사가 그대로 거부한다.
    반환: (프레임, [(원본명, 개명명), ...]). 바꿀 게 없으면 원본 df 객체를 그대로 돌려준다.
    """
    n_meta = len(META_COLUMNS)
    if not isinstance(df, pd.DataFrame) or len(df.columns) <= n_meta:
        return df, []
    items = [str(c).strip() for c in df.columns[n_meta:]]
    # 생성한 접미사가 원본에 이미 있는 이름과 부딪히지 않게 한다 — 'A, A, A_2' 에서
    # 2번째 A 를 A_2 로 만들면 원본 A_2 를 침범하므로 A_3 으로 밀어낸다.
    taken = {str(c).strip() for c in df.columns}
    seen: Counter = Counter()
    out: list[str] = []
    renames: list[tuple[str, str]] = []
    for name in items:
        seen[name] += 1
        if seen[name] == 1:
            out.append(name)
            continue
        n = seen[name]
        while f"{name}_{n}" in taken:
            n += 1
        new = f"{name}_{n}"
        taken.add(new)
        out.append(new)
        renames.append((name, new))
    if not renames and out == [str(c) for c in df.columns[n_meta:]]:
        return df, []
    if renames:
        _log.warning("duplicate item columns renamed: %s", renames)
    frame = df.copy(deep=False)
    frame.columns = list(df.columns[:n_meta]) + out
    return frame, renames


def validate_honeyform_df(df: pd.DataFrame) -> list[str]:
    issues: list[str] = []
    if df is None:
        return ["df is None"]
    if not isinstance(df, pd.DataFrame):
        return [f"df must be pandas.DataFrame, got {type(df)!r}"]

    n_rows, n_cols = df.shape
    if n_cols < len(META_COLUMNS) + 1:
        issues.append("meta 7 columns + at least 1 item column are required")
    else:
        got_cols = [_norm(c) for c in list(df.columns[:len(META_COLUMNS)])]
        if got_cols != META_COLUMNS:
            issues.append(f"first 7 columns must be {META_COLUMNS}, got {got_cols}")

    if n_rows < DATA_START_ROW:
        issues.append(f"{DATA_START_ROW} metadata rows are required")
    elif n_cols:
        got_labels = [_norm(df.iloc[i, 0]) for i in range(DATA_START_ROW)]
        if got_labels != META_ROW_LABELS:
            issues.append(f"metadata row labels must be {META_ROW_LABELS}, got {got_labels}")

    item_cols = [str(c) for c in list(df.columns[len(META_COLUMNS):])]
    counts = Counter(item_cols)
    duplicates = sorted(c for c, n in counts.items() if n > 1)
    if duplicates:
        issues.append(f"duplicate item columns: {duplicates}")
    # item 이름이 메타 컬럼명과 겹치면 tail[meta_labels] 가 같은 라벨로 2개 컬럼을 뽑아
    # 컬럼이 밀린다(TNO/HILIM 이 엉뚱한 항목에 배정되거나 500). 위 중복 검사는 item 끼리만
    # 세므로 여기서 따로 잡는다 — 이런 파일은 현재도 이미 깨지므로 막아도 회귀가 아니다.
    collide = sorted({c for c in item_cols if _norm(c) in set(META_COLUMNS)})
    if collide:
        issues.append(f"item columns collide with meta columns: {collide}")
    if n_rows <= DATA_START_ROW:
        issues.append("at least 1 data row is required")
    return issues


def _require_parquet_engine() -> None:
    try:
        import pyarrow  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("pyarrow is required for web report parquet payloads") from exc


def _string_frame_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    # per-column astype 루프(2000+ 컬럼 × 프레임 재배치) 대신 일괄 변환 — 값 동일, 속도만 개선
    out = df.astype("string")
    # 앞 7컬럼은 canonical 대문자로 강제 — 'Bin'/BOM 붙은 헤더가 그대로 저장되면 저장은
    # 성공하고 조회만 data["BIN"] KeyError 로 500 이 난다(validate 는 대소문자 무시라 통과).
    out.columns = list(META_COLUMNS) + [str(c).strip() for c in df.columns[len(META_COLUMNS):]]
    return out


def encode_honeyform_parquet(df: pd.DataFrame, *, compression: str = "zstd") -> bytes:
    """Encode a validated honeyform DataFrame to parquet bytes."""
    # 검증보다 먼저 개명한다. _string_frame_for_parquet 의 strip 이 검증 뒤에 걸리므로,
    # 'A' 와 'A ' 는 여기서 묶지 않으면 검증을 통과한 뒤 parquet 안에서 비로소 중복이 되어
    # 서버 _decode_parts 재검증에서 터진다(클라는 성공했는데 업로드만 실패).
    df, _ = dedupe_item_columns(df)
    issues = validate_honeyform_df(df)
    if issues:
        raise ValueError(kor_issues(issues))
    _require_parquet_engine()
    buf = BytesIO()
    _string_frame_for_parquet(df).to_parquet(
        buf, index=False, engine="pyarrow", compression=compression)
    return buf.getvalue()


def _numeric_item_block(frame: pd.DataFrame, item_labels: list) -> pd.DataFrame:
    """item 컬럼 블록을 per-column pd.to_numeric 으로 변환해 DataFrame 으로 반환.

    per-column 변환을 유지하는 이유: 정수 전용 컬럼은 int64, 그 외는 float64 로
    dtype 이 갈리는 기존 동작(payload 의 1 vs 1.0 표기)을 보존해야 한다.
    다만 2000+ 컬럼 각각을 .loc 로 조회/대입하면 프레임 재배치로 수 초가 걸리므로
    numpy 블록에서 변환해 한 번에 조립한다 (값·dtype 동일, 속도만 개선).
    """
    vals = frame[item_labels].to_numpy(dtype=object)
    conv = {c: pd.to_numeric(vals[:, j], errors="coerce")
            for j, c in enumerate(item_labels)}
    return pd.DataFrame(conv, index=frame.index, columns=item_labels)


def _object_with_none(frame: pd.DataFrame) -> pd.DataFrame:
    # astype(object).where(...) 는 컬럼(2000+) 단위 오버헤드가 커서 numpy 로 일괄 처리
    vals = frame.to_numpy(dtype=object)
    vals[pd.isna(vals)] = None
    return pd.DataFrame(vals, index=frame.index, columns=frame.columns)


def _decode_parts(data: bytes):
    """parquet bytes → 검증 후 (head, tail_meta, num_df, item_labels) 조립 부품.

    item 데이터 셀(전체의 99%)은 어차피 numeric 으로 변환하므로 object+None 변환은
    메타 행/컬럼에만 적용한다 (전체 프레임 astype(object).where 왕복 제거).
    """
    _require_parquet_engine()
    raw = pd.read_parquet(BytesIO(data), engine="pyarrow")
    # 구버전 클라가 올린 중복 컬럼 parquet 방어. 정상 parquet 에는 중복이 없어 no-op 이며
    # 원본 객체를 그대로 돌려주므로 조회 경로 비용은 O(컬럼수) 비교뿐이다.
    raw, _ = dedupe_item_columns(raw)
    issues = validate_honeyform_df(raw)
    if issues:
        raise ValueError(kor_issues(issues))
    # 이미 저장된 오염 parquet('Bin' 등)도 조회 시점에 구제 — validate 통과가 무손실 치환을
    # 보장한다. 마이그레이션 스크립트 없이 지금 500 나는 세션이 살아난다(비용 O(컬럼수)).
    raw = canonicalize_meta_columns(raw)
    item_labels = list(raw.columns[len(META_COLUMNS):])
    meta_labels = list(raw.columns[:len(META_COLUMNS)])
    head = _object_with_none(raw.iloc[:DATA_START_ROW])
    tail = raw.iloc[DATA_START_ROW:]
    tail_meta = _object_with_none(tail[meta_labels])
    num_df = _numeric_item_block(tail, item_labels) if item_labels else None
    return head, tail_meta, num_df, item_labels


def _assemble_df(head, tail_meta, num_df) -> pd.DataFrame:
    if num_df is None:
        df = pd.concat([head, tail_meta], axis=0)
    else:
        df = pd.concat(
            [head, pd.concat([tail_meta, num_df.astype(object)], axis=1)], axis=0)
    df.index = pd.RangeIndex(len(df))
    return df


def decode_honeyform_parquet(data: bytes) -> pd.DataFrame:
    """Decode parquet bytes and restore item data rows to numeric values."""
    head, tail_meta, num_df, _ = _decode_parts(data)
    return _assemble_df(head, tail_meta, num_df)


def decode_split_honeyform_parquet(data: bytes, *, source: str,
                                   file_name: str = "",
                                   keep_df: bool = True) -> HoneyformTable:
    """decode + split 을 한 번에 수행 (loader 전용 빠른 경로).

    ``split_honeyform(decode_honeyform_parquet(data), ...)`` 과 결과가 동일하지만,
    item 블록 to_numeric 1회분과 재검증을 생략한다 (콜드 로드 시간 절반).

    keep_df=False 면 전체 object 프레임(df) 조립을 생략하고 df=None 을 담는다 —
    읽기 경로(tabs 계산·캐시)는 data/meta dict 만 쓰므로 값이 동일하고 메모리·시간만
    준다. df 가 필요한 편집/재인코딩 경로는 기본값(keep_df=True)으로 호출할 것.
    """
    head, tail_meta, num_df, item_labels = _decode_parts(data)
    df = _assemble_df(head, tail_meta, num_df) if keep_df else None
    item_cols = [str(c) for c in item_labels]
    if num_df is None:
        data_frame = tail_meta.reset_index(drop=True)
    else:
        data_frame = pd.concat([tail_meta.reset_index(drop=True),
                                num_df.reset_index(drop=True)], axis=1)
    # 메타 6행 dict 를 df.at 라벨 조회(항목수×6회) 대신 head 블록에서 일괄 추출
    meta_rows = head[item_labels].to_numpy(dtype=object)
    def _row(i):
        return dict(zip(item_cols, meta_rows[i]))
    return HoneyformTable(
        source=source,
        file_name=file_name or source,
        df=df,
        item_columns=item_cols,
        tseq=_row(0),
        tno=_row(1),
        step=_row(2),
        units=_row(3),
        hilim=_row(4),
        lolim=_row(5),
        data=data_frame,
    )


def read_honeyform_file(path) -> pd.DataFrame:
    """Read a 7-meta honeyform CSV/xlsx file without legacy df_honey normalization."""
    p = Path(path)
    if p.suffix.lower() == ".xlsx":
        df = pd.read_excel(p, dtype=object)
    else:
        df = pd.read_csv(p, dtype=object, keep_default_na=False)
    df, _ = dedupe_item_columns(df)
    issues = validate_honeyform_df(df)
    if issues:
        raise ValueError(kor_issues(issues))
    return df


def _fmt_dut(value) -> str:
    """DUT 셀 값을 라벨 문자열로 정규화 (float '1.0' → '1', 공백/None → '(blank)')."""
    if value is None:
        return "(blank)"
    try:
        if pd.isna(value):
            return "(blank)"
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s:
        return "(blank)"
    # '1.0' 처럼 소수부가 0 인 실수 표기는 정수로 다듬는다 (DUT/site 는 정수 코드).
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return s


def _dut_sort_key(label: str):
    """DUT 라벨 정렬 키 — 숫자 라벨은 수치 오름차순(1,2,…,10,11,12), 비숫자는 뒤로 문자순."""
    try:
        return (0, float(label))
    except (TypeError, ValueError):
        return (1, label)


def split_table_by_dut(table: "HoneyformTable") -> list["HoneyformTable"]:
    """단일 HoneyformTable 을 DUT 컬럼 값별로 분할 — 각 DUT 가 새 source 가 된다 (DUT 모드).

    meta(tno/step/units/hilim/lolim/item_columns)는 공유하고 data/df 만 필터한다.
    DUT 종류가 1개 이하면 분할이 무의미하므로 원본을 그대로 담아 반환한다.
    다운샘플 없음 — 모든 die 를 해당 DUT source 로 보존한다 (규칙 #6).
    """
    data = table.data
    labels = data["DUT"].map(_fmt_dut)
    # DUT legend/series 순서를 전 탭(yield/distribution/issue table/map)에서 수치
    # 오름차순으로 통일 (1,2,…,10,11,12). 비숫자('(blank)' 등)는 뒤로 문자순.
    uniq = sorted(dict.fromkeys(labels.tolist()), key=_dut_sort_key)
    if len(uniq) <= 1:
        return [table]

    # df=None(읽기 경로 슬림 테이블)이면 sub_df 도 None — DUT 분할 결과는 읽기 전용
    # 렌더에만 쓰이므로 data/meta 만으로 충분하다 (Phase 5).
    meta_rows = table.df.iloc[:DATA_START_ROW] if table.df is not None else None
    data_rows = (table.df.iloc[DATA_START_ROW:].reset_index(drop=True)
                 if table.df is not None else None)
    out: list[HoneyformTable] = []
    for label in uniq:
        mask = (labels == label).to_numpy()
        sub_data = data[mask].reset_index(drop=True)
        sub_df = (pd.concat([meta_rows, data_rows[mask]], ignore_index=True)
                  if table.df is not None else None)
        out.append(HoneyformTable(
            source=f"DUT {label}",
            file_name=f"{table.file_name} · DUT {label}",
            df=sub_df,
            item_columns=list(table.item_columns),
            tseq=dict(table.tseq),
            tno=dict(table.tno), step=dict(table.step), units=dict(table.units),
            hilim=dict(table.hilim), lolim=dict(table.lolim),
            data=sub_data,
        ))
    return out


def split_honeyform(df: pd.DataFrame, source: str, file_name: str = "") -> HoneyformTable:
    # 개명본을 이후 전 구간(item_cols·df.at 메타 조회·HoneyformTable.df)에 그대로 쓴다.
    df, _ = dedupe_item_columns(df)
    issues = validate_honeyform_df(df)
    if issues:
        raise ValueError(kor_issues(issues))
    # decode 산물은 이미 canonical 이지만 read_honeyform_file(ingest) 산물도 여기를 지나므로
    # 방어적으로 한 번 더 정규화한다 (비용 O(컬럼수)).
    df = canonicalize_meta_columns(df)
    item_cols = [str(c) for c in list(df.columns[len(META_COLUMNS):])]
    meta_labels = list(df.columns[:len(META_COLUMNS)])
    data = df.iloc[DATA_START_ROW:].reset_index(drop=True)
    if item_cols:
        # per-column to_numeric 의 dtype 동작을 유지하며 블록 단위로 변환 (decode 참조)
        data = pd.concat([data[meta_labels].copy(),
                          _numeric_item_block(data, item_cols)], axis=1)
    else:
        data = data.copy()
    return HoneyformTable(
        source=source,
        file_name=file_name or source,
        df=df,
        item_columns=item_cols,
        tseq={c: df.at[0, c] for c in item_cols},
        tno={c: df.at[1, c] for c in item_cols},
        step={c: df.at[2, c] for c in item_cols},
        units={c: df.at[3, c] for c in item_cols},
        hilim={c: df.at[4, c] for c in item_cols},
        lolim={c: df.at[5, c] for c in item_cols},
        data=data,
    )

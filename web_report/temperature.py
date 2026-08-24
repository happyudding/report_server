"""Temperature 모드 — RT/CT/HT pair rawdata 정리 + .lt/.pds limit 테이블 파서.

PMIC 는 같은 웨이퍼를 RT(상온)/CT(저온)/HT(고온) 로 나눠 측정한다. Temperature 모드는
그 pair 를 하나의 그룹으로 묶고, **그룹의 RT 를 기준(reference)** 으로 삼아 업로드 직전에
CT/HT rawdata 를 정리한다:

  1. RT 에서 BIN==1(pass) 인 (XPOS, YPOS) 좌표만 CT/HT 에 남긴다 — RT 에서 이미 죽은
     die 는 저온/고온 결과를 볼 의미가 없다.
     **좌표가 없는 rawdata 예외**(2026-08-24): 좌표가 비어 있으면 이 필터가 아무것도
     걸러내지 못한 채 조용히 통과한다(RT 에서 죽은 die 가 CT/HT 에 그대로 남는다).
     그래서 클라가 사용자에게 확인을 받아 ``serial_match=True`` 로 부르면, 좌표가 없는
     pair 만 **SERIAL 순서 i 번째끼리** 짝지어 같은 필터를 적용한다(개수가 다르면 적은
     쪽 기준). 확인 없이(기본값) 부르면 종전 좌표 매칭 그대로다.
  2. 남은 CT/HT 행을 **RT 의 HILIM/LOLIM** 으로 다시 판정한다 (CT/HT 자신의 limit 메타행은
     그대로 둔다 — 화면 표시용 원본 보존).
  3. 재판정으로 fail 이 된 행의 BIN 은 ① .lt/.pds 매핑(LSL/USL 위반 방향별) →
     ② RT 에서 같은 항목으로 죽은 bin → ③ 999(unknown) 순으로 정한다.

순수 모듈이다 — dist_pack/dist_blob 과 같이 Honey 클라이언트(werkzeug·flask 없음)에서도
import 한다. ``web_report.tabs`` 를 import 하면 TAB_REGISTRY 전체가 딸려오므로,
``tabs.common.fmt_type`` 과 ``tabs.yield_tab._tseq_sort_key`` 는 여기서 소형 미러
(``_fmt`` / ``_tseq_key``)로 재구현한다. **두 곳의 판정이 갈리면 안 되므로 원본을 고칠 때
여기도 같이 본다.**
"""
from __future__ import annotations

import csv
import logging
import re
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

from .honeyform import DATA_START_ROW, META_COLUMNS

_log = logging.getLogger(__name__)

UNKNOWN_BIN = "999"
PASS_BIN = "1"

# has_coords 가 먼저 훑어보는 행 수 (좌표 유무는 앞부분만 봐도 대개 판별된다).
_COORD_PROBE = 200

_COL_SERIAL = META_COLUMNS.index("SERIAL")
_COL_XPOS = META_COLUMNS.index("XPOS")
_COL_YPOS = META_COLUMNS.index("YPOS")
_COL_BIN = META_COLUMNS.index("BIN")
_COL_FAILTNO = META_COLUMNS.index("FAILTNO")

_ROW_TSEQ = 0
_ROW_TNO = 1
_ROW_HILIM = 4
_ROW_LOLIM = 5


# ── 값 정규화 (tabs.common / yield_tab 미러) ─────────────────────────────────
def _fmt(value) -> str:
    """tabs.common.fmt_type 미러 — 5.0 → "5", NaN/None → "" (좌표·BIN 비교 키)."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    try:
        f = float(value)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _tseq_key(tseq_value, item) -> tuple:
    """yield_tab._tseq_sort_key 미러 — TSEQ 숫자 우선, 비수치는 뒤로(이름순)."""
    try:
        return (0, float(tseq_value), str(item))
    except (TypeError, ValueError):
        return (1, 0.0, str(item))


def _num(value):
    """limit 값 → float. 공백/비수치는 None(무제한)."""
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


# ── .lt / .pds 파서 ──────────────────────────────────────────────────────────
# 110, "T002_VBAT_NOS", ",4", "0V", 19, [-0.9, -0.2, 20:19];
# 1200, "T001_1GDM_IIH", ".4", "uA", 11, [-9.5,9.5];
_LT_LINE = re.compile(
    r'^\s*([0-9]+)\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,'
    r'\s*([^,\[\]]*?)\s*,\s*\[([^\]]*)\]')


def parse_lt_text(text: str) -> dict:
    """.lt LimitTable 본문 → {item: {tno, lsl, usl, lsl_bin, usl_bin}}.

    행 형식은 ``TNO, "ITEM", "Scale", "UNIT", Bin, [LSL, USL];`` 이며 bin 결정이 2가지다:
      - 일반: 대괄호 **앞** 5번째 필드가 bin (LSL/USL 위반 모두 같은 bin).
      - 특수: 대괄호 안 3번째 원소 ``20:19`` 가 있으면 LSL 위반=20 / USL 위반=19 로 덮어쓴다.
    ``#`` 주석·중괄호 등 나머지 줄은 정규식에 걸리지 않아 자연히 무시된다.
    """
    out: dict = {}
    for line in (text or "").splitlines():
        m = _LT_LINE.match(line)
        if not m:
            continue
        tno, item, _scale, _unit, bin_field, bracket = m.groups()
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in bracket.split(",")]
        lsl_bin = usl_bin = _fmt(bin_field)
        if len(parts) >= 3 and ":" in parts[2]:
            lo_bin, _, hi_bin = parts[2].partition(":")
            lsl_bin, usl_bin = _fmt(lo_bin), _fmt(hi_bin)
        out[item] = {
            "tno": _fmt(tno),
            "lsl": _num(parts[0] if parts else None),
            "usl": _num(parts[1] if len(parts) > 1 else None),
            "lsl_bin": lsl_bin or None,
            "usl_bin": usl_bin or None,
        }
    return out


_PDS_SECTION = re.compile(r"^\s*\[(.+)\]\s*$")
_PDS_VAR_MAP = "datasheet variable map"


def parse_pds_text(text: str) -> dict:
    """.pds 본문 → {item: {tno, lsl, usl, lsl_bin, usl_bin}}.

    ``[Datasheet Variable Map]`` 섹션 **다음에 나오는 대괄호부터** Test Item 섹션이다.
    행 형식: ``BLANK, TNO1, TNO2, "ITEM", LSL, USL, SCALE, "UNIT", "LSL_BIN", "USL_BIN"``.
    Variable Map 이 없는 파일은 모든 섹션을 Test Item 으로 본다(구제 폴백).
    """
    lines = (text or "").splitlines()
    started = not any(_PDS_VAR_MAP in ln.lower() for ln in lines)
    seen_var_map = started
    out: dict = {}
    for line in lines:
        section = _PDS_SECTION.match(line)
        if section:
            if not seen_var_map:
                seen_var_map = _PDS_VAR_MAP in section.group(1).strip().lower()
            elif not started:
                started = True          # Variable Map 다음 첫 대괄호 = 첫 Test Item
            continue
        if not started or not line.strip():
            continue
        try:
            fields = next(csv.reader([line]))
        except Exception:
            continue
        if len(fields) < 10:
            continue
        item = fields[3].strip()
        if not item:
            continue
        out[item] = {
            "tno": _fmt(fields[1]),
            "lsl": _num(fields[4]),
            "usl": _num(fields[5]),
            "lsl_bin": _fmt(fields[8]) or None,
            "usl_bin": _fmt(fields[9]) or None,
        }
    return out


def load_limits_file(path) -> tuple[dict, str]:
    """.lt/.pds 파일 경로 → (매핑, 종류("lt"|"pds")). 확장자로 파서를 고른다."""
    from pathlib import Path

    p = Path(path)
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    kind = "pds" if p.suffix.lower() == ".pds" else "lt"
    mapping = parse_pds_text(text) if kind == "pds" else parse_lt_text(text)
    return mapping, kind


def _match_key(name: str) -> str:
    """항목명 비교 키 — 대문자화 + 선행 TNO 접두(``T001_``) 제거."""
    key = str(name).strip().upper()
    return re.sub(r"^T\d+_", "", key)


def bin_lookup(mapping: dict | None) -> dict:
    """파서 매핑 → 항목명 조회 인덱스. 정확 일치와 TNO 접두 제거 일치를 모두 담는다."""
    index: dict = {}
    for name, entry in (mapping or {}).items():
        index.setdefault(str(name).strip().upper(), entry)
        index.setdefault(_match_key(name), entry)
    return index


def match_item(column: str, index: dict):
    """honeyform item 컬럼명 → limit 테이블 엔트리. 못 찾으면 None."""
    if not index:
        return None
    key = str(column).strip().upper()
    return index.get(key) or index.get(_match_key(column))


# ── honeyform 프레임 헬퍼 ────────────────────────────────────────────────────
def _items(df: pd.DataFrame) -> list:
    return [str(c) for c in df.columns[len(META_COLUMNS):]]


def _meta_row(df: pd.DataFrame, row: int) -> dict:
    """메타 행(TSEQ/TNO/HILIM/LOLIM) → {item: 값}."""
    items = _items(df)
    values = df.iloc[row, len(META_COLUMNS):].tolist()
    return dict(zip(items, values))


def _data(df: pd.DataFrame) -> pd.DataFrame:
    return df.iloc[DATA_START_ROW:]


def _col(data: pd.DataFrame, idx: int) -> list:
    return [_fmt(v) for v in data.iloc[:, idx].tolist()]


def rt_pass_coords(rt_df: pd.DataFrame) -> set:
    """RT 에서 BIN==1 인 (XPOS, YPOS) 좌표 집합 — CT/HT 행 필터의 기준."""
    data = _data(rt_df)
    xs, ys, bins = _col(data, _COL_XPOS), _col(data, _COL_YPOS), _col(data, _COL_BIN)
    return {(x, y) for x, y, b in zip(xs, ys, bins) if b == PASS_BIN}


def data_row_count(df: pd.DataFrame) -> int:
    """데이터 행 수(메타 6행 제외) — 클라의 '개수 불일치' 안내가 쓰는 기준."""
    return int(len(_data(df)))


def _coord_blank(series: pd.Series) -> pd.Series:
    """좌표 값이 비었는가(NaN 또는 공백 문자열) — ``_fmt`` 가 "" 를 내는 값과 같은 기준."""
    return series.isna() | (series.astype(str).str.strip() == "")


def _coord_filled(data: pd.DataFrame) -> pd.Series:
    """(XPOS, YPOS) 가 **둘 다** 채워진 행 마스크."""
    return (~_coord_blank(data.iloc[:, _COL_XPOS])
            & ~_coord_blank(data.iloc[:, _COL_YPOS]))


def has_coords(df: pd.DataFrame) -> bool:
    """(XPOS, YPOS) 가 **둘 다 채워진** 데이터 행이 하나라도 있으면 True.

    "좌표가 없는 rawdata" 판정의 단일 기준이다. 값이 숫자인지는 보지 않는다 — 좌표
    필터(``rt_pass_coords``/``_clean_member``)가 ``_fmt`` 문자열로 비교하므로, 비어 있지
    않은 값은 (숫자가 아니어도) 좌표로 짝이 맞는다. 데이터 행이 없는 프레임은 짝지을
    좌표도 없으므로 False 다.

    앞 ``_COORD_PROBE`` 행을 먼저 본다 — 좌표가 있는 rawdata(대다수)는 거기서 즉시
    끝나므로, 클라가 **UI 스레드에서 전 소스를 검사**해도(소스 수 × 행수) 창이 멎지
    않는다. 전량 스캔은 앞부분에 좌표가 없는 프레임에서만 돈다.
    """
    data = _data(df)
    if data.empty:
        return False
    if bool(_coord_filled(data.head(_COORD_PROBE)).any()):
        return True
    return bool(_coord_filled(data).any())


def sources_without_coords(frames: dict) -> list:
    """좌표가 없는 source 이름 목록 (입력 dict 순서) — 클라 확인창의 파일 목록."""
    return [name for name, df in (frames or {}).items() if not has_coords(df)]


def _serial_positions(data: pd.DataFrame) -> list:
    """SERIAL 오름차순 행 위치 — 수치 우선, 비수치는 뒤로(문자순). 안정 정렬."""
    serials = _col(data, _COL_SERIAL)

    def key(i):
        try:
            return (0, float(serials[i]), "")
        except (TypeError, ValueError):
            return (1, 0.0, serials[i])

    return sorted(range(len(serials)), key=key)


def _serial_keep(rt_df: pd.DataFrame, member_df: pd.DataFrame) -> tuple:
    """좌표 없는 pair 의 keep 마스크 — SERIAL 순서 i 번째끼리 짝짓는다.

    반환 ``(keep 불린 배열, rt 행수, member 행수, 짝지은 행수)``. 개수가 다르면 **적은
    쪽 기준**으로 앞에서부터만 짝짓고 남는 행은 버린다(사용자 확정 — 클라가 그 전에
    "가장 적은 raw data 기준으로 진행합니다" 안내를 띄운다). 짝의 RT 행이 BIN==1 일
    때만 남기므로 좌표 매칭과 **같은 규칙**(RT pass die 만)이다.
    """
    rt_data, m_data = _data(rt_df), _data(member_df)
    rt_pos, m_pos = _serial_positions(rt_data), _serial_positions(m_data)
    paired = min(len(rt_pos), len(m_pos))
    rt_bins = _col(rt_data, _COL_BIN)
    keep = np.zeros(len(m_pos), dtype=bool)
    for i in range(paired):
        if rt_bins[rt_pos[i]] == PASS_BIN:
            keep[m_pos[i]] = True
    return keep, len(rt_pos), len(m_pos), paired


def rt_limits(rt_df: pd.DataFrame) -> dict:
    """RT 의 {item: (lolim, hilim)} — 값이 없는 쪽은 None(그 방향 무제한)."""
    hi, lo = _meta_row(rt_df, _ROW_HILIM), _meta_row(rt_df, _ROW_LOLIM)
    return {item: (_num(lo.get(item)), _num(hi.get(item))) for item in _items(rt_df)}


def _tno_to_item(df: pd.DataFrame) -> dict:
    """정규화 TNO → item 1개 (TSEQ 가 가장 앞선 항목 — yield_tab.tno_to_item_map 규칙)."""
    tno, tseq = _meta_row(df, _ROW_TNO), _meta_row(df, _ROW_TSEQ)
    grouped = defaultdict(list)
    for item, value in tno.items():
        key = _fmt(value)
        if key and key != "0":
            grouped[key].append(item)
    return {key: min(items, key=lambda it: _tseq_key(tseq.get(it), it))
            for key, items in grouped.items()}


def rt_fail_bin_map(rt_df: pd.DataFrame) -> dict:
    """RT 에서 실제로 관측된 fail bin — {(item, "lo"|"hi"): bin, (item, ""): bin}.

    .lt/.pds 매핑이 없을 때의 폴백("RT 에서 죽은 bin"). RT fail 행의 FAILTNO 를 항목으로
    되돌린 뒤(그 소스 자신의 TNO 메타 기준), 그 행의 측정값이 LOLIM/HILIM 중 어느 쪽을
    위반했는지로 방향까지 나눠 **최빈 bin** 을 남긴다. 방향 판정이 불가한 행(값 결측 등)은
    방향 없는 키("")로만 집계한다.
    """
    data = _data(rt_df)
    if data.empty:
        return {}
    bins, failtnos = _col(data, _COL_BIN), _col(data, _COL_FAILTNO)
    tno_to_item = _tno_to_item(rt_df)
    limits = rt_limits(rt_df)

    rows_by_item = defaultdict(list)
    for i, (b, tno) in enumerate(zip(bins, failtnos)):
        if b == PASS_BIN or not b or not tno:
            continue
        item = tno_to_item.get(tno)
        if item is not None:
            rows_by_item[item].append((i, b))

    counters: dict = defaultdict(Counter)
    for item, rows in rows_by_item.items():
        lo, hi = limits.get(item, (None, None))
        values = pd.to_numeric(data[item], errors="coerce").to_numpy() \
            if item in data.columns else None
        for i, b in rows:
            counters[(item, "")][b] += 1
            if values is None:
                continue
            v = values[i]
            if lo is not None and v < lo:
                counters[(item, "lo")][b] += 1
            elif hi is not None and v > hi:
                counters[(item, "hi")][b] += 1
    return {key: counter.most_common(1)[0][0] for key, counter in counters.items()}


def _resolve_bins(items, index, fail_bins) -> tuple:
    """항목별 (LSL 위반 bin, USL 위반 bin) 배열 — 매핑 → RT 관측 → 999 순."""
    lsl, usl = [], []
    unknown = []
    for item in items:
        entry = match_item(item, index)
        lo_bin = (entry or {}).get("lsl_bin") or fail_bins.get((item, "lo"))
        hi_bin = (entry or {}).get("usl_bin") or fail_bins.get((item, "hi"))
        generic = fail_bins.get((item, ""))
        lo_bin = lo_bin or generic or UNKNOWN_BIN
        hi_bin = hi_bin or generic or UNKNOWN_BIN
        if UNKNOWN_BIN in (lo_bin, hi_bin):
            unknown.append(item)
        lsl.append(str(lo_bin))
        usl.append(str(hi_bin))
    return np.array(lsl, dtype=object), np.array(usl, dtype=object), unknown


def _clean_member(member_df, rt_df, coords, limits, index, fail_bins,
                  serial=False) -> tuple:
    """CT/HT 소스 1개 정리 → (새 프레임, 통계 dict).

    ``serial=True`` 면 좌표 대신 SERIAL 순서로 RT 와 짝짓는다(좌표 없는 rawdata 전용 —
    호출부가 사용자 확인을 받은 뒤에만 켠다). 재판정 이후는 두 경로가 완전히 같다.
    """
    head = member_df.iloc[:DATA_START_ROW]
    data = _data(member_df)
    before = len(data)

    serial_info = None
    if serial:
        keep, rt_rows, member_rows, paired = _serial_keep(rt_df, member_df)
        serial_info = {"rt_rows": rt_rows, "member_rows": member_rows, "paired": paired}
    else:
        xs, ys = _col(data, _COL_XPOS), _col(data, _COL_YPOS)
        keep = np.array([(x, y) in coords for x, y in zip(xs, ys)], dtype=bool)
    data = data.loc[keep]
    dropped = before - len(data)

    # 판정 대상 = RT 와 member 양쪽에 있고 RT limit 이 하나라도 있는 항목, RT TSEQ 순.
    rt_tseq = _meta_row(rt_df, _ROW_TSEQ)
    member_items = set(_items(member_df))
    items = [it for it in _items(rt_df)
             if it in member_items and limits.get(it, (None, None)) != (None, None)]
    items.sort(key=lambda it: _tseq_key(rt_tseq.get(it), it))

    n = len(data)
    first = np.full(n, -1, dtype=np.int64)
    dir_lo = np.zeros(n, dtype=bool)
    if n and items:
        # 항목을 하나씩 훑어 '첫 fail' 만 기록한다 — 전 항목 행렬을 한 번에 만들면
        # (행수 × 항목수) float 이 메모리를 그대로 잡아먹는다.
        for j, item in enumerate(items):
            lo, hi = limits[item]
            values = pd.to_numeric(data[item], errors="coerce").to_numpy(dtype=float)
            fail_lo = values < lo if lo is not None else np.zeros(n, dtype=bool)
            fail_hi = values > hi if hi is not None else np.zeros(n, dtype=bool)
            fresh = (fail_lo | fail_hi) & (first < 0)
            if not fresh.any():
                continue
            first[fresh] = j
            dir_lo[fresh] = fail_lo[fresh]

    lsl_bins, usl_bins, unknown_items = _resolve_bins(items, index, fail_bins)
    member_tno, rt_tno = _meta_row(member_df, _ROW_TNO), _meta_row(rt_df, _ROW_TNO)
    tnos = np.array([_fmt(member_tno.get(it)) or _fmt(rt_tno.get(it)) for it in items],
                    dtype=object)

    bin_out = np.full(n, PASS_BIN, dtype=object)
    tno_out = np.full(n, "", dtype=object)
    failed = first >= 0
    if failed.any():
        idx = first[failed]
        bin_out[failed] = np.where(dir_lo[failed], lsl_bins[idx], usl_bins[idx])
        tno_out[failed] = tnos[idx]

    out = data.astype(object).copy()
    out.iloc[:, _COL_BIN] = bin_out
    out.iloc[:, _COL_FAILTNO] = tno_out
    frame = pd.concat([head.astype(object), out], axis=0)
    frame.index = pd.RangeIndex(len(frame))

    n_fail = int(failed.sum())
    stats = {
        "dropped": dropped,
        "kept": n,
        "fail": n_fail,
        "pass": n - n_fail,
        "unknown_bin_items": sorted(set(unknown_items)),
    }
    if serial_info is not None:
        stats["serial"] = True
        stats.update(serial_info)
    return frame, stats


def clean_group(frames: dict, rt: str, members, bin_map=None,
                serial_match=False) -> tuple:
    """RT/CT/HT 그룹 1개의 rawdata 정리.

    frames: {source 이름: 7-meta honeyform DataFrame} (RT + members 를 포함해야 한다)
    rt:     기준 source 이름 — **반환 프레임은 원본 그대로**(정리 대상 아님)
    members: 정리할 CT/HT source 이름 목록 (비어 있으면 RT 단독 그룹 — no-op)
    bin_map: parse_lt_text/parse_pds_text 결과(없으면 RT 관측 bin → 999 폴백만)
    serial_match: 좌표가 없는 pair 를 SERIAL 순서로 짝지어도 되는가 (사용자 확인 결과).
        판정은 **pair 단위**다 — RT 나 그 member 중 한쪽이라도 좌표가 없으면 그 pair 만
        SERIAL 매칭이고, 양쪽에 좌표가 있는 pair 는 종전 좌표 매칭 그대로다.

    반환 ({이름: 프레임}, {이름: 통계}). 입력 프레임은 변형하지 않는다.
    """
    rt_df = frames[rt]
    coords = rt_pass_coords(rt_df)
    limits = rt_limits(rt_df)
    fail_bins = rt_fail_bin_map(rt_df)
    index = bin_lookup(bin_map)
    rt_missing = serial_match and not has_coords(rt_df)

    out = {rt: rt_df}
    stats: dict = {}
    for name in members or []:
        serial = serial_match and (rt_missing or not has_coords(frames[name]))
        frame, info = _clean_member(frames[name], rt_df, coords, limits, index, fail_bins,
                                    serial=serial)
        out[name] = frame
        stats[name] = info
    return out, stats


def clean_frames(frames: dict, groups, bin_map=None, serial_match=False) -> tuple:
    """그룹 목록 전체에 clean_group 적용 → (정리된 frames, {source: 통계}).

    groups: [{"rt": 이름, "members": [이름, ...]}, ...] (manifest.options.temperature 형식)
    그룹에 속하지 않은 source 는 원본 그대로 통과시킨다.
    serial_match: 좌표 없는 pair 의 SERIAL 순서 매칭 허용 (clean_group 참조 — 기본값
        False 는 종전 동작과 완전히 동일하다).
    """
    out = dict(frames)
    stats: dict = {}
    for group in groups or []:
        rt = str(group.get("rt") or "")
        members = [str(m) for m in (group.get("members") or []) if m in frames]
        if rt not in frames:
            _log.warning("temperature: RT source not found, group skipped: %r", rt)
            continue
        cleaned, info = clean_group(out, rt, members, bin_map, serial_match)
        out.update(cleaned)
        stats.update(info)
    return out, stats


def format_stats(stats: dict) -> list:
    """정리 통계 → 사용자용 한국어 로그 줄 목록 (Honey 실행 로그에 출력)."""
    lines = []
    for source, info in stats.items():
        if info.get("serial"):
            basis = (f"좌표 없음 → SERIAL 순서 매칭(RT {info['rt_rows']}행 ↔ "
                     f"{info['member_rows']}행 중 {info['paired']}행 짝지음), "
                     f"RT pass 필터 {info['dropped']}행 제외")
        else:
            basis = f"RT pass 좌표 필터 {info['dropped']}행 제외"
        line = (f"{source}: {basis} → {info['kept']}행, "
                f"재판정 fail {info['fail']} / pass {info['pass']}")
        if info["unknown_bin_items"]:
            n = len(info["unknown_bin_items"])
            sample = ", ".join(info["unknown_bin_items"][:5])
            line += f" · bin 미매칭 항목 {n}건(999 처리): {sample}"
        lines.append(line)
    return lines

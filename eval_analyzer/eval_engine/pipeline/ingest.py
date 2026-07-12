"""L0 Ingest — run_input(메모리 raw) → 캐노니컬 fail_case 들.

입력: docs/INTEGRATION_CONTRACT §3 (meta + raw_df[신규 df 포맷] / raw_table[레거시] / degrade items).
할 일:
  1. product_master / item_master / item_spec upsert (마스터).
  2. item 명 파싱: item_canonical(정규화) / item_base / item_phase, item_alias 해소.
  3. category_major(TRIM 포함 여부) / value_type(units→V|A|Hz|CODE|P_F|Ohm|Sec) 분류.
  4. fail item 추출: bin != PASS_BIN 또는 limit 위반(CODE_TO_PORT §4)인 (item, bin) 조합.
  5. case_id = store.make_case_id(...), item_class = f"{category_major}|{value_type}|{bin}".
  6. ingest_run 생성(run_id, meta 의 temperature/corner 포함), run_case 링크, fail_case upsert.
  7. 각 case 에 per-DUT 측정 시리즈(values/x/y/site)를 메모리로 첨부(저장 안 함) → L1/L2 가 사용.
반환: {"run_id": int|None, "cases": [case_ctx, ...]}

주의: persist=False(preview) 모드는 DB 미접근 — item_id 를 canonical 해시로 대체한다.
  이 모드의 case_id 는 persist=True 재실행 시 달라질 수 있다(preview 전용).
"""
import hashlib
import logging
import math
import re

from .. import store
from ._rules import load_yaml
from .. import config

logger = logging.getLogger(__name__)

PASS_BIN = 1

UNIT_TO_VALUE_TYPE = {
    "v": "V", "volt": "V", "volts": "V",
    "a": "A", "amp": "A", "amps": "A", "ma": "A", "ua": "A",
    "hz": "Hz", "khz": "Hz", "mhz": "Hz",
    "code": "CODE",
    "ohm": "Ohm", "ohms": "Ohm",
    "s": "Sec", "sec": "Sec", "secs": "Sec", "ms": "Sec", "us": "Sec", "ns": "Sec",
    "p_f": "P_F", "pass/fail": "P_F", "p/f": "P_F", "": "P_F",
}
PHASE_TOKENS = {"init", "code", "trim", "p2", "p1", "final"}

# 정본 raw_df 레이아웃(REPORT_GENERATOR_DATA_REQUEST) — 파서가 위치/이름으로 고정 접근.
_META_COLS = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO"]
_META_ROW_LABELS = ["TSEQ", "TNO", "STEP", "UNIT", "HILIM", "LOLIM"]


def _norm(x):
    return str(x).strip().lstrip("﻿").upper()


def _validate_raw_df(df) -> None:
    """정본 raw_df 레이아웃 선검증 — 계약 위반 시 명확한 ValueError(파서 위치/이름 접근 전에 차단).

    _ingest_raw_df 가 cols[7:]=item, iloc[1/3/4/5]=메타행, data[XPOS/YPOS/BIN/FAILTNO] 로
    고정 접근하므로, 어긋나면 조용한 0케이스/opaque 에러 대신 여기서 원인을 legible 하게 잡는다.
    비교는 정규화(strip+BOM제거+대문자)로 BOM/공백/대소문자에 견고.
    """
    n_rows, n_cols = df.shape
    if n_cols < len(_META_COLS) + 1:
        raise ValueError(f"raw_df 컬럼 {n_cols}개 - meta 7 + item 1개 이상 = 최소 8개 필요")
    if n_rows < len(_META_ROW_LABELS):
        raise ValueError(
            f"raw_df 행 {n_rows}개 - 메타행 6개({'/'.join(_META_ROW_LABELS)}) 필요")
    got_cols = [_norm(c) for c in list(df.columns[:7])]
    if got_cols != _META_COLS:
        raise ValueError(f"raw_df 앞 7 meta 컬럼 불일치 - 기대 {_META_COLS}, 실제 {got_cols}")
    got_labels = [_norm(df.iloc[i, 0]) for i in range(len(_META_ROW_LABELS))]
    if got_labels != _META_ROW_LABELS:
        raise ValueError(
            f"raw_df 메타행 순서 불일치 - 기대 {_META_ROW_LABELS}, 실제 {got_labels}")
    item_cols = list(df.columns[7:])
    dups = sorted({c for c in item_cols if item_cols.count(c) > 1})
    if dups:
        raise ValueError(f"raw_df item 컬럼 중복: {dups}")


def _validate_product_meta(meta: dict) -> None:
    """product_type ↔ family_product 조합을 product_taxonomy.yaml 로 강제 검증.

    드롭다운 1:1 매칭 전제 — 허용표에 없는 조합이면 ValueError.
    """
    tax = load_yaml(str(config.PRODUCT_TAXONOMY_FILE))
    product_type = meta.get("product_type")
    family_product = meta.get("family_product")
    allowed_types = tax.get("product_types") or []
    if product_type not in allowed_types:
        raise ValueError(
            f"product_type '{product_type}' 은 허용값 {allowed_types} 에 없음")
    allowed_families = (tax.get("family_product") or {}).get(product_type) or []
    if family_product not in allowed_families:
        raise ValueError(
            f"family_product '{family_product}' 은 product_type '{product_type}' 의 "
            f"허용값 {allowed_families} 에 없음")


def _alias_map():
    try:
        doc = load_yaml(str(config.ITEM_ALIAS_FILE))
        return {k.strip(): v for k, v in (doc.get("aliases") or {}).items()}
    except FileNotFoundError:
        return {}


def _canonicalize(raw_name: str) -> str:
    return re.sub(r"\s+", "_", raw_name.strip().lower())


def _classify_value_type(unit, item_name) -> str:
    if unit:
        vt = UNIT_TO_VALUE_TYPE.get(str(unit).strip().lower())
        if vt:
            return vt
    if "CODE" in item_name.upper():
        return "CODE"
    return "P_F"


def _classify_category_major(item_name: str) -> str:
    return "TRIM" if "TRIM" in item_name.upper() else "NON_TRIM"


def _parse_base_phase(item_canonical: str):
    parts = item_canonical.split("_")
    phase = next((p for p in parts if p in PHASE_TOKENS), None)
    base = "_".join(p for p in parts if p != phase) if phase else item_canonical
    return base, phase


def _is_num(x):
    return isinstance(x, (int, float)) and not (isinstance(x, float) and x != x)  # NaN 제외


def _num_or_none(v):
    """숫자면 float, NaN/변환불가면 None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _bin_or_none(v):
    n = _num_or_none(v)
    return int(n) if n is not None else None


def _tno_norm(v):
    """TNO/FAILTNO 비교용 정규화. 숫자면 int, 문자면 strip, 공란/NaN 이면 None(=무fail)."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        s = str(v).strip()
        return s or None


def _case_dict(meta, case_id, item_id, item_canonical, cat, value_type, bin_,
               revision, lsl, usl, values, fail_mask, x_pos, y_pos, site, skewness=None,
               item_raw=None):
    """fail_case context dict (raw_table/raw_df 경로 공유 — 스키마 단일 소스)."""
    return {
        "case_id": case_id, "item_id": item_id, "item_canonical": item_canonical,
        "item_raw": item_raw,
        "category_major": cat, "value_type": value_type, "bin": bin_,
        "revision": revision, "item_class": f"{cat}|{value_type}|{bin_}",
        "product_type": meta.get("product_type"),
        "family_product": meta.get("family_product"),
        "lsl": lsl, "usl": usl, "skewness": skewness,
        "values": values, "fail_mask": fail_mask,
        "x_pos": x_pos, "y_pos": y_pos, "site": site,
    }


def _resolve_item_identity(raw_name, value_type, persist, conn, alias):
    item_canonical = alias.get(raw_name.strip(), _canonicalize(raw_name))
    base, phase = _parse_base_phase(item_canonical)
    cat = _classify_category_major(raw_name)
    if persist:
        item_id = store.resolve_item_id(raw_name, conn=conn)
        if item_id is None:
            item_id = store.upsert_item_master(item_canonical, raw_name, base, phase,
                                               cat, None, value_type, None, conn=conn)
            store.upsert_item_alias(raw_name, item_id, conn=conn)
    else:
        item_id = int(hashlib.sha1(item_canonical.encode()).hexdigest()[:8], 16)
    return item_id, item_canonical, cat


def _ingest_raw_table(meta, raw_table, persist, conn, alias):
    revision = meta.get("revision")
    item_cols = raw_table["item_columns"]
    units = raw_table.get("units", {})
    lowers = raw_table.get("lower_limit", {})
    uppers = raw_table.get("upper_limit", {})
    rows = raw_table["rows"]
    has_site = "Site" in (raw_table.get("meta_columns") or [])

    cases = []
    for item in item_cols:
        unit = units.get(item)
        value_type = _classify_value_type(unit, item)
        lsl, usl = lowers.get(item), uppers.get(item)

        values, x_pos, y_pos, site, bins = [], [], [], [], []
        for r in rows:
            v = r.get(item)
            if not _is_num(v):
                continue
            values.append(float(v))
            x_pos.append(r.get("XCoord"))
            y_pos.append(r.get("YCoord"))
            site.append(r.get("Site") if has_site else None)
            bins.append(r.get("Bin"))

        if not values:
            continue

        # fail bin 집합: 이 item 자체가 한계 위반(lo|hi) AND DUT 가 non-pass bin 인 (item,bin)만
        # (report_server build_issue_table 의 (lo|hi|break) & non_pass 와 동일 의미 — Yield 기준 Issue)
        fail_bins = set()
        for v, b in zip(values, bins):
            lo = (lsl is not None) and (v < lsl)
            hi = (usl is not None) and (v > usl)
            if (lo or hi) and (b is not None and b != PASS_BIN):
                fail_bins.add(int(b))
        if not fail_bins:
            continue

        item_id, item_canonical, cat = _resolve_item_identity(
            item, value_type, persist, conn, alias)
        if persist and revision is not None and (lsl is not None or usl is not None):
            store.upsert_item_spec(item_id, meta.get("product_name"), revision,
                                   lsl, usl, conn=conn)

        for bin_ in sorted(fail_bins):
            fail_mask = [b == bin_ for b in bins]
            case_id = store.make_case_id(meta.get("product_name"), meta.get("lot_id"),
                                         meta.get("wafer_number"), item_id, bin_, revision)
            cases.append(_case_dict(meta, case_id, item_id, item_canonical, cat,
                                    value_type, bin_, revision, lsl, usl,
                                    values, fail_mask, x_pos, y_pos, site,
                                    item_raw=item))
    return cases


def _ingest_raw_df(meta, df, persist, conn, alias):
    """신규 raw df 포맷(REPORT_GENERATOR_DATA_REQUEST) → fail_case 들. 컬럼 단위 처리.

    레이아웃: columns[:7]=meta(SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO), [7:]=item.
      row0=TSEQ(미사용) row1=TNO row2=STEP(P1/P2/P3, 미사용) row3=UNIT row4=HILIM(USL)
      row5=LOLIM(LSL) row6+=측정.
    fail 식별: FAILTNO(serial이 fail한 test의 TNO) == 그 item의 TNO → fail item, 그 serial BIN=fail bin.
    per-DUT dict 미생성 — 컬럼을 병렬 배열로 직접 읽는다.
    """
    _validate_raw_df(df)
    revision = meta.get("revision")
    cols = list(df.columns)
    item_cols = cols[7:]
    tno_row, unit_row = df.iloc[1], df.iloc[3]   # row2=STEP(미사용) skip
    hilim_row, lolim_row = df.iloc[4], df.iloc[5]
    data = df.iloc[6:]

    x_all = [_num_or_none(v) for v in data["XPOS"]]
    y_all = [_num_or_none(v) for v in data["YPOS"]]
    bin_all = [_bin_or_none(v) for v in data["BIN"]]
    failtno_all = [_tno_norm(v) for v in data["FAILTNO"]]

    cases = []
    for item in item_cols:
        value_type = _classify_value_type(unit_row[item], item)
        lsl, usl = _num_or_none(lolim_row[item]), _num_or_none(hilim_row[item])
        tno_i = _tno_norm(tno_row[item])

        values, x_pos, y_pos, bins, failtnos = [], [], [], [], []
        for v, x, y, b, ft in zip(data[item], x_all, y_all, bin_all, failtno_all):
            if not _is_num(v):
                continue
            values.append(float(v))
            x_pos.append(x); y_pos.append(y); bins.append(b); failtnos.append(ft)
        if not values:
            continue

        # fail bin: FAILTNO == 이 item 의 TNO 인 serial 의 BIN
        fail_bins = set()
        if tno_i is not None:
            for b, ft in zip(bins, failtnos):
                if ft == tno_i and b is not None:
                    fail_bins.add(b)

        item_id, item_canonical, cat = _resolve_item_identity(
            item, value_type, persist, conn, alias)
        if persist and revision is not None and (lsl is not None or usl is not None):
            store.upsert_item_spec(item_id, meta.get("product_name"), revision,
                                   lsl, usl, conn=conn)

        site = [None] * len(values)
        # fail bin 별 case; fail 없으면 PASS_BIN candidate 1개(저장 판단은 rule 계산 후 present.should_store)
        for bin_ in (sorted(fail_bins) if fail_bins else [PASS_BIN]):
            if fail_bins:
                fail_mask = [(ft == tno_i and b == bin_) for b, ft in zip(bins, failtnos)]
            else:
                fail_mask = [False] * len(values)
            case_id = store.make_case_id(meta.get("product_name"), meta.get("lot_id"),
                                         meta.get("wafer_number"), item_id, bin_, revision)
            cases.append(_case_dict(meta, case_id, item_id, item_canonical, cat,
                                    value_type, bin_, revision, lsl, usl,
                                    values, fail_mask, x_pos, y_pos, site,
                                    item_raw=item))
    if item_cols and len(data) > 0 and not cases:
        logger.warning("raw_df: item %d개, 데이터 %d행이나 case 0 - item 셀 dtype 확인"
                       "(문자열이면 파서가 무시, docs/EVALUATE_RETURN_SPEC 6절)",
                       len(item_cols), len(data))
    return cases


def _ingest_degrade(meta, items, persist, conn, alias):
    revision = meta.get("revision")
    cases = []
    for it in items:
        raw_name = it["item_name"]
        value_type = _classify_value_type(it.get("unit"), raw_name)
        bin_ = int(it["bin"])
        lsl, usl = it.get("lsl"), it.get("usl")
        item_id, item_canonical, cat = _resolve_item_identity(
            raw_name, value_type, persist, conn, alias)
        if persist and revision is not None and (lsl is not None or usl is not None):
            store.upsert_item_spec(item_id, meta.get("product_name"), revision,
                                   lsl, usl, conn=conn)
        case_id = store.make_case_id(meta.get("product_name"), meta.get("lot_id"),
                                     meta.get("wafer_number"), item_id, bin_, revision)
        cases.append({
            "case_id": case_id, "item_id": item_id, "item_canonical": item_canonical,
            "item_raw": raw_name,
            "category_major": cat, "value_type": value_type, "bin": bin_,
            "revision": revision, "item_class": f"{cat}|{value_type}|{bin_}",
            "product_type": meta.get("product_type"),
            "family_product": meta.get("family_product"),
            "lsl": lsl, "usl": usl, "skewness": it.get("skewness"),
            "values": [], "fail_mask": [], "x_pos": [], "y_pos": [], "site": [],
            "yield": it.get("yield"), "fail_count": it.get("fail_count"),
            "total_count": it.get("total_count"),
        })
    return cases


def _build_cases(meta, run_input, persist, conn):
    alias = _alias_map()
    raw_df = run_input.get("raw_df")
    raw_table = run_input.get("raw_table")
    if raw_df is not None:              # 신규 df 포맷 (DataFrame — 진리값 모호 → is not None)
        cases = _ingest_raw_df(meta, raw_df, persist, conn, alias)
    elif raw_table:
        missing = [k for k in ("item_columns", "rows") if k not in raw_table]
        if missing:
            raise ValueError(f"raw_table 필수 키 누락: {missing}")
        cases = _ingest_raw_table(meta, raw_table, persist, conn, alias)
    elif "items" in run_input:
        cases = _ingest_degrade(meta, run_input["items"], persist, conn, alias)
    else:
        raise ValueError("run_input 에 raw_df / raw_table / items 중 하나가 필요")
    for case in cases:
        case["product_name"] = meta.get("product_name")
        case["lot_id"] = meta.get("lot_id")
        case["wafer_number"] = meta.get("wafer_number")
    return cases


def ingest(run_input: dict, *, persist: bool = True) -> dict:
    meta = run_input["meta"]
    _validate_product_meta(meta)
    if not persist:
        cases = _build_cases(meta, run_input, persist=False, conn=None)
        return {"run_id": None, "cases": cases}

    with store.get_conn() as conn:
        store.upsert_product_master(meta, conn=conn)
        run_id = store.create_ingest_run(meta, conn=conn)
        # fail_case/run_case 는 여기서 쓰지 않는다 — rule 계산 후 present.persist 가
        # 저장 대상(should_store 통과)에만 upsert. (product/item master·spec 는 위에서 upsert.)
        cases = _build_cases(meta, run_input, persist=True, conn=conn)
    return {"run_id": run_id, "cases": cases}

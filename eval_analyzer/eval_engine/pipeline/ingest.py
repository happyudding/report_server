"""L0 Ingest — run_input(메모리 raw) → 캐노니컬 fail_case 들.

입력: docs/INTEGRATION_CONTRACT §3 (meta + raw_df[신규 df 포맷] / raw_table[레거시] / degrade items).
할 일:
  1. product_master / item_master / item_spec upsert (마스터).
  2. item 명 파싱: item_canonical(정규화) / item_base / item_phase, item_alias 해소.
  3. category_major(TRIM 포함 여부) / value_type(units→V|A|Hz|CODE|PF|Ohm|Sec) 분류.
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
from collections import Counter

import numpy as np

from .. import store
from ._rules import load_yaml
from .. import config

logger = logging.getLogger(__name__)

PASS_BIN = 1

# `_is_num` 과 동치인 "빠른 경로" 셀 타입 — 셀 타입이 전부 이 집합이면 NaN 제외 벡터
# 필터(astype float64 → isnan)가 종전 행 단위 _is_num 루프와 같은 집합을 남긴다.
# np.float64 는 float 하위형이라 _is_num 통과. np.int64/np.float32 는 (int, float)
# 하위형이 **아니라서** _is_num 이 걸렀으므로 여기 넣으면 안 된다(판정이 뒤집힌다).
_FAST_NUM_TYPES = {float, int, bool, np.float64}

# 단위 원문(소문자) → value_type. **정확일치 표**라 여기 없는 표기는 PF 로 떨어지고,
# PF 는 L1/L2 가 통계를 전부 비우는 부류라 그 item 은 어떤 signature 도 발화하지 못한다
# (= 무판정). 그래서 배율 접두(m/u/k/n)와 "0V"/"0A" 같은 테스터 표기도 명시 등록한다.
UNIT_TO_VALUE_TYPE = {
    "v": "V", "volt": "V", "volts": "V",
    "mv": "V", "uv": "V", "kv": "V", "nv": "V", "0v": "V",
    "a": "A", "amp": "A", "amps": "A", "ma": "A", "ua": "A", "na": "A", "0a": "A",
    "hz": "Hz", "khz": "Hz", "mhz": "Hz",
    "code": "CODE",
    "ohm": "Ohm", "ohms": "Ohm",
    "s": "Sec", "sec": "Sec", "secs": "Sec", "ms": "Sec", "us": "Sec", "ns": "Sec",
    # 선례 적재(db_input)가 쓰던 어휘 '%' 를 엔진에도 등록했다(2026-08-12). 미등록일 땐
    # PF 로 떨어져 통계가 통째로 비었고, 실측 fail 의 8% 가 그 이유로 무판정이었다.
    "%": "%", "pct": "%", "percent": "%",
    # LSB(code unit) 는 CODE 와 같은 부류 — 미등록 시 PF 폴백으로 무판정이 됐다.
    "lsb": "CODE",
    "pf": "PF", "p_f": "PF", "pass/fail": "PF", "p/f": "PF", "": "PF",
}
PHASE_TOKENS = {"init", "code", "trim", "p2", "p1", "final"}

# 정본 raw_df 레이아웃(REPORT_GENERATOR_DATA_REQUEST) — 파서가 위치/이름으로 고정 접근.
_META_COLS = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO"]
_META_ROW_LABELS = ["TSEQ", "TNO", "STEP", "UNIT", "HILIM", "LOLIM"]


def _norm(x):
    """레이아웃 검증용 정규화 — strip + BOM 제거 + 대문자. 컬럼·메타행 라벨 비교에만 쓴다."""
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
    # Counter 1회 — 종전 list.count 를 컬럼마다 부르면 O(M²)라 item 수천 개에서 눈에 띈다.
    dups = sorted(c for c, n in Counter(item_cols).items() if n > 1)
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
    """rules/item_alias.yaml 의 {원본 item명: canonical} 사전. 파일이 없으면 빈 dict."""
    try:
        doc = load_yaml(str(config.ITEM_ALIAS_FILE))
        return {k.strip(): v for k, v in (doc.get("aliases") or {}).items()}
    except FileNotFoundError:
        return {}


def _canonicalize(raw_name: str) -> str:
    """alias 에 없는 item 명의 기본 정규화 — 소문자 + 연속 공백을 밑줄 하나로."""
    return re.sub(r"\s+", "_", raw_name.strip().lower())


def _classify_value_type(unit, item_name) -> str:
    """UNIT 행 → value_type(V|A|Hz|CODE|Ohm|Sec|PF). 룰 스코프의 한 축이라 분류가 곧 임계값이다.

    UNIT 을 못 읽으면 item 명에 CODE 가 있는지로 한 번 더 보고, 그래도 모르면 PF(양불)로
    떨어뜨린다. PF 는 측정값이 없는 부류라 L1 이 통계량을 전부 비운다.
    """
    if unit:
        vt = UNIT_TO_VALUE_TYPE.get(str(unit).strip().lower())
        if vt:
            return vt
    if "CODE" in item_name.upper():
        return "CODE"
    return "PF"


def _classify_category_major(item_name: str) -> str:
    """item 명에 TRIM 이 들어 있으면 'TRIM', 아니면 'NON_TRIM'. item_class 의 첫 축."""
    return "TRIM" if "TRIM" in item_name.upper() else "NON_TRIM"


def _parse_base_phase(item_canonical: str):
    """canonical 을 밑줄로 쪼개 PHASE_TOKENS(init/code/trim/p1/p2/final) 하나를 phase 로 뽑는다.

    반환: (base, phase) — phase 를 뺀 나머지가 base. phase 토큰이 없으면 (canonical, None).
    같은 측정을 단계만 달리한 item 들을 base 로 묶어 보기 위한 분해다.
    """
    parts = item_canonical.split("_")
    phase = next((p for p in parts if p in PHASE_TOKENS), None)
    base = "_".join(p for p in parts if p != phase) if phase else item_canonical
    return base, phase


def _is_num(x):
    """이미 숫자 타입인가(NaN 제외). 문자열은 변환하지 않고 그대로 False — 파서가 건너뛴다."""
    return isinstance(x, (int, float)) and not (isinstance(x, float) and x != x)  # NaN 제외


def _num_or_none(v):
    """숫자면 float, NaN/변환불가면 None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _bin_or_none(v):
    """BIN 셀 → int. 공란/NaN/변환불가면 None."""
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


def _unit_text(unit):
    """UNIT 셀 → 진단 표시용 문자열. 공란/NaN 은 None (pandas 스칼라도 안전하게 처리)."""
    if unit is None or (isinstance(unit, float) and math.isnan(unit)):
        return None
    return str(unit).strip() or None


def _case_dict(meta, case_id, item_id, item_canonical, cat, value_type, bin_,
               revision, lsl, usl, values, fail_mask, x_pos, y_pos, site, skewness=None,
               item_raw=None, unit=None,
               spatial_fail_mask=None, spatial_x_pos=None, spatial_y_pos=None,
               spatial_dut=None):
    """fail_case context dict (raw_table/raw_df 경로 공유 — 스키마 단일 소스).

    `unit` 은 판정에 쓰이지 않는다(분류는 이미 value_type 으로 끝났다). value_type 이
    왜 그렇게 나왔는지 되짚기 위한 진단용 원문이다 — /pe/eval 트레이스가 표시한다.

    ⚠ **좌표/fail_mask 가 두 벌이다** (2026-08-28). `values`/`fail_mask`/`x_pos`/`y_pos`
    는 서로 같은 인덱스로 정렬된 **측정값 축**이고(= 측정 셀 파싱에 성공한 die 만),
    `spatial_*` 는 측정값과 무관한 **전체 die 축**이다. 공간 룰(E1/EDGE/CENTER/RING/
    SPOT)은 FAILTNO 와 좌표만 보므로 전체 die 를 모집단으로 써야 한다 — 측정값 축으로
    재면 값이 빈 die 가 분모에서 빠져 점유율이 왜곡되고, 값이 **전부** 빈 item 은 아예
    판정 불가가 된다. 두 축을 하나로 합칠 수 없는 이유는 측정값 기반 지표
    (`_fail_outlier_features`·`_fail_body_jump_ratio`·`_pass_limit_hit_ratio`)가
    `values` 와 길이가 같아야 하고, 다르면 None 을 돌려주며 조용히 죽기 때문이다.
    `spatial_*` 가 None 이면 측정값 축을 그대로 쓴다(레거시·degrade 경로 = 종전 동작).
    """
    return {
        "case_id": case_id, "item_id": item_id, "item_canonical": item_canonical,
        "item_raw": item_raw, "unit": _unit_text(unit),
        "category_major": cat, "value_type": value_type, "bin": bin_,
        # item_class 는 **2단**(category_major|value_type) — 2026-08-19.
        # 종전 3단은 마지막 조각이 bin 이었는데, case 가 item 단위가 된 뒤로 그 자리에
        # 대표 bin 을 박으면 세션마다 스코프 키가 흔들린다(thresholds item_class 오버레이·
        # calibrate 모집단이 갈린다). 동일성 기준을 value_type + item 으로 잡은 결정과도
        # 일치한다. 실측: 다제품 시드에서 버킷 34개(대부분 n<10) → 6개(전부 n≥10).
        "revision": revision, "item_class": f"{cat}|{value_type}",
        "product_type": meta.get("product_type"),
        "family_product": meta.get("family_product"),
        "lsl": lsl, "usl": usl, "skewness": skewness,
        "values": values, "fail_mask": fail_mask,
        "x_pos": x_pos, "y_pos": y_pos, "site": site,
        # 공간 축(전체 die) — 미전달이면 측정값 축을 그대로 쓴다(종전 동작).
        "spatial_fail_mask": fail_mask if spatial_fail_mask is None else spatial_fail_mask,
        "spatial_x_pos": x_pos if spatial_x_pos is None else spatial_x_pos,
        "spatial_y_pos": y_pos if spatial_y_pos is None else spatial_y_pos,
        # DUT(테스터 채널) — DUT_FAIL 판정용. 공간 축과 같은 전체 die 정렬이라
        # `spatial_fail_mask` 와 짝으로 읽는다. 없으면 None → feature 결측(미발화).
        "spatial_dut": spatial_dut,
    }


def _resolve_item_identity(raw_name, value_type, persist, conn, alias, unit=None):
    """원본 item 명 → (item_id, item_canonical, category_major). 3개 입력 경로가 공유한다.

    persist=True 면 item_master/item_alias 를 조회·upsert 해 **DB 가 준 item_id** 를 쓴다.
    persist=False(preview)는 DB 를 아예 열지 않으므로 canonical 의 sha1 앞 8자리를 item_id
    로 대신 쓴다 — 그래서 preview 의 case_id 는 나중에 persist 로 재실행하면 달라질 수 있다.

    `unit` 은 판정에 쓰이지 않는 진단용 원문이지만 **반드시 넘겨야 한다** —
    `upsert_item_master` 가 `unit=excluded.unit` 으로 덮으므로 None 을 넘기면 다른 적재
    경로(web_report/eval_export)가 채워 둔 UNIT 원문을 지운다. value_type 이 PF 로
    오분류된 항목을 되짚는 유일한 단서라 지워지면 진단이 불가능해진다.
    """
    item_canonical = alias.get(raw_name.strip(), _canonicalize(raw_name))
    base, phase = _parse_base_phase(item_canonical)
    cat = _classify_category_major(raw_name)
    if persist:
        item_id = store.resolve_item_id(raw_name, conn=conn)
        if item_id is None:
            item_id = store.upsert_item_master(item_canonical, raw_name, base, phase,
                                               cat, None, value_type, _unit_text(unit),
                                               conn=conn)
            store.upsert_item_alias(raw_name, item_id, conn=conn)
    else:
        item_id = int(hashlib.sha1(item_canonical.encode()).hexdigest()[:8], 16)
    return item_id, item_canonical, cat


def _ingest_raw_table(meta, raw_table, persist, conn, alias):
    """레거시 raw_table(중립 dict) → fail_case 들. df_honey 어댑터가 쓰던 경로.

    정본 raw_df 와 **fail 식별 방식이 다르다** — 여기서는 FAILTNO 가 없으므로 "limit 위반
    (lo|hi) AND non-pass bin" 인 (item, bin) 조합을 fail 로 본다(report_server
    build_issue_table 과 같은 의미). limit 위반이 하나도 없는 item 은 case 를 만들지 않는다.
    """
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
            item, value_type, persist, conn, alias, unit)
        if persist and revision is not None and (lsl is not None or usl is not None):
            store.upsert_item_spec(item_id, meta.get("product_name"), revision,
                                   lsl, usl, conn=conn)

        # case 는 item 당 1개 (2026-08-19 — raw_df 경로와 같은 규약, 위 _ingest_raw_df 참조).
        # 대표 bin = fail bin 중 가장 작은 값. 이 레거시 경로는 fail_mask 를 "그 bin 인
        # DUT 전부" 로 잡는 별개 문제가 있어(이 item 이 limit 을 안 넘긴 DUT 도 fail 로 표시)
        # **mask 로직은 이번에 건드리지 않는다** — case 축만 맞춘다.
        bin_ = min(fail_bins)
        fail_mask = [b in fail_bins for b in bins]
        case_id = store.make_case_id(meta.get("product_name"), meta.get("lot_id"),
                                     meta.get("wafer_number"), item_id, None, revision)
        cases.append(_case_dict(meta, case_id, item_id, item_canonical, cat,
                                value_type, bin_, revision, lsl, usl,
                                values, fail_mask, x_pos, y_pos, site,
                                item_raw=item, unit=unit))
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
    # DUT(테스터 채널/소켓) — DUT_FAIL 판정용. 공간 좌표와 같은 **전체 die 축**이다
    # (측정값 파싱 성공 여부와 무관 — fail 식별은 FAILTNO 로 끝나고 DUT 는 그 die 가
    # 어느 채널에서 측정됐나일 뿐이다). 값 형태가 제품마다 숫자/문자로 갈려 _tno_norm
    # 처럼 "숫자면 int, 문자면 strip" 으로 정규화한다 — 그룹 키로만 쓰므로 크기 비교는
    # 하지 않는다.
    dut_all = [_tno_norm(v) for v in data["DUT"]]
    bin_all = [_bin_or_none(v) for v in data["BIN"]]
    failtno_all = [_tno_norm(v) for v in data["FAILTNO"]]
    # fail 행 인덱스 사전 계산 (2026-08-13 콜드 빌드 최적화) — fail 은 전체 행의 소수라,
    # case(bin)마다 전량 재스캔하던 fail_bins/fail_mask/fail_count 를 이 목록 순회로
    # 대체한다(판정값 동일 — FAILTNO 가 None 인 행은 어떤 tno 와도 같을 수 없다).
    fail_idx_all = [i for i, ft in enumerate(failtno_all) if ft is not None]

    # 좌표 전처리(features._spatial_geometry: 중심정렬·반경·E1 마스크) 공유통 — 한 소스의
    # item 들은 같은 die 목록을 쓰므로 좌표가 하나뿐인데 종전에는 item 마다 다시 만들었다.
    # 아래에서 **x_pos/y_pos 가 x_all/y_all 그 객체인 case 에만** 붙인다(NaN 이 섞여 좌표를
    # 따로 만든 item 은 좌표가 다르므로 공유하면 안 된다).
    run_geom = {}
    cases = []
    empty_values = 0        # 측정값이 하나도 안 읽힌 item 수 (아래 경고 판정용)
    for item in item_cols:
        value_type = _classify_value_type(unit_row[item], item)
        lsl, usl = _num_or_none(lolim_row[item]), _num_or_none(hilim_row[item])
        tno_i = _tno_norm(tno_row[item])

        # ── 측정 셀 파싱 — _is_num 규약(이미 숫자 타입만, NaN 제외) 그대로 ──────────
        # 빠른 경로: 셀 타입이 전부 _FAST_NUM_TYPES 면 _is_num 은 "NaN 만 제외" 와
        # 동치라 벡터로 거른다. honeyform 경로(ai_comment._table_to_raw_df)는
        # astype("float64") 를 거쳐 와 실운영 셀이 전부 이 경로다. 전 셀 유효(대부분)면
        # 메타 병렬 배열을 복사하지 않고 **그대로 공유**한다 — 종전에는 item 마다
        # N 행 파이썬 루프로 5개 리스트를 재조립했다(O(N·M) 지배 비용).
        # 그 외 타입(문자열·None·np.int64 등)이 섞이면 종전 행 단위 루프로 폴백한다.
        arr = data[item].to_numpy()
        values = x_pos = y_pos = bins = failtnos = fail_idx = None
        if set(map(type, arr)) <= _FAST_NUM_TYPES:
            farr = arr.astype(np.float64)
            keep = ~np.isnan(farr)
            if keep.all():
                values = farr.tolist()
                x_pos, y_pos, bins, failtnos = x_all, y_all, bin_all, failtno_all
                fail_idx = fail_idx_all
            else:
                values = farr[keep].tolist()
                keep_list = keep.tolist()
                x_pos = [x for x, k in zip(x_all, keep_list) if k]
                y_pos = [y for y, k in zip(y_all, keep_list) if k]
                bins = [b for b, k in zip(bin_all, keep_list) if k]
                failtnos = [f for f, k in zip(failtno_all, keep_list) if k]
        if values is None:
            values, x_pos, y_pos, bins, failtnos = [], [], [], [], []
            for v, x, y, b, ft in zip(arr, x_all, y_all, bin_all, failtno_all):
                if not _is_num(v):
                    continue
                values.append(float(v))
                x_pos.append(x); y_pos.append(y); bins.append(b); failtnos.append(ft)
        # ⚠ 측정값이 하나도 없어도 **case 를 만든다** (2026-08-28).
        # 종전에는 여기서 `continue` 로 버렸는데, 그러면 값이 전부 빈 item(functional
        # test 등 — 컬럼은 있고 셀이 전부 NaN)이 평가 대상에서 통째로 사라져 공간 룰
        # (E1/EDGE/CENTER/RING/SPOT)이 발화할 기회조차 없었다. 그 item 도 FAILTNO 는
        # 채워져 있어 "어느 die 가 이 item 때문에 fail 났나" 를 알 수 있고, 공간 판정에
        # 필요한 건 그 좌표뿐이다. 실제 증상은 "Yield 행은 있는데 AI Comment 만 없다".
        # 측정값 기반 지표는 values 가 비면 L1/L2 가 알아서 None 으로 둔다(PF 와 같은 취급).
        if not values:
            empty_values += 1
        if fail_idx is None:
            fail_idx = [i for i, ft in enumerate(failtnos) if ft is not None]

        # fail bin 도수: FAILTNO == 이 item 의 TNO 인 serial 의 BIN 별 건수.
        # **대표 bin 선정에만 쓴다** — case 를 가르는 축이 아니다(2026-08-19, 아래 참조).
        fail_bin_counts = {}
        if tno_i is not None:
            for i in fail_idx:
                if failtnos[i] == tno_i and bins[i] is not None:
                    fail_bin_counts[bins[i]] = fail_bin_counts.get(bins[i], 0) + 1

        item_id, item_canonical, cat = _resolve_item_identity(
            item, value_type, persist, conn, alias, unit_row[item])
        if persist and revision is not None and (lsl is not None or usl is not None):
            store.upsert_item_spec(item_id, meta.get("product_name"), revision,
                                   lsl, usl, conn=conn)

        # site 는 raw_df 경로에 없다 — [None]*n 리스트 대신 None:
        # features._site_cpk_delta 가 O(N) 전수 스캔 없이 즉시 결측 처리한다(판정 동일).
        site = None
        # ── case 는 **item 당 1개**다 (2026-08-19, 사용자 결정) ──────────────────
        # 종전에는 fail bin 마다 case 를 만들었다. bin 을 뺀 이유:
        #  · bin 은 serial(die)의 최종 binning 결과이지 item 의 속성이 아니다. 실업로드
        #    3,334 fail 그룹 전수에서 (소스, FAILTNO) 당 distinct BIN 이 **100% 1개**였고,
        #    반대로 bin 하나가 평균 4.53개 item 을 담았다 — bin 은 item 을 가르는 축이
        #    아니라 item 들을 묶는 더 굵은 축이라 **식별 정보를 0 추가**하면서 case 만 쪼갠다.
        #  · 제품군·개발 ENGR 이 바뀌면 같은 item 의 bin 이 달라진다(다제품 시드 실측:
        #    같은 item 이 제품군별로 bin 집합이 전부 다름). 불안정한 축을 키로 쓰면 같은
        #    현상의 코멘트·라벨·선례가 쪼개져 희석된다(운영 라벨 1건이 실제로 100% 고아였다).
        #  · 동일성 기준은 **value_type + item 명**이다(선례검색이 이미 그 축만 쓴다).
        # bin 을 버리는 것은 아니다 — 아래 대표 bin 을 case["bin"] 에 실어 fail_case.bin
        # 컬럼과 bin_taxonomy(severity_bias) 조회에 계속 쓴다.
        item_shared = {}
        fail_mask = [False] * len(values)
        if tno_i is not None:
            for i in fail_idx:
                if failtnos[i] == tno_i:
                    fail_mask[i] = True
        # 공간 축은 **전체 die** 기준으로 따로 만든다 (2026-08-28) — 위 fail_mask 는
        # values 와 정렬을 맞춘 측정값 축이라, 측정값이 빈 die 가 빠져 점유율 분모가
        # 좁아진다(값이 전부 비면 아예 길이 0). 공간 룰은 FAILTNO 와 좌표만 보므로
        # 측정 여부와 무관하게 전 die 를 모집단으로 써야 한다.
        # 측정값이 전 die 유효한 흔한 경우엔 두 배열이 같은 내용이므로 재사용한다.
        if x_pos is x_all and y_pos is y_all:
            sp_mask = fail_mask
        else:
            sp_mask = [False] * len(x_all)
            if tno_i is not None:
                for i in fail_idx_all:
                    if failtno_all[i] == tno_i:
                        sp_mask[i] = True
        # 대표 bin = 최다 fail bin. 동률은 **작은 bin**(재실행마다 흔들리면 fail_case.bin
        # 이 요동쳐 트레이스 diff·표본함이 허위 변화를 보고한다 — 결정성이 필수다).
        bin_ = (min(fail_bin_counts, key=lambda b: (-fail_bin_counts[b], b))
                if fail_bin_counts else PASS_BIN)
        # case_id 는 **fail 유무와 무관하게 bin 자리를 항상 None** 으로 둔다 — CPK/ETC
        # 섹션 라벨(bin 개념이 없다)과 같은 case 로 만나야 하기 때문(eval_export).
        case_id = store.make_case_id(meta.get("product_name"), meta.get("lot_id"),
                                     meta.get("wafer_number"), item_id, None, revision)
        case = _case_dict(meta, case_id, item_id, item_canonical, cat,
                          value_type, bin_, revision, lsl, usl,
                          values, fail_mask, x_pos, y_pos, site,
                          item_raw=item, unit=unit_row[item],
                          spatial_fail_mask=sp_mask,
                          spatial_x_pos=x_all, spatial_y_pos=y_all,
                          spatial_dut=dut_all)
        case["_shared"] = item_shared
        # 좌표 전처리(_spatial_geometry) 공유통 — 공간 축은 **항상 소스 공용 좌표**
        # (x_all/y_all)라 item 마다 같다. 그래서 종전의 "측정값 결측이 없을 때만" 조건이
        # 필요 없어졌고, NaN 이 섞인 item 도 이제 전처리를 공유한다(값 동일·재계산만 제거).
        case["_geom_shared"] = run_geom
        # yield 분모/분자는 전체 DUT(데이터 행) 기준 — item 셀 파싱 성공분(len(values))으로
        # 재면 item 마다 분모가 달라져 trump/GROSS_FAIL 비교가 왜곡된다. FAILTNO 기반
        # fail 식별은 측정값 파싱과 무관하므로 전체 행에서 센다. (fail_mask 는 공간
        # feature 용 — values 배열과 정렬 유지, 그대로 둔다.)
        # ⚠ 의미 변화(의도적): 종전 bin 별 합계는 `bin_all[i] is not None` 을 요구해
        # **FAILTNO 는 맞는데 BIN 셀이 파싱 불가인 행을 조용히 누락**했다. item 단위
        # 합집합은 그 행도 센다 — 더 옳지만 옛 값과 합이 다를 수 있다.
        case["total_count"] = len(data)
        case["fail_count"] = sum(1 for i in fail_idx_all if failtno_all[i] == tno_i)
        cases.append(case)
    # ⚠ 경고 조건이 "case 0" 이 아니라 "**측정값이 빈 case 가 전부**" 다 (2026-08-28).
    # 값이 전부 빈 item 도 이제 case 를 만들므로(공간 룰 발화용) case 는 0 이 되지 않는다.
    # 그래도 진단 가치는 그대로다 — item 셀이 문자열 dtype 이면 측정값을 통째로 못 읽어
    # cpk·분포 룰이 전멸하는데, 그게 파서 무시인지 원래 값이 없는 것인지 여기서만 갈린다.
    if item_cols and len(data) > 0 and cases and empty_values == len(cases):
        logger.warning("raw_df: item %d개, 데이터 %d행이나 측정값이 읽힌 item 0 - "
                       "item 셀 dtype 확인(문자열이면 파서가 무시, "
                       "docs/EVALUATE_RETURN_SPEC 6절). 공간 룰만 평가된다.",
                       len(item_cols), len(data))
    return cases


def _ingest_degrade(meta, items, persist, conn, alias):
    """degrade 경로 — per-DUT raw 없이 요약통계(yield/fail_count/…)를 직접 받는다.

    values/fail_mask/좌표가 전부 빈 리스트라 L1 은 넘겨받은 요약값을 그대로 쓰고 L2 공간
    feature 는 결측이 된다(→ data_completeness 하락). raw 를 못 구하는 입력의 폴백 경로.
    """
    revision = meta.get("revision")
    # 입력이 (item, bin) 행 목록이라 item 으로 묶는다 — case 는 item 당 1개
    # (2026-08-19, raw_df 경로와 같은 규약). fail_count 는 합, 대표 bin 은 최다 fail
    # (동률은 작은 bin — 결정성), total_count/yield 는 같은 item 이면 동일값 전제라 첫 행 값.
    grouped: dict = {}
    order: list = []
    for it in items:
        key = str(it["item_name"])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(it)
    cases = []
    for key in order:
        rows = grouped[key]
        it = rows[0]
        raw_name = it["item_name"]
        value_type = _classify_value_type(it.get("unit"), raw_name)
        bin_counts: dict = {}
        for r in rows:
            b = int(r["bin"])
            bin_counts[b] = bin_counts.get(b, 0) + int(r.get("fail_count") or 0)
        bin_ = min(bin_counts, key=lambda b: (-bin_counts[b], b))
        fail_count = sum(int(r.get("fail_count") or 0) for r in rows)
        total_count = it.get("total_count")
        yield_ = (1 - fail_count / total_count) if total_count else it.get("yield")
        lsl, usl = it.get("lsl"), it.get("usl")
        item_id, item_canonical, cat = _resolve_item_identity(
            raw_name, value_type, persist, conn, alias, it.get("unit"))
        if persist and revision is not None and (lsl is not None or usl is not None):
            store.upsert_item_spec(item_id, meta.get("product_name"), revision,
                                   lsl, usl, conn=conn)
        case_id = store.make_case_id(meta.get("product_name"), meta.get("lot_id"),
                                     meta.get("wafer_number"), item_id, None, revision)
        cases.append({
            "case_id": case_id, "item_id": item_id, "item_canonical": item_canonical,
            "item_raw": raw_name, "unit": _unit_text(it.get("unit")),
            "category_major": cat, "value_type": value_type, "bin": bin_,
            "revision": revision, "item_class": f"{cat}|{value_type}",
            "product_type": meta.get("product_type"),
            "family_product": meta.get("family_product"),
            "lsl": lsl, "usl": usl, "skewness": it.get("skewness"),
            "values": [], "fail_mask": [], "x_pos": [], "y_pos": [], "site": [],
            "yield": yield_, "fail_count": fail_count,
            "total_count": total_count,
        })
    return cases


def _build_cases(meta, run_input, persist, conn):
    """run_input 키로 입력 3경로를 분기하고, 만들어진 case 에 공통 meta 를 덧붙인다.

    분기 순서: raw_df(정본) → raw_table(레거시) → items(degrade). raw_df 는 DataFrame 이라
    진리값이 모호하므로 **반드시 `is not None`** 으로 판별한다(`if raw_df:` 로 바꾸면
    빈 df 가 아닌데도 다음 분기로 새거나 ValueError 가 난다).
    """
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
        # 선례검색 자기 세션/자기 데이터 제외용 (store.search_precedents) — 없으면 no-op
        case["session_id"] = meta.get("session_id")
        case["analysis_key"] = meta.get("analysis_key")
    return cases


def ingest(run_input: dict, *, persist: bool = True, db_path=None) -> dict:
    """L0 진입점 — run_input → {"run_id", "cases"}. 위 모듈 docstring 이 전체 계약.

    persist=True 면 커넥션 하나로 product_master upsert + ingest_run 생성 + case 조립을
    한 트랜잭션에 묶는다. fail_case/run_case 는 **여기서 쓰지 않는다** — 룰 계산 뒤
    `present.should_store` 를 통과한 case 만 `present.persist` 가 남긴다.
    persist=False(preview)는 DB 를 열지 않아 run_id 가 None 이다.

    `db_path` 는 persist=True 일 때 열 DB 파일(기본 `config.DB_PATH`). 전역 대입 대신
    인자로 받는 이유는 `store.get_conn` docstring 참조.
    """
    meta = run_input["meta"]
    _validate_product_meta(meta)
    if not persist:
        cases = _build_cases(meta, run_input, persist=False, conn=None)
        return {"run_id": None, "cases": cases}

    with store.get_conn(db_path) as conn:
        store.upsert_product_master(meta, conn=conn)
        run_id = store.create_ingest_run(meta, conn=conn)
        # fail_case/run_case 는 여기서 쓰지 않는다 — rule 계산 후 present.persist 가
        # 저장 대상(should_store 통과)에만 upsert. (product/item master·spec 는 위에서 upsert.)
        cases = _build_cases(meta, run_input, persist=True, conn=conn)
    return {"run_id": run_id, "cases": cases}

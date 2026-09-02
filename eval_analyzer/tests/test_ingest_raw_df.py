"""신규 raw df 포맷(REPORT_GENERATOR_DATA_REQUEST) ingest 테스트.

레이아웃: columns[:7]=meta(SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO), [7:]=item.
row0 TSEQ / row1 TNO / row2 STEP / row3 UNIT / row4 HILIM / row5 LOLIM / row6+ 측정.
fail 식별 = FAILTNO(serial이 fail한 test의 TNO) == item의 TNO.
"""
import logging
import math

import pandas as pd
import pytest

from eval_engine import api, store
from eval_engine.pipeline import ingest

_COLS = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO", "VREF_TRIM", "IDDQ"]
_VREF_TNO, _IDDQ_TNO = 101, 202


def _new_df(n_pass=20, n_fail=4):
    """VREF_TRIM 이 n_fail 개 serial 에서 fail(FAILTNO=101, bin18, edge). IDDQ 는 무fail."""
    nan = float("nan")
    meta_rows = [
        ["TSEQ", None, None, None, None, None, None, 1, 2],
        ["TNO", None, None, None, None, None, None, _VREF_TNO, _IDDQ_TNO],
        ["STEP", None, None, None, None, None, None, "P2", "P1"],
        ["UNIT", None, None, None, None, None, None, "V", "A"],
        ["HILIM", None, None, None, None, None, None, 1.4, 15.0],
        ["LOLIM", None, None, None, None, None, None, 1.0, nan],
    ]
    data_rows = []
    s = 1
    for i in range(n_pass):  # pass: bin1, 중앙부, spec 내, FAILTNO 없음
        data_rows.append([s, 1, s, i % 5, i // 5, 1, nan,
                          1.20 + 0.01 * (i % 3), 12.0 + 0.01 * (i % 4)])
        s += 1
    for i in range(n_fail):  # fail: VREF_TRIM(FAILTNO=101), bin18, edge, usl 초과
        data_rows.append([s, 1, s, 50 + i, 50 + i, 18, _VREF_TNO,
                          1.55 + 0.02 * i, 12.1])
        s += 1
    return pd.DataFrame(meta_rows + data_rows, columns=_COLS)


def _meta():
    """product_taxonomy 검증을 통과하는 최소 meta(PMIC/SOC)."""
    return {"product_name": "S5E_TEST_0000001", "family_product": "SOC",
            "product_type": "PMIC", "revision": 0.0, "lot_id": "LOT001",
            "wafer_number": 3}


def test_raw_df_fail_mapping_no_persist():
    run = {"meta": _meta(), "raw_df": _new_df()}
    ctx = ingest.ingest(run, persist=False)
    cases = ctx["cases"]
    # 모든 item 이 candidate 로 방출: VREF_TRIM fail(bin18) + IDDQ 무fail(PASS_BIN candidate)
    assert len(cases) == 2
    c = next(x for x in cases if x["bin"] == 18)
    assert c["item_canonical"] == "vref_trim"
    assert c["item_raw"] == "VREF_TRIM"      # 원본 item명 보존 (Issue Table join 키)
    assert c["value_type"] == "V"
    assert c["lsl"] == 1.0 and c["usl"] == 1.4
    # 분포는 전체 serial(측정된 값) 기준, fail_mask 는 4개만 True
    assert len(c["values"]) == 24
    assert sum(1 for f in c["fail_mask"] if f) == 4
    # 공간: fail serial 은 edge 좌표
    assert all(x is not None for x in c["x_pos"])
    # IDDQ 는 fail 없음 → PASS_BIN(1) candidate, fail_mask 전부 False (저장 판단은 이후 should_store)
    iddq = next(x for x in cases if x["bin"] == 1)
    assert iddq["item_canonical"] == "iddq"
    assert not any(iddq["fail_mask"])


def test_raw_df_e2e_fires_signature(fresh_db):
    result = api.evaluate({"meta": _meta(), "raw_df": _new_df()}, persist=True)
    assert result["run_id"] is not None
    cases = [c for c in result["cases"] if c["bin"] == 18]
    assert len(cases) == 1
    case = cases[0]
    assert case["item_canonical"] == "vref_trim"
    assert case["item_raw"] == "VREF_TRIM"
    assert case["issue_category"] in {"YIELD", "CPK", "ETC"}
    assert case["status"] in {"MAJOR", "CRITICAL"}
    assert case["primary_signature"] is not None
    with store.get_conn() as conn:
        # bin 으로 세는 이유: should_store 가 signature 발화만으로도 저장하므로
        # pass bin(1) candidate 도 룰이 걸리면 함께 적재된다(총 개수는 룰에 따라 변함).
        assert conn.execute("SELECT COUNT(*) FROM fail_case WHERE bin=18").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM features").fetchone()[0] >= 1


def test_bin1_mask_is_wired_to_case():
    """ingest 가 `bin1_mask` 를 case 에 실어야 L1 cpk 가 Bin1 기준이 된다 (2026-09-02).

    배선 테스트다 — 마스크를 안 넘기면 metrics 가 조용히 전 die 폴백으로 돌아가고,
    cpk 만 옛 값으로 되돌아온 채 아무 에러도 안 난다(화면 증상은 "미분류").
    """
    ctx = ingest.ingest({"meta": _meta(), "raw_df": _new_df()}, persist=False)
    c = next(x for x in ctx["cases"] if x["item_raw"] == "VREF_TRIM")
    mask = c["bin1_mask"]
    assert mask is not None
    assert len(mask) == len(c["values"])        # 측정값 축과 같은 정렬
    assert sum(mask) == 20                      # pass 20 개만 Bin1
    # fail(BIN=18) 자리는 정확히 마스크에서 빠져 있다.
    assert [i for i, keep in enumerate(mask) if not keep] == list(range(20, 24))


def test_bin1_cpk_differs_from_all_die_end_to_end():
    """L0→L1 전 구간에서 cpk 가 Bin1 모집단으로 나온다 (계산이 아니라 흐름 확인)."""
    from eval_engine.pipeline import metrics
    ctx = ingest.ingest({"meta": _meta(), "raw_df": _new_df()}, persist=False)
    c = next(x for x in ctx["cases"] if x["item_raw"] == "VREF_TRIM")
    m = metrics.compute(c)
    bin1_vals = [v for v, keep in zip(c["values"], c["bin1_mask"]) if keep]
    assert m["cpk"] == metrics.cpk_summary(bin1_vals, c["lsl"], c["usl"])["cpk"]
    # fail 이 usl 을 넘겨 있으므로 전 die 기준과 실제로 값이 갈린다.
    assert m["cpk"] != metrics.cpk_summary(c["values"], c["lsl"], c["usl"])["cpk"]


def test_raw_df_failtno_blank_is_pass():
    """FAILTNO 공란/NaN serial 은 fail 로 잡히지 않는다. 이제 모든 item 은 PASS_BIN candidate
    로 방출되고(저장 여부는 이후 api.evaluate 의 should_store 판단), fail_mask 는 전부 False."""
    df = _new_df(n_pass=24, n_fail=0)
    ctx = ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)
    cases = ctx["cases"]
    assert len(cases) == 2                          # VREF_TRIM, IDDQ — 둘 다 무fail
    assert all(c["bin"] == 1 for c in cases)        # PASS_BIN candidate
    assert all(not any(c["fail_mask"]) for c in cases)


def _multibin_df(bin_counts=((18, 5), (31, 3), (20, 3))):
    """VREF_TRIM 의 fail 이 **여러 bin 으로 흩어진** 프레임.

    bin_counts = ((bin, fail 개수), …). 같은 item 을 fail 한 serial 이 binning 관례에 따라
    서로 다른 bin 을 받은 상황(제품군·ENGR 이 다르면 실제로 일어난다).
    """
    nan = float("nan")
    meta_rows = [
        ["TSEQ", None, None, None, None, None, None, 1, 2],
        ["TNO", None, None, None, None, None, None, _VREF_TNO, _IDDQ_TNO],
        ["STEP", None, None, None, None, None, None, "P2", "P1"],
        ["UNIT", None, None, None, None, None, None, "V", "A"],
        ["HILIM", None, None, None, None, None, None, 1.4, 15.0],
        ["LOLIM", None, None, None, None, None, None, 1.0, nan],
    ]
    rows, s = [], 1
    for i in range(20):
        rows.append([s, 1, s, i % 5, i // 5, 1, nan,
                     1.20 + 0.01 * (i % 3), 12.0 + 0.01 * (i % 4)])
        s += 1
    for bin_, cnt in bin_counts:
        for i in range(cnt):
            rows.append([s, 1, s, 50 + i, 50 + i, bin_, _VREF_TNO, 1.55 + 0.02 * i, 12.1])
            s += 1
    return pd.DataFrame(meta_rows + rows, columns=_COLS)


def test_multi_bin_item_becomes_one_case():
    """**case 는 item 당 1개**다 — fail 이 여러 bin 으로 흩어져도 쪼개지지 않는다 (2026-08-19).

    bin 은 serial(die)의 binning 관례이지 item 의 속성이 아니라, 키로 쓰면 같은 현상의
    코멘트·라벨·선례가 갈라진다. 동일성 기준은 value_type + item 명이다.
    """
    ctx = ingest.ingest({"meta": _meta(), "raw_df": _multibin_df()}, persist=False)
    cases = ctx["cases"]
    assert len(cases) == 2, [(c["item_raw"], c["bin"]) for c in cases]   # VREF_TRIM, IDDQ
    vref = next(c for c in cases if c["item_canonical"] == "vref_trim")

    # fail_mask 는 bin 과 무관하게 **그 item 을 fail 한 전 serial** 의 합집합
    assert sum(1 for f in vref["fail_mask"] if f) == 11, vref["fail_mask"]
    assert vref["fail_count"] == 11
    assert vref["total_count"] == 31

    # 대표 bin = 최다 fail(18: 5건). 참고값으로 보존되며 severity_bias 조회에 쓰인다.
    assert vref["bin"] == 18

    # item_class 에도 bin 이 없다(2단) — thresholds 스코프가 세션마다 흔들리지 않게.
    assert vref["item_class"] == "TRIM|V", vref["item_class"]


def test_representative_bin_is_deterministic():
    """동률이면 **작은 bin** — 재실행마다 흔들리면 fail_case.bin 이 요동쳐 트레이스 diff·
    표본함이 허위 변화를 보고한다."""
    ctx = ingest.ingest(
        {"meta": _meta(), "raw_df": _multibin_df(((31, 4), (18, 4)))}, persist=False)
    vref = next(c for c in ctx["cases"] if c["item_canonical"] == "vref_trim")
    assert vref["bin"] == 18                      # 4:4 동률 → 작은 쪽
    assert vref["fail_count"] == 8


def test_case_id_ignores_bin():
    """같은 item 이면 fail bin 구성이 달라도 **같은 case_id** 여야 한다.

    이게 깨지면 사람이 단 코멘트·라벨이 엔진 판정과 다른 case 로 가서 학습이 성립하지 않는다
    (운영 DB 에서 실제로 라벨 100% 가 고아였다).
    """
    a = ingest.ingest({"meta": _meta(), "raw_df": _multibin_df(((18, 5), (31, 3)))},
                      persist=False)["cases"]
    b = ingest.ingest({"meta": _meta(), "raw_df": _multibin_df(((20, 2), (40, 9)))},
                      persist=False)["cases"]
    id_a = next(c["case_id"] for c in a if c["item_canonical"] == "vref_trim")
    id_b = next(c["case_id"] for c in b if c["item_canonical"] == "vref_trim")
    assert id_a == id_b
    # 대표 bin 은 서로 다르다 — 값은 보존되지만 키에는 안 들어간다는 뜻
    bin_a = next(c["bin"] for c in a if c["item_canonical"] == "vref_trim")
    bin_b = next(c["bin"] for c in b if c["item_canonical"] == "vref_trim")
    assert bin_a == 18 and bin_b == 40


_LOWCPK_COLS = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO", "VOUT", "VREF_OK"]


def _lowcpk_df(n=30):
    """무fail(FAILTNO 공란) 두 item: VOUT 은 산포 넓어 cpk<1.33, VREF_OK 는 tight 해 cpk 높음."""
    nan = float("nan")
    meta_rows = [
        ["TSEQ", None, None, None, None, None, None, 1, 2],
        ["TNO", None, None, None, None, None, None, 303, 404],
        ["STEP", None, None, None, None, None, None, "P2", "P2"],
        ["UNIT", None, None, None, None, None, None, "V", "V"],
        ["HILIM", None, None, None, None, None, None, 1.4, 2.0],
        ["LOLIM", None, None, None, None, None, None, 1.0, 0.0],
    ]
    data_rows = []
    for i in range(n):  # 전부 bin1, FAILTNO 공란(무fail)
        vout = 1.20 + 0.06 * ((i % 5) - 2)   # 1.08~1.32, 넓은 산포 → cpk 낮음
        vref = 1.00 + 0.02 * ((i % 3) - 1)   # 0.98~1.02, tight → cpk 높음
        data_rows.append([i + 1, 1, i + 1, i % 5, i // 5, 1, nan, vout, vref])
    return pd.DataFrame(meta_rows + data_rows, columns=_LOWCPK_COLS)


def test_lowcpk_nofail_is_stored(fresh_db):
    """yield fail 없어도 cpk<cpk_warn 이면 저장 (PASS_BIN candidate 의 cpk 트리거)."""
    result = api.evaluate({"meta": _meta(), "raw_df": _lowcpk_df()}, persist=True)
    # VOUT 을 지목해 확인한다 — should_store 가 signature 발화만으로도 저장하므로
    # 다른 item 이 룰에 걸려 함께 담길 수 있고, 총 개수는 룰 구성에 따라 변한다.
    case = next(c for c in result["cases"] if c["item_canonical"] == "vout")
    assert case["bin"] == 1                       # PASS_BIN — yield fail 아닌 cpk 트리거
    with store.get_conn() as conn:
        cpk = conn.execute(
            "SELECT cpk FROM raw_metrics WHERE case_id=?", (case["case_id"],)).fetchone()[0]
    assert cpk is not None and cpk < 1.33         # cpk<cpk_warn 이라 저장된 것


# --- 레이아웃 구조 선검증(_validate_raw_df) — 계약 위반 시 명확한 ValueError -------------

def test_raw_df_rejects_wrong_meta_columns():
    df = _new_df()
    df.columns = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "WRONG", "VREF_TRIM", "IDDQ"]
    with pytest.raises(ValueError, match="meta 컬럼"):
        ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)


def test_raw_df_rejects_reordered_meta_rows():
    df = _new_df()
    df.iloc[2, 0], df.iloc[3, 0] = "UNIT", "STEP"   # STEP↔UNIT 라벨 교환
    with pytest.raises(ValueError, match="메타행"):
        ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)


def test_raw_df_rejects_too_few_rows():
    df = pd.DataFrame([[lab, None, None, None, None, None, None, 1, 2]
                       for lab in ["TSEQ", "TNO", "STEP", "UNIT", "HILIM"]],  # 메타행 5개뿐
                      columns=_COLS)
    with pytest.raises(ValueError, match="메타행 6개"):
        ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)


def test_raw_df_rejects_duplicate_items():
    df = _new_df()
    df.columns = _COLS[:7] + ["IDDQ", "IDDQ"]        # item 컬럼 중복
    with pytest.raises(ValueError, match="중복"):
        ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)


def test_raw_df_warns_on_nonnumeric_items(caplog):
    """item 데이터셀이 전부 문자열 → 파서가 무시해 측정값 0. 하드에러 아님, warning 으로 legible.

    ⚠ case 자체는 **만들어진다** (2026-08-28) — 측정값이 없어도 FAILTNO+좌표만으로
    공간 룰(E1/EDGE/CENTER/RING/SPOT)을 평가해야 하기 때문이다. 종전에는 여기서
    case 를 통째로 버려 그 item 이 평가 대상에서 사라졌다.
    """
    df = _new_df(n_pass=4, n_fail=0)
    for col in ("VREF_TRIM", "IDDQ"):
        df.loc[6:, col] = df.loc[6:, col].astype(str)   # 데이터행만 문자열화
    with caplog.at_level(logging.WARNING):
        ctx = ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)
    assert len(ctx["cases"]) == 2, "측정값이 없어도 case 는 만들어져야 한다(공간 룰용)"
    assert all(c["values"] == [] for c in ctx["cases"])
    # 좌표·공간 mask 는 **전체 die** 기준으로 실린다(측정값 축이 비어도).
    assert all(len(c["spatial_x_pos"]) == 4 for c in ctx["cases"])
    assert "측정값이 읽힌 item 0" in caplog.text


# --- meta 전파: 선례검색 자기 데이터 제외의 전제 -----------------------------------------

def test_meta_session_keys_reach_every_case():
    """session_id/analysis_key 가 모든 case 에 실려야 선례검색이 자기 세션을 뺄 수 있다.

    이 주입이 빠지면 precedent_client 가 항상 None 을 넘겨 시간 누출 차단이 조용히
    무력화된다(발화·status 는 멀쩡해 보여 눈치채기 어렵다).
    """
    meta = {**_meta(), "session_id": "1700000000_abc", "analysis_key": "AK9"}
    cases = ingest.ingest({"meta": meta, "raw_df": _new_df()}, persist=False)["cases"]
    assert cases
    assert all(c["session_id"] == "1700000000_abc" for c in cases)
    assert all(c["analysis_key"] == "AK9" for c in cases)


def test_meta_session_keys_default_to_none_on_degrade_path():
    """두 키가 없는 구 호출부도 KeyError 없이 None — degrade 경로에서도 동일."""
    ri = {"meta": {"product_name": "P1", "product_type": "PMIC", "revision": 0.0,
                   "lot_id": "L1", "wafer_number": 1, "family_product": "SOC"},
          "items": [{"item_name": "BUCK_SCAN", "bin": 40, "unit": "PF",
                     "yield": 0.3, "fail_count": 196, "total_count": 280,
                     "lsl": None, "usl": None}]}
    cases = ingest.ingest(ri, persist=False)["cases"]
    assert cases
    assert all(c["session_id"] is None and c["analysis_key"] is None for c in cases)


# --- 측정값 없는 item 의 공간 룰 발화 (2026-08-28) ---------------------------------
# 사용자 요구: "공간 Fail 은 해당 Item 의 Rawdata 가 없어도 FAILTNO 는 있으니
# FAILTNO 기준으로 Signature 발화하게 해달라". 이 부류가 깨지는 방식은 조용하다 —
# 에러가 아니라 "Yield 행은 있는데 AI Comment 만 없다" 로 나타난다.

def _edge_df(n_edge_fail=14, n_pass=60, blank_values=True):
    """E1(최외곽 1열)에 fail 이 몰린 df. blank_values=True 면 측정 셀을 전부 비운다.

    좌표는 격자 10x10 (XPOS/YPOS 는 항상 양수 — CLAUDE.md 불변규칙 9).
    E1 = 각 행의 좌·우 끝 + 각 열의 위·아래 끝(features._e1_mask) 이므로
    경계 좌표(1 또는 10)를 가진 die 를 fail 로 만든다.
    """
    nan = float("nan")
    blank = "" if blank_values else None
    meta_rows = [
        ["TSEQ", None, None, None, None, None, None, 1, 2],
        ["TNO", None, None, None, None, None, None, _VREF_TNO, _IDDQ_TNO],
        ["STEP", None, None, None, None, None, None, "P2", "P1"],
        # UNIT 을 비우면 엔진이 PF(양불)로 분류한다 — 측정값 없는 functional test 의 모습.
        ["UNIT", None, None, None, None, None, None, "" if blank_values else "V", "A"],
        ["HILIM", None, None, None, None, None, None, nan if blank_values else 1.4, 15.0],
        ["LOLIM", None, None, None, None, None, None, nan if blank_values else 1.0, nan],
    ]
    edge, inner = [], []
    for x in range(1, 11):
        for y in range(1, 11):
            (edge if x in (1, 10) or y in (1, 10) else inner).append((x, y))
    rows, s = [], 1

    def _cell(v):
        return blank if blank_values else v

    for x, y in edge[:n_edge_fail]:                    # E1 fail
        rows.append([s, 1, s, x, y, 18, _VREF_TNO, _cell(1.55), 12.1]); s += 1
    for x, y in inner[:n_pass]:                        # 내부 pass
        rows.append([s, 1, s, x, y, 1, nan, _cell(1.20), 12.0]); s += 1
    return pd.DataFrame(meta_rows + rows, columns=_COLS)


def test_blank_value_item_still_makes_case_with_spatial_axis():
    """측정값이 전부 빈 item 도 case 가 생기고, 공간 축이 **전체 die** 로 실린다."""
    df = _edge_df()
    cases = ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)["cases"]
    vref = next(c for c in cases if c["item_raw"] == "VREF_TRIM")
    assert vref["values"] == [], "측정 셀이 비었으므로 측정값 축은 비어야 한다"
    assert vref["fail_count"] == 14, "fail 은 FAILTNO 로 세므로 측정값과 무관하다"
    # 공간 축은 전체 die(74행) 기준 — 측정값 축(0)과 길이가 다르다.
    assert len(vref["spatial_x_pos"]) == 74
    assert sum(1 for f in vref["spatial_fail_mask"] if f) == 14


def test_blank_value_item_fires_spatial_signature():
    """★ 핵심 회귀 — 측정값이 없어도 E1_FAIL 이 발화한다(FAILTNO+좌표만으로 판정).

    종전에는 ingest 가 `if not values: continue` 로 case 를 버려 룰이 평가될 기회조차
    없었다. 화면에서는 에러가 아니라 'AI Comment 만 비어 있음' 으로 보인다.
    """
    from eval_engine.pipeline import features as feat, metrics as met, signatures as sig
    from eval_engine.pipeline._rules import thresholds_for

    df = _edge_df()
    cases = ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)["cases"]
    vref = next(c for c in cases if c["item_raw"] == "VREF_TRIM")
    assert vref["value_type"] == "PF", "UNIT 이 비면 PF 로 분류된다"

    rm = met.compute(vref)
    assert rm["fail_count"] == 14, "fail 은 FAILTNO 로 센다"
    assert rm["yield"] is not None, '측정값이 없어도 yield 는 FAILTNO 로 나와야 한다(PF trump 가 여기 의존)'
    f = feat.compute(vref, rm, "test")
    assert f["e1_fail_share"] is not None, "공간 feature 가 비면 룰이 평가되지 않는다"
    assert f["e1_fail_share"] >= thresholds_for(vref)["region_fail_share_min"]

    ids = {s["id"] for s in sig.evaluate(vref, f, rm)["signatures"]}
    assert "E1_FAIL" in ids, f"측정값 없는 item 에서 공간 룰이 안 떴다: {ids}"


def test_spatial_axis_uses_all_dies_not_just_measured():
    """측정값이 **일부만** 있는 item 도 공간 판정 모집단은 전체 die 다.

    사용자 결정(2026-08-28): 전체를 FAILTNO 기준으로 통일. 측정된 die 만 세면
    fail 이 난 die 의 값이 비었을 때 그 fail 이 공간 판정에서 통째로 빠진다.
    """
    df = _edge_df(blank_values=False)
    # fail die(앞 14행)의 측정값만 지운다 — 종전 규칙이면 이 fail 들이 전부 사라진다.
    df.loc[6:19, "VREF_TRIM"] = float("nan")
    cases = ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)["cases"]
    vref = next(c for c in cases if c["item_raw"] == "VREF_TRIM")
    assert not any(vref["fail_mask"]), "측정값 축에는 fail 이 하나도 안 남는다(값이 지워짐)"
    assert sum(1 for f in vref["spatial_fail_mask"] if f) == 14, \
        "공간 축은 측정값과 무관하게 fail 14개를 그대로 본다"

    from eval_engine.pipeline import features as feat, metrics as met
    f = feat.compute(vref, met.compute(vref), "test")
    assert f["e1_fail_share"] == pytest.approx(1.0), \
        "공간 판정이 측정값 축을 타면 여기서 None 이 된다"


def test_measured_axis_survives_partial_missing():
    """★ 안전망 — 부분결측이 있어도 **측정값 기반 지표가 죽지 않는다**.

    공간 축을 전체 die 로 돌리면서 측정값 축까지 함께 늘리면
    `_fail_outlier_features`/`_fail_body_jump_ratio`/`_pass_limit_hit_ratio` 의 길이 가드
    (`fm.size != v.size -> None`)에 걸려 OUTLIER 판정 2축이 **조용히** 사라진다.
    두 축을 갈라 둔 이유가 이것이므로, 그 계약을 여기서 고정한다.
    """
    from eval_engine.pipeline import features as feat, metrics as met

    df = _edge_df(blank_values=False)
    df.loc[30:39, "VREF_TRIM"] = float("nan")     # pass die 일부만 측정 실패
    cases = ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)["cases"]
    vref = next(c for c in cases if c["item_raw"] == "VREF_TRIM")

    assert len(vref["values"]) == 64 and len(vref["fail_mask"]) == 64, "측정값 축"
    assert len(vref["spatial_fail_mask"]) == 74, "공간 축은 전체 die"

    f = feat.compute(vref, met.compute(vref), "test")
    for key in ("fail_mad_min", "fail_body_jump_ratio", "pass_limit_hit_ratio"):
        assert f[key] is not None, f"{key} 가 죽었다 - 길이 가드에 걸렸다는 뜻"
    assert f["e1_fail_share"] == pytest.approx(1.0), "공간은 전체 die 기준"


def test_geom_shared_attached_even_with_partial_missing():
    """좌표 전처리 공유통은 이제 **항상** 붙는다 — 공간 축이 늘 소스 공용 좌표라서.

    종전에는 부분결측 item 만 제외됐고, 그 item 들은 중심정렬·반경·E1 마스크를 매번
    다시 만들었다(콜드 평가의 10%). 공유가 끊기면 조용히 느려질 뿐이라 테스트로 고정한다.
    """
    df = _edge_df(blank_values=False)
    df.loc[30:39, "VREF_TRIM"] = float("nan")
    cases = ingest.ingest({"meta": _meta(), "raw_df": df}, persist=False)["cases"]
    assert all("_geom_shared" in c for c in cases)
    shared = {id(c["_geom_shared"]) for c in cases}
    assert len(shared) == 1, "한 소스의 item 들은 같은 공유통을 봐야 한다"

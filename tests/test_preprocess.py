"""조회 전처리(항목 제외 · outlier 마스킹 · 셀 패치 · 조건 일괄 규칙) 테스트.

실행:
    python tests/test_preprocess.py

핵심 회귀 기준은 **"옵션이 없으면 도입 전과 완전히 같다"** 이다:
  - preprocess.digest({}) == "" 이고 cache_policy 각 빌더가 종전과 동일한 튜플을 낸다
  - apply_tables 가 입력 객체를 그대로 돌려준다 (비용 0)
  - **레거시 spec(항목 제외/outlier)의 정규형·digest 가 패치 계층 도입 전과 문자 그대로 동일**
    (여기가 깨지면 배포 순간 기존 세션의 tables/dist 캐시가 통째로 무효화된다)
그 위에 실제 동작(마스킹 대상/미대상, 수율 불변, 되돌리기, 셀 패치·규칙 적용)을 확인한다.

pytest 미사용(그건 eval_analyzer 전용) — 자체 실행 + assert 스타일(web_report tests/ 관례).
"""
from __future__ import annotations

import json
import math
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report import cache_policy, preprocess  # noqa: E402
from web_report.honeyform import META_COLUMNS, split_honeyform  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402
from web_report.tabs.common import fmt_type  # noqa: E402

# ItemA 는 10 근처 값 9개 + 극단값 1개(1000) → k=2 에서 그 1건만 제거된다.
# (표본이 작고 극단값이 하나면 그 값이 mean/σ 를 끌어올려 k 를 크게 잡을수록 안 걸린다 —
#  mean=109, σ≈313 이라 k=3 이면 임계 939 > |1000-109|=891 로 제거되지 않는다. k 의 의미가
#  "몇 σ 밖을 자를 것인가" 라는 사용자 정의 그대로임을 보이는 값 선택이다.)
# ItemB 는 전부 같은 값(std=0) → 어떤 k 에서도 제거 대상이 없다.
_A_VALUES = [10, 11, 9, 10, 12, 8, 10, 11, 9, 1000]
_B_VALUES = [5] * 10
_K_REMOVES_ONE = 2


def make_table():
    """합성 honeyform 테이블 1개 (호출마다 fresh — 소비자가 item_columns 를 in-place 변형)."""
    cols = META_COLUMNS + ["ItemA", "ItemB"]
    rows = [
        ["TSEQ", "", "", "", "", "", "", 1, 2],
        ["TNO", "", "", "", "", "", "", 100, 200],
        ["STEP", "", "", "", "", "", "", "P1", "P1"],
        ["UNIT", "", "", "", "", "", "", "V", "V"],
        ["HILIM", "", "", "", "", "", "", 2000, 10],
        ["LOLIM", "", "", "", "", "", "", 0, 0],
    ]
    for i, (a, b) in enumerate(zip(_A_VALUES, _B_VALUES)):
        # 마지막 die 만 fail(BIN 5) — 수율이 전처리로 바뀌지 않는지 확인용
        bin_code = 5 if i == len(_A_VALUES) - 1 else 1
        failtno = 100 if bin_code == 5 else ""
        rows.append([f"s{i}", 1, 1, i, 0, bin_code, failtno, a, b])
    df = pd.DataFrame(rows, columns=cols)
    return split_honeyform(df, source="src0", file_name="src0")


def _canon(payload):
    p = dict(payload)
    p.pop("selected_items", None)
    return json.dumps(p, sort_keys=True, ensure_ascii=False, default=str)


# ── 무회귀 (옵션 없음) ───────────────────────────────────────────────────────
def test_empty_spec_is_noop():
    """전처리가 없으면 digest 는 빈 문자열이고 tables 는 입력 객체 그대로."""
    for spec in ({}, None, {"exclude_items": []}, {"outlier": {"k": 0}},
                 {"outlier": {"k": "abc"}}, {"outlier": {"mode": "iqr", "k": 3}}):
        assert preprocess.normalize(spec) == {}, f"정규화 실패: {spec!r}"
        assert preprocess.digest(spec) == "", f"digest 가 비어있지 않음: {spec!r}"

    tables = [make_table()]
    out, stats = preprocess.apply_tables(tables, {})
    assert out is tables, "빈 spec 인데 tables 를 새로 만들었다 (불필요 비용)"
    assert stats["removed_total"] == 0


def test_cache_keys_unchanged_without_preprocess():
    """prep_digest 가 빈 문자열이면 모든 캐시 키가 도입 전 튜플과 동일해야 한다."""
    session = {"analysis_key": "AKEY", "content_hash": "CHASH", "mode": "Normal",
               "webreport_options": ""}
    assert cache_policy.tables_key(session, "") == ("AKEY", "CHASH")
    assert cache_policy.tables_key(session) == ("AKEY", "CHASH")
    assert cache_policy.dist_key(session) == ("AKEY", "CHASH", "Normal")
    assert cache_policy.dist_key(session, bin1=True) == ("AKEY", "CHASH", "Normal", "bin1")
    assert cache_policy.map_key(session) == ("AKEY", "CHASH", "Normal",
                                             cache_policy.MAP_SCHEMA_VERSION)
    assert cache_policy.scatter_key(session, "ItemA") == ("AKEY", "CHASH", "Normal", "ItemA")
    assert cache_policy.dist_batch_key(session, "D") == ("AKEY", "CHASH", "Normal", "D")
    assert cache_policy.trim_chart_key(session, "src0", "D") == (
        "AKEY", "CHASH", "Normal", "src0", "D")
    # rev 를 가진 키에는 prep 을 넣지 않는다 (rev 가 같은 역할)
    assert cache_policy.report_key(session, "sid", 3) == (
        "AKEY", "CHASH", "sid", 3, "", "Normal", cache_policy.REPORT_SCHEMA_VERSION)


def test_cache_keys_split_with_preprocess():
    """전처리가 있으면 (akey, chash) 뒤에 digest 가 끼어 옛 캐시와 분리된다."""
    session = {"analysis_key": "AKEY", "content_hash": "CHASH", "mode": "Normal"}
    prep = preprocess.digest({"outlier": {"k": 3}})
    assert prep and cache_policy.tables_key(session, prep) == ("AKEY", "CHASH", prep)
    assert cache_policy.dist_key(session, prep_digest=prep) == (
        "AKEY", "CHASH", prep, "Normal")
    assert cache_policy.tables_key(session, prep) != cache_policy.tables_key(session)


# ── outlier 마스킹 ───────────────────────────────────────────────────────────
def test_outlier_masks_only_extreme_values():
    """ItemA 의 극단값 1건만 결측이 되고 나머지·ItemB 는 그대로."""
    out, stats = preprocess.apply_tables([make_table()],
                                         {"outlier": {"k": _K_REMOVES_ONE}})
    data = out[0].data
    assert stats["removed"] == {"ItemA": 1}, f"제거 대상이 다르다: {stats['removed']}"
    assert stats["removed_total"] == 1
    a = list(data["ItemA"])
    assert math.isnan(a[-1]), "극단값(1000)이 결측 처리되지 않았다"
    assert a[:-1] == _A_VALUES[:-1], f"정상 값이 변경됨: {a[:-1]}"
    assert list(data["ItemB"]) == _B_VALUES, "std=0 항목은 건드리면 안 된다"
    # 행·메타는 불변 → 수율/맵 영향 없음
    assert len(data) == len(_A_VALUES)
    assert list(data["BIN"]) == list(make_table().data["BIN"])


def test_outlier_large_k_removes_nothing():
    """k 가 크면(사용자 기본 50) 제거 대상이 없어 프레임이 그대로다."""
    table = make_table()
    out, stats = preprocess.apply_tables([table], {"outlier": {"k": 50}})
    assert stats["removed_total"] == 0
    assert out[0].data is table.data, "변경이 없는데 프레임을 새로 만들었다"


def test_outlier_yield_unchanged():
    """outlier 제거는 CPK/분포만 바꾸고 수율(BIN 기반)은 바꾸지 않는다."""
    base = build_report_payload([make_table()])
    masked = build_report_payload(
        preprocess.apply_tables([make_table()], {"outlier": {"k": _K_REMOVES_ONE}})[0])
    assert base["yield_summary"] == masked["yield_summary"], "수율이 전처리로 변했다"
    assert base["sheets"]["Yield"] == masked["sheets"]["Yield"], "Yield 시트가 변했다"


# ── 항목 제외 ────────────────────────────────────────────────────────────────
def test_exclude_items_removes_from_report():
    """제외한 항목은 item_columns 에서만 빠진다 — 메타/data 는 유지 (Yield 정합).

    메타(tno/step)까지 지우면 Yield 의 fail 집계(전체 table.tno 기준)가 그 항목의 fail die 를
    잃어 표 행 합과 수율이 어긋난다. manifest.selected_items 필터와 같은 의미론이다
    (tests/test_yield_step_selected_items.py 가 그 동작을 고정한다)."""
    out, _ = preprocess.apply_tables([make_table()], {"exclude_items": ["ItemB"]})
    table = out[0]
    assert table.item_columns == ["ItemA"]
    assert "ItemB" in table.data.columns, "data 컬럼까지 지우면 안 된다"
    assert "ItemB" in table.tno and "ItemB" in table.step, "메타까지 지우면 Yield 가 깨진다"
    payload = build_report_payload(out)
    items = {row.get("subject") for row in payload.get("distribution_index") or []}
    assert items == {"ItemA"}, f"제외가 리포트에 반영되지 않았다: {items}"


def test_exclude_is_reversible():
    """spec 을 비우면 원래 payload 와 정준 JSON 이 완전히 일치한다 (되돌리기)."""
    before = _canon(build_report_payload([make_table()]))
    excluded, _ = preprocess.apply_tables([make_table()], {"exclude_items": ["ItemB"]})
    assert _canon(build_report_payload(excluded)) != before, "제외가 payload 에 반영되지 않음"
    restored, _ = preprocess.apply_tables([make_table()], {})
    assert _canon(build_report_payload(restored)) == before, "해제 후 원래 값으로 안 돌아옴"


# ── 편집 DB 왕복 ─────────────────────────────────────────────────────────────
class FakeEditDB:
    """report_webreport_edit 최소 구현 (kind/item_key/value + rev)."""

    def __init__(self):
        self.rows = {}
        self.rev = 0

    def get_webreport_edits(self, session_id, kinds=None, exclude_kinds=None):
        out = []
        for (kind, key), value in self.rows.items():
            if kinds and kind not in kinds:
                continue
            if exclude_kinds and kind in exclude_kinds:
                continue
            out.append({"kind": kind, "item_key": key, "value": value})
        return out

    def apply_webreport_edits(self, session_id, changes, updated_by=None):
        if not changes:
            return self.rev
        for kind, key, value in changes:
            if value is None:
                self.rows.pop((kind, key), None)
            else:
                self.rows[(kind, key)] = str(value)
        self.rev += 1
        return self.rev


def test_edits_roundtrip_and_clear():
    """저장 → 조회 → 해제(빈 spec = 행 삭제)가 왕복하고 rev 가 증가한다."""
    from web_report import edits

    db = FakeEditDB()
    assert edits.load_preprocess(db, "sid") == {}

    rev = edits.save_preprocess(db, "sid", {"exclude_items": ["B", "A"],
                                            "outlier": {"k": 50}})
    assert rev == 1
    loaded = edits.load_preprocess(db, "sid")
    assert loaded == {"exclude_items": ["A", "B"], "outlier": {"mode": "stdev", "k": 50.0}}
    assert preprocess.digest(loaded) == preprocess.digest({"exclude_items": ["A", "B"],
                                                           "outlier": {"k": 50}})

    rev = edits.save_preprocess(db, "sid", {})
    assert rev == 2 and edits.load_preprocess(db, "sid") == {}
    assert not db.rows, f"해제했는데 편집행이 남았다: {db.rows}"


def test_preprocess_kind_excluded_from_edit_state():
    """전처리 행이 표 상태(load_edit_state) 조회에 섞이지 않는다."""
    from web_report import edits

    db = FakeEditDB()
    edits.save_preprocess(db, "sid", {"outlier": {"k": 50}})
    state = edits.load_edit_state(db, "sid")
    assert state["etc_items"] == [] and state["issue_comments"] == {}, state


def test_preprocessed_table_has_no_df():
    """전처리 산출물은 재인코딩에 쓰이면 안 되므로 df 가 비어 있어야 한다."""
    out, _ = preprocess.apply_tables([make_table()], {"outlier": {"k": _K_REMOVES_ONE}})
    assert out[0].df is None


# ── 셀 패치 (edits) ──────────────────────────────────────────────────────────
def test_legacy_spec_normal_form_is_frozen():
    """패치 계층 도입 전 spec 의 정규형·digest 가 문자 그대로 같아야 한다.

    digest 가 바뀌면 배포 즉시 기존 전처리 세션의 tables/dist/map 캐시가 전부 콜드가 되고
    Distribution pack variant 도 헛돈다 — 아래 hex 는 패치 계층 도입 전 커밋(HEAD)의
    preprocess.py 로 직접 산출해 대조한 값이다."""
    legacy = {"exclude_items": ["B", "A"], "outlier": {"mode": "stdev", "k": 50}}
    assert preprocess.normalize(legacy) == {
        "exclude_items": ["A", "B"], "outlier": {"mode": "stdev", "k": 50.0}}
    assert preprocess.digest(legacy) == "bbcc680289a5", preprocess.digest(legacy)
    assert preprocess.digest({"outlier": {"k": 3}}) == "317b562d209e"


def test_edits_patch_values_and_keep_dtype():
    """셀 패치가 item/메타에 반영되고, 정수만 들어오면 int dtype 을 유지한다."""
    out, stats = preprocess.apply_tables([make_table()], {"edits": [
        {"source": "src0", "row_idx": 0, "column": "ItemA", "value": "3.5"},
        {"source": "src0", "row_idx": 1, "column": "BIN", "value": "7"},
        {"source": "src0", "row_idx": 2, "column": "ItemB", "value": ""},   # 결측 처리
    ]})
    data = out[0].data
    assert stats["edited_cells"] == 3
    assert abs(float(data["ItemA"][0]) - 3.5) < 1e-9
    assert fmt_type(data["BIN"][1]) == "7"
    assert math.isnan(float(data["ItemB"][2])), "빈값은 결측이어야 한다"
    # 손대지 않은 값은 그대로
    assert list(data["ItemA"])[1:] == _A_VALUES[1:]

    ints, _ = preprocess.apply_tables([make_table()], {"edits": [
        {"source": "src0", "row_idx": 0, "column": "ItemA", "value": "7"}]})
    assert ints[0].data["ItemA"].dtype.kind == "i", "정수만 넣었는데 dtype 이 넓어졌다"


def test_edits_ignore_unknown_targets():
    """없는 source/컬럼·범위 밖 row_idx 는 조회를 죽이지 않고 조용히 무시된다.

    (저장 시점에는 service._check_edit_targets 가 400 으로 막는다 — 여기는 원본이
    Excel 왕복으로 줄어든 뒤 남아 있던 패치를 만난 조회 경로의 방어선이다.)"""
    out, stats = preprocess.apply_tables([make_table()], {"edits": [
        {"source": "nope", "row_idx": 0, "column": "ItemA", "value": "1"},
        {"source": "src0", "row_idx": 999, "column": "ItemA", "value": "1"},
        {"source": "src0", "row_idx": 0, "column": "NoSuchItem", "value": "1"},
    ]})
    assert stats["edited_cells"] == 0
    assert list(out[0].data["ItemA"]) == _A_VALUES


def test_edits_digest_is_order_independent():
    """같은 편집 집합이면 클라가 보낸 순서와 무관하게 같은 digest (캐시 헛돌기 방지)."""
    a = {"edits": [{"source": "b", "row_idx": 2, "column": "X", "value": "1"},
                   {"source": "a", "row_idx": 1, "column": "Y", "value": "2"}]}
    b = {"edits": [{"source": "a", "row_idx": 1, "column": "Y", "value": "2"},
                   {"source": "b", "row_idx": 2, "column": "X", "value": "1"}]}
    assert preprocess.digest(a) == preprocess.digest(b)
    # 같은 셀을 두 번 고치면 마지막 값만 남는다
    dup = preprocess.normalize({"edits": [
        {"source": "a", "row_idx": 1, "column": "Y", "value": "1"},
        {"source": "a", "row_idx": 1, "column": "Y", "value": "9"}]})
    assert dup["edits"] == [{"source": "a", "row_idx": 1, "column": "Y", "value": "9"}]


# ── 조건 일괄 규칙 (rules) ───────────────────────────────────────────────────
def _rule(conds, action, source=None):
    where = {"conds": conds}
    if source:
        where["source"] = source
    return {"where": where, "action": action}


def test_rule_exclude_rows_bin1_only():
    """BIN ∉ [1] → die 제외 = 'Bin1 only'. 행이 실제로 사라지고 수율이 100% 가 된다."""
    spec = {"rules": [_rule([{"field": "BIN", "op": "not_in", "values": ["1"]}],
                            {"op": "exclude_rows"})]}
    out, stats = preprocess.apply_tables([make_table()], spec)
    assert stats["excluded_dies"] == 1
    assert len(out[0].data) == len(_A_VALUES) - 1
    assert set(fmt_type(v) for v in out[0].data["BIN"]) == {"1"}


def test_rule_spec_out_clear():
    """HILIM 밖 측정값만 결측 처리 (ItemA HILIM=2000 이므로 여기선 미적중, ItemB=10)."""
    spec = {"rules": [_rule([{"field": "item", "item": "ItemB", "op": "spec_out"}],
                            {"op": "clear", "target": "ItemB"})]}
    out, stats = preprocess.apply_tables([make_table()], spec)
    assert stats["rule_hits"] == 0, "규격 안 값이 규격 밖으로 잡혔다"
    # ItemA 는 1000 하나가 HILIM(2000) 안이라 미적중 → LOLIM 밖 조건으로 바꿔 확인
    spec = {"rules": [_rule([{"field": "item", "item": "ItemA", "op": ">", "value": 100}],
                            {"op": "clear", "target": "ItemA"})]}
    out, stats = preprocess.apply_tables([make_table()], spec)
    assert stats["rule_hits"] == 1 and math.isnan(float(out[0].data["ItemA"].iloc[-1]))


def test_rule_and_conds_with_offset():
    """한 규칙 안 조건은 AND — DUT==1 이면서 ItemA>10 인 행만 -1."""
    spec = {"rules": [_rule([{"field": "DUT", "op": "in", "values": ["1"]},
                             {"field": "item", "item": "ItemA", "op": ">", "value": 10}],
                            {"op": "offset", "target": "ItemA", "value": -1})]}
    out, stats = preprocess.apply_tables([make_table()], spec)
    got = list(out[0].data["ItemA"])
    # DUT 는 make_table 에서 전 행 1 → ItemA>10 인 11,12,11,1000 네 건이 대상
    assert stats["rule_hits"] == 4
    assert [g for g, o in zip(got, _A_VALUES) if g != o] == [10.0, 11.0, 10.0, 999.0]


def test_rule_scale_and_set():
    """scale(×)·set(동일 값) 동작."""
    spec = {"rules": [_rule([{"field": "BIN", "op": "in", "values": ["5"]}],
                            {"op": "scale", "target": "ItemA", "value": 0.5})]}
    out, _ = preprocess.apply_tables([make_table()], spec)
    assert abs(float(out[0].data["ItemA"].iloc[-1]) - 500.0) < 1e-9

    spec = {"rules": [_rule([{"field": "BIN", "op": "in", "values": ["5"]}],
                            {"op": "set", "target": "BIN", "value": "9"})]}
    out, _ = preprocess.apply_tables([make_table()], spec)
    assert fmt_type(out[0].data["BIN"].iloc[-1]) == "9"


def test_rule_source_scope():
    """where.source 가 다른 소스면 그 테이블에는 적용되지 않는다."""
    spec = {"rules": [_rule([{"field": "BIN", "op": "in", "values": ["5"]}],
                            {"op": "exclude_rows"}, source="other")]}
    out, stats = preprocess.apply_tables([make_table()], spec)
    assert stats["excluded_dies"] == 0 and len(out[0].data) == len(_A_VALUES)


def test_rule_numeric_cond_skips_missing():
    """결측(NaN)은 숫자 비교 어느 쪽에도 걸리지 않는다."""
    spec = {"edits": [{"source": "src0", "row_idx": 0, "column": "ItemA", "value": ""}],
            "rules": [_rule([{"field": "item", "item": "ItemA", "op": "<", "value": 1e9}],
                            {"op": "clear", "target": "ItemB"})]}
    out, stats = preprocess.apply_tables([make_table()], spec)
    assert stats["rule_hits"] == len(_A_VALUES) - 1, "결측 행이 비교에 걸렸다"


def test_apply_order_edits_then_rules():
    """규칙은 셀 패치가 반영된 값 위에서 평가된다 (① edits → ② rules)."""
    spec = {"edits": [{"source": "src0", "row_idx": 0, "column": "BIN", "value": "9"}],
            "rules": [_rule([{"field": "BIN", "op": "in", "values": ["9"]}],
                            {"op": "exclude_rows"})]}
    out, stats = preprocess.apply_tables([make_table()], spec)
    assert stats["edited_cells"] == 1 and stats["excluded_dies"] == 1
    assert len(out[0].data) == len(_A_VALUES) - 1


def test_rules_are_reversible():
    """규칙을 지우면 원래 payload 와 정준 JSON 이 완전히 일치한다 (되돌리기)."""
    before = _canon(build_report_payload([make_table()]))
    spec = {"rules": [_rule([{"field": "BIN", "op": "not_in", "values": ["1"]}],
                            {"op": "exclude_rows"})]}
    patched, _ = preprocess.apply_tables([make_table()], spec)
    assert _canon(build_report_payload(patched)) != before, "규칙이 payload 에 반영 안 됨"
    restored, _ = preprocess.apply_tables([make_table()], {})
    assert _canon(build_report_payload(restored)) == before, "해제 후 원래 값으로 안 돌아옴"


def test_bad_rules_are_dropped():
    """해석 불가 규칙은 정규화에서 사라진다 (조회 경로를 죽이지 않는다)."""
    for bad in ([{"where": {"conds": []}, "action": {"op": "exclude_rows"}}],   # 조건 없음
                [_rule([{"field": "NOPE", "op": "in", "values": ["1"]}],
                       {"op": "exclude_rows"})],                                # 없는 필드
                [_rule([{"field": "BIN", "op": "??", "values": ["1"]}],
                       {"op": "exclude_rows"})],                                # 없는 연산
                [_rule([{"field": "BIN", "op": "in", "values": ["1"]}],
                       {"op": "clear"})],                                       # target 없음
                [_rule([{"field": "BIN", "op": "spec_out"}],
                       {"op": "exclude_rows"})]):                               # 메타 spec_out
        assert preprocess.normalize({"rules": bad}) == {}, bad


def test_describe_covers_patch_layers():
    spec = {"exclude_items": ["ItemB"],
            "edits": [{"source": "src0", "row_idx": 0, "column": "ItemA", "value": "1"}],
            "rules": [_rule([{"field": "BIN", "op": "not_in", "values": ["1"]}],
                            {"op": "exclude_rows"})]}
    text = preprocess.describe(spec)
    assert "항목 1개 제외" in text and "셀 수정 1건" in text and "일괄 규칙 1건" in text
    assert preprocess.describe_rule(spec["rules"][0]) == "BIN ∉ [1] → die 제외"


def test_drop_preprocess_edits_keeps_rules():
    """원본 교체 시 셀 패치만 해제되고 규칙·항목 제외는 남는다."""
    from web_report import edits

    db = FakeEditDB()
    spec = {"exclude_items": ["ItemB"],
            "edits": [{"source": "src0", "row_idx": 0, "column": "ItemA", "value": "1"}],
            "rules": [_rule([{"field": "BIN", "op": "not_in", "values": ["1"]}],
                            {"op": "exclude_rows"})]}
    edits.save_preprocess(db, "sid", spec)
    dropped = edits.drop_preprocess_edits(db, ["sid", "sid2"])
    assert dropped == 1
    left = edits.load_preprocess(db, "sid")
    assert "edits" not in left
    assert left["exclude_items"] == ["ItemB"] and len(left["rules"]) == 1


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = [
        test_empty_spec_is_noop,
        test_cache_keys_unchanged_without_preprocess,
        test_cache_keys_split_with_preprocess,
        test_outlier_masks_only_extreme_values,
        test_outlier_large_k_removes_nothing,
        test_outlier_yield_unchanged,
        test_exclude_items_removes_from_report,
        test_exclude_is_reversible,
        test_edits_roundtrip_and_clear,
        test_preprocess_kind_excluded_from_edit_state,
        test_preprocessed_table_has_no_df,
        test_legacy_spec_normal_form_is_frozen,
        test_edits_patch_values_and_keep_dtype,
        test_edits_ignore_unknown_targets,
        test_edits_digest_is_order_independent,
        test_rule_exclude_rows_bin1_only,
        test_rule_spec_out_clear,
        test_rule_and_conds_with_offset,
        test_rule_scale_and_set,
        test_rule_source_scope,
        test_rule_numeric_cond_skips_missing,
        test_apply_order_edits_then_rules,
        test_rules_are_reversible,
        test_bad_rules_are_dropped,
        test_describe_covers_patch_layers,
        test_drop_preprocess_edits_keeps_rules,
    ]
    for fn in checks:
        fn()
    print(f"PASS: test_preprocess ({len(checks)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

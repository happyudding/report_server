"""조회 전처리(항목 제외 + outlier 마스킹) 테스트.

실행:
    python tests/test_preprocess.py

핵심 회귀 기준은 **"옵션이 없으면 도입 전과 완전히 같다"** 이다:
  - preprocess.digest({}) == "" 이고 cache_policy 각 빌더가 종전과 동일한 튜플을 낸다
  - apply_tables 가 입력 객체를 그대로 돌려준다 (비용 0)
그 위에 실제 동작(마스킹 대상/미대상, 수율 불변, 되돌리기)을 확인한다.

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
    """제외한 항목이 item_columns/data/메타에서 빠지고 리포트에도 나오지 않는다."""
    out, _ = preprocess.apply_tables([make_table()], {"exclude_items": ["ItemB"]})
    table = out[0]
    assert table.item_columns == ["ItemA"]
    assert "ItemB" not in table.data.columns
    assert "ItemB" not in table.units and "ItemB" not in table.tno
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
    ]
    for fn in checks:
        fn()
    print(f"PASS: test_preprocess ({len(checks)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

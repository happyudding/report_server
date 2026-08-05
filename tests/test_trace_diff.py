"""트레이스 전후 비교(_trace_diff) + 직전 run 조회(trace_store) 검증.

실행:
    python tests/test_trace_diff.py

검증:
  (a) _trace_diff — status/primary/stored/발화집합 변화만 changed 로, 케이스 증감은
      added/removed, 변화 없으면 전부 빈 목록
  (b) 같은 (source_index, item, bin) 키가 중복되면 비교에서 제외(오보 방지)
  (c) trace_store.latest_for_session — 같은 세션 최신 run 반환, 타 세션 무시,
      TTL 지난 run 제외

트레이스 자체는 세션·parquet 이 필요하므로 합성 케이스로 비교 로직만 검증한다.
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "server"))

from eval_panel import routes, trace_store        # noqa: E402


def case(item, bin_, status, primary, stored, fired, source_index=0):
    return {"item_raw": item, "bin": bin_, "status": status, "stored": stored,
            "primary_signature": primary, "source_index": source_index,
            "signature_matrix": [{"id": s, "fired": True} for s in fired]
                                + [{"id": "NOISE", "fired": False}]}


def main():
    base = [
        case("VOUT", 18, "MAJOR", "SEVERE_OUTLIER", True, ["SEVERE_OUTLIER", "OUTLIER_WARN"]),
        case("IDD", 31, "OK", None, False, []),
        case("VREF", 5, "MINOR", "SPEC_TOO_TIGHT", True, ["SPEC_TOO_TIGHT"]),
    ]

    # (a) 변화 없음 ─────────────────────────────────────────────────────────
    same = routes._trace_diff(base, list(base))
    assert same["changed"] == [] and same["added"] == [] and same["removed"] == [], same
    assert same["compared"] == 3, same

    # status + 발화 변화 / 케이스 삭제 / 케이스 추가
    after = [
        case("VOUT", 18, "MINOR", "OUTLIER_WARN", True, ["OUTLIER_WARN"]),   # 변화
        case("IDD", 31, "OK", None, False, []),                              # 그대로
        case("NEWITEM", 7, "MAJOR", "LOW_CPK", True, ["LOW_CPK"]),           # 추가
    ]
    diff = routes._trace_diff(base, after)
    assert len(diff["changed"]) == 1, diff["changed"]
    ch = diff["changed"][0]
    assert ch["item_raw"] == "VOUT" and ch["idx"] == 0, ch
    assert ch["old"]["status"] == "MAJOR" and ch["new"]["status"] == "MINOR", ch
    assert ch["fired_removed"] == ["SEVERE_OUTLIER"] and ch["fired_added"] == [], ch
    assert [a["item_raw"] for a in diff["added"]] == ["NEWITEM"], diff["added"]
    assert [r["item_raw"] for r in diff["removed"]] == ["VREF"], diff["removed"]

    # stored 만 바뀌어도 changed
    only_stored = [dict(base[0], stored=False), base[1], base[2]]
    assert len(routes._trace_diff(base, only_stored)["changed"]) == 1

    # 발화가 늘어난 경우
    more_fired = [case("VOUT", 18, "MAJOR", "SEVERE_OUTLIER", True,
                       ["SEVERE_OUTLIER", "OUTLIER_WARN", "LOW_CPK"]), base[1], base[2]]
    ch2 = routes._trace_diff(base, more_fired)["changed"][0]
    assert ch2["fired_added"] == ["LOW_CPK"] and ch2["fired_removed"] == [], ch2

    # (b) 중복 키는 비교 제외 ────────────────────────────────────────────────
    dup = [base[0], case("VOUT", 18, "OK", None, False, [])]
    assert routes._trace_diff(dup, dup)["compared"] == 0, "중복 키가 비교에 남았다"
    # 중복은 added/removed 로도 새지 않는다
    d = routes._trace_diff(dup, [base[1]])
    assert d["added"] == [] or [a["item_raw"] for a in d["added"]] == ["IDD"], d
    assert d["removed"] == [], d

    # (c) latest_for_session ────────────────────────────────────────────────
    trace_store._runs.clear()
    trace_store.put("S1-100", {"session_id": "S1", "cases": [], "max_cases": 400})
    trace_store.put("S2-101", {"session_id": "S2", "cases": [], "max_cases": 400})
    trace_store.put("S1-102", {"session_id": "S1", "cases": [1], "max_cases": None})

    got = trace_store.latest_for_session("S1")
    assert got is not None and got[0] == "S1-102", got
    assert trace_store.latest_for_session("S9") is None
    assert trace_store.latest_for_session("S2")[0] == "S2-101"

    # TTL 지난 run 은 제외 — 저장 시각을 과거로 밀어 확인
    stale_ts = time.time() - trace_store.TTL_SECONDS - 10
    trace_store._runs["S1-102"] = (stale_ts, trace_store._runs["S1-102"][1])
    trace_store._runs["S1-100"] = (stale_ts, trace_store._runs["S1-100"][1])
    assert trace_store.latest_for_session("S1") is None, "TTL 만료 run 이 반환됐다"

    print("PASS: test_trace_diff (a/b/c)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

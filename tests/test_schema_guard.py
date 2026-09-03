"""schema_guard 자체 회귀 — 가드가 조용히 무력해지는 것을 막는다.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_schema_guard.py

가드는 **틀린 것을 잡고 맞는 것을 통과시켜야** 쓸모가 있다. 둘 중 하나만 깨져도
"돌고는 있는데 아무것도 안 잡는" 상태가 되는데, 그건 가드가 없는 것보다 나쁘다
(있다고 믿게 만든다). 그래서 양성·음성을 함께 고정한다.

검증:
  (a) SCHEMA 파싱 — 실제 컬럼이 잡히고, 마이그레이션 ALTER 컬럼도 포함된다
  (b) 양성 — 없는 컬럼(2026-09-02 실제 버그 `uploaded_at`)을 잡는다
  (c) 음성 — 실제 컬럼·파생 필드(_EXTRA_OK)는 통과시킨다
  (d) 면제 주석(`# schema-guard: allow`)이 동작한다
  (e) 현재 코드 전체가 위반 0 (잔여 버그 없음)

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import schema_guard as G  # noqa: E402


def test_schema_parsed():
    cols = G._schema_columns()
    sess = cols.get("report_session") or set()
    assert sess, "SCHEMA 에서 report_session 을 못 읽었다 — 가드가 통째로 무력해진다"
    for c in ("session_id", "analysis_key", "created_at", "content_hash"):
        assert c in sess, f"{c} 가 안 잡혔다: {sorted(sess)[:12]}"
    # 마이그레이션(ALTER TABLE)으로 붙는 컬럼도 정상으로 봐야 오탐이 없다.
    assert "family_family" not in sess          # 없는 건 없다
    assert len(sess) >= 20, f"컬럼이 너무 적게 잡혔다({len(sess)}) — 파서 회귀 의심"
    print(f"  (a) SCHEMA 파싱 OK (report_session {len(sess)}컬럼)")


def _scan_src(text: str):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "probe.py"
        p.write_text(text, encoding="utf-8")
        return G.scan([p])


def test_catches_missing_column():
    # 2026-09-02 실제 버그 그대로 — 이걸 못 잡으면 가드를 만든 이유가 없다.
    hits = _scan_src('raw = session.get("uploaded_at")\n')
    assert len(hits) == 1, f"없는 컬럼을 못 잡았다: {hits}"
    assert hits[0]["key"] == "uploaded_at"
    assert "created_at" in hits[0]["near"] or "updated_at" in hits[0]["near"], \
        f"오타 후보를 제시하지 못했다: {hits[0]['near']}"
    # 오타·폐지 컬럼도 같은 부류다.
    assert _scan_src('x = session.get("analysis_ky")\n'), "오타를 못 잡았다"
    print("  (b) 없는 컬럼·오타 검출 OK")


def test_passes_valid():
    ok = ('a = session.get("created_at")\n'
          'b = session.get("analysis_key")\n'
          'c = session.get("webreport_options")\n'
          'd = session.get("has_password")\n')      # 파생 필드(_EXTRA_OK)
    hits = _scan_src(ok)
    assert not hits, f"정상 컬럼을 오탐했다 — 가드가 무시당하게 된다: {hits}"
    print("  (c) 정상 컬럼·파생 필드 통과 OK")


def test_allow_comment():
    src = 'raw = session.get("uploaded_at")  # schema-guard: allow (외부 dict)\n'
    assert not _scan_src(src), "면제 주석이 안 먹는다"
    print("  (d) 면제 주석 OK")


def test_repo_clean():
    hits = G.scan(G._all_files())
    assert not hits, "현재 코드에 컬럼명 위반이 있다:\n" + G._fmt(hits)
    print("  (e) 저장소 전체 위반 0 OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    test_schema_parsed()
    test_catches_missing_column()
    test_passes_valid()
    test_allow_comment()
    test_repo_clean()
    print("\ntest_schema_guard: 전부 통과")


if __name__ == "__main__":
    main()

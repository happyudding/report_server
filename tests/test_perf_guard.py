"""성능 회귀 가드(tools/perf_guard.py) 자기 검증 — 규칙마다 위반/정상/면제.

실행:
    python tests/test_perf_guard.py
    python tools/perf_guard.py --selftest      (같은 것)

검증 항목:
  (a) 경로 glob (`**` 포함)이 의도대로 매치한다.
  (b) forbid_add 규칙마다 위반 샘플은 잡히고, 정상 샘플은 안 잡힌다.
  (c) 순수 주석 줄은 검사하지 않는다 (문서·설명에 패턴이 나와도 오탐 없음).
  (d) `perf-guard: allow <id>` 면제가 같은 줄·윗줄 모두에서 동작한다.
  (e) forbid_remove 는 삭제된 줄에서만 발화한다.
  (f) require_pair 는 짝 변경이 없을 때만 발화한다.
  (g) require_import 는 파일이 cache_policy 를 쓰면 발화하지 않는다.
  (h) 범위(web_report/·server/report/) 밖 경로는 통과한다.
  (i) Stop 훅의 벤치 제안 — 성능 민감 파일이 바뀌면 1회만, 무관 파일엔 무반응,
      위반이 있으면 위반이 우선, stop_hook_active 면 무조건 통과.
  (j) 규칙 메타(id 중복·필수 키·정규식 컴파일)가 온전하다.

가드는 fail-open 이라 훅에서 조용히 죽어도 티가 안 난다. 이 테스트가 유일한 안전망이다.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import perf_guard as pg   # noqa: E402


def _ids(hits) -> set[str]:
    return {h["rule"]["id"] for h in hits}


# --------------------------------------------------------------------------
# 규칙별 샘플: (규칙 id, 파일 경로, 위반 텍스트, 정상 텍스트)
# --------------------------------------------------------------------------
ADD_CASES = [
    ("R01-dist-downsample", "web_report/tabs/distribution.py",
     "    pts = _downsample(values, 2000)",
     "    pts = build_distribution_compact(values)"),
    ("R01-dist-downsample", "web_report/dist_blob.py",
     "_MAX_CDF_POINTS = 5000",
     "_CHUNK_TARGET_CELLS = 240"),
    ("R02-ecdf-line", "server/report/static/webreport/distribution.js",
     "  line: { shape: 'hv', width: 1 },",
     "  mode: 'markers', marker: { size: 2 },"),
    ("R03-gzip-level", "web_report/disk_cache.py",
     "    data = gzip.compress(report_bytes, compresslevel=6)",
     "    data = gzip.compress(report_bytes, compresslevel=1)"),
    ("R05-excel-import", "web_report/loader.py",
     "import openpyxl",
     "import pyarrow.parquet as pq"),
    ("R06-es-module", "server/report/static/webreport/yield_tab.js",
     "import { renderYield } from './common.js';",
     "function renderYield(rows) { return rows; }"),
    ("R08-inline-fallback", "web_report/compute.py",
     "    except BrokenProcessPool:\n"
     "        _reset_pool(shutdown=True)\n"
     "        return job(*args)\n",
     "    except BrokenProcessPool:\n"
     "        _reset_pool(shutdown=True)\n"
     "        raise\n"),
    ("R09-chunk-keyed-lock", "web_report/dist_pack_store.py",
     "    with cache.keyed_lock_ctx(('chunk',) + cache_key):",
     "    items = cache.cache_get(cache.DIST_CHUNK_CACHE, cache_key)"),
    ("R10-tmp-fixed-name", "web_report/disk_cache.py",
     '    tmp = path.with_name(path.name + ".tmp")',
     '    tmp = path.with_name(f"{path.name}.{os.getpid()}-{tid}.tmp")'),
    ("R11-keyed-lock-cap", "web_report/cache.py",
     "_KEYED_LOCKS_MAX = 64",
     "_KEYED_LOCKS_MAX = 256"),
]

REMOVE_CASES = [
    ("S05-unique-once", "web_report/dist_pack.py",
     "    uniq, inv = np.unique(values, return_inverse=True)"),
    ("S07-finite-scan", "web_report/metrics.py",
     "    finite = finite_count_map(tables)"),
    ("S08-cancel-preserve-pool", "web_report/compute.py",
     "        if fut.cancel():"),
    ("S09-map-seed", "web_report/service.py",
     "                                seed_map(session_id, session, tables,"),
    ("S09-map-seed", "server/report/routes_session.py",
     "        web_report_service.schedule_map_backfill("),
]


def check_globs() -> None:
    m = pg._match_path
    assert m("web_report/tabs/distribution.py", ["web_report/**/*.py"])
    assert m("web_report/loader.py", ["web_report/**/*.py"])          # ** 는 0단계도
    assert not m("web_report/loader.js", ["web_report/**/*.py"])
    assert not m("client/honey_main.py", ["web_report/**/*.py"])
    assert m("server/report/static/webreport/a.js",
             ["server/report/static/webreport/*.js"])
    assert not m("server/report/static/webreport/sub/a.js",
                 ["server/report/static/webreport/*.js"])
    assert pg.in_scope("web_report/x.py") and pg.in_scope("server/report/x.py")
    assert not pg.in_scope("server/admin_panel/metrics.py")
    assert not pg.in_scope("client/honey_main.py")
    print("  (a) 경로 glob·범위 판정 OK")


def check_add_rules() -> None:
    for rid, path, bad, good in ADD_CASES:
        assert rid in _ids(pg._scan_added(path, bad)), f"{rid}: 위반을 못 잡았다"
        assert rid not in _ids(pg._scan_added(path, good)), f"{rid}: 정상 코드에 오탐"
    print(f"  (b) forbid_add {len(ADD_CASES)}건 위반 탐지 / 정상 통과 OK")


def check_comments_ignored() -> None:
    # 주석으로만 언급하는 것은 위반이 아니다 — 실제 docstring·설명이 이렇게 생겼다.
    assert not pg._scan_added("web_report/dist_blob.py",
                              "# _MAX_CDF_POINTS 같은 포인트 상한을 넣지 말 것")
    assert not pg._scan_added("web_report/dist_pack_store.py",
                              "    # chunk 단위 keyed_lock(...) 은 잡지 않는다")
    assert not pg._scan_added("server/report/static/webreport/distribution.js",
                              "// shape: 'hv' 계단형은 금지다")
    print("  (c) 주석 줄 무시 OK")


def check_exempt() -> None:
    same = "    data = gzip.compress(b, compresslevel=6)  # perf-guard: allow R03-gzip-level (검증용)"
    above = ("    # perf-guard: allow R03-gzip-level (검증용)\n"
             "    data = gzip.compress(b, compresslevel=6)")
    assert not pg._scan_added("web_report/disk_cache.py", same), "같은 줄 면제 실패"
    assert not pg._scan_added("web_report/disk_cache.py", above), "윗줄 면제 실패"
    # 다른 규칙 id 로는 면제되지 않는다
    wrong = "    data = gzip.compress(b, compresslevel=6)  # perf-guard: allow R01-dist-downsample"
    assert "R03-gzip-level" in _ids(pg._scan_added("web_report/disk_cache.py", wrong))
    print("  (d) 면제 주석 (같은 줄/윗줄/타 규칙 무효) OK")


def check_remove_rules() -> None:
    for rid, path, line in REMOVE_CASES:
        assert rid in _ids(pg._scan_removed(path, line)), f"{rid}: 삭제를 못 잡았다"
        assert rid not in _ids(pg._scan_added(path, line)), f"{rid}: 추가인데 발화"
    print(f"  (e) forbid_remove {len(REMOVE_CASES)}건 OK")


def check_require_pair() -> None:
    bump = "REPORT_SCHEMA_VERSION = 28\n"
    payload = "    payload['newkey'] = 1\n"

    fired = pg.scan_diff(diff={"web_report/metrics.py": {"added": payload,
                                                         "removed": ""}})
    assert "S01-report-schema" in _ids(fired), "스키마 미상향을 못 잡았다"

    paired = pg.scan_diff(diff={
        "web_report/metrics.py": {"added": payload, "removed": ""},
        "web_report/cache_policy.py": {"added": bump, "removed": ""},
    })
    assert "S01-report-schema" not in _ids(paired), "짝 변경이 있는데 발화"

    exempt = pg.scan_diff(diff={"web_report/metrics.py": {
        "added": payload + "    # perf-guard: allow S01-report-schema (값만 수정)\n",
        "removed": ""}})
    assert "S01-report-schema" not in _ids(exempt), "면제가 안 먹었다"

    # when_pattern 이 있는 규칙은 그 줄이 바뀔 때만
    w = pg.scan_diff(diff={"web_report/compute.py": {
        "added": "_WORKERS = int(os.environ.get('X', '8'))\n", "removed": ""}})
    assert "S03-worker-pair" in _ids(w), "_WORKERS 단독 변경을 못 잡았다"
    w2 = pg.scan_diff(diff={"web_report/compute.py": {
        "added": "    STATS['x'] = 0\n", "removed": ""}})
    assert "S03-worker-pair" not in _ids(w2), "무관한 변경에 발화"
    print("  (f) require_pair (미상향/짝변경/면제/when_pattern) OK")


def check_require_import() -> None:
    add = "    blob = cache.cache_get(cache.DIST_CACHE, cache_key)\n"
    assert "S06-cache-key-builder" in _ids(
        pg._scan_require_import("web_report/x.py", add, add))
    with_policy = "from . import cache_policy\n" + add
    assert "S06-cache-key-builder" not in _ids(
        pg._scan_require_import("web_report/x.py", add, with_policy))
    print("  (g) require_import OK")


def check_out_of_scope() -> None:
    # 범위 밖 파일은 어떤 규칙도 적용되지 않는다 (paths 가 이미 범위 안으로 한정).
    assert not pg._scan_added("client/report_generator/_builders.py",
                              "_MAX_CDF_POINTS = 5000")
    assert not pg._scan_added("tools/warm_webreport.py", "import openpyxl")
    print("  (h) 범위 밖 통과 OK")


def check_bench_prompt() -> None:
    """Stop 훅: 위반이 없어도 성능 민감 파일이 바뀌면 벤치 제안을 한 번만 돌려준다."""
    import io
    import tempfile

    def stop_with(diff):
        buf, out = io.StringIO(), sys.stdout
        sys.stdin, sys.stdout = io.StringIO("{}"), buf
        try:
            pg.run_stop()
        finally:
            sys.stdout = out
        return buf.getvalue()

    orig_collect, orig_marker = pg.collect_diff, pg._MARKER
    tmp = Path(tempfile.mkdtemp(prefix="pg_test_")) / "marker.txt"
    pg._MARKER = tmp
    try:
        # 성능 민감 파일 — 1회차만 제안, 2회차는 조용 (같은 파일 집합)
        perf = {"web_report/service.py": {"added": "    return p\n", "removed": ""}}
        pg.collect_diff = lambda *a, **k: perf
        first = stop_with(perf)
        assert "bench_webreport.py --quick" in first, "벤치 제안이 안 떴다"
        assert not stop_with(perf), "같은 파일 집합인데 두 번 떴다"

        # 성능과 무관한 파일만 바뀌면 아무 말도 하지 않는다
        pg._MARKER = tmp.with_name("m2.txt")
        pg.collect_diff = lambda *a, **k: {
            "web_report/edits.py": {"added": "    x = 1\n", "removed": ""}}
        assert not stop_with(None), "무관한 파일에 제안이 떴다"

        # 위반이 있으면 벤치 제안보다 위반이 우선한다
        pg._MARKER = tmp.with_name("m3.txt")
        bad = {"web_report/service.py":
               {"added": "    gzip.compress(b, compresslevel=6)\n", "removed": ""}}
        pg.collect_diff = lambda *a, **k: bad
        assert "R03-gzip-level" in stop_with(bad), "위반이 우선하지 않았다"

        # stop_hook_active 면 무조건 통과 (무한 루프 방지)
        pg._MARKER = tmp.with_name("m4.txt")
        buf, out = io.StringIO(), sys.stdout
        sys.stdin, sys.stdout = io.StringIO('{"stop_hook_active":true}'), buf
        try:
            pg.run_stop()
        finally:
            sys.stdout = out
        assert not buf.getvalue(), "stop_hook_active 인데 막았다"
    finally:
        pg.collect_diff, pg._MARKER = orig_collect, orig_marker
    assert pg._match_path("web_report/service.py", pg.PERF_SENSITIVE)
    assert pg._match_path("web_report/tabs/yield_tab.py", pg.PERF_SENSITIVE)
    assert not pg._match_path("web_report/edits.py", pg.PERF_SENSITIVE)
    print("  (i) Stop 벤치 제안 (1회만/무관무반응/위반우선/루프가드) OK")


def check_rule_meta() -> None:
    seen = set()
    for r in pg._RULES:
        assert r["id"] not in seen, f"규칙 id 중복: {r['id']}"
        seen.add(r["id"])
        assert r["why"] and r["doc"], f"{r['id']}: why/doc 누락"
        assert r["kind"] in ("forbid_add", "forbid_remove",
                            "require_pair", "require_import"), r["id"]
        if r["kind"] in ("forbid_add", "forbid_remove", "require_import"):
            re.compile(r["pattern"])
            assert r.get("paths"), f"{r['id']}: paths 누락"
        else:
            re.compile(r["then_pattern"])
            assert r.get("when") and r.get("then_file"), f"{r['id']}: when/then 누락"
        if r.get("unless"):
            re.compile(r["unless"])
    # 샘플이 없는 forbid_* 규칙이 있으면 테스트가 규칙을 못 따라간 것이다.
    covered = {c[0] for c in ADD_CASES} | {c[0] for c in REMOVE_CASES}
    missing = [r["id"] for r in pg._RULES
               if r["kind"] in ("forbid_add", "forbid_remove")
               and r["id"] not in covered]
    assert not missing, f"샘플 없는 규칙: {missing}"
    print(f"  (j) 규칙 메타 {len(pg._RULES)}개 OK")


def selftest() -> int:
    print("perf_guard 자기 검증")
    check_globs()
    check_add_rules()
    check_comments_ignored()
    check_exempt()
    check_remove_rules()
    check_require_pair()
    check_require_import()
    check_out_of_scope()
    check_bench_prompt()
    check_rule_meta()
    print("전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())

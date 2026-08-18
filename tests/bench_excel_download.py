"""Excel Download 벤치 — 규모를 키워가며 소요/용량/경고를 잰다 (3분 SLA 확인).

    python tests/bench_excel_download.py [항목수 …]          (기본 40 200 600)
    python tests/bench_excel_download.py --sources 7 --points 1000 --cpk 200 2000
    python tests/bench_excel_download.py --com 200            기존 COM 엔진과 비교(Excel 필요)

옵션
    --sources N  입력 source(=파일) 개수      기본 2
    --points N   source 당 die(ECDF 점) 수    기본 800
    --cpk N      Issue Table CPK 섹션 행 수   기본 1 (행마다 썸네일 1장이 렌더된다)

ProcessPoolExecutor 를 쓰므로 **파일로 실행**해야 한다(heredoc/stdin 불가 — Windows spawn).
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "client"), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _opt(argv, name, default):
    return int(argv[argv.index(name) + 1]) if name in argv else default


def main(argv):
    import multiprocessing
    multiprocessing.freeze_support()
    import test_excel_xlsxwriter as T

    engine = "com" if "--com" in argv else "xlsxwriter"
    n_sources = _opt(argv, "--sources", 2)
    n_points = _opt(argv, "--points", 800)
    n_cpk = _opt(argv, "--cpk", 1)
    flags = {"--com", "--sources", "--points", "--cpk"}
    consumed = {argv[argv.index(f) + 1] for f in flags & set(argv) if f != "--com"}
    counts = [int(a) for a in argv if a.isdigit() and a not in consumed] or [40, 200, 600]
    T.set_scale(n_sources, n_points)

    print(f"엔진={engine}  CPU={os.cpu_count()}  DPI="
          f"{__import__('excel_download._charts', fromlist=['DPI']).DPI}  "
          f"source={n_sources}  die/source={n_points}  CPK행={n_cpk}")
    print(f"{'항목':>6} {'ECDF점':>10} {'소요(s)':>9} {'용량(MB)':>10} {'경고':>5}  판정")
    tmp = tempfile.mkdtemp(prefix="bench_exceldl_")
    rows = []
    try:
        for n in counts:
            t0 = time.perf_counter()
            res = T.build(tmp, n_items=n, out_name=f"b{n}.xlsx", engine=engine,
                          n_cpk=n_cpk)
            elapsed = time.perf_counter() - t0
            size = os.path.getsize(res["out_path"]) / 1e6
            points = (n + 4) * n_sources * n_points
            verdict = "OK" if elapsed < 180 else "SLA 초과(3분)"
            print(f"{n + 4:>6} {points / 1e6:>9.1f}M {elapsed:>9.1f} {size:>10.1f} "
                  f"{len(res['warnings']):>5}  {verdict}")
            for w in res["warnings"][:3]:
                print(f"         ! {w}")
            rows.append((n + 4, elapsed, size, len(res["warnings"])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    worst = max(rows, key=lambda r: r[1]) if rows else None
    if worst:
        print(f"\n최대 소요: 항목 {worst[0]}개 → {worst[1]:.1f}s "
              f"({'3분 이내' if worst[1] < 180 else '3분 초과'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

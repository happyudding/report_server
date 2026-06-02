"""distribution 차트 생성 병목 측정 러너 (PyQt 없이 UI 표준 흐름 재현).

차트 생성은 client/report_generator/xlsx_writer.py 의 xlwings(Excel COM) 경로에서
일어난다. 이 스크립트는 honey_main 의 분석→xlsx 생성 흐름을 그대로 재현하되,
HONEY_CHART_PROFILE 가 켜져 있으면 xlsx_writer 가 phase 별 소요시간 breakdown 을
stderr 로 출력한다.

사용 (Excel 설치된 Windows, PowerShell):
    $env:HONEY_CHART_PROFILE = "1"; python tests/profile_distribution.py
    $env:HONEY_CHART_PROFILE = "1"; python tests/profile_distribution.py --no-fail-item
    $env:HONEY_CHART_PROFILE = "1"; python tests/profile_distribution.py --limit 10

옵션:
    --limit N        fail subject 중 앞 N개만 차트화 (빠른 반복용). 미지정 시 전체 fail.
    --no-fail-item   sheets 에서 fail_item 제외 → _attach_fail_item_charts(PNG export/
                     삽입) 미실행. --no-fail-item 유무의 wall 차이 = PNG 부착 순비용.
    --csv PATH       측정용 CSV 추가 지정 (반복 가능). 미지정 시 기본 샘플 3개.
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CLIENT = _ROOT / "client"
sys.path.insert(0, str(_CLIENT))

import report_generator as rg                         # noqa: E402
from report_generator import xlsx_writer              # noqa: E402

_DEFAULT_CSVS = [
    _CLIENT / "data" / "mass_data_up_a.csv",
    _CLIENT / "data" / "mass_data_up_b.csv",
    _CLIENT / "data" / "mass_data_up_c.csv",
]


def _load_colors():
    """honey_main 과 동일하게 차트 색을 불러온다 (실패 시 None → Excel 기본색)."""
    try:
        import chart_colors
        return chart_colors.load_colors()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="distribution 차트 병목 측정")
    ap.add_argument("--limit", type=int, default=None,
                    help="fail subject 중 앞 N개만 차트화")
    ap.add_argument("--no-fail-item", action="store_true",
                    help="fail_item 시트 제외 (PNG attach 비용 분리)")
    ap.add_argument("--csv", action="append", default=None,
                    help="측정용 CSV 경로 (반복 지정 가능)")
    args = ap.parse_args()

    csvs = [Path(p) for p in args.csv] if args.csv else _DEFAULT_CSVS
    missing = [str(p) for p in csvs if not p.exists()]
    if missing:
        print("[runner] CSV 없음: " + ", ".join(missing), file=sys.stderr)
        return 1

    t0 = time.perf_counter()
    group = rg.df_honey_group.from_csvs([str(p) for p in csvs])
    sel = rg.ItemSelector.fail_only(group)              # UI 기본 선택 미러링
    if args.limit is not None and sel.selected_items:
        sel.selected_items = sel.selected_items[:args.limit]
    result = rg.analyze(group, selector=sel)
    t_analyze = time.perf_counter() - t0

    sheets = ["distribution"] + ([] if args.no_fail_item else ["fail_item"])
    colors = _load_colors()

    with tempfile.TemporaryDirectory(prefix="honey_prof_") as td:
        out = str(Path(td) / "profile_report.xlsx")
        t1 = time.perf_counter()
        xlsx_writer.write(result, out, sheets=sheets, colors=colors)
        t_write = time.perf_counter() - t1

    print(
        f"\n[runner] analyze={t_analyze:.3f}s  write()={t_write:.3f}s  "
        f"charts={len(result.distributions)}  sources={len(result.sources)}  "
        f"sheets={sheets}  fail_rows={len(result.yield_rows)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

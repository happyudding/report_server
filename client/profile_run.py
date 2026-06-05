"""Honey 리포트 파이프라인 구간별 프로파일링 러너 (PyQt 불필요).

전체 흐름(parse → analyze → xlsx write → [charts])을 headless 로 구동하면서
analyzer/_flow_time, xlsx_writer/_flow_prof 가 측정한 모든 구간을 _profile sink 로
수집한다. 결과를 client/_profiles/<label|timestamp>.json 으로 저장하고, 이번 런의
구간별 정렬 breakdown 을 출력한다. compare 로 두 저장 런(예: 최적화 전/후)을 비교한다.

사용 예:
  python client/profile_run.py run --csv client/data/mass_data_up_a.csv --label baseline
  python client/profile_run.py run --csv a.csv b.csv --charts --label full
  python client/profile_run.py compare baseline full      # 명시
  python client/profile_run.py compare                    # 최신 2개 자동
  python client/profile_run.py list
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
import time
from pathlib import Path

# client/ 를 path 에 추가 (report_generator import 용) — honey_main 과 동일 전제
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import report_generator as rg                       # noqa: E402
from report_generator import xlsx_writer, _profile  # noqa: E402

_SAVE_DIR = _HERE / "_profiles"
_TABLE_SHEETS = ["summary", "yield", "cpk", "fail_item", "issue_table"]


# ---------------------------------------------------------------------------
# run

def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(_HERE), capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "?"
    except Exception:
        return "?"


def _make_parse_cb():
    """from_csvs progress_cb — 콜백 간 시간차로 per-file parse 시간을 sink 에 적재.

    from_csvs 는 파일 i 로드 직전 cb(i, n, filename) 을, 끝에 cb(n, n, "") 을 호출한다.
    파일 내부 sub-callback(5-arg) 은 무시한다.
    """
    state = {"prev": None, "t": None}

    def cb(*a):
        if len(a) != 3:   # 파일 내부 sub-callback (i,n,f,s,t) → 무시
            return
        _i, _n, fname = a
        now = time.perf_counter()
        if state["prev"] is not None:
            _profile.add("parse", f"file[{state['prev']}]", now - state["t"])
        state["prev"] = fname or None
        state["t"] = now

    return cb


def cmd_run(args) -> int:
    paths = [str(Path(p)) for p in args.csv]
    for p in paths:
        if not Path(p).exists():
            print(f"[error] CSV 없음: {p}", file=sys.stderr)
            return 2

    sel = list(_TABLE_SHEETS)
    if args.charts:
        sel.append("distribution")

    meta = rg.ReportMeta()
    selector = rg.ItemSelector(selected_items=(args.items or None))

    _profile.start_collection()
    wall_t0 = time.perf_counter()

    # 1) parse
    t0 = time.perf_counter()
    group = rg.df_honey_group.from_csvs(paths, report_meta=meta, progress_cb=_make_parse_cb())
    _profile.add("", "parse.total", time.perf_counter() - t0)

    # 입력 규모
    mm = group.mass_data_map
    first = next(iter(mm.values())) if mm else None
    rows, cols = (first.scores.shape if first is not None else (0, 0))

    # 2) analyze (내부 fine 구간 자동 수집)
    t0 = time.perf_counter()
    result = rg.analyze(group, meta=meta, selector=selector)
    _profile.add("", "analyze.total", time.perf_counter() - t0)

    # 3) xlsx write (옵션)
    out_path = None
    if not args.no_xlsx:
        out_path = str(Path(args.out) if args.out else (_SAVE_DIR / f"_run_{args.label or 'tmp'}.xlsx"))
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        xlsx_writer.write(result, out_path, sheets=sel, raw_sheets=group.raw_frames())
        _profile.add("", "xlsx.total", time.perf_counter() - t0)

    wall_total = time.perf_counter() - wall_t0
    _profile.stop_collection()

    records = _profile.snapshot()
    stamp = _dt.datetime.now().strftime("%y%m%d_%H%M%S")
    label = args.label or stamp
    payload = {
        "label": label,
        "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "inputs": {"csv": paths, "files": len(paths), "rows": int(rows), "cols": int(cols)},
        "charts": bool(args.charts),
        "xlsx": (not args.no_xlsx),
        "out_path": out_path,
        "wall_total_s": wall_total,
        "records": records,
    }
    save_path = _profile.save(_SAVE_DIR / f"{label}.json", payload)

    _print_breakdown(payload)
    print(f"\n저장: {save_path}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# 출력

def _print_breakdown(payload: dict) -> None:
    wall = payload["wall_total_s"] or 0.0
    agg = _profile.aggregate(payload["records"])
    inp = payload["inputs"]
    print(f"\n=== 구간 breakdown : {payload['label']} "
          f"(commit {payload['git_commit']}, "
          f"{inp['files']}files {inp['rows']}x{inp['cols']}, "
          f"charts={payload['charts']}) ===")
    print(f"wall_total {wall:8.3f}s\n")
    print(f"  {'module.label':<46} {'elapsed':>9}  {'%wall':>6}  {'cnt':>3}")
    print("  " + "-" * 70)
    for key, slot in sorted(agg.items(), key=lambda kv: -kv[1]["elapsed"]):
        el = slot["elapsed"]
        pct = (el / wall * 100.0) if wall else 0.0
        print(f"  {key:<46} {el:8.3f}s  {pct:5.1f}%  {slot['count']:>3}")


# ---------------------------------------------------------------------------
# compare

def _resolve_two(args) -> tuple:
    if args.a and args.b:
        def _resolve(s):
            # .json 명시 경로 우선, 아니면 라벨로 보고 _profiles/<라벨>.json
            if s.endswith(".json"):
                return Path(s)
            cand = _SAVE_DIR / f"{s}.json"
            return cand if cand.exists() else Path(s)
        return _resolve(args.a), _resolve(args.b)
    # 인자 생략 → 최신 2개 (수정시각 기준, b=최신 / a=직전)
    files = sorted(_SAVE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if len(files) < 2:
        return None, None
    return files[-2], files[-1]


def cmd_compare(args) -> int:
    a_path, b_path = _resolve_two(args)
    if a_path is None or not Path(a_path).exists() or not Path(b_path).exists():
        print("[error] 비교할 프로파일 2개가 필요합니다. (list 로 확인)", file=sys.stderr)
        return 2
    A, B = _profile.load(a_path), _profile.load(b_path)
    aggA, aggB = _profile.aggregate(A["records"]), _profile.aggregate(B["records"])
    keys = sorted(set(aggA) | set(aggB),
                  key=lambda k: -max(aggA.get(k, {}).get("elapsed", 0.0),
                                     aggB.get(k, {}).get("elapsed", 0.0)))
    print(f"\n=== compare : A={A['label']} ({A['git_commit']})  vs  "
          f"B={B['label']} ({B['git_commit']}) ===")
    print(f"wall_total   A {A['wall_total_s']:8.3f}s   B {B['wall_total_s']:8.3f}s   "
          f"diff {B['wall_total_s'] - A['wall_total_s']:+8.3f}s\n")
    print(f"  {'module.label':<46} {'A':>9} {'B':>9} {'diff':>9}  {'diff%':>7}")
    print("  " + "-" * 84)
    for k in keys:
        ea = aggA.get(k, {}).get("elapsed", 0.0)
        eb = aggB.get(k, {}).get("elapsed", 0.0)
        d = eb - ea
        dp = (d / ea * 100.0) if ea else float("inf")
        dp_s = f"{dp:+6.1f}%" if ea else "   new "
        print(f"  {k:<46} {ea:8.3f}s {eb:8.3f}s {d:+8.3f}s  {dp_s}")
    return 0


# ---------------------------------------------------------------------------
# list

def cmd_list(_args) -> int:
    files = sorted(_SAVE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        print(f"(저장된 프로파일 없음: {_SAVE_DIR})")
        return 0
    print(f"{'label':<24} {'timestamp':<20} {'wall':>9}  {'commit':<8} inputs")
    print("-" * 80)
    for f in files:
        try:
            d = _profile.load(f)
        except Exception:
            continue
        inp = d.get("inputs", {})
        print(f"{d.get('label', f.stem):<24} {d.get('timestamp', ''):<20} "
              f"{d.get('wall_total_s', 0):8.3f}s  {d.get('git_commit', '?'):<8} "
              f"{inp.get('files', '?')}files {inp.get('rows', '?')}x{inp.get('cols', '?')}")
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Honey 파이프라인 구간 프로파일러")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="전체 흐름 1회 실행 + 측정 + 저장")
    r.add_argument("--csv", nargs="+", required=True, help="입력 CSV 경로(1개 이상)")
    r.add_argument("--items", nargs="*", default=None, help="분석 subject 제한 (생략 시 전체)")
    r.add_argument("--charts", action="store_true", help="distribution 차트(xlwings/Excel) 포함")
    r.add_argument("--no-xlsx", action="store_true", help="xlsx write 생략 (parse+analyze 만)")
    r.add_argument("--out", default=None, help="xlsx 출력 경로 (생략 시 _profiles 내 임시)")
    r.add_argument("--label", default=None, help="저장 파일/표시 라벨 (생략 시 timestamp)")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="두 프로파일 비교 (생략 시 최신 2개)")
    c.add_argument("a", nargs="?", default=None, help="기준 프로파일(라벨 또는 .json)")
    c.add_argument("b", nargs="?", default=None, help="대상 프로파일(라벨 또는 .json)")
    c.set_defaults(func=cmd_compare)

    l = sub.add_parser("list", help="저장된 프로파일 목록")
    l.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

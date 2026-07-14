"""Yield 탭 STEP 빈칸 원인 진단 (read-only).

실행:
    python -m web_report.diag_yield_step <parquet_dir | source_*.parquet ...>

예:
    python -m web_report.diag_yield_step uploads/report/web_report/<hash>/

빈 STEP fail 행이 왜 생기는지를 production 과 동일한 매칭 코드로 재현해 분류한다.
FAILTNO → (정규화 후 TNO 동등비교) → item → item.STEP 흐름에서, STEP 이 비는 원인은:
  (a) genuine blank        : 매칭된 item 의 STEP 메타셀이 원본부터 공백
  (b) collision tie-break  : 같은 TNO 형제 중 STEP 있는 item 대신 STEP 없는 item 이 선택됨
  (c) multi-source blank    : item 이 여러 source 에 있고 첫 source 의 STEP 만 공백

파일 저장·DB·네트워크 없음. 순수 조회·출력.
"""
from __future__ import annotations

import glob
import os
import sys
from collections import Counter, defaultdict

from .honeyform import decode_split_honeyform_parquet
from .tabs import common, yield_tab


def _load_tables(paths):
    """(source_name, parquet_path) 목록 → HoneyformTable 리스트 (production 디코더 재사용)."""
    tables = []
    for name, path in paths:
        with open(path, "rb") as fh:
            data = fh.read()
        tables.append(decode_split_honeyform_parquet(
            data, source=name, file_name=os.path.basename(path), keep_df=False))
    return tables


def _resolve_paths(args):
    """CLI 인자(디렉토리 또는 파일들) → [(source_name, path)] (source_0, source_1 … 순)."""
    files = []
    for a in args:
        if os.path.isdir(a):
            files.extend(sorted(glob.glob(os.path.join(a, "source_*.parquet"))))
        else:
            files.append(a)
    seen, out = set(), []
    for f in files:
        f = os.path.abspath(f)
        if f in seen:
            continue
        seen.add(f)
        out.append((os.path.splitext(os.path.basename(f))[0], f))
    return out


def _grouped_by_tno(table):
    """table 의 정규화 TNO → [item...] (tie-break 이전, 충돌 확인용). yield_tab 규칙과 동일."""
    grouped = defaultdict(list)
    for item, tno in table.tno.items():
        norm = yield_tab._tno_norm(tno)
        if norm is not None:
            grouped[norm].append(item)
    return grouped


def _winner(table, items):
    """yield_tab.tno_to_item_map 와 동일한 tie-break(가장 빠른 TSEQ) 승자."""
    return min(items, key=lambda it: yield_tab._tseq_sort_key(table, it))


def _fmt_step(table, item):
    return common.fmt_type(table.step.get(item))


def _classify(item, tno_disp, tables):
    """빈 STEP fail item 의 원인 (a)/(b)/(c) 분류 + 증거 문자열.

    반환 (verdict, evidence). verdict ∈ {'a','b','c'}.
    """
    ev = []
    verdict = "a"  # 기본: 어디에서도 STEP 없고 대안도 없음
    step_nonblank_somewhere = False
    for t in tables:
        if item not in t.item_columns:
            continue
        raw = t.step.get(item)
        fstep = common.fmt_type(raw)
        norm = yield_tab._tno_norm(t.tno.get(item))
        siblings = _grouped_by_tno(t).get(norm, [])
        if fstep:
            step_nonblank_somewhere = True
        # (b): 같은 TNO 형제 중 STEP 있는 item 존재 & 현재 승자는 STEP 공백
        sib_steps = [(s, _fmt_step(t, s), yield_tab._tseq_sort_key(t, s),
                      common._item_has_data([t], s)) for s in siblings]
        win = _winner(t, siblings) if siblings else item
        alt = [s for s in siblings if _fmt_step(t, s) and s != win]
        if len(siblings) > 1 and not _fmt_step(t, win) and alt:
            verdict = "b"
            ev.append(f"{t.source}: TNO={norm} 충돌 winner={win}(STEP='') "
                      f"대안={[(s, _fmt_step(t, s)) for s in alt]}")
        else:
            ev.append(f"{t.source}: rawSTEP={raw!r} fmt='{fstep}' "
                      f"siblings={[(s, st) for s, st, _, _ in sib_steps]}")
    # (c): 어떤 source 에선 STEP 있는데 병합값(item_meta first-wins)은 공백
    if verdict != "b" and step_nonblank_somewhere:
        verdict = "c"
    return verdict, " | ".join(ev)


def diagnose(paths):
    tables = _load_tables(paths)
    print("=" * 78)
    print(f"sources ({len(tables)}): " +
          ", ".join(f"{t.source}[{len(t.item_columns)} items]" for t in tables))

    # ── per-source 기본 지표 + 미매칭 FAILTNO (진단만, 수정 안 함) ──────────────
    for t in tables:
        bins = common.bin_types(t)
        failtno = yield_tab.failtno_norms(t)
        grouped = _grouped_by_tno(t)
        tno_set = set(grouped.keys())
        collisions = {k: v for k, v in grouped.items() if len(v) > 1}
        fail_norms = [f for f in failtno if f is not None]
        distinct_fail = Counter(fail_norms)
        matched = [f for f in distinct_fail if f in tno_set]
        unmatched = [f for f in distinct_fail if f not in tno_set]
        dropped_dies = sum(distinct_fail[f] for f in unmatched)
        n_pass = sum(1 for b in bins if b == common.PASS_BIN)
        print("-" * 78)
        print(f"{t.source}: dies={len(bins)} pass={n_pass} fail={len(bins) - n_pass}")
        print(f"  TNO 충돌(같은 TNO 여러 item): {len(collisions)}")
        print(f"  FAILTNO distinct={len(distinct_fail)} matched={len(matched)} "
              f"UNMATCHED={len(unmatched)} (누락 fail die={dropped_dies}) [범위밖, 참고]")
        if unmatched:
            print(f"    unmatched 예: {unmatched[:10]}")

    # ── production 매칭 재현 → yield_rows ───────────────────────────────────────
    fail_counts = {t.source: yield_tab.fail_counts_by_source(t) for t in tables}
    yield_rows = yield_tab.build_yield_rows(tables, fail_counts)

    fail_rows = [r for r in yield_rows
                 if str(r.get("bin")) != common.PASS_BIN and r.get("Item")]
    blank_rows = [r for r in fail_rows if not str(r.get("step") or "").strip()]

    def _row_count(r):
        return sum(int(v or 0) for k, v in r.items() if str(k).endswith("_count"))

    total_fail_dies = sum(_row_count(r) for r in fail_rows)
    blank_fail_dies = sum(_row_count(r) for r in blank_rows)
    print("=" * 78)
    print(f"fail 행: {len(fail_rows)} (fail die {total_fail_dies}) / "
          f"빈 STEP fail 행: {len(blank_rows)} (fail die {blank_fail_dies})")

    verdicts = Counter()
    for r in sorted(blank_rows, key=_row_count, reverse=True):
        item = r.get("Item")
        v, ev = _classify(item, r.get("TNO"), tables)
        verdicts[v] += 1
        print(f"  [{v}] bin={r.get('bin')} item={item!r} TNO={r.get('TNO')!r} "
              f"count={_row_count(r)}")
        print(f"       {ev}")

    # ── TNO 충돌 상세 (최대 20개) ───────────────────────────────────────────────
    printed = 0
    for t in tables:
        grouped = _grouped_by_tno(t)
        collisions = {k: v for k, v in grouped.items() if len(v) > 1}
        for norm, items in collisions.items():
            if printed >= 20:
                break
            win = _winner(t, items)
            print("-" * 78)
            print(f"충돌 {t.source} normTNO={norm}:")
            for s in items:
                mark = "  <-- winner" if s == win else ""
                print(f"    {s!r} TSEQ={t.tseq.get(s)!r} STEP='{_fmt_step(t, s)}' "
                      f"data={common._item_has_data([t], s)}{mark}")
            printed += 1

    print("=" * 78)
    print("빈 STEP 원인 분류:")
    print(f"  (a) genuine blank        : {verdicts['a']}")
    print(f"  (b) collision tie-break  : {verdicts['b']}")
    print(f"  (c) multi-source blank    : {verdicts['c']}")
    return verdicts


def main(argv=None):
    try:  # Korean 출력이 콘솔 인코딩에서 깨지거나 죽지 않도록 (VSCode/UTF-8 기준)
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 1
    paths = _resolve_paths(argv)
    if not paths:
        print("parquet 파일을 찾지 못했습니다. (디렉토리면 source_*.parquet 존재 확인)")
        return 1
    diagnose(paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Yield tab payload builder."""
from __future__ import annotations

from collections import Counter, defaultdict

import pandas as pd

from .common import PASS_BIN, bin_sort_key, bin_types, item_meta as _item_meta


def _tno_norm(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        f = float(value)
        if f == 0:
            return None
        if f.is_integer():
            return int(f)
        return f
    except (TypeError, ValueError):
        text = str(value).strip()
        return text or None


def failtno_norms(table) -> list:
    """table.data["FAILTNO"] 전체의 _tno_norm 변환 리스트 — 테이블 인스턴스 단위 lazy 캐시.

    common.bin_types 와 같은 방식: 한 요청에서 yield/distribution/map 빌더가 같은 컬럼을
    각자 재변환하지 않도록 재사용한다 (tables 는 요청마다 새 클론이라 무효화 불필요).
    """
    cached = getattr(table, "_failtno_norms_cache", None)
    if cached is None:
        cached = [_tno_norm(v) for v in table.data["FAILTNO"].tolist()]
        table._failtno_norms_cache = cached
    return cached


def _tseq_sort_key(table, item):
    """항목의 TEST SEQ(TSEQ, 메타 row0) 정렬 키 — 숫자 우선, 비수치는 뒤로(이름순).

    동일 TNO 를 공유하는 항목 중 'TEST SEQ 가 가장 앞선' 하나를 고르는 데 쓴다
    (distribution.tseq_sort_key 와 동일 규칙, 여기선 단일 table 기준).
    """
    try:
        return (0, float(table.tseq.get(item)), str(item))
    except (TypeError, ValueError):
        return (1, 0.0, str(item))


def tno_to_item_map(table) -> dict:
    """정규화 TNO → 항목명 리스트 맵 — 테이블 인스턴스 단위 lazy 캐시.

    FAILTNO→항목 귀속(yield/distribution 공통 규칙)의 단일 출처.
    동일 TNO 를 여러 항목이 공유하면 TEST SEQ(TSEQ) 가 가장 앞선 항목 1개만 남긴다
    — Yield/Issue table 에 같은 fail count 가 여러 item 행으로 중복 표시되는 것을 막는
    사전 필터(나머지 항목은 이 TNO 에 귀속하지 않는다). 반환 계약(list)은 유지한다.
    """
    cached = getattr(table, "_tno_to_item_cache", None)
    if cached is None:
        grouped = defaultdict(list)
        for item, tno in table.tno.items():
            norm = _tno_norm(tno)
            if norm is not None:
                grouped[norm].append(item)
        cached = {
            norm: [min(items, key=lambda it: _tseq_sort_key(table, it))]
            for norm, items in grouped.items()
        }
        table._tno_to_item_cache = cached
    return cached


def fail_counts_by_source(table) -> Counter:
    tno_to_item = tno_to_item_map(table)

    # 행 단위 iterrows 대신 컬럼 일괄 변환 후 (BIN, FAILTNO) 쌍으로 집계한다.
    pair_counts = Counter(
        (b, f) for b, f in zip(bin_types(table), failtno_norms(table)) if f is not None)

    counts = Counter()
    for (bin_value, fail_tno), cnt in pair_counts.items():
        for item in tno_to_item.get(fail_tno, []):
            counts[(bin_value, item)] += cnt
    return counts


def build_yield_rows(tables, fail_counts):
    rows = []
    totals = {t.source: len(t.data) for t in tables}
    item_meta = _item_meta(tables)

    pass_row = {"step": "", "bin": PASS_BIN, "TNO": "", "Item": "Pass"}
    pass_portions = []
    for table in tables:
        count = sum(1 for b in bin_types(table) if b == PASS_BIN)
        portion = round(count / totals[table.source] * 100.0, 2) if totals[table.source] else 0.0
        pass_row[f"{table.source}_yield"] = portion
        pass_row[f"{table.source}_count"] = count
        pass_portions.append(portion)
    pass_row["avg"] = round(sum(pass_portions) / len(pass_portions), 2) if pass_portions else 0.0
    rows.append(pass_row)

    keys = sorted(
        {key for counts in fail_counts.values() for key in counts.keys() if key[0] != PASS_BIN},
        key=lambda key: (bin_sort_key(key[0]), str(key[1])),
    )
    for bin_value, item in keys:
        meta = item_meta.get(item, {})
        row = {
            "step": meta.get("step", ""),
            "bin": bin_value,
            "TNO": meta.get("tno", ""),
            "Item": item,
        }
        portions = []
        for table in tables:
            count = int(fail_counts[table.source].get((bin_value, item), 0))
            portion = round(count / totals[table.source] * 100.0, 2) if totals[table.source] else 0.0
            row[f"{table.source}_yield"] = portion
            row[f"{table.source}_count"] = count
            portions.append(portion)
        row["avg"] = round(sum(portions) / len(portions), 2) if portions else 0.0
        rows.append(row)
    return rows


def _bin_total_row(rows_sorted):
    """Bin 대표 행: 식별정보(Step/TNO/Item)는 most-fail 행에서, 수치(avg/{src}_yield/{src}_count)
    는 그 Bin 의 모든 fail TNO 행 합계로 채운 집계 행. 대표 행이 'Bin 별 총합 Yield' 를 보여주고,
    펼치면 TNO 별 개별 yield 행(most-fail 포함 전체)이 보이도록 하기 위함."""
    total = dict(rows_sorted[0])
    for key in rows_sorted[0]:
        name = str(key)
        if name.endswith("_count"):
            total[key] = sum(int(r.get(key) or 0) for r in rows_sorted)
        elif name == "avg" or name.endswith("_yield"):
            total[key] = round(sum(float(r.get(key) or 0) for r in rows_sorted), 2)
    return total


def build_yield_bin_groups(yield_rows):
    """Bin 별 그룹(Pass 제외). rep = Bin 총합 집계 행, rows = [rep] + 그 Bin 의 전체 fail TNO 행.

    Yield 시트가 Bin 당 대표(총합) 1행만 접힌 상태로 보여주고, 펼치면 rows[1:](모든 TNO 행,
    most-fail 포함)를 보여주는 데 쓴다. Issue Table 의 Bin 집계도 동일 그룹을 쓴다.
    그룹 정렬은 Bin 총 fail 비중(avg 합 = 대표행 avg) 내림차순 = 화면상 worst-yield 우선.
    """
    groups_map = {}
    for row in yield_rows or []:
        bin_value = row.get("bin")
        if str(bin_value).strip() == PASS_BIN:
            continue
        if not row.get("Item"):
            continue
        groups_map.setdefault(str(bin_value), []).append(row)

    groups = []
    for bin_value, rows in groups_map.items():
        rows_sorted = sorted(rows, key=lambda r: r.get("avg") or 0, reverse=True)
        total = _bin_total_row(rows_sorted)
        groups.append({"bin": bin_value, "rep": total, "rows": [total] + rows_sorted})
    groups.sort(key=lambda g: g["rep"].get("avg") or 0, reverse=True)
    return groups


# ── STEP 별 분해 (Yield 탭 전용, cascade 수율) ─────────────────────────────────
# Yield 탭은 STEP(P1/P2/P3) 별로 표를 나누고, 각 표의 bin portion 은 "그 STEP 에 진입한
# die 수"를 분모로 쓴다(fail-stop 누적 수율). Issue Table·Summary·fail_bin_ranking 은
# 여기를 쓰지 않고 build_yield_rows 의 전체(total) 기준 값을 그대로 쓴다 — 이 분해는
# yield_rows 를 변형하지 않고 재계산한 복사본으로만 동작한다.

def _step_order_key(step):
    """STEP 정렬 키: P<n> 은 숫자순(P1<P2<P3), 그 외 이름은 알파벳, 빈 값은 맨 뒤."""
    s = str(step or "").strip().upper()
    if not s:
        return (2, 0, "")
    if s.startswith("P") and s[1:].isdigit():
        return (0, int(s[1:]), s)
    return (1, 0, s)


def _cascade_denoms(tables, fail_rows):
    """source 별 STEP 진입 die 수(cascade 분모)를 계산.

    fail_rows: build_yield_rows 가 만든 fail 행(``step`` + ``{source}_count`` 보유).
    STEP 순서대로 진입 die 수(entered)에서 그 STEP fail 을 빼며 다음 STEP 진입 수를 얻는다
    (P1 진입=전체 die, P2 진입=전체-P1fail, ...). 반환: (ordered_steps, denom, step_fail, totals).
    """
    totals = {t.source: len(t.data) for t in tables}
    step_fail = {t.source: defaultdict(int) for t in tables}
    steps = set()
    for r in fail_rows:
        step = str(r.get("step") or "")
        steps.add(step)
        for t in tables:
            step_fail[t.source][step] += int(r.get(f"{t.source}_count") or 0)
    ordered = sorted(steps, key=_step_order_key)
    denom = {t.source: {} for t in tables}
    for t in tables:
        entered = totals[t.source]
        for step in ordered:
            denom[t.source][step] = entered
            entered -= step_fail[t.source][step]
    return ordered, denom, step_fail, totals


def _reyield_row(row, tables, denom):
    """행의 ``{source}_yield``/``avg`` 를 그 STEP 의 진입 die 수(denom) 기준으로 재계산한
    복사본을 반환한다(원본 yield_rows 불변 — Issue Table 이 total 기준 값을 계속 쓰게)."""
    step = str(row.get("step") or "")
    out = dict(row)
    portions = []
    for t in tables:
        count = int(row.get(f"{t.source}_count") or 0)
        d = denom[t.source].get(step) or 0
        portion = round(count / d * 100.0, 2) if d > 0 else 0.0
        out[f"{t.source}_yield"] = portion
        portions.append(portion)
    out["avg"] = round(sum(portions) / len(portions), 2) if portions else 0.0
    return out


def build_yield_step_groups(yield_rows, tables):
    """STEP 별로 분리한 Bin 그룹 목록. 각 원소 ``{step, groups: [bin group...]}``.

    STEP 순서는 _step_order_key(P1→P2→P3). 각 STEP 내부는 build_yield_bin_groups 와 동일한
    Bin 대표+펼침 구조지만, portion 은 그 STEP 진입 die 수 기준으로 재계산된다.
    """
    fail_rows = [r for r in (yield_rows or [])
                 if str(r.get("bin")).strip() != PASS_BIN and r.get("Item")]
    ordered, denom, _, _ = _cascade_denoms(tables, fail_rows)
    by_step = defaultdict(list)
    for r in fail_rows:
        by_step[str(r.get("step") or "")].append(_reyield_row(r, tables, denom))
    out = []
    for step in ordered:
        rows = by_step.get(step) or []
        if not rows:
            continue
        out.append({"step": step, "groups": build_yield_bin_groups(rows)})
    return out


def yield_step_summary(tables, yield_rows):
    """상단 요약 박스용 STEP 요약 행: STEP 별 진입/fail/통과 die 수 + step 수율%.

    step_yield_pct = 통과(survivor) / 진입(entered) * 100 = 그 STEP 의 실효 수율.
    소스 여러 개면 소스 합산 기준(전체)으로 집계한다.
    """
    fail_rows = [r for r in (yield_rows or [])
                 if str(r.get("bin")).strip() != PASS_BIN and r.get("Item")]
    ordered, denom, step_fail, _ = _cascade_denoms(tables, fail_rows)
    out = []
    for step in ordered:
        entered = sum(denom[t.source].get(step, 0) for t in tables)
        fail = sum(step_fail[t.source].get(step, 0) for t in tables)
        survivor = max(entered - fail, 0)
        out.append({
            "step": step,
            "entered": entered,
            "fail": fail,
            "survivor": survivor,
            "step_yield_pct": round(survivor / entered * 100.0, 2) if entered else 0.0,
        })
    return out


def yield_overview(tables, yield_rows):
    """Yield 탭 상단 요약 박스: 전체 pass/fail/total count + 종합 yield% + 소스별 수율.

    yield_rows[0] 은 build_yield_rows 가 항상 추가하는 Pass 행이므로 그 값을 소스별로 합산한다.
    by_source: 소스마다 {source, yield_pct, pass, fail, total} (Total Yield 소스별 표시용).
    """
    total = sum(len(t.data) for t in tables)
    pass_row = yield_rows[0] if yield_rows else {}
    passed = sum(int(pass_row.get(f"{t.source}_count") or 0) for t in tables)
    failed = max(total - passed, 0)
    yield_pct = round(passed / total * 100.0, 2) if total else 0.0

    by_source = []
    for t in tables:
        t_total = len(t.data)
        t_pass = int(pass_row.get(f"{t.source}_count") or 0)
        by_source.append({
            "source": t.source,
            "yield_pct": round(t_pass / t_total * 100.0, 2) if t_total else 0.0,
            "pass": t_pass,
            "fail": max(t_total - t_pass, 0),
            "total": t_total,
        })

    return {"yield_pct": yield_pct, "pass": passed, "fail": failed, "total": total,
            "by_source": by_source, "by_step": yield_step_summary(tables, yield_rows)}


def _row_total_count(row):
    """행에 있는 소스별 ``{source}_count`` 값을 모두 합산."""
    return sum(int(v or 0) for k, v in row.items() if str(k).endswith("_count"))


def fail_bin_ranking(yield_rows):
    """yield_rows 에서 Fail bin(비-Pass) 만 뽑아 총합 count 내림차순 정렬.

    소스 Merge 는 소스별 ``{source}_count`` 합산 / 평균 ``avg`` 를 사용한다.
    Summary(상위 5)·Yield(0.5% 임계 분리) 가 공유하는 단일 출처.
    """
    fails = [r for r in yield_rows if str(r.get("bin")) != PASS_BIN]
    fails.sort(key=_row_total_count, reverse=True)

    ranked = []
    for r in fails:
        ranked.append({
            "bin": r.get("bin"),
            "item": r.get("Item"),
            "count": _row_total_count(r),
            "yield_pct": r.get("avg"),
        })
    return ranked

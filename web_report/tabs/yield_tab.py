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


def tno_to_item_map(table) -> dict:
    """정규화 TNO → 항목명 리스트 맵 — 테이블 인스턴스 단위 lazy 캐시.

    FAILTNO→항목 귀속(yield/distribution 공통 규칙)의 단일 출처.
    """
    cached = getattr(table, "_tno_to_item_cache", None)
    if cached is None:
        cached = defaultdict(list)
        for item, tno in table.tno.items():
            norm = _tno_norm(tno)
            if norm is not None:
                cached[norm].append(item)
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
        # 확대 파이용: Bin 별 die 수(BIN 컬럼 die당 1회 집계 = 정확). Pass(Bin1) 제외,
        # 합계는 fail 과 정확히 일치. FAILTNO 귀속(중복 가능)과 달리 die 기준.
        counts = Counter(bin_types(t))
        fail_bins = sorted(
            ({"bin": b, "count": int(c)} for b, c in counts.items() if b != PASS_BIN),
            key=lambda d: bin_sort_key(d["bin"]),
        )
        by_source.append({
            "source": t.source,
            "yield_pct": round(t_pass / t_total * 100.0, 2) if t_total else 0.0,
            "pass": t_pass,
            "fail": max(t_total - t_pass, 0),
            "total": t_total,
            "fail_bins": fail_bins,
        })

    return {"yield_pct": yield_pct, "pass": passed, "fail": failed, "total": total,
            "by_source": by_source}


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

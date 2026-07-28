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


def gross_die_value(gross_die):
    """세션 기준정보 gross_die(TEXT) → 양의 정수 또는 None(사용 불가 → rawdata 폴백)."""
    if gross_die is None:
        return None
    try:
        value = int(float(str(gross_die).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ── 수율 분모 기준 (2026-07-28) ────────────────────────────────────────────────
# 분모는 **소스마다 따로** 정해진다: 기본은 제품 기준정보 Gross Die 지만, 그 값이 실제 측정
# die 수(test die)와 크게 어긋나면 자동으로 test die 로 내려간다. 규칙(사용자 확정):
#   1. 분모는 Gross Die 가 기준이다.
#   2. 수율은 100% 를 넘을 수 없다 — 넘으면 분모가 잘못된 것이므로 다른 기준을 쓴다.
#   3. 그 source 의 Gross Die 가 test die 보다 작으면(=2번 상황) test die 를 분모로.
#   4. test die 가 Gross Die 보다 100 개 이상 적으면 test die 를 분모로.
# 3번은 **강제**(사용자가 Gross 를 골라도 test 로 내린다 — 규칙 2 는 표시 불변식이다),
# 4번은 **기본값**(사용자가 Gross 를 명시하면 존중한다 — 100% 를 넘지 않으므로).
GROSS_SHORTFALL_LIMIT = 100
BASIS_GROSS = "gross"          # edits.YIELD_BASIS_GROSS 와 같은 문자열 (저장 계층은 edits)
BASIS_TEST = "test"


def auto_basis(gross, tested):
    """사용자 선택이 없을 때의 (기준, 사유코드). gross 는 gross_die_value 결과(None 가능)."""
    if not gross:
        return BASIS_TEST, "no_gross"
    if gross < tested:
        return BASIS_TEST, "gross_lt_tested"      # 규칙 2·3 — gross 분모면 수율 100% 초과
    if gross - tested >= GROSS_SHORTFALL_LIMIT:
        return BASIS_TEST, "tested_short"         # 규칙 4
    return BASIS_GROSS, ""


def resolve_source_basis(tables, gross_die=None, basis_map=None) -> dict:
    """소스별 분모 결정 — {source: {source,basis,auto,override,forced,gross,tested,total,
    reason,gross_allowed}}.

    basis_map: 세션에 저장된 사용자 선택 ``{"mode": "auto|gross|test", "sources": {name: basis}}``
    (edits.load_yield_basis_map). None 이면 전 소스 auto. mode 가 gross/test 면 소스별 지정이
    없는 소스의 override 로 쓴다(구 세션의 전역 스위치 하위호환).

    ``gross_allowed`` 는 그 소스에 Gross 기준을 고를 수 있는지 — 규칙 2 때문에 UI 가 선택지를
    막는 데 쓴다. 분자(pass/fail die 수)는 어느 경우에도 실측값 그대로다.
    """
    gross = gross_die_value(gross_die)
    basis_map = basis_map or {}
    mode = str(basis_map.get("mode") or "auto").strip().lower()
    overrides = basis_map.get("sources") or {}
    out = {}
    for table in tables:
        tested = len(table.data)
        auto, reason = auto_basis(gross, tested)
        override = overrides.get(table.source)
        if override is None and mode in (BASIS_GROSS, BASIS_TEST):
            override = mode
        override = override if override in (BASIS_GROSS, BASIS_TEST) else None
        gross_allowed = bool(gross and gross >= tested)
        basis, forced = (override or auto), False
        if basis == BASIS_GROSS and not gross_allowed:
            basis, forced = BASIS_TEST, bool(override)   # 규칙 2 — 선택보다 우선
        out[table.source] = {
            "source": table.source,
            "basis": basis,
            "auto": auto,
            "override": override,
            "forced": forced,
            "gross": gross,
            "tested": tested,
            "total": gross if basis == BASIS_GROSS else tested,
            "reason": reason,
            "gross_allowed": gross_allowed,
        }
    return out


def source_totals(tables, gross_die=None, basis_map=None) -> dict:
    """소스별 수율 **분모** — resolve_source_basis 의 total 만 뽑은 것.

    basis_map 을 주지 않으면 전 소스 auto 판정(위 규칙)이다.
    """
    return {src: info["total"]
            for src, info in resolve_source_basis(tables, gross_die, basis_map).items()}


def yield_basis_payload(basis_info, mode="auto") -> dict:
    """payload["yield_basis"] — 프런트 배지/소스별 표가 "이 % 의 분모가 무엇인지" 를 그린다.

    ``basis``(전 소스가 같으면 그 값, 섞이면 "mixed")와 ``gross_die`` 는 소스별 분해가 없던
    옛 프런트가 읽던 키라 그대로 둔다.
    """
    infos = list(basis_info.values())
    kinds = {i["basis"] for i in infos}
    return {
        "basis": (kinds.pop() if len(kinds) == 1 else "mixed") if kinds else BASIS_TEST,
        "mode": mode or "auto",
        "gross_die": infos[0]["gross"] if infos else None,
        "by_source": [{k: i[k] for k in ("source", "basis", "auto", "override", "forced",
                                         "total", "tested", "reason")} for i in infos],
    }


def build_yield_rows(tables, fail_counts, totals=None):
    rows = []
    totals = dict(totals) if totals else {t.source: len(t.data) for t in tables}
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


# ── STEP 별 분해 (Yield 탭 전용, 전체 rawdata 기준 수율) ────────────────────────
# Yield 탭은 STEP(P1/P2/P3) 별로 표를 나누지만, 각 표의 **bin fail 행** portion 은
# build_yield_rows 가 이미 전체(total) die 수를 분모로 계산한 값을 그대로 쓴다(재계산하지
# 않음). 따라서 같은 fail 항목이 Yield 탭·Issue Table·Summary 에서 모두 동일한 % 로
# 표시된다(pass% + 모든 STEP fail% 합 = 100%).
# 반면 **STEP 요약 수율**(yield_step_summary → 요약 박스 STEP 표 + 각 STEP 표 최상단 Pass
# 행)은 2026-07-21 부터 **누적** 기준이다: 분모는 똑같이 전체 rawdata 로 고정하고 분자에서만
# 그 STEP 까지의 fail 을 누적 차감한다 — P1 = (전체 − P1)/전체, P2 = (전체 − P1 − P2)/전체.
# 개별 bin fail 행의 % 는 이 누적과 무관하게 (그 bin 자신의 fail / 전체) 그대로다.
# 2026-07-23: "전체"(분모)는 기본이 **제품 기준정보의 Gross Die** 이고, 값이 없거나 세션
# 옵션이 "Test data 개수" 면 rawdata 행 수로 폴백한다(source_totals). 분자는 항상 실측이다.
# 2026-07-28: 그 분모를 **소스마다** 정한다(resolve_source_basis) — Gross Die 가 test die 와
# 크게 어긋나면 자동으로 test die 로 내려가고, 사용자가 소스별로 고를 수도 있다.

def _step_order_key(step):
    """STEP 정렬 키: P<n> 은 숫자순(P1<P2<P3), 그 외 이름은 알파벳, 빈 값은 맨 뒤."""
    s = str(step or "").strip().upper()
    if not s:
        return (2, 0, "")
    if s.startswith("P") and s[1:].isdigit():
        return (0, int(s[1:]), s)
    return (1, 0, s)


def _step_fail_counts(tables, fail_rows):
    """STEP 순서(P1→P2→P3)와 소스별 STEP fail die 수를 계산.

    fail_rows: build_yield_rows 가 만든 fail 행(``step`` + ``{source}_count`` 보유).
    반환: (ordered_steps, step_fail) — step_fail[source][step] = 그 STEP 의 fail die 합.
    """
    step_fail = {t.source: defaultdict(int) for t in tables}
    steps = set()
    for r in fail_rows:
        step = str(r.get("step") or "")
        steps.add(step)
        for t in tables:
            step_fail[t.source][step] += int(r.get(f"{t.source}_count") or 0)
    ordered = sorted(steps, key=_step_order_key)
    return ordered, step_fail


def build_yield_step_groups(yield_rows):
    """STEP 별로 분리한 Bin 그룹 목록. 각 원소 ``{step, groups: [bin group...]}``.

    STEP 순서는 _step_order_key(P1→P2→P3). 각 STEP 내부는 build_yield_bin_groups 와 동일한
    Bin 대표+펼침 구조이며, portion 은 build_yield_rows 의 전체(total) 기준 값을 그대로 쓴다
    (재계산 없음 — yield_rows 원본은 변형하지 않도록 복사본으로만 그룹핑).
    """
    fail_rows = [r for r in (yield_rows or [])
                 if str(r.get("bin")).strip() != PASS_BIN and r.get("Item")]
    ordered = sorted({str(r.get("step") or "") for r in fail_rows}, key=_step_order_key)
    by_step = defaultdict(list)
    for r in fail_rows:
        by_step[str(r.get("step") or "")].append(dict(r))
    out = []
    for step in ordered:
        rows = by_step.get(step) or []
        if not rows:
            continue
        out.append({"step": step, "groups": build_yield_bin_groups(rows)})
    return out


def yield_step_summary(tables, yield_rows, totals=None):
    """상단 요약 박스용 STEP 요약 행: STEP 별 fail die 수 + 전체 rawdata 기준 **누적** 수율%.

    분모는 항상 전체 die 수(total)로 고정하고, 분자에서만 그 STEP 까지의 fail 을 누적
    차감한다: step_yield_pct = (total - Σ(P1..이 STEP 의 fail)) / total * 100.
    예) 1000 die, P1 fail 100 / P2 fail 50 / P3 fail 10 → 90% / 85% / 84%.
    STEP 순서는 _step_order_key(P1<P2<P3<빈 STEP)이며 빈 STEP("")도 맨 뒤에서 누적에 든다.
    소스 여러 개면 pooled(소스 합산)와 소스별 누적을 각각 따로 굴린다.

    ``sources``: STEP×Source 표시용 소스별 분해(각 소스 전체 die 분모 + 그 소스의 누적 fail).
    ``avg_yield_pct``: 소스별 누적 yield 의 산술평균(병합 Step 셀에 표시). 소스 순서는
    tables 순서 유지(모든 STEP 에서 동일 소스 컬럼 위치).

    키 계약: ``entered``(=전체 die, 전 STEP 동일)와 ``fail``(=그 STEP **자체** fail)은 의미가
    그대로고, ``survivor``/``yield_pct``/``step_yield_pct``/``avg_yield_pct`` 가 누적 기준으로
    바뀐다. ``cum_fail``(누적 fail)을 새로 병기해 survivor + cum_fail == entered 가 pooled·
    소스별 양쪽에서 항상 성립한다(= 화면의 "Pass / In" 과 "Fail" 이 모순되지 않게 하는 키).

    ``totals`` 로 소스별 분모를 주면 그 값을 entered 로 쓴다(Gross Die 기준, source_totals).
    주지 않으면 종전처럼 rawdata 행 수다.
    """
    fail_rows = [r for r in (yield_rows or [])
                 if str(r.get("bin")).strip() != PASS_BIN and r.get("Item")]
    ordered, step_fail = _step_fail_counts(tables, fail_rows)
    src_totals = dict(totals) if totals else {t.source: len(t.data) for t in tables}
    total = sum(src_totals.values())
    cum_by_src = {t.source: 0 for t in tables}   # 소스별 누적 fail (STEP 순회하며 증가)
    out = []
    for step in ordered:
        for t in tables:
            cum_by_src[t.source] += int(step_fail[t.source].get(step, 0))
        fail = sum(int(step_fail[t.source].get(step, 0)) for t in tables)
        cum_fail = sum(cum_by_src[t.source] for t in tables)
        survivor = max(total - cum_fail, 0)
        src_rows = []
        for t in tables:
            t_total = src_totals[t.source]
            t_fail = int(step_fail[t.source].get(step, 0))
            t_cum = cum_by_src[t.source]
            t_surv = max(t_total - t_cum, 0)
            src_rows.append({
                "source": t.source,
                "entered": t_total,
                "fail": t_fail,
                "cum_fail": t_cum,
                "survivor": t_surv,
                "yield_pct": round(t_surv / t_total * 100.0, 2) if t_total else 0.0,
            })
        avg_pct = round(sum(s["yield_pct"] for s in src_rows) / len(src_rows), 2) if src_rows else 0.0
        out.append({
            "step": step,
            "entered": total,
            "fail": fail,
            "cum_fail": cum_fail,
            "survivor": survivor,
            "step_yield_pct": round(survivor / total * 100.0, 2) if total else 0.0,
            "avg_yield_pct": avg_pct,
            "sources": src_rows,
        })
    return out


def yield_overview(tables, yield_rows, totals=None):
    """Yield 탭 상단 요약 박스: 전체 pass/fail/total count + 종합 yield% + 소스별 수율.

    yield_rows[0] 은 build_yield_rows 가 항상 추가하는 Pass 행이므로 그 값을 소스별로 합산한다.
    by_source: 소스마다 {source, yield_pct, pass, fail, total, tested} (Total Yield 소스별 표시용).

    ``totals`` 로 소스별 분모(Gross Die 기준)를 주면 ``total``·``yield_pct`` 가 그 값을 쓴다.
    ``pass``/``fail`` 은 **언제나 실측 die 수**(fail = 측정 die − pass)이므로, Gross Die 기준
    에서는 pass + fail < total 일 수 있다(= 미측정 die). 그 간극을 감추지 않도록 실제 측정
    die 수를 ``tested`` 로 병기한다.
    """
    src_totals = dict(totals) if totals else {t.source: len(t.data) for t in tables}
    total = sum(src_totals.get(t.source, len(t.data)) for t in tables)
    tested = sum(len(t.data) for t in tables)
    pass_row = yield_rows[0] if yield_rows else {}
    passed = sum(int(pass_row.get(f"{t.source}_count") or 0) for t in tables)
    failed = max(tested - passed, 0)
    yield_pct = round(passed / total * 100.0, 2) if total else 0.0

    by_source = []
    for t in tables:
        t_total = src_totals.get(t.source, len(t.data))
        t_tested = len(t.data)
        t_pass = int(pass_row.get(f"{t.source}_count") or 0)
        by_source.append({
            "source": t.source,
            "yield_pct": round(t_pass / t_total * 100.0, 2) if t_total else 0.0,
            "pass": t_pass,
            "fail": max(t_tested - t_pass, 0),
            "total": t_total,
            "tested": t_tested,
        })

    return {"yield_pct": yield_pct, "pass": passed, "fail": failed, "total": total,
            "tested": tested, "by_source": by_source,
            "by_step": yield_step_summary(tables, yield_rows, totals=src_totals)}


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

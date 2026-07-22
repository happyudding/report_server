"""Distribution tab payload builder.

두 계층을 제공한다:
- ECDF 컴팩트(``build_distribution_compact``): lazy 엔드포인트 ``GET .../web_report/distribution``
  전용 columnar 페이로드. 갤러리 미니셀 + Issue Table 산포 카드가 ``distDataCache`` 로 소비한다.
  다운샘플 없음(불변 규칙 #6).
- 산포 탭 인덱스/상세(``build_distribution_index`` / ``fail_items`` / ``scatter_item``): 갤러리
  카드의 status/cpk 분류와, 카드 클릭 시 지연 로드하는 항목별 전체 측정값(상세 CDF+히스토그램)을
  공급한다. cpk 는 이미 계산된 ``cpk_rows`` 를 재사용하고, fail 은 FAILTNO==항목 TNO 로 귀속한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import _is_passfail_unit, fmt_type, json_safe, num, round_num
from .cpk import CPK_THRESHOLD, _stats, worst_cpk_by_subject
from .raw_data import _META_COLUMNS
from .yield_tab import _tno_norm, failtno_norms, tno_to_item_map

# Item_detail 의 Fail rawdata 표 상한 (초대형 Fail 항목 페이로드 폭증 방지)
_FAIL_ROW_CAP = 2000


def to_numeric_clean(series):
    """Series → float64 배열 (유한값만, NaN·inf 제거)."""
    # split_honeyform 이 item 컬럼을 이미 numeric dtype 으로 만들므로 대부분 변환 생략
    # (int/float 만 지름길 — bool 등은 기존 to_numeric 경로 유지).
    if getattr(series.dtype, "kind", "") in "if":
        arr = series.to_numpy()
    else:
        arr = pd.to_numeric(series, errors="coerce").to_numpy()
    return arr[np.isfinite(arr)]


def cumulative_distribution_full(values):
    """고유값별 누적 분포(ECDF) 계산. 반환: (unique_vals, cumulative_percent)."""
    if values.size == 0:
        return np.empty(0), np.empty(0)
    unique_vals, counts = np.unique(values, return_counts=True)
    cum = np.cumsum(counts) / values.size * 100.0
    return unique_vals, cum


def build_distribution_compact(tables, all_items, *, bin1_only=False) -> dict:
    """ECDF 전량(다운샘플 없음, 불변 규칙 #6)을 columnar 포맷으로 반환.

    행마다 반복되던 subject/source/units/limits 키를 제거한 컴팩트 표현으로,
    lazy 엔드포인트 ``GET .../web_report/distribution`` 전용이다 (208MB → 수십 MB).

    ``bin1_only`` 이면 각 소스에서 BIN==PASS_BIN(양품) **그리고** 이 항목 규격(LSL/USL)
    이내인 die 의 측정값만으로 ECDF 를 계산한다 — Distribution 탭 "Bin1 only" 토글용.
    (규격 밖으로 벗어난 양품 die 도 제외해 "양품·규격내 산포"만 남긴다.) 이 규격 필터는
    성능용 다운샘플이 아니라 bin1 모드 전용 의미 필터다 — 전체 모드는 여전히 전 포인트를
    빠짐없이 표시한다(불변 규칙 #6). units/limits(spec)은 bin 과 무관하므로 전체 기준과
    동일하게 항목 메타에서 취한다.
    """
    from .common import PASS_BIN, bin_types

    bin1_masks = {}
    if bin1_only:
        # BIN 마스크는 item 과 무관 — 테이블당 1회만 계산 (item 루프 안에서 재계산 금지)
        for table in tables:
            bin1_masks[id(table)] = np.asarray(
                [b == PASS_BIN for b in bin_types(table)], dtype=bool)

    items = {}
    for item in all_items:
        sources = {}
        units = ""
        lo = hi = None
        first = True
        for table in tables:
            if item not in table.item_columns:
                continue
            if first:
                units = json_safe(table.units.get(item)) or ""
                lo = round_num(table.lolim.get(item))
                hi = round_num(table.hilim.get(item))
                first = False
            col = table.data[item]
            if bin1_only:
                # 양품(BIN==PASS_BIN) & 규격(LSL/USL) 이내 die 만 — 규격 밖 양품 die 도 제외.
                numeric = pd.to_numeric(col, errors="coerce").to_numpy()
                m = np.isfinite(numeric) & bin1_masks[id(table)]
                ilo = num(table.lolim.get(item))
                ihi = num(table.hilim.get(item))
                if ilo is not None:
                    m &= (numeric >= ilo)
                if ihi is not None:
                    m &= (numeric <= ihi)
                values = numeric[m]
            else:
                values = to_numeric_clean(col)
            unique_vals, cum = cumulative_distribution_full(values)
            # 수백만 포인트를 파이썬 round_num 루프로 돌리면 요청당 수 초가 걸려 numpy 로
            # 벡터화한다 — to_numeric_clean 이 유한 float64 만 반환하므로(NaN/inf 없음)
            # np.round(half-even)는 round_num 의 round()와 동일한 값을 낸다.
            sources[table.source] = {
                "x": np.round(unique_vals, 6).tolist(),
                "y": np.round(cum, 3).tolist(),
            }
        if sources:
            items[item] = {"units": units, "lo": lo, "hi": hi, "sources": sources}
    return {"format": "ecdf-columnar-v1", "items": items}


def fail_items(tables) -> set:
    """FAILTNO 가 항목의 TNO 와 일치하는 칩이 하나라도 있으면 그 항목을 fail 로 본다.

    yield_tab 의 fail 귀속(``FAILTNO`` == ``TNO``)과 동일한 규칙을 재사용한다. 소스 합집합.
    (칩 자체의 fail 정의는 FAILTNO 존재 또는 BIN≠1 이지만, 특정 항목에 귀속되는 것은 FAILTNO 뿐.)
    """
    failed: set = set()
    for table in tables:
        tno_to_item = tno_to_item_map(table)
        for norm in failtno_norms(table):
            if norm is None:
                continue
            for item in tno_to_item.get(norm, []):
                failed.add(item)
    return failed


def _status(is_fail, cpk) -> str:
    if is_fail:
        return "fail"
    if cpk is not None and cpk < CPK_THRESHOLD:
        return "cpk_low"
    return "ok"


def _first_table_for(tables, item):
    for table in tables:
        if item in table.item_columns:
            return table
    return None


def tseq_sort_key(tables):
    """항목 → TEST SEQ(TSEQ, 메타 row0) 정렬 키 함수.

    Distribution 갤러리를 TSEQ 순으로 표시하기 위한 것. 숫자로 해석되면 숫자 우선, 아니면
    뒤로 보내 이름순으로 안정 정렬한다. 소스마다 값이 같다는 보장은 없어 항목이 처음
    등장한 테이블의 TSEQ 를 쓴다.
    """
    tseq_of = {}
    for table in tables:
        for item in table.item_columns:
            tseq_of.setdefault(item, table.tseq.get(item))

    def key(item):
        try:
            return (0, float(tseq_of.get(item)), str(item))
        except (TypeError, ValueError):
            return (1, 0.0, str(item))
    return key


def build_distribution_index(tables, cpk_rows, exclude=None) -> list:
    """갤러리/툴바/타입어헤드용 항목 인덱스. subject 당 1행 (경량, 점 배열 없음).

    cpk 는 ``cpk_rows`` 재사용(재계산 없음), fail 은 ``fail_items`` 로 귀속.
    항목 순서는 TEST SEQ(TSEQ) 순 — 갤러리가 이 순서대로 표시된다.
    ``exclude`` 에 담긴 항목(Pass/Fail unit·측정 data 전무)은 인덱스에서 제외한다.
    """
    exclude = exclude or set()
    worst = worst_cpk_by_subject(cpk_rows)
    failed = fail_items(tables)
    all_items = sorted({c for t in tables for c in t.item_columns if c not in exclude},
                       key=tseq_sort_key(tables))

    rows = []
    for item in all_items:
        meta_t = _first_table_for(tables, item)
        n = 0
        for table in tables:
            if item in table.item_columns:
                n += int(to_numeric_clean(table.data[item]).size)
        cpk = worst.get(item)
        is_fail = item in failed
        rows.append({
            "subject": item,
            "test_num": fmt_type(meta_t.tno.get(item)) if meta_t else "",
            "units": (json_safe(meta_t.units.get(item)) if meta_t else None) or "",
            "lower_limit": round_num(meta_t.lolim.get(item)) if meta_t else None,
            "upper_limit": round_num(meta_t.hilim.get(item)) if meta_t else None,
            "n": n,
            "cpk": round_num(cpk, 3),
            "is_fail": is_fail,
            # Pass/Fail 단위 항목 표시 여부 — 프런트 "P/F 없애기" 토글(기본 ON)이 이 플래그로
            # 필터한다. 항목은 인덱스/ECDF 에 포함되고 숨김/표시는 프런트가 결정한다.
            "is_passfail": _is_passfail_unit(meta_t.units.get(item)) if meta_t else False,
            "status": _status(is_fail, cpk),
        })
    return rows


def scatter_item(tables, subject, *, fail_row_cap: int = _FAIL_ROW_CAP,
                 bin1: bool = False) -> dict:
    """Item_detail 용: 항목의 소스별 전체 측정값(다운샘플 없음) + 통계 + cpk/status +
    이 항목으로 Fail 된 die 의 rawdata 행(전 metadata + 측정값).

    ``bin1`` 이면 분포(values/serial/xpos/ypos)를 양품(BIN==PASS_BIN) **그리고** 규격
    (LSL/USL) 이내인 die 만으로 낸다("Bin1 only" 상세, CDF/히스토그램 표시용). 규격 필터는
    성능 다운샘플이 아니라 이 모드 전용 의미 필터다. ``stats``/``cpk`` 는 규격 클리핑을 하지
    않고 양품(BIN==PASS_BIN) 기준을 유지한다(규격 클리핑은 cpk 를 왜곡하므로).
    ``fail_rows``/``fail_total``/``is_fail`` 은 "이 항목으로 fail 한 die" 진단이라 bin 필터와
    무관하게 전체 기준을 유지한다(status 는 all-data is_fail + bin1 cpk 조합).

    - ``stats``: 소스별 _stats (n/min/median/max/average/stdev/cp/cpl/cpu/cpk).
    - ``sources[].serial/xpos/ypos``: CDF hover 용으로 ``values`` 와 동일 순서·길이로 정렬된
      die 식별 metadata (Item_detail CDF 차트 전용, histogram 은 미사용).
    - ``fail_rows``: FAILTNO 가 이 항목의 TNO 와 일치하는 행(= 이 항목으로 fail 된 die).
      ``_META_COLUMNS`` + SOURCE + 측정값. ``fail_row_cap`` 상한 초과 시 잘리고
      ``fail_truncated=True`` (전체 개수는 ``fail_total``).
    항목이 어떤 소스에도 없으면 ``KeyError`` (라우트가 404 처리).
    """
    from .common import PASS_BIN, bin_types

    matched = [t for t in tables if subject in t.item_columns]
    if not matched:
        raise KeyError(subject)
    meta_t = matched[0]

    sources = []
    stats = []
    cpks = []
    fail_rows = []
    fail_total = 0
    for table in matched:
        col = table.data[subject]
        # to_numeric_clean 과 동일한 유한값 필터를 mask 로 남겨 SERIAL/XPOS/YPOS 를
        # values 와 같은 순서·길이로 정렬한다 (hover 용, Item_detail CDF 전용).
        numeric = pd.to_numeric(col, errors="coerce")
        finite_mask = np.isfinite(numeric.to_numpy())
        # Bin1 only: 양품(BIN==PASS_BIN) & 규격(LSL/USL) 이내 die 만 분포(CDF/히스토그램)에
        # 반영 (disp_mask = 유한 ∩ 양품 ∩ 규격내). 통계(stat_col)는 규격 클리핑 없이 양품
        # 기준만 유지 — 규격 클리핑은 cpk 를 왜곡하므로("cpk 유지").
        disp_mask = finite_mask
        stat_col = col
        if bin1:
            bin1_mask = np.asarray([b == PASS_BIN for b in bin_types(table)], dtype=bool)
            disp_mask = finite_mask & bin1_mask
            arr = numeric.to_numpy()
            ilo = num(table.lolim.get(subject))
            ihi = num(table.hilim.get(subject))
            if ilo is not None:
                disp_mask = disp_mask & (arr >= ilo)
            if ihi is not None:
                disp_mask = disp_mask & (arr <= ihi)
            stat_col = col[bin1_mask]
        values = numeric.to_numpy()[disp_mask]
        sources.append({
            "name": table.source,
            "values": values.round(6).tolist(),
            "serial": [fmt_type(v) for v in table.data["SERIAL"].to_numpy()[disp_mask]],
            "xpos": [fmt_type(v) for v in table.data["XPOS"].to_numpy()[disp_mask]],
            "ypos": [fmt_type(v) for v in table.data["YPOS"].to_numpy()[disp_mask]],
        })
        st = _stats(stat_col, table.lolim.get(subject), table.hilim.get(subject))
        # report용 정규분포 곡선(프론트)의 축퇴 판정: n<2 또는 std<=0 이면 곡선을
        # 그리지 못하므로 서버가 degenerate 로 표시(프론트는 스파이크로 대체).
        # stdev 는 표본표준편차(ddof=1) — n≤1이면 None, 전부 동일값이면 0.
        degenerate = (st["n"] is None or st["n"] < 2
                      or st["stdev"] is None or st["stdev"] <= 0)
        stats.append({"source": table.source, "degenerate": degenerate, **st})
        if st["cpk"] is not None:
            cpks.append(st["cpk"])

        # 이 항목으로 Fail 된 die: FAILTNO(정규화) == 항목 TNO(정규화). fail_items 귀속과 동일.
        item_tno = _tno_norm(table.tno.get(subject))
        if item_tno is None:
            continue
        data = table.data
        failtno_norm = failtno_norms(table)
        # 매칭 인덱스를 먼저 수집 — fail 0건(대부분의 항목 클릭)이나 cap 도달 시 전 행
        # meta 컬럼 .tolist() 물질화(O(rows×meta_cols))를 피한다. 값 변환은 기존과
        # 동일하게 .tolist() 경유(numpy→native)라 payload 표기가 변하지 않는다.
        idxs = [i for i, fn in enumerate(failtno_norm) if fn == item_tno]
        fail_total += len(idxs)
        take = idxs[:max(0, fail_row_cap - len(fail_rows))]
        if take:
            meta_slice = {c: data[c].iloc[take].tolist() for c in _META_COLUMNS}
            vals_slice = col.iloc[take].tolist()
            for j in range(len(take)):
                row = {"SOURCE": table.source}
                for c in _META_COLUMNS:
                    row[c] = fmt_type(meta_slice[c][j])
                row["value"] = round_num(vals_slice[j])
                fail_rows.append(row)

    cpk = min(cpks) if cpks else None
    is_fail = fail_total > 0
    return {
        "subject": subject,
        "test_num": fmt_type(meta_t.tno.get(subject)),
        "units": json_safe(meta_t.units.get(subject)) or "",
        "lower_limit": round_num(meta_t.lolim.get(subject)),
        "upper_limit": round_num(meta_t.hilim.get(subject)),
        "cpk": round_num(cpk, 3),
        "is_fail": is_fail,
        "status": _status(is_fail, cpk),
        "sources": sources,
        "stats": stats,
        "fail_rows": fail_rows,
        "fail_total": fail_total,
        "fail_truncated": fail_total > len(fail_rows),
    }

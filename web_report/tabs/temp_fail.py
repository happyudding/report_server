"""Temperature 모드 CT/HT 전 항목 RT limit 재판정 (조회 시점, 서버) — 2026-08-05.

업로드 직전 정리(``web_report.temperature._clean_member``)는 CT/HT 행을 RT pass 좌표로
자른 뒤 **TSEQ 순서상 첫 fail 항목 하나만** BIN/FAILTNO 에 적는다. 그래서 앞 순번 항목에서
죽은 die 의 뒷 항목 이탈이 통째로 가려졌다. 이 모듈은 그 판정을 **조회 시점에 다시, 전
항목에 대해** 수행한다:

- 좌표 필터(①)는 이미 저장된 parquet 에 반영돼 있으므로 다시 하지 않는다.
- 판정(②)만 **모든 항목**에 대해 되풀이한다 — 한 die 가 3개 항목을 벗어났으면 3개 항목
  모두에 계상된다. 따라서 **소스별 fail% 합계가 100% 를 넘을 수 있다**(2026-08-05 사용자
  확정 — 가려지는 항목이 없는 쪽이 목적).
- 저장된 CT/HT BIN/FAILTNO 는 건드리지 않는다. 클라 정리 로직도 그대로다(기존 세션과
  신규 세션의 저장 데이터가 갈리지 않게).

판정 규칙은 ``temperature._clean_member`` 의 미러다(첫 fail 제한만 제거) — 한쪽을 고치면
다른 쪽도 같이 봐야 두 경로의 판정이 갈리지 않는다.

Bin 표기 규칙 (2026-08-05 사용자 확정):
  ① manifest ``temperature_limits`` 의 ``usl_bin`` — ``.lt`` 의 ``20:19`` 는 **콜론 오른쪽
     (USL 위반 bin, 19)만** 반영한다. 콜론이 없으면 lsl/usl 이 같은 값이라 그대로 단일 bin.
  ② 없으면 관측 bin — 그 소스(없으면 RT)에서 같은 item 으로 실제 죽은 bin 의 최빈값.
     ``"999"``(클라가 붙인 미상 표식)는 제외한다.
  ③ 그래도 없으면 공백. 999 를 화면에 쓰지 않는다.

한 항목은 항상 **1행**이다 — row_key ``TEMP|<item>`` 이 comment/Status/숨김의 키라
bin 별로 행을 나누면 기존에 저장된 편집값과 파서 4곳(sheets.js·eval_export·chatbot·
service 가드)이 전부 깨진다.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

from .common import PASS_BIN, fmt_type, item_meta as _item_meta, num
from .issue_table import _blank_row, _comment_values
from .yield_tab import _tseq_sort_key
from ..temperature import UNKNOWN_BIN, bin_lookup, match_item

# 이 시트(탭) 이름 — TAB_REGISTRY / 프런트 렌더가 공유하는 단일 문자열.
TEMP_SHEET = "Issue Table Temp"


def temp_member_pairs(tables, groups) -> list:
    """[(rt_table, member_table)] — groups 순서. tables 에 없는 이름은 건너뛴다.

    corner("CT"/"HT") 라벨은 여기서 만들지 않는다 — 판정에 쓰이지 않고, 그 폴백 규칙을
    복제해 두면 metrics 의 source 태깅과 조용히 갈릴 수 있다(정본은 metrics 1곳).
    """
    by_name = {t.source: t for t in tables or []}
    out = []
    for group in groups or []:
        rt = by_name.get(str(group.get("rt") or ""))
        if rt is None:
            continue
        for name in (str(m) for m in (group.get("members") or [])):
            member = by_name.get(name)
            if member is None or member is rt:
                continue
            out.append((rt, member))
    return out


def judge_items(rt_table, member_table) -> list:
    """판정 대상 [(item, lo, hi)] — 양쪽에 있고 RT limit 이 하나라도 있는 항목, RT TSEQ 순."""
    member_items = set(member_table.item_columns)
    items = []
    for item in rt_table.item_columns:
        if item not in member_items:
            continue
        lo, hi = num(rt_table.lolim.get(item)), num(rt_table.hilim.get(item))
        if lo is None and hi is None:
            continue
        items.append((item, lo, hi))
    items.sort(key=lambda t: _tseq_sort_key(rt_table, t[0]))
    return items


def iter_fail_masks(rt_table, member_table):
    """(item, fail_lo, fail_hi) 스트리밍 — 항목당 float 배열 1개만 상주시킨다.

    전 항목 행렬((행수 × 항목수) float)을 한 번에 만들면 대형 세션에서 메모리를 그대로
    잡아먹는다(원본 _clean_member 주석과 같은 이유). NaN(측정 결측)은 비교가 False 라
    pass 로 남는다 — 클라 판정과 동일.
    """
    for item, lo, hi in judge_items(rt_table, member_table):
        values = pd.to_numeric(member_table.data[item], errors="coerce").to_numpy(dtype=float)
        n = values.size
        fail_lo = values < lo if lo is not None else np.zeros(n, dtype=bool)
        fail_hi = values > hi if hi is not None else np.zeros(n, dtype=bool)
        yield item, fail_lo, fail_hi


def _observed_bins(counts) -> dict:
    """fail_counts(Counter[(bin, item)]) → {item: 최빈 fail bin}. Pass/999 는 제외.

    metrics 가 이미 계산해 둔 소스별 fail_counts 를 그대로 쓰므로 추가 스캔이 없다.
    """
    by_item = defaultdict(Counter)
    for (bin_value, item), n in (counts or {}).items():
        b = str(bin_value)
        if not b or b == PASS_BIN or b == UNKNOWN_BIN:
            continue
        by_item[item][b] += n
    return {item: c.most_common(1)[0][0] for item, c in by_item.items()}


def _merged_counts(fail_counts, sources) -> Counter:
    """여러 소스의 fail_counts 를 (bin, item) 키로 합산."""
    merged = Counter()
    for src in sources or ():
        merged.update(fail_counts.get(src) or {})
    return merged


def _bin_of(item, index, *observed) -> str:
    """항목의 표시 Bin — limits 매핑(usl_bin) → 관측 bin(인자 순) → 공백."""
    entry = match_item(item, index)
    if entry:
        # .lt "20:19" 는 콜론 오른쪽(USL 위반 bin)만 반영한다 (2026-08-05 사용자 확정).
        b = fmt_type(entry.get("usl_bin") or entry.get("lsl_bin"))
        if b and b != UNKNOWN_BIN:
            return b
    for obs in observed:
        b = (obs or {}).get(item)
        if b:
            return str(b)
    return ""


def compute_temp_fail(tables, groups) -> list:
    """판정 **1회 순회**의 단일 산출물 — ``[{source, n, items:[{item, count, idx}]}]``.

    표(집계)와 Map(die 인덱스)이 같은 결과를 쓴다. 종전에는 ``build_temp_fail_rows`` 와
    ``temp_fail_indices`` 가 각각 전 항목을 순회해 **같은 판정을 두 번**(그룹 7개 세션이면
    항목당 float 컬럼 복사까지 두 벌) 돌았다.

    - ``count`` = 전 행 기준 fail die 수 (표의 분자 — 좌표 결측 행도 포함).
    - ``idx``   = **좌표 유효 행만 추린 뒤의** 인덱스. 기준은
      ``Map_analysis.build_map_analysis_rows`` 의 ``XPOS/YPOS notna`` mask 와 **문자 그대로
      같아야** dies 배열과 정합한다(한쪽을 고치면 다른 쪽도 같이 볼 것).

    결과는 tables 클론(요청 단위, loader.clone_table)에 캐시한다 — 콜드 빌드가 만든 것을
    같은 요청의 temp_map 시딩이 재계산 없이 그대로 쓴다(common.bin_types 와 같은 규약).
    """
    if not tables:
        return []
    key = json.dumps(groups, sort_keys=True, default=str)
    cached = getattr(tables[0], "_temp_fail_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1]

    out = []
    for rt, member in temp_member_pairs(tables, groups):
        data = member.data
        x_num = pd.to_numeric(data["XPOS"], errors="coerce")
        y_num = pd.to_numeric(data["YPOS"], errors="coerce")
        keep = (x_num.notna() & y_num.notna()).to_numpy()
        items = []
        for item, fail_lo, fail_hi in iter_fail_masks(rt, member):
            fail = fail_lo | fail_hi
            count = int(np.count_nonzero(fail))
            if not count:
                continue
            items.append({"item": item, "count": count, "idx": np.flatnonzero(fail[keep])})
        out.append({"source": member.source, "n": int(keep.sum()), "items": items})
    tables[0]._temp_fail_cache = (key, out)
    return out


def temp_fail_counts(tables, groups, packs=None) -> tuple:
    """({item: {source: fail die 수}}, [CT/HT source 이름]) — 표가 쓰는 집계 형태.

    source 순서는 groups 배치 순서(= 화면 컬럼 순서)다.
    """
    agg: dict = {}
    sources: list = []
    for pack in (packs if packs is not None else compute_temp_fail(tables, groups)):
        src = pack["source"]
        if src not in sources:
            sources.append(src)
        for entry in pack["items"]:
            agg.setdefault(entry["item"], {})[src] = entry["count"]
    return agg, sources


def build_temp_fail_rows(tables, groups, totals=None, *, fail_counts=None,
                         limits_meta=None, hidden=(), status_of=None,
                         issue_comments=None, ai_comments=None, packs=None) -> list:
    """Issue Table Temp 시트 행 — 첫 행은 섹션 divider(``Category="TEMP"``).

    컬럼 소스는 **CT/HT 만**이고, 정렬은 소스 합산 fail die 수 내림차순이다.
    fail 이 한 건도 없는 항목은 행을 만들지 않는다. 값이 없으면(그룹·fail 부재) 빈
    리스트를 돌려 프런트가 "데이터 없음"으로 처리하게 한다.
    ``packs`` 를 주면 그 판정 결과를 쓴다(compute_temp_fail — 재계산 없음).
    """
    agg, sources = temp_fail_counts(tables, groups, packs)
    if not agg or not sources:
        return []

    totals = totals or {}
    fail_counts = fail_counts or {}
    index = bin_lookup(limits_meta)
    hidden = set(hidden or ())
    ai = ai_comments is not None
    meta = _item_meta(tables)
    # 관측 bin 폴백: member 소스 합산 → RT 소스 합산 순으로 본다.
    rt_names = [rt.source for rt, _m in temp_member_pairs(tables, groups)]
    member_obs = _observed_bins(_merged_counts(fail_counts, sources))
    rt_obs = _observed_bins(_merged_counts(fail_counts, rt_names))

    ordered = sorted(agg.items(),
                     key=lambda kv: (-sum(kv[1].values()), str(kv[0])))
    rows = [{"Category": "TEMP", "Step": "", "Bin": "", "TNO": "", "Item": "", "avg": "",
             **_blank_row(sources, ai)}]
    for item, counts in ordered:
        if f"TEMP|{item}" in hidden:
            continue
        m = meta.get(item, {})
        portions = []
        values = {}
        for src in sources:
            total = totals.get(src) or 0
            count = int(counts.get(src, 0))
            portion = round(count / total * 100.0, 2) if total else 0.0
            values[f"{src}_yield"] = portion
            portions.append(portion)
        data = {
            "Category": "",
            "Step": fmt_type(m.get("step")),
            "Bin": _bin_of(item, index, member_obs, rt_obs),
            "TNO": fmt_type(m.get("tno")),
            "Item": item,
            "avg": round(sum(portions) / len(portions), 2) if portions else "",
        }
        data.update(_blank_row(sources, ai))
        data.update(values)
        data["Status"] = status_of(f"TEMP|{item}") if status_of else "Open"
        data.update(_comment_values(issue_comments, f"TEMP|{item}", ai_comments))
        rows.append(data)
    return rows if len(rows) > 1 else []


def temp_fail_indices(tables, groups, packs=None) -> list:
    """Map 용 JSON — ``[{source, n, items:[{item, idx:[...]}]}]`` (die **인덱스** 배열).

    좌표를 실어 보내지 않고 인덱스만 보내 payload 를 최소화한다(다운샘플 아님 — 규칙 #6).
    판정은 ``compute_temp_fail`` 한 곳이고 여기서는 JSON 직렬화 형태로 옮기기만 한다.
    """
    out = []
    for pack in (packs if packs is not None else compute_temp_fail(tables, groups)):
        items = [{"item": e["item"], "idx": e["idx"].tolist()}
                 for e in pack["items"] if e["idx"].size]
        out.append({"source": pack["source"], "n": pack["n"], "items": items})
    return out

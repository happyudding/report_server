"""Distribution "Serial 순"(rawdata 누적 순) compact 빌더 (2026-08-24).

Distribution 갤러리의 기본 미니셀은 ECDF(값 오름차순 × 누적%)라 **측정 순서 정보가 없다**
(`build_distribution_compact` 이 `np.unique` 로 동일값을 접기 때문). 사용자가 보고 싶어하는
"각 source 의 rawdata 가 쌓인 순서대로 값이 어떻게 흘러갔는가"(run chart, x=측정 순서 /
y=측정값)는 그 payload 로는 그릴 수 없으므로, **행 순서를 보존한 값 배열**을 따로 낸다.

- 반환 포맷 ``seq-columnar-v1``: ``{"items": {item: {"units","lo","hi","sources":{src:{"v":[…]}}}}}``
  x 축은 배열 인덱스(프런트가 1..n 으로 만든다) — 서버가 인덱스 배열을 실어 보내면 payload
  가 두 배가 되므로 값만 보낸다.
- **다운샘플 없음**(불변 규칙 #5). 표시용 캡은 프런트 미니셀(distribution.js `distHardCap`)뿐.
- 항목 선택·bin1 필터의 의미론은 `dist_blob.compute_dist_compact` / `tabs.distribution.
  build_distribution_compact` 와 **완전히 동일**하다(규칙 #13 — 같은 항목이 탭마다 다른
  기준으로 보이지 않게). 값 필터도 Item_detail(`tabs.distribution.scatter_item` 의
  ``disp_mask``)과 같은 규칙이라, 같은 항목을 갤러리에서 보든 상세에서 보든 점 집합이 같다.

⚠️ **이 모듈을 `web_report/tabs/` 로 옮기지 말 것** — perf_guard S01 이 tabs 변경마다
`REPORT_SCHEMA_VERSION` bump 를 요구하고, 그 bump 는 전 세션 콜드 재빌드 폭풍이 된다
(`gap_chart.py` 와 같은 이유). 이 계산은 report payload 와 무관한 지연 조회 전용이다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SEQ_FORMAT = "seq-columnar-v1"


def build_seq_compact(tables, all_items, *, bin1=False, bin1_sources=None) -> dict:
    """행 순서를 보존한 소스별 측정값 배열(다운샘플 없음)을 columnar 로 반환.

    ``bin1`` 이면 각 소스에서 BIN==PASS_BIN(양품) **그리고** 그 항목 규격(LSL/USL) 이내인
    die 의 값만 남긴다 — `build_distribution_compact` 의 bin1 필터와 같은 의미 필터다
    (성능용 다운샘플이 아니다). ``bin1_sources`` 를 주면 그 소스에만 필터를 건다
    (Temperature "Bin1(RT만)"). ``None`` 이면 전 소스.
    """
    from .tabs.common import PASS_BIN, bin_types, json_safe, num, round_num

    def _use_bin1(table):
        return bin1 and (bin1_sources is None or table.source in bin1_sources)

    # BIN 마스크는 item 과 무관 — 테이블당 1회만 계산(item 루프 안에서 재계산 금지).
    bin1_masks = {}
    if bin1:
        for table in tables:
            if _use_bin1(table):
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
            arr = pd.to_numeric(table.data[item], errors="coerce").to_numpy()
            mask = np.isfinite(arr)
            if _use_bin1(table):
                mask &= bin1_masks[id(table)]
                ilo = num(table.lolim.get(item))
                ihi = num(table.hilim.get(item))
                if ilo is not None:
                    mask &= (arr >= ilo)
                if ihi is not None:
                    mask &= (arr <= ihi)
            # 행 순서 그대로(정렬·집약 없음) — 이 순서가 이 payload 의 존재 이유다.
            sources[table.source] = {"v": np.round(arr[mask], 6).tolist()}
        if sources:
            items[item] = {"units": units, "lo": lo, "hi": hi, "sources": sources}
    return {"format": SEQ_FORMAT, "items": items}


def compute_seq_compact(tables, selected_items, mode, *, only=None, bin1=False,
                        bin1_sources=None) -> dict:
    """세션 tables → Serial 순 compact dict.

    항목 집합 산출은 `dist_blob.compute_dist_compact` 와 **같은 순서·같은 규칙**이다
    (모드 변형 → selected_items → 무데이터 항목 제외 → ``only`` 화이트리스트). 갤러리가
    ECDF 와 seq 를 같은 카드 목록으로 오가므로 두 경로의 항목 집합이 어긋나면 카드가
    조용히 비어 보인다.

    ``tables`` 의 item_columns 를 in-place 로 좁히므로 호출자는 소모성 tables(loader 클론)를
    넘길 것 — `compute_dist_compact` 와 같은 계약이다.
    """
    from .tabs.common import empty_items
    from .validation import mode_tables, validate_mode

    tables = mode_tables(tables, validate_mode(mode))
    selected = {str(v) for v in (selected_items or []) if str(v)}
    if selected:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected]
    excluded = empty_items(tables)
    all_items = sorted({c for t in tables for c in t.item_columns if c not in excluded})
    if only is not None:
        wanted = {str(v) for v in only if str(v)}
        all_items = [c for c in all_items if c in wanted]
    return build_seq_compact(tables, all_items, bin1=bin1, bin1_sources=bin1_sources)

"""Commonality 모드 payload 빌더 — 사용자가 고른 chip 을 wafer 위치로 강조하고,
각 항목 분포(ECDF)에서 그 chip 이 어느 누적 %에 있는지 계산한다.

Commonality 모드는 입력 source 1개일 때만 허용된다(클라 제약). 여기 함수들은 그래도
여러 table 이 와도 안전하게 순회한다(첫 매칭 chip 사용).

chip 선택은 프런트가 raw data 표에서 serial/xpos/ypos 로 검색·행 선택한다:
  - search_chips: 검색어(serial/xpos/ypos/dut 부분일치)로 chip 후보 목록.
  - chip_percentiles: 선택 chip 의 항목별 값 + 누적%(ECDF 위치) — 프런트가 분포를
    그 누적% 기준으로 색 분리하고 wafer map 에 위치를 강조하는 데 쓴다.
  - chip_percentiles_many: 위를 **여러 chip 한 번에**. Item_detail 에서 드래그 박스로
    수십~수백 die 를 잡는 경로 전용(2026-09-03) — chip 마다 왕복하면 chip 수만큼
    HTTP + item 마다 스칼라 변환이 돌아 실질적으로 못 쓴다.
다운샘플 없음(규칙 #6) — 누적%는 전체 die 대상으로 계산한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import fmt_type, json_safe, round_num, to_coord
from .distribution import to_numeric_clean
from ..honeyform import META_COLUMNS as _META

_SEARCH_COLS = ("SERIAL", "XPOS", "YPOS", "DUT")


def _meta_lists(data):
    return {c: [fmt_type(v) for v in data[c].tolist()] for c in _META}


def _index_entry(table, *, with_sorted=True):
    """테이블 1개의 사전 계산 엔트리: 메타 리스트(+검색용 소문자본) [+ item별 정렬 numeric 배열]."""
    meta = _meta_lists(table.data)
    entry = {
        "meta": meta,
        "meta_lower": {c: [v.lower() for v in meta[c]] for c in _SEARCH_COLS},
        "pos": None,      # (SERIAL, XPOS, YPOS) → 첫 행 idx — _pos_map 이 지연 생성
    }
    if with_sorted:
        entry["sorted"] = {
            item: np.sort(to_numeric_clean(table.data[item]))
            for item in table.item_columns
        }
    return entry


def _pos_map(entry) -> dict:
    """(SERIAL, XPOS, YPOS) → 첫 행 인덱스. ``_locate`` 의 '첫 매칭' 의미를 그대로 유지한다.

    지연 생성 후 entry 에 붙여 둔다(entry 는 세션 단위 캐시라 두 번째 조회부터 O(1)).
    드래그로 수백 chip 을 한 번에 찾을 때 chip 마다 전 행 선형 스캔하던 비용을 없앤다.
    """
    pm = entry.get("pos")
    if pm is None:
        meta = entry["meta"]
        pm = {}
        for i, key in enumerate(zip(meta["SERIAL"], meta["XPOS"], meta["YPOS"])):
            pm.setdefault(key, i)     # 중복 좌표(재검)는 첫 행 우선 — _locate 와 같은 규칙
        entry["pos"] = pm
    return pm


def build_index(tables) -> list:
    """세션 단위 사전 계산 인덱스 — service 가 (analysis_key, content_hash) 키로 캐시한다.

    chip 검색(키스트로크마다)·백분위 계산(chip 클릭마다)이 매번 전 컬럼을 fmt_type/
    to_numeric_clean 으로 재변환하지 않도록, 메타 리스트와 item별 정렬 배열을 미리 만든다.
    다운샘플 없음(규칙 #6) — 정렬 배열은 전체 유한값이며 누적%는 여기서 전량 기준으로 계산된다.
    """
    return [_index_entry(table) for table in tables]


def search_chips(tables, q="", limit=300, index=None, serial="", xpos="", ypos="") -> dict:
    """검색어로 chip 후보를 찾는다. serial/xpos/ypos/dut 부분일치(대소문자 무시).

    두 가지 검색 방식을 지원한다:
    - serial/xpos/ypos 중 하나라도 값이 있으면 **필드별 AND 조건**으로 좁힌다 — 개별 칸
      검색용(예: serial=A AND xpos=3 AND ypos=-2). serial 은 부분일치(긴 식별자 일부 검색),
      xpos/ypos 는 **정확일치**(좌표 핀포인트 — 부분일치는 3↔13↔-3 처럼 오검색). 빈 필드는 무시.
    - 셋 다 비고 q 만 있으면 기존 방식(serial/xpos/ypos/dut OR 부분일치).

    q/필드 모두 비면 앞에서부터 limit 개. 결과가 limit 을 넘으면 truncated=True.
    index 는 build_index 결과(없으면 즉석 계산 — 메타만).
    """
    q = str(q or "").strip().lower()
    ser = str(serial or "").strip().lower()
    xp = str(xpos or "").strip().lower()
    yp = str(ypos or "").strip().lower()
    field_mode = bool(ser or xp or yp)
    out = []
    for ti, table in enumerate(tables):
        entry = index[ti] if index else _index_entry(table, with_sorted=False)
        meta, meta_lower = entry["meta"], entry["meta_lower"]
        n = len(meta["SERIAL"])
        for i in range(n):
            if field_mode:
                if ser and ser not in meta_lower["SERIAL"][i]:
                    continue
                if xp and xp != meta_lower["XPOS"][i]:
                    continue
                if yp and yp != meta_lower["YPOS"][i]:
                    continue
            elif q and not (
                q in meta_lower["SERIAL"][i]
                or q in meta_lower["XPOS"][i]
                or q in meta_lower["YPOS"][i]
                or q in meta_lower["DUT"][i]
            ):
                continue
            out.append({
                "source": table.source,
                "serial": meta["SERIAL"][i], "shot": meta["SHOT"][i],
                "dut": meta["DUT"][i], "xpos": meta["XPOS"][i], "ypos": meta["YPOS"][i],
                "bin": meta["BIN"][i],
            })
            if len(out) >= limit:
                return {"chips": out, "truncated": True}
    return {"chips": out, "truncated": False}


def _locate(meta, serial, xpos, ypos):
    """메타 리스트에서 (serial/xpos/ypos 부분조건) 에 맞는 첫 행 위치를 찾는다. 없으면 None."""
    ser_q = fmt_type(serial) if serial != "" else ""
    x_q = fmt_type(xpos) if xpos != "" else ""
    y_q = fmt_type(ypos) if ypos != "" else ""
    for i in range(len(meta["SERIAL"])):
        if ser_q and meta["SERIAL"][i] != ser_q:
            continue
        if x_q and meta["XPOS"][i] != x_q:
            continue
        if y_q and meta["YPOS"][i] != y_q:
            continue
        return i
    return None


def _find_chip(tables, index, serial, xpos, ypos, source):
    """chip 1건의 (table, 행 idx, entry) 를 찾는다. 못 찾으면 None.

    source 를 지정하면 그 소스에서 먼저 찾고, 실패하면 source 없이 전체를 다시 훑는다
    (지정 소스에 없는 좌표를 다른 소스에서라도 찾아 주던 종전 동작 유지).
    세 필드가 모두 채워졌으면 위치 dict(``_pos_map``)로 O(1), 하나라도 비면 부분조건
    선형 탐색(``_locate``) — 판정 결과는 두 경로가 같다.
    """
    def _seek(only_source):
        for ti, table in enumerate(tables):
            if only_source and table.source != only_source:
                continue
            entry = index[ti]
            if serial != "" and xpos != "" and ypos != "":
                found = _pos_map(entry).get(
                    (fmt_type(serial), fmt_type(xpos), fmt_type(ypos)))
            else:
                found = _locate(entry["meta"], serial, xpos, ypos)
            if found is not None:
                return (table, found, entry)
        return None

    target = _seek(source)
    if target is None and source:
        target = _seek("")
    return target


def _chip_meta(table, meta, idx) -> dict:
    """chip 식별 dict (source/serial/shot/dut/xpos/ypos/bin + 수치 좌표 x,y)."""
    chip = {
        "source": table.source,
        "serial": meta["SERIAL"][idx], "shot": meta["SHOT"][idx], "dut": meta["DUT"][idx],
        "xpos": meta["XPOS"][idx], "ypos": meta["YPOS"][idx], "bin": meta["BIN"][idx],
    }
    coord = to_coord(meta["XPOS"][idx], meta["YPOS"][idx])
    chip["x"], chip["y"] = coord if coord else (None, None)
    return chip


def _percentile_block(table, entry, idxs):
    """행 idxs 여러 개의 항목별 값·누적%를 **item-major** 로 한 번에 계산한다.

    반환 (items, values, cums) — items 는 sorted(item_columns), values/cums 는
    idxs 순서 × items 순서의 2차원 리스트(값 없으면 None).

    chip 마다 item 을 도는 대신 item 마다 chip 전부를 벡터로 처리한다: item 당
    to_numeric 1회 + searchsorted 1회. 값 자체는 단건 경로와 동일한 식
    (``searchsorted(right)/n*100`` → round 2, ``round_num(float(v))``)이다.
    """
    items = sorted(table.item_columns)
    k = len(idxs)
    values = [[None] * len(items) for _ in range(k)]
    cums = [[None] * len(items) for _ in range(k)]
    if not k:
        return items, values, cums
    rows = table.data.iloc[list(idxs)]
    sorted_vals = entry.get("sorted") or {}
    for j, item in enumerate(items):
        arr = sorted_vals.get(item)
        if arr is None:
            arr = np.sort(to_numeric_clean(table.data[item]))   # 유한값만
        n = int(arr.size)
        if not n:
            continue
        col = rows[item]
        if getattr(col.dtype, "kind", "") in "if":
            raw = col.to_numpy(dtype="float64")
        else:
            raw = pd.to_numeric(col, errors="coerce").to_numpy(dtype="float64")
        ok = np.isfinite(raw)
        if not ok.any():
            continue
        # 정렬 배열에서 searchsorted(right) == (chip 값 이하 die 수) — count_nonzero 와 동일
        hit = np.flatnonzero(ok)
        cum = np.searchsorted(arr, raw[hit], side="right").astype("float64") / n * 100.0
        for r, v, c in zip(hit, raw[hit], cum):
            values[int(r)][j] = round_num(float(v))
            cums[int(r)][j] = round(float(c), 2)
    return items, values, cums


def chip_percentiles(tables, *, serial="", xpos="", ypos="", source="", index=None) -> dict:
    """선택 chip 의 항목별 값 + 누적%(ECDF 위치)를 계산한다.

    누적% = (chip 값 이하인 die 수 / 유효 die 수) × 100. 항목 값이 없거나(비수치)
    분포가 비면 value/cum_pct=None. chip 을 못 찾으면 KeyError(라우트가 404).
    index 는 build_index 결과(없으면 즉석 계산).
    """
    if index is None:
        index = build_index(tables)
    target = _find_chip(tables, index, serial, xpos, ypos, source)
    if target is None:
        raise KeyError("chip not found")

    table, idx, entry = target
    chip = _chip_meta(table, entry["meta"], idx)
    items, values, cums = _percentile_block(table, entry, [idx])
    sorted_vals = entry.get("sorted") or {}
    rows = []
    for j, item in enumerate(items):
        arr = sorted_vals.get(item)
        n = int(arr.size) if arr is not None else int(
            np.sort(to_numeric_clean(table.data[item])).size)
        rows.append({
            "subject": item,
            "units": json_safe(table.units.get(item)) or "",
            "lower_limit": round_num(table.lolim.get(item)),
            "upper_limit": round_num(table.hilim.get(item)),
            "n": n,
            "value": values[0][j],
            "cum_pct": cums[0][j],
        })
    return {"chip": chip, "items": rows}


def chip_percentiles_many(tables, chips, *, index=None) -> dict:
    """여러 chip 의 항목별 값 + 누적%를 한 번에 (Item_detail 드래그 강조 전용).

    chips = [{source, serial, xpos, ypos}, ...] — **입력 순서를 그대로 유지**하고,
    못 찾은 chip 은 None 이다(단건과 달리 KeyError 를 내지 않는다 — 드래그 박스에는
    다른 소스의 die 나 이미 지워진 좌표가 섞일 수 있고, 하나 때문에 전체가 실패하면 안 된다).

    반환::

        {"item_lists": [[item...], ...],        # 테이블별 sorted(item_columns)
         "chips": [{"chip": {...}, "items_ref": <item_lists 인덱스>,
                    "value": [...], "cum_pct": [...]} | None, ...]}

    값은 ``chip_percentiles`` 와 원소 단위로 같다(tests/test_commonality_batch.py 가
    전 die 를 대조한다). 항목명을 chip 마다 반복하지 않으려고 ``items_ref`` 로 참조한다 —
    300 chip × 3000 item 이면 이름 반복만 수십 MB 다.
    """
    if index is None:
        index = build_index(tables)
    found = []           # [(입력 i, table, idx, entry)]
    for i, chip in enumerate(chips or []):
        c = chip or {}
        target = _find_chip(tables, index,
                            str(c.get("serial") or ""), str(c.get("xpos") or ""),
                            str(c.get("ypos") or ""), str(c.get("source") or ""))
        if target is not None:
            found.append((i, target[0], target[1], target[2]))

    # 같은 테이블끼리 묶어 _percentile_block 을 테이블당 1회만 돈다.
    groups: dict = {}
    for i, table, idx, entry in found:
        groups.setdefault(id(table), (table, entry, []))[2].append((i, idx))

    item_lists: list = []
    out = [None] * len(chips or [])
    for table, entry, pairs in groups.values():
        items, values, cums = _percentile_block(table, entry, [idx for _, idx in pairs])
        ref = len(item_lists)
        item_lists.append(items)
        for r, (i, idx) in enumerate(pairs):
            out[i] = {
                "chip": _chip_meta(table, entry["meta"], idx),
                "items_ref": ref,
                "value": values[r],
                "cum_pct": cums[r],
            }
    return {"item_lists": item_lists, "chips": out}

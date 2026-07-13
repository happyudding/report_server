"""product_info.csv 로더 — 업로드 Product 검색 후보 + 세션 기준정보 lookup.

server/ 가 sys.path 루트라 `from product_info import ...` 로 import 한다(config 선례).
CSV 는 mtime 기준 1회 파싱 후 캐시 — 실데이터 파일로 교체하면 자동 재로딩된다.
파일 부재/파싱 실패는 best-effort 로 빈 결과(예외 미전파) — part_ids 라우트 관례와 동일.
"""
from __future__ import annotations

import csv
import logging
import threading

from config import PRODUCT_INFO_CSV_PATH

_log = logging.getLogger(__name__)

# 세션에 저장할 기준정보 컬럼(= sessions.create_session 화이트리스트와 동일 집합).
INFO_COLUMNS = (
    "part_id", "sub_part_id", "product_group", "wf_size", "chip_size_x",
    "chip_size_y", "gross_die", "pkg_type", "e2f_fab_site", "step",
    "temperature", "equip", "para", "flat_zone",
)

_lock = threading.Lock()
_cache_mtime = None
_candidates: list[str] = []
_lookup_map: dict[str, dict] = {}


def _parse_braces(val: str) -> list[str]:
    """`{a, b, c}` → ['a','b','c']. 중괄호 없으면 [val]. 빈 항목 제외."""
    s = (val or "").strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
        return [t.strip() for t in s.split(",") if t.strip()]
    return [s] if s else []


def _load():
    """CSV mtime 이 바뀌었으면 재파싱해 후보 리스트/lookup 맵을 갱신. _lock 하에서 호출."""
    global _cache_mtime, _candidates, _lookup_map
    try:
        mtime = PRODUCT_INFO_CSV_PATH.stat().st_mtime
    except OSError:
        # 파일 없음 — 빈 결과(에러 미전파, 하위호환)
        _cache_mtime, _candidates, _lookup_map = None, [], {}
        return
    if mtime == _cache_mtime and _lookup_map:
        return
    candidates: list[str] = []
    lookup: dict[str, dict] = {}
    try:
        with open(PRODUCT_INFO_CSV_PATH, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                info = {c: (row.get(c) or "").strip() for c in INFO_COLUMNS}
                # 검색 후보 = 대표 part_id(단일) + sub_part_id 중괄호 개별 항목.
                # 어느 후보를 고르든 그 행의 원본 기준정보(info) 를 세션에 저장한다.
                keys = [info["part_id"]] if info["part_id"] else []
                keys.extend(_parse_braces(row.get("sub_part_id", "")))
                for k in keys:
                    if k and k not in lookup:
                        lookup[k] = info
                        candidates.append(k)
    except Exception as exc:  # noqa: BLE001
        _log.warning("product_info.csv 파싱 실패 (%s): %s", PRODUCT_INFO_CSV_PATH, exc)
        return
    _cache_mtime = mtime
    _candidates = sorted(set(candidates))
    _lookup_map = lookup


def list_search_candidates() -> list[str]:
    """Product 검색 자동완성 후보(정렬·중복제거). part_ids 라우트용."""
    with _lock:
        _load()
        return list(_candidates)


def lookup(selected: str) -> dict:
    """선택된 part_id/sub_part_id → 14개 기준정보 dict. 미매칭/빈값이면 {}."""
    key = (selected or "").strip()
    if not key:
        return {}
    with _lock:
        _load()
        info = _lookup_map.get(key)
        return dict(info) if info else {}

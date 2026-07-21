"""product_info.csv 로더 — 업로드 Product 검색 후보 + 세션 기준정보 lookup.

server/ 가 sys.path 루트라 `from product_info import ...` 로 import 한다(config 선례).
CSV 는 mtime 기준 1회 파싱 후 캐시 — 실데이터 파일로 교체하면 자동 재로딩된다.
파일 부재/파싱 실패는 best-effort 로 빈 결과(예외 미전파) — part_ids 라우트 관례와 동일.
"""
from __future__ import annotations

import csv
import io
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
# 파일 부재 경고를 상태 전이에서 1회만 찍기 위한 플래그(로드 성공하면 해제).
_missing_logged = False


def _parse_braces(val: str) -> list[str]:
    """`{a, b, c}` → ['a','b','c']. 중괄호 없으면 [val]. 빈 항목 제외."""
    s = (val or "").strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
        return [t.strip() for t in s.split(",") if t.strip()]
    return [s] if s else []


def _load():
    """CSV mtime 이 바뀌었으면 재파싱해 후보 리스트/lookup 맵을 갱신. _lock 하에서 호출."""
    global _cache_mtime, _candidates, _lookup_map, _missing_logged
    try:
        mtime = PRODUCT_INFO_CSV_PATH.stat().st_mtime
    except OSError:
        # 파일 없음 — 빈 결과(에러 미전파, 하위호환). 다만 조용히 넘어가면 "CSV 를
        # 갈아끼웠는데 경로가 틀렸다" 를 진단할 방법이 없어 로그는 남긴다. 매 호출마다
        # 찍지 않도록 플래그로 1회만(파일이 생기면 아래 성공 경로가 해제한다).
        if not _missing_logged:
            _log.warning("product_info.csv 없음 — Product 검색 후보 0건 (%s)",
                         PRODUCT_INFO_CSV_PATH)
            _missing_logged = True
        _cache_mtime, _candidates, _lookup_map = None, [], {}
        return
    if mtime == _cache_mtime and _lookup_map:
        return
    candidates: list[str] = []
    lookup: dict[str, dict] = {}
    try:
        # 한국 Windows 의 Excel 이 "CSV(쉼표로 분리)" 로 저장하면 cp949 다 — utf-8 로만
        # 열면 한글 한 글자에 UnicodeDecodeError 가 나고 아래 except 가 삼켜서 후보가
        # 조용히 0 건이 된다. utf-8(BOM 허용) → cp949 순으로 시도한다.
        raw = PRODUCT_INFO_CSV_PATH.read_bytes()
        text = None
        for enc in ("utf-8-sig", "cp949"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("utf-8/cp949 어느 쪽으로도 디코딩할 수 없음")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        # 헤더가 다르면 파싱은 성공하는데 후보만 0 건이 된다 — CSV 를 교체했을 때 가장
        # 헷갈리는 실패라 실제 컬럼명을 그대로 찍어준다.
        if "part_id" not in (reader.fieldnames or ()):
            raise ValueError(f"필수 컬럼 part_id 없음 (헤더: {reader.fieldnames})")
        for row in reader:
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
    _missing_logged = False
    # 성공 경로에도 로그를 남긴다 — 자동완성 장애의 첫 질문이 "어느 파일에서 몇 건을
    # 읽었나" 인데 지금은 성공 시 아무 흔적도 없다. CSV mtime 이 바뀔 때만 찍힌다.
    _log.info("product_info.csv 로드: 후보 %d건 (%s)", len(_candidates), PRODUCT_INFO_CSV_PATH)


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

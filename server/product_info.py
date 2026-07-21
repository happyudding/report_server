"""product_info.db 로더 — 업로드 Product 검색 후보 + 세션 기준정보 lookup.

원본 기준정보 CSV 가 DRM(NASCA)으로 암호화돼 서버가 평문으로 읽을 수 없다. 서버는 Excel 을
쓰지 않으므로(CLAUDE.md 불변 규칙 #1), Excel 이 설치된 별도 PC 에서 tools/product_info_import
로 만든 product_info.db(SQLite)를 손으로 복사해 두면 이 모듈이 **읽기 전용**으로 연다.

server/ 가 sys.path 루트라 `from product_info import ...` 로 import 한다(config 선례).
DB 는 (mtime, size) 기준 1회 로드 후 캐시 — 파일을 갈아끼우면 자동 재로딩된다(서버 재기동
불필요). 파일 부재/읽기 실패는 best-effort 로 빈 결과(예외 미전파) — part_ids 라우트 관례와 동일.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from config import PRODUCT_INFO_DB_PATH

_log = logging.getLogger(__name__)

# 세션에 저장할 기준정보 컬럼(= sessions.create_session 화이트리스트와 동일 집합).
INFO_COLUMNS = (
    "part_id", "sub_part_id", "product_group", "wf_size", "chip_size_x",
    "chip_size_y", "gross_die", "pkg_type", "e2f_fab_site", "step",
    "temperature", "equip", "para", "flat_zone",
)

_TABLE = "report_product_info"

_lock = threading.Lock()
# 캐시 무효화 키 = (st_mtime_ns, st_size). mtime 단독으로 보지 않는 이유: 이 파일은 사람이
# 손으로 복사해 오는데, mtime 을 보존하는 복사 도구(xcopy /d 등)를 쓰면 교체를 놓친다.
_cache_key = None
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


def _read_meta(conn) -> dict:
    """report_product_info_meta → dict. 테이블이 없거나 실패하면 {} (구버전 .db 호환)."""
    try:
        return {r[0]: r[1] for r in
                conn.execute("SELECT key, value FROM report_product_info_meta")}
    except sqlite3.Error:
        return {}


def _load():
    """DB 파일이 바뀌었으면 재조회해 후보 리스트/lookup 맵을 갱신. _lock 하에서 호출."""
    global _cache_key, _candidates, _lookup_map, _missing_logged
    try:
        st = PRODUCT_INFO_DB_PATH.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        # 파일 없음 — 빈 결과(에러 미전파, 하위호환). 다만 조용히 넘어가면 "DB 를 복사했는데
        # 경로가 틀렸다" 를 진단할 방법이 없어 로그는 남긴다. 매 호출마다 찍지 않도록
        # 플래그로 1회만(파일이 생기면 아래 성공 경로가 해제한다).
        if not _missing_logged:
            _log.warning("product_info.db 없음 — Product 검색 후보 0건 (%s)",
                         PRODUCT_INFO_DB_PATH)
            _missing_logged = True
        _cache_key, _candidates, _lookup_map = None, [], {}
        return
    if key == _cache_key:
        return
    candidates: list[str] = []
    lookup: dict[str, dict] = {}
    meta = {}
    try:
        # mode=ro 는 선택이 아니라 필수다. 기본 sqlite3.connect 는 경로가 틀리면 빈 .db 를
        # '만들어' 버려서, 위의 '파일 없음' 경고가 영영 안 뜨고 매 로드가 no such table 로
        # 조용히 실패한다(진단 불가능한 최악의 형태). URI 는 as_uri() 로 만든다 —
        # 수동 f"file:{path}" 는 경로에 공백·?·# 이 있으면 깨진다.
        uri = Path(PRODUCT_INFO_DB_PATH).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            meta = _read_meta(conn)
            # SELECT * 를 쓰지 않는다 — 임포터가 컬럼을 추가/재배치해도 서버가 안 깨진다.
            cols = ", ".join(INFO_COLUMNS)
            rows = conn.execute(
                f"SELECT {cols} FROM {_TABLE} ORDER BY row_no").fetchall()
        finally:
            conn.close()
        for row in rows:
            info = {c: (row[c] or "").strip() for c in INFO_COLUMNS}
            # 검색 후보 = 대표 part_id(단일) + sub_part_id 중괄호 개별 항목.
            # 어느 후보를 고르든 그 행의 원본 기준정보(info) 를 세션에 저장한다.
            keys = [info["part_id"]] if info["part_id"] else []
            keys.extend(_parse_braces(info["sub_part_id"]))
            for k in keys:
                if k and k not in lookup:
                    lookup[k] = info
                    candidates.append(k)
    except Exception as exc:  # noqa: BLE001
        # _cache_key 를 갱신하지 않고 빠진다 → 다음 호출이 재시도한다.
        _log.warning("product_info.db 읽기 실패 (%s): %s", PRODUCT_INFO_DB_PATH, exc)
        return
    _cache_key = key
    _candidates = sorted(set(candidates))
    _lookup_map = lookup
    _missing_logged = False
    # 자동완성 장애의 첫 질문이 "어느 파일에서 몇 건을, 언제 만든 DB 로 읽었나" 다.
    # DB mtime 이 바뀔 때만 찍힌다.
    _log.info("product_info.db 로드: 후보 %d건 rows=%s imported_at=%s (%s)",
              len(_candidates), meta.get("row_count", "?"),
              meta.get("imported_at", "?"), PRODUCT_INFO_DB_PATH)


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

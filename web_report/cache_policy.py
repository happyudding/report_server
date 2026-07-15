"""web_report 캐시 키 정책 — 키 구성 규약의 단일 진실 (Phase 5, 2026-07-11).

지금까지 "mode 는 이 키엔 포함, 저 키엔 불포함(이유: …)" 같은 규약이 각 호출부
주석으로만 존재했다. 이 모듈이 캐시별 키 빌더를 제공하고, 호출부는 반드시 이
빌더로 키를 만든다 — 새 캐시를 추가하면 여기 빌더와 아래 표를 함께 추가할 것.

| 캐시               | 키 구성                                          | 무효화 트리거                       |
|--------------------|--------------------------------------------------|-------------------------------------|
| TABLES_CACHE       | (akey, chash)                                    | raw_data 편집(chash) / 세션 삭제    |
| DIST_CACHE         | (akey, chash, mode[, "bin1"])                    | 〃 (mode 불변; "bin1"=양품만 ECDF)  |
| COMMONALITY_CACHE  | (akey, chash)                                    | raw_data 편집 / 세션 삭제           |
| REPORT_CACHE       | (akey, chash, sid, edits_rev, opts, mode, sver)  | comment/override 편집(rev) + payload 스키마 변경 + 위 전부 |
| TRIM_CACHE         | (akey, chash, sid, edits_rev, mode, source)      | trim override 편집(rev) + 위 전부   |
| TRIM_CHART_CACHE   | (akey, chash, mode, source, items_digest)        | 그룹 슬롯 구성 변경 / raw_data 편집 |
| _FULL_CACHE        | (akey, chash, "sid:edits_rev", extras_digest)    | 편집 rev / annotations 등 extras    |
| _SCATTER_CACHE     | (akey, chash, mode, subject[, "bin1"])           | raw_data 편집 / 세션 삭제 ("bin1"=양품만) |

공통 규약:
- 모든 키의 **첫 요소는 analysis_key** — AKEY_CACHES 무효화(evict/invalidate)의 전제.
- content_hash 는 raw parquet 내용의 해시 — raw_data 편집·rawdata_replace 로만 바뀐다.
- edits_rev 는 세션 편집 DB 의 단조 rev — comment/etc/trim override/engr 편집으로 바뀐다.
  세션 단위 편집(2026-07-11 결정)이라 sid 와 항상 짝으로 들어간다.
- mode/webreport_options 는 세션 생성 시 확정되어 불변 — 키에 넣는 이유는 dedup
  (동일 akey 를 공유하는 다른 세션)과의 충돌 방지다.
- selected_items 는 analysis_key 산출에 포함되므로 어떤 키에도 따로 넣지 않는다.
"""
from __future__ import annotations

from .validation import validate_mode


def _base(session) -> tuple:
    return (session.get("analysis_key"), str(session.get("content_hash") or ""))


def _mode(session) -> str:
    return validate_mode(session.get("mode"))


def tables_key(session) -> tuple:
    return _base(session)


def commonality_key(session) -> tuple:
    return _base(session)


def dist_key(session, *, bin1: bool = False) -> tuple:
    # DUT 모드는 같은 akey 라도 분할된 ECDF 를 내므로 mode 포함.
    # bin1=True 는 양품(Bin1)만으로 재계산한 ECDF — 전체 기준과 별도 캐시(키에만 추가해
    # 기존 전체 기준 키는 불변 유지 → 기존 캐시 무효화 없음).
    base = _base(session) + (_mode(session),)
    return base + ("bin1",) if bin1 else base


# build_report_payload 출력 스키마 버전. payload 구조(최상위 키·그룹 형태)가 바뀌면
# 이 값을 올려 인메모리 REPORT_CACHE + disk_cache report 파일(같은 데이터의 옛 세대
# 산출물)을 자연 무효화한다 — 안 올리면 코드가 새 키를 내도 캐시가 stale payload 를
# 반환한다(예: yield_step_groups 추가 후 옛 캐시엔 그 키가 없어 접기 UI 가 폴백됨).
# v3: yield_step_groups·yield_summary.by_step 분모를 cascade→전체 rawdata 기준으로 전환
#     (키는 동일하나 값이 바뀌어 옛 캐시가 stale cascade 값을 반환하는 것을 막는다).
# v4: Map Analysis die 스키마 확장 — 앞 step fail die 회색 마커({x,y,g}), fail die 대표 항목명
#     ({..,"it":item}). 옛 캐시엔 이 필드가 없어 회색/TNO Map 이 폴백되는 것을 막는다.
# v5: Yield Tab Step FailTNO logic change.
# v6: yield_summary.by_step 에 sources/avg_yield_pct(STEP×Source) 추가 + CPK 단측 limit 지원
#     (USL만→CPU=CPK, LSL만→CPL=CPK, cpk 값 변경) + distribution_index 에 is_passfail 플래그·
#     P/F 항목 포함(empty 만 제외, 프런트 "P/F 없애기" 토글이 필터). 옛 캐시엔 이 필드가 없어
#     새 표·토글이 폴백되고 cpk 가 옛값으로 회귀하는 것을 막는다.
# v7: IssueTable CPK 섹션 정렬을 worst-case cpk 내림차순→오름차순(낮은 순 위)으로 변경.
#     rows 순서만 바뀌므로 키는 동일 — 옛 캐시가 stale 순서를 반환하는 것을 막는다.
REPORT_SCHEMA_VERSION = 7


def report_key(session, session_id: str, edits_rev: int) -> tuple:
    return _base(session) + (session_id, edits_rev,
                             session.get("webreport_options") or "", _mode(session),
                             REPORT_SCHEMA_VERSION)


def trim_key(session, session_id: str, edits_rev: int, source: str) -> tuple:
    return _base(session) + (session_id, edits_rev, _mode(session), str(source or ""))


def trim_chart_key(session, source: str, items_digest: str) -> tuple:
    return _base(session) + (_mode(session), str(source or ""), items_digest)


def full_key(session, session_id: str, edits_rev: int, extras_digest: str) -> tuple:
    return _base(session) + (f"{session_id}:{edits_rev}", extras_digest)


def scatter_key(session, subject: str, *, bin1: bool = False) -> tuple:
    # bin1=True 는 양품(Bin1)만으로 낸 상세 — 전체 기준과 별도 캐시(키에만 추가).
    base = _base(session) + (_mode(session), subject)
    return base + ("bin1",) if bin1 else base

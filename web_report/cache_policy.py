"""web_report 캐시 키 정책 — 키 구성 규약의 단일 진실 (Phase 5, 2026-07-11).

지금까지 "mode 는 이 키엔 포함, 저 키엔 불포함(이유: …)" 같은 규약이 각 호출부
주석으로만 존재했다. 이 모듈이 캐시별 키 빌더를 제공하고, 호출부는 반드시 이
빌더로 키를 만든다 — 새 캐시를 추가하면 여기 빌더와 아래 표를 함께 추가할 것.

| 캐시               | 키 구성                                          | 무효화 트리거                       |
|--------------------|--------------------------------------------------|-------------------------------------|
| TABLES_CACHE       | (akey, chash[, prep])                            | raw_data 편집(chash) / 전처리 / 세션 삭제 |
| DIST_CACHE         | (akey, chash[, prep], mode[, "bin1"])            | 〃 (mode 불변; "bin1"=양품만 ECDF)  |
| _DIST_BATCH_CACHE  | (akey, chash[, prep], mode, subjects_digest[, "bin1"]) | raw_data 편집 / 전처리 / 세션 삭제 |
| MAP_CACHE          | (akey, chash[, prep], mode)                      | raw_data 편집(chash) / 전처리 / 세션 삭제 |
| COMMONALITY_CACHE  | (akey, chash)                                    | raw_data 편집 / 세션 삭제           |
| REPORT_CACHE       | (akey, chash, sid, edits_rev, opts, mode, sver)  | comment/override/전처리 편집(rev) + payload 스키마 변경 + 위 전부 |
| TRIM_CACHE         | (akey, chash, sid, edits_rev, mode, source)      | trim override/전처리 편집(rev) + 위 전부 |
| TRIM_CHART_CACHE   | (akey, chash[, prep], mode, source, items_digest) | 그룹 슬롯 구성 변경 / raw_data 편집 |
| _FULL_CACHE        | (akey, chash, "sid:edits_rev", extras_digest)    | 편집 rev / annotations 등 extras    |
| _SCATTER_CACHE     | (akey, chash[, prep], mode, subject[, "bin1"])   | raw_data 편집 / 전처리 / 세션 삭제 ("bin1"=양품만) |

공통 규약:
- 모든 키의 **첫 요소는 analysis_key** — AKEY_CACHES 무효화(evict/invalidate)의 전제.
- content_hash 는 raw parquet 내용의 해시 — raw_data 편집·rawdata_replace 로만 바뀐다.
- edits_rev 는 세션 편집 DB 의 단조 rev — comment/etc/trim override/engr 편집으로 바뀐다.
  세션 단위 편집(2026-07-11 결정)이라 sid 와 항상 짝으로 들어간다.
- **prep** 은 조회 전처리(항목 제외·outlier 마스킹) spec 의 digest —
  [preprocess.digest](preprocess.py). 전처리가 **없으면 빈 문자열이고 키에 아무것도 덧붙이지
  않는다** → 옵션을 쓰지 않는 세션의 키는 도입 전과 완전히 동일(무회귀). edits_rev 를
  이미 가진 키(REPORT/TRIM/_FULL)에는 넣지 않는다 — rev 가 전처리 저장 시 함께 증가해
  같은 역할을 하고, 덧붙여도 재사용 이득이 없기 때문이다.
- mode/webreport_options 는 세션 생성 시 확정되어 불변 — 키에 넣는 이유는 dedup
  (동일 akey 를 공유하는 다른 세션)과의 충돌 방지다.
- selected_items 는 analysis_key 산출에 포함되므로 어떤 키에도 따로 넣지 않는다.
"""
from __future__ import annotations

from .validation import validate_mode


def _base(session, prep_digest: str = "") -> tuple:
    """(akey, chash) + 전처리 digest(있을 때만).

    prep_digest 가 빈 문자열이면 **도입 전과 동일한 2-튜플** 을 돌려준다 — 기존 세션의
    RAM/디스크 캐시가 그대로 유효해야 하기 때문이다.
    """
    base = (session.get("analysis_key"), str(session.get("content_hash") or ""))
    return base + (str(prep_digest),) if prep_digest else base


def _mode(session) -> str:
    return validate_mode(session.get("mode"))


def tables_key(session, prep_digest: str = "") -> tuple:
    return _base(session, prep_digest)


def commonality_key(session) -> tuple:
    # Commonality 는 SERIAL/BIN 등 메타만 쓰고 측정값·item 구성을 보지 않는다 —
    # 전처리로 결과가 달라지지 않으므로 prep 을 넣지 않는다.
    return _base(session)


def dist_key(session, *, bin1: bool = False, prep_digest: str = "") -> tuple:
    # DUT 모드는 같은 akey 라도 분할된 ECDF 를 내므로 mode 포함.
    # bin1=True 는 양품(Bin1)만으로 재계산한 ECDF — 전체 기준과 별도 캐시(키에만 추가해
    # 기존 전체 기준 키는 불변 유지 → 기존 캐시 무효화 없음).
    base = _base(session, prep_digest) + (_mode(session),)
    return base + ("bin1",) if bin1 else base


def dist_batch_key(session, subjects_digest: str, *, bin1: bool = False,
                   prep_digest: str = "") -> tuple:
    """항목 배치 ECDF(GET .../distribution_batch) 응답 gzip 캐시 키.

    dist_key 와 같은 (akey, chash, mode) 기반에 요청 항목 집합의 digest 를 더한다 —
    배치 구성이 스크롤에 따라 달라지므로 집합 자체가 키의 일부다. 전체 dist 캐시와
    같은 세션을 가리키지만 별도 캐시라 서로를 무효화하지 않는다.
    """
    base = _base(session, prep_digest) + (_mode(session), str(subjects_digest))
    return base + ("bin1",) if bin1 else base


# build_map_analysis_rows 출력 세대. map rows 의 **값**이 바뀌는 변경(스키마 확장 포함)마다
# 올려 MAP_CACHE + disk_cache map 파일을 자연 무효화한다 — map_key 에는 edits_rev 가 없어
# 이 값 말고는 재계산을 강제할 수단이 없다.
# v2: STEP 이름에 "eval"(대소문자 무시)이 들어가는 STEP 맵 제외 (2026-07-21).
MAP_SCHEMA_VERSION = 2


def map_key(session, prep_digest: str = "") -> tuple:
    # DUT 모드는 같은 akey 라도 병합 맵(All DUT)이 다르므로 mode 포함 — dist_key 와 동일 이유.
    # dies 는 편집과 무관하므로 edits_rev 불포함. 전처리(항목 제외)는 TNO 맵의 fail 항목
    # 구성을 바꾸므로 prep 은 포함한다.
    return _base(session, prep_digest) + (_mode(session), MAP_SCHEMA_VERSION)


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
# v8: Map Analysis dies 를 /full 에서 제외(경량 메타만 + map_deferred=True) — 별도
#     GET .../web_report/map_analysis 지연 로드. 안 올리면 옛 disk_cache 가 dies 포함
#     대형 payload 를 계속 반환해 초기 로드 freeze 수정이 조용히 무효화된다.
# v9: IssueTable 행 Status 컬럼 + 행 숨김(issue_hidden 필터) + CPK 섹션 선정/표시값을
#     규격내 cpk(cpk_limited) 기준으로 전환, cpk_rows 에 *_limited 통계 병기. 안 올리면
#     옛 캐시가 Status 없는 rows / 전체 die 기준 CPK 섹션을 반환한다.
# v10: Compare goodlog 의 limit 일치 판정(_lim_equal)을 소수 4자리 반올림 비교로 전환 —
#      compare_lolimit/compare_hilimit 와 limit_change_map 의 **값**이 바뀐다(구조는 동일).
#      안 올리면 옛 disk_cache 가 부동소수 잔차로 False 가 찍힌 payload 를 계속 반환한다.
# v11: Map Analysis 에서 STEP 이름에 "eval"(대소문자 무시)이 들어가는 STEP 맵을 제외 —
#      맵 rows 개수·bin_counts·fail step 귀속 값이 바뀐다(구조는 동일). 안 올리면 옛
#      disk_cache 가 eval STEP 맵이 포함된 payload 를 계속 반환한다.
# v12: Yield STEP 요약 수율을 누적 차감 기준으로 전환 — yield_summary.by_step 의
#      survivor/step_yield_pct/sources[].{survivor,yield_pct}/avg_yield_pct **값**이 바뀌고
#      cum_fail 키가 추가된다(분모 entered·개별 bin fail% 는 불변). 안 올리면 옛 disk_cache
#      가 STEP 자체 fail 만 뺀 stale 수율(P1 90/P2 95/P3 99)을 계속 반환한다.
# v13: CPK 행의 stdev 를 반올림 없이(원값) 내보낸다 — CPK/Compare 시트의 stdev **값**이
#      바뀐다(구조 동일). 안 올리면 옛 disk_cache 가 round(3) 된 stdev 를 계속 반환해
#      Limit 역산이 예전 값 그대로 나온다.
REPORT_SCHEMA_VERSION = 13


def report_key(session, session_id: str, edits_rev: int) -> tuple:
    # 전처리 변경은 edits_rev 증가로 무효화되므로 prep 을 따로 넣지 않는다
    # (rev 가 이미 키에 있어 덧붙여도 재사용 이득이 없다).
    return _base(session) + (session_id, edits_rev,
                             session.get("webreport_options") or "", _mode(session),
                             REPORT_SCHEMA_VERSION)


def trim_key(session, session_id: str, edits_rev: int, source: str) -> tuple:
    # report_key 와 동일 — edits_rev 가 전처리 변경을 덮는다.
    return _base(session) + (session_id, edits_rev, _mode(session), str(source or ""))


def trim_chart_key(session, source: str, items_digest: str, prep_digest: str = "") -> tuple:
    return _base(session, prep_digest) + (_mode(session), str(source or ""), items_digest)


def full_key(session, session_id: str, edits_rev: int, extras_digest: str) -> tuple:
    # /full 은 report payload 를 감싼 응답 gzip 캐시 — 전처리 변경은 edits_rev 증가로
    # 함께 무효화되므로 prep 을 따로 넣지 않는다.
    return _base(session) + (f"{session_id}:{edits_rev}", extras_digest)


def scatter_key(session, subject: str, *, bin1: bool = False, prep_digest: str = "") -> tuple:
    # bin1=True 는 양품(Bin1)만으로 낸 상세 — 전체 기준과 별도 캐시(키에만 추가).
    base = _base(session, prep_digest) + (_mode(session), subject)
    return base + ("bin1",) if bin1 else base

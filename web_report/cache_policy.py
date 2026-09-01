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
| REPORT_CACHE       | (akey, chash, sid, edits_rev, opts, mode, sver[, rules_rev]) | comment/override/전처리 편집(rev) + payload 스키마 변경 + **eval 룰 편집(/pe/eval)** + 위 전부 |
| TRIM_CACHE         | (akey, chash, sid, edits_rev, mode, source)      | trim override/전처리 편집(rev) + 위 전부 |
| TRIM_CHART_CACHE   | (akey, chash[, prep], mode, source, items_digest) | 그룹 슬롯 구성 변경 / raw_data 편집 |
| _FULL_CACHE        | (akey, chash, "sid:edits_rev", extras_digest)    | 편집 rev / annotations 등 extras    |
| _SCATTER_CACHE     | (akey, chash[, prep], mode, subject[, "bin1"])   | raw_data 편집 / 전처리 / 세션 삭제 ("bin1"=양품만) |
| DIST_CHUNK_CACHE   | (akey, chash[, prep], mode, chunk_id)            | raw_data 편집 / 전처리 / 세션 삭제  |
| _GAP_CACHE         | (akey, chash[, prep], mode, chart_id, spec_digest, gver[, "bin1"]) | raw_data 편집 / 전처리 / **수식 수정(spec_digest)** / 세션 삭제 — **edits_rev·sid 무관** |
| AI_COMMENT_CACHE   | (akey, chash[, prep], mode, meta_digest[, rules_rev][, "evalfail"][, sens_digest], aiver) | raw_data 편집 / 전처리 / 세션 메타(PATCH) / eval 룰 편집 / **세션 민감도 게이지** — **edits_rev·sid 무관**(comment 편집으로 재평가 안 함) |

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

import hashlib
import json

from .validation import (validate_mode, webreport_ai_comment, webreport_ai_no_suggest,
                         webreport_compare_para, webreport_eval_overrides)


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


def commonality_key(session, prep_digest: str = "") -> tuple:
    # Commonality 는 SERIAL/BIN 등 메타만 쓰지만, 전처리의 셀 패치·조건 규칙은 그 메타와
    # die 구성(행) 자체를 바꾼다(die 제외·BIN 변경) → prep 을 키에 넣어야 stale 이 안 나온다.
    # 항목 제외/outlier 만 걸린 세션은 인덱스 내용이 실제로 같지만 다른 키로 갈린다 —
    # 다른 빌더와 같은 규약(전처리 digest 통째로)을 지키는 편이 낫다고 보고 감수한 비용이다
    # (그 세션이 Commonality 모드일 때 인덱스를 한 번 더 만드는 정도).
    return _base(session, prep_digest)


def _bin1_suffix(bin1: bool, bin1_scope: str = "") -> tuple:
    """bin1 변형 키 꼬리표. scope 가 비면 종전 키와 **완전히 동일**(기존 캐시 유효).

    scope="rt" = Temperature "Bin1(RT만)" — RT 소스만 양품 필터, CT/HT 는 전체.
    """
    if not bin1:
        return ()
    return ("bin1",) + ((str(bin1_scope),) if bin1_scope else ())


def dist_key(session, *, bin1: bool = False, prep_digest: str = "",
             bin1_scope: str = "") -> tuple:
    # DUT 모드는 같은 akey 라도 분할된 ECDF 를 내므로 mode 포함.
    # bin1=True 는 양품(Bin1)만으로 재계산한 ECDF — 전체 기준과 별도 캐시(키에만 추가해
    # 기존 전체 기준 키는 불변 유지 → 기존 캐시 무효화 없음).
    return _base(session, prep_digest) + (_mode(session),) + _bin1_suffix(bin1, bin1_scope)


# 배치 ECDF 응답 스키마 세대. 응답 구조(items[].sources[].x/y/n)를 바꾸면 올린다 —
# 이 키에는 edits_rev 가 없어 이 값 말고는 재계산을 강제할 수단이 없다.
# **전역 REPORT_SCHEMA_VERSION 과 무관한 전용 세대**다(전역 bump 는 콜드 폭풍).
# 짝인 dist_key 에는 **일부러 넣지 않는다** — 그쪽은 Honey 가 업로드 때 시딩한 dist blob
# 이 얹히는 자리라, 무효화하면 그 시딩이 막아 주던 수십 초 콜드 dist 빌드가 되살아난다.
# 웹 화면(갤러리 카드·미니셀·composite·Gap)은 전부 이 배치 경로만 쓴다.
# v1: 소스별 표본 수 n 추가 (미니셀 세로 채움 간격 정본 — 2026-08-25)
DIST_BATCH_SCHEMA_VERSION = 1


def dist_batch_key(session, subjects_digest: str, *, bin1: bool = False,
                   prep_digest: str = "", bin1_scope: str = "") -> tuple:
    """항목 배치 ECDF(GET .../distribution_batch) 응답 gzip 캐시 키.

    dist_key 와 같은 (akey, chash, mode) 기반에 요청 항목 집합의 digest 를 더한다 —
    배치 구성이 스크롤에 따라 달라지므로 집합 자체가 키의 일부다. 전체 dist 캐시와
    같은 세션을 가리키지만 별도 캐시라 서로를 무효화하지 않는다.
    """
    return (_base(session, prep_digest)
            + (_mode(session), str(subjects_digest), DIST_BATCH_SCHEMA_VERSION)
            + _bin1_suffix(bin1, bin1_scope))


# Serial 순(rawdata 누적 순) 배치 응답 스키마 세대. 응답 구조(items[].sources[].v)를 바꾸면
# 올린다 — 이 키에는 edits_rev 가 없어 이 값 말고는 재계산을 강제할 수단이 없다.
# **전역 REPORT_SCHEMA_VERSION 과 무관한 전용 세대**다(전역 bump 는 콜드 폭풍).
DIST_SEQ_SCHEMA_VERSION = 1


def dist_seq_batch_key(session, subjects_digest: str, *, bin1: bool = False,
                       prep_digest: str = "", bin1_scope: str = "") -> tuple:
    """항목 배치 **Serial 순** 값 배열(GET .../distribution_batch?order=seq) gzip 캐시 키.

    dist_batch_key 와 같은 재료에 "seq" 표식과 전용 스키마 세대를 더한 별도 키다 —
    같은 항목 집합의 ECDF 응답과 **절대 섞이면 안 되므로**(축 의미가 다르다) 키를
    공유하지 않는다. ETag 도 이 키에서 파생되므로 두 변형이 서로의 304 로 오염되지 않는다.
    """
    return (_base(session, prep_digest)
            + (_mode(session), str(subjects_digest), "seq", DIST_SEQ_SCHEMA_VERSION)
            + _bin1_suffix(bin1, bin1_scope))


def dist_chunk_key(analysis_key, content_hash, mode, chunk_id: int,
                   prep_digest: str = "") -> tuple:
    """dist pack chunk **디코드 결과** 캐시 키.

    다른 빌더와 달리 session dict 가 아니라 원시 인자를 받는다 — 호출부
    (dist_pack_store.load_chunk_items)가 pack 디렉토리 인자만 가지고 session 을 모른다.
    구성은 pack 디렉토리 세대(chash+mode+prep)와 1:1 이라, raw 편집·전처리로 pack
    디렉토리가 갈리면 캐시 키도 함께 갈린다.

    mode 는 validate_mode 가 아니라 **dist_pack_store._gen_name 과 같은 정규화**
    (``str(mode or "Normal")``)를 쓴다 — 이 키가 가리키는 것은 세션 모드가 아니라
    디스크의 pack 디렉토리이고, 둘의 정규화가 어긋나면 서로 다른 디렉토리가 같은 키로
    뭉쳐 다른 세대의 데이터를 돌려줄 수 있다.
    """
    base = (analysis_key, str(content_hash or ""))
    if prep_digest:
        base += (str(prep_digest),)
    return base + (str(mode or "Normal"), int(chunk_id))


# build_map_analysis_rows 출력 세대. map rows 의 **값**이 바뀌는 변경(스키마 확장 포함)마다
# 올려 MAP_CACHE + disk_cache map 파일을 자연 무효화한다 — map_key 에는 edits_rev 가 없어
# 이 값 말고는 재계산을 강제할 수단이 없다.
# v2: STEP 이름에 "eval"(대소문자 무시)이 들어가는 STEP 맵 제외 (2026-07-21).
MAP_SCHEMA_VERSION = 2


def map_key(session, prep_digest: str = "") -> tuple:
    # DUT 모드는 같은 akey 라도 병합 맵(All DUT)이 다르므로 mode 포함 — dist_key 와 동일 이유.
    # dies 는 편집과 무관하므로 edits_rev 불포함. 전처리(항목 제외)는 TNO 맵의 fail 항목
    # 구성을 바꾸므로 prep 은 포함한다.
    key = _base(session, prep_digest) + (_mode(session), MAP_SCHEMA_VERSION)
    # Para Conversion 도 같은 mode("Compare") 인데 맵이 다르다(After 만 All DUT 병합).
    # **para 세션에만** 마커를 덧붙여 기존 세션 키는 바이트 그대로 둔다(콜드 폭풍 회피).
    if webreport_compare_para(session.get("webreport_options") or ""):
        key += ("para",)
    return key


# Temperature 항목별 fail die 인덱스(GET .../web_report/temp_map) 스키마 버전.
# 응답 구조(sources[].items[].idx)를 바꾸면 올린다.
TEMP_MAP_SCHEMA_VERSION = 1


def temp_map_key(session, prep_digest: str = "") -> tuple:
    """Temperature 항목별 fail die 인덱스 캐시 키.

    map_key 와 같은 기반 — 인덱스는 map dies 배열과 1:1 대응이라 같은 것들(raw 편집·
    전처리·모드)에 함께 무효화돼야 한다. 편집(rev)과는 무관하다.
    """
    return _base(session, prep_digest) + (_mode(session), TEMP_MAP_SCHEMA_VERSION)


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
# v14: 수율 **분모**를 제품 기준정보 Gross Die 기준으로 전환(세션 옵션 yield_basis=test 면
#      종전 rawdata 행 수). yield_rows/yield_bin_groups/yield_step_groups/yield_summary 의
#      % 값이 바뀌고 yield_summary.tested·by_source[].tested·payload.yield_basis 가 추가된다.
#      안 올리면 옛 disk_cache 가 rawdata 분모로 계산된 payload 를 계속 반환한다.
# v15: CPK 통계를 **Bin1(양품) 기준 하나로 통일** — cpk_rows 의 base 필드(n/min/median/max/
#      average/stdev/cp/cpl/cpu/cpk)가 Bin1 값이 되고 *_bin1/*_limited 병기가 사라진다.
#      CPK 시트 값, Issue Table CPK 섹션 선정·표시값, distribution_index 의 cpk/status,
#      Compare dist_shift 값이 모두 바뀐다. 안 올리면 옛 disk_cache 가 전체 die 기준 base
#      필드 + 사라진 *_limited 를 계속 반환해 통일이 조용히 무효화된다.
# v16: Compare 모드 재정의 — payload["compare"] 에 groups/before_sources/after_sources·
#      bin_matrix(구 bin_transition 대체)·equivalence(동일성 검증) 추가, common_map.dies 에
#      source 별 bins 병기, dist_shift 대상이 source 2개→그룹 pool 2개로 바뀐다. 안 올리면
#      옛 disk_cache 가 bin_transition 만 든 payload 를 반환해 Bin/동일성 탭이 빈 화면이 된다.
# v17: Compare dist_shift 를 list→dict(after/before/thresholds/summary/rows)로 변경 —
#      Δ 4필드(delta_average/delta_stdev/delta_cpk/mean_gap_pct) 제거, Before 분모 지표 6종
#      (meanshift_sigma/cpk_ratio_pct/stdev_delta_pct/median_shift/iqr_delta_pct/ks_d)·
#      focus 플래그·after/before.n 추가, 정렬이 meanshift_sigma 내림차순으로 바뀐다.
#      안 올리면 옛 disk_cache 가 list 형 dist_shift 를 반환해 산포 비교 탭이 지표·필터
#      없는 legacy 표로 폴백된다.
# v18: Compare dist_shift 에 유의성 2종(p_mean=Welch t, p_stdev=Brown-Forsythe)·
#      thresholds.alpha 추가 + focus 판정에 **노이즈 게이트** 도입 — |Δσ%|≥15 트리거가
#      p_stdev<alpha 일 때만 관심으로 잡힌다(표본이 작은 항목의 오경보 제거, 큰 n 에선 무동작).
#      안 올리면 옛 disk_cache 가 p 없는 rows 를 반환해 ns 마커가 안 뜨고 게이트도 적용되지
#      않은 focus 를 계속 쓴다.
# v19: 수율 **분모를 소스별로** 정한다 — Gross Die 가 기본이지만 그 소스의 측정 die 수보다
#      작거나(수율 100% 초과) 100 개 이상 크면 자동으로 test die 로 내려가고, 사용자가
#      소스별로 고를 수도 있다(yield_tab.resolve_source_basis). yield_rows/yield_bin_groups/
#      yield_step_groups/yield_summary 의 % **값**이 바뀌고 payload.yield_basis 에 mode·
#      by_source 가 추가된다. 안 올리면 옛 disk_cache 가 전 소스 동일 Gross Die 분모로 계산된
#      payload(수율 100% 초과 포함)를 계속 반환한다.
# v20: Compare goodlog 이 **테스트 프로그램이 완전히 같아도 rows 를 채운다**(limit 변경이
#      없어도 항목별 값 gap% 를 봐야 한다는 요구). identical 은 안내 플래그로만 남는다.
#      안 올리면 옛 disk_cache 가 rows=[] 인 payload 를 계속 반환해 표가 비어 보인다.
# v21: STEP 메타가 공백인 fail 행을, 세션 STEP 이 1종뿐일 때 그 STEP 으로 흡수(_sole_step) —
#      yield_rows[].step / yield_step_groups(섹션 수) / yield_summary.by_step(항목 수) 의 **값**이
#      바뀐다(구조 동일). 안 올리면 옛 disk_cache 가 "(기타)" 섹션이 분리된 payload 를 계속 반환한다.
# v22: IssueTable AI Comment 셀에 이봉/다봉/분리 배지를 status 뒤에 접두한다
#      ("[MAJOR][이봉] …") — SUBPOP_GAP 이 primary 가 아니어도 붙는다. ai_comment 옵션
#      세션의 **AI Comment 값**만 바뀌고 구조는 동일하다. 안 올리면 옛 disk_cache 가 배지
#      없는 셀 텍스트를 계속 반환한다(ai_comment 를 안 쓰는 세션은 값 무변경이지만 키가
#      전 세션 공통이라 1회 재계산된다 — rules_rev 는 코드 변경을 감지하지 못해 대체 불가).
# v23: IssueTable ETC 섹션에 "룰만 위반한 item"(수율·cpk 정상 + signature 발화) 자동 행이
#      붙는다 — ai_comment.build_ai_comments 반환 구조도 {"comments","etc_auto_items"} 로
#      바뀌었다. ai_comment 옵션 세션의 **행 구성**이 달라지므로 안 올리면 옛 disk_cache 가
#      자동 행 없는 payload 를 계속 반환한다.
# v24: Temperature 모드 — sources[] 에 temp_role/temp_group 이 붙고 payload.temperature 가
#      추가된다. 비RT(CT/HT) 소스는 수율 분모가 남은 die 수로 강제된다
#      (resolve_source_basis force_test). 다른 모드 세션은 값 무변경이지만 키가 전 세션
#      공통이라 1회 재계산된다.
# v25: AI Comment 표시 판정이 webreport_options.ai_comment → **ai_comment_optin 동반**
#      으로 바뀐다(구 클라가 사용자 의사 없이 보낸 ai_comment=True 세션을 미표시로
#      되돌림 — validation.webreport_ai_comment). 그 세션들의 payload 에서 AI Comment
#      컬럼과 ETC 자동 행이 사라지므로 안 올리면 옛 disk_cache 가 컬럼이 든 payload 를
#      계속 반환해 화면상 그대로 남는다.
# v26: Temperature 모드 — payload 에 yield_corner_groups(RT Corner / Temp Corner 2표)가
#      추가되고, Issue Table 이 Yield/CPK 섹션을 **RT source 기준으로만** 계산하며
#      TEMP 섹션(row_key "TEMP|<item>")이 CPK 와 ETC 사이에 들어간다. sources[] 에는
#      temp_corner("RT"/"CT"/"HT")가 붙는다(Distribution 소스 그룹 필터). 다른 모드
#      세션은 값 무변경이지만 키가 전 세션 공통이라 1회 재계산된다.
# v27: Temperature 모드 대개편 (2026-08-05) — ① Yield 계열(Yield 시트·yield_summary·
#      Bin/STEP 그룹·issue_bin_summary·Fail Bin·Issue Table)이 **RT source 만** 본다
#      ② yield_corner_groups 키 삭제 ③ 신규 시트 "Issue Table Temp"(CT/HT 를 RT limit 으로
#      **전 항목** 재판정 — 첫 fail 제한 없음, row_key 는 "TEMP|<item>" 유지)가 Issue Table
#      과 Distribution 사이에 들어가고 Issue Table 의 TEMP 섹션은 사라진다. 다른 모드
#      세션은 값 무변경(빈 시트 1개 추가)이지만 키가 전 세션 공통이라 1회 재계산된다.
# v28: IssueTable CPK 섹션에서 Pass/Fail 단위 항목과 OTP_/CHIP_ID/CHIPID 이름 항목을
#      제외한다 (2026-08-10 사용자 요청 — tabs/issue_table._cpk_skip_subject). 전 모드
#      공통으로 **행 구성**이 달라지므로 안 올리면 옛 disk_cache 가 그 행이 든 payload 를
#      계속 반환한다.
# v29: Temperature 모드 CT/HT 의 CPK 를 "**RT 에서 Bin1 이던 die × RT limit**" 기준으로
#      계산한다 (2026-08-10 사용자 요청 — tabs/cpk.temperature_reference_tables). 자기 BIN
#      필터를 걸지 않으므로 cpk/average/stdev/n 과 표시 limit 이 모두 달라지고, 그 값을
#      쓰는 Issue Table CPK 섹션·Distribution status 도 함께 바뀐다. 다른 모드 세션은 값
#      무변경이지만 키가 전 세션 공통이라 1회 재계산된다.
# v30: signature 포함관계 억제(`suppressed_by`) 도입 — SEVERE_OUTLIER 가 뜨면 조건상 항상
#      따라오던 OUTLIER_WARN 을 발화 목록에서 뺀다. status(최대 severity)는 그대로지만
#      secondary_signatures·evidence·reason_codes 가 줄고, ai_comment._rank(동률 시 이봉
#      우선)가 그 목록을 보므로 AI Comment 셀 값이 바뀔 수 있다. 코드 배포에 따른 변경이라
#      rules_rev 가 아니라 이 값을 올린다.
# v31: IssueTable CPK 섹션 정렬을 "CODE_ 없는 항목 먼저, 각 덩어리 안에서 cpk 오름차순"
#      으로 바꾼다 (2026-08-11 사용자 요청 — tabs/issue_table._cpk_fail_subjects). v28 과
#      같은 성격으로 **행 순서**가 전 모드에서 달라지므로, 안 올리면 옛 disk_cache 가
#      종전 순서의 payload 를 계속 반환한다. 같은 배포에 들어간 STEP 표시 치환
#      (metrics._apply_step_label)은 세션 옵션(webreport_options)이 이미 키에 있어
#      이 값과 무관하게 갈린다.
# v32: "Issue Table Temp" 시트 행을 Bin 별로 묶는다 (2026-08-11 사용자 요청 —
#      tabs/temp_fail._group_by_bin). 행 **순서**가 Bin 그룹 단위로 재배열되고 접기 토글용
#      내부 필드(_grp/_detail/_ndetail)가 붙는다. 행 자체(항목당 1행)·row_key·값은 그대로
#      지만 v31 과 같은 성격이라, 안 올리면 옛 disk_cache 가 접기 마킹 없는 payload 를
#      계속 반환해 Temp 표가 종전 평면 목록으로 남는다.
# v33: 그 Temp 표의 **정렬 기준**을 소스 합산 fail die 수 → **avg(소스 평균 fail%)**
#      내림차순으로 바꾼다 (2026-08-11 사용자 확정 — 일반 Yield 표와 같은 기준). 항목
#      순서와 Bin 그룹 순서(대표 avg 순 = 가장 큰 Bin 최상단)가 함께 달라진다. 소스마다
#      분모가 다르면 v32 와 순서가 갈리므로 v32 캐시를 재사용하면 안 된다.
# v34: Issue Table 에 **Signature 컬럼**이 붙는다 (ai_comment 옵션 세션만) — 행에
#      Signature/_sig/_sigrev 키와 payload 최상위 signature_options 가 추가되고, 반대로
#      "Issue Table Temp" 시트에서는 **AI Comment 컬럼이 빠진다**(CT/HT 는 RT limit
#      재판정이라 저장 FAILTNO 기준 엔진 평가와 어긋난다 — 2026-08-11 사용자 결정).
#      AI Comment 를 안 쓰는 세션은 값·키 모두 무변경이지만 키가 전 세션 공통이라
#      1회 재계산된다. 안 올리면 옛 disk_cache 가 Signature 없는 payload 를 계속 반환한다.
# v35: eval 엔진이 **미분류 fail 에 UNKNOWN 을 명시 발화**하고(설명 못 한 fail 이 status=OK
#      로 새던 구멍 차단), UNIT 표에 %/LSB 를 등록해 PF 오분류를 줄이고, LOW_CPK·
#      WIDE_DISTRIBUTION·MEAN_SHIFT·HEAVY_TAIL 4룰을 다시 켰다(2026-08-12 — 실측 미분류
#      46.8%→6.9%). AI Comment 셀 텍스트·Signature 컬럼 값이 바뀐다. 룰 yaml 을 손으로
#      고쳤으므로 `.rules_rev`(패널 저장 카운터)로는 무효화되지 않아 여기서 올린다.
# v36: eval 룰셋 재편(2026-08-12 사용자 검토 반영) — ① SEVERE_OUTLIER+OUTLIER_WARN 을
#      **OUTLIER** 하나로 통합하고 판정을 비율이 아닌 **거리**(fail_robust_z_max ≥ 12,
#      MAD 기반 robust z)로 바꿨다 ② SPEC_TOO_TIGHT·WIDE_DISTRIBUTION 을 LOW_CPK 로 통합
#      (둘 다 off) ③ 공간 룰 E1/EDGE/CENTER/RING 을 **점유율 95%** 기준으로 재정의해 켜고
#      CLUSTER 는 임계 1.0→2.5 로 올려 켰다 ④ HEAVY_TAIL 임계 2.0→8.0 ⑤ SUBPOP_GAP →
#      **BIMODALITY** 개명. AI Comment 본문·Signature 컬럼 값이 광범위하게 바뀐다.
#      룰 yaml 을 손으로 고쳤으므로 `.rules_rev`(패널 저장 카운터)로는 무효화되지 않는다.
# v37: eval 룰셋 **2차** 재편(2026-08-13 사용자 v6 검토 반영) — ① OUTLIER 판정축을 거리 단독
#      에서 **거리 AND 끊김**(`fail_mad_min ≥ 4` AND `fail_pass_gap_sigma ≥ 1.5`)으로 교체.
#      거리만으로는 "꼬리가 이어져 규격을 넘은 것"(HEAVY_TAIL)과 "뚝 떨어져 나간 것"이
#      구분되지 않았다(실측에서 순서가 뒤집혔다) ② `suppressed_by` 의 의미를 "목록에서 제거"
#      → **"primary 만 양보"** 로 바꿔 여러 현상이 동시에 보이게 했다 ③ 양자화(계단형) 값에서
#      히스토그램 bin 이 어긋나 생기던 **BIMODALITY 오탐 제거**(격자 정렬).
#      AI Comment 본문·Signature 컬럼 값이 광범위하게 바뀐다(코드 변경이라 .rules_rev 로는
#      무효화되지 않는다).
# v38: eval 룰셋 **3차** 재편(2026-08-13 사용자 v8 검토) — ① HEAVY_TAIL 이 kurtosis 단독에서
#      **kurtosis>10 AND 꼬리질량 1~5%** 로(4제곱 지표라 점 몇 개에도 치솟고 다봉에서도
#      커지던 과대평가 해소) ② 룰 5종 **완전 삭제**(SPEC_TOO_TIGHT·SEVERE_OUTLIER·
#      WIDE_DISTRIBUTION·OUTLIER_WARN·WAFER_GRADIENT) ③ **SPOT_CLUSTER 신설**(fail 좌표
#      몰림 — 사분면 경계에 걸친 뭉침까지) + quadrant_imbalance 를 0°/45° max 로
#      ④ CODE_RAIL·BIDIR_TAIL 활성화, 이산(격자) 데이터의 BIMODALITY 는 빈 계단 ≥2 요구
#      ⑤ AI Comment 에서 [다봉] 배지 제거. Signature 컬럼·코멘트 값이 광범위하게 바뀐다.
# v39: OUTLIER 연속성 축 교체 + RING_FAIL 산포 하한(2026-08-14 사용자 v9 검토).
#      ① OUTLIER 의 끊김 조건이 `fail_pass_gap_sigma`(양쪽 꼬리 |z| 혼합 — 반대쪽에 더 먼
#      pass 가 있으면 음수) → **`fail_body_jump_ratio`**(같은 쪽에서 몸통~최근접 fail 구간의
#      최대 빈 폭 비율)로. 튄 값 때문에 죽은 fail 이 LOW_CPK/HEAVY_TAIL/UNKNOWN 으로 새던
#      것이 OUTLIER 로 잡히고, 반대로 HEAVY_TAIL 이 OUTLIER 에 primary 를 뺏기던 것도 해소.
#      ② RING_FAIL 에 `fail_spread_norm > 0.25` AND 추가 — ring 밴드 안의 한 점 뭉침이
#      전부 RING 으로 잡히던 것(SPOT_CLUSTER 겨냥 4건 전부)을 차단.
#      features.py 코드 변경이라 .rules_rev 로는 무효화되지 않아 전역 bump 가 필요하다.
# v40: Compare 계산 분리 (2026-08-19). payload 에 `compare_pending` 플래그가 생기고
#      compare 본문은 분리 캐시(compare_key)에서 주입된다 — 옛 payload 는 그 플래그를
#      모르므로 전역 bump 로 세대를 갈라야 프런트/Excel 이 pending 을 "데이터 없음" 으로
#      오독하지 않는다. **Compare 모드가 아닌 세션도 함께 콜드가 된다**(전역 bump 의 대가)
#      — 배포 직후 콜드 폭풍을 감안할 것(webreport-change 절차: 재기동 → 프리웜 스윕).
#      compare 구조만 바뀔 때는 여기가 아니라 COMPARE_SCHEMA_VERSION 을 올린다.
# v41: eval 룰셋 **4차** 재편(2026-08-19 사용자 지시). ① `CLUSTER_FAIL` 삭제(사분면 격자는
#      결함 모양과 무관한 인공 경계) ② `SPOT_CLUSTER` → **`SPOT_FAIL`** 개명 + CENTER_FAIL 과
#      함께 뜨면 **목록에서 제거**(신규 `hidden_by`) ③ `HEAVY_TAIL` → **`USL_TAIL`/`LSL_TAIL`**
#      방향 분리, 둘 다 뜨면 **`BIDIR_TAIL` 하나로 합침**(신규 `replaces`)
#      ④ `LOW_SAMPLE_UNCERTAIN` 삭제 ⑤ `outlier_sigma` 4.5 → 2.5.
#      Signature 컬럼·AI Comment 값이 광범위하게 바뀌고, features.py(방향별 꼬리 질량 신설)·
#      signatures.py·status.py 코드 변경이라 .rules_rev 로는 무효화되지 않는다.
# v42: CPK 탭 **TOTAL 행** 신설 (2026-08-27 사용자 요청) — payload sheets 에
#      `"CPK Total"`(전 source rawdata 를 하나로 통합한 항목별 1행, source="TOTAL",
#      source 별 행과 같은 15개 컬럼)이 새로 생긴다. 옛 disk_cache payload 에는 이 키가
#      아예 없어 프런트 폴백(`|| []`)으로는 "TOTAL 을 골라도 빈 표"가 되므로, 기존 세션에서
#      기능이 동작하게 하는 수단이 bump 뿐이다. TOTAL 은 4개 모드에서 생성돼 모드 전용
#      상수로는 대상 세션 대다수를 못 덮는다.
#      **sheets["CPK"] 는 손대지 않았다** — Issue Table CPK 섹션·distribution_index.cpk·
#      Excel·public API 의 값·목록은 전부 불변이다(규칙 13, tests/test_cpk_total.py 가 고정).
#      같은 세대에 보류돼 있던 Issue Table Compare 의 `Unit` 컬럼 payload 제거도 함께
#      실었다(전역 bump 가 Compare 세션도 어차피 재빌드하므로 추가 비용 0).
#      ⚠ 배포는 webreport-change 절차: 재기동 → **프리웜 스윕** → 벤치.
# v43: CPK 탭 **행 순서**를 item 이름 사전순 → **TEST SEQ(TSEQ) 순**으로 (2026-08-27 사용자
#      요청). 대상은 sheets["CPK"] 와 sheets["CPK Total"] **둘의 행 순서뿐**이며 행의 값·
#      키·개수는 전부 불변이다. 정렬 규칙은 distribution.tseq_sort_key 재사용(사본 금지 —
#      이미 Distribution 갤러리가 쓰던 규칙이라 두 탭의 항목 순서가 비로소 일치한다).
#      metrics 의 `stat_items` 자체는 **바꾸지 않았다** — Compare 의 pool 통계가 같은
#      리스트를 쓰므로 거기서 바꾸면 Compare 행 순서까지 함께 변한다. 별도 `cpk_items`.
#      cpk_rows 순서 의존처는 worst_cpk_by_subject 뿐인데 소비처(Issue Table CPK 섹션은
#      자체 sort, distribution_index 는 값 조회)라 다른 탭은 영향 없다.
#      값이 아니라 순서만 바뀌므로 옛 캐시가 조용히 남으면 "고쳤는데 그대로"가 되어
#      bump 가 유일한 반영 수단이다.
#      ⚠ 배포는 webreport-change 절차: 재기동 → **프리웜 스윕** → 벤치.
REPORT_SCHEMA_VERSION = 43

# Temperature 세션 **전용** payload 세대 — 값이 Temperature 모드에서만 바뀌는 변경은
# REPORT_SCHEMA_VERSION 대신 여기를 올린다. 전역 bump 는 전 세션의 report 캐시를 한 번에
# 무효화해 콜드 빌드 폭풍을 부른다(2026-08-06 조회 성능 급락의 원인 중 하나) — 영향 범위가
# 한 모드로 한정되면 그 모드만 갈아끼우는 편이 안전하다.
# v1: distribution_index 의 lower/upper_limit 을 그룹의 **RT limit** 으로 (2026-08-13).
#     종전에는 항목이 처음 등장한 소스의 limit 이라, 업로드 소스 순서상 첫 소스가 CT/HT 면
#     CT/HT 규격선이 나갔다. 프런트가 미니셀·갤러리 규격선을 이 인덱스에서 가져가므로
#     (static/webreport/distribution.js distSpecLimits) 캐시된 옛 payload 를 쓰면 안 바뀐다.
TEMPERATURE_SCHEMA_VERSION = 1

# Compare 세션 **전용** payload 세대 — TEMPERATURE_SCHEMA_VERSION 과 같은 취지다.
# ⚠ COMPARE_SCHEMA_VERSION(아래)과 혼동 금지:
#   COMPARE_SCHEMA_VERSION        = compare **계산 결과**(build_compare_payload) 캐시 세대
#   COMPARE_REPORT_SCHEMA_VERSION = 그 결과를 **report payload 에 어떻게 싣는지**의 세대
#     (시트 구성·행 구조). compare 계산은 그대로 재사용하면서 report 만 다시 굽고 싶을 때.
# v1: "Issue Table Compare" 시트 신설 (2026-08-20) — sheets 키가 늘어 옛 payload 를 쓰면
#     탭이 빈 화면이 된다.
# v2 (2026-09-01): Distribution 행에 `_sd_origin`(산포 증가 기원 표식) 추가 + focus 룰 v3 로
#     **행 목록 자체가 달라진다**. 옛 payload 를 쓰면 옛 판정으로 고른 행이 그대로 남는다.
COMPARE_REPORT_SCHEMA_VERSION = 2


def _eval_rules_suffix() -> tuple:
    """eval 룰 상태 키 꼬리표 — report_key(ai 세션)와 ai_comment_key 가 공유.

    rules_rev: /pe/eval 저장 시 +1 되는 카운터 — 룰 편집이 재평가를 강제한다.
    rev 파일이 없으면 빈 문자열이라 아무것도 덧붙지 않는다(기존 키 불변).
    "evalfail": 평가 범위(fail item 만 ↔ 전체 item) env 토글 표식 — rules_rev 가
    감지하지 못하므로 기본(fail-only)에서만 붙어, 되돌리면 종전 키 캐시가 재사용된다.
    "evalcpk" (2026-09-01): fail-only 범위의 **모집단이 코드로 바뀐** 표식 — fail item 에
    Issue Table CPK 섹션 후보(cpk<1.33)를 더했다(ai_comment.eval_fail_scope). 코드 변경은
    rules_rev(/pe/eval 저장 카운터)가 감지 못 하므로 여기서 갈아야 ai 옵션 세션의
    payload(Signature 셀 포함)·분리 캐시가 함께 재평가된다. 되돌리지 않는 영구 표식이며,
    전체 item 범위(env 0)는 모집단이 원래 전부라 붙지 않는다(비-AI 세션 키는 바이트 불변).
    "aiprec" (2026-09-02): report payload 에 `ai_precedents`(행별 사례 건수) 키가 생기고
    코멘트 3섹션이 [현상]/[사례]/[제안] 전량 형태로 바뀐 표식 — **payload 구조 변경**이라
    전역 REPORT_SCHEMA_VERSION 이 정석이지만 그건 전 세션 콜드 폭풍이다(규칙 14).
    이 꼬리표는 ai 옵션 세션의 report_key 에만 붙으므로 대상만 정확히 갈아낸다.
    "evalcpk" 와 같은 영구 표식이다(되돌리지 않는다).
    """
    from .eval_debug import rules_rev
    parts = ()
    rev = rules_rev()
    if rev:
        parts += ("rules" + rev,)
    from .ai_comment import fail_only_enabled
    if fail_only_enabled():
        parts += ("evalfail", "evalcpk")
    return parts + ("aiprec",)


def _eval_sensitivity_suffix(opts_raw: str) -> tuple:
    """세션 민감도 게이지 꼬리표 — ai_comment_key 전용.

    **이 꼬리표가 없으면 조용한 오답이 난다.** `ai_comment_key` 는 dedup 이익을 위해
    session_id 를 일부러 빼는데(perf_guard S10), 민감도는 `webreport_options` 에만 있고
    analysis_key·content_hash 에는 반영되지 않는다 — 같은 rawdata 를 서로 다른 민감도로
    두 번 올리면 두 세션이 **같은 키**를 공유해 두 번째가 첫 번째의 평가 결과를 그대로 본다.

    session_id 가 아니라 **설정값 digest** 를 넣는 것이 요점이다: 값이 같으면 형제 세션이
    계속 캐시를 공유하므로 S10 이 지키려던 이익은 그대로다.
    설정이 없으면 빈 튜플 = 기존 세션 키 바이트 불변(콜드 폭풍 회피 — _eval_rules_suffix 와 같은 규약).
    """
    ovr = webreport_eval_overrides(opts_raw)
    if not ovr:
        return ()
    canon = json.dumps(ovr, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ("sens" + hashlib.sha256(canon).hexdigest()[:12],)


def _ai_no_suggest_suffix(opts_raw: str) -> tuple:
    """"제안 제외" 꼬리표 — ai_comment_key 전용 (2026-09-02).

    **`_eval_sensitivity_suffix` 와 같은 이유로 반드시 있어야 한다.** 이 옵션은
    `webreport_options` 에만 있고 analysis_key·content_hash 에는 안 들어가는데,
    `ai_comment_key` 는 dedup 이익을 위해 session_id 를 일부러 뺀다(perf_guard S10).
    꼬리표가 없으면 같은 rawdata 를 "제안 제외"로 올린 세션과 아닌 세션이 **같은 키**를
    공유해, 제안 제외 세션이 남의 [제안] 문장을 그대로 보게 된다(조용한 오답).
    끈 세션(기본값)은 빈 튜플 = 기존 세션 키 바이트 불변(콜드 폭풍 회피).
    """
    return ("nosugg",) if webreport_ai_no_suggest(opts_raw) else ()


def report_key(session, session_id: str, edits_rev: int) -> tuple:
    # 전처리 변경은 edits_rev 증가로 무효화되므로 prep 을 따로 넣지 않는다
    # (rev 가 이미 키에 있어 덧붙여도 재사용 이득이 없다).
    key = _base(session) + (session_id, edits_rev,
                            session.get("webreport_options") or "", _mode(session),
                            REPORT_SCHEMA_VERSION)
    # Temperature 세션만 갈리는 세대 — 그 외 모드의 기존 캐시는 종전 키 그대로 유효하다.
    if _mode(session) == "Temperature":
        key += (TEMPERATURE_SCHEMA_VERSION,)
    # Compare 세션도 같은 방식으로 그 모드만 갈아끼운다 (Issue Table Compare 시트).
    if _mode(session) == "Compare":
        key += (COMPARE_REPORT_SCHEMA_VERSION,)
    # AI Comment 는 payload 안에 박혀 캐시되므로 eval 룰(threshold/signature)을 고치면
    # 이 키가 갈려야 재평가된다(/pe/eval 저장 시 rev +1). **ai_comment 옵션 세션에만**
    # 덧붙고 rev 파일이 없으면 빈 문자열이라, 그 외 세션의 기존 캐시는 그대로 유효하다.
    opts = session.get("webreport_options") or ""
    if webreport_ai_comment(opts):
        key += _eval_rules_suffix()
    return key


# AI Comment 평가 결과(build_ai_comments 반환 dict)의 캐시 세대 — _to_row_keys 반환
# **구조**(키 구성)가 바뀔 때만 올린다. 평가 **값**의 변화는 rules_rev(_eval_rules_suffix)
# 가 덮으므로 여기서 올리지 않는다. REPORT_SCHEMA_VERSION 과 무관(payload 와 분리 캐시).
# v2 (2026-08-19): 엔진 case 가 item 당 1개가 되면서 row_key 채움 방식이 바뀌었다
#   (대표 bin 1행 → 그 item 의 **모든 fail bin 행** fan-out). 반환 dict 의 키 구성이
#   달라지므로 옛 캐시를 재사용하면 Yield 행 일부가 빈 채로 굳는다.
# v3 (2026-08-28): ⚠ **위 "구조 변경 때만" 규약의 의도적 예외**다. 공간 룰
#   (E1/EDGE/CENTER/RING/SPOT)의 판정 모집단이 '측정된 die' → '전체 die' 로 바뀌고,
#   측정값이 없는 item 도 평가 대상이 됐다(엔진 ingest.py/features.py 코드 변경).
#   값 변화를 덮어 주는 rules_rev 는 **`/pe/eval` 저장 카운터라 코드 변경을 감지하지
#   못한다** — 그래서 여기서 올린다. 배포 시 `.rules_rev` 도 함께 +1 해야 report payload
#   안에 이미 구워진 AI Comment 셀까지 갈린다(이 상수는 ai_comment_key 에만 들어간다).
# v4 (2026-08-28): 반환 dict 에 `prompts`(클라 LLM 대행 프롬프트 — docs/23) 키 추가.
#   구조 변경이라 규약 그대로다. ai 옵션 세션만 1회 재평가 — 전역 프리웜 불필요.
# v5 (2026-08-28): `prompts` 안의 **프롬프트 본문** 확장(선례 상세 + 현재 통계 — docs/23).
#   dict 키 구조는 그대로지만 캐시에 굳은 옛 prompt/sha 를 갈지 않으면 클라가 계속 옛
#   프롬프트로 대행한다(sha 가 저장분과 맞아 폴백조차 안 걸린다). 여기만 올린다 —
#   report payload 는 무관하므로 전역 bump 금지(규칙 14).
# v6 (2026-09-01): 프롬프트 지시문 확장(_INSTRUCTION_EXTRA 에 "5줄은 상한이지 목표가
#   아니다" 3줄 — 근거 없는 줄 채우기 차단). v5 와 같은 이유로 여기만 올린다: 안 올리면
#   캐시에 굳은 옛 prompt/sha 가 그대로 나가 클라가 옛 지시문으로 대행한다.
# (2026-09-01) 평가 모집단 확장(fail ∪ CPK 섹션 후보)은 여기를 올리지 않고
#   `_eval_rules_suffix` 의 "evalcpk" 표식으로 갈았다 — 그 꼬리표는 report_key(ai 세션)와
#   ai_comment_key 가 공유하므로 payload 안에 구워진 Signature 셀까지 한 번에 갈린다.
#   반환 dict 구조는 그대로라 이 상수의 규약("구조 변경 때만")에도 맞다.
# v7 (2026-09-02): prompts 항목에 `precedents`(그 프롬프트에 실린 선례 건수) 추가 +
#   운영자 지시문(rules/ai_prompt.yaml)이 프롬프트에 합류. 지시문 **편집**은 rules_rev 가
#   갈아 주지만, **코드 배포로 처음 들어가는 기본 지시**는 rules_rev 가 감지하지 못한다
#   (그건 /pe/eval 저장 카운터다) — v5·v6 과 같은 이유로 여기만 올린다(전역 bump 금지).
# v8 (2026-09-01): 발화 signature **커버리지** 요구. signature 가 여러 개 걸려도 [제안]이
#   primary 하나만 다루던 문제 — 재료는 이미 전량 실렸고(_sig_lines 무제한) 원인은 지시
#   충돌이었다: v6 에서 넣은 축소 지시가 뒤에·더 구체적이라 _INSTRUCTION 의 "전체를
#   종합하라" 를 눌렀다. ① rules/ai_prompt.yaml 에 기본 지시 2개(cover_all_signatures /
#   signature_budget_first) ② _INSTRUCTION_EXTRA 축소 지시의 대상을 "발화 목록 밖" 으로
#   한정 ③ [발화 signature 전체] 헤더에 발화 건수 표기. ②③ 은 코드이고 ① 도 코드 배포로
#   처음 들어가는 기본 지시라 rules_rev 가 감지하지 못한다 — v5·v6·v7 과 같은 이유로
#   여기만 올린다(전역 bump 금지).
# v9 (2026-09-02): 반환 dict 에 `precedents`/`precedent_counts` 키 추가 + 프롬프트가
#   **두 블록 계약**([사례] 요약 / [제안] 통합)으로 바뀌고 선례 0건 item 은 프롬프트를
#   만들지 않는다. 구조 변경이라 규약 그대로다. payload 쪽 변화(ai_precedents 키·코멘트
#   3섹션 전량화)는 `_eval_rules_suffix` 의 "aiprec" 표식이 담당한다 — 전역 bump 금지.
# v10 (2026-09-02): 지시문을 **출력 형식 예시**로 다시 씀 — 현장에서 모델이 문장 대신
#   `{"precedent":…,"suggestion":{"text":…}}` 를 내 그대로 셀에 박혔다. 원인은 두 가지였다:
#   ① 종전 지시문은 `[사례]`/`[제안]` 을 **소제목처럼** 써서 "출력할 토큰" 으로 안 읽혔고,
#   ② 바깥 배치 래퍼가 "JSON 배열로 답하라" 를 요구해 안쪽 "JSON 쓰지 마라" 와 충돌했다
#      (call_claude/batch.py 가 "text 안에 또 JSON 을 만들지 마라" 로 분리 안내).
#   그래서 `[사례] <…>` / `[제안] <…>` 두 줄을 **예시로 못 박고** JSON 금지를 명시했다.
#   서버는 `unwrap_json_reply` 로 뒤에서도 걷어내지만 애초에 안 나오게 하는 게 낫다.
#   지시문이 바뀌면 sha 가 갈리므로 v5~v9 와 같은 이유로 여기만 올린다(전역 bump 금지).
# v11 (2026-09-02): [제안] 의 **분량·문체·중심**을 바꿨다(사용자 결정). ① 5줄 상한 →
#   전체 10줄 + signature 하나당 5줄 ② 내부 지표명·수치 출력 금지(`FAIL_MAD_MIN`
#   `TAIL_MASS_3S_HIGH` 류 — CPK·수율·단위 붙은 측정값만 예외) ③ [제안]의 중심을
#   action_ko 나열에서 **사례**로 이동 ④ 한 줄은 핵심 단어만.
#   ⚠ 금지는 **출력 문장에만** 건다 — 프롬프트 재료의 수치([근거:…]·[현재 통계]·선례
#   당시 통계)는 그대로다. 재료까지 빼면 "그때 값 vs 지금 값" 대조가 원리적으로 불가능해져
#   사례가 무용지물이 된다(되돌림 방지: tests/test_ai_prompt_determinism.py (t)).
#   ①②④ 는 `_INSTRUCTION`(+엔진 원본 recommend.py)·`_INSTRUCTION_EXTRA` 코드이고,
#   ③ 은 yaml 기본 지시(integrate_precedents 개정 + no_metric_names/terse_lines 신설)라
#   코드 배포로 처음 들어간다 — v5~v10 과 같은 이유로 여기만 올린다(전역 bump 금지).
# v12 (2026-09-02): [제안] 예산 10줄 → **12줄** + 예산 부족 시 줄이는 **순서**를 못 박았다
#   (사용자 결정). ① `integrate_precedents` — 기본 조치 문구를 그대로/요약해 쓰지 말고
#   사례가 실제로 내린 결론(진성·낙도성, 재현 여부, wait 안정화, 개발팀 협의 내용)을
#   지금 확인할 일로 바꿔 쓴다 ② `signature_budget_first` — 예산이 모자라면 **사례가 없는**
#   signature 줄부터 줄이고 사례가 있는 signature 의 구체적 판단 조치는 최대한 지킨다.
#   ①② 는 yaml 이라 `/pe/eval` 저장이면 rules_rev 가 갈아 주지만, 이번엔 **파일을 직접**
#   고쳤으므로 그 카운터가 안 움직인다. 게다가 줄 수(12줄)는 `_INSTRUCTION`(+엔진 원본
#   recommend.py)·`_INSTRUCTION_EXTRA` **코드**에도 있어 v5~v11 과 같은 이유로 여기서 올린다
#   (전역 bump 금지 — 이 상수는 ai_comment_key 에만 들어간다).
#   ⚠ 잘라내기 상한 `ai_prompt.MAX_SUGGESTION_CHARS` 도 1800 → 2160 으로 함께 올렸다.
#     안 올리면 12줄을 지시해 놓고 10줄어치에서 문장이 잘린 채 저장된다.
AI_COMMENT_SCHEMA_VERSION = 12


def _ai_meta_digest(session) -> str:
    """ai_comment._session_meta 가 평가 입력으로 쓰는 세션 메타의 digest.

    `PATCH /session/<sid>/meta` 는 analysis_key 를 재산출하지 않으므로(CLAUDE.md 규칙 #3)
    product/lot 등이 바뀌어도 akey 로는 감지할 수 없다 — 이 digest 가 재평가를 강제한다.
    필드 목록은 _session_meta 와 짝: product/product_type/family_product/lot_id/revision.
    (session_id·analysis_key 는 선례검색 자기제외용 전달값이라 평가 결과에 영향 없음 — 제외.)
    """
    fields = (session.get("product") or "", session.get("product_type") or "",
              session.get("family_product") or "", session.get("lot_id") or "",
              session.get("revision") or "")
    canon = "|".join(str(f).strip() for f in fields).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()[:12]


def report_pending_key(session, session_id: str, edits_rev: int,
                       kinds=("ai",)) -> tuple:
    """계산 **대기 중** payload 의 디스크 캐시 키 (2026-08-13 AI, 2026-08-19 Compare).

    정본 키(`report_key`)에 표식을 덧붙인 별도 키다. 정본 키에 그냥 저장하지 않는
    이유는 **롤백 안전**이다 — 이 기능을 되돌린 옛 코드가 정본 키에서 pending 본을 읽으면
    그 부분이 빈 채로 굳는다. 표식이 붙으면 옛 코드는 이 키를 만들지도, 읽지도 않는다.

    이 본이 없으면 백그라운드 잡이 끝나기 전의 재접속(서버 재시작·RAM 축출 후)이 매번 완전
    콜드 빌드가 된다 — 첫 조회만 빠르고 재접속은 느린 것이 사용자에게는 회귀로 보인다.

    `kinds` 는 이 본에서 **비어 있는 부분**의 집합이다. AI 와 Compare 는 동시에 대기할 수
    있으므로 키가 갈려야 한다 — 안 그러면 "AI 만 빈 본"과 "둘 다 빈 본"이 같은 키를 놓고
    서로 덮어써, 이미 계산된 Compare 가 사라진 본이 최종본처럼 재사용된다.
    ai 단독은 종전 꼬리(`aipending`)를 그대로 써서 **기존 파일이 계속 유효**하다.
    """
    tail = tuple(sorted(str(k) for k in (kinds or ()) if k)) or ("ai",)
    if tail == ("ai",):
        return report_key(session, session_id, edits_rev) + ("aipending",)
    return report_key(session, session_id, edits_rev) + ("pending",) + tail


# Compare payload(build_compare_payload 반환 dict)의 캐시 세대 — compare **구조**가
# 바뀔 때만 올린다. Compare 모드 세션에만 붙는 키라 여기를 올려도 다른 모드의 report
# 캐시는 그대로다(전역 REPORT_SCHEMA_VERSION bump 가 부르는 콜드 폭풍 회피 —
# TEMPERATURE_SCHEMA_VERSION 과 같은 취지).
# v2 (2026-08-20): new_items(After 에만 있는 신규 test item) 추가.
# v3 (2026-09-01): dist_shift focus 판정 **룰 v3** — 과검출(변화 없음·개선·미세 σ) 제외
#     게이트 + 형태 차이(ks_d) 검출 신설, thresholds 키 확장, tail_ratio_* 2개 추가.
#     focus 불린이 바뀌므로 옛 캐시를 쓰면 Issue Table Compare 행 구성이 옛 판정으로 남는다.
COMPARE_SCHEMA_VERSION = 3


def compare_key(session, prep_digest: str = "") -> tuple:
    """Compare 계산 결과 캐시 키 — report payload 캐시와 **분리** (2026-08-19).

    `ai_comment_key` 와 같은 논리로 session_id·edits_rev 를 넣지 않는다: compare 입력은
    tables(= chash + prep) + Before/After 배치 + mode 뿐이라 comment/override 편집
    (edits_rev+1)·REPORT_SCHEMA_VERSION bump·dedup 형제 세션에서 재계산할 이유가 없다.
    종전에는 이것들이 전부 compare 전량 재계산(실측 1.1초)을 유발했다.

    배치는 **정규화된 compare_groups 가 아니라 원본 `webreport_options` 문자열**로 넣는다.
    정규화에는 소스 이름이 필요해 tables 를 디코드해야 하는데, 이 키는 tables 를 열기 전
    (콜드 판정·pending 판정)에도 같은 값이 나와야 하기 때문이다. 소스 이름 자체는
    content_hash 에 이미 반영돼 있어(_base) 정보 손실은 없다 — report_key 와 같은 규약.
    """
    return (_base(session, prep_digest)
            + (_mode(session), session.get("webreport_options") or "",
               COMPARE_SCHEMA_VERSION))


def ai_comment_key(session, prep_digest: str = "", stage: str = "") -> tuple:
    """AI Comment 평가 결과 캐시 키 — report payload 캐시와 **분리** (2026-08-13).

    session_id·edits_rev 를 넣지 않는 것이 핵심이다: 평가 입력은 tables(= chash + prep)
    + 세션 메타(_ai_meta_digest) + eval 룰(_eval_rules_suffix)뿐이라, comment/override
    편집(edits_rev+1)·REPORT_SCHEMA_VERSION bump·dedup 형제 세션에서 재평가가 필요 없다.
    selected_items 는 analysis_key 산출에 이미 포함(모듈 docstring)이라 따로 넣지 않는다.
    ⚠ 선례검색이 실제 DB 를 갖게 되면(meta 의 session_id 자기제외로 세션마다 선례가
    달라질 수 있음) 이 전제가 흔들린다 — LLM/선례 활성화 시 세션 축 추가를 재검토할 것.

    `stage` 는 2026-08-28 에 붙은 **2단계 분리** 축이다. LLM 이 켜지면 L5(코멘트 합성)가
    케이스마다 HTTP 왕복이라 전체가 수십 초로 늘어나는데, Signature(L1~L4)는 그 전에
    이미 확정돼 있다. `stage="sig"` 는 코멘트 없이 판정만 담은 중간 결과의 별도 캐시다.
    기본값("")은 **종전 키 바이트 그대로** — 기존 디스크 캐시가 그대로 유효하다
    (여기에 빈 튜플을 더하므로 최종본 키는 한 글자도 바뀌지 않는다).
    """
    return (_base(session, prep_digest) + (_mode(session), _ai_meta_digest(session))
            + _eval_rules_suffix()
            + _eval_sensitivity_suffix(session.get("webreport_options") or "")
            + _ai_no_suggest_suffix(session.get("webreport_options") or "")
            + (AI_COMMENT_SCHEMA_VERSION,)
            + ((str(stage),) if stage else ()))


def trim_key(session, session_id: str, edits_rev: int, source: str) -> tuple:
    # report_key 와 동일 — edits_rev 가 전처리 변경을 덮는다.
    return _base(session) + (session_id, edits_rev, _mode(session), str(source or ""))


def trim_chart_key(session, source: str, items_digest: str, prep_digest: str = "") -> tuple:
    return _base(session, prep_digest) + (_mode(session), str(source or ""), items_digest)


def full_key(session, session_id: str, edits_rev: int, extras_digest: str) -> tuple:
    # /full 은 report payload 를 감싼 응답 gzip 캐시 — 전처리 변경은 edits_rev 증가로
    # 함께 무효화되므로 prep 을 따로 넣지 않는다.
    return _base(session) + (f"{session_id}:{edits_rev}", extras_digest)


def scatter_key(session, subject: str, *, bin1: bool = False, prep_digest: str = "",
                bin1_scope: str = "") -> tuple:
    # bin1=True 는 양품(Bin1)만으로 낸 상세 — 전체 기준과 별도 캐시(키에만 추가).
    return (_base(session, prep_digest) + (_mode(session), subject)
            + _bin1_suffix(bin1, bin1_scope))


# Gap Chart 응답 구조를 바꿀 때만 올린다 (build_gap_item 의 반환 키/형태).
# 전역 REPORT_SCHEMA_VERSION 과 무관 — gap 은 report payload 에 실리지 않는다.
#   v2: 응답에 tokens 추가 (Item_detail 헤더에 수식을 원래 서식으로 표시)
GAP_SCHEMA_VERSION = 2


def gap_key(session, chart_id: str, spec_digest: str, *, bin1: bool = False,
            prep_digest: str = "", bin1_scope: str = "") -> tuple:
    """Gap Chart 조회 응답(gzip bytes) 캐시 키.

    **spec_digest 는 기본값 없는 필수 인자다** — 빠뜨리면 TypeError 로 즉시 터지게 해서
    "수식을 고쳤는데 옛 숫자가 그대로" 를 구조적으로 막는다. 값은
    `gap_chart.spec_digest(spec)` 하나만 쓴다(라우트가 ETag 에도 같은 값을 박는다).

    **edits_rev·sid 를 넣지 않는 이유**는 ai_comment_key/compare_key 와 같다 — edits_rev 는
    남이 코멘트 한 줄만 쳐도 올라가므로, 키에 넣으면 그때마다 이 차트 캐시가 통째로 죽는다.
    이 차트의 수식이 바뀌었는지는 spec_digest 하나로 정확히 판별된다."""
    return (_base(session, prep_digest)
            + (_mode(session), str(chart_id), str(spec_digest), GAP_SCHEMA_VERSION)
            + _bin1_suffix(bin1, bin1_scope))

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
| AI_COMMENT_CACHE   | (akey, chash[, prep], mode, meta_digest[, rules_rev][, "evalfail"], aiver) | raw_data 편집 / 전처리 / 세션 메타(PATCH) / eval 룰 편집 — **edits_rev·sid 무관**(comment 편집으로 재평가 안 함) |

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

from .validation import validate_mode, webreport_ai_comment


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


def dist_batch_key(session, subjects_digest: str, *, bin1: bool = False,
                   prep_digest: str = "", bin1_scope: str = "") -> tuple:
    """항목 배치 ECDF(GET .../distribution_batch) 응답 gzip 캐시 키.

    dist_key 와 같은 (akey, chash, mode) 기반에 요청 항목 집합의 digest 를 더한다 —
    배치 구성이 스크롤에 따라 달라지므로 집합 자체가 키의 일부다. 전체 dist 캐시와
    같은 세션을 가리키지만 별도 캐시라 서로를 무효화하지 않는다.
    """
    return (_base(session, prep_digest) + (_mode(session), str(subjects_digest))
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
    return _base(session, prep_digest) + (_mode(session), MAP_SCHEMA_VERSION)


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
REPORT_SCHEMA_VERSION = 38


def _eval_rules_suffix() -> tuple:
    """eval 룰 상태 키 꼬리표 — report_key(ai 세션)와 ai_comment_key 가 공유.

    rules_rev: /pe/eval 저장 시 +1 되는 카운터 — 룰 편집이 재평가를 강제한다.
    rev 파일이 없으면 빈 문자열이라 아무것도 덧붙지 않는다(기존 키 불변).
    "evalfail": 평가 범위(fail item 만 ↔ 전체 item) env 토글 표식 — rules_rev 가
    감지하지 못하므로 기본(fail-only)에서만 붙어, 되돌리면 종전 키 캐시가 재사용된다.
    """
    from .eval_debug import rules_rev
    parts = ()
    rev = rules_rev()
    if rev:
        parts += ("rules" + rev,)
    from .ai_comment import fail_only_enabled
    if fail_only_enabled():
        parts += ("evalfail",)
    return parts


def report_key(session, session_id: str, edits_rev: int) -> tuple:
    # 전처리 변경은 edits_rev 증가로 무효화되므로 prep 을 따로 넣지 않는다
    # (rev 가 이미 키에 있어 덧붙여도 재사용 이득이 없다).
    key = _base(session) + (session_id, edits_rev,
                            session.get("webreport_options") or "", _mode(session),
                            REPORT_SCHEMA_VERSION)
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
AI_COMMENT_SCHEMA_VERSION = 1


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


def report_pending_key(session, session_id: str, edits_rev: int) -> tuple:
    """AI 평가 **대기 중** payload(ai_comment_pending)의 디스크 캐시 키 (2026-08-13).

    정본 키(`report_key`)에 표식 하나를 덧붙인 별도 키다. 정본 키에 그냥 저장하지 않는
    이유는 **롤백 안전**이다 — 이 기능을 되돌린 옛 코드가 정본 키에서 pending 본을 읽으면
    AI Comment 가 빈 채로 굳는다. 표식이 붙으면 옛 코드는 이 키를 만들지도, 읽지도 않는다.

    이 본이 없으면 AI 잡이 끝나기 전의 재접속(서버 재시작·RAM 축출 후)이 매번 완전
    콜드 빌드가 된다 — 첫 조회만 빠르고 재접속은 느린 것이 사용자에게는 회귀로 보인다.
    """
    return report_key(session, session_id, edits_rev) + ("aipending",)


def ai_comment_key(session, prep_digest: str = "") -> tuple:
    """AI Comment 평가 결과 캐시 키 — report payload 캐시와 **분리** (2026-08-13).

    session_id·edits_rev 를 넣지 않는 것이 핵심이다: 평가 입력은 tables(= chash + prep)
    + 세션 메타(_ai_meta_digest) + eval 룰(_eval_rules_suffix)뿐이라, comment/override
    편집(edits_rev+1)·REPORT_SCHEMA_VERSION bump·dedup 형제 세션에서 재평가가 필요 없다.
    selected_items 는 analysis_key 산출에 이미 포함(모듈 docstring)이라 따로 넣지 않는다.
    ⚠ 선례검색이 실제 DB 를 갖게 되면(meta 의 session_id 자기제외로 세션마다 선례가
    달라질 수 있음) 이 전제가 흔들린다 — LLM/선례 활성화 시 세션 축 추가를 재검토할 것.
    """
    return (_base(session, prep_digest) + (_mode(session), _ai_meta_digest(session))
            + _eval_rules_suffix() + (AI_COMMENT_SCHEMA_VERSION,))


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

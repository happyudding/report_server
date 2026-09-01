# 11 · web_report — 탭 데이터 계약 & 렌더

> web_report 각 탭의 **서버 데이터 계약**(무엇을 계산해 내려주는가)과 렌더 구조.
> 관련: 파이프라인 [10](10_web_report_pipeline.md) · 캐시 [12](12_web_report_cache.md)

`web_report/tabs/` 는 **데이터만 공급**하고, 실제 화면 UI(표/차트/필터/편집)는
[report_view.html](../server/report/report_view.html) + [static/webreport/](../server/report/static/webreport/)
의 JS 모듈에 있다.

## 탭 레지스트리 (시트 구성의 단일 진실)
[tabs/__init__.py](../web_report/tabs/__init__.py) 의 `TAB_REGISTRY` 가 시트 목록·순서의
단일 진실. `metrics.build_report_payload` 가 공용 컨텍스트(yield/cpk 등 1회 계산)를 조립해
레지스트리를 순회한다 — 개별 탭 이름을 모른다.

**새 탭 추가 절차**: ① `tabs/` 에 빌더 모듈 1개 ② `TAB_REGISTRY` 에 `TabSpec` 1줄
(표시 순서 = 레지스트리 순서) ③ 프런트 JS 모듈 1개(static/webreport/).

| 탭 | 빌더 | 요지 |
|----|------|------|
| Summary | `summary.py` | placeholder(`[]`) — 화면은 프런트가 Map/Fail Bin 으로 자체 구성 |
| Raw Data | `raw_data.py` | placeholder. ⚠ **프런트 탭은 없다**(2026-08 제거 — rawdata 편집은 Honey 사이드바 'Rawdata 수정'으로 이관). 시트만 남아 lazy 조회/편집 라우트가 쓴다 |
| Yield | `yield_tab.py` | `build_yield_rows` + fail_counts/fail_bin_ranking/yield_overview + STEP 분리(`build_yield_step_groups`). **Temperature 는 RT source 만** 입력으로 받는다(metrics 가 결정) |
| CPK | `cpk.py` | `build_cpk_rows` (source 별 행) — 통계는 **Bin1(양품) 기준 단일 값**. 유일한 예외 = Temperature 의 CT/HT(**RT Bin1 die × RT limit**, 2026-08-10). 전 source 통합 행은 `sheets["CPK Total"]` **별도 시트**(TAB_REGISTRY 밖, 2026-08-27) |
| Issue Table | `issue_table.py` | Yield 파생 + cpk<1.33 파생(Bin1 기준, **Pass/Fail 단위·`OTP_`/`CHIP_ID`/`CHIPID` 이름 항목 제외** — `_cpk_skip_subject`, 2026-08-10) + ETC. comment/Status/행 숨김은 편집 DB 에서 채움. **Temperature 는 RT source 만**(TEMP 는 아래 별도 시트로 분리) |
| Issue Table Temp | `temp_fail.py` | **Temperature 전용** — CT/HT 를 RT limit 으로 **전 항목** 재판정한 item 단위 행(다른 모드는 `[]`). row_key `TEMP\|<item>` |
| Distribution | — (lazy, 항목 배치) | `/full` 은 빈 시트 + `distribution_index`(항목 목록). ECDF 는 **화면에 보이는 항목만** `GET .../web_report/distribution_batch?subjects=…` 로 받는다 |
| Trim Analysis | — (lazy, **버튼 시작**) | `/full` 은 빈 시트. **탭 진입만으로는 아무 요청도 안 한다** — 「분석 시작」을 눌러야 `GET .../web_report/trim_analysis` 를 받고, 그 뒤 차트는 `GET .../web_report/trim_chart_batch` 로 **한 페이지 6개씩**. ⚠ 화면상으로는 최상위 탭이 아니라 **Characteristic 탭의 서브탭**이다(아래 절) |
| Map Analysis | `Map_analysis.py` (하이브리드 lazy) | wafer map die/bin 집계 — `/full` 은 dies 뺀 경량 메타(`include_dies=False`), die 전량은 `GET .../web_report/map_analysis` 지연 로드 (schema v8) |
| Fail Bin | `yield_tab.fail_bin_ranking` | Bin 랭킹 |
| Note | — (클라 전용) | TAB_REGISTRY 밖 — 프런트 자체구성 Luckysheet 캔버스, 아래 "Note 탭" 절 |
| Issue Table Compare | `compare_issue.py` | **TAB_REGISTRY 밖** — Compare 세션일 때 `metrics.py` 가 `sheets["Issue Table Compare"]` 에 직접 주입한다. 레지스트리만 보고 "탭 목록"을 세면 이게 빠지니 주의 |

⚠️ **표의 이름 = 시트(sheets) 키이지 화면 탭 목록이 아니다.** 둘은 대체로 같지만 어긋나는
곳이 셋 있다: `Raw Data`(시트만 있고 탭 없음) · `Trim Analysis`(Characteristic 의 서브탭) ·
`Issue Table Compare`(탭은 있는데 레지스트리 밖). 실제 상단 탭 10개는
[report_view.html](../server/report/report_view.html) 의 `data-tab` 이 정본:
`summary` / `yield` / `cpk` / `issues` / `issue-temp` / `issue-cmp` / `distribution` /
`map-analysis` / `characteristic` / `note`.

> 구 최상위 `compare` 탭은 **2026-08-27 `issue-cmp` 에 흡수됐다** — 아래
> "Issue Table Compare (서브탭 5개)" 절 참조.

**lazy 탭 관례**: 대용량 payload(Distribution ECDF, Trim 매칭)는 `/full` 에 싣지 않고
빈 시트로 두고 전용 라우트로 지연 로드한다. Map Analysis 는 하이브리드 — 범례·격자 틀이
쓰는 경량 메타(source/x·y min·max/total/bin_counts[/step][/duts])는 `/full` 에 남기고
dies(STEP 분리 시 수백만 객체 — 메인스레드 JSON 파싱 freeze 의 주범)만 분리한다
(`map_deferred: true`, 프런트 `ensureMapData`/`fetchMapViaWorker` — wafer_charts.js).
`/full` 경로는 `build_map_analysis_rows(include_dies=False)` 로 **die dict 를 애초에 만들지
않는다** — 종전엔 전량 생성 후 `strip_dies` 로 버렸다(같은 결과, 낭비만 제거).
DUT 모드만 예외로 dies 를 만든다(`_merge_dut_rows` 가 병합 입력으로 쓴다) — 그래서
`strip_dies` 는 안전망으로 남아 있다.

**Map 3초 SLA** (2026-08-10, CLAUDE.md §5-11): gross die 10,000 × 7 source 세션에서 Map
Analysis 첫 화면과 **Issue Table 의 Map 컬럼**(같은 응답을 소비 — issue_dist.js)은 3초
안에 떠야 한다. 지연 로드만으로는 첫 진입이 콜드 202 + 전체 재디코드라 30초+ 걸렸으므로,
report 콜드 빌드가 map dies gzip 을 함께 시딩한다(`service.seed_map`) — 첫 진입은 항상
RAM/디스크 히트다. 다운샘플로 달성하지 말 것(규칙 §5-5). 상세 [docs/12](12_web_report_cache.md).

**Map Detail(크게 보기) 범례** (2026-08-06): 갤러리 범례는 전 소스 합산이지만 Detail 은
**지금 보고 있는 맵 1장** 기준이다 — Bin Legend 의 count·비율이 화면의 웨이퍼와 일치한다
(`renderMapDetailLegend`, 색은 세션 공통 `globalBinColorMap` 이라 갤러리와 같은 bin=같은 색).
Detail 은 `Temperature Map` 축도 갤러리와 같은 색으로 그린다(`mapDetailAxis` temp 분기 —
`waferHeatmap` 의 `catOf(d, k)` 2번째 인자 = die 인덱스).

**Map Analysis eval STEP 제외** (2026-07-21): STEP 이름에 `eval`(대소문자 무시)이 들어가면
맵을 그리지 않는다(`_is_eval_step`, [Map_analysis.py](../web_report/tabs/Map_analysis.py)).
STEP 이 eval 하나뿐인 소스는 맵 자체가 없다. fail step 귀속(`_fail_step_indexes`)에는
원래 STEP 목록을 그대로 써 앞/뒤 step 판정(Pass·회색)이 어긋나지 않게 하되, **eval STEP 에서
fail 한 die 는 그리는 맵들에선 Pass** 로 남기고(`skip_idx`), fail step 불명 die 는 첫 step 이
아니라 **그리는 첫 step**에 귀속시킨다(`unknown_idx`). 맵 rows 값이 바뀌므로
`cache_policy.MAP_SCHEMA_VERSION`(map_key 는 edits_rev 가 없어 이 값만이 무효화 수단) +
`REPORT_SCHEMA_VERSION` 을 함께 올렸다 — **서버 재시작 필요**.

## 주요 탭 계약
- **Temperature 개편 (2026-08-05)** — Yield 는 RT 기준, CT/HT 는 별도 시트:
  - **Yield 계열은 RT source 만** 본다(Yield 시트·`yield_summary`·Bin/STEP 그룹·
    `issue_bin_summary`·Fail Bin·Issue Table). `metrics.build_report_payload` 가
    `yield_tables`(= RT subset)를 만들어 넘기는 한 지점에서 결정된다 — 구
    `yield_corner_groups` 키와 `build_yield_corner_groups` 는 **삭제**됐다.
  - **CT/HT 는 신규 시트 `sheets["Issue Table Temp"]`** 로 나간다(Issue Table 과
    Distribution 사이, 프런트 탭 `data-tab="issue-temp"` — Temperature 에서만 노출).
    첫 행은 섹션 divider(`Category="TEMP"`)이고 이후가 데이터 행이다. 컬럼 소스는
    **CT/HT 만**, 정렬은 소스 합산 fail die 수 내림차순.
  - **판정은 조회 시점 서버 재계산**이다(`tabs/temp_fail.py`). 업로드 전 정리
    (`web_report/temperature.py`)는 RT pass 좌표 필터 + **첫 fail 하나만** BIN/FAILTNO 에
    적지만, 여기서는 좌표 필터가 이미 반영된 parquet 위에서 **모든 항목**을 RT 의
    LOLIM/HILIM 으로 다시 판정한다 → 한 die 가 여러 항목을 벗어나면 그 항목 전부에
    계상되고, 소스별 fail% 합이 **100% 를 넘을 수 있다**(사용자 확정). 클라 정리 로직은
    한 줄도 바뀌지 않아 기존 세션도 재배포 없이 새 화면이 된다.
  - **좌표 없는 rawdata (2026-08-24 사용자 요청)** — 업로드 전 정리의 RT pass 좌표 필터는
    좌표가 비어 있으면 아무것도 걸러내지 못한 채 **조용히 통과**한다(RT 에서 죽은 die 가
    CT/HT 에 남아 재판정이 통째로 틀린다). 그래서 Honey 는 파싱 직후(배치 확정 후, 인코딩
    전) 좌표 없는 source 를 찾아 **파일 목록 + 확인창**을 띄운다
    ([honey_main.py](../client/honey_main.py) `_temp_coord_check`):
    - **Yes** → `clean_frames(..., serial_match=True)` — 좌표가 없는 **pair 만** SERIAL
      오름차순 i 번째끼리 짝지어 같은 규칙(RT BIN==1 인 짝만 남김)을 적용한다. 양쪽에
      좌표가 있는 pair 는 종전 좌표 매칭 그대로다(pair 단위 판정).
      행 개수가 다르면 **적은 쪽 기준**으로 앞에서부터만 짝짓고("가장 적은 raw data
      기준으로 진행합니다" 안내), 남는 행은 버린다.
    - **No** → Web Report 생성 중단(rawdata 좌표 수정 요청).
    판정 기준은 `temperature.has_coords` 한 곳이다 — XPOS/YPOS 가 **둘 다** 채워진 데이터
    행이 하나라도 있으면 "좌표 있음"(부분 결측은 종전 좌표 매칭 유지). 서버·manifest 는
    무변경이다(정리 결과가 곧 parquet 이고, 조회 경로는 좌표로 pair 를 잇지 않는다) —
    좌표가 없으므로 그 소스의 웨이퍼 맵만 비어 있게 된다.
    회귀 고정: [tests/test_temperature_serial_match.py](../tests/test_temperature_serial_match.py).
  - **Bin 표기**: `manifest["temperature_limits"]`(.lt/.pds 유래, 신규 업로드만)의
    `usl_bin` — `.lt` 의 `20:19` 는 **콜론 오른쪽(19)만** 쓴다. 없으면 관측 bin 최빈값
    (member → RT 순, `"999"` 제외), 그래도 없으면 공백. 행은 항상 item 1개다 —
    row_key `TEMP|<item>` 을 유지해야 기존 comment/Status 와 파서 4곳이 안 깨진다.
  - **판정은 1회만 돈다**: `compute_temp_fail` 이 (count, die 인덱스)를 한 순회로 만들고
    tables 클론에 캐시한다 — 표(`build_temp_fail_rows`)와 Map(`temp_fail_indices`)이 그
    결과를 공유한다. 21 source 세션에서 같은 판정을 두 벌 돌던 것을 없앤 지점이다.
  - **Map 항목 legend**: Map Analysis 색 기준 축에 `Temperature Map`(구 `Temp Item`,
    2026-08-06 개명)이 추가된다(Temperature 전용). **Temperature 모드에서는 축이 소스를
    가른다** — `Bin`/`TNO` 축 = RT 소스 맵만, `Temperature Map` 축 = CT/HT 소스 맵만
    (`wafer_charts.js mapVisibleMaps`, 갤러리·Detail 공용이라 인덱스가 어긋나지 않는다).
    항목별 fail die 는 `GET .../web_report/temp_map` 이 **dies 배열 인덱스**로
    내려준다(`{"format":"temp-map-v1","sources":[{source,n,items:[{item,idx}]}]}`) —
    map_analysis 응답에 얹지 않는 이유는 프런트 Worker 가 dies/metas 외 필드를 버리기
    때문이다. 인덱스 기준은 `Map_analysis` 의 `XPOS/YPOS notna` mask 와 **문자 그대로
    동일**해야 한다(회귀 고정: `test_temperature_fail_eval.test_indices_align_with_map_dies`).
    Issue Table Temp 의 Map 셀(`data-temp-item`)도 같은 데이터를 쓴다.
    **report 콜드 빌드가 `service.seed_temp_map` 으로 RAM+디스크를 미리 채운다** — 안 하면
    Issue Table 첫 진입에서 요청 스레드가 전 항목 판정을 다시 돈다. 라우트 단독 콜드는
    워커 오프로드(`compute.temp_map_job`).
  - **legend 는 전 항목을 나열하고 항목마다 서로 다른 색**이다(2026-08-06 — 구 "상위 7항목
    + 기타 회색" 폐지). 팔레트(`FAIL_PALETTE` 7색)를 먼저 쓰고 그 뒤는 황금각 색상환 회전
    (`tempItemColorAt`, Pass 초록 대역 제외)으로 만든다. 클릭하면 그 항목 fail die 만 강조.
    Detail(크게 보기) 범례는 **그 소스에서 fail 난 항목만** + 그 소스 die 수
    (`tempItemInfoForSource`) — Bin Legend 와 같은 규약이다. 항목명은 말줄임 없이 전부
    보여준다(CSS `.temp-leg-item` 줄바꿈, 2026-08-11). **이탈 없는 die 는 Pass 초록이
    아니라 연한 바탕색**(`TEMP_MAP_BASE_COLOR`) — 온통 초록이면 범례 클릭 강조가 묻힌다.
  - **Temp 표는 Bin 별로 묶인다** (2026-08-11) — 서버 `temp_fail._group_by_bin` 이
    **avg(소스 평균 fail%) 내림차순**을 유지한 채 같은 Bin 을 모아 **avg 최대 항목 행을
    대표**로 두고(= 그 Bin 이 처음 등장하는 순서가 곧 대표 avg 순 → 가장 큰 Bin 이 최상단)
    나머지에 `_detail` 마킹을 단다(Issue Table Yield 섹션과 같은 `_grp`/`_detail`/
    `_ndetail` 규약 — 프런트 sheets.js 가 두 표를 같은 코드로 접는다). 대표행이 집계행이
    아니라 **항목 행 자체**인 점만 다르다: row_key 가 `TEMP|<item>` 이라 집계행에 줄 키가
    없고, 항목끼리 die 가 겹쳐 합산이 뜻을 갖지 않는다. Bin 이 빈 항목은 묶지 않고 뒤에
    붙인다. **Excel(웹/Honey 둘 다)은 TEMP 접힘 행을 대표행에 합치지 않는다** — 화면
    접기일 뿐 행 하나가 독립 항목이라, 합치면 항목이 사라진다.
    2026-08-25 부터 이 표도 **펼치면 Bin 집계 헤더행이 선다**(수치는 빈칸, 대표행은 상세행으로
    복제) — 아래 §"Bin 그룹 펼침 = 집계 헤더행 분리" 의 ② 경로다.
  - **Yield 탭 하단 Temp Corner 섹션은 요약**이다 — 편집 열(Map/Distribution/Status/comment)
    을 뺀 읽기 전용이고 행은 전량 청크 렌더한다(Bin 묶음·접기는 위와 동일, 토글은
    `.yield-toggle`). 편집은 "Issue Table Temp 탭에서".
  - **Issue Table(메인)의 Bin 미니맵·⤢ 는 RT 소스 맵만 본다** (`issueBinMaps`, 2026-08-11)
    — 그 표가 RT source 기준이라 CT/HT 맵이 뜨면 표의 Bin 과 어긋난다. 항목별 fail die 를
    그리는 Temp 미니셀(`map-cell-temp`)은 반대로 CT/HT 가 대상이라 이 목록을 쓰지 않는다.
  - **Issue Table(메인)의 Distribution 미니셀도 RT 소스만 그린다** (2026-08-12) — Bin 미니맵과
    같은 이유다(표가 RT 기준). 셀에 `data-src-scope="rt"`(sheets.js)를 달고
    `renderMiniDistCell`(item_detail.js)이 `tempFilterSources("RT","")` 로 `bySource` 를 걸러
    낸다 — 서버 payload 는 그대로 전 소스이고 **그리는 목록만** 좁힌다(표시점 캡
    `distCapFor` 는 걸러낸 소스 수로 계산). 겹치는 RT 소스가 없으면 빈 칸으로 확정한다.
    같은 변경에서 메인 CPK 섹션 미니셀 variant 를 `bin1` → **`rtbin1`** 로 옮겼다: CT/HT 의
    저장 BIN 은 업로드 정리의 "첫 fail" 값이라 plain `bin1` 을 걸면 CT/HT 곡선이 통째로 비어
    "일부 source 만 보이는" 증상이 됐고, `rtbin1` 이 그 표의 CPK 숫자(RT Bin1 die × RT limit)와
    같은 기준이며 `Issue Table Temp` 탭과 캐시(`distRtBin1Cache`)·배치 요청까지 공유한다.
    **`Issue Table Temp` 탭은 반대로 전 소스**(RT 만 Bin1, CT/HT 는 fail 포함 전체)라
    `data-src-scope` 를 달지 않는다.
  - **Honey 전체 Excel** 에도 `Issue Table Temp` 시트가 들어간다(Temperature 세션만) —
    Distribution 열은 항목 CDF, Map 열은 **항목별 fail die 강조** 썸네일
    (`_map.render_temp_map_png`, temp_map 인덱스 기준). temp_map 수신 실패 시 Map 열만
    비우고 시트는 만든다.
  - **Distribution**: 소스 그룹 필터의 그룹 라벨에서 `_RT` 접미사를 뗀다. 신규 버튼
    `Bin1 (RT만)` = `?bin1=1&bin1_scope=rt` — RT 소스만 양품·규격내로 좁히고 CT/HT 는
    fail 포함 전체(`dist_pack._ecdf_sources(bin1_sources=)` /
    `build_distribution_compact(bin1_sources=)`). `bin1_scope` 가 없으면 캐시 키가
    종전과 **완전히 동일**해 기존 캐시가 그대로 유효하다.
  - `sources[]` 의 `temp_corner`(`"RT"|"CT"|"HT"`)·`temp_group` 과 `payload.temperature`
    는 그대로다(Distribution 소스 그룹 필터·Map legend·Temp 시트 렌더가 쓴다).
  - ⚠️ **그룹 해석이 실패하면 조용히 "전체 source" 로 계산된다.** 그룹은 세션 옵션에
    **이름**으로 저장되는데([validation.py](../web_report/validation.py)
    `webreport_temperature_groups`), 그 이름이 현재 manifest 의 source 이름과 안 맞으면
    `rt not in present` 로 그룹을 버리고 결국 `None` 이 된다 → [metrics.py](../web_report/metrics.py)
    `_temperature_context` 가 `yield_tables = tables`(전체)로 폴백해 **Yield·Issue Table 에
    CT/HT 가 섞여 계산**되고 RT만/CT만/HT만 필터도 무력해진다. 에러가 아니라 "숫자가 조용히
    틀린" 상태라 발견이 늦는다(2026-08-25 신고).
    → 서버는 `_log.warning` 을 남기고, 화면은 **`temp_corner` 부재**로 판정해 경고 배지를
    띄운다([distribution.js](../server/report/static/webreport/distribution.js)
    `tempGroupsBroken`/`tempWarnHtml` — Yield·Issue Table·Distribution 툴바 공용).
    **payload 에 경고 키를 넣지 말 것** — 스키마 bump = 전 세션 콜드 폭풍이라 일부러
    기존 필드만으로 판정한다. 회귀 고정: [tests/test_temp_warn_js.py](../tests/test_temp_warn_js.py)
    (프런트) + [tests/test_temperature_payload.py](../tests/test_temperature_payload.py)
    `test_broken_groups_fall_back_and_are_detectable`(서버 계약).

- **Yield STEP 분리 (2026-07-14 분모 전체 기준으로 통일 / 2026-07-21 STEP 요약만 누적 차감)**: Yield 탭은 STEP(P1/P2/P3)별로
  표를 나눈다. STEP 은 각 fail die 의 `FAILTNO → (TNO 매칭) item → item 의 STEP 메타행
  (raw 4번째 행)` 으로 정한다 (`item_meta`). 각 STEP 표의 bin portion 분모는 **항상 전체
  rawdata die 수**(`build_yield_rows` 가 이미 계산한 total 기준 값을 그대로 사용, 재계산 없음)
  — `build_yield_step_groups`(payload `yield_step_groups`) 는 원본 yield_rows 를 변형하지 않고
  STEP 별 그룹핑만 한다. 따라서 같은 fail 항목이 Yield 탭·Issue Table·Summary 에서 모두 동일
  % (pass% + 모든 STEP fail% 합 = 100%). 반면 상단 요약 박스의 STEP 요약
  (`yield_summary.by_step`)과 각 STEP 표 최상단 Pass 행은 **분모 고정 + 분자 누적 차감** —
  `yield_step_summary` 에서 `entered = 전체 die`(전 STEP 동일, 불변),
  `step_yield% = (전체 − Σ 그 STEP 까지의 fail)/전체`. 예) 1000 die, P1 100 / P2 50 / P3 10
  fail → 90% / 85% / 84%. `fail` 은 그 STEP **자체** fail 로 두고 누적은 `cum_fail` 로 병기해
  `survivor + cum_fail = entered` 가 pooled·소스별 양쪽에서 성립한다(요약 박스 "Pass / In" +
  "Fail (step / cum)" 열). **STEP 이 1종뿐이면 이 STEP 요약 박스 자체를 그리지 않는다**
  (`sheets.js yieldOverviewHtml` — 전체 수율 카드와 같은 값을 반복해 헷갈린다는 사용자 요청
  2026-08-06). 서버 payload(`by_step`)는 그대로다 — 표시만 생략.
  빈 STEP(`""`)은 정렬상 맨 뒤라 누적의 마지막 항에 포함된다.
  단, 세션 전체 STEP 메타에 비어있지 않은 STEP 이 **1종뿐**이면 빈 STEP fail 행을 그 STEP 으로
  흡수한다(`yield_tab._sole_step`, 2026-07-29 사용자 확정) — 화면에 "(기타)" 섹션이 생기지 않는다.
  판정 기준은 fail 행에 등장한 STEP 이 아니라 **전체 item 메타**(`table.step`)라 Map Analysis 의
  단일-STEP 판정과 어긋나지 않으며, STEP 2종 이상이면 어느 STEP 인지 알 수 없으므로 종전대로
  빈 STEP 을 유지한다. FAILTNO 가 **어느 item TNO 와도 매칭되지 않는** 경우는 이와 별개로 그
  fail die 가 Yield 표에 잡히지 않는다(`fail_counts_by_source`, 기존 동작 — 진단은
  `web_report/diag_yield_step.py` 의 UNMATCHED).
  **개별 bin fail 행의 % 는 이 누적과 무관하게 (그 bin fail / 전체) 유지**하며, Issue Table 도
  현행 유지다(맨 위 전체 Pass 행이 이미 최종 누적값과 같아 STEP Pass 행을 추가하지 않는다).
  **Issue Table·Summary·fail_bin_ranking 도 동일한 전체(total) 기준 값(`build_yield_rows`)**
  — Issue Table 은 merge 유지(STEP 열 포함, fail 비중 내림차순이라 P1/P3 가 교차 등장).
  프런트 원형 파이는 제거. `yield_bin_groups`(전체 기준 merge 그룹)는 Excel 내보내기용으로 유지.
- **Yield 분모 = Gross Die, 소스별 판정 (2026-07-23 도입 / 2026-07-28 소스별로 확장)**:
  위의 "전체 die"(분모)는 기본이 **제품 기준정보 `report_session.gross_die`**(product_info.db
  lookup 값)이며, **source 마다 따로** 정한다 (`yield_tab.resolve_source_basis` 가 정본,
  `source_totals` 는 그 `total` 만 뽑는다). 판정 규칙(사용자 확정):
  1. 분모는 Gross Die 가 기준이다.
  2. **수율은 100% 를 넘을 수 없다** — 넘으면 분모가 잘못된 것이다.
  3. 그 source 의 Gross Die < 그 source 의 test die → test die 분모 (2번의 구현, **강제**:
     사용자가 Gross 를 골라도 test 로 내린다 = `forced`).
  4. test die 가 Gross Die 보다 **100개 이상**(`GROSS_SHORTFALL_LIMIT`) 적으면 test die 분모
     (**기본값**일 뿐이라 사용자가 Gross 를 명시하면 존중한다 — 100% 를 넘지 않으므로).

  **분자(pass/fail die 수)는 언제나 실측**이라 Gross Die 기준에선 `pass + fail < total`
  (미측정 die) 일 수 있고, 그래서 `yield_summary.tested`(실측 die 수)와 payload
  `yield_basis = {basis:"gross"|"test"|"mixed", mode, gross_die, by_source[]}` 를 병기한다.
  요약 박스 배지 + **소스별 표의 "분모" 열**(`sheets.js yieldBasisBadgeHtml` /
  `yieldOverviewHtml`)과 Summary 탭 소스별 Yield 툴팁(`map_select.js`)이 그 값을 그대로
  보여준다 — 소스마다 분모가 다를 수 있으므로 화면에서 분모를 감추지 않는다.
  세션별 선택은 Honey **Rawdata 허브 [Yield 계산] 탭**(소스별 자동/Gross/Test) →
  `POST .../web_report/preprocess` 의 `yield_basis`
  (`{"mode":"auto|test","sources":{"<source>":"gross|test"}}`, 구 클라의 문자열도 수용) →
  세션 편집 DB (`edits.KIND_YIELD_BASIS`, item_key `basis` = 전역 모드 /
  `src\x1f<source>` = 소스별 override). 구 값 `'gross'` 는 읽을 때 `auto` 로 승격한다
  (auto = Gross Die 기준 + 위 예외 회피라 구 기본값의 의도를 포함).
  허브가 실시간 수율을 그리는 수치(pass/tested/gross)는 `GET .../web_report/yield_basis`
  (`service.get_yield_basis`) 1회로 받고, 체크를 바꿀 때는 **왕복 없이** 클라가 다시 계산한다.
  **preprocess spec 과 분리한 이유**: preprocess digest 가 붙으면 Distribution pack(정렬 전가)
  경로가 폴백으로 떨어지는데 수율 분모는 ECDF 와 무관하다. rev 증가로 REPORT//full 캐시만
  무효화된다(payload 구조가 바뀌므로 `REPORT_SCHEMA_VERSION` 도 함께 올렸다 — v19).
  회귀 고정: [tests/test_yield_basis_auto.py](../tests/test_yield_basis_auto.py)(판정 규칙·경계) +
  [tests/test_yield_gross_die.py](../tests/test_yield_gross_die.py)(계산) +
  [tests/test_yield_basis_session.py](../tests/test_yield_basis_session.py)(저장·재오픈·폴백·소스별).
- **CPK 임계값·기준 통일 (2026-07-23)**: `CPK_THRESHOLD = 1.33` ([cpk.py](../web_report/tabs/cpk.py)).
  subject 당 **worst-case(최저) cpk** 로 이슈를 판단한다(`worst_cpk_by_subject`).
  **모든 CPK 통계는 Bin1(양품, BIN==1) die 기준 하나**다 — `build_cpk_rows` 의 base 필드
  (`n/min/median/max/average/stdev/cp/cpl/cpu/cpk`)가 곧 Bin1 값이고 `*_bin1`/`*_limited`
  병기는 없앴다(구 3종 기준: 전체 die / Bin1 / 규격내). 이 값을 CPK 탭·**Issue Table CPK
  섹션**(1.33 미만 선정 + 표시값)·`distribution_index` 의 cpk/status·Excel(웹 Excel Down,
  Honey Excel Download) 이 그대로 쓴다 — 리포트 어디서나 같은 항목의 CPK 가 같은 값이다.
  CPK 탭의 기준 토글("Data 구분")은 제거했다(UX 간편화). Issue Table CPK 섹션 미니 분포도
  같은 기준을 따라 **Bin1 ECDF**(`data-bin1` → `distBin1Cache`, 갤러리 "Bin1 only" 와 같은
  배치 변형)로 그린다 — Honey Excel Download 는 그 섹션 항목만
  `distribution_batch?bin1=1` 로 따로 받아 같은 그림을 만든다(`fetch_distribution_bin1`,
  실패 시 전체 기준 폴백). 서버 bin1 ECDF 는 양품 **그리고** 규격내라 cpk 통계(규격 클리핑
  없음)와 표본이 완전히 같지는 않다.
  회귀 고정: [tests/test_cpk_bin1_basis.py](../tests/test_cpk_bin1_basis.py).
- **Temperature CT/HT 의 CPK 기준 (2026-08-10 사용자 요청)** — 위 "Bin1 기준 하나"의
  **유일한 예외**다. CT/HT 는 "**RT 에서 Bin1 이던 die**" 를 "**RT limit**" 으로 계산한다:
  자기 BIN 으로 거르지 않고(CT/HT 프레임은 업로드 전 정리로 이미 RT pass 좌표만 남아
  있어 **프레임 전 행**이 곧 그 모집단), lolim/hilim 만 그 그룹 RT 것으로 바꾼다(정리
  단계가 CT/HT 자신의 limit 메타행은 화면 표시용으로 보존하므로 여기서 갈아끼워야 한다).
  자기 BIN 으로 거르면 RT limit 재판정까지 통과한 die 만 남아 **저온/고온에서 규격을
  벗어난 분포가 통계에서 빠져 CPK 가 실제보다 좋게** 나온다. 대상 선정은
  `cpk.temperature_reference_tables` 한 곳이고, `build_cpk_rows(tables, items,
  temperature_groups)` 의 3번째 인자를 생략하면 종전과 완전히 동일하다(Compare 의 pooled
  계산·Honey 빠른 수정 미리보기가 그 경로). 행의 `lower_limit`/`upper_limit` 도 RT 값으로
  나간다 — 계산에 쓴 규격과 화면 규격이 다르면 CPK 탭의 한계값 역산(avg ± 3·Cpk·stdev)이
  맞지 않는다. 값이 바뀌므로 `REPORT_SCHEMA_VERSION` v29(**서버 재시작 필요**).
  회귀 고정: [tests/test_cpk_temperature_basis.py](../tests/test_cpk_temperature_basis.py).
- **Source 선택 UI + TOTAL 행 (2026-08-27 사용자 요청, v42)** — CPK 탭 툴바의 Source 는
  **다중 선택 드롭다운**이다(`cpkSourceMenuHtml`, 룩은 `.issue-menu` 팝오버 재사용).
  종전 토글 칩 바(2026-08-25)는 source 가 많으면 툴바 아래 한 줄을 통째로 먹고 이름이
  길면 표를 밀어냈다. 그 이전(2026-07-14)은 단일 선택 `<select>` 였다 — 지금은 둘의 합집합.
  - **TOTAL = 전 source 의 rawdata 를 하나의 source 로 통합**한 행이다. CPK 값 하나만
    따로 내는 게 아니라 die 를 세로로 이어붙여 `build_cpk_rows` 와 **같은 계산**
    (`_stats_batch` 재사용)을 돌리므로 source 별 행과 **같은 15개 컬럼**을 채운다
    (`n/min/median/max/average/stdev/cp/cpl/cpu/cpk`). 가중평균 합성이 아니라 실제
    병합이라 `median`·`min`·`max` 처럼 합성 불가능한 값도 정확하다 — 그래서 프런트가
    만들어낼 수 없고 서버 계산(`build_cpk_total_rows`)이어야 한다.
  - **`sheets["CPK Total"]` 별도 시트**이고 `sheets["CPK"]` 에는 넣지 않는다. 섞으면
    `worst_cpk_by_subject` 를 거쳐 **Issue Table CPK 섹션 목록**·`distribution_index.cpk`·
    Excel·public API 가 함께 바뀐다(규칙 13). `TAB_REGISTRY` 밖이라 탭도 안 생긴다 —
    `metrics.py` 가 직접 주입한다(`sheets["Issue Table Compare"]` 와 같은 선례).
  - **화면 규약**: 기본은 TOTAL 미선택(종전 동작 그대로). 고르면 그 항목의 **첫 행**으로
    끼어 들어가고 이어지는 source 행은 subject/limit 이 비워진다(첫 행의 limit 이 곧
    TOTAL 계산에 쓴 규격이라 Limit 역산과 일관). **CPK 임계 필터는 면제**된다 — cpk 가
    좋아서 걸러진 항목도 통합 통계로 확인할 수 있어야 하기 때문. 나머지 보조 필터
    (Unit CODE·동일Limit·검색어)는 동일 적용.
  - **모드 제한**: Temperature 는 만들지 않는다(RT/CT/HT 는 조건이 다른 3집단이라 합치면
    온도 스윙 폭이 σ 에 들어가 **그럴듯한 오답**이 된다). source 1개도 빈 배열.
    계산 실패는 격리해 빈 배열로 두고 리포트는 계속 만든다 — TOTAL 하나 때문에 세션이
    통째로 안 열리면 안 된다.
  - limit·units 는 항목이 **처음 등장한 source** 기준(`setdefault`, `_pool_tables` 규약).
    source 마다 규격이 다르면 TOTAL 의 cp/cpk 는 그 대표 규격 기준임에 주의.
  - **웹 Excel Down 은 TOTAL 을 포함하지 않는다** — `sheets["CPK"]` 전량이라는 Honey 클라
    (`client/excel_download/_sheets.py write_cpk_sheet`) 파리티를 지킨다.
  - 대형 세션 RAM 방어로 item 컬럼을 `_TOTAL_COL_CHUNK`(256)씩 잘라 merge 한다 — 통째로
    `pd.concat` 하면 피크가 3~6GB 로 뛰어 컴퓨트 워커가 OOM 된다.
  회귀 고정: [tests/test_cpk_total.py](../tests/test_cpk_total.py)(서버 10건) ·
  [tests/test_cpk_total_js.py](../tests/test_cpk_total_js.py)(프런트 12건).
- **Bin 그룹 펼침 = 집계 헤더행 분리 (2026-08-25)**: Bin 묶음을 쓰는 표 전부
  (Yield 탭 · Issue Table Yield 섹션 · Issue Table Temp · Yield 탭 Temp Corner)에서,
  같은 Bin 에 항목이 **여럿일 때만** 접힘/펼침의 첫 줄이 달라진다.

  | 상태 | 첫 줄 | 그 아래 |
  |------|-------|---------|
  | 접힘 (**종전과 동일**) | 대표행 — 숫자는 Bin 합계, 이름은 most-fail 항목 | 없음 |
  | 펼침 (**변경**) | 집계 헤더행 `BIN 15    (3 items)`, TNO `-`, 숫자는 Bin 합계 | 그 Bin 의 **모든** TNO 행(각자 실제 값) |

  종전에는 펼칠 때 첫 상세행(= most-fail 항목)을 "대표행과 중복"이라며 지웠는데, 대표행의
  숫자는 그 항목 값이 아니라 **Bin 합계**여서 그 항목의 실제 fail 수가 화면 어디에도 없었다
  (TEST1 이 2개 fail 인데 `0.5 / 5` 로 표시 — 사용자 신고). 이제 그 행을 되살리고 합계는
  정체가 분명한 집계 헤더행으로 옮긴다. rep 와 agg 는 **상호 배타**로 표시된다.
  - **규약 정본은 [web_report/yield_agg.py](../web_report/yield_agg.py)** (`bin_agg_label` /
    `build_bin_agg_row` / `expand_bin_group` / `insert_bin_agg_rows`). 소비자가 4곳
    (웹 [sheets.js](../server/report/static/webreport/sheets.js) `insertBinAggRows` +
    [yield_issue.js](../server/report/static/webreport/yield_issue.js) `yieldBinAggRow`,
    Honey Excel [_extra.py](../client/excel_download/_extra.py)·[_sheets.py](../client/excel_download/_sheets.py)·[_xlsx.py](../client/excel_download/_xlsx.py))
    이라 **라벨 문자열을 각자 만들지 말 것** — JS 사본 1벌은 테스트가 파이썬 정본과 글자
    단위로 대조한다([tests/test_yield_bin_agg_js.py](../tests/test_yield_bin_agg_js.py)).
    `tabs/` 밖에 둔 이유는 Honey 클라가 pandas·전 탭 레지스트리를 끌어오지 않게 하기 위함.
  - **서버 payload 무변경** — 집계행은 `rep` 에서 표시 직전에 파생할 뿐이라
    `REPORT_SCHEMA_VERSION` 을 올리지 않는다(§5-14 콜드 폭풍 회피). `yield_bin_groups`·
    `yield_step_groups`·`sheets["Yield"]`·public_api 응답 전부 종전 그대로다.
  - **항목이 1개뿐인 Bin 은 집계행도 ▼ 토글도 만들지 않는다**(사용자 확정) — 값이 그 항목
    값과 같아 같은 줄이 두 번 보일 뿐이다. 표시는 종전과 100% 동일.
  - **Issue Table 집계행은 Map/Distribution 이 빈 칸**이다(미니셀 112px 이 빠져 행 높이가
    숫자에 맞게 좁아진다 — 사용자 요청). 그림이 바로 아래 항목 행들과 같아 중복이기도 하다.
  - **저장 키**: 집계행은 comment 키가 **없다**(`issueRowKey` → `""`) — 라벨을 키로 쓰면
    기존 `Yield|<bin>|<item>` comment 가 고립된다(§5-12). 반대로 Status/숨김 키
    `Yield|<bin>` 은 **집계행에도 준다**(원래 bin 단위 키라 집계행이 자연스러운 주인이고,
    접힘 대표행과 상호 배타로 보이므로 한쪽만 보인다). 같은 키의 셀이 DOM 에 2개 생기므로
    comment blur·Status 변경은 [edit_mode.js](../server/report/static/webreport/edit_mode.js)
    `mirrorCommentCell` / Status 낙관 갱신이 **같은 키 전부**를 맞춘다.
  - ⚠ **`_grp` 을 쓰는 표가 둘인데 대표행의 성격이 달라 처리가 갈린다.** 판정은
    `rep.Item == 첫 상세행.Item` 하나 — 구조상 항상 참/거짓이 갈린다.

    | | 대표행 | 헤더행 수치 | 대표행 처리 |
    |---|---|---|---|
    | **Yield 섹션** (`yield_tab._bin_total_row`) | Bin **합계**(이름만 most-fail 항목) | rep 승계 | 헤더행이 **대신**(펼침에서 숨김) — 그 항목의 진짜 행은 첫 상세행 |
    | **Issue Table Temp** (`temp_fail._group_by_bin`) | avg 최대 **항목 행 자체** | **빈칸** | 상세행으로 **복제**(안 하면 펼침에서 그 항목이 사라진다) |

    Temp 헤더행을 비우는 이유는 두 가지다 — 항목끼리 die 가 겹쳐 **합산이 틀린 값**이 되고
    (`temp_fail` 모듈 docstring), bin 단위 저장 키가 없다(키는 `TEMP|<item>`). 그래서
    Temp 헤더행에는 Status·comment·체크박스·Item_detail 링크를 **일절 달지 않는다**
    (`issueHideStatusKey` 가 `_agg && section !== "Yield"` 를 `""` 로 돌린다).
    복제 상세행은 `Category` 를 비운다 — 값이 남으면 `emitRows` 가 섹션 divider 로 보고
    그 행을 건너뛴다.
  - **Yield 탭 하단 Temp Corner 요약표**도 같은 표라 함께 적용된다(`renderSheetTable` 이
    `kind:"yield"` + `_grp` 인 표에도 `insertBinAggRows` 를 태운다). 토글은 종전대로
    `.yield-toggle`.
  - ⚠ **Issue Table Compare 는 대상이 아니다** — `_grp`/`_detail` 자체를 만들지 않는
    평평한 표라 첫 분기에서 빠진다.
  - **검색 강제 펼침**(`.yield-searching`/`.issue-searching`)도 대표행을 감추고 집계행을
    띄운다(`tr.*-bin-rep.has-agg`) — 안 그러면 검색 결과에서 옛 혼동이 그대로 재발한다.
  - **Excel 3경로도 화면과 같은 구성**(사용자 확정): 웹 Yield Excel Down / 웹 Issue Table
    Excel / Honey 전체 Excel 모두 집계 헤더행 + 전 TNO 행. Honey Excel 의
    `write_issue_sheet` 은 상세 comment 를 대표행에 합치던 동작을 **집계행 그룹에서만**
    멈춘다(상세행이 각자 한 줄로 나가므로 합칠 이유가 없다).
  - 회귀 고정: [tests/test_yield_bin_agg.py](../tests/test_yield_bin_agg.py)(값·합 검산·원본
    불변) + [tests/test_yield_bin_agg_js.py](../tests/test_yield_bin_agg_js.py)(headless Edge
    로 실제 DOM — 접힘/펼침 행 구성·미니셀 빈 칸·저장 키·Temp 표 불변).
- **Issue Table comment 키**: `row_key` 규약 — Yield 행 `Yield|<bin>|<item>`,
  CPK 데이터 행 `CPK|<item>`, TEMP 행 `TEMP|<item>`(Temperature 전용), ETC 행 `ETC|<item>`.
  comment 컬럼은 `COMMENT_COLS = ["PTE comment", "개발 comment"]`. 값은 세션 편집 DB 에서
  채운다. **파서 사본이 4곳**이라 접두를 늘리면 전부 같이 고쳐야 한다:
  [issue_table.py](../web_report/tabs/issue_table.py) 생성 · [sheets.js](../server/report/static/webreport/sheets.js)
  `issueRowKey`/`issueHideStatusKey` · [eval_export.py](../web_report/eval_export.py) `_parse_row_key`
  · [chatbot/rowkey.py](../server/chatbot/rowkey.py) (+ service.py 의 숨김/Status 허용 접두 2곳).
- **Issue Table 행 숨김/Status 키** (2026-07-16): 이슈 단위 키 — Yield 는 **bin 단위**
  `Yield|<bin>`(대표행+상세행 일괄), CPK/TEMP/ETC 는 `CPK|<item>`/`TEMP|<item>`/`ETC|<item>`
  (sheets.js `issueHideStatusKey` ↔ issue_table.py 동기 필수). 숨김(kind `issue_hidden`,
  Yield/CPK 만 — ETC 는 기존 etc remove)은 행별 복원 없이 툴바 "삭제 전체 초기화"로만
  일괄 복원.
  ⚠️ **삭제 후 세션을 재로드하지 않는다** (2026-08-14, perf_guard `R12`). 저장은 그대로
  편집 DB 에 하되, 프런트는 `load(false)` 대신 `removeIssueRowsLocal` 로 화면에서만 지우고
  그 패널만 다시 그린다 — 편집은 rev 를 올려 report 캐시 키를 바꾸므로 그 재로드가 **행
  하나 지울 때마다 리포트 전체 콜드 빌드**를 유발했다(무한 로딩 사건의 방아쇠 →
  [12](12_web_report_cache.md)). 로컬 삭제 규칙은 백엔드와 같아야 한다: Yield 는 그 bin 의
  대표행+상세행 전부, CPK/TEMP/ETC 는 item 단위, 그리고 **지운 행이 들고 있던 Category
  라벨은 남는 첫 행으로 옮긴다**(라벨을 잃으면 뒤 행들의 섹션 상속이 끊겨 표가 어긋난다).
  일괄 삭제는 `{"action":"hide","keys":[...]}` 배치 1회로 보낸다(rev +1) — 단건을 N회
  보내면 콜드 유발 지점이 N개가 된다. ETC **추가**와 '삭제 전체 초기화'는 서버가 행을
  만들어/되살려야 하므로 재로드를 유지한다. Status(kind `issue_status`)는 Open/Close 드랍다운(편집모드 전용, 기본 Open —
  **"Close" 만 저장, 부재=Open**). Summary 탭 Issue Status 카드가 카테고리별 Open/Close
  를 집계한다(`issueStatusCounts`, map_select.js).
- **Issue Table Signature 컬럼** (2026-08-11): AI Comment **왼쪽** 열. 값은 엔진이 발화한
  룰 전체(제안, 흐리게) 또는 ENGR 이 확정한 목록(진하게), 발화가 없으면 `미분류`.
  ai_comment 옵션 세션에만 생기고(`ai_comments` 와 같은 조건), 행 보조 필드
  `_sig`/`_sigrev` 는 화면 컬럼이 아니다(sheets.js `orderColumns` 제외 — 컬럼 자체도
  이름에 "comment" 가 없어 issue 분기에서 **Status 뒤·comment 앞**에 명시 배치한다).
  편집모드는 드랍다운 N개 + `+`(가로 추가) + `확정`(엔진값과 같아도 저장 — 동의 사례를
  남겨야 통계가 안 치우친다), 변경 즉시 저장(kind `issue_signature`, row_key 는
  comment 와 동일, value=JSON 배열로 순서 보존). **Issue Table Temp 에는 만들지 않는다**
  (CT/HT 는 RT limit 재판정이라 저장 FAILTNO 기준 엔진 평가와 어긋난다 — AI Comment 도
  같은 이유로 뺐다). eval DB 라벨 적재·Unknown 모음은 [13 §6-3](13_eval_analyzer_integration.md).
- **Issue Table 선택 모드 = 일괄 삭제 + Status 일괄** (2026-07-28): 툴바
  "☑ Issue Item 추가/변경/삭제"(구 "☑ 선택 모드" → 2026-08-10 개명, 그 전엔 "🗑 삭제 모드".
  id/CSS 클래스는 `issueDelMode`/`.issue-del-mode` 그대로)를 켜면 행 체크박스가
  뜬다. 체크박스가 작아 **Step 셀 아무 곳이나 클릭해도 토글**되고(`td.issue-sel-cell`,
  Step 셀 안 ▼ 는 클릭 위임에서 먼저 처리), 선택 행은 `tr.issue-row-sel` 로 강조한다.
  선택 대상 동작: 전체 선택/선택 해제 · 선택 Open/선택 Close · 선택 삭제 · 삭제 전체 초기화.
  Status 전체 일괄(All Open/All Close)은 선택과 무관해 편집모드 툴바에 상시 노출된다.
  Status 일괄은 `/issue_table/status` 에 `items:[{key,value},…]` 로 보내
  (`service.update_issue_status_bulk`) 편집 DB write·rev 증가를 1회로 묶고, 프런트는
  재렌더 없이 드랍다운·셀 색·`DATA.issue_table_text` 만 낙관 갱신한다(단건 경로와 동일).
  Status 표시는 **셀 전체 배경색**이다(2026-08-13 사용자 요청 — 종전 신호등 점 폐지):
  Open 파스텔 주황 / Close 파스텔 초록. Issue Table 과 Issue Table Temp 가 같은 렌더 경로
  ([sheets.js](../server/report/static/webreport/sheets.js) `renderSheetTable`)라 함께 바뀐다.
  배경 CSS 에는 `!important` 가 필요하다 — zebra·hover 규칙이 특이도로 이기기 때문.
- **Issue Table Map/Distribution 미니셀 클릭 이동** (2026-07-21): 미니셀 그림 자체가 링크다
  ([edit_mode.js](../server/report/static/webreport/edit_mode.js) `.content` 위임).
  Distribution 셀 → 그 Item 의 Item_detail(`openItemDetail`). Map 셀 → Map Analysis 탭
  (`openMapAnalysisForBin` / `openMapAnalysisForItem`, wafer_charts.js) — 탭을 dirty 로
  되돌린 뒤 탭 버튼을 click 해 새 선택 상태로 재렌더한다. Yield/ETC 행(`data-bin`)은
  Bin Map 에서 그 Bin 을 범례 선택 상태로 시작하고(1회성 `mapBinPreselect`), CPK 행
  (`data-subject`)은 STDF Map 에서 그 Item 을 선택 상태로 연다. 우상단 ⤢(전체 소스
  펼치기)는 클릭 위임 순서상 먼저 처리돼 기존 팝오버 동작을 유지한다.
- **Issue Table CPK 행 Map 열 = STDF Map 썸네일** (2026-07-21): CPK 행엔 Bin 이 없으므로
  Bin 미니맵 대신 그 Item 의 측정값 10분위 맵을 그린다(`renderMiniStdfCell` →
  `stdfThumbMap`/`stdfDrawThumb`, [stdf_map.js](../server/report/static/webreport/stdf_map.js)).
  Map Analysis STDF Map 과 같은 색·분위 기준이되 렌더는 갤러리와 같은 canvas
  (`drawWaferThumb`) — Plotly 미사용. 소스가 여럿이면 첫 소스(값 있는)를 그리고 ⤢ 로 전
  소스를 나열한다(DUT 모드는 Bin Map 과 동일하게 병합해 1장). 데이터는 항목별
  `GET .../web_report/scatter/<subject>` 라 셀당 요청 1건이며, 동시 요청은
  `STDF_MINI_MAX_INFLIGHT`(2)로 제한하고 상한에 걸린 셀은 요청 완료 시 재큐잉한다.
- **Issue Table/Yield 컬럼 표시 규칙** (2026-07-21, [sheets.js](../server/report/static/webreport/sheets.js)):
  - **좌측 틀고정 재실측**: 고정열(Step/Bin/**Item**/Map/Distribution — **TNO 는 2026-08-10
    부터 고정 제외**, 가로 스크롤하면 Step/Bin 뒤로 밀려 사라진다) left 오프셋은 렌더
    시점 실측값(`--issue-colN-left`)이라, TNO 상세행을 펼쳐 Item 열이 넓어지면 stale 이 되어
    Map/Distribution 이 Item 위로 겹친다. `toggleIssueGroup`/`setAllIssueGroups` 는
    `afterIssueRowsToggled()` 로 반드시 재실측한다(Yield 는 `setYieldGroup` 에서 동일).
    **행 표시/폭을 바꾸는 새 동작을 추가하면 여기에 합류시킬 것.**
    - ⚠ **숨김 상태 실측은 전부 0** 이다. 백그라운드 프리렌더가 Issue Table 을
      `display:none` 인 채로 그리므로 그때 실측하면 아무 값도 안 심어지고 CSS fallback
      (Item=124px 가정)이 남아 Map/Distribution 이 Item 을 덮는다(2026-08-10 신고 원인).
      탭 활성화 시점([tabs_topbar.js](../server/report/static/webreport/tabs_topbar.js))에서
      `syncIssueStickyOffsets` 를 반드시 다시 부른다.
    - 고정열 셀의 강조 배경은 **불투명**이어야 한다 — 셀 선택(`.cell-sel`)의 기본 반투명
      배경을 그대로 쓰면 고정열 밑을 지나가는 우측 데이터가 비쳐 글자가 겹친다.
  - **TNO 전체 펼치기**: 툴바 버튼이 아니라 **Yield 섹션 헤더 Step 열 아래 작은 ▼**
    (`.issue-toggle-all`, 2026-08-10). 핸들러는 종전 `data-issue-act="toggle-all"` 그대로.
  - **Excel식 셀 조작**: 클릭=1셀 / 드래그=사각 범위 선택(`.cell-sel`) → `Ctrl+C` 로 TSV 복사,
    **`F2` 로 선택 셀 편집 진입**(2026-08-14). F2 는 편집 로직을 새로 갖지 않고 anchor 셀에
    **합성 `dblclick` 을 보내** edit_mode.js 의 진입 핸들러를 그대로 태운다(`cellSelEditAnchor`)
    — 진입 조건(`MODE==="edit"` 가드·`data-raw` 원문 복원)이 두 벌로 갈라지면 안 되기 때문.
  - **source 헤더 축약**: source 컬럼이 `SRC_ABBREV_MIN`(8) 이상이면 공통 접두/접미를 떼고
    다른 부분만 표시한다(첫 컬럼만 전체 이름, `sourceHeaderLabels`). 전체 이름은 th `title`
    에 남는다. 이때 `{src}_yield/_count` 열너비도 숫자 크기로 좁힌다(`colWidth(..., narrowSrc)`).
  - **comment 열 고정폭**: AI/PTE/개발팀 comment 셀·헤더에 `st-comment` 클래스를 달고 CSS 로
    `min/max-width: 330px` 를 못박는다 — source 가 늘어도 폭이 흔들리지 않고 대신 가로
    스크롤이 길어진다(사용자 요청).
  - **컬럼 폭 드래그**: Issue Table 은 `bindIssueColResize`(미니차트 재렌더 포함), Yield 는
    `buildSheetTableHead(cols, {resize:true})` + 공용 `bindSheetColResize`.
  - **"개발 comment" 표기**: 화면·Excel 내보내기 헤더만 `COLUMN_DISPLAY_ALIAS` 로
    "개발팀 Comment" 로 보인다. **저장 키는 `"개발 comment"` 그대로** — 편집 DB
    (`issue_comments[row_key]`)·[eval_export.py](../web_report/eval_export.py) `_COMMENT_PREFIX`·
    클라 [excel_download/_sheets.py](../client/excel_download/_sheets.py) 가 이 키를 쓰므로
    바꾸면 기존 세션 comment 가 유실된다.
- **Issue Table Excel 다운로드**: 툴바 "Excel 다운로드" 버튼(`exportIssueExcel`,
  [yield_issue.js](../server/report/static/webreport/yield_issue.js)) — Trim 탭과 같은
  vendored exceljs(`loadExcelJS`)로 브라우저에서 xlsx 1시트 생성(서버 무관여). 화면과 동일
  컬럼 순서(`orderColumns`)에서 미니차트 열(Map/Distribution)만 빼고 섹션을 Category
  컬럼으로 되살리며, Yield 상세(TNO) 행은 접힘과 무관하게 전부 포함.
- **Honey Excel Download (전체본) — XlsxWriter 단일 경로 (2026-08-18)**: 진입점은 종전대로
  [client/excel_download/__init__.py](../client/excel_download/__init__.py)
  `run_excel_download` 하나이고, **서버는 무변경**(기존 조회 GET 만 쓴다 — 계산·렌더·기입은
  전부 클라이언트).
  - **엔진 선택 UI 는 없다.** 구 "새 방식으로 만들기" 체크박스를 없애고 항상
    **XlsxWriter**([_xlsx.py](../client/excel_download/_xlsx.py))로 만든다 — Excel 설치·COM
    없이 xlsx 를 직접 생성. Honey 다이얼로그에 남은 옵션은 **Bin1 기준 산포** 하나뿐.
  - 폴백 엔진 **Excel COM**([_sheets.py](../client/excel_download/_sheets.py), 동결) —
    기본 엔진이 실패하면 **이미 받은 데이터·렌더된 PNG 를 재사용해 자동 재시도**하므로
    파일은 어떤 경우에도 만들어진다(사용자가 고를 수는 없고, 발동 시 완료 안내에만 표기).
    두 엔진은 `_fill_workbook` 한 벌을 공유하고, COM 쪽은 `_ComBook` 어댑터로 감싼다
    (`_sheets.py` 자체는 수정하지 않는다).
  - **XlsxWriter 엔진에만 있는 web_report 파리티**: Summary 의 Issue Status·Engr Comment
    카드 / Yield 상단 요약 3표 / fail 빨강 그라데이션(웹 `--yw` 와 같은 hsl 식) /
    Status 셀 Open·Close 색 / Compare 시트 / 차트 주석(chart_note) PNG 오버레이.
    값 계산은 전부 순수 빌더 [_extra.py](../client/excel_download/_extra.py) 에 있고
    self-run 테스트로 검증한다.
    (구 **전처리 안내·Download Status** 시트는 2026-08-18 사용자 요청으로 생성 중단 —
    경고는 완료 안내창과 Honey 실행 로그로만 보고한다.)
  - 시트 실패는 그 시트에만 안내 문구를 남기고 나머지는 정상 생성하며, 무엇이 빠졌는지는
    완료 안내창 경고 목록에 모인다. 시간 예산(150s 이미지 skip → 165s 저장)으로 3분 SLA.
  - **Issue Table 썸네일**(Map·Distribution)은 가로·세로 각 2배(5.2 x 2.3 in — 2026-08-18).
    열 너비·행 높이가 이 상수에서 유도되므로 칸도 함께 커진다
    ([_charts.py](../client/excel_download/_charts.py) `ISSUE_CELL_W_IN`/`ISSUE_CELL_H_IN`).
  - **Map Analysis 격자**는 좌표마다 벡터 선으로 긋는다
    ([_map.py](../client/excel_download/_map.py) `_draw_cell_grid`, die 수에 따라 DPI 상향).
    블록 이미지에 1px 격자를 새기면 축 크기로 리샘플될 때 선이 좌표에 따라 통째로 사라진다
    (실측: 100x100 die 에서 99개 중 56개만 남았다). 미니셀은 웹과 같은 픽셀 방식 유지.
  - 산포 화질: 차트 렌더 DPI 96 → **144**(웹 카드의 1.5배 선명도). 물리 크기(pt)는
    그대로라 선명도·용량만 바뀐다. 다운샘플은 여전히 없다(규칙 #5) — chip 강조도 종전대로.
    실측(2004항목×7source×1000die): 96→29.0s/46MB · 144→29.2s/83MB · 192→32.6s/120MB
    — 소요는 평평하고 용량만 갈리므로 선명도와 용량의 절충점으로 144 를 골랐다
    ([_charts.py](../client/excel_download/_charts.py) `DPI` 한 줄로 조정).
  - 검증: [tests/test_excel_extra_builders.py](../tests/test_excel_extra_builders.py)(값) ·
    [tests/test_excel_xlsxwriter.py](../tests/test_excel_xlsxwriter.py)(실제 xlsx 를 만들어
    stdlib zip/XML 로 시트·색·병합·이미지 검사) ·
    [tests/test_excel_com_fallback.py](../tests/test_excel_com_fallback.py)(Excel 있는 PC 전용) ·
    [tests/bench_excel_download.py](../tests/bench_excel_download.py)(3분 SLA).
- **Yield/CPK 탭 Excel Down**: 각 탭 툴바 우상단 "Excel Down" 버튼(`exportYieldExcel` /
  `exportCpkExcel`, [excel_export.js](../server/report/static/webreport/excel_export.js)) —
  같은 vendored exceljs 로 시트 1장 생성. 레이아웃·서식은 Honey 클라 Excel Download
  (`client/excel_download/_sheets.py` `write_yield_sheet`/`write_cpk_sheet`)와 동일하게 맞춘다
  (B3 헤더행·A1 배너·H1 세션링크·CPK `cpk<1.33` 노란 fill·열너비/행높이). 입력이 같은 /full
  payload 라 값 파리티는 자동 — Yield 는 Pass 행+`yield_bin_groups[].rep`(접힌 상태), CPK 는
  `sheets["CPK"]` 전량·원순서(화면 필터·기준 토글 무관, 전체 die 컬럼만).
- **Compare 계산 (2026-07-23 재정의, Before/After 그룹)**: source 2개 이상을 Before/After 두
  그룹으로 나눈다(배치·업로드 순서는 [10](10_web_report_pipeline.md) 분석 모드 표). 그룹은
  `webreport_options.compare` → `validation.webreport_compare_groups` → `build_compare_payload`
  로 흐르고, 옵션이 없으면 `after=[s0], before=[s1]` 로 폴백해 **기존 세션 화면이 바뀌지 않는다**.

  ⚠️ **화면은 2026-08-27 부터 `Issue Table Compare` 탭 하나다** — 구 최상위 `Compare` 탭
  (`#panel-compare` / `renderCompare`)은 제거됐고 그 서브탭이 그리로 흡수됐다. 아래
  "Issue Table Compare 탭" 절이 정본. 이 절의 payload 설명(`bin_matrix`·`goodlog`·
  `dist_shift`·`equivalence`)은 **계산 쪽이라 그대로 유효**하다.

  `Test Time 비교` 는 **자리만 있는 빈 화면**이다(2026-08-20) — 입력 계약(7-meta
  honeyform)에 시간 컬럼이 없고 STDF 는 서버가 파싱하지 않아 원천 데이터가 없다.
  **구 `산포 비교` 서브탭은 2026-08-20 제거**됐다 — 그 표는 Issue Table Compare 의
  ISSUE_TABLE 서브탭이다(`dist_shift` payload 계산·`_dist_focus` 판정은 그대로다).
  - **표의 Before/After 열 순서**(2026-08-20): Log 비교·산포 비교·Bin Yield 비교는
    **Before 가 왼쪽**이다(시간순으로 읽힌다). 서버 payload 키·`GOODLOG_HEADER` 는
    after 먼저 그대로이고 **프런트 표시 순서만** 바꾼 것이다. 예외는 공통성 Map —
    거기 색이 업로드 순서 인덱스(`COMPARE_SRC_PALETTE[i]`)에 묶여 있어 순서를 바꾸면
    Honey 배치 창·Distribution 탭과 색 의미가 어긋난다(`cmpOrderedSources` 를 쓰지 않는다).
  - **Compare 행 코멘트 (kind=compare_note, 2026-08-20)**: Log 비교의 Comment 열과
    동일 좌표 Bin 비교 표의 Comment 열(신설)에 더블클릭으로 직접 입력한다. 종전 Comment
    열은 서버가 항상 `""` 를 주는 **장식 컬럼**이었다(저장소 자체가 없었다).
    저장은 세션 편집 DB — `POST .../web_report/compare_notes`, 읽기는 `/full` extras 의
    `DATA.compare_notes`. compare 캐시(`compare_key`)는 edits_rev 를 안 쓰므로
    **코멘트를 적어도 compare 재계산이 일어나지 않는다**.
    **item_key 는 고정 규약이다**(바꾸면 기존 입력 유실 — CLAUDE.md §5-12):
    `gl:<after_item_name>` + U+001F + `<before_item_name>` (한쪽만 있는 행은 반대편이 빈 문자열) /
    `bm:<x>,<y>`. **행 인덱스를 쓰면 안 된다** — 필터·접기로 순서가 바뀌어 남의 행에 붙는다.
  - **Log 비교의 False 셀 강조**(2026-08-20): Compare 열이 False 면 그 값이 든
    Before/After 셀(Item·LoLim·HiLim)에 `.gl-mismatch` 빨강을 함께 칠한다. CSS 는
    `!important` 가 필수다 — zebra(`nth-child(even)`)·hover 규칙이 특이도에서 이긴다.
  - **Distribution 탭 "신규항목보기"**(2026-08-20): Compare 모드에서만 뜨는 필터 버튼으로,
    Before 에 없고 After 에만 있는 test item 만 남긴다. 판정은 서버
    `build_compare_payload` 의 `new_items`(= After 그룹 합집합 − Before 그룹 합집합)
    하나뿐이다 — goodlog(그룹 대표 2개)로 프런트에서 다시 세지 말 것(규칙 #13).
  - 산출물마다 **비교 대상이 다르다**: 공통성 Map·Bin Yield·Bin 불일치 좌표표는 **전 source**,
    goodlog 는 **그룹 대표 2개**(After 최상단 vs Before 최상단), 산포 비교(`dist_shift`)와
    동일성 검증은 **그룹 pool**(그룹 전체 die 를 합친 가상 테이블, `_pool_tables`).
    그룹이 1 source 씩이면 pool 이 그 테이블 자체라 **CPK 탭 값과 완전히 같다**(복사도 없음).
  - `bin_matrix`(구 `bin_transition` 대체): 모든 source 에 있는 공통 좌표 중 **BIN 이 전부
    같지는 않은 die 를 좌표 1행씩** 나열하고 컬럼을 Before/After 그룹으로 묶는다.
    `counts.pass_to_fail`/`fail_to_pass` 만 **그룹 대표 기준**이다(화면에도 그렇게 표기).
  - `common_map.dies[].bins` — die 마다 source 별 BIN 을 실어 **마우스오버로 확인**한다.
    hover 문자열은 `waferHeatmap` 의 `opts.labelOf` 훅으로 갈아끼운다(미지정이면 종전과 동일).
  - **Para Conversion**(2026-08-27 — `options.compare.para=True`, mode 는 계속 `Compare`):
    Before=Single Mass Data 1개, After=같은 웨이퍼를 DUT 로 펼친 `DUT<라벨>` N개다.
    분할은 **클라이언트가 업로드 전에** 한다([05](05_client_ui.md) `CompareArrangeDialog`) —
    서버는 이 source 들을 일반 Compare source 로 소비하므로 대부분의 탭에 분기가 없다.
    분기는 3곳뿐이다:
    ① `goodlog` 의 Value 가 **DUT 별 한 칸씩**(`rows[].after_values`, 순서는 `para_duts`)이고,
      값 기준이 공통 좌표/Bin1 reference die 가 아니라 **각 source 의 첫 데이터 행**이다
      (Single 쪽도 같은 기준). 기존 `after_value`/`gap` 은 첫 DUT 기준으로 계속 채워
      Excel 다운로드·구 렌더(15컬럼 고정)가 그대로 동작한다. 프런트는 `gl.para_duts` 로
      컬럼 수를 동적화하되 **`gl:` 코멘트 키는 불변**이다(규칙 #12).
    ② `common_map`·`bin_matrix` 는 **[All DUT 합본, Single] 2-source** 로 굽는다 —
      DUT source 끼리는 die 좌표가 서로소라 "전 source 공통 좌표"가 공집합이 되기 때문이다.
      프런트는 세션 source 목록이 아니라 `common_map.sources` 를 축으로 쓴다.
    ③ Map Analysis 는 DUT source 만 `All DUT` 한 장으로 접고 Single 은 그대로 둔다
      (`build_map_analysis_rows(para_after=…)` — 세션 전체를 접는 DUT 모드와 달리 **일부만**).
      호출부 3곳(payload 경량 메타·lazy 조회·`seed_map`)에 같은 값을 넘겨야 정준 JSON 이
      일치한다(규칙 #11). 캐시 분리는 전역 bump 가 아니라 `map_key` 의 조건부 `("para",)`
      마커가 담당한다 — 기존 세션 키는 바이트 불변(규칙 #14).
    나머지(`bin_delta`·`dist_shift`·`equivalence`·`new_items`·Yield/CPK/Distribution)는
    Single+DUT1~N 을 그냥 N+1개 source 로 본다.
  - **Log 비교(goodlog) 표는 항상 전 항목을 그린다** (2026-07-28). 종전에는 항목·limit 이
    완전히 같으면(`identical`) `rows=[]` 로 내려 '차이 없음' 안내만 띄웠는데, limit 이 안
    바뀌어도 **항목별 Gap %** 를 봐야 한다는 요구로 `build_goodlog` 이 identical 이어도 행을
    채운다. `identical` 은 안내용 플래그로만 남는다(payload 구조 변경 → `REPORT_SCHEMA_VERSION`
    20). Gap 수식은 Honey 원본 그대로 `(After−Before)/Before×100` 이고 화면 표시는 소수 2자리,
    `|Gap|≥10%` 면 빨강(`GL_GAP_LIMIT`, 셀 강조와 필터가 같은 상수를 쓴다).
    프런트 표시 필터 2종(독립 토글, 둘 다 켜면 AND — [compare.js](../server/report/static/webreport/compare.js)):
    `Item·Limit 차이만`(행 분류 ≠ normal = Item/LoLim/HiLim 비교가 False 이거나 항목 추가·제거) /
    `Gap ≥10% 만`. 필터가 걸리면 git-diff 식 '변화 없음' 접기를 풀고 평평한 목록으로 그리며,
    접기 전용인 '전체 펼치기' 버튼은 숨긴다(`glSyncExpandBtn`).
  - **산포 비교**(`build_dist_shift`, 2026-07-28 개편 — 구 "CPK 비교"): 공통 항목의 After/
    Before pool 통계(Avg·Stdev·Cpk) 병기 + **Before(b) 분모** 정규화 지표 6종.
    a=After, b=Before: `meanshift_sigma=|avg_a−avg_b|/σ_b` · `cpk_ratio_pct=cpk_a/cpk_b×100`
    (>100%=개선, `cpk_b≤0`/결측이면 None) · `stdev_delta_pct=(σ_a−σ_b)/σ_b×100`(양수=After
    산포 증가) · `median_shift=|med_a−med_b|/IQR_b` · `iqr_delta_pct` · `ks_d`(두 pool ECDF
    최대거리 0~1 — 평균·σ 로 못 잡는 분포 형태 차이). 반환은 dict
    `{after, before, thresholds, summary{total,focus}, rows[]}`.
    avg/stdev/cpk/n 은 pooled `build_cpk_rows` 재사용이고, **median/IQR/KS 만** 같은 Bin1
    pooled frame(`_bin1_frame` — `build_cpk_rows` 의 마스크·numeric 강제를 복제)에서 직접
    계산한다(모집단 일치를 테스트로 고정).
    **화면 컬럼은 4종**(MeanShift σ / Cpk% / Stdev증가율 / Median Shift) — `iqr_delta_pct`·
    `ks_d` 는 2026-07-28 사용자 요청으로 표에서 뺐다. **payload 에는 그대로 남는다**
    (median_shift 의 분모 IQR·p_stdev 의 정렬배열을 어차피 계산하므로 지우는 이득이 없고,
    되살릴 때 서버를 안 건드려도 된다).
    ⚠ `ks_d` 는 화면에만 없을 뿐 **2026-09-01 부터 focus 판정에 쓰인다**(룰 v3 ⑥) — 표시
    대상이 아니라고 payload 에서 빼면 형태 차이 검출이 통째로 죽는다.
    ⚠ **화면은 2026-08-20 부터 Issue Table Compare 탭**이다(아래) — 구 산포 비교 서브탭의
    페이지 넘김(`CMP_DIST_PAGE_SIZE`)·전용 미니셀(`cmpDistRenderCell`)은 삭제됐고, 미니
    ECDF 는 Issue Table 과 같은 `renderIssueMiniDist` 경로를 쓴다. 서버 계약은 무변경.
  - **유의성 검정 + 노이즈 게이트**(2026-07-28, [significance.py](../web_report/tabs/significance.py)):
    `p_mean`(Welch t — 표시된 avg/stdev/n 을 그대로 써 화면 값과 어긋나지 않게) ·
    `p_stdev`(**Brown-Forsythe** = `|x−median|` 에 대한 Welch t). scipy 없이 `math.lgamma`
    기반 정규화 불완전베타로 Student-t CDF 를 직접 구현했다(폐쇄망 wheelhouse 배포라 의존성
    추가 비용이 크다). F-test 를 쓰지 않는 이유는 규격 절단·bimodal 분포에서 오경보율이
    폭증해 게이트가 무력화되기 때문.
    ⚠ **p 는 억제에만 쓰고 포함 근거로는 쓰지 않는다** — 같은 wafer 의 die 는 공간 상관이
    있어 독립 표본이 아니라 p 가 실제보다 작게 나오고, pooled n 이 수천~수만이라 무의미한
    차이도 거의 항상 유의해진다. "p 가 커서 낙관적으로 봐도 유의하지 않다 → 노이즈" 라는
    한 방향만 신뢰할 수 있다. 다중비교(FDR) 보정은 하지 않는다 — 억제 게이트라 관대한 쪽이
    실제 열화 누락을 막는다.
    화면에는 **p 전용 컬럼을 두지 않는다**(n 이 크면 0.000 벽이라 정보가 없다). 유의하지 않은
    값만 해당 셀에 `cmp-ns`(흐리게+ns)를 붙이고 p·n 은 title 툴팁으로 보여준다.
    **focus(관심 항목) 판정은 서버가 정본**이고 프런트는 그 불린으로 필터 토글만 한다.
    **룰 v3 (2026-09-01)** — `_dist_focus` 가 순서대로 본다:

    | # | 동작 | 조건 | 왜 |
    |---|------|------|-----|
    | ① | 제외 | 양쪽 `Cpk>DIST_CPK_HIGH`(**10**) / 양쪽 σ=0·결측 | 여유 과대·고정값 |
    | ② | 제외 | `p_mean≥α` **and** `p_stdev≥α` **and** `ks_d<0.10` | **사실상 같다** |
    | ③ | 제외 | `cpk_ratio≥105%` **and** `Δσ%<20` **and** `meanshift<1σ` | **개선** |
    | ④ | 포함 | 한쪽 `Cpk<CPK_THRESHOLD`(1.33) | 절대 품질(②③ 통과분만) |
    | ⑤ | 포함 | `\|Δσ%\|≥20`(After Cpk<1.33 이면 15) **and** `p_stdev<α` | 산포 증가 |
    | ⑥ | 포함 | `ks_d≥0.15` **and** (σ 거의 불변이거나 `p<α`) | **형태/위치 차이**(신규) |

    v2 와 갈리는 지점 3개가 곧 개선 내용이다. ⓐ ④ 에 **유의성 게이트가 없어** 원래 Cpk 가
    낮기만 하면 before/after 가 같아도 무조건 잡혔다(과검출 1순위) → ② 신설. ⓑ `|Δσ%|`
    **절대값** 비교라 산포가 줄어도(=개선) 잡혔다 → ③ 신설 + ⑤ 를 부호로 판단. ⓒ 모멘트
    2개(μ·σ)만 봐서 μ·σ 가 보존된 형태 변화(쌍봉화·꼬리 반전·완전 분리)를 **원리적으로**
    못 봤다 → ⑥ 신설(`ks_d` 는 이미 payload 에 있던 값이라 계산 추가 없음).
    실측 검증(`data/compare_shape_v2_verify.csv`, 202 항목): 과검출 39→17, 미검출 62→38,
    **눈으로 확실히 다른(L3~L4) 항목의 검출 손실 0건**.

    ⚠ ⑥ 의 "σ 거의 불변"(`DIST_KS_SOLO_MAX_SD`=3%) 단서를 빼지 말 것 — 값 종류가 적은
    이산(code unit) 항목은 σ 가 조금만 변해도 ECDF 계단이 통째로 밀려 `ks_d` 가 부푼다
    (실측: 15개 값 반복 데이터에서 Δσ +5% 가 ks 0.13, 같은 변화가 연속 데이터에선 0.02).
    ⚠ ③ 의 `cpk_ratio` 여유분(105%)도 `≥100` 으로 낮추지 말 것 — μ·σ 를 그대로 둔 채 모양만
    바뀌면 비가 정확히 100.00 이라 **변화 없음이 개선으로 오인**돼 ⑥ 에 닿지 못한다.

    σ 추정치의 변동계수가 `1/√(2(n−1))` 이라 n=15 면 ≈19% — 표본이 작으면 그 정도 변화가
    추정 노이즈와 구분되지 않아 오경보가 된다. n 이 수천인 보통의 pool 에서는 게이트가 사실상
    무동작이고, 작은 n(수율 낮은 항목·outlier 마스킹 후)에서만 일한다. p 를 낼 수 없으면
    (n<3·양쪽 고정값) 효과크기만 본다.

    **σ 증가의 기원 표시**(판정 아님): `tail_ratio_after/before`(σ/robust-σ)를 payload 에
    함께 싣고, Issue Table Compare 의 △σ% 셀에 `▲`(소수 die 이탈) / `▬`(전체 확산) 표식을
    붙인다(`compare_issue._stdev_origin` → 행의 `_sd_origin`, 화면 컬럼은 아니다).
    같은 △σ% 라도 조치가 다른데 숫자만으로 구분이 안 되기 때문이다(실측: die 2% 이탈
    1.16~1.19 vs 전체적 확산 0.93~1.04). ⚠ **이 둘을 판정으로 가르려 하지 말 것** — 그렇게
    하면 소수 die 이탈이 전부 최우선 검출로 올라온다(2026-09-01 실측 확인).

    정렬은 `meanshift_sigma` 내림차순(None 최하단, tie `|Δσ%|`).
    임계값은 `thresholds` 로 내려 프런트가 하드코딩하지 않는다(동일성 검증과 같은 패턴).
    룰을 고칠 때는 반드시 위 실측 데이터로 재확인한다 — 생성기는
    [tools/eval_testdata/make_compare_shape_testdata.py](../tools/eval_testdata/make_compare_shape_testdata.py)
    이고, 실행하면 현행 코드로 지표를 다시 재 `*_verify.csv` 를 낸다.
  - **동일성 검증**(`build_equivalence`): 항목별 `AVG차 = |After−Before|`,
    `AVG차(%) = |After−Before| / |Before| × 100` (**둘 다 절대값** — Grade1 이 "5% 이하"라
    부호가 섞이면 판정이 어긋난다). Grade1 = AVG차(%) ≤ `EQUIV_AVG_PCT_LIMIT`(5) /
    Grade2 = 초과 & `min(CPK_Before, CPK_After) ≥ EQUIV_CPK_LIMIT`(5) / Grade3 = 그 외.
    **판정 불가(Before 평균 0·한쪽 결측)도 Grade3 으로 집계**해 `Total = G1+G2+G3` 가 항상
    성립한다. 대상은 양쪽 pool 공통 항목이고 통계는 pooled `build_cpk_rows` 재사용(Bin1 기준).
    강조 3종: `AVG차(%)>5` / `CPK<5`(Before·After 각각) / `Grade 3`. **글씨에 색이 붙는 셀은
    예외 없이 배경도 칠한다** — 배경 규칙에 `!important` 가 붙어 있는데, 이는 sheet-table 의
    zebra(`tr:nth-child(even) td`)·hover 가 특이도 (0,2,2) 로 `.compare-table td.eq-bad`(0,2,1)를
    이겨 짝수 행·마우스오버에서 배경만 사라지던 문제 때문이다(`cpk-warn` 과 같은 처방).
    `AVG차`·`AVG차(%)` 헤더에는 위 수식을 `.eq-formula` 작은 글씨로 병기한다.
    **표시 규약** (2026-08-26 개정): 서버 반올림 자리수는 컬럼마다 다르다(min~max 6자리·
    average 4자리·cpk/cp 3자리·limit 6자리, stdev 만 **무반올림**). 그래서 `String(v)` 로
    그냥 찍으면 `2347582934789.234783` 같은 값 하나가 컬럼 폭을 통째로 밀어냈다 —
    **통계값은 소수 최대 4자리 반올림 + 전체 8자(부호 제외) 이내로 줄인다**
    (core.js `fmtLen8`). 적용 대상은 **CPK 탭 5컬럼**(min/median/max/average/stdev) ·
    Compare `_cmpStdev` · Item_detail(σ·통계표) · Composite 통계표 ·
    **Issue Table Compare 의 `before_*`/`after_*` 통계 컬럼**(sheets.js `CMP_STAT_COL_RE` —
    그 표는 전용 렌더러 없이 `renderSheetTable(kind:"issue")` 를 일반 Issue Table 과
    공유하므로 **컬럼명으로만** 대상을 가른다. 경계가 무너지면 일반 표까지 축약된다).
    규칙: 원문이 8자 이하면 손대지 않음 · 소수 4자리 **반올림** · 정수부가 길면 소수를 더
    줄임(`234626.2346234`→`234626.2`) · 끝자리 0 제거 · **자리올림이 나면 그 자리에서 버림**
    (`999999.99`→`999999.9` — `1000000` 이 되면 앞자리가 통째로 바뀌어 다른 값처럼 보인다) ·
    유효숫자 2자리 미만으로 뭉개지면 지수표기(`0.00034345`→`3.4345e-4`, 단 원래 짧은
    `0.0003` 은 그대로).
    축약된 셀만 원값을 `title` 툴팁으로 보여준다 — **점선 밑줄은 넣지 않는다**(통계 컬럼
    대부분이 축약 대상이라 표 전체에 밑줄이 깔려 산만했다. `.cpk-abbr` 클래스는 축약 표식
    으로 계속 붙되 스타일은 없다).
    `cpl`/`cpu`/`cp`/`cpk`·limit 은 서버가 이미 짧게 주므로 **원문 그대로**(`_cmpServer`).
    ⚠️ **표시 전용이다 — payload 값은 불변**이므로 CPK Limit 역산(`cpkComputeTargets`)·
    Item_detail 가우시안 곡선·Excel Download 는 계속 원값을 쓴다.
    구현 함정 3종은 [tests/test_cpk_len8_js.py](../tests/test_cpk_len8_js.py) 가 고정한다
    (지수부 길이 누락 → 값 100배 오차 / 지수표기 끝자리 0 제거 → 문자열 깨짐 /
    곱셈 버림 → 부동소수점 오차로 `8.7`→`8.6`). `fmtStdev` 는 호출자가 없어졌지만 보존.
    회귀 고정: [tests/test_compare_equivalence.py](../tests/test_compare_equivalence.py)
    (`test_single_source_group_matches_cpk_sheet` 이 average/stdev/cpk + limit 을 CPK 시트와 대조).
- **Issue Table Compare 탭 (Compare 모드 전용, 2026-08-20 신설 → 2026-08-27 서브탭 5개)**:
  Compare 결과 전체를 담는 **단일 탭**. 탭 노출 규칙은 `tabs_topbar.syncTabVisibility`
  (`modeNow === "Compare"`). 2026-08-27 구 최상위 `Compare` 탭을 흡수해 서브탭이 됐다.

  | 서브탭 | 내용 | 빌더 | 코멘트 채널 |
  |---|---|---|---|
  | `ISSUE_TABLE` | Distribution(`dist_shift` focus 전부 + `new_items`) + ETC | 시트 `sheets["Issue Table Compare"]` (`compare_issue.py`) | `issue_comment` (`CMPDIST\|` / `CMPETC\|`) |
  | `MAP비교` | 공통성 Map + 동일 좌표 Bin 비교 + Bin Yield 비교 | `compare.js cmpMapPanelHtml` | `compare_note` (`bm:<x>,<y>`) |
  | `LOG비교` | 추가/삭제/Limit 변경 **요약표** + goodlog 전체표 | `compare.js cmpLogPanelHtml` | `compare_note` (`gl:…`) |
  | `TESTTIME비교` | 정적 안내(데이터 없음) | report_view.html 정적 마크업 | — |
  | `동일성검증` | Grade 표 | `compare.js cmpEquivPanelHtml` | — |

  **구조상 핵심 2가지**:
  - 탭 패널은 `#panel-issue-cmp`(서브탭 바 + 서브패널 5개)이고, **이슈 표는 그 안
    `#panel-issue-cmp-table` 서브패널에만** 들어간다. `renderIssueTableInto` 가 대상 div 의
    innerHTML 을 통째로 갈아치우기 때문에 서브탭 바가 그 밖에 있어야 살아남는다
    (Characteristic 이 `#panel-trim-analysis` 를 첫 서브패널에 둔 것과 같은 관례).
    `core.js` 의 `ISSUE_PANEL_CMP`/`ISSUE_PANEL_SEL` 이 **서브패널 id** 를 가리킨다.
  - 서브탭은 **lazy** 다(`CMP_SUB_RENDERERS` + `cmpSubDirty`, Characteristic 패턴) —
    공통성 Map 이 Plotly + die 수천 개라 숨김 상태 선렌더를 피한다. `issue-cmp` 는 이
    Plotly 때문에 `edit_mode.PLOTLY_TABS` 에 등록돼 있다.

  2026-08-27 변경 2건: **구 하단 Bin Transition 표 삭제**(MAP비교에 같은
  `compareBinMatrixHtml` 표가 있어 중복 — 규칙 13) · **Log 요약표는 LOG비교 상단으로 이동**
  (goodlog 전체표와 같은 `gl:` 키를 공유하므로 한 화면에 있어야 한다).
  - **Log 요약표는 "변경 행만"** 이다(사용자 확정) — Gap% 만 큰 행은 이슈가 아니라 값 차이
    관찰이라 같은 서브탭 아래 goodlog 전체표에서 본다. 분류는 `goodlogRowType` **재사용**.
  - ⚠ **같은 `gl:` 키 셀이 LOG비교 안에 2개** 있다(요약표 + 전체표). 저장 시
    `compare.js syncCompareNoteCells` 가 **같은 키 셀을 전부 갱신**한다 — 편집한 td 만
    고치면 같은 항목 코멘트가 화면에서 갈린다(규칙 13). 데이터는 `DATA.compare_notes` 가
    서버 권위본이라 항상 맞고, 갈리는 건 DOM 뿐이다.
  - **코멘트가 두 채널로 갈린다**: ISSUE_TABLE 은 Issue Table 과 같은 `issue_comment`
    (PTE 1열 + Status), MAP/LOG비교는 `compare_note`(`DATA.compare_notes` 한 벌).
  - **컬럼 (2026-08-27 사용자 요청)**: `Unit` 과 `개발 comment` 는 **화면에서만 숨긴다**
    (`sheets.js orderColumns` 의 `cmpHidden` — Compare 시트 판정은 `before_*`/`after_*`
    컬럼 존재). payload·저장 키는 그대로라 **스키마 bump 불필요**하고 기존 '개발 comment'
    입력값도 DB 에 살아 있다(규칙 12). payload 에서 실제로 빼는 것은 다음 번
    `COMPARE_REPORT_SCHEMA_VERSION` bump 때로 미뤘다(그것만으로 콜드 폭풍을 부르지 않으려고).
    `statsFold` 는 **Compare 패널만 기본 접힘**이고 접는 대상은 before/after 원시 통계 6개 +
    `meanshift_σ` **7개**다 — `△σ%`·`cpk%` 는 항상 보이며 표시만 소수 1자리로 줄인다
    (원값은 title 툴팁, `sheets.js CMP_PCT_COL_RE`).
    ⚠ `report_view.html` 의 `#panel-issue-cmp … td:nth-child(4)/(5)/(6)` 무효화 규칙은
    **컬럼 인덱스 하드코딩**이라 컬럼을 넣고 뺄 때마다 함께 점검해야 한다.
  - **row_key(저장 키, 불변)**: `CMPDIST|<item>` / `CMPETC|<item>`. 숨김·Status 키도 같다
    (행이 곧 item). 숨김은 **CMPDIST 만** 허용한다(CMPETC 는 항목 자체를 지운다 — 기존 ETC
    와 같은 취급, `service._ISSUE_HIDABLE_PREFIXES`). 서버가 Category 셀에 **섹션 키를
    그대로** 싣고(`tabs/compare_issue.py`) 프런트가 그 값을 상속해 접두를 만든다
    (`sheets.js issueRowKey`) — 화면 표시 이름은 `ISSUE_SECTION_TITLES` 가 따로 갖는다.
    Category 에 "ETC" 같은 표시 문구를 넣으면 메인 시트 섹션 키와 충돌해 저장 키가
    `ETC|<item>` 이 된다(= 메인 Issue Table 코멘트를 덮어쓴다).
  - **패널 일반화 재사용**: `core.js` 의 `ISSUE_PANEL_SEL` 에 `#panel-issue-cmp` 를 넣는
    것만으로 편집·검색·Status 필터·미니셀·삭제 모드가 전부 함께 돈다(Temperature 개편이
    터놓은 길). 새 Issue 계열 표를 또 만들 일이 있으면 이 상수부터 보라.
  - **화면 정리 4종 (2026-08-26 사용자 요청 — 전부 프런트 전용, payload·스키마 무변경)**:
    | 무엇 | 어디 |
    |---|---|
    | 통계 9컬럼(before/after avg·stdev·cpk + 비교지표 3) 접기 | 툴바 버튼 `data-issue-act="cmp-stats"`(`yield_issue.issueToolbarHtml`) → `applyCmpStatsFold` → 패널 클래스 `.cmp-stats-folded` + 셀 클래스 `.cmp-stat-col`(`sheets.js CMP_FOLD_COL_RE` 가 col/th/td 에 부여). 상태는 `issueUi(panel).statsFold` — **휘발**(새로고침하면 펼침) |
    | 헤더 표기 축약 | `sheets.js COLUMN_DISPLAY_ALIAS` — `meanshift_sigma→meanshift_σ` / `stdev_delta_pct→△σ%` / `cpk_ratio_pct→cpk%`. **표시 전용**(저장 키·payload 키는 원문 그대로) |
    | `구분`(산포/신규) 컬럼 숨김 | `sheets.js orderColumns` 가 Category 와 같은 방식으로 화면 컬럼에서 제외. 서버는 계속 값을 싣는다(`compare_issue._base_row`) — **payload 무변경이라 캐시 세대를 올리지 않는다** |
    | 산포 미니셀 | `sheets.js` Distribution 셀 화이트리스트에 `CMPDIST`/`CMPETC` 추가 → 일반 Issue Table 과 **같은** 지연 렌더 경로(`renderIssueMiniDist` → `distribution_batch`). variant 를 안 붙여 전체 범위 ECDF 라 Before/After 곡선이 한 셀에 겹쳐 그려진다 |
  - ⚠ **구 하단 2표가 "잘려서 안 보이던" 이유** (2026-08-26 수정, 그 표들은 2026-08-27
    서브탭으로 이동해 지금은 하단 형제가 없다): `.sheet-wrap.kind-issue` 는
    `position:sticky` + 뷰포트 `max-height` 라 표가 화면에 눌러앉는다. 뒤 형제가 없는
    메인/Temp 패널은 문제가 없지만 이 패널만 `.cmpiss-extra` 가 그 뒤라 영영 가려졌다.
    → `#panel-issue-cmp .sheet-wrap.kind-issue { position: static }` 한 줄로 푼다(래퍼 추가
    금지 규칙을 지키면서). **이 한 줄은 계속 유지한다** — 서브탭 흡수 후 sticky 층이
    `.cmp-toolbar`(서브탭 바) → `.issue-toolbar` → `.issue-hscroll` 3층이 됐는데, 표 본체까지
    sticky 면 그 위에 또 눌러앉는다. 내부 스크롤·섹션 헤더 sticky 는 그대로다(헤더 고정
    기준은 position 이 아니라 overflow 컨테이너). 같은 스코프로 공용 규칙의 컬럼
    좌측고정·`min-width:144px`(원래 Map/Distribution 용)도 무효화한다 — 이 표엔 Map 이 없어
    그 자리가 통계 컬럼이다.
  - **ETC scope 분리**: ETC 항목 목록은 `etc_item`(메인) ↔ `cmp_etc_item`(Compare) 으로
    kind 자체가 갈린다. 라우트는 하나이고 body 의 `scope`("main"|"compare")로 고른다
    (`POST .../web_report/issue_table/etc`). 프런트는 모달 dataset 에 scope 를 실어
    저장 시점에 읽는다(`edit_mode.issueEtcScope`).
  - **Summary 합류**: Compare 요약 카드(산포 검출/신규·삭제 item/Limit 변경/Bin 불일치 —
    수치는 서버 payload 를 그대로 읽는다) + Issue Status 카드의 `Compare` 행(CMPDIST+CMPETC
    합산) + ENGR Comment 의 `compare` 칸. compare 가 pending 이면 카드가 "계산 중" 을 낸다.
  - **캐시 세대**: 이 시트는 report payload 에 실리므로 구조를 바꾸면 전역
    `REPORT_SCHEMA_VERSION` 이 아니라 **`COMPARE_REPORT_SCHEMA_VERSION`** 을 올린다
    (Compare 세션만 무효화 — 콜드 폭풍 회피, `TEMPERATURE_SCHEMA_VERSION` 과 같은 취지).
    compare **계산 결과** 캐시 세대인 `COMPARE_SCHEMA_VERSION` 과 혼동하지 말 것.
  - **eval DB export**: CMPDIST/CMPETC 코멘트는 `fail_case.test_condition='COMPARE'` 로
    나간다 — 같은 item 의 일반 코멘트(`''`)와 **다른 case** 라 서로 덮어쓰지 않는다
    (TEMP 와 같은 규약 → [13 §row_key](13_eval_analyzer_integration.md)). bm:/gl: 는
    `compare_note` kind 라 애초에 export 대상이 아니다.
  - 회귀 고정: [tests/test_compare_issue_table.py](../tests/test_compare_issue_table.py)(서버) ·
    [tests/test_compare_issue_js.py](../tests/test_compare_issue_js.py)(headless Edge — 저장 키·
    Log 필터·요약 카드). 검증용 합성 데이터는
    [tools/eval_testdata/make_compare_testdata.py](../tools/eval_testdata/make_compare_testdata.py).
- **Distribution**: `build_distribution_index`(항목별 test_num·worst cpk·fail·status) /
  `scatter_item`(상세 전체 측정값) / `build_distribution_compact`(ECDF 전 포인트 컴팩트
  columnar, lazy 전용). `/distribution`(전량)과 `/distribution_batch`(항목 배치) 모두
  전 포인트·gzip·ETag.
  - **항목 배치 로드 (2026-07-21, 대용량 대응)**: 전량 `/distribution` 은 10 sources ×
    500 items × 2000 rows 에서 ECDF 970만 포인트 = gz 55MB 라 다운로드·파싱·JS 힙 상주가
    모두 폭증했다(실측). 프런트는 이제 IntersectionObserver 로 **보이는 항목만** 모아
    (디바운스 50ms, 배치 ≤30, 동시 ≤2) `distribution_batch` 로 받는다 — 같은 조건에서
    첫 화면 전송량 gz 3.3MB(17배 감소). 서버는 `compute_dist_compact(only=…)` 로 항목만
    좁혀 계산하므로 **결과는 전량 payload 에서 그 항목만 뽑은 것과 정준 JSON 일치**
    (다운샘플 아님 — 규칙 #6 무관). 표시용 다운샘플·세로 채움은 종전대로 클라 담당.
  - **표시 규격선(LSL/USL)의 단일 기준도 `distribution_index`** (2026-08-13). ECDF
    compact/pack 이 함께 싣는 `lo`/`hi` 는 **항목이 처음 등장한 소스**의 limit 이라,
    Temperature 세션에서 업로드 소스 순서상 첫 소스가 CT/HT 면 CT/HT 규격선이 그려졌다
    (CPK 탭·Item_detail 은 RT 기준이라 화면끼리 어긋난다). 이제 인덱스가 그룹의 **RT
    limit** 을 담고(`build_distribution_index(temperature_groups=…)`, 기준 선택은
    `cpk.temperature_reference_tables` 한 곳), 프런트 미니셀·갤러리 셀은 `distSpecLimits`
    로 인덱스에서 가져간다. pack 의 `lo`/`hi` 는 업로드 시점에 굳어 RT 를 알 수 없으므로
    **데이터가 아니라 표시 기준을 인덱스로 모으는** 방향으로 맞췄다. 캐시는
    `cache_policy.TEMPERATURE_SCHEMA_VERSION`(Temperature 세션 전용 세대)으로 무효화한다.
  - **항목 존재 판단은 `distribution_index`** — 인덱스와 ECDF compact 는 같은 기준
    (측정 data 전무 항목만 제외)으로 항목을 고르므로, 데이터를 받아보지 않고도 "분포가
    있는 항목인지"를 알 수 있다. Issue Table 미니셀 생성 여부가 이 판단을 쓴다
    (`distHasData` — 캐시 보유 여부로 판단하면 아직 안 받은 항목의 셀이 안 만들어진다).
  - 보유 항목은 LRU 상한(`DIST_BATCH.CACHE_MAX` 300)으로 잘라 오래 스크롤해도 힙이
    무한히 자라지 않게 한다. 축출된 항목은 다시 보이면 재요청된다.
  - **전량 `/distribution` 라우트는 유지** — 클라 업로드 프리컴퓨트 dist blob 시딩
    (ingest)과 하위호환 폴백이 쓴다. 프런트가 더 이상 호출하지 않을 뿐이다.
  - 항목 상세(전 포인트 + serial/xpos/ypos hover 메타)는 종전대로 `/scatter/<subject>`.
  - **← Back 은 들어가기 전 스크롤 위치로 돌아온다** (2026-08-25) — 갤러리를 한참 내려가
    카드를 열면 매번 맨 위로 튀던 문제. `openItemDetail` 이 **상세를 새로 열 때만**
    `_itemDetailReturnScroll` 에 문서 스크롤을 기억하고(`_itemDetailReturnId` 와 같은
    블록), `closeItemDetail` 이 복귀 패널을 active 로 되돌린 **뒤** 그 값으로 `scrollTo`
    한다(rAF 로 1회 보정). 상세 안 prev/next 이동은 이미 active 라 기억값을 덮지 않고,
    탭 전환(`hideItemDetail`)은 복원 없이 버린다. 갤러리 카드가 `.distg-card` 고정 높이
    264px 이라 IntersectionObserver 가 차트를 purge 해도 문서 높이가 유지되는 것이 전제다
    — 카드를 가변 높이로 바꾸면 복원 위치가 어긋난다. 회귀 고정
    [tests/test_item_detail_scroll_js.py](../tests/test_item_detail_scroll_js.py).
  - **Distribution composite (합성 산포 차트 — 2026-08-24)**: 툴바 우측 "분석하기 ▾"
    (편집모드 전용) → 모달에서 source 복수 + TestItem 검색·체크 복수 선택 → 고른
    **source × item 조합 각각이 legend 1개**(`<source>_<item>`)인 차트 1장을 갤러리 맨 앞에
    추가한다. 카드 클릭 시 전용 상세 패널(`#panel-dist-composite-detail`)에서 ECDF 전량 CDF +
    pair 별 통계표를 본다. 프런트 [dist_composite.js](../server/report/static/webreport/dist_composite.js).
    - **서버 계산 추가 없음** — 기존 `distribution_batch` 응답의 `bySource` 에서 고른 source 만
      골라 그린다. 저장하는 것은 **정의뿐**(`kind=dist_composite`, item_key=UUID 불변,
      value=`{name, pairs, limit, colors}`). pairKey 구분자는 issue_comment 와 같은 U+001F.
    - 색은 **생성 시 배정해 저장**한다(`distDefaultColor` 를 랜덤 오프셋부터 순차) — 리로드마다
      바뀌면 사람이 기억한 legend 색이 무의미해진다. 수정 시 살아남은 pair 색은 유지.
    - limit 은 ① 선택 항목 하나의 limit(표시 기준은 `distSpecLimits` = distribution_index)
      또는 ② 직접 입력(lo/hi). 항목마다 단위가 달라 x축을 공유하므로 규격선은 이 기준 하나만 긋는다.
    - ECDF 는 고유값+누적%라 die 수를 모른다 → 통계표는 Δp 가중 **모집단** 통계
      (mean=Σx·Δp, σ, median, cpk)로 복원하고 각주로 밝힌다. scatter API 를 항목 수만큼
      호출하지 않는 이유가 이것(비용 대비 정밀도 이득 없음).
    - 카드는 `.distg-card.distg-comp` 라 IntersectionObserver·purge·rAF 큐를 그대로 재사용하고,
      점 색만 `plot._distColorFor` 주입으로 pairKey 기준이 된다(미설정이면 기존 경로 그대로).
    - 저장은 `POST .../web_report/dist_composites`(ops 배열, null=삭제) 단발. 응답이 권위본이라
      `load(false)` 재로드를 하지 않는다 — kind 가 payload 중립이라 report 캐시가 살아 있다.
    - **Serial 순 토글**이 켜지면 카드·상세가 run chart 로 바뀐다(단 통계표는 ECDF 기준 고정) →
      아래 "Serial 순" 절.
    - **상세의 차트 주석** (2026-08-24 추가): 키는 `cdf:comp:<uuid>`(`dcNoteSubject`) — 이름이
      아니라 **UUID** 라 차트를 개명해도 주석이 따라온다(§5-12). 툴바(`#chartNoteBar`)와 코멘트
      뷰(`#cdfCommentView`)의 DOM id 를 **Item_detail 과 공유**하므로 상세는 한 번에 하나만
      열려야 한다 — `dcOpenDetail` 이 먼저 `hideItemDetail()`(미저장 주석 flush + 패널 비우기)을
      부르는 것이 그 보장이다. 저장값을 다시 그릴 때 어느 쪽 상세가 열려 있는지는
      `cnActiveSubject()` 가 판정한다(`_itemDetailData` 는 상세를 닫아도 남아 있어서 그것만
      보면 합성 차트 밑에 **직전에 보던 항목의 코멘트**가 찍힌다). seq 모드에는 붙이지 않는다.
    - ⚠️ **저장 spec 을 `distIndex`/현재 source 목록으로 filter 하지 말 것** — 전처리 항목 제외나
      source 축소로 목록에서 빠진 pair 가 "이름만 바꿔 저장" 하는 순간 조용히 사라진다.
      서버(`service._sanitize`)는 실재 여부를 검사하지 않고 보존하는데 클라가 버리면 그 방어가
      무의미해진다. `dcOrderedPick(order, sel)` 이 표시 순서만 목록을 따르고 목록 밖은 뒤에
      붙여 보존한다(모달의 `dcRenderPicked` 가 이미 쓰던 규칙과 통일).
  - **Gap Chart (사용자 수식 파생 분포 — 2026-08-24)**: 같은 "분석하기 ▾" 메뉴의 두 번째
    항목. 모달(좌우 2단 — 왼쪽 항목 목록 / 오른쪽 source 선택·수식, 차트 이름 **아래 줄**에
    Limit)에서 식을 만들면 그 결과 분포가 갤러리 맨 앞 카드로 추가되고, 카드를 누르면
    **기존 Item_detail 화면이 그대로** 열린다. 프런트
    [gap_chart.js](../server/report/static/webreport/gap_chart.js),
    계산 [web_report/gap_chart.py](../web_report/gap_chart.py).
    - **수식 입력은 평문 타이핑이다 (2026-08-26 개편 — Honey `honey_ui/formula_editor.py`
      와 같은 방식)**: 숫자·`+ - * / ( )` 는 그대로 치고, 항목은 `@` 자동완성이 넣는
      `@"항목명"` 인용 표기(이름 안 `"` 는 `""`), source 명시는 `@"source"!"항목명"`.
      렉서는 `gcLex`(gap 문법 부분집합 — 함수·비교 없음) 이고 위쪽 해석 창(#gcExpr)은
      읽기 전용 칩이다. **목록에 없는 이름은 경고만 하고 막지 않는다** — 전처리 제외로
      목록에서 빠진 항목을 참조하는 기존 차트의 이름·Limit 수정을 막으면 안 된다(§5-12).
      구 방식(빈 입력창에서 연산자 키 커밋 + 버튼)은 폐기됐다.
    - **수식은 평문이 아니라 토큰 배열이 정본**이다(`kind=gap_chart`, item_key=UUID 불변,
      value=`{name, sources, tokens, limit}`). item 이름에 공백·`( )`·`+ - * /` 가 전부
      합법이라(honeyform 은 중복·메타충돌만 검사) **인용 없는** 평문을 토큰으로 되돌리는
      렉서는 원리적으로 존재할 수 없고, source 명·item 명 둘 다 `_` 를 포함할 수 있어
      `source_item` 분해도 불가능하다 — `@"..."` 인용이 그 모호성을 없애는 장치다.
      수정 모달은 tokens → `gcTokensToText` 로 원문을 복원한다(라운드트립 — 음수 num
      토큰만 op(-)+num 으로 갈라지는데 서버 단항 문법상 등가).
      표시 문자열(`render_formula`/`gcFormulaText`)은 **절대 재파싱하지 않는다**.
    - 수식 모드는 **저장하지 않고 매번 유도**한다(규칙 13) — 항목만 참조면 `per_source`
      (선택한 각 source 안에서 계산, 시리즈 N개), 전부 source 명시면 `explicit`
      (**좌표(XPOS,YPOS) 교집합**으로 계산, 시리즈 1개). 좌표 중복(재검)은 **첫 행 우선**
      으로 `tabs/compare.py _coord_bin_map` 과 같은 규칙이다. 둘을 섞으면 400 —
      같은 수식이 source 마다 다른 의미가 되어 결과를 읽을 수 없다.
    - 파서는 `eval()` 없이 **재귀하강**이다(shunting-yard 가 아닌 이유: 위반 토큰의 인덱스를
      알 수 있어 400 응답에 실어 프런트가 그 칩을 표시한다). 평가는 numpy 벡터 연산이고
      0 나눗셈은 inf/NaN 을 만든 뒤 유한값 마스크로 제외한다(제외 수는 `dropped_nonfinite`).
      마스크는 values·serial·xpos·ypos **네 배열에 함께** 적용한다(`scatter_item` 규약).
    - 응답은 **`scatter_item` 키 집합을 그대로 포함**한다 → Item_detail 이 수정 없이 재사용된다
      (`openItemDetail(subject, navList, opts)` 의 `opts.url` 만 갈아끼운다). Item_detail 헤더
      바로 아래에는 **어떤 수식이었는지**를 만들 때와 같은 서식(item 파란 기울임 / source
      빨간 기울임)으로 보여준다 — 그래서 응답에 `tokens` 를 함께 싣는다(평문 `formula` 는
      되돌려 읽을 수 없어 서식을 복원할 수 없다. 구 캐시 응답은 평문으로 폴백).
      모달 폭은 `min(1240px, 94vw)`(2026-08-26 사용자 요청으로 1600px 에서 축소 —
      해석 창 글씨 13px 축소와 세트)이고, 셀렉터는
      **`.modal-box.gc-modal-box`** 로 특이도를 올려야 한다 — 한 클래스로 쓰면 뒤쪽
      `.modal-box{width:360px}` 가 이겨 창이 좁아진다(dc 와 같은 함정을 실제로 밟았다).
      차트 주석 키는
      `note_subject = "gap:<uuid>"` 로 갈라 동명의 실제 항목과 섞이지 않게 한다.
      CDF die 제외 편집바는 gap 에서 숨긴다 — 합성값에는 되돌릴 원본 항목이 없다.
    - 조회는 전용 라우트 `GET .../web_report/gap_chart/<chart_id>` **하나**이고 갤러리 카드와
      상세가 같은 응답을 공유한다(카드는 프런트 `distCdfFromValues` 로 ECDF 를 만든다) →
      카드를 본 뒤 클릭하면 상세가 클라 캐시 히트다. 캐시 키·ETag 둘 다 `spec_digest`
      (정의 sha256[:16])를 물고 있어 수식 수정 시 자연 무효화된다. **합성 항목명을
      `distribution_batch` 의 `subjects` 나 `/scatter/<subject>` 에 섞지 않는다** — 섞으면
      dist pack 미스로 전 tables 디코드 + 전 항목 ECDF 재계산이 터진다.
    - 상한: 세션당 차트 20개 / 토큰 200개 / 괄호 16단 / 참조 항목 20개 / 정의 16KB.
      카드 1장이 Item_detail 1개분 페이로드라 차트 수 상한이 실질 보호막이다.
    - `distUpdateCount()` 의 "N 개"에는 gap·composite 카드가 **포함되지 않는다**(distIndex
      밖이라 검색·세그먼트 필터 대상이 아니다) — composite 와 같은 기존 동작이다.
    - **Serial 순 토글**이 켜지면 카드도 run chart 가 된다(상세는 Item_detail 재사용이라
      자동으로 따라온다). 캐시는 늘리지 않는다 → 아래 "Serial 순" 절.
  - **Serial 순 (rawdata 누적 순 run chart — 2026-08-24)**: 툴바 **맨 앞** 버튼
    (`data-seg="seq"`, Item_detail 표시옵션에도 같은 버튼 `data-idet-seg="seq"`)으로
    갤러리 미니셀과 Item_detail CDF 자리를 **x = 각 source 의 측정 순서(1..n) · y = 측정값**
    차트로 바꾼다. Limit 은 **수평** 점선이고 markers 전용(선 금지 — 규칙 #5). 켜고 끄는
    상태는 전역 하나(`distSeqOnly`)라 갤러리·상세가 같은 모드를 본다(Bin1·Limit 토글 관례).
    - **ECDF payload 로는 그릴 수 없다** — `build_distribution_compact` 이 `np.unique` 로
      동일값을 접어 순서를 버린다. 그래서 **행 순서를 보존한 값 배열**을 내는 배치 응답
      `GET .../distribution_batch?order=seq`(포맷 `seq-columnar-v1`, 계산
      [web_report/dist_seq.py](../web_report/dist_seq.py))를 따로 낸다. x 는 인덱스라
      서버는 값만 보낸다(ECDF 의 xs+ys 2배열보다 가볍다).
    - **dist pack 지름길을 쓰지 않는다** — pack 은 업로드 시점에 정렬(np.unique)해 굳힌
      산출물이라 순서가 없다. seq 는 항상 tables 를 읽는다(TABLES_CACHE 공유 = `/scatter`
      와 같은 비용). Item_detail 은 아예 서버를 다시 부르지 않는다(`scatter_item` 의
      values/serial 이 이미 행 순서다).
    - **모듈 위치가 `web_report/tabs/` 밖인 이유**: perf_guard S01 이 tabs 변경마다
      `REPORT_SCHEMA_VERSION` bump 를 요구하고 그 bump 는 전 세션 콜드 폭풍이다
      (gap_chart.py 와 같은 이유). 이 계산은 report payload 와 무관하다.
    - **변형 키가 6종이 된다** — bin1 축 3종 × 정렬 축 2종. `distGalleryVariant()` 는
      **계속 bin1 3종만** 반환하고(그 값을 dist_composite/gap_chart 가 자기 캐시 인덱스로,
      item_detail 이 `/scatter` 쿼리로 쓴다) 갤러리 미니셀이 쓰는 키는
      `distGalleryDataVariant()` 다. 캐시는 변형마다 별도 객체다.
    - **미니셀 표시 캡은 균등 stride**(`distHardCap`, 양끝 보존)뿐이다 — ECDF 전용 규칙
      (세로 채움 `distFillVertical`·꼬리/Δy 보존 다운샘플)은 "x 오름차순 · y 단조 누적%"
      전제 위에 있어 run chart 에 쓰면 없던 구조를 만든다. 상세는 전량 렌더.
    - **차트 주석(chart_notes)은 seq 차트에 붙이지 않는다** — 주석 도형은 데이터 좌표
      (xref"x"/yref"y")로 저장되는데 이 축은 (순서, 측정값)이라 CDF 좌표와 의미가 다르다.
      붙이면 편집 모드에서 도형을 한 번 건드리는 순간 `cnSyncFromChart` 가 **seq 좌표로
      저장값을 덮어써** 사용자가 CDF 에 그려둔 주석이 망가진다(§5-12). 저장값은 그대로
      두고 표시만 생략한다 — CDF 로 돌아가면 다시 보인다. Map 선택 좌표 마커·Compare
      before-limit 선도 같은 이유로 제외(누적% 축 전용), CDF x축 옵션 바도 비운다.
      - ⚠️ **렌더에서 안 부르는 것만으로는 부족하다 (2026-08-24 보강)**. `_cnCharts` 는 CDF 를
        한 번 그리면 등록되고 스스로 지워지지 않는데 seq 는 **같은 DOM 노드**(`#distCdf` /
        합성 상세는 `#dcDetailChart`)를 덮는다. 등록이 남아 있으면 그 뒤의 저장 경로
        (`cnFlush`·comment 입력·undo)가 seq layout 에서 shapes 를 회수해 **빈 배열**을 얻고,
        `cnFlush` 가 그것을 `value:null` 로 보내 **서버의 주석 레코드를 지운다**. 그래서 seq 로
        덮어 그리기 **직전에** `cnDetach(key)`(chart_notes.js)를 부른다 — dirty 면 아직 살아
        있는 CDF layout 에서 도형을 회수한 뒤 등록만 푼다(pending 은 보존, 텍스트 도구가 쓰는
        `gd._cnBoundKey` 도 함께 비운다). 호출 지점은 `distRenderCdf` 의 seq 분기 한 곳이라
        재렌더 호출부(축옵션·칩 편집·항목 이동)가 전부 따라온다.
    - **seq 배치 크기는 ECDF 보다 작다** (2026-08-24). seq 는 동일값을 접지 않으므로 항목당
      payload 가 ECDF 의 한 자릿수 배 이상이다 — 5 source × 25,000 die 면 항목 1개가 125,000
      값이라 ECDF 와 같은 30개 묶음은 한 요청이 수십 MB 가 된다. 프런트
      `DIST_BATCH.SEQ_SIZE`(8) / 서버 `_DIST_SEQ_BATCH_MAX`(10)로 **짝**이며 한쪽만 바꾸면
      400 이 난다(`dist_composite.dcEnsureItems` 도 같은 값을 쓴다). 총 데이터량은 그대로고
      요청당 크기만 줄이는 것이라 **규칙 #5(다운샘플 금지)와 무관**하다.
    - Issue Table 미니셀은 **전체 기준 ECDF 를 유지**한다(Bin1 토글과 같은 정책 — 그 표의
      숫자가 ECDF 기준이라 그림만 다른 축이 되면 표와 어긋난다).
    - **사용자가 만든 카드 2종에도 적용된다** (2026-08-24 확장) — 갤러리가 한 모드로 보여야
      "왜 어떤 카드만 안 바뀌지"가 없다. 서버는 **둘 다 무수정**이다:
      - **Gap Chart**: `/gap_chart/<id>` 응답 값이 **이미 rawdata 행 순서**다(per_source = 그
        source 의 행 순서 / explicit = 첫 참조 source 행 순서의 좌표 교집합, `gap_chart.py`
        `_build_explicit`). 그래서 **변형 캐시를 늘리지 않는다** — `gcBuildSeries` 가 같은
        응답에서 ECDF(`entry`)와 Serial 순(`seqEntry`) 두 표현을 한 번에 만든다.
        seq 키를 `_gcCache` 에 넣으면 `gcDropCache` 의 키 목록과 어긋나 수식을 고쳐도 옛 값이
        남으므로 넣지 말 것. explicit 모드의 순서는 좌표 교집합이라 **base source 행 순서의
        부분수열**이고 x 는 1..m 으로 다시 매겨진다.
      - **Distribution composite**: 기존 `distribution_batch` 를 `order=seq` 로 한 번 더 받는다
        (`_dcCache`/`_dcInflight` 를 `DIST_VARIANTS` 6키로 생성 — 리터럴로 적지 말 것).
        배치 크기는 seq 만 따로다 → 아래 "seq 배치 크기" 항.
        **상세는 차트만 seq 이고 통계표(`dcPairStats`)는 항상 ECDF 기준**이다: 그 함수가
        ECDF 의 Δp 가중으로 모집단 통계를 복원하는 구조라 seq 배열로는 만들 수 없고, 원본값으로
        다시 계산하면 같은 화면 숫자가 모드마다 달라진다(규칙 #13). 그래서 seq 상세는 ECDF·seq
        **두 캐시를 함께 확보**한다.
      - 두 카드 모두 미니셀 레이아웃은 공용 `distSeqCellLayout` 하나를 쓴다(축·여백·기준선이
        갈라지지 않게). Map 선택 좌표 마커는 (값, 누적%) 좌표라 seq 축에서 제외한다.
      - **y축 스케일 지배**: 단위·범위가 다른 항목을 겹치면 큰 값이 y축을 먹는다(ECDF 는 y가
        0~100%라 정규화 효과가 있었다). 같은 단위끼리 고르는 것을 전제한 의도된 타협이다.
    - **Note 붙여넣기 폴백**: seq 는 `chartNotesApply("cdf", …)` 를 부르지 않아 `_cnCharts` 에
      등록되지 않는다 → `cnPasteToNote` 는 엔트리가 없으면 `#distCdf` 를 직접 집는다(화면
      그대로 캡처). **등록은 하지 않는다** — 등록하면 드래그가 주석을 seq 좌표로 덮어쓴다.
    - 회귀 고정: [tests/test_dist_seq.py](../tests/test_dist_seq.py)(서버 6항목 — 행 순서·
      ETag 분리·bin1·`/scatter` 값 일치) · [tests/test_dist_seq_js.py](../tests/test_dist_seq_js.py)
      (프런트 12항목 — 변형 분리·stride 캡·주석 미부착·**세 카드 공용 레이아웃 일치**·
      **composite 상세 통계표 불변**·`_dcCache` 6키·Note 폴백).
- **Trim Analysis**: `build_trim_payload`(항목 매칭 + 슬롯별 통계 + initial shift 판정) /
  `build_trim_chart`(그룹 1개 chip-to-chip 차트).
  **탭 진입만으로는 서버를 전혀 부르지 않는다** (2026-07-23) — 진입 시엔 sticky 툴바만
  그리고, 초록색 「분석 시작」(`#trimStartBtn`)을 눌러야 payload 부터 받는다. 종전엔 탭
  클릭 1번에 payload + 차트 6건이 즉시 나가서, 탭을 스쳐 지나가기만 해도 무거운 계산이
  시작됐다. 기본 화면은 종전대로 **② 산포 분석**이고, 시작 전 서브탭 클릭은 선택 표시만
  바꾼다(`trimMarkSubtabs`). 분석이 시작되면 버튼은 감춰지고 이후는 종전과 동일하다.
  ② 산포 분석은 한 페이지 **6개**(`TRIM.PAGE_SIZE` = 라우트 `_TRIM_BATCH_MAX`)를
  `GET .../web_report/trim_chart_batch` **요청 1건**으로 받는다 — 서버가 tables 로드 +
  `build_groups` 를 그룹 수만큼 반복하던 것을 1회로 줄이는 것이 목적이다
  (`service._trim_chart_ctx` 1회 + 그룹별 `service._trim_chart_gzip`).
  배치 응답 `{"charts":[...]}` 는 **보낸 group 순서 그대로**이고 각 chart 는 단일
  `/trim_chart` 결과와 값이 같다. 배치 실패 시 프런트가 그룹별 단일 요청으로 폴백한다
  ([trim.js](../server/report/static/webreport/trim.js) `trimPrefetchBatch`). 매칭 규칙은
  [trim_match.py](../web_report/trim_match.py)(product_type 별 PMIC4/TV2 규칙셋).
  TV2(MDDI/PDDI) 는 이름 끝의 `_PRE[_P<n>]` → TRIM / `_POST[_P<n>]` → VERIFY 꼬리를
  **통째로** 떼어 stem 을 잡는다(`_TV2_PREPOST_RE`, 2026-07-21) — `VREF_PRE_P1` ↔
  `VREF_POST_P2` ↔ `VREF_POST_P3` 가 P 번호와 무관하게 같은 그룹(`VREF`)이 된다.
  이 꼬리는 phase 를 명시하므로 마커(`FUSE_`/`OTP_`)보다 **우선**한다(마커가 있어도
  `_POST` 는 VERIFY). 한 그룹에 POST 가 둘 이상이면 TV2 는 2-slot 이라 입력순 첫 항목만
  VERIFY 슬롯을 갖고 나머지는 members 로 남는다.

## 편집 흐름 (세션 편집 DB)
web_report 편집(comment / ETC item / trim override / Summary Engr comment)의 **진실은
세션 단위 DB**(`report_webreport_edit` + `_rev`)다. manifest 는 업로드 시점 불변 스냅샷.
- 라우트: `POST .../web_report/issue_table/{etc,comments,hidden,status}`, `.../summary/engr`,
  `.../trim/overrides` (CSRF + 편집자 가드 — [02](02_server_query_edit.md)).
- Raw Data 편집은 예외 — 편집 DB 가 아니라 parquet 원본을 재인코딩해 덮어쓰고
  `content_hash` 를 갱신한다(undo 없음). 채널 2개: 웹 셀 편집(`.../raw_data/edit`)과
  Honey Excel 왕복(`GET .../rawdata_export` → `POST .../rawdata_replace`, 아래).
  둘 다 덮어쓰기 직전 1세대 백업(`webreport_backup/<akey>/`)이 유일한 복구 수단이며,
  같은 analysis_key 를 공유하는 dedup 형제 세션의 `content_hash` 도 함께 갱신한다
  (안 하면 형제가 옛 hash 로 stale 캐시를 서빙).

#### Excel 왕복 편집 — 시트 삭제 = source 제거 (2026-07-20)
source 1개가 Excel 시트 1장이다([excel_session.py](../client/excel_edit/excel_session.py)).
- **시트↔source 매칭은 시트 이름 기준**(`match_sheets`, 대소문자·앞뒤공백 무시) — 시트
  순서를 바꿔도 원본 순서로 되돌린다. 이름이 안 맞고 개수만 같으면 위치 기반 폴백
  (이름 변경 용인). 순서 변경과 이름 변경을 **동시에** 하면 폴백이라 오귀속 위험이 남는다.
- **시트를 지우면 그 source 를 물리 제거**한다. 클라가 남긴 원본 idx 를 form 필드
  `source_indices`(JSON 오름차순)로 보내고, 서버는 그 parquet 만 저장한 뒤 **manifest 의
  sources 목록도 함께 축소**해 재저장한다 — manifest 불변 규칙의 유일한 예외(안 그러면
  idx↔parquet 대응이 어긋난다). 초과 idx 의 object_info 행·로컬 파일·S3 객체는
  `storage_gateway.save_webreport_sources` 가 정리한다(남기면 로더가 되살린다).
- 되돌릴 수 없으므로 Honey 가 업로드 **전에** 확인 다이얼로그를 띄운다(거부 시 전체 취소 —
  다시 실행하면 서버 원본을 새로 받아 원상복구). 시트 **추가**와 전량 삭제는 계속 거부하고,
  삭제하면서 남은 시트 이름을 바꾸면 매칭 불가로 재편집 루프로 돌아간다.
- 확인창은 **스크롤되는 전용 다이얼로그**([change_review_dialog.py](../client/honey_ui/change_review_dialog.py),
  2026-07-23). 종전 QMessageBox 는 수정이 많아지면 창이 화면을 넘어가 버튼이 사라졌다 —
  그래서 `build_confirm_message` 가 40줄에서 잘라야 했다. 지금은 UI 가
  `build_confirm_sections`(구조화, 상한 없음)를 받아 렌더하고 셀 상세도 source 당 200건까지
  담는다. 평문 빌더는 하위호환으로 남는다.
- **서버 부하** (2026-07-23): export 는 `ETag = content_hash` 로 304 를 지원해 Honey 가 temp
  캐시(`%TEMP%/honey_exceledit/<sid>/export_<etag>.zip`)를 재사용한다. replace 는 전량 pandas
  디코드 대신 `honeyform.validate_parquet_bytes`(스키마+메타 6행)로 검증하고, 클라가 새
  parquet 으로 만든 **Distribution pack** 을 함께 받아 저장한 뒤 프리웜을 걸어 리빌드를
  컴퓨트 워커로 넘긴다.

#### 웹 Rawdata 다운로드 — 조회 전용 2경로 (개별 CSV / 전체 zip)
위 Excel 왕복(`rawdata_export`)은 **Honey 전용 편집 경로**다. Honey 없이 원본만 받아 보는
읽기 전용 경로가 따로 있고, 세션 상세 상단 **⬇ 버튼** 하나에서 둘 다 진입한다
([edit_mode.js](../server/report/static/webreport/edit_mode.js) `rawdlDownload`, 구현은
[rawedit.py](../web_report/rawedit.py)).
- `GET .../web_report/rawdata_csv?source=<idx>` — source 1개를 7-meta honeyform CSV 로.
- `GET .../web_report/rawdata_csv_all` — 전 source 를 CSV 로 만들어 **zip 하나**로.
  source 가 2개 이상일 때만 메뉴에 뜬다(1개면 zip 이 순손해라 개별 CSV 를 바로 받는다).
- 둘 다 저장된 parquet 문자 그대로다 — 메타 6행 포함, **전처리·편집 상태 미반영**
  (Raw Data 탭 조회 API 와 같은 판단: 표시용 필터를 여기까지 적용하면 편집 대상 값과
  화면 값이 어긋난다). 가드는 `_require_web_report_session` 하나뿐이라 읽기 전용
  사용자도 받는다.
- **xlsx 다중 시트가 아닌 이유**: 서버는 openpyxl 을 쓰지 않는다(불변 규칙 #1). 브라우저
  ExcelJS 로 만드는 탭 Excel 과 달리 raw data 는 규모가 커서 브라우저에서 만들 수 없다.
- zip 은 전량을 메모리에 만들지 않고 흘려보낸다(CSV 는 parquet 대비 전개 크기가 몇 배).
  중간에 실패하면 중앙 디렉토리를 쓰지 않고 끊어 **손상 zip 으로 인지**되게 한다 —
  정상처럼 열리는데 source 가 빠진 zip 은 유실을 조용히 지나가게 하므로 만들지 않는다.

#### 조회 전처리 — Item Select / Outlier / 빠른 수정 (2026-07-23, 패치 계층 2026-07-28)
Honey 의 `Rawdata edit` 은 Excel 을 바로 띄우지 않고 **허브 다이얼로그**
([rawdata_hub_dialog.py](../client/honey_ui/rawdata_hub_dialog.py))를 먼저 연다. 레이아웃은
**좌측 기능 버튼 + 우측 활성 패널**(QStackedWidget)이다 — 페이지: `현재 상태`(적용 중인
전처리 목록 + 개별/전체 해제) / `Options`(Bin1 only) / `Item Select`(2-리스트 + 검색) /
`Outlier 제거` / `Yield 계산`(소스별 수율 분모) / `Rawdata 원본 수정`(주황 — Excel 로 원본을
고치는 유일한 버튼) + 하단 `저장`·`닫기`. `저장` 은 **화면 상태 전체**를 저장한다(행별 부분
저장은 다른 행을 되돌려야 해서 옮겨 둔 항목이 조용히 사라진다).

**서버 조회는 창을 띄운 뒤 스레드**(`_HubLoadWorker`)에서 GET 3건(preprocess /
raw_data/columns / yield_basis)을 돌린다 — 생성자에서 동기 호출하면 항목이 수천 개인 세션에서
버튼을 누른 뒤 창이 뜨기까지 UI 가 멈춘다(2026-07-28). 로드가 끝날 때까지 `저장`·Excel 진입은
비활성이다.

`Options` 페이지는 **조건을 짤 필요가 없는 옵션**을 모아 둔 곳이다 — `Bin1 only`(BIN ∉ [1] →
die 제외) 체크박스. 내부적으로는 조건 규칙(`rules`)을 만들 뿐이라 `현재 상태` 목록에 그대로
나타나고, 거기서 해제하면 체크박스도 함께 풀린다(`_sync_options` — 규칙 정규형끼리 비교해
동기화한다. 표시 문자열 비교가 아니다).

> **2026-07-28 임시 비활성(사용자 요청)**: `빠른 수정` 페이지와 `Options` 의 `Spec Out 빈값`
> 은 허브에서 **화면만 뺐다**(코드·다이얼로그·규칙 생성 함수는 그대로 — 등록/레이아웃 3줄만
> 복구하면 되살아난다). 이미 저장된 셀 패치·spec_out 규칙은 **계속 적용되고** `현재 상태`
> 에서 해제할 수 있다.

Excel 을 뺀 나머지는 전부 **원본 parquet 을 고치지 않는 되돌릴 수 있는 옵션**이다 —
Raw Data 편집과 정반대 성격이라 백업·content_hash 갱신이 없다.
- 저장소: 세션 편집 DB `kind=preprocess`, `item_key='spec'`, value JSON — 라우트
  `GET/POST .../web_report/preprocess`. 빈 spec 저장 = 해제. 키 4종:
  ```json
  {"exclude_items": ["ITEM_A"], "outlier": {"mode":"stdev","k":50},
   "edits": [{"source":"CP1","row_idx":12,"column":"VREF","value":"3.3"}],
   "rules": [{"where": {"source":"CP1", "conds":[{"field":"DUT","op":"in","values":["3"]},
                                                 {"field":"item","item":"VREF","op":">","value":4.5}]},
              "action": {"op":"clear","target":"VREF"}}]}
  ```
  적용 순서는 **① edits → ② rules → ③ exclude_items → ④ outlier**(규칙은 셀 패치가 반영된
  값 위에서 평가, outlier 통계는 규칙으로 걸러진 잔존 die 기준).
- **edits/rules 는 저장 시 "키 부재 = 유지"**, 빈 리스트 = 해제다
  ([service.save_preprocess](../web_report/service.py) `_merge_preprocess_spec`). 이 두 키를
  모르는 구버전 Honey 허브의 `저장` 한 번이 빠른 수정 결과를 조용히 지우는 것을 막는다.
  레거시 키(exclude_items/outlier/yield_basis)는 종전 replace 의미론 그대로 — 허브가
  "화면 상태를 그대로 저장"하는 계약이라 부재 = 해제여야 한다. 그래서 **빠른 수정
  다이얼로그는 저장된 레거시 키를 그대로 되돌려 보내고**, 허브는 edits/rules 를 항상 명시해
  보낸다(현재 상태 페이지에서 해제할 수 있으므로).
- 저장 시 검증(`_validate_preprocess`): tables 를 한 번 적용해 보고 없는 source/컬럼·범위 밖
  row_idx·값 규칙 위반(rawvalues)·적중 0 규칙·die 전멸을 **400** 으로 막는다. 조회 경로는
  반대로 이상한 spec 을 조용히 건너뛴다(원본이 Excel 왕복으로 줄어든 뒤 남은 패치 방어).
  상한: edits 10,000 / rules 50.
- **원본 수정이 들어오면 `edits` 만 자동 해제**된다 — 행이 지워지거나 순서가 바뀌면
  `(source, row_idx)` 가 다른 die 를 가리키기 때문. 조건 기반인 rules 와 이름 기반인
  exclude_items/outlier 는 유지된다. dedup 형제 세션까지 해제하며
  ([edits.drop_preprocess_edits_for_akey](../web_report/edits.py)), Excel 왕복
  (`rawedit.replace_sources`)·웹 셀 편집(`service.edit_raw_data`) 둘 다 같은 헬퍼를 쓴다.
  허브는 Excel 진입 전에 "셀 N건이 해제된다"고 먼저 확인받는다.
- 적용: [loader.load_tables](../web_report/loader.py) 한 곳에서 `preprocess.apply_tables` —
  그 아래 모든 탭(Summary/Yield/CPK/Issue Table/Distribution/Trim/Map)이 자동으로 같은 값을
  본다. **Raw Data 탭 조회/편집과 Excel 왕복은 `apply_prep=False`** 로 원본을 본다(제외한
  항목을 되돌릴 수 있어야 하고, 재인코딩 대상은 언제나 원본이어야 한다). 전처리된 테이블은
  `df=None` 이라 실수로 재인코딩될 수 없다.
- **항목 제외는 `item_columns` 만 줄인다** — 메타(tno/step/units/limit)와 data 프레임 컬럼은
  그대로 둔다. `manifest.selected_items` 필터와 **같은 의미론**이며, 여기서 메타까지 지우면
  Yield 의 fail 집계(`fail_counts` 는 전체 `table.tno` 기준)가 제외 항목의 fail die 를 잃어
  **표 행 합(90+5+5=100%)과 수율이 어긋난다**. 제외는 "그 항목을 분석에서 뺀다"이지
  "그 die 를 없앤다"가 아니다 → CPK·Distribution·Trim·TNO Map 에서는 사라지고 **Yield 표와
  수율은 불변**. 회귀 고정: [test_preprocess_tabs.py](../tests/test_preprocess_tabs.py).
- outlier 규칙: 항목별 `mean ± k·stdev`(ddof=1) 밖 **측정값만 결측(NaN)**. die(행)·BIN·좌표는
  불변이라 **수율·Wafer Map 은 그대로**고 CPK·Distribution 의 n·평균·σ 만 달라진다.
  σ=0(값이 모두 같음)·표본 1개 이하 항목은 대상 없음.
- **셀 패치(`edits`)·조건 규칙(`rules`)은 data 프레임의 값·행 자체를 바꾼다** — 항목 제외와
  달리 BIN·수율·Wafer Map 까지 달라진다. 조건 필드는 메타 7열 + `item`, 연산은
  `in/not_in`(fmt_type 정규형 일치) · `> >= < <=`(결측은 어느 쪽에도 미적중) · `spec_out`
  (item 전용, HILIM/LOLIM 밖). 동작은 `set/clear/offset/scale/exclude_rows`. 한 규칙 안
  `conds` 는 AND, 규칙 리스트는 **적힌 순서대로** 적용된다. dtype 은 함부로 넓히지 않는다
  (정수 컬럼에 정수만 들어오면 int64 유지 — 리포트 표기 회귀 방지).
- 조회 필터와 규칙 조건은 **같은 구조·같은 판정**을 쓴다(`preprocess.normalize_where` /
  `match_rows` 공개). 빠른 수정 다이얼로그가 화면에 띄운 "대상 N행"이 곧 저장 후 바뀌는
  행이라는 계약이다(예외: SERIAL 은 조회에서만 부분일치 — 규칙은 정확히 일치).
- 캐시: spec digest 를 tables/dist/map/scatter/trim_chart 키와 dist/map/scatter 라우트 ETag 에
  덧붙인다. **옵션이 없으면 digest 가 빈 문자열이라 키가 종전과 완전히 동일** →
  기존 세션 무회귀, 껐다 켜면 옛 캐시가 다시 히트 ([12](12_web_report_cache.md)).
  Distribution pack 은 업로드 시점(전처리 없음) 기준이라 **전처리 세션은 pack 을 쓰지 않고**
  기존 계산 경로로 폴백한다.
- 캐시(추가): `commonality_key` 에도 prep digest 가 붙는다(2026-07-28) — 셀 패치·규칙이
  Commonality 인덱스가 읽는 SERIAL/BIN·die 구성을 바꾸므로. 전처리 없는 세션은 종전 키 그대로.
- 화면 표시는 없다 — 세션 상단에 상태 배지를 두지 않는다(사용자 요청, 2026-07-23).
  현재 적용값은 Honey 의 Rawdata 허브 **현재 상태** 페이지에서 목록으로 보고 개별 해제할 수
  있다(`GET .../web_report/preprocess`).

#### 신규 Item(수식) 추가 (2026-08-24) — 원본에 파생 컬럼을 박는다

허브 좌측 `Options` **바로 밑** 페이지. 측정 항목끼리 계산한 파생 항목(예:
`IF(VREF_TRIM > MIN(VDD_A, VDD_B), 0, 1)`)을 만들어 **원본 parquet 에 컬럼 하나로 추가**한다.
반영 경로는 Excel 왕복이 쓰는 `POST .../web_report/rawdata_replace` 를 그대로 재사용하므로
백업 1세대·`content_hash` 갱신·dedup 형제 세션 동기화·프리웜(세션 재빌드)이 전부 따라온다.

- **수식 엔진**: [web_report/formula.py](../web_report/formula.py) — 순수 모듈이라 Honey 클라가
  직접 import 한다(preprocess·rawvalues·dist_pack·temperature 와 같은 계약).
  `IF MIN MAX SUM AVERAGE ABS ROUND SQRT AND OR NOT` + 비교 `> >= < <= = <>` + 사칙연산·괄호.
  gap_chart 파서의 **확장 사본**이며(gap_chart 는 한 글자도 고치지 않는다 — 거기 저장된
  토큰·에러 문구·`spec_digest` 가 운영 세션의 사용자 입력에 걸려 있다), 사본 드리프트는
  [tests/test_formula_item.py](../tests/test_formula_item.py) 의 **동치 테스트**가 막는다.
- **입력은 줄글이다** (2026-08-25 개편 — 종전 연산자·함수 버튼 22개 조립 방식을 대체).
  사용자가 `if(@"VREF_TRIM" > min(@"VDD_A", @"VDD_B"), 0, 1)` 처럼 그대로 치면
  [formula.lex](../web_report/formula.py) 가 토큰으로 해독한다. **항목은 `@"이름"` 인용으로만**
  들어오고(`@` 자동완성이 자동 삽입한다 — 이름 안의 `"` 는 `""` 로 escape) 인용 밖에서는
  숫자·연산자·괄호·함수명만 받는다. item 이름에 공백·괄호·연산자·따옴표가 전부 합법이라
  글자만 보고 `VDD-VSS + 1` 을 가릴 수 없기 때문이며, 명시적 구분자가 그 모호성과
  `SUM(...)`=함수 / `@"SUM"`=항목 충돌을 **함께** 없앤다. 함수명·항목 조회는 **대소문자
  무관**이되 토큰에는 목록의 **원본 이름**이 들어간다(소문자로 눕히면 source 별 참조가
  어긋나거나 엉뚱한 컬럼을 덮어쓴다). `!=`·`==` 는 별칭으로 받지 않고 `<>`·`=` 를 쓰라고
  안내한다 — 문법 표면을 넓히면 "어떤 건 되고 어떤 건 안 되는" 상태가 된다. `lex` 는 토큰마다
  문자 오프셋 span 을 함께 돌려주고 파서 오류의 토큰 index 는 `error_span` 이 문자 위치로
  되돌린다 — **위치 없는 오류를 만들지 않는다**.
- **계산은 클라(Honey)에서** 한다. 서버가 parquet 전량을 디코드·계산·재인코딩하면 대형
  세션에서 웹 프로세스가 통째로 묶인다(빠른 수정을 웹이 아니라 Honey 에 둔 것과 같은 판단).
- **모든 source 에 각각 계산**한다. 참조 항목이 없는 source 는 **그 source 만 건너뛰고
  원본 parquet bytes 를 그대로 되올린다**(재인코딩조차 하지 않으므로 값이 한 비트도 안 바뀐다).
  전부 NaN 컬럼을 만들지 않는 이유: `empty_items` 제외 로직에 걸려 "왜 이 source 만 값이
  없나"를 두 번 설명해야 한다. 건너뛴 source 는 미리보기와 확인창에 명시된다.
- **수식을 저장하지 않는다**(사용자 확정). 추가되고 나면 일반 item 과 구별되지 않는다 —
  잘못 만들었으면 `Rawdata 원본 수정`(Excel)에서 그 열을 지운다. 대신 **미리보기를 통과해야만**
  [원본에 추가] 가 열리고, 수식·메타를 고치면 즉시 다시 잠긴다(보지 않은 값이 원본에 박히는
  것을 막는다).

**평가 규약 — 모든 중간값은 float64, 진리값은 1.0/0.0/NaN(3-값 논리).** 이 셋이 이 기능에서
가장 조용히 틀리는 지점이다:
- **비교의 NaN 은 FALSE 가 아니다.** numpy 는 NaN 비교를 False 로 주므로 마스크
  (`isnan(l) | isnan(r)` → NaN)를 명시하지 않으면 **미측정 die 가 FALSE 로 뭉개져**
  `IF(A > B, 0, 1)` 이 거기에 1 을 찍는다.
- **±inf 는 마지막에 NaN 으로 정규화**한다. `encode_honeyform_parquet` 이 값을 문자열로
  저장하므로 inf 가 `"inf"` 로 parquet 에 박히고, 조회 때 `to_numeric` 이 되살려 평균·σ·CPK 를
  통째로 오염시킨다.
- **`AND`/`OR` 는 `np.minimum`/`np.maximum`** 으로 구현한다(NaN 전파). `logical_and` 를 쓰면
  NaN 이 True 로 뭉개진다. `MIN`/`MAX`/`SUM`/`AVERAGE` 도 인자들끼리 **원소별**이며 결측을
  전파한다(`fmin` 아님) — 참조 항목 하나가 미측정인 die 를 부분 모집단으로 계산하면 조용히
  틀린다. 그 die 는 미리보기의 **계산 실패**에 잡히고 셀은 빈칸이 된다.
- 비교는 **비연관**이다(`A > B > C` 거부 → `AND(A>B, B>C)` 안내). Excel 의 좌결합 + TRUE 승격을
  흉내내려면 "TRUE 는 모든 숫자보다 크다" 같은 Excel 고유 서수까지 따라가야 한다.
- `ROUND` 의 자릿수는 **파스 타임 상수**로 강제한다(배열 자릿수는 행마다 `np.round` 를 돈다).

**메타 7칸**(ITEMNAME/TSEQ/TNO/STEP/UNIT/HILIM/LOLIM):
- 기본값은 **탭을 처음 열 때 받은 rawdata** 에서 채운다 — TSEQ/TNO 는 **전 source 최대 +1**,
  STEP 은 첫 source 마지막 항목 승계, 나머지는 빈칸. 서버 `raw_data/columns` 응답에 필드를
  넣지 않은 이유는 그게 `web_report/tabs/` 라 perf_guard `S01` 이 `REPORT_SCHEMA_VERSION`
  bump 를 요구하고, 그 bump 가 전 세션 콜드 폭풍이기 때문이다. `rawdata_export` 는
  ETag=content_hash 로 304 캐시되므로 두 번째부터는 사실상 즉시다.
- 마지막 항목의 TSEQ/TNO 가 숫자가 아니면 `+1` 을 만들 수 없다 → **빈칸으로 두고** 직접
  입력받는다(추측하면 기존 항목과 부딪힌다).
- **TSEQ/TNO/STEP 은 전 source 공통 값 하나**다. source 별로 다르면
  [yield_tab](../web_report/tabs/yield_tab.py) `tno_to_item_map`(TNO 키, TSEQ 앞선 것만 생존) ·
  [tabs/common](../web_report/tabs/common.py) `item_meta`(`setdefault` — 첫 테이블 값) ·
  [distribution](../web_report/tabs/distribution.py) `scatter_item`(표시 TNO 는 대표 테이블,
  fail 판정은 source 별)이 서로 다른 기준을 잡아 **fail 귀속과 표시 TNO 가 조용히 갈린다**.
- **TNO 는 전역 유일해야 한다.** 기존 TNO 를 재사용하면 `tno_to_item_map` 이 그 TNO 를 쓰는
  항목 중 TSEQ 가 앞선 하나만 남겨 **기존 항목의 Fail 집계가 통째로 사라진다**(에러 없이).
  거부 메시지에 그 항목 이름과 결과를 함께 적는다.
- 값 표기 판정은 [rawvalues.parse_number](../web_report/rawvalues.py) 를 쓴다(사본 금지) —
  `float()` 은 `1_000` 을 통과시키는데 메타 값은 parquet 에 **문자열 그대로** 저장되므로
  조회 때 `to_numeric` 이 NaN 으로 떨궈 **규격이 조용히 사라진다**.
- ITEMNAME 은 기존 항목·메타 7컬럼과 겹칠 수 없다. UI 검증에 더해 **적용 직전에도 다시**
  본다 — pandas 의 `df[name] = ...` 는 겹치는 이름에 새 컬럼을 만들지 않고 **기존 컬럼을
  조용히 덮어쓴다**(BIN 을 덮으면 수율·Wafer Map 이 통째로 바뀌는데 parquet 은 유효하다).

**BIN·FAILTNO 는 바뀌지 않는다.** 신규 항목의 limit 위반이 die 판정을 바꾸지 않으므로 Yield
표·수율·Wafer Map 은 불변이고, 새 항목은 CPK·Distribution·Trim·Raw Data 에만 나타난다
(limit 을 비우면 CPK 도 없이 분포만). 미리보기와 확인창에 이 문장을 띄운다.

**서버 반영** ([rawedit.replace_sources](../web_report/rawedit.py), form 필드 2개 추가):
- `add_items` — `manifest.selected_items` 가 **비어 있지 않을 때만** 신규 이름을 덧붙인다
  (manifest 불변 규칙의 **두 번째 예외** — CLAUDE.md §5-6). 안 하면 parquet 에는 컬럼이
  있는데 리포트 어디에도 안 보인다(8곳이 `selected_items` 로 `item_columns` 를 거른다).
  비어 있으면 갱신하지 않는다 — 빈 값 = 전 항목 선택이라 한 개짜리 목록을 만들면 오히려
  나머지가 전부 사라진다. 업로드된 parquet 에 없는 이름은 400 으로 거부한다.
- `rows_preserved="1"` — **전처리 셀 패치(`edits`)를 지우지 않는다.** Excel 왕복이 그것을
  해제하는 근거는 "행이 지워지거나 순서가 바뀌면 `(source,row_idx)` 가 다른 die 를 가리킨다"
  인데, 열만 붙이는 이 경로는 그 전제가 성립하지 않는다. 지우면 사용자가 빠른 수정으로 넣어
  둔 값이 소리 없이 사라진다(CLAUDE.md §5-12).
- 감사로그는 `raw_data(add_item, +<이름>, backup=...)` — Excel 왕복(`raw_data(excel, ...)`)과
  구분돼야 나중에 "이 컬럼이 어디서 왔나"를 감사로그만으로 답할 수 있다.
- **Distribution pack 은 갱신된 `selected_items` 로 다시 만든다.**
  [service.get_distribution_batch](../web_report/service.py) 는 pack 을 그대로 믿고 없는 항목은
  없는 채로 응답하므로, 옛 manifest 로 pack 을 만들면 갤러리에서 **그 카드만 조용히 빈다**.

**UI**: [rawdata_hub_dialog.py](../client/honey_ui/rawdata_hub_dialog.py) 페이지 +
[formula_editor.py](../client/honey_ui/formula_editor.py)(자유 타이핑 + `@` 인용 자동완성 + 문법 강조 + IME 가드) +
[excel_edit/item_add.py](../client/excel_edit/item_add.py)(왕복) +
`excel_edit/worker.py` `AddItemWorker`. 상세는 [05](05_client_ui.md).
회귀 고정: `tests/test_formula_item.py` · `test_new_item_roundtrip.py` ·
`test_rawedit_add_item.py` · `test_new_item_dialog.py`.

#### 빠른 수정 다이얼로그 (2026-07-28) — **현재 허브에서 진입 비활성**
[rawdata_quick_dialog.py](../client/honey_ui/rawdata_quick_dialog.py) — Excel 없이 표·조건으로
고치는 화면. **웹이 아니라 Honey UI 에 둔 이유**: 서버 부하를 늘리지 않기 위해서다. 원본이
불변이라 `content_hash` 가 그대로고, 그래서 `rawdata_export` 의 ETag 캐시가 계속 유효해
**두 번째부터는 서버가 304 만 응답한다**(전 source 를 메모리에 올려 zip 으로 싸는 작업 소멸).
저장도 작은 JSON POST 1회다.

흐름: ① source 체크 선택 → 체크한 것만 디코드
([excel_session.fetch_rawdata_tables](../client/excel_edit/excel_session.py), Excel 왕복과 같은
zip·같은 ETag 캐시) → ② 필터 조회 → 표에서 셀 수정 / 선택 영역 값 지정·빈값·오프셋·배율 /
클립보드 TSV 붙여넣기 / 찾아 바꾸기 / 조건 일괄 규칙(대상 건수 확인 후 추가) →
③ 수율·worst CPK **미리보기**(저장된 상태 → 저장하면 될 상태) → ④ 저장.

- 값 검증·조건 판정·규칙 적용·미리보기 통계 전부 **서버와 같은 모듈**(`rawvalues`,
  `preprocess`, `tabs.cpk`, `tabs.common`)을 그대로 돌린다 — 값 일치를 구조적으로 보장.
- 무거운 작업(다운로드·디코드, 미리보기 CPK 계산)은 QThread 로 뺀다(honey_ui freeze 규칙).
- 표 상한은 웹 Raw Data 와 같은 값(행 20,000 / item 컬럼 60).
- 화면이 빽빽해지기 쉬운 창이라 **조회 조건·수정·적용 대기·미리보기를 접이식 구역**
  (`_Section`)으로 두고, 접으면 제목 옆에 요약(대상 행수 / 대기 중인 셀·규칙 수)만 남긴다.
  항목 목록은 우측 패널에서 최소 340px 폭·세로 대부분을 갖는다. 창 전체 폰트는 11px.
- 조건을 짤 필요 없는 Bin1 only 는 여기가 아니라 **허브 [Options]** 에 있다.
- 진입점(허브 `빠른 수정` 페이지)은 2026-07-28 사용자 요청으로 **잠시 비활성**이다. 코드는
  그대로라 허브 생성자의 등록 3줄 주석을 풀면 되살아난다.

#### Raw Data 값 검증 (2026-07-21)
정본은 [rawvalues.py](../web_report/rawvalues.py). **값 규칙을 `validate_honeyform_df` 에
넣지 말 것** — 그 함수는 `_decode_parts` 에서 저장된 parquet 을 **읽을 때마다** 실행되므로
값 규칙을 넣으면 기존 세션이 열리지 않는다. 그래서 값 검증은 별도 순수 모듈에만 둔다
(pandas 무의존 셀 함수 + 지연 import 하는 프레임 함수, 클라 excel_session 이 공유).

| 컬럼 | 규칙 | 위반 시 |
|------|------|---------|
| item(측정값) | 숫자 또는 빈값(=결측). `nan`/`inf`/`0x10` 등은 거부 | 웹=400, Excel=경고 |
| BIN | 정수, 빈값 금지. `01`/`1.0`/` 1 ` → `1` 로 정규화 | 〃 |
| SHOT·DUT·XPOS·YPOS·FAILTNO | 정수(음수 허용), 빈값 허용 | 〃 |
| SERIAL | 자유 문자열(선행 0 보존), 빈값·개행·200자 초과 금지 | 〃 |

- **채널별 정책이 다르다.** 웹은 편집한 셀이 특정되므로 **하드 거부**(400, 위반 위치를
  한국어로 나열, 한 셀도 쓰지 않음)하고, Excel 은 자유 편집 도구라 셀 단위로 막지 않고
  **자동 교정 + 확인창 경고**로 처리한다. 검증 대상은 **편집한 셀/변경된 부분뿐** —
  업로드 당시 통과한 기존 데이터를 소급 거부하지 않는다(ingest 정책도 그대로).
- **판정은 프런트와 서버가 반드시 같아야 한다.** 규칙 테이블·문안은 서버가 단일 진실이고
  (`/raw_data/columns` 응답의 `value_rules`), [raw_data.js](../server/report/static/webreport/raw_data.js)
  는 판정 프리미티브만 복제한다. 파이썬 `float()` 은 `1_000`·전각숫자·`infinity` 를,
  JS `Number()` 는 `0x10`·`0b101` 을 받아들여 판정이 갈리므로 **양쪽 모두 동일한 정규식**
  (`_NUM_RE` ↔ `RAW_NUM_RE`, `\d` 가 아니라 `[0-9]`)으로 표기를 먼저 좁힌다. 이 둘을
  고칠 땐 반드시 같이 고칠 것(`tests/test_rawvalues_cell.py` 의 NUMERIC_TRAPS 가 고정).
- Excel 채널이 **조용히 교정**하는 것(확인창에 보고): used_range 확장으로 들어온 유령 행/
  무명 빈 컬럼 제거(안 지우면 유효 die 로 저장돼 수율이 희석된다), 메타 컬럼명 대소문자
  복원, 정수 dtype 복원(xlwings 가 숫자를 전부 float 로 돌려줘 편집 안 한 int 컬럼까지
  `1`→`1.0` 이 된다 — **원본 dtype 기준**으로만 되돌린다).
- Excel 채널이 **경고만** 하는 것: LOLIM>HILIM(규격내 CPK 미계산 → Issue Table 에서 항목이
  사라짐), TNO 빈값·0·중복(fail 이 Yield 표에 집계 안 됨), 비수치 측정값, BIN 비정수,
  SERIAL 빈값, XY 비좌표, item 컬럼명 변경·추가. 셀 단위 diff 도 함께 보여준다
  (형태가 같고 `EXCEL_SCAN_CELL_BUDGET` 이내일 때만 — 초과 시 생략을 **명시 보고**).
  - diff 는 **구조화 행**(`inspect_edited_frame` 의 `cell_rows` — 위치/항목/이전/이후가 열로
    분리)이 정본이고, 확인창이 그걸 표로 그린다(→[05](05_client_ui.md) `ChangeReviewDialog`).
    같은 행에서 파생시킨 평문 `cells` 는 구 평문 빌더·전문 저장용이라 `_CELL_TEXT_LIMIT`
    (200)에서 끊는다. 행 상한은 호출부(`excel_session._CELL_DETAIL_LIMIT` = 50,000)가 정하고,
    걸리면 `cell_total > len(cell_rows)` 로 드러나 확인창이 "외 N건"을 띄운다 —
    **침묵 잘림 금지**(1,500건 수정에서 200건만 보이던 것이 원래 문제였다).
- 메타 컬럼명은 encode/decode/split 모두에서 canonical 대문자로 정규화한다
  ([honeyform.canonicalize_meta_columns](../web_report/honeyform.py)). 없으면 `BIN`→`Bin`
  케이스 변경이 검증을 통과해 저장된 뒤 조회만 `data["BIN"]` KeyError→500 이 난다
  ("저장은 됐는데 세션이 안 열리는" 상태). decode 에도 넣어 **이미 오염된 parquet 도
  마이그레이션 없이 구제**한다. item 컬럼명이 메타 컬럼명과 겹치는 것은 구조 검증
  (`validate_honeyform_df`)에서 거부한다 — 그런 파일은 지금도 컬럼이 밀려 깨지므로 회귀가 아니다.
- `kind` 16종: `issue_comment` / `etc_item` / `cmp_etc_item` / `trim_override` / `summary_engr` /
  `chart_note` / `note_sheet` / `note_tag` / `compare_note` / `dist_composite` / `gap_chart` /
  `issue_hidden` / `issue_status` / `issue_signature` / `preprocess` / `yield_basis`
  ([edits.py](../web_report/edits.py) 규약 — 정본은 그 파일의 KIND_* 상수 주석).
  편집마다 `rev` 가
  단조 증가해 캐시가 자연 무효화된다([12](12_web_report_cache.md)). dedup(동일 analysis_key)
  세션 간 편집 비공유. legacy 세션(rev==0)은 조회 시 manifest 폴백 + 첫 편집 직전 자동 시드
  (manifest 에 있던 4종 — issue_comment/etc_item/trim_override/summary_engr — 만 시드 대상,
  나머지는 신규 kind 라 해당 없음). 세션 단위 저장이라 rawdata 수정 → 재업로드(새 세션) 시
  숨김/Status 는 자연 리셋된다.
- **payload 중립 kind**: `chart_note` / `note_sheet` / `note_tag` / `dist_composite` /
  `gap_chart` 는
  report payload 계산에 안 들어가므로 저장해도 `payload_rev` 가 오르지 않는다
  ([webreport_edits.py](../server/database/webreport_edits.py) `PAYLOAD_NEUTRAL_KINDS`).
  새 kind 를 만들 때 **여기에 넣는 것을 빠뜨리면 저장할 때마다 report 전체가 콜드 재빌드**
  된다(2026-08-13 조회 급락 사건과 같은 기전). `/full` 응답 캐시는 전역 `rev` + extras
  digest 로 정상 무효화되므로 화면은 즉시 갱신된다.
  ⚠️ 이 목록(5종)은 [edits.py](../web_report/edits.py) `_STATE_EXCLUDED_KINDS`(8종)와
  **같지 않다** — `preprocess`·`yield_basis`·`compare_note` 는 편집 state dict 에서만 빠지고
  payload_rev 는 올린다(실제로 payload 를 바꾸므로 올려야 맞다). 두 목록을 기계적으로 함께
  채우지 말 것 — 판단 기준은 [CLAUDE.md](../CLAUDE.md) §5 규칙 16.
- **`issue_status` 는 `"Close"` 만 저장한다** — 값이 없으면 Open 이다(기본값을 행마다 쓰지
  않아 저장량이 행 수에 비례하지 않는다). 그래서 "Open 으로 되돌리기" 는 값 갱신이 아니라
  **행 삭제**이고, 조회 코드가 `get(key) == "Open"` 을 기대하면 아무 행도 못 찾는다.

### 차트 주석 (chart_note — 2026-07-12)
그래프 위 동그라미/사각형/선/텍스트 + 코멘트. Plotly 내장 draw(dragmode drawcircle 등,
vendored v3.5) 사용, 프런트는 [chart_notes.js](../server/report/static/webreport/chart_notes.js).
- item_key = **chart_key**: `cdf:<subject>` / `hist:<subject>` (예약: `trim:`/`map:`/`gap:`/
  `overlay:`). value = JSON `{shapes, texts, comment}` — 서버가 허용 키만 통과시키고
  텍스트의 `<` `>` 를 제거한다 (`service._sanitize_chart_note`, 도형 ≤40·comment ≤2000자·16KB).
- 저장 `POST .../web_report/chart_notes` (ops 배열, null=삭제). 조회는 `/full` extras 의
  `chart_notes` (값싼 kind 지정 SELECT — [routes_session.py](../server/report/routes_session.py)).
- LSL/USL 스펙 점선도 `layout.shapes` 이므로 프런트는 렌더 시점 개수(base)를 기억해
  사용자 도형만 base 뒤에 붙인다. 표시는 전원, 편집은 편집 권한자만.

### Note 탭 (note_sheet — 2026-07-12)
탭 전체가 **Luckysheet 시트 캔버스** (vendored 2.1.13, MIT — `server/report/vendor/luckysheet/`).
엑셀 정리 워크플로 대체: 차트 PNG 붙여넣기(플로팅 이미지), 셀 수식/서식, 엑셀 range 붙여넣기.
셀 계산은 전부 브라우저 — 서버는 시트 JSON 저장만. 프런트 [note.js](../server/report/static/webreport/note.js).
- 시트 저장: kind `note_sheet`(item_key=`"sheet"`, 전체 치환, **≤10MB**). `/full` 에는
  `note_info`(존재/최종수정 메타)만 — 본문은 lazy `GET·POST .../web_report/note`.
  `load_edit_state` 는 이 kind 를 **제외 조회**해 comment 저장·콜드 빌드가 이 블롭을
  끌어오지 않는다 (`get_webreport_edits(kinds/exclude_kinds)`).
- ⚠️ **이미지가 들어오는 경로는 2개이고 저장 위치가 다르다** (헷갈리기 쉬운 지점):
  ① **차트 반입**(아래) 만 서버 업로드 → 시트 JSON 에는 **URL 문자열**만 남는다.
  ② **Luckysheet 자체 삽입**(툴바 이미지 업로드 · 캔버스 드래그&드롭 · Ctrl+V 스크린샷)은
  번들 내부에서 `FileReader.readAsDataURL` → `inserImg(dataURI)` 라 **base64 가 시트 JSON
  안에 통째로 박힌다**(원본 대비 +33%). [note_frame.html](../server/report/note_frame.html)
  에 이 경로를 가로채는 훅은 없다. 그래서 ≤10MB 상한은 사실상 **②의 이미지 예산**이다
  (1MB 캡처 ≈ 5장). 상한이 부담되면 ②도 ①처럼 업로드→URL 치환으로 바꾸는 것이 근본 해결이고,
  기존 세션은 시트 JSON 의 data URI 를 업로드 후 src 만 바꾸는 배치로 이전할 수 있다.
- 이미지 업로드(①): `POST .../web_report/note_image` (PNG/JPEG 매직바이트, ≤2MB, 세션당 200장) →
  S3(`pe/report_server/note_img/<sid>/`)+로컬 폴백
  ([_note_images.py](../server/storage_gateway/_note_images.py) — **세션 단위** 네임스페이스,
  dedup 세션 간 누출 방지). 서빙 `GET /pe/report/note_image/<sid>/<id>` (nosniff).
  세션 삭제 시 항상 일괄 정리 (akey 공유 여부 무관).
- 차트 반입: 항목 상세의 [📋 Note에 붙여넣기] → `Plotly.toImage`(주석 포함) → note_image
  업로드 → `luckysheet.insertImage`. Luckysheet 번들(≈4MB)은 Note 탭 첫 진입 시 지연 로드,
  vendor 서빙은 `routes_misc.py` 의 luckysheet/ 경로 정규식 + 확장자 mime.

### 코멘트 태그 `@` `#` `$` (2026-08-06 확장)
코멘트 본문에 **평문 토큰**으로 저장하고, 표시할 때만 링크로 바꾼다. 변환은
`linkifyComment`([sheets.js](../server/report/static/webreport/sheets.js)) 하나가 정본이고,
입력 자동완성은 `showMention`/`mentionInsert`([edit_mode.js](../server/report/static/webreport/edit_mode.js))
가 담당한다. 트리거 문자 집합은 `TRIGGER_RE`/`TRIGGER_TAIL_RE`(edit_mode.js) 와
`linkifyComment` 의 정규식이 **짝**이라 늘릴 땐 둘 다 고쳐야 한다.

| 토큰 | 후보 출처 | 클릭 동작 | `.missing` 판정 |
|---|---|---|---|
| `@[항목명]` | `distIndex`·`rawDataMeta`·`etcItemMeta` (측정 항목) | Item_detail 열기 | 없음 |
| `#[태그명]` | `DATA.note_tags` (Note 앵커 태그) | Note 탭 + 그 **셀**로 점프 | 태그 없으면 표시 |
| `$[시트명]` | Note 시트 이름 목록 | Note 탭 + 그 **시트**로 점프 | 목록 도착 후 이름 없으면 표시 |

- **쓸 수 있는 자리 2곳**: Issue Table 의 comment 열과 Summary 탭 **Engr Comment** — 둘 다
  `contenteditable` 이라 `mentionQueryAtCaret`/`mentionInsert` 는 `Selection` + Text 노드
  한 경로만 쓰고, 대상 판별은 `tagFieldOf()` 하나로 모은다.
- **Engr Comment 는 contenteditable 이다** (2026-08-13 — 글자 크기·색 편집 도입으로 종전
  textarea 에서 전환). ⚠️ **값을 읽을 때 `innerText` 를 쓰면 안 된다** — 패널이 `display:none`
  인 상태(탭 전환 뒤 자동저장)에서 줄바꿈을 통째로 잃는다(headless 실측 확인). 그래서
  `engrEditorValue`/`engrTextOf`([map_select.js](../server/report/static/webreport/map_select.js))는
  렌더와 무관한 **DOM 순회**로 `<br>`·`<div>` 를 `\n` 으로 되돌린다. 링크는 종전대로 입력칸
  **아래 칩 줄**(`engrLinkChips`)에 띄우고, 조회 모드는 본문을 `engrValueHtml` 로 렌더한다.
  저장 경로(`POST .../summary/engr`, kind=`summary_engr`)는 종전 그대로다.
- **Note 시트 이름 목록**: `GET .../web_report/note/sheet_names` → `{"sheets":[{index,name,order}]}`.
  이름만 필요한 화면이 본문까지 내려주는 lazy `GET .../note`(≤10MB)를 부르지 않게 만든 경량
  라우트다. 서버는 `updated_at` 을 키로 memo 하고, 클라(note.js `noteEnsureSheetList`)는 Note
  탭 fetch·저장 때 손에 쥔 시트 배열로 공짜로 채운다. `DATA.note_info.exists` 가 false 면
  요청 자체를 하지 않는다.
- Summary 의 Engr Comment 카드 아래 **Note 시트 버튼 줄**(`renderEngrNoteJump`)도 같은 목록을
  쓰고, 클릭은 `$` 링크와 같은 `noteJumpToSheet`([note.js](../server/report/static/webreport/note.js))
  로 간다 — `noteJumpToTag` 과 동일한 pending 큐 패턴이라 Note 탭이 아직 init 전이어도 안전하다.
  시트 index 를 몰라도 `note_frame.html` 의 `gotoCell` 이 `sheetName` 으로 폴백 매칭한다.

### 코멘트 서식 토큰 `*[..]` — 색·굵기 (2026-08-07)
PTE/개발 comment 안에서 **특정 글자만** 색·굵기로 강조한다. 위 `@#$` 링크 토큰과 같은
"평문 저장 + 표시 시점 변환" 구조라 **DB 스키마·저장 API·캐시는 무변경**이다.

| 토큰 | 의미 | 토큰 | 의미 |
|---|---|---|---|
| `*[텍스트]` | 굵게 | | |
| `*r[텍스트]` | 빨강 | `*R[텍스트]` | 빨강 + 굵게 |
| `*o[텍스트]` | 주황 | `*O[텍스트]` | 주황 + 굵게 |
| `*g[텍스트]` | 초록 | `*G[텍스트]` | 초록 + 굵게 |
| `*b[텍스트]` | 파랑 | `*B[텍스트]` | 파랑 + 굵게 |

- 굵기는 **"글자 없음" 또는 "대문자"로만** 표현한다 → `b` 는 bold 가 아니라 **blue** 다.
- 스타일 글자가 `r/o/g/b` 가 아니면 **토큰이 아니다**(`*x[..]` = 평문). 기존 코멘트의
  곱셈·각주 `*` 가 서식으로 오인되는 것을 막는 방어다(도입 시 운영 DB 오탐 실측 0건).
- **중첩 불가** — 정규식이 `[^\]]+` 라 `*[a @[item] b]` 나 `*[REG[7:0]]` 는 표현할 수 없다.
  툴바가 "선택 구간에 `[`/`]` 가 있거나 기존 토큰과 겹치면" 아예 뜨지 않게 막는다.
- 입력은 **플로팅 툴바 + 단축키**: 편집 중(더블클릭) 셀에서 글자를 드래그하면
  셀 위에 버튼이 뜬다(`selectionchange` → `_cmtFmtBarEl`, edit_mode.js). 단축키는
  `Ctrl+B`(굵게) / `Ctrl+Shift+1~4`(빨강·주황·초록·파랑) / `Ctrl+Shift+0`(제거).
  `Ctrl+B/I/U` 는 `preventDefault` 로 가로챈다 — contenteditable 기본 동작이 `<b>` 를
  삽입하는데 저장은 `textContent` 라 조용히 사라지기 때문이다.
- **색·굵기는 웹 화면 전용이다.** Excel·챗봇·eval DB 로 나갈 때는 표시문자를 벗기고
  본문만 보낸다. strip 은 JS `stripCommentFormat`(sheets.js)과 Python
  `strip_format`([comment_format.py](../web_report/comment_format.py)) 두 짝이고,
  호출 지점은 5곳뿐이다:

  | 소비처 | 지점 |
  |---|---|
  | eval.db 관문 | [eval_export.py](../web_report/eval_export.py) `_merge_comment` — 여기 하나로 챗봇 코멘트 검색·AI Comment 선례 인용·관리자 패널·CSV 가 전부 커버된다 |
  | 챗봇 report.db 직독 | [chatbot/tools_report.py](../server/chatbot/tools_report.py) (eval_export 를 우회하는 유일한 경로) |
  | 웹 Excel Down | [excel_export.js](../server/report/static/webreport/excel_export.js) |
  | Honey Excel Download (COM 엔진) | [client/excel_download/_sheets.py](../client/excel_download/_sheets.py) |
  | Honey Excel Download (XlsxWriter 엔진, 2026-08-14) | [client/excel_download/_extra.py](../client/excel_download/_extra.py) — Issue comment(`build_issue_matrix`) + Summary Engr Comment(`engr_plain`) |

  **저장 경로에서는 절대 벗기지 않는다** — 원문이 정본이다. `_COMMENT_MAX_LEN`(2000자)
  검사도 마크업 포함 길이 그대로다(서식 1개당 3~4자 오버헤드).
- 회귀 고정: [tests/test_comment_format.py](../tests/test_comment_format.py) — strip 표·멱등성·
  `_merge_comment` 관문 + **JS↔Python 문법 드리프트 가드**(sheets.js 정규식·색 테이블 대조).

### Summary Engr Comment 서식 — 글자 크기·색 (2026-08-13)
Engr Comment 4칸(Yield/CPK/TEMP/ETC)은 위 `*[..]` 토큰이 아니라 **WYSIWYG 편집**이다
(도구모음: 크기 12/14/18/24px · 색 5종 · 굵게/기울임/밑줄 · 서식 지우기 —
[map_select.js](../server/report/static/webreport/map_select.js) `engrFmtBarHtml`).
⚠️ **굵게와 기울임·밑줄은 `styleWithCSS` 값이 반대다**(`engrRunCmd`/`ENGR_TAG_CMDS`).
굵게는 `true`(→`span[font-weight:bold]`, 허용 style)이고, 기울임·밑줄은 `false` 여야
`<i>`/`<u>` 태그로 남는다 — `true` 로 두면 브라우저가 `font-style`/`text-decoration`
span 을 만드는데 그 둘은 허용 style 목록에 없어 저장 직전 `engrSanitize` 가 지운다
(= 사용자가 건 서식이 조용히 사라진다).
Issue comment 와 문법이 다른 이유는 소비처가 다르기 때문 — Engr 값은 웹 화면 밖으로 나가는
경로(Excel·eval·챗봇)가 **없어서** strip 짝이 필요 없다.

- **저장값은 문자열 1개 그대로**다(DB 스키마·API·캐시 무변경). 서식이 붙은 값만 선두에
  마커 `<!--rich-->` + 제한 HTML 이고, 마커가 없으면 예전 그대로 **평문**이다 → 기존 세션
  값은 손대지 않아도 그대로 보인다. 서식을 안 쓴 편집 결과는 다시 평문으로 되돌려 저장한다.
- **허용 태그/스타일 화이트리스트**: `span/b/strong/i/em/u/br/div/p` + style 은
  `color`·`background-color`(#hex·rgb())·`font-size`(Npx)·`font-weight` 만. `script/style/
  iframe/object/embed` 는 **내용까지** 버리고, 그 외 모르는 태그는 껍데기만 버리고 글자는 살린다.
- **필터는 저장할 때와 그릴 때 양쪽에서 돈다**(`engrSanitize`). 남의 브라우저에 그려지는
  값이라 렌더 쪽이 실제 방어선이다 — 조회 경로에서 이 호출을 빼지 말 것.
  서버([service.py](../web_report/service.py) `update_summary_engr`)는 얕은 방어로 실행 가능
  태그만 거부하고 나머지는 해석하지 않는다. 상한은 Issue comment(2000자)와 분리된
  `_ENGR_MAX_LEN`(8000자) — 태그 오버헤드로 정상 입력이 저장 거부되면 안 되기 때문(§5-12).
- 화면 표기는 크기·색뿐이므로 **세로 스크롤을 만들지 않는다** — 내용이 길어지면 칸 자체가
  늘어난다(`.engr-comment-input`/`.engr-comment-view` 에 `max-height`·`overflow-y` 금지).

## 렌더 구조 (report_view.html + static/webreport)
- 마크업+CSS 는 [report_view.html](../server/report/report_view.html), 탭별 JS 는
  [static/webreport/](../server/report/static/webreport/) — 파일 **32개** 중 세션 상세가
  **31개**를 로드한다(`old_client_notice.js` 만 랜딩·검색결과 전용).
- **classic script 순서 로드(전역 스코프 공유)** — ES module 로 바꾸거나 로드 순서를 바꾸지
  말 것. 정적 서빙은 `GET /pe/report/static/webreport/<filename>`(화이트리스트).

### 로드 순서 (report_view.html 하단이 정본)

| # | 모듈 | 역할 |
|--:|------|------|
| 1 | `error_beacon.js` | 브라우저 JS 에러/rejection → `POST /api/client_error` |
| 2 | `honey_hint.js` | Honey 전용 기능 안내(일반 브라우저에서만 노출) |
| 3 | `user_name.js` | 사용자 실명 표기·입력창 (3페이지 공유, `UserName.uid()` 정규화) |
| 4 | `core.js` | SESSION_ID·MODE·YIELD_COLS·표 골격·loadAuth 등 기반 |
| 5 | `tabs_topbar.js` | 탭 전환 + 탭별 대용량 지연 데이터 트리거 |
| 6 | `sheets.js` | grid model 렌더 + `issueRowKey`/`linkifyComment` |
| 7 | `yield_issue.js` | Yield STEP별 Bin 접기 표 + Issue 표 렌더 본체 |
| 8 | `sig_reason.js` | Signature 판정 근거 팝업(`?` 버튼) |
| 9 | `wafer_charts.js` | wafer map + fail-bin 차트(Plotly) |
| 10 | `stdf_map.js` | STDF Map(값 기반 웨이퍼 맵 서브모드) |
| 11 | `compare.js` | Compare 서브패널 빌더(map/log/equiv) + 서브탭 전환(`showCmpSub`) + 표 헬퍼·compare_note 바인딩 |
| 12 | `compare_issue.js` | Issue Table Compare **탭 진입점** + ISSUE_TABLE 서브패널 — **11·7 재사용이라 그 뒤** |
| 13 | `map_select.js` | Map 좌표 선택(chip)·`mapSelMarkerTraces`·Summary 카드 |
| 14 | `cpk.js` | CPK 탭 |
| 15 | `distribution.js` | Distribution 갤러리·툴바 (`DIST` 상수·`DIST_VARIANTS`) |
| 16 | `item_detail.js` | Item_detail(CDF+히스토그램 상세) |
| 17 | `dist_composite.js` | 합성 산포 차트 — **15·16 재사용, `edit_mode.js` 앞** |
| 18 | `gap_chart.js` | Gap Chart — **15·16·17 재사용, `edit_mode.js` 앞** |
| 19 | `issue_dist.js` | Issue Table Map 미니셀 |
| 20 | `trim.js` | Trim Analysis 화면 + loadExcelJS |
| 21 | `characteristic.js` | Characteristic 탭(서브탭 6종 전환) |
| 22 | `excel_export.js` | 탭별 Excel Down (vendored exceljs) |
| 23 | `raw_data.js` | Raw Data lazy-load + `RAW_NUM_RE` |
| 24 | `chart_notes.js` | 차트 주석 + `cnDetach`/`cnSyncFromChart`/`cnFlush` |
| 25 | `note.js` | Note 탭 Luckysheet(iframe 격리) |
| 26 | `edit_mode.js` | 편집 위젯 — **`MODE` 를 정의하므로 17·18 이 앞이어야 한다** |
| 27 | `input_info.js` | Input File Information 모달(ℹ) |
| 28 | `leave_guard.js` | 세션 이탈 확인(미저장 편집 보호) |
| 29 | `chat.js` | 챗봇 위젯 — 독립(자체 DOM 주입) |
| 30 | `admin_message.js` | 관리자 팝업 메시지 — 독립 |
| 31 | `boot.js` | 로드 오버레이 + 부트스트랩 — **항상 마지막** |

순서에 **의미가 있는 곳은 12·17·18·26 네 줄뿐**이다(재사용 대상이 먼저 와야 한다).
나머지는 관례적 묶음이므로, 새 모듈은 재사용하는 모듈 뒤·`edit_mode.js` 앞에 넣는다.

- 활성 탭만 즉시 렌더, 나머지는 dirty + idle 프리렌더. Distribution/Issue 미니셀은
  IntersectionObserver + rAF 로 보이는 셀만 그린다.
- 모드별 탭 노출: `syncTabVisibility` 가 Compare/Commonality 탭을 해당 모드에서만 표시.
  legacy(`source != "web_report"`) 세션은 web_report 전용 데이터로만 채워지는 탭을 숨긴다 —
  `WEB_REPORT_ONLY_TABS = ["cpk", "map-analysis", "characteristic", "note"]`
  ([tabs_topbar.js](../server/report/static/webreport/tabs_topbar.js)).

## Characteristic 탭 (2026-08)

상위 탭 1개(`data-tab="characteristic"`) 아래 **서브탭 6종**: Trim Analysis / Shmoo / BV /
Analog Chart / TCB / DVO. **Trim Analysis 는 종전 최상위 탭이던 화면을 그대로 옮긴 것**이라
렌더는 계속 [trim.js](../server/report/static/webreport/trim.js) 가 담당하고 컨테이너 id
`#panel-trim-analysis` 도 그대로다 —
[characteristic.js](../server/report/static/webreport/characteristic.js) 는 **서브탭 전환만**
관리한다(`CHAR_SUB_RENDERERS`). 나머지 5개는 아직 화면이 없어 안내 문구만 보여준다.

서브탭도 상위 탭과 **같은 lazy 규칙**이다 — 그 서브탭에 들어갈 때 처음 그린다
(`charSubDirty` + `showCharSub`). 새 서브 화면을 붙일 때는 `CHAR_SUB_RENDERERS` 에
렌더 함수 1개를 등록하는 것이 전부이고, 서버 계약(TAB_REGISTRY)은 건드리지 않는다.

## 불변 규칙
- **Distribution 다운샘플 절대 금지** (프로젝트 CLAUDE.md §5 규칙 #5). 상세·통계는 전
  포인트. 미니셀(썸네일)만 표시용 다운샘플(`DIST.DOWNSAMPLE`, 소스별 소프트 상한 1500)이
  유일한 예외.
- **미니셀 ECDF 점은 canvas 오버레이로 그린다** (2026-07-20). 축·그리드·LSL/USL 점선·주석·
  상태 배경은 그대로 Plotly 가 그리고, 점만 `distPaintPoints`(`distribution.js`)가 Plotly 축
  좌표계(`_offset`+`l2p`)로 canvas 에 찍는다. Plotly 에는 전 소스 통합 min/max x 2점을 담은
  투명 sentinel trace(`distSentinelTrace`)만 넘겨 x autorange 기여를 그대로 재현한다
  (표시용 다운샘플이 양끝점을 항상 보존하므로 값이 불변 — 헤드리스 Plotly 로 축 범위
  동일성 검증 완료). 소스 수만큼 SVG 마커 DOM 이 늘던 병목을 없애기 위한 것이며 좌표·색·
  점 크기(3px)는 그대로다. 상세 CDF(`distRenderCdf`)는 별개 경로(scattergl)라 무관.
  **주의: `Plotly.toImage` 는 Plotly 자체 SVG 만 직렬화하므로 canvas 오버레이의 점을 담지
  못한다.** 현재 `chartNotesApply`(차트 주석·Note 붙여넣기 PNG)는 상세 CDF·히스토그램에만
  붙어 있어 안전하지만(chart_notes.js ← item_detail.js), 이를 미니셀로 확장하면 **점 없는
  PNG** 가 조용히 나간다 — 확장 시 canvas 를 합성하는 별도 경로가 필요하다.
- **표시점 캡은 칸 예산을 소스 수로 나눈다** (2026-07-21, 다소스 대응). `DIST.DOWNSAMPLE`
  은 **소스별** 상한이라 소스 40개 세션에서는 칸 하나가 수만 점이 됐다. IssueTable
  미니셀은 112px 라 찍히는 픽셀이 ~1.7만개뿐이라 전부 덧칠 낭비. 이제 `distCapFor(소스수,
  칸예산)` 이 소스별 유효 캡을 정한다 — 칸 예산은 `CELL_BUDGET_MINI`(3000, IssueTable) /
  `CELL_BUDGET_CARD`(8000, 갤러리·Bin 상세), 하한 `MIN_PER_SOURCE`(150).
  소스가 적으면 나눗셈 결과가 `DOWNSAMPLE`(1500)로 클램프된다(카드 ≤5소스 / 미니 ≤2소스).
  캡이 기본값보다 낮을 때만 `distHardCap` 이 마지막에 균등 stride 로
  캡을 강제한다(양끝 유지) — 기본 캡 경로는 강제 보존 초과를 그대로 허용하는 소프트 상한.
  `distStepY` 의 채움 예산(`cap×1.5`)과 budget 하한(`cap×0.4`)도 캡에 연동돼 기본 캡
  1500 에서 각각 2250·600 이다(`FILL_MAX_POINTS` 3000 은 절대 천장이라 미도달).
  rAF 프레임당 렌더 장수도 `distPerFrame()` 이 소스 수로
  조절한다(≥16 → 1장, ≥8 → 2장). 실측 40소스 미니셀 1장: 44.6ms → 11.3ms(콜드),
  재스크롤 4.4ms. 표시점 메모(`distDisplayPoints` / limitWin 전용 `distDisplayPointsWindowed`)
  는 **캡별로 분리 저장**한다 — 같은 항목도 칸 종류에 따라 캡이 다르기 때문.
- **Serial 순 모드의 미니셀 캡은 균등 stride 뿐이다**(위 Distribution 절) — ECDF 전용
  세로 채움·꼬리 보존 규칙을 run chart 에 적용하지 말 것(없던 구조가 생긴다).
- **Distribution ECDF 미니셀 렌더는 markers 전용, 선 금지.** 갤러리 카드
  (`distRenderGalleryCell`)·Bin 상세 셀(`renderDistCell`)·Issue Table 산포 미니셀
  (`renderMiniDistCell`) 3곳 모두 점만 찍고 어떤 연결선도 긋지 않는다(계단형
  `line.shape:"hv"` 포함 금지 — x축 수평선은 UX 에 반함). 고유값이 적은 이산(code) 항목의
  성김은 동일값 구간을 세로 점으로 채우는 보간(`distPointsForDisplay` = `distFillVertical`
  → `distDownsampleForDisplay` 순서)으로만 보정한다.
- **채우는 점 개수 = 그 값의 실제 측정 개수** (2026-08-25). 채움 간격(stepY)은 서버가
  응답에 함께 싣는 **소스별 표본 수 n** 으로 정한다 — `distStepY(ys, cap, n)` 이 `100/n`
  을 돌려주고 성능 하한(`100/fillMax`, 기본 캡 1500 → 2250)으로만 클램프한다.
  응답 필드는 `items[].sources[].n` 이며 [tabs/distribution.py](../web_report/tabs/distribution.py)
  `build_distribution_compact` 와 [dist_pack.py](../web_report/dist_pack.py) `_ecdf_sources`
  **두 경로가 같은 자리**에 낸다(정준 JSON 일치 계약 — `canon()` 이 sort_keys 를 쓰지 않아
  삽입 순서가 곧 계약이다. `tests/test_dist_pack.py` 가 검사).
  - 이 규칙 전까지는 최소 양의 Δy 를 `DIST.FILL_VISUAL_MAX_DY`(0.3%)로 캡했는데, 표본이
    333 미만이면 **단일 관측 riser 까지 쪼개져** 실제보다 촘촘해졌다(n=100 이 400점).
    최소 Δy 는 n 의 **상한 추정**일 뿐이라(모든 고유값이 2회 이상 중복이면 과대) 그 캡이
    성김 보정으로 필요했던 것인데, n 이 오면 두 경우가 한 규칙으로 정리된다.
    두 상수(`FILL_VISUAL_MAX_DY`/`FILL_STEP_Y`)는 **n 이 없는 옛 응답 폴백 전용**으로 남았다
    (구버전 Honey 가 올린 dist blob·옛 캐시). perf_guard `R13-ecdf-fill-cap` 이 폴백 밖
    사용을 차단한다.
  - ⚠️ 채움 루프는 **누적 덧셈이 아니라 riser 균등 분할**이다 — `k = round(Δy/stepY)` 를
    먼저 확정하고 `prevY + j*Δy/k` 로 배치한다. 서버가 y 를 `np.round(cum, 3)` 으로 내리기
    때문에 stepY 가 굵어지면 누적 덧셈이 riser 끝과 미세하게 어긋나 없어야 할 점이 생긴다
    (n=7 → stepY 14.285714… vs y[0] 14.286). 회귀 검사는
    [tests/test_dist_fill_js.py](../tests/test_dist_fill_js.py).
  - 캐시 세대는 `cache_policy.DIST_BATCH_SCHEMA_VERSION` 이다 — `dist_batch_key` 에만 넣고
    `dist_key` 에는 **일부러 넣지 않았다**(그쪽은 Honey blob 시딩 자리라 무효화하면 콜드
    dist 빌드가 되살아난다). 상세 [docs/12](12_web_report_cache.md).
  - Excel 다운로드 포팅본(`client/excel_download/_charts.py` `_dist_step_y`/
    `_dist_fill_vertical`)은 이 규칙의 사본이라 **같이 고친다**. sources 튜플의 표본 수는
    **인덱스 7(ecdf_n)** 이다 — 인덱스 4 의 `n` 은 정규분포 곡선 전용(의도적 None)이라
    자리를 나눴다.
  세로 방향 표시용 업샘플링만 허용하고 x값을 만들어내는 가로 보간은 금지다. 상세 CDF
  (`distRenderCdf`)는 원본 전 측정값을 값당 1점으로 그려 이미 세로 점기둥이므로 대상 외.
- **tabs/ 통계·honeyform 변환 로직을 고칠 때 검증 기준은 "같은 세션 payload 의 정준 JSON
  완전 일치"** — 벡터화·리팩토링은 값을 바꾸지 않는다(정수 컬럼 int64 dtype 보존 포함).
- Excel 내보내기는 vendored `exceljs.min.js` 를 브라우저에서 동적 로드해 생성(서버
  openpyxl 금지 규칙 준수).

## 작업 경계
report_view.html + static/webreport 는 web_report 탭 UI 작업 범위에서 **자유 수정 가능**
(권한 경계는 [../CLAUDE.md](../CLAUDE.md) §5). 단 DB/세션/인증 관련 로직은 web_report 탭과
무관하므로 그쪽은 별도 확인.

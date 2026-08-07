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
| Raw Data | `raw_data.py` | payload 는 placeholder — 실제는 lazy 조회/편집 라우트 |
| Yield | `yield_tab.py` | `build_yield_rows` + fail_counts/fail_bin_ranking/yield_overview + STEP 분리(`build_yield_step_groups`). **Temperature 는 RT source 만** 입력으로 받는다(metrics 가 결정) |
| CPK | `cpk.py` | `build_cpk_rows` (source 별 행, total 합산 행 없음) — 통계는 **Bin1(양품) 기준 단일 값** |
| Issue Table | `issue_table.py` | Yield 파생 + cpk<1.33 파생(Bin1 기준) + ETC. comment/Status/행 숨김은 편집 DB 에서 채움. **Temperature 는 RT source 만**(TEMP 는 아래 별도 시트로 분리) |
| Issue Table Temp | `temp_fail.py` | **Temperature 전용** — CT/HT 를 RT limit 으로 **전 항목** 재판정한 item 단위 행(다른 모드는 `[]`). row_key `TEMP\|<item>` |
| Distribution | — (lazy, 항목 배치) | `/full` 은 빈 시트 + `distribution_index`(항목 목록). ECDF 는 **화면에 보이는 항목만** `GET .../web_report/distribution_batch?subjects=…` 로 받는다 |
| Trim Analysis | — (lazy, **버튼 시작**) | `/full` 은 빈 시트. **탭 진입만으로는 아무 요청도 안 한다** — 「분석 시작」을 눌러야 `GET .../web_report/trim_analysis` 를 받고, 그 뒤 차트는 `GET .../web_report/trim_chart_batch` 로 **한 페이지 6개씩** |
| Map Analysis | `Map_analysis.py` (하이브리드 lazy) | wafer map die/bin 집계 — `/full` 은 dies 뺀 경량 메타(`include_dies=False`), die 전량은 `GET .../web_report/map_analysis` 지연 로드 (schema v8) |
| Fail Bin | `yield_tab.fail_bin_ranking` | Bin 랭킹 |
| Note | — (클라 전용) | TAB_REGISTRY 밖 — 프런트 자체구성 Luckysheet 캔버스, 아래 "Note 탭" 절 |

**lazy 탭 관례**: 대용량 payload(Distribution ECDF, Trim 매칭)는 `/full` 에 싣지 않고
빈 시트로 두고 전용 라우트로 지연 로드한다. Map Analysis 는 하이브리드 — 범례·격자 틀이
쓰는 경량 메타(source/x·y min·max/total/bin_counts[/step][/duts])는 `/full` 에 남기고
dies(STEP 분리 시 수백만 객체 — 메인스레드 JSON 파싱 freeze 의 주범)만 분리한다
(`map_deferred: true`, 프런트 `ensureMapData`/`fetchMapViaWorker` — wafer_charts.js).
`/full` 경로는 `build_map_analysis_rows(include_dies=False)` 로 **die dict 를 애초에 만들지
않는다** — 종전엔 전량 생성 후 `strip_dies` 로 버렸다(같은 결과, 낭비만 제거).
DUT 모드만 예외로 dies 를 만든다(`_merge_dut_rows` 가 병합 입력으로 쓴다) — 그래서
`strip_dies` 는 안전망으로 남아 있다.

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
    (`tempItemInfoForSource`) — Bin Legend 와 같은 규약이다.
  - **Yield 탭 하단 Temp Corner 섹션은 요약**이다 — 편집 열(Map/Distribution/Status/comment)
    을 뺀 읽기 전용이고 행은 전량 청크 렌더한다. 편집은 "Issue Table Temp 탭에서".
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
  일괄 복원. Status(kind `issue_status`)는 Open/Close 드랍다운(편집모드 전용, 기본 Open —
  **"Close" 만 저장, 부재=Open**). Summary 탭 Issue Status 카드가 카테고리별 Open/Close
  를 집계한다(`issueStatusCounts`, map_select.js).
- **Issue Table 선택 모드 = 일괄 삭제 + Status 일괄** (2026-07-28): 툴바 "☑ 선택 모드"(구
  "🗑 삭제 모드", id/CSS 클래스는 `issueDelMode`/`.issue-del-mode` 그대로)를 켜면 행 체크박스가
  뜬다. 체크박스가 작아 **Step 셀 아무 곳이나 클릭해도 토글**되고(`td.issue-sel-cell`,
  Step 셀 안 ▼ 는 클릭 위임에서 먼저 처리), 선택 행은 `tr.issue-row-sel` 로 강조한다.
  선택 대상 동작: 전체 선택/선택 해제 · 선택 Open/선택 Close · 선택 삭제 · 삭제 전체 초기화.
  Status 전체 일괄(All Open/All Close)은 선택과 무관해 편집모드 툴바에 상시 노출된다.
  Status 일괄은 `/issue_table/status` 에 `items:[{key,value},…]` 로 보내
  (`service.update_issue_status_bulk`) 편집 DB write·rev 증가를 1회로 묶고, 프런트는
  재렌더 없이 드랍다운·신호등·`DATA.issue_table_text` 만 낙관 갱신한다(단건 경로와 동일).
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
  - **좌측 틀고정 재실측**: 고정열(Step/Bin/TNO/Item/Map/Distribution) left 오프셋은 렌더
    시점 실측값(`--issue-colN-left`)이라, TNO 상세행을 펼쳐 Item 열이 넓어지면 stale 이 되어
    Map/Distribution 이 Item 위로 겹친다. `toggleIssueGroup`/`setAllIssueGroups` 는
    `afterIssueRowsToggled()` 로 반드시 재실측한다(Yield 는 `setYieldGroup` 에서 동일).
    **행 표시/폭을 바꾸는 새 동작을 추가하면 여기에 합류시킬 것.**
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
- **Yield/CPK 탭 Excel Down**: 각 탭 툴바 우상단 "Excel Down" 버튼(`exportYieldExcel` /
  `exportCpkExcel`, [excel_export.js](../server/report/static/webreport/excel_export.js)) —
  같은 vendored exceljs 로 시트 1장 생성. 레이아웃·서식은 Honey 클라 Excel Download
  (`client/excel_download/_sheets.py` `write_yield_sheet`/`write_cpk_sheet`)와 동일하게 맞춘다
  (B3 헤더행·A1 배너·H1 세션링크·CPK `cpk<1.33` 노란 fill·열너비/행높이). 입력이 같은 /full
  payload 라 값 파리티는 자동 — Yield 는 Pass 행+`yield_bin_groups[].rep`(접힌 상태), CPK 는
  `sheets["CPK"]` 전량·원순서(화면 필터·기준 토글 무관, 전체 die 컬럼만).
- **Compare 탭 (2026-07-23 재정의, Before/After 그룹)**: source 2개 이상을 Before/After 두
  그룹으로 나눈다(배치·업로드 순서는 [10](10_web_report_pipeline.md) 분석 모드 표). 그룹은
  `webreport_options.compare` → `validation.webreport_compare_groups` → `build_compare_payload`
  로 흐르고, 옵션이 없으면 `after=[s0], before=[s1]` 로 폴백해 **기존 세션 화면이 바뀌지 않는다**.
  서브탭 4개 = `Map 비교` / `Log 비교` / `산포 비교` / `동일성 검증`
  ([compare.js](../server/report/static/webreport/compare.js)).
  - 산출물마다 **비교 대상이 다르다**: 공통성 Map·Bin Yield·Bin 불일치 좌표표는 **전 source**,
    goodlog 는 **그룹 대표 2개**(After 최상단 vs Before 최상단), 산포 비교(`dist_shift`)와
    동일성 검증은 **그룹 pool**(그룹 전체 die 를 합친 가상 테이블, `_pool_tables`).
    그룹이 1 source 씩이면 pool 이 그 테이블 자체라 **CPK 탭 값과 완전히 같다**(복사도 없음).
  - `bin_matrix`(구 `bin_transition` 대체): 모든 source 에 있는 공통 좌표 중 **BIN 이 전부
    같지는 않은 die 를 좌표 1행씩** 나열하고 컬럼을 Before/After 그룹으로 묶는다.
    `counts.pass_to_fail`/`fail_to_pass` 만 **그룹 대표 기준**이다(화면에도 그렇게 표기).
  - `common_map.dies[].bins` — die 마다 source 별 BIN 을 실어 **마우스오버로 확인**한다.
    hover 문자열은 `waferHeatmap` 의 `opts.labelOf` 훅으로 갈아끼운다(미지정이면 종전과 동일).
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
  - **Distribution 열 + 페이지 넘김**(산포 비교, 2026-07-28): 행마다 Distribution 탭 갤러리
    카드와 같은 ECDF 미니차트를 **1/2 크기**(200×132px, `.cmp-dist-cell`)로 붙인다.
    데이터는 Distribution 탭·Issue Table 미니셀과 **같은 `distDataCache`(전체 die 기준)**
    이고 표시점 계산(`distDisplayPoints`)·canvas 점 렌더(`distPaintPoints`)도 공용 경로라
    규칙 #5(다운샘플 금지/markers 전용)를 그대로 따른다. 셀은 IntersectionObserver 로 보이는
    것만 rAF 2칸/프레임으로 그리고, 아직 안 받은 항목은 `distRequestSubject` 배치 요청 후
    `refreshDistConsumers` 가 다시 그린다(그 훅에 Compare 셀 셀렉터를 추가했다).
    표는 **한 페이지 20행**(`CMP_DIST_PAGE_SIZE`) — 미니차트가 붙어 전량 렌더가 무겁다.
    페이지 전환·필터 토글 시 `renderCmpDistSection` 이 이전 셀을 `Plotly.purge` 하고 다시
    관측을 건다(인스턴스 누수 방지). 소스 색은 Distribution 탭과 같은 `distColorFor` 라
    표 상단에 source 색 범례를 함께 띄운다.
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
    **focus(관심 항목) 판정은 서버가 정본**이고 프런트는 그 불린으로 필터 토글만 한다:
    양쪽 `Cpk>DIST_CPK_HIGH`(100, 여유 과대) 또는 양쪽 σ=0·결측(고정값)이면 **무조건 제외**,
    한쪽 `Cpk<CPK_THRESHOLD`(1.33)면 포함(**절대 품질 조건이라 게이트 미적용**),
    `|stdev_delta_pct|≥DIST_STDEV_DELTA_PCT`(15) **이고** `p_stdev<DIST_ALPHA`(0.05)면 포함.
    σ 추정치의 변동계수가 `1/√(2(n−1))` 이라 n=15 면 ≈19% — 표본이 작으면 15% 변화가 추정
    노이즈와 구분되지 않아 오경보가 된다. n 이 수천인 보통의 pool 에서는 게이트가 사실상
    무동작이고, 작은 n(수율 낮은 항목·outlier 마스킹 후)에서만 일한다. p 를 낼 수 없으면
    (n<3·양쪽 고정값) 종전대로 효과크기만 본다.
    화면 기본값은 "관심 항목만" ON(버튼으로 ALL 전환, 라벨=현재 적용 값 — cpk.js 관례),
    `N/M 항목` 카운트 표시. 정렬은 `meanshift_sigma` 내림차순(None 최하단, tie `|Δσ%|`).
    임계값은 `thresholds` 로 내려 프런트가 하드코딩하지 않는다(동일성 검증과 같은 패턴).
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
    **표시 규약**: 서버가 이미 반올림한 값(average 4자리·cpk 3자리·limit 6자리)은 프런트에서
    다시 반올림하지 않고 그대로 찍는다(`_cmpServer` = cpk.js `String(v)`) — 이중 반올림하면
    같은 값을 보여주는 CPK 탭과 표시가 갈린다. 서버가 유일하게 반올림하지 않는 stdev 만
    표시 시점에 유효숫자를 맞춘다(`_cmpStdev` → core.js `fmtStdev`: 소수 3자리, |v|<1 이면
    유효숫자 3자리까지 자리수 확장 — 원값은 CPK Limit 역산이 계속 쓰므로 불변).
    회귀 고정: [tests/test_compare_equivalence.py](../tests/test_compare_equivalence.py)
    (`test_single_source_group_matches_cpk_sheet` 이 average/stdev/cpk + limit 을 CPK 시트와 대조).
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
  - **항목 존재 판단은 `distribution_index`** — 인덱스와 ECDF compact 는 같은 기준
    (측정 data 전무 항목만 제외)으로 항목을 고르므로, 데이터를 받아보지 않고도 "분포가
    있는 항목인지"를 알 수 있다. Issue Table 미니셀 생성 여부가 이 판단을 쓴다
    (`distHasData` — 캐시 보유 여부로 판단하면 아직 안 받은 항목의 셀이 안 만들어진다).
  - 보유 항목은 LRU 상한(`DIST_BATCH.CACHE_MAX` 300)으로 잘라 오래 스크롤해도 힙이
    무한히 자라지 않게 한다. 축출된 항목은 다시 보이면 재요청된다.
  - **전량 `/distribution` 라우트는 유지** — 클라 업로드 프리컴퓨트 dist blob 시딩
    (ingest)과 하위호환 폴백이 쓴다. 프런트가 더 이상 호출하지 않을 뿐이다.
  - 항목 상세(전 포인트 + serial/xpos/ypos hover 메타)는 종전대로 `/scatter/<subject>`.
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
- `kind` 8종: `issue_comment` / `etc_item` / `trim_override` / `summary_engr` /
  `chart_note` / `note_sheet` / `issue_hidden` / `issue_status`
  ([edits.py](../web_report/edits.py) 규약). 편집마다 `rev` 가
  단조 증가해 캐시가 자연 무효화된다([12](12_web_report_cache.md)). dedup(동일 analysis_key)
  세션 간 편집 비공유. legacy 세션(rev==0)은 조회 시 manifest 폴백 + 첫 편집 직전 자동 시드
  (chart_note/note_sheet/issue_hidden/issue_status 는 manifest 에 없던 신규 kind 라 시드
  대상 아님). 세션 단위 저장이라 rawdata 수정 → 재업로드(새 세션) 시 숨김/Status 는 자연
  리셋된다.

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

- **쓸 수 있는 자리 2곳**: Issue Table 의 comment 열(`contenteditable td`)과 Summary 탭
  **Engr Comment**(`textarea`). 위젯이 달라 `mentionQueryAtCaret`/`mentionInsert` 는
  textarea(`selectionStart`) / contenteditable(`Selection` + Text 노드) **두 경로**를 갖고,
  대상 판별은 `tagFieldOf()` 하나로 모은다.
- **Engr Comment 는 textarea 를 유지한다** — contenteditable 로 바꾸면 패널이 `display:none`
  일 때 `innerText` 가 줄바꿈을 잃어(자동저장은 그 상태로도 돈다) 값이 뭉개진다. 대신 링크는
  입력칸 **아래 칩 줄**(`engrLinkChips`, [map_select.js](../server/report/static/webreport/map_select.js))
  에 띄우고, 조회 모드는 본문 자체를 `linkifyComment` 로 렌더한다. 저장 경로
  (`POST .../summary/engr`, kind=`summary_engr`)는 종전 그대로다.
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
  호출 지점은 4곳뿐이다:

  | 소비처 | 지점 |
  |---|---|
  | eval.db 관문 | [eval_export.py](../web_report/eval_export.py) `_merge_comment` — 여기 하나로 챗봇 코멘트 검색·AI Comment 선례 인용·관리자 패널·CSV 가 전부 커버된다 |
  | 챗봇 report.db 직독 | [chatbot/tools_report.py](../server/chatbot/tools_report.py) (eval_export 를 우회하는 유일한 경로) |
  | 웹 Excel Down | [excel_export.js](../server/report/static/webreport/excel_export.js) |
  | Honey Excel Download | [client/excel_download/_sheets.py](../client/excel_download/_sheets.py) |

  **저장 경로에서는 절대 벗기지 않는다** — 원문이 정본이다. `_COMMENT_MAX_LEN`(2000자)
  검사도 마크업 포함 길이 그대로다(서식 1개당 3~4자 오버헤드).
- 회귀 고정: [tests/test_comment_format.py](../tests/test_comment_format.py) — strip 표·멱등성·
  `_merge_comment` 관문 + **JS↔Python 문법 드리프트 가드**(sheets.js 정규식·색 테이블 대조).

## 렌더 구조 (report_view.html + static/webreport)
- 마크업+CSS 는 [report_view.html](../server/report/report_view.html), 탭별 JS 는
  [static/webreport/](../server/report/static/webreport/) **17개 모듈**(boot / core / sheets /
  tabs_topbar / yield_issue / cpk / issue_dist / distribution / item_detail / trim / compare /
  map_select / wafer_charts / raw_data / chart_notes / note / edit_mode).
- **classic script 순서 로드(전역 스코프 공유)** — ES module 로 바꾸거나 로드 순서를 바꾸지
  말 것. 정적 서빙은 `GET /pe/report/static/webreport/<filename>`(화이트리스트).
- 활성 탭만 즉시 렌더, 나머지는 dirty + idle 프리렌더. Distribution/Issue 미니셀은
  IntersectionObserver + rAF 로 보이는 셀만 그린다.
- 모드별 탭 노출: `syncTabVisibility` 가 Compare/Commonality 탭을 해당 모드에서만 표시.
  legacy(`source != "web_report"`) 세션은 web_report 전용 탭(Raw Data/CPK/Map)을 숨긴다.

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
- **Distribution ECDF 미니셀 렌더는 markers 전용, 선 금지.** 갤러리 카드
  (`distRenderGalleryCell`)·Bin 상세 셀(`renderDistCell`)·Issue Table 산포 미니셀
  (`renderMiniDistCell`) 3곳 모두 점만 찍고 어떤 연결선도 긋지 않는다(계단형
  `line.shape:"hv"` 포함 금지 — x축 수평선은 UX 에 반함). 고유값이 적은 이산(code) 항목의
  성김은 동일값 구간을 세로 점으로 채우는 보간(`distPointsForDisplay` = `distFillVertical`
  → `distDownsampleForDisplay` 순서)으로만 보정한다. 채움 간격(stepY)은 소스별 "단일 점
  1개의 ECDF 증가량"(최소 양의 Δy, `distStepY`)을 시각 연속성 캡 `DIST.FILL_VISUAL_MAX_DY`
  (0.3%)로 캡해 유도한다 — 표본이 작아 단일점 증가량이 0.3% 를 넘으면 단일점 riser 포함
  모든 riser 를 0.3% 간격 세로 점으로 채워 썸네일 누적 0~100% 에 marker 빈 구간이 없게
  한다(세로 방향 표시용 업샘플링, x값을 만들어내는 가로 보간은 금지). 조밀한 데이터
  (stepY≤0.3%)는 캡이 no-op 라 기존과 픽셀 동일. 상세 CDF(`distRenderCdf`)는 원본 전
  측정값을 값당 1점으로 그려 이미 세로 점기둥이 되므로 대상 외.
- **tabs/ 통계·honeyform 변환 로직을 고칠 때 검증 기준은 "같은 세션 payload 의 정준 JSON
  완전 일치"** — 벡터화·리팩토링은 값을 바꾸지 않는다(정수 컬럼 int64 dtype 보존 포함).
- Excel 내보내기는 vendored `exceljs.min.js` 를 브라우저에서 동적 로드해 생성(서버
  openpyxl 금지 규칙 준수).

## 작업 경계
report_view.html + static/webreport 는 web_report 탭 UI 작업 범위에서 **자유 수정 가능**
(권한 경계는 [../CLAUDE.md](../CLAUDE.md) §5). 단 DB/세션/인증 관련 로직은 web_report 탭과
무관하므로 그쪽은 별도 확인.

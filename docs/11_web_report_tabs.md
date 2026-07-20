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
| Yield | `yield_tab.py` | `build_yield_rows` + fail_counts/fail_bin_ranking/yield_overview + STEP 분리(`build_yield_step_groups`) |
| CPK | `cpk.py` | `build_cpk_rows` (source 별 행, total 합산 행 없음) |
| Issue Table | `issue_table.py` | Yield 파생 + 규격내 cpk(`cpk_limited`)<1.33 파생 + ETC. comment/Status/행 숨김은 편집 DB 에서 채움 |
| Distribution | — (lazy) | `/full` 은 빈 시트, `GET .../web_report/distribution` 지연 로드 |
| Trim Analysis | — (lazy) | `/full` 은 빈 시트, `GET .../web_report/trim_analysis` 지연 로드 |
| Map Analysis | `Map_analysis.py` (하이브리드 lazy) | wafer map die/bin 집계 — `/full` 은 dies 뺀 경량 메타(`strip_dies`), die 전량은 `GET .../web_report/map_analysis` 지연 로드 (schema v8) |
| Fail Bin | `yield_tab.fail_bin_ranking` | Bin 랭킹 |
| Note | — (클라 전용) | TAB_REGISTRY 밖 — 프런트 자체구성 Luckysheet 캔버스, 아래 "Note 탭" 절 |

**lazy 탭 관례**: 대용량 payload(Distribution ECDF, Trim 매칭)는 `/full` 에 싣지 않고
빈 시트로 두고 전용 라우트로 지연 로드한다. Map Analysis 는 하이브리드 — 범례·격자 틀이
쓰는 경량 메타(source/x·y min·max/total/bin_counts[/step][/duts])는 `/full` 에 남기고
dies(STEP 분리 시 수백만 객체 — 메인스레드 JSON 파싱 freeze 의 주범)만 분리한다
(`map_deferred: true`, 프런트 `ensureMapData`/`fetchMapViaWorker` — wafer_charts.js).

## 주요 탭 계약
- **Yield STEP 분리 (2026-07-14, 분모 전체 기준으로 통일)**: Yield 탭은 STEP(P1/P2/P3)별로
  표를 나눈다. STEP 은 각 fail die 의 `FAILTNO → (TNO 매칭) item → item 의 STEP 메타행
  (raw 4번째 행)` 으로 정한다 (`item_meta`). 각 STEP 표의 bin portion 분모는 **항상 전체
  rawdata die 수**(`build_yield_rows` 가 이미 계산한 total 기준 값을 그대로 사용, 재계산 없음)
  — `build_yield_step_groups`(payload `yield_step_groups`) 는 원본 yield_rows 를 변형하지 않고
  STEP 별 그룹핑만 한다. 따라서 같은 fail 항목이 Yield 탭·Issue Table·Summary 에서 모두 동일
  % (pass% + 모든 STEP fail% 합 = 100%). 상단 요약 박스의 STEP 요약
  (`yield_summary.by_step`: entered/fail/survivor/step_yield%)도 전체 기준 — `yield_step_summary`
  에서 `entered = 전체 die`(전 STEP 동일), `step_yield% = (전체 − 그 STEP fail)/전체`.
  **Issue Table·Summary·fail_bin_ranking 도 동일한 전체(total) 기준 값(`build_yield_rows`)**
  — Issue Table 은 merge 유지(STEP 열 포함, fail 비중 내림차순이라 P1/P3 가 교차 등장).
  프런트 원형 파이는 제거. `yield_bin_groups`(전체 기준 merge 그룹)는 Excel 내보내기용으로 유지.
- **CPK 임계값·기준 3종**: `CPK_THRESHOLD = 1.33` ([cpk.py](../web_report/tabs/cpk.py)).
  subject 당 **worst-case(최저) cpk** 로 이슈를 판단한다(`worst_cpk_by_subject(rows, field)`).
  `build_cpk_rows` 는 기준 3종을 병기한다: 전체 die(`cpk` 등 base 필드) / Bin1 양품
  (`*_bin1`, 실제 BIN==1 만) / **규격내**(`*_limited`, BIN 무관 [LSL,USL] 안 값만 —
  `_limit_masked` NaN 마스킹 후 재계산). CPK 탭 기준 토글(`cpkBasis`)이 3상 순환하고,
  **Issue Table CPK 섹션은 항상 `cpk_limited` 기준**으로 선정·표시하며 미니 분포도 규격
  창으로 재정규화(`distWindowRenorm`, `data-limitwin`)해 그린다. Distribution
  status/index 는 기존 전체 die `cpk` 유지.
- **Issue Table comment 키**: `row_key` 규약 — Yield 행 `Yield|<bin>|<item>`,
  CPK 데이터 행 `CPK|<item>`, ETC 행 `ETC|<item>`. comment 컬럼은
  `COMMENT_COLS = ["PTE comment", "개발 comment"]`. 값은 세션 편집 DB 에서 채운다.
- **Issue Table 행 숨김/Status 키** (2026-07-16): 이슈 단위 키 — Yield 는 **bin 단위**
  `Yield|<bin>`(대표행+상세행 일괄), CPK/ETC 는 `CPK|<item>`/`ETC|<item>`
  (sheets.js `issueHideStatusKey` ↔ issue_table.py 동기 필수). 숨김(kind `issue_hidden`,
  Yield/CPK 만 — ETC 는 기존 etc remove)은 행별 복원 없이 툴바 "삭제 전체 초기화"로만
  일괄 복원. Status(kind `issue_status`)는 Open/Close 드랍다운(편집모드 전용, 기본 Open —
  **"Close" 만 저장, 부재=Open**). Summary 탭 Issue Status 카드가 카테고리별 Open/Close
  를 집계한다(`issueStatusCounts`, map_select.js).
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
- **Distribution**: `build_distribution_index`(항목별 test_num·worst cpk·fail·status) /
  `scatter_item`(상세 전체 측정값) / `build_distribution_compact`(ECDF 전 포인트 컴팩트
  columnar, lazy 전용). `/distribution` 은 전 포인트·gzip·ETag.
- **Trim Analysis**: `build_trim_payload`(항목 매칭 + 슬롯별 통계 + initial shift 판정) /
  `build_trim_chart`(그룹 1개 chip-to-chip 차트). 매칭 규칙은
  [trim_match.py](../web_report/trim_match.py)(product_type 별 PMIC4/TV2 규칙셋).

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
- 시트 저장: kind `note_sheet`(item_key=`"sheet"`, 전체 치환, **≤2MB**). `/full` 에는
  `note_info`(존재/최종수정 메타)만 — 본문은 lazy `GET·POST .../web_report/note`.
  `load_edit_state` 는 이 kind 를 **제외 조회**해 comment 저장·콜드 빌드가 2MB 블롭을
  끌어오지 않는다 (`get_webreport_edits(kinds/exclude_kinds)`).
- 이미지: `POST .../web_report/note_image` (PNG/JPEG 매직바이트, ≤2MB, 세션당 200장) →
  S3(`pe/report_server/note_img/<sid>/`)+로컬 폴백
  ([_note_images.py](../server/storage_gateway/_note_images.py) — **세션 단위** 네임스페이스,
  dedup 세션 간 누출 방지). 서빙 `GET /pe/report/note_image/<sid>/<id>` (nosniff).
  세션 삭제 시 항상 일괄 정리 (akey 공유 여부 무관).
- 차트 반입: 항목 상세의 [📋 Note에 붙여넣기] → `Plotly.toImage`(주석 포함) → note_image
  업로드 → `luckysheet.insertImage`. Luckysheet 번들(≈4MB)은 Note 탭 첫 진입 시 지연 로드,
  vendor 서빙은 `routes_misc.py` 의 luckysheet/ 경로 정규식 + 확장자 mime.

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
  포인트. 미니셀(썸네일)만 표시용 다운샘플(`DIST.DOWNSAMPLE`, 소스별 소프트 상한 2000)이
  유일한 예외.
- **미니셀 ECDF 점은 canvas 오버레이로 그린다** (2026-07-20). 축·그리드·LSL/USL 점선·주석·
  상태 배경은 그대로 Plotly 가 그리고, 점만 `distPaintPoints`(`distribution.js`)가 Plotly 축
  좌표계(`_offset`+`l2p`)로 canvas 에 찍는다. Plotly 에는 전 소스 통합 min/max x 2점을 담은
  투명 sentinel trace(`distSentinelTrace`)만 넘겨 x autorange 기여를 그대로 재현한다
  (표시용 다운샘플이 양끝점을 항상 보존하므로 값이 불변 — 헤드리스 Plotly 로 축 범위
  동일성 검증 완료). 소스 수만큼 SVG 마커 DOM 이 늘던 병목을 없애기 위한 것이며 좌표·색·
  점 크기(3px)는 그대로다. 상세 CDF(`distRenderCdf`)는 별개 경로(scattergl)라 무관.
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

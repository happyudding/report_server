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
| Issue Table | `issue_table.py` | Yield 파생 + CPK<1.33 파생 + ETC. comment 는 편집 DB 에서 채움 |
| Distribution | — (lazy) | `/full` 은 빈 시트, `GET .../web_report/distribution` 지연 로드 |
| Trim Analysis | — (lazy) | `/full` 은 빈 시트, `GET .../web_report/trim_analysis` 지연 로드 |
| Map Analysis | `Map_analysis.py` | wafer map die/bin 집계 (제품 기준정보 있으면 고정 프레임) |
| Fail Bin | `yield_tab.fail_bin_ranking` | Bin 랭킹 |
| Note | — (클라 전용) | TAB_REGISTRY 밖 — 프런트 자체구성 Luckysheet 캔버스, 아래 "Note 탭" 절 |

**lazy 탭 관례**: 대용량 payload(Distribution ECDF, Trim 매칭)는 `/full` 에 싣지 않고
빈 시트로 두고 전용 라우트로 지연 로드한다.

## 주요 탭 계약
- **Yield STEP 분리 (2026-07-14)**: Yield 탭은 STEP(P1/P2/P3)별로 표를 나눈다. STEP 은
  각 fail die 의 `FAILTNO → (TNO 매칭) item → item 의 STEP 메타행(raw 4번째 행)` 으로 정한다
  (`item_meta`). 각 STEP 표의 bin portion 분모는 **그 STEP 에 진입한 die 수**(fail-stop
  cascade: P1 진입=전체, P2 진입=전체−P1fail, …) — `build_yield_step_groups`(payload
  `yield_step_groups`) 가 원본 yield_rows 를 변형하지 않고 재계산한 복사본으로 만든다. 상단
  요약 박스의 STEP 요약(`yield_summary.by_step`: entered/fail/survivor/step_yield%)은
  `yield_step_summary`. **Issue Table·Summary·fail_bin_ranking 은 STEP cascade 를 쓰지 않고
  전체(total) 기준 값(`build_yield_rows`) 그대로** — Issue Table 은 merge 유지(STEP 열 포함,
  fail 비중 내림차순이라 P1/P3 가 교차 등장). 프런트 원형 파이는 제거. `yield_bin_groups`
  (전체 기준 merge 그룹)는 Excel 내보내기용으로 유지.
- **CPK 임계값**: `CPK_THRESHOLD = 1.33` ([cpk.py](../web_report/tabs/cpk.py)). Issue
  Table·Distribution 이 공유하며 subject 당 **worst-case(최저) cpk** 로 이슈를 판단한다
  (`worst_cpk_by_subject`).
- **Issue Table comment 키**: `row_key` 규약 — Yield 행 `Yield|<bin>|<item>`,
  CPK 데이터 행 `CPK|<item>`, ETC 행 `ETC|<item>`. comment 컬럼은
  `COMMENT_COLS = ["PTE comment", "개발 comment"]`. 값은 세션 편집 DB 에서 채운다.
- **Distribution**: `build_distribution_index`(항목별 test_num·worst cpk·fail·status) /
  `scatter_item`(상세 전체 측정값) / `build_distribution_compact`(ECDF 전 포인트 컴팩트
  columnar, lazy 전용). `/distribution` 은 전 포인트·gzip·ETag.
- **Trim Analysis**: `build_trim_payload`(항목 매칭 + 슬롯별 통계 + initial shift 판정) /
  `build_trim_chart`(그룹 1개 chip-to-chip 차트). 매칭 규칙은
  [trim_match.py](../web_report/trim_match.py)(product_type 별 PMIC4/TV2 규칙셋).

## 편집 흐름 (세션 편집 DB)
web_report 편집(comment / ETC item / trim override / Summary Engr comment)의 **진실은
세션 단위 DB**(`report_webreport_edit` + `_rev`)다. manifest 는 업로드 시점 불변 스냅샷.
- 라우트: `POST .../web_report/issue_table/{etc,comments}`, `.../summary/engr`,
  `.../trim/overrides` (CSRF + 편집자 가드 — [02](02_server_query_edit.md)).
- Raw Data 셀 편집(`.../raw_data/edit`)은 예외 — parquet 원본을 재인코딩해
  `content_hash` 를 갱신한다(undo 없음).
- `kind` 6종: `issue_comment` / `etc_item` / `trim_override` / `summary_engr` /
  `chart_note` / `note_sheet` ([edits.py](../web_report/edits.py) 규약). 편집마다 `rev` 가
  단조 증가해 캐시가 자연 무효화된다([12](12_web_report_cache.md)). dedup(동일 analysis_key)
  세션 간 편집 비공유. legacy 세션(rev==0)은 조회 시 manifest 폴백 + 첫 편집 직전 자동 시드
  (chart_note/note_sheet 는 manifest 에 없던 신규 kind 라 시드 대상 아님).

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
  포인트. 미니셀(썸네일)만 표시용 1000점 다운샘플이 유일한 예외.
- **Distribution ECDF 미니셀 렌더는 markers 전용, 선 금지.** 갤러리 카드
  (`distRenderGalleryCell`)·Bin 상세 셀(`renderDistCell`)·Issue Table 산포 미니셀
  (`renderMiniDistCell`) 3곳 모두 점만 찍고 어떤 연결선도 긋지 않는다(계단형
  `line.shape:"hv"` 포함 금지 — x축 수평선은 UX 에 반함). 고유값이 적은 이산(code) 항목의
  성김은 동일값 구간을 세로 점으로 채우는 보간(`distPointsForDisplay` = `distFillVertical`
  → `distDownsampleForDisplay` 순서)으로만 보정한다. 상세 CDF(`distRenderCdf`)는 원본 전
  측정값을 값당 1점으로 그려 이미 세로 점기둥이 되므로 대상 외.
- **tabs/ 통계·honeyform 변환 로직을 고칠 때 검증 기준은 "같은 세션 payload 의 정준 JSON
  완전 일치"** — 벡터화·리팩토링은 값을 바꾸지 않는다(정수 컬럼 int64 dtype 보존 포함).
- Excel 내보내기는 vendored `exceljs.min.js` 를 브라우저에서 동적 로드해 생성(서버
  openpyxl 금지 규칙 준수).

## 작업 경계
report_view.html + static/webreport 는 web_report 탭 UI 작업 범위에서 **자유 수정 가능**
(권한 경계는 [../CLAUDE.md](../CLAUDE.md) §5). 단 DB/세션/인증 관련 로직은 web_report 탭과
무관하므로 그쪽은 별도 확인.

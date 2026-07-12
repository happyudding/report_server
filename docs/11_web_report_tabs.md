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
| Yield | `yield_tab.py` | `build_yield_rows` + fail_counts/fail_bin_ranking/yield_overview |
| CPK | `cpk.py` | `build_cpk_rows` (source 별 행, total 합산 행 없음) |
| Issue Table | `issue_table.py` | Yield 파생 + CPK<1.33 파생 + ETC. comment 는 편집 DB 에서 채움 |
| Distribution | — (lazy) | `/full` 은 빈 시트, `GET .../web_report/distribution` 지연 로드 |
| Trim Analysis | — (lazy) | `/full` 은 빈 시트, `GET .../web_report/trim_analysis` 지연 로드 |
| Map Analysis | `Map_analysis.py` | wafer map die/bin 집계 (제품 기준정보 있으면 고정 프레임) |
| Fail Bin | `yield_tab.fail_bin_ranking` | Bin 랭킹 |

**lazy 탭 관례**: 대용량 payload(Distribution ECDF, Trim 매칭)는 `/full` 에 싣지 않고
빈 시트로 두고 전용 라우트로 지연 로드한다.

## 주요 탭 계약
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
- `kind` 4종: `issue_comment` / `etc_item` / `trim_override` / `summary_engr`
  ([edits.py](../web_report/edits.py) 규약). 편집마다 `rev` 가 단조 증가해 캐시가 자연
  무효화된다([12](12_web_report_cache.md)). dedup(동일 analysis_key) 세션 간 편집 비공유.
  legacy 세션(rev==0)은 조회 시 manifest 폴백 + 첫 편집 직전 자동 시드.

## 렌더 구조 (report_view.html + static/webreport)
- 마크업+CSS 는 [report_view.html](../server/report/report_view.html), 탭별 JS 는
  [static/webreport/](../server/report/static/webreport/) **15개 모듈**(boot / core / sheets /
  tabs_topbar / yield_issue / cpk / issue_dist / distribution / item_detail / trim / compare /
  map_select / wafer_charts / raw_data / edit_mode).
- **classic script 순서 로드(전역 스코프 공유)** — ES module 로 바꾸거나 로드 순서를 바꾸지
  말 것. 정적 서빙은 `GET /pe/report/static/webreport/<filename>`(화이트리스트).
- 활성 탭만 즉시 렌더, 나머지는 dirty + idle 프리렌더. Distribution/Issue 미니셀은
  IntersectionObserver + rAF 로 보이는 셀만 그린다.
- 모드별 탭 노출: `syncTabVisibility` 가 Compare/Commonality 탭을 해당 모드에서만 표시.
  legacy(`source != "web_report"`) 세션은 web_report 전용 탭(Raw Data/CPK/Map)을 숨긴다.

## 불변 규칙
- **Distribution 다운샘플 절대 금지** (프로젝트 CLAUDE.md §5 규칙 #6). 상세·통계는 전
  포인트. 미니셀(썸네일)만 표시용 1000점 다운샘플이 유일한 예외.
- **tabs/ 통계·honeyform 변환 로직을 고칠 때 검증 기준은 "같은 세션 payload 의 정준 JSON
  완전 일치"** — 벡터화·리팩토링은 값을 바꾸지 않는다(정수 컬럼 int64 dtype 보존 포함).
- Excel 내보내기는 vendored `exceljs.min.js` 를 브라우저에서 동적 로드해 생성(서버
  openpyxl 금지 규칙 준수).

## 작업 경계
report_view.html + static/webreport 는 web_report 탭 UI 작업 범위에서 **자유 수정 가능**
(권한 경계는 [../CLAUDE.md](../CLAUDE.md) §5). 단 DB/세션/인증 관련 로직은 web_report 탭과
무관하므로 그쪽은 별도 확인.

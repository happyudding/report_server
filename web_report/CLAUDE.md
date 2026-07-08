# web_report — Claude Code 진입점

> **중요**: 이 폴더(`web_report/`)만 열려 있는 이유는 **여기 안에서만 작업하기 위함**이다.
> `web_report/` 밖의 파일(server/, client/, _reference/ 등)을 수정해야 하는 상황이면
> **절대 임의로 고치지 말고, 먼저 사용자에게 이유와 영향 범위를 설명하고 확인을 받은 뒤에만 진행한다.**
> 이는 상위 [report_server/CLAUDE.md](../CLAUDE.md) §7 규칙과 동일하며, 이 폴더에서는 특히 엄격히 지킨다.

전체 프로젝트 구조/불변 규칙은 [report_server/docs/INDEX.md](../docs/INDEX.md) 와
[report_server/CLAUDE.md](../CLAUDE.md) 참조. 아래는 `web_report/` 패키지 자체의 요약.

---

## 0. 이 패키지가 하는 일

`web_report/` 는 Honey 클라이언트가 보내는 **7-meta honeyform parquet** (SERIAL, SHOT, DUT,
XPOS, YPOS, BIN, FAILTNO + 측정 항목 컬럼, 상단 6행은 TSEQ/TNO/STEP/UNIT/HILIM/LOLIM 메타)
페이로드를 받아 세션으로 저장하고, 요약/수율/이슈 테이블 등을 계산해 웹 리포트 페이지로
보여주는 로직을 담당한다. 기존 xlsx 업로드 흐름(`server/upload_xlsx.py`)과는 별개의
**신규 병행 흐름**이며, 서버 쪽 진입점은 `server/upload_webreport.py` (외부 파일, 여기 밖).

## 1. 디렉토리 인덱스

```
web_report/
├── __init__.py        패키지 docstring만
├── service.py          ingest_webreport() — 업로드 처리(해시→analysis_key→DB 세션→
│                        parquet+manifest 저장), load_webreport() — 세션 재계산 조회,
│                        raw_data 조회·편집, update_issue_etc_items/update_issue_comments
│                        (Issue Table ETC 항목·comment 편집 — manifest 만 재저장)
├── honeyform.py         7-meta honeyform 검증/파싱, parquet 인코딩·디코딩
│                        (META_COLUMNS, META_ROW_LABELS 등 스키마 상수)
├── metrics.py           build_report_payload() — tabs/ 각 모듈을 모아 최종 report dict 조립
│                        (Summary/Raw Data/Yield/CPK/Issue Table/Distribution/
│                         Trim Analysis/Histogram/Map Analysis/Fail Bin 시트)
├── html.py              render_report_html() — 세션 상세 페이지 HTML 렌더 (⚠ 아직 어떤
│                        라우트에서도 호출되지 않음 — 실제 서빙 중인 세션 상세 페이지는
│                        server/report/report_view.html, 이 파일은 web_report 밖)
└── tabs/                시트별 row 빌더 (metrics.py 가 호출)
    ├── common.py        json_safe/fmt_type/num/round_num, bin_sort_key(BIN 정렬 공용)
    ├── summary.py        build_summary_rows (placeholder — return [])
    ├── raw_data.py       build_raw_data_rows(payload 용 placeholder — return []) +
    │                     build_raw_data_columns/query_raw_data/apply_raw_data_edits (lazy-load 조회·편집)
    ├── yield_tab.py      build_yield_rows(comment/count 컬럼 없음), fail_counts_by_source, fail_bin_ranking, yield_overview(상단 요약 박스)
    ├── cpk.py            build_cpk_rows (source 별 행만 — "total" 합산 행 없음. Issue Table 이
    │                     subject 당 worst-case(최저) cpk 로 이슈 판단)
    ├── issue_table.py    build_issue_table_rows (Yield 파생 행 + CPK<1.33(subject 별 worst-case) 파생 행 +
    │                     ETC). PTE/개발 comment 는 manifest.issue_comments 에서 row_key 로 채움
    ├── distribution.py   build_distribution_rows (전량 ECDF — 프런트는 아직 렌더 안 함)
    ├── trim_analysis.py  build_trim_analysis_rows (placeholder — return [])
    ├── histogram.py      build_histogram_rows (placeholder — return [])
    └── Map_analysis.py   build_map_analysis_rows (wafer map die/bin 집계)
```

## 2. 외부(밖) 연결점 — 여기는 참고만, 수정 시 확인 필수

| 연결점 | 파일 (web_report 밖) | 용도 |
|--------|----------------------|------|
| 업로드 라우트 | `server/upload_webreport.py` | `POST /pe/report/upload_webreport`, `GET /pe/report/web_report/<sid>` → `/pe/report/view/<sid>` 리다이렉트 |
| web_report 편집 라우트 | `server/report/report_routes.py` | `.../web_report/raw_data`(조회), `.../raw_data/edit`, `.../issue_table/etc`, `.../issue_table/comments` (모두 PIN 재검증) |
| 세션 상세 페이지 | `server/report/report_view.html` | 실제 서빙되는 세션 상세 UI (topbar/tabs/grid 등) |
| 저장소(parquet/manifest) | `server/storage_gateway/__init__.py` | `save/load_webreport_sources`(parquet+manifest), `save/load_webreport_manifest`(manifest 만) |
| DB CRUD | `server/database/report_db.py` | `create_session`, `update_session`, `get_all_object_infos`, `log_audit` |
| 공통 설정 | `server/config.py` | `REPORT_UPLOAD_DIR` 등 |

`web_report/` 안에서 위 연결점의 **호출 시그니처(함수명·인자·반환 dict 키)** 를 바꾸면
바깥 파일도 맞춰 고쳐야 하므로, 그 경우도 먼저 사용자에게 알릴 것.

**예외 — `server/report/report_view.html`**: web_report 각 탭(Raw Data 등)의 실제 화면
UI(체크박스 목록 스타일, 표 컬럼 순서/정렬 화살표/테두리, 글자크기, 필터/버튼 등)는 이
파일에 구현되어 있다 (web_report/tabs/ 는 데이터만 공급). web_report 탭 UI 작업 범위 안의
수정이라면 report_view.html 을 매번 승인받지 않고 바로 Edit 해도 된다 (2026-07-07 사용자
승인). 단, 이 파일의 DB/세션/인증 관련 로직(verify_password, 세션 삭제 등 web_report 탭과
무관한 부분)을 바꿔야 하면 여전히 먼저 물어볼 것.

## 3. 작업 시 유의

- `report_server/CLAUDE.md` §5 의 불변 규칙(원본 xlsx 미저장, `report_` prefix, analysis_key
  산출 방식 등)은 web_report 흐름에도 동일하게 적용된다.
- `html.py::render_report_html` 은 아직 미사용 코드다. 세션 상세 UI를 고칠 때 사용자가
  "세션 페이지"라고 하면 우선 `server/report/report_view.html` (web_report 밖!) 을
  의미하는지 확인할 것 — 헷갈리기 쉬운 지점.
- **탭 구현 상태 (2026-07-08 기준)**: Yield / CPK / Issue Table / Map Analysis / Fail Bin 은
  계산·렌더 완료 (CPK 탭 렌더 함수 `renderCpk()` 는 report_view.html 에 추가 — web_report 밖
  파일이라 사용자 승인 후 수정함). Raw Data 는 lazy-load 조회/편집 완료. Distribution 은
  산포 탭으로 구현 완료 — `tabs/distribution.py` 가 ECDF(`sheets["Distribution"]`, 갤러리 미니셀 +
  Issue Table 미니분포 공용)와 함께 `build_distribution_index`(항목별 test_num·worst-case cpk·
  fail(FAILTNO==TNO 귀속)·status) / `scatter_item`(상세용 전체 측정값)을 공급한다. metrics 가
  `report["distribution_index"]` 로 내려주고, 상세 전체점은 `GET .../web_report/scatter/<subject>`
  (report_routes.py, 승인 후 추가)로 지연 로드. 프런트(report_view.html `renderDistribution`)는
  툴바(전체/cpk<1.33/Fail Only, 기본 cpk<1.33)+가상스크롤 갤러리+Typeahead+카드→상세(CDF+히스토그램)
  로 렌더. 미니셀만 1000점 다운샘플(상세·통계는 전체점). summary / raw_data(payload) /
  trim_analysis / histogram 빌더는 `return []` 플레이스홀더 (Summary 탭 화면은 프런트가
  Map Analysis + Fail Bin 시트로 자체 구성).
- **Issue Table comment 저장**: web_report 세션은 legacy 의 `PATCH /content` 를 쓰지 않는다
  (해당 라우트가 web_report 를 400 거부). PTE/개발 comment 는 프런트가
  `POST .../web_report/issue_table/comments` 로 보내고 `manifest.issue_comments` 에 row_key
  (`Yield|<bin>|<item>` / `CPK|<item>` / `ETC|<item>`) 단위로 저장 → 조회 시
  build_issue_table_rows 가 다시 채운다. sources parquet 는 불변, manifest 만 재저장.
- **legacy 세션 탭**: web_report 전용 탭(Raw Data/CPK/Map Analysis)은 `source != "web_report"`
  세션에선 프런트가 탭 버튼을 숨긴다(`syncTabVisibility`).

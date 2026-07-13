# web_report — Claude Code 진입점

`web_report/` 는 Honey 클라이언트가 보내는 **7-meta honeyform parquet** 를 받아 세션으로
저장하고, 요약/수율/이슈/분포 등을 계산해 웹 리포트 페이지로 공급하는 패키지다. xlsx 업로드
흐름([server/upload_xlsx.py](../server/upload_xlsx.py))과는 별개의 **신규 병행 흐름**이며,
서버 진입점은 [server/upload_webreport.py](../server/upload_webreport.py)(밖 파일).

> **기능 정본은 docs 3개**: [파이프라인 10](../docs/10_web_report_pipeline.md)
> (업로드→ingest→저장→로드·honeyform 스키마·분석 모드 4종·신원) ·
> [탭 계약 11](../docs/11_web_report_tabs.md)(TAB_REGISTRY·탭별 데이터 계약·편집 흐름·렌더) ·
> [캐시 12](../docs/12_web_report_cache.md)(3계층 캐시·키 규약·컴퓨트·env).
> 이 문서는 패키지 자체의 디렉토리 인덱스와 작업 규칙만 담는다. 전체 구조·불변 규칙은
> [../CLAUDE.md](../CLAUDE.md), [../docs/INDEX.md](../docs/INDEX.md).

## 디렉토리 인덱스

```
web_report/
├── __init__.py        패키지 docstring
├── service.py          조회/편집 오케스트레이션 (외부 진입점 — 공개 시그니처 불변,
│                        ingest_webreport 는 ingest.py 재노출)
├── ingest.py           업로드 ingest (해시→저장→세션 생성→편집값 시드→프리웜)
├── loader.py           세션 → parquet 다운로드·디코드 → HoneyformTable (TABLES_CACHE 결합)
├── honeyform.py        7-meta honeyform 검증/파싱, parquet 인코딩·디코딩 (스키마 상수)
├── validation.py       canon·mode/meta 정규화·client_identity — 캐시·저장소 무의존 순수 헬퍼
├── edits.py            편집 상태 — 진실은 세션 단위 DB(report_webreport_edit). legacy 폴백/시드
├── ai_comment.py       eval_analyzer(eval_engine) 통합의 유일한 접점 — IssueTable AI Comment
│                        (ai_comment 옵션 세션 콜드 빌드에서 evaluate() 호출 → docs/13)
├── metrics.py          build_report_payload — 공용 컨텍스트 조립 후 tabs.TAB_REGISTRY 순회
├── cache.py            인메모리 LRU 캐시 인프라 (레지스트리·락·무효화)
├── cache_policy.py     캐시 키 구성 규약의 단일 진실 (빌더 + 무효화 트리거 표)
├── disk_cache.py       계산 산출물(report/dist) 로컬 디스크 캐시
├── response_cache.py    /full·/scatter 응답 gzip bytes LRU 캐시
├── compute.py          콜드 빌드 ProcessPool 오프로드 (prewarm 포함)
├── runtime.py          저장소 포트 주입 지점 (report_extension.init_app 이 주입)
├── ports.py            StoragePort/SessionRepo Protocol (DIP 경계)
├── rawedit.py          Raw Data 소스 교체/내보내기 헬퍼
├── trim_match.py       Trim 항목명 매칭 순수 모듈 (product_type 별 PMIC4/TV2 규칙셋)
├── wafer_frame.py      제품 기준정보(die pitch+wafer 크기) → 고정 map 프레임
└── tabs/               시트별 row 빌더 + TAB_REGISTRY (시트 구성 단일 진실)
    ├── __init__.py        TAB_REGISTRY / TabContext / TabSpec
    ├── common.py          json_safe/bin_sort_key/to_coord 공용 헬퍼
    ├── summary.py         build_summary_rows (placeholder)
    ├── raw_data.py        build_raw_data_rows(placeholder) + lazy 조회/편집
    ├── yield_tab.py       build_yield_rows / fail_counts / fail_bin_ranking / yield_overview
    ├── cpk.py             build_cpk_rows + CPK_THRESHOLD(1.33) + worst_cpk_by_subject
    ├── issue_table.py     build_issue_table_rows (Yield + CPK<1.33 + ETC, comment 는 편집 DB)
    ├── distribution.py    build_distribution_index / scatter_item / build_distribution_compact (lazy)
    ├── trim_analysis.py   build_trim_payload / build_trim_chart (lazy)
    ├── commonality.py     search_chips / chip_percentiles (Commonality 모드)
    ├── compare.py         build_compare_payload / build_goodlog (Compare 모드)
    └── Map_analysis.py    build_map_analysis_rows (wafer map die/bin 집계)
```

## 외부(밖) 연결점 — 참고만, 시그니처 변경 시 확인 필수

| 연결점 | 파일 (web_report 밖) | 용도 |
|--------|----------------------|------|
| 업로드 라우트 | [server/upload_webreport.py](../server/upload_webreport.py) | `POST /pe/report/upload_webreport` |
| 데이터/편집 라우트 | [server/report/routes_webreport.py](../server/report/routes_webreport.py) | `.../web_report/*` (CSRF + 편집자 가드) |
| 세션 상세 페이지 | [report_view.html](../server/report/report_view.html) + [static/webreport/](../server/report/static/webreport/) | 마크업+CSS / 탭별 JS 17모듈 |
| 저장소(parquet/manifest) | [storage_gateway](../server/storage_gateway/__init__.py) | `save/load_webreport_sources`. **직접 import 금지** — `runtime.storage()` 포트로 접근 |
| DB CRUD | [database/report_db.py](../server/database/report_db.py) | create/update_session, log_audit, get/apply_webreport_edits, get_webreport_edit_rev |
| eval_analyzer 엔진 | [eval_analyzer/](../eval_analyzer/) `eval_engine.evaluate()` | **ai_comment.py 에서만 import** (단방향, persist=False) → [docs/13](../docs/13_eval_analyzer_integration.md) |

web_report 안에서 위 연결점의 **호출 시그니처(함수명·인자·반환 dict 키)** 를 바꾸면 바깥
파일도 맞춰 고쳐야 하므로, 그 경우 함께 반영할 것.

## 작업 규칙

- **소유권/수정 권한 경계** (정본 [../docs/15_ownership.md](../docs/15_ownership.md)):
  `web_report/`(여기) + web_report 관련 html(report_view.html, static/webreport/) + `server/`
  (단 `storage_gateway/` 제외) 는 🟢 신서버·자유 수정. `client/` 나머지는 🟡 사전 승인.
  🔒 구서버 동결 = `d1/`·`report_generator/`·`honey_parse/`·`storage_gateway/`.
- **분할 JS 는 classic script 순서 로드(전역 스코프 공유)** — ES module 로 바꾸거나 로드
  순서를 바꾸지 말 것.
- 세션 상세 UI 를 고칠 때 사용자가 "세션 페이지"라고 하면 우선 report_view.html(web_report
  밖!)을 의미하는지 확인할 것 — 헷갈리기 쉬운 지점.
- `../CLAUDE.md` §5 불변 규칙(원본 xlsx 미저장, `report_` prefix, analysis_key 산출,
  Distribution 다운샘플 금지)은 web_report 흐름에도 동일 적용.
- **검증 기준**: tabs 통계·honeyform 변환을 고칠 땐 "같은 세션 payload 정준 JSON 완전
  일치"로 회귀 없음을 확인한다(값 불변 — 정수 컬럼 int64 dtype 보존 포함).

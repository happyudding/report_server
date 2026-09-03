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
├── dist_blob.py        Distribution ECDF compact 공용 빌더 — 서버 폴백 계산과 (구) 클라
│                        dist blob 프리컴퓨트가 공유. 순수 모듈 — 클라에서 import 됨
├── dist_seq.py         **Serial 순**(rawdata 누적 순) 값 배열 빌더 — Distribution "Serial 순"
│                        토글 전용 배치 응답(`?order=seq`, 포맷 seq-columnar-v1). ECDF 는
│                        np.unique 로 동일값을 접어 순서를 버리므로 그 payload 로는 못 그린다.
│                        pack 지름길 없음(pack 은 정렬 산출물). ⚠ **tabs/ 로 옮기지 말 것**
│                        (perf_guard S01 → REPORT_SCHEMA_VERSION bump = 콜드 폭풍)
├── dist_dut.py         **DUT 별 분리** — 각 source 를 DUT 값별 pseudo-source
│                        (`"<src> · DUT <label>"`)로 쪼갠 배치 응답(`?dut=1`). 분할 규칙은
│                        만들지 않고 honeyform.dut_labels/split_table_by_dut 을 재사용한다
│                        (mode="DUT" 세션이 이미 쓰는 것과 같은 정본 — 규칙 #13). ECDF/seq
│                        본체도 기존 빌더 그대로. pack 지름길 없음(pack 은 count 집약이라
│                        DUT 축 소실). ⚠ `expand_bin1_sources` 를 빼면 Temperature
│                        "Bin1(RT만)" 이 조용히 안 걸린다(분할 후 이름이 달라서).
│                        ⚠ **tabs/ 로 옮기지 말 것** (perf_guard S01 → 콜드 폭풍)
├── gap_chart.py        **Gap Chart** — 사용자 수식(토큰 배열) 파서(재귀하강, eval 금지)
│                        + numpy 평가 + 좌표 교집합. 응답은 scatter_item 과 같은 구조라
│                        Item_detail 이 그대로 재사용한다. ⚠ **tabs/ 로 옮기지 말 것**
│                        (perf_guard S01 → REPORT_SCHEMA_VERSION bump = 콜드 폭풍)
├── formula.py          **신규 Item 수식 엔진** — Excel 풍 수식(IF/MIN/MAX/SUM/AVERAGE/
│                        ABS/ROUND/SQRT/AND/OR/NOT + 비교 6종 + 사칙연산)의 토큰 파서
│                        (재귀하강, eval 금지) + numpy 평가. 순수 모듈 — **Honey 클라
│                        (honey_ui/formula_editor.py · excel_edit/item_add.py)가 import**.
│                        gap_chart 파서의 확장 사본이며 드리프트는 tests/test_formula_item.py
│                        의 동치 테스트가 막는다. ⚠ **tabs/ 로 옮기지 말 것**
│                        (perf_guard S01 → REPORT_SCHEMA_VERSION bump = 콜드 폭풍)
├── dist_pack.py        Distribution **정렬 pack** 빌더/검증/ECDF 변환 (2026-07-23) —
│                        Honey 가 정렬(np.unique)까지 끝내 올리고 서버는 덧셈(cumsum)만.
│                        순수 모듈 — 클라 honey_main._build_webreport_dist_pack 이 import
├── dist_pack_store.py  pack **영구** 저장 (캐시 아님 — 축출·재시작에도 생존).
│                        <upload_root>/web_report/<akey>/dist_pack/<chash12>_<mode>/
│                        전처리 세션은 `_p<digest8>` 붙은 variant 를 서버가 1회 생성
│                        (service.materialize_dist_pack ← compute.request_dist_pack 큐)
├── validation.py       canon·mode/meta 정규화·client_identity — 순수 헬퍼 (werkzeug 는
│                        validate_meta 안 지연 import — 클라가 mode_tables 를 쓰기 때문)
├── edits.py            편집 상태 — 진실은 세션 단위 DB(report_webreport_edit). legacy 폴백/시드
├── ai_comment.py       eval_analyzer(eval_engine) 통합 접점 1/3 — IssueTable AI Comment
│                        (ai_comment 옵션 세션 콜드 빌드에서 evaluate() 호출 → docs/13)
│                        + **평가 범위 정본**: Issue Table 행 item 만 볼지의 플래그
│                        (env WEB_REPORT_EVAL_FAIL_ONLY) 와 스코프 산출 = fail item ∪ CPK
│                        섹션 후보(eval_fail_scope/_eval_items) — eval_debug 가 같은 함수를
│                        쓴다. 발화 판정은 엔진 threshold 가 정본(서버가 덧붙이지 않음)
│                        + Signature 컬럼 입력(row_signatures/signature_catalog)
├── eval_export.py      eval_analyzer 통합 접점 2/3 — IssueTable PTE/개발 comment 를
│                        eval.db 스키마 별도 DB(REPORT_EVAL_DB_PATH)로 export
│                        (업로드/편집 훅에서 export_async → docs/13 §9)
├── eval_debug.py       eval_analyzer 통합 접점 3/3 — 룰 경로/리로드 + L0~L6 트레이스
│                        (`/pe/eval` 관리자 패널 전용 → docs/13 §11). 운영 조회 경로가
│                        쓰는 것은 rules_rev() 하나 — cache_policy.report_key 가
│                        ai_comment 옵션 세션 키에 덧붙여 룰 편집을 재평가시킨다
├── metrics.py          build_report_payload — 공용 컨텍스트 조립 후 tabs.TAB_REGISTRY 순회
├── cache.py            인메모리 LRU 캐시 인프라 (레지스트리·락·무효화)
├── cache_policy.py     캐시 키 구성 규약의 단일 진실 (빌더 + 무효화 트리거 표)
├── disk_cache.py       계산 산출물(report/dist/map/temp_map) 로컬 디스크 캐시
├── response_cache.py    /full·/scatter 응답 gzip bytes LRU 캐시
├── compute.py          콜드 빌드 ProcessPool 오프로드 (prewarm 포함)
├── eta.py              콜드 빌드 **예상시간** 추정 (2026-08-05) — 로드 오버레이 "예상 약 N초"
│                        안내 전용(진행바 %는 종전 creep). parquet footer 로 규모(Mcells·kcols)
│                        를 디코드 없이 재고, build_log 실측으로 배율 하나를 학습해 사양차를
│                        흡수. 실패는 전부 None = 안내 생략 → docs/12
├── build_status.py     콜드 빌드 진행 상태 등록/조회 (`GET .../web_report/build_status` —
│                        202 를 받은 프런트가 진행률·단계를 폴링한다)
├── diag_yield_step.py  yield STEP 분해 진단 도구 (운영 조회 경로 아님 — 수동 실행)
├── build_log.py        콜드 빌드 **단계별 소요 + 대기 3종(큐/풀/IPC)** 기록 (2026-08-04).
│                        server/log/webreport_build_*.log JSON line · 실패(타임아웃·워커
│                        붕괴)도 기록 · 관리자 이력 탭 카드. 오프로드 빌드는 잡이
│                        (결과, timing) 튜플로 자식 시간을 부모에 실어 보낸다.
│                        + **실행 중 체크포인트**(2026-08-11) — 워커가 단계마다
│                        server/log/build_state_<pid>.json 을 원자적으로 덮어써, 타임아웃으로
│                        terminate 돼도 부모가 last_stage/last_source 를 건져 실패 레코드에
│                        남긴다(그게 없으면 300초를 어디서 썼는지 영영 모른다 → docs/20).
│                        ⚠️ source 단위 진행 표시는 `checkpoint()` 를 쓸 것 — 같은 이름으로
│                        `stage()` 를 중첩하면 소요가 2배로 누적된다
├── runtime.py          저장소 포트 주입 지점 (report_extension.init_app 이 주입)
├── ports.py            StoragePort/SessionRepo Protocol (DIP 경계)
├── rawedit.py          Raw Data 소스 내보내기/교체·삭제 헬퍼 (Excel 왕복 — 시트 삭제 시
│                        kept_indices 로 source 물리 제거 + manifest sources 축소).
│                        **신규 Item(수식) 추가**도 같은 replace_sources 를 탄다:
│                        `add_items`(manifest.selected_items 에 덧붙일 이름) +
│                        `rows_preserved`(열만 추가 → 전처리 셀 패치를 지우지 않는다)
├── rawvalues.py        Raw Data 편집 **값** 검증 — 셀 규칙(웹 400)·Excel 프레임 자동 교정/
│                        diff·경고 + 반영 확인 요약(build_confirm_sections=구조화/
│                        build_confirm_message=구 평문). 순수 모듈(셀 함수는 pandas 무의존,
│                        프레임 함수만 지연 import) — 클라 excel_edit/excel_session.py 가 import
├── preprocess.py       **조회 전처리** (항목 제외 + outlier `mean ± k·stdev` 마스킹, 2026-07-23
│                        + 셀 패치 `edits` · 조건 일괄 규칙 `rules`, 2026-07-28).
│                        원본 parquet 불변 — 세션 편집 DB(kind=preprocess)의 spec 을 loader 가
│                        조회 시점에 적용하고 digest 를 캐시 키에 덧붙인다(옵션 없으면 빈
│                        문자열 = 종전 키). 항목 제외는 item_columns 만 줄인다(메타·data 유지
│                        = selected_items 와 같은 의미론 — 안 그러면 Yield 표 합이 깨진다).
│                        edits/rules 는 그와 달리 data 프레임의 **값·행 자체**를 바꾼다.
│                        적용 순서 ① edits → ② rules → ③ exclude_items → ④ outlier.
│                        공개 API: normalize/digest/describe/describe_rule/normalize_where/
│                        match_rows/apply_tables. 순수 모듈 — Honey 허브·빠른 수정
│                        다이얼로그가 같은 코드를 돌려 값 일치를 구조적으로 보장
├── temperature.py      Temperature 모드(PMIC·SECURITY RT/CT/HT) — .lt/.pds limit 파서 + 업로드 전
│                        rawdata 정리(RT pass 좌표 필터 + RT limit 재판정 + bin 매칭).
│                        좌표가 없는 rawdata 는 `serial_match=True` 로 부르면 그 pair 만
│                        SERIAL 순서 짝짓기(적은 쪽 기준) — 판정은 `has_coords` 한 곳,
│                        확인창은 클라(honey_main `_temp_coord_check`)가 띄운다.
│                        순수 모듈 — Honey 클라 honey_main._clean_temperature_frames 가 import
├── trim_match.py       Trim 항목명 매칭 순수 모듈 (product_type 별 PMIC4/TV2 규칙셋)
├── comment_format.py   Issue comment 서식 토큰(*[..]=굵게 / *r[..]=색) strip — 색·굵기는
│                        웹 화면 전용이라 Excel·eval·챗봇으로 나갈 땐 본문만 남긴다.
│                        문법 정본은 sheets.js linkifyComment, 이 모듈은 strip 쪽 짝.
│                        순수 모듈 — Honey 클라 excel_download/_sheets.py 가 import
│                        → [docs/11](../docs/11_web_report_tabs.md)
├── wafer_frame.py      제품 기준정보(die pitch+wafer 크기) → 고정 map 프레임
└── tabs/               시트별 row 빌더 + TAB_REGISTRY (시트 구성 단일 진실)
    ├── __init__.py        TAB_REGISTRY / TabContext / TabSpec
    ├── common.py          json_safe/bin_sort_key/to_coord 공용 헬퍼
    ├── summary.py         build_summary_rows (placeholder)
    ├── raw_data.py        build_raw_data_rows(placeholder) + lazy 조회/편집
    ├── yield_tab.py       build_yield_rows / fail_counts / fail_bin_ranking / yield_overview
    │                       + **수율 분모 판정 정본** resolve_source_basis(소스별 Gross Die ↔
    │                       test die, 100% 초과·부족분 100 예외) / source_totals / auto_basis
    │                       + Temperature 소스 분류 temperature_corner_sources(RT / CT·HT)
    ├── temp_fail.py       **Temperature 전용** — CT/HT 를 RT limit 으로 **전 항목** 재판정
    │                       (조회 시점 서버 계산, 구 '첫 fail 하나만' 제한 없음).
    │                       compute_temp_fail 이 (count, die 인덱스)를 **한 순회로** 만들고
    │                       tables 클론에 캐시 → 표(build_temp_fail_rows)와 Map
    │                       (temp_fail_indices)이 같은 결과를 공유(판정 1회화)
    ├── cpk.py             build_cpk_rows(**Bin1 기준 단일** — 예외는 Temperature CT/HT:
    │                       **RT Bin1 die × RT limit**, temperature_reference_tables)
    │                       + CPK_THRESHOLD(1.33) + worst_cpk_by_subject
    ├── issue_table.py     build_issue_table_rows (Yield + cpk<1.33 + ETC, comment/Status/
    │                       숨김은 편집 DB). 모드 분기 없음 — Temperature 면 호출부가 RT
    │                       source 테이블·RT 기준 yield_rows 만 넘긴다(CT/HT 는 temp_fail.py)
    ├── distribution.py    build_distribution_index / scatter_item / build_distribution_compact (lazy)
    ├── trim_analysis.py   build_trim_payload / build_trim_chart (lazy)
    ├── commonality.py     search_chips / chip_percentiles [+ chip_percentiles_many =
    │                       배치, Item_detail 드래그 좌표 강조. 단건과 **값이 같아야 한다** —
    │                       tests/test_commonality_batch.py 가 전 die 를 대조]
    ├── compare.py         build_compare_payload (Compare 모드 — Before/After 그룹 N source.
    │                       common_map/bin_delta/bin_matrix=전 source, goodlog=그룹 대표 2개,
    │                       dist_shift(산포 비교 — Before 분모 지표 6종 meanshift σ/Cpk%/
    │                       stdev 증가율/median·IQR/KS D + 유의성 2종 + focus 판정)·
    │                       equivalence(동일성 검증 Grade1/2/3)=그룹 pool)
    ├── compare_issue.py    Compare 검출을 **이슈 표**로 (Distribution·ETC 시트).
    │                       row_key `CMPDIST|<item>` / `CMPETC|<item>` — 불변 저장 키.
    │                       ⚠ **TAB_REGISTRY 밖**이다: metrics.py 가 Compare 세션일 때
    │                       sheets["Issue Table Compare"] 에 직접 주입한다
    ├── significance.py     2표본 유의성 검정 (scipy 없이 — 불완전베타→Student-t CDF,
    │                       Welch t / Brown-Forsythe). dist_shift 의 **노이즈 게이트** 전용:
    │                       die 공간상관·거대 n 때문에 p 는 "노이즈다" 한 방향만 신뢰 가능
    └── Map_analysis.py    build_map_analysis_rows (wafer map die/bin 집계)
```

## 외부(밖) 연결점 — 참고만, 시그니처 변경 시 확인 필수

| 연결점 | 파일 (web_report 밖) | 용도 |
|--------|----------------------|------|
| 업로드 라우트 | [server/upload_webreport.py](../server/upload_webreport.py) | `POST /pe/report/upload_webreport` |
| 데이터/편집 라우트 | [server/report/routes_webreport.py](../server/report/routes_webreport.py) | `.../web_report/*` (CSRF + 편집자 가드) |
| 세션 상세 페이지 | [report_view.html](../server/report/report_view.html) + [static/webreport/](../server/report/static/webreport/) | 마크업+CSS / 탭별 JS — 파일 32개 중 31개 순서 로드(정본 표 [docs/11 §렌더 구조](../docs/11_web_report_tabs.md)) |
| 저장소(parquet/manifest) | [storage_gateway](../server/storage_gateway/__init__.py) | `save/load_webreport_sources`. **직접 import 금지** — `runtime.storage()` 포트로 접근 |
| DB CRUD | [database/report_db.py](../server/database/report_db.py) | create/update_session, log_audit, get/apply_webreport_edits, get_webreport_edit_rev |
| eval_analyzer 엔진 | [eval_analyzer/](../eval_analyzer/) `eval_engine` | **ai_comment.py(evaluate, persist=False) + eval_export.py(store·ingest 헬퍼) + eval_debug.py(룰 리로드·트레이스) 3곳에서만 import** (단방향) → [docs/13](../docs/13_eval_analyzer_integration.md) |
| eval 룰 관리자 패널 | [server/eval_panel/](../server/eval_panel/) | `/pe/eval` — eval_debug 경유로만 엔진 접근 (직접 import 금지) |
| 관리자 Eval DB 탭 | 구현 [server/admin_panel/eval_admin.py](../server/admin_panel/eval_admin.py) · 화면/라우트 [server/eval_panel/](../server/eval_panel/) (2026-08-03 `/pe/eval` 로 이관) | eval_export.open_conn/db_path 재사용 (overview/목록/삭제/재적재) |

web_report 안에서 위 연결점의 **호출 시그니처(함수명·인자·반환 dict 키)** 를 바꾸면 바깥
파일도 맞춰 고쳐야 하므로, 그 경우 함께 반영할 것.

## 작업 규칙

- **소유권/수정 권한 경계** (정본 [../docs/15_ownership.md](../docs/15_ownership.md)):
  `web_report/`(여기) + web_report 관련 html(report_view.html, static/webreport/) + `server/`
  (단 `storage_gateway/` 제외) + client 자주 쓰는 영역(honey_ui·honey_main·transport·excel_*)
  은 🟢 자유 수정. `client/` 나머지 비동결은 🟡 사전 승인.
  🔒 외부 담당자 영역(건들 때 승인) = `d1/`·`report_generator/`·`honey_parse/`·`storage_gateway/`.
- **분할 JS 는 classic script 순서 로드(전역 스코프 공유)** — ES module 로 바꾸거나 로드
  순서를 바꾸지 말 것.
- 세션 상세 UI 를 고칠 때 사용자가 "세션 페이지"라고 하면 우선 report_view.html(web_report
  밖!)을 의미하는지 확인할 것 — 헷갈리기 쉬운 지점.
- `../CLAUDE.md` §5 불변 규칙(원본 xlsx 미저장, `report_` prefix, analysis_key 산출,
  Distribution 다운샘플 금지)은 web_report 흐름에도 동일 적용.
- **값 검증은 `rawvalues.py` 에만 둔다** — `honeyform.validate_honeyform_df` 는 조회 경로
  (`_decode_parts`)가 저장된 parquet 을 읽을 때마다 호출하므로, 거기에 값 규칙을 넣으면
  기존 세션이 열리지 않는다. 프런트(raw_data.js)와 판정이 갈리면 사용자가 통과시킨 값이
  400 으로 튕기므로 `_NUM_RE` ↔ `RAW_NUM_RE` 는 **문자 그대로 동일**하게 유지할 것.
- **검증 기준**: tabs 통계·honeyform 변환을 고칠 땐 "같은 세션 payload 정준 JSON 완전
  일치"로 회귀 없음을 확인한다(값 불변 — 정수 컬럼 int64 dtype 보존 포함).

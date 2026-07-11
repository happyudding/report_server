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

### 분석 모드 (Normal / Compare / DUT / Commonality) — 2026-07 추가

세션마다 **분석 모드**를 가진다. Honey 클라 업로드 시점에 확정(파일 개수로 가용 모드 제한:
Compare=정확히 2개(서버 ingest 도 2개 아니면 400 거부), DUT/Commonality=1개)되어
`manifest.mode` 로 전송되고 `report_session.mode`
컬럼(외부 `report_db.py`)에 저장된다. mode 는 **analysis_key 산출에 불포함**(신원과 동일 —
같은 데이터면 mode 달라도 같은 key), 대신 report/dist/scatter 캐시 키에 포함한다
(키 구성 규약: `cache_policy.py`).

- **Normal**: 기존 동작(회귀 없음). payload 에 `"mode":"Normal"` 만 추가.
- **DUT**: 업로드된 단일 honeyform 을 **서버에서** DUT 컬럼으로 분할(`honeyform.split_table_by_dut`,
  클라 분할 아님 — df_honey→honeyform 포맷 변환 회피). `service._mode_tables` 가 load/dist/scatter
  경로에서 tables 를 DUT별 pseudo-source(`DUT <값>`)로 바꿔 기존 multi-source 렌더 재사용.
- **Compare**: source 2개↑일 때 `tabs/compare.py::build_compare_payload` 가 `report["compare"]`
  로 통계 delta(cpk_rows pivot)/bin delta/공통·비공통 fail map(좌표 교차) 제공. 프런트
  `renderCompare`(report_view.html) 가 Compare 탭에 delta 표 + 공통성 map(비공통 fail 을 어느
  source 에서만 fail 인지 색 구분) + source map 나란히 렌더.
  **goodlog (2026-07-09, Honey Compare Mode 이식)**: 정확히 2 source 일 때
  `tabs/compare.py::build_goodlog` 이 테스트 프로그램 diff 를 `compare["goodlog"]` 로 추가 —
  after=첫째/before=둘째 파일, 항목명·lolimit·hilimit 일치 여부(True/False) + 공통 die
  (또는 각자 Bin1 최상단 행) reference 값 기준 gap%, difflib 정렬로 한쪽만 있는 항목은 한쪽
  셀만 채움. 프로그램 완전 동일이면 `identical: true`(프런트 '차이 없음' 배너). 이름 같고
  limit 만 바뀐 항목은 `limit_change_map` 으로 내려가 프런트 `beforeLimitShapes` 가 모든
  distribution 차트(갤러리/미니셀/상세 CDF·히스토그램)에 before limit 회색 점선을 덧그림.
  goodlog 표 렌더는 `goodlogSectionHtml`(report_view.html). 신규 업로드는 클라
  `_validate_web_mode`(honey_main.py)와 서버 ingest 가 2개만 허용하고, legacy 3-source
  세션은 goodlog=None 으로 기존 비교 탭만 유지된다.
- **Commonality**: 1 source. `tabs/commonality.py::search_chips`(serial/xpos/ypos/dut 검색) +
  `chip_percentiles`(선택 chip 의 항목별 값·누적%(ECDF 위치)·wafer 좌표). 라우트
  `.../web_report/commonality/chips`·`/chip`(외부 report_routes.py, 읽기 전용). 프런트
  `renderCommonality` 가 chip 검색·행선택 → wafer 강조 + 항목별 ECDF 를 chip 백분위 기준
  색 분리 렌더. chip 선택은 view-time(비영속).

프런트 탭 노출: `syncTabVisibility` 가 `webReportMode()` 로 Compare/Commonality 탭을 각 모드에서만
표시. 상단 `renderMeta` 는 Normal 이 아닐 때 mode 배지 표시.

## 1. 디렉토리 인덱스

```
web_report/
├── __init__.py        패키지 docstring만
├── service.py          조회/편집 오케스트레이션 (외부 진입점 — 공개 함수 시그니처 불변,
│                        ingest_webreport 는 ingest.py 재노출):
│                        load_webreport() — 세션 재계산 조회(report dict 를
│                        (akey, chash, session_id, edits_rev, opts, mode) 키로 LRU 캐시 —
│                        키 규약은 cache_policy.py. tables 로드는 실제 재계산이 필요할
│                        때로 지연, 콜드 빌드는 compute.py 워커로 오프로드 가능),
│                        get_distribution()/get_distribution_gzip() — Distribution ECDF lazy
│                        조회(컴팩트 columnar, gzip bytes 캐시), raw_data 조회·편집,
│                        commonality 검색/백분위, update_issue_etc_items/update_issue_comments/
│                        update_summary_engr/update_trim_overrides (편집 — **세션 편집 DB**
│                        report_webreport_edit 에 저장, manifest 불변 · 2026-07-11),
│                        invalidate_caches (cache.py 위임 — 외부 호출부용 진입점 유지)
├── ingest.py           업로드 ingest (2026-07-11 분리) — 해시→analysis_key→storage 저장→
│                        DB 세션 생성→manifest 편집값 시드→프리웜(compute.prewarm — 풀 제출,
│                        동시성 상한 = 워커 수). 검증 겸 decode_split(keep_df=False) 결과를
│                        TABLES_CACHE 에 시딩해 첫 조회 재디코드를 없앤다
├── edits.py            편집 상태 (2026-07-11) — issue_comments/etc_items/trim_overrides/
│                        summary_engr 의 진실은 세션 단위 DB(report_webreport_edit).
│                        dedup(동일 akey) 세션 간 비공유. legacy(rev==0)는 manifest 폴백
│                        (effective_state) + 첫 편집 직전 자동 시드(ensure_seeded).
│                        일괄 이전 도구: server/tools/migrate_manifest_edits.py (실행 완료)
├── cache.py            인메모리 LRU 캐시 인프라:
│                        TABLES/DIST/REPORT/COMMONALITY/TRIM/MANIFEST 캐시 + 공유 CACHE_LOCK +
│                        single-flight keyed_lock + AKEY_CACHES 레지스트리(register_akey_cache)
│                        + evict_akey_caches(편집용, manifest 유지)/invalidate_caches(세션삭제용).
│                        tables 캐시는 tables_cache_put 전용 — 개수(WEB_REPORT_TABLES_CACHE=4)
│                        + 추정 바이트(WEB_REPORT_TABLES_CACHE_MB=4096) 이중 상한 (Phase 5)
├── cache_policy.py     캐시 키 구성 규약의 **단일 진실** (2026-07-11) — 캐시별 키 빌더 +
│                        무효화 트리거 표. 새 캐시·키는 반드시 여기 빌더로 만들 것
├── compute.py          콜드 report/dist/trim 빌드 오프로드 (ProcessPoolExecutor,
│                        WEB_REPORT_COMPUTE_WORKERS=2, 0=인라인). tables 캐시가 따뜻하면
│                        인라인, 워커 붕괴 시 인라인 폴백. prewarm 도 풀 제출
├── runtime.py          저장소 포트 주입 지점 (컴포지션 루트: report_extension.init_app —
│                        web_report 는 storage_gateway 를 직접 import 하지 않는다)
├── ports.py            StoragePort/SessionRepo Protocol (DIP 경계 정의)
├── validation.py       canon(정준 JSON)·validate_mode/mode_tables(DUT 분할)·validate_meta·
│                        client_identity·webreport_colors — 캐시·저장소 무의존 순수 헬퍼
├── loader.py           load_tables() — 세션 → parquet 다운로드·디코드 → HoneyformTable 리스트
│                        (TABLES_CACHE 결합, 반환은 clone_table 클론 — data 읽기 전용 공유.
│                        읽기 경로는 keep_df=False 슬림 디코드(df=None), df 를 고치는 편집
│                        경로는 use_cache=False + keep_df=True)
├── response_cache.py    /full·/scatter 응답의 JSON+gzip bytes LRU 캐시 (_FULL_CACHE /
│                        _SCATTER_CACHE, env WEB_REPORT_FULL_CACHE(8)/WEB_REPORT_SCATTER_CACHE(16)).
│                        cache.register_akey_cache 로 등록되어 편집·세션삭제 무효화에
│                        자동 편입. /full 캐시 키에는 manifest digest + extras(annotations 등
│                        값싼 부분) digest 가 포함되어 comment/annotation 편집이 자연 무효화됨.
├── disk_cache.py        계산 산출물(report json.gz / dist gzip) 로컬 디스크 캐시 (2026-07-10).
│                        <upload_root>/web_report/<akey>/cache/ 에 저장 — 서버 재시작·LRU
│                        퇴출에도 콜드 재계산(수 초)이 디스크 읽기(수십 ms)로 끝난다.
│                        파일명에 content_hash+키 digest 포함(자연 무효화), 세션 삭제는
│                        storage_gateway 가 akey 폴더째 지워 별도 훅 불필요. 총량 상한 env
│                        WEB_REPORT_DISK_CACHE_MAX_GB(기본 500, 0 이하=비활성) — 쓰기 후
│                        백그라운드 스레드가 mtime 오래된 순 삭제, 히트는 mtime 갱신.
├── honeyform.py         7-meta honeyform 검증/파싱, parquet 인코딩·디코딩
│                        (META_COLUMNS, META_ROW_LABELS 등 스키마 상수).
│                        2026-07-10 벡터화: per-column .loc 루프 제거(numpy 블록 변환,
│                        정수 컬럼 int64 dtype 보존 위해 per-column to_numeric 은 유지),
│                        decode_split_honeyform_parquet(keep_df=) — decode+split 결합 빠른
│                        경로. keep_df=False(읽기/캐시)면 전체 object 프레임(df)을 만들지
│                        않아 메모리 절반↓ (payload 정준 JSON 완전 일치 검증됨, Phase 5)
├── metrics.py           build_report_payload() — 공용 컨텍스트(yield/cpk 등 1회 계산)를
│                        조립해 tabs.TAB_REGISTRY 를 순회 (개별 탭 이름을 모름 — 시트
│                        구성의 단일 진실은 tabs/__init__.py 레지스트리)
├── wafer_frame.py       제품별 기준정보(PRODUCT_WAFER_REF: die pitch+wafer 크기) → 고정 map
│                        프레임 계산 frame_for(). die pitch 입력된 제품만 Map_analysis 가
│                        틀을 고정(부분 데이터 방지), 없으면 현행(데이터 min/max) 유지
├── trim_match.py        Trim Analysis 항목명 매칭 순수 모듈 (Flask/pandas 무의존 — 재사용용).
│                        product_type 별 규칙셋 2종: PMIC4(PMIC/SECURITY/TCON, 기본) =
│                        정규화→토큰→INIT/CODE/TRIM/VERIFY 판정→stem→orphan 병합,
│                        TV2(MDDI/PDDI) = 원본 이름의 마커(fuse_/OTP_→무조건 TRIM)/접미사
│                        (_p1/_trim/_pre→TRIM, _p2/_post→VERIFY) 판정, MDDI 는 제외어
│                        (CPU80/SCAN/RAM/IOFF). 수동 재배치(세션 편집 DB, kind=trim_override)는
│                        build_groups 가 자동 결과 위에 우선 적용
└── tabs/                시트별 row 빌더 + **탭 레지스트리** (__init__.py TAB_REGISTRY 가
                         시트 구성 단일 진실 — 새 탭 = 빌더 모듈 1 + TabSpec 1줄 + 프런트 JS 1개)
    ├── common.py        json_safe/fmt_type/num/round_num, bin_sort_key(BIN 정렬 공용),
    │                     to_coord((XPOS,YPOS)→int 좌표 정규화 — compare/commonality 공용)
    ├── summary.py        build_summary_rows (placeholder — return [])
    ├── raw_data.py       build_raw_data_rows(payload 용 placeholder — return []) +
    │                     build_raw_data_columns/query_raw_data/apply_raw_data_edits (lazy-load 조회·편집)
    ├── yield_tab.py      build_yield_rows(comment/count 컬럼 없음), fail_counts_by_source, fail_bin_ranking, yield_overview(상단 요약 박스)
    ├── cpk.py            build_cpk_rows (source 별 행만 — "total" 합산 행 없음) +
    │                     CPK_THRESHOLD(1.33)·worst_cpk_by_subject (Issue Table/Distribution 공용
    │                     — subject 당 worst-case(최저) cpk 로 이슈 판단)
    ├── issue_table.py    build_issue_table_rows (Yield 파생 행 + CPK<1.33(subject 별 worst-case) 파생 행 +
    │                     ETC). PTE/개발 comment 는 세션 편집 DB(edits.py)에서 row_key 로 채움
    ├── distribution.py   build_distribution_rows (전량 ECDF — 프런트는 아직 렌더 안 함)
    ├── trim_analysis.py  build_trim_payload(항목 매칭+슬롯별 cpk._stats+initial shift 판정) /
    │                     build_trim_chart(그룹 1개 chip-to-chip 차트, base 값 서버 정렬, 전 die)
    │                     — lazy 전용, /full payload 의 sheets["Trim Analysis"] 는 항상 []
    └── Map_analysis.py   build_map_analysis_rows(tables, product_type, product) — wafer map
                          die/bin 집계. 제품 기준정보 있으면 wafer_frame 고정 프레임으로 틀 덮어씀
```

## 2. 외부(밖) 연결점 — 여기는 참고만, 수정 시 확인 필수

| 연결점 | 파일 (web_report 밖) | 용도 |
|--------|----------------------|------|
| 업로드 라우트 | `server/upload_webreport.py` | `POST /pe/report/upload_webreport`, `GET /pe/report/web_report/<sid>` → `/pe/report/view/<sid>` 리다이렉트 |
| web_report 편집 라우트 | `server/report/routes_webreport.py` (집결자 report_routes.py) | `.../web_report/raw_data`(조회), `.../raw_data/edit`, `.../issue_table/etc`, `.../issue_table/comments`, `.../summary/engr`, `.../trim/overrides` (CSRF + HoneyUser 편집자 가드) |
| 세션 상세 페이지 | `server/report/report_view.html` + `server/report/static/webreport/*.js` | 마크업+CSS 는 html, 탭별 JS 는 static/webreport 15개 모듈 (2026-07-11 분할 — 순서 로드, 전역 공유) |
| 저장소(parquet/manifest) | `server/storage_gateway/__init__.py` | `save/load_webreport_sources`(parquet+manifest, 저장 위치 기록). **직접 import 금지** — `runtime.storage()` 포트로 접근 (`save_webreport_manifest` 는 manifest 불변화로 미사용) |
| DB CRUD | `server/database/report_db.py` (facade) | `create_session`, `update_session`, `get_all_object_infos`, `log_audit`, `get/apply_webreport_edits`, `get_webreport_edit_rev` |
| 공통 설정 | `server/config.py` | `REPORT_UPLOAD_DIR` 등 |

`web_report/` 안에서 위 연결점의 **호출 시그니처(함수명·인자·반환 dict 키)** 를 바꾸면
바깥 파일도 맞춰 고쳐야 하므로, 그 경우도 먼저 사용자에게 알릴 것.

**예외 — `server/report/report_view.html` + `static/webreport/*.js`**: web_report 각 탭의
실제 화면 UI(체크박스 목록 스타일, 표 컬럼 순서/정렬 화살표/테두리, 글자크기, 필터/버튼
등)는 이 파일들에 구현되어 있다 (web_report/tabs/ 는 데이터만 공급 — 2026-07-11 분할로
JS 는 static/webreport/ 탭별 모듈, html 은 마크업+CSS). web_report 탭 UI 작업 범위 안의
수정이라면 이 파일들을 매번 승인받지 않고 바로 Edit 해도 된다 (2026-07-07 사용자 승인,
분할 후에도 동일 적용). 단, DB/세션/인증 관련 로직(verify_password, 세션 삭제 등 web_report
탭과 무관한 부분)을 바꿔야 하면 여전히 먼저 물어볼 것. **분할 JS 는 classic script 순서
로드(전역 스코프 공유)** — ES module 로 바꾸거나 로드 순서를 바꾸지 말 것.

## 3. 작업 시 유의

- `report_server/CLAUDE.md` §5 의 불변 규칙(원본 xlsx 미저장, `report_` prefix, analysis_key
  산출 방식 등)은 web_report 흐름에도 동일하게 적용된다.
- **성능 작업 (2026-07-10)**: 세션 로딩(콜드 14.8s→0.1s/재계산 7s, 웜 페이지 1.34s→0.78s)·
  최초 생성(업로드→/full 6.9s→4.5s) 최적화 완료 — 서버 벡터화(cpk `_stats_batch`,
  honeyform numpy 블록 변환)·disk_cache.py·ingest tables 시딩·report_view.html 선행
  fetch+distribution Worker 청크 로드·honey_main 분석/인코딩 대화상자 병렬화.
  **tabs/ 통계·honeyform 변환 로직을 고칠 땐 "같은 세션 payload 정준 JSON 완전 일치"가
  검증 기준** (벡터화는 값을 바꾸지 않는다 — 정수 컬럼 int64 dtype 보존 포함).
- **리팩토링 (2026-07-11)**: 편집 상태 세션단위 DB화(edits.py — manifest 불변)·저장 포트
  주입(runtime.py)·캐시 키 규약 명문화(cache_policy.py)·슬림 tables 캐시(keep_df=False +
  바이트 상한)·콜드 빌드 워커 오프로드(compute.py)·탭 레지스트리(tabs.TAB_REGISTRY)·
  report_view.html JS 15분할. 전부 payload 정준 JSON 완전 일치로 검증됨.
- 세션 상세 UI를 고칠 때 사용자가 "세션 페이지"라고 하면 우선
  `server/report/report_view.html` (web_report 밖!) 을 의미하는지 확인할 것 — 헷갈리기
  쉬운 지점. (구 `html.py::render_report_html` 은 미사용 코드라 2026-07-09 삭제됨.)
- **탭 구현 상태 (2026-07-08 기준)**: Yield / CPK / Issue Table / Map Analysis / Fail Bin 은
  계산·렌더 완료 (CPK 탭 렌더 함수 `renderCpk()` 는 report_view.html 에 추가 — web_report 밖
  파일이라 사용자 승인 후 수정함). Raw Data 는 lazy-load 조회/편집 완료. Distribution 은
  산포 탭으로 구현 완료 — `tabs/distribution.py` 가 `build_distribution_index`(항목별
  test_num·worst-case cpk·fail(FAILTNO==TNO 귀속)·status) / `scatter_item`(상세용 전체 측정값) /
  `build_distribution_compact`(ECDF 전 포인트 컴팩트 columnar, lazy 전용)를 공급한다. metrics 가
  `report["distribution_index"]` 로 내려주고, 상세 전체점은 `GET .../web_report/scatter/<subject>`
  (report_routes.py, 승인 후 추가)로 지연 로드. 프런트(report_view.html `renderDistribution`)는
  툴바(전체/cpk<1.33/Fail Only, 기본 cpk<1.33)+가상스크롤 갤러리+Typeahead+카드→상세(CDF+히스토그램)
  로 렌더. 미니셀만 1000점 다운샘플(상세·통계는 전체점). **Distribution embed 폐지(2026-07-08)**:
  `sheets["Distribution"]` 은 항상 `[]`(`distribution_deferred=True` 고정), 구
  `build_distribution_rows` embed 는 제거됨. 프런트가 첫 페인트 후
  `GET .../web_report/distribution` (`build_distribution_compact` 의 컴팩트 columnar,
  전 포인트·gzip·ETag)을 백그라운드 fetch 해 `distDataCache` 를 채운다 — 도착 전 그려진
  미니셀/갤러리는 `refreshDistConsumers()` 가 다시 채움. Issue Table 미니셀/Bin 상세 분포도
  이 `distDataCache` 를 산포 갤러리 카드 포맷(표시용 다운샘플 static CDF)으로 렌더.
  **Issue 미니셀은 lazy 렌더(2026-07-08)**: 항목 수 규모(수천 셀)라 전량 동기 렌더 시
  메인스레드가 분 단위로 얼어붙어(실측 264s), 갤러리와 같은 IntersectionObserver + rAF
  분할(`renderIssueMiniDist`→`issueDistQueueRender`/`issueDistFlush`, 화면 밖 purge)로
  보이는 셀만 그린다 — refreshDistConsumers 도 visible 셀만 재큐잉. report_view.html 의 `renderActive` 는
  활성 탭만 즉시 렌더하고 나머지는 tabDirty + requestIdleCallback 프리렌더(`schedulePrerender`).
  summary / raw_data(payload) 빌더는 `return []` 플레이스홀더 (Summary 탭 화면은 프런트가
  Map Analysis + Fail Bin 시트로 자체 구성. tabs/histogram.py 는 어디서도 import 되지 않아
  삭제됨).
- **Trim Analysis 탭 (2026-07-10 구현)**: 독립 화면 3개(① 항목 매칭 ② 산포 분석 ③ 분석
  리포트). payload 는 /full 에 싣지 않고(`sheets["Trim Analysis"]`=[] 고정) 탭 첫 진입 시
  `GET .../web_report/trim_analysis?source=`(gzip+ETag, ETag 에 편집 rev 토큰 포함)로 lazy
  로드, 그룹 차트는 `GET .../web_report/trim_chart?source=&group=` 를 프런트가 동시 8개 큐로
  병렬 로드(클라 캐시, 화면 밖 Plotly.purge). 매칭 규칙은 `trim_match.py`(product_type 별
  PMIC4/TV2), 통계는 `tabs/trim_analysis.py`(cpk._stats 재사용 + initial shift: base 분포
  p20~p80 vs target 평균, PMIC4=(INIT,TRIM)/TV2=(TRIM,VERIFY)). 드래그앤드랍 수동 재배치는
  `POST .../web_report/trim/overrides`(CSRF+편집자 가드)로 **세션 편집 DB**
  (kind=trim_override, 2026-07-11)에 저장(parquet·manifest 불변). 캐시는 cache.py
  `TRIM_CACHE`((sid, edits_rev) 포함 키 — cache_policy.trim_key)/`TRIM_CHART_CACHE`
  (슬롯 구성 digest 키). Excel 내보내기는 vendored
  `server/report/vendor/exceljs.min.js` 를 첫 클릭 시 동적 로드해 브라우저에서 생성(서버
  openpyxl 금지 규칙 준수), 차트 PNG 클립보드 복사는 비보안 컨텍스트에서 다운로드 폴백.
  다운샘플 없음 — n>2000 은 scattergl 로 렌더만 가속.
- **Issue Table comment 저장**: web_report 세션은 legacy 의 `PATCH /content` 를 쓰지 않는다
  (해당 라우트는 항상 405 비활성). PTE/개발 comment 는 프런트가
  `POST .../web_report/issue_table/comments` 로 보내고 **세션 편집 DB**
  (report_webreport_edit, kind=issue_comment — 2026-07-11)에 row_key
  (`Yield|<bin>|<item>` / `CPK|<item>` / `ETC|<item>`) 단위로 저장 → 조회 시
  build_issue_table_rows 가 다시 채운다. sources parquet · manifest 는 불변.
  dedup(동일 akey) 세션 간 편집 비공유 — legacy 세션은 rev==0 manifest 폴백 + 첫 편집 시
  자동 시드 (edits.py 참조).
- **legacy 세션 탭**: web_report 전용 탭(Raw Data/CPK/Map Analysis)은 `source != "web_report"`
  세션에선 프런트가 탭 버튼을 숨긴다(`syncTabVisibility`).

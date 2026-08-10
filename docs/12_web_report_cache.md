# 12 · web_report — 캐시 계층 & 컴퓨트

> web_report 조회 성능의 핵심. 캐시 3계층·무효화 규칙·환경변수·컴퓨트 오프로드.
> 관련: 파이프라인 [10](10_web_report_pipeline.md) · 탭 [11](11_web_report_tabs.md)

**단일 프로세스(waitress 1 process) 전제** — manifest write-through 일관성의 근거다.

## 캐시 3계층
1. **인메모리 LRU** ([cache.py](../web_report/cache.py)) — 프로세스 RAM. decoded tables·
   파생 산출물(report/dist/commonality/trim/manifest)을 담는다. 상한 초과 시 오래 안 쓴
   항목부터 자동 퇴출.
2. **디스크 캐시** ([disk_cache.py](../web_report/disk_cache.py)) — report JSON·dist gzip 을
   `<upload_root>/web_report/<akey>/cache/` 에 파일로도 남긴다. 서버 재시작·LRU 퇴출에도
   콜드 재계산(수 초)을 디스크 읽기(수십 ms)로 대체. 총량 상한 초과 시 mtime 오래된 순 삭제.
3. **응답 캐시** ([response_cache.py](../web_report/response_cache.py)) — `/full`·`/scatter`
   응답의 최종 JSON+gzip bytes 를 캐시해 warm 요청을 bytes 반환만으로 끝낸다.

세션 삭제는 `storage_gateway.delete_report_artifacts` 가 akey 디렉토리째 지우므로 디스크
캐시에 별도 삭제 훅이 필요 없다. 디스크 캐시는 재계산 가능한 파생물이라 모든 실패는
조용히 무시(best-effort).

## Distribution pack — 캐시가 아닌 영구 파생 데이터 (2026-07-23)

**Distribution 은 서버 최대 병목이었다** — 항목×소스마다 `np.unique`(정렬)를 돌리는 콜드
빌드가 수십 초 CPU + GiB급 RAM 스파이크를 냈고, 스크롤할 때마다의 `distribution_batch` 도
tables 에서 매번 다시 정렬했다. 이제 **Honey 가 업로드 시점에 정렬·중복묶기까지 끝낸
pack** 을 올리고, 서버는 조회 때 **덧셈(cumsum)만** 한다.

- 저장 위치: `<upload_root>/web_report/<akey>/dist_pack/<chash12>_<mode>/`
  (`index.json` + `chunk_<n>.gz`, 항목 30개/chunk = 프런트 배치 크기).
  **`cache/` 하위가 아니라 축출 대상 밖**이고 세션 삭제(akey 디렉토리째) 시 함께 지워진다.
  디렉토리명의 content_hash 가 곧 무효화 수단 — raw 편집 후엔 구조적으로 조회되지 않고
  `dist_pack_store.delete_stale` 이 구 세대를 회수한다.
- pack 내용: 항목·소스별 `x`(round6 고유값) + `c`(전체 count) + `c1`(bin1·규격내 count).
  y(누적%)는 저장하지 않는다 — count 에서 만들고, bin1 은 `c1>0` 행만 골라 같은 방법으로
  만들면 "필터 후 unique" 와 결과가 수학적으로 동일하다.
- 값 일치: `dist_pack.ecdf_from_pack_items` 결과는 서버 폴백
  (`tabs.distribution.build_distribution_compact`)과 **정준 JSON 완전 일치**여야 한다.
  항목 정렬(사전순)·소스 순서·반올림 순서를 바꾸면 깨진다 (`tests/test_dist_pack.py`).
- **응답 형식은 종전과 동일**(`ecdf-columnar-v1`) — 프런트는 pack 세션인지 알지 못한다.
- **전처리(preprocess) 세션은 전용 variant 를 쓴다** (2026-07-23). Honey 가 올린 pack 은
  업로드 시점(전처리 없음) 기준이라 항목 제외·outlier 가 반영돼 있지 않다. 그래서 그 spec
  전용 pack 을 **서버가 1회 만들어** `<chash12>_<mode>_p<digest8>/` 에 영구 저장하고, 이후
  조회는 원본 pack 과 똑같이 덧셈만 한다.
  - 생성은 백그라운드 전용 큐(`compute.request_dist_pack` → `service.materialize_dist_pack`)
    — 프리웜 큐(포화 시 폐기·중복 허용)와 온디맨드 큐(사용자가 202 를 기다리는 경로)를
    쓰지 않는다. 예약 지점은 전처리 저장 시점과, variant 가 아직 없는 Distribution 조회.
  - 생성 전(첫 조회)이나 실패 시엔 종전대로 폴백 계산한다 — 사용자는 기다리지 않는다.
  - 값 일치 근거는 **입력 tables 가 같다는 것**이다. 폴백도 variant 빌드도
    `loader.load_tables(apply_prep=True)` 결과를 받는다(전처리 로직 재구현 없음).
  - spec 을 바꾸면 digest 가 바뀌어 새 variant 가 만들어지고, 구 variant 는
    `dist_pack_store.delete_variant` 로 회수한다(chash 가 그대로라 `delete_stale` 로는
    안 지워진다). 전처리를 해제하면 digest 가 빈 문자열이라 원본 pack 으로 자동 복귀.
- **웹 셀 편집(`service.edit_raw_data`) 후에도 서버가 새 세대 pack 을 다시 만든다** — 편집으로
  content_hash 가 바뀌면 구 pack 은 회수되는데, 종전에는 Honey 로 Excel 왕복을 다시 하기
  전까지 그 세션이 영구히 폴백 계산으로 열렸다. 이제 `request_dist_pack(base=True)` +
  `compute.prewarm` 을 백그라운드로 예약한다(응답은 기다리지 않는다).
- 미첨부(구 Honey)·검증 실패·chunk 손상은 전부 조용히 **기존 계산 폴백** — 기존 세션은
  종전과 완전히 동일하게 열린다.
- 구 `dist_blob` 시딩 경로(2026-07-15, DIST_CACHE+disk 시딩)는 구 Honey 하위호환으로 남는다.

## 캐시 키 규약 (단일 진실 = cache_policy.py)
[cache_policy.py](../web_report/cache_policy.py) 가 캐시별 키 빌더를 제공하고 **호출부는
반드시 이 빌더로 키를 만든다**. 새 캐시를 추가하면 여기 빌더와 아래 표를 함께 추가할 것.

> **bin1 변형 키 꼬리표** (`cache_policy._bin1_suffix`): `bin1=True` 면 `("bin1",)`,
> 거기에 `bin1_scope="rt"`(Temperature "Bin1(RT만)" — RT 소스만 양품 필터, CT/HT 는
> fail 포함 전체)면 `("rt",)` 를 더 붙인다. **scope 가 비면 종전 키와 완전히 동일**해
> 기존 캐시가 그대로 유효하다. scope 는 실제로 적용되는 세션(Temperature + RT 존재)일
> 때만 키에 들어간다(`service._bin1_source_filter` 가 판정).

| 캐시 | 키 구성 | 무효화 트리거 |
|------|---------|---------------|
| TABLES_CACHE | (akey, chash[, prep]) | raw_data 편집(chash) / 전처리 / 세션 삭제 |
| DIST_CACHE | (akey, chash[, prep], mode) | 〃 (mode 는 세션 생성 후 불변) |
| _DIST_BATCH_CACHE | (akey, chash[, prep], mode, subjects_digest[, "bin1"[, scope]]) | 〃 — 항목 배치 ECDF gzip (`/web_report/distribution_batch`) |
| MAP_CACHE | (akey, chash[, prep], mode) | 〃 — Map dies gzip (`/web_report/map_analysis`, schema v8). **report 콜드 빌드가 `service.seed_map` 으로 RAM+디스크를 함께 채운다** (아래 "Map dies 시딩" — Map 3초 SLA) |
| TEMP_MAP_CACHE | (akey, chash[, prep], mode, v) | 〃 — Temperature 항목별 fail die **인덱스** gzip (`/web_report/temp_map`, 2026-08-05). map dies 와 같은 세대여야 인덱스가 맞는다. **report 콜드 빌드가 `service.seed_temp_map` 으로 RAM+디스크를 함께 채운다**(같은 판정 결과 재사용) — 라우트 단독 콜드는 디스크 → 워커 오프로드(`compute.temp_map_job`) 순 |
| COMMONALITY_CACHE | (akey, chash) | raw_data 편집 / 세션 삭제 (메타만 쓰므로 전처리 무관) |
| REPORT_CACHE | (akey, chash, sid, edits_rev, opts, mode) | comment/override/전처리 편집(rev) + 위 전부 |
| TRIM_CACHE | (akey, chash, sid, edits_rev, mode, source) | trim override/전처리 편집(rev) + 위 전부 |
| TRIM_CHART_CACHE | (akey, chash[, prep], mode, source, items_digest) | 그룹 슬롯 구성 변경 / raw_data 편집 — 단일 `/trim_chart` 와 배치 `/trim_chart_batch` 가 **같은 엔트리를 공유**한다(배치는 그룹별로 이 캐시를 조회·적재할 뿐) |
| _FULL_CACHE | (akey, chash, "sid:edits_rev", extras_digest) | 편집 rev / annotations 등 extras |
| _SCATTER_CACHE | (akey, chash[, prep], mode, subject) | raw_data 편집 / 전처리 / 세션 삭제 |

공통 규약:
- **모든 키의 첫 요소는 analysis_key** — `AKEY_CACHES` 무효화(`evict`/`invalidate`)의 전제.
  새 파생 캐시는 `register_akey_cache` 로 등록만 하면 무효화에 자동 편입된다.
- `content_hash` 는 raw parquet 내용 해시 — raw_data 편집·rawdata_replace(Excel 시트 삭제로
  source 가 줄어드는 경우 포함)로만 바뀐다. 갱신은 편집한 세션 1건이 아니라 **같은
  analysis_key 의 모든 세션**에 적용한다(`update_content_hash_for_analysis_key`) — 물리
  원본이 바뀌었는데 dedup 형제 세션이 옛 hash 로 남으면 disk_cache 의 옛 payload 를 계속
  서빙한다.
- `edits_rev` 는 세션 편집 DB(`report_webreport_edit_rev`)의 단조 rev — comment/etc/trim
  override/engr 편집으로 바뀐다. 세션 단위 편집이라 `sid` 와 항상 짝으로 들어간다.
- `mode`/`webreport_options` 는 세션 생성 시 확정되어 불변 — 키에 넣는 이유는 dedup(동일
  akey 공유 세션)과의 충돌 방지.
- **`prep`** 은 조회 전처리(항목 제외·outlier 마스킹 + 셀 패치·조건 규칙) spec 의 digest —
  [preprocess.py](../web_report/preprocess.py) `digest()`. 전처리가 **없으면 빈 문자열이고
  키에 아무것도 덧붙이지 않는다** → 옵션을 안 쓰는 세션의 키는 도입(2026-07-23) 전과 완전히
  동일하다(무회귀). 옵션을 켰다 끄면 원래 키로 돌아와 **옛 캐시가 그대로 다시 히트**한다.
  `edits_rev` 를 이미 가진 키(REPORT/TRIM/_FULL)에는 넣지 않는다 — 전처리 저장 시 rev 가
  함께 증가해 같은 역할을 하기 때문. 전처리는 `content_hash` 를 바꾸지 않으므로
  (원본 parquet 불변) dist/map/scatter **라우트 ETag** 에도 이 digest 를 붙인다
  (`routes_webreport._prep_tag` — 없으면 옵션 토글 직후 브라우저가 stale 304 를 받는다).
  **COMMONALITY 키에도 붙는다**(2026-07-28) — 종전엔 "전처리는 메타를 안 바꾼다"는 전제로
  뺐지만, 셀 패치·조건 규칙이 그 인덱스가 읽는 SERIAL/BIN·die 구성을 바꾼다.
  **레거시 spec 의 digest 값은 패치 계층 도입 후에도 문자 그대로 같다** — 기존 전처리 세션의
  캐시·pack variant 가 배포 순간 통째로 무효화되지 않도록
  `tests/test_preprocess.py::test_legacy_spec_normal_form_is_frozen` 이 hex 를 박아 고정한다.
- `selected_items` 는 analysis_key 산출에 포함되므로 어떤 키에도 따로 넣지 않는다.

**무효화 두 종류**: `evict_akey_caches`(raw_data 편집 — content_hash 만 바뀌어 구 키가 안
쓰이므로 메모리 회수용, manifest 캐시 유지) / `invalidate_caches`(세션 삭제 — manifest
포함 전부 정리).

## 콜드 미스 처리
- **single-flight 락** (`keyed_lock`): 캐시에 없는 같은 세션을 여러 사용자가 동시에 열면
  수 초짜리 CPU-bound 계산이 GIL 로 서로 밀리므로, 같은 `(종류, akey, chash)` 계산은 한
  스레드만 수행하고 나머지는 대기 후 캐시를 재확인한다.
- **컴퓨트 오프로드** ([compute.py](../web_report/compute.py)): 콜드 report/dist/map/trim
  빌드를 `ProcessPoolExecutor` 워커로 보내 waitress 스레드의 GIL 점유를 피한다. dist 는 전체·
  bin1 변형 모두 오프로드 대상(2026-07-15 — 종전엔 bin1 이 요청 스레드 인라인이었음).
  **Trim 그룹 차트도 배치 경로(`trim_chart_batch_job`)로 오프로드 대상**이다(2026-07-23 —
  종전엔 그룹당 1요청이 전부 요청 스레드 인라인이었다). tables 캐시가 따뜻하면 인라인,
  워커 붕괴 시 인라인 폴백. 업로드 직후 `prewarm` 도 풀에 제출되어 동시성 상한(워커 수)이
  자동 적용된다.
- **클라 프리컴퓨트 시딩** (2026-07-15): Honey 가 업로드에 첨부한 dist blob(전체/bin1,
  [dist_blob.py](../web_report/dist_blob.py) 공용 빌더)을 ingest 가 DIST_CACHE+디스크
  캐시에 시딩 — 첨부 세션은 dist 콜드 미스 자체가 없다 → [10](10_web_report_pipeline.md).
- **콜드 빌드 관측 로그**: report/dist 콜드 빌드가 `akey/항목수/포인트수/크기/소요초`
  INFO 로그를 남긴다 ([service.py](../web_report/service.py)) — 실데이터 규모가 위험
  구간(수천만 포인트)에 닿는지 운영 로그로 판단. **단, 이 INFO 는 인라인 분기 전용**이라
  운영의 정상 경로(워커 오프로드)는 안 찍힌다 — 아래 build_log 가 그 공백을 메운다.
- **콜드 빌드 단계별 기록** ([build_log.py](../web_report/build_log.py), 2026-08-04):
  완료·실패 빌드 1건 = JSON 1줄로 `server/log/webreport_build_YYYYMMDD.log`.
  단계(`download`/`decode`/`preprocess`/`ai_comment`/`tab:<탭>`/`dist_index`/`serialize`)와
  **대기 3종**(`queue_wait` 온디맨드·프리웜·distpack 큐 / `pool_wait` 워커 실행 시작까지
  = 풀 큐 + spawn·모듈 재임포트 / `ipc` payload 반송)을 함께 남긴다.
  - 오프로드 빌드의 단계는 자식 프로세스에서만 잴 수 있어, `report_job`/`dist_job`/
    `map_job` 이 **`(결과, timing)` 튜플**을 반환해 부모로 실어 보낸다(`prewarm_job` 은
    payload 를 버리고 timing dict 만). 호출부는 [service.py](../web_report/service.py)
    3곳 — 잡 반환 형태를 바꾸면 여기도 함께 고칠 것.
  - 프로세스 **안**의 구간은 `perf_counter`, 프로세스 **사이** 시점 비교는 `time.time()`
    (perf_counter 는 프로세스마다 기준점이 달라 비교 불가). 음수는 0 클램프.
  - 실패(timeout/broken/error)는 그걸 아는 유일한 지점인 `compute.run()` except 에서 기록.
    ⚠️ `WEB_REPORT_COMPUTE_TIMEOUT_SEC`(기본 300s)는 **풀 큐 대기까지 포함**해 잰다 —
    기록된 `total` 이 상한에 붙어 있으면 계산이 느린 게 아니라 앞 작업(프리웜·distpack·
    업로드 ingest 가 같은 풀을 공유)에 밀린 것일 수 있다. 타임아웃은 `_reset_pool`
    (전 워커 terminate)을 부르므로 **동시 진행 빌드가 함께 죽는다**(= broken 레코드 동반).
  - 조회: 관리자 `/pe/admin-<secret>/` **이력 탭 → 콜드 빌드 이력** 카드
    (`GET api/webreport/builds?hours=&limit=`). 원본 파일은 console log 탭에서도 열람 가능
    (`maintenance._LOG_GLOBS`). 계측은 전부 best-effort — 실패해도 빌드에 영향 없다.
- **202 + 백그라운드 빌드** (2026-07-21): `/full` 과 `/web_report/map_analysis` 는 콜드
  미스에서 요청 스레드가 빌드를 기다리지 않는다. `service.ColdBuildRequired` 를 올려
  `compute.request_build`(전용 큐 + 소비자 스레드 `WEB_REPORT_ONDEMAND_WORKERS`)에 넘기고
  `202 {"building":true,"stage","elapsed"}` 를 즉시 반환하며, 프런트가 1s→5s 백오프로
  재요청한다(boot.js `retryWhileBuilding` / wafer_charts.js `fetchMapUntilBuilt`).
  waitress 스레드는 8개뿐이라 여러 명이 서로 다른 신규 세션을 동시에 열면 값싼 요청까지
  밀리던 문제를 없앤다. **warm/디스크 히트는 종전대로 200** — 202 는 실제 콜드에서만.
  프리웜 큐와 분리한 이유: 프리웜은 포화 시 가장 오래된 요청을 버리는데(무해), 여기 요청은
  사용자가 화면에서 기다리는 중이라 버리면 그 사용자만 영영 로드되지 않는다. 대신
  `(session, kind)` 중복 등록을 막아 재요청 폭주에도 큐가 자라지 않게 한다.
  `build_status` 는 (session, stage) 단위로 기록한다 — report/map 콜드가 겹칠 때
  한쪽 `end()` 가 다른 쪽 기록을 지우지 않게 하기 위함.
  - **적용 범위는 `/full` 과 `map_analysis` 둘뿐이다.** `distribution_batch`/`scatter` 에도
    잠시 확장했다가 **철회했다**(2026-08-06) — 그 둘은 배치당 부분 계산이라 인라인이
    싼데도 백오프(3s/5s/8s/10s) 대기가 붙어 첫 진입 체감이 오히려 느려졌다.
  - **예상시간 안내** (2026-08-05, [web_report/eta.py](../web_report/eta.py)): 202 응답과
    `build_status` 가 `eta`(예상초)를 함께 준다 — 오버레이 문구가 "예상 약 12초 / 5초 경과"
    로 바뀐다. 진행바(%)는 종전 추정 creep 그대로다(안내일 뿐 진척률이 아니라서).
    추정식은 `A + B·Mcells + C·kcols` (Mcells=Σ항목×행/1e6, kcols=Σ항목/1e3). **총 MB 하나로는
    부족하다** — 같은 용량도 항목 수에 따라 ±50% 어긋난다(항목당 고정 비용). 두 변수는
    저장된 parquet **footer 만** 읽어 디코드 없이 구하고 (analysis_key, content_hash) 로
    캐시한다(2초 폴링이 파일을 다시 열지 않게). 로컬 parquet 이 없으면(S3 저장) 키를 빼고
    보내며 프런트는 종전 문구로 돌아간다. 계수는 개발 PC 실측이라 운영에서 그대로 맞지
    않으므로, `build_log` 에 같은 두 변수(`mcells`/`kcols`)를 남겨 실측/예측 중앙값을
    **배율 하나**로 학습한다(5분 캐시). 표본 5건에서 100% 반영하고 그 전에는 1.0 쪽으로
    비례 축소해 섞는다 — 벤치 계수는 무부하 단독 실행값이라 실사용보다 낙관적이라서
    (같은 21소스 세션이 폴링 동시요청·GIL 경합만으로 6.5s→10.5s), 5건이 쌓일 때까지
    기다리면 그동안 계속 짧게 예상한다.
    학습은 계산 시간(`build`)만 보고 큐 대기(`pool_wait`)는 뺀다 — 대기는 부하 따라
    요동쳐 예측 대상이 아니다. 따라서 서버가 붐빌 때는 실제 대기가 예상을 넘고, 문구가
    "예상보다 오래 걸리고 있습니다"로 전환된다. 끄려면 `WEB_REPORT_ETA_ENABLED=0`.
  - **콜드 판정은 single-flight 락 밖에서** (2026-07-28): 빌드 중인 세션의
    `("report"/"map",)+key` 락은 온디맨드 소비자가 빌드 내내 잡고 있다. 락에 들어간 뒤
    판정하면 202 로 돌려보내려던 폴링이 빌드가 끝날 때까지 waitress 스레드를 물고
    대기했다(같은 세션을 N명이 열면 스레드 N개가 묶임). 지금은
    `disk_cache.report_exists`/`map_exists`(stat 1회)로 락 **전에** 판정한다. 락 안의
    기존 판정은 TOCTOU 안전망으로 남겨 둔다.
- **Map dies 시딩** (2026-08-10, CLAUDE.md §5-11 Map 3초 SLA): 위 202 규약은 "콜드일 때
  스레드를 물지 않는다"를 보장할 뿐, **사용자 대기 자체는 그대로**다. map dies 는
  프리웜 대상이 아니라(`compute._prewarm_one` 은 report payload 만 만든다) Map 탭 /
  Issue Table Map 컬럼 첫 진입이 사실상 항상 콜드 202 + 전체 재디코드였다(대형 세션
  30초+ "맵 로드 중…").
  - **report 콜드 빌드가 `service.seed_map` 으로 함께 채운다** — temp_map 시딩과 대칭.
    이미 웜인 tables 를 재사용하므로 한계비용은 die dict 생성 + dumps + gzip 뿐이고,
    `build_log` 의 `map_seed` stage 로 상시 실측된다. 워커 오프로드 빌드면 워커
    프로세스 안에서 돌아 디스크를 직접 채우고(기존 규약), 부모 첫 조회는 disk 히트.
  - **재생성 방지**: 시딩 전에 RAM+디스크 존재를 확인해 조기 종료한다. comment/override
    편집 리빌드는 `edits_rev` 만 바뀌고 map 키는 그대로라 no-op 이다 — "편집마다 전체
    리빌드" 금지(2026-08-06)와 충돌하지 않는다.
  - **시딩 도입 전 세션**(report 캐시는 있는데 map 캐시가 없음)은 `/full` **200** 경로가
    `service.schedule_map_backfill` 로 백그라운드 빌드를 **예약만** 한다(대기 없음).
    사용자가 몇 초 뒤 탭을 클릭할 때 이미 준비돼 있게 하는 것으로, 어차피 유발될 빌드를
    앞당길 뿐이라 신규 202 도 부분 계산도 아니다. 폭주 방어는 `request_build` 의
    `(session, kind)` 중복 제거 + 연속 실패 차단 + 온디맨드 워커 상한.
  - 시딩 산출이 지연 라우트 경로(`get_map_analysis`)와 **정준 JSON 완전 일치**해야 한다
    (tables 준비 순서 `_mode_tables` → `selected_items` 필터가 같아야 함) —
    회귀 테스트 [tests/test_map_seed_equivalence.py](../tests/test_map_seed_equivalence.py),
    SLA 실측은 [tests/bench_webreport.py](../tests/bench_webreport.py) `bench_sla_map`.
- **dist pack chunk 디코드 캐시** (2026-07-28): `distribution_batch` 는 요청마다 chunk
  파일을 read+gunzip+json.loads 했다(대형 세션은 chunk 1개가 비압축 15~20MB, 순수 GIL
  점유). 디코드 결과를 `DIST_CHUNK_CACHE` 에 담아 갤러리 스크롤이 같은 chunk 를 되짚을 때
  파일·디코드를 건너뛴다. **반환 dict 는 읽기 전용 공유** — 소비자
  (`dist_pack.ecdf_from_pack_items`)가 입력을 변경하지 않는다는 계약 위에서만 성립한다.
  chunk 단위 keyed_lock 은 잡지 않는다(락 레지스트리 LRU 256 에 chunk 키가 대량 유입되면
  보유 중인 빌드·편집 락이 축출돼 상호배제가 깨진다).

- **기동 후 재웜 스윕** (2026-08-06, `compute.start_rewarm_sweep`): 재기동이나
  `REPORT_SCHEMA_VERSION` 상승 배포 직후에는 **전 세션이 한꺼번에 콜드**가 되어, 그날 처음
  세션을 여는 사용자마다 콜드 빌드를 정면으로 맞는다(실제로 2026-08-06 에 일반 세션이
  5초→1분으로 느려진 원인). 기동 `WEB_REPORT_REWARM_DELAY_SEC` 후 최근 세션
  `WEB_REPORT_REWARM_LIMIT` 건을 훑어 **콜드인 것만**(`service.report_is_cold`) 프리웜 큐에
  넣는다. 투입 조건은 "온디맨드 pending 이 비었고 프리웜 큐도 빈 상태" — 사용자 요청이
  항상 우선이고, 양보하다 `_REWARM_BUDGET_SEC`(1시간)을 넘기면 스윕을 접는다.
  [report_extension.init_app](../server/report/report_extension.py) 이 기동하며 워커
  프로세스에서는 뜨지 않는다.

## 환경변수
| 변수 | 기본값 | 설명 |
|------|--------|------|
| `WEB_REPORT_COMPUTE_WORKERS` | `2` (운영 `8`) | 콜드 빌드 워커 프로세스 수. `0` = 전부 인라인(구 동작). 운영값은 [server/env/server.env](../server/env/server.env) — `_ONDEMAND_WORKERS` 와 **짝으로** 올려야 효과가 있다. 제약은 RAM 이 아니라 CPU(유휴 워커 ≈100MB 실측). 운영 8 은 16코어/64GB 기준(2026-08-06 상향) — 슬롯 부족이 `_COMPUTE_TIMEOUT_SEC` 큐 대기 타임아웃 → 전 워커 terminate 연쇄로 이어지던 것을 완화 |
| `WEB_REPORT_TABLES_CACHE` | `4` | decoded tables 캐시 개수 상한. **실데이터 규모에선 이쪽이 먼저 걸린다** (세션 1건 ≈ 229MB → 4건 ≈ 0.9GB) |
| `WEB_REPORT_TABLES_CACHE_MB` | `4096` (운영 `2048`) | tables 캐시 추정 바이트 상한 (개수와 이중 적용, 0=비활성). **부모와 워커가 각자 갖는 상한**이라 실효 천장 = 값 × (1+워커수). 실데이터에선 개수 상한이 먼저 걸려 이 값은 발동하지 않는다(= 성능 손실 없이 천장만 절반) |
| `WEB_REPORT_DIST_CACHE` | `4` | Distribution gzip 캐시 개수 |
| `WEB_REPORT_DIST_CACHE_MB` | `1024` | dist blob RAM 바이트 상한 (개수와 이중 적용, 0=비활성 — worst case blob ~505MB 실측) |
| `WEB_REPORT_MAP_CACHE` | `4` (운영 `8`) | Map dies gzip 캐시 개수 |
| `WEB_REPORT_MAP_CACHE_MB` | `512` (운영 `1024`) | Map dies blob RAM 바이트 상한 (개수와 이중 적용, 0=비활성) |
| `WEB_REPORT_REPORT_CACHE` | `8` (운영 `32`) | report dict 캐시 개수 |
| `WEB_REPORT_REPORT_CACHE_MB` | `256` (운영 `1024`) | report dict 캐시 추정 바이트 상한 (개수와 이중 적용, 0=비활성). 크기는 put 시 1회 직렬화 길이로 추정 |
| `WEB_REPORT_DIST_BATCH_CACHE` | `64` | Distribution 항목 배치 응답 gzip 캐시 개수 |
| `WEB_REPORT_DIST_BATCH_CACHE_MB` | `256` | 〃 바이트 상한 (개수와 이중 적용, 0=비활성). 소스·die 가 많은 세션은 배치 1건이 수 MB |
| `WEB_REPORT_DIST_CHUNK_CACHE` | `64` (운영 `128`) | dist pack chunk **디코드 결과** 캐시 개수 (distribution_batch 의 gunzip+json.loads 반복 제거) |
| `WEB_REPORT_DIST_CHUNK_CACHE_MB` | `512` | 〃 비압축 바이트 상한 (개수와 이중 적용, 0=비활성) |
| `WEB_REPORT_ONDEMAND_WORKERS` | `2` (운영 `8`) | 콜드 미스 조회가 202 를 반환한 뒤 백그라운드에서 빌드하는 소비자 스레드 수. `_COMPUTE_WORKERS` 와 같은 값으로 유지 |
| `WEB_REPORT_COMMONALITY_CACHE` | `2` | Commonality 인덱스 캐시 개수 |
| `WEB_REPORT_TRIM_CACHE` | `4` | Trim payload 캐시 개수 |
| `WEB_REPORT_TRIM_CHART_CACHE` | `64` | Trim 그룹 차트 캐시 개수 |
| `WEB_REPORT_TRIM_CHART_CACHE_MB` | `256` | Trim 그룹 차트 gzip 바이트 상한 (개수와 이중 적용, 0=비활성). 차트 1건이 전 die 전 포인트라 개수 상한만으론 RAM 이 예측 불가 |
| `WEB_REPORT_MANIFEST_CACHE` | `16` | manifest 캐시 개수 |
| `WEB_REPORT_FULL_CACHE` | `8` (운영 `32`) | `/full` 응답 gzip 캐시 개수 |
| `WEB_REPORT_FULL_CACHE_MB` | `512` (운영 `1024`) | 〃 바이트 상한 (개수와 이중 적용, 0=비활성) |
| `WEB_REPORT_SCATTER_CACHE` | `16` | `/scatter` 응답 gzip 캐시 개수 |
| `WEB_REPORT_SCATTER_CACHE_MB` | `256` | 〃 바이트 상한 (개수와 이중 적용, 0=비활성) |
| `WEB_REPORT_DISK_CACHE_MAX_GB` | `500` | 디스크 캐시 총량 상한 (0 이하 = 비활성) |
| `WEB_REPORT_REWARM_ON_START` | `1` | 기동 후 재웜 스윕 사용 여부 (`0` = 끔) |
| `WEB_REPORT_REWARM_LIMIT` | `30` | 스윕이 훑을 최근 web_report 세션 수 |
| `WEB_REPORT_REWARM_DELAY_SEC` | `60` | 기동 후 스윕 시작까지 지연(초) |

## 불변 규칙
- 캐시는 전부 재계산 가능한 파생물 — 언제 지워져도 무해해야 한다.
- manifest write-through 일관성은 **단일 프로세스 전제**에 의존한다. 멀티프로세스로
  바꾸면 이 전제가 깨지므로 캐시/무효화 설계를 재검토할 것.
- 캐시 키 구성은 항상 `cache_policy.py` 빌더를 통한다 — 호출부에서 즉석으로 키를 만들지 말 것.

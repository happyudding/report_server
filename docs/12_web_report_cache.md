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
- **전처리(preprocess) 세션은 pack 을 쓰지 않는다** — pack 은 업로드 시점(전처리 없음)
  기준이라 항목 제외·outlier 가 반영돼 있지 않다. `service.pack_available` 이 digest 로
  가드하고 폴백 계산한다(전처리 해제 시 자동 복귀).
- 미첨부(구 Honey)·검증 실패·chunk 손상은 전부 조용히 **기존 계산 폴백** — 기존 세션은
  종전과 완전히 동일하게 열린다.
- 구 `dist_blob` 시딩 경로(2026-07-15, DIST_CACHE+disk 시딩)는 구 Honey 하위호환으로 남는다.

## 캐시 키 규약 (단일 진실 = cache_policy.py)
[cache_policy.py](../web_report/cache_policy.py) 가 캐시별 키 빌더를 제공하고 **호출부는
반드시 이 빌더로 키를 만든다**. 새 캐시를 추가하면 여기 빌더와 아래 표를 함께 추가할 것.

| 캐시 | 키 구성 | 무효화 트리거 |
|------|---------|---------------|
| TABLES_CACHE | (akey, chash[, prep]) | raw_data 편집(chash) / 전처리 / 세션 삭제 |
| DIST_CACHE | (akey, chash[, prep], mode) | 〃 (mode 는 세션 생성 후 불변) |
| _DIST_BATCH_CACHE | (akey, chash[, prep], mode, subjects_digest[, "bin1"]) | 〃 — 항목 배치 ECDF gzip (`/web_report/distribution_batch`) |
| MAP_CACHE | (akey, chash[, prep], mode) | 〃 — Map dies gzip (`/web_report/map_analysis`, schema v8) |
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
- **`prep`** 은 조회 전처리(항목 제외·outlier 마스킹) spec 의 digest —
  [preprocess.py](../web_report/preprocess.py) `digest()`. 전처리가 **없으면 빈 문자열이고
  키에 아무것도 덧붙이지 않는다** → 옵션을 안 쓰는 세션의 키는 도입(2026-07-23) 전과 완전히
  동일하다(무회귀). 옵션을 켰다 끄면 원래 키로 돌아와 **옛 캐시가 그대로 다시 히트**한다.
  `edits_rev` 를 이미 가진 키(REPORT/TRIM/_FULL)에는 넣지 않는다 — 전처리 저장 시 rev 가
  함께 증가해 같은 역할을 하기 때문. 전처리는 `content_hash` 를 바꾸지 않으므로
  (원본 parquet 불변) dist/map/scatter **라우트 ETag** 에도 이 digest 를 붙인다
  (`routes_webreport._prep_tag` — 없으면 옵션 토글 직후 브라우저가 stale 304 를 받는다).
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
  구간(수천만 포인트)에 닿는지 운영 로그로 판단.
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

## 환경변수
| 변수 | 기본값 | 설명 |
|------|--------|------|
| `WEB_REPORT_COMPUTE_WORKERS` | `2` | 콜드 빌드 워커 프로세스 수. `0` = 전부 인라인(구 동작) |
| `WEB_REPORT_TABLES_CACHE` | `4` | decoded tables 캐시 개수 상한 |
| `WEB_REPORT_TABLES_CACHE_MB` | `4096` | tables 캐시 추정 바이트 상한 (개수와 이중 적용, 0=비활성) |
| `WEB_REPORT_DIST_CACHE` | `4` | Distribution gzip 캐시 개수 |
| `WEB_REPORT_DIST_CACHE_MB` | `1024` | dist blob RAM 바이트 상한 (개수와 이중 적용, 0=비활성 — worst case blob ~505MB 실측) |
| `WEB_REPORT_MAP_CACHE` | `4` | Map dies gzip 캐시 개수 |
| `WEB_REPORT_MAP_CACHE_MB` | `512` | Map dies blob RAM 바이트 상한 (개수와 이중 적용, 0=비활성) |
| `WEB_REPORT_REPORT_CACHE` | `8` | report dict 캐시 개수 |
| `WEB_REPORT_REPORT_CACHE_MB` | `256` | report dict 캐시 추정 바이트 상한 (개수와 이중 적용, 0=비활성). 크기는 put 시 1회 직렬화 길이로 추정 |
| `WEB_REPORT_DIST_BATCH_CACHE` | `64` | Distribution 항목 배치 응답 gzip 캐시 개수 (배치 1건 = 항목 수십 개분이라 작다) |
| `WEB_REPORT_ONDEMAND_WORKERS` | `2` | 콜드 미스 조회가 202 를 반환한 뒤 백그라운드에서 빌드하는 소비자 스레드 수 |
| `WEB_REPORT_COMMONALITY_CACHE` | `2` | Commonality 인덱스 캐시 개수 |
| `WEB_REPORT_TRIM_CACHE` | `4` | Trim payload 캐시 개수 |
| `WEB_REPORT_TRIM_CHART_CACHE` | `64` | Trim 그룹 차트 캐시 개수 |
| `WEB_REPORT_TRIM_CHART_CACHE_MB` | `256` | Trim 그룹 차트 gzip 바이트 상한 (개수와 이중 적용, 0=비활성). 차트 1건이 전 die 전 포인트라 개수 상한만으론 RAM 이 예측 불가 |
| `WEB_REPORT_MANIFEST_CACHE` | `16` | manifest 캐시 개수 |
| `WEB_REPORT_FULL_CACHE` | `8` | `/full` 응답 gzip 캐시 개수 |
| `WEB_REPORT_SCATTER_CACHE` | `16` | `/scatter` 응답 gzip 캐시 개수 |
| `WEB_REPORT_DISK_CACHE_MAX_GB` | `500` | 디스크 캐시 총량 상한 (0 이하 = 비활성) |

## 불변 규칙
- 캐시는 전부 재계산 가능한 파생물 — 언제 지워져도 무해해야 한다.
- manifest write-through 일관성은 **단일 프로세스 전제**에 의존한다. 멀티프로세스로
  바꾸면 이 전제가 깨지므로 캐시/무효화 설계를 재검토할 것.
- 캐시 키 구성은 항상 `cache_policy.py` 빌더를 통한다 — 호출부에서 즉석으로 키를 만들지 말 것.

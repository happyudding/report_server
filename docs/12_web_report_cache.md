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

## 캐시 키 규약 (단일 진실 = cache_policy.py)
[cache_policy.py](../web_report/cache_policy.py) 가 캐시별 키 빌더를 제공하고 **호출부는
반드시 이 빌더로 키를 만든다**. 새 캐시를 추가하면 여기 빌더와 아래 표를 함께 추가할 것.

| 캐시 | 키 구성 | 무효화 트리거 |
|------|---------|---------------|
| TABLES_CACHE | (akey, chash) | raw_data 편집(chash) / 세션 삭제 |
| DIST_CACHE | (akey, chash, mode) | 〃 (mode 는 세션 생성 후 불변) |
| MAP_CACHE | (akey, chash, mode) | 〃 — Map dies gzip (`/web_report/map_analysis`, schema v8) |
| COMMONALITY_CACHE | (akey, chash) | raw_data 편집 / 세션 삭제 |
| REPORT_CACHE | (akey, chash, sid, edits_rev, opts, mode) | comment/override 편집(rev) + 위 전부 |
| TRIM_CACHE | (akey, chash, sid, edits_rev, mode, source) | trim override 편집(rev) + 위 전부 |
| TRIM_CHART_CACHE | (akey, chash, mode, source, items_digest) | 그룹 슬롯 구성 변경 / raw_data 편집 |
| _FULL_CACHE | (akey, chash, "sid:edits_rev", extras_digest) | 편집 rev / annotations 등 extras |
| _SCATTER_CACHE | (akey, chash, mode, subject) | raw_data 편집 / 세션 삭제 |

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
  tables 캐시가 따뜻하면 인라인, 워커 붕괴 시 인라인 폴백. 업로드 직후 `prewarm` 도
  풀에 제출되어 동시성 상한(워커 수)이 자동 적용된다.
- **클라 프리컴퓨트 시딩** (2026-07-15): Honey 가 업로드에 첨부한 dist blob(전체/bin1,
  [dist_blob.py](../web_report/dist_blob.py) 공용 빌더)을 ingest 가 DIST_CACHE+디스크
  캐시에 시딩 — 첨부 세션은 dist 콜드 미스 자체가 없다 → [10](10_web_report_pipeline.md).
- **콜드 빌드 관측 로그**: report/dist 콜드 빌드가 `akey/항목수/포인트수/크기/소요초`
  INFO 로그를 남긴다 ([service.py](../web_report/service.py)) — 실데이터 규모가 위험
  구간(수천만 포인트)에 닿는지 운영 로그로 판단.

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
| `WEB_REPORT_COMMONALITY_CACHE` | `2` | Commonality 인덱스 캐시 개수 |
| `WEB_REPORT_TRIM_CACHE` | `4` | Trim payload 캐시 개수 |
| `WEB_REPORT_TRIM_CHART_CACHE` | `64` | Trim 그룹 차트 캐시 개수 |
| `WEB_REPORT_MANIFEST_CACHE` | `16` | manifest 캐시 개수 |
| `WEB_REPORT_FULL_CACHE` | `8` | `/full` 응답 gzip 캐시 개수 |
| `WEB_REPORT_SCATTER_CACHE` | `16` | `/scatter` 응답 gzip 캐시 개수 |
| `WEB_REPORT_DISK_CACHE_MAX_GB` | `500` | 디스크 캐시 총량 상한 (0 이하 = 비활성) |

## 불변 규칙
- 캐시는 전부 재계산 가능한 파생물 — 언제 지워져도 무해해야 한다.
- manifest write-through 일관성은 **단일 프로세스 전제**에 의존한다. 멀티프로세스로
  바꾸면 이 전제가 깨지므로 캐시/무효화 설계를 재검토할 것.
- 캐시 키 구성은 항상 `cache_policy.py` 빌더를 통한다 — 호출부에서 즉석으로 키를 만들지 말 것.

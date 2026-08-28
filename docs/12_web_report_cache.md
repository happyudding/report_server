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
| _DIST_SEQ_CACHE | (akey, chash[, prep], mode, subjects_digest, "seq", ver[, "bin1"[, scope]]) | 〃 — 항목 배치 **Serial 순**(rawdata 누적 순) 값 배열 gzip (`?order=seq`). ECDF 와 별도 키 = 축이 다른 응답이 서로의 304 로 오염되지 않는다. pack 지름길 없음(순서 보존) |
| MAP_CACHE | (akey, chash[, prep], mode) | 〃 — Map dies gzip (`/web_report/map_analysis`, schema v8). **report 콜드 빌드가 `service.seed_map` 으로 RAM+디스크를 함께 채운다** (아래 "Map dies 시딩" — Map 3초 SLA) |
| TEMP_MAP_CACHE | (akey, chash[, prep], mode, v) | 〃 — Temperature 항목별 fail die **인덱스** gzip (`/web_report/temp_map`, 2026-08-05). map dies 와 같은 세대여야 인덱스가 맞는다. **report 콜드 빌드가 `service.seed_temp_map` 으로 RAM+디스크를 함께 채운다**(같은 판정 결과 재사용) — 라우트 단독 콜드는 디스크 → 워커 오프로드(`compute.temp_map_job`) 순 |
| COMMONALITY_CACHE | (akey, chash) | raw_data 편집 / 세션 삭제 (메타만 쓰므로 전처리 무관) |
| REPORT_CACHE | (akey, chash, sid, edits_rev, opts, mode[, rules_rev][, "evalfail"]) | comment/override/전처리 편집(rev) + eval 룰 편집(ai 세션만) + 위 전부 |
| AI_COMMENT_CACHE | (akey, chash[, prep], mode, meta_digest[, rules_rev][, "evalfail"][, sens_digest], aiver) | raw_data 편집 / 전처리 / **세션 메타 PATCH**(meta_digest) / eval 룰 편집 / **세션 민감도 게이지**(sens_digest) — **sid·edits_rev 무관**: comment 편집·스키마 bump·dedup 형제 세션에서 eval 재평가(콜드 빌드 80%)를 안 한다 (2026-08-13, 아래 "AI Comment 비동기 분리") |
| TRIM_CACHE | (akey, chash, sid, edits_rev, mode, source) | trim override/전처리 편집(rev) + 위 전부 |
| TRIM_CHART_CACHE | (akey, chash[, prep], mode, source, items_digest) | 그룹 슬롯 구성 변경 / raw_data 편집 — 단일 `/trim_chart` 와 배치 `/trim_chart_batch` 가 **같은 엔트리를 공유**한다(배치는 그룹별로 이 캐시를 조회·적재할 뿐) |
| _FULL_CACHE | (akey, chash, "sid:edits_rev", extras_digest) | 편집 rev / annotations 등 extras |
| _SCATTER_CACHE | (akey, chash[, prep], mode, subject) | raw_data 편집 / 전처리 / 세션 삭제 |
| _GAP_CACHE | (akey, chash[, prep], mode, chart_id, spec_digest, gver[, "bin1"]) | raw_data 편집 / 전처리 / **수식 수정(spec_digest)** / 세션 삭제 — **edits_rev·sid 무관**(남의 코멘트 저장으로 죽지 않게, ai_comment_key 와 같은 논리). 갤러리 카드와 Item_detail 이 이 한 엔트리를 공유한다 |
| COMPARE_CACHE | (akey, chash[, prep], mode, opts, cmpver) | raw_data 편집 / 전처리 / 세션 삭제 — **sid·edits_rev 무관**(ai_comment_key 와 같은 논리: 코멘트 편집으로 재계산하지 않는다). 값에 common_map 이 있어 수 MB 라 개수(`WEB_REPORT_COMPARE_CACHE`)+바이트(`_MB`) **이중 상한** |

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
  ⚠️ **REPORT 키만은 `rev` 가 아니라 `payload_rev` 를 쓴다** (2026-08-14,
  `service._payload_rev` → `get_webreport_edit_rev(payload=True)`). 같은 표의 rev 를
  그대로 쓰면 **Note 시트 한 글자·차트 주석 하나**를 고쳐도 report payload 가 통째로
  콜드가 됐다 — 그 편집들은 payload 계산에 안 들어가고 `/full` 조립에서만 붙는데도.
  `payload_rev` 는 `webreport_edits.PAYLOAD_NEUTRAL_KINDS`(**5종** — chart_note/note_sheet/
  note_tag/dist_composite/gap_chart) **외의** kind 가 저장될 때만 오른다(모르는 kind 는
  무효화하는 쪽으로 간주). ⚠ 이 목록은 `edits._STATE_EXCLUDED_KINDS`(8종)와 **다르다** —
  preprocess·yield_basis·compare_note 는 state 에서만 빠지고 payload_rev 는 올린다
  (실제로 payload 를 바꾸므로 올려야 맞다). 판단 기준은 CLAUDE.md 규칙 16.
  `_FULL_CACHE`·`TRIM_CACHE` 는 종전대로 `rev` 를 쓴다 — /full 은 그 extras 를 담으므로
  빼면 Note 편집이 화면에 반영되지 않는다. 마이그레이션은 기존 행에 `payload_rev = rev`
  를 물려줘 배포 시 무효화도, 옛 캐시 부활도 없다(core.py 마이그레이션 주석).
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

### 스키마 버전 상수 9개 — "그 캐시 것만" 올린다

응답이나 계산 결과의 **구조**를 바꾸면 옛 캐시가 새 코드에 먹히지 않으므로 버전을 올려
무효화한다. 상수마다 무효화 범위가 다르니 **바꾼 것에 해당하는 상수만** 올린다
(판단 규칙은 [CLAUDE.md](../CLAUDE.md) §5 규칙 14).

| 상수 | 현재 | 무효화 범위 | 언제 올리나 |
|------|:----:|-------------|-------------|
| `REPORT_SCHEMA_VERSION` | 41 | **전 세션 payload** — 콜드 폭풍 | `build_report_payload` 구조·탭 구성 변경 |
| `TEMPERATURE_SCHEMA_VERSION` | 1 | Temperature 세션 payload | Temp 시트 구조 변경 |
| `COMPARE_REPORT_SCHEMA_VERSION` | 1 | Compare 세션 payload **적재 방식** | payload 에 compare 결과를 싣는 방식 변경 |
| `COMPARE_SCHEMA_VERSION` | 2 | compare **계산 결과**(`compare_key`) | dist_shift·equivalence 등 계산 변경 |
| `AI_COMMENT_SCHEMA_VERSION` | 2 | ai comment 반환 dict | 반환 키 구조 변경 (룰 변경은 `.rules_rev` 몫) |
| `MAP_SCHEMA_VERSION` | 2 | map rows 값 | die/bin 집계 결과 변경 |
| `TEMP_MAP_SCHEMA_VERSION` | 1 | temp_map 응답 구조 | fail die 인덱스 응답 변경 |
| `DIST_SEQ_SCHEMA_VERSION` | 1 | Serial 순 배치 응답 | `seq-columnar-v1` 페이로드 변경 |
| `DIST_BATCH_SCHEMA_VERSION` | 1 | ECDF **배치** 응답(`dist_batch_key`) | `ecdf-columnar-v1` 응답 구조 변경 (`x`/`y`/`n`) |
| `GAP_SCHEMA_VERSION` | 2 | Gap Chart 응답 | 응답 키 변경 — **캐시 키와 ETag 양쪽**에 들어간다 |

⚠️ **`COMPARE_SCHEMA_VERSION`(계산) 과 `COMPARE_REPORT_SCHEMA_VERSION`(payload 적재)은 다른
상수다.** 반대쪽을 올리면 아무것도 안 갈리거나 필요 없는 재계산이 돈다.

⚠️ **`DIST_BATCH_SCHEMA_VERSION` 은 `dist_batch_key` 에만 있고 짝인 `dist_key`(전체 dist)에는
일부러 없다** (2026-08-25). 같은 `ecdf-columnar-v1` 응답을 담는 두 캐시인데도 갈라 둔 이유는
`dist_key` 가 **Honey 가 업로드 때 시딩한 dist blob 이 얹히는 자리**이기 때문이다
([ingest.py](../web_report/ingest.py) `seed_client_dist_blobs`) — 무효화하면 그 시딩이 막아
주던 수십 초 콜드 dist 빌드 + RAM 스파이크가 운영 세션마다 되살아난다. 웹 화면(갤러리
카드·미니셀·composite·Gap)은 전부 배치 경로만 쓰고, 배치 재계산은 pack 이 있으면 덧셈뿐이라
싸다. 전체 dist 캐시는 자연히 miss 될 때 pack 으로 재조립되며 그때 새 필드가 실린다.

### 키 빌더의 설계 장치 3개 (되돌리지 말 것)

- **`gap_key(..., spec_digest)` 의 `spec_digest` 는 기본값이 없는 필수 인자다.** 수식을
  고쳤는데 옛 숫자가 나오는 것은 조용히 틀리는 종류라, 호출부가 빠뜨리면 **TypeError 로
  즉시 터지게** 만들어 뒀다. 기본값을 주면 그 안전장치가 사라진다.
- **`dist_chunk_key` 만 `validate_mode` 가 아니라 `str(mode or "Normal")` 로 정규화한다.**
  이 키는 session dict 가 아니라 원시 인자를 받고, `dist_pack_store._gen_name` 의 세대
  이름과 **문자 그대로 같아야** 하기 때문이다. 어긋나면 다른 세대의 pack 을 돌려준다.
- **`report_pending_key` 는 ai 단독일 때 `("aipending",)` 꼬리를 유지한다.** 종전 형식과
  같은 키라 이미 디스크에 있는 대기본이 계속 유효하다(롤백 안전). kinds 가 여럿이면
  `("pending",) + sorted(kinds)` 로 간다.

### 직렬화 포맷 문자열 4종

payload 안에 `format` 필드로 실려 나가는 이름이다. **바꾸면 옛 캐시·pack 을 새 코드가
거부**하므로, 구조를 바꿀 땐 이름에 `-v2` 를 붙이고 위 버전 상수도 함께 올린다.

| 상수 | 값 | 정의 |
|------|-----|------|
| `dist_blob.DIST_BLOB_FORMAT` | `ecdf-columnar-v1` | Distribution ECDF compact |
| `dist_pack.DIST_PACK_FORMAT` | `dist-pack-v1` | 클라 정렬 pack 본문 |
| `dist_pack.DIST_PACK_INDEX_FORMAT` | `dist-pack-index-v1` | pack 청크 인덱스 |
| `dist_seq.SEQ_FORMAT` | `seq-columnar-v1` | Serial 순 값 배열 |

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
- **진행 중 콜드 빌드 상세 + 개입** ([admin_panel/builds_admin.py](../server/admin_panel/builds_admin.py),
  2026-08-13): 위 build_log 는 **끝난** 빌드의 기록이다. 지금 도는 빌드는 현황 탭이
  `GET api/runtime` 의 `builds` 로 본다 — 세션 메타(제품·LOT·파일명·업로더)·대기 중인
  사용자(`metrics.active_users` 의 session_id 조인)·**워커의 현재 단계/source**
  (`compute.worker_states()` = 살아 있는 sidecar 읽기, 종전에는 타임아웃 실패 때만 읽었다)·
  `eta` 대비 초과 배수를 한 행에 합쳐 준다. **유발 원인**은 `build_status` 가 등록 시점에
  큐 컨텍스트(`build_log.current_context()` 의 trigger/kind)를 함께 담아 얻는다 —
  `ondemand:report`(사용자 대기) / `ondemand:map` / `ondemand:ai`(백그라운드 평가) /
  `prewarm` / 빈 값(부모 인라인). 호출부(service.py)는 무변경이다.
  - 개입 `POST api/webreport/build_action`: `clear_failure` / `clear_stuck`(워커 타임아웃
    초과 건만 — 정상 진행 중인 등록을 지우면 프런트가 "끝났다"고 오판한다) / `rebuild`.
    **개별 빌드 취소는 구조적으로 불가능하다**(ProcessPoolExecutor 는 실행 중 잡 cancel 불가,
    워커 1개만 죽여도 풀 전체가 broken — `run()` 주석). 그래서 액션은 전부 *막힌 것을 푸는*
    쪽이고 캐시·편집·산출물은 건드리지 않는다.
  - **사용자 단위 강제 중단** (2026-08-14): 콜드 빌드가 오래 걸리는 세션을 열어둔 채 자리를
    뜬 탭이 15분간 폴링하며 재빌드를 계속 유발한다. 관리자 **사용자 탭 → 지금 접속 중 →
    ⛔ 중단**(`POST api/users/action`)이 ① 그 세션에 `kill_wait`(진행 표시 정리 +
    `drop_pending` + `mark_failure`×FAIL_LIMIT → `/full` 이 202 대신 즉시 503) ② 그 사용자
    브라우저에 중단 신호(`admin_panel/messages.request_stop` → 기존 `GET /api/my_messages`
    30초 폴링에 실려 감 → `admin_message.js` 가 대기 폴링 정지 + 안내)를 건다.
    **여기서도 진행 중인 워커 계산은 못 끊는다** — 워커 타임아웃까지 돌다 스스로 끝난다.
    쿨다운(기본 10분)은 `clear_failure` 로 즉시 해제. `location.reload()` 강제는 쓰지
    않는다(leave_guard 의 beforeunload 에 걸려 미저장 입력을 잃을 수 있다 — 불변 규칙 12).
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
  - ⚠️ **시간 상한은 워커 오프로드에서만 나온다** (2026-08-14). 소비자 스레드는 잡을 직접
    부르므로, 그 안에서 `should_offload=False`(편집 직후처럼 부모 tables 가 웜)로 떨어지면
    빌드 전체가 그 스레드에서 인라인으로 돌고 **파이썬 스레드는 강제 종료가 불가**해 아무도
    못 끊는다(2026-08-13 Issue Table 편집 후 무한 로딩의 근본 원인). 그래서
    `compute.force_offload_for_consumer()` 가 온디맨드 컨텍스트를 보고 무조건 워커로 보내고,
    202 규약이 없어 요청 스레드가 직접 계산하던 dist/temp_map/trim 도
    `compute.should_offload_heavy()` 로 항상 워커로 간다. perf_guard `S11` 이 이 둘의 제거를
    막는다. 이 경로들이 `QueueWaitTimeout`/`BrokenProcessPool` 로 끊기면 라우트가
    `security.compute_busy` 로 **503 + Retry-After**(재시도하면 되는 상황)를 준다.
  - **자동 재시도 1회** (총 실행 2회, `WEB_REPORT_ONDEMAND_MAX_ATTEMPTS`): 소비자가
    `time.sleep` 없이 **재큐잉**한다(소비자가 2개뿐이라 sleep 하면 다른 사용자 202 가 멈춘다).
    재시도 대상은 `QueueWaitTimeout`/`CancelledError`/`BrokenProcessPool` 뿐이고 **순수
    `TimeoutError`(워커 hang)는 제외** — 재시도 1회당 300초를 더 태우고 `_reset_pool` 이
    무고한 동시 빌드를 함께 죽이므로 피해가 배가 된다. `mark_failure` 는 **마지막 시도에서만**
    불러 `FAIL_LIMIT`(2)의 의미가 "논리 빌드 실패 2회"로 유지된다.
  - **유령 자동 회수**: 등록만 남고 소비자가 사라지면 `request_build` 가 그 (세션,kind)를
    영원히 무시해 202 가 무한 반복됐다(회수 수단은 관리자 수동뿐). 이제
    `_expire_ghost_pending` 이 TTL(`WEB_REPORT_ONDEMAND_PENDING_TTL_SEC`, 기본 480s) 초과 +
    큐에 없음이면 해제 후 즉시 재등록한다 — 사용자가 새로고침만 해도 풀린다. 진행 표시도
    `build_status.snapshot` 이 `STALE_SEC`(900s) 초과 등록을 걷어내 "N초 경과"가 무한히
    커지지 않는다(관리자 `snapshot_all` 은 지우지 않고 `stale` 플래그만 — 안 보이면 지울 수도 없다).
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
- **AI Comment 비동기 분리** (2026-08-13): ai_comment 옵션 세션의 콜드 빌드는 실측에서
  **eval 평가가 80%** 였다(5.9s 중 4.7s — 운영 대형 세션은 300s 타임아웃의 주범).
  두 겹으로 분리했다:
  - **분리 캐시**: 평가 결과(build_ai_comments dict)를 `cache_policy.ai_comment_key`
    (위 표 AI_COMMENT_CACHE — sid·edits_rev 불포함)로 RAM+디스크(`aicmt-*`)에 따로
    저장한다(`service._ai_comment_cached`). comment 편집·`REPORT_SCHEMA_VERSION` bump·
    dedup 형제 세션의 재빌드는 캐시 히트로 평가를 건너뛴다. **예외 폴백(빈 결과)은
    캐시하지 않는다**(일시 오류 영구화 방지 — `ai_comment.safe_build_ex`).
  - **pending payload**: 캐시 미스 콜드 빌드(사용자 대기 경로, `ai_inline=False`)는
    AI 없는 payload 에 `ai_comment_pending: true` 를 얹어 **리포트를 먼저 연다**.
    이 대기본은 **별도 키**(`cache_policy.report_pending_key` = 정본 키 + `"aipending"`)로
    디스크에도 저장한다 — 없으면 AI 잡이 끝나기 전 재접속(서버 재시작·RAM 축출 후)이
    매번 완전 콜드가 되어 "첫 조회만 빠르고 재접속은 느린" 회귀가 된다(2026-08-13 신고).
    정본 키를 쓰지 않는 이유는 **롤백 안전**이다: 이 기능을 되돌린 옛 코드가 정본 키에서
    대기본을 읽으면 AI Comment 가 빈 채로 굳는다. 조회 순서는 **정본 → (AI 캐시 미준비
    시) 대기본**이고, 최종본 저장 시 `disk_cache.drop_report` 로 대기본을 회수한다.
    콜드 판정(`report_is_cold`)과 실제 로드가 같은 조건(`_pending_report_ready`)을 봐야
    202 를 냈다가 200 이 되는 불일치가 없다. AI 평가는 온디맨드 `"ai"` 잡
    (`compute._ONDEMAND_JOBS`, `report_job(ai_inline=True)` — 워커 강제 오프로드)이
    백그라운드로 끝내고 최종 payload 를 재빌드·디스크 저장하며, 부모 RAM 의 pending
    본을 최종본으로 덮는다. 프런트(boot.js `maybeStartAiPendingPoll`)는 /full 을
    폴링하다 최종본이 오면 다시 그린다(셀 표시는 sheets.js `renderAiComment` 의
    "계산 중…"). **프리웜·ingest 경로는 종전처럼 동기**(`ai_inline=True`) — 아무도
    기다리지 않으므로 AI 캐시·최종본을 미리 채우는 편이 낫다.
  - pending 응답은 response 캐시(_FULL_CACHE)에 넣지 않고 etag 도 `-ai` 꼬리로 가른다
    (완료 후 stale 304 방지). AI 잡 실패는 `build_status` (sid,"ai") 실패 누적으로
    재시도가 차단되고 리포트는 계속 열린다. 가드: perf_guard `S10-ai-comment-cache`,
    벤치 `#14 bench_ai_comment`(quick 포함).
  - **세션 민감도 게이지 꼬리표** (2026-08-28): `_eval_sensitivity_suffix` — 세션이
    `webreport_options.eval_sensitivity.overrides` 를 가지면 `("sens"+digest12,)` 가
    붙는다. **없으면 빈 튜플**이라 기존 세션 키는 바이트 그대로다(콜드 폭풍 회피 —
    `_eval_rules_suffix` 와 같은 규약). 이 꼬리표가 없으면 **조용한 오답**이 난다:
    민감도는 `webreport_options` 에만 있고 analysis_key·content_hash 에는 없으므로,
    같은 rawdata 를 다른 민감도로 두 번 올린 dedup 형제 세션이 같은 키를 공유해 두 번째가
    첫 번째의 평가 결과를 그대로 본다. `session_id` 가 아니라 **설정값 digest** 라
    S10 이 지키려는 dedup 이익(같은 설정이면 형제끼리 캐시 공유)은 그대로다.
    → [docs/13 §17](13_eval_analyzer_integration.md)
- **Compare 비동기 분리** (2026-08-19): Compare 모드 세션의 콜드 빌드에서 compare 계산이
  **34%** 였다(3.323s 중 1.147s — payload stage 의 74%). AI Comment 와 **같은 두 겹** 구조로
  분리했다. 값은 완전 등가다(`tests/test_compare_async.py` (a) 가 정준 JSON 일치를 고정).
  - **분리 캐시**: `cache_policy.compare_key`(sid·edits_rev **불포함**, `COMPARE_SCHEMA_VERSION`)
    로 RAM(`COMPARE_CACHE`, 바이트 이중 상한 — common_map 이 수 MB)+디스크(`cmp-*`)에 저장
    (`service._compare_cached`). 코멘트 한 줄 편집·전역 스키마 bump·dedup 형제가 더는
    compare 를 재계산하지 않는다 — **이게 이번 변경의 가장 큰 이득**이다.
    키에 정규화된 `compare_groups` 대신 **원본 `webreport_options`** 를 넣는 이유: 정규화에
    소스 이름이 필요해 tables 를 열어야 하는데, 이 키는 tables 를 열기 전(콜드·pending 판정)
    에도 같은 값이 나와야 한다. 소스 이름은 content_hash 에 이미 반영돼 정보 손실이 없다.
  - **pending payload**: 사용자 대기 경로(`ai_inline=False`)는 캐시 히트만 쓰고, 미스면
    `compare` 키 **없이** `compare_pending: true` 를 얹어 리포트를 먼저 연다
    (`metrics.build_report_payload(compare_deferred=True)`). 프런트 `compare.js` 가 이
    플래그로 "⏳ 계산 중"과 "데이터 없음"을 구분하고, `boot.js` 폴링이 완료 시 다시 그린다.
  - **pending 키가 갈린다**: `report_pending_key(..., kinds)` — ai 단독은 종전 꼬리
    (`aipending`) 그대로라 **기존 파일이 계속 유효**하고, compare 가 끼면 별도 키가 된다.
    안 갈리면 "AI 만 빈 본"과 "둘 다 빈 본"이 서로 덮어써 이미 계산된 부분이 사라진 본이
    최종본처럼 재사용된다. 최종본 승격 시 **3가지 조합을 전부** `drop_report` 한다.
  - 백그라운드 잡은 `"compare"`(= `report_job(ai_inline=True)` — `"ai"` 와 같은 잡이라 둘 다
    pending 이면 `"ai"` 하나만 예약해 같은 콜드 빌드를 두 번 하지 않는다). etag 꼬리는
    무엇이 대기 중인지까지 담는다(`-ai`/`-cmp`/`-aicmp`).
  - **Excel 다운로드는 compare 완료까지 기다린다**(`client/excel_download/_fetch.py`
    `_wait_compare_ready`, 상한 180초). AI Comment 는 셀만 비지만 Compare 는 **시트 단위**로
    빠져 사용자가 산출물이 잘못된 줄 모르고 쓰게 되기 때문이다. 상한 초과 시 경고만 남기고
    나머지 시트로 진행한다(다운로드 전체 실패가 더 나쁘다).
  - 가드: perf_guard `S12-compare-cache`, `tests/test_compare_async.py`,
    값 회귀는 기존 `tests/test_compare_equivalence.py` 가 그대로 지킨다.
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
| `WEB_REPORT_DIST_SEQ_CACHE` | `32` | Distribution **Serial 순** 배치 응답 gzip 캐시 개수 |
| `WEB_REPORT_DIST_SEQ_CACHE_MB` | `256` | 〃 바이트 상한 (개수와 이중 적용, 0=비활성). seq 는 pack 지름길이 없어 계산이 비싸다 — 히트율을 ECDF 와 따로 지킨다 |
| `WEB_REPORT_DIST_CHUNK_CACHE` | `64` (운영 `128`) | dist pack chunk **디코드 결과** 캐시 개수 (distribution_batch 의 gunzip+json.loads 반복 제거) |
| `WEB_REPORT_DIST_CHUNK_CACHE_MB` | `512` | 〃 비압축 바이트 상한 (개수와 이중 적용, 0=비활성) |
| `WEB_REPORT_ONDEMAND_WORKERS` | `2` (운영 `8`) | 콜드 미스 조회가 202 를 반환한 뒤 백그라운드에서 빌드하는 소비자 스레드 수. `_COMPUTE_WORKERS` 와 같은 값으로 유지 |
| `WEB_REPORT_COMMONALITY_CACHE` | `2` | Commonality 인덱스 캐시 개수 |
| `WEB_REPORT_COMPARE_CACHE` | `4` | Compare 계산 결과 캐시 개수 (콜드 빌드의 34%를 차지하던 계산) |
| `WEB_REPORT_COMPARE_CACHE_MB` | `256` | 〃 바이트 상한 (개수와 이중 적용, 0=비활성). 값에 common_map 이 있어 세션당 수 MB |
| `WEB_REPORT_TRIM_CACHE` | `4` | Trim payload 캐시 개수 |
| `WEB_REPORT_TRIM_CHART_CACHE` | `64` | Trim 그룹 차트 캐시 개수 |
| `WEB_REPORT_TRIM_CHART_CACHE_MB` | `256` | Trim 그룹 차트 gzip 바이트 상한 (개수와 이중 적용, 0=비활성). 차트 1건이 전 die 전 포인트라 개수 상한만으론 RAM 이 예측 불가 |
| `WEB_REPORT_MANIFEST_CACHE` | `16` | manifest 캐시 개수 |
| `WEB_REPORT_FULL_CACHE` | `8` (운영 `32`) | `/full` 응답 gzip 캐시 개수 |
| `WEB_REPORT_FULL_CACHE_MB` | `512` (운영 `1024`) | 〃 바이트 상한 (개수와 이중 적용, 0=비활성) |
| `WEB_REPORT_SCATTER_CACHE` | `16` | `/scatter` 응답 gzip 캐시 개수 |
| `WEB_REPORT_GAP_CACHE` | `16` | Gap Chart 응답 gzip 캐시 개수 |
| `WEB_REPORT_GAP_CACHE_MB` | `256` | 〃 바이트 상한 (개수와 이중 적용, 0=비활성). 건당 크기가 `/scatter` 급이다 |
| `WEB_REPORT_GAP_WARM_MAX` | `2` | 저장 1회에 **미리 계산**해 둘 Gap Chart 수 (`0` = 끔) |
| `WEB_REPORT_SCATTER_CACHE_MB` | `256` | 〃 바이트 상한 (개수와 이중 적용, 0=비활성) |
| `WEB_REPORT_DISK_CACHE_MAX_GB` | `500` | 디스크 캐시 총량 상한 (0 이하 = 비활성) |
| `WEB_REPORT_REWARM_ON_START` | `1` | 기동 후 재웜 스윕 사용 여부 (`0` = 끔) |
| `WEB_REPORT_REWARM_LIMIT` | `30` | 스윕이 훑을 최근 web_report 세션 수 |
| `WEB_REPORT_REWARM_DELAY_SEC` | `60` | 기동 후 스윕 시작까지 지연(초) |

## Gap Chart — 저장 직후 프리컴퓨트 (2026-08-24)

gap 캐시 키(`cache_policy.gap_key`)에는 `spec_digest` 가 들어간다. 그래서 **새로 만들거나
수식을 고친 직후는 정의상 100% 캐시 미스**이고, 그 계산이 사용자의 첫 조회 요청 안에서
통째로 일어나 카드가 "계산 중…" 으로 머물렀다.

`POST .../web_report/gap_charts` 가 저장 응답을 보낸 뒤
`response_cache.warm_gap_chart` 로 **부모 프로세스의 데몬 스레드** 하나를 띄워 미리 만든다.
모달이 닫히고 갤러리가 다시 그려지는 사이에 계산이 진행되고, 뒤늦게 도착한 조회는 같은
`keyed_lock` 에서 결과를 받는다(중복 계산이 아니라 대기 후 재사용).

- ⚠️ **컴퓨트 워커(별도 프로세스)에서 계산하면 안 된다** — `_GAP_CACHE` 는 웹 프로세스 RAM 의
  OrderedDict 라 자식 프로세스의 결과가 부모에 남지 않는다.
- 데우는 변형은 **전체 기준(bin1=False) 하나**다. Bin1 계열 토글이 켜진 채 저장하면 빗나가지만
  그때도 종전과 같은 인라인 계산으로 떨어질 뿐 손해가 없다.
- 동시 실행은 1개(`_GAP_WARM_LOCK`, non-blocking) — 저장 연타로 스레드가 쌓이지 않게.
- 실패는 조용히 무시한다(조회가 다시 계산한다).

**실측 (2026-08-24, 5 source × 25,000 die · 항목 40개 · 참조 2개, tables 웜):**
`build_gap_item` 0.31s + `json.dumps` 0.05s(raw 4.5MB) + gzip(level=1) 0.01s(1.28MB) = **약 0.37s**.
meta 3벌(serial/xpos/ypos) 문자열 변환이 그중 약 0.16s 인데,
**`pd.to_numeric` 선변환으로 벡터화해도 순이득이 0.06s 미만**이라 하지 않았다 —
XPOS/YPOS(정수)만 1.9배 빨라지고 SERIAL(비수치 문자열)은 파싱이 전부 실패해 1.6배 느려진다.
게다가 그 문자열은 hover 키·Map 좌표 매칭에 쓰여 한 글자만 달라져도 조용히 어긋난다
(`gap_chart._series_entry` 주석에 같은 내용을 박아 뒀다).

## 불변 규칙
- 캐시는 전부 재계산 가능한 파생물 — 언제 지워져도 무해해야 한다.
- manifest write-through 일관성은 **단일 프로세스 전제**에 의존한다. 멀티프로세스로
  바꾸면 이 전제가 깨지므로 캐시/무효화 설계를 재검토할 것.
- 캐시 키 구성은 항상 `cache_policy.py` 빌더를 통한다 — 호출부에서 즉석으로 키를 만들지 말 것.

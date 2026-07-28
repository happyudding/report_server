# Flask 서버

Honey 클라이언트가 업로드한 산출물(xlsx 추출 grid / web_report parquet)을 수신·저장하고
브라우저 검색결과·세션 상세 페이지로 제공한다. 이 문서는 **환경변수·API 엔드포인트·모듈
구조의 정본**이다. 데이터 흐름·불변 규칙은 [../CLAUDE.md](../CLAUDE.md) 와
[../docs/INDEX.md](../docs/INDEX.md) 참조.

---

## 요구사항 / 실행

Python 3.11+ (web_report 컴퓨트 워커의 `ProcessPoolExecutor(max_tasks_per_child=...)` 가
3.11 신설이라 3.10 에선 기동 불가). 의존성은 [requirements.txt](requirements.txt) 참조
(버전은 그 파일이 정본).
pyyaml 은 eval_analyzer(eval_engine) rules 로딩용 — ai_comment 옵션 세션의 IssueTable
AI Comment 평가 경로([../web_report/ai_comment.py](../web_report/ai_comment.py),
[../docs/13](../docs/13_eval_analyzer_integration.md))에서만 쓰인다.

```powershell
cd F:\COINAPI\report_server\server
pip install -r requirements.txt

.\start.bat        # 또는 python wsgi.py
```

기동 후 `http://127.0.0.1:8080/pe/report/` 에서 검색결과 페이지 확인. LAN 전체 노출은
`HOST=0.0.0.0`.

---

## 환경변수

### 서버 기동 / 경로

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `HOST` | `0.0.0.0` | 바인드 주소 (모든 인터페이스 = 운영 IP 12.81.220.117 포함) |
| `PORT` | `8080` | 포트 (클라이언트 `HONEY_SERVER_URL` 과 일치) |
| `SERVER_BASE_URL` | `http://12.81.220.117:8080` | 절대 URL 생성 기준 (운영 서버 주소). **정본은 `env/server.env`** — 서버가 직접 읽고, `build_zip` 도 여기서 읽어 클라 배포본에 넣는다 |
| `REPORT_DB_PATH` | `<repo>/DB/pe/report/report.db` | SQLite DB 파일 |
| `REPORT_EVAL_DB_PATH` | `<repo>/DB/pe/report/eval/eval.db` | Issue Table PTE/개발 comment export DB (eval.db 스키마, report.db 와 분리 — [docs/13 §9](../docs/13_eval_analyzer_integration.md)) |
| `REPORT_UPLOAD_DIR` | `<repo>/uploads/report` | 업로드/로컬 폴백/디스크 캐시 루트 |
| `HONEY_RELEASES_DIR` | `<repo>/server/releases` | Honey exe 릴리스 폴더 |
| `PRODUCT_INFO_DB_PATH` | `<repo>/DB/pe/report/product_info.db` | 기준정보 DB — Product 검색 후보(part_ids)와 세션 기준정보 lookup. **읽기 전용**. 원본 CSV 가 DRM 이라 Excel 있는 별도 PC 에서 [tools/product_info_import](../tools/product_info_import/README.md) 로 만들어 수동 복사한다. (mtime, size) 바뀌면 자동 재로딩(재기동 불필요) |

### 인증 (SSO 전환)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `AUTH_SSO_HEADER` | `""`(비움) | 비우면 Honey UA(`HoneyUser/<계정>`)로 신원 식별. 역프록시 SSO 헤더명(예 `X-Auth-User`) 지정 시 그 헤더가 우선 ([auth_identity.py](auth_identity.py)) |

### S3 (선택 — 외부 스토리지, 현재 코드에선 검증용. 미설정 시 로컬 폴백)

> S3/storage_gateway 는 **외부 담당자 영역·동결** 경계다([storage_gateway/README.md](storage_gateway/README.md)).
> 미설정(`REPORT_S3_BUCKET` 비움)이면 산출물은 `REPORT_UPLOAD_DIR` 로컬에 저장되고 조회도
> 로컬을 따른다. yield rows 등 DB 저장은 S3 와 무관하게 정상.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `REPORT_S3_BUCKET` | `""` | 버킷 (비우면 로컬 폴백) |
| `REPORT_S3_ENDPOINT` | `""` | 호환 엔드포인트 (AWS 는 비움) |
| `REPORT_S3_REGION` | `us-east-1` | 리전 |
| `REPORT_S3_ACCESS_KEY` / `REPORT_S3_SECRET_KEY` | `""` | 비우면 boto3 기본 자격증명 |
| `REPORT_S3_MAX_POOL_CONNECTIONS` | `30` | boto3 커넥션 풀 |

S3 키 prefix(`REPORT_S3_*_PREFIX`, 모두 `pe/report_server/` 네임스페이스)는
[config.py](config.py) 와 [storage_gateway/README.md](storage_gateway/README.md) 참조.

### web_report 캐시 / 컴퓨트

캐시 계층·환경변수 전체는 [../docs/12_web_report_cache.md](../docs/12_web_report_cache.md) 가
정본. 자주 만지는 것: `WEB_REPORT_COMPUTE_WORKERS`(기본 2 / **운영 4**, 0=인라인),
`WEB_REPORT_TABLES_CACHE_MB`(기본 4096 / **운영 2048** — 부모·워커가 각자 갖는 상한),
`WEB_REPORT_DISK_CACHE_MAX_GB`(기본 500),
`WEB_REPORT_REPORT_CACHE_MB`(기본 256 — report dict 바이트 상한),
`WEB_REPORT_TRIM_CHART_CACHE_MB`(기본 256 — Trim 그룹 차트 gzip 바이트 상한),
`WEB_REPORT_ONDEMAND_WORKERS`(기본 2 / **운영 4** — 콜드 202 후 백그라운드 빌드 스레드),
`WEB_REPORT_DIST_CHUNK_CACHE_MB`(기본 512 — dist pack chunk 디코드 결과 캐시).
컴퓨트 워커 2종은 **짝으로** 올려야 한다 — 풀만 늘리면 소비자 스레드 수가 새 상한이 된다.

### 세션/DB 유지보수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `REPORT_RETENTION_DAYS` | `180` | (보존기간 만료 **삭제는 폐지** — 티어링이 대체. 값은 표시용으로만 남음) |
| `REPORT_CLEANUP_DRYRUN` | `1`(참) | **기본은 실삭제 안 함**(대상만 로그). 실삭제는 `0` 으로 명시. 대상=고아 세션행·휴지통 경과분·고아 산출물 |
| `REPORT_AUDIT_RETENTION_DAYS` | `365` | 감사 로그 롤오프. 0 이하 = 무기한. **cleanup dry-run 과 무관하게 항상 실행** |
| `REPORT_DB_BACKUP_ENABLED` / `_INTERVAL_HOURS` / `_KEEP` / `_DIR` | `1` / `24` / `7` / `<db>/backup` | 온라인 백업 사이클. 대상은 report.db + eval.db (voc.db 는 VOC 미사용 중이라 제외, DB 별 prefix 로 rotation) |
| `REPORT_DB_BACKUP_EXTERNAL_DIR` | (없음) | 지정 시 integrity 통과 백업본을 이 경로로도 복사(best-effort). 같은 디스크 사망 대비 |
| `REPORT_WEBREPORT_TOTAL_MB` | `1024` | web_report parquet **합계** 상한. 개별 파일은 512MB 고정, 요청 전체는 `MAX_CONTENT_LENGTH_MB` |
| `WEB_REPORT_PREWARM_QUEUE` | `8` | 업로드 직후 프리웜 대기 큐 상한. 초과 시 가장 오래된 요청 폐기(로그) |
| `REPORT_ADMIN_SECRET` | `pte` | admin 경로 조각 → `/pe/admin-<secret>/` (기본 `/pe/admin-pte/`) |

### 로그 / 무인 운영 (wsgi.py, watchdog.ps1)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `LOG_MAX_MB` | `256` | 활성 콘솔 로그(`server/log/server_*.txt`) 크기 상한 — 초과 시 새 파일로 로테이션. `0` 이하 = 비활성 |
| `LOG_KEEP_FILES` / `LOG_KEEP_DAYS` | `30` / `14` | 로그 파일 정리 상한(개수/일수) — 기동·로테이션 시 초과분 삭제. faulthandler·metrics 파일도 `LOG_KEEP_DAYS` 준용 |
| `LOG_MIN_KEEP_HOURS` | `48` | **이 시간 안쪽 `server_*.txt` 는 개수·용량 상한과 무관하게 보존** — 재기동 폭주(기동 1회=파일 1개)가 원인 구간 로그를 밀어내지 못하게 하는 안전장치 |
| `LOG_KEEP_TOTAL_MB` | `4096` | `LOG_MIN_KEEP_HOURS` 밖 구간에 적용되는 총 용량 상한 (넘어선 지점부터 과거를 삭제) |
| `REPORT_METRICS_FILE_KEEP_DAYS` | `14` | flight recorder(`metrics_YYYYMMDD.log`, 분당 1줄 리소스 추이) + `runtime_YYYYMMDD.log` 보존 일수. `0` = 비활성 |
| `REPORT_RUNTIME_LOG_INTERVAL_SEC` | `300` | `runtime_*.log` 응답시간 스냅샷(p50/p95/p99 + 느린 경로 top5) 기록 주기. 최소 60 |
| `REPORT_SLOW_REQ_MS` | `10000` | 이 시간을 넘긴 요청을 `runtime_*.log` 에 개별 기록. `0` 이하 = 비활성 |
| `WATCHDOG_BACKOFF_MAX_PER_HOUR` / `_GAP_MIN` | `3` / `30` | (watchdog.ps1) healthz 계열 재기동 백오프 — 최근 1시간 재기동이 임계 이상이면 마지막 재기동 후 지정 분이 지날 때까지 재기동을 건너뛴다 |
| `WATCHDOG_BACKOFF_NL_MAX` / `_NL_GAP_MIN` | `6` / `15` | (watchdog.ps1) `not_listening`(프로세스 사망) 백오프 — 가용성 우선이라 더 관대 |

**`server/log/` 파일 종류** (모두 위 정리 정책으로 자동 회수):

| 파일 | 생성 주체 | 내용 |
|------|-----------|------|
| `server_<stamp>.txt` | wsgi(부모) | 콘솔 로그 tee. 기동마다 새 파일 = **파일 수가 재기동 횟수**. 이제 `logging` INFO 도 타임스탬프 포함해 여기 남는다 |
| `faulthandler_<stamp>.txt` | wsgi(부모) | 네이티브 크래시(세그폴트/OS 강제종료) 스택. server_ 와 stamp 공유. **크래시 없으면 0바이트→다음 기동 시 삭제** |
| `faulthandler_worker_<pid>.txt` | 컴퓨트 워커 | 워커 프로세스 네이티브 크래시(OOM 등) 스택. per-PID |
| `metrics_YYYYMMDD.log` | metrics 샘플러 | flight recorder — `ts,cpu,rss,mem_used,inflight,win_peak,workers_rss` (분당 1줄). 크래시 직전 리소스 추이 부검용. 7번째(컴퓨트 워커 RSS 합)는 2026-07-28 추가 — **뒤에 붙여** 6컬럼 구파일도 그대로 파싱된다 |
| `runtime_YYYYMMDD.log` | metrics 샘플러 | 응답시간 스냅샷(`type:lat`, 5분마다) + 느린 요청 개별 기록(`type:slow`, JSON lines). **재시작으로 초기화되지 않는 부하 이력** — admin '이력' 탭이 읽음 |
| `watchdog_events.log` | watchdog | 재기동/실패 이벤트(JSON lines) — admin 대시보드 현황 탭이 읽음 |
| `watchdog_checks.log` | watchdog | **매 실행 1줄**(JSON lines) — 실행 빈도 자체. `mutex_busy` = 태스크 겹쳐 뜬 직접 증거 |
| `watchdog_snap_<stamp>.txt` | watchdog | 재기동 직전 프로세스 부검 + 최신 `server_*.txt` 마지막 20줄 스냅샷(죽은 이유 원문) |
| `diagnose_<stamp>.txt` | diagnose_watchdog.ps1 | 진단 스크립트 리포트(수동 실행 시) |

**watchdog 자동 재기동**: [register_watchdog.bat](register_watchdog.bat) 을 관리자 권한으로
1회 실행하면 작업 스케줄러에 5분 주기 + 부팅 시 감시([watchdog.ps1](watchdog.ps1))가
등록된다 — 포트 미리스닝이면 즉시, `/healthz` 무응답이면 2연속 실패 시 자동 재기동.
재기동 이력은 admin 대시보드 현황 탭 또는 `server/log/watchdog_events.log`.
수동 점검 시간에는 `schtasks /Change /TN report-server-watchdog /DISABLE` 로 먼저 정지할 것.

**재기동 백오프**(2026-07-23): 재기동해도 낫지 않는 상태에서 10분마다 재기동을 반복하면
(관측 142회/일 — healthz 상시 실패 시의 이론 최대치 144회에 근접) 서버가 종일 기동 중이라
오히려 복구를 막고 `server_*.txt` 를 밀어내 원인 추적까지 없앤다. 최근 1시간 재기동이
`WATCHDOG_BACKOFF_MAX_PER_HOUR` 이상이면 재기동을 건너뛰고(`backoff_skip` 이벤트) 판정만
기록한다. **판정 로직(포트/healthz/2연속)은 불변**이고 연속 실패 카운터도 유지하므로
gap 이 지나면 다음 주기에 곧바로 재기동된다. 억제 상황은 현황 탭 watchdog 타일과
'이상 징후 요약' 칩에 표시된다.

**원인 추적 (admin 대시보드)**: 현황 탭 **Watchdog 상세** 카드에서 24시간 점검 결과 분포
(정상/healthz 실패/백오프 억제/재기동/태스크 겹침) · `/healthz` 응답시간 추이 · 최근 점검
20건(코드·소요·연속실패·부검 스냅샷 링크)을 본다. 스냅샷 링크를 누르면 **console log 탭**이
그 파일을 연다 — 이 탭은 `server_*.txt` 외에 `watchdog_*` · `metrics_*` · `runtime_*` ·
`faulthandler_*` · `diagnose_*` 도 선택해 볼 수 있다(그 외 파일은 열람 거부).
재기동 원인 판별: `healthz_503` = `/healthz` 의 DB 체크 실패(report.db 잠금/디스크),
`healthz_timeout`(code=0, ms≈30000) = 스레드 고갈·CPU 포화, `not_listening` = 프로세스 사망.

**재기동 폭주 진단** (짧은 시간 다수 재기동이 의심될 때): 운영 PC 에서 관리자 권한으로
[diagnose_watchdog.ps1](diagnose_watchdog.ps1) 을 1회 실행하면(read-only) events 간격 분석 ·
예약 작업 중복 여부 · **TaskScheduler operational 로그의 실제 기동 횟수**(핵심 증거) ·
`server_*.txt` 생성 클러스터 · python 크래시/PC 재부팅 이벤트를 한 리포트로 모아
`server/log/diagnose_<stamp>.txt` 에 남긴다:
`powershell -NoProfile -ExecutionPolicy Bypass -File .\diagnose_watchdog.ps1 -Hours 48`
> 참고: TaskScheduler operational 로그가 비활성이면 실기동 횟수를 확인할 수 없다 —
> 리포트가 활성화 명령을 안내한다.

> **반영 시점**: watchdog.ps1 강화는 재기동 없이 다음 5분 주기부터 적용된다. 그러나
> Python 측 강화(faulthandler·`logging` INFO·flight recorder)는 **서버 재기동 후에만**
> 적용된다(terminate.bat → start.bat, 이때 watchdog 일시 정지 절차 준수).

### 운영 배포 체크리스트 (8cpu / 32GB / 2TB, 동시 ~5명 기준 — 2026-07-15)

배포·재기동 시 이 순서로 env 를 확인한다. 기본값이 적절한 항목은 "기본 유지"로 표기.

| 확인 항목 | 권장 | 이유 |
|-----------|------|------|
| `HOST` / `PORT` | 기본(`0.0.0.0` / `8080`) 유지 | `env/server.env` 가 정본. 포트를 바꾸면 클라이언트 `HONEY_SERVER_URL` 도 함께 바꿔야 한다 |
| `WAITRESS_THREADS` | 기본(13) 유지 | 동접 처리용. waitress 본문 상한은 `MAX_CONTENT_LENGTH_MB` 와 자동 정합(wsgi.py) |
| `MAX_CONTENT_LENGTH_MB` | 기본(2048) 유지 | 업로드 본문 상한(parquet + dist blob 첨부 합산). waitress/Flask 공용 |
| `WEB_REPORT_COMPUTE_WORKERS` + `_ONDEMAND_WORKERS` | **4 / 4** (server.env 에 명시) | 콜드 빌드 워커. **둘을 짝으로** 올릴 것(풀만 늘리면 소비자 스레드가 새 상한). 제약은 RAM 이 아니라 CPU — 유휴 워커 약 100MB 실측, 8코어에서 4건 동시 콜드 빌드 시 4코어를 버스트로 쓴다. `_ONDEMAND_WORKERS` 는 부모 tables 가 웜인 세션을 **부모에서 인라인** 빌드하므로(should_offload=False) 값이 크면 부모 GIL 경합도 는다 |
| 적정성 판단 | admin 현황 탭 "온디맨드 대기" | 상시 0 = 워커 과잉(줄여도 됨) / 자주 1 이상 = 늘린 값이 실제로 일하는 중. CPU 피크와 함께 볼 것 |
| `WEB_REPORT_TABLES_CACHE_MB` | **2048** (server.env 에 명시) | **부모와 워커가 각자** 갖는 상한이라 실효 천장 = 값 × (1+워커수). 실데이터(세션 ≈229MB)에선 개수 상한(4건 ≈0.9GB)이 먼저 걸려 2048 은 발동하지 않는다 = 성능 손실 없이 천장만 절반 |
| `WEB_REPORT_DIST_CACHE_MB` | 기본(1024) 유지 | 부모 프로세스 RAM 상한 |
| 서버 RAM 실측 | admin 현황 탭 "report_server RSS (부모+워커)" | 같은 박스의 다른 서비스와 섞이지 않은 우리 몫. 워커 증설 판단은 이 값으로 |
| `REPORT_CLEANUP_DRYRUN` | 실삭제 원하면 `0` 명시 | **기본 1 = orphan 회수도 로그만** 남김 |
| `REPORT_TIER_ENABLED`/`REPORT_TIER_DRYRUN` | S3 확정 환경에서 dryrun 해제 검토 | 티어링이 로컬 hot 캐시를 S3 로 내려 2TB 디스크를 지킴 (S3 미설정 시 no-op) |
| `REPORT_DB_BACKUP_DIR` | **다른 물리 디스크/네트워크 경로 지정 권장** | 기본은 DB 옆 폴더 — 디스크 사망 시 원본과 백업이 함께 유실 |
| `WEB_REPORT_DIST_GZIP_LEVEL` | 실데이터 실측 후 `6` 검토 | 서버 dist blob 전송량 절감 (클라 프리컴퓨트 blob 은 이미 level 6) |
| 장기 무중단 실행 | `register_watchdog.bat` 1회 등록 | 크래시/재부팅 자동 재기동 (5분 주기 + 부팅 시). 활성 로그는 `LOG_MAX_MB`(기본 256) 초과 시 자동 로테이션 |
| 콜드 빌드 관측 로그 | `dist cold build`/`report cold build` INFO 라인 주시 | 포인트 수가 수천만 급이면 docs 진단의 보류 항목(항목 청크 분할 등) 재검토 |

---

## API 엔드포인트

신원은 `HoneyUser/<계정>` User-Agent 로 자동 식별한다(일반 브라우저 = 신원 없음 = 읽기
전용). 접근 수준: **공개**(누구나) / **Honey**(Honey 접속 사용자) / **업로더**(세션 업로더
본인) / **편집자**(업로더 또는 위임받은 편집자). 브라우저 변경요청은 CSRF double-submit
쿠키, Honey 전용 업로드는 `X-Honey-Agent` 헤더로 구분. 상세 가드는
[../docs/02_server_query_edit.md](../docs/02_server_query_edit.md) 참조.

### 업로드 (`/pe/report/`) — Honey 클라이언트 전용

| 메서드 | 경로 | 접근 | 설명 |
|--------|------|------|------|
| `POST` | `/upload_xlsx` | Honey | xlsx 추출 grid(JSON) + issue PNG 업로드 |
| `POST` | `/upload_webreport` | Honey | web_report honeyform parquet + manifest 업로드. 선택 필드 `dist_blob`/`dist_blob_bin1`(클라 프리컴퓨트 Distribution ECDF gzip — 검증 후 dist 캐시 시딩, 미첨부 시 서버 폴백 계산) |
| `GET` | `/web_report/<sid>` | 공개 | `/view/<sid>` 로 리다이렉트 |

### 세션 조회/변경 (`/pe/report/`)

| 메서드 | 경로 | 접근 | 설명 |
|--------|------|------|------|
| `GET` | `/` | 공개 | 검색결과 페이지 (HTML) |
| `GET` | `/view/<sid>` | 공개 | 세션 상세 페이지 (HTML) |
| `GET` | `/api/history` | 공개 | 세션 목록 JSON (필터: product_type/product/lot_id/source) |
| `GET` | `/api/part_ids` | 공개 | 기준정보 part id 목록 (product_info.db 의 part_id + sub_part_id flatten) |
| `POST` | `/api/client_error` | 공개 | 브라우저 JS 에러 beacon 수신 (error_beacon.js — CSRF 미적용, per-IP 스로틀, 감사 action=`client_error`) |
| `GET` | `/result/<sid>` | 공개 | 세션 요약 JSON |
| `GET` | `/session/<sid>` | 공개 | 세션 메타 JSON (password 제거, has_password 만) |
| `GET` | `/session/<sid>/full` | 공개 | 세션 전체 데이터 JSON (summary+objects+주석+추출텍스트). web_report 세션이 **콜드**면 빌드를 백그라운드에 걸고 `202 {"building":true,"stage","elapsed"}` 즉시 반환 — 프런트가 재시도 (warm 은 종전대로 200) |
| `GET` | `/session/<sid>/my_access` | Honey | 현재 사용자의 이 세션 권한 |
| `DELETE` | `/session/<sid>` | 업로더 | 세션 삭제 |
| `POST` | `/session/<sid>/important` | Honey | 개인 중요표시 토글 |
| `POST` | `/session/<sid>/private` | 업로더 | 비공개 토글 |
| `PATCH` | `/session/<sid>/meta` | 편집자 + Honey | 세션 메타 수정 — `{file_name, family_product, product, lot_id, process}`. **`X-Honey-Agent: 1` 필수**(= Honey 앱 전용 강제, CSRF 대체). product 변경 시 product_info.db 재lookup(미등록이면 기준정보 14컬럼 비움). product_type·analysis_key 는 불변 |
| `GET` | `/honey/session_meta/<sid>` | 공개 | 위 편집창의 **진입 URL** — Honey 내장 브라우저가 네비게이션을 가로채 편집창을 띄우므로 실제로는 요청되지 않는다. 가드 없는 환경용 안내 HTML |
| `POST` | `/session/<sid>/verify_password` | Honey | **하위호환 스텁** — UA 업로더 확인만, 항상 `has_password:false` |
| `PATCH` | `/session/<sid>/content` | — | **비활성, 항상 405** (구 xlsx 텍스트 수정 폐기) |
| `GET`/`POST` | `/session/<sid>/editors` | 업로더 | 편집자 위임 조회/부여 |
| `DELETE` | `/session/<sid>/editors/<user>` | 업로더 | 편집자 회수 |
| `GET` | `/session/<sid>/editors/candidates` | 업로더 | 편집자 후보(web_visitor 풀) |

### web_report 데이터/편집 (`/pe/report/session/<sid>/web_report/`)

조회는 공개, 편집(`edit`/`overrides`/`etc`/`comments`/`engr`/`chart_notes`/`note`/
`note_image`/`rawdata_replace`/`preprocess`)은 CSRF + 편집자 가드. 계약 상세는
[../docs/11_web_report_tabs.md](../docs/11_web_report_tabs.md).

| 메서드 | 경로 | 접근 | 설명 |
|--------|------|------|------|
| `GET` | `/raw_data/columns`, `/raw_data` | 공개 | Raw Data 컬럼 UI / 조회 |
| `POST` | `/raw_data/edit` | 편집자 | Raw Data 셀 편집 (parquet 재인코딩) |
| `GET` | `/distribution` | 공개 | Distribution ECDF **전량** (컴팩트 gzip, 전 포인트). 클라 프리컴퓨트 시딩·하위호환 폴백용 — 프런트는 아래 배치를 쓴다 |
| `GET` | `/distribution_batch?subjects=a,b,c[&bin1=1]` | 공개 | Distribution ECDF **항목 배치** (화면에 보이는 항목만, 최대 40개/요청). 전량 payload 의 부분집합과 값 동일 |
| `GET` | `/map_analysis` | 공개 | Map Analysis die 전량 (gzip+ETag — `/full` 은 dies 뺀 경량 메타, schema v8). 콜드면 `202 {"building":true}` |
| `GET` | `/scatter/<subject>` | 공개 | 항목 상세 산포 (전 측정값) |
| `GET` | `/trim_analysis`, `/trim_chart` | 공개 | Trim 매칭·통계 / 그룹 차트 1개 (gzip+ETag). 프런트는 배치를 쓰고 이 단일 경로는 폴백·하위호환용 |
| `GET` | `/trim_chart_batch?source=&group=A&group=B…` | 공개 | Trim 그룹 차트 **배치** (1~6개=산포 한 페이지, `group` 반복 param **순서 유지**) → `{"charts":[...]}` gzip. 각 chart 는 단일 `/trim_chart` 결과와 값 동일. 콜드면 컴퓨트 워커로 오프로드. **Trim 탭은 「분석 시작」 버튼을 눌러야 호출된다**(탭 진입만으로는 요청 0건) |
| `POST` | `/trim/overrides` | 편집자 | Trim 수동 재배치 저장 |
| `GET` | `/commonality/chips`, `/commonality/chip` | 공개 | Commonality chip 검색 / 백분위 |
| `POST` | `/issue_table/etc`, `/issue_table/comments`, `/summary/engr` | 편집자 | Issue/Summary 편집 |
| `POST` | `/issue_table/hidden` | 편집자 | Issue 행 숨김/전체 초기화 (kind=issue_hidden, Yield/CPK 만) |
| `POST` | `/issue_table/status` | 편집자 | Issue 행 Status Open/Close (kind=issue_status, Close 만 저장) |
| `POST` | `/chart_notes` | 편집자 | 차트 주석(도형/텍스트/코멘트) 저장 (kind=chart_note) |
| `GET`/`POST` | `/note` | 공개/편집자 | Note 탭 시트 JSON 지연 조회 / 저장 (kind=note_sheet, ≤2MB) |
| `POST` | `/note_image` | 편집자 | Note 이미지 업로드 (PNG/JPEG raw body, ≤2MB·세션 200장) |
| `GET` | `/rawdata_export` | 공개 | Honey Excel 편집용 zip(manifest + source_*.parquet) 내보내기. **ETag = content_hash** — Honey 가 temp 에 받아둔 zip 을 `If-None-Match` 로 물어보면 내용 무변경 시 **304**(서버가 원본을 메모리에 올려 zip 으로 싸는 작업 자체를 안 함) |
| `POST` | `/rawdata_replace` | 편집자 | Raw Data 소스 전체 교체 (Honey 전용, `X-Honey-Agent`). Excel 시트를 지워 source 가 줄면 form 필드 `source_indices`(남긴 원본 idx JSON 배열, 오름차순)를 함께 받아 그 source 를 물리 제거. 선택 첨부 `dist_pack_index`+`dist_pack_chunk_<n>`(클라가 새 parquet 으로 만든 Distribution pack — 업로드 라우트와 같은 규약)을 받으면 새 content_hash 로 영구 저장해 반영 후 콜드 dist 정렬을 없앤다. 반영 후 프리웜을 걸어 리빌드를 컴퓨트 워커로 넘긴다 |
| `GET`/`POST` | `/preprocess` | 공개/편집자 | **조회 전처리** 옵션(항목 제외 / outlier `mean ± k·stdev`) 조회·저장 (kind=preprocess). 원본 parquet 은 그대로 두고 조회 시점에만 적용 — 빈 spec 저장 = 해제, 되돌리기 가능. Honey 는 `X-Honey-Agent` 로 CSRF 대체 |

### 주석 / 즐겨찾기 / 인증 (`/pe/report/`)

| 메서드 | 경로 | 접근 | 설명 |
|--------|------|------|------|
| `POST`/`GET`/`PATCH`/`DELETE` | `/annotation`, `/annotation/<sid>`, `/annotation/<aid>` | 공개* | 주석 CRUD (*변경은 CSRF) |
| `GET`/`POST` | `/api/favorites` | Honey | 개인 즐겨찾기 |
| `POST` | `/api/auth/login` | 공개 | 웹 로그인 (singleID + 비밀번호 4자리). 5회/5분 실패 시 429 |
| `POST` | `/api/auth/signup` | 공개 | **웹 회원가입** — Honey 사용 이력(업로드·web_report 방문)이 **없는 미사용 singleID** 만 자유 가입(403/409로 차단), 가입 즉시 로그인. IP 당 5회/1시간 |
| `GET` | `/api/auth/signup_hint` | 공개 | 회원가입 창 ID 자동완성 힌트 — **요청자 자신의 IP** 로 최근 180일 Honey 업로드 계정 1건 (`{user_id, honey_seen}` / 없으면 `{}`). 신원 판단에는 미사용 |
| `POST` | `/api/auth/set_password` | Honey | 웹 로그인 비밀번호 설정/변경 (Honey 접속 전용 — 본인확인) |
| `POST` | `/api/auth/change_password` | — | **410 Gone** (set_password 로 대체) |
| `POST`/`GET` | `/api/auth/logout`, `/api/auth/me` | 공개 | 로그아웃 / 현재 신원·출처 확인 |
| `GET` | `/_threads` | 공개 | 진단 (스레드 덤프) |

### 이미지 스트리밍 (`/pe/report/`, storage_gateway)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET` | `/chart/<sid>/<idx>` | 차트 PNG |
| `GET` | `/issue_image/<sid>/<row>` | 이슈 이미지 |
| `GET` | `/note_image/<sid>/<image_id>` | Note 탭 이미지 (세션 단위, nosniff) |
| `GET` | `/distribution_combined/<sid>` | 합성 분포 PNG |

### Honey 업데이트 (`/honey/`)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET` | `/honey/version` | 버전 정보 JSON (`version.json` 반환). 호출을 'Honey 실행'으로 사용자별 집계 (`report_usage_daily`, 신원은 HoneyUser UA — 구버전 클라는 IP) |
| `GET` | `/honey/download` | Honey exe/ZIP 다운로드 |
| `GET` | `/honey/announcement` | 릴리스 공지 원문 (`releases/announcement.txt` 그대로, text/plain). 클라가 최신 버전 실행 중일 때 PC 계정별 1회 팝업 → [docs/04](../docs/04_honey_update.md) |

### 관리 대시보드 (`/pe/admin-<secret>/`, 기본 `/pe/admin-pte/`) — 인증 없음, 내부망 전용

비-GET 요청은 `X-Admin-Request: 1` 헤더 요구. `GET /` 대시보드 + `GET /api/*`
(health/storage/s3-status/metrics/stats(daily·users·client_errors·usage(접속 사용량 —
Honey 실행·웹 방문 순위))/sessions/users/
voc(overview·목록, 읽기 전용)/audit(.csv)/logs/list·tail) +
`POST /api/*` (sessions/delete, session/<sid>/important·password, db/backup·cleanup 등).
운영 진단용 GET 4개: `watchdog`(재기동 이력+reason 분포) · `watchdog/checks?hours=`(매 점검
기록 요약) · `metrics/history?window=`(in-memory 10초 해상도) · `metrics/file_history?hours=`
(**파일 기반, 재시작과 무관한 1분 해상도 이력** — 최대 336시간). `logs/tail?file=` 은
`server/log/` 화이트리스트(server_·watchdog_·metrics_·runtime_·faulthandler_·diagnose_) 밖이면 400.
구 공개 `/pe/admin` (`admin_routes.py`)은 **미등록 dead file**.

### 기타

`GET /healthz` (ops), `GET /` (root_redirect → `/pe/report/`).

---

## 모듈 구조

```
server/
├── wsgi.py                   WSGI 진입점 (컴퓨트 워커 __mp_main__ 재임포트 스킵 가드)
├── plugin.py                 register_report_server() — Blueprint 3개 + admin_panel + ops 등록
├── config.py                 환경변수·경로 통합 설정 (정본)
├── auth_identity.py          신원 provider 체인 (HoneyUser UA 기본 / AUTH_SSO_HEADER SSO)
├── upload_xlsx.py            POST /upload_xlsx 핸들러
├── upload_webreport.py       POST /upload_webreport 핸들러 (web_report.ingest 호출)
├── xlsx_parser.py            시트 grid → 텍스트 추출 (_GridSheet 셸, openpyxl 미사용)
├── honey_routes.py           /honey/version, /honey/download, /honey/announcement
├── admin_routes.py           [미등록 dead file — /pe/admin 구현, admin_panel 로 흡수됨]
├── report_cleanup.py         오래된 세션·감사로그 정리 (DRYRUN 기본)
├── db_backup.py              report.db 온라인 백업 사이클
├── report/
│   ├── report_extension.py   report_bp 정의 + DB init + web_report 저장 포트 주입
│   ├── report_routes.py      라우트 집결자 (구현은 아래 4모듈)
│   ├── security.py           CSRF·신원 가드(_uploader_guard/_editor_guard)·감사 헬퍼
│   ├── routes_session.py     세션 조회/삭제/권한·편집자 위임 라우트
│   ├── routes_webreport.py   web_report 데이터/편집 라우트
│   ├── routes_misc.py        페이지·history·주석·favorites·auth 스텁·정적
│   ├── static_pages.py       검색결과/상세 HTML 서빙 헬퍼
│   ├── static/webreport/     세션 상세 JS 모듈 (탭별, 순서 로드)
│   ├── report_analysis_index.html  검색결과 페이지
│   ├── report_view.html      세션 상세 (마크업+CSS)
│   └── admin_dashboard.html  구 감사로그 대시보드 (admin_panel 로 대체)
├── database/                 SQLite 계층 (report_db.py 는 재노출 facade)
│   ├── core.py               SCHEMA(정본)·마이그레이션·get_conn·analysis lock
│   ├── sessions.py / objects.py / audit.py / users.py / annotations.py
│   ├── usage.py              접속 사용량 일별 집계 (Honey 실행·웹 방문)
│   ├── webreport_edits.py    web_report 편집 상태 (세션 단위)
│   └── models.py             Session dataclass (Mapping 호환)
├── storage_gateway/          S3 산출물 저장 단일 진입점 (ENTRYPOINT/EXTERNAL_OWNER)
│   ├── __init__.py           facade (공개 API + 예외 + 저장 위치 기록)
│   ├── routes.py             이미지 URL 라우트
│   ├── _s3.py               boto3 어댑터 + 키 빌더 (내부)
│   ├── _issue_images.py     이슈 이미지 (S3+로컬 폴백)
│   └── _note_images.py      Note 탭 이미지 (S3+로컬 폴백, 세션 단위)
├── admin_panel/              /pe/admin-<secret>/ 대시보드 + metrics 샘플러
│   ├── __init__.py           register_admin_panel() + metrics.init_app
│   ├── routes.py / sysinfo.py / stats.py / sessions_admin.py / users_admin.py / voc_admin.py
│   ├── maintenance.py / metrics.py / admin_panel.html
├── tools/migrate_manifest_edits.py  manifest 편집값 → 세션 편집 DB 이전 (운영 1회 실행 완료)
└── releases/version.json     Honey 배포 manifest
```

`web_report/` 패키지(honeyform 처리·탭 계산·캐시)는 server/ 밖에 있으며 blueprint 가 아니라
`report_routes` 가 직접 import 한다. 상세는
[../docs/10_web_report_pipeline.md](../docs/10_web_report_pipeline.md).

---

## DB 초기화

서버 시작 시 `database/core.py` 의 `SCHEMA`(`CREATE TABLE IF NOT EXISTS`)로 자동 생성·
마이그레이션. 테이블 16개 — 목록·컬럼은 [../docs/03_storage.md](../docs/03_storage.md) 와
[../DB/pe/report/report_README.md](../DB/pe/report/report_README.md)(스냅샷) 참조. **스키마
정본은 항상 `database/core.py`.**

---

## 참조 문서

| 내용 | 문서 |
|------|------|
| 업로드 파이프라인 (xlsx) | [docs/01_server_upload.md](../docs/01_server_upload.md) |
| 조회·수정·삭제 라우트 + 접근제어 | [docs/02_server_query_edit.md](../docs/02_server_query_edit.md) |
| SQLite 스키마 + storage_gateway | [docs/03_storage.md](../docs/03_storage.md) |
| storage_gateway facade 교체 가이드 | [storage_gateway/README.md](storage_gateway/README.md) |
| web_report 파이프라인 / 탭 / 캐시 | [docs/10](../docs/10_web_report_pipeline.md) · [11](../docs/11_web_report_tabs.md) · [12](../docs/12_web_report_cache.md) |

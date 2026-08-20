# Flask 서버

Honey 클라이언트가 업로드한 산출물(xlsx 추출 grid / web_report parquet)을 수신·저장하고
브라우저 검색결과·세션 상세 페이지로 제공한다. 이 문서는 **환경변수·API 엔드포인트·모듈
구조의 정본**이다. 데이터 흐름·불변 규칙은 [../CLAUDE.md](../CLAUDE.md) 와
[../docs/INDEX.md](../docs/INDEX.md) 참조.

---

## 요구사항 / 실행

Python 3.11+ (web_report 컴퓨트 워커의 `ProcessPoolExecutor(max_tasks_per_child=...)` 가
3.11 신설이라, 3.10 이하에서는 **콜드 빌드가 전부 TypeError 로 실패**하고 화면에는
"리포트 계산이 반복 실패했습니다" 만 뜬다). 의존성은 [requirements.txt](requirements.txt)
참조 (버전은 그 파일이 정본).

> **⚠️ 파이썬을 새로 깔아도 기존 `.venv` 는 바뀌지 않는다.** venv 는 만들어질 당시
> 인터프리터 경로를 `pyvenv.cfg` 에 박아두므로, 3.10 으로 만든 `.venv` 가 남아 있으면
> 나중에 3.14 를 설치해도 서버는 계속 3.10 으로 뜬다(2026-08-06 실제 사고).
> 그래서 [start.bat](start.bat) · [mypc_start.bat](mypc_start.bat) ·
> [install.bat](install.bat) 이 기동 전에 `.venv` 의 **버전까지** 확인한다:
> - 요구 버전 미만이면 `.venv` 를 `.venv_old` 로 **옮겨 두고**(지우지 않는다) 다시 만든다.
>   새로 만들다 실패하면 원래 것을 되돌리고 기동을 멈춘다.
> - 3.11+ 인터프리터를 못 찾으면 `.venv` 를 건드리지 않고 안내 후 중단한다.
>   임시로 그대로 띄우려면 `set "ALLOW_OLD_PYTHON=1"` (web_report 는 계속 실패한다).
> - 특정 파이썬을 지정하려면 `set "PYTHON=C:\경로\python.exe"`.
>
> 인터프리터 탐색·최소 버전 판정은 [_find_python.bat](_find_python.bat) 한 곳에 있다
> (탐색 순서: `%PYTHON%` → `py -3`(설치된 최신) → `PATH`. 예전에는 PATH 가 먼저라
> 오래된 파이썬이 앞에 있으면 그것으로 venv 가 만들어졌다).
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
| `REPORT_EVAL_IMPORT_PYTHON` | `sys.executable` | Honey 'DB Input' 이 `db_input/import_csv.py` 를 돌릴 인터프리터. 서버가 파이썬 호스트가 **아닐** 때만 지정 ([docs/13 §10](../docs/13_eval_analyzer_integration.md)) |
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
정본. 자주 만지는 것: `WEB_REPORT_COMPUTE_WORKERS`(기본 2 / **운영 8**, 0=인라인),
`WEB_REPORT_TABLES_CACHE_MB`(기본 4096 / **운영 2048** — 부모·워커가 각자 갖는 상한),
`WEB_REPORT_DISK_CACHE_MAX_GB`(기본 500),
`WEB_REPORT_REPORT_CACHE_MB`(기본 256 — report dict 바이트 상한),
`WEB_REPORT_TRIM_CHART_CACHE_MB`(기본 256 — Trim 그룹 차트 gzip 바이트 상한),
`WEB_REPORT_ONDEMAND_WORKERS`(기본 2 / **운영 8** — 콜드 202 후 백그라운드 빌드 스레드),
`WEB_REPORT_DIST_CHUNK_CACHE_MB`(기본 512 — dist pack chunk 디코드 결과 캐시).
컴퓨트 워커 2종은 **짝으로** 올려야 한다 — 풀만 늘리면 소비자 스레드 수가 새 상한이 된다.

**시간 상한·재시도 (2026-08-14 — 콜드 빌드 무한 대기 대응)**. 계산 상한은
`WEB_REPORT_COMPUTE_TIMEOUT_SEC`(기본 300) 하나인데, 그 상한은 **워커 오프로드 경로에만**
걸린다(파이썬 스레드는 강제 종료가 불가하다). 그래서 아래 두 스위치가 기본 켜져 있고,
끄면 그 경로의 계산이 다시 상한 없이 돌 수 있다 — 긴급 롤백용으로만 쓸 것:

| 변수 | 기본 | 뜻 |
|------|------|-----|
| `WEB_REPORT_ONDEMAND_FORCE_OFFLOAD` | 1 | 온디맨드(202) 백그라운드 빌드를 tables 웜이어도 워커로 |
| `WEB_REPORT_HEAVY_FORCE_OFFLOAD` | 1 | dist/temp_map/trim 콜드 산출물도 항상 워커로 (202 규약이 없어 인라인이면 요청 스레드가 무제한 계산) |
| `WEB_REPORT_ONDEMAND_MAX_ATTEMPTS` | 2 | 논리 빌드 1건의 최대 실행 횟수(최초+재시도). 일시 장애(큐 대기 초과·워커 붕괴·취소)만 재시도하고 **워커 hang(순수 TimeoutError)은 재시도하지 않는다** |
| `WEB_REPORT_ONDEMAND_RETRY_DELAY_SEC` | 1 | 재시도 재큐잉 지연 |
| `WEB_REPORT_ONDEMAND_PENDING_TTL_SEC` | 480 | 등록만 남은 "유령" 자동 회수 상한(다음 요청에서 해제 후 즉시 재빌드) |
| `WEB_REPORT_BUILD_STATUS_STALE_SEC` | 900 | 진행 표시 유령 정리 — 이걸 넘긴 등록은 프런트 조회에서 걷어낸다("N초 경과" 무한 증가 차단) |

### 세션/DB 유지보수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `REPORT_RETENTION_DAYS` | `180` | (보존기간 만료 **삭제는 폐지** — 티어링이 대체. 값은 표시용으로만 남음) |
| `REPORT_CLEANUP_DRYRUN` | `1`(참) | **기본은 실삭제 안 함**(대상만 로그). 실삭제는 `0` 으로 명시. 대상=고아 세션행·휴지통 경과분·고아 산출물 |
| `REPORT_AUDIT_RETENTION_DAYS` | `365` | 감사 로그 롤오프. 0 이하 = 무기한. **cleanup dry-run 과 무관하게 항상 실행** |
| `REPORT_CHATBOT_RETENTION_DAYS` | `90` | 챗봇 질문/답변 **전문** 보존. 삭제 직전 `report_chatbot_daily` 일별 비식별 집계로 접는다(추이·부하 지표는 영구). dry-run 무관 |
| `REPORT_USAGE_HOURLY_RETENTION_DAYS` | `90` | 시간별 사용량(`report_usage_hourly`) 롤오프 — 요일×시간 히트맵용이라 최근 구간만 필요. dry-run 무관 |
| `REPORT_USAGE_DAILY_RETENTION_DAYS` | `730` | 일별 사용량·Peak 롤오프(장기 추이라 2년). **eval 지표 일별 집계(`report_eval_daily`)도 이 값을 쓴다**. dry-run 무관 |
| `REPORT_EVAL_ROLLUP_DAYS` | `14` | eval 룰 지표 일별 집계에서 **매번 다시 계산하는 최근 구간**(일). 누적 더하기가 아니라 덮어쓰기라 재실행이 안전하다 — 세션 재수집·뒤늦은 Close 가 과거 날짜 값을 바꾸므로 겹쳐 본다. 0 이하 = 집계 안 함. dry-run 무관(비파괴) |
| `REPORT_EVAL_PURGE_STALE_RUNS` | `0`(끔) | eval.db 옛 스냅샷 run 정리. `force` 재수집이 기존 run 을 지우지 않고 새로 쌓기 때문에(사람 라벨 보호) 반복하면 판정 사본이 늘어난다. 같은 (세션,소스)의 **최신이 아니고 라벨이 하나도 안 붙은** run 만 걷는다(`fail_case`·`label`·마스터는 보존). **`REPORT_CLEANUP_DRYRUN` 을 존중**한다 — 켜기 전에 dry-run 로그로 대상 수를 먼저 볼 것 |
| `REPORT_DB_BACKUP_ENABLED` / `_INTERVAL_HOURS` / `_KEEP` / `_DIR` | `1` / `24` / `7` / `<db>/backup` | 온라인 백업 사이클. 대상은 report.db + eval.db (voc.db 는 VOC 미사용 중이라 제외, DB 별 prefix 로 rotation) |
| `REPORT_DB_BACKUP_EXTERNAL_DIR` | (없음) | 지정 시 integrity 통과 백업본을 이 경로로도 복사(best-effort). 같은 디스크 사망 대비 |
| `REPORT_WEBREPORT_TOTAL_MB` | `1024` | web_report parquet **합계** 상한. 개별 파일은 512MB 고정, 요청 전체는 `MAX_CONTENT_LENGTH_MB` |
| `WEB_REPORT_UPLOAD_CONCURRENCY` | `2` | 동시에 처리하는 web_report 업로드 건수. 업로드 1건이 parquet bytes + 디코드 tables 를 함께 들고 있어 대형 세션이면 건당 RAM 피크가 GB 급 — 겹치면 웹 프로세스가 죽는다 |
| `WEB_REPORT_UPLOAD_WAIT_SEC` | `90` | 위 상한이 찼을 때 대기하는 시간(초). 초과하면 503. 대기 중에는 본문이 디스크에 스풀돼 있어 RAM 을 거의 안 쓴다. ⚠️ **클라 업로드 read timeout(200초)보다 충분히 짧아야 한다** — 대기만 하다 클라가 먼저 끊으면 사용자는 503+`Retry-After` 안내조차 못 받는다. 운영 env 는 90 을 명시하지만, **env 파일을 읽지 않는 기동**(`python wsgi.py` 직접 = 디버그)에서도 같은 값이 되도록 기본값을 180→90 으로 맞췄다(2026-08-19) |
| `WEB_REPORT_UPLOAD_MAX_WAITERS` | `4` | **동시에 줄 설 수 있는** 업로드 요청 수. 대기는 RAM 은 안 쓰지만 waitress 스레드는 문다 — 상한이 없으면 클라 여러 대의 동시 업로드가 스레드를 전부 물어 조회·`/healthz` 까지 수 분간 멎는다. 초과분은 기다리지 않고 즉시 503(`Retry-After: 30`) |
| `WEB_REPORT_PREWARM_QUEUE` | `8` | 업로드 직후 프리웜 대기 큐 상한. 초과 시 가장 오래된 요청 폐기(로그) |
| `WEB_REPORT_ETA_ENABLED` | `1` | 세션 로드 오버레이의 "예상 약 N초" 안내(202·build_status 응답의 `eta`). `0` 이면 키를 싣지 않고 프런트는 종전 문구 — 추정이 어긋나 혼란을 줄 때의 차단 스위치 ([docs/12](../docs/12_web_report_cache.md)) |
| `WEB_REPORT_EVAL_FAIL_ONLY` | `1` | AI Comment 평가 범위. `1`=fail 이 1chip 이상인 item 만(Yield/Issue Table 과 같은 기준), `0`=전체 item(종전). **item 컬럼만** 줄고 chip 행은 전량 유지돼 분포·CPK 계산은 그대로다. 표본함 수집·골든셋 검사는 이 값과 무관하게 항상 전체. 바꾸면 재기동 필요(ai_comment 세션 캐시 1회 재계산) ([docs/13 §6-2](../docs/13_eval_analyzer_integration.md)) |
| `REPORT_ADMIN_SECRET` | `pte` | admin 경로 조각 → `/pe/admin-<secret>/` (기본 `/pe/admin-pte/`) |

### 로그 / 무인 운영 (wsgi.py, watchdog.ps1)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `LOG_MAX_MB` | `256` | 활성 콘솔 로그(`server/log/server_*.txt`) 크기 상한 — 초과 시 새 파일로 로테이션. `0` 이하 = 비활성 |
| `LOG_KEEP_FILES` / `LOG_KEEP_DAYS` | `30` / `14` | 로그 파일 정리 상한(개수/일수) — 기동·로테이션 시 초과분 삭제. faulthandler·metrics 파일도 `LOG_KEEP_DAYS` 준용 |
| `LOG_MIN_KEEP_HOURS` | `48` | **이 시간 안쪽 `server_*.txt` 는 개수·용량 상한과 무관하게 보존** — 재기동 폭주(기동 1회=파일 1개)가 원인 구간 로그를 밀어내지 못하게 하는 안전장치 |
| `LOG_KEEP_TOTAL_MB` | `4096` | `LOG_MIN_KEEP_HOURS` 밖 구간에 적용되는 총 용량 상한 (넘어선 지점부터 과거를 삭제) |
| `REPORT_METRICS_FILE_KEEP_DAYS` | `14` | flight recorder(`metrics_YYYYMMDD.log`, 분당 1줄 리소스 추이) + `runtime_YYYYMMDD.log` + `publicapi_YYYYMMDD.log` 보존 일수. `0` = 비활성 |
| `PUBLIC_API_METRICS_ENABLED` | `1` | 공개 API(`/pe/api/v1`) 호출 계측 — 관리자 **public API** 탭. `0` = 계측만 끔(API 는 정상 동작) |
| `PUBLIC_API_SLOW_MS` | `1000` | 이 시간을 넘긴 공개 API 호출을 '느린·실패 호출' 목록에 개별 기록. 단순 조회만 노출하므로 사람 요청(`REPORT_SLOW_REQ_MS`)보다 낮게 잡는다 |
| `REPORT_RUNTIME_LOG_INTERVAL_SEC` | `300` | `runtime_*.log` 응답시간 스냅샷(p50/p95/p99 + 느린 경로 top5) 기록 주기. 최소 60 |
| `REPORT_SLOW_REQ_MS` | `10000` | 이 시간을 넘긴 요청을 `runtime_*.log` 에 개별 기록. `0` 이하 = 비활성. ⚠️ **요청이 끝나야** 기록된다 — 안 끝나는 요청은 아래 `REPORT_STUCK_REQ_SEC` 가 맡는다 |
| `REPORT_STUCK_REQ_SEC` | `120` | **아직 안 끝난** 요청이 이 시간을 넘기면 진단 사건(`stuck_request`) + 스레드 덤프(`diagnose_stuck_*.txt`, 기동당 1회). `0` 이하 = 비활성. 위 slow 계측은 teardown 에서만 돌아 hang 을 영영 못 잡으므로(2026-08-19 업로드 hang 이 그랬다) 이 경로가 유일한 기록 지점이다 |
| `REPORT_UPLOAD_SLOW_SEC` | `100` | **업로드 전용** stuck 임계(위 값 대신 적용). 업로드는 동기 구간이 13단계이고 그중 S3 저장·DB 쓰기처럼 밖에서 멎을 수 있는 구간이 섞여 가장 자주 hang 하는데, 정작 사용자는 클라 타임아웃(200초)까지 화면만 보고 기다린다 — 범용 120초보다 먼저 잡아야 조치할 시간이 남는다. 사건에는 **그때 어느 단계였는지**가 함께 실린다(`stage`/`stage_source`) |
| `REPORT_ACTIVE_USER_WINDOW_SEC` | `300` | 관리자 현황 탭 "실시간 접속 사용자" 기본 판정 창 — 이 시간 안에 요청이 있었으면 접속 중. 화면에서 기간 선택 가능(최소 30) |
| `WATCHDOG_BACKOFF_MAX_PER_HOUR` / `_GAP_MIN` | `3` / `30` | (watchdog.ps1) healthz 계열 재기동 백오프 — 최근 1시간 재기동이 임계 이상이면 마지막 재기동 후 지정 분이 지날 때까지 재기동을 건너뛴다 |
| `WATCHDOG_BACKOFF_NL_MAX` / `_NL_GAP_MIN` | `6` / `15` | (watchdog.ps1) `not_listening`(프로세스 사망) 백오프 — 가용성 우선이라 더 관대 |
| `DIAG_PORT` | `PORT+1` | 사이드 진단 리스너([diag_listener.py](diag_listener.py)) 포트. `127.0.0.1` 전용. `0` = 비활성. watchdog 도 같은 규칙으로 포트를 찾는다 |
| `REPORT_DIAG_DIR` | (없음) | 진단 사건·빌드 로그를 쓸 폴더. **운영에선 지정하지 않는다**(기본 `server/log`) — 테스트가 운영 로그를 오염시키지 않도록 격리하는 용도 |

**`server/log/` 파일 종류** (모두 위 정리 정책으로 자동 회수):

| 파일 | 생성 주체 | 내용 |
|------|-----------|------|
| `server_<stamp>.txt` | wsgi(부모) | 콘솔 로그 tee. 기동마다 새 파일 = **파일 수가 재기동 횟수**. 이제 `logging` INFO 도 타임스탬프 포함해 여기 남는다 |
| `faulthandler_<stamp>.txt` | wsgi(부모) | 네이티브 크래시(세그폴트/OS 강제종료) 스택. server_ 와 stamp 공유. **크래시 없으면 0바이트→다음 기동 시 삭제** |
| `faulthandler_worker_<pid>.txt` | 컴퓨트 워커 | 워커 프로세스 네이티브 크래시(OOM 등) 스택. per-PID |
| `metrics_YYYYMMDD.log` | metrics 샘플러 | flight recorder — `ts,cpu,rss,mem_used,inflight,win_peak,workers_rss` (분당 1줄). 크래시 직전 리소스 추이 부검용. 7번째(컴퓨트 워커 RSS 합)는 2026-07-28 추가 — **뒤에 붙여** 6컬럼 구파일도 그대로 파싱된다 |
| `runtime_YYYYMMDD.log` | metrics 샘플러 | 응답시간 스냅샷(`type:lat`, 5분마다) + 느린 요청 개별 기록(`type:slow`, JSON lines). **재시작으로 초기화되지 않는 부하 이력** — admin '이력' 탭이 읽음 |
| `diagnostic_YYYYMMDD.log` | [diagnostics.py](diagnostics.py) | **진단 사건**(JSON lines) — 서버 500/503·느린 요청·콜드 빌드 실패/중단·브라우저·Honey 오류를 상관 ID(request/operation/build/session)로 이어 모은 단일 저장소. admin '진단 사건' 탭이 읽음 |
| `diagnostic_detail_<event_id>.txt` | diagnostics.py | 사용자가 오류 창에서 직접 보낸 상세(정제 traceback + 실행 로그 꼬리). JSONL 한 줄이 커지면 조회가 통째로 느려져 따로 뺀다 |
| `diagnose_stuck_<stamp>.txt` | metrics 샘플러 | `REPORT_STUCK_REQ_SEC` 를 넘겨 **아직 처리 중인** 요청의 전 스레드 스택. 기동당 1회 |
| `diagnose_terminate_<stamp>.txt` | [drain_wait.ps1](drain_wait.ps1) | 종료 직전 스레드 덤프 — drain 이 정체/제한시간/무응답으로 끝났을 때만. **서버를 내리면 현행범 스택이 사라지므로** 그 전에 받아 둔다 |
| `diagnostic_ack.json` | diagnostics.py | 사건 확인 처리 상태(DB 스키마 무변경 원칙) |
| `compute_worker_YYYYMMDD.log` | 컴퓨트 워커 | 워커 프로세스의 logging — 워커 stdout 은 부모 tee 로 안 흘러 지금까지 증발했다 |
| `build_state_<pid>.json` | 컴퓨트 워커 | **실행 중** 콜드 빌드 체크포인트(현재 단계·source 파일). 타임아웃으로 워커가 죽어도 부모가 이걸 읽어 마지막 단계를 실패 레코드에 남긴다. 정상 종료 시 삭제 |
| `watchdog_events.log` | watchdog | 재기동/실패 이벤트(JSON lines) — admin 대시보드 현황 탭이 읽음 |
| `watchdog_checks.log` | watchdog | **매 실행 1줄**(JSON lines) — 실행 빈도 자체. `mutex_busy` = 태스크 겹쳐 뜬 직접 증거 |
| `watchdog_snap_<stamp>.txt` | watchdog | 재기동 직전 프로세스 부검 + **스레드 덤프**(사이드 진단 리스너에서 채집) + 최신 `server_*.txt` 마지막 20줄 스냅샷(죽은 이유 원문) |
| `diagnose_<stamp>.txt` | diagnose_watchdog.ps1 | 진단 스크립트 리포트(수동 실행 시) |
| `diagnose_port_<stamp>.txt` | diagnose_port.bat | 포트 점유 진단 리포트 — 바인딩 주소·포트 주인·TCP 접속 시험(수동 실행 시) |

**watchdog 자동 재기동**: [register_watchdog.bat](register_watchdog.bat) 을 관리자 권한으로
1회 실행하면 작업 스케줄러에 5분 주기 + 부팅 시 감시([watchdog.ps1](watchdog.ps1))가
등록된다 — 포트 미리스닝이면 즉시, `/healthz` 무응답이면 2연속 실패 시 자동 재기동.
healthz 점검 주소는 **실제 LISTEN 주소를 따라간다**(`Get-ProbeHost`) — `0.0.0.0`/`127.0.0.1`
이면 loopback, 특정 IP 에만 bind 돼 있으면 그 IP. loopback 고정이던 시절 `HOST` 가 운영 IP
하나로 바뀌자 점검이 100% 실패해 재기동이 종일 반복된 사고(2026-07-29)를 막는 장치다.
점검에 쓴 주소는 checks 레코드 `addr` 에 남는다.
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
20건(사유·코드·소요·오류·연속실패·부검 스냅샷 링크)을 본다. 스냅샷 링크를 누르면
**console log 탭**이 그 파일을 연다 — 이 탭은 `server_*.txt` 외에 `watchdog_*` · `metrics_*` ·
`runtime_*` · `faulthandler_*` · `diagnose_*` 도 선택해 볼 수 있다(그 외 파일은 열람 거부).

> 타일의 `원인(24h)` 은 **재기동한** 이벤트의 사유라 `healthz_fail_x2`/`not_listening` 만 나온다.
> 세분 사유(`healthz_timeout`/`healthz_connect`/`healthz_503`)는 그 아래 `실패 감지(24h)` 줄에 있다.
> `최근:` 줄은 24h 대표값이 아니라 **마지막 이벤트 1건**이다.

**healthz 실패 원인 판정표** — Watchdog 상세의 `code`/`ms`/`오류(wstat)` 조합으로 가른다:

| 관측 (checks) | 보강 증거 | 판정 |
|---|---|---|
| `code=0, ms≈30000, wstat=Timeout` | inflight ≥ `WAITRESS_THREADS`, 스냅샷 스레드 덤프에서 다수 스레드가 같은 지점 대기 | **스레드 고갈** — 덤프의 공통 대기 지점이 근본 원인 |
| `code=0, ms≈30000, wstat=Timeout` | inflight 낮음, cpu≈100%, `runtime_*.log` 에 slow 다수 | **CPU 포화 / GIL 경합** |
| `code=503, ms<6000` | server 로그에 healthz db check 실패, DB 잠금 카운터 증가 | **DB 잠금** (report.db busy_timeout 5s 초과 — 백업 체크포인트 등) |
| `wstat=ConnectFailure` (사유 `healthz_connect`), **ms≈2000 고정** | LISTEN 은 있는데 `LocalAddress` 가 특정 IP (127.0.0.1/0.0.0.0 아님) | **바인딩 주소 문제** — 사용자는 정상인데 점검만 실패해 재기동이 무한 반복. `HOST=0.0.0.0` 으로 되돌릴 것 (2026-07-29 실제 사고) |
| `wstat=ConnectFailure`, 부검 `procs=0` | `server_*.txt` 신규 다수 + `faulthandler_*` 존재 | **크래시 루프** (리스닝 확인~healthz 사이에 프로세스 사망) |

> ⚠️ **ms 로 거부/무응답을 가르지 말 것.** Windows 에서는 **접속 거부도 약 2초**가 걸린다(실측
> 2033ms). `ms≈2000` + `ConnectFailure` 는 "느린 것"이 아니라 **"그 주소:포트에 아무도 없다"**
> 는 뜻이다. 거부와 무응답의 구분은 소켓 오류 코드(`ConnectionRefused` vs `TimedOut`)로 한다
> — [diagnose_port.ps1](diagnose_port.ps1) `[10]` 항목이 이걸 재본다.

**포트 진단** ([diagnose_port.bat](diagnose_port.bat) 더블클릭, 읽기 전용): `healthz_connect` 가
보이면 이걸 먼저 돌린다. 바인딩 주소·포트 주인 PID·진단 포트 대조·TCP 접속 시험(주소별)·
watchdog 최근 기록을 한 파일(`log/diagnose_port_<stamp>.txt`)로 모은다. `[4] 판정` 이
바인딩 주소 문제와 포트 가로채기를 자동으로 짚어준다.
| `not_listening` 반복 + 부검 `procs=N` | 프로세스는 살아있는데 리스너 소켓만 소실 | 포트/소켓 이상 |

판정 순서: ① `diagnose_watchdog.ps1 -Hours 48` 로 태스크 중복·집계 착시를 먼저 배제
② Watchdog 상세의 code/ms/사유/오류 ③ 실패 시각대 `metrics_*.log` 의 inflight·cpu
④ `runtime_*.log` 의 slow 요청 라우트 ⑤ `watchdog_snap_*.txt` 의 스레드 덤프.

**사이드 진단 리스너** ([diag_listener.py](diag_listener.py)): 기존 `/pe/report/_threads` 는
waitress 스레드 풀을 공유해 **정작 스레드 고갈 상황에선 같이 굶는다**. 그래서 별도 소켓·별도
스레드로 도는 최소 HTTP 리스너를 둔다 — `127.0.0.1` 전용(외부 노출 안 됨), 포트는 `DIAG_PORT`
(미설정 시 `PORT+1` = 운영 8081, `0` 이면 비활성). `GET /alive`(생존·inflight) ·
`GET /threads`(전 스레드 스택). watchdog 이 **재기동 직전**(kill 이전) 여기서 덤프를 받아
`watchdog_snap_*.txt` 에 남기므로, 고갈 현행범 스택이 재기동으로 사라지지 않는다.
포트 충돌 등으로 기동 실패해도 서버 본체에는 영향이 없다(로그 1줄 후 비활성).

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

### 랜딩 (`/pe`) — 서버 첫 화면 ([landing/](landing/__init__.py))

| 메서드 | 경로 | 접근 | 설명 |
|--------|------|------|------|
| `GET` | `/pe` · `/pe/` | 공개 | 랜딩 페이지 (HTML). 제품군 바로가기(`/pe/report/?pt=<PT>`) + Honey 다운로드 + 현황 수치. `/` 는 여기로 리다이렉트하고 Honey 클라 첫 화면도 여기다 (🏠 홈 버튼은 계속 `/pe/report/`) |
| `GET` | `/pe/report/api/landing` | 공개 | 랜딩이 쓰는 유일한 조회 — `viewer`(요청마다) + `sessions`/`recent`/`usage`/`active`(30초 전역 캐시). 세션 수는 **비공개 포함 전체**라 누가 봐도 같은 값. `recent` 은 최근 7일 `created`(신규)/`updated`(그 이전 생성분 중 `report_webreport_edit` 이 찍힌 것)로 서로 겹치지 않는다. 계정ID·IP·session_id 는 싣지 않는다(`active` 는 `count`/`window_sec` 만) |

랜딩 자체(`landing_bp`)에는 CSRF `after_request` 가 없다 — 화면이 진입 직후 부르는
`/pe/report/api/landing`(report_bp) 응답이 `report_csrf` 쿠키를 심어 로그아웃 POST 가 통한다.

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
| `GET` | `/api/family_products` | 공개 | product_type → family_product 허용 목록. `?product_type=` 주면 `{families:[…]}`, 없으면 `{taxonomy:{…}}`. 세션 메타 편집 폼의 Family 선택지 — 정본은 eval 엔진 `product_taxonomy.yaml`(사본을 두면 eval ingest 가 거부하는 조합을 화면이 권하게 된다). 룰 파일을 못 읽으면 빈 목록(500 아님) |
| `POST` | `/api/client_error` | 공개 | 브라우저 JS 에러 beacon 수신 (error_beacon.js — CSRF 미적용, per-IP 스로틀, 감사 action=`client_error` + 진단 사건 component=`browser`). fetch 5xx·콜드 빌드 폴링 타임아웃처럼 **이미 catch 된** 실패도 boot.js 가 여기로 명시 전송한다 |
| `POST` | `/api/client_diagnostic` | 공개 | **Honey 앱** 오류 수신 (client/transport/error_report.py). client_error 와 같은 규약(항상 204·CSRF 미적용·IP 스로틀)이되 본문 상한 640KB(`mode=detail` = 사용자가 오류 창에서 직접 보낸 상세). **event_id 는 클라 값을 유지**한다 — 서버가 죽어 있던 동안 로컬 큐에 쌓였다 재전송되므로 서버가 새로 발급하면 한 사고가 여러 건이 된다 |
| `POST` | `/api/eval/labels_import` | Honey | **선례 CSV 검증/적재** (Honey 'DB Input'). multipart `file` + `mode=validate\|commit`. **`X-Honey-Agent: 1` 필수**(CSRF 대체), Honey 신원 있으면 누구나, ≤5MB, 단순 5컬럼만. CSV **내용** 오류는 4xx 가 아니라 `200 {"ok":false,"errors":[…]}`. `db_input/import_csv.py` 를 subprocess 로 실행(→ [docs/13 §10](../docs/13_eval_analyzer_integration.md)). 관리자 `GET /api/eval/labels.csv` 의 반대 방향. 감사 action=`eval_db_input` |
| `GET` | `/result/<sid>` | 공개 | 세션 요약 JSON |
| `GET` | `/session/<sid>` | 공개 | 세션 메타 JSON (password 제거, has_password 만) |
| `GET` | `/session/<sid>/full` | 공개 | 세션 전체 데이터 JSON (summary+objects+주석+추출텍스트). web_report 세션이 **콜드**면 빌드를 백그라운드에 걸고 `202 {"building":true,"stage","elapsed"}` 즉시 반환 — 프런트가 재시도 (warm 은 종전대로 200) |
| `GET` | `/session/<sid>/my_access` | Honey | 현재 사용자의 이 세션 권한 |
| `DELETE` | `/session/<sid>` | 업로더 | 세션 삭제 |
| `POST` | `/session/<sid>/important` | Honey | 개인 중요표시 토글 |
| `POST` | `/session/<sid>/private` | 업로더 | 비공개 토글 |
| `PATCH` | `/session/<sid>/meta` | 편집자 + Honey(또는 master) | 세션 메타 수정 — `{file_name, family_product, product, lot_id, process, step}`. **`X-Honey-Agent: 1` 필수**(= Honey 앱 전용 강제, CSRF 대체) — **예외: master(admin 로그인 4h)는 웹 브라우저에서도 수정 가능하며, 헤더가 없는 만큼 CSRF 를 요구한다**(둘 중 하나는 반드시 통과). product 변경 시 product_info.db 재lookup(미등록이면 기준정보 14컬럼 비움). product_type·analysis_key 는 불변 |
| `GET` | `/honey/session_meta/<sid>` | 공개 | 위 편집창의 **진입 URL** — Honey 내장 브라우저가 네비게이션을 가로채 편집창을 띄우므로 실제로는 요청되지 않는다. 가드 없는 환경용 안내 HTML |
| `POST` | `/session/<sid>/verify_password` | Honey | **하위호환 스텁** — UA 업로더 확인만, 항상 `has_password:false`. 세션 PIN 자체가 2026-08-14 폐지(평문 저장 중단·기존 값 NULL, 관리자 `POST /api/session/<sid>/password` 는 410) |
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
| `POST` | `/issue_table/status` | 편집자 | Issue 행 Status Open/Close (kind=issue_status, Close 만 저장). 단건 `{key,value}` / 일괄 `{items:[{key,value},…]}` (전체·선택 Open/Close, DB write 1회) |
| `POST` | `/issue_table/signature` | 편집자 | Issue 행의 **ENGR 확정 Signature** 저장 (kind=issue_signature, `{key, signatures:[id,…]}`, 빈 배열=해제). 카탈로그 id 또는 `UNKNOWN` 만·중복 불가·최대 8개. 저장 후 eval DB 로 비동기 동기화 ([docs/13 §6-3](../docs/13_eval_analyzer_integration.md)) |
| `POST` | `/chart_notes` | 편집자 | 차트 주석(도형/텍스트/코멘트) 저장 (kind=chart_note) |
| `POST` | `/compare_notes` | 편집자 | **Compare 탭 행 코멘트** 저장 (kind=compare_note, `{ops:[{key,value}]}`, 빈 값/null=삭제). key 는 `gl:<after>U+001F<before>`(Log 비교 행) 또는 `bm:<x>,<y>`(동일 좌표 Bin 비교 행) — **고정 규약**(키가 바뀌면 기존 입력이 유실된다, CLAUDE.md §5-12). 응답에 권위본 `compare_notes` 동봉 |
| `GET`/`POST` | `/note` | 공개/편집자 | Note 탭 시트 JSON 지연 조회 / 저장 (kind=note_sheet, ≤10MB). 본문은 **객체 저장**(report_session_blob 포인터), 전환 기간에는 legacy 편집행에도 dual-write — 응답 형식·낙관적 잠금 `base` 는 불변 |
| `GET` | `/note/sheet_names` | 공개 | Note 시트 **이름만** `[{index,name,order}]`. Summary 의 `$[시트명]` 자동완성·시트 버튼 줄 전용 — 본문(≤10MB)까지 내려주는 `/note` 를 이름 때문에 부르지 않게 한 경량 라우트 (서버는 updated_at 키로 memo) |
| `POST` | `/note_image` | 편집자 | Note 이미지 업로드 (PNG/JPEG raw body, ≤2MB·세션 200장) |
| `GET` | `/rawdata_export` | 공개 | **Honey 전용** Excel 편집용 zip(manifest + source_*.parquet, ZIP_STORED) 내보내기 — 웹 다운로드용 `/rawdata_csv_all` 과는 내용·파일명이 다른 별개 경로다. **ETag = content_hash** — Honey 가 temp 에 받아둔 zip 을 `If-None-Match` 로 물어보면 내용 무변경 시 **304**(서버가 원본을 메모리에 올려 zip 으로 싸는 작업 자체를 안 함) |
| `GET` | `/rawdata_csv?source=<idx>` | 공개 | **웹 브라우저용 Rawdata 다운로드** — source 1개를 7-meta honeyform CSV(UTF-8 BOM, 메타 6행 TSEQ~LOLIM 포함)로 내보낸다. Honey·추가 exe 없이 세션 상세 상단 ⬇ 버튼에서 받는다. 저장된 parquet 문자 그대로이며 전처리·편집 상태는 미반영. **ETag = `<content_hash>:src<idx>`**. 범위 밖 idx 404 / 정수 아님 400. 서버는 openpyxl 을 쓰지 않으므로 xlsx 가 아니라 CSV 다(불변 규칙 #1) |
| `GET` | `/rawdata_csv_all` | 공개 | **웹 브라우저용 Rawdata 전체 일괄 다운로드** — 전 source 를 위와 같은 CSV 로 만들어 zip 하나(ZIP_DEFLATED level 1)로 **스트리밍**한다. ⬇ 버튼 메뉴의 '전체' 항목. 가드·내용 정책은 `/rawdata_csv` 와 동일. zip 내부 파일명은 단일 다운로드와 같은 `rawdata_<lot>_<source>.csv`(같은 이름이 겹치면 `_2`, `_3` — zipfile 이 중복을 경고만 하고 써서 푸는 쪽이 조용히 덮어쓰는 것을 막는다). **ETag = `<content_hash>:all`**. 스트리밍이라 `Content-Length` 없음. 응답을 흘리기 시작한 뒤 실패하면 중앙 디렉토리를 쓰지 않고 끊는다 — 받는 쪽이 손상 zip 으로 인지하게 하려는 것으로, 정상처럼 열리는 부분 zip 은 만들지 않는다. source 0개 404. 규칙 #1 때문에 xlsx 다중 시트가 아니라 zip+CSV 다 |
| `POST` | `/rawdata_replace` | 편집자 | Raw Data 소스 전체 교체 (Honey 전용, `X-Honey-Agent`). Excel 시트를 지워 source 가 줄면 form 필드 `source_indices`(남긴 원본 idx JSON 배열, 오름차순)를 함께 받아 그 source 를 물리 제거. 선택 첨부 `dist_pack_index`+`dist_pack_chunk_<n>`(클라가 새 parquet 으로 만든 Distribution pack — 업로드 라우트와 같은 규약)을 받으면 새 content_hash 로 영구 저장해 반영 후 콜드 dist 정렬을 없앤다. 반영 후 프리웜을 걸어 리빌드를 컴퓨트 워커로 넘긴다 |
| `GET`/`POST` | `/preprocess` | 공개/편집자 | **조회 전처리** 옵션(항목 제외 / outlier `mean ± k·stdev` / 셀 패치 / 조건 규칙) 조회·저장 (kind=preprocess). 원본 parquet 은 그대로 두고 조회 시점에만 적용 — 빈 spec 저장 = 해제, 되돌리기 가능. 같은 body 의 `yield_basis`(수율 분모 기준, 별도 kind)도 함께 저장한다. Honey 는 `X-Honey-Agent` 로 CSRF 대체 |
| `GET` | `/yield_basis` | 공개 | 소스별 **수율 분모 기준**(자동 판정 결과 + 사용자 선택)과 그 판정에 쓰인 수치(pass/tested/gross die). Honey 허브 [Yield 계산] 탭이 이 값으로 기준을 바꿔 가며 수율을 **왕복 없이** 다시 계산한다. 저장은 `/preprocess` POST 의 `yield_basis` 필드 |

### 주석 / 즐겨찾기 / 인증 (`/pe/report/`)

| 메서드 | 경로 | 접근 | 설명 |
|--------|------|------|------|
| `POST`/`GET`/`PATCH`/`DELETE` | `/annotation`, `/annotation/<sid>`, `/annotation/<aid>` | 공개* | 주석 CRUD (*변경은 CSRF) |
| `GET`/`POST` | `/api/favorites` | Honey | 개인 즐겨찾기 |
| `POST` | `/api/auth/login` | 공개 | 웹 로그인 (singleID + 비밀번호 4자리). 5회/5분 실패 시 429 |
| `POST` | `/api/auth/signup` | 공개 | **웹 회원가입** — Honey 사용 이력(업로드·web_report 방문)이 **없는 미사용 singleID** 만 자유 가입(403/409로 차단), 가입 즉시 로그인. IP 당 5회/1시간. body 의 `name`(실명)은 폼에서만 필수 — 서버는 **없어도 가입시킨다**(브라우저에 캐시된 옛 JS 대비, 이름은 첫 화면 입력창이 채운다) |
| `POST` | `/api/auth/display_name` | 신원 | **사용자 실명 등록/변경** (1~30자). 신원(Honey UA·웹 로그인·SSO)만 가능하고 익명은 403. 로그인 계정이 없는 Honey 전용 사용자도 저장된다(저장소 `report_user_profile` 은 `report_user` 와 별개). 이름은 **표시 전용** — 접근제어·감사 식별은 계속 `user_id` |
| `GET` | `/api/auth/signup_hint` | 공개 | 회원가입 창 ID 자동완성 힌트 — **요청자 자신의 IP** 로 최근 180일 Honey 업로드 계정 1건 (`{user_id, honey_seen}` / 없으면 `{}`). 신원 판단에는 미사용 |
| `POST` | `/api/auth/set_password` | Honey | 웹 로그인 비밀번호 설정/변경 (Honey 접속 전용 — 본인확인) |
| `POST` | `/api/auth/change_password` | — | **410 Gone** (set_password 로 대체) |
| `POST`/`GET` | `/api/auth/logout`, `/api/auth/me` | 공개 | 로그아웃 / 현재 신원·출처 확인 (`display_name` 동봉 — 비면 프런트가 이름 입력창을 띄운다) |
| `GET` | `/api/my_messages` | 공개 | **관리자 팝업 메시지** 수신 — 아직 확인하지 않은 것만. 두 페이지(검색결과·세션 상세)의 `admin_message.js` 가 30초 폴링(화면이 보일 때만). 수신자 키는 사용량 집계와 같은 규칙(신원 있으면 소문자 계정, 없으면 `ip:<addr>`)이라 **신원 없는 브라우저는 전체 공지만** 받는다. 저장소는 서버 프로세스 메모리(`admin_panel/messages.py`) — 재시작 시 미확인분 소멸 |
| `POST` | `/api/my_messages/<id>/ack` | 공개* | 확인 버튼 → 읽음 기록(그 사람에겐 다시 안 뜸). *CSRF. 없는 id 도 200(멱등 — 재시도가 실패로 보이지 않게) |
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
| `GET` | `/honey/version` | 버전 정보 JSON (`version.json` 반환). 호출을 'Honey 실행'으로 사용자별 집계 (`report_usage_daily`, 신원은 HoneyUser UA — 구버전 클라는 IP). **`?probe=1` 이면 집계를 건너뛴다** — 웹 페이지가 다운로드 버튼의 링크·파일명을 보정하려고 부르는 경우(실행이 아니다). `/pe` 랜딩이 이걸 쓰고, 응답 내용은 완전히 동일하다. UA 에 `HoneyVer/<버전>` 토큰이 있으면 **버전 대장**(`report_client_version`)도 함께 갱신한다 — 클라 버전이 바뀌는 시점이 곧 앱 시작이라 서버가 버전을 기록하는 지점은 여기 하나뿐이다 |
| `GET` | `/honey/download` | Honey exe/ZIP 다운로드 |
| `GET` | `/honey/announcement` | 릴리스 공지 원문 (`releases/announcement.txt` 그대로, text/plain). 클라가 최신 버전 실행 중일 때 PC 계정별 1회 팝업 → [docs/04](../docs/04_honey_update.md) |
| `GET` | `/honey/client_notice` | **구버전 클라 사용자에게 웹에서 띄울 안내문 + 기준 버전** (JSON: `min_version`/`version`/`file`/`title`/`body`). 구버전 exe 는 고칠 수 없으므로 그 사람이 내장 브라우저로 서버 페이지를 열 때 안내한다 — 판정·표시는 `static/webreport/old_client_notice.js`(랜딩·검색결과 공유, 하루 1회). 본문은 `releases/old_client_notice.txt`(첫 줄=제목), 기준은 `version.json` 의 `min_version` 이며 **비어 있으면 기능이 꺼진다**. 집계하지 않는다 |

### 관리 대시보드 (`/pe/admin-<secret>/`, 기본 `/pe/admin-pte/`) — 인증 없음, 내부망 전용

비-GET 요청은 `X-Admin-Request: 1` 헤더 요구. `GET /` 대시보드 + `GET /api/*`
(health/storage/s3-status/metrics/stats(daily·users·client_errors·usage(접속 사용량 —
Honey 실행·웹 방문 순위)·usage_trend·usage_hourly)/sessions/users/
voc(overview·목록, 읽기 전용)/audit(.csv)/logs/list·tail/webreport/builds) +
`POST /api/*` (sessions/delete·restore·purge, session/<sid>/important·password, db/backup·cleanup,
webreport/build_action 등).
**진행 중 콜드 빌드**(현황 탭): `GET /api/runtime` 의 `builds` 는 (세션, stage, 경과) 3칸이
아니라 세션 메타(제품·LOT·파일명·업로더)·**대기 중인 사용자**·**워커의 현재 단계/source**
(build_log sidecar)·예상 대비 초과 배수까지 붙여 준다 — 조립은
[admin_panel/builds_admin.py](admin_panel/builds_admin.py), 빌드가 0건이면 DB·파일 접근 없음.
같은 응답의 `build_queues` 는 큐 대기/실행 중 목록 + 재빌드 차단 세션이다.
개입은 `POST /api/webreport/build_action {action, session_id, kind}` — `clear_failure`(연속 실패
쿨다운 해제) / `clear_stuck`(등록 잔재 정리 — 워커 타임아웃 초과 건만, 그 미만은 400) /
`rebuild`(빌드 큐 투입). 셋 다 관측 상태만 건드리고 캐시·편집·산출물은 손대지 않으며 감사
기록(`build_action`)이 남는다. **개별 빌드만 취소하는 수단은 없다** — ProcessPoolExecutor 는
실행 중 잡을 cancel 할 수 없고 워커 1개만 죽여도 풀 전체가 broken 이 된다(compute.run).
**사용자 팝업 메시지**(사용자 탭 `📢 메시지 보내기`): `GET /api/messages`(보낸 목록 + 확인 인원) ·
`POST /api/messages`(발송 — `targets` 가 비면 전체 공지, 있으면 그 계정만. 접속자 표의 `보내기`
버튼이 대상을 채운다) · `POST /api/messages/<id>/revoke`(회수 — 아직 안 본 사람에게 중단) ·
`POST /api/messages/<id>/delete`. 저장소는 **DB 가 아니라 프로세스 메모리**
(`admin_panel/messages.py` — 단일 프로세스 전제, metrics 의 in-flight 카운터와 같은 방식)라
서버 재시작 시 미확인 메시지가 사라진다. 보관 상한 200건 / 7일.
**접속 추이 그래프**(사용자 탭 `📈 접속 추이`): `GET /api/stats/usage_trend?days=`(일별 고유
사용자·신규/재방문·접속 횟수·주간 WAU(7일 롤링)·누적 고유 사용자·일별 Peak 동시 접속자) ·
`GET /api/stats/usage_hourly?days=`(요일×시간 히트맵). 소스는 `report_usage_daily` /
`report_usage_hourly` / `report_usage_peak_daily`. 사람 수는 순위표와 **같은 기준**으로
identity_merge 병합 후 센다 — 한쪽만 병합하면 같은 사람이 둘로 세어져 표와 그래프가 어긋난다.
**Peak 동시 접속자·시간대 데이터는 2026-08-13 배포분부터 쌓인다**(그 이전 기간은 빈 것이 정상 —
`peak_users`는 수집 이전 날짜에서 `0` 이 아니라 `null`).
**Eval DB 탭·`/api/eval/*` 라우트는 2026-08-03 `/pe/eval` 로 이관**했다(아래 절) — 구현 모듈
`admin_panel/eval_admin.py` 는 그대로 남아 eval_panel 이 import 한다.
**세션 삭제 3종 구분**: `sessions/delete` = 관리자 **즉시 영구 삭제**(휴지통을 거치지 않고
행·산출물·캐시 회수) / 사용자 웹 삭제(`DELETE /pe/report/session/<sid>`) = 휴지통(soft) /
`sessions/purge` = 휴지통 세션 영구 정리. purge 는 기본이 `REPORT_TRASH_RETENTION_DAYS`(30일)
경과분만이라 방금 버린 세션은 `not expired` 로 스킵된다. 대상 지정은 3가지(배타):
`all_expired:true`(경과분 전체 — **cleanup 스케줄러가 쓰는 자동 경로**) /
`all_trashed:true`(휴지통 **전체**, 미경과분 포함 — 세션 탭 `🗑 휴지통 비우기` 버튼. dry-run 이
`scanned_expired`·`scanned_recent` 로 쪼개 보고한다) / `session_ids` + `force:true`(그 세션만
경과일 무시 — 행별 purge 버튼). `all_expired` 와 `force`, `all_expired` 와 `all_trashed` 조합은
의미가 모호해 400. **자동 경로는 계속 `all_expired` 만 쓴다** — 스케줄러가 미경과분을 지우면
사용자의 30일 복구 창이 통째로 사라진다.
**콜드 빌드 이력**: `GET /api/webreport/builds?hours=&limit=` (이력 탭 `콜드 빌드 이력` 카드) —
web_report 콜드 빌드의 단계별 소요 + 대기 3종(큐/풀/IPC) + 실패(타임아웃·워커 붕괴).
기록은 [web_report/build_log.py](../web_report/build_log.py) 가
`server/log/webreport_build_YYYYMMDD.log` 에 JSON line 으로 남긴다 →
[docs/12](../docs/12_web_report_cache.md).
**진단 사건 (오류 추적 단일 진입점)**: `GET /api/diagnostics/events?hours=&severity=&component=&q=&unacked=` /
`GET /api/diagnostics/events/<event_id>` (상관 ID 로 이어진 타임라인 + 콜드 빌드 기록 +
watchdog 병합 + **증거 기반 원인 안내**) / `POST /api/diagnostics/events/<event_id>/ack`.
구현 [admin_panel/diagnostics_admin.py](admin_panel/diagnostics_admin.py), 저장소
[diagnostics.py](diagnostics.py)(`server/log/diagnostic_*.log`). 화면은 `🚨 진단 사건` 탭.
서버 500/503 응답은 본문 `error_id` 와 `X-Request-ID` 헤더에 **같은 값**을 실어 주므로,
사용자가 화면에서 읽어준 번호 하나로 서버 스택·빌드 기록·클라 오류를 한 번에 찾는다.
원인 안내는 근거가 있을 때만 말하고, 없으면 "확인 불가"를 그대로 표시한다.

**실시간 접속 사용자**: `GET /api/active_users?window=` (사용자 탭 10초 폴링 전용 경량 API) —
최근 `window` 초 안에 요청을 보낸 신원 목록. 신원은 `auth_identity.current_user()`
(Honey UA / SSO 헤더 / 웹 로그인), 없으면 `ip:<addr>` 로 묶는다. 관리자 자신·`/healthz`
(watchdog 폴링)·정적 파일은 집계에서 제외. `GET /api/runtime` 응답에도 같은 값이
`active_users` 로 실린다(현황 탭 요약 타일용, `?user_window=`).
각 행에는 **클라 버전**(`ver`, 출처 `ver_src`: `ua`=지금 요청의 UA 토큰 / `db`=버전 대장
폴백 / 빈 값=버전을 안 보내는 구버전)과 **접속 경로**(`agents`: `app`=Honey 앱 요청,
`browser`=내장 브라우저 — 누적이라 둘 다 나올 수 있다), 보고 있는 세션이 콜드 빌드 중이면
`waiting`(stage·경과초, 소스는 `build_status` 메모리 스냅샷이라 DB 접근 없음)이 함께 실린다.
**활동 타임라인**: `GET /api/user_timeline?key=&window=` — 그 사람의 최근 요청
(경로·세션·소요 ms·상태코드). 소스는 사람당 최근 20건 메모리 링버퍼라 재시작 시 비워진다.
**Honey 버전 현황**: `GET /api/client_versions?days=` — 최근 N일 안에 Honey 를 실행한
**전원**(지금 접속 중이 아니어도)의 버전 분포와 사용자별 마지막 실행 버전. 모집단이
`report_usage_daily`(honey_run)라 버전 미보고 사용자도 '미상' 행으로 남는다 — 그게 곧
업데이트 대상이다. 최신 배포 버전(`latest`)은 `releases/version.json` 에서 읽어 동봉한다.
관리자 화면에서 **사용자 관련 화면은 전부 `사용자` 탭에 모여 있다** — 지금 접속 중(실시간) /
누적 사용량(작업 활동·접속 횟수) / 사용자 이름 관리. 통계 탭은 일별 추이와 최근 활동만 담당한다.
**사용자 이름 관리**(`GET /api/users`, `POST /api/user/<uid>/name`)의 목록 대상은 웹 로그인
계정이 아니라 **이 서버에 흔적을 남긴 사람 전체**다 — `report_user` ∪ `report_user_profile`
∪ `report_web_visitor` ∪ `report_usage_daily`(무신원 `ip:` 행 제외)를 uid 로 합친다
([users_admin.py](admin_panel/users_admin.py) `_PEOPLE_CTE`). ID/PW 로그인 폐지 후 계정 행은
더 늘지 않으므로 계정만 보면 실제 Honey 사용자가 표에 없다. 관리자는 여기서 사용자 실명을
직접 지정/변경할 수 있고(오타·개명·미입력 대응, 한글 2~10자 — 사용자 본인 경로는
`POST /pe/report/api/auth/display_name`), 비밀번호·삭제 버튼은 웹 로그인 계정이 있는 행에만
뜬다.

**"IP 가 같으면 같은 사용자" 병합** ([admin_panel/identity_merge.py](admin_panel/identity_merge.py)):
신원 토큰 없는 접속은 `ip:<addr>` 로 잡혀 한 사람이 계정 행 + IP 행으로 갈라져 보인다.
`report_audit_log` 의 (client_user, client_ip) 짝 + 현재 접속자에서 **IP→계정** 매핑을 만들고
(TTL 60초 캐시, 90일 창), IP 로 표시되는 행을 그 계정에 합친다. 적용 범위는 관리자 화면 전체 —
실시간 접속자 / 누적 사용량 2종 / 감사 기록(표시 `resolved_user` + 계정명 검색이 그 IP 의
무신원 기록까지 포함). **한 IP 에 계정이 2개 이상이면 활동이 가장 많은 계정(주 사용자)으로
합친다**(2026-08-12 완화 — 그전에는 병합하지 않아 한 사람의 행이 갈라져 목록이 길어졌다).
공용 PC·NAT 에서는 남의 활동이 주 사용자에게 붙을 수 있으므로 합쳐진 행은 `merged_from`
(화면 `IP 병합` 배지)에 원래 이름을 남긴다. 동률이면 계정명 사전순으로 고정(표가 흔들리지 않게).
`admin-panel`·`system` 은 사람이 아니라 매핑 근거에서 제외한다. 순위표의 LIMIT 은 **병합 후**
적용된다(자르고 합치면 조각이 사라지므로).
`GET /api/eval/labels.csv` = 코멘트 라벨 전체를 db_input 5컬럼 CSV 로 export
(고쳐서 `eval_analyzer\db_input\run_import.bat` 으로 재적재 — [docs/13 §10](../docs/13_eval_analyzer_integration.md)).
**Unit 그룹 교정 2종**: `POST /api/eval/items/value_type`(선택 항목의 value_type 수동 지정) /
`POST /api/eval/items/remap_units`(`dry_run` 미리보기 → 별칭 규칙 VOLT/AMP/HERTZ 일괄 재적용).
둘 다 `item_master.value_type` 과 `fail_case.item_class` 를 함께 고친다 — 선례검색이
value_type 을 등호 하드필터로 쓰기 때문 ([docs/13 §9](../docs/13_eval_analyzer_integration.md)).
운영 진단용 GET 4개: `watchdog`(재기동 이력+reason 분포) · `watchdog/checks?hours=`(매 점검
기록 요약) · `metrics/history?window=`(in-memory 10초 해상도) · `metrics/file_history?hours=`
(**파일 기반, 재시작과 무관한 1분 해상도 이력** — 최대 336시간). `logs/tail?file=` 은
`server/log/` 화이트리스트(server_·watchdog_·metrics_·runtime_·faulthandler_·diagnose_) 밖이면 400.
구 공개 `/pe/admin` (`admin_routes.py`)은 **미등록 dead file**.

### eval 룰 관리 (`/pe/eval`) — admin 비밀번호 게이트 ([eval_panel/](eval_panel/))

eval_analyzer 의 임계값·signature 를 브라우저에서 고치고 **서버 재시작 없이** 반영시킨다.
관리 대시보드와 같은 비밀번호(`REPORT_ADMIN_PASSWORD`)로 별도 쿠키 `pe_admin_gate_eval`
(path=`/pe/eval`, 12h)를 발급하며 admin `/login` 이 함께 발급한다. 비-GET 은 admin 과 같은
`X-Admin-Request: 1` 헤더 요구. 규약·화면 설명은 [docs/13 §11](../docs/13_eval_analyzer_integration.md).

| 메서드 | 경로 (`/pe/eval` 하위) | 설명 |
|--------|------|------|
| `GET` | `/` · `POST /login` | 패널 페이지 / 게이트 쿠키 발급 |
| `GET` | `/api/meta` | product_taxonomy · 임계값 키 31개 · signature id · rules_rev · 파일 sha256 |
| `GET`/`PUT` | `/api/thresholds` | 제품군×family 오버레이 조회(적용값+출처)/저장. 빈 값=상속, 전부 비면 파일 삭제 |
| `GET`/`PUT` | `/api/signatures[/<id>]` | 21종 조회 / 1건 갱신(enable·조건·status_hint·문구). 추가·삭제 불가 |
| `POST` | `/api/reload` | 룰 캐시 강제 클리어 + rev bump |
| `GET` | `/api/validate` | 참조 무결성(임계값 키 존재·고아 오버레이·전 조합 병합·SPECIFICITY_ORDER) |
| `GET`/`POST` | `/api/backups[/restore]` | 저장 직전 백업 목록 / 복원 |
| `GET` | `/api/sessions` | 트레이스 대상 web_report 세션 목록 |
| `POST`/`GET` | `/api/trace[/<token>/case/<i>]` | L0~L6 트레이스 실행(요약) / 케이스 상세 |
| `GET` | `/api/eval/scoring` | 엔진 판정 ↔ 사람 정답 채점(전체 누적) — 혼동행렬·precision/recall |
| `GET` | `/api/golden/auto` | 임계값 저장 직후 **자동 실행된** 골든 회귀의 최신 상태(running/done/empty/skipped/error). 저장은 되돌리지 않고 사후 경고만 한다 — rev 가 이미 올라 리포트 캐시가 무효화됐으므로 롤백이 비싸다. 결과는 프로세스 메모리(재시작 시 소멸), `trace_store` 에는 **넣지 않는다**(LRU 4런이라 관리자가 보던 트레이스를 밀어낸다) |
| `GET` | `/api/eval/trend?days=` | **일별 지표 추이**(`report_eval_daily`) — UNKNOWN 비율·signature 확정 일치율·코멘트 정합률·status 일치율. 비율이 아니라 **카운터**를 돌려주고 화면이 나눈다. 수집 이전 날짜는 행 자체가 없다(‘0’ 과 ‘기록 없음’ 은 다르다) |
| `POST` | `/api/eval/trend/rollup` | 지금 즉시 재집계(스케줄러 24h 를 기다리지 않을 때). 최근 `REPORT_EVAL_ROLLUP_DAYS` 일 **덮어쓰기**라 여러 번 눌러도 값이 부풀지 않는다 |

저장은 검증 → `rules/_backup/` 백업 → 원자적 쓰기 → `rules/.rules_rev` +1 → 감사
(`action=eval_rules_edit`, `client_user=eval-panel`) 순. rev 는 ai_comment 옵션 세션의
report 캐시 키에 실려 다음 조회에서 재평가를 강제한다.
**임계값이 실제로 바뀌면(no_op 아님) 골든 회귀가 백그라운드로 자동 실행된다**(2026-08-19) —
저장 응답을 붙잡지 않는다(회귀는 골든 세션마다 트레이스 1회라 초~분). 화면은
`/api/golden/auto` 를 3초 간격으로 폴링해 "통과 / N건 어긋남"을 임계값 탭에 띄운다.
다른 트레이스가 CPU 를 쓰고 있으면 겹쳐 돌리지 않고 `skipped` 로 안내한다(수동 실행 권유).

### 공개 REST API (`/pe/api/v1`) — 무인증·읽기 전용, 사내망 타 서버용 ([public_api/](public_api/README.md))

| 메서드 | 경로 (`/pe/api/v1` 하위) | 설명 |
|--------|--------------------------|------|
| GET | `/product-info/candidates` | 기준정보 part_id 검색 후보(part_id+sub_part_id flatten). DB 부재 시 빈 목록 200 |
| GET | `/product-info/lookup?part_id=` | part_id → 기준정보 14컬럼. 미매칭 404 |
| GET | `/help/features` | HONEY 사용자 기능 전체 또는 q/category/surface/status 필터 검색 |
| GET | `/help/features/<id>` | 기능 한 건의 제공 상태·사용 조건·절차·도움말 anchor. 미매칭 404 |

부하가 작은 조회만 노출한다(단순 SELECT / 메모리 dict) — 파싱·재계산 경로는 넣지 않는다.
외부 소비자용 접근 규약(Base URL·에러 형식·버저닝)은 [public_api/README.md](public_api/README.md).

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
├── diagnostics.py            진단 사건 저장소 (JSONL) + 요청 상관 ID(request/operation) 훅
├── upload_xlsx.py            POST /upload_xlsx 핸들러
├── upload_webreport.py       POST /upload_webreport 핸들러 (web_report.ingest 호출)
├── xlsx_parser.py            시트 grid → 텍스트 추출 (_GridSheet 셸, openpyxl 미사용)
├── honey_routes.py           /honey/version, /honey/download, /honey/announcement, /honey/client_notice
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
│   ├── diagnostics_admin.py  진단 사건 조회·타임라인·원인 안내 ('🚨 진단 사건' 탭)
│   ├── maintenance.py / metrics.py / admin_panel.html
├── eval_panel/               /pe/eval eval 룰 관리 (저장 즉시 반영)
│   ├── __init__.py           register_eval_panel()
│   ├── routes.py             blueprint (게이트 + thresholds/signatures/trace API)
│   ├── rules_io.py           룰 yaml 검증·백업·원자적 저장 (엔진 import 없음)
│   ├── trace_store.py        트레이스 결과 LRU (4런/30분)
│   └── eval_panel.html / eval_login.html
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

# 20. 오류 추적 / 콜드 빌드 진단

> 2026-08-11 도입. 관련 코드: [server/diagnostics.py](../server/diagnostics.py) ·
> [server/ops.py](../server/ops.py) · [web_report/build_log.py](../web_report/build_log.py) ·
> [web_report/compute.py](../web_report/compute.py) ·
> [server/admin_panel/diagnostics_admin.py](../server/admin_panel/diagnostics_admin.py) ·
> [client/transport/error_report.py](../client/transport/error_report.py)

## 0. 왜 만들었나 (해결한 구멍 5개)

| 증상 | 종전 상태 |
|---|---|
| 콜드 빌드가 300초 걸려 죽었다 | 실패 레코드에 `session/300초/TimeoutError` 뿐 — **어느 단계·어느 파일**인지 알 수 없음. 워커를 terminate 하면 자식이 잰 단계 기록이 IPC 로 돌아오지 못한다 |
| 사용자가 "500 떴어요" 라고 신고 | 응답에 상관 키가 없어 `server_*.txt` 의 어느 traceback 인지 특정 불가 |
| Honey 에서 업로드가 timeout 났다 | **서버에 흔적 0** — 연결 자체가 안 닿으면 서버 로그에 아무것도 안 남는다 |
| web_report 업로드가 400/503 으로 거절됐다 | 서버 로그·감사 모두 무기록 (xlsx 흐름은 `report_session.error_message` 에 남기는데 비대칭이었다) |
| 워커 안에서 난 예외 | `wsgi` 의 `__mp_main__` 가드가 워커에서 tee 설치를 건너뛰어 stderr 로 증발 |

## 1. 상관 ID — 이 설계의 전부

사건들은 **ID 하나만 알면 나머지가 전부 딸려오도록** 이어져 있다.

| ID | 발급 | 붙는 곳 |
|---|---|---|
| `request_id` | `diagnostics.init_app` 의 `before_request` (8자 hex) | 모든 응답의 `X-Request-ID` 헤더 + 500/503 본문의 `error_id` + 콘솔 로그 `[rid=…]` |
| `operation_id` | Honey 가 작업 시작 시 (`error_report.begin_operation`) | 업로드 요청 헤더 `X-Honey-Operation-ID` + Honey 오류 보고 |
| `build_id` | 워커가 잡 시작 시 (`build_log.begin_job`) | sidecar + 빌드 실패 레코드 + 빌드 사건 |
| `event_id` | 사건 자신. **Honey 는 클라가 만든 값을 서버가 유지한다** | 오류 창에 표시 · 오프라인 재전송 중복 제거 키 |
| `session_id` | 기존 | 사건·빌드 레코드 공통 |

`diagnostics.related(event_id)` 는 기준 사건이 가진 ID 중 **하나라도** 같은 사건을 전부
모은다. 하나씩 따라가는 방식은 요청→빌드→오류가 서로 다른 ID 로만 이어져 있어 중간에서
끊긴다.

**사용자 대면 규약**: 500/503 화면과 Honey 오류 창은 `오류번호`를 보여준다. 관리자는 그
번호를 진단 사건 탭 검색창에 넣으면 서버 스택·빌드 기록·클라 오류가 한 화면에 뜬다.

## 2. 저장소 — DB 를 쓰지 않는 이유

사건은 `server/log/diagnostic_YYYYMMDD.log` 에 **JSON 1줄 = 1건**으로 append 한다
(build_log 와 같은 open-append-close). 새 테이블을 만들지 않은 것은 취향이 아니라 판단이다:
**에러가 나는 순간은 SQLite 가 잠기거나 디스크가 눌린 순간이기 쉽고, 기록하려다 또 터지면
기록 자체가 사라진다.** 감사 로그(`report_audit_log`)는 종전 역할 그대로 유지한다 —
"누가 무엇을 했나"의 이력이고, 오류가 섞이면 그 이력이 밀려난다.

파일 4종:

| 파일 | 내용 |
|---|---|
| `diagnostic_YYYYMMDD.log` | 사건 본문 (JSONL) |
| `diagnostic_detail_<event_id>.txt` | 사용자가 직접 보낸 상세(정제 traceback + 실행 로그 꼬리). JSONL 한 줄이 커지면 `history()` 가 통째로 느려져 분리 |
| `diagnostic_ack.json` | 확인 처리 상태 |
| `build_state_<pid>.json` | **실행 중** 콜드 빌드 체크포인트 (§3) |

보관은 `LOG_KEEP_DAYS`(14일). 파일 쓰기가 실패하면 메모리 링버퍼(300건)로 폴백한다.
`REPORT_DIAG_DIR` 로 폴더를 덮어쓸 수 있다 — **테스트 전용**이며 운영에서는 지정하지 않는다.

## 3. 콜드 빌드 체크포인트 (300초 타임아웃의 답)

문제의 핵심: `compute.run` 이 타임아웃하면 워커를 terminate 하므로 자식이 모은 `stages` 가
부모로 오지 못한다. 그래서 **워커가 진행 상황을 파일에 흘려 두고**, 부모가 죽이기 전에 읽는다.

```
워커                                        부모(웹)
build_log.begin_job("report", sid)          compute.run(...) 대기
  └ build_state_<pid>.json 생성
stage("download")      → sidecar 갱신
stage("decode")                              ⋮ 300초 경과
  checkpoint("decode", "3/7 lot_c.csv")     _dead_worker_state(sid)  ← sidecar 읽기
  checkpoint("decode", "4/7 lot_d.csv")      record_failure(..., state)
stage("yield_cpk")                           _reset_pool(shutdown=True)  ← 여기서 terminate
  (여기서 멎음)                               drop_states(doomed_pids)
build_log.end_job() → sidecar 삭제
```

실패 레코드에 추가되는 필드: `build_id` · `last_stage` · `last_source` ·
`last_stage_elapsed` · `build_elapsed` · `stages`(끝난 단계들) + 세션 메타
(`akey`/`product`/`lot_id`/`file_name`).

**`last_stage` 가 빈 문자열이면 그 자체가 증거다** — 워커가 계산을 시작조차 못 했다는 뜻이고,
곧 "큐에서 대기만 하다 타임아웃"이다(계산이 느린 것이 아니다). 값이 아예 없는 것(성공 빌드)과
구분된다.

배선 규칙:
- `stage(name, source="")` 는 소요를 누적하면서 체크포인트도 갱신한다.
- `checkpoint(name, source)` 는 **누적 없이** 체크포인트만 갱신한다. 같은 이름으로 `stage()`
  를 중첩하면 바깥과 안쪽이 같은 키에 두 번 더해져 소요가 2배가 되고 `eta` 배율 학습까지
  망가진다 — source 단위 진행 표시는 반드시 `checkpoint` 를 쓸 것.
- 잡 배선은 `compute._job(kind)` 데코레이터 한 곳 (`report_job`/`dist_job`/`map_job`/… 에 부착).
  중첩 잡(`prewarm_job` → `report_job` → `dist_job`)은 depth 로 최외곽만 기록한다.
- 같은 데코레이터가 `faulthandler.dump_traceback_later(_TIMEOUT_SEC - 10)` 도 건다 — 타임아웃
  10초 전에 워커가 **자기 스택**을 `faulthandler_worker_<pid>.txt` 에 찍는다. 부모가 자식
  스택을 뜨는 방법은 Windows 에 없으므로 자식이 스스로 찍는 수밖에 없다.
- 기동 시 `compute.sweep_interrupted_builds()` 가 잔존 sidecar 를 걷어 `result="interrupted"`
  레코드 + 사건으로 남긴다 — watchdog 재기동이 무엇을 끊었는지는 이 흔적으로만 알 수 있다.

**storage_gateway 는 동결이라 `download` 단계는 전체 소요만 잰다.** 다운로드 중 정체하면
sidecar 의 stage 가 `download` 로 멈춰 있는 것까지가 얻을 수 있는 정보다.

## 3-1. 업로드 단계 계측 (2026-08-19)

콜드 빌드는 §3 의 sidecar 로 "어느 단계에서 멎었나"를 답할 수 있는데 **업로드는 그럴
수단이 없었다.** sidecar 는 `_in_worker()` 가 False 면 no-op 이고(웹 요청 스레드에선
아무것도 안 남는다) PID 단위 전역 1개라 동시 업로드 3건을 표현하지도 못한다. 그래서
업로드는 별도 레지스트리 대신 **이미 있는 진행 중 요청 등록부에 단계만 얹었다**
([metrics.py](../server/admin_panel/metrics.py) `stage()` / `_req_stages`).

```
요청 스레드                                 샘플러(10초) / 관리자 화면
with stage("slot_wait"):   → 현재 단계 갱신
with stage("multipart"):
with stage("decode", "3/7 lot_c.parquet")   inflight_detail() → stage/stage_elapsed/stages_done
with stage("storage_save"):  (여기서 멎음)   _check_stuck_requests() → stuck_request 사건
teardown                   → 단계 회수        _emit_slow_event() → slow_request 에 stages_done
```

- 단계는 **라우트 4개**(`slot_wait`/`multipart`/`read_files`/`read_dist` —
  [upload_webreport.py](../server/upload_webreport.py)) + **ingest 9개**(`decode`(파일별)/
  `storage_save`/`seed_cache`/`dist_seed`/`dist_pack_save`/`create_session`/`seed_edits`/
  `eval_queue`/`prewarm` — [ingest.py](../web_report/ingest.py)).
- `ingest_webreport(…, trace=)` 는 `with trace(이름, 파일):` 하나만 요구하는 **함수를 인자로**
  받는다. 미지정이면 no-op 이라 기존 호출부는 종전과 동일하고, `web_report` → `server`
  의존도 생기지 않는다.
- 같은 이름으로 **반복** 호출하면 소요가 누적된다(파일을 순회하는 `decode` 가 그렇다).
  중첩하면 안쪽이 끝날 때 바깥 단계로 복원된다.
- 완료 시 **항상** `[upload_webreport] 완료 … total=…s cpu=… slot_wait=… decode=…` 한 줄을
  남긴다. 느리지 않은 업로드까지 남기는 이유는 **기준선이 없으면 "느려졌다"를 판정할 수
  없기** 때문이다.
- `cpu` 는 프로세스 CPU 시간 / 실제 시간 비율이다. 낮으면 계산이 아니라 대기(다른
  프로세스와의 CPU 경합·IO)에 시간을 쓴 것 — 콜드 빌드 워커가 코어를 채워 업로드 디코드가
  굶는 현상([compute.py](../web_report/compute.py) `_lower_worker_priority`)을 사후에
  가려내는 지표다. 20초 이상이면서 0.3 미만이면 로그에 `⚠CPU경합의심` 이 붙는다.

**서버가 볼 수 없는 구간이 있다.** waitress 는 바디를 **전량 수신한 뒤에야** 요청을 처리
큐에 넣으므로, 네트워크 전송 시간과 큐 대기는 위 계측 이전이다. 즉 "클라는 200초 무응답인데
서버 기록은 20초"가 정상적으로 가능하다. 그 구간은 Honey 가 재는 `body_sec`(바디 송신
완료까지) / `wait_sec`(그 뒤 응답까지)로만 알 수 있고
([uploader.post_webreport](../client/transport/uploader.py)), 두 값은 실행 로그와
`honey_upload_fail`/`honey_upload_slow` 사건에 실려 `operation_id` 로 서버 사건과 이어진다.

## 3-2. 서버가 통째로 멎는데 로그가 한 줄도 없다 — 콘솔 QuickEdit (2026-08-19)

**증상**: 클라는 "Web Report 업로드 중...(100%)" 에서 read timeout, 브라우저는 네트워크
에러, 서버 창은 멀쩡히 떠 있는데 출력만 정지. 진단 사건도 로그도 **그 구간에 아무것도 안
남는다**(로그를 쓰는 것 자체가 막힌 상태이므로).

**원인**: Windows 콘솔은 QuickEdit(빠른 편집)이 기본 켜짐이라 **창을 클릭하거나 드래그하면
선택 모드**에 들어가고, 그동안 그 창에 대한 쓰기가 블록된다. 이 서버는 stdout/stderr 를
`_TeeStream`(콘솔+파일 동시 기록)으로 감싸고 루트 로거까지 그 stderr 로 내보내므로
([wsgi.py](../server/wsgi.py)), 콘솔 쓰기가 막히면 로그를 찍던 스레드가 **logging 핸들러 락을
쥔 채** 멈추고 waitress 요청 스레드가 전원 그 락에서 줄줄이 대기한다. 요청 하나가 느린 게
아니라 **서버 전체가 멎는다**.

**판정법 (5초)**: 서버 콘솔 창에서 **Enter 를 한 번** 누른다. 즉시 응답이 재개되면 확정이다
(선택 모드 해제). `log/server_*.txt` 가 멈춘 구간 동안 비어 있다가 Enter 직후 몰아서 쌓이는
것도 같은 증거다.

**조치(도입됨)**: [wsgi.py](../server/wsgi.py) `_disable_console_quickedit()` 가 부팅 시
자기 콘솔의 QuickEdit 비트를 끈다. 배치가 아니라 파이썬 프로세스 자신이 끄는 이유는 기동
경로가 여러 개(start.bat 이 여는 별도 창 · watchdog 재기동 · mypc_start.bat ·
`python wsgi.py` 직접)이고, **그중 start.bat 의 새 창에는 방어가 없었기** 때문이다
(mypc_start.bat 의 `_disable_quickedit.ps1` 은 자기 창만 껐다). 콘솔이 없는 환경이면 조용히
넘어간다.

⚠️ **스레드 덤프를 읽을 때**: `QueueFeederThread` 가 `_feed → wait()` 에 있는 것은 컴퓨트
워커 큐의 **정상 유휴**다 — 멈춘 지점이 아니다. 덤프에서는 `#` 머리말의 `stage=`/`done=` 와
`waitress-*` 스레드의 마지막 프레임을 봐야 한다.

## 4. 사건이 만들어지는 지점

| 사건 | severity | 지점 |
|---|---|---|
| `unhandled_exception` | critical | [ops.py](../server/ops.py) 전역 핸들러 (스택 전문 포함) |
| `compute_unavailable` | warning | 같은 곳 — BrokenProcessPool/TimeoutError → 503 (스택 없음: 서버 버그가 아니라 용량 문제) |
| `slow_request` | warning | [metrics.py](../server/admin_panel/metrics.py) `_emit_slow_event` — `REPORT_SLOW_REQ_MS`(10초) 초과. 기존 `runtime_*.log` 통계와 달리 **요청 상관 ID가 붙는다**. 단 **요청이 끝나야** 도는 teardown 훅이다 |
| `stuck_request` | critical | 같은 파일 `_check_stuck_requests` — **아직 안 끝난** 요청이 임계 초과(**업로드 `REPORT_UPLOAD_SLOW_SEC` 100초 / 그 외 `REPORT_STUCK_REQ_SEC` 120초**, 판정은 `_stuck_threshold` 한 곳). 샘플러(10초)가 돌며 잡고, **첫 1회 스레드 덤프**(`diagnose_stuck_*.txt`)를 함께 남긴다. 위 slow 는 teardown 에서만 돌아 **영영 안 끝나는 요청은 구조적으로 한 줄도 남기지 못했다** — 2026-08-19 업로드 hang(클라 300초 timeout, 서버 무기록, 종료 때 "진행 중 10건" 으로만 존재를 앎)이 그 공백이었다. 사건·로그·덤프 머리말에 **그때의 단계**(`stage`/`stage_source`)가 함께 실린다 → §3-1 |
| `upload_failed` | 400=info / 503=warning / 500=critical | [upload_webreport.py](../server/upload_webreport.py) `_record_upload_failure` (+ 감사 `action=upload, result=fail`) |
| `build_timeout` / `build_broken` / `build_error` | critical/warning | [compute.py](../web_report/compute.py) `_emit_build_failure` |
| `build_interrupted` | warning | 기동 시 잔존 sidecar 스윕 |
| `load_failed` / `load_exception` / `poll_timeout` | warning/info | [boot.js](../server/report/static/webreport/boot.js) → `/api/client_error`. **이미 catch 된 실패**라 `window.onerror` 로는 안 잡히는데, 정작 사용자가 신고하는 건 대부분 이쪽이다 |
| `error` / `unhandledrejection` | warning | [error_beacon.js](../server/report/static/webreport/error_beacon.js) (종전 그대로) |
| `honey_upload_fail` / `honey_crash` | warning | Honey → `/api/client_diagnostic`. 업로드 실패에는 `mb`/`body_sec`/`wait_sec` 가 함께 실린다(§3-1) |
| `honey_upload_slow` | warning | Honey → 같은 곳. **성공했는데도** 서버 응답 대기가 60초를 넘긴 업로드. 실패만 보고하면 임계 직전 상태(=다음 타임아웃의 예보)를 영영 못 본다 |
| `honey_render_crash` | warning | Honey 내장 브라우저의 렌더러(QtWebEngineProcess) 비정상 종료 — [embedded_browser.py](../client/embedded_browser.py) `_on_render_terminated`. GPU/드라이버가 흔한 원인이라 `status`/`exit_code`/`url` 을 context 로 싣는다. 클라 로그(`log/<날짜>.txt`)에는 `--disable-gpu` 조치 힌트도 남는다 |

**의도된 HTTPException(404 등)은 사건을 만들지 않는다.** 정상 응답까지 사건이 되면 목록이
못 쓸 물건이 된다.

### 4-1. 내장 브라우저가 특정 PC 에서만 이상하다 — GPU 우회

같은 페이지가 **웹 브라우저에서는 멀쩡한데 Honey 내장 브라우저에서만**, 그리고 **특정
PC 에서만** 이상하면 GPU 드라이버와 Chromium 합성의 궁합을 먼저 의심한다. 증상 3종은
원인이 같다:

| 증상 | 비고 |
|---|---|
| **마우스를 움직일 때마다 화면 전체가 깜빡임** | 세션 상세처럼 sticky 헤더·고정열 + Plotly SVG + canvas 로 합성 레이어가 많은 페이지에서 두드러진다. 커서 이동이 프레임을 만드는데 드라이버가 부분 갱신에 실패하면 매 프레임 전체가 다시 그려진다. 메인 목록은 레이어가 적어 티가 안 난다 |
| 버튼·모달이 깜빡이거나 통째로 사라짐 | 같은 계열. `server/landing/landing.html` 의 hover 합성 주석 3곳이 그 선례다 |
| 렌더러 비정상 종료(`honey_render_crash`) | 위 표 참조 |

**조치는 하나다 — 그 PC 에서만 소프트웨어 렌더링을 쓰게 한다.** 재빌드·재배포가 필요 없다.

```
setx QTWEBENGINE_CHROMIUM_FLAGS "--disable-gpu"
```

Qt 가 이 환경변수를 직접 읽는다. 사용자 환경변수라 재부팅·클라 업데이트에도 남는다.
빌드본에 동봉된 `honey_safe_gfx.bat` 가 이 한 줄을 대신 실행한다.
배포본에서는 `Honey.exe` 옆 `honey.env` 에 `HONEY_CHROMIUM_FLAGS=--disable-gpu` 를 적어도
같은 효과다(`client/transport/config.py` `CHROMIUM_FLAGS` → `honey_main.main`). 단
**honey.env 는 자동 업데이트 때 배포본 값으로 덮이므로** 영구 조치는 위 환경변수 쪽이다.

적용됐는지는 시작 로그 한 줄로 확인한다 — `[startup] QTWEBENGINE_CHROMIUM_FLAGS=...`
(`(없음)` 이면 안 먹은 것이다).

## 5. Honey 클라이언트 (client/transport/error_report.py)

- `begin_operation(name)` — 작업 단위 시작. 이후 업로드 요청 헤더와 오류 보고가 같은
  `operation_id` 를 공유한다 (`uploader._upload_headers` 가 자동으로 실어 보낸다).
- `report_error(kind, message, ...)` — `/pe/report/api/client_diagnostic` 로 best-effort POST.
  timeout `(3,5)`, 프로세스당 같은 오류 1회, **모든 예외 무음**(보고가 UI 를 깨면 안 된다).
- 최소 수집만 자동으로 보낸다: 파일은 **basename**, 단계·버전·오류 종류까지. 전체 경로·
  원본 데이터는 보내지 않는다(서버도 `diagnostics.scrub_paths` 로 한 번 더 정제).
  사용자 취소나 정상적인 입력 검증 오류는 보고 대상이 아니다 — 그건 고장이 아니다.
- **오프라인 큐** — 서버에 못 보내면 `<CONFIG_DIR>/diag_queue.jsonl` 에 쌓고 다음 실행 시
  `flush_queue()` 가 재전송한다(14일·50건·20MB 상한). 서버가 죽어 있던 사고일수록 기록이
  중요한데 그때가 바로 전송이 실패하는 때라서 필요하다. `event_id` 를 클라가 만들어 보내므로
  재전송이 중복 사건이 되지 않는다.

## 6. 관리자 화면 (`🚨 진단 사건` 탭)

- **미확인 critical/warning 타일** + 현황 탭 경고 칩(클릭 시 이 탭으로 이동)
- **오래 걸리거나 실패한 콜드 빌드** 표 — 60초 이상이면 다음엔 타임아웃이 될 수 있다는
  신호라 성공 빌드도 세운다. `마지막 단계 / 파일` 컬럼이 §3 의 산출물이다.
- **사건 목록** → 클릭 시 **타임라인**: 같은 상관 ID 사건 + 콜드 빌드 레코드 +
  `watchdog_events.log` 를 시간순으로 병합(watchdog.ps1 은 읽기만 하고 수정하지 않는다).
- **증거 기반 원인 안내** (`diagnostics_admin.explain`) — 근거가 있을 때만 말한다:

  | 근거 | 결론 |
  |---|---|
  | `last_stage` 있음 | "'decode' 단계에서 멎었습니다 [3/7 lot_c.csv]" |
  | `last_stage` 가 빈 문자열 + timeout | "큐에서 대기만 하다 타임아웃 (계산이 느린 것이 아님)" |
  | `queue_wait` 가 총 시간의 절반 초과 | 온디맨드 큐 포화 |
  | `pool_wait` 가 절반 초과 | 워커 슬롯·spawn 대기 |
  | 클라 사건에 **상관 ID 가 있는데** 서버 사건 0건 | 네트워크 단절·서버 도달 실패 |
  | 그 외 | **"확인 불가 — 근거가 기록에 없습니다"** |

  마지막 줄이 핵심이다. 틀린 단서 하나가 진짜 원인 탐색을 몇 시간 돌려세운다. 상관 ID 자체가
  없는 사건을 "네트워크 문제"로 단정하지 않는 것도 같은 이유다(찾을 방법이 없었던 것이지
  안 닿았다는 증거가 아니다).

## 7. User Action 기록

- `action="view"` 신설 — `_record_web_visit`(= `/my_access` 조회) 에 얹었고
  **(사용자, 세션)당 1시간 1회**로 중복을 제거한다. 매번 남기면 열람이 감사 로그를 도배해
  정작 업로드·편집 이력을 밀어낸다.
- 모든 GET 을 기록하지는 **않는다** — 사용량은 기존 `report_usage_daily` 가 담당한다.
- 관리자 감사 드롭다운에 누락돼 있던 action(`login*`/`signup`/`eval_*`/`view`/`diag_ack`)을
  보강했다. 기록은 되는데 필터로 못 고르던 상태였다.

## 8. 검증

```
server\.venv\Scripts\python.exe tests\test_diagnostics.py        # 사건 e2e (a~j)
server\.venv\Scripts\python.exe tests\test_build_log.py          # (8) 타임아웃 시 last_stage 보존
server\.venv\Scripts\python.exe tests\test_stuck_request.py      # 끝나기 전에 사건+덤프
server\.venv\Scripts\python.exe tests\test_upload_stage_trace.py # §3-1 단계 계측
```

`test_upload_stage_trace.py` 가 고정하는 것: 단계 누적/중첩 복원/예외 안전 · 진행 중
요청에 현재 단계 노출 · **업로드에 전용 임계(100초)가 범용(120초)보다 먼저 적용** ·
stuck 사건·덤프 머리말에 단계 포함 · 완료된 느린 요청의 `slow_request` 에 단계 분해 첨부 ·
요청 종료 시 회수.

`test_diagnostics.py` 가 확인하는 것: `X-Request-ID` 헤더 / 500 응답 `error_id` == 헤더 ==
사건 `request_id` / 503 은 스택 없이 warning / 404 는 사건 미생성 / Honey 수집과 event_id 유지 /
경로 정제 / 타임라인·"확인 불가" / 업로드 실패 감사+사건 / 세션 열람 1행.

두 테스트 모두 `REPORT_DIAG_DIR` 로 로그를 임시 폴더에 격리한다 — 테스트 사건이 운영
`server/log` 에 섞이면 관리자 화면의 이력을 못 믿게 된다.

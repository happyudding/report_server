# 23. AI Comment [제안] — 클라이언트 LLM 대행 (구현 의뢰서)

> **상태**: 미구현 (설계 승인 완료, 2026-08-27). 아래 "프롬프트" 절 전문을 담당자
> (또는 담당자의 Claude Code 세션)에게 그대로 전달하면 된다.
> **저장소**: `f:\COINAPI\report_server` — **서비스 중인 서버**라 기존 세션 조회에 지장을
> 주면 안 된다.
> **관련 문서**: [19_llm_wiring.md](19_llm_wiring.md)(현행 LLM 배선) ·
> [13_eval_analyzer_integration.md](13_eval_analyzer_integration.md)(eval 연동) ·
> [12_web_report_cache.md](12_web_report_cache.md)(캐시 키 규약)

## 왜 이 작업이 필요한가

AI Comment 의 `[제안]` 문장을 Claude Enterprise 로 만들고 싶은데, **서버 PC 에는
Enterprise 자격증명이 없다**. Claude Enterprise 좌석은 API 키를 발급하지 않으므로 서버
`EVAL_LLM_*` 로는 연결할 수 없다. 반면 사용자 PC 는 사내 **LLM Gateway** 권한이 있다
(`gateway-cli login` 으로 개인 토큰 취득).

따라서 **서버가 프롬프트를 만들어 주고, 업로더 PC 의 Honey 가 Gateway 호출을 대행해
문장을 서버에 push** 하는 구조를 택한다. LLM 접점이 `llm_client.complete()` 한 곳뿐이고
`[제안]` 만 LLM 산출물이며 실패 시 `action_ko` 폴백이 이미 구조적으로 존재해서,
이 분리가 안전하게 성립한다.

---

## 프롬프트 (여기부터 담당자에게 전달)

당신은 COINAPI report_server 저장소에서 아래 기능을 구현한다. 시작 전에 `CLAUDE.md` 와
`docs/INDEX.md`, `docs/19_llm_wiring.md`, `docs/12_web_report_cache.md` 를 읽어라.

### 목표

AI Comment 의 3섹션(`[현상]/[과거사례]/[제안]`) 중 LLM 이 만드는 `[제안]` 문장을,
서버(LLM 자격증명 없음) 대신 **업로드 직후 업로더 PC 의 Honey 클라이언트**가 사내
LLM Gateway(사용자별 `gateway-cli login` 토큰)로 생성해 서버에 push 하고, 서버가 코멘트에
병합하게 하라. 실패 시엔 지금처럼 룰 폴백(`action_ko`) 문장이 그대로 나와야 한다.

추가로 Honey 좌측 Options 패널(입력 파일 / 설정 슬라이드 패널)에 **AI Model 콤보
(`default` / `claude`)** 를 넣고, `claude` 를 고른 업로드에서만 이 대행 흐름이 돈다.
`default` 는 현행 동작(룰 폴백 문장) 그대로 — 즉 이 기능은 전면 전환이 아니라 옵트인이다.

### 배경 사실 (검증 완료 — 그대로 신뢰하되 수정 전 해당 파일은 직접 읽을 것)

- LLM 접점은 단 한 곳: `eval_analyzer/eval_engine/pipeline/recommend.py` `make_comment()`
  → `llm_client.complete()` (OpenAI 호환 chat completions, env `EVAL_LLM_*`, 현재 미설정=off).
  off/실패 시 `suggestion = action_ko`. 최종 문자열:
  `f"[현상] {phenomenon}\n[과거사례] {past_case} \n [제안] {suggestion}"` — **바이트 단위 불변 유지**.
  ⚠ 마지막 섹션 토큰은 2026-08-28 에 `[점검제안]` → `[제안]` 으로 바뀌었다. **캐시에 굳은 옛
  코멘트는 계속 `[점검제안]` 을 실어 오므로**, 이 문자열을 파싱하는 코드는 반드시 둘 다
  받아야 한다(프런트 `sheets.js` 가 그렇게 돼 있다).
- 서버 캐시: `web_report/service.py` `_ai_comment_cached()` — RAM `cache.AI_COMMENT_CACHE` →
  디스크 `disk_cache.load_ai_comment(upload_root, cache_policy.ai_comment_key(session))`
  (`aicmt` json.gz) → 미스면 `ai_comment.safe_build_ex()`. 키에 session_id/edits_rev 는
  **의도적으로 없음**(perf_guard S10·S12 — 절대 추가 금지).
- 조회 대기: 캐시 미스면 `ai_comment_pending=True` payload 즉시 반환 +
  `compute.request_build(sid, root, "ai")` 백그라운드 잡 → boot.js 폴링 → 재렌더.
- 클라→서버 push 선례: `POST .../web_report/rawdata_replace`
  (`server/report/routes_webreport.py` ~L906) — `X-Honey-Agent: 1` 헤더 + `_editor_guard`.
  영구 파생 데이터 저장 선례: `web_report/dist_pack_store.py`
  (`<upload_root>/web_report/<akey>/dist_pack/<chash12>_<mode>[_p<dig8>]/` — 캐시 아님, 축출 제외).
- `evaluate()` 는 `web_report/ai_comment.py` `build_ai_comments()` 가
  `persist=False` 로 호출. eval_engine import 는 ai_comment.py/eval_export.py/eval_debug.py
  **3곳만 허용**(불변규칙 #8 — 새 접점 만들지 말 것).
- Claude Enterprise 좌석은 API 키를 주지 않으므로 서버 `EVAL_LLM_*` 직접 연결 불가가 전제.

### 핵심 설계 결정 (변경 금지)

1. **suggestion 은 캐시가 아니라 영구 오버레이.** dist_pack_store 패턴의 영구 파일에
   저장하고, aicmt 캐시가 채워질 때마다(콜드 재빌드 포함) 재병합한다.
   `ai_comment_key` 에 LLM/세션 축을 추가하지 않는다.
2. **프롬프트 sha 게이트.** `sha256(prompt)[:12]` 를 suggestion 과 함께 저장, 재병합은
   현재 프롬프트 sha 일치 시에만. 룰 변경으로 판정이 바뀌면 sha 가 갈려 자동으로
   action_ko 폴백(옛 LLM 문장이 새 판정에 붙는 사고 차단).
3. **push 반영은 기존 rev 채널 재사용.** 신규 편집 kind `ai_suggest` marker 1건 저장으로
   payload_rev +1 → report/full 캐시 자연 무효화 + `request_build("report")` 선빌드.
   표적 캐시 삭제 API 를 새로 만들지 않는다.

### Phase 0 — 선행 조사 (사용자 PC, 코드 착수 전)

1. `gateway-cli login` 후 토큰 저장 위치·형식·만료 확인.
2. Gateway endpoint 가 OpenAI 호환 chat completions 인지 실호출 1회로 확인.
3. **먼저 확인**: Gateway 가 비대화형 서비스 토큰을 발급해 준다면 이 구현 전체가 불필요 —
   `server/env/server.env` 의 `EVAL_LLM_*` 5줄 설정으로 끝난다. 안 될 때만 아래 진행.
4. 결과에 따라 honey.env 키 확정: 고정 `HONEY_LLM_API_KEY` vs `HONEY_LLM_TOKEN_CMD`.
   Gateway 가 HTTP 가 아니라 CLI 실행형이면: 프롬프트는 stdin 으로, 빈 임시 디렉터리에서,
   도구/MCP/세션저장 차단 + `ANTHROPIC_API_KEY` 등 비의도 env 제거 후 실행.

### Phase 1 — 엔진: 프롬프트 노출 (LLM 미호출, 규칙 #8 유지)

- `eval_analyzer/eval_engine/pipeline/recommend.py`:
  `make_comment_ex(...) -> (comment, prompt)` 신설 — `_build_prompt` 를 LLM on/off 무관
  항상 생성. 기존 `make_comment` 는 `make_comment_ex(...)[0]` 래퍼로 시그니처 유지.
- `eval_analyzer/eval_engine/api.py`: `evaluate(..., include_prompts: bool = False)` kwarg.
  `_process_case` 에서 결과 dict 에 `res["llm_prompt"] = prompt` 후처리(to_result 불변).

### Phase 2 — 서버: 저장·병합·서빙

- `web_report/ai_comment.py`:
  - `build_ai_comments` 가 `include_prompts=True` 호출, 대표 case 기준
    `result["prompts"] = {item: {"prompt", "sha"}}` 추가. `_EMPTY_RESULT` 에 `"prompts": {}`.
  - 순수 헬퍼 3개: `sanitize_suggestion`(개행·제어문자·섹션 토큰 제거, 500자 상한) /
    `patch_suggestion_text`(`re.sub(r"\[(?:점검제안|제안)\]\s*.*$", ...)` — 마지막 섹션만 치환,
    옛/새 토큰 **둘 다** 받는다(캐시에 굳은 옛 코멘트 때문),
    `[MAJOR][이봉]` 접두·앞 2섹션 보존) / `apply_suggestions(result, stored)`(sha 일치
    item 만 `key.endswith("|"+item)` 행 전부 패치 — Yield fan-out 포함, **항상 새 dict
    반환** — 캐시 공유 객체 in-place 수정 금지).
- `web_report/cache_policy.py`: `AI_COMMENT_SCHEMA_VERSION = 3`
  (이 상수만 — `REPORT_SCHEMA_VERSION` 절대 불변, CLAUDE.md 규칙 14).
- **신규** `web_report/ai_suggest_store.py` (dist_pack_store 패턴):
  경로 `<upload_root>/web_report/<akey>/ai_suggest/<chash12>_<mode>[_p<dig8>].json`,
  형식 `{"schema":1, "items": {item: {"sha","suggestion","by","ts"}}}`,
  `load` / `save_merge`(tmp→replace 원자 교체) / `delete_stale`.
- `web_report/service.py`:
  - `_AI_RESULT_KEYS` 에 `"prompts"` 추가.
  - `_ai_comment_cached` build 성공 시 캐시 저장 직전 store 재병합(**재빌드 생존 지점**).
  - 신규 `get_ai_comment_prompts(sid,...)`: ai 옵션 아님→None(라우트 404), 캐시 미스→
    `request_build(sid, root, "ai")` 예약 후 None(202), 히트→`{"items":[{key,sha,prompt}]}`.
  - 신규 `apply_ai_suggestions(sid, items,...)`: 검증(건수≤500, sha 12hex, sanitize) →
    `cache.keyed_lock_ctx` 안 `save_merge` → aicmt RAM+디스크 패치(sha 불일치 skip) →
    `apply_webreport_edits(sid, [("ai_suggest","push",marker)])` rev bump →
    `request_build("report")` → `{"accepted","skipped"}`.
- `web_report/edits.py`: `KIND_AI_SUGGEST = "ai_suggest"`, `_STATE_EXCLUDED_KINDS` 에 추가.
  **`PAYLOAD_NEUTRAL_KINDS` 에는 넣지 않는다** — payload 값을 실제로 바꾸므로(규칙 16).
- `server/report/routes_webreport.py`: `GET .../web_report/ai_comment/prompts` +
  `POST .../ai_comment/suggestions` — rawdata_replace 와 동일 가드
  (`X-Honey-Agent: 1` 아니면 403 + `_editor_guard` + `_audit`), POST body ≤ 2MB.
- (선택) boot.js `ai_suggest_pending` 재폴링(상한 90초) — 미적용 시에도 새로고침으로 반영.

### Phase 3 — 클라(Honey) UI: Options 패널의 AI Model 콤보

좌측 Options 패널은 전부 `client/honey_main.py` `_build_controls_panel()` (약 L903~1027)
안에서 만들어진다(`honey_ui/` 아님). Web Report QGroupBox 안 "AI Comment" 체크 행이
약 L966~985 에 있고, 그 아래 L986 이 `btn_web_report` 다.

- **UI 추가**: `ai_row`(L985) 바로 뒤, `web_v.addWidget(self.btn_web_report)` **앞**에
  새 QHBoxLayout — `QLabel("AI Model")` + `self.cbo_ai_model = QComboBox()`,
  `addItems(["default", "claude"])`. `QComboBox` 를 L906~907 지연 import 목록에 추가.
- **활성화 연동**: AI Comment 체크는 라벨 10회 클릭 숨김 스위치(`eventFilter` L800~812)로
  풀린다. 콤보도 같은 자리에서 함께 `setEnabled` 하고 `chk_ai_comment.toggled` 에 연결 —
  AI Comment 가 꺼져 있으면 모델 선택은 의미가 없다.
- **영속화**: 모델 선택만 `client/app_settings.py` `get_setting`/`set_setting("ai_model")`
  로 기억한다(QSettings 아님). AI Comment on/off 는 **영속하지 않는 현행 결정을 유지**한다
  — L970~973 주석의 2026-08-04 이력("화면은 비활성인데 체크는 켜짐" 버그) 존중.
- **옵션 주입**: `_prepare_web_report_context()` 의 options dict(L2779~2781)에
  `"ai_model": "claude"` 를 넣되 **`default` 면 키를 아예 넣지 않는다** — `report_key`
  (cache_policy.py L422)가 옵션 원문을 통째로 물고 있어, 키가 없어야 기존 세션 캐시 키가
  바이트 그대로 유지된다(콜드 폭풍 회피).
- ⚠ 두 번째 manifest 조립 경로(약 L3704~3710, Excel 왕복/재업로드 계열)도 함께 볼 것.
- **서버 파싱**: `web_report/validation.py` 에 `webreport_ai_model(opts_raw) -> str` 을
  `webreport_step`(L58~76) 형태로 추가 — 화이트리스트 `{"default","claude"}`, 미지값은
  `"default"` 폴백. `REPORT_SCHEMA_VERSION` bump 불필요(옵션 원문이 이미 키에 있음).
- **서버 게이팅**: Phase 2 의 `get_ai_comment_prompts` 는 `ai_model == "claude"` 세션에만
  프롬프트를 내주고, 아니면 None(404). 클라도 업로드 시 자기가 고른 값으로 대행 여부를
  판단하므로 이중 게이트다.
- (선택) 모델명 실제 반영이 필요해지면 `llm_client.py` L57 의 `model_version` 인자가 이미
  후크로 열려 있다 — 이번 범위에서는 쓰지 않는다(클라가 자기 Gateway 모델로 호출).

### Phase 4 — 클라(Honey): 업로드 직후 조용한 백그라운드

- **신규** `client/transport/llm_gateway.py`: honey.env 키
  `HONEY_LLM_ENDPOINT/MODEL/API_KEY|TOKEN_CMD/TIMEOUT(30)/MAX_CASES(50)/WORKERS(4)`.
  `is_enabled()`(셋 다 있어야 True) / `chat_url()`(eval_engine `llm_client.chat_url` 정규화
  규칙 vendor copy — import 금지) / `get_token()`(고정키 우선, 없으면 TOKEN_CMD subprocess
  1회+인메모리 캐시) / `complete(prompt)`.
- **신규** `client/report_flow/ai_suggest.py`: `start_background(sid, base_url)` —
  daemon 스레드 즉시 반환(UI 블록 금지, Qt 위젯 접근 금지). worker:
  prompts 폴링(3초 간격, 상한 300초; 202 재시도, 403/404/5xx 3회 후 포기) →
  ThreadPoolExecutor(3~5) 병렬 Gateway 호출(건별 실패 skip) → `POST suggestions`.
  모든 예외 조용히 삼킴(폴백 무해).
- `client/honey_main.py`: webreport 업로드 성공 지점 2곳
  (`sid = result.get("session_id")` 직후 — 약 L3243·L3733)에서 **ai_comment optin +
  ai_model=="claude"** 인 업로드에만 기동.

### 반드시 지킬 것 (위반 시 리뷰 반려)

1. 코멘트 평문 3섹션 형식 바이트 단위 불변 — sheets.js/Excel/챗봇/eval_export 가 같은
   평문을 소비한다(CLAUDE.md 규칙 12).
2. `ai_comment_key` 에 session_id/provider/LLM 축 추가 금지(perf_guard S10·S12).
   suggestion 은 데이터 세대(akey+chash+mode+prep)의 파생물 — dedup 형제 공유가 의도.
3. eval_engine import 는 기존 3파일 밖으로 늘리지 않는다(규칙 #8). 클라에
   eval_analyzer 패키징/직접 import 금지 — 정규화 규칙은 vendor copy.
4. 서버는 자기가 만든 `prompts` 의 item+sha 일치 건만 수용(임의 row_key 제출 불가).
   불일치·불합격은 409 가 아니라 조용히 skip+카운트(dist_pack 관례).
5. Gateway 토큰·자격증명을 report 서버로 절대 전송하지 않는다. 프롬프트는 사내
   Gateway 로만 나간다.
6. 서버 `EVAL_LLM_*` 는 계속 미설정 유지(켜면 이중 생성). 챗봇 질문해석도 이 env 를
   공유하지만 현 상태(off) 유지이므로 무영향 — 클라 대행 경로는 AI Comment 전용.
7. 신구 호환: 구 Honey → `ai_model` 키 없음 → `default` → 서버 동작 불변(폴백 문장).
   신 Honey + 구 서버 → prompts 404 시 조용히 포기.
   `ai_model` 값이 `"default"` 면 options dict 에 키를 넣지 않는다(기존 캐시 키 보존).
8. `web_report/`·`server/report/` 쓰기는 perf_guard 훅이 검사한다 — 막히면 우회하지 말고
   설계를 재검토하라(`docs/18_perf_guard.md`).

### 검증 (이 프로젝트는 pytest 일괄 아님 — `.claude/skills/run-tests` 관례: self-run)

1. 엔진: `make_comment_ex` 프롬프트 반환 + LLM off 시 comment 문자열 종전 완전 일치,
   `evaluate(include_prompts=True)` 케이스별 `llm_prompt` 존재.
2. 서버 `tests/test_ai_suggest.py` 신설: sanitize / patch(접두·2섹션 보존) / sha 게이트 /
   store 원자성 round-trip / apply 가 RAM+disk 패치 + payload_rev bump /
   aicmt 캐시 삭제 후 재빌드 시 store 재병합 / sha 불일치(룰 변경 모사) 폴백.
3. 라우트: X-Honey-Agent 부재 403, 비편집자 거부, 빌드 미완 202, 완료 200, push 카운트,
   `ai_model != claude` 세션은 404.
4. 옵션 파싱: `webreport_ai_model` 이 미지값·결측·파싱실패에서 `"default"` 폴백,
   `default` 업로드의 options JSON 이 기존과 바이트 동일(캐시 키 보존).
5. 클라 시뮬레이션: 가짜 OpenAI-shape 로컬 http 서버 + 로컬 report_server 로 worker 함수를
   스레드 없이 동기 실행(폴링→호출→push).
6. e2e: Phase 0 확인 → honey.env 설정 → **Options 에서 AI Model=claude 선택** → 실업로드 →
   build_log ai 잡 완료 → 클라 push 로그 → 리포트 새로고침 시 `[제안]` 이 LLM 문장인지 →
   `/pe/eval` 룰 저장 후 재조회로 생존/폴백 확인. 그리고 **AI Model=default 업로드가 종전과
   동일**한지 대조. 서버 반영은 terminate.bat → start.bat (`.claude/skills/server-restart`).
   aicmt v3 는 ai 옵션 세션 한정 재평가라 전역 프리웜 불필요.

## 프롬프트 끝

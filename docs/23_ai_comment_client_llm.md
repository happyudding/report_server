# 23. AI Comment [제안] — 클라 로컬 Claude CLI 대행

> **상태**: **서버·클라·call_claude 구현 완료 (2026-08-28)** — 개발 환경에서 검증 가능한
> 전부를 검증했고, **남은 것은 현장(Enterprise gateway) 검증뿐**이다
> (→ §현장 검증 항목, [call_claude/README.md §7](../call_claude/README.md)).
> **2026-09-01 보강**: 배치 구조화 출력(`--json-schema` 자동 게이팅) · 지시문 확장
> (근거 없는 줄 채우기 차단, `AI_COMMENT_SCHEMA_VERSION` v6 → **발화 signature 커버리지
> v8**) · 관리자 흐름 디버깅 3종
> (프롬프트 본문·타임라인·skip 사유) · **사용자에게 보이는 실패 알림**(종전엔 워커가
> 전부 조용해 사용자가 실패를 인지할 방법이 없었다).
> **저장소**: `f:\COINAPI\report_server` — **서비스 중인 서버**라 기존 세션 조회에 지장을
> 주면 안 된다.
> **관련 문서**: [19_llm_wiring.md](19_llm_wiring.md)(현행 LLM 배선) ·
> [13_eval_analyzer_integration.md](13_eval_analyzer_integration.md)(eval 연동) ·
> [12_web_report_cache.md](12_web_report_cache.md)(캐시 키 규약) ·
> [call_claude/README.md](../call_claude/README.md)(CLI 호출 모듈 정본)

## 왜 이 작업이 필요한가

AI Comment 의 `[제안]` 문장을 Claude 로 만들고 싶은데, **서버 PC 에는 Enterprise
자격증명이 없다**(Enterprise 좌석은 API 키를 발급하지 않아 서버 `EVAL_LLM_*` 로 연결
불가). 반면 **사용자 PC 의 standalone Claude Code CLI 는 gateway 인증으로 동작하며,
`claude -p` 프린트 모드 비대화형 호출이 Gateway 관리팀 승인 없이 가능함이 확인됐다**
(2026-08-28 검증). 따라서 **서버가 프롬프트를 만들어 주고, 업로드 직후 업로더 PC 의
Honey 가 로컬 claude CLI subprocess 로 문장을 생성해 서버에 push, 서버가 코멘트에
병합**한다. 실패 시엔 지금처럼 룰 폴백(`action_ko`) 문장이 그대로 나온다.

옵트인이다: Honey 좌측 Options 패널의 **AI Model 콤보(`default`/`claude`)** 에서
`claude` 를 고른 업로드에서만 이 흐름이 돈다. `default` 는 현행 동작 그대로.

### 2026-08-28 설계 변경 2가지 (구 설계서 대비)

1. **수송 = 로컬 Claude CLI subprocess** (구: 사내 LLM Gateway HTTP + `gateway-cli` 토큰).
   Gateway HTTP 안(`client/transport/llm_gateway.py`, `HONEY_LLM_*` 키)은 **폐기**됐다 —
   CLI 가 인증·모델 선택을 전부 안고 있어 토큰 관리 자체가 사라진다. 호출부는 재사용
   가능한 최상위 패키지 **[call_claude/](../call_claude/README.md)** 로 분리했다
   (다른 프로젝트에서도 사용 — Entry point 명확화).
2. **Phase 1(엔진 프롬프트 노출) 폐기 — 프롬프트는 서버가 조립한다.**
   [web_report/ai_prompt.py](../web_report/ai_prompt.py) 가 evaluate() 의 case dict 로
   프롬프트를 조립한다. 성립 근거: 서버 LLM off 상태에서는 case comment 의 `[제안]`
   섹션 == `action_ko` 그대로이고, 발화 signature(action_ko 포함)·선례가 전부 case dict
   에 있다. 프롬프트는 이 경로 안에서만 생성→sha 게이트→소비되는 **자기완결 계약**이라
   엔진과 바이트 일치가 필요 없다. 단 지시문은 `recommend._build_prompt` 의 원문 사본이며
   드리프트는 `tests/test_ai_prompt_determinism.py` 가 ast 대조로 감지한다.
   > 이 구조는 도입 당시(2026-08-28) "eval_analyzer 를 못 고친다"(git pull 불가·외부
   > 진행분 conflict)가 근거였다. **그 동결은 같은 날 해제됐지만 구조는 그대로 옳다** —
   > 규칙 #8 이 eval_engine import 를 3파일로 고정하고 있어 `ai_prompt.py` 는 어차피
   > 엔진을 부를 수 없기 때문이다. 다만 **"엔진이 줘야 맞는 재료"는 이제 엔진을 고쳐서
   > 받는다** — 선례 상세가 그 예다(아래 §선례 상세 보강). 엔진 노출(`make_comment_ex`)로
   > 되돌리는 선택지도 열려 있다(그때도 sha 게이트 계약은 그대로).

## 핵심 설계 결정 (변경 금지 — 구현 완료)

1. **suggestion 은 캐시가 아니라 영구 오버레이 — 그리고 세션 고유다** (2026-09-02 개정).
   **저장은 세션 편집 DB**(`report_webreport_edit`, kind=`ai_suggest`, item_key=item_raw,
   value=JSON `{suggestion,cases,sha,by,ts,provider,raw?}`)이고, 병합은 **payload 조립
   시점**([service.py](../web_report/service.py) `_session_ai_overlay`)에 한다.
   > **왜 바꿨나**: 종전엔 `<upload_root>/web_report/<akey>/ai_suggest/…json` 공유 파일에
   > 저장하고 그 병합 결과를 **aicmt 캐시(`ai_comment_key` — session_id 없음)에 구웠다**.
   > 그래서 같은 rawdata 를 다시 올린 dedup 형제 세션이 서로의 문장을 봤다 — 새 세션이
   > 남의 옛 LLM 문장부터 보여 주고(사용자 신고 ①), 한쪽 push 가 이미 만들어진 다른
   > 세션 화면을 바꿨다(신고 ②). 세션 편집 DB 는 테이블이 세션 단위라 이 간섭이
   > **구조적으로** 불가능하고, 세션 삭제 시 함께 정리되며 DB 백업에도 포함된다.
   > 저장 자체가 `payload_rev` +1 이라 별도 marker 행도 필요 없다(legacy `push` 행은
   > 로더가 건너뛴다). 회귀 가드: perf_guard **S17-ai-suggest-session-scope** +
   > `tests/test_ai_suggest.py` **(b2) 형제 세션 무간섭**.
   `ai_comment_key`(엔진 평가 캐시)에는 **여전히** 세션 축을 넣지 않는다(perf_guard
   S10·S12) — 엔진 산출(signature·[현상]·action_ko·선례)은 결정적이라 형제 공유가 이득이고,
   그 위에 세션 문장만 덧칠하는 구조다.
   `ai_suggest_store.py` 는 옛 파일을 읽을 코드가 없어져 **사실상 죽은 모듈**이다
   (롤백 창구로 한동안 남긴 뒤 제거 예정 — 아래 §행 단위 Loading 참조).
2. ~~**프롬프트 sha 게이트.**~~ → **폐기됨 (2026-09-02 사용자 결정).** `sha256(prompt)[:12]`
   는 계속 저장하지만 **판정에 쓰지 않는다** — 수용(`apply_ai_suggestions`)·재병합
   (`apply_suggestions`) 둘 다 sha 와 무관하게 최신 저장분을 쓴다.
   폐기 이유: 지시문을 한 글자만 고쳐도 sha 가 전량 갈려 **전 세션이 재대행 전까지
   action_ko 나열로 후퇴**했다. store 에는 멀쩡한 LLM 문장이 있는데 관리자 탭
   (게이트 없음)과 Issue Table(게이트 있음)이 서로 다르게 보이는 신고의 실제 원인이었고,
   룰을 자주 고칠수록 화면이 나빠지는 구조였다. 옛 프롬프트 기준 문장이라도 룰 문장보다
   낫고, 클라 워커는 sha 로 건너뛰지 않으므로(전 항목 재생성) 다음 재대행에 자연 교체된다.
   대신 **금지 문구(deny)를 병합 시점에도 적용**해 옛 변명 문장이 되살아나는 것을 막는다
   (= 패턴 편집이 저장분에 소급). sha 는 관리자 `stale` 표시(옛 프롬프트 기준 문장)용으로만
   남는다. ⚠ 되살리려면 위 회귀를 먼저 해결할 것 —
   회귀 방지는 `tests/test_ai_suggest.py` (h)·`test_ai_prompt_determinism.py` (h).
3. **push 반영은 기존 rev 채널 재사용.** 편집 kind `ai_suggest`(marker 1건, item_key
   `"push"`) 저장으로 payload_rev +1 → report/full 캐시 자연 무효화 +
   `request_build("report")` 선빌드. 표적 캐시 삭제 API 없음.
   (`PAYLOAD_NEUTRAL_KINDS` 에 **넣지 않는다** — payload 값을 실제로 바꾼다, 규칙 16.)

## 구현 지도 (2026-08-28 완료분)

### call_claude/ — CLI 호출 (신규 최상위 패키지, 재사용 대상)

정본은 [call_claude/README.md](../call_claude/README.md). 요지: `find_cli/probe/
run_prompt/run_batch`, 표준 lib 만·무의존·공개 함수 무예외(None 폴백), 프롬프트 stdin
only, 빈 임시 cwd, utf-8 고정, `--help` 스캔 플래그 게이팅(`--safe-mode`·`--tools ""` 등,
**`--bare` 금지** — OAuth 차단), 배치 = nonce 구분 메타 프롬프트 1회 호출 + 관대 파싱
(실패 배치는 전건 None — 폴백 무해).

**구조화 출력**(2026-09-01): 지원 버전이면 배치에 `--json-schema` 를 자동 부착한다
(`batch.BATCH_JSON_SCHEMA` — `[{"id":int,"text":str}]`). 목적은 파싱 대체가 아니라
**형식 이탈 제거**다 — 관대 파싱은 코드펜스·서두를 걷어낼 수 있지만 모델이 배열 대신
객체를 내거나 id 를 빠뜨리면 그 배치가 통째로 None 이 된다. **단건에는 붙이지 않는다**
(자유 문장이 정상). 미지원 버전은 종전 동작 그대로 — `probe()["json_schema"]` 로
어느 쪽인지 확인한다.

### 서버

| 파일 | 내용 |
|---|---|
| [web_report/ai_prompt.py](../web_report/ai_prompt.py) (신규) | case dict → 프롬프트 재구성(`build_prompt(case, enrich)`/`build_prompts`, 키는 **item_raw**) · `prompt_sha` · `split_comment`(신 `[제안]`/구 `[점검제안]` 둘 다) · `sanitize_suggestion`(개행 보존 — 엔진 프롬프트가 '- ' 항목 형식 요구, 1800자 상한) · `patch_suggestion_text`(마지막 섹션만 치환, `[MAJOR][이봉]` 접두·앞 2섹션 바이트 보존) · `apply_suggestions`(sha 게이트 + `key.endswith("\|"+item)` fan-out, **항상 copy**) |
| [web_report/ai_suggest_store.py](../web_report/ai_suggest_store.py) (신규) | 영구 저장 `load/save_merge(tmp pid→os.replace)/delete_stale` · 상한 500 · akey 안(세션 삭제 시 정리) |
| [web_report/ai_comment.py](../web_report/ai_comment.py) | `build_ai_comments` 반환에 `prompts` 부착 · `_EMPTY_RESULT` 에 `"prompts": {}` (eval import 무변경 — 규칙 #8) · `_prompt_enrich` 가 **현재 케이스** 재료를 조립 (§선례 상세 보강) |
| [eval_analyzer/eval_engine/store.py](../eval_analyzer/eval_engine/store.py) | `search_precedents` 가 선례 행에 **당시 수치**(최신 run 의 raw_metrics/features + unit/status)를 함께 싣는다 (§선례 상세 보강). DDL 무변경 — SELECT 확장뿐 |
| [eval_analyzer/…/present.py](../eval_analyzer/eval_engine/pipeline/present.py) | `_precedent_result` — 선례 계약 dict(종전 5키 + 식별·당시 수치) |
| [web_report/cache_policy.py](../web_report/cache_policy.py) | `AI_COMMENT_SCHEMA_VERSION = 10` (v4 prompts 키 → v5 본문 확장 → v6 지시문 확장 → v7 운영자 지시문 합류 → v8 커버리지 → v9 두 블록 계약 + `precedents`/`precedent_counts` 키 → **v10 "JSON 으로 답하지 마라" 지시**). `ai_comment_key` 구성 불변. payload 쪽(`ai_precedents` 키)은 `_eval_rules_suffix` 의 **"aiprec"** 표식이 담당 — 전역 bump 금지 |
| [eval_analyzer/…/rules/ai_prompt.yaml](../eval_analyzer/eval_engine/rules/ai_prompt.yaml) (신규) | 운영자 지시문 + 금지 문구 **정본**. 편집은 `/pe/eval` AI 지시문 탭 → [eval_panel/rules_io.py](../server/eval_panel/rules_io.py) `save_ai_prompt` · 로더 [_rules.py](../eval_analyzer/eval_engine/pipeline/_rules.py) `ai_prompt_doc`/`ai_prompt_instructions` · 서버 창구 [eval_debug.py](../web_report/eval_debug.py) `ai_prompt_rules` |
| [web_report/ai_suggest_store.py](../web_report/ai_suggest_store.py) | 항목에 선택 키 `raw`(sanitize **이전** LLM 원문) — sanitize 결과와 **다를 때만** 저장, 상한 `MAX_RAW_CHARS`(4000). 관리자 검수에서 "모델이 이상한 것" vs "서버가 걷어낸 것" 구분용 |

### 선례 상세 보강 (2026-08-28)

**문제**: 프롬프트의 과거사례가 `- <제품명>: <comment 원문>` 한 줄뿐이고 현재 케이스 쪽에도
수치가 하나도 없어(signature id + action_ko 문구만) "그때 vs 지금" 대조가 원리적으로
불가능했다. 과거 Comment 는 당시 담당자가 무엇을 근거로 판단하고 어떻게 해결했는지 적어 둔
**정답지**인데, LLM 이 이를 활용할 재료가 없어 사례를 흘려보냈다.

**원인**: 선례의 당시 통계(`raw_metrics`)·L2 feature(`features`)·unit(`item_master`)·당시
signature(`case_signature`)·lot_id(`fail_case`)는 **eval DB 에 전부 저장돼 있는데**, 엔진
`present.to_result` 가 계약 dict 에 comment/product_name/family_product 만 남기고 나머지를
버렸다.

**해결** — 과거 쪽은 **엔진에서**, 현재 쪽은 web_report 에서:

| 방향 | 무엇을 | 어디서 |
|---|---|---|
| 과거 | 당시 통계·L2 feature·signature·status·unit·lot·bin | **엔진**: `store.search_precedents` 가 최신 run 의 `raw_metrics`/`features` 를 LEFT JOIN(+`im.unit`·`ev.status`) → `present._precedent_result` 가 계약 dict 에 담는다 |
| 현재 | unit/LSL/USL + L1 통계(`[현재 통계]` 줄) | `eval_export._find_item_meta` / `_dist_metrics` / `_yield_metrics` **재사용** — 선례 `raw_metrics` 를 만든 바로 그 산식이라 같은 자로 잰 값이 된다(규칙 13) |
| 현재 | L2 근거값(signature 줄의 `[근거: …]`) | 엔진이 이미 case dict 에 싣는 `signatures[].evidence` — 추가 계산 없음 |
| 지시 | 정답지로 대조·활용하라 | `ai_prompt._INSTRUCTION_EXTRA` (base `_INSTRUCTION` 은 사본이라 **바이트 그대로** 두고 뒤에 잇는다 → 드리프트 감지 테스트 (e) 무변경) |

### 지시문 확장 — 근거 없는 줄 채우기 차단 (2026-09-01)

base 지시문의 "최대 5줄"은 **상한**인데, 상한만 주면 근거가 2개뿐인 케이스에서도 5줄을
채우려고 재료에 없는 점검 항목이 나온다. 지어낸 *수치* 는 프롬프트와 대조하면 잡히지만
지어낸 *일반론* 은 그렇지 않아 오히려 더 위험하다. `_INSTRUCTION_EXTRA` 에 3줄을 덧붙여
막는다(5줄은 목표가 아님 / 근거 부족한 항목은 빼라 / 판단 불가는 확인 대상으로 써라).

⚠ 지시문을 한 글자라도 바꾸면 **프롬프트 sha 가 전량 갈려 저장된 suggestion 이 폐기**되고
룰 문장으로 되돌아간다(설계상 정상 — sha 게이트). 그래서 `AI_COMMENT_SCHEMA_VERSION` 을
**반드시 함께 올린다**(v5→v6) — 안 올리면 캐시에 굳은 옛 prompt/sha 가 그대로 나가
클라가 옛 지시문으로 계속 대행한다. 전역 bump 는 금지(규칙 14).

### 발화 signature 커버리지 — 위 축소 지시의 대상 한정 (2026-09-01, v8)

**증상**: Issue Table Signature 컬럼에는 여러 개가 뜨는데 [제안]은 **그 중 하나**(사실상
primary)에 대한 얘기만 나온다.

**원인은 재료가 아니라 지시 충돌이었다.** `_sig_lines` 는 `case["signatures"]` 를 상한·
슬라이스·primary 필터 **없이** 전량 싣고, Signature 컬럼의 정본 `ai_comment._case_sig_ids`
도 **같은 배열**을 읽는다 — 화면에 N개가 보이면 프롬프트에도 N줄이 들어가 있었다.
(엔진 관계 선언 `exclusive`/`hidden_by`/`replaces` 가 목록을 지웠다면 화면 컬럼도 함께
줄었을 것이므로 그 경우와 구분된다.) 문제는 그 아래였다:

| 위치 | 방향 |
|---|---|
| `_INSTRUCTION` "발화한 signature 전체와 … 종합해서 작성하라 - 하나만 보고 쓰지 마라" | 확장 |
| `_INSTRUCTION_EXTRA` 선례 대조 4줄 | 5줄 예산을 선례로 소진 |
| `_INSTRUCTION_EXTRA` 위 축소 2줄 (v6) | **축소** |

축소 지시가 **더 뒤에·더 구체적**이라 모델이 그쪽을 따랐다. 게다가 "전체를 종합하라"는
*"여러 개를 보고 판단하라"* 로도 읽혀, primary 하나만 서술하고도 "종합했다"가 성립한다.

**조치는 줄 수 늘리기가 아니라 5줄 예산의 배분 순서 뒤집기다** (5줄 상한은 유지):

1. `rules/ai_prompt.yaml` `instructions` 에 2개 추가 — `cover_all_signatures`(목록의 각
   항목이 최소 한 줄에서 다뤄지게, 원인이 이어지면 묶어도 되지만 빠뜨리지는 마라) ·
   `signature_budget_first`(예산은 signature 를 먼저 덮는 데 쓰고 남는 줄로 선례 대조).
   **yaml 에 둔 이유**: 이 부류는 문구 튜닝이 필연이라 `/pe/eval` AI 지시문 탭에서 코드
   배포 없이 고칠 수 있어야 하고, yaml 은 서버 사본과 엔진 `recommend._build_prompt`
   **양쪽에** 자동 합류한다(코드 하드코딩은 서버 전용이라 두 경로가 갈린다).
2. `_INSTRUCTION_EXTRA` 축소 2줄의 **대상을 "발화 목록 밖"으로 한정**(삭제 아님 — v6 의
   일반론 오염 차단 의도는 유효하다). 발화한 signature 는 발화 사실 + `[근거: …]` 가 이미
   재료라 "근거 부족"에 해당하지 않는다. 3번째 줄("판단할 수 없는 부분은 … 무엇을 더
   확인해야 하는지로 써라")은 **커버리지의 탈출구**라 그대로 둔다.
3. `[발화 signature 전체]` 헤더에 **발화 건수** 표기(`_sig_count`) — 지시문만으로는 모델이
   목록 개수를 세다 틀린다. ⚠ `_sig_count` 는 `_sig_lines` 와 **문자 그대로 같은 기준**
   (id 빈 행 스킵)이어야 한다. 갈리면 "3건이라 써놓고 2줄만 있는" 프롬프트가 나간다
   (`_precedent_count` ↔ `_precedent_lines` 와 같은 규약).

회귀 가드는 `tests/test_ai_prompt_determinism.py` (l)(m) — 헤더 건수 == `_sig_lines` 줄 수
== `_case_sig_ids` 길이 3자 일치 + 배포 yaml 에 두 지시가 켜져 있는지 + 축소 지시가
무조건형으로 되돌아가지 않았는지.

⚠ 튜닝 시: 일반론이 늘었으면(v6 역효과 재발) **yaml 쪽**을 강화한다 — 코드 배포가 필요
없다. 단 `/pe/eval` 저장은 rules_rev 를 올려 또 sha 가 갈리므로 몰아서 할 것.

### 분량·문체·중심 개정 — 10줄 / 지표명 금지 / 사례 위주 (2026-09-02, v11)

사용자 결정 4건을 한 번에 반영했다. 위 v6·v8 이 **5줄 예산 안에서** 배분을 다투던 것을
예산 자체를 늘려 푼 것이라, 그 두 절의 조치(축소 지시 한정·커버리지 지시)는 유지된다.

| 바뀐 것 | 전 | 후 | 어디 |
|---|---|---|---|
| 분량 | 최대 5줄 | **전체 10줄 + signature 하나당 5줄** | `_INSTRUCTION`(양쪽 사본) · yaml `signature_budget_first` · `_INSTRUCTION_EXTRA`("10줄은 상한이지") |
| 수치 | 제한 없음 | **내부 지표명·값 출력 금지** (CPK·수율·단위 붙은 측정값만 예외) | `_INSTRUCTION` · yaml `no_metric_names`(신설) |
| 중심 | 기본 조치 목록(action_ko) 통합 | **사례 위주** — 조치 목록은 사례가 안 덮는 signature 를 메우는 보조 | `_INSTRUCTION` · yaml `integrate_precedents`(개정) |
| 문체 | 규정 없음 | 핵심 단어만, 군더더기 제거 | `_INSTRUCTION` · yaml `terse_lines`(신설) |

**⚠ 수치 금지는 출력에만 건다 — 프롬프트 재료는 그대로다.** 이 비대칭이 이 절의 핵심이다.
사용자가 모른다고 한 것은 `FAIL_MAD_MIN`·`TAIL_MASS_3S_HIGH` 같은 **지표 이름**이지 대조
자체가 아니다. 재료 쪽(`[근거: …]` · `[현재 통계]` · 선례의 `당시 통계`/`당시 분포·공간`)
까지 빼면 이 프롬프트의 존재 이유인 *"그때 값 vs 지금 값"* 대조가 **원리적으로 불가능**해져
사례가 무용지물이 된다. 되돌림 방지 가드는 `tests/test_ai_prompt_determinism.py` **(t)** —
재료 4종이 프롬프트에 그대로 실리는지를 함께 고정한다.

`MAX_SUGGESTION_CHARS` 도 1000 → **1800** 으로 올렸다(상한이 5줄→10줄이 됐으므로). 이 값은
**잘라내기**라 모자라면 마지막 줄이 문장 중간에서 끊긴 채 저장된다 — 줄 수 상한을 다시
건드릴 때 함께 볼 것.

캐시는 v5~v10 과 같은 이유로 `AI_COMMENT_SCHEMA_VERSION` 만 v11 로 올린다(전역 bump 금지).

### 예산 12줄 + 줄이는 순서 · 사례의 "결론"을 쓴다 (2026-09-02, v12)

같은 날 v11 의 10줄이 다시 **12줄**이 됐다. 이유는 분량이 아니라 **무엇을 먼저 버리는가**
였다 — 예산이 모자랄 때 모델이 사례가 있는 signature 의 구체적 조치부터 뭉개고 일반론을
남기는 일이 있었다. 그래서 줄 수를 늘리는 것과 **줄이는 순서를 못 박는 것**을 같이 했다.

| 바뀐 것 | 전 | 후 | 어디 |
|---|---|---|---|
| 분량 | 전체 10줄 | **전체 12줄** (signature 당 5줄은 유지) | `_INSTRUCTION`(양쪽 사본) · `_INSTRUCTION_EXTRA`("12줄은 상한이지") · yaml `signature_budget_first` |
| 예산 배분 | "발화를 먼저 덮어라" 까지만 | **사례가 없는 signature 줄부터 줄이고, 사례가 있는 signature 의 구체적 판단 조치는 최대한 지킨다** | yaml `signature_budget_first` |
| 사례 활용 | "무엇을 확인해 어떻게 해결했는지" | **사례가 실제로 내린 결론**(진성/낙도성 여부, 재현 여부, wait 안정화, 개발팀 협의 내용)을 지금 확인할 일로 바꿔 쓴다. 기본 조치 문구를 그대로 쓰거나 요약하는 것 금지 | yaml `integrate_precedents`(재개정) |

⚠ **줄 수가 네 곳에 흩어져 있다** — `_INSTRUCTION` 2벌(`web_report/ai_prompt.py` +
엔진 원본 `recommend.py`, 바이트 동일이어야 함) · `_INSTRUCTION_EXTRA` · yaml
`signature_budget_first`. 하나만 고치면 프롬프트가 스스로 모순된 예산을 말한다.
`tests/test_ai_prompt_determinism.py` **(t)** 가 네 곳을 함께 고정한다.

`MAX_SUGGESTION_CHARS` 1800 → **2160**(12줄 × 여유 180자). 안 올리면 12줄을 지시해 놓고
10줄어치에서 문장이 잘린 채 저장된다.

캐시는 `AI_COMMENT_SCHEMA_VERSION` 만 v12(전역 bump 금지). ⚠ 이번엔 yaml 을 `/pe/eval`
저장이 아니라 **파일로 직접** 고쳤으므로 `.rules_rev` 가 움직이지 않는다 — 그 카운터에
기대지 말고 이 상수로 갈아야 한다.

### 용어·메타 판단·빈 정보 (2026-09-02, v13)

사용자 지시 3건. 셋 다 "무엇을 **쓰지** 마라" 라, 지시문만으로는 모델이 어길 수 있는
두 건(②③)에는 **deny_patterns 안전망**을 짝으로 뒀다.

| # | 규칙 | 지시문(yaml) | 안전망(deny) |
|---|---|---|---|
| ① | 현장 영어는 영어로 — retest·contact·open·leak·wait 를 "재시험/콘택트/오프리크" 로 옮기지 마라. 반대 변환도 금지 | `keep_english_terms` | — (문체라 정규식으로 못 잡는다) |
| ② | "대표 사례로 선정하기 어렵다" 류 **메타 판단 금지** — 확실하지 않으면 "그런 사례가 있었다" 로 끝낸다 | `no_meta_judgment` | `meta_judgment` |
| ③ | 사례에 근거·조치가 없으면 그 사실을 **설명하지 말고 생략/축약** — "제품명과 LOT 정보만 있어 활용할 수 없다" 금지 | `omit_when_no_info` | `no_info_excuse` |

**⚠ deny 패턴의 어려운 부분은 양성이 아니라 음성이다.** ②③ 둘 다 "사례 + 부정어" 모양이라
조금만 넓게 쓰면 사례를 *활용하는* 문장까지 지운다 — `사례와 달리 …` · `사례에서 확인되지
않은 항목 …` · `유사 사례 2건 있었음` 은 반드시 통과해야 한다. 필터가 이것들을 지우면
없는 것보다 나쁘다. 양성 7 · 음성 7 샘플을 `tests/test_ai_prompt_determinism.py` **(k4)**
가 고정한다.

`no_info_excuse` 는 `only_with_precedents: false` 다 — **선례가 실렸는데 내용이 부실한
경우**가 바로 이 문장이 나오는 상황이라, 선례 유무로 게이트하면 정작 잡아야 할 때 안 걸린다.

캐시는 지시문(프롬프트 합류 → sha 변경) 때문에 `AI_COMMENT_SCHEMA_VERSION` v13.
deny 패턴만 고치는 경우는 프롬프트에 안 들어가 **sha 불변**이라 bump 가 필요 없다
(다음 push 부터 적용).

### 화면 — 태그 라벨이 새던 두 경로 (2026-09-02)

라벨(`[사례]`/`[제안]`)을 화면에서 뺐는데도 그대로 보인다는 재신고가 있었다. 원인은
`renderAiComment` 의 렌더가 아니라 **게이트**였다.

| 경로 | 증상 | 고친 것 |
|---|---|---|
| `[현상]` 이 없는 문자열 | 파싱을 통째로 건너뛰어(`raw.indexOf("[현상]") < 0`) 평문으로 나가 태그가 그대로 보였다 | 게이트를 `AIC_SEC_RE` 로 넓혀 **섹션 토큰이 하나라도 있으면** 분해한다 |
| 사례 0건 | 서버가 `[사례] -` 를 만드는데(`recommend._NO_PRECEDENT_TEXT`) 라벨을 뺀 뒤로 `-` 만 남아 다음 섹션에 붙었다("`-- 조치`") | 내용이 자리표시뿐인 섹션은 통째로 생략(`aicIsPlaceholder`) |

둘 다 **화면에서만** 처리한다 — 서버 문자열·payload·Excel·챗봇·eval export 는 그대로라
캐시 무효화가 없다. 섹션 토큰이 **하나도 없는** 옛 코멘트는 종전대로 평문 폴백이다.
회귀 가드는 `tests/test_webreport_sheets_js.py` **(k)**.
지시문이 갈리면 sha 가 전량 갈려 저장된 [제안]이 폐기되고 클라가 재대행하는데, 그때
payload 셀은 push → `payload_rev` 증가 → 재빌드로 자연히 새 문장으로 교체된다.

### 지시문·금지 문구를 관리자 화면에서 (2026-09-02)

**문제**: 사례가 회수됐는데도 [제안]이 "직접 적용할 수 있는 사례는 확인되지 않았습니다"로
나온다는 신고. 그 문장은 **코드에 없다** — 선례 검색(value_type/family/유사도 0.5/자기 세션
제외/top5)은 정상 회수했고, 프롬프트에도 실렸는데 **LLM 이 스스로 버렸다**. 2026-08-28 에
넣은 "사례를 버리는 문장을 쓰지 마라" 지시는 강제가 아니라 계속 어긴다.

**결정**(사용자): ① 사례를 버리지 말고 **제안을 다듬는 재료**로 쓰고 사실을 왜곡하지 말 것,
② 이런 조건은 앞으로도 계속 생기므로 **코드가 아니라 관리자 모드에서 관리**할 것.

**정본은 `eval_analyzer/eval_engine/rules/ai_prompt.yaml` 한 파일**이고, 편집은
`/pe/eval` → **AI 지시문** 탭이다(thresholds·exclusions 와 같은 저장 인프라 재사용:
검증 → 백업 → 원자적 쓰기 → `.rules_rev` +1 → mtime 캐시로 재기동 없이 반영).

| 목록 | 어디서 쓰나 | 저장 효과 |
|---|---|---|
| `instructions` | 프롬프트 base 지시문 **뒤**에 순서대로 합류 (엔진 `recommend._build_prompt` + 서버 사본 `ai_prompt.build_prompt(rules=…)` **양쪽**) | 프롬프트가 바뀐다 → **sha 전량 갈림 → 저장된 [제안] 폐기 → 재대행**. 화면 안내에 명시 |
| `deny_patterns` | 클라 push 수용 시 [제안]을 **줄 단위**로 거른다 (`service.apply_ai_suggestions`) | 프롬프트 밖이라 **sha 불변** — 이미 저장된 문장은 그대로, 다음 push 부터 적용 |

- 금지 문구는 **줄 단위**다 — 사례를 버리는 한 줄만 빼고 나머지 점검 항목은 살린다.
  한 줄도 안 남으면 저장하지 않고 skip 사유 **`denied`** 로 센다(룰 문장 폴백).
  `raw`(LLM 원문)는 그대로 저장되므로 관리자 문장 검수에서 "서버가 걷어낸 것"이 보인다.
- `only_with_precedents: true`(기본) — **선례가 실제로 프롬프트에 실린 item 에만** 적용한다.
  사례 0건 item 의 "참고할 사례가 없습니다"는 **사실**이라 지우면 그게 왜곡이다. 판정 재료는
  `prompts[item].precedents`(그 프롬프트에 실린 선례 수, `_precedent_count` — `_precedent_lines`
  와 같은 기준). 두 기준이 갈리면 "줬다고 판단해 지웠는데 실은 안 준" 경우가 생긴다.
- ⚠ 정규식은 **사례를 버리는 문장만** 잡아야 한다. `사례와 달리 …` / `사례에서 확인되지 않은
  항목` 같은 **비교문은 통과**시킨다(그게 사례를 활용하는 문장이다).
  회귀는 `tests/test_ai_prompt_determinism.py` (k3) 이 배포 yaml 의 실제 패턴을 읽어
  양성 7·음성 5로 고정한다 — 손으로 베낀 사본을 쓰면 배포 패턴이 틀려도 통과한다(실제로
  "사례는 **직접** 적용할 수 없습니다" 를 놓치던 초안이 이 방식으로 잡혔다).
- 기본 지시가 **코드 배포로 처음 들어가는 것**은 `.rules_rev`(= `/pe/eval` 저장 카운터)가
  감지하지 못하므로 `AI_COMMENT_SCHEMA_VERSION` v6→**v7** 로 갈았다(이후의 화면 편집은
  rev 가 갈아 준다). 전역 bump 는 금지.
- 함께 고친 것: `eval_panel/routes.py` `_audit` 이 `changed_fields` **list** 를 그대로
  `log_audit` 에 넘겨 sqlite 바인딩 오류가 났고 `except: pass` 가 삼켜, **`/pe/eval` 룰 변경이
  감사에 한 건도 남지 않고 있었다**. 문자열로 이어 붙이고 실패는 로그로 남긴다.

> **초기 구현은 `eval_export.precedent_details` 가 `(product_name, human_comment)` 로 eval DB
> 를 되짚는 match-back 이었다**(엔진 동결 전제). 동결 해제 후 엔진 정공법으로 교체하면서
> 그 함수는 제거됐다. 교체 이유는 코드가 줄어서만이 아니라 **회수율** 때문이다 — 개발 DB
> 실측에서 match-back 은 검색된 선례 4건 중 1건만 되짚었고(코멘트 문자열 동일성에 의존),
> 엔진 경로는 4건 전부가 상세를 갖고 온다.

### 재설계 — 코드가 3섹션을 완성하고 LLM 은 덧칠만 (2026-09-02, v9)

**신고**: 재기동·새 세션 뒤에도 사례가 2건 있는데 `[사례]` 에 "…직접 적용할 수 잇는 사례는
확인 되지 않았습니다" 가 나오고, 사례를 눌러도 목록이 안 나온다.

**원인 3개** (전부 코드로 확인):
1. **선례가 프롬프트에 실리지 않는 엔진 버그** — [store.search_precedents](../eval_analyzer/eval_engine/store.py)
   `_rank` 가 대표행을 `(label_id, 코멘트유무)` 순으로 골랐다. 한 case 에는 labeler 가 다른
   라벨이 여러 개 붙는데(`web_report`=코멘트 있음 / `web-signature`·`eval-panel`=코멘트 None,
   보통 **나중에** 들어와 id 가 크다), label_id 가 1순위라 **빈 라벨이 코멘트 라벨을 밀어냈다.**
   그 결과 `[사례 목록]` 이 비고 → LLM 이 "적용할 사례 없음" 을 쓰고 → 금지 문구 게이트
   (`only_with_precedents`)마저 `precedents=0` 이라 **돌지 않았다**. `/pe/eval` L5 선례 표에는
   행이 보이므로 사람 눈에는 "있는데 무시한다" 로만 보였다.
   → `(코멘트유무, label_id)` 로 **순서 교체**. 회귀는
   `test_store.test_search_precedents_comment_label_beats_newer_empty`.
2. **금지 문구가 띄어쓰기 변형을 놓쳤다** — `확인되지\s*않` 은 "확인 **되지** 않았습니다"를
   못 잡는다. → `strip_denied_lines` 가 각 줄을 **원문과 공백 제거본 두 벌로** 검사한다.
3. **[사례] 는 1위 하나만 인용했고 목록 UI 가 없었다** — 2건이어도 1건처럼 보였고,
   "사례 링크" 로 보이던 것은 4줄 클램프 `▾ 더보기` 토글이었다.

**결정**(사용자): ① [사례]는 사례가 있으면 **무조건** 요약(LLM 이 적용 가능성을 판단·부정하지
않는다) ② [제안]은 발화 signature **전부**의 조치 + 사례 통합 ③ **사례가 없으면 LLM 을 아예
거치지 않는다**(토큰·시간 절약).

**구조** — 코드가 먼저 완성하고, LLM 은 있을 때만 덧칠한다(LLM 이 무엇을 쓰든 화면이 틀리지 않는다):

| 섹션 | 코드가 만드는 것(항상) | LLM 이 오면 |
|---|---|---|
| `[현상]` | 발화 signature **전부**의 phenomenon_ko | (안 바뀜, 화면에선 숨김) |
| `[사례]` | 회수된 선례 **전부**의 코멘트 원문 `①(제품/lot) …` | 있는 그대로의 **요약**으로 교체 |
| `[제안]` | 발화 signature **전부**의 action_ko | action_ko+사례 근거+현재 수치의 **통합 제안**으로 교체 |

- LLM 출력은 **두 블록**(`[사례]`/`[제안]`)이고 `parse_llm_blocks` 가 갈라 `patch_cell` 이
  각 섹션을 교체한다. 안 온 블록은 코드 문장이 그대로 남는다(빈 섹션 금지).
  `patch_cell` 은 **멱등**이다(재빌드마다 재병합되므로 필수).
- **선례 0건 → `build_prompt` 가 None** → 그 item 은 `prompts` 에 없고 클라 워커가 보내지
  않는다. 관리자 프롬프트 뷰의 `사례` 열이 그 게이트의 확인 창구다.
- 사례 **건수**는 payload `ai_precedents{row_key: n}`(텍스트 아님 — LLM 이 숫자를 못 바꾼다),
  **상세 목록**은 `GET .../web_report/ai_comment/precedents?key=<row_key>` 로 지연 조회해
  팝오버로 뜬다(`sig_reason.js openAicPrec` — 근거 팝업과 같은 장치 재사용).
  payload 는 건수만 실어 무게를 늘리지 않는다.
- **섹션 토큰이 `[현상]/[사례]/[제안]` 으로 통일**됐다(옛 `[과거사례]`/`[점검제안]` 은 읽기
  호환). 불변 계약이라 CLAUDE.md §5 규칙 12 에 등재 — 파서 사본 4곳을 함께 고칠 것.
- 캐시: `AI_COMMENT_SCHEMA_VERSION` v8→**v9**, payload 쪽은 `_eval_rules_suffix` 에
  **"aiprec"** 영구 표식(ai 세션의 report_key 에만 붙는다). **전역 bump 금지**(규칙 14).

#### 후속 — 모델이 JSON 으로 답하는 형식 이탈 (같은 날, v10)

재기동 후 현장에서 `[제안]` 자리에 이것이 그대로 박혔다:
```
{"precedent":{"use:":false,"selected_id":null,"relevance":"low","summary":null},
 "suggestion":{"text":"Retest를통해 …"},"evidence_refs":["E1"]}
```
**DB 문제도 LLM 실패도 아니다** — 오히려 선례를 정상 회수해 LLM 이 호출됐고, 배치 계약
(`[{id,text}]`)도 지켜졌다. 모델이 그 `text` **안에** 자기 스키마를 또 만든 것이고
(`precedent`/`selected_id`/`evidence_refs` 는 **우리 코드에 없는 이름**이다 — 프롬프트가
두 블록을 요구하니 "구조화해서 답해야 한다" 고 넘겨짚은 부류), 파서는 `[사례]`/`[제안]`
토큰만 찾으므로 토큰 없는 그 덩어리가 통째로 [제안] 본문이 됐다.

**왜 모델이 그랬나 — 지시가 두 군데서 충돌했다**:
- 종전 지시문은 `[사례]`/`[제안]` 을 **소제목처럼** 배치해, 모델이 "지시문의 구조" 로 읽고
  자기가 출력할 토큰으로 여기지 않을 여지가 있었다.
- 바깥 **배치 래퍼**([call_claude/batch.py](../call_claude/batch.py))는 "JSON 배열 하나만
  내라" 를 요구하는데, 안쪽 요청이 "JSON 쓰지 마라" 라고 하면 둘이 정면 충돌한다.

대응 3단:
1. **지시문을 출력 형식 예시로 다시 썼다** — `[사례] <사례 요약 문장들>` / `[제안] <점검
   제안 항목들>` 두 줄을 **그대로 포함해 쓰라**고 못 박고, JSON·코드펜스·인사말을 금지
   (엔진·서버 사본 양쪽). 실제 관측된 응답 모양(토큰이 줄 맨 앞, 본문은 다음 줄, 사이에
   빈 줄)은 `test_ai_prompt_determinism` (r) 이 픽스처로 고정한다.
2. **배치 래퍼에 형식 분리 안내 1줄** — "각 text 값에는 그 요청이 시킨 형식의 답을
   문자열 하나로 담고, text 안에 또 다른 JSON 을 만들지 마라". 도메인 문구는 넣지 않는다
   (`call_claude/` 는 재사용 모듈 — 경계 유지). 회귀는 `test_call_claude` (e).
3. **`unwrap_json_reply`** — `sanitize_suggestion` 이 코드펜스 제거 **뒤**에 부른다.
   JSON 이면 그 안에서 사람 문장(10자 이상)만 꺼내고, 못 꺼내면 `""` 를 돌려 호출부가
   skip → 룰 문장으로 폴백한다(사용자에게 JSON 을 보여 주는 것보다 낫다).
   JSON 이 아니면 원문 그대로 — 정상 경로는 아무 일도 하지 않는다.
   엔진에도 같은 함수 사본이 있다(규칙 #8, 서버 LLM 경로용) — 동치는
   `test_ai_prompt_rules.test_unwrap_json_reply` 가 두 구현을 같은 입력으로 대조한다.

같은 날 **사례 0건 표시도 `-` 한 글자로** 바꿨다(`recommend._NO_PRECEDENT_TEXT`, 사용자
요청) — "참고할 수 있는 과거 사례가 없습니다." 를 매번 읽을 이유가 없다.

#### 화면에서 [사례] 를 접고, "제안 제외" 옵션 (같은 날)

사용자 요청 3건을 반영했다.

**① [사례] 는 제안이 있으면 화면에서 숨긴다** — 사례 내용이 [제안]에 녹아 들어가므로 셀에
두 번 적는 셈이다. 원문은 셀 아래 「📋 사례 N건 상세」에서 본다.
⚠ 조건은 **사례 있음 AND 제안 있음** 둘 다다(`sheets.js` `hideCase`) — 아래 "제안 제외"
세션은 [제안] 섹션 자체가 없어, 사례까지 숨기면 셀이 텅 빈다. 서버 문자열은 그대로다
(Excel·챗봇·eval export 가 같은 평문을 소비 — [현상] 을 숨기는 것과 같은 방식).

**② "제안 제외" 체크(Honey)** — 켜면 LLM 을 **아예 호출하지 않고** 사례만 남긴다.
| 층 | 처리 |
|---|---|
| 클라 UI | `chk_ai_no_suggest`(AI Comment 를 켠 동안만 표시). 켜면 신호등·AI Model 을 숨긴다 — LLM 을 안 쓰는데 모델을 고르게 두면 "골랐는데 왜 안 도나" 가 된다 |
| 클라 옵션 | `options["ai_no_suggest"]=True`, **`ai_model` 은 싣지 않는다** — 실으면 워커가 떠서 받을 프롬프트도 없이 폴링만 하다 실패 보고를 남긴다 |
| 서버 | [validation.webreport_ai_no_suggest](../web_report/validation.py) → `ai_comment.build_ai_comments` 가 `prompts={}`(생성 자체를 생략) + `_cell_text(no_suggest=True)` 가 **[제안] 섹션을 토큰까지 제거** |
| 캐시 | `_ai_no_suggest_suffix` → `ai_comment_key` 에 `"nosugg"` |
- **기본값(끔)이면 옵션 키를 싣지 않는다** — 옵션 원문이 `report_key` 의 원소라 기존 세션
  캐시 키가 바이트 그대로 유지된다(`ai_model`·`eval_sensitivity` 와 같은 규약).
- ⚠ **캐시 꼬리표가 반드시 필요하다**: `ai_comment_key` 는 dedup 이익을 위해 session_id 를
  일부러 뺀다(perf_guard S10). 이 옵션은 `webreport_options` 에만 있고 analysis_key·
  content_hash 에는 없어서, 꼬리표가 없으면 같은 rawdata 를 제안 제외로 올린 세션이
  **먼저 올라간 세션의 [제안] 을 그대로 본다**(`_eval_sensitivity_suffix` 가 막는 것과
  같은 부류의 조용한 오답 — 구현 중 실제로 발견해 고쳤다).
- 검증: [tests/test_ai_no_suggest_option.py](../tests/test_ai_no_suggest_option.py) 가
  리더·클라 배선(소스 검사)·서버 소비·캐시 키 분리를 한 파일에서 고정한다.

**③ ⚙ 아이콘 통일** — AI Comment 옆 민감도 버튼이 `QPushButton("⚙️")` 텍스트라 폰트에 따라
좌측 툴바 Options 톱니바퀴와 다른 모양·크기로 보였다. 둘 다 `_emoji_icon` 픽스맵을 쓰도록
바꿔 같은 그림이 된다.

계약상 중요한 점:
- **종전 선례 5키(action/result/comment/product_name/family_product)는 이름·의미 불변**이다 —
  추가만 했다. 기존 소비자(`tools/testbench_eval.py`, 엔진 테스트)는 그대로 동작한다.
- **`ai_prompt.py` 는 순수 함수를 유지한다** — DB 를 열지 않는다. 규칙 #8 이 eval_engine
  import 를 3파일로 고정하므로 이 파일은 엔진을 부를 수도 없다. 현재 쪽 재료만
  `ai_comment._prompt_enrich` 가 `enrich` dict 로 주입한다(`enrich=None` 이면 통계 줄 생략).
- **상세가 없어도 무해** — 전부 LEFT JOIN 이라 통계 없는 선례(CSV 적재분)는 식별+코멘트만
  나가고, 옛 계약 dict(캐시에 굳은 값)는 종전 한 줄 형태로 떨어진다(`_has_detail` 분기).
- **수치 포맷은 `%.6g` 고정** (`_fmt`). 4자리로 줄이면 `1.0000123 → "1"` 처럼 뭉개져 과거/
  현재 대비가 "같은 값"으로 보인다. 출력 순서도 상수(`_PRECEDENT_METRIC_ORDER` 등)로 고정
  한다 — dict 순서에 기대면 프롬프트가 흔들려 sha 게이트가 매번 갈린다.
- **선례 dedup 대표행 선택이 run 수에 오염되지 않게** `raw_metrics`/`features` JOIN 은
  `run_id = (SELECT MAX(run_id) …)` 로 **case 당 1행**만 붙인다. 이 조건이 빠지면 같은 case
  가 run 수만큼 복제돼 `(제품, lot, item)` dedup 이 엉뚱한 행을 대표로 고른다.
- 선례 개수는 엔진 `EVAL_PRECEDENT_TOPK`(기본 5) 그대로. 상세가 붙어도 프롬프트는 수 KB 다.

프롬프트 최종 형태(선례 상세가 붙은 경우):

```
<_INSTRUCTION>            ← recommend._build_prompt 원문 사본 (바이트 불변)
<_INSTRUCTION_EXTRA>      ← 이 프로젝트 확장
item: <canonical> / class: <class> / unit: V / LSL=0.5 / USL=1.5
status: MAJOR / primary: LOW_CPK
secondary: ...
[현재 통계] cpk=0.42, mean=1.01, yield=0.98, fail_count=30, total_count=1500
[발화 signature 전체] 2건 - 아래 2개 항목을 모두 다뤄라   ← 건수는 _sig_count (0건이면 표기 없음)
- LOW_CPK(primary): <action_ko> [근거: CPK=0.42]
- OUTLIER(secondary): <action_ko> [근거: fail_mad_min=6.1]
[현상] ...
[과거사례 목록]
- 사례1 / 제품 P1 / lot L1 / item itema / unit V / 당시 status MAJOR / 당시 signature LOW_CPK
  당시 통계: cpk=0.370006, mean=6.25, stdev=3.37832, yield=1, fail_count=0, total_count=24, ...
  당시 분포/공간: outlier_ratio=0.125, bimodality_score=0.97958, value_gap_ratio=1
  당시 판단·조치(원문): [PTE] CT 확인 필요        ← truncate 없음
참고용 기본 조치(action_ko):...
```

검증은 `tests/test_ai_prompt_determinism.py` (i)(j) — enrich 결정성·상세 유무 분기(통계
없는 선례·옛 계약 dict 는 한 줄), 임시 sqlite eval DB 로 **엔진 계약**(`search_precedents`
+`_precedent_result` 가 최신 run 기준 상세를 싣는지). 엔진을 고쳤으므로
`cd eval_analyzer && python -m pytest -q`(242 통과)도 함께 돌린다.
| [web_report/edits.py](../web_report/edits.py) | `KIND_AI_SUGGEST` + `_STATE_EXCLUDED_KINDS` 등재 (DB 계층 무수정 — 미등재 kind = payload_rev 자동 +1) |
| [web_report/validation.py](../web_report/validation.py) | `webreport_ai_model(opts_raw)` — 화이트리스트 {default,claude}, 미지값 폴백 |
| [web_report/service.py](../web_report/service.py) | `_merge_ai_suggestions`(빌드 성공 시 cache_put 직전 재병합 — 재빌드 생존 지점) · `get_ai_comment_prompts`(대상 아님 None→404 / 미스 `request_build("ai")`+202 / 히트 items) · `apply_ai_suggestions`(검증→sha 게이트→keyed_lock 안 save_merge→RAM+디스크 aicmt 패치→marker rev+1→report 선빌드) · `_AI_RESULT_KEYS` 에 prompts |
| [web_report/rawedit.py](../web_report/rawedit.py) | `replace_sources` 에서 `ai_suggest_store.delete_stale` (dist_pack 과 같은 자리) |
| [server/report/routes_webreport.py](../server/report/routes_webreport.py) | `GET .../web_report/ai_comment/prompts` + `POST .../ai_comment/suggestions` — `X-Honey-Agent: 1` 아니면 403 + `_editor_guard` + POST 2MB + 감사(`ai_suggest(accepted,skipped)`) |

### 클라(Honey)

| 파일 | 내용 |
|---|---|
| [client/honey_main.py](../client/honey_main.py) | ① `_build_controls_panel` 의 AI Comment 행 아래 **AI Model 콤보**(`cbo_ai_model`, chk 연동 활성화, `app_settings("ai_model")` 로 **모델 선택만 영속** — on/off 비영속 유지) ② 옵션 주입: `ai_on && claude` 일 때만 `options["ai_model"]="claude"` (**default 는 키 미탑재** — report_key 바이트 보존) ③ 업로드 성공 직후 `ai_suggest.start_background(..., on_progress=self._ai_suggest_progress)` (try/except — 업로드 흐름 불침해) ④ **Claude 연결 신호등**(`lbl_ai_health` + `_check_ai_health` — 아래 절) ⑤ `_ai_suggest_progress` — 워커 알림을 UI 스레드로 넘겨 실행 로그·상태바에 표시 |
| [client/transport/ai_suggest.py](../client/transport/ai_suggest.py) (신규) | daemon 워커: prompts 폴링(3s 간격·상한 300s, 202 재시도, 403/404 ×3 후 조용히 포기=구서버 호환) → `call_claude.run_batch` 배치 10건씩 **순차**(병렬 없음 — 업로더 PC 부하 억제, 전체 상한 20분) → POST push(202 는 10s 후 1회 재시도 — merge 멱등). 모든 예외 삼킴. **위치가 구 설계의 report_flow 가 아니라 transport 인 이유**: 🟢 자유수정 + 모듈의 일이 "서버와 말하기"라 transport 역할 정의와 일치. **`on_progress` 콜백**(2026-09-01)으로 진행·실패를 사용자 실행 로그에 남긴다(위 §사용자에게 보이는 알림) · `http_hint` 가 상태 코드를 조치 문장으로 |
| [client/transport/config.py](../client/transport/config.py) | `env_value(name)` — PC env > honey.env 공용 헬퍼 |
| [client/build_honey.spec](../client/build_honey.spec) | `collect_submodules('call_claude')` (조건부 import 정적 분석 보강 — [LIB_HANDOFF.md](../client/LIB_HANDOFF.md) 이력 기재) |

honey.env 선택 키(전부 없어도 동작): `HONEY_CLAUDE_BIN` /
**`HONEY_CLAUDE_MODEL`(기본 `claude-sonnet-5`)** / `HONEY_CLAUDE_TIMEOUT`(240) /
`HONEY_CLAUDE_BATCH`(10) / `HONEY_CLAUDE_MAX_ITEMS`(50).

⚠ 기본 모델은 별칭(`sonnet`)이 아니라 **정식명**이다. CLI 는 둘 다 받지만(2026-08-28
2.1.247 실호출 확인), 별칭은 "최신 sonnet"을 가리켜 새 버전이 나오면 말없이 바뀐다 —
이 값은 프롬프트 sha 에 안 들어가 캐시로도 안 걸리므로, 같은 세션이 어제와 다른 모델로
생성될 수 있다.

### Claude 연결 신호등 (Honey UI, 2026-08-28)

AI Comment 체크를 **켜는 순간** ⚙ 옆에 신호등(●)이 뜬다 — 회색=확인 중, 초록=사용 가능,
빨강=사용 불가. 클릭하면 재확인하고, 마우스를 올리면 사유가 보인다.

판정은 [ai_suggest.check_status](../client/transport/ai_suggest.py) 가 하며 **실호출
1회**(`"ok 라고만 답하라."`)로 정한다. `probe`(--version/--help)만 보지 않는 이유:
바이너리만 있으면 초록이 켜져 **인증·게이트웨이·정책 실패를 못 잡는다** — 현장에서 정작
알고 싶은 것이 그 인증 여부다. 실패 시 detail 에 call_claude 단계 로그가 실린다.
UI 스레드를 막지 않도록 워커 스레드에서 돌리고 결과만 `QTimer.singleShot(0, ...)` 으로
되돌린다(수 초 소요). 상한은 probe 20초 / 실호출 45초 — 워커의 240초를 쓰면 사용자가
버튼을 누르고 하염없이 기다리게 된다.

### ⚠ prompts 폴링이 'ai' 잡을 사용자보다 **먼저** 건다 (2026-09-02)

업로드 성공 직후 워커가 `GET .../ai_comment/prompts` 를 치고, 서버
[`get_ai_comment_prompts`](../web_report/service.py) 는 캐시 미스면
`compute.request_build(sid, "ai")` 를 걸고 202 를 준다. 이 시점은 **업로더가 세션을
클릭하기 전**이라, 'ai' 잡이 항상 사용자 조회보다 앞선다.

종전에는 그 잡이 곧 `report_job(ai_inline=True)` 여서 부모가 report 락을 엔진 평가 내내
(실측 100초+) 쥐었고, 뒤이은 사용자 조회의 1초짜리 pending 빌드가 그 락 뒤에 줄을 섰다 —
"AI Model 을 claude 로 바꾼 뒤 첫 조회가 100초" 로 관찰됐지만 원인은 대행 기능이 아니라
**잡 구조**였다(`default` 여도 프리웜·다른 사용자 조회가 같은 잡을 건다).
지금은 그 잡이 2단계(분리 캐시 채우기 → 짧은 재빌드)라 **먼저 걸리는 것이 오히려 이득**
이다 — 사용자가 열기 전에 평가가 진행돼 있다. 되돌리지 말 것(CLAUDE.md 규칙 17,
perf_guard `S15`).

### 행 단위 Loading · 처리 주체 아이콘 · 배치 병렬 (2026-09-02 개편)

사용자 요구 6건을 한 번에 반영했다. 앞의 §핵심 설계 결정 ①(세션 고유 저장)이 그 토대다.

| # | 요구 | 구현 |
|---|---|---|
| 1 | 형제의 옛 문장 재사용 금지 · 새 세션은 "Loading 중…" | 저장을 세션 편집 DB 로(§①) + payload `ai_llm_pending`{row_key:1} |
| 2 | 사례 0건 셀은 즉시 `action_ko` | **자동 충족** — `build_prompt` 가 선례 0건이면 None → prompts 에 없음 → pending 대상 아님 |
| 3 | Signature / 사례없음 / 사례있음 병렬 | 'ai' 잡 1회가 Signature+action_ko 를 **같은 순간** 완성(서버 LLM off 면 L5 는 룰 템플릿 조립이라 비용 ≈0)하고, 사례 있는 셀만 push 로 점진 완성 |
| 4 | 10분 → 1~2분 | 클라 배치 **4 병렬**(`HONEY_CLAUDE_PARALLEL`, 상한 5) + **배치 완료 즉시 push** + 배치별 진행 알림 |
| 5 | 처리 주체 아이콘 | payload `ai_sources`{row_key: claude\|llm\|rule} → `sheets.js aicSrcIconHtml`(✴/🤖/⚙) |
| 6 | 세션 고유 평문 저장 | §핵심 설계 결정 ① |

- **`ai_llm_pending` 인 행은 엔진 [제안]을 그대로 보여 주고 그 아래에 대기 줄을 붙인다**
  (`.aic-wait-line`, 2026-09-02 사용자 결정). 문구는 **"Loading… (Claude)"** — 그냥
  "Loading" 이면 사용자는 서버가 느린 것으로 읽지만 실제로는 그 PC 의 Claude CLI 가 돈다.
  Claude 문장이 도착하면 서버가 [제안] 섹션을 교체해 내려주고 그 행이 pending 맵에서
  빠지면서 대기 줄도 사라진다.
  > **가리는 안을 쓰지 않는 이유**: 대기 중 본문을 감추면 그 칸이 정보 0 인 채로 몇 분간
  > 남는다. 지금 읽을 것(룰·서버 LLM 문장)을 주고 "더 나은 문장이 오는 중"을 함께 알리는
  > 편이 낫다는 판단이다.
  ⚠ **`hideCase` 는 대기 여부와 무관하다** — 제안이 있으면 [사례]를 숨기는 것이 기본이고
  (원문은 상세 링크로 본다), 서버 LLM 문장이든 Claude 문장이든 화면 규칙은 같다.
  2026-09-02 에 여기에 `&& !llmWait` 를 달았다가, Claude 대행 세션에서 서버 LLM 문장이
  나오는 동안 [사례]가 통째로 노출되는 회귀를 냈다(사용자 신고). 다시 달지 말 것.
- **재렌더는 페이지 스크롤을 보존한다**(`boot.js` 폴링 tick). Claude 문장이 배치로 도착해
  세션당 여러 번 다시 그리는데, 그때 맨 위로 튀면 아래쪽 데이터를 보던 사용자가 반복해서
  자리를 잃는다. `window.scrollTo` 를 렌더 직후 + `requestAnimationFrame` 에서 두 번
  되돌린다(표 높이가 나중에 확정되는 경우 대비). 가드: `tests/test_ai_poll_js.py` (e).
  섹션 토큰이 없는 셀·옛 평문 코멘트에도 대기 줄이 붙는다(그 칸만 조용하면 안 된다).
- ⚠ **TTL 이중 방어**(`AI_LLM_PENDING_TTL_SEC`, 기본 3600초 — 서버 생성 시 + 프런트 렌더 시).
  워커가 영영 push 하지 않는 PC(Honey 종료·CLI 인증 실패)가 있으면, 이게 없을 때 그 세션은
  **방문할 때마다** 20분짜리 Loading 을 보여 준다. 만료 후에는 코드가 만든 action_ko 가
  최종본이다. `boot.js` 의 폴링 포기(`__aiLlmFailed`)도 같은 폴백을 즉시 적용한다.
- **`boot.js` 폴링이 배치별 push 를 따라간다** — `ai_llm_pending` 이 **줄어들 때마다**
  DATA 를 갈아 다시 그리고, 남아 있으면 계속 폰다(종전엔 pending 플래그가 풀릴 때 한 번만).
  입력 중이면 3초 뒤로 미룬다(불변 규칙 #12).
  ⚠ 재렌더 조건은 **"이번에 실제로 변한 것이 있을 때"** 다: ① 평가 pending 이 이번 tick 에
  풀렸거나(`done && wasPending`) ② 대기 행 수가 줄었을 때. `done` 만으로 그리면 최종본
  세션이 5초마다 전체 재렌더를 돌아 스크롤·팝오버가 튄다. `lastLlmPending` 기준선은
  재렌더 여부와 **무관하게** 매 tick 갱신한다 — 안 그러면 서버가 pending 을 다시 만든
  경우 계속 "줄었다"로 오판한다. 회귀 가드: `tests/test_ai_poll_js.py`.
- **아이콘 판정은 선례 유무로 가른다** (2026-09-02 정정). 엔진은 선례가 1건도 없으면
  LLM 을 **아예 호출하지 않는다**(`recommend.make_comment` 의 `has_precedent_comments`
  게이트) — 그 행의 문장은 룰 조립(action_ko)이다. 그래서:
  | 행 | 아이콘 |
  |---|---|
  | 세션 저장 행 있음(클라 push 수용됨) | `claude` ✴ |
  | 프롬프트 있음(선례≥1) + 대행 대상 아님/TTL 경과 | 서버 LLM 켜짐이면 `llm` 🤖, 아니면 `rule` ⚙ |
  | **프롬프트 없음(선례 0건)** | 항상 `rule` ⚙ |
  ⚠ 종전에는 `llm_enabled` 만 보고 **전 행**에 `llm` 을 줘서, LLM 을 거치지도 않은 칸이
  🤖 로 보였다. `claude` 는 저장 행이 곧 증거라 정확하고, `llm` 은 여전히 배선 기준
  근사다(엔진이 case 별 LLM 사용 여부를 돌려주지 않는다).
- **셀 하단은 한 줄이다**(`.aic-foot`, 2026-09-02 사용자 요청) — 왼쪽에 「📋 사례 N건 상세」,
  오른쪽 끝에 처리 주체 아이콘, 그 바로 왼쪽에 "Loading… (Claude)". 종전엔 셋이 각자
  블록이라 세 줄을 먹고 아이콘만 `float:right` 로 떠 있었다.
- **사례 팝오버의 각 사례에 "세션 열기 ↗" 링크**가 붙는다 — 그 코멘트가 저장됐던 세션으로
  새 탭으로 간다(보던 표를 잃지 않게). 재료는 선례 행의 `session_id` 이고, 엔진이 계약
  dict 에 담아 준다(`store.search_precedents` → `present._precedent_result`).
  ⚠ 서버 필터 **`ai_comment._PREC_VIEW_KEYS` 에 `session_id` 가 있어야 한다** — 2026-09-02
  에 실제로 빠져 있어, 화면 코드(`sig_reason.js aicPrecRowHtml`)는 링크를 그릴 준비가
  돼 있는데 값이 안 와서 안 떴다. `session_id` 가 없는 선례(CSV 적재분)는 링크 없이
  나머지만 나온다. 캐시는 `AI_COMMENT_SCHEMA_VERSION` v15.
  검증: `tests/test_ai_suggest.py` (o) · `tests/test_webreport_sheets_js.py` [m].
- 캐시: `AI_COMMENT_SCHEMA_VERSION` v13→**v14**(옛 캐시에 형제 문장이 구워져 있어 반드시
  갈아야 한다 = 사용자 결정 "기존 문장 전부 초기화"의 실행 수단) + `_eval_rules_suffix` 에
  영구 표식 **"aisess"**(payload 신규 키 2종). 전역 bump 금지(규칙 14).
- **기존 공유 문장은 전부 초기화**(사용자 결정) — 서버가 옛 파일을 읽지 않으므로 전 세션이
  action_ko 로 복귀하고, 재대행된 세션부터 세션 고유 문장이 붙는다. 파일 물리 삭제는
  롤백 창구로 남겨 두었다가 안정화 후 정리한다.

검증: `tests/test_ai_suggest.py` (b2)(p) · `tests/test_webreport_sheets_js.py` (l).

## 운영 모니터링 — 어디서 무엇을 보나 (2026-08-28 신설)

**이 기능은 실패해도 화면에 에러가 뜨지 않는다** — 룰 폴백 문장이 그대로 나가므로
사용자도 관리자도 모른 채 지나간다. 그래서 현황을 볼 화면을 따로 만들었다.

**`/pe/admin-<secret>/` → `✨ AI Comment` 탭** (구현
[admin_panel/ai_comment_admin.py](../server/admin_panel/ai_comment_admin.py) ·
라우트 `GET /api/ai_comment`, `GET /api/ai_comment/session/<sid>`).
**새 DB 테이블 없음 — 조회 시점에 세 데이터원을 합친다**:

| 카드 | 데이터원 | 무엇을 답하나 |
|---|---|---|
| 대상 세션 / 문장 반영 | `report_session.webreport_options`(대상 판정) + `report_webreport_edit` 의 push marker(`kind=ai_suggest, item_key=push`) | claude 로 올린 세션 중 몇 개가 실제로 문장을 받았나 |
| 최근 N일 push | 감사 로그 `action='ai_suggest'` 의 `changed_fields` 파싱 | 서버가 무엇을 수용/스킵했나 (형식 불명은 `unparsed` 로 드러낸다) |
| 클라이언트 실패 | 진단 사건 `component=honey` + `event` 가 `ai_suggest` 로 시작 | **생성이 안 된 이유** — 아래 5종 |
| 저장된 문장 | `ai_suggest_store` 파일(세션 1개만 읽음) | 실제 문장 품질 + `stale`(룰이 바뀌어 폐기 중인지) + **LLM 원문**(`raw` — 있으면 sanitize 가 뭔가 걷어냈다는 뜻) |

### 흐름 디버깅 3종 (2026-09-01 신설)

통계만으로는 `[제안]` 이 이상할 때 **프롬프트가 나빴나 / 모델이 이상했나 / 서버가
걸렀나** 를 가를 수 없었다 — 3단계 중 첫 단계(프롬프트)가 아예 안 보였다. 새 저장소
없이 기존 데이터원 조합으로 셋을 추가한다:

| 화면 | 라우트 | 무엇을 답하나 |
|---|---|---|
| **흐름 타임라인** | `GET /api/ai_comment/session/<sid>/timeline` | 업로드→클라 실패→push 를 시간순 한 줄씩. 맨 위 한 문장이 결론(정상 / 클라 실패 / **워커가 아예 안 돎**). "실패도 push 도 없음" 이 종전에는 못 읽던 상태다 |
| **LLM 이 받은 프롬프트** | `GET /api/ai_comment/session/<sid>/prompts` | 서버가 조립한 본문 전문 + sha + 길이. 재료가 충분한지 눈으로 검증 |
| **skip 사유** | 기존 `/api/ai_comment` 의 push 표 | `skipped=3` 숫자를 6종(`sha_mismatch`/`empty`/`unknown_item`/`badsha`/`badrow`/**`denied`**=금지 문구로 폐기)으로 분해 |

- 두 신규 라우트 모두 **`allow_build=False`** — 관리자 조회가 콜드 빌드를 유발하면 안
  된다. 캐시가 없으면 안내 문장만 돌려준다(빈 화면 금지).
- skip 사유는 감사 `changed_fields` 에 `ai_suggest(accepted=N,skipped=M) [sha_mismatch=1 …]`
  로 실린다. **접두는 `_AUDIT_RE` 파싱 대상이라 바이트 불변**이고 내역은 뒤에 덧붙인
  확장이라, 옛 기록도 그대로 파싱된다.

### 클라 실패 kind 5종과 대응

클라 워커가 `transport/error_report.report_error` 로 보낸다(기존 Honey 오류 보고 경로
재사용 — 신규 라우트·테이블 없음). **성공은 보고하지 않는다**(서버가 push 로 이미 안다).

| kind | 뜻 | 먼저 볼 것 |
|---|---|---|
| `ai_suggest_no_cli` | claude 실행 파일을 못 찾음 | 그 PC 의 설치 경로 · `HONEY_CLAUDE_BIN` 배포 |
| `ai_suggest_no_prompts` | 서버에서 프롬프트를 못 받음(`denied`=구서버/대상아님/권한, `timeout`) | 서버 버전 · 세션 옵션 · 편집 권한 |
| `ai_suggest_empty` | **CLI 는 찾았는데 한 건도 생성 못 함** | **현장 인증·정책 실패의 1순위 신호.** context 의 `cli_log`(call_claude 가 남긴 `not_found/timeout/exit/parse/output`)를 본다 |
| `ai_suggest_push_failed` | 생성은 됐는데 서버 저장 실패 | `status`(403=신원/권한, 404=대상아님, 5xx=서버) |
| `ai_suggest_worker_error` | 워커 예외 | `error_type` |

⚠ **`call_claude/` 는 이 배선에 관여하지 않는다** — 그 패키지는 다른 프로젝트에도 붙는
범용 모듈이라 서버 보고를 모른다. `log` 콜백만 넘겨주고, 그 로그를 워커가 모아
실패 사건 context 에 싣는다. 경계를 되돌리지 말 것.

### 사용자에게 보이는 알림 (2026-09-01)

위 진단 사건은 **관리자용**이다. 정작 업로드한 사용자는 종전에 아무것도 못 봤다 —
워커가 모든 실패를 삼키고, 화면에는 룰 폴백 문장이 정상처럼 나오기 때문에 "AI 문장이
안 왔다"는 사실조차 인지하지 못했다. 그래서 워커에 `on_progress` 콜백을 추가해
**Honey 실행 로그 + 상태바**에 한 줄씩 남긴다:

- 진행: `서버 평가를 기다리는 중…`(202 가 길어질 때 1회만) / `N개 항목 문장 생성 중…`
- 성공: `대행 완료: N건 반영됨 (리포트 화면을 새로고침하면 보입니다)`
  — push 는 payload_rev 만 올려 열려 있는 화면이 자동 갱신되지 않으므로 **다음 행동까지** 적는다.
- 실패: 사유 + **무엇을 확인할지**. HTTP 상태는 숫자만 주지 않고
  [`ai_suggest.http_hint`](../client/transport/ai_suggest.py) 가 조치 문장으로 바꾼다
  (403=권한, 404=서버 버전/대상 아님, 423=세션 잠김, 5xx=서버, 0=네트워크).

⚠ 콜백은 **워커 스레드에서 불린다** — 워커는 위젯을 만지지 않는다는 규약을 지키려고
문자열만 넘기고, `honey_main._ai_suggest_progress` 가 `QTimer.singleShot(0, …)` 으로
UI 스레드에 넘긴다(`_check_ai_health` 와 같은 패턴). 콜백 실패는 전부 무음이다.

## 반드시 지킬 것 (위반 시 리뷰 반려)

1. 코멘트 평문 3섹션 형식 바이트 단위 불변 — sheets.js/Excel/챗봇/eval_export 가 같은
   평문을 소비한다(CLAUDE.md 규칙 12). 파서는 옛 토큰 `[점검제안]` 도 받아야 한다.
2. `ai_comment_key` 에 session_id/provider/LLM 축 추가 금지(perf_guard S10·S12).
   suggestion 은 데이터 세대(akey+chash+mode+prep)의 파생물 — dedup 형제 공유가 의도.
3. eval_engine import 는 기존 3파일 밖으로 늘리지 않는다(규칙 #8) — `ai_prompt.py` 가
   엔진을 부르지 않는 이유다. 엔진 **코드 자체는 자유 수정**이지만 eval.db **DDL 변경은
   사전 승인** 대상이다. 클라에 eval_analyzer 패키징 금지.
4. 서버는 자기가 만든 `prompts` 의 item+sha 일치 건만 수용(임의 row_key 제출 불가).
   불일치·불합격은 409 가 아니라 조용히 skip+카운트.
5. 자격증명을 report 서버로 절대 전송하지 않는다 — 서버와는 프롬프트/문장만 오간다.
   (CLI 대행에서는 토큰 취급 자체가 없다 — claude CLI 가 인증을 안고 있다.)
6. 서버 `EVAL_LLM_*` 는 계속 미설정 유지(켜면 이중 생성 + 2단계 sig 분리 경로까지
   활성화된다 — service.`_ai_two_stage_wanted`).
7. 신구 호환: 구 Honey → `ai_model` 키 없음 → default → 서버 동작 불변. 신 Honey + 구
   서버 → prompts 404 조용히 포기. `default` 면 options dict 에 키를 넣지 않는다.
8. `web_report/`·`server/report/` 쓰기는 perf_guard 훅이 검사한다 — 막히면 우회하지
   말고 설계를 재검토하라(docs/18).

## 검증 (완료분 — self-run 관례)

| 테스트 | 내용 | 상태 |
|---|---|---|
| [tests/test_call_claude.py](../tests/test_call_claude.py) | 스텁 CLI 주입 — 한글 round-trip/argv·stdin 격리/배치 변형 4종/실패 5종/find_cli/probe 게이팅/무예외 + **`--json-schema` 자동 게이팅**(배치 전용 부착·단건 미부착·미지원 폴백) | ✅ 통과 (9항목) |
| [tests/test_ai_prompt_determinism.py](../tests/test_ai_prompt_determinism.py) | 결정성/신구 토큰/action_ko 우선순위/재료부족 None/**지시문 vendor copy ast 대조**/sanitize/patch/apply | ✅ 통과 |
| [tests/test_ai_suggest.py](../tests/test_ai_suggest.py) | store round-trip·delete_stale·**raw 보존**(동일=미저장/상한/옛 클라) / 옵션 폴백 / 라우트 403·비편집자·404 / 202→200 / sha 게이트·rev bump·**skip 사유 5종 분해** / **/full 병합 반영** / **재빌드 생존** / **룰 변경 sha 폴백** / **클라 워커 동기 시뮬레이션**(폴링→생성→push→반영 전체) + **사용자 알림**(성공·실패 문장) / **http_hint** | ✅ 통과 (12항목) |
| 실 claude 스모크 (개발 PC, 개인 계정) | probe ok + 배치 2건 3.2초 2/2 수신 · **`claude-sonnet-5`·`sonnet` 둘 다 실호출 확인** · `check_status()` 정상 2.6초 초록 / CLI 없음 즉시 빨강 | ✅ 완료 |
| [tests/test_ai_comment_admin.py](../tests/test_ai_comment_admin.py) | 관리자 모니터링 — 커버리지 분류 / **"비었다"의 사유 3분기** / push 파싱·형식불명 / 클라 실패 필터·kind 집계 / 문장 검수 / 구성요소 격리 / 라우트 401·200·404 / **화면 정합(탭·패널·로더·DOM id)** + **디버깅 3종**(프롬프트 4분기·타임라인 정렬/KeyError·skip 사유 신구 형식) | ✅ 통과 |
| 기존 회귀 | tests/test_ai_comment_modality.py (9 passed — `_to_row_keys` 불변) | ✅ 통과 |

⚠ 진단 사건을 쓰는 테스트는 **`REPORT_DIAG_DIR` 을 임시 폴더로 격리**해야 한다 —
안 하면 테스트 사건이 운영 `server/log/diagnostic_*.log` 에 섞여 조회를 못 믿게 되고,
재실행마다 누적돼 건수 assert 가 깨진다(2026-08-28 실제로 겪음).

서버 반영은 terminate.bat → start.bat (`.claude/skills/server-restart`). aicmt v4 는
ai 옵션 세션 한정 재평가라 전역 프리웜 불필요.

## 현장 검증 항목 (외부 담당자 — 개발 환경에서 확인 불가)

정본 체크리스트는 [call_claude/README.md §7](../call_claude/README.md) (8항목):
현장 CLI 경로/버전 probe → **Enterprise gateway 인증 하 `claude -p` 실호출** → 충돌 env
(`ANTHROPIC_API_KEY` 등) 조사 → 구버전 플래그 폴백 → 배치 10건 실측·타임아웃 조정 →
조직 정책(managed settings) → **e2e**(AI Model=claude 업로드 → 클라 로그 → `[제안]` LLM
문장 확인 → `/pe/eval` 룰 저장 후 sha 폴백 확인 → default 업로드 종전 동일 대조) →
(선택) `--json-schema` 강화.

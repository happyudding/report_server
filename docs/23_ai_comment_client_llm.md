# 23. AI Comment [제안] — 클라 로컬 Claude CLI 대행

> **상태**: **서버·클라·call_claude 구현 완료 (2026-08-28)** — 개발 환경에서 검증 가능한
> 전부를 검증했고, **남은 것은 현장(Enterprise gateway) 검증뿐**이다
> (→ §현장 검증 항목, [call_claude/README.md §7](../call_claude/README.md)).
> **2026-09-01 보강**: 배치 구조화 출력(`--json-schema` 자동 게이팅) · 지시문 확장
> (근거 없는 줄 채우기 차단, `AI_COMMENT_SCHEMA_VERSION` v6) · 관리자 흐름 디버깅 3종
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

1. **suggestion 은 캐시가 아니라 영구 오버레이.**
   [web_report/ai_suggest_store.py](../web_report/ai_suggest_store.py)
   (`<upload_root>/web_report/<akey>/ai_suggest/<chash12>_<mode>[_p<dig8>].json` —
   dist_pack_store 패턴, 축출 제외)에 저장하고, aicmt 캐시가 채워질 때마다(콜드 재빌드
   포함) `service._merge_ai_suggestions` 가 재병합한다. `ai_comment_key` 에 LLM/세션 축을
   추가하지 않는다(perf_guard S10·S12).
2. **프롬프트 sha 게이트.** `sha256(prompt)[:12]` 를 suggestion 과 함께 저장, 재병합은
   현재 프롬프트 sha 일치 시에만. 룰 변경으로 판정이 바뀌면 sha 가 갈려 자동으로
   action_ko 폴백(옛 LLM 문장이 새 판정에 붙는 사고 차단).
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
| [web_report/ai_prompt.py](../web_report/ai_prompt.py) (신규) | case dict → 프롬프트 재구성(`build_prompt(case, enrich)`/`build_prompts`, 키는 **item_raw**) · `prompt_sha` · `split_comment`(신 `[제안]`/구 `[점검제안]` 둘 다) · `sanitize_suggestion`(개행 보존 — 엔진 프롬프트가 '- ' 항목 형식 요구, 1000자 상한) · `patch_suggestion_text`(마지막 섹션만 치환, `[MAJOR][이봉]` 접두·앞 2섹션 바이트 보존) · `apply_suggestions`(sha 게이트 + `key.endswith("\|"+item)` fan-out, **항상 copy**) |
| [web_report/ai_suggest_store.py](../web_report/ai_suggest_store.py) (신규) | 영구 저장 `load/save_merge(tmp pid→os.replace)/delete_stale` · 상한 500 · akey 안(세션 삭제 시 정리) |
| [web_report/ai_comment.py](../web_report/ai_comment.py) | `build_ai_comments` 반환에 `prompts` 부착 · `_EMPTY_RESULT` 에 `"prompts": {}` (eval import 무변경 — 규칙 #8) · `_prompt_enrich` 가 **현재 케이스** 재료를 조립 (§선례 상세 보강) |
| [eval_analyzer/eval_engine/store.py](../eval_analyzer/eval_engine/store.py) | `search_precedents` 가 선례 행에 **당시 수치**(최신 run 의 raw_metrics/features + unit/status)를 함께 싣는다 (§선례 상세 보강). DDL 무변경 — SELECT 확장뿐 |
| [eval_analyzer/…/present.py](../eval_analyzer/eval_engine/pipeline/present.py) | `_precedent_result` — 선례 계약 dict(종전 5키 + 식별·당시 수치) |
| [web_report/cache_policy.py](../web_report/cache_policy.py) | `AI_COMMENT_SCHEMA_VERSION = 6` (v4 prompts 키 추가 → v5 프롬프트 본문 확장 → **v6 지시문 확장** — ai 옵션 세션만 재평가). `ai_comment_key` 구성 불변 |
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

> **초기 구현은 `eval_export.precedent_details` 가 `(product_name, human_comment)` 로 eval DB
> 를 되짚는 match-back 이었다**(엔진 동결 전제). 동결 해제 후 엔진 정공법으로 교체하면서
> 그 함수는 제거됐다. 교체 이유는 코드가 줄어서만이 아니라 **회수율** 때문이다 — 개발 DB
> 실측에서 match-back 은 검색된 선례 4건 중 1건만 되짚었고(코멘트 문자열 동일성에 의존),
> 엔진 경로는 4건 전부가 상세를 갖고 온다.

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
[발화 signature 전체]
- LOW_CPK(primary): <action_ko> [근거: CPK=0.42]
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

### (선택·미적용) boot.js 재폴링

push 반영은 payload_rev bump 로 다음 조회에 자연 반영된다. 열려 있는 화면의 자동
재렌더(`ai_suggest` 재폴링, 상한 90초)는 후속 과제 — 미적용 시에도 새로고침으로 반영.

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
| **skip 사유** | 기존 `/api/ai_comment` 의 push 표 | `skipped=3` 숫자를 5종(`sha_mismatch`/`empty`/`unknown_item`/`badsha`/`badrow`)으로 분해 |

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

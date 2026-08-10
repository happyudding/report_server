# server/chatbot — ENGR 이력 검색 챗봇 (2단계: 조회 툴 + CLI + 웹 위젯)

반도체 ENGR 이 자연어로 과거 평가 이력을 찾게 한다. 목표 질문:

- "PMIC SOC family 에 SGM 들어가는 항목, 예전에 어떻게 됐었지?"
- "S3222 평가 보고서에서 LDO item 이슈 어떻게 close 됐지?"
- "이 세션 수율/CPK 알려줘" · "S3222 보고서 찾아줘" · "VDD_INT 상세 보여줘"
- "PMIC 에 MAJOR 몇 건이야?" · "제품별 건수 알려줘" (집계)
- "PMIC SOC 에 무슨 Item 있어" (항목 목록) · "이 세션 누가 올렸어?"·"온도 몇 도야?" (세션 메타)
- "IW06 세션 있냐?" · "어제 올라온 세션 목록" (존재 확인·기간)

## 분류가 실패해도 "모르겠다"로 끝내지 않는다

1. **규칙**(빠름) → 2. **LLM**(약할 때만) → 3. **광역 폴백**.
`unknown` 핸들러는 질문에서 건진 토큰을 세션·항목·코멘트 세 축에 던져 *무엇으로 걸리는지*
를 보여주고, 걸린 축만 클릭 버튼으로 준다.
또 규칙이 근거 없이 찍은 계획(`QueryPlan.weak`, catch-all `elif items:`)은 조회 결과가 0건이면
**agent 가 세션 자유검색으로 재검증해 교정**한다 — "IW06 있어?" 가 item 이 아니라 세션으로
잡히는 이유다(모양이 아니라 데이터가 판정한다). 재분류는 `viewer` 가 필요해 planner 가 아닌
agent 에 둔다.

**이 패키지는 라우트를 등록하지 않는다** (`plugin.py` 무변경). 웹 노출은 바깥의
[../report/routes_chat.py](../report/routes_chat.py) 한 곳뿐이고, 이 패키지는 CLI 와 그
라우트가 공유하는 순수 엔진이다.

## 웹 노출 (2026-08-10, 관리자 전용 테스트)

| 조각 | 위치 |
|---|---|
| 라우트 `POST /pe/report/api/chat` | [../report/routes_chat.py](../report/routes_chat.py) — master 404 가드 + CSRF + 세마포어 3 + 계측 |
| 프런트 위젯 (홈·세션 상세 공용) | [../report/static/webreport/chat.js](../report/static/webreport/chat.js) — 우하단 플로팅 버튼, DOM/스타일 자체 주입 |
| 노출 판정 | 홈 `applyViewer(v.is_master)` / 상세 `core.js loadAuth()` 의 `IS_MASTER` |
| 딥링크 `?tab=item_detail\|map&item=` | [../report/static/webreport/boot.js](../report/static/webreport/boot.js) `applyDeepLink()` |
| 사용 현황·부하·이력 | 관리자 패널 **Chatbot 탭** (`report_chatbot_log` 테이블) |

웹 응답은 `agent.answer_web` 이 `text` 에 더해 두 가지를 준다:
- `links[]` — 대상이 **지금 열린 세션**이면 `action`(그 자리에서 이동), 아니면 `url`(딥링크).
- `choices[]` — 애매할 때의 선택 버튼. 서버가 **완성된 후속 질의문**을 담아 보내고 클릭 =
  그 문자열을 새 요청으로 보내는 것뿐이라, 서버에 대화 상태가 없다.

CLI 계약(`agent.answer` 반환 키 4개)은 그대로다 — 웹 확장이 깨지 않는지 단위 테스트가 지킨다.

> ⚠ **`chatbot` 이라는 top-level 이름은 가로채이기 쉽다.** 2026-08-10 운영에서
> `eval_analyzer/chatbot`(LangChain 실험)이 먼저 잡혀 `answer_web` 이 없다는 AttributeError
> 가 났다 → 그쪽을 **`chatbot_prototype` 으로 개명**해 원인을 없앴다.
> 방어는 그대로 남아 있다: [routes_chat.py](../report/routes_chat.py) `_agent()` 가 경로를
> 검증하고 어긋나면 이 폴더를 고유 별칭으로 직접 적재한다. 회귀는
> [tests/test_chatbot_module_collision.py](../../tests/test_chatbot_module_collision.py)
> 가 가짜 충돌 패키지로 검사한다. 새 코드에서 eval_analyzer 를 `sys.path` 에 넣을 땐 **append**.

## 왜 LangChain/LangGraph 를 안 쓰나

1단계 흐름은 "질문 → QueryPlan(JSON) → 툴 2~3개 순차 호출 → 답"이 전부다. LangGraph 가
값어치를 하는 재검색 루프·후보 확인 멀티턴이 아직 없고, 필요한 LLM 기능은 OpenAI 호환
`chat/completions` POST 1개 + JSON 스키마 강제뿐이다. 운영 venv 가 Python 3.14 라 무거운
의존을 새로 얹는 위험이 이득보다 크다. 멀티턴/재검색이 실제로 필요해지면 그때 도입한다.

## 파일

| 파일 | 역할 |
|---|---|
| `planner.py` | 질문 → `QueryPlan`(intent 13종 + 제품/family/item/세션/metric/jump/집계축/기간). **규칙이 1차**, 약할 때만 LLM |
| `tools_report.py` | report.db — 세션/제품/Issue Table/세션 횡단 item 검색 |
| `tools_eval.py` | eval.db — item 마스터·alias·과거 케이스·수치·사람 코멘트 + `stats_summary`(축별 건수 집계) |
| `tools_metrics.py` | web_report 계산값 — 수율/CPK/측정값. 콜드면 배경 빌드만 걸고 `building` 반환 |
| `eval_store.py` | eval.db read-only 커넥션 (경로 override 가능) |
| `rowkey.py` | Issue Table `row_key` 파서 (`Yield\|bin\|item` 등) |
| `agent.py` | 고정 워크플로 오케스트레이션 + 근거가 붙은 답변 렌더 |
| `cli.py` | 단발/REPL/골든 채점 CLI |

## 실행

```
cd server
python -m chatbot                       # 상태 확인(eval.db 경로, LLM 설정 여부)
python -m chatbot --no-llm "PMIC 에 POR 들어가는 항목 이력 알려줘"
python -m chatbot --master --json "SOC-000016 보고서 이슈 close 됐어?"
python -m chatbot --golden ../tests/chatbot_golden.yaml --no-llm
python -m chatbot --eval-db D:\path\to\eval.db "SGM 항목 이력"
```

검증: `python tests/test_chatbot_tools.py` (repo 루트에서, 임시 DB 사용)

## 알아야 할 함정 3가지

1. **item 축은 report.db 에 없다.** `report_analysis_summary.item_name` 은 item 이름이
   아니라 **bin 번호 문자열**이고(`server/upload_xlsx.py:141`), web_report 세션은 그 테이블을
   쓰지도 않는다. 진짜 item 은 ① eval.db `item_master`, ② `report_webreport_edit.item_key`
   안에 인코딩된 문자열 두 곳에만 있다.
2. **Yield 이슈는 키가 비대칭이다.** 코멘트 키는 `Yield|<bin>|<item>`(item 단위)인데
   Status 키는 `Yield|<bin>`(bin 단위)다. item 으로 찾은 이슈의 Open/Close 를 보려면
   bin 으로 되짚어야 한다 (`rowkey.status_key`).
3. **`viewer` 는 절대 생략하면 안 된다.** `database/sessions.py:_history_where` 는
   `viewer=None` 이면 비공개 필터를 아예 붙이지 않는다. 그래서 이 패키지의 조회 함수는
   `viewer` 를 **키워드 필수 인자**로 두고 기본값을 주지 않는다.

## 설정 (LLM)

기존 eval_analyzer 관례를 그대로 쓴다 — `server/env/server.env` 또는 환경변수:

```
EVAL_LLM_ENABLED=true
EVAL_LLM_ENDPOINT=<사내 OpenAI 호환 chat/completions URL>
EVAL_LLM_MODEL=<모델명>
EVAL_LLM_API_KEY=<키, 필요 시>
EVAL_LLM_TIMEOUT=30
```

미설정이면 규칙 기반 계획으로 동작한다(골든 세트 56/56 통과 기준).

**호출 순서는 규칙이 먼저다.** `planner._needs_llm()` 이 참일 때(=unknown·weak·조건 없는
session_find)만 LLM 을 부른다. 챗 동시 처리는 3슬롯이고 LLM 왕복은 최대 30초라 전부 태우면
최악 처리량이 3건/30초가 되고, 무엇보다 골든셋이 지키는 것이 규칙 경로여서 **테스트되는 경로가
곧 운영 경로**여야 하기 때문이다. 되돌리려면 `_needs_llm` 이 항상 True 를 반환하면 된다.

⚠ `_ISSUE_KW` 에 `문제/에러` 가 들어가면서, **세션을 열어 둔 상태**의 "…문제된 적 있어?" 는
item 이력이 아니라 그 세션의 이슈(session_issue)로 간다(의도된 개선). LLM 은 **질문 해석만**
하고 답변 본문은 조회 결과 템플릿이라, LLM 이 없어도/틀려도 없는 값을 지어내지 않는다 —
오분류는 "되묻기(choices)" 나 unknown 으로 나타난다.

**동시실행 상한 3** (`routes_chat._CONCURRENCY`): LLM 을 켜면 요청당 최대 30초를 기다리는데
waitress 스레드가 13개뿐이라, 상한이 없으면 챗 몇 건이 검색결과·세션 조회까지 굶긴다.
못 잡으면 대기열에 쌓지 않고 429 로 즉시 돌려보낸다(관리자 탭에 `busy` 로 집계).

## 다음 단계

3단계 검색 품질(alias 사전·벡터 검색) → 4단계 분석 엔진 툴 연계. 전면 개방(master 해제) 시엔
관리자 탭의 대기시간·혼잡거절 추이를 먼저 보고 상한·워커를 조정할 것.

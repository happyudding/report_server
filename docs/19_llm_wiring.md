# 19. LLM 배선 — 어디에 꽂고, 어디로 나가는가

이 프로젝트에서 LLM 을 쓰는 지점 전부와, 붙이는 방법·확인 방법.

**요약: 설정은 [server/env/server.env](../server/env/server.env) 5줄, 확인은 명령 1개다.**

```
# server/env/server.env
EVAL_LLM_ENABLED=true
EVAL_LLM_ENDPOINT=http://사내-LLM-호스트:8000/v1
EVAL_LLM_MODEL=모델명
EVAL_LLM_API_KEY=필요할때만
EVAL_LLM_TIMEOUT=30
```
```
python tools/llm_check.py --ping     # 소비자별 상태 + 실제 왕복 확인 (종료코드 0=전부 정상)
```
값을 바꾸면 **서버 재기동**이 필요하다 — 엔진 설정은 import 시점 1회만 읽는다(§4).

---

## 1. 소비자 (LLM 이 실제로 불리는 곳) — 2개

| # | 무엇에 쓰나 | 진입 | 출구(endpoint) | 꺼져 있을 때 |
|---|---|---|---|---|
| ① | **AI Comment 의 [점검제안] 문장** — 이슈 코멘트 3섹션 중 마지막 | [pipeline/recommend.py](../eval_analyzer/eval_engine/pipeline/recommend.py) `make_comment` | [eval_engine/llm_client.py](../eval_analyzer/eval_engine/llm_client.py) `complete()` | 룰의 `action_ko` 문구로 폴백 |
| ② | **웹 챗봇 질문 해석** — 자연어 → 어느 조회로 보낼지 | [chatbot/planner.py](../server/chatbot/planner.py) `plan()` | 같은 파일 `_call_llm()` | 정규식·키워드 규칙 분류 |

**둘 다 LLM 없이 완전히 동작한다.** ①은 코멘트가 항상 나오고, ②는 골든셋 22/22 를 규칙만으로
통과한다. LLM 은 **문장 합성(①)과 의도 분류(②)에만** 쓰고 수치·조회 결과는 언제나 DB 에서
온다 — 그래서 LLM 이 틀려도 "없는 값을 지어내는" 실패는 구조적으로 일어나지 않는다.

### 왜 HTTP 구현이 두 벌인가
불변규칙 #8(`eval_engine` import 는 web_report 의 3파일만)이라 `chatbot/planner.py` 는
`llm_client` 를 import 할 수 없다. 그래서 같은 shape 을 각자 구현한다 — 대신 **endpoint 해석
규칙(`chat_url`)을 문자 그대로 동일하게** 유지해야 한다. 어긋나면 "챗봇은 되는데 AI Comment
는 404" 가 된다. 회귀 가드는 `tools/llm_check.py` 가 두 URL 을 나란히 출력하는 것.

## 2. 아직 배선되지 않은 자리 (외부 담당자가 남긴 훅)

| 자리 | 파일 | 상태 |
|---|---|---|
| 선례 RAG 검색 | [eval_engine/precedent_client.py](../eval_analyzer/eval_engine/precedent_client.py) `_rag_search` | 스텁. `EVAL_PRECEDENT_BACKEND=rag` + `EVAL_PRECEDENT_RAG_ENDPOINT` 로 전환 예정. 계약은 [PRECEDENT_RAG_HANDOFF.md](../eval_analyzer/docs/PRECEDENT_RAG_HANDOFF.md) — **report_server 담당 몫으로 계약된 1함수** |
| 텍스트 → 선례 행 | [db_input/ai_extract.py](../eval_analyzer/db_input/ai_extract.py) `extract_rows_from_text` | 스텁. 검증(`validate_rows`)·CSV 변환·적재는 완성이라 **rows JSON 만 만들면 된다** |
| LangChain 프로토타입 | [eval_analyzer/chatbot/](../eval_analyzer/chatbot/llm.py) `build_llm` | 별개 실험용(langchain-openai 의존). 운영 챗봇은 `server/chatbot/` 이고 서로 코드 의존 없음 — **이름이 같아 혼동 주의**(§4) |

## 3. endpoint 표기 — base URL 과 완성 경로 둘 다 받는다

사내 배포마다 주는 값이 다르다. `chat_url()` 이 아래로 정규화한다(두 소비자 동일):

| 준 값 | 실제 POST |
|---|---|
| `http://host:8000/v1` | `http://host:8000/v1/chat/completions` |
| `http://host:8000/v1/chat/completions` | 그대로 |
| `http://host/우리게이트웨이/generate` | **그대로** (임의로 덧붙이지 않는다) |

payload 는 OpenAI 호환 chat completions(`model` + `messages`, `temperature:0`),
인증은 키가 있을 때만 `Authorization: Bearer …`. **다른 provider 라면 `llm_client.complete()`
하나만 교체**하면 ①이 통째로 바뀐다(모델명 하드코딩 금지 — eval_analyzer 규칙 #6).

## 4. 함정 3가지 (배선이 "반만" 먹던 자리)

1. **기동 경로에 따라 한쪽만 켜졌다.** 엔진(`eval_engine/config.py`)은 `os.environ` 만 읽는데,
   `server.env` 를 환경변수로 올려 주는 건 `start.bat` 뿐이다. `python wsgi.py` 로 직접 띄우면
   챗봇(파일 폴백 있음)만 켜지고 AI Comment 는 꺼진 채로 돌았다.
   → [server/config.py](../server/config.py) `_export_engine_env()` 가 엔진 import 전에
   `EVAL_*` 를 `os.environ` 으로 옮겨 비대칭을 없앤다(이미 설정된 값은 덮지 않는다).
2. **엔진 설정은 import 시점 1회다.** `eval_engine/config.py` 는 모듈 상수라, 실행 중에
   `os.environ` 을 바꿔도 반영되지 않는다. 값을 바꿨으면 **재기동**이 답이다.
3. **`chatbot` 패키지가 두 개다.** `server/chatbot`(운영)과 `eval_analyzer/chatbot`(실험).
   `sys.path` 에 eval_analyzer 를 **앞에** 넣으면 후자가 이겨 `from chatbot import planner` 가
   깨진다. 운영 코드는 [ai_comment.py](../web_report/ai_comment.py) `_evaluate_fn` 처럼 항상
   **append** 한다 — 새 스크립트를 쓸 때도 그 순서를 따를 것.

## 5. 확인 절차

```
python tools/llm_check.py              # 설정만 (호출 없음)
python tools/llm_check.py --ping       # 실제 1회 호출까지
```
출력에 소비자별 `OK/OFF`, 원본 endpoint, 정규화된 POST URL, 모델, 왕복 결과가 나온다.
프로그램에서 쓰려면 [web_report/ai_comment.py](../web_report/ai_comment.py) `llm_status(ping=…)`
(엔진 접근은 규칙 #8 때문에 이 함수를 경유한다).

부하 관점 확인은 관리자 대시보드 **Chatbot 탭** — 챗봇 쪽 LLM 왕복시간(`llm_ms`)이 질문마다
기록된다([docs/03 §report_chatbot_log](03_storage.md)). AI Comment 쪽은 콜드 빌드 안에서 돌아
[build_log](../web_report/build_log.py) 의 단계 소요에 포함된다.

> ⚠ LLM 을 켜면 **AI Comment 세션의 콜드 빌드가 느려진다.** `complete()` 는 case 마다 불리되
> 저장 게이트(`present.should_store` — yield fail 또는 cpk<cpk_warn)를 통과한 case 만이고,
> [api.py](../eval_analyzer/eval_engine/api.py) 의 `ThreadPoolExecutor(max_workers=3)` 로
> 3건씩 병렬로 돈다. 대략 `ceil(대상 case 수 / 3) × 1회 왕복`이 추가되고 최악은
> `× EVAL_LLM_TIMEOUT` 이다. 콜드 빌드 타임아웃(`WEB_REPORT_COMPUTE_TIMEOUT_SEC`, 기본 300s)은
> **풀 큐 대기까지 포함**해서 재므로, 켜기 전에 실제 세션 하나로 시간을 재 볼 것
> (관리자 이력 탭의 콜드 빌드 카드에 단계별 소요가 남는다).

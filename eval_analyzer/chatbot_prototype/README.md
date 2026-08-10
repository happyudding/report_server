# chatbot_prototype — eval.db 자연어 조회 (보류된 프로토타입 뼈대)

> ⚠ **운영 챗봇은 여기가 아니라 [server/chatbot/](../../server/chatbot/README.md) 이다.**
> 이 패키지는 참조용으로 남긴 LangChain 실험이며 **어디에서도 import 되지 않고**,
> langchain 미설치라 LLM 경로는 실행되지 않는다. `pytest.ini` 의 `testpaths = tests` 라
> `test_smoke.py` 도 일반 실행에서 수집되지 않는다.
>
> **2026-08-10 개명** (`chatbot` → `chatbot_prototype`): 같은 top-level 이름이
> `server/chatbot` 과 충돌해 운영에서
> `AttributeError: module 'chatbot.agent' has no attribute 'answer_web'` 를 냈다.
> 이 폴더의 `stats_summary`(집계 조회)는 운영 쪽
> [tools_eval.py](../../server/chatbot/tools_eval.py) 로 이식됐다. 아직 이식하지 않은 것은
> `get_case_detail`(웹 화면이 대체)과 `find_precedents`(엔진 `store.search_precedents` 위임 —
> 운영 챗봇은 불변규칙 #8 때문에 eval_engine 을 import 할 수 없어 구조적으로 불가).

엔지니어가 **자연어로 "쌓인 평가 결과를 검색"** 하는 챗봇. 지금은 **뼈대**만 — DB가 충분히
쌓이고 엔진이 성숙하면 `config.EVAL_LLM_*` endpoint만 켜서 실동작시킬 예정.

- **구조화 Tool 방식**: 임의 SQL 생성 없음. 미리 정의한 read-only 조회 4종을 LLM이 골라 호출.
- **독립 패키지**: `eval_engine`(config/store)만 참조. `report_server` 무관. langchain은 이 패키지 requirements.
- **read-only**: eval.db를 `mode=ro`로만 연다(쓰기 소유권은 `eval_engine.store`).

## 데이터 파이프라인
```
[NL 질문]
  → llm.py     LLM 이해 + Tool 선택 (LLM off면 router 가 대체)
  → tools.py   LangChain StructuredTool (queries 래핑)
  → queries.py 정의된 read-only 조회 (SELECT만)
  → db.py      read-only 커넥션 (eval_engine.config.DB_PATH)
  → agent.py   결과 → 한국어 답변
[답변]
```
계층 경계: `db.py`/`queries.py`/`router.py`(**langchain 무관 코어**) ⟂ `tools.py`/`llm.py`/`agent.py`(langchain).
→ langchain 미설치여도 코어와 규칙기반 `ask()`는 동작한다.

## 조회 함수 (queries.py, read-only 4종)
| 함수 | 용도 |
|---|---|
| `search_cases(product,item,status,item_class,limit)` | 조건별 fail case + 최신 평가 |
| `get_case_detail(case_id)` | 단일 case 전체 맥락(평가·metrics·signature·label·outcome) |
| `find_precedents(item_name,value_type,family_product)` | 선례검색(`store.search_precedents` 위임) |
| `stats_summary(group_by)` | status/product/product_type/item_class 집계 |

## 실행
```bash
# 코어/규칙기반 경로 (langchain 불필요)
python -m chatbot_prototype.cli "MAJOR 케이스 통계"
python -m chatbot_prototype.cli "vref 선례"

# LLM agent 경로 (나중에 켜는 법)
pip install -r chatbot_prototype/requirements.txt
set EVAL_LLM_ENABLED=true
set EVAL_LLM_ENDPOINT=http://<host>:<port>/v1   # OpenAI 호환 base URL
set EVAL_LLM_MODEL=<사용자 지정 모델>
set EVAL_LLM_API_KEY=<필요 시>
python -m chatbot_prototype.cli "vref trim 최근 fail 알려줘"
```
LLM 설정이 비면 `ask()`는 규칙기반 `router`로 자동 fallback(실제 DB 결과 반환).

## 후속(뼈대 범위 밖)
대화 메모리(multi-turn) · RAG/임베딩 검색 · 웹 UI/서버 · 스트리밍 · 권한/인증 · 프롬프트 튜닝.

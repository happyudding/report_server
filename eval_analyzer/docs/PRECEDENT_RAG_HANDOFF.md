# 선례검색 RAG 연동 — report_server 담당자 전달

> 그대로 담당자/담당 AI 에게 전달 가능. eval_analyzer 의 **선례(precedent) 검색**을 기존 SQL 에서
> **RAG 로 교체**할 때 **딱 한 함수만 구현**하면 되도록 entrypoint·입출력 계약을 정리한 문서.
>
> 전제: eval_analyzer 는 report_server 코드를 **import 하지 않는다**(의존 방향 1방향). RAG 호출은
> HTTP endpoint 또는 주입식으로만 붙인다. 나머지 파이프라인·DB·출력 계약은 **전부 불변**.

---

## 0. 한 줄
`eval_engine/precedent_client.py` 의 **`_rag_search(case_ctx, sig_result)` 한 함수만 구현**하고,
`EVAL_PRECEDENT_BACKEND=rag` 로 켜면 끝. 그 외 어떤 코드도 손대지 않는다.

## 1. 교체 지점 (단 하나)
| 파일 | 함수 | 할 일 |
|---|---|---|
| `eval_engine/precedent_client.py` | `_rag_search(case_ctx, sig_result)` | 현재 `NotImplementedError` 스텁 → RAG 검색 구현 |

- `recommend.find_precedents()` → `precedent_client.search()` → (backend 분기) `_rag_search()` 로 도달한다.
- 기본 백엔드는 `sql`(기존 `store.search_precedents`). RAG 미설정 시 동작은 종전과 동일.

## 2. 활성화 (환경변수)
| 변수 | 기본 | 의미 |
|---|---|---|
| `EVAL_PRECEDENT_BACKEND` | `sql` | `rag` 로 두면 `_rag_search` 사용 |
| `EVAL_PRECEDENT_RAG_ENDPOINT` | `""` | RAG 검색 endpoint(URL). 모델/주소 하드코딩 금지, 여기로 주입 |
| `EVAL_PRECEDENT_RAG_TOPK` | `5` | 회수할 선례 top-k |

> endpoint/모델은 **사용자 지정**한다. `_rag_search` 안에서 주소를 하드코딩하지 말고 `config.EVAL_PRECEDENT_RAG_*` 를 읽어라.

## 3. 입력 계약 — `_rag_search` 가 받는 것
```python
def _rag_search(case_ctx: dict, sig_result: dict) -> list: ...
```
- `case_ctx`:
  - `bin` (int) — 참고용(더 이상 필수 매칭 조건 아님)
  - `value_type` (str) — 측정값 종류(V/A/...) — 필수 매칭 조건
  - `item_canonical` (str) — **쿼리 핵심**(item 정규화 이름)
  - `family_product` (str | None) — 제품군 스코프(있으면 우선)
  - `case_id` (str) — **자기 자신은 선례에서 제외**할 것
- `sig_result` (dict) — 발화한 signature 들. 쿼리 보강용(선택).

## 4. 출력 계약 (엄수)
- 반환: `list[dict]`, **관련도 내림차순 정렬**. 결과 없으면 `[]`.
- 각 dict 의 **최소 필수 key 3개** (downstream 이 실제로 읽는 전부):

| key | 의미 | 예시 |
|---|---|---|
| `action` | 과거 조치 | `"retest"` |
| `result` | 결과 | `recovered_normal` / `confirmed_defective` / `improved` / `pending` |
| `human_comment` | 엔지니어 코멘트 원문 | `"site 편차로 재측정"` |

- **반환된 precedents 전부의 `human_comment` 가 코멘트 템플릿·LLM 프롬프트에 쓰인다**(action/result
  는 더 이상 코멘트 생성 판단에 쓰이지 않고 benchtest 표시용 참고 metadata) → 정렬 순서는
  표시 우선순위용.
- 위 3개 외 key(similarity 등)는 넣어도 무시되니 자유. 빠지면 안 되는 건 위 3개뿐.

## 5. 인덱싱 대상 가이드 (권장)
현 SQL(`store.search_precedents`)이 회수하는 소스와 동일하게 잡으면 동등 이상:
- **본문(임베딩 대상)**: `item_master.item_canonical` (+ `value_type`, `bin` 컨텍스트), `fail_case`
- **메타(반환 payload 구성)**: `label.human_comment` → `human_comment`,
  `case_outcome.action / result` → `action / result`, `case_signature(role='primary').signature`
- 스코프: 동일 `value_type` + (있으면) 동일 `family_product`, item 유사도 ≥0.70,
  `case_id != 현재`. `bin` 은 매칭 조건 아님.
- 상세 grain·조인은 [DB_SCHEMA.md](DB_SCHEMA.md) §9 참조.

## 6. 참고 구현 (동형 SQL)
`eval_engine/store.py` 의 `search_precedents()` 가 동일 계약의 SQL 버전:
- `value_type`(+ `family_product`) 로 후보 좁힘 → item 이름 퍼지유사도
  `≥ EVAL_PRECEDENT_SIM`(기본 0.70) 후처리 → 유사도순 전체 반환.
- RAG 는 이 "관련 선례 top-k" 를 **임베딩 유사도**로 대체하는 것뿐. 반환 dict 모양은 동일하게 맞춰라.

## 7. 검증 체크리스트
1. `EVAL_PRECEDENT_BACKEND` 미설정(=sql): 기존 테스트 그린 — `pytest tests/test_store.py tests/test_e2e.py`.
2. `EVAL_PRECEDENT_BACKEND=rag`: `_rag_search` 가 §4 계약대로 `list[dict]` 반환.
3. e2e: 반환 선례가 결과 `comment` 와 `case["precedents"]` 에 반영되는지(특히 `precedents[0]`).

## 8. 금지
- eval_engine 이 report_server 모듈을 import 하지 않는다(의존 방향 단방향 유지).
- RAG endpoint/모델 이름 하드코딩 금지 — `config.EVAL_PRECEDENT_RAG_*` 로만.
- `_rag_search` 외 다른 파일(`recommend.py` / `api.py` / `present.py` / `store.py`) 수정 불필요.

# LLM 배선 — 담당자 전달

> 그대로 담당자/담당 AI 에게 전달 가능. `eval_engine` 에 **LLM 을 실제로 붙이는 방법**과,
> 남겨 두셨던 스텁 3개가 **지금 어떤 상태인지**를 정리한 문서.
>
> 전제: eval_analyzer 는 report_server 코드를 **import 하지 않는다**(의존 방향 1방향).
> 모델·endpoint 는 전부 `config.EVAL_LLM_*` 로 주입한다(하드코딩 금지 — 불변 규칙 #6).

---

## 0. 한 줄

`server/env/server.env` 에 **`EVAL_LLM_*` 5줄**을 채우고 서버를 재기동하면 켜진다.
**코드 수정은 필요 없다** — HTTP 호출은 이미 구현돼 있다. 확인은 `python tools/llm_check.py --ping`.

---

## 1. 남겨 두신 스텁 3개 — 현재 상태

| 위치 | 상태 | 비고 |
|---|---|---|
| `eval_engine/llm_client.py` **`complete()`** | ✅ **구현 완료** (2026-08-10, report_server 측) | 표준 라이브러리 `urllib` 만 사용, **새 의존성 0**. 운영 venv 가 Python 3.14 라 SDK 를 얹지 않았다 |
| `eval_engine/precedent_client.py` `_rag_search()` | ⬜ 비어 있음 | 계약은 [PRECEDENT_RAG_HANDOFF.md](PRECEDENT_RAG_HANDOFF.md) 그대로 |
| `db_input/ai_extract.py` `extract_rows_from_text()` | ⬜ 비어 있음 | 검증(`validate_rows`)·CSV 변환·적재는 완성이라 **rows JSON 만 만들면 된다** |

`complete()` 를 채운 곳은 **엔진의 유일한 LLM 출구**이고, 소비자는
`pipeline/recommend.py` `make_comment()` 의 `[점검제안]` 섹션 하나뿐이다.

---

## 2. 켜는 법

### 운영 서버 (report_server 안에서 돌 때)
`server/env/server.env` 의 주석을 풀고 값만 채운다. 이 파일이 정본이다.

```
EVAL_LLM_ENABLED=true
EVAL_LLM_ENDPOINT=http://사내-LLM-호스트:8000/v1
EVAL_LLM_MODEL=모델명
EVAL_LLM_API_KEY=필요할때만
EVAL_LLM_TIMEOUT=30
```

### eval_analyzer 를 단독 실행할 때 (콘솔 testbench·CLI)
같은 이름의 **환경변수**를 직접 export 한다. 엔진은 `os.environ` 만 읽는다.

### ENDPOINT 표기 — 둘 다 받는다
`llm_client.chat_url()` 이 아래로 정규화한다. 배포마다 표기가 달라 404 로 조용히 실패하던 자리다.

| 준 값 | 실제 POST 대상 |
|---|---|
| `http://host:8000/v1` | `http://host:8000/v1/chat/completions` |
| `http://host:8000/v1/chat/completions` | 그대로 |
| `http://host/게이트웨이/generate` | **그대로** (임의로 덧붙이지 않는다) |

`is_enabled()` 는 `ENABLED=true` **그리고** `ENDPOINT` **그리고** `MODEL` 이 모두 있어야 True 다
— 반만 설정해 두고 `NotImplementedError` 로 터지는 일이 없게 한 기존 설계 그대로다.

---

## 3. 켜면 무엇이 달라지나

- **AI Comment 의 `[점검제안]` 문장**이 룰의 `action_ko` 문구 → LLM 합성으로 바뀐다.
  `[현상]`·`[과거사례]` 두 섹션은 **언제나 룰·선례에서** 만든다(변화 없음).
- 실패하면 `make_comment` 가 예외를 잡아 **조용히 `action_ko` 로 폴백**한다. LLM 유무와 무관하게
  코멘트는 항상 나온다.
- (참고) report_server 의 웹 챗봇도 같은 `EVAL_LLM_*` 를 읽어 함께 켜진다. 별개 구현이며
  엔진 동작에는 영향이 없다.

> ⚠ **콜드 빌드가 느려진다.** `complete()` 는 저장 게이트(`present.should_store` — yield fail
> 또는 cpk<cpk_warn)를 통과한 case 마다 불리고, `api.py` 의 `ThreadPoolExecutor(max_workers=3)`
> 로 3건씩 병렬로 돈다. 대략 **`ceil(대상 case 수 / 3) × 1회 왕복`** 이 추가되고 최악은
> `× EVAL_LLM_TIMEOUT` 이다. 켜기 전에 실제 세션 하나로 시간을 재 볼 것.

---

## 4. `complete()` 구현 계약

다른 provider 로 바꾸려면 **이 함수 하나만** 교체하면 된다.

| 항목 | 내용 |
|---|---|
| 요청 | `POST chat_url()` · `Content-Type: application/json` |
| payload | `{"model": model_version or config.EVAL_LLM_MODEL, "messages": [{"role":"user","content": prompt}], "temperature": 0}` |
| 인증 | `Authorization: Bearer <EVAL_LLM_API_KEY>` — **키가 있을 때만** 헤더를 붙인다 |
| timeout | `config.EVAL_LLM_TIMEOUT` (초) |
| 반환 | `body["choices"][0]["message"]["content"]` (문자열) |
| 실패 | **예외를 그대로 올린다.** 삼키지 않는다 — 상위(`make_comment`)가 룰 문구로 폴백하므로, 여기서 숨기면 폴백한 사실이 드러나지 않는다 |

프롬프트는 `recommend._build_prompt()` 가 만든다. 지시문의 목적은 **환각 억제**다(원인 단정 금지,
입력·선례에 없는 수치·제품명·설비를 지어내지 말 것, 섹션 제목 출력 금지).

---

## 5. 확인 절차

```
python tools/llm_check.py            # 설정만 확인 (호출 없음)
python tools/llm_check.py --ping     # 실제로 1회 호출해 왕복까지 확인
```

출력에 소비자별 `OK/OFF`, 원본 endpoint, **정규화된 실제 POST URL**, 모델, 왕복 결과가 나온다.
종료코드는 0(정상) / 1(하나라도 꺼짐·실패)이라 CI 에서도 쓸 수 있다.

미설정 상태의 출력 예:

```
[OFF ] 엔진 AI Comment   (eval_engine/llm_client.complete → pipeline/recommend.make_comment)
        endpoint : (미설정)
        model    : (미설정)   timeout 30.0s   api_key 없음
        꺼져 있으면 → 룰 기반 [점검제안] 문구로 폴백(코멘트는 항상 나온다)
```

프로그램에서 상태를 읽으려면 report_server 쪽 `web_report/ai_comment.py` 의
`llm_status(ping=…)` 을 쓴다.

---

## 6. 지켜진 규약

- **모델·endpoint 하드코딩 없음** — 전부 `config.EVAL_LLM_*` (불변 규칙 #6).
- **실패는 상위 폴백** — 코멘트는 LLM 유무와 무관하게 항상 나온다.
- **새 의존성 0** — 표준 라이브러리 `urllib` 만. LangChain 계열을 엔진 런타임에 섞지 않는다.
- eval.db 스키마·`store.py` DDL 은 건드리지 않았다.

---

## 7. 함정 2가지

1. **`eval_engine/config.py` 는 import 시점에 값을 1회만 읽는다**(모듈 상수). 실행 중에
   `os.environ` 을 바꿔도 반영되지 않는다 → **값을 바꿨으면 재기동**이 답이다.
2. **단독 실행 시에는 환경변수를 직접 export** 해야 한다. report_server 안에서 돌 때는
   `server/config.py` 가 `server.env` → `os.environ` 브리지를 태워 주지만(엔진 import 전에
   실행된다), eval_analyzer 만 따로 띄우면 그 브리지가 없다.

---

## 관련 문서

- 선례 RAG 교체: [PRECEDENT_RAG_HANDOFF.md](PRECEDENT_RAG_HANDOFF.md)
- report_server 팀용 전체 배선 지도(소비자 2개·부하·함정): `docs/19_llm_wiring.md`

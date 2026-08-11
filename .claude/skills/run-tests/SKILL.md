---
name: run-tests
description: 이 프로젝트의 테스트 실행 관례(pytest 미사용 self-run + 예외 파일들). 사용자가 "테스트 돌려줘", "test_xxx 실행해줘", "테스트 어떻게 돌려?" 라고 하거나, 코드를 고친 뒤 관련 테스트를 실행하려 할 때 사용. pytest 로 일괄 실행하기 전에 반드시 확인할 것.
---

# 테스트 실행 관례

**`tests/` 는 pytest 를 쓰지 않는다.** conftest.py·pytest.ini·pyproject 설정이 없고,
각 파일이 `if __name__ == "__main__":` 로 자기 자신을 돌리는 self-run + assert 스타일이다.
파일 상단 docstring 에 실행법이 적혀 있다.

## 1. 기본형

```
server\.venv\Scripts\python.exe tests\test_xxx.py
```

`.venv` 를 써야 한다 — 전역 파이썬에는 의존성이 없다. 여러 개를 돌릴 때도 **한 파일씩
순차 실행**한다. `pytest tests/` 로 통째로 돌리지 말 것(아래 이유).

## 2. 예외 — 이것만 다르다

| 대상 | 특이사항 |
|---|---|
| `tests/test_eta.py` | **유일하게 pytest 를 import 하는 파일**. `server\.venv\Scripts\python.exe -m pytest tests/test_eta.py -q`. `config` 가 import 시점 env 로 굳고 프로세스에 하나뿐이라, 다른 테스트와 한 프로세스에서 돌리면 **먼저 import 된 모듈의 환경이 전부를 지배**한다. `_OWNS_ENV` 가드가 자기가 첫 번째일 때만 격리 환경을 만든다 |
| `tests/test_build_log.py`, `tests/test_prewarm_offload.py` | Windows spawn — 워커가 모듈을 `__mp_main__` 으로 재실행한다. 워커 기동은 반드시 `__main__` 가드 안에서 (재귀 spawn 방지) |
| `tests/load_test_10users.py` | **실 DB·업로드 디렉토리에 LOADTEST 세션을 만든다.** 서버가 떠 있어야 하고, 끝난 뒤 관리자 패널에서 product="LOADTEST" 로 검색해 **수동 일괄 삭제** 필요(자동 정리 없음) |
| `tests/bench_webreport.py` | 임시 DB 격리라 운영 무접촉. 전용 skill `webreport-bench` 로 실행 |
| `tests/test_perf_guard.py` | `tools/perf_guard.py` 의 `_RULES` 를 건드렸으면 **필수**. `python tools/perf_guard.py --selftest` 와 같은 것 |
| `tests/test_eval_panel_js.py` | **`server/eval_panel/eval_panel.html` 을 고쳤으면 필수.** headless Edge 로 패널 JS 를 실제 서버 페이로드에 대고 돌린다(node 가 없어 Edge 가 유일한 JS 실행 수단). Edge 가 없으면 정적 id 검사만 하고 SKIP. 파이썬 테스트로는 안 잡히는 부류 — 렌더 예외·**로더 실패 시 빈 화면**을 잡는다 |
| `tests/test_issue_signature.py` | Issue Table Signature 컬럼 + ENGR 정답 라벨(eval DB) 을 고쳤으면 필수. `REPORT_EVAL_DB_PATH` 를 임시 경로로 잡으므로 **단독 실행**(다른 test 와 pytest 로 묶으면 격리가 import 순서에 좌우된다) |
| `eval_analyzer/tests/` | **여기만 conftest.py 가 있다** — autouse fixture `all_signatures_enabled` 가 배포용 `enabled:false` 를 무시한다. pytest 로 실행하며, 배포 상태 그대로 보려면 `rules_as_deployed` 마커 |

## 3. 주의

- **eval self-run 계열 테스트를 pytest 로 묶어 돌리면 개발 `report.db` 가 오염된다.**
  단독 실행이 원칙이다.
- 테스트 실패를 보고할 때는 출력 그대로 전달한다. "통과했다"는 실제로 돌려 exit code 와
  출력을 확인한 뒤에만 말한다.
- 개발 PC 에서 **Web Report 업로드 e2e 는 실패가 정상**이다 — `client/honey_parse/` 더미
  폴백이 구형 5-meta 라서다(CLAUDE.md 불변규칙 9). 이걸 회귀로 오진하지 말 것.
- tabs 통계·honeyform 변환을 고쳤다면 테스트 통과와 별개로 **같은 세션 payload 의 정준
  JSON 완전 일치**로 회귀 없음을 확인한다(정수 컬럼 int64 dtype 보존 포함) —
  [web_report/CLAUDE.md](../../../web_report/CLAUDE.md).

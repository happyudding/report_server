# web_report MCP 서버 (**골격**)

Claude 등 MCP 클라이언트가 web_report 의 계산 결과를 직접 조회하게 하는 stdio 서버.
공개 REST API(`/pe/api/v1/web-report`)를 얇게 감싼 것이고, **tool 스키마는 서버의
`GET /capabilities` 하나에서만 나온다**.

> ## 현재 상태: 골격
> 뼈대(HTTP 래퍼 · capabilities 로딩 · 대용량 URL 포인터 처리)와 **예시 tool 2개**
> (`get_overview`, `list_sessions`) + `list_web_report_functions` 까지만 있다.
> 전 함수(22개) tool 등록은 후속 작업이다 — 아래 "후속 구현" 참조.

## 실행

```bash
pip install -r server/web_report_mcp/requirements.txt

# 서버 주소(기본: 운영 http://12.81.220.117:8080)
set WEBREPORT_API_BASE=http://12.81.220.117:8080
# 비공개 세션까지 보려면(서버에 같은 값이 설정돼 있어야 한다)
set WEB_REPORT_API_KEY=<키>

python server/web_report_mcp/server.py
```

Claude Code 에 등록:

```bash
claude mcp add web-report -- python F:/COINAPI/report_server/server/web_report_mcp/server.py
```

## 설계 결정

| 결정 | 이유 |
|---|---|
| REST 를 HTTP 로 호출 (facade 직접 import 아님) | 직접 import 는 config·DB·캐시 초기화를 이 프로세스에 복제하고, 운영 waitress 와 SQLite/디스크 캐시를 이중으로 연다. 또 REST 층의 동시 실행 상한(429)과 관리자 패널 계측을 우회한다. HTTP 경유는 운영 무개입 + 타 호스트 동작 + 계측 자동 포함 |
| tool 스키마를 `/capabilities` 에서 로드 | 규약을 두 벌로 적으면 반드시 갈라진다. 서버 `contracts.py` 가 유일한 원천 |
| 202(building)를 그대로 전달 | 에러가 아니라 "계산 중"이다. LLM 이 잠시 후 재시도하도록 결과에 담아 넘긴다 |
| 대용량(`cost: heavy`)은 값 대신 `full_data_url` | ECDF 전량·map die 전량은 MCP 응답에 실을 크기가 아니다 |

## 후속 구현

1. **전 tool 등록** — `build_server()` 의 예시 2개를 걷어내고 `specs` 전체를 순회해
   등록한다. 그 전에 각 tool 의 **설명문**을 다듬어야 한다: MCP 에서 tool 선택은 설명문
   품질이 좌우하므로, `contracts.py` 의 `summary` 를 "언제 이걸 쓰나" 문장으로 보강하는
   편이 좋다(`server/chatbot/tools_report.py` 의 docstring 이 좋은 본보기다 —
   "언제 쓰나 / 언제 쓰지 않나" 를 명시한다).
2. **입력 스키마 변환** — SPEC 의 `params`(JSON Schema 조각)를 FastMCP 의 tool 인자
   시그니처로 옮기는 변환기. 지금은 예시 tool 이 인자를 손으로 적고 있다.
3. **검증** — REST 응답과 tool 응답의 값 일치 확인. 서버를 띄우고
   `python server/web_report_mcp/server.py` 로 tool 을 호출해 `curl` 결과와 대조한다.
4. **에러 문안** — `unreachable`(서버 미기동)·`session_not_found`·`busy` 를 LLM 이
   사용자에게 그대로 전할 수 있는 한국어 문장으로 다듬기.

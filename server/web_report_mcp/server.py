"""web_report MCP 서버 — **골격**. REST(/pe/api/v1/web-report)를 얇게 감싼다.

> 현재 상태: 뼈대 + 예시 tool 2개까지만 구현돼 있다. 전 tool 등록·검증은 후속 작업이다
> (README.md "후속 구현" 절). 이 파일은 Flask 앱에 import 되지 않는 **독립 실행
> 프로세스**라 서버 동작·의존성에 영향이 없다.

## 왜 HTTP 를 타나 (facade 직접 import 가 아니라)

facade 를 직접 부르면 config·DB·캐시 초기화를 이 프로세스에 복제해야 하고, 운영
waitress 프로세스와 같은 SQLite/디스크 캐시를 이중으로 열게 된다. 게다가 REST 층이
가진 동시 실행 상한(429)·관리자 패널 계측을 통째로 우회한다. HTTP 경유는 운영 서버에
손대지 않고, 다른 호스트에서도 돌고, 호출이 계측에 그대로 잡힌다.

## tool 스키마의 출처

`GET /capabilities` 가 돌려주는 `FUNCTION_SPECS` 하나뿐이다(서버의
`public_api/web_report/contracts.py`). 여기서 스키마를 다시 적지 않는다 — 두 벌이 되면
반드시 갈라진다.

실행:
    pip install -r requirements.txt
    python server/web_report_mcp/server.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# 서버 주소 정본은 server/env/server.env 의 SERVER_BASE_URL 이다. 여기서는 env 로만 받고
# 기본값은 운영 주소를 쓴다(사내망).
API_BASE = os.environ.get(
    "WEBREPORT_API_BASE", "http://12.81.220.117:8080").rstrip("/") + "/pe/api/v1/web-report"
API_KEY = os.environ.get("WEB_REPORT_API_KEY", "").strip()
TIMEOUT = float(os.environ.get("WEBREPORT_API_TIMEOUT", "30"))


# ── REST thin wrapper ────────────────────────────────────────────────────────
def call(path, params=None):
    """REST GET → dict. 상태코드를 **에러가 아니라 분기 키**로 옮긴다.

    MCP tool 결과는 LLM 이 읽는 값이라 예외로 끊지 않는다. 특히 202(building)는
    "아직 계산 중이니 잠시 후 다시" 라는 정상 흐름이므로 그대로 전달해 재시도를 맡긴다.
    """
    url = f"{API_BASE}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if API_KEY:
        req.add_header("X-Report-Api-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:                                    # noqa: BLE001
            body = {"error": f"http_{exc.code}"}
        if exc.code == 202:
            body.setdefault("building", True)
        body.setdefault("status_code", exc.code)
        return body
    except urllib.error.URLError as exc:
        return {"error": "unreachable", "message": str(exc.reason), "url": API_BASE}


def capabilities():
    """서버가 선언한 함수 규약 전체 — tool 등록의 유일한 원천."""
    return call("/capabilities")


def _fill(path, params):
    """SPEC 의 `/{session_id}/overview` 를 실제 경로로. 채운 값은 params 에서 뺀다."""
    out = dict(params or {})
    filled = path
    for key in list(out):
        token = "{" + key + "}"
        if token in filled:
            filled = filled.replace(token, urllib.parse.quote(str(out.pop(key)), safe=""))
    return filled, out


def call_spec(spec, params):
    """SPEC 1개 실행. 대용량은 값 대신 URL 포인터를 준다 — MCP 응답에 담을 크기가 아니다."""
    path, query = _fill(spec["path"], params)
    if spec.get("cost") == "heavy":
        url = f"{API_BASE}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return {"full_data_url": url, "cost": "heavy",
                "note": "응답이 커서 MCP 로 싣지 않는다. 이 URL 로 직접 받을 것."}
    return call(path, query)


# ── MCP 서버 ─────────────────────────────────────────────────────────────────
def build_server():
    """capabilities 를 읽어 tool 을 자동 등록한다.

    골격 단계에서는 **예시 tool 2개만** 등록한다(_EXAMPLE_TOOLS). 전 tool 로 넓히려면
    그 집합을 비우고 `specs` 전체를 도는 것으로 바꾸면 되는데, 그 전에 각 tool 의
    설명문을 LLM 이 고를 수 있는 문장으로 다듬는 작업이 필요하다 — README 참조.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("mcp SDK 가 없다. pip install -r server/web_report_mcp/requirements.txt",
              file=sys.stderr)
        raise

    caps = capabilities()
    if caps.get("error"):
        print(f"[warn] capabilities 조회 실패: {caps} — 서버 주소를 확인할 것 "
              f"(WEBREPORT_API_BASE={API_BASE})", file=sys.stderr)
    specs = {s["name"]: s for s in (caps.get("functions") or [])}

    mcp = FastMCP("web_report")

    @mcp.tool()
    def list_web_report_functions() -> dict:
        """이 서버가 제공하는 web_report 조회 함수 목록과 입력 규약을 돌려준다."""
        return capabilities()

    # ── 예시 tool (골격) ─────────────────────────────────────────────────────
    @mcp.tool()
    def get_overview(session_id: str) -> dict:
        """세션의 분석 모드·source·수율 요약(STEP 분해 포함)·수율 분모 기준·ENGR 결론.

        평가 세션에 대해 가장 먼저 부르는 함수다. session_id 는 list_sessions 로 찾는다.
        아직 계산 중이면 {"building": true} 를 돌려주니 잠시 후 다시 부른다.
        """
        spec = specs.get("get_overview")
        if not spec:
            return call(f"/{urllib.parse.quote(session_id, safe='')}/overview")
        return call_spec(spec, {"session_id": session_id})

    @mcp.tool()
    def list_sessions(product: str = "", lot_id: str = "", q: str = "",
                      limit: int = 20) -> dict:
        """평가 세션을 제품·LOT·검색어로 찾는다. 다른 함수에 넘길 session_id 를 여기서 얻는다."""
        spec = specs.get("list_sessions")
        params = {"product": product, "lot_id": lot_id, "q": q, "limit": limit}
        if not spec:
            return call("/sessions", params)
        return call_spec(spec, params)

    return mcp


def main():
    build_server().run()


if __name__ == "__main__":
    main()

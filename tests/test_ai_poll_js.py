"""AI Comment 자동 갱신 폴링(boot.js) 회귀 — headless Edge 로 실제 로직을 돌린다.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_ai_poll_js.py

**왜 이 파일이 생겼나** (2026-09-02): 클라 Claude 대행 문장이 도착해도 화면이 갱신되지
않아 사용자가 **세션을 나갔다 다시 들어와야** 새 문장을 봤다는 신고. 원인은 폴링 tick 의
판정이었다:

  ① 완료 판정이 `done`(평가 pending 없음)만으로도 참이라, 최종본 세션에서 **5초마다
     전체 재렌더**가 돌았다(스크롤·팝오버가 튄다). 그런데도 종료 조건은
     `done && llmLeft === 0` 이라 폴링은 안 끝났다.
  ② 반대로 `lastLlmPending` 을 재렌더한 경우에만 갱신해, 서버가 pending 을 다시 만든
     경우 다음 tick 이 계속 "줄었다"로 오판했다.

파이썬 테스트로는 못 잡는다 — 판정이 브라우저 JS 안에 있고 fetch 응답 순서에 따라
갈린다. 그래서 fetch 를 가짜로 갈아끼우고 tick 을 실제로 돌린다.

검증하는 것:
  (a) 문장이 배치로 도착하면 **나갔다 오지 않아도** 화면이 다시 그려진다 (핵심 신고)
  (b) 변화가 없는 tick 은 재렌더하지 않는다 (5초마다 화면이 튀지 않는다)
  (c) 전부 도착하면 폴링이 종료된다 (무한 폴링 없음)
  (d) 입력 중에는 다시 그리지 않는다 (불변 규칙 #12 — 사용자 입력 보호)

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "server" / "report" / "static" / "webreport"
_TMP = Path(tempfile.mkdtemp(prefix="wr_ai_poll_js_"))

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def edge_path():
    for p in _EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def run_probe(harness_js: str, name: str) -> str:
    """boot.js 의 폴링 함수만 떼어 돌린다.

    boot.js 전체는 DOM·다른 모듈에 의존해 그대로 로드할 수 없다. 폴링 블록은 자기완결이라
    **소스에서 그 구간만 추출**해 스텁과 함께 평가한다 — 실제 배포되는 코드 그대로를
    검사하므로 사본 드리프트가 없다.
    """
    src = (_JS / "boot.js").read_text(encoding="utf-8")
    start = src.index("const AI_POLL")
    end = src.index("function showLoadOverlay")
    poll_src = src[start:end]
    html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            "<script>"
            # boot.js 폴링이 기대하는 바깥 심볼 스텁 — 렌더 횟수만 센다.
            "var SESSION_ID='S1', DATA={web_report:{}}, _globalBinColors=null;"
            "var RENDERS=0;"
            "function renderActive(){RENDERS++;}"
            "function seedEmptyFrames(){}"
            "function buildDistColorMap(){}"
            "function aiLlmPendingExpired(){return false;}"
            "</script>"
            f"<script>{poll_src}</script>"
            + harness_js + "</body></html>")
    page = _TMP / f"{name}.html"
    page.write_text(html, encoding="utf-8")
    dump = _TMP / f"{name}.dom.txt"
    args = ",".join("'%s'" % a for a in (
        "--headless=new", "--disable-gpu", "--no-sandbox",
        "--virtual-time-budget=20000", "--dump-dom", page.as_uri()))
    ps = (f"Start-Process -FilePath '{edge_path()}' -ArgumentList @({args}) "
          f"-RedirectStandardOutput '{dump}' -NoNewWindow -Wait")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=180, check=False)
    raw = dump.read_text(encoding="utf-8", errors="replace") if dump.is_file() else ""
    m = re.search(r'<pre id="res">([\s\S]*?)</pre>', raw)
    assert m, f"{name}: 하네스가 실행되지 않았습니다 (스크립트 파싱 오류 의심)"
    return m.group(1).strip()


def test_poll_updates_without_reentering():
    """(a)(b)(c) 문장이 배치로 도착하면 재진입 없이 갱신되고, 변화 없으면 안 그린다."""
    # 서버 응답 시나리오: 2행 대기 → 1행 도착 → 0행(완료).
    harness = (
        "<script>(function(){var out={};"
        "var seq=["
        "  {web_report:{ai_llm_pending:{'CPK|A':1,'CPK|B':1}}},"   # 변화 없음
        "  {web_report:{ai_llm_pending:{'CPK|A':1,'CPK|B':1}}},"   # 변화 없음
        "  {web_report:{ai_llm_pending:{'CPK|B':1}}},"             # 1건 도착 → 그려야 함
        "  {web_report:{}}"                                        # 완료 → 그리고 종료
        "];"
        "var i=0; out.calls=0;"
        "window.fetch=function(){ out.calls++;"
        "  var body=seq[Math.min(i++, seq.length-1)];"
        "  return Promise.resolve({status:200, json:function(){return Promise.resolve(body);}});"
        "};"
        "AI_POLL.INTERVAL_MS=30;"      # 테스트 속도 — 판정 로직은 그대로다
        "DATA={web_report:{ai_llm_pending:{'CPK|A':1,'CPK|B':1}}};"
        "maybeStartAiPendingPoll();"
        "setTimeout(function(){"
        "  out.renders=RENDERS;"
        "  out.leftover=Object.keys((DATA.web_report||{}).ai_llm_pending||{}).length;"
        "  var before=out.calls;"
        "  setTimeout(function(){"
        "    out.stopped=(out.calls===before);"   # 완료 후 더 이상 폴링하지 않는가
        "    var pre=document.createElement('pre');pre.id='res';"
        "    pre.textContent=JSON.stringify(out);document.body.appendChild(pre);"
        "  }, 300);"
        "}, 900);"
        "})();</script>")
    r = json.loads(run_probe(harness, "poll_update"))
    assert r["renders"] >= 2, \
        f"문장이 도착했는데 화면을 다시 그리지 않았습니다(재진입해야 보임): {r}"
    assert r["renders"] <= 3, \
        f"변화 없는 tick 에도 재렌더했습니다(5초마다 화면이 튄다): {r}"
    assert r["leftover"] == 0, f"완료 payload 가 반영되지 않았습니다: {r}"
    assert r["stopped"], f"완료 후에도 폴링이 계속됩니다(무한 폴링): {r}"
    print(f"  (a)(b)(c) 재진입 없이 갱신·불필요 재렌더 없음·완료 시 종료 OK "
          f"(렌더 {r['renders']}회)")


def test_poll_defers_while_editing():
    """(d) 입력 중에는 다시 그리지 않는다 — 규칙 #12(사용자 입력 불소실)."""
    harness = (
        "<script>(function(){var out={};"
        "var ta=document.createElement('textarea');document.body.appendChild(ta);ta.focus();"
        "window.fetch=function(){"
        "  return Promise.resolve({status:200, json:function(){"
        "    return Promise.resolve({web_report:{}});}});};"
        "AI_POLL.INTERVAL_MS=30;"
        "DATA={web_report:{ai_llm_pending:{'CPK|A':1}}};"
        "maybeStartAiPendingPoll();"
        "setTimeout(function(){"
        "  out.editing=document.activeElement===ta;"
        "  out.renders=RENDERS;"
        "  var pre=document.createElement('pre');pre.id='res';"
        "  pre.textContent=JSON.stringify(out);document.body.appendChild(pre);"
        "}, 500);"
        "})();</script>")
    r = json.loads(run_probe(harness, "poll_editing"))
    assert r["editing"], "하네스 오류 — textarea 포커스가 유지되지 않았습니다"
    assert r["renders"] == 0, \
        f"입력 중에 화면을 다시 그렸습니다(입력이 날아간다): {r}"
    print("  (d) 입력 중 재렌더 보류(입력 보호) OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if edge_path() is None:
        print(f"SKIP — headless Edge 를 찾지 못했습니다 (찾은 경로: {_EDGE_CANDIDATES})")
        return
    test_poll_updates_without_reentering()
    test_poll_defers_while_editing()
    print("\ntest_ai_poll_js: 전부 통과")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()

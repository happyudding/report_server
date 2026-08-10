"""/pe/eval 패널 JS 회귀 — headless Edge 로 **실제 서버 페이로드**를 렌더해 본다.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_eval_panel_js.py

**왜 이 파일이 생겼나** (2026-08-10): 트레이스 케이스 상세에서 룰을 눌렀을 때 화면이
아무 메시지 없이 빈 채로 굳는 신고가 있었다. 원인은 렌더 로직이 아니라 오류 처리였다 —
`activateTab` 이 `loaded.add(id)` 를 `await` **앞에서** 해서, 로더가 던지면 (a) 탭은 이미
전환돼 내용이 빈 채로 남고 (b) `loaded` 에 등록돼 다시 눌러도 아무 일이 안 일어났다.
`init()` 도 try/catch 가 없어 `/api/meta` 한 번 실패(게이트 쿠키 만료 등)로 패널 전체가
죽었다. 두 결함 다 파이썬 테스트로는 절대 잡히지 않는다 — 브라우저에서 돌려야 보인다.

검증하는 것 4가지:
  (a) 스크립트가 참조하는 DOM id 가 전부 마크업에 있다
      (`$('없는id').addEventListener` 는 최상위 TypeError 라 이후 JS 가 통째로 죽는다)
  (b) 실제 트레이스 케이스 페이로드로 `renderCase` 가 예외 없이 그려진다 (라벨 유/무 둘 다)
  (c) 실제 signatures 페이로드로 룰 칩 클릭 경로(`gotoSignature` → 탭 전환 → 로더)가 동작
  (d) **로더가 실패하면 빈 화면이 아니라 오류 + 재시도가 뜨고 `loaded` 에 남지 않는다**

Edge 가 없으면 (a) 만 하고 나머지는 SKIP 한다(이 저장소는 node 가 없어 headless Edge 가
유일한 JS 실행 수단이다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "server"))

_TMP = Path(tempfile.mkdtemp(prefix="eval_panel_js_"))
os.environ.setdefault("REPORT_EVAL_DB_PATH", str(_TMP / "eval" / "eval.db"))

PANEL = _ROOT / "server" / "eval_panel" / "eval_panel.html"

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
    """패널 HTML 뒤에 하네스를 붙여 headless Edge 로 돌리고 `<pre id=res>` 를 돌려준다.

    stdout 을 **파일로** 리다이렉트한다 — 파이프로 받으면 Windows 에서 빈 출력이 온다.
    """
    html = PANEL.read_text(encoding="utf-8").rstrip() + harness_js
    page = _TMP / f"{name}.html"
    page.write_text(html, encoding="utf-8")
    dump = _TMP / f"{name}.dom.txt"
    with open(dump, "wb") as fh:
        subprocess.run(
            [edge_path(), "--headless=new", "--disable-gpu", "--no-sandbox",
             "--virtual-time-budget=5000", "--dump-dom", page.as_uri()],
            stdout=fh, stderr=subprocess.DEVNULL, timeout=120, check=False)
    raw = dump.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'<pre id="res">([\s\S]*?)</pre>', raw)
    assert m, f"{name}: 하네스가 실행되지 않았습니다 (스크립트 파싱 오류 의심)"
    return m.group(1).strip()


def js_literal(obj) -> str:
    """JSON 을 <script> 안에 안전하게 심는다 — `</` 만 끊어 조기 종료를 막는다."""
    return json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")


# ── (a) DOM id 참조 정합 ─────────────────────────────────────────────────────

def test_dom_ids_resolve():
    src = PANEL.read_text(encoding="utf-8")
    js = "\n".join(re.findall(r"<script>(.*?)</script>", src, re.S))
    markup = re.sub(r"<script>.*?</script>", "", src, flags=re.S)
    defined = set(re.findall(r'\bid="([^"]+)"', markup))
    defined |= set(re.findall(r'\bid="([^"${]+)"', js))     # 템플릿으로 만드는 id
    used = set(re.findall(r"\$\('([^']+)'\)", js)) | set(re.findall(r'\$\("([^"]+)"\)', js))
    missing = sorted(used - defined)
    assert not missing, (f"$() 가 참조하지만 마크업에 없는 id: {missing} — "
                         "최상위에서 참조되면 이후 JS 가 통째로 죽습니다")
    print(f"[a] DOM id 정합 OK (참조 {len(used)} / 정의 {len(defined)})")


# ── 실제 서버 페이로드 만들기 ────────────────────────────────────────────────

def build_trace_cases():
    """`/api/trace/<token>/case/<i>` 가 내려주는 것과 **같은 경로**로 케이스를 만든다.

    합성 dict 를 손으로 적으면 계약이 바뀌어도 테스트가 통과해 버린다 — 엔진과
    eval_debug 를 실제로 태워야 프런트가 보는 것과 같은 모양이 나온다.
    """
    import pandas as pd
    from web_report import ai_comment, eval_debug
    from web_report.honeyform import META_COLUMNS, split_honeyform

    cols = META_COLUMNS + ["ItemA"]
    head = [["TSEQ", "", "", "", "", "", "", 1], ["TNO", "", "", "", "", "", "", 100],
            ["STEP", "", "", "", "", "", "", "P1"], ["UNIT", "", "", "", "", "", "", "V"],
            ["HILIM", "", "", "", "", "", "", 10], ["LOLIM", "", "", "", "", "", "", 0]]
    body = []
    for i in range(60):                       # 8/60 outlier → SEVERE_OUTLIER 발화
        v = 5.0 if i < 52 else 15.0
        body.append([f"s{i}", 1, 1, i, 0, (4 if v > 10 else 1), (100 if v > 10 else ""), v])
    table = split_honeyform(pd.DataFrame(head + body, columns=cols),
                            source="src0", file_name="src0")

    eval_debug._eval_path()
    from eval_engine import config as ec
    from eval_engine.pipeline import (_rules, features, ingest, metrics, present,
                                      recommend, signatures, status)
    mods = (metrics, features, signatures, status, present, recommend, _rules)
    meta = ai_comment._session_meta(
        {"product_type": "MDDI", "product": "P", "lot_id": "L", "revision": "1.0"}, 1)
    raw_df = ai_comment._table_to_raw_df(table, ["ItemA"])
    ing = ingest.ingest({"meta": meta, "raw_df": raw_df}, persist=False)
    out = []
    for case in ing["cases"]:
        d = eval_debug._trace_case(case, ec.ENGINE_VERSION, mods, [160_000])
        d["source"], d["source_index"] = "src0", 0
        out.append(d)
    assert out, "트레이스 케이스가 0건 — 픽스처가 게이트를 못 넘었습니다"
    return out


def build_signatures_payload():
    from eval_panel import rules_io
    return rules_io.read_signatures("MDDI", None)


# ── (b)(c) 정상 경로 ─────────────────────────────────────────────────────────

def test_render_and_goto(cases, sigs):
    harness = (
        "<script>(function(){var out=[];"
        "var SIGP=" + js_literal(sigs) + ", CASES=" + js_literal(cases) + ";"
        "window.getJSON=function(u){ if(u.indexOf('api/signatures')===0)"
        "  return Promise.resolve(SIGP); return Promise.resolve({}); };"
        "window.Plotly={newPlot:function(){},purge:function(){}};"
        "TRACE={token:'t',session_id:'s1',cases:[]};"
        "try{ for(var i=0;i<CASES.length;i++) renderCase(CASES[i],null);"
        "     renderCase(CASES[0],{engine_comment_accepted:0,human_status:'MAJOR',"
        "       root_cause_category:'x',created_at:1700000000});"
        "     out.push('renderCase=OK'); }"
        "catch(e){ out.push('renderCase=FAIL '+e.message); }"
        "gotoSignature('SEVERE_OUTLIER').then(function(){out.push('goto=OK');},"
        " function(e){out.push('goto=FAIL '+e.message);}).then(function(){"
        "  out.push('sigListLen='+document.getElementById('sigList').innerHTML.length);"
        "  var pre=document.createElement('pre');pre.id='res';"
        "  pre.textContent=out.join(' || ');document.body.appendChild(pre);});"
        "})();</script>")
    res = run_probe(harness, "render_goto")
    assert "renderCase=OK" in res, res
    assert "goto=OK" in res, res
    length = int(re.search(r"sigListLen=(\d+)", res).group(1))
    assert length > 1000, f"Signatures 탭이 비었습니다: {res}"
    print(f"[b,c] renderCase + 룰 칩 클릭 경로 OK (sigList {length}자)")


# ── (d) 로더 실패 = 빈 화면 금지 ─────────────────────────────────────────────

def test_loader_failure_is_visible():
    """오류를 삼켜 빈 화면으로 두지 않는지 — 이번 신고의 직접 원인."""
    harness = (
        "<script>(function(){var out=[];"
        "window.getJSON=function(){return Promise.reject(new Error('HTTP 401'));};"
        "activateTab('pSignatures').then(function(){out.push('resolved');},"
        " function(e){out.push('rejected');}).then(function(){"
        "  var p=document.getElementById('pSignatures');"
        "  out.push('errbox='+(p.querySelector('.panel-error')?'YES':'NO'));"
        "  out.push('retry='+(p.querySelector('[data-retry]')?'YES':'NO'));"
        "  out.push('loaded='+loaded.has('pSignatures'));"
        "  var pre=document.createElement('pre');pre.id='res';"
        "  pre.textContent=out.join(' || ');document.body.appendChild(pre);});"
        "})();</script>")
    res = run_probe(harness, "loader_fail")
    assert "errbox=YES" in res, f"로더 실패가 화면에 안 뜹니다(빈 화면 회귀): {res}"
    assert "retry=YES" in res, f"재시도 버튼이 없습니다: {res}"
    assert "loaded=false" in res, \
        f"실패했는데 loaded 에 등록됐습니다 — 다시 눌러도 안 뜹니다: {res}"
    print("[d] 로더 실패 → 오류 표시 + 재시도 + 미등록 OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    test_dom_ids_resolve()
    if edge_path() is None:
        print("[b,c,d] SKIP — headless Edge 를 찾지 못했습니다 "
              f"(찾은 경로: {_EDGE_CANDIDATES})")
        print("\n부분 통과 (정적 검사만)")
        return
    cases, sigs = build_trace_cases(), build_signatures_payload()
    test_render_and_goto(cases, sigs)
    test_loader_failure_is_visible()
    print("\n전부 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

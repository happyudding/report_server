"""Bin 집계 헤더행 렌더 JS 회귀 — headless Edge 로 sheets.js / yield_issue.js 를 돌려 본다.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_yield_bin_agg_js.py

**왜 이 파일이 생겼나** (2026-08-25): Yield 탭·Issue Table 의 Bin 그룹을 펼칠 때 대표행
(숫자는 Bin 합계, 이름은 most-fail 항목)을 집계 헤더행으로 갈아끼우고 그 항목을 자기 실제
값을 가진 상세행으로 되돌렸다. 행이 **사라지거나 남의 자리에 서면** 에러가 아니라 "숫자가
조용히 틀린" 화면이 되므로 파이썬 테스트로는 못 잡는다 — 브라우저에서 실제 DOM 을 만들어
확인해야 한다. 특히 집계행에 저장 키(row_key)가 새면 기존 comment 가 고립된다.

검증하는 것:
  (a) Yield 탭 접힘 = 대표행 1줄(Bin 합계) — **종전과 동일**
  (b) Yield 탭 펼침 = 집계 헤더행 + 모든 TNO 행, most-fail 항목이 자기 값(0.2/2)으로 복원
  (c) 항목 1개 Bin 은 집계행도 토글도 없다 (사용자 확정)
  (d) Issue Table 집계행은 Map/Distribution 이 빈 칸(st-empty) — 미니셀이 빠져 행이 좁아진다
  (e) 집계행은 comment 저장 키가 없고(issueRowKey === "") Status 키는 갖는다
  (f) Issue Table Temp(대표행이 항목 행 자체)는 행 구성이 **불변**
  (g) 라벨 문자열이 파이썬 정본(web_report/yield_agg.py)과 **글자 그대로** 같다
  (h) 분할 JS 는 classic script 유지 (import/export 금지)
  (i) 웹 Excel Down 2경로가 화면 펼침과 **같은 행 구성**을 내보낸다 (사용자 확정)

Edge 가 없으면 정적 검사(g)(h)만 하고 나머지는 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_JS = _ROOT / "server" / "report" / "static" / "webreport"
_TMP = Path(tempfile.mkdtemp(prefix="wr_binagg_js_"))

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

from web_report.yield_agg import bin_agg_label  # noqa: E402

LABEL = bin_agg_label("15", 3)          # "BIN 15    (3 items)"


def edge_path():
    for p in _EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def js_literal(obj) -> str:
    """JSON 을 <script> 안에 안전하게 심는다 - `</` 만 끊어 조기 종료를 막는다."""
    return json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")


def run_probe(harness_js: str, name: str) -> dict:
    """core.js + sheets.js + yield_issue.js 를 인라인한 페이지를 돌리고 결과 JSON 반환.

    stdout 은 **파일로** 리다이렉트한다 - 파이프로 받으면 Windows 에서 빈 출력이 온다
    (test_webreport_sheets_js.py 와 같은 방식).
    """
    scripts = "".join(
        f"<script>{(_JS / n).read_text(encoding='utf-8')}</script>"
        for n in ("core.js", "sheets.js", "yield_issue.js", "excel_export.js"))
    # ⚠ DATA 를 스크립트보다 먼저 var 로 선언하면 core.js 의 let/const 와 충돌해 그 파일이
    # 통째로 SyntaxError 로 죽는다(= 전 항목이 "파싱 오류"로 위장 실패). 로드 뒤에 대입만 한다.
    html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            "<div class='content'></div>"
            + scripts
            + "<script>DATA={web_report:{}};</script>"
            + harness_js + "</body></html>")
    page = _TMP / f"{name}.html"
    page.write_text(html, encoding="utf-8")
    dump = _TMP / f"{name}.dom.txt"
    args = ",".join("'%s'" % a for a in (
        "--headless=new", "--disable-gpu", "--no-sandbox",
        "--virtual-time-budget=5000", "--dump-dom", page.as_uri()))
    ps = (f"Start-Process -FilePath '{edge_path()}' -ArgumentList @({args}) "
          f"-RedirectStandardOutput '{dump}' -NoNewWindow -Wait")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=180, check=False)
    raw = dump.read_text(encoding="utf-8", errors="replace") if dump.is_file() else ""
    m = re.search(r'<pre id="res">([\s\S]*?)</pre>', raw)
    assert m, f"{name}: 하네스가 실행되지 않았습니다 (스크립트 파싱 오류 의심)"
    text = (m.group(1).replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&amp;", "&"))
    return json.loads(text)


# ── 픽스처 (파이썬 테스트와 같은 상황: 100 die 중 TEST1 2 / TEST2 2 / TEST3 1) ──────

YIELD_GROUPS = [{
    "bin": "15",
    "rep": {"step": "P2", "bin": "15", "TNO": "100", "Item": "TEST1", "avg": 0.5,
            "S1_yield": 0.5, "S1_count": 5},
    "rows": [
        {"step": "P2", "bin": "15", "TNO": "100", "Item": "TEST1", "avg": 0.5,
         "S1_yield": 0.5, "S1_count": 5},
        {"step": "P2", "bin": "15", "TNO": "100", "Item": "TEST1", "avg": 0.2,
         "S1_yield": 0.2, "S1_count": 2},
        {"step": "P2", "bin": "15", "TNO": "101", "Item": "TEST2", "avg": 0.2,
         "S1_yield": 0.2, "S1_count": 2},
        {"step": "P2", "bin": "15", "TNO": "102", "Item": "TEST3", "avg": 0.1,
         "S1_yield": 0.1, "S1_count": 1},
    ],
}, {
    "bin": "7",
    "rep": {"step": "P2", "bin": "7", "TNO": "200", "Item": "SOLO", "avg": 0.3,
            "S1_yield": 0.3, "S1_count": 3},
    "rows": [
        {"step": "P2", "bin": "7", "TNO": "200", "Item": "SOLO", "avg": 0.3,
         "S1_yield": 0.3, "S1_count": 3},
        {"step": "P2", "bin": "7", "TNO": "200", "Item": "SOLO", "avg": 0.3,
         "S1_yield": 0.3, "S1_count": 3},
    ],
}]

ISSUE_ROWS = [
    {"Category": "Yield", "Step": "P2", "Bin": "15", "TNO": "100", "Item": "TEST1",
     "avg": 0.5, "S1_yield": 0.5, "Map": "", "Distribution": "", "Status": "Open",
     "PTE comment": "메인", "개발 comment": "",
     "_grp": "y0", "_detail": False, "_ndetail": 3},
    {"Category": "", "Step": "P2", "Bin": "15", "TNO": "100", "Item": "TEST1",
     "avg": 0.2, "S1_yield": 0.2, "Map": "", "Distribution": "", "Status": "",
     "PTE comment": "메인", "개발 comment": "", "_grp": "y0", "_detail": True},
    {"Category": "", "Step": "P2", "Bin": "15", "TNO": "101", "Item": "TEST2",
     "avg": 0.2, "S1_yield": 0.2, "Map": "", "Distribution": "", "Status": "",
     "PTE comment": "", "개발 comment": "", "_grp": "y0", "_detail": True},
    {"Category": "", "Step": "P2", "Bin": "15", "TNO": "102", "Item": "TEST3",
     "avg": 0.1, "S1_yield": 0.1, "Map": "", "Distribution": "", "Status": "",
     "PTE comment": "", "개발 comment": "", "_grp": "y0", "_detail": True},
]

# Issue Table Temp 모양 — 대표행이 **항목 행 자체**라 집계 개념이 없다.
# ⚠ Category 를 든 행은 sheets.js emitRows 가 섹션 divider 로 보고 건너뛴다(ETC/TEMP/CMPETC).
# 그래서 라벨 행을 따로 두고 데이터 행은 Category 를 비운다 — 실제 payload 와 같은 모양.
TEMP_ROWS = [
    {"Category": "TEMP", "Step": "", "Bin": "", "TNO": "", "Item": "",
     "avg": "", "S1_yield": "", "Map": "", "Distribution": "", "Status": "",
     "PTE comment": "", "개발 comment": ""},
    {"Category": "", "Step": "", "Bin": "3", "TNO": "", "Item": "ITEM_A",
     "avg": 1.0, "S1_yield": 1.0, "Map": "", "Distribution": "", "Status": "Open",
     "PTE comment": "", "개발 comment": "", "_grp": "t0", "_detail": False, "_ndetail": 2},
    {"Category": "", "Step": "", "Bin": "3", "TNO": "", "Item": "ITEM_B", "avg": 0.5,
     "S1_yield": 0.5, "Map": "", "Distribution": "", "Status": "",
     "PTE comment": "", "개발 comment": "", "_grp": "t0", "_detail": True},
    {"Category": "", "Step": "", "Bin": "3", "TNO": "", "Item": "ITEM_C", "avg": 0.2,
     "S1_yield": 0.2, "Map": "", "Distribution": "", "Status": "",
     "PTE comment": "", "개발 comment": "", "_grp": "t0", "_detail": True},
]


# ── 정적 검사 (Edge 없이도 돈다) ─────────────────────────────────────────────

def test_no_es_module():
    """분할 JS 는 classic script 순서 로드다 - import/export 를 쓰면 전부 죽는다."""
    for name in ("sheets.js", "yield_issue.js", "edit_mode.js", "excel_export.js"):
        src = (_JS / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|export)\s", src, re.M), f"{name}: ES module 금지"
    print("[h] classic script 유지 OK")


def test_label_parity_with_python():
    """라벨은 파이썬(web_report/yield_agg.py)이 정본 - JS 사본이 글자 그대로 같아야 한다."""
    src = (_JS / "yield_issue.js").read_text(encoding="utf-8")
    m = re.search(r"function binAggLabel\(binValue, nItems\) \{\s*return `([^`]*)`", src)
    assert m, "yield_issue.js 의 binAggLabel 을 찾지 못했습니다"
    js_tpl = m.group(1)
    py_tpl = bin_agg_label("${binValue}", "${nItems}")
    assert js_tpl == py_tpl, f"라벨 서식 불일치\n  JS: {js_tpl!r}\n  PY: {py_tpl!r}"
    # 집계행 TNO 표기도 짝
    assert 'const BIN_AGG_TNO = "-"' in src, "BIN_AGG_TNO 표기가 파이썬과 어긋납니다"
    # 공백이 접히지 않도록 CSS 가 붙어 있어야 한다 (라벨의 공백 4칸이 이 규칙에 기댄다)
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    assert "td.bin-agg-item" in view and "white-space: pre" in view, \
        "집계행 Item 셀의 white-space:pre 규칙이 없습니다 (라벨 공백이 접힙니다)"
    print("[g] 라벨/TNO 표기 파이썬 정본과 일치 OK - %r" % LABEL)


# ── (a)~(c) Yield 탭 ────────────────────────────────────────────────────────

def test_yield_table_rows():
    harness = (
        "<script>(function(){var out={};try{"
        "var groups=" + js_literal(YIELD_GROUPS) + ";"
        "var cols=['step','bin','TNO','Item','S1_yield','S1_count','avg'];"
        "var box=document.createElement('div');"
        "box.innerHTML=renderYieldTable(cols,groups,0,null);"
        "document.body.appendChild(box);"
        "function rowOf(tr){return Array.prototype.map.call(tr.cells,function(td){"
        "  return td.textContent.replace(/[\\u25b2\\u25bc]/g,'').trim();});}"
        "var trs=Array.prototype.slice.call(box.querySelectorAll('tbody tr'));"
        "out.rows=trs.map(function(tr){return {cls:tr.className,"
        "  hidden:tr.style.display==='none',grp:tr.dataset.grp,cells:rowOf(tr)};});"
        "out.toggles=box.querySelectorAll('.yield-toggle').length;"
        "out.togglesG0=box.querySelectorAll('.yield-toggle[data-grp=\"0_0\"]').length;"
        "out.togglesG1=box.querySelectorAll('.yield-toggle[data-grp=\"0_1\"]').length;"
        "out.aggItemPre=!!box.querySelector('tr.yield-bin-agg td.bin-agg-item');"
        "out.aggHasLink=!!box.querySelector('tr.yield-bin-agg .item-detail-link');"
        "}catch(e){out.error=e.message;}"
        "var pre=document.createElement('pre');pre.id='res';"
        "pre.textContent=JSON.stringify(out);document.body.appendChild(pre);})();</script>")
    r = run_probe(harness, "yield")
    assert not r.get("error"), r["error"]
    rows = r["rows"]

    # (a) 접힘 기본 — 보이는 행은 Bin 별 대표행 2개뿐이고 값은 종전(Bin 합계) 그대로
    visible = [x for x in rows if not x["hidden"]]
    assert len(visible) == 2, [x["cells"] for x in visible]
    assert "yield-bin-rep" in visible[0]["cls"] and "has-agg" in visible[0]["cls"]
    assert visible[0]["cells"][:4] == ["P2", "15", "100", "TEST1"], visible[0]["cells"]
    assert visible[0]["cells"][4:7] == ["0.5", "5", "0.5"], visible[0]["cells"]
    # (c) 항목 1개 Bin 은 has-agg 가 붙지 않는다
    assert "has-agg" not in visible[1]["cls"], visible[1]["cls"]
    assert visible[1]["cells"][:4] == ["P2", "7", "200", "SOLO"], visible[1]["cells"]
    print("[a] Yield 접힘 = 대표행만, 값 종전 유지 OK")

    # (b) 펼침 구성 — 집계 헤더행 + TNO 3행, most-fail 이 자기 값으로 복원
    g0 = [x for x in rows if x["grp"] == "0_0"]
    assert len(g0) == 5, [x["cells"] for x in g0]      # rep + agg + detail 3
    agg = g0[1]
    assert "yield-bin-agg" in agg["cls"] and agg["hidden"] is True
    assert agg["cells"][:4] == ["P2", "15", "-", LABEL], agg["cells"]
    assert agg["cells"][4:7] == ["0.5", "5", "0.5"], agg["cells"]
    details = g0[2:]
    assert [d["cells"][3] for d in details] == ["TEST1", "TEST2", "TEST3"], details
    assert all("yield-bin-detail" in d["cls"] for d in details)
    assert details[0]["cells"][4:7] == ["0.2", "2", "0.2"], details[0]["cells"]
    print("[b] Yield 펼침 = 집계행 + 전 TNO, TEST1 복원(0.2/2) OK")

    # (c) 토글: 항목 여럿인 Bin 만 2개(rep+agg), 항목 1개 Bin 은 0개
    assert r["togglesG0"] == 2 and r["togglesG1"] == 0, r
    assert r["toggles"] == 2
    g1 = [x for x in rows if x["grp"] == "0_1"]
    assert len(g1) == 1, [x["cells"] for x in g1]
    print("[c] 항목 1개 Bin 은 집계행·토글 없음 OK")

    # 집계행 Item 은 라벨이라 상세 링크를 걸지 않고, 공백 유지 클래스가 붙는다
    assert r["aggItemPre"] is True, "집계행 Item 셀에 bin-agg-item 이 없습니다"
    assert r["aggHasLink"] is False, "집계행 Item 에 Item_detail 링크가 걸렸습니다"
    print("[b2] 집계행 Item = 라벨(링크 없음) + 공백 유지 클래스 OK")


# ── (d)~(f) Issue Table ─────────────────────────────────────────────────────

def test_issue_table_rows():
    harness = (
        "<script>(function(){var out={};try{"
        "DATA.web_report={distribution_index:{subjects:[]}};"
        # 미니셀 판정이 다른 파일(wafer_charts.js/distribution.js)의 전역을 부른다 —
        # 이 하네스는 sheets.js 만 검증하므로 최소 스텁으로 대신한다.
        "window.issueBinMaps=function(){return [];};"
        "window.webReportSourceCount=function(){return 1;};"
        "window.tempSourceCount=function(){return 0;};"
        "window.distHasData=function(){return true;};"
        "var rows=" + js_literal(ISSUE_ROWS) + ";"
        "var temp=" + js_literal(TEMP_ROWS) + ";"
        "var box=document.createElement('div');"
        "box.innerHTML=renderSheetTable(rows,{kind:'issue'});"
        "document.body.appendChild(box);"
        "var trs=Array.prototype.slice.call(box.querySelectorAll('tbody tr'))"
        "  .filter(function(tr){return !/issue-shead/.test(tr.className);});"
        "out.rows=trs.map(function(tr){return {cls:tr.className,"
        "  hidden:tr.style.display==='none',"
        "  cells:Array.prototype.map.call(tr.cells,function(td){"
        "    return td.textContent.replace(/[\\u25b2\\u25bc]/g,'').trim();}),"
        "  empties:Array.prototype.map.call(tr.cells,function(td){"
        "    return td.className.indexOf('st-empty')>=0;})};});"
        "out.head=Array.prototype.map.call("
        "  box.querySelectorAll('tbody tr')[0].cells,function(td){return td.textContent.trim();});"
        # (아래는 파이썬 주석) 저장 키 규약 - 집계행에 comment 키가 새면 코멘트가 고립된다
        "var agg={Bin:'15',Item:" + js_literal(LABEL) + ",_agg:true,_grp:'y0',_detail:false};"
        "out.aggRowKey=issueRowKey(agg,'Yield');"
        "out.aggStatusKey=issueHideStatusKey(agg,'Yield');"
        "out.repRowKey=issueRowKey(rows[0],'Yield');"
        # (아래는 파이썬 주석) Issue Table Temp 표는 손대지 않는다
        "var tbox=document.createElement('div');"
        "tbox.innerHTML=renderSheetTable(temp,{kind:'issue'});"
        "out.tempAll=Array.prototype.map.call(tbox.querySelectorAll('tbody tr'),"
        "  function(tr){return (tr.className||'(none)')+'::'+tr.cells[3].textContent.trim();});"
        "out.tempRows=Array.prototype.slice.call(tbox.querySelectorAll('tbody tr'))"
        "  .filter(function(tr){return !/issue-shead/.test(tr.className);})"
        "  .map(function(tr){return tr.className;});"
        "out.allRows=Array.prototype.map.call(box.querySelectorAll('tbody tr'),"
        "  function(tr){return (tr.className||'(none)');});"
        "out.tempAgg=tbox.querySelectorAll('tr.issue-bin-agg').length;"
        "out.toggles=box.querySelectorAll('.issue-toggle[data-grp=\"y0\"]').length;"
        "out.aggToggleUp=(function(b){return b?b.getAttribute('aria-expanded'):'';})"
        "  (box.querySelector('tr.issue-bin-agg .issue-toggle'));"
        "}catch(e){out.error=e.message+' | '+(e.stack||'');}"
        "var pre=document.createElement('pre');pre.id='res';"
        "pre.textContent=JSON.stringify(out);document.body.appendChild(pre);})();</script>")
    r = run_probe(harness, "issue")
    assert not r.get("error"), r["error"]
    rows = r["rows"]
    assert len(rows) == 5, [x["cells"] for x in rows]     # rep + agg + detail 3

    rep, agg, details = rows[0], rows[1], rows[2:]
    assert "issue-bin-rep" in rep["cls"] and "has-agg" in rep["cls"], rep["cls"]
    assert rep["hidden"] is False
    assert "issue-bin-agg" in agg["cls"] and agg["hidden"] is True, agg["cls"]

    head = r["head"]
    idx = {name: i for i, name in enumerate(head)}
    for want in ("Map", "Distribution", "Item", "TNO"):
        assert want in idx, head

    # (d) 집계행의 Map/Distribution 은 빈 칸 — 미니셀이 빠져 행이 좁아진다
    assert agg["empties"][idx["Map"]] is True, agg
    assert agg["empties"][idx["Distribution"]] is True, agg
    assert agg["cells"][idx["Item"]] == LABEL, agg["cells"]
    assert agg["cells"][idx["TNO"]] == "-", agg["cells"]
    # 상세행은 반대로 미니셀 칸이 살아 있다(Map 은 Bin 이 있으므로)
    assert details[0]["empties"][idx["Map"]] is False, details[0]
    print("[d] 집계행 Map/Distribution 빈 칸, 상세행은 유지 OK")

    # 상세행 3개가 각자 자기 값 — TEST1 복원
    assert [d["cells"][idx["Item"]] for d in details] == ["TEST1", "TEST2", "TEST3"]
    print("[d2] Issue 상세 3행 복원 OK")

    # (e) 저장 키 — 집계행은 comment 키 없음 / Status 키는 있음
    assert r["aggRowKey"] == "", r["aggRowKey"]
    assert r["aggStatusKey"] == "Yield|15", r["aggStatusKey"]
    assert r["repRowKey"] == "Yield|15|TEST1", r["repRowKey"]
    print("[e] 집계행 comment 키 없음 / Status 키 Yield|15 OK")

    # 토글은 rep·agg 양쪽에 — 둘이 상호 배타로 보이므로 접힘에선 ▼, 펼침에선 ▲ 가 보인다.
    assert r["toggles"] == 2, r["toggles"]
    assert r["aggToggleUp"] == "true", r["aggToggleUp"]
    print("[d3] 토글 rep+agg 양쪽, 집계행 토글은 펼침 상태(▲) OK")

    # (f) Issue Table Temp 는 불변 (대표행 1 + 상세 2, 집계행 0)
    assert r["tempAgg"] == 0, r["tempRows"]
    assert len(r["tempRows"]) == 3, (r["tempRows"], r.get("tempAll"), r.get("allRows"))
    assert "issue-bin-rep" in r["tempRows"][0] and "has-agg" not in r["tempRows"][0]
    print("[f] Issue Table Temp 행 구성 불변 OK")


# ── (i) Excel 내보내기 행 구성 = 화면 펼침과 동일 ────────────────────────────

def test_excel_export_rows():
    """웹 Excel Down 2경로가 화면 펼침과 같은 행 구성을 내보내는지 (사용자 확정).

    순수 빌더(buildYieldSheetData / buildIssueSheetData)만 부른다 - ExcelJS·DOM 무관.
    """
    harness = (
        "<script>(function(){var out={};try{"
        "window.issueBinMaps=function(){return [];};"
        "window.webReportSourceCount=function(){return 1;};"
        "window.tempSourceCount=function(){return 0;};"
        "window.distHasData=function(){return true;};"
        "var report={sources:[{name:'S1'}],yield_summary:{by_step:[]},"
        "  yield_step_groups:[{step:'P2',groups:" + js_literal(YIELD_GROUPS) + "}],sheets:{}};"
        "var y=buildYieldSheetData(report);"
        "out.yieldRows=y.sections[0].rows.map(function(r){return r.slice(0,7);});"
        "var i=buildIssueSheetData(" + js_literal(ISSUE_ROWS) + ",['S1']);"
        "out.issueHeader=i.header;"
        "out.issueRows=i.rows.map(function(r){return r.slice(0,12);});"
        "}catch(e){out.error=e.message+' | '+(e.stack||'');}"
        "var pre=document.createElement('pre');pre.id='res';"
        "pre.textContent=JSON.stringify(out);document.body.appendChild(pre);})();</script>")
    r = run_probe(harness, "excel")
    assert not r.get("error"), r["error"]

    # Yield 시트 — [step, bin, TNO, Item, avg, S1 (%), S1 count]
    yr = r["yieldRows"]
    # 집계행 + TNO 3 + 항목 1개 Bin 1행 (Pass 행은 by_step 이 비어 생략된다)
    assert len(yr) == 5, yr
    assert yr[0][2] == "-" and yr[0][3] == LABEL, yr[0]
    assert yr[0][4] == 0.5 and yr[0][6] == 5, yr[0]
    assert [x[3] for x in yr[1:4]] == ["TEST1", "TEST2", "TEST3"], yr
    assert yr[1][4] == 0.2 and yr[1][6] == 2, yr[1]     # TEST1 자기 값
    assert yr[4][3] == "SOLO", yr[4]                    # 항목 1개 Bin 은 그대로 1행
    print("[i] Yield Excel = 집계행 + 전 TNO, TEST1 자기 값 OK")

    # Issue 시트 — 접힘 대표행은 빠지고 집계행 1 + 상세 3
    ir = r["issueRows"]
    idx = {n: k for k, n in enumerate(r["issueHeader"])}
    assert len(ir) == 4, ir
    assert ir[0][idx["Item"]] == LABEL and ir[0][idx["TNO"]] == "-", ir[0]
    assert [x[idx["Item"]] for x in ir[1:]] == ["TEST1", "TEST2", "TEST3"], ir
    assert ir[1][idx["avg"]] == 0.2, ir[1]
    # comment 는 첫 TNO 행이 갖고, 집계행에는 남지 않는다(대표행 합치기 폐지)
    assert ir[0][idx["PTE comment"]] == "", ir[0]
    assert ir[1][idx["PTE comment"]] == "메인", ir[1]
    print("[i2] Issue Excel = 집계행 + 전 TNO, comment 는 첫 TNO 행 OK")


if __name__ == "__main__":
    test_no_es_module()
    test_label_parity_with_python()
    if not edge_path():
        print("\n[SKIP] Edge 를 찾지 못해 렌더 검사는 건너뜁니다 (정적 검사만 수행)")
        sys.exit(0)
    test_yield_table_rows()
    test_issue_table_rows()
    test_excel_export_rows()
    print("\n전부 통과")

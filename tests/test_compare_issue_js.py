"""Issue Table Compare 탭 JS 회귀 — headless Edge (2026-08-20).

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_compare_issue_js.py

**왜 파이썬 테스트로 안 되나**: 이 탭이 깨지는 방식은 대부분 화면에서만 드러난다.
  · row_key 접두(CMPDIST|/CMPETC|)를 프런트가 서버와 다르게 만들면 comment/Status 가
    조용히 다른 키로 저장된다 — 에러 없이 "적었는데 사라졌다"가 된다(CLAUDE.md 규칙 #12).
  · Log 카테고리는 **변경 행만** 이라는 규칙이 뒤집히면 수백 행이 쏟아져 표가 못 쓰게 된다.
  · 하단 표 코멘트(bm:/gl:)는 Map 비교·Log 비교 탭과 **같은 키**를 써야 동기화된다.

검증하는 것:
  (a) issueRowKey/issueHideStatusKey 가 CMPDIST/CMPETC 섹션 키에 맞는 저장 키를 만든다
  (b) 기존 섹션(Yield/CPK/TEMP/ETC) 키는 **한 글자도** 안 바뀐다(회귀 0)
  (c) Log 표 = 추가/삭제/Limit 변경 행만 (Gap% 만 큰 행은 제외)
  (d) 하단 두 표의 Comment 셀이 bm:<x>,<y> / gl:<after>\\x1f<before> 키를 쓴다
  (e) Compare 모드가 아니면 Summary 의 Compare 카드가 없고, 맞으면 서버 수치를 그대로 쓴다
  (f) ENGR Comment 칸이 Compare 모드에서 compare 키를 포함한다
  (g) 정적: classic script 유지 + report_view.html 로드 등록/순서 + 탭 버튼·패널 존재

Edge 가 없으면 정적 검사만 하고 나머지는 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from html import unescape
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "server" / "report" / "static" / "webreport"
_TMP = Path(tempfile.mkdtemp(prefix="wr_cmpiss_js_"))

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

SEP = "\x1f"


def edge_path():
    for p in _EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def js_literal(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")


# 결과를 담는 <pre> 는 **textContent 로만** 채운다 — HTML 문자열을 값으로 담는 하네스가
# 있어(요약 카드) innerHTML 로 넣으면 브라우저가 그 마크업을 실제 DOM 으로 파싱해 버린다.
_EMIT = ("<script>function _emit(o){var p=document.createElement('pre');"
         "p.id='res';p.textContent=JSON.stringify(o);document.body.appendChild(p);}</script>")


def run_probe(scripts, body_html, harness_js, name) -> str:
    """지정 JS 를 인라인한 페이지를 돌리고 `_emit()` 이 남긴 JSON 을 반환.

    stdout 은 **파일로** 리다이렉트한다 — 파이프로 받으면 Windows 에서 빈 출력이 온다.
    """
    tags = "".join(f"<script>{(_JS / n).read_text(encoding='utf-8')}</script>"
                   for n in scripts)
    html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            + body_html + tags + _EMIT + harness_js + "</body></html>")
    page = _TMP / f"{name}.html"
    page.write_text(html, encoding="utf-8")
    dump = _TMP / f"{name}.dom.txt"
    # msedge 는 python subprocess 의 파일 stdout 으로는 **아무것도 쓰지 않는다**(파이프도
    # 마찬가지 — 실측 0 bytes). PowerShell Start-Process -RedirectStandardOutput 만 동작한다.
    args = ",".join("'%s'" % a for a in (
        "--headless=new", "--disable-gpu", "--no-sandbox",
        "--virtual-time-budget=5000", "--dump-dom", page.as_uri()))
    ps = (f"Start-Process -FilePath '{edge_path()}' -ArgumentList @({args}) "
          f"-RedirectStandardOutput '{dump}' -NoNewWindow -Wait")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=180, check=False)
    raw = dump.read_text(encoding="utf-8", errors="replace") if dump.is_file() else ""
    # **마지막** 매치를 쓴다 — 앞쪽에는 하네스 <script> 소스 자체가 그대로 들어 있어
    # 그 안의 문자열 리터럴이 먼저 걸릴 수 있다.
    found = re.findall(r'<pre id="res">([\s\S]*?)</pre>', raw)
    assert found, f"{name}: 하네스가 실행되지 않았습니다 (스크립트 파싱 오류 의심)"
    # --dump-dom 은 텍스트 노드의 &<> 를 엔티티로 직렬화한다 — HTML 문자열을 값으로
    # 담은 결과(JSON 안의 마크업)를 되읽으려면 되돌려야 한다.
    return unescape(found[-1]).strip()


# goodlog 행 4종 — 추가 / 삭제 / Limit 변경 / Gap% 만 큰 정상 행.
GOODLOG = {
    "after_source": "WF_A", "before_source": "WF_B", "identical": False,
    "rows": [
        {"after_item_name": "NEW_ITEM", "after_lolimit": 0, "after_hilimit": 10,
         "after_unit": "V", "after_value": "5", "compare_item_name": None,
         "compare_lolimit": None, "compare_hilimit": None, "comment": "", "gap": None,
         "before_item_name": "", "before_lolimit": None, "before_hilimit": None,
         "before_unit": "", "before_value": ""},
        {"after_item_name": "", "after_lolimit": None, "after_hilimit": None,
         "after_unit": "", "after_value": "", "compare_item_name": None,
         "compare_lolimit": None, "compare_hilimit": None, "comment": "", "gap": None,
         "before_item_name": "GONE_ITEM", "before_lolimit": 0, "before_hilimit": 10,
         "before_unit": "V", "before_value": "5"},
        {"after_item_name": "LIM_ITEM", "after_lolimit": 0, "after_hilimit": 9.5,
         "after_unit": "V", "after_value": "5", "compare_item_name": True,
         "compare_lolimit": True, "compare_hilimit": False, "comment": "", "gap": 1.0,
         "before_item_name": "LIM_ITEM", "before_lolimit": 0, "before_hilimit": 10,
         "before_unit": "V", "before_value": "4.95"},
        {"after_item_name": "GAPPY", "after_lolimit": 0, "after_hilimit": 10,
         "after_unit": "V", "after_value": "8", "compare_item_name": True,
         "compare_lolimit": True, "compare_hilimit": True, "comment": "", "gap": 60.0,
         "before_item_name": "GAPPY", "before_lolimit": 0, "before_hilimit": 10,
         "before_unit": "V", "before_value": "5"},
    ],
    "limit_change_map": {"LIM_ITEM": [None, 10]},
}
BIN_MATRIX = {
    "sources": ["WF_A", "WF_B"], "before_sources": ["WF_B"], "after_sources": ["WF_A"],
    "rep_before": "WF_B", "rep_after": "WF_A",
    "rows": [{"x": 3, "y": 7, "bins": ["5", "1"]}],
    "counts": {"common_dies": 100, "mismatch": 1, "pass_to_fail": 1, "fail_to_pass": 0},
}
COMPARE = {
    "sources": ["WF_A", "WF_B"], "groups": {"WF_A": "after", "WF_B": "before"},
    "before_sources": ["WF_B"], "after_sources": ["WF_A"],
    "goodlog": GOODLOG, "bin_matrix": BIN_MATRIX,
    "dist_shift": {"after": "WF_A", "before": "WF_B", "rows": [],
                   "thresholds": {"cpk_low": 1.33, "cpk_high": 100,
                                  "stdev_delta_pct": 15, "alpha": 0.05},
                   "summary": {"total": 12, "focus": 4}},
    "new_items": ["NEW_ITEM"],
    "common_map": {"x_min": None, "counts": {}},
    "equivalence": {"rows": [], "summary": {}},
}


# ── 정적 검사 (Edge 없이도 돈다) ─────────────────────────────────────────────

def test_no_es_module():
    """분할 JS 는 classic script 순서 로드다 — import/export 를 쓰면 전부 죽는다."""
    src = (_JS / "compare_issue.js").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\s", src, re.M), "compare_issue.js: ES module 금지"
    print("[정적] classic script 유지 OK")


def test_registered():
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    assert "static/webreport/compare_issue.js" in view, "compare_issue.js 가 로드되지 않았습니다"
    # compare.js(표 헬퍼)와 yield_issue.js(표 렌더 본체)를 재사용하므로 그 뒤여야 한다.
    for dep in ("compare.js", "yield_issue.js", "core.js"):
        assert view.index(f"webreport/{dep}") < view.index("webreport/compare_issue.js"), \
            f"compare_issue.js 는 {dep} 뒤에 로드돼야 합니다"
    assert 'data-tab="issue-cmp"' in view, "탭 버튼이 없습니다"
    assert 'id="panel-issue-cmp"' in view, "패널 div 가 없습니다"
    # 이슈 표는 탭 패널이 아니라 ISSUE_TABLE **서브패널**에 들어간다(2026-08-27 서브탭 흡수)
    # — renderIssueTableInto 가 대상 div 의 innerHTML 을 통째로 갈아치우기 때문이다.
    assert 'id="panel-issue-cmp-table"' in view, "ISSUE_TABLE 서브패널 id 가 없습니다"
    # 이슈 표 CSS 는 패널 id 로 걸려 있다 — 새 패널이 빠지면 sticky/삭제모드가 조용히 깨진다.
    assert view.count("#panel-issue-cmp") >= 10, \
        "이슈 표 CSS 셀렉터에 #panel-issue-cmp 가 충분히 반영되지 않았습니다"
    print("[정적] 로드 순서 + 탭/패널 + CSS 셀렉터 OK")


def test_compare_tab_absorbed():
    """구 최상위 Compare 탭이 완전히 제거됐는지 (2026-08-27 서브탭 흡수)."""
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    assert 'data-tab="compare"' not in view, "구 Compare 탭 버튼이 남아 있습니다"
    assert 'id="panel-compare"' not in view, "구 Compare 패널 div 가 남아 있습니다"
    cmp_js = (_JS / "compare.js").read_text(encoding="utf-8")
    assert "function renderCompare()" not in cmp_js, "renderCompare 가 남아 있습니다"
    assert '"panel-compare"' not in cmp_js, "compare.js 에 panel-compare 참조가 남아 있습니다"
    edit = (_JS / "edit_mode.js").read_text(encoding="utf-8")
    assert '"compare": renderCompare' not in edit, "TAB_RENDERERS 에 compare 가 남아 있습니다"
    tabs = (_JS / "tabs_topbar.js").read_text(encoding="utf-8")
    assert 'data-tab="compare"' not in tabs, "syncTabVisibility 에 compare 노출 규칙이 남아 있습니다"
    print("[정적] 구 Compare 탭 완전 제거 OK")


def test_subtabs_markup():
    """서브탭 5개가 요구 순서대로 있어야 한다 (ISSUE_TABLE→MAP→LOG→TESTTIME→동일성)."""
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    keys = re.findall(r'data-cmpsub="([a-z]+)"', view)
    assert keys == ["table", "map", "log", "ttime", "equiv"], \
        f"서브탭 순서가 기대와 다릅니다: {keys}"
    panels = re.findall(r'data-cmppanel="([a-z]+)"', view)
    assert set(panels) == set(keys), f"서브패널이 서브탭과 짝이 안 맞습니다: {panels}"
    core = (_JS / "core.js").read_text(encoding="utf-8")
    assert 'ISSUE_PANEL_CMP = "panel-issue-cmp-table"' in core, \
        "core.js ISSUE_PANEL_CMP 가 서브패널을 가리키지 않습니다"
    assert "#panel-issue-cmp-table" in core, "ISSUE_PANEL_SEL 이 서브패널을 가리켜야 합니다"
    # MAP비교가 Plotly 공통성 Map 을 그리므로 도착 대기가 필요하다.
    edit = (_JS / "edit_mode.js").read_text(encoding="utf-8")
    assert re.search(r'PLOTLY_TABS\s*=\s*\{[^}]*"issue-cmp"', edit), \
        "PLOTLY_TABS 에 issue-cmp 가 없습니다 (MAP비교가 빈 맵으로 남는다)"
    print("[정적] 서브탭 5개 순서 + core/PLOTLY_TABS 등록 OK")


def test_dist_data_ensured_for_compare():
    """Compare 이슈 표의 Distribution 미니셀은 ensureDistData 선행이 필요하다."""
    tabs = (_JS / "tabs_topbar.js").read_text(encoding="utf-8")
    body = tabs[tabs.index("function ensureTabData"):]
    body = body[:body.index("\n}")]
    assert '"issue-cmp"' in body, "ensureTabData 에 issue-cmp 가 없습니다 (미니셀이 빈다)"
    # Map 계열은 받지 않는다 — Compare 표에는 Map 컬럼이 없어 수백만 die 를 헛받는다.
    map_line = [l for l in body.splitlines() if "needMap" in l and "=" in l]
    assert map_line and "issue-cmp" not in map_line[0], \
        "issue-cmp 가 Map 데이터까지 받고 있습니다 (불필요한 대용량 다운로드)"
    print("[정적] ensureTabData: issue-cmp 는 dist 만 OK")


def test_panel_registered_in_core():
    """core.js ISSUE_PANEL_SEL 에 새 패널이 들어가야 편집·검색·미니셀이 함께 돈다."""
    src = (_JS / "core.js").read_text(encoding="utf-8")
    assert "#panel-issue-cmp" in src, "core.js ISSUE_PANEL_SEL 에 새 패널이 없습니다"
    assert "ISSUE_CMP_SHEET" in src, "core.js 에 Compare 시트 이름 상수가 없습니다"
    tabs = (_JS / "tabs_topbar.js").read_text(encoding="utf-8")
    assert 'data-tab="issue-cmp"' in tabs, "tabs_topbar.js 에 탭 노출 규칙이 없습니다"
    edit = (_JS / "edit_mode.js").read_text(encoding="utf-8")
    assert '"issue-cmp"' in edit, "edit_mode.js TAB_RENDERERS 에 등록되지 않았습니다"
    print("[정적] core/tabs_topbar/edit_mode 등록 OK")


def test_dist_subtab_removed():
    """구 '산포 비교' 서브탭과 그 전용 코드가 남아 있지 않아야 한다(사장 코드)."""
    src = (_JS / "compare.js").read_text(encoding="utf-8")
    assert 'data-cmpsub="dist"' not in src, "산포 비교 서브탭 버튼이 남아 있습니다"
    assert "cmpDist" not in src, "cmpDist* 함수군이 남아 있습니다"
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    assert "cmpDistQueueRender" not in dist, "distribution.js 에 죽은 참조가 남아 있습니다"
    print("[정적] 구 산포 비교 서브탭 제거 OK")


# ── (a)~(f) 브라우저 실행 ────────────────────────────────────────────────────

def test_row_keys():
    """(a)(b) 섹션 키 → 저장 키. 신규 2종이 붙고 기존 4종은 불변."""
    harness = (
        "<script>(function(){var out={};"
        "var r = {Item:'ITEM_A', Bin:'20', _grp:'y0'};"
        "out.key = {};out.hkey = {};"
        "['Yield','CPK','TEMP','ETC','CMPDIST','CMPETC'].forEach(function(sec){"
        "  out.key[sec] = issueRowKey(r, sec);"
        "  out.hkey[sec] = issueHideStatusKey(r, sec);"
        "});"
        "out.emptyItem = issueRowKey({Item:'  '}, 'CMPDIST');"
        "_emit(out);})();</script>")
    out = json.loads(run_probe(["core.js", "sheets.js"], "", harness, "rowkeys"))
    assert out["key"]["CMPDIST"] == "CMPDIST|ITEM_A"
    assert out["key"]["CMPETC"] == "CMPETC|ITEM_A"
    assert out["hkey"]["CMPDIST"] == "CMPDIST|ITEM_A"
    assert out["hkey"]["CMPETC"] == "CMPETC|ITEM_A"
    # 기존 4종 회귀 0
    assert out["key"]["Yield"] == "Yield|20|ITEM_A"
    assert out["key"]["CPK"] == "CPK|ITEM_A"
    assert out["key"]["TEMP"] == "TEMP|ITEM_A"
    assert out["key"]["ETC"] == "ETC|ITEM_A"
    assert out["hkey"]["Yield"] == "Yield|20"
    assert out["emptyItem"] == "", "Item 이 비면 키를 만들지 않아야 합니다"
    print("[a][b] row_key/숨김키 — 신규 2종 추가 + 기존 4종 불변 OK")


def test_log_and_bin_tables():
    """(c)(d) Log 표는 변경 행만, 두 표의 Comment 키는 compare_note 규약.

    2026-08-27 서브탭 흡수로 두 표의 자리가 갈렸다 — Log 요약표는 LOG비교 서브탭 상단
    (cmpLogPanelHtml), Bin 비교표는 MAP비교 서브탭(cmpMapPanelHtml). 둘 다 compare.js 가
    만든다. 저장 키(bm:/gl:)는 불변이어야 한다.
    """
    harness = (
        "<script>(function(){var out={};"
        "DATA = {compare_notes:{}, web_report:{compare:" + js_literal(COMPARE) + "}};"
        "MODE = 'edit';"
        "var cmp = DATA.web_report.compare;"
        "document.body.insertAdjacentHTML('beforeend',"
        "  '<div id=\"plog\">'+cmpLogPanelHtml(cmp)+'</div>'+"
        "  '<div id=\"pmap\">'+cmpMapPanelHtml(cmp)+'</div>');"
        "var log = document.getElementById('plog');"
        "var map = document.getElementById('pmap');"
        "out.logItems = [].map.call(log.querySelectorAll('table tr.gl-row td:nth-child(2)'),"
        "  function(td){return td.textContent;});"
        "out.logKinds = [].map.call(log.querySelectorAll('table tr.gl-row td:nth-child(1)'),"
        "  function(td){return td.textContent.trim();});"
        "out.logNoteKeys = [].map.call(log.querySelectorAll('td.cmp-note-cell'),"
        "  function(td){return td.dataset.noteKey;});"
        "out.mapNoteKeys = [].map.call(map.querySelectorAll('td.cmp-note-cell'),"
        "  function(td){return td.dataset.noteKey;});"
        "_emit(out);})();</script>")
    out = json.loads(run_probe(
        ["core.js", "sheets.js", "compare.js", "compare_issue.js"], "", harness, "logbin"))
    assert out["logItems"] == ["NEW_ITEM", "GONE_ITEM", "LIM_ITEM"], \
        f"Log 표 행이 기대와 다릅니다: {out['logItems']}"
    assert "GAPPY" not in out["logItems"], "Gap% 만 큰 행이 Log 카테고리에 들어갔습니다"
    assert out["logKinds"] == ["추가", "삭제", "Limit 변경"]
    # gl: 키(after + U+001F + before). 한쪽만 있는 행은 반대편이 빈 문자열.
    assert f"gl:NEW_ITEM{SEP}" in out["logNoteKeys"]
    assert f"gl:{SEP}GONE_ITEM" in out["logNoteKeys"]
    assert f"gl:LIM_ITEM{SEP}LIM_ITEM" in out["logNoteKeys"]
    # bm: 좌표 키는 MAP비교 쪽으로 갔다.
    assert "bm:3,7" in out["mapNoteKeys"], f"Bin 표 코멘트 키가 없습니다: {out['mapNoteKeys']}"
    print("[c][d] Log 는 변경 행만 · Comment 키는 bm:/gl: 규약 OK")


def test_note_cell_sync_same_key():
    """LOG비교엔 같은 gl: 키 셀이 2개(요약표+goodlog 전체표) — 저장 시 함께 갱신돼야 한다.

    한쪽만 고치면 같은 항목의 코멘트가 화면에서 갈린다(규칙 13). 종전에는 두 표가 서로
    다른 탭이라 드러나지 않던 문제라, 통합과 함께 syncCompareNoteCells 를 넣었다.
    """
    harness = (
        "<script>(function(){var out={};"
        "DATA = {compare_notes:{}, web_report:{compare:" + js_literal(COMPARE) + "}};"
        "MODE = 'edit';"
        "var cmp = DATA.web_report.compare;"
        "document.body.insertAdjacentHTML('beforeend',"
        "  '<div id=\"panel-issue-cmp\">'+cmpLogPanelHtml(cmp)+'</div>');"
        "var panel = document.getElementById('panel-issue-cmp');"
        "renderGoodlogSection(panel);"
        "var key = 'gl:LIM_ITEM' + String.fromCharCode(31) + 'LIM_ITEM';"
        "var sel = 'td.cmp-note-cell[data-note-key=\"' + key.replace(/\"/g,'') + '\"]';"
        "var cells = panel.querySelectorAll('td.cmp-note-cell');"
        "var same = [].filter.call(cells, function(c){return c.dataset.noteKey === key;});"
        "out.sameKeyCount = same.length;"
        "if (same.length) { syncCompareNoteCells(same[0], '작성한코멘트'); }"
        "out.texts = same.map(function(c){return c.textContent;});"
        "out.hasNote = same.map(function(c){return c.classList.contains('has-note');});"
        "_emit(out);})();</script>")
    out = json.loads(run_probe(
        ["core.js", "sheets.js", "compare.js", "compare_issue.js"], "", harness, "notesync"))
    assert out["sameKeyCount"] >= 2, \
        f"LOG비교에 같은 gl: 키 셀이 2개여야 합니다(요약표+전체표): {out['sameKeyCount']}"
    assert all(t == "작성한코멘트" for t in out["texts"]), \
        f"같은 키 셀이 함께 갱신되지 않았습니다: {out['texts']}"
    assert all(out["hasNote"]), "has-note 클래스가 일부 셀에만 붙었습니다"
    print(f"[신규] 같은 gl: 키 셀 {out['sameKeyCount']}개 동기 갱신 OK")


def test_compare_columns():
    """(신규) Compare 표 컬럼 — 개발 comment 숨김 / 접기 대상 7개 / %는 1자리.

    `개발 comment` 는 **화면에서만** 숨긴다(sheets.orderColumns) — 서버 payload 와 저장 키는
    그대로라 기존 입력값이 DB 에 살아 있다(CLAUDE.md 규칙 12).
    `Unit` 은 계산 파생값이라 2026-08-27(v42) 에 payload 에서 아예 제거했다 — 그래서 이
    픽스처에도 없고, 없음을 서버 쪽에서 고정하는 것은 test_compare_issue_no_unit_column 이다.
    """
    row = {
        "Category": "CMPDIST", "구분": "산포", "Step": "P2", "TNO": "1", "Item": "ITEM_A",
        "before_avg": 1.0, "before_stdev": 0.5, "before_cpk": 1.4,
        "after_avg": 1.1, "after_stdev": 0.6, "after_cpk": 1.2,
        "meanshift_sigma": 0.2, "stdev_delta_pct": -23.456789, "cpk_ratio_pct": 85.71,
        "Distribution": "", "Status": "",
        "PTE comment": "", "개발 comment": "기존입력",
    }
    harness = (
        "<script>(function(){var out={};"
        "DATA={}; MODE='view';"
        # chunk 를 안 주면 통짜 HTML 문자열을 그대로 돌려준다(fill 없음).
        "var html = renderSheetTable([" + js_literal(row) + "], {kind:'issue'});"
        "document.body.insertAdjacentHTML('beforeend','<div id=\"p\">'+html+'</div>');"
        "var host=document.getElementById('p');"
        "out.headers=[].map.call(host.querySelectorAll('tr.issue-shead-top > th'),"
        "  function(th){return th.textContent.trim();});"
        "out.foldCols=host.querySelectorAll('colgroup col.cmp-stat-col').length;"
        "out.cells=[].map.call(host.querySelectorAll('tbody tr td'),"
        "  function(td){return td.textContent.trim();});"
        "_emit(out);})();</script>")
    out = json.loads(run_probe(
        # distribution.js 는 Distribution 셀의 distHasData 판정에 필요하다.
        ["core.js", "distribution.js", "sheets.js"], "", harness, "cmpcols"))
    heads = " | ".join(out["headers"])
    # 화면 숨김 대상 — payload 에는 있고 화면에만 없어야 한다.
    assert "개발팀 Comment" not in heads and "개발 comment" not in heads, \
        f"개발팀 Comment 가 화면에 남아 있습니다: {heads}"
    # PTE comment 는 남아야 한다
    assert any("PTE" in h for h in out["headers"]), f"PTE comment 가 사라졌습니다: {heads}"
    # 접기 대상 = before/after 6 + meanshift_σ = 7 (△σ%·cpk% 는 제외)
    assert out["foldCols"] == 7, \
        f"접기 대상 컬럼이 7개가 아닙니다: {out['foldCols']} ({heads})"
    assert "△σ%" in heads and "cpk%" in heads, \
        f"△σ% / cpk% 는 접기 대상이 아니라 항상 보여야 합니다: {heads}"
    # % 2종은 소수 1자리 표시 (원값은 title 툴팁에 남는다)
    assert "-23.5" in out["cells"], f"△σ% 가 1자리로 표시되지 않았습니다: {out['cells']}"
    assert "85.7" in out["cells"], f"cpk% 가 1자리로 표시되지 않았습니다: {out['cells']}"
    print(f"[신규] Compare 컬럼 — 개발comment 숨김 · 접기 {out['foldCols']}개 · %1자리 OK")


def test_compare_issue_no_unit_column():
    """서버 payload 에 `Unit` 이 없다 (v42) — 화면 숨김이 아니라 실제 제거다.

    `개발 comment` 는 **반대로** payload 에 남아 있어야 한다: 그 컬럼은 사용자가 입력한
    값을 화면으로 실어 나르는 통로라, 빼면 DB 에 값이 남아도 다시 보여줄 길이 사라진다
    (CLAUDE.md 규칙 12). 두 컬럼의 성격이 다르다는 것이 이 테스트의 요지다.
    """
    src = (_ROOT / "web_report" / "tabs" / "compare_issue.py").read_text(encoding="utf-8")
    body = src[src.index("def build_compare_issue_rows"):]
    assert '"Unit"' not in body, "compare_issue.py 가 아직 Unit 컬럼을 싣고 있습니다"
    assert "_comment_values" in body, (
        "개발 comment 통로(_comment_values)가 사라졌습니다 — 사용자 입력이 화면에 "
        "나올 길이 없어집니다(규칙 12)")


def test_compare_cols_scoped_to_compare():
    """숨김·접기는 Compare 시트에만 걸려야 한다 — 일반 Issue Table 은 종전 그대로."""
    # Map 컬럼은 뺀다 — 그 셀 렌더가 wafer_charts.js(issueBinMaps)를 요구하는데 이 테스트가
    # 보는 것은 Unit/개발팀 Comment 유지 여부라 무관하다.
    row = {"Category": "Yield", "Step": "P2", "Bin": "20", "TNO": "1", "Item": "ITEM_A",
           "Unit": "V", "Distribution": "", "Status": "",
           "PTE comment": "", "개발 comment": "지켜져야함"}
    harness = (
        "<script>(function(){var out={};"
        "DATA={}; MODE='view';"
        "var html = renderSheetTable([" + js_literal(row) + "], {kind:'issue'});"
        "document.body.insertAdjacentHTML('beforeend','<div id=\"p\">'+html+'</div>');"
        "var host=document.getElementById('p');"
        "out.headers=[].map.call(host.querySelectorAll('tr.issue-shead-top > th'),"
        "  function(th){return th.textContent.trim();});"
        "out.foldCols=host.querySelectorAll('colgroup col.cmp-stat-col').length;"
        "_emit(out);})();</script>")
    out = json.loads(run_probe(
        ["core.js", "distribution.js", "sheets.js"], "", harness, "maincols"))
    heads = " | ".join(out["headers"])
    assert any(h.strip().lower() == "unit" for h in out["headers"]), \
        f"일반 Issue Table 의 Unit 이 사라졌습니다(회귀): {heads}"
    assert "개발팀 Comment" in heads, \
        f"일반 Issue Table 의 개발팀 Comment 가 사라졌습니다(회귀): {heads}"
    assert out["foldCols"] == 0, "일반 Issue Table 에 접기 컬럼이 생겼습니다(회귀)"
    print("[신규] 일반 Issue Table 무영향(Unit·개발팀 Comment 유지) OK")


def test_subtab_switching():
    """(신규) 서브탭 전환이 실제로 도는지 — 진입점부터 클릭까지 실물 마크업으로.

    정적 검사로는 못 잡는 부류(렌더 예외·lazy dirty 오작동·빈 패널)를 잡는다.
    마크업은 report_view.html 에서 그대로 떼어와 쓴다 — 손으로 다시 쓰면 검증이 무의미하다.
    """
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    m = re.search(r'(<div class="panel" id="panel-issue-cmp">.*?)\n  <div class="panel" '
                  r'id="panel-distribution"', view, re.S)
    assert m, "report_view.html 에서 Compare 패널 마크업을 찾지 못했습니다"
    body = f'<div class="content">{m.group(1)}</div>'
    harness = (
        "<script>(function(){var out={};"
        "MODE='view';"
        "DATA={compare_notes:{}, issue_table_text:[],"
        "  session:{mode:'Compare', source:'web_report'},"
        "  web_report:{mode:'Compare', compare:" + js_literal(COMPARE) + ","
        "    sheets:{'Issue Table Compare':[]}}};"
        "window.Plotly=null;"
        # webReportSheets 는 wafer_charts.js(무거운 Plotly 모듈) 소관이라 여기선 스텁으로
        # 대신한다 — 이 테스트가 보는 것은 서브탭 전환이지 시트 로딩이 아니다.
        "window.webReportSheets=function(){return (DATA.web_report||{}).sheets||{};};"
        "try{ renderCompareIssueTab(); }catch(e){ out.EX=String(e); }"
        "var panel=document.getElementById('panel-issue-cmp');"
        "var act=function(){var a=panel.querySelector('.cmp-subpanel.active');"
        "  return a?a.dataset.cmppanel:null;};"
        "out.initial=act();"
        "out.tableHasId=!!panel.querySelector('#panel-issue-cmp-table[data-cmppanel=\"table\"]');"
        "panel.querySelector('[data-cmpsub=\"log\"]').click();"
        "out.afterLog=act();"
        "out.logHasSummaryTable=!!panel.querySelector('.cmp-subpanel[data-cmppanel=\"log\"] table');"
        "out.logHasSection=!!panel.querySelector('#cmp-log-section');"
        "panel.querySelector('[data-cmpsub=\"map\"]').click();"
        "out.afterMap=act();"
        "out.mapHasMapDiv=!!panel.querySelector('#cmp-common-map');"
        "out.mapBinSections=panel.querySelectorAll("
        "  '.cmp-subpanel[data-cmppanel=\"map\"] .cmp-bin-grid section').length;"
        "panel.querySelector('[data-cmpsub=\"equiv\"]').click();"
        "out.afterEquiv=act();"
        "out.equivFilled=panel.querySelector("
        "  '.cmp-subpanel[data-cmppanel=\"equiv\"]').innerHTML.length>50;"
        "panel.querySelector('[data-cmpsub=\"ttime\"]').click();"
        "out.afterTtime=act();"
        "panel.querySelector('[data-cmpsub=\"table\"]').click();"
        "out.backToTable=act();"
        "out.noBinInTable=panel.querySelector("
        "  '#panel-issue-cmp-table').innerHTML.indexOf('Bin Transition')<0;"
        "_emit(out);})();</script>")
    out = json.loads(run_probe(
        # tabs_topbar.js 는 emptyPanel(빈 화면 안내) 때문에 필요하다.
        ["core.js", "sheets.js", "tabs_topbar.js", "yield_issue.js",
         "compare.js", "compare_issue.js"],
        body, harness, "subtabs"))
    assert "EX" not in out, f"renderCompareIssueTab 에서 예외: {out.get('EX')}"
    assert out["initial"] == "table", f"기본 서브탭이 ISSUE_TABLE 이 아닙니다: {out['initial']}"
    assert out["tableHasId"], "ISSUE_TABLE 서브패널이 이슈 표 패널 id 를 갖지 않습니다"
    # 전환이 실제로 되는가 + 각 화면이 비어 있지 않은가
    assert out["afterLog"] == "log", "LOG비교 전환 실패"
    assert out["logHasSummaryTable"], "LOG비교 상단 요약표가 없습니다"
    assert out["logHasSection"], "LOG비교에 goodlog 전체표 섹션이 없습니다"
    assert out["afterMap"] == "map", "MAP비교 전환 실패"
    assert out["mapHasMapDiv"], "MAP비교에 공통성 Map 컨테이너가 없습니다"
    assert out["mapBinSections"] == 2, \
        f"MAP비교 Bin 표 2개(동일좌표/BinYield)가 아닙니다: {out['mapBinSections']}"
    assert out["afterEquiv"] == "equiv" and out["equivFilled"], "동일성검증 전환/렌더 실패"
    assert out["afterTtime"] == "ttime", "TESTTIME비교 전환 실패"
    assert out["backToTable"] == "table", "ISSUE_TABLE 복귀 실패"
    assert out["noBinInTable"], "ISSUE_TABLE 에 Bin Transition 이 남아 있습니다"
    print("[신규] 서브탭 5개 전환 + 각 화면 렌더 + Bin Transition 제거 OK")


def test_summary_card_and_engr():
    """(e)(f) Compare 요약 카드는 서버 수치를 그대로, ENGR 칸에 compare 추가."""
    # 모드 판정 정본은 **세션 DB 컬럼**(core.js webReportMode → DATA.session.mode).
    harness = (
        "<script>(function(){var out={};"
        "DATA = {session:{mode:'Compare', source:'web_report'},"
        "  web_report:{mode:'Compare', compare:" + js_literal(COMPARE) + "}};"
        "out.cmpCard = compareSummaryCardHtml();"
        "out.engrCompare = engrCommentFields().map(function(f){return f.key;});"
        "DATA = {session:{mode:'Normal', source:'web_report'}, web_report:{mode:'Normal'}};"
        "out.normalCard = compareSummaryCardHtml();"
        "out.engrNormal = engrCommentFields().map(function(f){return f.key;});"
        "DATA = {session:{mode:'Compare', source:'web_report'},"
        "  web_report:{mode:'Compare', compare_pending:true}};"
        "out.pendingCard = compareSummaryCardHtml();"
        "_emit(out);})();</script>")
    out = json.loads(run_probe(
        ["core.js", "sheets.js", "compare.js", "map_select.js"], "", harness, "summary"))
    assert out["normalCard"] == "", "Compare 모드가 아닌데 카드가 생겼습니다"
    card = out["cmpCard"]
    assert ">4<" in card, "산포 검출 수(dist_shift.summary.focus)가 안 보입니다"
    assert "공통 항목 12개 중" in card
    assert "Bin 불일치" in card and ">1<" in card
    assert 'data-jump="issue-cmp"' in card, "카드 클릭 점프 대상이 없습니다"
    assert "계산 중" in out["pendingCard"], "pending 안내가 없습니다"
    assert out["engrCompare"] == ["yield", "cpk", "compare", "etc"], \
        f"Compare 모드 ENGR 칸이 기대와 다릅니다: {out['engrCompare']}"
    assert out["engrNormal"] == ["yield", "cpk", "etc"], "Normal 모드 ENGR 칸이 바뀌었습니다"
    print("[e][f] Compare 요약 카드 + ENGR compare 칸 OK")


def main():
    static = [test_no_es_module, test_registered, test_panel_registered_in_core,
              test_dist_subtab_removed, test_compare_tab_absorbed, test_subtabs_markup,
              test_dist_data_ensured_for_compare, test_compare_issue_no_unit_column]
    browser = [test_row_keys, test_log_and_bin_tables, test_note_cell_sync_same_key,
               test_compare_columns, test_compare_cols_scoped_to_compare,
               test_subtab_switching, test_summary_card_and_engr]
    for fn in static:
        fn()
    if not edge_path():
        print("\n[SKIP] Edge 를 찾지 못해 브라우저 검증은 건너뜁니다 "
              f"({len(browser)}개). 정적 {len(static)}개만 통과.")
        return
    for fn in browser:
        fn()
    print(f"\n전체 {len(static) + len(browser)}개 통과")


if __name__ == "__main__":
    main()

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
    # 이슈 표 CSS 는 패널 id 로 걸려 있다 — 새 패널이 빠지면 sticky/삭제모드가 조용히 깨진다.
    assert view.count("#panel-issue-cmp") >= 10, \
        "이슈 표 CSS 셀렉터에 #panel-issue-cmp 가 충분히 반영되지 않았습니다"
    print("[정적] 로드 순서 + 탭/패널 + CSS 셀렉터 OK")


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
    """(c)(d) Log 표는 변경 행만, 두 표의 Comment 키는 compare_note 규약."""
    harness = (
        "<script>(function(){var out={};"
        "DATA = {compare_notes:{}, web_report:{}}; MODE = 'edit';"
        "document.body.insertAdjacentHTML('beforeend',"
        "  '<div id=\"probe\">'+cmpIssExtraHtml(" + js_literal(COMPARE) + ")+'</div>');"
        "var host = document.getElementById('probe');"
        "out.logItems = [].map.call(host.querySelectorAll('table tr.gl-row td:nth-child(2)'),"
        "  function(td){return td.textContent;});"
        "out.logKinds = [].map.call(host.querySelectorAll('table tr.gl-row td:nth-child(1)'),"
        "  function(td){return td.textContent.trim();});"
        "out.noteKeys = [].map.call(host.querySelectorAll('td.cmp-note-cell'),"
        "  function(td){return td.dataset.noteKey;});"
        "out.anchors = [].map.call(host.querySelectorAll('[id^=cmpiss-]'),"
        "  function(el){return el.id;});"
        "_emit(out);})();</script>")
    out = json.loads(run_probe(
        ["core.js", "sheets.js", "compare.js", "compare_issue.js"], "", harness, "logbin"))
    assert out["logItems"] == ["NEW_ITEM", "GONE_ITEM", "LIM_ITEM"], \
        f"Log 표 행이 기대와 다릅니다: {out['logItems']}"
    assert "GAPPY" not in out["logItems"], "Gap% 만 큰 행이 Log 카테고리에 들어갔습니다"
    assert out["logKinds"] == ["추가", "삭제", "Limit 변경"]
    # bm: 좌표 키 + gl: 키(after + U+001F + before). 한쪽만 있는 행은 반대편이 빈 문자열.
    assert "bm:3,7" in out["noteKeys"], f"Bin 표 코멘트 키가 없습니다: {out['noteKeys']}"
    assert f"gl:NEW_ITEM{SEP}" in out["noteKeys"]
    assert f"gl:{SEP}GONE_ITEM" in out["noteKeys"]
    assert f"gl:LIM_ITEM{SEP}LIM_ITEM" in out["noteKeys"]
    assert set(out["anchors"]) == {"cmpiss-bin", "cmpiss-log"}, "점프 앵커가 없습니다"
    print("[c][d] Log 는 변경 행만 · Comment 키는 bm:/gl: 규약 OK")


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
              test_dist_subtab_removed]
    browser = [test_row_keys, test_log_and_bin_tables, test_summary_card_and_engr]
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

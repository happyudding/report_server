"""Gap Chart JS 회귀 — headless Edge (2026-08-24).

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_gap_chart_js.py

왜 파이썬 테스트로 안 되나: 이 기능이 깨지는 방식은 화면에서만 드러난다.
  · `openItemDetail` 의 opts 가 조건부로 대입되면, gap 상세를 본 뒤 **일반 항목**을 열 때
    이전 gap URL 이 남아 엉뚱한 데이터가 뜬다(에러 없이 조용히 — 플랜 R-3).
  · 평문 렉서(gcLex)가 인용 규칙을 어기면 이름에 공백·연산자가 든 항목이 못 들어가거나
    수정 모달의 tokens → 원문 라운드트립이 깨져 저장된 수식이 변형된다.
  · 수식 토큰 서식(item=파란 기울임 / source=빨간 기울임)이 빠지면 사용자 요구가 무너진다.
  · 메뉴 항목 순서(composite → Gap Chart)는 사용자가 지정한 것이다.

검증하는 것:
  (a) 정적: classic script 유지 + report_view.html 로드 순서/모달/필수 입력요소/data-no-dirty
  (b) 정적: 훅 5곳 (갤러리 카드·셀 렌더·패널 위임·openItemDetail opts·Bin1 재진입)
  (c) 정적: **openItemDetail 의 opts 무조건 대입** + 조건부 대입 금지 (R-3)
  (d) 정적: 메뉴에서 Gap Chart 가 Distribution composite **바로 뒤**
  (e) 정적: CSS 라이트 + 다크
  (f) 토큰 → 표시 문자열 (이름에 괄호·연산자·공백이 든 경우 포함)
  (g) gcModeOf 3분기 / gcValidate 케이스
  (h) gcExprHtml 색 클래스 + HTML 이스케이프 (읽기 전용 — 클릭 삭제 속성 없음)
  (i) gcCardsHtml — data-gap-id / .distg-plot / 편집모드 게이트
  (j) 평문 렉서 — @"이름" 인용·@"source"!"항목" 명시·별칭 연산자·경고(목록 밖 이름)·
      tokens → gcTokensToText → gcLex 라운드트립 (2026-08-26 입력 방식 개편)
  (j2) `@` 자동완성 조각 판정 + 삽입(조각 치환·source 드롭다운 반영)
  (k) **fetch 스텁으로 gap → 일반 항목 연속 호출 시 마지막 URL 이 /scatter/** (R-3 실동작)

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
_VIEW = _ROOT / "server" / "report" / "report_view.html"
_TMP = Path(tempfile.mkdtemp(prefix="wr_gap_js_"))

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def edge_path():
    for p in _EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


_EMIT = ("<script>function _emit(o){var p=document.createElement('pre');"
         "p.id='res';p.textContent=JSON.stringify(o);document.body.appendChild(p);}</script>")


def run_probe(scripts, body_html, harness_js, name) -> str:
    """지정 JS 를 인라인한 페이지를 돌리고 `_emit()` 이 남긴 JSON 을 반환.

    stdout 은 **파일로** 리다이렉트한다 — 파이프로 받으면 Windows 에서 빈 출력이 온다."""
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
    found = re.findall(r'<pre id="res">([\s\S]*?)</pre>', raw)
    assert found, f"{name}: 하네스가 실행되지 않았습니다 (스크립트 파싱 오류 의심)"
    return unescape(found[-1]).strip()


# gap_chart.js 는 core.js(esc/showToast/csrfToken/fetchJson202) + distribution.js
# (distSuggestions/distCapFor/DIST 상수/distLimInnerHtml) 위에서 돈다.
DEPS = ["core.js", "distribution.js", "gap_chart.js"]
# R-3 실동작 검증은 openItemDetail 이 필요하므로 item_detail.js 까지 싣는다.
DEPS_DETAIL = ["core.js", "distribution.js", "item_detail.js", "gap_chart.js"]

# 하네스 공용 전역 — 실제 payload 의 최소 형태.
# ⚠ SESSION_ID 는 core.js 에서 const 라 재할당하면 TypeError 로 하네스가 통째로 죽는다.
SETUP = (
    "DATA={session:{source:'web_report',mode:'Normal'},web_report:{"
    "  sources:[{name:'WF1'},{name:'WF2'}],"
    "  distribution_index:[{subject:'IT00',test_num:'1000',units:'V',"
    "     lower_limit:-1,upper_limit:1,cpk:1.1,status:'ok'},"
    "    {subject:'A-B (max)',test_num:'1001',units:'A',"
    "     lower_limit:0,upper_limit:10,cpk:2.0,status:'ok'}]},"
    "  dist_composites:{}, gap_charts:{}};"
    "distIndex=DATA.web_report.distribution_index;"
)


# ── 정적 검사 (Edge 없이도 돈다) ─────────────────────────────────────────────

def test_no_es_module():
    """분할 JS 는 classic script 순서 로드다 — import/export 를 쓰면 전부 죽는다."""
    src = (_JS / "gap_chart.js").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\s", src, re.M), "gap_chart.js: ES module 금지"
    print("[정적] classic script 유지 OK")


def test_registered():
    view = _VIEW.read_text(encoding="utf-8")
    assert "static/webreport/gap_chart.js" in view, "gap_chart.js 가 로드되지 않았습니다"
    # distribution.js(렌더 헬퍼)·item_detail.js(openItemDetail/distCdfFromValues)·
    # dist_composite.js("분석하기" 메뉴) 뒤, edit_mode.js(MODE) 앞.
    for dep in ("core.js", "distribution.js", "item_detail.js", "dist_composite.js"):
        assert view.index(f"webreport/{dep}") < view.index("webreport/gap_chart.js"), \
            f"gap_chart.js 는 {dep} 뒤에 로드돼야 합니다"
    assert view.index("webreport/gap_chart.js") < view.index("webreport/edit_mode.js"), \
        "gap_chart.js 는 edit_mode.js 앞에 로드돼야 합니다"
    assert 'id="gcModal"' in view, "생성/수정 모달이 없습니다"
    for el in ("gcModalTitle", "gcName", "gcLimitLo", "gcLimitHi", "gcSourceList",
               "gcItemSearch", "gcItemList", "gcExpr", "gcFormulaInput", "gcSrcQual",
               "gcSuggest", "gcStatus", "gcSave", "gcCancel", "gcDelete"):
        assert f'id="{el}"' in view, f"모달에 #{el} 이 없습니다"
    # 좌우 2단 (사용자 확정 레이아웃) — 왼쪽 source·항목, 오른쪽 수식
    assert "gc-cols" in view and view.count("gc-col") >= 3, "2단 레이아웃 마크업이 없습니다"
    assert view.index('id="gcSourceList"') < view.index('id="gcExpr"'), \
        "왼쪽(source·항목)이 오른쪽(수식)보다 먼저 와야 합니다"
    # Limit 은 차트 이름 **아래** 줄 — 세로 2줄 레이아웃 (2026-08-26 사용자 요청)
    assert view.index('id="gcName"') < view.index('id="gcLimitLo"'), \
        "Limit 이 차트 이름보다 앞에 있습니다"
    top = re.search(r"\.gc-top \{([^}]*)\}", view)
    assert top and "flex-direction: column" in top.group(1), \
        ".gc-top 이 세로 배치가 아닙니다 (Limit 이 이름 밑으로 안 내려갑니다)"
    # 모달 타이핑이 세션을 dirty 로 만들면 이탈 경고가 오작동한다
    assert re.search(r'id="gcModal"[^>]*data-no-dirty', view), "모달에 data-no-dirty 가 없습니다"
    # 구 토큰 커밋 UI(연산자 버튼·⌫)는 제거됐다 — 되살아나면 입력 방식이 갈라진 것
    assert "data-gc-op=" not in view and 'data-gc-act="pop"' not in view, \
        "구 연산자 버튼 UI 가 남아 있습니다 (2026-08-26 평문 입력 개편)"
    print("[정적] 로드 순서 + 2단 모달/입력요소 + 이름 밑 Limit + data-no-dirty OK")


def test_hooks():
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    idet = (_JS / "item_detail.js").read_text(encoding="utf-8")
    comp = (_JS / "dist_composite.js").read_text(encoding="utf-8")
    notes = (_JS / "chart_notes.js").read_text(encoding="utf-8")

    # (b-1) 갤러리 맨 앞 카드 + 셀 렌더 분기
    assert "gcCardsHtml" in dist, "distribution.js: 갤러리 카드 훅이 없습니다"
    assert "cell.dataset.gapId" in dist, "distribution.js: gap 셀 렌더 분기가 없습니다"
    assert 'typeof gcRenderGapCell === "function"' in dist, "typeof 가드가 없습니다"
    # 셀 렌더 분기는 distGalleryReady() 보다 앞이어야 한다(gap 은 준비상태를 자체 판단).
    assert dist.index("cell.dataset.gapId") < dist.index("if (!distGalleryReady())"), \
        "gap 분기가 distGalleryReady 뒤에 있습니다"

    # (b-2) 패널 위임 — 반드시 `.distg-card` 일반 분기보다 앞
    assert 'typeof gcPanelClick === "function"' in idet, "item_detail.js: gcPanelClick 훅이 없습니다"
    assert idet.index("gcPanelClick") < idet.index('e.target.closest(".distg-card")'), \
        "gcPanelClick 은 일반 카드 분기보다 먼저 호출돼야 합니다"

    # (b-3) Bin1 재진입이 opts 를 함께 넘긴다 (안 넘기면 gap 상세에서 Bin1 이 일반 scatter 로)
    assert "openItemDetail(_itemDetailSubject, _itemDetailNav, _itemDetailOpts)" in idet, \
        "Bin1 재진입에서 _itemDetailOpts 를 넘기지 않습니다"

    # (b-4) chart_notes 는 note_subject 를 우선한다 (cdf:gap:<uuid> 네임스페이스)
    assert "function cnSubjectOf" in notes, "chart_notes.js: cnSubjectOf 가 없습니다"
    assert "note_subject" in notes, "chart_notes.js: note_subject 우선 처리가 없습니다"

    # (b-5) 메뉴 항목이 dist_composite 의 메뉴에 붙는다
    assert 'typeof gcMenuItemHtml === "function"' in comp, "메뉴 항목 훅이 없습니다"
    print("[정적] 훅 5곳 (갤러리·셀·위임·Bin1 재진입·주석 키·메뉴) OK")


def test_opts_unconditional():
    """(c) **R-3** — openItemDetail 의 opts 는 무조건 대입해야 한다.

    조건부(`if (opts)`)로 쓰면 gap 상세를 본 뒤 일반 항목을 2인자로 열 때 이전 gap URL 이
    남아 엉뚱한 데이터가 뜬다. 시그니처 기본값(`opts = null`)이 그 실수를 구조적으로 막는다."""
    idet = (_JS / "item_detail.js").read_text(encoding="utf-8")
    assert re.search(r"function openItemDetail\(subject, navList, opts = null\)", idet), \
        "openItemDetail 시그니처에 `opts = null` 기본값이 없습니다"
    assert re.search(r"_itemDetailOpts = opts;", idet), "_itemDetailOpts 무조건 대입이 없습니다"
    assert not re.search(r"if\s*\(\s*opts\s*\)\s*_itemDetailOpts", idet), \
        "조건부 대입이 있습니다 (R-3 회귀)"
    # URL 은 opts 가 URL 을 줄 때만 갈아끼운다(고정 url 또는 subject 별 urlOf).
    assert re.search(r"typeof opts\.urlOf === \"function\" \? opts\.urlOf\(subject\) : opts\.url",
                     idet), "opts URL 분기(urlOf/url)가 없습니다"
    # prev/next 로 옮길 때 고정 url 짜리 opts 를 이어받으면 다음 항목이 이전 URL 로 조회된다.
    assert re.search(r"typeof _itemDetailOpts\.urlOf === \"function\"\s*\)?\s*\n?\s*\?", idet), \
        "itemDetailNav 가 urlOf 없는 opts 를 걸러내지 않습니다 (R-3 파생)"
    print("[정적] openItemDetail opts 무조건 대입 + URL 분기(urlOf 포함) OK (R-3)")


def test_menu_order():
    """(d) 메뉴에서 Gap Chart 는 Distribution composite **바로 뒤**(사용자 지정 순서)."""
    comp = (_JS / "dist_composite.js").read_text(encoding="utf-8")
    at_comp = comp.index("📊 Distribution composite")
    at_gap = comp.index("gcMenuItemHtml()")
    assert at_comp < at_gap, "Gap Chart 항목이 composite 앞에 있습니다"
    gap = (_JS / "gap_chart.js").read_text(encoding="utf-8")
    assert "📈 Gap Chart" in gap, "메뉴 라벨이 없습니다"
    assert 'data-dc-act="gap-modal"' in gap and 'kind === "gap-modal"' in comp, \
        "메뉴 진입 위임이 이어지지 않았습니다"
    print("[정적] 메뉴 순서 composite → Gap Chart OK")


def test_modal_width():
    """모달 폭은 **`.modal-box.gc-modal-box`(특이도 0,2,0)** 로 써야 한다.

    이 블록은 스타일시트에서 `.modal-box`(width:360px) **앞에** 있어 한 클래스로 쓰면
    같은 특이도라 그쪽이 이겨 폭이 360px 로 되돌아간다(2026-08-24 실제로 밟아 사용자가
    "창이 너무 작다"고 신고했다 — dc-modal-box 와 같은 함정)."""
    view = _VIEW.read_text(encoding="utf-8")
    assert ".modal-box.gc-modal-box {" in view,         ".gc-modal-box 는 .modal-box 와 함께 써 특이도를 올려야 합니다 (폭 360px 회귀)"
    box = re.search(r"\.modal-box\.gc-modal-box \{([^}]*)\}", view)
    assert box, ".modal-box.gc-modal-box 규칙이 없습니다"
    css = box.group(1)
    # 2026-08-26 사용자 요청으로 1600px → 1240px 축소 (수식 평문 입력 개편과 세트)
    assert "1240px" in css, "가로 폭이 1240px 가 아닙니다 (2026-08-26 축소)"
    assert "overflow: hidden" in css, "모달이 자체 스크롤을 내면 안쪽 목록 스크롤이 안 생깁니다"
    assert "flex-direction: column" in css, "세로 flex 가 아니면 목록이 남는 공간을 못 받습니다"
    print("[정적] 모달 폭 1240px + 특이도 + 내부 스크롤 OK")


def test_formula_bar():
    """Item_detail 에 수식 줄이 붙는다 — 만들 때와 같은 서식(읽기 전용)."""
    gap = (_JS / "gap_chart.js").read_text(encoding="utf-8")
    idet = (_JS / "item_detail.js").read_text(encoding="utf-8")
    view = _VIEW.read_text(encoding="utf-8")
    assert "function gcTokenParts" in gap, "토큰 서식 공용 함수가 없습니다"
    assert "function gcExprHtml" in gap, "읽기 전용 수식 렌더가 없습니다"
    assert "function gcFormulaBarHtml" in gap, "Item_detail 수식 줄 생성기가 없습니다"
    assert 'typeof gcFormulaBarHtml === "function"' in idet, "item_detail.js 훅이 없습니다"
    # 수식 줄은 헤더(.idet-head) 바로 아래 = cdfEditBar 앞
    assert idet.index("gcFormulaBarHtml") < idet.index('id="cdfEditBar"'),         "수식 줄이 CDF 편집바보다 뒤에 있습니다"
    assert ".idet-formula {" in view and ".idet-formula .gc-tok:hover" in view,         "수식 줄 CSS(또는 hover 해제)가 없습니다"
    assert 'html[data-theme="dark"] .idet-formula' in view, "다크 테마 규칙이 없습니다"
    print("[정적] Item_detail 수식 줄 + 서식 공용화 OK")


def test_css_present():
    view = _VIEW.read_text(encoding="utf-8")
    for cls in (".distg-gap ", ".gc-modal-box", ".gc-cols", ".gc-expr", ".gc-tok-item",
                ".gc-tok-src", ".gc-suggest", ".gc-status-bad"):
        assert cls in view, f"라이트 CSS 에 {cls} 가 없습니다"
    # 사용자 요구 서식: item=파란 기울임 / source=빨간 기울임
    item_rule = re.search(r"\.gc-tok-item \{([^}]*)\}", view)
    src_rule = re.search(r"\.gc-tok-src \{([^}]*)\}", view)
    assert item_rule and "italic" in item_rule.group(1), "item 토큰이 기울임이 아닙니다"
    assert src_rule and "italic" in src_rule.group(1), "source 토큰이 기울임이 아닙니다"
    assert 'html[data-theme="dark"] .gc-tok-item' in view, "다크 테마 규칙이 없습니다"
    assert 'html[data-theme="dark"] .gc-expr' in view, "다크 테마 수식 영역 규칙이 없습니다"
    print("[정적] CSS 라이트 + 다크 (item 파란 기울임 / source 빨간 기울임) OK")


# ── 브라우저 검사 ────────────────────────────────────────────────────────────

def test_formula_text():
    """(f) 토큰 → 표시 문자열. 이름에 괄호·연산자·공백이 있어도 그대로 보인다."""
    js = f"""<script>
      {SETUP}
      var toks = [{{t:'lp'}}, {{t:'item',item:'A-B (max)'}}, {{t:'op',v:'-'}},
                  {{t:'num',v:2}}, {{t:'rp'}}, {{t:'op',v:'/'}},
                  {{t:'item',source:'WF1',item:'IT00'}}];
      _emit({{text: gcFormulaText(toks), empty: gcFormulaText([])}});
    </script>"""
    got = json.loads(run_probe(DEPS, "", js, "formula"))
    assert got["text"] == "(A-B (max) − 2) ÷ WF1_IT00", got
    assert got["empty"] == "", got
    print("  [browser] 표시 문자열 (연산자·공백 든 항목명 포함) OK")


def test_mode_and_validate():
    """(g) gcModeOf 3분기 + gcValidate 케이스."""
    js = f"""<script>
      {SETUP}
      var I=function(n,s){{var t={{t:'item',item:n}}; if(s) t.source=s; return t;}};
      var O=function(v){{return {{t:'op',v:v}};}};
      var N=function(v){{return {{t:'num',v:v}};}};
      var LP={{t:'lp'}}, RP={{t:'rp'}};
      var modes = {{
        per: gcModeOf([I('A'), O('-'), I('B')]),
        exp: gcModeOf([I('A','WF1'), O('-'), I('B','WF2')]),
        mix: gcModeOf([I('A'), O('-'), I('B','WF2')])
      }};
      var v = {{
        ok:      gcValidate([LP, I('A'), O('-'), N(2), RP, O('*'), I('B')]).ok,
        unary:   gcValidate([O('-'), I('A')]).ok,
        empty:   gcValidate([]).ok,
        tail:    gcValidate([I('A'), O('+')]).ok,
        unbal:   gcValidate([LP, I('A')]).ok,
        adjacent:gcValidate([I('A'), I('B')]).ok,
        noitem:  gcValidate([N(1), O('+'), N(2)]).ok,
        mixed:   gcValidate([I('A'), O('-'), I('B','WF2')]).ok
      }};
      _emit({{modes: modes, v: v, mixedMsg: gcValidate([I('A'), O('-'), I('B','WF2')]).msg}});
    </script>"""
    got = json.loads(run_probe(DEPS, "", js, "mode"))
    assert got["modes"] == {"per": "per_source", "exp": "explicit", "mix": "mixed"}, got["modes"]
    v = got["v"]
    assert v["ok"] is True and v["unary"] is True, v
    for bad in ("empty", "tail", "unbal", "adjacent", "noitem", "mixed"):
        assert v[bad] is False, f"{bad} 가 통과했다: {v}"
    assert "섞을 수 없습니다" in got["mixedMsg"], got["mixedMsg"]
    print("  [browser] gcModeOf 3분기 + gcValidate 8케이스 OK")


def test_token_html():
    """(h) 토큰 서식 클래스 + HTML 이스케이프 — 칩은 어디서나 읽기 전용(클릭 삭제 없음)."""
    js = f"""<script>
      {SETUP}
      var plain = gcExprHtml([{{t:'item',item:'IT00'}}]);
      var qual  = gcExprHtml([{{t:'item',source:'WF1',item:'IT00'}}]);
      var evil  = gcExprHtml([{{t:'item',item:'<script>x</scr'+'ipt>'}}]);
      var op    = gcExprHtml([{{t:'op',v:'*'}}]);
      _emit({{plain: plain, qual: qual, evil: evil, op: op}});
    </script>"""
    got = json.loads(run_probe(DEPS, "", js, "tokhtml"))
    assert "gc-tok-item" in got["plain"] and "gc-tok-src" not in got["plain"], got["plain"]
    assert "gc-tok-src" in got["qual"] and "gc-tok-item" in got["qual"], got["qual"]
    assert "<script>" not in got["evil"] and "&lt;script&gt;" in got["evil"], got["evil"]
    assert "×" in got["op"], got["op"]
    assert "data-gc-tok" not in got["qual"], "칩에 구 클릭-삭제 속성이 남아 있습니다"
    print("  [browser] 토큰 서식 클래스 + 이스케이프 OK")


def test_cards_html():
    """(i) 카드 HTML — data-gap-id / .distg-plot / 편집모드에서만 ✎✕."""
    js = f"""<script>
      {SETUP}
      DATA.gap_charts = {{'u1': {{name:'Gap A-B', sources:['WF1'],
        tokens:[{{t:'item',item:'IT00'}}], limit:{{mode:'manual',lo:-1,hi:1}}}}}};
      var view = gcCardsHtml();
      // MODE 는 core.js 에서 `let` 이라 **재할당만** 가능하다 — `var MODE` 로 다시 선언하면
      // 중복 선언 SyntaxError 로 하네스가 통째로 죽는다(파싱 단계라 try/catch 도 못 잡는다).
      MODE = 'edit';
      var editView = gcCardsHtml();
      _emit({{view: view, hasEdit: view.indexOf('data-gc-act="edit"') >= 0,
             editHasEdit: editView.indexOf('data-gc-act="edit"') >= 0,
             editHasDel: editView.indexOf('data-gc-act="del"') >= 0}});
    </script>"""
    got = json.loads(run_probe(DEPS, "", js, "cards"))
    view = got["view"]
    assert 'data-gap-id="u1"' in view, view[:300]
    assert "distg-card distg-gap" in view, view[:300]
    assert '<div class="distg-plot"></div>' in view, view[:300]
    assert "Gap A-B" in view and "계산 중" in view, view[:300]
    assert got["hasEdit"] is False, "읽기 모드에서 편집 버튼이 노출됩니다"
    assert got["editHasEdit"] is True and got["editHasDel"] is True,         "편집 모드에서 수정/삭제 버튼이 안 나옵니다"
    print("  [browser] 카드 HTML (골격 + 편집모드 게이트 양방향) OK")


def test_formula_bar_render():
    """gcFormulaBarHtml — 토큰 서식 렌더 + tokens 없을 때 평문 폴백."""
    js = f"""<script>
      {SETUP}
      var withTok = gcFormulaBarHtml({{is_gap:true, gap_mode:'explicit', matched_dies:12,
        dropped_nonfinite:2, missing:[],
        tokens:[{{t:'item',source:'WF1',item:'IT00'}}, {{t:'op',v:'-'}},
                {{t:'item',source:'WF2',item:'IT00'}}], formula:'x'}});
      var fallback = gcFormulaBarHtml({{is_gap:true, gap_mode:'per_source',
        matched_dies:3, tokens:[], formula:'IT00 - IT01'}});
      var notGap = gcFormulaBarHtml({{is_gap:false, tokens:[{{t:'item',item:'A'}}]}});
      _emit({{withTok: withTok, fallback: fallback, notGap: notGap}});
    </script>"""
    got = json.loads(run_probe(DEPS, "", js, "fbar"))
    w = got["withTok"]
    assert "gc-tok-src" in w and "gc-tok-item" in w, w[:200]
    assert "data-gc-tok" not in w, "상세의 수식 칩에 클릭 삭제 속성이 붙었습니다"
    assert "die 12개" in w and "계산 불가 2개" in w, w[:300]
    assert "좌표가 같은 die" in w, w[:300]
    assert "IT00 - IT01" in got["fallback"], got["fallback"][:200]
    assert got["notGap"] == "", got["notGap"]
    print("  [browser] Item_detail 수식 줄 렌더 + 평문 폴백 OK")


def test_lexer():
    """(j) 평문 렉서 — 인용·source 명시·별칭·경고·라운드트립 (2026-08-26 개편의 핵심)."""
    js = f"""<script>
      {SETUP}
      function T(s) {{
        try {{ var o = gcLex(s); return {{ok:true, toks:o.tokens, warns:o.warns}}; }}
        catch (e) {{ return {{ok:false, msg:e.message, part:(e.tokens||[]).length}}; }}
      }}
      var basic = T('(@"A-B (max)" - 2) / @"WF1"!"IT00"');
      var alias = T('@"IT00" × 2');            // 칩 표시 문자 붙여넣기 허용
      var caseIns = T('@"it00"');              // 대소문자는 조회에만 관대 → 정식 이름
      var unknown = T('@"NOPE"');              // 목록 밖 이름 = 경고, 오류 아님 (§5-12)
      var badAt = T('@abc');
      var unclosed = T('@"abc');
      var badChar = T('@"IT00" & 2');
      var toks = [{{t:'lp'}}, {{t:'item',item:'A-B (max)'}}, {{t:'op',v:'-'}}, {{t:'num',v:2}},
                  {{t:'rp'}}, {{t:'op',v:'/'}}, {{t:'item',source:'WF1',item:'IT00'}}];
      var text = gcTokensToText(toks);
      // 키 순서 무시 구조 비교 — 렉서는 t,item,source 순으로 만들지만 의미는 같다.
      function sortTok(t) {{ var o = {{}}; Object.keys(t).sort().forEach(function(k) {{ o[k] = t[k]; }}); return o; }}
      var round = JSON.stringify(gcLex(text).tokens.map(sortTok))
        === JSON.stringify(toks.map(sortTok));
      _emit({{basic:basic, alias:alias, caseIns:caseIns, unknown:unknown,
             badAt:badAt, unclosed:unclosed, badChar:badChar, text:text, round:round}});
    </script>"""
    got = json.loads(run_probe(DEPS, "", js, "lexer"))
    b = got["basic"]
    assert b["ok"] and len(b["toks"]) == 7 and not b["warns"], b
    assert b["toks"][1] == {"t": "item", "item": "A-B (max)"}, b["toks"]
    assert b["toks"][6] == {"t": "item", "source": "WF1", "item": "IT00"}, b["toks"]
    a = got["alias"]
    assert a["ok"] and a["toks"][1] == {"t": "op", "v": "*"}, a
    c = got["caseIns"]
    assert c["ok"] and c["toks"][0]["item"] == "IT00" and not c["warns"], c
    u = got["unknown"]
    assert u["ok"] and u["toks"][0]["item"] == "NOPE" and len(u["warns"]) == 1, \
        f"목록 밖 이름이 경고+보존으로 처리되지 않았다: {u}"
    for k in ("badAt", "unclosed", "badChar"):
        assert got[k]["ok"] is False, f"{k} 가 통과했다: {got[k]}"
    assert '@"A-B (max)"' in got["text"] and '@"WF1"!"IT00"' in got["text"], got["text"]
    assert got["round"] is True, f"tokens → 원문 → tokens 라운드트립이 깨졌다: {got['text']}"
    print("  [browser] 평문 렉서 + 라운드트립 OK")


def test_mention_insert():
    """(j2) `@` 자동완성 — 조각 판정·후보·삽입(조각 치환 + source 드롭다운 반영)."""
    body = ('<div id="gcModal"><input id="gcFormulaInput"><select id="gcSrcQual"></select>'
            '<div id="gcExpr"></div><div id="gcStatus"></div><div id="gcSuggest"></div>'
            '<input id="gcName" value="n"><button id="gcSave"></button></div>')
    js = f"""<script>
      {SETUP}
      var input = document.getElementById('gcFormulaInput');
      gcRenderSrcQual();
      // 1) `@IT` 조각 → 후보 → 삽입하면 조각이 @"IT00" 으로 치환된다
      input.value = '@IT';
      input.focus(); input.setSelectionRange(3, 3);
      gcUpdateSuggest();
      var nSug = _gcSuggest.length;
      gcInsertItem(_gcSuggest.length ? _gcSuggest[0].subject : 'IT00');
      var v1 = input.value;
      // 2) source 드롭다운을 고르면 삽입이 @"source"!"항목" 명시 참조가 된다
      input.value = '';
      input.setSelectionRange(0, 0);
      document.getElementById('gcSrcQual').value = 'WF1';
      gcInsertItem('IT00');
      var v2 = input.value;
      gcRelex();
      _emit({{nSug:nSug, v1:v1, v2:v2, nTok:_gcTokens.length,
             err:_gcLexError ? _gcLexError.message : "", mode: gcModeOf(_gcTokens)}});
    </script>"""
    got = json.loads(run_probe(DEPS, body, js, "mention"))
    assert got["nSug"] >= 1, f"`@IT` 조각에 후보가 없다: {got}"
    assert got["v1"] == '@"IT00"', f"조각 치환이 틀렸다: {got['v1']!r}"
    assert got["v2"] == '@"WF1"!"IT00"', f"source 명시 삽입이 틀렸다: {got['v2']!r}"
    assert got["nTok"] == 1 and not got["err"], got
    assert got["mode"] == "explicit", got
    print("  [browser] `@` 자동완성 + 삽입 OK")


def test_gap_detail_full_points():
    """(l) Gap 상세 CDF 는 **die 1개당 점 1개** — 중복값 접힘·다운샘플 금지 (§5 불변).

    composite 상세가 압축 ECDF(고유값 접힘)로 그려져 다운샘플처럼 보인 회귀(2026-08-26
    신고)의 gap 판 방지선. gap 상세는 /gap_chart 응답의 원본 values 전량을
    distRenderCdf 가 그대로 그려야 한다 — 100 die 가 고유값 3개뿐이어도 점 100개."""
    body = '<div id="distCdf"></div>'
    js = f"""<script>
      {SETUP}
      // 다른 파일(맵/비교/주석) 전역 스텁 — distRenderCdf 가 무조건 부르는 것만 최소로.
      var mapSelChips = [];
      function mapSelMarkerTraces() {{ return null; }}
      function beforeLimitShapes() {{ return []; }}
      function beforeLimitAnnos() {{ return []; }}
      var captured = null;
      window.Plotly = {{
        newPlot: function(div, traces) {{ captured = traces; div.data = traces; div.on = function() {{}}; }},
        purge: function() {{}}
      }};
      var N = 100, vals = [], ser = [], xs = [], ys = [];
      for (var i = 0; i < N; i++) {{
        vals.push([1, 2, 3][i % 3]);         // 고유값 3개뿐인 100 die
        ser.push('S' + i); xs.push(String(i % 10)); ys.push(String((i / 10) | 0));
      }}
      var data = {{ subject: 'G1', is_gap: true, gap_id: 'u1', gap_mode: 'per_source',
        tokens: [{{t:'item',item:'IT00'}}], formula: 'IT00', status: 'ok', units: '',
        lower_limit: null, upper_limit: null,
        sources: [{{ name: 'WF1', values: vals, serial: ser, xpos: xs, ypos: ys }}] }};
      var err = "";
      try {{ distRenderCdf(data); }} catch (e) {{ err = String(e && e.message || e); }}
      var curve = (captured || []).filter(function(t) {{ return t.mode === 'markers'; }})[0];
      _emit({{ err: err, nPts: curve ? curve.x.length : -1 }});
    </script>"""
    got = json.loads(run_probe(DEPS_DETAIL, body, js, "fullpts"))
    assert not got["err"], f"하네스 오류: {got['err']}"
    assert got["nPts"] == 100, \
        f"gap 상세 CDF 점 수가 die 수와 다르다 (다운샘플/고유값 접힘 의심): {got['nPts']}"
    print("  [browser] gap 상세 die 전량 렌더 (100 die → 100점) OK (§5)")


def test_detail_url_no_leak():
    """(k) **R-3 실동작** — gap 상세를 본 뒤 일반 항목을 열면 /scatter 로 가야 한다."""
    body = ('<div class="content"><div class="panel" id="panel-item-detail"></div></div>')
    js = f"""<script>
      {SETUP}
      // chart_notes.js 는 싣지 않으므로 item_detail 이 참조하는 전역만 최소로 세운다.
      var _cnDirty = new Set();
      var calls = [];
      window.fetch = function(u) {{ calls.push(String(u)); return new Promise(function() {{}}); }};
      DATA.gap_charts = {{'u1': {{name:'Gap A-B', sources:['WF1'],
        tokens:[{{t:'item',item:'IT00'}}], limit:{{mode:'none'}}}}}};
      var err = "";
      try {{
        gcOpenDetail('u1');                       // gap 상세 (opts.url 사용)
        openItemDetail('IT00', ['IT00']);          // 일반 항목 — 2인자
      }} catch (e) {{ err = String(e && e.message || e); }}
      _emit({{calls: calls, err: err}});
    </script>"""
    got = json.loads(run_probe(DEPS_DETAIL, body, js, "urlleak"))
    assert not got["err"], f"하네스 오류: {got['err']}"
    calls = got["calls"]
    assert len(calls) == 2, f"요청이 2건이 아니다: {calls}"
    assert "/web_report/gap_chart/u1" in calls[0], calls
    assert "/web_report/scatter/IT00" in calls[1], \
        f"일반 항목이 gap URL 로 조회됐다 (R-3 회귀): {calls}"
    print("  [browser] gap → 일반 항목 URL 누수 없음 OK (R-3)")


def main():
    print("[gap_chart JS 회귀]")
    test_no_es_module()
    test_registered()
    test_hooks()
    test_opts_unconditional()
    test_menu_order()
    test_modal_width()
    test_formula_bar()
    test_css_present()
    if not edge_path():
        print("[SKIP] msedge.exe 를 찾지 못해 브라우저 검사는 건너뜁니다")
        print("[통과] 정적 검사만")
        return
    test_formula_text()
    test_mode_and_validate()
    test_token_html()
    test_cards_html()
    test_formula_bar_render()
    test_lexer()
    test_mention_insert()
    test_gap_detail_full_points()
    test_detail_url_no_leak()
    print("[통과] Gap Chart JS 정상")


if __name__ == "__main__":
    main()

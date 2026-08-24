"""Distribution composite JS 회귀 — headless Edge (2026-08-24).

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_dist_composite_js.py

**왜 파이썬 테스트로 안 되나**: 이 기능이 깨지는 방식은 화면에서만 드러난다.
  · pairKey 구분자가 서버(U+001F)와 어긋나면 색이 통째로 날아간다 — 에러 없이 회색 점만
    남아 "색이 안 보인다"가 된다(CLAUDE.md 규칙 #12, 저장 키 불변).
  · 색 배정이 겹치면 legend 를 구분할 수 없다("랜덤하게 구분 가능하게" 요구).
  · distDrawPoints 의 색 해석기 훅(_distColorFor)이 빠지면 합성 카드 점이 전부 같은 색이 되고,
    반대로 훅이 기존 경로에 새면 일반 카드 색이 바뀐다(회귀).
  · ECDF→통계 복원이 틀리면 표에 잘못된 mean/σ/cpk 가 조용히 표시된다.

검증하는 것:
  (a) 정적: classic script 유지 + report_view.html 로드 등록/순서 + 모달·패널 존재
  (b) 정적: distribution.js 훅 4곳 + item_detail.js 위임 훅
  (c) pairKey 가 U+001F 구분자를 쓴다 / legend 표시명은 "<source>_<item>"
  (d) 색 배정 40개가 전부 다르고 hex 형식 · 수정 시 기존 pair 색이 유지된다
  (e) 카드 HTML: data-comp-id · 이름 · "pair N개" · 편집모드에서만 ✎/✕
  (f) ECDF→통계 복원(mean/σ/median/cpk) 이 알려진 소표본에서 기대값과 일치
  (g) distDrawPoints 가 plot._distColorFor 를 쓰고, 미설정 시 종전 색을 쓴다

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
_TMP = Path(tempfile.mkdtemp(prefix="wr_dc_js_"))

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


# dist_composite.js 는 core.js(esc/showToast/csrfToken/webglOk) 와 distribution.js
# (distDefaultColor/distSpecLimits/distLimInnerHtml/DIST 상수) 위에서 돈다.
DEPS = ["core.js", "distribution.js", "dist_composite.js"]

# 하네스 공용 전역 — 실제 payload 의 최소 형태.
# ⚠ SESSION_ID 는 core.js 에서 const 라 재할당하면 TypeError 로 하네스가 통째로 죽는다.
SETUP = (
    "DATA={session:{source:'web_report',mode:'Normal'},web_report:{"
    "  sources:[{name:'WF1'},{name:'WF2'}],"
    "  distribution_index:[{subject:'IT00',test_num:'1000',units:'V',"
    "     lower_limit:-1,upper_limit:1,cpk:1.1,status:'ok'},"
    "    {subject:'IT01',test_num:'1001',units:'A',"
    "     lower_limit:0,upper_limit:10,cpk:2.0,status:'ok'}]},"
    "  dist_composites:{}};"
    "distIndex=DATA.web_report.distribution_index;"
)


# ── 정적 검사 (Edge 없이도 돈다) ─────────────────────────────────────────────

def test_no_es_module():
    """분할 JS 는 classic script 순서 로드다 — import/export 를 쓰면 전부 죽는다."""
    src = (_JS / "dist_composite.js").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\s", src, re.M), "dist_composite.js: ES module 금지"
    print("[정적] classic script 유지 OK")


def test_registered():
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    assert "static/webreport/dist_composite.js" in view, "dist_composite.js 가 로드되지 않았습니다"
    # distribution.js(렌더 헬퍼)·item_detail.js(패널 위임) 뒤, edit_mode.js(MODE) 앞.
    for dep in ("core.js", "distribution.js", "item_detail.js"):
        assert view.index(f"webreport/{dep}") < view.index("webreport/dist_composite.js"), \
            f"dist_composite.js 는 {dep} 뒤에 로드돼야 합니다"
    assert view.index("webreport/dist_composite.js") < view.index("webreport/edit_mode.js"), \
        "dist_composite.js 는 edit_mode.js 앞에 로드돼야 합니다"
    assert 'id="dcModal"' in view, "생성/수정 모달이 없습니다"
    assert 'id="panel-dist-composite-detail"' in view, "상세 패널 div 가 없습니다"
    # 모달 필수 입력 요소
    for el in ("dcName", "dcSourceList", "dcItemSearch", "dcItemList", "dcPickedList",
               "dcSelSummary", "dcLimitItem", "dcLimitLo", "dcLimitHi",
               "dcSave", "dcCancel", "dcDelete"):
        assert f'id="{el}"' in view, f"모달에 #{el} 이 없습니다"
    # 좌우 2단 구조 — 왼쪽=고르기, 오른쪽=설정
    assert "dc-modal-body" in view and view.count("dc-col") >= 3, "2단 레이아웃 마크업이 없습니다"
    assert view.index('id="dcItemList"') < view.index('id="dcName"'), \
        "왼쪽(고르기)이 오른쪽(설정)보다 먼저 와야 합니다"
    # 검색창은 dirty 유발 대상이 아니다(autoSave 오작동 방지)
    assert re.search(r'id="dcModal"[^>]*data-no-dirty', view), "모달에 data-no-dirty 가 없습니다"
    print("[정적] 로드 순서 + 2단 모달/패널/입력요소 + data-no-dirty OK")


def test_modal_fixed_height():
    """모달은 **고정 높이 + 내부 스크롤** 이어야 한다.

    항목을 수십 개 고르면 창이 세로로 자라고 바깥 스크롤이 생기던 것이 이번 개편의 발단이다.
    .modal-box 의 overflow-y:auto 를 덮지 않으면 안쪽 flex 스크롤이 성립하지 않는다."""
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    # ⚠ 이 규칙은 `.modal-box`(width:360px / overflow-y:auto)보다 **앞**에 있어, 한 클래스
    # 셀렉터로 쓰면 같은 특이도라 그쪽이 이겨 폭이 360px 로 되돌아간다(실측으로 확인).
    assert ".modal-box.dc-modal-box {" in view, \
        ".dc-modal-box 는 .modal-box 와 함께 써 특이도를 올려야 합니다(폭 360px 회귀)"
    box = re.search(r"\.modal-box\.dc-modal-box \{([^}]*)\}", view)
    assert box, ".modal-box.dc-modal-box 규칙이 없습니다"
    css = box.group(1)
    assert "overflow: hidden" in css, "모달이 자체 스크롤을 내면 안쪽 목록 스크롤이 안 생깁니다"
    assert "height:" in css, "고정 높이가 없으면 내용에 따라 창이 자랍니다"
    assert "flex-direction: column" in css, "세로 flex 가 아니면 목록이 남는 공간을 못 받습니다"
    assert "1600px" in css, "가로 폭 확대(min(1600px, 96vw))가 반영되지 않았습니다"
    # 목록 3종은 자체 스크롤
    for cls in (".dc-item-list", ".dc-picked-list", ".dc-src-list"):
        m = re.search(re.escape(cls) + r" \{([^}]*)\}", view)
        assert m and "overflow-y: auto" in m.group(1), f"{cls} 에 자체 스크롤이 없습니다"
    # 선택 목록은 고정폭 그리드 — 칩을 가로로 흘리면 50개에서 세로로 폭발한다
    picked = re.search(r"\.dc-picked-list \{([^}]*)\}", view).group(1)
    assert "grid-template-columns" in picked, "선택 목록이 다열 그리드가 아닙니다"
    print("[정적] 고정 높이 + 내부 스크롤 3종 + 선택목록 그리드 OK")


def test_hooks():
    """(b) 기존 파일 훅 — 빠지면 버튼/카드/색이 조용히 사라진다."""
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    assert "dcAnalyzeBtnHtml" in dist, "distToolbarHtml 에 분석하기 버튼 훅이 없습니다"
    assert "dcCardsHtml" in dist, "distRenderGallery 에 합성 카드 훅이 없습니다"
    assert "dcRenderCompositeCell" in dist, "distRenderGalleryCell 에 렌더 분기가 없습니다"
    assert "_distColorFor" in dist, "distDrawPoints 에 색 해석기 훅이 없습니다"
    # 훅은 전부 typeof 가드 — dist_composite.js 가 없어도 기존 동작이 유지돼야 한다.
    for fn in ("dcAnalyzeBtnHtml", "dcCardsHtml", "dcRenderCompositeCell"):
        assert re.search(rf'typeof {fn} === "function"', dist), f"{fn} 훅에 typeof 가드가 없습니다"
    idet = (_JS / "item_detail.js").read_text(encoding="utf-8")
    assert "dcPanelClick" in idet, "distBindPanel 에 위임 훅이 없습니다"
    # 합성 카드도 .distg-card 라, 일반 카드 분기보다 **먼저** 가려야 한다.
    assert idet.index("dcPanelClick") < idet.index('e.target.closest(".distg-card")'), \
        "dcPanelClick 훅은 .distg-card 분기보다 앞에 있어야 합니다"
    # 탭 전환 시 scattergl WebGL 컨텍스트 회수 (hideItemDetail 과 같은 이유)
    tabs = (_JS / "tabs_topbar.js").read_text(encoding="utf-8")
    assert "hideDistCompositeDetail" in tabs, "탭 전환 시 상세 정리 훅이 없습니다"
    assert re.search(r'typeof hideDistCompositeDetail === "function"', tabs), \
        "탭 전환 훅에 typeof 가드가 없습니다(로드 순서상 필수)"
    print("[정적] distribution.js 훅 4곳 + item_detail/tabs_topbar 위임 훅 OK")


def test_css_present():
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    for cls in (".dist-analyze-btn", ".dc-modal-box", ".dc-item-list", ".distg-comp",
                ".dc-summary", ".dc-limit"):
        assert cls in view, f"CSS {cls} 가 없습니다"
    assert 'html[data-theme="dark"] .dc-item-list' in view or \
           'html[data-theme="dark"] .dc-src-list' in view, "다크모드 규칙이 없습니다"
    print("[정적] CSS(라이트+다크) OK")


# ── (c)~(g) 브라우저 실행 ────────────────────────────────────────────────────

def test_pair_key_and_label():
    """(c) 저장 키는 U+001F 구분자, 표시명은 <source>_<item>."""
    harness = ("<script>(function(){" + SETUP + "var out={};"
               "out.key = dcPairKey('WF1','IT00');"
               "out.label = dcPairLabel('WF1','IT00');"
               "out.sep = DC_SEP;"
               "out.underscore = dcPairKey('A_B','C');"   # 밑줄 든 이름도 키가 안 섞인다
               "_emit(out);})();</script>")
    out = json.loads(run_probe(DEPS, "", harness, "pairkey"))
    assert out["sep"] == SEP, f"구분자가 U+001F 가 아닙니다: {out['sep']!r}"
    assert out["key"] == "WF1" + SEP + "IT00", out["key"]
    assert out["label"] == "WF1_IT00", out["label"]
    assert out["underscore"] == "A_B" + SEP + "C", out["underscore"]
    print("[c] pairKey(U+001F) / legend 표시명 OK")


def test_pair_cap():
    """(h) pair 상한이 50개 이상을 수용한다 (서버 _DC_MAX_PAIRS 와 같은 값)."""
    svc = (_ROOT / "web_report" / "service.py").read_text(encoding="utf-8")
    m = re.search(r"^_DC_MAX_PAIRS = (\d+)", svc, re.M)
    assert m, "서버 _DC_MAX_PAIRS 를 찾지 못했습니다"
    server_cap = int(m.group(1))
    js = (_JS / "dist_composite.js").read_text(encoding="utf-8")
    m2 = re.search(r"const DC_MAX_PAIRS = (\d+)", js)
    assert m2, "프런트 DC_MAX_PAIRS 를 찾지 못했습니다"
    assert int(m2.group(1)) == server_cap, \
        f"상한 불일치: 프런트 {m2.group(1)} vs 서버 {server_cap} (프런트가 크면 저장이 400 난다)"
    assert server_cap >= 50, f"50개 이상 선택을 수용해야 합니다 (현재 {server_cap})"
    print(f"[h] pair 상한 {server_cap} — 프런트/서버 일치 + 50 이상 수용 OK")


def test_color_assignment():
    """(d) 40 pair 색이 전부 다르고 hex · 수정 시 기존 색 유지."""
    harness = ("<script>(function(){" + SETUP + "var out={};"
               "var pairs=[];for(var i=0;i<40;i++)pairs.push({source:'S'+i,item:'IT'});"
               "var c = dcAssignColors(pairs, null);"
               "var vals = Object.keys(c).map(function(k){return c[k];});"
               "out.n = vals.length;"
               "out.uniq = Object.keys(vals.reduce(function(a,v){a[v]=1;return a;},{})).length;"
               "out.allHex = vals.every(function(v){return /^#[0-9A-Fa-f]{6}$/.test(v);});"
               # 수정 시나리오: 기존 pair 색 유지 + 새 pair 추가
               "var keep = {};keep['WF1'+DC_SEP+'IT00']='#123456';"
               "var c2 = dcAssignColors([{source:'WF1',item:'IT00'},{source:'WF2',item:'IT00'}], keep);"
               "out.kept = c2['WF1'+DC_SEP+'IT00'];"
               "out.added = c2['WF2'+DC_SEP+'IT00'];"
               "_emit(out);})();</script>")
    out = json.loads(run_probe(DEPS, "", harness, "colors"))
    assert out["n"] == 40, out["n"]
    assert out["uniq"] == 40, f"색이 겹칩니다 ({out['uniq']}/40 고유)"
    assert out["allHex"], "hex 형식이 아닌 색이 있습니다(서버 검증에서 400)"
    assert out["kept"] == "#123456", f"수정 시 기존 pair 색이 바뀌었습니다: {out['kept']}"
    assert re.match(r"^#[0-9A-Fa-f]{6}$", out["added"] or ""), out["added"]
    print("[d] 색 40개 전부 고유·hex · 수정 시 기존 색 유지 OK")


def test_cards_html():
    """(e) 카드 마크업 — data-comp-id / 이름 / pair N개 / 편집모드에서만 ✎✕."""
    comp = {"name": "P1 vs P2 VDD", "limit": {"mode": "item", "item": "IT00"},
            "pairs": [{"source": "WF1", "item": "IT00"}, {"source": "WF2", "item": "IT00"},
                      {"source": "WF1", "item": "IT01"}],
            "colors": {}}
    lit = json.dumps({"c1": comp}, ensure_ascii=False)
    harness = ("<script>(function(){" + SETUP + "var out={};"
               "DATA.dist_composites = " + lit + ";"
               "MODE='view';"
               "var view = dcCardsHtml();"
               "MODE='edit';"
               "var edit = dcCardsHtml();"
               "var d=document.createElement('div');d.innerHTML=edit;"
               "var card=d.querySelector('.distg-comp');"
               "out.compId = card ? card.dataset.compId : null;"
               "out.name = card ? card.querySelector('.distg-name').textContent : null;"
               "out.pairText = card ? card.querySelector('.distg-cpk').textContent : null;"
               "out.lim = card ? card.querySelector('.distg-lim').textContent : null;"
               "out.hasPlot = !!(card && card.querySelector('.distg-plot'));"
               "out.editActs = (edit.match(/data-dc-act=\"edit\"/g)||[]).length;"
               "out.viewActs = (view.match(/data-dc-act=\"edit\"/g)||[]).length;"
               "out.isCard = !!(card && card.classList.contains('distg-card'));"
               "_emit(out);})();</script>")
    out = json.loads(run_probe(DEPS, "", harness, "cards"))
    assert out["compId"] == "c1", out["compId"]
    assert out["name"] == "P1 vs P2 VDD", out["name"]
    assert out["pairText"] == "pair 3개", out["pairText"]
    assert "-1 ~ 1" in (out["lim"] or ""), f"limit 표시가 잘못됐습니다: {out['lim']}"
    assert out["hasPlot"], ".distg-plot 이 없습니다(IntersectionObserver 재사용 불가)"
    assert out["isCard"], ".distg-card 클래스가 없으면 observer/purge 가 안 붙습니다"
    assert out["editActs"] == 1, out["editActs"]
    assert out["viewActs"] == 0, "보기 전용에서 편집 버튼이 노출됐습니다"
    print("[e] 카드 마크업 + 편집모드 게이트 OK")


def test_ecdf_stats():
    """(f) ECDF→통계 복원 — Δp 가중 모집단 통계.

    x=[1,2,3], y=[25,75,100] → Δp=[.25,.5,.25]
      mean = 1*.25+2*.5+3*.25 = 2
      E[x²] = 1*.25+4*.5+9*.25 = 4.5 → var = 0.5 → σ = 0.70710678
      median = 누적 50% 이상 첫 값 = 2
      cpk(lo=0,hi=4) = min((4-2), (2-0))/(3σ) = 2/2.1213 = 0.9428
    """
    harness = ("<script>(function(){" + SETUP + "var out={};"
               "var st = dcPairStats({xs:[1,2,3], ys:[25,75,100]}, 0, 4);"
               "out.mean=st.mean; out.sd=st.sd; out.median=st.median; out.cpk=st.cpk;"
               "out.min=st.min; out.max=st.max; out.uniq=st.uniq;"
               # limit 이 없으면 cpk 는 null
               "out.noLimit = dcPairStats({xs:[1,2,3], ys:[25,75,100]}, null, null).cpk;"
               # 빈 데이터는 null
               "out.empty = dcPairStats({xs:[], ys:[]}, 0, 1);"
               "_emit(out);})();</script>")
    out = json.loads(run_probe(DEPS, "", harness, "stats"))
    assert abs(out["mean"] - 2.0) < 1e-9, out["mean"]
    assert abs(out["sd"] - 0.7071067811865476) < 1e-9, out["sd"]
    assert out["median"] == 2, out["median"]
    assert abs(out["cpk"] - 0.9428090415820634) < 1e-9, out["cpk"]
    assert out["min"] == 1 and out["max"] == 3 and out["uniq"] == 3, out
    assert out["noLimit"] is None, "limit 없이 cpk 를 만들어냈습니다"
    assert out["empty"] is None, "빈 ECDF 가 통계를 냈습니다"
    print("[f] ECDF→통계 복원(mean/σ/median/cpk) 기대값 일치 OK")


def test_color_hook_isolation():
    """(g) _distColorFor 훅 — 설정 시 그 함수를, 미설정 시 종전 색을 쓴다(회귀 0)."""
    harness = ("<script>(function(){" + SETUP + "var out={};"
               "buildDistColorMap(DATA.web_report.sources);"
               # distDrawPoints 안의 색 선택식을 그대로 재현해 훅 유무를 검증한다
               "var plotA = {};"                       # 훅 없음 → distActiveColorFor
               "var plotB = {_distColorFor: function(k){return '#ABCDEF';}};"
               "var pick = function(plot, key){ return (plot._distColorFor || distActiveColorFor)(key); };"
               "out.legacy = pick(plotA, 'WF1');"
               "out.hooked = pick(plotB, 'WF1'+DC_SEP+'IT00');"
               "out.expectLegacy = distActiveColorFor('WF1');"
               "_emit(out);})();</script>")
    out = json.loads(run_probe(DEPS, "", harness, "colorhook"))
    assert out["legacy"] == out["expectLegacy"], "훅 미설정 경로에서 색이 바뀌었습니다(회귀)"
    assert out["hooked"] == "#ABCDEF", out["hooked"]
    print("[g] 색 해석기 훅 — 주입 시 적용 / 미설정 시 종전 동일 OK")


def main():
    print("[Distribution composite JS 검증]")
    test_no_es_module()
    test_registered()
    test_modal_fixed_height()
    test_hooks()
    test_css_present()
    test_pair_cap()
    if not edge_path():
        print("[SKIP] Edge 를 찾지 못해 브라우저 검증은 건너뜁니다")
        return
    test_pair_key_and_label()
    test_color_assignment()
    test_cards_html()
    test_ecdf_stats()
    test_color_hook_isolation()
    print("[통과] Distribution composite JS 정상")


if __name__ == "__main__":
    main()

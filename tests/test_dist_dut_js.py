"""Distribution "DUT 별 분리" 프런트 회귀 — headless Edge (2026-09-03).

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_dist_dut_js.py

왜 파이썬 테스트로 안 되나: 이 기능이 깨지는 방식은 전부 **화면에서만, 조용히** 드러난다.
  · `distGalleryVariant()` 가 dut 키를 반환하게 되면 dist_composite.js `_dcCache[key]` /
    gap_chart.js `_gcCache[key]` 가 undefined 를 인덱싱해 합성 카드·Gap 카드가 죽고,
    item_detail 의 /scatter URL 에 서버가 모르는 값이 붙는다.
  · 합성 카드(composite)는 색이 pairKey(source+item) 라 DUT 분할 이름이 오면 **전 시리즈가
    회색**이 된다 — 에러가 아니라 "색이 사라짐" 으로만 보인다.
  · 제외 칩(cdfExcluded) 키를 분할 이름으로 만들면 토글할 때마다 **이미 제외한 die 가
    되살아난다**(CLAUDE.md §5-12 — 사용자 입력은 무슨 일이 있어도 잃지 않는다).
  · seq×dut 에서 x 를 DUT 안에서 1..m 으로 다시 매기면 **interleave 측정 순서가 소실**돼
    조용히 틀린 run chart 가 된다.
  · 서버/JS 상수 짝(구분자·배치 상한)이 어긋나면 "저장은 되는데 400" 같은 형태로 나온다.

검증하는 것:
  (a) 정적: **distGalleryVariant() 에 dut 가 없다** (R2 — 가장 중요)
  (b) 정적: DIST_VARIANTS 12종 == distGalleryDataVariant 3축 곱집합
  (c) 정적: gap_chart `_gcCache` 3종 리터럴 유지 (DUT 미적용 계약)
  (d) 정적: dist_composite `dcDropDut` 존재 + 사용 (composite 제외 계약)
  (e) 정적: **cdfChipKey( 직접 호출이 cdfKeyOf 정의부 1곳뿐** (R4)
  (f) 정적: DIST_DUT_SEP == 서버 DUT_SOURCE_SEP (규칙 15 짝)
  (g) 정적: DIST_BATCH.DUT_SIZE <= 서버 _DIST_DUT_BATCH_MAX (규칙 15 짝)
  (h) 정적: FILL_VISUAL_MAX_DY 를 n 폴백 밖에서 쓰지 않는다 (R13)
  (i) 정적: 툴바 메뉴 항목 · 상세 버튼 · 토글 핸들러
  (j) 브라우저: 변형 키/쿼리 매핑 12조합 + 캐시 12벌이 서로 다른 객체
  (k) 브라우저: distDutBase/Label 오파싱 방어 3케이스
  (l) 브라우저: distDutSortCmp 가 서버 _dut_sort_key 와 같은 순서
  (m) 브라우저: distSplitSourcesByDut — 값·메타·idx 가 같은 순서로 갈린다
  (n) 브라우저: distDutColor n=1/n=8 · distMakeDutColorFor 강조(dim)가 base 기준
  (o) 브라우저: IDET_DUT_MAX_TRACES 초과 시 분할하지 않고 원본 반환

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
_TMP = Path(tempfile.mkdtemp(prefix="wr_dut_js_"))

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


DEPS = ["core.js", "distribution.js", "item_detail.js"]
DEPS_ALL = ["core.js", "map_select.js", "distribution.js", "item_detail.js",
            "dist_composite.js", "gap_chart.js"]

# ⚠ SESSION_ID 는 core.js 에서 const, MODE 는 let 이라 재선언하면 하네스가 통째로 죽는다.
SETUP = (
    "DATA={session:{source:'web_report',mode:'Normal'},web_report:{"
    "  sources:[{name:'WF1'},{name:'WF2'}],"
    "  distribution_index:[{subject:'IT00',test_num:'1000',units:'V',"
    "     lower_limit:-1,upper_limit:1,cpk:1.1,status:'ok'}]}};"
    "distIndex=DATA.web_report.distribution_index;"
    "buildDistColorMap(DATA.web_report.sources);"
)


def _fn_body(src, name):
    """`function name(...) { … }` 본문을 중괄호 균형으로 잘라낸다(정적 검사용)."""
    m = re.search(r"function\s+%s\s*\(" % re.escape(name), src)
    assert m, f"{name} 정의를 찾지 못했습니다"
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"{name} 본문이 닫히지 않았습니다")


# ── 정적 검사 (Edge 없이도 돈다) ─────────────────────────────────────────────

def test_static_gallery_variant_clean():
    """(a) **R2** — distGalleryVariant() 는 bin1 축만 반환해야 한다."""
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    body = _fn_body(dist, "distGalleryVariant")
    assert "dut" not in body.lower(), (
        "distGalleryVariant() 에 dut 가 섞였습니다 — dist_composite._dcCache / "
        "gap_chart._gcCache 가 이 값을 캐시 인덱스로 직접 씁니다(undefined 로 죽습니다).\n"
        + body)
    assert "seq" not in body.lower(), "distGalleryVariant() 에 seq 가 섞였습니다(기존 계약)"
    dbody = _fn_body(dist, "distGalleryDataVariant")
    assert "distDutOnly" in dbody, "distGalleryDataVariant 가 DUT 축을 반영하지 않습니다"
    print("[정적] distGalleryVariant() 는 bin1 축만 — 합성/Gap 캐시 인덱스 보호 OK")


def test_static_variants_complete():
    """(b) DIST_VARIANTS 12종 == 3축 곱집합 (없는 키면 all 로 폴백해 다른 축을 그린다)."""
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    m = re.search(r"const DIST_VARIANTS = \[([\s\S]*?)\];", dist)
    assert m, "DIST_VARIANTS 정의를 찾지 못했습니다"
    got = set(re.findall(r'"([^"]+)"', m.group(1)))

    def data_variant(b, dut, seq):     # distGalleryDataVariant 와 같은 로직
        k = ("dut" if b == "all" else "dut-" + b) if dut else b
        if not seq:
            return k
        return "seq" if k == "all" else "seq-" + k

    want = {data_variant(b, d, s)
            for b in ("all", "bin1", "rtbin1") for d in (0, 1) for s in (0, 1)}
    assert got == want, f"변형 목록 불일치\n  누락 {sorted(want - got)}\n  초과 {sorted(got - want)}"
    assert len(got) == 12, f"변형이 12종이 아닙니다: {len(got)}"
    # 캐시 switch 도 12분기여야 한다(빠지면 조용히 distDataCache 로 폴백).
    cbody = _fn_body(dist, "distCacheFor")
    for k in sorted(want - {"all"}):
        assert f'case "{k}"' in cbody, f"distCacheFor 에 {k} 분기가 없습니다"
    print(f"[정적] DIST_VARIANTS 12종 == 3축 곱집합 · distCacheFor 12분기 OK")


def test_static_composite_gap_excluded():
    """(c)(d) Gap 은 리터럴 3종 유지(자동 제외) · composite 는 dcDropDut 로 명시 제외."""
    gap = (_JS / "gap_chart.js").read_text(encoding="utf-8")
    m = re.search(r"^const _gcCache = (\{.*\});", gap, re.M)
    assert m, "_gcCache 정의를 찾지 못했습니다"
    keys = set(re.findall(r"(\w+)\s*:", m.group(1)))
    assert keys == {"all", "bin1", "rtbin1"}, \
        f"_gcCache 가 3종이 아닙니다: {sorted(keys)} (Gap 은 DUT 축이 없다 — 계약)"
    assert "distGalleryDataVariant" not in gap, \
        "gap_chart 가 데이터 변형 키를 쓰면 DUT 배치를 받아 캐시가 어긋납니다"

    dc = (_JS / "dist_composite.js").read_text(encoding="utf-8")
    assert "function dcDropDut" in dc, "dcDropDut 이 없습니다 (합성 카드 색이 전부 회색이 됩니다)"
    bare = re.findall(r"(?<!dcDropDut\()distGalleryDataVariant\(\)", dc)
    assert not bare, \
        f"dcDropDut 없이 distGalleryDataVariant() 를 쓰는 곳이 {len(bare)}곳 있습니다"
    print("[정적] Gap 자동 제외(3종 리터럴) · composite dcDropDut 적용 OK")


def test_static_chip_key_unified():
    """(e) **R4** — 제외 칩 키는 cdfKeyOf 한 곳에서만 만든다(base source 로 정규화)."""
    idet = (_JS / "item_detail.js").read_text(encoding="utf-8")
    assert "function cdfKeyOf" in idet, "cdfKeyOf 헬퍼가 없습니다"
    calls = re.findall(r"cdfChipKey\(", idet)
    # 정의(function cdfChipKey) 1 + cdfKeyOf 안의 호출 1 = 2 개만 허용.
    assert len(calls) == 2, (
        f"cdfChipKey( 사용이 {len(calls)}곳입니다 — 전부 cdfKeyOf 경유여야 합니다.\n"
        "하나라도 남으면 DUT 토글 시 이미 제외한 die 가 되살아납니다(§5-12).")
    kbody = _fn_body(idet, "cdfKeyOf")
    assert "distDutBase" in kbody, "cdfKeyOf 가 base source 로 정규화하지 않습니다"
    print("[정적] 제외 칩 키는 cdfKeyOf 한 곳 · base source 정규화 OK")


def test_static_server_js_pairs():
    """(f)(g) 서버/JS 이중 정의 상수 짝 (CLAUDE.md §5 규칙 15)."""
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    py = (_ROOT / "web_report" / "dist_dut.py").read_text(encoding="utf-8")
    routes = (_ROOT / "server" / "report" / "routes_webreport.py").read_text(encoding="utf-8")

    js_sep = re.search(r'const DIST_DUT_SEP = "([^"]*)"', dist)
    py_sep = re.search(r'DUT_SOURCE_SEP = "([^"]*)"', py)
    assert js_sep and py_sep, "구분자 상수를 찾지 못했습니다"
    assert js_sep.group(1) == py_sep.group(1), (
        f"구분자 불일치: JS {js_sep.group(1)!r} != PY {py_sep.group(1)!r} "
        "— 시리즈 이름 파싱이 통째로 깨집니다")

    js_size = int(re.search(r"DUT_SIZE:\s*(\d+)", dist).group(1))
    srv_max = int(re.search(r"_DIST_DUT_BATCH_MAX = (\d+)", routes).group(1))
    assert js_size <= srv_max, f"DUT_SIZE({js_size}) > 서버 상한({srv_max}) — 400 이 납니다"
    print(f"[정적] 짝 상수 OK (구분자 {js_sep.group(1)!r} · DUT_SIZE {js_size} <= {srv_max})")


def test_static_fill_cap_untouched():
    """(h) **R13** — 세로 채움 간격에 고정 상수 상한을 n 폴백 밖에서 걸지 않는다."""
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    body = _fn_body(dist, "distStepY")
    # n 이 있는 경로는 반드시 100/n 을 쓴다(표본 수 = 채우는 점 개수).
    assert re.search(r"100\s*/\s*n\b", body), "distStepY 가 100/n 을 쓰지 않습니다(R13)"
    print("[정적] distStepY 100/n 유지 — DUT 별 n 이 그대로 반영됨 (R13) OK")


def test_static_wiring():
    """(i) 툴바 메뉴 항목 · 상세 버튼 · 토글 핸들러 · classic script."""
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    idet = (_JS / "item_detail.js").read_text(encoding="utf-8")
    for name, src in (("distribution.js", dist), ("item_detail.js", idet)):
        assert not re.search(r"^\s*(import|export)\s", src, re.M), f"{name}: ES module 금지"
    assert '"dut", "DUT 별 분리"' in dist, "Chart Option 메뉴에 DUT 항목이 없습니다"
    # DUT 모드 세션에서는 항목을 내린다(이미 분할돼 있어 다시 쪼갤 게 없다).
    mbody = _fn_body(dist, "distChartMenuItems")
    assert 'webReportMode() === "DUT"' in mbody, "DUT 모드 가드가 없습니다"
    assert 'seg.dataset.seg === "dut"' in idet, "갤러리 툴바 dut 토글 핸들러가 없습니다"
    assert 'data-idet-seg="dut"' in idet, "Item_detail DUT 버튼이 없습니다"
    assert 'kind === "dut"' in idet, "Item_detail dut 토글 핸들러가 없습니다"
    print("[정적] 툴바 메뉴 · DUT 모드 가드 · 상세 버튼 · 토글 핸들러 OK")


# ── 브라우저 검사 ────────────────────────────────────────────────────────────

def test_variant_map():
    """(j) 12조합 변형 키/쿼리 + 캐시 12벌이 서로 다른 객체."""
    js = """<script>
      %s
      var map={}, q={};
      [['all',0,0],['all',0,1],['all',1,0],['all',1,1],
       ['bin1',0,0],['bin1',0,1],['bin1',1,0],['bin1',1,1],
       ['rtbin1',0,0],['rtbin1',0,1],['rtbin1',1,0],['rtbin1',1,1]].forEach(function(t){
        distBin1Only = (t[0]==='bin1'); distRtBin1Only = (t[0]==='rtbin1');
        distDutOnly = !!t[1]; distSeqOnly = !!t[2];
        var k = distGalleryDataVariant();
        map[t.join('/')] = k; q[k] = distVariantQuery(k);
      });
      // ⚠ 변수명 `caches` 는 브라우저 전역(Cache Storage API)이라 덮이지 않는다 — 쓰면
      // 배열이 아닌 CacheStorage 가 남아 `.map is not a function` 으로 죽는다.
      var _cacheList = DIST_VARIANTS.map(function(k){ return distCacheFor(k); });
      var distinct = _cacheList.every(function(c,i){ return _cacheList.indexOf(c)===i; });
      _emit({map:map, q:q, distinct:distinct, n:DIST_VARIANTS.length});
    </script>""" % SETUP
    got = json.loads(run_probe(DEPS, "", js, "variant"))
    assert got["n"] == 12, got["n"]
    assert got["distinct"], "변형 캐시 12벌 중 같은 객체를 공유하는 것이 있습니다"
    assert got["map"]["all/1/0"] == "dut", got["map"]
    assert got["map"]["bin1/1/0"] == "dut-bin1", got["map"]
    assert got["map"]["rtbin1/1/1"] == "seq-dut-rtbin1", got["map"]
    assert got["map"]["all/0/0"] == "all", got["map"]
    # 쿼리 — dut 는 &dut=1, 조합은 둘 다
    assert got["q"]["dut"] == "&dut=1", got["q"]["dut"]
    assert got["q"]["all"] == "", got["q"]["all"]
    assert "&bin1=1" in got["q"]["dut-bin1"] and "&dut=1" in got["q"]["dut-bin1"], got["q"]
    assert "bin1_scope=rt" in got["q"]["seq-dut-rtbin1"], got["q"]
    for k in ("seq-dut", "seq-dut-bin1", "seq-dut-rtbin1"):
        assert "&order=seq" in got["q"][k] and "&dut=1" in got["q"][k], (k, got["q"][k])
    print("  [browser] 12조합 변형 키·쿼리 · 캐시 12벌 독립 OK")


def test_base_label_parse():
    """(k)(l) base/label 오파싱 방어 · 정렬이 서버 _dut_sort_key 와 같다."""
    js = """<script>
      %s
      var r = {
        normal: [distDutBase('WF1 \\u00b7 DUT 3'), distDutLabel('WF1 \\u00b7 DUT 3')],
        plain:  [distDutBase('WF1'), distDutLabel('WF1')],
        // payload 에 없는 base → 분할 이름이 아니라고 보고 원본 유지(오파싱 방어)
        bogus:  [distDutBase('ZZZ \\u00b7 DUT 3'), distDutLabel('ZZZ \\u00b7 DUT 3')],
        sorted: ['10','2','(blank)','1'].slice().sort(distDutSortCmp)
      };
      _emit(r);
    </script>""" % SETUP
    got = json.loads(run_probe(DEPS, "", js, "parse"))
    assert got["normal"] == ["WF1", "3"], got["normal"]
    assert got["plain"] == ["WF1", ""], got["plain"]
    assert got["bogus"] == ["ZZZ \u00b7 DUT 3", ""], \
        f"오파싱 방어 실패: {got['bogus']} — payload 에 없는 base 는 원본을 유지해야 합니다"
    # 서버 _dut_sort_key: 숫자 수치 오름차순, 비숫자는 뒤로 문자순
    assert got["sorted"] == ["1", "2", "10", "(blank)"], \
        f"정렬이 서버와 다릅니다: {got['sorted']} (문자 정렬이면 1,10,2 가 된다)"
    print("  [browser] base/label 파싱 3케이스 · 정렬 서버 일치 OK")


def test_split_sources():
    """(m) distSplitSourcesByDut — 값·메타·idx 가 같은 순서로 갈린다."""
    js = """<script>
      %s
      var src = [{name:'WF1', values:[10,20,30,40], dut:['2','1','2','1'],
                  serial:['a','b','c','d'], xpos:[1,2,3,4], ypos:[1,1,1,1]}];
      var out = distSplitSourcesByDut(src);
      // dut 배열이 없으면 원본 그대로 (옛 캐시 응답 방어)
      var nodut = distSplitSourcesByDut([{name:'WF1', values:[1,2]}]);
      // 길이 불일치도 원본 그대로
      var bad = distSplitSourcesByDut([{name:'WF1', values:[1,2], dut:['1']}]);
      _emit({names:out.map(function(s){return s.name;}),
             vals:out.map(function(s){return s.values;}),
             idx:out.map(function(s){return s.idx;}),
             ser:out.map(function(s){return s.serial;}),
             nodut:nodut.length===1 && !nodut[0].idx,
             bad:bad.length===1 && bad[0].values.length===2});
    </script>""" % SETUP
    got = json.loads(run_probe(DEPS, "", js, "split"))
    assert got["names"] == ["WF1 \u00b7 DUT 1", "WF1 \u00b7 DUT 2"], got["names"]
    assert got["vals"] == [[20, 40], [10, 30]], got["vals"]
    # idx 는 **원본 행 번호**(1-based) — DUT 안에서 1..m 으로 다시 매기면 interleave 소실
    assert got["idx"] == [[2, 4], [1, 3]], \
        f"idx 가 원본 행 순서가 아닙니다: {got['idx']} (R5 — 측정 순서 소실)"
    assert got["ser"] == [["b", "d"], ["a", "c"]], got["ser"]
    assert got["nodut"], "dut 배열이 없는데 분할했습니다"
    assert got["bad"], "dut 길이가 안 맞는데 분할했습니다"
    print("  [browser] 분할 값·serial·idx(원본 행순) · 폴백 2종 OK")


def test_colors():
    """(n) DUT 색 변주 · 강조(dim) 판정이 base 기준."""
    js = """<script>
      %s
      var names = ['WF1 \\u00b7 DUT 1','WF1 \\u00b7 DUT 2','WF2 \\u00b7 DUT 1'];
      var f = distMakeDutColorFor(names);
      var noFilter = names.map(f);
      distSourceFilter = new Set(['WF1']);          // base 를 고른다(범례 클릭과 같음)
      var g = distMakeDutColorFor(names);
      var filtered = names.map(g);
      distSourceFilter = new Set();
      _emit({one: distDutColor('#112233', 0, 1),
             eight: [0,1,2,3,4,5,6,7].map(function(i){return distDutColor('#808080', i, 8);}),
             noFilter: noFilter, filtered: filtered, dim: DIST_DIM_COLOR});
    </script>""" % SETUP
    got = json.loads(run_probe(DEPS, "", js, "colors"))
    assert got["one"] == "#112233", f"n=1 이면 base 색 그대로여야 합니다: {got['one']}"
    assert len(set(got["eight"])) == 8, f"8 DUT 색이 중복됩니다: {got['eight']}"
    assert len(set(got["noFilter"][:2])) == 2, "같은 source 의 DUT 색이 같습니다"
    # 강조: WF1 의 두 DUT 는 원색 유지, WF2 는 dim
    assert got["filtered"][0] == got["noFilter"][0], "강조된 base 의 색이 바뀌었습니다"
    assert got["filtered"][1] == got["noFilter"][1], "강조된 base 의 색이 바뀌었습니다"
    assert got["filtered"][2] == got["dim"], \
        f"비선택 source 가 dim 이 아닙니다: {got['filtered'][2]} (base 기준 판정 실패)"
    print("  [browser] 명도 변주 8색 고유 · 강조 판정 base 기준 OK")


def test_trace_cap():
    """(o) IDET_DUT_MAX_TRACES 초과 시 분할하지 않고 원본 유지(그룹핑만 되돌림)."""
    js = """<script>
      %s
      // 상한을 넘기는 시리즈 수를 만든다 (source 1개 × DUT 다수)
      var n = IDET_DUT_MAX_TRACES + 5, duts = [], vals = [];
      for (var i = 0; i < n; i++) { duts.push(String(i)); vals.push(i); }
      var out = distSplitSourcesByDut([{name:'WF1', values:vals, dut:duts}]);
      _emit({cap: IDET_DUT_MAX_TRACES, len: out.length, name: out[0].name,
             toast: document.getElementById('toast').textContent});
    </script>""" % SETUP
    # showToast(core.js)가 #toast 를 직접 참조하므로 하네스에 그 자리를 만들어 준다.
    got = json.loads(run_probe(DEPS, "<div id='toast'></div>", js, "cap"))
    assert "너무 많아" in got["toast"], f"상한 안내 토스트가 없습니다: {got['toast']!r}"
    assert got["len"] == 1, f"상한 초과인데 분할했습니다: {got['len']} trace"
    assert got["name"] == "WF1", got["name"]
    print(f"  [browser] trace 상한 {got['cap']} 초과 → source 단위 유지 OK")


def main():
    print("[Distribution DUT 별 분리 JS 회귀]")
    test_static_gallery_variant_clean()
    test_static_variants_complete()
    test_static_composite_gap_excluded()
    test_static_chip_key_unified()
    test_static_server_js_pairs()
    test_static_fill_cap_untouched()
    test_static_wiring()
    if not edge_path():
        print("[SKIP] Edge 를 찾지 못해 브라우저 검사는 건너뜁니다")
        return
    test_variant_map()
    test_base_label_parse()
    test_split_sources()
    test_colors()
    test_trace_cap()
    print("[통과] DUT 별 분리 프런트 계약 정상")


if __name__ == "__main__":
    main()

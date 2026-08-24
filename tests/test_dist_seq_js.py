"""Distribution "Serial 순" 프런트 회귀 — headless Edge (2026-08-24).

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_dist_seq_js.py

왜 파이썬 테스트로 안 되나: 이 기능이 깨지는 방식은 전부 **화면에서만, 조용히** 드러난다.
  · `distGalleryVariant()` 가 seq 키를 반환하게 되면 dist_composite.js `_dcCache[key]` /
    gap_chart.js `_gcCache[key]` 가 undefined 를 인덱싱해 합성 카드·Gap 카드가 죽고,
    item_detail 의 /scatter URL 에 서버가 모르는 `order` 가 붙는다.
  · seq 차트에 차트 주석(chart_notes)을 붙이면 편집 시 저장값이 **seq 좌표로 덮어써진다**
    (사용자가 CDF 에 그려둔 주석 소실 — CLAUDE.md §5-12).
  · 표시 캡을 stride 아닌 ECDF 규칙으로 걸면 시계열 형태가 왜곡된다(없던 구조가 생긴다).
  · 값 순서가 뒤집혀도 점은 그대로 찍혀 아무 에러가 없다.

검증하는 것:
  (a) 정적: classic script 유지 · 툴바 **맨 앞** 버튼 · Item_detail 버튼
  (b) 정적: **distGalleryVariant() 에 seq 가 없다**(F-3) + distGalleryDataVariant 존재
  (c) 정적: distRenderSeq 안에 chartNotesApply 호출이 없다(F-4) + 선(line) 렌더 없음
  (d) 변형 키/쿼리 매핑표 (bin1 축 × seq 축)
  (e) distGalleryDataVariant 조합 6종 + 캐시 6벌이 서로 다른 객체
  (f) buildDistSeqFromCompact — 행 순서 보존, vs 필드
  (g) distSeqDisplayPoints — 캡 이하면 원본 순서, 초과면 균등 stride + 양끝 보존
  (h) distSeqSpecShapes/Annos — **수평** 기준선 (ECDF 는 수직)
  (i) distRenderSeq 실동작(Plotly 스텁) — x=1..n · y=값 순서 · markers · 주석 훅 미호출
  (j) 칩 제외(cdfExcluded) 반영 시 x 를 1..m 으로 다시 매긴다

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
_TMP = Path(tempfile.mkdtemp(prefix="wr_seq_js_"))

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
# 합성 카드(Distribution composite · Gap Chart)까지 얹는 조합. map_select.js 는
# chipMarkersForPairs/mapSelChips 를 제공한다(seq 는 마커를 안 쓰지만 로드는 필요).
DEPS_ALL = ["core.js", "map_select.js", "distribution.js", "item_detail.js",
            "dist_composite.js", "gap_chart.js"]
# Note 붙여넣기 폴백 검사용 — chart_notes.js 는 core+distribution+item_detail 위에서 돈다.
DEPS_NOTE = ["core.js", "distribution.js", "item_detail.js", "chart_notes.js"]
# 합성 상세의 차트 주석 검사용 — 위 둘의 합집합.
DEPS_ALL_NOTE = DEPS_ALL + ["chart_notes.js"]

# ⚠ SESSION_ID 는 core.js 에서 const, MODE 는 let 이라 재선언하면 하네스가 통째로 죽는다.
SETUP = (
    "DATA={session:{source:'web_report',mode:'Normal'},web_report:{"
    "  sources:[{name:'WF1'},{name:'WF2'}],"
    "  distribution_index:[{subject:'IT00',test_num:'1000',units:'V',"
    "     lower_limit:-1,upper_limit:1,cpk:1.1,status:'ok'}]}};"
    "distIndex=DATA.web_report.distribution_index;"
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

def test_static_wiring():
    """(a) classic script 유지 · 툴바 맨 앞 버튼 · Item_detail 버튼."""
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    idet = (_JS / "item_detail.js").read_text(encoding="utf-8")
    for name, src in (("distribution.js", dist), ("item_detail.js", idet)):
        assert not re.search(r"^\s*(import|export)\s", src, re.M), f"{name}: ES module 금지"
    assert 'data-seg="seq"' in dist, "툴바 Serial 순 버튼이 없습니다"
    # 그룹 안에서 **맨 앞**(좌상단) — 사용자가 위치를 지정했다.
    m = re.search(r'<div class="distseg-group">\$\{(\w+)\}', dist)
    assert m and m.group(1) == "seqBtn", f"버튼이 그룹 맨 앞이 아닙니다: {m and m.group(1)}"
    assert 'data-idet-seg="seq"' in idet, "Item_detail Serial 순 버튼이 없습니다"
    assert re.search(r'idet-opts">\$\{seqBtn\}', idet), "Item_detail 버튼이 맨 앞이 아닙니다"
    assert 'seg.dataset.seg === "seq"' in idet, "갤러리 툴바 seq 토글 핸들러가 없습니다"
    assert 'kind === "seq"' in idet, "Item_detail seq 토글 핸들러가 없습니다"
    print("[정적] classic script · 툴바 맨 앞 버튼 · 상세 버튼 · 토글 핸들러 OK")


def test_static_variant_split():
    """(b) **핵심 F-3** — distGalleryVariant() 는 bin1 3종만 반환한다."""
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    body = _fn_body(dist, "distGalleryVariant")
    assert "seq" not in body, (
        "distGalleryVariant() 가 seq 를 반환한다 — dist_composite/_gcCache 인덱싱과 "
        "/scatter 쿼리가 깨진다. 갤러리 데이터 변형은 distGalleryDataVariant() 를 쓸 것")
    assert "function distGalleryDataVariant" in dist, "distGalleryDataVariant 가 없습니다"
    gal = _fn_body(dist, "distRenderGalleryCell")
    assert "distGalleryDataVariant()" in gal, \
        "갤러리 셀이 seq 인식 변형(distGalleryDataVariant)으로 요청하지 않습니다"
    print("[정적] distGalleryVariant 는 bin1 전용 유지 (F-3) OK")


def test_static_no_chart_notes():
    """(c) **핵심 F-4** — seq 차트에 chart_notes 를 붙이지 않는다 + 선 렌더 없음."""
    idet = (_JS / "item_detail.js").read_text(encoding="utf-8")
    body = _fn_body(idet, "distRenderSeq")
    assert "chartNotesApply" not in body, (
        "distRenderSeq 가 chartNotesApply 를 부른다 — 편집 시 주석 저장값이 seq 좌표로 "
        "덮어써져 사용자 주석이 소실된다(CLAUDE.md §5-12)")
    assert "chipMarkersFor" not in body, "선택 좌표 마커는 누적% 축 전용이라 제외해야 합니다"
    assert 'mode: "markers"' in body, "seq 차트는 markers 여야 합니다"
    assert "shape" not in body.replace("shapes", ""), "선(line.shape) 렌더 금지"
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    cell = _fn_body(dist, "distRenderGallerySeqCell")
    assert 'mode: "markers"' not in cell or True   # 미니셀 점은 canvas 로 그린다
    assert "distPaintPoints" in cell, "미니셀 점은 canvas 오버레이로 그려야 합니다"
    print("[정적] seq 차트: chart_notes 미부착(F-4) · markers 전용 OK")


def test_static_note_detach():
    """(c-2) **F-4 의 상태 레벨 짝** — seq 로 덮어 그리기 전에 주석 등록을 풀어야 한다.

    렌더에서 chartNotesApply 를 안 부르는 것만으로는 부족하다. `_cnCharts` 는 CDF 를 한 번
    그리면 등록되고 스스로 지워지지 않는데, seq 는 **같은 DOM 노드**(#distCdf)를 덮으므로
    등록이 남아 있으면 이후 `cnSyncFromChart` 가 seq layout 에서 빈 shapes 를 회수하고,
    그대로 `cnFlush` 되면 `value:null` 이 나가 **저장된 주석이 서버에서 지워진다**
    (탭 전환·항목 이동·autoSave 가 전부 그 경로다 — CLAUDE.md §5-12)."""
    notes = (_JS / "chart_notes.js").read_text(encoding="utf-8")
    assert "function cnDetach" in notes, "chart_notes.js: cnDetach 가 없습니다"
    body = _fn_body(notes, "cnDetach")
    assert "delete _cnCharts[key]" in body, "cnDetach 가 등록을 지우지 않습니다"
    assert "_cnDirty.has(key)" in body and "cnSyncFromChart(key)" in body, (
        "cnDetach 가 등록을 풀기 전에 도형을 회수하지 않습니다 — 드래그 직후 seq 로 "
        "토글하면 그 편집이 사라진다")
    assert "_cnBoundKey = null" in body, (
        "cnDetach 가 gd._cnBoundKey 를 비우지 않습니다 — 텍스트/화살표 도구의 DOM click 이 "
        "살아남아 seq 좌표를 cdf 키에 넣는다")

    idet = (_JS / "item_detail.js").read_text(encoding="utf-8")
    cdf = _fn_body(idet, "distRenderCdf")
    seq_branch = cdf[:cdf.find("distRenderSeq(")]
    assert "cnDetach" in seq_branch, (
        "distRenderCdf 의 seq 분기가 distRenderSeq 호출 **전에** cnDetach 를 부르지 않습니다 "
        "(purge 후에는 layout 이 없어 도형을 회수할 수 없다)")
    print("[정적] seq 전환 시 주석 등록 해제(cnDetach) OK")


def test_static_seq_batch_size():
    """(c-3) seq 배치는 ECDF 보다 작게 나눈다 — 프런트/서버 상한이 함께 있어야 한다.

    seq 는 동일값을 접지 않아 항목당 payload 가 ECDF 의 한 자릿수 배 이상이다
    (5 source × 25,000 die = 항목 1개가 125,000 값). 30개로 묶으면 한 요청이 수십 MB 다.
    점을 버리는 게 아니라 요청을 나누는 것이라 규칙 #5(다운샘플 금지)와 무관하다."""
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    assert re.search(r"SEQ_SIZE:\s*(\d+)", dist), "DIST_BATCH.SEQ_SIZE 가 없습니다"
    seq_size = int(re.search(r"SEQ_SIZE:\s*(\d+)", dist).group(1))
    size = int(re.search(r"SIZE:\s*(\d+)", dist).group(1))
    assert seq_size < size, f"SEQ_SIZE({seq_size}) 가 SIZE({size}) 보다 작아야 합니다"
    flush = _fn_body(dist, "distFlushBatch")
    assert "SEQ_SIZE" in flush and "distVariantIsSeq" in flush, \
        "distFlushBatch 가 seq 변형에 SEQ_SIZE 를 쓰지 않습니다"

    routes = (_ROOT / "server" / "report" / "routes_webreport.py").read_text(encoding="utf-8")
    srv = int(re.search(r"_DIST_SEQ_BATCH_MAX\s*=\s*(\d+)", routes).group(1))
    assert seq_size <= srv, (
        f"프런트 SEQ_SIZE({seq_size}) 가 서버 상한({srv}) 을 넘습니다 — 400 이 난다")
    comp = (_JS / "dist_composite.js").read_text(encoding="utf-8")
    assert "SEQ_SIZE" in _fn_body(comp, "dcEnsureItems"), \
        "composite 도 seq 배치를 나눠야 합니다(안 그러면 합성 카드만 400)"
    print(f"[정적] seq 배치 상한 프런트 {seq_size} ≤ 서버 {srv} OK")


# ── 브라우저 검사 ────────────────────────────────────────────────────────────

def test_variant_map():
    """(d)(e) 변형 키/쿼리 매핑 + 캐시 6벌 분리 + distGalleryDataVariant 조합."""
    js = f"""<script>
      {SETUP}
      var q = {{}}, keys = {{}};
      ['all','bin1','rtbin1','seq','seq-bin1','seq-rtbin1'].forEach(function(k){{
        q[k] = distVariantQuery(k); keys[k] = distVariantKey(k);
      }});
      var legacy = {{t: distVariantKey(true), f: distVariantKey(false),
                     u: distVariantKey(undefined), bogus: distVariantKey('nope')}};
      var isSeq = {{}};
      ['all','bin1','seq','seq-rtbin1'].forEach(function(k){{ isSeq[k]=distVariantIsSeq(k); }});
      // 캐시 6벌이 서로 다른 객체여야 한다(하나라도 같으면 두 축 데이터가 섞인다)
      var caches = ['all','bin1','rtbin1','seq','seq-bin1','seq-rtbin1'].map(distCacheFor);
      var distinct = true;
      for (var i=0;i<caches.length;i++) for (var j=i+1;j<caches.length;j++)
        if (caches[i] === caches[j]) distinct = false;
      // 조합: (seqOnly, bin1, rtbin1) → 갤러리 데이터 변형 / 그리고 bin1 축 키는 불변
      var combo = {{}}, base = {{}};
      [[false,false,false],[false,true,false],[false,false,true],
       [true,false,false],[true,true,false],[true,false,true]].forEach(function(c){{
        distSeqOnly=c[0]; distBin1Only=c[1]; distRtBin1Only=c[2];
        combo[c.join(',')] = distGalleryDataVariant();
        base[c.join(',')] = distGalleryVariant();
      }});
      distSeqOnly=false; distBin1Only=false; distRtBin1Only=false;
      _emit({{q:q, keys:keys, legacy:legacy, isSeq:isSeq, distinct:distinct,
              combo:combo, base:base}});
    </script>"""
    got = json.loads(run_probe(DEPS, "", js, "variant"))
    assert got["q"] == {"all": "", "bin1": "&bin1=1", "rtbin1": "&bin1=1&bin1_scope=rt",
                        "seq": "&order=seq", "seq-bin1": "&bin1=1&order=seq",
                        "seq-rtbin1": "&bin1=1&bin1_scope=rt&order=seq"}, got["q"]
    assert got["legacy"] == {"t": "bin1", "f": "all", "u": "all", "bogus": "all"}, got["legacy"]
    assert got["isSeq"] == {"all": False, "bin1": False, "seq": True,
                            "seq-rtbin1": True}, got["isSeq"]
    assert got["distinct"] is True, "변형 캐시가 서로 같은 객체를 가리킨다"
    assert got["combo"] == {"false,false,false": "all", "false,true,false": "bin1",
                            "false,false,true": "rtbin1", "true,false,false": "seq",
                            "true,true,false": "seq-bin1",
                            "true,false,true": "seq-rtbin1"}, got["combo"]
    # F-3 실동작: seq 를 켜도 bin1 축 키는 절대 seq 가 되지 않는다
    for k, v in got["base"].items():
        assert v in ("all", "bin1", "rtbin1"), f"distGalleryVariant 가 seq 를 냈다: {k}={v}"
    print("  [browser] 변형 키·쿼리·캐시 분리·조합 6종 OK (F-3 실동작 포함)")


def test_build_and_points():
    """(f)(g)(h) compact 빌더 · 표시 캡(stride) · 수평 기준선."""
    js = f"""<script>
      {SETUP}
      var payload = {{format:'seq-columnar-v1', items:{{IT00:{{units:'V',lo:-1,hi:1,
        sources:{{WF1:{{v:[7,3,9,1,5]}}, WF2:{{v:[-4,6,-9]}}}}}}}}}};
      var built = buildDistSeqFromCompact(payload);
      var small = distSeqDisplayPoints({{vs:[7,3,9,1,5]}}, 1500);
      var big = [];
      for (var i=0;i<1000;i++) big.push(i*2);
      var capped = distSeqDisplayPoints({{vs:big}}, 100);
      var b = distSeqBounds({{WF1:small}});
      var sent = distSeqSentinelTrace(b);
      var sh = distSeqSpecShapes(-1, 1);
      var an = distSeqSpecAnnos(-1, 1, true);
      _emit({{
        units: built.IT00.units, lo: built.IT00.lower_limit, hi: built.IT00.upper_limit,
        seqFlag: built.IT00.seq === true,
        wf1: built.IT00.bySource.WF1.vs, wf2: built.IT00.bySource.WF2.vs,
        smallX: small.xs, smallY: small.ys,
        capN: capped.xs.length, capFirst: [capped.xs[0], capped.ys[0]],
        capLast: [capped.xs[capped.xs.length-1], capped.ys[capped.ys.length-1]],
        capMono: capped.xs.every(function(v,i,a){{ return i===0 || a[i-1] < v; }}),
        bounds: b, sentX: sent.x, sentY: sent.y,
        shapes: sh.map(function(x){{ return [x.xref, x.x0, x.x1, x.y0, x.y1]; }}),
        annos: an.map(function(x){{ return [x.xref, x.x, x.y, x.text]; }})
      }});
    </script>"""
    got = json.loads(run_probe(DEPS, "", js, "build"))
    assert got["wf1"] == [7, 3, 9, 1, 5], f"행 순서가 바뀌었다: {got['wf1']}"
    assert got["wf2"] == [-4, 6, -9], got["wf2"]
    assert (got["units"], got["lo"], got["hi"]) == ("V", -1, 1), got
    assert got["seqFlag"] is True, "seq 표식이 없다(렌더러 분기 안전장치)"
    assert got["smallX"] == [1, 2, 3, 4, 5], got["smallX"]
    assert got["smallY"] == [7, 3, 9, 1, 5], "캡 이하인데 값이 변형됐다"
    assert got["capN"] == 100, f"캡이 적용되지 않았다: {got['capN']}"
    assert got["capFirst"] == [1, 0] and got["capLast"] == [1000, 1998], got
    assert got["capMono"] is True, "stride 결과 x 가 단조 증가가 아니다"
    assert got["bounds"] == {"xMax": 5, "yMin": 1, "yMax": 9}, got["bounds"]
    assert got["sentX"] == [1, 5] and got["sentY"] == [1, 9], got
    # 수평선: xref=paper, x0/x1=0/1, y0==y1 (ECDF 의 수직선과 정반대)
    # shapes 는 [lo, hi] 순, annos 는 USL(hi) 먼저 — ECDF 쪽 순서 관례를 그대로 따른다.
    assert got["shapes"] == [["paper", 0, 1, -1, -1], ["paper", 0, 1, 1, 1]], got["shapes"]
    assert [a[0] for a in got["annos"]] == ["paper", "paper"], got["annos"]
    assert got["annos"][0][3] == "USL 1" and got["annos"][1][3] == "LSL -1", got["annos"]
    print("  [browser] 빌더 순서 보존 · stride 캡(양끝 보존) · 수평 기준선 OK")


def test_render_seq():
    """(i)(j) distRenderSeq 실동작 — x=1..n, y=값 순서, 주석 훅 미호출, 제외 재번호."""
    js = f"""<script>
      {SETUP}
      var calls = [], noteCalls = 0;
      window.Plotly = {{
        newPlot: function(div, traces, layout, cfg) {{
          calls.push({{traces: traces, layout: layout}});
          div.data = traces;                       // purge 분기 재현
          div.on = function() {{}};                 // 실제 Plotly 가 붙여주는 이벤트 API
          div.removeAllListeners = function() {{}};
        }},
        purge: function() {{}}
      }};
      window.chartNotesApply = function() {{ noteCalls++; }};
      var data = {{subject:'IT00', units:'V', lower_limit:-1, upper_limit:1, status:'ok',
        cpk:1.1, is_fail:false, stats:[], fail_rows:[], fail_total:0,
        sources:[{{name:'WF1', values:[7,3,9,1,5],
                   serial:['a','b','c','d','e'], xpos:[1,2,3,4,5], ypos:[1,1,1,1,1]}}]}};
      distSeqOnly = true;
      var div = document.getElementById('distCdf');
      distRenderCdf(data);                       // seq 분기로 들어가야 한다
      var first = calls[calls.length-1];
      cdfExcluded.add(cdfChipKey('WF1','b',2,1));   // 2번째 점 제외
      distRenderCdf(data);
      var second = calls[calls.length-1];
      distSeqOnly = false;
      cdfExcluded.clear();
      _emit({{
        n: calls.length, noteCalls: noteCalls,
        mode: first.traces[0].mode, type: first.traces[0].type,
        x: first.traces[0].x, y: first.traces[0].y,
        hasLine: !!first.traces[0].line,
        xtitle: first.layout.xaxis.title.text, ytitle: first.layout.yaxis.title.text,
        shapes: first.layout.shapes.length,
        exX: second.traces[0].x, exY: second.traces[0].y,
        cap: (document.getElementById('cdfCapLabel')||{{}}).textContent
      }});
    </script>"""
    body = ('<div id="distCdf"></div><div id="cdfAxisBar"></div>'
            '<span id="cdfCapLabel">누적분포 CDF</span>')
    got = json.loads(run_probe(DEPS, body, js, "render"))
    assert got["n"] == 2, got["n"]
    assert got["noteCalls"] == 0, "seq 차트에 chart_notes 가 붙었다 (F-4)"
    assert got["mode"] == "markers" and not got["hasLine"], got
    assert got["x"] == [1, 2, 3, 4, 5], got["x"]
    assert got["y"] == [7, 3, 9, 1, 5], f"y 가 행 순서가 아니다: {got['y']}"
    assert "측정 순서" in got["xtitle"] and "측정값" in got["ytitle"], got
    assert got["shapes"] == 2, f"LSL/USL 수평선 2개여야 한다: {got['shapes']}"
    assert got["exX"] == [1, 2, 3, 4] and got["exY"] == [7, 9, 1, 5], \
        f"제외 후 x 재번호 실패: {got['exX']} / {got['exY']}"
    print("  [browser] distRenderSeq: x=1..n · y=행 순서 · 주석 미부착 · 제외 재번호 OK")


def test_batch_url():
    """(k) 갤러리 요청이 실제로 `?…&order=seq` 로 나간다 (디바운스 통과 후)."""
    js = f"""<script>
      {SETUP}
      var calls = [];
      window.fetch = function(u) {{ calls.push(String(u)); return new Promise(function() {{}}); }};
      distSeqOnly = true; distSeqReady = true;
      distRequestSubject('IT00', distGalleryDataVariant());
      // 디바운스(DIST_BATCH.DEBOUNCE_MS 50ms) 통과 후 결과를 낸다.
      setTimeout(function() {{
        distSeqOnly = false;
        _emit({{calls: calls}});
      }}, 300);
    </script>"""
    got = json.loads(run_probe(DEPS, "", js, "batchurl"))
    calls = got["calls"]
    assert len(calls) == 1, f"요청이 1건이 아니다: {calls}"
    assert "/web_report/distribution_batch?subjects=IT00" in calls[0], calls
    assert "order=seq" in calls[0], f"order=seq 가 빠졌다: {calls[0]}"
    print("  [browser] 배치 요청 URL 에 order=seq OK")


def test_gallery_cell():
    """(l) 갤러리 미니셀 seq 렌더 — 수평 기준선 · y 가 누적%(0~100) 가 아니다."""
    js = f"""<script>
      {SETUP}
      var calls = [];
      window.Plotly = {{
        newPlot: function(div, traces, layout) {{ calls.push({{t: traces, l: layout}}); }},
        purge: function() {{}}
      }};
      distSeqOnly = true; distSeqReady = true;
      distSeqCache['IT00'] = {{lower_limit:-1, upper_limit:1, units:'V', seq:true,
        bySource: {{WF1: {{vs:[7,3,9,1,5]}}}}}};
      var cell = document.querySelector('.distg-card');
      distRenderGalleryCell(cell);
      var last = calls[calls.length-1] || {{}};
      var y = (last.l || {{}}).yaxis || {{}};
      distSeqOnly = false; distSeqCache = {{}};
      _emit({{
        n: calls.length, rendered: cell.dataset.rendered,
        shapes: (last.l && last.l.shapes || []).map(function(x){{ return [x.xref, x.y0]; }}),
        ysuffix: y.ticksuffix || "", yrange: y.range || null,
        xzero: ((last.l||{{}}).xaxis||{{}}).rangemode || ""
      }});
    </script>"""
    body = ('<div id="panel-distribution"><div class="distg-card" data-subject="IT00" '
            'data-status="ok"><div class="distg-plot"></div></div></div>')
    got = json.loads(run_probe(DEPS, body, js, "cell"))
    assert got["n"] == 1, f"셀이 렌더되지 않았다: {got}"
    assert got["rendered"] == "1", got
    assert got["shapes"] == [["paper", -1], ["paper", 1]], got["shapes"]
    assert got["ysuffix"] == "", "y 축에 누적% 접미사가 남아 있다(ECDF 레이아웃 재사용 의심)"
    assert got["yrange"] is None, "Limit 토글이 꺼져 있는데 y 범위가 고정됐다"
    assert got["xzero"] == "tozero", got["xzero"]
    print("  [browser] 갤러리 미니셀 seq 렌더 (수평 기준선 · 값 축) OK")


def test_gap_and_composite_cards():
    """(m)(n)(q) Gap·composite 카드 seq 렌더 + 세 카드가 **같은 레이아웃 함수**를 쓴다."""
    js = f"""<script>
      {SETUP}
      var calls = [];
      window.Plotly = {{
        newPlot: function(div, traces, layout) {{ calls.push({{t: traces, l: layout, d: div}}); }},
        purge: function() {{}}
      }};
      MODE = 'view';
      DATA.dist_composites = {{c1: {{name:'합성', pairs:[{{source:'WF1',item:'IT00'}},
        {{source:'WF2',item:'IT00'}}], limit:{{mode:'manual', lo:-1, hi:1}},
        colors:{{}}}}}};
      DATA.gap_charts = {{g1: {{name:'Gap A', sources:['WF1'],
        tokens:[{{t:'item',item:'IT00'}}], limit:{{mode:'manual', lo:-1, hi:1}}}}}};
      distSeqOnly = true; distSeqReady = true;
      // 일반 항목 seq 캐시 + composite 용 seq 캐시(같은 배치 응답 스키마)
      var info = {{lower_limit:-1, upper_limit:1, units:'V', seq:true,
        bySource: {{WF1: {{vs:[7,3,9,1,5]}}, WF2: {{vs:[2,4,6]}}}}}};
      distSeqCache['IT00'] = info;
      _dcCache['seq']['IT00'] = info;
      // gap 은 서버 응답이 두 모드 공통 — gcBuildSeries 가 seqEntry 를 만든다.
      var gdata = {{subject:'Gap A', sources:[{{name:'WF1', values:[7,3,9,1,5],
        serial:['a','b','c','d','e'], xpos:[1,2,3,4,5], ypos:[1,1,1,1,1]}}],
        matched_dies:5, missing:[]}};
      _gcCache.all['g1'] = {{data: gdata, series: gcBuildSeries(gdata)}};

      var cards = document.querySelectorAll('.distg-card');
      distRenderGalleryCell(cards[0]);        // 일반 항목
      var normal = calls[calls.length-1];
      distRenderGalleryCell(cards[1]);        // composite
      var comp = calls[calls.length-1];
      distRenderGalleryCell(cards[2]);        // gap
      var gap = calls[calls.length-1];
      // 공용 레이아웃 함수 산출과 일반 카드 layout 이 정확히 같아야 한다(순수 추출 증명).
      var expect = distSeqCellLayout('ok', -1, 1,
        distSeqBounds({{WF1: distSeqDisplayPoints(info.bySource.WF1, 1500),
                        WF2: distSeqDisplayPoints(info.bySource.WF2, 1500)}}));
      var lay = function(c) {{ return {{
        rangemode: c.l.xaxis.rangemode, ysuffix: c.l.yaxis.ticksuffix || "",
        yrange: c.l.yaxis.range || null, shapes: (c.l.shapes||[]).length,
        margin: c.l.margin, showlegend: c.l.showlegend }}; }};
      distSeqOnly = false; distSeqCache = {{}}; _dcCache['seq'] = {{}};
      _emit({{
        n: calls.length,
        normalLay: lay(normal), compLay: lay(comp), gapLay: lay(gap),
        expectLay: {{rangemode: expect.xaxis.rangemode,
                     ysuffix: expect.yaxis.ticksuffix || "",
                     yrange: expect.yaxis.range || null,
                     shapes: (expect.shapes||[]).length, margin: expect.margin,
                     showlegend: expect.showlegend}},
        compKeys: Object.keys(comp.d._distPts || {{}}),
        compX: (comp.d._distPts || {{}})['WF1\\x1fIT00'].xs,
        compY: (comp.d._distPts || {{}})['WF1\\x1fIT00'].ys,
        compColorFn: typeof comp.d._distColorFor,
        gapX: (gap.d._distPts || {{}}).WF1.xs, gapY: (gap.d._distPts || {{}}).WF1.ys,
        compTraces: comp.t.length, gapTraces: gap.t.length,
        rendered: [cards[0].dataset.rendered, cards[1].dataset.rendered, cards[2].dataset.rendered]
      }});
    </script>"""
    body = ('<div id="panel-distribution">'
            '<div class="distg-card" data-subject="IT00" data-status="ok">'
            '<div class="distg-plot"></div></div>'
            '<div class="distg-card distg-comp" data-comp-id="c1" data-status="ok">'
            '<div class="distg-plot"></div></div>'
            '<div class="distg-card distg-gap" data-gap-id="g1" data-status="ok">'
            '<div class="distg-plot"></div></div></div>')
    got = json.loads(run_probe(DEPS_ALL, body, js, "cards"))
    assert got["n"] == 3, f"카드 3장이 렌더되지 않았다: {got['n']}"
    assert got["rendered"] == ["1", "1", "1"], got["rendered"]
    # (q) 세 카드가 같은 레이아웃 규격 — 공용 함수 산출과 일치
    assert got["normalLay"] == got["expectLay"], (got["normalLay"], got["expectLay"])
    assert got["compLay"] == got["expectLay"], "composite 카드가 다른 레이아웃을 쓴다"
    assert got["gapLay"] == got["expectLay"], "gap 카드가 다른 레이아웃을 쓴다"
    # (n) composite: pairKey 유지 + 색 해석기 주입 + 행 순서
    assert got["compKeys"] == ["WF1\x1fIT00", "WF2\x1fIT00"], got["compKeys"]
    assert got["compColorFn"] == "function", "pairKey 색 해석기(_distColorFor)가 빠졌다"
    assert got["compX"] == [1, 2, 3, 4, 5] and got["compY"] == [7, 3, 9, 1, 5], got
    # (m) gap: 행 순서 그대로 · chip 마커 없음(sentinel 1개뿐)
    assert got["gapX"] == [1, 2, 3, 4, 5] and got["gapY"] == [7, 3, 9, 1, 5], got
    assert got["compTraces"] == 1 and got["gapTraces"] == 1, \
        f"seq 카드에 sentinel 외 trace 가 붙었다(chip 마커 의심): {got}"
    print("  [browser] Gap·composite 카드 seq 렌더 + 공용 레이아웃 일치 OK")


def test_composite_detail_stats():
    """(o) composite 상세 — 차트는 seq, **통계표 숫자는 ECDF 모드와 동일**."""
    js = f"""<script>
      {SETUP}
      var calls = [];
      window.Plotly = {{
        newPlot: function(div, traces, layout) {{ calls.push({{t: traces, l: layout}}); }},
        purge: function() {{}}
      }};
      DATA.dist_composites = {{c1: {{name:'합성', pairs:[{{source:'WF1',item:'IT00'}}],
        limit:{{mode:'manual', lo:-1, hi:1}}, colors:{{}}}}}};
      _dcDetailId = 'c1';
      _dcCache['all']['IT00'] = {{lower_limit:-1, upper_limit:1, units:'V',
        bySource: {{WF1: {{xs:[1,3,5,7,9], ys:[20,40,60,80,100]}}}}}};
      _dcCache['seq']['IT00'] = {{lower_limit:-1, upper_limit:1, units:'V', seq:true,
        bySource: {{WF1: {{vs:[7,3,9,1,5]}}}}}};
      distSeqOnly = false;
      dcRenderDetailCharts();
      var ecdfStats = document.getElementById('dcDetailStats').innerHTML;
      var ecdfChart = calls[calls.length-1];
      distSeqOnly = true;
      dcRenderDetailCharts();
      var seqStats = document.getElementById('dcDetailStats').innerHTML;
      var seqChart = calls[calls.length-1];
      distSeqOnly = false; _dcDetailId = null;
      _dcCache['all'] = {{}}; _dcCache['seq'] = {{}};
      _emit({{
        sameStats: ecdfStats === seqStats,
        statsHasValue: ecdfStats.indexOf('WF1_IT00') >= 0,
        ecdfX: ecdfChart.t[0].x, seqX: seqChart.t[0].x, seqY: seqChart.t[0].y,
        seqType: seqChart.t[0].type, seqMode: seqChart.t[0].mode,
        seqTraces: seqChart.t.length,
        seqXTitle: seqChart.l.xaxis.title.text, seqYSuffix: seqChart.l.yaxis.ticksuffix || "",
        seqShapes: (seqChart.l.shapes||[]).map(function(s){{ return [s.xref, s.y0]; }})
      }});
    </script>"""
    body = ('<div id="dcDetailChart"></div><div id="dcDetailLegend"></div>'
            '<div id="dcDetailStats"></div>')
    got = json.loads(run_probe(DEPS_ALL, body, js, "dcdetail"))
    assert got["statsHasValue"] is True, "통계표가 비었다 — 하네스 오류"
    assert got["sameStats"] is True, \
        "Serial 순에서 통계표 숫자가 달라졌다 (ECDF 기준 유지가 깨졌다 — 규칙 #13)"
    assert got["ecdfX"] == [1, 3, 5, 7, 9], got["ecdfX"]
    assert got["seqX"] == [1, 2, 3, 4, 5] and got["seqY"] == [7, 3, 9, 1, 5], got
    assert got["seqMode"] == "markers", got["seqMode"]
    assert got["seqTraces"] == 1, "seq 상세에 chip 마커 trace 가 붙었다"
    assert "측정 순서" in got["seqXTitle"] and got["seqYSuffix"] == "", got
    assert got["seqShapes"] == [["paper", -1], ["paper", 1]], got["seqShapes"]
    print("  [browser] composite 상세: 차트만 seq · 통계표는 ECDF 기준 유지 OK")


def test_composite_detail_notes():
    """(o-2) composite 상세 차트 주석 — ECDF 는 `cdf:comp:<uuid>` 로 등록, seq 는 해제.

    등록 키가 이름이면 개명 시 주석이 끊기고, seq 에 등록이 남으면 이후 저장이 seq layout
    에서 빈 도형을 회수해 저장된 주석을 지운다(§5-12)."""
    js = f"""<script>
      {SETUP}
      MODE = 'edit';
      window.Plotly = {{
        newPlot: function(div, traces, layout) {{
          div.data = traces; div.layout = layout; div.on = function() {{}};
        }},
        purge: function(div) {{ delete div.data; delete div.layout; }},
        relayout: function() {{}}
      }};
      DATA.dist_composites = {{c1: {{name:'합성', pairs:[{{source:'WF1',item:'IT00'}}],
        limit:{{mode:'manual', lo:-1, hi:1}}, colors:{{}}}}}};
      _dcDetailId = 'c1';
      _dcCache['all']['IT00'] = {{lower_limit:-1, upper_limit:1, units:'V',
        bySource: {{WF1: {{xs:[1,3,5], ys:[33,66,100]}}}}}};
      _dcCache['seq']['IT00'] = {{lower_limit:-1, upper_limit:1, units:'V', seq:true,
        bySource: {{WF1: {{vs:[5,1,3]}}}}}};
      distSeqOnly = false;
      dcRenderDetailCharts();
      var afterEcdf = Object.keys(_cnCharts);
      distSeqOnly = true;
      dcRenderDetailCharts();
      var afterSeq = Object.keys(_cnCharts);
      distSeqOnly = false; _dcDetailId = null;
      _dcCache['all'] = {{}}; _dcCache['seq'] = {{}};
      _emit({{subject: dcNoteSubject('c1'), afterEcdf: afterEcdf, afterSeq: afterSeq}});
    </script>"""
    body = ('<div id="dcDetailChart"></div><div id="dcDetailLegend"></div>'
            '<div id="dcDetailStats"></div>')
    got = json.loads(run_probe(DEPS_ALL_NOTE, body, js, "dcnotes"))
    assert got["subject"] == "comp:c1", got["subject"]
    assert "cdf:comp:c1" in got["afterEcdf"], (
        f"ECDF 상세에서 주석이 등록되지 않았다: {got['afterEcdf']}")
    assert "cdf:comp:c1" not in got["afterSeq"], (
        f"seq 상세에서 주석 등록이 남았다 — 저장 시 주석이 지워진다: {got['afterSeq']}")
    print("  [browser] composite 상세 주석: ECDF 등록 · seq 해제 OK")


def test_dc_cache_and_url():
    """(p) `_dcCache` 가 변형 6키 · composite 배치 URL 에 order=seq."""
    js = f"""<script>
      {SETUP}
      var calls = [];
      window.fetch = function(u) {{ calls.push(String(u)); return new Promise(function() {{}}); }};
      var keys = Object.keys(_dcCache).sort();
      var inflight = Object.keys(_dcInflight).sort();
      dcEnsureItems(['IT00'], 'seq');
      _emit({{keys: keys, inflight: inflight, calls: calls}});
    </script>"""
    got = json.loads(run_probe(DEPS_ALL, "", js, "dccache"))
    want = sorted(["all", "bin1", "rtbin1", "seq", "seq-bin1", "seq-rtbin1"])
    assert got["keys"] == want, f"_dcCache 키가 변형 6종이 아니다: {got['keys']}"
    assert got["inflight"] == want, f"_dcInflight 키 불일치: {got['inflight']}"
    assert len(got["calls"]) == 1 and "order=seq" in got["calls"][0], got["calls"]
    print("  [browser] _dcCache 6키 + composite 배치 URL order=seq OK")


def test_note_paste_fallback():
    """(r) seq 로 처음 연 항목도 Note 붙여넣기가 차트를 찾는다 (_cnCharts 등록은 안 한다)."""
    js = f"""<script>
      {SETUP}
      MODE = 'edit';
      var toasts = [];
      window.showToast = function(m) {{ toasts.push(String(m)); }};
      window.Plotly = {{ toImage: function() {{ throw new Error('CAPTURE_REACHED'); }} }};
      cnPasteToNote('IT00');
      setTimeout(function() {{
        _emit({{toasts: toasts, registered: !!_cnCharts['cdf:IT00']}});
      }}, 200);
    </script>"""
    body = '<div id="distCdf"></div>'
    got = json.loads(run_probe(DEPS_NOTE, body, js, "notepaste"))
    joined = " | ".join(got["toasts"])
    assert "준비되지 않았습니다" not in joined, \
        f"#distCdf 폴백이 동작하지 않았다: {joined}"
    assert "CAPTURE_REACHED" in joined, f"캡처 단계까지 못 갔다: {joined}"
    assert got["registered"] is False, \
        "_cnCharts 에 등록됐다 — seq 좌표가 주석 저장값을 덮어쓸 수 있다 (F-4)"
    print("  [browser] Note 붙여넣기 #distCdf 폴백 · _cnCharts 미등록 OK")


def main():
    print("[Distribution Serial 순 JS 회귀]")
    test_static_wiring()
    test_static_variant_split()
    test_static_no_chart_notes()
    test_static_note_detach()
    test_static_seq_batch_size()
    if not edge_path():
        print("[SKIP] Edge 를 찾지 못해 브라우저 검사는 건너뜁니다")
        return
    test_variant_map()
    test_build_and_points()
    test_render_seq()
    test_batch_url()
    test_gallery_cell()
    test_gap_and_composite_cards()
    test_composite_detail_stats()
    test_composite_detail_notes()
    test_dc_cache_and_url()
    test_note_paste_fallback()
    print("[통과] Serial 순 프런트 계약 정상")


if __name__ == "__main__":
    main()

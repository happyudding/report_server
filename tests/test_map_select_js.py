r"""Item_detail 드래그 → 좌표 강조(chipsel) 회귀 — headless Edge (2026-09-03).

실행:
    server\.venv\Scripts\python.exe tests\test_map_select_js.py

**왜 파이썬 테스트로 안 되나**: 이 기능이 깨지는 방식은 전부 조용하다 —
  · mapSelMarkerTraces 를 hit 당 trace 로 되돌리면 카드 canvas(distDrawPoints)가
    **첫 점만** 그린다. 에러 없이 "드래그로 여러 개를 골랐는데 하나만 강조"가 된다.
  · chipsel 모드가 제외(cdfExcluded) 경로와 섞이면 강조하려던 die 가 곡선에서 **사라진다**.
  · 항목을 옮길 때 강조가 초기화되면(cdfResetEdits 가 mapSelChips 를 건드리면)
    "전 Item detail 에서 강조" 라는 요구 자체가 무너진다.
  · 상한(MAPSEL_MAX)이 프런트에 없으면 서버가 400 을 내며 드래그가 통째로 실패한다.

검증하는 것:
  (a) mapSelMarkerTraces 는 **차트당 trace 1개**(점 N개) · 크로스헤어는 chip 1개일 때만
  (b) chipsel 모드 드래그 박스 → 요청 1회(중복 제거) → mapSelChips 증가 → 옥색
  (c) 항목 이동(openItemDetail) 후에도 강조 유지
  (d) 점 클릭 토글(추가 → 제거)
  (e) 상한 초과분은 잘라내고 cut 으로 알린다
  (f) 정적 배선 — canvas 다점 루프 / Seq 오버레이 / seq 카드에는 마커 없음 /
      강조 색 세그먼트가 Map·Item_detail 양쪽에 있음
  (g) 단색 ↔ 색 구분 전환 — 기존 chip 재배색 · 테두리 동반 · 10색 순환 · 같은 모드 no-op

Edge 가 없으면 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_dist_composite_js import edge_path, run_probe   # noqa: E402

_JS = Path(__file__).resolve().parent.parent / "server" / "report" / "static" / "webreport"

# map_select.js 는 core.js(esc/showToast) 위에서 돌고, item_detail.js 의 chipsel 경로가
# distribution.js(distActiveColorFor 등)와 map_select 를 함께 쓴다.
DEPS = ["core.js", "distribution.js", "map_select.js", "chart_notes.js", "item_detail.js"]

# #distCdf 는 distRenderCdf 가 찾는 노드, #toast 는 core.js showToast 가 쓰는 노드다
# (둘 중 하나만 빠져도 하네스가 통째로 죽어 무엇이 깨졌는지 안 보인다).
BODY = """
<style>.panel{display:none}.panel.active{display:block}</style>
<div id="toast"></div>
<div class="content">
  <div id="panel-distribution" class="panel active"></div>
  <div id="panel-map-analysis" class="panel"></div>
  <div id="panel-item-detail" class="panel">
    <div id="cdfEditBar"></div>
    <div id="distCdf"></div>
    <div id="distHist"></div>
    <div id="cdfAxisBar"></div>
    <div id="idetChipVals"></div>
  </div>
</div>
"""

# Plotly 스텁: newPlot 이 받은 traces/layout 을 노드에 남기고, on() 으로 잡은 핸들러를
# 하네스가 직접 발화할 수 있게 보관한다. fetch 는 배치 라우트 응답을 흉내낸다.
SETUP = r"""
window.__plots = {};
window.__fetchCalls = [];
window.Plotly = {
  purge: function(d){ if (d) { d.data = null; d._handlers = null; } },
  react: function(){}, moveTraces: function(){}, restyle: function(){},
  newPlot: function(div, traces, layout){
    div.data = traces; div.layout = layout;
    div._handlers = {};
    div.on = function(name, fn){ div._handlers[name] = fn; };
    div.removeAllListeners = function(name){ if (div._handlers) delete div._handlers[name]; };
    window.__plots[div.id] = div;
    return Promise.resolve(div);
  }
};
webglOk = function(){ return false; };          // SVG 분기로 고정(useGl 판정 단순화)
renderMapAnalysis = function(){ window.__mapRendered = (window.__mapRendered || 0) + 1; };
// edit_mode.js(DEPS 밖)의 탭 dirty 맵 — mapSelRefreshMap 이 "안 보이면 예약" 경로에서 쓴다.
var tabDirty = {};
distQueueRender = function(){};
ensureDistData = function(){};
beforeLimitShapes = function(){ return []; };
beforeLimitAnnos = function(){ return []; };

// 서버 batch 응답 스텁 — 요청한 chip 을 그대로 되돌려 주되, serial 이 "MISS" 면 null.
window.fetch = function(url, opt){
  var body = JSON.parse((opt && opt.body) || '{"chips":[]}');
  window.__fetchCalls.push({ url: String(url), chips: body.chips });
  var items = ["ITEM_A", "ITEM_B"];
  var out = body.chips.map(function(c, i){
    if (String(c.serial).indexOf("MISS") === 0) return null;
    return { chip: { source: c.source, serial: c.serial, xpos: c.xpos, ypos: c.ypos,
                     x: Number(c.xpos), y: Number(c.ypos), shot: "0", dut: "0", bin: "1" },
             items_ref: 0, value: [1.5 + i, 2.5 + i], cum_pct: [30 + i, 60 + i] };
  });
  return Promise.resolve({ ok: true, status: 200,
    json: function(){ return Promise.resolve({ item_lists: [items], chips: out }); } });
};

// /scatter 응답 형태 — 점마다 serial/xpos/ypos 가 값과 같은 순서로 실린다.
window.__data = {
  subject: "ITEM_A", test_num: "1000", units: "V",
  lower_limit: -10, upper_limit: 10, cpk: 1.5, status: "ok", is_fail: false,
  stats: [{ source: "WF1", n: 3, min: 1, median: 2, max: 3,
            average: 2, stdev: 1, cp: 1, cpl: 1, cpu: 1, cpk: 1 }],
  sources: [{ name: "WF1", values: [1, 2, 3],
              serial: ["S0", "S1", "S2"], xpos: ["1", "2", "3"], ypos: ["1", "1", "1"] }],
  fail_rows: [], fail_total: 0, fail_truncated: false
};
function _pt(i){   // trace 위 i 번째 점을 Plotly 이벤트 모양으로
  var s = window.__data.sources[0];
  return { data: { name: s.name }, customdata: [s.serial[i], s.xpos[i], s.ypos[i]] };
}
function _fire(divId, evName, points){
  var d = window.__plots[divId];
  if (!d || !d._handlers || !d._handlers[evName]) return "no-handler:" + evName;
  d._handlers[evName]({ points: points });
  return "fired";
}
function _sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
"""


def probe(harness: str, name: str):
    return json.loads(run_probe(DEPS, BODY, f"<script>{SETUP}{harness}</script>", name))


def test_marker_traces_single():
    """(a) hit N개 → trace 1개 · 크로스헤어는 chip 1개일 때만."""
    res = probe("""
      try {
        var out = {};
        mapSelChips = [
          { key: "a", color: MAPSEL_HL_COLOR, source: "WF1", xpos: "1", ypos: "1",
            items: { ITEM_A: { value: 1, cum_pct: 10 } } },
          { key: "b", color: MAPSEL_HL_COLOR, source: "WF1", xpos: "2", ypos: "1",
            items: { ITEM_A: { value: 2, cum_pct: 20 } } },
          { key: "c", color: MAPSEL_HL_COLOR, source: "WF1", xpos: "3", ypos: "1",
            items: { ITEM_A: { value: 3, cum_pct: 30 } } }];
        var m = chipMarkersFor("ITEM_A", false);
        out.traceCount = m.traces.length;
        out.pointCount = m.traces[0].x.length;
        out.colorIsArray = Array.isArray(m.traces[0].marker.color);
        out.color0 = m.traces[0].marker.color[0];
        out.size = m.traces[0].marker.size;
        out.shapesMulti = m.shapes.length;
        // 카드 크기 옵션
        out.cardSize = chipMarkersFor("ITEM_A", false, { size: MAPSEL_CARD_MARKER_SIZE }).traces[0].marker.size;
        // chip 1개면 크로스헤어 2줄
        mapSelChips = [mapSelChips[0]];
        out.shapesOne = chipMarkersFor("ITEM_A", false).shapes.length;
        out.detailSize = MAPSEL_MARKER_SIZE;
        _emit(out);
      } catch (e) { _emit({ error: String((e && e.stack) || e) }); }
    """, "mapsel_marker")
    assert "error" not in res, res
    assert res["traceCount"] == 1, f"(a) 차트당 trace 1개여야 한다: {res}"
    assert res["pointCount"] == 3, f"(a) 점 3개가 한 trace 에 들어가야 한다: {res}"
    assert res["colorIsArray"] is True, f"(a) 색은 점별 배열: {res}"
    assert res["color0"] == "#2DD4BF", f"(a) 옥색이 아니다: {res}"
    assert res["size"] == res["detailSize"] == 10, f"(a) 상세 마커 크기 10: {res}"
    assert res["cardSize"] == 9, f"(a) 카드 마커 크기 9: {res}"
    assert res["shapesMulti"] == 0, f"(a) 여러 개 선택 시 크로스헤어 없음: {res}"
    assert res["shapesOne"] == 2, f"(a) chip 1개면 크로스헤어 2줄: {res}"
    print("  [OK] 마커는 차트당 trace 1개 · 옥색 · 상세 10 / 카드 9 · 크로스헤어는 1개일 때만")


def test_chipsel_drag():
    """(b)(c) 드래그 박스 → 요청 1회 → 강조 추가 → 항목 이동 후에도 유지."""
    res = probe("""
      (async function(){
       try {
        var out = {};
        cdfEditMode = "chipsel";
        _itemDetailData = window.__data;
        distRenderCdf(window.__data);
        out.dragmode = window.__plots.distCdf.layout.dragmode;
        // 같은 점을 두 번 포함시켜 중복 제거를 확인한다
        out.fired = _fire("distCdf", "plotly_selected", [_pt(0), _pt(1), _pt(0)]);
        await _sleep(120);
        out.calls = window.__fetchCalls.length;
        out.sentChips = window.__fetchCalls[0].chips.length;
        out.url = window.__fetchCalls[0].url.indexOf("chips_lookup") >= 0;
        out.chips = mapSelChips.length;
        out.color = mapSelChips[0].color;
        out.hasItems = !!(mapSelChips[0].items && mapSelChips[0].items.ITEM_A);
        // (c) 다른 항목으로 이동해도 강조는 남는다 (cdfResetEdits 는 제외만 지운다)
        cdfExcluded.add("dummy");
        cdfResetEdits();
        out.afterReset = mapSelChips.length;
        out.excludedAfterReset = cdfExcluded.size;
        out.modeAfterReset = cdfEditMode;
        _emit(out);
       } catch (e) { _emit({ error: String((e && e.stack) || e) }); }
      })();
    """, "mapsel_drag")
    assert "error" not in res, res
    assert res["dragmode"] == "select", f"(b) chipsel 모드는 드래그가 박스 선택: {res}"
    assert res["fired"] == "fired", f"(b) plotly_selected 핸들러가 없다: {res}"
    assert res["calls"] == 1, f"(b) 요청은 1회여야 한다: {res}"
    assert res["sentChips"] == 2, f"(b) 중복 점이 제거되지 않았다: {res}"
    assert res["url"] is True, f"(b) 배치 라우트를 부르지 않았다: {res}"
    assert res["chips"] == 2, f"(b) 강조 좌표가 2개여야 한다: {res}"
    assert res["color"] == "#2DD4BF", f"(b) 옥색이 아니다: {res}"
    assert res["hasItems"] is True, f"(b) 항목 값 맵이 없다: {res}"
    assert res["afterReset"] == 2, f"(c) 항목 이동에서 강조가 초기화됐다: {res}"
    assert res["excludedAfterReset"] == 0 and res["modeAfterReset"] == "none", res
    print("  [OK] 드래그 → 요청 1회(중복 제거) → 옥색 강조 2개 · 항목 이동에도 유지")


def test_click_toggle_and_cap():
    """(d) 클릭 토글 · (e) 상한 초과분 cut."""
    res = probe("""
      (async function(){
       try {
        var out = {};
        cdfEditMode = "chipsel";
        _itemDetailData = window.__data;
        distRenderCdf(window.__data);
        _fire("distCdf", "plotly_click", [_pt(0)]);
        await _sleep(120);
        out.added = mapSelChips.length;
        distRenderCdf(window.__data);                 // 재렌더(핸들러 재바인딩)
        _fire("distCdf", "plotly_click", [_pt(0)]);   // 같은 점 → 해제
        await _sleep(120);
        out.toggled = mapSelChips.length;
        out.max = MAPSEL_MAX;
        // 못 찾은 chip → missing (상한을 채우기 **전에** 확인한다 — 꽉 찬 뒤에는
        // 요청 자체가 안 나가 missing 이 0 이 된다)
        var rm = await mapSelAddChips([{ source: "WF1", serial: "MISS-1", xpos: "77", ypos: "77" }]);
        out.missing = rm.missing;
        out.afterMiss = mapSelChips.length;
        // (e) 상한 초과 — MAPSEL_MAX + 5 개를 한 번에
        var many = [];
        for (var i = 0; i < MAPSEL_MAX + 5; i++) {
          many.push({ source: "WF1", serial: "X" + i, xpos: String(i), ypos: "9" });
        }
        var r = await mapSelAddChips(many);
        out.cut = r.cut;
        out.total = mapSelChips.length;
        out.sentLast = window.__fetchCalls[window.__fetchCalls.length - 1].chips.length;
        // 꽉 찬 뒤 더 넣으면 요청 없이 cut 만 돌아온다
        var callsBefore = window.__fetchCalls.length;
        var r3 = await mapSelAddChips([{ source: "WF1", serial: "Z", xpos: "5", ypos: "5" }]);
        out.fullCut = r3.cut;
        out.noExtraCall = window.__fetchCalls.length === callsBefore;
        _emit(out);
       } catch (e) { _emit({ error: String((e && e.stack) || e) }); }
      })();
    """, "mapsel_toggle")
    assert "error" not in res, res
    assert res["added"] == 1, f"(d) 클릭으로 추가되지 않았다: {res}"
    assert res["toggled"] == 0, f"(d) 같은 점 재클릭이 해제하지 않았다: {res}"
    assert res["missing"] == 1, f"(e) 못 찾은 chip 은 missing 으로: {res}"
    assert res["afterMiss"] == 0, f"(e) 못 찾은 chip 이 추가되면 안 된다: {res}"
    assert res["cut"] == 5, f"(e) 상한 초과분 5개를 잘라내야 한다: {res}"
    assert res["total"] == res["max"], f"(e) 총 chip 이 상한과 같아야 한다: {res}"
    assert res["sentLast"] == res["max"], f"(e) 상한 개수만 보내야 한다: {res}"
    assert res["fullCut"] == 1 and res["noExtraCall"] is True, \
        f"(e) 꽉 찬 뒤에는 요청 없이 cut 만 돌려야 한다: {res}"
    print(f"  [OK] 클릭 토글 · 미발견 missing · 상한 {res['max']}에서 cut 5 · 초과 시 무요청")


def test_color_mode_toggle():
    """(g) 단색 ↔ 색 구분 전환 (2026-09-04).

    깨지는 방식이 조용하다 — 색 배정은 mapSelColorAt 한 곳이지만 **이미 배정된 chip**
    을 다시 칠하지 않으면(mapSelReassignColors 누락) 버튼만 눌리고 화면은 그대로다.
    테두리를 MAPSEL_HL_LINE 고정으로 되돌리면 색 구분 모드에서 마커 윤곽이 전부 같은
    진한 청록이 되어 애써 나눈 색이 도로 비슷해 보인다.
    """
    res = probe("""
      try {
        var out = {};
        mapSelChips = [
          { key: "a", source: "WF1", xpos: "1", ypos: "1",
            items: { ITEM_A: { value: 1, cum_pct: 10 } } },
          { key: "b", source: "WF1", xpos: "2", ypos: "1",
            items: { ITEM_A: { value: 2, cum_pct: 20 } } }];
        mapSelReassignColors();
        out.monoColors = mapSelChips.map(function(c){ return c.color; });
        out.monoLine = chipMarkersFor("ITEM_A", false).traces[0].marker.line.color;

        // 색 구분으로 전환 — 이미 있던 chip 이 **다시 칠해져야** 한다.
        // Map 패널은 지금 숨어 있으므로(Item_detail 에서 누른 상황) 즉시 재렌더가 아니라
        // dirty 예약으로 가야 한다 — 안 보이는 canvas 를 소스 수만큼 다시 그리지 않는다.
        delete tabDirty["map-analysis"];
        mapSelSetPalette(true);
        out.mode = mapSelPaletteMode;
        out.paletteColors = mapSelChips.map(function(c){ return c.color; });
        out.paletteLine = chipMarkersFor("ITEM_A", false).traces[0].marker.line.color;
        out.hiddenDirty = tabDirty["map-analysis"] === true;
        out.hiddenNoRedraw = !window.__mapRendered;
        // Map 탭이 보일 때는 그 자리에서 다시 그린다
        document.getElementById("panel-map-analysis").classList.add("active");
        mapSelSetPalette(false);
        out.visibleRedrawn = window.__mapRendered > 0;
        mapSelSetPalette(true);

        // 11번째는 순환(사용자 확정: 10색 순환 유지)
        out.cycle = mapSelColorAt(0) === mapSelColorAt(MAPSEL_PALETTE.length);

        // 단색으로 되돌리면 원래 옥색·고정 테두리
        mapSelSetPalette(false);
        out.backColors = mapSelChips.map(function(c){ return c.color; });
        out.backLine = chipMarkersFor("ITEM_A", false).traces[0].marker.line.color;

        // 같은 모드로 다시 부르면 no-op (불필요한 전체 재렌더 방지)
        var before = window.__mapRendered;
        mapSelSetPalette(false);
        out.noop = (window.__mapRendered === before);

        // 세그먼트 HTML 은 두 화면 공용 — 현재 모드가 active 로 표시된다
        var box = document.createElement("div");
        box.innerHTML = mapSelColorSegHtml();
        out.segCount = box.querySelectorAll("[data-mapsel-palette]").length;
        out.activeIsMono = box.querySelector(".active").dataset.mapselPalette === "0";
        _emit(out);
      } catch (e) { _emit({ error: String((e && e.stack) || e) }); }
    """, "mapsel_colormode")
    assert "error" not in res, res
    assert res["monoColors"] == ["#2DD4BF", "#2DD4BF"], f"(g) 기본은 전 chip 단색: {res}"
    assert res["monoLine"] == ["#0F766E", "#0F766E"], f"(g) 단색 테두리는 고정 청록: {res}"
    assert res["mode"] is True, f"(g) 모드가 바뀌지 않았다: {res}"
    assert len(set(res["paletteColors"])) == 2, \
        f"(g) 색 구분인데 기존 chip 이 다시 칠해지지 않았다: {res}"
    assert res["paletteColors"][0] == "#e11d48", f"(g) 팔레트 첫 색: {res}"
    assert len(set(res["paletteLine"])) == 2 and "#0F766E" not in res["paletteLine"], \
        f"(g) 색 구분 테두리는 chip 색을 어둡게 한 것이어야 한다: {res}"
    assert res["hiddenDirty"] is True and res["hiddenNoRedraw"] is True, \
        f"(g) Map 이 숨어 있으면 dirty 예약만 해야 한다(안 보이는 canvas 재렌더 금지): {res}"
    assert res["visibleRedrawn"] is True, f"(g) Map 이 보이면 즉시 다시 그려야 한다: {res}"
    assert res["cycle"] is True, f"(g) 11번째는 첫 색으로 순환해야 한다: {res}"
    assert res["backColors"] == ["#2DD4BF", "#2DD4BF"], f"(g) 단색 복귀 실패: {res}"
    assert res["backLine"] == ["#0F766E", "#0F766E"], f"(g) 단색 복귀 시 테두리: {res}"
    assert res["noop"] is True, f"(g) 같은 모드 재클릭은 재렌더하지 않아야 한다: {res}"
    assert res["segCount"] == 2 and res["activeIsMono"] is True, f"(g) 세그먼트 마크업: {res}"
    print("  [OK] 단색↔색 구분 전환 · 기존 chip 재배색 · 테두리 동반 · 순환 · no-op")


def test_static_wiring():
    """(f) 되돌리면 조용히 깨지는 배선을 소스에서 직접 확인."""
    dist = (_JS / "distribution.js").read_text(encoding="utf-8")
    idet = (_JS / "item_detail.js").read_text(encoding="utf-8")
    msel = (_JS / "map_select.js").read_text(encoding="utf-8")
    wafer = (_JS / "wafer_charts.js").read_text(encoding="utf-8")

    # canvas 가 extra trace 안의 모든 점을 돈다 (첫 점만 그리면 강조가 하나만 보인다)
    extra = dist.split("plot._distExtra || []")[1][:600]
    assert "t.x.length" in extra and "t.x[i]" in extra, \
        "distDrawPoints 가 extra trace 의 모든 점을 그리지 않습니다"
    assert "Array.isArray(m.color)" in extra, "점별 색 배열을 처리하지 않습니다"

    # Seq 상세는 전용 오버레이를 쓴다(누적% 마커를 옮겨 그리지 않는다)
    seq = idet.split("function distRenderSeq(")[1].split("\nfunction ")[0]
    assert "idetSeqHighlightTrace" in seq, "Serial 순 상세에 강조 오버레이가 없습니다"
    assert "chipMarkersFor" not in seq, \
        "Serial 순 축에 (값,누적%) 마커를 그리면 안 됩니다 (좌표 의미가 다름)"

    # 갤러리 seq 카드는 좌표가 없어 강조 대상이 아니다
    gseq = dist.split("function distRenderGallerySeqCell(")[1].split("\nfunction ")[0]
    assert "chipMarkersFor" not in gseq, "seq 갤러리 카드에는 마커를 그리지 않습니다"

    # 상한은 서버(routes_webreport _COMMONALITY_LOOKUP_MAX)와 짝
    routes = (Path(__file__).resolve().parent.parent / "server" / "report"
              / "routes_webreport.py").read_text(encoding="utf-8")
    srv = int(routes.split("_COMMONALITY_LOOKUP_MAX = ")[1].split("\n")[0])
    cli = int(msel.split("const MAPSEL_MAX = ")[1].split(";")[0])
    assert srv == cli, f"상한 불일치: 서버 {srv} / 프런트 {cli} (한쪽만 바꾸면 400)"

    # Map 상세도 chip 전체를 trace 1개로
    det = wafer.split("function mapDetailTraces(")[1].split("\nfunction ")[0]
    assert "sx.push" in det and "if (sx.length)" in det, \
        "Map 상세가 chip 마다 trace 를 만들고 있습니다"

    # Map 재렌더는 보일 때만
    assert "function mapSelRefreshMap" in msel and 'tabDirty["map-analysis"]' in msel, \
        "Map 지연 재렌더(mapSelRefreshMap)가 없습니다"

    # 강조 색 모드 세그먼트는 **두 화면 모두**에 있어야 한다 — 한쪽이 빠지면 그 탭에서
    # 좌표를 고르는 사용자는 색을 바꿀 방법이 없다(에러 없이 "버튼이 없다").
    assert "mapSelColorSegHtml()" in wafer, \
        "Map Analysis 툴바에 강조 색 세그먼트가 없습니다"
    assert "mapSelColorSegHtml()" in idet, \
        "Item_detail 편집바에 강조 색 세그먼트가 없습니다"
    assert "bindMapSelColorSeg(panel)" in wafer, \
        "Map Analysis 세그먼트 클릭 바인딩이 없습니다"
    assert "data-mapsel-palette" in idet, \
        "Item_detail 위임 핸들러가 세그먼트를 받지 않습니다"
    # 테두리는 두 마커 생성부 모두 헬퍼를 거친다(고정 상수로 되돌리면 색 구분이 흐려진다)
    assert "mapSelLineColorFor" in msel and "mapSelLineColorFor" in idet, \
        "마커 테두리가 mapSelLineColorFor 를 거치지 않습니다"
    print(f"  [OK] canvas 다점 · seq 오버레이 · seq 카드 제외 · 상한 짝({srv}) · Map trace 1개"
          " · 색 세그먼트 양쪽")


def main():
    fails = 0
    try:
        test_static_wiring()
    except AssertionError as e:
        fails += 1
        print(f"  [FAIL] test_static_wiring: {e}")
    if not edge_path():
        print("SKIP: Edge 를 찾지 못했습니다 (동작 검증 불가 — 정적 검사만 수행)")
        return 1 if fails else 0
    for fn in (test_marker_traces_single, test_chipsel_drag, test_click_toggle_and_cap,
               test_color_mode_toggle):
        try:
            fn()
        except AssertionError as e:
            fails += 1
            print(f"  [FAIL] {fn.__name__}: {e}")
    print("ALL PASS" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

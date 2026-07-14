// ── web_report: wafer map + fail-bin 차트 (Plotly) ─────────────────────────
// Pass 는 초록 고정, Fail 은 등장 순서대로 팔레트(dataviz 검증 팔레트에서 초록 제외).
// 색이 넘치면 순환하지만 Fail 셀의 bin 번호 텍스트가 2차 식별자 역할을 한다.
const FAIL_PALETTE = ["#2a78d6","#eda100","#e34948","#4a3aa7","#eb6834","#e87ba4","#1baf7a"];
const PASS_COLOR = "#0ca30c";
const PLOTLY_FONT = { family: 'system-ui, -apple-system, "Segoe UI", sans-serif', size: 11, color: "#52514e" };

function webReportSheets() {
  return (DATA && DATA.web_report && DATA.web_report.sheets) ? DATA.web_report.sheets : null;
}

function makeBinColorMap(binList) {
  const map = {}; let fi = 0;
  binList.forEach(b => {
    if (b === "1") map[b] = PASS_COLOR;
    else { map[b] = FAIL_PALETTE[fi % FAIL_PALETTE.length]; fi++; }
  });
  return map;
}

// 세션 전체에서 같은 bin 은 어느 차트(Summary 미니 웨이퍼/Fail Bin 막대/Yield Pareto/
// Map Analysis 범례)에서든 같은 색이 되도록, Map Analysis 전역 범례 순서를 기준으로
// 색상 매핑을 한 번만 만든다. load() 가 DATA 갱신 시 _globalBinColors 를 초기화한다.
let _globalBinColors = null;
function globalBinColorMap() {
  if (_globalBinColors) return _globalBinColors;
  const sheets = webReportSheets() || {};
  const bins = [];
  buildGlobalBinLegend(sheets["Map Analysis"] || []).forEach(r => {
    const b = String(r.bin);
    if (!bins.includes(b)) bins.push(b);
  });
  (sheets["Fail Bin"] || []).forEach(r => {
    const b = String(r.bin);
    if (!bins.includes(b)) bins.push(b);
  });
  _globalBinColors = makeBinColorMap(bins);
  return _globalBinColors;
}

// 전역 매핑에 없던 bin(좌표 없는 die 등)도 안정적으로 이어서 색을 배정.
function binColor(bin) {
  const map = globalBinColorMap();
  const b = String(bin);
  if (!(b in map)) {
    const failCount = Object.values(map).filter(c => c !== PASS_COLOR).length;
    map[b] = (b === "1") ? PASS_COLOR : FAIL_PALETTE[failCount % FAIL_PALETTE.length];
  }
  return map[b];
}

// 소스 map dict → { trace } (die → bin 이산 heatmap). opts.showText: Fail 셀에 bin 번호.
// opts.binOrder/opts.colorMap 을 넘기면 그 값을 그대로 쓴다 (여러 맵 간 동일 bin 색상 통일용).
function waferHeatmap(m, opts) {
  opts = opts || {};
  const xMin = m.x_min, xMax = m.x_max, yMin = m.y_min, yMax = m.y_max;
  if (xMin == null || yMin == null) return null;
  const W = xMax - xMin + 1, H = yMax - yMin + 1;
  const binOrder = opts.binOrder || (m.bin_counts || []).map(bc => bc.bin);
  const colorMap = opts.colorMap || makeBinColorMap(binOrder);
  const binIndex = {}; binOrder.forEach((b, i) => { binIndex[b] = i; });
  const N = binOrder.length || 1;

  const z = Array.from({ length: H }, () => Array(W).fill(null));
  const text = Array.from({ length: H }, () => Array(W).fill(""));
  const cbin = Array.from({ length: H }, () => Array(W).fill(""));
  (m.dies || []).forEach(d => {
    const c = d.x - xMin, r = d.y - yMin;
    if (r < 0 || r >= H || c < 0 || c >= W) return;
    const idx = binIndex[d.bin] != null ? binIndex[d.bin] : 0;
    z[r][c] = idx + 0.5;
    cbin[r][c] = d.bin;
    if (d.bin !== "1") text[r][c] = d.bin;   // Fail 셀만 bin 번호, Pass 는 빈칸
  });

  const colorscale = [];
  binOrder.forEach((b, i) => {
    colorscale.push([i / N, colorMap[b]]);
    colorscale.push([(i + 1) / N, colorMap[b]]);
  });

  const trace = {
    type: "heatmap", z, zmin: 0, zmax: N,
    x0: xMin, dx: 1, y0: yMin, dy: 1,
    colorscale: colorscale.length ? colorscale : [[0, PASS_COLOR], [1, PASS_COLOR]],
    showscale: false, xgap: 0.5, ygap: 0.5, hoverongaps: false,
    customdata: cbin,
  };
  if (opts.showText) {
    trace.text = text;
    trace.texttemplate = "%{text}";
    trace.textfont = { size: opts.textSize || 8, color: "#0b0b0b" };
  }
  if (opts.mini) trace.hoverinfo = "skip";
  else trace.hovertemplate = "(%{x}, %{y})<br>Bin %{customdata}<extra></extra>";
  return { trace, colorMap, binOrder };
}

// MDDI/PDDI 는 chip 이 세로로 길쭉(die pitch Y>X)해 같은 원형 웨이퍼에서 Y die 수가 적다.
// 셀을 정사각(scaleratio 1)으로 두면 웨이퍼가 가로로 납작해 보이므로, 격자 폭/높이(W/H)만큼
// Y 셀을 세로로 늘려(scaleratio) 원형에 가깝게 그린다. 그 외 제품은 1(정사각) 유지.
function waferCellYScale(m) {
  const pt = ((DATA && DATA.session && DATA.session.product_type) || "").trim().toUpperCase();
  if (pt !== "MDDI" && pt !== "PDDI") return 1;
  if (m.x_min == null || m.y_min == null) return 1;
  const W = m.x_max - m.x_min + 1, H = m.y_max - m.y_min + 1;
  return (W > 0 && H > 0) ? W / H : 1;
}

function waferLayout(m, opts) {
  opts = opts || {};
  const layout = {
    margin: opts.mini ? { l: 2, r: 2, t: 2, b: 2 } : { l: 42, r: 10, t: 8, b: 36 },
    paper_bgcolor: "#ffffff", plot_bgcolor: "#ffffff", font: PLOTLY_FONT,
    showlegend: false,
    // 셀은 x 에 scale 고정. 정사각(=1)이 기본, MDDI/PDDI 는 tall chip 반영해 y 를 늘림.
    xaxis: { zeroline: false, showgrid: false, constrain: "domain",
             title: opts.mini ? "" : "X", visible: !opts.mini },
    // 웨이퍼 맵 관례: Y 는 위에서 아래로 내려갈수록 커진다(=y축 역방향).
    yaxis: { zeroline: false, showgrid: false, constrain: "domain",
             scaleanchor: "x", scaleratio: waferCellYScale(m), autorange: "reversed",
             title: opts.mini ? "" : "Y", visible: !opts.mini },
  };
  return layout;
}

// 모든 소스의 bin_counts 를 합산해 하나의 공통 범례 순서/집계를 만든다.
// Pass 항상 최상단, 나머지는 전체 합산 count 내림차순.
// step 분리 맵(m.step 있음)은 Pass 칩이 step 수만큼 중복 등장하므로 Pass 는 소스별
// step 맵 중 최솟값(=마지막 step 의 Pass = 전체 Pass)만 반영한다. fail bin 은 칩당
// 한 step 맵에만 나오므로 그대로 합산. step 없는 맵(단일 STEP/legacy)은 현행 합산.
function buildGlobalBinLegend(maps) {
  const totals = {};
  const order = [];
  const stepPassBySource = {};   // source → step 맵별 Pass count 목록
  (maps || []).forEach(m => {
    let stepPass = 0;
    (m.bin_counts || []).forEach(bc => {
      if (!(bc.bin in totals)) { order.push(bc.bin); totals[bc.bin] = { count: 0, is_pass: bc.is_pass }; }
      if (bc.is_pass && m.step != null) stepPass += bc.count;
      else totals[bc.bin].count += bc.count;
    });
    if (m.step != null) {
      (stepPassBySource[m.source] = stepPassBySource[m.source] || []).push(stepPass);
    }
  });
  const passBin = order.find(b => totals[b].is_pass);
  if (passBin != null) {
    Object.values(stepPassBySource).forEach(arr => {
      totals[passBin].count += Math.min.apply(null, arr);
    });
  }
  order.sort((a, b) => {
    const pa = totals[a].is_pass, pb = totals[b].is_pass;
    if (pa !== pb) return pa ? -1 : 1;
    return totals[b].count - totals[a].count;
  });
  const grandTotal = order.reduce((s, b) => s + totals[b].count, 0);
  return order.map(b => ({
    bin: b, count: totals[b].count, is_pass: totals[b].is_pass,
    pct: grandTotal ? Math.round((totals[b].count / grandTotal) * 10000) / 100 : 0,
  }));
}

// selected 는 단일 bin(문자열) 또는 Set(다중선택) 둘 다 지원. 선택 행에 is-selected + data-bin.
function _binIsSelected(selected, bin) {
  if (selected instanceof Set) return selected.has(bin);
  return selected != null && bin === selected;
}
function binLegendHtml(legendRows, colorMap, selected) {
  const body = legendRows.map(bc => {
    const cls = [bc.is_pass ? "is-pass" : "", _binIsSelected(selected, bc.bin) ? "is-selected" : ""]
      .filter(Boolean).join(" ");
    return `<tr${cls ? ` class="${cls}"` : ""} data-bin="${esc(bc.bin)}">` +
      `<td><span class="bin-swatch" style="background:${colorMap[bc.bin]}"></span>${esc(bc.bin)}${bc.is_pass ? " (Pass)" : ""}</td>` +
      `<td>${bc.count}</td><td>${bc.pct}%</td></tr>`;
  }).join("");
  return `<table class="bin-table"><thead><tr><th>Bin</th><th>Count</th><th>비율</th></tr></thead>` +
         `<tbody>${body}</tbody></table>`;
}

// 선택된 bin만 원색 유지, 나머지는 회색으로 dim. selected 는 단일 bin(문자열) 또는 Set(다중).
const MAP_BIN_DIM_COLOR = "#d9d9d9";
function dimColorMap(colorMap, binOrder, selected) {
  const isSet = selected instanceof Set;
  if (!selected || (isSet && selected.size === 0)) return colorMap;
  const out = {};
  binOrder.forEach(b => { out[b] = _binIsSelected(selected, b) ? colorMap[b] : MAP_BIN_DIM_COLOR; });
  return out;
}

// source 가 여럿이면 가로 2칸 그리드로 wafer map 을 나열하고, bin 범례는 전체 소스
// 합산 기준으로 한 번만 만들어 오른쪽에 고정(sticky)한다. 모든 맵이 같은 색상 매핑을 쓴다.
let mapGridCols = 1;   // Map Analysis 가로 칸수 기본 1칸(확대). 숫자 입력으로 조절, 세션 내 유지.
// 칸수가 적을수록 맵을 크게(높게) 보여준다 — scaleanchor 정사각 맵이라 폭·높이 함께 커져야 확대됨.
function mapPlotHeight() { return Math.min(720, Math.max(220, Math.round(720 / mapGridCols))); }

function renderMapAnalysis() {
  const panel = document.getElementById("panel-map-analysis");
  const sheets = webReportSheets();
  const maps = sheets ? sheets["Map Analysis"] : null;
  if (!window.Plotly || !maps || !maps.length) {
    emptyPanel(panel, "Map Analysis 데이터 없음"); return;
  }
  panel.classList.add("viz-root");

  const legendRows = buildGlobalBinLegend(maps);
  const binOrder = legendRows.map(r => r.bin);
  const colorMap = globalBinColorMap();   // 세션 전체 공통 색상 (Summary/Fail Bin 과 일치)
  const mapBinFilter = new Set();   // 범례 클릭으로 선택된 bin 다중선택(재클릭 시 해제, 없으면 전체 표시)

  const plotH = mapPlotHeight();
  // 선택 좌표 색 Legend (Map Analysis 전용). 각 항목: 색 스와치 + 좌표 + 제거(×).
  const selLegend = mapSelChips.length
    ? `<div class="mapsel-legend"><span class="mapsel-leg-title">선택 좌표</span>` +
      mapSelChips.map(c =>
        `<span class="mapsel-leg-item"><span class="mapsel-sw" style="background:${c.color}"></span>` +
        `X ${esc(c.xpos)}·Y ${esc(c.ypos)} <span class="mapsel-src">${esc(c.source)}</span>` +
        `<button type="button" class="mapsel-del" data-key="${esc(c.key)}" title="제거">×</button></span>`
      ).join("") +
      `<button type="button" id="mapSelClearBtn" class="btn-sm mapsel-clear">전체 해제</button></div>`
    : "";
  panel.innerHTML =
    `<div class="map-toolbar">가로 칸수 ` +
    `<input type="number" id="mapGridColsInput" min="1" max="8" step="1" value="${mapGridCols}">` +
    `<span class="map-toolbar-hint">칸 (1 = 확대해서 보기 · 2~3 = 한꺼번에 보기)</span>` +
    `<span class="mapsel-sep"></span>` +
    `<button type="button" id="mapSelBtn" class="btn-sm">좌표 선택</button>` +
    `</div>` +
    `<div id="mapSelSearchBox" class="mapsel-search" style="display:none">` +
      `<div class="common-search">` +
        `<input id="mapSelSerial" type="text" class="mapsel-field" placeholder="SERIAL (부분일치)" />` +
        `<input id="mapSelXpos" type="text" class="mapsel-field" placeholder="XPOS (정확)" />` +
        `<input id="mapSelYpos" type="text" class="mapsel-field" placeholder="YPOS (정확)" />` +
        `<button id="mapSelSearchBtn" class="btn-sm">검색</button>` +
        `<button id="mapSelAddSelected" class="btn-sm primary" disabled>선택 추가</button>` +
        `<button id="mapSelCollapseBtn" class="btn-sm" title="검색 패널 접기">접기 ▲</button>` +
        `<span id="mapSelInfo" class="muted"></span>` +
      `</div>` +
      `<div class="common-list mapsel-list" id="mapSelList"><div class="placeholder">SERIAL(부분) / XPOS·YPOS(정확) 칸에 입력해 검색하고, 체크한 좌표를 '선택 추가' 로 한 번에 추가하세요 (여러 개 가능).</div></div>` +
    `</div>` +
    selLegend +
    `<div class="wafer-analysis-layout">` +
    `<div class="wafer-grid" style="grid-template-columns:repeat(${mapGridCols}, minmax(0, 1fr))">` +
    maps.map((m, i) =>
      `<div class="wafer-card">
        <div class="wafer-card-title">${esc(m.source)}${m.step ? " — " + esc(m.step) : ""} — ${esc(String(m.total))} dies</div>
        <div id="wafer-full-${i}" style="width:100%;height:${plotH}px;"></div>
      </div>`).join("") +
    `</div>` +
    `<div class="wafer-legend-fixed">` +
    `<div class="wafer-legend-title">Bin Legend</div>` +
    `<div class="wafer-legend-body"></div>` +
    `</div>` +
    `</div>`;

  panel.querySelector("#mapGridColsInput").addEventListener("change", (e) => {
    const v = parseInt(e.target.value, 10);
    mapGridCols = isNaN(v) ? 1 : Math.min(8, Math.max(1, v));
    renderMapAnalysis();   // 칸수 변경 → 그리드·플롯 높이 다시 그림(범례 선택은 초기화됨)
  });

  // 좌표 선택 툴바 — 검색 패널 토글 + 검색 + 해제.
  panel.querySelector("#mapSelBtn").addEventListener("click", mapSelToggleSearch);
  const _mapSelClearBtn = panel.querySelector("#mapSelClearBtn");
  if (_mapSelClearBtn) _mapSelClearBtn.addEventListener("click", mapSelClear);
  const _doMapSelSearch = () => mapSelSearch();
  panel.querySelector("#mapSelSearchBtn").addEventListener("click", _doMapSelSearch);
  panel.querySelector("#mapSelAddSelected").addEventListener("click", mapSelAddSelected);
  panel.querySelector("#mapSelCollapseBtn").addEventListener("click", () => {
    const box = document.getElementById("mapSelSearchBox");
    if (box) box.style.display = "none";   // 검색 패널 접기(명시적 닫기).
  });
  ["mapSelSerial", "mapSelXpos", "mapSelYpos"].forEach(id => {
    const el = panel.querySelector("#" + id);
    if (el) el.addEventListener("keydown", e => { if (e.key === "Enter") _doMapSelSearch(); });
  });
  panel.querySelectorAll(".mapsel-del").forEach(b => b.addEventListener("click", () => mapSelRemove(b.dataset.key)));

  // colorMap 만 dim 처리해서 다시 넘기면 되므로 waferHeatmap 구조는 그대로 재사용.
  function redraw() {
    const activeColorMap = dimColorMap(colorMap, binOrder, mapBinFilter);
    maps.forEach((m, i) => {
      const built = waferHeatmap(m, { showText: true, textSize: 8, colorMap: activeColorMap, binOrder });
      if (!built) return;
      const traces = [built.trace];
      // 이 source 의 map 에 속한 선택 좌표들을 각자 색의 빈 원으로 강조.
      mapSelChips.forEach(c => {
        if (c.source === m.source && c.x != null && c.y != null) {
          traces.push({ type: "scatter", mode: "markers", x: [c.x], y: [c.y],
            marker: { symbol: "circle-open", size: 20, color: c.color, line: { width: 3, color: c.color } },
            hovertemplate: `X ${c.x} · Y ${c.y}<extra></extra>` });
        }
      });
      Plotly.react(`wafer-full-${i}`, traces, waferLayout(m, {}),
        { responsive: true, displayModeBar: false });
    });
    const legendBody = panel.querySelector(".wafer-legend-body");
    legendBody.innerHTML = binLegendHtml(legendRows, colorMap, mapBinFilter);
    legendBody.querySelectorAll("tbody tr[data-bin]").forEach(tr => {
      tr.addEventListener("click", () => {
        const bin = tr.dataset.bin;
        if (mapBinFilter.has(bin)) mapBinFilter.delete(bin); else mapBinFilter.add(bin);
        redraw();
      });
    });
  }
  redraw();
}


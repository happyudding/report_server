// ── web_report: wafer map + fail-bin 차트 (Plotly) ─────────────────────────
// Pass 는 초록 고정, Fail 은 등장 순서대로 팔레트(dataviz 검증 팔레트에서 초록 제외).
// 색이 넘치면 순환하지만 Fail 셀의 bin 번호 텍스트가 2차 식별자 역할을 한다.
const FAIL_PALETTE = ["#2a78d6","#eda100","#e34948","#4a3aa7","#eb6834","#e87ba4","#1baf7a"];
const PASS_COLOR = "#0ca30c";
const PLOTLY_FONT = { family: 'system-ui, -apple-system, "Segoe UI", sans-serif', size: 11, color: "#52514e" };

// die 수가 이 값을 넘으면 Bin Map 을 이미지 모드(gap=0, Bin 라벨 off)로 그려 SVG 셀 폭증(freeze)을
// 막는다. Detail 확대 시엔 보이는 die 가 적어 forceGap/forceText 로 격자선·라벨을 되살린다.
const MAP_DENSE_DIES = 3000;
// 회색 die(앞 STEP fail — 모양만 유지)·TNO Map 기타 항목 색.
const MAP_GRAY_RGB = [200, 204, 208];
const TNO_OTHER_COLOR = "#9aa0a6";

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

// 소스 map dict → { trace } (die → 이산 heatmap). opts.catOf(die→카테고리, 기본 bin)/order/colorMap
// 로 Bin·TNO·회색 등 어떤 축이든 색칠(여러 맵 간 색 통일용). Detail(Plotly)·Compare·미니맵 공용.
// (맵 셀 bin 번호 텍스트는 표시하지 않는다 — Bin 은 Legend/ hover 로 확인.)
function waferHeatmap(m, opts) {
  opts = opts || {};
  const xMin = m.x_min, xMax = m.x_max, yMin = m.y_min, yMax = m.y_max;
  if (xMin == null || yMin == null) return null;
  const W = xMax - xMin + 1, H = yMax - yMin + 1;
  const order = opts.order || opts.binOrder || (m.bin_counts || []).map(bc => bc.bin);
  const colorMap = opts.colorMap || makeBinColorMap(order);
  const catOf = opts.catOf || (d => d.bin);   // die → 색 카테고리(기본 bin). TNO/회색은 호출부가 지정.
  const catIndex = {}; order.forEach((c, i) => { catIndex[c] = i; });
  const N = order.length || 1;

  // die 가 조밀하면(임계 초과) 셀 gap>0 은 Plotly 가 die 마다 SVG rect(brick)를 만들어 무겁다
  // → gap=0 이미지 모드. opts.forceGap 은 Detail 확대 시 격자선 복원용. die 는 전량 유지(다운샘플 아님).
  const dense = (m.dies || []).length > MAP_DENSE_DIES;
  const useGap = opts.forceGap || !dense;

  const z = Array.from({ length: H }, () => Array(W).fill(null));
  const cdata = Array.from({ length: H }, () => Array(W).fill(""));
  (m.dies || []).forEach(d => {
    const c = d.x - xMin, r = d.y - yMin;
    if (r < 0 || r >= H || c < 0 || c >= W) return;
    const cat = catOf(d);
    const idx = catIndex[cat] != null ? catIndex[cat] : 0;
    z[r][c] = idx + 0.5;
    cdata[r][c] = d.g ? "(prev-fail)" : cat;   // hover 표시(회색 die 는 이전 step fail)
  });

  const trace = {
    type: "heatmap", z, zmin: 0, zmax: N,
    x0: xMin, dx: 1, y0: yMin, dy: 1,
    colorscale: binColorscale(order, colorMap),
    showscale: false, xgap: useGap ? 0.5 : 0, ygap: useGap ? 0.5 : 0, hoverongaps: false,
    customdata: cdata,
  };
  if (opts.mini) trace.hoverinfo = "skip";
  else trace.hovertemplate = opts.hovertemplate || "(%{x}, %{y})<br>%{customdata}<extra></extra>";
  return { trace, colorMap, binOrder: order };
}

// bin 이산 colorscale (각 bin 을 두 정지점으로 계단화). waferHeatmap·범례 색 restyle 공용.
function binColorscale(binOrder, colorMap) {
  const cs = []; const N = binOrder.length || 1;
  binOrder.forEach((b, i) => { cs.push([i / N, colorMap[b]]); cs.push([(i + 1) / N, colorMap[b]]); });
  return cs.length ? cs : [[0, PASS_COLOR], [1, PASS_COLOR]];
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
function binLegendHtml(legendRows, colorMap, selected, descMap) {
  const desc = descMap || {};
  const body = legendRows.map(bc => {
    const cls = [bc.is_pass ? "is-pass" : "", _binIsSelected(selected, bc.bin) ? "is-selected" : ""]
      .filter(Boolean).join(" ");
    const d = bc.is_pass ? "" : (desc[String(bc.bin)] || "");
    return `<tr${cls ? ` class="${cls}"` : ""} data-bin="${esc(bc.bin)}">` +
      `<td><span class="bin-swatch" style="background:${colorMap[bc.bin]}"></span>${esc(bc.bin)}${bc.is_pass ? " (Pass)" : ""}</td>` +
      `<td class="bin-desc" title="${esc(d)}">${esc(d)}</td>` +
      `<td>${bc.count}</td><td>${bc.pct}%</td></tr>`;
  }).join("");
  return `<table class="bin-table"><thead><tr><th>Bin</th><th>Description</th><th>Count</th><th>비율</th></tr></thead>` +
         `<tbody>${body}</tbody></table>`;
}

// DUT Legend (DUT 모드 병합 맵 전용) — 클릭 시 해당 DUT 강조. 색 스와치 없음(색은 bin 기준).
function dutLegendHtml(dutList, selected) {
  const body = (dutList || []).map(d =>
    `<tr${d === selected ? ` class="is-selected"` : ""} data-dut="${esc(d)}"><td>DUT ${esc(d)}</td></tr>`).join("");
  return `<table class="bin-table dut-table"><tbody>${body}</tbody></table>`;
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

// Map Analysis 서브모드: "bin"=Bin Map(기존), "stdf"=STDF Map(값 기반, stdf_map.js). 세션 내 유지.
let mapMode = "bin";
// Map 초기 그리기(rAF 스텝퍼) 재진입 가드 — 새 렌더가 시작되면 이전 체인을 중단시킨다.
let _mapDrawToken = 0;
// 색 기준 축: "bin"=Bin Map(기존), "tno"=die 가 fail 난 항목(FAILTNO)별 색. 세션 내 유지.
let mapColorKey = "bin";

// ── canvas 썸네일 렌더 (갤러리) ────────────────────────────────────────────────
// hex → [r,g,b]. 6자리 hex 만 지원(팔레트가 전부 6자리).
function hexToRgb(hex) {
  const h = String(hex || "").replace("#", "");
  return [parseInt(h.slice(0, 2), 16) || 0, parseInt(h.slice(2, 4), 16) || 0, parseInt(h.slice(4, 6), 16) || 0];
}
// 흰색과 블렌드해 흐리게(DUT 미선택 die). k=원색 비율.
function fadeRgb(rgb, k) {
  return [Math.round(rgb[0] * k + 255 * (1 - k)),
          Math.round(rgb[1] * k + 255 * (1 - k)),
          Math.round(rgb[2] * k + 255 * (1 - k))];
}
// die 격자를 canvas 에 그린다 — die 당 cellSize px 블록 + 셀 사이 1px 격자선(투명=흰 카드 노출)으로
// 각 chip 구분·윤곽선을 유지(Plotly xgap brick 과 동일 시각, SVG 없이 픽셀 한 번에 → 빠름).
// rgbFor(die, cache) → [r,g,b] 또는 null(그리지 않음). Y 는 위=작은 값(웨이퍼 관례).
function drawWaferThumb(canvas, m, rgbFor) {
  const xMin = m.x_min, yMin = m.y_min;
  if (xMin == null || yMin == null) return;
  const W = m.x_max - xMin + 1, H = m.y_max - yMin + 1;
  if (!(W > 0) || !(H > 0)) return;
  // 캔버스 픽셀 상한(~1600px)을 넘지 않게 cellSize 자동 축소, 최소 2(1px 격자선 확보).
  const MAXPX = 1600;
  let cell = Math.min(6, Math.floor(MAXPX / Math.max(W, H)) || 2);
  if (cell < 2) cell = 2;
  const gap = cell >= 3 ? 1 : 0;   // cell 이 너무 작으면 격자선 생략
  const CW = W * cell, CH = H * cell;
  canvas.width = CW; canvas.height = CH;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(CW, CH);
  const data = img.data;
  const cache = {};
  const dies = m.dies || [];
  const w = cell - gap, h = cell - gap;
  for (let k = 0; k < dies.length; k++) {
    const d = dies[k];
    const cx = d.x - xMin, cy = d.y - yMin;
    if (cx < 0 || cx >= W || cy < 0 || cy >= H) continue;
    const rgb = rgbFor(d, cache);
    if (!rgb) continue;
    const px0 = cx * cell, py0 = cy * cell;
    for (let yy = 0; yy < h; yy++) {
      let off = ((py0 + yy) * CW + px0) * 4;
      for (let xx = 0; xx < w; xx++) {
        data[off] = rgb[0]; data[off + 1] = rgb[1]; data[off + 2] = rgb[2]; data[off + 3] = 255;
        off += 4;
      }
    }
  }
  ctx.putImageData(img, 0, 0);
}

// sheets["Yield"] → bin 별 대표 fail item(most fail = avg 최대). Bin Legend description 용.
function buildBinDescMap() {
  const yl = (webReportSheets() || {})["Yield"] || [];
  const best = {};   // bin → {item, avg}
  yl.forEach(r => {
    const b = String(r.bin);
    if (!b || b === "1" || !r.Item) return;
    const avg = Number(r.avg) || 0;
    if (!(b in best) || avg > best[b].avg) best[b] = { item: r.Item, avg };
  });
  const desc = {};
  Object.keys(best).forEach(b => { desc[b] = best[b].item; });
  return desc;
}

// sheets["Fail Bin"](fail_bin_ranking {bin,item,count}) → fail item count 집계.
// 상위 FAIL_PALETTE 개만 팔레트 색(top), 나머지는 "기타"(중립색). TNO Map/TNO Legend 용.
function buildTnoInfo() {
  const fb = (webReportSheets() || {})["Fail Bin"] || [];
  const cnt = {};
  fb.forEach(r => { const it = r.item; if (it) cnt[it] = (cnt[it] || 0) + (Number(r.count) || 0); });
  const items = Object.keys(cnt).sort((a, b) => cnt[b] - cnt[a]);
  const colorMap = {};
  items.forEach((it, i) => { colorMap[it] = (i < FAIL_PALETTE.length) ? FAIL_PALETTE[i] : TNO_OTHER_COLOR; });
  const top = items.slice(0, FAIL_PALETTE.length);
  const otherCount = items.slice(FAIL_PALETTE.length).reduce((s, it) => s + cnt[it], 0);
  return { colorMap, items, top, cnt, otherCount };
}

// TNO Legend 표(상위 항목 색+count, 나머지 "기타" 1행). 클릭 dim 은 상위 항목만(data-tno).
function tnoLegendHtml(tnoInfo, selected) {
  const body = tnoInfo.top.map(it => {
    const sel = selected.has(it);
    const sw = (selected.size === 0 || sel) ? tnoInfo.colorMap[it] : MAP_BIN_DIM_COLOR;
    return `<tr${sel ? ` class="is-selected"` : ""} data-tno="${esc(it)}">` +
      `<td><span class="bin-swatch" style="background:${sw}"></span>${esc(it)}</td>` +
      `<td>${tnoInfo.cnt[it] || 0}</td></tr>`;
  }).join("");
  const other = tnoInfo.otherCount > 0
    ? `<tr class="tno-other"><td><span class="bin-swatch" style="background:${TNO_OTHER_COLOR}"></span>기타</td>` +
      `<td>${tnoInfo.otherCount}</td></tr>` : "";
  if (!body && !other) return `<div class="placeholder" style="padding:12px 4px">fail 항목 없음</div>`;
  return `<table class="bin-table"><thead><tr><th>Item</th><th>Count</th></tr></thead>` +
         `<tbody>${body}${other}</tbody></table>`;
}

// 갤러리 canvas wrap 의 CSS aspect-ratio(W : H*yScale) — Plotly scaleanchor 비율 재현.
function waferThumbAspect(m) {
  if (m.x_min == null || m.y_min == null) return "1 / 1";
  const W = m.x_max - m.x_min + 1, H = m.y_max - m.y_min + 1;
  const denom = Math.max(1, H * waferCellYScale(m));
  return `${W} / ${denom}`;
}

// 선택 좌표 마커를 canvas 위 CSS 절대위치 원으로 오버레이(canvas 는 hover 없음).
function renderThumbMarkers(wrap, m) {
  wrap.querySelectorAll(".wafer-sel-marker").forEach(e => e.remove());
  if (m.x_min == null || m.y_min == null) return;
  const W = m.x_max - m.x_min + 1, H = m.y_max - m.y_min + 1;
  mapSelChips.forEach(c => {
    if (c.source !== m.source || c.x == null || c.y == null) return;
    const cx = c.x - m.x_min, cy = c.y - m.y_min;
    if (cx < 0 || cx >= W || cy < 0 || cy >= H) return;
    const mk = document.createElement("div");
    mk.className = "wafer-sel-marker";
    mk.style.left = ((cx + 0.5) / W * 100) + "%";
    mk.style.top = ((cy + 0.5) / H * 100) + "%";
    mk.style.borderColor = c.color;
    wrap.appendChild(mk);
  });
}
// 두 서브모드 공통 세그먼트(패널 최상단). renderStdfMap 도 같은 마크업을 쓴다.
function mapModeSegHtml() {
  const seg = (m, label) => `<button class="distseg${mapMode === m ? " active" : ""}" data-mapmode="${m}">${label}</button>`;
  return `<div class="map-mode-seg distseg-group">${seg("bin", "Bin Map")}${seg("stdf", "STDF Map")}</div>`;
}
function bindMapModeSeg(panel) {
  panel.querySelectorAll("[data-mapmode]").forEach(b => b.addEventListener("click", () => {
    const m = b.dataset.mapmode;
    if (m !== mapMode) { mapMode = m; renderMapAnalysis(); }
  }));
}

function renderMapAnalysis() {
  const panel = document.getElementById("panel-map-analysis");
  if (mapMode === "stdf") { renderStdfMap(panel); return; }
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
  const mapTnoFilter = new Set();   // TNO 축 범례 클릭 필터
  const binDesc = buildBinDescMap();   // bin → 대표 fail item(Bin Legend description)
  const tnoInfo = buildTnoInfo();      // {colorMap, items, top, cnt, otherCount} (TNO 축·Legend)
  // DUT 모드: 병합 맵은 die 마다 dut 태그가 있고 row.duts 에 DUT 목록이 온다.
  const isDutMode = webReportMode() === "DUT";
  const dutList = (isDutMode && maps[0] && maps[0].duts) || [];
  let mapDutSelected = null;   // 강조 선택된 DUT (null = 전체 원색)

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
    mapModeSegHtml() +
    `<div class="map-toolbar">가로 칸수 ` +
    `<input type="number" id="mapGridColsInput" min="1" max="8" step="1" value="${mapGridCols}">` +
    `<span class="map-toolbar-hint">칸 (1 = 확대해서 보기 · 2~3 = 한꺼번에 보기)</span>` +
    `<span class="mapsel-sep"></span>` +
    `<span class="map-axis-seg distseg-group" title="색 기준 축">` +
      `<button type="button" class="distseg${mapColorKey === "bin" ? " active" : ""}" data-axis="bin">Bin</button>` +
      `<button type="button" class="distseg${mapColorKey === "tno" ? " active" : ""}" data-axis="tno">TNO</button>` +
    `</span>` +
    `<span class="mapsel-sep"></span>` +
    `<button type="button" id="mapSelBtn" class="btn-sm">좌표 선택</button>` +
    `<span id="mapRenderProg" class="muted map-render-prog"></span>` +
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
      `<div class="wafer-card wafer-card-clickable" data-map-index="${i}" title="클릭하면 크게(확대·마우스오버) 봅니다">
        <div class="wafer-card-title">${esc(m.source)}${m.step ? " — " + esc(m.step) : ""} — ${esc(String(m.total))} dies<span class="wafer-card-zoom">⤢ 크게 보기</span></div>
        <div id="wafer-full-${i}" class="wafer-thumb-wrap" style="aspect-ratio:${waferThumbAspect(m)}"><div class="placeholder">맵 로드 중…</div></div>
      </div>`).join("") +
    `</div>` +
    `<div class="wafer-legend-fixed">` +
    `<div class="wafer-legend-title">Bin Legend</div>` +
    `<div class="wafer-legend-body"></div>` +
    `<div class="wafer-legend-title tno-legend-title">TNO Legend</div>` +
    `<div class="tno-legend-body"></div>` +
    (dutList.length
      ? `<div class="wafer-legend-title dut-legend-title">DUT Legend</div>` +
        `<div class="dut-legend-hint">클릭 시 해당 DUT 강조 (나머지 연하게)</div>` +
        `<div class="dut-legend-body"></div>`
      : "") +
    `</div>` +
    `</div>`;

  bindMapModeSeg(panel);
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

  // 카드 클릭 → Map Detail 전체화면(확대·마우스오버). 갤러리는 개요(빠른 canvas 썸네일).
  panel.querySelector(".wafer-grid").addEventListener("click", (e) => {
    const card = e.target.closest(".wafer-card-clickable");
    if (!card) return;
    const idx = parseInt(card.dataset.mapIndex, 10);
    if (!isNaN(idx)) openMapDetail(idx);
  });
  // Bin / TNO 색 기준 축 전환.
  panel.querySelectorAll("[data-axis]").forEach(b => b.addEventListener("click", () => {
    const ax = b.dataset.axis;
    if (ax !== mapColorKey) { mapColorKey = ax; renderMapAnalysis(); }
  }));

  // 갤러리는 canvas 라 색 필터 변경 시 canvas 를 다시 그린다(맵당 <5ms, rAF 스텝퍼).
  function restyleColors() {
    drawAllMaps();
    renderLegendBody();
    renderTnoLegend();
  }
  function renderLegendBody() {
    const legendBody = panel.querySelector(".wafer-legend-body");
    if (!legendBody) return;
    legendBody.innerHTML = binLegendHtml(legendRows, colorMap, mapBinFilter, binDesc);
    legendBody.querySelectorAll("tbody tr[data-bin]").forEach(tr => {
      tr.addEventListener("click", () => {
        if (mapColorKey !== "bin") return;   // TNO 축일 땐 Bin 범례 클릭 무시
        const bin = tr.dataset.bin;
        if (mapBinFilter.has(bin)) mapBinFilter.delete(bin); else mapBinFilter.add(bin);
        restyleColors();
      });
    });
  }
  function renderTnoLegend() {
    const host = panel.querySelector(".tno-legend-body");
    if (!host) return;
    host.innerHTML = tnoLegendHtml(tnoInfo, mapTnoFilter);
    host.querySelectorAll("tbody tr[data-tno]").forEach(tr => {
      tr.addEventListener("click", () => {
        if (mapColorKey !== "tno") return;   // Bin 축일 땐 TNO 범례 클릭 무시
        const it = tr.dataset.tno;
        if (mapTnoFilter.has(it)) mapTnoFilter.delete(it); else mapTnoFilter.add(it);
        restyleColors();
      });
    });
  }
  function renderDutLegend() {
    const host = panel.querySelector(".dut-legend-body");
    if (!host) return;
    host.innerHTML = dutLegendHtml(dutList, mapDutSelected);
    host.querySelectorAll("tbody tr[data-dut]").forEach(tr => {
      tr.addEventListener("click", () => {
        const d = tr.dataset.dut;
        mapDutSelected = (mapDutSelected === d) ? null : d;   // 재클릭 시 해제(전체 원색)
        renderDutLegend();
        drawAllMaps();
      });
    });
  }
  function drawMap(i, activeColorMap) {
    const m = maps[i];
    const traces = [];
    const sel = (mapDutSelected && (m.duts || []).includes(mapDutSelected)) ? mapDutSelected : null;
    if (sel) {
      // 선택 DUT = 원색, 나머지 DUT = 같은 bin 색을 흐리게(opacity↓). bin 색 구분 유지.
      const others = (m.dies || []).filter(d => d.dut !== sel);
      const selfDies = (m.dies || []).filter(d => d.dut === sel);
      const faded = waferHeatmap(Object.assign({}, m, { dies: others }),
        { colorMap: activeColorMap, binOrder });
      if (faded) { faded.trace.opacity = 0.25; traces.push(faded.trace); }
      const full = waferHeatmap(Object.assign({}, m, { dies: selfDies }),
        { showText: true, textSize: 8, colorMap: activeColorMap, binOrder });
      if (full) traces.push(full.trace);
      if (!traces.length) return;
    } else {
      const built = waferHeatmap(m, { showText: true, textSize: 8, colorMap: activeColorMap, binOrder });
      if (!built) return;
      traces.push(built.trace);
    }
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
  }
  // 맵을 한 프레임에 한 장씩 그려 UI 스레드를 쪼갠다(대량 die freeze 방지) + 진행률 표시.
  function drawAllMaps() {
    const token = ++_mapDrawToken;
    const activeColorMap = dimColorMap(colorMap, binOrder, mapBinFilter);
    const prog = panel.querySelector("#mapRenderProg");
    let i = 0;
    function step() {
      if (token !== _mapDrawToken) return;   // 칸수 변경·chip 추가·재렌더가 시작되면 이전 체인 중단
      if (i >= maps.length) { if (prog) prog.textContent = ""; return; }
      if (prog && maps.length > 1) prog.textContent = `맵 ${i + 1} / ${maps.length} 그리는 중…`;
      drawMap(i, activeColorMap);
      i++;
      requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  renderLegendBody();
  renderDutLegend();
  drawAllMaps();
}

// ── Map Detail (카드 클릭 → 전체화면 1장: 확대·해상도·격자선·마우스오버) ───────────────
// item_detail 패턴 복제 — sticky-head 는 그대로 두고 #panel-map-detail 만 활성화, Back 복귀.
let _mapDetailReturnId = null;   // 복귀할 탭 패널 id
let _mapDetailIndex = 0;         // 현재 보는 맵 인덱스
let _mapDetailBound = false;
let _mapDetailBinFilter = new Set();

function mapDetailMaps() {
  const sheets = webReportSheets();
  return (sheets && sheets["Map Analysis"]) ? sheets["Map Analysis"] : [];
}

// Detail 플롯 높이 — 뷰포트에서 sticky/헤더 여백을 뺀 큰 값(해상도 우선), 420~900 clamp.
function mapDetailPlotHeight() {
  return Math.max(420, Math.min(900, window.innerHeight - 200));
}

function purgeMapDetailChart() {
  const el = document.getElementById("map-detail-plot");
  if (el && window.Plotly) { try { Plotly.purge(el); } catch (e) {} }
}

function openMapDetail(i) {
  const dp = document.getElementById("panel-map-detail");
  if (!dp) return;
  bindMapDetailPanel();
  if (!dp.classList.contains("active")) {
    const cur = document.querySelector(".content > .panel.active");
    _mapDetailReturnId = cur ? cur.id : "panel-map-analysis";
    if (cur) cur.classList.remove("active");
    dp.classList.add("active");
  }
  _mapDetailIndex = i;
  window.scrollTo(0, 0);
  renderMapDetail();
}

function closeMapDetail() {
  const dp = document.getElementById("panel-map-detail");
  if (!dp) return;
  purgeMapDetailChart();
  dp.classList.remove("active");
  dp.innerHTML = "";
  const back = document.getElementById(_mapDetailReturnId || "panel-map-analysis");
  if (back) {
    back.classList.add("active");
    // 갤러리 플롯은 Detail 동안 숨겨져(0폭) 있었으므로 복귀 시 현재 폭으로 리사이즈.
    if (window.Plotly) back.querySelectorAll(".js-plotly-plot").forEach(d => { try { Plotly.Plots.resize(d); } catch (e) {} });
  }
  _mapDetailReturnId = null;
}

// 탭 버튼 클릭 시: 복원 없이 상세만 닫는다(해당 탭 패널이 이어서 활성화됨).
function hideMapDetail() {
  const dp = document.getElementById("panel-map-detail");
  if (dp && dp.classList.contains("active")) { purgeMapDetailChart(); dp.classList.remove("active"); dp.innerHTML = ""; }
  _mapDetailReturnId = null;
}

function mapDetailNav(delta) {
  const maps = mapDetailMaps();
  if (!maps.length) return;
  let idx = _mapDetailIndex + delta;
  if (idx < 0) idx = 0;
  if (idx >= maps.length) idx = maps.length - 1;
  if (idx === _mapDetailIndex) return;
  _mapDetailIndex = idx;
  renderMapDetail();
}

function bindMapDetailPanel() {
  if (_mapDetailBound) return;
  const dp = document.getElementById("panel-map-detail");
  if (!dp) return;
  dp.addEventListener("click", e => {
    if (e.target.closest(".idet-back")) { closeMapDetail(); return; }
    if (e.target.closest(".mapd-prev")) { mapDetailNav(-1); return; }
    if (e.target.closest(".mapd-next")) { mapDetailNav(1); return; }
    const tr = e.target.closest("tbody tr[data-bin]");
    if (tr) { mapDetailToggleBin(tr.dataset.bin); return; }
  });
  document.addEventListener("keydown", e => {
    if (!dp.classList.contains("active")) return;
    if (e.key === "Escape") { closeMapDetail(); return; }
    if (e.altKey && e.key === "ArrowLeft") { e.preventDefault(); mapDetailNav(-1); }
    else if (e.altKey && e.key === "ArrowRight") { e.preventDefault(); mapDetailNav(1); }
  });
  _mapDetailBound = true;
}

// heatmap trace + 선택 좌표 오버레이. opts 로 forceGap/forceText 를 waferHeatmap 에 전달.
function mapDetailTraces(m, binOrder, colorMap, opts) {
  const activeColorMap = dimColorMap(colorMap, binOrder, _mapDetailBinFilter);
  const built = waferHeatmap(m, Object.assign(
    { showText: true, textSize: 9, colorMap: activeColorMap, binOrder }, opts || {}));
  if (!built) return null;
  const traces = [built.trace];
  mapSelChips.forEach(c => {
    if (c.source === m.source && c.x != null && c.y != null) {
      traces.push({ type: "scatter", mode: "markers", x: [c.x], y: [c.y],
        marker: { symbol: "circle-open", size: 22, color: c.color, line: { width: 3, color: c.color } },
        hovertemplate: `X ${c.x} · Y ${c.y}<extra></extra>` });
    }
  });
  return traces;
}

const MAP_DETAIL_CONFIG = {
  responsive: true, scrollZoom: true, displayModeBar: true, displaylogo: false,
  modeBarButtonsToRemove: ["select2d", "lasso2d", "toImage"],
};

function drawMapDetail(m, binOrder, colorMap, opts) {
  const traces = mapDetailTraces(m, binOrder, colorMap, opts);
  if (!traces) return;
  Plotly.newPlot("map-detail-plot", traces, waferLayout(m, {}), MAP_DETAIL_CONFIG);
}

// 확대 시 보이는 die 가 임계 이하로 줄면 격자선+Bin 라벨을 강제 복원하고, 리셋하면 이미지 모드로.
// forced 가드로 상태가 안 바뀌면 재렌더를 생략해 relayout 무한루프를 막는다.
function bindMapDetailZoom(el, m, binOrder, colorMap) {
  if (!el || !el.on) return;
  let forced = false;
  el.on("plotly_relayout", () => {
    const xa = el.layout && el.layout.xaxis, ya = el.layout && el.layout.yaxis;
    const xr = xa && xa.range, yr = ya && ya.range;
    const isAuto = (xa && xa.autorange) || (ya && ya.autorange);
    let visible = (m.dies || []).length;
    if (!isAuto && xr && yr) {
      const x0 = Math.min(xr[0], xr[1]), x1 = Math.max(xr[0], xr[1]);
      const y0 = Math.min(yr[0], yr[1]), y1 = Math.max(yr[0], yr[1]);
      visible = 0;
      const dies = m.dies || [];
      for (let k = 0; k < dies.length; k++) {
        const d = dies[k];
        if (d.x >= x0 && d.x <= x1 && d.y >= y0 && d.y <= y1) visible++;
      }
    }
    const wantForce = visible <= MAP_DENSE_DIES;
    if (wantForce === forced) return;
    forced = wantForce;
    const traces = mapDetailTraces(m, binOrder, colorMap,
      wantForce ? { forceGap: true, forceText: true } : null);
    if (traces) Plotly.react("map-detail-plot", traces, el.layout, MAP_DETAIL_CONFIG);
  });
}

function renderMapDetailLegend(m, legendRows, colorMap) {
  const dp = document.getElementById("panel-map-detail");
  if (!dp) return;
  const legendBody = dp.querySelector(".wafer-legend-body");
  if (legendBody) legendBody.innerHTML = binLegendHtml(legendRows, colorMap, _mapDetailBinFilter);
}

// 범례 클릭: 색만 restyle(확대/격자 상태 유지). z 재계산·재렌더 없음.
function mapDetailToggleBin(bin) {
  const maps = mapDetailMaps();
  const m = maps[_mapDetailIndex];
  if (!m) return;
  if (_mapDetailBinFilter.has(bin)) _mapDetailBinFilter.delete(bin); else _mapDetailBinFilter.add(bin);
  const legendRows = buildGlobalBinLegend(maps);
  const binOrder = legendRows.map(r => r.bin);
  const colorMap = globalBinColorMap();
  const el = document.getElementById("map-detail-plot");
  if (el && el.data) {
    const activeColorMap = dimColorMap(colorMap, binOrder, _mapDetailBinFilter);
    try { Plotly.restyle(el, { colorscale: [binColorscale(binOrder, activeColorMap)] }, [0]); } catch (e) {}
  }
  renderMapDetailLegend(m, legendRows, colorMap);
}

function renderMapDetail() {
  const dp = document.getElementById("panel-map-detail");
  if (!dp) return;
  const maps = mapDetailMaps();
  const m = maps[_mapDetailIndex];
  if (!m || !window.Plotly) {
    dp.innerHTML = `<div class="idet"><div class="idet-head"><button class="btn-sm idet-back">← Back</button></div>` +
      `<div class="placeholder">맵을 표시할 수 없습니다</div></div>`;
    return;
  }
  purgeMapDetailChart();
  dp.classList.add("viz-root");
  const legendRows = buildGlobalBinLegend(maps);
  const binOrder = legendRows.map(r => r.bin);
  const colorMap = globalBinColorMap();
  _mapDetailBinFilter = new Set();   // 맵 진입 시 필터 초기화

  const total = maps.length;
  const navHtml = total > 1
    ? `<button class="btn-sm mapd-prev" title="이전 (Alt+←)">‹</button>` +
      `<button class="btn-sm mapd-next" title="다음 (Alt+→)">›</button>` +
      `<span class="idet-navpos">${_mapDetailIndex + 1} / ${total}</span>` : "";

  dp.innerHTML =
    `<div class="idet">` +
      `<div class="idet-head">` +
        `<button class="btn-sm idet-back">← Back</button>` +
        navHtml +
        `<span class="idet-title"><b>${esc(m.source)}${m.step ? " — " + esc(m.step) : ""}</b>` +
        ` — ${esc(String(m.total))} dies` +
        `<span class="mapd-hint">스크롤/드래그로 확대 · 마우스오버로 X·Y·Bin · 더블클릭 리셋</span></span>` +
      `</div>` +
      `<div class="wafer-analysis-layout">` +
        `<div class="wafer-grid" style="grid-template-columns:repeat(1, minmax(0, 1fr))">` +
          `<div class="wafer-card">` +
            `<div id="map-detail-plot" style="width:100%;height:${mapDetailPlotHeight()}px;"><div class="placeholder">맵 로드 중…</div></div>` +
          `</div>` +
        `</div>` +
        `<div class="wafer-legend-fixed">` +
          `<div class="wafer-legend-title">Bin Legend</div>` +
          `<div class="wafer-legend-body"></div>` +
        `</div>` +
      `</div>` +
    `</div>`;

  renderMapDetailLegend(m, legendRows, colorMap);
  // 셸+placeholder 페인트 후 다음 프레임에 무거운 렌더(로딩 표시가 실제로 보이도록).
  requestAnimationFrame(() => {
    if (mapDetailMaps()[_mapDetailIndex] !== m) return;   // 그 사이 다른 맵으로 이동하면 취소
    drawMapDetail(m, binOrder, colorMap);
    bindMapDetailZoom(document.getElementById("map-detail-plot"), m, binOrder, colorMap);
  });
}


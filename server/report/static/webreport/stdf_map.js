// ── web_report: STDF Map (Map Analysis 탭의 값 기반 웨이퍼 맵 서브모드) ─────────
// Bin Map 이 die 별 BIN 값을 그리는 반면, STDF Map 은 특정 Source·Item 의 measurement
// value 를 같은 XPOS/YPOS 격자에 그린다. 색은 값 분포를 10분위(decile)로 나눈 파란
// 그라데이션(값↑ 진함)이고, legend 클릭으로 구간을 highlight(선택 외 회색 dim)한다.
// 격자 그리기·레이아웃·dim 로직은 wafer_charts.js 의 것을 값 기반으로 재사용한다.
//
// 전역 의존(로드 순서상 wafer_charts.js·core.js 뒤): mapModeSegHtml/bindMapModeSeg,
// waferLayout, MAP_BIN_DIM_COLOR, webReportSheets, esc, SESSION_ID, Plotly.

// dataviz 검증 sequential blue ramp(100→700)에서 균등 샘플한 10단계(연→진).
const STDF_BLUES = ["#cde2fb", "#b7d3f6", "#86b6ef", "#6da7ec", "#5598e7",
                    "#2a78d6", "#256abf", "#1c5cab", "#104281", "#0d366b"];
const STDF_DECILES = 10;

let stdfSource = null;             // 선택 source 이름(null = 첫 source 기본)
let stdfItem = null;               // 선택 item(subject), null = 미선택
let stdfBucketFilter = new Set();  // legend 다중선택 bucket index(비었으면 전체 표시)
const _stdfScatterCache = {};      // subject → scatter 응답(재선택 시 재fetch 회피)

// 정렬된 배열의 p 분위값(선형보간, numpy 기본과 동일 규약).
function stdfQuantile(sorted, p) {
  const n = sorted.length;
  if (n === 0) return NaN;
  if (n === 1) return sorted[0];
  const idx = p * (n - 1);
  const lo = Math.floor(idx), hi = Math.ceil(idx), frac = idx - lo;
  return sorted[lo] + frac * (sorted[hi] - sorted[lo]);
}

// 값 배열 → 10분위 bucket 경계·집계·값→bucket 매핑. 순수 함수(단위 검증 대상).
// 중복값이 많아 경계가 겹치면 해당 bucket 은 count 0 으로 남고(색은 유지), 값은
// 항상 유효 bucket(경계 이상인 최상위)으로 배정된다 — 빈 bucket 이 생겨도 크래시 없음.
function stdfDecileBuckets(values) {
  const clean = [];
  for (const v of values) { const n = Number(v); if (isFinite(n)) clean.push(n); }
  clean.sort((a, b) => a - b);
  const N = STDF_DECILES;
  const q = [];
  for (let k = 0; k <= N; k++) q.push(stdfQuantile(clean, k / N));  // q[0..N]
  // 내부 경계 q[1..N-1] 이상인 최상위 bucket 을 고른다(q 는 비감소라 단조).
  function indexOf(v) {
    const x = Number(v);
    if (!isFinite(x)) return -1;
    let b = 0;
    for (let k = 1; k < N; k++) { if (x >= q[k]) b = k; else break; }
    return b;
  }
  const buckets = [];
  for (let b = 0; b < N; b++) buckets.push({ lo: q[b], hi: q[b + 1], count: 0, color: STDF_BLUES[b] });
  for (const v of clean) { const b = indexOf(v); if (b >= 0) buckets[b].count++; }
  return { buckets, indexOf, n: clean.length, min: q[0], max: q[N] };
}

// 값 라벨 포맷(legend·hover 보조): 매우 크거나 작은 값은 지수, 그 외는 소수 3자리.
function stdfFmt(v) {
  if (v == null || !isFinite(v)) return "-";
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e6)) return Number(v).toExponential(2);
  return String(Math.round(Number(v) * 1000) / 1000);
}

// 선택 source 의 Map Analysis 프레임(x/y min·max). step 분리 시 같은 source 맵이
// 여럿이면 합집합. 없으면 null(호출부가 데이터 min/max 로 폴백).
function stdfFrameForSource(srcName) {
  const maps = (webReportSheets() || {})["Map Analysis"] || [];
  const matched = maps.filter(m => m.source === srcName && m.x_min != null && m.y_min != null);
  if (!matched.length) return null;
  let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
  matched.forEach(m => {
    xmin = Math.min(xmin, m.x_min); xmax = Math.max(xmax, m.x_max);
    ymin = Math.min(ymin, m.y_min); ymax = Math.max(ymax, m.y_max);
  });
  return { x_min: xmin, x_max: xmax, y_min: ymin, y_max: ymax };
}
function stdfFrameFromData(xs, ys) {
  let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
  for (let i = 0; i < xs.length; i++) {
    const x = parseInt(xs[i], 10), y = parseInt(ys[i], 10);
    if (!isNaN(x)) { xmin = Math.min(xmin, x); xmax = Math.max(xmax, x); }
    if (!isNaN(y)) { ymin = Math.min(ymin, y); ymax = Math.max(ymax, y); }
  }
  if (!isFinite(xmin) || !isFinite(ymin)) return null;
  return { x_min: xmin, x_max: xmax, y_min: ymin, y_max: ymax };
}

// die(xpos/ypos)별 값을 격자 heatmap 트레이스로. z=bucket index, customdata=실제 값.
// activeColors 는 dim 반영된 10색(선택 외 회색). waferHeatmap 과 동일한 이산 colorscale 방식.
function stdfHeatmapTrace(frame, xs, ys, vals, deciles, activeColors) {
  const xMin = frame.x_min, xMax = frame.x_max, yMin = frame.y_min, yMax = frame.y_max;
  const W = xMax - xMin + 1, H = yMax - yMin + 1;
  const N = STDF_DECILES;
  const z = Array.from({ length: H }, () => Array(W).fill(null));
  const cval = Array.from({ length: H }, () => Array(W).fill(null));
  for (let i = 0; i < vals.length; i++) {
    const x = parseInt(xs[i], 10), y = parseInt(ys[i], 10);
    if (isNaN(x) || isNaN(y)) continue;
    const c = x - xMin, r = y - yMin;
    if (r < 0 || r >= H || c < 0 || c >= W) continue;
    const b = deciles.indexOf(vals[i]);
    if (b < 0) continue;
    z[r][c] = b + 0.5;
    cval[r][c] = vals[i];
  }
  const colorscale = [];
  for (let b = 0; b < N; b++) {
    colorscale.push([b / N, activeColors[b]]);
    colorscale.push([(b + 1) / N, activeColors[b]]);
  }
  return {
    type: "heatmap", z, zmin: 0, zmax: N,
    x0: xMin, dx: 1, y0: yMin, dy: 1,
    colorscale, showscale: false, xgap: 0.5, ygap: 0.5, hoverongaps: false,
    customdata: cval,
    hovertemplate: "(X %{x}, Y %{y})<br>값 %{customdata}<extra></extra>",
  };
}

// 10분위 legend(값 낮은 구간이 위). 각 행: 색 스와치 + 값 범위 + die count. 클릭 highlight.
function stdfLegendHtml(buckets, selected) {
  const body = buckets.map((bk, b) => {
    const sel = selected.has(b);
    const swatch = (selected.size === 0 || sel) ? STDF_BLUES[b] : MAP_BIN_DIM_COLOR;
    const range = `${stdfFmt(bk.lo)} ~ ${stdfFmt(bk.hi)}`;
    return `<tr${sel ? ` class="is-selected"` : ""} data-bucket="${b}">` +
      `<td><span class="bin-swatch" style="background:${swatch}"></span>${esc(range)}</td>` +
      `<td>${bk.count}</td></tr>`;
  }).join("");
  return `<table class="bin-table stdf-legend-table"><thead><tr><th>값 범위</th><th>Count</th></tr></thead>` +
         `<tbody>${body}</tbody></table>`;
}

// scatter 엔드포인트(die 별 값)를 캐시와 함께 가져온다 — item_detail.js 와 동일 소스.
function stdfFetchScatter(subject) {
  if (_stdfScatterCache[subject]) return Promise.resolve(_stdfScatterCache[subject]);
  return fetch(`/pe/report/session/${SESSION_ID}/web_report/scatter/${encodeURIComponent(subject)}`)
    .then(res => { if (!res.ok) throw new Error("HTTP " + res.status); return res.json(); })
    .then(data => { _stdfScatterCache[subject] = data; return data; });
}

// STDF Map 서브모드 진입점(renderMapAnalysis 가 mapMode==="stdf" 일 때 호출).
function renderStdfMap(panel) {
  panel.classList.add("viz-root");
  const sources = (DATA.web_report && DATA.web_report.sources) || [];
  const distIndex = (DATA.web_report && DATA.web_report.distribution_index) || [];
  if (!window.Plotly || !sources.length || !distIndex.length) {
    panel.innerHTML = mapModeSegHtml() + `<div class="placeholder">STDF Map 데이터 없음</div>`;
    bindMapModeSeg(panel);
    return;
  }
  if (!stdfSource || !sources.some(s => s.name === stdfSource)) stdfSource = sources[0].name;

  const srcOpts = sources.map(s =>
    `<option value="${esc(s.name)}"${s.name === stdfSource ? " selected" : ""}>${esc(s.name)}</option>`).join("");
  panel.innerHTML =
    mapModeSegHtml() +
    `<div class="map-toolbar stdf-toolbar">` +
      `<label class="stdf-lbl">Source <select id="stdfSourceSel">${srcOpts}</select></label>` +
      `<div class="dist-search-wrap stdf-search-wrap">` +
        `<input id="stdfItemSearch" class="dist-search" type="text" autocomplete="off" ` +
          `placeholder="Item 검색" value="${stdfItem ? esc(stdfItem) : ""}">` +
        `<div id="stdfItemSuggest" class="dist-suggest" style="display:none"></div>` +
      `</div>` +
      (stdfItem ? `<button type="button" id="stdfClearItem" class="btn-sm">선택 해제</button>` : "") +
    `</div>` +
    `<div class="wafer-analysis-layout" id="stdfBody">` +
      `<div class="placeholder">Source 를 고르고 Item 을 검색·선택하세요.</div>` +
    `</div>`;
  bindMapModeSeg(panel);

  panel.querySelector("#stdfSourceSel").addEventListener("change", (e) => {
    stdfSource = e.target.value;
    stdfBucketFilter.clear();
    if (stdfItem) stdfRenderMap(panel);
  });

  const search = panel.querySelector("#stdfItemSearch");
  const suggest = panel.querySelector("#stdfItemSuggest");
  const doSuggest = () => stdfShowSuggest(search, suggest);
  search.addEventListener("input", doSuggest);
  search.addEventListener("focus", doSuggest);
  // item mousedown 이 blur 보다 먼저 처리되도록 blur 는 지연 후 닫는다.
  search.addEventListener("blur", () => setTimeout(() => { suggest.style.display = "none"; }, 150));

  const clr = panel.querySelector("#stdfClearItem");
  if (clr) clr.addEventListener("click", () => {
    stdfItem = null; stdfBucketFilter.clear(); renderMapAnalysis();
  });

  if (stdfItem) stdfRenderMap(panel);
}

// 검색어에 맞는 후보(최대 30) 표시. 빈 검색이면 앞부분 목록. 클릭 시 item 선택 → 재렌더.
function stdfShowSuggest(search, suggest) {
  const term = String(search.value || "").trim().toLowerCase();
  const distIndex = (DATA.web_report && DATA.web_report.distribution_index) || [];
  const rows = [];
  for (const r of distIndex) {
    if (!term || String(r.subject).toLowerCase().includes(term)) {
      rows.push(r);
      if (rows.length >= 30) break;
    }
  }
  if (!rows.length) { suggest.style.display = "none"; return; }
  suggest.innerHTML = rows.map(r =>
    `<div class="dist-sug-item stdf-sug-item" data-subject="${esc(r.subject)}">` +
      `<span class="sug-tno">${esc(r.test_num || "")}</span>` +
      `<span class="sug-name">${esc(r.subject)}</span></div>`).join("");
  suggest.style.display = "block";
  suggest.querySelectorAll(".stdf-sug-item").forEach(it => {
    it.addEventListener("mousedown", (e) => {
      e.preventDefault();
      stdfItem = it.dataset.subject;
      stdfBucketFilter.clear();
      renderMapAnalysis();
    });
  });
}

// 선택된 item 의 scatter 데이터를 가져와 맵을 그린다(item 이 그 사이 바뀌면 무시).
function stdfRenderMap(panel) {
  const body = panel.querySelector("#stdfBody");
  if (!body) return;
  const item = stdfItem;
  body.innerHTML = `<div class="placeholder">로드 중…</div>`;
  stdfFetchScatter(item).then(data => {
    if (stdfItem !== item) return;
    stdfDrawMap(body, data);
  }).catch(e => {
    if (stdfItem !== item) return;
    body.innerHTML = `<div class="placeholder">데이터 로드 실패 (${esc(e.message)})</div>`;
  });
}

function stdfDrawMap(body, data) {
  const srcObj = (data.sources || []).find(s => s.name === stdfSource);
  if (!srcObj || !srcObj.values || !srcObj.values.length) {
    body.innerHTML = `<div class="placeholder">이 Source(${esc(stdfSource)}) 에 「${esc(stdfItem)}」 측정값이 없습니다.</div>`;
    return;
  }
  const xs = srcObj.xpos, ys = srcObj.ypos, vals = srcObj.values;
  const frame = stdfFrameForSource(stdfSource) || stdfFrameFromData(xs, ys);
  if (!frame) { body.innerHTML = `<div class="placeholder">좌표 정보가 없어 맵을 그릴 수 없습니다.</div>`; return; }
  const deciles = stdfDecileBuckets(vals);
  const units = data.units || "";
  const title = `${esc(stdfSource)} — ${esc(stdfItem)}${units ? ` (${esc(units)})` : ""} — ${vals.length} dies`;

  body.innerHTML =
    `<div class="wafer-grid" style="grid-template-columns:repeat(1, minmax(0, 1fr))">` +
      `<div class="wafer-card">` +
        `<div class="wafer-card-title">${title}</div>` +
        `<div id="stdf-map-plot" style="width:100%;height:640px;"></div>` +
      `</div>` +
    `</div>` +
    `<div class="wafer-legend-fixed">` +
      `<div class="wafer-legend-title">값 분포 (10분위)</div>` +
      `<div class="wafer-legend-body"></div>` +
    `</div>`;
  const legendBody = body.querySelector(".wafer-legend-body");

  function redraw() {
    const activeColors = deciles.buckets.map((bk, b) =>
      (stdfBucketFilter.size === 0 || stdfBucketFilter.has(b)) ? STDF_BLUES[b] : MAP_BIN_DIM_COLOR);
    const trace = stdfHeatmapTrace(frame, xs, ys, vals, deciles, activeColors);
    Plotly.react("stdf-map-plot", [trace], waferLayout(frame, {}), { responsive: true, displayModeBar: false });
    legendBody.innerHTML = stdfLegendHtml(deciles.buckets, stdfBucketFilter);
    legendBody.querySelectorAll("tbody tr[data-bucket]").forEach(tr => {
      tr.addEventListener("click", () => {
        const b = parseInt(tr.dataset.bucket, 10);
        if (stdfBucketFilter.has(b)) stdfBucketFilter.delete(b); else stdfBucketFilter.add(b);
        redraw();
      });
    });
  }
  redraw();
}

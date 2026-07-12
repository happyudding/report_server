// ── Distribution (ECDF, small-multiples grid) ─────────────────────────────
// F:\COINAPI\report_webserver\plotly_core_code (figure_builder.py/page_builder.py) 참고:
// item(subject)별 산점(scattergl, value vs 누적%) + spec 상/하한 점선. 데이터는
// web_report/tabs/distribution.py 가 이미 전량(다운샘플링 없음) 계산해 sheets["Distribution"]
// 로 내려준다. item 이 많을 때 WebGL 컨텍스트가 한꺼번에 수십 개 살아있으면 브라우저가
// 느려지거나 깨질 수 있어 IntersectionObserver 로 화면에 보이는 칸만 그리고, 화면 밖으로
// 나가면 일정 시간 후 Plotly.purge 로 해제한다.
const DIST_PALETTE = ["#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
  "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52"];
// DIST_PALETTE(10색) 을 넘는 source 폴백 색 — client/chart_colors.py
// generate_default_colors() 와 동일 공식(진한 원색 7 + 황금비 hue 분산 + S/V 순환)이라
// dist_colors 미지정 세션의 11번째+ 색이 Honey 기본 48색과 일치한다. i 상한 없음.
const DIST_GEN_DEEP_HUES = [0.60, 0.33, 0.92, 0.80, 0.00, 0.09, 0.50];
const DIST_GEN_S_CYCLE = [1.00, 0.82, 0.92];
const DIST_GEN_V_CYCLE = [0.92, 0.68, 0.80, 0.55];
function distHsvHex(h, s, v) {
  // Python colorsys.hsv_to_rgb 1:1 포팅 (int() 절사 포함 — hex 가 바이트 단위로 일치)
  h = ((h % 1.0) + 1.0) % 1.0;
  let r, g, b;
  const i = Math.trunc(h * 6.0) % 6, f = (h * 6.0) - Math.trunc(h * 6.0);
  const p = v * (1.0 - s), q = v * (1.0 - s * f), t = v * (1.0 - s * (1.0 - f));
  if (i === 0)      { r = v; g = t; b = p; }
  else if (i === 1) { r = q; g = v; b = p; }
  else if (i === 2) { r = p; g = v; b = t; }
  else if (i === 3) { r = p; g = q; b = v; }
  else if (i === 4) { r = t; g = p; b = v; }
  else              { r = v; g = p; b = q; }
  const hex = n => Math.trunc(n * 255).toString(16).toUpperCase().padStart(2, "0");
  return "#" + hex(r) + hex(g) + hex(b);
}
function distDefaultColor(i) {
  if (i < DIST_GEN_DEEP_HUES.length) return distHsvHex(DIST_GEN_DEEP_HUES[i], 1.0, 0.85);
  const j = i - DIST_GEN_DEEP_HUES.length;
  const h = (0.07 + (j + 1) * 0.61803398875) % 1.0;
  return distHsvHex(h, DIST_GEN_S_CYCLE[j % DIST_GEN_S_CYCLE.length],
                       DIST_GEN_V_CYCLE[j % DIST_GEN_V_CYCLE.length]);
}
let distDataCache = {};       // subject → {lower_limit, upper_limit, units, bySource:{source:{xs,ys}}} (ECDF, Issue Table 미니분포와 공용)
let distColorMap = {};        // source → color

// ── Distribution 산포 탭 (툴바/갤러리/상세) 상태·규격 ─────────────────────────
const DIST = { CPK_GOOD: 1.33, DOWNSAMPLE: 1500, PER_FRAME: 3,
  ROOT_MARGIN: "1200px 0px", EXCLUDE: ["chipid", "gpib", "otp", "code"] };
const DIST_STATUS_BG = { fail: "#FDECEC", cpk_low: "#FEF9E7", ok: "#FFFFFF" };  // 연빨강 / 연노랑 / 흰
const DIST_PLOT_BG = {
  paper_bgcolor: "#FFFFFF", plot_bgcolor: "#FFFFFF",
  font: { color: "#1A1A1F", size: 13, family: "Malgun Gothic, 'IBM Plex Sans KR', sans-serif" },
  hoverlabel: { bgcolor: "#FFFFFF", bordercolor: "#E7E7EA",
    font: { color: "#1A1A1F", size: 14, family: "JetBrains Mono, monospace" } },
  hovermode: "closest",
};
const DIST_CFG = { responsive: true, displaylogo: false, displayModeBar: false };
const DIST_CFG_STATIC = { responsive: true, displaylogo: false, displayModeBar: false, staticPlot: true };
let distIndex = [];            // DATA.web_report.distribution_index
// cpk<1.33 / Fail Only 는 독립 토글(동시 on 가능, 둘 다 AND). 둘 다 off 면 전체.
let distCpkOnly = true;        // 기본 진입 cpk<1.33 on
let distFailOnly = false;
let distLimitOnly = false;     // 켜면 각 분포 차트 x축을 [LSL,USL] 창으로 클램프(Limit 벗어난 산포 숨김)
// distLimitOnly 켜짐 + lo/hi 존재 시 x축 표시범위 [lo,hi](±2% pad). 아니면 null(기존 범위 유지).
function distLimitRange(lo, hi) {
  if (!distLimitOnly || lo == null || hi == null) return null;
  const span = (hi - lo) || Math.abs(hi) || 1;
  const pad = span * 0.02;
  return [lo - pad, hi + pad];
}
let distFiltered = [];         // 현재 필터 결과 (갤러리 표시/네비 범위)
let distSelected = new Set();  // 검색 체크박스로 고른 항목(있으면 갤러리를 이 항목들만으로 필터)
let distView = "gallery";      // "gallery" | "detail"
let distGalleryObserver = null;
let distRenderQueue = [];
let distRafScheduled = false;
let distNavList = [];          // 상세 <>/Alt 이동용 캡처 목록
let distNavPos = -1;
let distPanelBound = false;

// ── Distribution 지연 로드 (distribution_deferred 응답용) ─────────────────────
// /full 은 대용량 ECDF 를 내려주지 않고, 첫 페인트 후 백그라운드로
// GET .../web_report/distribution (컴팩트 columnar, 전 포인트) 을 받아 distDataCache 를
// 채운다. 도착 전에 그려진 미니셀/갤러리는 refreshDistConsumers 가 다시 채운다.
let distDataReady = false;     // distDataCache 사용 가능 여부 (구형 embed 응답이면 로드 직후 true)
let distDataPromise = null;    // 진행 중/완료된 fetch (중복 요청 방지)
let _distContentHash = "";     // 마지막 fetch 시점의 content_hash — 동일하면 재fetch 안 함

function buildDistDataFromCompact(payload) {
  // 컴팩트 columnar → 기존 distDataCache 스키마 그대로 (소비자 코드 무수정)
  const out = {};
  const items = (payload && payload.items) || {};
  Object.keys(items).forEach(subj => {
    const it = items[subj], bySource = {};
    Object.keys(it.sources || {}).forEach(src => {
      bySource[src] = { xs: it.sources[src].x || [], ys: it.sources[src].y || [] };
    });
    out[subj] = { lower_limit: it.lo, upper_limit: it.hi, units: it.units || "", bySource };
  });
  return out;
}

function fetchDistViaWorker(url) {
  // 수십 MB JSON 을 메인스레드에서 파싱하면 로드 직후 첫 상호작용이 얼어붙는다.
  // Web Worker 에서 fetch+parse 하고, 결과를 통째로 넘기면 structured clone
  // 역직렬화(~0.5s)가 다시 메인스레드를 막으므로 items 를 포인트 수 기준 청크로
  // 쪼개 전송해 블록을 수십 ms 단위로 분산한다. (Worker 실패 시 호출측 폴백.)
  return new Promise((resolve, reject) => {
    let blobUrl = null, w = null;
    const cleanup = () => {
      try { if (w) w.terminate(); } catch (e) {}
      try { if (blobUrl) URL.revokeObjectURL(blobUrl); } catch (e) {}
    };
    try {
      const src = 'self.onmessage=function(e){' +
        'fetch(e.data,{cache:"no-cache"})' +
        '.then(function(r){if(!r.ok)throw new Error("HTTP "+r.status);return r.json();})' +
        '.then(function(j){' +
          'var items=(j&&j.items)||{};var keys=Object.keys(items);' +
          'var batch={},pts=0;' +
          'for(var i=0;i<keys.length;i++){var k=keys[i];batch[k]=items[k];' +
            'var ss=items[k].sources||{};' +
            'for(var s in ss)pts+=((ss[s].x||[]).length);' +
            'if(pts>=250000){self.postMessage({chunk:batch});batch={};pts=0;}}' +
          'self.postMessage({chunk:batch,done:true,format:j&&j.format});' +
        '})' +
        '.catch(function(err){self.postMessage({error:String(err&&err.message||err)});});' +
        '};';
      blobUrl = URL.createObjectURL(new Blob([src], { type: "text/javascript" }));
      w = new Worker(blobUrl);
    } catch (e) { cleanup(); reject(e); return; }
    const items = {};
    w.onmessage = ev => {
      const d = ev.data || {};
      if (d.error) { cleanup(); reject(new Error(d.error)); return; }
      Object.assign(items, d.chunk || {});
      if (d.done) { cleanup(); resolve({ format: d.format, items }); }
    };
    w.onerror = () => { cleanup(); reject(new Error("worker failed")); };
    // blob URL Worker 의 상대경로 기준이 페이지와 달라질 수 있어 절대 URL 로 전달
    w.postMessage(new URL(url, location.origin).href);
  });
}

function ensureDistData() {
  const ch = (DATA && DATA.session && DATA.session.content_hash) || "";
  if (distDataPromise && ch === _distContentHash) return distDataPromise;   // 로딩 중/완료 재사용
  _distContentHash = ch;
  distDataReady = false;
  const url = `/pe/report/session/${SESSION_ID}/web_report/distribution`;
  distDataPromise = fetchDistViaWorker(url)
    .catch(() => fetch(url, { cache: "no-cache" })   // Worker 실패 시 메인스레드 폴백
      .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }))
    .then(j => {
      distDataCache = buildDistDataFromCompact(j);
      distDataReady = true;
      refreshDistConsumers();
    })
    .catch(e => {
      distDataPromise = null;   // 실패 시 다음 호출에서 재시도
      showToast("분포 데이터 로드 실패: " + e.message);
    });
  return distDataPromise;
}

// 데이터 도착 전에 만들어진 분포 소비처들을 다시 채운다.
function refreshDistConsumers() {
  // Issue Table 미니셀 — 화면에 보이는(관측 중) 셀만 rAF 큐로 재큐잉.
  // 전량 동기 렌더 금지 (수천 셀 × Plotly = 분 단위 freeze). 나머지는 스크롤 시 lazy.
  document.querySelectorAll('#panel-issues .dist-cell-mini[data-visible="1"]')
    .forEach(issueDistQueueRender);
  // Issue Table Bin 상세의 단일 분포 셀
  const detail = document.getElementById("issueDetailDistCell");
  if (detail && detail.dataset.distLoaded !== "1") {
    if (distDataCache[detail.dataset.subject]) renderDistCell(detail);
    else detail.outerHTML = `<div class="placeholder">분포 데이터 없음</div>`;
  }
  // Distribution 갤러리 — 화면에 보이는(관측 중) 카드 재큐잉
  document.querySelectorAll('#panel-distribution .distg-card[data-visible="1"]')
    .forEach(distQueueRender);
}

function buildDistColorMap(sources) {
  distColorMap = {};
  // F10 에서 지정한 색(웹리포트 payload.dist_colors)이 있으면 source 순서대로 사용,
  // 없으면(legacy) 기본 팔레트 자동 배정. 색 번호 i = source i.
  const custom = (DATA && DATA.web_report && Array.isArray(DATA.web_report.dist_colors))
    ? DATA.web_report.dist_colors : null;
  (sources || []).forEach((s, i) => {
    // 지정 색 없는 자리: 1~10번째는 기존 DIST_PALETTE 유지(legacy 무회귀),
    // 그 이후는 모듈로 순환(색 중복) 대신 48색 공식 연장으로 고유 색 보장.
    distColorMap[s.name] = (custom && custom[i]) ? custom[i]
      : (i < DIST_PALETTE.length ? DIST_PALETTE[i] : distDefaultColor(i));
  });
}

function distColorFor(source) { return distColorMap[source] || "#888"; }

// source 가 이 수 이상이면 상세 3개 차트(CDF/히스토그램/정규분포)의 내장 세로 legend 를
// 끄고 차트 위 공용 legend 스트립 1개로 대체 — 내장 legend 가 차트 폭을 잠식하고
// 같은 내용이 3중복되는 것을 방지. 미만이면 기존 내장 legend 그대로(무회귀).
const DIST_EXT_LEGEND_MIN = 8;
function distUseExtLegend(data) { return ((data && data.sources) || []).length >= DIST_EXT_LEGEND_MIN; }

function distFmtLimit(v) { return (v === null || v === undefined) ? "?" : String(v); }
// 단위를 대괄호로 감싼다 (예: uA → [uA]). 빈 단위는 "" 반환.
function distUnitBr(u) { u = (u === null || u === undefined) ? "" : String(u).trim(); return u ? "[" + esc(u) + "]" : ""; }

// Issue Table Bin 상세의 단일 분포 셀 — 산포탭 갤러리 카드와 동일한 포맷(표시용 다운샘플 static
// CDF + LSL/USL 점선 + status 배경). distDataCache 재사용(전체점은 데이터에 그대로 유지).
function renderDistCell(cell) {
  if (!cell || cell.dataset.distLoaded === "1") return;
  const subject = cell.dataset.subject;
  const info = distDataCache[subject];
  const div = cell.querySelector(".dist-plot");
  const placeholder = cell.querySelector(".dist-placeholder");
  if (!info || !div || typeof Plotly === "undefined") return;

  const idx = distIndex.find(x => x.subject === subject);
  const status = (idx && idx.status) || "ok";
  const lo = info.lower_limit, hi = info.upper_limit;
  const traces = Object.keys(info.bySource).map(source => {
    const ds = distDownsampleForDisplay(info.bySource[source].xs, info.bySource[source].ys);
    return { type: "scatter", mode: "markers", cliponaxis: false, name: source,
      x: ds.xs, y: ds.ys, marker: { color: distColorFor(source), size: 4 } };
  });
  const layout = { ...DIST_PLOT_BG, plot_bgcolor: DIST_STATUS_BG[status] || "#FFFFFF",
    title: {
      text: `<b>${esc(subject)}</b><br><span style="font-size:10px">` +
        `(${esc(distFmtLimit(lo))} ~ ${esc(distFmtLimit(hi))} ${distUnitBr(info.units)})</span>`,
      font: { size: 12 }, x: 0.5, xanchor: "center" },
    xaxis: { showgrid: true, gridcolor: "#eee", zeroline: false, ticks: "outside",
      tickcolor: "#bbb", tickfont: { size: 10 } },
    yaxis: { range: [0, 100], ticksuffix: "%", showgrid: true, gridcolor: "#eee",
      zeroline: false, tickfont: { size: 10 } },
    shapes: distSpecShapes(lo, hi, false).concat(beforeLimitShapes(subject)),
    annotations: distSpecAnnos(lo, hi, false),
    margin: { l: 46, r: 14, t: 46, b: 28 }, showlegend: false };

  Plotly.newPlot(div, traces, layout, DIST_CFG_STATIC);
  cell.dataset.distLoaded = "1";
  if (placeholder) placeholder.style.display = "none";
}

// ── 세그먼트 / 타입어헤드 필터 ─────────────────────────────────────────────────
function distApplySegment(rows) {
  let out = rows.slice();
  if (distCpkOnly) {
    out = out.filter(r => r.cpk != null && r.cpk < DIST.CPK_GOOD
      && !DIST.EXCLUDE.some(k => String(r.subject).toLowerCase().includes(k)));
  }
  if (distFailOnly) out = out.filter(r => r.is_fail);
  return out;
}
function distSuggestions(q) {
  // 단일어 부분일치(대소문자 무시). 세그먼트(cpk<1.33 등)에 걸리지 않게 전체 항목에서 검색해
  // 어떤 항목이든 체크박스로 고를 수 있게 한다.
  const term = String(q || "").trim().toLowerCase();
  if (!term) return [];
  const out = [];
  for (const r of distIndex) {
    if (String(r.subject).toLowerCase().includes(term)) {
      out.push(r);
      if (out.length >= 30) break;
    }
  }
  return out;
}

// ── 기준선(shapes) / 라벨(annotations): LSL·USL 세로 점선 + y=50% 가로 점선 ─────
function distSpecShapes(lo, hi, withMid) {
  const sh = [];
  [lo, hi].forEach(v => { if (v !== null && v !== undefined) sh.push({
    type: "line", x0: v, x1: v, yref: "paper", y0: 0, y1: 1,
    line: { color: "#DC2626", width: 1.2, dash: "dash" } }); });
  if (withMid) sh.push({ type: "line", xref: "paper", x0: 0, x1: 1, y0: 50, y1: 50,
    line: { color: "#888", width: 1, dash: "dashdot" } });
  return sh;
}
function distSpecAnnos(lo, hi, mini) {
  const fs = mini ? 9.2 : 11.5;
  const mk = (v, label) => ({ x: v, yref: "paper", y: 1, text: `${label} ${v}`,
    showarrow: false, textangle: -90, font: { size: fs, color: "#DC2626" },
    bgcolor: "rgba(255,255,255,.72)", borderpad: 1, xanchor: "left", yanchor: "top" });
  const a = [];
  if (hi !== null && hi !== undefined) a.push(mk(hi, "USL"));
  if (lo !== null && lo !== undefined) a.push(mk(lo, "LSL"));
  return a;
}

// ── Compare(goodlog) before-limit 회색 기준선: 이름 같고 limit 만 바뀐 항목의 before
//    limit 을 distribution 차트에 회색 점선으로 덧그림 (Honey Compare Mode 이식). ─────
function beforeLimitsFor(subject) {
  const wr = DATA.web_report;
  const gl = wr && wr.compare && wr.compare.goodlog;
  const m = gl && gl.limit_change_map;
  return (m && m[subject]) || null;   // [before_lo|null, before_hi|null]
}
function beforeLimitShapes(subject) {
  const bl = beforeLimitsFor(subject);
  if (!bl) return [];
  const sh = [];
  bl.forEach(v => { if (v !== null && v !== undefined) sh.push({
    type: "line", x0: v, x1: v, yref: "paper", y0: 0, y1: 1,
    line: { color: "#9ca3af", width: 1.2, dash: "dash" } }); });
  return sh;
}
function beforeLimitAnnos(subject) {
  const bl = beforeLimitsFor(subject);
  if (!bl) return [];
  const mk = (v, label) => ({ x: v, yref: "paper", y: 1, text: `${label} ${v}`,
    showarrow: false, textangle: -90, font: { size: 11.5, color: "#6b7280" },
    bgcolor: "rgba(255,255,255,.72)", borderpad: 1, xanchor: "left", yanchor: "top" });
  const a = [];
  if (bl[1] !== null && bl[1] !== undefined) a.push(mk(bl[1], "이전 USL"));
  if (bl[0] !== null && bl[0] !== undefined) a.push(mk(bl[0], "이전 LSL"));
  return a;
}
// x축 표시범위에 before limit 포함 (범위 밖이면 회색선이 안 보이는 것 방지).
function extendRangeForBeforeLimits(range, subject) {
  const bl = beforeLimitsFor(subject);
  if (!bl || !range) return range;
  let x0 = range[0], x1 = range[1];
  bl.forEach(v => { if (v !== null && v !== undefined) {
    if (v < x0) x0 = v; if (v > x1) x1 = v; } });
  return [x0, x1];
}

// ── 미니셀 표시용 다운샘플 (ECDF 전제: x 오름차순, y 누적%) ────────────────────
// 단순 stride 는 꼬리 outlier·고질량 계단(Δy 큰 점)·x축 고립점을 눈멀고 떨어뜨려
// 누적산포를 왜곡한다. 규칙: (1) 첫/마지막 + 누적% 상·하위 5% 전량 보존,
// (2) Δy ≥ 0.15%p(질량 큰 고유값)·x갭 ≥ range×0.5%(갭 양끝) 강제 보존,
// (3) 나머지만 잔여 budget 으로 균등 stride. 강제 보존 때문에 총점이 1500 을
// 다소 넘는 것은 허용(소프트 상한) — 왜곡 없음이 우선.
function distDownsampleForDisplay(xs, ys) {
  const n = xs.length;
  if (n <= DIST.DOWNSAMPLE) return { xs, ys };
  const keep = new Uint8Array(n);
  keep[0] = 1; keep[n - 1] = 1;
  const range = xs[n - 1] - xs[0];
  const gap = range > 0 ? range * 0.005 : Infinity;
  for (let i = 0; i < n; i++) {
    if (ys[i] <= 5 || ys[i] >= 95) { keep[i] = 1; continue; }             // 꼬리 전량
    if (ys[i] - ys[i - 1] >= 0.15) keep[i] = 1;                           // Δy 질량
    if (xs[i] - xs[i - 1] >= gap) { keep[i] = 1; keep[i - 1] = 1; }       // x갭 양끝
  }
  let kept = 0;
  for (let i = 0; i < n; i++) kept += keep[i];
  const rest = [];
  for (let i = 0; i < n; i++) if (!keep[i]) rest.push(i);
  // 꼬리에 고유값이 많아 budget 이 소진되어도 중간 몸통이 비지 않도록 최소 200점 보장
  const budget = Math.max(DIST.DOWNSAMPLE - kept, 200);
  if (budget > 0 && rest.length > budget) {
    const st = Math.ceil(rest.length / budget);
    for (let j = 0; j < rest.length; j += st) keep[rest[j]] = 1;
  } else if (budget > 0) {
    for (const i of rest) keep[i] = 1;
  }
  const ox = [], oy = [];
  for (let i = 0; i < n; i++) if (keep[i]) { ox.push(xs[i]); oy.push(ys[i]); }
  return { xs: ox, ys: oy };
}

// ── 갤러리 미니셀(정적 CDF, distDataCache 재사용, 표시용만 1500점 다운샘플) ─────
function distRenderGalleryCell(cell) {
  if (cell.dataset.rendered === "1") return;
  // 분포 데이터 도착 전 — rendered 플래그를 세우지 않고 리턴해야 도착 후
  // refreshDistConsumers 의 재큐잉으로 다시 그려진다 (빈 차트 고정 방지).
  if (!distDataReady) return;
  const subject = cell.dataset.subject;
  const status = cell.dataset.status || "ok";
  const info = distDataCache[subject];
  const plot = cell.querySelector(".distg-plot");
  if (!plot || typeof Plotly === "undefined") return;
  const lo = info ? info.lower_limit : null;
  const hi = info ? info.upper_limit : null;
  const traces = [];
  if (info) Object.keys(info.bySource).forEach(src => {
    // 미니셀 표시용만 다운샘플(통계·상세는 전체점) — 꼬리/계단/갭 보존 규칙 적용
    const ds = distDownsampleForDisplay(info.bySource[src].xs, info.bySource[src].ys);
    traces.push({ type: "scatter", mode: "markers", cliponaxis: false, x: ds.xs, y: ds.ys,
      marker: { color: distColorFor(src), size: 3 } });
  });
  // 선택 좌표(Map Analysis)가 있으면 이 항목 위치를 점+빨간 점선으로 오버레이.
  let shapes = distSpecShapes(lo, hi, false).concat(beforeLimitShapes(subject));
  const cm = chipMarkersFor(subject);
  if (cm) { traces.push(...cm.traces); shapes = shapes.concat(cm.shapes); }
  const glr = distLimitRange(lo, hi);
  const layout = { ...DIST_PLOT_BG, plot_bgcolor: DIST_STATUS_BG[status] || "#FFFFFF",
    xaxis: { showgrid: true, gridcolor: "#eee", zeroline: false, ticks: "outside",
      tickcolor: "#bbb", tickfont: { size: 9 }, ...(glr ? { range: glr, autorange: false } : {}) },
    yaxis: { range: [0, 100], ticksuffix: "%", showgrid: true, gridcolor: "#eee",
      zeroline: false, tickfont: { size: 9 } },
    shapes, annotations: distSpecAnnos(lo, hi, true),
    margin: { l: 34, r: 10, t: 8, b: 20 }, showlegend: false };
  Plotly.newPlot(plot, traces, layout, DIST_CFG_STATIC);
  cell.dataset.rendered = "1";
}
function distPurgeGalleryCell(cell) {
  if (cell.dataset.rendered !== "1") return;
  const plot = cell.querySelector(".distg-plot");
  try { if (plot && window.Plotly) Plotly.purge(plot); } catch (e) {}
  cell.dataset.rendered = "";
}

// ── rAF 분할 렌더(프레임당 PER_FRAME 개) ──────────────────────────────────────
function distQueueRender(cell) {
  if (cell.dataset.rendered === "1" || distRenderQueue.includes(cell)) return;
  distRenderQueue.push(cell);
  if (!distRafScheduled) { distRafScheduled = true; requestAnimationFrame(distFlushRender); }
}
function distFlushRender() {
  distRafScheduled = false;
  let n = 0;
  while (distRenderQueue.length && n < DIST.PER_FRAME) {
    const cell = distRenderQueue.shift();
    if (cell.isConnected && cell.dataset.visible === "1") { distRenderGalleryCell(cell); n++; }
  }
  if (distRenderQueue.length) { distRafScheduled = true; requestAnimationFrame(distFlushRender); }
}

// ── 툴바 + 갤러리 ─────────────────────────────────────────────────────────────
function distToolbarHtml() {
  // cpk<1.33 / Fail Only 독립 토글(둘 다 켜면 교집합). 둘 다 끄면 전체.
  const seg = (on, key, label) => `<button class="distseg${on ? " active" : ""}" data-seg="${key}">${esc(label)}</button>`;
  // 검색창 오른쪽 범례: 소스별 색상(distColorMap) 을 스와치로 표시(갤러리 미니셀 색과 동일).
  const sources = (DATA.web_report && DATA.web_report.sources) || [];
  const legend = sources.length ? `<div class="dist-legend">` + sources.map(s =>
    `<span class="dist-leg-item"><span class="dist-leg-sw" style="background:${distColorFor(s.name)}"></span>${esc(s.name)}</span>`
  ).join("") + `</div>` : "";
  // 검색 체크박스로 고른 항목이 있으면 개수+해제 버튼을 세그먼트 옆에 표시.
  const selChip = distSelected.size
    ? `<button class="distseg dist-sel-clear" data-seg="clearsel" title="선택 해제">선택 ${distSelected.size}개 ✕</button>` : "";
  return `<div class="dist-toolbar">
    <div class="distseg-group">${seg(distCpkOnly, "cpk", "cpk < 1.33")}${seg(distFailOnly, "fail", "Fail Only")}${seg(distLimitOnly, "limit", "Limit 안 Data만")}${selChip}</div>
    <div class="dist-search-wrap">
      <input id="distSearch" class="dist-search" type="text" autocomplete="off" placeholder="항목 검색 (체크로 선택)">
      <div id="distSuggest" class="dist-suggest" style="display:none"></div>
    </div>
    ${legend}
    <span class="dist-count"></span>
  </div>`;
}
function distUpdateCount() {
  const el = document.querySelector("#panel-distribution .dist-count");
  if (el) el.textContent = `${distFiltered.length} 개`;
}
function distRenderGallery() {
  const panel = document.getElementById("panel-distribution");
  distView = "gallery";
  if (distGalleryObserver) { try { distGalleryObserver.disconnect(); } catch (e) {} distGalleryObserver = null; }
  distRenderQueue = []; distRafScheduled = false;
  // 검색 체크박스 선택이 있으면 그 항목들만(세그먼트 무시), 없으면 세그먼트 필터.
  distFiltered = distSelected.size
    ? distIndex.filter(r => distSelected.has(r.subject))
    : distApplySegment(distIndex);
  const cards = distFiltered.map(r => {
    const cpk = r.cpk == null ? "-" : r.cpk;
    const lim = `${distFmtLimit(r.lower_limit)} ~ ${distFmtLimit(r.upper_limit)}${r.units ? " " + distUnitBr(r.units) : ""}`;
    // Comment(차트 하단 코멘트)가 있으면 카드에 노트 아이콘 배지. cnSavedFor 는 chart_notes.js(런타임 로드).
    const hasComment = typeof cnSavedFor === "function" && !!(cnSavedFor("cdf:" + r.subject) || {}).comment;
    const noteBadge = hasComment ? `<span class="distg-note" title="Comment 있음">📝</span>` : "";
    return `<div class="distg-card" data-subject="${esc(r.subject)}" data-status="${esc(r.status)}" style="background:${DIST_STATUS_BG[r.status] || "#fff"}">
      <div class="distg-head">
        <div class="distg-line1">
          <span class="distg-tno">${esc(r.test_num || "")}</span>
          <span class="distg-name" title="${esc(r.subject)}">${esc(r.subject)}</span>
          ${noteBadge}
        </div>
        <div class="distg-line2">
          <span class="distg-lim">${esc(lim)}</span>
          <span class="distg-cpk">cpk ${esc(cpk)}</span>
        </div>
      </div>
      <div class="distg-plot"></div>
    </div>`;
  }).join("");
  panel.innerHTML = distToolbarHtml() +
    (distFiltered.length ? `<div class="distg-grid">${cards}</div>`
                         : `<div class="placeholder">해당 조건의 항목이 없습니다</div>`);
  distUpdateCount();
  if (typeof Plotly === "undefined" || typeof IntersectionObserver === "undefined") return;
  distGalleryObserver = new IntersectionObserver(entries => {
    entries.forEach(en => {
      const cell = en.target;
      if (en.isIntersecting) { cell.dataset.visible = "1"; distQueueRender(cell); }
      else {
        cell.dataset.visible = "";
        distPurgeGalleryCell(cell);
        const i = distRenderQueue.indexOf(cell);
        if (i >= 0) distRenderQueue.splice(i, 1);
      }
    });
  }, { rootMargin: DIST.ROOT_MARGIN, threshold: 0 });
  panel.querySelectorAll(".distg-card").forEach(c => distGalleryObserver.observe(c));
}


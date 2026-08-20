// ── Compare 모드 (같은 Wafer 2~3 source 비교) ─────────────────────────────────
const COMPARE_SRC_PALETTE = ["#e11d48", "#2563eb", "#d97706"];   // source 별 색(빨강/파랑/…)
const CMP_MATCH_GREEN = "#16a34a";    // 모든 source 에서 Bin 동일 — 초록
const CMP_MIXED_COLOR = "#7c3aed";    // Bin 다르고 2개↑ source 에서 Fail — 보라(혼합)

function _cmpNum(v, digits) {
  if (v === null || v === undefined || v === "") return "–";
  if (typeof v !== "number") return esc(String(v));
  return Number.isInteger(v) ? String(v) : v.toFixed(digits == null ? 3 : digits);
}
// 서버가 이미 반올림한 값(average 4자리 / cpk·cp 3자리 / limit 6자리 — web_report/tabs/cpk.py
// `_stats_batch`)은 **그대로** 찍는다. 여기서 toFixed 를 한 번 더 걸면 이중 반올림이 되어
// 같은 값을 String(v) 로 찍는 CPK 탭(cpk.js)과 표시가 갈린다.
function _cmpServer(v) {
  if (v === null || v === undefined || v === "") return "–";
  return esc(String(v));
}
// stdev 는 서버가 유일하게 반올림하지 않고 내려보내는 값이라(Limit 역산이 원값에 의존)
// 표시할 때만 유효숫자를 맞춘다 — CPK 탭·Item_detail 과 같은 fmtStdev(core.js)를 쓴다.
function _cmpStdev(v) {
  if (v === null || v === undefined || v === "") return "–";
  return esc(fmtStdev(v));
}
function _cmpDeltaCell(v, digits, extraCls, title) {
  const tip = title ? ` title="${esc(title)}"` : "";
  if (v === null || v === undefined || v === "") return `<td class="num"${tip}>–</td>`;
  const cls = [(v > 0 ? "cmp-up" : (v < 0 ? "cmp-down" : "")), extraCls || ""]
    .filter(Boolean).join(" ");
  const s = v > 0 ? "+" + _cmpNum(v, digits) : _cmpNum(v, digits);
  return `<td class="num ${cls}"${tip}>${s}</td>`;
}

// 유의성 표시 — 서버가 준 p 가 alpha 이상이면 "통계적으로 노이즈와 구분 안 됨"(ns).
// p 는 억제 판단에만 쓰는 값이라(compare.py significance.py) 값 자체는 툴팁으로만 보여준다.
function _cmpIsNs(p, alpha) {
  return p !== null && p !== undefined && alpha != null && p >= alpha;
}
function _cmpPTip(label, p, alpha, na, nb) {
  if (p === null || p === undefined) return "";
  const ps = p === 0 ? "<0.000001" : String(p);
  const n = `n=${na == null ? "–" : na}/${nb == null ? "–" : nb}`;
  return `${label} p=${ps} · ${n}` +
    (_cmpIsNs(p, alpha) ? ` — 표본이 작아 노이즈와 구분되지 않음 (p≥${alpha}, ns)` : "");
}

// 공통성 Map(단일): 좌표별 Bin 일치=초록, 한 source 에서만 Fail=그 source 색,
// 둘 다 Fail(Bin 만 다름)=보라. waferHeatmap 을 재사용하되 bin 자리에 분류 라벨을 넣는다.
function _cmpClsLabel(cls, sources) {
  if (cls === "match") return "Bin 일치";
  if (cls === "mixed") return "혼합 · 2개↑ Fail";
  return `${cls} 에서만 Fail`;   // cls = source 이름
}
// source 이름 뒤에 Before/After 그룹을 괄호로 (groups 없으면 이름 그대로 — legacy payload).
function _cmpSrcLabel(src, groups) {
  const g = groups && groups[src];
  return g ? `${src} (${g === "after" ? "After" : "Before"})` : String(src);
}
// ── Compare 표 행 코멘트 (kind=compare_note) ────────────────────────────────
// 저장은 세션 편집 DB — POST .../web_report/compare_notes, 읽기는 DATA.compare_notes.
// **키는 고정 규약이다**(edits.py KIND_COMPARE_NOTE 주석이 정본, CLAUDE.md 5-12):
//   Log 비교 행           : "gl:" + after_item_name + U+001F + before_item_name
//   동일 좌표 Bin 비교 행 : "bm:<x>,<y>"
// 행 인덱스를 쓰면 필터/접기로 순서가 바뀌어 남의 행에 코멘트가 붙는다.
const CMP_NOTE_SEP = String.fromCharCode(31);
function glNoteKey(r) {
  return "gl:" + (r.after_item_name || "") + CMP_NOTE_SEP + (r.before_item_name || "");
}
function cmpNoteText(key) {
  const e = ((DATA && DATA.compare_notes) || {})[key];
  return (e && e.text) ? String(e.text) : "";
}
function cmpNoteCell(key) {
  const v = cmpNoteText(key);
  const tip = (MODE === "edit") ? "더블클릭하여 코멘트 입력" : "코멘트 (읽기 전용)";
  return `<td class="cmp-note-cell${v ? " has-note" : ""}" data-note-key="${esc(key)}"` +
    ` title="${esc(tip)}">${esc(v)}</td>`;
}
async function saveCompareNote(td, text, before) {
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/compare_notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ ops: [{ key: td.dataset.noteKey, value: text || null }] }),
      keepalive: true,   // 입력 직후 탭을 닫아도 요청이 취소되지 않게 (다른 편집 채널과 동일)
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    DATA.compare_notes = j.compare_notes || {};   // 서버 권위본으로 갱신
    td.textContent = text;
    td.classList.toggle("has-note", !!text);
  } catch (err) {
    td.textContent = before;                      // 실패 시 원복
    td.classList.toggle("has-note", !!before);
    showToast("Comment 저장 실패: " + err.message);
  }
}
// 위임 1회 바인딩 — 섹션 innerHTML 이 필터/페이지 전환으로 갈려도 리스너가 살아 있어야 한다.
// Issue Table 의 td.dblclick-edit 경로는 재사용하지 않는다(그 경로는 @멘션·linkify·
// issue_comment 저장까지 함께 태워 Compare 표에서 오작동한다).
function bindCompareNotes(panel) {
  if (panel.dataset.cmpNoteBound === "1") return;
  panel.dataset.cmpNoteBound = "1";
  panel.addEventListener("dblclick", e => {
    const td = e.target.closest("td.cmp-note-cell");
    if (!td || MODE !== "edit" || td.isContentEditable) return;
    td.dataset.before = td.textContent || "";
    td.contentEditable = "true";
    td.focus();
  });
  panel.addEventListener("keydown", e => {
    const td = e.target.closest && e.target.closest("td.cmp-note-cell");
    if (!td || !td.isContentEditable) return;
    if (e.key === "Escape") { td.textContent = td.dataset.before || ""; td.blur(); }
    else if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); td.blur(); }
  });
  // 엑셀·서식 있는 텍스트를 그대로 붙이면 노드가 섞여 저장(textContent)과 화면이 갈린다.
  panel.addEventListener("paste", e => {
    const td = e.target.closest("td.cmp-note-cell");
    if (!td || !td.isContentEditable) return;
    const cb = e.clipboardData || window.clipboardData;
    if (!cb) return;
    e.preventDefault();
    const text = (cb.getData("text/plain") || "").replace(/\s+/g, " ").trim();
    if (text) document.execCommand("insertText", false, text);
  });
  panel.addEventListener("focusout", e => {
    const td = e.target.closest ? e.target.closest("td.cmp-note-cell") : null;
    if (!td || !td.isContentEditable) return;
    td.contentEditable = "false";
    const text = (td.textContent || "").trim();
    const before = (td.dataset.before || "").trim();
    td.textContent = text;
    if (text === before) { td.classList.toggle("has-note", !!text); return; }
    saveCompareNote(td, text, before);
  });
}

function drawCompareCommonMap(cm, sources) {
  const div = document.getElementById("cmp-common-map");
  if (!div || !window.Plotly) return;
  if (cm.x_min == null) { div.innerHTML = '<div class="placeholder">좌표 데이터 없음</div>'; return; }
  const groups = cm.groups || {};
  const srcColor = {};
  sources.forEach((s, i) => { srcColor[s] = COMPARE_SRC_PALETTE[i % COMPARE_SRC_PALETTE.length]; });

  const matchLabel = "Bin 일치", mixedLabel = "혼합 · 2개↑ Fail";
  const colorMap = { [matchLabel]: CMP_MATCH_GREEN, [mixedLabel]: CMP_MIXED_COLOR };
  sources.forEach(s => { colorMap[_cmpClsLabel(s, sources)] = srcColor[s]; });
  const binOrder = [matchLabel, ...sources.map(s => _cmpClsLabel(s, sources)), mixedLabel];

  // bins(= source 별 BIN, sources 순서)를 die 에 실어 hover 에서 볼 수 있게 한다.
  const dies = (cm.dies || []).map(d => ({
    x: d.x, y: d.y, bin: _cmpClsLabel(d.cls, sources), bins: d.bins }));
  const m = { x_min: cm.x_min, x_max: cm.x_max, y_min: cm.y_min, y_max: cm.y_max, dies };
  // compact 격자로 그린다(메모리 span 무관 — 좌표 span 이 넓어도 OOM 방지. Map Detail 과 동일).
  // grid 모드에선 hovertemplate 이 무시되고 customdata 에 담긴 실좌표가 표시된다.
  const g = waferCompactGrid(m);
  // hover: 분류 + source 별 Bin 한 줄씩 ("WF3 (After): 1"). bins 가 없으면(legacy payload)
  // 종전처럼 분류만 보여준다.
  const labelOf = (d, cat) => {
    if (!d.bins) return cat;
    const lines = sources.map((s, i) => `${esc(_cmpSrcLabel(s, groups))}: ${esc(String(d.bins[i]))}`);
    return cat + "<br>" + lines.join("<br>");
  };
  const built = waferHeatmap(m, { colorMap, binOrder, grid: g, labelOf });
  if (!built) { div.innerHTML = '<div class="placeholder">공통 die 없음</div>'; return; }
  Plotly.newPlot(div, [built.trace], waferLayout(m, { grid: g }), { responsive: true, displayModeBar: false });

  // 범례 — 실제 등장하는 분류만 count 와 함께
  const legend = document.getElementById("cmp-common-legend");
  if (legend) {
    const c = cm.counts || {};
    const rows = [`<span class="cmp-lg"><i style="background:${CMP_MATCH_GREEN}"></i>Bin 일치 (${c.match || 0})</span>`];
    sources.forEach(s => {
      const n = (c.per_source && c.per_source[s]) || 0;
      rows.push(`<span class="cmp-lg"><i style="background:${srcColor[s]}"></i>${esc(_cmpSrcLabel(s, groups))} 에서만 Fail (${n})</span>`);
    });
    if (c.mixed) rows.push(`<span class="cmp-lg"><i style="background:${CMP_MIXED_COLOR}"></i>혼합 · 2개↑ Fail (${c.mixed})</span>`);
    legend.innerHTML = rows.join("");
  }
}

// 동일 좌표 Bin 비교표 (Map 바로 밑) — Bin 이 전 source 에서 같지는 않은 좌표를 1행씩.
// 컬럼은 Before 그룹 / After 그룹으로 묶는다. null/빈 rows 면 생략.
function compareBinMatrixHtml(bm) {
  if (!bm || !bm.rows) return "";
  const c = bm.counts || {};
  const before = bm.before_sources || [], after = bm.after_sources || [];
  const order = bm.sources || [];
  const binCell = b => {
    const s = String(b);
    return `<td class="num${s === "1" ? " cmp-pass-cell" : ""}">${esc(s)}</td>`;
  };
  const summary = `<div class="compare-summary">
      <span class="cmp-chip">공통 die ${c.common_dies || 0}</span>
      <span class="cmp-chip cmp-unique">Bin 불일치 ${c.mismatch || 0}</span>
      <span class="cmp-chip">Pass→Fail ${c.pass_to_fail || 0}</span>
      <span class="cmp-chip">Fail→Pass ${c.fail_to_pass || 0}</span>
      <span class="gl-sub">Pass→Fail / Fail→Pass 는 그룹 대표(${esc(bm.rep_before || "")} → ${esc(bm.rep_after || "")}) 기준</span>
    </div>`;
  if (!bm.rows.length) {
    return summary + `<div class="gl-identical">공통 좌표의 Bin 이 모든 source 에서 동일합니다.</div>`;
  }
  // 서버는 sources 순서(=업로드 순서, After 먼저)로 bins 를 담는다. 표는 Before→After 로
  // 묶어 보여주므로 표시 순서에 맞춰 인덱스를 다시 잡는다.
  const idxOf = {}; order.forEach((s, i) => { idxOf[s] = i; });
  const shown = before.concat(after);
  const grpHead = (before.length ? `<th colspan="${before.length}">Before</th>` : "") +
    (after.length ? `<th colspan="${after.length}">After</th>` : "");
  const head = `<thead>
      <tr><th class="num" rowspan="2">X</th><th class="num" rowspan="2">Y</th>${grpHead}
          <th rowspan="2">Comment</th></tr>
      <tr>${shown.map(s => `<th class="num">${esc(s)}</th>`).join("")}</tr></thead>`;
  const body = bm.rows.map(r =>
    `<tr><td class="num">${_cmpNum(r.x)}</td><td class="num">${_cmpNum(r.y)}</td>` +
    shown.map(s => binCell(r.bins[idxOf[s]])).join("") +
    cmpNoteCell("bm:" + r.x + "," + r.y) + `</tr>`).join("");
  return summary +
    `<div class="sheet-wrap cmp-scroll cmp-binmatrix-wrap"><table class="sheet-table compare-table">${head}<tbody>${body}</tbody></table></div>`;
}

// ── 산포 비교 (dist_shift) — Before 분모 정규화 지표 + 서버 focus 필터 ────────
// 표시 지표 4종(MeanShift σ / Cpk% / Stdev증가율 / Median Shift)·유의성 2종·focus 판정·
// 정렬(MeanShift σ 큰 순)은 전부 서버(compare.py build_dist_shift)가 정본이고, 여기서는
// 표시 + focus 플래그 필터 토글 + 페이지 넘김만 한다. (서버가 함께 내려주는 iqr_delta_pct·
// ks_d 는 2026-07-28 사용자 요청으로 **화면에서 뺐다** — 값 자체는 payload 에 남는다.)
// p 는 전용 컬럼 없이 해당 값 셀의 ns 마커와 툴팁으로만 보여준다(n 이 수천이면 p 컬럼은
// 0.000 벽이 되어 정보가 없다).
// 대상은 **그룹 pool(Bin1)** — 그룹이 1 source 씩이면 avg/stdev/cpk 가 CPK 탭 값과 같다.
// 그래서 avg/cpk 는 서버 반올림값을 그대로(_cmpServer), stdev 는 CPK 탭과 같은 유효숫자
// 포맷(_cmpStdev)으로 찍어 **두 탭의 표시가 문자 단위로 일치**하게 한다.
// 임계값은 dist.thresholds 만 참조(하드코딩 금지).
let cmpDistFocusOnly = true;   // 필터 기본 ON — 관심(focus) 항목만 표시
const CMP_DIST_PAGE_SIZE = 20; // 한 페이지 행 수 — Distribution 미니차트가 붙어 무한 스크롤이 무겁다
let cmpDistPage = 1;

// Distribution 열(미니 ECDF 카드) — Distribution 탭 갤러리 카드와 같은 규격을 1/2 크기로.
// 데이터·표시점 계산은 distribution.js 공용 경로 그대로 쓴다(distDataCache = 전체 die 기준,
// Issue Table 미니셀과 같은 캐시). 점은 canvas 오버레이(distPaintPoints) — 규칙 #5 준수.
function cmpDistRenderCell(cell) {
  if (!cell || cell.dataset.rendered === "1") return;
  const subject = cell.dataset.subject;
  const info = distDataCache[subject];
  // 아직 안 받은 항목은 배치로 요청만 하고 리턴 — 도착하면 refreshDistConsumers 가 다시 부른다.
  if (!info) { if (distHasData(subject)) distRequestSubject(subject, false); return; }
  const plot = cell.querySelector(".dist-plot");
  if (!plot || typeof Plotly === "undefined") return;
  const lo = info.lower_limit, hi = info.upper_limit;
  const pts = {};
  const srcNames = Object.keys(info.bySource);
  const cap = distCapFor(srcNames.length, DIST.CELL_BUDGET_MINI);
  srcNames.forEach(s => { pts[s] = distDisplayPoints(info.bySource[s], cap); });
  const traces = [];
  const sentinel = distSentinelTrace(pts);
  if (sentinel) traces.push(sentinel);
  const layout = { ...DIST_PLOT_BG,
    xaxis: { showgrid: true, gridcolor: "#eee", zeroline: false, ticks: "outside",
      tickcolor: "#bbb", tickfont: { size: 8 } },
    yaxis: { range: [0, 100], ticksuffix: "%", showgrid: true, gridcolor: "#eee",
      zeroline: false, tickfont: { size: 8 } },
    shapes: distSpecShapes(lo, hi, false).concat(beforeLimitShapes(subject)),
    annotations: distSpecAnnos(lo, hi, true),
    margin: { l: 30, r: 8, t: 6, b: 18 }, showlegend: false };
  Plotly.newPlot(plot, traces, layout, DIST_CFG_STATIC);
  distPaintPoints(plot, pts, null);
  cell.dataset.rendered = "1";
}

// 보이는 셀만 rAF 로 나눠 그린다(한 페이지 20칸이라 IntersectionObserver + 프레임당 2칸).
let cmpDistObserver = null;
let cmpDistQueue = [];
let cmpDistRaf = false;
function cmpDistQueueRender(cell) {
  if (cell.dataset.rendered === "1" || cmpDistQueue.includes(cell)) return;
  cmpDistQueue.push(cell);
  if (!cmpDistRaf) { cmpDistRaf = true; requestAnimationFrame(cmpDistFlushRender); }
}
function cmpDistFlushRender() {
  cmpDistRaf = false;
  let n = 0;
  while (cmpDistQueue.length && n < 2) {
    const cell = cmpDistQueue.shift();
    if (cell.isConnected && cell.dataset.visible === "1") { cmpDistRenderCell(cell); n++; }
  }
  if (cmpDistQueue.length) { cmpDistRaf = true; requestAnimationFrame(cmpDistFlushRender); }
}
// 페이지 전환 전 정리 — innerHTML 교체로 DOM 만 지우면 Plotly 인스턴스·캔버스 옵저버가 남는다.
function cmpDistPurgeCells(sec) {
  cmpDistQueue = [];
  if (cmpDistObserver) { try { cmpDistObserver.disconnect(); } catch (e) {} cmpDistObserver = null; }
  sec.querySelectorAll('.cmp-dist-cell[data-rendered="1"]').forEach(cell => {
    const plot = cell.querySelector(".dist-plot");
    try { if (plot && window.Plotly) { distClearPoints(plot); Plotly.purge(plot); } } catch (e) {}
  });
}
function cmpDistObserveCells(sec) {
  if (typeof IntersectionObserver === "undefined") return;
  cmpDistObserver = new IntersectionObserver(entries => {
    entries.forEach(en => {
      const cell = en.target;
      if (en.isIntersecting) { cell.dataset.visible = "1"; cmpDistQueueRender(cell); }
      else cell.dataset.visible = "";
    });
  }, { rootMargin: "600px 0px", threshold: 0 });
  sec.querySelectorAll(".cmp-dist-cell").forEach(c => cmpDistObserver.observe(c));
}

function cmpDistSectionHtml(dist) {
  if (!dist) return '<div class="placeholder">산포 비교 데이터 없음</div>';
  // legacy payload(스키마 v16 이전, list 형) — 새 지표가 없어 기본 통계만 표시.
  const legacy = Array.isArray(dist);
  if (legacy) dist = { rows: dist, after: "", before: "", thresholds: {} };
  const rows = dist.rows || [];
  if (!rows.length) return '<div class="placeholder">공통 항목 없음</div>';
  const th = dist.thresholds || {};
  const shown = (!legacy && cmpDistFocusOnly) ? rows.filter(r => r.focus) : rows;
  // 소스 색 범례 — Distribution 열의 ECDF 색이 어느 source 인지 알아야 한다(Distribution 탭과 같은 색).
  const srcs = (DATA.web_report && DATA.web_report.sources) || [];
  const grp = (DATA.web_report && DATA.web_report.compare && DATA.web_report.compare.groups) || {};
  // srcs 는 {name, file_name} **객체** 배열이다(metrics.py). 종전엔 객체를 그대로
  // distColorFor/_cmpSrcLabel 에 넘겨 색이 전부 회색이고 이름이 [object Object] 로
  // 찍혔다 — 범례가 없는 것과 같았다(2026-08-20 수정).
  const legendHtml = (typeof distColorFor === "function" && srcs.length)
    ? `<span class="cmp-dist-legend">${srcs.map(s =>
        `<span class="cmp-lg" title="${esc(s.file_name || s.name)}"><i style="background:${distColorFor(s.name)}"></i>${esc(_cmpSrcLabel(s.name, grp))}</span>`).join("")}</span>`
    : "";
  const toolbar = legacy ? "" : `<div class="compare-summary">
      <button type="button" id="cmpDistFocusBtn" class="btn-sm cmp-fbtn${cmpDistFocusOnly ? " active" : ""}"
        title="클릭 전환 — 관심 항목만 ↔ ALL(전체)">${cmpDistFocusOnly ? "관심 항목만" : "ALL"}</button>
      <span class="cmp-chip">${shown.length}/${rows.length} 항목</span>
      ${legendHtml}
      <span class="gl-sub">관심 판정(서버): 한쪽 Cpk&lt;${th.cpk_low} 또는 |Stdev증가율|≥${th.stdev_delta_pct}%(단 유의할 때 — p&lt;${th.alpha}) · 양쪽 Cpk&gt;${th.cpk_high}(여유 과대)·양쪽 고정값은 항상 제외. <b>ns</b>=표본이 작아 노이즈와 구분 안 되는 값(셀에 마우스를 올리면 p·n)</span>
    </div>`;
  if (!shown.length) {
    return toolbar +
      `<div class="placeholder">관심 항목 없음 — ALL 로 전환하면 전체 ${rows.length}개 항목을 표시합니다</div>`;
  }
  // 페이지 분할(20행) — Distribution 미니차트가 행마다 붙어 전량 렌더는 무겁다.
  const pages = Math.max(1, Math.ceil(shown.length / CMP_DIST_PAGE_SIZE));
  if (cmpDistPage > pages) cmpDistPage = pages;
  if (cmpDistPage < 1) cmpDistPage = 1;
  const start = (cmpDistPage - 1) * CMP_DIST_PAGE_SIZE;
  const pageRows = shown.slice(start, start + CMP_DIST_PAGE_SIZE);
  const pager = pages <= 1 ? "" : `<div class="cmp-pager">
      <button type="button" class="btn-sm cmp-page-prev"${cmpDistPage <= 1 ? " disabled" : ""}>‹ 이전</button>
      <span class="cmp-page-info">${start + 1}–${start + pageRows.length} / ${shown.length} 항목 · ${cmpDistPage} / ${pages} 페이지</span>
      <button type="button" class="btn-sm cmp-page-next"${cmpDistPage >= pages ? " disabled" : ""}>다음 ›</button>
    </div>`;
  const metricHead = legacy ? "" :
    `<th class="num" rowspan="2" title="|Avg(A)−Avg(B)| / Stdev(B) — 평균 이동을 σ 단위로 정규화">MeanShift(σ)</th>` +
    `<th class="num" rowspan="2" title="Cpk(A) / Cpk(B) × 100 — 100% 미만이면 악화">Cpk(%)</th>` +
    `<th class="num" rowspan="2" title="(Stdev(A)−Stdev(B)) / Stdev(B) × 100 — 양수면 After 산포 증가">Stdev증가율(%)</th>` +
    `<th class="num" rowspan="2" title="|Median(A)−Median(B)| / IQR(B) — outlier 에 강건한 이동량">Median Shift</th>` +
    `<th rowspan="2" title="Distribution 탭 카드와 같은 ECDF(전체 die 기준) — 빨간 점선 LSL/USL, 색은 위 범례의 source 색">Distribution</th>`;
  const head = `<thead>
      <tr><th rowspan="2">Item</th><th rowspan="2">Unit</th>
          <th colspan="3">Before — ${esc(dist.before || "")}</th>
          <th colspan="3">After — ${esc(dist.after || "")}</th>${metricHead}</tr>
      <tr><th class="num">Avg</th><th class="num">Stdev</th><th class="num">Cpk</th>
          <th class="num">Avg</th><th class="num">Stdev</th><th class="num">Cpk</th></tr></thead>`;
  const cpkCell = v =>
    `<td class="num${v != null && th.cpk_low != null && v < th.cpk_low ? " cpk-warn" : ""}">${_cmpServer(v)}</td>`;
  const ratioCell = v => {   // cmp-up/cmp-down 은 색 재사용(빨강=악화/파랑=개선)
    if (v === null || v === undefined) return `<td class="num">–</td>`;
    const cls = v < 100 ? " cmp-up" : (v > 100 ? " cmp-down" : "");
    return `<td class="num${cls}">${_cmpNum(v, 1)}</td>`;
  };
  // Distribution 미니 카드 셀 — ECDF 가 없는 항목(측정 data 전무)은 빈 칸으로 둔다.
  const distCell = subject => (typeof distHasData === "function" && distHasData(subject))
    ? `<td class="cmp-dist-td"><div class="cmp-dist-cell" data-subject="${esc(subject)}"><div class="dist-plot"></div></div></td>`
    : `<td class="cmp-dist-td"><span class="gl-sub">–</span></td>`;
  const body = pageRows.map(r => {
    const a = r.after || {}, b = r.before || {};
    const nTip = (a.n != null || b.n != null)
      ? ` title="After n=${a.n == null ? "–" : a.n} · Before n=${b.n == null ? "–" : b.n}"` : "";
    // Before → After 순 (헤더와 같은 순서 — 2026-08-20 Compare 탭 전체 통일).
    const base = `<tr><td${nTip}>${esc(r.subject)}</td><td>${esc(r.units || "")}</td>` +
      `<td class="num">${_cmpServer(b.average)}</td><td class="num">${_cmpStdev(b.stdev)}</td>${cpkCell(b.cpk)}` +
      `<td class="num">${_cmpServer(a.average)}</td><td class="num">${_cmpStdev(a.stdev)}</td>${cpkCell(a.cpk)}`;
    if (legacy) return base + `</tr>`;
    const sdRed = (r.stdev_delta_pct != null && th.stdev_delta_pct != null &&
                   Math.abs(r.stdev_delta_pct) >= th.stdev_delta_pct) ? "gl-gap-red" : "";
    const sdNs = _cmpIsNs(r.p_stdev, th.alpha), msNs = _cmpIsNs(r.p_mean, th.alpha);
    const msTip = _cmpPTip("평균차", r.p_mean, th.alpha, a.n, b.n);
    return base +
      `<td class="num${msNs ? " cmp-ns" : ""}"${msTip ? ` title="${esc(msTip)}"` : ""}>` +
      `${_cmpNum(r.meanshift_sigma)}</td>` + ratioCell(r.cpk_ratio_pct) +
      _cmpDeltaCell(r.stdev_delta_pct, 2, [sdRed, sdNs ? "cmp-ns" : ""].filter(Boolean).join(" "),
                    _cmpPTip("산포차", r.p_stdev, th.alpha, a.n, b.n)) +
      `<td class="num">${_cmpNum(r.median_shift)}</td>` + distCell(r.subject) + `</tr>`;
  }).join("");
  return toolbar + pager +
    `<div class="sheet-wrap cmp-scroll"><table class="sheet-table compare-table cmp-dist-table">${head}<tbody>${body}</tbody></table></div>` +
    pager;
}

// 산포 비교 섹션만 부분 재렌더 — renderCompare 전체 재호출은 Plotly Map 재그리기·goodlog
// 리바인딩을 유발하므로 필터/페이지 전환 시엔 이 섹션 innerHTML 만 교체한다.
function renderCmpDistSection(panel) {
  const sec = panel.querySelector("#cmp-dist-section");
  if (!sec) return;
  cmpDistPurgeCells(sec);
  const cmp = DATA.web_report && DATA.web_report.compare;
  sec.innerHTML = cmpDistSectionHtml(cmp && cmp.dist_shift);
  cmpDistObserveCells(sec);
}

// ── 동일성 검증 — 항목별 Grade 판정 (Before pool vs After pool) ─────────────
// Grade1: AVG차(%) ≤ 5 / Grade2: 5 초과 & 양쪽 CPK ≥ 5 / Grade3: 그 외(판정 불가 포함).
// 판정 규칙·임계값은 서버(compare.py build_equivalence)가 정본이고 여기서는 표시만 한다.
function compareEquivHtml(eq) {
  if (!eq || !eq.rows) {
    return '<div class="placeholder">동일성 검증 데이터 없음</div>';
  }
  const th = eq.thresholds || {};
  const pctLimit = th.avg_pct == null ? 5 : th.avg_pct;
  const cpkLimit = th.cpk == null ? 5 : th.cpk;
  const s = eq.summary || {};
  const pair = `${esc(eq.before || "")} vs ${esc(eq.after || "")}`;
  const summary = `<div class="sheet-wrap eq-summary"><table class="sheet-table compare-table">
      <thead><tr><th>구분</th><th class="num">TestITEM</th>
        <th class="num">Grade1</th><th class="num">Grade2</th><th class="num">Grade3</th></tr></thead>
      <tbody><tr><td>${pair}</td><td class="num">${s.total || 0}</td>
        <td class="num">${s.grade1 || 0}</td><td class="num">${s.grade2 || 0}</td>
        <td class="num">${s.grade3 || 0}</td></tr></tbody></table></div>`;
  if (!eq.rows.length) {
    return summary + '<div class="placeholder">공통 항목 없음</div>';
  }

  const cpkCell = v => {
    const bad = (typeof v === "number") && v < cpkLimit;
    return `<td class="num${bad ? " eq-bad" : ""}">${_cmpServer(v)}</td>`;
  };
  const pctCell = v => {
    if (v === null || v === undefined) return `<td class="num">–</td>`;
    return `<td class="num${v > pctLimit ? " eq-bad" : ""}">${_cmpNum(v, 2)}</td>`;
  };
  const head = `<thead>
      <tr><th rowspan="2">STEP</th><th rowspan="2">Item</th><th rowspan="2">UNIT</th>
          <th class="num" rowspan="2">HiLIM</th><th class="num" rowspan="2">LoLIM</th>
          <th colspan="3">Before — ${esc(eq.before || "")}</th>
          <th colspan="3">After — ${esc(eq.after || "")}</th>
          <th class="num" rowspan="2">AVG차<span class="eq-formula">|After − Before|</span></th>
          <th class="num" rowspan="2">AVG차(%)<span class="eq-formula">|After − Before| / |Before| × 100</span></th>
          <th rowspan="2">동일성</th></tr>
      <tr><th class="num">AVG</th><th class="num">STD</th><th class="num">CPK</th>
          <th class="num">AVG</th><th class="num">STD</th><th class="num">CPK</th></tr></thead>`;
  const body = eq.rows.map(r => {
    const b = r.before || {}, a = r.after || {};
    const g3 = r.grade === 3;
    return `<tr><td>${esc(r.step || "")}</td><td>${esc(r.subject)}</td><td>${esc(r.units || "")}</td>` +
      `<td class="num">${_cmpServer(r.hilim)}</td><td class="num">${_cmpServer(r.lolim)}</td>` +
      `<td class="num">${_cmpServer(b.average)}</td><td class="num">${_cmpStdev(b.stdev)}</td>${cpkCell(b.cpk)}` +
      `<td class="num">${_cmpServer(a.average)}</td><td class="num">${_cmpStdev(a.stdev)}</td>${cpkCell(a.cpk)}` +
      `<td class="num">${_cmpNum(r.delta_avg)}</td>` + pctCell(r.delta_pct) +
      `<td class="eq-grade${g3 ? " eq-grade3" : ""}">Grade ${r.grade}</td></tr>`;
  }).join("");
  const legend = `<div class="compare-summary eq-legend">
      <span class="cmp-chip">Grade1 · AVG차(%) ${pctLimit} 이하</span>
      <span class="cmp-chip">Grade2 · AVG차(%) ${pctLimit} 초과 &amp; 양쪽 CPK ${cpkLimit} 이상</span>
      <span class="cmp-chip cmp-unique">Grade3 · 그 외</span></div>`;
  return summary + legend +
    `<div class="sheet-wrap cmp-scroll"><table class="sheet-table compare-table">${head}<tbody>${body}</tbody></table></div>`;
}

// 표시용 source 순서 — Before 그룹 먼저, 그 다음 After (2026-08-20 Compare 탭 통일).
// 공통성 Map 은 **여기를 쓰지 않는다**: 그 색이 업로드 순서 인덱스(COMPARE_SRC_PALETTE[i])
// 에 묶여 있어 순서를 바꾸면 Honey 배치 창·Distribution 탭과 색 의미가 어긋난다.
function cmpOrderedSources(cmp) {
  const b = cmp.before_sources || [], a = cmp.after_sources || [];
  return (b.length || a.length) ? b.concat(a) : (cmp.sources || []);
}

function compareBinTableHtml(binDelta, sources, groups) {
  if (!binDelta || !binDelta.length) return '<div class="placeholder">Bin 데이터 없음</div>';
  const srcHead = sources.map(s =>
    `<th colspan="2">${esc(s)}<div class="gl-sub">${esc(((groups || {})[s] === "after") ? "After" : ((groups || {})[s] === "before" ? "Before" : ""))}</div></th>`).join("");
  const subHead = sources.map(() => `<th class="num">Cnt</th><th class="num">%</th>`).join("");
  const head = `<thead>
      <tr><th rowspan="2">Bin</th>${srcHead}
          <th class="num" rowspan="2">Δ%</th><th class="num" rowspan="2">편차%</th></tr>
      <tr>${subHead}</tr></thead>`;
  const body = binDelta.map(r => {
    const perSrc = {};
    (r.sources || []).forEach(s => { perSrc[s.source] = s; });
    const cells = sources.map(s => {
      const d = perSrc[s] || {};
      return `<td class="num">${_cmpNum(d.count)}</td><td class="num">${_cmpNum(d.pct, 2)}</td>`;
    }).join("");
    const binLabel = r.is_pass ? `${esc(String(r.bin))} (Pass)` : esc(String(r.bin));
    return `<tr class="${r.is_pass ? "cmp-pass-row" : ""}"><td>${binLabel}</td>${cells}` +
      _cmpDeltaCell(r.delta_pct, 2) +
      `<td class="num">${_cmpNum(r.range_pct, 2)}</td></tr>`;
  }).join("");
  return `<div class="sheet-wrap"><table class="sheet-table compare-table">${head}<tbody>${body}</tbody></table></div>`;
}

// ── goodlog(테스트 프로그램 diff) — Honey Compare Mode 이식. after/before 두 파일의
//    항목명/limit 일치 여부(True 초록/False 빨강) + reference die 값 gap%(|gap|≥10% 빨강).
//    **limit 이 하나도 안 바뀌어도 표 전체를 그린다** — Gap% 를 보는 화면이기도 하기 때문
//    (2026-07-28, 서버 build_goodlog 이 identical 이어도 rows 를 채운다).
//    이상(항목 추가/제거·limit 변경)만 상단 요약 + 항상 표시하고, 나머지 정상 행은
//    git-diff 식으로 접어둔다(초기 접힘, '전체 펼치기' 토글). ──
// 행 분류: added(after 만)·removed(before 만)·limitchg(양쪽 존재 & limit 불일치)·normal.
// Comment 열은 서버 payload 의 r.comment(항상 "")가 아니라 **세션 편집 DB**(compare_note)
// 가 진실이다 — cmpNoteCell/glNoteKey 참조.
// goodlog 15컬럼 기본 폭(px) — colgroup 순서 = **화면 표시 순서**로, 2026-08-20 부터
// Before 가 왼쪽이다(사용자 요청 — 시간순으로 읽힌다).
// [before Item/Lo/Hi/Unit/Value, compare Item/Lo/Hi, Comment, Gap%, after Item/Lo/Hi/Unit/Value]
// 서버 GOODLOG_HEADER(compare.py)·payload 키는 after 먼저 그대로다 — 표시 순서만 바꾼다.
const GOODLOG_COLW = [130, 76, 76, 44, 84, 58, 58, 58, 160, 58, 130, 76, 76, 44, 84];

// Gap% 강조/필터 임계값 — 셀 빨강과 'Gap 큰 항목만' 버튼이 같은 값을 쓴다.
const GL_GAP_LIMIT = 10;
// 표시 필터 2종(독립 토글, 둘 다 켜면 AND). 필터가 걸리면 '변화 없음' 접기 없이 평평하게 그린다.
let glDiffOnly = false;   // Item/LoLim/HiLim 비교가 False 인 행(+항목 추가·제거)만
let glGapOnly = false;    // |Gap%| ≥ GL_GAP_LIMIT 인 행만
function glFilterOn() { return glDiffOnly || glGapOnly; }
function glRowPass(r, t) {
  if (glDiffOnly && t === "normal") return false;
  if (glGapOnly && !(r.gap !== null && r.gap !== undefined && Math.abs(r.gap) >= GL_GAP_LIMIT)) return false;
  return true;
}

function goodlogRowType(r) {
  const aHas = (r.after_item_name || "") !== "";
  const bHas = (r.before_item_name || "") !== "";
  if (aHas && !bHas) return "added";
  if (!aHas && bHas) return "removed";
  if (aHas && bHas && (r.compare_item_name === false ||
      r.compare_lolimit === false || r.compare_hilimit === false)) return "limitchg";
  return "normal";
}

function goodlogSectionHtml(gl) {
  if (!gl) return "";   // legacy(3-source 등) 세션 — 섹션 생략
  const title = `<h3 class="compare-h">테스트 프로그램 비교 (goodlog) — ` +
    `after: ${esc(gl.after_source || "")} / before: ${esc(gl.before_source || "")}</h3>`;
  // 구 payload(스키마 v19 이전)는 identical 이면 rows 가 비어 있다 — 그때만 종전 안내를 낸다.
  if (gl.identical && !(gl.rows || []).length) {
    return title + `<div class="gl-identical">두 파일의 테스트 프로그램(항목/limit)이 동일합니다.</div>`;
  }
  // 결측(None)은 공백 — Honey goodlog 시트의 _disp 관례와 동일 (한쪽만 존재하는 행 포함).
  const glNum = v => (v === null || v === undefined) ? "" : _cmpNum(v);
  const boolCell = v => v === true ? `<td class="gl-true">True</td>`
    : (v === false ? `<td class="gl-false">False</td>` : `<td></td>`);
  const gapCell = v => {
    if (v === null || v === undefined) return `<td class="num"></td>`;
    return `<td class="num${Math.abs(v) >= GL_GAP_LIMIT ? " gl-gap-red" : ""}">${_cmpNum(v, 2)}</td>`;
  };
  const COLS = 15;
  const rows = gl.rows || [];
  const types = rows.map(goodlogRowType);

  // ── 이상 요약(항목 추가/제거·limit 변경) ──
  const added = [], removed = [], limitchg = [];
  rows.forEach((r, i) => {
    if (types[i] === "added") added.push(r.after_item_name);
    else if (types[i] === "removed") removed.push(r.before_item_name);
    else if (types[i] === "limitchg") limitchg.push(r.after_item_name || r.before_item_name);
  });
  const abnCount = added.length + removed.length + limitchg.length;
  const nameChips = (arr, cls) => arr.map(n => `<span class="gl-ab-item ${cls}">${esc(n)}</span>`).join("");
  let summary;
  if (abnCount === 0) {
    summary = `<div class="gl-ab-summary gl-ab-none">항목/Limit 차이 없음 — 아래 표에서 항목별 Gap % 를 확인하세요</div>`;
  } else {
    // 이상 항목 목록(칩)은 개수가 많으면 표를 밀어내므로 헤드 버튼으로 따로 접었다 편다.
    summary = `<div class="gl-ab-summary">
      <div class="gl-ab-head">
        <button class="gl-ab-toggle" type="button"><span class="gl-fold-arrow">▾</span> 이상 항목</button>
        <span class="cmp-chip gl-chip-add">추가 ${added.length}</span>
        <span class="cmp-chip gl-chip-del">제거 ${removed.length}</span>
        <span class="cmp-chip gl-chip-lim">Limit 변경 ${limitchg.length}</span></div>
      <div class="gl-ab-items">${nameChips(added, "gl-add")}${nameChips(removed, "gl-del")}${nameChips(limitchg, "gl-lim")}</div>
    </div>`;
  }

  // colgroup 기본 폭(px) — 사용자 드래그 리사이즈의 시작값. Item 은 기본이 너무 넓다는
  // 요청으로 좁게 잡고, 넘치는 이름은 ellipsis + title 툴팁으로 본다(table-layout:fixed).
  const colgroup = `<colgroup>${GOODLOG_COLW.map(w => `<col style="width:${w}px">`).join("")}</colgroup>`;
  const rz = i => `<span class="col-resize-handle" data-col="${i}"></span>`;
  const head = colgroup + `<thead>
      <tr><th colspan="5">Before — ${esc(gl.before_source || "")}</th><th colspan="3">Compare</th>
          <th rowspan="2">Comment${rz(8)}</th><th class="num" rowspan="2">Gap %${rz(9)}</th>
          <th colspan="5">After — ${esc(gl.after_source || "")}</th></tr>
      <tr><th>Item${rz(0)}</th><th class="num">LoLim${rz(1)}</th><th class="num">HiLim${rz(2)}</th><th>Unit${rz(3)}</th><th class="num">Value${rz(4)}</th>
          <th>Item${rz(5)}</th><th>LoLim${rz(6)}</th><th>HiLim${rz(7)}</th>
          <th>Item${rz(10)}</th><th class="num">LoLim${rz(11)}</th><th class="num">HiLim${rz(12)}</th><th>Unit${rz(13)}</th><th class="num">Value${rz(14)}</th></tr></thead>`;
  // Compare 열이 False 면 **그 값이 든 Before/After 셀**도 빨갛게 칠한다 — True/False 만
  // 보고 어느 숫자가 달라졌는지 눈으로 되짚어야 했다(2026-08-20 요청).
  const mm = v => (v === false) ? " gl-mismatch" : "";
  // 폭 고정(fixed layout)이라 긴 Item 명은 잘린다 — title 로 전체 이름을 볼 수 있게 한다.
  const nameCell = (v, cls) => `<td class="${cls}" title="${esc(v || "")}">${esc(v || "")}</td>`;
  const limCell = (v, cls) => `<td class="num${cls}">${glNum(v)}</td>`;
  const rowHtml = (r, t) => {
    const mName = mm(r.compare_item_name), mLo = mm(r.compare_lolimit), mHi = mm(r.compare_hilimit);
    return `<tr class="gl-row gl-${t}">` +
      nameCell(r.before_item_name, mName.trim()) + limCell(r.before_lolimit, mLo) +
      limCell(r.before_hilimit, mHi) + `<td>${esc(r.before_unit || "")}</td>` +
      `<td class="num">${esc(r.before_value || "")}</td>` +
      boolCell(r.compare_item_name) + boolCell(r.compare_lolimit) + boolCell(r.compare_hilimit) +
      cmpNoteCell(glNoteKey(r)) + gapCell(r.gap) +
      nameCell(r.after_item_name, mName.trim()) + limCell(r.after_lolimit, mLo) +
      limCell(r.after_hilimit, mHi) + `<td>${esc(r.after_unit || "")}</td>` +
      `<td class="num">${esc(r.after_value || "")}</td></tr>`;
  };

  // ── 표시 필터 툴바 (버튼 라벨=기능, active=적용 중) ──
  const nShown = rows.reduce((n, r, k) => n + (glRowPass(r, types[k]) ? 1 : 0), 0);
  const filterBar = `<div class="compare-summary gl-filterbar">
      <button type="button" class="btn-sm cmp-fbtn gl-fbtn-diff${glDiffOnly ? " active" : ""}"
        title="Item/LoLim/HiLim 비교가 False 인 행(항목 추가·제거 포함)만 표시">Item·Limit 차이만</button>
      <button type="button" class="btn-sm cmp-fbtn gl-fbtn-gap${glGapOnly ? " active" : ""}"
        title="|Gap %| 가 ${GL_GAP_LIMIT} 이상인 행만 표시">Gap ≥ ${GL_GAP_LIMIT}% 만</button>
      <span class="cmp-chip">${nShown}/${rows.length} 항목</span>
      ${glFilterOn() ? `<span class="gl-sub">필터 적용 중 — '변화 없음' 접기는 해제됩니다</span>` : ""}
    </div>`;

  // ── 본문: 필터가 걸리면 평평한 목록, 아니면 git-diff 식 세그먼트
  //    (normal run 은 접힘 tbody, 이상 행은 항상 노출) ──
  const parts = [];
  if (glFilterOn()) {
    const buf = [];
    rows.forEach((r, k) => { if (glRowPass(r, types[k])) buf.push(rowHtml(r, types[k])); });
    parts.push(buf.length
      ? `<tbody>${buf.join("")}</tbody>`
      : `<tbody><tr><td colspan="${COLS}" class="gl-fold-empty">조건에 맞는 항목이 없습니다</td></tr></tbody>`);
  } else {
    let i = 0;
    while (i < rows.length) {
      if (types[i] === "normal") {
        let j = i; const buf = [];
        while (j < rows.length && types[j] === "normal") { buf.push(rowHtml(rows[j], "normal")); j++; }
        parts.push(
          `<tbody class="gl-fold-summary"><tr class="gl-fold-toggle"><td colspan="${COLS}">` +
          `<span class="gl-fold-arrow">▸</span> 변화 없음 ${buf.length}개</td></tr></tbody>` +
          `<tbody class="gl-fold" hidden>${buf.join("")}</tbody>`);
        i = j;
      } else {
        let j = i; const buf = [];
        while (j < rows.length && types[j] !== "normal") { buf.push(rowHtml(rows[j], types[j])); j++; }
        parts.push(`<tbody>${buf.join("")}</tbody>`);
        i = j;
      }
    }
  }
  // 상단 프록시 가로 스크롤바 — 표가 길어 하단까지 내려가지 않아도 좌우로 스크롤할 수 있게
  // (Issue Table 의 .issue-hscroll 과 동일 패턴, scrollLeft 만 양방향 동기화).
  return title + summary + filterBar +
    `<div class="gl-hscroll"><div class="gl-hscroll-spacer"></div></div>` +
    `<div class="sheet-wrap gl-wrap"><table class="sheet-table compare-table goodlog-table">${head}${parts.join("")}</table></div>`;
}

// Log 비교 섹션만 부분 재렌더 (필터 토글) — 산포 비교의 renderCmpDistSection 과 같은 관례.
// 접기·리사이즈·프록시 스크롤바는 섹션 안 요소에 직접 바인딩돼 있어 교체 후 다시 건다
// ('전체 펼치기' 는 섹션 밖 툴바라 renderCompare 에서 1회만 바인딩 — 중복 방지).
function renderGoodlogSection(panel) {
  const sec = panel.querySelector("#cmp-log-section");
  if (!sec) return;
  const cmp = DATA.web_report && DATA.web_report.compare;
  sec.innerHTML = goodlogSectionHtml(cmp && cmp.goodlog) ||
    '<div class="placeholder">테스트 프로그램 비교(goodlog) 데이터 없음</div>';
  bindGoodlogFolding(panel);
  bindGoodlogColResize(panel);
  bindGoodlogHscroll(panel);
  // 표를 다시 그리면 접힘 세그먼트도 초기(접힘) 상태로 돌아간다 — 버튼 라벨을 맞춰준다.
  const expandBtn = panel.querySelector(".gl-expand-all");
  if (expandBtn) expandBtn.textContent = "전체 펼치기";
  glSyncExpandBtn(panel);
}

// '전체 펼치기' 는 goodlog 표의 접힘 세그먼트 전용 — Log 서브탭이 활성이고 필터가 꺼진
// (=접힘 세그먼트가 존재하는) 동안에만 노출한다.
function glSyncExpandBtn(panel) {
  const btn = panel.querySelector(".gl-expand-all");
  if (!btn) return;
  const logActive = !!panel.querySelector('.cmp-subpanel[data-cmppanel="log"].active');
  btn.hidden = !logActive || glFilterOn();
}

// ── goodlog 표: 폭 동기화 / 프록시 가로 스크롤바 / 컬럼 드래그 리사이즈 ──────────
// table-layout:fixed 는 표 전체 width 를 따로 잡아줘야 컬럼 폭 합이 그대로 반영된다.
function glSyncTableWidth(table) {
  const cg = table.querySelector("colgroup");
  if (!cg) return;
  let sum = 0;
  Array.from(cg.children).forEach(c => { sum += parseFloat(c.style.width) || 0; });
  if (sum > 0) table.style.width = sum + "px";
}
function syncGoodlogHscroll(panel) {
  const wrap = panel.querySelector(".sheet-wrap.gl-wrap");
  const spacer = panel.querySelector(".gl-hscroll-spacer");
  if (!wrap || !spacer) return;
  spacer.style.width = wrap.scrollWidth + "px";
}
function bindGoodlogHscroll(panel) {
  const wrap = panel.querySelector(".sheet-wrap.gl-wrap");
  const hs = panel.querySelector(".gl-hscroll");
  if (!wrap || !hs) return;
  syncGoodlogHscroll(panel);
  let syncing = false;
  hs.addEventListener("scroll", () => {
    if (syncing) return;
    syncing = true; wrap.scrollLeft = hs.scrollLeft; syncing = false;
  });
  wrap.addEventListener("scroll", () => {
    if (syncing) return;
    syncing = true; hs.scrollLeft = wrap.scrollLeft; syncing = false;
  });
}
// 헤더 우측 경계 핸들(.col-resize-handle, data-col=컬럼인덱스)을 끌어 <col> width 를 바꾼다.
// 저장 없음(새로고침 시 기본 폭 복귀) — Issue Table 의 bindIssueColResize 와 같은 관례.
function bindGoodlogColResize(panel) {
  const table = panel.querySelector(".goodlog-table");
  const cg = table && table.querySelector("colgroup");
  if (!table || !cg) return;
  glSyncTableWidth(table);
  const MIN_W = 30;
  table.addEventListener("mousedown", e => {
    const handle = e.target.closest(".col-resize-handle");
    if (!handle) return;
    const col = cg.children[+handle.dataset.col];
    if (!col) return;
    const th = handle.closest("th");
    const startW = th ? th.getBoundingClientRect().width : parseFloat(col.style.width) || 80;
    const startX = e.clientX;
    e.preventDefault();   // 드래그 중 텍스트 선택 방지
    const onMove = ev => {
      col.style.width = Math.max(MIN_W, Math.round(startW + (ev.clientX - startX))) + "px";
      glSyncTableWidth(table);
      syncGoodlogHscroll(panel);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      syncGoodlogHscroll(panel);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// goodlog 접기/펼치기 바인딩 — renderCompare / renderGoodlogSection 이 innerHTML 갱신 후 호출
// (섹션 안 요소에 직접 바인딩이라 표를 다시 그릴 때마다 필요).
function bindGoodlogFolding(panel) {
  const setLabel = (toggleRow, shown) => {
    const arrow = toggleRow.querySelector(".gl-fold-arrow");
    if (arrow) arrow.textContent = shown ? "▾" : "▸";
  };
  panel.querySelectorAll(".gl-fold-toggle").forEach(row => {
    row.addEventListener("click", () => {
      const fold = row.closest(".gl-fold-summary").nextElementSibling;
      if (!fold || !fold.classList.contains("gl-fold")) return;
      const show = fold.hasAttribute("hidden");
      if (show) fold.removeAttribute("hidden"); else fold.setAttribute("hidden", "");
      setLabel(row, show);
    });
  });
  // 이상 항목 칩 목록만 따로 접기 (표의 '변화 없음' 접기와 독립).
  panel.querySelectorAll(".gl-ab-toggle").forEach(tbtn => {
    tbtn.addEventListener("click", () => {
      const items = tbtn.closest(".gl-ab-summary").querySelector(".gl-ab-items");
      if (!items) return;
      const show = items.hasAttribute("hidden");
      if (show) items.removeAttribute("hidden"); else items.setAttribute("hidden", "");
      const arrow = tbtn.querySelector(".gl-fold-arrow");
      if (arrow) arrow.textContent = show ? "▾" : "▸";
    });
  });
}

// '전체 펼치기' 버튼은 섹션 밖(sticky 툴바)이라 표를 다시 그려도 살아 있다 —
// renderCompare 에서 1회만 바인딩한다(bindGoodlogFolding 과 함께 걸면 리스너가 중복된다).
function bindGoodlogExpandAll(panel) {
  const btn = panel.querySelector(".gl-expand-all");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const anyHidden = !!panel.querySelector(".gl-fold[hidden]");
    panel.querySelectorAll(".gl-fold").forEach(f => {
      if (anyHidden) f.removeAttribute("hidden"); else f.setAttribute("hidden", "");
    });
    panel.querySelectorAll(".gl-fold-toggle").forEach(row => {
      const arrow = row.querySelector(".gl-fold-arrow");
      if (arrow) arrow.textContent = anyHidden ? "▾" : "▸";
    });
    btn.textContent = anyHidden ? "전체 접기" : "전체 펼치기";
  });
}

// 서브탭 전환 — Compare 패널 안에서 Map/Log/산포 비교/동일성 하위 화면을 토글한다.
// (Bin 비교는 서브탭 없이 Map 패널 하단에 함께 표시한다.)
function bindCompareSubtabs(panel) {
  const bar = panel.querySelector(".cmp-subtabs");
  if (!bar) return;
  bar.addEventListener("click", e => {
    const btn = e.target.closest("[data-cmpsub]");
    if (!btn) return;
    const key = btn.dataset.cmpsub;
    bar.querySelectorAll("[data-cmpsub]").forEach(b => b.classList.toggle("active", b === btn));
    panel.querySelectorAll(".cmp-subpanel").forEach(p =>
      p.classList.toggle("active", p.dataset.cmppanel === key));
    // '전체 펼치기' 는 goodlog 표의 접힘 세그먼트 전용 — Log 화면 + 필터 꺼짐일 때만 노출.
    glSyncExpandBtn(panel);
    // 숨김(0px) 상태에서 그려진 Plotly 맵은 보일 때 리사이즈해야 폭이 복구된다.
    const active = panel.querySelector(`.cmp-subpanel[data-cmppanel="${key}"]`);
    if (active && window.Plotly) {
      active.querySelectorAll(".js-plotly-plot").forEach(d => { try { Plotly.Plots.resize(d); } catch (e) {} });
    }
    // 숨김 상태에선 scrollWidth 가 0 이라 보일 때 프록시 스크롤바 폭을 다시 실측.
    if (key === "log") syncGoodlogHscroll(panel);
  });
}

function renderCompare() {
  const panel = document.getElementById("panel-compare");
  const cmp = DATA.web_report && DATA.web_report.compare;
  if (!cmp) {
    // 계산 대기(2026-08-19)와 진짜 없음을 구분한다 — 둘 다 "데이터 없음"으로 보이면
    // 잠시 뒤 채워질 것을 사용자가 오류로 읽는다. boot.js 폴링이 완료 시 다시 그린다.
    if (DATA.web_report && DATA.web_report.compare_pending) {
      emptyPanel(panel, window.__aiPendingFailed
        ? "Compare 계산 미완료 — 새로고침하면 다시 시도합니다."
        : "⏳ Compare 계산 중… 끝나면 자동으로 표시됩니다.");
      return;
    }
    emptyPanel(panel, "Compare 데이터 없음 (같은 Wafer source 2개 이상 필요)");
    return;
  }
  panel.classList.add("viz-root");
  const sources = cmp.sources || [];
  const groups = cmp.groups || {};
  const cm = cmp.common_map || {};
  const c = cm.counts || {};
  const mismatch = Math.max(0, (c.common_dies || 0) - (c.match || 0));
  const eq = cmp.equivalence;
  const groupChips = (cmp.before_sources || []).length
    ? `<span class="cmp-chip">Before ${(cmp.before_sources || []).map(esc).join(" · ")}</span>` +
      `<span class="cmp-chip">After ${(cmp.after_sources || []).map(esc).join(" · ")}</span>`
    : "";

  panel.innerHTML =
    `<div class="compare-wrap">
      <div class="compare-summary">
        <span class="mk">Sources</span> ${sources.map(esc).join(" · ")}
        ${groupChips}
        <span class="cmp-chip">공통 die ${c.common_dies || 0}</span>
        <span class="cmp-chip cmp-common">Bin 일치 ${c.match || 0}</span>
        <span class="cmp-chip cmp-unique">Bin 불일치 ${mismatch}</span>
      </div>
      <div class="cmp-toolbar">
        <div class="cmp-subtabs distseg-group">
          <button class="distseg active" data-cmpsub="map">Map 비교</button>
          <button class="distseg" data-cmpsub="log">Log 비교</button>
          <button class="distseg" data-cmpsub="dist">산포 비교</button>
          <button class="distseg" data-cmpsub="ttime">Test Time 비교</button>
          <button class="distseg" data-cmpsub="equiv">동일성 검증</button>
        </div>
        ${(cmp.goodlog && (cmp.goodlog.rows || []).length)
            ? `<button class="btn-sm gl-expand-all" type="button" hidden>전체 펼치기</button>` : ""}
      </div>
      <div class="cmp-subpanel active" data-cmppanel="map">
        <h3 class="compare-h">공통성 Map — Bin 일치=초록 / 한쪽만 Fail=source 색 / 2개↑ Fail=보라
          <span class="gl-sub">(die 에 마우스를 올리면 source 별 Bin 이 보입니다)</span></h3>
        <div class="wafer-card">
          <div id="cmp-common-map" style="width:100%;height:520px;"></div>
          <div id="cmp-common-legend" class="cmp-legend"></div>
        </div>
        <div class="cmp-bin-grid">
          <section>
            <h3 class="compare-h">동일 좌표 Bin 비교 (불일치 die)</h3>
            ${compareBinMatrixHtml(cmp.bin_matrix) || '<div class="placeholder">Bin 비교 데이터 없음</div>'}
          </section>
          <section>
            <h3 class="compare-h">Bin Yield 비교</h3>
            ${compareBinTableHtml(cmp.bin_delta, cmpOrderedSources(cmp), groups)}
          </section>
        </div>
      </div>
      <div class="cmp-subpanel" data-cmppanel="log">
        <div id="cmp-log-section"></div>
      </div>
      <div class="cmp-subpanel" data-cmppanel="dist">
        <h3 class="compare-h">산포 비교 (공통 항목 · MeanShift σ 큰 순 · 그룹 전체 die · Bin1 기준)</h3>
        <div id="cmp-dist-section"></div>
      </div>
      <div class="cmp-subpanel" data-cmppanel="ttime">
        <h3 class="compare-h">Test Time 비교</h3>
        <div class="placeholder">Test Time 데이터가 아직 없습니다.<br>
          업로드 입력 계약(7-meta honeyform)에 시간 컬럼이 없고 STDF 는 서버가 파싱하지
          않습니다 — 클라이언트가 Test Time 을 실어 보내기 시작하면 이 화면에 표시됩니다.</div>
      </div>
      <div class="cmp-subpanel" data-cmppanel="equiv">
        <h3 class="compare-h">동일성 검증 (Before vs After · 그룹 전체 die 기준)</h3>
        ${compareEquivHtml(eq)}
      </div>
    </div>`;

  drawCompareCommonMap(cm, sources);
  cmpDistPage = 1;
  renderCmpDistSection(panel);
  renderGoodlogSection(panel);
  // 필터/페이지 토글 — wrapper 에 위임 1회 바인딩(innerHTML 교체에도 리스너 유지).
  const distSec = panel.querySelector("#cmp-dist-section");
  if (distSec) distSec.addEventListener("click", e => {
    if (e.target.closest("#cmpDistFocusBtn")) { cmpDistFocusOnly = !cmpDistFocusOnly; cmpDistPage = 1; }
    else if (e.target.closest(".cmp-page-prev")) { if (cmpDistPage <= 1) return; cmpDistPage--; }
    else if (e.target.closest(".cmp-page-next")) cmpDistPage++;
    else return;
    renderCmpDistSection(panel);
  });
  const logSec = panel.querySelector("#cmp-log-section");
  if (logSec) logSec.addEventListener("click", e => {
    if (e.target.closest(".gl-fbtn-diff")) glDiffOnly = !glDiffOnly;
    else if (e.target.closest(".gl-fbtn-gap")) glGapOnly = !glGapOnly;
    else return;
    renderGoodlogSection(panel);
  });
  bindGoodlogExpandAll(panel);
  bindCompareSubtabs(panel);
  bindCompareNotes(panel);
  syncCompareToolbarH(panel);
}

// sticky 툴바 실제 높이 → 그 아래 붙는 프록시 가로 스크롤바의 top 오프셋(--cmp-toolbar-h).
function syncCompareToolbarH(panel) {
  panel = panel || document.getElementById("panel-compare");
  if (!panel) return;
  const bar = panel.querySelector(".cmp-toolbar");
  if (bar && bar.offsetHeight) panel.style.setProperty("--cmp-toolbar-h", bar.offsetHeight + "px");
}
window.addEventListener("resize", () => {
  const panel = document.getElementById("panel-compare");
  if (!panel) return;
  syncCompareToolbarH(panel);
  syncGoodlogHscroll(panel);
});


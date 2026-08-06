// ── Issue Table Map 미니셀 (해당 Bin 만 하이라이트한 웨이퍼) ────────────────────
// 분포 미니셀과 동일하게 수천 개라, 같은 IntersectionObserver + rAF 분할로 보이는 셀만
// lazy 렌더하고 화면 밖은 purge. 색상/범례는 세션 공통(globalBinColorMap) 을 dim 처리해 재사용.
// 렌더는 Map Analysis 갤러리와 동일한 canvas(drawWaferThumb) — Plotly 미사용(대량 die freeze 방지).
// 색상: 선택 bin 원색, 나머지 dim(#d9d9d9), 앞 step fail die(d.g) 는 회색.
function issueMapRgbFor(dim) {
  return (d, cache) => {
    if (d.g) return MAP_GRAY_RGB;
    const hex = dim[d.bin] || PASS_COLOR;
    let rgb = cache[hex];
    if (!rgb) { rgb = hexToRgb(hex); cache[hex] = rgb; }
    return rgb;
  };
}
// CPK 행 Map 셀: Bin 이 없으므로 그 Item 의 STDF Map(값 10분위) 썸네일을 그린다.
// Bin 미니맵과 달리 항목마다 scatter(die 전량 값) 응답이 필요해 셀 하나당 요청 1건이다.
// 보이는 셀이 수십 개일 수 있으므로 동시 요청에 상한을 두고, 상한에 걸린 셀은 loaded 를
// 세우지 않은 채 두었다가 진행 중 요청이 끝날 때 다시 큐에 올린다(rAF 스핀 없음).
const STDF_MINI_MAX_INFLIGHT = 2;
// subject 별 연속 실패 횟수. 실패를 기억하지 않으면 finally 의 재큐잉과 맞물려
// 같은 subject 를 화면에 떠 있는 내내 무한 재요청한다(사용자에겐 빈 셀만 보인 채
// 서버는 계속 두들겨 맞는다). 이 횟수를 넘기면 포기하고 셀에 실패를 표시한다.
const STDF_MINI_MAX_TRIES = 2;
const _stdfMiniFails = {};
let _stdfMiniInflight = 0;
function renderMiniStdfCell(cell) {
  const div = cell.querySelector(".map-plot");
  const subject = cell.dataset.subject;
  if (!div || !subject) return;
  if (_stdfScatterCache[subject]) {   // 캐시 적중 — 상한과 무관하게 즉시 그림
    const cached = _stdfScatterCache[subject];
    cell.dataset.mapLoaded = "1";
    stdfDrawThumb(div, cached, stdfThumbDefaultSource(cached));
    return;
  }
  if ((_stdfMiniFails[subject] || 0) >= STDF_MINI_MAX_TRIES) {
    cell.dataset.mapLoaded = "1";   // 재큐잉 대상에서 제외 — 새로고침으로만 재시도
    div.innerHTML = `<div class="placeholder" style="font-size:11px">로드 실패</div>`;
    return;
  }
  if (_stdfMiniInflight >= STDF_MINI_MAX_INFLIGHT) return;   // 아래 finally 가 재큐잉
  cell.dataset.mapLoaded = "1";   // 중복 fetch 방지 — 실패 시 해제해 재시도 가능하게
  _stdfMiniInflight++;
  stdfFetchScatter(subject).then(data => {
    delete _stdfMiniFails[subject];
    if (!cell.isConnected || cell.dataset.mapLoaded !== "1") return;   // 그 사이 purge 됨
    stdfDrawThumb(div, data, stdfThumbDefaultSource(data));
  }).catch(() => {
    _stdfMiniFails[subject] = (_stdfMiniFails[subject] || 0) + 1;
    cell.dataset.mapLoaded = "";
  }).finally(() => {
    _stdfMiniInflight--;
    issuePanelsQueryAll('.map-cell-mini[data-subject][data-visible="1"]')
      .forEach(issueMapQueueRender);
  });
}

// Issue Table Temp 행 Map 셀: "이 항목을 RT Limit 기준으로 벗어난 die" 를 강조한 미니맵.
// die 인덱스는 서버 temp_map(GET .../web_report/temp_map) 이 주고, dies 배열과 같은 순서라
// drawWaferThumb 콜백의 3번째 인자(k)로 바로 매칭한다.
// fail die 색은 **온도 조건**으로 가른다 — CT(저온) fail = 파랑, HT(고온) fail = 빨강
// (사용자 요청 2026-08-06). corner 를 못 찾는 소스는 종전대로 빨강.
const TEMP_MINI_FAIL_CT = "#2a78d6";
const TEMP_MINI_FAIL_HT = "#dc2626";
function tempMiniFailColor(source) {
  return tempCornerOf(source) === "CT" ? TEMP_MINI_FAIL_CT : TEMP_MINI_FAIL_HT;
}
// 항목 fail die(hit) 는 그 소스의 온도 색, 나머지는 dim, 앞 step fail(d.g)은 회색.
// 미니셀과 ⤢ 확장이 같은 색 규칙을 쓴다 (issueMapRgbFor 와 같은 패턴).
function tempMapRgbFor(hit, failHex) {
  const fail = failHex || TEMP_MINI_FAIL_HT;
  return (d, cache, k) => {
    if (d.g) return MAP_GRAY_RGB;
    const hex = hit.has(k) ? fail : MAP_BIN_DIM_COLOR;
    let rgb = cache[hex];
    if (!rgb) { rgb = hexToRgb(hex); cache[hex] = rgb; }
    return rgb;
  };
}
function renderMiniTempCell(cell) {
  const div = cell.querySelector(".map-plot");
  const item = cell.dataset.tempItem;
  if (!div || !item) return;
  const maps = (webReportSheets() || {})["Map Analysis"];
  if (!Array.isArray(maps) || !maps.length) { div.innerHTML = ""; cell.dataset.mapLoaded = "1"; return; }
  if (!tempMapReady) { ensureTempMapData(); return; }   // 도착 후 refreshMapConsumers 가 재큐잉
  // 인덱스가 있는 첫 CT/HT 소스의 맵 1장(⤢ 로 전 소스 보기). CT/HT 는 온도 조건이
  // 서로 달라 "어느 소스인지" 가 의미를 가지므로 title 에 소스명·die 수를 적는다.
  const entries = tempMapItemEntries(item);
  const first = entries[0];
  const m = first ? maps.find(mm => mm.source === first.source) : null;
  if (!m) { div.innerHTML = ""; cell.dataset.mapLoaded = "1"; return; }
  if (!Array.isArray(m.dies)) { ensureMapData(); return; }
  const hit = new Set(first.idx);
  const corner = tempCornerOf(first.source);
  cell.title = `${first.source}${corner ? ` (${corner})` : ""} — 이 항목 fail ${first.idx.length} die` +
    (entries.length > 1 ? ` (외 ${entries.length - 1}개 소스는 ⤢)` : "") +
    "\nfail die 색: CT = 파랑 · HT = 빨강" +
    "\n클릭하면 Map Analysis 탭 Temperature Map 축에서 이 항목을 강조해 봅니다";
  let canvas = div.querySelector("canvas.wafer-thumb");
  if (!canvas) {
    div.innerHTML = "";
    canvas = document.createElement("canvas");
    canvas.className = "wafer-thumb";
    div.appendChild(canvas);
  }
  drawWaferThumb(canvas, m, tempMapRgbFor(hit, tempMiniFailColor(first.source)));
  cell.dataset.mapLoaded = "1";
}

function renderMiniMapCell(cell) {
  if (cell.dataset.mapLoaded === "1") return;
  if (cell.dataset.tempItem) { renderMiniTempCell(cell); return; }
  if (cell.dataset.subject) { renderMiniStdfCell(cell); return; }
  const div = cell.querySelector(".map-plot");
  if (!div) return;
  const maps = (webReportSheets() || {})["Map Analysis"];
  if (!Array.isArray(maps) || !maps.length) { div.innerHTML = ""; cell.dataset.mapLoaded = "1"; return; }
  const bin = cell.dataset.bin;
  const binOrder = buildGlobalBinLegend(maps).map(r => r.bin);
  const dim = dimColorMap(globalBinColorMap(), binOrder, bin);   // 선택 bin만 원색, 나머지 회색
  // 접힘(기본): 첫 소스 1개. 펼치기 버튼으로 전 소스 보기.
  // step 분리 맵이면 해당 bin 의 fail 이 실제로 등장하는 step 맵을 우선 선택
  // (fail 은 자기 fail step 맵에만 그려지므로 maps[0](첫 step)엔 없을 수 있음).
  let m = maps[0];
  if (m.step != null) {
    m = maps.find(mm => (mm.bin_counts || []).some(bc => String(bc.bin) === String(bin))) || maps[0];
  }
  // dies 지연 로드 중 — mapLoaded 를 세우지 않고 skip (빈 셀 고착 방지),
  // 도착하면 refreshMapConsumers(wafer_charts.js) 가 보이는 셀을 재큐잉한다.
  if (!Array.isArray(m.dies)) { ensureMapData(); return; }
  let canvas = div.querySelector("canvas.wafer-thumb");
  if (!canvas) { div.innerHTML = ""; canvas = document.createElement("canvas"); canvas.className = "wafer-thumb"; div.appendChild(canvas); }
  drawWaferThumb(canvas, m, issueMapRgbFor(dim));
  cell.dataset.mapLoaded = "1";
}

// ⚠️ 상태는 **패널별** (item_detail.js _issueDistState 와 같은 이유 — 두 Issue 패널).
const _issueMapState = new Map();   // panelId → {observer, queue, raf}
function issueMapStateOf(panel) {
  const id = (panel && panel.id) || ISSUE_PANEL_MAIN;
  let st = _issueMapState.get(id);
  if (!st) { st = { observer: null, queue: [], raf: false }; _issueMapState.set(id, st); }
  return st;
}
function issueMapQueueRender(cell) {
  const st = issueMapStateOf(issuePanelOf(cell));
  if (cell.dataset.mapLoaded === "1" || st.queue.includes(cell)) return;
  st.queue.push(cell);
  if (!st.raf) { st.raf = true; requestAnimationFrame(() => issueMapFlush(st)); }
}
function issueMapFlush(st) {
  st.raf = false;
  let n = 0;
  while (st.queue.length && n < DIST.PER_FRAME) {
    const cell = st.queue.shift();
    if (cell.isConnected && cell.dataset.visible === "1") { renderMiniMapCell(cell); n++; }
  }
  if (st.queue.length) { st.raf = true; requestAnimationFrame(() => issueMapFlush(st)); }
}
function issueMapPurge(cell) {
  if (cell.dataset.mapLoaded !== "1") return;
  const div = cell.querySelector(".map-plot");
  if (div) div.innerHTML = "";   // canvas 제거해 화면 밖 메모리 반환
  cell.dataset.mapLoaded = "";
}
function renderIssueMiniMap(panel) {
  const st = issueMapStateOf(panel);
  if (st.observer) { try { st.observer.disconnect(); } catch (e) {} st.observer = null; }
  st.queue = []; st.raf = false;
  const cells = panel.querySelectorAll(".map-cell-mini");
  if (!cells.length) return;
  if (typeof IntersectionObserver === "undefined") {
    // IO 폴백은 visible 플래그가 없어 refreshMapConsumers 재큐잉을 못 받으므로
    // dies 도착을 기다렸다가 전량 렌더한다 (이미 로드됐으면 즉시 resolve).
    ensureMapData().then(() => cells.forEach(cell => renderMiniMapCell(cell)));
    return;
  }
  st.observer = new IntersectionObserver(entries => {
    entries.forEach(en => {
      const cell = en.target;
      if (en.isIntersecting) { cell.dataset.visible = "1"; issueMapQueueRender(cell); }
      else {
        cell.dataset.visible = "";
        issueMapPurge(cell);
        const i = st.queue.indexOf(cell);
        if (i >= 0) st.queue.splice(i, 1);
      }
    });
  }, { rootMargin: "600px 0px", threshold: 0 });
  cells.forEach(c => st.observer.observe(c));
}

// ── Issue Table Map 셀 소스별 펼치기 팝오버(전 소스 웨이퍼 가로 나열) ─────────────
// 표 td 폭을 흔들지 않도록 셀에 앵커된 절대위치 오버레이로 띄운다. 한 번에 하나만 연다.
let _mapExpandEl = null;
let _mapExpandAnchor = null;
function closeMapExpand() {
  if (!_mapExpandEl) return;
  _mapExpandEl.remove();
  _mapExpandEl = null; _mapExpandAnchor = null;
}
// 셀 위치 기준으로 빈 팝오버를 띄우고 반환 — Bin/STDF 두 경로가 배치 로직을 공유한다.
// 스크롤 컨테이너에 잘리지 않도록 body 에 fixed 로 붙인다.
function openMapExpandPop(cell, count) {
  const pop = document.createElement("div");
  pop.className = "map-expand-pop";
  const rect = cell.getBoundingClientRect();
  const maxW = Math.min(count * 232 + 16, window.innerWidth - 40);
  let left = rect.right + 4;
  if (left + maxW > window.innerWidth) left = Math.max(20, window.innerWidth - maxW - 20);
  pop.style.position = "fixed";
  pop.style.top = Math.max(8, rect.top) + "px";
  pop.style.left = left + "px";
  pop.style.maxWidth = maxW + "px";
  document.body.appendChild(pop);
  _mapExpandEl = pop; _mapExpandAnchor = cell;
  return pop;
}

// CPK 행 Map 셀 ⤢ — 전 소스의 STDF 썸네일을 가로로 나열(소스별 10분위 독립 계산).
function openStdfExpand(cell) {
  const subject = cell.dataset.subject;
  const names = ((DATA.web_report && DATA.web_report.sources) || []).map(s => s.name);
  if (!subject || names.length < 2) return;
  const pop = openMapExpandPop(cell, names.length);
  pop.innerHTML = names.map((n, i) =>
    `<div class="map-exp-item"><div class="map-exp-title">${esc(n)}</div>` +
    `<div class="map-exp-plot" id="mapexp-${i}"></div></div>`).join("");
  stdfFetchScatter(subject).then(data => {
    if (_mapExpandEl !== pop) return;   // 그 사이 닫혔거나 다른 셀이 열림
    names.forEach((n, i) => {
      const host = pop.querySelector(`#mapexp-${i}`);
      if (host) stdfDrawThumb(host, data, n);
    });
  }).catch(e => {
    if (_mapExpandEl === pop) pop.innerHTML = `<div class="placeholder">값 로드 실패: ${esc(e.message)}</div>`;
  });
}

// Temp 항목 미니셀 ⤢ — 그 항목이 fail 난 CT/HT 소스 맵을 가로로 나열한다.
function openTempExpand(cell) {
  const item = cell.dataset.tempItem;
  const maps = (webReportSheets() || {})["Map Analysis"];
  if (!item || !Array.isArray(maps) || !maps.length) return;
  if (!tempMapReady) { ensureTempMapData(); return; }
  if (maps.some(m => !Array.isArray(m.dies))) { ensureMapData(); return; }
  const entries = [];
  tempMapItemEntries(item).forEach(e => {
    const m = maps.find(mm => mm.source === e.source);
    if (m) entries.push({ m, hit: new Set(e.idx) });
  });
  if (!entries.length) return;
  const pop = openMapExpandPop(cell, entries.length);
  pop.innerHTML = entries.map((e, i) => {
    const corner = tempCornerOf(e.m.source);
    return `<div class="map-exp-item"><div class="map-exp-title">${esc(e.m.source)}` +
      `${corner ? ` (${esc(corner)})` : ""}${e.m.step ? " — " + esc(e.m.step) : ""}</div>` +
      `<div class="map-exp-plot" id="mapexp-${i}"></div></div>`;
  }).join("");
  entries.forEach((e, i) => {
    const host = pop.querySelector(`#mapexp-${i}`);
    if (!host) return;
    const canvas = document.createElement("canvas");
    canvas.className = "wafer-thumb";
    host.appendChild(canvas);
    drawWaferThumb(canvas, e.m, tempMapRgbFor(e.hit, tempMiniFailColor(e.m.source)));
  });
}

function toggleMapExpand(btn) {
  const cell = btn.closest(".map-cell-mini");
  if (!cell) return;
  if (_mapExpandAnchor === cell) { closeMapExpand(); return; }
  closeMapExpand();
  if (cell.dataset.subject) { openStdfExpand(cell); return; }
  if (cell.dataset.tempItem) { openTempExpand(cell); return; }
  const maps = (webReportSheets() || {})["Map Analysis"];
  if (!Array.isArray(maps) || maps.length < 2) return;
  // dies 지연 로드 중 — 로드만 킥하고 열지 않는다(boot 선로드라 드묾, 배지가 진행 표시).
  if (maps.some(m => !Array.isArray(m.dies))) { ensureMapData(); return; }
  const bin = cell.dataset.bin;
  const binOrder = buildGlobalBinLegend(maps).map(r => r.bin);
  const dim = dimColorMap(globalBinColorMap(), binOrder, bin);
  const pop = openMapExpandPop(cell, maps.length);
  pop.innerHTML = maps.map((m, i) =>
    `<div class="map-exp-item"><div class="map-exp-title">${esc(m.source)}${m.step ? " — " + esc(m.step) : ""}</div>` +
    `<div class="map-exp-plot" id="mapexp-${i}"></div></div>`).join("");
  const rgbFor = issueMapRgbFor(dim);
  maps.forEach((m, i) => {
    const host = pop.querySelector(`#mapexp-${i}`);
    if (!host) return;
    const canvas = document.createElement("canvas");
    canvas.className = "wafer-thumb";
    host.appendChild(canvas);
    drawWaferThumb(canvas, m, rgbFor);   // canvas 라 4 source 동기 루프도 freeze 없음
  });
}
// 팝오버 바깥 클릭 / ESC 로 닫기 (여는 클릭 자체는 btn-map-expand 제외 가드로 통과).
document.addEventListener("click", e => {
  if (_mapExpandEl && !e.target.closest(".map-expand-pop") && !e.target.closest(".btn-map-expand")) closeMapExpand();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeMapExpand(); });

// ── Issue Table: Item 클릭 → Bin 상세(FailTNO 구성 + 분포) 화면 전환 ─────────────
// 새 창/모달 대신 상단 tab 은 그대로 두고 panel-issues 내용만 상세 화면으로 바꾼다.
// 같은 Bin 이라도 서로 다른 TNO(Item)에서 fail 한 유닛이 섞일 수 있어, 그 Bin 전체의
// 구성을 issue_bin_summary(세션 로드 시 미리 계산됨)에서 조회해 보여준다.
function renderIssueDetail(bin, item) {
  const panel = document.getElementById("panel-issues");
  const web = DATA.web_report || {};
  const compRows = (web.issue_bin_summary && web.issue_bin_summary[String(bin)]) || [];
  const compHtml = compRows.length
    ? renderSheetTable(compRows, { kind: "yield" })
    : `<div class="placeholder">구성 정보 없음</div>`;

  // 분포 유무는 distribution_index 로 판단 — ECDF 는 보이는 셀만 배치로 받으므로
  // 캐시 보유 여부로 판단하면 아직 안 받은 항목의 셀이 안 만들어진다.
  const hasDist = distHasData(item);
  panel.innerHTML =
    `<div class="section-title">Issue Table — Bin ${esc(bin)} 상세</div>` +
    `<button type="button" class="btn-sm" id="issueDetailBack">← Issue Table로 돌아가기</button>` +
    `<div class="section-title small" style="margin-top:12px">FailTNO(Item) 구성</div>` +
    compHtml +
    `<div class="section-title small" style="margin-top:14px">${esc(item)} 분포</div>` +
    (hasDist
      ? `<div class="dist-cell" id="issueDetailDistCell" data-subject="${esc(item)}">` +
        `<div class="dist-placeholder">로드 중...</div><div class="dist-plot"></div></div>`
      : `<div class="placeholder">분포 데이터 없음</div>`);

  if (hasDist) renderDistCell(document.getElementById("issueDetailDistCell"));
  document.getElementById("issueDetailBack").addEventListener("click", () => {
    if (MODE === "edit") renderIssuesEdit();
    else renderIssues(DATA.issue_table_text);
  });
}


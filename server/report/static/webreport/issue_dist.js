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
function renderMiniMapCell(cell) {
  if (cell.dataset.mapLoaded === "1") return;
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

let issueMapObserver = null;
let issueMapQueue = [];
let issueMapRafScheduled = false;
function issueMapQueueRender(cell) {
  if (cell.dataset.mapLoaded === "1" || issueMapQueue.includes(cell)) return;
  issueMapQueue.push(cell);
  if (!issueMapRafScheduled) { issueMapRafScheduled = true; requestAnimationFrame(issueMapFlush); }
}
function issueMapFlush() {
  issueMapRafScheduled = false;
  let n = 0;
  while (issueMapQueue.length && n < DIST.PER_FRAME) {
    const cell = issueMapQueue.shift();
    if (cell.isConnected && cell.dataset.visible === "1") { renderMiniMapCell(cell); n++; }
  }
  if (issueMapQueue.length) { issueMapRafScheduled = true; requestAnimationFrame(issueMapFlush); }
}
function issueMapPurge(cell) {
  if (cell.dataset.mapLoaded !== "1") return;
  const div = cell.querySelector(".map-plot");
  if (div) div.innerHTML = "";   // canvas 제거해 화면 밖 메모리 반환
  cell.dataset.mapLoaded = "";
}
function renderIssueMiniMap(panel) {
  if (issueMapObserver) { try { issueMapObserver.disconnect(); } catch (e) {} issueMapObserver = null; }
  issueMapQueue = []; issueMapRafScheduled = false;
  const cells = panel.querySelectorAll(".map-cell-mini");
  if (!cells.length) return;
  if (typeof IntersectionObserver === "undefined") {
    // IO 폴백은 visible 플래그가 없어 refreshMapConsumers 재큐잉을 못 받으므로
    // dies 도착을 기다렸다가 전량 렌더한다 (이미 로드됐으면 즉시 resolve).
    ensureMapData().then(() => cells.forEach(cell => renderMiniMapCell(cell)));
    return;
  }
  issueMapObserver = new IntersectionObserver(entries => {
    entries.forEach(en => {
      const cell = en.target;
      if (en.isIntersecting) { cell.dataset.visible = "1"; issueMapQueueRender(cell); }
      else {
        cell.dataset.visible = "";
        issueMapPurge(cell);
        const i = issueMapQueue.indexOf(cell);
        if (i >= 0) issueMapQueue.splice(i, 1);
      }
    });
  }, { rootMargin: "600px 0px", threshold: 0 });
  cells.forEach(c => issueMapObserver.observe(c));
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
function toggleMapExpand(btn) {
  const cell = btn.closest(".map-cell-mini");
  if (!cell) return;
  if (_mapExpandAnchor === cell) { closeMapExpand(); return; }
  closeMapExpand();
  const maps = (webReportSheets() || {})["Map Analysis"];
  if (!Array.isArray(maps) || maps.length < 2) return;
  // dies 지연 로드 중 — 로드만 킥하고 열지 않는다(boot 선로드라 드묾, 배지가 진행 표시).
  if (maps.some(m => !Array.isArray(m.dies))) { ensureMapData(); return; }
  const bin = cell.dataset.bin;
  const binOrder = buildGlobalBinLegend(maps).map(r => r.bin);
  const dim = dimColorMap(globalBinColorMap(), binOrder, bin);
  const pop = document.createElement("div");
  pop.className = "map-expand-pop";
  pop.innerHTML = maps.map((m, i) =>
    `<div class="map-exp-item"><div class="map-exp-title">${esc(m.source)}${m.step ? " — " + esc(m.step) : ""}</div>` +
    `<div class="map-exp-plot" id="mapexp-${i}"></div></div>`).join("");
  // 스크롤 컨테이너에 잘리지 않도록 body 에 fixed 로 띄우고 셀 위치 기준으로 배치.
  const rect = cell.getBoundingClientRect();
  const maxW = Math.min(maps.length * 232 + 16, window.innerWidth - 40);
  let left = rect.right + 4;
  if (left + maxW > window.innerWidth) left = Math.max(20, window.innerWidth - maxW - 20);
  pop.style.position = "fixed";
  pop.style.top = Math.max(8, rect.top) + "px";
  pop.style.left = left + "px";
  pop.style.maxWidth = maxW + "px";
  document.body.appendChild(pop);
  _mapExpandEl = pop; _mapExpandAnchor = cell;
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

  // 분포 데이터 로딩 중엔 셀을 만들어 두면 도착 후 refreshDistConsumers 가 채운다.
  const hasDist = distDataReady ? !!distDataCache[item] : true;
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


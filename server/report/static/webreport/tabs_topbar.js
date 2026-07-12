// ── tab switching ──────────────────────────────────────────────────────────
document.getElementById("tabs").addEventListener("click", e => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  const tab = btn.dataset.tab;
  hideItemDetail();   // Item_detail 열려 있으면 닫고 해당 탭으로
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b === btn));
  document.querySelectorAll(".panel").forEach(p =>
    p.classList.toggle("active", p.id === `panel-${tab}`));
  // lazy 렌더: 아직 안 그려진(dirty) 탭이면 이 시점에 렌더 (프리렌더가 이미 그렸으면 no-op)
  renderTab(tab);
  // 숨김 상태에서 그려진 Plotly 차트는 0px 로 렌더되므로 활성화 시 리사이즈
  const active = document.getElementById(`panel-${tab}`);
  if (active && window.Plotly) {
    active.querySelectorAll(".js-plotly-plot").forEach(d => { try { Plotly.Plots.resize(d); } catch (e) {} });
  }
  // 프리렌더가 숨김(display:none) 상태에서 그렸으면 헤더 상단행 높이가 0으로 측정되므로,
  // 보이는 시점에 다시 실측한다.
  if (tab === "issues" && active) syncIssueHeadRowHeight(active);
  // Note 탭(Luckysheet 캔버스)은 숨김 상태에서 크기가 0 — 재진입 시 리사이즈.
  if (tab === "note" && window.noteOnTabShown) noteOnTabShown();
});

// ── topbar meta ──────────────────────────────────────────────────────────────
function renderMeta(session) {
  const pt = (session.product_type || "").trim();
  const ptCls = ["MDDI", "PDDI", "PMIC", "SECURITY", "TCON"].includes(pt) ? pt : "default";
  const sourceFiles = ((DATA && DATA.web_report && DATA.web_report.sources) || [])
    .map(s => s.file_name || s.name).filter(Boolean);
  const fname = sourceFiles[0] || session.file_name || "-";
  const fnameTitle = sourceFiles.length > 1 ? sourceFiles.join("\n") : fname;
  // 분석 모드 배지: web_report 세션이고 Normal 이 아닐 때만 표시 (Normal 은 기본이라 생략).
  const mode = (session.mode || "Normal").trim();
  const modeBadge = (isWebReportSession() && mode && mode !== "Normal")
    ? `<span class="mode-badge ${esc(mode)}" title="분석 모드">${esc(mode)}</span>` : "";
  const line1 = [
    `<span class="pt-badge ${ptCls}">${esc(pt || "-")}</span>`,
    modeBadge,
    `<span class="meta-inline"><span class="mk">Product</span>${esc(session.product || "-")}</span>`,
    `<span class="meta-inline"><span class="mk">Revision</span>${esc(session.revision || "-")}</span>`,
    `<span class="meta-inline"><span class="mk">Process</span>${esc(session.process || "-")}</span>`,
    `<span class="meta-inline"><span class="mk">LOT</span>${esc(session.lot_id || "-")}</span>`,
  ];
  const line2 = [
    `<span class="meta-inline-file" title="${esc(fnameTitle)}"><span class="mk">Filename</span>${esc(fname)}</span>`,
    `<span class="meta-inline" title="${esc(session.client_host || "")}"><span class="mk">Uploader</span>${esc(session.uploaded_by || "-")}</span>`,
    `<span class="meta-inline"><span class="mk">Uploaded</span>${esc(fmtDate(session.created_at))}</span>`,
    `<span class="meta-inline"><span class="mk">Status</span>${esc(session.status || "-")}</span>`,
  ];
  document.getElementById("topbarMeta").innerHTML =
    `<div class="meta-row">${line1.join("")}</div><div class="meta-row">${line2.join("")}</div>`;
  updateImportantBtn();
  updatePrivateBtn(session);
  syncTabVisibility();
  syncStickyHeadHeight();
}

// 중요표시는 사용자별 개인 상태(MY_IMPORTANT) — 각자 자기 화면에만 반영된다.
function updateImportantBtn() {
  const btn = document.getElementById("btnImportant");
  if (!btn) return;
  const on = !!MY_IMPORTANT;
  btn.classList.toggle("active", on);
  btn.textContent = on ? "★" : "☆";
  btn.title = on
    ? "내 중요 표시됨 — 내 화면에서만, 자동삭제 제외 (클릭 시 해제)"
    : "중요 표시(내 화면에서만 표시, 오래된 세션 자동삭제에서 제외)";
}

function updatePrivateBtn(session) {
  const btn = document.getElementById("btnPrivate");
  if (!btn) return;
  const on = !!(session && session.is_private);
  btn.classList.toggle("active", on);
  btn.textContent = on ? "🔒" : "🔓";
  btn.title = on ? "비공개 표시됨 (클릭 시 공개로)" : "비공개 표시(클릭 시 비공개로)";
}

// web_report 전용 데이터로만 채워지는 탭(CPK/Map Analysis)은 legacy(xlsx_upload)
// 세션에서 항상 빈 화면이므로 탭 버튼 자체를 숨긴다. 숨긴 탭이 활성 상태였으면 Summary 로 전환.
// (Raw Data 탭은 제거됨 — rawdata 편집은 Honey 사이드바 'Rawdata 수정'(Excel) 으로 이관.)
const WEB_REPORT_ONLY_TABS = ["cpk", "map-analysis", "trim-analysis", "note"];

function syncTabVisibility() {
  const web = isWebReportSession();
  // 1) web_report 전용 탭: 비-web 세션에서 숨김
  WEB_REPORT_ONLY_TABS.forEach(name => {
    const btn = document.querySelector(`.tab[data-tab="${name}"]`);
    if (btn) btn.style.display = web ? "" : "none";
  });
  // 1b) 모드 전용 탭: Compare/Commonality 는 각 모드(web_report)에서만 표시.
  const modeNow = web ? webReportMode() : "Normal";
  const compareBtn = document.querySelector('.tab[data-tab="compare"]');
  if (compareBtn) compareBtn.style.display = (modeNow === "Compare") ? "" : "none";
  // 2) 활성 탭이 숨겨졌으면 첫 번째로 보이는 탭으로 전환
  const activeBtn = document.querySelector(".tab.active");
  if (activeBtn && activeBtn.style.display === "none") {
    const firstVisible = [...document.querySelectorAll(".tab")].find(b => b.style.display !== "none");
    if (firstVisible) {
      const name = firstVisible.dataset.tab;
      document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b === firstVisible));
      document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", p.id === `panel-${name}`));
      renderTab(name);   // lazy 렌더에서 강제 전환된 탭이 dirty 로 남지 않도록
    }
  }
}

// topbar 가 1줄/2줄로 실제 렌더 높이가 바뀌므로, 그 아래 붙는 sticky 요소(rawdata-cols 등)가
// 참조할 --sticky-head-h 를 매번 실측해 갱신한다 (매직넘버 고정값 대신).
function syncStickyHeadHeight() {
  const head = document.getElementById("stickyHead");
  if (head) document.documentElement.style.setProperty("--sticky-head-h", `${head.offsetHeight}px`);
}
window.addEventListener("resize", syncStickyHeadHeight);

// Raw Data 검색/필터 바(.rawdata-filters) 도 반응형으로 줄바꿈되어 높이가 바뀌므로,
// 그 아래 sticky 로 고정하는 그리드 헤더가 참조할 --rawdata-filters-h 를 실측해 갱신한다.
function syncRawFiltersHeight() {
  const bar = document.querySelector(".rawdata-filters");
  document.documentElement.style.setProperty("--rawdata-filters-h", `${bar ? bar.offsetHeight : 0}px`);
}
window.addEventListener("resize", syncRawFiltersHeight);

// Raw Data 그리드의 실제 가로 스크롤 폭(.tabulator-tableholder.scrollWidth)을 프록시
// 스크롤바(#rawDataHscroll 안의 스페이서)의 폭에 반영한다. 컬럼 리사이즈/윈도우 리사이즈/
// 그리드 재빌드 시마다 다시 불러야 한다 — 프록시 자체의 높이는 우리가 만든 고정 크기 UI라
// CSS 리터럴(--rawdata-hscroll-h)로 충분하고, 실측이 필요한 건 폭뿐이다.
function syncRawHscrollSpacer() {
  const spacer = document.getElementById("rawDataHscrollSpacer");
  const holder = document.querySelector("#rawDataGridHost .tabulator-tableholder");
  if (!spacer) return;
  spacer.style.width = holder ? `${holder.scrollWidth}px` : "0px";
}
window.addEventListener("resize", syncRawHscrollSpacer);

// ── read-only helpers ────────────────────────────────────────────────────────
function emptyPanel(panel, msg) {
  panel.innerHTML = `<div class="placeholder">${esc(msg)}</div>`;
}

function deriveCols(rows) {
  const seen = [];
  (rows || []).forEach(r => Object.keys(r || {}).forEach(k => { if (!seen.includes(k)) seen.push(k); }));
  return seen;
}


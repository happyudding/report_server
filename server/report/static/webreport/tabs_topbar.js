// ── 탭별 대용량 지연 데이터 ──────────────────────────────────────────────────
// 분포 ECDF(수십~수백 MB)와 map dies(수백만 개)는 그 데이터를 쓰는 탭에 들어갈 때만
// 받는다. 종전에는 페이지 로드 시 무조건 둘 다 받아서, Summary 만 보고 나가는 사용자도
// 전량 다운로드 비용을 냈다. ensure* 는 멱등이라 중복 호출은 진행 중 promise 를 재사용한다.
function ensureTabData(tab) {
  // Issue Table Compare 도 Distribution 미니셀을 그린다 — 여기서 빠져 있으면 표는 뜨는데
  // 미니 차트만 비어 있다(2026-08-27 확인·수정). 단 **Map 계열은 받지 않는다**: Compare
  // 이슈 표에는 Map 컬럼이 없어(report_view.html 의 5·6번째 컬럼 보정 참조) 수백만 die 를
  // 헛받게 된다. ensureTempMapData 는 원래 Temperature 전용이라 조건을 좁혀도 동작이 같다.
  const needDist = (tab === "distribution" || tab === "issues" ||
                    tab === "issue-temp" || tab === "issue-cmp");
  const needMap = (tab === "map-analysis" || tab === "issues" || tab === "issue-temp");
  if (needDist) ensureDistData();
  if (needMap) ensureMapData();
  // Issue Table Temp 의 Map 셀은 항목별 fail die 인덱스가 따로 필요하다(Temperature 전용).
  if (tab === "issue-temp" && typeof ensureTempMapData === "function") ensureTempMapData();
}

// ── tab switching ──────────────────────────────────────────────────────────
document.getElementById("tabs").addEventListener("click", e => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  const tab = btn.dataset.tab;
  hideItemDetail();   // Item_detail 열려 있으면 닫고 해당 탭으로
  hideMapDetail();    // Map Detail 열려 있으면 닫고 해당 탭으로
  // Distribution composite 상세 (dist_composite.js — 로드 전이면 no-op)
  if (typeof hideDistCompositeDetail === "function") hideDistCompositeDetail();
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b === btn));
  document.querySelectorAll(".panel").forEach(p =>
    p.classList.toggle("active", p.id === `panel-${tab}`));
  ensureTabData(tab);
  // lazy 렌더: 아직 안 그려진(dirty) 탭이면 이 시점에 렌더 (프리렌더가 이미 그렸으면 no-op)
  renderTab(tab);
  // 숨김 상태에서 그려진 Plotly 차트는 0px 로 렌더되므로 활성화 시 리사이즈
  const active = document.getElementById(`panel-${tab}`);
  if (active && window.Plotly) {
    active.querySelectorAll(".js-plotly-plot").forEach(d => { try { Plotly.Plots.resize(d); } catch (e) {} });
  }
  // 프리렌더가 숨김(display:none) 상태에서 그렸으면 헤더 상단행 높이가 0으로 측정되므로,
  // 보이는 시점에 다시 실측한다.
  // 좌측 고정열 left 오프셋(--issue-colN-left)도 같은 이유로 숨김 상태에선 전부 0 이라
  // 아예 심어지지 않고 CSS fallback(Item=124px 가정) 이 쓰였다 → 실제 Item 열이 그보다
  // 넓으면 Map/Distribution 이 Item 위로 겹쳐 "Item 이 잘리고 Map/Dist 가 고정블록
  // 오른쪽에 안 붙는" 증상이 났다(사용자 신고 2026-08-10). 여기서 반드시 재실측한다.
  if ((tab === "issues" || tab === "issue-temp") && active) {
    syncIssueHeadRowHeight(active);
    syncIssueStickyOffsets(active);
    syncIssueHscrollSpacer(active);
    // 미니셀·폰트 반영으로 폭이 한 번 더 흔들리므로 레이아웃 확정 후 재실측(렌더 경로와 동일).
    requestAnimationFrame(() => {
      syncIssueHeadRowHeight(active);
      syncIssueStickyOffsets(active);
      syncIssueHscrollSpacer(active);
    });
  }
  // 같은 이유 — Yield 좌측 고정열 left 오프셋도 숨김 상태에선 0 으로 측정된다.
  if (tab === "yield" && active) syncYieldStickyOffsets(active);
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
    // product_info.db 기준정보 — 업로드 시 선택 product 로 서버 lookup 되어 세션에 저장됨.
    `<span class="meta-inline"><span class="mk">WF Size</span>${esc(session.wf_size || "-")}</span>`,
    `<span class="meta-inline"><span class="mk">Gross Die</span>${esc(session.gross_die || "-")}</span>`,
    `<span class="meta-inline"><span class="mk">PKG</span>${esc(session.pkg_type || "-")}</span>`,
    `<span class="meta-inline"><span class="mk">Para</span>${esc(session.para || "-")}</span>`,
    `<span class="meta-inline"><span class="mk">Equip</span>${esc(session.equip || "-")}</span>`,
    `<span class="meta-inline"><span class="mk">Flat Zone</span>${esc(session.flat_zone || "-")}</span>`,
  ];
  const line2 = [
    // Session_name = report_session.file_name — 메인 검색결과 목록의 파일명 칸과 같은 값이고
    // ✏️(Honey 편집창)에서 바꾸는 대상. 오른쪽 Filename(manifest 의 원본 소스 파일명)과는
    // 별개 값이라 서로 덮어쓰지 않는다.
    // 편집 권한자는 **이 자리를 클릭해 바로** 이름을 고친다(edit_mode.js sessionNameEdit) —
    // 이름은 표시 전용이라 나머지 메타(Honey 편집창 전용)와 달리 웹에서도 열려 있다.
    `<span class="meta-inline" title="${canEditSession()
        ? "세션 이름 (검색결과 목록에 표시) — 클릭해서 수정"
        : "세션 이름 (검색결과 목록에 표시)"}"><span class="mk">Session_name</span>` +
      `<span id="sessionNameVal"${canEditSession() ? ' class="sname-editable" role="button" tabindex="0"' : ""}>` +
      `${esc(session.file_name || "-")}</span></span>`,
    `<span class="meta-inline-file" title="${esc(fnameTitle)}"><span class="mk">Filename</span>${esc(fname)}</span>`,
    // 업로더는 '이름(ID)' 로 — 이름은 my_access 가 실어준다(UPLOADER_NAME, core.js).
    `<span class="meta-inline" title="${esc(session.client_host || "")}"><span class="mk">Uploader</span>${esc(UserName.fmt(UserName.uid(session.uploaded_by), UPLOADER_NAME) || "-")}</span>`,
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
const WEB_REPORT_ONLY_TABS = ["cpk", "map-analysis", "characteristic", "note"];

function syncTabVisibility() {
  const web = isWebReportSession();
  // 1) web_report 전용 탭: 비-web 세션에서 숨김
  WEB_REPORT_ONLY_TABS.forEach(name => {
    const btn = document.querySelector(`.tab[data-tab="${name}"]`);
    if (btn) btn.style.display = web ? "" : "none";
  });
  // 1b) 모드 전용 탭: Compare/Commonality 는 각 모드(web_report)에서만 표시.
  const modeNow = web ? webReportMode() : "Normal";
  // Issue Table Compare 는 Compare 모드 전용 — 서브탭 5개(ISSUE_TABLE/MAP비교/LOG비교/
  // TESTTIME비교/동일성검증)로 구 최상위 Compare 탭을 흡수했다(2026-08-27).
  const cmpIssBtn = document.querySelector('.tab[data-tab="issue-cmp"]');
  if (cmpIssBtn) cmpIssBtn.style.display = (modeNow === "Compare") ? "" : "none";
  // Issue Table Temp 는 Temperature 모드 전용 (CT/HT 를 RT Limit 으로 재판정한 표).
  const tempBtn = document.querySelector('.tab[data-tab="issue-temp"]');
  if (tempBtn) tempBtn.style.display = (modeNow === "Temperature") ? "" : "none";
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


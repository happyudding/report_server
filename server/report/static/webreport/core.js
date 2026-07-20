const SESSION_ID = location.pathname.split("/").pop();
const YIELD_COLS = ["item_name", "bin_number", "yield_percent", "fail_count",
                    "cpk_val", "mean_val", "stdev_val", "lsl", "usl", "unit"];

// ── 없는 sheet 용 기본 표 골격(frame) ──────────────────────────────────────────
// 업로드 xlsx 에 해당 sheet 가 없으면 헤더만 있는 빈 표(헤더 + 빈 행 1개)를 보여준다.
// 동적 source 열({src}_count/_yield)은 sheet 가 없으면 알 수 없으므로 정적 열만 포함.
// 헤더 값은 생성기 client/report_generator/xlsx_writer.py 레이아웃과 일치.
const YIELD_FRAME_COLS = ["step", "bin", "TNO", "Item", "avg", "comment"];
const ISSUE_FRAME_COLS = ["Step", "Bin", "TNO", "Item", "avg", "Distribution",
                          "PTE comment", "개발 comment"];
const SUMMARY_FRAME_BLOCKS = [
  { label: "1. Device Feature",
    headers: ["DEVICE", "Customer", "PKG_Type", "GrossDie", "Process Line", "EVT_Version"] },
  { label: "2. Yield",
    headers: ["Lot NO", "Yield", "Major Fail Bins", "Comment"] },
  { label: "3. Evaluation Summary",
    headers: ["Category", "Condition & Judge Limit", "Result"] },
];

// 헤더+빈 행 1개용 행 객체 (모든 열 빈 문자열)
function frameRow(cols) { const o = {}; cols.forEach(c => { o[c] = ""; }); return o; }
// summary blocks frame: 블록마다 빈 행 1개
function summaryFrameData() {
  return { blocks: SUMMARY_FRAME_BLOCKS.map(b => ({
    label: b.label, headers: b.headers, rows: [b.headers.map(() => "")] })) };
}

// legacy summary dict 에 표시할 의미있는 내용이 있는지 (title 단독은 제외)
function summaryDictHasContent(st) {
  if (!st || typeof st !== "object") return false;
  return !!((st.feature && Object.keys(st.feature).length) ||
            (Array.isArray(st.yield_summary_text) && st.yield_summary_text.length) ||
            (Array.isArray(st.major_fail_bins) && st.major_fail_bins.length) ||
            (st.evaluation && Object.keys(st.evaluation).length) ||
            (Array.isArray(st.raw_rows) && st.raw_rows.length));
}

// 표 3종이 "정말로 비었을 때"만 frame 구조로 시드 → 보기/수정/저장이 신규 포맷 경로를 탄다.
// 판정 조건은 각 render* 의 emptyPanel fall-through 와 동일.
function seedEmptyFrames() {
  if (!DATA) return;
  const summaryRows = DATA.summary;

  // summary: blocks/grid 아니고 legacy dict 내용도 summary_rows 도 없을 때
  if (!isSummaryBlocks(DATA.summary_text) && !isGrid(DATA.summary_text) &&
      !summaryDictHasContent(DATA.summary_text) &&
      !(Array.isArray(summaryRows) && summaryRows.length)) {
    DATA.summary_text = summaryFrameData();
  }

  // yield: yield_text(list/grid) 미사용 그리고 legacy summary_rows 도 없을 때
  if (!(Array.isArray(DATA.yield_text) && DATA.yield_text.length) &&
      !isGrid(DATA.yield_text) &&
      !(Array.isArray(summaryRows) && summaryRows.length)) {
    DATA.yield_text = [frameRow(YIELD_FRAME_COLS)];
  }

  // issue: issue_table_text(list/grid) 미사용일 때
  if (!(Array.isArray(DATA.issue_table_text) && DATA.issue_table_text.length) &&
      !isGrid(DATA.issue_table_text)) {
    DATA.issue_table_text = [frameRow(ISSUE_FRAME_COLS)];
  }
}

// CSRF: 서버가 /view 응답에 내려준 double-submit 쿠키를 읽어 변경요청 헤더로 되돌려준다.
function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)report_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

let DATA = null;
function isWebReportSession() {
  return !!(DATA && DATA.session && DATA.session.source === "web_report");
}
// 세션 분석 모드(Normal/Compare/DUT/Commonality). 세션 DB 컬럼이 authoritative.
function webReportMode() {
  return (((DATA && DATA.session && DATA.session.mode) || "Normal") + "").trim();
}
// 편집 권한: 로그인 ID 가 세션 업로더와 같을 때만 "edit" (업로더 기록 없는 legacy 는
// 로그인만 하면 허용). 그 외에는 "view" — 흩어진 MODE==="edit" 분기가 읽기전용으로 렌더.
// 로그인은 검색결과 페이지에서 수행하고, 서버가 편집 라우트에서 같은 규칙으로 재검증한다.
let MODE = "view";
let LOGIN_USER = "";            // 현재 PC 사용자 (Honey 밖이면 "")
let CAN_EDIT = false;           // 이 세션 편집 가능(업로더 또는 위임 편집자) — 서버 판정
let IS_UPLOADER = false;        // 이 세션 업로더 본인 — 권한부여/비공개/삭제용
let MY_IMPORTANT = false;       // 내 개인 중요표시 상태(사용자별)
let verifiedPassword = "";      // (구 PIN 흐름 잔재 — 저장 payload 호환용, 항상 "")

// 세션별 권한·개인상태는 서버가 요청자(User-Agent) 기준으로 판정해 내려준다.
// (session_full 은 세션 단위 gzip 캐시라 사용자별 값을 섞으면 안 되므로 별도 경량 엔드포인트)
async function loadAuth() {
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/my_access`);
    if (res.ok) {
      const j = await res.json();
      LOGIN_USER = j.user_id || "";
      CAN_EDIT = !!j.can_edit;
      IS_UPLOADER = !!j.is_uploader;
      MY_IMPORTANT = !!j.my_important;
    } else {
      console.warn("my_access 조회 실패 — 읽기 전용으로 표시 (HTTP " + res.status + ")");
    }
  } catch (e) {
    console.warn("my_access 조회 실패 — 읽기 전용으로 표시", e);
    LOGIN_USER = ""; CAN_EDIT = false; IS_UPLOADER = false; MY_IMPORTANT = false;
  }
}

function canEditSession() {
  return CAN_EDIT;
}
let yieldGrid = null;           // 수정 모드 Yield 편집 Tabulator 인스턴스 (없으면 null)


// & < > " ' 모두 이스케이프 → 텍스트/속성 양쪽에서 안전
function esc(v) {
  return String(v ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtDate(unix) {
  if (!unix) return "-";
  const d = new Date(unix * 1000);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function showToast(msg, ms=2200) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), ms);
}

function splitLines(v) {
  const a = (v || "").split("\n");
  while (a.length && a[a.length - 1].trim() === "") a.pop();
  return a;
}

// ── 라이트/다크 테마 토글 (head 부트스트랩이 저장값을 먼저 적용해 둔 상태) ──
(function () {
  const btn = document.getElementById("themeToggle");
  if (!btn) return;
  const root = document.documentElement;
  function syncThemeBtn() {
    const dark = root.getAttribute("data-theme") === "dark";
    btn.textContent = dark ? "☀️" : "🌙";
    btn.title = dark ? "라이트 모드로 전환" : "다크 모드로 전환";
  }
  btn.addEventListener("click", () => {
    const toDark = root.getAttribute("data-theme") !== "dark";
    if (toDark) root.setAttribute("data-theme", "dark");
    else root.removeAttribute("data-theme");
    try {
      if (toDark) localStorage.setItem("report_theme", "dark");
      else localStorage.removeItem("report_theme");
    } catch (e) {}
    syncThemeBtn();
  });
  syncThemeBtn();
})();

// ── 화면 설정(글꼴) 팝오버 — head 부트스트랩이 저장값을 먼저 적용해 둔 상태 ──
const UI_FONTS = {
  default: "",   // 빈 값 → CSS 기본 스택(--report-font 미설정과 동일 효과)
  malgun: '"Malgun Gothic", sans-serif',
  noto: '"Noto Sans KR", "Malgun Gothic", sans-serif',
  system: 'system-ui, -apple-system, "Segoe UI", sans-serif',
};
// 화면 배율(글자 크기). font-size 가 아니라 zoom 인 이유 — 이 페이지의 font-size 는 전부
// 절대 px 이고(루트 크기 없음), 열 폭(sheets.js colWidth)·카드 높이·Plotly 글자 크기가 모두
// 그 px 에 맞춰 손튜닝돼 있다. 글자만 키우면 잘리거나 열이 넘치지만, zoom 은 전부 같은
// 비율로 확대해 상대 레이아웃을 보존한다. 키는 "100"=기본(스타일 미설정).
const UI_ZOOMS = { "100": "", "110": "1.1", "125": "1.25", "150": "1.5" };
(function () {
  const toggle = document.getElementById("settingsToggle");
  const panel = document.getElementById("settingsPanel");
  if (!toggle || !panel) return;
  const root = document.documentElement;
  let curFont = (() => { try { return localStorage.getItem("report_ui_font") || "default"; } catch (e) { return "default"; } })();
  let curZoom = (() => { try { return localStorage.getItem("report_ui_zoom") || "100"; } catch (e) { return "100"; } })();

  function applyFont(key) {
    curFont = UI_FONTS[key] != null ? key : "default";
    const stack = UI_FONTS[curFont];
    if (!stack) root.style.removeProperty("--report-font");
    else root.style.setProperty("--report-font", stack);
    try { localStorage.setItem("report_ui_font", curFont); } catch (e) {}
    syncActive();
  }
  function applyZoom(key) {
    curZoom = UI_ZOOMS[key] != null ? key : "100";
    const z = UI_ZOOMS[curZoom];
    // zoom 은 100vh 도 배율만큼 곱해버려(실측 확인) 뷰포트 기준 높이 계산이 화면을 넘친다.
    // --vph 를 배율로 나눠 넣어 CSS 의 calc(var(--vph) - ...) 들이 다시 뷰포트에 맞게 한다.
    if (!z) { root.style.removeProperty("zoom"); root.style.removeProperty("--vph"); }
    else { root.style.zoom = z; root.style.setProperty("--vph", `calc(100vh / ${z})`); }
    try { localStorage.setItem("report_ui_zoom", curZoom); } catch (e) {}
    syncActive();
    // 실측 기반 레이아웃(--sticky-head-h, Issue Table 고정열 left, Plotly responsive)은 전부
    // window.resize 에만 걸려 있는데 zoom 변경은 resize 를 발생시키지 않는다. 리플로우가
    // 끝난 다음 프레임에 1회 쏴서 재동기화시킨다.
    requestAnimationFrame(() => {
      window.dispatchEvent(new Event("resize"));
      // Note 탭 Luckysheet 는 iframe 격리라 부모 resize 를 못 받는다 — 직접 알린다.
      if (typeof noteOnTabShown === "function") noteOnTabShown();
    });
  }
  function syncActive() {
    panel.querySelectorAll("#uiFontSeg button").forEach(b =>
      b.classList.toggle("active", b.dataset.font === curFont));
    panel.querySelectorAll("#uiZoomSeg button").forEach(b =>
      b.classList.toggle("active", b.dataset.zoom === curZoom));
  }
  toggle.addEventListener("click", (e) => { e.stopPropagation(); panel.classList.toggle("open"); });
  panel.addEventListener("click", (e) => {
    e.stopPropagation();
    const ft = e.target.closest("#uiFontSeg button");
    if (ft) { applyFont(ft.dataset.font); return; }
    const zm = e.target.closest("#uiZoomSeg button");
    if (zm) { applyZoom(zm.dataset.zoom); return; }
    const stab = e.target.closest(".settings-tab");
    if (stab) { switchSettingsTab(stab.dataset.stab); return; }
    const grant = e.target.closest("button[data-grant]");
    if (grant && !grant.disabled) { grantEditor(grant.dataset.grant); return; }
    const revoke = e.target.closest("button[data-revoke]");
    if (revoke) { revokeEditor(revoke.dataset.revoke); return; }
  });
  // 팝오버 밖 클릭 시 닫기.
  document.addEventListener("click", (e) => {
    if (panel.classList.contains("open") && !panel.contains(e.target) && e.target !== toggle)
      panel.classList.remove("open");
  });

  // ── 권한 탭 (업로더만 노출) — 편집 권한 위임 부여/회수 ──
  let _permLoaded = false;
  function switchSettingsTab(name) {
    panel.querySelectorAll(".settings-tab").forEach(b =>
      b.classList.toggle("active", b.dataset.stab === name));
    panel.querySelectorAll(".settings-body").forEach(b =>
      b.style.display = b.dataset.sbody === name ? "" : "none");
    if (name === "perm" && !_permLoaded) {
      _permLoaded = true;
      loadEditorsList();
      searchCandidates("");
    }
  }
  function renderEditorsList(editors) {
    const box = document.getElementById("permCurrent");
    if (!box) return;
    if (!editors.length) { box.innerHTML = `<div class="perm-empty">아직 편집 권한을 준 사용자가 없습니다.</div>`; return; }
    box.innerHTML = editors.map(ed => `
      <div class="perm-item">
        <span class="perm-user" title="${esc(ed.editor_user)}">${esc(ed.editor_user)}</span>
        <button class="revoke" data-revoke="${esc(ed.editor_user)}">회수</button>
      </div>`).join("");
  }
  async function loadEditorsList() {
    const box = document.getElementById("permCurrent");
    if (!box) return;
    try {
      const res = await fetch(`/pe/report/session/${SESSION_ID}/editors`);
      if (!res.ok) { box.innerHTML = `<div class="perm-empty">불러오기 실패</div>`; return; }
      renderEditorsList((await res.json()).editors || []);
    } catch (e) { box.innerHTML = `<div class="perm-empty">불러오기 실패</div>`; }
  }
  async function searchCandidates(q) {
    const box = document.getElementById("permCandidates");
    if (!box) return;
    try {
      const res = await fetch(`/pe/report/session/${SESSION_ID}/editors/candidates?q=${encodeURIComponent(q)}`);
      if (!res.ok) { box.innerHTML = `<div class="perm-empty">검색 실패</div>`; return; }
      const list = (await res.json()).candidates || [];
      if (!list.length) { box.innerHTML = `<div class="perm-empty">해당 사용자가 없습니다 (web_report 방문 기록 기준).</div>`; return; }
      box.innerHTML = list.map(c => `
        <div class="perm-item">
          <span class="perm-user" title="${esc(c.user)}">${esc(c.user)}</span>
          <button data-grant="${esc(c.user)}"${c.already ? " disabled" : ""}>${c.already ? "부여됨" : "부여"}</button>
        </div>`).join("");
    } catch (e) { box.innerHTML = `<div class="perm-empty">검색 실패</div>`; }
  }
  async function grantEditor(user) {
    try {
      const res = await fetch(`/pe/report/session/${SESSION_ID}/editors`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({ user }),
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) { showToast(j.error || "권한 부여 실패"); return; }
      renderEditorsList(j.editors || []);
      searchCandidates((document.getElementById("permSearch") || {}).value || "");
      showToast(`${user} 에게 편집 권한을 주었습니다.`);
    } catch (e) { showToast("권한 부여 실패: " + e.message); }
  }
  async function revokeEditor(user) {
    try {
      const res = await fetch(`/pe/report/session/${SESSION_ID}/editors/${encodeURIComponent(user)}`, {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrfToken() },
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) { showToast(j.error || "권한 회수 실패"); return; }
      renderEditorsList(j.editors || []);
      searchCandidates((document.getElementById("permSearch") || {}).value || "");
      showToast(`${user} 의 편집 권한을 회수했습니다.`);
    } catch (e) { showToast("권한 회수 실패: " + e.message); }
  }
  const permSearch = document.getElementById("permSearch");
  if (permSearch) {
    let _searchTimer = null;
    permSearch.addEventListener("input", () => {
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => searchCandidates(permSearch.value || ""), 250);
    });
  }

  syncActive();
})();


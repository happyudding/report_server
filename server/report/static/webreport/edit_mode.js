// ── 편집용 위젯 ────────────────────────────────────────────────────────────
function editRowHtml(r, cols) {
  return `<tr>` + cols.map(c =>
    `<td><input class="cell-input" data-col="${esc(c)}" value="${esc(r ? r[c] : "")}"></td>`
  ).join("") + `<td><button type="button" class="btn-del-row" title="행 삭제">×</button></td></tr>`;
}

function editTableHtml(rows, cols, tableId) {
  const head = cols.map(c => `<th data-col="${esc(c)}">${esc(c)}</th>`).join("") + `<th></th>`;
  const body = (rows || []).map(r => editRowHtml(r, cols)).join("");
  return `<table class="edit-table" id="${tableId}"><thead><tr>${head}</tr></thead>` +
         `<tbody>${body}</tbody></table>` +
         `<button type="button" class="btn-sm add-row" data-table="${tableId}">+ 행 추가</button>`;
}

function readEditTable(tableEl) {
  if (!tableEl) return null;
  const cols = [...tableEl.querySelectorAll("thead th[data-col]")].map(th => th.dataset.col);
  const rows = [];
  tableEl.querySelectorAll("tbody tr").forEach(tr => {
    const obj = {};
    tr.querySelectorAll("input.cell-input").forEach(inp => { obj[inp.dataset.col] = inp.value; });
    if (cols.some(c => String(obj[c] ?? "").trim() !== "")) rows.push(obj);
  });
  return rows;
}

function kvEditHtml(obj, group) {
  const entries = Object.entries(obj || {}).filter(([k]) => k);
  if (!entries.length) return "";
  return `<div class="kv-edit">` + entries.map(([k, v]) => {
    const val = (v && typeof v === "object") ? JSON.stringify(v) : (v ?? "");
    return `<label title="${esc(k)}">${esc(k)}</label>` +
      `<input class="field-input" data-f="${esc(group)}" data-key="${esc(k)}" value="${esc(val)}">`;
  }).join("") + `</div>`;
}

// ── Summary (edit) ────────────────────────────────────────────────────────────
function collectSummaryText() {
  const panel = document.getElementById("panel-summary");
  const out = DATA.summary_text ? JSON.parse(JSON.stringify(DATA.summary_text)) : {};

  const titleInp = panel.querySelector('[data-f="title"]');
  if (titleInp) out.title = titleInp.value;

  const featInputs = panel.querySelectorAll('[data-f="feature"]');
  if (featInputs.length) {
    out.feature = {};
    featInputs.forEach(i => { out.feature[i.dataset.key] = i.value; });
  }

  const yst = panel.querySelector('[data-f="yield_summary_text"]');
  if (yst) out.yield_summary_text = splitLines(yst.value);

  const mfb = readEditTable(document.getElementById("tbl-mfb"));
  if (mfb !== null) out.major_fail_bins = mfb;

  const evalInputs = panel.querySelectorAll('[data-f="evaluation"]');
  if (evalInputs.length) {
    out.evaluation = {};
    evalInputs.forEach(i => { out.evaluation[i.dataset.key] = i.value; });
  }

  const rr = panel.querySelector('[data-f="raw_rows"]');
  if (rr) out.raw_rows = splitLines(rr.value).map(line => line.split("\t"));

  return out;
}

// ── Yield 편집: Tabulator 인스턴스 정리 (buildPayload 의 yieldGrid.getData() 폴백 대상) ──
function destroyYieldGrid() {
  if (yieldGrid) { try { yieldGrid.destroy(); } catch (e) {} yieldGrid = null; }
}

// ── Issue Table (edit) ──────────────────────────────────────────────────────
// 수정모드에서도 PTE comment / 개발 comment 두 열만 편집 가능(더블클릭으로 활성화), 나머지
// 열(Category/Bin/TNO/Item/avg/{source}_yield/Distribution 등)은 읽기전용으로 유지한다.
const ISSUE_COMMENT_COLS = new Set(["PTE comment", "개발 comment"]);

function renderIssuesEdit() {
  const panel = document.getElementById("panel-issues");
  if (Array.isArray(DATA.issue_table_text) && DATA.issue_table_text.length) {
    panel.innerHTML =
      issueToolbarHtml() +
      renderSheetTable(DATA.issue_table_text, { edit: true, kind: "issue", editableCols: ISSUE_COMMENT_COLS });
    syncIssueHeadRowHeight(panel);
    // 읽기 모드와 동일하게 좌측 고정(Step~Distribution) 오프셋 실측 — 편집 모드에서도 고정열 정렬.
    syncIssueStickyOffsets(panel);
    requestAnimationFrame(() => syncIssueStickyOffsets(panel));   // 레이아웃 확정 후 재실측
    renderIssueMiniDist(panel);
    renderIssueMiniMap(panel);
    bindIssueColResize(panel);
    return;
  }
  emptyPanel(panel, "Issue Table 데이터 없음");
}

// ── 모드 전환 ──────────────────────────────────────────────────────────────
// ── 탭별 lazy 렌더 ────────────────────────────────────────────────────────────
// 활성 탭(보통 Summary)만 즉시 그려 첫 화면을 빨리 띄우고, 나머지는 dirty 플래그를
// 세워뒀다가 탭 클릭 시 또는 백그라운드 프리렌더(schedulePrerender)에서 그린다.
// Issue Table 의 comment 두 열, Raw Data 외에는 수정모드에서도 조회 화면과 동일하게
// 읽기전용으로 둔다 (Summary/Yield 는 편집 UI 자체를 렌더하지 않음).
const TAB_RENDERERS = {
  "summary": () => { renderWebSummary(); },
  "yield": () => {
    destroyYieldGrid();   // 재렌더 전 이전 Tabulator 인스턴스 정리
    renderYield(DATA.yield_text, DATA.summary);
    // Yield 탭 하단 Fail Bin 막대차트는 표시하지 않는다(avg 셀 가로 게이지로 대체).
  },
  "issues": () => {
    if (MODE === "edit") renderIssuesEdit();
    else renderIssues(DATA.issue_table_text);
  },
  "cpk": renderCpk,
  "distribution": renderDistribution,
  "map-analysis": renderMapAnalysis,
  "compare": renderCompare,
  // "raw-data" 탭 제거 — rawdata 편집은 Honey 사이드바 'Rawdata 수정'(Excel) 로 이관.
  // 관련 JS(renderRawDataTab 등)와 #panel-raw-data 는 비활성 상태로 남겨둠(참조 안전).
  // Trim Analysis 는 탭 진입 시 lazy fetch (프리렌더 큐 제외 — 숨김 Plotly 렌더 회피)
  "trim-analysis": renderTrimAnalysis,
  // Note(Luckysheet 캔버스)도 탭 진입 시 lazy — 번들(≈4MB) 로드가 첫 페인트를 막지 않게.
  "note": () => renderNoteTab(),
};
const tabDirty = {};

function activeTabName() {
  const btn = document.querySelector(".tab.active");
  return (btn && btn.dataset.tab) || "summary";
}

function renderTab(name) {
  if (!tabDirty[name] || !TAB_RENDERERS[name]) return;
  tabDirty[name] = false;
  TAB_RENDERERS[name]();
}

// 백그라운드 프리렌더: 첫 페인트를 막지 않도록 idle 타임에 한 탭씩 미리 그려둔다.
// raw-data 는 사용자 조작(항목 선택) 기반 lazy 조회라 첫 클릭 시 렌더가 자연스럽고,
// distribution 은 카드 셸만 미리 만들어도 실제 차트는 가시성+데이터 도착 후 그려진다.
let _prerenderToken = 0;
function schedulePrerender() {
  const token = ++_prerenderToken;
  // map-analysis 는 프리렌더에서 제외한다. wafer map 은 scaleanchor(정사각 고정) 플롯이라
  // 숨김(0폭) 상태에서 그려지면 탭 활성화 시 Plotly.Plots.resize 로도 종횡비/폭이 제대로
  // 복구되지 않아 짤려 보인다 → 탭을 처음 열 때(패널 visible) renderTab 이 정상 폭으로 그리도록 둔다.
  const queue = ["yield", "issues", "cpk", "distribution"];
  const idle = window.requestIdleCallback
    ? (fn => window.requestIdleCallback(fn, { timeout: 1000 }))
    : (fn => setTimeout(fn, 200));
  function next() {
    if (token !== _prerenderToken) return;   // 새 renderActive 가 돌면 이전 체인 중단
    const name = queue.shift();
    if (name === undefined) return;
    const btn = document.querySelector(`.tab[data-tab="${name}"]`);
    if ((!btn || btn.style.display !== "none") && tabDirty[name]) renderTab(name);
    idle(next);
  }
  idle(next);
}

function renderActive() {
  Object.keys(TAB_RENDERERS).forEach(n => { tabDirty[n] = true; });
  renderTab(activeTabName());
  schedulePrerender();
}

// add/delete row (편집 테이블 공용 위임)
document.querySelector(".content").addEventListener("click", e => {
  // Item명 클릭 → Item_detail (Yield/CPK/IssueTable/Bin상세 공용).
  const itemLink = e.target.closest(".item-detail-link");
  if (itemLink) {
    const subject = itemLink.dataset.subject;
    // 미저장 comment 가 있으면 먼저 저장(유실 방지) 후 이동.
    flushPendingComments().then(ok => { if (ok && subject) openItemDetail(subject, [subject]); });
    return;
  }
  // Issue Table Yield 대표행 토글 → 그 Bin 의 detail TNO 행 펼치기/접기.
  const issueToggle = e.target.closest(".issue-toggle");
  if (issueToggle) { toggleIssueGroup(issueToggle); return; }
  // Issue Table Map 셀 소스별 펼치기.
  const mapExpandBtn = e.target.closest(".btn-map-expand");
  if (mapExpandBtn) { toggleMapExpand(mapExpandBtn); return; }
  if (e.target.id === "issueToggleAll") {
    const expand = e.target.dataset.expanded !== "true";
    e.target.dataset.expanded = expand ? "true" : "false";
    e.target.textContent = expand ? "TNO 전체 접기" : "TNO 전체 펼치기";
    setAllIssueGroups(expand);
    return;
  }
  const jumpBtn = e.target.closest("[data-issue-jump]");
  if (jumpBtn) { jumpToIssueSection(jumpBtn.dataset.issueJump); return; }
  const etcAddBtn = e.target.closest("#etcAddItemBtn");
  if (etcAddBtn) { openEtcItemModal(); return; }
  const etcDelBtn = e.target.closest(".btn-del-etc-item");
  if (etcDelBtn) { removeEtcItem(etcDelBtn.dataset.item); return; }
  const addBtn = e.target.closest(".add-row");
  if (addBtn) {
    const table = document.getElementById(addBtn.dataset.table);
    const cols = [...table.querySelectorAll("thead th[data-col]")].map(th => th.dataset.col);
    table.querySelector("tbody").insertAdjacentHTML("beforeend", editRowHtml(null, cols));
    return;
  }
  const delBtn = e.target.closest(".btn-del-row");
  if (delBtn) { delBtn.closest("tr").remove(); }
});

// PTE/개발 comment 등 dblclick-edit 셀: 더블클릭 전에는 읽기전용 표시, 더블클릭 시에만
// contenteditable 활성화 (Distribution 등 다른 열이 즉시 편집 가능한 것과 구분).
document.querySelector(".content").addEventListener("dblclick", e => {
  if (MODE !== "edit") return;   // 읽기전용(비업로더) 모드에서는 편집 진입 차단
  const cell = e.target.closest("td.dblclick-edit");
  if (!cell || cell.isContentEditable) return;
  // comment 셀: 링크로 표시 중인 내용을 원문(@[항목] 토큰) 평문으로 되돌려 편집·저장 라운드트립 보장.
  if (cell.dataset.raw != null) cell.textContent = cell.dataset.raw;
  cell.contentEditable = "true";
  cell.focus();
});

// comment 셀 편집 종료(focusout): 편집 중 원문(@[항목])을 data-raw 에 되저장하고 링크 표시로
// 복귀시킨다. 이래야 저장 버튼을 누르지 않아도(=전체 탭 재렌더 없이) 방금 입력한 @멘션이 곧바로
// 클릭 가능한 Item_detail 링크가 된다. mention 드롭다운 선택은 mousedown+preventDefault 로
// blur 를 막으므로 여기서 조기 종료되지 않는다.
document.querySelector(".content").addEventListener("focusout", e => {
  const cell = e.target.closest("td.dblclick-edit");
  if (!cell || !cell.isContentEditable || !isCommentCol(cell.dataset.col)) return;
  const raw = cell.textContent || "";
  cell.contentEditable = "false";
  cell.dataset.raw = raw;                  // 저장·재편집 라운드트립용 원문 갱신
  cell.innerHTML = linkifyComment(raw);    // @[항목] → 링크 표시로 복귀
  hideMention();
});

document.getElementById("btnDel").addEventListener("click", () => {
  if (!DATA) return;
  if (confirm("정말 삭제하시겠습니까?")) doDelete("");
});

document.getElementById("btnSaveComment").addEventListener("click", () => { saveNow(); });

// 저장 버튼 수동 클릭 — 편집 중인 comment 를 기다리지 않고 즉시 DB 반영.
// autoSave() 는 web_report 세션이면 saveIssueComments(), 아니면 buildPayload()+PATCH 를 태운다.
async function saveNow() {
  if (!DATA || MODE !== "edit" || _autoSaving) return;
  if (!_dirty) { showToast("변경된 내용이 없습니다."); return; }
  const btn = document.getElementById("btnSaveComment");
  if (btn) btn.disabled = true;
  await autoSave();
  if (btn) btn.disabled = false;
  showToast(_dirty ? "저장 실패 — 다시 시도해주세요." : "저장했습니다.");
}

document.getElementById("btnImportant").addEventListener("click", () => {
  if (!DATA) return;
  doSetImportant(!MY_IMPORTANT);
});

document.getElementById("btnPrivate").addEventListener("click", () => {
  if (!DATA) return;
  doSetPrivate(!(DATA.session && DATA.session.is_private));
});

// 개인 중요표시 토글 — 서버가 report_user_important 에 내 계정 기준으로만 저장한다.
async function doSetImportant(important) {
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/important`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ important }),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      showToast(j.error || "중요 표시 변경 실패");
      return;
    }
    const j = await res.json();
    MY_IMPORTANT = !!j.important;
    updateImportantBtn();
    showToast(MY_IMPORTANT ? "중요 표시했습니다 (내 화면에서만)." : "중요 표시를 해제했습니다.");
  } catch (e) {
    showToast("중요 표시 변경 실패: " + e.message);
  }
}

async function doSetPrivate(private_) {
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/private`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ private: private_ }),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      showToast(j.error || "비공개 표시 변경 실패");
      return;
    }
    const j = await res.json();
    if (DATA.session) DATA.session.is_private = j.is_private;
    updatePrivateBtn(DATA.session);
    showToast(j.is_private ? "비공개로 설정했습니다." : "공개로 전환했습니다.");
  } catch (e) {
    showToast("비공개 표시 변경 실패: " + e.message);
  }
}

// ── save / cancel ─────────────────────────────────────────────────────────────
let _dirty = false;        // 수정 모드에서 셀이 변경되면 true
let _autoSaving = false;   // 자동저장 진행 중 중복 방지

// edit bar 상태표시 dot
function _setDot(state) {
  const dot = document.getElementById("autosaveDot");
  if (dot) dot.className = "autosave-dot" + (state ? " " + state : "");
  const saveBtn = document.getElementById("btnSaveComment");
  if (saveBtn) saveBtn.classList.toggle("dirty", state === "dirty");
}

// dirty 마킹: 수정 모드에서 contenteditable 또는 input 변경 시
document.querySelector(".content").addEventListener("input", () => {
  if (MODE !== "edit") return;
  _dirty = true;
  _setDot("dirty");
});

// ── Issue Table comment @멘션: contenteditable comment 셀에서 '@' 입력 시 Testitem 검색 드롭다운 ──
// 선택하면 @[항목명] 토큰이 캐럿 위치에 삽입되고, 저장/표시 시 Item_detail 링크가 된다.
let _mentionCell = null;
function mentionCandidates() {
  const out = [], seen = new Set();
  const push = n => { n = String(n == null ? "" : n).trim(); if (n && !seen.has(n)) { seen.add(n); out.push(n); } };
  (distIndex || []).forEach(r => push(r.subject));
  if (rawDataMeta && Array.isArray(rawDataMeta.items)) rawDataMeta.items.forEach(it => push(it.name));
  if (etcItemMeta && Array.isArray(etcItemMeta.items)) etcItemMeta.items.forEach(it => push(it.name));
  return out;
}
function _mentionDD() {
  let dd = document.getElementById("mentionDropdown");
  if (!dd) {
    dd = document.createElement("div");
    dd.id = "mentionDropdown"; dd.className = "mention-dd"; dd.style.display = "none";
    document.body.appendChild(dd);
    dd.addEventListener("mousedown", ev => {   // blur 전에 처리하도록 mousedown 사용
      const btn = ev.target.closest(".mention-opt");
      if (!btn) return;
      ev.preventDefault();
      if (_mentionCell) mentionInsert(_mentionCell, btn.dataset.name);
      hideMention();
    });
  }
  return dd;
}
function hideMention() { const dd = document.getElementById("mentionDropdown"); if (dd) dd.style.display = "none"; _mentionCell = null; }
function mentionQueryAtCaret(cell) {
  const sel = window.getSelection();
  if (!sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  if (!cell.contains(range.startContainer)) return null;
  const node = range.startContainer;
  const before = (node.nodeType === 3) ? node.textContent.slice(0, range.startOffset) : (cell.textContent || "");
  const m = before.match(/@([^\[\]@\n]*)$/);   // 완결(@[..]) 안 된 마지막 @query
  return m ? m[1] : null;
}
function mentionInsert(cell, item) {
  const sel = window.getSelection();
  const token = `@[${item}] `;
  if (!sel.rangeCount) return;
  const range = sel.getRangeAt(0);
  const node = range.startContainer;
  if (node.nodeType !== 3) {
    cell.textContent = (cell.textContent || "").replace(/@([^\[\]@\n]*)$/, "") + token;
  } else {
    const off = range.startOffset, text = node.textContent;
    const nb = text.slice(0, off).replace(/@([^\[\]@\n]*)$/, token);
    node.textContent = nb + text.slice(off);
    const r = document.createRange();
    r.setStart(node, Math.min(nb.length, node.textContent.length)); r.collapse(true);
    sel.removeAllRanges(); sel.addRange(r);
  }
  cell.dispatchEvent(new Event("input", { bubbles: true }));   // _dirty 마킹(기존 리스너)
}
function showMention(cell, query) {
  const cands = mentionCandidates()
    .filter(n => !query || n.toLowerCase().includes(query.toLowerCase())).slice(0, 20);
  const dd = _mentionDD();
  if (!cands.length) { hideMention(); return; }
  dd.innerHTML = cands.map(n => `<button type="button" class="mention-opt" data-name="${esc(n)}">${esc(n)}</button>`).join("");
  const rect = cell.getBoundingClientRect();
  dd.style.left = (window.scrollX + rect.left) + "px";
  dd.style.top = (window.scrollY + rect.bottom) + "px";
  dd.style.display = "block";
  _mentionCell = cell;
}
document.querySelector(".content").addEventListener("input", e => {
  const cell = e.target.closest("td.dblclick-edit");
  if (!cell || !cell.isContentEditable || !isCommentCol(cell.dataset.col)) { hideMention(); return; }
  const q = mentionQueryAtCaret(cell);
  if (q === null) hideMention(); else showMention(cell, q);
});
document.addEventListener("keydown", e => { if (e.key === "Escape") hideMention(); });
document.addEventListener("click", e => {
  if (!e.target.closest("#mentionDropdown") && !e.target.closest("td.dblclick-edit")) hideMention();
});

// payload 조립 (saveEdits / autoSave 공용)
function buildPayload() {
  const payload = { password: verifiedPassword };

  if (isSummaryBlocks(DATA.summary_text)) {
    payload.summary_text = collectSummaryBlocks(document.getElementById("panel-summary"), DATA.summary_text);
  } else if (isGrid(DATA.summary_text)) {
    payload.summary_text = collectGrid(document.getElementById("panel-summary"), DATA.summary_text);
  } else {
    payload.summary_text = collectSummaryText();
  }

  if (Array.isArray(DATA.yield_text) && DATA.yield_text.length) {
    // Tabulator 그리드 편집 → 동일 구조(list-of-dicts)로 회수. (행 순서·서브헤더 보존)
    payload.yield_text = yieldGrid
      ? yieldGrid.getData()
      : collectSheetTable(document.getElementById("panel-yield"), DATA.yield_text);
  } else if (isGrid(DATA.yield_text)) {
    payload.yield_text = collectGrid(document.getElementById("panel-yield"), DATA.yield_text);
  } else {
    payload.yield_rows = readEditTable(document.getElementById("tbl-yield")) || [];
  }

  if (Array.isArray(DATA.issue_table_text) && DATA.issue_table_text.length) {
    payload.issue_table_text = collectSheetTable(document.getElementById("panel-issues"), DATA.issue_table_text);
  } else if (isGrid(DATA.issue_table_text)) {
    payload.issue_table_text = collectGrid(document.getElementById("panel-issues"), DATA.issue_table_text);
  } else {
    const issueTbl = document.getElementById("tbl-issue");
    if (issueTbl) payload.issue_rows = readEditTable(issueTbl);
  }

  return payload;
}

// web_report 세션 전용 저장: Issue Table comment(PTE/개발) 변경분만 모아
// manifest.issue_comments 로 보낸다 (PATCH /content 는 web_report 를 지원하지 않음).
// 변경 없으면 요청을 보내지 않는다. 실패 시 throw — 호출부가 dirty 복원.
async function saveIssueComments(opts) {
  opts = opts || {};
  const panel = document.getElementById("panel-issues");
  const rows = Array.isArray(DATA && DATA.issue_table_text) ? DATA.issue_table_text : [];
  const comments = [];
  const applied = [];   // 성공 시 DATA.issue_table_text 에도 반영해 재렌더 시 옛 값으로 되돌지 않게 함
  if (panel) {
    panel.querySelectorAll("td[data-key][data-col]").forEach(td => {
      const col = td.dataset.col;
      const ri = parseInt(td.dataset.r, 10);
      const orig = String(((rows[ri] || {})[col]) ?? "").trim();
      // 링크 표시 중인 comment 셀은 textContent 가 대괄호 없는 표시용 문자열(@항목)이므로
      // 원문(@[항목])을 보관한 data-raw 를 읽는다. 편집 중(contenteditable)이면 textContent 가 원문.
      const value = ((isCommentCol(col) && !td.isContentEditable && td.dataset.raw != null)
        ? td.dataset.raw : (td.textContent || "")).trim();
      if (value !== orig) {
        comments.push({ key: td.dataset.key, col, value });
        applied.push({ ri, col, value });
      }
    });
  }
  if (!comments.length) return { ok: true, updated: 0 };
  const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/issue_table/comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
    body: JSON.stringify({ password: verifiedPassword, comments }),
    keepalive: !!opts.keepalive,
  });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(j.error || `comment 저장 실패 (HTTP ${res.status})`);
  applied.forEach(({ ri, col, value }) => { if (rows[ri]) rows[ri][col] = value; });
  return j;
}

// 자동저장: 페이지가 숨겨질 때(탭 전환·창 최소화 등) 변경사항 자동 저장.
// keepalive:true 로 페이지 언로드 중에도 요청이 완료된다.
async function autoSave() {
  if (!DATA || MODE !== "edit" || !_dirty || _autoSaving) return;
  _autoSaving = true;
  _setDot("saving");
  if (isWebReportSession()) {
    _dirty = false;  // optimistic
    try {
      await saveIssueComments({ keepalive: true });
      _setDot("saved");
    } catch (_) {
      _dirty = true;
      _setDot("dirty");
    } finally {
      _autoSaving = false;
    }
    return;
  }
  const payload = buildPayload();
  _dirty = false;  // optimistic
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/content`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(payload),
      keepalive: true,
    });
    if (!res.ok) { _dirty = true; _setDot("dirty"); }
    else _setDot("saved");
  } catch (_) {
    _dirty = true;
    _setDot("dirty");
  } finally {
    _autoSaving = false;
  }
}

// 탭이 백그라운드로 가거나 창이 최소화될 때 자동저장
document.addEventListener("visibilitychange", () => {
  if (document.hidden) autoSave();
});

// 페이지를 떠날 때: dirty 상태면 브라우저 경고 + keepalive 저장 시도
window.addEventListener("beforeunload", e => {
  if (MODE === "edit" && _dirty) {
    autoSave();            // keepalive 요청 발사 (비동기, 브라우저가 완료 보장)
    e.preventDefault();
    e.returnValue = "";    // 브라우저 "변경사항이 저장되지 않을 수 있습니다" 경고
  }
});

async function doDelete(pin) {
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ password: pin }),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      showToast(j.error || "삭제 실패");
      return;
    }
    showToast("삭제되었습니다. 검색결과로 이동합니다.");
    setTimeout(() => { location.href = "/pe/report/"; }, 700);
  } catch (e) {
    showToast("삭제 실패: " + e.message);
  }
}

// ── Issue Table ETC 항목 추가/삭제 (web_report 전용) ─────────────────────────
// item 명만 서버(manifest.etc_items)에 저장하고, Bin/TNO/Distribution 은 항상
// 조회 시점에 tables/yield_rows/distDataCache 에서 자동으로 다시 채워진다.
let etcItemMeta = null;  // 전체 item 목록 캐시(rawDataMeta 있으면 그걸 재사용)

function currentEtcItems() {
  const rows = DATA && DATA.issue_table_text;
  if (!Array.isArray(rows)) return [];
  const idx = rows.findIndex(r => r && r["Category"] === "ETC");
  if (idx === -1) return [];
  return rows.slice(idx + 1).map(r => r && r["Item"]).filter(Boolean);
}

function closeEtcItemModal() { document.getElementById("etcItemModal").classList.remove("show"); }

async function openEtcItemModal() {
  document.getElementById("etcItemModal").classList.add("show");
  document.getElementById("etcItemSearch").value = "";
  const engrName = document.getElementById("etcEngrName");
  const engrComment = document.getElementById("etcEngrComment");
  if (engrName) engrName.value = "";
  if (engrComment) engrComment.value = "";
  _etcSetSelected(null);   // 선택-후-Comment 바 초기화
  const host = document.getElementById("etcItemList");
  host.innerHTML = `<div class="placeholder" style="padding:12px;">불러오는 중...</div>`;
  try {
    if (!etcItemMeta) etcItemMeta = rawDataMeta || await fetchRawDataMeta();
  } catch (e) {
    host.innerHTML = `<div class="placeholder" style="padding:12px;">항목 조회 실패: ${esc(e.message)}</div>`;
    return;
  }
  renderEtcItemList("");
}

function renderEtcItemList(filterText) {
  const host = document.getElementById("etcItemList");
  if (!host || !etcItemMeta) return;
  const q = String(filterText || "").trim().toLowerCase();
  const already = new Set(currentEtcItems());
  const items = (etcItemMeta.items || []).filter(it => !q || it.name.toLowerCase().includes(q));
  host.innerHTML = items.length ? items.map(it => {
    const added = already.has(it.name);
    return `<button type="button" class="rawdata-item${added ? " selected" : ""}" ` +
      `data-name="${esc(it.name)}" ${added ? "disabled" : ""}>${esc(it.name)}` +
      `${added ? ` <span class="rawdata-item-meta">(추가됨)</span>` : ""}</button>`;
  }).join("") : `<div class="placeholder" style="padding:12px;">일치하는 항목 없음</div>`;
}

// ETC 추가/삭제·Bin 상세 전환은 panel-issues 를 재렌더하므로, 아직 저장 안 된 comment
// 편집이 있으면 먼저 저장해 유실을 막는다. 저장 실패 시 false 를 반환해 조작을 중단시킨다.
async function flushPendingComments() {
  if (MODE !== "edit" || !_dirty || !isWebReportSession()) return true;
  try {
    await saveIssueComments();
    _dirty = false;
    _setDot("saved");
    return true;
  } catch (e) {
    showToast("저장되지 않은 comment 저장 실패 — 화면 전환을 중단합니다: " + e.message);
    return false;
  }
}

// ETC 항목 추가 + comment 저장. Item 명을 etc_items 에 추가하고, comment 가 있으면
// issue_comments(ETC|<item> / col)에 함께 저장(둘 다 DB=manifest 적재).
// col: 자유입력 Engr 항목="개발 comment"(기본), 측정항목 선택="PTE comment".
async function addEtcEngrItem(name, comment, col) {
  name = String(name || "").trim();
  comment = String(comment || "").trim();
  col = col || "개발 comment";
  if (!name) { showToast("Item 명을 입력하세요."); return; }
  if (!(await flushPendingComments())) return;
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/issue_table/etc`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ password: verifiedPassword, action: "add", item: name }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    if (comment) {
      const cres = await fetch(`/pe/report/session/${SESSION_ID}/web_report/issue_table/comments`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({ password: verifiedPassword,
          comments: [{ key: `ETC|${name}`, col, value: comment }] }),
      });
      const cj = await cres.json().catch(() => ({}));
      if (!cres.ok) throw new Error(cj.error || `comment 저장 실패 (HTTP ${cres.status})`);
    }
    showToast(`${name} 추가되었습니다.`);
    await load(false);
  } catch (e) {
    showToast("추가 실패: " + e.message);
  }
}

async function removeEtcItem(item) {
  if (!confirm(`"${item}" 항목을 Issue Table 에서 제거할까요?`)) return;
  if (!(await flushPendingComments())) return;
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/issue_table/etc`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ password: verifiedPassword, action: "remove", item }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    showToast(`${item} 제거되었습니다.`);
    await load(false);
  } catch (e) {
    showToast("제거 실패: " + e.message);
  }
}

document.getElementById("etcItemCancel").addEventListener("click", closeEtcItemModal);
document.getElementById("etcItemSearch").addEventListener("input", e => renderEtcItemList(e.target.value));

// Report 재생성 확인 모달: 아니요=닫기(편집 유지), 예=저장 후 재생성. 오버레이 바깥 클릭/ESC 도 취소.
document.getElementById("rawRegenCancel").addEventListener("click", closeRawRegenConfirm);
document.getElementById("rawRegenConfirm").addEventListener("click", () => {
  closeRawRegenConfirm();
  // 예 클릭 즉시 로드 오버레이로 화면 전환(저장·재계산 동안 "세션 불러오는 중" 표시).
  showLoadOverlay();
  startLoadCreep(6, 55, 5000, "세션 불러오는 중…");
  saveRawDataEdits();
});
document.getElementById("rawRegenModal").addEventListener("click", e => {
  if (e.target.id === "rawRegenModal") closeRawRegenConfirm();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && document.getElementById("rawRegenModal").classList.contains("show")) {
    closeRawRegenConfirm();
  }
});
// 측정항목 선택: 바로 닫지 않고 선택 상태로 전환 → PTE Comment 입력 후 '확인'에서 추가·닫기.
let _etcSelectedItem = null;
function _etcSetSelected(name) {
  _etcSelectedItem = name || null;
  const bar = document.getElementById("etcSelectedBar");
  document.querySelectorAll("#etcItemList .rawdata-item").forEach(b =>
    b.classList.toggle("picked", !!name && b.dataset.name === name));
  if (!name) { if (bar) bar.style.display = "none"; return; }
  const nameEl = document.getElementById("etcSelectedName");
  const cmt = document.getElementById("etcItemComment");
  if (nameEl) nameEl.textContent = name;
  if (cmt) cmt.value = "";
  if (bar) bar.style.display = "";
  if (cmt) cmt.focus();
}
document.getElementById("etcItemList").addEventListener("click", e => {
  const btn = e.target.closest(".rawdata-item");
  if (!btn || btn.disabled) return;
  _etcSetSelected(btn.dataset.name);
});
document.getElementById("etcItemConfirm").addEventListener("click", () => {
  if (!_etcSelectedItem) return;
  const name = _etcSelectedItem;
  const comment = document.getElementById("etcItemComment").value;
  _etcSetSelected(null);
  closeEtcItemModal();
  addEtcEngrItem(name, comment, "PTE comment");   // 측정항목 선택 → Comment 는 PTE comment 로
});
document.getElementById("etcItemReselect").addEventListener("click", () => _etcSetSelected(null));
document.getElementById("etcEngrAdd").addEventListener("click", () => {
  const name = document.getElementById("etcEngrName").value;
  const comment = document.getElementById("etcEngrComment").value;
  if (!String(name || "").trim()) { showToast("Item 명을 입력하세요."); return; }
  closeEtcItemModal();
  addEtcEngrItem(name, comment);
});


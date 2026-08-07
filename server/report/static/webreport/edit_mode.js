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
  // 렌더 본체는 조회 모드와 공용 (yield_issue.js renderIssueTableInto).
  renderIssueTableInto(document.getElementById(ISSUE_PANEL_MAIN), DATA.issue_table_text,
                       { edit: true });
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
  // Temperature 전용 — CT/HT 를 RT Limit 으로 전 항목 재판정한 이슈 표 (yield_issue.js).
  "issue-temp": () => renderIssueTempTab(),
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

// Plotly 로 그리는 탭들. plotly.min.js 는 async 로드라 시작 탭(표 기반)이 뜬 뒤에도
// 아직 도착하지 않았을 수 있다 — 그 사이 이 탭들이 렌더되면 차트가 비어버리므로
// dirty 를 유지한 채 도착을 기다렸다 다시 그린다. (표 탭은 Tabulator+canvas 만 쓴다)
const PLOTLY_TABS = { "distribution": 1, "map-analysis": 1, "compare": 1, "trim-analysis": 1 };

function renderTab(name) {
  if (!tabDirty[name] || !TAB_RENDERERS[name]) return;
  if (PLOTLY_TABS[name] && !window.Plotly) {
    // __plotlyReady 가 없는 경우(구 html 캐시)는 대기 없이 종전대로 진행한다.
    if (window.__plotlyReady) {
      window.__plotlyReady.then(() => renderTab(name));
      return;                      // tabDirty 유지 — 도착 후 이 함수가 다시 돈다
    }
  }
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
  const queue = ["yield", "issues", "issue-temp", "cpk", "distribution"];
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
  // comment 의 #[태그] 클릭 → Note 탭 + 해당 셀로 이동 (미저장 comment 먼저 flush).
  const tagLink = e.target.closest(".note-tag-link");
  if (tagLink) {
    const name = tagLink.dataset.tag;
    flushPendingComments().then(ok => { if (ok && name) noteJumpToTag(name); });
    return;
  }
  // comment 의 $[시트명] 클릭 → Note 탭 + 해당 시트로 이동 (Summary 의 시트 버튼과 동일 경로).
  const sheetLink = e.target.closest(".note-sheet-link");
  if (sheetLink) {
    const name = sheetLink.dataset.sheetName;
    flushPendingComments().then(ok => { if (ok && name) noteJumpToSheet(name); });
    return;
  }
  // "탭에서 편집 ›" 등 다른 탭으로 보내는 버튼 (Yield 탭 하단 Temp Corner 섹션).
  const gotoTab = e.target.closest("[data-goto-tab]");
  if (gotoTab) {
    const btn = document.querySelector(`.tab[data-tab="${gotoTab.dataset.gotoTab}"]`);
    if (btn) btn.click();
    return;
  }
  // Issue Table Yield 대표행 토글 → 그 Bin 의 detail TNO 행 펼치기/접기.
  const issueToggle = e.target.closest(".issue-toggle");
  if (issueToggle) { toggleIssueGroup(issueToggle); return; }
  // Issue Table Map 셀 소스별 펼치기.
  const mapExpandBtn = e.target.closest(".btn-map-expand");
  if (mapExpandBtn) { toggleMapExpand(mapExpandBtn); return; }
  // Issue Table Distribution 미니셀(산포) 클릭 → 그 Item 의 Item_detail.
  const distMiniAny = e.target.closest(".dist-cell-mini[data-subject]");
  const distMini = (distMiniAny && issuePanelOf(distMiniAny)) ? distMiniAny : null;
  if (distMini) {
    const subject = distMini.dataset.subject;
    flushPendingComments().then(ok => { if (ok && subject) openItemDetail(subject, [subject]); });
    return;
  }
  // Issue Table Map 미니셀 클릭 → Map Analysis 탭. Yield/ETC 행(data-bin)은 Bin Map 에서
  // 그 Bin 을 범례 선택 상태로, CPK 행(data-subject)은 STDF Map 에서 그 Item 을 선택 상태로.
  const mapMiniAny = e.target.closest(".map-cell-mini");
  const mapMini = (mapMiniAny && issuePanelOf(mapMiniAny)) ? mapMiniAny : null;
  if (mapMini) {
    if (mapMini.dataset.tempItem) openMapAnalysisForTempItem(mapMini.dataset.tempItem);
    else if (mapMini.dataset.subject) openMapAnalysisForItem(mapMini.dataset.subject);
    else openMapAnalysisForBin(mapMini.dataset.bin);
    return;
  }
  // Issue 표 툴바 버튼 — 두 패널(Issue Table / Issue Table Temp)이 같은 마크업을 쓰므로
  // 고정 id 가 아니라 data-issue-act + 소속 패널로 판별한다.
  const act = e.target.closest("[data-issue-act]");
  const actPanel = act ? issuePanelOf(act) : null;
  if (act && actPanel) {
    const kind = act.dataset.issueAct;
    if (kind === "toggle-all") {
      const expand = act.dataset.expanded !== "true";
      act.dataset.expanded = expand ? "true" : "false";
      act.textContent = expand ? "TNO 전체 접기" : "TNO 전체 펼치기";
      setAllIssueGroups(expand, actPanel);
    } else if (kind === "excel") {
      exportIssueExcel(issueRowsOf(actPanel),
                       actPanel.id === ISSUE_PANEL_TEMP ? ISSUE_TEMP_SHEET : "Issue Table");
    } else if (kind === "etc-add") { openEtcItemModal(); }
    else if (kind === "delmode") {
      const ui = issueUi(actPanel);
      ui.delMode = !ui.delMode;
      applyIssueDelMode(actPanel);
    } else if (kind === "del-selected") { deleteSelectedIssueRows(actPanel); }
    else if (kind === "sel-all") { setAllIssueDelChecked(true, actPanel); }
    else if (kind === "sel-none") { setAllIssueDelChecked(false, actPanel); }
    else if (kind === "sel-open") { bulkSetIssueStatus("Open", "selected", actPanel); }
    else if (kind === "sel-close") { bulkSetIssueStatus("Close", "selected", actPanel); }
    else if (kind === "all-open") { bulkSetIssueStatus("Open", "all", actPanel); }
    else if (kind === "all-close") { bulkSetIssueStatus("Close", "all", actPanel); }
    else if (kind === "reset-hidden") { resetHiddenIssueRows(); }
    return;
  }
  const jumpBtn = e.target.closest("[data-issue-jump]");
  if (jumpBtn) { jumpToIssueSection(jumpBtn.dataset.issueJump, issuePanelOf(jumpBtn)); return; }
  if (e.target.id === "yieldExcelBtn") { exportYieldExcel(); return; }
  if (e.target.id === "cpkExcelBtn") { exportCpkExcel(); return; }
  const etcDelBtn = e.target.closest(".btn-del-etc-item");
  if (etcDelBtn) { removeEtcItem(etcDelBtn.dataset.item); return; }
  // Issue Table Yield 대표행/CPK 행 숨김(삭제) + 숨김 전체 초기화 (편집모드 전용).
  const rowDelBtn = e.target.closest(".btn-del-issue-row");
  if (rowDelBtn) { hideIssueRow(rowDelBtn.dataset.hkey); return; }
  const delChk = e.target.closest(".issue-del-chk");
  if (delChk) { markIssueRowSelected(delChk); syncIssueDelCount(issuePanelOf(delChk)); return; }
  // 선택 모드: Step 셀 아무 곳이나 클릭해도 체크된다 — 체크박스가 작아 정확히 누르기 어렵다.
  // (Step 셀 안의 TNO 펼치기 ▼ 버튼은 위에서 먼저 처리되고 여기까지 오지 않는다.)
  const selCellAny = e.target.closest("td.issue-sel-cell");
  const selPanel = selCellAny ? issuePanelOf(selCellAny) : null;
  const selCell = (selPanel && issueUi(selPanel).delMode) ? selCellAny : null;
  if (selCell) {
    const chk = selCell.querySelector(".issue-del-chk");
    if (chk) { chk.checked = !chk.checked; markIssueRowSelected(chk); syncIssueDelCount(selPanel); }
    return;
  }
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

// Issue Table Status(Open/Close) 드랍다운 — 변경 즉시 저장 (편집모드 전용, 세션 편집 DB).
// change 는 버블되므로 위임 1회로 충분. select 의 change 는 comment _dirty 마킹과 무간섭.
document.querySelector(".content").addEventListener("change", async e => {
  const sel = e.target.closest("select.issue-status-sel");
  if (!sel || MODE !== "edit") return;
  const key = sel.dataset.skey;
  const value = sel.value;
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/issue_table/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ password: verifiedPassword, key, value }),
      keepalive: true,   // 변경 직후 탭을 닫아도 요청이 취소되지 않게 (autoSave 채널과 동일)
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    // 낙관 반영: 재렌더 없이 rows 데이터만 갱신 → Summary Open/Close 카운트 재계산 유도.
    // rows 는 그 셀이 속한 패널의 것(Issue Table / Issue Table Temp)이어야 한다.
    const td = sel.closest("td");
    const ri = td ? parseInt(td.dataset.r, 10) : NaN;
    const rows = issueRowsOf(issuePanelOf(sel));
    if (!isNaN(ri) && rows[ri]) rows[ri]["Status"] = value;
    setStatusDot(td, value);   // 드랍다운 아래 신호등 점 갱신
    tabDirty["summary"] = true;
  } catch (err) {
    sel.value = (value === "Close") ? "Open" : "Close";   // 실패 시 롤백
    setStatusDot(sel.closest("td"), sel.value);
    showToast("Status 저장 실패: " + err.message);
  }
});

// PTE/개발 comment 등 dblclick-edit 셀: 더블클릭 전에는 읽기전용 표시, 더블클릭 시에만
// contenteditable 활성화 (Distribution 등 다른 열이 즉시 편집 가능한 것과 구분).
document.querySelector(".content").addEventListener("dblclick", e => {
  if (MODE !== "edit") return;   // 읽기전용(비업로더) 모드에서는 편집 진입 차단
  const cell = e.target.closest("td.dblclick-edit");
  if (!cell || cell.isContentEditable) return;
  // comment 셀: 링크로 표시 중인 내용을 원문(@[항목] 토큰) 평문으로 되돌려 편집·저장 라운드트립 보장.
  if (cell.dataset.raw != null) cell.textContent = cell.dataset.raw;
  else cell.dataset.raw = cell.textContent || "";   // Escape 원복 기준 (raw 없는 셀도 확보)
  cell.contentEditable = "true";
  cell.focus();
});

// 붙여넣기: 엑셀 셀·서식 있는 텍스트를 그대로 넣으면 <div>/<span> 노드가 삽입되는데,
// 저장은 td.textContent 라 줄바꿈이 조용히 사라진다(= 붙여넣은 것과 저장된 것이 다름).
// 평문으로 정규화해 넣어 보이는 대로 저장되게 한다. execCommand 는 input 이벤트를
// 발생시키므로 기존 dirty 마킹·@멘션 감지가 그대로 동작한다.
document.querySelector(".content").addEventListener("paste", e => {
  const cell = e.target.closest("td.dblclick-edit");
  if (!cell || !cell.isContentEditable) return;
  const cb = e.clipboardData || window.clipboardData;
  if (!cb) return;
  e.preventDefault();
  const text = (cb.getData("text/plain") || "").replace(/\s+/g, " ").trim();
  if (text) document.execCommand("insertText", false, text);
});

// Escape: 편집 중인 셀을 편집 진입 시점 원문으로 되돌린다(값이 같아지므로 저장 요청 없음).
// @멘션 드롭다운이 열려 있으면 그것만 닫는 기존 동작 우선(document 리스너가 처리).
document.querySelector(".content").addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  const dd = document.getElementById("mentionDropdown");
  if (dd && dd.style.display === "block") return;
  // 서식 툴바가 떠 있으면 그것만 닫는다(document 리스너가 닫는다) — mention 과 같은 규칙.
  const fb = document.getElementById("cmtFmtBar");
  if (fb && fb.style.display === "block") return;
  const cell = e.target.closest("td.dblclick-edit");
  if (!cell || !cell.isContentEditable) return;
  cell.textContent = cell.dataset.raw || "";
  cell.blur();   // focusout 핸들러가 링크 표시로 복귀시킨다
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
  cell.innerHTML = linkifyComment(raw);    // @[항목] → 링크, *[..] → 색·굵기 표시로 복귀
  hideMention();
  hideCmtFmtBar();
  // 편집 종료 즉시 DB 저장 (web_report 만 — legacy PATCH /content 는 405 폐지).
  if (MODE === "edit" && _dirty && isWebReportSession()) saveCommentOnBlur();
});

document.getElementById("btnDel").addEventListener("click", () => {
  if (!DATA) return;
  if (confirm("정말 삭제하시겠습니까?")) doDelete("");
});

// 세션 정보 수정 — 편집 UI 는 Honey 앱(업로드 다이얼로그 재사용)에만 있다.
// Honey 안에서는 이 이동을 내장 브라우저가 가로채 취소하고(honey_main._browser_leave_guard)
// 편집창을 띄우므로 실제 요청은 나가지 않는다. 웹에서는 기존 Honey 전용 기능 안내를 띄운다.
document.getElementById("btnMetaEdit").addEventListener("click", () => {
  if (IDENTITY_SRC !== "honey") { try { HoneyHint.open(); } catch (e) {} return; }
  location.href = `/pe/report/honey/session_meta/${SESSION_ID}`;
});

document.getElementById("btnSaveComment").addEventListener("click", () => { saveNow(); });

// 저장 버튼 수동 클릭 — 편집 중인 comment 를 기다리지 않고 즉시 DB 반영.
// autoSave() 는 web_report 세션이면 comment/ENGR/차트주석 3채널 병렬 저장,
// 아니면 buildPayload()+PATCH 를 태운다.
async function saveNow() {
  if (!DATA || MODE !== "edit" || _autoSaving) return;
  if (!_dirty && !_cnDirty.size) { showToast("변경된 내용이 없습니다."); return; }
  const btn = document.getElementById("btnSaveComment");
  if (btn) btn.disabled = true;
  await autoSave();
  if (btn) btn.disabled = false;
  // 실패 사유는 autoSave 가 채널명과 함께 toast 로 알린다 — 여기서 덮어쓰지 않는다.
  if (!_dirty && !_cnDirty.size) showToast("저장했습니다.");
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

// dirty 마킹: 수정 모드에서 contenteditable 또는 input 변경 시.
// 검색·필터 입력은 저장할 내용이 아니므로 data-no-dirty 로 제외한다 — 안 그러면
// 검색만 하고 나가도 "저장하지 않은 변경" 모달이 떠서 경고를 무시하게 된다.
document.querySelector(".content").addEventListener("input", e => {
  if (MODE !== "edit") return;
  if (e.target && e.target.closest("[data-no-dirty]")) return;
  _dirty = true;
  _setDot("dirty");
});

// ── 태그 멘션: comment 셀 / Summary Engr Comment 에서 '@ # $' 입력 시 검색 드롭다운 ──
// 선택하면 @[항목명] / #[태그명] / $[시트명] 토큰이 캐럿 위치에 삽입되고, 저장/표시 시
// linkifyComment(sheets.js)가 각각 Item_detail·Note 셀·Note 시트 링크로 바꾼다.
// 트리거 문자 집합은 이 두 상수와 sheets.js linkifyComment 의 정규식이 짝이다.
// 단 서식 토큰(*[..]/*r[..])은 자동완성 대상이 아니다 — 플로팅 툴바·단축키로만 삽입되므로
// '*' 를 아래 문자클래스에 넣지 않는다. 넣으면 '*' 가 든 항목명의 자동완성이 깨지고,
// 안 넣어도 오작동이 없다(토큰 삽입 직후 캐럿 앞은 ']' 인데 ']' 가 이미 tail 매치를 끊는다).
const TRIGGER_RE = /([@#$])([^\[\]@#$\n]*)$/;        // 캐럿 앞의 미완결 트리거+쿼리
const TRIGGER_TAIL_RE = /[@#$]([^\[\]@#$\n]*)$/;     // 그 부분을 토큰으로 치환할 때
let _mentionCell = null;
// 태그를 입력할 수 있는 필드면 그 요소, 아니면 null.
// contenteditable comment 셀(Issue Table)과 textarea(Summary Engr Comment) 둘 다 대상.
function tagFieldOf(t) {
  if (!t) return null;
  if (t.matches && t.matches("textarea.engr-comment-input") && !t.readOnly) return t;
  const td = t.closest && t.closest("td.dblclick-edit");
  if (td && td.isContentEditable && isCommentCol(td.dataset.col)) return td;
  return null;
}
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
      if (_mentionCell) mentionInsert(_mentionCell, btn.dataset.name, dd.dataset.trigger || "@");
      hideMention();
    });
  }
  return dd;
}
function hideMention() { const dd = document.getElementById("mentionDropdown"); if (dd) dd.style.display = "none"; _mentionCell = null; }
function mentionQueryAtCaret(cell) {
  if (cell.tagName === "TEXTAREA") {   // Summary Engr Comment — selectionStart 기준
    const m = cell.value.slice(0, cell.selectionStart).match(TRIGGER_RE);
    return m ? { trigger: m[1], q: m[2] } : null;
  }
  const sel = window.getSelection();
  if (!sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  if (!cell.contains(range.startContainer)) return null;
  const node = range.startContainer;
  const before = (node.nodeType === 3) ? node.textContent.slice(0, range.startOffset) : (cell.textContent || "");
  const m = before.match(TRIGGER_RE);   // 완결(@[..] 등) 안 된 마지막 트리거+query
  return m ? { trigger: m[1], q: m[2] } : null;
}
function mentionInsert(cell, item, trigger) {
  const token = `${trigger || "@"}[${item}] `;
  if (cell.tagName === "TEXTAREA") {
    const head = cell.value.slice(0, cell.selectionStart).replace(TRIGGER_TAIL_RE, token);
    const tail = cell.value.slice(cell.selectionStart);
    cell.value = head + tail;
    cell.selectionStart = cell.selectionEnd = head.length;
    cell.dispatchEvent(new Event("input", { bubbles: true }));   // _dirty + 링크 칩 갱신
    return;
  }
  const sel = window.getSelection();
  if (!sel.rangeCount) return;
  const range = sel.getRangeAt(0);
  const node = range.startContainer;
  if (node.nodeType !== 3) {
    cell.textContent = (cell.textContent || "").replace(TRIGGER_TAIL_RE, "") + token;
  } else {
    const off = range.startOffset, text = node.textContent;
    const nb = text.slice(0, off).replace(TRIGGER_TAIL_RE, token);
    node.textContent = nb + text.slice(off);
    const r = document.createRange();
    r.setStart(node, Math.min(nb.length, node.textContent.length)); r.collapse(true);
    sel.removeAllRanges(); sel.addRange(r);
  }
  cell.dispatchEvent(new Event("input", { bubbles: true }));   // _dirty 마킹(기존 리스너)
}
// @ = Testitem 검색(Item_detail 링크), # = Note 앵커 태그(Note 셀 점프),
// $ = Note 시트 이름(Note 시트 점프).
function showMention(cell, query, trigger) {
  trigger = trigger || "@";
  const dd = _mentionDD();
  const match = n => !query || n.toLowerCase().includes(query.toLowerCase());
  let cands, emptyMsg = "";
  if (trigger === "#") {
    const tags = (typeof DATA !== "undefined" && DATA && DATA.note_tags) || {};
    cands = Object.keys(tags).filter(match).slice(0, 20);
    if (!cands.length) emptyMsg = "등록된 태그 없음 — Note 탭 [🔖 태그]로 생성";
  } else if (trigger === "$") {
    const sheets = noteSheetNames();
    if (sheets === null) {   // 목록 미도착 — 받아오고 드롭다운이 열려 있으면 다시 그린다
      cands = [];
      emptyMsg = "Note 시트 목록 불러오는 중…";
      noteEnsureSheetList().then(() => {
        if (_mentionCell === cell && dd.dataset.trigger === "$") showMention(cell, query, trigger);
      });
    } else {
      // 이름에 [ ] 가 있으면 $[..] 토큰으로 표현할 수 없다 — 후보에서 뺀다(버튼 줄로는 이동 가능).
      cands = sheets.map(s => s.name).filter(n => !/[\[\]]/.test(n)).filter(match).slice(0, 20);
      if (!cands.length) emptyMsg = "일치하는 Note 시트 없음";
    }
  } else {
    cands = mentionCandidates().filter(match).slice(0, 20);
    // 후보 0건이면 종전에는 조용히 닫혔다 — Summary 는 첫 화면이라 distIndex 가 아직
    // 안 채워졌을 수 있어(프리렌더 큐) 사용자가 고장으로 오해한다.
    if (!cands.length) emptyMsg = query ? "일치하는 항목 없음" : "항목 목록 준비 중 — 잠시 후 다시 시도";
  }
  if (!cands.length && !emptyMsg) { hideMention(); return; }
  dd.dataset.trigger = trigger;
  const pre = (trigger === "@") ? "" : trigger;
  dd.innerHTML = cands.length
    ? cands.map(n => `<button type="button" class="mention-opt" data-name="${esc(n)}">${pre}${esc(n)}</button>`).join("")
    : `<div class="mention-empty">${esc(emptyMsg)}</div>`;
  const rect = cell.getBoundingClientRect();
  dd.style.left = (window.scrollX + rect.left) + "px";
  dd.style.top = (window.scrollY + rect.bottom) + "px";
  dd.style.display = "block";
  _mentionCell = cell;
}
document.querySelector(".content").addEventListener("input", e => {
  const cell = tagFieldOf(e.target);
  if (!cell) { hideMention(); return; }
  const q = mentionQueryAtCaret(cell);
  if (q === null) hideMention(); else showMention(cell, q.q, q.trigger);
});
document.addEventListener("keydown", e => { if (e.key === "Escape") { hideMention(); hideCmtFmtBar(); } });
document.addEventListener("click", e => {
  if (!e.target.closest("#mentionDropdown") && !e.target.closest("td.dblclick-edit")
      && !e.target.closest("textarea.engr-comment-input")) hideMention();
});

// ── comment 서식 툴바: 편집 중 셀에서 글자를 선택하면 뜨는 플로팅 버튼 ─────────
// 토큰 문법·문자열 조작은 전부 sheets.js 순수 함수(cmtFormatRange)에 있고 여기는 DOM 글루만.
// mention 드롭다운과 같은 방식이다 — body 직속 절대위치 + mousedown+preventDefault 로 blur 회피.
// (둘이 동시에 뜨는 일은 없다: mention 은 캐럿이 collapsed 일 때, 이건 선택 구간이 있을 때.)
const CMT_FMT_BTNS = [
  ["",     "B", "굵게 (Ctrl+B)"],
  ["r",    "●", "빨강 (Ctrl+Shift+1)"],
  ["o",    "●", "주황 (Ctrl+Shift+2)"],
  ["g",    "●", "초록 (Ctrl+Shift+3)"],
  ["b",    "●", "파랑 (Ctrl+Shift+4)"],
  ["none", "✕", "서식 제거 (Ctrl+Shift+0)"],
];
let _cmtFmtCell = null;
function _cmtFmtBarEl() {
  let bar = document.getElementById("cmtFmtBar");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "cmtFmtBar"; bar.className = "cmt-fmt-bar"; bar.style.display = "none";
    bar.innerHTML = CMT_FMT_BTNS.map(([act, label, title]) =>
      `<button type="button" class="cmt-fmt-btn" data-act="${act}" title="${esc(title)}">${label}</button>`).join("");
    document.body.appendChild(bar);
    bar.addEventListener("mousedown", ev => {   // blur 전에 처리하도록 mousedown
      const btn = ev.target.closest(".cmt-fmt-btn");
      if (!btn) return;
      ev.preventDefault();
      if (_cmtFmtCell) cmtApplyFormat(_cmtFmtCell, btn.dataset.act);
    });
  }
  return bar;
}
function hideCmtFmtBar() {
  const bar = document.getElementById("cmtFmtBar");
  if (bar) bar.style.display = "none";
  _cmtFmtCell = null;
}
// 노드가 속한 편집 중 comment 셀 (텍스트 노드·엘리먼트 둘 다 받는다).
function cmtCellOfNode(node) {
  const el = (node && node.nodeType === 3) ? node.parentElement : node;
  const td = (el && el.closest) ? el.closest("td.dblclick-edit") : null;
  return (td && td.isContentEditable && isCommentCol(td.dataset.col)) ? td : null;
}
// 셀 시작부터 (node, off) 까지의 문자 수 — Range.toString() 이라 cell.textContent 와 같은 좌표계다.
function _cmtLenTo(cell, node, off) {
  const r = document.createRange();
  r.setStart(cell, 0); r.setEnd(node, off);
  return r.toString().length;
}
function cmtSelOffsets(cell, range) {
  return { start: _cmtLenTo(cell, range.startContainer, range.startOffset),
           end:   _cmtLenTo(cell, range.endContainer, range.endOffset) };
}
// 선택 구간에 서식을 적용하고 캐럿을 토큰 끝으로 옮긴다.
// textContent 로 통째 교체해 텍스트 노드 1개로 정규화한다(붙여넣기 잔재 노드도 같이 정리).
// 저장은 건드리지 않는다 — dispatch 한 input 이 기존 dirty 마킹을 태우고, focusout 이 저장한다.
function cmtApplyFormat(cell, action) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return;
  const range = sel.getRangeAt(0);
  if (!cell.contains(range.startContainer) || !cell.contains(range.endContainer)) return;
  const off = cmtSelOffsets(cell, range);
  const res = cmtFormatRange(cell.textContent || "", off.start, off.end,
                             action === "none" ? null : action);
  if (!res) {
    showToast("이 구간에는 서식을 넣을 수 없습니다 — 대괄호나 @[]/#[] 토큰과 겹칩니다.");
    return;
  }
  cell.textContent = res.text;
  const tn = cell.firstChild;
  if (tn) {
    const r = document.createRange();
    r.setStart(tn, Math.min(res.caret, tn.length)); r.collapse(true);
    sel.removeAllRanges(); sel.addRange(r);
  }
  cell.dispatchEvent(new Event("input", { bubbles: true }));   // _dirty 마킹 + mention 재판정
  hideCmtFmtBar();
}
// 선택 변화 감지는 selectionchange 여야 한다 — mouseup 만으로는 키보드 선택(Shift+←/→)을 놓친다.
// 적용 불가한 선택(링크 토큰 겹침·대괄호 포함)에는 아예 안 띄운다(비활성 버튼을 그리지 않는다).
document.addEventListener("selectionchange", () => {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount || sel.isCollapsed) { hideCmtFmtBar(); return; }
  const range = sel.getRangeAt(0);
  const cell = cmtCellOfNode(range.startContainer);
  if (!cell || !cell.contains(range.endContainer)) { hideCmtFmtBar(); return; }
  const off = cmtSelOffsets(cell, range);
  if (!cmtFormatRange(cell.textContent || "", off.start, off.end, "")) { hideCmtFmtBar(); return; }
  const bar = _cmtFmtBarEl();
  _cmtFmtCell = cell;
  bar.style.display = "block";
  let rect = range.getBoundingClientRect();
  if (!rect.width && !rect.height) rect = cell.getBoundingClientRect();
  // 셀 폭이 330px 고정(.st-comment)이라 툴바가 셀보다 넓을 수 있어 뷰포트로 클램프한다.
  const bw = bar.offsetWidth, bh = bar.offsetHeight;
  const maxLeft = window.scrollX + document.documentElement.clientWidth - bw - 4;
  bar.style.left = Math.max(window.scrollX + 4,
                            Math.min(window.scrollX + rect.left, maxLeft)) + "px";
  bar.style.top = (window.scrollY + (rect.top - bh - 6 > 0 ? rect.top - bh - 6 : rect.bottom + 6)) + "px";
});
// 서식 단축키. Ctrl+B 는 contenteditable 기본이 <b> 를 삽입하는데 저장은 textContent 라
// 조용히 사라진다 — 우리 토큰으로 가로채면서 그 구멍도 같이 막는다. Ctrl+I/U 는 대응
// 기능이 없으니 기본 동작만 차단한다(보이지 않는 HTML 이 생기는 것 방지).
// Ctrl+1~8 은 Edge 탭 전환이라 색은 Ctrl+Shift+숫자 로 잡는다.
// ⚠️ e.key 가 아니라 **e.code**(물리 키)로 판정한다 — Shift+1 의 e.key 는 레이아웃에 따라
// "!" 이고, 한글 IME 입력 상태에서는 B 의 e.key 가 "ㅠ" 로 올 수 있다.
const CMT_FMT_KEYS = { Digit1: "r", Digit2: "o", Digit3: "g", Digit4: "b", Digit0: "none" };
const CMT_FMT_BLOCK_CODES = { KeyB: 1, KeyI: 1, KeyU: 1 };
document.querySelector(".content").addEventListener("keydown", e => {
  if (!(e.ctrlKey || e.metaKey)) return;
  const cell = cmtCellOfNode(e.target);
  if (!cell) return;
  if (!e.shiftKey && CMT_FMT_BLOCK_CODES[e.code]) {
    e.preventDefault();
    if (e.code === "KeyB") cmtApplyFormat(cell, "");
    return;
  }
  if (e.shiftKey && Object.prototype.hasOwnProperty.call(CMT_FMT_KEYS, e.code)) {
    e.preventDefault();
    cmtApplyFormat(cell, CMT_FMT_KEYS[e.code]);
  }
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
  const comments = [];
  const applied = [];   // 성공 시 rows 에도 반영해 재렌더 시 옛 값으로 되돌지 않게 함
  // Issue Table + Issue Table Temp 두 패널을 함께 훑는다 — 서버는 row_key 로 저장하므로
  // 요청 형식·엔드포인트는 종전과 같다(패널이 하나뿐이면 동작도 완전히 같다).
  issuePanelEls().forEach(panel => {
    const rows = issueRowsOf(panel);
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
        applied.push({ rows, ri, col, value });
      }
    });
  });
  if (!comments.length) return { ok: true, updated: 0 };
  const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/issue_table/comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
    body: JSON.stringify({ password: verifiedPassword, comments }),
    keepalive: !!opts.keepalive,
  });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(j.error || `comment 저장 실패 (HTTP ${res.status})`);
  applied.forEach(({ rows, ri, col, value }) => { if (rows[ri]) rows[ri][col] = value; });
  return j;
}

// 자동저장: 페이지가 숨겨질 때(탭 전환·창 최소화 등) 변경사항 자동 저장.
// web_report 세션은 3채널(Issue comment / Summary ENGR / 차트 주석)을 병렬로 flush —
// 언로드 중 직렬 await 는 첫 요청 뒤 중단되므로 병렬이어야 keepalive 요청이 전부 나간다.
// 각 함수는 자체 diff 로 변경 없으면 요청을 보내지 않는다.
//
// opts.unload: 페이지 이탈/숨김 경로에서만 true — 그때만 fetch keepalive 를 켠다.
// keepalive 요청은 브라우저가 본문 합계 64KiB 로 제한하므로(Fetch 표준), 큰 comment
// 묶음이나 차트 주석 다발은 keepalive 를 켜면 오히려 네트워크 오류로 실패한다.
// 수동 저장·blur 저장처럼 페이지가 살아 있는 경로에서는 일반 요청으로 보낸다.
async function autoSave(opts) {
  opts = opts || {};
  const ka = !!opts.unload;
  if (!DATA || MODE !== "edit" || _autoSaving) return;
  if (!_dirty && !_cnDirty.size) return;   // 차트 주석은 자체 dirty 채널(_cnDirty, chart_notes.js)
  _autoSaving = true;
  _setDot("saving");
  if (isWebReportSession()) {
    _dirty = false;  // optimistic
    const jobs = [
      ["Issue comment", () => saveIssueComments({ keepalive: ka })],
      ["Engr Comment", () => saveSummaryEngr({ keepalive: ka })],       // map_select.js
      ["차트 주석", () => cnFlush({ keepalive: ka })],                   // chart_notes.js
    ];
    try {
      // allSettled — 한 채널이 실패해도 나머지는 끝까지 보낸다(Promise.all 은 fail-fast).
      const results = await Promise.allSettled(jobs.map(([, fn]) => fn()));
      const failed = jobs.filter((_, i) => results[i].status === "rejected");
      if (!failed.length) { _setDot("saved"); return; }
      _dirty = true;
      _setDot("dirty");
      const first = results.find(r => r.status === "rejected");
      const why = (first && first.reason && first.reason.message) || "알 수 없는 오류";
      showToast(`자동저장 실패(${failed.map(j => j[0]).join("·")}): ${why}`, 5000);
      _scheduleRetry();
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
      keepalive: ka,
    });
    if (!res.ok) { _dirty = true; _setDot("dirty"); showToast("자동저장 실패 — 변경사항이 저장되지 않았습니다"); }
    else _setDot("saved");
  } catch (_) {
    _dirty = true;
    _setDot("dirty");
    showToast("자동저장 실패 — 변경사항이 저장되지 않았습니다");
  } finally {
    _autoSaving = false;
  }
}

// 저장 실패 후 1회 재시도 — 일시적 네트워크 단절·CSRF 재발급 직후를 자동 복구한다.
// 각 채널 저장이 diff 기반 멱등이라 중복 전송 위험이 없다. 재시도도 실패하면 그대로
// dirty 로 남겨 사용자 조작(다음 blur·💾)을 기다린다(무한 재시도 금지).
let _retryTimer = null;
function _scheduleRetry() {
  if (_retryTimer) return;
  _retryTimer = setTimeout(() => {
    _retryTimer = null;
    if (MODE === "edit" && (_dirty || _cnDirty.size)) autoSave();
  }, 5000);
}

// comment 셀 blur 즉시 저장 — autoSave 재사용(dot: dirty→saving→saved, _autoSaving 중복 방지,
// 실패 시 _dirty 복원 + 채널명·사유 toast + 5초 뒤 1회 재시도).
async function saveCommentOnBlur() {
  await autoSave();
}

// 탭이 백그라운드로 가거나 창이 최소화될 때 자동저장
document.addEventListener("visibilitychange", () => {
  if (document.hidden) autoSave({ unload: true });
});

// 페이지를 떠날 때: dirty 상태면 브라우저 경고 + keepalive 저장 시도
window.addEventListener("beforeunload", e => {
  if (leaveGuardBypassed()) return;   // 이탈 확인 모달에서 확정 — 중복 경고·재저장 방지
  if (MODE === "edit" && (_dirty || _cnDirty.size)) {
    autoSave({ unload: true });   // keepalive 요청 발사 (비동기, 브라우저가 완료 보장)
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
  // 항목이 수백 개라 목록을 훑는 것보다 검색이 빠르다 — 열자마자 바로 타이핑되게 포커스.
  document.getElementById("etcItemSearch").focus();
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
// Engr Comment 도 같은 _dirty 를 쓰므로 함께 flush 한다 — 안 그러면 Engr 를 고친 직후
// 링크를 눌렀을 때 그 편집이 저장 없이 dirty 만 풀린다(변경 없으면 요청도 안 나간다).
async function flushPendingComments() {
  if (MODE !== "edit" || !_dirty || !isWebReportSession()) return true;
  try {
    await saveIssueComments();
    await saveSummaryEngr();
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

// Issue Table Yield 대표행(bin 단위)/CPK 행 숨김(삭제) — 세션 편집 DB(issue_hidden)에
// 키만 기록하고 재로드로 반영한다. 행별 복원은 없고 resetHiddenIssueRows(전체 초기화)뿐.
async function hideIssueRow(key) {
  if (!key) return;
  if (!confirm(`이 행을 Issue Table 에서 삭제(숨김)할까요?\n(${key})\n※ 복원은 툴바 "삭제 전체 초기화"로만 가능합니다.`)) return;
  if (!(await flushPendingComments())) return;
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/issue_table/hidden`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ password: verifiedPassword, action: "hide", key }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    showToast("행이 삭제(숨김)되었습니다.");
    await load(false);
  } catch (e) {
    showToast("행 삭제 실패: " + e.message);
  }
}

// Status 셀 신호등 점 갱신 — 색은 td 의 is-open/is-close 클래스가 결정한다(CSS).
function setStatusDot(td, value) {
  if (!td) return;
  const close = value === "Close";
  td.classList.toggle("is-close", close);
  td.classList.toggle("is-open", !close);
}

// 삭제 모드에서 체크한 행 일괄 삭제 — Yield/CPK 행은 issue_hidden, ETC 항목은 etc remove.
// 백엔드가 단건 API 라 순차 호출한다(세션 편집 DB read-modify-write 경합 방지). 재로드는 마지막 1회.
async function deleteSelectedIssueRows(panel) {
  panel = panel || activeIssuePanel();
  const checked = panel ? [...panel.querySelectorAll(".issue-del-chk:checked")] : [];
  if (!checked.length) { showToast("삭제할 행을 체크하세요."); return; }
  if (!confirm(`체크한 ${checked.length}개 행을 Issue Table 에서 삭제할까요?\n※ 삭제한 행 복원은 "삭제 전체 초기화"로만 가능합니다.`)) return;
  if (!(await flushPendingComments())) return;
  const btn = panel.querySelector('[data-issue-act="del-selected"]');
  if (btn) btn.disabled = true;
  let done = 0;
  try {
    for (const chk of checked) {
      const hkey = chk.dataset.hkey || "";
      const item = chk.dataset.etc || "";
      const url = hkey
        ? `/pe/report/session/${SESSION_ID}/web_report/issue_table/hidden`
        : `/pe/report/session/${SESSION_ID}/web_report/issue_table/etc`;
      const body = hkey
        ? { password: verifiedPassword, action: "hide", key: hkey }
        : { password: verifiedPassword, action: "remove", item };
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify(body),
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
      done += 1;
    }
    showToast(`${done}개 행을 삭제했습니다.`);
  } catch (e) {
    showToast(`일괄 삭제 실패 (${done}개 처리 후 중단): ` + e.message);
  } finally {
    if (btn) btn.disabled = false;
    if (done) await load(false);
  }
}

// Issue Table Status 일괄 변경 (편집모드 전용) — scope "selected" = 체크한 행, "all" = 표 전체.
// 대상 행의 Status 드랍다운을 모아 배치 API(items)로 한 번에 저장하고, 재렌더 없이 화면
// (드랍다운·신호등·DATA)만 갱신한다 — 단건 변경(change 위임)과 같은 낙관 반영 방식.
async function bulkSetIssueStatus(value, scope, panel) {
  panel = panel || activeIssuePanel();
  if (!panel) return;
  let sels;
  if (scope === "selected") {
    const checked = [...panel.querySelectorAll(".issue-del-chk:checked")];
    if (!checked.length) { showToast("Status 를 바꿀 행을 선택하세요."); return; }
    sels = checked.map(chk => chk.closest("tr").querySelector("select.issue-status-sel")).filter(Boolean);
  } else {
    sels = [...panel.querySelectorAll("select.issue-status-sel")];
  }
  const targets = sels.filter(sel => sel.value !== value && sel.dataset.skey);
  if (!targets.length) { showToast(`바꿀 행이 없습니다 (이미 모두 ${value}).`); return; }
  const what = scope === "selected" ? "선택한" : "전체";
  if (!confirm(`${what} ${targets.length}개 행의 Status 를 ${value} 로 변경할까요?`)) return;
  if (!(await flushPendingComments())) return;
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/issue_table/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({
        password: verifiedPassword,
        items: targets.map(sel => ({ key: sel.dataset.skey, value })),
      }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    const rows = issueRowsOf(panel);
    targets.forEach(sel => {
      sel.value = value;
      const td = sel.closest("td");
      const ri = td ? parseInt(td.dataset.r, 10) : NaN;
      if (!isNaN(ri) && rows[ri]) rows[ri]["Status"] = value;
      setStatusDot(td, value);
    });
    tabDirty["summary"] = true;
    showToast(`${targets.length}개 행을 ${value} 로 바꿨습니다.`);
  } catch (err) {
    showToast("Status 일괄 저장 실패: " + err.message);
  }
}

async function resetHiddenIssueRows() {
  // 숨김은 kind 1개(issue_hidden)를 두 패널이 공유하므로 reset 은 **양쪽 모두** 복원한다.
  const what = (webReportMode() === "Temperature") ? "Yield/CPK/TEMP" : "Yield/CPK";
  if (!confirm(`삭제(숨김)한 ${what} 행을 전부 복원할까요?`)) return;
  if (!(await flushPendingComments())) return;
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/issue_table/hidden`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ password: verifiedPassword, action: "reset_all" }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    showToast(j.storage === "unchanged" ? "삭제된 행이 없습니다." : "삭제한 행을 전부 복원했습니다.");
    if (j.storage !== "unchanged") await load(false);
  } catch (e) {
    showToast("초기화 실패: " + e.message);
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


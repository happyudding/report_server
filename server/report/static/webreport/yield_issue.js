// ── Yield: STEP(P1/P2/P3) 별 Bin 접기/펼치기 표 렌더 (web_report) ────────────────
// STEP 마다 표 1개. 각 표는 Bin 당 most-fail TNO 대표 행(접힘) + ▼ 토글로 그 Bin 의 나머지
// fail TNO 행을 펼친다. bin portion 은 그 STEP 진입 die 수 기준(cascade 수율, 서버 계산).
// Pass 행은 표에 넣지 않고 상단 요약 박스가 담당한다(전체 + STEP 별 통과율).
let yieldPanelBound = false;

// Yield 표 컬럼 순서(step/bin/tno/item → source yield/count → avg). 모든 STEP 표가 같은
// 컬럼을 쓰도록 전체 행에서 한 번만 계산한다.
function yieldColumnsFrom(allRows) {
  let cols = [];
  (allRows || []).forEach(r => Object.keys(r || {}).forEach(k => { if (!cols.includes(k)) cols.push(k); }));
  return orderColumns(cols, "yield");
}

// STEP 표 최상단에 붙일 Bin1(Pass) 행 — Issue Table Yield 섹션 맨 위 Pass 행과 같은 역할.
// 값은 yield_summary.by_step(서버 yield_step_summary) 의 STEP 별 수율 = (전체 - 그 STEP fail)
// / 전체 die. 소스별 yield_pct/survivor 를 {src}_yield/{src}_count 컬럼에 옮긴다.
function yieldStepPassRow(step) {
  const ov = DATA.web_report && DATA.web_report.yield_summary;
  const byStep = (ov && Array.isArray(ov.by_step)) ? ov.by_step : [];
  const st = byStep.find(s => String(s.step || "") === String(step || ""));
  if (!st) return null;
  const row = { step: "", bin: "1", TNO: "", Item: "Pass", avg: st.avg_yield_pct };
  (st.sources || []).forEach(s => {
    row[`${s.source}_yield`] = s.yield_pct;
    row[`${s.source}_count`] = s.survivor;
  });
  return row;
}

// 한 STEP 표를 렌더. si = 섹션 인덱스 — 그룹 토글 data-grp 를 표들 사이에서 유일하게 만든다.
// passRow = 표 맨 위 Bin1 행(yieldStepPassRow, 없으면 null).
function renderYieldTable(cols, groups, si, passRow) {
  const colgroup = "<colgroup>" + cols.map(c => `<col style="width:${colWidth(c)}">`).join("") + "</colgroup>";
  const head = buildSheetTableHead(cols);

  // 각 source _yield 컬럼 + avg 컬럼별로 빨강 그라데이션(값 클수록 진함) 정규화 기준 =
  // 그 컬럼 내 최댓값. 표에 실린 모든 행(대표 + detail)을 기준으로 컬럼별 max 를 구한다.
  const gradCols = cols.filter(c => /_yield$/i.test(String(c)) || String(c).trim().toLowerCase() === "avg");
  const gradSet = new Set(gradCols);
  const colMax = {};
  gradCols.forEach(c => { colMax[c] = 0; });
  (groups || []).forEach(g => {
    const rows = [g.rep].concat((g.rows || []).slice(1));
    rows.forEach(r => gradCols.forEach(c => {
      const n = parseFloat(r ? r[c] : "");
      if (!isNaN(n) && n > colMax[c]) colMax[c] = n;
    }));
  });

  // isPass = Bin1 행 — fail 행 전용 장식(Item 상세 링크 / 빨강 그라데이션)을 뺀다.
  const cellTds = (r, toggleHtml, isPass) => cols.map(c => {
    const v = r ? r[c] : "";
    const txt = (v === null || v === undefined) ? "" : String(v);
    const isEmpty = txt === "";
    const cLower = String(c).trim().toLowerCase();
    const cls = [];
    let cellStyle = "";
    if (isEmpty) cls.push("st-empty"); else if (isNumVal(v)) cls.push("st-num");
    let inner = isEmpty ? "" : esc(txt);
    // Item 셀 → Item_detail 링크 (Bin1 Pass 행은 측정 항목이 아니라 제외)
    if (cLower === "item" && !isEmpty && !isPass) {
      inner = `<span class="item-detail-link" data-subject="${esc(txt)}">${esc(txt)}</span>`;
    }
    // source _yield / avg 셀: 각 컬럼 내 최댓값 대비 빨강 그라데이션(불량률 높을수록 진함).
    if (!isEmpty && !isPass && gradSet.has(c)) {
      const num = parseFloat(v);
      if (!isNaN(num) && num > 0 && colMax[c] > 0) {
        const ratio = Math.min(1, num / colMax[c]);
        cls.push("yield-grad");
        cellStyle = ` style="--yw:${ratio.toFixed(3)}"`;
      }
    }
    // 접기/펼치기 토글은 STEP 셀 오른쪽에 배치(우측 정렬).
    if (toggleHtml && cLower === "step") inner = inner + toggleHtml;
    return `<td${cls.length ? ` class="${cls.join(" ")}"` : ""}${cellStyle}>${inner}</td>`;
  }).join("");

  let body = "";
  if (passRow) body += `<tr class="yield-pass-row">${cellTds(passRow, "", true)}</tr>`;
  (groups || []).forEach((g, gi) => {
    const grp = `${si}_${gi}`;   // 표 간 유일한 그룹 id
    const detail = (g.rows || []).slice(1);   // 대표(most-fail) 제외 나머지 fail TNO
    const toggle = detail.length
      ? ` <button type="button" class="yield-toggle" data-grp="${grp}" aria-expanded="false">▼</button>` : "";
    body += `<tr class="yield-bin-rep" data-grp="${grp}">${cellTds(g.rep, toggle)}</tr>`;
    detail.forEach(r => {
      body += `<tr class="yield-bin-detail" data-grp="${grp}" style="display:none">${cellTds(r, "")}</tr>`;
    });
  });

  return `<div class="sheet-wrap kind-yield"><table class="sheet-table kind-yield">${colgroup}${head}<tbody>${body}</tbody></table></div>`;
}

// STEP 별 표 섹션(제목 + 표)을 순서대로 렌더 (P1→P2→P3).
function renderYieldStepSections(stepGroups, allRows) {
  const cols = yieldColumnsFrom(allRows);
  if (!cols.length || !Array.isArray(stepGroups) || !stepGroups.length) return "";
  return stepGroups.map((sg, si) => {
    const groups = sg.groups || [];
    const label = String(sg.step || "").trim() || "(기타)";
    return `<div class="yield-step-section">` +
      `<div class="yield-step-title">STEP ${esc(label)}</div>` +
      renderYieldTable(cols, groups, si, yieldStepPassRow(sg.step)) + `</div>`;
  }).join("");
}

// Yield 탭 상단 툴바: 모든 Bin 그룹의 FAILTNO 상세행을 한 번에 펼치기/접기하는 토글.
function yieldToolbarHtml() {
  return `<div class="yield-toolbar">` +
    `<button type="button" class="btn-sm" id="yieldToggleAll" data-expanded="false">전체 펼치기</button>` +
    `</div>`;
}
// 한 Bin 그룹(대표행)의 detail FAILTNO 행 펼치기/접기 + 그룹 토글 버튼 상태 갱신.
function setYieldGroup(gi, expand, btn) {
  if (btn) { btn.setAttribute("aria-expanded", expand ? "true" : "false"); btn.textContent = expand ? "▲" : "▼"; }
  document.querySelectorAll(`#panel-yield tr.yield-bin-detail[data-grp="${gi}"]`).forEach(tr => {
    tr.style.display = expand ? "" : "none";
  });
}
function setAllYieldGroups(expand) {
  document.querySelectorAll("#panel-yield .yield-toggle").forEach(btn => setYieldGroup(btn.dataset.grp, expand, btn));
}
function bindYieldPanel() {
  if (yieldPanelBound) return;
  const panel = document.getElementById("panel-yield");
  if (!panel) return;
  panel.addEventListener("click", e => {
    const all = e.target.closest("#yieldToggleAll");
    if (all) {
      const expand = all.dataset.expanded !== "true";
      all.dataset.expanded = expand ? "true" : "false";
      all.textContent = expand ? "전체 접기" : "전체 펼치기";
      setAllYieldGroups(expand);
      return;
    }
    const btn = e.target.closest(".yield-toggle");
    if (!btn) return;
    setYieldGroup(btn.dataset.grp, btn.getAttribute("aria-expanded") !== "true", btn);
  });
  yieldPanelBound = true;
}

// ── Yield (read) ──────────────────────────────────────────────────────────────
function renderYield(yield_text, summary_rows) {
  const panel = document.getElementById("panel-yield");
  const overview = yieldOverviewHtml();

  // web_report: STEP(P1/P2/P3) 별 분리 표 (yield_step_groups 가 있을 때)
  const stepGroups = DATA.web_report && DATA.web_report.yield_step_groups;
  if (Array.isArray(stepGroups) && stepGroups.length && Array.isArray(yield_text)) {
    bindYieldPanel();
    panel.innerHTML = overview + yieldToolbarHtml() + renderYieldStepSections(stepGroups, yield_text);
    return;
  }

  // list of dicts (STEP 그룹이 없거나 fail 이 전혀 없을 때의 폴백 — Pass 행 포함 평면 표)
  if (Array.isArray(yield_text) && yield_text.length) {
    panel.innerHTML = overview + renderSheetTable(yield_text, { kind: "yield" });
    return;
  }

  emptyPanel(panel, "Yield 데이터 없음");
}

// 섹션 2행 헤더 블록의 하단행(Avg/source)은 상단행(그룹 라벨) 높이만큼 top 을 내려 sticky
// 시켜야 두 줄이 겹치지 않는다. 상단행 실제 높이를 재서 --issue-shead-row1-h 로 심는다.
function syncIssueHeadRowHeight(panel) {
  const table = panel.querySelector(".sheet-table.kind-issue");
  if (!table) return;
  const top = table.querySelector("tr.issue-shead-top > th");
  const bot = table.querySelector("tr.issue-shead-bot > th");
  if (!top || !bot) return;   // 단일 소스 등 2행 헤더가 아니면 생략
  // offsetTop 은 sticky 시각 오프셋과 무관한 레이아웃 위치라 상단행 높이를 정확히 준다.
  const h = bot.offsetTop - top.offsetTop;
  if (h > 0) table.style.setProperty("--issue-shead-row1-h", `${h}px`);
}

// ── Issue Table (read) ──────────────────────────────────────────────────────
// Issue Table 상단 sticky 툴바: Yield 섹션 Bin 그룹 전체 펼치기/접기.
function issueToolbarHtml() {
  // 수정모드 버튼: ISSUE ITEM 추가 + 삭제 모드 토글. 삭제 실행/복원 버튼(.issue-del-actions)은
  // 삭제 모드일 때만 CSS 로 노출된다.
  const editBtns = (MODE === "edit")
    ? `<button type="button" class="btn-sm" id="etcAddItemBtn">ISSUE ITEM 추가</button>` +
      `<button type="button" class="btn-sm" id="issueDelModeBtn" title="행별 체크박스를 켜고 여러 행을 한 번에 삭제">🗑 삭제 모드</button>` +
      `<span class="issue-del-actions">` +
        `<button type="button" class="btn-sm" id="issueDelSelectedBtn" title="체크한 행 일괄 삭제">선택 삭제</button>` +
        `<button type="button" class="btn-sm" id="issueResetHiddenBtn" title="삭제(숨김)한 Yield/CPK 행 전부 복원">삭제 전체 초기화</button>` +
      `</span>` : "";
  return `<div class="issue-toolbar">` +
    `<span class="issue-jump-group" title="섹션으로 이동">` +
      `<button type="button" class="btn-sm" data-issue-jump="Yield">YIELD</button>` +
      `<button type="button" class="btn-sm" data-issue-jump="CPK">CPK</button>` +
      `<button type="button" class="btn-sm" data-issue-jump="ETC">ETC</button>` +
    `</span>` +
    `<button type="button" class="btn-sm" id="issueToggleAll" data-expanded="false">TNO 전체 펼치기</button>` +
    editBtns +
    `<button type="button" class="btn-sm issue-excel-btn" id="issueExcelBtn" title="Issue Table 을 xlsx 로 다운로드">⬇ Excel</button>` +
    `</div>`;
}

// ── Issue Table 삭제 모드 ────────────────────────────────────────────────────
// 켜면 행 체크박스·개별 삭제(×)·삭제 실행 버튼이 보인다(CSS #panel-issues.issue-del-mode).
// 삭제 후 재로드(load)로 표가 다시 그려져도 모드가 유지되도록 모듈 전역에 둔다.
let issueDelMode = false;
function applyIssueDelMode(panel) {
  panel = panel || document.getElementById("panel-issues");
  if (!panel) return;
  panel.classList.toggle("issue-del-mode", issueDelMode);
  const btn = panel.querySelector("#issueDelModeBtn");
  if (btn) {
    btn.classList.toggle("active", issueDelMode);
    btn.textContent = issueDelMode ? "✕ 삭제 모드 종료" : "🗑 삭제 모드";
  }
  syncIssueDelCount(panel);
  syncIssueStickyOffsets(panel);   // 체크박스 노출로 Step 열 폭이 변할 수 있어 재실측
}
// 체크 개수를 "선택 삭제 (n)" 라벨에 반영.
function syncIssueDelCount(panel) {
  panel = panel || document.getElementById("panel-issues");
  if (!panel) return;
  const n = panel.querySelectorAll(".issue-del-chk:checked").length;
  const btn = panel.querySelector("#issueDelSelectedBtn");
  if (!btn) return;
  btn.textContent = n ? `선택 삭제 (${n})` : "선택 삭제";
  btn.disabled = !n;
}

// ── Issue Table Excel 내보내기 (vendored exceljs — trim.js loadExcelJS 재사용) ──
// 화면과 동일한 컬럼 도출·순서(orderColumns)를 따르되, 미니차트 전용 Map/Distribution
// 열은 제외하고 섹션(Yield/CPK/ETC)을 Category 컬럼으로 되살린다. Yield 상세(TNO) 행은
// 접힘 여부와 무관하게 전부 내보낸다. CPK 섹션의 source 컬럼 값은 화면처럼 cpk 값.
async function exportIssueExcel() {
  const rows = (DATA && Array.isArray(DATA.issue_table_text)) ? DATA.issue_table_text : [];
  const btn = document.getElementById("issueExcelBtn");
  if (btn) btn.disabled = true;
  try {
    let cols = [];
    rows.forEach(r => Object.keys(r || {}).forEach(k => { if (!cols.includes(k)) cols.push(k); }));
    cols = orderColumns(cols, "issue").filter(c => !isMapCol(c) && !isDistCol(c));

    // 행별 섹션 — renderSheetTable 의 rowSection 파생과 동일(빈 Category 는 위 행 상속).
    const section = [];
    let sec = "";
    rows.forEach(r => { const cat = (r && r["Category"]) || ""; if (cat) sec = cat; section.push(sec); });

    const dataRows = rows.filter(r =>
      !isCpkSubheadRow(r) && String((r && r["Item"]) ?? "").trim() !== "");
    if (!dataRows.length) { showToast("내보낼 Issue Table 행이 없습니다"); return; }

    const ExcelJS = await loadExcelJS();
    const wb = new ExcelJS.Workbook();
    const ws = wb.addWorksheet("Issue Table");
    ws.addRow(["Category"].concat(cols.map(c =>
      /_yield$/i.test(String(c)) ? sheetHeaderShortLabel(c) : displayLabel(c))));
    ws.getRow(1).font = { bold: true };
    rows.forEach((r, ri) => {
      if (isCpkSubheadRow(r) || String((r && r["Item"]) ?? "").trim() === "") return;
      ws.addRow([section[ri]].concat(cols.map(c =>
        (r[c] === null || r[c] === undefined) ? "" : r[c])));
    });
    ws.columns.forEach((c, i) => { c.width = i === 0 ? 10 : 18; });

    const buf = await wb.xlsx.writeBuffer();
    const blob = new Blob([buf],
      { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    const meta = (DATA && DATA.session) || {};
    a.download = `issue_table_${meta.lot_id || SESSION_ID}.xlsx`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    showToast("Excel 다운로드 완료");
  } catch (e) {
    showToast("Excel 생성 실패: " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}
// Issue Table 섹션(CPK/ETC) 헤더로 스크롤 이동.
function jumpToIssueSection(sec) {
  const row = document.querySelector(`#panel-issues tr.issue-shead-top[data-sec="${sec}"]`);
  if (row) row.scrollIntoView({ behavior: "smooth", block: "start" });
}
// 한 Bin 그룹(대표행)의 detail TNO 행 펼치기/접기.
function setIssueGroup(gi, expand, btn) {
  if (btn) { btn.setAttribute("aria-expanded", expand ? "true" : "false"); btn.textContent = expand ? "▲" : "▼"; }
  document.querySelectorAll(`#panel-issues tr.issue-bin-detail[data-grp="${gi}"]`).forEach(tr => {
    tr.style.display = expand ? "" : "none";
  });
}
function toggleIssueGroup(btn) {
  setIssueGroup(btn.dataset.grp, btn.getAttribute("aria-expanded") !== "true", btn);
}
function setAllIssueGroups(expand) {
  document.querySelectorAll("#panel-issues .issue-toggle").forEach(btn => setIssueGroup(btn.dataset.grp, expand, btn));
}

// 상단 프록시 가로스크롤바 ↔ .sheet-wrap.kind-issue 실제 스크롤 동기화 (피드백 루프 가드).
let _issueHscrollSyncing = false;
function syncIssueHscrollSpacer(panel) {
  panel = panel || document.getElementById("panel-issues");
  if (!panel) return;
  const wrap = panel.querySelector(".sheet-wrap.kind-issue");
  const spacer = panel.querySelector("#issueHscrollSpacer");
  if (!wrap || !spacer) return;
  spacer.style.width = wrap.scrollWidth + "px";
}
function bindIssueHscroll(panel) {
  const wrap = panel.querySelector(".sheet-wrap.kind-issue");
  const hscroll = panel.querySelector("#issueHscroll");
  if (!wrap || !hscroll) return;
  syncIssueHscrollSpacer(panel);
  // 표 폭이 미니셀 지연 렌더 후에도 변할 수 있어 rAF 로 한 번 더 실측.
  requestAnimationFrame(() => syncIssueHscrollSpacer(panel));
  hscroll.addEventListener("scroll", () => {
    if (_issueHscrollSyncing) return;
    _issueHscrollSyncing = true; wrap.scrollLeft = hscroll.scrollLeft; _issueHscrollSyncing = false;
  });
  wrap.addEventListener("scroll", () => {
    if (_issueHscrollSyncing) return;
    _issueHscrollSyncing = true; hscroll.scrollLeft = wrap.scrollLeft; _issueHscrollSyncing = false;
  });
}
// 좌측 고정열(Step/Bin/TNO/Item/Map/Distribution)의 left 오프셋을 실제 렌더 폭으로 계산 —
// 내용이 길어 컬럼이 colWidth 힌트보다 넓어져도 셀이 겹치지(깨지지) 않게 한다.
function syncIssueStickyOffsets(panel) {
  panel = panel || document.getElementById("panel-issues");
  if (!panel) return;
  const table = panel.querySelector(".sheet-table.kind-issue");
  if (!table) return;
  // 셀 6개 이상인 대표 행 하나로 앞 5개 컬럼(Step/Bin/TNO/Item/Map) 실측 폭을 잰다.
  let row = null;
  table.querySelectorAll("tbody tr").forEach(tr => { if (!row && tr.children.length >= 6) row = tr; });
  if (!row) return;
  const w1 = row.children[0].getBoundingClientRect().width;
  const w2 = row.children[1].getBoundingClientRect().width;
  const w3 = row.children[2].getBoundingClientRect().width;
  const w4 = row.children[3].getBoundingClientRect().width;
  const w5 = row.children[4].getBoundingClientRect().width;
  if (w1 > 0) table.style.setProperty("--issue-col2-left", w1 + "px");
  if (w1 > 0 && w2 > 0) table.style.setProperty("--issue-col3-left", (w1 + w2) + "px");
  if (w1 > 0 && w2 > 0 && w3 > 0) table.style.setProperty("--issue-col4-left", (w1 + w2 + w3) + "px");
  if (w1 > 0 && w2 > 0 && w3 > 0 && w4 > 0) table.style.setProperty("--issue-col5-left", (w1 + w2 + w3 + w4) + "px");
  if (w1 > 0 && w2 > 0 && w3 > 0 && w4 > 0 && w5 > 0) table.style.setProperty("--issue-col6-left", (w1 + w2 + w3 + w4 + w5) + "px");
}
window.addEventListener("resize", () => { syncIssueHscrollSpacer(); syncIssueStickyOffsets(); });

// 컬럼 폭 드래그 리사이즈 — 헤더 우측 경계 핸들(.col-resize-handle, data-col=컬럼인덱스)을 끌어
// 해당 <col> width 를 바꾼다. 저장 없음(새로고침 시 기본 폭 복귀). 폭 변경 시 좌측 고정 오프셋과
// 상단 프록시 스크롤바 폭을 즉시 재실측하고, Map/Distribution 이면 미니 차트를 새 폭으로 재렌더.
function bindIssueColResize(panel) {
  const table = panel.querySelector(".sheet-table.kind-issue");
  const colgroup = table && table.querySelector("colgroup");
  if (!table || !colgroup) return;
  const MIN_W = 24;
  // mousedown 은 매 렌더마다 새로 만들어진 table 에 건다(리스너 중첩 방지).
  table.addEventListener("mousedown", e => {
    const handle = e.target.closest(".col-resize-handle");
    if (!handle) return;
    const idx = +handle.dataset.col;
    const col = colgroup.children[idx];
    if (!col) return;
    const th = handle.closest("th");
    const startW = th ? th.getBoundingClientRect().width : parseFloat(col.style.width) || 80;
    const startX = e.clientX;
    const colName = String(handle.dataset.colName || "").toLowerCase();
    e.preventDefault();   // 드래그 중 텍스트 선택 방지

    let rafPending = false;
    const applySync = () => {
      rafPending = false;
      syncIssueStickyOffsets(panel);
      syncIssueHscrollSpacer(panel);
    };
    const onMove = ev => {
      const w = Math.max(MIN_W, Math.round(startW + (ev.clientX - startX)));
      col.style.width = w + "px";
      if (!rafPending) { rafPending = true; requestAnimationFrame(applySync); }
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      applySync();
      // Map/Distribution 미니 Plotly 차트는 폭 변경에 자동반응하지 않으므로 재렌더.
      if (colName === "map") renderIssueMiniMap(panel);
      else if (colName === "distribution") renderIssueMiniDist(panel);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

function renderIssues(issue_table_text) {
  const panel = document.getElementById("panel-issues");

  if (Array.isArray(issue_table_text) && issue_table_text.length) {
    panel.innerHTML = issueToolbarHtml() +
      `<div id="issueHscroll" class="issue-hscroll"><div id="issueHscrollSpacer" class="issue-hscroll-spacer"></div></div>` +
      renderSheetTable(issue_table_text, { kind: "issue" });
    syncIssueHeadRowHeight(panel);
    syncIssueStickyOffsets(panel);
    requestAnimationFrame(() => syncIssueStickyOffsets(panel));   // 레이아웃 확정 후 재실측
    bindIssueHscroll(panel);
    renderIssueMiniDist(panel);
    renderIssueMiniMap(panel);
    bindIssueColResize(panel);
    applyIssueDelMode(panel);   // 재렌더 후에도 삭제 모드 유지
    return;
  }
  emptyPanel(panel, "Issue Table 데이터 없음");
}


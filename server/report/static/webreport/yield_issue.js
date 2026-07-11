// ── Yield: Bin 접기/펼치기 그룹 렌더 (web_report) ───────────────────────────────
// Pass 행 + Bin 당 most-fail TNO 대표 행(접힘). 대표 행에 ▶ 토글 → 그 Bin 의 나머지
// fail TNO 행을 펼쳐 보여준다(대표 + 펼침 = 그 Bin 의 모든 fail TNO).
let yieldPanelBound = false;
function renderYieldGrouped(passRow, groups, allRows) {
  let cols = [];
  (allRows || []).forEach(r => Object.keys(r || {}).forEach(k => { if (!cols.includes(k)) cols.push(k); }));
  cols = orderColumns(cols, "yield");
  if (!cols.length) return "";

  const colgroup = "<colgroup>" + cols.map(c => `<col style="width:${colWidth(c)}">`).join("") + "</colgroup>";
  const head = buildSheetTableHead(cols);

  // 게이지 정규화 기준: Pass(bin1) 제외한 fail bin 대표행 avg 중 최댓값. Pass 행은 게이지를
  // 그리지 않고, fail 행 게이지는 이 최댓값을 100%로 상대 표시(사용자 요청 — fail 간 비교가 보이게).
  const maxFailAvg = Math.max(0, ...(groups || [])
    .map(g => parseFloat(g && g.rep && g.rep.avg)).filter(v => !isNaN(v)));

  const cellTds = (r, toggleHtml) => cols.map(c => {
    const v = r ? r[c] : "";
    const txt = (v === null || v === undefined) ? "" : String(v);
    const isEmpty = txt === "";
    const cLower = String(c).trim().toLowerCase();
    const cls = [];
    if (isEmpty) cls.push("st-empty"); else if (isNumVal(v)) cls.push("st-num");
    let inner = isEmpty ? "" : esc(txt);
    // Item 셀(Pass 행 제외) → Item_detail 링크
    if (cLower === "item" && !isEmpty && String((r && r.bin) ?? "").trim() !== "1") {
      inner = `<span class="item-detail-link" data-subject="${esc(txt)}">${esc(txt)}</span>`;
    }
    // avg(수율) 셀: Pass(bin1) 행은 게이지 없이 값만. fail 행은 fail bin 최댓값 기준 상대 게이지.
    if (cLower === "avg" && !isEmpty) {
      const isPass = String((r && r.bin) ?? "").trim() === "1";
      if (!isPass) {
        const val = parseFloat(v) || 0;
        const pct = maxFailAvg > 0 ? Math.max(0, Math.min(100, val / maxFailAvg * 100)) : 0;
        inner = `<span class="yield-avg-cell"><span class="yield-avg-val">${esc(txt)}</span>` +
          `<span class="yield-gauge"><span class="yield-gauge-fill" style="width:${pct}%"></span></span></span>`;
      }
    }
    // 접기/펼치기 토글은 STEP 셀 오른쪽에 배치(우측 정렬).
    if (toggleHtml && cLower === "step") inner = inner + toggleHtml;
    return `<td${cls.length ? ` class="${cls.join(" ")}"` : ""}>${inner}</td>`;
  }).join("");

  let body = "";
  if (passRow) body += `<tr class="yield-pass-row">${cellTds(passRow, "")}</tr>`;
  (groups || []).forEach((g, gi) => {
    const detail = (g.rows || []).slice(1);   // 대표(most-fail) 제외 나머지 fail TNO
    const toggle = detail.length
      ? ` <button type="button" class="yield-toggle" data-grp="${gi}" aria-expanded="false">▼</button>` : "";
    body += `<tr class="yield-bin-rep" data-grp="${gi}">${cellTds(g.rep, toggle)}</tr>`;
    detail.forEach(r => {
      body += `<tr class="yield-bin-detail" data-grp="${gi}" style="display:none">${cellTds(r, "")}</tr>`;
    });
  });

  return `<div class="sheet-wrap kind-yield"><table class="sheet-table kind-yield">${colgroup}${head}<tbody>${body}</tbody></table></div>`;
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

  // web_report: Bin 접기/펼치기 그룹 렌더 (yield_bin_groups 가 있을 때)
  const groups = DATA.web_report && DATA.web_report.yield_bin_groups;
  if (Array.isArray(groups) && Array.isArray(yield_text)) {
    const passRow = yield_text.find(r => String(r.bin).trim() === "1") || null;
    bindYieldPanel();
    panel.innerHTML = overview + yieldToolbarHtml() + renderYieldGrouped(passRow, groups, yield_text);
    return;
  }

  // list of dicts (그룹 데이터가 없을 때의 폴백)
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
  // 수정모드에서는 "ETC 에 항목 추가" 버튼도 같은 sticky 툴바 한 줄에 함께 둔다.
  const etcBtn = (MODE === "edit")
    ? `<button type="button" class="btn-sm" id="etcAddItemBtn">ETC 에 항목 추가</button>` : "";
  return `<div class="issue-toolbar">` +
    `<button type="button" class="btn-sm" data-issue-jump="Yield">Yield 로 이동</button>` +
    `<button type="button" class="btn-sm" data-issue-jump="CPK">CPK 로 이동</button>` +
    `<button type="button" class="btn-sm" data-issue-jump="ETC">ETC 로 이동</button>` +
    `<button type="button" class="btn-sm" id="issueToggleAll" data-expanded="false">TNO 전체 펼치기</button>` +
    etcBtn +
    `</div>`;
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
// 좌측 고정열(Step/Bin/TNO/Item)의 left 오프셋을 실제 렌더 폭으로 계산 — 내용이 길어 컬럼이
// colWidth 힌트보다 넓어져도 셀이 겹치지(깨지지) 않게 한다.
function syncIssueStickyOffsets(panel) {
  panel = panel || document.getElementById("panel-issues");
  if (!panel) return;
  const table = panel.querySelector(".sheet-table.kind-issue");
  if (!table) return;
  // 셀 4개 이상인 대표 tbody 행 하나로 앞 3개 컬럼 실측 폭을 잰다.
  let row = null;
  table.querySelectorAll("tbody tr").forEach(tr => { if (!row && tr.children.length >= 4) row = tr; });
  if (!row) return;
  const w1 = row.children[0].getBoundingClientRect().width;
  const w2 = row.children[1].getBoundingClientRect().width;
  const w3 = row.children[2].getBoundingClientRect().width;
  if (w1 > 0) table.style.setProperty("--issue-col2-left", w1 + "px");
  if (w1 > 0 && w2 > 0) table.style.setProperty("--issue-col3-left", (w1 + w2) + "px");
  if (w1 > 0 && w2 > 0 && w3 > 0) table.style.setProperty("--issue-col4-left", (w1 + w2 + w3) + "px");
}
window.addEventListener("resize", () => { syncIssueHscrollSpacer(); syncIssueStickyOffsets(); });

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
    return;
  }
  emptyPanel(panel, "Issue Table 데이터 없음");
}


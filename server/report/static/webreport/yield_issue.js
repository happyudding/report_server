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

// 한 STEP 표를 렌더. si = 섹션 인덱스 — 그룹 토글 data-grp 를 표들 사이에서 유일하게 만든다.
// maxFailAvg = 그 STEP 내 fail bin 대표행 avg 최댓값(게이지 100% 기준, STEP 별 정규화).
function renderYieldTable(cols, groups, maxFailAvg, si) {
  const colgroup = "<colgroup>" + cols.map(c => `<col style="width:${colWidth(c)}">`).join("") + "</colgroup>";
  const head = buildSheetTableHead(cols);

  const cellTds = (r, toggleHtml) => cols.map(c => {
    const v = r ? r[c] : "";
    const txt = (v === null || v === undefined) ? "" : String(v);
    const isEmpty = txt === "";
    const cLower = String(c).trim().toLowerCase();
    const cls = [];
    if (isEmpty) cls.push("st-empty"); else if (isNumVal(v)) cls.push("st-num");
    let inner = isEmpty ? "" : esc(txt);
    // Item 셀 → Item_detail 링크 (STEP 표는 전부 fail 행이라 Pass 예외 없음)
    if (cLower === "item" && !isEmpty) {
      inner = `<span class="item-detail-link" data-subject="${esc(txt)}">${esc(txt)}</span>`;
    }
    // avg(수율) 셀: STEP 내 최대 fail 비중 기준 상대 게이지.
    if (cLower === "avg" && !isEmpty) {
      const val = parseFloat(v) || 0;
      const pct = maxFailAvg > 0 ? Math.max(0, Math.min(100, val / maxFailAvg * 100)) : 0;
      inner = `<span class="yield-avg-cell"><span class="yield-avg-val">${esc(txt)}</span>` +
        `<span class="yield-gauge"><span class="yield-gauge-fill" style="width:${pct}%"></span></span></span>`;
    }
    // 접기/펼치기 토글은 STEP 셀 오른쪽에 배치(우측 정렬).
    if (toggleHtml && cLower === "step") inner = inner + toggleHtml;
    return `<td${cls.length ? ` class="${cls.join(" ")}"` : ""}>${inner}</td>`;
  }).join("");

  let body = "";
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
    const maxFailAvg = Math.max(0, ...groups
      .map(g => parseFloat(g && g.rep && g.rep.avg)).filter(v => !isNaN(v)));
    const label = String(sg.step || "").trim() || "(기타)";
    return `<div class="yield-step-section">` +
      `<div class="yield-step-title">STEP ${esc(label)}</div>` +
      renderYieldTable(cols, groups, maxFailAvg, si) + `</div>`;
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


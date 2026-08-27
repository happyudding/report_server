// ── Yield: STEP(P1/P2/P3) 별 Bin 접기/펼치기 표 렌더 (web_report) ────────────────
// STEP 마다 표 1개. 각 표는 Bin 당 most-fail TNO 대표 행(접힘) + ▼ 토글로 그 Bin 의 나머지
// fail TNO 행을 펼친다. bin portion 분모는 그 STEP 진입 die 가 아니라 **전체 rawdata die**
// (서버 build_yield_rows 값 그대로 — STEP 별 재계산 없음).
// Pass 행은 표에 넣지 않고 상단 요약 박스가 담당한다(전체 + STEP 별 통과율).
let yieldPanelBound = false;

// ── Bin 집계 헤더행 (펼침 표시 전용) ─────────────────────────────────────────
// 서버 rep(대표) 행은 숫자가 Bin 합계인데 식별정보(Step/TNO/Item)는 most-fail 항목 것이다
// (yield_tab._bin_total_row). 접힘에선 "Bin 요약"으로 읽히지만 펼치면 그 항목 혼자 Bin 전체
// 만큼 죽은 것처럼 보인다 — 그래서 펼칠 때만 대표행을 이 집계 헤더행으로 갈아끼우고
// most-fail 항목은 자기 실제 값을 가진 상세행으로 되돌린다(2026-08-25).
//
// 규약 정본은 서버 web_report/tabs/yield_tab.py (BIN_AGG_TNO / bin_agg_label /
// build_bin_agg_row) — Excel 내보내기가 같은 라벨을 쓰므로 문자열을 여기서 다시 만들지 말고
// 이 함수 하나만 호출할 것. 라벨 가운데 공백 4칸은 CSS `white-space: pre` 로 지킨다.
const BIN_AGG_TNO = "-";
// 대표행이 **항목 행 자체**인 표(Issue Table Temp)의 헤더행에서 값을 남길 열 — 나머지
// 수치·편집 열은 전부 비운다(합산이 뜻을 갖지 않고 bin 단위 저장 키도 없다).
// 파이썬 짝: web_report/yield_agg.py AGG_ID_COLS.
const BIN_AGG_ID_COLS = ["Category", "Step", "Bin", "TNO", "Item"];
function binAggLabel(binValue, nItems) {
  return `BIN ${binValue}    (${nItems} items)`;
}
// rep 행 → 집계 헤더행 사본. 항목이 1개뿐인 Bin 은 null (펼쳐도 상세가 1줄이라 같은 값을
// 두 번 보여줄 뿐 — 사용자 확정). extra 로 비울 컬럼(Map/Distribution/comment 등)을 준다.
function yieldBinAggRow(rep, binValue, nItems, extra) {
  if (!rep || !(nItems > 1)) return null;
  const row = Object.assign({}, rep, extra || {});
  row.TNO = BIN_AGG_TNO;
  row.Item = binAggLabel(binValue, nItems);
  row._agg = true;
  return row;
}

// Yield 표 컬럼 순서(step/bin/tno/item → source yield/count → avg). 모든 STEP 표가 같은
// 컬럼을 쓰도록 전체 행에서 한 번만 계산한다.
function yieldColumnsFrom(allRows) {
  let cols = [];
  (allRows || []).forEach(r => Object.keys(r || {}).forEach(k => { if (!cols.includes(k)) cols.push(k); }));
  return orderColumns(cols, "yield");
}

// STEP 표 최상단에 붙일 Bin1(Pass) 행 — Issue Table Yield 섹션 맨 위 Pass 행과 같은 역할.
// 값은 yield_summary.by_step(서버 yield_step_summary) 의 STEP 별 **누적** 수율 =
// (전체 die − 그 STEP 까지의 누적 fail) / 전체 die. 분모는 전 STEP 고정이라 P1→P3 로
// 갈수록 값이 단조 감소한다. 소스별 yield_pct/survivor 를 {src}_yield/{src}_count 로 옮긴다.
// byStepOverride 는 소스 부분집합으로 다시 계산한 by_step 을 쓰고 싶을 때만 준다.
function yieldStepPassRow(step, byStepOverride) {
  const ov = DATA.web_report && DATA.web_report.yield_summary;
  const byStep = Array.isArray(byStepOverride) ? byStepOverride
    : ((ov && Array.isArray(ov.by_step)) ? ov.by_step : []);
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
  // source 가 2개 이상이면 헤더가 축약 라벨이 되므로 그 컬럼 폭 힌트도 함께 낮춘다.
  const narrowSrc = sourceColCount(cols) >= SRC_NARROW_MIN;
  const colgroup = "<colgroup>" + cols.map(c =>
    `<col style="width:${colWidth(c, undefined, narrowSrc)}">`).join("") + "</colgroup>";
  const head = buildSheetTableHead(cols, { resize: true });   // 헤더 우측 경계 드래그로 열너비 조절

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
  // isAgg = 펼침 집계 헤더행 — Item 이 측정 항목이 아니라 라벨이라 상세 링크를 걸지 않는다
  // (그라데이션은 rep 와 같은 숫자이므로 그대로 둔다).
  const cellTds = (r, toggleHtml, isPass, isAgg) => cols.map(c => {
    const v = r ? r[c] : "";
    const txt = (v === null || v === undefined) ? "" : String(v);
    const isEmpty = txt === "";
    const cLower = String(c).trim().toLowerCase();
    const cls = [];
    let cellStyle = "";
    if (isEmpty) cls.push("st-empty"); else if (isNumVal(v)) cls.push("st-num");
    let inner = isEmpty ? "" : esc(txt);
    // Item 셀 → Item_detail 링크 (Bin1 Pass 행은 측정 항목이 아니라 제외)
    if (cLower === "item" && !isEmpty && !isPass && !isAgg) {
      inner = `<span class="item-detail-link" data-subject="${esc(txt)}">${esc(txt)}</span>`;
    }
    // 집계행 Item 라벨의 공백 4칸은 HTML 이 접으므로 그 셀만 pre 로 지킨다.
    if (cLower === "item" && isAgg) cls.push("bin-agg-item");
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
  if (passRow) body += `<tr class="yield-pass-row">${cellTds(passRow, "", true, false)}</tr>`;
  (groups || []).forEach((g, gi) => {
    const grp = `${si}_${gi}`;   // 표 간 유일한 그룹 id
    // g.rows = [집계 rep, most-fail TNO, 나머지 TNO...].
    // 접힘 = rep 1행(Bin 합계, most-fail Item 이름) — 종전과 동일.
    // 펼침 = 집계 헤더행(BIN n    (k items)) + **모든** TNO 행(most-fail 포함, slice(1)).
    //   most-fail 행을 되살린 것이 이번 변경의 핵심이다 — 그 항목의 실제 fail 수가 종전엔
    //   화면 어디에도 없었다(대표행이 그 자리에 Bin 합계를 들고 앉아 있었다, 2026-08-25).
    // 항목이 1개뿐인 Bin 은 집계행도 토글도 만들지 않고 rep 만 그린다(값이 그 항목 값과 동일).
    const detail = (g.rows || []).slice(1);
    const agg = yieldBinAggRow(g.rep, g.bin, detail.length);
    const toggle = agg
      ? ` <button type="button" class="yield-toggle" data-grp="${grp}" aria-expanded="false">▼</button>` : "";
    body += `<tr class="yield-bin-rep${agg ? " has-agg" : ""}" data-grp="${grp}">${cellTds(g.rep, toggle, false, false)}</tr>`;
    if (!agg) return;
    // 토글 버튼은 rep·agg 양쪽에 둔다 — 둘은 상호 배타로 표시되므로 접힘에선 rep 의 ▼,
    // 펼침에선 agg 의 ▲ 가 보인다.
    const toggleUp = ` <button type="button" class="yield-toggle" data-grp="${grp}" aria-expanded="true">▲</button>`;
    body += `<tr class="yield-bin-agg" data-grp="${grp}" style="display:none">${cellTds(agg, toggleUp, false, true)}</tr>`;
    detail.forEach(r => {
      body += `<tr class="yield-bin-detail" data-grp="${grp}" style="display:none">${cellTds(r, "", false, false)}</tr>`;
    });
  });

  return `<div class="sheet-wrap kind-yield"><table class="sheet-table kind-yield">${colgroup}${head}<tbody>${body}</tbody></table></div>`;
}

// STEP 별 표 섹션(제목 + 표)을 순서대로 렌더 (P1→P2→P3).
// byStep/keyPrefix 는 소스 부분집합 표를 그릴 때만 쓴다(생략하면 종전과 동일).
function renderYieldStepSections(stepGroups, allRows, byStep, keyPrefix) {
  const cols = yieldColumnsFrom(allRows);
  if (!cols.length || !Array.isArray(stepGroups) || !stepGroups.length) return "";
  return stepGroups.map((sg, si) => {
    const groups = sg.groups || [];
    const label = String(sg.step || "").trim() || "(기타)";
    return `<div class="yield-step-section" data-step="${esc(label)}">` +
      `<div class="yield-step-title">STEP ${esc(label)}` +
      `<span class="merge-note">${esc(MERGE_NOTE_TEXT)}</span></div>` +
      renderYieldTable(cols, groups, `${keyPrefix || ""}${si}`,
                       yieldStepPassRow(sg.step, byStep)) + `</div>`;
  }).join("");
}

// Yield 탭 하단 Temp Corner 섹션 (Temperature 모드 전용, 2026-08-05).
// 내용은 Issue Table Temp 탭과 **같은 시트**(sheets["Issue Table Temp"])다 — 여기서는
// 편집 열(Map/Distribution/Status/comment)을 뺀 읽기 전용 요약으로만 보여주고,
// 편집은 그 탭에서 하도록 안내 버튼을 단다.
const TEMP_SUMMARY_SKIP = /^(map|distribution|status|category)$/i;
// 전 항목 재판정이라 항목이 수백 개가 될 수 있어 **전량을 청크 렌더**로 그린다(사용자 요청
// 2026-08-06 — 구 상위 60행 제한 폐지). 통짜 innerHTML 로 수백 행 × 20열을 만들면 Yield 탭
// 진입이 그대로 블록되므로, Issue Table 과 같은 청크 채우기(fill)를 쓴다.
// 반환값은 {html, fill} — 호출부가 panel.innerHTML 을 심은 뒤 fill(onDone) 을 부른다.
const TEMP_SECTION_EMPTY = { html: "", fill: () => {} };
function renderYieldTempSection() {
  if (webReportMode() !== "Temperature") return TEMP_SECTION_EMPTY;
  const rows = (webReportSheets() || {})[ISSUE_TEMP_SHEET];
  if (!Array.isArray(rows) || !rows.length) return TEMP_SECTION_EMPTY;
  // 섹션 divider 행(Category="TEMP") 제외 — 열 정리(편집 전용 열 제거)는 여기서 한다.
  const dataRows = rows.filter(r => String((r && r.Item) || "").trim());
  if (!dataRows.length) return TEMP_SECTION_EMPTY;
  const data = dataRows.map(r => {
    const o = {};
    Object.keys(r).forEach(k => {
      if (!TEMP_SUMMARY_SKIP.test(String(k)) && !/comment/i.test(String(k))) o[k] = r[k];
    });
    return o;
  });
  // grad: source _yield / avg 셀에 Yield 표와 같은 빨강 그라데이션(값 클수록 진함).
  const table = renderSheetTable(data, { kind: "yield", grad: true, chunk: true });
  const html = `<div class="yield-corner-section">` +
    `<div class="yield-corner-title">Temp Corner (CT / HT) — RT Limit 이탈 항목` +
    ` <span class="yield-corner-more">전체 ${dataRows.length}항목 · Bin 별 most fail 만 표시` +
    ` (▼ 로 펼치기)</span>` +
    ` <button type="button" class="btn-sm" data-goto-tab="issue-temp" ` +
    `title="Issue Table Temp 탭에서 comment·Status 를 편집합니다">탭에서 보기 ›</button></div>` +
    table.html + `</div>`;
  return {
    html,
    fill: onDone => table.fill(
      document.querySelector("#panel-yield .yield-corner-section .sheet-table tbody"), onDone),
  };
}

// Yield 탭 상단 툴바: 모든 Bin 그룹의 FAILTNO 상세행을 한 번에 펼치기/접기하는 토글.
function yieldToolbarHtml() {
  return `<div class="yield-toolbar">` +
    yieldJumpGroupHtml() +
    `<button type="button" class="btn-sm" id="yieldToggleAll" data-expanded="false">전체 펼치기</button>` +
    sheetSearchHtml("yieldSearchInput", yieldSearchTerm, "Item 검색") +
    yieldExcelBtnHtml() +
    `</div>`;
}

// ── 표 검색 (Issue Table / Yield 공용) ────────────────────────────────────────
// 상단 sticky 툴바의 검색창에 친 문자열로 데이터 행을 걸러낸다(부분일치·대소문자 무시).
// 대상은 **Item 명 + comment 셀**(Issue Table 만 comment 열이 있다). 표 구조를 지키기
// 위해 섹션 헤더·서브헤더 행은 항상 남기고, 나머지는 .row-search-hide 로만 숨긴다
// (접기/펼치기가 쓰는 인라인 display 를 건드리지 않아 검색 해제 시 원상 복구된다).
// Issue 표 검색어는 패널별 상태(core.js issueUi)에 둔다 — 두 패널이 서로를 덮어쓰지 않게.
let yieldSearchTerm = "";

function sheetSearchHtml(id, value, placeholder) {
  return `<span class="sheet-search-wrap">` +
    `<input type="text" class="sheet-search" id="${id}" data-no-dirty autocomplete="off" ` +
    `placeholder="${esc(placeholder)}" value="${esc(value || "")}">` +
    `<span class="sheet-search-cnt" id="${id}Cnt"></span></span>`;
}

// 검색 대상 텍스트. Item 셀은 읽기 모드 Issue Table 이면 data-col="Item", 그 외(Yield·
// 편집 모드)는 좌측 고정열 순서(step/bin/tno/item — sheets.js orderColumns)상 4번째다.
// comment 셀은 편집 모드에서 원문(data-raw)이 링크로 치환돼 있어 원문을 우선 쓴다.
function sheetRowMatches(tr, term) {
  const itemCell = tr.querySelector('td[data-col="Item"]') || tr.children[3];
  let txt = itemCell ? itemCell.textContent : "";
  // 서식 토큰은 벗기고 본문만 검색 대상에 넣는다 — 안 그러면 "*r[" 같은 표시문자가
  // 검색어에 걸려 엉뚱한 행이 남는다.
  tr.querySelectorAll("td.st-comment").forEach(td => {
    txt += " " + stripCommentFormat(td.dataset.raw != null ? td.dataset.raw : td.textContent);
  });
  return txt.toLowerCase().indexOf(term) >= 0;
}

function setSearchCount(id, shown, total, term) {
  const el = document.getElementById(id + "Cnt");
  if (el) el.textContent = term ? `${shown} / ${total}` : "";
}

// Issue Table: 섹션 2행 헤더(issue-shead-*)와 CPK/ETC 서브헤더 행은 항상 남긴다.
// 검색 중에는 접혀 있는 TNO 상세행도 매칭되면 보이게 한다(CSS .issue-searching).
function applyIssueSearch(rawTerm, panel) {
  panel = panel || activeIssuePanel();
  if (!panel) return;
  const ui = issueUi(panel);
  ui.search = String(rawTerm || "");
  const term = ui.search.trim().toLowerCase();
  panel.classList.toggle("issue-searching", !!term);
  let shown = 0, total = 0;
  panel.querySelectorAll(".sheet-table.kind-issue tbody tr").forEach(tr => {
    if (tr.classList.contains("issue-shead-top") || tr.classList.contains("issue-shead-bot")
        || tr.querySelector("td.sheet-subhead")) return;   // 구조 행 — 필터 대상 아님
    total++;
    const keep = !term || sheetRowMatches(tr, term);
    tr.classList.toggle("row-search-hide", !keep);
    if (keep) shown++;
  });
  setSearchCount(panel.querySelector(".sheet-search")?.id || "issueSearchInput",
                 shown, total, term);
  afterIssueRowsToggled(panel);   // 행 구성이 바뀌면 좌측 고정 오프셋·가로 스크롤 폭 재실측
}

// Status(Open/Close) 별 행 숨김 — 액션 메뉴의 "Close/Open 항목 숨기기" (2026-08-11 요청).
// 검색(.row-search-hide)과 **다른 클래스**를 써서 두 필터가 서로를 지우지 않게 한다.
//
// Status 셀이 없는 행(Yield 섹션의 Pass 행·TNO 상세행)은 **직전 이슈 행의 판정을 상속**
// 한다 — Yield 는 대표행 하나만 Status 를 갖고 그 아래 상세행이 따라붙으므로, 대표행을
// 숨기면서 상세행만 남기면 표가 깨진다. 섹션 헤더/서브헤더에서 상속을 리셋한다.
function applyIssueStatusFilter(panel) {
  panel = panel || activeIssuePanel();
  if (!panel) return;
  const ui = issueUi(panel);
  let visible = true;
  panel.querySelectorAll(".sheet-table.kind-issue tbody tr").forEach(tr => {
    if (tr.classList.contains("issue-shead-top") || tr.classList.contains("issue-shead-bot")
        || tr.querySelector("td.sheet-subhead")) { visible = true; return; }   // 구조 행
    const cell = tr.querySelector("td.issue-status-cell");
    if (cell) {
      // 값은 셀 클래스가 정본이다(편집 모드는 드랍다운, 조회 모드는 텍스트라 마크업이 다름).
      const closed = cell.classList.contains("is-close");
      visible = !(closed ? ui.hideClose : ui.hideOpen);
    }
    tr.classList.toggle("row-status-hide", !visible);
  });
  afterIssueRowsToggled(panel);   // 행 구성이 바뀌면 좌측 고정 오프셋·가로 스크롤 폭 재실측
}

// Yield: STEP 별로 표가 여러 개라 패널 전체를 훑는다. comment 열은 없어 Item 명만 본다.
function applyYieldSearch(rawTerm) {
  const panel = document.getElementById("panel-yield");
  if (!panel) return;
  yieldSearchTerm = String(rawTerm || "");
  const term = yieldSearchTerm.trim().toLowerCase();
  panel.classList.toggle("yield-searching", !!term);
  let shown = 0, total = 0;
  panel.querySelectorAll(".sheet-table.kind-yield tbody tr").forEach(tr => {
    total++;
    const keep = !term || sheetRowMatches(tr, term);
    tr.classList.toggle("row-search-hide", !keep);
    if (keep) shown++;
  });
  setSearchCount("yieldSearchInput", shown, total, term);
  syncYieldStickyOffsets(panel);
}

// 키입력마다 수천 행을 훑지 않도록 입력이 멈춘 뒤 한 번만 적용한다(CPK 탭과 같은 처방).
const SHEET_SEARCH_DEBOUNCE_MS = 150;
let _sheetSearchTimer = null;
function sheetSearchDebounced(fn, value) {
  if (_sheetSearchTimer) clearTimeout(_sheetSearchTimer);
  _sheetSearchTimer = setTimeout(() => { _sheetSearchTimer = null; fn(value); },
                                 SHEET_SEARCH_DEBOUNCE_MS);
}
// 툴바는 매 렌더마다 새로 만들어지므로 document 위임으로 1회만 건다.
document.addEventListener("input", e => {
  const issuePanel = e.target.classList.contains("sheet-search") ? issuePanelOf(e.target) : null;
  if (issuePanel) sheetSearchDebounced(v => applyIssueSearch(v, issuePanel), e.target.value);
  else if (e.target.id === "yieldSearchInput") sheetSearchDebounced(applyYieldSearch, e.target.value);
});
// Yield 탭 우상단 Excel Down (excel_export.js exportYieldExcel).
function yieldExcelBtnHtml() {
  return `<button type="button" class="btn-sm tab-excel-btn" id="yieldExcelBtn" ` +
    `title="Honey Excel Download 의 Yield 시트와 동일한 xlsx 다운로드 (STEP 별 표 분리·Bin 접힌 상태)">Excel Down</button>`;
}
// 한 Bin 그룹(대표행)의 detail FAILTNO 행 펼치기/접기 + 그룹 토글 버튼 상태 갱신.
// 펼침으로 Item 등 컬럼 폭이 바뀌면 좌측 고정 오프셋이 stale 이 되므로 토글 후 재실측한다.
function setYieldGroup(gi, expand, btn, skipSync) {
  // 토글 버튼은 rep·agg 양쪽에 있으므로 클릭된 하나가 아니라 그 그룹 전체를 갱신한다.
  document.querySelectorAll(`#panel-yield .yield-toggle[data-grp="${gi}"]`).forEach(b => {
    b.setAttribute("aria-expanded", expand ? "true" : "false");
    b.textContent = expand ? "▲" : "▼";
  });
  // 3행 규칙: rep(접힘 전용) ↔ agg(펼침 전용) 상호 배타 + detail 일괄.
  // 집계 헤더행이 없는 그룹에서 rep 를 감추면 그 행이 통째로 사라지므로 있을 때만 바꾼다
  // (setIssueGroup 과 같은 가드).
  const aggRows = document.querySelectorAll(`#panel-yield tr.yield-bin-agg[data-grp="${gi}"]`);
  if (aggRows.length) {
    document.querySelectorAll(`#panel-yield tr.yield-bin-rep[data-grp="${gi}"]`).forEach(tr => {
      tr.style.display = expand ? "none" : "";
    });
    aggRows.forEach(tr => { tr.style.display = expand ? "" : "none"; });
  }
  document.querySelectorAll(`#panel-yield tr.yield-bin-detail[data-grp="${gi}"]`).forEach(tr => {
    tr.style.display = expand ? "" : "none";
  });
  // 전체 펼치기/접기(setAllYieldGroups)는 그룹마다 재실측하면 그룹 수 × 전체 행
  // 강제 reflow 로 수 초 freeze 가 난다 — 호출부가 마지막에 1회만 실측한다.
  if (!skipSync) syncYieldStickyOffsets();
}
function setAllYieldGroups(expand) {
  const seen = new Set();   // 그룹당 토글 버튼이 2개(rep/agg)라 중복 호출을 막는다
  document.querySelectorAll("#panel-yield .yield-toggle").forEach(btn => {
    const gi = btn.dataset.grp;
    if (seen.has(gi)) return;
    seen.add(gi);
    setYieldGroup(gi, expand, btn, true);
  });
  syncYieldStickyOffsets();
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
    const jump = e.target.closest("[data-yield-jump]");
    if (jump) { yieldJumpTo(jump.dataset.yieldJump); return; }
    const btn = e.target.closest(".yield-toggle");
    if (!btn) return;
    setYieldGroup(btn.dataset.grp, btn.getAttribute("aria-expanded") !== "true", btn);
  });
  yieldPanelBound = true;
}

// ── STEP / Temp Corner 바로가기 (사용자 요청 2026-08-06) ─────────────────────
// STEP 마다 표가 하나씩 세로로 이어져 아래 STEP 까지 스크롤하기 번거롭다 — sticky 툴바에
// 점프 버튼을 둔다. Issue Table 의 issue-jump-group 과 같은 역할.
// 스크롤 목표 y 는 sticky 헤더+툴바 높이만큼 올려 잡는다(안 그러면 섹션 제목이 툴바에 가린다).
function yieldStickyTop() {
  const head = parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue("--sticky-head-h")) || 96;
  const bar = document.querySelector("#panel-yield .yield-toolbar");
  return head + (bar ? bar.getBoundingClientRect().height : 40);
}
function yieldJumpTo(target) {
  if (target === "top") { window.scrollTo({ top: 0, behavior: "smooth" }); return; }
  const panel = document.getElementById("panel-yield");
  if (!panel) return;
  let el = null;
  if (target === "temp") {
    el = panel.querySelector(".yield-corner-section");
  } else if (target.startsWith("step:")) {
    const want = target.slice(5);
    panel.querySelectorAll(".yield-step-section").forEach(sec => {
      if (!el && sec.dataset.step === want) el = sec;
    });
  }
  if (!el) return;
  const y = window.scrollY + el.getBoundingClientRect().top - yieldStickyTop() - 8;
  window.scrollTo({ top: Math.max(0, y), behavior: "smooth" });
}
// 툴바에 넣을 점프 버튼들. STEP 이 2개 이상이거나 Temp Corner 가 있을 때만 만든다
// (표가 하나뿐이면 점프할 곳이 없다).
function yieldJumpGroupHtml() {
  const steps = (DATA.web_report && DATA.web_report.yield_step_groups) || [];
  const tempRows = (webReportMode() === "Temperature")
    ? (webReportSheets() || {})[ISSUE_TEMP_SHEET] : null;
  const hasTemp = Array.isArray(tempRows) && tempRows.length > 0;
  if (!(steps.length > 1 || hasTemp)) return "";
  const btns = steps.map(sg => {
    const label = String(sg.step || "").trim() || "(기타)";
    return `<button type="button" class="btn-sm" data-yield-jump="step:${esc(label)}" ` +
      `title="STEP ${esc(label)} 표로 이동">${esc(label)}</button>`;
  });
  if (hasTemp) {
    btns.push(`<button type="button" class="btn-sm" data-yield-jump="temp" ` +
      `title="Temp Corner (CT / HT) 표로 이동">Temp Corner</button>`);
  }
  return `<span class="yield-jump-group" title="표로 이동">` + btns.join("") +
    `<button type="button" class="btn-sm" data-yield-jump="top" title="맨 위로">▲ 맨 위</button>` +
    `</span>`;
}

// Yield 표 좌측 고정열(Step/Bin/TNO/Item)의 left 오프셋을 실제 렌더 폭으로 계산 —
// 내용이 길어 컬럼이 colWidth 힌트보다 넓어져도 셀이 겹치지(깨지지) 않게 한다.
// STEP 별로 표가 여러 개라 표마다 따로 심는다. 탭이 숨겨져 폭이 0 이면 건너뛴다.
// sticky 툴바 실제 높이 → --yield-toolbar-h (CSS 가 표 max-height·프록시 바 top 계산에 쓴다).
// 점프 버튼이 늘어 툴바가 두 줄이 되면 고정값 40px 은 어긋나므로 실측한다.
function syncYieldToolbarHeight(panel) {
  const bar = panel && panel.querySelector(".yield-toolbar");
  if (!bar) return;
  const h = Math.round(bar.getBoundingClientRect().height);
  if (h > 0) panel.style.setProperty("--yield-toolbar-h", h + "px");
}
// 2행 헤더(그룹 라벨 / source)에서 하단행이 상단행 높이만큼 내려가 sticky 되도록 실측
// (Issue Table 의 syncIssueHeadRowHeight 와 같은 처방). 표마다 따로 심는다.
function syncYieldHeadRowHeight(panel) {
  if (!panel) return;
  panel.querySelectorAll(".sheet-table.kind-yield").forEach(table => {
    const top = table.querySelector("thead > tr:first-child > th");
    const bot = table.querySelector("thead > tr:nth-child(2) > th");
    if (!top || !bot) return;   // 단일 소스 등 2행 헤더가 아니면 생략
    // 상단행 th 의 **높이**를 재면 안 된다 — 식별 4열은 rowspan=2 라 두 행 합(≈50px)이
    // 나와 하단행이 그만큼 더 내려가고 그 틈으로 데이터 행이 비쳤다(2026-08-06 실측).
    // offsetTop 차이는 sticky 시각 오프셋과 무관한 레이아웃 위치라 상단행 높이를 정확히 준다
    // (Issue Table 의 syncIssueHeadRowHeight 와 같은 처방).
    const h = bot.offsetTop - top.offsetTop;
    if (h > 0) table.style.setProperty("--yield-shead-row1-h", h + "px");
  });
}

// ── 세로 틀고정: 페이지 스크롤 ↔ 표 내부 스크롤 1:1 동기화 ────────────────────
// (구조 설명은 report_view.html 의 .yield-vfreeze 주석이 정본)
// ① 섹션 높이를 표 전체 높이로 늘려 페이지가 그만큼 스크롤되게 하고
// ② 페이지가 섹션을 지나간 만큼 wrap.scrollTop 을 그대로 따라 준다.
// 표가 뷰포트보다 짧으면 늘릴 것도 따라갈 것도 없다(그 섹션은 그냥 통째로 보인다).
function yieldVFreezeSections(panel) {
  return (panel || document).querySelectorAll(
    "#panel-yield .yield-step-section, #panel-yield .yield-corner-section");
}
function syncYieldVFreeze(panel) {
  panel = panel || document.getElementById("panel-yield");
  if (!panel) return;
  // 클램프(max-height)를 **먼저** 걸어야 wrap.clientHeight 가 "잘린 높이"로 읽힌다 —
  // 클래스를 나중에 붙이면 첫 측정에서 wrap 높이 = 표 전체 높이라 늘 "고정 불필요" 로 나온다.
  panel.classList.add("yield-vfreeze");
  let any = false;
  yieldVFreezeSections(panel).forEach(sec => {
    const wrap = sec.querySelector(".sheet-wrap.kind-yield");
    const table = wrap && wrap.querySelector(".sheet-table.kind-yield");
    if (!wrap || !table) return;
    // 표 전체 높이(클램프 전) + 섹션 안 다른 요소(제목·프록시 바) 높이.
    const tableH = table.scrollHeight;
    let others = 0;
    Array.prototype.forEach.call(sec.children, el => {
      if (el !== wrap) others += el.getBoundingClientRect().height;
    });
    const wrapH = wrap.clientHeight;
    if (!(tableH > 0) || !(wrapH > 0)) return;
    if (tableH > wrapH) {
      sec.style.minHeight = Math.ceil(tableH + others + 16) + "px";
      any = true;
    } else {
      sec.style.minHeight = "";
    }
  });
  // 늘릴 섹션이 하나도 없으면(짧은 표뿐) 고정 구조 자체가 필요 없다 — 클래스를 되돌린다.
  if (!any) { panel.classList.remove("yield-vfreeze"); return; }
  applyYieldVFreezeScroll(panel);
}
// 페이지 스크롤 위치 → 각 표의 scrollTop. sticky 로 고정된 wrap 은 섹션이 위로 지나간
// 만큼(pinTop - secTop) 안쪽을 내려 보여줘야 페이지 스크롤 한 번에 표가 이어서 읽힌다.
function applyYieldVFreezeScroll(panel) {
  panel = panel || document.getElementById("panel-yield");
  if (!panel || !panel.classList.contains("yield-vfreeze")) return;
  const pinTop = yieldStickyTop() + (parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue("--yield-hscroll-h")) || 14);
  yieldVFreezeSections(panel).forEach(sec => {
    const wrap = sec.querySelector(".sheet-wrap.kind-yield");
    if (!wrap) return;
    const max = wrap.scrollHeight - wrap.clientHeight;
    if (max <= 0) { wrap.scrollTop = 0; return; }
    const past = pinTop - sec.getBoundingClientRect().top;
    wrap.scrollTop = Math.max(0, Math.min(max, past));
  });
}
let _yieldVFreezeRaf = false;
window.addEventListener("scroll", () => {
  if (_yieldVFreezeRaf) return;
  const panel = document.getElementById("panel-yield");
  if (!panel || !panel.classList.contains("active")) return;
  _yieldVFreezeRaf = true;
  requestAnimationFrame(() => { _yieldVFreezeRaf = false; applyYieldVFreezeScroll(panel); });
}, { passive: true });

function syncYieldStickyOffsets(panel) {
  panel = panel || document.getElementById("panel-yield");
  if (!panel) return;
  syncYieldToolbarHeight(panel);
  syncYieldHeadRowHeight(panel);
  syncYieldVFreeze(panel);
  panel.querySelectorAll(".sheet-table.kind-yield").forEach(table => {
    let row = null;
    table.querySelectorAll("tbody tr").forEach(tr => {
      if (!row && tr.children.length >= 4 && tr.offsetParent !== null) row = tr;
    });
    if (!row) return;
    const w = [0, 1, 2].map(i => row.children[i].getBoundingClientRect().width);
    if (!w.every(v => v > 0)) return;
    table.style.setProperty("--yield-col2-left", w[0] + "px");
    table.style.setProperty("--yield-col3-left", (w[0] + w[1]) + "px");
    table.style.setProperty("--yield-col4-left", (w[0] + w[1] + w[2]) + "px");
  });
  // 표 폭이 바뀌는 시점(렌더/펼치기/열 리사이즈/창 크기)은 고정열 재실측 시점과 같으므로
  // 상단 프록시 스크롤바 폭도 여기서 함께 갱신한다.
  syncYieldHscroll(panel);
}
window.addEventListener("resize", () => syncYieldStickyOffsets());

// ── Yield 표 상단 프록시 가로 스크롤바 (사용자 요청 2026-08-04) ───────────────
// 네이티브 가로 스크롤바는 표 아래에 붙어 표가 길면 화면 밖이라 닿기 어렵다 — 표 위에 프록시
// 바를 놓고 scrollLeft 를 양방향 동기화하고, 프록시가 붙은 wrap(.has-htop)은 네이티브 가로
// 스크롤바를 시각적으로만 숨긴다(overflow 는 유지 — 휠·프로그램적 스크롤 그대로).
// #panel-yield 안의 표에만 붙인다(Issue Bin 상세의 kind-yield 표는 대상 아님).
function setupYieldHscroll(panel) {
  panel = panel || document.getElementById("panel-yield");
  if (!panel) return;
  panel.querySelectorAll(".sheet-wrap.kind-yield").forEach(wrap => {
    if (wrap.classList.contains("has-htop")) return;
    wrap.classList.add("has-htop");
    const bar = document.createElement("div");
    bar.className = "yield-hscroll";
    const spacer = document.createElement("div");
    spacer.className = "yield-hscroll-spacer";
    bar.appendChild(spacer);
    wrap.parentNode.insertBefore(bar, wrap);
    let syncing = false;   // 피드백 루프 가드 (표마다 독립)
    bar.addEventListener("scroll", () => {
      if (syncing) return;
      syncing = true; wrap.scrollLeft = bar.scrollLeft; syncing = false;
    });
    wrap.addEventListener("scroll", () => {
      if (syncing) return;
      syncing = true; bar.scrollLeft = wrap.scrollLeft; syncing = false;
    });
  });
  syncYieldHscroll(panel);
}
// 프록시 바 내부 spacer 폭 = 표 전체 폭. 넘칠 게 없으면 바 자체를 숨긴다(빈 띠 방지).
function syncYieldHscroll(panel) {
  panel = panel || document.getElementById("panel-yield");
  if (!panel) return;
  panel.querySelectorAll(".yield-hscroll").forEach(bar => {
    const wrap = bar.nextElementSibling;
    const spacer = bar.firstElementChild;
    if (!wrap || !spacer) return;
    spacer.style.width = wrap.scrollWidth + "px";
    bar.style.display = (wrap.scrollWidth > wrap.clientWidth) ? "" : "none";
  });
}

// Yield 표는 STEP 별로 표가 여러 개라 각 표에 리사이즈를 따로 건다(각자 자기 colgroup 기준).
// 폭이 바뀌면 좌측 고정열 오프셋을 다시 실측한다.
function bindYieldColResize(panel) {
  panel.querySelectorAll(".sheet-table.kind-yield").forEach(table =>
    bindSheetColResize(table, () => syncYieldStickyOffsets(panel)));
}

// ── Yield (read) ──────────────────────────────────────────────────────────────
function renderYield(yield_text, summary_rows) {
  const panel = document.getElementById("panel-yield");
  // Temperature 그룹 해석이 실패하면 이 표가 CT/HT 를 포함한 전체 source 로 계산된다
  // (metrics._temperature_context 폴백) — 틀린 숫자를 말없이 보여주지 않도록 맨 위에
  // 경고를 얹는다. 정상 세션에서는 빈 문자열이라 마크업이 종전과 동일하다.
  // overview 에 합쳐 두면 아래 두 분기(STEP 분리 표 / 폴백)가 모두 자동으로 얹는다.
  const overview = tempWarnHtml() + yieldOverviewHtml();
  // 접기 토글 위임은 STEP 표가 없는 폴백에서도 필요하다 — Temp Corner 표가 Bin 그룹
  // 토글(.yield-toggle)을 쓰기 때문(2026-08-11). 중복 호출은 가드로 no-op.
  bindYieldPanel();

  // web_report: STEP(P1/P2/P3) 별 분리 표 (yield_step_groups 가 있을 때)
  // Temperature 면 표 자체가 이미 RT source 기준이라(서버 metrics) 별도 분기가 없다 —
  // 그 아래에 CT/HT 재판정 결과를 Temp Corner 섹션으로 덧붙이기만 한다.
  const stepGroups = DATA.web_report && DATA.web_report.yield_step_groups;
  const temp = renderYieldTempSection();
  // Temp Corner 행은 청크로 나중에 붙으므로, 다 붙은 뒤 고정열 오프셋·검색어를 다시 맞춘다.
  const fillTemp = () => temp.fill(() => {
    syncYieldStickyOffsets(panel);
    bindYieldColResize(panel);
    if (yieldSearchTerm.trim()) applyYieldSearch(yieldSearchTerm);
  });
  if (Array.isArray(stepGroups) && stepGroups.length && Array.isArray(yield_text)) {
    panel.innerHTML = overview + yieldToolbarHtml() +
      renderYieldStepSections(stepGroups, yield_text) + temp.html;
    setupYieldHscroll(panel);
    syncYieldStickyOffsets(panel);
    requestAnimationFrame(() => syncYieldStickyOffsets(panel));   // 레이아웃 확정 후 재실측
    bindYieldColResize(panel);
    if (yieldSearchTerm.trim()) applyYieldSearch(yieldSearchTerm);   // 검색어 유지
    fillTemp();
    return;
  }

  // list of dicts (STEP 그룹이 없거나 fail 이 전혀 없을 때의 폴백 — Pass 행 포함 평면 표)
  if (Array.isArray(yield_text) && yield_text.length) {
    panel.innerHTML = overview +
      `<div class="yield-toolbar">` +
      yieldJumpGroupHtml() +   // STEP 표가 없는 폴백이라 보통 빈 문자열 (Temp Corner 만 있으면 노출)
      sheetSearchHtml("yieldSearchInput", yieldSearchTerm, "Item 검색") +
      yieldExcelBtnHtml() + `</div>` +
      renderSheetTable(yield_text, { kind: "yield" }) + temp.html;
    setupYieldHscroll(panel);
    syncYieldStickyOffsets(panel);
    requestAnimationFrame(() => syncYieldStickyOffsets(panel));
    bindYieldColResize(panel);
    if (yieldSearchTerm.trim()) applyYieldSearch(yieldSearchTerm);
    fillTemp();
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
// panelId 를 받아 두 패널(Issue Table / Issue Table Temp)이 같은 툴바를 쓴다. 버튼 식별은
// 고정 id 가 아니라 **data-issue-act** 다 — 같은 id 가 두 패널에 생기면 getElementById 가
// 엉뚱한 패널의 버튼을 잡는다. Temp 패널은 섹션이 1개·Bin 그룹이 없어 섹션 점프/TNO 펼치기/
// ISSUE ITEM 추가를 뺀다(ETC 는 Issue Table 전용 개념).
const ISSUE_MENU_LABEL = "☑ Issue Item 추가/변경/삭제";
function issueToolbarHtml(panelId) {
  const isTemp = panelId === ISSUE_PANEL_TEMP;
  const isCmp = panelId === ISSUE_PANEL_CMP;
  const ui = issueUi(document.getElementById(panelId || ISSUE_PANEL_MAIN));
  const searchId = isTemp ? "issueTempSearchInput"
    : (isCmp ? "issueCmpSearchInput" : "issueSearchInput");
  // 수정모드 액션(ISSUE ITEM 추가 · 선택 모드 · 선택/전체 Status · 삭제)은 버튼을 가로로
  // 늘어놓지 않고 드롭다운 메뉴 버튼 하나로 묶는다(사용자 요청 2026-08-10 — 오른쪽으로 쭉
  // 펼쳐지면 직관적이지 않다). 항목 정의·열기는 issueActionMenuHtml / toggleIssueMenu.
  const editBtns = (MODE === "edit")
    ? `<button type="button" class="btn-sm issue-menu-btn" data-issue-act="menu" ` +
      `aria-haspopup="true" aria-expanded="false" ` +
      `title="ISSUE ITEM 추가 / 행 선택 삭제 / Status 일괄 변경">${ISSUE_MENU_LABEL} ▾</button>`
    : "";
  // 'TNO 전체 펼치기' 는 툴바에서 빼고 Yield 섹션 헤더의 Step 열 아래 작은 ▼ 아이콘으로
  // 옮겼다(2026-08-10 사용자 요청 — sheets.js issueSectionHeadRowsHtml). 동작·핸들러
  // (data-issue-act="toggle-all")는 그대로다.
  // Compare 패널(ISSUE_TABLE 서브탭)의 이 표에는 섹션이 2개(Distribution/ETC)뿐이라
  // 섹션 키로만 점프한다. 구 Bin Transition/Log 앵커 점프는 2026-08-27 그 표들이
  // MAP비교/LOG비교 서브탭으로 옮겨가며 없앴다 — 숨김 서브패널의 요소로
  // scrollIntoView 하면 아무 일도 일어나지 않는다.
  const jumpAndToggle = isTemp ? "" : (isCmp
    ? `<span class="issue-jump-group" title="카테고리로 이동">` +
        `<button type="button" class="btn-sm" data-issue-jump="CMPDIST">Distribution</button>` +
        `<button type="button" class="btn-sm" data-issue-jump="CMPETC">ETC</button>` +
      `</span>` +
      // Before/After 통계 9컬럼(before_avg ~ cpk_ratio_pct)을 한꺼번에 접는다
      // (2026-08-26 사용자 요청 — 가로폭이 너무 크다). 상태는 패널별 휘발(issueUi.statsFold).
      `<button type="button" class="btn-sm" data-issue-act="cmp-stats" ` +
      `title="Before/After 통계 컬럼(avg·stdev·cpk·비교지표) 접기/펼치기">` +
      `${ui.statsFold ? "통계 펼치기" : "통계 접기"}</button>`
    : `<span class="issue-jump-group" title="섹션으로 이동">` +
        `<button type="button" class="btn-sm" data-issue-jump="Yield">YIELD</button>` +
        `<button type="button" class="btn-sm" data-issue-jump="CPK">CPK</button>` +
        `<button type="button" class="btn-sm" data-issue-jump="ETC">ETC</button>` +
      `</span>`);
  // Issue Table Temp 안내문 — 문구를 그대로 두면 툴바가 길어져(줄바꿈·버튼 밀림) 표가
  // 아래로 내려가고 하단 가로 스크롤바가 화면 밖으로 나간다(사용자 요청 2026-08-06).
  // 아이콘 하나로 줄이고 설명 전문은 hover(title) 로만 남긴다.
  const tempNote = isTemp
    ? `<span class="issue-toolbar-note" title="CT / HT 를 RT Limit(LOLIM·HILIM)으로 전 항목 재판정한 결과입니다. 한 die 가 여러 항목을 벗어나면 그 항목 전부에 계산되므로 소스별 합이 100% 를 넘을 수 있습니다.&#10;Map 셀 fail die 색: CT = 파랑 · HT = 빨강">ⓘ</span>`
    : "";
  return `<div class="issue-toolbar">` +
    jumpAndToggle +
    sheetSearchHtml(searchId, ui.search, "Item / comment 검색") +
    editBtns +
    tempNote +
    // Excel 내보내기는 Issue Table 시트 전용 서버 경로다 — Compare 표는 컬럼 구성이
    // 달라 그 시트로 나갈 수 없어 버튼을 빼둔다.
    (isCmp ? "" :
      `<button type="button" class="btn-sm issue-excel-btn" data-issue-act="excel" title="Honey Excel Download 의 Issue Table 시트와 동일한 xlsx 다운로드 (Map/Distribution 썸네일 제외)">⬇ Excel</button>`) +
    `</div>`;
}

// ── Issue Table 액션 메뉴 (툴바 버튼 → 세로 리스트 팝오버) ────────────────────
// 우클릭 컨텍스트 메뉴처럼 생긴 세로 리스트 하나로 편집 액션을 모았다(2026-08-10 사용자 요청).
// 표 스크롤 컨테이너에 잘리지 않도록 fixed 로 띄우되 **패널 안에** 붙인다 — body 직속이면
// .content 클릭 위임(edit_mode.js)이 못 잡고, sticky 툴바(z-index:92) 안에 넣으면 그
// 스태킹 컨텍스트에 갇혀 표 sticky 셀에 가린다. 한 번에 하나만 연다.
let _issueMenuEl = null;
let _issueMenuAnchor = null;
function closeIssueMenu() {
  if (!_issueMenuEl) return;
  if (_issueMenuAnchor) _issueMenuAnchor.setAttribute("aria-expanded", "false");
  _issueMenuEl.remove();
  _issueMenuEl = null; _issueMenuAnchor = null;
}
function issueMenuItemHtml(act, label, opts) {
  opts = opts || {};
  return `<button type="button" class="issue-menu-item${opts.checked ? " checked" : ""}"` +
    ` data-issue-act="${act}"${opts.disabled ? " disabled" : ""}` +
    ` title="${esc(opts.title || "")}">` +
    `<span class="issue-menu-mark">${opts.checked ? "✓" : ""}</span>` +
    `<span class="issue-menu-label">${esc(label)}</span></button>`;
}
// 항목 활성/라벨은 **여는 시점**에 계산한다 — 메뉴는 열 때마다 새로 그리므로 열려 있는
// 동안 갱신할 일이 없다(체크 변경은 메뉴가 닫힌 뒤에만 일어난다).
function issueActionMenuHtml(panel) {
  const ui = issueUi(panel);
  const on = ui.delMode;
  const n = issueSelectedChks(panel).length;
  const sep = `<div class="issue-menu-sep"></div>`;
  // ETC 는 Issue Table 전용 개념이라 Temp 패널에는 'ISSUE ITEM 추가' 가 없다.
  return (panel.id === ISSUE_PANEL_TEMP ? "" :
      issueMenuItemHtml("etc-add", "＋ ISSUE ITEM 추가", { title: "ETC 섹션에 항목을 직접 추가한다" }) + sep) +
    issueMenuItemHtml("delmode", "행 선택 모드", {
      checked: on, title: "행 체크박스를 켜고 끈다 (Step 셀 클릭 = 선택)" }) +
    issueMenuItemHtml("sel-all", "전체 선택", { disabled: !on, title: "보이는 행 전체 선택" }) +
    issueMenuItemHtml("sel-none", "선택 해제", { disabled: !on, title: "선택 모두 해제" }) +
    sep +
    issueMenuItemHtml("sel-open", "선택 Open", { disabled: !on || !n, title: "선택한 행 Status 를 Open 으로" }) +
    issueMenuItemHtml("sel-close", "선택 Close", { disabled: !on || !n, title: "선택한 행 Status 를 Close 로" }) +
    issueMenuItemHtml("del-selected", n ? `선택 삭제 (${n})` : "선택 삭제", {
      disabled: !on || !n, title: "체크한 행 일괄 삭제" }) +
    sep +
    issueMenuItemHtml("all-open", "All Open", { title: "이 표 전체 행 Status 를 Open 으로" }) +
    issueMenuItemHtml("all-close", "All Close", { title: "이 표 전체 행 Status 를 Close 로" }) +
    issueMenuItemHtml("reset-hidden", "삭제 전체 초기화", { title: "삭제(숨김)한 행 전부 복원" }) +
    sep +
    // 보기 필터 — 데이터를 바꾸지 않고 화면에서만 감춘다(삭제/숨김 편집과 무관, 저장 안 함).
    issueMenuItemHtml("hide-close", "Close 항목 숨기기", {
      checked: ui.hideClose, title: "Status = Close 인 행을 화면에서 감춘다 (다시 누르면 보기)" }) +
    issueMenuItemHtml("hide-open", "Open 항목 숨기기", {
      checked: ui.hideOpen, title: "Status = Open 인 행을 화면에서 감춘다 (다시 누르면 보기)" }) +
    issueMenuItemHtml("show-all", "전체 보기", {
      disabled: !ui.hideClose && !ui.hideOpen, title: "Open/Close 숨김을 모두 해제한다" });
}
function toggleIssueMenu(btn) {
  if (_issueMenuAnchor === btn) { closeIssueMenu(); return; }   // 같은 버튼 재클릭 = 닫기
  closeIssueMenu();
  const panel = issuePanelOf(btn);
  if (!panel) return;
  const menu = document.createElement("div");
  menu.className = "issue-menu";
  menu.innerHTML = issueActionMenuHtml(panel);
  menu.style.position = "fixed";
  menu.style.visibility = "hidden";   // 폭·높이 실측 전에는 깜빡임 방지로 숨긴다
  panel.appendChild(menu);
  const rect = btn.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let top = rect.bottom + 4;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, rect.top - mh - 4);   // 아래 공간 부족 → 위로
  menu.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - mw - 12)) + "px";
  menu.style.top = top + "px";
  menu.style.visibility = "";
  btn.setAttribute("aria-expanded", "true");
  _issueMenuEl = menu; _issueMenuAnchor = btn;
}
// 바깥 클릭 / ESC 로 닫기 (issue_dist.js map-expand-pop 과 같은 처방).
document.addEventListener("click", e => {
  if (_issueMenuEl && !e.target.closest(".issue-menu") && !e.target.closest('[data-issue-act="menu"]'))
    closeIssueMenu();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeIssueMenu(); });

// ── Issue Table 선택 모드 (일괄 삭제 / Status 일괄 변경) ─────────────────────
// 켜면 행 체크박스·개별 삭제(×)·선택 실행 버튼이 보인다(CSS .issue-del-mode).
// 삭제 후 재로드(load)로 표가 다시 그려져도 모드가 유지되도록 패널별 상태(issueUi)에 둔다.
function applyIssueDelMode(panel) {
  panel = panel || activeIssuePanel();
  if (!panel) return;
  const on = issueUi(panel).delMode;
  panel.classList.toggle("issue-del-mode", on);
  // 모드가 메뉴 안으로 들어가 보이지 않으므로 트리거 버튼 자체를 켜짐 표시(빨강)로 쓴다.
  const btn = panel.querySelector('[data-issue-act="menu"]');
  if (btn) btn.classList.toggle("active", on);
  syncIssueDelCount(panel);
  syncIssueStickyOffsets(panel);   // 체크박스 노출로 Step 열 폭이 변할 수 있어 재실측
}
// Compare 표의 Before/After 통계 9컬럼 접기 (2026-08-26 사용자 요청 — 가로폭 축소).
// 실제 숨김은 CSS(#panel-issue-cmp.cmp-stats-folded .cmp-stat-col{display:none}) 가 하고
// 여기선 패널 클래스와 버튼 라벨만 맞춘다. 컬럼이 사라지면 좌측 고정열 오프셋과 가로
// 스크롤 폭이 달라지므로 행 토글과 같은 재실측(afterIssueRowsToggled)을 태운다.
function applyCmpStatsFold(panel) {
  panel = panel || activeIssuePanel();
  if (!panel || panel.id !== ISSUE_PANEL_CMP) return;
  const on = issueUi(panel).statsFold;
  panel.classList.toggle("cmp-stats-folded", on);
  const btn = panel.querySelector('[data-issue-act="cmp-stats"]');
  if (btn) {
    btn.textContent = on ? "통계 펼치기" : "통계 접기";
    btn.classList.toggle("active", on);
  }
  afterIssueRowsToggled(panel);
}
// 체크 상태를 행 강조(tr.issue-row-sel)에 반영 — 체크박스가 작아 행 전체로 선택을 보인다.
function markIssueRowSelected(chk) {
  const tr = chk && chk.closest("tr");
  if (tr) tr.classList.toggle("issue-row-sel", chk.checked);
}
// 체크된 행 중 **논리적으로 서로 다른 이슈**만 남긴다.
// Yield 섹션은 접힘 대표행과 펼침 집계 헤더행이 같은 bin 단위 키(Yield|<bin>)를 공유해
// 체크박스가 DOM 에 2개 있다(둘은 상호 배타로 표시된다). 그대로 세면 "체크한 6개 행"
// 처럼 개수가 부풀고 일괄 요청에도 같은 키가 두 번 실린다 — 키로 접는다.
function issueSelectedChks(panel) {
  const seen = new Set();
  const out = [];
  (panel ? panel.querySelectorAll(".issue-del-chk:checked") : []).forEach(chk => {
    const key = (chk.dataset.hkey || "") + "" + (chk.dataset.etc || "");
    if (key === "" || seen.has(key)) return;   // 키 없는 체크박스는 대상 아님
    seen.add(key);
    out.push(chk);
  });
  return out;
}

// 전체 선택 / 선택 해제.
function setAllIssueDelChecked(checked, panel) {
  panel = panel || activeIssuePanel();
  if (!panel) return;
  panel.querySelectorAll(".issue-del-chk").forEach(chk => {
    chk.checked = checked;
    markIssueRowSelected(chk);
  });
  syncIssueDelCount(panel);
}
// 체크 개수를 툴바 트리거 라벨에 반영 — 선택 실행 항목이 메뉴 안으로 들어가 평소엔 보이지
// 않으므로, 몇 행을 골랐는지는 버튼에서 읽혀야 한다. 메뉴 항목의 활성/라벨은 메뉴를 여는
// 시점에 계산한다(issueActionMenuHtml).
function syncIssueDelCount(panel) {
  panel = panel || activeIssuePanel();
  if (!panel) return;
  const trigger = panel.querySelector('[data-issue-act="menu"]');
  if (!trigger) return;
  const n = issueSelectedChks(panel).length;
  trigger.textContent = ISSUE_MENU_LABEL + (issueUi(panel).delMode && n ? ` (${n})` : "") + " ▾";
}

// Issue Table Excel 내보내기는 excel_export.js exportIssueExcel — Yield/CPK 탭과 같은
// 헬퍼(hxl*)를 써서 Honey 전체본 Excel Download 의 Issue Table 시트와 서식을 맞춘다.

// Issue Table 섹션(CPK/ETC) 헤더로 스크롤 이동.
function jumpToIssueSection(sec, panel) {
  panel = panel || activeIssuePanel();
  const row = panel && panel.querySelector(`tr.issue-shead-top[data-sec="${sec}"]`);
  if (row) row.scrollIntoView({ behavior: "smooth", block: "start" });
}
// 한 Bin 그룹(대표행)의 detail TNO 행 펼치기/접기.
function setIssueGroup(gi, expand, btn, panel) {
  panel = panel || (btn && issuePanelOf(btn)) || activeIssuePanel();
  if (!panel) return;
  // 토글 버튼은 rep·agg 양쪽에 있을 수 있으므로 그 그룹 전체를 갱신한다(Yield 탭과 동일).
  panel.querySelectorAll(`.issue-toggle[data-grp="${gi}"]`).forEach(b => {
    b.setAttribute("aria-expanded", expand ? "true" : "false");
    b.textContent = expand ? "▲" : "▼";
  });
  // 3행 규칙: rep(접힘 전용) ↔ agg(펼침 전용) 상호 배타 + detail 일괄.
  // agg 행이 없는 표(Issue Table Temp — 대표행이 항목 행 자체)는 rep 를 숨기면 안 되므로
  // **agg 행이 있는 그룹에서만** rep 를 감춘다.
  const aggRows = panel.querySelectorAll(`tr.issue-bin-agg[data-grp="${gi}"]`);
  if (aggRows.length) {
    panel.querySelectorAll(`tr.issue-bin-rep[data-grp="${gi}"]`).forEach(tr => {
      tr.style.display = expand ? "none" : "";
    });
    aggRows.forEach(tr => { tr.style.display = expand ? "" : "none"; });
  }
  panel.querySelectorAll(`tr.issue-bin-detail[data-grp="${gi}"]`).forEach(tr => {
    tr.style.display = expand ? "" : "none";
  });
}
// 행 펼침/접힘으로 상세행의 긴 Item 명이 드러나면 Item 열이 넓어지는데, 좌측 고정 오프셋
// (--issue-colN-left)은 렌더 시점 실측값이라 그대로면 stale 이 된다 → Map/Distribution 이
// 옛 오프셋에 서서 Item 열 위로 겹쳐 보인다(2026-07-21 재현). 토글 뒤 반드시 재실측한다.
function afterIssueRowsToggled(panel) {
  panel = panel || activeIssuePanel();
  if (!panel) return;
  syncIssueStickyOffsets(panel);
  syncIssueHscrollSpacer(panel);
  requestAnimationFrame(() => { syncIssueStickyOffsets(panel); syncIssueHscrollSpacer(panel); });
}
function toggleIssueGroup(btn) {
  const panel = issuePanelOf(btn);
  setIssueGroup(btn.dataset.grp, btn.getAttribute("aria-expanded") !== "true", btn, panel);
  afterIssueRowsToggled(panel);
}
function setAllIssueGroups(expand, panel) {
  panel = panel || activeIssuePanel();
  if (!panel) return;
  const seen = new Set();   // 그룹당 토글 버튼이 2개(rep/agg)일 수 있다
  panel.querySelectorAll(".issue-toggle").forEach(btn => {
    const gi = btn.dataset.grp;
    if (seen.has(gi)) return;
    seen.add(gi);
    setIssueGroup(gi, expand, btn, panel);
  });
  afterIssueRowsToggled(panel);
}

// 상단 프록시 가로스크롤바 ↔ .sheet-wrap.kind-issue 실제 스크롤 동기화 (피드백 루프 가드).
let _issueHscrollSyncing = false;
function syncIssueHscrollSpacer(panel) {
  panel = panel || activeIssuePanel();
  if (!panel) return;
  const wrap = panel.querySelector(".sheet-wrap.kind-issue");
  const spacer = panel.querySelector(".issue-hscroll-spacer");
  if (!wrap || !spacer) return;
  spacer.style.width = wrap.scrollWidth + "px";
}
function bindIssueHscroll(panel) {
  const wrap = panel.querySelector(".sheet-wrap.kind-issue");
  const hscroll = panel.querySelector(".issue-hscroll");
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
// sticky 툴바 실제 높이 → --issue-toolbar-h. 표(.sheet-wrap.kind-issue)의 top·max-height 가
// 이 값을 빼서 뷰포트 안에 들어오는데, CSS 에 40px 로 박아두면 툴바가 그보다 커질 때
// (버튼이 많은 편집 모드·안내문 등) 표 하단이 화면 밖으로 밀려 **하단 가로 스크롤바가
// 보이지 않는다**(사용자 신고 2026-08-06). 실측값으로 대체한다.
function syncIssueToolbarHeight(panel) {
  const bar = panel && panel.querySelector(".issue-toolbar");
  if (!bar) return;
  const h = Math.round(bar.getBoundingClientRect().height);
  if (h > 0) panel.style.setProperty("--issue-toolbar-h", h + "px");
}

// 좌측 고정열(Step/Bin/Item/Map/Distribution)의 left 오프셋을 실제 렌더 폭으로 계산 —
// 내용이 길어 컬럼이 colWidth 힌트보다 넓어져도 셀이 겹치지(깨지지) 않게 한다.
// **TNO(3번째)는 고정 대상이 아니다**(2026-08-10 사용자 요청) — 가로 스크롤 시 Step/Bin 뒤로
// 밀려 사라지므로 그 폭(w3)을 누적에서 빼야 Item 이하가 고정 블록에 딱 붙는다.
// ⚠ 패널이 숨김(display:none, 백그라운드 프리렌더)일 때 부르면 실측이 전부 0 이라 아무 값도
// 안 심어지고 CSS fallback 이 그대로 남는다 → 탭이 보이는 시점에 다시 부를 것(tabs_topbar.js).
function syncIssueStickyOffsets(panel) {
  panel = panel || activeIssuePanel();
  if (!panel) return;
  syncIssueToolbarHeight(panel);
  const table = panel.querySelector(".sheet-table.kind-issue");
  if (!table) return;
  // 셀 6개 이상인 대표 행 하나로 앞 5개 컬럼(Step/Bin/TNO/Item/Map) 실측 폭을 잰다.
  let row = null;
  table.querySelectorAll("tbody tr").forEach(tr => { if (!row && tr.children.length >= 6) row = tr; });
  if (!row) return;
  const w1 = row.children[0].getBoundingClientRect().width;   // Step
  const w2 = row.children[1].getBoundingClientRect().width;   // Bin
  const w4 = row.children[3].getBoundingClientRect().width;   // Item (TNO=children[2] 는 고정 제외)
  const w5 = row.children[4].getBoundingClientRect().width;   // Map
  if (w1 > 0) table.style.setProperty("--issue-col2-left", w1 + "px");
  if (w1 > 0 && w2 > 0) table.style.setProperty("--issue-col4-left", (w1 + w2) + "px");
  if (w1 > 0 && w2 > 0 && w4 > 0) table.style.setProperty("--issue-col5-left", (w1 + w2 + w4) + "px");
  if (w1 > 0 && w2 > 0 && w4 > 0 && w5 > 0) table.style.setProperty("--issue-col6-left", (w1 + w2 + w4 + w5) + "px");
}
window.addEventListener("resize", () => {
  issuePanelEls().forEach(p => { syncIssueHscrollSpacer(p); syncIssueStickyOffsets(p); });
});

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

// Issue 표 렌더 본체 — Issue Table / Issue Table Temp / Issue Table Compare 세 패널과
// 조회/편집 모드가 공유한다.
// opts.edit=true 면 comment 두 열만 편집 가능(ISSUE_COMMENT_COLS).
// opts.extraHtml: 표 **뒤에 형제로** 붙일 마크업. 현재 쓰는 곳은 없다 — 구 Compare 탭의
//   Bin Transition/Log 표가 유일한 소비자였고 2026-08-27 서브탭으로 옮겨갔다. 경로는
//   남겨둔다(앞으로 같은 필요가 생길 수 있고, 아래 제약은 그때도 그대로다).
//   ⚠ 표를 감싸는 요소를 추가하면 .sheet-wrap.kind-issue 의 sticky 기준 부모가 바뀌어
//   헤더 고정이 깨진다 — 형제로만 붙인다(sheets.js renderSheetTable 주석).
//   그 안의 표에는 .sheet-table.kind-issue 클래스를 쓰지 말 것(아래 fill 대상 질의가
//   첫 표를 잡아야 한다 + 후처리가 전부 패널 전역 질의다).
// opts.afterFill: 본문 채우기가 끝난 뒤 부를 추가 후처리.
// 표 위 안내문(구 opts.intro)은 하단 가로 스크롤바를 화면 밖으로 밀어내 폐지했다 —
// Temp 탭 안내는 sticky 툴바의 .issue-toolbar-note 로 옮겼다(issueToolbarHtml).
function renderIssueTableInto(panel, rows, opts) {
  if (!panel) return;
  opts = opts || {};
  closeIssueMenu();   // 아래 innerHTML 교체가 열려 있던 메뉴를 지운다 — 참조도 같이 버린다

  if (!Array.isArray(rows) || !rows.length) {
    emptyPanel(panel, opts.emptyText || "Issue Table 데이터 없음");
    return;
  }
  // 표 본문은 청크로 채운다(프레임당 50행) — 행 수백~수천 × 20열을 통짜 innerHTML 로
  // 만들면 첫 진입(및 백그라운드 프리렌더)에서 수백 ms 를 통으로 블록한다. 삽입되는
  // 마크업·DOM 구조는 통짜 렌더와 동일하다(감싸는 요소를 추가하지 않는다). 후처리(고정열
  // 오프셋 실측·미니차트 관측 등록 등)는 행이 다 붙은 뒤 해야 실측이 맞다.
  const table = renderSheetTable(rows, opts.edit
    ? { edit: true, kind: "issue", editableCols: ISSUE_COMMENT_COLS, chunk: true }
    : { kind: "issue", chunk: true });
  panel.innerHTML = issueToolbarHtml(panel.id) +
    // 상단 프록시 가로 스크롤바는 조회 모드 전용(편집 모드는 종전대로 없다).
    (opts.edit ? "" : `<div class="issue-hscroll"><div class="issue-hscroll-spacer"></div></div>`) +
    table.html + (opts.extraHtml || "");
  table.fill(panel.querySelector(".sheet-table.kind-issue tbody"), () => {
    syncIssueHeadRowHeight(panel);
    syncIssueStickyOffsets(panel);
    requestAnimationFrame(() => syncIssueStickyOffsets(panel));   // 레이아웃 확정 후 재실측
    bindIssueHscroll(panel);
    renderIssueMiniDist(panel);
    renderIssueMiniMap(panel);
    bindIssueColResize(panel);
    applyIssueDelMode(panel);   // 재렌더 후에도 삭제 모드 유지
    applyCmpStatsFold(panel);   // Compare 통계 접기 상태 유지 (Compare 패널만 동작)
    const ui = issueUi(panel);
    if (ui.search.trim()) applyIssueSearch(ui.search, panel);   // 검색어 유지
    if (ui.hideClose || ui.hideOpen) applyIssueStatusFilter(panel);   // Status 필터 유지
    if (opts.afterFill) opts.afterFill(panel);
  });
}

function renderIssues(issue_table_text) {
  const panel = document.getElementById(ISSUE_PANEL_MAIN);
  renderIssueTableInto(panel, issue_table_text, { edit: false });
  // Yield 와 같은 경고 — Temperature 그룹 해석이 실패하면 이 표의 Yield 섹션도 CT/HT 를
  // 포함한 전체 source 로 계산된다(ctx.yield_rows 를 그대로 소비하므로 두 표는 항상 같은
  // 목록이다 — CLAUDE.md 규칙 13). 표를 그린 **뒤** 얹는다: renderIssueTableInto 가
  // innerHTML 을 통째로 교체하므로 먼저 넣으면 지워진다. 정상 세션에선 빈 문자열이라 no-op.
  // Temp 탭(renderIssueTempTab)에는 붙이지 않는다 — 그룹이 없으면 그 표 자체가 비어 있다.
  const warn = tempWarnHtml();
  if (warn && panel) panel.insertAdjacentHTML("afterbegin", warn);
}

// Temperature 전용 탭 — CT/HT 를 RT Limit 으로 전 항목 재판정한 이슈 표.
function renderIssueTempTab() {
  const panel = document.getElementById(ISSUE_PANEL_TEMP);
  renderIssueTableInto(panel, (webReportSheets() || {})[ISSUE_TEMP_SHEET], {
    edit: MODE === "edit",
    emptyText: "RT Limit 을 벗어난 CT / HT 항목이 없습니다",
  });
  // 판정 기준 안내 — 이 표가 왜 CT/HT 를 fail 로 잡는지는 source 이름 옆 RT 배지
  // tooltip 에만 있었다. renderIssueTableInto 가 innerHTML 을 통째로 교체하므로
  // 표를 그린 **뒤** 얹는다 (renderIssues 의 tempWarnHtml 과 같은 순서 이유).
  if (panel) {
    panel.insertAdjacentHTML("afterbegin",
      `<div class="temp-note">${esc(TEMP_JUDGE_NOTE_TEXT)}</div>`);
  }
}


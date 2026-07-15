// ── grid model 렌더 (xlsx 원형 재현) ─────────────────────────────────────────
function isGrid(o) { return !!(o && typeof o === "object" && Array.isArray(o.cells)); }

// 편집된 grid DOM → 원본 grid 복제본에 셀 텍스트만 반영 (legacy 저장 경로 전용)
function collectGrid(panelEl, baseGrid) {
  if (!isGrid(baseGrid)) return baseGrid;
  const out = JSON.parse(JSON.stringify(baseGrid));
  panelEl.querySelectorAll("td[data-r]").forEach(td => {
    const r = +td.dataset.r, c = +td.dataset.c;
    if (out.cells[r] && out.cells[r][c]) out.cells[r][c].t = td.textContent;
  });
  return out;
}

// ── sheet-table 렌더 (xlsx 텍스트 데이터 원형 재현) ──────────────────────────

// 열 이름 → 고정 너비(px) — xlsx 실측 기준 패턴.
// kind==="issue" 이면 Issue Table 은 Distribution 셀을 크게 보여줘야 해 전체 컬럼을 1.5배로 키운다.
function colWidth(name, kind) {
  const n = String(name || "").toLowerCase().trim();
  const s = kind === "issue" ? 1.5 : 1;   // Issue Table 전체 1.5배 확대
  const px = base => `${Math.round(base * s)}px`;
  // Step/Bin 은 최대 3자리라 아주 좁게, TNO 는 조금 넓게, avg/yield 는 xx.xx 라 짧게.
  // Issue Table 은 Map/Distribution 을 왼쪽에 함께 틀고정하므로 식별컬럼을 좁혀 고정 블록 폭을
  // 줄인다(Step 유지, Yield 표는 무영향). Map/Dist 최소폭은 CSS min-width 로 보장하고, 공간이
  // 모자라면 Item 을 더 줄이는 방향(사용자 요청) — Bin/TNO 70%, Item 55%.
  if (n === "step")                     return px(44);
  if (n === "bin")                      return px(kind === "issue" ? 44 * 0.7 : 44);
  if (n === "tno")                      return px(kind === "issue" ? 60 * 0.7 : 60);
  if (n === "map")                      return px(96);   // Distribution 과 동일 폭
  if (n === "distribution")             return px(96);   // 기존 120 의 0.8배
  if (n.endsWith("_count"))             return px(60);
  if (n.endsWith("_yield"))             return px(60);
  if (n === "avg")                      return px(48);
  if (n === "item")                     return px(kind === "issue" ? 150 * 0.55 : 150);
  if (n === "category")                 return "50px";
  if (n === "condition & judge limit")  return "185px";
  if (n === "result")                   return "80px";
  if (n.includes("comment"))            return px(220);
  return px(80);
}

// 값이 숫자인지 (td 정렬용)
function isNumVal(v) { return v !== null && v !== undefined && v !== "" && !isNaN(+v); }

function isDistCol(c) { return String(c || "").toLowerCase() === "distribution"; }
function isMapCol(c) { return String(c || "").toLowerCase() === "map"; }
function isCommentCol(c) { return /comment/i.test(String(c || "")); }
// comment 텍스트의 @[항목명] 토큰을 Item_detail 링크로 변환(그 외 텍스트는 이스케이프).
// 저장은 항상 @[항목명] 평문으로(td.textContent), 표시할 때만 링크로 보인다.
function linkifyComment(txt) {
  const s = String(txt == null ? "" : txt);
  let out = "", last = 0, m;
  const re = /@\[([^\]]+)\]/g;
  while ((m = re.exec(s))) {
    out += esc(s.slice(last, m.index));
    out += `<span class="item-detail-link" data-subject="${esc(m[1])}">@${esc(m[1])}</span>`;
    last = m.index + m[0].length;
  }
  out += esc(s.slice(last));
  return out;
}

// web_report Map Analysis 소스(웨이퍼) 개수 — 2개 이상일 때만 Map 셀 펼치기 버튼 노출.
function mapSourceCount() {
  const maps = (webReportSheets() || {})["Map Analysis"];
  return Array.isArray(maps) ? maps.length : 0;
}

// 열 순서 보정: 고정 prefix 컬럼(step/bin/tno/item, issue 는 category 포함 avg 까지)
// → 지정 순서로 맨 앞에 배치(대소문자 무시), comment 류 → 최우측. 나머지 상대순서 유지.
// yield: source 별 yield 값 → source 별 _cnt/_count → avg 순으로 그루핑.
function orderColumns(cols, kind) {
  const isComment = c => /comment/i.test(String(c));

  const comments = cols.filter(isComment);
  // Issue Table 은 Category 컬럼을 화면에 표시하지 않는다(섹션 구분은 상단 고정 헤더 라벨이 담당).
  // 단 rows 의 Category 데이터 필드는 섹션 판정(rowSection)에 그대로 쓰이므로 여기서 컬럼만 뺀다.
  let rest = cols.filter(c => !isComment(c)
    && !(kind === "issue" && String(c).trim().toLowerCase() === "category")
    && !/^_(grp|detail|ndetail)$/.test(String(c)));   // 토글 전용 내부 마킹 필드 제외

  // source(={src}_yield 컬럼 수)가 1개면 avg 는 그 source 값과 동일해 의미가 없으므로
  // Yield/Issue 표에서 avg 컬럼을 표시하지 않는다(행 데이터의 avg 값은 그대로 유지 —
  // Issue Table CPK subhead 감지가 avg 값에 의존). 2개 이상(compare/DUT 포함)이면 표시.
  const singleSource = rest.filter(c => /_yield$/i.test(String(c))).length <= 1;

  const PREFIX_ORDER = {
    yield: ["step", "bin", "tno", "item"],
    issue: ["step", "bin", "tno", "item"],
  };
  const prefix = PREFIX_ORDER[kind];
  if (prefix) {
    const lower = c => String(c).trim().toLowerCase();
    const front = [];
    prefix.forEach(p => {
      const idx = rest.findIndex(c => lower(c) === p);
      if (idx !== -1) front.push(rest.splice(idx, 1)[0]);
    });
    rest = front.concat(rest);
  }

  if (kind === "yield") {
    const isCntCol = c => /_(cnt|count)$/i.test(String(c));
    const isAvgCol = c => String(c).trim().toLowerCase() === "avg";
    const yieldVals = rest.filter(c => !isCntCol(c) && !isAvgCol(c));
    const cntVals = rest.filter(isCntCol);
    const avgVals = singleSource ? [] : rest.filter(isAvgCol);
    rest = yieldVals.concat(cntVals).concat(avgVals);
  }

  if (kind === "issue") {
    // 식별컬럼 뒤: Map → Distribution → Avg → source별 yield 순. (Avg 는 yield 그룹 헤더 아래로 묶임)
    const isMap = c => String(c).trim().toLowerCase() === "map";
    const isDist = c => String(c).trim().toLowerCase() === "distribution";
    const isAvgCol = c => String(c).trim().toLowerCase() === "avg";
    const isYieldCol = c => /_yield$/i.test(String(c));
    const map = rest.filter(isMap);
    const dist = rest.filter(isDist);
    const avg = singleSource ? [] : rest.filter(isAvgCol);
    const yields = rest.filter(isYieldCol);
    const others = rest.filter(c => !isMap(c) && !isDist(c) && !isAvgCol(c) && !isYieldCol(c));
    rest = others.concat(map).concat(dist).concat(avg).concat(yields);
  }

  return rest.concat(comments);
}

// 빈 헤더 placeholder(_colN)는 화면엔 빈칸으로
function headerLabel(c) { return /^_col\d+$/.test(String(c)) ? "" : c; }

// _yield/_count 처럼 소스별로 반복되는 컬럼이 연속 2개 이상 묶이면 상단에 "yield"/"count"
// 병합 헤더 + 하단에 접미사를 뗀 소스 짧은 이름을 보여주는 2행 헤더를 만든다.
// 그런 묶음이 없으면(소스 1개뿐이거나 다른 시트) 기존과 동일한 1행 헤더를 그대로 렌더.
const SHEET_HEADER_SUFFIX_GROUPS = [
  { re: /_yield$/i, label: "yield" },
  { re: /_(cnt|count)$/i, label: "count" },
];
function sheetHeaderShortLabel(c) {
  return headerLabel(c).replace(/_(yield|cnt|count)$/i, "");
}
// 화면 표시용 헤더 라벨 (avg → Avg 등).
function displayLabel(c) {
  const n = String(c).trim().toLowerCase();
  if (n === "avg") return "Avg";
  return headerLabel(c);
}
// 여러 source 이름을 ".." + 이름 끝 6글자로 축약한다 (사용자 요청 — 소스 이름은 보통
// 끝부분에서 달라지므로 구분 유지). 이름이 8글자 이하면 그대로 둔다. hover 로 전체이름 확인.
function abbrevSourceLabels(fulls) {
  if (!fulls || fulls.length < 2) return (fulls || []).map(f => ({ short: f, full: f }));
  return fulls.map(full =>
    (full.length <= 8) ? { short: full, full } : { short: ".." + full.slice(-6), full });
}
function buildSheetTableHead(cols) {
  const isAvgCol = c => String(c).trim().toLowerCase() === "avg";
  // avg 컬럼이 _yield 그룹 바로 앞에 오면 그 yield 그룹에 흡수(= "yield" 헤더 아래 "Avg" 열).
  const groupOf = c => SHEET_HEADER_SUFFIX_GROUPS.find(g => g.re.test(String(c)));
  const groupKeyAt = i =>
    groupOf(cols[i]) ||
    ((isAvgCol(cols[i]) && i + 1 < cols.length && /_yield$/i.test(String(cols[i + 1])))
      ? SHEET_HEADER_SUFFIX_GROUPS[0] : null);
  const runs = [];
  for (let i = 0; i < cols.length; ) {
    const g = groupKeyAt(i);
    let j = i + 1;
    if (g) { while (j < cols.length && groupKeyAt(j) === g) j++; }
    runs.push({ start: i, len: j - i, group: (g && (j - i) >= 2) ? g : null });
    i = j;
  }
  if (!runs.some(r => r.group)) {
    return "<thead><tr>" + cols.map(c => `<th>${esc(displayLabel(c))}</th>`).join("") + "</tr></thead>";
  }
  const topRow = runs.map(r => r.group
    ? `<th colspan="${r.len}" class="sheet-group-th">${esc(r.group.label)}</th>`
    : `<th rowspan="2">${esc(displayLabel(cols[r.start]))}</th>`
  ).join("");
  const botRow = runs.filter(r => r.group).map(r => {
    const runCols = cols.slice(r.start, r.start + r.len);
    // source 컬럼만 뽑아 공통 뒤글자로 축약(avg 는 "Avg" 고정 라벨).
    const srcCols = runCols.filter(c => !isAvgCol(c));
    const abbr = abbrevSourceLabels(srcCols.map(sheetHeaderShortLabel));
    const abbrByCol = {};
    srcCols.forEach((c, i) => { abbrByCol[c] = abbr[i]; });
    return runCols.map(c => {
      if (isAvgCol(c)) return `<th>Avg</th>`;
      const a = abbrByCol[c];
      const titleAttr = a.short !== a.full ? ` title="${esc(a.full)}"` : "";
      return `<th class="sheet-src-th"${titleAttr}>${esc(a.short)}</th>`;
    }).join("");
  }).join("");
  return `<thead><tr>${topRow}</tr><tr>${botRow}</tr></thead>`;
}

// 반복 섹션 헤더 행(yield의 P1/P2/P3 구간별 헤더) 감지: 셀 값이 자기 컬럼명과
// 동일한 비율이 높으면 "헤더가 데이터로 들어간" 행으로 판단.
function isHeaderLikeRow(r, cols) {
  if (!r) return false;
  let total = 0, matches = 0;
  for (const c of cols) {
    const name = String(c).trim().toLowerCase();
    if (!name || /^_col\d+$/.test(name)) continue;
    total++;
    const v = r[c];
    const txt = (v === null || v === undefined) ? "" : String(v).trim().toLowerCase();
    if (txt === name) matches++;
  }
  return total > 0 && (matches / total) >= 0.6;
}

// issue_table 의 CPK 카테고리 전환 행("item name" / "cpk" 서브헤더) 감지.
function isCpkSubheadRow(r) {
  if (!r) return false;
  const cat = String(r["Category"] ?? "").trim().toLowerCase();
  const avg = String(r["avg"] ?? "").trim().toLowerCase();
  return cat === "cpk" && avg === "cpk";
}

// yield 행 정렬(보기 전용): step 별 섹션(반복 헤더 행으로 구분) 안에서
// Bin1(PASS) 행을 맨 위에 두고, 나머지는 avg 내림차순. 합계(Sum) 행은 섹션 끝 유지.
function reorderYieldRows(rows, cols) {
  if (!rows || rows.length < 2) return rows || [];
  const binCol = cols.find(c => String(c).trim().toLowerCase() === "bin");
  const avgCol = cols.find(c => String(c).trim().toLowerCase() === "avg");
  if (!binCol || !avgCol) return rows;

  const firstCol = cols[0];
  const isPassBin = r => String((r ? r[binCol] : "") ?? "").trim() === "1";
  const isSumRow = r => String((r ? r[firstCol] : "") ?? "").trim().toLowerCase() === "sum";

  const out = [];
  let section = [];
  const flushSection = () => {
    if (!section.length) return;
    const sumRows = section.filter(isSumRow);
    const dataRows = section.filter(r => !isSumRow(r));
    const passRows = dataRows.filter(isPassBin);
    const restRows = dataRows.filter(r => !isPassBin(r))
      .slice()
      .sort((a, b) => (parseFloat(b[avgCol]) || 0) - (parseFloat(a[avgCol]) || 0));
    out.push(...passRows, ...restRows, ...sumRows);
    section = [];
  };

  rows.forEach(r => {
    if (isHeaderLikeRow(r, cols)) {
      flushSection();
      out.push(r);
    } else {
      section.push(r);
    }
  });
  flushSection();
  return out;
}

/**
 * rows(list of dict) → HTML sheet-table.
 * opts: { edit, kind:"yield"|"issue", columns:[...] }
 *  - columns: 명시적 열 목록(있으면 derive·정렬 생략, 순서 그대로).
 *    데이터행이 없어도 헤더는 항상 렌더 → 내용 없는 컬럼도 유지.
 *  - kind: comment 최우측 / (issue) category 최좌측 정렬.
 */
// Issue Table comment 저장용 행 식별 키 — 백엔드 web_report/tabs/issue_table.py 의
// manifest.issue_comments 키 규칙과 반드시 동일해야 한다:
// Yield 행 "Yield|<bin>|<item>", CPK 데이터 행 "CPK|<item>", ETC 데이터 행 "ETC|<item>".
function issueRowKey(r, section) {
  const item = String((r && r["Item"]) ?? "");
  if (!item.trim()) return "";
  if (section === "Yield") return `Yield|${String((r && r["Bin"]) ?? "")}|${item}`;
  if (section === "CPK") return `CPK|${item}`;
  if (section === "ETC") return `ETC|${item}`;
  return "";
}

// Issue Table 섹션별 2행 헤더 블록. 컬럼 구조(식별/Distribution/Avg+source/comment)는 세 섹션이
// 동일하고, 그룹 라벨(yield/cpk/etc)과 Avg 라벨(Avg↔cpk)만 섹션마다 다르다. buildSheetTableHead
// 의 run 로직을 그대로 따르되 라벨만 섹션값으로 채워 <tr.issue-shead-top>/<tr.issue-shead-bot>
// 두 줄을 만든다(둘 다 sticky top 으로 통째 고정).
const ISSUE_SECTION_LABELS = {
  Yield: { group: "yield", avg: "Avg" },
  CPK:   { group: "cpk",   avg: "cpk" },
  ETC:   { group: "etc",   avg: "Avg" },
};
function issueSectionHeadRowsHtml(cols, sec) {
  const lab = ISSUE_SECTION_LABELS[sec] || ISSUE_SECTION_LABELS.Yield;
  const isAvgCol = c => String(c).trim().toLowerCase() === "avg";
  // 컬럼 폭 드래그 리사이즈 핸들 — 단일 컬럼 th 우측 경계에 붙여 그 col 인덱스를 나른다
  // (그룹 라벨 colspan th 는 제외). colgroup.children[idx] 와 1:1 대응(bindIssueColResize).
  const resizeHandle = idx =>
    `<span class="col-resize-handle" data-col="${idx}" data-col-name="${esc(String(cols[idx]))}"></span>`;
  const groupOf = c => SHEET_HEADER_SUFFIX_GROUPS.find(g => g.re.test(String(c)));
  const groupKeyAt = i =>
    groupOf(cols[i]) ||
    ((isAvgCol(cols[i]) && i + 1 < cols.length && /_yield$/i.test(String(cols[i + 1])))
      ? SHEET_HEADER_SUFFIX_GROUPS[0] : null);
  const runs = [];
  for (let i = 0; i < cols.length; ) {
    const g = groupKeyAt(i);
    let j = i + 1;
    if (g) { while (j < cols.length && groupKeyAt(j) === g) j++; }
    runs.push({ start: i, len: j - i, group: (g && (j - i) >= 2) ? g : null });
    i = j;
  }
  if (!runs.some(r => r.group)) {
    return `<tr class="issue-shead-top" data-sec="${esc(sec)}">` +
      cols.map((c, k) => `<th>${esc(displayLabel(c))}${resizeHandle(k)}</th>`).join("") + `</tr>`;
  }
  const topRow = runs.map(r => r.group
    ? `<th colspan="${r.len}" class="sheet-group-th">${esc(lab.group)}</th>`
    : `<th rowspan="2">${esc(displayLabel(cols[r.start]))}${resizeHandle(r.start)}</th>`
  ).join("");
  const botRow = runs.filter(r => r.group).map(r => {
    const runCols = cols.slice(r.start, r.start + r.len);
    return runCols.map((c, k) => {
      const idx = r.start + k;
      if (isAvgCol(c)) return `<th>${esc(lab.avg)}${resizeHandle(idx)}</th>`;
      // Issue Table 은 source 이름을 생략하지 않고 full 로 표시(사용자 요청).
      const full = sheetHeaderShortLabel(c);
      return `<th class="sheet-src-th" title="${esc(full)}">${esc(full)}${resizeHandle(idx)}</th>`;
    }).join("");
  }).join("");
  return `<tr class="issue-shead-top" data-sec="${esc(sec)}">${topRow}</tr><tr class="issue-shead-bot">${botRow}</tr>`;
}

function renderSheetTable(rows, opts) {
  opts = opts || {};
  let cols;
  if (opts.columns && opts.columns.length) {
    cols = opts.columns.slice();
  } else {
    if (!rows || !rows.length) return "";
    cols = [];
    rows.forEach(r => Object.keys(r || {}).forEach(k => { if (!cols.includes(k)) cols.push(k); }));
    if (opts.kind) cols = orderColumns(cols, opts.kind);
  }
  if (!cols.length) return "";

  let bodyRows = rows || [];
  if (opts.kind === "yield" && !opts.edit) bodyRows = reorderYieldRows(bodyRows, cols);
  const binCol = opts.kind === "yield" ? cols.find(c => String(c).trim().toLowerCase() === "bin") : null;

  const colgroup = "<colgroup>" + cols.map(c =>
    `<col style="width:${colWidth(c, opts.kind)}">`
  ).join("") + "</colgroup>";

  // Issue 는 persistent thead 대신 섹션(Yield/CPK/ETC)별 2행 헤더 블록을 tbody 안에 sticky 로
  // 심어 스크롤 시 헤더가 통째로 교체되게 한다 → 여기선 상단 thead 를 만들지 않는다.
  const head = (opts.kind === "issue") ? "" : buildSheetTableHead(cols);

  // Issue Table 의 Category 는 화면 컬럼으로 렌더하지 않는다(섹션 식별은 2행 헤더 블록이 담당).
  // 각 행이 속한 섹션(Yield/CPK/ETC) — Category 데이터 필드가 비어있는 상세 행은 바로 위
  // 헤더 행의 섹션을 상속한다. Distribution 자동표시(Yield/ETC/CPK) 판단, ETC 상세행 삭제버튼,
  // 섹션 경계에서의 헤더 블록 삽입 판단에 쓰인다. edit 모드에서도 필요해 조건 없이 계산.
  const rowSection = {};
  if (opts.kind === "issue") {
    let sec = "";
    for (let i = 0; i < bodyRows.length; i++) {
      const cat = (bodyRows[i] && bodyRows[i]["Category"]) || "";
      if (cat) sec = cat;
      rowSection[i] = sec;
    }
  }

  // Issue Table 다중 source(=_yield 소스 컬럼 2개 이상)일 때만 문제 셀 강조:
  // CPK 섹션 소스 셀은 cpk < 1.33, Yield/ETC 섹션 소스 셀은 값이 0 이 아닌 셀을 연빨강으로.
  const issueMultiSource = opts.kind === "issue"
    && cols.filter(c => /_yield$/i.test(String(c))).length > 1;

  // Yield/ETC fail yield 셀 빨강 그라데이션의 기준값 = 각 source 컬럼 내 최대 fail yield(>0).
  // 값이 클수록 진한 빨강(--yw 1 에 가까움). 컬럼별로 나눠 정규화한다. Pass(Bin1) 행·CPK 섹션 제외.
  const issueYieldColMax = {};
  if (issueMultiSource) {
    bodyRows.forEach((r, ri) => {
      if (isCpkSubheadRow(r)) return;
      const sec = rowSection[ri];
      if (sec !== "Yield" && sec !== "ETC") return;
      if (String((r && (r["Bin"] ?? r["bin"])) ?? "").trim() === "1") return;   // Pass 행 제외
      cols.forEach(c => {
        if (!/_yield$/i.test(String(c))) return;
        const n = parseFloat(r ? r[c] : "");
        if (!isNaN(n) && n > (issueYieldColMax[c] || 0)) issueYieldColMax[c] = n;
      });
    });
  }

  const renderDataRowTr = (r, ri) => {
    const subhead = (opts.kind === "yield" && isHeaderLikeRow(r, cols))
      || (opts.kind === "issue" && isCpkSubheadRow(r));
    // 이 행이 속한 섹션(Issue Table 전용) — Map/Distribution 셀 표시 판단·셀 강조에 쓰인다.
    const issueRowSec = opts.kind === "issue" ? rowSection[ri] : "";
    // Yield 섹션 최상단 Pass(Bin1) 행 — Map/Distribution/빨강강조 제외, 초록 Pass 스타일.
    const issuePassRow = opts.kind === "issue" && !subhead
      && String((r && (r["Bin"] ?? r["bin"])) ?? "").trim() === "1";
    const tds = cols.map((c, ci) => {
      const v = r ? r[c] : "";
      let txt = (v === null || v === undefined) ? "" : String(v);
      if (subhead && txt.trim().toLowerCase() === "cpk") txt = "CPK";
      const isEmpty = txt === "";
      const isNum = isNumVal(v);
      // Map 열: 해당 행 Bin 만 원색·나머지 회색·숫자 제거한 웨이퍼(있는 Yield/ETC 행). 없으면 빈 칸.
      if (isMapCol(c)) {
        const binv = r && (r["Bin"] ?? r["bin"]);
        const hasBin = String(binv ?? "").trim() !== "";
        if (opts.kind === "issue" && !subhead && hasBin && !issuePassRow
          && (issueRowSec === "Yield" || issueRowSec === "ETC")) {
          const expandBtn = (mapSourceCount() > 1)
            ? `<button type="button" class="btn-map-expand" title="전체 소스 맵 보기">⤢</button>` : "";
          return `<td data-r="${ri}" data-c="${ci}">` +
            `<div class="map-cell map-cell-mini" data-bin="${esc(String(binv))}"><div class="map-plot"></div>${expandBtn}</div></td>`;
        }
        return `<td class="st-empty${subhead ? " sheet-subhead" : ""}" data-r="${ri}" data-c="${ci}"></td>`;
      }
      // Distribution 열: web_report 분포(있는 Item)로 작은 산포 카드를 채운다. 없으면 빈 칸.
      if (isDistCol(c)) {
        const item = r && r["Item"];
        // 분포 데이터 로딩 중(distDataReady=false)엔 일단 셀을 만들어 두고, 도착 후
        // refreshDistConsumers 가 채운다 (데이터 없는 항목은 그때 빈 칸으로).
        // Yield/ETC/CPK 섹션의 데이터 행(서브헤더 제외)에 산포 카드 표시.
        if (opts.kind === "issue" && item && !subhead && !issuePassRow
          && (rowSection[ri] === "Yield" || rowSection[ri] === "ETC" || rowSection[ri] === "CPK")
          && (distDataReady ? !!distDataCache[item] : true)) {
          return `<td${subhead ? ` class="sheet-subhead"` : ""} data-r="${ri}" data-c="${ci}">` +
            `<div class="dist-cell dist-cell-mini" data-subject="${esc(item)}"><div class="dist-plot"></div></div></td>`;
        }
        return `<td class="st-empty${subhead ? " sheet-subhead" : ""}" data-r="${ri}" data-c="${ci}"></td>`;
      }
      // opts.editableCols 가 있으면 그 컬럼만 편집 가능(더블클릭으로 활성화), 나머지는 읽기전용으로
      // 아래 일반 렌더링을 그대로 탄다. 없으면 기존처럼 opts.edit 전체 컬럼이 즉시 편집 가능.
      if (opts.edit && (!opts.editableCols || opts.editableCols.has(c))) {
        if (opts.editableCols) {
          const cls = "editing-cell dblclick-edit" + (subhead ? " sheet-subhead" : "");
          // web_report comment 저장용 행 식별 키 — 없으면(서브헤더/placeholder 행) 저장 대상 아님.
          const rowKey = (opts.kind === "issue" && !subhead) ? issueRowKey(r, rowSection[ri]) : "";
          const keyAttr = rowKey ? ` data-key="${esc(rowKey)}"` : "";
          // comment 셀: @[항목] 토큰을 링크로 표시하되 원문(data-raw)을 보관 — 더블클릭 편집 시 원문으로 되돌린다.
          const cInner = isCommentCol(c) ? linkifyComment(txt) : esc(txt);
          const rawAttr = isCommentCol(c) ? ` data-raw="${esc(txt)}"` : "";
          return `<td class="${cls}"${keyAttr}${rawAttr} data-r="${ri}" data-c="${ci}" data-col="${esc(c)}">${cInner}</td>`;
        }
        const cls = "editing-cell" + (isNum ? " st-num" : "") + (subhead ? " sheet-subhead" : "");
        return `<td class="${cls}" contenteditable="true" data-r="${ri}" data-c="${ci}" data-col="${esc(c)}">${esc(txt)}</td>`;
      }
      const clsParts = [];
      let cellStyle = "";
      if (isEmpty) clsParts.push("st-empty");
      else if (isNum) clsParts.push("st-num");
      if (subhead) clsParts.push("sheet-subhead");
      // 다중 source 문제 셀 강조(소스별 _yield 컬럼 한정). CPK 섹션은 임계 미만 연빨강(고정),
      // Yield/ETC 섹션은 값이 클수록 진한 빨강 그라데이션(표 내 최대 fail yield 기준). Pass 행 제외.
      if (issueMultiSource && !subhead && !issuePassRow && !isEmpty && /_yield$/i.test(String(c))) {
        const num = parseFloat(v);
        if (!isNaN(num)) {
          if (issueRowSec === "CPK") { if (num <= CPK_WARN_THRESHOLD) clsParts.push("issue-cell-warn"); }
          else if (num > 0) {
            const cmax = issueYieldColMax[c] || 0;
            const ratio = cmax > 0 ? Math.min(1, num / cmax) : 0;
            clsParts.push("issue-yield-warn");
            cellStyle = ` style="--yw:${ratio.toFixed(3)}"`;
          }
        }
      }
      const cls = clsParts.join(" ");
      // Item 셀: 클릭 시 Item_detail 로 이동(항목명 = 측정항목). issue + yield(Bin 상세 구성표) 공용.
      // Pass(Bin 1) 행과 자유입력 Engr ETC 항목(TNO 없음 = 측정항목 아님)은 제외.
      const etcFreeform = opts.kind === "issue" && rowSection[ri] === "ETC"
        && String((r && r["TNO"]) ?? "").trim() === "";
      const itemClickable = (opts.kind === "issue" || opts.kind === "yield") && !subhead && !isEmpty
        && c === "Item" && String((r && (r["Bin"] ?? r["bin"])) ?? "").trim() !== "1" && !etcFreeform;
      // ETC 섹션의 상세 행(ENGR 가 수동 추가한 item) Item 셀: 수정 모드에서만 삭제(×) 버튼 노출.
      const etcDeletable = opts.kind === "issue" && opts.edit && !isEmpty && c === "Item"
        && rowSection[ri] === "ETC" && String(r["Category"] || "") === "";
      let cellHtml;
      if (itemClickable) {
        cellHtml = `<span class="item-detail-link" data-subject="${esc(txt)}">${esc(txt)}</span>`;
      } else if (opts.kind === "issue" && isCommentCol(c) && !isEmpty) {
        cellHtml = linkifyComment(txt);   // 읽기 모드 comment: @[항목] → Item_detail 링크
      } else {
        cellHtml = isEmpty ? "" : esc(txt);
      }
      if (etcDeletable) {
        cellHtml += ` <button type="button" class="btn-del-etc-item" data-item="${esc(txt)}" title="ETC 항목 제거">×</button>`;
      }
      // Issue Table Yield 대표행 STEP 셀 오른쪽에 접기/펼치기 토글(그 Bin 의 detail TNO 가 있을 때).
      if (opts.kind === "issue" && c === "Step" && r && r._grp && !r._detail && (Number(r._ndetail) || 0) > 0) {
        cellHtml += ` <button type="button" class="issue-toggle" data-grp="${esc(r._grp)}" aria-expanded="false">▼</button>`;
      }
      // 읽기 모드 Issue Table 셀에만 data-col 부여 → CSS 로 BIN/ITEM/Yield/CPK 폰트 확대(값 가독성).
      // 편집 모드는 부여하지 않아 collectSheetTable 저장 대상(=comment 셀)이 그대로 유지된다.
      const colAttr = (opts.kind === "issue" && !opts.edit) ? ` data-col="${esc(c)}"` : "";
      return `<td${cls ? ` class="${cls}"` : ""}${cellStyle}${colAttr} data-r="${ri}" data-c="${ci}">${cellHtml}</td>`;
    }).join("");
    const isPassRow = !subhead && (issuePassRow
      || (binCol && String((r ? r[binCol] : "") ?? "").trim() === "1"));
    let trAttr = isPassRow ? ` class="yield-pass-row"` : "";
    if (!isPassRow && opts.kind === "issue" && r && r._grp) {
      trAttr = r._detail
        ? ` class="issue-bin-detail" data-grp="${esc(r._grp)}" style="display:none"`
        : ` class="issue-bin-rep" data-grp="${esc(r._grp)}"`;
    }
    return `<tr${trAttr}>${tds}</tr>`;
  };

  let body;
  if (opts.kind === "issue") {
    // 섹션이 바뀔 때마다 그 섹션 2행 헤더 블록을 먼저 심고, 헤더가 대체하는 divider 행
    // (CPK 서브헤더 / ETC 라벨행)은 데이터로 렌더하지 않는다.
    let out = "";
    let curSec = null;
    bodyRows.forEach((r, ri) => {
      const sec = rowSection[ri];
      if (sec && sec !== curSec) { curSec = sec; out += issueSectionHeadRowsHtml(cols, sec); }
      if (isCpkSubheadRow(r)) return;
      if (String((r && r["Category"]) || "").trim() === "ETC") return;
      out += renderDataRowTr(r, ri);
    });
    body = "<tbody>" + out + "</tbody>";
  } else {
    body = "<tbody>" + bodyRows.map(renderDataRowTr).join("") + "</tbody>";
  }

  const kindCls = opts.kind ? ` kind-${opts.kind}` : "";
  return `<div class="sheet-wrap${kindCls}"><table class="sheet-table${kindCls}">${colgroup}${head}${body}</table></div>`;
}

// 편집된 sheet-table DOM → rows 재구성
function collectSheetTable(panelEl, baseRows) {
  if (!baseRows || !baseRows.length) return baseRows;
  const cols = [];
  baseRows.forEach(r => Object.keys(r || {}).forEach(k => { if (!cols.includes(k)) cols.push(k); }));
  const tds = panelEl.querySelectorAll("td[data-r][data-col]");
  const out = JSON.parse(JSON.stringify(baseRows));
  tds.forEach(td => {
    const ri = +td.dataset.r, col = td.dataset.col;
    if (out[ri] && col in out[ri]) out[ri][col] = td.textContent;
  });
  return out;
}

// summary blocks 편집 수집
function collectSummaryBlocks(panelEl, baseData) {
  if (!baseData || !baseData.blocks) return baseData;
  const out = JSON.parse(JSON.stringify(baseData));
  const blockEls = panelEl.querySelectorAll(".sheet-block");
  out.blocks.forEach((blk, bi) => {
    const blockEl = blockEls[bi];
    if (!blockEl) return;
    const tds = blockEl.querySelectorAll("td[data-r][data-c]");
    tds.forEach(td => {
      const ri = +td.dataset.r, ci = +td.dataset.c;
      if (out.blocks[bi].rows[ri] !== undefined) {
        out.blocks[bi].rows[ri][ci] = td.textContent;
      }
    });
  });
  return out;
}

// isSummaryBlocks: {"blocks":[...]} 형태인지
function isSummaryBlocks(o) {
  return !!(o && typeof o === "object" && Array.isArray(o.blocks) && o.blocks.length > 0);
}

// Yield 상단 요약 박스 HTML (web_report 세션의 yield_summary 가 있을 때만).
function yieldOverviewHtml() {
  const ov = DATA.web_report && DATA.web_report.yield_summary;
  if (!ov) return "";
  const pct = (typeof ov.yield_pct === "number") ? ov.yield_pct.toFixed(2) : ov.yield_pct;
  // 소스가 2개 이상일 때만 소스별 수율을 따로 표시(단일 소스는 Total 과 동일하므로 생략).
  // yield% 내림차순(높은 순 위 → 아래) 정렬. 메인 Total Yield 오른쪽에 작은 테이블로 붙인다.
  const bySrc = (Array.isArray(ov.by_source) ? ov.by_source.slice() : [])
    .sort((a, b) => (Number(b.yield_pct) || 0) - (Number(a.yield_pct) || 0));
  const bySrcHtml = bySrc.length >= 2 ? `<div class="yield-by-source"><table class="ybs-table">
    <thead><tr><th>Source</th><th>Yield</th><th>Pass / Total</th></tr></thead>
    <tbody>` + bySrc.map(s => {
    const sp = (typeof s.yield_pct === "number") ? s.yield_pct.toFixed(2) : s.yield_pct;
    return `<tr>
      <td class="ybs-src">${esc(s.source)}</td>
      <td class="ybs-pct">${esc(sp)}%</td>
      <td class="ybs-cnt">${esc(s.pass)} / ${esc(s.total)}</td>
    </tr>`;
  }).join("") + `</tbody></table></div>` : "";
  // STEP(P1/P2/P3) 별 요약: 분모는 항상 전체 rawdata(In=전체 die, 전 STEP 동일).
  // Step Yield = (전체 - 그 STEP fail) / 전체 = 그 STEP fail 만 제외한 수율(전체 기준).
  const byStep = Array.isArray(ov.by_step) ? ov.by_step : [];
  const byStepHtml = byStep.length ? `<div class="yield-by-step"><table class="ybs-table">
    <thead><tr><th>Step</th><th>Step Yield</th><th>Pass / In</th><th>Fail</th></tr></thead>
    <tbody>` + byStep.map(s => {
    const sp = (typeof s.step_yield_pct === "number") ? s.step_yield_pct.toFixed(2) : s.step_yield_pct;
    return `<tr>
      <td class="ybs-src">${esc(s.step)}</td>
      <td class="ybs-pct">${esc(sp)}%</td>
      <td class="ybs-cnt">${esc(s.survivor)} / ${esc(s.entered)}</td>
      <td class="ybs-cnt">${esc(s.fail)}</td>
    </tr>`;
  }).join("") + `</tbody></table></div>` : "";
  return `<div class="yield-overview">
    <div class="yo-pct">${esc(pct)}%</div>
    <div class="yo-stats">
      <div class="yo-stat"><span class="yo-num">${esc(ov.pass)}</span><span class="yo-label">Pass</span></div>
      <div class="yo-stat"><span class="yo-num">${esc(ov.total)}</span><span class="yo-label">Total</span></div>
      <div class="yo-stat yo-fail"><span class="yo-num">${esc(ov.fail)}</span><span class="yo-label">Fail</span></div>
    </div>
    ${byStepHtml}
    ${bySrcHtml}
  </div>`;
}


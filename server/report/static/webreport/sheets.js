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
// narrowSrc: source 컬럼이 SRC_ABBREV_MIN 이상이라 헤더가 공통부분을 뗀 짧은 라벨(01/02/…)로
// 표시될 때 — 값이 숫자뿐이므로 {src}_yield/_count 폭을 숫자 크기에 맞게 좁힌다.
function colWidth(name, kind, narrowSrc) {
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
  if (n.endsWith("_count"))             return px(narrowSrc ? 38 : 60);
  if (n.endsWith("_yield"))             return px(narrowSrc ? 38 : 60);
  if (n === "avg")                      return px(48);
  if (n === "status")                   return px(56);   // Issue Table Open/Close 드랍다운
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

// 입력 소스(파일) 개수 — CPK 행 STDF 미니맵은 소스별 측정값이라 이 수로 펼치기를 판단한다
// (Map Analysis 맵 개수는 STEP 분리로 소스 수보다 많을 수 있어 mapSourceCount 와 별개).
function webReportSourceCount() {
  const srcs = DATA.web_report && DATA.web_report.sources;
  return Array.isArray(srcs) ? srcs.length : 0;
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
    // 식별컬럼 뒤: Map → Distribution → Avg → source별 yield → Status 순.
    // (Avg 는 yield 그룹 헤더 아래로 묶임. Status 는 좌측 sticky 6컬럼 뒤 comment 앞 —
    //  좌측 고정 블록 폭 계산(syncIssueStickyOffsets)에 영향 주지 않는 위치.)
    const isMap = c => String(c).trim().toLowerCase() === "map";
    const isDist = c => String(c).trim().toLowerCase() === "distribution";
    const isAvgCol = c => String(c).trim().toLowerCase() === "avg";
    const isYieldCol = c => /_yield$/i.test(String(c));
    const isStatus = c => String(c).trim().toLowerCase() === "status";
    const map = rest.filter(isMap);
    const dist = rest.filter(isDist);
    const avg = singleSource ? [] : rest.filter(isAvgCol);
    const yields = rest.filter(isYieldCol);
    const status = rest.filter(isStatus);
    const others = rest.filter(c => !isMap(c) && !isDist(c) && !isAvgCol(c) && !isYieldCol(c) && !isStatus(c));
    rest = others.concat(map).concat(dist).concat(avg).concat(yields).concat(status);
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
// COLUMN_DISPLAY_ALIAS: 저장 키(= 편집 DB·eval export·클라 Excel 이 쓰는 컬럼명)는 그대로 두고
// 화면/Excel 내보내기 헤더 표기만 바꾼다 — 키를 바꾸면 기존 세션의 저장된 comment 가 유실된다.
const COLUMN_DISPLAY_ALIAS = { "개발 comment": "개발팀 Comment" };
function displayLabel(c) {
  const n = String(c).trim().toLowerCase();
  if (n === "avg") return "Avg";
  const alias = COLUMN_DISPLAY_ALIAS[String(c).trim()];
  return alias !== undefined ? alias : headerLabel(c);
}

// ── source 헤더 라벨 축약 (사용자 요청 2026-07-21) ────────────────────────────
// source 컬럼이 이 수 이상이면 헤더에서 공통 부분을 생략하고 서로 다른 부분만 보여준다.
// 예: kucak_01 … kucak_11 → 첫 컬럼만 "kucak_01" 전체, 나머지는 "02" … "11".
const SRC_ABBREV_MIN = 8;

// 이름 목록의 공통 접두/접미 길이(문자 수). 이름이 1개 이하면 축약 대상 아님.
function commonAffixLen(names) {
  if (!names || names.length < 2) return { pre: 0, suf: 0 };
  const first = names[0];
  let pre = first.length;
  names.forEach(n => {
    let i = 0;
    while (i < pre && i < n.length && n[i] === first[i]) i++;
    pre = i;
  });
  let suf = first.length - pre;
  names.forEach(n => {
    const cap = Math.min(suf, n.length - pre);
    let i = 0;
    while (i < cap && n[n.length - 1 - i] === first[first.length - 1 - i]) i++;
    suf = i;
  });
  return { pre, suf };
}

// source 전체 이름 목록 → [{short, full}]. SRC_ABBREV_MIN 미만이면 전부 전체 이름 그대로.
// 이상이면 첫 컬럼만 전체 이름이고 나머지는 공통 접두/접미를 뗀 부분만 남긴다(빈 문자열이
// 되면 전체 이름으로 폴백 — 이름이 전부 같은 경우).
function sourceHeaderLabels(fulls) {
  const names = (fulls || []).map(f => String(f));
  if (names.length < SRC_ABBREV_MIN) return names.map(f => ({ short: f, full: f }));
  const { pre, suf } = commonAffixLen(names);
  return names.map((full, i) => {
    if (i === 0) return { short: full, full };
    const core = full.slice(pre, full.length - suf);
    return { short: core || full, full };
  });
}

// 표의 source 컬럼({src}_yield) 수 — 축약·narrow 폭 판단 기준(Yield/Issue 공용).
function sourceColCount(cols) {
  return (cols || []).filter(c => /_yield$/i.test(String(c))).length;
}
// 여러 source 이름을 ".." + 이름 끝 6글자로 축약한다 (사용자 요청 — 소스 이름은 보통
// 끝부분에서 달라지므로 구분 유지). 이름이 8글자 이하면 그대로 둔다. hover 로 전체이름 확인.
function abbrevSourceLabels(fulls) {
  if (!fulls || fulls.length < 2) return (fulls || []).map(f => ({ short: f, full: f }));
  return fulls.map(full =>
    (full.length <= 8) ? { short: full, full } : { short: ".." + full.slice(-6), full });
}
// opts.resize=true 면 단일 컬럼 th 우측에 폭 드래그 핸들을 심는다(colgroup 인덱스 동반).
// Yield 표에서 쓰며(bindSheetColResize), 지정 없으면 기존과 동일한 헤더를 그대로 낸다.
function buildSheetTableHead(cols, opts) {
  opts = opts || {};
  const handle = opts.resize
    ? idx => `<span class="col-resize-handle" data-col="${idx}" data-col-name="${esc(String(cols[idx]))}"></span>`
    : () => "";
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
  const commentCls = c => isCommentCol(c) ? ` class="st-comment"` : "";
  if (!runs.some(r => r.group)) {
    return "<thead><tr>" + cols.map((c, k) =>
      `<th${commentCls(c)}>${esc(displayLabel(c))}${handle(k)}</th>`).join("") + "</tr></thead>";
  }
  const topRow = runs.map(r => r.group
    ? `<th colspan="${r.len}" class="sheet-group-th">${esc(r.group.label)}</th>`
    : `<th rowspan="2"${commentCls(cols[r.start])}>${esc(displayLabel(cols[r.start]))}${handle(r.start)}</th>`
  ).join("");
  const botRow = runs.filter(r => r.group).map(r => {
    const runCols = cols.slice(r.start, r.start + r.len);
    // source 컬럼만 뽑아 축약(avg 는 "Avg" 고정 라벨). source 가 SRC_ABBREV_MIN 이상이면
    // 공통 접두/접미를 뗀 라벨(첫 컬럼만 전체), 미만이면 기존 뒤글자 축약.
    const srcCols = runCols.filter(c => !isAvgCol(c));
    const fulls = srcCols.map(sheetHeaderShortLabel);
    const abbr = srcCols.length >= SRC_ABBREV_MIN
      ? sourceHeaderLabels(fulls) : abbrevSourceLabels(fulls);
    const abbrByCol = {};
    srcCols.forEach((c, i) => { abbrByCol[c] = abbr[i]; });
    return runCols.map((c, k) => {
      const idx = r.start + k;
      if (isAvgCol(c)) return `<th>Avg${handle(idx)}</th>`;
      const a = abbrByCol[c];
      const titleAttr = a.short !== a.full ? ` title="${esc(a.full)}"` : "";
      return `<th class="sheet-src-th"${titleAttr}>${esc(a.short)}${handle(idx)}</th>`;
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

// Issue Table 행 숨김/Status 용 이슈 단위 키 — 백엔드 edits.py KIND_ISSUE_HIDDEN/
// KIND_ISSUE_STATUS 규약과 반드시 동일해야 한다: Yield 는 bin 단위 "Yield|<bin>"
// (대표행에만 부여 — 상세행/Pass 행 제외), CPK 행 "CPK|<item>", ETC 행 "ETC|<item>".
function issueHideStatusKey(r, section) {
  const item = String((r && r["Item"]) ?? "").trim();
  if (section === "Yield") {
    const bin = String((r && r["Bin"]) ?? "").trim();
    return (bin && bin !== "1" && r && r._grp && !r._detail) ? `Yield|${bin}` : "";
  }
  if (section === "CPK") return item ? `CPK|${item}` : "";
  if (section === "ETC") return item ? `ETC|${item}` : "";
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
  const commentCls = c => isCommentCol(c) ? ` class="st-comment"` : "";
  if (!runs.some(r => r.group)) {
    return `<tr class="issue-shead-top" data-sec="${esc(sec)}">` +
      cols.map((c, k) => `<th${commentCls(c)}>${esc(displayLabel(c))}${resizeHandle(k)}</th>`).join("") + `</tr>`;
  }
  const topRow = runs.map(r => r.group
    ? `<th colspan="${r.len}" class="sheet-group-th">${esc(lab.group)}</th>`
    : `<th rowspan="2"${commentCls(cols[r.start])}>${esc(displayLabel(cols[r.start]))}${resizeHandle(r.start)}</th>`
  ).join("");
  const botRow = runs.filter(r => r.group).map(r => {
    const runCols = cols.slice(r.start, r.start + r.len);
    // source 이름은 SRC_ABBREV_MIN 미만이면 full 표시(기존 동작), 이상이면 공통 접두/접미를
    // 뗀 라벨(첫 컬럼만 full) — 소스가 많을 때 열너비를 숫자 크기까지 좁히기 위함.
    const srcCols = runCols.filter(c => !isAvgCol(c));
    const labels = sourceHeaderLabels(srcCols.map(sheetHeaderShortLabel));
    const labByCol = {};
    srcCols.forEach((c, i) => { labByCol[c] = labels[i]; });
    return runCols.map((c, k) => {
      const idx = r.start + k;
      if (isAvgCol(c)) return `<th>${esc(lab.avg)}${resizeHandle(idx)}</th>`;
      const a = labByCol[c];
      return `<th class="sheet-src-th" title="${esc(a.full)}">${esc(a.short)}${resizeHandle(idx)}</th>`;
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

  // source 가 많으면 헤더가 공통부분을 뗀 짧은 라벨이 되므로 그 컬럼 폭도 함께 좁힌다.
  const narrowSrc = sourceColCount(cols) >= SRC_ABBREV_MIN;
  const colgroup = "<colgroup>" + cols.map(c =>
    `<col style="width:${colWidth(c, opts.kind, narrowSrc)}">`
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

  // Issue Table CPK 섹션 연빨강 강조(cpk < 1.33)는 다중 source(=_yield 소스 컬럼 2개 이상)일
  // 때만(단일 소스는 cpk 값이 avg 와 동일해 중복). Yield/ETC 섹션 빨강 그라데이션은 소스 수와
  // 무관하게 Yield 탭과 동일하게 적용한다(아래 issueYieldColMax).
  const issueMultiSource = opts.kind === "issue"
    && cols.filter(c => /_yield$/i.test(String(c))).length > 1;

  // Yield/ETC fail yield 셀 빨강 그라데이션의 기준값 = 각 source 컬럼 내 최대 fail yield(>0).
  // 값이 클수록 진한 빨강(--yw 1 에 가까움). 컬럼별로 나눠 정규화한다. Pass(Bin1) 행·CPK 섹션 제외.
  const issueYieldColMax = {};
  if (opts.kind === "issue") {
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
    // 삭제 대상 키 — Yield 대표행/CPK 행은 숨김 키, ETC 상세행은 item 명. 행 단위로 한 번만
    // 계산해 첫 컬럼(Step) 체크박스와 Item 셀 개별 삭제(×) 버튼이 같은 기준을 쓰게 한다.
    const rowItemTxt = String((r && r["Item"]) ?? "").trim();
    const delHideKey = (opts.kind === "issue" && opts.edit && !subhead && !issuePassRow
      && ((issueRowSec === "Yield" && r && r._grp && !r._detail)
        || (issueRowSec === "CPK" && rowItemTxt !== "")))
      ? issueHideStatusKey(r, issueRowSec) : "";
    const delEtcItem = (opts.kind === "issue" && opts.edit && issueRowSec === "ETC"
      && String((r && r["Category"]) || "") === "" && rowItemTxt !== "") ? rowItemTxt : "";
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
            `<div class="map-cell map-cell-mini" data-bin="${esc(String(binv))}" ` +
            `title="클릭하면 Map Analysis 탭에서 이 Bin 을 강조해 봅니다"><div class="map-plot"></div>${expandBtn}</div></td>`;
        }
        // CPK 섹션은 Bin 이 없다 — 대신 그 Item 의 STDF Map(측정값 10분위) 미니맵을 넣는다.
        const cpkItem = String((r && r["Item"]) ?? "").trim();
        if (opts.kind === "issue" && !subhead && issueRowSec === "CPK" && cpkItem !== "") {
          const stdfExpand = (webReportSourceCount() > 1)
            ? `<button type="button" class="btn-map-expand" title="전체 소스 맵 보기">⤢</button>` : "";
          return `<td data-r="${ri}" data-c="${ci}">` +
            `<div class="map-cell map-cell-mini map-cell-stdf" data-subject="${esc(cpkItem)}" ` +
            `title="클릭하면 Map Analysis 탭 STDF Map 으로 이 Item 을 봅니다"><div class="map-plot"></div>${stdfExpand}</div></td>`;
        }
        return `<td class="st-empty${subhead ? " sheet-subhead" : ""}" data-r="${ri}" data-c="${ci}"></td>`;
      }
      // Distribution 열: web_report 분포(있는 Item)로 작은 산포 카드를 채운다. 없으면 빈 칸.
      if (isDistCol(c)) {
        const item = r && r["Item"];
        // 분포 유무는 distribution_index(=/full 에 이미 있음)로 판단한다 — ECDF 는 보이는
        // 셀만 배치로 받으므로 캐시 보유 여부로 판단하면 아직 안 받은 항목의 셀이 통째로
        // 안 만들어진다. Yield/ETC/CPK 섹션의 데이터 행(서브헤더 제외)에 산포 카드 표시.
        if (opts.kind === "issue" && item && !subhead && !issuePassRow
          && (rowSection[ri] === "Yield" || rowSection[ri] === "ETC" || rowSection[ri] === "CPK")
          && distHasData(item)) {
          // CPK 섹션 미니셀은 규격내(limit 안) 구간만 재정규화해 그린다(data-limitwin) —
          // 행의 cpk 값(cpk_limited)과 동일 기준. Yield/ETC 는 기존 전체 범위 유지.
          const limitWin = rowSection[ri] === "CPK" ? ` data-limitwin="1"` : "";
          return `<td${subhead ? ` class="sheet-subhead"` : ""} data-r="${ri}" data-c="${ci}">` +
            `<div class="dist-cell dist-cell-mini" data-subject="${esc(item)}"${limitWin}><div class="dist-plot"></div></div></td>`;
        }
        return `<td class="st-empty${subhead ? " sheet-subhead" : ""}" data-r="${ri}" data-c="${ci}"></td>`;
      }
      // Status 열: 이슈 행(Yield 대표/CPK/ETC — 백엔드가 값 채움)만 Open/Close 표시.
      // 편집모드는 드랍다운(변경 즉시 저장 — edit_mode.js 위임), 조회모드는 텍스트.
      if (String(c).trim().toLowerCase() === "status") {
        const skey = (opts.kind === "issue" && !subhead) ? issueHideStatusKey(r, rowSection[ri]) : "";
        if (!skey || txt === "") {
          return `<td class="st-empty${subhead ? " sheet-subhead" : ""}" data-r="${ri}" data-c="${ci}"></td>`;
        }
        // 드랍다운(또는 텍스트) 아래 신호등 점 — Open 빨강 / Close 초록. 색은 td 의
        // is-open/is-close 클래스가 결정한다(편집모드 변경 시 edit_mode.js 가 갱신).
        const statusCls = `issue-status-cell ${txt === "Close" ? "is-close" : "is-open"}`;
        if (opts.edit) {
          return `<td class="${statusCls}" data-r="${ri}" data-c="${ci}"><select class="issue-status-sel" data-skey="${esc(skey)}">` +
            `<option value="Open"${txt !== "Close" ? " selected" : ""}>Open</option>` +
            `<option value="Close"${txt === "Close" ? " selected" : ""}>Close</option></select>` +
            `<span class="status-dot"></span></td>`;
        }
        return `<td class="issue-status ${statusCls}" data-r="${ri}" data-c="${ci}">${esc(txt)}<span class="status-dot"></span></td>`;
      }
      // opts.editableCols 가 있으면 그 컬럼만 편집 가능(더블클릭으로 활성화), 나머지는 읽기전용으로
      // 아래 일반 렌더링을 그대로 탄다. 없으면 기존처럼 opts.edit 전체 컬럼이 즉시 편집 가능.
      if (opts.edit && (!opts.editableCols || opts.editableCols.has(c))) {
        if (opts.editableCols) {
          const cls = "editing-cell dblclick-edit" + (subhead ? " sheet-subhead" : "")
            + (isCommentCol(c) ? " st-comment" : "");
          // web_report comment 저장용 행 식별 키 — 없으면(서브헤더/placeholder 행) 저장 대상 아님.
          const rowKey = (opts.kind === "issue" && !subhead) ? issueRowKey(r, rowSection[ri]) : "";
          const keyAttr = rowKey ? ` data-key="${esc(rowKey)}"` : "";
          // comment 셀: @[항목] 토큰을 링크로 표시하되 원문(data-raw)을 보관 — 더블클릭 편집 시 원문으로 되돌린다.
          const cInner = isCommentCol(c) ? linkifyComment(txt) : esc(txt);
          const rawAttr = isCommentCol(c) ? ` data-raw="${esc(txt)}"` : "";
          return `<td class="${cls}"${keyAttr}${rawAttr} data-r="${ri}" data-c="${ci}" data-col="${esc(c)}">${cInner}</td>`;
        }
        const cls = "editing-cell" + (isNum ? " st-num" : "") + (subhead ? " sheet-subhead" : "")
          + (isCommentCol(c) ? " st-comment" : "");
        return `<td class="${cls}" contenteditable="true" data-r="${ri}" data-c="${ci}" data-col="${esc(c)}">${esc(txt)}</td>`;
      }
      const clsParts = [];
      if (isCommentCol(c)) clsParts.push("st-comment");   // 열너비 고정 (CSS .st-comment)
      let cellStyle = "";
      if (isEmpty) clsParts.push("st-empty");
      else if (isNum) clsParts.push("st-num");
      if (subhead) clsParts.push("sheet-subhead");
      // 문제 셀 강조(소스별 _yield 컬럼 한정). Yield/ETC 섹션은 값이 클수록 진한 빨강
      // 그라데이션(표 내 최대 fail yield 기준, Yield 탭과 동일 — 소스 1개여도 적용). CPK 섹션은
      // 임계 미만 연빨강이되 다중 소스일 때만(단일 소스는 cpk 가 avg 와 동일해 중복). Pass 행 제외.
      if (opts.kind === "issue" && !subhead && !issuePassRow && !isEmpty && /_yield$/i.test(String(c))) {
        const num = parseFloat(v);
        if (!isNaN(num)) {
          if (issueRowSec === "CPK") { if (issueMultiSource && num <= CPK_WARN_THRESHOLD) clsParts.push("issue-cell-warn"); }
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
      let cellHtml;
      if (itemClickable) {
        cellHtml = `<span class="item-detail-link" data-subject="${esc(txt)}">${esc(txt)}</span>`;
      } else if (opts.kind === "issue" && isCommentCol(c) && !isEmpty) {
        cellHtml = linkifyComment(txt);   // 읽기 모드 comment: @[항목] → Item_detail 링크
      } else {
        cellHtml = isEmpty ? "" : esc(txt);
      }
      // Item 셀 개별 삭제(×) — 삭제 모드에서만 보인다(CSS .issue-del-mode).
      if (c === "Item" && delEtcItem) {
        cellHtml += ` <button type="button" class="btn-del-etc-item" data-item="${esc(delEtcItem)}" title="ETC 항목 제거">×</button>`;
      }
      if (c === "Item" && delHideKey) {
        cellHtml += ` <button type="button" class="btn-del-issue-row" data-hkey="${esc(delHideKey)}" title="이 행 삭제(숨김) — 복원은 툴바 '삭제 전체 초기화'">×</button>`;
      }
      // 일괄 삭제용 체크박스 — 첫 컬럼(Step) 셀 왼쪽. 삭제 모드에서만 보인다.
      if (opts.kind === "issue" && ci === 0 && (delHideKey || delEtcItem)) {
        cellHtml = `<input type="checkbox" class="issue-del-chk"` +
          (delHideKey ? ` data-hkey="${esc(delHideKey)}"` : ` data-etc="${esc(delEtcItem)}"`) +
          ` title="삭제 대상 선택">` + cellHtml;
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

  // 본문 행 생성 — 행 구간(슬라이스) 단위로 부를 수 있게 분리한다(청크 렌더용).
  // state.curSec 는 "섹션이 바뀌면 그 섹션 2행 헤더 블록을 먼저 심는다"는 판단의 이월
  // 상태라, 슬라이스를 이어 부를 때 호출자가 같은 객체를 계속 넘겨야 한다.
  function emitRows(start, end, state) {
    let out = "";
    for (let ri = start; ri < end; ri++) {
      const r = bodyRows[ri];
      if (opts.kind === "issue") {
        const sec = rowSection[ri];
        if (sec && sec !== state.curSec) {
          state.curSec = sec;
          out += issueSectionHeadRowsHtml(cols, sec);
        }
        // 헤더 블록이 대체하는 divider 행(CPK 서브헤더 / ETC 라벨행)은 데이터로 안 그린다.
        if (isCpkSubheadRow(r)) continue;
        if (String((r && r["Category"]) || "").trim() === "ETC") continue;
      }
      out += renderDataRowTr(r, ri);
    }
    return out;
  }

  const kindCls = opts.kind ? ` kind-${opts.kind}` : "";
  const shell = bodyHtml =>
    `<div class="sheet-wrap${kindCls}"><table class="sheet-table${kindCls}">` +
    `${colgroup}${head}<tbody>${bodyHtml}</tbody></table></div>`;

  // chunk: 본문을 통짜로 만들지 않고, 빈 tbody 를 가진 표 골격 html 과 그것을 채우는
  // fill(tbody, onDone) 을 돌려준다. Issue Table 은 행 수백~수천 × 20열이라 한 번에 만들면
  // 수백 ms 를 통으로 블록한다. **DOM 구조는 통짜 렌더와 완전히 같다** — 호출부가 이 html 을
  // 기존과 같은 자리에 그대로 넣기 때문이다(감싸는 요소를 추가하면 .sheet-wrap.kind-issue 의
  // position:sticky 기준 부모가 바뀌어 고정 동작이 달라진다).
  if (opts.chunk) {
    return {
      html: shell(""),
      fill: (tbody, onDone) =>
        sheetChunkFill(tbody, bodyRows.length, emitRows, onDone),
    };
  }
  return shell(emitRows(0, bodyRows.length, { curSec: null }));
}

// ── 표 본문 청크 채우기 ───────────────────────────────────────────────────────
// 총 작업량은 같고 프레임 단위로 쪼개기만 한다 — 행 내용·순서·DOM 은 통짜 렌더와 동일.
// 같은 tbody 에 새 렌더가 시작되면 토큰이 바뀌어 이전 체인이 스스로 멈춘다.
const SHEET_CHUNK_ROWS = 50;
const _sheetChunkTokens = new WeakMap();
function sheetChunkFill(tbody, total, emitRows, onDone) {
  if (!tbody) return;
  const token = (_sheetChunkTokens.get(tbody) || 0) + 1;
  _sheetChunkTokens.set(tbody, token);
  const state = { curSec: null };
  let i = 0;
  const step = () => {
    if (_sheetChunkTokens.get(tbody) !== token) return;   // 새 렌더가 시작됨 — 중단
    const end = Math.min(total, i + SHEET_CHUNK_ROWS);
    if (end > i) {
      tbody.insertAdjacentHTML("beforeend", emitRows(i, end, state));
      i = end;
    }
    if (i < total) requestAnimationFrame(step);
    else if (onDone) onDone();
  };
  step();   // 첫 청크는 동기 — 빈 표가 한 프레임이라도 보이지 않게
}

// ── 컬럼 폭 드래그 리사이즈 (Yield 등 thead 표 공용) ─────────────────────────
// buildSheetTableHead(cols, {resize:true}) 가 심은 .col-resize-handle(data-col=인덱스)을 끌어
// 그 <col> width 를 바꾼다. 저장 없음(새로고침 시 기본 폭 복귀). Issue Table 은 미니차트
// 재렌더 등 고유 후처리가 있어 별도 바인더(bindIssueColResize)를 유지한다.
// afterResize(선택): 드래그 중/끝에 부를 후처리(고정열 오프셋 재실측 등).
function bindSheetColResize(table, afterResize) {
  const colgroup = table && table.querySelector("colgroup");
  if (!table || !colgroup) return;
  const MIN_W = 24;
  table.addEventListener("mousedown", e => {
    const handle = e.target.closest(".col-resize-handle");
    if (!handle) return;
    const col = colgroup.children[+handle.dataset.col];
    if (!col) return;
    const th = handle.closest("th");
    const startW = th ? th.getBoundingClientRect().width : parseFloat(col.style.width) || 80;
    const startX = e.clientX;
    e.preventDefault();   // 드래그 중 텍스트 선택 방지
    let rafPending = false;
    const sync = () => { rafPending = false; if (afterResize) afterResize(); };
    const onMove = ev => {
      col.style.width = Math.max(MIN_W, Math.round(startW + (ev.clientX - startX))) + "px";
      if (!rafPending) { rafPending = true; requestAnimationFrame(sync); }
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      sync();
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
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
  // 정렬하지 않고 payload(by_source) 순서 = source 순서 그대로 — 아래 STEP×Source 표와
  // 소스 나열 순서를 맞춘다.
  const bySrc = Array.isArray(ov.by_source) ? ov.by_source : [];
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
  // STEP×Source 표: STEP 셀은 소스 수만큼 rowspan 병합(병합 셀에 STEP 평균 yield 표시).
  // 분모는 각 소스 전체 die(In) 로 **고정**하고 분자만 누적 차감한다 —
  // Cum Yield = (In − 그 STEP 까지의 누적 fail) / In. avg = 소스 산술평균.
  // Fail 열은 "그 STEP 자체 fail / 누적 fail" 2값 — survivor + cum_fail = In 이 성립한다.
  const byStep = Array.isArray(ov.by_step) ? ov.by_step : [];
  const byStepHtml = byStep.length ? `<div class="yield-by-step"><table class="ybs-table">
    <thead><tr><th>Step</th><th>Source</th><th>Cum Yield</th><th>Pass / In</th><th>Fail (step / cum)</th></tr></thead>
    <tbody>` + byStep.map(s => {
    // sources 가 없으면(옛 payload) pooled 값으로 1행 폴백.
    const srcs = (Array.isArray(s.sources) && s.sources.length) ? s.sources
      : [{ source: "", yield_pct: s.step_yield_pct, survivor: s.survivor, entered: s.entered,
           fail: s.fail, cum_fail: s.cum_fail }];
    const avg = (typeof s.avg_yield_pct === "number") ? s.avg_yield_pct.toFixed(2)
      : (s.avg_yield_pct != null ? s.avg_yield_pct : s.step_yield_pct);
    return srcs.map((sr, i) => {
      const sp = (typeof sr.yield_pct === "number") ? sr.yield_pct.toFixed(2) : sr.yield_pct;
      // cum_fail 이 없는 옛 캐시 payload(스키마 bump 전)는 자기 STEP fail 만 표시.
      const failTxt = (sr.cum_fail === null || sr.cum_fail === undefined)
        ? `${esc(sr.fail)}` : `${esc(sr.fail)} / ${esc(sr.cum_fail)}`;
      const stepCell = i === 0
        ? `<td class="ybs-step" rowspan="${srcs.length}">${esc(s.step)}<span class="ybs-step-avg">avg ${esc(avg)}%</span></td>`
        : "";
      return `<tr>
      ${stepCell}
      <td class="ybs-src">${esc(sr.source)}</td>
      <td class="ybs-pct">${esc(sp)}%</td>
      <td class="ybs-cnt">${esc(sr.survivor)} / ${esc(sr.entered)}</td>
      <td class="ybs-cnt">${failTxt}</td>
    </tr>`;
    }).join("");
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

// ── Excel식 셀 선택/복사 (Issue Table) ─────────────────────────────────────────
// 클릭 = 1셀, 드래그 = 사각 범위 선택 → Ctrl+C 로 TSV 복사 (Excel 붙여넣기 호환).
// 이벤트 위임(document)이라 탭 재렌더에도 리스너 재바인딩이 필요 없다.
// 활성 contenteditable·버튼·select 등 interactive 요소에서는 선택을 시작하지 않아
// 편집 렌더(dblclick 편집·상태 select·TNO 펼침)와 간섭하지 않는다.
const CELLSEL_SCOPE = "#panel-issues";  // 적용 패널 — 확장 시 콤마 셀렉터로 추가
let _cellSel = null;   // {table, grid, r1, c1, r2, c2}
let _cellDrag = null;  // {table, r1, c1, moved}
let _cellPainted = []; // 현재 .cell-sel 이 붙은 td — 도색 해제를 표 전체 스캔 없이 하기 위함

// 좌표 → td 맵. 드래그 시작 시 1회만 만든다 (mousemove 마다 표 전체를 훑지 않도록).
function _cellGrid(table) {
  const grid = new Map();
  table.querySelectorAll("td[data-r][data-c]").forEach(td => {
    grid.set(td.dataset.r + ":" + td.dataset.c, td);
  });
  return grid;
}

function cellSelClear() {
  if (!_cellSel) return;
  _cellPainted.forEach(td => td.classList.remove("cell-sel"));
  _cellPainted = [];
  _cellSel = null;
}

// 선택 사각형(minR..maxR × minC..maxC)을 .cell-sel 클래스로 표시 (숨김 행 제외).
// 새 사각형만 칠하고 직전 도색분 중 범위 밖만 지운다 — 표가 커도 mousemove 비용이 일정.
function cellSelPaint(sel) {
  const rA = Math.min(sel.r1, sel.r2), rB = Math.max(sel.r1, sel.r2);
  const cA = Math.min(sel.c1, sel.c2), cB = Math.max(sel.c1, sel.c2);
  const next = [];
  for (let r = rA; r <= rB; r++) {
    for (let c = cA; c <= cB; c++) {
      const td = sel.grid.get(r + ":" + c);
      if (!td || !td.offsetParent) continue;
      td.classList.add("cell-sel");
      next.push(td);
    }
  }
  _cellPainted.forEach(td => {
    const r = +td.dataset.r, c = +td.dataset.c;
    if (r < rA || r > rB || c < cA || c > cB || !td.offsetParent) td.classList.remove("cell-sel");
  });
  _cellPainted = next;
}

// 셀 표시 텍스트 — select 는 현재 값, 그 외는 렌더 텍스트(개행은 공백으로, TSV 격자 보존).
function cellSelText(td) {
  const sel = td.querySelector("select");
  if (sel) return String(sel.value || "");
  return String(td.innerText || "").replace(/\s+/g, " ").trim();
}

// 선택 범위 → TSV. 접힌(display:none) 행은 제외, 행 안의 빈 좌표는 빈 문자열로 채운다.
function cellSelTsv(sel) {
  const rA = Math.min(sel.r1, sel.r2), rB = Math.max(sel.r1, sel.r2);
  const cA = Math.min(sel.c1, sel.c2), cB = Math.max(sel.c1, sel.c2);
  const grid = new Map();  // r → Map(c → text)
  let count = 0;
  sel.table.querySelectorAll("td[data-r][data-c]").forEach(td => {
    const r = +td.dataset.r, c = +td.dataset.c;
    if (r < rA || r > rB || c < cA || c > cB || !td.offsetParent) return;
    if (!grid.has(r)) grid.set(r, new Map());
    grid.get(r).set(c, cellSelText(td));
    count++;
  });
  const lines = [];
  // spread 대신 Array.from — QJSEngine 검증 하네스 파서 호환 (js-verify 관례)
  Array.from(grid.keys()).sort((a, b) => a - b).forEach(r => {
    const row = grid.get(r), cells = [];
    for (let c = cA; c <= cB; c++) cells.push(row.has(c) ? row.get(c) : "");
    lines.push(cells.join("\t"));
  });
  return { text: lines.join("\n"), count };
}

// HTTP LAN 환경에선 navigator.clipboard 가 없어(secure context 아님) execCommand 폴백 필수.
function cellSelCopyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => true, () => _cellSelExecCopy(text));
  }
  return Promise.resolve(_cellSelExecCopy(text));
}
function _cellSelExecCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

// mousedown 대상이 선택 가능한 셀이면 반환 (interactive 요소 위는 null).
function _cellSelTarget(ev) {
  const td = ev.target.closest ? ev.target.closest("td[data-r][data-c]") : null;
  if (!td || !td.closest(CELLSEL_SCOPE)) return null;
  if (ev.target.closest("button, select, a, input, textarea, [contenteditable='true']")) return null;
  return td;
}

document.addEventListener("mousedown", (ev) => {
  if (ev.button !== 0) return;
  const td = _cellSelTarget(ev);
  if (!td) { cellSelClear(); return; }  // 표 밖/interactive 요소 클릭 → 선택 해제
  const table = td.closest("table");
  cellSelClear();
  _cellDrag = { table, r1: +td.dataset.r, c1: +td.dataset.c, moved: false };
  _cellSel = { table, grid: _cellGrid(table),
               r1: _cellDrag.r1, c1: _cellDrag.c1, r2: _cellDrag.r1, c2: _cellDrag.c1 };
  cellSelPaint(_cellSel);
});

document.addEventListener("mousemove", (ev) => {
  if (!_cellDrag) return;
  const td = ev.target.closest ? ev.target.closest("td[data-r][data-c]") : null;
  if (!td || td.closest("table") !== _cellDrag.table) return;
  const r2 = +td.dataset.r, c2 = +td.dataset.c;
  if (_cellSel && r2 === _cellSel.r2 && c2 === _cellSel.c2) return;  // 같은 셀이면 재도색 생략
  if (!_cellDrag.moved) {
    // 드래그 진입 시점에만 네이티브 텍스트 선택 억제 (단일 클릭 셀 내 텍스트 선택은 보존)
    _cellDrag.moved = true;
    _cellDrag.table.classList.add("cell-drag");
    const s = window.getSelection && window.getSelection();
    if (s) s.removeAllRanges();
  }
  _cellSel.r2 = r2;
  _cellSel.c2 = c2;
  cellSelPaint(_cellSel);
});

document.addEventListener("mouseup", () => {
  if (_cellDrag) { _cellDrag.table.classList.remove("cell-drag"); _cellDrag = null; }
});

document.addEventListener("keydown", (ev) => {
  if (!_cellSel) return;
  if (ev.key === "Escape") { cellSelClear(); return; }
  if (!(ev.ctrlKey || ev.metaKey) || (ev.key !== "c" && ev.key !== "C")) return;
  // 사용자가 텍스트를 직접 드래그 선택했거나 편집 중이면 기본 복사에 양보
  const s = window.getSelection && window.getSelection();
  if (s && String(s).length) return;
  const ae = document.activeElement;
  if (ae && (ae.isContentEditable || ae.tagName === "INPUT" || ae.tagName === "TEXTAREA")) return;
  const { text, count } = cellSelTsv(_cellSel);
  if (!count) return;
  ev.preventDefault();
  cellSelCopyText(text).then(ok => {
    if (typeof showToast === "function") {
      showToast(ok ? `${count}개 셀 복사됨` : "복사 실패 — 브라우저가 클립보드를 차단했습니다");
    }
  });
});


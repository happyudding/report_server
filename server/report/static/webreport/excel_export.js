// 탭별 Excel Down (vendored exceljs — trim.js loadExcelJS 재사용, 서버 무변경).
//
// Honey 클라이언트 "Excel Download"(client/excel_download/_sheets.py) 의 Yield/CPK/
// Issue Table 시트와 **같은 레이아웃·서식**으로 xlsx 를 브라우저에서 만든다. 입력이 같은
// /full payload 라 값 파리티는 자동으로 맞고, 여기서는 시트 배치(배너·헤더행 위치·STEP
// 분리·행 필터·셀 병합·강조 fill)를 맞춘다. 파이썬 함수를 직접 부를 수는 없으므로
// _sheets.py 규칙을 이 파일 한 곳에 미러링하고, 3개 탭이 같은 hxl* 헬퍼를 공유한다
// (탭마다 따로 만들면 서식이 다시 갈라진다 — 2026-07-23 통합).
//
// 전체본(Excel Download)과의 유일한 의도적 차이: Issue Table 의 Map/Distribution 은
// **열만 있고 이미지는 없다**(브라우저에서 썸네일을 렌더하지 않는다).
//
// 순수 빌더(buildYieldSheetData/buildCpkSheetData/buildIssueSheetData)는 DOM·ExcelJS 를
// 참조하지 않는다 — QJSEngine 으로 함수만 추출해 검증하기 위한 관례(spread 문법도 안 쓴다).

// ── _xlsx_style.py / _sheets.py 상수 미러 ──────────────────────────────────
const HXL = {
  HEADER_ROW: 3,          // 표 헤더 행 (B3 부터 시작)
  START_COL: 2,           // B열
  TITLE_MAX_COL: 26,      // 제목 배너 A1:Z1
  HDR_FILL: "FFD9E1F2",
  DATA_FILL: "FFFFFFFF",
  TITLE_FILL: "FFBDD7EE",
  CPK_WARN_FILL: "FFFFF3B0",
  ISSUE_FAIL_FILL: "FFFAD4D4",   // Issue Table fail yield > 0 (_ISSUE_FAIL_FILL_RGB)
  CPK_THRESHOLD: 1.33,
  HDR_FONT: { name: "Calibri", size: 11 },
  DATA_FONT: { name: "Calibri", size: 10 },
  BANNER_FONT: { name: "Tahoma", bold: true, size: 22 },   // 모든 시트 A1 제목
  SECTION_FONT: { name: "Tahoma", size: 20 },              // 표 위 섹션 제목(STEP 등)
  LINK_FONT: { name: "Calibri", bold: true, size: 12, color: { argb: "FF0563C1" } },
  YIELD_HDR_H: 40,
  YIELD_ROW_H: 22,
  YIELD_COL_W: 6.5 * 1.6,   // _NARROW_COL_WIDTH * 1.6
  CPK_N_COL_W: 6.5 * 1.05,
  // 전체본이 썸네일 물리크기(82.8pt / 187.2pt)에 맞춘 열너비의 문자폭 환산값.
  ISSUE_MAP_COL_W: 11.07,
  ISSUE_DIST_COL_W: 26.0,
};

const CPK_SHEET_HEADER = ["TEST NAME", "LOW SPEC", "HIGH SPEC", "SCALE", "계열", "n",
  "min", "median", "max", "average", "stdev",
  "cpl", "cpu", "cp", "cpk", "comment"];
const CPK_LABEL_NCOL = 4;    // TEST NAME/LOW SPEC/HIGH SPEC/SCALE — 계열 간 공통이라 병합
// 전체본(_sheets.py _COMMENT_COLS)과 같은 2개만 — AI Comment 는 양쪽 모두 내보내지 않는다.
const HXL_ISSUE_COMMENT_COLS = ["PTE comment", "개발 comment"];
const ISSUE_ID_COLS = ["Category", "Step", "Bin", "TNO", "Item"];

// ── 순수 빌더 (DOM/ExcelJS 무의존) ──────────────────────────────────────────
function hxlSourceNames(report) {
  const srcs = (report && report.sources) || [];
  return srcs.map(s => (s && s.name) || "");
}

function hxlNum(v) {
  const n = parseFloat(v);
  return isNaN(n) ? null : n;
}

// _sheets.py yield_header / _yield_row_values / write_yield_sheet 파리티.
// STEP(P1/P2/P3) 마다 표 1개 — 맨 위 Pass 행(그 STEP 까지의 누적 수율) + Bin 대표행(접힘).
// yield_step_groups 가 없으면(구 payload) 전체 Bin 표 1개로 폴백한다.
function buildYieldSheetData(report) {
  const srcs = hxlSourceNames(report);
  const header = ["Step", "Bin", "TNO", "Item", "avg (%)"]
    .concat(srcs.map(s => `${s} (%)`))
    .concat(srcs.map(s => `${s} count`));

  function rowValues(row) {
    const r = row || {};
    return [r.step, r.bin, r.TNO, r.Item, r.avg]
      .concat(srcs.map(s => r[`${s}_yield`]))
      .concat(srcs.map(s => r[`${s}_count`]));
  }

  // _sheets.py _step_pass_row_values — yield_summary.by_step 의 누적 수율/생존 die.
  function stepPassRow(step) {
    const byStep = ((report && report.yield_summary) || {}).by_step || [];
    const st = byStep.find(s => String((s && s.step) || "") === String(step || ""));
    if (!st) return null;
    const bySrc = {};
    (st.sources || []).forEach(s => { if (s) bySrc[s.source] = s; });
    return ["", "1", "", "Pass", st.avg_yield_pct]
      .concat(srcs.map(s => (bySrc[s] || {}).yield_pct))
      .concat(srcs.map(s => (bySrc[s] || {}).survivor));
  }

  const stepGroups = (report && report.yield_step_groups) || [];
  const sections = [];
  if (stepGroups.length) {
    stepGroups.forEach(sg => {
      const rows = [];
      const pass = stepPassRow((sg || {}).step);
      if (pass) rows.push(pass);
      ((sg || {}).groups || []).forEach(g => rows.push(rowValues((g || {}).rep)));
      sections.push({
        title: `STEP ${String((sg || {}).step || "").trim() || "(기타)"}`,
        rows: rows,
      });
    });
    return { header: header, sections: sections };
  }

  const sheets = (report && report.sheets) || {};
  const yieldRows = sheets["Yield"] || [];
  const rows = [];
  if (yieldRows.length && String((yieldRows[0] || {}).bin) === "1") {
    rows.push(rowValues(yieldRows[0]));
  }
  ((report && report.yield_bin_groups) || []).forEach(g => rows.push(rowValues((g || {}).rep)));
  sections.push({ title: "", rows: rows });
  return { header: header, sections: sections };
}

// _sheets.py write_cpk_sheet 파리티 — sheets["CPK"] 전량·원순서(화면 필터 무관).
// 통계 컬럼은 Bin1(양품) 기준 단일 값이다(서버 tabs/cpk.py 통일, 2026-07-23).
function buildCpkSheetData(report) {
  const sheets = (report && report.sheets) || {};
  const src = sheets["CPK"] || [];
  const rows = [];
  const warnOffsets = [];
  src.forEach(r0 => {
    const r = r0 || {};
    const cpk = parseFloat(r.cpk);
    // NaN 은 경고 아님 — python float() 예외를 무시하는 것과 같다.
    if (cpk < HXL.CPK_THRESHOLD) warnOffsets.push(rows.length);
    rows.push([r.subject, r.lower_limit, r.upper_limit, r.units, r.source, r.n,
      r.min, r.median, r.max, r.average, r.stdev,
      r.cpl, r.cpu, r.cp, r.cpk, ""]);
  });
  const labelRuns = blankRepeatedCpkLabels(rows);
  return { header: CPK_SHEET_HEADER, rows: rows, warnOffsets: warnOffsets,
    labelRuns: labelRuns };
}

// 같은 subject 연속 행의 TEST NAME/SPEC/SCALE 반복 생략 (_blank_repeated_labels).
// 반환: 같은 라벨을 공유하는 연속 구간 [[첫 행 offset, 행 수], ...] — 세로 병합에 쓴다.
function blankRepeatedCpkLabels(rows) {
  let prevKey = null;
  const runs = [];
  rows.forEach((row, i) => {
    const key = JSON.stringify(row.slice(0, CPK_LABEL_NCOL));
    if (key === prevKey) {
      for (let c = 0; c < CPK_LABEL_NCOL; c++) row[c] = "";
      runs[runs.length - 1][1] += 1;
    } else {
      prevKey = key;
      runs.push([i, 1]);
    }
  });
  return runs;
}

// _sheets.py write_issue_sheet 파리티 — 컬럼 순서(식별 → Map → Distribution → avg →
// source → Status → comment), 접힌 detail 행 comment 흡수, CPK 서브헤더 소스명,
// 강조(Yield/ETC fail>0 / CPK<1.33), Category 세로 병합 구간.
// rows: DATA.issue_table_text (화면 편집이 반영된 배열).
// 반환 offset 은 데이터 행 기준(0=첫 데이터 행) — 기입 시 HEADER_ROW+1 을 더한다.
function buildIssueSheetData(issueRows, srcs) {
  const header = ISSUE_ID_COLS.concat(["Map", "Distribution", "avg"])
    .concat(srcs).concat(["Status"]).concat(HXL_ISSUE_COMMENT_COLS);
  const rows = [];
  const fails = [];        // [[rowOffset, colOffset], ...] — colOffset 은 header 기준
  const warns = [];
  const subheads = [];     // CPK 서브헤더 행 offset
  const merges = [];       // Category 병합 [[startOffset, endOffset], ...]
  const avgCol = ISSUE_ID_COLS.length + 2;   // Map/Distribution 다음이 avg

  // _grp 별 detail comment 수집 (접힌 상세행 → 대표행 comment 칸에 "<Item>: <내용>")
  const detailComments = {};
  (issueRows || []).forEach(r0 => {
    const r = r0 || {};
    if (!r._detail) return;
    HXL_ISSUE_COMMENT_COLS.forEach(col => {
      const text = String(r[col] || "").trim();
      if (!text) return;
      const key = `${r._grp} ${col}`;
      (detailComments[key] = detailComments[key] || []).push(`${r.Item}: ${text}`);
    });
  });

  let section = "";
  let span = null;
  (issueRows || []).forEach(r0 => {
    const r = r0 || {};
    if (r._detail) return;
    if (r.Category === "Yield" || r.Category === "CPK" || r.Category === "ETC") {
      section = r.Category;
    }
    const off = rows.length;
    const subhead = section === "CPK" && String(r.avg || "").trim().toLowerCase() === "cpk";
    const binText = String(r.Bin === undefined || r.Bin === null ? "" : r.Bin).trim();
    const isPass = binText === "1";

    const srcVals = subhead ? srcs.slice() : srcs.map(s => r[`${s}_yield`]);
    const vals = [r.Category, r.Step, r.Bin, r.TNO, r.Item, "", "", r.avg]
      .concat(srcVals).concat([r.Status || ""]);
    HXL_ISSUE_COMMENT_COLS.forEach(col => {
      const parts = [];
      const own = String(r[col] || "").trim();
      if (own) parts.push(own);
      const extra = detailComments[`${r._grp} ${col}`];
      if (extra) extra.forEach(t => parts.push(t));
      vals.push(parts.join("\n"));
    });

    if (subhead) {
      subheads.push(off);
    } else if (!isPass) {
      [r.avg].concat(srcs.map(s => r[`${s}_yield`])).forEach((v, i) => {
        const num = hxlNum(v);
        if (num === null) return;
        if ((section === "Yield" || section === "ETC") && num > 0) fails.push([off, avgCol + i]);
        else if (section === "CPK" && num < HXL.CPK_THRESHOLD) warns.push([off, avgCol + i]);
      });
    }

    if (span && span.section === section) span.end = off;
    else { span = { section: section, start: off, end: off }; merges.push(span); }
    rows.push(vals);
  });

  return {
    header: header, rows: rows, fails: fails, warns: warns, subheads: subheads,
    merges: merges.filter(m => m.end > m.start).map(m => [m.start, m.end]),
  };
}

// ── ExcelJS 기입 헬퍼 (3개 탭 공용) ────────────────────────────────────────
function hxlFill(argb) {
  return { type: "pattern", pattern: "solid", fgColor: { argb: argb } };
}
const HXL_BORDER = {
  top: { style: "thin" }, left: { style: "thin" },
  bottom: { style: "thin" }, right: { style: "thin" },
};

// xlwings 는 Range 1회, ExcelJS 는 셀 단위 — 결과 서식은 같다.
function hxlStyleCells(ws, r1, c1, r2, c2, st) {
  for (let r = r1; r <= r2; r++) {
    for (let c = c1; c <= c2; c++) {
      const cell = ws.getCell(r, c);
      if (st.fill) cell.fill = hxlFill(st.fill);
      if (st.font) cell.font = st.font;
      if (st.center) cell.alignment = { horizontal: "center", vertical: "middle", wrapText: true };
      if (st.border) cell.border = HXL_BORDER;
    }
  }
}

// 모든 시트 A1 = 시트명 제목 배너 (Tahoma 22 Bold, 배경 연파랑 — _title_banner 파리티).
function hxlTitleBanner(ws, text) {
  ws.getCell(1, 1).value = text;
  hxlStyleCells(ws, 1, 1, 1, HXL.TITLE_MAX_COL, { fill: HXL.TITLE_FILL, font: HXL.BANNER_FONT });
}

// 표 위 섹션 제목 1줄 (_section_label — Yield 의 "STEP P1" 등).
function hxlSectionLabel(ws, row, text) {
  ws.getCell(row, HXL.START_COL).value = text;
  ws.getCell(row, HXL.START_COL).font = HXL.SECTION_FONT;
}

// 모든 시트 H1 에 세션 웹뷰 링크 (honey add_session_link 와 동일 위치·문구).
function hxlSessionLink(ws) {
  const text = "▶ 웹에서 이 세션 열기";
  const cell = ws.getCell(1, 8);
  cell.value = {
    text: text,
    hyperlink: `${location.origin}/pe/report/view/${SESSION_ID}`,
    tooltip: text,
  };
  cell.font = HXL.LINK_FONT;
}

// 헤더+데이터를 (headerRow, B) 부터 기입 (_write_table). 반환: 마지막 데이터 행 번호.
function hxlWriteTable(ws, header, rows, headerRow) {
  const hr = headerRow || HXL.HEADER_ROW;
  const c1 = HXL.START_COL;
  const c2 = c1 + header.length - 1;
  header.forEach((h, i) => { ws.getCell(hr, c1 + i).value = h; });
  hxlStyleCells(ws, hr, c1, hr, c2,
    { fill: HXL.HDR_FILL, font: HXL.HDR_FONT, center: true, border: true });
  rows.forEach((row, ri) => {
    row.forEach((v, ci) => {
      ws.getCell(hr + 1 + ri, c1 + ci).value = (v === undefined || v === null) ? null : v;
    });
  });
  if (rows.length) {
    hxlStyleCells(ws, hr + 1, c1, hr + rows.length, c2,
      { fill: HXL.DATA_FILL, font: HXL.DATA_FONT, center: true, border: true });
  }
  return hr + rows.length;
}

// 데이터 행 offset 목록에 배경색 (헤더행 기준 상대 → 절대 행 변환).
function hxlFillCellsAt(ws, cells, argb, headerRow) {
  const hr = headerRow || HXL.HEADER_ROW;
  cells.forEach(rc => {
    ws.getCell(hr + 1 + rc[0], HXL.START_COL + rc[1]).fill = hxlFill(argb);
  });
}

function hxlSetColWidths(ws, header, widths, defWidth) {
  header.forEach((name, i) => {
    const w = Object.prototype.hasOwnProperty.call(widths, name) ? widths[name] : defWidth;
    if (w !== undefined && w !== null) ws.getColumn(HXL.START_COL + i).width = w;
  });
}

// 한 열의 연속 행을 세로 병합 (_merge_label_runs / Category 병합).
function hxlMergeCol(ws, r1, r2, col) {
  if (r2 <= r1) return;
  ws.mergeCells(r1, col, r2, col);
  ws.getCell(r1, col).alignment = { horizontal: "center", vertical: "middle", wrapText: true };
}

async function hxlDownload(wb, baseName) {
  const buf = await wb.xlsx.writeBuffer();
  const blob = new Blob([buf],
    { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  const meta = (DATA && DATA.session) || {};
  a.download = `${baseName}_${meta.lot_id || SESSION_ID}.xlsx`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  showToast("Excel 다운로드 완료");
}

// 시트 1장짜리 workbook 생성 공통부 (제목 배너 + 세션 링크).
async function hxlNewSheet(name) {
  const ExcelJS = await loadExcelJS();
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet(name);
  hxlTitleBanner(ws, name);
  hxlSessionLink(ws);
  return { wb: wb, ws: ws };
}

// ── Yield 탭 Excel Down ─────────────────────────────────────────────────────
async function exportYieldExcel() {
  const report = DATA && DATA.web_report;
  const btn = document.getElementById("yieldExcelBtn");
  if (!report) { showToast("Yield 데이터가 없습니다"); return; }
  if (btn) btn.disabled = true;
  try {
    const d = buildYieldSheetData(report);
    const total = d.sections.reduce((n, s) => n + s.rows.length, 0);
    if (!total) { showToast("내보낼 Yield 행이 없습니다"); return; }
    const wsx = await hxlNewSheet("Yield");
    const ws = wsx.ws;
    let row = HXL.HEADER_ROW;                 // 첫 섹션 제목(또는 헤더) = B3
    d.sections.forEach(sec => {
      if (sec.title) { hxlSectionLabel(ws, row, sec.title); row += 1; }
      hxlWriteTable(ws, d.header, sec.rows, row);
      ws.getRow(row).height = HXL.YIELD_HDR_H;
      sec.rows.forEach((_, i) => { ws.getRow(row + 1 + i).height = HXL.YIELD_ROW_H; });
      row = row + 1 + sec.rows.length + 2;    // 표 사이 2행 비우고 다음 STEP 제목
    });
    hxlSetColWidths(ws, d.header, { "Item": 36 }, HXL.YIELD_COL_W);
    await hxlDownload(wsx.wb, "yield");
  } catch (e) {
    showToast("Excel 생성 실패: " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── CPK 탭 Excel Down ───────────────────────────────────────────────────────
async function exportCpkExcel() {
  const report = DATA && DATA.web_report;
  const btn = document.getElementById("cpkExcelBtn");
  if (!report) { showToast("CPK 데이터가 없습니다"); return; }
  if (btn) btn.disabled = true;
  try {
    const d = buildCpkSheetData(report);
    if (!d.rows.length) { showToast("내보낼 CPK 행이 없습니다"); return; }
    const wsx = await hxlNewSheet("CPK");
    const ws = wsx.ws;
    hxlWriteTable(ws, d.header, d.rows);
    // cpk < 1.33 행은 흰 데이터 fill 위에 연노랑을 덮어쓴다 (폰트·테두리는 유지).
    d.warnOffsets.forEach(off => {
      const r = HXL.HEADER_ROW + 1 + off;
      hxlStyleCells(ws, r, HXL.START_COL, r, HXL.START_COL + d.header.length - 1,
        { fill: HXL.CPK_WARN_FILL });
    });
    // 여러 계열이 한 항목을 공유하면 TEST NAME/LOW·HIGH SPEC/SCALE 를 세로 병합.
    d.labelRuns.forEach(run => {
      if (run[1] < 2) return;
      const r1 = HXL.HEADER_ROW + 1 + run[0];
      for (let c = 0; c < CPK_LABEL_NCOL; c++) {
        hxlMergeCol(ws, r1, r1 + run[1] - 1, HXL.START_COL + c);
      }
    });
    hxlSetColWidths(ws, d.header, {
      "TEST NAME": 60, "계열": 15, "n": HXL.CPK_N_COL_W, "comment": 30,
    }, 9.5);
    await hxlDownload(wsx.wb, "cpk");
  } catch (e) {
    showToast("Excel 생성 실패: " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Issue Table 탭 Excel Down ───────────────────────────────────────────────
// 전체본 Issue Table 시트와 같은 구성(Map/Distribution 열은 이미지 없이 빈 칸).
async function exportIssueExcel() {
  const report = DATA && DATA.web_report;
  const btn = document.getElementById("issueExcelBtn");
  const issueRows = (DATA && Array.isArray(DATA.issue_table_text))
    ? DATA.issue_table_text : [];
  if (!issueRows.length) { showToast("Issue Table 데이터가 없습니다"); return; }
  if (btn) btn.disabled = true;
  try {
    const d = buildIssueSheetData(issueRows, hxlSourceNames(report));
    if (!d.rows.length) { showToast("내보낼 Issue Table 행이 없습니다"); return; }
    const wsx = await hxlNewSheet("Issue Table");
    const ws = wsx.ws;
    hxlWriteTable(ws, d.header, d.rows);
    // CPK 서브헤더 행은 헤더 서식 — Yield 섹션 헤더와 같은 형태(소스명이 값).
    d.subheads.forEach(off => {
      const r = HXL.HEADER_ROW + 1 + off;
      hxlStyleCells(ws, r, HXL.START_COL, r, HXL.START_COL + d.header.length - 1,
        { fill: HXL.HDR_FILL, font: HXL.HDR_FONT, center: true, border: true });
    });
    hxlFillCellsAt(ws, d.fails, HXL.ISSUE_FAIL_FILL);
    hxlFillCellsAt(ws, d.warns, HXL.CPK_WARN_FILL);
    d.merges.forEach(m => {
      hxlMergeCol(ws, HXL.HEADER_ROW + 1 + m[0], HXL.HEADER_ROW + 1 + m[1], HXL.START_COL);
    });
    const widths = { "Item": 36, "Category": 10, "Status": 10,
      "Map": HXL.ISSUE_MAP_COL_W, "Distribution": HXL.ISSUE_DIST_COL_W };
    HXL_ISSUE_COMMENT_COLS.forEach(c => { widths[c] = 40; });
    hxlSetColWidths(ws, d.header, widths, HXL.YIELD_COL_W);
    await hxlDownload(wsx.wb, "issue_table");
  } catch (e) {
    showToast("Excel 생성 실패: " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

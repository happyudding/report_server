// 탭별 Excel Down (vendored exceljs — trim.js loadExcelJS 재사용, 서버 무변경).
//
// Honey 클라이언트 "Excel Download"(client/excel_download/_sheets.py) 의 Yield/CPK 시트와
// 같은 레이아웃·서식으로 xlsx 를 브라우저에서 만든다. 입력이 같은 /full payload 라
// 값 파리티는 자동으로 맞고, 여기서는 시트 배치(배너·헤더행 위치·행 필터·경고 fill)만
// 맞춘다. 스타일 상수는 report_generator/_xlsx_style.py 의 값을 그대로 옮겨 적었다.
//
// 순수 빌더(buildYieldSheetData/buildCpkSheetData)는 DOM·ExcelJS 를 참조하지 않는다
// — QJSEngine 으로 함수만 추출해 검증하기 위한 관례(spread 문법도 쓰지 않는다).

// ── _xlsx_style.py 상수 미러 ────────────────────────────────────────────────
const HXL = {
  HEADER_ROW: 3,          // 표 헤더 행 (B3 부터 시작)
  START_COL: 2,           // B열
  TITLE_MAX_COL: 26,      // 제목 배너 A1:Z1
  HDR_FILL: "FFD9E1F2",
  DATA_FILL: "FFFFFFFF",
  TITLE_FILL: "FFBDD7EE",
  CPK_WARN_FILL: "FFFFF3B0",
  CPK_THRESHOLD: 1.33,
  HDR_FONT: { name: "Calibri", size: 11 },
  DATA_FONT: { name: "Calibri", size: 10 },
  TITLE_FONT: { name: "Calibri", size: 20 },
  LINK_FONT: { name: "Calibri", bold: true, size: 12, color: { argb: "FF0563C1" } },
  YIELD_HDR_H: 40,
  YIELD_ROW_H: 22,
  YIELD_COL_W: 6.5 * 1.6,   // _NARROW_COL_WIDTH * 1.6
  CPK_N_COL_W: 6.5 * 1.05,
};

const CPK_SHEET_HEADER = ["TEST NAME", "LOW SPEC", "HIGH SPEC", "SCALE", "계열", "n",
  "min", "median", "max", "average", "stdev",
  "cpl", "cpu", "cp", "cpk", "comment"];

// ── 순수 빌더 (DOM/ExcelJS 무의존) ──────────────────────────────────────────
function hxlSourceNames(report) {
  const srcs = (report && report.sources) || [];
  return srcs.map(s => (s && s.name) || "");
}

// _sheets.py yield_header / _yield_row_values / write_yield_sheet 파리티.
// Pass 행(bin=="1") 1개 + Bin 그룹 대표행 — 웹 Yield 탭의 접힌 상태와 같다.
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

  const sheets = (report && report.sheets) || {};
  const yieldRows = sheets["Yield"] || [];
  const rows = [];
  if (yieldRows.length && String((yieldRows[0] || {}).bin) === "1") {
    rows.push(rowValues(yieldRows[0]));
  }
  ((report && report.yield_bin_groups) || []).forEach(g => rows.push(rowValues((g || {}).rep)));
  return { header: header, rows: rows };
}

// _sheets.py write_cpk_sheet 파리티 — sheets["CPK"] 전량·원순서(화면 필터 무관),
// 전체(all-die) 기준 컬럼만(*_bin1 / *_limited 무시).
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
  blankRepeatedCpkLabels(rows);
  return { header: CPK_SHEET_HEADER, rows: rows, warnOffsets: warnOffsets };
}

// 같은 subject 연속 행의 TEST NAME/SPEC/SCALE 반복 생략 (_blank_repeated_labels).
function blankRepeatedCpkLabels(rows) {
  let prevKey = null;
  rows.forEach(row => {
    const key = JSON.stringify(row.slice(0, 4));
    if (key === prevKey) {
      row[0] = ""; row[1] = ""; row[2] = ""; row[3] = "";
    } else {
      prevKey = key;
    }
  });
}

// ── ExcelJS 기입 헬퍼 ───────────────────────────────────────────────────────
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

function hxlTitleBanner(ws, text) {
  ws.getCell(1, 1).value = text;
  hxlStyleCells(ws, 1, 1, 1, HXL.TITLE_MAX_COL, { fill: HXL.TITLE_FILL, font: HXL.TITLE_FONT });
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

// 헤더+데이터를 B3 부터 기입 (_write_table). 반환: 마지막 데이터 행 번호.
function hxlWriteTable(ws, header, rows) {
  const hr = HXL.HEADER_ROW;
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

function hxlSetColWidths(ws, header, widths, defWidth) {
  header.forEach((name, i) => {
    const w = Object.prototype.hasOwnProperty.call(widths, name) ? widths[name] : defWidth;
    if (w !== undefined && w !== null) ws.getColumn(HXL.START_COL + i).width = w;
  });
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

// ── Yield 탭 Excel Down ─────────────────────────────────────────────────────
async function exportYieldExcel() {
  const report = DATA && DATA.web_report;
  const btn = document.getElementById("yieldExcelBtn");
  if (!report) { showToast("Yield 데이터가 없습니다"); return; }
  if (btn) btn.disabled = true;
  try {
    const d = buildYieldSheetData(report);
    if (!d.rows.length) { showToast("내보낼 Yield 행이 없습니다"); return; }
    const ExcelJS = await loadExcelJS();
    const wb = new ExcelJS.Workbook();
    const ws = wb.addWorksheet("Yield");
    hxlTitleBanner(ws, "Yield");
    hxlSessionLink(ws);
    hxlWriteTable(ws, d.header, d.rows);
    hxlSetColWidths(ws, d.header, { "Item": 36 }, HXL.YIELD_COL_W);
    ws.getRow(HXL.HEADER_ROW).height = HXL.YIELD_HDR_H;
    d.rows.forEach((_, i) => { ws.getRow(HXL.HEADER_ROW + 1 + i).height = HXL.YIELD_ROW_H; });
    await hxlDownload(wb, "yield");
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
    const ExcelJS = await loadExcelJS();
    const wb = new ExcelJS.Workbook();
    const ws = wb.addWorksheet("CPK");
    hxlTitleBanner(ws, "CPK");
    hxlSessionLink(ws);
    hxlWriteTable(ws, d.header, d.rows);
    // cpk < 1.33 행은 흰 데이터 fill 위에 연노랑을 덮어쓴다 (폰트·테두리는 유지).
    d.warnOffsets.forEach(off => {
      const r = HXL.HEADER_ROW + 1 + off;
      hxlStyleCells(ws, r, HXL.START_COL, r, HXL.START_COL + d.header.length - 1,
        { fill: HXL.CPK_WARN_FILL });
    });
    hxlSetColWidths(ws, d.header, {
      "TEST NAME": 60, "계열": 15, "n": HXL.CPK_N_COL_W, "comment": 30,
    }, 9.5);
    await hxlDownload(wb, "cpk");
  } catch (e) {
    showToast("Excel 생성 실패: " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

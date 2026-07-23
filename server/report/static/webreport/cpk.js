// ── CPK ──────────────────────────────────────────────────────────────────
const CPK_COLUMNS = ["subject", "lower_limit", "upper_limit", "units", "source",
  "n", "min", "median", "max", "average", "stdev", "cpl", "cpu", "cp", "cpk"];
const CPK_NUMERIC = new Set(["lower_limit", "upper_limit", "n", "min", "median", "max",
  "average", "stdev", "cpl", "cpu", "cp", "cpk"]);
const CPK_WARN_THRESHOLD = 1.33;   // 기본 임계값 (item_detail/Issue Table 하이라이트는 이 고정값 사용)
const CPK_PAGE_SIZE = 100;    // 페이지당 표시 행 수

// 기준(basis)은 **Bin1(양품, BIN==1) 하나로 통일**돼 있다 (2026-07-23, UX 간편화).
// 종전 3상 토글(전체/Bin1/Limit 안)은 제거했다 — 서버 cpk_rows 의 base 필드가 곧 Bin1
// 통계이며(web_report/tabs/cpk.py), Issue Table·Distribution·Excel 도 같은 값을 쓴다.
// 버튼 라벨은 전부 "현재 적용 중인 값"만 쓴다(누르면 바뀔 값이 아님) — 툴바 왼쪽 라벨이
// 무슨 구분인지 설명한다.
let cpkShowLowOnly = true;    // 기본값: 임계 미만 항목만 항목명 순으로 정렬해 보여줌
// CPK 임계값 — 사용자가 툴바에서 직접 입력한다(기본 1.33). 필터·셀 강조·빈 메시지가 이 값을 쓴다.
// cpkLowInputRaw 는 입력 중 원문(빈 문자열·"1." 같은 중간 상태 허용), cpkLowThreshold 는
// 마지막으로 유효했던 숫자 — 입력이 잠깐 비어도 필터가 튀지 않게 분리해 둔다.
let cpkLowThreshold = CPK_WARN_THRESHOLD;
let cpkLowInputRaw = String(CPK_WARN_THRESHOLD);
// "동일Limit" 3상: "exclude"=제외(기본)·"all"=전체·"only"=그 항목만.
// 판정 기준은 cpkIsAbnormal 참조(상·하한 동일 또는 CPK 계산 불가) — 화면 라벨만 "동일Limit".
let cpkAbnormalMode = "exclude";
let cpkHideCodeUnit = false;  // 켜면 Unit(단위)이 CODE 인 항목(디지털 code 값) 숨김
let cpkSearchTerm = "";       // subject/source 검색어 (실시간 필터)
let cpkSourceFilter = "";     // 특정 source 만 표시 (빈 문자열 = 전체 source)
let cpkPage = 1;              // 현재 페이지 (1-base)
let cpkPanelBound = false;    // panel-cpk 페이저 클릭 위임 1회 바인딩 플래그
let cpkTargetMode = false;    // "Limit 계산" 토글 — 켜지면 체크박스 열 + 목표 Cpk 역산 컨트롤 노출
let cpkSelected = new Set();  // 선택된 행 키 (`${subject}||${source}`)
let cpkTargetVal = 1.33;      // 목표 Cpk 입력값
let cpkMarginPct = 0;         // 역산 Margin(%) — 0/1/5/10. 각 한계값을 (1-margin) 으로 나눠 확대
let cpkTargetResults = new Map(); // 역산 결과: 행 키 → {lo, hi}

function cpkRowKey(r) { return `${r.subject}||${r.source}`; }

// 동일Limit 3상 토글: 버튼 라벨 + 클릭 시 순환 순서(제외(기본) → 전체 → 그 항목만 → …).
const CPK_ABN_LABELS = { exclude: "동일Limit 제외", all: "ALL", only: "동일Limit only" };
const CPK_ABN_ORDER = ["exclude", "all", "only"];

function cpkFmt(x) { return Number(x.toFixed(4)); }

// CPK 필터 버튼 라벨 — 항상 "현재 적용 중인 값"(ALL 또는 임계값)을 보여준다.
function cpkLowBtnLabel() { return cpkShowLowOnly ? `CPK &lt; ${cpkLowThreshold}` : "ALL"; }

// 목표 Cpk → 시그마 수준 (σ = 3 × Cpk. 예: 1.33→4.0σ, 2→6.0σ, 4→12.0σ).
function cpkSigmaText(v) {
  const c = parseFloat(v);
  return (c > 0) ? `= ${(3 * c).toFixed(1)}σ` : "";
}

// 목표 Cpk 로부터 선택 행의 규격 한계 역산 (평균 중심 대칭: avg ± 3·Cpk·stdev).
// 결과는 누적된다 — 이전 역산값을 지우지 않고 현재 선택 행만 덮어쓴다. 항목마다 다른
// Margin 으로 나눠 역산하려면 체크 → 역산 → 체크해제 → 다른 항목 체크 → 역산 을 반복한다.
// (전체 초기화는 "역산값 지우기" 버튼 / Limit 계산 모드 해제)
// 단 **복사는 누적분 전체가 아니라 지금 체크된 행만** 대상이다 — 누적 역산 후 원하는 행을
// 다시 체크하고 "역산값 복사"를 누르면 그 행들만 나온다.
function cpkComputeTargets() {
  const cpk = parseFloat(cpkTargetVal);
  if (!(cpk > 0)) return;
  const sheets = webReportSheets();
  const byKey = new Map((sheets ? (sheets["CPK"] || []) : []).map(r => [cpkRowKey(r), r]));
  for (const key of cpkSelected) {
    const row = byKey.get(key);
    if (!row) continue;
    // average/stdev 는 Bin1 기준 단일 값(서버 통일) — 표에 보이는 값 그대로 역산한다.
    const avg = parseFloat(row.average), sd = parseFloat(row.stdev);
    if (isNaN(avg) || isNaN(sd) || sd <= 0) continue;   // 계산 불가 → 빈칸
    const d = 3 * cpk * sd;
    let lo = avg - d, hi = avg + d;
    // Margin: 최종 규격이 (역산값)/(1-margin) 가 되도록 각 한계값을 확대.
    // 예) 역산 -0.95~0.95 에 5% → -1~1 (0.95/0.95 = 1.0).
    const m = (parseFloat(cpkMarginPct) || 0) / 100;
    if (m > 0 && m < 1) { lo = lo / (1 - m); hi = hi / (1 - m); }
    cpkTargetResults.set(key, { lo: cpkFmt(lo), hi: cpkFmt(hi) });
  }
}

function updateCpkSelInfo() {
  const el = document.getElementById("cpkSelInfo");
  if (!el) return;
  // 역산값은 체크해제·필터·페이지와 무관하게 누적으로 남지만 복사 대상은 "체크된 행 ∩
  // 역산값 있는 행" 뿐이므로, 실제 복사될 개수를 따로 보여준다.
  let copyable = 0;
  for (const k of cpkSelected) if (cpkTargetResults.has(k)) copyable++;
  el.textContent = `선택 ${cpkSelected.size}개 · 역산값 ${cpkTargetResults.size}개 · 복사 대상 ${copyable}개`;
}

// "동일Limit" 판정: ① Limit(하한==상한) 동일 ② CPK 값 없음 → 대상.
// cpk 는 Bin1 기준 단일 값이라 별도 기준 정규화 없이 행을 그대로 본다.
function cpkIsAbnormal(r) {
  const lo = parseFloat(r.lower_limit), hi = parseFloat(r.upper_limit);
  if (!isNaN(lo) && !isNaN(hi) && lo === hi) return true;   // Limit 동일
  const cpk = parseFloat(r.cpk);
  if (isNaN(cpk)) return true;   // CPK 값 없음
  return false;
}

function cpkFilterRows(rows) {
  if (cpkHideCodeUnit)
    rows = rows.filter(r => String(r.units || "").trim().toUpperCase() !== "CODE");
  if (cpkSourceFilter) rows = rows.filter(r => String(r.source || "") === cpkSourceFilter);
  const term = cpkSearchTerm.trim().toLowerCase();
  if (!term) return rows;
  return rows.filter(r => String(r.subject || "").toLowerCase().includes(term)
    || String(r.source || "").toLowerCase().includes(term));
}

// CPK 행에서 등장 순서대로 중복 제거한 source 목록 (드롭다운 옵션용).
function cpkSourceList(rows) {
  return [...new Set((rows || []).map(r => String(r.source || "")).filter(Boolean))];
}

// 표시용 행 목록 생성. 각 행에 _key(원본 subject/source 기준 안정 키)를 붙여 접힌 행도
// 개별 선택이 가능하게 한다. (select-all 계산과 cpkTableHtml 이 공용으로 사용)
function cpkBodyRows(rows) {
  if (cpkAbnormalMode === "exclude") rows = rows.filter(r => !cpkIsAbnormal(r));     // 동일Limit 제외
  else if (cpkAbnormalMode === "only") rows = rows.filter(r => cpkIsAbnormal(r));    // 동일Limit 만 (all=필터 없음)
  if (cpkShowLowOnly) {
    // cpk 값 내림/오름차순이 아니라 같은 Item name 끼리 묶여 보이도록 항목명(subject) 순으로 정렬.
    return rows
      .filter(r => { const v = parseFloat(r.cpk); return !isNaN(v) && v < cpkLowThreshold; })
      .slice()
      .sort((a, b) => String(a.subject).localeCompare(String(b.subject)))
      .map(r => ({ ...r, _key: cpkRowKey(r) }));
  }
  // subject 가 연속으로 반복되면(같은 item, source 별 행) 2번째 행부터 subject/limit/units 를 비움
  let prevSubject = null;
  return rows.map(r => {
    const row = { ...r, _key: cpkRowKey(r) };
    if (r.subject === prevSubject) {
      row.subject = ""; row.lower_limit = ""; row.upper_limit = ""; row.units = "";
    } else {
      prevSubject = r.subject;
    }
    return row;
  });
}

// 필터+본문행 생성은 전 행(항목×소스)을 매번 다시 매핑·필터·정렬한다. 대형 세션에선
// 수천 행이라 검색 키입력·페이지 이동마다 돌면 눈에 띄게 뻑뻑해진다. 표시 결과를 정하는
// 상태(아래 sig)가 그대로면 지난 결과를 그대로 쓴다 — 페이지 이동은 sig 가 안 변하므로
// 재정렬이 0회가 된다. 원본 rows 는 identity 로 비교한다(payload 교체 시 자동 무효화).
let _cpkRowsMemo = null;
function cpkDisplayRows(rows) {
  const sig = JSON.stringify([cpkAbnormalMode, cpkShowLowOnly,
    cpkLowThreshold, cpkHideCodeUnit, cpkSearchTerm, cpkSourceFilter]);
  if (_cpkRowsMemo && _cpkRowsMemo.rows === rows && _cpkRowsMemo.sig === sig) {
    return _cpkRowsMemo.out;
  }
  const out = cpkBodyRows(cpkFilterRows(rows));
  _cpkRowsMemo = { rows, sig, out };
  return out;
}

function cpkTableHtml(rows) {
  const bodyRows = cpkDisplayRows(rows);

  if (!bodyRows.length) {
    const msg = cpkSearchTerm.trim() ? "검색 결과 없음"
      : `CPK &lt; ${cpkLowThreshold} 항목 없음`;
    return `<div class="placeholder">${msg}</div>`;
  }

  // 100개씩 페이지네이션 (subject/source 컬럼 좌측 고정, cpk 컬럼 우측 고정, 헤더 상단 고정).
  const totalPages = Math.max(1, Math.ceil(bodyRows.length / CPK_PAGE_SIZE));
  if (cpkPage > totalPages) cpkPage = totalPages;
  if (cpkPage < 1) cpkPage = 1;
  const start = (cpkPage - 1) * CPK_PAGE_SIZE;
  const pageRows = bodyRows.slice(start, start + CPK_PAGE_SIZE);

  // Limit 계산 모드면 표시 컬럼에 target 열 2개를 upper_limit 뒤에 삽입, 체크박스 열은 별도 선두.
  const displayCols = CPK_COLUMNS.slice();
  if (cpkTargetMode) {
    const i = displayCols.indexOf("upper_limit");
    displayCols.splice(i + 1, 0, "target_lolimit", "target_hilimit");
  }
  const allSelected = cpkTargetMode && bodyRows.every(r => cpkSelected.has(r._key));
  const selTh = cpkTargetMode
    ? `<th class="cpk-sel-col"><input type="checkbox" id="cpkSelAll"${allSelected ? " checked" : ""}></th>` : "";
  const head = "<thead><tr>" + selTh + displayCols.map(c => `<th>${esc(c)}</th>`).join("") + "</tr></thead>";
  const body = "<tbody>" + pageRows.map(row => {
    const cpkVal = parseFloat(row.cpk);
    const isWarn = !isNaN(cpkVal) && cpkVal < cpkLowThreshold;
    const res = cpkTargetResults.get(row._key);
    const selTd = cpkTargetMode
      ? `<td class="cpk-sel-col"><input type="checkbox" class="cpk-row-chk" data-key="${esc(row._key)}"${cpkSelected.has(row._key) ? " checked" : ""}></td>` : "";
    const tds = displayCols.map(c => {
      if (c === "target_lolimit" || c === "target_hilimit") {
        const tv = res ? (c === "target_lolimit" ? res.lo : res.hi) : "";
        const cls = tv === "" ? "st-empty" : "st-num cpk-target";
        return `<td class="${cls}">${esc(String(tv))}</td>`;
      }
      const v = row[c];
      const txt = (v === null || v === undefined) ? "" : String(v);
      const cls = [];
      if (txt === "") cls.push("st-empty");
      else if (CPK_NUMERIC.has(c)) cls.push("st-num");
      if (c === "cpk" && isWarn) cls.push("cpk-warn");
      // subject 셀(비어있지 않을 때) → Item_detail 링크
      const inner = (c === "subject" && txt !== "")
        ? `<span class="item-detail-link" data-subject="${esc(txt)}">${esc(txt)}</span>` : esc(txt);
      return `<td${cls.length ? ` class="${cls.join(" ")}"` : ""}>${inner}</td>`;
    }).join("");
    return `<tr>${selTd}${tds}</tr>`;
  }).join("") + "</tbody>";

  const table = `<div class="sheet-wrap cpk-sheet"><table class="sheet-table cpk-sheet${cpkTargetMode ? " has-select" : ""}">${head}${body}</table></div>`;
  const end = Math.min(start + CPK_PAGE_SIZE, bodyRows.length);
  const pager = `<div class="cpk-pager">` +
    `<button type="button" class="btn-sm" data-cpk-page="${cpkPage - 1}"${cpkPage <= 1 ? " disabled" : ""}>‹ 이전</button>` +
    `<span class="cpk-pager-info">${start + 1}–${end} / ${bodyRows.length} (page ${cpkPage}/${totalPages})</span>` +
    `<button type="button" class="btn-sm" data-cpk-page="${cpkPage + 1}"${cpkPage >= totalPages ? " disabled" : ""}>다음 ›</button>` +
    `</div>`;
  return table + pager;
}

// 검색어/토글 상태만 바뀌었을 때 테이블만 다시 그림 (검색창 포커스 유지).
function renderCpkTable() {
  const sheets = webReportSheets();
  const rows = sheets ? (sheets["CPK"] || []) : [];
  const host = document.getElementById("cpkTableHost");
  // 필터는 cpkDisplayRows(메모) 안에서 적용된다 — 여기서 미리 걸면 매 호출 새 배열이 나와
  // 메모가 항상 미스가 된다.
  if (host) host.innerHTML = cpkTableHtml(rows);
}

// 검색·임계값 입력은 키입력마다 수천 행 재필터·재정렬을 유발한다. 입력이 멈춘 뒤 한 번만
// 그린다 (표만 다시 그리므로 입력 포커스·캐럿은 그대로 유지된다).
const CPK_INPUT_DEBOUNCE_MS = 150;
let _cpkTableTimer = null;
function renderCpkTableDebounced() {
  if (_cpkTableTimer) clearTimeout(_cpkTableTimer);
  _cpkTableTimer = setTimeout(() => { _cpkTableTimer = null; renderCpkTable(); },
                              CPK_INPUT_DEBOUNCE_MS);
}

function renderCpk() {
  const panel = document.getElementById("panel-cpk");
  const sheets = webReportSheets();
  const rows = sheets ? (sheets["CPK"] || []) : [];
  if (!rows.length) { emptyPanel(panel, "CPK 데이터 없음"); return; }

  const targetBar = cpkTargetMode
    ? `<div class="cpk-target-bar">목표 Cpk ` +
      `<input type="number" id="cpkTargetInput" min="0" step="0.01" value="${esc(String(cpkTargetVal))}">` +
      `<span id="cpkSigmaVal" class="cpk-pager-info" title="시그마 수준 = 3 × Cpk (1.33=4σ, 2=6σ, 4=12σ)">${cpkSigmaText(cpkTargetVal)}</span>` +
      `<span class="cpk-margin-wrap">Margin ` +
      `<select id="cpkMarginSel">` +
      [0, 1, 5, 10].map(v =>
        `<option value="${v}"${Number(cpkMarginPct) === v ? " selected" : ""}>${v === 0 ? "없음" : v + "%"}</option>`).join("") +
      `</select></span>` +
      `<button type="button" id="cpkCalcBtn" class="btn-sm">역산</button>` +
      `<button type="button" id="cpkCopyBtn" class="btn-sm" title="지금 체크된 행의 역산값만 TSV 로 복사 (체크 해제된 행의 누적 역산값은 복사되지 않음)">역산값 복사</button>` +
      `<button type="button" id="cpkClearSelBtn" class="btn-sm">선택 해제</button>` +
      `<button type="button" id="cpkClearResBtn" class="btn-sm" title="누적된 역산값을 모두 지운다 (체크해제·선택 해제로는 지워지지 않음)">역산값 지우기</button>` +
      `<span id="cpkSelInfo" class="cpk-pager-info"></span></div>`
    : "";
  const sourceOpts = `<option value="">전체 source</option>` +
    cpkSourceList(rows).map(s =>
      `<option value="${esc(s)}"${cpkSourceFilter === s ? " selected" : ""}>${esc(s)}</option>`).join("");
  panel.innerHTML =
    `<div class="cpk-toolbar">` +
    `<input type="text" id="cpkSearchInput" data-no-dirty placeholder="항목/source 검색" value="${esc(cpkSearchTerm)}">` +
    `<select id="cpkSourceSel" title="특정 source 만 표시">${sourceOpts}</select>` +
    `<span class="cpk-tool-group"><span class="cpk-tool-label">CPK 구분</span>` +
    `<input type="number" id="cpkLowInput" min="0" step="0.01" value="${esc(cpkLowInputRaw)}" title="CPK 임계값 — 이 값 미만인 항목만 추려 보거나(버튼) 표에서 노랗게 강조한다.">` +
    `<button type="button" id="cpkLowBtn" class="btn-sm${cpkShowLowOnly ? " active" : ""}" title="현재 적용 중인 CPK 필터(클릭하면 전환): 'ALL'=전체 항목 · 'CPK &lt; 임계값'=임계 미만 항목만 항목명 순 정렬">` +
    `${cpkLowBtnLabel()}</button></span>` +
    `<span class="cpk-tool-group"><span class="cpk-tool-label">동일Limit 구분</span>` +
    `<button type="button" id="cpkAbnBtn" class="btn-sm${cpkAbnormalMode !== "all" ? " active" : ""}" title="현재 적용 중인 동일Limit 필터(클릭하면 순환): '동일Limit 제외'(기본)=해당 항목을 뺀 나머지 → 'ALL'=전체 표시 → '동일Limit only'=해당 항목만. 판정 기준: ① 상·하한(Limit)이 같아 공차가 0인 항목, ② CPK 계산 불가(값 없음).">${CPK_ABN_LABELS[cpkAbnormalMode]}</button></span>` +
    `<button type="button" id="cpkCodeUnitBtn" class="btn-sm${cpkHideCodeUnit ? " active" : ""}" title="켜짐: 단위(Unit)가 CODE 인 항목(디지털 code 값, 공정능력 지표가 무의미) 숨김 · 꺼짐: 전체 표시">Unit CODE 제거</button>` +
    `<button type="button" id="cpkTargetBtn" class="btn-sm${cpkTargetMode ? " active" : ""}">Limit 계산</button>` +
    `<button type="button" class="btn-sm tab-excel-btn" id="cpkExcelBtn" title="Honey Excel Download 의 CPK 시트와 동일한 xlsx 다운로드 (Bin1 기준·화면 필터 무관)">Excel Down</button></div>` +
    targetBar +
    `<div id="cpkTableHost"></div>`;
  renderCpkTable();
  updateCpkSelInfo();
  document.getElementById("cpkLowBtn").addEventListener("click", () => {
    cpkShowLowOnly = !cpkShowLowOnly;
    cpkPage = 1;
    renderCpk();
  });
  // 임계값 입력: 표만 다시 그려 입력 포커스·캐럿을 유지한다(renderCpk 는 입력창을 날림).
  document.getElementById("cpkLowInput").addEventListener("input", (e) => {
    cpkLowInputRaw = e.target.value;
    const v = parseFloat(cpkLowInputRaw);
    if (!isFinite(v)) return;   // 빈칸·중간 입력 상태 — 마지막 유효값 유지
    cpkLowThreshold = v;
    cpkPage = 1;
    const btn = document.getElementById("cpkLowBtn");
    if (btn) btn.innerHTML = cpkLowBtnLabel();
    renderCpkTableDebounced();
  });
  document.getElementById("cpkAbnBtn").addEventListener("click", () => {
    cpkAbnormalMode = CPK_ABN_ORDER[(CPK_ABN_ORDER.indexOf(cpkAbnormalMode) + 1) % CPK_ABN_ORDER.length];
    cpkPage = 1;
    renderCpk();
  });
  document.getElementById("cpkCodeUnitBtn").addEventListener("click", () => {
    cpkHideCodeUnit = !cpkHideCodeUnit;
    cpkPage = 1;
    renderCpk();
  });
  document.getElementById("cpkSearchInput").addEventListener("input", (e) => {
    cpkSearchTerm = e.target.value;
    cpkPage = 1;
    renderCpkTableDebounced();
  });
  document.getElementById("cpkSourceSel").addEventListener("change", (e) => {
    cpkSourceFilter = e.target.value;
    cpkPage = 1;
    renderCpkTable();
  });
  document.getElementById("cpkTargetBtn").addEventListener("click", () => {
    cpkTargetMode = !cpkTargetMode;
    if (!cpkTargetMode) { cpkSelected.clear(); cpkTargetResults.clear(); }
    renderCpk();
  });
  if (cpkTargetMode) {
    document.getElementById("cpkTargetInput").addEventListener("input", (e) => {
      cpkTargetVal = e.target.value;
      const sig = document.getElementById("cpkSigmaVal");
      if (sig) sig.textContent = cpkSigmaText(cpkTargetVal);   // 시그마 수준 실시간 갱신
    });
    document.getElementById("cpkMarginSel").addEventListener("change", (e) => {
      cpkMarginPct = parseFloat(e.target.value) || 0;
      // Margin 변경 즉시 재계산 — 단 대상은 "지금 체크된 행"뿐이다. 체크 해제한 행의
      // 역산값은 이전 Margin 으로 계산된 채 그대로 남는다(항목별 Margin 혼용).
      if (cpkTargetResults.size) { cpkComputeTargets(); renderCpkTable(); }
    });
    document.getElementById("cpkCalcBtn").addEventListener("click", () => {
      cpkComputeTargets();
      renderCpkTable();
    });
    document.getElementById("cpkCopyBtn").addEventListener("click", (e) => {
      // **지금 체크된 행**의 역산값만 헤더 포함 4열 TSV 로 복사 → Excel 표 붙여넣기.
      // 역산값(cpkTargetResults)은 항목별 Margin 혼용을 위해 체크 해제 후에도 누적 보존되지만,
      // 복사 대상은 그 누적분 전체가 아니라 "체크된 행 ∩ 역산값 있는 행"이다.
      // 원본 CPK 행 순서로 훑어 화면 필터·페이지와 무관하게 선택분을 빠짐없이 담는다.
      const sheets = webReportSheets();
      const lines = [["subject", "target_lolimit", "target_hilimit", "units"].join("\t")];
      for (const r of (sheets ? (sheets["CPK"] || []) : [])) {
        const key = cpkRowKey(r);
        if (!cpkSelected.has(key)) continue;
        const res = cpkTargetResults.get(key);
        if (!res) continue;   // 체크만 하고 역산 안 한 행은 제외
        const u = r.units;
        lines.push(`${r.subject}\t${res.lo}\t${res.hi}\t${(u === null || u === undefined) ? "" : u}`);
      }
      const btn = e.currentTarget;
      const flash = (msg) => { const t = btn.textContent; btn.textContent = msg; setTimeout(() => { btn.textContent = t; }, 1200); };
      if (lines.length <= 1) { flash(cpkSelected.size ? "역산값 없음" : "선택 없음"); return; }
      // HTTP LAN 환경(secure context 아님) 대비 execCommand 폴백이 있는 공용 헬퍼 사용.
      cellSelCopyText(lines.join("\n")).then(ok => flash(ok ? "복사됨" : "복사 실패"));
    });
    document.getElementById("cpkClearSelBtn").addEventListener("click", () => {
      cpkSelected.clear();   // 역산값은 보존 — 지우려면 "역산값 지우기"
      updateCpkSelInfo();
      renderCpkTable();
    });
    document.getElementById("cpkClearResBtn").addEventListener("click", () => {
      cpkTargetResults.clear();
      updateCpkSelInfo();
      renderCpkTable();
    });
  }
  // panel-cpk 는 재렌더돼도 요소 자체는 유지되므로 페이저 클릭·체크박스 위임은 1회만 바인딩한다.
  if (!cpkPanelBound) {
    panel.addEventListener("click", (e) => {
      const pb = e.target.closest("[data-cpk-page]");
      if (!pb || pb.disabled) return;
      cpkPage = parseInt(pb.dataset.cpkPage, 10) || 1;
      renderCpkTable();
    });
    panel.addEventListener("change", (e) => {
      const t = e.target;
      if (t.id === "cpkSelAll") {
        const sheets = webReportSheets();
        const keys = cpkDisplayRows(sheets ? (sheets["CPK"] || []) : []).map(r => r._key);
        // 체크해제해도 역산값(cpkTargetResults)은 지우지 않는다 — 항목별로 다른 Margin 으로
        // 나눠 역산할 수 있게 결과를 누적 보존한다.
        if (t.checked) keys.forEach(k => cpkSelected.add(k));
        else keys.forEach(k => cpkSelected.delete(k));
        updateCpkSelInfo();
        renderCpkTable();
      } else if (t.classList.contains("cpk-row-chk")) {
        const k = t.dataset.key;
        if (t.checked) cpkSelected.add(k);
        else cpkSelected.delete(k);
        updateCpkSelInfo();
        renderCpkTable();
      }
    });
    cpkPanelBound = true;
  }
}


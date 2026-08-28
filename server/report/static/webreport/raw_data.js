// ── Raw Data (web_report lazy-load) ─────────────────────────────────────────
const RAW_DATA_COLUMN_CAP = 60;
let rawDataMeta = null;              // {items, sources, total_dies} — 세션당 1회만 fetch
let rawDataGrid = null;              // 조회 결과 Tabulator 인스턴스
let rawDataSelected = new Set();     // 선택된 item 컬럼명
// 편집 대기열: key `${source}${row_idx}${column}` → {source,row_idx,column,value}
// (같은 셀을 여러 번 고치면 마지막 값만 유지). 저장 성공 또는 수정모드 종료 시 비운다.
let rawDataPendingEdits = new Map();
let rawDataFailOnly = false;         // true 면 BIN != Pass(1) 인 fail die 만 그리드에 표시
let rawDataSource = "";              // 선택된 source (항상 1개 선택 — "전체" 옵션 없음)
let _rawHscrollSyncing = false;      // 프록시 ↔ 실제 tableholder 상호 동기화 시 피드백 루프 방지 가드

function destroyRawDataGrid() {
  if (rawDataGrid) { try { rawDataGrid.destroy(); } catch (e) {} rawDataGrid = null; }
}

// ── 값 검증 (서버 web_report/rawvalues.py 와 동기 필수) ──────────────────────────
// 규칙 테이블·문안은 서버가 단일 진실이고 rawDataMeta.value_rules 로 내려온다. 여기 복제하는
// 것은 '판정 프리미티브'뿐 — rawParseNumber/rawParseInt 가 서버 _parse_number/_parse_int 와
// 달라지면 사용자가 통과시킨 값이 400 으로 튕기므로 고칠 땐 **양쪽을 같이** 고칠 것.
// value_rules 가 없는 구 페이지에서는 사전 검증을 건너뛰고 서버 400 에 맡긴다(안전한 폴백).
const RAW_META_FIELDS = new Set(["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO"]);

function rawRules() { return (rawDataMeta && rawDataMeta.value_rules) || null; }

// 서버 rawvalues._NUM_RE 와 **문자 그대로 동일**해야 한다 — Number() 는 '0x10'/'0b101' 을,
// 파이썬 float() 은 '1_000'/전각숫자/'infinity' 를 받아들여 양쪽 판정이 갈리기 때문이다
// (갈리면 여기서 통과한 값이 서버 400 으로 튕긴다).
const RAW_NUM_RE = /^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$/;

function rawParseNumber(text) {
  const s = String(text).trim();
  if (!RAW_NUM_RE.test(s)) return null;    // '' / 'nan' / '0x10' / '1_000' 전부 여기서 탈락
  const f = Number(s);
  if (!Number.isFinite(f)) return null;    // '1e999' → Infinity
  return f;
}

function rawParseInt(text) {
  const f = rawParseNumber(text);
  if (f === null || !Number.isInteger(f) || Math.abs(f) >= Math.pow(2, 53)) return null;
  return f;
}

// 반환: 위반 사유 문자열 | null
function checkRawCell(column, value, isItem) {
  const rules = rawRules();
  if (!rules) return null;
  const msg = rules.messages || {};
  const text = (value === null || value === undefined) ? "" : String(value);
  if (text.indexOf("\n") >= 0 || text.indexOf("\r") >= 0) return msg.newline || "줄바꿈 문자는 넣을 수 없습니다.";
  const s = text.trim();
  const show = s === "" ? "(빈값)" : (s.length <= 40 ? s : s.slice(0, 40) + "…");
  if (isItem) {
    if (s === "") return null;             // 결측 허용
    return rawParseNumber(s) === null ? String(msg.number || "").replace("{value}", show) : null;
  }
  const kind = (rules.meta_kind || {})[column];
  if (!kind) return null;
  if (s === "") return (rules.required_meta || []).indexOf(column) >= 0 ? (msg.required || "비울 수 없습니다.") : null;
  if (kind === "int") return rawParseInt(s) === null ? String(msg.int || "").replace("{value}", show) : null;
  if (s.length > (rules.max_text_len || 200)) return msg.too_long || "값이 너무 깁니다.";
  return null;
}

// 저장될 정규형 (서버 normalize_cell_value 와 동기) — diff 모달이 실제 저장값을 보여주도록.
function normalizeRawCell(column, value, isItem) {
  const s = ((value === null || value === undefined) ? "" : String(value)).trim();
  if (isItem || s === "") return s;
  if (((rawRules() || {}).meta_kind || {})[column] === "int") {
    const n = rawParseInt(s);
    if (n !== null) return String(n);
  }
  return s;
}

// 차단하지는 않지만 결과가 달라진다고 알려야 하는 변경 (diff 모달 '확인' 컬럼)
function rawCellWarning(column, oldValue, newValue, isItem) {
  const w = (rawRules() || {}).warnings || {};
  const s = ((newValue === null || newValue === undefined) ? "" : String(newValue)).trim();
  if (isItem) return s === "" ? (w.item_blank || null) : null;
  if (column === "BIN") {
    const before = ((oldValue === null || oldValue === undefined) ? "" : String(oldValue)).trim();
    const wasPass = rawParseInt(before) === 1, isPass = rawParseInt(s) === 1;
    return wasPass !== isPass ? (w.bin_change || null) : null;
  }
  if ((column === "XPOS" || column === "YPOS") && s === "") return w.coord_blank || null;
  if (column === "SERIAL") return w.serial_change || null;
  return null;
}

async function fetchRawDataMeta() {
  const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/raw_data/columns`, { cache: "no-store" });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
  return j;
}

function rawDataControlsHtml(meta) {
  const sourceBtns = (meta.sources || []).map(s => {
    const active = rawDataSource === s ? " active" : "";
    return `<button type="button" class="btn-sm rawdata-source-btn${active}" data-source="${esc(s)}">` +
      `${esc(s)}</button>`;
  }).join("");
  return `
    <div class="rawdata-filters">
      <div class="rawdata-filters-row">
        <input type="text" id="rawSearch" placeholder="SERIAL/DUT 검색">
        <input type="text" id="rawBin" placeholder="BIN">
        <button type="button" id="rawQueryBtn" class="btn-sm">조회</button>
        <button type="button" id="rawFailOnlyBtn" class="btn-sm${rawDataFailOnly ? " active" : ""}">FailItem만 보기</button>
        <span id="rawSelCount" class="rawdata-selcount"></span>
      </div>
      <div class="rawdata-filters-row">
        <input type="text" id="rawItemSearch" placeholder="항목 검색">
        <button type="button" id="rawItemSelectAllBtn" class="btn-sm">전체선택</button>
        <div class="rawdata-source-btns">${sourceBtns}</div>
        ${MODE === "edit" ? `<button type="button" id="rawSaveBtn" class="btn-sm btn-report-regen rawdata-regen" disabled>` +
          `Report 재생성 (<span id="rawEditCount">0</span>)</button>` : ""}
      </div>
    </div>
    <div class="rawdata-body">
      <div class="rawdata-cols">
        <div class="rawdata-cols-scroll">
          <div id="rawItemList" class="rawdata-item-list"></div>
        </div>
      </div>
      <div class="rawdata-grid-wrap">
        <div id="rawDataBanner"></div>
        <div id="rawDataHscroll" class="rawdata-hscroll"><div id="rawDataHscrollSpacer" class="rawdata-hscroll-spacer"></div></div>
        <div id="rawDataGridHost"></div>
      </div>
    </div>`;
}

function updateRawSelCount() {
  const el = document.getElementById("rawSelCount");
  if (el) el.textContent = `선택 ${rawDataSelected.size}/${RAW_DATA_COLUMN_CAP}`;
}

function rawBadEditCount() {
  let bad = 0;
  rawDataPendingEdits.forEach(e => { if (e.error) bad += 1; });
  return bad;
}

function updateRawEditBtn() {
  const btn = document.getElementById("rawSaveBtn");
  const cnt = document.getElementById("rawEditCount");
  const bad = rawBadEditCount();
  if (cnt) cnt.textContent = String(rawDataPendingEdits.size);
  // 잘못된 값이 하나라도 있으면 저장을 막는다 — 서버도 400 으로 거부하지만, 재생성이
  // 수십 초 걸리는 작업이라 왕복 전에 여기서 끊는 게 낫다.
  if (btn) {
    btn.disabled = rawDataPendingEdits.size === 0 || bad > 0;
    btn.title = bad ? `잘못된 값 ${bad}건을 고쳐야 저장할 수 있습니다.` : "";
  }
  const banner = document.getElementById("rawDataBanner");
  if (banner && bad) {
    banner.innerHTML = `<div class="rawdata-banner rawdata-banner-bad">잘못된 값 ${bad}건 — ` +
      `빨간 셀을 고쳐야 저장할 수 있습니다.</div>`;
  } else if (banner && banner.querySelector(".rawdata-banner-bad")) {
    banner.innerHTML = "";
  }
}

function renderRawItemList(filterText) {
  const host = document.getElementById("rawItemList");
  if (!host || !rawDataMeta) return;
  const terms = searchTerms(filterText);
  const items = (rawDataMeta.items || []).filter(it => searchMatch(it.name, terms));
  host.innerHTML = items.length ? items.map(it => {
    const selected = rawDataSelected.has(it.name);
    const disabled = (!selected && rawDataSelected.size >= RAW_DATA_COLUMN_CAP) ? "disabled" : "";
    const hasLim = it.unit || it.lolim !== null || it.hilim !== null;
    const limTxt = hasLim
      ? ` <span class="rawdata-item-meta">${esc(it.lolim ?? "-")}~${esc(it.hilim ?? "-")}[${esc(it.unit || "")}]</span>`
      : "";
    return `<button type="button" class="rawdata-item${selected ? " selected" : ""}" ` +
      `data-name="${esc(it.name)}" ${disabled}>${esc(it.name)}${limTxt}</button>`;
  }).join("") : `<div class="placeholder" style="padding:12px;">일치하는 항목 없음</div>`;
  updateRawSelCount();
}

function selectAllVisibleRawItems() {
  if (!rawDataMeta) return;
  const terms = searchTerms(document.getElementById("rawItemSearch")?.value);
  const items = (rawDataMeta.items || []).filter(it => searchMatch(it.name, terms));
  for (const it of items) {
    if (rawDataSelected.size >= RAW_DATA_COLUMN_CAP) break;
    rawDataSelected.add(it.name);
  }
  renderRawItemList(document.getElementById("rawItemSearch")?.value || "");
}

function renderRawDataControls() {
  const panel = document.getElementById("panel-raw-data");
  const sources = (rawDataMeta && rawDataMeta.sources) || [];
  if (sources.length && !sources.includes(rawDataSource)) rawDataSource = sources[0];
  destroyRawDataGrid();
  panel.classList.remove("placeholder");
  panel.innerHTML = rawDataControlsHtml(rawDataMeta);
  renderRawItemList("");
  updateRawEditBtn();
  syncRawFiltersHeight();
  // 프록시 가로 스크롤바 → 실제 그리드 tableholder 동기화(반대 방향은 runRawDataQuery 에서
  // Tabulator 의 scrollHorizontal 이벤트로 처리). #rawDataHscroll 은 panel 이 다시 그려질
  // 때만 새로 생기므로 여기서 1회만 바인딩하면 된다.
  const hscroll = document.getElementById("rawDataHscroll");
  if (hscroll) {
    hscroll.addEventListener("scroll", () => {
      if (_rawHscrollSyncing || !rawDataGrid) return;
      const holder = document.querySelector("#rawDataGridHost .tabulator-tableholder");
      if (!holder) return;
      _rawHscrollSyncing = true;
      holder.scrollLeft = hscroll.scrollLeft;
      _rawHscrollSyncing = false;
    });
  }
}

async function renderRawDataTab() {
  const panel = document.getElementById("panel-raw-data");
  if (!panel) return;
  if (!isWebReportSession()) { destroyRawDataGrid(); emptyPanel(panel, "Raw Data 데이터 없음"); return; }
  if (rawDataMeta) { renderRawDataControls(); return; }
  panel.innerHTML = `<div class="placeholder">불러오는 중...</div>`;
  try {
    rawDataMeta = await fetchRawDataMeta();
  } catch (e) {
    emptyPanel(panel, `Raw Data 메타 조회 실패: ${e.message}`);
    return;
  }
  renderRawDataControls();
}

async function runRawDataQuery() {
  const banner = document.getElementById("rawDataBanner");
  const host = document.getElementById("rawDataGridHost");
  if (!banner || !host) return;
  if (!rawDataSelected.size) {
    banner.innerHTML = `<div class="rawdata-banner">항목을 1개 이상 선택하세요.</div>`;
    return;
  }
  const params = new URLSearchParams();
  params.set("columns", [...rawDataSelected].join(","));
  const search = (document.getElementById("rawSearch")?.value || "").trim();
  const bin = (document.getElementById("rawBin")?.value || "").trim();
  const source = rawDataSource;
  if (search) params.set("search", search);
  if (bin) params.set("bin", bin);
  if (source) params.set("source", source);

  banner.innerHTML = `<div class="rawdata-banner">조회 중...</div>`;
  destroyRawDataGrid();
  host.innerHTML = "";
  let data;
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/raw_data?${params}`, { cache: "no-store" });
    data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  } catch (e) {
    banner.innerHTML = `<div class="rawdata-banner">조회 실패: ${esc(e.message)}</div>`;
    return;
  }
  // 응답이 res.ok 지만 본문이 기대 형태가 아니면(프록시 오류 페이지 등) 여기서 멈춘다 —
  // 아래 data.rows.length / toLocaleString() 이 TypeError 로 "조회 중..." 상태에 갇히는 것 방지.
  if (!Array.isArray(data.rows) || typeof data.total_matched !== "number") {
    banner.innerHTML = `<div class="rawdata-banner">조회 실패: 응답 형식 오류</div>`;
    return;
  }
  banner.innerHTML = data.truncated
    ? `<div class="rawdata-banner">결과가 많아 앞 ${data.rows.length.toLocaleString()}행만 표시합니다 — 필터를 좁혀주세요.</div>`
    : "";
  if (!data.rows.length) { host.innerHTML = `<div class="placeholder">조회 결과 없음</div>`; return; }
  // jsonify(sort_keys=True) 가 응답 키를 알파벳순으로 재정렬하므로 deriveCols(Object.keys
  // 순서 의존) 대신 컬럼 순서를 여기서 명시적으로 고정한다: 고정 메타 컬럼 → 선택한 순서의
  // item 컬럼. _row_idx 는 이 목록에 없으므로 자연히 제외된다 (편집 저장용 내부 필드).
  // SOURCE 는 항상 단일 source 조회라 화면에 표시하지 않는다(값은 row 데이터에 남아
  // cellEdited 핸들러의 row.SOURCE 참조용으로만 쓰인다).
  const present = new Set();
  data.rows.forEach(r => Object.keys(r || {}).forEach(k => present.add(k)));
  const editable = MODE === "edit";
  // 식별 컬럼: #(행번호) + SHOT/DUT/X/Y/BIN/TNO. field 는 기존값 유지(편집/데이터 경로 불변),
  // 화면 라벨(title)만 X/Y/TNO 로 바꾸고 SERIAL 은 표시에서 제외한다.
  const IDENTITY = [
    { field: "_rownum", label: "#", rownum: true },
    { field: "SHOT", label: "SHOT" },
    { field: "DUT", label: "DUT" },
    { field: "XPOS", label: "X" },
    { field: "YPOS", label: "Y" },
    { field: "BIN", label: "BIN" },
    { field: "FAILTNO", label: "TNO" },
  ];
  // 항목 메타(unit/lolim/hilim)는 rawDataMeta 재사용 — 추가 fetch 없음.
  const metaByName = {};
  ((rawDataMeta && rawDataMeta.items) || []).forEach(it => { metaByName[it.name] = it; });
  const rdLine = v => `<span class="rd-meta">${(v === null || v === undefined || v === "") ? "&nbsp;" : esc(String(v))}</span>`;
  // 다중행 헤더: 1행=이름, 2~4행=UNIT/LOLIM/HILIM. 세로 스크롤 시 헤더째 고정된다.
  const metaHead = (label, l1, l2, l3) => `<span class="rd-name">${esc(label)}</span>${rdLine(l1)}${rdLine(l2)}${rdLine(l3)}`;

  // # 컬럼 헤더에만 UNIT/LOLIM/HILIM 라벨을 세로로 쌓아 좌측 라벨 정렬(값은 각 item 컬럼에 표시).
  const idCols = IDENTITY
    .filter(d => d.rownum || present.has(d.field))
    .map(d => ({
      title: d.rownum ? metaHead("#", "UNIT", "LOLIM", "HILIM") : metaHead(d.label, "", "", ""),
      field: d.field, resizable: true, headerSort: false, frozen: true,
      formatter: d.rownum ? "rownum" : rawCellFormatter,
      editor: (editable && !d.rownum) ? "input" : false,
    }));
  const itemCols = [...rawDataSelected].filter(c => present.has(c)).map(c => {
    const m = metaByName[c] || {};
    const unit = m.unit ? `[${m.unit}]` : "";
    return {
      title: metaHead(c, unit, m.lolim, m.hilim), field: c,
      resizable: true, headerSort: false, frozen: false,
      editor: editable ? "input" : false,
      formatter: rawCellFormatter,
    };
  });
  rawDataGrid = new Tabulator("#rawDataGridHost", {
    data: data.rows,
    columns: [...idCols, ...itemCols],
    layout: "fitDataStretch",
    pagination: true, paginationSize: 100, paginationSizeSelector: [50, 100, 200, 500],
    selectableRange: true,      // 셀 클릭 후 화살표 키로 이동(Excel 형) + Shift+화살표 범위 선택
    selectableRangeColumns: true,
    selectableRangeRows: true,
    // 더블클릭으로만 편집 진입 — 단일 클릭은 selectableRange 의 범위 선택 전용으로 남겨둬야
    // 편집기 진입/커밋이 range 선택 제스처와 충돌하지 않고, 편집 중 Enter 로 정상 확정된다.
    editTriggerEvent: "dblclick",
  });
  if (rawDataFailOnly) rawDataGrid.setFilter("BIN", "!=", "1");
  if (editable) {
    rawDataGrid.on("cellEdited", cell => {
      const row = cell.getData();
      const field = cell.getField();
      const key = `${row.SOURCE}|${row._row_idx}|${field}`;
      const prev = rawDataPendingEdits.get(key);
      const isItem = !RAW_META_FIELDS.has(field);
      // diff 표시용(서버 전송 안 함): 같은 셀을 여러 번 고쳐도 최초 기존값을 유지한다.
      const oldValue = prev ? prev.old : cell.getOldValue();
      rawDataPendingEdits.set(key, {
        source: row.SOURCE, row_idx: row._row_idx,
        column: field, value: cell.getValue(),
        old: oldValue,
        where: rawRowLabel(row),
        isItem: isItem,
        error: checkRawCell(field, cell.getValue(), isItem),
        warn: rawCellWarning(field, oldValue, cell.getValue(), isItem),
      });
      cell.getRow().reformat();     // 오류/편집 표시를 즉시 반영
      updateRawEditBtn();
    });
  }
  // 실제 그리드 → 프록시 가로 스크롤바 동기화(반대 방향은 renderRawDataControls 에서 처리).
  // 그리드는 조회할 때마다 파괴/재생성되므로 이 등록도 매번 다시 필요하다.
  rawDataGrid.on("scrollHorizontal", left => {
    const hscroll = document.getElementById("rawDataHscroll");
    if (!hscroll || _rawHscrollSyncing) return;
    _rawHscrollSyncing = true;
    hscroll.scrollLeft = left;
    _rawHscrollSyncing = false;
  });
  // 프록시 스크롤바 폭(스페이서)은 실제 스크롤 가능 폭에 맞아야 하므로, 그리드 최초 빌드/재렌더/
  // 컬럼 리사이즈 시마다 다시 잰다.
  rawDataGrid.on("tableBuilt", syncRawHscrollSpacer);
  rawDataGrid.on("renderComplete", syncRawHscrollSpacer);
  rawDataGrid.on("columnResized", syncRawHscrollSpacer);
}

// 편집 대기열 상태(오류/정상 편집)를 셀에 칠한다. classList 직접 조작은 Tabulator 가상
// 렌더에서 페이지 이동·스크롤 시 지워지므로, 렌더될 때마다 다시 그리는 formatter 로 둔다.
function rawCellFormatter(cell) {
  const row = cell.getData();
  const value = cell.getValue();
  const shown = (value === null || value === undefined) ? "" : String(value);
  const pend = rawDataPendingEdits.get(`${row.SOURCE}|${row._row_idx}|${cell.getField()}`);
  if (!pend) return esc(shown);
  if (pend.error) return `<span class="rd-cell-bad" title="${esc(pend.error)}">${esc(shown)}</span>`;
  return `<span class="rd-cell-edited">${esc(shown)}</span>`;
}

// Raw Data 편집 행을 사람이 읽을 수 있는 위치 문자열로 (diff 표 "위치" 컬럼용).
function rawRowLabel(row) {
  const parts = [];
  if (row.SOURCE) parts.push(String(row.SOURCE));
  if (row.SHOT !== undefined && row.SHOT !== "") parts.push(`SHOT ${row.SHOT}`);
  if (row.DUT !== undefined && row.DUT !== "") parts.push(`DUT ${row.DUT}`);
  if (row.XPOS !== undefined && row.YPOS !== undefined) parts.push(`(X,Y)=(${row.XPOS},${row.YPOS})`);
  if (row.BIN !== undefined && row.BIN !== "") parts.push(`BIN ${row.BIN}`);
  return parts.join(" · ");
}

// "Report 재생성" 클릭 시: 즉시 저장하지 않고 변경 diff 를 모달로 보여준 뒤 예/아니요 확인.
function openRawRegenConfirm() {
  if (!rawDataPendingEdits.size) return;
  const edits = [...rawDataPendingEdits.values()];
  const bad = rawBadEditCount();
  const rows = edits.map(e => {
    const oldTxt = (e.old === null || e.old === undefined || e.old === "") ? "(빈값)" : String(e.old);
    // 실제 저장될 정규형을 보여준다 — ' 1 '/'01' 을 넣었는데 '1' 로 저장되는 괴리 방지.
    const normalized = normalizeRawCell(e.column, e.value, e.isItem);
    const newTxt = normalized === "" ? "(빈값)" : normalized;
    const cls = e.error ? " class=\"rd-row-bad\"" : (e.warn ? " class=\"rd-row-warn\"" : "");
    return `<tr${cls}>` +
      `<td>${esc(e.where || e.source || "")}</td>` +
      `<td>${esc(e.column)}</td>` +
      `<td class="rd-old">${esc(oldTxt)}</td>` +
      `<td class="rd-arrow">→</td>` +
      `<td class="rd-new">${esc(newTxt)}</td>` +
      `<td class="rd-why">${esc(e.error || e.warn || "")}</td>` +
    `</tr>`;
  }).join("");
  const host = document.getElementById("rawRegenDiff");
  if (host) {
    const notice = bad
      ? `<div class="rawdata-banner rawdata-banner-bad">잘못된 값 ${bad}건이 있어 저장할 수 없습니다 — ` +
        `아래 빨간 줄을 고쳐 주세요.</div>`
      : "";
    host.innerHTML = notice +
      `<table><thead><tr><th>위치</th><th>항목</th><th>기존값</th><th></th><th>바뀐값</th>` +
      `<th>확인</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  const okBtn = document.getElementById("rawRegenConfirm");
  if (okBtn) okBtn.disabled = bad > 0;
  const modal = document.getElementById("rawRegenModal");
  if (modal) modal.classList.add("show");
}

function closeRawRegenConfirm() {
  const modal = document.getElementById("rawRegenModal");
  if (modal) modal.classList.remove("show");
}

async function saveRawDataEdits() {
  if (!rawDataPendingEdits.size) return;
  const banner = document.getElementById("rawDataBanner");
  // 모달을 우회해 호출되더라도 잘못된 값은 보내지 않는다(서버도 400 으로 막지만 이중 방어).
  const bad = rawBadEditCount();
  if (bad) {
    hideLoadOverlay();
    if (banner) {
      banner.innerHTML = `<div class="rawdata-banner rawdata-banner-bad">잘못된 값 ${bad}건이 있어 ` +
        `저장하지 않았습니다 — 빨간 셀을 고쳐 주세요.</div>`;
    }
    return;
  }
  // 서버에는 기존처럼 source/row_idx/column/value 4필드만 전송 (old/where 는 diff 표시 전용).
  const edits = [...rawDataPendingEdits.values()].map(e => ({
    source: e.source, row_idx: e.row_idx, column: e.column, value: e.value,
  }));
  if (banner) banner.innerHTML = `<div class="rawdata-banner">저장 중...</div>`;
  // 재생성은 서버가 parquet 전체를 재디코드·재인코딩하고 캐시를 비워 대용량 세션은
  // 1분 이상 걸릴 수 있다 — 텍스트 배너만으론 멈춘 것처럼 보여 load 오버레이를 함께 띄운다
  // (성공 시 이어지는 load(false) 가 오버레이를 이어받고, 실패 catch 가 걷는다).
  showLoadOverlay();
  startLoadCreep(4, 60, 30000, "Rawdata 저장 · Report 재생성 중…");
  scheduleLoadStageMsgs();
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/raw_data/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ password: verifiedPassword, edits }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    rawDataPendingEdits.clear();
    showToast(`Raw Data ${edits.length}개 셀 저장 완료 — 수정된 rawdata 로 Report(yield·cpk·distribution) 및 parquet 을 재생성합니다.`);
    await load(false);
    // 세션 재로드 후 Raw Data 그리드는 비워지므로, 방금 저장한 항목/필터로 재조회해
    // 저장된 값이 화면에 바로 보이게 한다.
    if (rawDataSelected.size && document.getElementById("rawDataGridHost")) {
      await runRawDataQuery();
    }
  } catch (e) {
    hideLoadOverlay();
    if (banner) banner.innerHTML = `<div class="rawdata-banner">저장 실패: ${esc(e.message)}</div>`;
  }
}

// Raw Data 전용 이벤트 위임 — panel 자체는 고정 요소(#panel-raw-data), 내부만 매번 다시 그려짐.
// input 이벤트는 stopPropagation 로 .content 의 전역 dirty-마킹 리스너로 안 새게 막는다
// (Raw Data 는 조회 전용이라 편집 dirty 상태와 무관해야 함).
const _rawDataPanel = document.getElementById("panel-raw-data");
if (_rawDataPanel) {
  _rawDataPanel.addEventListener("input", e => {
    e.stopPropagation();
    if (e.target.id === "rawItemSearch") renderRawItemList(e.target.value);
  });
  _rawDataPanel.addEventListener("click", e => {
    if (e.target.closest("#rawQueryBtn")) { runRawDataQuery(); return; }
    if (e.target.closest("#rawSaveBtn")) { openRawRegenConfirm(); return; }
    if (e.target.closest("#rawItemSelectAllBtn")) { selectAllVisibleRawItems(); return; }
    if (e.target.closest("#rawFailOnlyBtn")) {
      rawDataFailOnly = !rawDataFailOnly;
      e.target.closest("#rawFailOnlyBtn").classList.toggle("active", rawDataFailOnly);
      if (rawDataGrid) {
        if (rawDataFailOnly) rawDataGrid.setFilter("BIN", "!=", "1");
        else rawDataGrid.clearFilter();
      }
      return;
    }
    const sourceBtn = e.target.closest(".rawdata-source-btn");
    if (sourceBtn) {
      rawDataSource = sourceBtn.dataset.source || "";
      document.querySelectorAll(".rawdata-source-btn").forEach(b =>
        b.classList.toggle("active", b.dataset.source === rawDataSource));
      runRawDataQuery();
      return;
    }
    const itemBtn = e.target.closest("#rawItemList .rawdata-item");
    if (itemBtn) {
      const name = itemBtn.dataset.name;
      if (rawDataSelected.has(name)) {
        rawDataSelected.delete(name);
      } else {
        if (rawDataSelected.size >= RAW_DATA_COLUMN_CAP) return;
        rawDataSelected.add(name);
      }
      renderRawItemList(document.getElementById("rawItemSearch")?.value || "");
    }
  });
}


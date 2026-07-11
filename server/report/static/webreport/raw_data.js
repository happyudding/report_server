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

function updateRawEditBtn() {
  const btn = document.getElementById("rawSaveBtn");
  const cnt = document.getElementById("rawEditCount");
  if (cnt) cnt.textContent = String(rawDataPendingEdits.size);
  if (btn) btn.disabled = rawDataPendingEdits.size === 0;
}

function renderRawItemList(filterText) {
  const host = document.getElementById("rawItemList");
  if (!host || !rawDataMeta) return;
  const q = String(filterText || "").trim().toLowerCase();
  const items = (rawDataMeta.items || []).filter(it => !q || it.name.toLowerCase().includes(q));
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
  const q = (document.getElementById("rawItemSearch")?.value || "").trim().toLowerCase();
  const items = (rawDataMeta.items || []).filter(it => !q || it.name.toLowerCase().includes(q));
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
      formatter: d.rownum ? "rownum" : undefined,
      editor: (editable && !d.rownum) ? "input" : false,
    }));
  const itemCols = [...rawDataSelected].filter(c => present.has(c)).map(c => {
    const m = metaByName[c] || {};
    const unit = m.unit ? `[${m.unit}]` : "";
    return {
      title: metaHead(c, unit, m.lolim, m.hilim), field: c,
      resizable: true, headerSort: false, frozen: false,
      editor: editable ? "input" : false,
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
      const key = `${row.SOURCE}|${row._row_idx}|${cell.getField()}`;
      const prev = rawDataPendingEdits.get(key);
      rawDataPendingEdits.set(key, {
        source: row.SOURCE, row_idx: row._row_idx,
        column: cell.getField(), value: cell.getValue(),
        // diff 표시용(서버 전송 안 함): 같은 셀을 여러 번 고쳐도 최초 기존값을 유지한다.
        old: prev ? prev.old : cell.getOldValue(),
        where: rawRowLabel(row),
      });
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
  const rows = edits.map(e => {
    const oldTxt = (e.old === null || e.old === undefined || e.old === "") ? "(빈값)" : String(e.old);
    const newTxt = (e.value === null || e.value === undefined || e.value === "") ? "(빈값)" : String(e.value);
    return `<tr>` +
      `<td>${esc(e.where || e.source || "")}</td>` +
      `<td>${esc(e.column)}</td>` +
      `<td class="rd-old">${esc(oldTxt)}</td>` +
      `<td class="rd-arrow">→</td>` +
      `<td class="rd-new">${esc(newTxt)}</td>` +
    `</tr>`;
  }).join("");
  const host = document.getElementById("rawRegenDiff");
  if (host) {
    host.innerHTML =
      `<table><thead><tr><th>위치</th><th>항목</th><th>기존값</th><th></th><th>바뀐값</th></tr></thead>` +
      `<tbody>${rows}</tbody></table>`;
  }
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
  // 서버에는 기존처럼 source/row_idx/column/value 4필드만 전송 (old/where 는 diff 표시 전용).
  const edits = [...rawDataPendingEdits.values()].map(e => ({
    source: e.source, row_idx: e.row_idx, column: e.column, value: e.value,
  }));
  if (banner) banner.innerHTML = `<div class="rawdata-banner">저장 중...</div>`;
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


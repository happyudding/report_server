// ── 상세(검색뷰): CDF + 히스토그램 (전체점 지연 로드) ─────────────────────────
// ── Item_detail (콘텐츠 상세 뷰) ────────────────────────────────────────────────
// sticky-head(topbar+tabs)는 그대로 두고 #panel-item-detail 만 활성화한다. Back(또는 Esc)으로
// 원래 탭 패널 복원. Distribution 카드/Yield/CPK/IssueTable item 클릭 모두 이 화면으로 온다.
const ITEM_FAIL_PAGE_SIZE = 30;
let _itemDetailReturnId = null;   // 복귀할 탭 패널 id
let _itemDetailSubject = null;
let _itemDetailNav = [];          // prev/next 이동 목록(subject 배열)
let _itemDetailFailRows = [];
let _itemDetailFailPage = 1;
let _itemDetailReq = 0;           // 경합 가드(진행 중 fetch 무효화)
let _itemDetailBound = false;
let _itemDetailData = null;        // 현재 상세 응답(scatter_item) — CDF 재렌더에 재사용
let idetHistMode = "analysis";     // 히스토그램 블록 탭: "analysis"(빈도 폴리곤) | "report"(정규분포 곡선)
let _idetNormalRendered = false;   // report 정규분포 곡선 1회 렌더 가드(항목 진입마다 리셋)
// ── CDF 칩 편집(임시, 클라이언트 전용) — 항목 이동/새로고침 시 초기화 ──────────────
let cdfExcluded = new Set();       // 제외할 칩 키 `${source}||${serial}` → CDF 곡선에서 뺌(분모 감소)
let cdfEditMode = "none";          // "none" | "exclude" (선택이 cdfExcluded 에 들어가는지)
// CDF x축 옵션(임시, 클라이언트 전용) — Excel 축옵션식 경계/단위. 항목 이동/새로고침 시 초기화.
let cdfAxisOverride = null;         // null=자동(autorange). 적용 시 {min, max, major|null, minor|null}
// 칩(die) 고유 식별키 — SERIAL 이 die 간 중복될 수 있어 XPOS/YPOS 까지 포함해야
// 드래그/클릭 제외가 정확히 그 die 만 겨냥한다(serial 단독이면 같은 serial 전량 오제외).
function cdfChipKey(source, serial, xpos, ypos) {
  return `${source}||${serial}||${xpos == null ? "" : xpos}||${ypos == null ? "" : ypos}`;
}
function cdfActiveSet() { return cdfEditMode === "exclude" ? cdfExcluded : null; }
function cdfResetEdits() { cdfExcluded.clear(); cdfEditMode = "none"; }

function openItemDetail(subject, navList) {
  const dp = document.getElementById("panel-item-detail");
  if (!dp) return;
  bindItemDetailPanel();
  // 상세가 아직 안 열려 있으면 현재 활성 탭 패널을 복귀 대상으로 기억하고 숨긴다.
  if (!dp.classList.contains("active")) {
    const cur = document.querySelector(".content > .panel.active");
    _itemDetailReturnId = cur ? cur.id : "panel-summary";
    if (cur) cur.classList.remove("active");
    dp.classList.add("active");
  }
  _itemDetailSubject = subject;
  _itemDetailNav = Array.isArray(navList) && navList.length ? navList : [subject];
  _itemDetailFailPage = 1;
  cdfResetEdits();   // 항목이 바뀌면 CDF 제외 편집 초기화
  cdfAxisOverride = null;   // 항목이 바뀌면 CDF x축 옵션(경계/단위)도 자동으로 되돌림
  _itemDetailData = null;
  const reqId = ++_itemDetailReq;
  window.scrollTo(0, 0);
  purgeItemDetailCharts();   // 항목 이동 시 이전 차트(WebGL 컨텍스트) 해제 후 갈아끼움
  dp.innerHTML = `<div class="idet"><div class="idet-head"><button class="btn-sm idet-back">← Back</button>` +
    `<span class="idet-title"><b>${esc(subject)}</b></span></div><div class="placeholder">로드 중…</div></div>`;
  // Bin1 only 가 켜져 있으면 상세도 양품(BIN==1)만으로 낸 분포/통계를 받는다(?bin1=1).
  // cache 옵션 없음(기본) — 서버 ETag 조건부 응답으로 재클릭·재방문 시 304 재검증된다.
  const scatterUrl = `/pe/report/session/${SESSION_ID}/web_report/scatter/${encodeURIComponent(subject)}`
    + (distBin1Only ? "?bin1=1" : "");
  fetch(scatterUrl)
    .then(res => { if (!res.ok) throw new Error("HTTP " + res.status); return res.json(); })
    .then(data => { if (reqId === _itemDetailReq) renderItemDetail(data); })
    .catch(e => {
      if (reqId !== _itemDetailReq) return;
      dp.innerHTML = `<div class="idet"><div class="idet-head"><button class="btn-sm idet-back">← Back</button></div>` +
        `<div class="placeholder">상세 로드 실패 (${esc(e.message)})</div></div>`;
    });
}

// 헤더 요약 통계: min/max/average/stdev 를 한 줄로. 다중 source 면 첫 source 값을
// 소스명과 함께 표시(상세 per-source 값은 아래 통계표에서 전부 확인 가능).
function idetHeaderStats(stats) {
  if (!stats || !stats.length) return "";
  const s = stats[0];
  const fmt = v => (v === null || v === undefined || v === "") ? "-" : String(v);
  // min/max/avg 는 표시용으로 소수 4자리 반올림(round,4). 숫자가 아니면 원래 표기 유지.
  const fmt4 = v => (typeof v === "number") ? String(Math.round(v * 1e4) / 1e4) : fmt(v);
  const src = stats.length > 1 ? `<span class="idet-stat-src">${esc(s.source || "")}</span>` : "";
  return `<span class="idet-stat">${src}` +
    `min <b>${esc(fmt4(s.min))}</b> · max <b>${esc(fmt4(s.max))}</b> · ` +
    `avg <b>${esc(fmt4(s.average))}</b> · σ <b>${esc(fmt(s.stdev))}</b></span>`;
}

// Fail 항목이면 Fail 된 die 의 BIN 목록(중복 제거·숫자 우선 정렬)을 헤더에 표시.
// fail_rows 는 fail_row_cap 상한(fail_truncated)에서 잘릴 수 있어, 그 경우 표시 bin 도
// 잘린 행 기준이다(항목별 fail die 가 상한을 넘는 드문 경우).
function idetFailBinsHtml(data) {
  if (!data || !data.is_fail) return "";
  const seen = new Set(), bins = [];
  for (const r of (data.fail_rows || [])) {
    const b = r.BIN;
    if (b === null || b === undefined || b === "") continue;
    const key = String(b);
    if (!seen.has(key)) { seen.add(key); bins.push(key); }
  }
  if (!bins.length) return "";
  bins.sort((a, b) => {
    const na = Number(a), nb = Number(b), aNum = !isNaN(na), bNum = !isNaN(nb);
    if (aNum && bNum) return na - nb;
    if (aNum !== bNum) return aNum ? -1 : 1;
    return a < b ? -1 : a > b ? 1 : 0;
  });
  return `<span class="idet-fail-bins">Bin ${esc(bins.join(", "))}</span>`;
}

// 상세 상단 공용 legend 스트립 — distUseExtLegend 일 때만 (툴바 .dist-legend 와 동일
// 마크업/CSS 재사용, flex-wrap 줄바꿈). 순서는 상세 응답 data.sources 순서.
function idetLegendHtml(data) {
  if (!distUseExtLegend(data)) return "";
  return `<div class="dist-legend idet-legend">` + (data.sources || []).map(s =>
    `<span class="dist-leg-item"><span class="dist-leg-sw" style="background:${distColorFor(s.name)}"></span>${esc(s.name)}</span>`
  ).join("") + `</div>`;
}

function renderItemDetail(data) {
  const dp = document.getElementById("panel-item-detail");
  if (!dp) return;
  const subject = data.subject;
  _itemDetailData = data;   // CDF 재렌더(제외 반영)에 재사용
  idetHistMode = "analysis";      // 항목 진입 시 기본 Analysis 탭
  _idetNormalRendered = false;
  const navLen = _itemDetailNav.length;
  const pos = _itemDetailNav.indexOf(subject);
  const navHtml = navLen > 1
    ? `<button class="btn-sm idet-prev" title="이전 (Alt+↑)">‹</button>` +
      `<button class="btn-sm idet-next" title="다음 (Alt+↓)">›</button>` +
      `<span class="idet-navpos">${pos + 1} / ${navLen}</span>` : "";
  const statusLabel = { fail: "FAIL", cpk_low: "CPK LOW", ok: "OK" }[data.status] || data.status || "";
  _itemDetailFailRows = data.fail_rows || [];
  const failTitle = data.is_fail
    ? `<div class="section-title small idet-fail-title">이 항목으로 Fail 된 die — ${data.fail_total}개` +
      `${data.fail_truncated ? ` (앞 ${_itemDetailFailRows.length}개만 표시)` : ""}</div><div id="idetFailHost"></div>`
    : "";
  dp.innerHTML = `<div class="idet">
    <div class="idet-head">
      <button class="btn-sm idet-back">← Back</button>
      ${navHtml}
      <span class="idet-title" data-status="${esc(data.status || "ok")}">
        ${data.test_num ? `<span class="idet-tno">#${esc(data.test_num)}</span>` : ""}
        <b class="idet-subject">${esc(subject)}</b>
        <span class="idet-badge idet-${esc(data.status || "ok")}">${esc(statusLabel)}</span>
        ${idetFailBinsHtml(data)}
        <span class="idet-cpk">cpk ${esc(data.cpk == null ? "-" : data.cpk)}</span>
        <span class="idet-lim">(${distLimInnerHtml(data.lower_limit, data.upper_limit, data.units)})</span>
        ${idetHeaderStats(data.stats)}
      </span>
    </div>
    <div id="cdfEditBar" class="cdf-editbar"></div>
    <div id="cdfAxisBar" class="cdf-axisbar"></div>
    <div id="chartNoteBar"></div>
    ${idetLegendHtml(data)}
    <div class="idet-charts">
      <div class="idet-chart-block"><div class="dist-chart-cap">누적분포 CDF</div><div id="distCdf" class="dist-chart"></div>
        <div class="idet-chart-comment" id="cdfCommentView"></div></div>
      <div class="idet-chart-block">
        <div class="dist-chart-cap idet-hist-cap">
          <span>분포 히스토그램</span>
          <span class="idet-hist-tabs">
            <button type="button" class="btn-sm idet-hist-mode${idetHistMode === "analysis" ? " active" : ""}" data-hist-mode="analysis">Analysis</button>
            <button type="button" class="btn-sm idet-hist-mode${idetHistMode === "report" ? " active" : ""}" data-hist-mode="report">Report</button>
          </span>
        </div>
        <div id="distHist" class="dist-chart"${idetHistMode === "report" ? ' style="display:none"' : ""}></div>
        <div id="distNormal" class="dist-chart"${idetHistMode === "analysis" ? ' style="display:none"' : ""}></div>
        <div class="idet-chart-comment" id="histCommentView"></div>
      </div>
    </div>
    ${itemStatsTableHtml(data.stats)}
    <div id="idetChipVals"></div>
    ${failTitle}
  </div>`;
  renderCdfEditBar();
  renderCdfAxisBar();
  distRenderDetailCharts(data);   // #distCdf / #distHist (기존 함수 재사용)
  if (window.chartNotesBar) chartNotesBar(data);   // 차트 주석 툴바 (chart_notes.js)
  if (window.cnRenderChartComments) cnRenderChartComments(subject);   // 차트 하단 Comment 표시
  renderIdetChipVals();           // Map Analysis 선택 좌표의 이 항목 값
  if (data.is_fail) renderItemFailRows();
}

// Map Analysis 에서 선택한 좌표(mapSelChips)의 '현재 항목' 측정값·누적% 표. 좌표 변경 시에도 갱신.
function idetChipValuesHtml(subject) {
  if (!mapSelChips.length) return "";
  const rows = mapSelChips.map(c => {
    const it = c.items[subject];
    const val = (it && typeof it.value === "number") ? String(it.value) : "-";
    const cum = (it && typeof it.cum_pct === "number") ? it.cum_pct.toFixed(1) + "%" : "-";
    return `<tr>` +
      `<td><span class="mapsel-sw" style="background:${c.color}"></span></td>` +
      `<td>${esc(c.source || "")}</td>` +
      `<td>X ${esc(c.xpos)} · Y ${esc(c.ypos)}</td>` +
      `<td>${esc(c.serial == null ? "" : c.serial)}</td>` +
      `<td class="num">${esc(val)}</td><td class="num">${esc(cum)}</td></tr>`;
  }).join("");
  return `<div class="section-title small">선택 좌표의 이 항목 값 (Map Analysis)</div>` +
    `<table class="idet-chipval-table"><thead><tr>` +
    `<th></th><th>Source</th><th>좌표</th><th>SERIAL</th><th>값</th><th>누적%</th>` +
    `</tr></thead><tbody>${rows}</tbody></table>`;
}
function renderIdetChipVals() {
  const host = document.getElementById("idetChipVals");
  if (!host) return;
  host.innerHTML = _itemDetailData ? idetChipValuesHtml(_itemDetailData.subject) : "";
}

const ITEM_STAT_COLS = ["n", "min", "median", "max", "average", "stdev", "cp", "cpl", "cpu", "cpk"];
function itemStatsTableHtml(stats) {
  if (!stats || !stats.length) return "";
  const multi = stats.length > 1;
  const head = "<thead><tr>" + (multi ? "<th>source</th>" : "") +
    ITEM_STAT_COLS.map(c => `<th>${esc(c)}</th>`).join("") + "</tr></thead>";
  const body = "<tbody>" + stats.map(s => {
    const warn = (s.cpk != null && parseFloat(s.cpk) < CPK_WARN_THRESHOLD);
    const tds = ITEM_STAT_COLS.map(c => {
      const v = s[c]; const t = (v === null || v === undefined) ? "" : String(v);
      const cls = "st-num" + (c === "cpk" && warn ? " cpk-warn" : "");
      return `<td class="${cls}">${esc(t)}</td>`;
    }).join("");
    return "<tr>" + (multi ? `<td>${esc(s.source)}</td>` : "") + tds + "</tr>";
  }).join("") + "</tbody>";
  return `<div class="idet-stats sheet-wrap"><table class="sheet-table">${head}${body}</table></div>`;
}

function renderItemFailRows() {
  const host = document.getElementById("idetFailHost");
  if (!host) return;
  const rows = _itemDetailFailRows;
  if (!rows.length) { host.innerHTML = `<div class="placeholder">Fail 행 없음</div>`; return; }
  const multi = !!(DATA.web_report && (DATA.web_report.sources || []).length > 1);
  const cols = (multi ? ["SOURCE"] : []).concat(["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO", "value"]);
  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total / ITEM_FAIL_PAGE_SIZE));
  if (_itemDetailFailPage > pages) _itemDetailFailPage = pages;
  if (_itemDetailFailPage < 1) _itemDetailFailPage = 1;
  const start = (_itemDetailFailPage - 1) * ITEM_FAIL_PAGE_SIZE;
  const page = rows.slice(start, start + ITEM_FAIL_PAGE_SIZE);
  // 체크박스 열: 현재 편집 모드(제외)의 Set 멤버십을 표시·토글. 모드=없음이면 비활성.
  const editing = cdfEditMode !== "none";
  const activeSet = cdfActiveSet();
  const chkLabel = cdfEditMode === "exclude" ? "제외" : "칩";
  const chkTh = `<th class="idet-fail-chk-col">${chkLabel}</th>`;
  const head = "<thead><tr>" + chkTh + cols.map(c => `<th>${esc(c)}</th>`).join("") + "</tr></thead>";
  const body = "<tbody>" + page.map(r => {
    const key = cdfChipKey(r.SOURCE, r.SERIAL, r.XPOS, r.YPOS);
    const checked = activeSet && activeSet.has(key) ? " checked" : "";
    const chkTd = `<td class="idet-fail-chk-col"><input type="checkbox" class="cdf-fail-chk" data-chipkey="${esc(key)}"${editing ? "" : " disabled"}${checked}></td>`;
    return "<tr>" + chkTd + cols.map(c => {
      const v = r[c]; const t = (v === null || v === undefined) ? "" : String(v);
      return `<td>${esc(t)}</td>`;
    }).join("") + "</tr>";
  }).join("") + "</tbody>";
  const end = Math.min(start + ITEM_FAIL_PAGE_SIZE, total);
  const pager = pages > 1
    ? `<div class="cpk-pager">` +
      `<button type="button" class="btn-sm" data-idet-page="${_itemDetailFailPage - 1}"${_itemDetailFailPage <= 1 ? " disabled" : ""}>‹ 이전</button>` +
      `<span class="cpk-pager-info">${start + 1}–${end} / ${total} (page ${_itemDetailFailPage}/${pages})</span>` +
      `<button type="button" class="btn-sm" data-idet-page="${_itemDetailFailPage + 1}"${_itemDetailFailPage >= pages ? " disabled" : ""}>다음 ›</button></div>`
    : "";
  host.innerHTML = `<div class="sheet-wrap"><table class="sheet-table">${head}${body}</table></div>` + pager;
}

function itemDetailNav(delta) {
  if (_itemDetailNav.length < 2) return;
  let p = _itemDetailNav.indexOf(_itemDetailSubject) + delta;
  if (p < 0) p = 0;
  if (p >= _itemDetailNav.length) p = _itemDetailNav.length - 1;
  const next = _itemDetailNav[p];
  if (next && next !== _itemDetailSubject) openItemDetail(next, _itemDetailNav);
}

// 상세 패널 Plotly 차트 해제 — scattergl(WebGL 컨텍스트)은 innerHTML 교체만으로 회수되지
// 않으므로 패널을 비우기/갈아끼우기 전에 purge 한다 (SVG 차트에도 무해).
function purgeItemDetailCharts() {
  ["distCdf", "distHist", "distNormal"].forEach(id => {
    const el = document.getElementById(id);
    if (el && el.data) { try { Plotly.purge(el); } catch (e) { /* no-op */ } }
  });
}

function closeItemDetail() {
  const dp = document.getElementById("panel-item-detail");
  if (!dp) return;
  _itemDetailReq++;   // 진행 중 fetch 무효화
  dp.classList.remove("active");
  purgeItemDetailCharts();
  dp.innerHTML = "";
  const back = document.getElementById(_itemDetailReturnId || "panel-summary");
  if (back) back.classList.add("active");
  _itemDetailReturnId = null;
}

// 탭 버튼 클릭 시: 복원 없이 상세만 닫는다(해당 탭 패널이 이어서 활성화됨).
function hideItemDetail() {
  const dp = document.getElementById("panel-item-detail");
  if (dp && dp.classList.contains("active")) { _itemDetailReq++; dp.classList.remove("active"); purgeItemDetailCharts(); dp.innerHTML = ""; }
  _itemDetailReturnId = null;
}

function bindItemDetailPanel() {
  if (_itemDetailBound) return;
  const dp = document.getElementById("panel-item-detail");
  if (!dp) return;
  dp.addEventListener("click", e => {
    if (e.target.closest(".idet-back")) { closeItemDetail(); return; }
    if (e.target.closest(".idet-prev")) { itemDetailNav(-1); return; }
    if (e.target.closest(".idet-next")) { itemDetailNav(1); return; }
    const hm = e.target.closest("[data-hist-mode]");
    if (hm) { setIdetHistMode(hm.dataset.histMode); return; }
    const mb = e.target.closest("[data-cdf-mode]");
    if (mb) { cdfEditMode = mb.dataset.cdfMode; cdfAfterEdit(); return; }
    if (e.target.closest(".cdf-reset")) { cdfResetEdits(); cdfAfterEdit(); return; }
    const axb = e.target.closest("[data-cdf-axis]");
    if (axb) { axb.dataset.cdfAxis === "apply" ? cdfAxisApply() : cdfAxisAuto(); return; }
    const pg = e.target.closest("[data-idet-page]");
    if (pg && !pg.disabled) { _itemDetailFailPage = parseInt(pg.dataset.idetPage, 10) || 1; renderItemFailRows(); return; }
  });
  dp.addEventListener("change", e => {
    const chk = e.target.closest(".cdf-fail-chk");
    if (!chk) return;
    const set = cdfActiveSet();
    if (!set) return;
    if (chk.checked) set.add(chk.dataset.chipkey); else set.delete(chk.dataset.chipkey);
    cdfAfterEdit();
  });
  dp.addEventListener("keydown", e => {   // 축옵션 입력칸에서 Enter → 적용
    if (e.key === "Enter" && e.target.closest(".cdf-ax-in")) { e.preventDefault(); cdfAxisApply(); }
  });
  document.addEventListener("keydown", e => {
    if (!dp.classList.contains("active")) return;
    if (e.key === "Escape") { closeItemDetail(); return; }
    if (e.altKey && e.key === "ArrowUp") { e.preventDefault(); itemDetailNav(-1); }
    else if (e.altKey && e.key === "ArrowDown") { e.preventDefault(); itemDetailNav(1); }
  });
  _itemDetailBound = true;
}
// values 를 오름차순 정렬하며 원래 index 순서를 함께 반환 — serial/xpos/ypos 를 같은 순서로
// 재배열해 CDF hover(customdata)에 붙이는 데 쓴다.
function distCdfFromValues(values) {
  const order = values.map((_, i) => i).sort((a, b) => values[a] - values[b]);
  const n = order.length;
  return { x: order.map(i => values[i]), y: order.map((_, k) => (k + 1) / n * 100), order };
}
// 빈도 폴리곤: subject 의 모든 source 를 21 균등구간으로 빈닝해 중심점-빈도 곡선을 만든다.
// 서버가 보낸 원본 values 재사용(재계산 최소화, 다운샘플 없음). 반환 곡선은 양끝 0 패딩 포함 23점.
function distHistPolygon(sources, lo, hi, excluded) {
  // excluded(Set): CDF '제외' 편집으로 뺀 die 키. 빈 구간(bin) 위치는 전체값 기준으로 고정하고
  // (제외해도 x 위치가 흔들리지 않게) 카운트에서만 제외 die 를 뺀다.
  const useExcl = excluded && excluded.size;
  // 3-1 hist_range(소스 공용): 전체값 min/max + LSL/USL 확장. 대용량이라 스프레드 대신 for 루프.
  let rlo = Infinity, rhi = -Infinity;
  (sources || []).forEach(s => {
    for (const v of s.values) { if (v < rlo) rlo = v; if (v > rhi) rhi = v; }
  });
  [lo, hi].forEach(v => { if (v !== null && v !== undefined) {
    if (v < rlo) rlo = v; if (v > rhi) rhi = v; } });
  if (!isFinite(rlo) || !isFinite(rhi)) { rlo = 0; rhi = 1; }   // 값이 전무한 경우 가드
  if (rhi === rlo) rhi = rlo + 1;                                // 축퇴(단일값) 가드
  const B = 21, step = (rhi - rlo) / B;
  // 3-3 중심점(21) → 3-4 양끝 0 패딩(bw=step)으로 23점 (곡선이 바닥에서 시작·종료)
  const centers = [rlo - step * 0.5];                            // padded left
  for (let k = 0; k < B; k++) centers.push(rlo + step * (k + 0.5));
  centers.push(rlo + step * (B + 0.5));                          // padded right
  return (sources || []).map(s => {
    // 3-2 균등 21구간 누적 (오른쪽 끝값은 마지막 bin 에 포함). 3-5 빈 source 는 자연히 전부 0.
    const counts = new Array(B).fill(0);
    const hasId = Array.isArray(s.serial) && s.serial.length === s.values.length;
    for (let i = 0; i < s.values.length; i++) {
      if (useExcl && hasId && excluded.has(cdfChipKey(s.name, s.serial[i], s.xpos[i], s.ypos[i]))) continue;
      const v = s.values[i];
      let idx = Math.floor((v - rlo) / step);
      if (idx === B) idx = B - 1;
      if (idx >= 0 && idx < B) counts[idx]++;
    }
    return { source: s.name, centers, counts: [0, ...counts, 0] };  // 패딩 0 앞뒤 → 23
  });
}
// 스펙 4장 x축 표시범위(히스토그램 전용): 소스 합친 data min/max·median 기준 4분기 규칙.
// 가드밴드 5% 기준은 USL-LSL span, median 은 소스 합친 전체값 기준.
function distHistXRange(sources, lo, hi, isFail) {
  const all = [];
  (sources || []).forEach(s => { for (const v of s.values) all.push(v); });
  const hasLo = lo !== null && lo !== undefined, hasHi = hi !== null && hi !== undefined;
  if (hasLo && hasHi) {
    if (!isFail) return [lo, hi];
    const span = hi - lo, gb = span * 0.05;
    let dmin = Infinity, dmax = -Infinity;
    for (const v of all) { if (v < dmin) dmin = v; if (v > dmax) dmax = v; }
    let x0 = lo, x1 = hi;
    if (dmin < lo) x0 = dmin - gb;      // 아래로 벗어난 쪽만 확장
    if (dmax > hi) x1 = dmax + gb;      // 위로 벗어난 쪽만 확장
    return [x0, x1];
  }
  // median (소스 합친 전체값)
  let med = 0;
  if (all.length) {
    const sorted = all.slice().sort((a, b) => a - b), m = sorted.length >> 1;
    med = sorted.length % 2 ? sorted[m] : (sorted[m - 1] + sorted[m]) / 2;
  }
  if (hasLo) return [lo, 2 * med - lo];        // USL 없음 → 위쪽을 median 대칭 확장
  if (hasHi) return [2 * med - hi, hi];        // LSL 없음 → 아래쪽을 median 대칭 확장
  // Limit 없음 → data min/max
  let dmin = Infinity, dmax = -Infinity;
  for (const v of all) { if (v < dmin) dmin = v; if (v > dmax) dmax = v; }
  if (!isFinite(dmin) || !isFinite(dmax)) return undefined;   // 값 전무 → Plotly 자동
  return [dmin, dmax];
}
// CDF 편집 툴바 렌더 (모드 세그먼트 + 초기화 + 카운트). 리스너는 bindItemDetailPanel 위임.
function renderCdfEditBar() {
  const bar = document.getElementById("cdfEditBar");
  if (!bar) return;
  const modeBtn = (m, label, cls) =>
    `<button type="button" class="btn-sm cdf-mode ${cls}${cdfEditMode === m ? " active" : ""}" data-cdf-mode="${m}">${label}</button>`;
  bar.innerHTML =
    `<span class="cdf-eb-label">CDF 편집</span>` +
    modeBtn("none", "선택 없음", "cdf-mode-none") +
    modeBtn("exclude", "제외", "cdf-mode-exclude") +
    `<button type="button" class="btn-sm cdf-reset">초기화</button>` +
    `<span class="cdf-eb-count">제외 ${cdfExcluded.size}</span>` +
    (cdfEditMode !== "none"
      ? `<span class="cdf-eb-hint">점 클릭(단일) 또는 드래그 박스(다중) · 하단 Fail 표 체크박스도 가능</span>` : "");
}
function cdfToggleChip(key) {
  const set = cdfActiveSet();
  if (!set) return;
  if (set.has(key)) set.delete(key); else set.add(key);
}
// CDF x축 옵션 툴바(Excel 축옵션식): 경계(min/max) + 단위(기본/보조) + 적용/자동.
// 정적 마크업만 그림 — 입력 기본값은 렌더 직후 syncCdfAxisInputs 가 '현재 그려진' 축값으로 채운다.
function renderCdfAxisBar() {
  const bar = document.getElementById("cdfAxisBar");
  if (!bar) return;
  const num = k => `<input type="number" class="cdf-ax-in" data-cdf-ax="${k}" step="any">`;
  bar.innerHTML =
    `<span class="cdf-eb-label">CDF x축</span>` +
    `<span class="cdf-ax-grp">경계 ${num("min")} ~ ${num("max")}</span>` +
    `<span class="cdf-ax-grp">단위 기본 ${num("major")} 보조 ${num("minor")}</span>` +
    `<button type="button" class="btn-sm cdf-ax-apply" data-cdf-axis="apply">적용</button>` +
    `<button type="button" class="btn-sm cdf-ax-auto" data-cdf-axis="auto">자동</button>` +
    `<span class="cdf-ax-msg"></span>`;
}
// 렌더된 실제 x축(자동 계산 포함)을 입력칸 기본값으로 반영 — 자동 모드(override=null)에서만.
// 사용자가 값을 '적용'한 상태에서는 그 값이 이미 반영돼 있으므로 덮어쓰지 않는다.
function syncCdfAxisInputs(cdfDiv) {
  const bar = document.getElementById("cdfAxisBar");
  if (!bar || cdfAxisOverride) return;
  const ax = cdfDiv && cdfDiv._fullLayout && cdfDiv._fullLayout.xaxis;
  if (!ax || !ax.range) return;
  const fmt = v => (v == null ? "" : Number(v.toPrecision(6)));   // float 잡음 제거
  const set = (k, v) => { const el = bar.querySelector(`[data-cdf-ax="${k}"]`); if (el) el.value = fmt(v); };
  set("min", ax.range[0]); set("max", ax.range[1]);
  set("major", typeof ax.dtick === "number" ? ax.dtick : null);
  set("minor", null);   // 자동 모드는 보조 눈금 미표시 → 비움(사용자가 입력하면 그때 생성)
}
// 적용: 입력 4칸을 읽어 override 확정 후 CDF만 재렌더. 경계 검증 실패 시 인라인 메시지.
function cdfAxisApply() {
  const bar = document.getElementById("cdfAxisBar");
  if (!bar) return;
  const val = k => { const el = bar.querySelector(`[data-cdf-ax="${k}"]`); const v = el ? parseFloat(el.value) : NaN; return isFinite(v) ? v : null; };
  const min = val("min"), max = val("max"), major = val("major"), minor = val("minor");
  const msg = bar.querySelector(".cdf-ax-msg");
  if (min == null || max == null || min >= max) { if (msg) msg.textContent = "경계 최소<최대 확인"; return; }
  if (msg) msg.textContent = "";
  cdfAxisOverride = { min, max, major: (major > 0 ? major : null), minor: (minor > 0 ? minor : null) };
  if (_itemDetailData) distRenderCdf(_itemDetailData);   // CDF만 재렌더(override 반영)
}
// 자동: override 해제 후 재렌더 → syncCdfAxisInputs 가 현재값으로 입력칸 재기입.
function cdfAxisAuto() {
  cdfAxisOverride = null;
  if (_itemDetailData) distRenderCdf(_itemDetailData);
}
// 편집(제외/모드전환/초기화) 후 CDF·히스토그램·툴바·Fail표를 다시 그림.
// 히스토그램은 제외(cdfExcluded)를 반영하므로 함께 재렌더해야 초기화 시 원복된다. 통계표는 불변.
function cdfAfterEdit() {
  if (_itemDetailData) { distRenderCdf(_itemDetailData); distRenderHist(_itemDetailData); }
  renderCdfEditBar();
  if (_itemDetailData && _itemDetailData.is_fail) renderItemFailRows();
}
// CDF 만 렌더(제외→분모 재계산, 편집 모드→dragmode=select + 클릭/박스선택).
function distRenderCdf(data) {
  const cdfDiv = document.getElementById("distCdf");
  if (!cdfDiv) return;
  // 재렌더(제외/강조 편집) 시 이전 plot 을 해제 — scattergl 의 WebGL 컨텍스트 누적 방지
  // (SVG 에도 무해). newPlot 이 이어서 새로 초기화한다.
  if (cdfDiv.data) { try { Plotly.purge(cdfDiv); } catch (e) { /* no-op */ } }
  const useGl = !!DIST.CDF_GL;   // 렌더 방식 토글 — distribution.js DIST 상수 참조
  const lo = data.lower_limit, hi = data.upper_limit;
  const bg = DIST_STATUS_BG[data.status] || "#FFFFFF";
  const multi = (data.sources || []).length > 1;
  const unit = data.units || "";
  const xtitle = `측정값${unit ? " [" + unit + "]" : ""}`;
  const traces = (data.sources || []).map(s => {
    const hasId = Array.isArray(s.serial) && s.serial.length === s.values.length;
    // 제외 칩을 뺀 값/식별정보 — 제외는 CDF 곡선에만 반영(분모 n 감소로 곡선 재계산).
    let vals = s.values, serial = s.serial, xpos = s.xpos, ypos = s.ypos;
    if (hasId && cdfExcluded.size) {
      vals = []; serial = []; xpos = []; ypos = [];
      for (let i = 0; i < s.values.length; i++) {
        if (cdfExcluded.has(cdfChipKey(s.name, s.serial[i], s.xpos[i], s.ypos[i]))) continue;
        vals.push(s.values[i]); serial.push(s.serial[i]); xpos.push(s.xpos[i]); ypos.push(s.ypos[i]);
      }
    }
    const c = distCdfFromValues(vals);
    const base = distColorFor(s.name);
    const trace = { type: useGl ? "scattergl" : "scatter", mode: "markers", name: s.name, x: c.x, y: c.y };
    if (!useGl) trace.cliponaxis = false;   // scattergl 미지원 속성 — SVG 분기에만
    if (hasId) {
      // customdata/hover 는 필터·정렬된 동일 순서 유지(클릭 식별·hover 지속).
      trace.customdata = c.order.map(i => [serial[i], xpos[i], ypos[i]]);
      trace.hovertemplate = "측정값 %{x}<br>누적 %{y:.1f}%<br>SERIAL %{customdata[0]} · X %{customdata[1]} / Y %{customdata[2]}<extra></extra>";
      trace.marker = { color: base, size: 5 };
    } else {
      trace.marker = { color: base, size: 5 };
      trace.hovertemplate = "측정값 %{x}<br>누적 %{y:.1f}%<extra></extra>";
    }
    return trace;
  });
  // 선택 좌표(Map Analysis)가 있으면 이 항목 위치를 점+빨간 점선으로 오버레이.
  let cdfShapes = distSpecShapes(lo, hi, true).concat(beforeLimitShapes(data.subject));
  const cdfCm = chipMarkersFor(data.subject);
  if (cdfCm) { traces.push(...cdfCm.traces); cdfShapes = cdfShapes.concat(cdfCm.shapes); }
  const dragmode = cdfEditMode === "none" ? "zoom" : "select";
  const cdfLr = distLimitRange(lo, hi);
  // x축: 사용자 축옵션(경계/단위)이 있으면 우선, 없으면 기존 동작(distLimitOnly 창 → autorange).
  const ov = cdfAxisOverride;
  const xaxisCfg = { title: { text: xtitle }, showgrid: true, gridcolor: "#eee", zeroline: false };
  if (ov) {
    xaxisCfg.range = [ov.min, ov.max]; xaxisCfg.autorange = false;
    if (ov.major) { xaxisCfg.dtick = ov.major; xaxisCfg.tick0 = ov.min; } else xaxisCfg.nticks = 10;
    if (ov.minor) xaxisCfg.minor = { dtick: ov.minor, showgrid: true, gridcolor: "#f2f2f2", ticklen: 3 };
  } else {
    xaxisCfg.nticks = 10;
    if (cdfLr) { xaxisCfg.range = cdfLr; xaxisCfg.autorange = false; }
  }
  Plotly.newPlot(cdfDiv, traces, { ...DIST_PLOT_BG, plot_bgcolor: bg, dragmode,
    xaxis: xaxisCfg,
    yaxis: { title: { text: "누적 %" }, range: [-2, 102], tick0: 0, dtick: 20, ticksuffix: "%", showgrid: true, gridcolor: "#eee", zeroline: false },
    shapes: cdfShapes,
    annotations: distSpecAnnos(lo, hi, false).concat(beforeLimitAnnos(data.subject)),
    margin: { l: 60, r: 22, t: 16, b: 46 }, showlegend: multi && !distUseExtLegend(data) }, DIST_CFG);
  // 재렌더마다 중복 방지 후 편집 모드에서만 동작하는 선택 이벤트 바인딩.
  if (cdfDiv.removeAllListeners) { cdfDiv.removeAllListeners("plotly_click"); cdfDiv.removeAllListeners("plotly_selected"); }
  cdfDiv.on("plotly_click", ev => {
    if (!cdfActiveSet() || !ev.points || !ev.points.length) return;
    const pt = ev.points[0];
    if (!pt.customdata) return;
    cdfToggleChip(cdfChipKey(pt.data.name, pt.customdata[0], pt.customdata[1], pt.customdata[2]));
    cdfAfterEdit();
  });
  cdfDiv.on("plotly_selected", ev => {
    const set = cdfActiveSet();
    if (!set || !ev || !ev.points || !ev.points.length) return;
    ev.points.forEach(pt => { if (pt.customdata) set.add(cdfChipKey(pt.data.name, pt.customdata[0], pt.customdata[1], pt.customdata[2])); });
    cdfAfterEdit();
  });
  // 렌더된 실제 x축값을 축옵션 입력칸 기본값으로 반영(자동 모드에서만).
  syncCdfAxisInputs(cdfDiv);
  // 차트 주석 오버레이 — 렌더 시점의 shapes 개수를 base 로 기억해야 하므로 항상 마지막에.
  if (window.chartNotesApply) chartNotesApply("cdf", data.subject, cdfDiv);
}
// 히스토그램(빈도 폴리곤)만 렌더 — CDF '제외'(cdfExcluded)를 반영하므로 편집/초기화 때도 재호출.
function distRenderHist(data) {
  const hDiv = document.getElementById("distHist");
  if (!hDiv) return;
  const lo = data.lower_limit, hi = data.upper_limit;
  const bg = DIST_STATUS_BG[data.status] || "#FFFFFF";
  const multi = (data.sources || []).length > 1;
  const unit = data.units || "";
  const xtitle = `측정값${unit ? " [" + unit + "]" : ""}`;
  // 막대 대신 빈도 폴리곤: 21bin 중심점-빈도 곡선(양끝 0 패딩), CDF 와 동일한 원본 values 재사용.
  const polys = distHistPolygon(data.sources || [], lo, hi, cdfExcluded);
  let ymax = 0;
  polys.forEach(p => p.counts.forEach(c => { if (c > ymax) ymax = c; }));
  const traces = polys.map(p => ({ type: "scatter", mode: "lines", name: p.source,
    x: p.centers, y: p.counts, line: { color: distColorFor(p.source), shape: "spline" },
    hovertemplate: "측정값 %{x}<br>빈도 %{y:d}<extra></extra>" }));
  Plotly.newPlot(hDiv, traces, { ...DIST_PLOT_BG, plot_bgcolor: bg,
    xaxis: { title: { text: xtitle },
      range: distLimitRange(lo, hi) || extendRangeForBeforeLimits(
        distHistXRange(data.sources || [], lo, hi, data.is_fail), data.subject),
      showgrid: true, gridcolor: "#eee", zeroline: false },
    yaxis: { title: { text: "빈도" }, range: [0, (ymax || 1) * 1.1], tickformat: "d",
      showgrid: true, gridcolor: "#eee", zeroline: false },
    shapes: distSpecShapes(lo, hi, false).concat(beforeLimitShapes(data.subject)),
    annotations: distSpecAnnos(lo, hi, false).concat(beforeLimitAnnos(data.subject)),
    margin: { l: 60, r: 22, t: 16, b: 46 }, showlegend: multi && !distUseExtLegend(data) }, DIST_CFG);
  // 차트 주석 오버레이 (chart_notes.js) — base shapes 기억을 위해 렌더 직후 호출.
  if (window.chartNotesApply) chartNotesApply("hist", data.subject, hDiv);
}
function distRenderDetailCharts(data) {
  distRenderCdf(data);    // #distCdf (제외 편집 반영)
  distRenderHist(data);   // #distHist (제외 반영)
}
// report용 정규분포 곡선(#distNormal): bin/막대 없이 source별 μ/σ 로 계산한 매끄러운
// 가우시안 PDF 곡선. degenerate(n<2 or std<=0, 서버 표시) source 는 곡선 대신 x=μ 세로
// 스파이크(shape). x축은 히스토그램 1단계 규칙(distHistXRange) + 2단계 ±5% 이중 마진.
function distRenderNormal(data) {
  const nDiv = document.getElementById("distNormal");
  if (!nDiv) return;
  const lo = data.lower_limit, hi = data.upper_limit;
  const bg = data.is_fail ? "#FEF9E7" : "#FFFFFF";   // 규격 이탈(Fail) → 배경 연노랑
  const multi = (data.sources || []).length > 1;
  const unit = data.units || "";
  const xtitle = `측정값${unit ? " [" + unit + "]" : ""}`;
  const statByName = {};
  (data.stats || []).forEach(s => { statByName[s.source] = s; });
  const traces = [], spikes = [];
  let ymax = 0;
  (data.sources || []).forEach(s => {
    const st = statByName[s.name];
    if (!st) return;
    const color = distColorFor(s.name), mean = st.average, std = st.stdev;
    if (st.degenerate || mean === null || mean === undefined) {
      // 축퇴 케이스: x=μ 세로 스파이크(축 전체 높이, paper 기준).
      if (mean !== null && mean !== undefined) spikes.push({
        type: "line", x0: mean, x1: mean, yref: "paper", y0: 0, y1: 1,
        line: { color, width: 1.4 } });
      return;
    }
    // 정상 케이스: μ±4σ 256점, 정규분포 PDF (명세 공식).
    const N = 256, xstart = mean - 4 * std, dx = (8 * std) / (N - 1);
    const coef = 1 / (std * Math.sqrt(2 * Math.PI));
    const xs = new Array(N), ys = new Array(N);
    for (let i = 0; i < N; i++) {
      const x = xstart + dx * i, z = (x - mean) / std;
      xs[i] = x; ys[i] = coef * Math.exp(-0.5 * z * z);
    }
    if (coef > ymax) ymax = coef;   // PDF 최대값은 x=μ 의 coef
    traces.push({ type: "scatter", mode: "lines", name: s.name, x: xs, y: ys,
      hoverinfo: "skip", line: { color, width: 1.4 } });
  });
  // x축: 1단계(distHistXRange, bin 버전과 동일) → 2단계(±5% 항상 적용, 이중 마진).
  let range = distHistXRange(data.sources || [], lo, hi, data.is_fail);
  if (range) {
    const span = range[1] - range[0];
    range = extendRangeForBeforeLimits([range[0] - span * 0.05, range[1] + span * 0.05], data.subject);
  }
  const normLr = distLimitRange(lo, hi);
  if (normLr) range = normLr;   // Limit 안 Data만 보기
  Plotly.newPlot(nDiv, traces, { ...DIST_PLOT_BG, plot_bgcolor: bg,
    xaxis: { title: { text: xtitle }, range, showgrid: false, zeroline: false },
    yaxis: { range: [0, ymax > 0 ? ymax * 1.1 : 1],
      showticklabels: false, showgrid: false, zeroline: false },
    shapes: distSpecShapes(lo, hi, false).concat(spikes, beforeLimitShapes(data.subject)),
    annotations: distSpecAnnos(lo, hi, false).concat(beforeLimitAnnos(data.subject)),
    margin: { l: 24, r: 22, t: 16, b: 46 }, showlegend: multi && !distUseExtLegend(data) }, DIST_CFG);
}
// 히스토그램 블록 탭 전환(Analysis 폴리곤 ↔ Report 정규분포). report 는 처음 볼 때만 렌더.
function setIdetHistMode(mode) {
  if (mode !== "analysis" && mode !== "report") return;
  idetHistMode = mode;
  const dp = document.getElementById("panel-item-detail");
  if (!dp) return;
  dp.querySelectorAll(".idet-hist-mode").forEach(b =>
    b.classList.toggle("active", b.dataset.histMode === mode));
  const hDiv = document.getElementById("distHist"), nDiv = document.getElementById("distNormal");
  if (hDiv) hDiv.style.display = (mode === "analysis") ? "" : "none";
  if (nDiv) nDiv.style.display = (mode === "report") ? "" : "none";
  if (mode === "report" && nDiv && !_idetNormalRendered && _itemDetailData) {
    distRenderNormal(_itemDetailData);   // display 켠 뒤 렌더(크기 0 방지)
    _idetNormalRendered = true;
  }
}
// 갤러리 재렌더(툴바가 새로 그려짐) 후 검색 상태(입력값·제안목록·포커스)를 복원한다.
// 체크박스를 여러 개 연속으로 고를 수 있게 하기 위함.
function restoreDistSearch(q) {
  const inp = document.getElementById("distSearch");
  if (!inp) return;
  inp.value = q || "";
  if (q) { inp.focus(); distRenderSuggest(q); }
}
function distRenderSuggest(q) {
  const box = document.getElementById("distSuggest");
  if (!box) return;
  const items = distSuggestions(q);
  if (!String(q).trim() || !items.length) { box.innerHTML = ""; box.style.display = "none"; return; }
  // 체크박스로 다중 선택 → 선택 항목만 갤러리에 표시. cpk 값은 표시하지 않는다.
  box.innerHTML = items.map(r =>
    `<label class="dist-sug-item">
      <input type="checkbox" class="dist-sug-chk" data-subject="${esc(r.subject)}"${distSelected.has(r.subject) ? " checked" : ""}>
      <span class="sug-tno">${esc(r.test_num || "")}</span>
      <span class="sug-name">${esc(r.subject)}</span>
    </label>`).join("");
  box.style.display = "block";
}

// ── 패널 이벤트 위임(1회 바인딩) ──────────────────────────────────────────────
function distBindPanel() {
  if (distPanelBound) return;
  const panel = document.getElementById("panel-distribution");
  if (!panel) return;
  panel.addEventListener("click", e => {
    const seg = e.target.closest(".distseg");
    if (seg) {
      if (seg.dataset.seg === "clearsel") distSelected.clear();
      else if (seg.dataset.seg === "cpk") distCpkOnly = !distCpkOnly;
      else if (seg.dataset.seg === "fail") distFailOnly = !distFailOnly;
      else if (seg.dataset.seg === "limit") distLimitOnly = !distLimitOnly;
      else if (seg.dataset.seg === "nopf") distHidePassfail = !distHidePassfail;
      else if (seg.dataset.seg === "bin1") { distBin1Only = !distBin1Only; if (distBin1Only) ensureDistBin1Data(); }
      const q = (document.getElementById("distSearch") || {}).value || "";
      distRenderGallery();
      restoreDistSearch(q);
      return;
    }
    const card = e.target.closest(".distg-card");
    if (card) { openItemDetail(card.dataset.subject, distFiltered.map(r => r.subject)); return; }
  });
  // 검색 제안 체크박스 토글 → 선택 집합 갱신 후 갤러리를 선택 항목만으로 필터(검색상태 복원).
  panel.addEventListener("change", e => {
    const chk = e.target.closest(".dist-sug-chk");
    if (!chk) return;
    if (chk.checked) distSelected.add(chk.dataset.subject);
    else distSelected.delete(chk.dataset.subject);
    const q = (document.getElementById("distSearch") || {}).value || "";
    distRenderGallery();
    restoreDistSearch(q);
  });
  panel.addEventListener("input", e => {
    if (e.target.id === "distSearch") distRenderSuggest(e.target.value);
  });
  distPanelBound = true;
}

function renderDistribution() {
  const panel = document.getElementById("panel-distribution");
  distIndex = (DATA.web_report && DATA.web_report.distribution_index) || [];
  buildDistColorMap((DATA.web_report && DATA.web_report.sources) || []);
  if (!distIndex.length) {
    if (distGalleryObserver) { try { distGalleryObserver.disconnect(); } catch (e) {} distGalleryObserver = null; }
    emptyPanel(panel, "Distribution 데이터 없음");
    return;
  }
  distBindPanel();
  distRenderGallery();
}

// 표 셀 안에 들어가는 작은 미리보기 차트(정적, 상호작용 없음) — Issue Table
// Distribution/CPK 열에서 해당 행 Item 의 ECDF 를 산포탭 갤러리 카드와 같은 포맷(표시용
// 다운샘플 static CDF + LSL/USL 점선)으로 축소해 보여주는 용도. 전체점은 데이터에 그대로 유지.
function renderMiniDistCell(cell) {
  if (cell.dataset.distLoaded === "1") return;
  const subject = cell.dataset.subject;
  const div = cell.querySelector(".dist-plot");
  if (!div || typeof Plotly === "undefined") return;
  // 분포 데이터 도착 전 — 플래그를 세우지 않고 리턴해야 도착 후 재큐잉으로 그려진다.
  if (!distDataReady) return;
  const info = distDataCache[subject];
  // 데이터 없는 항목: 빈 칸으로 확정 (loaded 마킹해 재큐잉 no-op 방지)
  if (!info) { cell.innerHTML = ""; cell.dataset.distLoaded = "1"; return; }

  const lo = info.lower_limit, hi = info.upper_limit;
  const traces = Object.keys(info.bySource).map(source => {
    // markers 전용(선 금지 — CLAUDE.md §5). 세로 점 보간으로 이산값 성김을 보정.
    const ds = distPointsForDisplay(info.bySource[source].xs, info.bySource[source].ys);
    return { type: "scatter", mode: "markers", cliponaxis: false, name: source,
      x: ds.xs, y: ds.ys, marker: { color: distColorFor(source), size: 3 } };
  });
  // x축을 데이터 범위로 고정한다. autorange 로 두면 LSL/USL 스펙선 shapes(데이터 범위 밖일
  // 수 있음)까지 x-autorange 에 포함돼 x축이 늘어나 곡선이 한쪽에 뭉쳐 잘린 것처럼 보인다.
  // xs 는 ECDF 라 오름차순(distDownsampleForDisplay 전제) → 양끝값으로 min/max 를 O(1) 로 잡음.
  let xMin = Infinity, xMax = -Infinity;
  Object.keys(info.bySource).forEach(source => {
    const xs = info.bySource[source].xs;
    if (xs && xs.length) {
      if (xs[0] < xMin) xMin = xs[0];
      if (xs[xs.length - 1] > xMax) xMax = xs[xs.length - 1];
    }
  });
  // 데이터 범위에 LSL/USL 도 포함시켜(경계 산포·스펙선이 짤리지 않게) ±5% 가드밴드를 준다.
  if (lo !== null && lo !== undefined && lo < xMin) xMin = lo;
  if (hi !== null && hi !== undefined && hi > xMax) xMax = hi;
  let xaxis = { visible: false };
  if (xMin !== Infinity && xMax !== -Infinity) {
    const gb = (xMax > xMin) ? (xMax - xMin) * 0.05 : (Math.abs(xMin) * 0.05 || 1);
    xaxis = { visible: false, autorange: false, range: [xMin - gb, xMax + gb] };
  }
  const layout = {
    xaxis, yaxis: { visible: false, range: [0, 100] },
    shapes: distSpecShapes(lo, hi, false).concat(beforeLimitShapes(subject)),
    margin: { l: 1, r: 1, t: 1, b: 1 },
    paper_bgcolor: "transparent", plot_bgcolor: "transparent", showlegend: false,
  };
  Plotly.newPlot(div, traces, layout, DIST_CFG_STATIC);
  cell.dataset.distLoaded = "1";
}

// Issue Table 미니셀은 수천 개(항목 수 규모)라 전량 동기 렌더 시 메인스레드가 분 단위로
// 얼어붙는다 — 갤러리와 같은 IntersectionObserver + rAF 분할(프레임당 3개) lazy 렌더를 쓰고,
// 화면 밖으로 나가면 purge 해 plot DOM 상주를 막는다. 큐는 갤러리(distRenderQueue)와
// 분리 — 갤러리 재렌더가 큐를 초기화해도 issue 셀이 유실되지 않도록.
let issueDistObserver = null;
let issueDistQueue = [];
let issueDistRafScheduled = false;

function issueDistQueueRender(cell) {
  if (cell.dataset.distLoaded === "1" || issueDistQueue.includes(cell)) return;
  issueDistQueue.push(cell);
  if (!issueDistRafScheduled) { issueDistRafScheduled = true; requestAnimationFrame(issueDistFlush); }
}
function issueDistFlush() {
  issueDistRafScheduled = false;
  let n = 0;
  while (issueDistQueue.length && n < DIST.PER_FRAME) {
    const cell = issueDistQueue.shift();
    if (cell.isConnected && cell.dataset.visible === "1") { renderMiniDistCell(cell); n++; }
  }
  if (issueDistQueue.length) { issueDistRafScheduled = true; requestAnimationFrame(issueDistFlush); }
}
function issueDistPurge(cell) {
  if (cell.dataset.distLoaded !== "1") return;
  const div = cell.querySelector(".dist-plot");
  if (!div) return;   // 데이터 없음으로 비워진 셀 — 확정 상태 유지
  try { if (window.Plotly) Plotly.purge(div); } catch (e) {}
  cell.dataset.distLoaded = "";
}

function renderIssueMiniDist(panel) {
  if (issueDistObserver) { try { issueDistObserver.disconnect(); } catch (e) {} issueDistObserver = null; }
  issueDistQueue = []; issueDistRafScheduled = false;
  const cells = panel.querySelectorAll(".dist-cell-mini");
  if (!cells.length) return;
  if (typeof IntersectionObserver === "undefined") {
    cells.forEach(cell => renderMiniDistCell(cell));   // 구형 브라우저 폴백: 기존 동작
    return;
  }
  issueDistObserver = new IntersectionObserver(entries => {
    entries.forEach(en => {
      const cell = en.target;
      if (en.isIntersecting) { cell.dataset.visible = "1"; issueDistQueueRender(cell); }
      else {
        cell.dataset.visible = "";
        issueDistPurge(cell);
        const i = issueDistQueue.indexOf(cell);
        if (i >= 0) issueDistQueue.splice(i, 1);
      }
    });
  }, { rootMargin: "600px 0px", threshold: 0 });
  cells.forEach(c => issueDistObserver.observe(c));
}


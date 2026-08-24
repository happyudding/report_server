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
// 적용 시 {min, max, major|null, minorDiv|null} — minorDiv 는 "기본 단위를 몇 등분할지"의
// 정수(n≥2)라 보조 눈금 간격은 항상 major/n 이다(기본 단위보다 커져 안 보이는 일이 없다).
let cdfAxisOverride = null;         // null=자동(autorange)
let histAxisOverride = null;        // 히스토그램 x축 옵션(CDF 와 독립). 항목 이동 시 초기화.
// 격자 색 — 종전 #eee/#f2f2f2 는 흰 배경·status 배경색 위에서 사실상 보이지 않았다.
const IDET_GRID_MAJOR = "#b9c0cc";
const IDET_GRID_MINOR = "#dbe0e8";
// 축옵션 바를 차트별로 굴리기 위한 디스패치 — 바 요소 id, 라벨, override 접근자, 재렌더 함수.
// CDF 는 기존 동작 그대로이고 hist 만 추가된다. (y축이 필요해지면 axis 필드를 얹으면 된다.)
// 바가 각 차트 블록 안(차트 바로 아래)에 있어 라벨은 "x축" 만으로 어느 차트인지 자명하다.
const IDET_AXIS = {
  cdf:  { bar: "cdfAxisBar",  label: "x축",
          get: () => cdfAxisOverride,  set: v => { cdfAxisOverride = v; },
          render: () => { if (_itemDetailData) distRenderCdf(_itemDetailData); } },
  hist: { bar: "histAxisBar", label: "x축",
          get: () => histAxisOverride, set: v => { histAxisOverride = v; },
          render: () => { if (_itemDetailData) distRenderHist(_itemDetailData); } },
};
// 칩(die) 고유 식별키 — SERIAL 이 die 간 중복될 수 있어 XPOS/YPOS 까지 포함해야
// 드래그/클릭 제외가 정확히 그 die 만 겨냥한다(serial 단독이면 같은 serial 전량 오제외).
function cdfChipKey(source, serial, xpos, ypos) {
  return `${source}||${serial}||${xpos == null ? "" : xpos}||${ypos == null ? "" : ypos}`;
}
function cdfActiveSet() { return cdfEditMode === "exclude" ? cdfExcluded : null; }
function cdfResetEdits() { cdfExcluded.clear(); cdfEditMode = "none"; }

// opts.url 을 주면 /scatter 대신 그 URL 로 데이터를 받는다 (Gap Chart 가 이 화면을
// 그대로 재사용한다 — 서버 응답 구조가 같다).
// ⚠️ **기본값을 시그니처에 박은 이유**: `_itemDetailOpts = opts;` 를 무조건 실행해야 한다.
// `if (opts)` 처럼 조건부로 대입하면 gap 상세를 본 뒤 일반 항목을 2인자로 열 때 이전 URL 이
// 남아 일반 항목이 gap 라우트로 조회된다(에러 없이 조용히 깨진다).
let _itemDetailOpts = null;
function openItemDetail(subject, navList, opts = null) {
  const dp = document.getElementById("panel-item-detail");
  if (!dp) return;
  // 상세는 Plotly 로 그린다. plotly.min.js 는 async 로드라(첫 화면을 막지 않기 위함)
  // 표에서 곧바로 항목을 클릭하면 아직 도착 전일 수 있다 — 도착 후 다시 연다.
  // 이 대기는 종전엔 화면에 아무것도 내지 않아 "링크를 눌러도 반응이 없다"로 보였다
  // (2026-08-20 신고 — AI Comment 과거사례 안 @링크). 대기 중임을 토스트로 알린다.
  if (!window.Plotly && window.__plotlyReady) {
    showToast("차트 모듈을 불러오는 중입니다 — 잠시 후 자동으로 열립니다.");
    window.__plotlyReady.then(() => openItemDetail(subject, navList));
    return;
  }
  // 미저장 차트 주석(원/화살표 등)은 항목 이동 전에 flush — purge 전이라 도형 회수 가능.
  // 실패해도 _cnPending/_cnDirty 는 key 별로 남아 다음 autoSave/beforeunload 가 재시도.
  if (_cnDirty.size) cnFlush().catch(e => showToast("차트 Comment 자동저장 실패: " + e.message));
  bindItemDetailPanel();
  // 상세가 아직 안 열려 있으면 현재 활성 탭 패널을 복귀 대상으로 기억하고 숨긴다.
  if (!dp.classList.contains("active")) {
    const cur = document.querySelector(".content > .panel.active");
    _itemDetailReturnId = cur ? cur.id : "panel-summary";
    if (cur) cur.classList.remove("active");
    dp.classList.add("active");
  }
  _itemDetailSubject = subject;
  _itemDetailOpts = opts;          // 무조건 대입 (위 주석 — 조건부로 바꾸지 말 것)
  _itemDetailNav = Array.isArray(navList) && navList.length ? navList : [subject];
  _itemDetailFailPage = 1;
  cdfResetEdits();   // 항목이 바뀌면 CDF 제외 편집 초기화
  cdfAxisOverride = null;   // 항목이 바뀌면 CDF x축 옵션(경계/단위)도 자동으로 되돌림
  histAxisOverride = null;  // 히스토그램 x축 옵션도 동일
  _itemDetailData = null;
  const reqId = ++_itemDetailReq;
  window.scrollTo(0, 0);
  purgeItemDetailCharts();   // 항목 이동 시 이전 차트(WebGL 컨텍스트) 해제 후 갈아끼움
  dp.innerHTML = `<div class="idet"><div class="idet-head"><button class="btn-sm idet-back">← Back</button>` +
    `<span class="idet-title"><b>${esc(subject)}</b></span></div><div class="placeholder">로드 중…</div></div>`;
  // Bin1 계열 토글이 켜져 있으면 상세도 같은 기준의 분포/통계를 받는다(?bin1=1[&bin1_scope=rt]).
  // cache 옵션 없음(기본) — 서버 ETag 조건부 응답으로 재클릭·재방문 시 304 재검증된다.
  const scatterVariantQ = distVariantQuery(distGalleryVariant()).replace(/^&/, "?");
  const scatterUrl = (opts && opts.url)
    ? opts.url + scatterVariantQ
    : `/pe/report/session/${SESSION_ID}/web_report/scatter/${encodeURIComponent(subject)}`
      + scatterVariantQ;
  // 콜드(서버 tables 미적재) 세션은 202 가 온다 — 백그라운드 웜업이 끝날 때까지
  // fetchJson202(core.js)가 백오프 재시도한다. 항목 이동 시(reqId 변경) 재시도 중단.
  fetchJson202(scatterUrl, {
    shouldStop: () => reqId !== _itemDetailReq,
    onWaiting: () => {
      if (reqId !== _itemDetailReq) return;
      const ph = dp.querySelector(".placeholder");
      if (ph) ph.textContent = "데이터 준비 중… (자동 재시도)";
    },
  })
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
  // stdev 만 서버가 반올림하지 않는다 → 표시 시 유효숫자를 맞춘다(core.js fmtStdev).
  const fmtSd = v => (v === null || v === undefined || v === "") ? "-" : fmtStdev(v);
  const src = stats.length > 1 ? `<span class="idet-stat-src">${esc(s.source || "")}</span>` : "";
  return `<span class="idet-stat">${src}` +
    `min <b>${esc(fmt4(s.min))}</b> · max <b>${esc(fmt4(s.max))}</b> · ` +
    `avg <b>${esc(fmt4(s.average))}</b> · σ <b>${esc(fmtSd(s.stdev))}</b></span>`;
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

// 상세 차트 **우측 세로 칸** 공용 legend — 항상 렌더한다(갤러리와 같은 distLegendHtml
// 재사용, 순서는 data.sources 순서). Plotly 내장 legend 는 상세 3개 차트(CDF/히스토그램/
// 정규분포) 모두에서 끈다(2026-08-19 사용자 요청) — 내장 legend 클릭·강조 해제가 차트를
// 접거나 덮는 문제가 있어, 강조 선택은 Distribution 갤러리와 동일하게 우측 칸에서만 한다.
// 배치는 Distribution 갤러리와 동일한 세로 규격(DIST_LEGEND_VERT_CLS).
function idetLegendHtml(data) {
  return distLegendHtml((data && data.sources) || [], "idet-legend " + DIST_LEGEND_VERT_CLS);
}

// 갤러리 툴바의 표시 옵션을 상세 안에서도 제공 — "Limit 안 Data만"(축 클램프)과
// "Bin1 only"(양품·규격내 재계산). Temperature 모드는 갤러리와 같은 규칙으로 Bin1 only
// 대신 "Bin1 (RT만)" 하나만 낸다(2026-08-07 Bin1(RT만) → 2026-08-14 Limit·Bin1 추가).
// 갤러리와 **같은 전역 상태**(distLimitOnly / distBin1Only / distRtBin1Only)를 공유하므로
// 어느 쪽에서 켜도 다른 쪽 버튼이 같은 상태로 보인다. 클릭 처리는 bindItemDetailPanel 위임.
function idetOptsHtml() {
  const limitBtn = `<button class="distseg${distLimitOnly ? " active" : ""}" data-idet-seg="limit" ` +
    `title="켜짐: x축을 Limit(LSL~USL) 범위로 고정 · 꺼짐: 데이터 전 범위">Limit 안 Data만</button>`;
  const bin1Btn = tempIsMode()
    ? `<button class="distseg${distRtBin1Only ? " active" : ""}" data-idet-seg="rtbin1" ` +
      `title="켜짐: RT source 만 양품(Bin1)·규격내로 좁히고 CT / HT 는 fail 포함 전체 die 로 표시 · 꺼짐: 전체 die">Bin1 (RT만)</button>`
    : `<button class="distseg${distBin1Only ? " active" : ""}" data-idet-seg="bin1" ` +
      `title="켜짐: 양품(Bin1, BIN==1) & 규격(LSL/USL) 이내 die 측정값만으로 재계산해 표시 · 꺼짐: 전체 die">Bin1 only</button>`;
  // Serial 순 — 갤러리 툴바 맨 앞 버튼과 **같은 전역 상태**(distSeqOnly)를 공유한다.
  // 상세는 /scatter 응답이 이미 rawdata 행 순서라 **데이터를 다시 받지 않는다**(차트만 재렌더).
  const seqBtn = `<button class="distseg${distSeqOnly ? " active" : ""}" data-idet-seg="seq" ` +
    `title="켜짐: 이 항목을 각 source 의 rawdata 순서(Serial 순)로 x=측정 순서 · y=측정값 표시 · 꺼짐: 누적분포 CDF">Serial 순</button>`;
  return `<div class="distseg-group idet-opts">${seqBtn}${limitBtn}${bin1Btn}</div>`;
}

// CDF 자리 차트의 제목 — Serial 순 모드에서는 같은 자리에 run chart 를 그리므로 문구도 바뀐다.
function idetCdfCapText() {
  return distSeqOnly ? "Serial 순 (측정 순서 · rawdata 누적 순)" : "누적분포 CDF";
}
function idetSyncCdfCaption() {
  const el = document.getElementById("cdfCapLabel");
  if (el) el.textContent = idetCdfCapText();
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
  // Limit·단위가 모두 없으면 빈 괄호 "()" 만 남으므로 헤더에서 통째로 뺀다.
  const limInner = distLimInnerHtml(data.lower_limit, data.upper_limit, data.units);
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
        ${limInner ? `<span class="idet-lim">(${limInner})</span>` : ""}
        ${idetHeaderStats(data.stats)}
      </span>
    </div>
    ${(typeof gcFormulaBarHtml === "function") ? gcFormulaBarHtml(data) : ""}
    <div id="cdfEditBar" class="cdf-editbar"></div>
    <div id="chartNoteBar"></div>
    ${distTempFilterHtml()}${idetOptsHtml()}
    <div class="idet-body">
    <div class="idet-charts">
      <div class="idet-chart-block">
        <div class="dist-chart-cap idet-hist-cap">
          <span id="cdfCapLabel">${esc(idetCdfCapText())}</span>
          <button type="button" class="btn-sm idet-png" data-idet-png="cdf" title="지금 보이는 CDF 차트를 PNG 로 클립보드에 복사 (클립보드 차단 시 PNG 다운로드)">클립보드로 복사</button>
        </div>
        <div id="distCdf" class="dist-chart"></div>
        <div id="cdfAxisBar" class="cdf-axisbar"></div>
        <div class="idet-chart-comment" id="cdfCommentView"></div></div>
      <div class="idet-chart-block">
        <div class="dist-chart-cap idet-hist-cap">
          <span>분포 히스토그램</span>
          <span class="idet-cap-right">
            <span class="idet-hist-tabs">
              <button type="button" class="btn-sm idet-hist-mode${idetHistMode === "analysis" ? " active" : ""}" data-hist-mode="analysis">Analysis</button>
              <button type="button" class="btn-sm idet-hist-mode${idetHistMode === "report" ? " active" : ""}" data-hist-mode="report">Report</button>
            </span>
            <button type="button" class="btn-sm idet-png" data-idet-png="hist" title="지금 보이는 히스토그램(Analysis/Report)을 PNG 로 클립보드에 복사 (클립보드 차단 시 PNG 다운로드)">클립보드로 복사</button>
          </span>
        </div>
        <div id="distHist" class="dist-chart"${idetHistMode === "report" ? ' style="display:none"' : ""}></div>
        <div id="distNormal" class="dist-chart"${idetHistMode === "analysis" ? ' style="display:none"' : ""}></div>
        <div id="histAxisBar" class="cdf-axisbar"></div>
        <div class="idet-chart-comment" id="histCommentView"></div>
      </div>
    </div>
    <aside class="dist-legend-side idet-legend-side">${idetLegendHtml(data)}</aside>
    </div>
    ${itemStatsTableHtml(data.stats)}
    <div id="idetChipVals"></div>
    ${failTitle}
  </div>`;
  renderCdfEditBar();
  renderIdetAxisBar("cdf");
  renderIdetAxisBar("hist");
  distRenderDetailCharts(data);   // #distCdf / #distHist (기존 함수 재사용)
  if (window.chartNotesBar) chartNotesBar(data);   // 차트 주석 툴바 (chart_notes.js)
  if (window.cnRenderChartComments) {   // 차트 하단 Comment 표시 (gap 은 gap:<uuid> 키)
    cnRenderChartComments(window.cnSubjectOf ? cnSubjectOf(data) : subject);
  }
  renderIdetChipVals();           // Map Analysis 선택 좌표의 이 항목 값
  if (data.is_fail) renderItemFailRows();
}

// Map Analysis 에서 선택한 좌표(mapSelChips)의 '현재 항목' 측정값·누적% 표. 좌표 변경 시에도 갱신.
// Gap Chart 상세는 파생값이라 chip.items 에 값이 없다 — distRenderCdf 가 곡선을 그리며
// 모아둔 값(_idetGapChipHits)을 쓴다. 차트 마커와 **같은 배열**에서 나온 값이라 어긋나지 않는다.
let _idetGapChipHits = [];
function idetChipValuesHtml(subject) {
  if (!mapSelChips.length) return "";
  const gap = !!(_itemDetailData && _itemDetailData.is_gap);
  const rows = mapSelChips.map(c => {
    const hit = gap ? _idetGapChipHits.find(h => h.chip === c) : null;
    const it = gap ? (hit ? { value: hit.value, cum_pct: hit.cum } : null) : c.items[subject];
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
      const v = s[c];
      // stdev 만 서버 무반올림 → 표시용 유효숫자 포맷. 곡선(가우시안 PDF)은 원값을 계속 쓴다.
      const t = (v === null || v === undefined) ? "" : (c === "stdev" ? fmtStdev(v) : String(v));
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

// ── 차트 PNG 클립보드 복사 (CDF / 히스토그램) ───────────────────────────────
// 히스토그램 블록은 Analysis(#distHist)·Report(#distNormal) 두 차트를 번갈아 감추므로
// "지금 보이는 쪽"을 복사한다. 폴백 3단: navigator.clipboard → execCommand → PNG 다운로드.
// 2단계가 실질 주경로다 — 운영 서버가 http(LAN) 라 **비보안 컨텍스트**여서
// navigator.clipboard 자체가 없고, 그래서 종전에는 누를 때마다 파일 다운로드로 빠졌다
// (Issue Table 셀 복사가 execCommand 폴백을 필수로 두는 것과 같은 이유).
function idetExecCopyImage(dataUrl) {
  // contenteditable 안의 <img> 를 선택해 execCommand("copy") — Chromium 계열은 이미지가
  // 담긴 HTML 을 클립보드에 넣어 Excel/PPT/Word 붙여넣기에서 그림으로 들어간다.
  // 클릭에서 여기까지 오는 사이 PNG 를 굽기 때문에 브라우저의 임시 사용자 활성화(~5초)가
  // 만료되면 false 가 돌아온다 — 그때는 호출부가 종전처럼 파일 다운로드로 떨어진다.
  return new Promise(resolve => {
    const host = document.createElement("div");
    host.setAttribute("contenteditable", "true");
    host.style.cssText = "position:fixed;left:-10000px;top:0;opacity:0;user-select:text";
    const img = new Image();
    const finish = () => {
      let ok = false;
      try {
        host.appendChild(img);
        document.body.appendChild(host);
        const range = document.createRange();
        range.selectNodeContents(host);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        ok = document.execCommand("copy");
        sel.removeAllRanges();
      } catch (e) { ok = false; }
      host.remove();
      resolve(ok);
    };
    img.onload = finish;
    img.onerror = () => resolve(false);
    img.src = dataUrl;
  });
}
async function idetCopyChartPng(kind) {
  const id = kind === "cdf" ? "distCdf" : (idetHistMode === "report" ? "distNormal" : "distHist");
  const gd = document.getElementById(id);
  if (!gd || !gd.data) { showToast("차트가 아직 로드되지 않았습니다"); return; }
  // 화면에 그려진 크기 그대로 2배 해상도로 — 축 범위·제외 편집 등 현재 뷰가 그대로 담긴다.
  const w = Math.round(gd.clientWidth || 900), h = Math.round(gd.clientHeight || 420);
  let url = null;
  try {
    url = await Plotly.toImage(gd, { format: "png", width: w, height: h, scale: 2 });
  } catch (e) {
    showToast("PNG 생성 실패: " + e.message);
    return;
  }
  try {
    if (!window.isSecureContext || !navigator.clipboard || !navigator.clipboard.write
        || typeof ClipboardItem === "undefined") throw new Error("clipboard unavailable");
    const blob = await (await fetch(url)).blob();
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    showToast("차트를 클립보드에 복사했습니다");
    return;
  } catch (e) { /* execCommand 폴백으로 */ }
  if (await idetExecCopyImage(url)) {
    showToast("차트를 클립보드에 복사했습니다 (붙여넣기: Ctrl+V)");
    return;
  }
  const a = document.createElement("a");
  a.href = url;
  a.download = `${String(_itemDetailSubject || "item").replace(/[\\/:*?"<>|]/g, "_")}_${id}.png`;
  document.body.appendChild(a); a.click(); a.remove();
  showToast("클립보드 복사 불가 — PNG 파일로 다운로드했습니다");
}

function closeItemDetail() {
  const dp = document.getElementById("panel-item-detail");
  if (!dp) return;
  // 미저장 차트 주석은 상세를 닫기 전에 flush (purge 전 — openItemDetail 과 동일 이유).
  if (_cnDirty.size) cnFlush().catch(e => showToast("차트 Comment 자동저장 실패: " + e.message));
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
  // 미저장 차트 주석은 탭 전환으로 상세가 사라지기 전에 flush.
  if (_cnDirty.size) cnFlush().catch(e => showToast("차트 Comment 자동저장 실패: " + e.message));
  const dp = document.getElementById("panel-item-detail");
  if (dp && dp.classList.contains("active")) { _itemDetailReq++; dp.classList.remove("active"); purgeItemDetailCharts(); dp.innerHTML = ""; }
  _itemDetailReturnId = null;
}

function bindItemDetailPanel() {
  if (_itemDetailBound) return;
  const dp = document.getElementById("panel-item-detail");
  if (!dp) return;
  dp.addEventListener("click", e => {
    if (distLegendClick(e)) return;   // 범례 클릭 → source 강조
    if (e.target.closest(".idet-back")) { closeItemDetail(); return; }
    if (e.target.closest(".idet-prev")) { itemDetailNav(-1); return; }
    if (e.target.closest(".idet-next")) { itemDetailNav(1); return; }
    // 표시 옵션 토글(Limit 안 Data만 / Bin1 only / Bin1 (RT만)) — 갤러리와 같은 전역
    // 상태를 바꾼다. 갤러리 툴바가 이미 그려져 있으면 재렌더해 버튼 상태를 맞춘다
    // (숨겨진 패널이라 실제 카드 렌더는 다시 보일 때 IntersectionObserver 가 지연 수행).
    const iseg = e.target.closest("[data-idet-seg]");
    if (iseg) {
      const kind = iseg.dataset.idetSeg;
      if (kind === "limit") {
        // 축 범위만 바뀌는 옵션이라 데이터를 다시 받지 않는다 — 상세 차트만 다시 그린다
        // (Report 탭은 이미 그려진 적 있을 때만; 안 그러면 숨김 상태에서 크기 0 으로 그려진다).
        distLimitOnly = !distLimitOnly;
        iseg.classList.toggle("active", distLimitOnly);
        if (_itemDetailData) {
          distRenderDetailCharts(_itemDetailData);
          if (_idetNormalRendered) distRenderNormal(_itemDetailData);
        }
        if (document.querySelector("#panel-distribution .dist-toolbar")) distRenderGallery();
        return;
      }
      if (kind === "seq") {
        // Serial 순 — 서버 응답(values/serial 이 이미 행 순서)을 그대로 다시 그리기만 한다.
        // 재조회 없음: /scatter 는 order 를 모르고, 알 필요도 없다.
        distSeqOnly = !distSeqOnly;
        iseg.classList.toggle("active", distSeqOnly);
        if (distSeqOnly) { cdfAxisOverride = null; ensureDistSeqData(); }
        idetSyncCdfCaption();
        renderIdetAxisBar("cdf");   // seq 모드에서는 바를 비운다(축 의미가 다르다)
        if (_itemDetailData) distRenderCdf(_itemDetailData);
        if (document.querySelector("#panel-distribution .dist-toolbar")) distRenderGallery();
        return;
      }
      // Bin1 계열은 데이터 변형이라 현재 항목을 새 변형으로 다시 연다. 두 버튼은 상호배타.
      if (kind === "bin1") {
        distBin1Only = !distBin1Only;
        if (distBin1Only) { distRtBin1Only = false; ensureDistBin1Data(); }
      } else if (kind === "rtbin1") {
        distRtBin1Only = !distRtBin1Only;
        if (distRtBin1Only) { distBin1Only = false; ensureDistRtBin1Data(); }
      } else return;
      if (document.querySelector("#panel-distribution .dist-toolbar")) distRenderGallery();
      if (_itemDetailSubject) openItemDetail(_itemDetailSubject, _itemDetailNav, _itemDetailOpts);
      return;
    }
    const hm = e.target.closest("[data-hist-mode]");
    if (hm) { setIdetHistMode(hm.dataset.histMode); return; }
    const mb = e.target.closest("[data-cdf-mode]");
    if (mb) { cdfEditMode = mb.dataset.cdfMode; cdfAfterEdit(); return; }
    if (e.target.closest(".cdf-reset")) { cdfResetEdits(); cdfAfterEdit(); return; }
    const axb = e.target.closest("[data-cdf-axis]");
    if (axb) {   // 감싸는 바에서 어느 차트(cdf|hist)의 축옵션인지 되짚는다
      const host = axb.closest("[data-axis-key]");
      const key = host ? host.dataset.axisKey : "cdf";
      axb.dataset.cdfAxis === "apply" ? idetAxisApply(key) : idetAxisAuto(key);
      return;
    }
    const png = e.target.closest("[data-idet-png]");
    if (png) { idetCopyChartPng(png.dataset.idetPng); return; }
    const pg = e.target.closest("[data-idet-page]");
    if (pg && !pg.disabled) { _itemDetailFailPage = parseInt(pg.dataset.idetPage, 10) || 1; renderItemFailRows(); return; }
  });
  dp.addEventListener("change", e => {
    if (distTempFilterChange(e)) return;   // Temperature 그룹 선택 → source 강조
    const chk = e.target.closest(".cdf-fail-chk");
    if (!chk) return;
    const set = cdfActiveSet();
    if (!set) return;
    if (chk.checked) set.add(chk.dataset.chipkey); else set.delete(chk.dataset.chipkey);
    cdfAfterEdit();
  });
  dp.addEventListener("keydown", e => {   // 축옵션 입력칸에서 Enter → 적용
    const inp = e.target.closest(".cdf-ax-in");
    if (e.key === "Enter" && inp) {
      e.preventDefault();
      const host = inp.closest("[data-axis-key]");
      idetAxisApply(host ? host.dataset.axisKey : "cdf");
    }
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
function distHistXRange(sources, lo, hi) {
  const all = [];
  (sources || []).forEach(s => { for (const v of s.values) all.push(v); });
  const hasLo = lo !== null && lo !== undefined, hasHi = hi !== null && hi !== undefined;
  if (hasLo && hasHi) {
    // 규격 밖 산포가 있으면 그쪽만 span×5% 가드밴드로 넓힌다. 전부 규격 안이면
    // dmin>=lo · dmax<=hi 라 [lo,hi] 그대로 — 기존 동작과 완전히 동일하다.
    // (예전엔 is_fail 일 때만 넓혔는데, is_fail 은 FAILTNO 기준이라 규격 이탈 여부와
    //  무관해 비-fail 항목의 규격 밖 데이터가 축 밖으로 잘려 안 보였다.)
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
  // Gap Chart 는 합성값이라 die 제외(전처리)로 되돌릴 원본 항목이 없다 → 편집 UI 를 숨긴다.
  if (_itemDetailData && _itemDetailData.is_gap) { bar.innerHTML = ""; return; }
  const modeBtn = (m, label, cls) =>
    `<button type="button" class="btn-sm cdf-mode ${cls}${cdfEditMode === m ? " active" : ""}" data-cdf-mode="${m}">${label}</button>`;
  bar.innerHTML =
    `<span class="cdf-eb-label">분포 편집</span>` +
    modeBtn("none", "선택 없음", "cdf-mode-none") +
    modeBtn("exclude", "제외", "cdf-mode-exclude") +
    `<button type="button" class="btn-sm cdf-reset">초기화</button>` +
    `<span class="cdf-eb-count">제외 ${cdfExcluded.size}</span>` +
    (cdfEditMode !== "none"
      ? `<span class="cdf-eb-hint">CDF: 점 클릭·드래그 박스 · 히스토그램: 드래그로 x구간 제외 · 하단 Fail 표 체크박스</span>` : "");
}
function cdfToggleChip(key) {
  const set = cdfActiveSet();
  if (!set) return;
  if (set.has(key)) set.delete(key); else set.add(key);
}
// x축 옵션 툴바(Excel 축옵션식): 경계(min/max) + 단위(기본/보조) + 적용/자동. CDF·히스토그램
// 공용이라 key(IDET_AXIS) 로 굴린다. 정적 마크업만 그림 — 입력 기본값은 렌더 직후
// syncIdetAxisInputs 가 '현재 그려진' 축값으로 채운다.
function renderIdetAxisBar(key) {
  const cfg = IDET_AXIS[key];
  const bar = cfg && document.getElementById(cfg.bar);
  if (!bar) return;
  // Serial 순 모드의 CDF 자리는 x 가 "측정 순서"라 이 바(측정값 경계·단위)의 의미가 사라진다.
  // 값을 남겨두면 순서 축에 규격 단위가 적용돼 조용히 이상한 눈금이 된다 → 바 자체를 비운다.
  if (key === "cdf" && distSeqOnly) { bar.innerHTML = ""; bar.dataset.axisKey = key; return; }
  const num = k => `<input type="number" class="cdf-ax-in" data-cdf-ax="${k}" step="any">`;
  // 보조는 간격이 아니라 **등분 수(정수 n≥2)** 를 받는다 — 보조 눈금 간격 = 기본 단위 / n.
  const div = `<input type="number" class="cdf-ax-in" data-cdf-ax="minor" step="1" min="2" placeholder="n">`;
  bar.dataset.axisKey = key;   // 위임 핸들러가 어느 차트의 바인지 되짚는 표식
  bar.innerHTML =
    `<span class="cdf-eb-label">${esc(cfg.label)}</span>` +
    `<span class="cdf-ax-grp">경계 ${num("min")} ~ ${num("max")}</span>` +
    `<span class="cdf-ax-grp" title="보조 눈금 간격 = 기본 단위 ÷ n (정수 2 이상)">단위 기본 ${num("major")} 보조 1/${div}</span>` +
    `<button type="button" class="btn-sm cdf-ax-apply" data-cdf-axis="apply">적용</button>` +
    `<button type="button" class="btn-sm cdf-ax-auto" data-cdf-axis="auto">자동</button>` +
    `<span class="cdf-ax-msg"></span>`;
}
// 렌더된 실제 x축(자동 계산 포함)을 입력칸 기본값으로 반영 — 자동 모드(override=null)에서만.
// 사용자가 값을 '적용'한 상태에서는 그 값이 이미 반영돼 있으므로 덮어쓰지 않는다.
function syncIdetAxisInputs(key, div) {
  const cfg = IDET_AXIS[key];
  const bar = cfg && document.getElementById(cfg.bar);
  if (!bar || cfg.get()) return;
  const ax = div && div._fullLayout && div._fullLayout.xaxis;
  if (!ax || !ax.range) return;
  const fmt = v => (v == null ? "" : Number(v.toPrecision(6)));   // float 잡음 제거
  const set = (k, v) => { const el = bar.querySelector(`[data-cdf-ax="${k}"]`); if (el) el.value = fmt(v); };
  set("min", ax.range[0]); set("max", ax.range[1]);
  set("major", typeof ax.dtick === "number" ? ax.dtick : null);
  set("minor", null);   // 자동 모드는 보조 눈금 미표시 → 비움(사용자가 입력하면 그때 생성)
}
// 보조 눈금 간격 = 기본 단위 ÷ 등분 수. 기본 단위를 모르면(자동 눈금) 보조도 만들지 않는다 —
// 기준 없이 그은 보조선은 주 눈금과 어긋나 보이기 때문이다.
function idetMinorDtick(ov) {
  if (!ov || !ov.major || !ov.minorDiv || ov.minorDiv < 2) return null;
  return ov.major / ov.minorDiv;
}
// 보조 눈금 layout 조각 — 격자선은 주 눈금과 같이 플롯 높이 전체를 가로지른다.
function idetMinorAxis(dtick) {
  return { dtick, showgrid: true, gridcolor: IDET_GRID_MINOR, gridwidth: 1, ticks: "" };
}
// 적용: 입력 4칸을 읽어 override 확정 후 해당 차트만 재렌더. 검증 실패 시 인라인 메시지.
// 기본 단위를 비워 둔 채 보조만 넣으면 지금 그려진 축의 자동 눈금 간격을 기본 단위로 삼는다
// (그래야 "기본의 1/n" 이 성립한다).
function idetAxisApply(key) {
  const cfg = IDET_AXIS[key];
  const bar = cfg && document.getElementById(cfg.bar);
  if (!bar) return;
  const raw = k => { const el = bar.querySelector(`[data-cdf-ax="${k}"]`); return el ? String(el.value).trim() : ""; };
  const val = k => { const v = parseFloat(raw(k)); return isFinite(v) ? v : null; };
  const min = val("min"), max = val("max");
  const msg = bar.querySelector(".cdf-ax-msg");
  if (min == null || max == null || min >= max) { if (msg) msg.textContent = "경계 최소<최대 확인"; return; }
  let major = val("major");
  if (!(major > 0)) major = null;
  const minorTxt = raw("minor");
  let minorDiv = null;
  if (minorTxt) {
    const n = Number(minorTxt);
    if (!Number.isInteger(n) || n < 2) { if (msg) msg.textContent = "보조는 2 이상 정수"; return; }
    minorDiv = n;
    if (major == null) {
      // 기본 단위 미입력 → 현재 그려진 축의 dtick 을 그대로 채택(입력칸에도 되비춘다).
      const div = document.getElementById(key === "cdf" ? "distCdf" : "distHist");
      const ax = div && div._fullLayout && div._fullLayout.xaxis;
      if (ax && typeof ax.dtick === "number" && ax.dtick > 0) {
        major = ax.dtick;
        const el = bar.querySelector('[data-cdf-ax="major"]');
        if (el) el.value = Number(major.toPrecision(6));
      } else { if (msg) msg.textContent = "보조를 쓰려면 기본 단위를 입력하세요"; return; }
    }
  }
  if (msg) msg.textContent = "";
  cfg.set({ min, max, major, minorDiv });
  cfg.render();   // 해당 차트만 재렌더(override 반영)
}
// 자동: override 해제 후 재렌더 → syncIdetAxisInputs 가 현재값으로 입력칸 재기입.
function idetAxisAuto(key) {
  const cfg = IDET_AXIS[key];
  if (!cfg) return;
  cfg.set(null);
  cfg.render();
}
// 편집(제외/모드전환/초기화) 후 CDF·히스토그램·툴바·Fail표를 다시 그림.
// 히스토그램은 제외(cdfExcluded)를 반영하므로 함께 재렌더해야 초기화 시 원복된다. 통계표는 불변.
function cdfAfterEdit() {
  if (_itemDetailData) { distRenderCdf(_itemDetailData); distRenderHist(_itemDetailData); }
  renderIdetChipVals();   // gap 은 제외 편집으로 분모가 바뀌면 누적%도 바뀐다
  renderCdfEditBar();
  if (_itemDetailData && _itemDetailData.is_fail) renderItemFailRows();
}
// CDF 만 렌더(제외→분모 재계산, 편집 모드→dragmode=select + 클릭/박스선택).
function distRenderCdf(data) {
  const cdfDiv = document.getElementById("distCdf");
  if (!cdfDiv) return;
  _idetGapChipHits = [];   // 이번 렌더에서 다시 모은다(Serial 순 분기로 빠져도 stale 금지)
  // Serial 순 모드는 같은 자리에 축이 다른 차트를 그린다 — 분기는 이 한 곳뿐이라
  // 재렌더 호출부(축옵션·칩 편집·항목 이동)가 전부 자동으로 따라온다.
  if (distSeqOnly) { distRenderSeq(data, cdfDiv); return; }
  // 재렌더(제외/강조 편집) 시 이전 plot 을 해제 — scattergl 의 WebGL 컨텍스트 누적 방지
  // (SVG 에도 무해). newPlot 이 이어서 새로 초기화한다.
  if (cdfDiv.data) { try { Plotly.purge(cdfDiv); } catch (e) { /* no-op */ } }
  // 렌더 방식 토글(distribution.js DIST 상수) — WebGL 불가 PC 는 SVG 폴백(core.js webglOk)
  const useGl = !!DIST.CDF_GL && webglOk();
  const lo = data.lower_limit, hi = data.upper_limit;
  const bg = DIST_STATUS_BG[data.status] || "#FFFFFF";
  const unit = data.units || "";
  const xtitle = `측정값${unit ? " [" + unit + "]" : ""}`;
  // 단측 스펙 클램프용 데이터 끝값 — 제외(cdfExcluded) 반영 후 곡선 기준으로 잡는다.
  let cdfMin = Infinity, cdfMax = -Infinity;
  // Gap Chart 상세(이 화면을 그대로 재사용한다)의 Map 선택 좌표 마커. gap 값은 파생이라
  // chip.items 에 없어 여기서 곡선과 **같은 배열**로부터 모은다(제외 편집도 자동 반영).
  let gapChipHits = [];
  // 강조 소스가 겹침에 묻히지 않게 dim 소스 먼저 그린다(distOrderedSources).
  const traces = distOrderedSources(data.sources).map(s => {
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
    if (data.is_gap && hasId && typeof gcChipHits === "function") {
      gapChipHits = gapChipHits.concat(
        gcChipHits(s.name, c, xpos, ypos, data.gap_mode !== "explicit"));
    }
    if (c.x.length) {   // c.x 는 오름차순 — 양끝만 보면 된다
      if (c.x[0] < cdfMin) cdfMin = c.x[0];
      if (c.x[c.x.length - 1] > cdfMax) cdfMax = c.x[c.x.length - 1];
    }
    const base = distActiveColorFor(s.name);
    const trace = { type: useGl ? "scattergl" : "scatter", mode: "markers", name: s.name,
      x: c.x, y: c.y };
    if (!useGl) trace.cliponaxis = false;   // scattergl 미지원 속성 — SVG 분기에만
    if (hasId) {
      // customdata/hover 는 필터·정렬된 동일 순서 유지(클릭 식별·hover 지속).
      // source 명은 본문 첫 줄에 넣고 <extra> 는 비운다 (2026-08-11 사용자 요청) —
      // <extra> 는 Plotly 가 좌표·SERIAL 과 **떨어진 별도 상자**로 그려서, 같은 칸에서
      // 읽히지 않았다.
      trace.customdata = c.order.map(i => [serial[i], xpos[i], ypos[i]]);
      trace.hovertemplate = "source : %{fullData.name}<br>측정값 %{x}<br>누적 %{y:.1f}%<br>SERIAL %{customdata[0]} · X %{customdata[1]} / Y %{customdata[2]}<extra></extra>";
      trace.marker = { color: base, size: 5 };
    } else {
      trace.marker = { color: base, size: 5 };
      trace.hovertemplate = "source : %{fullData.name}<br>측정값 %{x}<br>누적 %{y:.1f}%<extra></extra>";
    }
    return trace;
  });
  // 선택 좌표(Map Analysis)가 있으면 이 항목 위치를 점+빨간 점선으로 오버레이.
  let cdfShapes = distSpecShapes(lo, hi, true).concat(beforeLimitShapes(data.subject));
  if (data.is_gap) _idetGapChipHits = gapChipHits;   // 아래 chip 값 표가 같은 값을 쓴다
  // useGl 을 넘겨 곡선과 같은 레이어에 그린다 — 안 넘기면 SVG 마커가 gl 캔버스 아래로
  // 깔려 보이지 않는다(mapSelMarkerTraces 주석).
  const cdfCm = data.is_gap ? mapSelMarkerTraces(gapChipHits, useGl)
                            : chipMarkersFor(data.subject, useGl);
  if (cdfCm) { traces.push(...cdfCm.traces); cdfShapes = cdfShapes.concat(cdfCm.shapes); }
  const dragmode = cdfEditMode === "none" ? "zoom" : "select";
  const cdfLr = distLimitRange(lo, hi, cdfMin, cdfMax);
  // x축: 사용자 축옵션(경계/단위)이 있으면 우선, 없으면 기존 동작(distLimitOnly 창 → autorange).
  const ov = cdfAxisOverride;
  const xaxisCfg = { title: { text: xtitle }, showgrid: true, gridcolor: IDET_GRID_MAJOR, zeroline: false };
  if (ov) {
    xaxisCfg.range = [ov.min, ov.max]; xaxisCfg.autorange = false;
    if (ov.major) { xaxisCfg.dtick = ov.major; xaxisCfg.tick0 = ov.min; } else xaxisCfg.nticks = 10;
    const mdt = idetMinorDtick(ov);
    if (mdt) { xaxisCfg.minor = idetMinorAxis(mdt); xaxisCfg.minor.tick0 = ov.min; }
  } else {
    xaxisCfg.nticks = 10;
    if (cdfLr) { xaxisCfg.range = cdfLr; xaxisCfg.autorange = false; }
  }
  Plotly.newPlot(cdfDiv, traces, { ...DIST_PLOT_BG, plot_bgcolor: bg, dragmode,
    xaxis: xaxisCfg,
    yaxis: { title: { text: "누적 %" }, range: [-2, 102], tick0: 0, dtick: 20, ticksuffix: "%", showgrid: true, gridcolor: IDET_GRID_MAJOR, zeroline: false },
    shapes: cdfShapes,
    annotations: distSpecAnnos(lo, hi, false).concat(beforeLimitAnnos(data.subject)),
    margin: { l: 60, r: 22, t: 16, b: 46 }, showlegend: false }, DIST_CFG);
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
  syncIdetAxisInputs("cdf", cdfDiv);
  // 차트 주석 오버레이 — 렌더 시점의 shapes 개수를 base 로 기억해야 하므로 항상 마지막에.
  if (window.chartNotesApply) chartNotesApply("cdf", data.subject, cdfDiv);
}
// ── Serial 순(rawdata 누적 순) 상세 차트 — CDF 자리에 그리는 run chart ─────────
// x = 각 source 의 측정 순서(1..n) · y = 측정값. 데이터는 **/scatter 응답 그대로**다
// (values/serial/xpos/ypos 가 이미 rawdata 행 순서) — 서버 재조회가 없다.
// 전 포인트 렌더(다운샘플 없음, CLAUDE.md §5-5) — 대량 포인트는 CDF 와 같은 scattergl.
//
// ⚠️ **차트 주석(chart_notes)을 이 차트에 붙이지 않는다.** 주석 도형은 xref"x"/yref"y" =
// 데이터 좌표로 저장되는데 이 축은 (순서, 측정값)이라 CDF 좌표와 의미가 다르다. 붙이면
// 위치가 어긋나 보이는 데서 끝나지 않고, 편집 모드에서 도형을 한 번 건드리면
// cnSyncFromChart 가 **seq 좌표로 저장값을 덮어써** 사용자가 CDF 에 그려둔 주석이 망가진다
// (§5-12 "사용자 입력은 잃지 않는다"). 저장값은 그대로 두고 표시만 생략한다 — CDF 로
// 돌아가면 그대로 다시 보인다. Map Analysis 선택 좌표 마커·Compare before-limit 선도
// 같은 이유로 제외(누적% 축 전용).
function distRenderSeq(data, seqDiv) {
  if (seqDiv.data) { try { Plotly.purge(seqDiv); } catch (e) { /* no-op */ } }
  const useGl = !!DIST.CDF_GL && webglOk();
  const lo = data.lower_limit, hi = data.upper_limit;
  const bg = DIST_STATUS_BG[data.status] || "#FFFFFF";
  const unit = data.units || "";
  let yMin = Infinity, yMax = -Infinity;
  // 강조 소스가 겹침에 묻히지 않게 dim 소스 먼저 그린다(CDF 와 동일 규칙).
  const traces = distOrderedSources(data.sources).map(s => {
    const hasId = Array.isArray(s.serial) && s.serial.length === s.values.length;
    // 제외 칩(cdfExcluded)은 CDF 와 같은 규칙으로 뺀다 — 남은 점의 순서는 그대로 유지되고
    // x 는 1..m 으로 다시 매긴다(빈 자리를 남기면 없던 결측 구간처럼 보인다).
    const xs = [], ys = [], cd = [];
    for (let i = 0; i < s.values.length; i++) {
      if (hasId && cdfExcluded.size
          && cdfExcluded.has(cdfChipKey(s.name, s.serial[i], s.xpos[i], s.ypos[i]))) continue;
      const v = s.values[i];
      xs.push(xs.length + 1);
      ys.push(v);
      if (hasId) cd.push([s.serial[i], s.xpos[i], s.ypos[i]]);
      if (v < yMin) yMin = v;
      if (v > yMax) yMax = v;
    }
    const base = distActiveColorFor(s.name);
    const trace = { type: useGl ? "scattergl" : "scatter", mode: "markers", name: s.name,
      x: xs, y: ys, marker: { color: base, size: 5 } };
    if (!useGl) trace.cliponaxis = false;   // scattergl 미지원 속성 — SVG 분기에만
    if (hasId) {
      trace.customdata = cd;
      trace.hovertemplate = "source : %{fullData.name}<br>측정 순서 %{x}<br>측정값 %{y}<br>SERIAL %{customdata[0]} · X %{customdata[1]} / Y %{customdata[2]}<extra></extra>";
    } else {
      trace.hovertemplate = "source : %{fullData.name}<br>측정 순서 %{x}<br>측정값 %{y}<extra></extra>";
    }
    return trace;
  });
  // "Limit 안 Data만" 은 여기서 **y**(측정값) 축 클램프다 — 계산식은 축과 무관해 재사용한다.
  const yr = distLimitRange(lo, hi, yMin, yMax);
  Plotly.newPlot(seqDiv, traces, { ...DIST_PLOT_BG, plot_bgcolor: bg,
    dragmode: cdfEditMode === "none" ? "zoom" : "select",
    xaxis: { title: { text: "측정 순서 (rawdata 누적 순)" }, showgrid: true,
      gridcolor: IDET_GRID_MAJOR, zeroline: false, rangemode: "tozero", nticks: 10 },
    yaxis: { title: { text: `측정값${unit ? " [" + unit + "]" : ""}` }, showgrid: true,
      gridcolor: IDET_GRID_MAJOR, zeroline: false,
      ...(yr ? { range: yr, autorange: false } : {}) },
    shapes: distSeqSpecShapes(lo, hi), annotations: distSeqSpecAnnos(lo, hi, false),
    margin: { l: 60, r: 22, t: 16, b: 46 }, showlegend: false }, DIST_CFG);
  // 칩 제외 편집(클릭/박스선택)은 CDF 와 동일하게 동작한다 — 점 1개 = die 1개라 customdata
  // 로 그 die 를 정확히 겨냥할 수 있다.
  if (seqDiv.removeAllListeners) {
    seqDiv.removeAllListeners("plotly_click");
    seqDiv.removeAllListeners("plotly_selected");
  }
  seqDiv.on("plotly_click", ev => {
    if (!cdfActiveSet() || !ev.points || !ev.points.length) return;
    const pt = ev.points[0];
    if (!pt.customdata) return;
    cdfToggleChip(cdfChipKey(pt.data.name, pt.customdata[0], pt.customdata[1], pt.customdata[2]));
    cdfAfterEdit();
  });
  seqDiv.on("plotly_selected", ev => {
    const set = cdfActiveSet();
    if (!set || !ev || !ev.points || !ev.points.length) return;
    ev.points.forEach(pt => { if (pt.customdata) set.add(cdfChipKey(pt.data.name, pt.customdata[0], pt.customdata[1], pt.customdata[2])); });
    cdfAfterEdit();
  });
}

// 히스토그램(빈도 폴리곤)만 렌더 — CDF '제외'(cdfExcluded)를 반영하므로 편집/초기화 때도 재호출.
function distRenderHist(data) {
  const hDiv = document.getElementById("distHist");
  if (!hDiv) return;
  const lo = data.lower_limit, hi = data.upper_limit;
  const bg = DIST_STATUS_BG[data.status] || "#FFFFFF";
  const unit = data.units || "";
  const xtitle = `측정값${unit ? " [" + unit + "]" : ""}`;
  // 막대 대신 빈도 폴리곤: 21bin 중심점-빈도 곡선(양끝 0 패딩), CDF 와 동일한 원본 values 재사용.
  // 강조 시 dim 소스 먼저 그리기 (CDF 와 동일 규칙).
  const polys = distHistPolygon(distOrderedSources(data.sources), lo, hi, cdfExcluded);
  const hr = distSourcesRange(data.sources);   // 단측 스펙 클램프용 데이터 끝값
  let ymax = 0;
  polys.forEach(p => p.counts.forEach(c => { if (c > ymax) ymax = c; }));
  const traces = polys.map(p => ({ type: "scatter", mode: "lines", name: p.source,
    x: p.centers, y: p.counts, line: { color: distActiveColorFor(p.source), shape: "spline" },
    hovertemplate: "측정값 %{x}<br>빈도 %{y:d}<extra></extra>" }));
  // x축 우선순위: 사용자 축옵션 > "Limit 안 Data만" 클램프 > 데이터 인지 자동범위 (CDF 와 동일).
  // CDF else 분기의 nticks:10 은 여기 넣지 않는다 — 원래 없던 값이라 넣으면 기본 경로의
  // 눈금 배치가 바뀐다.
  const hov = histAxisOverride;
  const xaxisCfg = { title: { text: xtitle }, showgrid: true, gridcolor: IDET_GRID_MAJOR, zeroline: false };
  if (hov) {
    xaxisCfg.range = [hov.min, hov.max]; xaxisCfg.autorange = false;
    if (hov.major) { xaxisCfg.dtick = hov.major; xaxisCfg.tick0 = hov.min; }
    const mdt = idetMinorDtick(hov);
    if (mdt) { xaxisCfg.minor = idetMinorAxis(mdt); xaxisCfg.minor.tick0 = hov.min; }
  } else {
    xaxisCfg.range = distLimitRange(lo, hi, hr.min, hr.max) || extendRangeForBeforeLimits(
      distHistXRange(data.sources || [], lo, hi), data.subject);
  }
  Plotly.newPlot(hDiv, traces, { ...DIST_PLOT_BG, plot_bgcolor: bg,
    dragmode: cdfEditMode === "none" ? "zoom" : "select",
    xaxis: xaxisCfg,
    yaxis: { title: { text: "빈도" }, range: [0, (ymax || 1) * 1.1], tickformat: "d",
      showgrid: true, gridcolor: IDET_GRID_MAJOR, zeroline: false },
    shapes: distSpecShapes(lo, hi, false).concat(beforeLimitShapes(data.subject)),
    annotations: distSpecAnnos(lo, hi, false).concat(beforeLimitAnnos(data.subject)),
    margin: { l: 60, r: 22, t: 16, b: 46 }, showlegend: false }, DIST_CFG);
  // 히스토그램은 bin 중심점 폴리곤이라 점 1개 = die 1개가 아니다(customdata 없음).
  // 그래서 박스선택은 x구간만 읽고 원본 values 를 훑어 그 구간 die 를 제외집합에 넣는다.
  // 라쏘는 ev.range 가 없어(ev.lassoPoints) 무시 — DIST_CFG 가 modeBar 를 숨기고
  // dragmode 를 select 로 두므로 드래그 도구는 박스뿐이고, 아래 가드가 이중 방어다.
  // ★ newPlot 은 div 에 걸린 .on 핸들러를 지우지 않는다(CDF 는 앞서 purge 하지만 여기는
  //   안 한다). 제거하지 않으면 편집마다 핸들러가 누적돼 선택 1회에 cdfAfterEdit 가 N회 돈다.
  if (hDiv.removeAllListeners) hDiv.removeAllListeners("plotly_selected");
  hDiv.on("plotly_selected", ev => {
    const set = cdfActiveSet();
    if (!set || !ev || !ev.range || !ev.range.x || !_itemDetailData) return;
    const rx = ev.range.x, x0 = Math.min(rx[0], rx[1]), x1 = Math.max(rx[0], rx[1]);
    let n = 0;
    (_itemDetailData.sources || []).forEach(s => {
      if (!Array.isArray(s.serial) || s.serial.length !== s.values.length) return;
      for (let i = 0; i < s.values.length; i++) {
        const v = s.values[i];
        if (v < x0 || v > x1) continue;
        set.add(cdfChipKey(s.name, s.serial[i], s.xpos[i], s.ypos[i]));
        n++;
      }
    });
    if (n) cdfAfterEdit();
  });
  // 렌더된 실제 x축값을 축옵션 입력칸 기본값으로 반영(자동 모드에서만).
  syncIdetAxisInputs("hist", hDiv);
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
// Report 탭은 서버 통계(μ/σ) 기준이라 CDF/히스토그램의 '제외' 편집을 반영하지 않는다.
// (반영하려면 μ/σ 를 클라이언트에서 재계산해야 하고, 그러면 아래 통계표와 어긋난다.)
function distRenderNormal(data) {
  const nDiv = document.getElementById("distNormal");
  if (!nDiv) return;
  const lo = data.lower_limit, hi = data.upper_limit;
  const bg = data.is_fail ? "#FEF9E7" : "#FFFFFF";   // 규격 이탈(Fail) → 배경 연노랑
  const unit = data.units || "";
  const xtitle = `측정값${unit ? " [" + unit + "]" : ""}`;
  const statByName = {};
  (data.stats || []).forEach(s => { statByName[s.source] = s; });
  const traces = [], spikes = [];
  let ymax = 0;
  // 강조 시 dim 소스 먼저 그리기 (CDF 와 동일 규칙).
  distOrderedSources(data.sources).forEach(s => {
    const st = statByName[s.name];
    if (!st) return;
    const color = distActiveColorFor(s.name), mean = st.average, std = st.stdev;
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
    traces.push({ type: "scatter", mode: "lines", name: s.name,
      x: xs, y: ys, hoverinfo: "skip", line: { color, width: 1.4 } });
  });
  // x축: 1단계(distHistXRange, bin 버전과 동일) → 2단계(±5% 항상 적용, 이중 마진).
  let range = distHistXRange(data.sources || [], lo, hi);
  if (range) {
    const span = range[1] - range[0];
    range = extendRangeForBeforeLimits([range[0] - span * 0.05, range[1] + span * 0.05], data.subject);
  }
  const nr = distSourcesRange(data.sources);   // 단측 스펙 클램프용 데이터 끝값
  const normLr = distLimitRange(lo, hi, nr.min, nr.max);
  if (normLr) range = normLr;   // Limit 안 Data만 보기
  Plotly.newPlot(nDiv, traces, { ...DIST_PLOT_BG, plot_bgcolor: bg,
    xaxis: { title: { text: xtitle }, range, showgrid: false, zeroline: false },
    yaxis: { range: [0, ymax > 0 ? ymax * 1.1 : 1],
      showticklabels: false, showgrid: false, zeroline: false },
    shapes: distSpecShapes(lo, hi, false).concat(spikes, beforeLimitShapes(data.subject)),
    annotations: distSpecAnnos(lo, hi, false).concat(beforeLimitAnnos(data.subject)),
    margin: { l: 24, r: 22, t: 16, b: 46 }, showlegend: false }, DIST_CFG);
}
// 강조 변경 시 상세 차트의 색만 갈아끼운다 — 재렌더 없이 zoom/선택/주석을 보존.
// source trace 만 골라야 한다: chipMarkersFor(map_select.js) 가 붙이는 칩 trace 는
// name 자체가 없고, distNormal 은 degenerate source 를 곡선 대신 shape 로 빼서
// trace index 와 source index 가 어긋나므로 이름으로 되짚는다.
function idetRestyleSourceColors(div, prop) {
  if (!div || !div.data) return;
  const idx = [], cols = [];
  div.data.forEach((t, i) => {
    if (!t.name || !(t.name in distColorMap)) return;
    idx.push(i); cols.push(distActiveColorFor(t.name));
  });
  if (idx.length) { try { Plotly.restyle(div, { [prop]: cols }, idx); } catch (e) { /* no-op */ } }
  idetReorderSourceTraces(div);
}
// 강조 소스 trace 를 dim 소스 뒤(=위)로 이동 — 산포가 겹치면 색만 갈아서는 강조가
// 아래 trace 에 깔려 안 보인다(distDrawPoints 의 dim-먼저 정렬과 같은 규칙). 목표 순서를
// 항상 원본 sources 순서 기준으로 계산하므로 강조 해제 시 원래 그리기 순서로 복원된다.
// source trace 끼리 같은 슬롯 집합 안에서만 자리를 바꿔, 뒤에 붙는 칩 마커(이름 없음)는
// 계속 최상단이다. moveTraces 는 layout 을 건드리지 않아 zoom/주석 보존.
function idetReorderSourceTraces(div) {
  if (!div || !div.data || !_itemDetailData) return;
  const rank = {};
  (_itemDetailData.sources || []).forEach((s, i) => { rank[s.name] = i; });
  const slots = [];   // source trace 가 차지한 현재 인덱스(오름차순)
  div.data.forEach((t, i) => { if (t.name && (t.name in rank)) slots.push(i); });
  if (slots.length < 2) return;
  const want = slots.slice().sort((a, b) => {
    const na = div.data[a].name, nb = div.data[b].name;
    const ha = distSourceFilter.has(na) ? 1 : 0, hb = distSourceFilter.has(nb) ? 1 : 0;
    return (ha - hb) || (rank[na] - rank[nb]);
  });
  if (want.every((v, i) => v === slots[i])) return;   // 이미 원하는 순서 — redraw 생략
  try { Plotly.moveTraces(div, want, slots); } catch (e) { /* no-op */ }
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
  const all = distSuggestions(q, 0);              // 전량 — 개수 표시·전체 선택/해제용
  if (!String(q).trim() || !all.length) { box.innerHTML = ""; box.style.display = "none"; return; }
  const items = all.length > 30 ? all.slice(0, 30) : all;   // 목록은 기존대로 30개까지
  // 헤더: 실제 일치 수 + 전체 선택/해제. '전체'는 표시된 30개가 아니라 일치 전량이 대상이라
  // 개수를 명시해 동작이 놀랍지 않게 한다.
  const head = `<div class="dist-sug-head">` +
    `<span class="dist-sug-cnt">일치 <b>${all.length}</b>개` +
    (all.length > items.length ? ` <span class="dist-sug-more">(상위 ${items.length}개 표시)</span>` : "") +
    `</span>` +
    `<button type="button" class="btn-sm" data-sug-all="1">전체 선택</button>` +
    `<button type="button" class="btn-sm" data-sug-all="0">전체 해제</button></div>`;
  // 체크박스로 다중 선택 → 선택 항목만 갤러리에 표시. cpk 값은 표시하지 않는다.
  box.innerHTML = head + items.map(r =>
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
    if (distLegendClick(e)) return;   // 범례 클릭 → source 강조
    // 검색 결과 전체 선택/해제 — 표시된 30개가 아니라 현재 검색어의 전 일치 항목이 대상.
    // '전체 해제'는 이 검색어의 일치 항목만 빼고, 다른 검색으로 고른 선택은 유지한다
    // (전부 지우는 것은 툴바의 '선택 N개 ✕' 칩).
    const sa = e.target.closest("[data-sug-all]");
    if (sa) {
      const sq = (document.getElementById("distSearch") || {}).value || "";
      const on = sa.dataset.sugAll === "1";
      distSuggestions(sq, 0).forEach(r => {
        if (on) distSelected.add(r.subject); else distSelected.delete(r.subject);
      });
      distRenderGallery();
      restoreDistSearch(sq);
      return;
    }
    const seg = e.target.closest(".distseg");
    if (seg) {
      if (seg.dataset.seg === "clearsel") distSelected.clear();
      // 전체 보기 — 항목 숨김 필터 3종 일괄 해제 (토글 아님, distToolbarHtml 참조).
      else if (seg.dataset.seg === "showall") { distCpkOnly = false; distFailOnly = false; distHidePassfail = false; distNewOnly = false; }
      else if (seg.dataset.seg === "cpk") distCpkOnly = !distCpkOnly;
      else if (seg.dataset.seg === "fail") distFailOnly = !distFailOnly;
      else if (seg.dataset.seg === "limit") distLimitOnly = !distLimitOnly;
      else if (seg.dataset.seg === "nopf") distHidePassfail = !distHidePassfail;
      else if (seg.dataset.seg === "newitem") distNewOnly = !distNewOnly;
      // Bin1 계열 두 버튼은 상호배타 — 둘 다 켜지면 어느 기준인지 알 수 없다.
      else if (seg.dataset.seg === "bin1") {
        distBin1Only = !distBin1Only;
        if (distBin1Only) { distRtBin1Only = false; ensureDistBin1Data(); }
      } else if (seg.dataset.seg === "rtbin1") {
        distRtBin1Only = !distRtBin1Only;
        if (distRtBin1Only) { distBin1Only = false; ensureDistRtBin1Data(); }
      } else if (seg.dataset.seg === "seq") {
        // Serial 순 — bin1 축과 직교(둘 다 켤 수 있다). 데이터 변형이라 seq 배치를 받는다.
        distSeqOnly = !distSeqOnly;
        if (distSeqOnly) ensureDistSeqData();
      }
      const q = (document.getElementById("distSearch") || {}).value || "";
      distRenderGallery();
      restoreDistSearch(q);
      return;
    }
    // Distribution composite — 분석하기 버튼/메뉴/카드 ✎✕/합성 카드 클릭.
    // 합성 카드도 .distg-card 라 **아래 일반 카드 분기보다 먼저** 가려야 한다.
    if (typeof dcPanelClick === "function" && dcPanelClick(e)) return;
    // Gap Chart — 카드 ✎✕/카드 클릭. 이것도 .distg-card 라 일반 카드 분기보다 앞이다.
    if (typeof gcPanelClick === "function" && gcPanelClick(e)) return;
    const card = e.target.closest(".distg-card");
    if (card) { openItemDetail(card.dataset.subject, distFiltered.map(r => r.subject)); return; }
  });
  // 검색 제안 체크박스 토글 → 선택 집합 갱신 후 갤러리를 선택 항목만으로 필터(검색상태 복원).
  panel.addEventListener("change", e => {
    if (distTempFilterChange(e)) return;   // Temperature 그룹 선택 → source 강조
    const chk = e.target.closest(".dist-sug-chk");
    if (!chk) return;
    if (chk.checked) distSelected.add(chk.dataset.subject);
    else distSelected.delete(chk.dataset.subject);
    const q = (document.getElementById("distSearch") || {}).value || "";
    distRenderGallery();
    restoreDistSearch(q);
  });
  // 제안 계산은 distIndex 전량 스캔이라 항목이 많은 세션에서 키입력마다 돌면 입력이
  // 끊긴다. Trim 검색과 같은 250ms debounce 로 맞춘다.
  let suggestTimer = null;
  panel.addEventListener("input", e => {
    if (e.target.id !== "distSearch") return;
    const q = e.target.value;
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(() => distRenderSuggest(q), 250);
  });
  // 검색 제안 목록은 검색창 영역(.dist-search-wrap) 밖을 클릭하면 닫는다(2026-08-07 사용자
  // 요청). 입력값·선택(distSelected)은 유지되고, 다시 입력하면 제안이 다시 열린다.
  // 문서 레벨 1회 등록 — 패널 위임 핸들러(버블링 선행)가 재렌더한 뒤에 실행되므로
  // 세그 토글 클릭에도 목록이 열린 채 남지 않는다.
  document.addEventListener("click", e => {
    const box = document.getElementById("distSuggest");
    if (!box || box.style.display === "none") return;
    if (e.target.closest && e.target.closest(".dist-search-wrap")) return;
    box.style.display = "none";
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
  if (!distDataReady) return;
  // Issue Table CPK 섹션 미니셀(data-bin1)은 Bin1(양품) ECDF 캐시를, Issue Table Temp 의
  // TEMP 섹션(data-bin1-scope="rt")은 Bin1(RT) 캐시를 쓴다 — 행의 숫자가 그 기준으로
  // 계산되므로 그림도 같은 변형이어야 한다(갤러리 토글과 같은 변형·같은 배치 로더).
  // 나머지 셀은 종전대로 전체 기준 캐시.
  const variant = cell.dataset.bin1 !== "1" ? "all"
    : (cell.dataset.bin1Scope === "rt" ? "rtbin1" : "bin1");
  const info = distCacheFor(variant)[subject];
  // 아직 안 받은 항목은 배치로 요청하고 플래그를 세우지 않은 채 리턴 — 도착 후
  // refreshDistConsumers 재큐잉으로 그려진다.
  if (!info && distHasData(subject)) { distRequestSubject(subject, variant); return; }
  // 데이터 없는 항목: 빈 칸으로 확정 (loaded 마킹해 재큐잉 no-op 방지)
  if (!info) { cell.innerHTML = ""; cell.dataset.distLoaded = "1"; return; }

  // 규격선은 distribution_index 기준(distSpecLimits) — Temperature 는 RT limit 이다.
  const { lo, hi } = distSpecLimits(subject, info);
  // markers 전용(선 금지 — CLAUDE.md §5). 세로 점 보간으로 이산값 성김을 보정.
  // 점은 canvas 로 그린다(distPaintPoints) — Plotly 에는 sentinel 만. 이 칸은 112px 로
  // 작아 칸 예산(CELL_BUDGET_MINI)을 소스 수로 나눈 캡을 쓴다 — 소스가 적으면 갤러리와
  // 동일하고 소스 수십 개일 때만 소스별 점이 줄어든다.
  // Temperature 모드 메인 Issue Table 셀(data-src-scope="rt")은 RT source 만 그린다 —
  // 그 표의 숫자가 RT 기준이라 CT/HT 곡선이 섞이면 표와 그림이 어긋난다(Map 미니셀의
  // issueBinMaps() 와 같은 규약). RT 집합이 비면(비Temperature 세션) 필터하지 않아 기존
  // 동작과 동일하고, 겹치는 소스가 없으면 데이터 없음과 같게 빈 칸으로 확정한다.
  let srcNames = Object.keys(info.bySource);
  if (cell.dataset.srcScope === "rt" && typeof tempFilterSources === "function") {
    const rt = new Set(tempFilterSources("RT", ""));
    if (rt.size) {
      srcNames = srcNames.filter(s => rt.has(s));
      if (!srcNames.length) { cell.innerHTML = ""; cell.dataset.distLoaded = "1"; return; }
    }
  }
  const cap = distCapFor(srcNames.length, DIST.CELL_BUDGET_MINI);
  const dsBySource = {};
  srcNames.forEach(source => {
    dsBySource[source] = distDisplayPoints(info.bySource[source], cap);
  });
  const sentinel = distSentinelTrace(dsBySource);
  const traces = sentinel ? [sentinel] : [];
  // x축을 데이터 범위로 고정한다. autorange 로 두면 LSL/USL 스펙선 shapes(데이터 범위 밖일
  // 수 있음)까지 x-autorange 에 포함돼 x축이 늘어나 곡선이 한쪽에 뭉쳐 잘린 것처럼 보인다.
  // xs 는 ECDF 라 오름차순(distDownsampleForDisplay 전제) → 양끝값으로 min/max 를 O(1) 로 잡음.
  // 표시점(dsBySource)으로 잡아도 값은 같다 — 채움·다운샘플·하드캡 모두 양끝점을 항상
  // 보존하므로 원본과 min/max 가 동일하다(headless 검증).
  let xMin = Infinity, xMax = -Infinity;
  srcNames.forEach(source => {
    const xs = dsBySource[source].xs;
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
  distPaintPoints(div, dsBySource, null);
  cell.dataset.distLoaded = "1";
}

// Issue Table 미니셀은 수천 개(항목 수 규모)라 전량 동기 렌더 시 메인스레드가 분 단위로
// 얼어붙는다 — 갤러리와 같은 IntersectionObserver + rAF 분할(프레임당 3개) lazy 렌더를 쓰고,
// 화면 밖으로 나가면 purge 해 plot DOM 상주를 막는다. 큐는 갤러리(distRenderQueue)와
// 분리 — 갤러리 재렌더가 큐를 초기화해도 issue 셀이 유실되지 않도록.
// ⚠️ 상태는 **패널별**이다 (2026-08-05). Issue 표 패널이 2개(Issue Table / Issue Table Temp)라
// 전역 1개로 두면 두 번째 패널을 렌더하는 순간 첫 패널의 observer 가 disconnect 되고 큐가
// 비워져, 그 패널 미니셀이 영구 공백으로 남는다.
const _issueDistState = new Map();   // panelId → {observer, queue, raf}
function issueDistStateOf(panel) {
  const id = (panel && panel.id) || ISSUE_PANEL_MAIN;
  let st = _issueDistState.get(id);
  if (!st) { st = { observer: null, queue: [], raf: false }; _issueDistState.set(id, st); }
  return st;
}

function issueDistQueueRender(cell) {
  const st = issueDistStateOf(issuePanelOf(cell));
  if (cell.dataset.distLoaded === "1" || st.queue.includes(cell)) return;
  st.queue.push(cell);
  if (!st.raf) { st.raf = true; requestAnimationFrame(() => issueDistFlush(st)); }
}
function issueDistFlush(st) {
  st.raf = false;
  let n = 0;
  const perFrame = distPerFrame();
  while (st.queue.length && n < perFrame) {
    const cell = st.queue.shift();
    if (cell.isConnected && cell.dataset.visible === "1") { renderMiniDistCell(cell); n++; }
  }
  if (st.queue.length) { st.raf = true; requestAnimationFrame(() => issueDistFlush(st)); }
}
function issueDistPurge(cell) {
  if (cell.dataset.distLoaded !== "1") return;
  const div = cell.querySelector(".dist-plot");
  if (!div) return;   // 데이터 없음으로 비워진 셀 — 확정 상태 유지
  try { if (window.Plotly) { distClearPoints(div); Plotly.purge(div); } } catch (e) {}
  cell.dataset.distLoaded = "";
}

function renderIssueMiniDist(panel) {
  const st = issueDistStateOf(panel);
  if (st.observer) { try { st.observer.disconnect(); } catch (e) {} st.observer = null; }
  st.queue = []; st.raf = false;
  const cells = panel.querySelectorAll(".dist-cell-mini");
  if (!cells.length) return;
  if (typeof IntersectionObserver === "undefined") {
    cells.forEach(cell => renderMiniDistCell(cell));   // 구형 브라우저 폴백: 기존 동작
    return;
  }
  st.observer = new IntersectionObserver(entries => {
    entries.forEach(en => {
      const cell = en.target;
      if (en.isIntersecting) { cell.dataset.visible = "1"; issueDistQueueRender(cell); }
      else {
        cell.dataset.visible = "";
        issueDistPurge(cell);
        const i = st.queue.indexOf(cell);
        if (i >= 0) st.queue.splice(i, 1);
      }
    });
  }, { rootMargin: "600px 0px", threshold: 0 });
  cells.forEach(c => st.observer.observe(c));
}


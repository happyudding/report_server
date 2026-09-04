// ── Map 좌표 선택 (Map Analysis 탭에서 chip 여러 개 골라 각기 다른 색으로 Map 강조 +
//    Distribution 전 항목 CDF 에 반영). 선택 상태는 전역 — Map redraw 와 Distribution 이 함께 참조. ──
const MAPSEL_PALETTE = ["#e11d48", "#2563eb", "#059669", "#d97706", "#7c3aed",
  "#0891b2", "#db2777", "#65a30d", "#ea580c", "#4f46e5"];
// 강조 색·크기 (2026-09-03) — Item_detail 드래그로 수십~수백 die 를 한 번에 잡게 되면서
// chip 마다 다른 색(팔레트 순환)은 의미가 없어졌다. 전 화면(Item_detail·Distribution 카드·
// Map 썸네일/상세/레전드·Honey Excel)이 **같은 옥색**을 쓴다. 색 결정은 mapSelColorAt 한 곳뿐이라
// 팔레트로 되돌리려면 MAPSEL_USE_PALETTE 만 true 로 바꾸면 된다.
const MAPSEL_HL_COLOR = "#2DD4BF";   // 밝은 청록(옥색) — 채움
const MAPSEL_HL_LINE = "#0F766E";    // 진한 청록 — 테두리(밝은 배경·점 무리 위에서도 윤곽 유지)
const MAPSEL_USE_PALETTE = false;
const MAPSEL_MARKER_SIZE = 10;       // 상세 차트(Item_detail·composite 상세) — 종전 7
const MAPSEL_CARD_MARKER_SIZE = 9;   // 갤러리 미니셀(canvas) — 종전 7
// 동시에 강조할 수 있는 좌표 수. chip 하나가 전 항목의 값·누적%를 들고 있어(항목 수천 개)
// 이 값이 곧 브라우저 힙·응답 크기의 상한이다. 서버도 같은 값으로 자른다
// (routes_webreport.py _COMMONALITY_LOOKUP_MAX — 한쪽만 바꾸면 400 이 난다).
const MAPSEL_MAX = 300;
let mapSelChips = [];   // [{serial,shot,dut,xpos,ypos,bin,source,x,y, key, color, items:{subject:{value,cum_pct}}}]
let _mapSelLastQ = { serial: "", xpos: "", ypos: "" };  // 직전 검색 필드값 — 추가 후 검색 패널 재실행에 재사용(연속 추가 편의).
let _mapSelResults = []; // 직전 검색 결과 chip 배열 — 체크박스 index → chip 매핑용.

function mapSelChipKey(c) { return `${c.source || ""}|${c.serial || ""}|${c.xpos || ""}|${c.ypos || ""}`; }
// 색 결정 단일 지점 — 기본은 전 chip 단일 옥색. 팔레트 순환으로 되돌리려면 여기만 보면 된다.
function mapSelColorAt(i) {
  return MAPSEL_USE_PALETTE ? MAPSEL_PALETTE[i % MAPSEL_PALETTE.length] : MAPSEL_HL_COLOR;
}
function mapSelReassignColors() { mapSelChips.forEach((c, i) => { c.color = mapSelColorAt(i); }); }

// Honey 클라이언트 Excel Download 가 runJavaScript 로 읽어가는 선택 좌표 스냅샷.
// 선택 상태는 이 페이지 메모리에만 있어(서버·URL 미저장) 클라가 알 수 없으므로, 화면과
// 같은 강조를 xlsx 에 그리려면 여기서 넘겨야 한다. mapSelChips 내부 구조가 클라와 직접
// 묶이지 않도록 **필요한 필드만** 추린 계약을 이 함수 하나로 고정한다
// (Map 마커: source/x/y/color, CDF 마커: items[subject].{value,cum_pct}).
function honeyMapSelSnapshot() {
  return mapSelChips.map(c => ({
    source: c.source || "", color: c.color,
    x: c.x, y: c.y, xpos: c.xpos, ypos: c.ypos, serial: c.serial,
    items: c.items || {},
  }));
}
window.honeyMapSelSnapshot = honeyMapSelSnapshot;

// ── chip 마커 (CDF 위 위치 표시) ─────────────────────────────────────────────
// hits = [{color, value, cum}] → Plotly trace/shape. 점 크기·테두리·크로스헤어 규칙의
// **단일 진실**이라, 어느 차트(일반 Distribution / composite / Gap Chart)에서든 같은
// 모양으로 찍힌다. 크로스헤어는 "chip 도 1개, 이 차트에 찍힌 점도 1개" 일 때만 —
// 점이 여러 개인데 선을 그으면 어느 점의 선인지 알 수 없다.
//
// ⚠ `useGl` 은 **주변 곡선의 렌더 방식과 반드시 맞춰야 한다.** Plotly 는 scattergl 을
// 별도 WebGL 캔버스에 그리고 그 캔버스를 SVG 위에 얹으므로, gl 곡선 옆에 SVG(scatter)
// 마커를 두면 trace 순서와 무관하게 **데이터 점 아래로 깔려 안 보인다**. chip 마커는
// 정의상 곡선 위(같은 좌표)에 놓이므로 100% 가려진다 — Item detail 에서 마커가 안 보이던
// 원인이 이것이다(갤러리 카드는 점을 canvas 로 직접 그리며 마커도 같이 다시 그려서 보였다).
//
// ⚠ hit 마다 trace 를 만들지 말 것 — **차트당 trace 1개**다. Item_detail 드래그 선택이
// 생기면서 chip 이 수백 개가 될 수 있어, hit 당 trace 면 카드 30장 × chip 300개 = 9,000 개
// trace 가 되어 갤러리가 통째로 멈춘다. 소비처 canvas(distDrawPoints)도 trace 안의
// **모든 점**을 도는 전제라, 여기를 되돌리면 카드에 첫 점만 그려진다.
function mapSelMarkerTraces(hits, useGl, opts) {
  if (!hits || !hits.length) return null;
  const size = (opts && opts.size) || MAPSEL_MARKER_SIZE;
  const t = {
    type: useGl ? "scattergl" : "scatter", mode: "markers",
    x: hits.map(h => h.value), y: hits.map(h => h.cum),
    marker: { color: hits.map(h => h.color), size,
      line: { width: 1.5, color: MAPSEL_HL_LINE } },
    hoverinfo: "skip", showlegend: false };
  if (!useGl) t.cliponaxis = false;   // scattergl 미지원 속성
  const traces = [t];
  const shapes = [];
  if (mapSelChips.length === 1 && hits.length === 1) {
    const h = hits[0];
    shapes.push({ type: "line", x0: h.value, x1: h.value, yref: "paper", y0: 0, y1: 1,
      line: { color: h.color, width: 1, dash: "dot" } });
    shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, y0: h.cum, y1: h.cum,
      line: { color: h.color, width: 1, dash: "dot" } });
  }
  return { traces, shapes };
}

// 한 항목(subject)에 대해 선택된 모든 chip 의 위치 마커(각 chip 색).
// 해당 항목 값 없는 chip 은 건너뜀. 값·누적% 는 **서버가 준 것**(chip_percentiles)을 쓴다 —
// 같은 chip 이 어느 화면에 나오든 같은 좌표에 찍혀야 하기 때문(CLAUDE.md 규칙 13).
function chipMarkersFor(subject, useGl, opts) {
  if (!mapSelChips.length) return null;
  const hits = [];
  mapSelChips.forEach(c => {
    const it = c.items[subject];
    if (!it || typeof it.value !== "number" || typeof it.cum_pct !== "number") return;
    hits.push({ color: c.color, value: it.value, cum: it.cum_pct });
  });
  return mapSelMarkerTraces(hits, useGl, opts);
}

// Distribution composite 용 — pair(source×item) 목록 전체에 대한 마커를 **한 번에** 모은다.
// pair 마다 따로 만들면 크로스헤어 판정(점 1개)이 pair 수만큼 걸려 선이 여러 벌 생긴다.
// chip 은 특정 source 의 die 이므로 그 source 의 pair 에만 찍는다.
function chipMarkersForPairs(pairs, useGl, opts) {
  if (!mapSelChips.length) return null;
  const hits = [];
  (pairs || []).forEach(p => {
    mapSelChips.forEach(c => {
      if ((c.source || "") !== p.source) return;
      const it = c.items[p.item];
      if (!it || typeof it.value !== "number" || typeof it.cum_pct !== "number") return;
      hits.push({ color: c.color, value: it.value, cum: it.cum_pct });
    });
  });
  return mapSelMarkerTraces(hits, useGl, opts);
}

// 선택 변경 후 Distribution 소비처(보이는 갤러리 카드 + 열려있는 Item_detail) 재렌더.
// 갤러리 카드 셀렉터(.distg-card)에는 composite·Gap Chart 카드도 포함된다(같은 골격).
function applyChipToDistribution() {
  document.querySelectorAll('#panel-distribution .distg-card').forEach(c => { c.dataset.rendered = ""; });
  document.querySelectorAll('#panel-distribution .distg-card[data-visible="1"]').forEach(distQueueRender);
  // Item_detail 은 **칩 마커만 갈아끼운다** — 사용자가 맞춰 둔 zoom 을 보존하기 위함
  // (2026-09-04 요청: 확대해 놓고 그 안의 점을 강조하면 매번 zoom 이 풀렸다).
  // 전제가 안 맞으면(축 옵션 변경 등으로 차트 구조가 다르면) false 를 돌려주므로 종전대로
  // 전체 재렌더한다 — 강조가 화면에 반영되지 않는 것보다 zoom 이 풀리는 편이 낫다.
  if (_itemDetailData) {
    if (!idetUpdateChipMarkers()) distRenderCdf(_itemDetailData);
    renderIdetChipVals();
  }
  // 편집바의 '강조 N' 카운트도 같이 맞춘다(Item_detail 이 열려 있을 때만 요소가 있다).
  if (typeof renderCdfEditBar === "function") renderCdfEditBar();
  // composite 상세는 별도 패널이라 갤러리 재렌더에 걸리지 않는다.
  if (typeof _dcDetailId !== "undefined" && _dcDetailId) dcRenderDetailCharts();
}

// Map 패널 갱신 — **보이고 있을 때만** 즉시 다시 그린다. Item_detail 에서 드래그로 좌표를
// 고르는 동안 Map 패널은 화면에 없는데, 매번 renderMapAnalysis() 를 부르면 안 보이는
// 썸네일 canvas 를 소스 수만큼 다시 그리게 된다. 안 보일 때는 dirty 표시만 남기고
// edit_mode.js renderTab 이 탭에 들어갈 때 그린다(wafer_charts.js 의 기존 패턴과 동일).
function mapSelRefreshMap() {
  const p = document.getElementById("panel-map-analysis");
  if (p && p.classList.contains("active")) renderMapAnalysis();
  else if (typeof tabDirty !== "undefined") tabDirty["map-analysis"] = true;
}

// 좌표 검색 패널의 펼침 상태 — 좌표를 추가/해제하면 renderMapAnalysis 가 Map 패널을
// 통째로 다시 그려 패널이 닫혀 버렸다. 상태를 여기서 기억하고 재렌더 후 되살려,
// 열고 닫는 것은 '좌표 선택'/'접기 ▲' 두 버튼으로만 하게 한다 (2026-08-14 요청).
let _mapSelBoxOpen = false;

// Map Analysis 툴바 '좌표 선택' → 검색 패널 토글.
function mapSelToggleSearch() { mapSelSetSearchOpen(!_mapSelBoxOpen); }

function mapSelSetSearchOpen(open) {
  _mapSelBoxOpen = !!open;
  const box = document.getElementById("mapSelSearchBox");
  if (box) box.style.display = _mapSelBoxOpen ? "" : "none";
  if (_mapSelBoxOpen) { ensureDistData(); const inp = document.getElementById("mapSelSerial"); if (inp) inp.focus(); }
}

// Map 패널 재렌더 직후 호출 — 펼쳐져 있었으면 그대로 펼치고 마지막 검색어·결과까지
// 되살린다(추가·해제된 좌표가 체크 상태에 반영된다). 닫혀 있었으면 아무것도 안 한다.
function mapSelRestoreSearchBox() {
  const box = document.getElementById("mapSelSearchBox");
  if (!box || !_mapSelBoxOpen) return;
  box.style.display = "";
  const q = _mapSelLastQ || {};
  const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ""; };
  setVal("mapSelSerial", q.serial); setVal("mapSelXpos", q.xpos); setVal("mapSelYpos", q.ypos);
  if (q.serial || q.xpos || q.ypos) mapSelSearch();
}

// 좌표 검색(serial 부분일치 / xpos·ypos 정확일치, AND) → 후보 목록(체크박스). 여러 개 체크 후 '선택 추가' 로 일괄 추가.
// 행 아무 곳이나 클릭하면 그 행 체크박스가 토글되고, 헤더 체크박스로 전체 선택 가능.
function mapSelSearch() {
  const list = document.getElementById("mapSelList");
  const info = document.getElementById("mapSelInfo");
  if (!list) return;
  const serial = ((document.getElementById("mapSelSerial") || {}).value || "").trim();
  const xpos = ((document.getElementById("mapSelXpos") || {}).value || "").trim();
  const ypos = ((document.getElementById("mapSelYpos") || {}).value || "").trim();
  _mapSelLastQ = { serial, xpos, ypos };
  list.innerHTML = `<div class="placeholder">검색 중...</div>`;
  const p = new URLSearchParams({ serial, xpos, ypos });
  const url = `/pe/report/session/${SESSION_ID}/web_report/commonality/chips?${p.toString()}`;
  fetch(url, { cache: "no-cache" })
    .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(j => {
      const chips = j.chips || [];
      _mapSelResults = chips;
      if (info) info.textContent = `${chips.length}개${j.truncated ? "+ (잘림)" : ""}`;
      if (!chips.length) { list.innerHTML = `<div class="placeholder">일치하는 chip 없음</div>`; updateMapSelAddBtn(); return; }
      const head = `<table class="sheet-table common-chip-table"><thead><tr>
        <th class="common-chk-col"><input type="checkbox" id="mapSelChkAll" title="전체 선택"></th>
        <th>SOURCE</th><th>SERIAL</th><th>XPOS</th><th>YPOS</th><th>DUT</th><th>BIN</th></tr></thead><tbody>`;
      const body = chips.map((c, i) => {
        const added = mapSelChips.some(x => x.key === mapSelChipKey(c));
        return `<tr class="common-chip-row${added ? " common-chip-added" : ""}" data-i="${i}">
          <td class="common-chk-col"><input type="checkbox" class="mapsel-chk" data-i="${i}"${added ? " checked disabled" : ""}></td>
          <td>${esc(c.source || "")}</td>
          <td>${esc(c.serial)}</td><td class="num">${esc(c.xpos)}</td><td class="num">${esc(c.ypos)}</td>
          <td class="num">${esc(c.dut)}</td><td class="num">${esc(c.bin)}</td></tr>`;
      }).join("");
      list.innerHTML = head + body + `</tbody></table>`;
      list.querySelectorAll(".common-chip-row").forEach(tr => {
        tr.addEventListener("click", e => {
          const chk = tr.querySelector(".mapsel-chk");
          if (!chk || chk.disabled) return;
          if (e.target !== chk) chk.checked = !chk.checked;   // 체크박스 자체 클릭은 기본동작
          updateMapSelAddBtn();
        });
      });
      const chkAll = list.querySelector("#mapSelChkAll");
      if (chkAll) chkAll.addEventListener("click", e => {
        e.stopPropagation();
        list.querySelectorAll(".mapsel-chk:not(:disabled)").forEach(c => { c.checked = chkAll.checked; });
        updateMapSelAddBtn();
      });
      updateMapSelAddBtn();
    })
    .catch(e => { list.innerHTML = `<div class="placeholder">검색 실패: ${esc(e.message)}</div>`; updateMapSelAddBtn(); });
}

// 체크된 개수로 '선택 추가' 버튼 라벨/활성 상태 갱신.
function updateMapSelAddBtn() {
  const btn = document.getElementById("mapSelAddSelected");
  if (!btn) return;
  const list = document.getElementById("mapSelList");
  const n = list ? list.querySelectorAll(".mapsel-chk:not(:disabled):checked").length : 0;
  btn.textContent = n ? `선택 ${n}개 추가` : "선택 추가";
  btn.disabled = !n;
}

// chip 여러 개를 **한 요청**으로 조회해 선택에 추가한다. 반환 {added, missing, cut}.
//
// chip 마다 GET /commonality/chip 을 순차로 부르던 종전 방식은 Map 검색(몇 개)에는 맞았지만
// Item_detail 드래그(수십~수백 개)에는 못 쓴다 — chip 수만큼 왕복이 쌓인다. 서버가 같은
// 인덱스 위에서 한 번에 계산하므로(chips_lookup) 새 계산은 없다.
// 못 찾은 chip 은 서버가 null 로 돌려주며 나머지는 그대로 추가된다(하나 때문에 전체 실패 금지).
async function mapSelAddChips(chipsIn) {
  const have = new Set(mapSelChips.map(c => c.key));
  const seen = new Set();
  const todo = [];
  (chipsIn || []).forEach(c => {
    const key = mapSelChipKey(c);
    if (have.has(key) || seen.has(key)) return;   // 이미 선택됐거나 입력 안에서 중복
    seen.add(key);
    todo.push({ source: c.source || "", serial: c.serial == null ? "" : String(c.serial),
      xpos: c.xpos == null ? "" : String(c.xpos), ypos: c.ypos == null ? "" : String(c.ypos), key });
  });
  const room = Math.max(MAPSEL_MAX - mapSelChips.length, 0);
  let cut = 0;
  if (todo.length > room) { cut = todo.length - room; todo.length = room; }
  if (!todo.length) {
    if (cut) showToast(`강조 좌표는 최대 ${MAPSEL_MAX}개입니다`);
    return { added: 0, missing: 0, cut };
  }
  const resp = await fetch(`/pe/report/session/${SESSION_ID}/web_report/commonality/chips_lookup`, {
    method: "POST", cache: "no-cache", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chips: todo.map(c => ({ source: c.source, serial: c.serial, xpos: c.xpos, ypos: c.ypos })) }),
  });
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  const j = await resp.json();
  let added = 0, missing = 0;
  (j.chips || []).forEach((res, i) => {
    if (!res) { missing++; return; }
    const names = (j.item_lists || [])[res.items_ref] || [];
    const items = {};
    names.forEach((nm, k) => { items[nm] = { value: res.value[k], cum_pct: res.cum_pct[k] }; });
    mapSelChips.push({ ...(res.chip || {}), key: todo[i].key, items });
    added++;
  });
  mapSelReassignColors();
  mapSelRefreshMap();           // Map 강조 반영(보일 때만 즉시)
  applyChipToDistribution();    // Distribution 카드+상세 재렌더
  return { added, missing, cut };
}

// 좌표 1개 토글 — 이미 강조 중이면 해제, 아니면 추가 (Item_detail 점 클릭).
function mapSelToggle(chip) {
  const key = mapSelChipKey(chip);
  if (mapSelChips.some(c => c.key === key)) { mapSelRemove(key); return Promise.resolve({ removed: 1 }); }
  return mapSelAddChips([chip]);
}

// 체크된 좌표를 일괄 추가 → 색 재배정 → Map 강조 + Distribution 재렌더 → 검색결과 갱신.
async function mapSelAddSelected() {
  const list = document.getElementById("mapSelList");
  if (!list) return;
  const chips = Array.from(list.querySelectorAll(".mapsel-chk:not(:disabled):checked"))
    .map(chk => _mapSelResults[Number(chk.dataset.i)]).filter(Boolean);
  if (!chips.length) { showToast("선택된 좌표가 없습니다"); return; }
  const btn = document.getElementById("mapSelAddSelected");
  if (btn) { btn.disabled = true; btn.textContent = "추가 중..."; }
  try {
    const r = await mapSelAddChips(chips);
    // 검색 패널 복원(추가된 항목 disabled 반영)은 renderMapAnalysis 끝의
    // mapSelRestoreSearchBox 가 이미 했다.
    showToast(`${r.added}개 추가` +
      (r.missing ? ` · ${r.missing}개 못 찾음` : "") +
      (r.cut ? ` · ${r.cut}개 상한 초과` : ""));
  } catch (e) {
    console.warn("chip 조회 실패", e);
    showToast(`좌표 추가 실패 (${e.message || "네트워크 오류"})`);
  } finally {
    updateMapSelAddBtn();
  }
}

function mapSelRemove(key) {
  mapSelChips = mapSelChips.filter(c => c.key !== key);
  mapSelReassignColors();
  mapSelRefreshMap();
  applyChipToDistribution();
}

function mapSelClear() {
  mapSelChips = [];
  mapSelRefreshMap();
  applyChipToDistribution();
}

// Major Fail Bin: 전체 average(모든 소스 fail rate 평균) 기준 상위 N개(고정).
const MAJOR_FAIL_TOP_N = 5;
// 전체 Yield(세로 병합 셀) + 소스별 Yield + 전체 average 기준 Major Fail Bins Top5 를 한 테이블로.
// 데이터: yield_summary(전체·소스별 yield%) + sheets["Yield"](행별 avg).
function majorFailBinsTableHtml() {
  const ov = DATA.web_report && DATA.web_report.yield_summary;
  if (!ov) return `<div class="placeholder">Yield 데이터 없음</div>`;
  const sheets = webReportSheets() || {};
  const fmtPct = v => (typeof v === "number" ? v.toFixed(2) : v);

  // 소스별 Yield (높은 순 — DUT 모드는 DUT 번호 오름차순).
  const sources = orderSummarySources(ov.by_source);

  // 전체 average 기준 Major Fail Bin Top N (Pass 제외).
  const majors = (sheets["Yield"] || [])
    .filter(r => String(r.bin) !== "1")
    .map(r => ({ item: r.Item, bin: r.bin, rate: Number(r.avg) || 0 }))
    .sort((a, b) => b.rate - a.rate)
    .slice(0, MAJOR_FAIL_TOP_N);

  const rowCount = Math.max(sources.length, majors.length, 1);
  const basisBySrc = yieldBasisBySource();
  let body = "";
  for (let i = 0; i < rowCount; i++) {
    const cells = [];
    if (i === 0) cells.push(`<td class="mfb-yield" rowspan="${rowCount}">${esc(fmtPct(ov.yield_pct))}%</td>`);
    const s = sources[i];
    // 분모 기준은 소스마다 다를 수 있어(Gross Die / Test data) 툴팁으로 병기한다.
    const bi = s ? basisBySrc.get(String(s.source)) : null;
    const bTip = bi ? ` title="분모 ${esc(bi.basis === "gross" ? "Gross Die" : "Test data")} `
      + `${esc(bi.total)} · ${esc(yieldBasisReasonText(bi))}"` : "";
    cells.push(s
      ? `<td class="mfb-src">${esc(s.source)}</td><td class="mfb-syield"${bTip}>${esc(fmtPct(s.yield_pct))}%</td>`
      : `<td class="mfb-src"></td><td class="mfb-syield"></td>`);
    // Yield 블록과 Major Fail Bins 블록 사이의 빈 칸(테두리 없음) — 첫 행에서 rowspan 으로
    // 한 번만 낸다. 컬럼 순서는 Bin → Item → Fail Rate (2026-08-11 요청).
    if (i === 0) cells.push(`<td class="mfb-gap" rowspan="${rowCount}"></td>`);
    const m = majors[i];
    cells.push(m
      ? `<td class="mfb-bin">${esc(m.bin)}</td><td class="mfb-item">${esc(m.item)}</td>` +
        `<td class="mfb-rate">${esc(fmtPct(m.rate))}%</td>`
      : `<td class="mfb-bin"></td><td class="mfb-item"></td><td class="mfb-rate"></td>`);
    body += `<tr>${cells.join("")}</tr>`;
  }

  return `<div class="mfb-wrap"><table class="mfb-table">
    <thead>
      <tr><th rowspan="2">전체 Yield</th><th colspan="2">Source 별 Yield</th>
        <th class="mfb-gap" rowspan="2"></th><th colspan="3">Major Fail Bins</th></tr>
      <tr><th>Source</th><th>Yield</th><th>Bin</th><th>Item</th><th>Fail Rate</th></tr>
    </thead>
    <tbody>${body}</tbody>
  </table></div>`;
}

// Issue Table 카테고리별(Yield/CPK/ETC) Open/Close 카운트 — Status 가 채워진 이슈 행
// (Yield 대표행/CPK 행/ETC 행)만 집계한다. Status=="" 행(Pass/상세/서브헤더/placeholder)과
// 숨김 행(서버가 이미 제외)은 자동 비대상. 섹션 추적은 sheets.js rowSection 과 동일 로직.
function issueStatusCounts() {
  const counts = {
    Yield: { open: 0, close: 0 }, CPK: { open: 0, close: 0 },
    TEMP: { open: 0, close: 0 }, ETC: { open: 0, close: 0 },
    // Compare 모드 전용 시트의 두 섹션 — 화면에는 "Compare" 한 줄로 합산해 보여준다.
    CMPDIST: { open: 0, close: 0 }, CMPETC: { open: 0, close: 0 },
  };
  // Issue Table + (Temperature 면) Issue Table Temp + (Compare 면) Issue Table Compare 를
  // 같은 규칙으로 훑는다 — 섹션이 별도 시트로 빠졌으므로 여기서 합산해야 카드 값이 맞는다.
  // 시트마다 섹션 키가 달라(ETC ↔ CMPETC) 서로 섞이지 않는다.
  const sheets = [(DATA && Array.isArray(DATA.issue_table_text)) ? DATA.issue_table_text : []];
  const temp = (webReportSheets() || {})["Issue Table Temp"];
  if (Array.isArray(temp) && temp.length) sheets.push(temp);
  const cmp = (webReportSheets() || {})[ISSUE_CMP_SHEET];
  if (Array.isArray(cmp) && cmp.length) sheets.push(cmp);
  sheets.forEach(rows => {
    let sec = "";
    rows.forEach(r => {
      if (r && r["Category"]) sec = String(r["Category"]);
      const st = String((r && r["Status"]) || "");
      if (!st || !counts[sec]) return;
      counts[sec][st === "Close" ? "close" : "open"]++;
    });
  });
  return counts;
}

// Summary 의 Issue Status 카드(카테고리별 Open/Close + 진행률 소표) — 클릭 시 Issue Table 탭 이동.
// 진행률 = Close / (Open + Close) * 100 (소수 1자리). 이슈 행이 없는 카테고리는 "-".
function issueStatusCardHtml() {
  const counts = issueStatusCounts();
  const mode = webReportMode();
  const cats = (mode === "Temperature")
    ? ["Yield", "CPK", "TEMP", "ETC"] : ["Yield", "CPK", "ETC"];
  // Compare 모드면 그 표의 두 섹션을 "Compare" 한 줄로 합쳐 잇는다.
  if (mode === "Compare") {
    counts.Compare = { open: counts.CMPDIST.open + counts.CMPETC.open,
                       close: counts.CMPDIST.close + counts.CMPETC.close };
    cats.push("Compare");
  }
  const rows = cats.map(cat => {
    const c = counts[cat];
    const total = c.open + c.close;
    const prog = total ? (c.close / total * 100).toFixed(1) + "%" : "-";
    // TEMP/Compare 행은 Issue Table 이 아니라 각자의 탭에 있다 — 행 자체를 그 탭으로
    // 보내는 점프 대상으로 만든다(카드 기본 점프는 issues 라 클릭해도 없는 표로 갔었다).
    const jump = (cat === "TEMP")
      ? ` class="summary-jump" data-jump="issue-temp" title="Issue Table Temp 탭으로 이동"`
      : (cat === "Compare"
        ? ` class="summary-jump" data-jump="issue-cmp" title="Issue Table Compare 탭으로 이동"`
        : "");
    return `<tr${jump}><td class="iss-cat">${cat}</td>` +
      `<td class="iss-open${c.open ? "" : " st-empty"}">${c.open}</td>` +
      `<td class="iss-close${c.close ? "" : " st-empty"}">${c.close}</td>` +
      `<td class="iss-prog${total ? "" : " st-empty"}">${prog}</td></tr>`;
  }).join("");
  return `<div class="summary-section-card summary-jump" data-jump="issues" title="Issue Table 탭으로 이동">` +
    `<div class="section-title">Issue Status <span class="summary-jump-hint">▸ 탭 이동</span></div>` +
    `<div class="iss-status-wrap"><table class="iss-status-table">` +
    `<thead><tr><th>구분</th><th>Open</th><th>Close</th><th>진행률</th></tr></thead>` +
    `<tbody>${rows}</tbody></table></div></div>`;
}

// Summary 의 Compare 요약 카드 (Compare 모드 전용) — 클릭 시 Issue Table Compare 탭 이동.
// 수치는 전부 서버 payload 를 **그대로 읽는다**: focus 판정은 compare.py 가 정본이고
// (dist_shift.summary.focus), Bin 불일치는 bin_matrix.counts.mismatch 다. Log 3종만
// 표시 계층 집계(goodlogRowType — compare.js 의 이상 요약 칩과 같은 함수)를 쓴다.
function compareSummaryCardHtml() {
  if (webReportMode() !== "Compare") return "";
  const wr = DATA.web_report || {};
  const cmp = wr.compare;
  const shell = inner =>
    `<div class="summary-section-card summary-jump" data-jump="issue-cmp" ` +
    `title="Issue Table Compare 탭으로 이동">` +
    `<div class="section-title">Compare <span class="summary-jump-hint">▸ 탭 이동</span></div>` +
    inner + `</div>`;
  if (!cmp) {
    return shell(`<div class="placeholder">${wr.compare_pending
      ? "⏳ Compare 계산 중… 끝나면 자동으로 표시됩니다."
      : "Compare 데이터 없음 (같은 Wafer source 2개 이상 필요)"}</div>`);
  }
  const focus = ((cmp.dist_shift || {}).summary || {}).focus || 0;
  const total = ((cmp.dist_shift || {}).summary || {}).total || 0;
  const mismatch = ((cmp.bin_matrix || {}).counts || {}).mismatch || 0;
  const newN = (cmp.new_items || []).length;
  let removed = 0, limitchg = 0;
  ((cmp.goodlog || {}).rows || []).forEach(r => {
    const t = goodlogRowType(r);
    if (t === "removed") removed++;
    else if (t === "limitchg") limitchg++;
  });
  const row = (label, value, sub) =>
    `<tr><td class="iss-cat">${label}</td>` +
    `<td class="iss-open${value ? "" : " st-empty"}">${value}</td>` +
    `<td class="cmp-sum-sub">${sub}</td></tr>`;
  return shell(
    `<div class="iss-status-wrap"><table class="iss-status-table">` +
    `<thead><tr><th>구분</th><th>건수</th><th>비고</th></tr></thead><tbody>` +
    row("산포 검출", focus, `공통 항목 ${total}개 중`) +
    row("신규 item", newN, "After 에만 있음") +
    row("삭제 item", removed, "Before 에만 있음") +
    row("Limit 변경", limitchg, "이름·규격 변경 포함") +
    row("Bin 불일치", mismatch, "공통 좌표 die") +
    `</tbody></table></div>`);
}

let summaryJumpBound = false;
function renderWebSummary() {
  const panel = document.getElementById("panel-summary");
  const sheets = webReportSheets();
  // Plotly 를 요구하지 않는다 — 이 패널은 표 HTML + textarea 만 그린다(차트 시절의 잔재였음).
  // plotly.min.js 는 async 라 /full 보다 늦게 도착할 수 있고, renderTab 은 summary 를
  // PLOTLY_TABS 로 대기시키지 않은 채 dirty 를 이미 내려버려 재렌더도 안 됐다
  // → 간헐적으로 "Summary 데이터 없음" 만 뜨던 원인.
  if (!sheets) { emptyPanel(panel, "Summary 데이터 없음"); return; }
  // Summary 카드(Yield) 클릭 → 해당 탭 버튼 클릭 재사용(1회 위임 바인딩).
  if (!summaryJumpBound) {
    panel.addEventListener("click", (e) => {
      // Note 시트 버튼 → Note 탭 + 해당 시트 (Engr Comment 의 $[시트명] 과 같은 경로).
      const sheetBtn = e.target.closest(".note-sheet-btn");
      if (sheetBtn) { noteJumpToSheet(sheetBtn.dataset.sheetName); return; }
      const card = e.target.closest(".summary-jump");
      if (!card) return;
      const tabBtn = document.querySelector(`.tab[data-tab="${card.dataset.jump}"]`);
      if (tabBtn) tabBtn.click();
    });
    summaryJumpBound = true;
  }
  panel.classList.add("viz-root");
  const engr = (DATA.web_report && DATA.web_report.summary_engr) || {};
  // Engr Comment 편집은 다른 web_report 편집과 동일하게 업로더(edit 모드)만 허용한다.
  // view 모드에서는 읽기전용으로 렌더하고 저장 바인딩을 하지 않는다(저장 요청은 서버가 거부).
  const engrEditable = (MODE === "edit");
  panel.innerHTML =
    `<div class="summary-section-card summary-jump" data-jump="yield" title="Yield 탭으로 이동">` +
    `<div class="section-title">Yield <span class="summary-jump-hint">▸ 탭 이동</span></div>` + majorFailBinsTableHtml() +
    `</div>` +
    compareSummaryCardHtml() +
    issueStatusCardHtml() +
    `<div class="summary-section-card">` +
    `<div class="section-title">Engr Comment</div>` +
    `<div class="engr-comment-grid">` +
    engrCommentFields().map(f =>
      `<label class="engr-comment-label" for="engr-${f.key}">${f.label}</label>` +
      (engrEditable
        // 편집: contenteditable 편집칸(글자 크기·색을 그 자리에서 보며 편집) + 위쪽 서식
        // 도구모음. 태그(@[..] 등)는 편집 중 원문으로 두고 아래 링크 칩 줄로 클릭한다.
        // @/#/$ 자동완성은 edit_mode.js 가 붙인다.
        ? `<div class="engr-comment-cell">` +
          engrFmtBarHtml(f.key) +
          `<div id="engr-${f.key}" class="engr-comment-input" contenteditable="true" ` +
          `data-engr="${f.key}">${engrEditorHtml(engr[f.key])}</div>` +
          `<div class="engr-comment-links" data-engr-links="${f.key}" hidden></div>` +
          `</div>`
        // 조회: 편집이 없으니 본문 자체를 링크·서식으로 그린다.
        : `<div class="engr-comment-view">${engrValueHtml(engr[f.key])}</div>`)).join("") +
    `</div>` +
    `<div class="engr-note-jump" id="engrNoteJump" hidden></div>` +
    `</div>`;
  if (engrEditable) bindEngrComment(panel);
  renderEngrNoteJump();
}

// Engr Comment 안의 @[..]/#[..]/$[..] 토큰만 뽑아 클릭 가능한 칩으로 나열한다.
// textarea 는 HTML 을 못 그리므로 링크를 본문 밖에 두는 방식 — 클래스·.missing 판정은
// linkifyComment(sheets.js) 를 토큰 1개씩 통과시켜 재사용하고, 클릭은 .content 위임이 받는다.
function engrLinkChips(raw) {
  const re = /([@#$])\[([^\]]+)\]/g, seen = new Set();
  let out = "", m;
  while ((m = re.exec(String(raw || "")))) {
    if (seen.has(m[0])) continue;
    seen.add(m[0]);
    out += linkifyComment(m[0]);
  }
  return out;
}
function renderEngrChips(key) {
  const ta = document.getElementById(`engr-${key}`);
  const box = document.querySelector(`[data-engr-links="${key}"]`);
  if (!ta || !box) return;
  // 편집칸은 contenteditable 이므로 값이 아니라 표시 텍스트에서 토큰을 뽑는다.
  const html = engrLinkChips(ta.textContent || "");
  box.innerHTML = html;
  box.hidden = !html;
}

// Engr Comment 아래 Note 시트 버튼 줄 — 시트 이름만 받는 경량 라우트를 쓰고,
// Note 가 없는 세션에서는 요청조차 하지 않는다.
function renderEngrNoteJump() {
  const box = document.getElementById("engrNoteJump");
  if (!box) return;
  if (!(DATA && DATA.note_info && DATA.note_info.exists)) { box.hidden = true; return; }
  const list = noteSheetNames();
  if (list === null) { noteEnsureSheetList().then(renderEngrNoteJump); return; }
  box.hidden = !list.length;
  box.innerHTML = list.length
    ? `<span class="engr-note-jump-label">📄 Note 시트</span>` +
      list.map(s => `<button type="button" class="note-sheet-btn" data-sheet-name="${esc(s.name)}" ` +
        `title="Note 탭의 이 시트로 이동">${esc(s.name)}</button>`).join("")
    : "";
}

// Summary 탭 Engr Comment 칸 정의 (manifest.summary_engr 키와 일치).
// TEMP 는 Temperature 모드에서만, Compare 는 Compare 모드에서만 — Issue Status 카드의
// 해당 행과 같은 기준이다(webReportMode()). 상수가 아니라 함수인 이유: 모드는 DATA 가
// 온 뒤에야 안다. **저장 키는 모드와 무관하게 서버가 다 받는다**(service._ENGR_KEYS) —
// 모드가 바뀐 세션의 기존 값이 저장 불가로 막히지 않게.
const ENGR_COMMENT_FIELDS = [
  { key: "yield", label: "Yield" },
  { key: "cpk", label: "CPK" },
  { key: "etc", label: "ETC" },
];
function engrCommentFields() {
  const mode = webReportMode();
  if (mode === "Temperature") {
    return [
      { key: "yield", label: "Yield" },
      { key: "cpk", label: "CPK" },
      { key: "temp", label: "TEMP" },
      { key: "etc", label: "ETC" },
    ];
  }
  if (mode === "Compare") {
    return [
      { key: "yield", label: "Yield" },
      { key: "cpk", label: "CPK" },
      { key: "compare", label: "Compare" },
      { key: "etc", label: "ETC" },
    ];
  }
  return ENGR_COMMENT_FIELDS;
}

// ── Engr Comment 서식(글자 크기·색) ──────────────────────────────────────────
// 저장값은 종전과 같은 **문자열 1개**다. 서식이 붙은 값만 선두에 마커를 달고 제한된 HTML 을
// 담고, 마커가 없으면 예전 그대로 평문으로 읽는다 → 기존 세션 값은 그대로 보인다(§5-12).
// 서식을 하나도 안 쓴 편집 결과는 다시 평문으로 되돌려 저장하므로, 서식을 안 쓰는 사용자의
// 저장값 모양은 종전과 같다.
// ⚠️ 화면에 그리기 직전에 **항상** engrSanitize 를 통과시킨다 — 저장 시 걸러도 남의 브라우저에
// 그려지는 값이라 렌더 쪽 필터가 실제 방어선이다(script/on*/href 는 전부 버린다).
const ENGR_RICH_MARK = "<!--rich-->";
const ENGR_TAGS = { SPAN: 1, B: 1, STRONG: 1, I: 1, EM: 1, U: 1, BR: 1, DIV: 1, P: 1 };
// 이 태그들은 **속 내용까지 통째로** 버린다 — 모르는 태그는 글자를 살리는 게 원칙이지만,
// 이들의 내용은 글이 아니라 코드라 살리면 "alert(1)" 같은 문자열이 코멘트 본문으로 남는다.
const ENGR_DROP_TAGS = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, IFRAME: 1, OBJECT: 1, EMBED: 1 };
const ENGR_STYLE_PROPS = ["color", "background-color", "font-size", "font-weight"];
const ENGR_COLOR_RE = /^(#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\))$/i;
const ENGR_SIZE_RE = /^\d{1,3}(\.\d+)?px$/;
// 도구모음 항목 — 크기 4단(기본 14px)과 색 5종. 값은 그대로 style 에 들어가므로 위 정규식을
// 통과하는 형태여야 한다.
const ENGR_FMT_SIZES = [["작게", "12px"], ["기본", "14px"], ["크게", "18px"], ["특대", "24px"]];
const ENGR_FMT_COLORS = [["#111827", "검정"], ["#d92d20", "빨강"], ["#ea580c", "주황"],
                         ["#12805c", "초록"], ["#1d4ed8", "파랑"]];

function engrFmtBarHtml(key) {
  return `<div class="engr-fmt-bar" data-engr-bar="${key}">` +
    ENGR_FMT_SIZES.map(([label, px]) =>
      `<button type="button" class="engr-fmt-btn" data-engr-size="${px}" ` +
      `title="글자 크기 ${label}(${px})">${label}</button>`).join("") +
    `<span class="engr-fmt-sep"></span>` +
    ENGR_FMT_COLORS.map(([hex, name]) =>
      `<button type="button" class="engr-fmt-btn engr-fmt-color" data-engr-color="${hex}" ` +
      `style="background:${hex}" title="글자색 ${name}"></button>`).join("") +
    `<span class="engr-fmt-sep"></span>` +
    `<button type="button" class="engr-fmt-btn" data-engr-cmd="bold" title="굵게"><b>B</b></button>` +
    `<button type="button" class="engr-fmt-btn" data-engr-cmd="italic" title="기울임"><i>I</i></button>` +
    `<button type="button" class="engr-fmt-btn" data-engr-cmd="underline" title="밑줄"><u>U</u></button>` +
    `<button type="button" class="engr-fmt-btn" data-engr-cmd="clear" title="선택 구간 서식 지우기">✕ 서식</button>` +
    `<span class="engr-fmt-hint">글자를 드래그해 선택한 뒤 누르세요</span>` +
    `</div>`;
}

// 새 문서에서 파싱한다 — 이 문서는 렌더되지 않아 파싱 단계에서 스크립트/이미지가 돌지 않는다.
function _engrDoc() { return new DOMParser().parseFromString("<body></body>", "text/html"); }

// 허용 style 속성만 남긴 style 문자열 (값 형식까지 검사).
function engrSafeStyle(el) {
  const out = [];
  ENGR_STYLE_PROPS.forEach(p => {
    const v = String(el.style.getPropertyValue(p) || "").trim();
    if (!v) return;
    if ((p === "color" || p === "background-color") && !ENGR_COLOR_RE.test(v)) return;
    if (p === "font-size" && !ENGR_SIZE_RE.test(v)) return;
    if (p === "font-weight" && !/^(bold|[1-9]00)$/i.test(v)) return;
    out.push(`${p}:${v}`);
  });
  return out.join(";");
}
// 허용 태그·style 만 남긴 복사본을 만든다. 모르는 태그는 껍데기만 버리고 **글자는 살린다**
// (버리면 사용자가 붙여넣은 글이 사라진다 — §5-12).
function engrSanitizeInto(src, out, doc) {
  Array.from(src.childNodes).forEach(n => {
    if (n.nodeType === 3) { out.appendChild(doc.createTextNode(n.nodeValue)); return; }
    if (n.nodeType !== 1) return;                       // 주석 등은 버린다
    if (ENGR_DROP_TAGS[n.tagName]) return;              // 내용까지 통째로 버린다
    if (n.tagName === "BR") { out.appendChild(doc.createElement("br")); return; }
    if (!ENGR_TAGS[n.tagName]) { engrSanitizeInto(n, out, doc); return; }
    const el = doc.createElement(n.tagName.toLowerCase());
    const style = engrSafeStyle(n);
    if (style) el.setAttribute("style", style);
    engrSanitizeInto(n, el, doc);
    out.appendChild(el);
  });
}
function engrSanitize(html) {
  const doc = _engrDoc();
  const src = doc.createElement("div");
  src.innerHTML = String(html == null ? "" : html);
  const out = doc.createElement("div");
  engrSanitizeInto(src, out, doc);
  return out.innerHTML;
}
// 제한 HTML 안의 **텍스트 노드만** linkifyComment 를 태운다 — 문자열 전체에 정규식을 돌리면
// 태그 속성까지 건드려 서식이 깨진다.
function engrLinkifyHtml(html) {
  const doc = _engrDoc();
  const root = doc.createElement("div");
  root.innerHTML = html;
  const walk = node => {
    Array.from(node.childNodes).forEach(n => {
      if (n.nodeType === 1) { walk(n); return; }
      if (n.nodeType !== 3) return;
      const holder = doc.createElement("span");
      holder.innerHTML = linkifyComment(n.nodeValue);
      n.replaceWith.apply(n, Array.from(holder.childNodes));
    });
  };
  walk(root);
  return root.innerHTML;
}
// 저장값 → 조회 화면 HTML.
function engrValueHtml(value) {
  const s = String(value == null ? "" : value);
  if (s.indexOf(ENGR_RICH_MARK) !== 0) return linkifyComment(s);
  return engrLinkifyHtml(engrSanitize(s.slice(ENGR_RICH_MARK.length)));
}
// 저장값 → 편집칸 초기 HTML. 편집 중에는 토큰을 원문 그대로 둔다(링크로 바꾸면 되돌릴 수 없다).
function engrEditorHtml(value) {
  const s = String(value == null ? "" : value);
  if (s.indexOf(ENGR_RICH_MARK) === 0) return engrSanitize(s.slice(ENGR_RICH_MARK.length));
  return esc(s).replace(/\n/g, "<br>");
}
// 편집칸 DOM → 평문 (블록·<br> 을 줄바꿈으로 되돌린다).
function engrTextOf(node) {
  let out = "";
  Array.from(node.childNodes).forEach(n => {
    if (n.nodeType === 3) { out += n.nodeValue; return; }
    if (n.nodeType !== 1) return;
    if (n.tagName === "BR") { out += "\n"; return; }
    if ((n.tagName === "DIV" || n.tagName === "P") && out && !out.endsWith("\n")) out += "\n";
    out += engrTextOf(n);
  });
  return out;
}
// 편집칸 → 저장값. 서식이 하나도 없으면 평문으로, 있으면 마커+제한 HTML 로 저장한다.
function engrEditorValue(el) {
  const doc = _engrDoc();
  const root = doc.createElement("div");
  root.innerHTML = engrSanitize(el.innerHTML);
  const text = engrTextOf(root).replace(/\u00a0/g, " ").trim();
  if (!text) return "";                                   // 빈 값 = 삭제 (서버 규약)
  if (!root.querySelector("span, b, strong, i, em, u, [style]")) return text;
  return ENGR_RICH_MARK + root.innerHTML;
}

// 선택 구간에 서식을 적용한다. 도구모음 버튼은 mousedown 을 preventDefault 하므로
// 편집칸의 선택이 살아 있다(blur 가 안 난다).
function engrApplyFormat(key, fn) {
  const el = document.getElementById(`engr-${key}`);
  if (!el) return;
  const sel = window.getSelection();
  const range = (sel && sel.rangeCount) ? sel.getRangeAt(0) : null;
  if (!range || sel.isCollapsed || !el.contains(range.commonAncestorContainer)) {
    showToast("서식을 적용할 글자를 먼저 드래그해 선택하세요.");
    return;
  }
  fn(el);
  _dirty = true;
  renderEngrChips(key);
}
// 크기: execCommand("fontSize") 는 브라우저에 따라 <font size=7> 또는 span[font-size:키워드] 를
// 남긴다 — 둘 다 우리 규격(span[style=font-size:Npx])으로 정규화하고, 새로 감싼 구간 안쪽의
// 옛 크기는 지운다(안 지우면 안쪽 값이 이겨 크기가 안 바뀐 것처럼 보인다).
function engrSetSize(key, px) {
  engrApplyFormat(key, el => {
    document.execCommand("styleWithCSS", false, false);
    document.execCommand("fontSize", false, "7");
    el.querySelectorAll('font[size="7"]').forEach(f => {
      const span = document.createElement("span");
      span.style.fontSize = px;
      while (f.firstChild) span.appendChild(f.firstChild);
      f.replaceWith(span);
      span.querySelectorAll('[style*="font-size"]').forEach(d => d.style.removeProperty("font-size"));
    });
    el.querySelectorAll('span[style*="font-size"]').forEach(s => {
      if (!ENGR_SIZE_RE.test(s.style.fontSize || "")) {
        s.style.fontSize = px;
        s.querySelectorAll('[style*="font-size"]').forEach(d => d.style.removeProperty("font-size"));
      }
    });
  });
}
function engrSetColor(key, hex) {
  engrApplyFormat(key, () => {
    document.execCommand("styleWithCSS", false, true);
    document.execCommand("foreColor", false, hex);
  });
}
// 굵게는 styleWithCSS=true 로 span[font-weight:bold] 를 남긴다(허용 style 목록에 있음).
// 기울임·밑줄은 반대로 styleWithCSS=false 여야 한다 — true 면 브라우저가
// span[font-style/text-decoration] 을 만드는데 그 두 속성은 ENGR_STYLE_PROPS 에 없어
// 저장 직전 engrSanitize 가 통째로 지운다(=서식이 조용히 사라진다). false 면 <i>/<u>
// 태그가 남고 ENGR_TAGS 가 그대로 허용한다.
const ENGR_TAG_CMDS = { italic: 1, underline: 1 };
function engrRunCmd(key, cmd) {
  engrApplyFormat(key, () => {
    if (ENGR_TAG_CMDS[cmd]) {
      document.execCommand("styleWithCSS", false, false);
      document.execCommand(cmd);
      return;
    }
    document.execCommand("styleWithCSS", false, true);
    document.execCommand(cmd === "bold" ? "bold" : "removeFormat");
  });
}

// 편집칸 입력은 autoSave 경로로 저장 — dot/dirty/실패복원을 Issue comment 와 일원화하고,
// 탭 전환·페이지 이탈 시에도 autoSave 안전망이 ENGR 를 덮는다. contenteditable 은 change
// 이벤트가 없으므로 blur(focusout) 에서 저장한다.
function bindEngrComment(panel) {
  panel.querySelectorAll("[data-engr]").forEach(el => {
    el.addEventListener("focusout", () => { autoSave(); });
    // 방금 입력·선택한 태그가 blur 를 기다리지 않고 바로 클릭 가능해지도록 칩을 갱신한다
    // (≤2000자 정규식이라 debounce 없이 충분).
    el.addEventListener("input", () => { _dirty = true; renderEngrChips(el.dataset.engr); });
    renderEngrChips(el.dataset.engr);
  });
  // contenteditable 은 <label for> 로 포커스가 가지 않는다 — 라벨 클릭 동작을 유지한다.
  panel.querySelectorAll("label.engr-comment-label[for]").forEach(lb => {
    lb.addEventListener("click", () => {
      const el = document.getElementById(lb.getAttribute("for"));
      if (el) el.focus();
    });
  });
  panel.querySelectorAll(".engr-fmt-bar").forEach(bar => {
    // mousedown 을 막아야 편집칸이 blur 되지 않아 선택 구간이 유지된다.
    bar.addEventListener("mousedown", ev => { if (ev.target.closest("button")) ev.preventDefault(); });
    bar.addEventListener("click", ev => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      const key = bar.dataset.engrBar;
      if (btn.dataset.engrSize) engrSetSize(key, btn.dataset.engrSize);
      else if (btn.dataset.engrColor) engrSetColor(key, btn.dataset.engrColor);
      else if (btn.dataset.engrCmd) engrRunCmd(key, btn.dataset.engrCmd);
    });
  });
}

// Summary Engr Comment 저장: 3칸 현재값을 원본(DATA.web_report.summary_engr)과 비교해
// **바뀐 칸만** POST. 3칸을 통째로 보내면 동시 편집 시 내 화면에 남아있던 낡은 값이
// 상대가 방금 저장한 다른 칸을 덮어쓴다 (서버 update_summary_engr 는 온 키만 병합).
// 성공 시 DATA 에 반영해 재렌더 시 값 유지.
async function saveSummaryEngr(opts) {
  if (MODE !== "edit") return;   // 뷰 모드는 저장 시도 안 함(서버도 업로더만 허용).
  const panel = document.getElementById("panel-summary");
  if (!panel || !DATA || !DATA.web_report) return;
  const cur = DATA.web_report.summary_engr || {};
  const values = {};
  let changed = false;
  panel.querySelectorAll("[data-engr]").forEach(el => {
    const k = el.dataset.engr;
    const v = engrEditorValue(el);
    if (v !== String(cur[k] || "").trim()) { values[k] = v; changed = true; }
  });
  if (!changed) return;
  let res;
  try {
    res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/summary/engr`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ values }),
      keepalive: !!(opts && opts.keepalive),   // 언로드 중 autoSave 에서도 요청이 완료되게
    });
  } catch (err) {
    // toast 는 호출부(autoSave)가 채널명과 함께 한 번만 낸다 — 여기서 내면 이중 표시된다.
    throw new Error("네트워크 오류");
  }
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
  // 서버는 병합된 3칸 전체를 돌려준다. 폴백은 부분 payload 라 기존 값 위에 덮어 합친다.
  DATA.web_report.summary_engr = j.summary_engr || Object.assign({}, cur, values);
}

// 막대 위(바깥)엔 count 를 bold 로, 막대 안엔 yield_pct(%) 를 채워 넣는다.
function failBinCountText(rows) { return rows.map(r => `<b>${r.count}</b>`); }
function failBinPctAnnotations(x, rows) {
  return rows.map((r, i) => ({
    x: x[i], y: (r.count || 0) / 2,
    text: (typeof r.yield_pct === "number") ? `${r.yield_pct.toFixed(2)}%` : "",
    showarrow: false, xanchor: "center", yanchor: "middle",
    font: { color: "#fff", size: 13, family: PLOTLY_FONT.family },
  })).filter(a => a.text);
}
// x축(Bin·항목명) 라벨 가독성 공통 설정.
const FAILBIN_XAXIS = { tickangle: -30, automargin: true, tickfont: { size: 12, color: "#333" } };

function renderFailBinBar(divId, rows) {
  const el = document.getElementById(divId);
  if (!el) return;
  if (!window.Plotly || !rows || !rows.length) {
    el.innerHTML = `<div class="placeholder">Fail bin 데이터 없음</div>`; return;
  }
  const x = rows.map(r => r.item ? `Bin ${r.bin} : ${r.item}` : `Bin ${r.bin}`);
  const y = rows.map(r => r.count);
  const trace = {
    type: "bar", x, y, marker: { color: rows.map(r => binColor(r.bin)) },
    text: failBinCountText(rows), textposition: "outside", textfont: { size: 12, color: "#222" },
    customdata: rows.map(r => r.bin),
    hovertemplate: "%{x}<br>Bin %{customdata}<br>Count %{y}<extra></extra>",
  };
  const layout = {
    margin: { l: 44, r: 10, t: 14, b: 96 },
    paper_bgcolor: "#ffffff", plot_bgcolor: "#ffffff", font: PLOTLY_FONT,
    xaxis: FAILBIN_XAXIS,
    yaxis: { title: "Fail count", gridcolor: "#e1e0d9", zeroline: false },
    showlegend: false, bargap: 0.35, annotations: failBinPctAnnotations(x, rows),
  };
  Plotly.newPlot(divId, [trace], layout, { responsive: true, displayModeBar: false });
}

// Fail Bin 소스 합산 막대 차트: x축 라벨 "Bin xx : 항목명".
function renderPareto(divId, rows) {
  const el = document.getElementById(divId);
  if (!el) return;
  if (!window.Plotly || !rows || !rows.length) {
    el.innerHTML = `<div class="placeholder">데이터 없음</div>`; return;
  }
  const x = rows.map(r => r.item ? `Bin ${r.bin} : ${r.item}` : `Bin ${r.bin}`);
  const y = rows.map(r => r.count);
  const bar = {
    type: "bar", x, y,
    marker: { color: rows.map(r => binColor(r.bin)) }, customdata: rows.map(r => r.bin),
    text: failBinCountText(rows), textposition: "outside", textfont: { size: 12, color: "#222" },
    hovertemplate: "%{x}<br>Count %{y}<extra></extra>",
  };
  const layout = {
    margin: { l: 44, r: 10, t: 14, b: 96 },
    paper_bgcolor: "#ffffff", plot_bgcolor: "#ffffff", font: PLOTLY_FONT,
    xaxis: FAILBIN_XAXIS,
    yaxis: { title: "Fail count", gridcolor: "#e1e0d9", zeroline: false },
    showlegend: false, bargap: 0.35, annotations: failBinPctAnnotations(x, rows),
  };
  Plotly.newPlot(divId, [bar], layout, { responsive: true, displayModeBar: false });
}

// fail_bin_ranking 은 (bin, TNO) 조합별 행이라, 같은 Bin 의 여러 TNO 를 하나로 합쳐
// Bin 단위로 집계한다(x축이 TNO 가 아니라 Bin 기준이 되도록). count·fail rate 는 합산,
// item 은 그 Bin 의 most-fail TNO(입력이 count 내림차순이라 첫 행) Item 명을 대표로 유지해
// 라벨이 "Bin xx : 항목명" 이 되게 한다. count 내림차순 정렬.
function aggregateFailBinsByBin(rows) {
  const map = new Map();
  (rows || []).forEach(r => {
    const key = String(r.bin);
    if (!map.has(key)) map.set(key, { bin: r.bin, item: r.item || "", count: 0, yield_pct: 0 });
    const g = map.get(key);
    g.count += (Number(r.count) || 0);
    g.yield_pct += (Number(r.yield_pct) || 0);
  });
  return [...map.values()].sort((a, b) => b.count - a.count);
}

// Yield 패널 하단에 Fail bin 차트 2개 추가: 상위 10 (Fail Yield ≥ 0.5%) + 나머지 전부.
// 나머지 차트는 상위 10에 못 든 ≥0.5% bin 과 0.5% 미만 bin 을 전부 합쳐 하나로 표시.
function renderYieldFailBins() {
  const sheets = webReportSheets();
  if (!window.Plotly || !sheets) return;
  const failBins = aggregateFailBinsByBin(sheets["Fail Bin"] || []);   // Bin 단위 집계
  if (!failBins.length) return;
  const panel = document.getElementById("panel-yield");
  panel.classList.add("viz-root");
  const major = failBins.filter(r => (r.yield_pct || 0) >= 0.5);
  // ≥0.5% bin 이 하나도 없으면 폴백으로 count 상위 10개를 첫 차트에 표시.
  const majorTop = major.length ? major.slice(0, 10) : failBins.slice(0, 10);
  const rest = failBins.filter(r => !majorTop.includes(r));   // 상위 10 제외 나머지 전부

  const chartHtml = (id, h) =>
    `<div class="chart-box chart-box-wide"><div id="${id}" style="width:100%;height:${h}px;"></div></div>`;
  const wrap = document.createElement("div");
  wrap.innerHTML =
    `<div class="section-title small">Fail Bin — 상위 10 (Fail Yield ≥ 0.5%)</div>` +
    chartHtml("yield-pareto", 360) +
    (rest.length
      ? `<div class="section-title small">Fail Bin — 나머지</div>` + chartHtml("yield-rest", 340)
      : "");
  panel.appendChild(wrap);
  renderPareto("yield-pareto", majorTop);
  if (rest.length) renderFailBinBar("yield-rest", rest);
}


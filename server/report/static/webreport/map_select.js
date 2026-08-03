// ── Map 좌표 선택 (Map Analysis 탭에서 chip 여러 개 골라 각기 다른 색으로 Map 강조 +
//    Distribution 전 항목 CDF 에 반영). 선택 상태는 전역 — Map redraw 와 Distribution 이 함께 참조. ──
const MAPSEL_PALETTE = ["#e11d48", "#2563eb", "#059669", "#d97706", "#7c3aed",
  "#0891b2", "#db2777", "#65a30d", "#ea580c", "#4f46e5"];
let mapSelChips = [];   // [{serial,shot,dut,xpos,ypos,bin,source,x,y, key, color, items:{subject:{value,cum_pct}}}]
let _mapSelLastQ = { serial: "", xpos: "", ypos: "" };  // 직전 검색 필드값 — 추가 후 검색 패널 재실행에 재사용(연속 추가 편의).
let _mapSelResults = []; // 직전 검색 결과 chip 배열 — 체크박스 index → chip 매핑용.

function mapSelChipKey(c) { return `${c.source || ""}|${c.serial || ""}|${c.xpos || ""}|${c.ypos || ""}`; }
function mapSelReassignColors() { mapSelChips.forEach((c, i) => { c.color = MAPSEL_PALETTE[i % MAPSEL_PALETTE.length]; }); }

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

// 한 항목(subject)에 대해 선택된 모든 chip 의 위치 마커(각 chip 색). 단일 선택일 때만
// 포커싱용 점선 크로스헤어 추가(다중은 점 색으로 구분). 해당 항목 값 없는 chip 은 건너뜀.
function chipMarkersFor(subject) {
  if (!mapSelChips.length) return null;
  const traces = [], shapes = [];
  mapSelChips.forEach(c => {
    const it = c.items[subject];
    if (!it || typeof it.value !== "number" || typeof it.cum_pct !== "number") return;
    traces.push({ type: "scatter", mode: "markers", x: [it.value], y: [it.cum_pct],
      marker: { color: c.color, size: 7, line: { width: 1, color: "#fff" } },
      cliponaxis: false, hoverinfo: "skip", showlegend: false });
  });
  if (!traces.length) return null;
  if (mapSelChips.length === 1) {   // 단일: 해당 색 점선 크로스헤어로 포커싱
    const c = mapSelChips[0], it = c.items[subject];
    if (it && typeof it.value === "number" && typeof it.cum_pct === "number") {
      shapes.push({ type: "line", x0: it.value, x1: it.value, yref: "paper", y0: 0, y1: 1,
        line: { color: c.color, width: 1, dash: "dot" } });
      shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, y0: it.cum_pct, y1: it.cum_pct,
        line: { color: c.color, width: 1, dash: "dot" } });
    }
  }
  return { traces, shapes };
}

// 선택 변경 후 Distribution 소비처(보이는 갤러리 카드 + 열려있는 Item_detail) 재렌더.
function applyChipToDistribution() {
  document.querySelectorAll('#panel-distribution .distg-card').forEach(c => { c.dataset.rendered = ""; });
  document.querySelectorAll('#panel-distribution .distg-card[data-visible="1"]').forEach(distQueueRender);
  if (_itemDetailData) { distRenderCdf(_itemDetailData); renderIdetChipVals(); }
}

// Map Analysis 툴바 '좌표 선택' → 검색 패널 토글.
function mapSelToggleSearch() {
  const box = document.getElementById("mapSelSearchBox");
  if (!box) return;
  const show = (box.style.display === "none" || !box.style.display);
  box.style.display = show ? "" : "none";
  if (show) { ensureDistData(); const inp = document.getElementById("mapSelSerial"); if (inp) inp.focus(); }
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
        <th>SERIAL</th><th>XPOS</th><th>YPOS</th><th>DUT</th><th>BIN</th></tr></thead><tbody>`;
      const body = chips.map((c, i) => {
        const added = mapSelChips.some(x => x.key === mapSelChipKey(c));
        return `<tr class="common-chip-row${added ? " common-chip-added" : ""}" data-i="${i}">
          <td class="common-chk-col"><input type="checkbox" class="mapsel-chk" data-i="${i}"${added ? " checked disabled" : ""}></td>
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

// chip 좌표 → /chip 로 항목별 값·누적% 조회. 이미 선택돼 있으면 null 반환(중복 무시).
function mapSelFetchChip(chip) {
  const key = mapSelChipKey(chip);
  if (mapSelChips.some(c => c.key === key)) return Promise.resolve(null);
  const p = new URLSearchParams({ serial: chip.serial || "", xpos: chip.xpos || "",
    ypos: chip.ypos || "", source: chip.source || "" });
  return fetch(`/pe/report/session/${SESSION_ID}/web_report/commonality/chip?${p.toString()}`, { cache: "no-cache" })
    .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(j => {
      const items = {};
      (j.items || []).forEach(it => { items[it.subject] = { value: it.value, cum_pct: it.cum_pct }; });
      return { ...(j.chip || {}), key, items };
    });
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
  let added = 0, failed = 0, lastErr = null;
  for (const chip of chips) {
    try { const obj = await mapSelFetchChip(chip); if (obj) { mapSelChips.push(obj); added++; } }
    catch (e) { failed++; lastErr = e; console.warn("chip 조회 실패", chip, e); }
  }
  mapSelReassignColors();
  renderMapAnalysis();          // Map 강조 반영(전역 상태 읽어 redraw)
  applyChipToDistribution();    // Distribution 카드+상세 재렌더
  showToast(`${added}개 추가${failed ? ` · ${failed}개 실패 (${(lastErr && lastErr.message) || "네트워크 오류"})` : ""}`);
  // renderMapAnalysis 가 패널을 다시 그려 검색 패널이 닫히므로, 다시 열고 재검색(추가된 항목 disabled 반영).
  const box = document.getElementById("mapSelSearchBox");
  if (box) {
    box.style.display = "";
    const q = _mapSelLastQ || {};
    const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ""; };
    setVal("mapSelSerial", q.serial); setVal("mapSelXpos", q.xpos); setVal("mapSelYpos", q.ypos);
    mapSelSearch();
  }
}

function mapSelRemove(key) {
  mapSelChips = mapSelChips.filter(c => c.key !== key);
  mapSelReassignColors();
  renderMapAnalysis();
  applyChipToDistribution();
}

function mapSelClear() {
  mapSelChips = [];
  renderMapAnalysis();
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
    const m = majors[i];
    cells.push(m
      ? `<td class="mfb-item">${esc(m.item)}</td><td class="mfb-bin">${esc(m.bin)}</td>` +
        `<td class="mfb-rate">${esc(fmtPct(m.rate))}%</td>`
      : `<td class="mfb-item"></td><td class="mfb-bin"></td><td class="mfb-rate"></td>`);
    body += `<tr>${cells.join("")}</tr>`;
  }

  return `<div class="mfb-wrap"><table class="mfb-table">
    <thead>
      <tr><th rowspan="2">전체 Yield</th><th colspan="2">Source 별 Yield</th><th colspan="3">Major Fail Bins</th></tr>
      <tr><th>Source</th><th>Yield</th><th>Item</th><th>Bin</th><th>Fail Rate</th></tr>
    </thead>
    <tbody>${body}</tbody>
  </table></div>`;
}

// Issue Table 카테고리별(Yield/CPK/ETC) Open/Close 카운트 — Status 가 채워진 이슈 행
// (Yield 대표행/CPK 행/ETC 행)만 집계한다. Status=="" 행(Pass/상세/서브헤더/placeholder)과
// 숨김 행(서버가 이미 제외)은 자동 비대상. 섹션 추적은 sheets.js rowSection 과 동일 로직.
function issueStatusCounts() {
  const rows = (DATA && Array.isArray(DATA.issue_table_text)) ? DATA.issue_table_text : [];
  const counts = { Yield: { open: 0, close: 0 }, CPK: { open: 0, close: 0 }, ETC: { open: 0, close: 0 } };
  let sec = "";
  rows.forEach(r => {
    if (r && r["Category"]) sec = String(r["Category"]);
    const st = String((r && r["Status"]) || "");
    if (!st || !counts[sec]) return;
    counts[sec][st === "Close" ? "close" : "open"]++;
  });
  return counts;
}

// Summary 의 Issue Status 카드(카테고리별 Open/Close + 진행률 소표) — 클릭 시 Issue Table 탭 이동.
// 진행률 = Close / (Open + Close) * 100 (소수 1자리). 이슈 행이 없는 카테고리는 "-".
function issueStatusCardHtml() {
  const counts = issueStatusCounts();
  const rows = ["Yield", "CPK", "ETC"].map(cat => {
    const c = counts[cat];
    const total = c.open + c.close;
    const prog = total ? (c.close / total * 100).toFixed(1) + "%" : "-";
    return `<tr><td class="iss-cat">${cat}</td>` +
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

let summaryJumpBound = false;
function renderWebSummary() {
  const panel = document.getElementById("panel-summary");
  const sheets = webReportSheets();
  if (!window.Plotly || !sheets) { emptyPanel(panel, "Summary 데이터 없음"); return; }
  // Summary 카드(Yield) 클릭 → 해당 탭 버튼 클릭 재사용(1회 위임 바인딩).
  if (!summaryJumpBound) {
    panel.addEventListener("click", (e) => {
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
    issueStatusCardHtml() +
    `<div class="summary-section-card">` +
    `<div class="section-title">Engr Comment</div>` +
    `<div class="engr-comment-grid">` +
    ENGR_COMMENT_FIELDS.map(f =>
      `<label class="engr-comment-label" for="engr-${f.key}">${f.label}</label>` +
      `<textarea id="engr-${f.key}" class="engr-comment-input" data-engr="${f.key}" rows="4"${engrEditable ? "" : " readonly"}>${esc(engr[f.key] || "")}</textarea>`).join("") +
    `</div></div>`;
  if (engrEditable) bindEngrComment(panel);
}

// Summary 탭 Engr Comment 3칸 정의 (manifest.summary_engr 키와 일치).
const ENGR_COMMENT_FIELDS = [
  { key: "yield", label: "Yield" },
  { key: "cpk", label: "CPK" },
  { key: "etc", label: "ETC" },
];

// textarea 값이 바뀌면(blur 시 change 발생) autoSave 경로로 저장 — dot/dirty/실패복원을
// Issue comment 와 일원화하고, 탭 전환·페이지 이탈 시에도 autoSave 안전망이 ENGR 를 덮는다.
// (_dirty 명시 세팅은 .content input 버블링에 의존하지 않기 위한 방어 1줄.)
function bindEngrComment(panel) {
  panel.querySelectorAll("textarea[data-engr]").forEach(ta => {
    ta.addEventListener("change", () => { _dirty = true; autoSave(); });
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
  panel.querySelectorAll("textarea[data-engr]").forEach(ta => {
    const k = ta.dataset.engr;
    const v = (ta.value || "").trim();
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
    showToast("Engr Comment 저장 실패: 네트워크 오류");
    throw err;
  }
  const j = await res.json().catch(() => ({}));
  if (!res.ok) { showToast(j.error || `Engr Comment 저장 실패 (HTTP ${res.status})`); throw new Error("engr save failed"); }
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


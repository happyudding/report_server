// ── Compare 모드 (같은 Wafer 2~3 source 비교) ─────────────────────────────────
const COMPARE_SRC_PALETTE = ["#e11d48", "#2563eb", "#d97706"];   // source 별 색(빨강/파랑/…)
const CMP_MATCH_GREEN = "#16a34a";    // 모든 source 에서 Bin 동일 — 초록
const CMP_MIXED_COLOR = "#7c3aed";    // Bin 다르고 2개↑ source 에서 Fail — 보라(혼합)

function _cmpNum(v, digits) {
  if (v === null || v === undefined || v === "") return "–";
  if (typeof v !== "number") return esc(String(v));
  return Number.isInteger(v) ? String(v) : v.toFixed(digits == null ? 3 : digits);
}
function _cmpDeltaCell(v, digits) {
  if (v === null || v === undefined || v === "") return `<td class="num">–</td>`;
  const cls = v > 0 ? "cmp-up" : (v < 0 ? "cmp-down" : "");
  const s = v > 0 ? "+" + _cmpNum(v, digits) : _cmpNum(v, digits);
  return `<td class="num ${cls}">${s}</td>`;
}

// 공통성 Map(단일): 좌표별 Bin 일치=초록, 한 source 에서만 Fail=그 source 색,
// 둘 다 Fail(Bin 만 다름)=보라. waferHeatmap 을 재사용하되 bin 자리에 분류 라벨을 넣는다.
function _cmpClsLabel(cls, sources) {
  if (cls === "match") return "Bin 일치";
  if (cls === "mixed") return "혼합 · 둘 다 Fail";
  return `${cls} 에서만 Fail`;   // cls = source 이름
}
function drawCompareCommonMap(cm, sources) {
  const div = document.getElementById("cmp-common-map");
  if (!div || !window.Plotly) return;
  if (cm.x_min == null) { div.innerHTML = '<div class="placeholder">좌표 데이터 없음</div>'; return; }
  const srcColor = {};
  sources.forEach((s, i) => { srcColor[s] = COMPARE_SRC_PALETTE[i % COMPARE_SRC_PALETTE.length]; });

  const matchLabel = "Bin 일치", mixedLabel = "혼합 · 둘 다 Fail";
  const colorMap = { [matchLabel]: CMP_MATCH_GREEN, [mixedLabel]: CMP_MIXED_COLOR };
  sources.forEach(s => { colorMap[_cmpClsLabel(s, sources)] = srcColor[s]; });
  const binOrder = [matchLabel, ...sources.map(s => _cmpClsLabel(s, sources)), mixedLabel];

  const dies = (cm.dies || []).map(d => ({ x: d.x, y: d.y, bin: _cmpClsLabel(d.cls, sources) }));
  const m = { x_min: cm.x_min, x_max: cm.x_max, y_min: cm.y_min, y_max: cm.y_max, dies };
  const built = waferHeatmap(m, { colorMap, binOrder,
    hovertemplate: "(%{x}, %{y})<br>%{customdata}<extra></extra>" });
  if (!built) { div.innerHTML = '<div class="placeholder">공통 die 없음</div>'; return; }
  Plotly.newPlot(div, [built.trace], waferLayout(m, {}), { responsive: true, displayModeBar: false });

  // 범례 — 실제 등장하는 분류만 count 와 함께
  const legend = document.getElementById("cmp-common-legend");
  if (legend) {
    const c = cm.counts || {};
    const rows = [`<span class="cmp-lg"><i style="background:${CMP_MATCH_GREEN}"></i>Bin 일치 (${c.match || 0})</span>`];
    sources.forEach(s => {
      const n = (c.per_source && c.per_source[s]) || 0;
      rows.push(`<span class="cmp-lg"><i style="background:${srcColor[s]}"></i>${esc(s)} 에서만 Fail (${n})</span>`);
    });
    if (c.mixed) rows.push(`<span class="cmp-lg"><i style="background:${CMP_MIXED_COLOR}"></i>혼합 · 둘 다 Fail (${c.mixed})</span>`);
    legend.innerHTML = rows.join("");
  }
}

// 동일 좌표 Bin before→after 전이표 (Map 바로 밑). null/빈 rows 면 생략.
function compareBinTransitionHtml(bt) {
  if (!bt || !bt.rows || !bt.rows.length) return "";
  const c = bt.counts || {};
  const binLabel = b => (String(b) === "1") ? `${esc(String(b))} (Pass)` : esc(String(b));
  const head = `<thead><tr>
      <th>Before Bin<div class="gl-sub">${esc(bt.before_source || "")}</div></th>
      <th>After Bin<div class="gl-sub">${esc(bt.after_source || "")}</div></th>
      <th class="num">Count</th></tr></thead>`;
  const body = bt.rows.map(r =>
    `<tr class="${r.changed ? "bt-changed" : ""}"><td>${binLabel(r.before_bin)}</td>` +
    `<td>${binLabel(r.after_bin)}</td><td class="num">${_cmpNum(r.count)}</td></tr>`).join("");
  const summary = `<div class="compare-summary">
      <span class="cmp-chip">공통 die ${c.common_dies || 0}</span>
      <span class="cmp-chip cmp-unique">Bin 변경 ${c.changed || 0}</span>
      <span class="cmp-chip">Pass→Fail ${c.pass_to_fail || 0}</span>
      <span class="cmp-chip">Fail→Pass ${c.fail_to_pass || 0}</span></div>`;
  return summary + `<div class="sheet-wrap"><table class="sheet-table compare-table">${head}<tbody>${body}</tbody></table></div>`;
}

// 공통 항목 산포(avg/stdev/cpk) before/after 병기 + delta. |Δcpk| 큰 순(백엔드 정렬).
function compareDistShiftHtml(rows, sources) {
  if (!rows || !rows.length) return '<div class="placeholder">공통 항목 없음</div>';
  const after = sources[0] || "", before = sources[1] || "";   // payload sources = [after, before]
  const gapCell = v => {
    if (v === null || v === undefined) return `<td class="num">–</td>`;
    return `<td class="num${Math.abs(v) >= 10 ? " gl-gap-red" : ""}">${_cmpNum(v, 2)}</td>`;
  };
  const head = `<thead>
      <tr><th rowspan="2">Item</th><th rowspan="2">Unit</th>
          <th colspan="3">After — ${esc(after)}</th><th colspan="3">Before — ${esc(before)}</th>
          <th class="num" rowspan="2">ΔAvg</th><th class="num" rowspan="2">ΔStdev</th>
          <th class="num" rowspan="2">ΔCpk</th><th class="num" rowspan="2">평균 gap %</th></tr>
      <tr><th class="num">Avg</th><th class="num">Stdev</th><th class="num">Cpk</th>
          <th class="num">Avg</th><th class="num">Stdev</th><th class="num">Cpk</th></tr></thead>`;
  const body = rows.map(r => {
    const a = r.after || {}, b = r.before || {};
    return `<tr><td>${esc(r.subject)}</td><td>${esc(r.units || "")}</td>` +
      `<td class="num">${_cmpNum(a.average)}</td><td class="num">${_cmpNum(a.stdev)}</td><td class="num">${_cmpNum(a.cpk)}</td>` +
      `<td class="num">${_cmpNum(b.average)}</td><td class="num">${_cmpNum(b.stdev)}</td><td class="num">${_cmpNum(b.cpk)}</td>` +
      _cmpDeltaCell(r.delta_average) + _cmpDeltaCell(r.delta_stdev) + _cmpDeltaCell(r.delta_cpk) +
      gapCell(r.mean_gap_pct) + `</tr>`;
  }).join("");
  return `<div class="sheet-wrap"><table class="sheet-table compare-table">${head}<tbody>${body}</tbody></table></div>`;
}

function compareBinTableHtml(binDelta, sources) {
  if (!binDelta || !binDelta.length) return '<div class="placeholder">Bin 데이터 없음</div>';
  const srcHead = sources.map(s => `<th colspan="2">${esc(s)}</th>`).join("");
  const subHead = sources.map(() => `<th class="num">Cnt</th><th class="num">%</th>`).join("");
  const head = `<thead>
      <tr><th rowspan="2">Bin</th>${srcHead}
          <th class="num" rowspan="2">Δ%</th><th class="num" rowspan="2">편차%</th></tr>
      <tr>${subHead}</tr></thead>`;
  const body = binDelta.map(r => {
    const perSrc = {};
    (r.sources || []).forEach(s => { perSrc[s.source] = s; });
    const cells = sources.map(s => {
      const d = perSrc[s] || {};
      return `<td class="num">${_cmpNum(d.count)}</td><td class="num">${_cmpNum(d.pct, 2)}</td>`;
    }).join("");
    const binLabel = r.is_pass ? `${esc(String(r.bin))} (Pass)` : esc(String(r.bin));
    return `<tr class="${r.is_pass ? "cmp-pass-row" : ""}"><td>${binLabel}</td>${cells}` +
      _cmpDeltaCell(r.delta_pct, 2) +
      `<td class="num">${_cmpNum(r.range_pct, 2)}</td></tr>`;
  }).join("");
  return `<div class="sheet-wrap"><table class="sheet-table compare-table">${head}<tbody>${body}</tbody></table></div>`;
}

// ── goodlog(테스트 프로그램 diff) — Honey Compare Mode 이식. after/before 두 파일의
//    항목명/limit 일치 여부(True 초록/False 빨강) + reference die 값 gap%(|gap|≥10% 빨강).
//    이상(항목 추가/제거·limit 변경)만 상단 요약 + 항상 표시하고, 나머지 정상 행은
//    git-diff 식으로 접어둔다(초기 접힘, '전체 펼치기' 토글). ──
// 행 분류: added(after 만)·removed(before 만)·limitchg(양쪽 존재 & limit 불일치)·normal.
// goodlog 15컬럼 기본 폭(px) — colgroup 순서 = GOODLOG_HEADER 순서(compare.py).
// [after Item/Lo/Hi/Unit/Value, compare Item/Lo/Hi, Comment, Gap%, before Item/Lo/Hi/Unit/Value]
const GOODLOG_COLW = [130, 76, 76, 44, 84, 58, 58, 58, 110, 58, 130, 76, 76, 44, 84];

function goodlogRowType(r) {
  const aHas = (r.after_item_name || "") !== "";
  const bHas = (r.before_item_name || "") !== "";
  if (aHas && !bHas) return "added";
  if (!aHas && bHas) return "removed";
  if (aHas && bHas && (r.compare_lolimit === false || r.compare_hilimit === false)) return "limitchg";
  return "normal";
}

function goodlogSectionHtml(gl) {
  if (!gl) return "";   // legacy(3-source 등) 세션 — 섹션 생략
  const title = `<h3 class="compare-h">테스트 프로그램 비교 (goodlog) — ` +
    `after: ${esc(gl.after_source || "")} / before: ${esc(gl.before_source || "")}</h3>`;
  if (gl.identical) {
    return title + `<div class="gl-identical">두 파일의 테스트 프로그램(항목/limit)이 동일합니다.</div>`;
  }
  // 결측(None)은 공백 — Honey goodlog 시트의 _disp 관례와 동일 (한쪽만 존재하는 행 포함).
  const glNum = v => (v === null || v === undefined) ? "" : _cmpNum(v);
  const boolCell = v => v === true ? `<td class="gl-true">True</td>`
    : (v === false ? `<td class="gl-false">False</td>` : `<td></td>`);
  const gapCell = v => {
    if (v === null || v === undefined) return `<td class="num"></td>`;
    return `<td class="num${Math.abs(v) >= 10 ? " gl-gap-red" : ""}">${_cmpNum(v, 2)}</td>`;
  };
  const COLS = 15;
  const rows = gl.rows || [];
  const types = rows.map(goodlogRowType);

  // ── 이상 요약(항목 추가/제거·limit 변경) ──
  const added = [], removed = [], limitchg = [];
  rows.forEach((r, i) => {
    if (types[i] === "added") added.push(r.after_item_name);
    else if (types[i] === "removed") removed.push(r.before_item_name);
    else if (types[i] === "limitchg") limitchg.push(r.after_item_name || r.before_item_name);
  });
  const abnCount = added.length + removed.length + limitchg.length;
  const nameChips = (arr, cls) => arr.map(n => `<span class="gl-ab-item ${cls}">${esc(n)}</span>`).join("");
  let summary;
  if (abnCount === 0) {
    summary = `<div class="gl-ab-summary gl-ab-none">항목/Limit 차이 없음 — 값 gap 만 존재</div>`;
  } else {
    // 이상 항목 목록(칩)은 개수가 많으면 표를 밀어내므로 헤드 버튼으로 따로 접었다 편다.
    summary = `<div class="gl-ab-summary">
      <div class="gl-ab-head">
        <button class="gl-ab-toggle" type="button"><span class="gl-fold-arrow">▾</span> 이상 항목</button>
        <span class="cmp-chip gl-chip-add">추가 ${added.length}</span>
        <span class="cmp-chip gl-chip-del">제거 ${removed.length}</span>
        <span class="cmp-chip gl-chip-lim">Limit 변경 ${limitchg.length}</span></div>
      <div class="gl-ab-items">${nameChips(added, "gl-add")}${nameChips(removed, "gl-del")}${nameChips(limitchg, "gl-lim")}</div>
    </div>`;
  }

  // colgroup 기본 폭(px) — 사용자 드래그 리사이즈의 시작값. Item 은 기본이 너무 넓다는
  // 요청으로 좁게 잡고, 넘치는 이름은 ellipsis + title 툴팁으로 본다(table-layout:fixed).
  const colgroup = `<colgroup>${GOODLOG_COLW.map(w => `<col style="width:${w}px">`).join("")}</colgroup>`;
  const rz = i => `<span class="col-resize-handle" data-col="${i}"></span>`;
  const head = colgroup + `<thead>
      <tr><th colspan="5">After — ${esc(gl.after_source || "")}</th><th colspan="3">Compare</th>
          <th rowspan="2">Comment${rz(8)}</th><th class="num" rowspan="2">Gap %${rz(9)}</th>
          <th colspan="5">Before — ${esc(gl.before_source || "")}</th></tr>
      <tr><th>Item${rz(0)}</th><th class="num">LoLim${rz(1)}</th><th class="num">HiLim${rz(2)}</th><th>Unit${rz(3)}</th><th class="num">Value${rz(4)}</th>
          <th>Item${rz(5)}</th><th>LoLim${rz(6)}</th><th>HiLim${rz(7)}</th>
          <th>Item${rz(10)}</th><th class="num">LoLim${rz(11)}</th><th class="num">HiLim${rz(12)}</th><th>Unit${rz(13)}</th><th class="num">Value${rz(14)}</th></tr></thead>`;
  // 폭 고정(fixed layout)이라 긴 Item 명은 잘린다 — title 로 전체 이름을 볼 수 있게 한다.
  const nameCell = v => `<td title="${esc(v || "")}">${esc(v || "")}</td>`;
  const rowHtml = (r, t) =>
    `<tr class="gl-row gl-${t}">` + nameCell(r.after_item_name) + `<td class="num">${glNum(r.after_lolimit)}</td>` +
    `<td class="num">${glNum(r.after_hilimit)}</td><td>${esc(r.after_unit || "")}</td>` +
    `<td class="num">${esc(r.after_value || "")}</td>` +
    boolCell(r.compare_item_name) + boolCell(r.compare_lolimit) + boolCell(r.compare_hilimit) +
    `<td>${esc(r.comment || "")}</td>` + gapCell(r.gap) +
    nameCell(r.before_item_name) + `<td class="num">${glNum(r.before_lolimit)}</td>` +
    `<td class="num">${glNum(r.before_hilimit)}</td><td>${esc(r.before_unit || "")}</td>` +
    `<td class="num">${esc(r.before_value || "")}</td></tr>`;

  // ── git-diff 식 세그먼트: normal run 은 접힘 tbody, 이상 행은 항상 노출 ──
  const parts = [];
  let i = 0;
  while (i < rows.length) {
    if (types[i] === "normal") {
      let j = i; const buf = [];
      while (j < rows.length && types[j] === "normal") { buf.push(rowHtml(rows[j], "normal")); j++; }
      parts.push(
        `<tbody class="gl-fold-summary"><tr class="gl-fold-toggle"><td colspan="${COLS}">` +
        `<span class="gl-fold-arrow">▸</span> 변화 없음 ${buf.length}개</td></tr></tbody>` +
        `<tbody class="gl-fold" hidden>${buf.join("")}</tbody>`);
      i = j;
    } else {
      let j = i; const buf = [];
      while (j < rows.length && types[j] !== "normal") { buf.push(rowHtml(rows[j], types[j])); j++; }
      parts.push(`<tbody>${buf.join("")}</tbody>`);
      i = j;
    }
  }
  // 상단 프록시 가로 스크롤바 — 표가 길어 하단까지 내려가지 않아도 좌우로 스크롤할 수 있게
  // (Issue Table 의 .issue-hscroll 과 동일 패턴, scrollLeft 만 양방향 동기화).
  return title + summary +
    `<div class="gl-hscroll"><div class="gl-hscroll-spacer"></div></div>` +
    `<div class="sheet-wrap gl-wrap"><table class="sheet-table compare-table goodlog-table">${head}${parts.join("")}</table></div>`;
}

// ── goodlog 표: 폭 동기화 / 프록시 가로 스크롤바 / 컬럼 드래그 리사이즈 ──────────
// table-layout:fixed 는 표 전체 width 를 따로 잡아줘야 컬럼 폭 합이 그대로 반영된다.
function glSyncTableWidth(table) {
  const cg = table.querySelector("colgroup");
  if (!cg) return;
  let sum = 0;
  Array.from(cg.children).forEach(c => { sum += parseFloat(c.style.width) || 0; });
  if (sum > 0) table.style.width = sum + "px";
}
function syncGoodlogHscroll(panel) {
  const wrap = panel.querySelector(".sheet-wrap.gl-wrap");
  const spacer = panel.querySelector(".gl-hscroll-spacer");
  if (!wrap || !spacer) return;
  spacer.style.width = wrap.scrollWidth + "px";
}
function bindGoodlogHscroll(panel) {
  const wrap = panel.querySelector(".sheet-wrap.gl-wrap");
  const hs = panel.querySelector(".gl-hscroll");
  if (!wrap || !hs) return;
  syncGoodlogHscroll(panel);
  let syncing = false;
  hs.addEventListener("scroll", () => {
    if (syncing) return;
    syncing = true; wrap.scrollLeft = hs.scrollLeft; syncing = false;
  });
  wrap.addEventListener("scroll", () => {
    if (syncing) return;
    syncing = true; hs.scrollLeft = wrap.scrollLeft; syncing = false;
  });
}
// 헤더 우측 경계 핸들(.col-resize-handle, data-col=컬럼인덱스)을 끌어 <col> width 를 바꾼다.
// 저장 없음(새로고침 시 기본 폭 복귀) — Issue Table 의 bindIssueColResize 와 같은 관례.
function bindGoodlogColResize(panel) {
  const table = panel.querySelector(".goodlog-table");
  const cg = table && table.querySelector("colgroup");
  if (!table || !cg) return;
  glSyncTableWidth(table);
  const MIN_W = 30;
  table.addEventListener("mousedown", e => {
    const handle = e.target.closest(".col-resize-handle");
    if (!handle) return;
    const col = cg.children[+handle.dataset.col];
    if (!col) return;
    const th = handle.closest("th");
    const startW = th ? th.getBoundingClientRect().width : parseFloat(col.style.width) || 80;
    const startX = e.clientX;
    e.preventDefault();   // 드래그 중 텍스트 선택 방지
    const onMove = ev => {
      col.style.width = Math.max(MIN_W, Math.round(startW + (ev.clientX - startX))) + "px";
      glSyncTableWidth(table);
      syncGoodlogHscroll(panel);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      syncGoodlogHscroll(panel);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// goodlog 접기/펼치기 바인딩 — renderCompare 가 innerHTML 갱신 후 호출(직접 바인딩).
function bindGoodlogFolding(panel) {
  const setLabel = (toggleRow, shown) => {
    const arrow = toggleRow.querySelector(".gl-fold-arrow");
    if (arrow) arrow.textContent = shown ? "▾" : "▸";
  };
  panel.querySelectorAll(".gl-fold-toggle").forEach(row => {
    row.addEventListener("click", () => {
      const fold = row.closest(".gl-fold-summary").nextElementSibling;
      if (!fold || !fold.classList.contains("gl-fold")) return;
      const show = fold.hasAttribute("hidden");
      if (show) fold.removeAttribute("hidden"); else fold.setAttribute("hidden", "");
      setLabel(row, show);
    });
  });
  // 이상 항목 칩 목록만 따로 접기 (표의 '변화 없음' 접기와 독립).
  panel.querySelectorAll(".gl-ab-toggle").forEach(tbtn => {
    tbtn.addEventListener("click", () => {
      const items = tbtn.closest(".gl-ab-summary").querySelector(".gl-ab-items");
      if (!items) return;
      const show = items.hasAttribute("hidden");
      if (show) items.removeAttribute("hidden"); else items.setAttribute("hidden", "");
      const arrow = tbtn.querySelector(".gl-fold-arrow");
      if (arrow) arrow.textContent = show ? "▾" : "▸";
    });
  });
  // '전체 펼치기' 는 상단 sticky 툴바에 있다(패널 밖이 아니라 panel 안이라 그대로 찾힌다).
  const btn = panel.querySelector(".gl-expand-all");
  if (btn) btn.addEventListener("click", () => {
    const anyHidden = !!panel.querySelector(".gl-fold[hidden]");
    panel.querySelectorAll(".gl-fold").forEach(f => {
      if (anyHidden) f.removeAttribute("hidden"); else f.setAttribute("hidden", "");
    });
    panel.querySelectorAll(".gl-fold-toggle").forEach(r => setLabel(r, anyHidden));
    btn.textContent = anyHidden ? "전체 접기" : "전체 펼치기";
  });
}

// 서브탭 전환 — Compare 패널 안에서 Map/Log/CPK 하위 화면을 토글한다.
// (Bin 비교는 서브탭 없이 Map 패널 하단에 함께 표시한다.)
function bindCompareSubtabs(panel) {
  const bar = panel.querySelector(".cmp-subtabs");
  if (!bar) return;
  const expandBtn = panel.querySelector(".gl-expand-all");
  bar.addEventListener("click", e => {
    const btn = e.target.closest("[data-cmpsub]");
    if (!btn) return;
    const key = btn.dataset.cmpsub;
    bar.querySelectorAll("[data-cmpsub]").forEach(b => b.classList.toggle("active", b === btn));
    panel.querySelectorAll(".cmp-subpanel").forEach(p =>
      p.classList.toggle("active", p.dataset.cmppanel === key));
    // '전체 펼치기' 는 goodlog 표 전용이라 Log 화면에서만 노출.
    if (expandBtn) expandBtn.hidden = (key !== "log");
    // 숨김(0px) 상태에서 그려진 Plotly 맵은 보일 때 리사이즈해야 폭이 복구된다.
    const active = panel.querySelector(`.cmp-subpanel[data-cmppanel="${key}"]`);
    if (active && window.Plotly) {
      active.querySelectorAll(".js-plotly-plot").forEach(d => { try { Plotly.Plots.resize(d); } catch (e) {} });
    }
    // 숨김 상태에선 scrollWidth 가 0 이라 보일 때 프록시 스크롤바 폭을 다시 실측.
    if (key === "log") syncGoodlogHscroll(panel);
  });
}

function renderCompare() {
  const panel = document.getElementById("panel-compare");
  const cmp = DATA.web_report && DATA.web_report.compare;
  if (!cmp) { emptyPanel(panel, "Compare 데이터 없음 (같은 Wafer source 2개 이상 필요)"); return; }
  panel.classList.add("viz-root");
  const sources = cmp.sources || [];
  const cm = cmp.common_map || {};
  const c = cm.counts || {};
  const mismatch = Math.max(0, (c.common_dies || 0) - (c.match || 0));

  panel.innerHTML =
    `<div class="compare-wrap">
      <div class="compare-summary">
        <span class="mk">Sources</span> ${sources.map(esc).join(" · ")}
        <span class="cmp-chip">공통 die ${c.common_dies || 0}</span>
        <span class="cmp-chip cmp-common">Bin 일치 ${c.match || 0}</span>
        <span class="cmp-chip cmp-unique">Bin 불일치 ${mismatch}</span>
      </div>
      <div class="cmp-toolbar">
        <div class="cmp-subtabs distseg-group">
          <button class="distseg active" data-cmpsub="map">Map 비교</button>
          <button class="distseg" data-cmpsub="log">Log 비교</button>
          <button class="distseg" data-cmpsub="cpk">CPK 비교</button>
        </div>
        ${(cmp.goodlog && !cmp.goodlog.identical && (cmp.goodlog.rows || []).length)
            ? `<button class="btn-sm gl-expand-all" type="button" hidden>전체 펼치기</button>` : ""}
      </div>
      <div class="cmp-subpanel active" data-cmppanel="map">
        <h3 class="compare-h">공통성 Map — Bin 일치=초록 / 한쪽만 Fail=source 색 / 둘 다 Fail=보라</h3>
        <div class="wafer-card">
          <div id="cmp-common-map" style="width:100%;height:520px;"></div>
          <div id="cmp-common-legend" class="cmp-legend"></div>
        </div>
        <div class="cmp-bin-grid">
          <section>
            <h3 class="compare-h">동일 좌표 Bin 변화 (before → after)</h3>
            ${compareBinTransitionHtml(cmp.bin_transition) || '<div class="placeholder">Bin 전이표는 2 source 비교에서만 제공됩니다.</div>'}
          </section>
          <section>
            <h3 class="compare-h">Bin Yield 비교</h3>
            ${compareBinTableHtml(cmp.bin_delta, sources)}
          </section>
        </div>
      </div>
      <div class="cmp-subpanel" data-cmppanel="log">
        ${goodlogSectionHtml(cmp.goodlog) || '<div class="placeholder">테스트 프로그램 비교(goodlog)는 2 source 비교에서만 제공됩니다.</div>'}
      </div>
      <div class="cmp-subpanel" data-cmppanel="cpk">
        <h3 class="compare-h">산포 차이 (공통 항목 · |ΔCpk| 큰 순)</h3>
        ${compareDistShiftHtml(cmp.dist_shift, sources)}
      </div>
    </div>`;

  drawCompareCommonMap(cm, sources);
  bindGoodlogFolding(panel);
  bindGoodlogColResize(panel);
  bindGoodlogHscroll(panel);
  bindCompareSubtabs(panel);
  syncCompareToolbarH(panel);
}

// sticky 툴바 실제 높이 → 그 아래 붙는 프록시 가로 스크롤바의 top 오프셋(--cmp-toolbar-h).
function syncCompareToolbarH(panel) {
  panel = panel || document.getElementById("panel-compare");
  if (!panel) return;
  const bar = panel.querySelector(".cmp-toolbar");
  if (bar && bar.offsetHeight) panel.style.setProperty("--cmp-toolbar-h", bar.offsetHeight + "px");
}
window.addEventListener("resize", () => {
  const panel = document.getElementById("panel-compare");
  if (!panel) return;
  syncCompareToolbarH(panel);
  syncGoodlogHscroll(panel);
});


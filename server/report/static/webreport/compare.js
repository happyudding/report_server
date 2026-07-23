// ── Compare 모드 (같은 Wafer 2~3 source 비교) ─────────────────────────────────
const COMPARE_SRC_PALETTE = ["#e11d48", "#2563eb", "#d97706"];   // source 별 색(빨강/파랑/…)
const CMP_MATCH_GREEN = "#16a34a";    // 모든 source 에서 Bin 동일 — 초록
const CMP_MIXED_COLOR = "#7c3aed";    // Bin 다르고 2개↑ source 에서 Fail — 보라(혼합)

function _cmpNum(v, digits) {
  if (v === null || v === undefined || v === "") return "–";
  if (typeof v !== "number") return esc(String(v));
  return Number.isInteger(v) ? String(v) : v.toFixed(digits == null ? 3 : digits);
}
// 반올림 없이 서버가 준 값 그대로 (stdev 전용 — 서버도 stdev 만 round 하지 않는다,
// web_report/tabs/cpk.py `_stats_batch`). 표시상 자릿수를 줄이면 Limit 역산이 어긋난다.
function _cmpRaw(v) {
  if (v === null || v === undefined || v === "") return "–";
  return esc(String(v));
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
  if (cls === "mixed") return "혼합 · 2개↑ Fail";
  return `${cls} 에서만 Fail`;   // cls = source 이름
}
// source 이름 뒤에 Before/After 그룹을 괄호로 (groups 없으면 이름 그대로 — legacy payload).
function _cmpSrcLabel(src, groups) {
  const g = groups && groups[src];
  return g ? `${src} (${g === "after" ? "After" : "Before"})` : String(src);
}
function drawCompareCommonMap(cm, sources) {
  const div = document.getElementById("cmp-common-map");
  if (!div || !window.Plotly) return;
  if (cm.x_min == null) { div.innerHTML = '<div class="placeholder">좌표 데이터 없음</div>'; return; }
  const groups = cm.groups || {};
  const srcColor = {};
  sources.forEach((s, i) => { srcColor[s] = COMPARE_SRC_PALETTE[i % COMPARE_SRC_PALETTE.length]; });

  const matchLabel = "Bin 일치", mixedLabel = "혼합 · 2개↑ Fail";
  const colorMap = { [matchLabel]: CMP_MATCH_GREEN, [mixedLabel]: CMP_MIXED_COLOR };
  sources.forEach(s => { colorMap[_cmpClsLabel(s, sources)] = srcColor[s]; });
  const binOrder = [matchLabel, ...sources.map(s => _cmpClsLabel(s, sources)), mixedLabel];

  // bins(= source 별 BIN, sources 순서)를 die 에 실어 hover 에서 볼 수 있게 한다.
  const dies = (cm.dies || []).map(d => ({
    x: d.x, y: d.y, bin: _cmpClsLabel(d.cls, sources), bins: d.bins }));
  const m = { x_min: cm.x_min, x_max: cm.x_max, y_min: cm.y_min, y_max: cm.y_max, dies };
  // compact 격자로 그린다(메모리 span 무관 — 좌표 span 이 넓어도 OOM 방지. Map Detail 과 동일).
  // grid 모드에선 hovertemplate 이 무시되고 customdata 에 담긴 실좌표가 표시된다.
  const g = waferCompactGrid(m);
  // hover: 분류 + source 별 Bin 한 줄씩 ("WF3 (After): 1"). bins 가 없으면(legacy payload)
  // 종전처럼 분류만 보여준다.
  const labelOf = (d, cat) => {
    if (!d.bins) return cat;
    const lines = sources.map((s, i) => `${esc(_cmpSrcLabel(s, groups))}: ${esc(String(d.bins[i]))}`);
    return cat + "<br>" + lines.join("<br>");
  };
  const built = waferHeatmap(m, { colorMap, binOrder, grid: g, labelOf });
  if (!built) { div.innerHTML = '<div class="placeholder">공통 die 없음</div>'; return; }
  Plotly.newPlot(div, [built.trace], waferLayout(m, { grid: g }), { responsive: true, displayModeBar: false });

  // 범례 — 실제 등장하는 분류만 count 와 함께
  const legend = document.getElementById("cmp-common-legend");
  if (legend) {
    const c = cm.counts || {};
    const rows = [`<span class="cmp-lg"><i style="background:${CMP_MATCH_GREEN}"></i>Bin 일치 (${c.match || 0})</span>`];
    sources.forEach(s => {
      const n = (c.per_source && c.per_source[s]) || 0;
      rows.push(`<span class="cmp-lg"><i style="background:${srcColor[s]}"></i>${esc(_cmpSrcLabel(s, groups))} 에서만 Fail (${n})</span>`);
    });
    if (c.mixed) rows.push(`<span class="cmp-lg"><i style="background:${CMP_MIXED_COLOR}"></i>혼합 · 2개↑ Fail (${c.mixed})</span>`);
    legend.innerHTML = rows.join("");
  }
}

// 동일 좌표 Bin 비교표 (Map 바로 밑) — Bin 이 전 source 에서 같지는 않은 좌표를 1행씩.
// 컬럼은 Before 그룹 / After 그룹으로 묶는다. null/빈 rows 면 생략.
function compareBinMatrixHtml(bm) {
  if (!bm || !bm.rows) return "";
  const c = bm.counts || {};
  const before = bm.before_sources || [], after = bm.after_sources || [];
  const order = bm.sources || [];
  const binCell = b => {
    const s = String(b);
    return `<td class="num${s === "1" ? " cmp-pass-cell" : ""}">${esc(s)}</td>`;
  };
  const summary = `<div class="compare-summary">
      <span class="cmp-chip">공통 die ${c.common_dies || 0}</span>
      <span class="cmp-chip cmp-unique">Bin 불일치 ${c.mismatch || 0}</span>
      <span class="cmp-chip">Pass→Fail ${c.pass_to_fail || 0}</span>
      <span class="cmp-chip">Fail→Pass ${c.fail_to_pass || 0}</span>
      <span class="gl-sub">Pass→Fail / Fail→Pass 는 그룹 대표(${esc(bm.rep_before || "")} → ${esc(bm.rep_after || "")}) 기준</span>
    </div>`;
  if (!bm.rows.length) {
    return summary + `<div class="gl-identical">공통 좌표의 Bin 이 모든 source 에서 동일합니다.</div>`;
  }
  // 서버는 sources 순서(=업로드 순서, After 먼저)로 bins 를 담는다. 표는 Before→After 로
  // 묶어 보여주므로 표시 순서에 맞춰 인덱스를 다시 잡는다.
  const idxOf = {}; order.forEach((s, i) => { idxOf[s] = i; });
  const shown = before.concat(after);
  const grpHead = (before.length ? `<th colspan="${before.length}">Before</th>` : "") +
    (after.length ? `<th colspan="${after.length}">After</th>` : "");
  const head = `<thead>
      <tr><th class="num" rowspan="2">X</th><th class="num" rowspan="2">Y</th>${grpHead}</tr>
      <tr>${shown.map(s => `<th class="num">${esc(s)}</th>`).join("")}</tr></thead>`;
  const body = bm.rows.map(r =>
    `<tr><td class="num">${_cmpNum(r.x)}</td><td class="num">${_cmpNum(r.y)}</td>` +
    shown.map(s => binCell(r.bins[idxOf[s]])).join("") + `</tr>`).join("");
  return summary +
    `<div class="sheet-wrap cmp-scroll cmp-binmatrix-wrap"><table class="sheet-table compare-table">${head}<tbody>${body}</tbody></table></div>`;
}

// 공통 항목 산포(avg/stdev/cpk) before/after 병기 + delta. |Δcpk| 큰 순(백엔드 정렬).
// 대상은 **그룹 pool** — 그룹이 1 source 씩이면 CPK 탭 값과 같다(compare.py build_dist_shift).
// stdev 는 서버가 반올림하지 않는 값이라 화면에서도 원값 그대로 쓴다(_cmpRaw).
function compareDistShiftHtml(rows, eq) {
  if (!rows || !rows.length) return '<div class="placeholder">공통 항목 없음</div>';
  const after = (eq && eq.after) || "", before = (eq && eq.before) || "";
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
      `<td class="num">${_cmpNum(a.average)}</td><td class="num">${_cmpRaw(a.stdev)}</td><td class="num">${_cmpNum(a.cpk)}</td>` +
      `<td class="num">${_cmpNum(b.average)}</td><td class="num">${_cmpRaw(b.stdev)}</td><td class="num">${_cmpNum(b.cpk)}</td>` +
      _cmpDeltaCell(r.delta_average) + _cmpDeltaCell(r.delta_stdev) + _cmpDeltaCell(r.delta_cpk) +
      gapCell(r.mean_gap_pct) + `</tr>`;
  }).join("");
  return `<div class="sheet-wrap cmp-scroll"><table class="sheet-table compare-table">${head}<tbody>${body}</tbody></table></div>`;
}

// ── 동일성 검증 — 항목별 Grade 판정 (Before pool vs After pool) ─────────────
// Grade1: AVG차(%) ≤ 5 / Grade2: 5 초과 & 양쪽 CPK ≥ 5 / Grade3: 그 외(판정 불가 포함).
// 판정 규칙·임계값은 서버(compare.py build_equivalence)가 정본이고 여기서는 표시만 한다.
function compareEquivHtml(eq) {
  if (!eq || !eq.rows) {
    return '<div class="placeholder">동일성 검증 데이터 없음</div>';
  }
  const th = eq.thresholds || {};
  const pctLimit = th.avg_pct == null ? 5 : th.avg_pct;
  const cpkLimit = th.cpk == null ? 5 : th.cpk;
  const s = eq.summary || {};
  const pair = `${esc(eq.before || "")} vs ${esc(eq.after || "")}`;
  const summary = `<div class="sheet-wrap eq-summary"><table class="sheet-table compare-table">
      <thead><tr><th>구분</th><th class="num">TestITEM</th>
        <th class="num">Grade1</th><th class="num">Grade2</th><th class="num">Grade3</th></tr></thead>
      <tbody><tr><td>${pair}</td><td class="num">${s.total || 0}</td>
        <td class="num">${s.grade1 || 0}</td><td class="num">${s.grade2 || 0}</td>
        <td class="num">${s.grade3 || 0}</td></tr></tbody></table></div>`;
  if (!eq.rows.length) {
    return summary + '<div class="placeholder">공통 항목 없음</div>';
  }

  const cpkCell = v => {
    const bad = (typeof v === "number") && v < cpkLimit;
    return `<td class="num${bad ? " eq-bad" : ""}">${_cmpNum(v)}</td>`;
  };
  const pctCell = v => {
    if (v === null || v === undefined) return `<td class="num">–</td>`;
    return `<td class="num${v > pctLimit ? " eq-bad" : ""}">${_cmpNum(v, 2)}</td>`;
  };
  const head = `<thead>
      <tr><th rowspan="2">STEP</th><th rowspan="2">Item</th><th rowspan="2">UNIT</th>
          <th class="num" rowspan="2">HiLIM</th><th class="num" rowspan="2">LoLIM</th>
          <th colspan="3">Before — ${esc(eq.before || "")}</th>
          <th colspan="3">After — ${esc(eq.after || "")}</th>
          <th class="num" rowspan="2">AVG차</th><th class="num" rowspan="2">AVG차(%)</th>
          <th rowspan="2">동일성</th></tr>
      <tr><th class="num">AVG</th><th class="num">STD</th><th class="num">CPK</th>
          <th class="num">AVG</th><th class="num">STD</th><th class="num">CPK</th></tr></thead>`;
  const body = eq.rows.map(r => {
    const b = r.before || {}, a = r.after || {};
    const g3 = r.grade === 3;
    return `<tr><td>${esc(r.step || "")}</td><td>${esc(r.subject)}</td><td>${esc(r.units || "")}</td>` +
      `<td class="num">${_cmpNum(r.hilim)}</td><td class="num">${_cmpNum(r.lolim)}</td>` +
      `<td class="num">${_cmpNum(b.average)}</td><td class="num">${_cmpRaw(b.stdev)}</td>${cpkCell(b.cpk)}` +
      `<td class="num">${_cmpNum(a.average)}</td><td class="num">${_cmpRaw(a.stdev)}</td>${cpkCell(a.cpk)}` +
      `<td class="num">${_cmpNum(r.delta_avg)}</td>` + pctCell(r.delta_pct) +
      `<td class="eq-grade${g3 ? " eq-grade3" : ""}">Grade ${r.grade}</td></tr>`;
  }).join("");
  const legend = `<div class="compare-summary eq-legend">
      <span class="cmp-chip">Grade1 · AVG차(%) ${pctLimit} 이하</span>
      <span class="cmp-chip">Grade2 · AVG차(%) ${pctLimit} 초과 &amp; 양쪽 CPK ${cpkLimit} 이상</span>
      <span class="cmp-chip cmp-unique">Grade3 · 그 외</span></div>`;
  return summary + legend +
    `<div class="sheet-wrap cmp-scroll"><table class="sheet-table compare-table">${head}<tbody>${body}</tbody></table></div>`;
}

function compareBinTableHtml(binDelta, sources, groups) {
  if (!binDelta || !binDelta.length) return '<div class="placeholder">Bin 데이터 없음</div>';
  const srcHead = sources.map(s =>
    `<th colspan="2">${esc(s)}<div class="gl-sub">${esc(((groups || {})[s] === "after") ? "After" : ((groups || {})[s] === "before" ? "Before" : ""))}</div></th>`).join("");
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
  const groups = cmp.groups || {};
  const cm = cmp.common_map || {};
  const c = cm.counts || {};
  const mismatch = Math.max(0, (c.common_dies || 0) - (c.match || 0));
  const eq = cmp.equivalence;
  const groupChips = (cmp.before_sources || []).length
    ? `<span class="cmp-chip">Before ${(cmp.before_sources || []).map(esc).join(" · ")}</span>` +
      `<span class="cmp-chip">After ${(cmp.after_sources || []).map(esc).join(" · ")}</span>`
    : "";

  panel.innerHTML =
    `<div class="compare-wrap">
      <div class="compare-summary">
        <span class="mk">Sources</span> ${sources.map(esc).join(" · ")}
        ${groupChips}
        <span class="cmp-chip">공통 die ${c.common_dies || 0}</span>
        <span class="cmp-chip cmp-common">Bin 일치 ${c.match || 0}</span>
        <span class="cmp-chip cmp-unique">Bin 불일치 ${mismatch}</span>
      </div>
      <div class="cmp-toolbar">
        <div class="cmp-subtabs distseg-group">
          <button class="distseg active" data-cmpsub="map">Map 비교</button>
          <button class="distseg" data-cmpsub="log">Log 비교</button>
          <button class="distseg" data-cmpsub="cpk">CPK 비교</button>
          <button class="distseg" data-cmpsub="equiv">동일성 검증</button>
        </div>
        ${(cmp.goodlog && !cmp.goodlog.identical && (cmp.goodlog.rows || []).length)
            ? `<button class="btn-sm gl-expand-all" type="button" hidden>전체 펼치기</button>` : ""}
      </div>
      <div class="cmp-subpanel active" data-cmppanel="map">
        <h3 class="compare-h">공통성 Map — Bin 일치=초록 / 한쪽만 Fail=source 색 / 2개↑ Fail=보라
          <span class="gl-sub">(die 에 마우스를 올리면 source 별 Bin 이 보입니다)</span></h3>
        <div class="wafer-card">
          <div id="cmp-common-map" style="width:100%;height:520px;"></div>
          <div id="cmp-common-legend" class="cmp-legend"></div>
        </div>
        <div class="cmp-bin-grid">
          <section>
            <h3 class="compare-h">동일 좌표 Bin 비교 (불일치 die)</h3>
            ${compareBinMatrixHtml(cmp.bin_matrix) || '<div class="placeholder">Bin 비교 데이터 없음</div>'}
          </section>
          <section>
            <h3 class="compare-h">Bin Yield 비교</h3>
            ${compareBinTableHtml(cmp.bin_delta, sources, groups)}
          </section>
        </div>
      </div>
      <div class="cmp-subpanel" data-cmppanel="log">
        ${goodlogSectionHtml(cmp.goodlog) || '<div class="placeholder">테스트 프로그램 비교(goodlog) 데이터 없음</div>'}
      </div>
      <div class="cmp-subpanel" data-cmppanel="cpk">
        <h3 class="compare-h">산포 차이 (공통 항목 · |ΔCpk| 큰 순 · 그룹 전체 die 기준)</h3>
        ${compareDistShiftHtml(cmp.dist_shift, eq)}
      </div>
      <div class="cmp-subpanel" data-cmppanel="equiv">
        <h3 class="compare-h">동일성 검증 (Before vs After · 그룹 전체 die 기준)</h3>
        ${compareEquivHtml(eq)}
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


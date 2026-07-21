// ── Trim Analysis 탭 ─────────────────────────────────────────────────────────
// 독립 화면 3개(① 항목 매칭 ② 산포 분석 ③ 분석 리포트). payload(매칭+통계)는 탭 첫
// 진입 시 GET .../web_report/trim_analysis 로 lazy 로드(세션 open 비용 없음)하고,
// 그룹 차트는 GET .../web_report/trim_chart 를 동시 8개 큐로 병렬 로드해 클라 캐시한다.
// 데이터 다운샘플 절대 없음(불변 규칙 #6) — 대량 chip 은 scattergl 로 렌더만 가속.
// 드래그앤드랍 수동 재배치는 POST .../web_report/trim/overrides (로그인 업로더만).
const TRIM = {
  COLORS: { INIT: "#2E6FE8", CODE: "#7C3AED", TRIM: "#16A34A", VERIFY: "#F59E0B" },
  CONCURRENCY: 8, GL_THRESHOLD: 2000,
  PAGE_SIZE: 6,             // ② 산포 분석: 한 페이지 6개(가로3·세로2)로 나눠 렌더
  REPORT_ENABLED: false,    // ③ 분석 리포트 임시 비활성(웹에서 숨김) — renderTrimReport 코드는 보존
};
let trimState = {
  view: "scatter",          // match | scatter | report (기본: ② 산포 분석)
  source: "",               // 선택 source ("" = 첫 소스, payload 도착 후 실제 이름으로 확정)
  payloads: {},             // source → payload (클라 캐시)
  payloadPromises: {},      // source → 진행 중 fetch (중복 방지)
  charts: {},               // `${source}||${group}` → chart payload (재조회 즉시 표시)
  chartPromises: {},        // 같은 키 fetch 중복 방지
  queue: [], inflight: 0,   // 차트 fetch 동시 CONCURRENCY 개 제한 큐
  filter: "all", search: "",
  showUnassigned: false,    // ① 매칭: stem 미산출(미배정) 항목 노출 여부 — 기본 숨김
  scatterPage: 0,           // ② 산포 현재 페이지(0-index)
  scatterSel: new Set(),    // ② 산포 검색 체크박스로 고른 그룹 id (있으면 그것만 표시)
  yBasis: "target",         // ② 산포 y축 범위 기준 슬롯: target(VERIFY/P2, 기본) | base(PRE/INIT)
};
function trimPayload() { return trimState.payloads[trimState.source || ""] || null; }

function ensureTrimPayload() {
  const key = trimState.source || "";
  if (trimState.payloads[key]) return Promise.resolve(trimState.payloads[key]);
  if (trimState.payloadPromises[key]) return trimState.payloadPromises[key];
  const url = `/pe/report/session/${SESSION_ID}/web_report/trim_analysis` +
    (key ? `?source=${encodeURIComponent(key)}` : "");
  const promise = fetch(url, { cache: "no-cache" })
    .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(p => {
      trimState.payloads[p.source] = p;
      if (!key) trimState.payloads[""] = p;   // 기본(첫 소스) 별칭
      trimState.source = p.source;
      return p;
    })
    .finally(() => { delete trimState.payloadPromises[key]; });
  trimState.payloadPromises[key] = promise;
  return promise;
}

function trimBodyError(err) {
  const body = document.getElementById("trimBody");
  if (body) body.innerHTML =
    `<div class="placeholder">Trim 데이터 로드 실패 (${esc((err && err.message) || err)})</div>`;
}

function renderTrimAnalysis() {
  const panel = document.getElementById("panel-trim-analysis");
  if (!panel) return;
  if (!isWebReportSession()) { emptyPanel(panel, "Trim Analysis 데이터 없음"); return; }
  panel.innerHTML = `
    <div class="trim-topbar">
      <div class="distseg-group" id="trimSubtabs">
        <button class="distseg" data-tview="match">① 항목 매칭</button>
        <button class="distseg" data-tview="scatter">② 산포 분석</button>
        ${TRIM.REPORT_ENABLED ? `<button class="distseg" data-tview="report">③ 분석 리포트</button>` : ""}
      </div>
      <div class="distseg-group" id="trimYBasis" style="display:none">
        <button class="distseg" data-ybasis="base"
          title="보이는 차트의 y축 범위를 PRE(INIT) 슬롯 항목의 LSL/USL ±15% 로 잡는다">PRE(INIT) 기준 y축</button>
        <button class="distseg" data-ybasis="target"
          title="보이는 차트의 y축 범위를 VERIFY(P2) 슬롯 항목의 LSL/USL ±15% 로 잡는다">VERIFY(P2) 기준 y축</button>
      </div>
      <select id="trimSource" class="trim-source" style="display:none" title="분석 source 선택"></select>
      <span id="trimRule" class="trim-rule"></span>
      <button class="btn-sm" id="trimExcelBtn" title="현재 탭 데이터(매칭/리포트 표 + 차트 PNG)를 xlsx 로 다운로드">Excel 다운로드</button>
      <span class="dist-count" id="trimCount"></span>
    </div>
    <div id="trimBody"><div class="placeholder">Trim 매칭 계산 중…</div></div>`;
  panel.querySelector("#trimSubtabs").addEventListener("click", e => {
    const b = e.target.closest("[data-tview]");
    if (!b) return;
    trimState.view = b.dataset.tview;
    renderTrimView();
  });
  panel.querySelector("#trimYBasis").addEventListener("click", e => {
    const b = e.target.closest("[data-ybasis]");
    if (!b || b.dataset.ybasis === trimState.yBasis) return;
    trimState.yBasis = b.dataset.ybasis;
    renderTrimView();          // 현재 페이지 6개 차트를 캐시된 payload 로 즉시 재렌더
  });
  panel.querySelector("#trimSource").addEventListener("change", e => {
    trimState.source = e.target.value;
    trimState.scatterPage = 0;         // source 바뀌면 그룹 구성이 달라지므로 산포 상태 초기화
    trimState.scatterSel.clear();
    document.getElementById("trimBody").innerHTML = `<div class="placeholder">로드 중…</div>`;
    ensureTrimPayload().then(renderTrimView).catch(trimBodyError);
  });
  panel.querySelector("#trimExcelBtn").addEventListener("click", exportTrimExcel);
  ensureTrimPayload().then(renderTrimView).catch(trimBodyError);
}

function renderTrimView() {
  const p = trimPayload();
  const body = document.getElementById("trimBody");
  if (!p || !body) return;
  if (!TRIM.REPORT_ENABLED && trimState.view === "report") trimState.view = "match";
  document.querySelectorAll("#trimSubtabs [data-tview]").forEach(b =>
    b.classList.toggle("active", b.dataset.tview === trimState.view));
  // y축 기준 토글은 ② 산포 분석에서만 노출(다른 뷰엔 차트가 없다).
  const yb = document.getElementById("trimYBasis");
  if (yb) {
    yb.style.display = trimState.view === "scatter" ? "" : "none";
    yb.querySelectorAll("[data-ybasis]").forEach(b =>
      b.classList.toggle("active", b.dataset.ybasis === trimState.yBasis));
  }
  const sel = document.getElementById("trimSource");
  if (sel) {
    const sources = p.sources || [];
    if (sources.length > 1) {
      sel.style.display = "";
      sel.innerHTML = sources.map(s =>
        `<option value="${esc(s.name)}"${s.name === p.source ? " selected" : ""}>${esc(s.name)}</option>`).join("");
    } else sel.style.display = "none";
  }
  const rule = document.getElementById("trimRule");
  if (rule) rule.textContent = p.rule_set === "TV2"
    ? "TRIM/VERIFY 2-section (MDDI·PDDI)" : "INIT/CODE/TRIM/VERIFY 4-phase";
  trimState.queue = [];
  trimPurgePlots(body);   // 뷰 전환 시 남은 산포 차트 정리(누수 방지)
  if (trimState.view === "match") renderTrimMatch(body, p);
  else if (trimState.view === "scatter") renderTrimScatter(body, p);
  else renderTrimReport(body, p);
}

// body 안에 남아 있는 산포 Plotly 차트를 모두 purge (재렌더/뷰 전환 전 정리).
function trimPurgePlots(body) {
  if (!body || !window.Plotly) return;
  body.querySelectorAll(".trim-plot").forEach(div => { try { Plotly.purge(div); } catch (e) {} });
}

// ── 공용: shift 배지 (position 을 20~80 스케일로 환산해 "{target} NN%ile") ──────
function trimShiftBadge(p, g) {
  const sh = g.shift || {};
  if (!sh.eligible) return "";
  const pct = Math.round(20 + (sh.position || 0) * 60);
  const cls = sh.is_shift ? " shift" : "";
  return `<span class="trim-badge${cls}" title="${esc(p.base)} p20~p80 밴드 내 ${esc(p.target)} 평균 위치">${esc(p.target)} ${pct}%ile</span>`;
}

// ── 화면 ① 항목 매칭: 그룹 슬롯 카드(드래그앤드랍) + 미배정 + 매칭 상세 표 ──────
function trimItemChip(name, itemsMap, canEdit) {
  const info = itemsMap[name] || {};
  const draggable = canEdit ? ` draggable="true"` : "";
  const ovr = info.override ? `<span class="trim-ovr" title="수동 배치">✎</span>` : "";
  const reset = (info.override && canEdit)
    ? `<button class="trim-reset" data-item="${esc(name)}" title="자동 배치로 되돌리기">↺</button>` : "";
  return `<span class="trim-chip" data-item="${esc(name)}"${draggable} title="${esc(name)}">${ovr}${esc(name)}${reset}</span>`;
}

function renderTrimMatch(body, p) {
  const itemsMap = {};
  (p.items || []).forEach(i => { itemsMap[i.name] = i; });
  const canEdit = canEditSession();
  const groups = p.groups || [];
  const cards = groups.map(g => {
    const slotRows = p.phases.map(ph => {
      const item = g.slots[ph];
      return `<div class="trim-slot" data-group="${esc(g.id)}" data-slot="${esc(ph)}">
        <span class="trim-slot-label" style="color:${TRIM.COLORS[ph]}">${esc(ph)}</span>
        <span class="trim-slot-val">${item ? trimItemChip(item, itemsMap, canEdit)
          : `<span class="trim-empty">비어 있음</span>`}</span>
      </div>`;
    }).join("");
    const extras = (g.members || []).filter(m => !p.phases.some(ph => g.slots[ph] === m));
    const memberRow = `<div class="trim-slot trim-members" data-group="${esc(g.id)}" data-slot="MEMBER">
      <span class="trim-slot-label">MEMBER</span>
      <span class="trim-slot-val">${extras.length
        ? extras.map(m => trimItemChip(m, itemsMap, canEdit)).join("")
        : `<span class="trim-empty">-</span>`}</span></div>`;
    const flagBadges = [
      g.flags.complete_base_target ? `<span class="trim-badge ok">${esc(p.base)}+${esc(p.target)}</span>` : "",
      g.flags.complete_4phase ? `<span class="trim-badge ok">4-phase</span>` : "",
      g.cpk_warn ? `<span class="trim-badge warn">cpk&lt;${p.constants.cpk_threshold}</span>` : "",
      trimShiftBadge(p, g),
      g.manual ? `<span class="trim-badge manual">수동 그룹</span>` : "",
    ].join("");
    return `<div class="trim-card${g.cpk_warn ? " cpk-warn" : ""}" data-group="${esc(g.id)}">
      <div class="trim-card-head"><span class="trim-card-title" title="${esc(g.id)}">${esc(g.id)}</span>${flagBadges}</div>
      ${slotRows}${memberRow}
    </div>`;
  }).join("");

  const unassigned = (p.items || []).filter(i => !i.group);
  const unassignedChips = unassigned.map(i => {
    const badge = i.excluded ? `<span class="trim-badge excl">제외</span>` : "";
    return `<span class="trim-chip-wrap">${trimItemChip(i.name, itemsMap, canEdit)}${badge}</span>`;
  }).join("");

  const invalid = p.invalid_overrides || [];
  const invalidNote = invalid.length
    ? `<div class="trim-note">무효 override ${invalid.length}건 (항목 없음/슬롯 불일치): ${invalid.map(esc).join(", ")}</div>` : "";
  const editNote = canEdit
    ? `<div class="trim-note">항목 칩을 끌어 다른 그룹의 슬롯/MEMBER 에 놓으면 수동 재배치가 서버에 저장됩니다 (자동 매칭보다 우선). ↺ = 자동 배치 복귀.</div>`
    : `<div class="trim-note">수동 재배치는 로그인한 업로더만 가능합니다 (현재 읽기 전용).</div>`;

  const rows = (p.items || []).map(i => `<tr class="${i.group ? "" : "is-unassigned"}">
    <td>${esc(i.name)}</td><td>${esc(i.normalized)}</td>
    <td>${esc((i.tokens || []).join(" · "))}</td>
    <td>${i.phase ? `<span style="color:${TRIM.COLORS[i.phase]};font-weight:700">${esc(i.phase)}</span>`
      : (i.excluded ? "제외" : "-")}</td>
    <td>${esc(i.stem || "-")}</td><td>${esc(i.group || "-")}</td>
    <td>${esc(i.slot || (i.group ? "MEMBER" : "-"))}</td>
    <td>${i.override ? "✎ 수동" : ""}</td>
  </tr>`).join("");

  // 왼쪽 팔레트: 전체 항목을 세로 리스트로. 검색으로 필터하고, 칩을 끌어 오른쪽 그룹 슬롯에 놓는다.
  const paletteItems = (p.items || []).map(i => {
    const slotTxt = i.slot || (i.group ? "MEMBER" : "");
    const status = i.group ? (slotTxt ? `${i.group} · ${slotTxt}` : i.group)
      : (i.excluded ? "제외" : "미배정");
    const metaCls = i.group ? " assigned" : (i.excluded ? " excl" : "");
    const key = `${i.name} ${i.group || ""} ${i.normalized || ""}`.toLowerCase();
    return `<div class="trim-palette-item${i.group ? "" : " is-unassigned"}" data-name="${esc(key)}">
      ${trimItemChip(i.name, itemsMap, canEdit)}
      <span class="trim-palette-meta${metaCls}" title="${esc(status)}">${esc(status)}</span>
    </div>`;
  }).join("") || `<div class="trim-empty" style="padding:10px">항목 없음</div>`;

  // 미배정(stem 미산출) 항목은 기본 숨김 — 래퍼 클래스 하나로 팔레트·드롭존·상세표를 함께 제어.
  // 드롭존 자체는 숨기지 않는다(자동 배치 복귀 타깃). 칩이 안 보일 때만 안내 문구를 띄운다.
  const nTotal = (p.items || []).length;
  const nUnassigned = unassigned.length;
  const dropHint = `<span class="trim-empty trim-drop-hint${nUnassigned ? "" : " always"}">여기로 끌어놓으면 자동 배치로 복귀</span>`;

  body.innerHTML = editNote + invalidNote +
    `<div id="trimMatchWrap"${trimState.showUnassigned ? "" : ` class="trim-hide-unassigned"`}>
     <div class="trim-match-layout">
       <aside class="trim-palette">
         <div class="trim-palette-head">
           <div class="trim-palette-searchwrap">
             <input class="trim-palette-search" id="trimMatchSearch" type="text" autocomplete="off"
               placeholder="항목 찾기…">
           </div>
           <div class="trim-palette-count" id="trimPaletteCount"></div>
           <label class="trim-palette-toggle"><input type="checkbox" id="trimShowUnassigned"${
             trimState.showUnassigned ? " checked" : ""}>미배정 ${nUnassigned}개 보기</label>
         </div>
         <div class="trim-palette-list">${paletteItems}</div>
       </aside>
       <div class="trim-match-main">
         ${cards ? `<div class="trim-match-grid">${cards}</div>`
                 : `<div class="placeholder">매칭된 그룹이 없습니다</div>`}
         <div class="section-title small" style="margin-top:14px">미배정 항목 (여기로 끌어오면 자동 배치 복귀)</div>
         <div class="trim-unassigned" data-drop="reset">${unassignedChips}${dropHint}</div>
         ${canEdit ? `<div class="trim-newgroup" data-drop="newgroup">+ 새 그룹으로 끌어오기</div>` : ""}
       </div>
     </div>` +
    `<div class="section-title small" style="margin-top:16px">항목 매칭 상세</div>
     <div class="trim-table-wrap"><table class="trim-table">
       <thead><tr><th>원본명</th><th>정규화</th><th>토큰</th><th>Phase</th><th>Stem</th><th>그룹</th><th>슬롯</th><th>수동</th></tr></thead>
       <tbody>${rows}</tbody></table></div></div>`;

  const cnt = document.getElementById("trimCount");
  if (cnt) cnt.textContent =
    `그룹 ${groups.length}개 · 매칭 ${nTotal - nUnassigned}개 · 미배정 ${nUnassigned}개`;
  if (canEdit) bindTrimDnD(body);
  body.querySelectorAll(".trim-reset").forEach(b => b.addEventListener("click", e => {
    e.stopPropagation();
    saveTrimOverrides([{ item: b.dataset.item, reset: true }]);
  }));
  const psearch = document.getElementById("trimMatchSearch");
  const pcount = document.getElementById("trimPaletteCount");
  // 검색 표시 + 카운트 — 카운트는 현재 모드(미배정 숨김 여부)에서 셀 수 있는 항목만 센다.
  // display 는 전 항목에 걸되, 숨김 모드의 미배정은 CSS 규칙이 인라인 "" 를 계속 이긴다.
  const applyPaletteFilter = () => {
    const q = psearch ? psearch.value.trim().toLowerCase() : "";
    let vis = 0, total = 0;
    body.querySelectorAll(".trim-palette-item").forEach(el => {
      const match = !q || (el.dataset.name || "").includes(q);
      el.style.display = match ? "" : "none";
      if (trimState.showUnassigned || !el.classList.contains("is-unassigned")) {
        total++;
        if (match) vis++;
      }
    });
    if (pcount) pcount.textContent = q ? `${vis} / ${total}개`
      : (trimState.showUnassigned ? `전체 ${total}개` : `전체 ${nTotal}개 · 매칭 ${total}개`);
  };
  applyPaletteFilter();
  if (psearch) psearch.addEventListener("input", applyPaletteFilter);
  const ptoggle = document.getElementById("trimShowUnassigned");
  const pwrap = document.getElementById("trimMatchWrap");
  if (ptoggle) ptoggle.addEventListener("change", () => {
    trimState.showUnassigned = ptoggle.checked;
    if (pwrap) pwrap.classList.toggle("trim-hide-unassigned", !ptoggle.checked);
    applyPaletteFilter();
  });
}

function bindTrimDnD(body) {
  body.querySelectorAll('.trim-chip[draggable="true"]').forEach(chip => {
    chip.addEventListener("dragstart", e => {
      e.dataTransfer.setData("text/plain", chip.dataset.item);
      e.dataTransfer.effectAllowed = "move";
    });
  });
  body.querySelectorAll(".trim-slot, .trim-unassigned, .trim-newgroup").forEach(z => {
    z.addEventListener("dragover", e => { e.preventDefault(); z.classList.add("dragover"); });
    z.addEventListener("dragleave", () => z.classList.remove("dragover"));
    z.addEventListener("drop", e => {
      e.preventDefault();
      z.classList.remove("dragover");
      const item = e.dataTransfer.getData("text/plain");
      if (!item) return;
      if (z.dataset.drop === "reset") { saveTrimOverrides([{ item, reset: true }]); return; }
      if (z.dataset.drop === "newgroup") {
        const name = (window.prompt("새 그룹 이름", "") || "").trim();
        if (name) saveTrimOverrides([{ item, group: name, slot: "MEMBER" }]);
        return;
      }
      saveTrimOverrides([{ item, group: z.dataset.group, slot: z.dataset.slot }]);
    });
  });
}

let _trimSaving = 0;   // 진행 중 저장 수 — 드롭 직후 페이지 이탈로 요청이 끊기기 전 경고용

async function saveTrimOverrides(ops) {
  _trimSaving++;
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/trim/overrides`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ ops }),
      keepalive: true,   // 드롭 직후 이탈해도 요청이 완료된다 (ops 는 1건이라 소형)
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) { showToast(j.error || `저장 실패 (HTTP ${res.status})`); return; }
    showToast("Trim 재배치 저장됨");
    // 그룹 구성이 바뀌므로 클라 캐시를 비우고 재조회. 서버 payload 캐시는 manifest digest
    // 변경으로 자연 미스, 구성이 안 바뀐 그룹의 차트는 서버 캐시가 그대로 재사용된다.
    trimState.payloads = {};
    trimState.charts = {};
    trimState.chartPromises = {};
    trimState.scatterPage = 0;         // 그룹 재구성 → 산포 페이지/선택 초기화
    trimState.scatterSel.clear();
    const body = document.getElementById("trimBody");
    if (body) body.innerHTML = `<div class="placeholder">재계산 중…</div>`;
    ensureTrimPayload().then(renderTrimView).catch(trimBodyError);
  } catch (e) {
    showToast("저장 실패: " + e.message);
  } finally {
    _trimSaving--;
  }
}

// 저장이 서버에 닿기 전 이탈하면 유실 — 진행 중이면 브라우저 경고를 띄운다.
window.addEventListener("beforeunload", e => {
  if (_trimSaving) { e.preventDefault(); e.returnValue = ""; }
});

// ── 그룹 차트 fetch 큐 (동시 CONCURRENCY 개 제한 + 클라 캐시) ──────────────────
function trimFetchChart(group) {
  const source = trimState.source || "";
  const key = `${source}||${group}`;
  if (trimState.charts[key]) return Promise.resolve(trimState.charts[key]);
  if (trimState.chartPromises[key]) return trimState.chartPromises[key];
  const promise = new Promise((resolve, reject) => {
    trimState.queue.push({ key, group, source, resolve, reject });
    trimPumpQueue();
  });
  trimState.chartPromises[key] = promise;
  return promise;
}

function trimPumpQueue() {
  while (trimState.inflight < TRIM.CONCURRENCY && trimState.queue.length) {
    const job = trimState.queue.shift();
    trimState.inflight++;
    const q = new URLSearchParams({ source: job.source, group: job.group });
    fetch(`/pe/report/session/${SESSION_ID}/web_report/trim_chart?${q}`, { cache: "no-cache" })
      .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(chart => { trimState.charts[job.key] = chart; job.resolve(chart); })
      .catch(err => { delete trimState.chartPromises[job.key]; job.reject(err); })
      .finally(() => { trimState.inflight--; trimPumpQueue(); });
  }
}

// ── 화면 ② 산포 분석: 한 페이지 6개(3×2) 페이지네이션 + Distribution식 검색·선택 ─────
// 관찰자 purge 방식(스크롤 때 그렸다 지웠다 → 사라짐)을 버리고, 현재 페이지 ≤6개만 직접
// 렌더해 그대로 유지한다. 검색은 체크박스 제안 드롭다운으로 그룹을 골라 그것만 표시한다.
function renderTrimScatter(body, p) {
  trimPurgePlots(body);                 // 재렌더 전 이전 페이지 차트 정리(누수 방지)
  const allGroups = p.groups || [];
  // 존재하지 않는 그룹 선택은 정리(source/override 변경 대비).
  if (trimState.scatterSel.size) {
    const ids = new Set(allGroups.map(g => g.id));
    [...trimState.scatterSel].forEach(id => { if (!ids.has(id)) trimState.scatterSel.delete(id); });
  }
  const sel = trimState.scatterSel;
  const groups = sel.size ? allGroups.filter(g => sel.has(g.id)) : allGroups;
  const cnt = document.getElementById("trimCount");

  if (!groups.length) {
    body.innerHTML = trimScatterToolbarHtml() +
      `<div class="placeholder">${sel.size ? "선택한 그룹이 없습니다" : "매칭된 그룹이 없습니다"}</div>`;
    if (cnt) cnt.textContent = "";
    bindTrimScatterToolbar(body, p, allGroups);
    return;
  }

  const pageCount = Math.max(1, Math.ceil(groups.length / TRIM.PAGE_SIZE));
  trimState.scatterPage = Math.min(Math.max(trimState.scatterPage, 0), pageCount - 1);
  const page = trimState.scatterPage;
  const start = page * TRIM.PAGE_SIZE;
  const pageGroups = groups.slice(start, start + TRIM.PAGE_SIZE);

  const cards = pageGroups.map(g => `
    <div class="trim-gcard${g.cpk_warn ? " cpk-warn" : ""}" data-group="${esc(g.id)}">
      <div class="trim-gcard-head">
        <span class="trim-card-title" title="${esc(g.id)}">${esc(g.id)}</span>
        ${trimShiftBadge(p, g)}
        ${g.cpk_warn ? `<span class="trim-badge warn">cpk&lt;${p.constants.cpk_threshold}</span>` : ""}
        <button class="btn-sm trim-png" title="차트를 PNG 로 클립보드에 복사">클립보드로 복사</button>
      </div>
      <div class="trim-gplot"><div class="placeholder">대기 중…</div></div>
    </div>`).join("");

  body.innerHTML = trimScatterToolbarHtml() +
    `<div class="trim-gallery">${cards}</div>` +
    trimScatterPagerHtml(page, pageCount);
  if (cnt) cnt.textContent = sel.size
    ? `그룹 ${groups.length}/${allGroups.length}개` : `그룹 ${groups.length}개`;

  // 현재 페이지 카드만 직접 렌더(관찰자 없음 → purge-on-scroll 없음 → 사라지지 않음).
  body.querySelectorAll(".trim-gcard").forEach(c => { c.dataset.visible = "1"; trimLoadCard(c); });
  bindTrimScatterToolbar(body, p, allGroups);
}

// 검색 툴바(Distribution 클래스 재사용): 선택 해제 칩 + 검색 입력 + 제안 드롭다운.
function trimScatterToolbarHtml() {
  const n = trimState.scatterSel.size;
  const selChip = n
    ? `<div class="distseg-group"><button class="distseg trim-scatter-clear" title="선택 해제">선택 ${n}개 ✕</button></div>` : "";
  return `<div class="dist-toolbar">
    ${selChip}
    <div class="dist-search-wrap">
      <input id="trimScatterSearch" class="dist-search" type="text" autocomplete="off" placeholder="그룹/항목 검색 (체크로 선택)">
      <div id="trimScatterSuggest" class="dist-suggest" style="display:none"></div>
    </div>
  </div>`;
}

// 페이저: ◀ 이전 + 페이지 번호(많으면 …로 축약) + 다음 ▶. 페이지 1개면 숨김.
function trimScatterPagerHtml(page, pageCount) {
  if (pageCount <= 1) return "";
  const btn = (label, target, opts) => {
    const o = opts || {};
    return `<button class="trim-pager-btn${o.active ? " active" : ""}" data-tpage="${target}"${o.disabled ? " disabled" : ""}>${label}</button>`;
  };
  const nums = trimPageWindow(page, pageCount).map(n =>
    n === "…" ? `<span class="trim-pager-gap">…</span>` : btn(String(n + 1), n, { active: n === page })).join("");
  return `<div class="trim-pager">
    ${btn("◀ 이전", page - 1, { disabled: page <= 0 })}
    ${nums}
    ${btn("다음 ▶", page + 1, { disabled: page >= pageCount - 1 })}
  </div>`;
}

// 표시할 페이지 번호 배열 — 9개 이하는 전부, 많으면 첫/끝 + 현재±2 만 (…) 로 축약.
function trimPageWindow(page, pageCount) {
  if (pageCount <= 9) return Array.from({ length: pageCount }, (_, i) => i);
  const keep = new Set([0, pageCount - 1, page]);
  for (let d = 1; d <= 2; d++) { keep.add(Math.max(0, page - d)); keep.add(Math.min(pageCount - 1, page + d)); }
  const out = [];
  let prev = null;
  [...keep].sort((a, b) => a - b).forEach(n => {
    if (prev !== null && n - prev > 1) out.push("…");
    out.push(n); prev = n;
  });
  return out;
}

function trimScatterSuggestions(q, allGroups) {
  const term = String(q || "").trim().toLowerCase();
  if (!term) return [];
  const out = [];
  for (const g of allGroups) {
    if (String(g.id).toLowerCase().includes(term) ||
        (g.members || []).some(m => String(m).toLowerCase().includes(term))) {
      out.push(g);
      if (out.length >= 30) break;
    }
  }
  return out;
}

function trimRenderScatterSuggest(q, allGroups) {
  const box = document.getElementById("trimScatterSuggest");
  if (!box) return;
  const items = trimScatterSuggestions(q, allGroups);
  if (!String(q).trim() || !items.length) { box.innerHTML = ""; box.style.display = "none"; return; }
  box.innerHTML = items.map(g => {
    const nMem = (g.members || []).length;
    return `<label class="dist-sug-item">
      <input type="checkbox" class="dist-sug-chk" data-group="${esc(g.id)}"${trimState.scatterSel.has(g.id) ? " checked" : ""}>
      <span class="sug-tno">${nMem ? esc(nMem + "항목") : ""}</span>
      <span class="sug-name">${esc(g.id)}</span>
    </label>`;
  }).join("");
  box.style.display = "block";
}

// 재렌더 후 검색 입력값·포커스·드롭다운 복원(체크박스를 연속으로 고를 수 있게).
function restoreTrimScatterSearch(q, allGroups) {
  const inp = document.getElementById("trimScatterSearch");
  if (!inp) return;
  inp.value = q || "";
  if (q) { inp.focus(); trimRenderScatterSuggest(q, allGroups); }
}

// 산포 툴바/페이저/카드 이벤트 바인딩(재렌더마다 재바인딩 — 기존 뷰 방식과 동일).
function bindTrimScatterToolbar(body, p, allGroups) {
  body.querySelectorAll(".trim-png").forEach(b => b.addEventListener("click", () => {
    const card = b.closest(".trim-gcard");
    const gd = card && card.querySelector(".trim-gplot .js-plotly-plot");
    if (gd) trimCopyPng(gd, card.dataset.group);
    else showToast("차트가 아직 로드되지 않았습니다");
  }));
  const search = body.querySelector("#trimScatterSearch");
  if (search) search.addEventListener("input", () => trimRenderScatterSuggest(search.value, allGroups));
  const suggest = body.querySelector("#trimScatterSuggest");
  if (suggest) suggest.addEventListener("change", e => {
    const chk = e.target.closest(".dist-sug-chk");
    if (!chk) return;
    if (chk.checked) trimState.scatterSel.add(chk.dataset.group);
    else trimState.scatterSel.delete(chk.dataset.group);
    trimState.scatterPage = 0;
    const q = (body.querySelector("#trimScatterSearch") || {}).value || "";
    renderTrimScatter(body, p);
    restoreTrimScatterSearch(q, allGroups);
  });
  const clear = body.querySelector(".trim-scatter-clear");
  if (clear) clear.addEventListener("click", () => {
    trimState.scatterSel.clear();
    trimState.scatterPage = 0;
    renderTrimScatter(body, p);
  });
  body.querySelectorAll(".trim-pager-btn").forEach(b => b.addEventListener("click", () => {
    if (b.disabled) return;
    const t = parseInt(b.dataset.tpage, 10);
    if (isNaN(t) || t === trimState.scatterPage) return;
    trimState.scatterPage = t;
    renderTrimScatter(body, p);
    const gal = body.querySelector(".trim-gallery");
    if (gal) gal.scrollIntoView({ block: "nearest" });
  }));
}

function trimLoadCard(card) {
  if (card.dataset.rendered === "1") return;
  const plotWrap = card.querySelector(".trim-gplot");
  trimFetchChart(card.dataset.group)
    .then(chart => {
      if (!card.isConnected || card.dataset.visible !== "1" || card.dataset.rendered === "1") return;
      plotWrap.innerHTML = "";
      const div = document.createElement("div");
      div.className = "trim-plot";
      plotWrap.appendChild(div);
      drawTrimChart(div, chart, trimPayload());
      card.dataset.rendered = "1";
    })
    .catch(err => {
      if (card.isConnected) plotWrap.innerHTML =
        `<div class="placeholder">차트 로드 실패 (${esc(err.message)})</div>`;
    });
}

// ── y축 범위: TRIM 평균이 산포 display 정중앙에 오도록 고정(무조건). 나머지(다른 phase
//    데이터 + target 평균·base band 기준선)를 그 중심에 대칭으로 맞춰 스케일한다. CODE 는
//    보조축(y2)이라 제외. TRIM 슬롯/데이터가 없으면 null → 기존 autorange 유지. ──────────
function trimYRangeCenteredOnTrim(chart, phases) {
  const trimSpec = chart.phases && chart.phases.TRIM;
  if (!trimSpec) return null;
  const trimYs = (trimSpec.y || []).filter(v => Number.isFinite(v));
  if (!trimYs.length) return null;
  const trimMean = trimYs.reduce((a, b) => a + b, 0) / trimYs.length;
  let maxDist = 0;
  const consider = v => { if (Number.isFinite(v)) { const d = Math.abs(v - trimMean); if (d > maxDist) maxDist = d; } };
  phases.forEach(ph => {
    if (ph === "CODE") return;
    const spec = chart.phases[ph];
    if (spec) (spec.y || []).forEach(consider);
  });
  consider(chart.target_mean);
  if (chart.base_band) { consider(chart.base_band.p20); consider(chart.base_band.p80); }
  let half = maxDist * 1.05;                                 // 5% 여백
  if (!(half > 0)) half = Math.abs(trimMean) * 0.05 || 1;    // 전부 동일값 폴백
  return [trimMean - half, trimMean + half];
}

// ── y축 범위: USL/LSL 기준 ±15% 창. lo/hi 둘 다 있고 hi>lo 일 때만 유효(아니면 null →
//    호출부가 폴백). TRIM/VERIFY 가 spec 밴드 안에 오도록 프레이밍하고, CODE(y2)도 같은
//    방식으로 프레이밍해 두 축의 spec 밴드가 시각적으로 나란히 보이게 한다(스케일 맞춤). ──
function trimYRangeFromLimits(lo, hi) {
  if (lo === null || lo === undefined || hi === null || hi === undefined) return null;
  const span = hi - lo;
  if (!(span > 0)) return null;                              // lo==hi 등 비정상 → 폴백
  const pad = span * 0.15;
  return [lo - pad, hi + pad];
}

// ── 차트 스펙: chip-to-chip 라인+마커, phase 별 trace 오버레이, CODE 는 y2 ──────
function drawTrimChart(div, chart, payload) {
  const phases = (payload && payload.phases) || ["INIT", "CODE", "TRIM", "VERIFY"];
  const n = chart.n || 0;
  const x = Array.from({ length: n }, (_, i) => i + 1);
  const useGl = n > TRIM.GL_THRESHOLD;
  const custom = (chart.serial || []).map((s, i) =>
    [s, (chart.xpos || [])[i], (chart.ypos || [])[i]]);
  const traces = [];
  phases.forEach(ph => {
    const spec = chart.phases[ph];
    if (!spec) return;
    traces.push({
      type: useGl ? "scattergl" : "scatter", mode: "lines+markers",
      name: `${ph} · ${spec.item}`,
      x, y: spec.y,
      yaxis: ph === "CODE" ? "y2" : "y",
      line: { color: TRIM.COLORS[ph], width: 1 },
      marker: { color: TRIM.COLORS[ph], size: 3 },
      connectgaps: false,
      customdata: custom,
      hovertemplate: `${ph} %{y}<br>chip %{x} · S/N %{customdata[0]} (%{customdata[1]},%{customdata[2]})<extra>${esc(spec.item)}</extra>`,
    });
  });

  // LSL/USL 점선 — main 축은 target(폴백 base) 항목의 limit, CODE limit 은 y2 점선
  const mainSpec = chart.phases[chart.target] || chart.phases[chart.base] || {};
  const shapes = [];
  const annotations = [];
  [["LSL", mainSpec.lo], ["USL", mainSpec.hi]].forEach(([label, v]) => {
    if (v === null || v === undefined) return;
    shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, y0: v, y1: v,
      line: { color: "#DC2626", width: 1.2, dash: "dash" } });
    annotations.push({ xref: "paper", x: 1, y: v, text: `${label} ${v}`, showarrow: false,
      font: { size: 10, color: "#DC2626" }, xanchor: "right", yanchor: "bottom",
      bgcolor: "rgba(255,255,255,.72)" });
  });
  const codeSpec = chart.phases.CODE;
  if (codeSpec) [codeSpec.lo, codeSpec.hi].forEach(v => {
    if (v === null || v === undefined) return;
    shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, yref: "y2", y0: v, y1: v,
      line: { color: TRIM.COLORS.CODE, width: 1, dash: "dot" } });
  });
  // base p20~p80 음영 밴드(initial shift 판정 밴드) + target 평균 중심선
  if (chart.base_band && chart.base_band.p20 != null && chart.base_band.p80 != null) {
    shapes.push({ type: "rect", xref: "paper", x0: 0, x1: 1,
      y0: chart.base_band.p20, y1: chart.base_band.p80,
      fillcolor: "rgba(46,111,232,.08)", line: { width: 0 }, layer: "below" });
  }
  if (chart.target_mean != null) {
    const tColor = TRIM.COLORS[chart.target] || "#16A34A";
    shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1,
      y0: chart.target_mean, y1: chart.target_mean,
      line: { color: tColor, width: 1.4, dash: "dashdot" } });
    annotations.push({ xref: "paper", x: 0, y: chart.target_mean,
      text: `${chart.target} 평균`, showarrow: false,
      font: { size: 10, color: tColor }, xanchor: "left", yanchor: "bottom",
      bgcolor: "rgba(255,255,255,.72)" });
  }

  const layout = { ...DIST_PLOT_BG,
    // legend 를 산포 위쪽(가로)으로 올린다 — 플롯을 가리지 않고 상단에 나란히.
    // autoexpand(기본 on)가 legend 높이만큼 상단 여백을 확보한다.
    legend: { orientation: "h", x: 0.5, xanchor: "center", y: 1.0, yanchor: "bottom",
      font: { size: 9 }, itemsizing: "constant" },
    xaxis: { title: { text: `chip (${chart.order_by} 오름차순)`, font: { size: 11 } },
      showgrid: true, gridcolor: "#eee", zeroline: false, tickfont: { size: 10 } },
    yaxis: { title: { text: mainSpec.units || "", font: { size: 11 } },
      showgrid: true, gridcolor: "#eee", zeroline: false, tickfont: { size: 10 } },
    shapes, annotations,
    margin: { l: 54, r: 54, t: 46, b: 38 },
    showlegend: true };
  // 메인 y축: USL/LSL ±15% 창(spec 기준)으로 TRIM/VERIFY 를 보이게 한다.
  // 기준 슬롯은 상단 토글(trimState.yBasis) — target(VERIFY/P2, 기본) 또는 base(PRE/INIT).
  // **범위만** 바꾼다: LSL/USL 점선과 y축 단위 제목은 mainSpec(target) 기준 그대로다.
  // base 기준인데 그 슬롯이 없거나 limit 이 없으면 target limit → TRIM 평균 중심 순 폴백.
  const rangeSpec = (trimState.yBasis === "base" && chart.phases[chart.base]) || mainSpec;
  const yRange = trimYRangeFromLimits(rangeSpec.lo, rangeSpec.hi)
    || trimYRangeFromLimits(mainSpec.lo, mainSpec.hi)
    || trimYRangeCenteredOnTrim(chart, phases);
  if (yRange) { layout.yaxis.range = yRange; layout.yaxis.autorange = false; }
  if (codeSpec) {
    layout.yaxis2 = { overlaying: "y", side: "right", showgrid: false,
      title: { text: `CODE${codeSpec.units ? " (" + codeSpec.units + ")" : ""}`,
        font: { size: 11, color: TRIM.COLORS.CODE } },
      tickfont: { size: 10, color: TRIM.COLORS.CODE } };
    // CODE 도 자기 USL/LSL ±15% 창으로 프레이밍 — 메인축과 같은 방식이라 시각적으로
    // 나란히 보인다(스케일 맞춤). limit 없으면 autorange 유지.
    const y2Range = trimYRangeFromLimits(codeSpec.lo, codeSpec.hi);
    if (y2Range) { layout.yaxis2.range = y2Range; layout.yaxis2.autorange = false; }
  }
  // ±15% spec 창은 기본 뷰일 뿐 축을 잠그지 않는다 — INIT 등 창 밖으로 잘린 데이터를
  // 사용자가 전체로 보려면 mode bar(hover 시 표시)의 autoscale/줌, 또는 더블클릭
  // autoscale 로 zoom out 한다. (displayModeBar:false 를 제거해 상호작용 허용.)
  Plotly.newPlot(div, traces, layout,
    { responsive: true, displaylogo: false, doubleClick: "reset+autosize" });
}

// ── 차트 PNG 클립보드 복사 (비보안 컨텍스트/실패 시 PNG 다운로드 폴백) ──────────
async function trimCopyPng(gd, name) {
  let url = null;
  try {
    url = await Plotly.toImage(gd, { format: "png", width: 1200, height: 520, scale: 2 });
    if (!window.isSecureContext || !navigator.clipboard || !navigator.clipboard.write
        || typeof ClipboardItem === "undefined") throw new Error("clipboard unavailable");
    const blob = await (await fetch(url)).blob();
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    showToast("차트 PNG 를 클립보드에 복사했습니다");
  } catch (e) {
    if (!url) { showToast("PNG 생성 실패: " + e.message); return; }
    const a = document.createElement("a");
    a.href = url;
    a.download = `trim_${name || "chart"}.png`;
    document.body.appendChild(a); a.click(); a.remove();
    showToast("클립보드 복사 불가 — PNG 파일로 다운로드했습니다");
  }
}

// ── 화면 ③ 분석 리포트: 필터 6종(전체/complete/P2없음/cpk warn/shift/검색) + 통계 표 ──
function trimApplyFilter(p) {
  let out = (p.groups || []).slice();
  const f = trimState.filter;
  if (f === "complete") out = out.filter(g => g.flags.complete_base_target);
  else if (f === "noverify") out = out.filter(g => !g.flags.has_verify);
  else if (f === "cpk") out = out.filter(g => g.cpk_warn);
  else if (f === "shift") out = out.filter(g => g.shift && g.shift.is_shift);
  const q = (trimState.search || "").trim().toLowerCase();
  if (q) out = out.filter(g => g.id.toLowerCase().includes(q) ||
    (g.members || []).some(m => m.toLowerCase().includes(q)));
  return out;
}

function renderTrimReport(body, p) {
  const groups = trimApplyFilter(p);
  const segs = [
    ["all", "전체"],
    ["complete", `${p.base}+${p.target} complete`],
    ["noverify", "P2 없음"],
    ["cpk", "cpk warn"],
    ["shift", "initial shift"],
  ].map(([k, label]) =>
    `<button class="distseg${trimState.filter === k ? " active" : ""}" data-tfilter="${k}">${esc(label)}</button>`).join("");
  const head = p.phases.map(ph => `<th style="color:${TRIM.COLORS[ph]}">${esc(ph)}</th>`).join("");
  const rows = groups.map(g => {
    const slotCells = p.phases.map(ph => {
      const item = g.slots[ph];
      const st = g.stats && g.stats[ph];
      const cpk = st && st.cpk != null
        ? `<div class="trim-cell-cpk${st.cpk < p.constants.cpk_threshold ? " warn" : ""}">cpk ${st.cpk}</div>` : "";
      const nSub = st && st.n ? `<div class="trim-cell-sub">n ${st.n} · avg ${st.average != null ? st.average : "-"}</div>` : "";
      return `<td>${item ? `<div class="trim-cell-item" title="${esc(item)}">${esc(item)}</div>${nSub}${cpk}` : "-"}</td>`;
    }).join("");
    const sh = g.shift || {};
    const shiftCell = sh.eligible
      ? `${trimShiftBadge(p, g)}<div class="trim-cell-sub">p20 ${sh.p20} · p80 ${sh.p80} · ${esc(p.target)} 평균 ${sh.target_mean}</div>`
      : `<span class="trim-cell-sub">${sh.reason === "no_target_mean" ? `${esc(p.target)} 없음`
          : sh.reason === "base_n_lt_min" ? `n&lt;${p.constants.shift_min_n}` : "-"}</span>`;
    return `<tr class="trim-row${g.cpk_warn ? " cpk-warn" : ""}" data-group="${esc(g.id)}" title="클릭하면 산포 차트로 이동">
      <td class="trim-cell-group">${esc(g.id)}${g.manual ? ` <span class="trim-badge manual">수동</span>` : ""}</td>
      ${slotCells}
      <td>${g.cpk_warn ? `<span class="trim-badge warn">warn</span>` : "-"}</td>
      <td>${shiftCell}</td>
    </tr>`;
  }).join("");
  body.innerHTML = `
    <div class="dist-toolbar">
      <div class="distseg-group">${segs}</div>
      <input id="trimSearch" class="dist-search" type="text" autocomplete="off"
        placeholder="항목명/그룹 검색" value="${esc(trimState.search)}">
    </div>
    ${groups.length ? `<div class="trim-table-wrap"><table class="trim-table trim-report-table">
      <thead><tr><th>그룹</th>${head}<th>CPK</th><th>Initial shift</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`
      : `<div class="placeholder">해당 조건의 그룹이 없습니다</div>`}`;
  const cnt = document.getElementById("trimCount");
  if (cnt) cnt.textContent = `그룹 ${groups.length}/${(p.groups || []).length}개`;

  body.querySelectorAll("[data-tfilter]").forEach(b => b.addEventListener("click", () => {
    trimState.filter = b.dataset.tfilter;
    renderTrimReport(body, p);
  }));
  const search = body.querySelector("#trimSearch");
  let timer = null;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      trimState.search = search.value;
      const pos = search.selectionStart;
      renderTrimReport(body, p);
      const s2 = body.querySelector("#trimSearch");
      if (s2) { s2.focus(); try { s2.setSelectionRange(pos, pos); } catch (e) {} }
    }, 250);
  });
  body.querySelectorAll(".trim-row").forEach(tr => tr.addEventListener("click", e => {
    if (e.target.closest("button, input")) return;
    trimState.view = "scatter";
    renderTrimView();
    const card = document.querySelector(`.trim-gcard[data-group="${CSS.escape(tr.dataset.group)}"]`);
    if (card) card.scrollIntoView({ block: "center" });
  }));
}

// ── Excel 내보내기 (vendored exceljs, 첫 클릭 시 동적 로드 — 서버 무변경) ────────
let _exceljsPromise = null;
function loadExcelJS() {
  if (window.ExcelJS) return Promise.resolve(window.ExcelJS);
  if (_exceljsPromise) return _exceljsPromise;
  _exceljsPromise = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "/pe/report/vendor/exceljs.min.js";
    s.onload = () => window.ExcelJS ? resolve(window.ExcelJS)
      : reject(new Error("ExcelJS 로드 실패"));
    s.onerror = () => { _exceljsPromise = null; reject(new Error("exceljs.min.js 로드 실패")); };
    document.head.appendChild(s);
  });
  return _exceljsPromise;
}

async function exportTrimExcel() {
  const p = trimPayload();
  if (!p) { showToast("Trim 데이터가 아직 로드되지 않았습니다"); return; }
  const btn = document.getElementById("trimExcelBtn");
  if (btn) btn.disabled = true;
  try {
    showToast("Excel 생성 준비 중…");
    const ExcelJS = await loadExcelJS();
    const wb = new ExcelJS.Workbook();

    const ws1 = wb.addWorksheet("항목 매칭");
    ws1.addRow(["원본명", "정규화", "토큰", "Phase", "Stem", "그룹", "슬롯", "수동", "제외"]);
    ws1.getRow(1).font = { bold: true };
    (p.items || []).forEach(i => ws1.addRow([
      i.name, i.normalized, (i.tokens || []).join("_"), i.phase || "",
      i.stem || "", i.group || "", i.slot || (i.group ? "MEMBER" : ""),
      i.override ? "O" : "", i.excluded ? "O" : "",
    ]));
    ws1.columns.forEach(c => { c.width = 24; });

    // 분석 리포트 시트 — 현재 ③ 필터가 적용된 그룹만
    const groups = trimApplyFilter(p);
    const ws2 = wb.addWorksheet("분석 리포트");
    const statCols = ["n", "average", "stdev", "cpk"];
    const header = ["그룹"];
    p.phases.forEach(ph => { header.push(`${ph} item`); statCols.forEach(c => header.push(`${ph} ${c}`)); });
    header.push("cpk warn", "shift 판정", `${p.target} %ile`, "p20", "p80");
    ws2.addRow(header);
    ws2.getRow(1).font = { bold: true };
    groups.forEach(g => {
      const row = [g.id];
      p.phases.forEach(ph => {
        const st = (g.stats && g.stats[ph]) || {};
        row.push(g.slots[ph] || "");
        statCols.forEach(c => row.push(st[c] != null ? st[c] : ""));
      });
      const sh = g.shift || {};
      row.push(g.cpk_warn ? "O" : "",
        sh.eligible ? (sh.is_shift ? "SHIFT" : "OK") : (sh.reason || ""),
        sh.eligible ? Math.round(20 + (sh.position || 0) * 60) : "",
        sh.p20 != null ? sh.p20 : "", sh.p80 != null ? sh.p80 : "");
      ws2.addRow(row);
    });
    ws2.columns.forEach(c => { c.width = 18; });

    // 차트 시트 — 필터 통과 그룹의 차트 PNG (미로드 차트는 fetch 후 오프스크린 렌더)
    const ws3 = wb.addWorksheet("차트");
    const off = document.createElement("div");
    off.style.cssText = "position:fixed;left:-10000px;top:0;width:1100px;height:480px";
    document.body.appendChild(off);
    let rowAnchor = 0;
    for (let i = 0; i < groups.length; i++) {
      const g = groups[i];
      showToast(`차트 이미지 생성 중… (${i + 1}/${groups.length})`, 1200);
      try {
        const chart = await trimFetchChart(g.id);
        off.innerHTML = "";
        const div = document.createElement("div");
        div.style.cssText = "width:1100px;height:480px";
        off.appendChild(div);
        drawTrimChart(div, chart, p);
        const url = await Plotly.toImage(div, { format: "png", width: 1100, height: 480, scale: 2 });
        Plotly.purge(div);
        const imgId = wb.addImage({ base64: url, extension: "png" });
        ws3.getCell(rowAnchor + 1, 1).value = g.id;
        ws3.getCell(rowAnchor + 1, 1).font = { bold: true };
        ws3.addImage(imgId, { tl: { col: 0, row: rowAnchor + 1 }, ext: { width: 1100, height: 480 } });
        rowAnchor += 27;   // 이미지 480px ≈ 24행 + 제목/여백
      } catch (e) {
        ws3.getCell(rowAnchor + 1, 1).value = `${g.id} — 차트 생성 실패 (${e.message})`;
        rowAnchor += 2;
      }
    }
    off.remove();

    const buf = await wb.xlsx.writeBuffer();
    const blob = new Blob([buf],
      { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    const meta = (DATA && DATA.session) || {};
    a.download = `trim_analysis_${meta.lot_id || SESSION_ID}.xlsx`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    showToast("Excel 다운로드 완료");
  } catch (e) {
    showToast("Excel 생성 실패: " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}


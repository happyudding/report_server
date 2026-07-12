// ── 차트 주석 (chart_note) — 그래프 위 동그라미/사각형/선/텍스트 + 코멘트 ─────────
// 엑셀에서 그래프에 도형·코멘트를 얹던 워크플로의 대체. Plotly 내장 draw 기능
// (dragmode drawcircle/drawrect/drawline + layout.newshape, v3.5 내장)을 쓴다.
// 저장은 세션 편집 DB(kind=chart_note, item_key=chart_key) — POST .../web_report/chart_notes.
// chart_key 규약: "cdf:<subject>" / "hist:<subject>" (서버 service.py 검증 규칙과 일치).
// 표시는 전원(읽기 전용 포함), 편집·저장은 편집 권한자(MODE==="edit")만.
//
// item_detail.js 가 렌더 직후 chartNotesApply(kind, subject, gd) 를 불러 저장/미저장
// 주석을 오버레이하고, renderItemDetail 이 chartNotesBar(data) 로 툴바를 그린다.
// LSL/USL 스펙 점선도 layout.shapes 라서 렌더 시점의 개수(base)를 기억해 두고
// 사용자 도형은 base 뒤에만 붙인다 — 저장 시 slice(base) 로 분리.

const CNOTE_STYLE = { line: { color: "#DC2626", width: 2 }, fillcolor: "rgba(220,38,38,.06)" };
const CNOTE_TEXT_FONT = { size: 12, color: "#DC2626" };

let _cnEditing = false;   // 주석 편집 모드 (툴바 토글)
let _cnTool = null;       // "circle" | "rect" | "line" | "text" | null
let _cnCharts = {};       // chartKey -> { gd, base, baseAnnos } (현재 상세뷰의 차트)
let _cnPending = {};      // chartKey -> {shapes, texts, comment} — 미저장 편집 상태
let _cnDirty = new Set(); // 미저장 chartKey 집합

function cnSavedFor(key) {
  const all = (DATA && DATA.chart_notes) || {};
  return all[key] || null;
}
function cnStateFor(key) {
  if (_cnPending[key]) return _cnPending[key];
  const saved = cnSavedFor(key);
  return { shapes: (saved && saved.shapes) || [], texts: (saved && saved.texts) || [],
           comment: (saved && saved.comment) || "" };
}
function cnHasAny(key) {
  const st = cnStateFor(key);
  return !!(st.shapes.length || st.texts.length || st.comment);
}

// ── 렌더 훅: 저장/미저장 주석을 차트에 오버레이 (item_detail 렌더 직후 호출) ──────
function chartNotesApply(kind, subject, gd) {
  if (!gd || !isWebReportSession()) return;
  const key = `${kind}:${subject}`;
  const base = (gd.layout.shapes || []).length;
  const baseAnnos = (gd.layout.annotations || []).length;
  _cnCharts[key] = { gd, base, baseAnnos };
  const st = cnStateFor(key);
  const editable = _cnEditing && MODE === "edit";
  const relayout = {};
  if (st.shapes.length || editable) {
    relayout.shapes = (gd.layout.shapes || []).slice(0, base)
      .concat(st.shapes.map(s => Object.assign({}, s, { editable })));
  }
  if (st.texts.length) {
    relayout.annotations = (gd.layout.annotations || []).slice(0, baseAnnos)
      .concat(st.texts.map(t => Object.assign({ showarrow: true, arrowhead: 2, ax: 0, ay: -40,
        font: CNOTE_TEXT_FONT }, t, { captureevents: true })));
  }
  if (editable) {
    relayout.newshape = CNOTE_STYLE;
    if (_cnTool && _cnTool !== "text") relayout.dragmode = "draw" + _cnTool;
  }
  if (Object.keys(relayout).length) { try { Plotly.relayout(gd, relayout); } catch (e) {} }
  cnBindChart(key, gd);
}

// 도형 드래그/리사이즈/신규 그리기 → pending 동기화 + dirty 마킹.
function cnBindChart(key, gd) {
  if (gd._cnBoundKey === key) return;
  gd._cnBoundKey = key;
  gd.on("plotly_relayout", ev => {
    if (!_cnEditing || !ev) return;
    const keys = Object.keys(ev);
    if (!keys.some(k => k === "shapes" || k.startsWith("shapes["))) return;
    cnSyncFromChart(key);
    cnMarkDirty(key);
  });
  // 텍스트 주석 클릭(편집 모드) → 삭제 확인. captureevents:true 가 있어야 발화.
  gd.on("plotly_clickannotation", ev => {
    if (!_cnEditing) return;
    const info = _cnCharts[key];
    if (!info || ev.index < info.baseAnnos) return;   // 스펙 라벨 등 base 주석은 보호
    if (!confirm("이 텍스트 Comment를 삭제할까요?")) return;
    const st = cnStateFor(key);
    st.texts = st.texts.slice();
    st.texts.splice(ev.index - info.baseAnnos, 1);
    _cnPending[key] = st;
    cnMarkDirty(key);
    cnReapply(key);
  });
  // 텍스트 도구: 차트 여백 클릭 좌표(px)를 데이터 좌표로 변환해 주석 추가.
  gd.addEventListener("click", ev => {
    if (!_cnEditing || _cnTool !== "text") return;
    const pt = cnPixelToData(gd, ev);
    if (!pt) return;
    const text = prompt("Comment 텍스트를 입력하세요 (최대 300자):", "");
    if (!text || !text.trim()) return;
    const st = cnStateFor(key);
    st.texts = st.texts.concat([{ x: pt.x, y: pt.y, xref: "x", yref: "y",
      text: text.trim().slice(0, 300) }]);
    _cnPending[key] = st;
    cnMarkDirty(key);
    cnReapply(key);
  });
}

// px → 데이터 좌표 (plotly 내부 축 객체 사용 — 실패 시 null).
function cnPixelToData(gd, ev) {
  try {
    const fl = gd._fullLayout;
    const rect = gd.getBoundingClientRect();
    const x = fl.xaxis.p2d(ev.clientX - rect.left - fl.xaxis._offset);
    const y = fl.yaxis.p2d(ev.clientY - rect.top - fl.yaxis._offset);
    if (!isFinite(x) || !isFinite(y)) return null;
    return { x, y };
  } catch (e) { return null; }
}

// 차트 layout 에서 사용자 도형(base 이후)을 pending 으로 회수.
function cnSyncFromChart(key) {
  const info = _cnCharts[key];
  if (!info || !info.gd.layout) return;
  const st = cnStateFor(key);
  st.shapes = (info.gd.layout.shapes || []).slice(info.base).map(cnStripShape);
  _cnPending[key] = st;
}

// 저장 payload 로 보낼 필드만 남긴다 (서버 _sanitize_chart_note 허용 키의 클라 대응).
function cnStripShape(s) {
  const out = { type: s.type, xref: s.xref, yref: s.yref };
  ["x0", "x1", "y0", "y1", "path", "fillcolor", "opacity"].forEach(k => {
    if (s[k] !== undefined) out[k] = s[k];
  });
  if (s.line) out.line = { color: s.line.color, width: s.line.width, dash: s.line.dash };
  return out;
}

function cnMarkDirty(key) {
  _cnDirty.add(key);
  cnUpdateBarState();
}

// pending/저장 상태를 해당 차트에 다시 그림 (base 유지 + 사용자분 교체).
function cnReapply(key) {
  const info = _cnCharts[key];
  if (!info) return;
  const st = cnStateFor(key);
  const editable = _cnEditing && MODE === "edit";
  try {
    Plotly.relayout(info.gd, {
      shapes: (info.gd.layout.shapes || []).slice(0, info.base)
        .concat(st.shapes.map(s => Object.assign({}, s, { editable }))),
      annotations: (info.gd.layout.annotations || []).slice(0, info.baseAnnos)
        .concat(st.texts.map(t => Object.assign({ showarrow: true, arrowhead: 2, ax: 0, ay: -40,
          font: CNOTE_TEXT_FONT }, t, { captureevents: true }))),
    });
  } catch (e) {}
}

// ── 툴바 (item detail 의 #chartNoteBar) ───────────────────────────────────────
function chartNotesBar(data) {
  const bar = document.getElementById("chartNoteBar");
  if (!bar) return;
  if (!isWebReportSession()) { bar.innerHTML = ""; return; }
  const subject = data.subject;
  const cdfKey = `cdf:${subject}`, histKey = `hist:${subject}`;
  const canEdit = MODE === "edit";

  if (!canEdit) {
    // 읽기 전용: 주석 코멘트가 있으면 표시만.
    const parts = [cdfKey, histKey].map(k => {
      const saved = cnSavedFor(k);
      if (!saved || !saved.comment) return "";
      const who = saved.updated_by ? ` — ${saved.updated_by}` : "";
      return `<span class="cnote-view-comment">📝 ${esc(saved.comment)}${esc(who)}</span>`;
    }).filter(Boolean);
    bar.innerHTML = parts.length ? `<div class="cnote-bar view">${parts.join("")}</div>` : "";
    return;
  }

  const tool = (t, label, title) =>
    `<button type="button" class="btn-sm cnote-tool${_cnTool === t ? " active" : ""}" ` +
    `data-cnote-tool="${t}" title="${esc(title)}">${label}</button>`;
  const comment = cnStateFor(cdfKey).comment;
  bar.innerHTML = `<div class="cnote-bar">
    <button type="button" class="btn-sm cnote-toggle${_cnEditing ? " active" : ""}" id="cnoteToggle">
      ${_cnEditing ? "Comment 편집 종료" : "✏️ Comment 편집"}</button>
    ${_cnEditing ? `
      ${tool("circle", "○", "동그라미 그리기 (드래그)")}
      ${tool("rect", "□", "사각형 그리기 (드래그)")}
      ${tool("line", "╱", "선 그리기 (드래그)")}
      ${tool("text", "T", "텍스트 Comment (차트 클릭)")}
      <button type="button" class="btn-sm" id="cnoteUndo" title="마지막 도형 취소">↶</button>
      <button type="button" class="btn-sm" id="cnoteClear" title="이 항목의 Comment 전부 삭제">전체 지우기</button>
      <input type="text" id="cnoteComment" class="cnote-comment" placeholder="Comment (선택)"
        value="${esc(comment)}" maxlength="2000">
      <button type="button" class="btn-sm cnote-save${_cnDirty.size ? " dirty" : ""}" id="cnoteSave">저장</button>
      <span class="cnote-hint">도형은 CDF/히스토그램 어느 쪽에나 드래그로 그립니다 · 텍스트 Comment 클릭 = 삭제</span>
    ` : (cnHasAny(cdfKey) || cnHasAny(histKey)
        ? `<span class="cnote-hint">저장된 Comment가 표시되어 있습니다</span>` : "")}
    <button type="button" class="btn-sm" id="cnoteToNote" title="현재 차트(Comment 포함)를 Note 탭 시트에 이미지로 붙여넣기">📋 Note에 붙여넣기</button>
  </div>`;
  cnBindBar(subject);
}

function cnUpdateBarState() {
  const save = document.getElementById("cnoteSave");
  if (save) save.classList.toggle("dirty", _cnDirty.size > 0);
}

// ── 차트 하단 Comment 표시 (item_detail 의 #cdfCommentView / #histCommentView) ──
// comment 는 아이템당 1개(cdf 키). 편집 중이면 pending, 아니면 저장값을 두 차트 하단에 동일 표시.
function cnRenderChartComments(subject) {
  const cdf = document.getElementById("cdfCommentView");
  const hist = document.getElementById("histCommentView");
  if (!cdf && !hist) return;
  const st = cnStateFor(`cdf:${subject}`);
  const saved = cnSavedFor(`cdf:${subject}`);
  const text = String(st.comment || "").trim();
  let html = "";
  if (text) {
    const who = (saved && saved.updated_by) ? ` — ${esc(saved.updated_by)}` : "";
    html = `<span class="cnote-view-comment">📝 ${esc(text)}${who}</span>`;
  }
  if (cdf) cdf.innerHTML = html;
  if (hist) hist.innerHTML = html;
}

function cnSetTool(t) {
  _cnTool = (t === _cnTool) ? null : t;
  document.querySelectorAll(".cnote-tool").forEach(b =>
    b.classList.toggle("active", b.dataset.cnoteTool === _cnTool));
  const mode = (_cnTool && _cnTool !== "text") ? ("draw" + _cnTool) : false;
  Object.keys(_cnCharts).forEach(k => {
    const gd = _cnCharts[k].gd;
    if (!gd || !gd.layout) return;
    try { Plotly.relayout(gd, { dragmode: mode || "zoom", newshape: CNOTE_STYLE }); } catch (e) {}
  });
}

function cnBindBar(subject) {
  const bar = document.getElementById("chartNoteBar");
  if (!bar) return;
  const toggle = document.getElementById("cnoteToggle");
  if (toggle) toggle.onclick = () => {
    _cnEditing = !_cnEditing;
    if (!_cnEditing) _cnTool = null;
    // 편집 모드 진입/종료 시 editable 플래그 반영을 위해 현재 차트 전부 재적용.
    Object.keys(_cnCharts).forEach(k => cnReapply(k));
    if (!_cnEditing) cnSetTool(null);
    chartNotesBar(_itemDetailData || { subject });
    if (_cnEditing) cnSetTool(_cnTool);
  };
  bar.querySelectorAll("[data-cnote-tool]").forEach(b =>
    b.onclick = () => cnSetTool(b.dataset.cnoteTool));
  const undo = document.getElementById("cnoteUndo");
  if (undo) undo.onclick = () => {
    // 가장 마지막에 그린 도형 하나 취소 — 두 차트 중 pending 도형이 있는 쪽 우선.
    for (const key of [`cdf:${subject}`, `hist:${subject}`]) {
      cnSyncFromChart(key);
      const st = cnStateFor(key);
      if (st.shapes.length) {
        st.shapes = st.shapes.slice(0, -1);
        _cnPending[key] = st;
        cnMarkDirty(key);
        cnReapply(key);
        return;
      }
    }
    showToast("취소할 도형이 없습니다.");
  };
  const clear = document.getElementById("cnoteClear");
  if (clear) clear.onclick = () => {
    if (!confirm("이 항목의 Comment(도형·텍스트)를 전부 삭제할까요?")) return;
    [`cdf:${subject}`, `hist:${subject}`].forEach(key => {
      _cnPending[key] = { shapes: [], texts: [], comment: "" };
      cnMarkDirty(key);
      cnReapply(key);
    });
    const inp = document.getElementById("cnoteComment");
    if (inp) inp.value = "";
    cnRenderChartComments(subject);   // 하단 미리보기도 비움
  };
  const cmt = document.getElementById("cnoteComment");
  if (cmt) cmt.oninput = () => {
    const key = `cdf:${subject}`;
    const st = cnStateFor(key);
    st.comment = cmt.value;
    _cnPending[key] = st;
    cnMarkDirty(key);
    cnRenderChartComments(subject);   // 타이핑 중 하단 미리보기 반영
  };
  const save = document.getElementById("cnoteSave");
  if (save) save.onclick = () => cnSave();
  const toNote = document.getElementById("cnoteToNote");
  if (toNote) toNote.onclick = () => cnPasteToNote(subject);
}

// ── 저장: dirty 차트 전부 한 번에 POST (rev 1회 증가) ─────────────────────────
async function cnSave() {
  if (!_cnDirty.size) { showToast("변경된 Comment가 없습니다."); return; }
  // 저장 직전 차트 상태에서 최신 도형 회수 (드래그 직후 미동기 방지).
  _cnDirty.forEach(key => { if (_cnCharts[key]) cnSyncFromChart(key); });
  const ops = [...(_cnDirty)].map(key => {
    const st = cnStateFor(key);
    const empty = !st.shapes.length && !st.texts.length && !String(st.comment || "").trim();
    return { key, value: empty ? null : { shapes: st.shapes, texts: st.texts, comment: st.comment } };
  });
  const btn = document.getElementById("cnoteSave");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/chart_notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ ops }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    if (DATA) DATA.chart_notes = j.chart_notes || {};
    _cnDirty.clear();
    _cnPending = {};
    cnUpdateBarState();
    if (_itemDetailData) cnRenderChartComments(_itemDetailData.subject);   // 저장값을 차트 하단에 반영
    showToast("Comment를 저장했습니다.");
  } catch (e) {
    showToast("Comment 저장 실패: " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 미저장 주석이 있으면 이탈 경고 (edit_mode 의 comment dirty 와 별개 채널).
window.addEventListener("beforeunload", e => {
  if (_cnDirty.size) { e.preventDefault(); e.returnValue = ""; }
});

// ── Note 붙여넣기: 현재 CDF 차트(주석 오버레이 포함) → PNG 업로드 → Note 시트 삽입 ──
async function cnPasteToNote(subject) {
  const key = `cdf:${subject}`;
  const info = _cnCharts[key];
  if (!info || !info.gd) { showToast("차트가 아직 준비되지 않았습니다."); return; }
  if (MODE !== "edit") { showToast("편집 권한이 있어야 Note 에 붙여넣을 수 있습니다."); return; }
  try {
    showToast("차트 이미지를 생성하는 중…");
    const url = await Plotly.toImage(info.gd, { format: "png", width: 900, height: 420, scale: 2 });
    const blob = await (await fetch(url)).blob();
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/note_image`, {
      method: "POST",
      headers: { "Content-Type": "image/png", "X-CSRF-Token": csrfToken() },
      body: blob,
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    noteQueueImage(j.url, subject);   // note.js — Note 탭 열고 삽입 (미초기화면 큐잉)
  } catch (e) {
    showToast("Note 붙여넣기 실패: " + e.message);
  }
}

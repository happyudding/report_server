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
let _cnTool = null;       // "circle" | "rect" | "line" | "text" | "arrow" | null
let _cnCharts = {};       // chartKey -> { gd, base, baseAnnos } (현재 상세뷰의 차트)
let _cnPending = {};      // chartKey -> {shapes, texts, comment} — 미저장 편집 상태
let _cnDirty = new Set(); // 미저장 chartKey 집합
let _cnGen = 0;           // 편집 세대 — 저장 요청 중 추가된 편집을 지우지 않기 위한 카운터

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

// 텍스트 주석(화살표) 드래그 허용 여부를 차트에 반영한다.
// 도형(shape)은 개별 속성 editable 로 켜지지만, 주석은 Plotly config 의 edits 로만 켜진다
// (번들 v3.5: 화살표 있는 주석의 꼬리=annotationTail, 화살표 머리=annotationPosition).
// config 는 newPlot 시점에 고정이라 런타임 변경 경로가 없어 _context.edits 를 직접 갱신한다 —
// 주석을 다시 그릴 때(아래 relayout) 이 값을 읽으므로 편집 모드 토글에 바로 따라온다.
function cnSetEditContext(gd, editable) {
  try {
    const ed = gd && gd._context && gd._context.edits;
    if (!ed) return;
    ed.annotationTail = editable;      // 텍스트 상자(화살표 꼬리) 이동
    ed.annotationPosition = editable;  // 화살표 머리(가리키는 지점) 이동
  } catch (e) { /* 내부 구조가 바뀌면 드래그만 비활성 — 표시/저장은 그대로 */ }
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
  cnSetEditContext(gd, editable);
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
    // text/arrow 는 클릭 배치 도구라 draw dragmode 가 없다 — zoom 유지.
    if (_cnTool && _cnTool !== "text" && _cnTool !== "arrow") relayout.dragmode = "draw" + _cnTool;
  }
  if (Object.keys(relayout).length) { try { Plotly.relayout(gd, relayout); } catch (e) {} }
  cnBindChart(key, gd);
}

// 도형 드래그/리사이즈/신규 그리기 → pending 동기화 + dirty 마킹.
function cnBindChart(key, gd) {
  // Plotly.purge 는 gd.on/_ev 를 통째로 지운다(CDF 는 축 변경·칩 편집마다 purge→newPlot).
  // key 만으로 판정하면 purge 뒤에도 "이미 바인딩됨"으로 보여 드래그가 조용히 죽으므로,
  // newPlot 이 새로 만든 _ev 객체의 동일성까지 함께 본다.
  if (gd._cnBoundKey !== key || gd._cnBoundEv !== gd._ev) {
    gd._cnBoundKey = key;
    gd._cnBoundEv = gd._ev;
    cnBindPlotEvents(key, gd);
  }
  // DOM click 은 purge 와 무관하게 살아남으므로 1회만 — 재등록하면 prompt 가 중복된다.
  if (!gd._cnClickBound) {
    gd._cnClickBound = true;
    cnBindTextClick(gd);
  }
}

// 현재 바인딩된 chartKey — purge 재바인딩 후에도 최신 key 로 동작하도록 DOM click 은
// 이 값을 통해 간접 참조한다(핸들러를 다시 달지 않기 위함).
function cnBindPlotEvents(key, gd) {
  gd.on("plotly_relayout", ev => {
    if (!_cnEditing || !ev) return;
    const keys = Object.keys(ev);
    // 도형 드래그/리사이즈뿐 아니라 주석(화살표) 이동도 받는다 — 주석 드래그는
    // "annotations[0].ax" 같은 키로 온다.
    if (!keys.some(k => k === "shapes" || k.startsWith("shapes[")
                     || k === "annotations" || k.startsWith("annotations["))) return;
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
}

// 텍스트/화살표 도구: 차트 클릭 좌표(px)를 데이터 좌표로 변환해 주석 추가.
// 화살표는 텍스트 없는 annotation(showarrow) — 가리킬 지점을 클릭하면 붉은 화살표가 붙고,
// 편집 모드에서 머리(가리키는 지점)·꼬리를 드래그로 옮길 수 있다(텍스트 주석과 동일 채널로
// 세션 저장). 삭제는 ↶(마지막 취소) 또는 전체 지우기.
// 1회만 등록되므로 key 를 클로저로 잡지 않고 현재 바인딩된 gd._cnBoundKey 를 그때그때 읽는다.
function cnBindTextClick(gd) {
  gd.addEventListener("click", ev => {
    if (!_cnEditing || (_cnTool !== "text" && _cnTool !== "arrow")) return;
    const key = gd._cnBoundKey;
    if (!key) return;
    const pt = cnPixelToData(gd, ev);
    if (!pt) return;
    const anno = { x: pt.x, y: pt.y, xref: "x", yref: "y" };
    if (_cnTool === "text") {
      const text = prompt("Comment 텍스트를 입력하세요 (최대 300자):", "");
      if (!text || !text.trim()) return;
      anno.text = text.trim().slice(0, 300);
    } else {
      anno.text = "";
      anno.showarrow = true; anno.arrowhead = 3;
      anno.ax = 28; anno.ay = -34;   // 꼬리 기본 위치(우상단) — 드래그로 조정 가능
      anno.arrowcolor = CNOTE_STYLE.line.color; anno.arrowwidth = 2;
    }
    const st = cnStateFor(key);
    st.texts = st.texts.concat([anno]);
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

// 차트 layout 에서 사용자 도형·텍스트 주석(base 이후)을 pending 으로 회수.
// 텍스트도 함께 회수해야 드래그로 옮긴 화살표 위치(x/y·ax/ay)가 저장에 반영된다.
function cnSyncFromChart(key) {
  const info = _cnCharts[key];
  if (!info || !info.gd.layout) return;
  const st = cnStateFor(key);
  st.shapes = (info.gd.layout.shapes || []).slice(info.base).map(cnStripShape);
  st.texts = (info.gd.layout.annotations || []).slice(info.baseAnnos).map(cnStripText);
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

// 텍스트 주석의 저장 대상 필드만 남긴다 (서버 _TEXT_KEYS 대응).
// x/y = 화살표가 가리키는 지점, ax/ay = 텍스트 상자까지의 꼬리 offset — 드래그로 바뀌는 값들.
function cnStripText(t) {
  const out = { x: t.x, y: t.y, xref: t.xref, yref: t.yref, text: t.text };
  ["showarrow", "arrowhead", "ax", "ay", "bgcolor", "bordercolor",
   "arrowcolor", "arrowwidth"].forEach(k => {
    if (t[k] !== undefined) out[k] = t[k];
  });
  if (t.font) out.font = { size: t.font.size, color: t.font.color };
  return out;
}

function cnMarkDirty(key) {
  _cnDirty.add(key);
  _cnGen++;          // 저장 요청 비행 중 들어온 편집을 cnFlush 가 알아채는 근거
  cnUpdateBarState();
}

// pending/저장 상태를 해당 차트에 다시 그림 (base 유지 + 사용자분 교체).
function cnReapply(key) {
  const info = _cnCharts[key];
  if (!info) return;
  const st = cnStateFor(key);
  const editable = _cnEditing && MODE === "edit";
  cnSetEditContext(info.gd, editable);
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
      ${tool("arrow", "↗", "화살표 포인팅 (가리킬 지점을 차트에서 클릭)")}
      ${tool("text", "T", "텍스트 Comment (차트 클릭)")}
      <button type="button" class="btn-sm" id="cnoteUndo" title="마지막 도형 취소">↶</button>
      <button type="button" class="btn-sm" id="cnoteClear" title="이 항목의 Comment 전부 삭제">전체 지우기</button>
      <input type="text" id="cnoteComment" class="cnote-comment" placeholder="Comment (선택)"
        value="${esc(comment)}" maxlength="2000">
      <button type="button" class="btn-sm cnote-save${_cnDirty.size ? " dirty" : ""}" id="cnoteSave">저장</button>
      <span class="cnote-hint">도형은 CDF/히스토그램 어느 쪽에나 드래그로 그립니다 · 화살표는 가리킬 지점 클릭(삭제는 ↶) · 텍스트 Comment 클릭 = 삭제</span>
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
  const mode = (_cnTool && _cnTool !== "text" && _cnTool !== "arrow") ? ("draw" + _cnTool) : false;
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
    // 도형이 없으면 텍스트/화살표 주석을 취소한다(화살표는 클릭 삭제가 어려워 이 경로가 삭제 수단).
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
    for (const key of [`cdf:${subject}`, `hist:${subject}`]) {
      const st = cnStateFor(key);
      if (st.texts.length) {
        st.texts = st.texts.slice(0, -1);
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
    // 먼저 차트에서 현재 도형·주석 위치를 회수한다. 이걸 빼먹으면 텍스트만 바꿔 저장할 때
    // 저장돼 있던 옛 좌표가 그대로 올라가 방금 옮긴 위치가 되돌아간다.
    cnSyncFromChart(key);
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
// cnFlush — 실제 저장 본체. 변경 없으면 요청을 보내지 않고, 실패 시 throw 만 한다
// (사용자 알림은 호출부 몫). 수동 저장 버튼(cnSave) 외에 edit_mode.js autoSave 의
// visibilitychange/beforeunload keepalive 저장과 item_detail 항목 이동 flush 가 재사용.
async function cnFlush(opts) {
  if (!_cnDirty.size) return { ok: true, updated: 0 };
  // 저장 직전 차트 상태에서 최신 도형 회수 (드래그 직후 미동기 방지).
  _cnDirty.forEach(key => { if (_cnCharts[key]) cnSyncFromChart(key); });
  const gen = _cnGen;   // 이 요청이 담아 보내는 편집 세대
  const ops = [...(_cnDirty)].map(key => {
    const st = cnStateFor(key);
    const empty = !st.shapes.length && !st.texts.length && !String(st.comment || "").trim();
    return { key, value: empty ? null : { shapes: st.shapes, texts: st.texts, comment: st.comment } };
  });
  const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/chart_notes`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
    body: JSON.stringify({ ops }),
    keepalive: !!(opts && opts.keepalive),
  });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
  if (DATA) DATA.chart_notes = j.chart_notes || {};
  // 요청이 나간 뒤 사용자가 더 편집했다면(세대 변화) dirty·pending 을 지우지 않는다 —
  // 지우면 그 편집이 저장된 것처럼 보이면서 다음 편집 전까지 서버에 반영되지 않는다.
  if (gen === _cnGen) {
    _cnDirty.clear();
    _cnPending = {};
  }
  cnUpdateBarState();
  if (_itemDetailData) cnRenderChartComments(_itemDetailData.subject);   // 저장값을 차트 하단에 반영
  return j;
}

async function cnSave() {
  if (!_cnDirty.size) { showToast("변경된 Comment가 없습니다."); return; }
  const btn = document.getElementById("cnoteSave");
  if (btn) btn.disabled = true;
  try {
    await cnFlush();
    showToast("Comment를 저장했습니다.");
  } catch (e) {
    showToast("Comment 저장 실패: " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 미저장 주석이 있으면 이탈 경고 (edit_mode 의 comment dirty 와 별개 채널).
window.addEventListener("beforeunload", e => {
  if (leaveGuardBypassed()) return;   // 이탈 확인 모달에서 확정
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

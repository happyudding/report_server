// ── Note 탭 — Luckysheet 시트 캔버스 (iframe 격리) ──────────────────────────────
// Luckysheet 2.0.6 은 풀윈도우 앱 설계라 우리 페이지(전역 *{box-sizing}·sticky header·페이지
// 스크롤·중앙정렬 여백·소수 dpr)에 직접 임베드하면 클릭 좌표·캔버스 배율이 어긋나 셀 밀림/
// 격자 떨림이 머신(dpr)마다 재발한다. 그래서 Luckysheet 는 깨끗한 iframe 문서
// (/pe/report/note_frame = note_frame.html)에서 풀페이지로 돌리고, 이 파일은 부모 셸로서
// 서버 I/O(fetch/POST)·UI(note-bar/저장/메타)·dirty 만 맡아 postMessage 로 프레임과 통신한다.
// 조회는 전원, 편집·저장은 편집 권한자(MODE==="edit")만 — allowEdit(프레임)로 강제.

const NOTE_FRAME_SRC = "/pe/report/note_frame";
const NOTE_ORIGIN = window.location.origin;   // same-origin iframe

let _noteReady = false;        // iframe 에 init 전송 완료(= luckysheet 생성됨)
let _noteDirty = false;        // 미저장 시트 편집
let _noteInitToken = 0;        // 재렌더 경합 가드
let _notePendingImgs = [];     // 준비 전 요청된 삽입 이미지 큐 [{url, caption}]
let _notePendingGoto = null;   // 준비 전 요청된 셀 이동 {sheet, sheetName, r, c}
let _noteCanEdit = false;      // 현재 렌더의 편집 가능 여부
let _noteFrameReady = false;   // iframe 이 note:ready 전송함
let _noteFetched = false;      // 저장 시트 fetch 완료
let _noteSavedSheets = null;   // fetch 로 받은 시트 배열(없으면 null → 프레임이 기본시트 생성)
let _noteInitSent = false;     // init 중복 전송 방지
let _noteReqSeq = 0;           // getSheets 요청 id
const _noteSaveWaiters = new Map();   // reqId → {resolve, reject}
// 낙관적 잠금 토큰 — 내가 읽은 시점의 서버 저장본. 저장 시 되돌려 보내 그 사이 남이
// 저장했는지 서버가 판정한다(불일치 → 409). 시트는 통째로 치환되므로 이 검사가 없으면
// 동시 편집 시 상대 Note 전체가 조용히 사라진다.
let _noteBaseToken = null;

// ── 탭 렌더 (edit_mode.js TAB_RENDERERS["note"] → 탭 첫 활성화 시) ─────────────
function renderNoteTab() {
  const panel = document.getElementById("panel-note");
  if (!panel) return;
  if (!isWebReportSession()) { emptyPanel(panel, "web_report 세션에서만 사용할 수 있습니다."); return; }
  if (_noteReady && _noteDirty) return;   // 미저장 편집 보존
  const canEdit = MODE === "edit";
  const token = ++_noteInitToken;
  _noteCanEdit = canEdit;
  _noteReady = false; _noteFrameReady = false; _noteFetched = false;
  _noteSavedSheets = null; _noteInitSent = false; _noteDirty = false;
  _noteBaseToken = null;
  panel.innerHTML = `
    <div class="note-bar" id="noteBar">
      ${canEdit ? `<button type="button" class="btn-sm note-save" id="noteSave">💾 Note 저장</button>` : ""}
      <button type="button" class="btn-sm" id="noteTagBtn" title="셀 앵커 태그 — IssueTable comment 의 #[태그] 로 링크됩니다">🔖 태그</button>
      <span class="note-meta" id="noteMeta"></span>
      <span class="cnote-hint">${canEdit
        ? "차트는 각 항목 상세의 [📋 Note에 붙여넣기]로 담고, 셀에는 텍스트·수식(=A1-B1)·엑셀 붙여넣기가 됩니다. 저장 버튼을 눌러야 서버에 반영됩니다."
        : "읽기 전용 — 편집 권한자가 정리한 Note 입니다."}</span>
      <div class="note-tag-panel" id="noteTagPanel" style="display:none;"></div>
    </div>
    <iframe id="noteFrame" src="${NOTE_FRAME_SRC}" title="Note 시트"></iframe>`;
  const saveBtn = document.getElementById("noteSave");
  if (saveBtn) saveBtn.onclick = () => noteSave();
  const tagBtn = document.getElementById("noteTagBtn");
  if (tagBtn) tagBtn.onclick = () => noteToggleTagPanel();

  // 저장 시트 fetch 와 iframe 로드는 병렬 — 둘 다 완료되면 noteMaybeInit 이 init 전송.
  fetch(`/pe/report/session/${SESSION_ID}/web_report/note`, { cache: "no-store" })
    .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(saved => {
      if (token !== _noteInitToken) return;   // 그 사이 재렌더됨
      _noteSavedSheets = (saved && saved.sheet && Array.isArray(saved.sheet.sheets) && saved.sheet.sheets.length)
        ? saved.sheet.sheets : null;
      _noteBaseToken = (saved && saved.base) || null;
      _noteFetched = true;
      noteRenderMeta(saved);
      noteMaybeInit(token);
    })
    .catch(e => {
      if (token !== _noteInitToken) return;
      // 저장된 Note 를 못 읽은 채 빈 시트로 열면 저장 시 기존 Note 를 빈 내용으로
      // 덮어쓴다 → 편집/저장을 차단하고 재시도만 허용 (_noteFetched=false 유지라
      // 늦게 도착한 note:ready 가 있어도 init 되지 않는다).
      panel.innerHTML = `<div class="placeholder" style="padding:24px;">Note 로드 실패: ${esc(e.message)}
        <button type="button" class="btn-sm" id="noteRetry" style="margin-left:8px;">다시 시도</button></div>`;
      const retry = document.getElementById("noteRetry");
      if (retry) retry.onclick = () => renderNoteTab();
      showToast("Note 로드 실패: " + e.message);
    });
}

// iframe ready + 시트 fetch 완료가 모두 갖춰지면 딱 1회 init 전송(= 프레임에서 luckysheet 생성).
function noteMaybeInit(token) {
  if (token !== _noteInitToken) return;
  if (_noteInitSent || !_noteFrameReady || !_noteFetched) return;
  const frame = document.getElementById("noteFrame");
  if (!frame || !frame.contentWindow) return;
  _noteInitSent = true;
  _noteReady = true;
  frame.contentWindow.postMessage(
    { type: "note:init", canEdit: _noteCanEdit, sheets: _noteSavedSheets }, NOTE_ORIGIN);
  noteFlushPending();
  noteFlushGoto();
}

// ── iframe → 부모 메시지 (모듈 로드 시 1회 등록) ──────────────────────────────
window.addEventListener("message", ev => {
  if (ev.origin !== NOTE_ORIGIN) return;
  const msg = ev.data || {};
  if (typeof msg.type !== "string" || !msg.type.startsWith("note:")) return;
  switch (msg.type) {
    case "note:ready":
      _noteFrameReady = true;
      noteMaybeInit(_noteInitToken);
      break;
    case "note:dirty":
      noteMarkDirty();
      break;
    case "note:sheets": {
      const w = _noteSaveWaiters.get(msg.reqId);
      if (w) { _noteSaveWaiters.delete(msg.reqId); w.resolve(msg.sheets); }
      break;
    }
    case "note:selection": {
      const w = _noteSaveWaiters.get(msg.reqId);
      if (w) { _noteSaveWaiters.delete(msg.reqId); w.resolve(msg.sel); }
      break;
    }
    case "note:error": {
      if (msg.reqId != null && _noteSaveWaiters.has(msg.reqId)) {
        const w = _noteSaveWaiters.get(msg.reqId);
        _noteSaveWaiters.delete(msg.reqId);
        w.reject(new Error(msg.message || "Note 오류"));
      } else {
        showToast(msg.message || "Note 오류");
      }
      break;
    }
  }
});

function noteRenderMeta(saved) {
  const el = document.getElementById("noteMeta");
  if (!el) return;
  const info = saved && saved.sheet ? saved : null;
  if (info && info.updated_at) {
    el.textContent = `마지막 저장: ${info.updated_by || "?"} · ${info.updated_at}`;
  } else {
    const ni = DATA && DATA.note_info;
    el.textContent = (ni && ni.exists) ? `마지막 저장: ${ni.updated_by || "?"} · ${ni.updated_at || ""}` : "";
  }
}

function noteMarkDirty() {
  _noteDirty = true;
  const btn = document.getElementById("noteSave");
  if (btn) btn.classList.add("dirty");
}

// ── 저장: iframe 에 현재 시트 요청 → 받은 JSON 을 서버에 POST ────────────────────
// opts.force: 충돌(409) 안내 후 사용자가 덮어쓰기를 택했을 때의 재전송.
async function noteSave(opts) {
  if (!_noteReady) { showToast("Note 가 아직 준비되지 않았습니다."); return; }
  const btn = document.getElementById("noteSave");
  if (btn) btn.disabled = true;
  try {
    const sheets = await noteRequestSheets();
    // 빈 시트 배열을 보내면 서버가 400 으로 거부하지만, 애초에 POST 하지 않는다
    // (프레임 직렬화 이상 시 기존 Note 를 빈 내용으로 치환하는 사고 방지).
    if (!Array.isArray(sheets) || !sheets.length) throw new Error("시트 데이터가 비어 있습니다 — 저장하지 않았습니다");
    const payload = { sheet: { sheets }, base: _noteBaseToken };
    if (opts && opts.force) payload.force = true;
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/note`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(payload),
    });
    const j = await res.json().catch(() => ({}));
    if (res.status === 409) { noteResolveConflict(j); return; }
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    _noteBaseToken = j.base || null;
    _noteDirty = false;
    if (btn) btn.classList.remove("dirty");
    if (DATA) DATA.note_info = { exists: true, updated_by: LOGIN_USER, updated_at: "" };
    showToast("Note 를 저장했습니다.");
  } catch (e) {
    showToast("Note 저장 실패: " + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 409 — 내가 Note 를 연 뒤 다른 사용자가 저장했다. 시트는 통째 치환이라 어느 쪽을
// 살릴지 사용자가 정해야 한다(자동 병합 불가).
function noteResolveConflict(j) {
  const who = (j && j.conflict && j.conflict.updated_by) || "다른 사용자";
  const overwrite = confirm(
    `${who} 님이 이 Note 를 먼저 저장했습니다.\n\n` +
    `[확인] 내 편집으로 덮어쓰기 — ${who} 님의 저장 내용이 사라집니다.\n` +
    `[취소] 서버 최신본 다시 불러오기 — 내 미저장 편집이 사라집니다.`);
  if (overwrite) { noteSave({ force: true }); return; }
  _noteDirty = false;      // renderNoteTab 의 미저장 편집 보존 가드를 풀어야 재로드된다
  renderNoteTab();
  showToast("서버 최신 Note 를 다시 불러왔습니다.");
}

// iframe 에 시트 직렬화 요청 (postMessage 왕복). reqId 로 응답 매칭 + 타임아웃.
function noteRequestSheets() {
  const frame = document.getElementById("noteFrame");
  if (!frame || !frame.contentWindow) return Promise.reject(new Error("Note 프레임이 없습니다."));
  const reqId = ++_noteReqSeq;
  return new Promise((resolve, reject) => {
    _noteSaveWaiters.set(reqId, { resolve, reject });
    setTimeout(() => {
      if (_noteSaveWaiters.has(reqId)) { _noteSaveWaiters.delete(reqId); reject(new Error("시트 응답 시간 초과")); }
    }, 10000);
    frame.contentWindow.postMessage({ type: "note:getSheets", reqId }, NOTE_ORIGIN);
  });
}

// iframe 에 현재 선택 셀 조회 요청 (getSheets 와 동일 왕복 구조). null=선택 없음.
function noteRequestSelection() {
  const frame = document.getElementById("noteFrame");
  if (!frame || !frame.contentWindow) return Promise.reject(new Error("Note 프레임이 없습니다."));
  const reqId = ++_noteReqSeq;
  return new Promise((resolve, reject) => {
    _noteSaveWaiters.set(reqId, { resolve, reject });
    setTimeout(() => {
      if (_noteSaveWaiters.has(reqId)) { _noteSaveWaiters.delete(reqId); reject(new Error("선택 응답 시간 초과")); }
    }, 10000);
    frame.contentWindow.postMessage({ type: "note:getSelection", reqId }, NOTE_ORIGIN);
  });
}

// ── 앵커 태그 팝오버 (note-bar [🔖 태그]) ─────────────────────────────────────
// 태그 목록(전원 조회·클릭 이동) + 편집자만 이름 입력·현재 셀 태그·삭제. 저장은 즉시
// (kind=note_tag DB) — Note 시트 저장(base 잠금)과 독립 채널이다.
function noteToggleTagPanel() {
  const panel = document.getElementById("noteTagPanel");
  if (!panel) return;
  if (panel.style.display === "none" || !panel.style.display) {
    noteRenderTagPanel();
    panel.style.display = "block";
  } else {
    panel.style.display = "none";
  }
}
function noteHideTagPanel() {
  const panel = document.getElementById("noteTagPanel");
  if (panel) panel.style.display = "none";
}
function noteRenderTagPanel() {
  const panel = document.getElementById("noteTagPanel");
  if (!panel) return;
  const tags = (DATA && DATA.note_tags) || {};
  const names = Object.keys(tags).sort((a, b) => a.localeCompare(b));
  const canEdit = _noteCanEdit;
  let html = `<div class="note-tag-head">🔖 앵커 태그<span class="note-tag-close" id="noteTagClose" title="닫기">✕</span></div>`;
  if (!names.length) {
    html += `<div class="note-tag-empty">등록된 태그가 없습니다.${canEdit ? " 셀을 선택하고 아래에서 추가하세요." : ""}</div>`;
  } else {
    html += `<div class="note-tag-list">` + names.map(n => {
      const t = tags[n] || {};
      const loc = t.sheet_name ? esc(t.sheet_name) : "";
      return `<div class="note-tag-row">
        <span class="note-tag-go" data-name="${esc(n)}" title="이 셀로 이동">#${esc(n)}</span>
        <span class="note-tag-loc">${loc}</span>
        ${canEdit ? `<span class="note-tag-del" data-name="${esc(n)}" title="삭제">🗑</span>` : ""}
      </div>`;
    }).join("") + `</div>`;
  }
  if (canEdit) {
    html += `<div class="note-tag-add">
        <input type="text" id="noteTagName" maxlength="40" placeholder="태그 이름" data-no-dirty />
        <button type="button" class="btn-sm" id="noteTagAdd">현재 셀에 태그</button>
      </div>
      <div class="note-tag-note">선택한 셀에 태그 이름을 텍스트로 적고 앵커를 겁니다. 같은 이름은 위치가 갱신됩니다. 셀 내용은 Note 저장을 눌러야 보존됩니다.</div>`;
  }
  panel.innerHTML = html;
  panel.onclick = noteTagPanelClick;
}
function noteTagPanelClick(e) {
  if (e.target.closest("#noteTagClose")) { noteHideTagPanel(); return; }
  const del = e.target.closest(".note-tag-del");
  if (del) { noteDeleteTag(del.dataset.name); return; }
  if (e.target.closest("#noteTagAdd")) {
    const inp = document.getElementById("noteTagName");
    noteCreateTag((inp && inp.value) || "");
    return;
  }
  const go = e.target.closest(".note-tag-go");
  if (go) { noteHideTagPanel(); noteJumpToTag(go.dataset.name); return; }
}
async function noteCreateTag(name) {
  name = String(name || "").trim();
  if (!name) { showToast("태그 이름을 입력하세요."); return; }
  if (/[\[\]#@]/.test(name)) { showToast("태그 이름에 [ ] # @ 는 쓸 수 없습니다."); return; }
  if (!_noteReady) { showToast("Note 가 아직 준비되지 않았습니다."); return; }
  let sel;
  try { sel = await noteRequestSelection(); }
  catch (e) { showToast("선택 셀 조회 실패: " + e.message); return; }
  if (!sel) { showToast("먼저 Note 에서 셀을 선택하세요."); return; }
  // 태그 이름을 선택 셀에 텍스트로 기록한다. 이미 다른 내용이 있으면 덮어쓸지 확인.
  let writeCell = true;
  const curVal = String(sel.v == null ? "" : sel.v).trim();
  if (curVal && curVal !== name) {
    writeCell = confirm(`선택한 셀에 이미 내용이 있습니다:\n"${curVal.slice(0, 60)}"\n\n` +
      `[확인] 태그 이름 "${name}" 으로 덮어쓰기\n[취소] 셀 내용은 두고 태그(앵커)만 걸기`);
  }
  const target = { tab: "note", sheet: sel.sheet, sheet_name: sel.sheetName, r: sel.r, c: sel.c };
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/note_tags`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ action: "set", name, target }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    if (DATA) DATA.note_tags = j.note_tags || {};
    if (writeCell) noteWriteCell(sel, name);   // 셀에 이름 기록 (iframe → note:dirty)
    noteRenderTagPanel();
    showToast(writeCell
      ? `태그 #${name} 저장 — 셀에 이름을 적었습니다. Note 저장을 눌러야 셀 내용이 보존됩니다.`
      : `태그 #${name} 저장 — comment 에서 #${name} 로 링크됩니다.`);
  } catch (e) {
    showToast("태그 저장 실패: " + e.message);
  }
}
// 선택 셀에 태그 이름 텍스트 기록 요청 (iframe 이 setCellValue + note:dirty).
function noteWriteCell(sel, value) {
  const frame = document.getElementById("noteFrame");
  if (!frame || !frame.contentWindow) return;
  frame.contentWindow.postMessage(
    { type: "note:setCell", sheet: sel.sheet, r: sel.r, c: sel.c, value: String(value) }, NOTE_ORIGIN);
}
async function noteDeleteTag(name) {
  name = String(name || "");
  if (!confirm(`태그 #${name} 을(를) 삭제할까요? comment 의 링크는 남지만 이동할 수 없게 됩니다.`)) return;
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/note_tags`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ action: "delete", name }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
    if (DATA) DATA.note_tags = j.note_tags || {};
    noteRenderTagPanel();
    showToast(`태그 #${name} 삭제.`);
  } catch (e) {
    showToast("태그 삭제 실패: " + e.message);
  }
}

// ── comment 의 #[태그] 클릭 → Note 탭 전환 + 해당 셀로 이동 (noteQueueImage 패턴) ──
function noteJumpToTag(name) {
  const tags = (DATA && DATA.note_tags) || {};
  const tag = tags[String(name)];
  if (!tag) { showToast(`태그 #${name} 을(를) 찾을 수 없습니다 (삭제되었을 수 있습니다).`); return; }
  if (tag.tab && tag.tab !== "note") { showToast("이 태그는 Note 셀 태그가 아닙니다."); return; }
  _notePendingGoto = { sheet: tag.sheet, sheetName: tag.sheet_name, r: tag.r, c: tag.c };
  const panel = document.getElementById("panel-note");
  const active = panel && panel.classList.contains("active");
  if (_noteReady && active) { noteFlushGoto(); return; }
  const tabBtn = document.querySelector('.tab[data-tab="note"]');
  if (tabBtn) tabBtn.click();   // renderTab("note") → iframe init 후 noteFlushGoto
  if (_noteReady) setTimeout(() => noteFlushGoto(), 150);
}
function noteFlushGoto() {
  if (!_noteReady || !_notePendingGoto) return;
  const frame = document.getElementById("noteFrame");
  if (!frame || !frame.contentWindow) return;
  const g = _notePendingGoto; _notePendingGoto = null;
  frame.contentWindow.postMessage(Object.assign({ type: "note:goto" }, g), NOTE_ORIGIN);
}

// 미저장 Note 이탈 경고 (chart_notes 의 dirty 와 별개 채널).
window.addEventListener("beforeunload", e => {
  if (leaveGuardBypassed()) return;   // 이탈 확인 모달에서 확정
  if (_noteDirty) { e.preventDefault(); e.returnValue = ""; }
});

// ── 차트 이미지 삽입 (chart_notes.js → 업로드 완료 URL 전달) ───────────────────
function noteQueueImage(url, caption) {
  _notePendingImgs.push({ url, caption });
  const panel = document.getElementById("panel-note");
  const active = panel && panel.classList.contains("active");
  if (_noteReady && active) { noteFlushPending(); return; }
  showToast("Note 탭으로 이동해 차트를 붙여넣습니다…");
  const tabBtn = document.querySelector('.tab[data-tab="note"]');
  if (tabBtn) tabBtn.click();   // renderTab("note") → iframe init 후 noteFlushPending
  if (_noteReady) setTimeout(() => noteFlushPending(), 150);
}

function noteFlushPending() {
  if (!_noteReady || !_notePendingImgs.length) return;
  const frame = document.getElementById("noteFrame");
  if (!frame || !frame.contentWindow) return;
  const items = _notePendingImgs.splice(0);
  items.forEach(it => frame.contentWindow.postMessage(
    { type: "note:insertImage", url: it.url }, NOTE_ORIGIN));
  noteMarkDirty();
  showToast("차트를 Note 에 붙여넣었습니다 — 드래그로 위치를 옮기고 저장하세요.");
}

// 탭 재진입/표시 시 iframe 캔버스 재계산 (숨김 상태에서 크기가 0 이었던 경우 복구).
function noteOnTabShown() {
  const frame = document.getElementById("noteFrame");
  if (_noteReady && frame && frame.contentWindow) {
    frame.contentWindow.postMessage({ type: "note:resize" }, NOTE_ORIGIN);
  }
}

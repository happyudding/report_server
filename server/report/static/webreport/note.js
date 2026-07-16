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
let _noteCanEdit = false;      // 현재 렌더의 편집 가능 여부
let _noteFrameReady = false;   // iframe 이 note:ready 전송함
let _noteFetched = false;      // 저장 시트 fetch 완료
let _noteSavedSheets = null;   // fetch 로 받은 시트 배열(없으면 null → 프레임이 기본시트 생성)
let _noteInitSent = false;     // init 중복 전송 방지
let _noteReqSeq = 0;           // getSheets 요청 id
const _noteSaveWaiters = new Map();   // reqId → {resolve, reject}

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
  panel.innerHTML = `
    <div class="note-bar" id="noteBar">
      ${canEdit ? `<button type="button" class="btn-sm note-save" id="noteSave">💾 Note 저장</button>` : ""}
      <span class="note-meta" id="noteMeta"></span>
      <span class="cnote-hint">${canEdit
        ? "차트는 각 항목 상세의 [📋 Note에 붙여넣기]로 담고, 셀에는 텍스트·수식(=A1-B1)·엑셀 붙여넣기가 됩니다. 저장 버튼을 눌러야 서버에 반영됩니다."
        : "읽기 전용 — 편집 권한자가 정리한 Note 입니다."}</span>
    </div>
    <iframe id="noteFrame" src="${NOTE_FRAME_SRC}" title="Note 시트"></iframe>`;
  const saveBtn = document.getElementById("noteSave");
  if (saveBtn) saveBtn.onclick = () => noteSave();

  // 저장 시트 fetch 와 iframe 로드는 병렬 — 둘 다 완료되면 noteMaybeInit 이 init 전송.
  fetch(`/pe/report/session/${SESSION_ID}/web_report/note`, { cache: "no-store" })
    .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(saved => {
      if (token !== _noteInitToken) return;   // 그 사이 재렌더됨
      _noteSavedSheets = (saved && saved.sheet && Array.isArray(saved.sheet.sheets) && saved.sheet.sheets.length)
        ? saved.sheet.sheets : null;
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
async function noteSave() {
  if (!_noteReady) { showToast("Note 가 아직 준비되지 않았습니다."); return; }
  const btn = document.getElementById("noteSave");
  if (btn) btn.disabled = true;
  try {
    const sheets = await noteRequestSheets();
    // 빈 시트 배열을 보내면 서버가 400 으로 거부하지만, 애초에 POST 하지 않는다
    // (프레임 직렬화 이상 시 기존 Note 를 빈 내용으로 치환하는 사고 방지).
    if (!Array.isArray(sheets) || !sheets.length) throw new Error("시트 데이터가 비어 있습니다 — 저장하지 않았습니다");
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/note`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ sheet: { sheets } }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
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

// 미저장 Note 이탈 경고 (chart_notes 의 dirty 와 별개 채널).
window.addEventListener("beforeunload", e => {
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

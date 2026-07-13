// ── Note 탭 — Luckysheet 시트 캔버스 (엑셀 정리 워크플로의 대체) ─────────────────
// 탭 전체가 스프레드시트다: 차트 PNG 붙여넣기(플로팅 이미지), 셀 텍스트/수식/서식,
// 엑셀 range 복사→붙여넣기. 계산은 전부 브라우저(Luckysheet)에서 돌고 서버는
// 시트 JSON 저장만 한다 (POST .../web_report/note, kind=note_sheet, 상한 2MB).
// 조회는 전원, 편집·저장은 편집 권한자(MODE==="edit")만 — allowEdit 로 강제.
// Luckysheet 번들(≈4MB)은 vendored 후 Note 탭 첫 진입 시에만 지연 로드한다
// (trim.js loadExcelJS 패턴). 라이선스: MIT (vendor/luckysheet/LICENSE).

const NOTE_VENDOR = "/pe/report/vendor/luckysheet";
const NOTE_CSS = [
  `${NOTE_VENDOR}/plugins/css/pluginsCss.css`,
  `${NOTE_VENDOR}/plugins/plugins.css`,
  `${NOTE_VENDOR}/css/luckysheet.css`,
  `${NOTE_VENDOR}/assets/iconfont/iconfont.css`,
];
const NOTE_JS = [
  `${NOTE_VENDOR}/plugins/js/plugin.js`,
  `${NOTE_VENDOR}/luckysheet.umd.js`,
];

let _noteLibPromise = null;   // 라이브러리 로드 (1회)
let _noteReady = false;       // luckysheet.create 완료
let _noteDirty = false;       // 미저장 시트 편집
let _noteInitToken = 0;       // 재렌더 경합 가드
let _notePendingImgs = [];    // Note 미초기화 상태에서 요청된 삽입 이미지 큐 [{url, caption}]
let _noteResizeObs = null;    // 컨테이너 크기 변화 감시 (ResizeObserver) → 캔버스 재계산
let _noteResizeRaf = 0;       // resize 프레임 정렬 coalesce 토큰
let _noteDpr = 0;             // create 시점 devicePixelRatio (런타임 배율 변화 감지용)

function noteLoadLib() {
  if (window.luckysheet) return Promise.resolve();
  if (_noteLibPromise) return _noteLibPromise;
  NOTE_CSS.forEach(href => {
    const l = document.createElement("link");
    l.rel = "stylesheet"; l.href = href;
    document.head.appendChild(l);
  });
  const loadScript = src => new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`${src} 로드 실패`));
    document.head.appendChild(s);
  });
  _noteLibPromise = NOTE_JS.reduce((p, src) => p.then(() => loadScript(src)), Promise.resolve())
    .then(() => { if (!window.luckysheet) throw new Error("Luckysheet 로드 실패"); })
    .catch(e => { _noteLibPromise = null; throw e; });
  return _noteLibPromise;
}

function noteDefaultSheets() {
  return [{ name: "Note", color: "", index: "note_sheet_1", status: 1, order: 0,
            celldata: [], config: {}, row: 84, column: 26 }];
}

// ── 탭 렌더 (edit_mode.js TAB_RENDERERS["note"] → 탭 첫 활성화 시) ─────────────
function renderNoteTab() {
  const panel = document.getElementById("panel-note");
  if (!panel) return;
  if (!isWebReportSession()) { emptyPanel(panel, "web_report 세션에서만 사용할 수 있습니다."); return; }
  // 미저장 편집이 있으면 재렌더로 날리지 않는다 (load(false) 후 renderActive 가 다시 불러도 보존).
  if (_noteReady && _noteDirty) return;
  const canEdit = MODE === "edit";
  const token = ++_noteInitToken;
  panel.innerHTML = `
    <div class="note-bar" id="noteBar">
      ${canEdit ? `<button type="button" class="btn-sm note-save" id="noteSave">💾 Note 저장</button>` : ""}
      <span class="note-meta" id="noteMeta"></span>
      <span class="cnote-hint">${canEdit
        ? "차트는 각 항목 상세의 [📋 Note에 붙여넣기]로 담고, 셀에는 텍스트·수식(=A1-B1)·엑셀 붙여넣기가 됩니다. 저장 버튼을 눌러야 서버에 반영됩니다."
        : "읽기 전용 — 편집 권한자가 정리한 Note 입니다."}</span>
    </div>
    <div id="luckysheetHost"><div class="placeholder">Note 시트 로드 중…</div></div>`;
  const saveBtn = document.getElementById("noteSave");
  if (saveBtn) saveBtn.onclick = () => noteSave();

  Promise.all([
    noteLoadLib(),
    fetch(`/pe/report/session/${SESSION_ID}/web_report/note`, { cache: "no-store" })
      .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }),
  ]).then(([, saved]) => {
    if (token !== _noteInitToken) return;   // 그 사이 재렌더됨
    // 라이브러리(≈4MB)+fetch 로드 중 사용자가 다른 탭으로 이동했으면 host 가
    // display:none(크기 0)이라 이 상태로 create 하면 캔버스가 0px 로 굳는다. 재렌더
    // 대상으로 되돌리고 보류 — 탭 재진입 시 renderTab("note")가 정상 크기로 다시 그린다.
    if (!panel.classList.contains("active")) { tabDirty["note"] = true; return; }
    noteCreate(saved, canEdit);
  }).catch(e => {
    if (token !== _noteInitToken) return;
    const host = document.getElementById("luckysheetHost");
    if (host) host.innerHTML = `<div class="placeholder">Note 로드 실패: ${esc(e.message)}</div>`;
  });
}

function noteCreate(saved, canEdit) {
  const host = document.getElementById("luckysheetHost");
  if (!host) return;
  host.innerHTML = "";
  try { window.luckysheet.destroy(); } catch (e) {}
  let sheets = (saved && saved.sheet && Array.isArray(saved.sheet.sheets) && saved.sheet.sheets.length)
    ? saved.sheet.sheets : noteDefaultSheets();
  if (!sheets.some(s => s.status === 1)) sheets[0].status = 1;
  window.luckysheet.create({
    container: "luckysheetHost",
    // HiDPI/디스플레이 배율 대응. 명시하지 않으면 번들이 ga.devicePixelRatio 를 1 로 남겨
    // (실측 확인) 그리드는 1×, 선택 오버레이는 window.devicePixelRatio(예:2×)로 그려져
    // 좌표계가 어긋난다 → 셀 안 맞음 + 클릭 시 격자 떨림. 실제 비율을 넘겨 Math.ceil 로
    // 전 캔버스를 같은 정수 배율로 통일한다(125/150/200% 모두 정합).
    devicePixelRatio: window.devicePixelRatio || 1,
    lang: "en",
    data: sheets,
    showinfobar: false,
    allowEdit: canEdit,
    showtoolbar: canEdit,
    showsheetbar: true,
    showstatisticBar: canEdit,
    enableAddRow: canEdit,
    enableAddBackTop: false,
    sheetFormulaBar: canEdit,
    hook: {
      updated: () => { if (canEdit) noteMarkDirty(); },
    },
  });
  _noteReady = true;
  _noteDirty = false;
  _noteDpr = window.devicePixelRatio || 1;   // 이후 배율 변화 감지 기준
  noteRenderMeta(saved);
  // 첫 진입 정합 + 이후 컨테이너 크기 변화(늦은 레이아웃·창 리사이즈·메타 리플로우·
  // 숨김→표시)를 모두 캔버스에 반영. 단발 타이머는 폰트·스크롤바 확정이 60ms 뒤면
  // 놓치므로, 프레임 정렬 resize + host 크기 감시(ResizeObserver)로 대체한다.
  noteScheduleResize();
  noteAttachResizeObserver(host);
  // 대기 중인 차트 이미지 삽입 (📋 Note에 붙여넣기 → 탭 전환 직후 도착하는 경우)
  if (canEdit && _notePendingImgs.length) {
    // create 직후 내부 초기화가 끝나도록 다음 틱에 삽입.
    setTimeout(() => noteFlushPending(), 300);
  }
}

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

// ── 저장: getAllSheets → data(2D 렌더 행렬) 를 celldata 로 치환해 경량화 후 POST ──
async function noteSave() {
  if (!_noteReady || !window.luckysheet) { showToast("Note 가 아직 준비되지 않았습니다."); return; }
  const btn = document.getElementById("noteSave");
  if (btn) btn.disabled = true;
  try {
    const sheets = JSON.parse(JSON.stringify(window.luckysheet.getAllSheets()));
    sheets.forEach(s => {
      if (Array.isArray(s.data)) {
        s.celldata = window.luckysheet.transToCellData(s.data);
        delete s.data;
      }
      delete s.load;
    });
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

// 미저장 Note 이탈 경고 (chart_notes 의 dirty 와 별개 채널).
window.addEventListener("beforeunload", e => {
  if (_noteDirty) { e.preventDefault(); e.returnValue = ""; }
});

// ── 차트 이미지 삽입 (chart_notes.js → 업로드 완료 URL 전달) ───────────────────
function noteQueueImage(url, caption) {
  _notePendingImgs.push({ url, caption });
  const tabBtn = document.querySelector('.tab[data-tab="note"]');
  const panel = document.getElementById("panel-note");
  const active = panel && panel.classList.contains("active");
  if (_noteReady && active) { noteFlushPending(); return; }
  showToast("Note 탭으로 이동해 차트를 붙여넣습니다…");
  if (tabBtn) tabBtn.click();   // renderTab("note") → 초기화 후 noteFlushPending
  if (_noteReady) setTimeout(() => noteFlushPending(), 150);
}

function noteFlushPending() {
  if (!_noteReady || !window.luckysheet || !_notePendingImgs.length) return;
  const items = _notePendingImgs.splice(0);
  items.forEach(it => {
    try {
      window.luckysheet.insertImage(it.url, { rowIndex: 1, colIndex: 1 });
    } catch (e) {
      showToast("이미지 삽입 실패: " + e.message);
      return;
    }
  });
  noteMarkDirty();
  showToast("차트를 Note 에 붙여넣었습니다 — 드래그로 위치를 옮기고 저장하세요.");
}

// ── 캔버스 정합 resize (프레임 정렬 + ResizeObserver) ─────────────────────────
// Luckysheet 는 create 시점 host 크기로 캔버스를 그린다. 폰트/스크롤바로 레이아웃이 늦게
// 확정되거나(첫 진입) 창 리사이즈·메타 리플로우·숨김→표시로 host 크기가 바뀌면 캔버스와
// CSS 박스가 어긋나 셀이 안 맞고 클릭 시 격자가 떨린다 → 크기 변화를 감지해 재계산한다.
function noteScheduleResize() {
  if (_noteResizeRaf) cancelAnimationFrame(_noteResizeRaf);
  _noteResizeRaf = requestAnimationFrame(() => {
    _noteResizeRaf = requestAnimationFrame(() => {
      _noteResizeRaf = 0;
      if (!_noteReady || !window.luckysheet) return;
      // 배율이 바뀌면(모니터 이동·브라우저 줌) create 시점 devicePixelRatio 로 그려진
      // 캔버스가 어긋난다. resize() 로는 배율을 못 바꾸므로 재생성으로 다시 잡는다.
      // 미저장 편집(_noteDirty)이 있으면 날리지 않도록 건너뛴다(저장/재진입 시 정합).
      const dpr = window.devicePixelRatio || 1;
      const panel = document.getElementById("panel-note");
      if (dpr !== _noteDpr && !_noteDirty && panel && panel.classList.contains("active")) {
        renderNoteTab();
        return;
      }
      try { window.luckysheet.resize(); } catch (e) {}
    });
  });
}

function noteAttachResizeObserver(host) {
  if (_noteResizeObs) { try { _noteResizeObs.disconnect(); } catch (e) {} }
  _noteResizeObs = null;
  if (typeof ResizeObserver === "undefined" || !host) return;
  // host 는 renderNoteTab 마다 새로 생기므로 create 마다 재부착. observe 는 현재 크기로도
  // 즉시 1회 발화하므로 첫 진입 정합도 겸한다. resize() 는 host 자식만 바꿔 host 자신의
  // 박스는 불변 → 관찰 대상이 안 바뀌어 재발화 루프는 생기지 않는다.
  _noteResizeObs = new ResizeObserver(() => noteScheduleResize());
  try { _noteResizeObs.observe(host); } catch (e) {}
}

// 탭 재진입 시 캔버스 리사이즈 (숨김 상태에서 크기가 0 이었던 경우 복구).
function noteOnTabShown() {
  if (_noteReady && window.luckysheet) noteScheduleResize();
}

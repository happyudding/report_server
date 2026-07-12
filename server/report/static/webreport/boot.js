// ── load 오버레이 진행바 (0→100% + 단계 메시지) ────────────────────────────────
let _loadCreep = null;
function setLoadProgress(pct, msg) {
  pct = Math.max(0, Math.min(100, pct));
  const fill = document.getElementById("loadFill");
  const pc = document.getElementById("loadPct");
  const st = document.getElementById("loadStatus");
  if (fill) fill.style.width = pct + "%";
  if (pc) pc.textContent = Math.round(pct) + "%";
  if (st && msg != null) st.textContent = msg;
}
function stopLoadCreep() { if (_loadCreep) { clearInterval(_loadCreep); _loadCreep = null; } }
// 서버 재계산 대기(첫 바이트 전)엔 실제 진척을 알 수 없어 from→to 로 ease-out 천천히 채운다.
function startLoadCreep(from, to, ms, msg) {
  stopLoadCreep();
  setLoadProgress(from, msg);
  const t0 = performance.now();
  _loadCreep = setInterval(() => {
    const k = Math.min(1, (performance.now() - t0) / ms);
    setLoadProgress(from + (to - from) * (1 - Math.pow(1 - k, 2)));
    if (k >= 1) stopLoadCreep();
  }, 90);
}
function showLoadOverlay() {
  const ov = document.getElementById("loadOverlay");
  if (ov) ov.classList.add("show");
  setLoadProgress(0, "준비 중…");
}
function hideLoadOverlay() {
  stopLoadCreep();
  setLoadProgress(100, "완료");
  const ov = document.getElementById("loadOverlay");
  // 100% 표시가 한 프레임은 보이도록 최소 지연만 둔다 (체감 지연 최소화: 200→50ms)
  if (ov) setTimeout(() => ov.classList.remove("show"), 50);
}
async function load(resetMode=true) {
  try {
    showLoadOverlay();
    // 서버가 parquet 재계산(수 초)을 마칠 때까지 첫 바이트가 없어 실제 %를 알 수 없으므로
    // 대기 동안 62% 까지 천천히 채운다.
    startLoadCreep(6, 62, 4500, "리포트 계산·수신 중…");
    // head 의 선행 fetch 가 있으면 재사용 (1회성 — 편집 후 재로드는 새로 fetch).
    // cache:"no-cache" 는 ETag 재검증이라 재방문 시 304 로 다운로드를 생략한다.
    const prefetched = window.__fullPrefetch;
    window.__fullPrefetch = null;
    const freshFull = () => fetch(`/pe/report/session/${SESSION_ID}/full`, { cache: "no-cache" });
    const [res] = await Promise.all([
      prefetched ? prefetched.then(r => r || freshFull()) : freshFull(),
      loadAuth(),   // 편집 권한(MODE) 판정에 필요 — /full 수신과 병렬로
    ]);
    if (!res.ok) {
      const box = document.getElementById("errorBox");
      box.style.display = "";
      box.textContent = `세션을 불러올 수 없습니다 (${res.status})`;
      hideLoadOverlay();
      return;
    }
    stopLoadCreep();
    setLoadProgress(70, "데이터 파싱 중…");
    DATA = await res.json();
    // web_report 전용 뷰어 — xlsx 업로드(legacy) 세션 렌더링은 더 이상 지원하지 않는다.
    if (!isWebReportSession()) {
      document.querySelectorAll(".content .panel").forEach(p => {
        p.innerHTML = `<div class="placeholder" style="padding:24px;">이 세션은 web_report 형식이 아니어서 더 이상 표시할 수 없습니다.<br>(xlsx 업로드 흐름은 추후 재작업 예정)</div>`;
      });
      document.getElementById("topbarActions").style.display = "none";
      renderMeta(DATA.session || {});
      hideLoadOverlay();   // 이 분기가 오버레이를 안 걷으면 안내문까지 가려진다
      return;
    }
    setLoadProgress(80, "화면 구성 중…");
    _globalBinColors = null;   // 새 데이터 → bin 색상 매핑 재계산
    seedEmptyFrames();
    // Distribution 인덱스와 색상만 준비한다. ECDF 는 화면에 보이는 항목을 70개 이하로
    // 묶어 /distribution/query 에서 가져오며 전체 payload 는 선다운로드하지 않는다.
    const webPre = DATA.web_report || {};
    setLoadProgress(92, "분포 데이터 준비 중…");
    distResetDataIfChanged();
    buildDistColorMap(webPre.sources || []);
    renderMeta(DATA.session || {});
    // 편집 권한: 로그인 ID == 업로더(기록 없으면 로그인만으로) 일 때만 edit 렌더.
    MODE = canEditSession() ? "edit" : "view";
    const canEdit = MODE === "edit";
    // 저장·중요표시(개인)는 편집 권한자(업로더+위임 편집자) 공통.
    document.getElementById("btnSaveComment").style.display = canEdit ? "" : "none";
    document.getElementById("btnImportant").style.display = canEdit ? "" : "none";
    // 비공개·삭제·권한부여는 업로더 전용.
    document.getElementById("btnPrivate").style.display = IS_UPLOADER ? "" : "none";
    document.getElementById("btnDel").style.display = IS_UPLOADER ? "" : "none";
    document.getElementById("settingsTabPerm").style.display = IS_UPLOADER ? "" : "none";
    renderActive();
    hideLoadOverlay();
  } catch (e) {
    const box = document.getElementById("errorBox");
    box.style.display = "";
    box.textContent = "로드 실패: " + e.message;
    hideLoadOverlay();
  }
}

// vendor 가 defer 로 로드되므로 DOMContentLoaded(=defer 실행 완료) 후에 시작한다.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", load);
} else {
  load();
}

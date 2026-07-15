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
// 콜드 빌드(첫 조회 서버 계산)가 길어질 때 62% 정지가 "멈춤"으로 보이지 않도록
// 시간 경과에 따라 메시지를 갱신하고 아주 느린 2차 creep(62→85, 90s)을 이어간다.
let _loadStageTimers = [];
function clearLoadStageTimers() {
  _loadStageTimers.forEach(t => clearTimeout(t));
  _loadStageTimers = [];
}
function scheduleLoadStageMsgs() {
  clearLoadStageTimers();
  _loadStageTimers = [
    setTimeout(() => startLoadCreep(62, 78, 45000,
      "서버가 리포트를 계산하고 있습니다…"), 15000),
    setTimeout(() => startLoadCreep(78, 85, 60000,
      "대용량 세션은 첫 조회에 1~2분 걸릴 수 있습니다 (이후 조회는 즉시 열립니다)"), 60000),
  ];
}
function showLoadOverlay() {
  const ov = document.getElementById("loadOverlay");
  if (ov) ov.classList.add("show");
  setLoadProgress(0, "준비 중…");
}
function hideLoadOverlay() {
  stopLoadCreep();
  clearLoadStageTimers();
  setLoadProgress(100, "완료");
  const ov = document.getElementById("loadOverlay");
  // 100% 표시가 한 프레임은 보이도록 최소 지연만 둔다 (체감 지연 최소화: 200→50ms)
  if (ov) setTimeout(() => ov.classList.remove("show"), 50);
}
async function load(resetMode=true) {
  try {
    showLoadOverlay();
    // 서버가 parquet 재계산(수 초)을 마칠 때까지 첫 바이트가 없어 실제 %를 알 수 없으므로
    // 대기 동안 62% 까지 천천히 채우고, 콜드 빌드가 길어지면 단계 메시지로 안내한다.
    startLoadCreep(6, 62, 4500, "리포트 계산·수신 중…");
    scheduleLoadStageMsgs();
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
    clearLoadStageTimers();   // 응답 도착 — 콜드 대기 단계 메시지 예약 해제
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
    // Issue Table 의 미니 분포 차트가 renderDistribution() 보다 먼저 그려질 수 있으므로
    // distDataCache/distColorMap 은 렌더 순서와 무관하게 데이터 로드 직후 미리 준비해둔다.
    // distribution_deferred 응답이면 대용량 ECDF 를 백그라운드로 지연 로드하고
    // (첫 페인트를 막지 않음), 구형 embed 응답이면 기존 경로 그대로 (하위호환).
    const webPre = DATA.web_report || {};
    // Distribution ECDF 는 항상 지연 로드 (GET .../web_report/distribution 컴팩트 columnar).
    // 도착 전 그려진 미니셀/갤러리는 refreshDistConsumers 가 다시 채운다.
    setLoadProgress(92, "분포 데이터 준비 중…");
    ensureDistData();
    // Map dies(수백만 개 가능)도 /full 에서 제외됨(map_deferred) — 백그라운드 지연 로드.
    ensureMapData();
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

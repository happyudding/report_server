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
// 진행률은 그대로 두고 안내 문구만 교체 (creep 이 채우는 %를 건드리지 않는다).
function setLoadMessage(msg) {
  const st = document.getElementById("loadStatus");
  if (st) st.textContent = msg;
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
// 메시지는 추정이 아니라 서버 build_status 폴링의 실측(계산 중 여부 + 경과초)으로 덮어쓴다.
let _loadStageTimers = [];
function clearLoadStageTimers() {
  _loadStageTimers.forEach(t => clearTimeout(t));
  _loadStageTimers = [];
  stopBuildStatusPoll();
}
function scheduleLoadStageMsgs() {
  clearLoadStageTimers();
  // 폴링이 실측 문구를 대고 있으면(_buildStatusLive) 여기 추정 문구는 넘기지 않고(null)
  // % creep 만 이어간다 — 두 문구가 서로 덮어써 깜빡이지 않도록.
  _loadStageTimers = [
    setTimeout(() => startLoadCreep(62, 78, 45000, _buildStatusLive ? null :
      "서버가 리포트를 계산하고 있습니다…"), 15000),
    setTimeout(() => startLoadCreep(78, 85, 60000, _buildStatusLive ? null :
      "대용량 세션은 첫 조회에 1~2분 걸릴 수 있습니다 (이후 조회는 즉시 열립니다)"), 60000),
  ];
  startBuildStatusPoll();
}

// ── 콜드 빌드 실측 폴링 ────────────────────────────────────────────────────────
// /full 은 계산이 끝나야 첫 바이트가 오므로, 그 사이 서버가 실제로 빌드 중인지·몇 초째인지
// 를 별도 경량 라우트로 물어 안내 문구를 사실로 바꾼다. 응답이 없거나 idle 이면(구서버·
// 웜 세션) 위 시간 기반 문구를 그대로 두고 조용히 물러난다 — 폴링은 안내 전용이라
// 실패해도 로드에 영향을 주지 않는다.
let _buildPoll = null;
let _buildStatusLive = false;   // 폴링이 "계산 중"을 실제로 확인했는가 (문구 우선순위용)
function stopBuildStatusPoll() {
  if (_buildPoll) { clearInterval(_buildPoll); _buildPoll = null; }
  _buildStatusLive = false;
}
function startBuildStatusPoll() {
  stopBuildStatusPoll();
  const tick = async () => {
    try {
      const r = await fetch(`/pe/report/session/${SESSION_ID}/web_report/build_status`,
                            { cache: "no-store" });
      if (!r.ok) { stopBuildStatusPoll(); return; }   // 구서버(404) 등 — 조용히 물러남
      const s = await r.json();
      if (s.state !== "building") return;   // 웜이거나 아직 미등록 — 다음 tick 에서 재확인
      _buildStatusLive = true;
      const secs = Math.round(s.elapsed || 0);
      setLoadMessage(`서버가 리포트를 계산하고 있습니다… (${secs}초 경과, `
        + `첫 조회만 걸리며 이후에는 즉시 열립니다)`);
    } catch (e) {
      stopBuildStatusPoll();   // 네트워크 단절 등 — 안내 문구는 기존 것 유지
    }
  };
  _buildPoll = setInterval(tick, 2000);
  setTimeout(tick, 1500);   // 첫 확인은 조금 일찍 (웜이면 어차피 곧 hide 된다)
}
// ── 콜드 빌드 202 재시도 ──────────────────────────────────────────────────────
// 콜드 미스 조회는 서버가 빌드를 백그라운드 큐에 걸고 202 {"building":true} 를 즉시
// 돌려준다(요청 스레드가 수 초~수십 초 묶이면 waitress 스레드 8개가 금방 고갈된다).
// 여기서 완료될 때까지 재요청한다. 간격을 조금씩 늘려(1s→5s) 긴 빌드에서 요청이
// 쌓이지 않게 한다. 202 가 아니면(200/4xx/5xx) 그대로 돌려준다.
const BUILD_RETRY = { START_MS: 1000, MAX_MS: 5000, GROWTH: 1.4, TIMEOUT_MS: 15 * 60 * 1000 };
async function retryWhileBuilding(res, refetch) {
  let wait = BUILD_RETRY.START_MS;
  const deadline = Date.now() + BUILD_RETRY.TIMEOUT_MS;
  while (res && res.status === 202 && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, wait));
    wait = Math.min(BUILD_RETRY.MAX_MS, Math.round(wait * BUILD_RETRY.GROWTH));
    res = await refetch();
  }
  return res;
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
    let [res] = await Promise.all([
      prefetched ? prefetched.then(r => r || freshFull()) : freshFull(),
      loadAuth(),   // 편집 권한(MODE) 판정에 필요 — /full 수신과 병렬로
    ]);
    // 콜드 세션은 서버가 빌드를 백그라운드에 걸고 202 를 즉시 준다(요청 스레드를 붙잡지
    // 않기 위함). 빌드가 끝날 때까지 재시도한다 — 대기 안내는 기존 오버레이·build_status
    // 폴링이 그대로 담당한다.
    res = await retryWhileBuilding(res, freshFull);
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
    // Distribution ECDF·Map dies 는 /full 에 실리지 않는 대용량 별도 응답이다. 예전엔
    // 여기서 무조건 둘 다 받았는데, Summary 만 보고 나가는 사용자에게도 전량 다운로드
    // 비용을 물렸다. 지금은 그 데이터를 쓰는 탭(Distribution/Map/Issue Table)에 들어갈 때
    // ensureTabData 가 받는다. 시작 탭이 그중 하나면 여기서 바로 시작한다.
    setLoadProgress(92, "화면 구성 중…");
    ensureTabData(activeTabName());
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

/* 관리자 팝업 메시지 — 검색결과 페이지와 세션 상세가 같은 파일을 공유한다.
 *
 * chat.js 와 같은 방식으로 DOM·스타일을 스스로 주입한다(HTML 쪽 변경은 <script> 한 줄).
 * 관리자가 /pe/admin-<secret>/ 사용자 탭에서 보낸 메시지를 30초마다 확인해 띄우고,
 * 확인 버튼을 누르면 읽음 처리해 그 사람에겐 다시 뜨지 않는다.
 *
 * 저장소가 서버 프로세스 메모리라 서버 재시작 시 미확인 메시지는 사라진다(설계 결정).
 * 폴링은 화면이 보일 때만 돈다 — 백그라운드 탭이 쌓여 서버에 부담을 주지 않게.
 */
(function () {
  "use strict";

  var POLL_MS = 30000;
  var FIRST_DELAY_MS = 2000;   // 페이지 초기 로드와 겹치지 않게 살짝 미룬다
  var API = "/pe/report/api/my_messages";

  // 유휴 일시정지 — 화면을 켜둔 채 자리를 비우면(무입력 4시간) 폴링을 멈추고
  // "클릭하면 재개" 오버레이를 띄운다. 페이지는 그대로 두므로 미저장 편집은 안전하고,
  // 요청이 멎으면 접속자 집계(5분 창)에서도 자연히 빠진다. localStorage 는 검증용 훅.
  var IDLE_LIMIT_MS = Number(localStorage.idle_limit_ms) || 4 * 3600 * 1000;
  var ACTIVITY_MIN_GAP_MS = 30000;   // timestamp 갱신 스로틀 (4h 기준에 오차 30초는 무의미)

  var STYLE = `
#adminMsgMask{position:fixed;inset:0;z-index:3000;display:none;align-items:center;
  justify-content:center;background:rgba(15,23,42,.45);padding:16px}
#adminMsgMask.open{display:flex}
#adminMsgBox{width:440px;max-width:100%;max-height:80vh;display:flex;flex-direction:column;
  border-radius:12px;background:#fff;border:1px solid #d0d7de;
  box-shadow:0 12px 40px rgba(0,0,0,.28);overflow:hidden}
#adminMsgBox .am-head{display:flex;align-items:center;gap:8px;padding:12px 16px;
  border-bottom:1px solid #e5e7eb;font-weight:700;font-size:14px;color:#24292f;background:#f6f8fa}
#adminMsgBox .am-head .am-ico{font-size:17px}
#adminMsgBox.warn .am-head{background:#fff8e6;color:#7a4a00;border-color:#f2d799}
#adminMsgBox .am-body{padding:16px;font-size:13.5px;line-height:1.65;color:#24292f;
  white-space:pre-wrap;word-break:break-word;overflow-y:auto}
#adminMsgBox .am-meta{padding:0 16px 10px;font-size:11.5px;color:#6b7280}
#adminMsgBox .am-foot{display:flex;align-items:center;gap:8px;padding:10px 16px;
  border-top:1px solid #e5e7eb;background:#f6f8fa}
#adminMsgBox .am-foot .am-rest{flex:1;font-size:11.5px;color:#6b7280}
#adminMsgBox .am-foot button{padding:7px 18px;border:none;border-radius:6px;
  background:#2563eb;color:#fff;cursor:pointer;font-size:13px;font-weight:600}
#adminMsgBox .am-foot button:hover{background:#1d4ed8}
#adminMsgBox .am-foot button:disabled{background:#9ca3af;cursor:default}
html[data-theme="dark"] #adminMsgBox{background:#161b22;border-color:#30363d}
html[data-theme="dark"] #adminMsgBox .am-head{background:#0d1117;border-color:#30363d;color:#c9d1d9}
html[data-theme="dark"] #adminMsgBox.warn .am-head{background:#3a2d10;color:#f0c674;border-color:#5c4a1d}
html[data-theme="dark"] #adminMsgBox .am-body{color:#c9d1d9}
html[data-theme="dark"] #adminMsgBox .am-foot{background:#0d1117;border-color:#30363d}
#idleMask{position:fixed;inset:0;z-index:3500;display:none;align-items:center;
  justify-content:center;background:rgba(15,23,42,.55);padding:16px;cursor:pointer}
#idleMask.open{display:flex}
#idleMask .idle-card{max-width:360px;border-radius:12px;background:#fff;
  border:1px solid #d0d7de;box-shadow:0 12px 40px rgba(0,0,0,.28);
  padding:22px 26px;text-align:center;font-size:13.5px;line-height:1.7;color:#24292f}
#idleMask .idle-card .idle-ico{font-size:26px;margin-bottom:6px}
#idleMask .idle-card .idle-sub{font-size:11.5px;color:#6b7280;margin-top:4px}
html[data-theme="dark"] #idleMask .idle-card{background:#161b22;border-color:#30363d;color:#c9d1d9}
html[data-theme="dark"] #idleMask .idle-card .idle-sub{color:#8b949e}
`;

  var queue = [];        // 아직 안 띄운 메시지
  var showing = null;    // 현재 띄운 메시지
  var seen = {};         // 이번 페이지에서 이미 큐에 넣은 id (중복 폴링 방지)
  var mask = null, box = null, headEl = null, bodyEl = null, metaEl = null,
      restEl = null, okBtn = null;

  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)report_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function fmtTime(sec) {
    if (!sec) return "";
    var d = new Date(sec * 1000);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
           " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function build() {
    if (mask) return;
    var st = document.createElement("style");
    st.textContent = STYLE;
    document.head.appendChild(st);

    mask = document.createElement("div");
    mask.id = "adminMsgMask";
    mask.innerHTML =
      '<div id="adminMsgBox" role="dialog" aria-modal="true">' +
        '<div class="am-head"><span class="am-ico">📢</span><span id="adminMsgTitle"></span></div>' +
        '<div class="am-body" id="adminMsgBody"></div>' +
        '<div class="am-meta" id="adminMsgMeta"></div>' +
        '<div class="am-foot"><span class="am-rest" id="adminMsgRest"></span>' +
          '<button type="button" id="adminMsgOk">확인</button></div>' +
      '</div>';
    document.body.appendChild(mask);

    box = document.getElementById("adminMsgBox");
    headEl = document.getElementById("adminMsgTitle");
    bodyEl = document.getElementById("adminMsgBody");
    metaEl = document.getElementById("adminMsgMeta");
    restEl = document.getElementById("adminMsgRest");
    okBtn = document.getElementById("adminMsgOk");
    okBtn.addEventListener("click", ack);
  }

  function showNext() {
    if (showing || !queue.length) return;
    build();
    showing = queue.shift();
    box.className = showing.level === "warn" ? "warn" : "";
    box.querySelector(".am-ico").textContent = showing.level === "warn" ? "⚠️" : "📢";
    headEl.textContent = showing.title || "관리자 공지";
    bodyEl.textContent = showing.body || "";
    metaEl.textContent = fmtTime(showing.created_at);
    restEl.textContent = queue.length ? "다음 메시지 " + queue.length + "건" : "";
    okBtn.disabled = false;
    mask.classList.add("open");
    okBtn.focus();
  }

  function ack() {
    if (!showing) return;
    var id = showing.id;
    okBtn.disabled = true;
    // 실패해도 화면은 닫는다 — 서버가 못 받으면 다음 폴링에 다시 뜬다(유실보다 중복이 낫다).
    fetch(API + "/" + id + "/ack", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
    }).catch(function () {}).then(function () {
      mask.classList.remove("open");
      showing = null;
      showNext();
    });
  }

  // ── 유휴 일시정지 ──────────────────────────────────────────────────────────
  var lastActivity = Date.now();
  var idle = false;
  var idleMask = null;

  function touch() {
    var now = Date.now();
    if (now - lastActivity < ACTIVITY_MIN_GAP_MS) return;
    lastActivity = now;
  }

  function buildIdleMask() {
    if (idleMask) return;
    build();   // STYLE 주입 보장 (adminMsg DOM 과 같은 <style> 에 있다)
    idleMask = document.createElement("div");
    idleMask.id = "idleMask";
    idleMask.innerHTML =
      '<div class="idle-card"><div class="idle-ico">⏸️</div>' +
        '유휴 상태로 일시정지되었습니다.<br>화면을 클릭하면 재개됩니다.' +
        '<div class="idle-sub">작성 중이던 내용은 그대로 유지됩니다.</div></div>';
    idleMask.addEventListener("click", resumeIdle);
    document.body.appendChild(idleMask);
  }

  function enterIdle() {
    if (idle) return;
    idle = true;
    // 혹시 아직 돌고 있는 빌드/AI 폴링도 함께 멈춘다(applyAdminStop 과 같은 패턴 —
    // 검색결과 페이지에는 이 함수들이 없으므로 존재 여부를 보고 부른다).
    try { if (typeof stopBuildStatusPoll === "function") stopBuildStatusPoll(); } catch (e) {}
    try { if (typeof stopAiPendingPoll === "function") stopAiPendingPoll(); } catch (e) {}
    buildIdleMask();
    idleMask.classList.add("open");
  }

  // location.reload 는 쓰지 않는다 — applyAdminStop 과 같은 이유(미저장 입력 보호).
  // AI 폴링도 재시작하지 않는다(데드라인 20분이라 4시간 유휴 뒤엔 이미 만료).
  function resumeIdle() {
    if (!idle) return;
    idle = false;
    lastActivity = Date.now();
    if (idleMask) idleMask.classList.remove("open");
    poll();
  }

  // 관리자가 이 사용자의 대기를 끊었다 — 콜드 빌드 폴링을 멈추고 사실대로 알린다.
  // 강제 새로고침(location.reload)은 쓰지 않는다: 편집 중이면 leave_guard 의
  // beforeunload 확인창이 떠 사용자가 취소할 수 있고, 미저장 입력을 잃을 수 있다.
  // 검색결과 페이지에는 폴링 함수가 없으므로 존재 여부를 보고 부른다.
  function applyAdminStop(stop) {
    try { if (typeof stopBuildStatusPoll === "function") stopBuildStatusPoll(); } catch (e) {}
    try { if (typeof stopAiPendingPoll === "function") stopAiPendingPoll(); } catch (e) {}
    try { if (typeof hideLoadOverlay === "function") hideLoadOverlay(); } catch (e) {}
    var box = document.getElementById("errorBox");
    if (box) {
      box.style.display = "";
      box.textContent = "관리자가 이 세션의 계산 대기를 중단했습니다."
        + (stop.reason ? " (" + stop.reason + ")" : "")
        + " 필요하면 잠시 후 새로고침해 주세요.";
    }
  }

  function poll() {
    if (document.hidden) return;
    if (idle) return;
    if (Date.now() - lastActivity > IDLE_LIMIT_MS) { enterIdle(); return; }
    fetch(API, { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        if (data.stop) applyAdminStop(data.stop);
        if (!data.messages || !data.messages.length) return;
        data.messages.forEach(function (m) {
          if (seen[m.id]) return;
          seen[m.id] = 1;
          queue.push(m);
        });
        if (showing) restEl.textContent = queue.length ? "다음 메시지 " + queue.length + "건" : "";
        showNext();
      })
      .catch(function () {});
  }

  function start() {
    setTimeout(poll, FIRST_DELAY_MS);
    setInterval(poll, POLL_MS);
    // 다른 탭을 보다 돌아왔을 때 30초를 더 기다리지 않게 즉시 한 번 확인한다.
    // 탭 복귀는 그 자체가 의도적 활동이므로 유휴였다면 오버레이 없이 바로 재개한다.
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) return;
      lastActivity = Date.now();
      if (idle) resumeIdle(); else poll();
    });
    // 뒤로가기(BFCache) 복원도 활동으로 본다.
    window.addEventListener("pageshow", function () {
      lastActivity = Date.now();
      if (idle) resumeIdle();
    });
    // 유휴 판정용 입력 감지. Note 탭 Luckysheet iframe 내부 입력은 부모로 버블되지
    // 않지만 편집 시 note:* postMessage 가 window 로 오므로 "message" 를 활동 근사로 쓴다.
    ["mousemove", "mousedown", "keydown", "wheel", "touchstart", "scroll", "message"]
      .forEach(function (ev) {
        window.addEventListener(ev, touch, { capture: true, passive: true });
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

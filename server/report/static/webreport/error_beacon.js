// ── 브라우저 에러 beacon ───────────────────────────────────────────────────────
// JS 에러/unhandled rejection 을 서버(POST /pe/report/api/client_error)로 전송해
// admin console log·User Action Monitoring 에서 VOC 재현 정보를 확보한다.
// 다른 모듈보다 먼저 로드돼야 이후 스크립트의 로드/초기화 에러도 잡는다 —
// 의존 없는 단독 IIFE (core.js 의 SESSION_ID 등은 에러 발생 시점에 typeof 로만 참조).
(function () {
  "use strict";
  const MAX_PER_PAGE = 10;          // 페이지당 전송 상한 (루프 에러 폭주 방지)
  const sent = new Set();           // 동일 message|source|line 은 페이지당 1회
  let count = 0;

  function currentContext() {
    const ctx = { url: String(location.href).slice(0, 500) };
    try {
      // core.js 가 아직 안 로드됐거나 검색결과 페이지면 undefined — 생략
      if (typeof SESSION_ID !== "undefined" && SESSION_ID) ctx.session_id = String(SESSION_ID);
    } catch (e) { /* no-op */ }
    try {
      const tab = document.querySelector(".tab.active");
      if (tab && tab.dataset && tab.dataset.tab) ctx.tab = tab.dataset.tab;
    } catch (e) { /* no-op */ }
    return ctx;
  }

  function send(entry) {
    const key = `${entry.message}|${entry.source || ""}|${entry.line || 0}`;
    if (sent.has(key) || count >= MAX_PER_PAGE) return;
    sent.add(key);
    count += 1;
    const body = JSON.stringify(Object.assign(entry, currentContext()));
    try {
      // sendBeacon 은 페이지 이탈 중에도 전송 보장. 실패(큐 가득 등) 시 keepalive fetch 폴백.
      if (navigator.sendBeacon &&
          navigator.sendBeacon("/pe/report/api/client_error",
                               new Blob([body], { type: "application/json" }))) return;
    } catch (e) { /* fall through */ }
    try {
      fetch("/pe/report/api/client_error", {
        method: "POST", keepalive: true,
        headers: { "Content-Type": "application/json" }, body,
      }).catch(() => {});
    } catch (e) { /* 전송 실패는 무시 — beacon 자체가 에러를 만들면 안 됨 */ }
  }

  function trimStack(err) {
    try {
      const s = err && err.stack ? String(err.stack) : "";
      return s ? s.slice(0, 2000) : undefined;
    } catch (e) { return undefined; }
  }

  window.addEventListener("error", (ev) => {
    // 리소스 로드 실패(img/script 404 등)는 message 가 없는 Event — 스크립트 에러만 전송
    if (!ev || typeof ev.message !== "string" || !ev.message) return;
    send({
      kind: "error",
      message: String(ev.message).slice(0, 500),
      source: ev.filename ? String(ev.filename).slice(0, 300) : undefined,
      line: ev.lineno || undefined,
      col: ev.colno || undefined,
      stack: trimStack(ev.error),
    });
  });

  window.addEventListener("unhandledrejection", (ev) => {
    const r = ev && ev.reason;
    const msg = (r && r.message) ? String(r.message) : String(r);
    send({
      kind: "unhandledrejection",
      message: msg.slice(0, 500),
      stack: trimStack(r),
    });
  });

  // 이미 catch 된 실패(fetch 5xx·콜드 빌드 폴링 타임아웃 등)를 명시적으로 보고하는 창구.
  // window.onerror 는 try/catch 안의 실패를 못 본다 — 정작 사용자가 "안 열린다"고
  // 신고하는 경우가 대부분 그쪽이라, 화면에 에러를 띄우는 자리에서 직접 부른다.
  window.reportClientError = function (entry) {
    try {
      if (entry && typeof entry === "object") send(entry);
    } catch (e) { /* no-op */ }
  };
})();

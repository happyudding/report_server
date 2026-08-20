/* 구버전 Honey 사용자 안내 — 랜딩(/pe)과 검색결과(/pe/report/)가 공유한다.
 *
 * 구버전 클라는 이미 배포된 exe 라 코드를 고칠 수 없다. 대신 그 사람이 내장 브라우저로
 * 서버 페이지를 열 때 **서버 쪽 화면**에서 안내를 띄운다.
 *
 * 판정: User-Agent 에 `HoneyUser/` 는 있는데 `HoneyVer/` 가 없거나 min_version 미만.
 *   HoneyVer 토큰은 3.2.0 부터 붙기 시작했으므로(client/embedded_browser.py),
 *   "HoneyUser 있음 + HoneyVer 없음" 이 곧 3.2.0 미만이다. 일반 브라우저는
 *   HoneyUser 자체가 없어 대상에서 빠진다 — Honey 를 안 쓰는 사람에게 뜨지 않는다.
 *
 * 안내문·기준 버전은 전부 서버 파일에서 온다(GET /honey/client_notice):
 *   본문 = releases/old_client_notice.txt (첫 줄 제목, 나머지 본문)
 *   기준 = releases/version.json 의 min_version — **비어 있으면 아무것도 띄우지 않는다**.
 *
 * 하루 1회만 뜬다(localStorage). 매번 띄우면 무시당하고, 한 번만 띄우면 잊혀진다.
 * 기준 버전이 올라가면 그날 이미 봤어도 다시 뜬다(키에 min_version 을 함께 넣는다).
 */
(function () {
  "use strict";

  var API = "/honey/client_notice";
  var SEEN_KEY = "honey_old_client_notice";
  var DELAY_MS = 1200;   // 첫 렌더와 겹치지 않게 살짝 미룬다

  var STYLE = `
#oldVerMask{position:fixed;inset:0;z-index:2900;display:none;align-items:center;
  justify-content:center;background:rgba(15,23,42,.5);padding:16px}
#oldVerMask.open{display:flex}
#oldVerBox{width:460px;max-width:100%;max-height:80vh;display:flex;flex-direction:column;
  border-radius:12px;background:#fff;border:1px solid #f2d799;
  box-shadow:0 12px 40px rgba(0,0,0,.3);overflow:hidden}
#oldVerBox .ov-head{display:flex;align-items:center;gap:8px;padding:12px 16px;
  border-bottom:1px solid #f2d799;font-weight:700;font-size:14.5px;
  background:#fff8e6;color:#7a4a00}
#oldVerBox .ov-head .ov-ico{font-size:18px}
#oldVerBox .ov-body{padding:16px;font-size:13.5px;line-height:1.7;color:#24292f;
  white-space:pre-wrap;word-break:break-word;overflow-y:auto}
#oldVerBox .ov-ver{padding:0 16px 12px;font-size:11.5px;color:#6b7280}
#oldVerBox .ov-foot{display:flex;align-items:center;justify-content:flex-end;gap:8px;
  padding:10px 16px;border-top:1px solid #e5e7eb;background:#f6f8fa}
#oldVerBox .ov-foot a,#oldVerBox .ov-foot button{padding:8px 18px;border:none;
  border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none}
#oldVerBox .ov-foot .ov-dl{background:#F0A62A;color:#3b2600}
#oldVerBox .ov-foot .ov-dl:hover{background:#d8901c}
#oldVerBox .ov-foot .ov-later{background:transparent;color:#6b7280}
#oldVerBox .ov-foot .ov-later:hover{color:#24292f;text-decoration:underline}
html[data-theme="dark"] #oldVerBox{background:#161b22;border-color:#5c4a1d}
html[data-theme="dark"] #oldVerBox .ov-head{background:#3a2d10;color:#f0c674;border-color:#5c4a1d}
html[data-theme="dark"] #oldVerBox .ov-body{color:#c9d1d9}
html[data-theme="dark"] #oldVerBox .ov-foot{background:#0d1117;border-color:#30363d}
`;

  /* UA 의 HoneyVer 토큰 — 서버 auth_identity._HONEY_VER_RE 와 같은 규칙. */
  function honeyVersion() {
    var m = /HoneyVer\/([0-9][0-9A-Za-z.\-_]*)/.exec(navigator.userAgent || "");
    return m ? m[1] : "";
  }

  function isHoney() {
    return (navigator.userAgent || "").indexOf("HoneyUser/") >= 0;
  }

  /* a < b 인가. client/transport/app_update.is_newer 와 같은 판정(숫자 튜플 비교,
     자릿수가 다르면 짧은 쪽이 작다). 숫자가 아닌 조각이 있으면 문자열 비교로 폴백. */
  function isOlder(a, b) {
    var pa = String(a).split("."), pb = String(b).split(".");
    for (var i = 0; i < Math.max(pa.length, pb.length); i++) {
      if (i >= pa.length) return true;    // "3.2" < "3.2.0"
      if (i >= pb.length) return false;
      var x = Number(pa[i]), y = Number(pb[i]);
      if (!isFinite(x) || !isFinite(y)) return String(a) !== String(b) && String(a) < String(b);
      if (x !== y) return x < y;
    }
    return false;
  }

  function today() {
    var d = new Date();
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
  }

  function alreadySeenToday(minVer) {
    try {
      return localStorage.getItem(SEEN_KEY) === minVer + "|" + today();
    } catch (e) { return false; }
  }

  function markSeen(minVer) {
    try { localStorage.setItem(SEEN_KEY, minVer + "|" + today()); } catch (e) {}
  }

  function show(notice, myVer) {
    var st = document.createElement("style");
    st.textContent = STYLE;
    document.head.appendChild(st);

    var mask = document.createElement("div");
    mask.id = "oldVerMask";
    mask.innerHTML =
      '<div id="oldVerBox" role="dialog" aria-modal="true">' +
        '<div class="ov-head"><span class="ov-ico">⚠️</span><span id="oldVerTitle"></span></div>' +
        '<div class="ov-body" id="oldVerBody"></div>' +
        '<div class="ov-ver" id="oldVerMeta"></div>' +
        '<div class="ov-foot">' +
          '<button type="button" class="ov-later" id="oldVerLater">나중에</button>' +
          '<a class="ov-dl" id="oldVerDl" href="/honey/download">Honey 새로 받기</a>' +
        '</div>' +
      '</div>';
    document.body.appendChild(mask);

    document.getElementById("oldVerTitle").textContent = notice.title || "";
    document.getElementById("oldVerBody").textContent = notice.body || "";
    document.getElementById("oldVerMeta").textContent =
      "현재 버전 " + (myVer || "3.2.0 이전") +
      (notice.version ? " · 최신 버전 " + notice.version : "");

    var dl = document.getElementById("oldVerDl");
    if (notice.file) dl.setAttribute("download", notice.file);

    function close() { mask.classList.remove("open"); }
    document.getElementById("oldVerLater").addEventListener("click", close);
    // 다운로드는 새 창 없이 시작되므로(내장 브라우저 다운로드 핸들러) 창만 닫아 준다.
    dl.addEventListener("click", function () { setTimeout(close, 300); });

    mask.classList.add("open");
    markSeen(notice.min_version);
  }

  function start() {
    if (!isHoney()) return;                 // 일반 브라우저 — 대상 아님
    fetch(API, { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (n) {
        if (!n || !n.min_version) return;   // 기준 미설정 = 기능 꺼짐
        var myVer = honeyVersion();
        // HoneyVer 토큰 자체가 없으면 3.2.0 미만이 확정이다(토큰은 3.2.0부터 붙는다).
        if (myVer && !isOlder(myVer, n.min_version)) return;
        if (alreadySeenToday(n.min_version)) return;
        show(n, myVer);
      })
      .catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { setTimeout(start, DELAY_MS); });
  } else {
    setTimeout(start, DELAY_MS);
  }
})();

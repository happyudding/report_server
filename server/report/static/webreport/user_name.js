// 사용자 실명 표기 + 이름 입력창 — 검색결과 홈 · /pe 랜딩 · 세션 상세가 공유한다.
//
// 이 서버는 사람을 소문자 singleID 로만 식별한다. 권한 부여 창·감사로그·업로더 칸이
// 전부 ID 라 "누구인지" 를 알 수 없어서, 실명을 받아 **이름(ID)** 로 표기한다.
// 이름은 표시 전용이다 — 신원 판단·접근제어는 서버에서 계속 user_id 로만 한다.
//
// 모달은 honey_hint.js 와 같은 방식으로 마크업을 주입한다: 세 페이지가 모두
// .modal-overlay / .modal-box / .modal-btn + .show 토글 규약을 공유하므로 CSS 는
// 페이지 것을 그대로 쓰고, 페이지마다 다른 부분(입력칸·에러줄)만 인라인 style 로 채운다.
(function (global) {
  "use strict";

  var MODAL_ID = "userNameModal";
  var MAX_LEN = 10;                    // 서버 _DISPLAY_NAME_RE 와 같은 상한
  // 실명은 **완성형 한글 2~10자만** 받는다 (서버 report/security.py `_DISPLAY_NAME_RE`
  // 와 같은 규칙). 여기 검증은 사용자에게 즉시 알려주기 위한 것이고, 강제는 서버가 한다.
  var NAME_RE = /^[가-힣]{2,10}$/;
  var NAME_HELP = "이름은 한글 2~10자로 입력해주세요. (예: 홍길동)";
  var _open = false;                   // 같은 화면에서 두 번 겹쳐 띄우지 않기

  // 규칙에 맞는 이름인지. 규칙이 생기기 전에 저장된 영문 이름은 여기서 false 가 되어
  // 다음 접속에 입력창이 다시 뜬다(값을 지우지는 않는다 — 고칠 때까지 화면에는 그대로).
  function isValidName(name) {
    return NAME_RE.test(String(name == null ? "" : name).trim());
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)report_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  // ── 표기 (모든 화면의 단일 출처) ──
  // 이름이 없으면 ID 만 — 아직 이름을 안 넣은 사람을 빈칸으로 만들지 않는다.
  function fmt(uid, name) {
    uid = String(uid || "");
    name = String(name || "").trim();
    if (!uid) return name;
    return name ? name + "(" + uid + ")" : uid;
  }

  // 신원 키 정규화 — 서버 identity_norm.normalize_uid 와 같은 규칙
  // (마지막 백슬래시 뒤 → trim → 소문자). 'SECDS\Chumji.Kim' 과 'chumji.kim' 이
  // 화면에서 다른 사람으로 보이지 않게, ID 를 그리는 자리는 전부 이걸 통과시킨다.
  function uid(value) {
    return String(value == null ? "" : value).split("\\").pop().trim().toLowerCase();
  }

  // 목록 응답의 names 맵({uid: 이름})에서 찾아 표기.
  function fromMap(who, names) {
    var tail = uid(who);
    return fmt(tail, (names || {})[tail] || "");
  }

  // ── 이름 입력창 ──
  function buildModal() {
    if (document.getElementById(MODAL_ID)) return;
    var wrap = document.createElement("div");
    wrap.className = "modal-overlay";
    wrap.id = MODAL_ID;
    wrap.innerHTML =
      '<div class="modal-box" style="max-width:400px;">' +
        '<p class="modal-title">이름을 알려주세요</p>' +
        '<p style="font-size:14px; color:#888; margin:0 0 12px; line-height:1.6;">' +
          '권한 부여 창·업로더 표시가 ID 대신 <b>이름(ID)</b> 로 보이게 됩니다.<br>' +
          '이름은 <b>한글 2~10자</b>로만 입력할 수 있습니다.</p>' +
        '<div style="font-size:13px; color:#888; margin:0 0 6px;">계정 ' +
          '<b id="userNameWho" style="color:inherit;"></b></div>' +
        '<input id="userNameInput" type="text" maxlength="' + MAX_LEN + '" ' +
          'placeholder="예) 홍길동" autocomplete="name" ' +
          'style="width:100%; box-sizing:border-box; padding:8px 10px; font-size:15px; ' +
          'background:transparent; color:inherit; border:1px solid #8886; border-radius:6px;">' +
        '<p id="userNameErr" style="margin:8px 0 0; font-size:13px; color:#f87171; ' +
          'display:none;"></p>' +
        '<div class="modal-actions">' +
          '<button class="modal-btn" id="userNameLater" type="button">나중에</button>' +
          '<button class="modal-btn primary" id="userNameSave" type="button">저장</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(wrap);
  }

  function showErr(msg) {
    var el = document.getElementById("userNameErr");
    if (!el) return;
    el.textContent = msg || "";
    el.style.display = msg ? "" : "none";
  }

  function close() {
    var el = document.getElementById(MODAL_ID);
    if (el) el.classList.remove("show");
    _open = false;
  }

  /**
   * 이름이 없으면 입력창을 띄운다.
   * viewer: {user_id, display_name} (history / auth_me / my_access / landing 공통 규약)
   * opts.ensureCsrf : () => Promise — 랜딩처럼 CSRF 쿠키가 아직 없는 페이지용
   * opts.onSaved(name) : 저장 성공 콜백 (칩 갱신·캐시 무효화 등)
   * opts.force : 이미 이름이 있어도 띄운다 (사용자가 직접 '이름 변경' 을 누른 경우)
   * 반환 Promise 는 **모달이 끝났을 때**(저장/나중에/안 띄움) resolve 한다 —
   * 호출부가 뒤이은 다른 모달(비밀번호 설정)을 겹치지 않게 이어 띄우기 위한 것.
   *
   * 한글 규칙(2026-08-14)이 생기기 전에 저장된 영문 이름도 '이름 없음' 과 같이 취급해
   * 다시 띄운다 — 저장된 값을 지우지는 않으므로, 고치기 전까지 화면 표기는 그대로다.
   */
  function promptIfMissing(viewer, opts) {
    opts = opts || {};
    var uid = (viewer || {}).user_id || "";
    var name = ((viewer || {}).display_name || "").trim();
    var needsFix = !!name && !isValidName(name);   // 규칙 이전에 저장된 이름
    // 신원이 없는 일반 브라우저(익명 열람자)에게는 절대 띄우지 않는다.
    if (!uid || _open || (name && !needsFix && !opts.force)) return Promise.resolve(false);

    _open = true;
    buildModal();
    var wrap = document.getElementById(MODAL_ID);
    var input = document.getElementById("userNameInput");
    document.getElementById("userNameWho").textContent = "SECDS\\" + uid;
    // 고쳐야 하는 이름은 입력칸에 채워 둔다 — 지우고 새로 치게 하면 번거롭다.
    input.value = (opts.force || needsFix) ? name : "";
    document.getElementById("userNameLater").textContent = opts.force ? "취소" : "나중에";
    showErr(needsFix ? NAME_HELP : "");
    wrap.classList.add("show");
    setTimeout(function () { try { input.focus(); } catch (e) {} }, 50);

    return new Promise(function (resolve) {
      function done(saved) { close(); cleanup(); resolve(saved); }

      function save() {
        var val = (input.value || "").trim();
        if (!val) { showErr("이름을 입력해주세요."); input.focus(); return; }
        if (!isValidName(val)) { showErr(NAME_HELP); input.focus(); return; }
        showErr("");
        var ready = opts.ensureCsrf ? opts.ensureCsrf() : Promise.resolve();
        Promise.resolve(ready).then(function () {
          return fetch("/pe/report/api/auth/display_name", {
            method: "POST",
            headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken()},
            body: JSON.stringify({name: val}),
          });
        }).then(function (res) {
          return res.json().catch(function () { return {}; }).then(function (j) {
            if (!res.ok) { showErr(j.error || "저장에 실패했습니다."); return; }
            if (opts.onSaved) { try { opts.onSaved(j.display_name || val); } catch (e) {} }
            done(true);
          });
        }).catch(function (e) { showErr("저장 실패: " + e.message); });
      }

      function onKey(e) {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        else if (e.key === "Escape") { done(false); }
      }
      function onOutside(e) { if (e.target === wrap) done(false); }
      function cleanup() {
        input.removeEventListener("keydown", onKey);
        wrap.removeEventListener("click", onOutside);
        document.getElementById("userNameSave").removeEventListener("click", save);
        document.getElementById("userNameLater").removeEventListener("click", later);
      }
      // '나중에' 로 닫아도 억제 기록을 남기지 않는다 — 다음 접속에 다시 뜬다(수집 정책).
      function later() { done(false); }

      input.addEventListener("keydown", onKey);
      wrap.addEventListener("click", onOutside);
      document.getElementById("userNameSave").addEventListener("click", save);
      document.getElementById("userNameLater").addEventListener("click", later);
    });
  }

  global.UserName = {fmt: fmt, uid: uid, fromMap: fromMap, promptIfMissing: promptIfMissing,
                     close: close, MAX_LEN: MAX_LEN,
                     isValidName: isValidName, NAME_HELP: NAME_HELP};
})(window);

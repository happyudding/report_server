// Honey 전용 기능 안내 — 일반 브라우저(웹 로그인 포함)에서만 노출된다.
//
// 웹 로그인으로 편집·삭제 권한은 Honey 와 동등해지지만, 로컬 MS Excel(COM)·로컬
// 파일시스템·사내 D1 저장소가 필요한 기능은 브라우저에서 구현이 불가능하다.
// 그런 기능은 웹에 UI 자체가 없어 "왜 없는지" 알 길이 없으므로, 목록과 이유를
// 한 곳에 모아 보여주고 Honey 내려받기로 유도한다.
//
// 검색결과 페이지(report_analysis_index.html)와 세션 상세(report_view.html)가
// 공유한다. init(source, containerSelector) 를 각 페이지가 신원 확인 후 호출.
(function (global) {
  "use strict";

  var ITEMS = [
    ["Excel Upload", "로컬 xlsx 를 열어 파싱해야 해서 브라우저에서 불가"],
    ["새 리포트 생성", "MS Excel(COM) 로 원본을 읽어 분석 — 로컬 앱 필요"],
    ["Rawdata Excel 편집", "Excel 로 내려받아 편집 후 되돌리는 왕복 — 로컬 Excel 필요"],
    ["Excel Download (전체본)", "차트 PNG·Map 을 로컬에서 생성해 합침. 웹은 Yield/CPK 시트만 제공"],
    ["로컬 파일 열기", "브라우저는 PC 파일시스템에 접근할 수 없음"],
    ["D1(Dolphin) 불러오기", "사내 D1 저장소 접근 — 클라이언트 전용"],
    ["Options (색·기본값)", "클라이언트 로컬 설정"],
  ];

  var MODAL_ID = "honeyHintModal";

  function buildModal() {
    if (document.getElementById(MODAL_ID)) return;
    var rows = ITEMS.map(function (it) {
      return '<li style="margin:0 0 8px;"><b>' + it[0] + '</b>' +
             '<div style="color:#888; font-size:15px;">' + it[1] + '</div></li>';
    }).join("");

    var wrap = document.createElement("div");
    wrap.className = "modal-overlay";
    wrap.id = MODAL_ID;
    wrap.innerHTML =
      '<div class="modal-box" style="max-width:520px;">' +
        '<p class="modal-title">Honey 앱에서만 되는 기능</p>' +
        '<p style="font-size:16px; color:#888; margin:0 0 10px;">' +
          '조회와 편집은 웹에서 그대로 됩니다. 아래 기능만 로컬 Excel·파일 접근이 ' +
          '필요해 Honey 앱에서 실행해야 합니다.</p>' +
        '<ul style="margin:0; padding-left:18px; font-size:16px;">' + rows + '</ul>' +
        '<div class="modal-actions">' +
          '<button class="modal-btn" id="honeyHintClose">닫기</button>' +
          '<a class="modal-btn" id="honeyHintDl" href="/honey/download" ' +
             'style="background:#4a90e2; color:#fff; border-color:#4a90e2; ' +
             'text-decoration:none; display:inline-block;">⬇ Honey 내려받기</a>' +
        '</div>' +
      '</div>';
    document.body.appendChild(wrap);

    document.getElementById("honeyHintClose").addEventListener("click", close);
    wrap.addEventListener("click", function (e) { if (e.target === wrap) close(); });
  }

  function open() { buildModal(); document.getElementById(MODAL_ID).classList.add("show"); }
  function close() {
    var el = document.getElementById(MODAL_ID);
    if (el) el.classList.remove("show");
  }

  // source: auth_identity.identity_source() 값. "honey" 면 안내가 불필요하다.
  // label 을 생략하면 전체 문구, 좁은 툴바에서는 "🍯" 처럼 짧게 넘긴다.
  function init(source, containerSelector, btnClass, label) {
    if (source === "honey") return;
    var host = document.querySelector(containerSelector);
    if (!host || document.getElementById("honeyHintBtn")) return;
    var btn = document.createElement("button");
    btn.id = "honeyHintBtn";
    btn.className = btnClass || "";
    btn.textContent = label || "🍯 Honey 전용 기능";
    btn.title = "웹에서는 쓸 수 없는 기능 안내";
    btn.addEventListener("click", open);
    host.appendChild(btn);
  }

  global.HoneyHint = { init: init, open: open, close: close };
})(window);

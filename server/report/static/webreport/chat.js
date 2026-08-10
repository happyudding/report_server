/* 챗봇 위젯 (관리자 전용) — 검색결과 페이지와 세션 상세가 같은 파일을 공유한다.
 *
 * 버튼·패널 DOM 과 스타일을 이 파일이 스스로 주입한다. 두 페이지에 마크업을 각각
 * 심으면 두 벌을 계속 맞춰야 하므로, HTML 쪽 변경은 <script> 한 줄로 끝낸다.
 *
 * 노출은 서버가 내려준 is_master 로만 결정한다(master 쿠키는 httponly 라 JS 가 못 읽는다).
 * 다만 이건 편의이고 실효 경계는 서버 라우트의 404 다 — UI 숨김은 보안이 아니다.
 *
 * 세션 상세에서는 enable(SESSION_ID) 로 켜서 질문에 "이 세션" 컨텍스트를 붙이고,
 * 서버가 같은 세션 대상 이동을 action(open_item_detail/open_map/open_tab)으로 내려주면
 * 페이지를 떠나지 않고 그 자리에서 이동한다. 다른 세션이면 딥링크 url 로 온다.
 */
(function () {
  "use strict";

  var STYLE = `
#chatFab{position:fixed;right:16px;bottom:76px;z-index:950;width:48px;height:48px;
  border-radius:50%;border:none;cursor:pointer;font-size:22px;line-height:1;
  background:#2563eb;color:#fff;box-shadow:0 2px 10px rgba(0,0,0,.25)}
#chatFab:hover{background:#1d4ed8}
#chatPanel{position:fixed;right:16px;bottom:132px;z-index:950;width:380px;max-width:calc(100vw - 32px);
  max-height:min(60vh,560px);display:none;flex-direction:column;border-radius:10px;
  background:#fff;border:1px solid #d0d7de;box-shadow:0 6px 24px rgba(0,0,0,.18);overflow:hidden}
#chatPanel.open{display:flex}
#chatPanel .chat-head{display:flex;align-items:center;justify-content:space-between;
  padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:600;font-size:13px;background:#f6f8fa}
#chatPanel .chat-head button{border:none;background:none;cursor:pointer;font-size:16px;color:#57606a}
#chatLog{flex:1;overflow-y:auto;padding:10px 12px;font-size:12.5px;line-height:1.55}
#chatLog .msg{margin-bottom:10px}
#chatLog .msg.me{text-align:right}
#chatLog .bubble{display:inline-block;max-width:95%;padding:6px 9px;border-radius:8px;
  white-space:pre-wrap;word-break:break-word;text-align:left}
#chatLog .me .bubble{background:#2563eb;color:#fff}
#chatLog .bot .bubble{background:#f1f3f5;color:#24292f}
#chatLog .err .bubble{background:#ffebe9;color:#82071e}
#chatLog .acts{margin-top:5px;display:flex;flex-wrap:wrap;gap:5px}
#chatLog .acts a,#chatLog .acts button{font-size:11.5px;padding:3px 8px;border-radius:12px;
  border:1px solid #d0d7de;background:#fff;color:#0969da;cursor:pointer;text-decoration:none}
#chatLog .acts button.choice{color:#24292f}
#chatLog .acts a:hover,#chatLog .acts button:hover{background:#f3f4f6}
#chatLog .errdet{margin-top:5px;font-size:11px}
#chatLog .errdet summary{cursor:pointer;color:#82071e}
#chatLog .errdet pre{margin:4px 0 0;padding:6px;background:#fff;border:1px solid #ffcecb;
  border-radius:5px;max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-all}
#chatForm{display:flex;gap:6px;padding:8px;border-top:1px solid #e5e7eb}
#chatForm input{flex:1;padding:6px 8px;border:1px solid #d0d7de;border-radius:6px;font-size:12.5px;
  background:#fff;color:#24292f}
#chatForm button{padding:6px 12px;border:none;border-radius:6px;background:#2563eb;color:#fff;
  cursor:pointer;font-size:12.5px}
#chatForm button:disabled{background:#9ca3af;cursor:default}
html[data-theme="dark"] #chatPanel{background:#161b22;border-color:#30363d}
html[data-theme="dark"] #chatPanel .chat-head{background:#0d1117;border-color:#30363d;color:#c9d1d9}
html[data-theme="dark"] #chatPanel .chat-head button{color:#8b949e}
html[data-theme="dark"] #chatLog .bot .bubble{background:#21262d;color:#c9d1d9}
html[data-theme="dark"] #chatLog .err .bubble{background:#3d1d1d;color:#ffa198}
html[data-theme="dark"] #chatLog .acts a,html[data-theme="dark"] #chatLog .acts button{
  background:#21262d;border-color:#30363d;color:#58a6ff}
html[data-theme="dark"] #chatLog .acts button.choice{color:#c9d1d9}
html[data-theme="dark"] #chatForm{border-color:#30363d}
html[data-theme="dark"] #chatForm input{background:#0d1117;border-color:#30363d;color:#c9d1d9}
html[data-theme="dark"] #chatLog .errdet summary{color:#ffa198}
html[data-theme="dark"] #chatLog .errdet pre{background:#0d1117;border-color:#5c2b29;color:#c9d1d9}
`;

  var API = "/pe/report/api/chat";
  var TIMEOUT_MS = 60000;
  var sessionId = null;
  var built = false;
  var busy = false;
  var els = {};

  // 같은 페이지 안에서의 이동. 세션 상세에만 있는 함수라 홈에서는 조용히 무시한다.
  var ACTIONS = {
    open_item_detail: function (a) {
      if (typeof openItemDetail === "function" && a.subject) openItemDetail(a.subject, [a.subject]);
    },
    open_map: function (a) {
      if (a.subject && typeof openMapAnalysisForItem === "function") openMapAnalysisForItem(a.subject);
      else if (typeof gotoMapAnalysisTab === "function") gotoMapAnalysisTab();
      else clickTab("map-analysis");
    },
    open_tab: function (a) { clickTab(a.tab === "map" ? "map-analysis" : a.tab); },
  };

  function clickTab(name) {
    if (!name) return;
    var el = document.querySelector('.tab[data-tab="' + String(name).replace(/"/g, "") + '"]');
    if (el) el.click();
  }

  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)report_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function build() {
    if (built) return;
    built = true;
    var style = document.createElement("style");
    style.textContent = STYLE;
    document.head.appendChild(style);

    var fab = document.createElement("button");
    fab.id = "chatFab";
    fab.type = "button";
    fab.title = "챗봇 (관리자 전용)";
    fab.textContent = "💬";

    var panel = document.createElement("div");
    panel.id = "chatPanel";
    panel.innerHTML =
      '<div class="chat-head"><span>ENGR 챗봇 <small style="font-weight:400;color:#57606a">(관리자 테스트)</small></span>' +
      '<button type="button" id="chatClose" title="닫기">×</button></div>' +
      '<div id="chatLog"></div>' +
      '<form id="chatForm"><input id="chatInput" type="text" maxlength="500" ' +
      'placeholder="예: S3222 보고서 찾아줘 / 이 세션 수율" autocomplete="off">' +
      '<button type="submit" id="chatSend">전송</button></form>';

    document.body.appendChild(fab);
    document.body.appendChild(panel);

    els.fab = fab;
    els.panel = panel;
    els.log = panel.querySelector("#chatLog");
    els.input = panel.querySelector("#chatInput");
    els.send = panel.querySelector("#chatSend");

    fab.addEventListener("click", toggle);
    panel.querySelector("#chatClose").addEventListener("click", toggle);
    panel.querySelector("#chatForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var q = els.input.value.trim();
      if (q) { els.input.value = ""; send(q); }
    });

    addBot("무엇을 찾아드릴까요?\n" +
           "  · S3222 보고서 찾아줘\n" +
           "  · 이 세션 수율 / cpk 알려줘\n" +
           "  · SGM 들어가는 항목 예전에 어떻게 됐었지?\n" +
           "  · VDD_INT 상세 보여줘 / 맵 열어줘");
  }

  function toggle() {
    var open = els.panel.classList.toggle("open");
    if (open) els.input.focus();
  }

  function bubble(cls, text) {
    var wrap = document.createElement("div");
    wrap.className = "msg " + cls;
    var b = document.createElement("div");
    b.className = "bubble";
    b.textContent = text;          // 답변은 항상 평문 — innerHTML 로 넣지 않는다
    wrap.appendChild(b);
    els.log.appendChild(wrap);
    els.log.scrollTop = els.log.scrollHeight;
    return wrap;
  }

  function addBot(text) { return bubble("bot", text); }

  /** 오류 말풍선에 접힌 traceback 을 단다 (클릭하면 펼침). */
  function addDetail(wrap, detail) {
    var box = document.createElement("details");
    box.className = "errdet";
    var sum = document.createElement("summary");
    sum.textContent = "상세 (traceback)";
    var pre = document.createElement("pre");
    pre.textContent = detail;      // 항상 평문 — innerHTML 로 넣지 않는다
    box.appendChild(sum);
    box.appendChild(pre);
    wrap.appendChild(box);
    els.log.scrollTop = els.log.scrollHeight;
  }

  function addActions(wrap, links, choices) {
    if (!(links || []).length && !(choices || []).length) return;
    var box = document.createElement("div");
    box.className = "acts";
    (links || []).forEach(function (l) {
      if (l.url) {
        // 서버가 만든 내부 경로만 허용 — 외부 링크가 섞여 들어올 여지를 없앤다.
        if (String(l.url).indexOf("/pe/report/") !== 0) return;
        var a = document.createElement("a");
        a.href = l.url;
        a.textContent = l.label || "열기";
        box.appendChild(a);
      } else if (l.action && ACTIONS[l.action]) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = l.label || "이동";
        btn.addEventListener("click", function () {
          els.panel.classList.remove("open");
          try { ACTIONS[l.action](l.args || {}); } catch (e) { console.warn("chat action 실패", e); }
        });
        box.appendChild(btn);
      }
    });
    (choices || []).forEach(function (c) {
      if (!c || !c.question) return;
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "choice";
      btn.textContent = c.label || c.question;
      btn.addEventListener("click", function () { send(c.question); });
      box.appendChild(btn);
    });
    wrap.appendChild(box);
    els.log.scrollTop = els.log.scrollHeight;
  }

  function setBusy(on) {
    busy = on;
    els.input.disabled = on;
    els.send.disabled = on;
    els.send.textContent = on ? "…" : "전송";
  }

  function send(question) {
    if (busy) return;
    bubble("me", question);
    setBusy(true);
    var pending = addBot("찾는 중…");
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, TIMEOUT_MS);

    fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify({ question: question, context: { session_id: sessionId } }),
      signal: ctl.signal,
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) {
        return { ok: res.ok, status: res.status, body: j };
      });
    }).then(function (r) {
      els.log.removeChild(pending);
      if (!r.ok) {
        // master 전용 기능이라 원인을 감추지 않는다 — 예외 한 줄을 바로 보여 주고,
        // traceback 은 접어 둔다(관리자 탭에도 같은 내용이 남는다).
        var wrap = bubble("err", r.body.error || ("요청 실패 (HTTP " + r.status + ")"));
        if (r.body.detail) addDetail(wrap, r.body.detail);
        return;
      }
      var wrap = addBot(r.body.text || "(빈 응답)");
      addActions(wrap, r.body.links, r.body.choices);
    }).catch(function (e) {
      if (els.log.contains(pending)) els.log.removeChild(pending);
      bubble("err", e && e.name === "AbortError"
        ? "응답이 너무 오래 걸려 중단했습니다." : "요청 중 오류가 발생했습니다.");
    }).finally(function () {
      clearTimeout(timer);
      setBusy(false);
      els.input.focus();
    });
  }

  window.ChatWidget = {
    /** master 로 확인됐을 때만 호출한다. sid 를 주면 "이 세션" 질문이 그 세션으로 해석된다. */
    enable: function (sid) {
      sessionId = sid || null;
      build();
    },
  };
})();

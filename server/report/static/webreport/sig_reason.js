// ── Signature 판정 근거 팝업 ────────────────────────────────────────────────
// Issue Table Signature 셀의 [?] → "이 룰이 무슨 기준이고, 어떤 값이 임계값을 넘어서
// 걸렸는가" 를 보여준다. 데이터는 업로드 때 적재된 평가 스냅샷을 서버가 조회해 준다
// (GET .../issue_table/signature_reason — server/eval_panel/signature_reason.py).
//
// 배치·닫기 처방은 yield_issue.js 의 액션 메뉴와 같다: 표 스크롤에 잘리지 않게 fixed 로
// 띄우되 **패널 안에** 붙이고(body 직속이면 .content 클릭 위임이 못 잡는다), 바깥 클릭과
// ESC 로 닫는다. 한 번에 하나만 연다.
//
// ⚠️ 위임 핸들러를 여기서 따로 다는 이유 — edit_mode.js 의 .content click 핸들러는
// MODE !== "edit" 이면 곧바로 return 한다. [?] 는 조회 모드에서도 눌려야 한다.
let _sigrEl = null;
let _sigrAnchor = null;
let _sigrSeq = 0;          // 늦게 온 응답이 다시 열린 팝업을 덮지 않게 하는 토큰
const _sigrCache = new Map();   // "key|ids" → 응답 (스냅샷은 불변이라 무효화가 필요 없다)

function closeSigReason() {
  if (!_sigrEl) return;
  if (_sigrAnchor) _sigrAnchor.setAttribute("aria-expanded", "false");
  _sigrEl.remove();
  _sigrEl = null; _sigrAnchor = null;
  _sigrSeq++;              // 진행 중이던 요청의 결과를 버린다
}

// 이 셀의 signature id 목록 — 편집 모드는 드랍다운 값이 진실, 조회 모드는 data-sig.
function sigIdsOfCell(td) {
  const sels = td.querySelectorAll("select.issue-sig-sel");
  if (sels.length) return [...sels].map(s => s.value).filter(v => v && v !== "__del");
  return String(td.dataset.sig || "").split(",").filter(Boolean);
}

function sigrNum(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v !== "number") return String(v);
  if (!isFinite(v)) return String(v);
  const a = Math.abs(v);
  if (a !== 0 && (a < 0.001 || a >= 1e6)) return v.toExponential(2);
  return String(Math.round(v * 10000) / 10000);
}

function sigrPct(v) {
  if (v === null || v === undefined || typeof v !== "number" || !isFinite(v)) return "";
  const sign = v >= 0 ? "+" : "";
  return ` (${sign}${Math.round(v * 100)}%)`;
}

const SIGR_SRC_LABEL = {
  raw_metrics: "측정 통계", features: "분포 특징", derived: "파생값", evidence: "발화 근거",
};

function sigrCondRow(cond) {
  const applies = !!cond.applies;
  // ⚠️ "값이 저장되지 않아 판정할 수 없었다" 를 "조건 미충족" 으로 그리면 안 된다 —
  // 사용자가 "이 룰은 안 걸렸구나" 로 정반대로 읽는다.
  const verdict = !applies
    ? `<span class="sigr-na">판정 불가</span>`
    : (cond.passed ? `<span class="sigr-hit">충족${sigrPct(cond.exceedance)}</span>`
                   : `<span class="sigr-miss">미충족</span>`);
  const src = cond.value_source ? SIGR_SRC_LABEL[cond.value_source] || cond.value_source : "미저장";
  const ref = cond.ref_key
    ? `${esc(cond.ref_key)} = ${sigrNum(cond.ref_value)}`
    : sigrNum(cond.ref_value);
  return `<tr><td class="sigr-metric">${esc(cond.metric)}</td>` +
    `<td class="sigr-cond">${esc(cond.cond || "")}</td>` +
    `<td>${ref}</td>` +
    `<td>${sigrNum(cond.actual)}<span class="sigr-src">${esc(src)}</span></td>` +
    `<td>${verdict}</td></tr>`;
}

function sigrRuleHtml(rule) {
  if (rule.unknown_rule) {
    return `<div class="sigr-rule"><div class="sigr-rule-head">` +
      `<b>${esc(rule.id)}</b><span class="sigr-tag">현재 룰 목록에 없음</span></div>` +
      `<div class="sigr-desc">지금은 정의가 없는 룰입니다 — 사람이 확정해 둔 옛 값이거나 ` +
      `그 뒤 룰이 삭제된 경우입니다.</div></div>`;
  }
  const tags = [];
  if (rule.status_hint) tags.push(esc(rule.status_hint));
  if (rule.issue_category) tags.push(esc(rule.issue_category));
  if (rule.enabled === false) tags.push("비활성");
  tags.push(rule.fired ? (rule.role === "primary" ? "발화(주 원인)" : "발화") : "미발화");
  const crit = rule.criterion
    ? `<div class="sigr-crit">기준: <code>${esc(rule.criterion.metric)} ` +
      `${esc(rule.criterion.op)} ${esc(rule.criterion.threshold_key)}` +
      `(${sigrNum(rule.criterion.threshold)})</code></div>`
    : "";
  const conds = (rule.conditions || []).length
    ? `<table class="sigr-table"><thead><tr><th>지표</th><th>조건</th><th>임계값</th>` +
      `<th>실측값</th><th>판정</th></tr></thead><tbody>` +
      rule.conditions.map(sigrCondRow).join("") + `</tbody></table>`
    : `<div class="sigr-desc">${esc(rule.special_note || "조건식이 없는 룰입니다.")}</div>`;
  return `<div class="sigr-rule"><div class="sigr-rule-head"><b>${esc(rule.id)}</b>` +
    tags.map(t => `<span class="sigr-tag">${t}</span>`).join("") + `</div>` +
    (rule.phenomenon_ko ? `<div class="sigr-desc">${esc(rule.phenomenon_ko)}</div>` : "") +
    crit +
    (rule.special_note && (rule.conditions || []).length
      ? `<div class="sigr-desc">${esc(rule.special_note)}</div>` : "") +
    conds +
    (rule.action_ko ? `<div class="sigr-act">조치 제안: ${esc(rule.action_ko)}</div>` : "") +
    `</div>`;
}

function sigrBodyHtml(data) {
  const parts = [];
  if (data.evidence_note) parts.push(`<div class="sigr-note">${esc(data.evidence_note)}</div>`);
  (data.warnings || []).forEach(w => parts.push(`<div class="sigr-warn">⚠ ${esc(w)}</div>`));
  if (!(data.rules || []).length) {
    parts.push(`<div class="sigr-desc">확정된 signature 가 없습니다.</div>`);
  } else {
    (data.rules || []).forEach(r => parts.push(sigrRuleHtml(r)));
  }
  return parts.join("");
}

function sigrShell(inner, stamp) {
  return `<div class="sigr-head"><span class="sigr-title">Signature 판정 근거</span>` +
    (stamp ? `<span class="sigr-stamp">${esc(stamp)}</span>` : "") +
    `<button type="button" class="sigr-close" title="닫기">×</button></div>` +
    `<div class="sigr-body">${inner}</div>`;
}

function sigrPlace(pop, btn) {
  const rect = btn.getBoundingClientRect();
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let top = rect.bottom + 4;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, rect.top - ph - 4);
  pop.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - pw - 12)) + "px";
  pop.style.top = top + "px";
  pop.style.visibility = "";
}

async function openSigReason(btn) {
  if (_sigrAnchor === btn) { closeSigReason(); return; }   // 같은 버튼 재클릭 = 닫기
  closeSigReason();
  const td = btn.closest("td.issue-sig-cell");
  const panel = (typeof issuePanelOf === "function") ? issuePanelOf(btn) : null;
  if (!td || !panel) return;
  const key = td.dataset.key || "";
  const ids = sigIdsOfCell(td);
  if (!key) return;

  const pop = document.createElement("div");
  pop.className = "sigr-pop";
  pop.style.position = "fixed";
  pop.style.visibility = "hidden";
  pop.innerHTML = sigrShell(`<div class="sigr-desc">불러오는 중…</div>`, "");
  panel.appendChild(pop);
  sigrPlace(pop, btn);
  btn.setAttribute("aria-expanded", "true");
  _sigrEl = pop; _sigrAnchor = btn;

  const seq = ++_sigrSeq;
  const cacheKey = key + "|" + ids.join(",");
  try {
    let data = _sigrCache.get(cacheKey);
    if (!data) {
      const url = `/pe/report/session/${SESSION_ID}/web_report/issue_table/signature_reason`
        + `?key=${encodeURIComponent(key)}&ids=${encodeURIComponent(ids.join(","))}`;
      const res = await fetch(url);
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
      data = j;
      _sigrCache.set(cacheKey, data);
    }
    if (seq !== _sigrSeq || _sigrEl !== pop) return;   // 그 사이 닫혔거나 다른 셀이 열렸다
    const stamp = data.ingested_at ? "업로드 시점 기준" : "";
    pop.innerHTML = sigrShell(sigrBodyHtml(data), stamp);
    sigrPlace(pop, btn);
  } catch (err) {
    if (seq !== _sigrSeq || _sigrEl !== pop) return;
    // 빈 화면으로 굳지 않게 사유와 재시도를 팝업 안에 남긴다.
    pop.innerHTML = sigrShell(
      `<div class="sigr-warn">근거를 불러오지 못했습니다: ${esc(err.message)}</div>` +
      `<div><button type="button" class="btn-sm sigr-retry">다시 시도</button></div>`, "");
    sigrPlace(pop, btn);
  }
}

// ── AI Comment 사례 목록 팝오버 ──────────────────────────────────────────────
// 「📋 사례 N건 상세」(sheets.js aicPrecLinkHtml) → 그 항목에 매칭된 과거 사례 원문.
// 배치·닫기·늦은 응답 처리는 위 Signature 근거 팝업과 **같은 장치를 그대로 쓴다**
// (_sigrEl/_sigrSeq/sigrPlace/닫기 핸들러) — 한 번에 하나만 열리는 것도 그 덕이다.
// 셀 텍스트가 아니라 별도 라우트에서 받는 이유: 사례 원문은 payload 에 싣기엔 무겁고,
// 셀에는 LLM 이 요약한 한 줄만 남기기 때문이다(docs/23).
function aicPrecRowHtml(p) {
  const head = [];
  if (p.product_name) head.push(esc(p.product_name));
  if (p.lot_id) head.push("lot " + esc(p.lot_id));
  if (p.item_canonical) head.push(esc(p.item_canonical));
  if (p.unit) head.push(esc(p.unit));
  const tags = [];
  if (p.status) tags.push(esc(p.status));
  if (p.signature) tags.push(esc(p.signature));
  const m = p.metrics || {};
  const stats = ["cpk", "yield", "mean", "stdev", "fail_count", "total_count"]
    .filter(k => m[k] !== undefined && m[k] !== null)
    .map(k => `${k}=${sigrNum(m[k])}`).join(", ");
  // 출처 세션으로 바로 가는 링크(2026-09-02 사용자 요청). session_id 는 선례 행이
  // 실어 온다(eval_engine store.search_precedents → present._precedent_result).
  // 없는 선례(CSV 로 적재된 과거 사례 등)도 있으므로 **있을 때만** 그린다.
  // 자기 세션은 애초에 선례에서 제외되므로(exclude_session_id) 자기 자신으로 가는
  // 링크는 생기지 않는다. 새 탭으로 여는 이유는 지금 보던 표를 잃지 않기 위해서다.
  const sid = String(p.session_id || "").trim();
  const link = sid
    ? `<a class="sigr-src-link" href="/pe/report/view/${encodeURIComponent(sid)}"` +
      ` target="_blank" rel="noopener"` +
      ` title="이 사례가 나온 세션을 새 탭으로 엽니다">세션 열기 ↗</a>`
    : "";
  return `<div class="sigr-rule"><div class="sigr-rule-head">` +
    `<b>${head.join(" / ") || "(출처 미상)"}</b>` +
    tags.map(t => `<span class="sigr-tag">${t}</span>`).join("") + link + `</div>` +
    (stats ? `<div class="sigr-desc">당시 통계: ${esc(stats)}</div>` : "") +
    `<div class="sigr-act">${linkifyComment(String(p.comment || ""))}</div></div>`;
}

async function openAicPrec(btn) {
  if (_sigrAnchor === btn) { closeSigReason(); return; }
  closeSigReason();
  const key = btn.dataset.key || "";
  const panel = (typeof issuePanelOf === "function") ? issuePanelOf(btn) : null;
  if (!key || !panel) return;

  const pop = document.createElement("div");
  pop.className = "sigr-pop";
  pop.style.position = "fixed";
  pop.style.visibility = "hidden";
  pop.innerHTML = sigrShell(`<div class="sigr-desc">불러오는 중…</div>`, "");
  panel.appendChild(pop);
  sigrPlace(pop, btn);
  btn.setAttribute("aria-expanded", "true");
  _sigrEl = pop; _sigrAnchor = btn;

  const seq = ++_sigrSeq;
  const cacheKey = "prec|" + key;
  try {
    let data = _sigrCache.get(cacheKey);
    if (!data) {
      const url = `/pe/report/session/${SESSION_ID}/web_report/ai_comment/precedents`
        + `?key=${encodeURIComponent(key)}`;
      const res = await fetch(url);
      const j = await res.json().catch(() => ({}));
      if (res.status === 202) {
        if (seq !== _sigrSeq || _sigrEl !== pop) return;
        // 평가 캐시가 없어 백그라운드 잡을 예약한 상태 — 캐시하지 않는다(곧 채워진다).
        pop.innerHTML = sigrShell(
          `<div class="sigr-desc">평가 결과를 준비 중입니다 — 잠시 후 다시 눌러 주세요.</div>`,
          "");
        sigrPlace(pop, btn);
        return;
      }
      if (!res.ok) throw new Error(j.error || `HTTP ${res.status}`);
      data = j;
      _sigrCache.set(cacheKey, data);
    }
    if (seq !== _sigrSeq || _sigrEl !== pop) return;
    const rows = data.items || [];
    const body = rows.length
      ? rows.map(aicPrecRowHtml).join("")
      : `<div class="sigr-desc">이 항목에 매칭된 과거 사례가 없습니다.</div>`;
    pop.innerHTML = sigrShell(body, rows.length ? `${rows.length}건` : "");
    sigrPlace(pop, btn);
  } catch (err) {
    if (seq !== _sigrSeq || _sigrEl !== pop) return;
    pop.innerHTML = sigrShell(
      `<div class="sigr-warn">사례를 불러오지 못했습니다: ${esc(err.message)}</div>` +
      `<div><button type="button" class="btn-sm sigr-retry">다시 시도</button></div>`, "");
    sigrPlace(pop, btn);
  }
}

document.addEventListener("click", e => {
  const btn = e.target.closest(".sig-why");
  if (btn) { openSigReason(btn); return; }
  const prec = e.target.closest(".aic-prec-btn");
  if (prec) { openAicPrec(prec); return; }
  if (!_sigrEl) return;
  if (e.target.closest(".sigr-close")) { closeSigReason(); return; }
  if (e.target.closest(".sigr-retry")) {
    const anchor = _sigrAnchor;
    _sigrCache.clear();
    closeSigReason();
    // 재시도 대상이 사례 목록 버튼인지 Signature [?] 인지에 따라 여는 함수가 다르다.
    if (anchor) {
      if (anchor.classList.contains("aic-prec-btn")) openAicPrec(anchor);
      else openSigReason(anchor);
    }
    return;
  }
  if (!e.target.closest(".sigr-pop")) closeSigReason();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeSigReason(); });

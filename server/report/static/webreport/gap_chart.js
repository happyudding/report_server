// Gap Chart — 사용자 수식으로 만든 파생 분포 (2026-08-24).
//
// Distribution 툴바 "분석하기 ▾" → "Gap Chart" → 모달(좌우 2단: 항목 목록 |
// source 선택·수식 편집)에서 식을 조립하면 갤러리 맨 앞에 카드가 생기고, 카드를 누르면 **기존
// Item_detail** 화면이 그대로 열린다(서버 응답 구조가 /scatter 와 같다).
//
// 저장되는 것은 수식(토큰 배열)뿐이다 — 값은 조회 때 서버가 raw tables 로 다시 만든다
// (web_report/gap_chart.py). 정의는 세션 편집 DB kind=gap_chart 이고 payload 중립이라
// 저장해도 report 캐시가 살아 있다.
//
// **수식 에디터는 contenteditable 이 아니다.** 위쪽 칩 렌더 영역은 읽기 전용이고 입력은
// 평범한 <input> 하나다 → 한글 IME·캐럿 복원·붙여넣기 살균 문제가 구조적으로 없다.
// input+오버레이 하이라이트 방식은 애초에 불가능하다: item 이름에 공백·괄호·연산자가
// 전부 합법이라(honeyform 은 중복·메타충돌만 검사) 평문을 매 키입력마다 재렉싱할 수 없다.
// 그래서 커밋 규칙을 모호성 0 으로 둔다 —
//   · 입력창이 **비어 있을 때** `+ - * / ( )` 키  → 그 연산자 토큰
//   · 입력창에 **글자가 있을 때** 같은 키        → 그냥 검색어 문자 (item 명의 `-` 보호)
//   · 연산자·괄호는 버튼으로도 항상 커밋 가능(키 규칙을 몰라도 된다)
// v1 은 끝에만 삽입한다(중간 편집은 칩을 지우고 다시 입력).

const GC_MAX_TOKENS = 200;          // 서버 gap_chart.MAX_TOKENS 와 같은 값
const GC_MAX_DEPTH = 16;            // 서버 gap_chart.MAX_DEPTH
const GC_MAX_REFS = 20;             // 서버 gap_chart.MAX_REFS
const GC_NAME_MAX = 120;
const GC_LIST_MAX = 200;            // 왼쪽 항목 목록 표시 상한
const GC_SUGGEST_MAX = 30;          // 수식 입력 자동완성 표시 개수
const GC_OP_TEXT = { "+": "+", "-": "−", "*": "×", "/": "÷" };
const GC_OP_KEYS = { "+": "+", "-": "-", "*": "*", "/": "/", "(": "lp", ")": "rp" };

function gcAll() { return (DATA && DATA.gap_charts) || {}; }
function gcGet(id) { return gcAll()[id] || null; }
// 이름순 안정 정렬 — dict 라 생성 순서를 알 수 없다(dcSortedIds 와 같은 규칙).
function gcSortedIds() {
  const all = gcAll();
  return Object.keys(all).sort((a, b) => {
    const na = String((all[a] || {}).name || ""), nb = String((all[b] || {}).name || "");
    return na.localeCompare(nb) || a.localeCompare(b);
  });
}

function gcNumText(v) {
  const n = Number(v);
  return Number.isInteger(n) ? String(n) : String(n);
}

// 표시 문자열 — **재파싱 대상이 아니다**(서버 render_formula 와 같은 규칙).
function gcFormulaText(tokens) {
  const parts = (tokens || []).map(t => {
    if (t.t === "num") return gcNumText(t.v);
    if (t.t === "item") return t.source ? `${t.source}_${t.item}` : t.item;
    if (t.t === "op") return GC_OP_TEXT[t.v] || t.v;
    return t.t === "lp" ? "(" : ")";
  });
  return parts.join(" ").replace(/\( /g, "(").replace(/ \)/g, ")");
}

// 수식 모드 — 서버 gap_chart.formula_mode 와 **같은 규칙**(저장하지 않고 유도한다).
function gcModeOf(tokens) {
  let qualified = false, plain = false;
  (tokens || []).forEach(t => {
    if (t.t !== "item") return;
    if (t.source) qualified = true; else plain = true;
  });
  if (qualified && plain) return "mixed";
  return qualified ? "explicit" : "per_source";
}

// 선형 검증 — **UX 용이고 권위는 서버 400** 이다(서버는 재귀하강 파서로 다시 본다).
// 저장 버튼을 미리 잠가 왕복 없이 알려주는 것이 목적.
function gcValidate(tokens) {
  const toks = tokens || [];
  if (!toks.length) return { ok: false, msg: "수식을 입력하세요" };
  if (toks.length > GC_MAX_TOKENS) return { ok: false, msg: `수식이 너무 깁니다 (${GC_MAX_TOKENS}개 이하)` };
  let depth = 0, prev = null;          // prev: null | "val" | "op" | "lp" | "rp"
  for (let i = 0; i < toks.length; i++) {
    const t = toks[i], k = t.t;
    if (k === "lp") {
      if (prev === "val" || prev === "rp") return { ok: false, msg: `${i + 1}번째 여는 괄호 앞에 연산자가 필요합니다` };
      if (++depth > GC_MAX_DEPTH) return { ok: false, msg: `괄호가 너무 깊습니다 (${GC_MAX_DEPTH}단 이하)` };
      prev = "lp";
    } else if (k === "rp") {
      if (prev !== "val" && prev !== "rp") return { ok: false, msg: `${i + 1}번째 닫는 괄호 앞이 비었습니다` };
      if (--depth < 0) return { ok: false, msg: `${i + 1}번째 닫는 괄호에 짝이 없습니다` };
      prev = "rp";
    } else if (k === "op") {
      // 단항 +/- 은 시작·여는 괄호 뒤·연산자 뒤에서 허용(서버 parse_unary 와 같다).
      const unary = (t.v === "+" || t.v === "-") && (prev === null || prev === "lp" || prev === "op");
      if (!unary && (prev === null || prev === "lp" || prev === "op")) {
        return { ok: false, msg: `${i + 1}번째 연산자 앞에 항목이나 숫자가 필요합니다` };
      }
      prev = "op";
    } else {
      if (prev === "val" || prev === "rp") return { ok: false, msg: `${i + 1}번째 앞에 연산자가 필요합니다` };
      prev = "val";
    }
  }
  if (depth !== 0) return { ok: false, msg: "닫는 괄호가 없습니다" };
  if (prev === "op" || prev === "lp") return { ok: false, msg: "수식이 끝나지 않았습니다" };
  const refs = new Set();
  toks.forEach(t => { if (t.t === "item") refs.add((t.source || "") + "" + t.item); });
  if (!refs.size) return { ok: false, msg: "항목을 하나 이상 넣어야 합니다" };
  if (refs.size > GC_MAX_REFS) return { ok: false, msg: `참조 항목이 너무 많습니다 (${GC_MAX_REFS}개 이하)` };
  if (gcModeOf(toks) === "mixed") {
    return { ok: false, msg: "항목만 쓴 참조와 source 를 붙인 참조를 섞을 수 없습니다" };
  }
  return { ok: true, msg: "" };
}

// ── 계산 결과 캐시 (고정 — LRU 축출 없음) ─────────────────────────────────────
// 값은 서버 응답 그대로이고 ECDF({xs,ys})는 여기서 **한 번만** 만들어 보관한다.
// distDisplayPoints 가 entry 객체를 WeakMap 키로 메모하므로 객체가 안정적이어야 한다
// (매 렌더마다 새로 만들면 메모가 무의미해진다).
const _gcCache = { all: {}, bin1: {}, rtbin1: {} };
const _gcInflight = { all: new Set(), bin1: new Set(), rtbin1: new Set() };

function gcCacheFor(variant) { return _gcCache[distVariantKey(variant)] || _gcCache.all; }

// cdf(정렬 결과 order 포함)와 원본 시리즈를 함께 들고 있는다 — Map 선택 좌표 마커가
// "이 좌표의 gap 값과 그 누적%" 를 찾을 때 필요하다(gcChipHits).
function gcBuildSeries(data) {
  return ((data && data.sources) || []).map(s => {
    const c = distCdfFromValues(s.values || []);
    return { name: s.name, entry: { xs: c.x, ys: c.y }, cdf: c, src: s };
  }).filter(s => s.entry.xs && s.entry.xs.length);
}

// ── Map Analysis 선택 좌표(mapSelChips) → 이 gap 시리즈 위의 점 ──────────────
// gap 값은 수식으로 만든 파생값이라 chip.items(서버가 준 원본 항목 값·누적%)에 없다.
// 대신 서버 응답이 값과 나란히 SERIAL/XPOS/YPOS 를 주므로, **좌표로 원본 행을 찾아**
// 그 값과 정렬 위치(=누적%)를 그대로 읽는다. 즉 마커는 이 차트가 그린 곡선 위에 정확히
// 놓인다(별도 계산이 아니라 같은 배열에서 읽은 값).
//   per_source 모드: 시리즈 이름 = source 명 → 그 source 의 chip 만
//   explicit  모드: 좌표 교집합 시리즈 1개 → source 무관하게 좌표로만 매칭
function gcChipHits(seriesName, cdf, xpos, ypos, perSource) {
  const hits = [];
  if (!mapSelChips.length || !cdf || !cdf.order) return hits;
  const want = new Map();
  mapSelChips.forEach(c => {
    if (perSource && (c.source || "") !== seriesName) return;
    want.set(String(c.xpos) + "\x1f" + String(c.ypos), c);
  });
  if (!want.size || !xpos || !ypos) return hits;
  for (let k = 0; k < cdf.order.length; k++) {
    const i = cdf.order[k];
    const chip = want.get(String(xpos[i]) + "\x1f" + String(ypos[i]));
    // chip 참조를 함께 실어 보낸다 — Item_detail 의 '선택 좌표 값' 표가 이 값을 그대로 쓴다.
    if (chip) hits.push({ color: chip.color, value: cdf.x[k], cum: cdf.y[k], chip: chip });
  }
  return hits;
}
// 시리즈 전체분을 한 번에 모은다(크로스헤어 판정이 차트 단위여야 하므로 — mapSelMarkerTraces).
function gcChipMarkers(hit, chart) {
  if (!hit || !mapSelChips.length) return null;
  const perSource = gcModeOf(chart && chart.tokens) !== "explicit";
  let hits = [];
  (hit.series || []).forEach(s => {
    hits = hits.concat(gcChipHits(s.name, s.cdf, s.src.xpos, s.src.ypos, perSource));
  });
  return mapSelMarkerTraces(hits);
}

function gcEnsureChart(id, variant) {
  const key = distVariantKey(variant);
  const store = _gcCache[key], inflight = _gcInflight[key];
  if (store[id] || inflight.has(id)) return;
  inflight.add(id);
  const q = distVariantQuery(key).replace(/^&/, "?");
  const url = `/pe/report/session/${SESSION_ID}/web_report/gap_chart/${encodeURIComponent(id)}${q}`;
  fetchJson202(url, { shouldStop: () => !gcGet(id) })
    .then(data => {
      store[id] = { data: data, series: gcBuildSeries(data) };
      gcRefresh();
    })
    .catch(e => showToast("Gap Chart 계산 실패: " + e.message))
    .then(() => { inflight.delete(id); });
}

// 데이터 도착 후 재렌더 — 보이는 gap 카드만 다시 큐에 넣는다(합성 카드 흐름과 동일).
let _gcRefreshTimer = null;
function gcRefresh() {
  if (_gcRefreshTimer) return;
  _gcRefreshTimer = setTimeout(() => {
    _gcRefreshTimer = null;
    document.querySelectorAll('#panel-distribution .distg-gap[data-visible="1"]')
      .forEach(c => { c.dataset.rendered = ""; distQueueRender(c); });
  }, 0);
}

// 수식을 고치면 그 차트의 계산 결과만 버린다(다른 차트·일반 카드는 건드리지 않는다).
function gcDropCache(id) {
  ["all", "bin1", "rtbin1"].forEach(k => { delete _gcCache[k][id]; });
}

function gcLimitOf(chart) {
  const lim = (chart && chart.limit) || {};
  if (lim.mode !== "manual") return { lo: null, hi: null };
  return { lo: lim.lo == null ? null : lim.lo, hi: lim.hi == null ? null : lim.hi };
}

// ── 갤러리 카드 ───────────────────────────────────────────────────────────────
// 일반 카드와 같은 골격(.distg-card + .distg-plot)이라 IntersectionObserver·purge·
// rAF 큐(distQueueRender/distPurgeGalleryCell)를 그대로 재사용한다.
function gcCardsHtml() {
  const ids = gcSortedIds();
  if (!ids.length) return "";
  const editing = (typeof MODE !== "undefined" && MODE === "edit");
  return ids.map(id => {
    const c = gcAll()[id];
    const { lo, hi } = gcLimitOf(c);
    const lim = distLimInnerHtml(lo, hi, "");
    const cached = (_gcCache.all[id] || {}).data;
    const dies = cached ? `die ${cached.matched_dies}개` : "계산 중…";
    const acts = editing
      ? `<span class="distg-comp-acts">` +
        `<button type="button" class="distg-comp-btn" data-gc-act="edit" data-gap-id="${esc(id)}" title="수정">✎</button>` +
        `<button type="button" class="distg-comp-btn" data-gc-act="del" data-gap-id="${esc(id)}" title="삭제">✕</button></span>`
      : "";
    return `<div class="distg-card distg-gap" data-gap-id="${esc(id)}" data-status="ok">
      <div class="distg-head">
        <div class="distg-line1">
          <span class="distg-gap-badge" title="Gap Chart (수식)">📈</span>
          <span class="distg-name" title="${esc(gcFormulaText(c.tokens))}">${esc(c.name || "")}</span>
          ${acts}
        </div>
        <div class="distg-line2">
          <span class="distg-lim">${lim}</span>
          <span class="distg-cpk">${esc(dies)}</span>
        </div>
      </div>
      <div class="distg-plot"></div>
    </div>`;
  }).join("");
}

// gap 카드 1장 렌더 — distRenderGalleryCell 이 data-gap-id 를 보고 여기로 넘긴다.
function gcRenderGapCell(cell) {
  if (cell.dataset.rendered === "1") return;
  const id = cell.dataset.gapId;
  const chart = gcGet(id);
  const plot = cell.querySelector(".distg-plot");
  if (!chart || !plot || typeof Plotly === "undefined") return;
  const variant = distGalleryVariant();
  const hit = gcCacheFor(variant)[id];
  if (!hit) { gcEnsureChart(id, variant); return; }   // 도착 후 gcRefresh 가 재큐잉

  const series = hit.series;
  if (!series.length) {
    const why = (hit.data.missing || []).join(" · ") || "계산 가능한 데이터가 없습니다";
    plot.innerHTML = `<div class="placeholder gc-cell-empty">${esc(why)}</div>`;
    cell.dataset.rendered = "1";
    return;
  }
  // 표시점 캡은 시리즈 수로 나눈다(일반 카드가 source 수로 나누는 것과 같은 규칙).
  const cap = distCapFor(series.length, DIST.CELL_BUDGET_CARD);
  const pts = {};
  series.forEach(s => { pts[s.name] = distDisplayPoints(s.entry, cap); });
  const { lo, hi } = gcLimitOf(chart);
  const sentinel = distSentinelTrace(pts);
  // Map Analysis 선택 좌표 — 일반 카드와 같은 마커(값·누적%는 이 차트 곡선에서 읽는다).
  const cm = gcChipMarkers(hit, chart);
  const traces = sentinel ? [sentinel] : [];
  if (cm) traces.push(...cm.traces);
  const layout = { ...DIST_PLOT_BG, plot_bgcolor: "#FFFFFF",
    xaxis: { showgrid: true, gridcolor: "#eee", zeroline: false, ticks: "outside",
      tickcolor: "#bbb", tickfont: { size: 9 } },
    yaxis: { range: [0, 100], ticksuffix: "%", showgrid: true, gridcolor: "#eee",
      zeroline: false, tickfont: { size: 9 } },
    shapes: distSpecShapes(lo, hi, false).concat(cm ? cm.shapes : []),
    annotations: distSpecAnnos(lo, hi, true),
    margin: { l: 34, r: 10, t: 8, b: 20 }, showlegend: false };
  Plotly.newPlot(plot, traces, layout, DIST_CFG_STATIC);
  // explicit 모드는 시리즈 이름이 차트 이름이라 distColorMap 에 없다 → 오버라이드로 색을 준다
  // (per_source 는 시리즈 이름 = source 명이라 기존 색이 그대로 맞는다).
  if (chart && gcModeOf(chart.tokens) === "explicit") {
    plot._distColorFor = () => "#7C3AED";
  }
  // chip 마커는 canvas 위로 다시 그린다 — 안 넘기면 ECDF 점 canvas 에 가려 안 보인다.
  distPaintPoints(plot, pts, cm ? cm.traces : null);
  cell.dataset.rendered = "1";
}

// ── 메뉴 항목 (dist_composite 의 "분석하기 ▾" 메뉴를 공유) ────────────────────
function gcMenuItemHtml() {
  return `<button type="button" class="issue-menu-item" data-dc-act="gap-modal"` +
    ` title="수식(항목 사칙연산)으로 새 분포 차트를 만든다">` +
    `<span class="issue-menu-mark"></span>` +
    `<span class="issue-menu-label">📈 Gap Chart</span></button>`;
}

// ── 모달 ─────────────────────────────────────────────────────────────────────
let _gcEditId = null;                  // null = 신규
let _gcTokens = [];                    // 수식 토큰 (정본)
let _gcSelSources = new Set();
let _gcSuggest = [];                   // 현재 자동완성 후보
let _gcSuggestAt = -1;                 // 하이라이트 인덱스
let _gcSearchTimer = null;
let _gcListTimer = null;

function gcSourceNames() {
  return (((DATA && DATA.web_report && DATA.web_report.sources) || []).map(s => s.name || s))
    .filter(Boolean);
}

function gcOpenModal(id) {
  const modal = document.getElementById("gcModal");
  if (!modal) return;
  if (typeof dcCloseMenu === "function") dcCloseMenu();
  _gcEditId = id || null;
  const chart = id ? gcGet(id) : null;
  _gcTokens = chart ? JSON.parse(JSON.stringify(chart.tokens || [])) : [];
  _gcSelSources = new Set();
  if (chart) {
    (chart.sources || []).forEach(s => _gcSelSources.add(s));
  } else {
    gcSourceNames().forEach(s => _gcSelSources.add(s));   // 기본 전체 선택
  }
  const title = document.getElementById("gcModalTitle");
  if (title) title.textContent = chart ? "Gap Chart 수정" : "Gap Chart 만들기";
  const nameInput = document.getElementById("gcName");
  if (nameInput) nameInput.value = chart ? (chart.name || "") : "";
  const lim = (chart && chart.limit) || {};
  const loI = document.getElementById("gcLimitLo"), hiI = document.getElementById("gcLimitHi");
  if (loI) loI.value = (lim.mode === "manual" && lim.lo != null) ? lim.lo : "";
  if (hiI) hiI.value = (lim.mode === "manual" && lim.hi != null) ? lim.hi : "";
  const del = document.getElementById("gcDelete");
  if (del) del.style.display = chart ? "" : "none";
  const search = document.getElementById("gcItemSearch");
  if (search) search.value = "";
  const tokenInput = document.getElementById("gcTokenInput");
  if (tokenInput) tokenInput.value = "";
  _gcSuggest = []; _gcSuggestAt = -1;

  gcRenderSources();
  gcRenderSrcQual();
  gcRenderItemList("");
  gcRenderExpr();
  gcRenderSuggest();
  modal.classList.add("show");
  if (nameInput) nameInput.focus();
}

function gcCloseModal() {
  const modal = document.getElementById("gcModal");
  if (modal) modal.classList.remove("show");
  _gcEditId = null;
  _gcTokens = [];
}

function gcRenderSources() {
  const host = document.getElementById("gcSourceList");
  if (!host) return;
  host.innerHTML = gcSourceNames().map(s =>
    `<label class="gc-src-item" title="${esc(s)}">` +   // 한 줄 고정이라 긴 이름은 잘린다 → 툴팁으로 전체 노출
    `<input type="checkbox" class="gc-src-chk" data-source="${esc(s)}"` +
    `${_gcSelSources.has(s) ? " checked" : ""}><span>${esc(s)}</span></label>`).join("")
    || `<div class="placeholder">source 없음</div>`;
}

// source 한정 드롭다운 — 이걸로 고르면 다음 item 토큰이 source 명시 참조가 된다.
// 문자열을 `_` 로 분해하지 않는 **확실한 경로**다(source 명·item 명 둘 다 `_` 를 포함할 수 있다).
function gcRenderSrcQual() {
  const sel = document.getElementById("gcSrcQual");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = `<option value="">source 없음 (항목만)</option>` +
    gcSourceNames().map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  if (cur && gcSourceNames().indexOf(cur) >= 0) sel.value = cur;
}

// 왼쪽 항목 목록 — 갤러리 검색과 **같은** distSuggestions(부분일치)를 쓴다. 클릭하면 수식에 삽입.
function gcRenderItemList(q) {
  const host = document.getElementById("gcItemList");
  if (!host) return;
  const term = String(q || "").trim();
  const rows = term ? distSuggestions(term, 0) : distIndex.slice(0, GC_LIST_MAX);
  const show = rows.length > GC_LIST_MAX ? rows.slice(0, GC_LIST_MAX) : rows;
  const more = rows.length > GC_LIST_MAX ? `<div class="gc-list-more">상위 ${GC_LIST_MAX}개 표시 (일치 ${rows.length}개)</div>` : "";
  host.innerHTML = more + (show.map(r =>
    `<button type="button" class="dist-sug-item gc-list-item" data-gc-pick="${esc(r.subject)}">` +
    `<span class="sug-tno">${esc(r.test_num || "")}</span>` +
    `<span class="sug-name">${esc(r.subject)}</span></button>`).join("")
    || `<div class="placeholder">${term ? "일치하는 항목이 없습니다" : "항목 없음"}</div>`);
}

// 토큰 1개의 클래스+내용. 모달(클릭 삭제)과 Item_detail(읽기 전용)이 공유한다 —
// 같은 서식으로 보여야 "만들 때 본 식"과 "상세에서 보는 식"이 같은 것으로 읽힌다.
function gcTokenParts(tok) {
  if (tok.t === "item") {
    const inner = tok.source
      ? `<i class="gc-tok-src">${esc(tok.source)}</i><i class="gc-tok-us">_</i><i class="gc-tok-item">${esc(tok.item)}</i>`
      : `<i class="gc-tok-item">${esc(tok.item)}</i>`;
    return { cls: "gc-tok gc-tok-ref", html: inner };
  }
  if (tok.t === "num") return { cls: "gc-tok gc-tok-num", html: esc(gcNumText(tok.v)) };
  if (tok.t === "op") return { cls: "gc-tok gc-tok-op", html: esc(GC_OP_TEXT[tok.v] || tok.v) };
  return { cls: "gc-tok gc-tok-paren", html: tok.t === "lp" ? "(" : ")" };
}

function gcTokenHtml(tok, i) {
  const p = gcTokenParts(tok);
  return `<span class="${p.cls}" data-gc-tok="${i}" title="클릭하면 이 토큰을 지웁니다">` +
    `${p.html}</span>`;
}

// 읽기 전용 수식 렌더 — Item_detail 헤더가 쓴다(클릭 삭제 속성 없음).
function gcExprHtml(tokens) {
  return (tokens || []).map(t => {
    const p = gcTokenParts(t);
    return `<span class="${p.cls}">${p.html}</span>`;
  }).join("");
}

// Item_detail 상단 수식 줄 — item_detail.js 가 gap 응답일 때만 부른다.
// 서버 응답의 tokens 를 쓰고, 없으면(구 캐시) 평문 formula 로 폴백한다.
function gcFormulaBarHtml(data) {
  if (!data || !data.is_gap) return "";
  const expr = (data.tokens && data.tokens.length)
    ? gcExprHtml(data.tokens)
    : esc(data.formula || "");
  if (!expr) return "";
  const mode = data.gap_mode === "explicit"
    ? "source 명시 · 좌표가 같은 die 끼리"
    : "항목만 참조 · source 별로 각각";
  const dies = (data.matched_dies == null) ? "" : ` · die ${data.matched_dies}개`;
  const warn = (data.missing && data.missing.length)
    ? `<span class="idet-formula-warn">⚠ ${esc(data.missing.join(" · "))}</span>` : "";
  const dropped = data.dropped_nonfinite
    ? ` · 계산 불가 ${data.dropped_nonfinite}개 제외` : "";
  return `<div class="idet-formula">` +
    `<span class="idet-formula-label">수식</span>` +
    `<span class="idet-formula-expr">${expr}</span>${warn}` +
    `<span class="idet-formula-meta">${esc(mode + dies + dropped)}</span></div>`;
}

function gcRenderExpr() {
  const host = document.getElementById("gcExpr");
  if (host) {
    host.innerHTML = _gcTokens.length
      ? _gcTokens.map(gcTokenHtml).join("")
      : `<span class="gc-expr-hint">아래에서 항목·숫자·연산자를 넣어 수식을 만드세요</span>`;
  }
  gcRenderStatus();
}

function gcRenderStatus() {
  const host = document.getElementById("gcStatus");
  const v = gcValidate(_gcTokens);
  const mode = gcModeOf(_gcTokens);
  if (host) {
    if (!v.ok) {
      host.innerHTML = `<span class="gc-status-bad">⚠ ${esc(v.msg)}</span>`;
    } else {
      const label = mode === "explicit"
        ? "source 명시 — 좌표가 같은 die 끼리 계산해 곡선 1개"
        : `항목만 참조 — 고른 source 각각에서 계산해 곡선 ${_gcSelSources.size}개`;
      host.innerHTML = `<span class="gc-status-ok">✓ ${esc(label)}</span>` +
        `<span class="gc-status-formula">${esc(gcFormulaText(_gcTokens))}</span>`;
    }
  }
  const save = document.getElementById("gcSave");
  const name = String((document.getElementById("gcName") || {}).value || "").trim();
  const needSource = (mode !== "explicit") && !_gcSelSources.size;
  if (save) save.disabled = !(v.ok && name && !needSource);
}

// ── 토큰 커밋 ────────────────────────────────────────────────────────────────
function gcPush(tok) {
  if (_gcTokens.length >= GC_MAX_TOKENS) { showToast("수식이 너무 깁니다"); return; }
  _gcTokens.push(tok);
  gcRenderExpr();
}

function gcPopToken() {
  if (!_gcTokens.length) return;
  _gcTokens.pop();
  gcRenderExpr();
}

function gcRemoveToken(i) {
  if (i < 0 || i >= _gcTokens.length) return;
  _gcTokens.splice(i, 1);
  gcRenderExpr();
}

function gcPushOp(ch) {
  const kind = GC_OP_KEYS[ch];
  if (!kind) return;
  gcPush(kind === "lp" || kind === "rp" ? { t: kind } : { t: "op", v: kind });
}

// 항목 토큰 — source 드롭다운이 비어 있지 않으면 source 명시 참조가 된다.
function gcPushItem(subject, source) {
  const src = source != null ? source
                             : String((document.getElementById("gcSrcQual") || {}).value || "");
  const tok = { t: "item", item: String(subject) };
  if (src) tok.source = src;
  gcPush(tok);
  const input = document.getElementById("gcTokenInput");
  if (input) { input.value = ""; input.focus(); }
  _gcSuggest = []; _gcSuggestAt = -1;
  gcRenderSuggest();
}

// ── 수식 입력창 자동완성 ─────────────────────────────────────────────────────
// 입력어가 "<선택 source>_" 로 시작하면 그 source 로 한정해 나머지로 검색한다.
// 이 접두 추정은 **제안 랭킹에만** 쓴다 — 저장 경로는 항상 드롭다운/토큰 필드를 쓰므로
// source 명에 `_` 가 있어 잘못 갈려도 최악이 "제안이 안 뜬다" 뿐이다.
function gcSplitQualified(term) {
  for (const s of gcSourceNames()) {
    const pre = s + "_";
    if (term.length > pre.length && term.slice(0, pre.length) === pre) {
      return { source: s, rest: term.slice(pre.length) };
    }
  }
  return { source: "", rest: term };
}

function gcRenderSuggest() {
  const host = document.getElementById("gcSuggest");
  if (!host) return;
  if (!_gcSuggest.length) { host.innerHTML = ""; host.style.display = "none"; return; }
  host.style.display = "";
  host.innerHTML = _gcSuggest.map((r, i) =>
    `<button type="button" class="dist-sug-item gc-sug-item${i === _gcSuggestAt ? " active" : ""}"` +
    ` data-gc-sug="${i}">` +
    `<span class="sug-tno">${esc(r.test_num || "")}</span>` +
    `<span class="sug-name">${esc(r.subject)}</span>` +
    (r._source ? `<span class="gc-sug-src">${esc(r._source)}</span>` : "") +
    `</button>`).join("");
}

function gcUpdateSuggest(raw) {
  const term = String(raw || "").trim();
  if (!term) { _gcSuggest = []; _gcSuggestAt = -1; gcRenderSuggest(); return; }
  const { source, rest } = gcSplitQualified(term);
  const rows = distSuggestions(rest || term, 0).slice(0, GC_SUGGEST_MAX);
  _gcSuggest = rows.map(r => ({ ...r, _source: source }));
  _gcSuggestAt = _gcSuggest.length ? 0 : -1;
  gcRenderSuggest();
}

function gcCommitInput() {
  const input = document.getElementById("gcTokenInput");
  if (!input) return;
  if (_gcSuggestAt >= 0 && _gcSuggest[_gcSuggestAt]) {
    const pick = _gcSuggest[_gcSuggestAt];
    gcPushItem(pick.subject, pick._source || undefined);
    return;
  }
  const text = String(input.value || "").trim();
  if (!text) return;
  const n = Number(text);
  if (text !== "" && isFinite(n)) {
    gcPush({ t: "num", v: n });
    input.value = "";
    _gcSuggest = []; _gcSuggestAt = -1;
    gcRenderSuggest();
    return;
  }
  showToast("항목은 자동완성 목록에서 골라야 합니다");
}

// ── 저장 / 삭제 ──────────────────────────────────────────────────────────────
function gcCollectSpec() {
  const name = String((document.getElementById("gcName") || {}).value || "").trim();
  if (!name) throw new Error("차트 이름을 입력하세요");
  if (name.length > GC_NAME_MAX) throw new Error(`차트 이름이 너무 깁니다 (${GC_NAME_MAX}자 이하)`);
  const v = gcValidate(_gcTokens);
  if (!v.ok) throw new Error(v.msg);
  const sources = gcSourceNames().filter(s => _gcSelSources.has(s));
  if (gcModeOf(_gcTokens) !== "explicit" && !sources.length) {
    throw new Error("source 를 하나 이상 고르세요");
  }
  const numOf = value => {
    const t = String(value == null ? "" : value).trim();
    if (!t) return null;
    const n = Number(t);
    if (!isFinite(n)) throw new Error("Limit 은 숫자로 입력하세요");
    return n;
  };
  const lo = numOf((document.getElementById("gcLimitLo") || {}).value);
  const hi = numOf((document.getElementById("gcLimitHi") || {}).value);
  const limit = (lo == null && hi == null) ? { mode: "none" } : { mode: "manual", lo: lo, hi: hi };
  return { name: name, sources: sources, tokens: _gcTokens, limit: limit };
}

// 단발 POST — 모달 트랜잭션형 입력이라 autoSave 채널을 쓰지 않는다.
function gcPost(ops) {
  return fetch(`/pe/report/session/${SESSION_ID}/web_report/gap_charts`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
    body: JSON.stringify({ ops }),
  }).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    // 응답이 권위본 — 재로드(load) 없이 이것만 갈아끼운다(콜드 재빌드 회피).
    DATA.gap_charts = j.gap_charts || {};
    return j;
  });
}

function gcSaveFromModal() {
  let spec;
  try { spec = gcCollectSpec(); } catch (e) { showToast(e.message); return; }
  const id = _gcEditId || gcNewId();
  const btn = document.getElementById("gcSave");
  if (btn) btn.disabled = true;
  gcPost([{ key: id, value: spec }])
    .then(() => {
      gcDropCache(id);            // 수식이 바뀌었으면 이 차트 계산 결과만 버린다
      gcCloseModal();
      const q = (document.getElementById("distSearch") || {}).value || "";
      distRenderGallery();
      if (typeof restoreDistSearch === "function") restoreDistSearch(q);
      showToast("Gap Chart 를 저장했습니다");
    })
    .catch(e => { showToast("저장 실패: " + e.message); if (btn) btn.disabled = false; });
}

function gcDelete(id) {
  const chart = gcGet(id);
  if (!chart) return;
  if (!confirm(`Gap Chart "${chart.name || id}" 를 삭제할까요?`)) return;
  gcPost([{ key: id, value: null }])
    .then(() => {
      gcDropCache(id);
      gcCloseModal();
      const q = (document.getElementById("distSearch") || {}).value || "";
      distRenderGallery();
      if (typeof restoreDistSearch === "function") restoreDistSearch(q);
      showToast("삭제했습니다");
    })
    .catch(e => showToast("삭제 실패: " + e.message));
}

// UUID — 서버 정규식 ^[0-9a-fA-F-]{8,40}$ 를 만족해야 한다(dcNewId 와 같은 폴백).
function gcNewId() {
  if (window.crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const h = () => Math.floor(Math.random() * 65536).toString(16).padStart(4, "0");
  return `${h()}${h()}-${h()}-${h()}-${h()}-${h()}${h()}${h()}`;
}

// ── 상세 — **기존 Item_detail 을 그대로 재사용** ──────────────────────────────
// 서버 응답이 /scatter 와 같은 구조라 화면 코드가 필요 없다. URL 만 갈아끼운다.
function gcOpenDetail(id) {
  const chart = gcGet(id);
  if (!chart) return;
  const name = chart.name || "Gap";
  openItemDetail(name, [name],
                 { url: `/pe/report/session/${SESSION_ID}/web_report/gap_chart/${encodeURIComponent(id)}` });
}

// ── 패널 위임 (distBindPanel 이 .distg-card 분기보다 **앞에서** 호출한다) ─────
function gcPanelClick(e) {
  const act = e.target.closest("[data-gc-act]");
  if (act) {
    const kind = act.dataset.gcAct;
    if (typeof dcCloseMenu === "function") dcCloseMenu();
    if (kind === "edit") { gcOpenModal(act.dataset.gapId); return true; }
    if (kind === "del") { gcDelete(act.dataset.gapId); return true; }
    return true;
  }
  const card = e.target.closest(".distg-gap");
  if (card) { gcOpenDetail(card.dataset.gapId); return true; }
  return false;
}

// ── 모달 이벤트 (재렌더되므로 문서 레벨 위임 1회) ────────────────────────────
document.addEventListener("click", e => {
  const modal = e.target.closest("#gcModal");
  if (!modal) return;
  if (e.target === modal) { gcCloseModal(); return; }        // 오버레이 클릭
  if (e.target.closest("#gcCancel")) { gcCloseModal(); return; }
  if (e.target.closest("#gcSave")) { gcSaveFromModal(); return; }
  if (e.target.closest("#gcDelete")) { if (_gcEditId) gcDelete(_gcEditId); return; }
  const srcAll = e.target.closest("[data-gc-src-all]");
  if (srcAll) {
    const on = srcAll.dataset.gcSrcAll === "1";
    _gcSelSources = new Set(on ? gcSourceNames() : []);
    gcRenderSources();
    gcRenderStatus();
    return;
  }
  const op = e.target.closest("[data-gc-op]");
  if (op) { gcPushOp(op.dataset.gcOp); return; }
  const back = e.target.closest('[data-gc-act="pop"]');
  if (back) { gcPopToken(); return; }
  const clear = e.target.closest('[data-gc-act="clear"]');
  if (clear) { _gcTokens = []; gcRenderExpr(); return; }
  const tok = e.target.closest("[data-gc-tok]");
  if (tok) { gcRemoveToken(parseInt(tok.dataset.gcTok, 10)); return; }
  const sug = e.target.closest("[data-gc-sug]");
  if (sug) {
    const pick = _gcSuggest[parseInt(sug.dataset.gcSug, 10)];
    if (pick) gcPushItem(pick.subject, pick._source || undefined);
    return;
  }
  const pickBtn = e.target.closest("[data-gc-pick]");
  if (pickBtn) { gcPushItem(pickBtn.dataset.gcPick); return; }
});

document.addEventListener("change", e => {
  if (!e.target.closest || !e.target.closest("#gcModal")) return;
  const src = e.target.closest(".gc-src-chk");
  if (src) {
    if (src.checked) _gcSelSources.add(src.dataset.source);
    else _gcSelSources.delete(src.dataset.source);
    gcRenderStatus();
  }
});

document.addEventListener("input", e => {
  if (e.target.id === "gcItemSearch") {
    const q = e.target.value;
    clearTimeout(_gcListTimer);
    _gcListTimer = setTimeout(() => gcRenderItemList(q), 200);
    return;
  }
  if (e.target.id === "gcTokenInput") {
    const q = e.target.value;
    clearTimeout(_gcSearchTimer);
    _gcSearchTimer = setTimeout(() => gcUpdateSuggest(q), 200);
    return;
  }
  if (e.target.id === "gcName") gcRenderStatus();
});

// 수식 입력창 키 처리 — **입력창이 비어 있을 때만** 연산자 키가 토큰이 된다
// (글자가 있을 때의 `-` 는 item 이름의 일부일 수 있다).
document.addEventListener("keydown", e => {
  const input = e.target;
  if (!input || input.id !== "gcTokenInput") return;
  const empty = String(input.value || "") === "";
  if (empty && Object.prototype.hasOwnProperty.call(GC_OP_KEYS, e.key)) {
    e.preventDefault();
    gcPushOp(e.key);
    return;
  }
  if (e.key === "Backspace" && empty) { e.preventDefault(); gcPopToken(); return; }
  if (e.key === "Enter") { e.preventDefault(); gcCommitInput(); return; }
  if (e.key === "ArrowDown" && _gcSuggest.length) {
    e.preventDefault();
    _gcSuggestAt = Math.min(_gcSuggest.length - 1, _gcSuggestAt + 1);
    gcRenderSuggest();
    return;
  }
  if (e.key === "ArrowUp" && _gcSuggest.length) {
    e.preventDefault();
    _gcSuggestAt = Math.max(0, _gcSuggestAt - 1);
    gcRenderSuggest();
  }
});

document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  const modal = document.getElementById("gcModal");
  if (modal && modal.classList.contains("show")) gcCloseModal();
});

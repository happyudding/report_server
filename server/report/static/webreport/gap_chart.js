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
// **수식은 평문으로 타이핑한다 (2026-08-26 개편 — Honey 클라의
// honey_ui/formula_editor.py 와 같은 입력 방식).** 입력은 평범한 <input> 하나이고 그
// 텍스트가 입력 정본이다. 위쪽 칩 영역은 렉싱 결과를 보여 주는 **읽기 전용 해석 창**이다.
// item 이름에 공백·괄호·연산자가 전부 합법이라(honeyform 은 중복·메타충돌만 검사) 평문에서
// 항목을 *찾아낼* 수는 없으므로, 항목은 오직 `@` 자동완성이 넣는 `@"항목명"` 인용 표기로만
// 들어온다(이름 안의 `"` 는 `""` 로 escape, source 명시는 `@"source"!"항목명"` — Excel 의
// Sheet!Cell 표기). 인용 밖은 숫자·`+ - * / ( )` 뿐이라 매 키입력 재렉싱이 모호성 0 으로
// 가능하다 — client 렉서(web_report/formula.py lex)와 같은 원리를 gap 문법(함수·비교 없는
// 부분집합)으로 옮긴 것이 gcLex 다. **저장 정본은 여전히 토큰 배열**이고, 수정 모달은
// tokens → gcTokensToText 로 원문을 복원한다(라운드트립).

const GC_MAX_TOKENS = 200;          // 서버 gap_chart.MAX_TOKENS 와 같은 값
const GC_MAX_DEPTH = 16;            // 서버 gap_chart.MAX_DEPTH
const GC_MAX_REFS = 20;             // 서버 gap_chart.MAX_REFS
const GC_NAME_MAX = 120;
const GC_LIST_MAX = 200;            // 왼쪽 항목 목록 표시 상한
const GC_SUGGEST_MAX = 30;          // 수식 입력 자동완성 표시 개수
const GC_TEXT_MAX = 4000;           // 입력 원문 상한 — web_report.formula MAX_TEXT 와 동일
const GC_MENTION_MAX = 60;          // `@` 뒤 검색어 최대 길이 — formula_editor._MENTION_MAX 와 동일
const GC_OP_TEXT = { "+": "+", "-": "−", "*": "×", "/": "÷" };
const GC_OP_ALIAS = { "−": "-", "×": "*", "÷": "/" };  // 칩 표시 문자를 붙여넣어도 받아 준다

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
// ⚠️ 캐시 키는 **bin1 축 3종 그대로**다 — Distribution "Serial 순" 토글이 생겼지만
// `/gap_chart/<id>` 응답은 두 모드가 **같다**(서버는 order 를 모르고, 값은 이미 rawdata 행
// 순서다). 프런트가 같은 응답에서 ECDF(entry)와 Serial 순(seqEntry) 두 그림을 만든다.
// seq 키를 여기 넣지 말 것 — 아래 gcDropCache 의 키 목록과 어긋나면 수식을 고쳐도 옛 값이 남는다.
const _gcCache = { all: {}, bin1: {}, rtbin1: {} };
const _gcInflight = { all: new Set(), bin1: new Set(), rtbin1: new Set() };

function gcCacheFor(variant) { return _gcCache[distVariantKey(variant)] || _gcCache.all; }

// cdf(정렬 결과 order 포함)와 원본 시리즈를 함께 들고 있는다 — Map 선택 좌표 마커가
// "이 좌표의 gap 값과 그 누적%" 를 찾을 때 필요하다(gcChipHits).
// seqEntry(Serial 순 표시용 원본 값 배열)도 **여기서 한 번만** 만든다 — distSeqDisplayPoints
// 가 entry 객체를 WeakMap 키로 메모하므로 렌더마다 새로 만들면 메모가 무의미해진다.
function gcBuildSeries(data) {
  return ((data && data.sources) || []).map(s => {
    const c = distCdfFromValues(s.values || []);
    return { name: s.name, entry: { xs: c.x, ys: c.y }, seqEntry: { vs: s.values || [] },
             cdf: c, src: s };
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

// 실패한 차트 — `${variantKey}\x1f${id}` → 사유. 이 기록이 없으면 IntersectionObserver
// 재관측·툴바 토글·맵 칩 선택마다 같은 요청이 다시 나가 서버가 계속 503/500 을 내는 동안
// 토스트가 카드 수만큼 반복된다(일반 배치의 _distBatchFailed 배지와 같은 억제 장치).
const _gcFailed = {};
function gcFailKey(variant, id) { return distVariantKey(variant) + "\x1f" + id; }

function gcEnsureChart(id, variant) {
  const key = distVariantKey(variant);
  const store = _gcCache[key], inflight = _gcInflight[key];
  if (store[id] || inflight.has(id) || _gcFailed[gcFailKey(key, id)]) return;
  inflight.add(id);
  const q = distVariantQuery(key).replace(/^&/, "?");
  const url = `/pe/report/session/${SESSION_ID}/web_report/gap_chart/${encodeURIComponent(id)}${q}`;
  fetchJson202(url, { shouldStop: () => !gcGet(id) })
    .then(data => {
      delete _gcFailed[gcFailKey(key, id)];
      store[id] = { data: data, series: gcBuildSeries(data) };
      gcRefresh();
    })
    .catch(e => {
      // 카드 안에 사유 + 재시도 버튼으로 남긴다(토스트는 그 카드 1회분).
      _gcFailed[gcFailKey(key, id)] = e.message || "계산 실패";
      showToast("Gap Chart 계산 실패: " + e.message);
      gcRefresh();
    })
    .then(() => { inflight.delete(id); });
}

// 재시도 — 실패 기록만 지우고 다시 그리면 gcRenderGapCell 이 요청을 새로 낸다.
function gcRetry(id) {
  delete _gcFailed[gcFailKey(distGalleryVariant(), id)];
  gcRefresh();
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
  ["all", "bin1", "rtbin1"].forEach(k => {
    delete _gcCache[k][id];
    delete _gcFailed[k + "\x1f" + id];   // 수식을 고쳤으면 옛 실패 기록도 함께 버린다
  });
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
    // 헤더도 **지금 보고 있는 변형**의 캐시를 봐야 한다 — all 로 고정하면 Bin1 계열
    // 토글에서는 채워지는 store 가 달라 die 수가 영원히 "계산 중…" 으로 남는다.
    const variant = distGalleryVariant();
    const cached = (gcCacheFor(variant)[id] || {}).data;
    const dies = cached ? `die ${cached.matched_dies}개`
      : (_gcFailed[gcFailKey(variant, id)] ? "계산 실패" : "계산 중…");
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
  const failed = _gcFailed[gcFailKey(variant, id)];
  if (failed) {   // 자동 재요청하지 않는다 — 사용자가 누를 때만(요청 폭주·토스트 반복 차단)
    plot.innerHTML = `<div class="placeholder gc-cell-empty">계산 실패 (${esc(failed)})` +
      `<br><button type="button" class="btn-sm" data-gc-act="retry" data-gap-id="${esc(id)}">다시 시도</button></div>`;
    cell.dataset.rendered = "1";
    return;
  }
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
  const { lo, hi } = gcLimitOf(chart);
  // Serial 순 — 값이 이미 rawdata 행 순서라 seqEntry 를 표시 좌표로 바꾸기만 하면 된다
  // (per_source = 그 source 의 행 순서 / explicit = 첫 참조 source 행 순서의 좌표 교집합).
  // 선택 좌표 마커는 (값, 누적%) 좌표라 이 축에서 위치가 어긋나므로 붙이지 않는다 —
  // 일반 항목 seq 미니셀이 chipMarkersFor 를 제외하는 것과 같은 규칙.
  if (distSeqOnly) {
    const seqPts = {};
    series.forEach(s => { seqPts[s.name] = distSeqDisplayPoints(s.seqEntry, cap); });
    const b = distSeqBounds(seqPts);
    const seqSentinel = distSeqSentinelTrace(b);
    Plotly.newPlot(plot, seqSentinel ? [seqSentinel] : [],
                   distSeqCellLayout("ok", lo, hi, b), DIST_CFG_STATIC);
    if (chart && gcModeOf(chart.tokens) === "explicit") plot._distColorFor = () => "#7C3AED";
    distPaintPoints(plot, seqPts, null);
    cell.dataset.rendered = "1";
    return;
  }
  const pts = {};
  series.forEach(s => { pts[s.name] = distDisplayPoints(s.entry, cap); });
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
let _gcTokens = [];                    // 수식 토큰 (입력창 텍스트를 gcLex 한 결과)
let _gcLexError = null;                // null | {message, span} — 렉싱/인용 오류
let _gcLexWarns = [];                  // 목록에 없는 항목·source 경고 (저장은 막지 않는다)
let _gcSelSources = new Set();
let _gcSuggest = [];                   // 현재 `@` 자동완성 후보
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
  // 수정 모달은 토큰 → 평문을 복원해 입력창에 넣는다 (gcLex 라운드트립).
  const finput = document.getElementById("gcFormulaInput");
  if (finput) finput.value = chart ? gcTokensToText(chart.tokens || []) : "";
  _gcSuggest = []; _gcSuggestAt = -1;

  gcRenderSources();
  gcRenderSrcQual();
  gcRenderItemList("");
  gcRelex();
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

// 토큰 1개의 클래스+내용. 모달 해석 창과 Item_detail 수식 줄이 공유한다(둘 다 읽기 전용) —
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

// 읽기 전용 수식 렌더 — 모달 해석 창과 Item_detail 헤더가 함께 쓴다.
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
    let html = _gcTokens.length ? gcExprHtml(_gcTokens) : "";
    if (_gcLexError) {
      html += `<span class="gc-expr-bad" title="${esc(_gcLexError.message)}">⚠</span>`;
    }
    host.innerHTML = html
      || `<span class="gc-expr-hint">읽은 결과가 여기 보입니다 — 아래 입력창에 수식을 그대로 적으세요 (항목은 @)</span>`;
  }
  gcRenderStatus();
}

function gcRenderStatus() {
  const host = document.getElementById("gcStatus");
  const mode = gcModeOf(_gcTokens);
  // 렉싱 오류가 문법 오류보다 먼저다 — 토큰이 반쪽이라 gcValidate 메시지는 헛짚는다.
  let bad = _gcLexError ? _gcLexError.message : "";
  if (!bad) {
    const v = gcValidate(_gcTokens);
    if (!v.ok) bad = v.msg;
  }
  if (host) {
    const warns = _gcLexWarns.map(w =>
      `<span class="gc-status-warn">⚠ ${esc(w)}</span>`).join("");
    if (bad) {
      host.innerHTML = `<span class="gc-status-bad">⚠ ${esc(bad)}</span>` + warns;
    } else {
      const label = mode === "explicit"
        ? "source 명시 — 좌표가 같은 die 끼리 계산해 곡선 1개"
        : `항목만 참조 — 고른 source 각각에서 계산해 곡선 ${_gcSelSources.size}개`;
      host.innerHTML = `<span class="gc-status-ok">✓ ${esc(label)}</span>` +
        `<span class="gc-status-formula">${esc(gcFormulaText(_gcTokens))}</span>` + warns;
    }
  }
  const save = document.getElementById("gcSave");
  const name = String((document.getElementById("gcName") || {}).value || "").trim();
  const needSource = (mode !== "explicit") && !_gcSelSources.size;
  if (save) save.disabled = !!(bad || !name || needSource);
}

// ── 평문 렉서 — client FormulaEditor(web_report/formula.py lex)와 같은 인용 규칙 ──
// 입력 정본은 입력창의 **텍스트**다. 항목은 `@"이름"`(이름 안 `"` 는 `""`), source 명시는
// `@"source"!"항목명"`. 인용 밖은 숫자·`+ - * / ( )` 뿐이라 재렉싱에 모호성이 없다
// (gap 은 함수·비교가 없는 부분집합이라 서버 formula.py 를 그대로 포팅하지 않는다).

function gcQuoteName(name) { return '"' + String(name).replace(/"/g, '""') + '"'; }

// 토큰 → 입력 원문. gcLex(gcTokensToText(t)) 가 같은 토큰이 되는 라운드트립이 수정 모달의
// 전제다. 예외는 음수 num 토큰 하나 — op(-)+num 으로 갈라지는데 서버 재귀하강 파서의
// 단항 규칙(parse_unary)상 등가라 계산 결과가 같다.
function gcTokensToText(tokens) {
  const parts = (tokens || []).map(t => {
    if (t.t === "num") return gcNumText(t.v);
    if (t.t === "item") {
      return "@" + (t.source ? gcQuoteName(t.source) + "!" : "") + gcQuoteName(t.item);
    }
    if (t.t === "op") return t.v;
    return t.t === "lp" ? "(" : ")";
  });
  return parts.join(" ").replace(/\( /g, "(").replace(/ \)/g, ")");
}

const GC_NUM_RE = /(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?/y;

// 인용 이름 → 목록의 정식 이름(대소문자는 조회에만 관대 — formula._resolve_item 규약).
// **없어도 오류로 막지 않는다** — 전처리 제외 등으로 목록에서 빠진 항목을 참조하는 기존
// 차트를 열어 이름·Limit 만 고치는 길이 막히면 안 된다(§5-12, dist_composite 의
// "목록으로 filter 하지 말 것"과 같은 취지). 조회 시점 부재는 서버가 missing 으로 알린다.
function gcResolveName(name, known, kind, warns) {
  if (known.indexOf(name) >= 0) return name;
  const low = name.toLowerCase();
  const hits = known.filter(k => k.toLowerCase() === low);
  if (hits.length === 1) return hits[0];
  warns.push(`'${name}' — 목록에 없는 ${kind}`);
  return name;
}

function gcLexQuoted(raw, quoteAt, fail, refStart) {
  let i = quoteAt + 1, buf = "";
  for (;;) {
    if (i >= raw.length) fail('항목 이름의 닫는 " 가 없습니다', [refStart, raw.length]);
    const ch = raw[i];
    if (ch === '"') {
      if (raw[i + 1] === '"') { buf += '"'; i += 2; continue; }
      return { name: buf, end: i + 1 };
    }
    buf += ch;
    i += 1;
  }
}

// 평문 → {tokens, warns}. 위반은 Error(.span, .tokens=거기까지 읽은 토큰, .warns) throw —
// 오류가 나도 앞부분 칩 표시를 유지한다(formula_editor 의 부분 렉싱과 같은 동작).
function gcLex(text) {
  const raw = String(text || "");
  const items = (typeof distIndex !== "undefined" ? distIndex : []).map(r => r.subject);
  const sources = gcSourceNames();
  const tokens = [], warns = [];
  const fail = (msg, span) => {
    const e = new Error(msg);
    e.span = span; e.tokens = tokens; e.warns = warns;
    throw e;
  };
  if (raw.length > GC_TEXT_MAX) fail(`수식이 너무 깁니다 (${GC_TEXT_MAX}자 이하)`, [0, raw.length]);
  let i = 0;
  const n = raw.length;
  while (i < n) {
    const ch = raw[i];
    if (/\s/.test(ch)) { i += 1; continue; }
    const start = i;
    if (ch === "@") {
      if (raw[i + 1] !== '"') {
        fail('항목은 @ 를 치고 목록에서 고르세요 (@"항목명" 형태)', [start, Math.min(n, i + 2)]);
      }
      const first = gcLexQuoted(raw, i + 1, fail, start);
      let source = "", item, end = first.end;
      if (raw[end] === "!") {
        if (raw[end + 1] !== '"') {
          fail('source 명시는 @"source"!"항목명" 형태입니다', [start, Math.min(n, end + 2)]);
        }
        const second = gcLexQuoted(raw, end + 1, fail, start);
        source = gcResolveName(first.name, sources, "source", warns);
        item = gcResolveName(second.name, items, "항목", warns);
        end = second.end;
      } else {
        item = gcResolveName(first.name, items, "항목", warns);
      }
      const tok = { t: "item", item: item };
      if (source) tok.source = source;
      tokens.push(tok);
      i = end;
      continue;
    }
    GC_NUM_RE.lastIndex = i;
    const m = GC_NUM_RE.exec(raw);
    if (m) {
      i = GC_NUM_RE.lastIndex;
      tokens.push({ t: "num", v: Number(m[0]) });
      continue;
    }
    const op = GC_OP_ALIAS[ch] || ch;
    if (op === "(") { tokens.push({ t: "lp" }); i += 1; continue; }
    if (op === ")") { tokens.push({ t: "rp" }); i += 1; continue; }
    if ("+-*/".indexOf(op) >= 0) { tokens.push({ t: "op", v: op }); i += 1; continue; }
    fail(`'${ch}' 는 수식에 쓸 수 없는 문자입니다 — 항목이라면 @ 로 넣으세요`, [start, start + 1]);
  }
  return { tokens: tokens, warns: warns };
}

// 입력창 텍스트 → 토큰·오류·경고 갱신. 이 모달의 수식 상태는 전부 여기서 정해진다.
function gcRelex() {
  const input = document.getElementById("gcFormulaInput");
  const text = input ? String(input.value || "") : "";
  _gcLexError = null;
  _gcLexWarns = [];
  if (!text.trim()) {
    _gcTokens = [];             // 아직 아무것도 안 쳤다 — 오류로 띄우지 않는다
  } else {
    try {
      const out = gcLex(text);
      _gcTokens = out.tokens;
      _gcLexWarns = out.warns;
    } catch (e) {
      _gcTokens = e.tokens || [];
      _gcLexWarns = e.warns || [];
      _gcLexError = { message: e.message || "수식을 읽을 수 없습니다", span: e.span || null };
    }
  }
  gcRenderExpr();
}

// ── `@` 자동완성 (formula_editor._mention_query 와 같은 판정) ────────────────
// 커서 앞 마지막 `@` 뒤 조각이 검색어다. 완성된 `@"이름"` 은 조각에 `"` 가 들어 있으므로
// 자연히 걸러지고, 너무 긴 조각은 `@` 를 치다 만 흔적으로 보고 닫는다.
function gcMentionQuery() {
  const input = document.getElementById("gcFormulaInput");
  if (!input) return null;
  const value = String(input.value || "");
  const pos = input.selectionStart == null ? value.length : input.selectionStart;
  const at = value.slice(0, pos).lastIndexOf("@");
  if (at < 0) return null;
  const frag = value.slice(at + 1, pos);
  if (frag.indexOf('"') >= 0 || frag.length > GC_MENTION_MAX) return null;
  return { frag: frag, at: at, pos: pos };
}

// 항목 삽입 — `@검색어` 조각이 있으면 그 자리를 `@"이름"` 으로 바꾸고, 없으면(왼쪽 목록
// 클릭) 커서 위치에 넣는다. source 드롭다운이 비어 있지 않으면 source 명시 참조가 된다.
function gcInsertItem(subject, source) {
  const input = document.getElementById("gcFormulaInput");
  if (!input) return;
  const src = source != null ? source
                             : String((document.getElementById("gcSrcQual") || {}).value || "");
  const snippet = "@" + (src ? gcQuoteName(src) + "!" : "") + gcQuoteName(String(subject));
  const value = String(input.value || "");
  const q = gcMentionQuery();
  let start, end;
  if (q) { start = q.at; end = q.pos; }
  else if (input.selectionStart != null) { start = input.selectionStart; end = input.selectionEnd; }
  else { start = value.length; end = value.length; }
  input.value = value.slice(0, start) + snippet + value.slice(end);
  const caret = start + snippet.length;
  input.focus();
  try { input.setSelectionRange(caret, caret); } catch (e) { /* no-op */ }
  gcRelex();
  gcUpdateSuggest();   // 삽입으로 `@` 조각이 사라졌으니 팝업이 닫힌다
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

// `@` 조각이 있을 때만 후보를 낸다. 빈 조각(막 `@` 만 친 상태)은 전체 목록 앞부분 —
// formula_editor 의 force_all 과 같은 동작이다. 후보에는 드롭다운의 source 를 미리 태워
// 어떤 참조로 들어갈지(_source 배지) 보여 준다.
function gcUpdateSuggest() {
  const q = gcMentionQuery();
  if (!q) { _gcSuggest = []; _gcSuggestAt = -1; gcRenderSuggest(); return; }
  const term = q.frag.trim();
  const src = String((document.getElementById("gcSrcQual") || {}).value || "");
  const all = typeof distIndex !== "undefined" ? distIndex : [];
  const rows = (term ? distSuggestions(term, 0) : all).slice(0, GC_SUGGEST_MAX);
  _gcSuggest = rows.map(r => ({ subject: r.subject, test_num: r.test_num, _source: src }));
  _gcSuggestAt = _gcSuggest.length ? 0 : -1;
  gcRenderSuggest();
}

// ── 저장 / 삭제 ──────────────────────────────────────────────────────────────
function gcCollectSpec() {
  const name = String((document.getElementById("gcName") || {}).value || "").trim();
  if (!name) throw new Error("차트 이름을 입력하세요");
  if (name.length > GC_NAME_MAX) throw new Error(`차트 이름이 너무 깁니다 (${GC_NAME_MAX}자 이하)`);
  if (_gcLexError) throw new Error(_gcLexError.message);
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
function gcGapUrl(id) {
  return `/pe/report/session/${SESSION_ID}/web_report/gap_chart/${encodeURIComponent(id)}`;
}

function gcOpenDetail(id) {
  const chart = gcGet(id);
  if (!chart) return;
  const name = chart.name || "Gap";
  // 이 세션의 Gap Chart 끼리 prev/next(Alt+↑/↓) 이동 — nav 는 이름, URL 은 이름→id 로 되짚는다.
  // 이름이 겹치면 먼저 만든 쪽으로 열린다(이름은 표시용이고 저장 키는 UUID 라 데이터는 안전).
  const all = gcAll();
  const ids = gcSortedIds();
  const byName = new Map();
  ids.forEach(i => {
    const nm = (all[i] || {}).name || "Gap";
    if (!byName.has(nm)) byName.set(nm, i);
  });
  const nav = Array.from(byName.keys());
  openItemDetail(name, nav.length > 1 ? nav : [name], {
    url: gcGapUrl(id),
    urlOf: nm => gcGapUrl(byName.has(nm) ? byName.get(nm) : id),
  });
}

// ── 패널 위임 (distBindPanel 이 .distg-card 분기보다 **앞에서** 호출한다) ─────
function gcPanelClick(e) {
  const act = e.target.closest("[data-gc-act]");
  if (act) {
    const kind = act.dataset.gcAct;
    if (typeof dcCloseMenu === "function") dcCloseMenu();
    if (kind === "edit") { gcOpenModal(act.dataset.gapId); return true; }
    if (kind === "del") { gcDelete(act.dataset.gapId); return true; }
    if (kind === "retry") { gcRetry(act.dataset.gapId); return true; }
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
  // ⚠ 배경(오버레이) 클릭으로는 닫지 않는다 — 조립한 수식 토큰이 통째로 날아간다
  //    (dist_composite 모달이 같은 이유로 이미 막아 둔 규칙). 닫기는 취소 버튼과 Esc 뿐이다.
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
  const sug = e.target.closest("[data-gc-sug]");
  if (sug) {
    const pick = _gcSuggest[parseInt(sug.dataset.gcSug, 10)];
    if (pick) gcInsertItem(pick.subject, pick._source || undefined);
    return;
  }
  const pickBtn = e.target.closest("[data-gc-pick]");
  if (pickBtn) { gcInsertItem(pickBtn.dataset.gcPick); return; }
  // 입력창 클릭 = 커서 이동 — `@` 조각 판정이 바뀌므로 후보 팝업을 갱신한다.
  if (e.target.id === "gcFormulaInput") { gcUpdateSuggest(); return; }
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
  if (e.target.id === "gcFormulaInput") {
    clearTimeout(_gcSearchTimer);
    _gcSearchTimer = setTimeout(() => { gcRelex(); gcUpdateSuggest(); }, 120);
    return;
  }
  if (e.target.id === "gcName") gcRenderStatus();
});

// 수식 입력창 키 처리 — Enter=후보 확정(제출·커밋 없음), ↑↓=후보 이동, Esc=팝업만 닫기.
// 한글 조합 중(isComposing)에는 가로채지 않는다 — formula_editor._ImeTextEdit 과 같은 가드
// (조합 확정 Enter 가 후보 확정으로 오인되면 엉뚱한 항목이 들어간다).
document.addEventListener("keydown", e => {
  const input = e.target;
  if (!input || input.id !== "gcFormulaInput") return;
  if (e.isComposing) return;
  if (e.key === "Enter") {
    e.preventDefault();
    if (_gcSuggestAt >= 0 && _gcSuggest[_gcSuggestAt]) {
      const pick = _gcSuggest[_gcSuggestAt];
      gcInsertItem(pick.subject, pick._source || undefined);
    }
    return;
  }
  if (e.key === "Escape" && _gcSuggest.length) {
    // 팝업만 닫는다 — 아래 모달 Esc 핸들러(나중 등록이라 stopImmediatePropagation 이
    // 막는다)까지 가면 조립하던 수식째로 모달이 닫혀 버린다.
    e.preventDefault();
    e.stopImmediatePropagation();
    _gcSuggest = []; _gcSuggestAt = -1;
    gcRenderSuggest();
    return;
  }
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && _gcSuggest.length) {
    e.preventDefault();
    _gcSuggestAt = e.key === "ArrowDown"
      ? Math.min(_gcSuggest.length - 1, _gcSuggestAt + 1)
      : Math.max(0, _gcSuggestAt - 1);
    gcRenderSuggest();
  }
});

document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  const modal = document.getElementById("gcModal");
  if (modal && modal.classList.contains("show")) gcCloseModal();
});

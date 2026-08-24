// ── Distribution composite (합성 산포 차트) ───────────────────────────────────
// 사용자가 고른 source × TestItem 조합을 **한 차트에 겹쳐** 그린다. 조합 하나(pair)가
// legend 1개이고 이름은 "<source>_<item>", 색은 생성 시 배정해 저장한다(리로드 불변).
//
// 데이터는 새로 만들지 않는다 — 기존 ECDF 배치 API(distribution_batch)를 그대로 쓰고
// item 별 응답의 bySource 에서 고른 source 만 골라 그린다(서버 계산 추가 없음).
// 정의(이름/pairs/limit/colors)만 세션 편집 DB(kind=dist_composite)에 저장한다.
//
// 규칙 준수:
//  · §5 다운샘플 금지 — 카드는 표시용(distDisplayPoints) 만, detail 은 ECDF 전량.
//    전 차트 markers 전용(선·line.shape:"hv" 금지).
//  · §5-12 저장 키 불변 — item_key 는 생성 UUID, pairKey 구분자는 U+001F.
//  · 저장은 ops 배열 1회 POST(rev 1회 증가) + 응답 권위본으로 DATA 갱신(재로드 금지 —
//    report payload 는 kind 가 PAYLOAD_NEUTRAL 이라 캐시가 살아 있다).
const DC_SEP = "\x1f";                 // pairKey 구분자 (서버 _DC_PAIR_SEP 와 동일)
const DC_MAX_PAIRS = 200;              // 서버 _DC_MAX_PAIRS 와 동일 — 렌더 비용·저장 크기 기준
const DC_NAME_MAX = 120;
const DC_FETCH_CHUNK = 30;             // distribution_batch 한 요청 항목 수 (DIST_BATCH.SIZE 와 동일)
// 검색 결과 목록 표시 상한 — 목록이 자체 스크롤이라 넉넉히 둔다(전체 선택은 일치 전량 대상).
const DC_LIST_MAX = 300;

function dcPairKey(source, item) { return source + DC_SEP + item; }
function dcPairLabel(source, item) { return source + "_" + item; }

// 저장된 정의 목록 — /full extras 로 오는 권위본. {uuid: {name,pairs,limit,colors,...}}
function dcAll() { return (DATA && DATA.dist_composites) || {}; }
function dcGet(id) { return dcAll()[id] || null; }
// 만든 순서를 알 수 없으므로(dict) 이름 순으로 안정 정렬해 카드 위치가 튀지 않게 한다.
function dcSortedIds() {
  return Object.keys(dcAll()).sort((a, b) => {
    const na = String((dcAll()[a] || {}).name || ""), nb = String((dcAll()[b] || {}).name || "");
    return na < nb ? -1 : na > nb ? 1 : (a < b ? -1 : 1);
  });
}

// ── 색 배정 ──────────────────────────────────────────────────────────────────
// distDefaultColor(i)(황금비 hue 분산 공식)를 **랜덤 시작 오프셋**부터 순차로 뽑는다.
// 공식이 인접 인덱스 간 색상차를 보장하므로 "랜덤이되 서로 구분되는" 요구가 충족되고,
// 순수 난수(hue 무작위)처럼 두 legend 가 비슷한 색으로 겹치는 일이 없다.
const DC_COLOR_SPAN = 48;              // 공식이 고유 색을 주는 실질 범위
function dcAssignColors(pairs, keep) {
  const out = {};
  const used = new Set();
  (pairs || []).forEach(p => {
    const key = dcPairKey(p.source, p.item);
    const old = keep && keep[key];
    if (old) { out[key] = old; used.add(old); }
  });
  let i = Math.floor(Math.random() * DC_COLOR_SPAN);
  (pairs || []).forEach(p => {
    const key = dcPairKey(p.source, p.item);
    if (out[key]) return;
    let c = distDefaultColor(i % DC_COLOR_SPAN), guard = 0;
    while (used.has(c) && guard++ < DC_COLOR_SPAN) { i++; c = distDefaultColor(i % DC_COLOR_SPAN); }
    out[key] = c; used.add(c); i++;
  });
  return out;
}
function dcColorFor(comp, key) { return (comp && comp.colors && comp.colors[key]) || "#888888"; }

// ── ECDF 데이터 (기존 배치 API 재사용 + 고정 캐시) ────────────────────────────
// distDataCache 계열은 LRU(DIST_BATCH.CACHE_MAX 300) 라 스크롤 중 축출된다. composite 가
// 참조하는 항목은 카드가 화면에 남아 있는 한 계속 필요하므로 축출되지 않는 별도 맵에
// 보유한다(정의 상한 50×40 이라 힙은 유계). 공용 캐시에도 함께 넣어 일반 카드가 재사용한다.
// 변형 키는 distribution.js 의 DIST_VARIANTS 를 그대로 따른다 — bin1 축 3종 × 정렬 축 2종
// (기본 ECDF / "seq-" = Serial 순). 리터럴로 적으면 변형이 늘 때마다 여기가 조용히 빠져
// `_dcCache[key]` 가 undefined 가 되고 합성 카드만 죽는다.
const _dcCache = {};
const _dcInflight = {};
DIST_VARIANTS.forEach(k => { _dcCache[k] = {}; _dcInflight[k] = new Set(); });

function dcCacheFor(variant) { return _dcCache[distVariantKey(variant)] || _dcCache.all; }

// 실패한 항목 — `${variantKey}\x1f${item}` → 사유. 기록해 두지 않으면 셀이 렌더 완료로
// 표시되지 않아 IntersectionObserver 가 볼 때마다 같은 배치를 다시 요청하고, 서버가 계속
// 실패하는 동안 토스트가 반복된다(gap 카드·일반 배치 배지와 같은 억제 장치).
const _dcFailed = {};
function dcFailKey(variant, item) { return distVariantKey(variant) + "\x1f" + item; }

// 이 항목의 ECDF 를 고정 캐시에 확보한다. 이미 공용 캐시에 있으면 참조만 옮긴다.
function dcEnsureItems(items, variant) {
  const key = distVariantKey(variant);
  const store = _dcCache[key], shared = distCacheFor(key), inflight = _dcInflight[key];
  const want = [];
  let filled = false;                    // 공용 캐시에서 옮겨 담은 것이 있는가
  (items || []).forEach(it => {
    if (store[it]) return;
    if (shared[it]) { store[it] = shared[it]; filled = true; return; }
    if (inflight.has(it) || !distHasData(it) || _dcFailed[dcFailKey(key, it)]) return;
    want.push(it);
  });
  // ⚠ 옮겨 담기만 하고 끝나도 **재렌더를 예약해야 한다.** 호출자(dcRenderCompositeCell)는
  // 바로 앞에서 "데이터 없음"으로 이미 리턴했고, 새로 fetch 하는 게 없으면 도착 콜백도
  // 없다 → 예약이 없으면 셀이 "분포 로딩 중…"(.distg-plot:empty::before) 인 채로 영구히
  // 남는다. 일반 카드 배치가 먼저 도착해 공용 캐시를 채워 두는 흔한 순서에서 매번 발생했다.
  if (filled) dcRefresh();
  if (!want.length) return;
  // seq 변형은 서버 상한이 따로다(_DIST_SEQ_BATCH_MAX) — 30개로 묶으면 400 이 난다.
  const chunkSize = distVariantIsSeq(key) ? DIST_BATCH.SEQ_SIZE : DC_FETCH_CHUNK;
  for (let i = 0; i < want.length; i += chunkSize) {
    const chunk = want.slice(i, i + chunkSize);
    chunk.forEach(s => inflight.add(s));
    const url = `/pe/report/session/${SESSION_ID}/web_report/distribution_batch`
      + `?subjects=${encodeURIComponent(chunk.join(","))}${distVariantQuery(key)}`;
    fetch(url, { cache: "no-cache" })
      .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(j => {
        // seq 변형은 응답 스키마가 다르다(값 배열 vs ECDF columnar) — 빌더를 함께 가른다.
        const built = distVariantIsSeq(key) ? buildDistSeqFromCompact(j)
                                            : buildDistDataFromCompact(j);
        Object.keys(built).forEach(s => {
          delete _dcFailed[dcFailKey(key, s)];
          store[s] = built[s];
          distCachePut(key, s, built[s]);   // 공용 캐시와 공유(일반 카드가 재사용)
        });
        dcRefresh();
      })
      .catch(e => {
        chunk.forEach(s => { _dcFailed[dcFailKey(key, s)] = e.message || "로드 실패"; });
        showToast("합성 차트 데이터 로드 실패: " + e.message);
        dcRefresh();
      })
      .then(() => { chunk.forEach(s => inflight.delete(s)); });
  }
}

// 재시도 — 이 차트가 참조하는 항목의 실패 기록만 지우고 다시 그린다(요청은 렌더가 낸다).
function dcRetry(id) {
  const comp = dcGet(id);
  if (!comp) return;
  const variant = distGalleryDataVariant();
  dcItemsOf(comp).forEach(it => { delete _dcFailed[dcFailKey(variant, it)]; });
  dcRefresh();
}

// 데이터 도착 후 재렌더 — 보이는 합성 카드만 다시 큐에 넣는다(일반 카드 흐름과 동일).
let _dcRefreshTimer = null;
function dcRefresh() {
  if (_dcRefreshTimer) return;
  _dcRefreshTimer = setTimeout(() => {
    _dcRefreshTimer = null;
    document.querySelectorAll('#panel-distribution .distg-comp[data-visible="1"]')
      .forEach(c => { c.dataset.rendered = ""; distQueueRender(c); });
    if (_dcDetailId) dcRenderDetailCharts();
  }, 0);
}

// composite 가 참조하는 항목 집합(중복 제거).
function dcItemsOf(comp) {
  const seen = new Set();
  ((comp && comp.pairs) || []).forEach(p => seen.add(p.item));
  return Array.from(seen);
}

// limit 해석 — item 모드는 distribution_index(표시 규격선의 단일 진실)에서 가져오고,
// manual 모드는 저장값을 그대로 쓴다. units 는 item 모드일 때만 의미가 있다.
function dcLimitOf(comp) {
  const lim = (comp && comp.limit) || {};
  if (lim.mode === "manual") return { lo: lim.lo == null ? null : lim.lo, hi: lim.hi == null ? null : lim.hi, units: "" };
  const sub = lim.item || "";
  const sl = distSpecLimits(sub, null);
  const row = distIndex.find(r => r.subject === sub);
  return { lo: sl.lo, hi: sl.hi, units: (row && row.units) || "" };
}

// ── 갤러리 카드 ───────────────────────────────────────────────────────────────
// 일반 카드와 같은 골격(.distg-card + .distg-plot)이라 IntersectionObserver·purge·
// rAF 큐(distQueueRender/distPurgeGalleryCell)를 그대로 재사용한다.
function dcCardsHtml() {
  const ids = dcSortedIds();
  if (!ids.length) return "";
  const editing = (typeof MODE !== "undefined" && MODE === "edit");
  return ids.map(id => {
    const c = dcAll()[id];
    const { lo, hi, units } = dcLimitOf(c);
    const lim = distLimInnerHtml(lo, hi, units);
    const n = (c.pairs || []).length;
    const acts = editing
      ? `<span class="distg-comp-acts">` +
        `<button type="button" class="distg-comp-btn" data-dc-act="edit" data-comp-id="${esc(id)}" title="수정">✎</button>` +
        `<button type="button" class="distg-comp-btn" data-dc-act="del" data-comp-id="${esc(id)}" title="삭제">✕</button></span>`
      : "";
    return `<div class="distg-card distg-comp" data-comp-id="${esc(id)}" data-status="ok">
      <div class="distg-head">
        <div class="distg-line1">
          <span class="distg-comp-badge" title="합성 차트">📊</span>
          <span class="distg-name" title="${esc(c.name || "")}">${esc(c.name || "")}</span>
          ${acts}
        </div>
        <div class="distg-line2">
          <span class="distg-lim">${lim}</span>
          <span class="distg-cpk">pair ${n}개</span>
        </div>
      </div>
      <div class="distg-plot"></div>
    </div>`;
  }).join("");
}

// 합성 카드 1장 렌더 — distRenderGalleryCell 이 data-comp-id 를 보고 여기로 넘긴다.
function dcRenderCompositeCell(cell) {
  if (cell.dataset.rendered === "1") return;
  const comp = dcGet(cell.dataset.compId);
  const plot = cell.querySelector(".distg-plot");
  if (!comp || !plot || typeof Plotly === "undefined") return;
  // Serial 순 토글이 켜지면 seq 변형 캐시를 쓴다(distGalleryVariant 는 bin1 축만 준다).
  const variant = distGalleryDataVariant();
  const store = dcCacheFor(variant);
  const items = dcItemsOf(comp);
  const failed = items.filter(it => _dcFailed[dcFailKey(variant, it)]);
  if (failed.length) {   // 자동 재요청하지 않는다 — 사용자가 누를 때만
    plot.innerHTML = `<div class="placeholder gc-cell-empty">데이터 로드 실패` +
      `(${esc(_dcFailed[dcFailKey(variant, failed[0])])})` +
      `<br><button type="button" class="btn-sm" data-dc-act="retry" data-comp-id="${esc(cell.dataset.compId)}">다시 시도</button></div>`;
    cell.dataset.rendered = "1";
    return;
  }
  const missing = items.filter(it => !store[it] && distHasData(it));
  if (missing.length) { dcEnsureItems(items, variant); return; }   // 도착 후 dcRefresh 가 재큐잉

  // pair 별 표시점 — 캡은 pair 수로 나눈다(일반 카드가 source 수로 나누는 것과 같은 규칙).
  const pairs = (comp.pairs || []);
  const cap = distCapFor(pairs.length, DIST.CELL_BUDGET_CARD);
  const { lo, hi } = dcLimitOf(comp);
  // Serial 순 — x = 각 pair 의 측정 순서, y = 측정값. 단위가 다른 항목을 겹치면 큰 값이 y축을
  // 지배한다(ECDF 는 y가 0~100% 라 정규화 효과가 있었다) — 모달 안내대로 같은 단위끼리 고르는
  // 것을 전제한 의도된 타협이다. 선택 좌표 마커는 (값, 누적%) 좌표라 이 축에서는 제외한다.
  if (distSeqOnly) {
    const seqPts = {};
    pairs.forEach(p => {
      const entry = (store[p.item] || { bySource: {} }).bySource[p.source];
      if (entry) seqPts[dcPairKey(p.source, p.item)] = distSeqDisplayPoints(entry, cap);
    });
    const b = distSeqBounds(seqPts);
    const seqSentinel = distSeqSentinelTrace(b);
    Plotly.newPlot(plot, seqSentinel ? [seqSentinel] : [],
                   distSeqCellLayout("ok", lo, hi, b), DIST_CFG_STATIC);
    plot._distColorFor = k => dcColorFor(comp, k);
    distPaintPoints(plot, seqPts, null);
    cell.dataset.rendered = "1";
    return;
  }
  const pts = {};
  pairs.forEach(p => {
    const entry = (store[p.item] || { bySource: {} }).bySource[p.source];
    if (entry) pts[dcPairKey(p.source, p.item)] = distDisplayPoints(entry, cap);
  });
  const sentinel = distSentinelTrace(pts);
  // Map Analysis 선택 좌표(mapSelChips) — 일반 카드와 같은 규칙으로 이 pair 들 위에 찍는다.
  const cm = chipMarkersForPairs(pairs);
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
  // 점 색 해석기 주입 — distDrawPoints 는 키를 source 명으로 보지만, composite 는 pairKey
  // 라 저장된 색 맵을 쓰게 한다(기존 경로는 _distColorFor 가 없어 종전과 동일하게 동작).
  plot._distColorFor = k => dcColorFor(comp, k);
  // chip 마커는 canvas 위로 다시 그린다 — 안 넘기면 ECDF 점 canvas 에 가려 안 보인다.
  distPaintPoints(plot, pts, cm ? cm.traces : null);
  cell.dataset.rendered = "1";
}

// ── 툴바 "분석하기" 버튼 + 메뉴 ──────────────────────────────────────────────
function dcAnalyzeBtnHtml() {
  return `<button type="button" class="btn-sm dist-analyze-btn" data-dc-act="menu"` +
    ` aria-haspopup="true" aria-expanded="false" title="합성 산포 차트 만들기">분석하기 ▾</button>`;
}
// Issue Table 액션 메뉴(.issue-menu)와 같은 룩·배치 규칙을 쓰되 상태는 독립이다
// (data-issue-act 를 쓰면 edit_mode.js 의 .content 위임이 오발한다).
let _dcMenuEl = null, _dcMenuAnchor = null;
function dcCloseMenu() {
  if (!_dcMenuEl) return;
  if (_dcMenuAnchor) _dcMenuAnchor.setAttribute("aria-expanded", "false");
  _dcMenuEl.remove();
  _dcMenuEl = null; _dcMenuAnchor = null;
}
function dcToggleMenu(btn) {
  if (_dcMenuAnchor === btn) { dcCloseMenu(); return; }
  dcCloseMenu();
  const panel = document.getElementById("panel-distribution");
  if (!panel) return;
  const menu = document.createElement("div");
  menu.className = "issue-menu dc-menu";
  // 메뉴 항목 순서는 사용자 요청(2026-08-24): composite → **바로 아래** Gap Chart.
  // Gap Chart 항목 마크업은 gap_chart.js 가 준다(로드 전이면 항목이 빠질 뿐 메뉴는 뜬다).
  menu.innerHTML = `<button type="button" class="issue-menu-item" data-dc-act="open-modal"` +
    ` title="여러 source·항목의 산포를 한 차트에 겹쳐 그린다">` +
    `<span class="issue-menu-mark"></span>` +
    `<span class="issue-menu-label">📊 Distribution composite</span></button>`
    + ((typeof gcMenuItemHtml === "function") ? gcMenuItemHtml() : "");
  menu.style.position = "fixed";
  menu.style.visibility = "hidden";
  panel.appendChild(menu);
  const rect = btn.getBoundingClientRect();
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let top = rect.bottom + 4;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, rect.top - mh - 4);
  menu.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - mw - 12)) + "px";
  menu.style.top = top + "px";
  menu.style.visibility = "";
  btn.setAttribute("aria-expanded", "true");
  _dcMenuEl = menu; _dcMenuAnchor = btn;
}
document.addEventListener("click", e => {
  if (_dcMenuEl && !e.target.closest(".dc-menu") && !e.target.closest('[data-dc-act="menu"]')) dcCloseMenu();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") dcCloseMenu(); });

// ── 모달 (생성 / 수정) ───────────────────────────────────────────────────────
let _dcEditId = null;                  // null = 신규
let _dcSelSources = new Set();
let _dcSelItems = new Set();           // 갤러리 검색(distSelected)과 분리 — 오염 금지
let _dcSearchTimer = null;
let _dcListOpen = true;                // TestItem 목록 펼침 여부 (검색 입력 밖을 클릭하면 접힘)

function dcSourceNames() {
  return (((DATA && DATA.web_report && DATA.web_report.sources) || []).map(s => s.name || s))
    .filter(Boolean);
}

function dcOpenModal(id) {
  const modal = document.getElementById("dcModal");
  if (!modal) return;
  dcCloseMenu();
  _dcEditId = id || null;
  const comp = id ? dcGet(id) : null;
  _dcSelSources = new Set();
  _dcSelItems = new Set();
  if (comp) {
    (comp.pairs || []).forEach(p => { _dcSelSources.add(p.source); _dcSelItems.add(p.item); });
  } else {
    dcSourceNames().forEach(s => _dcSelSources.add(s));   // 기본 전체 선택
  }
  const title = document.getElementById("dcModalTitle");
  if (title) title.textContent = comp ? "Distribution composite 수정" : "Distribution composite 만들기";
  const nameInput = document.getElementById("dcName");
  if (nameInput) nameInput.value = comp ? (comp.name || "") : "";
  const search = document.getElementById("dcItemSearch");
  if (search) search.value = "";
  const del = document.getElementById("dcDelete");
  if (del) del.style.display = comp ? "" : "none";
  // limit 폼 복원
  const lim = (comp && comp.limit) || { mode: "item" };
  const mode = lim.mode === "manual" ? "manual" : "item";
  modal.querySelectorAll('input[name="dcLimitMode"]').forEach(r => { r.checked = (r.value === mode); });
  const loI = document.getElementById("dcLimitLo"), hiI = document.getElementById("dcLimitHi");
  if (loI) loI.value = (mode === "manual" && lim.lo != null) ? lim.lo : "";
  if (hiI) hiI.value = (mode === "manual" && lim.hi != null) ? lim.hi : "";
  modal.dataset.limitItem = (mode === "item" && lim.item) ? lim.item : "";

  _dcListOpen = true;                 // 열 때는 전체 목록이 보이는 상태로 시작
  dcRenderSources();
  dcRenderItemList("");
  dcRenderSummary();
  modal.classList.add("show");
  if (nameInput) nameInput.focus();
}
function dcCloseModal() {
  const modal = document.getElementById("dcModal");
  if (modal) modal.classList.remove("show");
  _dcEditId = null;
}

function dcRenderSources() {
  const host = document.getElementById("dcSourceList");
  if (!host) return;
  host.innerHTML = dcSourceNames().map(s =>
    `<label class="dc-src-item" title="${esc(s)}">` +   // 한 줄 고정이라 긴 이름은 잘린다 → 툴팁으로 전체 노출
    `<input type="checkbox" class="dc-src-chk" data-source="${esc(s)}"` +
    `${_dcSelSources.has(s) ? " checked" : ""}><span>${esc(s)}</span></label>`).join("")
    || `<div class="placeholder">source 없음</div>`;
}

// 항목 검색 — 검색어가 있으면 갤러리 검색과 **같은** distSuggestions(부분일치), 없으면
// **전체 항목**을 보여준다(2026-08-24 요청). 종전에는 검색어를 지우면 이미 고른 항목만
// 남아 목록이 사라진 것처럼 보였다.
function dcVisibleRows(q) {
  const term = String(q || "").trim();
  return term ? distSuggestions(term, 0) : distIndex.slice();
}
// 목록 머리 — 개수·전체선택 버튼. 접힌 상태면 "펼치기" 안내로 바뀐다.
// 목록 본문과 분리한 이유는 체크할 때마다 선택 개수만 갱신하면 되기 때문이다
// (전체 목록은 수천 행이라 매 체크마다 재렌더하면 눈에 띄게 버벅인다).
function dcRenderItemHead(q) {
  const head = document.getElementById("dcItemHead");
  if (!head) return;
  head.classList.toggle("is-collapsed", !_dcListOpen);
  if (!_dcListOpen) {
    head.innerHTML = `<span class="dist-sug-cnt">항목 목록 접힘 — 선택 <b>${_dcSelItems.size}</b>개 ` +
      `<span class="dist-sug-more">(클릭하면 펼칩니다)</span></span>`;
    return;
  }
  const term = String(q || "").trim();
  const n = dcVisibleRows(term).length;
  head.innerHTML =
    `<span class="dist-sug-cnt">${term ? "일치" : "전체"} <b>${n}</b>개` +
    (n > DC_LIST_MAX ? ` <span class="dist-sug-more">(상위 ${DC_LIST_MAX}개 표시)</span>` : "") +
    ` · 선택 <b>${_dcSelItems.size}</b>개</span>` +
    `<button type="button" class="btn-sm" data-dc-sug-all="1">전체 선택</button>` +
    `<button type="button" class="btn-sm" data-dc-sug-all="0">전체 해제</button>`;
}
function dcRenderItemList(q) {
  const host = document.getElementById("dcItemList");
  if (!host) return;
  dcRenderItemHead(q);
  host.style.display = _dcListOpen ? "" : "none";
  if (!_dcListOpen) { host.innerHTML = ""; return; }   // 접히면 DOM 도 비운다(수천 행 유지 비용)
  const term = String(q || "").trim();
  const rows = dcVisibleRows(term);
  const show = rows.length > DC_LIST_MAX ? rows.slice(0, DC_LIST_MAX) : rows;
  host.innerHTML = show.map(r =>
    `<label class="dist-sug-item">` +
    `<input type="checkbox" class="dc-item-chk" data-subject="${esc(r.subject)}"` +
    `${_dcSelItems.has(r.subject) ? " checked" : ""}>` +
    `<span class="sug-tno">${esc(r.test_num || "")}</span>` +
    `<span class="sug-name">${esc(r.subject)}</span></label>`).join("")
    || `<div class="placeholder">${term ? "일치하는 항목이 없습니다" : "표시할 항목이 없습니다"}</div>`;
}
// 목록 펼침/접힘 — 상태가 바뀔 때만 다시 그린다.
function dcSetListOpen(on) {
  const next = !!on;
  if (_dcListOpen === next) return;
  _dcListOpen = next;
  dcRenderItemList((document.getElementById("dcItemSearch") || {}).value || "");
}

// 선택된 항목 목록(오른쪽 칼럼) — 고정폭 그리드 + 자체 스크롤이라 50개를 골라도
// 모달 높이가 변하지 않는다. 검색 결과 목록과 분리한 이유는, 검색어를 바꿔도 지금까지
// 고른 것이 계속 보여야 "몇 개 골랐는지" 를 잃지 않기 때문이다.
function dcRenderPicked() {
  const host = document.getElementById("dcPickedList");
  const head = document.getElementById("dcPickedHead");
  const n = _dcSelItems.size;
  if (head) head.textContent = n ? `선택된 항목 (${n}개)` : "선택된 항목";
  if (!host) return;
  // 표시 순서는 distIndex(TEST SEQ) 순 — 고른 순서대로 쌓이면 찾기 어렵다.
  const picked = distIndex.filter(r => _dcSelItems.has(r.subject)).map(r => r.subject);
  _dcSelItems.forEach(it => { if (picked.indexOf(it) < 0) picked.push(it); });  // 인덱스 밖 항목 보존
  host.innerHTML = picked.map(it =>
    `<div class="dc-picked-item">` +
    `<span class="dc-picked-name" title="${esc(it)}">${esc(it)}</span>` +
    `<button type="button" class="dc-chip-x" data-dc-unpick="${esc(it)}" title="빼기">✕</button></div>`).join("")
    || `<div class="placeholder">왼쪽에서 항목을 검색해 고르세요</div>`;
}

// 조합 수 + limit 항목 드롭다운 + 저장 버튼 활성 상태를 함께 갱신한다.
function dcRenderSummary() {
  const modal = document.getElementById("dcModal");
  const host = document.getElementById("dcSelSummary");
  const nSrc = _dcSelSources.size, nItem = _dcSelItems.size, n = nSrc * nItem;
  dcRenderPicked();
  dcRenderItemHead((document.getElementById("dcItemSearch") || {}).value || "");
  if (host) {
    const warn = n > DC_MAX_PAIRS
      ? `<span class="dc-warn">조합이 ${DC_MAX_PAIRS}개를 넘습니다 — 항목이나 source 를 줄이세요</span>`
      : (n === 0 ? `<span class="dc-warn">source 와 항목을 각각 1개 이상 고르세요</span>` : "");
    host.innerHTML =
      `<div class="dc-count">조합: source ${nSrc} × item ${nItem} = <b>${n}</b>개 legend ${warn}</div>`;
  }
  // limit 기준 항목 드롭다운 — 선택된 항목만, 각 항목의 limit 을 함께 보여준다.
  const sel = document.getElementById("dcLimitItem");
  if (sel && modal) {
    const cur = sel.value || modal.dataset.limitItem || "";
    const opts = Array.from(_dcSelItems).map(it => {
      const sl = distSpecLimits(it, null);
      const row = distIndex.find(r => r.subject === it);
      const t = distLimText(sl.lo, sl.hi);
      const u = (row && row.units) ? " " + row.units : "";
      return `<option value="${esc(it)}"${it === cur ? " selected" : ""}>` +
        `${esc(it)}${t ? ` (${esc(t)}${esc(u)})` : ""}</option>`;
    }).join("");
    sel.innerHTML = opts || `<option value="">항목을 먼저 고르세요</option>`;
    if (cur && _dcSelItems.has(cur)) sel.value = cur;
  }
  const save = document.getElementById("dcSave");
  if (save) save.disabled = !(n > 0 && n <= DC_MAX_PAIRS);
}

// 선택 집합을 기준 목록 순서로 정렬하되, 기준 목록에 없는 것은 뒤에 붙여 보존한다.
function dcOrderedPick(order, sel) {
  const out = order.filter(v => sel.has(v));
  sel.forEach(v => { if (out.indexOf(v) < 0) out.push(v); });
  return out;
}

// 폼 → 저장 spec. 검증 실패는 문자열 메시지를 throw 한다(호출부가 토스트).
function dcCollectSpec() {
  const name = String((document.getElementById("dcName") || {}).value || "").trim();
  if (!name) throw new Error("차트 이름을 입력하세요");
  if (name.length > DC_NAME_MAX) throw new Error(`차트 이름이 너무 깁니다 (${DC_NAME_MAX}자 이하)`);
  // 선택 집합 전체를 저장한다 — 표시 순서만 distIndex(TEST SEQ)/source 순을 따르고,
  // 인덱스에 없는 것도 뒤에 붙여 보존한다(dcRenderPicked 와 같은 규칙).
  // ⚠ 인덱스 기준으로 filter 하면, 전처리 제외나 source 축소로 목록에서 빠진 항목이
  //    "이름만 바꿔 저장" 하는 순간 조용히 사라진다. 서버는 실재 여부를 검사하지 않고
  //    사용자 입력을 보존하는데(service._sanitize) 클라가 버리면 그 방어가 무의미해진다.
  const sources = dcOrderedPick(dcSourceNames(), _dcSelSources);
  const items = dcOrderedPick(distIndex.map(r => r.subject), _dcSelItems);
  const pairs = [];
  sources.forEach(s => items.forEach(it => pairs.push({ source: s, item: it })));
  if (!pairs.length) throw new Error("source 와 항목을 각각 1개 이상 고르세요");
  if (pairs.length > DC_MAX_PAIRS) throw new Error(`조합이 ${DC_MAX_PAIRS}개를 넘습니다`);

  const modal = document.getElementById("dcModal");
  const mode = (modal.querySelector('input[name="dcLimitMode"]:checked') || {}).value || "item";
  let limit;
  if (mode === "manual") {
    const num = v => {
      const t = String(v == null ? "" : v).trim();
      if (!t) return null;
      const n = Number(t);
      if (!isFinite(n)) throw new Error("Limit 은 숫자로 입력하세요");
      return n;
    };
    limit = { mode: "manual", lo: num((document.getElementById("dcLimitLo") || {}).value),
              hi: num((document.getElementById("dcLimitHi") || {}).value) };
  } else {
    const it = String((document.getElementById("dcLimitItem") || {}).value || "");
    if (!it) throw new Error("Limit 기준 항목을 고르세요");
    limit = { mode: "item", item: it };
  }
  const prev = _dcEditId ? (dcGet(_dcEditId) || {}).colors : null;
  return { name, pairs, limit, colors: dcAssignColors(pairs, prev) };
}

// ── 저장 (단발 POST — 모달 트랜잭션형 입력이라 autoSave 채널을 쓰지 않는다) ────
function dcPost(ops) {
  return fetch(`/pe/report/session/${SESSION_ID}/web_report/dist_composites`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
    body: JSON.stringify({ ops }),
  }).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    // 응답이 권위본 — 재로드(load) 없이 이것만 갈아끼운다(콜드 재빌드 회피).
    DATA.dist_composites = j.dist_composites || {};
    return j;
  });
}

function dcSaveFromModal() {
  let spec;
  try { spec = dcCollectSpec(); } catch (e) { showToast(e.message); return; }
  const id = _dcEditId || dcNewId();
  const btn = document.getElementById("dcSave");
  if (btn) btn.disabled = true;
  dcPost([{ key: id, value: spec }])
    .then(() => {
      dcCloseModal();
      const q = (document.getElementById("distSearch") || {}).value || "";
      distRenderGallery();
      if (typeof restoreDistSearch === "function") restoreDistSearch(q);
      // 상세를 열어 둔 채 ✎수정한 경우 — 갱신하지 않으면 눈앞의 상세만 옛 이름·옛 pair·
      // 옛 차트로 남는다(갱신되는 갤러리는 그 뒤에 가려 안 보인다).
      if (_dcDetailId === id) {
        dcRenderDetail();
        const comp = dcGet(id);
        if (comp) dcEnsureItems(dcItemsOf(comp), distGalleryVariant());
      }
      showToast("합성 차트를 저장했습니다");
    })
    .catch(e => { showToast("저장 실패: " + e.message); if (btn) btn.disabled = false; });
}

function dcDelete(id) {
  const comp = dcGet(id);
  if (!comp) return;
  if (!confirm(`합성 차트 "${comp.name || id}" 를 삭제할까요?`)) return;
  dcPost([{ key: id, value: null }])
    .then(() => {
      if (_dcDetailId === id) dcCloseDetail();
      dcCloseModal();
      const q = (document.getElementById("distSearch") || {}).value || "";
      distRenderGallery();
      if (typeof restoreDistSearch === "function") restoreDistSearch(q);
      showToast("삭제했습니다");
    })
    .catch(e => showToast("삭제 실패: " + e.message));
}

// UUID — crypto.randomUUID 가 없는 환경(비보안 컨텍스트 구버전)을 위한 폴백 포함.
// 서버 정규식 ^[0-9a-fA-F-]{8,40}$ 를 만족해야 한다.
function dcNewId() {
  if (window.crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const h = () => Math.floor(Math.random() * 65536).toString(16).padStart(4, "0");
  return `${h()}${h()}-${h()}-${h()}-${h()}-${h()}${h()}${h()}`;
}

// ── Composite detail (패널 교체 — Item detail 과 같은 진입감) ─────────────────
let _dcDetailId = null;
let _dcDetailReturnId = null;
let _dcLegendFocus = new Set();        // 강조 pair (로컬 — distSourceFilter 와 분리)

// 차트 주석 저장 키의 subject — **UUID 기반 불변 키**(이름을 바꿔도 주석이 따라온다).
// 서버 _CHART_KEY_RE 는 `cdf:<200자 이내>` 를 허용하므로 별도 변경이 필요 없다.
function dcNoteSubject(id) { return "comp:" + String(id || ""); }

function dcOpenDetail(id) {
  const dp = document.getElementById("panel-dist-composite-detail");
  const comp = dcGet(id);
  if (!dp || !comp) return;
  // 주석 툴바·코멘트 뷰의 DOM id 를 Item_detail 과 공유하므로 상세는 한 번에 하나만 열린다.
  // hideItemDetail 은 미저장 주석을 flush 한 뒤 그 패널을 비운다(= id 중복 원천 차단).
  if (typeof hideItemDetail === "function") hideItemDetail();
  if (!window.Plotly && window.__plotlyReady) {
    showToast("차트 모듈을 불러오는 중입니다 — 잠시 후 자동으로 열립니다.");
    window.__plotlyReady.then(() => dcOpenDetail(id));
    return;
  }
  if (!dp.classList.contains("active")) {
    const cur = document.querySelector(".content > .panel.active");
    _dcDetailReturnId = cur ? cur.id : "panel-distribution";
    if (cur) cur.classList.remove("active");
    dp.classList.add("active");
  }
  _dcDetailId = id;
  _dcLegendFocus = new Set();
  window.scrollTo(0, 0);
  dcRenderDetail();
  dcEnsureItems(dcItemsOf(comp), distGalleryVariant());
}

function dcCloseDetail() {
  const dp = document.getElementById("panel-dist-composite-detail");
  dcFlushNotes();
  dcPurgeDetailChart();
  if (dp) { dp.classList.remove("active"); dp.innerHTML = ""; }
  const back = document.getElementById(_dcDetailReturnId || "panel-distribution");
  if (back) back.classList.add("active");
  _dcDetailId = null;
  _dcDetailReturnId = null;
}
// 탭 버튼 클릭 시: 복원 없이 상세만 닫는다(해당 탭 패널이 이어서 활성화됨).
// 탭 전환은 .panel active 를 통째로 토글하므로 패널은 어차피 숨겨지지만, scattergl 의
// WebGL 컨텍스트는 그대로 남는다 — hideItemDetail 과 같은 이유로 여기서 purge 한다.
function hideDistCompositeDetail() {
  const dp = document.getElementById("panel-dist-composite-detail");
  if (dp && dp.classList.contains("active")) {
    dcFlushNotes();
    dp.classList.remove("active");
    dcPurgeDetailChart();
    dp.innerHTML = "";
  }
  _dcDetailId = null;
  _dcDetailReturnId = null;
}
function dcPurgeDetailChart() {
  // purge 는 gd.layout 을 지운다 — 그 전에 등록을 풀어야 미저장 도형을 회수할 수 있다.
  // cnDetach 가 dirty 일 때만 회수하므로 재렌더마다 저장 요청이 나가지는 않는다.
  if (_dcDetailId && window.cnDetach) cnDetach(`cdf:${dcNoteSubject(_dcDetailId)}`);
  const el = document.getElementById("dcDetailChart");
  if (el && el.data) { try { Plotly.purge(el); } catch (e) { /* no-op */ } }
}

// 상세를 떠날 때 미저장 주석을 저장한다(Item_detail 의 closeItemDetail 과 같은 규칙).
// 실패해도 pending 은 key 별로 남아 다음 autoSave/beforeunload 가 재시도한다.
function dcFlushNotes() {
  if (typeof _cnDirty !== "undefined" && _cnDirty.size) {
    cnFlush().catch(e => showToast("차트 Comment 자동저장 실패: " + e.message));
  }
}

function dcRenderDetail() {
  const dp = document.getElementById("panel-dist-composite-detail");
  const comp = dcGet(_dcDetailId);
  if (!dp || !comp) return;
  dcPurgeDetailChart();
  const { lo, hi, units } = dcLimitOf(comp);
  const editing = (typeof MODE !== "undefined" && MODE === "edit");
  const acts = editing
    ? `<button type="button" class="btn-sm" data-dc-act="edit" data-comp-id="${esc(_dcDetailId)}">✎ 수정</button>` +
      `<button type="button" class="btn-sm" data-dc-act="del" data-comp-id="${esc(_dcDetailId)}">✕ 삭제</button>`
    : "";
  dp.innerHTML = `<div class="idet dc-detail">
    <div class="idet-head">
      <button type="button" class="btn-sm idet-back" data-dc-act="back">← Back</button>
      <span class="idet-title"><span class="distg-comp-badge">📊</span> <b>${esc(comp.name || "")}</b>
        <span class="idet-lim">${distLimInnerHtml(lo, hi, units)}</span>
        <span class="idet-stat">pair ${(comp.pairs || []).length}개</span></span>
      <span class="dc-detail-acts">${acts}</span>
    </div>
    <div id="chartNoteBar"></div>
    <div class="idet-body">
      <div class="idet-charts">
        <div class="idet-chart-block">
          <div class="idet-chart-title">${esc(distSeqOnly ? "Serial 순 (측정 순서 · 합성)" : "누적분포 CDF (합성)")}</div>
          <div id="dcDetailChart" class="dist-chart"></div>
          <div class="idet-chart-comment" id="cdfCommentView"></div>
        </div>
      </div>
      <aside class="dist-legend-side idet-legend-side" id="dcDetailLegend"></aside>
    </div>
    <div class="dc-stats" id="dcDetailStats"></div>
  </div>`;
  // 차트 주석 — 툴바·코멘트 뷰의 DOM id 는 Item_detail 과 공유한다(한 번에 한 상세만 열려
  // 있도록 dcOpenDetail 이 보장한다). 저장 키의 subject 는 **이름이 아니라 UUID**다
  // (dcNoteSubject) — 이름으로 잡으면 차트를 개명하는 순간 그려 둔 주석이 끊긴다(§5-12).
  if (window.chartNotesBar) {
    chartNotesBar({ subject: comp.name || "", note_subject: dcNoteSubject(_dcDetailId) });
  }
  if (window.cnRenderChartComments) cnRenderChartComments(dcNoteSubject(_dcDetailId));
  dcRenderDetailCharts();
}

// pair legend — 클릭하면 그 pair 만 원색, 나머지는 dim(로컬 상태).
function dcLegendHtml(comp) {
  const items = (comp.pairs || []).map(p => {
    const key = dcPairKey(p.source, p.item);
    const on = _dcLegendFocus.has(key);
    return `<span class="dist-leg-item${on ? " is-selected" : ""}" data-dc-pair="${esc(key)}"` +
      ` title="${esc(dcPairLabel(p.source, p.item))}">` +
      `<span class="dist-leg-sw" style="background:${dcColorFor(comp, key)}"></span>` +
      `<span class="dist-leg-nm">${esc(dcPairLabel(p.source, p.item))}</span></span>`;
  }).join("");
  const clear = _dcLegendFocus.size
    ? `<button type="button" class="btn-sm dist-leg-clear" data-dc-leg="clear">강조 ${_dcLegendFocus.size}개 해제</button>`
    : `<button type="button" class="btn-sm dist-leg-clear is-placeholder" tabindex="-1" aria-hidden="true">강조 해제</button>`;
  return `<div class="dist-legend-row ${DIST_LEGEND_VERT_CLS} is-open">${clear}` +
    `<div class="dist-legend">${items}</div></div>`;
}
function dcActiveColor(comp, key) {
  if (!_dcLegendFocus.size) return dcColorFor(comp, key);
  return _dcLegendFocus.has(key) ? dcColorFor(comp, key) : DIST_DIM_COLOR;
}

function dcRenderDetailCharts() {
  const comp = dcGet(_dcDetailId);
  if (!comp) return;
  // 통계표는 **항상 ECDF 기준**이다(dcPairStats 가 Δp 가중으로 복원한다) — Serial 순
  // 모드에서도 이 store 로 계산해 같은 화면의 숫자가 모드에 따라 달라지지 않게 한다(규칙 #13).
  // 따라서 seq 모드 상세는 ECDF·seq 두 캐시를 함께 확보한다(참조 항목 수십 개라 부하는 작음).
  const store = dcCacheFor(distGalleryVariant());
  const pairs = comp.pairs || [];
  const items = dcItemsOf(comp);
  const missing = pairs.some(p => !store[p.item] && distHasData(p.item));
  if (missing) dcEnsureItems(items, distGalleryVariant());
  const seqVariant = distGalleryDataVariant();
  const seqStore = distSeqOnly ? dcCacheFor(seqVariant) : null;
  const seqMissing = seqStore
    ? pairs.some(p => !seqStore[p.item] && distHasData(p.item)) : false;
  if (seqMissing) dcEnsureItems(items, seqVariant);
  // legend·통계표는 순수 HTML 이라 Plotly 유무와 무관하게 먼저 그린다 — 차트 가드 안에
  // 두면 로드가 늦은 PC 에서 옆 칸과 표까지 통째로 비어 보인다.
  const legend = document.getElementById("dcDetailLegend");
  if (legend) legend.innerHTML = dcLegendHtml(comp);
  dcRenderStats(comp, store, missing);

  const div = document.getElementById("dcDetailChart");
  if (!div || typeof Plotly === "undefined") return;
  const useGl = !!DIST.CDF_GL && webglOk();
  const { lo, hi, units } = dcLimitOf(comp);
  // 강조가 걸리면 dim pair 를 먼저 그려 강조가 위로 오게 한다(distOrderedSources 와 같은 규칙).
  const ordered = _dcLegendFocus.size
    ? pairs.slice().sort((a, b) => (_dcLegendFocus.has(dcPairKey(a.source, a.item)) ? 1 : 0) -
                                   (_dcLegendFocus.has(dcPairKey(b.source, b.item)) ? 1 : 0))
    : pairs;
  // Serial 순 — x = 측정 순서, y = 측정값. 전량 렌더(다운샘플 없음)라 trace 방식을 ECDF 와
  // 같게 유지한다(한 차트에 SVG/WebGL 을 섞으면 SVG trace 가 gl 캔버스 아래로 가린다).
  // 선택 좌표 마커는 (값, 누적%) 좌표라 이 축에서는 제외한다.
  if (seqStore) {
    const seqTraces = [];
    ordered.forEach(p => {
      const entry = (seqStore[p.item] || { bySource: {} }).bySource[p.source];
      if (!entry || !entry.vs) return;
      const key = dcPairKey(p.source, p.item);
      const xs = new Array(entry.vs.length);
      for (let i = 0; i < entry.vs.length; i++) xs[i] = i + 1;
      const t = { type: useGl ? "scattergl" : "scatter", mode: "markers",
        name: dcPairLabel(p.source, p.item), x: xs, y: entry.vs,
        marker: { color: dcActiveColor(comp, key), size: 5 },
        hovertemplate: "%{fullData.name}<br>측정 순서 %{x}<br>측정값 %{y}<extra></extra>" };
      if (!useGl) t.cliponaxis = false;
      seqTraces.push(t);
    });
    Plotly.newPlot(div, seqTraces, { ...DIST_PLOT_BG, plot_bgcolor: "#FFFFFF",
      xaxis: { title: { text: "측정 순서 (rawdata 누적 순)" }, showgrid: true,
        gridcolor: IDET_GRID_MAJOR, zeroline: false, rangemode: "tozero", nticks: 10 },
      yaxis: { title: { text: `측정값${units ? " [" + units + "]" : ""}` }, showgrid: true,
        gridcolor: IDET_GRID_MAJOR, zeroline: false },
      shapes: distSeqSpecShapes(lo, hi), annotations: distSeqSpecAnnos(lo, hi, false),
      margin: { l: 60, r: 22, t: 16, b: 46 }, showlegend: false }, DIST_CFG);
    // Serial 순 차트에는 주석을 붙이지 않는다(축 의미가 달라 저장 좌표가 어긋난다).
    // 등록을 남기면 이후 저장이 이 layout 에서 빈 도형을 회수해 저장된 주석을 지운다.
    if (window.cnDetach) cnDetach(`cdf:${dcNoteSubject(_dcDetailId)}`);
    return;
  }
  // ECDF **전량** (표시용 다운샘플 없음 — 상세는 원본 그대로, 규칙 §5).
  const traces = [];
  ordered.forEach(p => {
    const entry = (store[p.item] || { bySource: {} }).bySource[p.source];
    if (!entry) return;
    const key = dcPairKey(p.source, p.item);
    const t = { type: useGl ? "scattergl" : "scatter", mode: "markers",
      name: dcPairLabel(p.source, p.item), x: entry.xs, y: entry.ys,
      marker: { color: dcActiveColor(comp, key), size: 5 },
      hovertemplate: "%{fullData.name}<br>측정값 %{x}<br>누적 %{y:.1f}%<extra></extra>" };
    if (!useGl) t.cliponaxis = false;
    traces.push(t);
  });
  // Map Analysis 선택 좌표 — 갤러리 카드와 같은 마커(상세는 canvas 가 없어 trace 만).
  // useGl 을 넘겨야 곡선과 같은 레이어에 올라간다(안 넘기면 gl 캔버스에 가려 안 보인다).
  const cm = chipMarkersForPairs(pairs, useGl);
  if (cm) traces.push(...cm.traces);
  const xtitle = `측정값${units ? " [" + units + "]" : ""}`;
  Plotly.newPlot(div, traces, { ...DIST_PLOT_BG, plot_bgcolor: "#FFFFFF",
    xaxis: { title: { text: xtitle }, showgrid: true, gridcolor: IDET_GRID_MAJOR, zeroline: false, nticks: 10 },
    yaxis: { title: { text: "누적 %" }, range: [-2, 102], tick0: 0, dtick: 20, ticksuffix: "%",
      showgrid: true, gridcolor: IDET_GRID_MAJOR, zeroline: false },
    shapes: distSpecShapes(lo, hi, true).concat(cm ? cm.shapes : []),
    annotations: distSpecAnnos(lo, hi, false),
    margin: { l: 60, r: 22, t: 16, b: 46 }, showlegend: false }, DIST_CFG);
  // 저장/미저장 주석 오버레이 — base shapes 개수를 기억해야 하므로 렌더 직후 마지막에.
  if (window.chartNotesApply) chartNotesApply("cdf", dcNoteSubject(_dcDetailId), div);
}

// ── pair 별 통계 (ECDF 복원) ─────────────────────────────────────────────────
// ECDF 는 고유값 x_i 와 누적% y_i 뿐이라 die 수를 모른다. 하지만 Δp_i = (y_i − y_{i−1})/100
// 이 그 값의 **확률질량**이므로 모집단 기준 통계는 정확히 복원된다:
//   mean = Σ x_i·Δp_i , var = Σ x_i²·Δp_i − mean² , median 은 누적 50% 지점.
// (표본 n−1 보정만 불가 — n 을 모르기 때문. 각주로 밝힌다.)
function dcPairStats(entry, lo, hi) {
  const xs = entry.xs, ys = entry.ys, n = xs.length;
  if (!n) return null;
  let prev = 0, mean = 0, m2 = 0, median = null;
  for (let i = 0; i < n; i++) {
    const dp = Math.max(0, (ys[i] - prev)) / 100;
    prev = ys[i];
    mean += xs[i] * dp;
    m2 += xs[i] * xs[i] * dp;
    if (median === null && ys[i] >= 50) median = xs[i];
  }
  const varr = Math.max(0, m2 - mean * mean);
  const sd = Math.sqrt(varr);
  let cpk = null;
  if (sd > 0) {
    const cands = [];
    if (hi != null) cands.push((hi - mean) / (3 * sd));
    if (lo != null) cands.push((mean - lo) / (3 * sd));
    if (cands.length) cpk = Math.min.apply(null, cands);
  }
  return { min: xs[0], max: xs[n - 1], median, mean, sd, cpk, uniq: n };
}
function dcRenderStats(comp, store, missing) {
  const host = document.getElementById("dcDetailStats");
  if (!host) return;
  const { lo, hi } = dcLimitOf(comp);
  const f4 = v => (v == null || !isFinite(v)) ? "-" : String(Math.round(v * 1e4) / 1e4);
  const rows = (comp.pairs || []).map(p => {
    const entry = (store[p.item] || { bySource: {} }).bySource[p.source];
    const key = dcPairKey(p.source, p.item);
    const st = entry ? dcPairStats(entry, lo, hi) : null;
    const sw = `<span class="dist-leg-sw" style="background:${dcColorFor(comp, key)}"></span>`;
    if (!st) return `<tr><td>${sw} ${esc(dcPairLabel(p.source, p.item))}</td>` +
      `<td class="st-num" colspan="6">${missing ? "로드 중…" : "데이터 없음"}</td></tr>`;
    const warn = (st.cpk != null && st.cpk < DIST.CPK_GOOD) ? " cpk-warn" : "";
    return `<tr><td>${sw} ${esc(dcPairLabel(p.source, p.item))}</td>` +
      `<td class="st-num">${esc(st.uniq)}</td>` +
      `<td class="st-num">${esc(f4(st.min))}</td>` +
      `<td class="st-num">${esc(f4(st.median))}</td>` +
      `<td class="st-num">${esc(f4(st.max))}</td>` +
      `<td class="st-num">${esc(f4(st.mean))}</td>` +
      `<td class="st-num">${esc(st.sd == null ? "-" : fmtStdev(st.sd))}</td>` +
      `<td class="st-num${warn}">${esc(st.cpk == null ? "-" : (Math.round(st.cpk * 1000) / 1000))}</td></tr>`;
  }).join("");
  host.innerHTML = `<div class="sheet-wrap"><table class="sheet-table">` +
    `<thead><tr><th>Legend</th><th>고유값</th><th>min</th><th>median</th><th>max</th>` +
    `<th>mean</th><th>stdev</th><th>cpk</th></tr></thead><tbody>${rows}</tbody></table></div>` +
    `<p class="dc-stats-note">※ ECDF(고유 측정값 + 누적%) 기반 모집단 통계 — die 수가 아니라 ` +
    `고유값 개수를 표시합니다. Limit 은 이 차트에 설정된 기준을 씁니다. ` +
    `<b>Serial 순으로 보는 중에도 이 표는 누적분포(ECDF) 기준</b>이라 모드에 따라 숫자가 ` +
    `달라지지 않습니다.</p>`;
}

// ── 이벤트 (Distribution 패널 위임에서 호출 · 모달/상세는 문서 위임) ──────────
// 반환 true = 이 클릭을 소비했다(호출부가 뒤 분기를 타지 않는다).
function dcPanelClick(e) {
  const act = e.target.closest("[data-dc-act]");
  if (act) {
    const kind = act.dataset.dcAct;
    if (kind === "menu") { dcToggleMenu(act); return true; }
    dcCloseMenu();
    if (kind === "open-modal") { dcOpenModal(null); return true; }
    // Gap Chart 만들기 — 메뉴를 이 파일이 그리므로 진입도 여기서 넘긴다(gap_chart.js).
    if (kind === "gap-modal") {
      if (typeof gcOpenModal === "function") gcOpenModal(null);
      return true;
    }
    if (kind === "edit") { dcOpenModal(act.dataset.compId); return true; }
    if (kind === "del") { dcDelete(act.dataset.compId); return true; }
    if (kind === "retry") { dcRetry(act.dataset.compId); return true; }
    return true;
  }
  const card = e.target.closest(".distg-comp");
  if (card) { dcOpenDetail(card.dataset.compId); return true; }
  return false;
}

// 모달·상세 패널 이벤트 — 둘 다 재렌더되므로 문서 레벨 위임 1회.
document.addEventListener("click", e => {
  // 상세 패널 (Back / 수정 / 삭제 / legend)
  const dp = e.target.closest("#panel-dist-composite-detail");
  if (dp) {
    const act = e.target.closest("[data-dc-act]");
    if (act) {
      const kind = act.dataset.dcAct;
      if (kind === "back") { dcCloseDetail(); return; }
      if (kind === "edit") { dcOpenModal(act.dataset.compId); return; }
      if (kind === "del") { dcDelete(act.dataset.compId); return; }
    }
    if (e.target.closest('[data-dc-leg="clear"]')) {
      _dcLegendFocus.clear(); dcRenderDetailCharts(); return;
    }
    const leg = e.target.closest("[data-dc-pair]");
    if (leg) {
      const k = leg.dataset.dcPair;
      if (_dcLegendFocus.has(k)) _dcLegendFocus.delete(k); else _dcLegendFocus.add(k);
      dcRenderDetailCharts();
      return;
    }
    return;
  }
  // 모달
  const modal = e.target.closest("#dcModal");
  if (!modal) return;
  // TestItem 목록 펼침/접힘 — 검색 입력·머리·목록 안을 클릭하면 펼치고, 모달의 다른
  // 영역을 클릭하면 접는다(2026-08-24 요청). 접혀도 선택은 오른쪽 칼럼에 남는다.
  dcSetListOpen(!!(e.target.closest("#dcItemSearch") || e.target.closest("#dcItemHead")
                   || e.target.closest("#dcItemList")));
  // ⚠ 배경(오버레이) 클릭으로는 닫지 않는다 — 입력한 이름·선택이 통째로 날아간다.
  //    닫기는 취소 버튼과 Esc 뿐이다.
  if (e.target.id === "dcCancel") { dcCloseModal(); return; }
  if (e.target.id === "dcSave") { dcSaveFromModal(); return; }
  if (e.target.id === "dcDelete") { if (_dcEditId) dcDelete(_dcEditId); return; }
  const srcAll = e.target.closest("[data-dc-src-all]");
  if (srcAll) {
    const on = srcAll.dataset.dcSrcAll === "1";
    _dcSelSources = new Set(on ? dcSourceNames() : []);
    dcRenderSources(); dcRenderSummary();
    return;
  }
  const sugAll = e.target.closest("[data-dc-sug-all]");
  if (sugAll) {
    const on = sugAll.dataset.dcSugAll === "1";
    const q = (document.getElementById("dcItemSearch") || {}).value || "";
    // 검색어가 없으면 전체 항목이 대상이다(목록에 보이는 것과 같은 집합).
    dcVisibleRows(q).forEach(r => {
      if (on) _dcSelItems.add(r.subject); else _dcSelItems.delete(r.subject);
    });
    dcRenderItemList(q); dcRenderSummary();
    return;
  }
  const unpick = e.target.closest("[data-dc-unpick]");
  if (unpick) {
    _dcSelItems.delete(unpick.dataset.dcUnpick);
    dcRenderItemList((document.getElementById("dcItemSearch") || {}).value || "");
    dcRenderSummary();
    return;
  }
  // 선택된 항목 전부 해제 — 검색어와 무관하게 고른 것을 통째로 비운다
  // (검색 결과 헤더의 '전체 해제' 는 그 검색어의 일치 항목만 뺀다 — 둘은 다른 동작이다).
  if (e.target.closest("[data-dc-pick-clear]")) {
    _dcSelItems.clear();
    dcRenderItemList((document.getElementById("dcItemSearch") || {}).value || "");
    dcRenderSummary();
    return;
  }
});
document.addEventListener("change", e => {
  if (!e.target.closest || !e.target.closest("#dcModal")) return;
  const src = e.target.closest(".dc-src-chk");
  if (src) {
    if (src.checked) _dcSelSources.add(src.dataset.source); else _dcSelSources.delete(src.dataset.source);
    dcRenderSummary();
    return;
  }
  const item = e.target.closest(".dc-item-chk");
  if (item) {
    if (item.checked) _dcSelItems.add(item.dataset.subject); else _dcSelItems.delete(item.dataset.subject);
    dcRenderSummary();
    return;
  }
  if (e.target.name === "dcLimitMode") dcRenderSummary();
});
document.addEventListener("input", e => {
  if (e.target.id !== "dcItemSearch") return;
  const q = e.target.value;
  clearTimeout(_dcSearchTimer);
  // 타이핑하면 접혀 있어도 펼친다(갤러리 검색과 같은 250ms debounce).
  _dcSearchTimer = setTimeout(() => { _dcListOpen = true; dcRenderItemList(q); }, 250);
});
// 키보드 탭 이동으로 검색창에 들어와도 목록이 열리게 한다(클릭 경로는 위 click 위임).
document.addEventListener("focusin", e => {
  if (e.target && e.target.id === "dcItemSearch") dcSetListOpen(true);
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  const modal = document.getElementById("dcModal");
  if (modal && modal.classList.contains("show")) { dcCloseModal(); return; }
  const dp = document.getElementById("panel-dist-composite-detail");
  if (dp && dp.classList.contains("active")) dcCloseDetail();
});

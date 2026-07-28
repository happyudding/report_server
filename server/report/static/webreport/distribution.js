// ── Distribution (ECDF, small-multiples grid) ─────────────────────────────
// F:\COINAPI\report_webserver\plotly_core_code (figure_builder.py/page_builder.py) 참고:
// item(subject)별 산점(value vs 누적%) + spec 상/하한 점선. 데이터는
// web_report/tabs/distribution.py 가 이미 전량(다운샘플링 없음) 계산해 별도 엔드포인트로
// 내려준다. 갤러리 칸은 축·그리드·스펙선만 Plotly 가 그리고 ECDF 점은 canvas 오버레이
// (distPaintPoints, 표시용 다운샘플) — IntersectionObserver 로 화면에 보이는 칸만 그리고,
// 화면 밖으로 나가면 Plotly.purge 로 해제해 plot DOM 상주를 막는다.
// 상세(item_detail.js)의 CDF 는 전 포인트 렌더라 DIST.CDF_GL 플래그로 scattergl(WebGL) 사용.
const DIST_PALETTE = ["#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
  "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52"];
// DIST_PALETTE(10색) 을 넘는 source 폴백 색 — client/chart_colors.py
// generate_default_colors() 와 동일 공식(진한 원색 7 + 황금비 hue 분산 + S/V 순환)이라
// dist_colors 미지정 세션의 11번째+ 색이 Honey 기본 48색과 일치한다. i 상한 없음.
const DIST_GEN_DEEP_HUES = [0.60, 0.33, 0.92, 0.80, 0.00, 0.09, 0.50];
const DIST_GEN_S_CYCLE = [1.00, 0.82, 0.92];
const DIST_GEN_V_CYCLE = [0.92, 0.68, 0.80, 0.55];
function distHsvHex(h, s, v) {
  // Python colorsys.hsv_to_rgb 1:1 포팅 (int() 절사 포함 — hex 가 바이트 단위로 일치)
  h = ((h % 1.0) + 1.0) % 1.0;
  let r, g, b;
  const i = Math.trunc(h * 6.0) % 6, f = (h * 6.0) - Math.trunc(h * 6.0);
  const p = v * (1.0 - s), q = v * (1.0 - s * f), t = v * (1.0 - s * (1.0 - f));
  if (i === 0)      { r = v; g = t; b = p; }
  else if (i === 1) { r = q; g = v; b = p; }
  else if (i === 2) { r = p; g = v; b = t; }
  else if (i === 3) { r = p; g = q; b = v; }
  else if (i === 4) { r = t; g = p; b = v; }
  else              { r = v; g = p; b = q; }
  const hex = n => Math.trunc(n * 255).toString(16).toUpperCase().padStart(2, "0");
  return "#" + hex(r) + hex(g) + hex(b);
}
function distDefaultColor(i) {
  if (i < DIST_GEN_DEEP_HUES.length) return distHsvHex(DIST_GEN_DEEP_HUES[i], 1.0, 0.85);
  const j = i - DIST_GEN_DEEP_HUES.length;
  const h = (0.07 + (j + 1) * 0.61803398875) % 1.0;
  return distHsvHex(h, DIST_GEN_S_CYCLE[j % DIST_GEN_S_CYCLE.length],
                       DIST_GEN_V_CYCLE[j % DIST_GEN_V_CYCLE.length]);
}
let distDataCache = {};       // subject → {lower_limit, upper_limit, units, bySource:{source:{xs,ys}}} (ECDF, Issue Table 미니분포와 공용)
let distColorMap = {};        // source → color

// ── Distribution 산포 탭 (툴바/갤러리/상세) 상태·규격 ─────────────────────────
// DOWNSAMPLE: 미니셀 표시점 소프트 상한(소스별). canvas 전환(distPaintPoints)으로 점 비용이
// 낮아져 1500 → 2000 으로 올렸다가(2026-07-20), 갤러리·미니셀 렌더 체감을 더 가볍게 하려고
// 2026-07-23 에 1500 으로 되돌렸다. 이 캡은 표시(브라우저)에만 걸리고 서버 페이로드·상세
// CDF(item_detail distRenderCdf, 전량 렌더)에는 영향이 없다.
const DIST = { CPK_GOOD: 1.33, DOWNSAMPLE: 1500, PER_FRAME: 3,
  ROOT_MARGIN: "1200px 0px", EXCLUDE: ["chipid", "gpib", "otp", "code"],
  // 칸 하나가 그리는 표시점 총예산 — 소스 수로 나눠 소스별 캡을 정한다(distCapFor).
  // DOWNSAMPLE 이 소스별 상한이라 소스 40개면 칸 하나가 6만 점이 되는데, IssueTable
  // 미니셀은 높이 112px 라 찍을 수 있는 픽셀이 ~1.7만개뿐이라 전부 덧칠 낭비였다.
  // 소스가 적으면 나눗셈 결과가 DOWNSAMPLE 로 클램프된다(카드 ≤5소스 / 미니 ≤2소스).
  CELL_BUDGET_MINI: 3000,    // IssueTable 미니셀(112px) — 작아서 더 과감히 줄인다
  CELL_BUDGET_CARD: 8000,    // 갤러리 카드 · Bin 상세 셀(263×189px)
  MIN_PER_SOURCE: 150,       // 소스가 아무리 많아도 ECDF 형태는 남기는 하한
  // ECDF 세로 점 보간 간격은 데이터에서 유도한다(distStepY) — "단일 데이터 점 1개의 ECDF
  // 증가량". FILL_STEP_Y 는 유효한 riser 가 없는 퇴화 케이스의 폴백 상수일 뿐.
  FILL_STEP_Y: 0.8,
  // 채움 간격의 시각 연속성 상한(%). 표본이 작아 stepY 가 이보다 굵으면(>0.3%) marker(3px)
  // 세로 점기둥이 점점이 끊겨 보이므로 이 값으로 캡해 썸네일 누적 0~100% 축에 빈 구간이
  // 없게 한다 — 단일점 riser 도 채우는 표시용 업샘플링(썸네일 한정, 상세 CDF 는 별도 경로).
  FILL_VISUAL_MAX_DY: 0.3,
  // 세로 채움점 총량의 절대 상한(성능). 실제 상한은 distStepY 가 유효 캡에 연동해
  // min(FILL_MAX_POINTS, cap×1.5) 로 잡으므로, 기본 캡 1500 에서는 2250 이 지배하고
  // 이 3000 은 도달하지 않는다(캡을 다시 2000 이상으로 올릴 때를 위한 천장).
  FILL_MAX_POINTS: 3000,
  // 상세 CDF(item_detail.js distRenderCdf) 렌더 방식 토글 — true: scattergl(WebGL,
  // 대량 포인트 SVG 프리즈 방지) / false: 기존 SVG scatter 로 즉시 롤백.
  // 데이터·배열 생성 코드는 양쪽 동일하고 trace type 만 바뀐다 (다운샘플 없음).
  CDF_GL: true };
const DIST_STATUS_BG = { fail: "#FDECEC", cpk_low: "#FEF9E7", ok: "#FFFFFF" };  // 연빨강 / 연노랑 / 흰
const DIST_PLOT_BG = {
  paper_bgcolor: "#FFFFFF", plot_bgcolor: "#FFFFFF",
  font: { color: "#1A1A1F", size: 13, family: "Malgun Gothic, 'IBM Plex Sans KR', sans-serif" },
  hoverlabel: { bgcolor: "#FFFFFF", bordercolor: "#E7E7EA",
    font: { color: "#1A1A1F", size: 14, family: "JetBrains Mono, monospace" } },
  hovermode: "closest",
};
const DIST_CFG = { responsive: true, displaylogo: false, displayModeBar: false };
const DIST_CFG_STATIC = { responsive: true, displaylogo: false, displayModeBar: false, staticPlot: true };
let distIndex = [];            // DATA.web_report.distribution_index
// cpk<1.33 / Fail Only 는 독립 토글(동시 on 가능, 둘 다 AND). 둘 다 off 면 전체.
let distCpkOnly = true;        // 기본 진입 cpk<1.33 on
let distFailOnly = false;
let distHidePassfail = true;   // "P/F 없애기" 기본 ON — unit 이 Pass/Fail 인 항목(is_passfail) 카드 숨김
let distLimitOnly = false;     // 켜면 각 분포 차트 x축을 Limit 창으로 클램프(Limit 벗어난 산포 숨김)
// distLimitOnly 켜짐 시 x축 표시범위를 반환(아니면 null → 기존 범위 유지).
// 양측 스펙: [lo,hi]±2% pad. 단측 스펙(한쪽만 있음): 있는 쪽은 pad 클램프, 없는 쪽은
// 데이터 끝(dmin/dmax)으로 잡는다 — 한쪽만 있어도 토글이 반응하게. pad 기준 span 은
// 항상 "보이는 구간"(limit ↔ 반대쪽 경계)이라 양측/단측 비례가 일관된다.
// dmin/dmax(선택): 소스 합친 데이터 최소/최대. 없거나 span≤0 이면 축퇴 폴백 span.
function distLimitRange(lo, hi, dmin, dmax) {
  if (!distLimitOnly) return null;
  const hasLo = lo != null, hasHi = hi != null;
  if (!hasLo && !hasHi) return null;
  const pad = s => ((s > 0 ? s : 0) || Math.abs(hasHi ? hi : lo) || 1) * 0.02;
  if (hasLo && hasHi) { const p = pad(hi - lo); return [lo - p, hi + p]; }
  if (hasHi) {                                  // USL 만 — 아래쪽은 데이터 최소
    const x0 = (dmin != null && isFinite(dmin) && dmin < hi) ? dmin : hi;
    const p = pad(hi - x0);
    return [x0 - p, hi + p];
  }
  const x1 = (dmax != null && isFinite(dmax) && dmax > lo) ? dmax : lo;   // LSL 만
  const p = pad(x1 - lo);
  return [lo - p, x1 + p];
}
// 소스 원본 values 의 min/max — 단측 스펙 클램프에서 "없는 쪽" 경계로 쓴다.
// 토글이 꺼져 있으면 distLimitRange 가 어차피 null 이라 전수 스캔을 생략한다.
function distSourcesRange(sources) {
  let min = Infinity, max = -Infinity;
  if (!distLimitOnly) return { min, max };
  (sources || []).forEach(s => {
    for (const v of (s.values || [])) { if (v < min) min = v; if (v > max) max = v; }
  });
  return { min, max };
}
let distFiltered = [];         // 현재 필터 결과 (갤러리 표시/네비 범위)
let distSelected = new Set();  // 검색 체크박스로 고른 항목(있으면 갤러리를 이 항목들만으로 필터)
let distView = "gallery";      // "gallery" | "detail"
let distGalleryObserver = null;
let distRenderQueue = [];
let distRafScheduled = false;
let distNavList = [];          // 상세 <>/Alt 이동용 캡처 목록
let distNavPos = -1;
let distPanelBound = false;

// ── Distribution 지연 로드 (distribution_deferred 응답용) ─────────────────────
// /full 은 대용량 ECDF 를 내려주지 않고, 첫 페인트 후 백그라운드로
// GET .../web_report/distribution (컴팩트 columnar, 전 포인트) 을 받아 distDataCache 를
// 채운다. 도착 전에 그려진 미니셀/갤러리는 refreshDistConsumers 가 다시 채운다.
let distDataReady = false;     // 배치 로더 사용 가능 여부 (항목 보유는 distDataCache 로 판단)

// ── "Bin1 only" (양품·규격내 분포) — 갤러리 미니셀 전용 별도 캐시(?bin1=1 지연 로드). ──
// Issue Table·item_detail 이 공유하는 전체 기준 distDataCache 와 분리해, 토글 영향이
// Distribution 갤러리에만 국한되게 한다(다른 탭 분포는 전체 기준 유지). 서버는 양품
// (BIN==1) & 규격(LSL/USL) 이내 die 만으로 ECDF 를 재계산한다.
let distBin1Only = false;      // 갤러리 미니셀을 양품(BIN==1) & 규격내 ECDF 로 표시
let distBin1Cache = {};        // subject → {...} (bin1 ECDF, distDataCache 와 동일 스키마)
let distBin1Ready = false;

function buildDistDataFromCompact(payload) {
  // 컴팩트 columnar → 기존 distDataCache 스키마 그대로 (소비자 코드 무수정)
  const out = {};
  const items = (payload && payload.items) || {};
  Object.keys(items).forEach(subj => {
    const it = items[subj], bySource = {};
    Object.keys(it.sources || {}).forEach(src => {
      bySource[src] = { xs: it.sources[src].x || [], ys: it.sources[src].y || [] };
    });
    out[subj] = { lower_limit: it.lo, upper_limit: it.hi, units: it.units || "", bySource };
  });
  return out;
}

// ── 분포 로딩 상태 배지 (우하단 고정 — Distribution 갤러리·Issue Table 미니셀 공용) ──
// 대용량 세션은 지연 로드가 수십 초 걸릴 수 있어, 도착 전 빈 미니셀이 "고장"으로
// 보이지 않도록 전역 배지로 수신 진행(비압축 MB)을 보여준다. 실패 시 재시도 버튼.
let _distBadgeJobs = 0;   // 진행 중 로드 수 (전체/Bin1 동시 로드 대비)
function distBadgeEl(create) {
  let el = document.getElementById("distLoadBadge");
  if (!el && create) {
    el = document.createElement("div");
    el.id = "distLoadBadge";
    el.className = "dist-load-badge";
    el.addEventListener("click", ev => {
      const b = ev.target.closest("[data-dist-retry]");
      if (!b) return;
      distBadgeHide();
      if (b.dataset.distRetry === "bin1") ensureDistBin1Data();
      else if (b.dataset.distRetry === "map") ensureMapData();
      else ensureDistData();
    });
    document.body.appendChild(el);
  }
  return el;
}
function distBadgeShow(html) {
  const el = distBadgeEl(true);
  el.innerHTML = html;
  el.style.display = "";
}
function distBadgeHide() {
  const el = distBadgeEl(false);
  if (el) el.style.display = "none";
}
function distBadgeStart(label) { _distBadgeJobs++; distBadgeShow(esc(label)); }
function distBadgeEnd() {
  _distBadgeJobs = Math.max(0, _distBadgeJobs - 1);
  if (!_distBadgeJobs) distBadgeHide();
}
function distBadgeFail(label, variant) {
  _distBadgeJobs = Math.max(0, _distBadgeJobs - 1);
  distBadgeShow(`${esc(label)} <button type="button" class="btn-sm" data-dist-retry="${variant}">재시도</button>`);
}
function distBadgeProgress(label, loadedBytes) {
  distBadgeShow(`${esc(label)} ${(loadedBytes / 1048576).toFixed(1)} MB 수신…`);
}

// ── 항목 배치 지연 로드 (전량 일괄 로드 대체) ────────────────────────────────
// 종전에는 세션의 ECDF 전량을 한 번에 받았다. 항목·소스·die 가 늘면 수천만 포인트가 되어
// 다운로드·파싱·JS 힙 상주가 전부 폭증하는데, 실제로 화면에 보이는 미니셀은 수십 개뿐이다.
// 이제 IntersectionObserver 가 보이는 셀의 항목만 모아 배치로 요청한다
// (GET .../web_report/distribution_batch?subjects=...). 항목 상세(전 포인트+hover 메타)는
// 종전대로 /scatter/<subject> 를 쓴다. 표시용 다운샘플·세로 채움은 클라 그대로다(규칙 #6).
const DIST_BATCH = {
  SIZE: 30,          // 한 요청 항목 수 (서버 상한 40 — 여유를 둔다)
  MAX_INFLIGHT: 2,   // 동시 요청 수 — 스크롤을 빨리 내려도 요청이 쌓이지 않게
  DEBOUNCE_MS: 50,   // 관측 이벤트가 몰려 들어오므로 모아서 한 번에 보낸다
  CACHE_MAX: 300,    // 보유 항목 상한(LRU) — 오래 스크롤해도 힙이 무한히 자라지 않게
};

let _distSubjectSet = null;    // distribution_index 항목 Set (ECDF 존재 여부의 단일 진실)
// distribution_index 는 "측정 data 전무" 항목만 제외하는데, 서버의 ECDF compact 도 같은
// 기준으로 항목을 고른다 — 즉 인덱스에 있으면 ECDF 가 있고, 없으면 없다. 덕분에 데이터를
// 받아보지 않고도 "이 항목에 분포가 있는지"를 즉시 알 수 있다(빈 셀 판단이 미리 가능).
// distIndex 전역이 아니라 DATA 에서 직접 읽는 이유: distIndex 는 Distribution 탭을 그릴
// 때 채워지는데, Issue Table 이 먼저 그려지면 빈 목록으로 판단이 굳어버린다.
function distHasData(subject) {
  if (!_distSubjectSet) {
    const idx = (DATA && DATA.web_report && DATA.web_report.distribution_index) || [];
    if (!idx.length) return false;   // 아직 /full 전 — Set 을 굳히지 않는다
    _distSubjectSet = new Set(idx.map(r => r.subject));
  }
  return _distSubjectSet.has(subject);
}

const _distPending = { all: new Set(), bin1: new Set() };   // 요청 대기(다음 배치)
const _distHave = { all: new Set(), bin1: new Set() };      // 요청 완료 또는 진행 중
const _distOrder = { all: [], bin1: [] };                   // LRU 축출 순서
let _distInflight = 0;
let _distBatchTimer = null;
let _distRefreshTimer = null;

function distCacheFor(bin1) { return bin1 ? distBin1Cache : distDataCache; }
function distVariantKey(bin1) { return bin1 ? "bin1" : "all"; }

// 배치 도착 후 재렌더 — 여러 배치가 연달아 오므로 한 프레임으로 모은다.
function distScheduleRefresh() {
  if (_distRefreshTimer) return;
  _distRefreshTimer = setTimeout(() => {
    _distRefreshTimer = null;
    refreshDistConsumers();
  }, 0);
}

// 받은 항목을 캐시에 넣되 보유 개수 상한을 유지한다(core.js cachePutCapped 공용).
// 버린 항목은 _distHave 에서도 빼야 다시 보일 때 재요청이 걸린다.
function distCachePut(bin1, subject, info) {
  const key = distVariantKey(bin1);
  cachePutCapped(distCacheFor(bin1), _distOrder[key], subject, info,
                 DIST_BATCH.CACHE_MAX, old => _distHave[key].delete(old));
}

// 이 항목의 ECDF 를 요청 큐에 넣는다 (이미 보유/요청 중이면 no-op).
// 렌더러는 데이터가 없으면 rendered 플래그를 세우지 않고 리턴하고, 도착 후
// refreshDistConsumers 재큐잉으로 다시 그려진다 — 기존 지연 로드와 동일한 흐름.
function distRequestSubject(subject, bin1) {
  if (!subject || !distHasData(subject)) return;
  const key = distVariantKey(bin1);
  if (_distHave[key].has(subject) || _distPending[key].has(subject)) return;
  _distPending[key].add(subject);
  if (_distBatchTimer) return;
  _distBatchTimer = setTimeout(() => { _distBatchTimer = null; distFlushBatch(); },
                               DIST_BATCH.DEBOUNCE_MS);
}

// 배치는 보통 수십 ms 라 배지를 바로 띄우면 스크롤 때마다 깜빡인다. 이 시간 넘게 걸릴
// 때만 띄운다. (전량 로드용 _distBadgeJobs 카운터와 섞지 않고 표시만 직접 제어한다 —
// 맵 로딩 배지가 떠 있으면 그쪽을 지우지 않도록 hide 전에 jobs 를 확인한다.)
const DIST_BATCH_BADGE_DELAY_MS = 400;
let _distBatchBadgeTimer = null;
let _distBatchFailed = false;   // 실패 안내(재시도 버튼)가 떠 있으면 자동으로 걷지 않는다
function distBatchBadgeSync() {
  if (_distInflight > 0) {
    if (_distBatchFailed) return;   // 실패 안내 유지 (재시도 클릭이 지운다)
    if (!_distBatchBadgeTimer) {
      _distBatchBadgeTimer = setTimeout(() => {
        _distBatchBadgeTimer = null;
        if (_distInflight > 0 && !_distBatchFailed) distBadgeShow("분포 데이터 로딩 중…");
      }, DIST_BATCH_BADGE_DELAY_MS);
    }
    return;
  }
  if (_distBatchBadgeTimer) { clearTimeout(_distBatchBadgeTimer); _distBatchBadgeTimer = null; }
  if (!_distBadgeJobs && !_distBatchFailed) distBadgeHide();   // 다른 로딩(맵 등)이 없을 때만
}

function distFlushBatch() {
  if (_distInflight >= DIST_BATCH.MAX_INFLIGHT) return;   // 도착 시 다시 호출된다
  // 전체 기준을 먼저 비운다(갤러리·IssueTable 이 공유하는 기본 캐시). 전체가 비면 bin1.
  const bin1 = _distPending.all.size === 0;
  const key = distVariantKey(bin1);
  const pending = _distPending[key];
  if (!pending.size) return;
  const subjects = Array.from(pending).slice(0, DIST_BATCH.SIZE);
  subjects.forEach(s => { pending.delete(s); _distHave[key].add(s); });

  _distInflight++;
  distBatchBadgeSync();
  const q = encodeURIComponent(subjects.join(","));
  const url = `/pe/report/session/${SESSION_ID}/web_report/distribution_batch`
    + `?subjects=${q}${bin1 ? "&bin1=1" : ""}`;
  fetch(url, { cache: "no-cache" })
    .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(j => {
      const built = buildDistDataFromCompact(j);
      Object.keys(built).forEach(s => distCachePut(bin1, s, built[s]));
      distScheduleRefresh();
    })
    .catch(e => {
      // 실패한 항목은 보유 표시를 되돌려 다음 관측/재시도에서 다시 요청되게 한다.
      subjects.forEach(s => _distHave[key].delete(s));
      _distBatchFailed = true;
      distBadgeShow("분포 데이터 로드 실패 " +
        `<button type="button" class="btn-sm" data-dist-retry="${key}">재시도</button>`);
      showToast("분포 데이터 로드 실패: " + e.message);
    })
    .then(() => {
      _distInflight--;
      distBatchBadgeSync();
      if (_distPending.all.size || _distPending.bin1.size) distFlushBatch();
    });
  if (_distPending.all.size || _distPending.bin1.size) distFlushBatch();   // 남은 배치
}

// 배치 로더는 별도 선행 로드가 필요 없다 — 호출 시 보이는 셀들이 각자 필요한 항목을
// 요청하도록 재큐잉만 한다. (구 전량 로드 진입점과 같은 이름·호출 규약 유지.)
function ensureDistData() {
  distDataReady = true;
  _distBatchFailed = false;   // 재시도 진입점이기도 하다 — 실패 안내를 걷는다
  refreshDistConsumers();
  return Promise.resolve();
}

// 데이터 도착 전에 만들어진 분포 소비처들을 다시 채운다.
function refreshDistConsumers() {
  // Issue Table 미니셀 — 화면에 보이는(관측 중) 셀만 rAF 큐로 재큐잉.
  // 전량 동기 렌더 금지 (수천 셀 × Plotly = 분 단위 freeze). 나머지는 스크롤 시 lazy.
  document.querySelectorAll('#panel-issues .dist-cell-mini[data-visible="1"]')
    .forEach(issueDistQueueRender);
  // Issue Table Bin 상세의 단일 분포 셀
  const detail = document.getElementById("issueDetailDistCell");
  if (detail && detail.dataset.distLoaded !== "1") {
    // 아직 안 받은 항목이면 renderDistCell 이 배치 요청만 하고 리턴한다(도착 후 재호출).
    // 인덱스에 없는 항목만 "없음"으로 확정한다.
    if (distHasData(detail.dataset.subject)) renderDistCell(detail);
    else detail.outerHTML = `<div class="placeholder">분포 데이터 없음</div>`;
  }
  // Distribution 갤러리 — 화면에 보이는(관측 중) 카드 재큐잉
  document.querySelectorAll('#panel-distribution .distg-card[data-visible="1"]')
    .forEach(distQueueRender);
  // Compare 산포 비교 표의 Distribution 미니셀 (compare.js — 로드 순서 무관하게 가드)
  if (typeof cmpDistQueueRender === "function") {
    document.querySelectorAll('#cmp-dist-section .cmp-dist-cell[data-visible="1"]')
      .forEach(cmpDistQueueRender);
  }
}

// ── Bin1 only: 양품(BIN==1) ECDF — 전체 기준과 같은 배치 로더를 bin1 변형으로 쓴다. ──
// 종전에는 토글 시 전 항목 bin1 payload 를 통째로 받았다. 이제 갤러리에 보이는 항목만
// ?bin1=1 배치로 받아 distBin1Cache 에 쌓는다(전체 기준 캐시와 분리는 그대로).
function ensureDistBin1Data() {
  distBin1Ready = true;
  _distBatchFailed = false;   // 재시도 진입점 — 실패 안내를 걷는다
  refreshDistGallery();
  return Promise.resolve();
}

// 갤러리에 보이는(관측 중) 카드만 재큐잉 — Bin1 데이터 도착 시 미니셀을 다시 채운다.
// (refreshDistConsumers 와 달리 Issue Table/상세는 건드리지 않아 전체 기준을 보존.)
function refreshDistGallery() {
  document.querySelectorAll('#panel-distribution .distg-card[data-visible="1"]')
    .forEach(distQueueRender);
}

function buildDistColorMap(sources) {
  distColorMap = {};
  // F10 에서 지정한 색(웹리포트 payload.dist_colors)이 있으면 source 순서대로 사용,
  // 없으면(legacy) 기본 팔레트 자동 배정. 색 번호 i = source i.
  const custom = (DATA && DATA.web_report && Array.isArray(DATA.web_report.dist_colors))
    ? DATA.web_report.dist_colors : null;
  (sources || []).forEach((s, i) => {
    // 지정 색 없는 자리: 1~10번째는 기존 DIST_PALETTE 유지(legacy 무회귀),
    // 그 이후는 모듈로 순환(색 중복) 대신 48색 공식 연장으로 고유 색 보장.
    distColorMap[s.name] = (custom && custom[i]) ? custom[i]
      : (i < DIST_PALETTE.length ? DIST_PALETTE[i] : distDefaultColor(i));
  });
}

function distColorFor(source) { return distColorMap[source] || "#888"; }

// ── legend 강조(source 필터) — wafer_charts.js dimColorMap 과 같은 규격 ────────
// 비어 있으면 전 소스 원색(기존과 완전 동일), 하나 이상 고르면 고른 소스만 원색이고
// 나머지는 회색으로 죽인다. 갤러리 툴바 범례와 item_detail 상단 범례가 이 집합 하나를
// 공유한다. 서버 저장 없는 표시 설정이라 갤러리↔상세 이동·항목 변경에도 유지하고
// (cdfExcluded 와 달리 openItemDetail 에서 초기화하지 않는다) 새로고침에서만 풀린다.
let distSourceFilter = new Set();
const DIST_DIM_COLOR = "#d9d9d9";        // wafer_charts MAP_BIN_DIM_COLOR 와 동일 값
function distActiveColorFor(source) {
  if (!distSourceFilter.size) return distColorFor(source);
  return distSourceFilter.has(source) ? distColorFor(source) : DIST_DIM_COLOR;
}
// 강조가 걸리면 dim 소스가 먼저(=아래에) 그려지도록 정렬한 사본 — 산포가 겹치면 색만
// 바꿔서는 강조 소스가 뒤 trace 에 깔려 안 보인다(distDrawPoints 의 order 정렬과 같은
// 규칙, 안정 정렬이라 그룹 내부는 원래 순서 유지). 필터가 비면 원본 그대로(그리기 순서 불변).
function distOrderedSources(list) {
  list = list || [];
  if (!distSourceFilter.size) return list;
  return list.slice().sort((a, b) => (distSourceFilter.has(a.name) ? 1 : 0) - (distSourceFilter.has(b.name) ? 1 : 0));
}

// source 가 이 수 이상이면 상세 3개 차트(CDF/히스토그램/정규분포)의 내장 세로 legend 를
// 끄고 차트 위 공용 legend 스트립 1개로 대체 — 내장 legend 가 차트 폭을 잠식하고
// 같은 내용이 3중복되는 것을 방지. 미만이면 기존 내장 legend 그대로(무회귀).
const DIST_EXT_LEGEND_MIN = 8;
function distUseExtLegend(data) { return ((data && data.sources) || []).length >= DIST_EXT_LEGEND_MIN; }

// ── source 색 범례 (갤러리 툴바 · item_detail 상단 공용) ──────────────────────
// 클릭 → 해당 source 강조(distSourceFilter). 스와치는 항상 원색(distColorFor)이다 —
// 강조 표시는 행 배경으로 하고 스와치까지 죽이면 어떤 소스인지 못 알아본다
// (wafer map 범례가 dimColorMap 을 맵에만 적용하는 것과 동일 규칙).
// 소스가 많으면 툴바 flex 행을 밀어내므로 별도 행의 고정폭 그리드로 뽑고 기본 접힘.
const DIST_LEGEND_COLLAPSE_MIN = 8;
let distLegendOpen = false;   // 펼침 상태 — 갤러리 재렌더(innerHTML 교체)에도 유지
function distLegendHtml(sources, cls) {
  const list = sources || [];
  if (!list.length) return "";
  const many = list.length >= DIST_LEGEND_COLLAPSE_MIN;
  const open = !many || distLegendOpen;
  const items = list.map(s => {
    const on = distSourceFilter.has(s.name);
    return `<span class="dist-leg-item${on ? " is-selected" : ""}" data-dist-src="${esc(s.name)}" title="${esc(s.name)}">` +
      `<span class="dist-leg-sw" style="background:${distColorFor(s.name)}"></span>` +
      `<span class="dist-leg-nm">${esc(s.name)}</span></span>`;
  }).join("");
  const toggle = many
    ? `<button type="button" class="btn-sm dist-leg-toggle" data-dist-leg="toggle">${open ? "▴" : "▾"} 범례 ${list.length}개</button>` : "";
  const clear = distSourceFilter.size
    ? `<button type="button" class="btn-sm dist-leg-clear" data-dist-leg="clear">강조 ${distSourceFilter.size}개 해제</button>` : "";
  return `<div class="dist-legend-row${cls ? " " + cls : ""}${open ? " is-open" : ""}">` +
    toggle + clear + `<div class="dist-legend">${items}</div></div>`;
}
// 강조 반영: 그려져 있는 캔버스·상세 차트 색만 갈고 범례 자신의 선택표시를 갱신한다.
// 갤러리 전체 재렌더(distRenderGallery)를 하지 않으므로 스크롤 위치·렌더된 칸이 보존된다.
function distApplySourceFilter() {
  distRepaintPoints();
  if (typeof idetRestyleSourceColors === "function") {
    idetRestyleSourceColors(document.getElementById("distCdf"), "marker.color");
    idetRestyleSourceColors(document.getElementById("distHist"), "line.color");
    idetRestyleSourceColors(document.getElementById("distNormal"), "line.color");
  }
  distRenderLegends();
}
// 현재 DOM 에 있는 범례 행만 제자리 교체. 리스너는 패널 위임이라 outerHTML 로 갈아도 안전하다.
function distRenderLegends() {
  const g = document.querySelector("#panel-distribution .dist-legend-row");
  if (g) g.outerHTML = distLegendHtml((DATA.web_report && DATA.web_report.sources) || [], "");
  // 상세는 idetLegendHtml 을 거쳐야 게이트가 일관된다 — 소스가 적은데 강조를 해제하면
  // 빈 문자열이 돌아와 행이 사라지고 Plotly 내장 legend 가 다시 그 역할을 맡는다.
  const d = document.querySelector("#panel-item-detail .dist-legend-row");
  if (d && _itemDetailData && typeof idetLegendHtml === "function") d.outerHTML = idetLegendHtml(_itemDetailData);
}
// 범례 클릭 처리(두 패널 공용) — 처리했으면 true 를 돌려 호출측이 조기 반환하게 한다.
function distLegendClick(e) {
  const b = e.target.closest("[data-dist-leg]");
  if (b) {
    if (b.dataset.distLeg === "toggle") distLegendOpen = !distLegendOpen;
    else distSourceFilter.clear();
    distApplySourceFilter();
    return true;
  }
  const it = e.target.closest("[data-dist-src]");
  if (!it) return false;
  const s = it.dataset.distSrc;
  if (distSourceFilter.has(s)) distSourceFilter.delete(s); else distSourceFilter.add(s);
  distApplySourceFilter();
  return true;
}

// Limit 값이 없으면 물음표 대신 공백으로 둔다 (사용자 요청).
function distFmtLimit(v) { return (v === null || v === undefined) ? "" : String(v); }
// "lo ~ hi" 텍스트. 상·하한이 모두 없으면 구분자 "~" 만 남지 않도록 통째로 생략한다.
function distLimText(lo, hi) {
  const l = distFmtLimit(lo), h = distFmtLimit(hi);
  return (l === "" && h === "") ? "" : `${l} ~ ${h}`;
}
// 단위를 대괄호로 감싼다 (예: uA → [uA]). 빈 단위는 "" 반환.
function distUnitBr(u) { u = (u === null || u === undefined) ? "" : String(u).trim(); return u ? "[" + esc(u) + "]" : ""; }
// CDF 카드/항목 상세 헤더의 "lo ~ hi [unit]" inner HTML — Limit(lo~hi)은 진한 파랑,
// 단위 문자(대괄호 안 V 등)는 진한 초록으로 강조한다(대괄호는 기본색). (사용자 요청)
function distLimInnerHtml(lo, hi, units) {
  const u = (units === null || units === undefined) ? "" : String(units).trim();
  const t = distLimText(lo, hi);
  const range = t ? `<span class="dist-lim-range">${esc(t)}</span>` : "";
  const unit = u ? `${range ? " " : ""}[<span class="dist-lim-unit">${esc(u)}</span>]` : "";
  return range + unit;
}

// Issue Table Bin 상세의 단일 분포 셀 — 산포탭 갤러리 카드와 동일한 포맷(표시용 다운샘플 static
// CDF + LSL/USL 점선 + status 배경). distDataCache 재사용(전체점은 데이터에 그대로 유지).
function renderDistCell(cell) {
  if (!cell || cell.dataset.distLoaded === "1") return;
  const subject = cell.dataset.subject;
  const info = distDataCache[subject];
  const div = cell.querySelector(".dist-plot");
  const placeholder = cell.querySelector(".dist-placeholder");
  // 아직 안 받은 항목은 배치로 요청 — 도착 후 refreshDistConsumers 가 다시 부른다.
  if (!info && distHasData(subject)) { distRequestSubject(subject, false); return; }
  if (!info || !div || typeof Plotly === "undefined") return;

  const idx = distIndex.find(x => x.subject === subject);
  const status = (idx && idx.status) || "ok";
  const lo = info.lower_limit, hi = info.upper_limit;
  // markers 전용(선 금지 — CLAUDE.md §5). 세로 점 보간으로 이산값 성김을 보정.
  // 점은 canvas 로 그리고 Plotly 에는 축 재현용 sentinel 만 넘긴다(distPaintPoints).
  const pts = {};
  const srcNames = Object.keys(info.bySource);
  const cap = distCapFor(srcNames.length, DIST.CELL_BUDGET_CARD);
  srcNames.forEach(source => { pts[source] = distDisplayPoints(info.bySource[source], cap); });
  const sentinel = distSentinelTrace(pts);
  const traces = sentinel ? [sentinel] : [];
  const layout = { ...DIST_PLOT_BG, plot_bgcolor: DIST_STATUS_BG[status] || "#FFFFFF",
    title: {
      text: `<b>${esc(subject)}</b><br><span style="font-size:10px">` +
        `(${[esc(distLimText(lo, hi)), distUnitBr(info.units)].filter(Boolean).join(" ")})</span>`,
      font: { size: 12 }, x: 0.5, xanchor: "center" },
    xaxis: { showgrid: true, gridcolor: "#eee", zeroline: false, ticks: "outside",
      tickcolor: "#bbb", tickfont: { size: 10 } },
    yaxis: { range: [0, 100], ticksuffix: "%", showgrid: true, gridcolor: "#eee",
      zeroline: false, tickfont: { size: 10 } },
    shapes: distSpecShapes(lo, hi, false).concat(beforeLimitShapes(subject)),
    annotations: distSpecAnnos(lo, hi, false),
    margin: { l: 46, r: 14, t: 46, b: 28 }, showlegend: false };

  Plotly.newPlot(div, traces, layout, DIST_CFG_STATIC);
  distPaintPoints(div, pts, null);
  cell.dataset.distLoaded = "1";
  if (placeholder) placeholder.style.display = "none";
}

// ── 세그먼트 / 타입어헤드 필터 ─────────────────────────────────────────────────
function distApplySegment(rows) {
  let out = rows.slice();
  if (distHidePassfail) out = out.filter(r => !r.is_passfail);
  if (distCpkOnly) {
    out = out.filter(r => r.cpk != null && r.cpk < DIST.CPK_GOOD
      && !DIST.EXCLUDE.some(k => String(r.subject).toLowerCase().includes(k)));
  }
  if (distFailOnly) out = out.filter(r => r.is_fail);
  return out;
}
// cap: 반환 상한(기본 30 — 드롭다운 표시용). 0 을 주면 전량 반환 — '전체 선택'이
// 표시된 30개가 아니라 실제 일치 항목 전부를 담게 하기 위함.
function distSuggestions(q, cap) {
  // 단일어 부분일치(대소문자 무시). 세그먼트(cpk<1.33 등)에 걸리지 않게 전체 항목에서 검색해
  // 어떤 항목이든 체크박스로 고를 수 있게 한다.
  const term = String(q || "").trim().toLowerCase();
  if (!term) return [];
  const lim = (cap === undefined) ? 30 : cap;
  const out = [];
  for (const r of distIndex) {
    if (String(r.subject).toLowerCase().includes(term)) {
      out.push(r);
      if (lim && out.length >= lim) break;
    }
  }
  return out;
}

// ── 기준선(shapes) / 라벨(annotations): LSL·USL 세로 점선 + y=50% 가로 점선 ─────
function distSpecShapes(lo, hi, withMid) {
  const sh = [];
  [lo, hi].forEach(v => { if (v !== null && v !== undefined) sh.push({
    type: "line", x0: v, x1: v, yref: "paper", y0: 0, y1: 1,
    line: { color: "#DC2626", width: 1.2, dash: "dash" } }); });
  if (withMid) sh.push({ type: "line", xref: "paper", x0: 0, x1: 1, y0: 50, y1: 50,
    line: { color: "#888", width: 1, dash: "dashdot" } });
  return sh;
}
function distSpecAnnos(lo, hi, mini) {
  const fs = mini ? 9.2 : 11.5;
  const mk = (v, label) => ({ x: v, yref: "paper", y: 1, text: `${label} ${v}`,
    showarrow: false, textangle: -90, font: { size: fs, color: "#DC2626" },
    bgcolor: "rgba(255,255,255,.72)", borderpad: 1, xanchor: "left", yanchor: "top" });
  const a = [];
  if (hi !== null && hi !== undefined) a.push(mk(hi, "USL"));
  if (lo !== null && lo !== undefined) a.push(mk(lo, "LSL"));
  return a;
}

// ── Compare(goodlog) before-limit 회색 기준선: 이름 같고 limit 만 바뀐 항목의 before
//    limit 을 distribution 차트에 회색 점선으로 덧그림 (Honey Compare Mode 이식). ─────
function beforeLimitsFor(subject) {
  const wr = DATA.web_report;
  const gl = wr && wr.compare && wr.compare.goodlog;
  const m = gl && gl.limit_change_map;
  return (m && m[subject]) || null;   // [before_lo|null, before_hi|null]
}
function beforeLimitShapes(subject) {
  const bl = beforeLimitsFor(subject);
  if (!bl) return [];
  const sh = [];
  bl.forEach(v => { if (v !== null && v !== undefined) sh.push({
    type: "line", x0: v, x1: v, yref: "paper", y0: 0, y1: 1,
    line: { color: "#9ca3af", width: 1.2, dash: "dash" } }); });
  return sh;
}
function beforeLimitAnnos(subject) {
  const bl = beforeLimitsFor(subject);
  if (!bl) return [];
  const mk = (v, label) => ({ x: v, yref: "paper", y: 1, text: `${label} ${v}`,
    showarrow: false, textangle: -90, font: { size: 11.5, color: "#6b7280" },
    bgcolor: "rgba(255,255,255,.72)", borderpad: 1, xanchor: "left", yanchor: "top" });
  const a = [];
  if (bl[1] !== null && bl[1] !== undefined) a.push(mk(bl[1], "이전 USL"));
  if (bl[0] !== null && bl[0] !== undefined) a.push(mk(bl[0], "이전 LSL"));
  return a;
}
// x축 표시범위에 before limit 포함 (범위 밖이면 회색선이 안 보이는 것 방지).
function extendRangeForBeforeLimits(range, subject) {
  const bl = beforeLimitsFor(subject);
  if (!bl || !range) return range;
  let x0 = range[0], x1 = range[1];
  bl.forEach(v => { if (v !== null && v !== undefined) {
    if (v < x0) x0 = v; if (v > x1) x1 = v; } });
  return [x0, x1];
}

// 소스 수로 나눈 소스별 유효 캡. 소스가 적으면 DIST.DOWNSAMPLE 로 클램프되므로 기존
// 동작과 바이트 단위로 같고, 다소스(수십 개)에서만 칸 예산을 나눠 갖는다.
function distCapFor(nSources, cellBudget) {
  const per = Math.floor(cellBudget / Math.max(nSources, 1));
  return Math.max(Math.min(per, DIST.DOWNSAMPLE), DIST.MIN_PER_SOURCE);
}

// 강제 보존(꼬리·Δy·x갭)만으로도 캡을 넘을 수 있다. 기본 캡 경로에서는 그 초과를 허용
// (소프트 상한 — 왜곡 없음이 우선)하지만, 다소스 미니셀처럼 캡이 기본값보다 낮게 잡힌
// 경우에는 그 초과가 곧 성능 문제라 마지막에 균등 stride 로 캡까지 낮춘다(양끝 유지).
function distHardCap(xs, ys, cap) {
  const n = xs.length;
  if (n <= cap || cap < 3) return { xs, ys };
  const ox = [xs[0]], oy = [ys[0]];
  const st = (n - 1) / (cap - 1);
  for (let i = 1; i < cap - 1; i++) { const j = Math.round(i * st); ox.push(xs[j]); oy.push(ys[j]); }
  ox.push(xs[n - 1]); oy.push(ys[n - 1]);
  return { xs: ox, ys: oy };
}

// ── 미니셀 표시용 다운샘플 (ECDF 전제: x 오름차순, y 누적%) ────────────────────
// 단순 stride 는 꼬리 outlier·고질량 계단(Δy 큰 점)·x축 고립점을 눈멀고 떨어뜨려
// 누적산포를 왜곡한다. 규칙: (1) 첫/마지막 + 누적% 상·하위 3% 전량 보존,
// (2) Δy ≥ 0.15%p(질량 큰 고유값)·x갭 ≥ range×0.1%(갭 양끝) 강제 보존,
// (3) 나머지만 잔여 budget 으로 균등 stride. 강제 보존 때문에 총점이 DIST.DOWNSAMPLE 을
// 다소 넘는 것은 허용(소프트 상한) — 왜곡 없음이 우선.
//
// 임계값 근거(2026-07-20, 미니셀 실측 플롯영역 263×189px 기준 — 1px ≒ y 0.53%p / x 범위의
// 0.38%): 강제 보존은 kept 를 키우고 kept 가 캡을 넘으면 budget 이 아래 하한까지 떨어져
// **몸통이 성겨진다**. 꼬리가 두꺼운 분포(늘어지는 die 10~35%)에서 옛 5% 밴드는 꼬리에만
// 예산을 다 써 전량 렌더 대비 픽셀 오차가 10~20% 까지 벌어졌다. 3% 로 좁히면 같은 분포에서
// 오차가 3% 안팎으로 6배 줄고 점 수도 준다(꼬리 자체는 픽셀상 겹쳐 손실이 안 보임).
// x갭은 반대로 1.31px→0.26px 상당(0.005→0.001)으로 조여 사람이 구분 못 하는 크기의 고립점
// 까지 살린다 — 두꺼운 꼬리 10% 에서 오차 0.97%→0.32%, 점은 소스당 +226(1995→2221)이고
// 캔버스 드로잉은 0.37→0.40ms 로 측정 노이즈 수준이다. 조밀·초조밀·lognormal 은 애초에
// 이 규칙이 발동하지 않아(xg=0) 무영향이고, 고립점을 일부러 흩뿌린 최악 케이스에서도
// 0.05% 까지 내려야 점이 +680 늘 뿐이라 폭증 위험은 없다. 더 내리면(0.05%) 이득이 사라지고
// 꼬리 35% 케이스는 오히려 미세하게 나빠져 0.1% 가 스위트스팟.
// Δy 0.15%p 는 이미 0.28px(지각 한계 미만)이고, 세로채움 후 이웃 간 Δy 가 항상 stepY
// (≤0.3%p, 보통 0.03%p)라 실측상 한 번도 발동하지 않는다 — 건드릴 이유가 없어 유지.
function distDownsampleForDisplay(xs, ys, cap) {
  const CAP = cap || DIST.DOWNSAMPLE;
  const n = xs.length;
  if (n <= CAP) return { xs, ys };
  const keep = new Uint8Array(n);
  keep[0] = 1; keep[n - 1] = 1;
  const range = xs[n - 1] - xs[0];
  const gap = range > 0 ? range * 0.001 : Infinity;
  for (let i = 0; i < n; i++) {
    if (ys[i] <= 3 || ys[i] >= 97) { keep[i] = 1; continue; }             // 꼬리 전량
    if (ys[i] - ys[i - 1] >= 0.15) keep[i] = 1;                           // Δy 질량
    if (xs[i] - xs[i - 1] >= gap) { keep[i] = 1; keep[i - 1] = 1; }       // x갭 양끝
  }
  let kept = 0;
  for (let i = 0; i < n; i++) kept += keep[i];
  const rest = [];
  for (let i = 0; i < n; i++) if (!keep[i]) rest.push(i);
  // 꼬리에 고유값이 많아 budget 이 소진되어도 중간 몸통이 비지 않도록 최소 800점 보장.
  // 이 하한은 kept 가 캡을 넘는 조밀 분포(고유값 ≳2만)에서만 작동하고, 그 외에는 자연
  // budget 이 이미 800 언저리라 no-op 이다(실측: 두꺼운 꼬리·롱테일 케이스는 800 까지
  // 출력 불변). 200 → 800 으로 올리면 전량 렌더 대비 픽셀 오차가 조밀 40k 3.3%→1.1%,
  // 초조밀 100k 3.8%→2.2% 로 줄고 점은 소스당 600개 안쪽 증가(캔버스 렌더라 비용 무시 가능).
  // 캡을 올리는 쪽은 대안이 못 된다 — 초조밀에서는 kept 가 어떤 캡보다도 커 하한이 계속 지배한다.
  // 하한도 캡에 비례시킨다(0.4×) — 캡이 낮을 때 하한이 캡을 넘어서는 모순을 막는다.
  // 기본 캡 1500 에서는 600 이 적용된다(캡 2000 시절의 800 에서 같은 비율로 내려감).
  const budget = Math.max(CAP - kept, Math.min(800, Math.round(CAP * 0.4)));
  if (budget > 0 && rest.length > budget) {
    const st = Math.ceil(rest.length / budget);
    for (let j = 0; j < rest.length; j += st) keep[rest[j]] = 1;
  } else if (budget > 0) {
    for (const i of rest) keep[i] = 1;
  }
  const ox = [], oy = [];
  for (let i = 0; i < n; i++) if (keep[i]) { ox.push(xs[i]); oy.push(ys[i]); }
  // 캡이 기본값보다 낮게 잡힌 경우(다소스 미니셀)만 초과분을 마지막에 잘라낸다.
  // 기본 캡 경로는 이 분기를 타지 않아 기존 출력과 완전히 동일하다.
  if (CAP < DIST.DOWNSAMPLE) return distHardCap(ox, oy, CAP);
  return { xs: ox, ys: oy };
}

// ── ECDF 세로 점 보간 (markers 전용, 선 절대 금지 — CLAUDE.md §5) ──────────────
// 백엔드가 동일값을 1점으로 축약(np.unique)하므로 이산(code)값은 점이 성기게 찍힌다.
// 각 고유값 x_i 의 riser(prevY→y_i)를 x=x_i 에 stepY 간격 세로 점으로 채워 "연속 분포"로
// 보이게 한다. 점끼리 잇지 않으므로 x축 수평선(계단 tread)은 자연히 없다.
// stepY 는 호출부(distStepY)가 min(단일 점 1개의 증가량, FILL_VISUAL_MAX_DY 0.3%) 로
// 유도한다 — riser 는 ECDF 계단함수의 실제 세로 구간이므로 단일점 riser 포함 모든 riser 를
// 0.3% 이하 간격으로 채워 누적 0~100% 에 marker 빈 구간이 없게 한다(x값 조작 없는
// 세로 방향 표시용 업샘플링). 가로(x) 방향 보간은 계속 금지.
// 전제: xs 오름차순, ys 단조 비감소·마지막 100, ECDF 시작 누적 0 (cumulative_distribution_full 보장).
function distFillVertical(xs, ys, stepY) {
  const n = xs.length;
  if (n === 0) return { xs: [], ys: [] };
  const ox = [], oy = [];
  let prevY = 0;                               // ECDF 는 0 에서 첫 riser 시작
  for (let i = 0; i < n; i++) {
    const x = xs[i], y = ys[i];
    // riser 중간점: prevY 다음 stepY 지점부터 y_i 미만까지. Δy<stepY(정규 산포)면 루프
    // 0회 → 실제 점만 남아 기존과 픽셀 동일. 실제 ECDF 점은 항상 마지막에 보존.
    for (let yy = prevY + stepY; yy < y - 1e-9; yy += stepY) { ox.push(x); oy.push(yy); }
    ox.push(x); oy.push(y);
    prevY = y;
  }
  return { xs: ox, ys: oy };
}

// 세로 채움 간격(stepY): 소스 내 "단일 데이터 점 1개의 ECDF 증가량" = 최소 양의 Δy
// (첫 riser 0→ys[0] 포함)를 FILL_VISUAL_MAX_DY(0.3%)로 캡한다. 표본이 작아 단일점
// 증가량이 0.3% 를 넘으면(대략 표본<333) 단일점 riser 까지 포함해 모든 riser 가 0.3%
// 간격으로 채워져 썸네일 누적축이 끊김 없이 보인다. 조밀한 데이터(stepY≤0.3%)는 캡이
// no-op 라 기존과 픽셀 동일.
// 표본이 매우 커 stepY 가 지나치게 잘면 100/fillMax 하한으로 채움점 폭증을 막는다.
// fillMax 는 유효 캡에 연동한다(cap×1.5) — 기본 캡 1500 이면 2250(절대 천장
// FILL_MAX_POINTS 3000 미만이라 이쪽이 지배), 다소스 미니셀처럼 캡이 낮으면 수천 개를
// 채웠다가 150 개만 남기는 낭비를 애초에 안 한다(채움 계산 비용 자체가 준다).
function distStepY(ys, cap) {
  let step = Infinity, prev = 0;
  for (let i = 0; i < ys.length; i++) {
    const d = ys[i] - prev;
    if (d > 1e-9 && d < step) step = d;
    prev = ys[i];
  }
  if (!isFinite(step)) step = DIST.FILL_STEP_Y;              // 유효 riser 없음 — 폴백
  const fillMax = Math.min(DIST.FILL_MAX_POINTS, Math.round((cap || DIST.DOWNSAMPLE) * 1.5));
  return Math.min(Math.max(step, 100 / fillMax), DIST.FILL_VISUAL_MAX_DY);
}

// 미니셀 표시용 좌표: 세로 보간 → 표시용 다운샘플 순서(순서 근거 CLAUDE.md §5·docs/11).
// 반대 순서면 다운샘플 stride 로 Δy 가 오염돼 없던 가짜 세로 줄무늬가 생긴다.
function distPointsForDisplay(xs, ys, cap) {
  const f = distFillVertical(xs, ys, distStepY(ys, cap));
  return distDownsampleForDisplay(f.xs, f.ys, cap);
}

// 표시용 좌표 메모 — 스크롤로 purge→재진입할 때마다 소스별 세로채움+다운샘플을 다시
// 돌리지 않게 한다. 키는 bySource 항목 객체 자체라, 재fetch 로 캐시가 통째로 교체되면
// (buildDistDataFromCompact) 새 객체가 되어 자동 무효화된다. 같은 항목이라도 칸 종류에
// 따라 캡이 달라지므로(갤러리 vs IssueTable 미니셀) 캡별로 따로 담는다.
const _distDisplayMemo = new WeakMap();
function distDisplayPoints(entry, cap) {
  const key = cap || DIST.DOWNSAMPLE;
  let m = _distDisplayMemo.get(entry);
  if (!m) { m = new Map(); _distDisplayMemo.set(entry, m); }
  let v = m.get(key);
  if (!v) { v = distPointsForDisplay(entry.xs, entry.ys, key); m.set(key, v); }
  return v;
}

// ── 미니셀 점 렌더: 축·그리드·스펙선은 Plotly, ECDF 점만 canvas 오버레이 ────────
// 표시점 캡(DIST.DOWNSAMPLE)은 소스별이라 소스 S개면 S×캡 만큼 SVG 마커 DOM 이 생겨
// 소스가 늘수록 카드가 급격히 느려진다.
// 점만 canvas 로 옮기면 마커 DOM 이 0 이 되고, 좌표·색·점 크기·표시점 계산
// (distPointsForDisplay)은 그대로라 그림은 기존과 동일하다. 상세 CDF(item_detail.js
// distRenderCdf, 전량 scattergl)는 별개 경로라 무관.
const DIST_MARKER_R = 1.5;        // marker.size 3 은 지름 → 반지름 1.5px

// Plotly 에 넘길 투명 sentinel: 전 소스 통합 min/max x 2점. 점을 canvas 로 뺀 뒤에도
// x autorange 기여를 그대로 재현한다(표시용 다운샘플은 양끝점을 항상 보존 → 값 불변).
function distSentinelTrace(ptsBySource) {
  let xMin = Infinity, xMax = -Infinity;
  Object.keys(ptsBySource).forEach(src => {
    const xs = ptsBySource[src].xs;
    if (xs && xs.length) {
      if (xs[0] < xMin) xMin = xs[0];
      if (xs[xs.length - 1] > xMax) xMax = xs[xs.length - 1];
    }
  });
  if (xMin === Infinity) return null;
  return { type: "scatter", mode: "markers", cliponaxis: false, x: [xMin, xMax], y: [0, 100],
    marker: { color: "#000", size: 3, opacity: 0 }, hoverinfo: "skip", showlegend: false };
}

// Plotly 축 좌표계(_offset + l2p)로 점을 canvas 에 찍는다. extra 는 Plotly trace 형태의
// 단일점 마커(chipMarkersFor) — canvas 가 SVG 위에 있어 그대로 두면 점에 가려지므로
// 같은 좌표에 다시 그려 위로 올린다(autorange 기여 때문에 Plotly trace 로도 남겨둔다).
function distDrawPoints(plot) {
  const pts = plot._distPts;
  const fl = plot._fullLayout;
  if (!pts || !fl || !fl.xaxis || typeof fl.xaxis.l2p !== "function" || fl.xaxis._offset == null) return false;
  const xa = fl.xaxis, ya = fl.yaxis;
  const w = plot.clientWidth, h = plot.clientHeight;
  if (!w || !h) return false;
  const dpr = window.devicePixelRatio || 1;
  let cv = plot.querySelector("canvas.dist-pts");
  if (!cv) {
    if (getComputedStyle(plot).position === "static") plot.style.position = "relative";
    cv = document.createElement("canvas");
    cv.className = "dist-pts";
    cv.style.cssText = "position:absolute;left:0;top:0;pointer-events:none;z-index:3;";
    plot.appendChild(cv);
  }
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  cv.style.width = w + "px"; cv.style.height = h + "px";
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const ox = xa._offset, oy = ya._offset, TAU = Math.PI * 2;
  // 강조가 걸리면 dim 소스를 먼저 칠해 강조 소스가 항상 위로 오게 한다(40소스 미니셀은
  // 점이 서로 덮어 강조가 묻힌다). sort 는 ES2019 이후 안정 정렬이라 그룹 내부 순서는
  // Object.keys 순서 그대로다. 필터가 비면 정렬 자체를 건너뛰어 기존과 그리기 순서·
  // 출력이 바이트 단위로 같다.
  const srcs = Object.keys(pts);
  const order = distSourceFilter.size
    ? srcs.slice().sort((a, b) => (distSourceFilter.has(a) ? 1 : 0) - (distSourceFilter.has(b) ? 1 : 0))
    : srcs;
  order.forEach(src => {
    const xs = pts[src].xs, ys = pts[src].ys;
    ctx.fillStyle = distActiveColorFor(src);
    ctx.beginPath();
    for (let i = 0; i < xs.length; i++) {
      const px = ox + xa.l2p(xs[i]), py = oy + ya.l2p(ys[i]);
      ctx.moveTo(px + DIST_MARKER_R, py);
      ctx.arc(px, py, DIST_MARKER_R, 0, TAU);
    }
    ctx.fill();
  });
  (plot._distExtra || []).forEach(t => {
    if (!t.x || !t.x.length) return;
    const m = t.marker || {}, r = (m.size || 7) / 2;
    const px = ox + xa.l2p(t.x[0]), py = oy + ya.l2p(t.y[0]);
    ctx.beginPath(); ctx.arc(px, py, r, 0, TAU);
    ctx.fillStyle = m.color || "#000"; ctx.fill();
    if (m.line && m.line.width) {
      ctx.lineWidth = m.line.width; ctx.strokeStyle = m.line.color || "#fff"; ctx.stroke();
    }
  });
  return true;
}

// 리사이즈(responsive)·재플롯 후 캔버스가 축과 어긋나지 않게 다시 그린다.
let _distCanvasRO = null;
function distPaintPoints(plot, ptsBySource, extraTraces) {
  plot._distPts = ptsBySource;
  plot._distExtra = extraTraces || null;
  if (!distDrawPoints(plot)) requestAnimationFrame(() => distDrawPoints(plot));
  if (plot._distHooked) return;
  plot._distHooked = true;
  if (typeof plot.on === "function") plot.on("plotly_afterplot", () => distDrawPoints(plot));
  if (typeof ResizeObserver !== "undefined") {
    if (!_distCanvasRO) _distCanvasRO = new ResizeObserver(ents => {
      ents.forEach(en => requestAnimationFrame(() => distDrawPoints(en.target)));
    });
    _distCanvasRO.observe(plot);
  }
}
function distClearPoints(plot) {
  if (!plot) return;
  const cv = plot.querySelector("canvas.dist-pts");
  if (cv) cv.remove();
  plot._distPts = null; plot._distExtra = null;
  if (_distCanvasRO && plot._distHooked) { try { _distCanvasRO.unobserve(plot); } catch (e) {} }
  plot._distHooked = false;
}
// legend 강조 변경 시 이미 그려진 캔버스만 다시 칠한다 — Plotly 재플롯 없음.
// 좌표·축·표시점(_distPts)은 그대로라 색과 그리기 순서만 바뀐다(§5 다운샘플·markers 무영향).
// .distg-plot = 갤러리 카드, .dist-plot = Issue Table 미니셀 + Bin 상세 셀.
function distRepaintPoints() {
  document.querySelectorAll(".distg-plot, .dist-plot").forEach(p => { if (p._distPts) distDrawPoints(p); });
}

// 갤러리 미니셀이 쓸 활성 분포 캐시/준비상태 — Bin1 only 토글 시 양품 캐시로 전환.
function distGalleryCache() { return distBin1Only ? distBin1Cache : distDataCache; }
function distGalleryReady() { return distBin1Only ? distBin1Ready : distDataReady; }

// ── 갤러리 미니셀(정적 CDF, distDataCache 재사용, 표시용만 1500점 다운샘플) ─────
function distRenderGalleryCell(cell) {
  if (cell.dataset.rendered === "1") return;
  if (!distGalleryReady()) return;
  const subject = cell.dataset.subject;
  const status = cell.dataset.status || "ok";
  const info = distGalleryCache()[subject];
  // 이 항목 ECDF 가 아직 없으면 배치로 요청하고 리턴 — rendered 플래그를 세우지 않아야
  // 도착 후 refreshDistConsumers/refreshDistGallery 재큐잉으로 다시 그려진다.
  // (인덱스에 아예 없는 항목은 데이터가 없는 것이 확정이라 그대로 빈 축만 그린다.)
  if (!info && distHasData(subject)) { distRequestSubject(subject, distBin1Only); return; }
  const plot = cell.querySelector(".distg-plot");
  if (!plot || typeof Plotly === "undefined") return;
  const lo = info ? info.lower_limit : null;
  const hi = info ? info.upper_limit : null;
  // markers 전용(선 금지 — CLAUDE.md §5). 세로 점 보간(distPointsForDisplay)으로
  // 이산(code)값의 성김을 세로 점기둥으로 채운다. 점 자체는 canvas 로 그린다(distPaintPoints).
  const pts = {};
  if (info) {
    const srcNames = Object.keys(info.bySource);
    const cap = distCapFor(srcNames.length, DIST.CELL_BUDGET_CARD);
    srcNames.forEach(src => { pts[src] = distDisplayPoints(info.bySource[src], cap); });
  }
  const traces = [];
  const sentinel = distSentinelTrace(pts);
  if (sentinel) traces.push(sentinel);
  // 선택 좌표(Map Analysis)가 있으면 이 항목 위치를 점+빨간 점선으로 오버레이.
  let shapes = distSpecShapes(lo, hi, false).concat(beforeLimitShapes(subject));
  const cm = chipMarkersFor(subject);
  if (cm) { traces.push(...cm.traces); shapes = shapes.concat(cm.shapes); }
  // 단측 스펙 클램프용 데이터 끝값 — ECDF xs 는 오름차순이라 양끝만 보면 된다.
  let gMin = Infinity, gMax = -Infinity;
  if (info && distLimitOnly) Object.keys(info.bySource).forEach(src => {
    const xs = info.bySource[src].xs;
    if (xs && xs.length) {
      if (xs[0] < gMin) gMin = xs[0];
      if (xs[xs.length - 1] > gMax) gMax = xs[xs.length - 1];
    }
  });
  const glr = distLimitRange(lo, hi, gMin, gMax);
  const layout = { ...DIST_PLOT_BG, plot_bgcolor: DIST_STATUS_BG[status] || "#FFFFFF",
    xaxis: { showgrid: true, gridcolor: "#eee", zeroline: false, ticks: "outside",
      tickcolor: "#bbb", tickfont: { size: 9 }, ...(glr ? { range: glr, autorange: false } : {}) },
    yaxis: { range: [0, 100], ticksuffix: "%", showgrid: true, gridcolor: "#eee",
      zeroline: false, tickfont: { size: 9 } },
    shapes, annotations: distSpecAnnos(lo, hi, true),
    margin: { l: 34, r: 10, t: 8, b: 20 }, showlegend: false };
  Plotly.newPlot(plot, traces, layout, DIST_CFG_STATIC);
  distPaintPoints(plot, pts, cm ? cm.traces : null);
  cell.dataset.rendered = "1";
}
function distPurgeGalleryCell(cell) {
  if (cell.dataset.rendered !== "1") return;
  const plot = cell.querySelector(".distg-plot");
  try { if (plot && window.Plotly) { distClearPoints(plot); Plotly.purge(plot); } } catch (e) {}
  cell.dataset.rendered = "";
}

// ── rAF 분할 렌더(프레임당 distPerFrame() 개) ─────────────────────────────────
// 셀 1장의 비용이 소스 수에 비례하므로 프레임당 장수를 소스 수로 조절한다. 총 렌더 시간은
// 같지만 프레임당 초과를 막아 스크롤 끊김이 준다. 실측(40소스·미니셀 150×112px, 칸 예산
// 적용 후): 셀 1장 콜드 11.3ms / 재스크롤 4.4ms → 3장이면 34ms 로 프레임 예산(16.7ms)을
// 넘지만 1장이면 들어온다. 소스가 적으면(<8) 셀이 가벼워 기존 3장 그대로.
function distPerFrame() {
  const n = ((DATA.web_report && DATA.web_report.sources) || []).length;
  if (n >= 16) return 1;
  if (n >= 8) return 2;
  return DIST.PER_FRAME;
}
function distQueueRender(cell) {
  if (cell.dataset.rendered === "1" || distRenderQueue.includes(cell)) return;
  distRenderQueue.push(cell);
  if (!distRafScheduled) { distRafScheduled = true; requestAnimationFrame(distFlushRender); }
}
function distFlushRender() {
  distRafScheduled = false;
  let n = 0;
  const perFrame = distPerFrame();
  while (distRenderQueue.length && n < perFrame) {
    const cell = distRenderQueue.shift();
    if (cell.isConnected && cell.dataset.visible === "1") { distRenderGalleryCell(cell); n++; }
  }
  if (distRenderQueue.length) { distRafScheduled = true; requestAnimationFrame(distFlushRender); }
}

// ── 툴바 + 갤러리 ─────────────────────────────────────────────────────────────
function distToolbarHtml() {
  // cpk<1.33 / Fail Only 독립 토글(둘 다 켜면 교집합). 둘 다 끄면 전체.
  const seg = (on, key, label) => `<button class="distseg${on ? " active" : ""}" data-seg="${key}">${esc(label)}</button>`;
  // 검색 체크박스로 고른 항목이 있으면 개수+해제 버튼을 표시. 세그먼트 그룹 밖(검색창 뒤)에
  // 둔다 — 그룹 안에 있으면 선택 개수에 따라 그룹 폭이 변해 오른쪽 검색창이 좌우로 밀렸다.
  const selChip = distSelected.size
    ? `<button class="distseg dist-sel-clear" data-seg="clearsel" title="선택 해제">선택 ${distSelected.size}개 ✕</button>` : "";
  const bin1Btn = `<button class="distseg${distBin1Only ? " active" : ""}" data-seg="bin1" title="켜짐: 각 항목 분포를 양품(Bin1, BIN==1) & 규격(LSL/USL) 이내 die 측정값만으로 재계산해 표시 · 꺼짐: 전체 die">Bin1 only</button>`;
  const nopfBtn = `<button class="distseg${distHidePassfail ? " active" : ""}" data-seg="nopf" title="켜짐: unit 이 Pass/Fail(P/F·P_F) 인 항목 카드를 숨김 · 꺼짐: 표시">P/F 없애기</button>`;
  return `<div class="dist-toolbar">
    <div class="distseg-group">${seg(distCpkOnly, "cpk", "cpk < 1.33")}${seg(distFailOnly, "fail", "Fail Only")}${seg(distLimitOnly, "limit", "Limit 안 Data만")}${bin1Btn}${nopfBtn}</div>
    <div class="dist-search-wrap" data-no-dirty>
      <input id="distSearch" class="dist-search" type="text" autocomplete="off" placeholder="항목 검색 (체크로 선택)">
      <div id="distSuggest" class="dist-suggest" style="display:none"></div>
    </div>
    ${selChip}
    <span class="dist-count"></span>
  </div>` +
  // 범례는 sticky 툴바 바깥 별도 행 — 소스 40개면 툴바 안에서 여러 줄로 부풀어
  // sticky 헤더가 갤러리 세로 공간을 계속 잡아먹는다(한 번 설정하고 잊는 컨트롤).
  distLegendHtml((DATA.web_report && DATA.web_report.sources) || [], "");
}
function distUpdateCount() {
  const el = document.querySelector("#panel-distribution .dist-count");
  if (el) el.textContent = `${distFiltered.length} 개`;
}
function distRenderGallery() {
  const panel = document.getElementById("panel-distribution");
  distView = "gallery";
  if (distGalleryObserver) { try { distGalleryObserver.disconnect(); } catch (e) {} distGalleryObserver = null; }
  distRenderQueue = []; distRafScheduled = false;
  // 검색 체크박스 선택이 있으면 그 항목들만(세그먼트 무시), 없으면 세그먼트 필터.
  distFiltered = distSelected.size
    ? distIndex.filter(r => distSelected.has(r.subject))
    : distApplySegment(distIndex);
  const cards = distFiltered.map(r => {
    const cpk = r.cpk == null ? "-" : r.cpk;
    const lim = distLimInnerHtml(r.lower_limit, r.upper_limit, r.units);
    // Comment(차트 하단 코멘트)가 있으면 카드에 노트 아이콘 배지. cnSavedFor 는 chart_notes.js(런타임 로드).
    const hasComment = typeof cnSavedFor === "function" && !!(cnSavedFor("cdf:" + r.subject) || {}).comment;
    const noteBadge = hasComment ? `<span class="distg-note" title="Comment 있음">📝</span>` : "";
    return `<div class="distg-card" data-subject="${esc(r.subject)}" data-status="${esc(r.status)}" style="background:${DIST_STATUS_BG[r.status] || "#fff"}">
      <div class="distg-head">
        <div class="distg-line1">
          <span class="distg-tno">${esc(r.test_num || "")}</span>
          <span class="distg-name" title="${esc(r.subject)}">${esc(r.subject)}</span>
          ${noteBadge}
        </div>
        <div class="distg-line2">
          <span class="distg-lim">${lim}</span>
          <span class="distg-cpk">cpk ${esc(cpk)}</span>
        </div>
      </div>
      <div class="distg-plot"></div>
    </div>`;
  }).join("");
  panel.innerHTML = distToolbarHtml() +
    (distFiltered.length ? `<div class="distg-grid">${cards}</div>`
                         : `<div class="placeholder">해당 조건의 항목이 없습니다</div>`);
  distUpdateCount();
  if (typeof Plotly === "undefined" || typeof IntersectionObserver === "undefined") return;
  distGalleryObserver = new IntersectionObserver(entries => {
    entries.forEach(en => {
      const cell = en.target;
      if (en.isIntersecting) { cell.dataset.visible = "1"; distQueueRender(cell); }
      else {
        cell.dataset.visible = "";
        distPurgeGalleryCell(cell);
        const i = distRenderQueue.indexOf(cell);
        if (i >= 0) distRenderQueue.splice(i, 1);
      }
    });
  }, { rootMargin: DIST.ROOT_MARGIN, threshold: 0 });
  panel.querySelectorAll(".distg-card").forEach(c => distGalleryObserver.observe(c));
}


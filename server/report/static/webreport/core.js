const SESSION_ID = location.pathname.split("/").pop();
const YIELD_COLS = ["item_name", "bin_number", "yield_percent", "fail_count",
                    "cpk_val", "mean_val", "stdev_val", "lsl", "usl", "unit"];

// ── 없는 sheet 용 기본 표 골격(frame) ──────────────────────────────────────────
// 업로드 xlsx 에 해당 sheet 가 없으면 헤더만 있는 빈 표(헤더 + 빈 행 1개)를 보여준다.
// 동적 source 열({src}_count/_yield)은 sheet 가 없으면 알 수 없으므로 정적 열만 포함.
// 헤더 값은 생성기 client/report_generator/xlsx_writer.py 레이아웃과 일치.
const YIELD_FRAME_COLS = ["step", "bin", "TNO", "Item", "avg", "comment"];
const ISSUE_FRAME_COLS = ["Step", "Bin", "TNO", "Item", "avg", "Distribution",
                          "PTE comment", "개발 comment"];
const SUMMARY_FRAME_BLOCKS = [
  { label: "1. Device Feature",
    headers: ["DEVICE", "Customer", "PKG_Type", "GrossDie", "Process Line", "EVT_Version"] },
  { label: "2. Yield",
    headers: ["Lot NO", "Yield", "Major Fail Bins", "Comment"] },
  { label: "3. Evaluation Summary",
    headers: ["Category", "Condition & Judge Limit", "Result"] },
];

// 헤더+빈 행 1개용 행 객체 (모든 열 빈 문자열)
function frameRow(cols) { const o = {}; cols.forEach(c => { o[c] = ""; }); return o; }
// summary blocks frame: 블록마다 빈 행 1개
function summaryFrameData() {
  return { blocks: SUMMARY_FRAME_BLOCKS.map(b => ({
    label: b.label, headers: b.headers, rows: [b.headers.map(() => "")] })) };
}

// legacy summary dict 에 표시할 의미있는 내용이 있는지 (title 단독은 제외)
function summaryDictHasContent(st) {
  if (!st || typeof st !== "object") return false;
  return !!((st.feature && Object.keys(st.feature).length) ||
            (Array.isArray(st.yield_summary_text) && st.yield_summary_text.length) ||
            (Array.isArray(st.major_fail_bins) && st.major_fail_bins.length) ||
            (st.evaluation && Object.keys(st.evaluation).length) ||
            (Array.isArray(st.raw_rows) && st.raw_rows.length));
}

// 표 3종이 "정말로 비었을 때"만 frame 구조로 시드 → 보기/수정/저장이 신규 포맷 경로를 탄다.
// 판정 조건은 각 render* 의 emptyPanel fall-through 와 동일.
function seedEmptyFrames() {
  if (!DATA) return;
  const summaryRows = DATA.summary;

  // summary: blocks/grid 아니고 legacy dict 내용도 summary_rows 도 없을 때
  if (!isSummaryBlocks(DATA.summary_text) && !isGrid(DATA.summary_text) &&
      !summaryDictHasContent(DATA.summary_text) &&
      !(Array.isArray(summaryRows) && summaryRows.length)) {
    DATA.summary_text = summaryFrameData();
  }

  // yield: yield_text(list/grid) 미사용 그리고 legacy summary_rows 도 없을 때
  if (!(Array.isArray(DATA.yield_text) && DATA.yield_text.length) &&
      !isGrid(DATA.yield_text) &&
      !(Array.isArray(summaryRows) && summaryRows.length)) {
    DATA.yield_text = [frameRow(YIELD_FRAME_COLS)];
  }

  // issue: issue_table_text(list/grid) 미사용일 때
  if (!(Array.isArray(DATA.issue_table_text) && DATA.issue_table_text.length) &&
      !isGrid(DATA.issue_table_text)) {
    DATA.issue_table_text = [frameRow(ISSUE_FRAME_COLS)];
  }
}

// ── WebGL 가용성 probe (1회 캐시) ──────────────────────────────────────────────
// scattergl(WebGL) trace 는 컨텍스트 생성 실패 시 Plotly 가 SVG 로 자동 폴백하지 않고
// "WebGL is not supported by your browser" 안내만 그린다. --disable-gpu 처방 PC(깜빡임
// 대응 honey_safe_gfx.bat)·원격데스크톱·드라이버 블록리스트 환경이 여기 해당한다.
// scattergl 을 쓰는 쪽(item_detail.js distRenderCdf, trim.js drawTrimChart)은 렌더 전에
// 이 함수로 판정해 SVG scatter 로 내려간다 — 데이터는 동일, trace type 만 갈린다.
let _webglOk = null;
function webglOk() {
  if (_webglOk !== null) return _webglOk;
  try {
    const c = document.createElement("canvas");
    _webglOk = !!(c.getContext("webgl") || c.getContext("experimental-webgl"));
  } catch (e) { _webglOk = false; }
  return _webglOk;
}

// CSRF: 서버가 /view 응답에 내려준 double-submit 쿠키를 읽어 변경요청 헤더로 되돌려준다.
// ── 응답 캐시 개수 상한 (per-item fetch 결과 무한 누적 방지) ──────────────────
// STDF Map·Trim 그룹 차트·Distribution ECDF 는 항목/그룹 단위로 응답을 받아 객체에
// 쌓아두는데, 상한이 없어 오래 볼수록 브라우저 메모리가 단조 증가했다. 삽입 순서를
// 배열로 따로 들고 상한 초과 시 가장 오래된 것부터 버린다(FIFO — 재요청은 저렴하다).
// onEvict(key): 버린 항목에 딸린 별도 상태(요청 완료 표시 등)를 함께 정리할 때 쓴다.
function cachePutCapped(store, order, key, value, max, onEvict) {
  if (!(key in store)) order.push(key);
  store[key] = value;
  while (order.length > max) {
    const old = order.shift();
    if (old === key) continue;         // 방금 넣은 항목은 남긴다
    delete store[old];
    if (onEvict) onEvict(old);
  }
}

// ── 탭 검색어 매칭 (전 탭 공통) ───────────────────────────────────────────────
// 탭 안의 흰 검색칸이 전부 이 두 함수를 쓴다. `%` 를 와일드카드로 지원한다:
//   "BB%ABC" → 문자열 어딘가에 BB 가 나오고 **그 뒤 어딘가에** ABC 가 나오면 매칭
//              (= *BB*ABC*, 사이에 무슨 문자가 몇 개 들어가든 무관).
// `%` 가 없으면 종전 그대로 단순 부분일치라 기존 동작이 바뀌지 않는다.
//
// 정규식으로 만들지 않는 이유: 항목명에 . ( ) [ ] + * 가 흔해 이스케이프 실수 하나로
// 조용히 오매칭이 나고, 조각 순차 indexOf 가 수천 행 필터에서 더 싸다.
//
// searchTerms(q) → 소문자 조각 배열(빈 조각 제거). 빈 검색어면 빈 배열 = "필터 없음".
//   "%ABC"·"ABC%"·"A%%B" 처럼 빈 조각이 생기는 형태는 그 조각만 무시한다.
// searchMatch(text, terms) → 조각들이 **순서대로** 나타나면 true.
function searchTerms(q) {
  return String(q == null ? "" : q).trim().toLowerCase()
    .split("%").map(s => s.trim()).filter(Boolean);
}
function searchMatch(text, terms) {
  if (!terms || !terms.length) return true;
  const s = String(text == null ? "" : text).toLowerCase();
  let from = 0;
  for (const t of terms) {
    const i = s.indexOf(t, from);
    if (i < 0) return false;
    from = i + t.length;
  }
  return true;
}
// 여러 필드 중 **하나라도** 통째로 매칭하면 true (CPK 의 subject|source 처럼).
// ⚠ 필드를 이어붙여 한 문자열로 만들면 안 된다 — "A%B" 가 subject 의 A 와 source 의 B
//    로 갈라져 매칭되어 사용자가 의도하지 않은 행이 남는다.
function searchMatchAny(texts, terms) {
  if (!terms || !terms.length) return true;
  return (texts || []).some(t => searchMatch(t, terms));
}

// ── 콜드 202 자동 재시도 fetch (scatter / STDF 등) ────────────────────────────
// 서버가 202({building:true})를 주면 데이터 준비(tables 웜업·pack 생성)가 백그라운드에서
// 도는 중이다 — 백오프로 재시도해 준비되는 즉시 결과를 돌려준다. 202 외 응답은 기존
// 규약 그대로(!ok → throw, ok → json). opts:
//   onWaiting(elapsedMs) : 202 를 받을 때마다 호출 — 호출부가 대기 UI 갱신.
//   shouldStop()         : true 면 재시도 중단(reject) — 항목 이동 등으로 결과가 불필요해진 경우.
//   timeoutMs            : 총 대기 상한 (기본 3분) — 초과 시 reject.
const FETCH202 = { DELAYS_MS: [2000, 3000, 5000, 8000, 10000], TIMEOUT_MS: 3 * 60 * 1000 };
function fetchJson202(url, opts) {
  opts = opts || {};
  const t0 = Date.now();
  const timeoutMs = opts.timeoutMs || FETCH202.TIMEOUT_MS;
  return new Promise((resolve, reject) => {
    let attempt = 0;
    function go() {
      if (opts.shouldStop && opts.shouldStop()) { reject(new Error("중단됨")); return; }
      fetch(url, opts.fetchOpts || undefined).then(r => {
        if (r.status === 202) {
          const elapsed = Date.now() - t0;
          if (elapsed > timeoutMs) { reject(new Error("데이터 준비 시간 초과")); return; }
          if (opts.onWaiting) { try { opts.onWaiting(elapsed); } catch (e) {} }
          const delay = FETCH202.DELAYS_MS[Math.min(attempt, FETCH202.DELAYS_MS.length - 1)];
          attempt++;
          setTimeout(go, delay);
          return;
        }
        if (!r.ok) { reject(new Error("HTTP " + r.status)); return; }
        r.json().then(resolve, reject);
      }, reject);
    }
    go();
  });
}

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)report_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

let DATA = null;
function isWebReportSession() {
  return !!(DATA && DATA.session && DATA.session.source === "web_report");
}
// 세션 분석 모드(Normal/Compare/DUT/Commonality). 세션 DB 컬럼이 authoritative.
function webReportMode() {
  return (((DATA && DATA.session && DATA.session.mode) || "Normal") + "").trim();
}
// Temperature 모드 source 역할 — "rt"(그룹 기준) / "member"(CT·HT) / ""(그 외·타 모드).
// payload sources[] 의 temp_role 이 정본 (서버 metrics.build_report_payload).
function tempRoleOf(name) {
  const sources = (DATA && DATA.web_report && DATA.web_report.sources) || [];
  const hit = sources.find(s => String(s.name) === String(name));
  return (hit && hit.temp_role) || "";
}
// RT source 뒤에 붙일 작은 표식 — "이 source 의 limit 이 그룹 판정 기준" 임을 알린다.
function tempRoleTag(name) {
  return tempRoleOf(name) === "rt"
    ? ` <span class="temp-rt" title="이 그룹(RT/CT/HT)의 Limit 기준 source">RT</span>` : "";
}
// 요약 박스의 소스별 표 정렬 — 기본은 yield% 내림차순(수율 낮은 소스 찾기)이지만,
// DUT 모드는 DUT 번호 오름차순(DUT 1,2,…,10)으로 고정한다. 백엔드 split_table_by_dut 가
// 이미 그 순서로 source 를 만들므로 정렬하지 않고 payload 순서를 그대로 쓴다.
function orderSummarySources(bySource) {
  const rows = Array.isArray(bySource) ? bySource.slice() : [];
  if (webReportMode() === "DUT") return rows;
  return rows.sort((a, b) => (Number(b.yield_pct) || 0) - (Number(a.yield_pct) || 0));
}
// 편집 권한: 로그인 ID 가 세션 업로더와 같을 때만 "edit" (업로더 기록 없는 legacy 는
// 로그인만 하면 허용). 그 외에는 "view" — 흩어진 MODE==="edit" 분기가 읽기전용으로 렌더.
// 로그인은 검색결과 페이지에서 수행하고, 서버가 편집 라우트에서 같은 규칙으로 재검증한다.
let MODE = "view";
let LOGIN_USER = "";            // 현재 사용자 (Honey UA 또는 웹 로그인, 없으면 "")
let IDENTITY_SRC = "";          // "honey"|"login"|"sso"|"" — Honey 전용 기능 안내 판단용
let CAN_EDIT = false;           // 이 세션 편집 가능(업로더 또는 위임 편집자) — 서버 판정
let IS_UPLOADER = false;        // 이 세션 업로더 본인(또는 master PC) — 권한부여/비공개/삭제용
let IS_MASTER = false;          // admin 로그인 PC 인가 — IS_UPLOADER 와 별도로 둔다(챗봇 등 master 전용 UI)
let MY_IMPORTANT = false;       // 내 개인 중요표시 상태(사용자별)
let DISPLAY_NAME = "";          // 내 실명 — 비어 있으면 이름 입력창을 띄운다
let UPLOADER_NAME = "";         // 세션 업로더의 실명 (상단바 '이름(ID)' 표기용)
let verifiedPassword = "";      // (구 PIN 흐름 잔재 — 저장 payload 호환용, 항상 "")

// 세션별 권한·개인상태는 서버가 요청자(User-Agent) 기준으로 판정해 내려준다.
// (session_full 은 세션 단위 gzip 캐시라 사용자별 값을 섞으면 안 되므로 별도 경량 엔드포인트)
async function loadAuth() {
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/my_access`);
    if (res.ok) {
      const j = await res.json();
      LOGIN_USER = j.user_id || "";
      IDENTITY_SRC = j.source || "";
      CAN_EDIT = !!j.can_edit;
      // master PC(admin 로그인 4h)는 업로더와 동일 권한 — 삭제/비공개/권한부여 UI 도 연다
      // (서버 _uploader_guard 가 같은 규칙으로 재검증한다).
      IS_UPLOADER = !!(j.is_uploader || j.is_master);
      IS_MASTER = !!j.is_master;
      MY_IMPORTANT = !!j.my_important;
      DISPLAY_NAME = j.display_name || "";
      UPLOADER_NAME = j.uploader_name || "";
      // 챗봇은 master 전용(테스트 단계) — 서버 라우트도 같은 규칙으로 404 를 낸다.
      if (IS_MASTER && window.ChatWidget) ChatWidget.enable(SESSION_ID);
      // 세션 링크로 곧장 들어온 사람도 이름을 받는다 (홈을 거치지 않는 경로).
      try {
        UserName.promptIfMissing(j, {onSaved: (name) => { DISPLAY_NAME = name; }});
      } catch (e) { /* 이름 수집은 부가기능 — 실패해도 화면은 그대로 */ }
    } else {
      console.warn("my_access 조회 실패 — 읽기 전용으로 표시 (HTTP " + res.status + ")");
    }
  } catch (e) {
    console.warn("my_access 조회 실패 — 읽기 전용으로 표시", e);
    LOGIN_USER = ""; IDENTITY_SRC = ""; CAN_EDIT = false; IS_UPLOADER = false;
    IS_MASTER = false; MY_IMPORTANT = false; DISPLAY_NAME = ""; UPLOADER_NAME = "";
  }
  // Honey 밖에서만 '전용 기능' 안내 버튼을 툴바에 붙인다 (honey_hint.js).
  try { HoneyHint.init(IDENTITY_SRC, ".topbar", "theme-toggle", "🍯"); } catch (e) { /* 안내는 부가기능 */ }
}

function canEditSession() {
  return CAN_EDIT;
}
let yieldGrid = null;           // 수정 모드 Yield 편집 Tabulator 인스턴스 (없으면 null)


// & < > " ' 모두 이스케이프 → 텍스트/속성 양쪽에서 안전
function esc(v) {
  return String(v ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// stdev 표시 전용 포맷 — 소수 3자리가 기본, |v|<1 이라 3자리로는 유효숫자가 모자라면
// 유효숫자 3자리가 나올 때까지 소수 자리수를 늘린다.
//   0.33234623463246 → 0.332 / 1235213.3331212 → 1235213.333 / 0.00000023422 → 0.000000234
// toPrecision 은 1e-7 미만에서 지수표기(2.34e-7)로 새기 때문에 자리수를 직접 계산해 toFixed 를 쓴다.
// **표시 전용**이다 — 원값(row.stdev)은 CPK Limit 역산(cpk.js cpkComputeTargets)과
// Item_detail 가우시안 곡선이 그대로 쓰므로 데이터를 반올림해서는 안 된다.
// ⚠ 2026-08-26 부터 호출자가 없다 — 통계값 표시는 전부 아래 fmtLen8 로 옮겼다(자리수 통일).
function fmtStdev(v) {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return String(v ?? "");
  const a = Math.abs(n);
  // 2 - floor(log10|v|) = 유효숫자 3자리에 필요한 소수 자리수. toFixed 상한(100)으로 캡.
  const digits = (a > 0 && a < 1) ? Math.min(100, 2 - Math.floor(Math.log10(a))) : 3;
  return n.toFixed(digits);
}

// CPK/Compare/Item_detail/Issue Table Compare 통계값(min·median·max·average·stdev)
// 표시 전용 — 소수 최대 4자리 반올림 + 전체 8자(부호 제외) 이내로 줄여 컬럼 폭을 잡는다.
// 서버가 컬럼마다 다른 자리수로 내려보내(min~max 6자리 / average 4자리 / stdev 무반올림)
// 값 하나가 컬럼 전체를 밀어내던 문제를 표시 단에서 통일한다.
//   원문이 8자 이하면 손대지 않는다   99999.5 → 99999.5 / 1234.567 → 1234.567
//   소수 최대 4자리 반올림           1.234565473457 → 1.2346 / 33.235235 → 33.2352
//   정수부가 길면 소수를 더 줄인다    234626.2346234 → 234626.2 (전체 8자 상한)
//   **자리올림이 나면 그 자리에서 버림** 999999.99 → 999999.9 (1000000 이 되면 앞자리가
//     통째로 바뀌어 다른 값처럼 보인다 — 사용자 확정 2026-08-26)
//   유효숫자 2자리 미만으로 뭉개지면 지수  0.00034345 → 3.4345e-4 (소수4 로는 0.0003)
//     단 원래 짧은 값은 그대로 둔다      0.0003 → 0.0003 (자른 게 아니므로)
//   지수표기는 e12 / e-6 형태 (e+12 의 + 제거)
// **표시 전용**이다 — 원값은 절대 덮어쓰지 않는다. CPK Limit 역산(cpk.js cpkComputeTargets)과
// Item_detail 가우시안 곡선이 원값을 그대로 쓴다.
const LEN8_MAX = 8;       // 전체 표시 최대 길이(부호 제외)
const LEN8_MAX_DEC = 4;   // 소수 최대 자리수
const LEN8_MIN_SIG = 2;   // 일반표기 유효숫자가 이 미만으로 뭉개지면 지수표기

function _len8StripPlain(s) {            // 소수부 끝자리 0 제거 ("3.5000"→"3.5")
  return s.indexOf(".") < 0 ? s : s.replace(/\.?0+$/, "");
}
function _len8Sig(s) {                   // 유효숫자 개수(가수부 기준)
  const b = s.split("e")[0].replace("-", "").replace(".", "").replace(/^0+/, "");
  return b.replace(/0+$/, "").length;
}
function _len8Body(s) { return s.replace("-", "").length; }   // 부호 제외 길이

// 지수표기 — 가수부만 끝자리 0 을 떼고 "e+12"→"e12".
// ⚠ 끝자리 0 제거를 문자열 전체에 걸면 "1.0000e+1" 의 지수부까지 먹어 "1.00000e" 라는
//   깨진 값이 되므로 가수부/지수부를 분리해 처리한다.
function _len8Exp(n, d) {
  const s = n.toExponential(d);
  const i = s.indexOf("e");
  return _len8StripPlain(s.slice(0, i)) + s.slice(i).replace("e+", "e");
}
// 소수 d자리 버림 — 반올림이 자리올림을 일으켰을 때만 쓴다.
// ⚠ 곱셈(Math.floor(n*10**d)/10**d)으로 구현하면 부동소수점 오차로 틀린다
//   (8.7*10 = 86.99999999999999 → floor → 8.6). toFixed 로 넉넉히 뽑아 문자열에서 자른다.
function _len8Trunc(n, d) {
  const neg = n < 0;
  const parts = Math.abs(n).toFixed(Math.min(100, d + 5)).split(".");
  const fp = ((parts[1] || "") + "0".repeat(d)).slice(0, d);
  return (neg ? "-" : "") + (d > 0 ? parts[0] + "." + fp : parts[0]);
}

function fmtLen8(v) {
  if (v === null || v === undefined || v === "") return "";   // Number(null)=0 오인 방지
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (n === 0) return "0";                                    // log10(0) = -Infinity
  const raw = String(v);
  if (_len8Body(raw) <= LEN8_MAX) return raw;                 // 짧으면 손대지 않는다

  const e = Math.floor(Math.log10(Math.abs(n)));
  // 소수 자리수: 최대 4자리, 단 정수부가 길면 전체 8자에 맞춰 줄인다.
  const dec = Math.max(0, Math.min(LEN8_MAX_DEC, LEN8_MAX - Math.max(1, e + 1) - 1));
  let plain = _len8StripPlain(n.toFixed(dec));
  // 자리올림(정수부가 커짐)이면 그 자리에서 버림으로 대체 — 앞자리가 바뀌지 않게.
  if (Math.floor(Math.abs(Number(plain))) > Math.floor(Math.abs(n)))
    plain = _len8StripPlain(_len8Trunc(n, dec));
  // 그래도 8자를 넘으면 한 자리 더 줄인다(방어 — 버림 대체 뒤엔 사실상 도달하지 않는다).
  if (_len8Body(plain) > LEN8_MAX && dec > 0) plain = _len8StripPlain(n.toFixed(dec - 1));

  // 지수표기 후보 — 지수부(e-10·e308)가 길이를 먹으므로 전체 길이로 판정해야 한다.
  //   가수부만 세면 12345678901 이 "1.2346e1"(=12.346) 이 되어 값이 통째로 틀어진다.
  let exp = null;
  for (let d = 5; d >= 0; d--) {
    const ex = _len8Exp(n, d);
    if (_len8Body(ex) <= LEN8_MAX) { exp = ex; break; }
  }
  if (exp === null) exp = _len8Exp(n, 0);       // 지수부가 길면(e308) 8자 초과를 허용

  if (Number(plain) === 0) return exp;          // 소수 4자리로 값이 사라짐
  if (_len8Body(plain) > LEN8_MAX) return exp;  // 정수부만으로 8자 초과
  // 잘려서 유효숫자를 잃었을 때만 지수로 (원래 짧은 0.0003 은 exp 도 유효숫자 1 이라 유지된다).
  return (_len8Sig(plain) < LEN8_MIN_SIG && _len8Sig(exp) > _len8Sig(plain)) ? exp : plain;
}

function fmtDate(unix) {
  if (!unix) return "-";
  const d = new Date(unix * 1000);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function showToast(msg, ms=2200) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), ms);
}

function splitLines(v) {
  const a = (v || "").split("\n");
  while (a.length && a[a.length - 1].trim() === "") a.pop();
  return a;
}

// ── 라이트/다크 테마 토글 (head 부트스트랩이 저장값을 먼저 적용해 둔 상태) ──
(function () {
  const btn = document.getElementById("themeToggle");
  if (!btn) return;
  const root = document.documentElement;
  function syncThemeBtn() {
    const dark = root.getAttribute("data-theme") === "dark";
    btn.textContent = dark ? "☀️" : "🌙";
    btn.title = dark ? "라이트 모드로 전환" : "다크 모드로 전환";
  }
  btn.addEventListener("click", () => {
    const toDark = root.getAttribute("data-theme") !== "dark";
    if (toDark) root.setAttribute("data-theme", "dark");
    else root.removeAttribute("data-theme");
    try {
      if (toDark) localStorage.setItem("report_theme", "dark");
      else localStorage.removeItem("report_theme");
    } catch (e) {}
    syncThemeBtn();
  });
  syncThemeBtn();
})();

// ── 화면 설정(글꼴) 팝오버 — head 부트스트랩이 저장값을 먼저 적용해 둔 상태 ──
const UI_FONTS = {
  default: "",   // 빈 값 → CSS 기본 스택(--report-font 미설정과 동일 효과)
  malgun: '"Malgun Gothic", sans-serif',
  noto: '"Noto Sans KR", "Malgun Gothic", sans-serif',
  system: 'system-ui, -apple-system, "Segoe UI", sans-serif',
};
// 화면 배율(글자 크기). font-size 가 아니라 zoom 인 이유 — 이 페이지의 font-size 는 전부
// 절대 px 이고(루트 크기 없음), 열 폭(sheets.js colWidth)·카드 높이·Plotly 글자 크기가 모두
// 그 px 에 맞춰 손튜닝돼 있다. 글자만 키우면 잘리거나 열이 넘치지만, zoom 은 전부 같은
// 비율로 확대해 상대 레이아웃을 보존한다. 키는 "100"=기본(스타일 미설정).
const UI_ZOOMS = { "100": "", "110": "1.1", "125": "1.25", "150": "1.5" };
(function () {
  const toggle = document.getElementById("settingsToggle");
  const panel = document.getElementById("settingsPanel");
  if (!toggle || !panel) return;
  const root = document.documentElement;
  let curFont = (() => { try { return localStorage.getItem("report_ui_font") || "default"; } catch (e) { return "default"; } })();
  let curZoom = (() => { try { return localStorage.getItem("report_ui_zoom") || "100"; } catch (e) { return "100"; } })();

  function applyFont(key) {
    curFont = UI_FONTS[key] != null ? key : "default";
    const stack = UI_FONTS[curFont];
    if (!stack) root.style.removeProperty("--report-font");
    else root.style.setProperty("--report-font", stack);
    try { localStorage.setItem("report_ui_font", curFont); } catch (e) {}
    syncActive();
  }
  function applyZoom(key) {
    curZoom = UI_ZOOMS[key] != null ? key : "100";
    const z = UI_ZOOMS[curZoom];
    // zoom 은 100vh 도 배율만큼 곱해버려(실측 확인) 뷰포트 기준 높이 계산이 화면을 넘친다.
    // --vph 를 배율로 나눠 넣어 CSS 의 calc(var(--vph) - ...) 들이 다시 뷰포트에 맞게 한다.
    if (!z) { root.style.removeProperty("zoom"); root.style.removeProperty("--vph"); }
    else { root.style.zoom = z; root.style.setProperty("--vph", `calc(100vh / ${z})`); }
    try { localStorage.setItem("report_ui_zoom", curZoom); } catch (e) {}
    syncActive();
    // 실측 기반 레이아웃(--sticky-head-h, Issue Table 고정열 left, Plotly responsive)은 전부
    // window.resize 에만 걸려 있는데 zoom 변경은 resize 를 발생시키지 않는다. 리플로우가
    // 끝난 다음 프레임에 1회 쏴서 재동기화시킨다.
    requestAnimationFrame(() => {
      window.dispatchEvent(new Event("resize"));
      // Note 탭 Luckysheet 는 iframe 격리라 부모 resize 를 못 받는다 — 직접 알린다.
      if (typeof noteOnTabShown === "function") noteOnTabShown();
    });
  }
  function syncActive() {
    panel.querySelectorAll("#uiFontSeg button").forEach(b =>
      b.classList.toggle("active", b.dataset.font === curFont));
    panel.querySelectorAll("#uiZoomSeg button").forEach(b =>
      b.classList.toggle("active", b.dataset.zoom === curZoom));
  }
  toggle.addEventListener("click", (e) => { e.stopPropagation(); panel.classList.toggle("open"); });
  panel.addEventListener("click", (e) => {
    e.stopPropagation();
    const ft = e.target.closest("#uiFontSeg button");
    if (ft) { applyFont(ft.dataset.font); return; }
    const zm = e.target.closest("#uiZoomSeg button");
    if (zm) { applyZoom(zm.dataset.zoom); return; }
    const stab = e.target.closest(".settings-tab");
    if (stab) { switchSettingsTab(stab.dataset.stab); return; }
    const grant = e.target.closest("button[data-grant]");
    if (grant && !grant.disabled) { grantEditor(grant.dataset.grant); return; }
    const revoke = e.target.closest("button[data-revoke]");
    if (revoke) { revokeEditor(revoke.dataset.revoke); return; }
  });
  // 팝오버 밖 클릭 시 닫기.
  document.addEventListener("click", (e) => {
    if (panel.classList.contains("open") && !panel.contains(e.target) && e.target !== toggle)
      panel.classList.remove("open");
  });

  // ── 권한 탭 (업로더만 노출) — 편집 권한 위임 부여/회수 ──
  let _permLoaded = false;
  function switchSettingsTab(name) {
    panel.querySelectorAll(".settings-tab").forEach(b =>
      b.classList.toggle("active", b.dataset.stab === name));
    panel.querySelectorAll(".settings-body").forEach(b =>
      b.style.display = b.dataset.sbody === name ? "" : "none");
    if (name === "perm" && !_permLoaded) {
      _permLoaded = true;
      loadEditorsList();
      searchCandidates("");
    }
  }
  // 사용자 표기는 전부 '이름(ID)' — 이름이 아직 없는 사람은 ID 만 나온다(UserName.fmt).
  function renderEditorsList(editors, names) {
    const box = document.getElementById("permCurrent");
    if (!box) return;
    if (!editors.length) { box.innerHTML = `<div class="perm-empty">아직 편집 권한을 준 사용자가 없습니다.</div>`; return; }
    box.innerHTML = editors.map(ed => {
      const who = UserName.fromMap(ed.editor_user, names);
      return `
      <div class="perm-item">
        <span class="perm-user" title="${esc(who)}">${esc(who)}</span>
        <button class="revoke" data-revoke="${esc(ed.editor_user)}">회수</button>
      </div>`;
    }).join("");
  }
  async function loadEditorsList() {
    const box = document.getElementById("permCurrent");
    if (!box) return;
    try {
      const res = await fetch(`/pe/report/session/${SESSION_ID}/editors`);
      if (!res.ok) { box.innerHTML = `<div class="perm-empty">불러오기 실패</div>`; return; }
      const j = await res.json();
      renderEditorsList(j.editors || [], j.names || {});
    } catch (e) { box.innerHTML = `<div class="perm-empty">불러오기 실패</div>`; }
  }
  async function searchCandidates(q) {
    const box = document.getElementById("permCandidates");
    if (!box) return;
    try {
      const res = await fetch(`/pe/report/session/${SESSION_ID}/editors/candidates?q=${encodeURIComponent(q)}`);
      if (!res.ok) { box.innerHTML = `<div class="perm-empty">검색 실패</div>`; return; }
      const list = (await res.json()).candidates || [];
      if (!list.length) { box.innerHTML = `<div class="perm-empty">해당 사용자가 없습니다 (web_report 방문 기록 · 웹 가입 계정 기준).</div>`; return; }
      // 검색어는 서버에서 ID·이름 둘 다에 매칭된다 — 이름으로 사람을 찾을 수 있다.
      box.innerHTML = list.map(c => {
        const who = UserName.fmt(c.user, c.name);
        return `
        <div class="perm-item">
          <span class="perm-user" title="${esc(who)}">${esc(who)}</span>
          <button data-grant="${esc(c.user)}"${c.already ? " disabled" : ""}>${c.already ? "부여됨" : "부여"}</button>
        </div>`;
      }).join("");
    } catch (e) { box.innerHTML = `<div class="perm-empty">검색 실패</div>`; }
  }
  async function grantEditor(user) {
    try {
      const res = await fetch(`/pe/report/session/${SESSION_ID}/editors`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({ user }),
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) { showToast(j.error || "권한 부여 실패"); return; }
      renderEditorsList(j.editors || [], j.names || {});
      searchCandidates((document.getElementById("permSearch") || {}).value || "");
      showToast(`${UserName.fromMap(user, j.names)} 에게 편집 권한을 주었습니다.`);
    } catch (e) { showToast("권한 부여 실패: " + e.message); }
  }
  async function revokeEditor(user) {
    try {
      const res = await fetch(`/pe/report/session/${SESSION_ID}/editors/${encodeURIComponent(user)}`, {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrfToken() },
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) { showToast(j.error || "권한 회수 실패"); return; }
      renderEditorsList(j.editors || [], j.names || {});
      searchCandidates((document.getElementById("permSearch") || {}).value || "");
      showToast(`${user} 의 편집 권한을 회수했습니다.`);   // 회수 후엔 이름 맵에서 빠진다
    } catch (e) { showToast("권한 회수 실패: " + e.message); }
  }
  const permSearch = document.getElementById("permSearch");
  if (permSearch) {
    let _searchTimer = null;
    permSearch.addEventListener("input", () => {
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => searchCandidates(permSearch.value || ""), 250);
    });
  }

  syncActive();
})();


// ── Issue Table 패널 공용 헬퍼 (Issue Table / Issue Table Temp / Issue Table Compare) ──
// 2026-08-05 Temperature 개편으로 Issue 표 패널이 2개가 됐고, 2026-08-20 Compare 개편으로
// 3개가 됐다. 편집·검색·미니셀 로직은 세 패널이 공유하므로 "패널을 인자로 받되 기본값은
// 기존 #panel-issues" 규약으로 일반화한다 (인자를 안 주면 동작이 종전과 같다).
// 툴바 버튼은 고정 id 대신 data-issue-act 로 식별한다
// — 같은 id 가 여러 패널에 생기면 document.getElementById 가 엉뚱한 패널을 잡는다.
const ISSUE_PANEL_MAIN = "panel-issues";
const ISSUE_PANEL_TEMP = "panel-issue-temp";
// ⚠ Compare 만 **탭 패널이 아니라 그 안의 서브패널**이다 (2026-08-27 Compare 탭 흡수).
// 탭 패널 #panel-issue-cmp 는 서브탭 바 + 서브패널 5개를 담고, 이슈 표는 그 중 ISSUE_TABLE
// 서브패널(#panel-issue-cmp-table)에만 들어간다 — renderIssueTableInto 가 대상 div 의
// innerHTML 을 통째로 갈아치우기 때문에 서브탭 바가 그 밖에 있어야 살아남는다.
// (Characteristic 이 #panel-trim-analysis 를 첫 서브패널에 둔 것과 같은 관례.)
const ISSUE_PANEL_CMP = "panel-issue-cmp-table";
const ISSUE_PANEL_SEL = "#panel-issues, #panel-issue-temp, #panel-issue-cmp-table";
// Temp 패널이 읽는 시트 이름 — 백엔드 tabs/temp_fail.TEMP_SHEET 와 같아야 한다.
const ISSUE_TEMP_SHEET = "Issue Table Temp";
// Compare 패널이 읽는 시트 이름 — 백엔드 metrics.py 의 주입 키와 같아야 한다.
const ISSUE_CMP_SHEET = "Issue Table Compare";

// Bin 그룹(▼ 토글이 달린 행)의 Yield 가 그 Bin 의 여러 Item 을 합친 값이라는 안내
// (사용자 요청 2026-08-26). Yield 탭 STEP 제목과 Issue Table Item 헤더가 같은 문구를
// 쓰므로 여기서 한 번만 정의한다 — 문구를 고칠 땐 이 상수만 고치면 두 곳이 함께 바뀐다.
const MERGE_NOTE_TEXT =
  "(※ STEP 에 화살표 버튼이 있는 Bin 의 Yield 는 동일 Bin 의 여러 Item 들이 Merge 된 Yield 입니다.)";

// Temperature 탭 판정 기준 안내 (2026-08-27). 이 표가 "CT/HT 측정값을 RT source 의
// Limit 으로 다시 판정한 결과" 라는 사실이 지금까지 source 이름 옆 RT 배지 tooltip
// 한 줄에만 있었다 — 그 규칙을 모르면 왜 CT/HT 가 fail 로 잡히는지 알 수 없다.
const TEMP_JUDGE_NOTE_TEXT =
  "(※ 이 표는 CT / HT 측정값을 RT source 의 Limit 기준으로 다시 판정한 결과입니다. " +
  "RT 표식이 붙은 source 가 그 기준입니다.)";

// 현재 DOM 에 존재하는 Issue 표 패널들 (Temp/Compare 는 해당 모드 세션에만 내용이 있다).
function issuePanelEls() {
  return [...document.querySelectorAll(ISSUE_PANEL_SEL)];
}
// 어떤 요소가 속한 Issue 표 패널 (아니면 null).
function issuePanelOf(el) {
  return (el && el.closest) ? el.closest(ISSUE_PANEL_SEL) : null;
}
// 전 패널을 통틀어 선택자 매칭 (구 '#panel-issues .xxx' 전역 질의의 대체).
function issuePanelsQueryAll(sel) {
  const out = [];
  issuePanelEls().forEach(p => p.querySelectorAll(sel).forEach(el => out.push(el)));
  return out;
}
// 인자 없는 호출의 기본 패널 — 활성 탭이 Issue 계열이면 그것, 아니면 기본 패널.
// Compare 는 서브패널이라 .active 가 .cmp-subpanel.active 와 같은 클래스다 —
// **ISSUE_TABLE 서브탭이 켜져 있을 때만** 잡힌다(다른 서브탭에선 이슈 툴바 자체가
// 안 보이므로 반환하지 않는 게 맞다).
function activeIssuePanel() {
  return document.querySelector("#panel-issues.active, #panel-issue-temp.active, " +
                                "#panel-issue-cmp-table.active")
    || document.getElementById(ISSUE_PANEL_MAIN);
}
// 그 패널이 그린 rows 배열 (저장 diff·낙관 반영의 기준 데이터).
function issueRowsOf(panel) {
  const sheetOf = name => {
    const rows = (webReportSheets() || {})[name];
    return Array.isArray(rows) ? rows : [];
  };
  if (panel && panel.id === ISSUE_PANEL_TEMP) return sheetOf(ISSUE_TEMP_SHEET);
  if (panel && panel.id === ISSUE_PANEL_CMP) return sheetOf(ISSUE_CMP_SHEET);
  return Array.isArray(DATA && DATA.issue_table_text) ? DATA.issue_table_text : [];
}
// 패널별 UI 상태(검색어·선택 모드) — 전역 1개면 두 패널이 서로의 상태를 덮어쓴다.
const issueUiState = {};
function issueUi(panel) {
  const id = (panel && panel.id) || ISSUE_PANEL_MAIN;
  // hideClose/hideOpen: Status 별 행 숨김(액션 메뉴 — yield_issue.applyIssueStatusFilter).
  // 검색과 독립된 별도 클래스(.row-status-hide)라 둘을 동시에 걸 수 있다.
  // statsFold: Compare 패널 전용 — before/after 원시 통계 6개 + meanshift_σ 접기
  // (툴바 '통계 접기'). △σ%·cpk% 는 접기 대상이 아니라 항상 보인다(sheets.CMP_FOLD_COL_RE).
  // **Compare 패널만 기본 접힘**이다(2026-08-27 사용자 요청) — 펼치면 가로폭이 너무 넓어
  // 정작 먼저 봐야 할 △σ%·cpk% 가 화면 밖으로 밀린다. 화면 표시 상태일 뿐이라 저장하지
  // 않는다(새로고침하면 이 기본값으로 돌아간다).
  if (!issueUiState[id]) issueUiState[id] = { search: "", delMode: false,
                                              hideClose: false, hideOpen: false,
                                              statsFold: (id === ISSUE_PANEL_CMP) };
  return issueUiState[id];
}

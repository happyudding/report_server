// ── CPK ──────────────────────────────────────────────────────────────────
const CPK_COLUMNS = ["subject", "lower_limit", "upper_limit", "units", "source",
  "n", "min", "median", "max", "average", "stdev", "cpl", "cpu", "cp", "cpk"];
const CPK_NUMERIC = new Set(["lower_limit", "upper_limit", "n", "min", "median", "max",
  "average", "stdev", "cpl", "cpu", "cp", "cpk"]);
// 통계 컬럼만 표시 길이를 8자로 제한한다(core.js fmtLen8) — cpl/cpu/cp/cpk 는 서버가
// 이미 3자리로 반올림해 내려보내고, limit 은 규격값이라 원문 그대로 보여준다
// (자리수 규약은 web_report/tabs/cpk.py _stats_batch).
const CPK_LEN8_COLS = new Set(["min", "median", "max", "average", "stdev"]);
const CPK_WARN_THRESHOLD = 1.33;   // 기본 임계값 (item_detail/Issue Table 하이라이트는 이 고정값 사용)
const CPK_PAGE_SIZE = 100;    // 페이지당 표시 행 수

// 기준(basis)은 **Bin1(양품, BIN==1) 하나로 통일**돼 있다 (2026-07-23, UX 간편화).
// 종전 3상 토글(전체/Bin1/Limit 안)은 제거했다 — 서버 cpk_rows 의 base 필드가 곧 Bin1
// 통계이며(web_report/tabs/cpk.py), Issue Table·Distribution·Excel 도 같은 값을 쓴다.
// 버튼 라벨은 전부 "현재 적용 중인 값"만 쓴다(누르면 바뀔 값이 아님) — 툴바 왼쪽 라벨이
// 무슨 구분인지 설명한다.
let cpkShowLowOnly = true;    // 기본값: 임계 미만 항목만 항목명 순으로 정렬해 보여줌
// CPK 임계값 — 사용자가 툴바에서 직접 입력한다(기본 1.33). 필터·셀 강조·빈 메시지가 이 값을 쓴다.
// cpkLowInputRaw 는 입력 중 원문(빈 문자열·"1." 같은 중간 상태 허용), cpkLowThreshold 는
// 마지막으로 유효했던 숫자 — 입력이 잠깐 비어도 필터가 튀지 않게 분리해 둔다.
let cpkLowThreshold = CPK_WARN_THRESHOLD;
let cpkLowInputRaw = String(CPK_WARN_THRESHOLD);
// 임계값 비교 방향 — "lt"=미만(기본, 종전 동작) / "gt"=초과. 필터·셀 강조·빈 메시지 공용.
let cpkLowOp = "lt";
// "동일Limit" 3상: "exclude"=제외(기본)·"all"=전체·"only"=그 항목만.
// 판정 기준은 cpkIsAbnormal 참조(상·하한 동일 또는 CPK 계산 불가) — 화면 라벨만 "동일Limit".
let cpkAbnormalMode = "exclude";
let cpkHideCodeUnit = false;  // 켜면 Unit(단위)이 CODE 인 항목(디지털 code 값) 숨김
let cpkSearchTerm = "";       // subject/source 검색어 (실시간 필터)
// 표시할 source 집합 — **빈 Set = 전체 source**(선택 없음 = 필터 없음). 여러 개를 함께
// 볼 수 있어야 해서 **다중 선택 드롭다운**(cpkSourceMenuHtml)으로 고른다 — 종전 토글 칩
// 바는 source 가 많으면 툴바 아래 한 줄을 통째로 먹고 이름이 길면 표를 밀어냈다.
let cpkSourceFilter = new Set();
// 전 source rawdata 를 하나로 통합한 가상 source. 서버 tabs/cpk.py TOTAL_SOURCE 와
// **짝으로 고쳐야 하는 이중 정의**다 (CLAUDE.md 규칙 15).
const CPK_TOTAL_SOURCE = "TOTAL";
// TOTAL 행 표시 여부 — cpkSourceFilter(Set) 와 **분리한다**. Set 에 특수값으로 넣으면
// ① source 이름이 실제로 "TOTAL" 인 세션과 충돌하고 ② stale 정리(아래 renderCpk)가 매
// 렌더마다 지워 드롭다운을 눌러도 즉시 꺼지며 ③ `.size` 를 "개별 필터 있음" 으로 쓰는
// 3곳의 뜻이 흐려진다.
let cpkShowTotal = false;
let cpkPage = 1;              // 현재 페이지 (1-base)
let cpkPanelBound = false;    // panel-cpk 페이저 클릭 위임 1회 바인딩 플래그
let cpkTargetMode = false;    // "Limit 계산" 토글 — 켜지면 체크박스 열 + 목표 Cpk 역산 컨트롤 노출
let cpkSelected = new Set();  // 선택된 행 키 (`${subject}||${source}`)
let cpkTargetVal = 1.33;      // 목표 Cpk 입력값
let cpkMarginPct = 0;         // 역산 Margin(%) — 0/1/5/10. 각 한계값을 (1-margin) 으로 나눠 확대
let cpkTargetResults = new Map(); // 역산 결과: 행 키 → {lo, hi}

function cpkRowKey(r) { return `${r.subject}||${r.source}`; }

// 동일Limit 3상 토글: 버튼 라벨 + 클릭 시 순환 순서(제외(기본) → 전체 → 그 항목만 → …).
const CPK_ABN_LABELS = { exclude: "동일Limit 제외", all: "ALL", only: "동일Limit only" };
const CPK_ABN_ORDER = ["exclude", "all", "only"];

function cpkFmt(x) { return Number(x.toFixed(4)); }

// 비교 연산자 표기(HTML 이스케이프) / 판정 — 필터·강조·라벨이 같은 함수를 쓴다.
function cpkOpSign() { return cpkLowOp === "gt" ? "&gt;" : "&lt;"; }
function cpkMatchThreshold(v) {
  return cpkLowOp === "gt" ? v > cpkLowThreshold : v < cpkLowThreshold;
}

// CPK 필터 버튼 라벨 — 항상 "현재 적용 중인 값"(ALL 또는 임계값)을 보여준다.
function cpkLowBtnLabel() { return cpkShowLowOnly ? `CPK ${cpkOpSign()} ${cpkLowThreshold}` : "ALL"; }

// 목표 Cpk → 시그마 수준 (σ = 3 × Cpk. 예: 1.33→4.0σ, 2→6.0σ, 4→12.0σ).
function cpkSigmaText(v) {
  const c = parseFloat(v);
  return (c > 0) ? `= ${(3 * c).toFixed(1)}σ` : "";
}

// 목표 Cpk 로부터 선택 행의 규격 한계 역산 (평균 중심 대칭: avg ± 3·Cpk·stdev).
// 결과는 누적된다 — 이전 역산값을 지우지 않고 현재 선택 행만 덮어쓴다. 항목마다 다른
// Margin 으로 나눠 역산하려면 체크 → 역산 → 체크해제 → 다른 항목 체크 → 역산 을 반복한다.
// (전체 초기화는 "역산값 지우기" 버튼 / Limit 계산 모드 해제)
// 단 **복사는 누적분 전체가 아니라 지금 체크된 행만** 대상이다 — 누적 역산 후 원하는 행을
// 다시 체크하고 "역산값 복사"를 누르면 그 행들만 나온다.
function cpkComputeTargets() {
  const cpk = parseFloat(cpkTargetVal);
  if (!(cpk > 0)) return;
  // TOTAL 행도 대상이다 — 그 역산은 "전 source 통합 분포 기준 규격"이라 신규 규격 산정에
  // 오히려 유용하다. CPK 시트만 훑으면 TOTAL 행을 체크하고 역산해도 조용히 아무 일도
  // 일어나지 않는다(침묵 실패).
  const byKey = new Map(cpkAllRows().concat(cpkTotalRows()).map(r => [cpkRowKey(r), r]));
  for (const key of cpkSelected) {
    const row = byKey.get(key);
    if (!row) continue;
    // average/stdev 는 Bin1 기준 단일 값(서버 통일) — 표에 보이는 값 그대로 역산한다.
    const avg = parseFloat(row.average), sd = parseFloat(row.stdev);
    if (isNaN(avg) || isNaN(sd) || sd <= 0) continue;   // 계산 불가 → 빈칸
    const d = 3 * cpk * sd;
    let lo = avg - d, hi = avg + d;
    // Margin: 최종 규격이 (역산값)/(1-margin) 가 되도록 각 한계값을 확대.
    // 예) 역산 -0.95~0.95 에 5% → -1~1 (0.95/0.95 = 1.0).
    const m = (parseFloat(cpkMarginPct) || 0) / 100;
    if (m > 0 && m < 1) { lo = lo / (1 - m); hi = hi / (1 - m); }
    cpkTargetResults.set(key, { lo: cpkFmt(lo), hi: cpkFmt(hi) });
  }
}

function updateCpkSelInfo() {
  const el = document.getElementById("cpkSelInfo");
  if (!el) return;
  // 역산값은 체크해제·필터·페이지와 무관하게 누적으로 남지만 복사 대상은 "체크된 행 ∩
  // 역산값 있는 행" 뿐이므로, 실제 복사될 개수를 따로 보여준다.
  let copyable = 0;
  for (const k of cpkSelected) if (cpkTargetResults.has(k)) copyable++;
  el.textContent = `선택 ${cpkSelected.size}개 · 역산값 ${cpkTargetResults.size}개 · 복사 대상 ${copyable}개`;
}

// "동일Limit" 판정: ① Limit(하한==상한) 동일 ② CPK 값 없음 → 대상.
// cpk 는 Bin1 기준 단일 값이라 별도 기준 정규화 없이 행을 그대로 본다.
function cpkIsAbnormal(r) {
  const lo = parseFloat(r.lower_limit), hi = parseFloat(r.upper_limit);
  if (!isNaN(lo) && !isNaN(hi) && lo === hi) return true;   // Limit 동일
  const cpk = parseFloat(r.cpk);
  if (isNaN(cpk)) return true;   // CPK 값 없음
  return false;
}

function cpkFilterRows(rows) {
  // ⚠ 여기 들어오는 rows 는 sheets["CPK"](source 별 행)뿐이다 — TOTAL 은 CPK 임계 필터를
  // 면제받아야 해서 cpkBodyRows 안에서 뒤늦게 합쳐진다(cpkTotalDisplayRows/cpkMergeTotal).
  if (cpkHideCodeUnit)
    rows = rows.filter(r => String(r.units || "").trim().toUpperCase() !== "CODE");
  if (cpkSourceFilter.size) rows = rows.filter(r => cpkSourceFilter.has(String(r.source || "")));
  const term = cpkSearchTerm.trim().toLowerCase();
  if (!term) return rows;
  return rows.filter(r => String(r.subject || "").toLowerCase().includes(term)
    || String(r.source || "").toLowerCase().includes(term));
}

// CPK 행에서 등장 순서대로 중복 제거한 source 목록 (선택 바 옵션용).
function cpkSourceList(rows) {
  return [...new Set((rows || []).map(r => String(r.source || "")).filter(Boolean))];
}

// CPK 시트(source 별 행) / CPK Total 시트(전 source 통합 행). TOTAL 은 payload 에 키가
// 없을 수 있다(스키마 v42 이전 캐시) — 그 경우 빈 배열이라 드롭다운에서 항목 자체를 감춘다.
function cpkAllRows() {
  const sheets = webReportSheets();
  return sheets ? (sheets["CPK"] || []) : [];
}
function cpkTotalRows() {
  const sheets = webReportSheets();
  return sheets ? (sheets["CPK Total"] || []) : [];
}

// 드롭다운에서 TOTAL 을 가리키는 **선택자 전용 sentinel**. 화면 라벨은 CPK_TOTAL_SOURCE
// ("TOTAL")를 그대로 쓰고 data-cpk-src 값만 이걸로 가른다 — 실제 source 이름이 "TOTAL" 인
// 세션에서 그 source 체크박스와 속성값이 겹치면 하나를 켤 때 둘 다 켜진 것처럼 보이고
// change 핸들러가 어느 쪽인지 구분하지 못한다. cpkShowTotal 상태 분리(위)는 Set 오염만
// 막을 뿐 DOM 속성 충돌은 못 막는다.
const CPK_TOTAL_PICK = "__total__";

// Source 입력칸 placeholder — 이 탭 관례대로 **현재 적용 중인 값**만 쓴다.
// 선택이 없으면 "무엇을 하는 칸인지"를, 있으면 무엇이 켜져 있는지를 보여준다.
// (입력칸 value 는 검색어 전용이라 선택 상태를 담지 않는다.)
function cpkSrcPlaceholder() {
  const parts = [];
  if (cpkShowTotal) parts.push(CPK_TOTAL_SOURCE);
  parts.push(...cpkSourceFilter);
  if (!parts.length) return "전체 source (클릭해 선택)";
  if (parts.length <= 2) return parts.join(", ");
  return `${parts[0]} 외 ${parts.length - 1}`;
}

// TOTAL 을 못 쓰는 사유 — 못 쓰면 그 문자열, 쓸 수 있으면 "". 빈 문자열이면 항목을 감춘다.
// **Temperature 만** 사유를 보여준다: 서버가 **영구히** 안 만드는 유일한 경우라
// (metrics.py `if not temp_groups and len(tables) >= 2`) 그냥 감추면 "왜 없지?" 가
// 반복된다(2026-08-27 실제 문의). source 1개·구버전 캐시는 재빌드로 해소되는 일시적
// 상태라 종전대로 감춘다.
function cpkTotalUnavailableReason() {
  if (cpkTotalRows().length) return "";
  if (typeof tempIsMode === "function" && tempIsMode())
    return "Temperature 는 미지원 — 온도별 평균차가 산포로 들어가 cpk 가 실제보다 나쁘게 나옵니다";
  return "";
}

// Source 다중 선택 드롭다운 목록 — Distribution 검색(.dist-suggest)과 **같은 클래스**를
// 재사용한다(trim.js 선례). 종전 .issue-menu 팝오버는 회색 버튼 + fixed 배치라 툴바의
// 흰 입력칸들과 룩이 갈렸다(2026-08-27 사용자 요청).
// ⚠ data-subject 를 쓰면 item_detail.js 의 #panel-distribution change 위임이 읽는 속성과
//   겹친다 — 전용 네임스페이스 data-cpk-src 만 쓴다.
// ⚠ 빈 검색어에도 **전체 목록을 연다**(Distribution 은 닫는다) — source 는 2~21개라
//   타이핑을 요구할 이유가 없고, 사용자는 "클릭해 고르는 칸"을 원했다.
// ⚠ 종전의 체크형 "전체" 항목은 없앴다 — 사용자가 '전체'(선택 해제 동작)와 'TOTAL'
//   (가상 통합 source)을 같은 종류로 오인했다. 해제는 헤더의 **버튼**으로 옮겨 형태로
//   구분되게 했다.
function cpkSourceMenuHtml(list, q) {
  const term = String(q || "").trim().toLowerCase();
  const rows = (list || []).filter(s => !term || String(s).toLowerCase().includes(term));
  const row = (val, badge, name, why, checked, title, off) =>
    (off
      ? `<div class="dist-sug-item cpk-sug-total is-off" title="${esc(title)}">` +
        `<input type="checkbox" class="dist-sug-chk" disabled>`
      : `<label class="dist-sug-item${badge ? " cpk-sug-total" : ""}" title="${esc(title)}">` +
        `<input type="checkbox" class="dist-sug-chk" data-cpk-src="${esc(val)}"` +
        `${checked ? " checked" : ""}>`) +
    `<span class="sug-tno${badge ? " cpk-sug-badge" : ""}">${esc(badge || "")}</span>` +
    `<span class="sug-name">${esc(name)}</span>` +
    (why ? `<span class="cpk-sug-why">${esc(why)}</span>` : "") +
    (off ? `</div>` : `</label>`);

  // TOTAL — source 행과 **같은 체크박스 마크업**으로 나란히 둔다(사용자 요청).
  // 구분은 왼쪽 배지와 옅은 배경뿐이다.
  const why = cpkTotalUnavailableReason();
  let totalHtml = "";
  if (!term || CPK_TOTAL_SOURCE.toLowerCase().includes(term)) {
    if (cpkTotalRows().length)
      totalHtml = row(CPK_TOTAL_PICK, "통합", CPK_TOTAL_SOURCE, "전 source 통합 통계",
        cpkShowTotal,
        "전 source 의 rawdata 를 하나로 합쳐 낸 통계 행. CPK 임계 필터가 적용되지 않아 " +
        "선택하면 전 항목의 TOTAL 행이 보인다. 규격(Limit)은 항목이 처음 등장한 source 기준.",
        false);
    else if (why)
      totalHtml = row("", "통합", CPK_TOTAL_SOURCE, why, false, why, true);
  }

  // 헤더 — 개수 + "전체 해제". 해제는 체크박스가 아니라 **버튼**이다(위 ⚠ 참조).
  const nSel = cpkSourceFilter.size + (cpkShowTotal ? 1 : 0);
  const head = `<div class="dist-sug-head">` +
    `<span class="dist-sug-cnt">source <b>${rows.length}</b>개` +
    (nSel ? ` · 선택 <b>${nSel}</b>개`
          : ` <span class="dist-sug-more">(선택 없음 = 전 source 표시)</span>`) +
    `</span>` +
    (nSel ? `<button type="button" class="btn-sm" data-cpk-src-clear="1">전체 해제</button>` : "") +
    `</div>`;

  const body = rows.map(s =>
    row(s, "", s, "", cpkSourceFilter.has(s),
        "체크해 선택/해제 — 여러 source 를 함께 볼 수 있다", false)).join("");
  return head + totalHtml +
    (body || (term ? `<div class="dist-sug-item cpk-sug-none">일치하는 source 없음</div>` : ""));
}

// 드롭다운 열기/닫기 — 컨테이너는 툴바 HTML 에 처음부터 있고 display 만 토글한다
// (Distribution #distSuggest 와 같은 구조). 종전 팝오버의 createElement + fixed 좌표
// 실측은 사라졌다 — .dist-suggest 의 position:absolute 가 위치를 맡는다.
function cpkOpenSrcBox() {
  const box = document.getElementById("cpkSrcSuggest");
  if (!box) return;
  const inp = document.getElementById("cpkSrcSearch");
  box.innerHTML = cpkSourceMenuHtml(cpkSourceList(cpkAllRows()), inp ? inp.value : "");
  box.style.display = "block";
  if (inp) inp.setAttribute("aria-expanded", "true");
}
function cpkCloseSrcBox() {
  const box = document.getElementById("cpkSrcSuggest");
  const inp = document.getElementById("cpkSrcSearch");
  if (box) box.style.display = "none";
  if (inp) inp.setAttribute("aria-expanded", "false");
}
// 선택이 바뀌어도 드롭다운은 열어 둔다(연속 다중 선택) — 목록의 체크 상태와 입력칸
// placeholder 만 갱신한다. 입력칸(#cpkSrcSearch)과 목록(#cpkSrcSuggest)이 별개 엘리먼트라
// 목록만 다시 그려도 검색어·포커스가 유지된다(Distribution 의 restoreDistSearch 불필요).
function cpkRefreshMenu() {
  const inp = document.getElementById("cpkSrcSearch");
  const box = document.getElementById("cpkSrcSuggest");
  if (box && box.style.display !== "none")
    box.innerHTML = cpkSourceMenuHtml(cpkSourceList(cpkAllRows()), inp ? inp.value : "");
  if (inp) {
    inp.placeholder = cpkSrcPlaceholder();
    inp.classList.toggle("has-sel", !!(cpkSourceFilter.size || cpkShowTotal));
  }
}
// 드롭다운은 Source 칸 영역 밖을 클릭하면 닫는다. 조건을 .dist-search-wrap 이 아니라
// 전용 .cpk-src-wrap 으로 좁힌다 — Distribution/Trim 이 같은 클래스를 쓴다.
document.addEventListener("click", e => {
  if (e.target.closest && e.target.closest(".cpk-src-wrap")) return;
  cpkCloseSrcBox();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") cpkCloseSrcBox(); });

// 표시용 행 목록 생성. 각 행에 _key(원본 subject/source 기준 안정 키)를 붙여 접힌 행도
// 개별 선택이 가능하게 한다. (select-all 계산과 cpkTableHtml 이 공용으로 사용)
// TOTAL 행 후보 — source 별 행과 **같은 보조 필터**(CODE unit / 동일Limit / 검색어)를
// 통과시키되 **CPK 임계 필터만 면제**한다(2026-08-27 사용자 결정): TOTAL 을 고르면 cpk 가
// 좋아서 걸러진 항목도 통합 통계로 확인할 수 있어야 한다.
function cpkTotalDisplayRows() {
  if (!cpkShowTotal) return [];
  let rows = cpkTotalRows();
  if (cpkHideCodeUnit)
    rows = rows.filter(r => String(r.units || "").trim().toUpperCase() !== "CODE");
  if (cpkAbnormalMode === "exclude") rows = rows.filter(r => !cpkIsAbnormal(r));
  else if (cpkAbnormalMode === "only") rows = rows.filter(r => cpkIsAbnormal(r));
  const term = cpkSearchTerm.trim().toLowerCase();
  if (term) rows = rows.filter(r => String(r.subject || "").toLowerCase().includes(term)
    || CPK_TOTAL_SOURCE.toLowerCase().includes(term));
  return rows;
}

// TOTAL 행을 subject 별로 **그 subject 의 첫 행 자리**에 끼워 넣는다. 첫 행인 이유:
// subject/limit/units 반복 생략(아래)이 첫 행의 limit 을 대표로 보여주는데, TOTAL 의
// limit(첫 source 기준)이 곧 TOTAL 계산에 실제로 쓴 규격이라 화면 표시와 Limit 역산이
// 일관해진다. source 행이 하나도 안 남은 subject 의 TOTAL 도 남긴다 — 임계 필터 면제의
// 요지가 "cpk 가 좋아서 걸러진 항목도 TOTAL 로는 보인다" 이기 때문이다.
function cpkMergeTotal(srcRows, totalRows) {
  if (!totalRows.length) return srcRows;
  const bySubject = new Map();
  // 삽입 순서 = totalRows 순서 = 서버가 낸 TEST SEQ 순. 아래 잔여분 재정렬의 기준이다.
  const orderOf = new Map();
  for (const r of totalRows) {
    const s = String(r.subject);
    if (!orderOf.has(s)) orderOf.set(s, orderOf.size);
    bySubject.set(s, r);
  }
  const out = [];
  let prev = null;
  for (const r of srcRows) {
    const s = String(r.subject);
    if (s !== prev) {
      const t = bySubject.get(s);
      if (t) { out.push(t); bySubject.delete(s); }
      prev = s;
    }
    out.push(r);
  }
  if (bySubject.size) {
    // srcRows 에 아예 없던 subject — 뒤에 붙이고 **TEST SEQ 순**(orderOf)으로 재정렬한다.
    // 종전엔 subject localeCompare(이름순)라 이 분기에서만 순서가 갈렸다(2026-08-27).
    // orderOf 에 없는 subject(TOTAL 이 없는 항목)는 뒤로 보내 이름순으로 안정 정렬한다.
    // sort 는 ES2019 부터 stable 이라 같은 subject 의 source 행 순서는 보존된다.
    out.push(...bySubject.values());
    // 미등재는 orderOf.size(=마지막 다음 자리)로 — Infinity 를 쓰면 둘 다 미등재일 때
    // Infinity-Infinity = NaN 이 되어 sort 비교가 깨진다.
    const rank = r => {
      const o = orderOf.get(String(r.subject));
      return o === undefined ? orderOf.size : o;
    };
    out.sort((a, b) => (rank(a) - rank(b))
      || String(a.subject).localeCompare(String(b.subject))
      || (a.source === CPK_TOTAL_SOURCE ? -1 : b.source === CPK_TOTAL_SOURCE ? 1 : 0));
  }
  return out;
}

function cpkBodyRows(rows) {
  if (cpkAbnormalMode === "exclude") rows = rows.filter(r => !cpkIsAbnormal(r));     // 동일Limit 제외
  else if (cpkAbnormalMode === "only") rows = rows.filter(r => cpkIsAbnormal(r));    // 동일Limit 만 (all=필터 없음)
  const totals = cpkTotalDisplayRows();   // CPK 임계 필터 면제분
  if (cpkShowLowOnly) {
    // cpk 값 내림/오름차순이 아니라 같은 Item name 끼리 묶여 보이도록 **서버가 내려준
    // 순서(TEST SEQ 순)를 그대로 쓴다** — filter 는 순서를 보존하므로 재정렬하지 않는다.
    // 종전에는 여기서 subject localeCompare 로 다시 정렬해 이름 사전순이 됐다(2026-08-27).
    // 이 분기는 종전대로 subject 반복 생략을 하지 않는다 — 임계 필터로 source 행이 듬성해져
    // 생략하면 오히려 읽기 어렵다.
    const low = rows
      .filter(r => { const v = parseFloat(r.cpk); return !isNaN(v) && cpkMatchThreshold(v); });
    return cpkMergeTotal(low, totals).map(r => ({ ...r, _key: cpkRowKey(r) }));
  }
  // subject 가 연속으로 반복되면(같은 item, source 별 행) 2번째 행부터 subject/limit/units 를 비움
  let prevSubject = null;
  return cpkMergeTotal(rows, totals).map(r => {
    const row = { ...r, _key: cpkRowKey(r) };
    if (r.subject === prevSubject) {
      row.subject = ""; row.lower_limit = ""; row.upper_limit = ""; row.units = "";
    } else {
      prevSubject = r.subject;
    }
    return row;
  });
}

// 필터+본문행 생성은 전 행(항목×소스)을 매번 다시 매핑·필터·정렬한다. 대형 세션에선
// 수천 행이라 검색 키입력·페이지 이동마다 돌면 눈에 띄게 뻑뻑해진다. 표시 결과를 정하는
// 상태(아래 sig)가 그대로면 지난 결과를 그대로 쓴다 — 페이지 이동은 sig 가 안 변하므로
// 재정렬이 0회가 된다. 원본 rows 는 identity 로 비교한다(payload 교체 시 자동 무효화).
let _cpkRowsMemo = null;
function cpkDisplayRows(rows) {
  // cpkSourceFilter 는 Set 이라 JSON.stringify 가 {} 로 접는다 — 정렬한 배열로 펴서
  // 선택이 바뀌면 반드시 메모가 무효화되게 한다(안 그러면 옛 표가 그대로 남는다).
  // cpkShowTotal 과 TOTAL 행 개수도 넣는다 — TOTAL 은 rows(identity) **밖**에서 오므로
  // 이게 빠지면 드롭다운에서 TOTAL 을 켜도 옛 표가 그대로 남는다(위와 같은 기전).
  const sig = JSON.stringify([cpkAbnormalMode, cpkShowLowOnly,
    cpkLowThreshold, cpkLowOp, cpkHideCodeUnit, cpkSearchTerm, [...cpkSourceFilter].sort(),
    cpkShowTotal, cpkTotalRows().length]);
  if (_cpkRowsMemo && _cpkRowsMemo.rows === rows && _cpkRowsMemo.sig === sig) {
    return _cpkRowsMemo.out;
  }
  const out = cpkBodyRows(cpkFilterRows(rows));
  _cpkRowsMemo = { rows, sig, out };
  return out;
}

function cpkTableHtml(rows) {
  const bodyRows = cpkDisplayRows(rows);

  if (!bodyRows.length) {
    const msg = cpkSearchTerm.trim() ? "검색 결과 없음"
      : `CPK ${cpkOpSign()} ${cpkLowThreshold} 항목 없음`;
    return `<div class="placeholder">${msg}</div>`;
  }

  // 100개씩 페이지네이션 (subject/source 컬럼 좌측 고정, cpk 컬럼 우측 고정, 헤더 상단 고정).
  const totalPages = Math.max(1, Math.ceil(bodyRows.length / CPK_PAGE_SIZE));
  if (cpkPage > totalPages) cpkPage = totalPages;
  if (cpkPage < 1) cpkPage = 1;
  const start = (cpkPage - 1) * CPK_PAGE_SIZE;
  const pageRows = bodyRows.slice(start, start + CPK_PAGE_SIZE);

  // Limit 계산 모드면 표시 컬럼에 target 열 2개를 upper_limit 뒤에 삽입, 체크박스 열은 별도 선두.
  const displayCols = CPK_COLUMNS.slice();
  if (cpkTargetMode) {
    const i = displayCols.indexOf("upper_limit");
    displayCols.splice(i + 1, 0, "target_lolimit", "target_hilimit");
  }
  const allSelected = cpkTargetMode && bodyRows.every(r => cpkSelected.has(r._key));
  const selTh = cpkTargetMode
    ? `<th class="cpk-sel-col"><input type="checkbox" id="cpkSelAll"${allSelected ? " checked" : ""}></th>` : "";
  const head = "<thead><tr>" + selTh + displayCols.map(c => `<th>${esc(c)}</th>`).join("") + "</tr></thead>";
  const body = "<tbody>" + pageRows.map(row => {
    const cpkVal = parseFloat(row.cpk);
    const isWarn = !isNaN(cpkVal) && cpkMatchThreshold(cpkVal);
    const res = cpkTargetResults.get(row._key);
    const selTd = cpkTargetMode
      ? `<td class="cpk-sel-col"><input type="checkbox" class="cpk-row-chk" data-key="${esc(row._key)}"${cpkSelected.has(row._key) ? " checked" : ""}></td>` : "";
    const tds = displayCols.map(c => {
      if (c === "target_lolimit" || c === "target_hilimit") {
        const tv = res ? (c === "target_lolimit" ? res.lo : res.hi) : "";
        const cls = tv === "" ? "st-empty" : "st-num cpk-target";
        return `<td class="${cls}">${esc(String(tv))}</td>`;
      }
      const v = row[c];
      const raw = (v === null || v === undefined) ? "" : String(v);
      // 통계 5컬럼은 표시 길이를 8자로 줄이고, 실제로 줄어든 경우에만 원값을 title 툴팁에
      // 남긴다(Issue Table CPK 축약 sheets.js 와 같은 관례 — 모든 셀에 붙이면 점선 밑줄이
      // 표 전체를 덮어 신호 가치가 사라진다). row 원값은 그대로라 cpkComputeTargets 의
      // Limit 역산은 영향받지 않는다.
      const txt = (raw !== "" && CPK_LEN8_COLS.has(c)) ? fmtLen8(v) : raw;
      const cls = [];
      if (txt === "") cls.push("st-empty");
      else if (CPK_NUMERIC.has(c)) cls.push("st-num");
      if (c === "cpk" && isWarn) cls.push("cpk-warn");
      let tip = "";
      if (txt !== raw) { tip = ` title="${esc(raw)}"`; cls.push("cpk-abbr"); }
      // subject 셀(비어있지 않을 때) → Item_detail 링크
      const inner = (c === "subject" && txt !== "")
        ? `<span class="item-detail-link" data-subject="${esc(txt)}">${esc(txt)}</span>` : esc(txt);
      return `<td${cls.length ? ` class="${cls.join(" ")}"` : ""}${tip}>${inner}</td>`;
    }).join("");
    return `<tr>${selTd}${tds}</tr>`;
  }).join("") + "</tbody>";

  const table = `<div class="sheet-wrap cpk-sheet"><table class="sheet-table cpk-sheet${cpkTargetMode ? " has-select" : ""}">${head}${body}</table></div>`;
  const end = Math.min(start + CPK_PAGE_SIZE, bodyRows.length);
  const pager = `<div class="cpk-pager">` +
    `<button type="button" class="btn-sm" data-cpk-page="${cpkPage - 1}"${cpkPage <= 1 ? " disabled" : ""}>‹ 이전</button>` +
    `<span class="cpk-pager-info">${start + 1}–${end} / ${bodyRows.length} (page ${cpkPage}/${totalPages})</span>` +
    `<button type="button" class="btn-sm" data-cpk-page="${cpkPage + 1}"${cpkPage >= totalPages ? " disabled" : ""}>다음 ›</button>` +
    `</div>`;
  return table + pager;
}

// 검색어/토글 상태만 바뀌었을 때 테이블만 다시 그림 (검색창 포커스 유지).
function renderCpkTable() {
  const sheets = webReportSheets();
  const rows = sheets ? (sheets["CPK"] || []) : [];
  const host = document.getElementById("cpkTableHost");
  // 필터는 cpkDisplayRows(메모) 안에서 적용된다 — 여기서 미리 걸면 매 호출 새 배열이 나와
  // 메모가 항상 미스가 된다.
  if (host) host.innerHTML = cpkTableHtml(rows);
}

// 검색·임계값 입력은 키입력마다 수천 행 재필터·재정렬을 유발한다. 입력이 멈춘 뒤 한 번만
// 그린다 (표만 다시 그리므로 입력 포커스·캐럿은 그대로 유지된다).
const CPK_INPUT_DEBOUNCE_MS = 150;
let _cpkTableTimer = null;
function renderCpkTableDebounced() {
  if (_cpkTableTimer) clearTimeout(_cpkTableTimer);
  _cpkTableTimer = setTimeout(() => { _cpkTableTimer = null; renderCpkTable(); },
                              CPK_INPUT_DEBOUNCE_MS);
}

function renderCpk() {
  const panel = document.getElementById("panel-cpk");
  const sheets = webReportSheets();
  const rows = sheets ? (sheets["CPK"] || []) : [];
  if (!rows.length) { emptyPanel(panel, "CPK 데이터 없음"); return; }
  // (Source 드롭다운은 툴바 innerHTML 안에 있어 패널 재렌더로 함께 사라진다 — 종전
  //  팝오버처럼 모듈 전역에 남는 고아 참조가 없어 정리 호출이 필요 없다.)

  const targetBar = cpkTargetMode
    ? `<div class="cpk-target-bar">목표 Cpk ` +
      `<input type="number" id="cpkTargetInput" min="0" step="0.01" value="${esc(String(cpkTargetVal))}">` +
      `<span id="cpkSigmaVal" class="cpk-pager-info" title="시그마 수준 = 3 × Cpk (1.33=4σ, 2=6σ, 4=12σ)">${cpkSigmaText(cpkTargetVal)}</span>` +
      `<span class="cpk-margin-wrap">Margin ` +
      `<select id="cpkMarginSel">` +
      [0, 1, 5, 10].map(v =>
        `<option value="${v}"${Number(cpkMarginPct) === v ? " selected" : ""}>${v === 0 ? "없음" : v + "%"}</option>`).join("") +
      `</select></span>` +
      `<button type="button" id="cpkCalcBtn" class="btn-sm">역산</button>` +
      `<button type="button" id="cpkCopyBtn" class="btn-sm" title="지금 체크된 행의 역산값만 TSV 로 복사 (체크 해제된 행의 누적 역산값은 복사되지 않음)">역산값 복사</button>` +
      `<button type="button" id="cpkClearSelBtn" class="btn-sm">선택 해제</button>` +
      `<button type="button" id="cpkClearResBtn" class="btn-sm" title="누적된 역산값을 모두 지운다 (체크해제·선택 해제로는 지워지지 않음)">역산값 지우기</button>` +
      `<span id="cpkSelInfo" class="cpk-pager-info"></span></div>`
    : "";
  // 전처리(항목/소스 축소)로 사라진 source 가 선택에 남아 있으면 표가 통째로 비어 보인다
  // — 실제 목록에 없는 선택은 그릴 때 걷어낸다.
  const sourceList = cpkSourceList(rows);
  if (cpkSourceFilter.size) {
    const alive = new Set(sourceList);
    for (const s of [...cpkSourceFilter]) if (!alive.has(s)) cpkSourceFilter.delete(s);
  }
  // TOTAL 은 cpkSourceFilter 밖(별도 플래그)이라 위 정리에 안 걸린다 — 서버가 행을 안 준
  // 세션(Temperature·단일 source·v42 이전 캐시)에서 켜진 채 남지 않게 여기서 끈다.
  if (cpkShowTotal && !cpkTotalRows().length) cpkShowTotal = false;
  panel.innerHTML =
    `<div class="cpk-toolbar">` +
    `<input type="text" id="cpkSearchInput" data-no-dirty placeholder="항목/source 검색" value="${esc(cpkSearchTerm)}">` +
    `<span class="cpk-tool-group"><span class="cpk-tool-label">CPK 구분</span>` +
    `<select id="cpkLowOpSel" title="임계값 비교 방향 — '<'=이 값 미만(기본) · '>'=이 값 초과">` +
    `<option value="lt"${cpkLowOp === "lt" ? " selected" : ""}>&lt;</option>` +
    `<option value="gt"${cpkLowOp === "gt" ? " selected" : ""}>&gt;</option></select>` +
    `<input type="number" id="cpkLowInput" min="0" step="0.01" value="${esc(cpkLowInputRaw)}" title="CPK 임계값 — 왼쪽 부등호 방향으로 이 값과 비교해 항목을 추려 보거나(버튼) 표에서 노랗게 강조한다.">` +
    `<button type="button" id="cpkLowBtn" class="btn-sm${cpkShowLowOnly ? " active" : ""}" title="현재 적용 중인 CPK 필터(클릭하면 전환): 'ALL'=전체 항목 · 'CPK &lt;(또는 &gt;) 임계값'=부등호 방향에 해당하는 항목만 항목명 순 정렬">` +
    `${cpkLowBtnLabel()}</button></span>` +
    `<span class="cpk-tool-group"><span class="cpk-tool-label">동일Limit 구분</span>` +
    `<button type="button" id="cpkAbnBtn" class="btn-sm${cpkAbnormalMode !== "all" ? " active" : ""}" title="현재 적용 중인 동일Limit 필터(클릭하면 순환): '동일Limit 제외'(기본)=해당 항목을 뺀 나머지 → 'ALL'=전체 표시 → '동일Limit only'=해당 항목만. 판정 기준: ① 상·하한(Limit)이 같아 공차가 0인 항목, ② CPK 계산 불가(값 없음).">${CPK_ABN_LABELS[cpkAbnormalMode]}</button></span>` +
    // Source 다중 선택 — Distribution 검색칸과 **같은 흰 입력칸 + 드롭다운**(사용자 요청).
    // 고를 것이 없으면(source 1개 + TOTAL 없음 + 안내할 사유도 없음) 통째로 감춘다.
    // ⚠ data-no-dirty 필수 — 없으면 검색만 해도 edit_mode 가 미저장 변경으로 보고
    //   이탈 경고를 띄운다.
    ((sourceList.length >= 2 || cpkTotalRows().length || cpkTotalUnavailableReason())
      ? `<span class="cpk-tool-group"><span class="cpk-tool-label">Source</span>` +
        `<span class="dist-search-wrap cpk-src-wrap" data-no-dirty>` +
        `<input type="text" id="cpkSrcSearch" class="dist-search cpk-src-search` +
        `${cpkSourceFilter.size || cpkShowTotal ? " has-sel" : ""}" autocomplete="off"` +
        ` role="combobox" aria-expanded="false" placeholder="${esc(cpkSrcPlaceholder())}"` +
        ` title="클릭하면 source 목록이 열린다 (체크로 여러 개 동시 선택 · TOTAL = 전 source 통합 통계). 타이핑하면 목록이 걸러진다.">` +
        `<div id="cpkSrcSuggest" class="dist-suggest cpk-src-suggest" style="display:none"></div>` +
        `</span></span>`
      : "") +
    `<button type="button" id="cpkCodeUnitBtn" class="btn-sm${cpkHideCodeUnit ? " active" : ""}" title="켜짐: 단위(Unit)가 CODE 인 항목(디지털 code 값, 공정능력 지표가 무의미) 숨김 · 꺼짐: 전체 표시">Unit CODE 제거</button>` +
    `<button type="button" id="cpkTargetBtn" class="btn-sm${cpkTargetMode ? " active" : ""}">Limit 계산</button>` +
    `<button type="button" class="btn-sm tab-excel-btn" id="cpkExcelBtn" title="Honey Excel Download 의 CPK 시트와 동일한 xlsx 다운로드 (Bin1 기준·화면 필터 무관 · TOTAL 행 제외)">Excel Down</button></div>` +
    targetBar +
    `<div id="cpkTableHost"></div>`;
  renderCpkTable();
  updateCpkSelInfo();
  document.getElementById("cpkLowBtn").addEventListener("click", () => {
    cpkShowLowOnly = !cpkShowLowOnly;
    cpkPage = 1;
    renderCpk();
  });
  // 임계값 입력: 표만 다시 그려 입력 포커스·캐럿을 유지한다(renderCpk 는 입력창을 날림).
  document.getElementById("cpkLowInput").addEventListener("input", (e) => {
    cpkLowInputRaw = e.target.value;
    const v = parseFloat(cpkLowInputRaw);
    if (!isFinite(v)) return;   // 빈칸·중간 입력 상태 — 마지막 유효값 유지
    cpkLowThreshold = v;
    cpkPage = 1;
    const btn = document.getElementById("cpkLowBtn");
    if (btn) btn.innerHTML = cpkLowBtnLabel();
    renderCpkTableDebounced();
  });
  // 부등호 변경: 필터·강조 방향이 바뀌므로 버튼 라벨과 표를 함께 갱신한다.
  document.getElementById("cpkLowOpSel").addEventListener("change", (e) => {
    cpkLowOp = e.target.value === "gt" ? "gt" : "lt";
    cpkPage = 1;
    const btn = document.getElementById("cpkLowBtn");
    if (btn) btn.innerHTML = cpkLowBtnLabel();
    renderCpkTable();
  });
  document.getElementById("cpkAbnBtn").addEventListener("click", () => {
    cpkAbnormalMode = CPK_ABN_ORDER[(CPK_ABN_ORDER.indexOf(cpkAbnormalMode) + 1) % CPK_ABN_ORDER.length];
    cpkPage = 1;
    renderCpk();
  });
  document.getElementById("cpkCodeUnitBtn").addEventListener("click", () => {
    cpkHideCodeUnit = !cpkHideCodeUnit;
    cpkPage = 1;
    renderCpk();
  });
  document.getElementById("cpkSearchInput").addEventListener("input", (e) => {
    cpkSearchTerm = e.target.value;
    cpkPage = 1;
    renderCpkTableDebounced();
  });
  document.getElementById("cpkTargetBtn").addEventListener("click", () => {
    cpkTargetMode = !cpkTargetMode;
    if (!cpkTargetMode) { cpkSelected.clear(); cpkTargetResults.clear(); }
    renderCpk();
  });
  if (cpkTargetMode) {
    document.getElementById("cpkTargetInput").addEventListener("input", (e) => {
      cpkTargetVal = e.target.value;
      const sig = document.getElementById("cpkSigmaVal");
      if (sig) sig.textContent = cpkSigmaText(cpkTargetVal);   // 시그마 수준 실시간 갱신
    });
    document.getElementById("cpkMarginSel").addEventListener("change", (e) => {
      cpkMarginPct = parseFloat(e.target.value) || 0;
      // Margin 변경 즉시 재계산 — 단 대상은 "지금 체크된 행"뿐이다. 체크 해제한 행의
      // 역산값은 이전 Margin 으로 계산된 채 그대로 남는다(항목별 Margin 혼용).
      if (cpkTargetResults.size) { cpkComputeTargets(); renderCpkTable(); }
    });
    document.getElementById("cpkCalcBtn").addEventListener("click", () => {
      cpkComputeTargets();
      renderCpkTable();
    });
    document.getElementById("cpkCopyBtn").addEventListener("click", (e) => {
      // **지금 체크된 행**의 역산값만 헤더 포함 4열 TSV 로 복사 → Excel 표 붙여넣기.
      // 역산값(cpkTargetResults)은 항목별 Margin 혼용을 위해 체크 해제 후에도 누적 보존되지만,
      // 복사 대상은 그 누적분 전체가 아니라 "체크된 행 ∩ 역산값 있는 행"이다.
      // 원본 CPK 행 순서로 훑어 화면 필터·페이지와 무관하게 선택분을 빠짐없이 담는다.
      // TOTAL 행도 뒤에 이어 훑는다 — 빠뜨리면 체크한 TOTAL 의 역산값이 조용히 누락된다.
      const lines = [["subject", "target_lolimit", "target_hilimit", "units"].join("\t")];
      for (const r of cpkAllRows().concat(cpkTotalRows())) {
        const key = cpkRowKey(r);
        if (!cpkSelected.has(key)) continue;
        const res = cpkTargetResults.get(key);
        if (!res) continue;   // 체크만 하고 역산 안 한 행은 제외
        const u = r.units;
        lines.push(`${r.subject}\t${res.lo}\t${res.hi}\t${(u === null || u === undefined) ? "" : u}`);
      }
      const btn = e.currentTarget;
      const flash = (msg) => { const t = btn.textContent; btn.textContent = msg; setTimeout(() => { btn.textContent = t; }, 1200); };
      if (lines.length <= 1) { flash(cpkSelected.size ? "역산값 없음" : "선택 없음"); return; }
      // HTTP LAN 환경(secure context 아님) 대비 execCommand 폴백이 있는 공용 헬퍼 사용.
      cellSelCopyText(lines.join("\n")).then(ok => flash(ok ? "복사됨" : "복사 실패"));
    });
    document.getElementById("cpkClearSelBtn").addEventListener("click", () => {
      cpkSelected.clear();   // 역산값은 보존 — 지우려면 "역산값 지우기"
      updateCpkSelInfo();
      renderCpkTable();
    });
    document.getElementById("cpkClearResBtn").addEventListener("click", () => {
      cpkTargetResults.clear();
      updateCpkSelInfo();
      renderCpkTable();
    });
  }
  // panel-cpk 는 재렌더돼도 요소 자체는 유지되므로 페이저 클릭·체크박스 위임은 1회만 바인딩한다.
  if (!cpkPanelBound) {
    panel.addEventListener("click", (e) => {
      // Source 드롭다운 열기 — 빈 검색어에도 전체 목록을 낸다.
      if (e.target.closest("#cpkSrcSearch")) { cpkOpenSrcBox(); return; }
      // "전체 해제" — 개별 선택 + TOTAL 을 모두 끈다. 체크박스가 아니라 **버튼**이라
      // 목록의 선택지들과 형태로 구분된다("전체"는 대상이 아니라 동작이다).
      // ⚠ renderCpk()(패널 전체 재렌더) 금지 — 드롭다운 DOM·검색어·포커스가 날아간다.
      if (e.target.closest("[data-cpk-src-clear]")) {
        cpkSourceFilter.clear();
        cpkShowTotal = false;
        cpkPage = 1;
        renderCpkTable();
        cpkRefreshMenu();
        return;
      }
      const pb = e.target.closest("[data-cpk-page]");
      if (!pb || pb.disabled) return;
      cpkPage = parseInt(pb.dataset.cpkPage, 10) || 1;
      renderCpkTable();
    });
    // Source 검색 — 타이핑하면 목록을 걸러 다시 연다. 위임인 이유는 이 입력칸이 조건부
    // 렌더(고를 것이 없으면 없음)라 getElementById 직접 바인딩이 매번 성립하지 않아서다.
    // debounce 없음 — 최대 22항목이라 250ms 를 기다리면 오히려 답답하다(Distribution 의
    // debounce 는 distIndex 수백~수천 항목 전량 스캔 때문이다).
    panel.addEventListener("input", (e) => {
      if (e.target.id !== "cpkSrcSearch") return;
      cpkOpenSrcBox();
    });
    panel.addEventListener("change", (e) => {
      const t = e.target;
      // Source 체크박스(다중 선택). TOTAL 은 cpkSourceFilter(Set) 가 아니라 cpkShowTotal
      // 별도 플래그이고, DOM 값도 sentinel 이라 실제 source 이름 "TOTAL" 과 안 겹친다.
      // ⚠ click 이 아니라 change 다 — <label> 안 체크박스는 click 이 label+input 두 번
      //   발화해 토글이 상쇄된다. t.checked 를 쓰는 것이 !flag 토글보다 안전하다(DOM 이 진실).
      // ⚠ renderCpk()(패널 전체 재렌더) 금지 — 드롭다운 DOM 이 날아가 연속 다중 선택이
      //   불가능해진다. 표와 목록만 부분 갱신한다.
      const sb = t.closest("[data-cpk-src]");
      if (sb) {
        const v = sb.dataset.cpkSrc;
        if (v === CPK_TOTAL_PICK) cpkShowTotal = sb.checked;
        else if (sb.checked) cpkSourceFilter.add(v);
        else cpkSourceFilter.delete(v);
        cpkPage = 1;
        renderCpkTable();
        cpkRefreshMenu();
        return;
      }
      if (t.id === "cpkSelAll") {
        const sheets = webReportSheets();
        const keys = cpkDisplayRows(sheets ? (sheets["CPK"] || []) : []).map(r => r._key);
        // 체크해제해도 역산값(cpkTargetResults)은 지우지 않는다 — 항목별로 다른 Margin 으로
        // 나눠 역산할 수 있게 결과를 누적 보존한다.
        if (t.checked) keys.forEach(k => cpkSelected.add(k));
        else keys.forEach(k => cpkSelected.delete(k));
        updateCpkSelInfo();
        renderCpkTable();
      } else if (t.classList.contains("cpk-row-chk")) {
        const k = t.dataset.key;
        if (t.checked) cpkSelected.add(k);
        else cpkSelected.delete(k);
        updateCpkSelInfo();
        renderCpkTable();
      }
    });
    cpkPanelBound = true;
  }
}


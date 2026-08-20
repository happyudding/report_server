// Input File Information — 세션 상세 우상단 ℹ 버튼이 여는 모달.
//
// "세션 안에서 내 input Data 정보를 볼 방법이 없다"(2026-08-20 요청)에 대한 답이다.
// source 별로 legend·입력 파일·파일 stat 과 STDF 헤더(LotID·Wafer No·Test 시각·Test Time)를
// 한 표에 펼치고, Compare/Temperature 면 그룹 소제목으로 나눈다.
//
// 값의 출처는 업로드 시점 manifest 하나뿐이라 세션이 열려 있는 동안 변하지 않는다 →
// **세션당 1회만 fetch** 하고 그 뒤로는 캐시본을 다시 그린다.
//
// ⚠️ 열 구성은 데이터가 정한다: 전 source 가 비어 있는 열은 **통째로 숨긴다**. 파서가
// STDF 헤더를 주지 않는 지금은 그 열들이 자동으로 사라지고 안내문만 남는다 —
// 담당자가 채워 보내기 시작하면 코드 수정 없이 열이 나타난다.

const IINFO_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"];

function iinfoSize(bytes) {
  if (typeof bytes !== "number" || !isFinite(bytes) || bytes < 0) return "";
  let v = bytes, u = 0;
  while (v >= 1024 && u < IINFO_SIZE_UNITS.length - 1) { v /= 1024; u++; }
  return (u === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + " " + IINFO_SIZE_UNITS[u];
}

// 초 → "1h 23m 45s". Test Time 은 사람이 "얼마나 걸렸나"로 읽는 값이라 초 원문보다 낫다.
function iinfoDuration(sec) {
  const n = Number(sec);
  if (!isFinite(n) || n < 0) return "";
  const h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), s = Math.round(n % 60);
  const parts = [];
  if (h) parts.push(h + "h");
  if (h || m) parts.push(m + "m");
  parts.push(s + "s");
  return parts.join(" ");
}

// STDF 가 test_time 을 직접 주지 않아도 시작/종료가 있으면 그 차이가 곧 Test Time 이다.
function iinfoTestTime(src) {
  const st = src.stdf || {};
  if (st.test_time_sec !== undefined && st.test_time_sec !== "") {
    return iinfoDuration(st.test_time_sec);
  }
  const a = Date.parse(String(st.start_time || "").replace(" ", "T"));
  const b = Date.parse(String(st.finish_time || "").replace(" ", "T"));
  if (isFinite(a) && isFinite(b) && b >= a) return iinfoDuration((b - a) / 1000);
  return "";
}

const IINFO_COLS = [
  // always: 값이 없어도 남기는 열 (source 를 식별하는 최소 정보)
  { label: "#",             always: true, num: true, get: s => String(s.index + 1) },
  { label: "Source",        always: true, get: s => s.name },
  { label: "Role",          get: s => s.role },
  { label: "Input File",    always: true, file: true, get: s => s.file_name },
  { label: "LOT ID",        get: s => (s.stdf || {}).lot_id },
  { label: "Sub LOT",       get: s => (s.stdf || {}).sublot_id },
  { label: "Wafer No",      get: s => (s.stdf || {}).wafer_id },
  { label: "Test 시작",     get: s => (s.stdf || {}).start_time },
  { label: "Test 종료",     get: s => (s.stdf || {}).finish_time },
  { label: "Test Time",     get: s => iinfoTestTime(s) },
  { label: "Test 수량",     num: true, get: s => (s.stdf || {}).part_count },
  { label: "Good",          num: true, get: s => (s.stdf || {}).good_count },
  { label: "Part Type",     get: s => (s.stdf || {}).part_type },
  { label: "Job",           get: s => (s.stdf || {}).job_name },
  { label: "Tester",        get: s => (s.stdf || {}).node_name },
  { label: "Tester Type",   get: s => (s.stdf || {}).tester_type },
  { label: "Operator",      get: s => (s.stdf || {}).oper_name },
  { label: "파일 생성",     get: s => s.file_created },
  { label: "파일 수정",     get: s => s.file_modified },
  { label: "크기",          num: true, get: s => iinfoSize(s.file_size) },
  { label: "경로",          path: true, get: s => s.file_path },
];

function iinfoValue(col, src) {
  let v;
  try { v = col.get(src); } catch (e) { v = ""; }
  return (v === undefined || v === null) ? "" : String(v);
}

// 병합 입력(input_files)이 2개 이상인 source 는 파일명 칸에 "외 N개" 를 덧붙인다 —
// MDDI 처럼 입력 n개가 1 source 로 병합되면 대표 파일 하나만 보여선 오해를 산다.
function iinfoFileCell(src) {
  const extra = (src.input_files || []).length;
  const name = src.file_name || "";
  if (!name) return "-";
  const suffix = extra > 1 ? ` <span class="iinfo-sub">외 ${extra - 1}개</span>` : "";
  return esc(name) + suffix;
}

function iinfoRowsHtml(sources, cols) {
  let html = "", lastGroup = null;
  sources.forEach(src => {
    if (src.group && src.group !== lastGroup) {
      html += `<tr class="iinfo-grouprow"><td colspan="${cols.length}">${esc(src.group)}</td></tr>`;
      lastGroup = src.group;
    }
    const tds = cols.map(col => {
      if (col.file) {
        // 툴팁에 병합 입력 전체 목록(없으면 절대경로) — 화면 폭은 파일명만 쓴다.
        const title = (src.input_files || []).join("\n") || src.file_path || src.file_name || "";
        return `<td title="${esc(title)}">${iinfoFileCell(src)}</td>`;
      }
      const raw = iinfoValue(col, src);
      const cls = [];
      if (col.num) cls.push("iinfo-num");
      if (col.path) cls.push("iinfo-path");
      if (!raw) cls.push("iinfo-none");
      const attr = cls.length ? ` class="${cls.join(" ")}"` : "";
      const title = col.path && raw ? ` title="${esc(raw)}"` : "";
      return `<td${attr}${title}>${raw ? esc(raw) : "-"}</td>`;
    }).join("");
    html += `<tr>${tds}</tr>`;
  });
  return html;
}

function iinfoRender(data) {
  // 그룹 정렬 — 그룹 안에서는 업로드 순서 유지(안정 정렬). 서버가 준 group_index 가 곧
  // Compare 의 Before→After, Temperature 의 그룹 번호 순서다.
  const sources = (data.sources || []).map((s, i) => ({ s, i }))
    .sort((a, b) => {
      const ga = a.s.group_index, gb = b.s.group_index;
      if (ga !== gb && ga >= 0 && gb >= 0) return ga - gb;
      return a.i - b.i;
    }).map(x => x.s);

  // 값이 하나라도 있는 열만 남긴다 (빈 칸만 늘어선 열은 정보가 아니라 소음이다).
  const cols = IINFO_COLS.filter(col =>
    col.always || sources.some(src => iinfoValue(col, src) !== ""));

  document.getElementById("iinfoBody").innerHTML =
    `<table class="iinfo-table"><thead><tr>` +
    cols.map(c => `<th>${esc(c.label)}</th>`).join("") +
    `</tr></thead><tbody>${iinfoRowsHtml(sources, cols)}</tbody></table>`;

  const mode = data.mode || "Normal";
  document.getElementById("iinfoDesc").textContent =
    `이 세션을 만든 입력 파일 정보입니다. (모드: ${mode} · source ${sources.length}개)`;

  // 왜 비었는지를 말해 주지 않으면 사용자는 "고장" 으로 읽는다.
  const notes = [];
  if (!data.has_file_info) {
    notes.push("이 세션은 Input File 정보 기능이 추가되기 전에 업로드되어 파일 경로·크기·"
               + "생성 날짜가 없습니다. 새로 업로드한 세션부터 표시됩니다.");
  }
  if (!data.has_stdf) {
    notes.push("STDF 헤더 정보(LOT ID·Wafer No·Test 시각·Test Time)는 현재 입력 파서가 "
               + "제공하지 않아 표시할 수 없습니다. 파서가 값을 넘겨주기 시작하면 해당 "
               + "열이 자동으로 나타납니다.");
  }
  document.getElementById("iinfoNote").innerHTML = notes.length
    ? `<p class="iinfo-note">${notes.map(esc).join("<br>")}</p>` : "";
}

let _iinfoCache = null;

async function openInputInfo() {
  const modal = document.getElementById("inputInfoModal");
  if (_iinfoCache) { iinfoRender(_iinfoCache); modal.classList.add("show"); return; }
  document.getElementById("iinfoNote").innerHTML = "";
  document.getElementById("iinfoBody").innerHTML =
    `<p class="placeholder" style="padding:18px;">입력 파일 정보를 불러오는 중…</p>`;
  modal.classList.add("show");
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/input_info`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _iinfoCache = await res.json();
    iinfoRender(_iinfoCache);
  } catch (e) {
    document.getElementById("iinfoBody").innerHTML =
      `<p class="placeholder" style="padding:18px;">입력 파일 정보를 불러오지 못했습니다. (${esc(e.message || e)})</p>`;
  }
}

function closeInputInfo() {
  document.getElementById("inputInfoModal").classList.remove("show");
}

document.getElementById("btnInputInfo").addEventListener("click", openInputInfo);
document.getElementById("iinfoClose").addEventListener("click", closeInputInfo);
document.getElementById("inputInfoModal").addEventListener("click", e => {
  if (e.target.id === "inputInfoModal") closeInputInfo();   // 바깥(오버레이) 클릭으로 닫기
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && document.getElementById("inputInfoModal").classList.contains("show")) {
    closeInputInfo();
  }
});

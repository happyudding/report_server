// ── grid model 렌더 (xlsx 원형 재현) ─────────────────────────────────────────
function isGrid(o) { return !!(o && typeof o === "object" && Array.isArray(o.cells)); }

// 편집된 grid DOM → 원본 grid 복제본에 셀 텍스트만 반영 (legacy 저장 경로 전용)
function collectGrid(panelEl, baseGrid) {
  if (!isGrid(baseGrid)) return baseGrid;
  const out = JSON.parse(JSON.stringify(baseGrid));
  panelEl.querySelectorAll("td[data-r]").forEach(td => {
    const r = +td.dataset.r, c = +td.dataset.c;
    if (out.cells[r] && out.cells[r][c]) out.cells[r][c].t = td.textContent;
  });
  return out;
}

// ── sheet-table 렌더 (xlsx 텍스트 데이터 원형 재현) ──────────────────────────

// 열 이름 → 고정 너비(px) — xlsx 실측 기준 패턴.
// kind==="issue" 이면 Issue Table 은 Distribution 셀을 크게 보여줘야 해 전체 컬럼을 1.5배로 키운다.
// narrowSrc: source 컬럼이 SRC_NARROW_MIN 이상일 때 — {src}_yield/_count 폭 힌트를 숫자(xx.xx)
// 크기까지 낮춘다. auto table-layout 은 지정 폭보다 내용(축약된 헤더 라벨 / 값)이 넓으면 그만큼만
// 넓히고 그 아래로는 줄이지 않으므로, 결과 폭 = max(헤더 축약 라벨, xx.xx) 라는 최소값이 된다.
function colWidth(name, kind, narrowSrc) {
  const n = String(name || "").toLowerCase().trim();
  const s = kind === "issue" ? 1.5 : 1;   // Issue Table 전체 1.5배 확대
  const px = base => `${Math.round(base * s)}px`;
  // Step/Bin 은 최대 3자리라 아주 좁게, TNO 는 조금 넓게, avg/yield 는 xx.xx 라 짧게.
  // Issue Table 은 Map/Distribution 을 왼쪽에 함께 틀고정하므로 식별컬럼을 좁혀 고정 블록 폭을
  // 줄인다(Step 유지, Yield 표는 무영향). Map/Dist 최소폭은 CSS min-width 로 보장하고, 공간이
  // 모자라면 Item 을 더 줄이는 방향(사용자 요청) — Bin/TNO 70%, Item 55%.
  // Issue Table Step 은 접기/펼치기 화살표를 값 아래 줄로 내렸으므로(CSS .kind-issue
  // .issue-toggle{display:block}) 폭 힌트를 헤더 라벨("Step") 크기까지 좁힌다.
  if (n === "step")                     return px(kind === "issue" ? 28 : 44);
  if (n === "bin")                      return px(kind === "issue" ? 44 * 0.7 : 44);
  if (n === "tno")                      return px(kind === "issue" ? 60 * 0.7 : 60);
  if (n === "map")                      return px(96);   // Distribution 과 동일 폭
  if (n === "distribution")             return px(96);   // 기존 120 의 0.8배
  if (n.endsWith("_count"))             return px(narrowSrc ? 38 : 60);
  if (n.endsWith("_yield"))             return px(narrowSrc ? 38 : 60);
  if (n === "avg")                      return px(48);
  // Open/Close 드랍다운은 CSS 로 "Close" 글자 폭까지 좁혔다(select.issue-status-sel) —
  // 폭 힌트도 함께 낮춰 실제 폭이 max(헤더 "Status", 드랍다운) 로 정해지게 한다.
  if (n === "status")                   return px(38);   // Issue Table Open/Close 드랍다운
  if (n === "item")                     return px(kind === "issue" ? 150 * 0.55 : 150);
  if (n === "category")                 return "50px";
  if (n === "condition & judge limit")  return "185px";
  if (n === "result")                   return "80px";
  if (n.includes("comment"))            return px(220);
  return px(80);
}

// 값이 숫자인지 (td 정렬용)
function isNumVal(v) { return v !== null && v !== undefined && v !== "" && !isNaN(+v); }

function isDistCol(c) { return String(c || "").toLowerCase() === "distribution"; }
function isMapCol(c) { return String(c || "").toLowerCase() === "map"; }
function isCommentCol(c) { return /comment/i.test(String(c || "")); }

// ── comment 서식 토큰 (*[..] 계열) ────────────────────────────────────────────
// PTE/개발 comment 안에서 특정 글자만 색·굵기로 강조하기 위한 표시 토큰.
//   *[텍스트]   굵게        *r[텍스트]  색만        *R[텍스트]  색 + 굵게
// 색 글자: r=빨강 o=주황 g=초록 b=파랑. **굵기는 "글자 없음" 또는 "대문자"로만
// 표현하므로 b 는 bold 가 아니라 blue 다.** 모르는 글자(*x[..])는 토큰이 아니라
// 평문 그대로 esc 된다 — 기존 코멘트의 곱셈/각주 * 가 서식으로 오인되지 않게 하는 방어다.
// 저장값은 늘 이 토큰이 섞인 평문이고, Excel·eval·챗봇으로 나갈 때는
// stripCommentFormat / web_report.comment_format.strip_format 이 본문만 남긴다.
const CMT_FMT_COLORS = { r: "red", o: "orange", g: "green", b: "blue" };
// 스타일 글자 → span class. 토큰이 아니면 null.
function cmtFmtClass(letter) {
  const s = String(letter == null ? "" : letter);
  if (s === "") return "cmt-b";
  const color = CMT_FMT_COLORS[s.toLowerCase()];
  if (!color) return null;
  return "cmt-" + color + (s === s.toUpperCase() ? " cmt-b" : "");
}

// comment 텍스트의 @[항목명]→Item_detail 링크, #[태그명]→Note 셀 앵커 링크,
// $[시트명]→Note 시트 링크, *[..]/*r[..]→서식 span 으로 변환 (그 외 텍스트는 전부 esc).
// 저장은 항상 @[..]/#[..]/$[..]/*[..] 평문으로(td.textContent), 표시할 때만 링크·색으로 보인다.
// #[..] 는 DATA.note_tags 에 없으면 .missing(삭제된 태그). $[..] 는 시트 목록이
// 도착한 뒤에만 .missing 판정한다(목록 미도착을 삭제로 오인하지 않게).
// ⚠️ 정규식은 반드시 이 함수 안에서 만든다 — g 플래그 정규식을 모듈 상수로 빼면
// engrLinkChips(map_select.js)가 자기 루프 안에서 이 함수를 부를 때 lastIndex 가 오염된다.
function linkifyComment(txt) {
  const s = String(txt == null ? "" : txt);
  let out = "", last = 0, m;
  const re = /([@#$])\[([^\]]+)\]|\*([A-Za-z]?)\[([^\]]+)\]/g;
  while ((m = re.exec(s))) {
    if (m[1] === undefined) {                 // 서식 토큰
      const cls = cmtFmtClass(m[3]);
      // 모르는 스타일 글자 — last 를 옮기지 않고 넘긴다(그 구간은 다음 esc(slice)에 흡수).
      if (cls === null) continue;
      out += esc(s.slice(last, m.index));
      out += `<span class="${cls}">${esc(m[4])}</span>`;
      last = m.index + m[0].length;
      continue;
    }
    out += esc(s.slice(last, m.index));
    const name = m[2];
    if (m[1] === "@") {
      out += `<span class="item-detail-link" data-subject="${esc(name)}">@${esc(name)}</span>`;
    } else if (m[1] === "$") {
      const sheets = (typeof noteSheetNames === "function") ? noteSheetNames() : null;
      const missing = !!sheets && !sheets.some(sh => sh.name === name);
      out += `<span class="note-sheet-link${missing ? " missing" : ""}" data-sheet-name="${esc(name)}"`
           + `${missing ? ' title="삭제되었거나 이름이 바뀐 Note 시트"' : ""}>$${esc(name)}</span>`;
    } else {
      const tags = (typeof DATA !== "undefined" && DATA && DATA.note_tags) || {};
      const missing = !Object.prototype.hasOwnProperty.call(tags, name);
      out += `<span class="note-tag-link${missing ? " missing" : ""}" data-tag="${esc(name)}"`
           + `${missing ? ' title="삭제되었거나 아직 만들어지지 않은 태그"' : ""}>#${esc(name)}</span>`;
    }
    last = m.index + m[0].length;
  }
  out += esc(s.slice(last));
  return out;
}

// 서식 토큰의 본문만 남긴다 — Excel·챗봇 등 평문 소비처로 나가기 직전에만 쓴다.
// @[..]/#[..]/$[..] 는 손대지 않는다(종전부터 원문 그대로 나가던 규약 유지).
// Python 짝은 web_report/comment_format.py strip_format — 둘의 문법이 같아야 한다.
function stripCommentFormat(txt) {
  return String(txt == null ? "" : txt).replace(
    /\*([A-Za-z]?)\[([^\]]+)\]/g,
    (all, st, body) => (cmtFmtClass(st) === null ? all : body));
}

// ── 서식 토큰 편집 (플로팅 툴바·단축키가 쓰는 순수 로직 — DOM 무의존) ──────────
// raw 안의 토큰 범위 목록. link=true 는 @[..]/#[..]/$[..], false 는 서식 토큰.
function cmtScanTokens(raw) {
  const s = String(raw == null ? "" : raw);
  const re = /([@#$])\[([^\]]+)\]|\*([A-Za-z]?)\[([^\]]+)\]/g;
  const out = [];
  let m;
  while ((m = re.exec(s))) {
    if (m[1] !== undefined) { out.push({ start: m.index, end: re.lastIndex, link: true }); continue; }
    if (cmtFmtClass(m[3]) === null) continue;   // 모르는 글자 = 토큰 아님
    out.push({ start: m.index, end: re.lastIndex, link: false, style: m[3], body: m[4] });
  }
  return out;
}

// 현재 스타일 글자 cur 에 action 을 적용한 다음 글자. null 이면 토큰 자체를 없앤다.
// action: "" = 굵기 토글 / "r"|"o"|"g"|"b" = 색 토글·교체.
// 같은 색을 다시 누르면 색만 빠지고 굵기는 남는다(축이 서로 독립).
function cmtNextStyle(cur, action) {
  cur = String(cur || "");
  const color = cur.toLowerCase();              // "" = 색 없음
  const bold = !cur || cur !== color;           // "" = 굵게만, 대문자 = 색+굵게
  if (action === "") return color ? (bold ? color : color.toUpperCase()) : (bold ? null : "");
  if (color === action) return bold ? "" : null;   // 같은 색 재클릭 → 색 해제
  return bold ? action.toUpperCase() : action;     // 색 교체(굵기 유지)
}

// 선택 구간 [start,end) 에 서식을 적용/변경/해제한 결과. 적용 불가면 null.
// action: "" = 굵게 / "r"|"o"|"g"|"b" = 색 / null = 서식 제거.
// 거부 조건 — 빈 선택 / 선택에 대괄호 포함 / 링크 토큰과 겹침 / 서식 토큰과 부분 겹침.
// (토큰 본문에 [ ] 를 넣으면 정규식 [^\]]+ 가 끊겨 표시가 깨지므로 아예 막는다.)
// 선택이 서식 토큰 하나에 **완전히 들어가면** 그 토큰 전체의 스타일 변경으로 해석한다(토글).
function cmtFormatRange(raw, start, end, action) {
  const s = String(raw == null ? "" : raw);
  let a = Math.max(0, Math.min(start, end)), b = Math.min(s.length, Math.max(start, end));
  while (a < b && /\s/.test(s[a])) a++;          // 선택 양끝 공백은 토큰 밖에 남긴다
  while (b > a && /\s/.test(s[b - 1])) b--;
  if (a >= b) return null;
  let host = null;
  for (const t of cmtScanTokens(s)) {
    if (b <= t.start || a >= t.end) continue;                       // 겹침 없음
    if (!t.link && a >= t.start && b <= t.end) { host = t; continue; }
    return null;                                 // 링크 토큰과 겹침 / 서식 토큰 부분 겹침
  }
  if (host) {
    const st = (action === null) ? null : cmtNextStyle(host.style, action);
    const rep = (st === null) ? host.body : `*${st}[${host.body}]`;
    return { text: s.slice(0, host.start) + rep + s.slice(host.end),
             caret: host.start + rep.length };
  }
  if (action === null) return null;              // 서식 없는 구간에서 '제거' → 할 일 없음
  const body = s.slice(a, b);
  if (body.indexOf("[") >= 0 || body.indexOf("]") >= 0) return null;
  // 색 버튼을 평문에 쓰면 색+굵게로 시작한다 — 좁은 표 셀(330px)에서 색만으로는 잘 안 보인다.
  // 굵기만 빼고 싶으면 이어서 Ctrl+B 를 누르면 된다(cmtNextStyle 이 색을 남긴다).
  const rep = `*${action === "" ? "" : action.toUpperCase()}[${body}]`;
  return { text: s.slice(0, a) + rep + s.slice(b), caret: a + rep.length };
}

// Issue Table Bin 미니셀 Map 소스(웨이퍼) 개수 — 2개 이상일 때만 ⤢(전 소스 보기) 노출.
// 실제로 그릴 목록과 같은 것을 세야 한다(Temperature 는 RT 만 — wafer_charts.issueBinMaps).
function mapSourceCount() {
  return issueBinMaps().length;
}

// 입력 소스(파일) 개수 — CPK 행 STDF 미니맵은 소스별 측정값이라 이 수로 펼치기를 판단한다
// (Map Analysis 맵 개수는 STEP 분리로 소스 수보다 많을 수 있어 mapSourceCount 와 별개).
function webReportSourceCount() {
  const srcs = DATA.web_report && DATA.web_report.sources;
  return Array.isArray(srcs) ? srcs.length : 0;
}

// Temperature CT/HT 소스 개수 — Temp 미니셀 ⤢(전 소스 보기) 노출 판단용.
// RT 는 Temp 판정 대상이 아니므로 전체 소스 수를 쓰면 CT/HT 가 1개인 세션에도 버튼이 뜬다.
function tempSourceCount() {
  const srcs = (DATA.web_report && DATA.web_report.sources) || [];
  return srcs.filter(s => s && (s.temp_corner === "CT" || s.temp_corner === "HT")).length;
}

// 열 순서 보정: 고정 prefix 컬럼(step/bin/tno/item, issue 는 category 포함 avg 까지)
// → 지정 순서로 맨 앞에 배치(대소문자 무시), comment 류 → 최우측. 나머지 상대순서 유지.
// yield: source 별 yield 값 → source 별 _cnt/_count → avg 순으로 그루핑.
function orderColumns(cols, kind) {
  const isComment = c => /comment/i.test(String(c));

  const comments = cols.filter(isComment);
  // Issue Table 은 Category 컬럼을 화면에 표시하지 않는다(섹션 구분은 상단 고정 헤더 라벨이 담당).
  // 단 rows 의 Category 데이터 필드는 섹션 판정(rowSection)에 그대로 쓰이므로 여기서 컬럼만 뺀다.
  let rest = cols.filter(c => !isComment(c)
    && !(kind === "issue" && String(c).trim().toLowerCase() === "category")
    && !/^_(grp|detail|ndetail)$/.test(String(c)));   // 토글 전용 내부 마킹 필드 제외

  // source(={src}_yield 컬럼 수)가 1개면 avg 는 그 source 값과 동일해 의미가 없으므로
  // Yield/Issue 표에서 avg 컬럼을 표시하지 않는다(행 데이터의 avg 값은 그대로 유지 —
  // Issue Table CPK subhead 감지가 avg 값에 의존). 2개 이상(compare/DUT 포함)이면 표시.
  const singleSource = rest.filter(c => /_yield$/i.test(String(c))).length <= 1;

  const PREFIX_ORDER = {
    yield: ["step", "bin", "tno", "item"],
    issue: ["step", "bin", "tno", "item"],
  };
  const prefix = PREFIX_ORDER[kind];
  if (prefix) {
    const lower = c => String(c).trim().toLowerCase();
    const front = [];
    prefix.forEach(p => {
      const idx = rest.findIndex(c => lower(c) === p);
      if (idx !== -1) front.push(rest.splice(idx, 1)[0]);
    });
    rest = front.concat(rest);
  }

  if (kind === "yield") {
    // 순서: 식별(step/bin/tno/item) → avg → source별 yield → source별 count
    // (2026-08-07 사용자 요청 — 종전에는 avg 가 맨 끝이었다). avg 가 _yield 그룹 바로
    // 앞이라 2행 헤더에서는 "yield" 그룹 아래 Avg 열로 흡수된다(buildSheetTableHead).
    const isCntCol = c => /_(cnt|count)$/i.test(String(c));
    const isAvgCol = c => String(c).trim().toLowerCase() === "avg";
    const isYieldCol = c => /_yield$/i.test(String(c));
    const idCols = rest.filter(c => !isCntCol(c) && !isAvgCol(c) && !isYieldCol(c));
    const yieldVals = rest.filter(isYieldCol);
    const cntVals = rest.filter(isCntCol);
    const avgVals = singleSource ? [] : rest.filter(isAvgCol);
    rest = idCols.concat(avgVals).concat(yieldVals).concat(cntVals);
  }

  if (kind === "issue") {
    // 식별컬럼 뒤: Map → Distribution → Avg → source별 yield → Status 순.
    // (Avg 는 yield 그룹 헤더 아래로 묶임. Status 는 좌측 sticky 6컬럼 뒤 comment 앞 —
    //  좌측 고정 블록 폭 계산(syncIssueStickyOffsets)에 영향 주지 않는 위치.)
    const isMap = c => String(c).trim().toLowerCase() === "map";
    const isDist = c => String(c).trim().toLowerCase() === "distribution";
    const isAvgCol = c => String(c).trim().toLowerCase() === "avg";
    const isYieldCol = c => /_yield$/i.test(String(c));
    const isStatus = c => String(c).trim().toLowerCase() === "status";
    const map = rest.filter(isMap);
    const dist = rest.filter(isDist);
    const avg = singleSource ? [] : rest.filter(isAvgCol);
    const yields = rest.filter(isYieldCol);
    const status = rest.filter(isStatus);
    const others = rest.filter(c => !isMap(c) && !isDist(c) && !isAvgCol(c) && !isYieldCol(c) && !isStatus(c));
    rest = others.concat(map).concat(dist).concat(avg).concat(yields).concat(status);
  }

  return rest.concat(comments);
}

// 빈 헤더 placeholder(_colN)는 화면엔 빈칸으로
function headerLabel(c) { return /^_col\d+$/.test(String(c)) ? "" : c; }

// _yield/_count 처럼 소스별로 반복되는 컬럼이 연속 2개 이상 묶이면 상단에 "yield"/"count"
// 병합 헤더 + 하단에 접미사를 뗀 소스 짧은 이름을 보여주는 2행 헤더를 만든다.
// 그런 묶음이 없으면(소스 1개뿐이거나 다른 시트) 기존과 동일한 1행 헤더를 그대로 렌더.
const SHEET_HEADER_SUFFIX_GROUPS = [
  { re: /_yield$/i, label: "yield" },
  { re: /_(cnt|count)$/i, label: "count" },
];
function sheetHeaderShortLabel(c) {
  return headerLabel(c).replace(/_(yield|cnt|count)$/i, "");
}
// 화면 표시용 헤더 라벨 (avg → Avg 등).
// COLUMN_DISPLAY_ALIAS: 저장 키(= 편집 DB·eval export·클라 Excel 이 쓰는 컬럼명)는 그대로 두고
// 화면/Excel 내보내기 헤더 표기만 바꾼다 — 키를 바꾸면 기존 세션의 저장된 comment 가 유실된다.
const COLUMN_DISPLAY_ALIAS = { "개발 comment": "개발팀 Comment" };
function displayLabel(c) {
  const n = String(c).trim().toLowerCase();
  if (n === "avg") return "Avg";
  const alias = COLUMN_DISPLAY_ALIAS[String(c).trim()];
  return alias !== undefined ? alias : headerLabel(c);
}

// ── source 헤더 라벨 축약 (사용자 요청 2026-07-21) ────────────────────────────
// source 컬럼이 이 수 이상이면 헤더에서 공통 부분을 생략하고 서로 다른 부분만 보여준다.
// 예: kucak_01 … kucak_11 → 첫 컬럼만 "kucak_01" 전체, 나머지는 "02" … "11".
const SRC_ABBREV_MIN = 8;
// 이 길이 이하의 소스명은 source 가 아무리 많아도 축약하지 않는다 — "CT_01" 같은 5자리
// 이름에서 공통부분을 떼면 한두 글자만 남아 무엇인지 알아볼 수 없다(사용자 요청 2026-08-06).
const SRC_KEEP_FULL_LEN = 5;

// source 컬럼 폭을 "숫자 크기"까지 좁히기 시작하는 source 개수. source 가 2개 이상이면 헤더가
// 축약(abbrevSourceLabels "…"+뒤 6글자 / SRC_ABBREV_MIN 이상이면 공통부분 제거)되므로, 폭 힌트를
// 낮춰 실제 폭이 max(축약 라벨, xx.xx) 로 정해지게 한다 → 소스명이 짧을수록 열이 좁아져 가로
// 스크롤이 줄어든다(사용자 요청 2026-08-04). 소스 1개는 헤더가 전체 이름이라 기존 폭 유지.
const SRC_NARROW_MIN = 2;

// 이름 목록의 공통 접두/접미 길이(문자 수). 이름이 1개 이하면 축약 대상 아님.
function commonAffixLen(names) {
  if (!names || names.length < 2) return { pre: 0, suf: 0 };
  const first = names[0];
  let pre = first.length;
  names.forEach(n => {
    let i = 0;
    while (i < pre && i < n.length && n[i] === first[i]) i++;
    pre = i;
  });
  let suf = first.length - pre;
  names.forEach(n => {
    const cap = Math.min(suf, n.length - pre);
    let i = 0;
    while (i < cap && n[n.length - 1 - i] === first[first.length - 1 - i]) i++;
    suf = i;
  });
  return { pre, suf };
}

// source 전체 이름 목록 → [{short, full}]. SRC_ABBREV_MIN 미만이면 전부 전체 이름 그대로.
// 이상이면 첫 컬럼만 전체 이름이고 나머지는 공통 접두/접미를 뗀 부분만 남긴다(빈 문자열이
// 되면 전체 이름으로 폴백 — 이름이 전부 같은 경우).
// 단 SRC_KEEP_FULL_LEN 이하로 짧은 이름은 축약해도 얻는 폭이 없어 전체 이름을 유지한다.
function sourceHeaderLabels(fulls) {
  const names = (fulls || []).map(f => String(f));
  if (names.length < SRC_ABBREV_MIN) return names.map(f => ({ short: f, full: f }));
  const { pre, suf } = commonAffixLen(names);
  return names.map((full, i) => {
    if (i === 0 || full.length <= SRC_KEEP_FULL_LEN) return { short: full, full };
    const core = full.slice(pre, full.length - suf);
    return { short: core || full, full };
  });
}

// 표의 source 컬럼({src}_yield) 수 — 축약·narrow 폭 판단 기준(Yield/Issue 공용).
function sourceColCount(cols) {
  return (cols || []).filter(c => /_yield$/i.test(String(c))).length;
}
// source 이름을 "…" + 이름 끝 6글자로 축약한다 (소스 이름은 보통 끝부분에서 달라지므로
// 뒤를 남긴다). **6글자를 넘으면 무조건 축약**하고 source 가 1개뿐이어도 적용한다
// (사용자 요청 2026-08-06 — 종전에는 2개 이상 + 8글자 초과일 때만이라 단일 소스 세션에서
// 12자 이름이 그대로 나왔다). 전체 이름은 hover(title)로 확인한다.
const SRC_HEAD_TAIL_LEN = 6;
function abbrevSourceName(full) {
  const s = String(full);
  return (s.length <= SRC_HEAD_TAIL_LEN) ? s : "…" + s.slice(-SRC_HEAD_TAIL_LEN);
}
function abbrevSourceLabels(fulls) {
  return (fulls || []).map(full => ({ short: abbrevSourceName(full), full: String(full) }));
}
// source 가 1개면 {src}_yield / {src}_count 가 각각 run 길이 1 이라 2행 헤더가 만들어지지
// 않는다 → 1행 헤더 경로에서는 종전에 컬럼 키가 통째로 노출됐다("PMIC_LOT1_RT_yield").
// 접미사(_yield/_count)는 열의 의미라 남기고 **source 이름 부분만** 축약한다.
// 반환 null = source 컬럼이 아님(호출부가 기존 displayLabel 사용).
function flatSourceHeadLabel(c) {
  const raw = String(c);
  const m = raw.match(/_(yield|cnt|count)$/i);
  if (!m) return null;
  const full = sheetHeaderShortLabel(c);
  const short = abbrevSourceName(full);
  return { short: short + m[0], full: raw, abbreviated: short !== full };
}

// opts.resize=true 면 단일 컬럼 th 우측에 폭 드래그 핸들을 심는다(colgroup 인덱스 동반).
// Yield 표에서 쓰며(bindSheetColResize), 지정 없으면 기존과 동일한 헤더를 그대로 낸다.
function buildSheetTableHead(cols, opts) {
  opts = opts || {};
  const handle = opts.resize
    ? idx => `<span class="col-resize-handle" data-col="${idx}" data-col-name="${esc(String(cols[idx]))}"></span>`
    : () => "";
  const isAvgCol = c => String(c).trim().toLowerCase() === "avg";
  // avg 컬럼이 _yield 그룹 바로 앞에 오면 그 yield 그룹에 흡수(= "yield" 헤더 아래 "Avg" 열).
  const groupOf = c => SHEET_HEADER_SUFFIX_GROUPS.find(g => g.re.test(String(c)));
  const groupKeyAt = i =>
    groupOf(cols[i]) ||
    ((isAvgCol(cols[i]) && i + 1 < cols.length && /_yield$/i.test(String(cols[i + 1])))
      ? SHEET_HEADER_SUFFIX_GROUPS[0] : null);
  const runs = [];
  for (let i = 0; i < cols.length; ) {
    const g = groupKeyAt(i);
    let j = i + 1;
    if (g) { while (j < cols.length && groupKeyAt(j) === g) j++; }
    runs.push({ start: i, len: j - i, group: (g && (j - i) >= 2) ? g : null });
    i = j;
  }
  const commentCls = c => isCommentCol(c) ? ` class="st-comment"` : "";
  if (!runs.some(r => r.group)) {
    return "<thead><tr>" + cols.map((c, k) => {
      const s = flatSourceHeadLabel(c);
      if (s) {
        return `<th class="sheet-src-th"${s.abbreviated ? ` title="${esc(s.full)}"` : ""}>` +
          `${esc(s.short)}${handle(k)}</th>`;
      }
      return `<th${commentCls(c)}>${esc(displayLabel(c))}${handle(k)}</th>`;
    }).join("") + "</tr></thead>";
  }
  const topRow = runs.map(r => r.group
    ? `<th colspan="${r.len}" class="sheet-group-th">${esc(r.group.label)}</th>`
    : `<th rowspan="2"${commentCls(cols[r.start])}>${esc(displayLabel(cols[r.start]))}${handle(r.start)}</th>`
  ).join("");
  const botRow = runs.filter(r => r.group).map(r => {
    const runCols = cols.slice(r.start, r.start + r.len);
    // source 컬럼만 뽑아 축약(avg 는 "Avg" 고정 라벨). source 가 SRC_ABBREV_MIN 이상이면
    // 공통 접두/접미를 뗀 라벨(첫 컬럼만 전체), 미만이면 기존 뒤글자 축약.
    const srcCols = runCols.filter(c => !isAvgCol(c));
    const fulls = srcCols.map(sheetHeaderShortLabel);
    const abbr = srcCols.length >= SRC_ABBREV_MIN
      ? sourceHeaderLabels(fulls) : abbrevSourceLabels(fulls);
    const abbrByCol = {};
    srcCols.forEach((c, i) => { abbrByCol[c] = abbr[i]; });
    return runCols.map((c, k) => {
      const idx = r.start + k;
      if (isAvgCol(c)) return `<th>Avg${handle(idx)}</th>`;
      const a = abbrByCol[c];
      const titleAttr = a.short !== a.full ? ` title="${esc(a.full)}"` : "";
      return `<th class="sheet-src-th"${titleAttr}>${esc(a.short)}${handle(idx)}</th>`;
    }).join("");
  }).join("");
  return `<thead><tr>${topRow}</tr><tr>${botRow}</tr></thead>`;
}

// 반복 섹션 헤더 행(yield의 P1/P2/P3 구간별 헤더) 감지: 셀 값이 자기 컬럼명과
// 동일한 비율이 높으면 "헤더가 데이터로 들어간" 행으로 판단.
function isHeaderLikeRow(r, cols) {
  if (!r) return false;
  let total = 0, matches = 0;
  for (const c of cols) {
    const name = String(c).trim().toLowerCase();
    if (!name || /^_col\d+$/.test(name)) continue;
    total++;
    const v = r[c];
    const txt = (v === null || v === undefined) ? "" : String(v).trim().toLowerCase();
    if (txt === name) matches++;
  }
  return total > 0 && (matches / total) >= 0.6;
}

// issue_table 의 CPK 카테고리 전환 행("item name" / "cpk" 서브헤더) 감지.
function isCpkSubheadRow(r) {
  if (!r) return false;
  const cat = String(r["Category"] ?? "").trim().toLowerCase();
  const avg = String(r["avg"] ?? "").trim().toLowerCase();
  return cat === "cpk" && avg === "cpk";
}

// yield 행 정렬(보기 전용): step 별 섹션(반복 헤더 행으로 구분) 안에서
// Bin1(PASS) 행을 맨 위에 두고, 나머지는 avg 내림차순. 합계(Sum) 행은 섹션 끝 유지.
function reorderYieldRows(rows, cols) {
  if (!rows || rows.length < 2) return rows || [];
  const binCol = cols.find(c => String(c).trim().toLowerCase() === "bin");
  const avgCol = cols.find(c => String(c).trim().toLowerCase() === "avg");
  if (!binCol || !avgCol) return rows;

  const firstCol = cols[0];
  const isPassBin = r => String((r ? r[binCol] : "") ?? "").trim() === "1";
  const isSumRow = r => String((r ? r[firstCol] : "") ?? "").trim().toLowerCase() === "sum";

  const out = [];
  let section = [];
  const flushSection = () => {
    if (!section.length) return;
    const sumRows = section.filter(isSumRow);
    const dataRows = section.filter(r => !isSumRow(r));
    const passRows = dataRows.filter(isPassBin);
    const restRows = dataRows.filter(r => !isPassBin(r))
      .slice()
      .sort((a, b) => (parseFloat(b[avgCol]) || 0) - (parseFloat(a[avgCol]) || 0));
    out.push(...passRows, ...restRows, ...sumRows);
    section = [];
  };

  rows.forEach(r => {
    if (isHeaderLikeRow(r, cols)) {
      flushSection();
      out.push(r);
    } else {
      section.push(r);
    }
  });
  flushSection();
  return out;
}

/**
 * rows(list of dict) → HTML sheet-table.
 * opts: { edit, kind:"yield"|"issue", columns:[...] }
 *  - columns: 명시적 열 목록(있으면 derive·정렬 생략, 순서 그대로).
 *    데이터행이 없어도 헤더는 항상 렌더 → 내용 없는 컬럼도 유지.
 *  - kind: comment 최우측 / (issue) category 최좌측 정렬.
 */
// Issue Table comment 저장용 행 식별 키 — 백엔드 web_report/tabs/issue_table.py 의
// manifest.issue_comments 키 규칙과 반드시 동일해야 한다:
// Yield 행 "Yield|<bin>|<item>", CPK 데이터 행 "CPK|<item>", TEMP 행 "TEMP|<item>"
// (Temperature 모드 전용), ETC 데이터 행 "ETC|<item>".
function issueRowKey(r, section) {
  const item = String((r && r["Item"]) ?? "");
  if (!item.trim()) return "";
  if (section === "Yield") return `Yield|${String((r && r["Bin"]) ?? "")}|${item}`;
  if (section === "CPK") return `CPK|${item}`;
  if (section === "TEMP") return `TEMP|${item}`;
  if (section === "ETC") return `ETC|${item}`;
  return "";
}

// Issue Table 행 숨김/Status 용 이슈 단위 키 — 백엔드 edits.py KIND_ISSUE_HIDDEN/
// KIND_ISSUE_STATUS 규약과 반드시 동일해야 한다: Yield 는 bin 단위 "Yield|<bin>"
// (대표행에만 부여 — 상세행/Pass 행 제외), CPK 행 "CPK|<item>", TEMP 행 "TEMP|<item>",
// ETC 행 "ETC|<item>".
function issueHideStatusKey(r, section) {
  const item = String((r && r["Item"]) ?? "").trim();
  if (section === "Yield") {
    const bin = String((r && r["Bin"]) ?? "").trim();
    return (bin && bin !== "1" && r && r._grp && !r._detail) ? `Yield|${bin}` : "";
  }
  if (section === "CPK") return item ? `CPK|${item}` : "";
  if (section === "TEMP") return item ? `TEMP|${item}` : "";
  if (section === "ETC") return item ? `ETC|${item}` : "";
  return "";
}

// Issue Table 섹션별 2행 헤더 블록. 컬럼 구조(식별/Distribution/Avg+source/comment)는 세 섹션이
// 동일하고, 그룹 라벨(yield/cpk/etc)과 Avg 라벨(Avg↔cpk)만 섹션마다 다르다. buildSheetTableHead
// 의 run 로직을 그대로 따르되 라벨만 섹션값으로 채워 <tr.issue-shead-top>/<tr.issue-shead-bot>
// 두 줄을 만든다(둘 다 sticky top 으로 통째 고정).
const ISSUE_SECTION_LABELS = {
  Yield: { group: "yield", avg: "Avg" },
  CPK:   { group: "cpk",   avg: "cpk" },
  TEMP:  { group: "temp",  avg: "Avg" },   // Temperature 모드 전용 (RT limit 이탈 항목)
  ETC:   { group: "etc",   avg: "Avg" },
};
function issueSectionHeadRowsHtml(cols, sec) {
  const lab = ISSUE_SECTION_LABELS[sec] || ISSUE_SECTION_LABELS.Yield;
  const isAvgCol = c => String(c).trim().toLowerCase() === "avg";
  // 컬럼 폭 드래그 리사이즈 핸들 — 단일 컬럼 th 우측 경계에 붙여 그 col 인덱스를 나른다
  // (그룹 라벨 colspan th 는 제외). colgroup.children[idx] 와 1:1 대응(bindIssueColResize).
  const resizeHandle = idx =>
    `<span class="col-resize-handle" data-col="${idx}" data-col-name="${esc(String(cols[idx]))}"></span>`;
  // Yield 섹션 헤더의 Step 열 아래 작은 ▼ = 그 표의 Bin 그룹 TNO 전체 펼치기/접기
  // (2026-08-10 사용자 요청 — 종전 툴바 'TNO 전체 펼치기' 버튼을 여기로 옮겼다).
  // 핸들러는 종전 그대로 edit_mode.js 의 data-issue-act="toggle-all" 위임을 탄다.
  // Bin 그룹이 있는 섹션은 Yield(TNO 묶음)와 TEMP(2026-08-11 — Bin 별 항목 묶음) 둘이다.
  // CPK/ETC 는 그룹이 없어 달지 않는다.
  const toggleAllBtn = idx => ((sec === "Yield" || sec === "TEMP")
      && String(cols[idx]).trim().toLowerCase() === "step")
    ? `<button type="button" class="issue-toggle-all" data-issue-act="toggle-all" ` +
      `data-expanded="false" title="${sec === "TEMP" ? "Bin 전체 펼치기" : "TNO 전체 펼치기"}">▼</button>` : "";
  const groupOf = c => SHEET_HEADER_SUFFIX_GROUPS.find(g => g.re.test(String(c)));
  const groupKeyAt = i =>
    groupOf(cols[i]) ||
    ((isAvgCol(cols[i]) && i + 1 < cols.length && /_yield$/i.test(String(cols[i + 1])))
      ? SHEET_HEADER_SUFFIX_GROUPS[0] : null);
  const runs = [];
  for (let i = 0; i < cols.length; ) {
    const g = groupKeyAt(i);
    let j = i + 1;
    if (g) { while (j < cols.length && groupKeyAt(j) === g) j++; }
    runs.push({ start: i, len: j - i, group: (g && (j - i) >= 2) ? g : null });
    i = j;
  }
  const commentCls = c => isCommentCol(c) ? ` class="st-comment"` : "";
  if (!runs.some(r => r.group)) {
    return `<tr class="issue-shead-top" data-sec="${esc(sec)}">` +
      cols.map((c, k) => {
        const s = flatSourceHeadLabel(c);   // source 1개 세션 — 컬럼 키 노출 방지(Yield 와 동일 규칙)
        if (s) {
          return `<th class="sheet-src-th"${s.abbreviated ? ` title="${esc(s.full)}"` : ""}>` +
            `${esc(s.short)}${resizeHandle(k)}</th>`;
        }
        return `<th${commentCls(c)}>${esc(displayLabel(c))}${resizeHandle(k)}${toggleAllBtn(k)}</th>`;
      }).join("") + `</tr>`;
  }
  const topRow = runs.map(r => r.group
    ? `<th colspan="${r.len}" class="sheet-group-th">${esc(lab.group)}</th>`
    : `<th rowspan="2"${commentCls(cols[r.start])}>${esc(displayLabel(cols[r.start]))}` +
      `${resizeHandle(r.start)}${toggleAllBtn(r.start)}</th>`
  ).join("");
  const botRow = runs.filter(r => r.group).map(r => {
    const runCols = cols.slice(r.start, r.start + r.len);
    // source 이름 축약은 Yield 탭(buildSheetTableHead)과 같은 규칙을 쓴다 —
    // SRC_ABBREV_MIN 이상이면 공통 접두/접미를 뗀 라벨(첫 컬럼만 full), 미만이면
    // ".."+뒤 6글자 축약. hover(title)로 전체 이름을 볼 수 있다.
    const srcCols = runCols.filter(c => !isAvgCol(c));
    const fulls = srcCols.map(sheetHeaderShortLabel);
    const labels = srcCols.length >= SRC_ABBREV_MIN
      ? sourceHeaderLabels(fulls) : abbrevSourceLabels(fulls);
    const labByCol = {};
    srcCols.forEach((c, i) => { labByCol[c] = labels[i]; });
    return runCols.map((c, k) => {
      const idx = r.start + k;
      if (isAvgCol(c)) return `<th>${esc(lab.avg)}${resizeHandle(idx)}</th>`;
      const a = labByCol[c];
      return `<th class="sheet-src-th" title="${esc(a.full)}">${esc(a.short)}${resizeHandle(idx)}</th>`;
    }).join("");
  }).join("");
  return `<tr class="issue-shead-top" data-sec="${esc(sec)}">${topRow}</tr><tr class="issue-shead-bot">${botRow}</tr>`;
}

// Issue Table Yield 섹션: 각 Bin 그룹의 첫 상세행(= most-fail TNO)은 대표행이 같은 Item 을
// 제목으로 이미 보여주므로 중복이라 표시에서 뺀다(2026-08-07 사용자 요청 — Yield 탭
// renderYieldTable 의 slice(2)와 같은 규칙). 대표행 _ndetail 을 함께 줄여 남은 상세가 없으면
// ▼ 토글도 사라진다. 서버 payload(캐시 포함)는 그대로 두고 표시 직전에만 거른다 —
// comment 키는 대표행과 동일(Yield|bin|item)이라 지워도 편집·표시 유실이 없다.
function dropIssueMostFailDetailRows(rows) {
  const repByGrp = {};
  const seen = new Set();   // 첫 상세행을 이미 제거한 _grp
  const out = [];
  (rows || []).forEach(r => {
    if (r && r._grp && !r._detail) {
      const cp = Object.assign({}, r);   // _ndetail 을 고치므로 원본 불변 사본
      repByGrp[r._grp] = cp;
      out.push(cp);
      return;
    }
    if (r && r._grp && r._detail && !seen.has(r._grp)) {
      seen.add(r._grp);
      const rep = repByGrp[r._grp];
      // 정렬 규약상 첫 상세행 = 대표행과 같은 most-fail Item. 혹시 어긋나면 지우지 않는다.
      if (rep && String(rep.Item ?? "") === String(r.Item ?? "")) {
        rep._ndetail = Math.max(0, (Number(rep._ndetail) || 0) - 1);
        return;
      }
    }
    out.push(r);
  });
  return out;
}

function renderSheetTable(rows, opts) {
  opts = opts || {};
  let cols;
  if (opts.columns && opts.columns.length) {
    cols = opts.columns.slice();
  } else {
    if (!rows || !rows.length) return "";
    cols = [];
    rows.forEach(r => Object.keys(r || {}).forEach(k => { if (!cols.includes(k)) cols.push(k); }));
    if (opts.kind) cols = orderColumns(cols, opts.kind);
  }
  if (!cols.length) return "";

  let bodyRows = rows || [];
  // Bin 그룹 마킹(_grp)이 실린 표는 그 순서 자체가 "대표행 + 그 아래 접힌 상세행" 이라
  // avg 내림차순 재정렬을 걸면 대표·상세가 흩어진다 (Yield 탭 Temp Corner — 서버
  // tabs/temp_fail._group_by_bin 이 이미 정렬해 보낸다).
  const hasBinGroups = (bodyRows || []).some(r => r && r._grp);
  if (opts.kind === "yield" && !opts.edit && !hasBinGroups) bodyRows = reorderYieldRows(bodyRows, cols);
  if (opts.kind === "issue") bodyRows = dropIssueMostFailDetailRows(bodyRows);
  const binCol = opts.kind === "yield" ? cols.find(c => String(c).trim().toLowerCase() === "bin") : null;

  // source 가 2개 이상이면 헤더가 축약 라벨이 되므로 그 컬럼 폭 힌트도 함께 낮춘다.
  const narrowSrc = sourceColCount(cols) >= SRC_NARROW_MIN;
  const colgroup = "<colgroup>" + cols.map(c =>
    `<col style="width:${colWidth(c, opts.kind, narrowSrc)}">`
  ).join("") + "</colgroup>";

  // Issue 는 persistent thead 대신 섹션(Yield/CPK/ETC)별 2행 헤더 블록을 tbody 안에 sticky 로
  // 심어 스크롤 시 헤더가 통째로 교체되게 한다 → 여기선 상단 thead 를 만들지 않는다.
  const head = (opts.kind === "issue") ? "" : buildSheetTableHead(cols);

  // Issue Table 의 Category 는 화면 컬럼으로 렌더하지 않는다(섹션 식별은 2행 헤더 블록이 담당).
  // 각 행이 속한 섹션(Yield/CPK/ETC) — Category 데이터 필드가 비어있는 상세 행은 바로 위
  // 헤더 행의 섹션을 상속한다. Distribution 자동표시(Yield/ETC/CPK) 판단, ETC 상세행 삭제버튼,
  // 섹션 경계에서의 헤더 블록 삽입 판단에 쓰인다. edit 모드에서도 필요해 조건 없이 계산.
  const rowSection = {};
  if (opts.kind === "issue") {
    let sec = "";
    for (let i = 0; i < bodyRows.length; i++) {
      const cat = (bodyRows[i] && bodyRows[i]["Category"]) || "";
      if (cat) sec = cat;
      rowSection[i] = sec;
    }
  }

  // Issue Table CPK 섹션 연빨강 강조(cpk < 1.33)는 다중 source(=_yield 소스 컬럼 2개 이상)일
  // 때만(단일 소스는 cpk 값이 avg 와 동일해 중복). Yield/ETC 섹션 빨강 그라데이션은 소스 수와
  // 무관하게 Yield 탭과 동일하게 적용한다(아래 issueYieldColMax).
  const issueMultiSource = opts.kind === "issue"
    && cols.filter(c => /_yield$/i.test(String(c))).length > 1;

  // Yield/ETC fail yield 셀 빨강 그라데이션의 기준값 = 각 source 컬럼 내 최대 fail yield(>0).
  // 값이 클수록 진한 빨강(--yw 1 에 가까움). 컬럼별로 나눠 정규화한다. Pass(Bin1) 행·CPK 섹션 제외.
  // opts.grad=true 면 kind:"yield" 표(Yield 탭 Temp Corner)에도 같은 규칙을 적용하고 avg 열까지
  // 포함한다 — Yield 표(renderYieldTable)와 같은 음영을 쓴다(사용자 요청 2026-08-06).
  const gradYield = opts.kind === "issue" || !!opts.grad;
  const isGradCol = c => /_yield$/i.test(String(c))
    || (!!opts.grad && String(c).trim().toLowerCase() === "avg");
  const issueYieldColMax = {};
  if (gradYield) {
    bodyRows.forEach((r, ri) => {
      if (isCpkSubheadRow(r)) return;
      if (opts.kind === "issue") {
        const sec = rowSection[ri];
        if (sec !== "Yield" && sec !== "ETC" && sec !== "TEMP") return;
      }
      if (String((r && (r["Bin"] ?? r["bin"])) ?? "").trim() === "1") return;   // Pass 행 제외
      cols.forEach(c => {
        if (!isGradCol(c)) return;
        const n = parseFloat(r ? r[c] : "");
        if (!isNaN(n) && n > (issueYieldColMax[c] || 0)) issueYieldColMax[c] = n;
      });
    });
  }

  const renderDataRowTr = (r, ri) => {
    const subhead = (opts.kind === "yield" && isHeaderLikeRow(r, cols))
      || (opts.kind === "issue" && isCpkSubheadRow(r));
    // 이 행이 속한 섹션(Issue Table 전용) — Map/Distribution 셀 표시 판단·셀 강조에 쓰인다.
    const issueRowSec = opts.kind === "issue" ? rowSection[ri] : "";
    // Yield 섹션 최상단 Pass(Bin1) 행 — Map/Distribution/빨강강조 제외, 초록 Pass 스타일.
    const issuePassRow = opts.kind === "issue" && !subhead
      && String((r && (r["Bin"] ?? r["bin"])) ?? "").trim() === "1";
    // 삭제 대상 키 — Yield 대표행/CPK 행은 숨김 키, ETC 상세행은 item 명. 행 단위로 한 번만
    // 계산해 첫 컬럼(Step) 체크박스와 Item 셀 개별 삭제(×) 버튼이 같은 기준을 쓰게 한다.
    const rowItemTxt = String((r && r["Item"]) ?? "").trim();
    const delHideKey = (opts.kind === "issue" && opts.edit && !subhead && !issuePassRow
      && ((issueRowSec === "Yield" && r && r._grp && !r._detail)
        || (issueRowSec === "CPK" && rowItemTxt !== "")
        // TEMP 행(Issue Table Temp 탭)도 item 단위로 숨긴다 — 키는 TEMP|<item>.
        || (issueRowSec === "TEMP" && rowItemTxt !== "")))
      ? issueHideStatusKey(r, issueRowSec) : "";
    const delEtcItem = (opts.kind === "issue" && opts.edit && issueRowSec === "ETC"
      && String((r && r["Category"]) || "") === "" && rowItemTxt !== "") ? rowItemTxt : "";
    const tds = cols.map((c, ci) => {
      const v = r ? r[c] : "";
      let txt = (v === null || v === undefined) ? "" : String(v);
      if (subhead && txt.trim().toLowerCase() === "cpk") txt = "CPK";
      const isEmpty = txt === "";
      const isNum = isNumVal(v);
      // Map 열: 해당 행 Bin 만 원색·나머지 회색·숫자 제거한 웨이퍼(있는 Yield/ETC 행). 없으면 빈 칸.
      if (isMapCol(c)) {
        const binv = r && (r["Bin"] ?? r["bin"]);
        const hasBin = String(binv ?? "").trim() !== "";
        if (opts.kind === "issue" && !subhead && hasBin && !issuePassRow
          && (issueRowSec === "Yield" || issueRowSec === "ETC")) {
          const expandBtn = (mapSourceCount() > 1)
            ? `<button type="button" class="btn-map-expand" title="전체 소스 맵 보기">⤢</button>` : "";
          return `<td data-r="${ri}" data-c="${ci}">` +
            `<div class="map-cell map-cell-mini" data-bin="${esc(String(binv))}" ` +
            `title="클릭하면 Map Analysis 탭에서 이 Bin 을 강조해 봅니다"><div class="map-plot"></div>${expandBtn}</div></td>`;
        }
        // TEMP 섹션(Issue Table Temp 탭)은 항목별 fail die 를 강조한 미니맵을 넣는다.
        // Bin 은 있지만 그 bin 의 die 가 아니라 "이 항목을 벗어난 die" 를 보여야 한다.
        const tempItem = String((r && r["Item"]) ?? "").trim();
        if (opts.kind === "issue" && !subhead && issueRowSec === "TEMP" && tempItem !== "") {
          // ⤢ 는 CT/HT 소스가 2개 이상일 때만 — 실제로 그 항목이 fail 난 소스만
          // 나열되므로(openTempExpand) 여기서는 상한 판단만 한다.
          const tempExpand = (tempSourceCount() > 1)
            ? `<button type="button" class="btn-map-expand" title="이 항목이 fail 난 CT/HT 소스 맵 전체 보기">⤢</button>` : "";
          return `<td data-r="${ri}" data-c="${ci}">` +
            `<div class="map-cell map-cell-mini map-cell-temp" data-temp-item="${esc(tempItem)}" ` +
            `title="클릭하면 Map Analysis 탭 Temperature Map 축에서 이 항목을 강조해 봅니다"><div class="map-plot"></div>${tempExpand}</div></td>`;
        }
        // CPK 섹션은 Bin 이 없다 — 대신 그 Item 의 STDF Map(측정값 10분위) 미니맵을 넣는다.
        const cpkItem = String((r && r["Item"]) ?? "").trim();
        if (opts.kind === "issue" && !subhead && issueRowSec === "CPK" && cpkItem !== "") {
          const stdfExpand = (webReportSourceCount() > 1)
            ? `<button type="button" class="btn-map-expand" title="전체 소스 맵 보기">⤢</button>` : "";
          return `<td data-r="${ri}" data-c="${ci}">` +
            `<div class="map-cell map-cell-mini map-cell-stdf" data-subject="${esc(cpkItem)}" ` +
            `title="클릭하면 Map Analysis 탭 STDF Map 으로 이 Item 을 봅니다"><div class="map-plot"></div>${stdfExpand}</div></td>`;
        }
        return `<td class="st-empty${subhead ? " sheet-subhead" : ""}" data-r="${ri}" data-c="${ci}"></td>`;
      }
      // Distribution 열: web_report 분포(있는 Item)로 작은 산포 카드를 채운다. 없으면 빈 칸.
      if (isDistCol(c)) {
        const item = r && r["Item"];
        // 분포 유무는 distribution_index(=/full 에 이미 있음)로 판단한다 — ECDF 는 보이는
        // 셀만 배치로 받으므로 캐시 보유 여부로 판단하면 아직 안 받은 항목의 셀이 통째로
        // 안 만들어진다. Yield/ETC/CPK 섹션의 데이터 행(서브헤더 제외)에 산포 카드 표시.
        if (opts.kind === "issue" && item && !subhead && !issuePassRow
          && (rowSection[ri] === "Yield" || rowSection[ri] === "ETC"
            || rowSection[ri] === "CPK" || rowSection[ri] === "TEMP")
          && distHasData(item)) {
          // CPK 섹션 미니셀은 Bin1(양품) ECDF 로 그린다(data-bin1) — 행의 cpk 값이 Bin1
          // 기준이라 그림과 숫자의 데이터 기준을 맞춘다. Yield/ETC 는 기존 전체 범위 유지.
          // TEMP 섹션(Issue Table Temp)은 **Bin1(RT)** 변형으로 고정한다 (2026-08-11 요청) —
          // 그 표의 재판정 자체가 "RT 에서 Bin1 이던 die × RT limit" 기준이라 그림도 같은
          // 기준이어야 숫자와 어긋나지 않는다. Distribution 탭 토글과 무관하게 항상 이 기준.
          const distBin1 = rowSection[ri] === "CPK" ? ` data-bin1="1"`
            : (rowSection[ri] === "TEMP" ? ` data-bin1="1" data-bin1-scope="rt"` : "");
          return `<td${subhead ? ` class="sheet-subhead"` : ""} data-r="${ri}" data-c="${ci}">` +
            `<div class="dist-cell dist-cell-mini" data-subject="${esc(item)}"${distBin1}><div class="dist-plot"></div></div></td>`;
        }
        return `<td class="st-empty${subhead ? " sheet-subhead" : ""}" data-r="${ri}" data-c="${ci}"></td>`;
      }
      // Status 열: 이슈 행(Yield 대표/CPK/ETC — 백엔드가 값 채움)만 Open/Close 표시.
      // 편집모드는 드랍다운(변경 즉시 저장 — edit_mode.js 위임), 조회모드는 텍스트.
      if (String(c).trim().toLowerCase() === "status") {
        const skey = (opts.kind === "issue" && !subhead) ? issueHideStatusKey(r, rowSection[ri]) : "";
        if (!skey || txt === "") {
          return `<td class="st-empty${subhead ? " sheet-subhead" : ""}" data-r="${ri}" data-c="${ci}"></td>`;
        }
        // 드랍다운(또는 텍스트) 아래 신호등 점 — Open 빨강 / Close 초록. 색은 td 의
        // is-open/is-close 클래스가 결정한다(편집모드 변경 시 edit_mode.js 가 갱신).
        const statusCls = `issue-status-cell ${txt === "Close" ? "is-close" : "is-open"}`;
        if (opts.edit) {
          return `<td class="${statusCls}" data-r="${ri}" data-c="${ci}"><select class="issue-status-sel" data-skey="${esc(skey)}">` +
            `<option value="Open"${txt !== "Close" ? " selected" : ""}>Open</option>` +
            `<option value="Close"${txt === "Close" ? " selected" : ""}>Close</option></select>` +
            `<span class="status-dot"></span></td>`;
        }
        return `<td class="issue-status ${statusCls}" data-r="${ri}" data-c="${ci}">${esc(txt)}<span class="status-dot"></span></td>`;
      }
      // opts.editableCols 가 있으면 그 컬럼만 편집 가능(더블클릭으로 활성화), 나머지는 읽기전용으로
      // 아래 일반 렌더링을 그대로 탄다. 없으면 기존처럼 opts.edit 전체 컬럼이 즉시 편집 가능.
      if (opts.edit && (!opts.editableCols || opts.editableCols.has(c))) {
        if (opts.editableCols) {
          const cls = "editing-cell dblclick-edit" + (subhead ? " sheet-subhead" : "")
            + (isCommentCol(c) ? " st-comment" : "");
          // web_report comment 저장용 행 식별 키 — 없으면(서브헤더/placeholder 행) 저장 대상 아님.
          const rowKey = (opts.kind === "issue" && !subhead) ? issueRowKey(r, rowSection[ri]) : "";
          const keyAttr = rowKey ? ` data-key="${esc(rowKey)}"` : "";
          // comment 셀: @[항목] 토큰을 링크로 표시하되 원문(data-raw)을 보관 — 더블클릭 편집 시 원문으로 되돌린다.
          const cInner = isCommentCol(c) ? linkifyComment(txt) : esc(txt);
          const rawAttr = isCommentCol(c) ? ` data-raw="${esc(txt)}"` : "";
          return `<td class="${cls}"${keyAttr}${rawAttr} data-r="${ri}" data-c="${ci}" data-col="${esc(c)}">${cInner}</td>`;
        }
        const cls = "editing-cell" + (isNum ? " st-num" : "") + (subhead ? " sheet-subhead" : "")
          + (isCommentCol(c) ? " st-comment" : "");
        return `<td class="${cls}" contenteditable="true" data-r="${ri}" data-c="${ci}" data-col="${esc(c)}">${esc(txt)}</td>`;
      }
      const clsParts = [];
      if (isCommentCol(c)) clsParts.push("st-comment");   // 열너비 고정 (CSS .st-comment)
      let cellStyle = "";
      if (isEmpty) clsParts.push("st-empty");
      else if (isNum) clsParts.push("st-num");
      if (subhead) clsParts.push("sheet-subhead");
      // 문제 셀 강조(소스별 _yield 컬럼 한정). Yield/ETC 섹션은 값이 클수록 진한 빨강
      // 그라데이션(표 내 최대 fail yield 기준, Yield 탭과 동일 — 소스 1개여도 적용). CPK 섹션은
      // 임계 미만 연빨강이되 다중 소스일 때만(단일 소스는 cpk 가 avg 와 동일해 중복). Pass 행 제외.
      if (gradYield && !subhead && !issuePassRow && !isEmpty && isGradCol(c)) {
        const num = parseFloat(v);
        if (!isNaN(num)) {
          if (issueRowSec === "CPK") { if (issueMultiSource && num <= CPK_WARN_THRESHOLD) clsParts.push("issue-cell-warn"); }
          else if (num > 0) {
            const cmax = issueYieldColMax[c] || 0;
            const ratio = cmax > 0 ? Math.min(1, num / cmax) : 0;
            // 표 종류에 맞는 클래스 — 음영 CSS 는 .kind-issue/.kind-yield 각각에 걸려 있다.
            clsParts.push(opts.kind === "issue" ? "issue-yield-warn" : "yield-grad");
            cellStyle = ` style="--yw:${ratio.toFixed(3)}"`;
          }
        }
      }
      // 선택 모드에서 체크박스를 다는 Step 셀 — 셀 전체가 체크 클릭 영역이다(edit_mode.js).
      const isSelCell = opts.kind === "issue" && ci === 0 && (delHideKey || delEtcItem);
      if (isSelCell) clsParts.push("issue-sel-cell");
      const cls = clsParts.join(" ");
      // Item 셀: 클릭 시 Item_detail 로 이동(항목명 = 측정항목). issue + yield(Bin 상세 구성표) 공용.
      // Pass(Bin 1) 행과 자유입력 Engr ETC 항목(TNO 없음 = 측정항목 아님)은 제외.
      const etcFreeform = opts.kind === "issue" && rowSection[ri] === "ETC"
        && String((r && r["TNO"]) ?? "").trim() === "";
      const itemClickable = (opts.kind === "issue" || opts.kind === "yield") && !subhead && !isEmpty
        && c === "Item" && String((r && (r["Bin"] ?? r["bin"])) ?? "").trim() !== "1" && !etcFreeform;
      let cellHtml;
      if (itemClickable) {
        cellHtml = `<span class="item-detail-link" data-subject="${esc(txt)}">${esc(txt)}</span>`;
      } else if (opts.kind === "issue" && isCommentCol(c) && !isEmpty) {
        cellHtml = linkifyComment(txt);   // 읽기 모드 comment: @[항목] → Item_detail 링크
      } else {
        cellHtml = isEmpty ? "" : esc(txt);
      }
      // Item 셀 개별 삭제(×) — 삭제 모드에서만 보인다(CSS .issue-del-mode).
      if (c === "Item" && delEtcItem) {
        cellHtml += ` <button type="button" class="btn-del-etc-item" data-item="${esc(delEtcItem)}" title="ETC 항목 제거">×</button>`;
      }
      if (c === "Item" && delHideKey) {
        cellHtml += ` <button type="button" class="btn-del-issue-row" data-hkey="${esc(delHideKey)}" title="이 행 삭제(숨김) — 복원은 툴바 '삭제 전체 초기화'">×</button>`;
      }
      // 일괄 삭제/Status 용 체크박스 — 첫 컬럼(Step) 셀 왼쪽. 선택 모드에서만 보인다.
      if (isSelCell) {
        cellHtml = `<input type="checkbox" class="issue-del-chk"` +
          (delHideKey ? ` data-hkey="${esc(delHideKey)}"` : ` data-etc="${esc(delEtcItem)}"`) +
          ` title="선택 (일괄 삭제 / Status 일괄 변경) — Step 셀 아무 곳이나 클릭">` + cellHtml;
      }
      // Bin 그룹 대표행 STEP 셀 오른쪽에 접기/펼치기 토글(접힌 상세행이 있을 때).
      // Issue Table Yield 섹션과 Yield 탭 Temp Corner 표가 같은 _grp 규약을 쓰되, 클래스는
      // 표 종류별로 다르다(핸들러가 각각 toggleIssueGroup / setYieldGroup 이다).
      if ((opts.kind === "issue" || opts.kind === "yield")
        && String(c).trim().toLowerCase() === "step"
        && r && r._grp && !r._detail && (Number(r._ndetail) || 0) > 0) {
        const tcls = opts.kind === "issue" ? "issue-toggle" : "yield-toggle";
        cellHtml += ` <button type="button" class="${tcls}" data-grp="${esc(r._grp)}" aria-expanded="false">▼</button>`;
      }
      // 읽기 모드 Issue Table 셀에만 data-col 부여 → CSS 로 BIN/ITEM/Yield/CPK 폰트 확대(값 가독성).
      // 편집 모드는 부여하지 않아 collectSheetTable 저장 대상(=comment 셀)이 그대로 유지된다.
      const colAttr = (opts.kind === "issue" && !opts.edit) ? ` data-col="${esc(c)}"` : "";
      return `<td${cls ? ` class="${cls}"` : ""}${cellStyle}${colAttr} data-r="${ri}" data-c="${ci}">${cellHtml}</td>`;
    }).join("");
    const isPassRow = !subhead && (issuePassRow
      || (binCol && String((r ? r[binCol] : "") ?? "").trim() === "1"));
    let trAttr = isPassRow ? ` class="yield-pass-row"` : "";
    if (!isPassRow && r && r._grp && (opts.kind === "issue" || opts.kind === "yield")) {
      const pfx = opts.kind === "issue" ? "issue-bin" : "yield-bin";
      trAttr = r._detail
        ? ` class="${pfx}-detail" data-grp="${esc(r._grp)}" style="display:none"`
        : ` class="${pfx}-rep" data-grp="${esc(r._grp)}"`;
    }
    return `<tr${trAttr}>${tds}</tr>`;
  };

  // 본문 행 생성 — 행 구간(슬라이스) 단위로 부를 수 있게 분리한다(청크 렌더용).
  // state.curSec 는 "섹션이 바뀌면 그 섹션 2행 헤더 블록을 먼저 심는다"는 판단의 이월
  // 상태라, 슬라이스를 이어 부를 때 호출자가 같은 객체를 계속 넘겨야 한다.
  function emitRows(start, end, state) {
    let out = "";
    for (let ri = start; ri < end; ri++) {
      const r = bodyRows[ri];
      if (opts.kind === "issue") {
        const sec = rowSection[ri];
        if (sec && sec !== state.curSec) {
          state.curSec = sec;
          out += issueSectionHeadRowsHtml(cols, sec);
        }
        // 헤더 블록이 대체하는 divider 행(CPK 서브헤더 / TEMP·ETC 라벨행)은 데이터로 안 그린다.
        if (isCpkSubheadRow(r)) continue;
        const catTxt = String((r && r["Category"]) || "").trim();
        if (catTxt === "ETC" || catTxt === "TEMP") continue;
      }
      out += renderDataRowTr(r, ri);
    }
    return out;
  }

  const kindCls = opts.kind ? ` kind-${opts.kind}` : "";
  const shell = bodyHtml =>
    `<div class="sheet-wrap${kindCls}"><table class="sheet-table${kindCls}">` +
    `${colgroup}${head}<tbody>${bodyHtml}</tbody></table></div>`;

  // chunk: 본문을 통짜로 만들지 않고, 빈 tbody 를 가진 표 골격 html 과 그것을 채우는
  // fill(tbody, onDone) 을 돌려준다. Issue Table 은 행 수백~수천 × 20열이라 한 번에 만들면
  // 수백 ms 를 통으로 블록한다. **DOM 구조는 통짜 렌더와 완전히 같다** — 호출부가 이 html 을
  // 기존과 같은 자리에 그대로 넣기 때문이다(감싸는 요소를 추가하면 .sheet-wrap.kind-issue 의
  // position:sticky 기준 부모가 바뀌어 고정 동작이 달라진다).
  if (opts.chunk) {
    return {
      html: shell(""),
      fill: (tbody, onDone) =>
        sheetChunkFill(tbody, bodyRows.length, emitRows, onDone),
    };
  }
  return shell(emitRows(0, bodyRows.length, { curSec: null }));
}

// ── 표 본문 청크 채우기 ───────────────────────────────────────────────────────
// 총 작업량은 같고 프레임 단위로 쪼개기만 한다 — 행 내용·순서·DOM 은 통짜 렌더와 동일.
// 같은 tbody 에 새 렌더가 시작되면 토큰이 바뀌어 이전 체인이 스스로 멈춘다.
const SHEET_CHUNK_ROWS = 50;
const _sheetChunkTokens = new WeakMap();
function sheetChunkFill(tbody, total, emitRows, onDone) {
  if (!tbody) return;
  const token = (_sheetChunkTokens.get(tbody) || 0) + 1;
  _sheetChunkTokens.set(tbody, token);
  const state = { curSec: null };
  let i = 0;
  const step = () => {
    if (_sheetChunkTokens.get(tbody) !== token) return;   // 새 렌더가 시작됨 — 중단
    const end = Math.min(total, i + SHEET_CHUNK_ROWS);
    if (end > i) {
      tbody.insertAdjacentHTML("beforeend", emitRows(i, end, state));
      i = end;
    }
    if (i < total) requestAnimationFrame(step);
    else if (onDone) onDone();
  };
  step();   // 첫 청크는 동기 — 빈 표가 한 프레임이라도 보이지 않게
}

// ── 컬럼 폭 드래그 리사이즈 (Yield 등 thead 표 공용) ─────────────────────────
// buildSheetTableHead(cols, {resize:true}) 가 심은 .col-resize-handle(data-col=인덱스)을 끌어
// 그 <col> width 를 바꾼다. 저장 없음(새로고침 시 기본 폭 복귀). Issue Table 은 미니차트
// 재렌더 등 고유 후처리가 있어 별도 바인더(bindIssueColResize)를 유지한다.
// afterResize(선택): 드래그 중/끝에 부를 후처리(고정열 오프셋 재실측 등).
function bindSheetColResize(table, afterResize) {
  const colgroup = table && table.querySelector("colgroup");
  if (!table || !colgroup) return;
  const MIN_W = 24;
  table.addEventListener("mousedown", e => {
    const handle = e.target.closest(".col-resize-handle");
    if (!handle) return;
    const col = colgroup.children[+handle.dataset.col];
    if (!col) return;
    const th = handle.closest("th");
    const startW = th ? th.getBoundingClientRect().width : parseFloat(col.style.width) || 80;
    const startX = e.clientX;
    e.preventDefault();   // 드래그 중 텍스트 선택 방지
    let rafPending = false;
    const sync = () => { rafPending = false; if (afterResize) afterResize(); };
    const onMove = ev => {
      col.style.width = Math.max(MIN_W, Math.round(startW + (ev.clientX - startX))) + "px";
      if (!rafPending) { rafPending = true; requestAnimationFrame(sync); }
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      sync();
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

// 편집된 sheet-table DOM → rows 재구성
function collectSheetTable(panelEl, baseRows) {
  if (!baseRows || !baseRows.length) return baseRows;
  const cols = [];
  baseRows.forEach(r => Object.keys(r || {}).forEach(k => { if (!cols.includes(k)) cols.push(k); }));
  const tds = panelEl.querySelectorAll("td[data-r][data-col]");
  const out = JSON.parse(JSON.stringify(baseRows));
  tds.forEach(td => {
    const ri = +td.dataset.r, col = td.dataset.col;
    if (out[ri] && col in out[ri]) out[ri][col] = td.textContent;
  });
  return out;
}

// summary blocks 편집 수집
function collectSummaryBlocks(panelEl, baseData) {
  if (!baseData || !baseData.blocks) return baseData;
  const out = JSON.parse(JSON.stringify(baseData));
  const blockEls = panelEl.querySelectorAll(".sheet-block");
  out.blocks.forEach((blk, bi) => {
    const blockEl = blockEls[bi];
    if (!blockEl) return;
    const tds = blockEl.querySelectorAll("td[data-r][data-c]");
    tds.forEach(td => {
      const ri = +td.dataset.r, ci = +td.dataset.c;
      if (out.blocks[bi].rows[ri] !== undefined) {
        out.blocks[bi].rows[ri][ci] = td.textContent;
      }
    });
  });
  return out;
}

// isSummaryBlocks: {"blocks":[...]} 형태인지
function isSummaryBlocks(o) {
  return !!(o && typeof o === "object" && Array.isArray(o.blocks) && o.blocks.length > 0);
}

// ── 수율 분모 기준 (payload.yield_basis) ────────────────────────────────────────
// basis="gross" 면 분모가 제품 기준정보 Gross Die, "test" 면 소스별 rawdata 행 수다
// (Gross Die 가 비어 있어 폴백한 경우도 "test"). Pass/Fail 은 어느 경우에도 실측 die 수라,
// Gross Die 기준에서는 미측정 die 만큼 Pass+Fail < Total 이 될 수 있다 — 그래서 배지로
// 분모가 무엇인지와 실제 측정 die 수를 함께 보여준다.
function yieldBasisInfo() {
  return (DATA.web_report && DATA.web_report.yield_basis) || null;
}
function yieldTotalLabel() {
  const b = yieldBasisInfo();
  return (b && b.basis === "gross") ? "Gross Die" : "Total";
}
// 소스별 분모 분해 (payload.yield_basis.by_source) — 없으면 빈 Map(옛 캐시 payload).
function yieldBasisBySource() {
  const b = yieldBasisInfo();
  const map = new Map();
  ((b && Array.isArray(b.by_source)) ? b.by_source : []).forEach(
    r => map.set(String(r.source), r));
  return map;
}
const YIELD_BASIS_REASON = {
  no_gross: "제품 기준정보에 Gross Die 가 없습니다",
  gross_lt_tested: "Gross Die 가 측정 die 보다 적습니다 (수율 100% 초과 방지)",
  tested_short: "측정 die 가 Gross Die 보다 100 개 이상 적습니다",
};
function yieldBasisReasonText(bi) {
  const parts = [];
  if (bi.forced) {
    parts.push("지정한 Gross Die 를 쓸 수 없어 Test data 기준으로 내렸습니다");
    if (YIELD_BASIS_REASON[bi.reason]) parts.push(YIELD_BASIS_REASON[bi.reason]);
  } else if (bi.override) {
    parts.push("Rawdata edit 에서 지정: "
      + (bi.override === "gross" ? "Gross Die" : "Test data 개수"));
  } else {
    parts.push("자동 판정");
    if (YIELD_BASIS_REASON[bi.reason]) parts.push(YIELD_BASIS_REASON[bi.reason]);
  }
  parts.push(`측정 die ${bi.tested}`);
  return parts.join(" · ");
}
function yieldBasisBadgeHtml(ov) {
  const b = yieldBasisInfo();
  if (!b) return "";   // 옛 캐시 payload — 배지 없이 종전 표시
  const tested = (ov && ov.tested != null) ? ov.tested : null;
  const rows = Array.isArray(b.by_source) ? b.by_source : [];
  const nGross = rows.filter(r => r.basis === "gross").length;
  let txt;
  if (b.basis === "mixed")
    txt = `분모: 소스별 — Gross Die ${b.gross_die} ${nGross}개 / Test data ${rows.length - nGross}개`
      + (tested != null ? ` · 측정 die ${tested}` : "");
  else if (b.basis === "gross")
    txt = `분모: Gross Die ${b.gross_die}` + (tested != null ? ` · 측정 die ${tested}` : "");
  else
    txt = `분모: Test data 개수` + (tested != null ? ` ${tested}` : "");
  return `<div class="yo-basis" title="수율 % 의 분모 기준 (Honey → Rawdata edit → [Yield 계산] 에서 변경)">${esc(txt)}</div>`;
}

// 소스별/STEP별 표의 세로 크기 — 행이 이 수 이하면 상한 없이 전부 보이고(스크롤바 없음),
// 넘칠 때만 .ybs-scroll 로 상한+스크롤을 건다 (사용자 요청 2026-08-06: 소스 7개까지는
// 한꺼번에 보이게, 8개 이상부터 스크롤).
const YBS_NOSCROLL_ROWS = 7;
function ybsScrollCls(rowCount) { return rowCount > YBS_NOSCROLL_ROWS ? " ybs-scroll" : ""; }

// Yield 상단 요약 박스 HTML (web_report 세션의 yield_summary 가 있을 때만).
function yieldOverviewHtml() {
  const ov = DATA.web_report && DATA.web_report.yield_summary;
  if (!ov) return "";
  const pct = (typeof ov.yield_pct === "number") ? ov.yield_pct.toFixed(2) : ov.yield_pct;
  // 소스가 2개 이상일 때만 소스별 수율을 따로 표시(단일 소스는 Total 과 동일하므로 생략).
  // 정렬하지 않고 payload(by_source) 순서 = source 순서 그대로 — 아래 STEP×Source 표와
  // 소스 나열 순서를 맞춘다.
  // 분모 열: 소스마다 분모 기준이 다를 수 있으므로(Gross Die / Test data) 무엇으로 나눈
  // 값인지 표에서 바로 보이게 한다 — "소스별 yield 가 왜 다른가"에 화면이 답하도록.
  const bySrc = Array.isArray(ov.by_source) ? ov.by_source : [];
  const basisBySrc = yieldBasisBySource();
  const bySrcHtml = bySrc.length >= 2 ? `<div class="yo-block"><div class="yield-by-source${ybsScrollCls(bySrc.length)}"><table class="ybs-table">
    <thead><tr><th>Source</th><th>Yield</th><th>Pass / Total</th><th>분모</th></tr></thead>
    <tbody>` + bySrc.map(s => {
    const sp = (typeof s.yield_pct === "number") ? s.yield_pct.toFixed(2) : s.yield_pct;
    const bi = basisBySrc.get(String(s.source));
    const bTxt = bi ? ((bi.basis === "gross" ? "Gross " : "Test ") + bi.total) : "";
    return `<tr>
      <td class="ybs-src">${esc(s.source)}${tempRoleTag(s.source)}</td>
      <td class="ybs-pct">${esc(sp)}%</td>
      <td class="ybs-cnt">${esc(s.pass)} / ${esc(s.total)}</td>
      <td class="ybs-cnt"${bi ? ` title="${esc(yieldBasisReasonText(bi))}"` : ""}>${esc(bTxt)}</td>
    </tr>`;
  }).join("") + `</tbody></table></div>
    <div class="yo-cap">입력 소스(파일)별 최종 수율 · '분모' 열 = 그 소스가 쓴 분모(Gross Die / Test data)</div>
    </div>` : "";
  // STEP×Source 표: STEP 셀은 소스 수만큼 rowspan 병합(병합 셀에 STEP 평균 yield 표시).
  // 분모는 각 소스 전체 die(In) 로 **고정**하고 분자만 누적 차감한다 —
  // Cum Yield = (In − 그 STEP 까지의 누적 fail) / In. avg = 소스 산술평균.
  // Fail 열은 "그 STEP 자체 fail / 누적 fail" 2값 — survivor + cum_fail = In 이 성립한다.
  const byStep = Array.isArray(ov.by_step) ? ov.by_step : [];
  // 표 행 수 = 각 STEP 의 source 행 합(옛 payload 는 STEP 당 1행 폴백) — 스크롤 판단용.
  const byStepRows = byStep.reduce(
    (n, s) => n + ((Array.isArray(s.sources) && s.sources.length) ? s.sources.length : 1), 0);
  // STEP 이 하나뿐이면 이 표는 전체 수율 카드와 같은 값을 반복할 뿐이라 오히려 헷갈린다
  // → 통째로 뺀다 (사용자 요청 2026-08-06). STEP 2개 이상일 때만 표시.
  const byStepHtml = byStep.length > 1 ? `<div class="yo-block"><div class="yield-by-step${ybsScrollCls(byStepRows)}"><table class="ybs-table">
    <thead><tr><th>Step</th><th>Source</th><th>Cum Yield</th><th>Pass / In</th><th>Fail (step / cum)</th></tr></thead>
    <tbody>` + byStep.map(s => {
    // sources 가 없으면(옛 payload) pooled 값으로 1행 폴백.
    const srcs = (Array.isArray(s.sources) && s.sources.length) ? s.sources
      : [{ source: "", yield_pct: s.step_yield_pct, survivor: s.survivor, entered: s.entered,
           fail: s.fail, cum_fail: s.cum_fail }];
    const avg = (typeof s.avg_yield_pct === "number") ? s.avg_yield_pct.toFixed(2)
      : (s.avg_yield_pct != null ? s.avg_yield_pct : s.step_yield_pct);
    return srcs.map((sr, i) => {
      const sp = (typeof sr.yield_pct === "number") ? sr.yield_pct.toFixed(2) : sr.yield_pct;
      // cum_fail 이 없는 옛 캐시 payload(스키마 bump 전)는 자기 STEP fail 만 표시.
      const failTxt = (sr.cum_fail === null || sr.cum_fail === undefined)
        ? `${esc(sr.fail)}` : `${esc(sr.fail)} / ${esc(sr.cum_fail)}`;
      const stepCell = i === 0
        ? `<td class="ybs-step" rowspan="${srcs.length}">${esc(s.step)}<span class="ybs-step-avg">avg ${esc(avg)}%</span></td>`
        : "";
      return `<tr>
      ${stepCell}
      <td class="ybs-src">${esc(sr.source)}${tempRoleTag(sr.source)}</td>
      <td class="ybs-pct">${esc(sp)}%</td>
      <td class="ybs-cnt">${esc(sr.survivor)} / ${esc(sr.entered)}</td>
      <td class="ybs-cnt">${failTxt}</td>
    </tr>`;
    }).join("");
  }).join("") + `</tbody></table></div>
    <div class="yo-cap">STEP 별 <b>누적</b> 수율 = (소스 전체 die − 그 STEP 까지의 누적 fail) ÷ 소스 전체 die ·
    분모는 전 STEP 고정이라 P1→P3 로 갈수록 값이 낮아집니다 · Fail 열 = 그 STEP 자체 fail / 누적 fail ·
    Step 셀의 avg = 그 STEP 소스들의 산술평균</div>
    </div>` : "";
  // 카드 밑 작은 글씨 설명(사용자 요청 2026-08-05) — 숫자만 보고 "무엇을 무엇으로 나눈 값인지"
  // 되묻지 않도록 각 카드가 자기 정의를 달고 있게 한다. Total 라벨은 분모 기준에 따라
  // "Total"/"Gross Die" 로 달라지므로 설명 문구도 같은 라벨을 그대로 쓴다.
  const totalLabel = yieldTotalLabel();
  return `<div class="yield-overview">
    <div class="yo-block">
      <div class="yo-pct">${esc(pct)}%</div>
      <div class="yo-cap">전체 수율 = Pass die ÷ ${esc(totalLabel)}</div>
    </div>
    <div class="yo-block">
      <div class="yo-stats">
        <div class="yo-stat"><span class="yo-num">${esc(ov.pass)}</span><span class="yo-label">Pass</span></div>
        <div class="yo-stat"><span class="yo-num">${esc(ov.total)}</span><span class="yo-label">${esc(totalLabel)}</span></div>
        <div class="yo-stat yo-fail"><span class="yo-num">${esc(ov.fail)}</span><span class="yo-label">Fail</span></div>
      </div>
      <div class="yo-cap">Pass = Bin1 통과 die · ${esc(totalLabel)} = 수율 분모 die · Fail = 불량 die</div>
    </div>
    ${yieldBasisBadgeHtml(ov)}
    ${byStepHtml}
    ${bySrcHtml}
  </div>`;
}

// ── Excel식 셀 선택/복사 (Issue Table) ─────────────────────────────────────────
// 클릭 = 1셀, 드래그 = 사각 범위 선택 → Ctrl+C 로 TSV 복사 (Excel 붙여넣기 호환).
// 이벤트 위임(document)이라 탭 재렌더에도 리스너 재바인딩이 필요 없다.
// 활성 contenteditable·버튼·select 등 interactive 요소에서는 선택을 시작하지 않아
// 편집 렌더(dblclick 편집·상태 select·TNO 펼침)와 간섭하지 않는다.
const CELLSEL_SCOPE = "#panel-issues, #panel-issue-temp";  // 적용 패널 — closest() 라 콤마 그대로 동작
let _cellSel = null;   // {table, grid, r1, c1, r2, c2}
let _cellDrag = null;  // {table, r1, c1, moved}
let _cellPainted = []; // 현재 .cell-sel 이 붙은 td — 도색 해제를 표 전체 스캔 없이 하기 위함

// 좌표 → td 맵. 드래그 시작 시 1회만 만든다 (mousemove 마다 표 전체를 훑지 않도록).
function _cellGrid(table) {
  const grid = new Map();
  table.querySelectorAll("td[data-r][data-c]").forEach(td => {
    grid.set(td.dataset.r + ":" + td.dataset.c, td);
  });
  return grid;
}

function cellSelClear() {
  if (!_cellSel) return;
  _cellPainted.forEach(td => td.classList.remove("cell-sel"));
  _cellPainted = [];
  _cellSel = null;
}

// 선택 사각형(minR..maxR × minC..maxC)을 .cell-sel 클래스로 표시 (숨김 행 제외).
// 새 사각형만 칠하고 직전 도색분 중 범위 밖만 지운다 — 표가 커도 mousemove 비용이 일정.
function cellSelPaint(sel) {
  const rA = Math.min(sel.r1, sel.r2), rB = Math.max(sel.r1, sel.r2);
  const cA = Math.min(sel.c1, sel.c2), cB = Math.max(sel.c1, sel.c2);
  const next = [];
  for (let r = rA; r <= rB; r++) {
    for (let c = cA; c <= cB; c++) {
      const td = sel.grid.get(r + ":" + c);
      if (!td || !td.offsetParent) continue;
      td.classList.add("cell-sel");
      next.push(td);
    }
  }
  _cellPainted.forEach(td => {
    const r = +td.dataset.r, c = +td.dataset.c;
    if (r < rA || r > rB || c < cA || c > cB || !td.offsetParent) td.classList.remove("cell-sel");
  });
  _cellPainted = next;
}

// 셀 표시 텍스트 — select 는 현재 값, 그 외는 렌더 텍스트(개행은 공백으로, TSV 격자 보존).
function cellSelText(td) {
  const sel = td.querySelector("select");
  if (sel) return String(sel.value || "");
  return String(td.innerText || "").replace(/\s+/g, " ").trim();
}

// 선택 범위 → TSV. 접힌(display:none) 행은 제외, 행 안의 빈 좌표는 빈 문자열로 채운다.
function cellSelTsv(sel) {
  const rA = Math.min(sel.r1, sel.r2), rB = Math.max(sel.r1, sel.r2);
  const cA = Math.min(sel.c1, sel.c2), cB = Math.max(sel.c1, sel.c2);
  const grid = new Map();  // r → Map(c → text)
  let count = 0;
  sel.table.querySelectorAll("td[data-r][data-c]").forEach(td => {
    const r = +td.dataset.r, c = +td.dataset.c;
    if (r < rA || r > rB || c < cA || c > cB || !td.offsetParent) return;
    if (!grid.has(r)) grid.set(r, new Map());
    grid.get(r).set(c, cellSelText(td));
    count++;
  });
  const lines = [];
  // spread 대신 Array.from — QJSEngine 검증 하네스 파서 호환 (js-verify 관례)
  Array.from(grid.keys()).sort((a, b) => a - b).forEach(r => {
    const row = grid.get(r), cells = [];
    for (let c = cA; c <= cB; c++) cells.push(row.has(c) ? row.get(c) : "");
    lines.push(cells.join("\t"));
  });
  return { text: lines.join("\n"), count };
}

// HTTP LAN 환경에선 navigator.clipboard 가 없어(secure context 아님) execCommand 폴백 필수.
function cellSelCopyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => true, () => _cellSelExecCopy(text));
  }
  return Promise.resolve(_cellSelExecCopy(text));
}
function _cellSelExecCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

// mousedown 대상이 선택 가능한 셀이면 반환 (interactive 요소 위는 null).
function _cellSelTarget(ev) {
  const td = ev.target.closest ? ev.target.closest("td[data-r][data-c]") : null;
  if (!td || !td.closest(CELLSEL_SCOPE)) return null;
  if (ev.target.closest("button, select, a, input, textarea, [contenteditable='true']")) return null;
  return td;
}

document.addEventListener("mousedown", (ev) => {
  if (ev.button !== 0) return;
  const td = _cellSelTarget(ev);
  if (!td) { cellSelClear(); return; }  // 표 밖/interactive 요소 클릭 → 선택 해제
  const table = td.closest("table");
  cellSelClear();
  _cellDrag = { table, r1: +td.dataset.r, c1: +td.dataset.c, moved: false };
  _cellSel = { table, grid: _cellGrid(table),
               r1: _cellDrag.r1, c1: _cellDrag.c1, r2: _cellDrag.r1, c2: _cellDrag.c1 };
  cellSelPaint(_cellSel);
});

document.addEventListener("mousemove", (ev) => {
  if (!_cellDrag) return;
  const td = ev.target.closest ? ev.target.closest("td[data-r][data-c]") : null;
  if (!td || td.closest("table") !== _cellDrag.table) return;
  const r2 = +td.dataset.r, c2 = +td.dataset.c;
  if (_cellSel && r2 === _cellSel.r2 && c2 === _cellSel.c2) return;  // 같은 셀이면 재도색 생략
  if (!_cellDrag.moved) {
    // 드래그 진입 시점에만 네이티브 텍스트 선택 억제 (단일 클릭 셀 내 텍스트 선택은 보존)
    _cellDrag.moved = true;
    _cellDrag.table.classList.add("cell-drag");
    const s = window.getSelection && window.getSelection();
    if (s) s.removeAllRanges();
  }
  _cellSel.r2 = r2;
  _cellSel.c2 = c2;
  cellSelPaint(_cellSel);
});

document.addEventListener("mouseup", () => {
  if (_cellDrag) { _cellDrag.table.classList.remove("cell-drag"); _cellDrag = null; }
});

document.addEventListener("keydown", (ev) => {
  if (!_cellSel) return;
  if (ev.key === "Escape") { cellSelClear(); return; }
  if (!(ev.ctrlKey || ev.metaKey) || (ev.key !== "c" && ev.key !== "C")) return;
  // 사용자가 텍스트를 직접 드래그 선택했거나 편집 중이면 기본 복사에 양보
  const s = window.getSelection && window.getSelection();
  if (s && String(s).length) return;
  const ae = document.activeElement;
  if (ae && (ae.isContentEditable || ae.tagName === "INPUT" || ae.tagName === "TEXTAREA")) return;
  const { text, count } = cellSelTsv(_cellSel);
  if (!count) return;
  ev.preventDefault();
  cellSelCopyText(text).then(ok => {
    if (typeof showToast === "function") {
      showToast(ok ? `${count}개 셀 복사됨` : "복사 실패 — 브라우저가 클립보드를 차단했습니다");
    }
  });
});


/* Issue Table Compare 탭 (Compare 모드 전용, 2026-08-20) ───────────────────────
 *
 * Compare 결과를 기존 Issue Table 형식으로 정리해 카테고리 4개로 보여준다.
 *   Distribution  : 산포 유의차 검출(dist_shift focus) + 신규 item  ┐ 서버가 구운 시트
 *   ETC           : ENGR 수동 추가 항목                              ┘ ("Issue Table Compare")
 *   Bin Transition: 동일 좌표 Bin 비교(불일치 die)   ┐ compare payload 에서 직접 그리는
 *   Log           : 항목 추가/삭제·Limit 변경 행     ┘ **별도 표**(표 뒤 형제)
 *
 * 아래 2개를 시트에 합치지 않은 이유는 축이 다르기 때문이다 — Bin Transition 은 die 좌표
 * 단위, Log 는 Before/After limit 쌍이라 item 한 줄 구조에 안 들어간다. 코멘트도 다르다:
 * 위 2개는 issue_comment(CMPDIST|/CMPETC| 키), 아래 2개는 **compare_note**(bm:/gl: 키)라
 * Map 비교·Log 비교 서브탭과 같은 저장소를 공유한다(한쪽에서 적으면 다른 쪽에도 보인다).
 *
 * 표 렌더·편집·검색·미니셀은 전부 yield_issue.js 의 renderIssueTableInto 를 그대로 쓴다
 * (core.js ISSUE_PANEL_SEL 에 이 패널이 등록돼 있어 후처리가 자동으로 함께 돈다).
 * 하단 표 2개는 compare.js 의 compareBinMatrixHtml / goodlogRowType / cmpNoteCell /
 * bindCompareNotes 를 재사용한다 — 같은 표를 두 번 구현하면 값이 갈린다.
 */

// Log 카테고리에 올릴 행 = **변경 행만** (사용자 확정 2026-08-20).
// 항목 추가(added) / 삭제(removed) / Limit·이름 변경(limitchg). Gap% 초과만인 행은
// 제외한다 — 그건 "이슈"가 아니라 값 차이 관찰이라 Compare 탭 Log 비교에서 본다.
const CMPISS_LOG_TYPES = { added: "추가", removed: "삭제", limitchg: "Limit 변경" };

function cmpIssLogRows(gl) {
  const rows = (gl && gl.rows) || [];
  const out = [];
  rows.forEach(r => {
    const t = goodlogRowType(r);
    if (CMPISS_LOG_TYPES[t]) out.push({ r, t });
  });
  return out;
}

// Log 표 — goodlog 15컬럼 전부가 아니라 이슈 판단에 필요한 것만 추린 요약형.
// Comment 셀은 gl: 키라 Compare 탭 Log 비교와 양방향 동기화된다.
function cmpIssLogTableHtml(gl) {
  const title = `<h3 class="compare-h" id="cmpiss-log">Log — 항목 추가 / 삭제 / Limit 변경</h3>`;
  if (!gl) return title + `<div class="placeholder">Log 비교 데이터 없음</div>`;
  const items = cmpIssLogRows(gl);
  const counts = { added: 0, removed: 0, limitchg: 0 };
  items.forEach(({ t }) => { counts[t]++; });
  const summary = `<div class="compare-summary">
      <span class="cmp-chip gl-chip-add">추가 ${counts.added}</span>
      <span class="cmp-chip gl-chip-del">삭제 ${counts.removed}</span>
      <span class="cmp-chip gl-chip-lim">Limit 변경 ${counts.limitchg}</span>
      <span class="gl-sub">Before ${esc(gl.before_source || "")} → After ${esc(gl.after_source || "")}
        · 값 차이(Gap %)만 있는 항목은 Compare 탭 &gt; Log 비교에서 봅니다</span>
    </div>`;
  if (!items.length) {
    return title + summary +
      `<div class="gl-identical">항목 추가·삭제·Limit 변경이 없습니다.</div>`;
  }
  const lim = v => (v === null || v === undefined) ? "" : _cmpNum(v);
  const head = `<thead><tr>
      <th>구분</th><th>Item</th>
      <th class="num">Before LoLim</th><th class="num">Before HiLim</th>
      <th class="num">After LoLim</th><th class="num">After HiLim</th>
      <th>Unit</th><th>Comment</th></tr></thead>`;
  const body = items.map(({ r, t }) => {
    const name = r.after_item_name || r.before_item_name || "";
    const unit = r.after_unit || r.before_unit || "";
    // Limit 변경 행은 바뀐 쪽 셀을 빨갛게 — 어느 값이 달라졌는지 눈으로 짚게 한다
    // (Compare 탭 Log 비교의 gl-mismatch 와 같은 규칙).
    const mLo = (r.compare_lolimit === false) ? " gl-mismatch" : "";
    const mHi = (r.compare_hilimit === false) ? " gl-mismatch" : "";
    return `<tr class="gl-row gl-${t}">` +
      `<td><span class="gl-ab-item gl-${t === "added" ? "add" : (t === "removed" ? "del" : "lim")}">` +
        `${esc(CMPISS_LOG_TYPES[t])}</span></td>` +
      `<td title="${esc(name)}">${esc(name)}</td>` +
      `<td class="num${mLo}">${lim(r.before_lolimit)}</td>` +
      `<td class="num${mHi}">${lim(r.before_hilimit)}</td>` +
      `<td class="num${mLo}">${lim(r.after_lolimit)}</td>` +
      `<td class="num${mHi}">${lim(r.after_hilimit)}</td>` +
      `<td>${esc(unit)}</td>` +
      cmpNoteCell(glNoteKey(r)) + `</tr>`;
  }).join("");
  return title + summary +
    `<div class="sheet-wrap cmp-scroll"><table class="sheet-table compare-table">` +
    `${head}<tbody>${body}</tbody></table></div>`;
}

// 하단 별도 표 2개 — Distribution/ETC 시트 뒤에 형제로 붙는다(래퍼 금지: sticky).
function cmpIssExtraHtml(cmp) {
  const bin = `<h3 class="compare-h" id="cmpiss-bin">Bin Transition — 동일 좌표 Bin 비교 (불일치 die)</h3>` +
    (compareBinMatrixHtml(cmp.bin_matrix) ||
     `<div class="placeholder">Bin 비교 데이터 없음</div>`);
  return `<div class="cmpiss-extra">${bin}${cmpIssLogTableHtml(cmp.goodlog)}</div>`;
}

function renderCompareIssueTab() {
  const panel = document.getElementById(ISSUE_PANEL_CMP);
  if (!panel) return;
  const wr = DATA.web_report || {};
  const cmp = wr.compare;
  if (!cmp) {
    // Compare 계산 대기(분리 캐시 미스)와 진짜 없음을 구분한다 — renderCompare 와 같은 문구.
    // boot.js 폴링이 완료 시 전 탭을 다시 그리므로 여기서 재시도 로직은 두지 않는다.
    emptyPanel(panel, wr.compare_pending
      ? (window.__aiPendingFailed
          ? "Compare 계산 미완료 — 새로고침하면 다시 시도합니다."
          : "⏳ Compare 계산 중… 끝나면 자동으로 표시됩니다.")
      : "Compare 데이터 없음 (같은 Wafer source 2개 이상 필요)");
    return;
  }
  renderIssueTableInto(panel, issueRowsOf(panel), {
    edit: MODE === "edit",
    emptyText: "Compare 이슈 항목이 없습니다",
    extraHtml: cmpIssExtraHtml(cmp),
    // bm:/gl: 코멘트 편집(더블클릭) — Issue Table 의 dblclick-edit 경로와 별개 채널이다.
    // 패널당 1회만 붙고(dataset 가드) 위임이라 innerHTML 교체에도 살아남는다.
    afterFill: p => bindCompareNotes(p),
  });
}

/* Issue Table Compare 탭 (Compare 모드 전용) ─────────────────────────────────
 *
 * 2026-08-20 신설 → 2026-08-27 구 최상위 Compare 탭(#panel-compare)을 흡수해 서브탭 5개가 됐다.
 *
 *   ISSUE_SUMMARY    Distribution(산포 유의차 + 신규 item) + ETC   ← 서버가 구운 시트
 *   MAP비교        공통성 Map + 동일 좌표 Bin 비교 + Bin Yield 비교
 *   LOG비교        추가/삭제/Limit 변경 요약표 + goodlog 전체표
 *   TESTTIME비교   (데이터 없음 — 정적 안내)
 *   동일성검증      Grade 표
 *
 * 이 파일은 **ISSUE_SUMMARY 서브패널과 탭 진입점**만 담당한다. 나머지 서브패널의 빌더와
 * 전환 로직(cmpMapPanelHtml / cmpLogPanelHtml / cmpEquivPanelHtml / showCmpSub)은
 * compare.js 가 갖는다 — 구 Compare 탭 화면을 그대로 옮긴 것이라 그쪽이 원본이다.
 *
 * 코멘트 채널이 둘로 갈린다:
 *   ISSUE_SUMMARY  → issue_comment  (row_key `CMPDIST|<item>` / `CMPETC|<item>`)
 *   MAP/LOG비교  → compare_note   (key `bm:<x>,<y>` / `gl:<after>\x1F<before>`)
 * 둘 다 **저장 키는 고정 규약**이다(CLAUDE.md 규칙 12) — 화면 라벨만 바꾸고 키는 손대지 말 것.
 *
 * 2026-08-27 변경 2건:
 *  - 구 하단 **Bin Transition 표 삭제** — MAP비교에 같은 표(compareBinMatrixHtml)가 있어
 *    중복이었다(규칙 13: 같은 값은 한 곳에서만).
 *  - 구 하단 **Log 요약표는 LOG비교 상단으로 이동** — goodlog 전체표와 같은 gl: 키를
 *    공유하므로 한 화면에 있어야 값이 갈리지 않는다(compare.js cmpIssLogTableHtml).
 *
 * 표 렌더·편집·검색·미니셀은 전부 yield_issue.js 의 renderIssueTableInto 를 그대로 쓴다
 * (core.js ISSUE_PANEL_SEL 에 ISSUE_SUMMARY 서브패널이 등록돼 있어 후처리가 함께 돈다).
 */

// 탭 진입점 — 서브탭 5개를 관리한다. 실제 하위 화면 렌더는 compare.js showCmpSub 가 한다.
function renderCompareIssueTab() {
  const panel = document.getElementById("panel-issue-cmp");
  if (!panel) return;
  if (!panel.dataset.bound) {
    bindCompareSubtabs(panel);
    // bm:(MAP비교) / gl:(LOG비교) 코멘트 더블클릭 편집 — 두 서브패널을 모두 덮도록
    // **상위 패널**에 위임 1회 건다(안에 dataset.cmpNoteBound 가드가 있다).
    bindCompareNotes(panel);
    // '전체 펼치기' 는 서브패널 밖 툴바에 있어 재렌더와 무관하다 — 1회만.
    bindGoodlogExpandAll(panel);
    panel.dataset.bound = "1";
  }
  panel.classList.add("viz-root");
  const cmp = cmpData();
  if (!cmp) {
    // Compare 계산 대기(분리 캐시 미스)와 진짜 없음을 구분한다 — 둘 다 "데이터 없음"으로
    // 보이면 잠시 뒤 채워질 것을 사용자가 오류로 읽는다. boot.js 폴링이 완료 시 다시 그리므로
    // 여기서 재시도 로직은 두지 않는다.
    const msg = (DATA.web_report || {}).compare_pending
      ? (window.__aiPendingFailed
          ? "Compare 계산 미완료 — 새로고침하면 다시 시도합니다."
          : "⏳ Compare 계산 중… 끝나면 자동으로 표시됩니다.")
      : "Compare 데이터 없음 (같은 Wafer source 2개 이상 필요)";
    ["table", "map", "log", "equiv"].forEach(k => {
      const el = cmpSubEl(panel, k);
      if (el) emptyPanel(el, msg);
    });
    return;
  }
  // 탭을 (다시) 그려야 할 때 = 하위 화면 전부 dirty, 현재 서브탭만 즉시 렌더.
  Object.keys(CMP_SUB_RENDERERS).forEach(k => { cmpSubDirty[k] = true; });
  showCmpSub(cmpSubActive);
  syncCompareToolbarH(panel);
}

// ISSUE_SUMMARY 서브패널 — 이슈 표 본체.
// ⚠ renderIssueTableInto 가 이 div 의 innerHTML 을 통째로 교체한다. 그래서 서브탭 바와
// 다른 서브패널이 이 div **밖**(상위 #panel-issue-cmp)에 있어야 한다(core.js 상수 주석).
// extraHtml 없음 — 구 Bin Transition/Log 표는 2026-08-27 MAP비교/LOG비교로 갔다.
function renderCompareIssueTable() {
  const t = document.getElementById(ISSUE_PANEL_CMP);
  if (!t) return;
  renderIssueTableInto(t, issueRowsOf(t), {
    edit: MODE === "edit",
    emptyText: "Compare 이슈 항목이 없습니다",
  });
}

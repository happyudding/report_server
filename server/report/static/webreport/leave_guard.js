/* leave_guard.js — 세션 이탈 확인 (미저장 편집 보호)
 *
 * 브라우저의 네이티브 beforeunload 경고는 문구를 바꾸거나 버튼을 추가할 수 없다.
 * 그래서 앱 내부에서 발생하는 이탈 — 상단 '뒤로가기' 버튼 / 브라우저 뒤로가기 — 만
 * 가로채 3버튼 확인 모달(저장하고 나가기 · 저장하지 않고 나가기 · 취소)을 띄운다.
 * 탭 닫기·새로고침은 각 채널(edit_mode/_dirty, chart_notes/_cnDirty, note/_noteDirty)의
 * 기존 beforeunload 네이티브 경고가 계속 담당하고, 모달에서 이탈을 확정한 뒤의 중복
 * 경고·재저장만 leaveGuardBypassed() 로 막는다.
 */

let _lgBypass = false;      // 모달에서 이탈 확정 → beforeunload 경고 억제
let _lgTrapArmed = false;   // 브라우저 뒤로가기 흡수용 더미 history 항목 보유 여부
let _lgOnLeave = null;      // 모달 확인 시 실행할 실제 이탈 동작

// 각 파일의 beforeunload 핸들러가 호출한다 — true 면 경고를 띄우지 않는다.
function leaveGuardBypassed() { return _lgBypass; }

// 저장되지 않은 편집이 하나라도 있는가 (dirty 채널 3개 합산).
// trim 의 저장 진행중(_trimSaving)은 '미저장 편집' 이 아니라 in-flight 라 제외한다.
function hasUnsavedEdits() {
  return MODE === "edit" && !!(_dirty || _cnDirty.size || _noteDirty);
}

// ── 확인 모달 ────────────────────────────────────────────────────────────────
function openLeaveConfirm(onLeave) {
  _lgOnLeave = onLeave;
  document.getElementById("leaveConfirmModal").classList.add("show");
}

function closeLeaveConfirm() {
  document.getElementById("leaveConfirmModal").classList.remove("show");
  _lgOnLeave = null;
}

// 이탈 확정 — beforeunload 재경고를 끄고 보관해 둔 이탈 동작을 실행한다.
function lgLeaveNow() {
  const go = _lgOnLeave;
  closeLeaveConfirm();
  _lgBypass = true;
  go();
}

document.getElementById("leaveCancel").addEventListener("click", closeLeaveConfirm);
document.getElementById("leaveDiscardExit").addEventListener("click", lgLeaveNow);

// 저장하고 나가기 — Note(별도 채널) + autoSave(comment/engr/차트주석)를 모두 끝낸 뒤 이탈.
// 저장에 실패하면 dirty 가 되살아나므로 이탈하지 않고 모달을 유지한다.
document.getElementById("leaveSaveExit").addEventListener("click", async () => {
  const btn = document.getElementById("leaveSaveExit");
  btn.disabled = true;
  btn.textContent = "저장 중…";
  try {
    if (_noteDirty) await noteSave();
    await autoSave();
  } finally {
    btn.disabled = false;
    btn.textContent = "저장하고 나가기";
  }
  if (hasUnsavedEdits()) { showToast("저장에 실패했습니다 — 페이지를 벗어나지 않았습니다."); return; }
  lgLeaveNow();
});

document.getElementById("leaveConfirmModal").addEventListener("click", e => {
  if (e.target.id === "leaveConfirmModal") closeLeaveConfirm();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && document.getElementById("leaveConfirmModal").classList.contains("show")) {
    closeLeaveConfirm();
  }
});

// ── 상단 '뒤로가기' 버튼 ─────────────────────────────────────────────────────
// 같은 출처에서 들어온 경우 앵커 전방 이동 대신 브라우저 히스토리로 복귀한다 — BFCache 가
// 살아 있으면 이전 화면 DOM 이 통째로 즉시 복원된다(메인은 pageshow 에서 목록만 조용히 갱신).
// -2 인 이유: 아래 브라우저 뒤로가기 흡수용 더미 항목 1칸 + 이 세션 항목 1칸.
// 직접 링크로 열어 돌아갈 히스토리가 없으면 기존 앵커 주소(/pe/report/)로 폴백한다.
const _lgBackBtn = document.querySelector(".back-btn");

function lgGoBack() {
  let sameOrigin = false;
  try {
    sameOrigin = !!document.referrer && new URL(document.referrer).origin === location.origin;
  } catch (e) { /* referrer 파싱 실패 — 폴백 */ }
  if (sameOrigin && history.length > 2) { _lgTrapArmed = false; history.go(-2); return; }
  location.href = _lgBackBtn ? _lgBackBtn.getAttribute("href") : "/pe/report/";
}

if (_lgBackBtn) {
  _lgBackBtn.addEventListener("click", e => {
    e.preventDefault();
    if (!hasUnsavedEdits()) { lgGoBack(); return; }
    openLeaveConfirm(lgGoBack);
  });
}

// ── 브라우저 뒤로가기 ────────────────────────────────────────────────────────
// 같은 URL 의 더미 history 항목을 하나 심어 뒤로가기 1회를 흡수한다. 미저장 편집이
// 없으면 그대로 통과시키고(추가 back), 있으면 제자리를 지키고 모달을 띄운다.
history.pushState({ leaveGuard: 1 }, "", location.href);
_lgTrapArmed = true;

window.addEventListener("popstate", () => {
  if (!_lgTrapArmed) return;
  if (!hasUnsavedEdits()) { _lgTrapArmed = false; history.back(); return; }
  history.pushState({ leaveGuard: 1 }, "", location.href);   // 제자리 유지
  // 더미 항목 + 세션 항목 2칸을 건너뛰어야 원래 이전 페이지로 나간다.
  openLeaveConfirm(() => { _lgTrapArmed = false; history.go(-2); });
});

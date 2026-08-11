// ── Characteristic 탭 ────────────────────────────────────────────────────────
// 상위 탭 1개 아래 서브탭 6개(Trim Analysis / Shmoo / BV / Analog Chart / TCB / DVO).
// Trim Analysis 는 종전 최상위 탭이던 화면을 그대로 옮긴 것이라 렌더는 계속 trim.js 가
// 담당한다(컨테이너 id #panel-trim-analysis 유지) — 여기서는 서브탭 전환만 관리한다.
// 나머지 5개는 아직 화면이 없어 report_view.html 의 안내 문구만 보여준다.
// 하위 화면은 상위 탭과 같은 lazy 규칙: 그 서브탭에 들어갈 때 처음 그린다.
const CHAR_SUB_RENDERERS = {
  "trim-analysis": () => renderTrimAnalysis(),
};
let charSubActive = "trim-analysis";
const charSubDirty = {};

function renderCharacteristic() {
  const panel = document.getElementById("panel-characteristic");
  if (!panel) return;
  if (!panel.dataset.bound) { bindCharacteristicSubtabs(panel); panel.dataset.bound = "1"; }
  // renderTab 이 이 함수를 부르는 시점 = 상위 탭을 (다시) 그려야 할 때. 하위 화면을 전부
  // dirty 로 돌리고 현재 서브탭만 즉시 그린다(나머지는 그 서브탭에 들어갈 때 그린다).
  Object.keys(CHAR_SUB_RENDERERS).forEach(k => { charSubDirty[k] = true; });
  showCharSub(charSubActive);
}

function bindCharacteristicSubtabs(panel) {
  const bar = panel.querySelector(".char-subtabs");
  if (!bar) return;
  bar.addEventListener("click", e => {
    const btn = e.target.closest("[data-charsub]");
    if (btn) showCharSub(btn.dataset.charsub);
  });
}

function showCharSub(key) {
  const panel = document.getElementById("panel-characteristic");
  if (!panel) return;
  charSubActive = key;
  panel.querySelectorAll("[data-charsub]").forEach(b =>
    b.classList.toggle("active", b.dataset.charsub === key));
  panel.querySelectorAll(".char-subpanel").forEach(p =>
    p.classList.toggle("active", p.dataset.charpanel === key));
  if (CHAR_SUB_RENDERERS[key] && charSubDirty[key]) {
    charSubDirty[key] = false;
    CHAR_SUB_RENDERERS[key]();
  }
  // 숨김(0px) 상태에서 그려진 Plotly 차트는 보일 때 리사이즈해야 폭이 복구된다(Compare 와 동일).
  const active = panel.querySelector(`.char-subpanel[data-charpanel="${key}"]`);
  if (active && window.Plotly) {
    active.querySelectorAll(".js-plotly-plot").forEach(d => { try { Plotly.Plots.resize(d); } catch (e) {} });
  }
}

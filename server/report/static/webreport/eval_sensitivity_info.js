// AI Comment 판정 기준(민감도 게이지) — 세션 상세 우상단 🎚 버튼이 여는 모달.
//
// 두 가지를 한 화면에서 한다:
//   ① 조회 — 이 세션의 AI Comment 가 어떤 임계값으로 판정됐나(값·기본값·출처·설명).
//   ② 사후 변경 — 편집 권한이 있으면 게이지를 바꿔 저장하고 세션을 다시 빌드한다.
//
// 값의 출처는 세션 옵션 하나뿐이라 열려 있는 동안 변하지 않는다 → **세션당 1회 fetch**
// (input_info.js 와 같은 전략). 단계표(카탈로그)는 서버 공용이라 따로 1회 받는다.
//
// ⚠️ 게이지 단계표를 여기 복제하지 말 것 — 정본은 서버
// (eval_analyzer/eval_engine/rules/sensitivity.yaml). 사본을 두면 사용자가 고른 "3단계" 와
// 서버가 아는 "3단계" 가 갈린다.

// 눈금 6칸: 1~5 단계 + '사용자설정'. 마지막 칸은 값을 직접 입력했을 때만 도달하는
// **표시 전용** 상태라 클릭으로 못 간다(Honey 설정 창 GaugeSlider 와 같은 규약).
const ESENS_STOPS = ["1", "2", "3", "4", "5", "사용자설정"];
const ESENS_CUSTOM = 0;
const ESENS_TIPS = {1: "1 rough — 덜 발화", 3: "3 기본(현행)", 5: "5 tight — 더 발화"};

let _esensCache = null;      // 이 세션의 적용 설정 (GET 응답)
let _esensCatalog = null;    // 단계표 카탈로그 (서버 공용)
let _esensDraft = null;      // 편집 중 상태 {global, groups:{}, manual:{}}
let _esensBusy = false;

function esensGroupById(id) {
  return ((_esensCatalog || {}).groups || []).find(g => g.id === id) || null;
}

// 게이지 단계 → 그 그룹 키들의 값. 직접 입력(manual)이 있으면 그것이 이긴다.
function esensGroupValues(group, level, manual) {
  const out = {};
  (group.keys || []).forEach(k => {
    const m = manual[k.key];
    out[k.key] = (typeof m === "number") ? m : k.levels[level - 1];
  });
  return out;
}

// 저장 payload — 기본값(게이지 3 + 직접입력 없음)인 키는 싣지 않는다.
// 그래야 제품군 오버레이(/pe/eval)가 기본 상태에서 계속 적용되고, 기본 세션의 캐시 키가
// 도입 전과 같은 바이트로 유지된다(콜드 폭풍 회피).
function esensBuildSpec(draft) {
  const overrides = {}, manual = {}, groups = {};
  ((_esensCatalog || {}).groups || []).forEach(group => {
    const level = draft.groups[group.id] || 3;
    groups[group.id] = level;
    (group.keys || []).forEach(k => {
      const m = draft.manual[k.key];
      if (typeof m === "number") {
        manual[k.key] = m;
        overrides[k.key] = m;
      } else if (level !== 3) {
        overrides[k.key] = k.levels[level - 1];
      }
    });
  });
  if (!Object.keys(overrides).length) return null;
  return { v: 1, global: draft.global, groups, manual, overrides };
}

function esensCurrentValue(group, key) {
  const level = _esensDraft.groups[group.id] || 3;
  return esensGroupValues(group, level, _esensDraft.manual)[key.key];
}

// ○─○─○─○─○─○ 눈금 게이지. attr 은 그룹 게이지면 data-group, 전체면 data-all.
function esensGaugeHtml(active, { group = "", fixed = false } = {}) {
  const stops = ESENS_STOPS.map((label, i) => {
    const level = i + 1;
    const isCustom = (i === ESENS_STOPS.length - 1);
    const on = (!fixed && (isCustom ? active === ESENS_CUSTOM : active === level));
    const cls = "esens-stop" + (on ? " on" : "") + (isCustom ? " custom" : "")
              + (fixed ? " off" : "");
    // 사용자설정 칸에는 data-level 을 주지 않는다 — 클릭 핸들러가 그걸로 선택 가능 여부를 가른다.
    const attr = (fixed || isCustom) ? ""
      : (group ? ` data-group="${esc(group)}" data-level="${level}"`
               : ` data-all="${level}"`);
    const tip = isCustom ? "값을 직접 입력하면 이 상태가 됩니다"
                         : (ESENS_TIPS[level] || `${level} 단계`);
    return `<span class="${cls}"${attr} title="${esc(tip)}">`
         + `<i class="esens-dot"></i><em>${esc(label)}</em></span>`;
  }).join("");
  return `<span class="esens-gauge${fixed ? " off" : ""}">${stops}</span>`;
}

// threshold 는 **세로로** 쌓는다 — 키 이름이 길어(subpop_density_gap_strong 등) 가로로
// 늘어놓으면 키 5개짜리 그룹(TAIL·BIMODALITY)에서 줄이 접힌다.
function esensValuesHtml(group, editable) {
  return (group.keys || []).map(k => {
    const value = esensCurrentValue(group, k);
    const changed = value !== k.default ? " changed" : "";
    const input = editable
      ? `<input class="esens-val${changed}" type="number" step="any" `
        + `value="${esc(value)}" data-key="${esc(k.key)}" data-group="${esc(group.id)}">`
      : `<span class="esens-val${changed}">${esc(value)}</span>`;
    return `<div class="esens-kv">`
         + `<span class="esens-key" data-help="${esc(k.key)}">${esc(k.key)}</span>`
         + input
         + `<span class="iinfo-sub">기본 ${esc(k.default)}</span></div>`;
  }).join("");
}

// 행 이름은 **SIGNATURE 영문 원문**이다 — 화면 어디서나(Issue Table Signature 컬럼·
// /pe/eval 트레이스) 영문으로 나오는데 여기만 한글이면 같은 것인지 알기 어렵다.
function esensGroupName(group) {
  return (group.signatures || []).map(esc).join("<br>") || esc(group.id);
}

function esensRender() {
  const data = _esensCache || {};
  const editable = !!data.can_edit && !!(_esensCatalog || {}).groups;
  const groups = (_esensCatalog || {}).groups || [];

  const rows = groups.map(group => {
    const level = _esensDraft.groups[group.id] || 3;
    const custom = (group.keys || []).some(
      k => typeof _esensDraft.manual[k.key] === "number");
    const fixed = !!group.gauge_fixed;
    return `<tr>
      <td class="esens-name" title="${esc(group.label_ko || "")}">${esensGroupName(group)}</td>
      <td>${esensGaugeHtml(custom ? ESENS_CUSTOM : level,
                           { group: group.id, fixed })}</td>
      <td>${esensValuesHtml(group, editable)}</td>
    </tr>`;
  }).join("");

  document.getElementById("esensBody").innerHTML = groups.length ? `
    <table class="iinfo-table esens-table">
      <thead><tr><th>SIGNATURE</th><th style="width:1%;">민감도</th><th>Threshold</th></tr></thead>
      <tbody>
        <tr class="iinfo-grouprow"><td>ALL</td>
          <td>${esensGaugeHtml(_esensDraft.global)}</td>
          <td><span class="iinfo-sub">1 rough(덜 발화) ← 3 기본 → 5 tight(더 발화)</span></td>
        </tr>
        ${rows}
      </tbody>
    </table>
    <p class="esens-help" id="esensHelp">항목 이름이나 값에 마우스를 올리면 그 기준이 무슨 뜻인지 알려줍니다.</p>`
    : `<p class="placeholder" style="padding:18px;">민감도 단계표를 불러오지 못했습니다.</p>`;

  const notes = [];
  if (!data.applied) {
    notes.push("이 세션은 기본 설정(3단계)으로 판정됐습니다. "
             + "Honey 의 Options → AI Comment 민감도에서 기본값을 바꿀 수 있습니다.");
  }
  if (data.ai_comment === false) {
    notes.push("이 세션은 AI Comment 를 사용하지 않아 판정 기준이 적용되지 않습니다.");
  }
  if (data.rules_rev) {
    notes.push(`업로드 당시 룰 버전: ${data.rules_rev}`);
  }
  document.getElementById("esensNote").innerHTML = notes.length
    ? `<p class="iinfo-note">${notes.map(esc).join("<br>")}</p>` : "";

  const save = document.getElementById("esensSave");
  save.style.display = editable ? "" : "none";
  save.textContent = _esensBusy ? "저장 중…" : "저장";
  save.disabled = _esensBusy;
  document.getElementById("esensDesc").textContent = data.applied
    ? "이 세션의 AI Comment 가 어떤 기준으로 판정됐는지 보여줍니다."
    : "이 세션은 기본 기준으로 판정됐습니다.";
}

// 설명은 서버 카탈로그(threshold_help)에서 온다 — 클라에 문구를 복제하지 않는다.
function esensHelpText(key) {
  const help = ((_esensCatalog || {}).help || {})[key] || {};
  const what = (help.what || "").split("\n")[0];
  const effect = (help.effect || "").split("\n")[0];
  return [what, effect].filter(Boolean).join(" / ") || key;
}

function esensBindHelp() {
  const bar = document.getElementById("esensHelp");
  if (!bar) return;
  document.getElementById("esensBody").querySelectorAll("[data-help], .esens-val").forEach(el => {
    const key = el.dataset.help || el.dataset.key;
    if (!key) return;
    const text = esensHelpText(key);
    el.title = text;                                   // 마우스오버 툴팁
    el.addEventListener("mouseenter", () => { bar.textContent = text; });
    el.addEventListener("focus", () => { bar.textContent = text; });
  });
}

function esensOnBodyClick(e) {
  const step = e.target.closest(".esens-stop");
  // data-level/data-all 이 없는 칸 = 사용자설정 또는 고정 그룹 — 클릭으로 못 간다.
  if (!step || (!step.dataset.all && !step.dataset.level)) return;
  if (step.dataset.all) {
    const level = Number(step.dataset.all);
    _esensDraft.global = level;
    // 전체를 움직이면 그룹·직접입력을 그 단계로 되돌린다 — "전체 N단계" 가 화면과
    // 저장값 양쪽에서 실제로 N단계를 뜻해야 한다.
    ((_esensCatalog || {}).groups || []).forEach(g => { _esensDraft.groups[g.id] = level; });
    _esensDraft.manual = {};
  } else {
    const group = step.dataset.group;
    _esensDraft.groups[group] = Number(step.dataset.level);
    // 그 그룹의 직접 입력은 게이지 선택으로 대체된다(둘이 공존하면 어느 쪽이 적용됐는지 모른다).
    const g = esensGroupById(group);
    (g ? g.keys || [] : []).forEach(k => { delete _esensDraft.manual[k.key]; });
    const levels = new Set(Object.values(_esensDraft.groups));
    _esensDraft.global = levels.size === 1 ? [...levels][0] : 0;   // 0 = 사용자설정
  }
  esensRender();
  esensBindHelp();
}

function esensOnValueChange(e) {
  const input = e.target.closest(".esens-val");
  if (!input || input.tagName !== "INPUT") return;
  const key = input.dataset.key;
  const group = esensGroupById(input.dataset.group);
  const entry = (group ? group.keys || [] : []).find(k => k.key === key);
  const value = Number(input.value);
  if (!entry || !isFinite(value)) return;
  // 기본값과 같아지면 직접 입력을 해제한다 — "손으로 넣었지만 기본값" 은 게이지와 구분할
  // 이유가 없고, 남겨 두면 저장 payload 만 커진다.
  const gaugeValue = entry.levels[(_esensDraft.groups[group.id] || 3) - 1];
  if (value === gaugeValue) delete _esensDraft.manual[key];
  else _esensDraft.manual[key] = value;
  _esensDraft.global = 0;
  esensRender();
  esensBindHelp();
}

async function esensSave() {
  if (_esensBusy) return;
  const spec = esensBuildSpec(_esensDraft);
  if (!confirm("변경된 Option 으로 Session Build 를 다시 진행 합니다.")) return;
  _esensBusy = true;
  esensRender();
  try {
    const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/eval_sensitivity`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ eval_sensitivity: spec }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    _esensCache = body;
    if (body.rebuild) {
      // 저장으로 캐시 키가 갈렸다 — 다음 조회가 콜드 빌드다. 새로 읽어야 바뀐 판정이 보인다.
      alert("변경된 Option 으로 Session Build 를 다시 진행 합니다.");
      location.reload();
      return;
    }
  } catch (err) {
    alert("민감도 설정을 저장하지 못했습니다.\n" + (err.message || err));
  } finally {
    _esensBusy = false;
    esensRender();
    esensBindHelp();
  }
}

function esensDraftFrom(data) {
  const groups = {};
  ((_esensCatalog || {}).groups || []).forEach(g => {
    groups[g.id] = (data.groups || {})[g.id] || 3;
  });
  const manual = {};
  (data.items || []).forEach(item => {
    if (item.source === "manual") manual[item.key] = item.value;
  });
  const levels = new Set(Object.values(groups));
  return { global: levels.size === 1 ? [...levels][0] : 0, groups, manual };
}

async function openEvalSensitivity() {
  const modal = document.getElementById("evalSensModal");
  document.getElementById("esensNote").innerHTML = "";
  if (!_esensCache) {
    document.getElementById("esensBody").innerHTML =
      `<p class="placeholder" style="padding:18px;">판정 기준을 불러오는 중…</p>`;
  }
  modal.classList.add("show");
  try {
    if (!_esensCatalog) {
      const res = await fetch("/pe/report/api/eval_sensitivity");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _esensCatalog = await res.json();
    }
    if (!_esensCache) {
      const res = await fetch(`/pe/report/session/${SESSION_ID}/web_report/eval_sensitivity`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _esensCache = await res.json();
    }
    _esensDraft = esensDraftFrom(_esensCache);
    esensRender();
    esensBindHelp();
  } catch (e) {
    document.getElementById("esensBody").innerHTML =
      `<p class="placeholder" style="padding:18px;">판정 기준을 불러오지 못했습니다. (${esc(e.message || e)})</p>`;
  }
}

function closeEvalSensitivity() {
  document.getElementById("evalSensModal").classList.remove("show");
}

// AI Comment 세션에서만 버튼을 보인다 — 그 외 세션은 적용 대상 자체가 없다.
// payload 의 ai comment 컬럼 유무로 판정하며, boot.js 가 렌더를 마친 뒤 부른다.
function evalSensButtonSync(hasAiComment) {
  const btn = document.getElementById("btnEvalSens");
  if (btn) btn.style.display = hasAiComment ? "" : "none";
}

document.getElementById("btnEvalSens").addEventListener("click", openEvalSensitivity);
document.getElementById("esensClose").addEventListener("click", closeEvalSensitivity);
document.getElementById("esensSave").addEventListener("click", esensSave);
document.getElementById("esensBody").addEventListener("click", esensOnBodyClick);
document.getElementById("esensBody").addEventListener("change", esensOnValueChange);
document.getElementById("evalSensModal").addEventListener("click", e => {
  if (e.target.id === "evalSensModal") closeEvalSensitivity();
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && document.getElementById("evalSensModal").classList.contains("show")) {
    closeEvalSensitivity();
  }
});

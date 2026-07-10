"""Trim Analysis 항목명 매칭 — 정규화·phase 판정·stem·그룹핑 순수 모듈.

Flask/pandas 무의존 순수 함수 모음 — 판정 로직을 나중에 다른 곳(클라이언트,
배치 스크립트 등)에서도 재사용할 수 있도록 이 모듈 안에 격리한다.

product_type 별 규칙셋 2종:
- PMIC4 (PMIC/SECURITY/TCON, 미지정·기타 기본값):
  정규화 → 토큰 분해 → INIT/CODE/TRIM/VERIFY 4-phase 판정 → stem 그룹핑 → orphan 병합
- TV2 (MDDI/PDDI): 전처리 없이 원본 이름에서 마커/접미사로 TRIM/VERIFY 2-slot 판정.
  MDDI 는 제외어(CPU80/SCAN/RAM/IOFF) 포함 항목을 매칭 대상에서 제외.

수동 재배치(overrides, manifest["trim_overrides"])는 자동 매칭 결과보다 우선 적용된다.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence

RULE_PMIC4 = "PMIC4"
RULE_TV2 = "TV2"

PHASES = ("INIT", "CODE", "TRIM", "VERIFY")          # PMIC4 슬롯 순서 고정
TV2_PHASES = ("TRIM", "VERIFY")                       # MDDI/PDDI 2-section
MEMBER_SLOT = "MEMBER"                                # override 전용: 슬롯 없이 그룹 소속만

# 규칙셋별 (base, target) — shift 판정(base 분포 p20~p80 vs target 평균)과
# 차트 정렬(base 값, 없으면 target)이 공유하는 슬롯 쌍.
BASE_TARGET = {RULE_PMIC4: ("INIT", "TRIM"), RULE_TV2: ("TRIM", "VERIFY")}

PHASE_ALIASES = {
    "INIT": frozenset({"INIT", "INITIAL"}),
    "CODE": frozenset({"CODE"}),
    "TRIM": frozenset({"TRIM", "TRM"}),
    "VERIFY": frozenset({"VERIFY", "VFY", "VRFY", "RETEST", "P2", "PWR2", "RECHK"}),
}
_ALL_PHASE_TOKENS = frozenset().union(*PHASE_ALIASES.values())
# 정규화 (c)단계: 세미콜론 뒤 조각이 이 목록일 때만 주석으로 보고 제거 (대소문자 무시)
_SEMI_COMMENT = frozenset({"INIT", "INITIAL", "CODE", "TRIM", "TRM",
                           "VERIFY", "VFY", "VRFY", "P2"})
_PHASE_PRIORITY = ("CODE", "VERIFY", "TRIM", "INIT")  # 판정 규칙 (3) 검사 순서

# TV2 규칙: 마커 포함 시 무조건 TRIM (접미사보다 우선). 매칭은 전부 대소문자 무시.
_TV2_MARKERS = {"MDDI": "FUSE_", "PDDI": "OTP_"}
_TV2_TRIM_SUFFIXES = ("_P1", "_TRIM", "_PRE")
_TV2_VERIFY_SUFFIXES = ("_P2", "_POST")
_MDDI_EXCLUDE_WORDS = ("CPU80", "SCAN", "RAM", "IOFF")


def rule_set_for(product_type) -> str:
    """product_type → 규칙셋. MDDI/PDDI 만 TV2, 나머지(legacy 값 포함)는 PMIC4."""
    return RULE_TV2 if str(product_type or "").strip().upper() in _TV2_MARKERS else RULE_PMIC4


def phases_for(rule_set: str) -> tuple:
    return TV2_PHASES if rule_set == RULE_TV2 else PHASES


# ── PMIC4: 정규화 → 토큰 → phase → stem ──────────────────────────────────────

def normalize_name(raw) -> str:
    s = str(raw or "").strip()
    s = re.sub(r"^#\d+\s*", "", s)
    if ";" in s:
        head, _, tail = s.rpartition(";")
        if tail.strip().upper() in _SEMI_COMMENT:
            s = head
    s = re.sub(r"\s+", "_", s.strip())
    return re.sub(r"_+", "_", s).strip("_")


def tokenize(normalized: str) -> list:
    tokens = [t for t in str(normalized or "").split("_") if t]
    if tokens and re.fullmatch(r"[Tt]\d+", tokens[0]):
        tokens = tokens[1:]
    return tokens


def classify_phase(tokens: Sequence) -> str | None:
    up = [str(t).upper() for t in tokens]
    if not up:
        return None
    if len(up) >= 2 and up[0] in PHASE_ALIASES["INIT"] and up[1] in PHASE_ALIASES["CODE"]:
        return "CODE"
    if up[0] in ("P2", "PWR2"):
        return "VERIFY"
    for phase in _PHASE_PRIORITY:
        if any(t in PHASE_ALIASES[phase] for t in up):
            return phase
    return None


def stem_of(tokens: Sequence) -> str:
    up = [str(t).upper() for t in tokens]
    if len(up) >= 2 and up[0] in PHASE_ALIASES["INIT"] and up[1] in PHASE_ALIASES["CODE"]:
        up = up[2:]
    return "_".join(t for t in up if t not in _ALL_PHASE_TOKENS)


# ── TV2 (MDDI/PDDI): 원본 이름 기반 마커/접미사 판정 ─────────────────────────

def tv2_excluded(name, product_type) -> bool:
    """MDDI 만: 이름에 제외어가 포함되면 매칭 대상에서 제외."""
    if str(product_type or "").strip().upper() != "MDDI":
        return False
    upper = str(name or "").upper()
    return any(word in upper for word in _MDDI_EXCLUDE_WORDS)


def classify_tv2(name, product_type) -> str | None:
    upper = str(name or "").strip().upper()
    if not upper:
        return None
    marker = _TV2_MARKERS.get(str(product_type or "").strip().upper())
    if marker and marker in upper:
        return "TRIM"
    if upper.endswith(_TV2_TRIM_SUFFIXES):
        return "TRIM"
    if upper.endswith(_TV2_VERIFY_SUFFIXES):
        return "VERIFY"
    return None


def stem_tv2(name, product_type) -> str:
    """접미사(끝 1개) + 마커 부분 제거 후 대문자 stem — fuse_VREF ↔ VREF_p2 짝 매칭용."""
    upper = str(name or "").strip().upper()
    for suffix in _TV2_TRIM_SUFFIXES + _TV2_VERIFY_SUFFIXES:
        if upper.endswith(suffix):
            upper = upper[:-len(suffix)]
            break
    marker = _TV2_MARKERS.get(str(product_type or "").strip().upper())
    if marker:
        upper = upper.replace(marker, "")
    return upper.strip("_")


# ── 공통: 항목 분석 → 그룹핑 → orphan 병합 → overrides ───────────────────────

def analyze_item(raw, rule_set: str = RULE_PMIC4, product_type: str = "") -> dict:
    """항목 1개의 매칭 전처리 결과 — 화면 ① 매칭 표 1행분."""
    name = str(raw)
    if rule_set == RULE_TV2:
        excluded = tv2_excluded(name, product_type)
        phase = None if excluded else classify_tv2(name, product_type)
        stem = "" if (excluded or phase is None) else stem_tv2(name, product_type)
        return {"name": name, "normalized": name.strip(), "tokens": [],
                "phase": phase, "stem": stem, "excluded": excluded}
    normalized = normalize_name(name)
    tokens = tokenize(normalized)
    phase = classify_phase(tokens)
    stem = stem_of(tokens) if phase else ""
    return {"name": name, "normalized": normalized, "tokens": tokens,
            "phase": phase, "stem": stem, "excluded": False}


def _new_group(group_id: str, phases: Sequence, manual: bool = False) -> dict:
    return {"id": group_id, "slots": {p: None for p in phases}, "members": [],
            "manual": manual}


def _merge_orphans(groups: "OrderedDict[str, dict]", item_group: dict) -> None:
    """INIT 슬롯이 빈 그룹(orphan)을 토큰 경계 접두어 관계인 anchor 에 흡수 (PMIC4 전용).

    anchor 조건: INIT 슬롯 보유 AND orphan_stem.startswith(anchor_stem + "_").
    후보 여럿이면 stem 최장 anchor. 병합 시 anchor 의 빈 슬롯만 채움(덮어쓰기 금지).
    """
    anchors = [g for g in groups.values() if g["slots"].get("INIT")]
    for orphan_id in [gid for gid, g in groups.items()
                      if not g["slots"].get("INIT") and any(g["slots"].values())]:
        orphan = groups[orphan_id]
        candidates = [a for a in anchors
                      if a["id"] != orphan_id and orphan_id.startswith(a["id"] + "_")]
        if not candidates:
            continue
        anchor = max(candidates, key=lambda a: len(a["id"]))
        for slot, item in orphan["slots"].items():
            if item is not None and anchor["slots"].get(slot) is None:
                anchor["slots"][slot] = item
        anchor["members"].extend(orphan["members"])
        for member in orphan["members"]:
            item_group[member] = anchor["id"]
        del groups[orphan_id]


def _apply_overrides(groups: "OrderedDict[str, dict]", items: "OrderedDict[str, dict]",
                     overrides: Mapping, phases: Sequence) -> list:
    """수동 재배치를 자동 결과 위에 적용 (dict 삽입순). 반환: 무효 override 항목명 목록.

    - 존재하지 않는 item / 잘못된 slot / 빈 group 명은 조용히 건너뛰고 무효 목록에 노출.
    - 슬롯 배치 시 기존 점유 항목은 슬롯 값만 잃고 members 에 남는다 (수동이 자동보다 우선).
    - orphan 병합은 재실행하지 않는다 — 수동 배치를 자동 병합이 다시 흔들지 않도록.
    """
    invalid = []
    valid_slots = set(phases) | {MEMBER_SLOT}
    for name, spec in (overrides or {}).items():
        spec = spec if isinstance(spec, Mapping) else {}
        info = items.get(str(name))
        slot = str(spec.get("slot") or "").strip().upper()
        group_id = str(spec.get("group") or "").strip().upper()
        if info is None or slot not in valid_slots or not group_id:
            invalid.append(str(name))
            continue
        # 1) 현 위치에서 제거 — 비게 된 슬롯은 빈 채 유지 (members 자동 승격 없음)
        old_gid = info.get("group")
        if old_gid and old_gid in groups:
            old = groups[old_gid]
            for s, occupant in old["slots"].items():
                if occupant == info["name"]:
                    old["slots"][s] = None
            old["members"] = [m for m in old["members"] if m != info["name"]]
            if not any(old["slots"].values()) and not old["members"]:
                del groups[old_gid]
        # 2) 대상 그룹 확보 (없으면 수동 그룹 생성) 후 배치
        group = groups.get(group_id)
        if group is None:
            group = _new_group(group_id, phases, manual=True)
            groups[group_id] = group
        if info["name"] not in group["members"]:
            group["members"].append(info["name"])
        if slot == MEMBER_SLOT:
            info["slot"] = None
        else:
            occupant = group["slots"].get(slot)     # 기존 점유 항목은 members 로 강등
            if occupant and occupant in items:
                items[occupant]["slot"] = None
            group["slots"][slot] = info["name"]
            info["slot"] = slot
        info["group"] = group_id
        info["override"] = True
    return invalid


def build_groups(item_names: Sequence, overrides: Mapping | None = None,
                 rule_set: str = RULE_PMIC4, product_type: str = "") -> dict:
    """항목명 목록 → 매칭 결과 (items 입력순 + groups 등장순). 수치값은 다루지 않는다."""
    phases = phases_for(rule_set)
    items: "OrderedDict[str, dict]" = OrderedDict()
    groups: "OrderedDict[str, dict]" = OrderedDict()
    item_group: dict = {}

    for raw in item_names:
        info = analyze_item(raw, rule_set, product_type)
        info.update({"group": None, "slot": None, "override": False})
        if info["name"] in items:      # 중복 항목명은 첫 항목만 (honeyform 이 이미 중복 거부)
            continue
        items[info["name"]] = info
        if info["phase"] is None or not info["stem"]:
            continue
        group = groups.get(info["stem"])
        if group is None:
            group = _new_group(info["stem"], phases)
            groups[info["stem"]] = group
        group["members"].append(info["name"])
        item_group[info["name"]] = info["stem"]
        if group["slots"][info["phase"]] is None:   # 같은 슬롯 중복은 입력순 첫 항목만
            group["slots"][info["phase"]] = info["name"]

    if rule_set == RULE_PMIC4:
        _merge_orphans(groups, item_group)
    for name, gid in item_group.items():
        info = items[name]
        info["group"] = gid
        group = groups[gid]
        info["slot"] = next((s for s, v in group["slots"].items() if v == name), None)

    invalid_overrides = _apply_overrides(groups, items, overrides or {}, phases)

    base, target = BASE_TARGET[rule_set]
    group_list = []
    for group in groups.values():
        slots = group["slots"]
        flags = {"complete_base_target": bool(slots.get(base) and slots.get(target)),
                 "has_verify": bool(slots.get("VERIFY"))}
        if rule_set == RULE_PMIC4:
            flags["complete_4phase"] = all(slots.get(p) for p in PHASES)
        group_list.append({**group, "flags": flags})

    return {"rule_set": rule_set, "phases": list(phases), "base": base, "target": target,
            "items": list(items.values()), "groups": group_list,
            "invalid_overrides": invalid_overrides}

"""eval 룰 yaml 읽기/검증/백업/저장 — /pe/eval 패널의 파일 계층.

eval_engine 을 직접 import 하지 않는다(규칙 #8). 경로·파싱 결과는 전부
web_report.eval_debug 를 경유하고, 이 모듈은 그 경로에 대한 **파일 IO** 만 한다.

저장 순서: 검증 → 백업 → 원자적 쓰기(tmp+replace) → rules_rev +1.
원자적 쓰기가 필수인 이유: 엔진 캐시가 (경로, mtime) 키라 반쯤 쓰인 파일을 읽으면
그 상태가 캐시된다.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

import yaml

from web_report import eval_debug

BACKUP_DIRNAME = "_backup"
BACKUP_KEEP = 50
# 백업 파일명 = "<경로를 __ 로 평탄화한 원본명>.<UTC timestamp>[-n].bak"
# 원본명 자체에 '.' 이 들어가므로(thresholds.yaml) 뒤에서부터 잘라야 한다.
# 같은 초에 두 번 저장되면 -2, -3 … 이 붙는다 (덮어쓰면 직전 원본이 사라진다).
_BACKUP_NAME_RE = re.compile(r"^(?P<flat>.+)\.\d{8}-\d{6}(-\d+)?\.bak$")

STATUS_HINTS = ("MONITOR", "MINOR", "MAJOR", "CRITICAL")
ISSUE_CATEGORIES = ("YIELD", "CPK", "ETC")
# signatures.py _eval_condition 과 같은 형식 — ">key" "<=0.5" "abs>key"
_COND_RE = re.compile(r"^(abs)?\s*([<>]=?)\s*(.+)$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# signature 편집 시 패널이 다루는 필드 (그 외 키는 원본 유지)
SIGNATURE_FIELDS = ("enabled", "when_metric", "status_hint", "issue_category",
                    "phenomenon_ko", "action_ko", "evidence")


class RuleError(ValueError):
    """사용자에게 그대로 보여줄 검증 실패 (라우트가 400 으로 변환)."""


# ── 공통 파일 IO ──────────────────────────────────────────────────────────────

def _backup(path: Path) -> str | None:
    """저장 전 원본 복사. 반환 = 백업 파일명(원본이 없으면 None)."""
    if not path.exists():
        return None
    root = eval_debug.rules_dir() / BACKUP_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    rel = path.relative_to(eval_debug.rules_dir()) if _under_rules(path) else Path(path.name)
    flat = str(rel).replace(os.sep, "__")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    name = f"{flat}.{stamp}.bak"
    seq = 1
    while (root / name).exists():          # 같은 초 재저장 — 직전 백업을 덮지 않는다
        seq += 1
        name = f"{flat}.{stamp}-{seq}.bak"
    shutil.copy2(path, root / name)
    _prune_backups(root, flat)
    return name


def _under_rules(path: Path) -> bool:
    try:
        path.relative_to(eval_debug.rules_dir())
        return True
    except ValueError:
        return False


def _prune_backups(root: Path, flat_prefix: str) -> None:
    files = sorted(root.glob(f"{flat_prefix}.*.bak"))
    for old in files[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def _write_atomic(path: Path, text: str) -> None:
    """newline="" 로 LF 를 그대로 쓴다 — 기본값이면 Windows 에서 CRLF 로 부풀어
    diff·백업 비교가 전부 어긋난다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _head_comments(path: Path) -> str:
    """파일 선두의 주석 블록 보존 (calibrate 의 head 보존과 같은 방식)."""
    if not path.exists():
        return ""
    head = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            head.append(line)
        else:
            break
    return "\n".join(head).rstrip() + "\n" if head else ""


def _dump(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _backup_target(name: str) -> str:
    """백업 파일명 → 원본의 rules 상대경로. 형식이 다르면 RuleError."""
    m = _BACKUP_NAME_RE.match(name)
    if not m:
        raise RuleError(f"백업 이름 형식이 아님: {name}")
    return m.group("flat").replace("__", os.sep)


def list_backups(limit: int = 200) -> list:
    root = eval_debug.rules_dir() / BACKUP_DIRNAME
    if not root.is_dir():
        return []
    rows = []
    for p in sorted(root.glob("*.bak"), reverse=True)[:limit]:
        st = p.stat()
        try:
            target = _backup_target(p.name).replace(os.sep, "/")
        except RuleError:
            continue
        rows.append({"name": p.name, "size": st.st_size,
                     "mtime": int(st.st_mtime), "target": target})
    return rows


def restore_backup(name: str) -> dict:
    """백업을 원래 위치로 되돌린다 (되돌리기 전 현재 파일도 백업)."""
    if "/" in name or "\\" in name:
        raise RuleError("잘못된 백업 이름")
    target_rel = _backup_target(name)
    root = eval_debug.rules_dir() / BACKUP_DIRNAME
    src = root / name
    if not src.is_file():
        raise RuleError(f"백업 없음: {name}")
    target = eval_debug.rules_dir() / target_rel
    # 내용을 먼저 읽는다 — _backup 이 (이름 충돌 회피에도 불구하고) src 를 건드릴 수 있는
    # 순서 의존을 아예 없앤다. 바이트 그대로 복원해 원본과 완전히 동일하게 만든다.
    data = src.read_bytes()
    yaml.safe_load(data.decode("utf-8"))                  # 파싱 가능 확인
    _backup(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    rev = eval_debug.bump_rules_rev()
    return {"restored": target_rel, "rules_rev": rev}


# ── thresholds 오버레이 ───────────────────────────────────────────────────────

def _check_scope(product_type: str, family_product: str | None) -> None:
    tax = eval_debug.taxonomy()
    if product_type not in tax:
        raise RuleError(f"허용되지 않는 product_type: {product_type}")
    if family_product and family_product not in tax[product_type]:
        raise RuleError(f"{product_type} 의 family_product 가 아님: {family_product}")


def read_thresholds(product_type: str, family_product: str | None = None) -> dict:
    """패널 표시용 — 층별 값 + 병합 결과 + 키별 출처."""
    _check_scope(product_type, family_product)
    doc = eval_debug.thresholds_doc()
    default = dict(doc.get("default") or {})
    legacy_pt = dict((doc.get("product_type") or {}).get(product_type) or {})
    ov_pt = _read_overlay(product_type, None)
    ov_family = _read_overlay(product_type, family_product) if family_product else {}

    origin = {k: "default" for k in default}
    origin.update({k: "product_type(legacy)" for k in legacy_pt})
    origin.update({k: "제품군 공통" for k in ov_pt})
    origin.update({k: f"family({family_product})" for k in ov_family})
    effective = eval_debug.effective_thresholds(product_type, family_product)
    return {"product_type": product_type, "family_product": family_product,
            "default": default, "legacy_product_type": legacy_pt,
            "overlay_pt": ov_pt, "overlay_family": ov_family,
            "effective": effective, "origin": origin,
            "item_class_count": len(doc.get("item_class") or {}),
            "rules_rev": eval_debug.rules_rev()}


def _read_overlay(product_type: str, family_product: str | None) -> dict:
    path = eval_debug.overlay_path(product_type, family_product)
    if not path.is_file():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(doc) if isinstance(doc, dict) else {}


def save_thresholds(product_type: str, family_product: str | None, overrides: dict) -> dict:
    """오버레이 파일 재작성. 값이 None 인 키는 제거, 전부 비면 파일 삭제."""
    _check_scope(product_type, family_product)
    if not isinstance(overrides, dict):
        raise RuleError("overrides 는 객체여야 함")
    allowed = set(eval_debug.default_thresholds())
    current = _read_overlay(product_type, family_product)
    merged = dict(current)
    for key, value in overrides.items():
        if key not in allowed:
            raise RuleError(f"알 수 없는 임계값 키: {key} (thresholds.yaml default 에 없음)")
        if value is None:
            merged.pop(key, None)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuleError(f"{key}: 숫자만 허용 (받은 값 {value!r})")
        merged[key] = value

    path = eval_debug.overlay_path(product_type, family_product)
    backup = _backup(path)
    if merged:
        _write_atomic(path, _dump(merged))
    elif path.exists():
        path.unlink()
    rev = eval_debug.bump_rules_rev()
    return {"path": str(path), "saved": merged, "backup": backup, "rules_rev": rev}


# ── signatures ────────────────────────────────────────────────────────────────

def read_signatures() -> dict:
    sigs = eval_debug.signatures_raw()
    order = eval_debug.specificity_order()
    rows = []
    for s in sigs:
        rows.append({"id": s.get("id"), "enabled": s.get("enabled") is not False,
                     "when_metric": dict(s.get("when_metric") or {}),
                     "status_hint": s.get("status_hint"),
                     "issue_category": s.get("issue_category") or "",
                     "phenomenon_ko": s.get("phenomenon_ko") or "",
                     "action_ko": s.get("action_ko") or "",
                     "evidence": list(s.get("evidence") or []),
                     "in_specificity_order": s.get("id") in order})
    return {"signatures": rows, "rules_rev": eval_debug.rules_rev(),
            "threshold_keys": sorted(eval_debug.default_thresholds())}


def _validate_condition(metric: str, cond, threshold_keys: set) -> None:
    if not _NAME_RE.match(str(metric)):
        raise RuleError(f"잘못된 metric 이름: {metric}")
    m = _COND_RE.match(str(cond).strip())
    if not m:
        raise RuleError(f"{metric}: 조건 형식이 아님 ('>key' '<=0.5' 'abs>key') — {cond!r}")
    ref = m.group(3).strip()
    if ref in threshold_keys:
        return
    try:
        float(ref)
    except ValueError:
        raise RuleError(f"{metric}: 임계값 키 '{ref}' 가 thresholds.yaml default 에 없음")


def save_signature(sig_id: str, payload: dict) -> dict:
    """기존 signature 1건 갱신. 신규 추가/삭제는 허용하지 않는다."""
    path = eval_debug.rules_files()["signatures"]
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sigs = doc.get("signatures") or []
    target = next((s for s in sigs if s.get("id") == sig_id), None)
    if target is None:
        raise RuleError(f"없는 signature: {sig_id} (신규 추가는 이 화면에서 지원하지 않음)")

    threshold_keys = set(eval_debug.default_thresholds())
    updates = {}
    for field in SIGNATURE_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if field == "enabled":
            if not isinstance(value, bool):
                raise RuleError("enabled 는 true/false")
            updates["enabled"] = value
        elif field == "when_metric":
            if not isinstance(value, dict) or not value:
                raise RuleError("when_metric 은 비어있지 않은 객체여야 함")
            for metric, cond in value.items():
                _validate_condition(metric, cond, threshold_keys)
            updates["when_metric"] = {str(k): str(v) for k, v in value.items()}
        elif field == "status_hint":
            if value not in STATUS_HINTS:
                raise RuleError(f"status_hint 는 {list(STATUS_HINTS)} 중 하나")
            updates["status_hint"] = value
        elif field == "issue_category":
            if value and value not in ISSUE_CATEGORIES:
                raise RuleError(f"issue_category 는 {list(ISSUE_CATEGORIES)} 중 하나")
            updates["issue_category"] = value or None
        elif field == "evidence":
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise RuleError("evidence 는 문자열 배열")
            updates["evidence"] = value
        else:
            updates[field] = str(value or "")

    warnings = []
    if updates.get("enabled") is False and sig_id in eval_debug.specificity_order():
        warnings.append("비활성화해도 status.py SPECIFICITY_ORDER 항목은 그대로 남습니다(무해).")

    for key, value in updates.items():
        if key == "issue_category" and value is None:
            target.pop("issue_category", None)
        elif key == "enabled" and value is True:
            target.pop("enabled", None)          # 기본값이므로 키를 지워 원본 형태 유지
        else:
            target[key] = value

    backup = _backup(path)
    _write_atomic(path, _head_comments(path) + _dump(doc))
    rev = eval_debug.bump_rules_rev()
    return {"id": sig_id, "updated": sorted(updates), "backup": backup,
            "rules_rev": rev, "warnings": warnings}


# ── 무결성 검증 ───────────────────────────────────────────────────────────────

def validate_all() -> dict:
    """저장돼 있는 룰 전체 점검 — 저장 버튼과 무관하게 언제든 실행 가능."""
    problems, notes = [], []
    default = eval_debug.default_thresholds()
    tax = eval_debug.taxonomy()
    threshold_keys = set(default)

    # 1) signatures 참조 무결성 + SPECIFICITY_ORDER 정합
    order = set(eval_debug.specificity_order())
    sig_ids = set()
    for s in eval_debug.signatures_raw():
        sig_id = s.get("id")
        sig_ids.add(sig_id)
        for metric, cond in (s.get("when_metric") or {}).items():
            try:
                _validate_condition(metric, cond, threshold_keys)
            except RuleError as exc:
                problems.append(f"[{sig_id}] {exc}")
    for missing in sorted(sig_ids - order):
        problems.append(f"[{missing}] status.py SPECIFICITY_ORDER 에 없음 — primary 정렬 누락")
    for extra in sorted(order - sig_ids):
        notes.append(f"SPECIFICITY_ORDER 에만 있는 id: {extra}")

    # 2) 오버레이 트리 점검 (고아 폴더/파일, 미지의 키)
    tree = eval_debug.rules_dir() / "thresholds"
    if tree.is_dir():
        for pt_dir in sorted(p for p in tree.iterdir() if p.is_dir()):
            if pt_dir.name not in tax:
                problems.append(f"고아 폴더: thresholds/{pt_dir.name} (허용 product_type 아님)")
                continue
            for f in sorted(pt_dir.glob("*.yaml")):
                stem = f.stem
                if stem != "_default" and stem not in tax[pt_dir.name]:
                    problems.append(f"고아 파일: thresholds/{pt_dir.name}/{f.name}")
                doc = yaml.safe_load(f.read_text(encoding="utf-8"))
                if not isinstance(doc, dict):
                    problems.append(f"{f.name}: 최상위가 매핑이 아님")
                    continue
                for key, value in doc.items():
                    if key not in threshold_keys:
                        problems.append(f"thresholds/{pt_dir.name}/{f.name}: 알 수 없는 키 {key}")
                    elif isinstance(value, bool) or not isinstance(value, (int, float)):
                        problems.append(f"thresholds/{pt_dir.name}/{f.name}: {key} 가 숫자가 아님")
    else:
        notes.append("오버레이 트리 없음 — default 만 적용 중")

    # 3) 전 조합 병합 시뮬레이션 (KeyError 유발 조합 조기 발견)
    for pt, families in tax.items():
        for family in [None] + list(families):
            merged = eval_debug.effective_thresholds(pt, family)
            for s in eval_debug.signatures_raw():
                if s.get("enabled") is False:
                    continue
                for metric, cond in (s.get("when_metric") or {}).items():
                    m = _COND_RE.match(str(cond).strip())
                    ref = m.group(3).strip() if m else ""
                    if ref and ref not in merged:
                        try:
                            float(ref)
                        except ValueError:
                            problems.append(
                                f"{pt}/{family or '_default'}: {s.get('id')} 가 참조하는 "
                                f"'{ref}' 가 병합 결과에 없음")
    return {"ok": not problems, "problems": problems, "notes": notes,
            "rules_rev": eval_debug.rules_rev()}

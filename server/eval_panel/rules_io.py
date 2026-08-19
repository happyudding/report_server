"""eval 룰 yaml 읽기/검증/백업/저장 — /pe/eval 패널의 파일 계층.

eval_engine 을 직접 import 하지 않는다(규칙 #8). 경로·파싱 결과는 전부
web_report.eval_debug 를 경유하고, 이 모듈은 그 경로에 대한 **파일 IO** 만 한다.

저장 순서: 검증 → 백업 → 원자적 쓰기(tmp+replace) → rules_rev +1.
원자적 쓰기가 필수인 이유: 엔진 캐시가 (경로, mtime) 키라 반쯤 쓰인 파일을 읽으면
그 상태가 캐시된다.
"""
from __future__ import annotations

import copy
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
                    "phenomenon_ko", "action_ko", "evidence", "scope")
# 제품군 오버레이 파일에 쓸 수 있는 필드 — scope 는 제외한다(오버레이 자체가 적용 범위다).
SIGNATURE_OVERLAY_FIELDS = tuple(f for f in SIGNATURE_FIELDS if f != "scope")
SCOPE_KEYS = ("product_type", "family_product")


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

# thresholds.yaml default 섹션의 "키: 값  # 설명" 에서 설명만 뽑는다 —
# 설명의 정본을 yaml 주석 한 곳에 두기 위함(패널에 사전을 따로 두면 곧 어긋난다).
_DESC_RE = re.compile(r"^\s+(?P<key>\w+)\s*:\s*[^#]*#\s*(?P<desc>.+?)\s*$")


def threshold_descriptions() -> dict:
    """{임계값 키: yaml 주석}. 주석이 없는 키는 빠진다."""
    path = eval_debug.rules_files()["thresholds"]
    out, in_default = {}, False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith(" ") and line.strip():
            in_default = line.startswith("default:")
            continue
        if not in_default:
            continue
        m = _DESC_RE.match(line)
        if m:
            out[m.group("key")] = m.group("desc")
    return out


# 긴 설명(통계 초보용)은 yaml 주석 한 줄에 담을 수 없어 패널 옆 파일에 둔다.
# 한 줄 요약의 정본은 여전히 thresholds.yaml 주석이고, 이 파일은 그 확장이다.
_HELP_FILE = Path(__file__).resolve().parent / "threshold_help.yaml"
_help_cache = {"mtime": None, "data": {}}


def threshold_help() -> dict:
    """{임계값 키: {what, how, effect, tip}}. 파일이 없거나 깨지면 빈 dict(화면은 정상 동작)."""
    try:
        mtime = _HELP_FILE.stat().st_mtime
    except OSError:
        return {}
    if _help_cache["mtime"] != mtime:
        try:
            doc = yaml.safe_load(_HELP_FILE.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return _help_cache["data"]
        _help_cache["data"] = {str(k): {kk: str(vv).strip() for kk, vv in (v or {}).items()}
                               for k, v in doc.items() if isinstance(v, dict)}
        _help_cache["mtime"] = mtime
    return _help_cache["data"]


def threshold_usage() -> dict:
    """{임계값 키: [그 값을 참조하는 signature ...]}. 어떤 룰에 영향을 주는지 표시용."""
    usage = {}
    for s in eval_debug.signatures_raw():
        sig_id = s.get("id")
        enabled = s.get("enabled") is not False
        for cond in (s.get("when_metric") or {}).values():
            # 조건은 문자열 하나이거나 밴드(목록) 다 — 목록이면 항목마다 참조를 센다.
            for one in (cond if isinstance(cond, (list, tuple)) else [cond]):
                m = _COND_RE.match(str(one).strip())
                if not m:
                    continue
                ref = m.group(3).strip()
                if ref and not _is_number(ref):
                    usage.setdefault(ref, []).append({"id": sig_id, "enabled": enabled})
    # 코드가 직접 읽는 임계값(선언형 조건이 아니라 파이썬에서 참조) — 표에서 "미사용" 으로
    # 보이면 지워도 되는 값으로 오해하므로 사용처를 명시한다.
    for key, where in _CODE_REFS.items():
        usage.setdefault(key, []).append({"id": where, "enabled": True, "code": True})
    return usage


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


# 선언형 when_metric 이 아니라 엔진 코드가 직접 읽는 임계값 → 표시용 사용처 라벨
_CODE_REFS = {
    "n_min": "min-n 가드(고차모멘트 룰 전체)",
    "outlier_sigma": "L2 features(outlier 판정)",
    "edge_region_pct": "L2 features(공간 영역 분할)",
    "center_region_pct": "L2 features(공간 영역 분할)",
    "spatial_fail_count_min": "L2 features(공간 룰 최소 fail)",
    "region_fail_share_min": "L2 features(wafer_zone_signature)",
    "spot_fail_spread_max": "L2 features(wafer_zone_signature)",
    "cpk_warn": "L6 저장 게이트(코멘트 생성 여부)",
    "cpk_bad": "L4 trump(CRITICAL 강제)",
    "cpk_trump_yield_floor": "L4 trump(CRITICAL 강제)",
    "bimodality_warn": "L2 features(modality_v2)",
    "subpop_n_min": "L2 features(modality_v2)",
    "subpop_outlier_ratio_max": "L2 features(modality_v2)",
    "subpop_density_gap_warn": "L2 features(modality_v2)",
    "subpop_density_gap_strong": "L2 features(modality_v2)",
    "subpop_value_gap_warn": "L2 features(modality_v2)",
    "subpop_minor_mass_min": "L2 features(modality_v2)",
    "gross_yield_bad": "L4 trump(PF CRITICAL)",
    "source_min_count": "cross_source(evaluate 미사용)",
    "source_fail_rate_delta_warn": "cross_source(evaluate 미사용)",
}


# 엔진이 암묵 전제하는 관계 불변식 (a op b). 위반은 "실험" 이 아니라 조용한 오동작이라
# 저장을 막는다. 검사는 **병합 결과(effective)** 기준 — 오버레이가 쌍의 한쪽만 덮으면
# 파일 단독으로는 판정할 수 없기 때문이다. 단 이번 저장이 실제로 바꾼 키(touched)가 쌍에
# 걸릴 때만 본다(상위 층에 이미 있는 위반 때문에 무관한 키 저장까지 막히지 않게 —
# 상위 층 위반은 validate_all 이 전역 보고한다).
THRESHOLD_RELATIONS = (
    ("cpk_bad", "<=", "cpk_warn"),
    ("center_region_pct", "<", "edge_region_pct"),
    ("subpop_density_gap_warn", "<=", "subpop_density_gap_strong"),
    ("heavy_tail_mass_min", "<=", "heavy_tail_mass_max"),
)

# 값 종류 — **opt-in 표**. 여기 없는 키는 검사하지 않는다(새 임계값이 저장을 막지 않게).
# 구조적으로 범위가 정해진 값만 담는다: ratio=0~1 비율/정규화 반경, count=1 이상 정수,
# positive=0 초과. "큰 값을 넣어 사실상 끄기" 가 정당한 키(spread_norm_warn·kurtosis_warn
# 등)는 일부러 뺐다 — 그 용도는 signature 의 enabled:false 가 담당한다.
THRESHOLD_KINDS = {
    "subpop_outlier_ratio_max": "ratio", "subpop_minor_mass_min": "ratio",
    "subpop_density_gap_warn": "ratio", "subpop_density_gap_strong": "ratio",
    "subpop_value_gap_warn": "ratio", "code_edge_hit_warn": "ratio",
    "edge_region_pct": "ratio", "center_region_pct": "ratio",
    "gross_yield_bad": "ratio", "cpk_trump_yield_floor": "ratio",
    "region_fail_share_min": "ratio", "spot_fail_spread_max": "ratio",
    "heavy_tail_mass_min": "ratio", "heavy_tail_mass_max": "ratio",
    "tail_side_share_min": "ratio",
    "n_min": "count", "subpop_n_min": "count", "spatial_fail_count_min": "count",
    "source_min_count": "count",
    "outlier_sigma": "positive", "outlier_fail_mad_min": "positive",
    "outlier_fail_gap_sigma_min": "positive",
}


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_threshold_values(effective: dict, touched: set) -> list:
    """관계·타입 위반 메시지 목록 (비면 통과). 파일 IO 없는 순수 함수."""
    problems = []
    for key in sorted(touched):
        kind = THRESHOLD_KINDS.get(key)
        value = effective.get(key)
        if kind is None or not _is_num(value):
            continue
        if kind == "ratio" and not 0 <= value <= 1:
            problems.append(f"{key}: 0~1 비율이어야 함 (받은 값 {value})")
        elif kind == "count" and (value <= 0 or float(value) != int(value)):
            problems.append(f"{key}: 1 이상 정수여야 함 (받은 값 {value})")
        elif kind == "positive" and value <= 0:
            problems.append(f"{key}: 0 보다 커야 함 (받은 값 {value})")
    for left, op, right in THRESHOLD_RELATIONS:
        if left not in touched and right not in touched:
            continue
        a, b = effective.get(left), effective.get(right)
        if not _is_num(a) or not _is_num(b):
            continue
        if a > b if op == "<=" else a >= b:
            word = "작거나 같아야" if op == "<=" else "작아야"
            problems.append(f"{left}({a}) 는 {right}({b}) 보다 {word} 함")
    return problems


def _check_scope(product_type: str, family_product: str | None) -> None:
    tax = eval_debug.taxonomy()
    if product_type not in tax:
        raise RuleError(f"허용되지 않는 product_type: {product_type}")
    if family_product and family_product not in tax[product_type]:
        raise RuleError(f"{product_type} 의 family_product 가 아님: {family_product}")


def read_thresholds(product_type: str, family_product: str | None = None) -> dict:
    """패널 표시용 — 이 범위의 적용값 + 상속 기준값 + 키별 출처 + 사용처.

    `inherited` = **이 범위의 오버레이를 뺀** 값(= 이 화면에서 값을 지웠을 때 돌아갈 값).
    화면은 `effective` 를 입력칸에 채우고, 저장 시 inherited 와 같은 값은 파일에 쓰지
    않는다 — "보이는 값이 곧 적용값" 이면서 오버레이 파일은 최소로 유지된다.

    product_type 이 비면 **기준값(default) 읽기 전용 뷰**를 돌려준다 — 전역 범위 선택기의
    "기준값 (전 제품 공통)" 자리다. 기준값은 패널에서 편집하지 않으므로(save_thresholds 는
    제품군을 요구한다) 화면도 입력칸을 잠근다.
    """
    if not product_type:
        default = dict(eval_debug.default_thresholds())
        return {"product_type": "", "family_product": None, "read_only": True,
                "default": default, "legacy_product_type": {},
                "overlay_pt": {}, "overlay_family": {},
                "inherited": default, "own": {}, "effective": default,
                "origin": {k: "기본값" for k in default},
                "descriptions": threshold_descriptions(), "usage": threshold_usage(),
                "help": threshold_help(),
                "item_class_count": len(eval_debug.thresholds_doc().get("item_class") or {}),
                "rules_rev": eval_debug.rules_rev()}
    _check_scope(product_type, family_product)
    inherited, origin, parts = _inherited_thresholds(product_type, family_product)
    default, legacy_pt = parts["default"], parts["legacy_pt"]
    ov_pt, ov_family = parts["overlay_pt"], parts["overlay_family"]
    own = parts["own"]

    return {"product_type": product_type, "family_product": family_product,
            "read_only": False,
            "default": default, "legacy_product_type": legacy_pt,
            "overlay_pt": ov_pt, "overlay_family": ov_family,
            "inherited": inherited, "own": own,
            "effective": {**inherited, **own}, "origin": origin,
            "descriptions": threshold_descriptions(), "usage": threshold_usage(),
            "help": threshold_help(),
            "item_class_count": len(doc.get("item_class") or {}),
            "rules_rev": eval_debug.rules_rev()}


def _read_overlay(product_type: str, family_product: str | None) -> dict:
    path = eval_debug.overlay_path(product_type, family_product)
    if not path.is_file():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(doc) if isinstance(doc, dict) else {}


def _inherited_thresholds(product_type: str, family_product: str | None):
    """(이 범위 오버레이를 뺀 값, 키별 출처, 원자료 조각).

    read_thresholds(표시)와 save_thresholds(관계 검증)가 **같은 병합 결과**를 봐야 한다 —
    오버레이가 관계쌍의 한쪽만 덮으면 오버레이 파일 단독으로는 검사할 수 없기 때문이다.
    """
    doc = eval_debug.thresholds_doc()
    default = dict(doc.get("default") or {})
    legacy_pt = dict((doc.get("product_type") or {}).get(product_type) or {})
    ov_pt = _read_overlay(product_type, None)
    ov_family = _read_overlay(product_type, family_product) if family_product else {}

    inherited = {**default, **legacy_pt}
    origin = {k: "기본값" for k in default}
    origin.update({k: "제품군(legacy 섹션)" for k in legacy_pt})
    if family_product:
        inherited.update(ov_pt)
        origin.update({k: f"{product_type} 공통" for k in ov_pt})
        own = ov_family
        origin.update({k: f"{family_product} 직접 지정" for k in ov_family})
    else:
        own = ov_pt
        origin.update({k: f"{product_type} 직접 지정" for k in ov_pt})
    return inherited, origin, {"default": default, "legacy_pt": legacy_pt,
                               "overlay_pt": ov_pt, "overlay_family": ov_family, "own": own}


def save_thresholds(product_type: str, family_product: str | None, overrides: dict) -> dict:
    """오버레이 파일 재작성. 값이 None 인 키는 제거, 전부 비면 파일 삭제.

    내용이 그대로면(no_op) 백업·쓰기·rev 증가를 전부 건너뛴다 — rev 를 올리면 ai_comment
    세션의 리포트 캐시가 통째로 무효화되므로 "저장 눌렀지만 안 바뀐" 경우까지 재평가시키지
    않는다.
    """
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
    if merged == current:
        return {"path": str(path), "saved": merged, "backup": None, "no_op": True,
                "rules_rev": eval_debug.rules_rev()}

    touched = {k for k in set(current) | set(merged) if current.get(k) != merged.get(k)}
    inherited, _, _ = _inherited_thresholds(product_type, family_product)
    problems = _check_threshold_values({**inherited, **merged}, touched)
    if problems:
        raise RuleError("; ".join(problems))

    backup = _backup(path)
    if merged:
        _write_atomic(path, _dump(merged))
    elif path.exists():
        path.unlink()
    rev = eval_debug.bump_rules_rev()
    return {"path": str(path), "saved": merged, "backup": backup, "no_op": False,
            "rules_rev": rev}


# ── signatures ────────────────────────────────────────────────────────────────

def _read_sig_overlay(product_type: str | None, family_product: str | None) -> dict:
    """signature 오버레이 파일 1개 → {id: {필드: 값}}. 없으면 빈 dict."""
    if not product_type:
        return {}
    path = eval_debug.signature_overlay_path(product_type, family_product)
    if not path.is_file():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    sigs = (doc or {}).get("signatures") if isinstance(doc, dict) else None
    return {str(k): dict(v) for k, v in (sigs or {}).items() if isinstance(v, dict)}


def _merge_sigs(base: list, overrides: dict) -> list:
    return [{**s, **(overrides.get(s.get("id")) or {})} for s in base]


def _norm_suppressed_by(raw) -> list:
    """`suppressed_by` 정규화 — 엔진 signatures._suppressor_ids 와 같은 규칙(문자열 1개 허용)."""
    if isinstance(raw, str):
        raw = [raw]
    return [str(v) for v in (raw or []) if v]


def _sig_row(s: dict) -> dict:
    """signature dict → 패널 표시용 정규화 행 (병합 결과·상속값 둘 다 이 모양으로 만든다)."""
    return {"id": s.get("id"), "enabled": s.get("enabled") is not False,
            "when_metric": dict(s.get("when_metric") or {}),
            "status_hint": s.get("status_hint"),
            "issue_category": s.get("issue_category") or "",
            "phenomenon_ko": s.get("phenomenon_ko") or "",
            "action_ko": s.get("action_ko") or "",
            "evidence": list(s.get("evidence") or []),
            # 읽기 전용 — 룰 사이의 관계 3종. 편집 UI 는 제공하지 않고 화면에 관계만
            # 찍는다(SIGNATURE_FIELDS 에 없으므로 저장에서도 건드리지 않는다).
            #   suppressed_by = 함께 뜨면 primary 만 양보(목록에는 남는다)
            #   hidden_by     = 함께 뜨면 목록에서 통째로 제거
            #   replaces      = 나열한 것이 모두 뜨면 그것들을 지우고 이 룰이 대신 발화
            "suppressed_by": _norm_suppressed_by(s.get("suppressed_by")),
            "hidden_by": _norm_suppressed_by(s.get("hidden_by")),
            "replaces": _norm_suppressed_by(s.get("replaces")),
            "scope": _norm_scope_doc(s.get("scope"))}


def read_signatures(product_type: str | None = None,
                    family_product: str | None = None) -> dict:
    """이 범위에서 엔진이 실제로 쓰는 signature 목록.

    thresholds 와 같은 규약이다 — 화면은 `병합 결과`(=적용값)를 보여주고, 저장 시
    상속값과 같은 필드는 오버레이 파일에 쓰지 않는다. `own_fields` 는 이 범위 파일이
    직접 지정한 필드라 "↺ 상속으로" 를 활성화하는 근거가 된다.
    """
    if product_type:
        _check_scope(product_type, family_product or None)
    else:
        family_product = None
    base = eval_debug.signatures_raw()
    parent_ov = _read_sig_overlay(product_type, None) if family_product else {}
    own_ov = _read_sig_overlay(product_type, family_product)

    inherited = {s.get("id"): _sig_row(s) for s in _merge_sigs(base, parent_ov)}
    merged = _merge_sigs(_merge_sigs(base, parent_ov), own_ov)
    order = eval_debug.specificity_order()

    rows = []
    for s in merged:
        row = _sig_row(s)
        sig_id = row["id"]
        inh = inherited.get(sig_id, {})
        row["own_fields"] = sorted(k for k in (own_ov.get(sig_id) or {})
                                   if k in SIGNATURE_OVERLAY_FIELDS)
        row["inherited"] = inh
        row["in_specificity_order"] = sig_id in order
        rows.append(row)

    thresholds = (eval_debug.effective_thresholds(product_type, family_product or None)
                  if product_type else eval_debug.default_thresholds())
    return {"signatures": rows, "rules_rev": eval_debug.rules_rev(),
            "product_type": product_type or "", "family_product": family_product or "",
            "threshold_keys": sorted(eval_debug.default_thresholds()),
            "thresholds": thresholds,
            "taxonomy": eval_debug.taxonomy(),
            "metric_keys": sorted(_known_metrics(merged))}


def _norm_scope_doc(scope) -> dict:
    """yaml 의 scope → 항상 {product_type: [...], family_product: [...]} 형태로."""
    scope = scope if isinstance(scope, dict) else {}
    return {k: [str(v) for v in (scope.get(k) or [])] for k in SCOPE_KEYS}


def _known_metrics(sigs) -> set:
    """조건 편집 드롭다운용 지표 이름 후보 — 지금 룰에 실제로 쓰이는 이름 전부."""
    return {str(m) for s in sigs for m in (s.get("when_metric") or {})}


def _write_sig_overlay(product_type: str, family_product: str | None,
                       entries: dict) -> str | None:
    """오버레이 파일 재작성 — 남는 항목이 없으면 파일 삭제. 반환 = 백업 파일명."""
    path = eval_debug.signature_overlay_path(product_type, family_product)
    backup = _backup(path)
    if entries:
        _write_atomic(path, _dump({"signatures": entries}))
    elif path.exists():
        path.unlink()
    return backup


def _inherited_row(sig_id: str, product_type: str | None,
                   family_product: str | None) -> dict:
    """이 범위 오버레이를 **뺀** 값 = 여기서 지웠을 때 돌아갈 값."""
    base = {s.get("id"): s for s in eval_debug.signatures_raw()}
    sig = dict(base.get(sig_id) or {})
    if family_product:
        sig.update(_read_sig_overlay(product_type, None).get(sig_id) or {})
    return _sig_row(sig)


def _same_as_inherited(field: str, value, inherited: dict) -> bool:
    """상속값과 같은 필드는 오버레이에 쓰지 않는다(상위 층이 바뀌면 따라가게)."""
    base = inherited.get(field)
    if field == "enabled":
        return bool(value) == bool(base)
    if field == "when_metric":
        return {str(k): str(v) for k, v in (value or {}).items()} == dict(base or {})
    if field == "evidence":
        return [str(v) for v in (value or [])] == list(base or [])
    return (value or "") == (base or "")


def set_signatures_enabled(sig_ids: list, enabled: bool, product_type: str | None = None,
                           family_product: str | None = None) -> dict:
    """여러 signature 의 활성 상태를 한 번에 바꾼다 (yaml 쓰기 1회 = 백업·rev 도 1회).

    제품군을 고르면 그 범위 오버레이에만 기록한다 — 기준값(signatures.yaml)은 안 건드린다.
    """
    if not isinstance(sig_ids, list) or not sig_ids:
        raise RuleError("적용할 signature 를 선택하세요")
    if product_type:
        _check_scope(product_type, family_product or None)
    else:
        family_product = None

    known = {s.get("id") for s in eval_debug.signatures_raw()}
    unknown = [s for s in sig_ids if s not in known]
    if unknown:
        raise RuleError(f"없는 signature: {unknown}")

    if product_type:
        entries = _read_sig_overlay(product_type, family_product)
        changed = []
        for sig_id in sig_ids:
            inherited = _inherited_row(sig_id, product_type, family_product)
            entry = dict(entries.get(sig_id) or {})
            current = entry.get("enabled", inherited["enabled"]) is not False
            if current == enabled:
                continue                      # 이미 그 상태
            if enabled == inherited["enabled"]:
                entry.pop("enabled", None)    # 상속으로 되돌아감 = 키 제거
            else:
                entry["enabled"] = enabled
            if entry:
                entries[sig_id] = entry
            else:
                entries.pop(sig_id, None)
            changed.append(sig_id)
        if not changed:
            return {"changed": [], "backup": None, "no_op": True,
                    "rules_rev": eval_debug.rules_rev()}
        backup = _write_sig_overlay(product_type, family_product, entries)
        return {"changed": changed, "backup": backup, "no_op": False,
                "rules_rev": eval_debug.bump_rules_rev()}

    path = eval_debug.rules_files()["signatures"]
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    by_id = {s.get("id"): s for s in (doc.get("signatures") or [])}
    changed = []
    for sig_id in sig_ids:
        target = by_id[sig_id]
        if (target.get("enabled") is not False) == enabled:
            continue                          # 이미 그 상태
        if enabled:
            target.pop("enabled", None)       # 기본값이므로 키를 지운다
        else:
            target["enabled"] = False
        changed.append(sig_id)

    if not changed:
        return {"changed": [], "backup": None, "no_op": True,
                "rules_rev": eval_debug.rules_rev()}
    backup = _backup(path)
    _write_atomic(path, _head_comments(path) + _dump(doc))
    return {"changed": changed, "backup": backup, "no_op": False,
            "rules_rev": eval_debug.bump_rules_rev()}


def _validate_condition(metric: str, cond, threshold_keys: set) -> None:
    if not _NAME_RE.match(str(metric)):
        raise RuleError(f"잘못된 metric 이름: {metric}")
    if isinstance(cond, (list, tuple)):
        # 같은 지표에 상·하한을 함께 거는 밴드(AND) — 엔진 `signatures._eval_condition` 과
        # 같은 규약. 각 항목을 따로 검증한다.
        if not cond:
            raise RuleError(f"{metric}: 조건 목록이 비어 있음")
        for one in cond:
            _validate_condition(metric, one, threshold_keys)
        return
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


def _validate_scope(value) -> dict | None:
    """scope 검증 — taxonomy 에 있는 값만 허용. 둘 다 비면 None(= 전 제품 공통, 키 제거)."""
    if not isinstance(value, dict):
        raise RuleError("scope 는 객체여야 함")
    tax = eval_debug.taxonomy()
    out = {}
    for key in SCOPE_KEYS:
        raw = value.get(key) or []
        if not isinstance(raw, list):
            raise RuleError(f"scope.{key} 는 배열이어야 함")
        allowed = set(tax) if key == "product_type" else {f for fs in tax.values() for f in fs}
        picked = [str(v) for v in raw if str(v)]
        unknown = [v for v in picked if v not in allowed]
        if unknown:
            raise RuleError(f"scope.{key}: 알 수 없는 값 {unknown}")
        if picked:
            out[key] = picked
    return out or None


def _validate_signature_payload(payload: dict) -> dict:
    """패널이 보낸 필드만 뽑아 검증 — 반환은 그대로 yaml 에 쓸 수 있는 값."""
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
        elif field == "scope":
            updates["scope"] = _validate_scope(value)
        else:
            updates[field] = str(value or "")
    return updates


def save_signature(sig_id: str, payload: dict, product_type: str | None = None,
                   family_product: str | None = None) -> dict:
    """기존 signature 1건 갱신. 신규 추가/삭제는 허용하지 않는다.

    제품군을 고르면 **그 범위 오버레이 파일에만** 쓴다 — 상속값과 같은 필드는 기록하지
    않으므로(thresholds 와 같은 규약) 기준값을 고치면 지정하지 않은 제품군은 따라간다.
    제품군을 안 고르면 종전대로 기준값 signatures.yaml 을 고친다.
    """
    if product_type:
        _check_scope(product_type, family_product or None)
    else:
        family_product = None

    base_ids = {s.get("id") for s in eval_debug.signatures_raw()}
    if sig_id not in base_ids:
        raise RuleError(f"없는 signature: {sig_id} (신규 추가는 이 화면에서 지원하지 않음)")

    updates = _validate_signature_payload(payload)
    warnings = []
    if updates.get("enabled") is False and sig_id in eval_debug.specificity_order():
        warnings.append("비활성화해도 status.py SPECIFICITY_ORDER 항목은 그대로 남습니다(무해).")

    if product_type:
        dropped = [f for f in updates if f not in SIGNATURE_OVERLAY_FIELDS]
        if dropped:
            warnings.append(f"제품군별 저장에서는 {dropped} 를 다루지 않아 무시했습니다.")
        inherited = _inherited_row(sig_id, product_type, family_product)
        entries = _read_sig_overlay(product_type, family_product)
        before = copy.deepcopy(entries)
        entry = dict(entries.get(sig_id) or {})
        for field in SIGNATURE_OVERLAY_FIELDS:
            if field not in updates:
                continue
            if _same_as_inherited(field, updates[field], inherited):
                entry.pop(field, None)
            else:
                entry[field] = updates[field]
        if entry:
            entries[sig_id] = entry
        else:
            entries.pop(sig_id, None)
        scope_label = f"{product_type}/{family_product or '_default'}"
        if entries == before:
            return {"id": sig_id, "updated": sorted(entry), "backup": None, "no_op": True,
                    "scope": scope_label, "rules_rev": eval_debug.rules_rev(),
                    "warnings": warnings}
        backup = _write_sig_overlay(product_type, family_product, entries)
        return {"id": sig_id, "updated": sorted(entry), "backup": backup, "no_op": False,
                "scope": scope_label,
                "rules_rev": eval_debug.bump_rules_rev(), "warnings": warnings}

    path = eval_debug.rules_files()["signatures"]
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    target = next((s for s in (doc.get("signatures") or []) if s.get("id") == sig_id), None)
    if target is None:
        raise RuleError(f"없는 signature: {sig_id} (신규 추가는 이 화면에서 지원하지 않음)")
    before = copy.deepcopy(doc)
    for key, value in updates.items():
        if key in ("issue_category", "scope") and value is None:
            target.pop(key, None)          # 전 제품 공통 = 키 자체를 두지 않는다
        elif key == "enabled" and value is True:
            target.pop("enabled", None)          # 기본값이므로 키를 지워 원본 형태 유지
        else:
            target[key] = value

    if doc == before:
        return {"id": sig_id, "updated": sorted(updates), "backup": None, "no_op": True,
                "scope": "", "rules_rev": eval_debug.rules_rev(), "warnings": warnings}
    backup = _backup(path)
    _write_atomic(path, _head_comments(path) + _dump(doc))
    rev = eval_debug.bump_rules_rev()
    return {"id": sig_id, "updated": sorted(updates), "backup": backup, "no_op": False,
            "scope": "", "rules_rev": rev, "warnings": warnings}


def reset_signature(sig_id: str, product_type: str, family_product: str | None) -> dict:
    """이 범위의 signature 전용 설정을 통째로 지운다(= 상속값으로 되돌리기)."""
    _check_scope(product_type, family_product or None)
    entries = _read_sig_overlay(product_type, family_product or None)
    if sig_id not in entries:
        return {"id": sig_id, "removed": False, "backup": None,
                "rules_rev": eval_debug.rules_rev()}
    entries.pop(sig_id)
    backup = _write_sig_overlay(product_type, family_product or None, entries)
    return {"id": sig_id, "removed": True, "backup": backup,
            "rules_rev": eval_debug.bump_rules_rev()}


# ── 평가 제외 목록 ────────────────────────────────────────────────────────────

EXCLUSION_KEYS = ("item_contains", "units")
_EXCLUSION_MAX_LEN = 100
_EXCLUSION_MAX_COUNT = 200


def read_exclusions() -> dict:
    """rules/exclusions.yaml — 패널 표시용 (전 제품군 공통)."""
    return {**eval_debug.exclusions(), "rules_rev": eval_debug.rules_rev()}


def save_exclusions(payload: dict) -> dict:
    """제외 목록 전체 재작성 (백업 → 원자적 쓰기 → rev +1 — thresholds 와 같은 순서).

    rev 를 올리므로 ai_comment 옵션 세션의 캐시가 무효화돼 저장 즉시 재평가된다.
    """
    if not isinstance(payload, dict):
        raise RuleError("payload 는 객체여야 함")
    out = {}
    for key in EXCLUSION_KEYS:
        raw = payload.get(key, [])
        if not isinstance(raw, list):
            raise RuleError(f"{key} 는 배열이어야 함")
        if len(raw) > _EXCLUSION_MAX_COUNT:
            raise RuleError(f"{key}: 최대 {_EXCLUSION_MAX_COUNT}개")
        vals, seen = [], set()
        for v in raw:
            s = str(v).strip()
            if not s:
                continue
            if len(s) > _EXCLUSION_MAX_LEN:
                raise RuleError(f"{key}: {_EXCLUSION_MAX_LEN}자 이하만 — {s[:20]}…")
            if s.upper() in seen:               # 매칭이 대소문자 무시라 중복도 무시 기준으로
                continue
            seen.add(s.upper())
            vals.append(s)
        out[key] = vals
    if out == eval_debug.exclusions():
        return {"saved": out, "backup": None, "no_op": True,
                "rules_rev": eval_debug.rules_rev()}
    path = eval_debug.rules_files()["exclusions"]
    backup = _backup(path)
    _write_atomic(path, _head_comments(path) + _dump(out))
    return {"saved": out, "backup": backup, "no_op": False,
            "rules_rev": eval_debug.bump_rules_rev()}


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
        if s.get("scope") is not None:
            try:
                _validate_scope(s.get("scope"))
            except RuleError as exc:
                problems.append(f"[{sig_id}] {exc}")
    for missing in sorted(sig_ids - order):
        problems.append(f"[{missing}] status.py SPECIFICITY_ORDER 에 없음 — primary 정렬 누락")
    for extra in sorted(order - sig_ids):
        notes.append(f"SPECIFICITY_ORDER 에만 있는 id: {extra}")

    # 1b) suppressed_by 참조 무결성 + 순환. 엔진은 원본 발화 집합 기준 1패스라 순환이
    # 나도 무한루프는 아니지만, 상호 참조는 "둘 다 사라지는" 오동작이라 문제로 본다.
    suppress = {s.get("id"): _norm_suppressed_by(s.get("suppressed_by"))
                for s in eval_debug.signatures_raw()}
    for sig_id, targets in suppress.items():
        for target in targets:
            if target not in sig_ids:
                problems.append(f"[{sig_id}] suppressed_by 가 없는 signature 를 가리킴: {target}")
            elif sig_id in suppress.get(target, []):
                problems.append(f"[{sig_id}] {target} 와 suppressed_by 상호 참조 — "
                                "둘 다 발화하면 양쪽이 사라집니다")

    # 1c) hidden_by / replaces 참조 무결성. 억제와 달리 이 둘은 발화를 **목록에서 지우므로**
    # 없는 id 를 가리키면 조용히 아무 일도 일어나지 않는다(오타가 침묵한다).
    raw_sigs = eval_debug.signatures_raw()
    for key in ("hidden_by", "replaces"):
        rel = {s.get("id"): _norm_suppressed_by(s.get(key)) for s in raw_sigs}
        for sig_id, targets in rel.items():
            for target in targets:
                if target not in sig_ids:
                    problems.append(f"[{sig_id}] {key} 가 없는 signature 를 가리킴: {target}")
                elif target == sig_id:
                    problems.append(f"[{sig_id}] {key} 가 자기 자신을 가리킴")
                elif sig_id in rel.get(target, []):
                    problems.append(f"[{sig_id}] {target} 와 {key} 상호 참조 — "
                                    "둘 다 발화하면 양쪽이 사라집니다")

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

    # 2b) signature 오버레이 트리 점검 (고아 폴더/파일, 없는 id·필드)
    sig_tree = eval_debug.rules_dir() / "signatures"
    if sig_tree.is_dir():
        for pt_dir in sorted(p for p in sig_tree.iterdir() if p.is_dir()):
            if pt_dir.name not in tax:
                problems.append(f"고아 폴더: signatures/{pt_dir.name} (허용 product_type 아님)")
                continue
            for f in sorted(pt_dir.glob("*.yaml")):
                if f.stem != "_default" and f.stem not in tax[pt_dir.name]:
                    problems.append(f"고아 파일: signatures/{pt_dir.name}/{f.name}")
                doc = yaml.safe_load(f.read_text(encoding="utf-8"))
                entries = (doc or {}).get("signatures") if isinstance(doc, dict) else None
                if not isinstance(entries, dict):
                    problems.append(f"signatures/{pt_dir.name}/{f.name}: signatures 매핑이 없음")
                    continue
                for key, value in entries.items():
                    where = f"signatures/{pt_dir.name}/{f.name}: {key}"
                    if key not in sig_ids:
                        problems.append(f"{where} — signatures.yaml 에 없는 id")
                    elif not isinstance(value, dict):
                        problems.append(f"{where} — 필드 매핑이 아님")
                    else:
                        for field in value:
                            if field not in SIGNATURE_OVERLAY_FIELDS:
                                problems.append(f"{where}.{field} — 다룰 수 없는 필드")
    else:
        notes.append("signature 오버레이 트리 없음 — 전 제품군이 기준값을 그대로 사용 중")

    # 3) 전 조합 병합 시뮬레이션 (KeyError 유발 조합 조기 발견)
    # 관계·타입 불변식도 여기서 본다 — 저장 시엔 "이번에 바꾼 키" 만 검사하므로
    # 상위 층(default·legacy 섹션)에 이미 있던 위반은 이 전역 점검에서만 드러난다.
    seen_value = set()
    for pt, families in tax.items():
        for family in [None] + list(families):
            merged = eval_debug.effective_thresholds(pt, family)
            for msg in _check_threshold_values(merged, set(merged)):
                if msg not in seen_value:
                    seen_value.add(msg)
                    problems.append(f"{pt}/{family or '_default'}: {msg}")
            for s in eval_debug.signatures_scoped(pt, family):
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

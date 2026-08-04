"""배포 rules/*.yaml 자체의 정합성 — 코드가 아니라 **선언 파일**을 검사한다.

signatures.yaml 은 임계값을 문자열 이름으로 참조하므로(`">outlier_ratio_bad"`), thresholds.yaml
에서 키가 사라지거나 이름이 바뀌면 파이썬은 조용히 넘어가고 런타임에야 KeyError 가 난다.
실제로 2026-08-03 `subpop_cdf_gap_warn` 삭제가 이 유형이었다. 룰은 코드 리뷰 없이 관리자
화면(/pe/eval)에서도 고칠 수 있으니, 파일만 보고 잡을 수 있는 것은 여기서 잡는다.

`enabled` 와 무관한 검사라 rules_as_deployed 마커는 쓰지 않는다(켜진 룰만 보면 꺼진 룰의
깨진 참조를 놓친다 — 나중에 다시 켜는 순간 터진다).
"""
import pathlib
import re

from eval_engine import config
from eval_engine.pipeline import status
from eval_engine.pipeline._rules import load_yaml

# signatures.py `_eval_condition` 의 조건식 문법과 같은 형태.
# 서버 eval_panel/rules_io.py 도 같은 규칙을 갖지만 import 하지 않는다(의존 방향 단방향).
_COND_RE = re.compile(r"(abs)?\s*([<>]=?)\s*(.+)")

_ISSUE_CATEGORIES = {"YIELD", "CPK", "ETC"}


def _signatures():
    return load_yaml(str(config.SIGNATURES_FILE))["signatures"]


def _default_thresholds():
    return load_yaml(str(config.THRESHOLDS_FILE))["default"]


def test_every_condition_reference_exists_in_thresholds():
    """조건이 가리키는 임계값 이름이 thresholds.yaml default 에 전부 있어야 한다."""
    default = _default_thresholds()
    missing = []
    for sig in _signatures():
        for metric, cond in (sig.get("when_metric") or {}).items():
            m = _COND_RE.match(str(cond).strip())
            assert m, f"{sig['id']}.{metric}: 조건식 문법 위반 {cond!r}"
            ref = m.group(3).strip()
            try:
                float(ref)          # 리터럴 임계값은 참조가 아니다
            except ValueError:
                if ref not in default:
                    missing.append(f"{sig['id']}.{metric} -> {ref}")
    assert not missing, f"thresholds.yaml default 에 없는 참조: {missing}"


def test_every_code_referenced_threshold_exists():
    """파이프라인 코드가 `th["키"]` 로 직접 읽는 임계값도 default 에 있어야 한다.

    선언형 when_metric 은 위 테스트가 보지만, subpop_*/edge_region_pct 처럼 **코드가
    직접 첨자로 읽는** 키는 어디에도 선언되지 않아 삭제해도 파일 검사로는 안 잡힌다.
    (`subpop_cdf_gap_warn` 삭제가 이 부류였고, 그때는 우연히 features 테스트가 KeyError
    로 대신 잡아줬다 — 우연에 기대지 않으려고 여기서 전수 대조한다.)
    """
    default = _default_thresholds()
    src_dir = pathlib.Path(status.__file__).parent
    used = {}
    for path in sorted(src_dir.glob("*.py")):
        for key in re.findall(r'th\["([a-z0-9_]+)"\]', path.read_text(encoding="utf-8")):
            used.setdefault(key, path.name)
    assert used, "th[\"...\"] 참조를 하나도 못 찾았다 — 정규식이 코드와 어긋났다"
    missing = {k: f for k, f in used.items() if k not in default}
    assert not missing, f"thresholds.yaml default 에 없는 코드 참조: {missing}"


def test_condition_operators_are_supported():
    """_eval_condition 이 실제로 아는 연산자만 쓴다(오타 연산자는 KeyError 로 터진다)."""
    for sig in _signatures():
        for metric, cond in (sig.get("when_metric") or {}).items():
            op = _COND_RE.match(str(cond).strip()).group(2)
            assert op in {">", ">=", "<", "<="}, f"{sig['id']}.{metric}: {cond!r}"


def test_signature_ids_match_specificity_order():
    """SPECIFICITY_ORDER 는 signature 전집합과 1:1 이어야 primary 선택이 결정적이다.

    빠진 id 는 동률일 때 순서가 리스트 밖으로 밀려 `top[0]`(발화 순서)에 좌우되고,
    남는 id 는 지워진 룰의 잔재다.
    """
    ids = {s["id"] for s in _signatures()}
    order = set(status.SPECIFICITY_ORDER)
    assert ids - order == set(), f"SPECIFICITY_ORDER 누락: {ids - order}"
    assert order - ids == set(), f"SPECIFICITY_ORDER 잔재: {order - ids}"
    assert len(status.SPECIFICITY_ORDER) == len(order), "SPECIFICITY_ORDER 에 중복 id"


def test_signature_ids_are_unique():
    ids = [s["id"] for s in _signatures()]
    assert len(ids) == len(set(ids)), f"중복 signature id: {ids}"


def test_declared_fields_are_within_vocabulary():
    """status_hint / issue_category 어휘 고정 — 오타는 status 매핑에서 조용히 무시된다."""
    for sig in _signatures():
        assert sig.get("status_hint") in status.SEVERITY_RANK, \
            f"{sig['id']}: 알 수 없는 status_hint {sig.get('status_hint')!r}"
        cat = sig.get("issue_category")
        assert cat is None or cat in _ISSUE_CATEGORIES, \
            f"{sig['id']}: 알 수 없는 issue_category {cat!r}"


def test_enabled_flag_is_boolean_when_present():
    """`enabled: "false"`(문자열)는 파이썬에서 참이라 룰이 안 꺼진다 — 조용한 함정."""
    for sig in _signatures():
        if "enabled" in sig:
            assert isinstance(sig["enabled"], bool), \
                f"{sig['id']}: enabled 가 bool 이 아님 ({sig['enabled']!r})"

"""AI Comment 이봉(BIMODALITY) 배지 회귀 테스트 (2026-08-03, 2026-08-12 개명).

실행:
    python tests/test_ai_comment_modality.py

eval_engine 을 호출하지 않는다 — `present.to_result` 가 돌려주는 case dict 모양만
합성해 `_modality_tag` / `_cell_text` / `_to_row_keys` 를 검증한다(DB·룰 무의존).

핵심 계약 2가지:
  1. BIMODALITY 가 **primary 가 아니어도** 배지가 붙는다 (엔진의 primary 편중 우회).
  2. BIMODALITY 미발화 케이스의 셀 텍스트는 **종전과 문자 그대로 동일**하다.

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report.ai_comment import (_cell_text, _modality_tag,  # noqa: E402
                                   _rank, _to_row_keys)


def _subpop_sig(note, role="secondary"):
    """signatures._evaluate_subpop_gap 이 만드는 evidence 모양 그대로."""
    return {"id": "BIMODALITY", "role": role, "action_ko": None,
            "evidence": [{"signal_code": "MODALITY_V2", "value": None, "note": note},
                         {"signal_code": "N_MODES", "value": 2, "note": "n_modes 2"},
                         {"signal_code": "DENSITY_GAP", "value": 12.5, "note": "cdf_gap 12.5"}]}


def _case(status="MAJOR", comment="[현상] 산포가 넓습니다.", signatures=None,
          item="VDD_TEST", bin_=5):
    return {"item_raw": item, "bin": bin_, "status": status, "comment": comment,
            "primary_signature": "WIDE_DISTRIBUTION", "secondary_signatures": [],
            "signatures": signatures if signatures is not None else [],
            "evidence": [], "precedents": []}


def test_no_subpop_is_byte_identical():
    """BIMODALITY 미발화 → 배지 없음 + 종전 포맷과 완전 일치."""
    case = _case()
    assert _modality_tag(case) == ""
    assert _cell_text(case) == "[MAJOR] [현상] 산포가 넓습니다."
    # signatures 키가 아예 없는(구 엔진) case 도 안전해야 한다.
    legacy = {"status": "MINOR", "comment": "x"}
    assert _modality_tag(legacy) == ""
    assert _cell_text(legacy) == "[MINOR] x"


def test_modality_labels():
    for note, tag in (("modality_v2 bimodal", "[이봉]"),
                      ("modality_v2 multimodal", "[다봉]"),
                      ("modality_v2 separated", "[분리]")):
        case = _case(signatures=[_subpop_sig(note)])
        assert _modality_tag(case) == tag, note
        assert _cell_text(case) == f"[MAJOR]{tag} [현상] 산포가 넓습니다.", note


def test_badge_when_not_primary():
    """primary 는 WIDE_DISTRIBUTION 인데도 배지가 붙어야 한다 (이 기능의 존재 이유)."""
    case = _case(signatures=[
        {"id": "WIDE_DISTRIBUTION", "role": "primary", "evidence": [], "action_ko": None},
        _subpop_sig("modality_v2 bimodal", role="secondary"),
    ])
    assert case["primary_signature"] == "WIDE_DISTRIBUTION"
    assert _cell_text(case).startswith("[MAJOR][이봉] ")


def test_note_format_drift_falls_back():
    """note 포맷이 바뀌어도 '발화했다' 는 사실은 잃지 않는다(조용한 미표시 방지)."""
    assert _modality_tag(_case(signatures=[_subpop_sig("모달리티 두봉")])) == "[분포분리]"
    # MODALITY_V2 evidence 자체가 사라져도 마찬가지.
    sig = {"id": "BIMODALITY", "evidence": [{"signal_code": "N_MODES", "note": "n_modes 2"}]}
    assert _modality_tag(_case(signatures=[sig])) == "[분포분리]"
    # evidence 가 통째로 없어도 예외 없이 폴백.
    assert _modality_tag(_case(signatures=[{"id": "BIMODALITY"}])) == "[분포분리]"


def test_status_missing_keeps_tag_only():
    case = _case(status="", signatures=[_subpop_sig("modality_v2 bimodal")])
    assert _cell_text(case) == "[이봉] [현상] 산포가 넓습니다."
    # status·comment 둘 다 없으면 배지만 남고 공백이 새지 않는다.
    bare = _case(status="", comment="", signatures=[_subpop_sig("modality_v2 bimodal")])
    assert _cell_text(bare) == "[이봉]"


def test_rank_tiebreak_prefers_modality():
    plain = _case()
    bimodal = _case(signatures=[_subpop_sig("modality_v2 bimodal")])
    assert _rank(bimodal) > _rank(plain)          # 같은 MAJOR — 이봉이 이긴다
    # severity 는 여전히 1순위: 이봉이어도 CRITICAL 을 이기지 못한다.
    critical = _case(status="CRITICAL")
    assert _rank(critical) > _rank(bimodal)


def test_to_row_keys_keeps_bimodal_on_tie():
    """같은 item 의 CPK/ETC 폴백 키에 이봉 케이스가 남아야 한다."""
    plain = _case(item="VDD", bin_=5)
    bimodal = _case(item="VDD", bin_=7, signatures=[_subpop_sig("modality_v2 bimodal")])
    # dict 삽입 순서상 plain 이 먼저 — tie-break 가 없으면 plain 이 남는다.
    out = _to_row_keys({("VDD", 5): plain, ("VDD", 7): bimodal})["comments"]
    assert out["Yield|5|VDD"] == "[MAJOR] [현상] 산포가 넓습니다."
    assert out["Yield|7|VDD"] == "[MAJOR][이봉] [현상] 산포가 넓습니다."
    assert out["CPK|VDD"].startswith("[MAJOR][이봉] ")
    assert out["ETC|VDD"].startswith("[MAJOR][이봉] ")


def test_to_row_keys_etc_auto_item_carries_badge():
    """fail bin 이 없어 ETC 자동 행으로만 나오는 이봉 항목도 배지를 달고 나온다."""
    bimodal = _case(item="IDD", bin_=1,  # PASS bin → Yield 행이 안 생긴다
                    signatures=[_subpop_sig("modality_v2 separated")])
    res = _to_row_keys({("IDD", 1): bimodal})
    assert res["etc_auto_items"] == ["IDD"]
    assert res["comments"]["ETC|IDD"].startswith("[MAJOR][분리] ")
    assert "Yield|1|IDD" not in res["comments"]


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()

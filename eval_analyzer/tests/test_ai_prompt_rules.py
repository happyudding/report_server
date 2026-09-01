"""rules/ai_prompt.yaml — 운영자 지시문이 **LLM 프롬프트까지 실제로 흘러가는지**.

`_rules.ai_prompt_instructions()` 단독 검사로는 부족하다(CLAUDE.md "배선을 보라"): 로더가
맞아도 `_build_prompt` 가 그 목록을 안 이으면 관리자가 저장한 지시가 조용히 무시되고,
증상은 "저장했는데 LLM 이 안 따른다" 로만 나타난다.

보는 것:
  - 로더 — enabled 만·순서 유지·빈 text 제외, 파일 없으면 [] (에러 아님)
  - 배선 — 지시 문장이 프롬프트에 들어가고 **고정 지시문 뒤·재료 앞**에 온다
  - 하위호환 — 지시가 비면 프롬프트가 종전과 **바이트 동일**(서버 사본의 sha 계약 전제)
  - 배포 yaml — 실제로 파싱되고 사례를 버리지 말라는 지시가 켜져 있다
"""
import pytest

from eval_engine import config
from eval_engine.pipeline import _rules, recommend

_DOC = {
    "instructions": [
        {"id": "keep", "enabled": True, "text": "사례를 버리지 마라."},
        {"id": "off", "enabled": False, "text": "꺼진 문장"},
        {"id": "blank", "enabled": True, "text": "   "},
        {"id": "no_distortion", "enabled": True, "text": "왜곡하지 마라."},
    ],
    # 서버 전용(push 필터) — 엔진은 읽지 않는다
    "deny_patterns": [{"id": "x", "enabled": True, "regex": "사례가 없"}],
}

_VERDICT = {"status": "MAJOR", "primary_signature": "LOW_CPK",
            "secondary_signatures": []}
_SIG = {"signatures": [{"id": "LOW_CPK", "role": "primary"}]}
_CTX = {"item_canonical": "vref_trim", "item_class": "ANALOG|V"}


def _prompt():
    return recommend._build_prompt(_CTX, _VERDICT, _SIG, [], "현상", "과거", "조치")


def test_loader_filters_and_order(monkeypatch):
    monkeypatch.setattr(_rules, "ai_prompt_doc", lambda: _DOC)
    assert _rules.ai_prompt_instructions() == ["사례를 버리지 마라.", "왜곡하지 마라."]
    # doc 을 직접 줘도 같은 규칙(호출부가 이미 읽어 둔 경우)
    assert _rules.ai_prompt_instructions(_DOC) == ["사례를 버리지 마라.", "왜곡하지 마라."]
    assert _rules.ai_prompt_instructions({}) == []


def test_missing_file_is_not_an_error(monkeypatch, tmp_path):
    """파일이 없어도 평가가 죽지 않는다 — 지시는 부가 재료다."""
    monkeypatch.setattr(config, "AI_PROMPT_FILE", tmp_path / "nope.yaml")
    assert _rules.ai_prompt_doc() == {}
    assert _rules.ai_prompt_instructions() == []


def test_instructions_reach_the_prompt(monkeypatch):
    """저장한 지시가 프롬프트에 실리고, 위치는 고정 지시문 뒤·재료 앞이다."""
    monkeypatch.setattr(_rules, "ai_prompt_doc", lambda: {"instructions": []})
    base = _prompt()
    monkeypatch.setattr(_rules, "ai_prompt_doc", lambda: _DOC)
    got = _prompt()
    assert "사례를 버리지 마라." in got and "왜곡하지 마라." in got
    assert "꺼진 문장" not in got
    # 재료(item: …) 뒤로 밀리면 지시가 아니라 데이터로 읽힌다
    assert got.index("반도체 fail item") < got.index("사례를 버리지 마라.") \
        < got.index("item: vref_trim")


def test_empty_rules_keep_prompt_byte_identical(monkeypatch):
    """지시가 없으면 종전 프롬프트 그대로 — 서버 사본(ai_prompt.py)의 sha 계약 전제."""
    monkeypatch.setattr(_rules, "ai_prompt_doc", lambda: {})
    a = _prompt()
    monkeypatch.setattr(_rules, "ai_prompt_doc", lambda: {"instructions": []})
    assert _prompt() == a
    monkeypatch.setattr(_rules, "ai_prompt_doc",
                        lambda: {"instructions": [{"id": "x", "enabled": False,
                                                   "text": "무시"}]})
    assert _prompt() == a


@pytest.mark.rules_as_deployed
def test_shipped_yaml_is_valid():
    """배포 yaml 이 파싱되고 '사례를 버리지 마라' 지시가 켜져 있다(사용자 결정 2026-09-01)."""
    doc = _rules.ai_prompt_doc()
    assert doc.get("instructions"), "배포 ai_prompt.yaml 에 지시문이 없다"
    texts = " ".join(_rules.ai_prompt_instructions())
    assert "사례" in texts and "왜곡" in texts, texts
    # 서버가 쓰는 금지 문구도 함께 배포된다(엔진은 안 읽지만 같은 파일이다)
    import re
    for row in doc.get("deny_patterns") or []:
        re.compile(row["regex"])          # 깨진 정규식이 배포되면 필터가 조용히 죽는다

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
from pathlib import Path

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


# ── 2026-09-02 재설계 — 코드 뼈대 3섹션 + LLM 두 블록 덧칠 ──────────────────

_SIG2 = {"signatures": [
    {"id": "LOW_CPK", "role": "primary", "action_ko": "spec 재검토"},
    {"id": "EDGE_FAIL", "role": "secondary", "action_ko": "edge 이력 확인"},
]}
_VERDICT2 = {"status": "MAJOR", "primary_signature": "LOW_CPK",
             "secondary_signatures": ["EDGE_FAIL"]}
_PRECS = [
    {"human_comment": "재측정으로 회복", "product_name": "P1", "lot_id": "L1"},
    {"human_comment": "trim 재조정", "product_name": "P2", "lot_id": "L2"},
    {"human_comment": None, "product_name": "P3"},          # 코멘트 없음 → 제외
]


def test_past_case_lists_every_precedent():
    """[사례] 는 회수된 **전부** — 1위 하나만 인용하던 종전 동작이 신고의 한 축이었다."""
    text = recommend._past_case_text(_PRECS)
    assert "재측정으로 회복" in text and "trim 재조정" in text
    assert "①" in text and "②" in text and "③" not in text   # 코멘트 없는 행은 안 센다
    assert "(P1/L1)" in text and "(P2/L2)" in text
    # 개행은 접는다 — 셀 한 섹션 안에 들어가야 한다
    multi = [{"human_comment": "첫 줄\n둘째 줄", "product_name": "P1"}]
    assert "\n" not in recommend._past_case_text(multi)
    # 0건이면 종전 문구
    assert recommend._past_case_text([]) == recommend._NO_PRECEDENT_TEXT
    assert recommend._past_case_text([{"human_comment": None}]) == recommend._NO_PRECEDENT_TEXT


def test_phenomenon_and_actions_cover_all_signatures():
    """[현상]·[제안] 기본값은 발화 **전부**, primary 가 맨 앞."""
    phen = recommend._phenomenon_text(_VERDICT2, _SIG2, _CTX)
    assert "LOW_CPK" in phen and "EDGE_FAIL" in phen
    assert phen.index("LOW_CPK") < phen.index("EDGE_FAIL")

    acts = recommend._action_lines(_VERDICT2, _CTX, _SIG2)
    assert "- LOW_CPK: spec 재검토" in acts and "- EDGE_FAIL: edge 이력 확인" in acts
    assert acts.index("LOW_CPK") < acts.index("EDGE_FAIL")
    # 같은 문장은 한 번만
    dup = {"signatures": [{"id": "A", "role": "primary", "action_ko": "같은 조치"},
                          {"id": "B", "role": "secondary", "action_ko": "같은 조치"}]}
    assert recommend._action_lines({"primary_signature": "A"}, _CTX, dup).count("같은 조치") == 1


def test_make_comment_skips_llm_without_precedents(monkeypatch):
    """사례 0건이면 **LLM 을 아예 부르지 않는다**(토큰·시간 절약, 사용자 결정)."""
    calls = []
    monkeypatch.setattr(recommend.llm_client, "is_enabled", lambda: True)
    monkeypatch.setattr(recommend.llm_client, "complete",
                        lambda *a, **kw: calls.append(a) or "[제안] LLM 문장")
    out = recommend.make_comment(_CTX, _VERDICT2, _SIG2, [])
    assert calls == [], "선례가 없는데 LLM 을 불렀다"
    assert recommend._NO_PRECEDENT_TEXT in out
    assert "- LOW_CPK: spec 재검토" in out and "- EDGE_FAIL: edge 이력 확인" in out
    assert out.startswith("[현상] ") and "\n[사례] " in out and "[제안] " in out

    # 사례가 있으면 부르고, 두 블록이 각 섹션을 교체한다
    calls.clear()
    monkeypatch.setattr(recommend.llm_client, "complete",
                        lambda *a, **kw: calls.append(a) or "[사례] 요약본\n[제안] - 통합 제안")
    out2 = recommend.make_comment(_CTX, _VERDICT2, _SIG2, _PRECS)
    assert len(calls) == 1
    assert "[사례] 요약본" in out2 and "[제안] - 통합 제안" in out2
    assert "재측정으로 회복" not in out2, "코드 나열이 요약으로 안 바뀌었다"

    # LLM 이 [제안] 만 내면 [사례] 는 코드 나열이 남는다(빈 섹션 금지)
    monkeypatch.setattr(recommend.llm_client, "complete", lambda *a, **kw: "- 제안만")
    out3 = recommend.make_comment(_CTX, _VERDICT2, _SIG2, _PRECS)
    assert "재측정으로 회복" in out3 and "[제안] - 제안만" in out3

    # 호출이 터져도 코멘트는 나온다(코드 뼈대 유지)
    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(recommend.llm_client, "complete", _boom)
    out4 = recommend.make_comment(_CTX, _VERDICT2, _SIG2, _PRECS)
    assert "- LOW_CPK: spec 재검토" in out4 and "재측정으로 회복" in out4


def test_section_tokens_are_fixed():
    """섹션 토큰은 [현상]/[사례]/[제안] 고정 (CLAUDE.md §5 규칙 12)."""
    assert (recommend.SEC_PHEN, recommend.SEC_CASE, recommend.SEC_SUGG) \
        == ("[현상]", "[사례]", "[제안]")
    out = recommend.make_comment(_CTX, _VERDICT2, _SIG2, [])
    assert "[과거사례]" not in out and "[점검제안]" not in out
    # cross_source 의 SOURCE_ONLY_FAIL 코멘트도 같은 토큰을 써야 한다
    from eval_engine import cross_source
    src = (Path(cross_source.__file__)).read_text(encoding="utf-8")
    assert "recommend.SEC_CASE" in src and "[과거사례]" not in src


def test_parse_llm_blocks_engine():
    """관대 파싱 3분기 — 서버 사본(web_report/ai_prompt.py)과 같은 동작이어야 한다."""
    assert recommend.parse_llm_blocks("[사례] A\n[제안] B") == ("A", "B")
    assert recommend.parse_llm_blocks("[과거사례] A\n[점검제안] B") == ("A", "B")
    assert recommend.parse_llm_blocks("[제안] B") == (None, "B")
    assert recommend.parse_llm_blocks("그냥 문장") == (None, "그냥 문장")
    assert recommend.parse_llm_blocks("") == (None, None)

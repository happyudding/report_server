"""L5 Recommend — 룰 골격 + 선례(precedent) + (옵션) LLM 합성 → 분석방향 comment.

find_precedents: 선례검색을 precedent_client 어댑터에 위임(sql 기본 | rag 교체).
  반환 dict 계약: action/result/human_comment, 관련도 내림차순. docs/PRECEDENT_RAG_HANDOFF.md.
  코멘트 생성 판단은 human_comment 만 사용(action/result 는 benchtest 표시용 참고 metadata).
make_comment:
  - LLM off(config.EVAL_LLM_ENABLED=False) 또는 실패 → 룰/선례 기반 템플릿 코멘트 fallback.
  - LLM on → llm_client.complete(prompt) 로 자연어 합성(모델은 사용자 지정).
"""
from .. import llm_client, precedent_client
from ._rules import signatures_doc


def find_precedents(case_ctx: dict, sig_result: dict) -> list:
    return precedent_client.search(case_ctx, sig_result)


def _template_comment(case_ctx, verdict, sig_result, precedents) -> str:
    by_id = {s["id"]: s for s in signatures_doc()["signatures"]}
    primary = verdict.get("primary_signature")
    base = by_id[primary].get("action_ko") if primary in by_id else None
    if not base:
        base = "추가 데이터 확인 필요"
    comments = [p["human_comment"] for p in precedents if p.get("human_comment")]
    if comments:
        base += f" (과거 사례 {len(comments)}건: " + " / ".join(comments) + ")"
    return base


def _build_prompt(case_ctx, verdict, sig_result, precedents, template) -> str:
    lines = [
        "반도체 fail item 분석방향 코멘트를 한국어 한 문장으로 작성하라.",
        f"item: {case_ctx.get('item_canonical')} / class: {case_ctx.get('item_class')}",
        f"status: {verdict.get('status')} / primary: {verdict.get('primary_signature')}",
        f"secondary: {', '.join(verdict.get('secondary_signatures', []))}",
        f"룰 골격: {template}",
    ]
    comments = [p["human_comment"] for p in precedents if p.get("human_comment")]
    if comments:
        lines.append("선례 comment 목록:")
        lines.extend(f"- {c}" for c in comments)
    return "\n".join(lines)


def make_comment(case_ctx: dict, verdict: dict, sig_result: dict, precedents: list,
                 *, model_version: str | None = None) -> str:
    template = _template_comment(case_ctx, verdict, sig_result, precedents)
    if llm_client.is_enabled():
        try:
            prompt = _build_prompt(case_ctx, verdict, sig_result, precedents, template)
            return llm_client.complete(prompt, model_version=model_version)
        except Exception:
            pass  # LLM 실패 → 템플릿 fallback
    return template

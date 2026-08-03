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

_MODALITY_V2_COMMENT = { 
    "bimodal": "분포가 2개 level로 분리되는 양상입니다.", 
    "multimodal": "분포가 여러 level로 분리되는 양상입니다.", 
    "separated": "분포가 하나의 중심으로 모이지 않고 분리되는 양상입니다.", }

_NO_PHENOMENON_FALLBACK = "엔지니어 확인 필요" 
_NO_PRECEDENT_TEXT = "참고할 수 있는 과거 사례가 없습니다."


def find_precedents(case_ctx: dict, sig_result: dict) -> list:
    return precedent_client.search(case_ctx, sig_result)


def _subpop_gap_comment(sig_result) -> str | None:
    for s in sig_result.get("signatures", []): 
        if s["id"] == "SUBPOP_GAP" and s.get("modality_v2"):
            return _MODALITY_V2_COMMENT.get(s["modality_v2"])
    return None

def _signature_by_id() -> dict:
    return {s["id"]: s for s in signatures_doc()["signatures"]} 


def _phenomenon_text(verdict, sig_result) -> str:
    by_id = _signature_by_id()
    primary = verdict.get("primary_signature")
    text = by_id[primary].get("phenomenon_ko") if primary in by_id else None
    if primary == "SUBPOP_GAP":
        text = _subpop_gap_comment(sig_result) or text
    return text or _NO_PHENOMENON_FALLBACK

def _action_ko_for(verdict) -> str:
    by_id = _signature_by_id()
    primary = verdict.get("primary_signature")
    return (by_id[primary].get("action_ko") if primary in by_id else None) or _NO_PHENOMENON_FALLBACK

def _past_case_text(precedents) -> str:
    comments = [p["human_comment"] for p in precedents if p.get("human_comment")]
    if not comments:
        return _NO_PRECEDENT_TEXT
    top = precedents[0]
    product = top.get("product_name")
    prefix = f"{product} 에서 " if product else ""
    return f"{prefix} 유사 사례가 확인 되었습니다 - {comments[0]}"




def _build_prompt(case_ctx, verdict, sig_result, precedents, phenomenon,past_case, action_ko) -> str:
    lines = [
        "반도체 fail item 분석 이후 다음에 확인해야 할 점검 방향을 한국어로 한 문장"
        "(최대 두 짧은 문장)만 제안하라."
        "원인을 확정적으로 단정하지 마라"
        "현재 입력이나 과거 사례에 없는 수치 제품명 설비 사이트 원인을 만들지 마라."
        "과거 사례의 조치를 정답으로 단정하지마라"
        "아래 [현상] / [과거사례] 문장을 반복하지 마라."
        "점검 순서 또는 확인 대상을 중심으로 작성하라."
        "[현상], [과거사례], [점검 제안] 같은 섹션 제목은 출력하지 마라 - 문장만 출력하라",
        f"item: {case_ctx.get('item_canonical')} / class: {case_ctx.get('item_class')}",
        f"status: {verdict.get('status')} / primary: {verdict.get('primary_signature')}",
        f"secondary: {', '.join(verdict.get('secondary_signatures', []))}",
        f"[현상] {phenomenon}",
        f"[과거사례] {past_case}",
        f"참고용 기본 조치(action_ko):{ action_ko}"
    ]
    
    return "\n".join(lines)


def make_comment(case_ctx: dict, verdict: dict, sig_result: dict, precedents: list,
                 *, model_version: str | None = None) -> str:
    phenomenon = _phenomenon_text(verdict,sig_result)
    past_case = _past_case_text(precedents)
    action_ko = _action_ko_for(verdict)
    suggestion = action_ko
    if llm_client.is_enabled():
        try:
            prompt = _build_prompt(case_ctx, verdict, sig_result, precedents, phenomenon, past_case, action_ko)
            llm_out = (llm_client.complete(prompt, model_version=model_version) or "").strip()
            suggestion = llm_out or action_ko
        except Exception:
            suggestion = action_ko
            
    return f"[현상] {phenomenon}\n[과거사례] {past_case} \n [점검제안] {suggestion}"




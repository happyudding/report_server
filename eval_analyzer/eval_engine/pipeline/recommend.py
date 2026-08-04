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
    """선례검색을 precedent_client 어댑터에 위임(sql 기본 | rag 교체). 관련도 내림차순 리스트.

    ⚠ 손타이핑 사본에서 통째로 사라졌다가 구버전 기준으로 복원한 함수다
    (VERIFY_CHECKLIST §4 ★).
    """
    return precedent_client.search(case_ctx, sig_result)


def _subpop_gap_comment(sig_result) -> str | None:
    """발화 signature 중 SUBPOP_GAP 의 modality_v2 → 한국어 현상 문구. 없으면 None."""
    for s in sig_result.get("signatures", []): 
        if s["id"] == "SUBPOP_GAP" and s.get("modality_v2"):
            return _MODALITY_V2_COMMENT.get(s["modality_v2"])
    return None

def _signature_by_id() -> dict:
    """signatures.yaml 의 signature 목록을 id → 항목 dict 로 색인."""
    return {s["id"]: s for s in signatures_doc()["signatures"]} 


def _phenomenon_text(verdict, sig_result) -> str:
    """[현상] 섹션 문구 — primary signature 의 phenomenon_ko.

    SUBPOP_GAP 만 modality_v2(bimodal/multimodal/separated)별 문구로 덮어쓴다. 같은
    signature 라도 분포 모양이 달라 한 문장으로 뭉뚱그릴 수 없기 때문.
    """
    by_id = _signature_by_id()
    primary = verdict.get("primary_signature")
    text = by_id[primary].get("phenomenon_ko") if primary in by_id else None
    if primary == "SUBPOP_GAP":
        text = _subpop_gap_comment(sig_result) or text
    return text or _NO_PHENOMENON_FALLBACK

def _action_ko_for(verdict) -> str:
    """[점검제안] 의 기본값 — primary signature 의 action_ko. LLM 실패 시 폴백으로도 쓰인다."""
    by_id = _signature_by_id()
    primary = verdict.get("primary_signature")
    return (by_id[primary].get("action_ko") if primary in by_id else None) or _NO_PHENOMENON_FALLBACK

def _past_case_text(precedents) -> str:
    """[과거사례] 섹션 문구 — 관련도 1위 선례의 human_comment 를 제품명과 함께 인용.

    사람이 쓴 코멘트가 하나도 없으면 `_NO_PRECEDENT_TEXT`. action/result 는 쓰지 않는다
    (benchtest 표시용 참고 metadata 일 뿐 코멘트의 근거가 아니다).
    """
    comments = [p["human_comment"] for p in precedents if p.get("human_comment")]
    if not comments:
        return _NO_PRECEDENT_TEXT
    top = precedents[0]
    product = top.get("product_name")
    prefix = f"{product} 에서 " if product else ""
    return f"{prefix} 유사 사례가 확인 되었습니다 - {comments[0]}"




def _build_prompt(case_ctx, verdict, sig_result, precedents, phenomenon,past_case, action_ko) -> str:
    """LLM 합성용 프롬프트 — 지시문 + case 요약 + 이미 만들어 둔 [현상]/[과거사례]/action_ko.

    지시문의 목적은 **환각 억제**다: 원인 단정 금지, 입력이나 선례에 없는 수치·제품명·설비를
    지어내지 말 것, 섹션 제목 출력 금지(문장만) — LLM 출력이 [점검제안] 자리에만 들어가기
    때문이다.
    ⚠ 지시문 8줄은 콤마 없는 암시적 문자열 연결로 한 덩어리다. 원본 의도일 수 있어 손대지
    않았다(VERIFY_CHECKLIST §2-3).
    """
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
    """L5 진입점 — [현상]/[과거사례]/[점검제안] 3섹션 comment 문자열.

    앞 두 섹션은 항상 룰·선례에서 만든다. [점검제안]만 LLM 이 켜져 있을 때 자연어로
    합성하고, **꺼져 있거나 호출이 실패하면 action_ko 로 조용히 폴백**한다 — LLM 유무와
    무관하게 코멘트는 항상 나와야 하므로 예외를 위로 던지지 않는다.
    """
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




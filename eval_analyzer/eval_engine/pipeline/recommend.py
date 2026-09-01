"""L5 Recommend — 룰 골격 + 선례(precedent) + (옵션) LLM 합성 → 분석방향 comment.

find_precedents: 선례검색을 precedent_client 어댑터에 위임(sql 기본 | rag 교체).
  반환 dict 계약: action/result/human_comment, 관련도 내림차순. docs/PRECEDENT_RAG_HANDOFF.md.
  코멘트 생성 판단은 human_comment 만 사용(action/result 는 benchtest 표시용 참고 metadata).
make_comment:
  - LLM off(config.EVAL_LLM_ENABLED=False) 또는 실패 → 룰/선례 기반 템플릿 코멘트 fallback.
  - LLM on → llm_client.complete(prompt) 로 자연어 합성(모델은 사용자 지정).
"""
from .. import llm_client, precedent_client
from ._rules import ai_prompt_instructions, signatures_for
from .signatures import _BIMODALITY_ID

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
    """발화 signature 중 BIMODALITY 의 modality_v2 → 한국어 현상 문구. 없으면 None."""
    for s in sig_result.get("signatures", []):
        if s["id"] == _BIMODALITY_ID and s.get("modality_v2"):
            return _MODALITY_V2_COMMENT.get(s["modality_v2"])
    return None

def _signature_by_id(case_ctx=None) -> dict:
    """signature 목록을 id → 항목 dict 로 색인 (제품군 오버레이 반영)."""
    return {s["id"]: s for s in signatures_for(case_ctx)}


def _phenomenon_text(verdict, sig_result, case_ctx=None) -> str:
    """[현상] 섹션 문구 — primary signature 의 phenomenon_ko.

    BIMODALITY 만 modality_v2(bimodal/multimodal/separated)별 문구로 덮어쓴다. 같은
    signature 라도 분포 모양이 달라 한 문장으로 뭉뚱그릴 수 없기 때문.
    """
    by_id = _signature_by_id(case_ctx)
    primary = verdict.get("primary_signature")
    text = by_id[primary].get("phenomenon_ko") if primary in by_id else None
    if primary == _BIMODALITY_ID:
        text = _subpop_gap_comment(sig_result) or text
    return text or _NO_PHENOMENON_FALLBACK

def _fired_by_id(sig_result) -> dict:
    """발화 signature 를 id → 발화 항목으로 색인.

    yaml 원문(`signatures_for`)이 아니라 **발화 결과**를 봐야 하는 곳이 있다: action_ko 의
    `{키}` 자리는 L3 가 발화 시점에 실제 값으로 채워 두므로(`signatures._fill_action`),
    문구는 발화 항목 쪽이 정본이다.
    """
    return {s["id"]: s for s in (sig_result or {}).get("signatures", []) if s.get("id")}


def _action_ko_for(verdict, case_ctx=None, sig_result=None) -> str:
    """[제안] 의 기본값 — primary signature 의 action_ko. LLM 실패 시 폴백으로도 쓰인다.

    `sig_result` 가 있으면 **발화 항목의 action_ko** 를 먼저 쓴다 — L3 가 `{dut_top}` 같은
    자리를 그 case 의 실제 값으로 이미 채워 놓았다. yaml 원문은 그 값이 비어 있는 폴백이다.
    """
    by_id = _signature_by_id(case_ctx)
    primary = verdict.get("primary_signature")
    fired = _fired_by_id(sig_result).get(primary) or {}
    text = fired.get("action_ko") or (
        by_id[primary].get("action_ko") if primary in by_id else None)
    return text or _NO_PHENOMENON_FALLBACK

def _fired_signature_lines(sig_result, case_ctx=None) -> str:
    """LLM 프롬프트에 넣을 **발화 signature 전체** 목록 — 한 줄에 하나(현상+기본조치).

    `_phenomenon_text`/`_action_ko_for` 가 primary 하나만 쓰는 것과 의도가 다르다:
    [제안] 은 걸린 룰을 전부 종합해야 하므로 secondary 까지 재료로 준다. 문구는 같은
    출처(`signatures_for` 의 phenomenon_ko/action_ko)를 쓰므로 표현이 갈리지 않는다.
    yaml 에 문구가 없는 id 는 id 만 싣는다(발화 사실 자체를 잃지 않기 위해).
    """
    by_id = _signature_by_id(case_ctx)
    out = []
    for s in (sig_result or {}).get("signatures", []):
        sid = s.get("id")
        if not sid:
            continue
        meta = by_id.get(sid) or {}
        # action_ko 는 **발화 항목** 것을 먼저 쓴다 — L3 가 `{dut_top}` 같은 자리를 그
        # case 의 실제 값으로 채워 두었다. yaml 원문이 들어가면 LLM 이 `{dut_top}` 을
        # 그대로 옮기거나 값을 지어낸다.
        parts = [x for x in (meta.get("phenomenon_ko"),
                             s.get("action_ko") or meta.get("action_ko")) if x]
        role = s.get("role") or ""
        head = f"- {sid}" + (f"({role})" if role else "")
        out.append(f"{head}: {' / '.join(parts)}" if parts else head)
    return "\n".join(out)


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


def _precedent_lines(precedents) -> str:
    """LLM 프롬프트에 넣을 **선례 전량** 목록 — 한 줄에 하나(제품/lot 출처 포함).

    `_past_case_text` 가 1위 하나만 인용하는 것과 의도가 다르다: [제안] 은 발화한 룰
    전부와 확보된 사례 전부를 종합해야 하므로, 프롬프트에는 회수된 코멘트를 모두 준다
    (상한은 호출측 `config.EVAL_PRECEDENT_TOPK` 가 이미 걸어 둔다).
    사람 코멘트가 있는 선례만 싣는다 — action/result 만 있는 행은 문장 재료가 안 된다.
    """
    lines = []
    for p in precedents or ():
        comment = str(p.get("human_comment") or "").strip()
        if not comment:
            continue
        product = str(p.get("product_name") or "").strip()
        lot = str(p.get("lot_id") or "").strip()
        src = " / ".join(x for x in (product, lot) if x)
        lines.append(f"- {src + ': ' if src else ''}{comment}")
    return "\n".join(lines)




def _build_prompt(case_ctx, verdict, sig_result, precedents, phenomenon,past_case, action_ko) -> str:
    """LLM 합성용 프롬프트 — 지시문 + case 요약 + 이미 만들어 둔 [현상]/[과거사례]/action_ko.

    지시문의 목적은 **환각 억제**다: 원인 단정 금지, 입력이나 선례에 없는 수치·제품명·설비를
    지어내지 말 것, 섹션 제목 출력 금지(문장만) — LLM 출력이 [제안] 자리에만 들어가기
    때문이다.

    2026-08-28 두 가지를 바꿨다.
    ① **발화 signature 전체**(primary 뿐 아니라 secondary 와 각 룰의 기본 조치까지)와
       **회수된 선례 전량**을 재료로 준다. 종전에는 primary 1개 + 선례 1위 인용문 하나만
       들어가, 여러 축으로 걸린 case 도 한 축짜리 제안이 됐다.
    ② 사례를 **버리지 말라**고 명시한다. 종전 지시문에는 "정답으로 단정하지 마라" 만
       있어서, 사례가 실제로 2건 회수됐는데도 LLM 이 "직접 적용할 수 있는 사례는 없다"고
       스스로 결론내 버리는 출력이 나왔다(사용자 신고). 조건이 완전히 같지 않아도 참고할
       점을 살려 쓰는 것이 이 섹션의 목적이다.
    분량은 최대 5줄 — 종합 재료가 늘어난 만큼 한 문장으로는 담기지 않는다.

    2026-09-02: base 지시문 **뒤에** `rules/ai_prompt.yaml` 의 운영자 지시를 잇는다
    (`ai_prompt_instructions`). "사례를 버리지 마라" 류의 조건은 앞으로도 계속 늘어나는데
    그때마다 코드를 고치면 배포가 필요하고 문구 이력도 남지 않는다 — 관리자
    `/pe/eval` "AI 지시문" 탭에서 편집·백업·rev 증가로 관리한다.
    ⚠ lines[0] 리터럴은 **바이트 그대로 유지**할 것 — web_report/ai_prompt.py `_INSTRUCTION`
    이 이 값의 사본이고 tests/test_ai_prompt_determinism.py (e) 가 ast 로 대조한다.
    """
    sig_lines = _fired_signature_lines(sig_result, case_ctx)
    prec_lines = _precedent_lines(precedents)
    lines = [
        "반도체 fail item 분석 이후 다음에 확인해야 할 점검 방향을 한국어로 제안하라."
        "발화한 signature 전체와 아래 과거 사례를 종합해서 작성하라 - 하나만 보고 쓰지 마라."
        "최대 5줄로 쓰고 각 줄은 '- ' 로 시작하는 짧은 항목으로 만들어라."
        "원인을 확정적으로 단정하지 마라"
        "현재 입력이나 과거 사례에 없는 수치 제품명 설비 사이트 원인을 만들지 마라."
        "과거 사례의 조치를 정답으로 단정하지마라"
        "다만 과거 사례가 주어졌다면 조건이 완전히 같지 않더라도 참고할 점을 최대한 살려서 반영하라."
        "'적용할 수 있는 사례가 없다' 같이 주어진 사례를 버리는 문장은 쓰지 마라."
        "아래 [현상] / [과거사례] 문장을 그대로 반복하지 마라."
        "점검 순서 또는 확인 대상을 중심으로 작성하라."
        "[현상], [과거사례], [점검 제안] 같은 섹션 제목은 출력하지 마라 - 문장만 출력하라",
        f"item: {case_ctx.get('item_canonical')} / class: {case_ctx.get('item_class')}",
        f"status: {verdict.get('status')} / primary: {verdict.get('primary_signature')}",
        f"secondary: {', '.join(verdict.get('secondary_signatures', []))}",
        "[발화 signature 전체]",
        sig_lines or "- (없음)",
        f"[현상] {phenomenon}",
        "[과거사례 목록]",
        prec_lines or f"- {_NO_PRECEDENT_TEXT}",
        f"참고용 기본 조치(action_ko):{ action_ko}"
    ]
    # 운영자 지시(yaml)는 base 지시문 **직후**에 넣는다 — 재료(item/status/…)보다 앞이어야
    # 지시로 읽힌다. 파일이 없거나 전부 꺼져 있으면 종전 프롬프트와 바이트 동일하다.
    lines[1:1] = ai_prompt_instructions()

    return "\n".join(lines)


def make_comment(case_ctx: dict, verdict: dict, sig_result: dict, precedents: list,
                 *, model_version: str | None = None) -> str:
    """L5 진입점 — [현상]/[과거사례]/[제안] 3섹션 comment 문자열.

    앞 두 섹션은 항상 룰·선례에서 만든다. [제안]만 LLM 이 켜져 있을 때 자연어로
    합성하고, **꺼져 있거나 호출이 실패하면 action_ko 로 조용히 폴백**한다 — LLM 유무와
    무관하게 코멘트는 항상 나와야 하므로 예외를 위로 던지지 않는다.
    """
    phenomenon = _phenomenon_text(verdict, sig_result, case_ctx)
    past_case = _past_case_text(precedents)
    action_ko = _action_ko_for(verdict, case_ctx, sig_result)
    suggestion = action_ko
    if llm_client.is_enabled():
        try:
            prompt = _build_prompt(case_ctx, verdict, sig_result, precedents, phenomenon, past_case, action_ko)
            llm_out = (llm_client.complete(prompt, model_version=model_version) or "").strip()
            suggestion = llm_out or action_ko
        except Exception:
            suggestion = action_ko
            
    return f"[현상] {phenomenon}\n[과거사례] {past_case} \n [제안] {suggestion}"




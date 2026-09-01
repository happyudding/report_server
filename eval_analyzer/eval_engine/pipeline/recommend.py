"""L5 Recommend — 룰 골격 + 선례(precedent) + (옵션) LLM 합성 → 분석방향 comment.

find_precedents: 선례검색을 precedent_client 어댑터에 위임(sql 기본 | rag 교체).
  반환 dict 계약: action/result/human_comment, 관련도 내림차순. docs/PRECEDENT_RAG_HANDOFF.md.
  코멘트 생성 판단은 human_comment 만 사용(action/result 는 benchtest 표시용 참고 metadata).
make_comment:
  - LLM off(config.EVAL_LLM_ENABLED=False) 또는 실패 → 룰/선례 기반 템플릿 코멘트 fallback.
  - LLM on **그리고 선례가 1건 이상일 때만** → llm_client.complete(prompt) 로 두 블록 합성.

**설계(2026-09-02 재설계)**: 코드가 먼저 **완성된 코멘트**를 만들고, LLM 은 있을 때만 그 위에
덧칠한다. LLM 이 무엇을 쓰든(또는 안 오든) 화면이 틀리지 않는 것이 요점이다.
  [현상] 발화 signature **전부**의 phenomenon_ko
  [사례]  회수된 선례 **전부**의 코멘트 원문   → LLM 이 오면 "있는 그대로 요약" 으로 교체
  [제안] 발화 signature **전부**의 action_ko  → LLM 이 오면 "통합 제안" 으로 교체
선례가 0건이면 LLM 을 아예 부르지 않는다 — 사례 대조가 이 프롬프트의 존재 이유라 재료가
없으면 토큰·시간만 쓴다(사용자 결정).
"""
import json
import re

from .. import llm_client, precedent_client
from ._rules import ai_prompt_instructions, signatures_for
from .signatures import _BIMODALITY_ID

# ── 섹션 토큰 (불변 계약 — ../../../CLAUDE.md §5 규칙 12) ────────────────────
# 프롬프트·LLM 출력 계약·엔진 출력·서버 파싱(web_report/ai_prompt.py)·화면 라벨
# (static/webreport/sheets.js)이 **같은 이름**을 쓴다. 옛 토큰 `[과거사례]`(→사례) ·
# `[점검제안]`(→제안) 은 캐시·저장 문장·Excel 에 굳어 있어 **읽기만** 계속 허용한다.
SEC_PHEN = "[현상]"
SEC_CASE = "[사례]"
SEC_SUGG = "[제안]"

_MODALITY_V2_COMMENT = {
    "bimodal": "분포가 2개 level로 분리되는 양상입니다.",
    "multimodal": "분포가 여러 level로 분리되는 양상입니다.",
    "separated": "분포가 하나의 중심으로 모이지 않고 분리되는 양상입니다.", }

_NO_PHENOMENON_FALLBACK = "엔지니어 확인 필요"
# 사례가 없을 때 **화면 [사례] 자리**에 들어가는 값 — 사용자 요청(2026-09-02)으로 문장을
# 없애고 대시 하나만 둔다("참고할 수 있는 …" 을 매번 읽을 필요가 없다).
# ⚠ 프롬프트 재료로는 이 값을 쓰지 않는다 — 사례가 0건이면 LLM 을 아예 부르지 않는다.
_NO_PRECEDENT_TEXT = "-"
# 선례 나열의 번호표 — 6건 이상이면 그냥 숫자로 떨어진다(top-k 기본 5).
_PREC_MARKS = "①②③④⑤⑥⑦⑧⑨⑩"

# LLM 출력의 블록 토큰 — 신·구 둘 다 받는다(모델이 예시를 흉내낼 수 있다).
# 교대 왼쪽 우선이라 긴 옛 토큰을 앞에 둔다("[점검제안]" 이 "[제안]" 으로 잘리는 것 방지).
_LLM_BLOCK_RE = re.compile(r"\[(과거사례|사례|점검제안|제안)\]")


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


def _ordered_fired(verdict, sig_result) -> list:
    """발화 signature 를 **primary 먼저** 정렬해 돌려준다 (id 없는 행 제외).

    [현상]·[제안] 이 같은 순서를 써야 사용자가 두 섹션을 나란히 읽을 수 있다.
    """
    rows = [s for s in (sig_result or {}).get("signatures", []) if s.get("id")]
    primary = verdict.get("primary_signature")
    return sorted(rows, key=lambda s: 0 if s.get("id") == primary else 1)


def _phenomenon_text(verdict, sig_result, case_ctx=None) -> str:
    """[현상] 섹션 문구 — 발화 signature **전부**의 phenomenon_ko (primary 먼저).

    2026-09-02: 종전에는 primary 하나만 썼다. 여러 축으로 걸린 case 도 한 줄짜리 현상만
    보여, 사용자가 "다른 룰은 왜 떴는지" 를 화면에서 알 수 없었다(사용자 요청 "현상도
    전부 다 전달"). LLM 프롬프트에도 이 블록이 그대로 나간다.
    BIMODALITY 만 modality_v2(bimodal/multimodal/separated)별 문구로 덮어쓴다 — 같은
    signature 라도 분포 모양이 달라 한 문장으로 뭉뚱그릴 수 없기 때문.
    발화가 하나도 없으면 종전처럼 폴백 한 줄.
    """
    by_id = _signature_by_id(case_ctx)
    fired = _ordered_fired(verdict, sig_result)
    lines, seen = [], set()
    for s in fired:
        sid = str(s["id"])
        text = (by_id.get(sid) or {}).get("phenomenon_ko")
        if sid == _BIMODALITY_ID:
            text = _subpop_gap_comment(sig_result) or text
        text = str(text or "").strip()
        line = f"- {sid}: {text}" if text else f"- {sid}"
        if line not in seen:            # 같은 문구가 두 번 나오면 읽는 사람이 헷갈린다
            seen.add(line)
            lines.append(line)
    if not lines:
        return _NO_PHENOMENON_FALLBACK
    # 1건이어도 목록 형태를 유지한다 — 건수에 따라 모양이 달라지면 파서·테스트·사람이
    # 모두 두 경우를 따로 다뤄야 한다(화면은 [현상] 섹션을 숨기므로 손해도 없다).
    return "\n".join(lines)

def _fired_by_id(sig_result) -> dict:
    """발화 signature 를 id → 발화 항목으로 색인.

    yaml 원문(`signatures_for`)이 아니라 **발화 결과**를 봐야 하는 곳이 있다: action_ko 의
    `{키}` 자리는 L3 가 발화 시점에 실제 값으로 채워 두므로(`signatures._fill_action`),
    문구는 발화 항목 쪽이 정본이다.
    """
    return {s["id"]: s for s in (sig_result or {}).get("signatures", []) if s.get("id")}


def _action_ko_for(verdict, case_ctx=None, sig_result=None) -> str:
    """primary signature 의 action_ko 한 줄. (프롬프트 재료·하위호환 호출부용)

    `sig_result` 가 있으면 **발화 항목의 action_ko** 를 먼저 쓴다 — L3 가 `{dut_top}` 같은
    자리를 그 case 의 실제 값으로 이미 채워 놓았다. yaml 원문은 그 값이 비어 있는 폴백이다.
    ⚠ [제안] 기본값은 2026-09-02 부터 `_action_lines`(발화 전부)다 — 이 함수가 아니다.
    """
    by_id = _signature_by_id(case_ctx)
    primary = verdict.get("primary_signature")
    fired = _fired_by_id(sig_result).get(primary) or {}
    text = fired.get("action_ko") or (
        by_id[primary].get("action_ko") if primary in by_id else None)
    return text or _NO_PHENOMENON_FALLBACK


def _action_lines(verdict, case_ctx=None, sig_result=None) -> str:
    """[제안] 의 기본값 — 발화 signature **전부**의 action_ko (primary 먼저).

    2026-09-02 사용자 결정: "제안은 발화된 Signature 에 대한 모든 제안". 종전에는 primary
    하나만 나가서, 3개 축으로 걸린 항목도 한 가지 조치만 보였다.
    action_ko 는 **발화 항목** 것을 먼저 쓴다 — L3(`signatures._fill_action`)가 `{dut_top}`
    같은 자리를 그 case 의 실제 값으로 이미 채워 두었다(yaml 원문은 미치환 폴백).
    같은 문장이 여러 룰에 걸리면 한 번만 쓴다 — 사용자에게는 중복이 곧 잡음이다.
    """
    by_id = _signature_by_id(case_ctx)
    lines, seen = [], set()
    for s in _ordered_fired(verdict, sig_result):
        sid = str(s["id"])
        text = str(s.get("action_ko")
                   or (by_id.get(sid) or {}).get("action_ko") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(f"- {sid}: {text}")
    return "\n".join(lines) or _action_ko_for(verdict, case_ctx, sig_result)

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
    """[사례] 섹션 문구 — 회수된 선례 **전부**를 출처와 함께 나열.

    2026-09-02: 종전에는 관련도 1위 하나만 인용했다("… 에서 유사 사례가 확인 되었습니다 -").
    선례가 2건이어도 화면에는 1건처럼 보여, 사용자가 "사례가 있는데 왜 안 쓰나" 를 확인할
    방법이 없었다(사용자 신고). 상한은 호출측 `config.EVAL_PRECEDENT_TOPK`(기본 5)가 이미
    걸어 두므로 여기서 다시 자르지 않는다.

    코멘트 원문은 **자르지 않는다** — 어떻게 해결했는지가 뒤에 있어 먼저 잘려 나간다.
    다만 개행은 ` / ` 로 접는다(셀 한 섹션 안에 들어가야 하고, 개행이 있으면 서버 파싱
    `SECTION_RE` 의 섹션 경계와 섞여 읽기 어려워진다).
    사람이 쓴 코멘트가 하나도 없으면 `_NO_PRECEDENT_TEXT`. action/result 는 쓰지 않는다
    (benchtest 표시용 참고 metadata 일 뿐 코멘트의 근거가 아니다).
    """
    parts = []
    for p in precedents or ():
        comment = str(p.get("human_comment") or "").strip()
        if not comment:
            continue
        comment = " / ".join(x.strip() for x in comment.splitlines() if x.strip())
        product = str(p.get("product_name") or "").strip()
        lot = str(p.get("lot_id") or "").strip()
        src = "/".join(x for x in (product, lot) if x)
        mark = _PREC_MARKS[len(parts)] if len(parts) < len(_PREC_MARKS) \
            else f"({len(parts) + 1})"
        parts.append(f"{mark}{f'({src}) ' if src else ' '}{comment}")
    return " ".join(parts) if parts else _NO_PRECEDENT_TEXT


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




def unwrap_json_reply(text):
    """모델이 문장 대신 **JSON 객체**를 냈으면 그 안의 사람 문장만 꺼낸다.

    ⚠ **web_report/ai_prompt.py 에 같은 함수의 사본**이 있다(규칙 #8 로 import 불가) —
    한쪽을 고치면 다른 쪽도 고칠 것. 배경·정책은 그쪽 docstring 참조(2026-09-02 현장 신고:
    `{"precedent":…,"suggestion":{"text":…},"evidence_refs":…}` 가 [제안] 자리에 그대로 박혔다).
    JSON 이 아니면 원문 그대로 통과 — 정상 경로에서는 아무 일도 하지 않는다.
    """
    s = str(text or "").strip()
    if not (s.startswith("{") or s.startswith("[")):
        return s
    try:
        doc = json.loads(s)
    except (ValueError, TypeError):
        return s
    found = []
    def _walk(node, depth=0):
        if depth > 6 or len(found) >= 20:
            return
        if isinstance(node, str):
            t = node.strip()
            if len(t) >= 10:
                found.append(t)
        elif isinstance(node, dict):
            for key in ("text", "suggestion", "content", "message", "value"):
                if key in node:
                    _walk(node[key], depth + 1)
            for k, v in node.items():
                if k not in ("text", "suggestion", "content", "message", "value"):
                    _walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                _walk(v, depth + 1)
    _walk(doc)
    out, seen = [], set()
    for t in found:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return "\n".join(out)


def parse_llm_blocks(text):
    """LLM 출력 → (사례 요약|None, 제안|None). 두 블록 계약(2026-09-02)의 파서.

    프롬프트가 `[사례]` / `[제안]` 두 블록만 내라고 요구하지만 모델은 형식을 흘린다.
    관대하게 받는다:
      - 토큰이 둘 다 있으면 각 블록을 잘라 준다.
      - `[제안]` 만 있으면 (None, 그 뒤 전부) — 사례 요약은 코드 나열을 유지한다.
      - 토큰이 하나도 없으면 (None, 전체) — 종전(단일 [제안] 출력) 하위호환이다.
    옛 토큰 `[과거사례]`/`[점검제안]` 도 받는다(모델이 프롬프트 예시를 흉내낼 수 있다).
    ⚠ **web_report/ai_prompt.py 에 같은 함수의 사본**이 있다(규칙 #8 로 import 불가) —
    한쪽을 고치면 다른 쪽도 고칠 것. tests/test_ai_prompt_determinism.py 가 대조한다.
    """
    s = str(text or "").strip()
    if not s:
        return None, None
    marks = []
    for m in _LLM_BLOCK_RE.finditer(s):
        marks.append(("case" if m.group(1) in ("사례", "과거사례") else "sugg",
                      m.start(), m.end()))
    if not marks:
        return None, s
    blocks = {}
    for i, (kind, _st, end) in enumerate(marks):
        stop = marks[i + 1][1] if i + 1 < len(marks) else len(s)
        body = s[end:stop].strip()
        if body and kind not in blocks:      # 같은 토큰이 반복되면 첫 블록을 쓴다
            blocks[kind] = body
    return blocks.get("case"), blocks.get("sugg")


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
    2026-09-02 분량·문체를 바꿨다(사용자 결정). ① 5줄 상한 → **전체 12줄 + signature 하나당
       5줄** — 발화가 여럿이면 5줄로는 커버리지와 사례 대조가 동시에 안 됐다(같은 날
       10줄로 올렸다가 12줄로 다시 올렸다 — 사례가 있는 signature 의 구체적 판단 조치를
       줄이지 않으려면 여유가 더 필요했다).
       ② **내부 지표명·수치 출력 금지**(CPK·수율·단위 붙은 측정값만 예외) — `FAIL_MAD_MIN`
       `TAIL_MASS_3S_HIGH` 같은 이름은 읽는 사람이 모른다. ⚠ 프롬프트 **재료**의 수치
       (`[근거: …]`·선례의 당시 통계)는 그대로 둔다 — 그게 사라지면 "그때 값 vs 지금 값"
       대조가 원리적으로 불가능해진다. 금지는 **출력 문장**에만 건다.
       ③ [제안]의 중심을 action_ko 나열이 아니라 **사례**로 옮겼다 — 기본 조치 목록은
       사례로 안 덮이는 signature 를 메우는 보조 재료다.

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
        "반도체 fail item 분석 결과다. 답은 **정확히 아래 형식의 평문**으로만 쓴다.\n"
        "출력 형식(이 두 줄 머리말을 그대로 포함해서 쓴다):\n"
        "[사례] <사례 요약 문장들>\n"
        "[제안] <점검 제안 항목들>\n"
        "이 두 머리말 밖의 텍스트, 인사말, 코드펜스, JSON 이나 키-값 구조는 쓰지 마라.\n"
        "답 전체를 JSON 으로 감싸지 말고 사람이 읽는 문장만 쓴다.\n"
        "[사례] 에 쓸 것: 아래 사례 목록의 각 사례를 있는 그대로 한 줄씩 요약한다 - 제품/lot 당시 판단 근거 조치 결과.\n"
        "현재 현상에 적용할 수 있는지 판단하거나 평가하거나 부정하지 마라 - 요약만 하라.\n"
        "사례에 없는 내용을 만들지 마라.\n"
        "[제안] 에 쓸 것: 사례에서 무엇을 어떻게 확인해 해결했는지를 중심으로 지금 무엇을 어떤 순서로 확인할지 정리한다.\n"
        "기본 조치 목록은 사례로 덮이지 않는 signature 를 메우는 데만 쓰고 그대로 옮겨 적지 마라.\n"
        "[제안] 전체는 최대 12줄이고 한 signature 를 다루는 데 5줄을 넘기지 마라.\n"
        "각 줄은 '- ' 로 시작하고 핵심 단어만 남긴 짧은 문장으로 써라 - 수식어 배경설명 반복은 빼라.\n"
        "발화한 signature 전체가 다뤄져야 한다 - 원인이 이어지는 항목은 한 줄에 묶어도 되지만 빠뜨리지는 마라.\n"
        "지표 이름과 그 수치는 쓰지 마라 - FAIL_MAD_MIN TAIL_MASS_3S_HIGH spread_norm 같은 내부 지표명과 값은 읽는 사람이 모른다.\n"
        "CPK 와 수율 그리고 측정값 단위가 붙은 값은 필요하면 써도 된다.\n"
        "원인을 확정적으로 단정하지 마라. 사례의 조치를 정답으로 단정하지 마라.\n"
        "현재 입력이나 사례에 없는 수치 제품명 설비 사이트 원인을 만들지 마라.\n"
        "'적용할 수 있는 사례가 없다' 같이 주어진 사례를 버리는 문장은 쓰지 마라.",
        f"item: {case_ctx.get('item_canonical')} / class: {case_ctx.get('item_class')}",
        f"status: {verdict.get('status')} / primary: {verdict.get('primary_signature')}",
        f"secondary: {', '.join(verdict.get('secondary_signatures', []))}",
        "[발화 signature 전체]",
        sig_lines or "- (없음)",
        f"{SEC_PHEN} {phenomenon}",
        # 사례가 0건이면 `make_comment` 가 LLM 을 아예 안 부르므로 이 자리는 항상 채워진다
        # (도달 불가 폴백은 남겨 둔다 — 다른 호출부가 직접 부를 수 있다).
        "[사례 목록]",
        prec_lines or "- (없음)",
        "[기본 조치 목록(action_ko)]",
        action_ko,
    ]
    # 운영자 지시(yaml)는 base 지시문 **직후**에 넣는다 — 재료(item/status/…)보다 앞이어야
    # 지시로 읽힌다. 파일이 없거나 전부 꺼져 있으면 종전 프롬프트와 바이트 동일하다.
    lines[1:1] = ai_prompt_instructions()

    return "\n".join(lines)


def has_precedent_comments(precedents) -> bool:
    """LLM 을 부를 값어치가 있나 — 코멘트가 있는 선례가 1건이라도 있는가.

    사용자 결정(2026-09-02): **사례가 없으면 LLM 을 거치지 않는다.** 이 프롬프트의 존재
    이유가 "사례를 현재 수치와 대조" 라, 사례가 없으면 남는 재료(발화 signature + action_ko)는
    이미 코드가 완성해 둔 것이라 토큰·시간만 쓴다. 판정 기준은 `_past_case_text`/
    `_precedent_lines` 와 같다(코멘트 없는 선례는 문장 재료가 안 되므로 세지 않는다).
    """
    return any(str((p or {}).get("human_comment") or "").strip()
               for p in (precedents or ()))


def make_comment(case_ctx: dict, verdict: dict, sig_result: dict, precedents: list,
                 *, model_version: str | None = None) -> str:
    """L5 진입점 — [현상]/[사례]/[제안] 3섹션 comment 문자열.

    **세 섹션 모두 코드가 먼저 완성한다**(2026-09-02 재설계):
      [현상] 발화 signature 전부의 phenomenon_ko
      [사례]  회수된 선례 전부의 코멘트 원문
      [제안] 발화 signature 전부의 action_ko
    LLM 이 켜져 있고 **선례가 1건 이상**일 때만 호출해, 돌려받은 두 블록으로 [사례]/[제안]을
    각각 교체한다. 블록이 없거나 호출이 실패하면 코드 문장이 그대로 남는다 — LLM 유무와
    무관하게 코멘트는 항상 나와야 하므로 예외를 위로 던지지 않는다.
    """
    phenomenon = _phenomenon_text(verdict, sig_result, case_ctx)
    past_case = _past_case_text(precedents)
    actions = _action_lines(verdict, case_ctx, sig_result)
    suggestion = actions
    if llm_client.is_enabled() and has_precedent_comments(precedents):
        try:
            prompt = _build_prompt(case_ctx, verdict, sig_result, precedents,
                                   phenomenon, past_case, actions)
            llm_out = (llm_client.complete(prompt, model_version=model_version) or "").strip()
            cases, sugg = parse_llm_blocks(llm_out)
            # 모델이 문장 대신 JSON 객체를 내는 경우가 있다 — 그 안의 문장만 꺼낸다.
            cases, sugg = unwrap_json_reply(cases), unwrap_json_reply(sugg)
            # 블록이 안 온 쪽은 **코드 문장을 유지**한다 — 빈 섹션을 만들면 화면에서
            # "사례가 사라진" 것으로 보인다.
            past_case = cases or past_case
            suggestion = sugg or actions
        except Exception:
            suggestion = actions

    return f"{SEC_PHEN} {phenomenon}\n{SEC_CASE} {past_case} \n {SEC_SUGG} {suggestion}"




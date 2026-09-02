# -*- coding: utf-8 -*-
"""AI Comment [제안] — 클라 LLM 대행용 프롬프트 조립·패치 (docs/23).

**eval_engine 을 import 하지 않는다** — 규칙 #8(단방향 의존)이 엔진 import 를
`ai_comment.py`/`eval_export.py`/`eval_debug.py` **3곳으로 고정**하고 있고 이 파일은 그
셋이 아니다. 대신 `evaluate()` 가 돌려준 case dict(present.to_result 계약)만으로
프롬프트를 조립한다 — 재료는 호출부 `ai_comment.py` 가 넘겨 준다.
(이 구조는 2026-08-28 엔진 일시 동결기에 도입됐지만, 동결이 풀린 뒤에도 규칙 #8 때문에
그대로 옳다. 다만 "엔진을 못 고쳐서 우회한다" 는 근거는 더 이상 아니다 — 엔진이 줘야
맞는 재료는 실제로 엔진을 고쳐서 받아 온다. 아래 3번 참조.)

성립 근거: 서버 LLM 이 꺼져 있으면 case["comment"] 의 [제안] 섹션 == action_ko 그대로이고,
[현상]/[과거사례]·발화 signature(action_ko 포함)·선례가 전부 case dict 에 있다.
프롬프트는 이 경로 안에서만 생성 → sha 게이트 → 소비되는 **자기완결 계약**이라
엔진 프롬프트와 바이트 일치가 필요 없다. 단 지시문 텍스트는 recommend.py `_build_prompt`
의 **원문 사본**이다 — 드리프트는 tests/test_ai_prompt_determinism.py 가 파일 텍스트
대조로 감지한다(사본을 유지하는 이유: 서버 LLM 경로와 클라 대행 경로가 같은 지시로
답하게 하려고. 두 경로가 갈리면 같은 세션이 배선에 따라 다른 품질을 낸다).

엔진 원본과의 의도된 차이:
- signature 줄에 phenomenon_ko 가 없다(to_result 가 action_ko 만 싣는다) — action_ko 만 싣는다.

**이 경로의 보강** (2026-08-28) — 엔진 프롬프트에 없는 3가지:
1. `_INSTRUCTION_EXTRA` — 과거 Comment 를 "그때의 판단·해결 기록"으로 다루고 현재 값과
   대조하라는 지시. base `_INSTRUCTION`(사본)은 바이트 그대로 두고 뒤에 잇는다.
2. `[현재 통계]` 줄 + signature 줄의 `[근거: …]` — 종전 프롬프트에는 현재 케이스의 수치가
   하나도 없어 "비교"가 원리적으로 불가능했다. 재료는 `enrich`(호출부 조립).
3. 선례 상세 블록 — 당시 통계/feature/signature/unit/lot. **엔진이 실어 준다**
   (`store.search_precedents` 가 최신 run 의 raw_metrics/features 를 JOIN →
   `present._precedent_result` 가 계약 dict 에 담는다).
이 파일은 **순수 함수**를 유지한다 — DB 접근 없음, 재료는 전부 인자로 받는다.

**운영자 지시문·금지 문구** (2026-09-02, `rules` 인자): "사례를 버리는 문장을 쓰지 마라"
같은 조건은 앞으로도 계속 늘어나므로 코드가 아니라 `/pe/eval` "AI 지시문" 탭
(rules/ai_prompt.yaml)에서 관리한다. `instructions` 는 프롬프트 뒤에 붙고(= sha 가 갈려
기존 [제안] 폐기 → 재대행), `deny_patterns` 는 프롬프트에 들어가지 않고 push 수용 때
줄 단위 필터로만 쓰인다(sha 불변 — 다음 push 부터 적용).

**두 블록 계약** (2026-09-02 재설계, docs/23): LLM 은 `[사례]`(회수된 사례를 있는 그대로
요약) 와 `[제안]`(action_ko + 사례 근거 + 현재 수치를 통합한 확인 순서) **두 블록**만
낸다. 서버가 `parse_llm_blocks` 로 갈라 `patch_cell` 로 각 섹션을 교체하고, 안 온 블록은
코드가 만든 문장이 그대로 남는다. **선례가 0건이면 `build_prompt` 가 None** — 그 item 은
LLM 을 아예 거치지 않는다(토큰·시간 절약). 섹션 토큰 `[현상]/[사례]/[제안]` 은 불변
계약이다(../CLAUDE.md §5 규칙 12) — 옛 `[과거사례]`/`[점검제안]` 은 읽기만 호환.
"""
from __future__ import annotations

import hashlib
import json
import re

# ── 코멘트 평문 파싱 (형식 정본: recommend.make_comment — 규칙 12) ──────────
# 섹션 토큰은 [현상]/[사례]/[제안] 고정이고, 옛 토큰 [과거사례]/[점검제안] 은 **읽기만**
# 계속 받는다 — 그 평문이 payload·디스크 캐시·저장된 문장·Excel 에 굳어 있어서, 한쪽만
# 알면 그 세션들은 섹션 분리가 통째로 풀려 한 덩어리 평문이 된다(에러가 아니라 "색이
# 사라짐"으로 보인다). 캐시를 앞당겨 갈려고 전역 bump 를 하면 콜드 폭풍이 된다.
# ⚠ 교대는 왼쪽 우선 — 긴 옛 토큰(과거사례/점검제안)을 신 토큰보다 **앞**에 둔다.
SECTION_RE = re.compile(
    r"^\[현상\]\s*(?P<phen>.*?)\s*\n\[(?:과거사례|사례)\]\s*(?P<past>.*?)\s*\n\s*"
    r"\[(?:점검제안|제안)\]\s*(?P<sugg>.*)$", re.S)
# 마지막 섹션 값만 치환 — 토큰은 원문 것을 유지한다(옛 캐시 [점검제안] 도 그대로 치환).
SUGGEST_TAIL_RE = re.compile(r"(\[(?:점검제안|제안)\]\s*).*$", re.S)
# [사례] 섹션 값만 치환 — 뒤 [제안] 토큰 직전까지가 그 섹션의 본문이다.
CASE_SEC_RE = re.compile(
    r"(\[(?:과거사례|사례)\]\s*)(?:.*?)(\s*\n\s*\[(?:점검제안|제안)\])", re.S)
# suggestion 안에 끼면 섹션 파싱을 깨뜨리는 토큰들 (sanitize 에서 제거 — 블록 파싱 **뒤**)
_SECTION_TOKEN_RE = re.compile(r"\[(?:현상|과거사례|사례|점검제안|제안)\]")
# LLM 출력의 블록 토큰 — **recommend.parse_llm_blocks 의 사본**(규칙 #8 로 import 불가).
_LLM_BLOCK_RE = re.compile(r"\[(과거사례|사례|점검제안|제안)\]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")   # \n(0x0a) 은 살린다 — 아래 참조

# 엔진 프롬프트가 "'- ' 항목 여러 줄" 을 요구하므로(2026-08-28) 개행은 정상 출력이다.
# docs/23 초안의 500자 상한 → 1000자(5줄) → 1800자(10줄) → **2160자**(2026-09-02, 12줄).
# ⚠ 이 상한은 **잘라내기**다 — 모자라면 마지막 줄이 문장 중간에서 끊긴 채 저장된다.
# 줄 수 상한(_INSTRUCTION)을 올릴 때 이 값도 같이 볼 것. 한 줄을 "핵심 단어만" 으로
# 짧게 쓰라고 지시하므로 12줄 × 여유 180자면 실측 분량을 덮는다.
MAX_SUGGESTION_CHARS = 2160

# 프롬프트의 [사례 목록] 이 비었을 때의 자리표시 — **도달 불가**다(선례 0건이면
# `build_prompt` 가 None 을 돌려줘 LLM 을 아예 안 부른다). 화면 [사례] 의 "없음" 표시는
# 엔진 `recommend._NO_PRECEDENT_TEXT`("-", 2026-09-02 사용자 요청)가 만든다.
_NO_PRECEDENT_TEXT = "(없음)"

# 선례 상세·현재 통계의 **출력 순서 고정** — dict 순서에 기대면 프롬프트가 흔들려
# sha 게이트가 매번 갈린다(저장된 suggestion 이 전부 폐기된다). 값 이름은 eval DB 컬럼
# (raw_metrics / features)과 같은 이름을 그대로 쓴다 — 현재 통계도 같은 이름으로 내야
# LLM 이 "그때 cpk vs 지금 cpk" 로 읽는다.
_PRECEDENT_METRIC_ORDER = ("cpk", "cpl", "cpu", "cp", "mean", "stdev", "min", "max",
                           "yield", "fail_count", "total_count", "bimodality")
_PRECEDENT_FEATURE_ORDER = ("spread_norm", "outlier_ratio", "bimodality_score",
                            "limit_hit_ratio", "edge_fail_ratio", "center_fail_ratio",
                            "ring_fail_ratio", "fail_spread_norm", "tail_mass_3s",
                            "value_gap_ratio")

# recommend._build_prompt 지시문 **원문 사본** — 원본과 같은 "콤마 없는 암시적 문자열 연결
# 1덩어리" 를 유지한다(줄 사이 개행 없음). 원본이 바뀌면 여기도 같이 고칠 것
# (tests/test_ai_prompt_determinism.py 가 대조).
_INSTRUCTION = (
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
    "'적용할 수 있는 사례가 없다' 같이 주어진 사례를 버리는 문장은 쓰지 마라."
)

# 이 프로젝트에서 덧붙이는 지시 (vendor copy 아님 — 위 _INSTRUCTION 은 바이트 보존).
# 취지: 과거 Comment 는 그때 담당자가 **어떻게 판단하고 무엇으로 해결했는지** 적어 둔
# 기록이라 사실상 정답지다. 상세(당시 통계·signature·unit)를 현재 값과 대조하게 시켜,
# 단정은 피하되 활용도를 올린다.
# ⚠ 2026-09-02 에 "판단할 수 없는 부분은 … 무엇을 더 확인해야 하는지로 써라" 한 줄을
#   **뺐다** — 그 문장이 [사례] 블록에까지 걸려 "적용 가능한지 판단할 수 없다" 류 부정문의
#   명분이 됐다(사용자 신고의 문장). 커버리지 탈출구는 yaml `cover_all_signatures` 가
#   "목록에 없는 항목을 만들지는 마라" 로 대신한다.
_INSTRUCTION_EXTRA = (
    "사례의 Comment 는 그때 담당자가 무엇을 근거로 판단하고 어떻게 해결했는지 남긴 기록이다."
    "각 사례의 당시 통계 signature unit item 명을 현재 값과 하나씩 대조해서 무엇이 닮았고 무엇이 다른지 보라."
    "닮은 사례의 판단 근거와 조치는 [제안] 에서 현재 상황에 맞게 바꿔 구체적으로 녹여 써라."
    "[제안] 의 각 줄은 사례와 기본 조치를 **합쳐서** 쓴다 - 사례가 있는 signature 도 기본 조치를 함께 담아라(둘 중 하나만 고르지 마라)."
    "대조에 쓴 근거 수치와 내부 지표 이름은 문장에 옮겨 적지 마라 - 다만 사례에 적힌 현상 조치 판정(무엇을 어떻게 해서 어떻게 됐는지)은 그대로 살려 써라."
    "12줄은 상한이지 채워야 하는 목표가 아니다 - 발화 signature 를 전부 덮고 나서도 쓸 근거가 없으면 줄 수를 줄여라."
    "발화 목록에 없는 항목을 지어내 줄을 채우지 마라 - 줄 수를 맞추려고 일반론을 넣지 마라."
)


def instruction_lines(rules) -> list:
    """운영자 지시문(rules/ai_prompt.yaml `instructions`) → 프롬프트에 넣을 문장 목록.

    엔진 `_rules.ai_prompt_instructions` 와 **같은 규칙**이다(enabled 만, 선언 순서 유지,
    빈 text 제외). 두 경로가 같은 지시를 받아야 서버 LLM 과 클라 대행이 같은 품질을 낸다.
    재료는 호출부(ai_comment)가 `eval_debug.ai_prompt_rules()` 로 읽어 넘긴다 — 이 모듈은
    순수 함수라 파일을 열지 않는다.
    """
    out = []
    for item in (rules or {}).get("instructions") or []:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        text = str(item.get("text") or "").strip()
        if text:
            out.append(text)
    return out


def compile_deny_patterns(rules) -> list:
    """금지 문구(`deny_patterns`) → [(id, only_with_precedents, compiled regex)].

    프롬프트에는 들어가지 않는다(sha 불변) — 클라가 push 한 [제안] 을 서버가 받을 때
    줄 단위로 거르는 데만 쓴다. 컴파일 실패는 **건너뛴다**: 저장 시 `/pe/eval` 이 이미
    검증하므로 평시엔 없고, 손으로 고친 yaml 때문에 push 전체가 막히면 안 된다.
    """
    out = []
    for item in (rules or {}).get("deny_patterns") or []:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        raw = str(item.get("regex") or "").strip()
        if not raw:
            continue
        try:
            out.append((str(item.get("id") or ""),
                        item.get("only_with_precedents") is not False,
                        re.compile(raw)))
        except re.error:
            continue
    return out


def strip_denied_lines(text, patterns, has_precedents: bool) -> str:
    """[사례]/[제안] 에서 금지 문구에 걸리는 **줄만** 제거. 전부 걸리면 "".

    줄 단위인 이유: 두 블록 모두 '- ' 항목 여러 줄이라, 사례를 버리는 한 줄이 섞여 있어도
    나머지 항목은 쓸모가 있다. 통째로 버리면 사용자는 룰 문장으로 되돌아간 것만 본다.
    `only_with_precedents` 패턴은 선례가 실제로 프롬프트에 실린 item 에만 적용한다 —
    사례가 0건인 item 의 "참고할 사례가 없어 …" 는 **사실**이라 지우면 왜곡이 된다.

    ⚠ 각 줄을 **원문과 공백 제거본 두 벌로** 검사한다(2026-09-02). 한국어는 같은 뜻이
    띄어쓰기만 다르게 나오고("확인되지 않았습니다" ↔ "확인 되지 않았습니다"), 정규식마다
    `\\s*` 를 빠짐없이 끼워 넣는 것은 사람이 계속 실수한다 — 실제로 사용자 신고 문장이
    그 한 칸 때문에 필터를 통과했다. 공백 제거본에서 잡히면 그 줄을 버린다.
    """
    if not isinstance(text, str) or not text or not patterns:
        return text if isinstance(text, str) else ""
    active = [rx for _id, only_prec, rx in patterns
              if has_precedents or not only_prec]
    if not active:
        return text
    def _denied(line: str) -> bool:
        squeezed = re.sub(r"\s+", "", line)
        return any(rx.search(line) or rx.search(squeezed) for rx in active)
    kept = [ln for ln in text.split("\n") if not _denied(ln)]
    return "\n".join(kept).strip()


def unwrap_json_reply(text):
    """모델이 문장 대신 **JSON 객체**를 냈으면 그 안의 사람 문장만 꺼낸다. 실패는 "".

    2026-09-02 현장 신고: `[제안]` 자리에 아래가 그대로 박혔다.
        {"precedent": {...}, "suggestion": {"text": "Retest 를 통해 …"}, "evidence_refs": [...]}
    이 구조는 **우리 코드에 없다** — 배치 계약(`[{id,text}]`)은 지켜졌고 모델이 그 `text`
    **안에** 자기 스키마를 또 만든 것이다(프롬프트가 두 블록을 요구하니 "구조화해서 답해야
    한다" 고 넘겨짚은 부류). 파서는 `[사례]`/`[제안]` 토큰만 찾으므로 토큰이 없는 이
    덩어리가 통째로 [제안] 본문이 됐다.

    정책: **문장을 건질 수 있으면 건지고, 아니면 버린다**(호출부가 빈 문자열을 skip 하고
    룰 문장으로 폴백 — 사용자에게 JSON 을 보여 주는 것보다 낫다).
    JSON 이 아니면 원문 그대로 통과시킨다(정상 경로는 여기서 아무 일도 하지 않는다).
    """
    s = str(text or "").strip()
    if not (s.startswith("{") or s.startswith("[")):
        return s
    try:
        doc = json.loads(s)
    except (ValueError, TypeError):
        return s          # JSON 처럼 생겼지만 아니다 — 판단하지 않고 원문 유지
    # 사람 문장이 들어 있을 만한 자리를 넓게 훑는다(모델이 키 이름을 매번 다르게 짓는다).
    found = []
    def _walk(node, depth=0):
        if depth > 6 or len(found) >= 20:
            return
        if isinstance(node, str):
            t = node.strip()
            if len(t) >= 10:          # 라벨("low"/"E1")이 아니라 문장인 것만
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
    # 중복 제거(같은 문장이 여러 키에 실릴 수 있다) — 순서는 유지.
    out, seen = [], set()
    for t in found:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return "\n".join(out)


def parse_llm_blocks(text):
    """LLM 출력 → (사례 요약|None, 제안|None). **recommend.parse_llm_blocks 의 사본**.

    규칙 #8 로 엔진을 import 할 수 없어 사본을 둔다 — 한쪽을 고치면 다른 쪽도 고칠 것
    (tests/test_ai_prompt_determinism.py 가 두 파일의 함수 본문을 대조한다).
    관대하게 받는다: 두 토큰이 다 있으면 각 블록, `[제안]` 만 있으면 (None, 뒤 전부),
    토큰이 없으면 (None, 전체) — 종전(단일 [제안] 출력) 하위호환.
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


def split_comment(comment) -> tuple[str, str, str] | None:
    """코멘트 평문 → (phenomenon, past_case, suggestion). 형식 불일치는 None.

    `_cell_text` 접두([MAJOR][이봉] )가 붙기 **전** 원문(case["comment"])을 기대한다.
    """
    if not isinstance(comment, str):
        return None
    m = SECTION_RE.match(comment.strip())
    if not m:
        return None
    return m.group("phen").strip(), m.group("past").strip(), m.group("sugg").strip()


def _fmt(value) -> str:
    """수치 → 프롬프트용 짧은 문자열. **결정적이어야 한다**(sha 게이트의 전제).

    유효숫자 6자리 %g — 1.2e-06 처럼 지수도 자연히 처리되고, float 재현오차가
    프롬프트 sha 를 흔들지 않는다. 4자리로 줄이지 말 것: 측정값은 spec 폭 대비 미세
    변화가 판단 근거인데(예: 1.0000123 → "1"), 그 자리에서 뭉개면 과거/현재 대비가
    "같은 값"으로 보인다. 숫자가 아니면 str() 그대로.
    """
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _kv_line(data: dict, cols=None) -> str:
    """{k: v} → "k=v, k=v" — None 은 빼고, cols 를 주면 그 순서로 고정(결정성)."""
    items = [(k, data.get(k)) for k in (cols or data.keys())]
    return ", ".join(f"{k}={_fmt(v)}" for k, v in items if v is not None)


def _evidence_text(sig: dict) -> str:
    """signature 행의 evidence[] → "signal=value" 나열 (현재 케이스의 L2 근거값).

    엔진이 to_result 에 이미 실어 주는 값이다(present.to_result 의 signatures[].evidence).
    발화 근거 수치를 함께 줘야 LLM 이 선례의 당시 feature 와 대조할 수 있다.
    """
    parts = []
    for e in sig.get("evidence") or []:
        code = str((e or {}).get("signal_code") or "").strip()
        value = (e or {}).get("value")
        if not code or value is None:
            continue        # 값 없는 코드는 대조에 못 쓴다 — 이름만 나열하면 잡음이다
        parts.append(f"{code}={_fmt(value)}")
    return ", ".join(parts)


def _sig_lines(case: dict) -> str:
    """발화 signature 전체 목록 — recommend._fired_signature_lines 의 case-dict 판.

    to_result 의 signatures[] 행에는 phenomenon_ko 가 없어 action_ko 만 싣는다(모듈
    docstring 의 의도된 차이). 문구가 없는 id 는 id 만 싣는다(발화 사실 보존).
    발화 근거값(evidence)은 선례의 당시 feature 와 대조하라고 뒤에 덧붙인다.
    """
    out = []
    for s in case.get("signatures") or []:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        role = str(s.get("role") or "")
        head = f"- {sid}" + (f"({role})" if role else "")
        action = str(s.get("action_ko") or "").strip()
        line = f"{head}: {action}" if action else head
        ev = _evidence_text(s)
        if ev:
            line += f" [근거: {ev}]"
        out.append(line)
    return "\n".join(out)


def _sig_count(case: dict) -> int:
    """프롬프트에 실제로 실리는 발화 signature 건수 — `_sig_lines` 와 **같은 기준**.

    헤더에 "N건 - N개 항목을 모두 다뤄라" 로 박히는 수라, 기준이 갈리면 "3건이라 써놓고
    2줄만 있는" 프롬프트가 나가 모델을 혼란시킨다(`_precedent_count` 와 같은 규약).
    id 가 빈 행을 건너뛰는 조건은 `_sig_lines` L230-233 과 문자 그대로 같아야 한다.
    """
    return sum(1 for s in (case.get("signatures") or []) if str(s.get("id") or ""))


def _precedent_block(idx: int, p: dict, comment: str) -> str:
    """선례 1건의 다행 블록.

    사람이 그 사례를 읽고 판단하듯 **식별(무슨 제품/lot/item) → 당시 판정 → 당시 수치 →
    당시 판단·조치 원문** 순으로 쌓는다. 정답지인 comment 는 맨 뒤에 전문으로 둔다
    (truncate 없음 — 요약하면 "어떻게 해결했는지"가 먼저 잘려 나간다).

    재료는 전부 엔진이 준 선례 dict 그대로다(present._precedent_result 계약). 통계가 없는
    선례(CSV 적재분 등)는 그 줄만 빠지고 식별+코멘트는 그대로 나간다.
    """
    head = [f"사례{idx}"]
    product = str(p.get("product_name") or "").strip()
    if product:
        head.append(f"제품 {product}")
    fields = [("lot_id", "lot"), ("item_canonical", "item"), ("unit", "unit")]
    # value_type 은 unit 을 어휘로 접은 값이라 대개 같다 — 다를 때만 싣는다(잡음 제거).
    if str(p.get("value_type") or "") != str(p.get("unit") or ""):
        fields.append(("value_type", "type"))
    fields.append(("bin", "bin"))
    for key, label in fields:
        value = p.get(key)
        if value is not None and str(value).strip():
            head.append(f"{label} {value}")
    if p.get("status"):
        head.append(f"당시 status {p['status']}")
    if p.get("signature"):
        head.append(f"당시 signature {p['signature']}")
    lines = ["- " + " / ".join(head)]

    stats = _kv_line(p.get("metrics") or {}, _PRECEDENT_METRIC_ORDER)
    if stats:
        lines.append(f"  당시 통계: {stats}")
    feats = _kv_line(p.get("features") or {}, _PRECEDENT_FEATURE_ORDER)
    if feats:
        lines.append(f"  당시 분포/공간: {feats}")
    lines.append(f"  당시 판단·조치(원문): {comment}")
    return "\n".join(lines)


def _has_detail(p: dict) -> bool:
    """이 선례에 comment 말고 실을 게 더 있나 — 없으면 한 줄로 쓴다.

    옛 계약(comment/product_name 만 있는 dict — 캐시에 굳은 값·테스트 픽스처)이 들어와도
    종전 한 줄 형태로 나가야 한다.
    """
    if p.get("metrics") or p.get("features"):
        return True
    return any(p.get(k) for k in ("lot_id", "item_canonical", "unit", "status",
                                  "signature", "bin"))


def _precedent_count(case: dict) -> int:
    """프롬프트에 실제로 실리는 선례 건수 — `_precedent_lines` 와 **같은 기준**.

    금지 문구의 `only_with_precedents` 게이트가 이 수를 본다. comment 가 없는 선례는
    프롬프트에 안 들어가므로(문장 재료가 안 된다) 여기서도 세지 않는다 — 기준이 갈리면
    "사례를 줬다고 판단해 지웠는데 실제로는 안 준" 경우가 생긴다.
    """
    return sum(1 for p in (case.get("precedents") or [])
               if str((p or {}).get("comment") or "").strip())


def _precedent_lines(case: dict) -> str:
    """선례 전량 목록 — 상세가 있으면 다행 블록, 없으면 한 줄.

    재료는 엔진 `to_result` 의 `precedents[]`(present._precedent_result 계약) 그대로다.
    한 줄 형태는 recommend._precedent_lines 와 같은 모양이다.
    """
    lines = []
    idx = 0
    for p in case.get("precedents") or []:
        comment = str(p.get("comment") or "").strip()   # to_result: human_comment → "comment"
        if not comment:
            continue
        idx += 1
        if _has_detail(p):
            lines.append(_precedent_block(idx, p, comment))
        else:
            product = str(p.get("product_name") or "").strip()
            lines.append(f"- {product + ': ' if product else ''}{comment}")
    return "\n".join(lines)


def _primary_action_ko(case: dict) -> str:
    """primary signature 행의 action_ko (없으면 "")."""
    for s in case.get("signatures") or []:
        if s.get("role") == "primary":
            return str(s.get("action_ko") or "").strip()
    return ""


def _action_block(case: dict) -> str:
    """발화 signature **전부**의 action_ko 목록 — 프롬프트의 [기본 조치 목록] 재료.

    엔진 `recommend._action_lines` 의 case-dict 판이다(primary 먼저, 같은 문장 중복 제거).
    LLM 은 이 목록을 **재료로** 통합 제안을 쓰고, 화면에는 그 통합 문장만 나간다 —
    목록 자체가 화면에 다시 나오지는 않는다(사용자 결정 2026-09-02).
    """
    rows = [s for s in (case.get("signatures") or []) if s.get("id")]
    rows.sort(key=lambda s: 0 if s.get("role") == "primary" else 1)
    lines, seen = [], set()
    for s in rows:
        text = str(s.get("action_ko") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(f"- {s['id']}: {text}")
    return "\n".join(lines)


def _item_head(case: dict, enrich: dict) -> str:
    """item 헤더 줄 — unit/limit 이 있으면 덧붙인다(선례의 unit 과 대조용)."""
    head = f"item: {case.get('item_canonical')} / class: {case.get('item_class')}"
    unit = str((enrich or {}).get("unit") or "").strip()
    if unit:
        head += f" / unit: {unit}"
    for key, label in (("lsl", "LSL"), ("usl", "USL")):
        value = (enrich or {}).get(key)
        if value is not None:
            head += f" / {label}={_fmt(value)}"
    return head


def build_prompt(case: dict, enrich: dict | None = None,
                 rules: dict | None = None) -> str | None:
    """case dict → LLM 프롬프트. 재료가 모자라면 None(그 item 은 폴백 유지 = 무해).

    구조는 recommend._build_prompt(L122~164) 를 따르되 이 프로젝트에서 3가지를 더한다
    (docs/23): 지시문 확장(_INSTRUCTION_EXTRA) · 현재 통계 줄 · 선례 상세 블록.

    `rules` 는 운영자가 `/pe/eval` 에서 편집한 지시문(rules/ai_prompt.yaml)이다. 엔진
    `_build_prompt` 와 마찬가지로 **고정 지시문 뒤·재료 앞**에 들어간다. None 이면
    종전 프롬프트와 바이트 동일하다(sha 불변).

    `enrich` 는 **현재 케이스 쪽 재료**다(과거 쪽은 엔진이 case dict 에 실어 준다).
    순수 입력이라 이 함수는 DB 를 열지 않는다(결정성 유지 — 호출부 ai_comment.py 가
    eval_export 헬퍼로 조립해 넘긴다). 형태:
        {"unit", "lsl", "usl", "stats": {raw_metrics 와 같은 이름의 키}}
    None 이면 현재 통계 줄 없이 만든다(여전히 유효한 프롬프트).

    **선례가 0건이면 None** (2026-09-02 사용자 결정) — 이 프롬프트의 존재 이유가 "사례를
    현재 수치와 대조" 라, 사례가 없으면 남는 재료는 코드가 이미 완성해 둔 것뿐이라
    LLM 호출이 토큰·시간 낭비다. None 이면 그 item 은 prompts 에 안 실려 클라 워커가
    아예 보내지 않는다(셀은 발화 signature 전부의 action_ko 그대로).
    """
    parsed = split_comment(case.get("comment"))
    if parsed is None:
        return None
    if not _precedent_count(case):
        return None
    phenomenon, _past_case, suggestion = parsed
    # 프롬프트 재료용 조치 목록 — 발화 전부. 하나도 없으면 [제안] 섹션 파싱값 폴백.
    action_block = _action_block(case) or _primary_action_ko(case) or suggestion
    if not action_block:
        return None
    enrich = enrich or {}
    sig_lines = _sig_lines(case)
    sig_count = _sig_count(case)
    prec_lines = _precedent_lines(case)
    secondary = case.get("secondary_signatures") or []
    stats_line = _kv_line(enrich.get("stats") or {}, _PRECEDENT_METRIC_ORDER)
    lines = [
        _INSTRUCTION,
        _INSTRUCTION_EXTRA,
        *instruction_lines(rules),
        _item_head(case, enrich),
        f"status: {case.get('status')} / primary: {case.get('primary_signature')}",
        f"secondary: {', '.join(str(s) for s in secondary)}",
    ]
    if stats_line:
        lines.append(f"[현재 통계] {stats_line}")
    lines += [
        # 건수를 재료 옆에 박아 "각 항목을 모두 다뤄라"(rules/ai_prompt.yaml
        # cover_all_signatures)를 검증 가능한 형태로 만든다 — 지시문만 주면 모델이
        # 목록 개수를 세다 틀린다. 0건이면 종전 헤더 그대로(셀 수가 없으니 요구도 없다).
        (f"[발화 signature 전체] {sig_count}건 - 아래 {sig_count}개 항목을 모두 다뤄라"
         if sig_count else "[발화 signature 전체]"),
        sig_lines or "- (없음)",
        f"[현상] {phenomenon}",
        "[사례 목록]",
        prec_lines or f"- {_NO_PRECEDENT_TEXT}",
        "[기본 조치 목록(action_ko)]",
        action_block,
    ]
    return "\n".join(lines)


def prompt_sha(prompt: str) -> str:
    """sha256(prompt)[:12] — suggestion 수용 게이트의 키 (docs/23 핵심 결정 ②)."""
    return hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()[:12]


def build_prompts(cases_by_item: dict, enrich_by_item: dict | None = None,
                  rules: dict | None = None) -> dict:
    """build_ai_comments 의 대표 case dict → {item_raw: {"prompt","sha","precedents"}}.

    키는 **item_raw**(Issue Table join 키 — comments 의 row_key 꼬리와 같은 값)다.
    프롬프트를 못 만든 item 은 키 자체를 만들지 않는다(클라 대행 대상에서 빠진다).
    `enrich_by_item` 은 같은 키(item_raw)로 찾는 build_prompt 의 enrich 모음이다.
    `rules` 는 운영자 지시문(build_prompt 참조).

    `precedents` 는 그 프롬프트에 실린 선례 건수다 — 금지 문구의 `only_with_precedents`
    게이트가 push 수용 때 이 값을 본다(service.apply_ai_suggestions). sha 재계산 없이
    "사례를 줬는가"를 알 수 있어야 하므로 prompts dict 에 함께 싣는다.
    """
    enrich_by_item = enrich_by_item or {}
    out = {}
    for item, case in (cases_by_item or {}).items():
        if not item:
            continue
        prompt = build_prompt(case, enrich_by_item.get(item), rules)
        if prompt is None:
            continue
        out[str(item)] = {"prompt": prompt, "sha": prompt_sha(prompt),
                          "precedents": _precedent_count(case)}
    return out


def sanitize_suggestion(text) -> str:
    """클라가 push 한 suggestion 정화 — 저장·병합 전 필수.

    개행은 **살린다**(엔진 프롬프트가 '- ' 항목 형식을 요구하므로 다행 출력이 정상) —
    \r 과 나머지 제어문자·섹션 토큰·코드펜스만 제거하고 1000자 상한을 건다.
    빈 문자열이 될 수 있다(호출부 skip).
    """
    if not isinstance(text, str):
        return ""
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n|\n?```\s*$", "", out.strip())
    # 모델이 문장 대신 JSON 객체를 낸 경우 그 안의 문장만 꺼낸다(2026-09-02 현장 신고).
    # 코드펜스 제거 **뒤**여야 ```json 으로 감싼 응답도 잡힌다.
    out = unwrap_json_reply(out)
    out = _CTRL_RE.sub("", out)
    out = _SECTION_TOKEN_RE.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out[:MAX_SUGGESTION_CHARS].strip()


def patch_cell(cell_text, *, past=None, suggestion=None) -> str:
    """셀 텍스트의 [사례]/[제안] 섹션 **값만** 교체 (None 인 쪽은 손대지 않는다).

    `[MAJOR][이봉] ` 접두와 [현상] 섹션, 그리고 섹션 **토큰 자체**는 바이트 그대로 둔다
    (규칙 12 — 같은 평문을 sheets.js/Excel/챗봇/eval_export 가 소비하고, 옛 캐시의
    `[과거사례]`/`[점검제안]` 토큰도 그 자리 그대로 유지된다).
    **멱등**이다 — 같은 값으로 두 번 적용해도 결과가 같다(재빌드마다 재병합되므로 필수).
    섹션 토큰이 없는 문자열은 원문 그대로 반환(치환 실패를 조용히 무해화).
    """
    if not isinstance(cell_text, str):
        return cell_text
    out = cell_text
    # 치환값은 **함수**로 넘긴다 — 문자열 replacement 였다면 본문의 `\1`·`\g` 가 역참조로
    # 해석돼 사용자 문장이 깨진다(LLM 출력에 백슬래시가 섞일 수 있다).
    if past:
        # 뒤 [제안] 토큰(그룹 2)은 그대로 되살린다 — 이 치환이 섹션 경계를 지우면
        # 그다음 patch 가 [제안] 을 찾지 못해 조용히 아무 일도 안 하게 된다.
        out = CASE_SEC_RE.sub(lambda m: m.group(1) + past + m.group(2), out, count=1)
    if suggestion:
        out = SUGGEST_TAIL_RE.sub(lambda m: m.group(1) + suggestion, out, count=1)
    return out


def patch_suggestion_text(cell_text, suggestion) -> str:
    """[제안] 섹션만 치환하는 얇은 래퍼 — 기존 호출부·테스트 호환."""
    if not suggestion:
        return cell_text
    return patch_cell(cell_text, suggestion=suggestion)


def apply_suggestions(result: dict, stored_items: dict,
                      deny: list | None = None) -> tuple[dict, int]:
    """AI 결과 dict 에 저장된 suggestion 들을 병합 — (새 dict, 패치 건수).

    **sha 게이트는 폐기됐다(2026-09-02 사용자 결정)** — 저장된 최신 문장을 sha 와
    무관하게 병합한다. 종전에는 sha 불일치 item 을 건너뛰었는데(구 docs/23 핵심 결정 ②),
    지시문을 고칠 때마다 프롬프트 sha 가 전부 갈려 **전 세션이 재대행 전까지 action_ko
    폴백으로 후퇴**했다 — LLM 문장이 store 에 멀쩡히 있는데 화면에는 룰 문장만 보이는
    회귀의 원인. 옛 프롬프트 기준 문장이라도 action_ko 나열보다 낫다는 것이 사용자
    결정이다(sha 는 정보용 — 관리자 stale 표시). **게이트를 되살리지 말 것.**

    `deny` (compile_deny_patterns 결과) 를 주면 병합 직전에 금지 문구 줄을 걷어낸다 —
    게이트 폐기로 옛 룰 시절 저장된 변명 문장이 되살아나는 것을 막고, 금지 패턴 편집이
    **이미 저장된 문장에도 소급 적용**되게 한다. 두 블록이 다 비면 병합하지 않는다
    (= 엔진 폴백 유지).

    패치 대상 row_key 는 `key.endswith("|"+item)` — Yield fan-out(Yield|<bin>|<item>) +
    CPK|<item> + ETC|<item> 전부.

    ★ 항상 **새 dict** 를 돌려준다 — 인자 result 는 RAM 캐시 공유 객체일 수 있어
    in-place 수정 금지(다른 요청 오염).
    """
    prompts = (result or {}).get("prompts") or {}
    comments = (result or {}).get("comments") or {}
    if not prompts or not comments or not stored_items:
        return result, 0
    accepted = {}
    for item, row in stored_items.items():
        meta = prompts.get(item)
        if not meta or not isinstance(row, dict):
            continue
        # 두 블록(2026-09-02) — 사례 요약만 온 경우도 반영한다(제안이 비어도 무해).
        suggestion = sanitize_suggestion(row.get("suggestion") or "")
        cases = sanitize_suggestion(row.get("cases") or "")
        if deny:
            has_prec = bool(meta.get("precedents"))
            suggestion = strip_denied_lines(suggestion, deny, has_prec)
            cases = strip_denied_lines(cases, deny, has_prec)
        if suggestion or cases:
            accepted[str(item)] = (cases, suggestion)
    if not accepted:
        return result, 0
    new_comments = dict(comments)
    patched = 0
    for key, cell in comments.items():
        for item, (cases, suggestion) in accepted.items():
            if key.endswith("|" + item):
                new_text = patch_cell(cell, past=cases or None,
                                      suggestion=suggestion or None)
                if new_text != cell:
                    new_comments[key] = new_text
                    patched += 1
                break
    out = dict(result)
    out["comments"] = new_comments
    return out, patched

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
"""
from __future__ import annotations

import hashlib
import re

# ── 코멘트 평문 파싱 (형식 정본: recommend.make_comment — 바이트 불변 계약, 규칙 12) ──
# 옛 토큰 [점검제안] 은 2026-08-28 이전 캐시에 굳은 코멘트가 계속 실어 온다 — 둘 다 받는다.
SECTION_RE = re.compile(
    r"^\[현상\]\s*(?P<phen>.*?)\s*\n\[과거사례\]\s*(?P<past>.*?)\s*\n\s*"
    r"\[(?:점검제안|제안)\]\s*(?P<sugg>.*)$", re.S)
# 마지막 섹션 값만 치환 — 토큰은 원문 것을 유지한다(옛 캐시 [점검제안] 도 그대로 치환).
SUGGEST_TAIL_RE = re.compile(r"(\[(?:점검제안|제안)\]\s*).*$", re.S)
# suggestion 안에 끼면 섹션 파싱을 깨뜨리는 토큰들 (sanitize 에서 제거)
_SECTION_TOKEN_RE = re.compile(r"\[(?:현상|과거사례|점검제안|제안)\]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")   # \n(0x0a) 은 살린다 — 아래 참조

# 엔진 프롬프트가 "최대 5줄 '- ' 항목" 을 요구하므로(2026-08-28) 개행은 정상 출력이다.
# docs/23 초안의 500자 상한은 그 형식 변경으로 1000자로 늘렸다.
MAX_SUGGESTION_CHARS = 1000

_NO_PRECEDENT_TEXT = "참고할 수 있는 과거 사례가 없습니다."

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
    "[현상], [과거사례], [점검 제안] 같은 섹션 제목은 출력하지 마라 - 문장만 출력하라"
)

# 이 프로젝트에서 덧붙이는 지시 (vendor copy 아님 — 위 _INSTRUCTION 은 바이트 보존).
# 취지: 과거 Comment 는 그때 담당자가 **어떻게 판단하고 무엇으로 해결했는지** 적어 둔
# 기록이라 사실상 정답지다. 종전 프롬프트는 그 원문 한 줄만 주고 "정답으로 단정하지 마라"
# 로 끝나 LLM 이 사례를 흘려보냈다. 상세(당시 통계·signature·unit)를 함께 주고 현재 값과
# 대조하도록 시켜, 단정은 계속 피하되 활용도는 올린다.
_INSTRUCTION_EXTRA = (
    "과거 사례의 Comment 는 그때 담당자가 무엇을 근거로 판단하고 어떻게 해결했는지 남긴 기록이다."
    "각 사례의 당시 통계 signature unit item 명을 현재 값과 하나씩 대조해서 무엇이 닮았고 무엇이 다른지 판단하라."
    "닮은 사례의 판단 근거와 조치를 현재 상황에 맞게 바꿔서 구체적으로 녹여 써라."
    "사례를 요약만 하고 끝내지 말고 지금 무엇을 확인할지로 바꿔서 써라."
    "비교에 쓰는 수치는 아래에 주어진 값만 쓰고 없는 값을 지어내지 마라."
    # 아래 3줄은 "5줄을 채우려고 억지 문장을 만드는" 부류를 막는다 (2026-09-01).
    # 최대 5줄은 상한이지 목표가 아닌데, 상한만 주면 근거가 2개뿐이어도 5줄을 채우려고
    # 재료에 없는 점검 항목이 나온다 — 지어낸 수치보다 잡아내기 어려운 오염이다.
    "5줄은 상한이지 채워야 하는 목표가 아니다 - 근거가 있는 만큼만 쓰고 재료가 부족하면 줄 수를 줄여라."
    "확인할 근거가 부족한 항목은 쓰지 말고 아예 빼라 - 줄 수를 맞추려고 일반론을 넣지 마라."
    "주어진 재료로 판단할 수 없는 부분은 단정하지 말고 무엇을 더 확인해야 하는지로 써라."
)


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


def build_prompt(case: dict, enrich: dict | None = None) -> str | None:
    """case dict → LLM 프롬프트. 재료가 모자라면 None(그 item 은 폴백 유지 = 무해).

    구조는 recommend._build_prompt(L122~164) 를 따르되 이 프로젝트에서 3가지를 더한다
    (docs/23): 지시문 확장(_INSTRUCTION_EXTRA) · 현재 통계 줄 · 선례 상세 블록.

    `enrich` 는 **현재 케이스 쪽 재료**다(과거 쪽은 엔진이 case dict 에 실어 준다).
    순수 입력이라 이 함수는 DB 를 열지 않는다(결정성 유지 — 호출부 ai_comment.py 가
    eval_export 헬퍼로 조립해 넘긴다). 형태:
        {"unit", "lsl", "usl", "stats": {raw_metrics 와 같은 이름의 키}}
    None 이면 현재 통계 줄 없이 만든다(여전히 유효한 프롬프트).
    """
    parsed = split_comment(case.get("comment"))
    if parsed is None:
        return None
    phenomenon, _past_case, suggestion = parsed
    action_ko = _primary_action_ko(case) or suggestion   # LLM off 상태의 [제안]==action_ko
    if not action_ko:
        return None
    enrich = enrich or {}
    sig_lines = _sig_lines(case)
    prec_lines = _precedent_lines(case)
    secondary = case.get("secondary_signatures") or []
    stats_line = _kv_line(enrich.get("stats") or {}, _PRECEDENT_METRIC_ORDER)
    lines = [
        _INSTRUCTION,
        _INSTRUCTION_EXTRA,
        _item_head(case, enrich),
        f"status: {case.get('status')} / primary: {case.get('primary_signature')}",
        f"secondary: {', '.join(str(s) for s in secondary)}",
    ]
    if stats_line:
        lines.append(f"[현재 통계] {stats_line}")
    lines += [
        "[발화 signature 전체]",
        sig_lines or "- (없음)",
        f"[현상] {phenomenon}",
        "[과거사례 목록]",
        prec_lines or f"- {_NO_PRECEDENT_TEXT}",
        f"참고용 기본 조치(action_ko):{action_ko}",
    ]
    return "\n".join(lines)


def prompt_sha(prompt: str) -> str:
    """sha256(prompt)[:12] — suggestion 수용 게이트의 키 (docs/23 핵심 결정 ②)."""
    return hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()[:12]


def build_prompts(cases_by_item: dict, enrich_by_item: dict | None = None) -> dict:
    """build_ai_comments 의 대표 case dict → {item_raw: {"prompt","sha"}}.

    키는 **item_raw**(Issue Table join 키 — comments 의 row_key 꼬리와 같은 값)다.
    프롬프트를 못 만든 item 은 키 자체를 만들지 않는다(클라 대행 대상에서 빠진다).
    `enrich_by_item` 은 같은 키(item_raw)로 찾는 build_prompt 의 enrich 모음이다.
    """
    enrich_by_item = enrich_by_item or {}
    out = {}
    for item, case in (cases_by_item or {}).items():
        if not item:
            continue
        prompt = build_prompt(case, enrich_by_item.get(item))
        if prompt is None:
            continue
        out[str(item)] = {"prompt": prompt, "sha": prompt_sha(prompt)}
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
    out = _CTRL_RE.sub("", out)
    out = _SECTION_TOKEN_RE.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out[:MAX_SUGGESTION_CHARS].strip()


def patch_suggestion_text(cell_text, suggestion) -> str:
    """셀 텍스트의 마지막 섹션 값만 suggestion 으로 치환.

    `[MAJOR][이봉] ` 접두와 앞 2섹션([현상]/[과거사례])은 **바이트 그대로** 보존한다
    (규칙 12 — 같은 평문을 sheets.js/Excel/챗봇/eval_export 가 소비). 섹션 토큰이 없는
    문자열은 원문 그대로 반환(치환 실패를 조용히 무해화).
    """
    if not isinstance(cell_text, str) or not suggestion:
        return cell_text
    new_text, n = SUGGEST_TAIL_RE.subn(lambda m: m.group(1) + suggestion, cell_text, count=1)
    return new_text if n else cell_text


def apply_suggestions(result: dict, stored_items: dict) -> tuple[dict, int]:
    """AI 결과 dict 에 저장된 suggestion 들을 병합 — (새 dict, 패치 건수).

    sha 게이트: stored_items[item]["sha"] 가 result["prompts"][item]["sha"] 와 일치하는
    item 만 패치한다(룰 변경 → 프롬프트 sha 갈림 → 자동 action_ko 폴백, docs/23 핵심 결정 ②).
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
        if str(row.get("sha") or "") != str(meta.get("sha") or ""):
            continue
        suggestion = sanitize_suggestion(row.get("suggestion") or "")
        if suggestion:
            accepted[str(item)] = suggestion
    if not accepted:
        return result, 0
    new_comments = dict(comments)
    patched = 0
    for key, cell in comments.items():
        for item, suggestion in accepted.items():
            if key.endswith("|" + item):
                new_text = patch_suggestion_text(cell, suggestion)
                if new_text != cell:
                    new_comments[key] = new_text
                    patched += 1
                break
    out = dict(result)
    out["comments"] = new_comments
    return out, patched

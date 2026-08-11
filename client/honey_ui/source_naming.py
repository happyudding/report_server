"""입력 파일명 → source(legend) 기본 이름 — product_type 별 규칙.

``report_generator.df_honey._sheetname_from_filename`` 이 원래 같은 일을 하지만 그 파일은
외부 담당자 소유 영역이라 고칠 수 없다. 그래서 규칙만 여기에 다시 세우고, 파싱이 끝난 뒤
``df_honey_group.rename_sources`` 로 덮어쓴다(정식 오버라이드 API — 빈 문자열이면 기존명
유지, 중복은 _2/_3 회피, 캐시 무효화까지 해준다).

product_type 별 규칙 (정본은 아래 ``_SOURCE_NAME_RULES`` 표):

  MDDI      ``00M.W``/``00P.W``/``00F.W`` 마커 → 마커 **앞부분 전체**=LOT, ``.W`` 뒤 숫자=WF.
            ``NH0D3-00M.W03`` → ``NH0D3_03``. 마커 3종은 같은 웨이퍼의 다른 측정이라 **같은
            legend** 가 나온다(= honey_parse 가 1 source 로 병합할 대상).
            ⚠ 마커가 없을 때의 2차 규칙(xlsx 시트명)은 **외부 담당자 honey_parse 안에 있어
            이 저장소에서는 구현하지 않는다** — 미구현 버그가 아니라 의도다. None 을 돌려
            rename_sources 가 "기존명 유지"(= honey_parse 가 이미 정한 이름)로 떨어뜨린다.
  PDDI      ``stdf_[LOTID]_[STEP]_[WFNO]_[PARTID]…`` **고정 위치** → ``LOTID_WFNO``.
            STEP(L1/L2)이 달라도 같은 legend 가 나온다(= 병합 대상).
  PMIC ·    LOT 헤더 + WF 토큰 → ``602XX2_3``. 세 product_type 이 같은 규칙을 쓴다
  SECURITY  (2026-08-11 — 종전에는 PMIC 전용이었다).
  TCON

동결 규칙이 PMIC 파일명에서 빗나가던 지점 3가지를 여기서 닫는다(LOT+WF 규칙의 존재 이유).

  1. WF 를 ``_W03`` 처럼 **W 접두 토큰으로만** 인정해 ``..._602XX2_3_...`` 의 ``_3`` 을
     놓쳤다 (사용자가 겪은 주 증상 — ``602XX2_3`` 이 나와야 하는데 ``602XX2`` 만 나왔다).
  2. ``re.IGNORECASE`` + 파일명 전체 검색이라 LOT 과 무관한 위치의 소문자 ``w`` 토큰까지
     잡았다 — ``awj_602XX2_3_wow.csv`` 가 ``602XX2_wow`` 가 됐다.
  3. LOT 앞에 ``_`` 가 반드시 있어야 해서 ``602XX2_3.csv`` 처럼 파일명 맨 앞이면 실패했다.

**입력 파일 개수 ≠ source 개수** (CLAUDE.md 불변 규칙 #9) 인데 ``rename_sources`` 는
positional 이다. 그 어긋남을 메우는 것이 ``collapse_merged_names`` + ``resolve_source_names``
다 — 호출부(honey_main)가 아니라 여기 두는 이유는, honey_main 은 Qt+pandas 의존이라 그
로직만 단위 테스트할 수 없기 때문이다. group 객체가 아니라 int 하나만 받으므로 순수성은
그대로다.

개명 이력: 구 ``pmic_source_name`` / ``pmic_lot_id`` → ``lot_wf_source_name`` /
``lot_wf_lot_id`` (2026-08-11 — PMIC 전용이 아니게 되어 접두가 거짓이 됐다).

Qt 에 의존하지 않는 순수 함수만 둔다 — QApplication 없이 단독 검증할 수 있어야 한다
(``tests/test_source_naming.py``).
"""
from __future__ import annotations

import re
from pathlib import Path

# LOT 헤더: 60/61/62/68/6A/6Z/80/81/82/8Z 로 시작하는 토큰. 구분자 뒤 **또는 파일명 맨 앞**.
# 68 은 6 계열에만 있다(사용자 요청 2026-08-06) — 88 은 LOT 이 아니라 6/8 을 통으로
# 묶은 [68][012Z8] 로 쓰지 않고 계열별로 나눠 쓴다. 6A 도 6 계열에만 추가(2026-08-11).
# 대소문자를 무시하지 않는다 — 소문자 '6z…' 까지 잡으면 무관한 토큰을 LOT 으로 오인한다.
_LOT_RE = re.compile(r"(?:^|[_\-.])((?:6[0128AZ]|8[012Z])[^_\-.]*)")

# WF 번호: LOT 바로 다음 토큰이 (선택적 W) + 숫자 1~3자리 **전체**일 때만 인정한다.
# 'final' · 'wow' 같은 토큰을 배제하려면 부분 일치가 아니라 전체 일치라야 한다.
_WF_RE = re.compile(r"^[Ww]?\d{1,3}$")

_SPLIT_RE = re.compile(r"[_\-.]")

# MDDI 마커: 'NH0D3-00M.W03' → LOT 'NH0D3' + WF '03'. LOT 은 마커 앞부분 **전체**라
# LOT 안에 '-'/'_' 가 들어가도 보존된다(PMIC 과 달리 뜻 없는 접두가 붙지 않는다).
# 마커가 6자 이상으로 특징적이라 대소문자를 무시해도 오탐이 없다(LOT 은 원문 보존).
_MDDI_RE = re.compile(r"^(.*?)[_\-.]*00[MPF]\.W(\d{1,3})", re.IGNORECASE)

# PDDI 고정 위치: stdf_[LOTID]_[STEP]_[WFNO]_[PARTID]…
_PDDI_PREFIX = "stdf"
_PDDI_LOT_IDX = 1
_PDDI_WF_IDX = 3

ROLE_SUFFIXES = ("RT", "CT", "HT")


# --------------------------------------------------------------- product_type 별 규칙

def lot_wf_source_name(filename) -> str | None:
    """PMIC/SECURITY/TCON 파일명 → ``LOT_WF`` 또는 ``LOT``. 안 맞으면 None(호출자 fallback).

    예) ``awjkelfjwkalef_602XX2_3_jkqewjklqjetk.std`` → ``602XX2_3``
        ``T2K_6Z1234_W03_260505.csv``                → ``6Z1234_W03``
        ``602XX2_final.std``                         → ``602XX2``  (final 은 WF 가 아니다)
        ``602XX2_3.csv``                             → ``602XX2_3`` (맨 앞 LOT 도 인정)
    """
    name = Path(str(filename)).name
    m = _LOT_RE.search(name)
    if not m:
        return None
    lot = m.group(1)
    # LOT 토큰 뒤에서 첫 번째 비어있지 않은 토큰 하나만 본다. 확장자도 구분자로 갈리므로
    # 'LOT.std' 처럼 뒤가 확장자뿐이면 'std' 가 걸리는데 WF 규칙에서 자연히 탈락한다.
    tail = [p for p in _SPLIT_RE.split(name[m.end(1):]) if p]
    if tail and _WF_RE.match(tail[0]):
        return f"{lot}_{tail[0]}"
    return lot


def lot_wf_lot_id(filename) -> str | None:
    """PMIC/SECURITY/TCON 파일명에서 LOT ID 토큰만 뽑는다. 규칙에 안 맞으면 None."""
    m = _LOT_RE.search(Path(str(filename)).name)
    return m.group(1) if m else None


def mddi_source_name(filename) -> str | None:
    """MDDI 파일명 → ``LOT_WF``. 마커(00M/00P/00F)가 없으면 None.

    예) ``NH0D3-00M.W03`` / ``NH0D3-00P.W03`` / ``NH0D3-00F.W03`` → 전부 ``NH0D3_03``

    None 은 "규칙 2(xlsx 시트명)로 넘어가라"는 뜻인데, 그 규칙은 honey_parse 안에 있어
    이 저장소가 이름을 만들지 않는다 — 기존명(honey_parse 산출)이 그대로 남는 게 정답이다.
    """
    lot = mddi_lot_id(filename)
    if not lot:
        return None
    m = _MDDI_RE.match(Path(str(filename)).name)
    return f"{lot}_{m.group(2)}"


def mddi_lot_id(filename) -> str | None:
    """MDDI 파일명에서 LOT(마커 앞부분 전체)만 뽑는다. 마커가 없으면 None."""
    m = _MDDI_RE.match(Path(str(filename)).name)
    return m.group(1) if m and m.group(1) else None


def pddi_source_name(filename) -> str | None:
    """PDDI ``stdf_[LOTID]_[STEP]_[WFNO]_[PARTID]…`` → ``LOTID_WFNO`` (고정 위치).

    STEP(L1/L2)이 달라도 같은 legend 가 나온다 — 같은 웨이퍼의 두 STEP 을 honey_parse 가
    1 source 로 병합하기 때문. 이 저장소는 **이름만** 그 규칙을 따르고 병합은 하지 않는다.
    """
    tokens = _pddi_tokens(filename)
    if not tokens:
        return None
    lot, wf = tokens[_PDDI_LOT_IDX], tokens[_PDDI_WF_IDX]
    return f"{lot}_{wf}" if lot and wf else None


def pddi_lot_id(filename) -> str | None:
    """PDDI 파일명에서 LOTID(2번째 토큰)만 뽑는다. 규칙에 안 맞으면 None."""
    tokens = _pddi_tokens(filename)
    return (tokens[_PDDI_LOT_IDX] or None) if tokens else None


def _pddi_tokens(filename) -> list | None:
    """PDDI 파일명을 '_' 로 쪼갠 토큰. 규칙에 안 맞으면 None.

    ``.stem`` 으로 확장자를 먼저 떼는 게 중요하다 — 전체 파일명으로 쪼개면
    ``stdf_ABC123_L1_03.stdf`` 의 WFNO 가 ``03.stdf`` 가 된다.

    ``stdf_`` 접두를 요구하는 이유: PDDI 는 폴더 열기에 확장자 화이트리스트가 없어 로그 등
    무관한 파일이 딸려 온다. 접두 검사가 없으면 ``other_A_B_C_D.txt`` 가 legend ``A_C`` 로
    오염된다. 접두가 없으면 None → 기존명 유지라 오늘과 동일하게 떨어져 손해가 없다.
    """
    tokens = Path(str(filename)).stem.split("_")
    if len(tokens) <= _PDDI_WF_IDX or tokens[0].lower() != _PDDI_PREFIX:
        return None
    return tokens


# 파일명 → legend. 이 표가 규칙의 정본이다.
# 호출 지점이 3개(_parse_group_core 파싱 후 / _guess_source_names 파싱 전 /
# _lot_id_from_sources 업로드 메타)라 if 분기를 3벌 두면 반드시 어긋난다 — 어긋나면
# Temperature 배치 창이 두 번 뜬다(honey_main._guess_source_names 주석). 모두가 같은 표를
# 조회하게 해 동기화를 기계적으로 보장한다. product_type 추가는 이 표에 1줄이다.
_SOURCE_NAME_RULES = {
    "MDDI": mddi_source_name,
    "PDDI": pddi_source_name,
    "PMIC": lot_wf_source_name,
    "SECURITY": lot_wf_source_name,   # 같은 함수를 3번 쓰는 건 의도 — 어느 product_type 이
    "TCON": lot_wf_source_name,       # 어느 규칙인지 표 한 장으로 보이게 한다
}

# 파일명 → LOT ID (업로드 메타 lot_id 기본값). legend 규칙과 짝을 이룬다.
_LOT_ID_RULES = {
    "MDDI": mddi_lot_id,
    "PDDI": pddi_lot_id,
    "PMIC": lot_wf_lot_id,
    "SECURITY": lot_wf_lot_id,
    "TCON": lot_wf_lot_id,
}

# honey_parse 가 입력 n개를 1 source 로 병합할 수 있는 product_type.
# 파싱 **전에는** source 개수를 알 수 없어 guess_source_names 가 값을 주지 않는다.
MERGING_PRODUCT_TYPES = frozenset({"MDDI", "PDDI"})


def _pt(product_type) -> str:
    """product_type 정규화 — 대문자 변환을 한 곳에만 둔다."""
    return str(product_type or "").upper()


def source_name_for(filename, product_type) -> str | None:
    """product_type 규칙으로 파일명 → legend. 규칙이 없거나 안 맞으면 None."""
    rule = _SOURCE_NAME_RULES.get(_pt(product_type))
    return rule(filename) if rule else None


def lot_id_for(filename, product_type) -> str | None:
    """product_type 규칙으로 파일명 → LOT ID. 규칙이 없거나 안 맞으면 None."""
    rule = _LOT_ID_RULES.get(_pt(product_type))
    return rule(filename) if rule else None


# ------------------------------------------------------------------- 호출부 진입점

def suggest_source_names(paths, product_type) -> list | None:
    """파일 경로 목록 → 파일별 이름 목록. 적용 대상이 아니면 None.

    규칙이 없는 product_type 이거나 규칙에 맞는 파일이 하나도 없으면 None 이라 **기존
    동작(honey_parse/df_honey 의 이름)이 그대로 남는다.** 개별 파일이 규칙에 안 맞으면 그
    자리는 빈 문자열이고, rename_sources 가 빈 문자열을 "기존명 유지"로 해석한다.

    ⚠ 반환은 **파일 1개당 1칸**이라 source 개수와 다를 수 있다. rename_sources 에 직접
    넘기지 말고 ``resolve_source_names`` 를 쓸 것.
    """
    names = [source_name_for(p, product_type) or "" for p in paths]
    return names if any(names) else None


def collapse_merged_names(names) -> list | None:
    """같은 legend 가 반복되면 **첫 등장 순서**로 1개씩만 남긴다. 접을 게 없으면 None.

    "같은 legend = honey_parse 가 1 source 로 병합한 것" 이라는 규칙을 그대로 인코딩한다
    (MDDI 의 00M/00P/00F 3종, PDDI 의 STEP L1/L2).

    인접(run)이 아니라 **첫 등장** 기준이다 — 사용자가 M/P/F 를 웨이퍼 순이 아니라 타입 순
    (M03, M05, P03, P05, …)으로 넣어도 웨이퍼 개수로 접혀야 한다. 파일 선택 순서는 자유다.

    빈 이름이 하나라도 있으면 None — 빈 이름은 어느 source 인지 정보를 담지 않아, 접으면
    길이만 우연히 맞고 배치는 틀린다(MDDI 규칙 2 미구현 때문에 실제로 생기는 상황이다).
    """
    items = [str(n or "").strip() for n in (names or [])]
    if not items or not all(items):
        return None
    folded = list(dict.fromkeys(items))
    return folded if len(folded) < len(items) else None


def resolve_source_names(paths, product_type, n_sources) -> list | None:
    """파일 경로 + **실제 source 개수** → rename_sources 인자. 확신이 없으면 None.

    ⚠ 입력 파일 개수 ≠ source 개수(CLAUDE.md #9)인데 rename_sources 는 positional 이다.
    길이가 어긋난 채 넘기면 앞에서부터 잘려 조용한 오배치가 난다.

      1) 파일별 이름 길이 == source 개수 → 그대로 (1:1). 같은 이름이 겹쳐도 rename_sources
         가 _2/_3 로 갈라 준다 — Temperature 는 RT/CT/HT 폴더에 같은 파일명이 있어 이
         경우가 정상 경로다. 그래서 **이걸 먼저** 본다.
      2) 접은 길이 == source 개수 → 접은 것 (honey_parse 병합)
      3) 둘 다 아니면 None — 조용한 오배치보다 기존명 유지가 낫다
         (honey_main._roles_for_names 와 같은 철학)
    """
    names = suggest_source_names(paths, product_type)
    if not names:
        return None
    if len(names) == n_sources:
        return names
    folded = collapse_merged_names(names)
    return folded if folded is not None and len(folded) == n_sources else None


def guess_source_names(paths, product_type) -> list | None:
    """파싱 **전** 이름 추정 — 전 파일이 규칙에 맞을 때만 목록, 하나라도 빠지면 None.

    Temperature 모드가 파싱과 병렬로 배치 창을 띄울 때 쓴다. 뜻 없는 조각으로 창을 띄우면
    자동 그룹 배치(pair_key)까지 빗나가 오히려 손해라, **전부 확실할 때만** 값을 준다.
    중복 해소는 호출부가 한다(df_honey_group 과 같은 규칙을 써야 하므로).

    병합 product_type(MDDI/PDDI)은 항상 None 이다 — 파싱 전에는 source 개수를 알 수 없어
    파일 n개가 source 1개일지 n개일지 판단할 수 없다. (Temperature 는 PMIC/SECURITY 에만
    노출되므로 실제로 이 경로에 도달하지도 않는다.)
    """
    if _pt(product_type) in MERGING_PRODUCT_TYPES:
        return None
    out = []
    for p in paths:
        base = source_name_for(p, product_type)
        if not base:
            return None
        out.append(base)
    return out


# ------------------------------------------------------------------- Temperature 접미사

def role_of_name(name) -> str:
    """legend 이름 끝의 온도 접미사(_RT/_CT/_HT) → 역할. 없으면 빈 문자열."""
    text = str(name or "")
    for role in ROLE_SUFFIXES:
        if text.upper().endswith(f"_{role}"):
            return role
    return ""


def apply_role_suffix(name, role) -> str:
    """legend 뒤에 ``_RT``/``_CT``/``_HT`` 를 붙인다.

    - 이미 같은 접미사면 그대로 (중복 부착 금지)
    - 다른 온도 접미사가 붙어 있으면 교체 (Role 을 바꿨을 때)
    - role 이 비면 붙어 있던 온도 접미사를 떼어낸다
    """
    text = str(name or "")
    cur = role_of_name(text)
    if cur:
        text = text[:-3]
    role = str(role or "").upper()
    return f"{text}_{role}" if role in ROLE_SUFFIXES else text

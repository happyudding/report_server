"""입력 파일명 → source(legend) 기본 이름 — PMIC LOT/WF 규칙.

``report_generator.df_honey._sheetname_from_filename`` 이 원래 같은 일을 하지만 그 파일은
외부 담당자 소유 영역이라 고칠 수 없다. 그래서 규칙만 여기에 다시 세우고, 파싱이 끝난 뒤
``df_honey_group.rename_sources`` 로 덮어쓴다(정식 오버라이드 API — 빈 문자열이면 기존명
유지, 중복은 _2/_3 회피, 캐시 무효화까지 해준다).

동결 규칙이 PMIC 파일명에서 빗나가던 지점 3가지를 여기서 닫는다.

  1. WF 를 ``_W03`` 처럼 **W 접두 토큰으로만** 인정해 ``..._602XX2_3_...`` 의 ``_3`` 을
     놓쳤다 (사용자가 겪은 주 증상 — ``602XX2_3`` 이 나와야 하는데 ``602XX2`` 만 나왔다).
  2. ``re.IGNORECASE`` + 파일명 전체 검색이라 LOT 과 무관한 위치의 소문자 ``w`` 토큰까지
     잡았다 — ``awj_602XX2_3_wow.csv`` 가 ``602XX2_wow`` 가 됐다.
  3. LOT 앞에 ``_`` 가 반드시 있어야 해서 ``602XX2_3.csv`` 처럼 파일명 맨 앞이면 실패했다.

Qt 에 의존하지 않는 순수 함수만 둔다 — QApplication 없이 단독 검증할 수 있어야 한다.
"""
from __future__ import annotations

import re
from pathlib import Path

# LOT 헤더: 60/61/62/68/6Z/80/81/82/8Z 로 시작하는 토큰. 구분자 뒤 **또는 파일명 맨 앞**.
# 68 은 6 계열에만 있다(사용자 요청 2026-08-06) — 88 은 LOT 이 아니라 6/8 을 통으로
# 묶은 [68][012Z8] 로 쓰지 않고 계열별로 나눠 쓴다.
# 대소문자를 무시하지 않는다 — 소문자 '6z…' 까지 잡으면 무관한 토큰을 LOT 으로 오인한다.
_LOT_RE = re.compile(r"(?:^|[_\-.])((?:6[0128Z]|8[012Z])[^_\-.]*)")

# WF 번호: LOT 바로 다음 토큰이 (선택적 W) + 숫자 1~3자리 **전체**일 때만 인정한다.
# 'final' · 'wow' 같은 토큰을 배제하려면 부분 일치가 아니라 전체 일치라야 한다.
_WF_RE = re.compile(r"^[Ww]?\d{1,3}$")

_SPLIT_RE = re.compile(r"[_\-.]")

ROLE_SUFFIXES = ("RT", "CT", "HT")


def pmic_lot_id(filename) -> str | None:
    """PMIC 파일명에서 LOT ID 토큰만 뽑는다. 규칙에 안 맞으면 None."""
    m = _LOT_RE.search(Path(str(filename)).name)
    return m.group(1) if m else None


def pmic_source_name(filename) -> str | None:
    """PMIC 파일명 → ``LOT_WF`` 또는 ``LOT``. 규칙에 안 맞으면 None(호출자 fallback).

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


def suggest_source_names(paths, product_type) -> list | None:
    """파일 경로 목록 → rename_sources 에 넘길 이름 목록. 적용 대상이 아니면 None.

    PMIC 이 아니면 None 을 돌려 **기존 동작(df_honey 의 이름)을 그대로 둔다.** PMIC 이라도
    규칙에 맞는 파일이 하나도 없으면 역시 None 이다. 개별 파일이 규칙에 안 맞으면 그 자리는
    빈 문자열이고, rename_sources 가 빈 문자열을 "기존명 유지"로 해석한다.
    """
    if str(product_type or "").upper() != "PMIC":
        return None
    names = [pmic_source_name(p) or "" for p in paths]
    return names if any(names) else None


def guess_source_names(paths, product_type) -> list | None:
    """파싱 **전** 이름 추정 — 전 파일이 규칙에 맞을 때만 목록, 하나라도 빠지면 None.

    Temperature 모드가 파싱과 병렬로 배치 창을 띄울 때 쓴다. 뜻 없는 조각으로 창을 띄우면
    자동 그룹 배치(pair_key)까지 빗나가 오히려 손해라, **전부 확실할 때만** 값을 준다.
    중복 해소는 호출부가 한다(df_honey_group 과 같은 규칙을 써야 하므로).
    """
    if str(product_type or "").upper() != "PMIC":
        return None
    out = []
    for p in paths:
        base = pmic_source_name(p)
        if not base:
            return None
        out.append(base)
    return out


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

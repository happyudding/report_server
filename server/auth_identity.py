"""요청 신원 확인 — provider 체인 (SSO-ready 추상화).

기본 provider 는 Honey 내장 브라우저가 User-Agent 에 넣는
``HoneyUser/<percent-encoded-계정>`` 토큰이다 (검색결과 페이지 JS 와 동일 규칙:
decode → trim → lower). Honey 밖 일반 브라우저는 토큰이 없어 "" 반환 → 읽기 전용.

미래 SSO 전환: 역프록시가 인증 후 신뢰 헤더(예: X-Auth-User)에 계정을 넣어주면
env ``AUTH_SSO_HEADER=X-Auth-User`` 설정만으로 그 헤더가 UA 보다 우선 사용된다
— 코드 변경 없음. 이때 역프록시가 외부 요청의 동명 헤더를 반드시 제거(strip)해야
위조를 막을 수 있다 (헤더 신뢰의 전제).

[SSO 전환 시 재검토] ``uploaded_by`` 가 비어 있는 legacy 세션은 is_uploader 가
신원만 있으면 True 를 반환한다 (현행 동작 유지) — SSO 도입 시 이 우회를 제거할 것.
"""
import os
import re
from urllib.parse import unquote

from flask import request

_HONEY_UA_RE = re.compile(r"HoneyUser/(\S+)")

# 역프록시 SSO 신뢰 헤더 이름. 비어 있으면(기본) Honey UA provider 만 사용.
AUTH_SSO_HEADER = os.getenv("AUTH_SSO_HEADER", "").strip()


def _from_sso_header():
    """신뢰 헤더 provider — AUTH_SSO_HEADER 미설정이면 항상 ""."""
    if not AUTH_SSO_HEADER:
        return ""
    value = request.headers.get(AUTH_SSO_HEADER) or ""
    # 'DOMAIN\\user' 형식 허용 — 저장/비교 규칙(_current_user 규칙)과 동일하게 정규화
    return value.split("\\")[-1].strip().lower()


def _from_honey_ua():
    """Honey 내장 브라우저 UA 토큰 provider (현행 기본)."""
    m = _HONEY_UA_RE.search(str(request.user_agent) or "")
    if not m:
        return ""
    try:
        return unquote(m.group(1)).strip().lower()
    except Exception:
        return ""


def current_user():
    """현재 요청의 PC 사용자 ID. provider 순서: SSO 신뢰 헤더 → Honey UA.

    신원이 없으면 "" (읽기 전용). 반환값은 소문자 정규화된 계정 문자열."""
    return _from_sso_header() or _from_honey_ua()


def is_uploader(session, uid):
    """uid 가 세션 업로더인지. uploaded_by 는 'DOMAIN\\user' 또는 'user' 형식이라
    뒷부분만 비교. 업로더 기록이 없는 legacy 세션은 신원만 있으면 True
    ([SSO 전환 시 재검토] — 모듈 docstring 참조)."""
    if not uid:
        return False
    ub = str((session or {}).get("uploaded_by") or "")
    if not ub:
        return True
    return ub.split("\\")[-1].strip().lower() == uid

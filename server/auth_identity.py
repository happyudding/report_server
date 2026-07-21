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

from flask import request, session as _flask_session

_HONEY_UA_RE = re.compile(r"HoneyUser/(\S+)")

# 웹 로그인 세션 쿠키에 담기는 키 (routes_misc.py auth 라우트가 설정/삭제).
_LOGIN_SESSION_KEY = "uid"

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


def _from_login_session():
    """웹 로그인 세션 provider — 일반 브라우저가 사번+PIN 으로 로그인한 경우.

    값은 로그인 시점에 이미 정규화되어 저장되므로 그대로 돌려준다."""
    try:
        return (_flask_session.get(_LOGIN_SESSION_KEY) or "").strip().lower()
    except Exception:
        # 요청 컨텍스트 밖이거나 SECRET_KEY 미설정 등 — 신원 없음으로 처리
        return ""


def current_user():
    """현재 요청의 사용자 ID. provider 순서: SSO 신뢰 헤더 → Honey UA → 웹 로그인 세션.

    Honey UA 를 로그인 세션보다 **앞에** 둔다 — Honey 사용자는 현행 동작이 그대로
    유지되고(회귀 0), 로그인 세션은 UA 토큰이 없는 일반 브라우저에서만 발동한다.

    신원이 없으면 "" (읽기 전용). 반환값은 소문자 정규화된 계정 문자열."""
    return _from_sso_header() or _from_honey_ua() or _from_login_session()


def identity_source():
    """신원의 출처. "sso" | "honey" | "login" | "" (신원 없음).

    용도는 2개뿐이다: (1) 웹 PIN 설정을 Honey 접속으로 제한, (2) 프런트가
    'Honey 전용 기능' 안내를 띄울지 판단. 접근제어 자체에는 쓰지 않는다."""
    if _from_sso_header():
        return "sso"
    if _from_honey_ua():
        return "honey"
    if _from_login_session():
        return "login"
    return ""


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

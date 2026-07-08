"""업로더 자동 식별 — Windows 로그인 계정/PC 이름 수집.

manifest 최상위 "client" 키로 서버에 전달된다. 수집 실패가 업로드를 깨면
안 되므로 각 항목은 개별적으로 실패 시 "" 로 대체한다.
"""
from __future__ import annotations

import getpass
import os
import socket


def collect() -> dict:
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    try:
        host = socket.gethostname()
    except Exception:
        host = ""
    try:
        domain = os.environ.get("USERDOMAIN", "")
    except Exception:
        domain = ""
    return {"user": user, "host": host, "domain": domain}

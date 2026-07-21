"""서버/전송/버전 상수.

honey 엔진·UI 의 로컬 설정(config.py: CONFIG_DIR, CHART_COLORS_PATH, D1_STORAGE_DIR)과
분리된, "서버와 말하기 위한" 상수만 모은다.

**서버 주소 정본은 server/env/server.env 의 SERVER_BASE_URL 하나다.**
개발 실행(repo 에서 python honey_main.py)은 그 파일을 직접 읽는다. 빌드본은 repo 가
없으므로 build_zip(release_honey.ps1)이 빌드 시점에 그 값을 Honey.exe 옆 honey.env
로 복사해 넣고, 실행 시 그 파일을 읽는다. 즉 주소를 바꿀 때 고칠 곳은 server.env 한 곳이며,
클라이언트에 반영하려면 build_zip 을 다시 돌리면 된다.

SERVER_BASE_URL 우선순위:
  1. HONEY_SERVER_URL 환경변수 (그 PC 에서만 일시적으로 다른 서버를 볼 때)
  2. env 파일의 SERVER_BASE_URL (위 설명의 정본 경로)
  3. 아래 하드코딩 폴백 (env 파일이 유실됐을 때만)
"""
import os
import sys
from pathlib import Path


def _env_file_paths():
    """SERVER_BASE_URL 을 찾을 env 파일들 — 앞에 있는 것이 우선.

    빌드본(onedir)은 Honey.exe 옆 honey.env 를 본다. 운영자가 재빌드 없이 이 파일만
    고쳐 서버 주소를 바꿀 수도 있다(단, 다음 자동 업데이트 때 배포본 값으로 덮인다).
    개발 실행이면 repo 의 server/env/server.env 를 그대로 읽는다.
    """
    if getattr(sys, "frozen", False):
        return [Path(sys.executable).parent / "honey.env"]
    repo_root = Path(__file__).resolve().parent.parent.parent
    return [repo_root / "server" / "env" / "server.env"]


def _env_file_value(name):
    """env 파일에서 name 값을 읽는다 (KEY=VALUE, '#' 은 주석). 없으면 None."""
    for path in _env_file_paths():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                value = value.strip()
                if value:
                    return value
    return None


SERVER_BASE_URL = (
    os.environ.get("HONEY_SERVER_URL")
    or _env_file_value("SERVER_BASE_URL")
    or "http://12.81.220.117:8080"
)

CURRENT_VERSION = "3.0.1"

REQUEST_TIMEOUT_SEC = (10, 300)  # (connect_timeout, read_timeout)

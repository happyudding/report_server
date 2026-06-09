"""Honey 클라이언트 로컬/UI 설정.

서버·전송·버전 상수(SERVER_BASE_URL, REQUEST_TIMEOUT_SEC, CURRENT_VERSION)는
transport/config.py 로 분리됨. 여기엔 로컬 파일·UI 관련 설정만 둔다.
"""
import os
import sys
from pathlib import Path



def _config_dir():
    """사용자별 설정 저장 폴더 (%APPDATA%/Honey, 없으면 홈)."""
    base = os.environ.get("APPDATA") or str(Path.home())
    return str(Path(base) / "Honey")


# 사용자 설정 디렉토리 + 차트 색 팔레트 파일 경로
CONFIG_DIR = os.environ.get("HONEY_CONFIG_DIR", _config_dir())
CHART_COLORS_PATH = str(Path(CONFIG_DIR) / "chart_colors.json")


def _default_stdinfo_db():
    """stdinfo DB 기본 경로. frozen exe 는 exe 폴더 또는 상위 DB/INFORMATION/ 에서 탐색."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        candidates = [
            base / "stdinfo_20260511.db",
            base.parent / "DB" / "INFORMATION" / "stdinfo_20260511.db",
        ]
    else:
        base = Path(__file__).resolve().parent.parent  # report_server/
        candidates = [base / "DB" / "INFORMATION" / "stdinfo_20260511.db"]
    for p in candidates:
        if p.exists():
            return str(p)
    return ""


STDINFO_DB_PATH = os.environ.get("HONEY_STDINFO_DB", _default_stdinfo_db())

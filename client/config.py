"""Honey 클라이언트 로컬/UI 설정.

서버·전송·버전 상수(SERVER_BASE_URL, REQUEST_TIMEOUT_SEC, CURRENT_VERSION)는
transport/config.py 로 분리됨. 여기엔 로컬 파일·UI 관련 설정만 둔다.
"""
import os
import sys
from pathlib import Path


def _default_d1_dir():
    """d1_storage(가상 서버 스토리지) 기본 경로.

    frozen(exe) → 실행 파일 폴더, 스크립트 → 이 파일 폴더 아래 d1_storage.
    HONEY_D1_STORAGE 환경변수로 실제 서버 스토리지 경로를 가리킬 수 있다.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent
    return str(base / "d1_storage")


# S3 외 별도 서버 스토리지(가상 폴더). CSV/xlsx 입력을 여기서 검색·선택한다.
D1_STORAGE_DIR = os.environ.get("HONEY_D1_STORAGE", _default_d1_dir())


def _config_dir():
    """사용자별 설정 저장 폴더 (%APPDATA%/Honey, 없으면 홈)."""
    base = os.environ.get("APPDATA") or str(Path.home())
    return str(Path(base) / "Honey")


# 사용자 설정 디렉토리 + 차트 색 팔레트 파일 경로
CONFIG_DIR = os.environ.get("HONEY_CONFIG_DIR", _config_dir())
CHART_COLORS_PATH = str(Path(CONFIG_DIR) / "chart_colors.json")

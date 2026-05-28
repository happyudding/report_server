"""Honey 클라이언트 설정.

빌드 시 SERVER_BASE_URL 은 환경별로 다르게 주입 가능.
"""
import os
import sys
from pathlib import Path

SERVER_BASE_URL = os.environ.get("HONEY_SERVER_URL", "http://127.0.0.1:8000")

CURRENT_VERSION = "0.1.0"

REQUEST_TIMEOUT_SEC = 30


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

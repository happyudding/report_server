"""Honey 클라이언트 설정.

빌드 시 SERVER_BASE_URL 은 환경별로 다르게 주입 가능.
"""
import os

SERVER_BASE_URL = os.environ.get("HONEY_SERVER_URL", "http://127.0.0.1:8000")

CURRENT_VERSION = "0.1.0"

REQUEST_TIMEOUT_SEC = 30

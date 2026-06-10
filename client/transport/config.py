"""서버/전송/버전 상수.

honey 엔진·UI 의 로컬 설정(config.py: CONFIG_DIR, CHART_COLORS_PATH, D1_STORAGE_DIR)과
분리된, "서버와 말하기 위한" 상수만 모은다. 빌드 시 SERVER_BASE_URL 은 환경별로
다르게 주입 가능(HONEY_SERVER_URL).
"""
import os

SERVER_BASE_URL = os.environ.get("HONEY_SERVER_URL", "http://12.81.220.117:8080")

CURRENT_VERSION = "3.0.0"

REQUEST_TIMEOUT_SEC = (10, 300)  # (connect_timeout, read_timeout)

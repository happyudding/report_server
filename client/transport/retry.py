"""멱등 GET 요청 재시도 헬퍼.

일시 오류(연결 실패/타임아웃/5xx)에 한해 짧은 백오프 후 재시도한다.
POST(업로드)는 서버에 부분 반영됐을 수 있어 자동 재시도하지 않는다 — 실패 시
사용자에게 알리고 수동 재시도에 맡긴다 (post_grids/post_webreport 호출측 유지).
"""
from __future__ import annotations

import time

import requests

RETRIES = 2          # 첫 시도 + 재시도 2회
BACKOFF_SEC = 1.5    # 재시도 간격 (선형 증가: 1.5s, 3.0s)


def get_with_retry(url, *, timeout, retries=RETRIES, backoff_sec=BACKOFF_SEC, **kwargs):
    """requests.get 을 일시 오류에 한해 재시도한다.

    - 연결 오류/타임아웃: 재시도.
    - 5xx 응답: 재시도 (서버 일시 장애 가정).
    - 그 외(2xx/3xx/4xx): 즉시 반환 — 상태 코드 처리(raise_for_status 등)는 호출측 유지.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff_sec * (attempt + 1))
                continue
            raise
        if resp.status_code >= 500 and attempt < retries:
            time.sleep(backoff_sec * (attempt + 1))
            continue
        return resp
    raise last_exc   # pragma: no cover — 위 루프에서 항상 return/raise 됨

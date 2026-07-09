"""Excel Download 용 서버 데이터 수신 — Qt/xlwings 비의존.

서버는 데이터만 내려주고(기존 엔드포인트, 서버 코드 무수정) 연산은 전부 클라이언트가 한다:
  - GET /pe/report/session/<sid>/full                      → 모든 탭 데이터 (gzip JSON)
  - GET /pe/report/session/<sid>/web_report/distribution   → ECDF 전량 columnar (gzip)

두 GET 은 스레드로 동시에 실행한다. requests 가 Content-Encoding: gzip 을 자동 해제한다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests

try:
    from transport.config import REQUEST_TIMEOUT_SEC
except Exception:  # 단독 실행/테스트 폴백
    REQUEST_TIMEOUT_SEC = (10, 300)


def _get_json(url):
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()


def fetch_report_data(server_base, session_id):
    """(full_payload, dist_payload) 를 동시에 받아 반환.

    full_payload["web_report"] 가 없으면(legacy xlsx 세션 등) ValueError —
    Excel Download 는 web_report 세션 전용이다.
    """
    base = str(server_base).rstrip("/")
    full_url = f"{base}/pe/report/session/{session_id}/full"
    dist_url = f"{base}/pe/report/session/{session_id}/web_report/distribution"

    with ThreadPoolExecutor(max_workers=2) as pool:
        full_f = pool.submit(_get_json, full_url)
        dist_f = pool.submit(_get_json, dist_url)
        full = full_f.result()
        dist = dist_f.result()

    report = full.get("web_report")
    if not isinstance(report, dict) or not report.get("sheets"):
        raise ValueError("web_report 세션이 아닙니다 — Excel Download 는 웹 리포트 세션에서만 사용할 수 있습니다.")
    if str(dist.get("format") or "") != "ecdf-columnar-v1":
        raise ValueError(f"지원하지 않는 distribution 포맷: {dist.get('format')!r}")
    return full, dist


def fetch_session_meta(server_base, session_id, timeout=(2, 3)):
    """세션 메타(product/lot_id 등)만 가볍게 조회 — 저장 기본 파일명용.

    실패하면 {} (파일명 폴백은 호출자 책임). UI 스레드에서 부르므로 timeout 을 짧게 잡는다.
    """
    try:
        base = str(server_base).rstrip("/")
        resp = requests.get(f"{base}/pe/report/session/{session_id}", timeout=timeout)
        resp.raise_for_status()
        return resp.json() or {}
    except Exception:
        return {}

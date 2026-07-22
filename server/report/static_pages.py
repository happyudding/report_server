"""HTML 페이지(report_view.html 등) gzip 서빙 헬퍼.

send_file 은 매 방문마다 202KB 를 비압축으로 보냈다. 여기서는 파일을 읽어 gzip 을
프로세스 RAM 에 캐시하고(mtime/size 변경 시에만 재압축), ETag + Cache-Control: no-cache
로 재방문은 304 로 끝낸다. vendor(.gz 사전압축, max-age=86400)와 달리 HTML 은 수정
배포가 즉시 반영돼야 하므로 max-age 를 주지 않는다.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import threading
from pathlib import Path

from flask import Response, request

_CACHE: dict = {}   # path str -> (mtime_ns, size, etag, raw bytes, gz bytes)
_LOCK = threading.Lock()

# error_beacon.js 참조를 콘텐츠 버전 URL(?v=mtime)로 바꿔 서빙한다 — webreport_static 이
# v 있는 요청을 immutable 로 응답해 재방문 시 재검증 왕복이 사라진다(head 렌더블로킹 스크립트).
# 버전은 프로세스 시작 시 1회 계산: 배포=재시작이라 충분하고, 파일만 바꾸면 반영이 안 된다.
_BEACON_PATH = Path(__file__).parent / "static" / "webreport" / "error_beacon.js"
try:
    _BEACON_SUB = (b'error_beacon.js"',
                   b'error_beacon.js?v=%d"' % int(_BEACON_PATH.stat().st_mtime))
except OSError:
    _BEACON_SUB = None


def _load(path: Path):
    st = os.stat(path)
    key = str(path)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached
    raw = path.read_bytes()
    if _BEACON_SUB:
        raw = raw.replace(*_BEACON_SUB)
    etag = '"' + hashlib.sha256(raw).hexdigest()[:32] + '"'
    gz = gzip.compress(raw, compresslevel=6)   # 파일 변경 시 1회만 — 고압축이 이득
    entry = (st.st_mtime_ns, st.st_size, etag, raw, gz)
    with _LOCK:
        _CACHE[key] = entry
    return entry


def send_html_gzip(path: Path) -> Response:
    """HTML 파일을 gzip(Accept-Encoding 시)+ETag 로 응답. If-None-Match 일치 시 304."""
    _, _, etag, raw, gz = _load(path)
    headers = {
        "ETag": etag,
        "Cache-Control": "no-cache",
        "Vary": "Accept-Encoding",
    }
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
        body = gz
    else:
        body = raw
    return Response(body, mimetype="text/html", headers=headers)

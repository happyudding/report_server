"""/full·/scatter 응답의 JSON 직렬화+gzip bytes LRU 캐시.

service.py 의 _REPORT_CACHE 는 report dict 까지만 캐시해서, warm 요청도 매번 수 MB
payload 의 json.dumps + gzip.compress 를 waitress 워커 스레드에서 재수행했다.
여기서는 dist(_DIST_CACHE) 패턴과 동일하게 최종 gzip bytes 를 캐시해 warm 요청을
bytes 반환만으로 끝낸다.

캐시는 전부 프로세스 RAM(LRU 개수 상한, env 로 조절) — 상한 초과 시 오래 안 쓴
항목부터 자동 퇴출되므로 별도 삭제 주기가 필요 없다. 키 첫 요소가 analysis_key 인
규약을 지켜 service._AKEY_CACHES 에 등록하면 편집·세션삭제 무효화에 자동 편입된다.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from collections import OrderedDict
from pathlib import Path

from . import service

# /full: 키 (akey, chash, manifest_digest, extras_digest) -> gzip bytes.
# extras_digest 에 annotations/is_important 등 값싼 부분 전부가 들어가므로
# 그 변경들도 키가 바뀌어 자연 무효화된다 (구 키는 LRU 퇴출로 회수).
_FULL_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_FULL_CACHE", "8") or 8))
_FULL_CACHE: OrderedDict = OrderedDict()

# /scatter: 키 (akey, chash, subject) -> gzip bytes. scatter 는 manifest 를 쓰지
# 않으므로(tables 만) manifest digest 는 키에 불필요하다.
_SCATTER_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_SCATTER_CACHE", "16") or 16))
_SCATTER_CACHE: OrderedDict = OrderedDict()

service._AKEY_CACHES.extend([_FULL_CACHE, _SCATTER_CACHE])


def _gzip_json(obj) -> bytes:
    return gzip.compress(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=1)


def get_full_gzip(session_id: str, *, session: dict, extras: dict,
                  report_db, upload_root: Path) -> tuple[str, bytes]:
    """/full 응답(payload 전체)의 gzip bytes 를 캐시해 (etag, bytes) 로 반환.

    extras: 라우트가 조립한 값싼 부분 전부(session public/summary/charts/issue_images/
    distribution_url/csv_files/objects/annotations). 여기에 load_webreport 결과
    (summary_text 계열 + web_report)를 합쳐 기존 payload 와 동일한 형태를 만든다.
    """
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    content_hash = str(session.get("content_hash") or "")
    manifest = service._load_manifest_cached(analysis_key, upload_root)
    manifest_digest = hashlib.sha256(service._canon(manifest)).hexdigest()
    extras_digest = hashlib.sha256(service._canon(extras)).hexdigest()
    cache_key = (analysis_key, content_hash, manifest_digest, extras_digest)
    etag = '"' + hashlib.sha256(repr(cache_key).encode("utf-8")).hexdigest()[:32] + '"'

    blob = service._cache_get(_FULL_CACHE, cache_key)
    if blob is not None:
        return etag, blob
    with service._keyed_lock(("full",) + cache_key):
        blob = service._cache_get(_FULL_CACHE, cache_key)
        if blob is not None:
            return etag, blob
        _, report = service.load_webreport(
            session_id, report_db=report_db, upload_root=upload_root, session=session)
        sheets = report.get("sheets", {})
        payload = dict(extras)
        payload["summary_text"] = sheets.get("Summary")
        payload["yield_text"] = sheets.get("Yield")
        payload["issue_table_text"] = sheets.get("Issue Table")
        payload["web_report"] = report
        blob = _gzip_json(payload)
        service._cache_put(_FULL_CACHE, cache_key, blob, _FULL_CACHE_MAX)
    return etag, blob


def get_scatter_gzip(session_id: str, subject: str, *, session: dict,
                     report_db, upload_root: Path) -> bytes:
    """/scatter 응답의 gzip bytes 를 캐시해 반환 (같은 항목 반복 클릭 시 재계산 제거)."""
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    cache_key = (analysis_key, str(session.get("content_hash") or ""), subject)

    blob = service._cache_get(_SCATTER_CACHE, cache_key)
    if blob is not None:
        return blob
    with service._keyed_lock(("scatter",) + cache_key):
        blob = service._cache_get(_SCATTER_CACHE, cache_key)
        if blob is not None:
            return blob
        result = service.scatter_item(
            session_id, subject, report_db=report_db, upload_root=upload_root)
        blob = _gzip_json(result)
        service._cache_put(_SCATTER_CACHE, cache_key, blob, _SCATTER_CACHE_MAX)
    return blob

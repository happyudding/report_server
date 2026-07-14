"""/full·/scatter 응답의 JSON 직렬화+gzip bytes LRU 캐시.

service.py 의 _REPORT_CACHE 는 report dict 까지만 캐시해서, warm 요청도 매번 수 MB
payload 의 json.dumps + gzip.compress 를 waitress 워커 스레드에서 재수행했다.
여기서는 dist(_DIST_CACHE) 패턴과 동일하게 최종 gzip bytes 를 캐시해 warm 요청을
bytes 반환만으로 끝낸다.

캐시는 전부 프로세스 RAM(LRU 개수 상한, env 로 조절) — 상한 초과 시 오래 안 쓴
항목부터 자동 퇴출되므로 별도 삭제 주기가 필요 없다. 키 첫 요소가 analysis_key 인
규약을 지켜 cache.register_akey_cache 로 등록하면 편집·세션삭제 무효화에 자동 편입된다.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from collections import OrderedDict
from pathlib import Path

from . import cache, cache_policy, service
from .validation import canon

# /full: 키 (akey, chash, "session:edits_rev", extras_digest) -> gzip bytes.
# comment/override 편집은 세션 편집 rev 증가로, annotations/is_important 등 값싼
# 부분 변경은 extras_digest 로 키가 바뀌어 자연 무효화된다 (구 키는 LRU 퇴출로 회수).
_FULL_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_FULL_CACHE", "8") or 8))
_FULL_CACHE: OrderedDict = OrderedDict()

# /scatter: 키 (akey, chash, subject) -> gzip bytes. scatter 는 manifest 를 쓰지
# 않으므로(tables 만) manifest digest 는 키에 불필요하다.
_SCATTER_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_SCATTER_CACHE", "16") or 16))
_SCATTER_CACHE: OrderedDict = OrderedDict()

cache.register_akey_cache(_FULL_CACHE)
cache.register_akey_cache(_SCATTER_CACHE)


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
    # 편집(comment/override)은 세션 단위 DB 가 진실 — manifest 는 불변 스냅샷이므로
    # digest 대신 (session_id, edits_rev)로 편집 무효화를 잡는다 (키 규약: cache_policy).
    edits_rev = report_db.get_webreport_edit_rev(session_id)
    extras_digest = hashlib.sha256(canon(extras)).hexdigest()
    cache_key = cache_policy.full_key(session, session_id, edits_rev, extras_digest)
    etag = '"' + hashlib.sha256(repr(cache_key).encode("utf-8")).hexdigest()[:32] + '"'

    blob = cache.cache_get(_FULL_CACHE, cache_key)
    if blob is not None:
        return etag, blob
    with cache.keyed_lock(("full",) + cache_key):
        blob = cache.cache_get(_FULL_CACHE, cache_key)
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
        cache.cache_put(_FULL_CACHE, cache_key, blob, _FULL_CACHE_MAX)
    return etag, blob


def get_scatter_gzip(session_id: str, subject: str, *, session: dict,
                     report_db, upload_root: Path, bin1: bool = False) -> bytes:
    """/scatter 응답의 gzip bytes 를 캐시해 반환 (같은 항목 반복 클릭 시 재계산 제거).

    ``bin1`` 변형(양품만)은 별도 캐시 키(scatter_key(bin1=True))로 전체 기준과 분리한다.
    """
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    cache_key = cache_policy.scatter_key(session, subject, bin1=bin1)   # 키 규약: cache_policy

    blob = cache.cache_get(_SCATTER_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock(("scatter",) + cache_key):
        blob = cache.cache_get(_SCATTER_CACHE, cache_key)
        if blob is not None:
            return blob
        result = service.scatter_item(
            session_id, subject, report_db=report_db, upload_root=upload_root, bin1=bin1)
        blob = _gzip_json(result)
        cache.cache_put(_SCATTER_CACHE, cache_key, blob, _SCATTER_CACHE_MAX)
    return blob

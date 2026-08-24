"""/full·/scatter 응답의 JSON 직렬화+gzip bytes LRU 캐시.

service.py 의 _REPORT_CACHE 는 report dict 까지만 캐시해서, warm 요청도 매번 수 MB
payload 의 json.dumps + gzip.compress 를 waitress 워커 스레드에서 재수행했다.
여기서는 dist(_DIST_CACHE) 패턴과 동일하게 최종 gzip bytes 를 캐시해 warm 요청을
bytes 반환만으로 끝낸다.

캐시는 전부 프로세스 RAM(LRU **개수 + 바이트** 이중 상한, env 로 조절) — 상한 초과 시
오래 안 쓴 항목부터 자동 퇴출되므로 별도 삭제 주기가 필요 없다. 값이 전부 gzip bytes 라
크기 측정이 len() 으로 끝나므로 dist/map 캐시와 같은 cache._bytes_capped_put 을 쓴다
(개수 상한만 두면 대형 세션 blob 몇 개로 RAM 이 예측 불가로 부푼다). 키 첫 요소가
analysis_key 인 규약을 지켜 cache.register_akey_cache 로 등록하면 편집·세션삭제
무효화에 자동 편입된다.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path

from . import cache, cache_policy, gap_chart, preprocess, service
from .validation import canon

_log = logging.getLogger(__name__)

# /full: 키 (akey, chash, "session:edits_rev", extras_digest) -> gzip bytes.
# comment/override 편집은 세션 편집 rev 증가로, annotations/is_important 등 값싼
# 부분 변경은 extras_digest 로 키가 바뀌어 자연 무효화된다 (구 키는 LRU 퇴출로 회수).
_FULL_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_FULL_CACHE", "8") or 8))
_FULL_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_FULL_CACHE_MB", "512")
                                   or 512)) * 1024 * 1024   # 0 = 바이트 상한 비활성
_FULL_CACHE: OrderedDict = OrderedDict()

# /scatter: 키 (akey, chash, subject) -> gzip bytes. scatter 는 manifest 를 쓰지
# 않으므로(tables 만) manifest digest 는 키에 불필요하다.
_SCATTER_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_SCATTER_CACHE", "16") or 16))
_SCATTER_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_SCATTER_CACHE_MB", "256")
                                      or 256)) * 1024 * 1024   # 0 = 바이트 상한 비활성
_SCATTER_CACHE: OrderedDict = OrderedDict()

# /distribution_batch: 키 (akey, chash, mode, subjects_digest[, "bin1"]) -> gzip bytes.
# 갤러리 스크롤이 같은 배치를 되짚는 경우(위/아래 왕복)가 잦아 개수 상한을 넉넉히 둔다 —
# 배치 하나는 항목 수십 개분이지만 소스·die 가 많은 세션에선 건당 수 MB 가 되므로
# 바이트 상한이 실질 상한 역할을 한다.
_DIST_BATCH_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_DIST_BATCH_CACHE", "64") or 64))
_DIST_BATCH_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_DIST_BATCH_CACHE_MB", "256")
                                         or 256)) * 1024 * 1024   # 0 = 바이트 상한 비활성
_DIST_BATCH_CACHE: OrderedDict = OrderedDict()

# /distribution_batch?order=seq (Serial 순): 키 (…, subjects_digest, "seq", ver[, "bin1"]) -> gzip.
# ECDF 배치와 **별도 캐시**다 — 같은 항목 집합이라도 축 의미가 다른 응답이라 섞이면 안 되고,
# seq 는 pack 지름길이 없어(순서 보존) 건당 계산이 비싸 히트율을 따로 지키는 편이 낫다.
_DIST_SEQ_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_DIST_SEQ_CACHE", "32") or 32))
_DIST_SEQ_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_DIST_SEQ_CACHE_MB", "256")
                                       or 256)) * 1024 * 1024   # 0 = 바이트 상한 비활성
_DIST_SEQ_CACHE: OrderedDict = OrderedDict()

# Gap Chart: 키 (akey, chash[, prep], mode, chart_id, spec_digest, gver[, "bin1"]) -> gzip bytes.
# 한 응답을 갤러리 카드와 Item_detail 상세가 **공유**한다(카드가 ECDF 를 프런트에서 만든다)
# → 카드를 본 뒤 클릭하면 상세는 클라 캐시 히트다. 건당 크기가 scatter 급이라 바이트 상한이
# 실질 상한 역할을 한다.
_GAP_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_GAP_CACHE", "16") or 16))
_GAP_CACHE_MAX_BYTES = max(0, int(os.getenv("WEB_REPORT_GAP_CACHE_MB", "256")
                                  or 256)) * 1024 * 1024   # 0 = 바이트 상한 비활성
_GAP_CACHE: OrderedDict = OrderedDict()
# 저장 1회에 프리컴퓨트할 차트 수 상한 — 한 번에 여러 개를 저장해도 웹 프로세스를 오래
# 점유하지 않게. 넘치는 것은 종전처럼 조회 시점에 계산된다.
_GAP_WARM_MAX = max(0, int(os.getenv("WEB_REPORT_GAP_WARM_MAX", "2") or 0))

cache.register_akey_cache(_FULL_CACHE)
cache.register_akey_cache(_SCATTER_CACHE)
cache.register_akey_cache(_DIST_BATCH_CACHE)
cache.register_akey_cache(_DIST_SEQ_CACHE)
cache.register_akey_cache(_GAP_CACHE)


def _stats() -> dict:
    """cache_stats()["response"] 에 실릴 캐시별 건수·바이트 (관리자 패널 노출용).

    cache.cache_stats 가 CACHE_LOCK 을 놓은 뒤 부르므로 여기서 다시 잡아도 안전하다
    (cache 가 response_cache 를 import 하면 순환이라 콜백으로 등록한다)."""
    with cache.CACHE_LOCK:
        return {name: {"n": len(c), "bytes": sum(len(v) for v in c.values())}
                for name, c in (("full", _FULL_CACHE), ("scatter", _SCATTER_CACHE),
                                ("dist_batch", _DIST_BATCH_CACHE),
                                ("dist_seq", _DIST_SEQ_CACHE), ("gap", _GAP_CACHE))}


cache.register_stats_provider(_stats)


def _gzip_json(obj) -> bytes:
    return gzip.compress(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=1)


def get_full_gzip(session_id: str, *, session: dict, extras: dict,
                  report_db, upload_root: Path,
                  build_if_cold: bool = True) -> tuple[str, bytes]:
    """/full 응답(payload 전체)의 gzip bytes 를 캐시해 (etag, bytes) 로 반환.

    extras: 라우트가 조립한 값싼 부분 전부(session public/summary/charts/issue_images/
    distribution_url/csv_files/objects/annotations). 여기에 load_webreport 결과
    (summary_text 계열 + web_report)를 합쳐 기존 payload 와 동일한 형태를 만든다.

    ``build_if_cold=False`` 면 콜드 빌드가 필요한 시점에 service.ColdBuildRequired 를
    올린다 — 라우트가 202 로 즉시 응답하고 빌드는 백그라운드에 맡기기 위함이다.
    이 gzip 캐시 자체의 미스는 콜드가 아니다(report 가 웜이면 직렬화만 하면 됨).
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
    with cache.keyed_lock_ctx(("full",) + cache_key):
        blob = cache.cache_get(_FULL_CACHE, cache_key)
        if blob is not None:
            return etag, blob
        _, report = service.load_webreport(
            session_id, report_db=report_db, upload_root=upload_root, session=session,
            build_if_cold=build_if_cold)
        sheets = report.get("sheets", {})
        payload = dict(extras)
        payload["summary_text"] = sheets.get("Summary")
        payload["yield_text"] = sheets.get("Yield")
        payload["issue_table_text"] = sheets.get("Issue Table")
        payload["web_report"] = report
        blob = _gzip_json(payload)
        pending = [k for k, flag in (("ai", "ai_comment_pending"),
                                     ("cmp", "compare_pending")) if report.get(flag)]
        if pending:
            # 백그라운드 계산 중의 임시 응답 — 캐시하면 완료 후에도 같은 키로 stale 이
            # 서빙된다(키에 그 상태가 없음). etag 도 최종본과 갈라 pending 응답의
            # If-None-Match 가 최종본 304 로 오인되지 않게 한다. 꼬리표에 **무엇이**
            # 대기 중인지 담아, AI 만 끝나고 Compare 는 아직인 중간 상태도 갈린다.
            return etag[:-1] + "-" + "".join(pending) + '"', blob
        cache._bytes_capped_put(_FULL_CACHE, cache_key, blob,
                                _FULL_CACHE_MAX, _FULL_CACHE_MAX_BYTES)
    return etag, blob


def get_dist_batch_gzip(session_id: str, subjects, *, session: dict,
                        report_db, upload_root: Path, bin1: bool = False,
                        bin1_scope: str = "") -> tuple[str, bytes]:
    """/distribution_batch 응답의 gzip bytes 를 캐시해 (etag, bytes) 로 반환.

    subjects 는 **정렬·중복제거된 리스트**여야 한다(라우트가 정규화) — 같은 항목 집합을
    다른 순서로 요청해도 같은 캐시 키가 되도록 하기 위함이다. etag 는 캐시 키에서 파생해
    배치 구성·변형(bin1)이 다르면 서로의 304 로 오염되지 않는다.
    """
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    digest = hashlib.sha256("\n".join(subjects).encode("utf-8")).hexdigest()[:32]
    # scope 는 실제로 적용되는 세션(Temperature+RT)일 때만 키에 들어간다 — 그 외에는
    # 종전 키와 완전히 동일해 기존 캐시가 유효하다(service._bin1_source_filter 와 같은 판정).
    scope = "rt" if service._bin1_source_filter(session, bin1_scope) else ""
    cache_key = cache_policy.dist_batch_key(   # 키 규약: cache_policy
        session, digest, bin1=bin1, bin1_scope=scope,
        prep_digest=preprocess.session_digest(report_db, session_id))
    etag = '"' + hashlib.sha256(repr(cache_key).encode("utf-8")).hexdigest()[:32] + '"'

    blob = cache.cache_get(_DIST_BATCH_CACHE, cache_key)
    if blob is not None:
        return etag, blob
    with cache.keyed_lock_ctx(("dist_batch",) + cache_key):
        blob = cache.cache_get(_DIST_BATCH_CACHE, cache_key)
        if blob is not None:
            return etag, blob
        result = service.get_distribution_batch(
            session_id, subjects, report_db=report_db, upload_root=upload_root,
            bin1=bin1, bin1_scope=scope)
        blob = _gzip_json(result)
        cache._bytes_capped_put(_DIST_BATCH_CACHE, cache_key, blob,
                                _DIST_BATCH_CACHE_MAX, _DIST_BATCH_CACHE_MAX_BYTES)
    return etag, blob


def get_dist_seq_batch_gzip(session_id: str, subjects, *, session: dict,
                            report_db, upload_root: Path, bin1: bool = False,
                            bin1_scope: str = "") -> tuple[str, bytes]:
    """/distribution_batch?order=seq 응답 gzip bytes 를 캐시해 (etag, bytes) 로 반환.

    `get_dist_batch_gzip` 과 같은 골격이고 키 빌더(dist_seq_batch_key)와 계산 함수만 다르다.
    subjects 는 라우트가 정렬·중복제거한 리스트여야 한다(같은 집합 → 같은 키).
    """
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    digest = hashlib.sha256("\n".join(subjects).encode("utf-8")).hexdigest()[:32]
    scope = "rt" if service._bin1_source_filter(session, bin1_scope) else ""
    cache_key = cache_policy.dist_seq_batch_key(   # 키 규약: cache_policy
        session, digest, bin1=bin1, bin1_scope=scope,
        prep_digest=preprocess.session_digest(report_db, session_id))
    etag = '"' + hashlib.sha256(repr(cache_key).encode("utf-8")).hexdigest()[:32] + '"'

    blob = cache.cache_get(_DIST_SEQ_CACHE, cache_key)
    if blob is not None:
        return etag, blob
    with cache.keyed_lock_ctx(("dist_seq",) + cache_key):
        blob = cache.cache_get(_DIST_SEQ_CACHE, cache_key)
        if blob is not None:
            return etag, blob
        result = service.get_distribution_seq_batch(
            session_id, subjects, report_db=report_db, upload_root=upload_root,
            bin1=bin1, bin1_scope=scope)
        blob = _gzip_json(result)
        cache._bytes_capped_put(_DIST_SEQ_CACHE, cache_key, blob,
                                _DIST_SEQ_CACHE_MAX, _DIST_SEQ_CACHE_MAX_BYTES)
    return etag, blob


def get_scatter_gzip(session_id: str, subject: str, *, session: dict,
                     report_db, upload_root: Path, bin1: bool = False,
                     bin1_scope: str = "") -> bytes:
    """/scatter 응답의 gzip bytes 를 캐시해 반환 (같은 항목 반복 클릭 시 재계산 제거).

    ``bin1`` 변형(양품만)은 별도 캐시 키(scatter_key(bin1=True))로 전체 기준과 분리한다.
    """
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    scope = "rt" if service._bin1_source_filter(session, bin1_scope) else ""
    cache_key = cache_policy.scatter_key(   # 키 규약: cache_policy
        session, subject, bin1=bin1, bin1_scope=scope,
        prep_digest=preprocess.session_digest(report_db, session_id))

    blob = cache.cache_get(_SCATTER_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock_ctx(("scatter",) + cache_key):
        blob = cache.cache_get(_SCATTER_CACHE, cache_key)
        if blob is not None:
            return blob
        result = service.scatter_item(
            session_id, subject, report_db=report_db, upload_root=upload_root,
            bin1=bin1, bin1_scope=scope, session=session)
        blob = _gzip_json(result)
        cache._bytes_capped_put(_SCATTER_CACHE, cache_key, blob,
                                _SCATTER_CACHE_MAX, _SCATTER_CACHE_MAX_BYTES)
    return blob


def get_gap_chart_gzip(session_id: str, chart_id: str, spec_digest: str, *, session: dict,
                       report_db, upload_root: Path, bin1: bool = False,
                       bin1_scope: str = "") -> bytes:
    """Gap Chart 응답 gzip bytes 캐시 (get_scatter_gzip 동형).

    ``spec_digest`` 는 호출부(라우트)가 `gap_chart.spec_digest(spec)` 로 만들어 넘긴다 —
    **같은 값이 ETag 에도 들어가야** 수식 수정 후 stale 304 가 나가지 않는다.
    정의가 없으면 service 가 KeyError (라우트가 404 처리)."""
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    scope = "rt" if service._bin1_source_filter(session, bin1_scope) else ""
    cache_key = cache_policy.gap_key(   # 키 규약: cache_policy
        session, chart_id, spec_digest, bin1=bin1, bin1_scope=scope,
        prep_digest=preprocess.session_digest(report_db, session_id))

    blob = cache.cache_get(_GAP_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock_ctx(("gap",) + cache_key):
        blob = cache.cache_get(_GAP_CACHE, cache_key)
        if blob is not None:
            return blob
        result = service.gap_chart_item(
            session_id, chart_id, report_db=report_db, upload_root=upload_root,
            bin1=bin1, bin1_scope=scope, session=session)
        blob = _gzip_json(result)
        cache._bytes_capped_put(_GAP_CACHE, cache_key, blob,
                                _GAP_CACHE_MAX, _GAP_CACHE_MAX_BYTES)
    return blob


# 저장 직후 프리컴퓨트에 쓰는 스레드 — 한 번에 하나만 돌린다(저장 연타로 스레드가 쌓이거나
# 웹 워커 스레드를 굶기지 않도록).
_GAP_WARM_LOCK = threading.Lock()


def warm_gap_chart(session_id: str, chart_ids, spec_of, *, session: dict,
                   report_db, upload_root: Path) -> None:
    """Gap Chart 응답을 백그라운드에서 미리 만들어 `_GAP_CACHE` 에 넣는다 (best-effort).

    Gap 캐시 키에는 `spec_digest` 가 들어가므로 **새로 만들거나 수식을 고친 직후에는
    100% 캐시 미스**다. 그 계산(5 source × 25,000 die 기준 실측 0.31s + 직렬화·gzip
    0.06s)이 사용자의 첫 조회 요청 안에서 통째로 일어나 카드가 "계산 중…" 으로 머문다.
    저장 응답을 보낸 직후에 시작해 두면 모달이 닫히고 갤러리가 다시 그려지는 사이에
    상당 부분이 진행되고, 뒤늦게 도착한 조회는 같은 `keyed_lock` 에서 결과를 받는다
    (중복 계산이 아니라 대기 후 재사용 — 그래서 낭비가 아니다).

    ⚠️ **반드시 이 프로세스(부모)의 스레드에서 돌아야 한다.** `_GAP_CACHE` 는 웹 프로세스
    RAM 의 OrderedDict 라, compute 워커(별도 프로세스)에서 계산하면 그 결과가 부모 캐시에
    남지 않아 아무 효과가 없다.

    변형은 **전체 기준(bin1=False) 하나만** 데운다 — Bin1 계열 토글이 켜진 채로 저장하면
    빗나가지만, 그때도 종전과 같은 인라인 계산으로 떨어질 뿐 손해는 없다.
    """
    ids = [str(i) for i in (chart_ids or []) if i]
    if not ids:
        return

    def _run():
        if not _GAP_WARM_LOCK.acquire(blocking=False):
            return          # 이미 다른 저장분을 데우는 중 — 조용히 양보(조회가 알아서 만든다)
        try:
            for cid in ids[:_GAP_WARM_MAX]:
                try:
                    spec = spec_of(cid)
                    if not spec:
                        continue
                    get_gap_chart_gzip(session_id, cid, gap_chart.spec_digest(spec),
                                       session=session, report_db=report_db,
                                       upload_root=upload_root)
                except Exception:   # 프리컴퓨트 실패는 사용자에게 영향이 없다(조회가 재시도)
                    _log.debug("gap warm failed: session=%s chart=%s", session_id, cid,
                               exc_info=True)
        finally:
            _GAP_WARM_LOCK.release()

    threading.Thread(target=_run, name="gap-warm", daemon=True).start()

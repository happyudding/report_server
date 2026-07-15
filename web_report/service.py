"""Service layer for /pe/report/upload_webreport and web report rendering.

계층 구성 (2026-07-10 분리, 2026-07-11 Phase 4 추가 분리):
- cache.py      — LRU 캐시 레지스트리·락·무효화 (evict_akey_caches/invalidate_caches)
- validation.py — canon/모드·meta 정규화 등 순수 헬퍼
- loader.py     — 세션 → parquet 다운로드·디코드 → HoneyformTable (tables 캐시 결합)
- ingest.py     — 업로드 ingest (해시→저장→세션 생성→시드→프리웜)
- edits.py      — 편집 상태 (세션 단위 DB — comment/override)
- runtime.py    — 저장소 포트 주입 지점 (ports.StoragePort)
- service.py    — 조회/편집 오케스트레이션 (외부 진입점, 공개 시그니처 불변 재노출 포함)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from . import cache
from . import cache_policy
from . import compute
from . import disk_cache
from . import dist_blob as _dist_blob
from . import edits
from . import runtime
from .honeyform import encode_honeyform_parquet
from .ingest import ingest_webreport  # noqa: F401  (외부 진입점 재노출 — upload_webreport.py)
from .loader import load_tables as _load_tables
from .metrics import build_report_payload
from .tabs import raw_data as raw_data_tab
from .validation import (
    canon as _canon,
    mode_tables as _mode_tables,
    validate_mode as _validate_mode,
    webreport_ai_comment as _webreport_ai_comment,
    webreport_colors as _webreport_colors,
)

# dist blob 전용 gzip 레벨 — 세션당 1회 생성 후 캐시(RAM+disk)되므로 레벨을 올려도 CPU 는
# 1회이고 매 조회 전송량(수십 MB ECDF)이 줄어든다. 실측 후 운영값 결정용 env.
# 대화형 경로(/full·scatter — response_cache._gzip_json)는 level 1 유지.
_DIST_GZIP_LEVEL = max(1, min(9, int(os.getenv("WEB_REPORT_DIST_GZIP_LEVEL", "1") or 1)))

_log = logging.getLogger(__name__)


def invalidate_caches(analysis_key) -> None:
    """akey 산출물이 삭제됐을 때(세션 삭제 등) 인메모리 캐시 전부 정리.

    외부(report_routes/report_cleanup/sessions_admin) 진입점 — 구현은 cache.py."""
    cache.invalidate_caches(analysis_key)


def load_webreport(session_id: str, *, report_db, upload_root: Path,
                   session: dict | None = None) -> tuple[dict, dict]:
    """세션 조회: build_report_payload 결과를 (analysis_key, content_hash, session_id,
    edits_rev) 키로 캐시한다 — comment/override 편집은 세션 편집 rev 증가로 자연 무효화되고,
    raw_data 편집은 content_hash 변경으로 무효화된다. 편집 상태는 세션 단위 DB
    (report_webreport_edit)가 진실이며 manifest 는 업로드 시점 불변 스냅샷이다
    (rev==0 legacy 세션만 manifest 필드로 폴백 — edits.effective_state). 반환 report 는
    캐시 공유 객체 — 호출자는 읽기 전용(jsonify 직렬화)으로만 쓸 것. 콜드 미스 계산은
    single-flight 락으로 중복 실행을 막는다. session 은 라우트가 이미 조회한 세션 dict
    전달용(재조회 생략).

    Distribution ECDF(대용량)는 항상 payload 에서 제외되고 프런트가 get_distribution 으로
    지연 로드한다. Map Analysis dies(대용량)도 payload 에서 제외(경량 메타만)되고
    get_map_gzip 으로 지연 로드한다 (schema v8).
    """
    if session is None:
        session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    # 편집 rev 는 작은 인덱스 SELECT 1회 — warm 요청은 manifest/tables 로드 없이
    # 캐시 키만으로 끝난다. 콜드 미스에서만 manifest(캐시)와 tables 를 로드한다.
    edits_rev = report_db.get_webreport_edit_rev(session_id)

    # F10 웹리포트 옵션(세션 DB, authoritative): Distribution source 색.
    dist_colors = _webreport_colors(session.get("webreport_options") or "")
    mode = _validate_mode(session.get("mode"))

    # 키 구성 규약은 cache_policy 참조 (opts/mode 포함 이유 포함)
    cache_key = cache_policy.report_key(session, session_id, edits_rev)
    report = cache.cache_get(cache.REPORT_CACHE, cache_key)
    if report is None:
        with cache.keyed_lock(("report",) + cache_key):
            report = cache.cache_get(cache.REPORT_CACHE, cache_key)
            if report is None:
                # RAM 미스여도 디스크 캐시(재시작·LRU 퇴출 생존)가 있으면 재계산 생략
                report = disk_cache.load_report(upload_root, cache_key)
                if report is None and compute.should_offload(cache_policy.tables_key(session)):
                    # 콜드 빌드(디코드 포함 수 초 CPU)는 워커 프로세스로 — GIL 비점유.
                    # 워커가 disk_cache 도 채우므로 여기서는 RAM 캐시만 넣는다.
                    report = compute.run(compute.report_job, session_id, str(upload_root))
                if report is None:
                    t0 = time.perf_counter()
                    session, tables, manifest = _load_tables(
                        session_id, report_db=report_db, upload_root=upload_root,
                        session=session)
                    edit_state, _ = edits.effective_state(report_db, session_id, manifest)
                    tables = _mode_tables(tables, mode)
                    # ai_comment 옵션 세션만 eval_analyzer 평가 실행 (콜드 빌드 1회 —
                    # rawdata 편집은 content_hash 변경으로 자동 재평가). 실패는
                    # safe_build 가 빈 dict 로 격리해 빌드가 죽지 않는다.
                    ai_comments = None
                    if _webreport_ai_comment(session.get("webreport_options") or ""):
                        from . import ai_comment
                        ai_comments = ai_comment.safe_build(
                            tables, session,
                            manifest.get("selected_items") or [])
                    report = build_report_payload(
                        tables,
                        selected_items=manifest.get("selected_items") or [],
                        sheets=manifest.get("sheets") or [],
                        etc_items=edit_state["etc_items"],
                        issue_comments=edit_state["issue_comments"],
                        summary_engr=edit_state["summary_engr"],
                        product_type=session.get("product_type", ""),
                        product=session.get("product", ""),
                        mode=mode,
                        dist_colors=dist_colors,
                        ai_comments=ai_comments,
                    )
                    disk_cache.save_report(upload_root, cache_key, report)
                    # 관측 로그 — 콜드 빌드(디코드 포함)가 실데이터에서 얼마나 걸리는지.
                    _log.info(
                        "report cold build akey=%.12s sid=%s sources=%d items=%d %.1fs",
                        str(analysis_key), session_id, len(tables),
                        len(report.get("distribution_index") or ()),
                        time.perf_counter() - t0)
                cache.cache_put(cache.REPORT_CACHE, cache_key, report, cache.REPORT_CACHE_MAX)
    public = dict(session)
    public["has_password"] = bool(public.get("password"))
    public.pop("password", None)
    return public, report


def get_distribution(session_id: str, *, report_db, upload_root: Path, bin1: bool = False) -> dict:
    """Distribution lazy 엔드포인트용 컴팩트 ECDF (전 포인트, 다운샘플 없음).

    계산 본체는 dist_blob.compute_dist_compact — Honey 클라의 업로드 시 프리컴퓨트와
    같은 코드를 공유해 값 일치를 구조적으로 보장한다. selected_items 필터를 빠뜨리면
    distribution_index 와 항목 집합이 어긋난다. tables 는 캐시 클론이라 필터가 안전하다.
    ``bin1`` 이면 양품(BIN==PASS_BIN) die 측정값만으로 ECDF 를 재계산한다("Bin1 only").
    """
    session, tables, manifest = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return _dist_blob.compute_dist_compact(
        tables, manifest.get("selected_items") or [], session.get("mode"), bin1=bin1)


def get_distribution_gzip(session_id: str, *, report_db, upload_root: Path,
                          bin1: bool = False) -> bytes:
    """get_distribution 결과를 JSON→gzip bytes 로 캐시해 반환 (라우트가 그대로 응답).

    계산(수 초 CPU)+직렬화+압축을 세션당 1회만 수행 — 동시 사용자·재방문 모두 캐시 히트.
    키는 tables 캐시와 동일한 (analysis_key, content_hash) — manifest.selected_items 는
    업로드 시 확정되어 content_hash 와 함께만 바뀌므로 키에 포함하지 않아도 안전하다.
    ``bin1`` 변형은 별도 캐시 키(dist_key(session, bin1=True))로 전체 기준과 분리 저장한다.
    콜드 빌드는 전체/bin1 모두 워커 오프로드 대상 — Honey 가 업로드 시 프리컴퓨트 blob
    을 첨부한 세션은 ingest 가 캐시를 미리 시딩해 여기 콜드 경로 자체가 없다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    cache_key = cache_policy.dist_key(session, bin1=bin1)
    blob = cache.cache_get(cache.DIST_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock(("dist",) + cache_key):
        blob = cache.cache_get(cache.DIST_CACHE, cache_key)
        if blob is not None:
            return blob
        # RAM 미스여도 디스크 캐시(재시작·LRU 퇴출 생존)가 있으면 재계산 생략
        blob = disk_cache.load_dist(upload_root, cache_key)
        if blob is None and compute.should_offload(cache_policy.tables_key(session)):
            # 콜드 빌드(수십 초 CPU 가능)는 전체/bin1 변형 모두 워커 프로세스로 —
            # 요청 스레드 GIL 점유를 피한다 (워커가 disk_cache 도 채움).
            blob = compute.run(compute.dist_job, session_id, str(upload_root), bin1)
        if blob is None:
            t0 = time.perf_counter()
            compact = get_distribution(session_id, report_db=report_db,
                                       upload_root=upload_root, bin1=bin1)
            raw = json.dumps(compact, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
            blob = gzip.compress(raw, compresslevel=_DIST_GZIP_LEVEL)
            disk_cache.save_dist(upload_root, cache_key, blob)
            # 관측 로그 — 실데이터가 위험 구간(수천만 포인트)에 닿는지 판단용 (docs 진단).
            _log.info(
                "dist cold build akey=%.12s bin1=%s items=%d points=%d raw=%.1fMB "
                "gz=%.1fMB %.1fs",
                str(analysis_key), bin1, len(compact.get("items") or {}),
                _dist_blob.count_points(compact), len(raw) / 1048576,
                len(blob) / 1048576, time.perf_counter() - t0)
        cache.dist_cache_put(cache_key, blob)   # 개수+바이트 이중 상한 (cache.py)
    return blob


def get_map_analysis(session_id: str, *, report_db, upload_root: Path) -> dict:
    """Map Analysis lazy 엔드포인트용 die 전량 rows (다운샘플 없음, 규칙 #6).

    /full 의 sheets["Map Analysis"] 는 dies 를 뺀 경량 메타만 싣는다(strip_dies,
    schema v8) — 여기서 같은 빌더로 dies 포함 rows 를 만들어 프런트가 지연 로드한다.
    selected_items 필터는 map 출력에 영향이 없으나 /full 콜드 빌드 경로
    (build_report_payload)와의 구조적 동일성을 위해 같은 순서로 적용한다.
    tables 는 캐시 클론이라 필터가 안전하다.
    """
    from .tabs.Map_analysis import build_map_analysis_rows

    session, tables, manifest = _load_tables(
        session_id, report_db=report_db, upload_root=upload_root)
    mode = _validate_mode(session.get("mode"))
    tables = _mode_tables(tables, mode)
    selected_set = {str(v) for v in (manifest.get("selected_items") or []) if str(v)}
    if selected_set:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected_set]
    rows = build_map_analysis_rows(
        tables, session.get("product_type", ""), session.get("product", ""), mode)
    return {"format": "map-dies-v1", "maps": rows}


def get_map_gzip(session_id: str, *, report_db, upload_root: Path) -> bytes:
    """get_map_analysis 결과를 JSON→gzip bytes 로 캐시해 반환 (라우트가 그대로 응답).

    get_distribution_gzip 과 1:1 대칭 — RAM(MAP_CACHE)→disk→single-flight→콜드 빌드.
    키는 dist 와 같은 (analysis_key, content_hash, mode) — dies 는 편집(rev)과 무관하고
    raw_data 편집만 content_hash 변경으로 무효화한다. 콜드 빌드는 워커 오프로드 대상
    (업로드 프리웜이 미리 채워 첫 조회 콜드가 없도록 한다).
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    cache_key = cache_policy.map_key(session)
    blob = cache.cache_get(cache.MAP_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock(("map",) + cache_key):
        blob = cache.cache_get(cache.MAP_CACHE, cache_key)
        if blob is not None:
            return blob
        # RAM 미스여도 디스크 캐시(재시작·LRU 퇴출 생존)가 있으면 재계산 생략
        blob = disk_cache.load_map(upload_root, cache_key)
        if blob is None and compute.should_offload(cache_policy.tables_key(session)):
            # 콜드 빌드(디코드 포함 수 초 CPU)는 워커 프로세스로 — GIL 비점유.
            blob = compute.run(compute.map_job, session_id, str(upload_root))
        if blob is None:
            t0 = time.perf_counter()
            payload = get_map_analysis(session_id, report_db=report_db,
                                       upload_root=upload_root)
            raw = json.dumps(payload, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
            blob = gzip.compress(raw, compresslevel=_DIST_GZIP_LEVEL)
            disk_cache.save_map(upload_root, cache_key, blob)
            # 관측 로그 — die 전량 payload 가 실데이터에서 얼마나 커지는지 진단용.
            _log.info(
                "map cold build akey=%.12s maps=%d dies=%d raw=%.1fMB gz=%.1fMB %.1fs",
                str(analysis_key), len(payload.get("maps") or ()),
                sum(len(m.get("dies") or ()) for m in payload.get("maps") or ()),
                len(raw) / 1048576, len(blob) / 1048576, time.perf_counter() - t0)
        cache.map_cache_put(cache_key, blob)   # 개수+바이트 이중 상한 (cache.py)
    return blob


def get_raw_data_columns(session_id: str, *, report_db, upload_root: Path) -> dict:
    """Raw Data 탭 컬럼 선택 UI용: item 메타 + source 목록 + 전체 die 수."""
    _, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return raw_data_tab.build_raw_data_columns(tables)


def query_raw_data(session_id: str, *, report_db, upload_root: Path, columns,
                   search="", bin_filter="", source_filter="") -> dict:
    """Raw Data 탭 조회: 선택된 columns + 필터로 행을 반환 (60개 컬럼 상한, ValueError 로 초과 통지)."""
    _, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return raw_data_tab.query_raw_data(
        tables, columns=columns, search=search, bin_filter=bin_filter, source_filter=source_filter)


def scatter_item(session_id: str, subject: str, *, report_db, upload_root: Path,
                 bin1: bool = False) -> dict:
    """Distribution 상세용: 항목의 소스별 전체 측정값(다운샘플 없음) + cpk/status 지연 로드.

    ``bin1`` 이면 분포/통계를 양품(BIN==PASS_BIN) die 만으로 낸다("Bin1 only" 상세).
    항목이 어떤 소스에도 없으면 KeyError (라우트가 404 처리).
    """
    from .tabs.distribution import scatter_item as _scatter_item

    session, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    tables = _mode_tables(tables, _validate_mode(session.get("mode")))
    return _scatter_item(tables, subject, bin1=bin1)


def _commonality_index(session: dict, tables):
    """Commonality 인덱스(메타 리스트 + item별 정렬 배열)를 세션 단위로 캐시해 반환.

    키는 tables 캐시와 동일한 (analysis_key, content_hash) — raw_data 편집 시 content_hash
    변경으로 자연 무효화되고, AKEY_CACHES 등록으로 세션 삭제 시에도 정리된다.
    콜드 미스(전 item 정렬, 수 초 CPU)는 single-flight 락으로 중복 계산을 막는다.
    """
    from .tabs.commonality import build_index

    cache_key = cache_policy.commonality_key(session)
    idx = cache.cache_get(cache.COMMONALITY_CACHE, cache_key)
    if idx is None:
        with cache.keyed_lock(("commonality",) + cache_key):
            idx = cache.cache_get(cache.COMMONALITY_CACHE, cache_key)
            if idx is None:
                idx = build_index(tables)
                cache.cache_put(cache.COMMONALITY_CACHE, cache_key, idx,
                                cache.COMMONALITY_CACHE_MAX)
    return idx


def commonality_chips(session_id: str, *, report_db, upload_root: Path,
                      q: str = "", limit: int = 300,
                      serial: str = "", xpos: str = "", ypos: str = "") -> dict:
    """Commonality chip 검색: serial/xpos/ypos 개별 칸(AND) 또는 q(OR, dut 포함) 부분일치 후보 목록."""
    from .tabs.commonality import search_chips

    session, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return search_chips(tables, q=q, limit=limit,
                        index=_commonality_index(session, tables),
                        serial=serial, xpos=xpos, ypos=ypos)


def commonality_chip(session_id: str, *, report_db, upload_root: Path,
                     serial: str = "", xpos: str = "", ypos: str = "", source: str = "") -> dict:
    """선택 chip 의 항목별 값 + 누적%(ECDF 위치) + wafer 좌표. 못 찾으면 KeyError."""
    from .tabs.commonality import chip_percentiles

    session, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return chip_percentiles(tables, serial=serial, xpos=xpos, ypos=ypos, source=source,
                            index=_commonality_index(session, tables))


def edit_raw_data(session_id: str, *, report_db, upload_root: Path, edits: list,
                  client_ip: str = "", user_agent: str = "") -> dict:
    """Raw Data 셀 편집을 저장된 parquet 원본에 그대로 반영한다.

    버전관리·undo 없음 — 편집된 source 는 df 기준으로 재인코딩해 기존 analysis_key 의
    web_report_source_<idx> 를 덮어쓴다 (Honey 재업로드 전까지 이전 값은 복구 불가).
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    # 같은 analysis_key 원본의 read-modify-write 직렬화 — 동시 편집 lost update 방지
    # (rawedit.replace_sources 와 같은 락 키. 단일 프로세스 전제라 in-process 락으로 충분 —
    # DB 기반 core.report_analysis_lock 은 멀티프로세스 전환 시에만 배선한다.)
    with cache.keyed_lock(("rawedit", analysis_key)):
        # apply_raw_data_edits 가 df 를 in-place 수정하므로 캐시 원본 오염 방지 위해 캐시 우회
        session, tables, manifest = _load_tables(
            session_id, report_db=report_db, upload_root=upload_root, use_cache=False,
            session=session)

        updated_tables = raw_data_tab.apply_raw_data_edits(tables, edits)
        sources_bytes = [encode_honeyform_parquet(t.df) for t in updated_tables]

        content_hash = hashlib.sha256(
            _canon({"files": [hashlib.sha256(b).hexdigest() for b in sources_bytes]})
        ).hexdigest()

        storage_result = runtime.storage().save_webreport_sources(
            analysis_key, content_hash, sources_bytes, manifest, upload_root=upload_root)

        report_db.update_session(session_id, content_hash=content_hash)
        # 구 content_hash 키 엔트리는 더 이상 조회되지 않으므로 메모리 회수용으로만 정리
        cache.evict_akey_caches(analysis_key)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"raw_data({len(edits)} cells)",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    return {"ok": True, "edited_cells": len(edits), "storage": storage_result["storage"]}


def update_issue_etc_items(session_id: str, *, report_db, upload_root: Path,
                           add: str = "", remove: str = "",
                           client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table ETC 섹션에 ENGR 가 임의로 추가/삭제한 item 이름을 세션 편집 DB
    (report_webreport_edit, kind=etc_item)에 반영한다. manifest 는 불변 스냅샷.

    Bin/TNO/Distribution 값 자체는 저장하지 않는다 — item 이름만 기억해두고, 조회할 때마다
    build_issue_table_rows 가 tables/yield_rows 에서 그때그때 다시 채운다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    add = str(add or "").strip()
    remove = str(remove or "").strip()
    if add and len(add) > 120:
        raise ValueError("item name too long (max 120 chars)")

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    etc_items = edits.load_edit_state(report_db, session_id)["etc_items"]
    changes = []
    if add and add not in etc_items:
        # 측정항목이 아닌 자유입력 Engr item(Item명 직접 타이핑)도 허용한다 — 이 경우
        # Bin/TNO/Distribution 은 매칭 데이터가 없어 조회 시 빈 칸으로 채워진다.
        changes.append((edits.KIND_ETC_ITEM, add, ""))
        etc_items.append(add)
    if remove and remove in etc_items:
        changes.append((edits.KIND_ETC_ITEM, remove, None))
        etc_items = [it for it in etc_items if it != remove]
    if changes:
        report_db.apply_webreport_edits(session_id, changes,
                                        updated_by=edits.user_from_ua(user_agent) or None)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"issue_table_etc_items(add={add!r},remove={remove!r})",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass
    if changes:
        # ETC 항목 추가/삭제 → eval DB 재적재 (삭제된 항목의 코멘트 case 는 stale 정리)
        try:
            from . import eval_export
            eval_export.export_async(session_id, report_db=report_db,
                                     upload_root=upload_root)
        except Exception:
            pass

    return {"ok": True, "etc_items": etc_items,
            "storage": "db" if changes else "unchanged"}


_COMMENT_MAX_ITEMS = 200
_COMMENT_MAX_LEN = 2000


def update_issue_comments(session_id: str, comments: list, *, report_db, upload_root: Path,
                          client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table 의 PTE/개발 comment 를 세션 편집 DB(kind=issue_comment)에 저장한다.
    manifest 는 불변 스냅샷.

    comments: [{"key": row_key, "col": comment 컬럼명, "value": str}, ...].
    row_key 는 tabs/issue_table.py 규칙("Yield|<bin>|<item>", "CPK|<item>", "ETC|<item>")을
    따르고, 빈 value 는 해당 항목 삭제로 처리한다.
    """
    from .tabs.issue_table import COMMENT_COLS

    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    if not isinstance(comments, list):
        raise ValueError("comments must be a list")
    if len(comments) > _COMMENT_MAX_ITEMS:
        raise ValueError(f"too many comment entries ({len(comments)} > {_COMMENT_MAX_ITEMS})")

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    saved = edits.load_edit_state(report_db, session_id)["issue_comments"]
    changes = []
    changed = 0
    for entry in comments:
        entry = entry or {}
        key = str(entry.get("key") or "").strip()
        col = str(entry.get("col") or "")
        value = str(entry.get("value") or "").strip()
        if not key or len(key) > 300:
            raise ValueError(f"invalid comment key: {key!r}")
        if col not in COMMENT_COLS:
            raise ValueError(f"unknown comment column: {col!r}")
        if len(value) > _COMMENT_MAX_LEN:
            raise ValueError(f"comment too long ({len(value)} > {_COMMENT_MAX_LEN} chars)")
        row = saved.get(key) or {}
        if str(row.get(col) or "") == value:
            continue
        changes.append((edits.KIND_ISSUE_COMMENT, edits.comment_key(key, col),
                        value if value else None))
        changed += 1
    if changed:
        report_db.apply_webreport_edits(session_id, changes,
                                        updated_by=edits.user_from_ua(user_agent) or None)
        try:
            report_db.log_audit(
                "edit", session_id=session_id, analysis_key=analysis_key,
                product_type=session.get("product_type", ""), product=session.get("product", ""),
                lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
                changed_fields=f"issue_comments({changed} cells)",
                client_ip=client_ip, user_agent=user_agent)
        except Exception:
            pass
        # 코멘트 변경 → eval_analyzer 스키마 DB 재적재 (백그라운드, 실패 무해 — docs/13)
        try:
            from . import eval_export
            eval_export.export_async(session_id, report_db=report_db,
                                     upload_root=upload_root)
        except Exception:
            pass
        storage = "db"
    else:
        storage = "unchanged"

    return {"ok": True, "updated": changed, "storage": storage}


_ENGR_KEYS = ("yield", "cpk", "etc")


def update_summary_engr(session_id: str, values: dict, *, report_db, upload_root: Path,
                        client_ip: str = "", user_agent: str = "") -> dict:
    """Summary 탭의 Engr Comment(Yield/CPK/ETC 3칸)를 세션 편집 DB(kind=summary_engr)에
    저장한다. manifest 는 불변 스냅샷.

    values: {"yield": str, "cpk": str, "etc": str} 중 온 키만 갱신하고, 빈 값은 삭제로
    처리한다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    if not isinstance(values, dict):
        raise ValueError("values must be an object")

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    saved = edits.load_edit_state(report_db, session_id)["summary_engr"]
    changes = []
    changed = 0
    for key in _ENGR_KEYS:
        if key not in values:
            continue
        val = str(values.get(key) or "").strip()
        if len(val) > _COMMENT_MAX_LEN:
            raise ValueError(f"comment too long ({len(val)} > {_COMMENT_MAX_LEN} chars)")
        if str(saved.get(key) or "") == val:
            continue
        changes.append((edits.KIND_SUMMARY_ENGR, key, val if val else None))
        if val:
            saved[key] = val
        else:
            saved.pop(key, None)
        changed += 1
    if changed:
        report_db.apply_webreport_edits(session_id, changes,
                                        updated_by=edits.user_from_ua(user_agent) or None)
        try:
            report_db.log_audit(
                "edit", session_id=session_id, analysis_key=analysis_key,
                product_type=session.get("product_type", ""), product=session.get("product", ""),
                lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
                changed_fields=f"summary_engr({changed} fields)",
                client_ip=client_ip, user_agent=user_agent)
        except Exception:
            pass
        storage = "db"
    else:
        storage = "unchanged"

    return {"ok": True, "updated": changed, "summary_engr": saved, "storage": storage}


# ── 차트 주석(chart_note) + Note 탭 시트(note_sheet) — 2026-07-12 ─────────────
# 둘 다 세션 편집 DB 가 진실. manifest 에 존재한 적 없는 신규 kind 라 legacy 시드
# (ensure_seeded)가 필요 없다. 저장은 rev 를 올려 REPORT/_FULL 캐시를 무효화하지만
# payload 재조립은 warm TABLES_CACHE 기반이라 comment 저장과 동일 비용이다.

_CHART_KEY_RE = re.compile(r"^(cdf|hist|trim|map|gap|overlay):.{1,200}$", re.DOTALL)
_CHART_NOTE_MAX_OPS = 100
_CHART_NOTE_MAX_SHAPES = 40
_CHART_NOTE_MAX_BYTES = 16 * 1024
_CHART_NOTE_TEXT_MAX = 300
_NOTE_SHEET_MAX_BYTES = 2 * 1024 * 1024

_SHAPE_TYPES = ("circle", "rect", "line", "path")
_SHAPE_KEYS = ("type", "x0", "x1", "y0", "y1", "path", "xref", "yref",
               "line", "fillcolor", "opacity")
_SHAPE_LINE_KEYS = ("color", "width", "dash")
_TEXT_KEYS = ("x", "y", "xref", "yref", "text", "showarrow", "arrowhead",
              "ax", "ay", "font", "bgcolor", "bordercolor")
_TEXT_FONT_KEYS = ("size", "color")


def _clean_scalar(v, maxlen=80):
    """shape/text 필드 값 정리 — 숫자/불리언은 그대로, 문자열은 길이 제한 + 태그 제거."""
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    return str(v)[:maxlen].replace("<", "").replace(">", "")


def _sanitize_chart_note(value: dict) -> dict:
    """저장 전 chart_note 값 정리 — 허용 키만 통과시키고 문자열을 바운드한다.

    Plotly layout.shapes/annotations 서브셋만 저장 (렌더 시 그대로 주입되므로
    text 의 <, > 는 제거해 HTML 해석 여지를 없앤다)."""
    shapes_in = value.get("shapes") or []
    texts_in = value.get("texts") or []
    if not isinstance(shapes_in, list) or not isinstance(texts_in, list):
        raise ValueError("shapes/texts must be lists")
    if len(shapes_in) > _CHART_NOTE_MAX_SHAPES or len(texts_in) > _CHART_NOTE_MAX_SHAPES:
        raise ValueError(f"too many shapes/texts (max {_CHART_NOTE_MAX_SHAPES})")
    shapes = []
    for s in shapes_in:
        if not isinstance(s, dict):
            raise ValueError("shape must be an object")
        if s.get("type") not in _SHAPE_TYPES:
            raise ValueError(f"unknown shape type: {s.get('type')!r}")
        out = {}
        for k in _SHAPE_KEYS:
            if k not in s:
                continue
            if k == "line":
                line = s.get("line") or {}
                if isinstance(line, dict):
                    out["line"] = {lk: _clean_scalar(line[lk])
                                   for lk in _SHAPE_LINE_KEYS if lk in line}
            elif k == "path":
                out[k] = _clean_scalar(s[k], maxlen=4000)
            else:
                out[k] = _clean_scalar(s[k])
        shapes.append(out)
    texts = []
    for t in texts_in:
        if not isinstance(t, dict):
            raise ValueError("text annotation must be an object")
        out = {}
        for k in _TEXT_KEYS:
            if k not in t:
                continue
            if k == "font":
                font = t.get("font") or {}
                if isinstance(font, dict):
                    out["font"] = {fk: _clean_scalar(font[fk])
                                   for fk in _TEXT_FONT_KEYS if fk in font}
            elif k == "text":
                out[k] = _clean_scalar(t[k], maxlen=_CHART_NOTE_TEXT_MAX)
            else:
                out[k] = _clean_scalar(t[k])
        if not str(out.get("text") or "").strip():
            continue
        texts.append(out)
    comment = str(value.get("comment") or "").strip()
    if len(comment) > _COMMENT_MAX_LEN:
        raise ValueError(f"comment too long ({len(comment)} > {_COMMENT_MAX_LEN} chars)")
    return {"shapes": shapes, "texts": texts, "comment": comment}


def update_chart_notes(session_id: str, ops: list, *, report_db, upload_root: Path,
                       client_ip: str = "", user_agent: str = "") -> dict:
    """차트 주석(도형/텍스트/코멘트) 저장 — 세션 편집 DB(kind=chart_note).

    ops: [{"key": chart_key, "value": {shapes,texts,comment} | null}] — null 은 삭제.
    chart_key 는 "cdf:<subject>" 형식 (프런트 chart_notes.js 규약과 일치)."""
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    if not isinstance(ops, list):
        raise ValueError("ops must be a list")
    if len(ops) > _CHART_NOTE_MAX_OPS:
        raise ValueError(f"too many note entries ({len(ops)} > {_CHART_NOTE_MAX_OPS})")

    changes = []
    for entry in ops:
        entry = entry or {}
        key = str(entry.get("key") or "")
        if not _CHART_KEY_RE.match(key):
            raise ValueError(f"invalid chart key: {key[:80]!r}")
        value = entry.get("value")
        if value is None:
            changes.append((edits.KIND_CHART_NOTE, key, None))
            continue
        if not isinstance(value, dict):
            raise ValueError("value must be an object or null")
        clean = _sanitize_chart_note(value)
        if not clean["shapes"] and not clean["texts"] and not clean["comment"]:
            changes.append((edits.KIND_CHART_NOTE, key, None))
            continue
        blob = json.dumps(clean, ensure_ascii=False, sort_keys=True)
        if len(blob.encode("utf-8")) > _CHART_NOTE_MAX_BYTES:
            raise ValueError(f"chart note too large (> {_CHART_NOTE_MAX_BYTES} bytes)")
        changes.append((edits.KIND_CHART_NOTE, key, blob))
    rev = report_db.apply_webreport_edits(session_id, changes,
                                          updated_by=edits.user_from_ua(user_agent) or None)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"chart_notes({len(changes)} charts)",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass
    return {"ok": True, "updated": len(changes), "rev": rev,
            "chart_notes": edits.load_chart_notes(report_db, session_id)}


def get_chart_notes(session_id: str, *, report_db) -> dict:
    """/full extras 조립용 — chart_key → {shapes,texts,comment,updated_by,updated_at}."""
    return edits.load_chart_notes(report_db, session_id)


def get_note_meta(session_id: str, *, report_db) -> dict:
    """/full extras 조립용 Note 존재 여부/최종 수정 메타 — 시트 본문(value)은 읽지 않는다."""
    for row in report_db.get_webreport_edit_meta(session_id, edits.KIND_NOTE_SHEET):
        if row.get("item_key") == "sheet":
            return {"exists": True, "updated_at": row.get("updated_at") or "",
                    "updated_by": row.get("updated_by") or ""}
    return {"exists": False}


def load_note(session_id: str, *, report_db) -> dict:
    """Note 탭 lazy GET — {"sheet": dict|None, "updated_at", "updated_by"}."""
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    return edits.load_note_sheet(report_db, session_id) or {"sheet": None}


def save_note(session_id: str, sheet, *, report_db, upload_root: Path,
              client_ip: str = "", user_agent: str = "") -> dict:
    """Note 탭 시트 JSON 저장 (전체 치환) — 세션 편집 DB(kind=note_sheet, item_key='sheet').

    sheet: Luckysheet 시트 상태 dict (셀 계산은 전부 클라이언트 — 서버는 저장만).
    null/빈 dict 는 삭제. 직렬화 크기 상한 2MB."""
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    if sheet:
        if not isinstance(sheet, dict):
            raise ValueError("sheet must be an object")
        blob = json.dumps(sheet, ensure_ascii=False, separators=(",", ":"))
        size = len(blob.encode("utf-8"))
        if size > _NOTE_SHEET_MAX_BYTES:
            raise ValueError(
                f"Note 시트가 너무 큽니다 ({size // 1024}KB > {_NOTE_SHEET_MAX_BYTES // 1024}KB). "
                "이미지가 아닌 셀 데이터를 줄여주세요.")
        changes = [(edits.KIND_NOTE_SHEET, "sheet", blob)]
        action = "save"
    else:
        changes = [(edits.KIND_NOTE_SHEET, "sheet", None)]
        action = "clear"
    rev = report_db.apply_webreport_edits(session_id, changes,
                                          updated_by=edits.user_from_ua(user_agent) or None)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"note_sheet({action})",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass
    return {"ok": True, "rev": rev}


# ── Trim Analysis (lazy — 탭 진입 시에만 계산, 세션 open 비용 없음) ────────────

_TRIM_SLOTS = ("INIT", "CODE", "TRIM", "VERIFY", "MEMBER")
_TRIM_OVERRIDE_MAX = 500
_TRIM_NAME_MAX = 200


def get_trim_analysis_gzip(session_id: str, *, report_db, upload_root: Path,
                           source: str = "") -> tuple[bytes, str]:
    """Trim Analysis 탭 payload(항목 매칭 + 그룹 통계/shift)를 JSON→gzip bytes 로 캐시해 반환.

    반환은 (gzip bytes, etag token) — token 은 라우트 ETag 용이다 (trim_overrides
    편집 직후 stale 304 방지). overrides 는 세션 편집 DB 가 진실이라 캐시 키·token 에
    (session_id, edits_rev)가 들어가 저장 시 자연 무효화된다. product_type 은
    analysis_key 산출 meta 에 이미 포함되어 키에 안 넣는다.
    """
    from .tabs.trim_analysis import build_trim_payload

    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    if not session.get("analysis_key"):
        raise FileNotFoundError(session_id)
    # 캐시 확인 전에 tables 를 로드하지 않는다 (Phase 6) — warm 요청은 rev SELECT +
    # bytes 반환으로 끝나고, 콜드 빌드는 워커로 보낼 수 있다.
    edits_rev = report_db.get_webreport_edit_rev(session_id)
    etag_token = hashlib.sha256(f"{session_id}:{edits_rev}".encode("utf-8")).hexdigest()
    mode = _validate_mode(session.get("mode"))

    cache_key = cache_policy.trim_key(session, session_id, edits_rev, source)
    blob = cache.cache_get(cache.TRIM_CACHE, cache_key)
    if blob is not None:
        return blob, etag_token
    with cache.keyed_lock(("trim",) + cache_key):
        blob = cache.cache_get(cache.TRIM_CACHE, cache_key)
        if blob is None and compute.should_offload(cache_policy.tables_key(session)):
            blob = compute.run(compute.trim_job, session_id, str(upload_root),
                               str(source or ""))
        if blob is None:
            session, tables, manifest = _load_tables(
                session_id, report_db=report_db, upload_root=upload_root, session=session)
            edit_state, _ = edits.effective_state(report_db, session_id, manifest)
            tables = _mode_tables(tables, mode)
            selected = {str(v) for v in (manifest.get("selected_items") or []) if str(v)}
            if selected:
                for table in tables:
                    table.item_columns = [c for c in table.item_columns if c in selected]
            payload = build_trim_payload(
                tables, str(source or ""), edit_state["trim_overrides"],
                session.get("product_type", ""))
            blob = gzip.compress(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                compresslevel=1)
        cache.cache_put(cache.TRIM_CACHE, cache_key, blob, cache.TRIM_CACHE_MAX)
    return blob, etag_token


def get_trim_chart_gzip(session_id: str, *, report_db, upload_root: Path,
                        source: str = "", group_id: str = "") -> bytes:
    """Trim 그룹 1개의 chip-to-chip 차트 payload 를 gzip bytes 로 캐시해 반환.

    그룹 재도출(build_groups)은 문자열 연산(ms 단위)이라 요청마다 수행하고, 캐시 키는
    슬롯 구성 digest — overrides 편집이 구성을 바꾸지 않은 그룹의 차트는 캐시가 살아있다.
    그룹/소스가 없으면 KeyError (라우트 404).
    """
    from .tabs.trim_analysis import _select_table, build_trim_chart
    from .trim_match import build_groups, rule_set_for

    session, tables, manifest = _load_tables(
        session_id, report_db=report_db, upload_root=upload_root)
    analysis_key = session.get("analysis_key")
    mode = _validate_mode(session.get("mode"))
    tables = _mode_tables(tables, mode)
    selected = {str(v) for v in (manifest.get("selected_items") or []) if str(v)}
    if selected:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected]

    table = _select_table(tables, str(source or ""))
    product_type = session.get("product_type", "")
    rule_set = rule_set_for(product_type)
    edit_state, _ = edits.effective_state(report_db, session_id, manifest)
    match = build_groups(table.item_columns,
                         overrides=edit_state["trim_overrides"],
                         rule_set=rule_set, product_type=product_type)
    group = next((g for g in match["groups"] if g["id"] == str(group_id)), None)
    if group is None:
        raise KeyError(str(group_id))

    items_digest = hashlib.sha256(_canon({"slots": group["slots"]})).hexdigest()[:16]
    cache_key = cache_policy.trim_chart_key(session, table.source, items_digest)
    blob = cache.cache_get(cache.TRIM_CHART_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock(("trim_chart",) + cache_key):
        blob = cache.cache_get(cache.TRIM_CHART_CACHE, cache_key)
        if blob is None:
            chart = build_trim_chart(table, group, rule_set)
            blob = gzip.compress(
                json.dumps(chart, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                compresslevel=1)
            cache.cache_put(cache.TRIM_CHART_CACHE, cache_key, blob,
                            cache.TRIM_CHART_CACHE_MAX)
    return blob


def update_trim_overrides(session_id: str, ops: list, *, report_db, upload_root: Path,
                          client_ip: str = "", user_agent: str = "") -> dict:
    """Trim Analysis 수동 재배치(드래그앤드랍)를 manifest.trim_overrides 에 병합 저장한다.

    ops: [{"item": 항목명, "group": 그룹 id, "slot": INIT|CODE|TRIM|VERIFY|MEMBER} |
          {"item": 항목명, "reset": true}]. reset 은 해당 override 삭제(자동 매칭 복귀).
    수정본은 자동 매칭 결과보다 우선 적용된다(적용 자체는 trim_match._apply_overrides).
    세션 편집 DB(kind=trim_override)에 저장하며 manifest 는 불변 스냅샷이다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    if not isinstance(ops, list):
        raise ValueError("ops must be a list")
    if len(ops) > _TRIM_OVERRIDE_MAX:
        raise ValueError(f"too many override entries ({len(ops)} > {_TRIM_OVERRIDE_MAX})")

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    saved = edits.load_edit_state(report_db, session_id)["trim_overrides"]
    changes = []
    changed = 0
    for entry in ops:
        entry = entry or {}
        item = str(entry.get("item") or "").strip()
        if not item or len(item) > _TRIM_NAME_MAX:
            raise ValueError(f"invalid item name: {item!r}")
        if entry.get("reset"):
            if saved.pop(item, None) is not None:
                changes.append((edits.KIND_TRIM_OVERRIDE, item, None))
                changed += 1
            continue
        slot = str(entry.get("slot") or "").strip().upper()
        group = str(entry.get("group") or "").strip().upper()
        if slot not in _TRIM_SLOTS:
            raise ValueError(f"unknown slot: {slot!r}")
        if not group or len(group) > _TRIM_NAME_MAX:
            raise ValueError(f"invalid group name: {group!r}")
        spec = {"group": group, "slot": slot}
        if saved.get(item) == spec:
            continue
        saved[item] = spec
        changes.append((edits.KIND_TRIM_OVERRIDE, item,
                        json.dumps(spec, sort_keys=True, ensure_ascii=False)))
        changed += 1
    if len(saved) > _TRIM_OVERRIDE_MAX:
        raise ValueError(f"too many overrides stored ({len(saved)} > {_TRIM_OVERRIDE_MAX})")

    if changed:
        report_db.apply_webreport_edits(session_id, changes,
                                        updated_by=edits.user_from_ua(user_agent) or None)
        try:
            report_db.log_audit(
                "edit", session_id=session_id, analysis_key=analysis_key,
                product_type=session.get("product_type", ""), product=session.get("product", ""),
                lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
                changed_fields=f"trim_overrides({changed} items)",
                client_ip=client_ip, user_agent=user_agent)
        except Exception:
            pass
        storage = "db"
    else:
        storage = "unchanged"

    return {"ok": True, "updated": changed, "overrides": saved, "storage": storage}

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

from . import build_log
from . import build_status
from . import cache
from . import cache_policy
from . import compute
from . import disk_cache
from . import dist_blob as _dist_blob
from . import dist_pack as _dist_pack
from . import dist_pack_store
from . import edits
from . import eta
from . import preprocess as _preprocess
from . import rawedit as _rawedit
from . import rawvalues
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
    webreport_compare_groups as _webreport_compare_groups,
    webreport_step as _webreport_step,
    webreport_temperature_groups as _webreport_temperature_groups,
    webreport_temperature_rt_names as _webreport_temperature_rt_names,
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


class ColdBuildRequired(Exception):
    """콜드 빌드가 필요한데 호출자가 대기하지 않기로 한 경우 (build_if_cold=False).

    라우트가 이 신호를 받아 202(building)로 즉시 응답하고, 실제 빌드는 백그라운드
    (compute.request_build)에서 돈다 — 요청 스레드가 수 초~수십 초 묶이지 않는다.
    """


def report_is_cold(session_id: str, *, report_db, upload_root: Path,
                   session: dict) -> bool:
    """report payload 가 콜드(RAM·디스크 둘 다 미스)인지 값싸게 판정한다.

    ``load_webreport(build_if_cold=False)`` 의 콜드 판정과 **같은 조건**이며 비용도
    같다 — edits_rev SELECT 1회 + RAM dict 조회 + stat 1회. 라우트가 202 를 내기 전에
    extras(DB 왕복 7~10건 + chart_index 다운로드 + canon/sha256)를 조립하지 않도록
    앞당겨 부르는 용도다. 콜드 세션 1건의 폴링이 15분간 수백 회 반복되므로 그 절감이
    크다.

    판정 후 응답 직전에 캐시가 축출되는 레이스는 여기서 막지 않는다 — 라우트의
    ``except ColdBuildRequired`` 폴백이 그대로 남아 있어 결과는 동일하다.
    """
    edits_rev = report_db.get_webreport_edit_rev(session_id)
    cache_key = cache_policy.report_key(session, session_id, edits_rev)
    if cache.cache_get(cache.REPORT_CACHE, cache_key) is not None:
        return False
    return not disk_cache.report_exists(upload_root, cache_key)


def load_webreport(session_id: str, *, report_db, upload_root: Path,
                   session: dict | None = None,
                   build_if_cold: bool = True) -> tuple[dict, dict]:
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
    if report is None and not build_if_cold:
        # 콜드 판정을 락 **밖에서** 먼저 한다 — 온디맨드 소비자가 빌드 내내 같은 키의
        # keyed_lock 을 잡고 있어, 락에 들어간 뒤 판정하면 202 로 즉시 돌려보내려던
        # 폴링 요청이 빌드가 끝날 때까지 waitress 스레드를 물고 대기했다(같은 세션을
        # 여러 명이 열면 스레드가 그만큼 묶임). stat 1회면 "아직 산출물 없음"을 알 수 있다.
        if not disk_cache.report_exists(upload_root, cache_key):
            raise ColdBuildRequired(session_id)
    if report is None:
        with cache.keyed_lock_ctx(("report",) + cache_key):
            report = cache.cache_get(cache.REPORT_CACHE, cache_key)
            if report is None:
                # RAM 미스여도 디스크 캐시(재시작·LRU 퇴출 생존)가 있으면 재계산 생략
                report = disk_cache.load_report(upload_root, cache_key)
                if report is None and not build_if_cold:
                    # 캐시 3계층(RAM→disk) 전부 미스 = 실제 콜드 빌드가 필요한 지점.
                    # 대기하지 않기로 한 호출자(라우트 202 경로)에게 즉시 알린다.
                    raise ColdBuildRequired(session_id)
                report_size = None   # 인라인 빌드에서 직렬화한 bytes 길이(크기추정 재사용)
                if report is None:
                    # 여기부터가 실제 콜드 빌드(워커 오프로드 포함) — 프런트 로드
                    # 오버레이가 build_status 폴링으로 "계산 중 + 경과초"를 사실대로
                    # 표시한다 (관측 전용 — 빌드 로직에는 영향 없음).
                    build_status.begin(session_id, "report")
                    try:
                        if compute.should_offload(cache_policy.tables_key(
                                session, _preprocess.session_digest(report_db, session_id))):
                            # 콜드 빌드(디코드 포함 수 초 CPU)는 워커 프로세스로 — GIL 비점유.
                            # 워커가 disk_cache 도 채우므로 여기서는 RAM 캐시만 넣는다.
                            t_sub = time.time()
                            report, child_t = compute.run(compute.report_job, session_id,
                                                          str(upload_root))
                            build_log.record_offloaded("report", session_id, analysis_key,
                                                       t_sub, time.time(), child_t)
                        if report is None:
                            t0 = time.perf_counter()
                            stages = build_log.start_stages()
                            session, tables, manifest = _load_tables(
                                session_id, report_db=report_db, upload_root=upload_root,
                                session=session)
                            edit_state, _ = edits.effective_state(report_db, session_id, manifest)
                            tables = _mode_tables(tables, mode)
                            # ai_comment 옵션 세션만 eval_analyzer 평가 실행 (콜드 빌드 1회 —
                            # rawdata 편집은 content_hash 변경으로 자동 재평가). 실패는
                            # safe_build 가 빈 dict 로 격리해 빌드가 죽지 않는다.
                            ai_comments = None
                            etc_auto_items = None
                            ai_signatures = None
                            signature_options = None
                            if _webreport_ai_comment(session.get("webreport_options") or ""):
                                from . import ai_comment
                                with build_log.stage("ai_comment"):
                                    ai_result = ai_comment.safe_build(
                                        tables, session,
                                        manifest.get("selected_items") or [])
                                ai_comments = ai_result["comments"]
                                # 수율·cpk 는 정상인데 룰만 위반한 item → ETC 자동 행
                                etc_auto_items = ai_result["etc_auto_items"]
                                # Signature 컬럼 — 엔진 발화 제안 + dropdown 선택지
                                ai_signatures = ai_result["row_signatures"]
                                signature_options = ai_result["signature_options"]
                            # 수율 분모: 기준정보 Gross Die 와 세션에 저장된 소스별 선택을
                            # 함께 넘기고, 실제 판정(자동 예외 포함)은 yield_tab 이 한다.
                            gross_die = session.get("gross_die")
                            yield_basis = edits.load_yield_basis_map(report_db, session_id)
                            t_payload = time.perf_counter()
                            report = build_report_payload(
                                tables,
                                selected_items=manifest.get("selected_items") or [],
                                sheets=manifest.get("sheets") or [],
                                etc_items=edit_state["etc_items"],
                                issue_comments=edit_state["issue_comments"],
                                summary_engr=edit_state["summary_engr"],
                                issue_hidden=edit_state["issue_hidden"],
                                issue_status=edit_state["issue_status"],
                                product_type=session.get("product_type", ""),
                                product=session.get("product", ""),
                                mode=mode,
                                dist_colors=dist_colors,
                                ai_comments=ai_comments,
                                etc_auto_items=etc_auto_items,
                                ai_signatures=ai_signatures,
                                signature_options=signature_options,
                                # ENGR 확정 signature — 편집 상태(세션 편집 DB)가 진실.
                                issue_signatures=edit_state["issue_signatures"],
                                gross_die=gross_die,
                                yield_basis=yield_basis,
                                # Compare 모드 Before/After 배치(업로드 시 Honey 가 지정).
                                # 없으면 compare 빌더가 legacy 폴백(after=0, before=1).
                                compare_groups=_webreport_compare_groups(
                                    session.get("webreport_options") or "",
                                    [t.source for t in tables]),
                                # Temperature 모드 RT/CT/HT 그룹(업로드 시 Honey 가 지정).
                                # 비RT 소스의 수율 분모를 남은 die 수로 강제한다.
                                temperature_groups=_webreport_temperature_groups(
                                    session.get("webreport_options") or "",
                                    [t.source for t in tables]),
                                # .lt/.pds 유래 항목별 fail bin (신규 업로드만 존재) —
                                # Temp 시트 Bin 표기용. 없으면 관측 bin 폴백.
                                temperature_limits=manifest.get("temperature_limits"),
                                # 업로드 창에서 고른 공정 STEP — honeyform 이 P2 로 실어 온
                                # STEP 표시만 이 값으로 바꾼다(원본 parquet 불변). 빈 값이면
                                # 종전 그대로. 캐시는 report_key 의 webreport_options 가 덮는다.
                                step_label=_webreport_step(
                                    session.get("webreport_options") or ""),
                            )
                            # payload(= 탭별 stage 합 + 조립 오버헤드) 총계.
                            stages["payload"] = round(time.perf_counter() - t_payload, 3)
                            # payload 를 한 번만 직렬화해 gzip 디스크 저장과 아래 RAM
                            # 캐시 크기추정에 함께 재사용한다(콜드 1회 3중 직렬화 제거).
                            with build_log.stage("serialize"):
                                report_bytes = disk_cache.dumps_report(report)
                                disk_cache.save_report_gz(upload_root, cache_key, report_bytes)
                            report_size = len(report_bytes)
                            # Temperature: 방금 판정한 결과(tables 에 캐시됨)로 temp_map 을
                            # 함께 채운다 — 안 하면 Issue Table 첫 진입에서 요청 스레드가
                            # 전 항목 판정을 다시 돈다.
                            with build_log.stage("temp_map_seed"):
                                seed_temp_map(session_id, session, tables,
                                              report_db=report_db, upload_root=upload_root)
                            # Map dies 도 같은 tables 로 함께 채운다 — 안 하면 Map 탭 /
                            # Issue Table Map 컬럼 첫 진입이 콜드 202 + 전체 재디코드
                            # (CLAUDE.md §5-11 Map 3초 SLA).
                            with build_log.stage("map_seed"):
                                seed_map(session_id, session, tables,
                                         report_db=report_db, upload_root=upload_root)
                            # 관측 로그 — 콜드 빌드(디코드 포함)가 실데이터에서 얼마나 걸리는지.
                            _log.info(
                                "report cold build akey=%.12s sid=%s sources=%d items=%d %.1fs",
                                str(analysis_key), session_id, len(tables),
                                len(report.get("distribution_index") or ()),
                                time.perf_counter() - t0)
                            # 단계별 기록 — 워커 안이면 부모로 실려가고(compute.report_job),
                            # 부모 인라인이면 여기서 바로 파일에 남는다.
                            # 입력 규모(셀·항목 수) — eta.calibration_factor 가 이 기록과
                            # 실측 시간을 비교해 예상시간 배율을 학습한다(web_report/eta.py).
                            mcells, kcols = eta.shape_from_tables(tables)
                            build_log.finish({
                                "kind": "report", "session": session_id,
                                "akey": str(analysis_key)[:12], "offloaded": False,
                                "result": "ok", "total": round(time.perf_counter() - t0, 3),
                                "stages": stages, "sources": len(tables),
                                "items": len(report.get("distribution_index") or ()),
                                "mcells": mcells, "kcols": kcols})
                    finally:
                        build_log.clear_stages()
                        build_status.end(session_id, "report")
                # size: 인라인 빌드면 위에서 잰 bytes 길이 재사용, 그 외(disk hit·워커
                # 오프로드)는 None → report_cache_put 이 자체 추정(현행 유지).
                cache.report_cache_put(cache_key, report, size=report_size)   # 이중 상한 (cache.py)
    public = dict(session)
    public["has_password"] = bool(public.get("password"))
    public.pop("password", None)
    return public, report


def pack_available(session, session_id, *, report_db, upload_root: Path) -> bool:
    """이 세션이 pack 으로 서빙되는가 (해당 세대 index 존재). 작은 파일 읽기 1회.

    콜드 빌드 워커 오프로드 여부를 정할 때 쓴다 — pack 이 있으면 계산이 덧셈뿐이라
    워커 프로세스를 띄우는 비용이 더 크다. 전처리 세션은 그 spec 의 variant 를 본다.
    """
    return dist_pack_store.load_index(
        upload_root, session.get("analysis_key"), session.get("content_hash"),
        _validate_mode(session.get("mode")),
        prep_digest=_preprocess.session_digest(report_db, session_id)) is not None


def _pack_items(session, session_id, *, report_db, upload_root: Path, subjects=None):
    """Honey 가 업로드에 첨부한 Distribution pack 에서 항목 데이터를 읽는다 (없으면 None).

    pack 은 정렬(np.unique)까지 끝난 영구 파생 데이터라, 서버는 chunk 를 읽어 덧셈만 하면
    된다 — tables 디코드도 하지 않는다. ``subjects`` 를 주면 그 항목이 든 chunk 만 읽는다.

    **전처리(preprocess) 세션은 전용 variant 를 쓴다** — Honey 가 올린 pack 은 업로드
    시점(전처리 없음) 기준이라 항목 제외·outlier 마스킹이 반영돼 있지 않다. 그 spec 의
    variant 가 아직 없으면 백그라운드 생성을 예약하고 이번 조회만 기존 계산으로 폴백한다
    (전처리는 되돌릴 수 있는 옵션이라 해제하면 digest 가 비어 원본 pack 으로 돌아온다).
    """
    analysis_key = session.get("analysis_key")
    content_hash = session.get("content_hash")
    mode = _validate_mode(session.get("mode"))
    prep = _preprocess.session_digest(report_db, session_id)
    index = dist_pack_store.load_index(upload_root, analysis_key, content_hash, mode,
                                       prep_digest=prep)
    if not index:
        if prep:
            # 전처리 variant 미생성 — 1회 생성 예약(중복 요청은 큐가 무시).
            compute.request_dist_pack(session_id, str(upload_root))
        return None

    chunk_ids = None
    if subjects is not None:
        chunk_of = _dist_pack.item_chunk_map(index)
        wanted = {str(s) for s in subjects if str(s)}
        chunk_ids = sorted({chunk_of[s] for s in wanted if s in chunk_of})
        if not chunk_ids:
            # 요청 항목이 pack 에 하나도 없다 — pack 세대가 어긋났을 수 있으니 폴백한다.
            return None
    else:
        chunk_ids = [int(e["id"]) for e in index.get("chunks") or ()]

    items = {}
    for chunk_id in chunk_ids:
        part = dist_pack_store.load_chunk_items(
            upload_root, analysis_key, content_hash, mode, chunk_id, prep_digest=prep)
        if part is None:
            return None            # 일부 chunk 손상/누락 — 부분 응답 대신 폴백
        items.update(part)
    return items


def materialize_dist_pack(session_id: str, *, report_db, upload_root: Path,
                          base: bool = False) -> str:
    """Distribution pack 을 서버가 1회 만들어 영구 저장한다 (백그라운드 잡 전용).

    Honey 가 pack 을 붙일 수 없는 두 경우를 메운다:
    - ``base=False``: 조회 전처리를 켠 세션의 variant (spec 적용 tables 기준)
    - ``base=True``: 웹 셀 편집으로 content_hash 가 바뀐 뒤의 원본 pack

    값이 폴백 계산과 어긋나지 않는 근거는 **같은 입력 tables 를 쓴다**는 것이다 —
    폴백(dist_blob.compute_dist_compact)도 여기도 load_tables(apply_prep) 결과를 받고,
    두 빌더의 정준 JSON 일치는 dist_pack.py 의 계약이다.

    반환값은 관측·테스트용 문자열: saved/exists/empty/stale/failed/no-session.
    어느 값이든 실패는 무해하다 — pack 이 없으면 조회가 기존 계산으로 폴백한다.
    """
    session = report_db.get_session(session_id)
    if not session:
        return "no-session"
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        return "no-session"
    mode = _validate_mode(session.get("mode"))
    content_hash = session.get("content_hash")
    prep = "" if base else _preprocess.session_digest(report_db, session_id)
    if dist_pack_store.load_index(upload_root, analysis_key, content_hash, mode,
                                  prep_digest=prep) is not None:
        return "exists"

    with cache.keyed_lock_ctx(("dist_pack_build", analysis_key, content_hash, mode, prep)):
        if dist_pack_store.load_index(upload_root, analysis_key, content_hash, mode,
                                      prep_digest=prep) is not None:
            return "exists"
        _, tables, manifest = _load_tables(session_id, report_db=report_db,
                                           upload_root=upload_root, apply_prep=not base)
        index, chunk_iter = _dist_pack.build_dist_pack(
            tables, manifest.get("selected_items") or [], mode)
        # chunk 를 만드는 즉시 gzip — 전체 항목 payload 를 한 번에 들고 있지 않는다.
        # level 은 클라(1)보다 높다: 서버는 1회 생성 후 영구 보관이라 압축률이 이득.
        chunks = {cid: _dist_pack.gzip_pack_chunk(chunk) for cid, chunk in chunk_iter}
        if not chunks:
            return "empty"
        # 저장 직전 세대 재확인 — 빌드 중 raw 편집이 끼어들었으면 이 pack 은 옛 원본
        # 기준이라 버린다(다음 조회가 새 세대로 다시 예약한다).
        fresh = report_db.get_session(session_id)
        if not fresh or (fresh.get("content_hash") or "") != (content_hash or ""):
            return "stale"
        saved = dist_pack_store.save(upload_root, analysis_key, content_hash, mode,
                                     _dist_pack.dumps_pack_index(index), chunks,
                                     prep_digest=prep)
    return "saved" if saved else "failed"


def _bin1_source_filter(session, bin1_scope: str):
    """bin1 을 **일부 소스에만** 걸 때의 소스 이름 집합 (아니면 None = 전 소스 동일).

    현재 유일한 scope 는 ``"rt"`` — Temperature 모드에서 RT source 만 양품(Bin1)·규격내로
    좁히고 CT/HT 는 fail 포함 전체를 유지한다("Bin1(RT만)" 버튼). Temperature 가 아니거나
    RT 가 없으면 None 을 돌려 평범한 bin1 로 폴백한다(캐시 키도 scope 없이 간다).
    """
    if str(bin1_scope or "") != "rt" or (session or {}).get("mode") != "Temperature":
        return None
    names = _webreport_temperature_rt_names(session.get("webreport_options") or "")
    return names or None


def get_distribution(session_id: str, *, report_db, upload_root: Path, bin1: bool = False,
                     bin1_scope: str = "") -> dict:
    """Distribution lazy 엔드포인트용 컴팩트 ECDF (전 포인트, 다운샘플 없음).

    계산 본체는 dist_blob.compute_dist_compact — Honey 클라의 업로드 시 프리컴퓨트와
    같은 코드를 공유해 값 일치를 구조적으로 보장한다. selected_items 필터를 빠뜨리면
    distribution_index 와 항목 집합이 어긋난다. tables 는 캐시 클론이라 필터가 안전하다.
    ``bin1`` 이면 양품(BIN==PASS_BIN) die 측정값만으로 ECDF 를 재계산한다("Bin1 only").

    Honey 가 pack 을 첨부한 세션은 정렬을 다시 하지 않고 pack 에서 덧셈만으로 만든다
    (값은 아래 폴백 계산과 정준 JSON 으로 동일 — dist_pack.py 참조).
    """
    session = report_db.get_session(session_id)
    if session:
        srcs = _bin1_source_filter(session, bin1_scope)
        items = _pack_items(session, session_id, report_db=report_db, upload_root=upload_root)
        if items is not None:
            return _dist_pack.ecdf_from_pack_items(items, bin1=bin1, bin1_sources=srcs)
    session, tables, manifest = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return _dist_blob.compute_dist_compact(
        tables, manifest.get("selected_items") or [], session.get("mode"), bin1=bin1,
        bin1_sources=_bin1_source_filter(session, bin1_scope))


def get_distribution_batch(session_id: str, subjects, *, report_db, upload_root: Path,
                           bin1: bool = False, bin1_scope: str = "") -> dict:
    """항목 배치 ECDF — 요청한 subject 만 계산한 compact dict (다운샘플 없음).

    전체 dist(get_distribution)는 대형 세션에서 수천만 포인트라 프런트가 한 번에 받으면
    다운로드·파싱·힙 상주가 모두 폭증한다. 갤러리/미니셀은 화면에 보이는 항목만 필요하므로
    ``subjects`` 로 좁혀 계산한다. 계산 경로는 전체와 동일한 compute_dist_compact(only=)라
    결과는 전체 payload 에서 그 항목만 뽑은 것과 정준 JSON 으로 일치한다.

    pack 세션은 요청 항목이 든 chunk 만 읽어 덧셈으로 만든다 — **tables 디코드조차 하지
    않는다**(스크롤할 때마다 반복되던 서버 재정렬이 사라지는 지점).
    """
    session = report_db.get_session(session_id)
    if session:
        srcs = _bin1_source_filter(session, bin1_scope)
        items = _pack_items(session, session_id, report_db=report_db,
                            upload_root=upload_root, subjects=subjects)
        if items is not None:
            return _dist_pack.ecdf_from_pack_items(items, bin1=bin1, only=subjects,
                                                   bin1_sources=srcs)
    session, tables, manifest = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return _dist_blob.compute_dist_compact(
        tables, manifest.get("selected_items") or [], session.get("mode"),
        bin1=bin1, only=subjects, bin1_sources=_bin1_source_filter(session, bin1_scope))


def get_distribution_gzip(session_id: str, *, report_db, upload_root: Path,
                          bin1: bool = False, bin1_scope: str = "") -> bytes:
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

    prep = _preprocess.session_digest(report_db, session_id)
    # scope 가 실제로 적용되는 세션(Temperature + RT 존재)일 때만 캐시 키에 넣는다 —
    # 그 외에는 종전 키와 완전히 같아 기존 캐시가 그대로 유효하다.
    scope = "rt" if _bin1_source_filter(session, bin1_scope) else ""
    cache_key = cache_policy.dist_key(session, bin1=bin1, prep_digest=prep, bin1_scope=scope)
    blob = cache.cache_get(cache.DIST_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock_ctx(("dist",) + cache_key):
        blob = cache.cache_get(cache.DIST_CACHE, cache_key)
        if blob is not None:
            return blob
        # RAM 미스여도 디스크 캐시(재시작·LRU 퇴출 생존)가 있으면 재계산 생략
        blob = disk_cache.load_dist(upload_root, cache_key)
        # pack 세션은 계산이 chunk 읽기+덧셈뿐이라 워커 프로세스를 띄우는 편이 손해다.
        has_pack = pack_available(session, session_id, report_db=report_db,
                                  upload_root=upload_root)
        if blob is None and not has_pack and prep:
            # 전처리 variant 미생성 — 이번 조회는 폴백으로 계산하고, 다음부터 덧셈만 하도록
            # 생성을 예약한다(워커 오프로드 경로는 _pack_items 를 부모에서 타지 않는다).
            compute.request_dist_pack(session_id, str(upload_root))
        if blob is None and not has_pack \
                and compute.should_offload(cache_policy.tables_key(session, prep)):
            # 콜드 빌드(수십 초 CPU 가능)는 전체/bin1 변형 모두 워커 프로세스로 —
            # 요청 스레드 GIL 점유를 피한다 (워커가 disk_cache 도 채움).
            t_sub = time.time()
            blob, child_t = compute.run(compute.dist_job, session_id, str(upload_root),
                                        bin1, scope)
            build_log.record_offloaded("dist", session_id, analysis_key,
                                       t_sub, time.time(), child_t)
        if blob is None:
            t0 = time.perf_counter()
            stages = build_log.start_stages()
            try:
                with build_log.stage("compute"):
                    compact = get_distribution(session_id, report_db=report_db,
                                               upload_root=upload_root, bin1=bin1,
                                               bin1_scope=scope)
                with build_log.stage("serialize"):
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
                build_log.finish({
                    "kind": "dist", "session": session_id,
                    "akey": str(analysis_key)[:12], "offloaded": False, "result": "ok",
                    "total": round(time.perf_counter() - t0, 3), "stages": stages,
                    "items": len(compact.get("items") or {})})
            finally:
                build_log.clear_stages()
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


def get_temp_map(session_id: str, *, report_db, upload_root: Path) -> dict:
    """Temperature 항목별 fail die **인덱스** (Map 항목 legend + Issue Table Temp Map 셀).

    반환 ``{"format":"temp-map-v1", "sources":[{source, n, items:[{item, idx:[...]}]}]}``.
    좌표가 아니라 ``get_map_analysis`` 의 ``maps[].dies`` 배열 인덱스를 보낸다 — 같은
    die 를 항목마다 반복해 싣지 않으므로 payload 가 훨씬 작다(다운샘플 아님, 규칙 #6).
    인덱스 기준은 tabs/temp_fail.temp_fail_indices 참조(Map_analysis 의 좌표 mask 와 동일).
    Temperature 가 아니거나 그룹이 없으면 빈 목록 — 프런트가 항목 축을 만들지 않는다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    if _validate_mode(session.get("mode")) != "Temperature":
        return _EMPTY_TEMP_MAP
    session, tables, _manifest = _load_tables(
        session_id, report_db=report_db, upload_root=upload_root, session=session)
    return temp_map_payload(session, tables)


_EMPTY_TEMP_MAP = {"format": "temp-map-v1", "sources": []}

# Issue Table 계열 row_key 접두 (tabs/issue_table.py + tabs/temp_fail.py 규약).
# hidden/status/comment 검증이 공유한다 — 새 섹션을 만들면 여기에 추가한다.
_ISSUE_KEY_PREFIXES = ("Yield|", "CPK|", "TEMP|", "ETC|")


def temp_map_payload(session, tables) -> dict:
    """이미 로드된 tables 로 temp_map payload 를 만든다 (콜드 빌드 시딩 공용).

    판정은 ``compute_temp_fail`` 이 tables 클론에 캐시하므로, 같은 요청에서 report 페이로드를
    만든 직후 호출하면 **재계산이 없다**.
    """
    from .tabs.temp_fail import temp_fail_indices

    groups = _webreport_temperature_groups(
        session.get("webreport_options") or "", [t.source for t in tables])
    if not groups:
        return _EMPTY_TEMP_MAP
    return {"format": "temp-map-v1",
            "sources": temp_fail_indices(tables, groups.get("groups"))}


def _temp_map_blob(payload: dict) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, compresslevel=_DIST_GZIP_LEVEL)


def seed_temp_map(session_id: str, session, tables, *, report_db, upload_root: Path) -> None:
    """report 콜드 빌드 직후 temp_map 을 같은 판정 결과로 미리 채운다 (RAM + 디스크).

    이게 없으면 Temperature 세션은 Issue Table 탭에 들어가는 순간 temp_map 라우트가
    **요청 스레드에서** 전 항목 판정을 다시 돌게 된다(21 source 에서 치명적). 실패는
    조용히 넘긴다 — 시딩은 최적화일 뿐이고 라우트가 폴백 계산을 갖고 있다.
    """
    if _validate_mode(session.get("mode")) != "Temperature":
        return
    try:
        prep = _preprocess.session_digest(report_db, session_id)
        cache_key = cache_policy.temp_map_key(session, prep)
        if cache.cache_get(cache.TEMP_MAP_CACHE, cache_key) is not None:
            return
        blob = _temp_map_blob(temp_map_payload(session, tables))
        disk_cache.save_temp_map(upload_root, cache_key, blob)
        cache._bytes_capped_put(cache.TEMP_MAP_CACHE, cache_key, blob,
                                cache.TEMP_MAP_CACHE_MAX, cache.TEMP_MAP_CACHE_MAX_BYTES)
    except Exception:
        _log.warning("temp_map seeding failed for session %s", session_id, exc_info=True)


def seed_map(session_id: str, session, tables, *, report_db, upload_root: Path) -> None:
    """report 콜드 빌드 직후 Map dies gzip 을 같은 tables 로 미리 채운다 (RAM + 디스크).

    CLAUDE.md §5-11 (Map 3초 SLA) 의 달성 수단이다. 이게 없으면 Map Analysis 탭 / Issue
    Table Map 컬럼 첫 진입이 콜드 202 + 전체 재디코드 빌드(30초+ "맵 로드 중…")가 된다 —
    map dies 는 프리웜 대상이 아니라 종전에는 첫 진입이 사실상 항상 콜드였다.
    여기서는 tables 가 이미 웜이라 한계비용이 die dict 생성 + dumps + gzip 뿐이다.

    호출 시점 tables 는 ``_mode_tables`` + ``build_report_payload`` 의 selected_items
    필터가 적용된 상태 = ``get_map_analysis`` 와 같은 준비 순서라 산출 rows 가 동일하다
    (그쪽을 고치면 여기도 같이 고칠 것 — 정준 JSON 일치가 계약).
    실패는 조용히 넘긴다 — 시딩은 최적화일 뿐이고 라우트가 폴백 계산을 갖고 있다.
    """
    from .tabs.Map_analysis import build_map_analysis_rows

    try:
        prep = _preprocess.session_digest(report_db, session_id)
        cache_key = cache_policy.map_key(session, prep)
        # comment/override 편집 리빌드는 edits_rev 만 바뀌어 map 키가 그대로다 — 이미
        # 있는 dies 를 다시 만들지 않는다(프로세스 무관 판정을 위해 디스크까지 확인).
        if (cache.cache_get(cache.MAP_CACHE, cache_key) is not None
                or disk_cache.map_exists(upload_root, cache_key)):
            return
        t0 = time.perf_counter()
        rows = build_map_analysis_rows(
            tables, session.get("product_type", ""), session.get("product", ""),
            _validate_mode(session.get("mode")))
        raw = json.dumps({"format": "map-dies-v1", "maps": rows},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        blob = gzip.compress(raw, compresslevel=_DIST_GZIP_LEVEL)
        disk_cache.save_map(upload_root, cache_key, blob)
        cache.map_cache_put(cache_key, blob)
        _log.info("map seeded akey=%.12s maps=%d dies=%d raw=%.1fMB gz=%.1fMB %.1fs",
                  str(session.get("analysis_key")), len(rows),
                  sum(len(m.get("dies") or ()) for m in rows),
                  len(raw) / 1048576, len(blob) / 1048576, time.perf_counter() - t0)
    except Exception:
        _log.warning("map seeding failed for session %s", session_id, exc_info=True)


def schedule_map_backfill(session_id: str, session, *, report_db, upload_root: Path) -> None:
    """시딩 도입 전 세션(report 캐시는 있는데 map 캐시가 없는 세션)만 백그라운드 빌드 예약.

    /full 200 직후에 부른다. 빌드를 **기다리지 않는다** — 사용자가 몇 초 뒤 Map/Issue
    Table 탭을 클릭할 때쯤 준비돼 있게 하는 것이 목적이라, 어차피 유발될 빌드를 앞당길
    뿐이다(신규 202 도 부분 계산도 아니다). ``seed_map`` 이 채운 신규 세션은 stat 1회로
    no-op. 폭주 방어는 ``compute.request_build`` 의 pending 중복 제거 + 연속 실패 차단.
    """
    try:
        prep = _preprocess.session_digest(report_db, session_id)
        cache_key = cache_policy.map_key(session, prep)
        if (cache.cache_get(cache.MAP_CACHE, cache_key) is not None
                or disk_cache.map_exists(upload_root, cache_key)):
            return
        compute.request_build(session_id, str(upload_root), "map")
    except Exception:
        _log.warning("map backfill scheduling failed for session %s", session_id, exc_info=True)


def get_temp_map_gzip(session_id: str, *, report_db, upload_root: Path) -> bytes:
    """get_temp_map 결과를 JSON→gzip bytes 로 캐시해 반환 (get_map_gzip 축소판).

    RAM(TEMP_MAP_CACHE) → 디스크 → single-flight → 계산. 보통은 report 콜드 빌드가
    ``seed_temp_map`` 으로 이미 채워 두므로 여기 계산 경로는 옛 캐시 세션에서만 돈다.
    그 드문 콜드는 워커 오프로드 대상이다(요청 스레드 GIL 점유 회피 — dist/map 과 동일).
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    if not session.get("analysis_key"):
        raise FileNotFoundError(session_id)
    prep = _preprocess.session_digest(report_db, session_id)
    cache_key = cache_policy.temp_map_key(session, prep)
    blob = cache.cache_get(cache.TEMP_MAP_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock_ctx(("temp_map",) + cache_key):
        blob = cache.cache_get(cache.TEMP_MAP_CACHE, cache_key)
        if blob is not None:
            return blob
        blob = disk_cache.load_temp_map(upload_root, cache_key)
        if blob is None and compute.should_offload(cache_policy.tables_key(session, prep)):
            blob, _child_t = compute.run(compute.temp_map_job, session_id, str(upload_root))
        if blob is None:
            blob = _temp_map_blob(
                get_temp_map(session_id, report_db=report_db, upload_root=upload_root))
            disk_cache.save_temp_map(upload_root, cache_key, blob)
        cache._bytes_capped_put(cache.TEMP_MAP_CACHE, cache_key, blob,
                                cache.TEMP_MAP_CACHE_MAX, cache.TEMP_MAP_CACHE_MAX_BYTES)
    return blob


def get_map_gzip(session_id: str, *, report_db, upload_root: Path,
                 build_if_cold: bool = True) -> bytes:
    """get_map_analysis 결과를 JSON→gzip bytes 로 캐시해 반환 (라우트가 그대로 응답).

    get_distribution_gzip 과 1:1 대칭 — RAM(MAP_CACHE)→disk→single-flight→콜드 빌드.
    키는 dist 와 같은 (analysis_key, content_hash, mode) — dies 는 편집(rev)과 무관하고
    raw_data 편집만 content_hash 변경으로 무효화한다. 콜드 빌드는 워커 오프로드 대상
    (업로드 프리웜이 미리 채워 첫 조회 콜드가 없도록 한다).

    ``build_if_cold=False`` 면 /full 과 같은 규약으로 ColdBuildRequired 를 올린다 —
    라우트가 202 를 반환하고 빌드는 백그라운드(compute.request_build)가 맡는다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    prep = _preprocess.session_digest(report_db, session_id)
    cache_key = cache_policy.map_key(session, prep)
    blob = cache.cache_get(cache.MAP_CACHE, cache_key)
    if blob is not None:
        return blob
    if not build_if_cold and not disk_cache.map_exists(upload_root, cache_key):
        # load_webreport 와 같은 이유 — 빌드 중인 키의 락을 기다리지 않고 즉시 202.
        raise ColdBuildRequired(session_id)
    with cache.keyed_lock_ctx(("map",) + cache_key):
        blob = cache.cache_get(cache.MAP_CACHE, cache_key)
        if blob is not None:
            return blob
        # RAM 미스여도 디스크 캐시(재시작·LRU 퇴출 생존)가 있으면 재계산 생략
        blob = disk_cache.load_map(upload_root, cache_key)
        if blob is None and not build_if_cold:
            raise ColdBuildRequired(session_id)
        cold = blob is None
        if cold:
            # 콜드 구간만 기록 — 프런트 맵 오버레이가 build_status 폴링으로 진행을 본다
            # (report 와 같은 레지스트리를 stage 로 구분해 쓴다).
            build_status.begin(session_id, "map")
        try:
            if blob is None and compute.should_offload(cache_policy.tables_key(session, prep)):
                # 콜드 빌드(디코드 포함 수 초 CPU)는 워커 프로세스로 — GIL 비점유.
                t_sub = time.time()
                blob, child_t = compute.run(compute.map_job, session_id, str(upload_root))
                build_log.record_offloaded("map", session_id, analysis_key,
                                           t_sub, time.time(), child_t)
            if blob is None:
                t0 = time.perf_counter()
                stages = build_log.start_stages()
                with build_log.stage("map_rows"):
                    payload = get_map_analysis(session_id, report_db=report_db,
                                               upload_root=upload_root)
                with build_log.stage("serialize"):
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
                build_log.finish({
                    "kind": "map", "session": session_id,
                    "akey": str(analysis_key)[:12], "offloaded": False, "result": "ok",
                    "total": round(time.perf_counter() - t0, 3), "stages": stages,
                    "items": len(payload.get("maps") or ())})
        finally:
            build_log.clear_stages()
            if cold:
                build_status.end(session_id, "map")
        cache.map_cache_put(cache_key, blob)   # 개수+바이트 이중 상한 (cache.py)
    return blob


def get_raw_data_columns(session_id: str, *, report_db, upload_root: Path) -> dict:
    """Raw Data 탭 컬럼 선택 UI용: item 메타 + source 목록 + 전체 die 수.

    apply_prep=False — 전처리로 제외한 항목도 목록에 남아야 Item Select 에서 되돌릴 수 있다.
    """
    _, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root,
                                apply_prep=False)
    return raw_data_tab.build_raw_data_columns(tables)


def query_raw_data(session_id: str, *, report_db, upload_root: Path, columns,
                   search="", bin_filter="", source_filter="") -> dict:
    """Raw Data 탭 조회: 선택된 columns + 필터로 행을 반환 (60개 컬럼 상한, ValueError 로 초과 통지).

    apply_prep=False — Raw Data 탭은 저장된 원본을 보여주는 자리다 (전처리는 표시용 필터라
    여기까지 적용하면 편집 대상 값과 화면 값이 어긋난다)."""
    _, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root,
                                apply_prep=False)
    return raw_data_tab.query_raw_data(
        tables, columns=columns, search=search, bin_filter=bin_filter, source_filter=source_filter)


def scatter_item(session_id: str, subject: str, *, report_db, upload_root: Path,
                 bin1: bool = False, bin1_scope: str = "",
                 session=None) -> dict:
    """Distribution 상세용: 항목의 소스별 전체 측정값(다운샘플 없음) + cpk/status 지연 로드.

    ``bin1`` 이면 분포/통계를 양품(BIN==PASS_BIN) die 만으로 낸다("Bin1 only" 상세).
    ``bin1_scope="rt"`` 면 그 필터를 RT source 에만 건다(Temperature "Bin1(RT만)").
    항목이 어떤 소스에도 없으면 KeyError (라우트가 404 처리).
    """
    from .tabs.distribution import scatter_item as _scatter_item

    session, tables, _ = _load_tables(session_id, report_db=report_db,
                                      upload_root=upload_root, session=session)
    mode = _validate_mode(session.get("mode"))
    tables = _mode_tables(tables, mode)
    # Temperature 그룹을 함께 넘긴다 — CT/HT 의 CPK 는 "RT Bin1 die × RT limit" 기준이라
    # (tabs/cpk.build_cpk_rows) 이걸 빼면 Item_detail 만 다른 값을 보여준다.
    # 넘기는 값은 **그룹 리스트**다 (temperature_reference_tables 계약 — metrics 가
    # build_cpk_rows 에 넘기는 것과 같은 형태). 옵션 dict 를 그대로 주면 조용히 no-op 된다.
    temp_groups = None
    if mode == "Temperature":
        temp_groups = (_webreport_temperature_groups(
            session.get("webreport_options") or "", [t.source for t in tables]) or {}).get("groups")
    return _scatter_item(tables, subject, bin1=bin1,
                         bin1_sources=_bin1_source_filter(session, bin1_scope),
                         temperature_groups=temp_groups)


def _commonality_index(session: dict, tables, prep_digest: str = ""):
    """Commonality 인덱스(메타 리스트 + item별 정렬 배열)를 세션 단위로 캐시해 반환.

    키는 tables 캐시와 동일한 (analysis_key, content_hash[, prep]) — raw_data 편집 시
    content_hash 변경으로, 전처리 변경 시 digest 변경으로 각각 자연 무효화되고,
    AKEY_CACHES 등록으로 세션 삭제 시에도 정리된다. 전처리 digest 가 키에 필요한 이유는
    셀 패치·조건 규칙이 이 인덱스가 읽는 SERIAL/BIN·die 구성을 바꾸기 때문이다.
    콜드 미스(전 item 정렬, 수 초 CPU)는 single-flight 락으로 중복 계산을 막는다.
    """
    from .tabs.commonality import build_index

    cache_key = cache_policy.commonality_key(session, prep_digest)
    idx = cache.cache_get(cache.COMMONALITY_CACHE, cache_key)
    if idx is None:
        with cache.keyed_lock_ctx(("commonality",) + cache_key):
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
                        index=_commonality_index(
                            session, tables,
                            _preprocess.session_digest(report_db, session_id)),
                        serial=serial, xpos=xpos, ypos=ypos)


def commonality_chip(session_id: str, *, report_db, upload_root: Path,
                     serial: str = "", xpos: str = "", ypos: str = "", source: str = "") -> dict:
    """선택 chip 의 항목별 값 + 누적%(ECDF 위치) + wafer 좌표. 못 찾으면 KeyError."""
    from .tabs.commonality import chip_percentiles

    session, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return chip_percentiles(tables, serial=serial, xpos=xpos, ypos=ypos, source=source,
                            index=_commonality_index(
                                session, tables,
                                _preprocess.session_digest(report_db, session_id)))


def edit_raw_data(session_id: str, *, report_db, upload_root: Path, edits: list,
                  client_ip: str = "", user_agent: str = "") -> dict:
    """Raw Data 셀 편집을 저장된 parquet 원본에 그대로 반영한다.

    편집된 source 는 df 기준으로 재인코딩해 기존 analysis_key 의 web_report_source_<idx>
    를 덮어쓴다. 덮어쓰기 직전 현재 원본을 로컬에 1세대 백업하므로(rawedit.
    backup_current_sources) 실수 편집은 운영자가 수동 복구할 수 있다 — 앱 내 undo 는 없다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    old_content_hash = session.get("content_hash") or ""

    # 같은 analysis_key 원본의 read-modify-write 직렬화 — 동시 편집 lost update 방지
    # (rawedit.replace_sources 와 같은 락 키. 단일 프로세스 전제라 in-process 락으로 충분 —
    # DB 기반 core.report_analysis_lock 은 멀티프로세스 전환 시에만 배선한다.)
    with cache.keyed_lock_ctx(("rawedit", analysis_key)):
        # apply_raw_data_edits 가 df 를 in-place 수정하므로 캐시 원본 오염 방지 위해 캐시 우회.
        # apply_prep=False — 편집·재인코딩 대상은 언제나 저장된 원본이다 (전처리는 표시용).
        session, tables, manifest = _load_tables(
            session_id, report_db=report_db, upload_root=upload_root, use_cache=False,
            session=session, apply_prep=False)

        updated_tables = raw_data_tab.apply_raw_data_edits(tables, edits)
        sources_bytes = [encode_honeyform_parquet(t.df) for t in updated_tables]

        content_hash = hashlib.sha256(
            _canon({"files": [hashlib.sha256(b).hexdigest() for b in sources_bytes]})
        ).hexdigest()

        # 덮어쓰기 직전 현재 원본 1세대 백업 — 실패 시 예외로 편집 거부(원본 무손상).
        backup_name = _rawedit.backup_current_sources(
            analysis_key, upload_root, old_content_hash=old_content_hash)

        storage_result = runtime.storage().save_webreport_sources(
            analysis_key, content_hash, sources_bytes, manifest, upload_root=upload_root)

        # dedup 형제 세션까지 갱신 — 편집한 세션만 갱신하면 같은 analysis_key 를 공유하는
        # 다른 세션이 옛 hash 로 stale disk_cache payload 를 계속 서빙한다.
        report_db.update_content_hash_for_analysis_key(analysis_key, content_hash)
        # 구 content_hash 키 엔트리는 더 이상 조회되지 않으므로 메모리 회수용으로만 정리
        cache.evict_akey_caches(analysis_key)
        # 구 세대 Distribution pack 회수 — 새 chash 로는 조회되지 않지만(디렉토리명에 chash)
        # 남겨두면 용량만 먹는다. 새 세대 pack 은 아래에서 백그라운드로 다시 만든다.
        dist_pack_store.delete_stale(upload_root, analysis_key, content_hash)
        # 행 위치 기반 전처리 셀 패치는 원본이 바뀌면 엉뚱한 행을 가리킨다 — 형제까지 해제.
        # (이 함수의 `edits` 파라미터가 모듈명을 가리므로 별칭으로 받는다.)
        from . import edits as _edits

        dropped = _edits.drop_preprocess_edits_for_akey(report_db, analysis_key, user_agent)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=(f"raw_data({len(edits)} cells, backup={backup_name}"
                            + (f", quick_edits_cleared={dropped}" if dropped else "") + ")"),
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    # 편집 후 재계산은 백그라운드 — 응답을 늦추지 않는다 (rawedit.replace_sources 와 동일).
    # 새 세대 pack 을 서버가 다시 만들어 둬야 Honey 왕복 없이도 이후 Distribution 조회가
    # 덧셈만 한다(구 pack 은 위에서 회수됐다). 전처리 variant 는 다음 조회가 예약한다.
    try:
        compute.request_dist_pack(session_id, str(upload_root), base=True)
        compute.prewarm(session_id, str(upload_root))
    except Exception:
        _log.warning("raw_data 편집 후 재계산 예약 실패 session=%s", session_id, exc_info=True)

    return {"ok": True, "edited_cells": len(edits), "storage": storage_result["storage"]}


def get_preprocess(session_id: str, *, report_db) -> dict:
    """세션의 조회 전처리 옵션 (항목 제외 / outlier). 없으면 빈 spec.

    Honey 의 Rawdata 허브 다이얼로그가 현재 상태를 그리기 위해 부른다 — DB 만 읽는다."""
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    spec = edits.load_preprocess(report_db, session_id)
    return {"spec": spec, "summary": _preprocess.describe(spec),
            "digest": _preprocess.digest(spec),
            **_yield_basis_view(session, edits.load_yield_basis_map(report_db, session_id))}


def _yield_basis_view(session, basis_map: dict) -> dict:
    """수율 분모 기준 응답 조각 — 허브 다이얼로그 상태 복원 + 안내용 gross_die.

    ``yield_basis``(전역 모드)는 구 허브가 체크박스 복원에 쓰던 키라 문자열 그대로 둔다."""
    from .tabs.yield_tab import gross_die_value

    return {"yield_basis": basis_map.get("mode") or edits.YIELD_BASIS_AUTO,
            "yield_basis_sources": dict(basis_map.get("sources") or {}),
            "gross_die": gross_die_value(session.get("gross_die"))}


def get_yield_basis(session_id: str, *, report_db, upload_root: Path) -> dict:
    """소스별 수율 분모 기준 + 그 기준을 고르는 데 필요한 수치 (Honey 허브 [Yield 계산] 탭).

    ``pass``/``tested``/``gross`` 를 함께 내려주면 클라가 두 기준의 수율을 **서버 왕복 없이**
    계산할 수 있다 — 체크를 바꿀 때마다 실시간으로 바뀌는 값이라 왕복하면 안 된다.
    수치는 리포트와 같아야 하므로 전처리를 적용한 tables(기본)로 센다.
    """
    from .tabs.common import PASS_BIN, bin_types
    from .tabs.yield_tab import GROSS_SHORTFALL_LIMIT, resolve_source_basis

    session, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    tables = _mode_tables(tables, _validate_mode(session.get("mode")))
    basis_map = edits.load_yield_basis_map(report_db, session_id)
    info = resolve_source_basis(tables, session.get("gross_die"), basis_map)
    sources = []
    for table in tables:
        entry = dict(info[table.source])
        entry["pass"] = sum(1 for b in bin_types(table) if b == PASS_BIN)
        sources.append(entry)
    return {"mode": basis_map.get("mode") or edits.YIELD_BASIS_AUTO,
            "shortfall_limit": GROSS_SHORTFALL_LIMIT,
            "sources": sources,
            **_yield_basis_view(session, basis_map)}


def save_preprocess(session_id: str, *, report_db, upload_root: Path, spec: dict,
                    client_ip: str = "", user_agent: str = "") -> dict:
    """조회 전처리 옵션 저장 (빈 spec = 해제). 원본 parquet 은 건드리지 않는다.

    body 에 ``yield_basis`` 가 함께 오면 수율 분모 기준도 저장한다 — 같은 허브 다이얼로그의
    [저장] 한 번에 묶여 오기 때문이다(저장 위치는 별도 kind). 형식은
    ``{"mode":"auto|test", "sources":{"<source>":"gross|test"}}`` 이며, 구 클라가 보내는
    문자열('gross'|'test')도 그대로 받는다(normalize_yield_basis_map).

    **셀 패치(edits)·조건 규칙(rules)은 키가 없으면 저장값을 유지**한다(빈 리스트로 보내면
    해제). 구버전 Honey 허브가 화면 상태 전체를 보내면서 이 두 키를 모르기 때문에, 종전처럼
    통짜 replace 로 두면 구 클라의 [저장] 한 번이 빠른 수정 결과를 조용히 지운다.
    레거시 키(exclude_items/outlier/yield_basis)는 종전 replace 의미론 그대로다 — 허브가
    "화면에 보이는 상태를 그대로 저장"하는 계약이라 부재 = 해제여야 한다.

    rev 증가로 REPORT//full/TRIM 캐시가, digest 변화로 tables/dist/map/scatter 캐시가
    각각 자연 무효화된다 — 여기서 evict 를 부르지 않는 이유다(되돌리면 옛 캐시가 그대로
    다시 히트한다). Distribution pack 은 캐시가 아니라 세대별 영구 데이터라 이 규칙 밖이다:
    새 spec 의 variant 생성을 예약하고, 더 이상 쓰이지 않는 구 spec variant 만 회수한다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    spec = _merge_preprocess_spec(edits.load_preprocess(report_db, session_id), spec)
    norm = _preprocess.normalize(spec)
    stats = _validate_preprocess(norm, session_id, report_db=report_db,
                                 upload_root=upload_root, session=session)
    old_digest = _preprocess.session_digest(report_db, session_id)
    updated_by = edits.user_from_ua(user_agent) or None
    rev = edits.save_preprocess(report_db, session_id, norm, updated_by=updated_by)

    new_digest = _preprocess.digest(norm)
    if old_digest and old_digest != new_digest:
        dist_pack_store.delete_variant(upload_root, analysis_key,
                                       session.get("content_hash"),
                                       _validate_mode(session.get("mode")), old_digest)
    if new_digest:
        # 첫 조회부터 덧셈만 하도록 미리 만들어 둔다 (이미 있으면 잡이 즉시 끝난다).
        try:
            compute.request_dist_pack(session_id, str(upload_root))
        except Exception:
            _log.warning("전처리 pack 생성 예약 실패 session=%s", session_id, exc_info=True)

    basis_changed = ""
    saved_basis = edits.load_yield_basis_map(report_db, session_id)
    if isinstance(spec, dict) and spec.get("yield_basis") is not None:
        basis = edits.normalize_yield_basis_map(spec.get("yield_basis"))
        if basis != saved_basis:
            rev = edits.save_yield_basis_map(report_db, session_id, basis,
                                             updated_by=updated_by)
            basis_changed = " yield_basis(%s%s)" % (
                basis["mode"],
                f"+소스 {len(basis['sources'])}" if basis["sources"] else "")
    else:
        basis = saved_basis

    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"preprocess({_preprocess.describe(norm) or 'off'}){basis_changed}",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass
    return {"ok": True, "spec": norm, "summary": _preprocess.describe(norm),
            "digest": _preprocess.digest(norm), "rev": rev, "stats": stats,
            **_yield_basis_view(session, basis)}


# 저장 spec 상한 — 상한을 넘으면 400. 셀 패치는 spec JSON 크기(편집 DB 1행)와 조회 때마다의
# 적용 비용을 함께 제한하는 값이고, 규칙은 소스마다 전량 스캔이라 개수 자체를 작게 잡는다.
_PREP_MAX_EDITS = 10_000
_PREP_MAX_RULES = 50


def _merge_preprocess_spec(saved: dict, incoming) -> dict:
    """저장된 spec 위에 요청 body 를 얹는다 — **신규 키(edits/rules)만 부재=유지**.

    구버전 Honey 허브는 exclude_items/outlier/yield_basis 만 보내므로, 그 저장이 빠른
    수정 결과(edits/rules)를 지우지 않게 하는 것이 목적이다. 해제하려면 빈 리스트를
    명시적으로 보내면 된다.
    """
    incoming = dict(incoming) if isinstance(incoming, dict) else {}
    for key in ("edits", "rules"):
        if key not in incoming and saved.get(key):
            incoming[key] = saved[key]
    return incoming


def _validate_preprocess(norm: dict, session_id: str, *, report_db, upload_root: Path,
                         session) -> dict:
    """정규화된 spec 이 이 세션에 실제로 적용 가능한지 검사하고 적용 통계를 돌려준다.

    조회 경로(preprocess.apply_tables)는 이상한 spec 을 조용히 건너뛰도록 만들어져 있지만,
    저장 시점에는 사용자에게 알려야 한다 — 오타난 source/컬럼이 조용히 무시되면 "저장했는데
    아무 일도 안 일어난다"가 된다. 여기서 tables 를 한 번 로드해(대개 캐시 히트) 실제로
    적용해 보고, 결과가 비면 ValueError(라우트에서 400)를 올린다.
    """
    if not norm:
        return {}
    if len(norm.get("edits") or ()) > _PREP_MAX_EDITS:
        raise ValueError(f"셀 수정이 너무 많습니다 ({len(norm['edits'])} > {_PREP_MAX_EDITS}).")
    if len(norm.get("rules") or ()) > _PREP_MAX_RULES:
        raise ValueError(f"일괄 규칙이 너무 많습니다 ({len(norm['rules'])} > {_PREP_MAX_RULES}).")
    if not (norm.get("edits") or norm.get("rules")):
        return {}          # 레거시 옵션만 — 종전대로 검증 없이 저장(비용 0)

    _, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root,
                                session=session, apply_prep=False)
    _check_edit_targets(norm.get("edits") or (), tables)
    applied, stats = _preprocess.apply_tables(tables, norm)
    for table in applied:
        if not len(table.data):
            raise ValueError(
                f"'{table.source}' 의 die 가 모두 제외됩니다 — 조건을 좁혀 주세요.")
    if norm.get("rules") and not (stats["rule_hits"] or stats["excluded_dies"]):
        raise ValueError("조건에 맞는 die 가 없습니다 — 조건을 확인해 주세요.")
    return {"edited_cells": stats["edited_cells"], "rule_hits": stats["rule_hits"],
            "excluded_dies": stats["excluded_dies"]}


def _check_edit_targets(edit_list, tables) -> None:
    """셀 패치의 source/column/row_idx 가 실제로 존재하는지 + 값이 규칙에 맞는지 검사.

    값 규칙은 웹 셀 편집(raw_data_tab.apply_raw_data_edits)과 같은 rawvalues 를 쓴다 —
    저장 경로가 둘로 갈려도 사용자가 겪는 판정은 하나여야 한다.
    """
    from .honeyform import META_COLUMNS

    by_source = {t.source: t for t in tables}
    for edit in edit_list:
        table = by_source.get(edit["source"])
        if table is None:
            raise ValueError(f"알 수 없는 source: {edit['source']}")
        column = edit["column"]
        is_item = column in table.item_columns
        if not is_item and column not in META_COLUMNS:
            raise ValueError(f"알 수 없는 컬럼: {column}")
        if not (0 <= edit["row_idx"] < len(table.data)):
            raise ValueError(f"행 위치가 범위를 벗어났습니다: {edit['row_idx']}")
        reason = rawvalues.check_cell_value(column, edit["value"], is_item=is_item)
        if reason:
            raise ValueError(f"[{column}] {reason}")


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
            # 트리거 실패를 조용히 삼키면 ETC 편집 후 eval DB 동기화가 상시 누락돼도
            # 아무도 모른다 — export 자체는 safe_export 에서 감사하지만 트리거 실패는 별개.
            _log.warning("eval export 재적재 트리거 실패 — ETC 항목 편집 후 코멘트 "
                         "eval DB 동기화 누락 (session=%s)", session_id, exc_info=True)

    return {"ok": True, "etc_items": etc_items,
            "storage": "db" if changes else "unchanged"}


def update_issue_hidden(session_id: str, *, report_db, upload_root: Path,
                        action: str, key: str = "",
                        client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table 행 숨김(삭제)을 세션 편집 DB(kind=issue_hidden)에 반영한다.

    action="hide": key("Yield|<bin>"|"CPK|<item>") 1건 숨김. ETC 행은 기존 etc_item
    remove 가 담당하므로 "ETC|" 키는 거부한다. action="reset_all": 숨김 전건 복원
    (행별 복원 없음 — '삭제 전체 초기화' 버튼 전용). 행 데이터는 저장하지 않고 키만
    기억한다 — build_issue_table_rows 가 조회 시 해당 이슈 행을 제외할 뿐이라,
    rawdata 변경(새 세션)이나 초기화 시 원래 행이 그대로 되살아난다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    action = str(action or "").strip()
    key = str(key or "").strip()
    if action not in ("hide", "reset_all"):
        raise ValueError(f"unknown action: {action!r}")
    if action == "hide":
        if not key or len(key) > 300:
            raise ValueError(f"invalid row key: {key!r}")
        if not key.startswith(_ISSUE_KEY_PREFIXES[:3]):   # ETC 는 숨김 대신 항목 제거
            raise ValueError(f"row not hidable: {key!r}")

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    hidden = edits.load_edit_state(report_db, session_id)["issue_hidden"]
    changes = []
    if action == "hide":
        if key not in hidden:
            changes.append((edits.KIND_ISSUE_HIDDEN, key, "1"))
            hidden.append(key)
    else:
        changes = [(edits.KIND_ISSUE_HIDDEN, k, None) for k in hidden]
        hidden = []
    if changes:
        report_db.apply_webreport_edits(session_id, changes,
                                        updated_by=edits.user_from_ua(user_agent) or None)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"issue_hidden({action}:{key!r})",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    return {"ok": True, "hidden": hidden,
            "storage": "db" if changes else "unchanged"}


def _trigger_eval_export(session_id: str, *, report_db, upload_root, why: str) -> None:
    """eval DB 재적재 트리거 (백그라운드 큐, 실패 무해 — docs/13 §9).

    트리거 실패를 조용히 삼키면 편집 후 eval DB 동기화가 상시 누락돼도 아무도 모른다
    — export 자체의 실패는 safe_export 가 감사에 남기지만 트리거 실패는 별개다.
    """
    try:
        from . import eval_export
        eval_export.export_async(session_id, report_db=report_db,
                                 upload_root=upload_root)
    except Exception:
        _log.warning("eval export 재적재 트리거 실패 — %s 편집 후 eval DB 동기화 누락 "
                     "(session=%s)", why, session_id, exc_info=True)


def _norm_issue_status(key, value):
    """Issue Table Status 키/값 검증 — 단건·일괄 저장 공용. 반환 (key, value)."""
    key = str(key or "").strip()
    value = str(value or "").strip()
    if not key or len(key) > 300 or not key.startswith(_ISSUE_KEY_PREFIXES):
        raise ValueError(f"invalid row key: {key!r}")
    if value not in ("Open", "Close"):
        raise ValueError(f"invalid status: {value!r}")
    return key, value


def update_issue_status(session_id: str, *, report_db, upload_root: Path,
                        key: str, value: str,
                        client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table 행 Status(Open/Close)를 세션 편집 DB(kind=issue_status)에 저장한다.

    key 는 이슈 단위 키("Yield|<bin>"|"CPK|<item>"|"ETC|<item>"). "Close" 만 저장하고
    Open 은 행 삭제(부재=Open) — 기본상태 무기록으로 DB 를 최소화한다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    key, value = _norm_issue_status(key, value)

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    changes = [(edits.KIND_ISSUE_STATUS, key, "Close" if value == "Close" else None)]
    report_db.apply_webreport_edits(session_id, changes,
                                    updated_by=edits.user_from_ua(user_agent) or None)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"issue_status({key!r}={value})",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    # Status 가 곧 적재 게이트다 (2026-08-04) — Close 인 이슈의 코멘트만 eval DB 로
    # 나가므로, Open↔Close 전환 때마다 재적재해야 한다(Close→Open 이면 reconciliation
    # 이 그 case 의 라벨을 지운다).
    _trigger_eval_export(session_id, report_db=report_db, upload_root=upload_root,
                         why="issue_status")

    return {"ok": True, "key": key, "value": value, "storage": "db"}


_SIGNATURE_MAX_IDS = 8


def _norm_issue_signature(key, value):
    """Signature 확정값 검증 → (key, [id...]). 빈 목록 = 해제(미검수로 되돌림).

    허용 id 는 **엔진 signature 카탈로그에 정의된 것 + UNKNOWN** 뿐이다 — 정규식만
    검사하면 UI 를 우회해 아무 문자열이나 라벨로 심을 수 있고, 그 라벨은 나중에
    "정답 signature" 통계에 그대로 섞인다. 카탈로그에서 사라진 legacy 값은 화면 표시
    (기존 저장값)는 되지만 새로 저장되지는 않는다.
    """
    key = str(key or "").strip()
    if not key or len(key) > 300 or not key.startswith(_ISSUE_KEY_PREFIXES):
        raise ValueError(f"invalid row key: {key!r}")
    if value is None:
        value = []
    if not isinstance(value, list):
        raise ValueError("signatures must be a list")
    if len(value) > _SIGNATURE_MAX_IDS:
        raise ValueError(f"too many signatures: {len(value)}")

    from . import ai_comment
    allowed = {s["id"] for s in ai_comment.signature_catalog()}
    ids, seen = [], set()
    for raw in value:
        sig = str(raw or "").strip().upper()
        if not sig:
            continue
        if sig not in allowed:
            raise ValueError(f"unknown signature: {sig!r}")
        if sig in seen:
            raise ValueError(f"duplicate signature: {sig!r}")
        seen.add(sig)
        ids.append(sig)
    return key, ids


def update_issue_signature(session_id: str, *, report_db, upload_root: Path,
                           key: str, value,
                           client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table 행의 **ENGR 확정 signature** 를 세션 편집 DB 에 저장한다.

    key 는 comment 와 같은 row_key("Yield|<bin>|<item>"|"CPK|<item>"|"ETC|<item>"),
    value 는 id 목록(순서 = 우선순위). 빈 목록이면 편집행을 지워 "미검수 + 엔진 제안"
    상태로 되돌린다. 엔진 제안과 **같은 값이어도 저장한다** — 그래야 "ENGR 가 동의한
    사례"가 남아 정정 사례만 쌓이는 편향을 피한다.

    저장 순서는 편집 DB 먼저(진실), eval DB 반영은 그 뒤 비동기다 — 동기화 워커가
    요청값이 아니라 편집 DB 의 최신 전체 상태를 다시 읽으므로 연속 편집도 마지막
    상태로 수렴한다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    key, ids = _norm_issue_signature(key, value)

    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    changes = [(edits.KIND_ISSUE_SIGNATURE, key,
                edits.signature_value(ids) if ids else None)]
    report_db.apply_webreport_edits(session_id, changes,
                                    updated_by=edits.user_from_ua(user_agent) or None)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"issue_signature({key!r}={'+'.join(ids) or '(해제)'})",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    _trigger_signature_sync(session_id, report_db=report_db, upload_root=upload_root)

    return {"ok": True, "key": key, "signatures": ids, "storage": "db"}


def _trigger_signature_sync(session_id: str, *, report_db, upload_root: Path) -> None:
    """ENGR 확정 signature → eval DB 동기화 예약 (best-effort — 실패해도 편집은 유지)."""
    try:
        from . import eval_export
        eval_export.sync_signatures_async(session_id, report_db=report_db,
                                          upload_root=upload_root)
    except Exception:
        _log.warning("signature 동기화 트리거 실패 — eval DB 반영 누락 (session=%s)",
                     session_id, exc_info=True)


_ISSUE_STATUS_MAX_ITEMS = 3000


def update_issue_status_bulk(session_id: str, *, report_db, upload_root: Path,
                             items, client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table 행 Status 를 여러 건 한 번에 저장 — update_issue_status 의 일괄판.

    items: [{"key": ..., "value": "Open"|"Close"}, ...]. 키 검증·저장 규약("Close" 만
    저장, Open 은 행 삭제)은 단건과 완전히 동일하고, 편집 DB write 만 1회로 묶는다
    (전체 Open/Close 는 행이 수백 개라 단건 요청을 반복하면 느리고 경합이 난다).
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if len(items) > _ISSUE_STATUS_MAX_ITEMS:
        raise ValueError(f"too many items: {len(items)}")

    changes, seen = [], set()
    for it in items:
        key, value = _norm_issue_status((it or {}).get("key"), (it or {}).get("value"))
        if key in seen:
            continue
        seen.add(key)
        changes.append((edits.KIND_ISSUE_STATUS, key, "Close" if value == "Close" else None))

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    report_db.apply_webreport_edits(session_id, changes,
                                    updated_by=edits.user_from_ua(user_agent) or None)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"issue_status(bulk {len(changes)} rows)",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    _trigger_eval_export(session_id, report_db=report_db, upload_root=upload_root,
                         why="issue_status(bulk)")

    return {"ok": True, "count": len(changes), "storage": "db"}


_COMMENT_MAX_ITEMS = 200
_COMMENT_MAX_LEN = 2000


def update_issue_comments(session_id: str, comments: list, *, report_db, upload_root: Path,
                          client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table 의 PTE/개발 comment 를 세션 편집 DB(kind=issue_comment)에 저장한다.
    manifest 는 불변 스냅샷.

    comments: [{"key": row_key, "col": comment 컬럼명, "value": str}, ...].
    row_key 는 tabs/issue_table.py 규칙("Yield|<bin>|<item>", "CPK|<item>", "TEMP|<item>",
    "ETC|<item>")을 따르고, 빈 value 는 해당 항목 삭제로 처리한다.
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
        if not key or len(key) > 300 or not key.startswith(_ISSUE_KEY_PREFIXES):
            # hidden/status 와 같은 접두 화이트리스트 — 오타 키가 DB 에 조용히 쌓이고
            # eval export 단계에서만 버려지던 구멍을 막는다 (2026-08-05).
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
            _log.warning("eval export 재적재 트리거 실패 — 코멘트 편집 후 eval DB "
                         "동기화 누락 (session=%s)", session_id, exc_info=True)
        storage = "db"
    else:
        storage = "unchanged"

    return {"ok": True, "updated": changed, "storage": storage}


# temp 는 Temperature 모드 세션에서만 화면에 뜨지만(map_select.js engrCommentFields),
# 키 자체는 모드와 무관하게 받는다 — 모드 판정을 저장 경로에 넣으면 옵션이 바뀐 세션의
# 기존 값이 저장 불가로 막힌다.
_ENGR_KEYS = ("yield", "cpk", "temp", "etc")
# 글자 크기·색 서식이 붙은 값은 제한 HTML(선두 마커 "<!--rich-->")이라 태그만큼 길어진다.
# Issue comment 와 같은 2000자를 그대로 쓰면 평문 기준으로는 짧은 글이 저장 거부돼 사용자
# 입력이 날아간다(§5-12) — Engr 칸만 상한을 따로 둔다.
_ENGR_MAX_LEN = 8000
# 클라이언트는 저장 전에도 화이트리스트로 걸러 보내고, **조회 화면도 그릴 때마다 다시 거른다**
# (map_select.js engrSanitize). 여기 검사는 그 위의 얕은 방어일 뿐이라, 평문에는 나올 수 없는
# 실행 가능 태그만 막는다 — 조건을 넓히면 정상 문장이 저장 거부돼 입력을 잃는다.
_ENGR_UNSAFE_RE = re.compile(r"<\s*/?\s*(script|iframe|object|embed|link|meta|style)\b", re.I)


def update_summary_engr(session_id: str, values: dict, *, report_db, upload_root: Path,
                        client_ip: str = "", user_agent: str = "") -> dict:
    """Summary 탭의 Engr Comment(Yield/CPK/TEMP/ETC 칸)를 세션 편집 DB(kind=summary_engr)에
    저장한다. manifest 는 불변 스냅샷.

    values: {"yield": str, "cpk": str, "temp": str, "etc": str} 중 온 키만 갱신하고,
    빈 값은 삭제로 처리한다. (temp 는 Temperature 모드 화면에서만 노출)

    값은 평문이거나, 글자 크기·색 서식이 붙었으면 선두 마커 ``<!--rich-->`` + 제한 HTML 이다
    (마커 문법·화이트리스트 정본은 static/webreport/map_select.js). 서버는 저장만 하고
    해석하지 않는다 — 서식 없는 편집 결과는 클라이언트가 다시 평문으로 되돌려 보낸다.
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
        if len(val) > _ENGR_MAX_LEN:
            raise ValueError(f"comment too long ({len(val)} > {_ENGR_MAX_LEN} chars)")
        if _ENGR_UNSAFE_RE.search(val):
            raise ValueError("허용되지 않는 태그가 포함돼 있습니다.")
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
# Note 시트 JSON 상한 (2026-08-06: 3MB → 10MB, 사용자 요청). 프런트 note.js NOTE_MAX_BYTES
# 와 **같은 값**을 유지할 것 — 프런트는 저장 전 안내용으로 같은 기준을 미리 검사한다.
#
# 이 상한이 큰 이유는 **이미지가 시트 JSON 안에 들어오기 때문**이다. Luckysheet 자체 경로
# (툴바 업로드·드래그&드롭·Ctrl+V)는 FileReader.readAsDataURL → base64 data URI 를 시트에
# 박는다(원본 대비 +33%). 서버에 따로 올라가는 것은 차트→Note 붙여넣기(chart_notes.js →
# POST .../note_image) 뿐이고 그건 URL 문자열만 시트에 남는다.
_NOTE_SHEET_MAX_BYTES = 10 * 1024 * 1024

_NOTE_TAG_MAX = 200
# 태그명 1~40자, linkify 토큰(#[..]/@[..])·드롭다운 쿼리와 충돌하는 문자 금지.
_NOTE_TAG_NAME_RE = re.compile(r"^[^\[\]#@\x00-\x1f\x7f]{1,40}$")
_NOTE_TAG_SHEET_MAX = 64
_NOTE_TAG_SHEET_NAME_MAX = 80
_NOTE_TAG_COORD_MAX = 99999


class NoteConflict(Exception):
    """Note 시트 저장 시 base 토큰 불일치 — 내가 읽은 뒤 남이 먼저 저장했다.

    info: {"updated_by", "updated_at", "base"} (라우트가 409 응답 본문에 실어준다)."""

    def __init__(self, info):
        super().__init__("note sheet conflict")
        self.info = info or {}


_SHAPE_TYPES = ("circle", "rect", "line", "path")
_SHAPE_KEYS = ("type", "x0", "x1", "y0", "y1", "path", "xref", "yref",
               "line", "fillcolor", "opacity")
_SHAPE_LINE_KEYS = ("color", "width", "dash")
_TEXT_KEYS = ("x", "y", "xref", "yref", "text", "showarrow", "arrowhead",
              "ax", "ay", "font", "bgcolor", "bordercolor",
              "arrowcolor", "arrowwidth")
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
        # 텍스트 없는 주석은 버리되, 화살표 전용 주석(showarrow, 텍스트 없이 지점만
        # 가리킴 — 프런트 "화살표" 도구)은 허용한다 (2026-08-07).
        if not str(out.get("text") or "").strip() and not out.get("showarrow"):
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


def _clean_note_tag_target(target) -> dict:
    """태그 위치 spec 검증·정리 — 1단계는 Note 셀만(tab=="note").

    좌표(r/c)는 Note 시트에 행/열이 삽입되면 어긋날 수 있다(1단계 한계) —
    같은 이름으로 재태그(upsert)하면 앵커가 갱신돼 복구된다. sheet(=시트 index)가
    안정 ID 이고 sheet_name 은 시트가 재생성돼 index 가 바뀐 경우의 폴백이다."""
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    tab = str(target.get("tab") or "note")
    if tab != "note":
        raise ValueError(f"unsupported tag target tab: {tab!r}")
    sheet = str(target.get("sheet") or "").strip()
    if not sheet or len(sheet) > _NOTE_TAG_SHEET_MAX:
        raise ValueError("invalid sheet index")
    sheet_name = str(target.get("sheet_name") or "")[:_NOTE_TAG_SHEET_NAME_MAX]
    try:
        r = int(target.get("r"))
        c = int(target.get("c"))
    except (TypeError, ValueError):
        raise ValueError("invalid cell coordinates")
    if not (0 <= r <= _NOTE_TAG_COORD_MAX) or not (0 <= c <= _NOTE_TAG_COORD_MAX):
        raise ValueError("cell coordinates out of range")
    return {"tab": "note", "sheet": sheet, "sheet_name": sheet_name, "r": r, "c": c}


def update_note_tag(session_id: str, *, report_db, upload_root: Path,
                    action: str, name: str, target=None,
                    client_ip: str = "", user_agent: str = "") -> dict:
    """앵커 태그 생성/삭제 — 세션 편집 DB(kind=note_tag, item_key=태그명).

    action=="set": target(Note 셀 위치)로 태그를 만들거나 재지정(upsert).
    action=="delete": 태그 삭제. 반환의 note_tags 는 전체 맵이라 클라 DATA.note_tags 를
    권위 갱신한다 (chart_notes 와 동형)."""
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    name = str(name or "").strip()
    if not _NOTE_TAG_NAME_RE.match(name):
        raise ValueError(f"invalid tag name: {name[:40]!r}")
    if action not in ("set", "delete"):
        raise ValueError(f"unknown action: {action!r}")

    existing = edits.load_note_tags(report_db, session_id)
    if action == "set":
        clean = _clean_note_tag_target(target)
        if name not in existing and len(existing) >= _NOTE_TAG_MAX:
            raise ValueError(f"too many tags (max {_NOTE_TAG_MAX})")
        blob = json.dumps(clean, ensure_ascii=False, sort_keys=True)
        changes = [(edits.KIND_NOTE_TAG, name, blob)]
    else:
        if name not in existing:
            return {"ok": True, "rev": report_db.get_webreport_edit_rev(session_id),
                    "note_tags": existing}
        changes = [(edits.KIND_NOTE_TAG, name, None)]

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    rev = report_db.apply_webreport_edits(session_id, changes,
                                          updated_by=edits.user_from_ua(user_agent) or None)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"note_tag({action}:{name!r})",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass
    return {"ok": True, "rev": rev,
            "note_tags": edits.load_note_tags(report_db, session_id)}


def get_note_tags(session_id: str, *, report_db) -> dict:
    """/full extras 조립용 — 태그명 → {tab,sheet,sheet_name,r,c,updated_by,updated_at}."""
    return edits.load_note_tags(report_db, session_id)


# 시트 이름 목록 memo — 세션 상세를 열 때마다 Summary 가 물어보는데 본문은 최대 10MB 라
# 매번 json 파싱을 돌리면 낭비다. 키에 updated_at 을 넣어 Note 가 저장되면 자동 무효화.
_NOTE_SHEET_NAMES_MEMO: dict = {}
_NOTE_SHEET_NAMES_MEMO_MAX = 128


def get_note_sheet_names(session_id: str, *, report_db) -> list:
    """Note 시트 이름 목록 — [{"index","name","order"}]. 셀 본문은 버린다.

    Summary 탭의 $[시트명] 자동완성·시트 버튼 줄이 쓴다. 이름만 필요한데 본문 전체를
    내려보내는 lazy GET(.../web_report/note)을 부르면 최대 10MB 를 헛되이 옮기게 된다."""
    stamp = ""
    for row in report_db.get_webreport_edit_meta(session_id, edits.KIND_NOTE_SHEET):
        if row.get("item_key") == "sheet":
            stamp = str(row.get("updated_at") or "")
            break
    else:
        return []   # Note 자체가 없음 — 본문을 읽지 않는다
    key = (session_id, stamp)
    hit = _NOTE_SHEET_NAMES_MEMO.get(key)
    if hit is not None:
        return hit

    saved = edits.load_note_sheet(report_db, session_id) or {}
    sheets = (saved.get("sheet") or {}).get("sheets")
    names = []
    for idx, sheet in enumerate(sheets or []):
        if not isinstance(sheet, dict):
            continue
        name = str(sheet.get("name") or "")
        if not name:
            continue
        try:
            order = int(sheet.get("order", idx))
        except (TypeError, ValueError):
            order = idx
        names.append({"index": str(sheet.get("index") or ""), "name": name, "order": order})
    names.sort(key=lambda s: s["order"])
    if len(_NOTE_SHEET_NAMES_MEMO) >= _NOTE_SHEET_NAMES_MEMO_MAX:
        _NOTE_SHEET_NAMES_MEMO.clear()
    _NOTE_SHEET_NAMES_MEMO[key] = names
    return names


def load_note(session_id: str, *, report_db) -> dict:
    """Note 탭 lazy GET — {"sheet": dict|None, "updated_at", "updated_by", "base"}.

    base 는 낙관적 잠금 토큰 — 클라가 저장 시 되돌려 보내면 그 사이 남이 저장했는지
    검사한다 (시트가 없으면 None)."""
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    return edits.load_note_sheet(report_db, session_id) or {"sheet": None, "base": None}


def save_note(session_id: str, sheet, *, report_db, upload_root: Path,
              base=None, check: bool = False, force: bool = False,
              client_ip: str = "", user_agent: str = "") -> dict:
    """Note 탭 시트 JSON 저장 (전체 치환) — 세션 편집 DB(kind=note_sheet, item_key='sheet').

    sheet: Luckysheet 시트 상태 dict (셀 계산은 전부 클라이언트 — 서버는 저장만).
    null/빈 dict 는 삭제. 직렬화 크기 상한은 _NOTE_SHEET_MAX_BYTES.

    시트는 통째로 치환되므로 동시 편집 시 상대의 Note 전체가 사라진다. check 이면
    호출자가 읽었던 base 토큰과 현재 저장본을 비교해 불일치 시 NoteConflict 를 올린다
    (force 는 사용자가 덮어쓰기를 택한 경우). check=False 는 base 를 보내지 않는
    구버전 클라용 하위호환 경로 — 종전대로 무검사 저장."""
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
        action = "save"
    else:
        blob = None
        action = "clear"
    ok, info = report_db.save_note_sheet_checked(
        session_id, edits.KIND_NOTE_SHEET, "sheet", blob, base,
        updated_by=edits.user_from_ua(user_agent) or None, check=check, force=force)
    if not ok:
        raise NoteConflict(info)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"note_sheet({action})",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        _log.warning("note_sheet 감사 기록 실패 (session=%s)", session_id, exc_info=True)
    return {"ok": True, "rev": info["rev"], "base": info["base"]}


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
    with cache.keyed_lock_ctx(("trim",) + cache_key):
        blob = cache.cache_get(cache.TRIM_CACHE, cache_key)
        if blob is None and compute.should_offload(cache_policy.tables_key(
                session, _preprocess.session_digest(report_db, session_id))):
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


def _trim_chart_ctx(session_id: str, *, report_db, upload_root: Path, source: str = ""):
    """Trim 차트 계산의 **요청당 1회** 준비 — (session, table, rule_set, match, prep) 반환.

    tables 로드 → mode/selected 필터 → source 선택 → 그룹 재도출(build_groups)까지.
    배치 요청이 그룹 수만큼 이 준비를 반복하지 않도록 분리한 것이고, 단일 요청의
    동작·산출은 종전과 동일하다. prep 은 차트 캐시 키에 쓰는 전처리 digest.
    """
    from .tabs.trim_analysis import _select_table
    from .trim_match import build_groups, rule_set_for

    prep = _preprocess.session_digest(report_db, session_id)
    session, tables, manifest = _load_tables(
        session_id, report_db=report_db, upload_root=upload_root)
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
    return session, table, rule_set, match, prep


def _pick_trim_group(match, group_id: str):
    """match 결과에서 그룹 1개를 고른다. 없으면 KeyError (라우트 404)."""
    group = next((g for g in match["groups"] if g["id"] == str(group_id)), None)
    if group is None:
        raise KeyError(str(group_id))
    return group


def _trim_chart_gzip(session, table, group, rule_set, prep: str = "") -> bytes:
    """그룹 1개 차트를 gzip bytes 로 (캐시 히트면 그대로) 반환 — 단일/배치 공용.

    캐시 키는 슬롯 구성 digest — overrides 편집이 구성을 바꾸지 않은 그룹의 차트는
    캐시가 살아있다. 단일 라우트와 배치 라우트가 **같은 캐시 엔트리를 공유**한다.
    prep 은 전처리 digest — 슬롯 구성이 같아도 outlier 마스킹으로 **값**이 달라지므로
    키에 함께 넣는다 (전처리 없으면 빈 문자열 = 종전 키).
    """
    from .tabs.trim_analysis import build_trim_chart

    items_digest = hashlib.sha256(_canon({"slots": group["slots"]})).hexdigest()[:16]
    cache_key = cache_policy.trim_chart_key(session, table.source, items_digest, prep)
    blob = cache.cache_get(cache.TRIM_CHART_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock_ctx(("trim_chart",) + cache_key):
        blob = cache.cache_get(cache.TRIM_CHART_CACHE, cache_key)
        if blob is None:
            chart = build_trim_chart(table, group, rule_set)
            blob = gzip.compress(
                json.dumps(chart, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                compresslevel=1)
            cache.trim_chart_cache_put(cache_key, blob)
    return blob


def get_trim_chart_gzip(session_id: str, *, report_db, upload_root: Path,
                        source: str = "", group_id: str = "") -> bytes:
    """Trim 그룹 1개의 chip-to-chip 차트 payload 를 gzip bytes 로 캐시해 반환.

    그룹 재도출(build_groups)은 문자열 연산(ms 단위)이라 요청마다 수행한다.
    그룹/소스가 없으면 KeyError (라우트 404). 프런트는 배치 라우트를 쓰지만 이 단일
    경로는 배치 실패 시 폴백 + 하위호환으로 유지한다.
    """
    session, table, rule_set, match, prep = _trim_chart_ctx(
        session_id, report_db=report_db, upload_root=upload_root, source=source)
    return _trim_chart_gzip(session, table, _pick_trim_group(match, group_id), rule_set, prep)


def get_trim_charts_batch(session_id: str, *, report_db, upload_root: Path,
                          source: str = "", group_ids=()) -> bytes:
    """Trim 그룹 여러 개(화면 1페이지 = 3개)의 차트를 **한 응답**으로 묶어 gzip 반환.

    `{"charts":[...]}` 를 **요청 순서 그대로** 담는다. 그룹당 요청 1건이던 종전 방식은
    요청마다 _trim_chart_ctx(tables 로드 + build_groups)를 반복했는데, 여기서는 그 준비를
    1회만 하고 그룹만 순회한다. 각 조각은 단일 라우트가 돌려주는 본문과 **바이트 동일**
    (같은 `_trim_chart_gzip` 산출을 풀어 이어 붙일 뿐)이라 프런트 렌더 코드가 그대로 쓴다.

    콜드(tables 캐시 미스)면 배치 전체를 컴퓨트 워커로 넘긴다 — payload 쪽
    get_trim_analysis_gzip 과 같은 규약이다. **_load_tables 전에** 판정해야 웹 프로세스가
    미리 디코드해버리는 일이 없다.
    """
    ids = [str(g) for g in (group_ids or [])]
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    if not session.get("analysis_key"):
        raise FileNotFoundError(session_id)
    if ids and compute.should_offload(cache_policy.tables_key(
            session, _preprocess.session_digest(report_db, session_id))):
        return compute.run(compute.trim_chart_batch_job, session_id, str(upload_root),
                           str(source or ""), ids)

    session, table, rule_set, match, prep = _trim_chart_ctx(
        session_id, report_db=report_db, upload_root=upload_root, source=source)
    parts = [gzip.decompress(
        _trim_chart_gzip(session, table, _pick_trim_group(match, gid), rule_set, prep))
        for gid in ids]
    return gzip.compress(b'{"charts":[' + b",".join(parts) + b"]}", compresslevel=1)


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

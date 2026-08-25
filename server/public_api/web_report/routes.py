"""web_report 공개 조회 API — /pe/api/v1/web-report.

HTTP 층은 얇다: 인증 판정 → facade 호출 → **분기 키를 상태코드로 옮기기**뿐이고
조회 로직은 0줄이다(규약 정본은 contracts.py, 계산은 web_report/service.py).

세 가지가 이 파일의 실효 경계다:

1. **신원** — `_access()` 가 `viewer` 를 결정한다. `viewer=None` 은 비공개 필터를 통째로
   생략하는 함정이라(`database/sessions.py:_history_where`) 어떤 경로에서도 만들지 않는다.
   기본은 `""`(공개 세션만), env `WEB_REPORT_API_KEY` 와 헤더가 일치할 때만 비공개 포함.
   키 불일치는 **차단이 아니라 공개 범위**다 — public_api 는 무인증이 기본 성격이고,
   막아 버리면 기존 무인증 소비자가 깨진다.
2. **콜드 빌드** — 요청 스레드에서 계산하지 않는다. facade 가 `{"building": True}` 를
   주면 202 + `status_url` 로 넘긴다(routes_session 의 `/full` 202 계약과 같은 태도).
3. **동시 실행 상한** — 대용량(gzip 전량) 라우트만 세마포어로 막고 못 잡으면 즉시 429.
   waitress 스레드가 13개뿐이라 외부 폴러가 전부 물면 사람 요청이 굶는다
   (`report/routes_chat.py` 가 챗봇에 같은 장치를 둔 이유와 동일).

CSRF 는 걸지 않는다 — 쿠키를 쥘 수 없는 프로그램 호출자가 대상이고, GET 읽기 전용이라
상태를 바꾸지 않는다(`upload_xlsx`/`upload_webreport` 가 이미 같은 이유로 예외다).
"""
from __future__ import annotations

import gzip
import hmac
import logging
import os
import threading
import time
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

from config import REPORT_UPLOAD_DIR
from database import report_db

from . import contracts, facade

_log = logging.getLogger(__name__)

web_report_bp = Blueprint("public_api_web_report", __name__)

# 대용량 응답 동시 실행 상한. 값이 작은 이유: 이 라우트 하나가 수십 MB 를 직렬화하는
# 동안 스레드를 물고 있고, 캐시 미스면 계산까지 얹힌다. 대기열은 두지 않는다 —
# 밀린 요청을 쌓아 두면 늦게라도 전부 처리하느라 사람 요청이 더 오래 굶는다.
_HEAVY_SEM = threading.BoundedSemaphore(2)
_HEAVY_TIMEOUT = 5.0

# 콜드 빌드 재시도 안내 간격(초). 빌드는 수십 초라 더 짧게 권해도 헛폴링만 는다.
_RETRY_AFTER = 5


# ── 인증·응답 변환 ───────────────────────────────────────────────────────────
def _access():
    """(viewer, see_all_private). viewer 는 절대 None 이 되지 않는다."""
    key = os.environ.get("WEB_REPORT_API_KEY", "").strip()
    if key and hmac.compare_digest(request.headers.get("X-Report-Api-Key", "") or "", key):
        return "", True          # 신뢰 호출자 — 비공개 세션 포함
    return "", False             # 기본 — 공개 세션만


def _status_url(session_id):
    return f"{contracts.capabilities()['base_path']}/{session_id}/build-status"


def _respond(result):
    """facade 분기 키 → HTTP. 여기 없는 키는 전부 200 본문으로 나간다."""
    if not isinstance(result, dict):
        return jsonify({"error": "internal", "message": "bad facade result"}), 500

    error = result.get("error")
    if error == "session_not_found":
        # 권한 없음도 같은 응답이다 — 존재 여부를 흘리지 않는다.
        return jsonify({"error": "session_not_found"}), 404
    if error == "item_not_found":
        return jsonify({"error": "item_not_found",
                        "subject": result.get("subject")}), 404
    if error == "not_web_report":
        return jsonify({"error": "not_web_report",
                        "message": "이 세션에는 web_report 계산값이 없다",
                        "source": result.get("source")}), 400
    if error == "bad_request":
        return jsonify({"error": "bad_request",
                        "message": result.get("message") or ""}), 400
    if error:
        return jsonify({"error": str(error)}), 400

    if result.get("building"):
        sid = result.get("session_id") or ""
        if result.get("blocked"):
            # 연속 실패로 차단된 세션 — 재시도해도 같은 실패라 202 로 낚지 않는다.
            return jsonify({"error": "build_failed", "session_id": sid,
                            "message": "리포트 빌드가 반복 실패해 차단된 세션이다"}), 503
        body = {"building": True, "blocked": False, "session_id": sid,
                "status_url": _status_url(sid), "retry_after_sec": _RETRY_AFTER}
        if result.get("kind"):
            body["kind"] = result["kind"]
        return jsonify(body), 202, {"Retry-After": str(_RETRY_AFTER)}

    return jsonify({"schema_version": contracts.SCHEMA_VERSION,
                    "data": result.get("data"),
                    "meta": result.get("meta") or {}})


def _arg(name, default=None):
    value = request.args.get(name)
    return default if value is None or value == "" else value


def _flag(name):
    return str(request.args.get(name) or "").strip().lower() in ("1", "true", "yes")


# ── discovery ────────────────────────────────────────────────────────────────
@web_report_bp.get("/capabilities")
def capabilities():
    """이 API 가 제공하는 함수·입력 스키마·에러 규약 전체. MCP·외부 시스템의 진입점."""
    return jsonify(contracts.capabilities())


# ── 세션 검색 ────────────────────────────────────────────────────────────────
@web_report_bp.get("/sessions")
def list_sessions():
    viewer, see_all = _access()
    return _respond(facade.list_sessions(
        viewer=viewer, see_all_private=see_all,
        product=_arg("product"), product_type=_arg("product_type"),
        lot_id=_arg("lot_id"), q=_arg("q"),
        date_from=_arg("date_from"), date_to=_arg("date_to"),
        limit=_arg("limit", 20), offset=_arg("offset", 0),
        sort=_arg("sort", "new")))


@web_report_bp.get("/compare-sessions")
def compare_sessions():
    viewer, see_all = _access()
    return _respond(facade.compare_sessions(
        viewer=viewer, see_all_private=see_all,
        sids=_arg("sids"), items=_arg("items")))


# ── 세션 단위 조회 ───────────────────────────────────────────────────────────
@web_report_bp.get("/<session_id>/overview")
def overview(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_overview(session_id, viewer=viewer, see_all_private=see_all))


@web_report_bp.get("/<session_id>/build-status")
def build_status(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_build_status(session_id, viewer=viewer,
                                            see_all_private=see_all))


@web_report_bp.get("/<session_id>/yield")
def yield_table(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_yield(session_id, viewer=viewer, see_all_private=see_all,
                                     limit=_arg("limit", 200)))


@web_report_bp.get("/<session_id>/fail-bins")
def fail_bins(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_fail_bins(session_id, viewer=viewer,
                                         see_all_private=see_all,
                                         limit=_arg("limit", 20)))


@web_report_bp.get("/<session_id>/cpk")
def cpk(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_cpk(session_id, viewer=viewer, see_all_private=see_all,
                                   item=_arg("item"), source=_arg("source"),
                                   worst_n=_arg("worst_n", 50), offset=_arg("offset", 0)))


@web_report_bp.get("/<session_id>/issue-table")
def issue_table(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_issue_table(session_id, viewer=viewer,
                                           see_all_private=see_all,
                                           table=_arg("table", "main"),
                                           item=_arg("item"), limit=_arg("limit")))


@web_report_bp.get("/<session_id>/items")
def items(session_id):
    viewer, see_all = _access()
    return _respond(facade.list_items(session_id, viewer=viewer, see_all_private=see_all,
                                      keyword=_arg("keyword"), limit=_arg("limit", 100),
                                      offset=_arg("offset", 0)))


@web_report_bp.get("/<session_id>/items/<path:subject>/stats")
def item_stats(session_id, subject):
    viewer, see_all = _access()
    return _respond(facade.get_item_stats(session_id, subject, viewer=viewer,
                                          see_all_private=see_all))


@web_report_bp.get("/<session_id>/compare")
def compare(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_compare(session_id, viewer=viewer, see_all_private=see_all,
                                       section=_arg("section", "summary"),
                                       limit=_arg("limit", 100)))


@web_report_bp.get("/<session_id>/temperature")
def temperature(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_temperature(session_id, viewer=viewer,
                                           see_all_private=see_all,
                                           limit=_arg("limit", 500)))


@web_report_bp.get("/<session_id>/input-info")
def input_info(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_input_info(session_id, viewer=viewer,
                                          see_all_private=see_all))


@web_report_bp.get("/<session_id>/map")
def map_summary(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_map_summary(session_id, viewer=viewer,
                                           see_all_private=see_all))


@web_report_bp.get("/<session_id>/raw-data/columns")
def raw_data_columns(session_id):
    viewer, see_all = _access()
    return _respond(facade.get_raw_data_columns(session_id, viewer=viewer,
                                                see_all_private=see_all))


@web_report_bp.get("/<session_id>/raw-data")
def raw_data(session_id):
    viewer, see_all = _access()
    with _heavy() as ok:
        if not ok:
            return _busy()
        return _respond(facade.get_raw_data(
            session_id, viewer=viewer, see_all_private=see_all,
            columns=_arg("columns"), search=_arg("search", ""),
            bin_filter=_arg("bin", ""), source_filter=_arg("source", ""),
            limit=_arg("limit", 200), offset=_arg("offset", 0)))


# ── 대용량(gzip 전량) ────────────────────────────────────────────────────────
class _heavy:
    """대용량 라우트용 동시 실행 상한. 못 잡으면 대기열 없이 즉시 실패한다."""

    def __enter__(self):
        self.held = _HEAVY_SEM.acquire(timeout=_HEAVY_TIMEOUT)
        return self.held

    def __exit__(self, *exc):
        if self.held:
            _HEAVY_SEM.release()
        return False


def _schedule(session_id):
    """콜드 → 백그라운드 빌드만 예약하고 202 분기 키를 돌려준다(대기하지 않는다)."""
    from web_report import build_status as bs, compute
    blocked = bs.failure_blocked(session_id, "report")
    if not blocked:
        compute.request_build(session_id, str(Path(REPORT_UPLOAD_DIR)), "report")
    return {"building": True, "blocked": bool(blocked), "session_id": session_id}


def _busy():
    return jsonify({"error": "busy",
                    "message": "대용량 조회 동시 실행 상한 — 잠시 후 재시도"}), 429, \
        {"Retry-After": str(_RETRY_AFTER)}


def _gzip_body(body, etag):
    """gzip bytes → 응답. 미지원 클라이언트에는 서버가 풀어 준다(기존 라우트와 동일)."""
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)
    return Response(body, mimetype="application/json", headers=headers)


def _heavy_session(session_id):
    """(session, err_response). 대용량 경로의 공통 관문 — 캐시를 만지기 **전에** 판정한다.

    캐시 키에는 viewer 가 없다(`web_report/cache_policy.py`). 즉 캐시 계층은 권한을
    막아 주지 않으므로, 여기서 통과시키면 그대로 나간다.
    """
    viewer, see_all = _access()
    session, err = facade._get_session(session_id, viewer=viewer, see_all_private=see_all)
    if err:
        return None, _respond(err)
    return session, None


def _etag(session, variant):
    return (f'W/"{session.get("analysis_key") or ""}-{session.get("content_hash") or ""}'
            f'-{variant}"')


@web_report_bp.get("/<session_id>/items/<path:subject>/values")
def item_values(session_id, subject):
    """항목 1개의 측정값 전량(gzip). 다운샘플하지 않는다(CLAUDE.md 규칙 5)."""
    session, err = _heavy_session(session_id)
    if err:
        return err
    bin1 = _flag("bin1")
    with _heavy() as ok:
        if not ok:
            return _busy()
        from web_report import response_cache, service
        try:
            body = response_cache.get_scatter_gzip(
                session_id, subject, report_db=report_db,
                upload_root=Path(REPORT_UPLOAD_DIR), bin1=bin1, bin1_scope="",
                session=session)
        except KeyError:
            return jsonify({"error": "item_not_found", "subject": subject}), 404
        except service.ColdBuildRequired:
            return _respond(_schedule(session_id))
        except FileNotFoundError:
            return jsonify({"error": "session_not_found"}), 404
    return _gzip_body(body, _etag(session, f"scatter-{subject}-{int(bin1)}"))


@web_report_bp.get("/<session_id>/distribution")
def distribution(session_id):
    """전 항목 ECDF 전량(gzip)."""
    session, err = _heavy_session(session_id)
    if err:
        return err
    bin1 = _flag("bin1")
    from web_report import service
    # get_distribution_gzip 에는 build_if_cold 스위치가 없어(웹 라우트는 대기해도 되는
    # 사용자 클릭이다) 값싼 사전 판정으로 콜드를 걸러 낸다 — 외부 폴러가 요청 스레드를
    # 수십 초씩 물지 못하게 하는 것이 이 API 의 규약이다.
    if service.report_is_cold(session_id, report_db=report_db,
                              upload_root=Path(REPORT_UPLOAD_DIR), session=session):
        return _respond(_schedule(session_id))
    with _heavy() as ok:
        if not ok:
            return _busy()
        try:
            body = service.get_distribution_gzip(
                session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
                bin1=bin1, bin1_scope="")
        except service.ColdBuildRequired:
            return _respond(_schedule(session_id))
        except (FileNotFoundError, KeyError):
            return jsonify({"error": "session_not_found"}), 404
    return _gzip_body(body, _etag(session, f"dist-{int(bin1)}"))


@web_report_bp.get("/<session_id>/map/dies")
def map_dies(session_id):
    """Map die 좌표·bin 전량(gzip)."""
    session, err = _heavy_session(session_id)
    if err:
        return err
    with _heavy() as ok:
        if not ok:
            return _busy()
        from web_report import service
        try:
            # build_if_cold=False — 콜드면 요청 스레드가 수십 초 묶인다.
            body = service.get_map_gzip(session_id, report_db=report_db,
                                        upload_root=Path(REPORT_UPLOAD_DIR),
                                        build_if_cold=False)
        except service.ColdBuildRequired:
            return _respond(_schedule(session_id))
        except (FileNotFoundError, KeyError):
            return jsonify({"error": "session_not_found"}), 404
    return _gzip_body(body, _etag(session, "map"))


@web_report_bp.get("/<session_id>/raw-data/sources/<int:index>.csv")
def raw_csv(session_id, index):
    """source 1개 raw data CSV 스트림 — 타 시스템 벌크 수집용."""
    session, err = _heavy_session(session_id)
    if err:
        return err
    with _heavy() as ok:
        if not ok:
            return _busy()
        from web_report import rawedit
        try:
            chunks, source_name = rawedit.export_source_csv(
                session_id, index, report_db=report_db,
                upload_root=Path(REPORT_UPLOAD_DIR))
        except (IndexError, ValueError):
            return jsonify({"error": "bad_request",
                            "message": f"source index out of range: {index}"}), 400
        except (FileNotFoundError, KeyError):
            return jsonify({"error": "session_not_found"}), 404
        name = rawedit.csv_download_name(session.get("lot_id") or "", source_name)
    # 스트리밍은 세마포어 밖에서 흐른다 — 파일을 열어 스키마를 읽는 데까지가 상한 대상이고,
    # 전송 시간까지 물고 있으면 느린 클라이언트 하나가 슬롯을 오래 잡는다.
    return Response(chunks, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@web_report_bp.get("/<session_id>/raw-data/all.zip")
def raw_zip(session_id):
    """전 source raw data CSV zip 스트림."""
    session, err = _heavy_session(session_id)
    if err:
        return err
    with _heavy() as ok:
        if not ok:
            return _busy()
        from web_report import rawedit
        try:
            chunks, _count = rawedit.export_sources_csv_zip(
                session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
        except IndexError:
            return jsonify({"error": "bad_request", "message": "source 가 없는 세션"}), 400
        except (FileNotFoundError, KeyError):
            return jsonify({"error": "session_not_found"}), 404
    return Response(chunks, mimetype="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{session_id}_rawdata.zip"'})


@web_report_bp.get("/<session_id>/full")
def full_payload(session_id):
    """report payload 전체(gzip) — 웹 화면이 받는 것과 같은 계산 결과.

    extras(주석·Note 등 화면 전용 부속물)는 싣지 않는다: 그 조립은 웹 라우트의 몫이고,
    여기서 흉내 내면 `full_key` 의 extras_digest 가 달라져 캐시만 한 벌 더 생긴다.
    """
    session, err = _heavy_session(session_id)
    if err:
        return err
    with _heavy() as ok:
        if not ok:
            return _busy()
        from web_report import service
        started = time.time()
        try:
            _, report = service.load_webreport(session_id, report_db=report_db,
                                               upload_root=Path(REPORT_UPLOAD_DIR),
                                               session=session, build_if_cold=False)
        except service.ColdBuildRequired:
            return _respond(_schedule(session_id))
        except (FileNotFoundError, KeyError):
            return jsonify({"error": "session_not_found"}), 404
        if time.time() - started > 1.0:
            _log.info("[public-api] full payload %s took %.1fs", session_id,
                      time.time() - started)
    return jsonify({"schema_version": contracts.SCHEMA_VERSION,
                    "data": report,
                    "meta": facade._meta(session)})

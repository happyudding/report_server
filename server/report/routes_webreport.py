"""web_report 프록시 라우트 (Phase 4 분리 — 구 report_routes.py).

/session/<sid>/web_report/* — raw_data 조회/편집, distribution/scatter,
trim_analysis/trim_chart/overrides, commonality, rawdata export/replace,
issue_table etc/comments, summary engr. URL·응답 형태는 분리 전과 동일하다.
"""
import gzip
import json
import logging
import uuid
from pathlib import Path

from flask import Response, abort, jsonify, request

import storage_gateway
from auth_identity import current_user as _current_user
from config import REPORT_UPLOAD_DIR
from database import report_db
from report.report_extension import report_bp
from report.security import (
    _audit,
    _client_meta,
    _editor_guard,
    _require_csrf,
    _require_web_report_session,
)
from web_report import service as web_report_service
from web_report import build_status as web_report_build_status_mod
from web_report import response_cache as web_report_response_cache
from web_report import rawedit as web_report_rawedit

_log = logging.getLogger(__name__)

_MAX_WEBREPORT_SOURCE_BYTES = 512 * 1024 * 1024


@report_bp.get("/session/<session_id>/web_report/build_status")
def web_report_build_status(session_id):
    """콜드 빌드 진행 상태 — 로드 오버레이가 /full 대기 중 폴링한다.

    {"state":"building","stage","elapsed"} 또는 {"state":"idle"}. 레지스트리 dict 조회뿐
    이라 /full 이 워커/락에 묶여 있어도 즉시 응답한다(진척률이 아니라 사실만 준다).
    """
    _require_web_report_session(session_id)
    return jsonify(web_report_build_status_mod.snapshot(session_id))


@report_bp.get("/session/<session_id>/web_report/raw_data/columns")
def web_report_raw_data_columns(session_id):
    """Raw Data 탭 컬럼 선택 UI용: item 메타 + source 목록 + 전체 die 수."""
    _require_web_report_session(session_id)
    try:
        result = web_report_service.get_raw_data_columns(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report raw_data columns failed for session %s", session_id)
        abort(500, "raw_data columns failed")
    return jsonify(result)


@report_bp.get("/session/<session_id>/web_report/raw_data")
def web_report_raw_data(session_id):
    """Raw Data 탭 lazy-load 조회: columns(콤마구분) + search/bin/source 필터."""
    _require_web_report_session(session_id)
    columns = [c for c in (request.args.get("columns") or "").split(",") if c]
    search = request.args.get("search") or ""
    bin_filter = request.args.get("bin") or ""
    source_filter = request.args.get("source") or ""
    try:
        result = web_report_service.query_raw_data(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            columns=columns, search=search, bin_filter=bin_filter, source_filter=source_filter)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report raw_data query failed for session %s", session_id)
        abort(500, "raw_data query failed")
    return jsonify(result)


@report_bp.get("/session/<session_id>/web_report/distribution")
def web_report_distribution(session_id):
    """Distribution ECDF 전량(다운샘플 없음)을 컴팩트 columnar JSON 으로 지연 로드.

    /full 에서 제외된 sheets["Distribution"] 의 대체 — 수십 MB 라 gzip(Accept-Encoding 시)과
    ETag(analysis_key+content_hash) 조건부 응답을 지원한다.
    """
    session = _require_web_report_session(session_id)
    # bin1=1 → 양품(Bin1)만으로 재계산한 ECDF ("Bin1 only"). ETag 에 포함해 전체/양품
    # 변형이 서로의 304 로 오염되지 않게 한다.
    bin1 = (request.args.get("bin1") or "") in ("1", "true", "True")
    variant = "bin1" if bin1 else "all"
    etag = f'"{session.get("analysis_key") or ""}-{session.get("content_hash") or ""}-{variant}"'
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    try:
        # 계산+직렬화+gzip 결과가 service 쪽에서 (analysis_key, content_hash, mode[, bin1]) 키로
        # 캐시됨 — 세션당 변형별 1회만 CPU 를 쓰고 이후 요청은 bytes 반환뿐이라 동시 사용자에도 안전.
        body = web_report_service.get_distribution_gzip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR), bin1=bin1)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report distribution failed for session %s", session_id)
        abort(500, "distribution failed")
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)   # 실사용 브라우저는 전부 gzip — 폴백 경로
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.get("/session/<session_id>/web_report/map_analysis")
def web_report_map_analysis(session_id):
    """Map Analysis die 전량(다운샘플 없음)을 JSON 으로 지연 로드.

    /full 의 sheets["Map Analysis"] 는 dies 를 뺀 경량 메타만 싣는다(schema v8) —
    die 전량(수십 MB 가능)은 여기서 받는다. /distribution 라우트와 동일하게
    gzip(Accept-Encoding 시)과 ETag(analysis_key+content_hash) 조건부 응답 지원.
    """
    session = _require_web_report_session(session_id)
    etag = f'"{session.get("analysis_key") or ""}-{session.get("content_hash") or ""}-map"'
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    try:
        # 계산+직렬화+gzip 결과가 service 쪽에서 (analysis_key, content_hash, mode) 키로
        # 캐시됨 — 세션당 1회만 CPU 를 쓰고 이후 요청은 bytes 반환뿐이라 동시 사용자에도 안전.
        body = web_report_service.get_map_gzip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report map_analysis failed for session %s", session_id)
        abort(500, "map_analysis failed")
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)   # 실사용 브라우저는 전부 gzip — 폴백 경로
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.get("/session/<session_id>/web_report/scatter/<path:subject>")
def web_report_scatter(session_id, subject):
    """Item_detail 용: 항목(subject)의 소스별 전체 측정값+hover metadata(다운샘플 없음) 지연 로드.

    values 배열에 serial/xpos/ypos 가 붙어 페이로드가 커질 수 있어(다운샘플은 여전히 금지),
    /distribution 라우트와 동일하게 gzip(Accept-Encoding 시)과 ETag 조건부 응답을 지원한다.
    계산+직렬화+gzip 결과는 response_cache 가 (analysis_key, content_hash, subject) 키로
    캐시 — 같은 항목 반복 클릭 시 bytes 반환뿐이다."""
    session = _require_web_report_session(session_id)
    subject = (subject or "").strip()
    if not subject or len(subject) > 200:
        abort(400, "invalid subject")
    # bin1=1 → 양품(Bin1)만으로 낸 분포/통계 상세 ("Bin1 only" 상세). ETag 에 포함.
    bin1 = (request.args.get("bin1") or "") in ("1", "true", "True")
    variant = "bin1" if bin1 else "all"
    # subject 는 URL 에, mode 는 세션 불변이라 ETag 는 /distribution 과 동일하게
    # analysis_key+content_hash(+변형) 로 충분 — raw_data 편집 시 content_hash 변경으로 재수신.
    etag = f'"{session.get("analysis_key") or ""}-{session.get("content_hash") or ""}-{variant}"'
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    try:
        body = web_report_response_cache.get_scatter_gzip(
            session_id, subject, session=session,
            report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR), bin1=bin1)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report item or session data not found")
    except Exception:
        _log.exception("web_report scatter failed for session %s item %s", session_id, subject)
        abort(500, "scatter failed")
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)   # 실사용 브라우저는 전부 gzip — 폴백 경로
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.get("/session/<session_id>/web_report/trim_analysis")
def web_report_trim_analysis(session_id):
    """Trim Analysis 탭 payload(항목 매칭 + 그룹 통계/shift) 지연 로드.

    /full 에서 제외된 sheets["Trim Analysis"] 의 대체 — distribution 라우트와 동일하게
    gzip 과 ETag 조건부 응답을 지원한다. ETag 에 편집 rev 토큰이 포함돼
    trim_overrides 편집 직후 stale 304 가 나가지 않는다. ?source= 로 소스 선택("" = 첫 소스).
    """
    session = _require_web_report_session(session_id)
    source = (request.args.get("source") or "").strip()
    if len(source) > 200:
        abort(400, "invalid source")
    try:
        body, mdigest = web_report_service.get_trim_analysis_gzip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            source=source)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report trim_analysis failed for session %s", session_id)
        abort(500, "trim_analysis failed")
    etag = (f'"{session.get("analysis_key") or ""}-{session.get("content_hash") or ""}'
            f'-{mdigest[:16]}"')
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)   # 실사용 브라우저는 전부 gzip — 폴백 경로
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.get("/session/<session_id>/web_report/trim_chart")
def web_report_trim_chart(session_id):
    """Trim 그룹 1개의 chip-to-chip 차트 데이터(전 die, 다운샘플 없음) 지연 로드.

    ?source=&group= 쿼리 사용 — 그룹 id(stem)에 특수문자가 올 수 있어 path 대신 query.
    프런트가 그룹 단위로 병렬(동시 8) fetch 하고 클라 캐시로 재조회를 흡수한다.
    """
    _require_web_report_session(session_id)
    source = (request.args.get("source") or "").strip()
    group = (request.args.get("group") or "").strip()
    if not group or len(group) > 200 or len(source) > 200:
        abort(400, "invalid group or source")
    try:
        body = web_report_service.get_trim_chart_gzip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            source=source, group_id=group)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report trim group or session data not found")
    except Exception:
        _log.exception("web_report trim_chart failed for session %s group %s",
                       session_id, group)
        abort(500, "trim_chart failed")
    headers = {"Vary": "Accept-Encoding"}
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)   # 실사용 브라우저는 전부 gzip — 폴백 경로
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.post("/session/<session_id>/web_report/trim/overrides")
def web_report_trim_overrides(session_id):
    """Trim Analysis 드래그앤드랍 수동 재배치 저장 — 세션 편집 DB 갱신 (parquet 불변).

    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    ops = body.get("ops")
    if not isinstance(ops, list) or not ops:
        return jsonify({"error": "ops가 비어 있습니다."}), 400
    ip, ua = _client_meta()
    try:
        result = web_report_service.update_trim_overrides(
            session_id, ops, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            client_ip=ip, user_agent=ua)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report trim overrides failed for session %s", session_id)
        abort(500, "trim overrides failed")
    return jsonify(result)


@report_bp.get("/session/<session_id>/web_report/commonality/chips")
def web_report_commonality_chips(session_id):
    """Commonality chip 검색: serial/xpos/ypos 개별 칸(AND) 또는 q(OR, dut 포함) 후보 목록 (읽기 전용)."""
    _require_web_report_session(session_id)
    q = request.args.get("q") or ""
    serial = request.args.get("serial") or ""
    xpos = request.args.get("xpos") or ""
    ypos = request.args.get("ypos") or ""
    try:
        limit = min(max(int(request.args.get("limit") or 300), 1), 2000)
    except (TypeError, ValueError):
        limit = 300
    try:
        result = web_report_service.commonality_chips(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            q=q, limit=limit, serial=serial, xpos=xpos, ypos=ypos)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report commonality chips failed for session %s", session_id)
        abort(500, "commonality chips failed")
    return jsonify(result)


@report_bp.get("/session/<session_id>/web_report/commonality/chip")
def web_report_commonality_chip(session_id):
    """선택 chip 의 항목별 값 + 누적%(ECDF 위치) + wafer 좌표 (읽기 전용)."""
    _require_web_report_session(session_id)
    try:
        result = web_report_service.commonality_chip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            serial=request.args.get("serial") or "", xpos=request.args.get("xpos") or "",
            ypos=request.args.get("ypos") or "", source=request.args.get("source") or "")
    except (FileNotFoundError, KeyError):
        abort(404, "chip or session data not found")
    except Exception:
        _log.exception("web_report commonality chip failed for session %s", session_id)
        abort(500, "commonality chip failed")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/raw_data/edit")
def web_report_raw_data_edit(session_id):
    """Raw Data 셀 편집 저장 — 저장된 parquet 원본을 직접 덮어쓴다 (버전관리/undo 없음).

    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    edits = body.get("edits") or []
    if not isinstance(edits, list) or not edits:
        return jsonify({"error": "edits가 비어 있습니다."}), 400
    if len(edits) > 500:
        return jsonify({"error": f"편집 개수가 너무 많습니다 ({len(edits)} > 500)"}), 400
    ip, ua = _client_meta()
    try:
        result = web_report_service.edit_raw_data(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            edits=edits, client_ip=ip, user_agent=ua)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report raw_data edit failed for session %s", session_id)
        abort(500, "raw_data edit failed")
    return jsonify(result)


def _read_webreport_source_files():
    """멀티파트 webreport_0..N parquet 필드를 list[bytes] 로 읽는다 (upload_webreport 패턴)."""
    out = []
    idx = 0
    while True:
        f = request.files.get(f"webreport_{idx}")
        if f is None:
            break
        data = f.read()
        if not data:
            abort(400, f"webreport_{idx} is empty")
        if len(data) > _MAX_WEBREPORT_SOURCE_BYTES:
            abort(413, f"webreport_{idx} payload is too large")
        out.append(data)
        idx += 1
    if not out:
        abort(400, "missing webreport parquet files")
    return out


@report_bp.get("/session/<session_id>/web_report/rawdata_export")
def web_report_rawdata_export(session_id):
    """Honey 클라 Excel 편집용: 세션의 모든 source parquet + manifest 를 zip 으로 내려준다.

    Honey(브라우저 아님)가 GET 으로 받아 Excel 로 연다 — 조회이므로 CSRF 불필요."""
    _require_web_report_session(session_id)
    try:
        blob = web_report_rawedit.export_sources_zip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report rawdata export failed for session %s", session_id)
        abort(500, "rawdata export failed")
    return Response(
        blob, mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="rawdata_{session_id}.zip"'})


@report_bp.post("/session/<session_id>/web_report/rawdata_replace")
def web_report_rawdata_replace(session_id):
    """Honey 가 Excel 편집 후 재인코딩한 parquet 전체를 받아 세션 원본을 덮어쓴다.

    Honey 클라(브라우저 아님)가 호출하므로 CSRF 대신 커스텀 헤더 X-Honey-Agent 를 요구한다
    (커스텀 헤더는 브라우저 폼 CSRF 로 위조 불가 — preflight 가 필요).
    편집은 raw_data/edit 와 동일하게 업로더 또는 위임받은 편집자만 가능하다
    (_editor_guard — Honey 는 HoneyUser UA 로 신원을 보낸다, excel_session._honey_headers)."""
    if request.headers.get("X-Honey-Agent") != "1":
        abort(403, "X-Honey-Agent header required")
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    sources = _read_webreport_source_files()
    ip, ua = _client_meta()
    try:
        result = web_report_rawedit.replace_sources(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            sources_bytes=sources, client_ip=ip, user_agent=ua,
            client_user=_current_user() or "")
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report rawdata replace failed for session %s", session_id)
        abort(500, "rawdata replace failed")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/issue_table/etc")
def web_report_issue_table_etc(session_id):
    """Issue Table ETC 섹션 item 추가/삭제 — 세션 편집 DB 갱신 (Bin/TNO/Distribution
    은 저장하지 않고 조회 시마다 자동으로 다시 채워진다).

    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    action = (body.get("action") or "add").strip()
    item = (body.get("item") or "").strip()
    if not item:
        return jsonify({"error": "item이 비어 있습니다."}), 400
    if action not in ("add", "remove"):
        return jsonify({"error": f"알 수 없는 action: {action}"}), 400
    ip, ua = _client_meta()
    try:
        result = web_report_service.update_issue_etc_items(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            add=item if action == "add" else "", remove=item if action == "remove" else "",
            client_ip=ip, user_agent=ua)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report issue_table etc failed for session %s", session_id)
        abort(500, "issue_table etc failed")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/issue_table/hidden")
def web_report_issue_table_hidden(session_id):
    """Issue Table 행 숨김(삭제)/전체 초기화 — 세션 편집 DB(kind=issue_hidden) 갱신.

    body: {"action": "hide"|"reset_all", "key": "Yield|<bin>"|"CPK|<item>"}.
    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    action = (body.get("action") or "").strip()
    key = (body.get("key") or "").strip()
    ip, ua = _client_meta()
    try:
        result = web_report_service.update_issue_hidden(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            action=action, key=key, client_ip=ip, user_agent=ua)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report issue_table hidden failed for session %s", session_id)
        abort(500, "issue_table hidden failed")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/issue_table/status")
def web_report_issue_table_status(session_id):
    """Issue Table 행 Status(Open/Close) 저장 — 세션 편집 DB(kind=issue_status) 갱신.

    body: {"key": "Yield|<bin>"|"CPK|<item>"|"ETC|<item>", "value": "Open"|"Close"}.
    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    key = (body.get("key") or "").strip()
    value = (body.get("value") or "").strip()
    ip, ua = _client_meta()
    try:
        result = web_report_service.update_issue_status(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            key=key, value=value, client_ip=ip, user_agent=ua)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report issue_table status failed for session %s", session_id)
        abort(500, "issue_table status failed")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/issue_table/comments")
def web_report_issue_table_comments(session_id):
    """Issue Table PTE/개발 comment 저장 — 세션 편집 DB 갱신 (parquet 불변).

    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    comments = body.get("comments")
    if not isinstance(comments, list) or not comments:
        return jsonify({"error": "comments가 비어 있습니다."}), 400
    ip, ua = _client_meta()
    try:
        result = web_report_service.update_issue_comments(
            session_id, comments, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            client_ip=ip, user_agent=ua)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report issue_table comments failed for session %s", session_id)
        abort(500, "issue_table comments failed")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/chart_notes")
def web_report_chart_notes(session_id):
    """차트 주석(도형/텍스트/코멘트) 저장 — 세션 편집 DB(kind=chart_note) 갱신.

    body: {"ops": [{"key": chart_key, "value": {shapes,texts,comment}|null}]}.
    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    ops = body.get("ops")
    if not isinstance(ops, list) or not ops:
        return jsonify({"error": "ops가 비어 있습니다."}), 400
    ip, ua = _client_meta()
    try:
        result = web_report_service.update_chart_notes(
            session_id, ops, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            client_ip=ip, user_agent=ua)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report chart_notes failed for session %s", session_id)
        abort(500, "chart_notes failed")
    return jsonify(result)


@report_bp.get("/session/<session_id>/web_report/note")
def web_report_note_get(session_id):
    """Note 탭 시트 JSON 지연 로드 (최대 2MB — /full 에서 제외). 읽기는 전원 가능."""
    _require_web_report_session(session_id)
    try:
        result = web_report_service.load_note(session_id, report_db=report_db)
    except KeyError:
        abort(404, "session not found")
    except Exception:
        _log.exception("web_report note load failed for session %s", session_id)
        abort(500, "note load failed")
    body = gzip.compress(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=1)
    headers = {"Vary": "Accept-Encoding"}
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.post("/session/<session_id>/web_report/note")
def web_report_note_save(session_id):
    """Note 탭 시트 JSON 저장 (전체 치환) — 세션 편집 DB(kind=note_sheet) 갱신.

    body: {"sheet": {...}, "base": <토큰|null>, "force": bool} — 셀 계산은 전부
    클라이언트(Luckysheet), 서버는 저장만. sheet 는 필수·비어있지 않은 dict —
    본문 손상/빈 payload 가 기존 Note 를 삭제(치환)하는 것을 막기 위해 HTTP 로는
    clear 경로를 제공하지 않는다.

    base 는 클라가 GET 으로 읽었던 시점의 낙관적 잠금 토큰이다. 그 사이 남이 저장했으면
    409 + conflict 메타를 돌려주고, 사용자가 덮어쓰기를 택하면 force 로 재전송한다.
    base 키가 아예 없는 요청(캐시된 구버전 JS)은 종전대로 무검사 저장한다.
    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or "sheet" not in body:
        _log.warning("note save rejected (sheet missing/malformed body) for session %s", session_id)
        return jsonify({"error": "sheet 데이터가 없습니다 — 저장 요청이 손상되었습니다. 다시 시도해주세요."}), 400
    sheet = body["sheet"]
    if not isinstance(sheet, dict) or not sheet:
        _log.warning("note save rejected (empty sheet) for session %s", session_id)
        return jsonify({"error": "빈 Note 는 저장할 수 없습니다."}), 400
    ip, ua = _client_meta()
    try:
        result = web_report_service.save_note(
            session_id, sheet, report_db=report_db,
            upload_root=Path(REPORT_UPLOAD_DIR),
            base=body.get("base"), check=("base" in body), force=bool(body.get("force")),
            client_ip=ip, user_agent=ua)
    except web_report_service.NoteConflict as exc:
        return jsonify({
            "error": "다른 사용자가 먼저 저장했습니다.",
            "conflict": {"updated_by": exc.info.get("updated_by", ""),
                         "updated_at": exc.info.get("updated_at", 0)},
        }), 409
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report note save failed for session %s", session_id)
        abort(500, "note save failed")
    return jsonify(result)


_NOTE_IMAGE_MAX_BYTES = 2 * 1024 * 1024
_NOTE_IMAGE_MAX_COUNT = 200
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


@report_bp.post("/session/<session_id>/web_report/note_image")
def web_report_note_image(session_id):
    """Note 탭 이미지 업로드 (raw body, Content-Type image/png|image/jpeg).

    매직바이트 검증 + 2MB/장·세션당 200장 상한. 저장은 S3(로컬 폴백), 파일명은
    서버가 uuid 로 생성 — 응답 {"image_id","url"} 을 Luckysheet 플로팅 이미지 src 로 쓴다."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    data = request.get_data(cache=False)
    if not data:
        return jsonify({"error": "이미지 데이터가 비어 있습니다."}), 400
    if len(data) > _NOTE_IMAGE_MAX_BYTES:
        return jsonify({"error": f"이미지가 너무 큽니다 (최대 {_NOTE_IMAGE_MAX_BYTES // (1024*1024)}MB)."}), 413
    if data[:8] == _PNG_MAGIC:
        ext = "png"
    elif data[:3] == _JPEG_MAGIC:
        ext = "jpg"
    else:
        return jsonify({"error": "PNG/JPEG 이미지만 업로드할 수 있습니다."}), 400
    try:
        if storage_gateway.count_note_images(session_id) >= _NOTE_IMAGE_MAX_COUNT:
            return jsonify({"error": f"세션당 이미지 상한({_NOTE_IMAGE_MAX_COUNT}장)을 초과했습니다."}), 400
        image_id = f"{uuid.uuid4().hex}.{ext}"
        storage_gateway.save_note_image(session_id, image_id, data)
    except Exception:
        _log.exception("web_report note image save failed for session %s", session_id)
        abort(500, "note image save failed")
    _audit("edit", session=session, changed_fields=f"note_image({image_id})")
    return jsonify({"ok": True, "image_id": image_id,
                    "url": f"/pe/report/note_image/{session_id}/{image_id}"})


@report_bp.post("/session/<session_id>/web_report/summary/engr")
def web_report_summary_engr(session_id):
    """Summary 탭 Engr Comment(Yield/CPK/ETC 3칸) 저장 — 세션 편집 DB 갱신 (parquet 불변).

    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        return jsonify({"error": "values가 비어 있습니다."}), 400
    ip, ua = _client_meta()
    try:
        result = web_report_service.update_summary_engr(
            session_id, values, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            client_ip=ip, user_agent=ua)
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report summary engr failed for session %s", session_id)
        abort(500, "summary engr failed")
    return jsonify(result)

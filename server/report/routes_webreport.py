"""web_report 프록시 라우트 (Phase 4 분리 — 구 report_routes.py).

/session/<sid>/web_report/* — raw_data 조회/편집, distribution/scatter,
trim_analysis/trim_chart/overrides, commonality, rawdata export/replace,
issue_table etc/comments, summary engr. URL·응답 형태는 분리 전과 동일하다.
"""
import gzip
import json
import logging
import re
import uuid
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from urllib.parse import quote

from flask import Response, abort, jsonify, request

import storage_gateway
from auth_identity import current_user as _current_user
from config import REPORT_UPLOAD_DIR
from database import report_db
from report.report_extension import report_bp
from report.routes_session import _building_response
from report.security import (
    _audit,
    _client_meta,
    _editor_guard,
    _require_csrf,
    _require_web_report_session,
    artifact_missing,
    compute_busy,
)
from web_report import compute as web_report_compute
from web_report import service as web_report_service
from web_report import build_status as web_report_build_status_mod
from web_report import eta as web_report_eta
from web_report import response_cache as web_report_response_cache
from web_report import preprocess as web_report_preprocess
from web_report import rawedit as web_report_rawedit

_log = logging.getLogger(__name__)

_MAX_WEBREPORT_SOURCE_BYTES = 512 * 1024 * 1024

# 컴퓨트 워커를 못 잡아 계산이 끊긴 경우 — 세션 데이터의 잘못이 아니라 그 순간 붐볐거나
# 워커가 죽은 것이다. generic except 로 흘려 500 을 주면 프런트가 "재시도하면 되는 상황"
# 임을 알 수 없으므로 503 + Retry-After(compute_busy)로 갈라 준다.
_COMPUTE_BUSY_EXC = (web_report_compute.QueueWaitTimeout, BrokenProcessPool)

# distribution_batch 한 요청의 항목 수 상한 — 프런트는 화면에 보이는 만큼(수십 개)만
# 요청한다. 상한을 두는 이유는 URL 길이/계산량 폭주 차단이며, 초과분은 프런트가 다음
# 배치로 나눠 보낸다.
_DIST_BATCH_MAX = 40

# trim_chart_batch 한 요청의 그룹 수 상한 — 프런트 산포 한 페이지 크기(TRIM.PAGE_SIZE=6)와
# 같은 값. 페이지를 키우면 두 곳을 함께 올려야 한다.
_TRIM_BATCH_MAX = 6


def _prep_tag(session_id):
    """ETag 에 붙일 전처리 digest 조각 (전처리 없으면 빈 문자열).

    dist/map/scatter 응답은 (analysis_key, content_hash, 변형) 만으로 ETag 를 만들었는데,
    전처리는 content_hash 를 바꾸지 않는다(원본 parquet 불변) — 이 조각이 없으면 옵션을
    켜거나 끈 직후 브라우저가 stale 304 를 받아 옛 값을 계속 보게 된다."""
    digest = web_report_preprocess.session_digest(report_db, session_id)
    return f"-{digest}" if digest else ""


@report_bp.get("/session/<session_id>/web_report/build_status")
def web_report_build_status(session_id):
    """콜드 빌드 진행 상태 — 로드 오버레이가 /full 대기 중 폴링한다.

    {"state":"building","stage","elapsed"} 또는 {"state":"idle"}. 레지스트리 dict 조회뿐
    이라 /full 이 워커/락에 묶여 있어도 즉시 응답한다(진척률이 아니라 사실만 준다).

    빌드 중이면 입력 규모 기반 예상초(eta)를 함께 준다 — 규모는 세션별로 캐시되므로
    2초 폴링이 반복돼도 parquet footer 를 다시 읽지 않는다. 모르면 키가 없다.
    """
    session = _require_web_report_session(session_id)
    status = web_report_build_status_mod.snapshot(session_id)
    if status.get("state") == "building":
        eta_sec = web_report_eta.session_eta(session, Path(REPORT_UPLOAD_DIR))
        if eta_sec is not None:
            status["eta"] = eta_sec
    return jsonify(status)


@report_bp.get("/session/<session_id>/web_report/input_info")
def web_report_input_info(session_id):
    """세션 상세 ℹ 모달 — source 별 입력 파일 정보 (manifest 만 읽어 즉시 응답).

    조회 기능이라 읽기 전용 사용자에게도 열려 있다(비공개 세션 차단은
    `_require_web_report_session` 안의 `_private_guard` 가 이미 한다).
    """
    _require_web_report_session(session_id)
    try:
        result = web_report_service.input_info(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report input_info failed for session %s", session_id)
        abort(500, "input_info failed")
    return jsonify(result)


@report_bp.get("/session/<session_id>/web_report/raw_data/columns")
def web_report_raw_data_columns(session_id):
    """Raw Data 탭 컬럼 선택 UI용: item 메타 + source 목록 + 전체 die 수."""
    _require_web_report_session(session_id)
    try:
        result = web_report_service.get_raw_data_columns(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report raw_data query failed for session %s", session_id)
        abort(500, "raw_data query failed")
    return jsonify(result)


# bin1 변형 파서 — bin1_scope 는 화이트리스트로만 받는다("rt" = Temperature RT 소스만
# 양품 필터, CT/HT 는 fail 포함 전체). 값이 유효하지 않으면 400 (조용한 오해석 방지).
_BIN1_SCOPES = ("", "rt")


def _bin1_args():
    bin1 = (request.args.get("bin1") or "") in ("1", "true", "True")
    scope = (request.args.get("bin1_scope") or "").strip().lower()
    if scope not in _BIN1_SCOPES:
        abort(400, "invalid bin1_scope")
    if not bin1:
        scope = ""
    return bin1, scope, ("bin1" + ("-" + scope if scope else "")) if bin1 else "all"


@report_bp.get("/session/<session_id>/web_report/distribution")
def web_report_distribution(session_id):
    """Distribution ECDF 전량(다운샘플 없음)을 컴팩트 columnar JSON 으로 지연 로드.

    /full 에서 제외된 sheets["Distribution"] 의 대체 — 수십 MB 라 gzip(Accept-Encoding 시)과
    ETag(analysis_key+content_hash) 조건부 응답을 지원한다.
    """
    session = _require_web_report_session(session_id)
    # bin1=1 → 양품(Bin1)만으로 재계산한 ECDF ("Bin1 only"). ETag 에 포함해 전체/양품
    # 변형이 서로의 304 로 오염되지 않게 한다.
    bin1, bin1_scope, variant = _bin1_args()
    etag = (f'"{session.get("analysis_key") or ""}-{session.get("content_hash") or ""}'
            f'-{variant}{_prep_tag(session_id)}"')
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    try:
        # 계산+직렬화+gzip 결과가 service 쪽에서 (analysis_key, content_hash, mode[, bin1]) 키로
        # 캐시됨 — 세션당 변형별 1회만 CPU 를 쓰고 이후 요청은 bytes 반환뿐이라 동시 사용자에도 안전.
        body = web_report_service.get_distribution_gzip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            bin1=bin1, bin1_scope=bin1_scope)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except _COMPUTE_BUSY_EXC as exc:
        return compute_busy(session_id, f"distribution: {exc!r}")
    except Exception:
        _log.exception("web_report distribution failed for session %s", session_id)
        abort(500, "distribution failed")
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)   # 실사용 브라우저는 전부 gzip — 폴백 경로
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.get("/session/<session_id>/web_report/distribution_batch")
def web_report_distribution_batch(session_id):
    """항목 배치 ECDF — ``?subjects=a,b,c`` 로 요청한 항목만 컴팩트 columnar JSON 으로 반환.

    Distribution 갤러리/미니셀은 화면에 보이는 항목만 필요한데, 전체 /distribution 은
    대형 세션(수천 항목×수천 die)에서 수천만 포인트라 한 번에 받으면 브라우저가 감당하지
    못한다. 프런트는 IntersectionObserver 로 보이는 항목을 모아 이 라우트를 호출한다.
    항목 상세(전 포인트+hover 메타)는 기존 /scatter/<subject> 그대로다.

    구분자는 콤마 — 항목명에 콤마가 들어갈 수 있으므로 개행(%0A)도 함께 허용한다.
    """
    session = _require_web_report_session(session_id)
    raw = request.args.get("subjects") or ""
    # 정렬+중복제거로 정규화 — 같은 집합을 다른 순서로 요청해도 같은 캐시 키/ETag 가 된다.
    subjects = sorted({s.strip() for s in re.split(r"[,\n]", raw) if s.strip()})
    if not subjects:
        abort(400, "subjects required")
    if len(subjects) > _DIST_BATCH_MAX:
        abort(400, f"too many subjects (max {_DIST_BATCH_MAX})")
    if any(len(s) > 200 for s in subjects):
        abort(400, "invalid subject")
    bin1, bin1_scope, _variant = _bin1_args()
    try:
        etag, body = web_report_response_cache.get_dist_batch_gzip(
            session_id, subjects, session=session, report_db=report_db,
            upload_root=Path(REPORT_UPLOAD_DIR), bin1=bin1, bin1_scope=bin1_scope)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except _COMPUTE_BUSY_EXC as exc:
        return compute_busy(session_id, f"distribution_batch: {exc!r}")
    except Exception:
        _log.exception("web_report distribution_batch failed for session %s", session_id)
        abort(500, "distribution_batch failed")
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)   # 실사용 브라우저는 전부 gzip — 폴백 경로
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.get("/session/<session_id>/web_report/temp_map")
def web_report_temp_map(session_id):
    """Temperature 항목별 fail die **인덱스** 지연 로드 (Map 항목 legend / Temp Map 셀).

    map_analysis 응답에 얹지 않는 이유: 프런트 Worker(wafer_charts.fetchMapViaWorker)가
    dies/metas 외 필드를 버려 도달하지 않는다. 값은 map dies 배열의 인덱스라 이 라우트와
    /map_analysis 는 같은 세대(analysis_key+content_hash+전처리)를 봐야 한다 — ETag 를
    같은 재료로 만든다.
    """
    session = _require_web_report_session(session_id)
    etag = (f'"{session.get("analysis_key") or ""}-{session.get("content_hash") or ""}'
            f'-tempmap{_prep_tag(session_id)}"')
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    try:
        body = web_report_service.get_temp_map_gzip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except _COMPUTE_BUSY_EXC as exc:
        return compute_busy(session_id, f"temp_map: {exc!r}")
    except Exception:
        _log.exception("web_report temp_map failed for session %s", session_id)
        abort(500, "temp_map failed")
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
    etag = (f'"{session.get("analysis_key") or ""}-{session.get("content_hash") or ""}'
            f'-map{_prep_tag(session_id)}"')
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    try:
        # 계산+직렬화+gzip 결과가 service 쪽에서 (analysis_key, content_hash, mode) 키로
        # 캐시됨 — 세션당 1회만 CPU 를 쓰고 이후 요청은 bytes 반환뿐이라 동시 사용자에도 안전.
        # 콜드면 202 + 백그라운드 빌드 — /full 과 같은 규약(요청 스레드 비블록).
        body = web_report_service.get_map_gzip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            build_if_cold=False)
    except web_report_service.ColdBuildRequired:
        return _building_response(session_id, "map")
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except _COMPUTE_BUSY_EXC as exc:
        return compute_busy(session_id, f"map_analysis: {exc!r}")
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
    bin1, bin1_scope, variant = _bin1_args()
    # subject 는 URL 에, mode 는 세션 불변이라 ETag 는 /distribution 과 동일하게
    # analysis_key+content_hash(+변형) 로 충분 — raw_data 편집 시 content_hash 변경으로 재수신.
    etag = (f'"{session.get("analysis_key") or ""}-{session.get("content_hash") or ""}'
            f'-{variant}{_prep_tag(session_id)}"')
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    try:
        body = web_report_response_cache.get_scatter_gzip(
            session_id, subject, session=session,
            report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            bin1=bin1, bin1_scope=bin1_scope)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except _COMPUTE_BUSY_EXC as exc:
        return compute_busy(session_id, f"trim_analysis: {exc!r}")
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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


@report_bp.get("/session/<session_id>/web_report/trim_chart_batch")
def web_report_trim_chart_batch(session_id):
    """Trim 산포 한 페이지(그룹 1~6개) 차트를 한 번에 반환 — `{"charts":[...]}`.

    ?source=&group=A&group=B&group=C — group 은 **반복 파라미터**라 보낸 순서가 그대로
    유지된다(distribution_batch 의 comma+정렬 방식은 순서를 잃어 쓰지 않는다). 그룹당
    요청 1건이던 종전 방식은 요청마다 tables 로드 + 그룹 재도출을 반복했다.
    상한은 프런트 한 페이지 크기(`TRIM.PAGE_SIZE`=6)와 같은 값이다.
    """
    _require_web_report_session(session_id)
    source = (request.args.get("source") or "").strip()
    groups = [g.strip() for g in request.args.getlist("group") if g.strip()]
    if (not groups or len(groups) > _TRIM_BATCH_MAX or len(source) > 200
            or any(len(g) > 200 for g in groups)):
        abort(400, "invalid group(s) or source")
    try:
        body = web_report_service.get_trim_charts_batch(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            source=source, group_ids=groups)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report trim group or session data not found")
    except _COMPUTE_BUSY_EXC as exc:
        return compute_busy(session_id, f"trim_chart_batch: {exc!r}")
    except Exception:
        _log.exception("web_report trim_chart_batch failed for session %s groups %r",
                       session_id, groups)
        abort(500, "trim_chart_batch failed")
    headers = {"Vary": "Accept-Encoding"}
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report raw_data edit failed for session %s", session_id)
        abort(500, "raw_data edit failed")
    return jsonify(result)


@report_bp.get("/session/<session_id>/web_report/preprocess")
def web_report_get_preprocess(session_id):
    """조회 전처리 옵션(항목 제외 / outlier) 현재 값 — Honey 허브 다이얼로그가 그린다.

    DB 만 읽는 값싼 조회라 CSRF/편집자 가드 없이 세션 조회 권한만 요구한다."""
    _require_web_report_session(session_id)
    try:
        result = web_report_service.get_preprocess(session_id, report_db=report_db)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session not found")
    return jsonify(result)


@report_bp.get("/session/<session_id>/web_report/yield_basis")
def web_report_get_yield_basis(session_id):
    """소스별 수율 분모 기준 + 판정에 쓰인 수치 — Honey 허브 [Yield 계산] 탭이 그린다.

    저장은 별도 라우트가 없다 — 허브 [저장] 이 preprocess POST 에 yield_basis 를 실어 보낸다.
    조회 전용이라 preprocess GET 과 같이 세션 조회 권한만 요구한다."""
    _require_web_report_session(session_id)
    try:
        result = web_report_service.get_yield_basis(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report yield_basis load failed for session %s", session_id)
        abort(500, "yield_basis load failed")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/preprocess")
def web_report_save_preprocess(session_id):
    """조회 전처리 옵션 저장 — 원본 parquet 은 그대로 두고 조회 시점에만 적용된다.

    body: {"exclude_items": [...], "outlier": {"mode":"stdev","k":50},
           "edits": [{"source","row_idx","column","value"}], "rules": [...]} — 빈 spec 이면 해제.
    edits(빠른 수정 셀 패치)/rules(조건 일괄 수정)는 **키가 없으면 저장값 유지**다 — 이 두 키를
    모르는 구버전 Honey 허브의 저장이 빠른 수정 결과를 지우지 않게 하기 위함(service 참조).
    같은 허브 다이얼로그가 보내는 수율 분모 기준 {"yield_basis": "gross"|"test"} 도 함께
    받는다(저장은 별도 kind — preprocess digest 를 건드리지 않는다).
    Honey 클라(브라우저 아님)도 호출하므로 rawdata_replace 와 같이 X-Honey-Agent 헤더를
    CSRF 대체로 허용한다. 편집 권한은 다른 편집 채널과 동일(_editor_guard)."""
    if request.headers.get("X-Honey-Agent") != "1":
        _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "본문 형식 오류"}), 400
    ip, ua = _client_meta()
    try:
        result = web_report_service.save_preprocess(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            spec=body, client_ip=ip, user_agent=ua)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report preprocess save failed for session %s", session_id)
        abort(500, "preprocess save failed")
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


def _read_dist_pack_fields():
    """선택 첨부 Distribution pack — upload_webreport._read_dist_pack 과 같은 규약.

    dist_pack_index(form JSON) + dist_pack_chunk_<n>(파일). 최적화용 첨부물이라 크기 초과·
    결손은 요청 실패가 아니라 **pack 전체 건너뛰기**(서버 폴백 계산)다 — 부분 pack 을
    저장하면 조회가 항목을 잃는다. 내용 검증·저장은 ingest.save_client_dist_pack 이 한다."""
    index_text = request.form.get("dist_pack_index")
    if not index_text:
        return None
    chunks = {}
    idx = 0
    total = 0
    while True:
        f = request.files.get(f"dist_pack_chunk_{idx}")
        if f is None:
            break
        data = f.read()
        if not data or len(data) > _MAX_WEBREPORT_SOURCE_BYTES:
            return None
        total += len(data)
        if total > _MAX_WEBREPORT_SOURCE_BYTES:
            return None
        chunks[idx] = data
        idx += 1
    if not chunks:
        return None
    return {"index": index_text, "chunks": chunks}


@report_bp.get("/session/<session_id>/web_report/rawdata_export")
def web_report_rawdata_export(session_id):
    """Honey 클라 Excel 편집용: 세션의 모든 source parquet + manifest 를 zip 으로 내려준다.

    Honey(브라우저 아님)가 GET 으로 받아 Excel 로 연다 — 조회이므로 CSRF 불필요.

    ETag = content_hash. Honey 가 temp 에 받아둔 zip 을 If-None-Match 로 물어보면
    내용이 그대로일 때 **304 + 본문 0바이트**로 끝난다 — 전 소스를 storage 에서 다시
    메모리에 올려 zip 으로 싸는 비용(응답 크기의 수 배 RAM) 자체가 발생하지 않는다.
    """
    session = _require_web_report_session(session_id)
    etag_value = web_report_rawedit.export_etag(session)
    etag = f'"{etag_value}"' if etag_value else ""
    if etag and request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    try:
        blob = web_report_rawedit.export_sources_zip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report rawdata export failed for session %s", session_id)
        abort(500, "rawdata export failed")
    headers = {"Content-Disposition": f'attachment; filename="rawdata_{session_id}.zip"',
               "Cache-Control": "no-cache"}
    if etag:
        headers["ETag"] = etag
    return Response(blob, mimetype="application/zip", headers=headers)


@report_bp.get("/session/<session_id>/web_report/rawdata_csv")
def web_report_rawdata_csv(session_id):
    """웹 브라우저용: 세션 rawdata 원본 source 1개를 7-meta CSV 로 내려준다.

    Honey 나 별도 exe 없이 웹에서 바로 받는 조회 전용 경로다 — 가드는 rawdata_export 와
    같이 _require_web_report_session 하나뿐이고(비공개 세션은 그 안에서 404), 조회이므로
    CSRF·편집자 가드는 없다. 읽기 전용 사용자도 받을 수 있다.

    내려주는 것은 저장된 parquet 그대로다 — 메타 6행(TSEQ~LOLIM) 포함, 전처리·편집 상태
    미반영. ETag 는 rawdata_export 와 같은 content_hash 에 source idx 를 붙인 것.
    """
    session = _require_web_report_session(session_id)
    try:
        source_idx = int(request.args.get("source", "0"))
    except (TypeError, ValueError):
        abort(400, "invalid source index")

    base_etag = web_report_rawedit.export_etag(session)
    etag = f'"{base_etag}:src{source_idx}"' if base_etag else ""
    if etag and request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    try:
        chunks, source_name = web_report_rawedit.export_source_csv(
            session_id, source_idx, report_db=report_db,
            upload_root=Path(REPORT_UPLOAD_DIR))
    except IndexError:
        abort(404, "source not found")
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report rawdata csv failed for session %s", session_id)
        abort(500, "rawdata csv export failed")

    lot = str(session.get("lot_id") or "").strip() or session_id
    pretty = web_report_rawedit.csv_download_name(lot, source_name)
    # filename= 은 ASCII 만 안전하므로 세션/idx 기반 폴백을 두고, 실제 이름(한글 가능)은
    # filename*(RFC5987) 로 준다 — 브라우저는 filename* 를 우선한다.
    ascii_name = f"rawdata_{session_id}_src{source_idx}.csv"
    headers = {
        "Content-Disposition": (f'attachment; filename="{ascii_name}"; '
                                f"filename*=UTF-8''{quote(pretty)}"),
        "Cache-Control": "no-cache",
    }
    if etag:
        headers["ETag"] = etag
    return Response(chunks, mimetype="text/csv; charset=utf-8", headers=headers)


@report_bp.get("/session/<session_id>/web_report/rawdata_csv_all")
def web_report_rawdata_csv_all(session_id):
    """웹 브라우저용: 세션 rawdata 전 source 를 CSV 로 묶은 zip 하나로 내려준다.

    rawdata_csv 를 source 개수만큼 반복하는 대신 한 번에 받는 경로다. 가드·내용 정책은
    rawdata_csv 와 완전히 같다(_require_web_report_session 하나, 조회라 CSRF·편집자
    가드 없음, 저장된 parquet 원형, 전처리·편집 미반영). ETag 도 같은 content_hash 에
    ':all' 을 붙인 것이라 source 별 ':src<idx>' 와 섞이지 않는다.

    zip 은 메모리에 다 만들어 두지 않고 흘려보낸다(export_sources_csv_zip) — 스트리밍
    이라 Content-Length 를 줄 수 없어 브라우저 진행률에 총 크기가 뜨지 않는다.
    Honey 용 rawdata_export(parquet zip)와는 파일명·내용이 모두 다른 별개 경로다.
    """
    session = _require_web_report_session(session_id)

    base_etag = web_report_rawedit.export_etag(session)
    etag = f'"{base_etag}:all"' if base_etag else ""
    if etag and request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    try:
        chunks, _count = web_report_rawedit.export_sources_csv_zip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except IndexError:
        abort(404, "source not found")
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except Exception:
        _log.exception("web_report rawdata csv zip failed for session %s", session_id)
        abort(500, "rawdata csv zip export failed")

    lot = str(session.get("lot_id") or "").strip() or session_id
    pretty = re.sub(r'[\\/:*?"<>|\r\n]+', "_", f"rawdata_{lot}_all.csv.zip")
    ascii_name = f"rawdata_{session_id}_all.csv.zip"
    headers = {
        "Content-Disposition": (f'attachment; filename="{ascii_name}"; '
                                f"filename*=UTF-8''{quote(pretty)}"),
        "Cache-Control": "no-cache",
    }
    if etag:
        headers["ETag"] = etag
    return Response(chunks, mimetype="application/zip", headers=headers)


@report_bp.post("/session/<session_id>/web_report/rawdata_replace")
def web_report_rawdata_replace(session_id):
    """Honey 가 Excel 편집 후 재인코딩한 parquet 전체를 받아 세션 원본을 덮어쓴다.

    Honey 클라(브라우저 아님)가 호출하므로 CSRF 대신 커스텀 헤더 X-Honey-Agent 를 요구한다
    (커스텀 헤더는 브라우저 폼 CSRF 로 위조 불가 — preflight 가 필요).
    편집은 raw_data/edit 와 동일하게 업로더 또는 위임받은 편집자만 가능하다
    (_editor_guard — Honey 는 HoneyUser UA 로 신원을 보낸다, excel_session._honey_headers).

    Excel 에서 시트를 지워 source 가 줄었으면 클라가 form 필드 source_indices 에 남긴
    source 의 원본 idx 배열(JSON, 오름차순)을 함께 보낸다. 없으면 전체 교체(개수 동일).

    선택 필드 dist_pack_index + dist_pack_chunk_<n> — 클라가 재인코딩한 parquet 으로 미리
    만든 Distribution pack. 첨부되면 새 content_hash 로 영구 저장해 서버 콜드 dist 정렬을
    없앤다 (업로드 라우트와 동일 규약)."""
    if request.headers.get("X-Honey-Agent") != "1":
        abort(403, "X-Honey-Agent header required")
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    sources = _read_webreport_source_files()
    dist_pack = _read_dist_pack_fields()
    kept_indices = None
    raw_indices = request.form.get("source_indices")
    if raw_indices:
        try:
            parsed = json.loads(raw_indices)
            kept_indices = [int(i) for i in parsed]
        except (ValueError, TypeError) as exc:
            return jsonify({"error": f"source_indices 형식 오류: {exc}"}), 400
    ip, ua = _client_meta()
    try:
        result = web_report_rawedit.replace_sources(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            sources_bytes=sources, kept_indices=kept_indices, client_ip=ip, user_agent=ua,
            client_user=_current_user() or "", dist_pack=dist_pack)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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
    일괄 삭제는 {"action":"hide", "keys": [...]} 로 보내 편집 DB write 1회·rev +1 로
    처리한다 — 단건을 N회 보내면 rev 가 N 올라 콜드 빌드 유발 지점이 N개가 된다.
    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    action = (body.get("action") or "").strip()
    key = (body.get("key") or "").strip()
    keys = body.get("keys")
    ip, ua = _client_meta()
    try:
        result = web_report_service.update_issue_hidden(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            action=action, key=key, keys=keys, client_ip=ip, user_agent=ua)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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
    일괄 변경(전체/선택 Open·Close)은 {"items": [{"key":..., "value":...}, ...]} 로 보내
    편집 DB write 1회로 처리한다 — 검증 규칙은 단건과 동일.
    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("items")
    key = (body.get("key") or "").strip()
    value = (body.get("value") or "").strip()
    ip, ua = _client_meta()
    try:
        if items is not None:
            result = web_report_service.update_issue_status_bulk(
                session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
                items=items, client_ip=ip, user_agent=ua)
        else:
            result = web_report_service.update_issue_status(
                session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
                key=key, value=value, client_ip=ip, user_agent=ua)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report issue_table status failed for session %s", session_id)
        abort(500, "issue_table status failed")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/issue_table/signature")
def web_report_issue_table_signature(session_id):
    """Issue Table 행의 ENGR 확정 signature 저장 — 세션 편집 DB(kind=issue_signature).

    body: {"key": "Yield|<bin>|<item>"|"CPK|<item>"|"ETC|<item>",
           "signatures": ["LOW_CPK", "UNKNOWN"]}.
    빈 배열이면 확정을 해제해 "미검수 + 엔진 제안" 상태로 되돌린다.
    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    ip, ua = _client_meta()
    try:
        result = web_report_service.update_issue_signature(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            key=(body.get("key") or "").strip(), value=body.get("signatures"),
            client_ip=ip, user_agent=ua)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report issue_table signature failed for session %s", session_id)
        abort(500, "issue_table signature failed")
    return jsonify(result)


_SIG_ID_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
_SIG_REASON_MAX_IDS = 9


@report_bp.get("/session/<session_id>/web_report/issue_table/signature_reason")
def web_report_issue_table_signature_reason(session_id):
    """Signature 판정 근거 — 룰 기준(조건식·임계값) + 업로드 시점 스냅샷 실측값.

    query: key=<row_key>, ids=<SIG_A,SIG_B>(생략 시 스냅샷 발화 목록).
    **조회 전용**이라 CSRF·편집자 가드가 아니라 세션 조회 권한(_require_web_report_session
    안의 비공개 가드)만 요구한다 — 판정 근거는 그 판정을 검토하는 사람이 봐야 하고,
    노출되는 지표(cpk/yield/outlier_ratio)는 같은 세션의 Cpk·Distribution 탭에 이미 있다.
    """
    session = _require_web_report_session(session_id)
    key = (request.args.get("key") or "").strip()
    if not key or len(key) > 300:
        return jsonify({"error": "invalid key"}), 400
    ids = [s.strip().upper() for s in (request.args.get("ids") or "").split(",") if s.strip()]
    if len(ids) > _SIG_REASON_MAX_IDS or any(not _SIG_ID_RE.match(s) for s in ids):
        return jsonify({"error": "invalid ids"}), 400
    # 엔진 경로·eval DB 의존을 모듈 로드 시점으로 끌어오지 않는다 — 이 파일은 모든 세션
    # 조회가 지나가는 진입 모듈이다.
    from eval_panel import signature_reason
    try:
        result = signature_reason.build(
            session_id, key, ids,
            product_type=str(session.get("product_type") or ""),
            family_product=str(session.get("family_product") or ""),
            preprocessed=bool(web_report_preprocess.session_digest(report_db, session_id)))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("signature_reason failed for session %s", session_id)
        abort(500, "signature reason failed")
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report chart_notes failed for session %s", session_id)
        abort(500, "chart_notes failed")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/compare_notes")
def web_report_compare_notes(session_id):
    """Compare 탭 행 코멘트 저장 — 세션 편집 DB(kind=compare_note) 갱신.

    body: {"ops": [{"key": row_key, "value": "텍스트"|null}]}.
    row_key 는 "gl:<after>"+U+001F+"<before>"(Log 비교) / "bm:<x>,<y>"(동일 좌표 Bin 비교).
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
        result = web_report_service.update_compare_notes(
            session_id, ops, report_db=report_db, client_ip=ip, user_agent=ua)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report compare_notes failed for session %s", session_id)
        abort(500, "compare_notes failed")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/note_tags")
def web_report_note_tags(session_id):
    """앵커 태그 생성/삭제 — 세션 편집 DB(kind=note_tag) 갱신.

    body: {"action": "set"|"delete", "name": str,
           "target": {"tab":"note","sheet":str,"sheet_name":str,"r":int,"c":int}}.
    IssueTable comment 의 #[태그명] 이 이 태그를 가리켜 Note 특정 셀로 점프한다.
    편집은 업로더 또는 위임받은 편집자만 가능하다 (CSRF + _editor_guard)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
    denied = _editor_guard(session)
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    action = (body.get("action") or "").strip()
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "태그 이름이 비어 있습니다."}), 400
    ip, ua = _client_meta()
    try:
        result = web_report_service.update_note_tag(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR),
            action=action, name=name, target=body.get("target"),
            client_ip=ip, user_agent=ua)
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report note_tags failed for session %s", session_id)
        abort(500, "note_tags failed")
    return jsonify(result)


@report_bp.get("/session/<session_id>/web_report/note")
def web_report_note_get(session_id):
    """Note 탭 시트 JSON 지연 로드 (최대 10MB — /full 에서 제외). 읽기는 전원 가능."""
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


@report_bp.get("/session/<session_id>/web_report/note/sheet_names")
def web_report_note_sheet_names(session_id):
    """Note 시트 **이름 목록만** — [{"index","name","order"}]. 읽기는 전원 가능.

    Summary 탭의 $[시트명] 자동완성·시트 버튼 줄 전용이다. 위 lazy GET 은 본문까지
    내려주므로(최대 10MB) 이름만 필요한 화면이 그걸 부르면 안 된다."""
    _require_web_report_session(session_id)
    try:
        sheets = web_report_service.get_note_sheet_names(session_id, report_db=report_db)
    except KeyError:
        abort(404, "session not found")
    except Exception:
        _log.exception("web_report note sheet_names failed for session %s", session_id)
        abort(500, "note sheet_names failed")
    return jsonify({"sheets": sheets})


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
        # 충돌은 감사 로그에만 남긴다 — 남지 않으면 동시편집이 실제로 얼마나 일어나는지
        # 운영자가 알 방법이 없다(사용자는 모달에서 덮어쓰기/새로고침만 고르고 끝).
        try:
            report_db.log_audit(
                "note_conflict", session_id=session_id,
                analysis_key=session.get("analysis_key"),
                product_type=session.get("product_type"), product=session.get("product"),
                lot_id=session.get("lot_id"), file_name=session.get("file_name"),
                changed_fields=f"note_sheet(선점자={exc.info.get('updated_by', '')})",
                client_ip=ip, user_agent=ua, result="conflict")
        except Exception:
            pass
        return jsonify({
            "error": "다른 사용자가 먼저 저장했습니다.",
            "conflict": {"updated_by": exc.info.get("updated_by", ""),
                         "updated_at": exc.info.get("updated_at", 0)},
        }), 409
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
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
    except FileNotFoundError as exc:
        return artifact_missing(session_id, str(exc))
    except KeyError:
        abort(404, "web_report session data not found")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        _log.exception("web_report summary engr failed for session %s", session_id)
        abort(500, "summary engr failed")
    return jsonify(result)

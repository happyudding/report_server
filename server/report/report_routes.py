import gzip
import json
import logging
import re
import secrets
import sqlite3
import sys
from pathlib import Path

from flask import Response, abort, jsonify, make_response, request, send_file

from database import report_db
from report_utils import to_float as _to_float, to_int as _to_int
import storage_gateway
from config import (
    REPORT_ANALYSIS_INDEX_HTML,
    REPORT_UPLOAD_DIR,
    REPORT_VIEW_HTML,
    STDINFO_DB_PATH,
)

_log = logging.getLogger(__name__)
from report.report_extension import report_bp

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from web_report import service as web_report_service

_ANALYSIS_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _validate_analysis_key(value):
    if not value or not _ANALYSIS_KEY_RE.match(value):
        abort(400, "invalid analysis_key")


def _validate_session_id(value):
    if not value or not _SESSION_ID_RE.match(value):
        abort(400, "invalid session_id")


# ── CSRF (double-submit cookie) ───────────────────────────────────────────────
# 쿠키 기반 세션 인증이 없고 PIN 을 본문으로 보내는 구조라, 표준 stateless 방어인
# double-submit 쿠키 패턴을 쓴다: GET(/, /view)에서 JS 가 읽을 수 있는 토큰 쿠키를
# 발급하고, 변경요청(PATCH/DELETE/POST)은 같은 토큰을 X-CSRF-Token 헤더로 되돌려
# 보낸다. 교차출처 공격자는 동일출처 정책 때문에 쿠키를 읽거나 커스텀 헤더를 위조할
# 수 없다. 단, Honey 클라이언트가 호출하는 /upload_xlsx 는 브라우저가 아니므로 제외.
_CSRF_COOKIE = "report_csrf"
_CSRF_HEADER = "X-CSRF-Token"


def _issue_csrf_cookie(resp):
    """토큰 쿠키가 없으면 새로 발급. JS 가 읽어야 하므로 httponly=False."""
    if not request.cookies.get(_CSRF_COOKIE):
        resp.set_cookie(
            _CSRF_COOKIE, secrets.token_urlsafe(32),
            max_age=86400, samesite="Strict",
            secure=request.is_secure, httponly=False, path="/",
        )
    return resp


def _require_csrf():
    """변경요청에서 헤더 토큰이 쿠키와 일치하는지 검증. 불일치 시 403."""
    cookie = request.cookies.get(_CSRF_COOKIE) or ""
    header = request.headers.get(_CSRF_HEADER) or ""
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        abort(403, "CSRF token missing or invalid")


def _public_session(session):
    """password 같은 민감 컬럼을 제거하고 has_password 플래그만 노출."""
    if not session:
        return session
    pub = dict(session)
    pub["has_password"] = bool(pub.get("password"))
    pub.pop("password", None)
    return pub


def _password_ok(session, password):
    """세션에 비밀번호가 설정돼 있으면 일치해야 True. 없으면 항상 True (legacy)."""
    stored = (session or {}).get("password")
    if not stored:
        return True
    return (password or "").strip() == stored


def _client_meta():
    """감사 로그용 (client_ip, user_agent). 역프록시 뒤면 X-Forwarded-For 첫 IP 사용."""
    fwd = request.headers.get("X-Forwarded-For")
    ip = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
    return ip, str(request.user_agent)


def _audit(action, session=None, session_id=None, changed_fields=None, result="ok"):
    """감사 로그 best-effort 기록 — 실패해도 본 요청 처리를 깨뜨리지 않는다."""
    try:
        ip, ua = _client_meta()
        meta = session or {}
        report_db.log_audit(
            action,
            session_id=session_id or meta.get("session_id"),
            analysis_key=meta.get("analysis_key"),
            product_type=meta.get("product_type"),
            product=meta.get("product"),
            lot_id=meta.get("lot_id"),
            file_name=meta.get("file_name"),
            changed_fields=changed_fields,
            client_ip=ip,
            user_agent=ua,
            result=result,
        )
    except Exception:
        pass


def _coerce_yield_row(row):
    """수정 모드에서 넘어온 yield 행 dict 를 summary 컬럼 타입으로 정리."""
    name = str(row.get("item_name") or "").strip()
    unit = row.get("unit")
    return {
        "item_name": name,
        "bin_number": _to_int(row.get("bin_number")),
        "yield_percent": _to_float(row.get("yield_percent")),
        "fail_count": _to_int(row.get("fail_count")),
        "cpk_val": _to_float(row.get("cpk_val")),
        "mean_val": _to_float(row.get("mean_val")),
        "stdev_val": _to_float(row.get("stdev_val")),
        "lsl": _to_float(row.get("lsl")),
        "usl": _to_float(row.get("usl")),
        "unit": (str(unit).strip() if unit not in (None, "") else None),
    }


# ── session ─────────────────────────────────────────────────────────────────

@report_bp.get("/result/<session_id>")
def result(session_id):
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    analysis_key = session.get("analysis_key")
    summary = report_db.get_summary_by_analysis_key(analysis_key) if analysis_key else []
    return jsonify({
        "session_id": session_id,
        "analysis_key": analysis_key,
        "status": session.get("status"),
        "file_name": session.get("file_name"),
        "error_message": session.get("error_message"),
        "summary": summary,
    })


@report_bp.get("/session/<session_id>")
def session_info(session_id):
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    return jsonify(_public_session(session))


@report_bp.get("/session/<session_id>/full")
def session_full(session_id):
    """세션 완전 복원에 필요한 모든 참조 반환."""
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    akey = session.get("analysis_key")
    objects = {}
    if akey:
        for obj in report_db.get_all_object_infos(akey):
            objects[obj["object_type"]] = {
                "s3_uri": obj["s3_uri"],
                "s3_key": obj["s3_key"],
            }

    if session.get("source") == "web_report":
        # web_report 세션: parquet 원본에서 재계산 (decoded tables 는 service 의 LRU 캐시 활용).
        # 대용량 Distribution ECDF 는 제외하고 distribution_deferred=True 로 내려보낸다 —
        # 프런트가 GET .../web_report/distribution 으로 백그라운드 지연 로드.
        try:
            _, report = web_report_service.load_webreport(
                session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
        except FileNotFoundError:
            abort(404, "web_report session data not found")
        except KeyError:
            abort(404, "session not found")
        except Exception as exc:
            _log.exception("web_report recompute failed for session %s", session_id)
            abort(500, f"web_report recompute failed: {exc}")
        sheets = report.get("sheets", {})
        summary_text = sheets.get("Summary")
        yield_text = sheets.get("Yield")
        issue_table_text = sheets.get("Issue Table")
        web_report_payload = report
    else:
        # sheet_data: DB 우선. DB 에 없으면 S3 폴백(구형 세션 하위호환).
        sheet_data = report_db.get_all_sheet_data(akey) if akey else {}
        summary_text = sheet_data.get("summary") or _load_json_object(objects, "summary_text")
        yield_text = sheet_data.get("yield") or _load_json_object(objects, "yield_text")
        issue_table_text = sheet_data.get("issue_table") or _load_json_object(objects, "issue_table_text")
        web_report_payload = sheet_data.get("web_report")
    charts = []
    if "chart_index" in objects:
        manifest = _load_json_object(objects, "chart_index")
        count = int((manifest or {}).get("count", 0))
        charts = [{"index": i, "url": f"/pe/report/chart/{session_id}/{i}"}
                  for i in range(count)]
    # Issue_table 행별 분포 이미지. 저장소(S3 또는 로컬 폴백)에서 행 인덱스를 조회.
    issue_images = []
    if akey:
        try:
            for row in storage_gateway.list_issue_image_rows(akey):
                issue_images.append({"row": int(row),
                                     "url": f"/pe/report/issue_image/{session_id}/{int(row)}"})
        except Exception:
            issue_images = []
    # Distribution 합성 PNG: S3 오브젝트 또는 로컬 폴백 파일이 있으면 프록시 URL 반환.
    distribution_url = None
    if "distribution_combined" in objects:
        distribution_url = f"/pe/report/distribution_combined/{session_id}"
    elif akey:
        if (Path(REPORT_UPLOAD_DIR) / "dist_combined" / f"{akey}.png").exists():
            distribution_url = f"/pe/report/distribution_combined/{session_id}"
    return jsonify({
        "session": _public_session(session),
        "summary": report_db.get_summary_by_analysis_key(akey) if akey else [],
        "summary_text": summary_text,
        "yield_text": yield_text,
        "issue_table_text": issue_table_text,
        "web_report": web_report_payload,
        "charts": charts,
        "issue_images": issue_images,
        "distribution_url": distribution_url,
        "csv_files": report_db.get_csv_files(akey) if akey else [],
        "objects": objects,
        "annotations": report_db.get_annotations(session_id),
    })


def _require_web_report_session(session_id):
    """session 조회 + web_report 세션인지 확인. 아니면 404."""
    _validate_session_id(session_id)
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    if session.get("source") != "web_report":
        abort(404, "not a web_report session")
    return session


@report_bp.get("/session/<session_id>/web_report/raw_data/columns")
def web_report_raw_data_columns(session_id):
    """Raw Data 탭 컬럼 선택 UI용: item 메타 + source 목록 + 전체 die 수."""
    _require_web_report_session(session_id)
    try:
        result = web_report_service.get_raw_data_columns(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except Exception as exc:
        _log.exception("web_report raw_data columns failed for session %s", session_id)
        abort(500, f"raw_data columns failed: {exc}")
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
    except Exception as exc:
        _log.exception("web_report raw_data query failed for session %s", session_id)
        abort(500, f"raw_data query failed: {exc}")
    return jsonify(result)


@report_bp.get("/session/<session_id>/web_report/distribution")
def web_report_distribution(session_id):
    """Distribution ECDF 전량(다운샘플 없음)을 컴팩트 columnar JSON 으로 지연 로드.

    /full 에서 제외된 sheets["Distribution"] 의 대체 — 수십 MB 라 gzip(Accept-Encoding 시)과
    ETag(analysis_key+content_hash) 조건부 응답을 지원한다.
    """
    session = _require_web_report_session(session_id)
    etag = f'"{session.get("analysis_key") or ""}-{session.get("content_hash") or ""}"'
    headers = {"Vary": "Accept-Encoding", "ETag": etag}
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers=headers)
    try:
        # 계산+직렬화+gzip 결과가 service 쪽에서 (analysis_key, content_hash) 키로 캐시됨 —
        # 세션당 1회만 CPU 를 쓰고 이후 요청은 bytes 반환뿐이라 동시 사용자에도 안전.
        body = web_report_service.get_distribution_gzip(
            session_id, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except (FileNotFoundError, KeyError):
        abort(404, "web_report session data not found")
    except Exception as exc:
        _log.exception("web_report distribution failed for session %s", session_id)
        abort(500, f"distribution failed: {exc}")
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        headers["Content-Encoding"] = "gzip"
    else:
        body = gzip.decompress(body)   # 실사용 브라우저는 전부 gzip — 폴백 경로
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.get("/session/<session_id>/web_report/scatter/<path:subject>")
def web_report_scatter(session_id, subject):
    """Item_detail 용: 항목(subject)의 소스별 전체 측정값+hover metadata(다운샘플 없음) 지연 로드.

    values 배열에 serial/xpos/ypos 가 붙어 페이로드가 커질 수 있어(다운샘플은 여전히 금지),
    /distribution 라우트와 동일하게 gzip(Accept-Encoding 시)을 지원한다."""
    _require_web_report_session(session_id)
    subject = (subject or "").strip()
    if not subject or len(subject) > 200:
        abort(400, "invalid subject")
    try:
        result = web_report_service.scatter_item(
            session_id, subject, report_db=report_db, upload_root=Path(REPORT_UPLOAD_DIR))
    except (FileNotFoundError, KeyError):
        abort(404, "web_report item or session data not found")
    except Exception as exc:
        _log.exception("web_report scatter failed for session %s item %s", session_id, subject)
        abort(500, f"scatter failed: {exc}")
    body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Vary": "Accept-Encoding"}
    if "gzip" in (request.headers.get("Accept-Encoding") or ""):
        body = gzip.compress(body, compresslevel=1)
        headers["Content-Encoding"] = "gzip"
    return Response(body, mimetype="application/json", headers=headers)


@report_bp.post("/session/<session_id>/web_report/raw_data/edit")
def web_report_raw_data_edit(session_id):
    """Raw Data 셀 편집 저장 — 저장된 parquet 원본을 직접 덮어쓴다 (버전관리/undo 없음).

    편집은 PIN 없이 누구나 가능하다 (CSRF 토큰만 검증)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
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
    except Exception as exc:
        _log.exception("web_report raw_data edit failed for session %s", session_id)
        abort(500, f"raw_data edit failed: {exc}")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/issue_table/etc")
def web_report_issue_table_etc(session_id):
    """Issue Table ETC 섹션 item 추가/삭제 — manifest.etc_items 갱신 (Bin/TNO/Distribution
    은 저장하지 않고 조회 시마다 자동으로 다시 채워진다).

    편집은 PIN 없이 누구나 가능하다 (CSRF 토큰만 검증)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
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
    except Exception as exc:
        _log.exception("web_report issue_table etc failed for session %s", session_id)
        abort(500, f"issue_table etc failed: {exc}")
    return jsonify(result)


@report_bp.post("/session/<session_id>/web_report/issue_table/comments")
def web_report_issue_table_comments(session_id):
    """Issue Table PTE/개발 comment 저장 — manifest.issue_comments 갱신 (parquet 불변).

    편집은 PIN 없이 누구나 가능하다 (CSRF 토큰만 검증)."""
    _require_csrf()
    session = _require_web_report_session(session_id)
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
    except Exception as exc:
        _log.exception("web_report issue_table comments failed for session %s", session_id)
        abort(500, f"issue_table comments failed: {exc}")
    return jsonify(result)


def _load_json_object(objects, object_type):
    """objects 인덱스에 object_type 이 있으면 S3 JSON 다운로드, 실패 시 None."""
    return storage_gateway.load_json_object(objects, object_type)


@report_bp.delete("/session/<session_id>")
def delete_session_route(session_id):
    _require_csrf()
    _validate_session_id(session_id)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    if not _password_ok(session, password):
        return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 403
    report_db.delete_session(session_id)
    _audit("delete", session=session)
    return jsonify({"deleted": True, "session_id": session_id})


@report_bp.post("/session/<session_id>/verify_password")
def verify_session_password(session_id):
    """수정/삭제 진입 전 PIN 확인. 비밀번호 미설정 세션은 ok=True."""
    _require_csrf()
    _validate_session_id(session_id)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    if not _password_ok(session, password):
        return jsonify({"ok": False, "error": "비밀번호가 일치하지 않습니다."}), 403
    return jsonify({"ok": True, "has_password": bool(session.get("password"))})


@report_bp.patch("/session/<session_id>/content")
def update_session_content(session_id):
    """수정 모드 저장: 텍스트 콘텐츠(summary_text / issue_rows / yield_rows) 치환.

    summary_text, issue_rows 는 S3 JSON 으로 다시 업로드하고, yield_rows 는
    report_analysis_summary 를 통째로 치환한다. analysis_key 는 재계산하지 않는다
    (원본 업로드 식별자로 유지)."""
    # 세션 수정 기능 비활성화(2026-07-08 사용자 요청). 검색결과 페이지의 '수정' 버튼 제거와
    # 함께 이 저장 라우트도 차단한다. 삭제(delete)·조회·verify_password 는 그대로 유지.
    return jsonify({"error": "세션 수정 기능이 비활성화되었습니다."}), 405
    _require_csrf()
    _validate_session_id(session_id)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    session = report_db.get_session(session_id)
    if not session:
        abort(404, "session not found")
    if not _password_ok(session, password):
        return jsonify({"error": "비밀번호가 일치하지 않습니다."}), 403

    akey = session.get("analysis_key")
    if not akey:
        return jsonify({"error": "이 세션에는 analysis_key 가 없어 수정할 수 없습니다."}), 400
    if session.get("source") == "web_report":
        return jsonify({"error": "web_report 세션은 현재 수정을 지원하지 않습니다."}), 400

    updated = {}
    errors = {}

    # yield_rows → DB (S3 미설정과 무관하게 저장 가능)
    if body.get("yield_rows") is not None:
        try:
            rows = [_coerce_yield_row(r) for r in (body.get("yield_rows") or [])]
            rows = [r for r in rows if r["item_name"]]
            report_db.replace_summary_batch(akey, session_id, rows)
            updated["yield_rows"] = len(rows)
        except Exception as exc:
            errors["yield_rows"] = str(exc)

    # summary_text → DB 갱신 (S3 는 있으면 추가 저장)
    if body.get("summary_text") is not None:
        try:
            report_db.upsert_sheet_data(akey, "summary", body["summary_text"])
            updated["summary_text"] = True
        except Exception as exc:
            errors["summary_text"] = str(exc)
        else:
            try:
                _write_text_object(akey, session, "summary_text", body["summary_text"])
            except Exception:
                pass

    # yield_text → DB 갱신
    if body.get("yield_text") is not None:
        try:
            report_db.upsert_sheet_data(akey, "yield", body["yield_text"])
            updated["yield_text"] = True
        except Exception as exc:
            errors["yield_text"] = str(exc)
        else:
            try:
                _write_text_object(akey, session, "yield_text", body["yield_text"])
            except Exception:
                pass

    # issue_table_text → DB 갱신
    issue_payload = body.get("issue_table_text")
    if issue_payload is None:
        issue_payload = body.get("issue_rows")
    if issue_payload is not None:
        try:
            report_db.upsert_sheet_data(akey, "issue_table", issue_payload)
            updated["issue_table_text"] = True
        except Exception as exc:
            errors["issue_table_text"] = str(exc)
        else:
            try:
                _write_text_object(akey, session, "issue_table_text", issue_payload)
            except Exception:
                pass

    status = 200 if not errors else (207 if updated else 500)
    if updated:
        _audit("edit", session=session,
               changed_fields=",".join(sorted(updated.keys())),
               result="ok" if not errors else "fail")
    return jsonify({"ok": not errors, "updated": updated, "errors": errors}), status


def _write_text_object(analysis_key, session, object_type, data):
    """텍스트 콘텐츠 JSON 을 S3 에 다시 올리고 report_object_info 를 갱신.
    content_hash / options_json 은 기존 행 값을 유지(없으면 세션 값/빈 객체로 폴백)."""
    storage_gateway.save_text_object(analysis_key, session, object_type, data)


# ── annotations ───────────────────────────────────────────────────────────────

@report_bp.post("/annotation")
def create_annotation():
    _require_csrf()
    body = request.get_json(force=True, silent=True) or {}
    session_id = body.get("session_id", "")
    _validate_session_id(session_id)
    analysis_key = body.get("analysis_key")
    target = (body.get("target") or "").strip()
    content = (body.get("content") or "").strip()
    if not target or not content:
        abort(400, "target and content are required")
    ann_id = report_db.create_annotation(session_id, analysis_key, target, content)
    return jsonify({"id": ann_id, "session_id": session_id, "target": target}), 201


@report_bp.get("/annotation/<session_id>")
def list_annotations(session_id):
    _validate_session_id(session_id)
    return jsonify(report_db.get_annotations(session_id))


@report_bp.patch("/annotation/<int:aid>")
def update_annotation(aid):
    _require_csrf()
    body = request.get_json(force=True, silent=True) or {}
    content = (body.get("content") or "").strip()
    if not content:
        abort(400, "content is required")
    report_db.update_annotation(aid, content)
    return jsonify({"id": aid, "updated": True})


@report_bp.delete("/annotation/<int:aid>")
def delete_annotation(aid):
    _require_csrf()
    report_db.delete_annotation(aid)
    return jsonify({"id": aid, "deleted": True})


# ── Report Analysis index / view pages ───────────────────────────────────────

@report_bp.get("/")
def index_page():
    return _issue_csrf_cookie(make_response(send_file(REPORT_ANALYSIS_INDEX_HTML)))


@report_bp.get("/view/<session_id>")
def view_page(session_id):
    _validate_session_id(session_id)
    return _issue_csrf_cookie(make_response(send_file(REPORT_VIEW_HTML)))


# ── Vendored 정적 자산 (Tabulator 등) ─────────────────────────────────────────
# report_view.html 이 send_file 로 통째 전송되고 정적 폴더 라우트가 없으므로, vendoring 한
# JS/CSS 를 화이트리스트로만 서빙(경로 traversal 차단). CDN/인터넷 불필요(폐쇄망 대응).
_VENDOR_DIR = REPORT_VIEW_HTML.parent / "vendor"
_VENDOR_MIME = {
    "tabulator.min.js": "application/javascript",
    "tabulator.min.css": "text/css",
    "plotly.min.js": "application/javascript",
    "pretendard/PretendardVariable.woff2": "font/woff2",
}


@report_bp.get("/vendor/<path:filename>")
def vendor_asset(filename):
    mime = _VENDOR_MIME.get(filename)
    if not mime:
        abort(404)
    return send_file(_VENDOR_DIR / filename, mimetype=mime)


@report_bp.get("/api/history")
def history():
    product_type = request.args.get("product_type") or None
    process = request.args.get("process") or None
    product = request.args.get("product") or None
    revision = request.args.get("revision") or None
    lot_id = request.args.get("lot_id") or None
    source = request.args.get("source") or None
    rows = report_db.get_history(
        product_type=product_type,
        process=process,
        product=product,
        revision=revision,
        lot_id=lot_id,
        source=source,
    )
    return jsonify(rows)


@report_bp.get("/api/part_ids")
def part_ids():
    """stdinfo DB 의 products.part_id 전체 목록. 업로드 다이얼로그 Product 검색용.

    DB 없음/조회 실패는 best-effort 로 빈 리스트 반환(500 안 냄). 서버 로그에만 경고.
    """
    ids = []
    try:
        con = sqlite3.connect(f"file:{STDINFO_DB_PATH}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT part_id FROM products ORDER BY part_id").fetchall()
        finally:
            con.close()
        ids = [r[0] for r in rows if r[0]]
    except Exception as exc:  # noqa: BLE001
        _log.warning("part_ids 조회 실패 (%s): %s", STDINFO_DB_PATH, exc)
    return jsonify({"part_ids": ids})


# ── debug helpers ─────────────────────────────────────────────────────────────

@report_bp.get("/_threads")
def debug_threads():
    """모든 스레드의 stack trace 덤프. hang 진단용."""
    import sys, threading, traceback
    out = []
    tid_to_name = {t.ident: t.name for t in threading.enumerate()}
    for tid, frame in sys._current_frames().items():
        name = tid_to_name.get(tid, "?")
        out.append(f"=== Thread {tid} ({name}) ===")
        out.append("".join(traceback.format_stack(frame)))
    from flask import Response
    return Response("\n".join(out), mimetype="text/plain; charset=utf-8")

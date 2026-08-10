"""Honey client update channel.

- GET /honey/version: return releases/version.json as-is.
- GET /honey/download: serve the release ZIP named by version.json.file.
- GET /honey/announcement: return releases/announcement.txt as-is (plain text).
"""
import json

from flask import Blueprint, Response, abort, jsonify, request, send_file

from auth_identity import current_user
from config import HONEY_ANNOUNCEMENT_TXT, HONEY_RELEASES_DIR, HONEY_VERSION_JSON
from database import report_db

honey_bp = Blueprint("honey", __name__, url_prefix="/honey")


def _record_run():
    """Honey 실행 집계 (best-effort) — 버전체크는 앱 시작 시 1회 호출된다.

    신원은 HoneyUser UA 토큰(transport/version_check 가 부착, 구버전 클라는 없음).
    없으면 IP 로 집계한다 (역프록시 뒤면 X-Forwarded-For 첫 IP)."""
    try:
        uid = current_user()
        if not uid:
            fwd = request.headers.get("X-Forwarded-For")
            ip = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
            uid = f"ip:{ip}" if ip else ""
        report_db.record_usage("honey_run", uid)
    except Exception:
        pass


@honey_bp.get("/version")
def get_version():
    # ?probe=1 은 집계를 건너뛴다 — 웹 페이지가 다운로드 버튼 링크/파일명을 보정하려고
    # 부르는 경우다(실행이 아니다). /pe 랜딩이 이걸 쓴다. 응답 내용은 완전히 동일하다.
    if request.args.get("probe") != "1":
        _record_run()
    if not HONEY_VERSION_JSON.exists():
        return jsonify({"error": "version.json not found", "version": None}), 404
    try:
        data = json.loads(HONEY_VERSION_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({"error": f"version.json invalid: {exc}"}), 500
    return jsonify(data)


@honey_bp.get("/announcement")
def get_announcement():
    """releases/announcement.txt 를 가공 없이 그대로 돌려준다.

    클라이언트는 이 텍스트를 "최신 버전으로 실행 중" 일 때 PC/사용자 계정별 1회만
    팝업한다 (client/honey_main.py `_maybe_show_announcement`). 파일이 없거나 비어
    있으면 빈 응답 → 클라이언트는 아무 것도 띄우지 않는다.
    """
    if not HONEY_ANNOUNCEMENT_TXT.exists():
        return Response("", mimetype="text/plain")
    try:
        text = HONEY_ANNOUNCEMENT_TXT.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        # 운영자가 메모장 "ANSI" 로 저장한 경우 (한국어 Windows = cp949)
        try:
            text = HONEY_ANNOUNCEMENT_TXT.read_text(encoding="cp949")
        except (OSError, UnicodeDecodeError):
            text = ""
    except OSError:
        text = ""
    return Response(text, mimetype="text/plain")


@honey_bp.get("/download")
def download_release():
    if not HONEY_VERSION_JSON.exists():
        abort(404, "no release published")
    try:
        manifest = json.loads(HONEY_VERSION_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        abort(500, "version.json invalid")

    filename = manifest.get("file")
    if not filename:
        abort(500, "version.json missing 'file' field")
    if "/" in filename or "\\" in filename or filename.startswith("."):
        abort(400, "invalid filename in version.json")

    release_path = HONEY_RELEASES_DIR / filename
    if not release_path.exists():
        abort(404, f"release file not found: {filename}")

    mimetype = "application/zip" if filename.lower().endswith(".zip") else "application/octet-stream"
    return send_file(
        str(release_path),
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype,
    )

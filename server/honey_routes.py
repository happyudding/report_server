"""Honey client update channel.

- GET /honey/version: return releases/version.json as-is.
- GET /honey/download: serve the release ZIP named by version.json.file.
- GET /honey/announcement: return releases/announcement.txt as-is (plain text).
- GET /honey/files/<version>: 그 버전의 파일 매니페스트 (델타 업데이트용).
- GET /honey/file/<version>?path=: 릴리스 zip 안의 파일 1개만 스트리밍 (델타 업데이트용).

뒤의 두 개는 **추가만 된 것**이라 기존 클라이언트에 영향이 없다. 런처는 둘 중 하나라도
없으면(404) 전체 zip 방식으로 폴백하므로, 서버와 클라 배포 순서도 상관없다.
"""
import json
import re
import zipfile

from flask import Blueprint, Response, abort, jsonify, request, send_file

import auth_identity
from auth_identity import current_user
from config import HONEY_ANNOUNCEMENT_TXT, HONEY_RELEASES_DIR, HONEY_VERSION_JSON
from database import report_db

honey_bp = Blueprint("honey", __name__, url_prefix="/honey")

_VERSION_RE = re.compile(r"\d+(\.\d+)*")   # 경로 조각으로 쓰이므로 숫자와 점만


def _record_run():
    """Honey 실행 집계 (best-effort) — 버전체크는 앱 시작 시 1회 호출된다.

    신원은 HoneyUser UA 토큰(transport/version_check 가 부착, 구버전 클라는 없음).
    없으면 IP 로 집계한다 (역프록시 뒤면 X-Forwarded-For 첫 IP).

    같은 UA 에 실려 오는 `HoneyVer/<버전>` 토큰을 버전 대장에도 남긴다 — 클라 버전이
    바뀌는 시점이 곧 앱 시작이라 기록 지점이 여기 하나면 충분하다(요청마다 쓰면 DB 경합).
    신원이 없는 IP 행은 사람이 아닐 수 있어 대장에 넣지 않는다."""
    try:
        uid = current_user()
        if not uid:
            fwd = request.headers.get("X-Forwarded-For")
            ip = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
            uid = f"ip:{ip}" if ip else ""
        report_db.record_usage("honey_run", uid)
    except Exception:
        pass
    try:
        uid = current_user()
        ver = auth_identity.client_version()
        if uid and ver:
            report_db.record_client_version(uid, ver)
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


# ── 델타 업데이트 (런처가 변경된 파일만 받는다) ────────────────────────────────

@honey_bp.get("/files/<version>")
def get_file_manifest(version):
    """그 버전의 파일별 sha256 목록 (`Honey-<ver>.files.json`).

    런처는 이걸 자기 버전 폴더의 캐시와 비교해 **바뀐 파일만** 받는다. 없으면 404 이고,
    런처는 전체 zip 을 받는 종전 경로로 폴백한다 — 즉 이 파일이 없어도 업데이트는 된다.
    """
    if not _VERSION_RE.fullmatch(version):
        abort(400, "invalid version")
    path = HONEY_RELEASES_DIR / f"Honey-{version}.files.json"
    if not path.exists():
        abort(404, "no file manifest for this release")
    return send_file(str(path), mimetype="application/json")


@honey_bp.get("/file/<version>")
def download_release_file(version):
    """릴리스 zip 안의 파일 **하나만** 스트리밍한다.

    zip 을 서버에 풀어 두지 않고도 개별 파일을 줄 수 있어, 릴리스 산출물은 zip 한 개
    그대로 유지된다. 전송 크기를 알리려고 Content-Length 를 붙인다(런처 진행률용).
    """
    if not _VERSION_RE.fullmatch(version):
        abort(400, "invalid version")
    rel = request.args.get("path") or ""
    # 경로 탈출 방어 (download_release 와 같은 취지). 매니페스트의 상대경로만 허용한다.
    if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
        abort(400, "invalid path")

    zip_path = HONEY_RELEASES_DIR / f"Honey-{version}.zip"
    if not zip_path.exists():
        abort(404, "release not found")
    try:
        archive = zipfile.ZipFile(str(zip_path))
    except (OSError, zipfile.BadZipFile):
        abort(500, "release archive unreadable")
    try:
        info = archive.getinfo(f"Honey/versions/{version}/{rel}")
    except KeyError:
        archive.close()
        abort(404, "file not in release")

    def _stream():
        try:
            with archive.open(info) as src:
                while True:
                    chunk = src.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            archive.close()

    resp = Response(_stream(), mimetype="application/octet-stream")
    resp.headers["Content-Length"] = str(info.file_size)
    return resp

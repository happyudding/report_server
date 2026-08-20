"""Honey client update channel.

- GET /honey/version: return releases/version.json as-is.
- GET /honey/download: serve the release ZIP named by version.json.file.
- GET /honey/announcement: return releases/announcement.txt as-is (plain text).
- GET /honey/client_notice: 구버전 클라 사용자에게 웹에서 띄울 안내문 + 기준 버전.
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
from config import (
    HONEY_ANNOUNCEMENT_TXT,
    HONEY_OLD_CLIENT_NOTICE_TXT,
    HONEY_RELEASES_DIR,
    HONEY_VERSION_JSON,
)
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


def _read_text_file(path):
    """운영자가 직접 편집하는 텍스트 파일을 인코딩 관용적으로 읽는다 (실패 시 "")."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        # 운영자가 메모장 "ANSI" 로 저장한 경우 (한국어 Windows = cp949)
        try:
            return path.read_text(encoding="cp949")
        except (OSError, UnicodeDecodeError):
            return ""
    except OSError:
        return ""


@honey_bp.get("/client_notice")
def get_client_notice():
    """구버전 Honey 사용자에게 **웹에서** 띄울 안내문 + 기준 버전.

    구버전 클라는 이미 배포된 exe 라 코드를 고칠 수 없다 — 대신 그 사람이 내장
    브라우저로 서버 페이지를 열 때 서버가 판단해 안내를 띄운다(판정·표시는
    static/webreport/old_client_notice.js). 이 라우트는 그 JS 가 쓰는 값만 준다.

    - min_version: version.json 의 같은 이름 필드. **없거나 비면 기능 자체가 꺼진다**
      (안내를 띄우지 않는다) — 실수로 전원에게 팝업이 뜨는 사고를 막는 안전 기본값.
    - title/body: old_client_notice.txt 의 첫 줄 / 나머지. 파일이 없으면 내장 문구.
    - file: 다운로드 버튼의 파일명 힌트(version.json 의 file). 링크는 /honey/download.

    집계하지 않는다 — 이 호출은 '실행'이 아니라 웹 페이지 렌더의 부수 요청이다
    (/honey/version?probe=1 과 같은 취지).
    """
    manifest = {}
    if HONEY_VERSION_JSON.exists():
        try:
            manifest = json.loads(HONEY_VERSION_JSON.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}

    text = _read_text_file(HONEY_OLD_CLIENT_NOTICE_TXT).strip()
    if text:
        head, _, rest = text.partition("\n")
        title, body = head.strip(), rest.strip()
    else:
        title, body = "Honey 업데이트가 필요합니다", (
            "지금 사용 중인 Honey 는 예전 버전입니다.\n"
            "새 버전부터는 자동으로 업데이트되므로 이번 한 번만 새로 받아 주세요.")

    return jsonify({
        "min_version": str(manifest.get("min_version") or "").strip(),
        "version": manifest.get("version") or "",
        "file": manifest.get("file") or "",
        "title": title,
        "body": body,
    })


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

"""자동 업데이트 테스트용 미니 서버 — /honey/version, /honey/download, /honey/announcement.

실행: python client\\update_test\\test_update_server.py  (Ctrl+C 로 종료)

client\\update_test\\release\\ 에 있는 version.json 과 zip 을 그대로 서빙한다
(build_test_release.ps1 의 출력). 운영 서버(server/)와는 아무 관계가 없다 —
업데이트 흐름만 떼어내 개발 PC 에서 돌려보기 위한 것이다.

version.json 은 요청할 때마다 다시 읽는다. 서버를 켜 둔 채로 새 버전을 빌드하면
Honey 를 다시 실행하는 것만으로 새 버전이 보인다.
"""
import argparse
import json
import re
import shutil
import sys
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote

# exe 로 묶으면(frozen) __file__ 은 PyInstaller 가 푼 임시 폴더를 가리킨다 — 그 옆에는
# release\ 가 없으므로 exe 자신이 놓인 폴더를 기준으로 삼아야 한다. 파이썬이 없는 PC
# 에서도 이 서버를 띄우려고 exe 로 배포하기 때문에 필요한 분기다.
_BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
RELEASE_DIR = _BASE_DIR / "release"
_VERSION_RE = re.compile(r"\d+(\.\d+)*")   # 경로 조각으로 쓰이므로 숫자와 점만


class Handler(BaseHTTPRequestHandler):
    server_version = "HoneyTestUpdate/1.0"

    def _send(self, code, body, content_type, extra_headers=()):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):   # noqa: N802 - BaseHTTPRequestHandler 규약
        path = self.path.split("?", 1)[0]
        if path == "/honey/version":
            self._serve_version()
        elif path == "/honey/download":
            self._serve_download()
        elif path == "/honey/announcement":
            self._send(200, b"", "text/plain; charset=utf-8")
        elif path.startswith("/honey/files/"):
            self._serve_file_manifest(path.rsplit("/", 1)[-1])
        elif path.startswith("/honey/file/"):
            self._serve_one_file(path.rsplit("/", 1)[-1])
        else:
            self._send(404, b'{"error":"not found"}', "application/json")

    def _manifest(self):
        # utf-8-sig: 사람이 메모장으로 열어 저장하면 BOM 이 붙는데, utf-8 로 읽으면 그
        # 순간 JSONDecodeError 로 서버가 죽는다 (app_update.read_current 와 같은 이유).
        try:
            return json.loads((RELEASE_DIR / "version.json").read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None

    def _serve_version(self):
        manifest = self._manifest()
        if manifest is None:
            self._send(404, b'{"error":"version.json not found","version":null}',
                       "application/json")
            return
        print(f"  /honey/version -> {manifest.get('version')}")
        self._send(200, json.dumps(manifest).encode("utf-8"), "application/json")

    def _serve_download(self):
        manifest = self._manifest() or {}
        name = manifest.get("file") or ""
        target = RELEASE_DIR / name
        # 경로 탈출 방어 (운영 honey_routes.py 와 같은 이유)
        if not name or "/" in name or "\\" in name or not target.is_file():
            self._send(404, b'{"error":"release file not found"}', "application/json")
            return
        size = target.stat().st_size
        print(f"  /honey/download -> {name} ({size / 1024 / 1024:.1f} MB)")
        # 700MB 를 통째로 메모리에 올리지 않도록 스트리밍한다. Content-Length 는 그대로
        # 보내야 클라이언트 진행바가 전체 크기를 안다.
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        with target.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1024 * 1024)

    # ── 델타 업데이트 (운영 honey_routes.py 와 같은 규약) ─────────────────────
    def _serve_file_manifest(self, version):
        """`Honey-<ver>.files.json` 을 그대로. 없으면 404 → 런처은 전체 zip 으로 폴백."""
        if not _VERSION_RE.fullmatch(version):
            self._send(400, b'{"error":"invalid version"}', "application/json")
            return
        try:
            body = (RELEASE_DIR / f"Honey-{version}.files.json").read_bytes()
        except OSError:
            self._send(404, b'{"error":"no file manifest"}', "application/json")
            return
        print(f"  /honey/files/{version} -> {len(body) / 1024:.0f} KB")
        self._send(200, body, "application/json")

    def _serve_one_file(self, version):
        """릴리스 zip 안의 파일 **하나만** 스트리밍한다 (변경분만 받기).

        zip 을 풀어 두지 않아도 되므로 릴리스 산출물은 zip 한 개 그대로다.
        """
        if not _VERSION_RE.fullmatch(version):
            self._send(400, b'{"error":"invalid version"}', "application/json")
            return
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        rel = unquote(parse_qs(query).get("path", [""])[0])
        # 경로 탈출 방어 (운영 라우트와 같은 취지)
        if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
            self._send(400, b'{"error":"invalid path"}', "application/json")
            return
        zip_path = RELEASE_DIR / f"Honey-{version}.zip"
        if not zip_path.is_file():
            self._send(404, b'{"error":"release not found"}', "application/json")
            return
        try:
            archive = zipfile.ZipFile(zip_path)
        except (OSError, zipfile.BadZipFile):
            self._send(500, b'{"error":"bad archive"}', "application/json")
            return
        try:
            info = archive.getinfo(f"Honey/versions/{version}/{rel}")
        except KeyError:
            archive.close()
            self._send(404, b'{"error":"file not in release"}', "application/json")
            return
        print(f"  /honey/file/{version} -> {rel} ({info.file_size / 1024 / 1024:.1f} MB)")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(info.file_size))
        self.end_headers()
        try:
            with archive.open(info) as src:
                shutil.copyfileobj(src, self.wfile, 256 * 1024)
        finally:
            archive.close()

    def log_message(self, fmt, *args):
        pass   # 기본 접근 로그는 끄고, 위의 print 로만 남긴다


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    if not RELEASE_DIR.exists():
        raise SystemExit(f"릴리스 폴더가 없습니다: {RELEASE_DIR}\n"
                         "먼저 build_test_release.ps1 로 테스트 릴리스를 만드세요.")
    manifest_path = RELEASE_DIR / "version.json"
    if manifest_path.exists():
        try:
            version = json.loads(manifest_path.read_text(encoding="utf-8-sig")).get("version")
            print(f"현재 배포 중인 버전: {version}")
        except (OSError, json.JSONDecodeError) as exc:
            # 여기서 죽으면 서버가 아예 안 떠서 원인을 모른다 — 알려주고 계속 띄운다.
            print(f"[경고] version.json 을 읽지 못했습니다: {exc}")
    print(f"테스트 업데이트 서버: http://{args.host}:{args.port}  (Ctrl+C 종료)")
    print(f"  릴리스 폴더: {RELEASE_DIR}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

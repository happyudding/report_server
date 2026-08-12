"""transport/app_update.py 단위 확인 — 가짜 설치 트리로 빌드본 없이 검증한다.

실행: python client\\update_test\\check_app_update.py
성공하면 마지막 줄에 ALL OK 가 찍힌다 (실패는 AssertionError 로 즉시 중단).

여기서 확인하는 것:
  - current.txt 읽기/쓰기 (원자 교체, 이전 버전 2행)
  - zip 안 앱 폴더 자동 탐지 (zip 루트 구조와 무관)
  - 압축 해제 진행률 / 사용자 취소 / zip-slip 거부
  - install_version 의 tmp -> rename, 기존 폴더 교체, 실패 시 잔재 없음
  - startup_cleanup 의 n-1 유지, updates zip 수거
  - 파일 매니페스트 생성/저장/읽기 (델타의 전제인 로컬 캐시)
  - **델타 조립** — 안 바뀐 파일 재사용(하드링크), 바뀐 것만 다운로드, sha256 거부,
    실패 시 잔재 없음. 미니 HTTP 서버를 띄워 실제 다운로드까지 돈다.
  - 런처가 쓰는 나머지 헬퍼 (is_newer / can_write / 서버주소 우선순위 / 실패 카운터)
"""
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from transport import app_update as au   # noqa: E402


def make_zip(path, version, extra_files=(), root_prefix="Honey/versions"):
    """테스트용 릴리스 zip (full 구조: 런처 + current.txt + versions/<ver>/앱)."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Honey/Honey.exe", b"launcher-stub")
        zf.writestr("Honey/current.txt", f"{version}\n")
        base = f"{root_prefix}/{version}"
        zf.writestr(f"{base}/HoneyApp.exe", b"app-stub-" + version.encode())
        zf.writestr(f"{base}/honey.env", "SERVER_BASE_URL=http://127.0.0.1:8090\n")
        zf.writestr(f"{base}/_internal/base_library.zip", b"x" * 50000)
        for name, data in extra_files:
            zf.writestr(f"{base}/{name}", data)
    return path


def start_fake_server(src_dir):
    """`/honey/file/<ver>?path=...` 만 흉내내는 미니 서버 (델타 점검용).

    실제 서버(honey_routes.download_release_file)는 릴리스 zip 에서 엔트리를 꺼내 주고
    여기서는 폴더를 그대로 서빙하지만, 클라이언트가 받는 바이트는 같다.
    포트 0 = 빈 포트 자동 배정 (다른 테스트/서버와 부딪히지 않게).
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, unquote, urlparse

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):   # noqa: N802 - BaseHTTPRequestHandler 규약
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/honey/file/"):
                self.send_error(404)
                return
            rel = unquote(parse_qs(parsed.query).get("path", [""])[0])
            target = Path(src_dir) / rel
            if not rel or not target.is_file():
                self.send_error(404)
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def check(name, condition):
    assert condition, f"FAILED: {name}"
    print(f"  ok  {name}")


def main():
    work = Path(tempfile.mkdtemp(prefix="honey_apptest_"))
    print(f"작업 폴더: {work}")
    try:
        root = work / "Honey"
        (root / "versions").mkdir(parents=True)
        (root / "updates").mkdir()

        print("[1] current.txt")
        check("파일 없으면 (None, None)", au.read_current(root) == (None, None))
        au.write_current(root, "9.0.0")
        check("1행만 쓰면 prev=None", au.read_current(root) == ("9.0.0", None))
        au.write_current(root, "9.0.1", "9.0.0")
        check("2행 = prev", au.read_current(root) == ("9.0.1", "9.0.0"))
        check("임시 파일 잔재 없음",
              not list(root.glob(".current.txt.tmp-*")))

        print("[2] zip 앱 폴더 탐지")
        zip_a = make_zip(work / "a.zip", "9.0.1")
        with zipfile.ZipFile(zip_a) as zf:
            check("full 구조 prefix",
                  au.zip_payload_prefix(zf) == "Honey/versions/9.0.1/")
        zip_flat = work / "flat.zip"
        with zipfile.ZipFile(zip_flat, "w") as zf:
            zf.writestr("HoneyApp.exe", b"x")
        with zipfile.ZipFile(zip_flat) as zf:
            check("루트에 바로 있으면 prefix=''", au.zip_payload_prefix(zf) == "")
        bad = work / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("readme.txt", b"x")
        with zipfile.ZipFile(bad) as zf:
            try:
                au.zip_payload_prefix(zf)
                raise AssertionError("HoneyApp.exe 없는 zip 이 통과했다")
            except RuntimeError:
                check("HoneyApp.exe 없으면 거부", True)

        print("[3] 압축 해제")
        seen = []
        dest = work / "x1"
        au.extract_payload(zip_a, dest, lambda d, t: seen.append((d, t)) or True)
        check("앱 파일만 풀림 (런처/current.txt 제외)",
              (dest / "HoneyApp.exe").exists() and not (dest / "Honey.exe").exists())
        check("_internal 유지", (dest / "_internal" / "base_library.zip").exists())
        check("진행률 total 일정하고 done 이 증가",
              seen and seen[-1][0] == seen[-1][1] and len({t for _d, t in seen}) == 1)

        dest2 = work / "x2"
        try:
            au.extract_payload(zip_a, dest2, lambda d, t: False)
            raise AssertionError("취소가 무시됐다")
        except au.InstallCancelled:
            check("progress_cb False → InstallCancelled", True)

        slip = work / "slip.zip"
        with zipfile.ZipFile(slip, "w") as zf:
            zf.writestr("app/HoneyApp.exe", b"x")
            zf.writestr("app/../../evil.txt", b"x")
        try:
            au.extract_payload(slip, work / "x3")
            raise AssertionError("zip-slip 이 통과했다")
        except RuntimeError as exc:
            check(f"zip-slip 거부 ({exc})", "unsafe path" in str(exc))

        print("[4] install_version")
        final = au.install_version(root, "9.0.1", zip_a)
        check("versions/9.0.1 생성", final == root / "versions" / "9.0.1"
              and (final / "HoneyApp.exe").exists())
        check("tmp 잔재 없음", not list((root / "versions").glob("*.tmp-*")))

        zip_b = make_zip(work / "b.zip", "9.0.1", extra_files=[("new.txt", b"second")])
        au.install_version(root, "9.0.1", zip_b)
        check("같은 버전 재설치 = 통째 교체",
              (final / "new.txt").read_bytes() == b"second")

        try:
            au.install_version(root, "9.0.2", bad)
            raise AssertionError("잘못된 zip 이 설치됐다")
        except RuntimeError:
            check("잘못된 zip → 설치 실패", True)
        check("실패해도 tmp/최종 폴더 잔재 없음",
              not list((root / "versions").glob("*.tmp-*"))
              and not (root / "versions" / "9.0.2").exists())

        print("[5] 정리/조회")
        au.install_version(root, "9.0.0", make_zip(work / "c.zip", "9.0.0"))
        au.install_version(root, "8.9.9", make_zip(work / "d.zip", "8.9.9"))
        check("sorted_versions 최신순",
              au.sorted_versions(root) == ["9.0.1", "9.0.0", "8.9.9"])
        (root / "updates" / "Honey-9.0.1.zip").write_bytes(b"leftover")
        (root / "versions" / "9.9.9.tmp-123").mkdir()
        removed = au.startup_cleanup(root, keep_versions=("9.0.1", "9.0.0"))
        check("keep 외 버전 삭제", not (root / "versions" / "8.9.9").exists())
        check("중단된 tmp 삭제", not list((root / "versions").glob("*.tmp-*")))
        check("updates zip 수거", not list((root / "updates").glob("*.zip")))
        check("유지 대상은 살아 있음",
              (root / "versions" / "9.0.1" / "HoneyApp.exe").exists()
              and (root / "versions" / "9.0.0" / "HoneyApp.exe").exists())
        print(f"  (removed={removed})")

        print("[6] check_disk")
        ok, free_mb, need_mb = au.check_disk(root, 1000)
        check(f"작은 요구치는 통과 (free={free_mb}MB need={need_mb}MB)", ok)
        ok, free_mb, need_mb = au.check_disk(root, 10 ** 15)
        check("비현실적 요구치는 거부", not ok)

        print("[7] 파일 매니페스트")
        cur_dir = root / "versions" / "9.0.1"
        local_files = au.build_file_manifest(cur_dir)
        check("현재 버전 파일 목록 생성", len(local_files) >= 4)
        au.write_file_manifest(cur_dir, local_files)
        check("저장 후 다시 읽으면 같다", au.read_file_manifest(cur_dir) == local_files)
        check("매니페스트 자신은 목록에 없다",
              all(f["p"] != au.MANIFEST_FILENAME for f in au.build_file_manifest(cur_dir)))
        check("없으면 None", au.read_file_manifest(root / "versions" / "9.0.0") is None)

        print("[8] 델타 — 변경분만 받고 나머지는 재사용")
        newdir = work / "release_9.1.0"
        (newdir / "_internal").mkdir(parents=True)
        (newdir / "HoneyApp.exe").write_bytes(b"app-stub-9.1.0")          # 바뀜
        # write_bytes 로 쓰는 이유: write_text 는 '\n' 을 CRLF 로 바꿔 쓰기 때문에
        # zip 에서 푼 같은 내용(LF)과 해시가 달라져 "안 바뀐 파일"이 아니게 된다.
        (newdir / "honey.env").write_bytes(                               # 그대로
            b"SERVER_BASE_URL=http://127.0.0.1:8090\n")
        (newdir / "_internal" / "base_library.zip").write_bytes(b"x" * 50000)   # 그대로
        (newdir / "_internal" / "added.dll").write_bytes(b"new-file")     # 신규
        remote_files = au.build_file_manifest(newdir)

        server, base_url = start_fake_server(newdir)
        try:
            seen = []
            final, stats = au.install_delta(
                root, "9.1.0", base_url, remote_files, cur_dir, local_files,
                progress_cb=lambda d, t: seen.append((d, t)) or True)
            check(f"재사용 2 / 받기 2 (stats={stats})",
                  stats["reuse"] == 2 and stats["fetch"] == 2)
            check("바뀐 파일은 새 내용",
                  (final / "HoneyApp.exe").read_bytes() == b"app-stub-9.1.0")
            check("신규 파일 받아짐",
                  (final / "_internal" / "added.dll").read_bytes() == b"new-file")
            check("안 바뀐 파일 재사용",
                  (final / "_internal" / "base_library.zip").stat().st_size == 50000)
            check("새 버전에 없는 파일은 딸려오지 않는다", not (final / "new.txt").exists())
            check("매니페스트가 새 버전 폴더에 저장됨",
                  au.read_file_manifest(final) == remote_files)
            check("진행 콜백이 불렸다", bool(seen))
            check("tmp 잔재 없음", not list((root / "versions").glob("*.tmp-*")))

            print("[9] 델타 sha256 불일치 → 설치 안 됨")
            tampered = [dict(f) for f in remote_files]
            for entry in tampered:
                if entry["p"] == "HoneyApp.exe":
                    entry["h"] = "0" * 64
            try:
                au.install_delta(root, "9.1.1", base_url, tampered, cur_dir, local_files)
                raise AssertionError("해시가 틀린 파일이 설치됐다")
            except RuntimeError as exc:
                check(f"거부됨 ({exc})", "sha256" in str(exc))
            check("실패해도 잔재 없음",
                  not list((root / "versions").glob("*.tmp-*"))
                  and not (root / "versions" / "9.1.1").exists())
        finally:
            server.shutdown()

        print("[10] 런처가 쓰는 나머지 헬퍼")
        check("is_newer 기본", au.is_newer("3.2.0", "3.1.1")
              and not au.is_newer("3.1.1", "3.2.0")
              and not au.is_newer("3.1.1", "3.1.1"))
        check("is_newer 자릿수", au.is_newer("3.10.0", "3.9.9"))
        check("can_write", au.can_write(root))
        (root / "versions" / "9.0.1" / "honey.env").write_text(
            "# c\nSERVER_BASE_URL=http://10.0.0.9:9999\n", encoding="utf-8")
        # 이 PC 에는 사용자 환경변수 HONEY_SERVER_URL(운영 주소)이 설정돼 있다 —
        # 그게 honey.env 를 이기는 것이 의도된 우선순위라, 여기서 둘 다 못박는다.
        saved_env = os.environ.pop("HONEY_SERVER_URL", None)
        try:
            check("honey.env 에서 서버 주소를 읽는다",
                  au.read_server_url(root, "9.0.1") == "http://10.0.0.9:9999")
            os.environ["HONEY_SERVER_URL"] = "http://env-wins:1234"
            check("HONEY_SERVER_URL 이 honey.env 를 이긴다",
                  au.read_server_url(root, "9.0.1") == "http://env-wins:1234")
        finally:
            os.environ.pop("HONEY_SERVER_URL", None)
            if saved_env is not None:
                os.environ["HONEY_SERVER_URL"] = saved_env
        check("연속 실패 카운터", au.read_fail_count(root, "9.1.0") == 0
              and au.bump_fail_count(root, "9.1.0") == 1
              and au.bump_fail_count(root, "9.1.0") == 2)
        check("다른 버전이면 0 부터", au.read_fail_count(root, "9.2.0") == 0)
        au.clear_fail_count(root)
        check("리셋", au.read_fail_count(root, "9.1.0") == 0)

        print("\nALL OK")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

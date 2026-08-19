"""업로드 경로가 바뀐 채로 턴이 끝나면 web_report 업로드 스모크를 돌린다 (Stop 훅).

왜 (2026-08-19):
    "Web report 업로드 중...(100%)" 타임아웃을 하루 동안 코드 버그로 오진했다. 실제 원인은
    콘솔 QuickEdit 이었지만, 그 과정에서 **성공 경로의 POST /pe/report/upload_webreport 를
    검사하는 테스트가 하나도 없다**는 것이 드러났다(기존 e2e 는 전부 ingest 직접 호출로
    라우트를 우회). 업로드는 깨져도 서버 로그가 조용하고 사용자 화면에서만 멈춘 것처럼
    보여 발견이 늦다 — 그래서 사람이 기억해서 돌리는 대신 훅으로 강제한다.

동작:
    작업트리 diff(HEAD 대비 + 미추적 신규)에 업로드 경로 파일이 있을 때만
    tests/test_upload_webreport_smoke.py 를 실행한다(수 초). 실패하면 Stop 훅 규약대로
    block 을 돌려 모델이 그 턴 안에서 고치게 한다.

fail-open:
    git·파이썬·테스트 파일을 못 찾으면 조용히 통과한다. 이 훅은 안전망이지 관문이 아니다
    (perf_guard 와 같은 관례).

수동 실행:
    server\\.venv\\Scripts\\python.exe tools\\upload_smoke_gate.py --check
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 이 파일들이 바뀌면 업로드가 깨질 수 있다 — 라우트·ingest·저장·부팅·설정.
# 좁게 유지할 것: 넓히면 매 턴 도는 상시 테스트가 되어 훅이 무시당한다.
WATCHED = (
    "server/upload_webreport.py",
    "server/wsgi.py",
    "server/config.py",
    "server/report/report_extension.py",
    "web_report/ingest.py",
    "web_report/honeyform.py",
    "web_report/edits.py",
    "tests/test_upload_webreport_smoke.py",
)

SMOKE = ROOT / "tests" / "test_upload_webreport_smoke.py"
PY = ROOT / "server" / ".venv" / "Scripts" / "python.exe"
_MARKER = Path(tempfile.gettempdir()) / "upload_smoke_last_stop.txt"


def _git(*args) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=15)
        return out.stdout or ""
    except Exception:
        return ""


def touched() -> list[str]:
    """작업트리에서 바뀐 감시 대상 파일 (추적 변경 + 미추적 신규)."""
    files = set()
    for args in (("diff", "--name-only", "HEAD", "--"),
                 ("ls-files", "--others", "--exclude-standard")):
        for line in _git(*args).splitlines():
            rel = line.strip().replace("\\", "/")
            if rel in WATCHED:
                files.add(rel)
    return sorted(files)


def run_smoke() -> tuple[bool, str]:
    """스모크 실행 → (통과 여부, 출력 꼬리). 실행 자체가 불가하면 통과로 본다."""
    if not PY.exists() or not SMOKE.exists():
        return True, ""
    try:
        proc = subprocess.run([str(PY), str(SMOKE)], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=180)
    except Exception as exc:
        return True, f"(스모크를 실행하지 못했습니다: {exc})"
    if proc.returncode == 0:
        return True, ""
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    return False, "\n".join(tail[-25:])


def _once(sig: str) -> bool:
    """같은 실패로 두 번 막지 않는다 — 못 고치는 상태에 갇히면 안 된다."""
    digest = hashlib.sha256(sig.encode()).hexdigest()
    try:
        if _MARKER.read_text(encoding="utf-8").strip() == digest:
            return False
    except OSError:
        pass
    try:
        _MARKER.write_text(digest, encoding="utf-8")
    except OSError:
        pass
    return True


def main() -> int:
    check_only = "--check" in sys.argv
    if not check_only:
        # 이미 Stop 훅 때문에 한 번 되돌아온 턴이면 두 번 막지 않는다.
        try:
            if json.loads(sys.stdin.read() or "{}").get("stop_hook_active"):
                return 0
        except Exception:
            pass

    files = touched()
    if not files and not check_only:
        return 0

    ok, tail = run_smoke()
    if check_only:
        print("통과" if ok else f"실패\n{tail}")
        return 0 if ok else 1
    if ok:
        return 0
    if _once("upload_smoke|" + "|".join(files)):
        sys.stdout.write(json.dumps({
            "decision": "block",
            "reason": "web_report 업로드 스모크가 실패했습니다 — 방금 바꾼 "
                      + ", ".join(files)
                      + " 가 업로드 라우트를 깨뜨렸을 수 있습니다.\n"
                        "업로드는 깨져도 서버 로그가 조용하고 클라 화면에서만 멈춘 것처럼 "
                        "보이므로 여기서 고치고 넘어가세요.\n\n"
                      + tail
                      + "\n\n재현: server\\.venv\\Scripts\\python.exe "
                        "tests\\test_upload_webreport_smoke.py",
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

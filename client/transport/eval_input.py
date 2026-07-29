"""서버 eval DB 선례(precedent) CSV 적재 — Honey 'DB Input'.

적재는 **서버가 자기 eval DB 에 수행**한다. Honey.exe 는 eval_analyzer 를 담지 않고
(build_honey.spec), eval DB 는 서버 파일이라 클라가 직접 열 수 없다.

2단계: mode="validate" 로 검증 결과만 받아 사용자에게 보여주고, 확인하면 **같은 바이트**를
mode="commit" 으로 다시 보낸다 (서버는 중간 상태를 갖지 않는다).
"""
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

from .config import REQUEST_TIMEOUT_SEC, SERVER_BASE_URL
from .uploader import _upload_headers


def post_labels_csv(csv_bytes, file_name, mode="validate", base_url=None):
    """CSV 바이트를 /pe/report/api/eval/labels_import 로 전송.

    mode:    "validate"(검증만) | "commit"(적재).
    Returns: {"ok", "mode", "format", "rows", "groups", "errors", "file_name"}.
             CSV **내용** 오류는 예외가 아니라 ok=False + errors(행별 목록)로 온다 —
             호출측이 목록을 그대로 보여준다.
    Raises:  RuntimeError — 권한/크기/서버 오류(비 2xx) 및 네트워크 실패.
    """
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/pe/report/api/eval/labels_import"

    encoder = MultipartEncoder(fields={
        "mode": mode,
        "file": (file_name or "labels.csv", csv_bytes, "text/csv"),
    })
    headers = _upload_headers(encoder.content_type)
    # 브라우저가 아니므로 CSRF 대신 이 커스텀 헤더가 "Honey 에서만" 을 증명한다.
    headers["X-Honey-Agent"] = "1"

    resp = requests.post(url, data=encoder, headers=headers, timeout=REQUEST_TIMEOUT_SEC)
    if not resp.ok:
        try:
            detail = (resp.json() or {}).get("error") or resp.json()
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(f"DB Input failed: HTTP {resp.status_code} — {detail}")
    return resp.json()

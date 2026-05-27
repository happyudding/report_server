"""서버 업로드 헬퍼."""
from pathlib import Path

import requests

from config import REQUEST_TIMEOUT_SEC, SERVER_BASE_URL


def post_xlsx(xlsx_path, product_type, product, lot_id, base_url=None):
    """xlsx 파일 + 메타를 /pe/report/upload_xlsx 로 전송.

    Returns: response.json() — 실패 시 RuntimeError 발생.
    """
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/pe/report/upload_xlsx"

    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"xlsx not found: {xlsx_path}")

    with xlsx_path.open("rb") as f:
        files = {"xlsx": (xlsx_path.name, f,
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        data = {
            "product_type": product_type,
            "product": product,
            "lot_id": lot_id,
        }
        resp = requests.post(url, files=files, data=data, timeout=REQUEST_TIMEOUT_SEC)

    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"upload failed: HTTP {resp.status_code} — {detail}")
    return resp.json()

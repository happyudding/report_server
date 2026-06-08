"""서버 업로드 헬퍼."""
from pathlib import Path

import requests

from .config import REQUEST_TIMEOUT_SEC, SERVER_BASE_URL


def post_xlsx(xlsx_path, product_type, product, lot_id, password,
              revision="", process="", edm_link="", base_url=None,
              chart_pngs=None, issue_imgs=None, dist_png=None):
    """xlsx 파일 + 메타 (+ 클라이언트가 렌더한 이미지) 를 /pe/report/upload_xlsx 로 전송.

    password:    4자리 숫자 PIN — 추후 서버에서 수정/삭제 시 요구된다.
    issue_imgs:  list[{"row": int, "png": bytes}] — Issue Table 행별 이미지
                 (issue_img_<row> 필드). row 는 0-based 데이터행 인덱스.
    chart_pngs/dist_png: Distribution 업로드 비활성화로 현재 전송하지 않는다.
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
        for item in (issue_imgs or []):
            ri = int(item["row"])
            files[f"issue_img_{ri}"] = (f"issue_{ri}.png", item["png"], "image/png")
        data = {
            "product_type": product_type,
            "product": product,
            "lot_id": lot_id,
            "revision": revision,
            "process": process,
            "edm_link": edm_link,
            "password": password,
        }
        resp = requests.post(url, files=files, data=data, timeout=REQUEST_TIMEOUT_SEC)

    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"upload failed: HTTP {resp.status_code} — {detail}")
    return resp.json()

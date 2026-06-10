"""서버 업로드 헬퍼."""
import json

import requests

from .config import REQUEST_TIMEOUT_SEC, SERVER_BASE_URL


def post_grids(sheet_grids, file_name, product_type, product, lot_id, password,
               revision="", process="", edm_link="", base_url=None,
               issue_imgs=None):
    """추출 시트 grid + 메타 (+ issue_table 행 이미지) 를 /pe/report/upload_xlsx 로 전송.

    sheet_grids: {"summary": {"origin":[r0,c0], "values":[[...]]}, ...} — Excel COM 추출 결과.
    file_name:   원본 xlsx basename (서버 file_name/감사로그용 — 파일 자체는 보내지 않음).
    password:    4자리 숫자 PIN — 추후 서버에서 수정/삭제 시 요구된다.
    issue_imgs:  list[{"row": int, "png": bytes}] — Issue Table 행별 이미지
                 (issue_img_<row> 필드). row 는 0-based 데이터행 인덱스.
    Returns: response.json() — 실패 시 RuntimeError 발생.
    """
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/pe/report/upload_xlsx"

    files = {}
    for item in (issue_imgs or []):
        ri = int(item["row"])
        files[f"issue_img_{ri}"] = (f"issue_{ri}.png", item["png"], "image/png")

    data = {
        "sheet_grids": json.dumps(sheet_grids, ensure_ascii=False, separators=(",", ":")),
        "file_name": file_name,
        "product_type": product_type,
        "product": product,
        "lot_id": lot_id,
        "revision": revision,
        "process": process,
        "edm_link": edm_link,
        "password": password,
    }
    resp = requests.post(url, files=files or None, data=data, timeout=REQUEST_TIMEOUT_SEC)

    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"upload failed: HTTP {resp.status_code} — {detail}")
    return resp.json()

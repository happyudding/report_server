"""서버 업로드 헬퍼."""
import json

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

from .config import REQUEST_TIMEOUT_SEC, SERVER_BASE_URL


def post_grids(sheet_grids, file_name, product_type, product, lot_id, password,
               revision="", process="", edm_link="", base_url=None,
               issue_imgs=None, progress_cb=None):
    """추출 시트 grid + 메타 (+ issue_table 행 이미지) 를 /pe/report/upload_xlsx 로 전송.

    sheet_grids: {"summary": {"origin":[r0,c0], "values":[[...]]}, ...} — Excel COM 추출 결과.
    file_name:   원본 xlsx basename (서버 file_name/감사로그용 — 파일 자체는 보내지 않음).
    password:    4자리 숫자 PIN — 추후 서버에서 수정/삭제 시 요구된다.
    issue_imgs:  list[{"row": int, "png": bytes}] — Issue Table 행별 이미지
                 (issue_img_<row> 필드). row 는 0-based 데이터행 인덱스.
    progress_cb: callable(bytes_read, total_bytes) — 업로드 진행률 콜백 (옵션).
    Returns: response.json() — 실패 시 RuntimeError 발생.
    """
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/pe/report/upload_xlsx"

    fields = {
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
    for item in (issue_imgs or []):
        ri = int(item["row"])
        fields[f"issue_img_{ri}"] = (f"issue_{ri}.png", item["png"], "image/png")

    encoder = MultipartEncoder(fields=fields)
    body = encoder
    if progress_cb is not None:
        body = MultipartEncoderMonitor(
            encoder, lambda monitor: progress_cb(monitor.bytes_read, monitor.len))

    resp = requests.post(
        url, data=body, headers={"Content-Type": body.content_type},
        timeout=REQUEST_TIMEOUT_SEC)

    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"upload failed: HTTP {resp.status_code} — {detail}")
    return resp.json()


def fetch_part_ids(base_url=None):
    """서버 stdinfo DB 의 part_id 전체 목록을 조회. (업로드 다이얼로그 Product 검색용)

    Returns: list[str]. 실패(네트워크/타임아웃/비200) 시 RuntimeError 발생 —
    호출측에서 잡아 사용자에게 안내한다(무음 실패 금지).
    """
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/pe/report/api/part_ids"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC)
    if not resp.ok:
        raise RuntimeError(f"part_ids fetch failed: HTTP {resp.status_code}")
    return resp.json().get("part_ids", [])

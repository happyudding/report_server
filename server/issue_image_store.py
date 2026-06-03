"""Issue_table 행별 분포(Distribution) PNG 저장소.

S3 가 설정돼 있으면 S3 에, 아니면 로컬 디스크(REPORT_UPLOAD_DIR/issue_img/<key>/)에
임시 보관한다. "임시로 PNG 가 보이게" 하기 위한 폴백 — S3 환경변수를 채우면
별도 코드 변경 없이 자동으로 S3 우선으로 동작한다.

키 빌더/업로드/다운로드는 s3_storage.report_s3 를 재사용하고, 이 모듈은
백엔드 선택(S3 vs 로컬)만 캡슐화한다.
"""
import json
from pathlib import Path

from config import REPORT_UPLOAD_DIR
from s3_storage import report_s3
from s3_storage.report_s3 import S3NotConfigured


def s3_available() -> bool:
    try:
        report_s3._require_config()
        return True
    except S3NotConfigured:
        return False


def _local_dir(analysis_key: str) -> Path:
    return Path(REPORT_UPLOAD_DIR) / "issue_img" / analysis_key


def save_images(analysis_key: str, images: list) -> dict:
    """images: [{"row": int, "png": bytes}] 저장. {"backend","rows"} 반환."""
    rows = []
    if s3_available():
        index = []
        for it in images:
            try:
                row = int(it["row"])
                key = report_s3.make_issue_image_s3_key(analysis_key, row)
                report_s3.upload_bytes_to_s3(key, it["png"], content_type="image/png")
                index.append({"row": row, "key": key})
                rows.append(row)
            except Exception:
                continue
        if index:
            try:
                report_s3.upload_json_to_s3(
                    report_s3.make_issue_image_index_s3_key(analysis_key),
                    {"images": index},
                )
            except Exception:
                pass
        return {"backend": "s3", "rows": rows}

    # 로컬 폴백
    d = _local_dir(analysis_key)
    d.mkdir(parents=True, exist_ok=True)
    for it in images:
        try:
            row = int(it["row"])
            (d / f"{row}.png").write_bytes(it["png"])
            rows.append(row)
        except Exception:
            continue
    try:
        (d / "index.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    except Exception:
        pass
    return {"backend": "local", "rows": rows}


def list_rows(analysis_key: str) -> list:
    """이미지가 있는 데이터행 인덱스 리스트. 없으면 []."""
    if s3_available():
        try:
            m = report_s3.download_json_from_s3(
                report_s3.make_issue_image_index_s3_key(analysis_key))
            return [int(x["row"]) for x in (m or {}).get("images", []) or []]
        except Exception:
            return []
    idx = _local_dir(analysis_key) / "index.json"
    if idx.exists():
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            return [int(r) for r in data.get("rows", []) or []]
        except Exception:
            return []
    return []


def load_image(analysis_key: str, row: int) -> bytes:
    """행별 PNG bytes. 없으면 예외."""
    if s3_available():
        return report_s3.download_bytes_from_s3(
            report_s3.make_issue_image_s3_key(analysis_key, int(row)))
    p = _local_dir(analysis_key) / f"{int(row)}.png"
    if not p.exists():
        raise FileNotFoundError(str(p))
    return p.read_bytes()

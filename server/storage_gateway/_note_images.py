"""Note 탭 이미지 백엔드 (S3 + 로컬 폴백) — _issue_images 패턴의 세션 단위 변형.

세션 상세의 Note 탭(Luckysheet)에 붙여넣는 차트 PNG 등을 보관한다. 네임스페이스가
analysis_key 가 아니라 **session_id** 인 이유: Note 는 세션 편집 DB(report_webreport_edit)
와 같은 세션 단위 상태라, dedup(동일 analysis_key) 세션 간에 이미지가 새면 안 된다.

S3 가 설정돼 있으면 S3(`pe/report_server/note_img/<sid>/<image_id>`)에, 아니면 로컬
디스크(REPORT_UPLOAD_DIR/note_img/<sid>/)에 저장한다. 세션당 index.json 으로 목록을
유지해 개수 상한 검사와 세션 삭제 시 일괄 정리에 쓴다 (best-effort — issue_img 와 동급).
"""
import json
import os
from pathlib import Path

from config import REPORT_UPLOAD_DIR
from . import _s3 as report_s3
from ._issue_images import s3_available

# _s3.py 는 외부 담당(무수정) — 신규 키 prefix 는 이 모듈에서 정의한다.
REPORT_S3_NOTE_IMG_PREFIX = os.getenv("REPORT_S3_NOTE_IMG_PREFIX",
                                      "pe/report_server/note_img")

_MIME_BY_EXT = {"png": "image/png", "jpg": "image/jpeg"}


def _s3_key(session_id: str, image_id: str) -> str:
    prefix = REPORT_S3_NOTE_IMG_PREFIX.strip("/")
    return f"{prefix}/{session_id}/{image_id}"


def _s3_index_key(session_id: str) -> str:
    prefix = REPORT_S3_NOTE_IMG_PREFIX.strip("/")
    return f"{prefix}/{session_id}/index.json"


def _local_dir(session_id: str) -> Path:
    return Path(REPORT_UPLOAD_DIR) / "note_img" / session_id


def mime_for(image_id: str) -> str:
    """image_id 확장자 기준 고정 mimetype (서빙 라우트가 nosniff 와 함께 사용)."""
    ext = image_id.rsplit(".", 1)[-1].lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def list_ids(session_id: str) -> list:
    """저장된 image_id 목록 (없으면 [])."""
    if s3_available():
        try:
            m = report_s3.download_json_from_s3(_s3_index_key(session_id))
            return [str(x) for x in (m or {}).get("ids", []) or []]
        except Exception:
            return []
    idx = _local_dir(session_id) / "index.json"
    if idx.exists():
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            return [str(x) for x in data.get("ids", []) or []]
        except Exception:
            return []
    return []


def save_image(session_id: str, image_id: str, data: bytes) -> dict:
    """이미지 1건 저장 + index 갱신. {"backend": "s3"|"local"} 반환."""
    ids = list_ids(session_id)
    if image_id not in ids:
        ids.append(image_id)
    if s3_available():
        report_s3.upload_bytes_to_s3(_s3_key(session_id, image_id), data,
                                     content_type=mime_for(image_id))
        try:
            report_s3.upload_json_to_s3(_s3_index_key(session_id), {"ids": ids})
        except Exception:
            pass
        return {"backend": "s3"}
    d = _local_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / image_id).write_bytes(data)
    try:
        (d / "index.json").write_text(json.dumps({"ids": ids}), encoding="utf-8")
    except Exception:
        pass
    return {"backend": "local"}


def load_image(session_id: str, image_id: str) -> bytes:
    """이미지 bytes. 없으면 예외 (라우트가 404 처리)."""
    if s3_available():
        return report_s3.download_bytes_from_s3(_s3_key(session_id, image_id))
    p = _local_dir(session_id) / image_id
    if not p.exists():
        raise FileNotFoundError(str(p))
    return p.read_bytes()


def delete_all(session_id: str) -> list:
    """세션의 note 이미지 전부 삭제 (세션 삭제 훅). 실패는 warnings 로 반환."""
    warnings = []
    if s3_available():
        for image_id in list_ids(session_id):
            try:
                report_s3.delete_object_from_s3(_s3_key(session_id, image_id))
            except Exception as exc:
                warnings.append(f"S3 note image delete failed ({image_id}): {exc}")
        try:
            report_s3.delete_object_from_s3(_s3_index_key(session_id))
        except Exception:
            pass
    d = _local_dir(session_id)
    try:
        if d.is_dir():
            import shutil
            shutil.rmtree(d)
    except Exception as exc:
        warnings.append(f"local note image delete failed ({d}): {exc}")
    return warnings

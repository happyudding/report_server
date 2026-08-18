"""세션 단위 **큰 본문** 객체 백엔드 (S3 + 로컬 spool) — _note_images 패턴의 변형.

"조회·조인하지 않는 큰 본문"만 이리로 나간다. 현재 유일한 kind 는 `note_sheet`
(Luckysheet 시트 JSON — 이미지가 base64 로 들어와 최대 10MB). 작은 편집값은 계속
DB(report_webreport_edit)에 둔다.

네임스페이스가 analysis_key 가 아니라 **session_id** 인 이유는 _note_images 와 같다 —
Note 는 세션 단위 상태라 dedup(동일 analysis_key) 세션 간에 새면 안 된다.

S3 가 설정돼 있으면 `pe/report_server/session_blob/<sid>/<kind>/<sha256>.json.gz`,
아니면 로컬 `REPORT_UPLOAD_DIR/session_blob/<같은 상대경로>` 에 저장한다. **S3 가 설정된
상태에서 업로드가 실패하면** 같은 상대경로의 로컬 spool 에 원자적으로 저장하고
`backend='local_pending'` 을 돌려준다 — 사용자 입력을 잃지 않고, cleanup 이 나중에 재이관한다.
"""
import os
import shutil
import time
import uuid
from pathlib import Path

from config import REPORT_UPLOAD_DIR
from . import _s3 as report_s3
from ._issue_images import s3_available

# _s3.py 는 외부 담당(무수정) — 신규 키 prefix 는 이 모듈에서 정의한다.
REPORT_S3_SESSION_BLOB_PREFIX = os.getenv("REPORT_S3_SESSION_BLOB_PREFIX",
                                          "pe/report_server/session_blob")

_LOCAL_ROOT_NAME = "session_blob"


def object_key(session_id: str, kind: str, content_hash: str) -> str:
    """backend 공통 상대 키. S3 키와 로컬 경로가 이 값으로 1:1 대응한다."""
    return f"{session_id}/{kind}/{content_hash}.json.gz"


def _s3_key(rel_key: str) -> str:
    return f"{REPORT_S3_SESSION_BLOB_PREFIX.strip('/')}/{rel_key}"


def _local_path(rel_key: str) -> Path:
    return Path(REPORT_UPLOAD_DIR) / _LOCAL_ROOT_NAME / rel_key


def local_root() -> Path:
    return Path(REPORT_UPLOAD_DIR) / _LOCAL_ROOT_NAME


def _write_local_atomic(rel_key: str, data: bytes) -> None:
    """temp 쓰기 + fsync 후 os.replace — 부분 기록 파일이 보이는 순간이 없게."""
    path = _local_path(rel_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    # Windows 는 잠깐 열려 있는 파일에 대해 replace 가 PermissionError 를 낸다.
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 2:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.05)


def save_blob(session_id: str, kind: str, content_hash: str, data: bytes) -> dict:
    """본문 1건 저장. {"backend", "object_key", "size_bytes"} 반환.

    backend: 's3'(업로드 성공) | 'local'(S3 미설정 — 확정 저장) |
    'local_pending'(S3 설정됐으나 업로드 실패 — 로컬에 보관 후 재이관 대기).
    """
    rel_key = object_key(session_id, kind, content_hash)
    info = {"object_key": rel_key, "size_bytes": len(data)}
    if s3_available():
        try:
            report_s3.upload_bytes_to_s3(_s3_key(rel_key), data,
                                         content_type="application/gzip")
            return dict(info, backend="s3")
        except Exception:
            _write_local_atomic(rel_key, data)
            return dict(info, backend="local_pending")
    _write_local_atomic(rel_key, data)
    return dict(info, backend="local")


def load_blob(backend: str, rel_key: str) -> bytes:
    """본문 bytes. 없으면 예외 (호출자가 legacy 폴백).

    backend='s3' 라도 로컬 spool 이 남아 있으면 그것을 먼저 쓴다 — 재이관 직후
    S3 일시 장애에서도 사용자 입력이 계속 보이게 한다.
    """
    path = _local_path(rel_key)
    if backend != "s3" or path.exists():
        if path.exists():
            return path.read_bytes()
        if backend != "s3":
            raise FileNotFoundError(str(path))
    return report_s3.download_bytes_from_s3(_s3_key(rel_key))


def promote_pending(rel_key: str) -> bool:
    """local_pending 을 S3 로 재이관. 성공하면 True (로컬 파일은 남겨두지 않는다)."""
    if not s3_available():
        return False
    path = _local_path(rel_key)
    if not path.exists():
        return False
    report_s3.upload_bytes_to_s3(_s3_key(rel_key), path.read_bytes(),
                                 content_type="application/gzip")
    try:
        path.unlink()
    except OSError:
        pass
    return True


def delete_blob(backend: str, rel_key: str) -> list:
    """본문 1건 삭제 (best-effort). 실패 사유 리스트 반환."""
    warnings = []
    if backend == "s3" and s3_available():
        try:
            report_s3.delete_object_from_s3(_s3_key(rel_key))
        except Exception as exc:
            warnings.append(f"S3 session blob delete failed ({rel_key}): {exc}")
    path = _local_path(rel_key)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        warnings.append(f"local session blob delete failed ({path}): {exc}")
    return warnings


def delete_all(session_id: str, keys=()) -> list:
    """세션의 blob 전부 삭제 (세션 삭제 훅).

    keys 는 DB(report_session_blob)가 아는 (backend, rel_key) 목록 — S3 는 목록 조회
    없이 이 기록으로만 지운다. 로컬은 세션 디렉토리째 rmtree 라 기록이 없어도 정리된다.
    """
    warnings = []
    for backend, rel_key in keys:
        if backend == "s3" and s3_available():
            try:
                report_s3.delete_object_from_s3(_s3_key(rel_key))
            except Exception as exc:
                warnings.append(f"S3 session blob delete failed ({rel_key}): {exc}")
    d = local_root() / session_id
    try:
        if d.is_dir():
            shutil.rmtree(d)
    except OSError as exc:
        warnings.append(f"local session blob delete failed ({d}): {exc}")
    return warnings

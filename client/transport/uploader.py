"""서버 업로드 헬퍼."""
import json
import time
from urllib.parse import quote

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

from .config import (CURRENT_VERSION, REQUEST_TIMEOUT_SEC, SERVER_BASE_URL,
                     WEBREPORT_UPLOAD_TIMEOUT_SEC)
from .retry import get_with_retry


def _upload_headers(content_type):
    """multipart Content-Type + 신원 토큰 UA (version_check._honey_headers 와 동일 규칙).

    UA 가 없으면 서버 관리자 화면의 '지금 접속 중' 에서 업로드 중인 사람이 계정이 아니라
    IP(익명)로 잡힌다. 접근제어에는 쓰이지 않는다 — 세션 소유자(uploaded_by)는 예전처럼
    manifest 의 신고값에서만 온다."""
    headers = {"Content-Type": content_type}
    try:
        import client_identity
        user = client_identity.collect().get("user", "")
    except Exception:
        user = ""
    if user:
        # HoneyVer 는 관리자 화면이 "지금 업로드 중인 사람이 어떤 버전을 쓰는가" 를
        # 보여주는 값이다 (server/auth_identity.client_version).
        headers["User-Agent"] = (f"python-requests HoneyUser/{quote(user, safe='')} "
                                 f"HoneyVer/{CURRENT_VERSION}")
    # 작업 상관 ID — 서버가 이 업로드 요청과 그 뒤 콜드 빌드·오류를 한 타임라인으로
    # 묶는다 (server/diagnostics.py). 없으면 헤더 자체가 안 붙어 종전과 동일하다.
    try:
        from .error_report import operation_headers
        headers.update(operation_headers())
    except Exception:
        pass
    return headers


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
        url, data=body, headers=_upload_headers(body.content_type),
        timeout=REQUEST_TIMEOUT_SEC)

    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"upload failed: HTTP {resp.status_code} — {detail}")
    return resp.json()


def post_webreport(manifest, parquet_items, base_url=None, progress_cb=None,
                   dist_blobs=None, dist_pack=None, timing=None):
    """7-meta honeyform parquet 묶음을 /pe/report/upload_webreport 로 전송.

    manifest: {sources, meta, selected_items, sheets}
    parquet_items: [{"index", "name", "file_name", "data": bytes}, ...]
    dist_blobs: {"all": gzip bytes, "bin1": gzip bytes} — 프리컴퓨트 Distribution ECDF
                (web_report.dist_blob 로 계산). None/빈 값이면 미첨부(서버가 폴백 계산).
    dist_pack: {"index": json str, "chunks": {id: gzip bytes}} — 정렬까지 끝낸
               Distribution pack (web_report.dist_pack). 서버가 **영구** 저장해 조회·
               재조회 모두 재정렬 없이 서빙한다. None 이면 미첨부(서버 폴백 계산).
    timing: dict 를 주면 {mb, body_sec, wait_sec} 를 채운다 (성공·실패 모두).
            body_sec = 바디를 소켓에 다 밀어 넣기까지, wait_sec = 그 뒤 응답까지.
            **서버는 이 둘을 볼 수 없다** — waitress 는 바디를 전량 수신한 뒤에야 요청을
            처리 큐에 넣으므로 전송 시간과 큐 대기가 서버 계측 밖에 있기 때문이다.
            "클라는 200초 무응답인데 서버 기록은 20초" 를 설명할 수 있는 유일한 값이다.
    """
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/pe/report/upload_webreport"

    fields = {
        "manifest": json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
    }
    for item in parquet_items:
        idx = int(item["index"])
        filename = item.get("file_name") or f"webreport_{idx}.parquet"
        fields[f"webreport_{idx}"] = (
            filename,
            item["data"],
            "application/vnd.apache.parquet",
        )
    for variant, field in (("all", "dist_blob"), ("bin1", "dist_blob_bin1")):
        data = (dist_blobs or {}).get(variant)
        if data:
            fields[field] = (f"{field}.json.gz", data, "application/gzip")
    if dist_pack and dist_pack.get("index") and dist_pack.get("chunks"):
        fields["dist_pack_index"] = dist_pack["index"]
        for chunk_id, blob in sorted(dist_pack["chunks"].items()):
            fields[f"dist_pack_chunk_{int(chunk_id)}"] = (
                f"chunk_{int(chunk_id)}.json.gz", blob, "application/gzip")

    encoder = MultipartEncoder(fields=fields)
    body = encoder
    t_start = time.monotonic()
    sent = {"at": None}     # 바디를 다 밀어 넣은 시각 (진행률 100% 도달 시점)

    if progress_cb is not None or timing is not None:
        def _monitor(monitor):
            if sent["at"] is None and monitor.bytes_read >= monitor.len:
                sent["at"] = time.monotonic()
            if progress_cb is not None:
                progress_cb(monitor.bytes_read, monitor.len)
        body = MultipartEncoderMonitor(encoder, _monitor)

    try:
        resp = requests.post(
            url, data=body, headers=_upload_headers(body.content_type),
            timeout=WEBREPORT_UPLOAD_TIMEOUT_SEC)
    finally:
        if timing is not None:
            now = time.monotonic()
            done = sent["at"] or now
            timing["mb"] = round(encoder.len / 1048576, 1)
            timing["body_sec"] = round(done - t_start, 1)
            timing["wait_sec"] = round(now - done, 1)

    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"web report upload failed: HTTP {resp.status_code} — {detail}")
    return resp.json()


def fetch_eval_sensitivity_catalog(base_url=None):
    """AI Comment 민감도 게이지 카탈로그(단계표 + 기본값 + 설명)를 서버에서 조회.

    단계표를 클라에 복제하지 않기 위한 조회다 — 정본은 서버 rules/sensitivity.yaml 이고,
    사본을 두면 사용자가 고른 "3단계" 와 서버가 아는 "3단계" 가 갈린다.

    Returns: dict {version, groups, allowed_keys, help, usage}. 실패 시 RuntimeError —
    호출측이 로컬 캐시본으로 폴백한다(part_ids 와 같은 무음 실패 금지 관례).
    """
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/pe/report/api/eval_sensitivity"
    resp = get_with_retry(url, timeout=REQUEST_TIMEOUT_SEC)
    if not resp.ok:
        raise RuntimeError(f"eval_sensitivity fetch failed: HTTP {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict) or not data.get("groups"):
        raise RuntimeError("eval_sensitivity: 빈 카탈로그")
    return data


def fetch_part_ids(base_url=None):
    """서버 product_info.db 의 part_id/sub_part_id 검색 후보 목록을 조회.
    (업로드 다이얼로그 Product 검색용)

    Returns: list[str]. 실패(네트워크/타임아웃/비200) 시 RuntimeError 발생 —
    호출측에서 잡아 사용자에게 안내한다(무음 실패 금지).
    """
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/pe/report/api/part_ids"
    resp = get_with_retry(url, timeout=REQUEST_TIMEOUT_SEC)
    if not resp.ok:
        raise RuntimeError(f"part_ids fetch failed: HTTP {resp.status_code}")
    return resp.json().get("part_ids", [])

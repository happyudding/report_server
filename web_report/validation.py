"""web_report 입력 검증·정규화 헬퍼 (service.py 에서 분리).

canonical JSON 인코딩(canon)과 manifest/meta/mode 정규화만 담당 — 저장소·캐시에
의존하지 않는 순수 함수 모음이라 cache.py/loader.py 양쪽에서 안전하게 import 한다.
"""
from __future__ import annotations

import json

from werkzeug.utils import secure_filename

WEB_REPORT_MODES = ("Normal", "Compare", "DUT", "Commonality")


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def webreport_colors(opts_raw: str):
    """세션의 webreport_options JSON → Distribution source 색 팔레트.

    반환 None → 색 미지정(legacy) → 프런트가 기본 팔레트(DIST_PALETTE) 사용.
    반환 list → 색 hex 리스트. distribution source i 가 리스트[i] 색을 쓴다.
    """
    if not opts_raw:
        return None
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return None
    if not isinstance(opts, dict):
        return None
    colors = opts.get("colors")
    return [str(c) for c in colors] if isinstance(colors, list) and colors else None


def validate_mode(value) -> str:
    """manifest.mode 를 허용 모드 중 하나로 정규화. 미지정/불명은 'Normal'."""
    mode = str(value or "").strip()
    return mode if mode in WEB_REPORT_MODES else "Normal"


def mode_tables(tables, mode):
    """세션 모드에 따라 분석용 tables 를 변형한다.

    DUT 모드는 업로드된 단일 source 를 honeyform 의 DUT 컬럼으로 분할해 DUT별 pseudo-source
    리스트로 만든다 (클라가 아니라 서버에서 분할 — df_honey→honeyform 포맷 변환 회피).
    Normal/Compare/Commonality 는 tables 를 그대로 쓴다. 반환 tables 는 새 객체(또는 원본
    클론)이므로 이후 in-place item 필터가 캐시 원본을 오염시키지 않는다.
    """
    if mode == "DUT" and len(tables) == 1:
        from .honeyform import split_table_by_dut
        return split_table_by_dut(tables[0])
    return tables


def validate_meta(meta: dict) -> dict:
    return {
        "product_type": str(meta.get("product_type") or "").strip(),
        "product": str(meta.get("product") or "").strip(),
        "lot_id": str(meta.get("lot_id") or "").strip(),
        "revision": str(meta.get("revision") or "").strip()[:80],
        "process": str(meta.get("process") or "").strip()[:80],
        "edm_link": str(meta.get("edm_link") or "").strip()[:500],
        "password": str(meta.get("password") or "").strip(),
        "file_name": secure_filename(str(meta.get("file_name") or "web_report")) or "web_report",
    }


def client_identity(manifest: dict) -> tuple[str, str]:
    """manifest["client"] 에서 클라이언트 신고 신원 추출 → (uploaded_by, client_host).

    구버전 클라이언트(키 없음)는 ("", "") — 하위호환. 클라 신고값이라 위조 가능
    (사내망 감사 용도). analysis_key 산출에는 포함되지 않는다.
    """
    info = manifest.get("client") or {}
    if not isinstance(info, dict):
        return "", ""
    user = str(info.get("user") or "").strip()[:80]
    host = str(info.get("host") or "").strip()[:80]
    domain = str(info.get("domain") or "").strip()[:80]
    # 도메인 미가입 PC 는 USERDOMAIN == 호스트명 — 중복 표기 생략
    if domain and user and domain.lower() != host.lower():
        uploaded_by = f"{domain}\\{user}"
    else:
        uploaded_by = user
    return uploaded_by, host

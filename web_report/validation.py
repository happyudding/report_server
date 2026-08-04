"""web_report 입력 검증·정규화 헬퍼 (service.py 에서 분리).

canonical JSON 인코딩(canon)과 manifest/meta/mode 정규화만 담당 — 저장소·캐시에
의존하지 않는 순수 함수 모음이라 cache.py/loader.py 양쪽에서 안전하게 import 한다.
"""
from __future__ import annotations

import json

WEB_REPORT_MODES = ("Normal", "Compare", "DUT", "Commonality", "Temperature")


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


def webreport_ai_comment(opts_raw: str) -> bool:
    """세션의 webreport_options JSON → IssueTable AI Comment 표시 여부.

    업로드 시 manifest.options 로 실려 세션에 고정된다. 없음/파싱 실패 = False.

    **판정 키는 ``ai_comment_optin``** (2026-08-04). 종전 ``ai_comment`` 단일 키는
    구 클라이언트가 settings.json 에 남은 체크 상태를 화면에 비활성으로 보여주면서도
    True 로 실어 보내, 사용자가 켠 적 없는 세션에도 컬럼이 생겼다. 새 클라는 "라벨
    10회 클릭으로 활성화 + 체크박스 클릭"을 모두 만족할 때만 두 키를 함께 보내므로,
    optin 키가 없는 기존 세션은 자동으로 미표시가 된다(DB 수정 없이 되돌림).
    """
    if not opts_raw:
        return False
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return False
    if not isinstance(opts, dict):
        return False
    return bool(opts.get("ai_comment")) and bool(opts.get("ai_comment_optin"))


def webreport_compare_groups(opts_raw: str, source_names):
    """세션의 webreport_options JSON → Compare 모드 Before/After 그룹 (source 이름 기준).

    업로드 시 Honey 배치 다이얼로그가 manifest.options.compare 로 실어 보낸다.
    index 가 아니라 **이름**으로 저장하므로 Excel 왕복에서 source 가 제거돼도 안전하다.
    반환 {"before": [...], "after": [...]} / 판단 불가면 None (호출부가 legacy 폴백:
    after=sources[0], before=sources[1] — 종전 goodlog 관례와 동일).
    """
    names = [str(n) for n in (source_names or [])]
    if not opts_raw or len(names) < 2:
        return None
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return None
    cmp_opt = opts.get("compare") if isinstance(opts, dict) else None
    if not isinstance(cmp_opt, dict):
        return None
    present = set(names)
    before = [str(n) for n in (cmp_opt.get("before") or []) if str(n) in present]
    after = [str(n) for n in (cmp_opt.get("after") or []) if str(n) in present]
    if not before or not after:
        return None
    return {"before": before, "after": after}


def webreport_temperature_groups(opts_raw: str, source_names):
    """세션의 webreport_options JSON → Temperature 모드 RT/CT/HT 그룹 (source 이름 기준).

    업로드 시 Honey 그룹 다이얼로그가 manifest.options.temperature 로 실어 보낸다.
    compare 와 같은 이유로 index 가 아니라 **이름**으로 저장한다(Excel 왕복 안전).
    형식: ``{"groups": [{"rt": 이름, "members": [이름, ...]}, ...]}`` — members 는
    CT/HT (없을 수 있다). rt 가 사라진 그룹은 버리고, 유효 그룹이 없으면 None
    (호출부는 Normal 과 동일하게 렌더 — 옵션이 깨져도 세션이 열린다).
    """
    names = [str(n) for n in (source_names or [])]
    if not opts_raw or not names:
        return None
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return None
    temp_opt = opts.get("temperature") if isinstance(opts, dict) else None
    if not isinstance(temp_opt, dict):
        return None
    present = set(names)
    groups = []
    for group in temp_opt.get("groups") or []:
        if not isinstance(group, dict):
            continue
        rt = str(group.get("rt") or "")
        if rt not in present:
            continue
        members = [str(m) for m in (group.get("members") or [])
                   if str(m) in present and str(m) != rt]
        groups.append({"rt": rt, "members": members})
    return {"groups": groups} if groups else None


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
    # werkzeug 는 서버 전용 의존성 — Honey 클라(werkzeug 미설치)가 dist_blob 프리컴퓨트로
    # validation(mode_tables/validate_mode)을 import 할 수 있도록 여기서만 지연 import 한다.
    from werkzeug.utils import secure_filename

    return {
        "product_type": str(meta.get("product_type") or "").strip(),
        "family_product": str(meta.get("family_product") or "").strip(),
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

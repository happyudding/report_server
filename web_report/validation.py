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


def webreport_ai_model(opts_raw: str) -> str:
    """세션의 webreport_options JSON → AI Comment 생성 모델 ("default" | "claude").

    "claude" = 업로더 PC 의 Honey 가 로컬 Claude CLI 로 [제안] 문장을 대행 생성해
    push 하는 옵트인 흐름 (docs/23). 없음/파싱 실패/미지값 = "default"(현행 폴백 문장).
    클라는 default 일 때 **키 자체를 싣지 않는다** — options 원문이 report_key 의
    원소라 키가 없어야 기존 세션 캐시 키가 바이트 그대로 유지된다(콜드 폭풍 회피).
    """
    if not opts_raw:
        return "default"
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return "default"
    if not isinstance(opts, dict):
        return "default"
    value = str(opts.get("ai_model") or "").strip()
    return value if value in ("default", "claude") else "default"


def webreport_step(opts_raw: str) -> str:
    """세션의 webreport_options JSON → 업로드 때 지정한 STEP 표시값 (없으면 "").

    honeyform 의 STEP 메타는 파서가 채우는 값이라 실데이터가 전부 ``P2`` 로 온다.
    사용자가 업로드 창에서 고른 공정 STEP(기본 L2)을 여기 실어, 조회 시점에
    ``P2`` 표시만 그 값으로 바꾼다 (metrics._apply_step_label). 원본 parquet 은 불변.

    ⚠️ 세션 컬럼 ``report_session.step`` 은 **기준정보(product_info) lookup 값**이라
    별개다 — 그쪽을 덮어쓰면 기준정보가 사라진다.
    """
    if not opts_raw:
        return ""
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return ""
    if not isinstance(opts, dict):
        return ""
    return str(opts.get("step") or "").strip()[:20]


def webreport_eval_sensitivity(opts_raw: str):
    """세션의 webreport_options JSON → eval 민감도 게이지 설정 (없으면 None).

    조회 모달("이 세션에 어떤 기준이 적용됐나")용 구조 그대로다. 형태:
        {"v":1, "global":3, "groups":{"OUTLIER":4,...}, "manual":{키:값},
         "overrides":{키:값}, "rules_rev":"7"}
    `overrides` 만이 평가에 실제로 쓰이는 값이고(webreport_eval_overrides), 나머지는
    "그 값이 어디서 나왔나" 를 사람에게 보여주기 위한 메타다.

    ⚠ 기본 설정(전 게이지 3·직접 수정 없음)인 세션은 이 키를 **아예 싣지 않는다** —
    옵션 원문이 report_key 의 원소라, 기본값도 실으면 기존 세션의 캐시 키가 통째로
    바뀌어 전 세션 콜드 재빌드가 된다.
    """
    if not opts_raw:
        return None
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return None
    if not isinstance(opts, dict):
        return None
    sens = opts.get("eval_sensitivity")
    return sens if isinstance(sens, dict) and sens else None


def webreport_eval_overrides(opts_raw: str) -> dict:
    """세션의 webreport_options JSON → 엔진에 넘길 {임계값 키: 숫자} (없으면 빈 dict).

    `evaluate(thresholds_override=...)` 인자로 그대로 들어가는 값이다. 값 검증(허용 키·
    범위·관계식)은 **업로드/저장 시점**에 서버가 끝내므로(server/upload_webreport.py) 여기서는
    숫자 형변환만 한다 — 조회 경로가 매번 룰 파일을 읽지 않게 하기 위해서다.
    bool 은 숫자로 받지 않는다(True 가 1.0 으로 새어 임계값이 되는 것을 막는다).
    """
    sens = webreport_eval_sensitivity(opts_raw)
    if not sens:
        return {}
    raw = sens.get("overrides")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        name = str(key).strip()
        if name:
            out[name] = value
    return out


def webreport_compare_groups(opts_raw: str, source_names):
    """세션의 webreport_options JSON → Compare 모드 Before/After 그룹 (source 이름 기준).

    업로드 시 Honey 배치 다이얼로그가 manifest.options.compare 로 실어 보낸다.
    index 가 아니라 **이름**으로 저장하므로 Excel 왕복에서 source 가 제거돼도 안전하다.
    반환 {"before": [...], "after": [...]} / 판단 불가면 None (호출부가 legacy 폴백:
    after=sources[0], before=sources[1] — 종전 goodlog 관례와 동일).

    Para Conversion 세션이면 ``"para": True`` 가 함께 실린다 — Single(before) 1개 vs
    DUT 별로 펼친 Para(after) N개 구성이라, goodlog 값 기준과 Map 비교 대상이 달라진다
    (tabs/compare.py). 옛 세션·Normal Compare 는 이 키가 없다.
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
    groups = {"before": before, "after": after}
    if cmp_opt.get("para"):
        groups["para"] = True
    return groups


def webreport_compare_para(opts_raw: str) -> bool:
    """webreport_options JSON → Para Conversion 세션 여부 (source 목록 없이).

    cache_policy.map_key 가 쓴다 — 캐시 키는 tables 를 디코드하기 전에도 같은 값이 나와야
    해서 이름 검증본(webreport_compare_groups)을 쓸 수 없다. compare_key 는 옵션 원문을
    통째로 담아 이미 갈리므로 이 함수가 필요 없다.
    """
    if not opts_raw:
        return False
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return False
    cmp_opt = opts.get("compare") if isinstance(opts, dict) else None
    return bool(isinstance(cmp_opt, dict) and cmp_opt.get("para"))


def webreport_temperature_groups(opts_raw: str, source_names):
    """세션의 webreport_options JSON → Temperature 모드 RT/CT/HT 그룹 (source 이름 기준).

    업로드 시 Honey 그룹 다이얼로그가 manifest.options.temperature 로 실어 보낸다.
    compare 와 같은 이유로 index 가 아니라 **이름**으로 저장한다(Excel 왕복 안전).
    형식: ``{"groups": [{"rt": 이름, "members": [이름, ...],
    "member_roles": ["CT", "HT"]}, ...]}`` — members 는 CT/HT (없을 수 있다).
    ``member_roles`` 는 members 와 같은 길이의 실제 역할이며, **없는 옛 세션도 있다**
    (그때는 호출부가 members 순서로 CT→HT 를 추정한다). members 를 걸러낼 때 짝이
    어긋나지 않도록 여기서 함께 걸러 길이를 맞춘다.
    rt 가 사라진 그룹은 버리고, 유효 그룹이 없으면 None (호출부는 Normal 과 동일하게
    렌더 — 옵션이 깨져도 세션이 열린다).
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
        raw_members = [str(m) for m in (group.get("members") or [])]
        raw_roles = [str(r) for r in (group.get("member_roles") or [])]
        keep = [i for i, m in enumerate(raw_members)
                if m in present and m != rt]
        members = [raw_members[i] for i in keep]
        # member_roles 는 옛 세션에 없다 — 있을 때만, members 와 같은 길이일 때만 싣는다.
        roles = ([raw_roles[i] for i in keep]
                 if len(raw_roles) == len(raw_members) else [])
        entry = {"rt": rt, "members": members}
        if roles:
            entry["member_roles"] = roles
        groups.append(entry)
    return {"groups": groups} if groups else None


def webreport_temperature_rt_names(opts_raw: str) -> set:
    """webreport_options JSON → RT source 이름 집합 (source 목록 없이도 쓸 수 있게).

    Distribution "Bin1(RT만)" 변형이 쓴다 — pack 경로는 tables 를 디코드하지 않아
    source 이름 목록을 갖고 있지 않으므로 위 함수(이름 검증본)를 쓸 수 없다. 실제로
    존재하지 않는 이름이 섞여도 소스별 필터에서 매칭되지 않아 무해하다.
    """
    if not opts_raw:
        return set()
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return set()
    temp_opt = opts.get("temperature") if isinstance(opts, dict) else None
    if not isinstance(temp_opt, dict):
        return set()
    return {str(g.get("rt")) for g in (temp_opt.get("groups") or [])
            if isinstance(g, dict) and g.get("rt")}


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
        # STEP 은 세션 컬럼이 아니라 webreport_options 로 간다 (report_session.step 은
        # 기준정보 lookup 값이라 별개). analysis_key 산출 meta 에는 포함하지 않는다.
        "step": str(meta.get("step") or "").strip()[:20],
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

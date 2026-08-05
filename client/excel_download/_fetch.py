"""Excel Download 용 서버 데이터 수신 — Qt/xlwings 비의존.

서버는 데이터만 내려주고 연산은 전부 클라이언트가 한다:
  - GET /pe/report/session/<sid>/full                      → 모든 탭 데이터 (gzip JSON)
  - GET /pe/report/session/<sid>/web_report/distribution   → ECDF 전량 columnar (gzip)
  - GET /pe/report/session/<sid>/web_report/map_analysis   → Map die 전량 (gzip, schema v8 —
    /full 의 sheets["Map Analysis"] 는 dies 없는 경량 메타라 여기서 받아 병합. 구 서버는
    404 → /full 에 dies 가 이미 실려 있어 무처리)

GET 은 스레드로 동시에 실행한다. requests 가 Content-Encoding: gzip 을 자동 해제한다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests

try:
    from transport.config import REQUEST_TIMEOUT_SEC
except Exception:  # 단독 실행/테스트 폴백
    REQUEST_TIMEOUT_SEC = (10, 300)


def _honey_headers():
    """서버 신원 토큰 — embedded_browser 와 동일 규칙(HoneyUser/<percent-encoded 계정>).

    비공개(is_private) 세션은 업로더/위임 편집자 신원이 있어야 조회 가능하다.
    수집 실패 시 토큰 없이 진행(공개 세션은 무신원으로도 조회됨)."""
    try:
        import client_identity
        user = client_identity.collect().get("user", "")
    except Exception:
        user = ""
    return {"User-Agent": f"python-requests HoneyUser/{quote(user, safe='')}"} if user else {}


def _get_json(url):
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC, headers=_honey_headers())
    resp.raise_for_status()
    return resp.json()


def _get_json_optional(url):
    """신형 엔드포인트용 — 404(구 서버)는 None 폴백, 그 외 오류는 그대로 raise."""
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC, headers=_honey_headers())
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _merge_map_dies(report, map_payload):
    """sheets["Map Analysis"] 경량 rows(schema v8)에 map_analysis 응답 dies 를 병합.

    rows 와 maps 는 같은 빌더 출력이라 인덱스가 일치한다 — source/step 대조는 안전장치.
    map_payload=None(구 서버)이고 rows 에 dies 도 없으면 맵 PNG 를 만들 수 없어 ValueError.
    """
    rows = (report.get("sheets") or {}).get("Map Analysis") or []
    if map_payload is None:
        if rows and not any(isinstance(r.get("dies"), list) for r in rows):
            raise ValueError("Map die 데이터를 받지 못했습니다 — 서버가 map_analysis 를 지원하지 않고 /full 에도 dies 가 없습니다.")
        return
    if str(map_payload.get("format") or "") != "map-dies-v1":
        raise ValueError(f"지원하지 않는 map_analysis 포맷: {map_payload.get('format')!r}")
    maps = map_payload.get("maps") or []
    for i, row in enumerate(rows):
        src = maps[i] if i < len(maps) else None
        if (not src or src.get("source") != row.get("source")
                or src.get("step") != row.get("step")):
            continue
        row["dies"] = src.get("dies") or []


def fetch_report_data(server_base, session_id, bin1=False):
    """(full_payload, dist_payload) 를 동시에 받아 반환.

    full_payload["web_report"] 가 없으면(legacy xlsx 세션 등) ValueError —
    Excel Download 는 web_report 세션 전용이다.

    ``bin1`` 이면 distribution 을 ``?bin1=1`` 로 받아 양품(BIN==1) & 규격(LSL/USL) 이내
    die 만의 ECDF 를 쓴다(산포 CDF/히스토그램을 bin1 기준으로 저장). full(요약/수율/CPK/
    이슈)은 전체 die 기준 그대로.
    """
    base = str(server_base).rstrip("/")
    full_url = f"{base}/pe/report/session/{session_id}/full"
    dist_url = f"{base}/pe/report/session/{session_id}/web_report/distribution"
    map_url = f"{base}/pe/report/session/{session_id}/web_report/map_analysis"
    if bin1:
        dist_url += "?bin1=1"

    with ThreadPoolExecutor(max_workers=3) as pool:
        full_f = pool.submit(_get_json, full_url)
        dist_f = pool.submit(_get_json, dist_url)
        map_f = pool.submit(_get_json_optional, map_url)
        full = full_f.result()
        dist = dist_f.result()
        map_payload = map_f.result()

    report = full.get("web_report")
    if not isinstance(report, dict) or not report.get("sheets"):
        raise ValueError("web_report 세션이 아닙니다 — Excel Download 는 웹 리포트 세션에서만 사용할 수 있습니다.")
    if str(dist.get("format") or "") != "ecdf-columnar-v1":
        raise ValueError(f"지원하지 않는 distribution 포맷: {dist.get('format')!r}")
    _merge_map_dies(report, map_payload)
    return full, dist


_DIST_BATCH_CHUNK = 40      # 서버 distribution_batch 의 subjects 상한과 동일


def fetch_distribution_bin1(server_base, session_id, subjects):
    """지정 항목만 Bin1(양품) ECDF 로 받아 ``{subject: {sources:{src:{x,y}}, ...}}`` 반환.

    Issue Table **CPK 섹션** 썸네일 전용 — 그 행의 cpk 가 Bin1 기준이라 그림도 같은 기준으로
    그린다(웹 미니셀 data-bin1 미러). 전량 재수신을 피하려고 웹과 같은
    ``/web_report/distribution_batch?subjects=...&bin1=1`` 을 40개씩 나눠 부른다.
    실패(구 서버 404 포함)하면 **빈 dict** — 호출부가 전체 기준 셀로 폴백한다.
    """
    names = [s for s in dict.fromkeys(subjects or []) if s]
    if not names:
        return {}
    base = str(server_base).rstrip("/")
    url = f"{base}/pe/report/session/{session_id}/web_report/distribution_batch"
    chunks = [names[i:i + _DIST_BATCH_CHUNK]
              for i in range(0, len(names), _DIST_BATCH_CHUNK)]

    def _one(chunk):
        return _get_json(f"{url}?subjects={quote(','.join(chunk), safe='')}&bin1=1")

    out = {}
    try:
        with ThreadPoolExecutor(max_workers=min(3, len(chunks))) as pool:
            for payload in pool.map(_one, chunks):
                out.update((payload or {}).get("items") or {})
    except Exception:
        return {}
    return out


def fetch_temp_map(server_base, session_id):
    """Temperature 항목별 fail die **인덱스** — ``{source: {item: [idx, ...]}}``.

    서버 ``GET .../web_report/temp_map`` (좌표가 아니라 Map dies 배열 인덱스). Issue Table
    Temp 시트의 Map 썸네일이 쓴다. 실패(구 서버 404 포함)하면 **빈 dict** — 호출부가 Map
    열만 비우고 시트는 그대로 만든다(전체 다운로드가 막히지 않게).
    """
    base = str(server_base).rstrip("/")
    try:
        payload = _get_json(f"{base}/pe/report/session/{session_id}/web_report/temp_map")
    except Exception:
        return {}
    out = {}
    for entry in (payload or {}).get("sources") or []:
        src = str(entry.get("source") or "")
        if not src:
            continue
        out[src] = {str(e.get("item")): list(e.get("idx") or [])
                    for e in (entry.get("items") or []) if e.get("item")}
    return out


def fetch_session_meta(server_base, session_id, timeout=(2, 3)):
    """세션 메타(product/lot_id 등)만 가볍게 조회 — 저장 기본 파일명용.

    실패하면 {} (파일명 폴백은 호출자 책임). UI 스레드에서 부르므로 timeout 을 짧게 잡는다.
    """
    try:
        base = str(server_base).rstrip("/")
        resp = requests.get(f"{base}/pe/report/session/{session_id}", timeout=timeout,
                            headers=_honey_headers())
        resp.raise_for_status()
        return resp.json() or {}
    except Exception:
        return {}

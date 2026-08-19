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

import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests

try:
    from transport.config import CURRENT_VERSION, REQUEST_TIMEOUT_SEC
except Exception:  # 단독 실행/테스트 폴백
    REQUEST_TIMEOUT_SEC = (10, 300)
    CURRENT_VERSION = ""

# 콜드 빌드(202) 대기 상한 — 서버가 준 eta 의 2배, 그래도 이 값을 넘지 않는다.
COLD_WAIT_MAX_SEC = 180
_COLD_POLL_SEC = 1.5


def _honey_headers():
    """서버 신원 토큰 — embedded_browser 와 동일 규칙
    (`HoneyUser/<percent-encoded 계정> HoneyVer/<버전>`).

    비공개(is_private) 세션은 업로더/위임 편집자 신원이 있어야 조회 가능하다.
    수집 실패 시 토큰 없이 진행(공개 세션은 무신원으로도 조회됨).
    버전 토큰은 관리자 화면 표시용이며 접근제어와 무관하다."""
    try:
        import client_identity
        user = client_identity.collect().get("user", "")
    except Exception:
        user = ""
    return ({"User-Agent": f"python-requests HoneyUser/{quote(user, safe='')} "
                           f"HoneyVer/{CURRENT_VERSION}"} if user else {})


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


def _note(status_cb, msg):
    if not status_cb:
        return
    try:
        status_cb(msg)
    except Exception:
        pass


def _wait_cold_build(poll_url, body, status_cb=None):
    """서버가 202(building) 를 준 상태에서 빌드 완료까지 대기.

    ``/full`` 은 콜드 세션이면 **202 + {"building": true, ...}** 를 준다. requests 의
    ``raise_for_status()`` 는 2xx 를 통과시키므로 이 body 를 그대로 쓰면 web_report 키가
    없어 "web_report 세션이 아닙니다" 로 **오진 실패**했다. build_status 를 폴링해 빌드가
    끝나기를 기다린다(상한 COLD_WAIT_MAX_SEC).

    빌드 실패로 차단된 세션은 서버가 503 을 주므로 여기 오지 않는다(_get_json_retry 가 raise).
    """
    eta = body.get("eta")
    try:
        limit = min(COLD_WAIT_MAX_SEC, max(30.0, float(eta) * 2)) if eta else COLD_WAIT_MAX_SEC
    except Exception:
        limit = COLD_WAIT_MAX_SEC
    started = time.monotonic()
    while True:
        waited = time.monotonic() - started
        if waited >= limit:
            raise TimeoutError(
                f"서버가 리포트를 준비하는 데 {int(waited)}초를 넘겼습니다 — "
                "웹에서 세션을 먼저 열어 계산이 끝난 뒤 다시 시도해 주세요.")
        _note(status_cb, f"서버 계산 대기 중 {int(waited)}초…")
        time.sleep(_COLD_POLL_SEC)
        if not poll_url:
            break
        try:
            state = (_get_json(poll_url) or {}).get("state")
        except Exception:
            break               # 폴링 자체가 막히면 그냥 재요청으로 확인한다
        if state != "building":
            break
    return None


def _get_json_retry(url, *, retries=2, backoff=1.5, status_cb=None, optional=False,
                    poll_url=None):
    """GET + 재시도 + 콜드 202 폴링.

    - 연결/타임아웃/5xx → 지수 백오프로 ``retries`` 회 재시도
    - 202 + body["building"] → ``poll_url``(build_status) 폴링 후 같은 URL 재요청.
      콜드 대기는 재시도 횟수를 소진하지 않는다(빌드가 끝나면 정상 응답이므로).
    - ``optional`` 이면 404(구 서버)는 None 폴백
    """
    last = None
    tries = 0
    cold_waits = 0
    while True:
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC, headers=_honey_headers())
            if optional and resp.status_code == 404:
                return None
            if resp.status_code == 202:
                body = {}
                try:
                    body = resp.json() or {}
                except Exception:
                    pass
                if body.get("building") and cold_waits < 2:
                    cold_waits += 1
                    _wait_cold_build(poll_url, body, status_cb=status_cb)
                    continue        # 재시도 카운트를 쓰지 않고 같은 URL 재요청
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            code = getattr(exc.response, "status_code", 0) or 0
            if code < 500:          # 4xx 는 재시도해도 같다
                raise
            last = exc
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
        tries += 1
        if tries > retries:
            break
        wait = backoff * (2 ** (tries - 1))
        _note(status_cb, f"서버 응답 없음 — {wait:.0f}초 후 재시도 ({tries}/{retries})")
        time.sleep(wait)
    raise last if last else RuntimeError(f"GET 실패: {url}")


# Compare 계산 대기 상한 — 콜드 빌드와 별개로 한 번 더 기다릴 수 있는 시간.
COMPARE_WAIT_MAX_SEC = 180
_COMPARE_POLL_SEC = 3.0


def _wait_compare_ready(full_url, report, status_cb=None):
    """Compare 계산이 끝날 때까지 ``/full`` 을 다시 받아 최종 payload 를 돌려준다.

    서버는 Compare 세션의 콜드 첫 조회에서 compare 를 비운 payload(**200** +
    ``compare_pending``)를 먼저 준다(2026-08-19 비동기 분리). 그대로 Excel 을 만들면
    **Compare 시트가 통째로 빠진다** — AI Comment 는 셀만 비지만 이건 시트 단위라
    사용자가 산출물이 잘못된 줄 모르고 쓰게 된다. 그래서 명시적 내보내기인 다운로드는
    기다린다(사용자 결정, 2026-08-19).

    상한을 넘기면 **경고만 남기고 그대로 진행**한다 — 나머지 시트는 정상이므로 다운로드
    전체를 실패시키는 것이 더 나쁘다. AI Comment 단독 대기는 하지 않는다(종전 동작 유지).
    """
    if not report.get("compare_pending"):
        return report
    started = time.monotonic()
    while True:
        waited = time.monotonic() - started
        if waited >= COMPARE_WAIT_MAX_SEC:
            _note(status_cb, "Compare 계산이 늦어져 그 시트 없이 진행합니다")
            return report
        _note(status_cb, f"Compare 계산 대기 중 {int(waited)}초…")
        time.sleep(_COMPARE_POLL_SEC)
        try:
            body = _get_json(full_url) or {}
        except Exception:
            continue          # 일시 오류 — 다음 폴에서 재확인
        got = body.get("web_report")
        if not isinstance(got, dict) or not got.get("sheets"):
            continue
        if not got.get("compare_pending"):
            return got
        report = got


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


def fetch_report_data(server_base, session_id, bin1=False, status_cb=None):
    """(full_payload, dist_payload) 를 동시에 받아 반환.

    full_payload["web_report"] 가 없으면(legacy xlsx 세션 등) ValueError —
    Excel Download 는 web_report 세션 전용이다.

    ``bin1`` 이면 distribution 을 ``?bin1=1`` 로 받아 양품(BIN==1) & 규격(LSL/USL) 이내
    die 만의 ECDF 를 쓴다(산포 CDF/히스토그램을 bin1 기준으로 저장). full(요약/수율/CPK/
    이슈)은 전체 die 기준 그대로.
    """
    base = str(server_base).rstrip("/")
    sess_base = f"{base}/pe/report/session/{session_id}"
    full_url = f"{sess_base}/full"
    dist_url = f"{sess_base}/web_report/distribution"
    map_url = f"{sess_base}/web_report/map_analysis"
    poll_url = f"{sess_base}/web_report/build_status"
    if bin1:
        dist_url += "?bin1=1"

    def _get(url, optional=False):
        return _get_json_retry(url, status_cb=status_cb, optional=optional, poll_url=poll_url)

    with ThreadPoolExecutor(max_workers=3) as pool:
        full_f = pool.submit(_get, full_url)
        dist_f = pool.submit(_get, dist_url)
        map_f = pool.submit(_get, map_url, True)
        full = full_f.result()
        dist = dist_f.result()
        map_payload = map_f.result()

    report = full.get("web_report")
    if not isinstance(report, dict) or not report.get("sheets"):
        raise ValueError("web_report 세션이 아닙니다 — Excel Download 는 웹 리포트 세션에서만 사용할 수 있습니다.")
    if str(dist.get("format") or "") != "ecdf-columnar-v1":
        raise ValueError(f"지원하지 않는 distribution 포맷: {dist.get('format')!r}")
    # Compare 가 아직 계산 중이면 기다린다 — 안 그러면 Compare 시트가 통째로 빠진다.
    report = _wait_compare_ready(full_url, report, status_cb=status_cb)
    full["web_report"] = report
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

    poll_url = f"{base}/pe/report/session/{session_id}/web_report/build_status"

    def _one(chunk):
        return _get_json_retry(f"{url}?subjects={quote(','.join(chunk), safe='')}&bin1=1",
                               retries=1, poll_url=poll_url)

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
    sess_base = f"{base}/pe/report/session/{session_id}"
    try:
        payload = _get_json_retry(f"{sess_base}/web_report/temp_map", retries=1,
                                  poll_url=f"{sess_base}/web_report/build_status")
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

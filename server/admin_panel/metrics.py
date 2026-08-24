"""서버 리소스 시계열 샘플러 + in-flight 요청 카운터 (admin 패널 모니터링).

오버헤드 원칙: 요청 경로에는 lock 1회 + 정수 증감만 얹는다. 샘플링은 10초 간격
데몬 스레드 1개가 psutil 순간값을 링버퍼(24h, deque maxlen)에 쌓는 것이 전부.
CPU 는 sysinfo._cpu_percent() 를 재사용한다 — psutil.cpu_percent(interval=None) 은
프로세스 전역 baseline 을 공유하므로 call-site 를 1개로 유지해야 기존 /api/health
값과 서로 구간을 잘라먹지 않는다.

구간 피크(5분/1시간/24시간)는 조회 시점에 링버퍼에서 계산하고(관리자 조회 시에만
수 ms), 기동 이후 최고치만 running max 로 상시 유지한다.

REPORT_METRICS_ENABLED=0 이면 init_app 이 no-op (kill-switch).
"""
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from pathlib import Path

import psutil
from flask import g, request

import auth_identity
from admin_panel.sysinfo import _cpu_percent, children_rss as _children_rss

_log = logging.getLogger(__name__)

METRICS_ENABLED = os.getenv("REPORT_METRICS_ENABLED", "1") != "0"
SAMPLE_INTERVAL = max(1.0, float(os.getenv("REPORT_METRICS_INTERVAL_SEC", "10")))
RETENTION_SEC = 24 * 3600
# wsgi.py 와 동일 규칙 — in-flight 점유율 분모용
WAITRESS_THREADS = int(os.getenv("WAITRESS_THREADS", "13"))

# flight recorder — 링버퍼(_samples)는 메모리 전용이라 프로세스가 죽으면 크래시 직전
# 리소스 추이가 함께 사라진다. 그 부검을 위해 1분에 1줄씩 metrics_YYYYMMDD.log 에 남긴다.
# REPORT_METRICS_FILE_KEEP_DAYS=0 이면 비활성.
METRICS_FILE_KEEP_DAYS = float(os.getenv("REPORT_METRICS_FILE_KEEP_DAYS", "14"))
_fr_last_minute = None   # 마지막 기록한 분 — 분이 바뀔 때만 append
_fr_last_date = None     # 마지막 기록 날짜 — 롤오버 시 오래된 파일 prune
_fr_warned = False       # 기록 실패 반복 경고 억제 (디스크 풀 시 로그 폭주 방지)

# runtime 이력 — "무엇이 서버에 부담을 주는가"(느린 경로/느린 요청)는 지금까지 in-memory
# 라 재기동마다 사라졌다. watchdog 재기동이 잦을수록 정작 필요한 순간에 비어 있으므로
# runtime_YYYYMMDD.log(JSON lines)에 남긴다. 보관은 metrics_*.log 와 같은 정책.
RUNTIME_LOG_INTERVAL = max(60.0, float(os.getenv("REPORT_RUNTIME_LOG_INTERVAL_SEC", "300")))
SLOW_REQ_MS = float(os.getenv("REPORT_SLOW_REQ_MS", "10000"))
_slow_pending = deque(maxlen=200)  # (ts, route, ms) — 요청 훅이 넣고 샘플러가 비운다
_rt_last_write = 0.0
_rt_warned = False

# ── 끝나지 않는 요청 (2026-08-19) ──────────────────────────────────────────────
# 위 SLOW_REQ_MS 는 요청이 **끝난 뒤** teardown 에서만 재므로, 영원히 안 끝나는 요청은
# 구조적으로 한 줄도 남기지 못한다. 2026-08-19 업로드 hang 이 정확히 그랬다 — 클라는
# 300초 만에 끊겼는데 서버 로그·진단 사건 모두 무기록이었고, 종료할 때 "진행 중 요청
# 10건" 이 안 줄어드는 것으로만 존재를 알 수 있었다. 그래서 진행 중 요청을 따로 들고
# 샘플러가 **끝나기 전에** 잡아낸다.
STUCK_REQ_SEC = float(os.getenv("REPORT_STUCK_REQ_SEC", "120"))
# 업로드는 별도 임계다. 동기 구간이 13단계나 되고 그중 S3 저장·DB 쓰기처럼 밖에서 멎을 수
# 있는 구간이 섞여 있어 가장 자주 hang 하는데, 정작 사용자는 클라 타임아웃(200초)까지
# 화면만 보고 기다린다 — 범용 120초보다 먼저 잡아야 조치할 시간이 남는다.
UPLOAD_SLOW_SEC = float(os.getenv("REPORT_UPLOAD_SLOW_SEC", "100"))
_UPLOAD_ROUTES = frozenset(("report.upload_webreport", "report.upload_xlsx"))
_inflight_reqs = {}    # thread_ident -> (t0_wall, route, session_id)
_stuck_seen = set()    # 이미 사건으로 남긴 (tid, t0) — 10초마다 같은 것을 다시 찍지 않는다
_stuck_dumped = False  # 스레드 덤프는 기동당 1회 (첫 증거가 가장 값지고, 반복은 디스크만 먹는다)

# 요청 **안의 어느 단계**인지 (2026-08-19). _inflight_reqs 는 "무엇이 몇 초째"까지만
# 주므로, 업로드처럼 동기 구간이 긴 요청은 그것만으로 원인을 좁힐 수 없다. 스택 덤프가
# 있긴 하나 기동당 1회뿐이고 사람이 읽어야 한다 — 단계 이름은 기계가 바로 쓴다.
# 스레드 단위이며 teardown 에서 회수한다(요청 훅과 같은 락을 공유).
_req_stages = {}       # thread_ident -> {"cur","src","t0","done":{name: sec}}
_STAGE_MAX = 64        # 한 요청이 남길 수 있는 단계 종류 상한 (폭주 방어)

_proc = psutil.Process()
_lock = threading.Lock()  # 카운터·링버퍼 공용 (임계구역은 정수 연산·append 뿐)
# 샘플: (ts, cpu%, mem_used, proc_rss, inflight, inflight_window_peak, workers_rss)
# workers_rss = 컴퓨트 워커(자식 프로세스) RSS 합 — proc_rss 에는 안 잡힌다.
_samples = deque(maxlen=int(RETENTION_SEC / SAMPLE_INTERVAL))
_inflight = 0
_inflight_window_peak = 0  # 샘플 구간 내 순간 최대 동시 요청 (샘플러가 읽고 리셋)
_boot_peaks = {"cpu": (0.0, 0.0), "rss": (0, 0.0), "mem": (0, 0.0), "inflight": (0, 0.0),
               "total_rss": (0, 0.0)}   # total_rss = 부모 + 컴퓨트 워커
_started = False

# 응답시간 — 최근 요청 소요(ms) 링버퍼(백분위용) + endpoint 별 누적(느린 경로 식별용).
# endpoint 수는 라우트 수만큼이라 상한이 자연스럽다.
_lat_recent = deque(maxlen=2000)
_lat_by_route = {}

# 동시 열람 세션 — "지금 몇 명이 어떤 세션을 보고 있나"가 부하 원인 파악의 출발점인데
# 지금까지 계측이 없었다. 별도 heartbeat 엔드포인트를 두는 대신(프런트 배포 + 상시
# 트래픽이 필요) **세션 데이터를 실제로 요청한 흔적**을 endpoint 화이트리스트로 줍는다.
# 화면을 열어만 두고 아무 요청도 안 하는 열람자는 안 잡히지만, 그런 세션은 부하가 아니다.
_VIEWER_ENDPOINTS = frozenset((
    "report.session_full", "report.web_report_distribution",
    "report.web_report_distribution_batch", "report.web_report_scatter",
    "report.web_report_map_analysis", "report.web_report_raw_data",
    "report.web_report_trim_analysis", "report.web_report_trim_chart_batch"))
# 공개 API(/pe/api/v1) 요청 — 무인증 폴러(사내 대시보드·배치)라 사람 트래픽 지표에서
# 뺀다. 섞이면 빠르고 잦은 API 호출이 p50/p95 표본 대부분을 차지해 **사람의 체감 악화를
# 가리고**, '실시간 접속 사용자'에 ip:<addr> 로 사람처럼 표시된다. 대신 전용 계측
# (public_api/metrics.py → 관리자 'public API' 탭)으로 따로 모은다.
# 식별 키는 Blueprint 이름 규약(public_api/__init__.py BLUEPRINT_PREFIX)이다.
_PUBLIC_API_PREFIX = "public_api_"

_VIEWERS_MAX = 500          # 상한 — 초과 시 가장 오래된 것부터 버린다
VIEWER_WINDOW_SEC = 300     # "최근 N초 안에 요청이 있었으면 열람 중"
_viewers: OrderedDict = OrderedDict()   # session_id -> 마지막 요청 ts (_lock 공유)

# 폴링성(passive) 요청 — 사람이 아무것도 안 해도 브라우저가 알아서 보내는 것들이다.
# 이것들을 '행동'으로 세면 두 가지가 망가진다: ① 마지막 경로가 폴링으로 덮여 화면의
# 활동 라벨이 전부 뭉뚱그려지고, ② 켜두기만 해도 영원히 '활동 중'으로 보인다.
# 그렇다고 집계에서 아예 빼면(=_skip_user_track) 접속자 목록에서 사라진다 — 이 폴링이
# 유일한 생존 신호이기 때문. 그래서 '생존은 갱신, 행동은 불변'으로 나눈다.
_PASSIVE_ENDPOINTS = frozenset((
    "report.my_messages",                  # 관리자 공지 확인 (30초 상시)
    "report.web_report_build_status"))     # 콜드 빌드 대기 (2초)
# 겸용 엔드포인트(실사용과 폴링이 같은 라우트를 쓰는 경우)는 호출부가 ?hb=1 로 알린다.
# 지금 쓰는 곳: boot.js 의 AI 코멘트 대기 폴링(session_full 재사용, 5초 × 최대 20분).
_HB_PARAM = "hb"
# 하트비트가 알려주는 '지금 보고 있는 화면'. 값 3종 외에는 버린다(표시 전용·위조 가능).
_HINT_PAGES = frozenset(("index", "view", "landing"))
_HINT_SID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
# view 탭이 알려준 세션을 index 하트비트가 지우기까지의 유예. 하트비트 3회(90초)를
# 기다리는 이유는 index 와 view 를 동시에 열어 둔 사람의 '보는 세션'이 30초마다
# 붙었다 지워졌다 깜빡이지 않게 하기 위해서다.
_SID_HINT_GRACE_SEC = 90

# 실시간 접속 사용자 — _viewers 는 "어떤 세션이 열려 있나"라서 사람 수를 모른다. 여기서는
# 요청 신원(auth_identity.current_user — Honey UA / SSO 헤더 / 웹 로그인)을 키로 최근 활동을
# 모은다. 신원이 없는 일반 브라우저는 ip:<addr> 로 묶어 "누군지는 몰라도 접속 중"은 보이게 한다.
# 오버헤드는 요청당 UA 정규식 1회 + dict 갱신 1회 (기존 teardown 훅 안에서 처리).
ACTIVE_USER_WINDOW_SEC = max(30, int(os.getenv("REPORT_ACTIVE_USER_WINDOW_SEC", "300")))
_ACTIVE_USERS_MAX = 300
_active_users: OrderedDict = OrderedDict()   # key -> dict(uid, ip, honey, first, last, count, route, session_id, ver, agent, recent, last_input, page, sid_hint_ts)
# last(마지막 요청=접속 유지)와 last_input(마지막 실제 행동)은 다른 값이다. 브라우저가
# 30초마다 보내는 폴링이 last 를 계속 밀어 올리므로, 자리를 비운 사람도 last 기준으로는
# 항상 '방금 활동'으로 보인다. last_input 은 실사용 요청과 클라이언트가 알려준 마지막
# 입력(마우스·키보드) 시각만 반영한다 — 화면의 초록/노랑이 이 값으로 갈린다.
# 신규 필드(last_input/page/sid_hint_ts)는 항상 .get() 으로 읽는다: 재시작 직후 레코드와
# 다른 모듈·테스트가 직접 만들어 넣은 레코드에는 이 키들이 없다.

# 사용자별 최근 요청 이력 — "지금 하는 일"이 마지막 요청 1건뿐이라 무엇을 하다 막혔는지
# 흐름을 볼 수 없었다. 사람당 최근 N건만 메모리에 둔다(전역 상한은 _ACTIVE_USERS_MAX 가
# 이미 건다 — 300명 × 20건). 요청 경로 비용은 기존 락 안에서 deque.append 1회.
_RECENT_PER_USER = 20

# 일별 Peak 동시 접속자 — 위 실시간 값은 메모리에만 있어 이력이 남지 않았다. 샘플러가 그날
# 최대치를 report_usage_peak_daily 에 적재한다. 아래 둘은 **DB 쓰기 억제용 캐시**일 뿐이라
# 재시작으로 0 이 되어도 무해하다(낮은 값으로 덮어쓰는 것은 DB 쪽 MAX 가 막는다).
_peak_day = None   # 마지막으로 본 날짜 ('YYYY-MM-DD') — 자정에 _peak_val 리셋
_peak_val = 0      # 그날 지금까지 기록한 최대값


@contextmanager
def stage(name, source=""):
    """이 요청이 지금 어느 단계인지 기록한다 (진행 중 조회·stuck 사건·완료 후 slow 사건 공용).

    같은 이름으로 **반복** 호출하면 소요가 누적된다 — 파일을 순회하는 decode 처럼
    "총합은 합치고 현재 파일만 바꾸고 싶은" 경우가 그렇다(중첩이 아니라 순차 반복이다).
    중첩해서 열면 안쪽이 끝날 때 바깥 단계로 되돌아간다.

    실패해도 요청을 깨지 않는다 — 계측이 기능을 망가뜨리면 안 된다.
    """
    tid = threading.get_ident()
    t0 = time.perf_counter()
    prev = ("", "")
    try:
        with _lock:
            st = _req_stages.get(tid)
            if st is None:
                st = _req_stages[tid] = {"cur": "", "src": "", "t0": time.time(), "done": {}}
            prev = (st["cur"], st["src"])
            st["cur"] = str(name)[:40]
            st["src"] = str(source or "")[:120]
            st["t0"] = time.time()
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            sec = round(time.perf_counter() - t0, 3)
            with _lock:
                st = _req_stages.get(tid)
                if st is not None:
                    if len(st["done"]) < _STAGE_MAX or name in st["done"]:
                        st["done"][name] = round(st["done"].get(name, 0.0) + sec, 3)
                    st["cur"], st["src"] = prev
                    st["t0"] = time.time()
        except Exception:
            pass


def stages_done():
    """이 요청이 지금까지 끝낸 단계별 소요 (없으면 빈 dict). 라우트가 완료 시 쓴다."""
    try:
        with _lock:
            st = _req_stages.get(threading.get_ident())
            return dict(st["done"]) if st else {}
    except Exception:
        return {}


def cpu_snapshot():
    """(wall, 프로세스 CPU초) 튜플 — 두 시점의 차로 **CPU 시간/실제 시간** 비율을 낸다.

    비율이 낮으면 CPU 를 못 얻었거나(=다른 프로세스와 경합) IO 를 기다린 것이고, 높으면
    실제로 계산한 것이다. 콜드 빌드 워커가 코어를 채워 업로드 디코드가 굶는 현상
    (web_report/compute.py `_lower_worker_priority`)을 사후에 판정하는 유일한 지표라
    업로드 라우트가 요청 전체에 대해 한 번씩만 잰다 — 단계마다 재면 psutil 호출이 늘어난다.
    """
    try:
        t = _proc.cpu_times()
        return time.perf_counter(), t.user + t.system
    except Exception:
        return time.perf_counter(), None


def cpu_ratio(before, after):
    """cpu_snapshot() 두 개로 CPU 점유 비율(0~1, 코어 1개 기준). 계산 불가면 None."""
    try:
        wall = after[0] - before[0]
        if wall <= 0 or before[1] is None or after[1] is None:
            return None
        return round((after[1] - before[1]) / wall, 3)
    except Exception:
        return None


def _on_request_start():
    global _inflight, _inflight_window_peak
    g._mx_counted = True
    g._mx_t0 = time.perf_counter()
    # 진행 중 요청 등록 — 개수만으로는 **무엇이** 걸렸는지 알 수 없다(_inflight_reqs 주석).
    # route/sid 추출은 락 밖에서 끝내고 락은 종전처럼 한 번만 잡는다.
    try:
        route = request.endpoint or request.path
    except Exception:
        route = "?"
    try:
        sid = (request.view_args or {}).get("session_id") or ""
    except Exception:
        sid = ""
    tid = threading.get_ident()
    now = time.time()
    with _lock:
        _inflight += 1
        if _inflight > _inflight_window_peak:
            _inflight_window_peak = _inflight
        _inflight_reqs[tid] = (now, route, sid)


def _on_response(resp):
    """teardown 훅은 응답을 받지 못하므로 상태코드만 여기서 넘겨 둔다 (공개 API 계측용).
    처리 중 예외로 이 훅을 못 거치면 teardown 이 500 으로 간주한다."""
    try:
        g._mx_status = resp.status_code
    except Exception:
        pass
    return resp


def _record_public_api(route, ms):
    """공개 API 계측 위임 — 진실 저장소는 public_api/metrics.py 다.

    지연 import + 광범위 try: 전역 teardown 훅이라 여기서 예외가 새면 모든 요청에 영향간다."""
    try:
        from public_api import metrics as pa_metrics
        fwd = request.headers.get("X-Forwarded-For")
        ip = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
        pa_metrics.record(route, ms, g.pop("_mx_status", 500), ip)
    except Exception:
        pass


def _on_request_teardown(exc=None):
    # 다른 before_request 가 먼저 abort 하면 우리 훅이 안 돌았을 수 있다 — 플래그 확인
    global _inflight
    if not g.pop("_mx_counted", None):
        return
    t0 = g.pop("_mx_t0", None)
    ms = (time.perf_counter() - t0) * 1000.0 if t0 is not None else None
    try:
        route = request.endpoint or request.path
    except Exception:
        route = "?"
    is_public_api = bool(route) and route.startswith(_PUBLIC_API_PREFIX)
    sid = None
    if route in _VIEWER_ENDPOINTS:      # 락 밖에서 뽑는다 (dict 조회 2회)
        try:
            sid = (request.view_args or {}).get("session_id")
        except Exception:
            sid = None
    # 접속 사용자 — 세션 데이터 요청이 아니어도(목록·편집 API 등) 사람은 접속 중이므로
    # _VIEWER_ENDPOINTS 보다 넓게 잡는다. 지금 보고 있는 세션은 참고용으로만 곁들이므로
    # 열람 세션 계측(sid)과 변수를 분리한다 — 섞으면 viewers 화이트리스트가 무너진다.
    ident = user_sid = None
    status = None
    slow = False
    passive = False
    hint_page = hint_sid = hint_idle = None
    if not _skip_user_track(route):
        ident = _identity_for_track()
        if ident is not None:
            try:
                user_sid = (request.view_args or {}).get("session_id")
            except Exception:
                user_sid = None
            # 응답 코드는 활동 타임라인에서 "실패한 요청"을 가려내는 유일한 단서다.
            # 락 밖에서 뽑는다(g 접근은 요청 컨텍스트 작업이라 임계구역에 넣지 않는다).
            status = g.get("_mx_status")
            # 폴링 판별·하트비트 힌트도 요청 컨텍스트 작업이라 락 밖에서 끝낸다.
            passive = _is_passive(route)
            hint_page, hint_sid, hint_idle = _heartbeat_hints()
    with _lock:
        _inflight -= 1
        # 진행 중 등록 해제. stuck 으로 이미 신고된 요청이면 그 표식도 함께 지운다 —
        # 스레드가 재사용되므로 (tid, t0) 쌍으로 지워야 다음 요청이 오탐되지 않는다.
        _done = _inflight_reqs.pop(threading.get_ident(), None)
        if _done is not None:
            _stuck_seen.discard((threading.get_ident(), _done[0]))
        # 단계 기록도 같은 자리에서 회수한다 — 스레드가 재사용되므로 남겨두면 다음
        # 요청이 이전 요청의 단계를 달고 다닌다. 느린 요청 사건에 실어야 하므로
        # 버리기 전에 집어 둔다(아래 _emit_slow_event 는 락 밖에서 돈다).
        _st = _req_stages.pop(threading.get_ident(), None)
        stages_done = dict(_st["done"]) if _st else {}
        if ident is not None:
            key, uid, ip, honey, ver, agent = ident
            now = time.time()
            rec = _active_users.get(key)
            if rec is None:
                rec = {"uid": uid, "ip": ip, "honey": honey, "first": now, "count": 0,
                       "session_id": None, "ver": "", "agents": [],
                       "recent": deque(maxlen=_RECENT_PER_USER)}
                _active_users[key] = rec
            rec["last"] = now
            rec["count"] += 1
            rec["ip"] = ip
            rec["honey"] = honey
            # 버전·접속 경로는 **빈 값으로 덮지 않는다** — 같은 사람이 Honey 앱과 내장
            # 브라우저를 섞어 쓰면 브라우저 요청에는 버전 토큰이 없을 수 있는데, 그때
            # 덮어쓰면 화면의 버전이 깜빡이며 사라진다.
            if ver:
                rec["ver"] = ver
            # 접속 경로는 **누적**한다 — 한 사람이 Honey 앱(업로드)과 내장 브라우저(열람)를
            # 오가는 것이 정상이라, 마지막 요청 하나로 덮으면 화면 배지가 계속 바뀐다.
            if agent and agent not in rec["agents"]:
                rec["agents"].append(agent)
            # 여기부터가 '행동' — 폴링은 위의 생존 신호까지만 갱신하고 아래는 건드리지
            # 않는다. 타임라인(recent)도 마찬가지다: 30초·2초 폴링이 20칸 링버퍼를 채우면
            # 진짜 행동이 몇 분 만에 밀려나 무엇을 하다 막혔는지 볼 수 없게 된다.
            if not passive:
                rec["route"] = route
                if user_sid:
                    rec["session_id"] = user_sid
                # 요청이 곧 행동이다 — 하트비트가 없는 Honey 앱도 이 경로로 초록이 된다.
                rec["last_input"] = now
                rec["recent"].append((now, route, user_sid or "",
                                      round(ms, 1) if ms is not None else None, status))
            if hint_page:
                rec["page"] = hint_page
                if hint_page == "view" and hint_sid:
                    rec["session_id"] = hint_sid
                    rec["sid_hint_ts"] = now
                elif (hint_page != "view"
                      and now - rec.get("sid_hint_ts", 0) > _SID_HINT_GRACE_SEC):
                    # 세션 상세를 떠났다 — 안 지우면 목록으로 돌아가도 '보는 세션'이 남는다.
                    rec["session_id"] = None
            if hint_idle is not None:
                li = now - hint_idle
                # max 로 합친다 — 실사용 요청으로 이미 올려둔 값을 하트비트가 뒤로 끌지 않게.
                if li > rec.get("last_input", 0):
                    rec["last_input"] = li
            _active_users.move_to_end(key)
            while len(_active_users) > _ACTIVE_USERS_MAX:
                _active_users.popitem(last=False)
        if sid:
            _viewers[sid] = time.time()
            _viewers.move_to_end(sid)
            while len(_viewers) > _VIEWERS_MAX:
                _viewers.popitem(last=False)
        if ms is not None:
            # 백분위(p50/p95)는 사람 트래픽만 — 공개 API 는 전용 계측으로 뺀다.
            # route 별 누적은 그대로 둔다 (경로별 평균은 섞여도 왜곡되지 않는다).
            if not is_public_api:
                _lat_recent.append(ms)
            n, total, mx = _lat_by_route.get(route, (0, 0.0, 0.0))
            _lat_by_route[route] = (n + 1, total + ms, max(mx, ms))
            # 느린 요청은 큐에만 넣는다 — 파일 IO 는 샘플러 스레드가 락 밖에서 처리
            if SLOW_REQ_MS > 0 and ms >= SLOW_REQ_MS:
                _slow_pending.append((time.time(), route, ms))
                slow = True
    if slow:
        _emit_slow_event(route, ms, stages_done)    # 락 밖에서 (파일 IO)
    if is_public_api and ms is not None:
        _record_public_api(route, ms)   # 락 밖에서 (전용 모듈이 자기 락을 쓴다)


def _emit_slow_event(route, ms, stages_done=None):
    """느린 요청을 진단 사건으로 — runtime_*.log 의 통계와 달리 **요청 상관 ID**가 붙는다.

    같은 요청이 뒤에 500 으로 끝나거나 콜드 빌드로 이어졌을 때 타임라인에서 이어 보려면
    request_id 가 필요한데, 그건 요청 컨텍스트가 살아 있는 지금만 알 수 있다.
    ≥10초 요청에서만 도는 경로라 파일 IO 비용은 문제되지 않는다.

    stages_done 이 있으면 **어느 단계에 시간이 갔는지**까지 실린다 — 이 사건은 "응답을
    줬는데 느렸다" 쪽이고, 아직 응답을 못 준 요청은 stuck_request 가 맡는다(역할 분리)."""
    try:
        import diagnostics
        ctx = {"endpoint": route, "elapsed_ms": int(ms)}
        if stages_done:
            ctx["stages_done"] = stages_done
        try:
            ctx["session_id"] = (request.view_args or {}).get("session_id") or ""
            ctx["method"] = request.method
        except Exception:
            pass
        ctx.update(diagnostics.current_ids())
        top = ""
        if stages_done:
            k, v = max(stages_done.items(), key=lambda kv: kv[1])
            top = f" (최장 {k} {v}s)"
        diagnostics.emit("warning", "server", "slow_request",
                         message=f"{route} {int(ms)}ms{top}", **ctx)
    except Exception:
        pass


def _skip_user_track(route):
    """접속 사용자 집계에서 뺄 요청 — 관리자 자신·healthz(watchdog 폴링)·정적 파일·
    공개 API(무인증 폴러는 사람이 아니다 → public API 탭에서 따로 본다).

    이것들을 빼야 "지금 몇 명이 쓰고 있나"가 실사용자 수에 가까워진다."""
    return (not route or route == "healthz" or route.endswith("static")
            or route.startswith("admin_panel.") or route.startswith(_PUBLIC_API_PREFIX))


def _is_passive(route):
    """폴링성 요청인가 — 사람의 행동이 아니라 브라우저가 자동으로 보낸 것인가.

    엔드포인트 이름으로 먼저 거른다(구버전 클라도 자동으로 걸린다). ?hb=1 은 실사용과
    라우트를 공유하는 폴링용 보조 표식이다."""
    if route in _PASSIVE_ENDPOINTS:
        return True
    try:
        return request.args.get(_HB_PARAM) == "1"
    except Exception:
        return False


def _heartbeat_hints():
    """하트비트가 실어 보낸 (page, sid, idle_sec). 없거나 이상하면 (None, None, None).

    서버는 어느 페이지를 보고 있는지 알 수 없다 — 세션 목록도 세션 상세도 요청이 멎으면
    똑같이 조용하기 때문이다. 그래서 이미 돌고 있는 관리자 공지 폴링(my_messages)에
    화면 종류와 '마지막 입력 이후 경과초'를 얹어 받는다. 표시 전용이며 위조 가능하므로
    접근제어·감사에는 쓰지 않는다."""
    try:
        if request.endpoint != "report.my_messages":
            return (None, None, None)
        a = request.args
        page = a.get("page") or ""
        if page not in _HINT_PAGES:
            page = None
        sid = a.get("sid") or ""
        if not _HINT_SID_RE.match(sid):
            sid = None
        idle = a.get("idle")
        if idle is not None:
            idle = max(0.0, min(float(idle), 86400.0))
        return (page, sid, idle)
    except Exception:
        return (None, None, None)


def _identity_for_track():
    """요청 신원 → (key, uid, ip, honey, ver, agent). 요청 컨텍스트 문제 시 None (집계 생략).

    ver/agent 는 UA 토큰 파싱 정본(auth_identity)을 그대로 쓴다 — 버전 토큰은 클라가
    보내야 있는 값이라 구버전 클라에서는 "" 다."""
    try:
        ip = request.remote_addr or "?"
        ua = request.headers.get("User-Agent") or ""
    except Exception:
        return None
    try:
        uid = auth_identity.current_user()
    except Exception:
        uid = ""     # SECRET_KEY 미설정 등으로 로그인 세션 조회가 터져도 IP 로는 잡는다
    return (uid or f"ip:{ip}", uid, ip, "HoneyUser/" in ua,
            auth_identity.client_version(ua), auth_identity.client_agent(ua))


def _bump_boot_peak(key, value, ts):
    if value > _boot_peaks[key][0]:
        _boot_peaks[key] = (value, ts)


def _prune_flight_files(log_dir):
    """오래된 metrics_*.log / runtime_*.log / publicapi_*.log 정리 (best-effort)."""
    try:
        cutoff = time.time() - METRICS_FILE_KEEP_DAYS * 86400
        for pattern in ("metrics_*.log", "runtime_*.log", "publicapi_*.log"):
            for p in log_dir.glob(pattern):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def _flight_record(ts, cpu, mem_used, rss, inflight, win_peak, workers_rss=0):
    """분이 바뀔 때만 metrics_YYYYMMDD.log 에 1줄 append (기록마다 open/close — 1회/분이라
    비용 무시 가능, 핸들 상시 보유 없이 외부 삭제·수집과 충돌 없음).

    workers_rss 는 **7번째 컬럼으로 뒤에 붙인다** — 6컬럼짜리 기존 파일도 그대로 파싱되게
    하기 위함이다(file_history 가 7번째를 옵셔널로 읽는다)."""
    global _fr_last_minute, _fr_last_date, _fr_warned
    if METRICS_FILE_KEEP_DAYS <= 0:
        return
    minute = int(ts // 60)
    if minute == _fr_last_minute:
        return
    _fr_last_minute = minute
    try:
        import config
        log_dir = Path(config.ROOT_DIR) / "server" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        lt = time.localtime(ts)
        date = time.strftime("%Y%m%d", lt)
        if date != _fr_last_date:      # 날짜 롤오버(+ 샘플러 시작 첫 기록) 시 prune
            _fr_last_date = date
            _prune_flight_files(log_dir)
        line = "%s,%.1f,%d,%d,%d,%d,%d\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%S", lt), cpu, rss, mem_used, inflight, win_peak,
            workers_rss)
        with (log_dir / f"metrics_{date}.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        if not _fr_warned:
            _fr_warned = True
            _log.warning("[metrics] flight recorder write failed (further warnings suppressed)", exc_info=True)


def _runtime_record(ts):
    """runtime_YYYYMMDD.log 에 (a) 쌓인 느린 요청 이벤트 (b) 주기적 응답시간 스냅샷을
    JSON line 으로 append. 샘플러 스레드에서 락 밖으로 호출한다 (요청 경로 무영향 —
    느린 요청은 최대 SAMPLE_INTERVAL 만큼 늦게 기록되고 종료 시 마지막 구간은 유실 허용)."""
    global _rt_last_write, _rt_warned
    if METRICS_FILE_KEEP_DAYS <= 0:
        return
    with _lock:
        pending = list(_slow_pending)
        _slow_pending.clear()
    due = (ts - _rt_last_write) >= RUNTIME_LOG_INTERVAL
    if not pending and not due:
        return
    lines = []
    for s_ts, route, ms in pending:
        lines.append(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s_ts)),
                                 "type": "slow", "route": route, "ms": round(ms, 1)},
                                ensure_ascii=False))
    if due:
        _rt_last_write = ts
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
        lat = latency_snapshot(top=5)
        lines.append(json.dumps({"ts": stamp,
                                 "type": "lat", "n": lat["samples"], "p50": lat["p50"],
                                 "p95": lat["p95"], "p99": lat["p99"], "max": lat["max"],
                                 "top": lat["slowest"]}, ensure_ascii=False))
        # 부하 스냅샷 — 캐시 히트/미스는 **기동 이후 누적**이라 구간 증분은 화면에서
        # 환산한다(재시작으로 카운터가 리셋되면 음수가 되므로 그때는 0 취급).
        load = dict(load_snapshot())
        load["ts"] = stamp
        load["type"] = "load"
        lines.append(json.dumps(load, ensure_ascii=False))
    try:
        import config
        log_dir = Path(config.ROOT_DIR) / "server" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        date = time.strftime("%Y%m%d", time.localtime(ts))
        with (log_dir / f"runtime_{date}.log").open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        if not _rt_warned:
            _rt_warned = True
            _log.warning("[metrics] runtime history write failed (further warnings suppressed)",
                         exc_info=True)


def _sample():
    ts = time.time()
    cpu = _cpu_percent()
    mem_used = psutil.virtual_memory().used
    rss = _proc.memory_info().rss
    wrss, _wn = _children_rss()      # 컴퓨트 워커 RSS 합 (부모 RSS 에는 안 잡힌다)
    with _lock:
        global _inflight_window_peak
        inflight = _inflight
        win_peak = _inflight_window_peak
        _inflight_window_peak = inflight
        _samples.append((ts, cpu, mem_used, rss, inflight, win_peak, wrss))
        _bump_boot_peak("cpu", cpu, ts)
        _bump_boot_peak("mem", mem_used, ts)
        _bump_boot_peak("rss", rss, ts)
        _bump_boot_peak("inflight", win_peak, ts)
        _bump_boot_peak("total_rss", rss + wrss, ts)
    # 파일 IO 는 락 밖에서 (요청 경로의 in-flight 카운터를 막지 않도록)
    _flight_record(ts, cpu, mem_used, rss, inflight, win_peak, wrss)
    _runtime_record(ts)
    _publicapi_record(ts)
    _record_user_peak(ts)


def _record_user_peak(ts):
    """그날 동시 접속자(사람) 최대값을 **갱신될 때만** DB 에 남긴다.

    사람 수는 active_users() 를 그대로 쓴다 — 관리자 '지금 접속 중' 타일과 같은 값이어야
    하고, 사람 수 산정(IP 병합 포함)을 두 벌 두면 화면마다 숫자가 갈린다.
    대부분의 샘플은 최대치를 넘지 않으므로 DB 쓰기는 하루 수십 건 수준이다.
    """
    global _peak_day, _peak_val
    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    if day != _peak_day:
        _peak_day, _peak_val = day, 0
    # 병합 전 원시 인원으로 먼저 거른다 — active_users() 는 IP 병합 때문에 감사로그를
    # 집계(60초 TTL)하므로, 관리자가 안 보고 있어도 10초마다 부르면 상시 DB 부하가 된다.
    # 병합은 사람 수를 **줄이기만** 하므로 원시 개수가 최대치 이하면 결과도 반드시 이하다.
    cut = ts - ACTIVE_USER_WINDOW_SEC
    with _lock:
        raw = sum(1 for v in _active_users.values() if v["last"] >= cut)
    if raw <= _peak_val:
        return
    try:
        count = active_users()["count"]
    except Exception:
        return
    if count <= _peak_val:
        return
    _peak_val = count
    try:
        from database.usage import record_active_peak
        record_active_peak(count, ACTIVE_USER_WINDOW_SEC, now=ts)
    except Exception:
        # 샘플러 스레드가 죽으면 리소스 차트가 통째로 멈춘다 — 기록 실패는 삼킨다
        # (_flight_record/_runtime_record 와 같은 원칙).
        _log.debug("[metrics] active peak record failed", exc_info=True)


def _publicapi_record(ts):
    """공개 API 분 버킷을 파일로 flush — 요청 경로에 IO 를 얹지 않으려고 샘플러가 부른다."""
    try:
        from public_api import metrics as pa_metrics
        pa_metrics.flush_file(ts)
    except Exception:
        pass


def _loop():
    time.sleep(SAMPLE_INTERVAL)  # 서버 초기화(priming 직후 cpu 0.0)와 겹치지 않게 지연
    while True:
        try:
            _sample()
        except Exception:
            _log.exception("[metrics] sample failed")
        # 샘플링과 분리한다 — 끝나지 않는 요청 감지는 샘플이 실패해도 반드시 돌아야 한다
        # (그 상황이 곧 서버가 이상한 순간이다).
        try:
            _check_stuck_requests()
        except Exception:
            _log.exception("[metrics] stuck request check failed")
        time.sleep(SAMPLE_INTERVAL)


def init_app(app):
    """app 전역 in-flight 훅 등록 + 샘플러 데몬 기동. 중복 기동은 _started 로 방지."""
    global _started
    if not METRICS_ENABLED:
        _log.info("[metrics] disabled (REPORT_METRICS_ENABLED=0)")
        return
    if _started:
        return
    _started = True
    app.before_request(_on_request_start)
    app.after_request(_on_response)
    app.teardown_request(_on_request_teardown)
    threading.Thread(target=_loop, name="admin-metrics-sampler", daemon=True).start()
    _log.info("[metrics] sampler started: interval=%.0fs retention=%dh buf=%d",
              SAMPLE_INTERVAL, RETENTION_SEC // 3600, _samples.maxlen)


def current_inflight():
    """현재 처리 중인 요청 수(호출자 자신 포함). 비활성/미기동이면 None.

    "모름(None)" 과 "0건" 을 반드시 구분해야 한다 — terminate.bat 의 종료 drain 이
    이 값을 보고 "지금 내려도 되는가" 를 판정하므로, 카운터가 없는 상태를 0건으로
    오인하면 진행 중인 업로드를 끊게 된다 (→ ops.healthz).
    """
    if not METRICS_ENABLED or not _started:
        return None
    with _lock:
        return _inflight


def inflight_detail(min_sec=0.0):
    """진행 중 요청 목록 (오래 걸린 것부터). 비활성/미기동이면 None.

    `current_inflight()` 가 **개수**만 준다면 이쪽은 **무엇이 몇 초째인지**를 준다.
    terminate.bat 의 종료 대기 표시와 관리자 '장시간 처리 중' 칩이 같은 값을 쓴다 —
    "10건" 만 보고는 기다려야 할지 끊어야 할지 판단할 수 없기 때문이다.
    """
    if not METRICS_ENABLED or not _started:
        return None
    now = time.time()
    with _lock:
        rows = [(t0, route, sid, _stage_view(_req_stages.get(tid), now))
                for tid, (t0, route, sid) in _inflight_reqs.items()]
    out = [{"route": route, "session_id": sid, "elapsed": round(now - t0, 1), **st}
           for t0, route, sid, st in rows if now - t0 >= min_sec]
    out.sort(key=lambda d: d["elapsed"], reverse=True)
    return out


def stuck_now():
    """지금 임계를 넘긴 진행 중 요청 (**경로별** 임계 적용 — 업로드는 더 짧다).

    `inflight_detail(고정초)` 를 쓰면 업로드 임계(100초)와 범용 임계(120초) 중 하나만
    맞출 수 있어, 관리자 화면이 stuck 사건과 다른 목록을 보여주게 된다. 판정 규칙은
    `_stuck_threshold` 한 곳에만 둔다."""
    rows = inflight_detail()
    if rows is None:
        return None
    return [r for r in rows if r["elapsed"] >= _stuck_threshold(r["route"])]


def _stage_view(st, now):
    """진행 중 단계 요약 (락 안에서 호출). 단계 기록이 없으면 빈 dict — 종전 응답 그대로."""
    if not st or not st.get("cur"):
        return {}
    return {"stage": st["cur"], "stage_source": st.get("src") or "",
            "stage_elapsed": round(now - st.get("t0", now), 1),
            "stages_done": dict(st.get("done") or {})}


def _dump_stuck_threads(rows):
    """stuck 요청의 현행범 스택을 파일로. 반환: 파일명(실패 시 "").

    덤프 자체는 사이드 진단 리스너와 **같은 함수**를 쓴다(diag_listener.dump_threads_text)
    — 사람이 `/threads` 로 받는 것과 같은 내용이어야 판정 절차도 하나로 유지된다.
    console log 탭이 `diagnose_*` 를 이미 열람 허용하므로 별도 배선이 필요 없다.
    """
    try:
        import diagnostics
        from diag_listener import dump_threads_text
        # 경로는 진단 사건과 같은 폴더 — 규칙을 두 번 쓰지 않고, 테스트 격리
        # (REPORT_DIAG_DIR)도 그대로 따라온다.
        path = diagnostics.log_dir() / f"diagnose_stuck_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        head = ["# 임계를 넘겨 아직 처리 중인 요청 — 아래 스택의 공통 대기 지점이 원인이다"
                f" (기준: 업로드 {UPLOAD_SLOW_SEC:.0f}s / 그 외 {STUCK_REQ_SEC:.0f}s)"]
        head += [f"#   {r['route']} {r['elapsed']}s session={r['session_id'] or '-'}"
                 f"{_stage_label(r)} done={r.get('stages_done') or {}}"
                 for r in rows]
        path.write_text("\n".join(head) + "\n\n" + dump_threads_text(), encoding="utf-8")
        return path.name
    except Exception:
        _log.warning("[metrics] stuck 스레드 덤프 실패", exc_info=True)
        return ""


def _stuck_threshold(route):
    """이 경로의 '너무 오래 걸린다' 기준(초). 비활성 임계는 사실상 무한대로 취급한다."""
    sec = UPLOAD_SLOW_SEC if route in _UPLOAD_ROUTES else STUCK_REQ_SEC
    return sec if sec > 0 else float("inf")


def _check_stuck_requests():
    """임계를 넘긴 **진행 중** 요청을 진단 사건으로 남긴다 (샘플러 스레드에서 호출).

    _emit_slow_event 는 teardown(=요청 종료) 에서만 도는 반면 이쪽은 **끝나기 전에** 돈다.
    끝나지 않는 요청은 그 경로를 영원히 못 타므로, 이것이 유일한 기록 지점이다.
    """
    global _stuck_dumped
    if STUCK_REQ_SEC <= 0 and UPLOAD_SLOW_SEC <= 0:
        return
    now = time.time()
    with _lock:
        items = [(tid, t0, route, sid, _stage_view(_req_stages.get(tid), now))
                 for tid, (t0, route, sid) in _inflight_reqs.items()]
        # 고아 단계 기록 회수 — teardown 을 못 거친 요청(다른 훅이 먼저 abort 한 경우)이
        # 남긴 것. 스레드가 재사용될 때 남의 단계를 달고 다니는 것을 막는다.
        for tid in [t for t in _req_stages if t not in _inflight_reqs]:
            _req_stages.pop(tid, None)
    fresh = [it for it in items
             if now - it[1] >= _stuck_threshold(it[2]) and (it[0], it[1]) not in _stuck_seen]
    if not fresh:
        return
    for it in fresh:
        _stuck_seen.add((it[0], it[1]))
    rows = [{"route": route, "session_id": sid, "elapsed": round(now - t0, 1), **st}
            for _tid, t0, route, sid, st in fresh]
    dump = ""
    if not _stuck_dumped:
        _stuck_dumped = True
        dump = _dump_stuck_threads(rows)
    for r in rows:
        _log.error("[metrics] 요청이 %ss 째 끝나지 않음: %s session=%s%s%s",
                   r["elapsed"], r["route"], r["session_id"] or "-", _stage_label(r),
                   f" (스레드 덤프: {dump})" if dump else "")
    try:
        import diagnostics
        top = rows[0]
        diagnostics.emit(
            "critical", "server", "stuck_request",
            error_type="StuckRequest",
            message=(f"{top['route']} {top['elapsed']}s 째 처리 중"
                     f"{_stage_label(top)} (총 {len(rows)}건)"),
            endpoint=top["route"], session_id=top["session_id"] or "",
            elapsed_ms=int(top["elapsed"] * 1000), stuck_count=len(rows),
            stage=top.get("stage", ""), stage_source=top.get("stage_source", ""),
            stages_done=top.get("stages_done") or "",
            thread_dump=dump)
    except Exception:
        pass


def _stage_label(r):
    """" [decode 42.1s lot_c.csv]" — 단계 기록이 없으면 빈 문자열(종전 문구 그대로)."""
    if not r.get("stage"):
        return ""
    src = f" {r['stage_source']}" if r.get("stage_source") else ""
    return f" [{r['stage']} {r.get('stage_elapsed', 0)}s{src}]"


def viewers(window_sec=VIEWER_WINDOW_SEC):
    """최근 window_sec 안에 세션 데이터를 요청한 세션 목록 (동시 열람 근사).

    prune 은 관리자 조회 시점에만 한다 — 요청 경로에 O(n) 을 얹지 않기 위함이다.
    """
    now = time.time()
    cut = now - window_sec
    with _lock:
        for sid in [s for s, ts in _viewers.items() if ts < cut]:
            _viewers.pop(sid, None)
        rows = [(sid, ts) for sid, ts in _viewers.items() if ts >= cut]
    rows.sort(key=lambda r: r[1], reverse=True)
    return {"count": len(rows), "window_sec": window_sec,
            "sessions": [{"session_id": sid, "ago": round(now - ts, 1)} for sid, ts in rows]}


def live_identity_pairs():
    """지금 추적 중인 (계정, IP) 짝 — identity_merge 가 매핑 근거로 쓴다.

    감사 기록이 아직 없는 새 PC 도 접속하는 순간 매핑에 잡히게 하는 용도라, 윈도우와
    무관하게 보유 중인 항목 전부를 준다."""
    with _lock:
        return [(v["uid"], v["ip"]) for v in _active_users.values() if v.get("uid")]


def active_users(window_sec=ACTIVE_USER_WINDOW_SEC):
    """최근 window_sec 안에 요청을 보낸 접속 사용자 목록 (실시간 현황).

    신원(Honey UA/SSO/웹 로그인)이 있으면 계정으로, 없으면 ip:<addr> 로 묶는다. 단
    **IP 가 같으면 같은 사람**이므로, 그 IP 가 계정 하나로 확정되면(identity_merge)
    익명 행을 그 계정 행에 합친다 — 한 사람이 Honey 와 일반 브라우저를 같이 쓸 때
    두 줄로 갈라져 보이던 문제를 없앤다.

    viewers() 와 같은 이유로 prune 은 관리자 조회 시점에만 한다 (요청 경로에 O(n) 을
    얹지 않는다).
    """
    try:
        window_sec = max(30, min(int(window_sec), 24 * 3600))
    except (TypeError, ValueError):
        window_sec = ACTIVE_USER_WINDOW_SEC
    now = time.time()
    cut = now - window_sec
    with _lock:
        for key in [k for k, v in _active_users.items() if v["last"] < cut]:
            _active_users.pop(key, None)
        rows = [(k, dict(v)) for k, v in _active_users.items() if v["last"] >= cut]
    rows.sort(key=lambda r: r[1]["last"], reverse=True)

    from admin_panel import identity_merge
    mapping = identity_merge.ip_to_user()

    merged = OrderedDict()   # 표시 키 -> 누적 행 (최근 활동 순서 유지)
    for key, v in rows:
        name, was_merged = identity_merge.resolve(v["uid"] or key, v["ip"], mapping)
        cur = merged.get(name)
        if cur is None:
            merged[name] = {
                "key": name, "user": name if (v["uid"] or was_merged) else "",
                "ip": v["ip"], "ips": [v["ip"]], "honey": bool(v["honey"]),
                "requests": v["count"], "last": v["last"], "first": v["first"],
                "route": v.get("route") or "", "session_id": v.get("session_id"),
                "merged": was_merged,
                "ver": v.get("ver") or "", "agents": _agent_set(v),
                "last_input": v.get("last_input"), "page": v.get("page") or "",
            }
            continue
        # 합치기 — 요청 수는 더하고, 마지막 활동/보는 세션은 더 최근 쪽을 남긴다.
        cur["requests"] += v["count"]
        cur["first"] = min(cur["first"], v["first"])
        cur["honey"] = cur["honey"] or bool(v["honey"])
        cur["merged"] = True
        # 한 사람이 Honey 앱과 내장 브라우저를 같이 쓰면 두 경로가 모두 남는다.
        for a in _agent_set(v):
            if a not in cur["agents"]:
                cur["agents"].append(a)
        if v.get("ver") and not cur["ver"]:
            cur["ver"] = v["ver"]
        if v["ip"] not in cur["ips"]:
            cur["ips"].append(v["ip"])
        # 마지막 행동 시각은 어느 쪽에서 왔든 더 최근을 남긴다 (last 최신 여부와 무관 —
        # Honey 앱 요청이 브라우저 폴링보다 오래됐어도 그게 진짜 행동일 수 있다).
        if v.get("last_input") and v["last_input"] > (cur.get("last_input") or 0):
            cur["last_input"] = v["last_input"]
        if v["last"] > cur["last"]:
            cur["last"] = v["last"]
            # route/page 는 **빈 값으로 덮지 않는다** — 폴링만 하고 있는 행이 더 최근이라고
            # 해서 그 사람이 마지막으로 한 일을 지우면 안 된다(ver/agents 와 같은 규칙).
            if v.get("route"):
                cur["route"] = v["route"]
            if v.get("page"):
                cur["page"] = v["page"]
            cur["ip"] = v["ip"]
            if v.get("session_id"):
                cur["session_id"] = v["session_id"]
        elif not cur.get("session_id") and v.get("session_id"):
            cur["session_id"] = v["session_id"]

    out = []
    for rec in sorted(merged.values(), key=lambda r: r["last"], reverse=True):
        li = rec.get("last_input")
        out.append({"key": rec["key"], "user": rec["user"], "ip": rec["ip"],
                    "ips": rec["ips"], "honey": rec["honey"], "merged": rec["merged"],
                    "requests": rec["requests"],
                    "ago": round(now - rec["last"], 1),
                    # 마지막 실제 행동 이후 경과초. 화면의 초록/노랑 기준이며, 하트비트가
                    # 없는 옛 클라에서는 None 이라 화면이 종전 ago 기준으로 되돌아간다.
                    "input_ago": round(now - li, 1) if li else None,
                    "since": round(now - rec["first"], 1),
                    "route": rec["route"], "page": rec.get("page") or "",
                    "session_id": rec["session_id"],
                    "ver": rec["ver"], "agents": rec["agents"]})
    return {"count": len(out), "window_sec": window_sec,
            "named": sum(1 for r in out if r["user"]),
            "honey": sum(1 for r in out if r["honey"]),
            "users": out}


def _agent_set(v):
    """추적 레코드 → 접속 경로 목록(사본). 값이 없으면 honey 불린으로 되돌아간다 —
    다른 관리자 모듈이 만들어 넣은 레코드에는 이 키가 없을 수 있다."""
    a = v.get("agents")
    if a:
        return list(a)
    return ["browser"] if v.get("honey") else []


def user_timeline(key, window_sec=ACTIVE_USER_WINDOW_SEC, limit=40):
    """표시 키(active_users 의 `key`) → 그 사람의 최근 요청 목록 (최신 순).

    -> {"key", "count", "items":[{ts, ago, route, session_id, ms, status}]}

    같은 사람이 여러 원본 키(계정 + ip:<addr>)로 잡혀 있을 수 있으므로 active_users 와
    **같은 병합 규칙**으로 묶어서 합친다 — 화면에 한 줄로 보이는 사람의 이력이 조회에서만
    갈라지면 안 된다. 소스는 메모리 링버퍼라 서버 재시작 시 비고, 오래된 항목은
    사람 단위로 최근 20건까지만 남는다.
    """
    if not key:
        return {"key": "", "count": 0, "items": []}
    try:
        window_sec = max(30, min(int(window_sec), 24 * 3600))
    except (TypeError, ValueError):
        window_sec = ACTIVE_USER_WINDOW_SEC
    now = time.time()
    cut = now - window_sec
    with _lock:
        rows = [(k, dict(v), list(v.get("recent") or ()))
                for k, v in _active_users.items() if v["last"] >= cut]

    from admin_panel import identity_merge
    mapping = identity_merge.ip_to_user()

    items = []
    for k, v, recent in rows:
        name, _merged = identity_merge.resolve(v["uid"] or k, v["ip"], mapping)
        if name != key:
            continue
        for ts, route, sid, ms, status in recent:
            # 정렬은 **반올림 전 원본 시각**으로 한다 — 같은 초에 몰린 요청을 정수로 깎으면
            # 순서가 뒤섞여, 화면에서 "무엇을 하다 막혔는지" 흐름이 거꾸로 읽힌다.
            items.append((ts, {"ts": round(ts), "ago": round(now - ts, 1), "route": route,
                               "session_id": sid, "ms": ms, "status": status}))
    items.sort(key=lambda p: p[0], reverse=True)
    return {"key": key, "count": len(items), "items": [d for _ts, d in items[:limit]]}


def load_snapshot():
    """부하 요약 — 동시 열람 + 컴퓨트 큐 + 진행 중 콜드 빌드 + 캐시 누적 히트/미스.

    api/runtime 집계와 시계열 기록(_runtime_record)이 같은 함수를 쓴다. web_report 는
    지연 import + 개별 try — 미기동/kill-switch 여도 나머지 값은 살린다.
    """
    out = {"viewers": viewers()["count"]}
    try:
        from web_report import compute
        st = compute.status()
        out.update(ondemand=st.get("ondemand_pending"), distpack=st.get("distpack_pending"),
                   prewarm=st.get("prewarm_pending"))
    except Exception:
        pass
    try:
        from web_report import build_status
        out["builds"] = len(build_status.snapshot_all())
    except Exception:
        pass
    try:
        from web_report import cache as wr_cache
        cs = wr_cache.cache_stats()
        out.update(hit=cs.get("hit"), miss=cs.get("miss"),
                   disk_hit=cs.get("disk_hit"), disk_miss=cs.get("disk_miss"))
    except Exception:
        pass
    return out


def _pct(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * q)))
    return round(sorted_vals[idx], 1)


def latency_snapshot(top=8):
    """최근 요청 응답시간 백분위 + 평균이 느린 endpoint 상위 목록.

    p95/p99 는 최근 _lat_recent(최대 2000건) 기준이라 "지금 느려졌는지"를 보고,
    route 별 평균/최대는 기동 이후 누적이라 "원래 무거운 경로"를 본다.
    """
    with _lock:
        recent = sorted(_lat_recent)
        routes = [(r, n, total / n, mx) for r, (n, total, mx) in _lat_by_route.items() if n]
    routes.sort(key=lambda x: x[2], reverse=True)
    return {
        "samples": len(recent),
        "p50": _pct(recent, 0.50), "p95": _pct(recent, 0.95), "p99": _pct(recent, 0.99),
        "max": round(recent[-1], 1) if recent else 0.0,
        "slowest": [{"route": r, "count": n, "avg_ms": round(avg, 1), "max_ms": round(mx, 1)}
                    for r, n, avg, mx in routes[:top]],
    }


def _window_peaks(rows, now, window_sec):
    cut = now - window_sec
    cpu = rss = mem = infl = total = 0
    for row in rows:
        ts, c, m, r, _i, wp = row[:6]
        wrss = row[6] if len(row) > 6 else 0
        if ts < cut:
            continue
        if c > cpu:
            cpu = c
        if m > mem:
            mem = m
        if r > rss:
            rss = r
        if wp > infl:
            infl = wp
        if r + wrss > total:
            total = r + wrss
    return {"cpu": cpu, "mem_used": mem, "rss": rss, "inflight": infl,
            "total_rss": total}


def snapshot_history(window_sec, max_points=360):
    """window_sec 구간 시계열 + 피크 요약. 초과 시 버킷 다운샘플(버킷별 max/avg —
    max 를 보존해 피크가 차트에서 사라지지 않게 함)."""
    now = time.time()
    wrss, wn = _children_rss()
    with _lock:
        rows = list(_samples)
        rss_now = _proc.memory_info().rss
        current = {"inflight": _inflight, "cpu": _cpu_percent(),
                   "mem_used": psutil.virtual_memory().used,
                   "rss": rss_now, "workers_rss": wrss, "workers_n": wn,
                   "total_rss": rss_now + wrss}
        boot = {k: {"v": v, "ts": ts} for k, (v, ts) in _boot_peaks.items()}

    peaks = {"w300": _window_peaks(rows, now, 300),
             "w3600": _window_peaks(rows, now, 3600),
             "w86400": _window_peaks(rows, now, 86400),
             "boot": boot}

    cut = now - window_sec
    win = [r for r in rows if r[0] >= cut]
    series = {"ts": [], "cpu_avg": [], "cpu_max": [],
              "mem_used_max": [], "rss_max": [], "inflight_max": [], "total_rss_max": []}
    if win:
        bucket = max(1, (len(win) + max_points - 1) // max_points)
        for i in range(0, len(win), bucket):
            chunk = win[i:i + bucket]
            series["ts"].append(round(chunk[-1][0]))
            cpus = [c[1] for c in chunk]
            series["cpu_avg"].append(round(sum(cpus) / len(cpus), 1))
            series["cpu_max"].append(max(cpus))
            series["mem_used_max"].append(max(c[2] for c in chunk))
            series["rss_max"].append(max(c[3] for c in chunk))
            series["inflight_max"].append(max(c[5] for c in chunk))
            series["total_rss_max"].append(
                max(c[3] + (c[6] if len(c) > 6 else 0) for c in chunk))

    return {"now": round(now), "interval": SAMPLE_INTERVAL, "threads": WAITRESS_THREADS,
            "enabled": METRICS_ENABLED, "current": current,
            "series": series, "peaks": peaks}


# ── 파일 기반 이력 (재시작과 무관) ────────────────────────────────────────────

FILE_HISTORY_MAX_HOURS = 24 * 14  # 파일 보관 기간(14일) 과 동일


def _hist_epoch(text):
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError, TypeError):
        return 0


def _hist_files(log_dir, prefix, hours, now):
    """구간에 걸치는 날짜 파일만 고른다 (일별 파일 — 경계 하루를 여유로 포함)."""
    days = int(hours // 24) + 2
    names = [time.strftime(f"{prefix}_%Y%m%d.log", time.localtime(now - d * 86400))
             for d in range(days)]
    return [log_dir / n for n in reversed(names) if (log_dir / n).exists()]


def file_history(hours=24, max_points=500):
    """metrics_*.log(1분 해상도 리소스) + runtime_*.log(응답시간·느린 요청)를 읽어
    재시작과 무관한 이력을 돌려준다. in-memory snapshot_history 를 대체하지 않고 병행한다
    (현황 탭 = 10초 해상도 실시간 / 이력 탭 = 1분 해상도 최대 14일)."""
    try:
        hours = max(1, min(int(hours), FILE_HISTORY_MAX_HOURS))
    except (TypeError, ValueError):
        hours = 24
    now = time.time()
    cutoff = now - hours * 3600
    import config
    log_dir = Path(config.ROOT_DIR) / "server" / "log"
    used = []

    rows = []
    for path in _hist_files(log_dir, "metrics", hours, now):
        used.append(path.name)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    parts = ln.strip().split(",")
                    if len(parts) < 6:
                        continue
                    ts = _hist_epoch(parts[0])
                    if ts < cutoff:
                        continue
                    try:
                        # 7번째(워커 RSS)는 2026-07-28 추가 — 없는 구파일은 0 으로 읽는다
                        rows.append((ts, float(parts[1]), int(parts[2]),
                                     int(parts[3]), int(parts[4]), int(parts[5]),
                                     int(parts[6]) if len(parts) > 6 else 0))
                    except ValueError:
                        pass
        except OSError:
            pass
    rows.sort(key=lambda r: r[0])

    # 버킷 다운샘플 — 버킷별 max 를 보존해 피크가 사라지지 않게 (snapshot_history 와 동일 규칙)
    resource = {"ts": [], "cpu_max": [], "mem_used_max": [], "rss_max": [],
                "inflight_max": [], "total_rss_max": []}
    if rows:
        bucket = max(1, (len(rows) + max_points - 1) // max_points)
        for i in range(0, len(rows), bucket):
            chunk = rows[i:i + bucket]
            resource["ts"].append(int(chunk[-1][0]))
            resource["cpu_max"].append(max(c[1] for c in chunk))
            resource["rss_max"].append(max(c[2] for c in chunk))
            resource["mem_used_max"].append(max(c[3] for c in chunk))
            resource["inflight_max"].append(max(c[5] for c in chunk))
            resource["total_rss_max"].append(max(c[2] + c[6] for c in chunk))

    lat = {"ts": [], "p95": [], "p99": []}
    # 부하 시계열 — 큐/빌드/열람은 순간값 그대로, 캐시 히트율만 구간 증분으로 환산한다
    # (기록값이 기동 이후 누적이라, 재시작 리셋 구간은 증분이 음수가 되므로 건너뛴다).
    load = {"ts": [], "ondemand": [], "distpack": [], "builds": [], "viewers": [],
            "hit_rate": []}
    _prev_hit = _prev_miss = None
    slow = []
    routes = {}
    for path in _hist_files(log_dir, "runtime", hours, now):
        used.append(path.name)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = json.loads(ln)
                    except ValueError:
                        continue
                    ts = _hist_epoch(rec.get("ts"))
                    if ts < cutoff:
                        continue
                    if rec.get("type") == "lat":
                        lat["ts"].append(int(ts))
                        lat["p95"].append(rec.get("p95") or 0)
                        lat["p99"].append(rec.get("p99") or 0)
                        for t in rec.get("top") or []:
                            r = t.get("route") or "?"
                            n, mx = routes.get(r, (0, 0.0))
                            routes[r] = (n + (t.get("count") or 0),
                                         max(mx, t.get("max_ms") or 0))
                    elif rec.get("type") == "slow":
                        slow.append({"ts": int(ts), "route": rec.get("route"),
                                     "ms": rec.get("ms")})
                    elif rec.get("type") == "load":
                        load["ts"].append(int(ts))
                        for key in ("ondemand", "distpack", "builds", "viewers"):
                            load[key].append(rec.get(key) or 0)
                        hit, miss = rec.get("hit"), rec.get("miss")
                        rate = None
                        if hit is not None and miss is not None:
                            if _prev_hit is not None:
                                d_hit, d_miss = hit - _prev_hit, miss - _prev_miss
                                if d_hit >= 0 and d_miss >= 0 and (d_hit + d_miss) > 0:
                                    rate = round(d_hit / (d_hit + d_miss) * 100, 1)
                            _prev_hit, _prev_miss = hit, miss
                        load["hit_rate"].append(rate)
        except OSError:
            pass
    slow.sort(key=lambda d: d["ts"], reverse=True)
    top_routes = sorted(({"route": r, "count": n, "max_ms": mx}
                         for r, (n, mx) in routes.items()),
                        key=lambda d: d["max_ms"], reverse=True)[:10]

    return {"hours": hours, "now": round(now), "resource": resource, "lat": lat,
            "load": load, "slow": slow[:100], "top_routes": top_routes,
            "slow_threshold_ms": SLOW_REQ_MS, "files": used,
            "coverage_from": int(rows[0][0]) if rows else None}

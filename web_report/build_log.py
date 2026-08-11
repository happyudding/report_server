"""콜드 빌드 소요시간 기록 (2026-08-04).

콜드 빌드가 한번씩 300초 가까이 걸리는데 **왜** 오래 걸렸는지 볼 기록이 없었다.
service.py 의 `report cold build ... %.1fs` 로그는 **인라인 분기 전용**이라, 운영의
정상 경로인 워커 오프로드 빌드는 소요시간이 전혀 남지 않았고, 타임아웃/워커 붕괴도
누적 카운터(compute.STATS)만 있어 "어느 세션이 언제 몇 초에 죽었는지" 를 알 수 없었다.

여기서 완료·실패 빌드 1건 = JSON 1줄로 `server/log/webreport_build_YYYYMMDD.log` 에
남긴다. 레코드에는 단계별 소요(다운로드/디코드/전처리/ai_comment/탭별/직렬화)와
**대기 시간 3종**이 함께 들어간다:

  queue_wait  온디맨드·프리웜·distpack 큐(deque)에서 기다린 시간 — 부모 측정
  pool_wait   워커에 실제로 실행이 시작되기까지 (풀 큐 대기 + 워커 spawn·모듈 재임포트)
  ipc         자식이 끝낸 뒤 payload pickle 반송

compute.run 의 타임아웃(기본 300s)은 **풀 큐 대기까지 포함**해서 재기 때문에, 300초가
계산이 느려서인지 남의 작업에 밀려서인지는 이 3종을 봐야만 구분된다. 그게 이 모듈의
존재 이유다.

측정 규칙:
- 프로세스 **안**의 구간은 perf_counter (단조·고해상도).
- 프로세스 **사이**의 시점 비교(부모 submit ↔ 자식 시작/종료 ↔ 부모 수신)는 time.time().
  perf_counter 는 프로세스마다 기준점이 달라 비교할 수 없다. 시계 튐 방어로 음수는 0 클램프.

전 함수 best-effort — 계측이 빌드를 죽이면 안 되므로 예외를 모두 삼킨다(수집기가 없으면
stage() 는 완전 no-op).

## 실행 중 체크포인트 (sidecar, 2026-08-11)

위 단계 기록은 **빌드가 끝나야** 부모로 돌아온다. 그런데 정작 알고 싶은 300초 타임아웃은
워커가 끝내지 못하고 terminate 되는 경우라, 단계 기록이 통째로 유실됐다 — 실패 레코드에는
"session, 300초, TimeoutError" 뿐이라 어느 단계·어느 source 파일에서 멎었는지 알 수 없었다.

그래서 워커는 단계에 들어갈 때마다 `server/log/build_state_<pid>.json` 을 원자적으로
덮어쓴다(tmp → os.replace). 부모는 타임아웃 시 풀을 버리기 **전에** 그 파일을 읽어
last_stage / last_source 를 실패 레코드에 보존한다. 빌드당 쓰기 십수 회(수백 바이트)라
비용은 무시할 수 있다.
"""
from __future__ import annotations

import collections
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path

LOG_PREFIX = "webreport_build"
STATE_PREFIX = "build_state"
_KEEP_DAYS = float(os.getenv("LOG_KEEP_DAYS", "14") or 14)
_HISTORY_MAX_HOURS = 24 * 14
# 워커가 콜드 빌드 없이 즉답한 왕복은 이 시간 미만이면 기록하지 않는다 (잡음 제거).
_MIN_NOOP_SEC = 1.0

# 파일이 없거나(권한·디스크) 아직 안 만들어졌을 때를 위한 최근 레코드 폴백.
_RECENT = collections.deque(maxlen=100)
_RECENT_LOCK = threading.Lock()

_tls = threading.local()
_last_prune_date = ""


# ── 단계 수집 ────────────────────────────────────────────────────────────────

def start_stages() -> dict:
    """이 스레드의 stage() 수집을 시작하고 누적 dict 를 돌려준다.

    호출부는 **반드시 finally 에서 clear_stages()** 를 부른다 — 큐 소비자 스레드는
    장수명이라 남겨두면 다음 빌드에 이전 단계가 섞인다. 콜드 빌드 구간은 이미
    build_status.begin/end 의 try/finally 안이라 거기에 얹는다.
    """
    stages: dict = {}
    _tls.stages = stages
    return stages


def clear_stages() -> None:
    _tls.stages = None


@contextmanager
def collecting():
    """start/clear 의 with 판 (중첩 안전)."""
    prev = getattr(_tls, "stages", None)
    stages = start_stages()
    try:
        yield stages
    finally:
        _tls.stages = prev


@contextmanager
def stage(name: str, source: str = ""):
    """단계 소요를 누적한다. 수집기가 없으면 소요 누적은 no-op (getattr 1회 비용).

    source 는 그 단계가 다루는 파일(basename) — decode/preprocess 처럼 source 를 순회하는
    단계에서 "몇 번째 파일에서 멎었나"를 남기기 위한 것이다. 체크포인트 기록은 수집기
    유무와 무관하게(워커 안이면) 항상 돈다 — 타임아웃 진단이 그것에 달려 있다.
    """
    _state_enter(name, source)
    stages = getattr(_tls, "stages", None)
    if stages is None:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        try:
            stages[name] = round(stages.get(name, 0.0) + (time.perf_counter() - t0), 3)
        except Exception:
            pass


# ── 실행 중 체크포인트 (워커 sidecar) ────────────────────────────────────────
# ProcessPoolExecutor 워커는 한 번에 잡 1개만 돌리므로 프로세스 전역 상태로 충분하다
# (스레드 분리 불필요). 중첩 잡(prewarm_job → report_job)은 depth 로 최외곽만 기록한다.

_JOB = {"depth": 0, "build_id": "", "kind": "", "session": "",
        "stage": "", "source": "", "t_stage": 0.0, "t0": 0.0, "done": {}}


def _in_worker() -> bool:
    try:
        from . import compute
        return bool(compute._IN_WORKER)
    except Exception:
        return False


def _state_path(pid=None) -> Path:
    return _log_dir() / f"{STATE_PREFIX}_{pid or os.getpid()}.json"


def _write_state() -> None:
    """sidecar 원자적 갱신 — 부모가 언제 읽어도 반쪽 JSON 을 보지 않게 tmp+replace."""
    try:
        p = _state_path()
        tmp = p.with_suffix(".tmp")
        now = time.time()
        rec = {"build_id": _JOB["build_id"], "pid": os.getpid(), "kind": _JOB["kind"],
               "session": _JOB["session"], "stage": _JOB["stage"],
               "source": _JOB["source"], "stage_elapsed": round(now - _JOB["t_stage"], 3),
               "elapsed": round(now - _JOB["t0"], 3),
               "stages_done": _JOB["done"],
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))}
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def _state_enter(name: str, source: str = "") -> None:
    if _JOB["depth"] <= 0 or not _in_worker():
        return
    try:
        prev, t_prev = _JOB["stage"], _JOB["t_stage"]
        if prev:
            _JOB["done"][prev] = round(_JOB["done"].get(prev, 0.0)
                                       + (time.time() - t_prev), 3)
        _JOB["stage"] = str(name)[:40]
        _JOB["source"] = str(source or "")[:120]
        _JOB["t_stage"] = time.time()
        _write_state()
    except Exception:
        pass


def checkpoint(name: str, source: str = "") -> None:
    """소요 누적 없이 체크포인트만 갱신한다.

    stage() 안에서 source 를 바꿔가며 진행 상황만 남기고 싶을 때 쓴다 — 같은 이름으로
    stage() 를 중첩하면 바깥과 안쪽이 **같은 키에 두 번 더해져** 소요가 2배로 부풀고
    eta 배율 학습까지 망가진다."""
    _state_enter(name, source)


def begin_job(kind: str, session_id: str) -> str:
    """워커 잡 시작 — sidecar 를 만들고 build_id 를 돌려준다 (부모/워커 공통 상관 키).

    워커 밖(부모 인라인 실행)에서는 아무것도 하지 않는다 — 부모가 hang 하면 sidecar 가
    아니라 diag_listener 스레드 덤프가 진단 수단이다."""
    if not _in_worker():
        return ""
    try:
        _JOB["depth"] += 1
        if _JOB["depth"] > 1:
            return _JOB["build_id"]
        _JOB.update({"build_id": f"{os.getpid()}-{secrets.token_hex(3)}",
                     "kind": str(kind)[:20], "session": str(session_id)[:64],
                     "stage": "", "source": "", "t0": time.time(),
                     "t_stage": time.time(), "done": {}})
        _write_state()
        return _JOB["build_id"]
    except Exception:
        return ""


def end_job() -> None:
    """워커 잡 종료 — sidecar 제거 (남아 있으면 부모가 '진행 중'으로 오독한다)."""
    if not _in_worker():
        return
    try:
        _JOB["depth"] = max(0, _JOB["depth"] - 1)
        if _JOB["depth"] > 0:
            return
        _JOB["build_id"] = ""
        _state_path().unlink(missing_ok=True)
    except Exception:
        pass


def current_build_id() -> str:
    return _JOB["build_id"] if _JOB["depth"] > 0 else ""


def read_states(pids=None) -> list[dict]:
    """워커 sidecar 읽기 — pids 를 주면 그 프로세스 것만. 부모에서만 부른다."""
    out = []
    try:
        want = {int(p) for p in pids} if pids else None
        for p in _log_dir().glob(f"{STATE_PREFIX}_*.json"):
            try:
                pid = int(p.stem.split("_")[-1])
            except ValueError:
                continue
            if want is not None and pid not in want:
                continue
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    except Exception:
        pass
    return out


def drop_states(pids) -> None:
    """지정한 pid 의 sidecar 만 지운다 (terminate 한 워커의 잔해 정리).

    "그 외 전부 삭제"가 아니라 **대상 지정**인 이유: 풀을 버린 직후 다른 스레드가 새 풀을
    띄웠다면, 싹 지우는 방식은 지금 막 시작한 남의 빌드 체크포인트까지 날린다."""
    for pid in (pids or ()):
        try:
            _state_path(pid).unlink(missing_ok=True)
        except Exception:
            pass


def clear_states(keep_pids=None) -> list[dict]:
    """살아있지 않은 워커의 sidecar 를 걷어내고 그 내용을 돌려준다.

    기동 시 호출하면 재기동 직전에 돌던 빌드(= interrupted)가 무엇이었는지 알 수 있고,
    타임아웃으로 워커를 terminate 한 뒤 호출하면 그 잔해를 정리한다."""
    stale = []
    keep = {int(p) for p in (keep_pids or ())}
    try:
        for p in _log_dir().glob(f"{STATE_PREFIX}_*.json"):
            try:
                pid = int(p.stem.split("_")[-1])
            except ValueError:
                pid = -1
            if pid in keep:
                continue
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(rec, dict):
                    stale.append(rec)
            except Exception:
                pass
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass
    return stale


@contextmanager
def context(**kw):
    """큐 소비자 스레드가 trigger/queue_wait 를 이 스레드의 레코드에 붙인다."""
    prev = getattr(_tls, "ctx", None)
    _tls.ctx = dict(kw)
    try:
        yield
    finally:
        _tls.ctx = prev


# ── 레코드 싱크 ──────────────────────────────────────────────────────────────

def _log_dir() -> Path:
    # REPORT_DIAG_DIR 은 테스트가 운영 로그를 오염시키지 않도록 하는 우회로다
    # (server/diagnostics.py 와 같은 변수를 공유 — 진단 기록은 한 폴더에 모인다).
    override = os.getenv("REPORT_DIAG_DIR", "").strip()
    if override:
        d = Path(override)
    else:
        import config
        d = Path(config.ROOT_DIR) / "server" / "log"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune(log_dir: Path, date: str) -> None:
    """날짜가 바뀔 때만 보관기간 경과 파일 정리 (compute._prune_worker_fault_files 패턴)."""
    global _last_prune_date
    if date == _last_prune_date:
        return
    _last_prune_date = date
    try:
        cutoff = time.time() - _KEEP_DAYS * 86400
        for p in log_dir.glob(f"{LOG_PREFIX}_*.log"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except Exception:
                pass
    except Exception:
        pass


def record(rec: dict) -> None:
    """완료/실패 빌드 1건 기록 — 부모 프로세스에서만 부른다.

    빌드 1건당 1회라 open-append-close 로 충분하다(핸들 상시 보유 없이 외부 삭제와 무충돌).
    """
    try:
        ctx = getattr(_tls, "ctx", None)
        if ctx:
            for k, v in ctx.items():
                rec.setdefault(k, v)
        ts = time.time()
        lt = time.localtime(ts)
        rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S", lt))
        with _RECENT_LOCK:
            _RECENT.append(rec)
        if _KEEP_DAYS <= 0:
            return
        date = time.strftime("%Y%m%d", lt)
        log_dir = _log_dir()
        _prune(log_dir, date)
        line = json.dumps(rec, ensure_ascii=False)
        with (log_dir / f"{LOG_PREFIX}_{date}.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def finish(rec: dict) -> None:
    """인라인 빌드 종료 시 호출. 워커 안이면 부모로 실어보내도록 스태시한다."""
    try:
        from . import compute
        if compute._IN_WORKER:
            _tls.stash = rec
            return
    except Exception:
        pass
    record(rec)


def pop_stash() -> dict | None:
    """워커 잡 래퍼가 스태시된 타이밍을 회수한다 (없으면 None)."""
    rec = getattr(_tls, "stash", None)
    _tls.stash = None
    return rec


def _pos(v) -> float:
    return round(max(0.0, float(v)), 3)


def record_offloaded(kind: str, session_id: str, akey, t_submit: float, t_recv: float,
                     child_timing: dict | None, *, result: str = "ok") -> None:
    """워커 오프로드 빌드 1건 기록 — 부모가 잰 왕복 시간 + 자식이 실어보낸 단계 시간.

    child_timing 이 None 이면 워커가 콜드 빌드 없이 즉답한 것이다(워커 자기 RAM/디스크
    캐시 히트 — 부모만 캐시를 잃은 경우). 그래도 **오래 걸렸다면 기록한다** — 300초의
    정체가 계산이 아니라 대기였다는 증거가 바로 그 레코드다. 1초 미만은 잡음이라 버린다.
    """
    try:
        total = _pos(t_recv - t_submit)
        if child_timing is None and total < _MIN_NOOP_SEC:
            return
        rec = {"kind": kind, "session": session_id, "akey": str(akey or "")[:12],
               "offloaded": True, "result": result, "total": total}
        t = child_timing or {}
        t_start, t_end = t.get("t_start"), t.get("t_end")
        if t_start and t_end:
            rec["pool_wait"] = _pos(t_start - t_submit)
            rec["build"] = _pos(t_end - t_start)
            rec["ipc"] = _pos(t_recv - t_end)
        else:
            rec["note"] = "worker cache hit (콜드 빌드 없음 — 전부 대기)"
        for k in ("stages", "sources", "items", "mcells", "kcols"):
            if t.get(k) is not None:
                rec[k] = t[k]
        record(rec)
    except Exception:
        pass


_JOB_KIND = {"report_job": "report", "dist_job": "dist", "map_job": "map",
             "prewarm_job": "report", "dist_pack_job": "dist_pack",
             "trim_job": "trim", "trim_chart_batch_job": "trim"}


def _session_meta(session_id: str) -> dict:
    """실패 레코드에 붙일 세션 메타 (실패했을 때만 도는 경로라 DB 왕복 1회는 무해).

    session_id 만으로는 "무엇이 죽었는지" 를 사람이 못 읽는다 — 제품·LOT·파일명이
    있어야 관리자가 같은 데이터를 재현하거나 사용자에게 되물을 수 있다."""
    if not session_id:
        return {}
    try:
        from database import report_db
        s = report_db.get_session(session_id) or {}
        out = {"akey": str(s.get("analysis_key") or "")[:12],
               "product": s.get("product") or "", "lot_id": s.get("lot_id") or "",
               "file_name": s.get("file_name") or ""}
        return {k: v for k, v in out.items() if v}
    except Exception:
        return {}


def record_failure(job_name: str, args, result: str, elapsed: float,
                   error_text: str = "", state: dict | None = None) -> None:
    """compute.run 의 타임아웃/워커 붕괴/예외를 기록한다 (그걸 아는 곳이 거기뿐).

    state 는 죽은 워커의 sidecar(read_states) — 있으면 마지막 단계·source 가 함께
    남아 "300초를 어디서 썼나"에 답할 수 있다. 없으면(= 큐에서 대기만 하다 죽음)
    그 부재 자체가 '대기 타임아웃'이라는 증거다.
    """
    try:
        session = args[0] if args and isinstance(args[0], str) else ""
        rec = {"kind": _JOB_KIND.get(job_name, job_name), "session": session,
               "offloaded": True, "result": result, "total": _pos(elapsed),
               "error": str(error_text)[:200]}
        rec.update(_session_meta(session))
        if state:
            rec["build_id"] = state.get("build_id") or ""
            rec["last_stage"] = state.get("stage") or ""
            rec["last_source"] = state.get("source") or ""
            rec["last_stage_elapsed"] = state.get("stage_elapsed")
            rec["build_elapsed"] = state.get("elapsed")
            if state.get("stages_done"):
                rec["stages"] = state["stages_done"]
        else:
            rec["last_stage"] = ""      # 명시적 부재 = 워커가 시작조차 못 함(큐 대기)
        record(rec)
    except Exception:
        pass


# ── 조회 (관리자 API) ────────────────────────────────────────────────────────

def _epoch(text) -> float:
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError, TypeError):
        return 0.0


def history(hours: int = 24, limit: int = 100) -> list[dict]:
    """최근 콜드 빌드 기록 — 최신순. 파일이 없으면 메모리 폴백."""
    try:
        hours = max(1, min(int(hours), _HISTORY_MAX_HOURS))
    except (TypeError, ValueError):
        hours = 24
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    now = time.time()
    cutoff = now - hours * 3600
    out: list[dict] = []
    try:
        log_dir = _log_dir()
        days = int(hours // 24) + 2
        for d in range(days):
            name = time.strftime(f"{LOG_PREFIX}_%Y%m%d.log", time.localtime(now - d * 86400))
            path = log_dir / name
            if not path.exists():
                continue
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
                        if _epoch(rec.get("ts")) < cutoff:
                            continue
                        out.append(rec)
            except OSError:
                pass
    except Exception:
        pass
    if not out:
        with _RECENT_LOCK:
            out = [r for r in _RECENT if _epoch(r.get("ts")) >= cutoff]
    out.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return out[:limit]

# -*- coding: utf-8 -*-
"""업로드 직후 AI Comment [제안] 클라 대행 워커 (docs/23 Phase 4).

서버(자격증명 없음) 대신 이 PC 의 로컬 Claude Code CLI(`claude -p`, Enterprise gateway
인증)로 [제안] 문장을 생성해 서버에 push 한다. 흐름:

    업로드 성공 → start_background()  (daemon 스레드, 즉시 반환)
      1) GET  .../web_report/ai_comment/prompts   (202 재폴링 — 서버 'ai' 잡 대기)
      2) call_claude.run_batch()  — 프롬프트 N건 = subprocess 1회, **배치 4개 병렬**
      3) POST .../web_report/ai_comment/suggestions — **배치가 끝나는 대로 즉시**

2026-09-02 개편: 종전엔 배치를 완전 순차로 돌리고 전부 모아 마지막에 한 번 push 해서,
100건 세션이 7~10분 걸리고 그동안 화면에는 아무 변화가 없었다. 지금은 배치를 병렬로
돌리고 끝나는 대로 보내, 서버가 행별 대기 상태를 내려주는 화면이 점진적으로 채워진다.

원칙:
- **모든 예외를 삼킨다** — 실패해도 서버 폴백(action_ko 문장)이 이미 있어 무해하다.
  업로드 완료 흐름·UI 를 절대 막지 않는다(Qt 위젯 접근 금지 — 순수 스레드).
- 구 서버(라우트 없음)는 404 로 조용히 포기한다. 신 서버 + AI Model=default 세션도 404.
- Gateway 토큰·자격증명은 서버로 보내지 않는다 — 서버와는 프롬프트/문장만 오간다.

honey.env 선택 키 (전부 없어도 동작):
    HONEY_CLAUDE_BIN        claude 실행 파일 경로 (기본: PATH 등 자동 탐색 — call_claude)
    HONEY_CLAUDE_MODEL      --model 값 (기본: claude-sonnet-5)
    HONEY_CLAUDE_TIMEOUT    배치 1회 subprocess 상한 초 (기본 240)
    HONEY_CLAUDE_BATCH      배치당 프롬프트 수 (기본 10)
    HONEY_CLAUDE_PARALLEL   동시 실행 배치 수 (기본 4, 상한 5 — PC 부하와의 절충)
    HONEY_CLAUDE_MAX_ITEMS  세션당 처리 상한 (기본 100 — 초과분은 폴백 유지)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# requests 는 클라 실행 환경에 항상 있다(requirements.txt). import 실패를 삼키는 이유는
# 서버측 테스트가 이 모듈을 requests 없는 venv 에서 import 해 오케스트레이션만 검증하기
# 때문 — 그 경우 테스트가 requests 속성을 가짜로 갈아끼운다. 실행 시 None 이면 워커가
# 조용히 종료한다(부가 기능 계약).
try:
    import requests
except ImportError:      # noqa: BLE001
    requests = None

from .config import REQUEST_TIMEOUT_SEC, SERVER_BASE_URL, env_value

_log = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 3       # prompts 202 재폴링 간격
_POLL_MAX_SEC = 300          # prompts 대기 상한 (서버 'ai' 잡)
_HARD_DEADLINE_SEC = 20 * 60  # 워커 전체 벽시계 상한
_PUSH_RETRY_WAIT_SEC = 10    # POST 202(aicmt 축출 레이스) 재시도 대기
_CLI_LOG_KEEP = 6            # 실패 보고에 실을 call_claude 로그 줄 수 상한

# HTTP 상태 → 사용자가 읽고 **다음에 뭘 할지 알 수 있는** 한 줄 (2026-09-01).
# 종전에는 워커 실패가 전부 조용해서 사용자는 "AI 문장이 왜 안 왔는지" 를 알 방법이
# 없었다(화면엔 룰 문장이 정상처럼 나온다). 숫자만 보여 주는 것도 같은 문제라 —
# 404 를 보고 무엇을 해야 하는지 아는 사용자는 없다 — 조치를 함께 적는다.
_HTTP_HINT = {
    401: "서버 인증이 필요합니다 (Honey 를 다시 실행해 보세요)",
    403: "이 세션을 편집할 권한이 없습니다 (업로더 본인 PC 인지 확인)",
    404: "서버가 이 기능을 모릅니다 (서버 버전이 낮거나 AI Model=claude 세션이 아님)",
    413: "보낼 내용이 너무 큽니다",
    423: "세션이 잠겨 있습니다 (다른 작업이 끝난 뒤 재시도)",
    429: "요청이 너무 많습니다 (잠시 후 자동 재시도)",
    500: "서버 내부 오류입니다 (관리자에게 event_id 를 알려 주세요)",
    502: "서버에 연결할 수 없습니다 (네트워크·프록시 확인)",
    503: "서버가 바쁩니다 (잠시 후 재시도)",
    504: "서버 응답이 지연됩니다 (네트워크 확인)",
}


def http_hint(status) -> str:
    """HTTP 상태 코드 → 사용자용 안내 문장. 모르는 코드는 코드만 돌려준다."""
    try:
        code = int(status or 0)
    except (TypeError, ValueError):
        return ""
    if not code:
        return "서버에 요청을 보내지 못했습니다 (네트워크·주소 확인)"
    hint = _HTTP_HINT.get(code)
    if hint:
        return f"HTTP {code} — {hint}"
    if 500 <= code < 600:
        return f"HTTP {code} — 서버 오류입니다 (잠시 후 재시도)"
    if 400 <= code < 500:
        return f"HTTP {code} — 요청이 거부됐습니다"
    return f"HTTP {code}"


def _notify(on_progress, text: str) -> None:
    """진행/실패 한 줄을 호출부(Honey UI 실행 로그)로 — 콜백 실패는 무시한다.

    워커는 UI 를 절대 만지지 않는다는 규약을 유지하려고, 문자열만 넘기고 위젯 접근은
    호출부에 맡긴다(honey_main 이 시그널로 UI 스레드에 넘긴다).
    """
    _log.info("%s", text)
    if on_progress is None:
        return
    try:
        on_progress(str(text))
    except Exception:  # noqa: BLE001 — 알림 실패가 본 흐름을 막지 않는다
        pass

# 기본 모델 — 별칭('sonnet')이 아니라 **정식명**을 쓴다. 별칭은 "최신 sonnet" 을 가리켜
# 새 버전이 나오면 말없이 바뀌는데, 이 값은 프롬프트 sha 와 무관해 캐시로도 안 걸린다
# (= 같은 세션이 어제와 다른 모델로 생성될 수 있다). 사용자가 바꾸려면 honey.env
# HONEY_CLAUDE_MODEL. 2026-08-28 CLI 2.1.247 에서 두 표기 다 실호출 확인.
DEFAULT_MODEL = "claude-sonnet-5"

# 상태 점검(check_status) — UI 신호등용이라 짧게 끊는다. 사용자가 버튼을 누르고
# 기다리는 경로이므로 워커의 240초 상한을 그대로 쓰면 안 된다.
_STATUS_PROBE_SEC = 20
_STATUS_CALL_SEC = 45
_STATUS_PROMPT = "ok 라고만 답하라."


def _import_call_claude():
    """최상위 패키지 call_claude import — dev 실행(cwd=client/) 경로 보정 포함.

    빌드본은 build_honey.spec 의 pathex/hiddenimports 가 수집한다. 개발 실행은
    sys.path 에 repo 루트가 없어 실패하므로 config._env_file_paths 와 같은 산식으로
    루트를 덧붙여 재시도한다.
    """
    try:
        import call_claude
        return call_claude
    except ImportError:
        if getattr(sys, "frozen", False):
            raise
        repo_root = str(Path(__file__).resolve().parent.parent.parent)
        if repo_root not in sys.path:
            sys.path.append(repo_root)
        import call_claude
        return call_claude


def _resolve_cli():
    """(call_claude 모듈, 실행 파일 경로|None) — honey.env HONEY_CLAUDE_BIN 반영.

    워커와 상태 점검(check_status)이 **같은 경로**로 CLI 를 찾아야 한다 — 신호등이
    초록인데 실제 대행은 CLI 를 못 찾는 상황을 원천 차단한다.
    """
    call_claude = _import_call_claude()
    bin_override = env_value("HONEY_CLAUDE_BIN")
    env = dict(os.environ)
    if bin_override:
        env[call_claude.ENV_BIN] = str(bin_override)
    return call_claude, call_claude.find_cli(env)


def check_status(timeout=None) -> dict:
    """지금 이 PC 에서 Claude 호출이 되는가 — {"ok", "detail", "model", "version"}.

    **실호출 1회로 판정한다.** `probe`(--version/--help)만으로는 인증·게이트웨이·정책을
    알 수 없어서, 바이너리만 있으면 초록이 켜지는 거짓 신호가 된다(현장에서 가장 알고
    싶은 것이 바로 그 인증 여부다). 그래서 아주 짧은 프롬프트를 한 번 실제로 보낸다.

    UI 스레드에서 부르지 말 것 — 수 초 걸린다(호출부가 워커 스레드에서 실행한다).
    """
    out = {"ok": False, "detail": "", "model": "", "version": ""}
    try:
        call_claude, bin_path = _resolve_cli()
        if not bin_path:
            out["detail"] = "claude 실행 파일을 찾지 못했습니다 (PATH 또는 HONEY_CLAUDE_BIN)"
            return out
        info = call_claude.probe(bin_path=bin_path, timeout=_STATUS_PROBE_SEC)
        out["version"] = str(info.get("version") or "")
        if not info.get("ok"):
            out["detail"] = f"claude 실행 확인 실패 ({info.get('error') or 'unknown'})"
            return out
        model = env_value("HONEY_CLAUDE_MODEL") or DEFAULT_MODEL
        out["model"] = model
        logs: list = []
        reply = call_claude.run_prompt(
            _STATUS_PROMPT, bin_path=bin_path, model=model,
            timeout=float(timeout or _STATUS_CALL_SEC),
            log=lambda m: logs.append(str(m)[:200]))
        if reply:
            out["ok"] = True
            out["detail"] = f"{model} 응답 확인 ({out['version'] or 'ver?'})"
        else:
            # 여기가 현장에서 가장 흔한 실패다 — 바이너리는 있는데 인증·정책으로 막힌 상태.
            out["detail"] = ("Claude 호출에 실패했습니다 (인증·정책·네트워크 확인). "
                             + (" | ".join(logs)[-200:] if logs else ""))
        return out
    except Exception as exc:  # noqa: BLE001 — 점검이 UI 를 죽이면 안 된다
        out["detail"] = f"{type(exc).__name__}: {exc}"
        return out


def _report_failure(kind: str, message: str, session_id: str, context: dict) -> None:
    """실패 1건을 서버 진단 사건으로 보고 — 기존 Honey 오류 보고 경로 재사용.

    성공은 보고하지 않는다: 서버가 push 를 감사 로그로 이미 알고 있고, 진단 JSONL 이
    성공 기록으로 부풀면 정작 실패가 묻힌다. 보고 자체가 실패해도 무음이다
    (`report_error` 계약) — 이 함수 때문에 워커가 죽으면 안 된다.
    """
    try:
        from .error_report import report_error
        report_error(kind, message, context=context, session_id=session_id)
    except Exception:  # noqa: BLE001 — 보고 실패가 본 흐름을 막지 않는다
        pass


def _headers():
    """JSON 헤더 + HoneyUser UA(_editor_guard 신원) + X-Honey-Agent(CSRF 대체).

    uploader import 를 지연하는 이유: uploader 는 requests_toolbelt 를 정적 import 하는데,
    이 모듈은 워커 스레드 밖(기동 게이트)에서도 import 되므로 의존을 얇게 유지한다.
    """
    from .uploader import _upload_headers
    headers = _upload_headers("application/json")
    headers["X-Honey-Agent"] = "1"
    return headers


def _fetch_prompts(base: str, session_id: str, headers: dict, on_progress=None):
    """prompts 폴링 — (items|None, reason, status). 202 는 재시도, 404/403 은 3회 후 포기.

    reason 은 실패 사유 문자열("denied"/"timeout"/"badbody")이며 성공 시 "". 관리자
    모니터링(진단 사건)이 "왜 못 받았나"를 구분하려면 None 하나로는 부족하다.
    status 는 마지막 HTTP 상태 — 사용자 안내(`http_hint`)에 쓴다.
    """
    url = f"{base}/pe/report/session/{session_id}/web_report/ai_comment/prompts"
    deadline = time.monotonic() + _POLL_MAX_SEC
    denied = 0
    last_status = 0
    waited_notified = False
    while time.monotonic() < deadline:
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 — 네트워크 오류는 재시도로 흡수
            _log.info("ai_suggest prompts 요청 실패(재시도): %s", exc)
            time.sleep(_POLL_INTERVAL_SEC)
            continue
        last_status = resp.status_code
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return None, "badbody", last_status
            items = data.get("items")
            if isinstance(items, list):
                return items, "", last_status
            return None, "badbody", last_status
        if resp.status_code == 202:
            # 서버가 아직 평가 중 — 길어지면 한 번만 알린다(반복 알림은 로그를 덮는다).
            if not waited_notified:
                waited_notified = True
                _notify(on_progress, "AI Comment: 서버 평가를 기다리는 중…")
            time.sleep(_POLL_INTERVAL_SEC)
            continue
        if resp.status_code in (403, 404):
            # 구 서버(라우트 없음)/대상 아님/권한 문제 — 몇 번 더 확인 후 포기.
            denied += 1
            if denied >= 3:
                _log.info("ai_suggest 포기: HTTP %s (구 서버 또는 대상 아님)",
                          resp.status_code)
                return None, "denied", last_status
            time.sleep(_POLL_INTERVAL_SEC)
            continue
        # 5xx 등 — 재시도
        time.sleep(_POLL_INTERVAL_SEC)
    _log.info("ai_suggest prompts 대기 시간 초과 — 포기 (session=%s)", session_id)
    return None, f"timeout(last={last_status})", last_status


def _post_suggestions(base: str, session_id: str, headers: dict, items: list):
    """suggestion push — (성공 여부, 마지막 status). 202(aicmt 축출 레이스)면 1회 재시도.

    merge 가 멱등이라 재시도가 안전하다. status 를 함께 돌려주는 이유는 실패 보고에
    "무엇 때문에 거부됐나"를 실어야 하기 때문(0 = 요청 자체가 안 나감)."""
    url = f"{base}/pe/report/session/{session_id}/web_report/ai_comment/suggestions"
    body = json.dumps({"items": items}, ensure_ascii=False)
    last_status = 0
    for attempt in (1, 2):
        try:
            resp = requests.post(url, data=body.encode("utf-8"), headers=headers,
                                 timeout=(10, 60))
        except Exception as exc:  # noqa: BLE001
            _log.info("ai_suggest push 실패(%d회): %s", attempt, exc)
            time.sleep(_PUSH_RETRY_WAIT_SEC)
            continue
        last_status = resp.status_code
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            _log.info("ai_suggest push 완료: accepted=%s skipped=%s (session=%s)",
                      data.get("accepted"), data.get("skipped"), session_id)
            return True, last_status
        if resp.status_code == 202:
            time.sleep(_PUSH_RETRY_WAIT_SEC)
            continue
        _log.info("ai_suggest push 거부: HTTP %s (session=%s)",
                  resp.status_code, session_id)
        return False, last_status
    return False, last_status


def _worker(session_id: str, base_url: str, on_progress=None) -> None:
    """백그라운드 본체 — 어떤 실패도 조용히 끝난다(폴백 무해 계약).

    단 **사유는 두 곳에 남긴다**:
    - 서버 진단 사건(`_report_failure`) — 관리자가 "왜 룰 문장인지" 를 볼 근거.
    - `on_progress` 콜백 — **사용자가 보는 실행 로그** 한 줄(2026-09-01). 종전에는
      워커가 전부 조용해서, 화면에 룰 문장이 정상처럼 나오는 탓에 사용자는 실패를
      알아챌 방법이 아예 없었다. 콜백은 문자열만 넘긴다(위젯 접근 금지 규약 유지).
    """
    started = time.monotonic()
    cli_log: list = []          # call_claude 가 남긴 마지막 줄들 — 실패 보고의 핵심 단서
    try:
        if requests is None:
            _log.info("ai_suggest 포기: requests 모듈 없음")
            return
        call_claude, bin_path = _resolve_cli()
        if not bin_path:
            _notify(on_progress,
                    "AI Comment 대행 실패: claude 실행 파일을 찾지 못했습니다 "
                    "(PATH 또는 honey.env 의 HONEY_CLAUDE_BIN 확인)")
            _report_failure("ai_suggest_no_cli",
                            "claude CLI 를 찾지 못했습니다 (HONEY_CLAUDE_BIN/PATH 확인)",
                            session_id,
                            {"bin_hint": "set" if env_value("HONEY_CLAUDE_BIN") else "none"})
            return
        headers = _headers()
        base = (base_url or SERVER_BASE_URL).rstrip("/")
        items, reason, status = _fetch_prompts(base, session_id, headers, on_progress)
        if not items:
            if reason:
                hint = http_hint(status) if reason == "denied" else ""
                if reason == "timeout":
                    hint = "서버 평가가 제한 시간 안에 끝나지 않았습니다"
                elif reason == "badbody":
                    hint = "서버 응답 형식이 예상과 다릅니다"
                _notify(on_progress,
                        "AI Comment 대행 실패: 서버에서 프롬프트를 받지 못했습니다"
                        + (f" — {hint}" if hint else f" ({reason})"))
                _report_failure("ai_suggest_no_prompts",
                                f"서버에서 프롬프트를 받지 못했습니다 ({reason})",
                                session_id, {"reason": reason, "status": status})
            return
        _notify(on_progress, f"AI Comment: {len(items)}개 항목 문장 생성 중…")
        # 상한 대상 = **선례가 있는 item 만**이다(서버 build_prompts 가 선례 0건이면
        # 프롬프트를 안 만든다) — 이슈 전체 수가 아니라 "사례가 붙은 항목" 수다.
        # 50 → 100 (2026-09-02): 이슈가 많은 세션에서 초과분이 조용히 룰 문장으로
        # 남는 신고가 있었다. 초과 건수는 아래에서 사용자·진단에 남긴다.
        max_items = int(env_value("HONEY_CLAUDE_MAX_ITEMS", "100") or 100)
        dropped = 0
        if len(items) > max_items:
            dropped = len(items) - max_items
            _log.info("ai_suggest 상한 초과: %d/%d 건만 처리 (초과분 폴백 유지)",
                      max_items, len(items))
            # 화면에 아무 흔적이 없으면 사용자는 "왜 이 항목만 룰 문장인지" 알 수 없다.
            _notify(on_progress,
                    f"AI Comment: 항목이 많아 {max_items}건만 처리합니다 "
                    f"({dropped}건은 기본 문장 유지 — HONEY_CLAUDE_MAX_ITEMS 로 조정)")
            items = items[:max_items]
        model = env_value("HONEY_CLAUDE_MODEL") or DEFAULT_MODEL
        timeout = float(env_value("HONEY_CLAUDE_TIMEOUT", "240") or 240)
        batch_size = max(1, int(env_value("HONEY_CLAUDE_BATCH", "10") or 10))
        # 배치 **병렬 실행** (2026-09-02 사용자 결정). 종전 완전 순차는 10배치 × 40초 =
        # 7~10분이라 사용자가 결과를 볼 때쯤엔 이미 리포트를 닫은 뒤였다. 4 로 나눠 돌면
        # 같은 양이 3 wave ≈ 2분 안팎이다. 상한 5 는 업로더 PC 부하(배치 1개 = node
        # 프로세스 1개)와 게이트웨이 동시 요청을 고려한 안전선이다.
        parallel = max(1, min(5, int(env_value("HONEY_CLAUDE_PARALLEL", "4") or 4)))

        def _cli_log(msg):
            _log.info("%s", msg)
            if len(cli_log) < _CLI_LOG_KEEP:
                cli_log.append(str(msg)[:200])

        chunks = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
        out, batches, pushed, push_fail = [], 0, 0, 0

        def _run_chunk(chunk):
            """배치 1개 = claude CLI subprocess 1회. 스레드에서 병렬로 돈다.

            call_claude 는 상태를 공유하지 않는 순수 subprocess 호출이라 동시 실행이
            안전하다(공유 상태는 --help 캐시 dict 하나뿐이고 값이 같다).
            """
            replies = call_claude.run_batch(
                [row.get("prompt") or "" for row in chunk],
                bin_path=bin_path, model=model, timeout=timeout, log=_cli_log)
            rows = []
            for row, reply in zip(chunk, replies):
                if reply:
                    # suggestion 은 서버가 sanitize 한다 — 여기서는 원문 그대로 보낸다.
                    # 서버가 sanitize 전후를 비교해 "모델이 이상하게 답한 것"과 "서버가
                    # 걷어낸 것"을 관리자 화면에서 구분할 수 있게 하기 위함이다(docs/23).
                    rows.append({"key": row.get("key"), "sha": row.get("sha"),
                                 "suggestion": reply})
            return rows

        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(_run_chunk, c): c for c in chunks}
            try:
                for fut in as_completed(futures):
                    batches += 1
                    try:
                        rows = fut.result()
                    except Exception:               # noqa: BLE001 — 배치 1개 실패는 폴백
                        _log.info("ai_suggest 배치 실패 — 건너뜀", exc_info=True)
                        rows = []
                    out.extend(rows)
                    if rows:
                        # **배치가 끝나는 대로 보낸다** (2026-09-02). 전부 모아 한 번에
                        # 보내면 마지막 배치가 끝날 때까지 화면이 통째로 Loading 이다.
                        # 서버 저장은 항목 단위 upsert(멱등)라 나눠 보내도 안전하고,
                        # 저장할 때마다 payload_rev 가 올라 화면이 점진적으로 채워진다.
                        ok, status = _post_suggestions(base, session_id, headers, rows)
                        if ok:
                            pushed += len(rows)
                        else:
                            push_fail = status
                            _log.info("ai_suggest 배치 push 실패 (HTTP %s) — 마지막에 재시도",
                                      status)
                    _notify(on_progress,
                            f"AI Comment: 배치 {batches}/{len(chunks)} 완료 "
                            f"(누적 {pushed}건 반영)")
                    if time.monotonic() - started > _HARD_DEADLINE_SEC:
                        _log.info("ai_suggest 전체 상한 초과 — 중단 (%d건 완료)", len(out))
                        # 건수 상한과 같은 이유로 사용자에게 남긴다 — 남은 항목이 룰 문장으로
                        # 보이는 것이 "실패"가 아니라 "시간 초과"임을 알 수 있어야 한다.
                        _notify(on_progress,
                                f"AI Comment: 시간 상한({_HARD_DEADLINE_SEC // 60}분)에 걸려 "
                                f"{len(out)}건까지만 생성했습니다 (나머지는 기본 문장 유지)")
                        break
            finally:
                # 남은 배치를 취소한다 — 이미 시작된 subprocess 는 끝까지 가지만(취소 불가)
                # 대기 중인 것은 여기서 버려져 상한을 넘겨 계속 돌지 않는다.
                for fut in futures:
                    fut.cancel()
        if not out:
            # CLI 는 찾았는데 한 건도 못 만든 경우 — 현장에서 인증·정책 실패의 1순위 신호다.
            _notify(on_progress,
                    "AI Comment 대행 실패: Claude 가 문장을 만들지 못했습니다 "
                    "(인증·정책·네트워크 확인 — AI Comment 체크 옆 신호등을 눌러 보세요)"
                    + (f" [{cli_log[-1]}]" if cli_log else ""))
            _report_failure("ai_suggest_empty",
                            "claude CLI 호출에서 결과를 하나도 받지 못했습니다",
                            session_id,
                            {"items": len(items), "batches": batches,
                             "model": model or "(default)",
                             "cli_log": " || ".join(cli_log)[:400]})
            return
        if pushed < len(out):
            # 배치별 push 에서 실패한 잔여분을 한 번에 다시 보낸다(merge 멱등 — 이미 들어간
            # 항목을 같이 보내도 같은 값으로 덮일 뿐이다).
            ok, status = _post_suggestions(base, session_id, headers, out)
            if ok:
                pushed = len(out)
            else:
                push_fail = status
        if not pushed:
            _notify(on_progress,
                    f"AI Comment 대행 실패: 만든 문장 {len(out)}건을 서버에 저장하지 "
                    f"못했습니다 — {http_hint(push_fail)}")
            _report_failure("ai_suggest_push_failed",
                            f"생성한 문장을 서버에 저장하지 못했습니다 (HTTP {push_fail})",
                            session_id, {"status": push_fail, "items": len(out)})
            return
        # 성공 — 사용자는 여기서 처음으로 "대행이 실제로 됐다"를 안다. 화면은 서버가 행별
        # 대기 상태(ai_llm_pending)를 내려주므로 열어 둔 리포트가 스스로 채워진다
        # (2026-09-02 — boot.js 폴링이 배치별 push 를 따라간다). 옛 화면·구서버 대비로
        # 새로고침 안내는 남긴다.
        _notify(on_progress,
                f"AI Comment 대행 완료: {pushed}건 반영됨 "
                "(리포트 화면이 곧 자동 갱신됩니다 — 안 보이면 새로고침)")
    except Exception as exc:  # noqa: BLE001 — 부가 기능: 예외가 업로드 흐름 밖으로 안 나간다
        _log.info("ai_suggest 워커 예외 — 조용히 종료 (session=%s)",
                  session_id, exc_info=True)
        _notify(on_progress,
                f"AI Comment 대행 중 오류가 발생했습니다 ({type(exc).__name__})")
        _report_failure("ai_suggest_worker_error", f"{type(exc).__name__}: {exc}",
                        session_id, {"error_type": type(exc).__name__})


def start_background(session_id: str, options: dict, base_url: str | None = None,
                     on_progress=None) -> bool:
    """업로드 성공 직후 호출 — 옵트인 세션에만 daemon 스레드 기동, 즉시 반환.

    게이트: options 의 ai_comment_optin + ai_model=="claude" (둘 다 참일 때만).
    반환은 기동 여부(로그·테스트용) — 호출부는 결과를 기다리지 않는다.

    `on_progress(str)` 는 워커 스레드에서 호출되는 진행/실패 알림 콜백이다(선택).
    **호출부가 UI 스레드로 넘길 책임을 진다** — 워커는 위젯을 만지지 않는다.
    """
    try:
        opts = options or {}
        if not (opts.get("ai_comment_optin") and opts.get("ai_model") == "claude"):
            return False
        sid = str(session_id or "").strip()
        if not sid or sid == "?":
            return False
        threading.Thread(target=_worker,
                         args=(sid, base_url or SERVER_BASE_URL, on_progress),
                         name="ai-suggest", daemon=True).start()
        return True
    except Exception:  # noqa: BLE001
        return False

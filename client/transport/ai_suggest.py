# -*- coding: utf-8 -*-
"""업로드 직후 AI Comment [제안] 클라 대행 워커 (docs/23 Phase 4).

서버(자격증명 없음) 대신 이 PC 의 로컬 Claude Code CLI(`claude -p`, Enterprise gateway
인증)로 [제안] 문장을 생성해 서버에 push 한다. 흐름:

    업로드 성공 → start_background()  (daemon 스레드, 즉시 반환)
      1) GET  .../web_report/ai_comment/prompts   (202 재폴링 — 서버 'ai' 잡 대기)
      2) call_claude.run_batch()  — 프롬프트 N건 = subprocess 1회 (순차, 병렬 없음)
      3) POST .../web_report/ai_comment/suggestions

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
    HONEY_CLAUDE_MAX_ITEMS  세션당 처리 상한 (기본 50 — 초과분은 폴백 유지)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
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


def _fetch_prompts(base: str, session_id: str, headers: dict):
    """prompts 폴링 — (items|None, reason). 202 는 재시도, 404/403 은 3회 후 포기.

    reason 은 실패 사유 문자열("denied"/"timeout"/"badbody")이며 성공 시 "". 관리자
    모니터링(진단 사건)이 "왜 못 받았나"를 구분하려면 None 하나로는 부족하다.
    """
    url = f"{base}/pe/report/session/{session_id}/web_report/ai_comment/prompts"
    deadline = time.monotonic() + _POLL_MAX_SEC
    denied = 0
    last_status = 0
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
                return None, "badbody"
            items = data.get("items")
            return (items, "") if isinstance(items, list) else (None, "badbody")
        if resp.status_code == 202:
            time.sleep(_POLL_INTERVAL_SEC)
            continue
        if resp.status_code in (403, 404):
            # 구 서버(라우트 없음)/대상 아님/권한 문제 — 몇 번 더 확인 후 조용히 포기.
            denied += 1
            if denied >= 3:
                _log.info("ai_suggest 포기: HTTP %s (구 서버 또는 대상 아님)",
                          resp.status_code)
                return None, "denied"
            time.sleep(_POLL_INTERVAL_SEC)
            continue
        # 5xx 등 — 재시도
        time.sleep(_POLL_INTERVAL_SEC)
    _log.info("ai_suggest prompts 대기 시간 초과 — 포기 (session=%s)", session_id)
    return None, f"timeout(last={last_status})"


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


def _worker(session_id: str, base_url: str) -> None:
    """백그라운드 본체 — 어떤 실패도 조용히 끝난다(폴백 무해 계약).

    단 **실패 사유는 서버 진단 사건으로 보고한다**(`_report_failure`). 로컬 로그만
    남기면 관리자는 "왜 AI Comment 가 룰 문장인지" 를 영영 알 수 없다 — 화면에 에러가
    아니라 그냥 폴백 문장이 나오기 때문에 발견 자체가 늦는다.
    """
    started = time.monotonic()
    cli_log: list = []          # call_claude 가 남긴 마지막 줄들 — 실패 보고의 핵심 단서
    try:
        if requests is None:
            _log.info("ai_suggest 포기: requests 모듈 없음")
            return
        call_claude, bin_path = _resolve_cli()
        if not bin_path:
            _log.info("ai_suggest 포기: claude CLI 없음 (HONEY_CLAUDE_BIN/PATH 확인)")
            _report_failure("ai_suggest_no_cli",
                            "claude CLI 를 찾지 못했습니다 (HONEY_CLAUDE_BIN/PATH 확인)",
                            session_id,
                            {"bin_hint": "set" if env_value("HONEY_CLAUDE_BIN") else "none"})
            return
        headers = _headers()
        base = (base_url or SERVER_BASE_URL).rstrip("/")
        items, reason = _fetch_prompts(base, session_id, headers)
        if not items:
            if reason:
                _report_failure("ai_suggest_no_prompts",
                                f"서버에서 프롬프트를 받지 못했습니다 ({reason})",
                                session_id, {"reason": reason})
            return
        max_items = int(env_value("HONEY_CLAUDE_MAX_ITEMS", "50") or 50)
        if len(items) > max_items:
            _log.info("ai_suggest 상한 초과: %d/%d 건만 처리 (초과분 폴백 유지)",
                      max_items, len(items))
            items = items[:max_items]
        model = env_value("HONEY_CLAUDE_MODEL") or DEFAULT_MODEL
        timeout = float(env_value("HONEY_CLAUDE_TIMEOUT", "240") or 240)
        batch_size = max(1, int(env_value("HONEY_CLAUDE_BATCH", "10") or 10))

        def _cli_log(msg):
            _log.info("%s", msg)
            if len(cli_log) < _CLI_LOG_KEEP:
                cli_log.append(str(msg)[:200])

        out = []
        batches = 0
        for start in range(0, len(items), batch_size):
            if time.monotonic() - started > _HARD_DEADLINE_SEC:
                _log.info("ai_suggest 전체 상한 초과 — 중단 (%d건 완료)", len(out))
                break
            chunk = items[start:start + batch_size]
            batches += 1
            replies = call_claude.run_batch(
                [row.get("prompt") or "" for row in chunk],
                bin_path=bin_path, model=model, timeout=timeout, log=_cli_log)
            for row, reply in zip(chunk, replies):
                if reply:
                    out.append({"key": row.get("key"), "sha": row.get("sha"),
                                "suggestion": reply})
        if not out:
            # CLI 는 찾았는데 한 건도 못 만든 경우 — 현장에서 인증·정책 실패의 1순위 신호다.
            _log.info("ai_suggest 생성 결과 없음 — 폴백 유지 (session=%s)", session_id)
            _report_failure("ai_suggest_empty",
                            "claude CLI 호출에서 결과를 하나도 받지 못했습니다",
                            session_id,
                            {"items": len(items), "batches": batches,
                             "model": model or "(default)",
                             "cli_log": " || ".join(cli_log)[:400]})
            return
        ok, status = _post_suggestions(base, session_id, headers, out)
        if not ok:
            _report_failure("ai_suggest_push_failed",
                            f"생성한 문장을 서버에 저장하지 못했습니다 (HTTP {status})",
                            session_id, {"status": status, "items": len(out)})
    except Exception as exc:  # noqa: BLE001 — 부가 기능: 예외가 업로드 흐름 밖으로 안 나간다
        _log.info("ai_suggest 워커 예외 — 조용히 종료 (session=%s)",
                  session_id, exc_info=True)
        _report_failure("ai_suggest_worker_error", f"{type(exc).__name__}: {exc}",
                        session_id, {"error_type": type(exc).__name__})


def start_background(session_id: str, options: dict, base_url: str | None = None) -> bool:
    """업로드 성공 직후 호출 — 옵트인 세션에만 daemon 스레드 기동, 즉시 반환.

    게이트: options 의 ai_comment_optin + ai_model=="claude" (둘 다 참일 때만).
    반환은 기동 여부(로그·테스트용) — 호출부는 결과를 기다리지 않는다.
    """
    try:
        opts = options or {}
        if not (opts.get("ai_comment_optin") and opts.get("ai_model") == "claude"):
            return False
        sid = str(session_id or "").strip()
        if not sid or sid == "?":
            return False
        threading.Thread(target=_worker, args=(sid, base_url or SERVER_BASE_URL),
                         name="ai-suggest", daemon=True).start()
        return True
    except Exception:  # noqa: BLE001
        return False

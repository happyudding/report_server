# -*- coding: utf-8 -*-
"""claude CLI 탐색·subprocess 실행·출력 파싱.

실행 격리 원칙(README §4):
- argv 는 전부 고정 리터럴 — 프롬프트는 stdin 으로만 넘긴다(Windows 32K 인자 한계·
  인코딩·.cmd 인젝션 표면 제거).
- cwd 는 빈 임시 디렉터리 — 프로젝트 CLAUDE.md/.claude 자동 발견을 차단한다.
- env 는 상속하고 **추가만** 한다(제거 없음) — Enterprise/OAuth 인증이 어떤 env 에
  기대는지 배포마다 달라 함부로 지우면 인증이 깨진다(제거 필요 여부는 현장 검증 항목).
- 선택 플래그는 그 버전 `--help` 에 있을 때만 붙인다(버전 차이 흡수).
  ⚠ `--bare` 는 절대 쓰지 않는다 — OAuth/keychain 을 읽지 않아(API 키 전용) 인증이 깨진다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

from .batch import build_meta_prompt, parse_batch_reply

_LOG = logging.getLogger("call_claude")

ENV_BIN = "CALL_CLAUDE_BIN"      # 바이너리 경로 오버라이드 env 키
DEFAULT_TIMEOUT = 240            # subprocess 1회(배치 1개) 상한(초)
PROBE_TIMEOUT = 30               # --version/--help 상한(초)

# help 텍스트에 토큰이 있을 때만 부착하는 선택 플래그 (docs/23 "도구/MCP/세션저장 차단")
_OPT_FLAGS = (
    (("--tools", ""), "--tools"),                              # 내장 도구 전면 차단
    (("--no-session-persistence",), "--no-session-persistence"),  # 세션 디스크 저장 차단
    (("--strict-mcp-config",), "--strict-mcp-config"),         # (--mcp-config 미지정) MCP 0개 강제
    (("--disable-slash-commands",), "--disable-slash-commands"),  # 스킬 차단
    (("--safe-mode",), "--safe-mode"),                         # CLAUDE.md·훅·플러그인 off, 인증은 정상
)
# 실행 부작용 억제 — 추가만 하는 env (기존 값은 덮지 않는다)
_EXTRA_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}

_HELP_CACHE = {}  # argv0 tuple -> help text ("" = 조회 실패)


class ClaudeCliError(Exception):
    """내부 단계 구분용(kind: not_found/timeout/exit/parse/output).

    공개 함수 밖으로 던져지지 않는다 — 로그 문자열의 kind 표기에만 쓴다.
    """


def _emit(log, msg):
    _LOG.info(msg)
    if log is not None:
        try:
            log(msg)
        except Exception:  # noqa: BLE001 — 로그 콜백 실패가 본 실행을 막으면 안 된다
            pass


def _as_argv(bin_path):
    """bin_path(str | Sequence[str] | None) → argv 접두 리스트 | None.

    Sequence 허용은 테스트 주입용(`[sys.executable, "stub.py"]`)이 목적.
    """
    if bin_path is None:
        return None
    if isinstance(bin_path, (list, tuple)):
        return [str(x) for x in bin_path]
    return [str(bin_path)]


def find_cli(env=None):
    """claude 실행 파일 절대경로 탐색. 없으면 None (예외 없음).

    순서: ① env[CALL_CLAUDE_BIN] — 지정돼 있으면 **그것만** 판정한다(틀린 지정을
    조용히 다른 후보로 대체하지 않는다) ② PATH 의 `claude`(shutil.which — Windows
    PATHEXT 로 .exe/.cmd 해석) ③ 알려진 설치 후보 2곳.
    """
    env = os.environ if env is None else env
    override = (env.get(ENV_BIN) or "").strip()
    if override:
        if os.path.isfile(override):
            return os.path.abspath(override)
        found = shutil.which(override)
        return os.path.abspath(found) if found else None
    found = shutil.which("claude")
    if found:
        return os.path.abspath(found)
    home = os.path.expanduser("~")
    appdata = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    for cand in (
        os.path.join(home, ".local", "bin", "claude.exe"),   # 네이티브 설치 기본
        os.path.join(appdata, "npm", "claude.cmd"),          # npm 전역 설치
    ):
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


def _resolve_argv0(bin_path, env, log):
    argv0 = _as_argv(bin_path)
    if argv0 is not None:
        return argv0
    found = find_cli(env)
    if not found:
        _emit(log, "call_claude not_found: claude CLI 없음 (CALL_CLAUDE_BIN/PATH 확인)")
        return None
    return [found]


def _run_cli(argv, *, input_text=None, timeout, log, tag):
    """subprocess 1회 실행 → CompletedProcess | None. 예외는 여기서 전부 삼킨다."""
    tmpdir = tempfile.mkdtemp(prefix="call_claude_")
    env = dict(os.environ)
    for key, val in _EXTRA_ENV.items():
        env.setdefault(key, val)
    try:
        return subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            encoding="utf-8",
            errors="replace",   # cp949 콘솔에서도 한글 출력이 깨지지 않게 utf-8 고정
            timeout=timeout,
            cwd=tmpdir,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        _emit(log, f"call_claude timeout: {tag} {timeout}s 초과")
        return None
    except Exception as exc:  # noqa: BLE001 — 공개 API 무예외 계약
        _emit(log, f"call_claude exec_error: {tag} {type(exc).__name__}: {exc}")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _help_text(argv0, *, log):
    """`--help` 출력(플래그 게이팅 근거). argv0 별 1회 캐시, 실패는 ""(선택 플래그 미부착)."""
    key = tuple(argv0)
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    completed = _run_cli(list(argv0) + ["--help"], timeout=PROBE_TIMEOUT, log=log, tag="--help")
    text = ""
    if completed is not None and completed.returncode == 0:
        text = completed.stdout or ""
    _HELP_CACHE[key] = text
    return text


def _gated_flags(help_text):
    flags = []
    for argv_part, token in _OPT_FLAGS:
        if token in help_text:
            flags.extend(argv_part)
    # --safe-mode 미지원 구버전 폴백: settings 소스 0개 (빈 값 수용 여부는 현장 검증 항목)
    if "--safe-mode" not in help_text and "--setting-sources" in help_text:
        flags.extend(["--setting-sources", ""])
    return flags


def probe(*, bin_path=None, env=None, timeout=PROBE_TIMEOUT, log=None):
    """가용성 점검 — {"ok","bin","version","flags","error"}.

    인증 여부는 판정하지 않는다(실호출로만 확인 가능 — README 현장 검증 항목).
    """
    out = {"ok": False, "bin": None, "version": None, "flags": [], "error": None}
    argv0 = _resolve_argv0(bin_path, env, log)
    if argv0 is None:
        out["error"] = "not_found"
        return out
    out["bin"] = subprocess.list2cmdline(argv0)
    completed = _run_cli(list(argv0) + ["--version"], timeout=timeout, log=log, tag="--version")
    if completed is None or completed.returncode != 0:
        out["error"] = "version_check_failed"
        return out
    out["version"] = (completed.stdout or "").strip()
    out["flags"] = _gated_flags(_help_text(argv0, log=log))
    out["ok"] = True
    return out


def _parse_cli_json(stdout):
    """`--output-format json` stdout → dict | None. 경고 줄이 섞여도 관대하게."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _invoke(prompt, *, bin_path, model, timeout, log, tag):
    """프롬프트 1개 실행 → 모델 출력 문자열 | None."""
    argv0 = _resolve_argv0(bin_path, os.environ, log)
    if argv0 is None:
        return None
    argv = list(argv0) + ["-p", "--output-format", "json"]
    argv += _gated_flags(_help_text(argv0, log=log))
    if model:
        argv += ["--model", str(model)]
    completed = _run_cli(argv, input_text=prompt, timeout=timeout, log=log, tag=tag)
    if completed is None:
        return None
    if completed.returncode != 0:
        tail = ((completed.stderr or "") + (completed.stdout or ""))[-400:].strip()
        _emit(log, f"call_claude exit: {tag} rc={completed.returncode} {tail}")
        return None
    data = _parse_cli_json(completed.stdout)
    if data is None:
        _emit(log, f"call_claude parse: {tag} stdout 이 JSON 이 아님 (len={len(completed.stdout or '')})")
        return None
    if data.get("is_error"):
        _emit(log, f"call_claude output: {tag} is_error=true {str(data.get('result'))[:200]}")
        return None
    result = data.get("result")
    if not isinstance(result, str) or not result.strip():
        _emit(log, f"call_claude output: {tag} result 필드 없음/비문자열")
        return None
    return result


def run_prompt(prompt, *, bin_path=None, model=None, timeout=DEFAULT_TIMEOUT, log=None):
    """단건 실행 — 모델 출력 문자열 | None. 모든 실패는 None (예외 없음)."""
    try:
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        return _invoke(prompt, bin_path=bin_path, model=model, timeout=timeout, log=log, tag="run_prompt")
    except Exception as exc:  # noqa: BLE001 — 공개 API 무예외 계약(최후 방어선)
        _emit(log, f"call_claude internal: run_prompt {type(exc).__name__}: {exc}")
        return None


def run_batch(prompts, *, bin_path=None, model=None, timeout=DEFAULT_TIMEOUT, log=None):
    """배치 실행 — N 건을 메타 프롬프트 1개로 묶어 subprocess 1회.

    반환 길이 == len(prompts). 실행/파싱 실패 → 전부 None (배치 단위 skip,
    건별 재시도 없음 — 호출부 폴백 무해 계약).
    """
    try:
        items = [str(p) for p in (prompts or [])]
        if not items:
            return []
        meta_prompt, _nonce = build_meta_prompt(items)
        reply = _invoke(meta_prompt, bin_path=bin_path, model=model, timeout=timeout,
                        log=log, tag=f"run_batch({len(items)})")
        if reply is None:
            return [None] * len(items)
        out = parse_batch_reply(reply, len(items))
        got = sum(1 for x in out if x is not None)
        _emit(log, f"call_claude run_batch: {got}/{len(items)} 건 수신")
        return out
    except Exception as exc:  # noqa: BLE001 — 공개 API 무예외 계약(최후 방어선)
        _emit(log, f"call_claude internal: run_batch {type(exc).__name__}: {exc}")
        return [None] * len(prompts or [])

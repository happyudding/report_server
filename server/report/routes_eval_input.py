"""Honey 'DB Input' — 선례(precedent) CSV 검증/적재 라우트.

관리자 Eval DB 탭의 `GET /api/eval/labels.csv`(내보내기)의 **반대 방향**이다. 같은 단순
5컬럼(Product type, Family Product, unit, Item, comment)을 받아 서버 소유 eval DB
(config.REPORT_EVAL_DB_PATH)에 적재한다 — 내려받아 고쳐서 다시 올리는 왕복이 성립한다.

**왜 subprocess 인가** (docs/13 §10):
  eval_analyzer/db_input/import_csv.py 의 `_import_group` 은 적재 대상 DB 를 가리키려고
  `eval_engine.config.DATA_DIR/DB_PATH` 를 **모듈 전역에 대입**한다. 그 모듈 객체는 이
  Flask 프로세스에서 web_report/ai_comment.py 와 공유되고 엔진 store.get_conn() 은 호출
  시점에 config.DB_PATH 를 읽으므로, in-process 로 부르면 waitress 스레드들이 공유하는
  전역이 오염된다. 프로세스 경계가 곧 격리다. 덤으로 "eval_engine import 는 ai_comment.py
  + eval_export.py 2곳만"(CLAUDE.md 규칙 #8) 규약도 그대로 지켜진다 — 실행은 import 가 아니다.

**2단계 UX**: Honey 가 같은 바이트를 mode=validate 로 한 번(검증 미리보기), 사용자가
확인하면 mode=commit 으로 다시 보낸다. 서버는 중간 상태를 갖지 않는다(토큰·TTL·정리 불필요).
commit 도 쓰기 전에 dry-run 을 한 번 더 돌려, 사용자가 승인한 검증을 지금 시점의 eval DB
기준으로 다시 증명한다.

**단순 포맷만 받는다**: 레거시 20컬럼은 행 검증이 `_import_group` 안에서 일어나고
store.get_conn() 이 그룹마다 커밋하므로 **부분 적재**가 날 수 있다. 단순 포맷은
`_convert_simple_rows` 가 전 행 + taxonomy 조합을 미리 검사해 부분 적재가 없다.
"""
import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

from flask import abort, jsonify, request

import config
from auth_identity import current_user as _current_user
from database import report_db
from report.report_extension import report_bp
from report.security import _client_meta

_log = logging.getLogger(__name__)

_IMPORT_SCRIPT = config.ROOT_DIR / "eval_analyzer" / "db_input" / "import_csv.py"
_CSV_MAX_BYTES = 5 * 1024 * 1024
# import_csv 는 pyyaml 로더 + SQLite 적재뿐이라 실측 1초 미만. 상한은 폭주 방지용.
_TIMEOUT_SEC = 300
_MODES = ("validate", "commit")
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z가-힣._-]+")
_LEGACY_MSG = ("단순 5컬럼 포맷(Product type, Family Product, unit, Item, comment)만 "
               "지원합니다. 20컬럼 레거시 CSV 는 서버에서 run_import.bat 으로 적재하세요.")
_EMPTY_MSG = "CSV 에서 헤더를 찾지 못했습니다 — 빈 파일이거나 형식이 다릅니다."

# 같은 프로세스 안에서 DB Input 을 직렬화한다. staged CSV 경로가 파일명 기반 고정이라
# 동시 실행 시 서로의 파일을 덮어쓸 수 있고, eval DB 동시 쓰기도 피하는 게 낫다.
# ⚠ 프로세스 내부 한정 — 운영자가 서버 콘솔에서 run_import.bat 을 동시에 돌리면
#   SQLite WAL + busy_timeout(5s)만이 방어다.
_import_lock = threading.Lock()


def _staged_csv_path(file_name):
    """임시 CSV 경로 — 같은 파일명이면 **같은 경로**를 쓴다 (랜덤 tmp 금지).

    import_csv._get_or_create_run 이 ingest_run 을 (source_file 문자열, session_id)로
    재사용하므로, 매번 다른 경로를 주면 같은 CSV 를 고쳐 재적재할 때마다 ingest_run 행이
    새로 쌓인다. 파일은 실행 직후 지우지만 비교 대상은 경로 문자열이라 재사용이 성립한다.
    uploads/report/eval_input/ 은 report_cleanup·report_tiering 순회 대상 밖이다.
    """
    name = _SAFE_NAME_RE.sub("_", os.path.basename(file_name or "labels.csv"))[:80]
    if not name.lower().endswith(".csv"):
        name += ".csv"
    return Path(config.REPORT_UPLOAD_DIR) / "eval_input" / name


def _run_import(csv_path, dry_run):
    """db_input/import_csv.py 를 별도 프로세스로 실행하고 JSON 결과를 돌려준다.

    계약(import_csv.main --json): stdout 마지막 줄에 JSON 1줄, 종료코드 0=정상 / 2=CSV 오류.
    """
    env = dict(os.environ)
    env["EVAL_DB_PATH"] = str(config.REPORT_EVAL_DB_PATH)
    # import_csv 는 stdout 만 utf-8 로 재설정한다 — 한국어 stderr(트레이스백)가 cp949 로
    # 나와 깨지지 않도록 자식 프로세스 전체를 utf-8 로 고정한다.
    env["PYTHONIOENCODING"] = "utf-8"
    argv = [config.REPORT_EVAL_IMPORT_PYTHON, str(_IMPORT_SCRIPT), str(csv_path),
            "--to-eval-db", "--json"] + (["--dry-run"] if dry_run else [])
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(_IMPORT_SCRIPT.parent),
                          timeout=_TIMEOUT_SEC)
    out = (proc.stdout or "").strip()
    if proc.returncode not in (0, 2) or not out:
        raise RuntimeError(f"import_csv exit={proc.returncode} "
                           f"stderr={(proc.stderr or '')[-800:]}")
    return json.loads(out.splitlines()[-1])


def _public(result, mode, file_name):
    """응답용 정리 — 서버 내부 경로(db_path)는 최상위·그룹 모두에서 제거한다."""
    groups = [{k: v for k, v in g.items() if k != "db_path"}
              for g in (result.get("groups") or [])]
    return {"ok": bool(result.get("ok")), "mode": mode,
            "format": result.get("format") or "", "rows": int(result.get("rows") or 0),
            "groups": groups, "errors": list(result.get("errors") or []),
            "file_name": file_name}


def _audit(payload, uid, result="ok"):
    """시도마다 1행 (validate 포함) — 누가 무엇을 넣었는지 남긴다. best-effort.

    security._audit 은 client_user 를 넘기지 못해 '누가' 가 비므로 log_audit 을 직접 부른다
    (routes_voc._audit_voc 선례). busy_timeout 100ms — 감사 때문에 응답을 붙잡지 않는다.
    """
    try:
        ip, ua = _client_meta()
        groups = " ".join(f"{g['product_type']}_{g['family_product']}:{g['rows']}"
                          for g in payload.get("groups") or [])
        errors = payload.get("errors") or []
        detail = (f"mode={payload.get('mode')} file={payload.get('file_name')} "
                  f"rows={payload.get('rows')} groups=[{groups}]")
        if errors:
            detail += f" errors={len(errors)}: {errors[0]}"
        report_db.log_audit("eval_db_input", changed_fields=detail[:1500],
                            file_name=payload.get("file_name"),
                            client_ip=ip, user_agent=ua, client_user=uid,
                            result=result, busy_timeout_ms=100)
    except Exception:
        _log.warning("DB Input 감사 기록 실패", exc_info=True)


@report_bp.post("/api/eval/labels_import")
def eval_labels_import():
    """선례 CSV 검증(mode=validate) / 적재(mode=commit) — Honey 'DB Input' 전용.

    브라우저가 아니므로 CSRF 대신 커스텀 헤더 X-Honey-Agent 를 요구한다
    (PATCH /session/<sid>/meta 선례 — 커스텀 헤더는 브라우저 폼으로 위조 불가).
    권한은 **Honey 신원이 있는 사용자 전원** — 선례 DB 는 세션 데이터와 분리돼 있고,
    누가 넣었는지는 감사 로그(action=eval_db_input)로 추적한다.

    CSV **내용** 오류는 4xx 가 아니라 200 + {"ok": false, "errors": [...]} 로 돌려준다 —
    UI 가 행별 목록을 그대로 렌더해야 하는 데이터지 전송 실패가 아니다.
    """
    if request.headers.get("X-Honey-Agent") != "1":
        return jsonify({"error": "선례 DB Input 은 Honey 앱에서만 가능합니다."}), 403
    uid = _current_user()
    if not uid:
        return jsonify({"error": "Honey 신원이 필요합니다 — Honey 앱에서 실행해 주세요."}), 401

    mode = (request.form.get("mode") or "validate").strip()
    if mode not in _MODES:
        return jsonify({"error": f"mode 는 {_MODES} 중 하나여야 합니다."}), 400
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "CSV 파일이 없습니다."}), 400
    data = upload.read()
    if not data:
        return jsonify({"error": "빈 CSV 파일입니다."}), 400
    if len(data) > _CSV_MAX_BYTES:
        return jsonify({"error": "CSV 가 너무 큽니다 (최대 5MB)."}), 413

    file_name = os.path.basename(upload.filename or "labels.csv")
    if not _IMPORT_SCRIPT.exists():
        _log.error("DB Input 적재기 없음: %s", _IMPORT_SCRIPT)
        return jsonify({"error": "서버에 선례 적재기가 설치되어 있지 않습니다."}), 503

    if not _import_lock.acquire(timeout=1.0):
        return jsonify({"error": "다른 DB Input 이 진행 중입니다 — 잠시 후 다시 시도해 주세요."}), 409
    path = _staged_csv_path(file_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        # commit 도 먼저 dry-run 을 돌린다: ① 레거시/빈 포맷을 **쓰기 전에** 걸러내고
        # ② 사용자가 승인한 검증을 지금 이 순간의 eval DB 기준으로 다시 증명한다.
        result = _run_import(path, dry_run=True)
        if result.get("ok") and result.get("format") != "simple":
            result["ok"] = False
            result["errors"] = [_LEGACY_MSG if result.get("format") == "legacy" else _EMPTY_MSG]
        if mode == "commit" and result.get("ok"):
            result = _run_import(path, dry_run=False)
    except subprocess.TimeoutExpired:
        _log.error("DB Input 시간 초과 (%s)", file_name)
        _audit({"mode": mode, "file_name": file_name}, uid, result="error")
        return jsonify({"error": "적재가 시간 내에 끝나지 않았습니다."}), 504
    except Exception:
        _log.exception("DB Input 실패 (%s)", file_name)
        _audit({"mode": mode, "file_name": file_name}, uid, result="error")
        return jsonify({"error": "서버에서 CSV 를 처리하지 못했습니다."}), 500
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _log.warning("staged CSV 삭제 실패: %s", path)
        _import_lock.release()

    payload = _public(result, mode, file_name)
    _audit(payload, uid, result="ok" if payload["ok"] else "error")
    return jsonify(payload)

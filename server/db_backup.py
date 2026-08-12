"""DB 주기 백업 스케줄러 (report.db + eval.db).

WAL 모드에서는 파일 단순 복사(shutil.copy)가 -wal 미반영/파손 위험이 있으므로
반드시 sqlite3 backup API(src.backup(dst))로 온라인 백업한다 (백업 중 쓰기는
API 가 페이지 단위로 재시도 처리). 백업본에 integrity_check 를 돌려 로그로 남기고,
REPORT_DB_BACKUP_KEEP 초과분은 DB 별 prefix 글롭으로 오래된 순으로 삭제한다.
integrity 실패본(.bad)은 사후 조사용으로 남기되 _BAD_KEEP 개까지만 보존한다
(안 그러면 실패가 반복될 때 DB 풀사이즈 파일이 무한 누적된다).

REPORT_DB_BACKUP_EXTERNAL_DIR 이 설정되면 integrity 통과본만 그 경로로 복사한다
(같은 디스크 손상 시 대비). 복사 실패는 로그만 남기고 백업 자체를 실패시키지 않는다.

스케줄러 패턴은 report_cleanup.py 와 동일 (daemon 스레드, _started 중복 방지).
"""
import logging
import shutil
import sqlite3
import threading
import time

import config

_log = logging.getLogger(__name__)
_started = False

# integrity 실패본(.bad) 보존 개수 — 조사에 필요한 최근 몇 개만 남기고 나머지는 정리.
_BAD_KEEP = 5

# 스케줄러 상태 (관리자 패널 /api/schedulers 노출용). 시간은 epoch 초.
STATE = {"last_run": None, "last_ok": None, "last_result": None, "next_run": None}


def _targets():
    """백업 대상 (경로, 파일 prefix) 목록. 존재하는 파일만.

    product_info.db 는 외부 PC 에서 만들어 손으로 배치하는 읽기전용 사본이라 제외한다
    (유실돼도 원본에서 재생성 — config.py PRODUCT_INFO_DB_PATH 주석 참조).
    voc.db 는 VOC 기능 미사용 중이라 백업 제외 (재사용 시 이 목록에 복원).
    """
    pairs = [
        (config.REPORT_DB_PATH, "report"),
        (config.REPORT_EVAL_DB_PATH, "eval"),
    ]
    return [(p, prefix) for p, prefix in pairs if p.exists()]


def _maintain_wal(conn):
    """백업 사이클에 얹는 원본 DB 유지보수 (best-effort).

    - wal_checkpoint(TRUNCATE): WAL 페이지를 본 파일로 반영하고 -wal 파일을 0 으로
      잘라 -wal 무한 증가를 막는다 (기본 auto-checkpoint 는 truncate 하지 않음).
    - PRAGMA optimize: 쿼리 패턴 기반 통계 갱신 (sqlite 권장 주기 실행).
    실패해도 백업 자체를 막지 않는다. VACUUM 은 장시간 잠금이라 자동 실행하지 않는다
    (필요 시 수동 — README 참조).
    """
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # 반환은 (busy, log, checkpointed). busy=1 은 **예외가 아니라 조용한 실패**다 —
        # 읽는 커넥션이 하나라도 남아 있으면 truncate 를 포기하고 그냥 1 을 돌려준다.
        # 여태 이 값을 보지 않아서, 상시 조회 트래픽이 있는 서버에서는 체크포인트가
        # 매번 실패해도 아무도 모른 채 -wal 만 계속 커졌다. 경고만 남긴다(재시도 안 함 —
        # 다음 백업 사이클에 다시 시도하고, 그래도 계속 실패하면 -wal 크기가 관리자
        # 현황 탭에 보인다).
        if row and row[0]:
            _log.warning("[db-backup] wal checkpoint busy — 이번 사이클 truncate 실패 "
                         "(reader 가 남아 있음). -wal 크기를 관리자 현황 탭에서 확인할 것")
        conn.execute("PRAGMA optimize")
    except Exception:
        _log.exception("[db-backup] wal maintenance failed")


def _prune(backup_dir, pattern, keep):
    for old in sorted(backup_dir.glob(pattern), key=lambda p: p.stat().st_mtime)[:-keep]:
        try:
            old.unlink()
            _log.info("[db-backup] pruned: %s", old)
        except Exception:
            _log.exception("[db-backup] prune failed: %s", old)


def _copy_external(src):
    """integrity 통과본을 외부 경로로 복사 (best-effort). 미설정이면 no-op."""
    ext_dir = config.REPORT_DB_BACKUP_EXTERNAL_DIR
    if not ext_dir:
        return None
    try:
        ext_dir.mkdir(parents=True, exist_ok=True)
        dst = ext_dir / src.name
        shutil.copy2(src, dst)
        _prune(ext_dir, src.name.split("_")[0] + "_*.db",
               max(1, int(config.REPORT_DB_BACKUP_KEEP)))
        return str(dst)
    except Exception:
        _log.exception("[db-backup] external copy failed: %s -> %s", src, ext_dir)
        return None


def _backup_one(src_path, prefix, backup_dir, stamp):
    """DB 1개 온라인 백업. {prefix,file,ok,bytes,external} 반환 (실패 시 예외)."""
    dest = backup_dir / f"{prefix}_{stamp}.db"

    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
            try:
                row = dst.execute("PRAGMA integrity_check").fetchone()
                ok = bool(row) and row[0] == "ok"
            except sqlite3.DatabaseError:
                # 심하게 손상된 백업본은 integrity_check 가 non-ok 행 대신 예외를
                # 던진다 — 동일하게 불량 백업으로 취급한다.
                ok = False
        finally:
            dst.close()
        _maintain_wal(src)
    except Exception:
        # backup 도중 예외로 중단된 부분 파일이 rotation glob 에 끼지 않게 제거.
        dest.unlink(missing_ok=True)
        raise
    finally:
        src.close()

    external = None
    if ok:
        _log.info("[db-backup] ok: %s", dest)
        external = _copy_external(dest)
    else:
        # 불량 백업은 rotation glob(<prefix>_*.db) 밖으로 rename — 그대로 두면 KEEP 계산에
        # 포함돼 오래된 '정상' 백업이 먼저 삭제된다. .bad 는 사후 조사용으로 남긴다.
        bad = dest.with_name(dest.name + ".bad")
        try:
            dest.rename(bad)
            dest = bad
        except OSError:
            _log.exception("[db-backup] bad backup rename failed: %s", dest)
        _log.error("[db-backup] integrity_check failed: %s", dest)

    # 보존 개수 초과분 정리 — 다른 파일 오삭 방지를 위해 이 DB 의 prefix 만 대상.
    # .bad 는 별도 글롭이라 정상본 KEEP 계산을 침범하지 않는다.
    _prune(backup_dir, f"{prefix}_*.db", max(1, int(config.REPORT_DB_BACKUP_KEEP)))
    _prune(backup_dir, f"{prefix}_*.db.bad", _BAD_KEEP)

    return {"prefix": prefix, "file": dest.name, "ok": ok,
            "bytes": dest.stat().st_size if dest.exists() else 0,
            "external": external}


def run_backup():
    """백업 1회 수행 (+ 원본 WAL checkpoint/optimize).

    {"ok": 전체성공여부, "results": [DB별 결과]} 반환. 개별 DB 예외는 삼켜서
    다른 DB 백업을 막지 않고 결과에 error 로 기록한다 (전부 실패하면 ok=False).
    """
    backup_dir = config.REPORT_DB_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    results = []
    for src_path, prefix in _targets():
        try:
            results.append(_backup_one(src_path, prefix, backup_dir, stamp))
        except Exception as exc:
            _log.exception("[db-backup] failed: %s", src_path)
            results.append({"prefix": prefix, "file": None, "ok": False,
                            "bytes": 0, "error": str(exc)})
    summary = {"ok": bool(results) and all(r["ok"] for r in results),
               "dir": str(backup_dir), "results": results}
    STATE["last_run"] = int(time.time())
    STATE["last_ok"] = summary["ok"]
    STATE["last_result"] = ", ".join(
        f"{r['prefix']}:{'ok' if r['ok'] else 'FAIL'}" for r in results) or "(대상 없음)"
    return summary


def start_backup_scheduler():
    """daemon 스레드로 주기 백업 시작. 중복 기동은 _started 플래그로 방지."""
    global _started
    if _started:
        return
    if not config.REPORT_DB_BACKUP_ENABLED:
        _log.info("[db-backup] disabled (REPORT_DB_BACKUP_ENABLED=0)")
        return
    _started = True
    interval = max(0.1, float(config.REPORT_DB_BACKUP_INTERVAL_HOURS)) * 3600

    def _loop():
        time.sleep(60)  # 서버 초기화와 겹치지 않게 첫 실행만 잠깐 지연
        while True:
            try:
                run_backup()
            except Exception:
                _log.exception("[db-backup] run_backup crashed")
            STATE["next_run"] = int(time.time() + interval)
            time.sleep(interval)

    threading.Thread(target=_loop, name="report-db-backup", daemon=True).start()
    _log.info("[db-backup] scheduler started: interval=%.1fh keep=%d dir=%s external=%s",
              config.REPORT_DB_BACKUP_INTERVAL_HOURS, config.REPORT_DB_BACKUP_KEEP,
              config.REPORT_DB_BACKUP_DIR, config.REPORT_DB_BACKUP_EXTERNAL_DIR or "(없음)")

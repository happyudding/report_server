"""골든셋(tools/eval_golden/golden.yaml) 읽기/추가 — /pe/eval 패널의 파일 계층.

트레이스 화면에서 "지금 이 발화가 옳다" 를 눌러 기대값으로 굳히는 곳이다. 임계값을 만진
뒤 골든 회귀를 돌리면 그 기대값 대비 누락/오탐이 바로 드러난다(회귀 실행은 CLI 와 같은
tools/eval_golden/golden_check.py 를 쓴다).

골든셋은 **룰이 아니다** — 저장해도 rules_rev 를 올리지 않는다(엔진 판정이 바뀌지 않으므로
세션 리포트 캐시를 무효화할 이유가 없다). 쓰기는 rules_io 의 원자적 쓰기/직렬화 헬퍼를
재사용하되, 백업만은 rules/_backup 이 아니라 골든셋 옆(_backup/)에 둔다 — 룰 백업 목록에
섞이면 "복원" 버튼이 golden.yaml 을 rules 디렉토리로 되돌리려 하기 때문이다.
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import yaml

from eval_panel import rules_io

GOLDEN_FILE = Path(__file__).resolve().parent.parent.parent / "tools" / "eval_golden" / "golden.yaml"
BACKUP_KEEP = 20

# 프로세스 내 read-modify-write 직렬화 (동시 추가로 한쪽이 사라지는 것 방지)
_lock = threading.Lock()


def _backup() -> str | None:
    """저장 전 원본 복사 → tools/eval_golden/_backup/. 반환 = 백업 파일명."""
    if not GOLDEN_FILE.is_file():
        return None
    root = GOLDEN_FILE.parent / "_backup"
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    name = f"{GOLDEN_FILE.name}.{stamp}.bak"
    seq = 1
    while (root / name).exists():                 # 같은 초 재저장 — 직전 백업 보존
        seq += 1
        name = f"{GOLDEN_FILE.name}.{stamp}-{seq}.bak"
    shutil.copy2(GOLDEN_FILE, root / name)
    for old in sorted(root.glob(f"{GOLDEN_FILE.name}.*.bak"))[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return name


def read_golden() -> dict:
    """{"sessions": [...]} 정규화. 파일이 없거나 비면 빈 목록."""
    if not GOLDEN_FILE.is_file():
        return {"sessions": []}
    doc = yaml.safe_load(GOLDEN_FILE.read_text(encoding="utf-8"))
    sessions = (doc or {}).get("sessions") if isinstance(doc, dict) else None
    return {"sessions": [dict(s) for s in (sessions or []) if isinstance(s, dict)]}


def _fired_ids(case: dict) -> list:
    return sorted({r.get("id") for r in (case.get("signature_matrix") or [])
                   if r.get("fired") and r.get("id")})


def _same_target(entry: dict, item: str, bin_, source) -> bool:
    return (str(entry.get("item") or "") == item
            and entry.get("bin") == bin_ and entry.get("source") == source)


def add_case(session_id: str, note: str, case: dict) -> dict:
    """트레이스 케이스 1건의 **현재 발화 상태**를 기대값으로 기록한다.

    같은 (item, bin, source) 항목이 이미 있으면 교체한다(replaced=True) — 룰을 고쳐
    기대값 자체가 바뀌는 경우가 정상 흐름이라 중복을 쌓지 않는다.
    """
    item = str(case.get("item_raw") or "")
    if not item:
        raise rules_io.RuleError("item_raw 가 없는 케이스는 골든셋에 담을 수 없습니다")
    bin_ = case.get("bin")
    source = case.get("source_index")
    fired = _fired_ids(case)

    entry = {"item": item, "bin": bin_, "source": source}
    if fired:                                   # 발화 0건이면 fire 키는 의미가 없다
        entry["fire"] = fired
    entry["status"] = case.get("status")

    with _lock:
        doc = read_golden()
        target = next((s for s in doc["sessions"]
                       if str(s.get("session_id") or "") == session_id), None)
        if target is None:
            target = {"session_id": session_id, "note": note, "expect": []}
            doc["sessions"].append(target)
        elif note and not target.get("note"):
            target["note"] = note
        expect = target.setdefault("expect", [])
        replaced = False
        for i, old in enumerate(expect):
            if isinstance(old, dict) and _same_target(old, item, bin_, source):
                expect[i] = entry
                replaced = True
                break
        if not replaced:
            expect.append(entry)

        backup = _backup()
        rules_io._write_atomic(GOLDEN_FILE,
                               rules_io._head_comments(GOLDEN_FILE) + rules_io._dump(doc))

    return {"session_id": session_id, "entry": entry, "replaced": replaced,
            "backup": backup, "total_sessions": len(doc["sessions"]),
            "total_expect": sum(len(s.get("expect") or []) for s in doc["sessions"])}

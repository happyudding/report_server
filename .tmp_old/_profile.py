"""구간 프로파일 측정값 중앙 sink (순수 stdlib).

analyzer._flow_time / xlsx_writer._flow_prof 가 측정한 (module, label, elapsed) 을
여기로 흘려보낸다. 평상시엔 비활성(collecting=False) — GUI 장기 실행 시 메모리 누적
없음. profile_run.py 도구가 start_collection() 으로 켤 때만 records 에 적재한다.

depth 로 중첩 구간(예: fill_fail_item 안의 fail_values.write_source[...])을 보존해
트리 출력·중복 합산 회피에 쓴다.
"""
from __future__ import annotations

import json
from pathlib import Path

_collecting = False
_records = []   # [{"seq","module","label","elapsed","depth"}]
_depth = 0
_seq = 0


def collecting() -> bool:
    return _collecting


def start_collection() -> None:
    """수집 시작 (이전 records 초기화)."""
    global _collecting
    reset()
    _collecting = True


def stop_collection() -> None:
    global _collecting
    _collecting = False


def reset() -> None:
    global _records, _depth, _seq
    _records = []
    _depth = 0
    _seq = 0


def push() -> int:
    """구간 진입 — 현재 depth 반환 (1-based)."""
    global _depth
    _depth += 1
    return _depth


def pop(module: str, label: str, elapsed: float, depth_at_enter: int) -> None:
    """구간 종료 — 수집 중이면 record 적재."""
    global _depth, _seq
    _depth -= 1
    if not _collecting:
        return
    _records.append({
        "seq": _seq,
        "module": module,
        "label": label,
        "elapsed": elapsed,
        "depth": depth_at_enter - 1,
    })
    _seq += 1


def add(module: str, label: str, elapsed: float, depth: int = 0) -> None:
    """외부(러너)에서 coarse phase 를 직접 적재할 때 사용 (push/pop 쌍 없이)."""
    global _seq
    if not _collecting:
        return
    _records.append({
        "seq": _seq,
        "module": module,
        "label": label,
        "elapsed": elapsed,
        "depth": depth,
    })
    _seq += 1


def snapshot() -> list:
    """현재까지 수집된 records 의 얕은 복사 반환."""
    return [dict(r) for r in _records]


# ---------------------------------------------------------------------------
# 저장 / 로드

def save(path, payload: dict) -> str:
    """payload(메타 + records) 를 JSON 으로 저장. 반환: 저장 경로(str)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def load(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def aggregate(records: list) -> dict:
    """(module, label) 키로 elapsed 합산 + count 집계. 반환: {key: {"elapsed","count"}}."""
    out = {}
    for r in records:
        key = f"{r['module']}.{r['label']}" if r.get("module") else r["label"]
        slot = out.setdefault(key, {"elapsed": 0.0, "count": 0})
        slot["elapsed"] += float(r["elapsed"])
        slot["count"] += 1
    return out

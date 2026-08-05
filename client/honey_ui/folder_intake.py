"""폴더 → 입력 파일 수집 + RT/CT/HT role 추정 (Qt 무의존 순수 모듈).

LOCAL FILE OPEN 의 "폴더 열기" 와 창 드래그앤드랍이 공유하는 스캔 규칙이다. 상위폴더
(예 EP1) 밑에 온도별 하위폴더가 있는 구조를 인식한다::

    EP1/
    ├── RT/    (RT, ROOM — 대소문자 무관, "RT_25C" 같은 변형도 인식)
    │    ├── wf1.stdf
    │    └── wf2.stdf
    ├── CT/    (CT, COLD)
    └── HT/    (HT, HOT)

role 폴더를 하나라도 찾으면 **그 폴더들의 데이터 파일만** 수집한다(인식 못 한 폴더는
건너뛴다). role 폴더가 하나도 없으면 Temperature 가 아닌 일반 폴더로 보고 하위 데이터
파일을 전부 재귀 수집한다(role 맵은 비어 있음) — 전 모드에서 폴더 열기를 쓸 수 있게.

여기서 얻은 ``{경로: role}`` 은 Temperature 배치 창의 **자동 배치 근거**로만 쓰인다.
입력 파일 개수 ≠ source 개수(honey_parse 가 내부 병합할 수 있음)이므로 role 을 source
이름에 잇는 일은 honey_main._roles_for_names 가 따로 한다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# 역할 → 폴더명에서 찾을 토큰들. 앞이 우선순위가 아니라 '어느 하나라도 걸리면' 그 역할이다.
ROLE_TOKENS = {
    "RT": ("RT", "ROOM"),
    "CT": ("CT", "COLD"),
    "HT": ("HT", "HOT"),
}
ROLE_ORDER = ("RT", "CT", "HT")

# 파싱 대상 데이터 파일. .lt/.pds 는 limit 테이블이라 입력 파일이 아니다
# (Temperature 배치 창의 드롭 영역이 따로 받는다).
DATA_SUFFIXES = (".csv", ".stdf", ".std")

# 토큰 경계는 '알파벳이 아닌 것' — "RT_25C"·"RT25C"·"Cold Temp" 는 걸리고
# "SHORT"(RT 포함)·"OCTOBER"(CT 포함) 는 안 걸린다.
_TOKEN_RE = {
    role: re.compile(r"(?:^|[^A-Za-z])(?:" + "|".join(tokens) + r")(?:[^A-Za-z]|$)",
                     re.IGNORECASE)
    for role, tokens in ROLE_TOKENS.items()
}


def role_of_dirname(name) -> str | None:
    """폴더명 → "RT" | "CT" | "HT" | None.

    2개 이상의 역할이 동시에 걸리면(예 "RT_HT") 모호하므로 None 을 돌려 건너뛴다 —
    조용히 한쪽으로 배정하면 사용자가 눈치채지 못한 채 잘못된 기준으로 판정된다.
    """
    text = str(name or "")
    hits = [role for role in ROLE_ORDER if _TOKEN_RE[role].search(text)]
    return hits[0] if len(hits) == 1 else None


def _is_data_file(path: Path) -> bool:
    return path.suffix.lower() in DATA_SUFFIXES


def _data_files(directory: Path) -> list:
    """폴더 직속 데이터 파일 (이름순). 하위 폴더는 보지 않는다."""
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    return [p for p in entries if p.is_file() and _is_data_file(p)]


def _walk_data_files(root: Path) -> list:
    """폴더 전체 재귀 수집 (role 폴더가 없을 때의 일반 폴더 폴백)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort(key=str.lower)
        for name in sorted(filenames, key=str.lower):
            path = Path(dirpath) / name
            if _is_data_file(path):
                out.append(path)
    return out


def scan_folder(root) -> tuple:
    """폴더 1개 스캔 → (파일 경로 목록, {경로: role}, 건너뛴 폴더명 목록).

    role 폴더를 찾는 범위는 root 자신 + root 의 직속 하위 폴더다. 파일 순서는
    (RT → CT → HT, 이름순)으로 고정한다 — 같은 폴더를 다시 열어도 순서가 흔들리면
    Temperature 그룹 자동 배치 결과가 실행마다 달라진다.
    """
    root = Path(root)
    if not root.is_dir():
        return [], {}, []

    buckets = {role: [] for role in ROLE_ORDER}
    skipped = []

    self_role = role_of_dirname(root.name)
    if self_role:
        buckets[self_role].extend(_data_files(root))
    else:
        try:
            children = sorted([p for p in root.iterdir() if p.is_dir()],
                              key=lambda p: p.name.lower())
        except OSError:
            children = []
        for child in children:
            role = role_of_dirname(child.name)
            if role:
                buckets[role].extend(_data_files(child))
            else:
                skipped.append(child.name)

    if any(buckets.values()):
        paths, roles = [], {}
        for role in ROLE_ORDER:
            for path in buckets[role]:
                key = str(path.resolve())
                if key not in roles:
                    paths.append(key)
                    roles[key] = role
        return paths, roles, skipped

    # role 폴더가 하나도 없다 → 일반 폴더. 하위 데이터 파일을 전부 가져오고 role 은 없다.
    return [str(p.resolve()) for p in _walk_data_files(root)], {}, []


def scan_folders(roots) -> tuple:
    """폴더 여러 개 스캔 → 결과 병합 (경로 중복 제거, 먼저 나온 role 유지)."""
    paths, roles, skipped = [], {}, []
    seen = set()
    for root in roots or []:
        found, role_map, skip = scan_folder(root)
        for path in found:
            if path in seen:
                continue
            seen.add(path)
            paths.append(path)
            if path in role_map:
                roles[path] = role_map[path]
        skipped.extend(skip)
    return paths, roles, skipped

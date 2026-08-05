"""폴더 열기 — 온도 폴더(RT/CT/HT) 인식 + 파일 수집 회귀 테스트.

실행:
    python tests/test_folder_intake.py

고정하는 계약:
  - 폴더명 role 인식: RT/ROOM · CT/COLD · HT/HOT, 대소문자 무관 + 토큰 경계 부분일치
    ("RT_25C" 인식 / "SHORT" 미인식), 2개 role 동시 매칭은 모호 → None
  - role 폴더가 하나라도 있으면 **그 폴더만** 수집하고 나머지는 skipped 로 보고
  - role 폴더가 하나도 없으면 일반 폴더로 보고 하위 파일 전부 재귀 수집(role 없음)
  - 수집 순서는 (RT → CT → HT, 이름순) 고정 — 자동 그룹 배치가 실행마다 흔들리면 안 된다
  - **확장자 필터 없음** — 파일 열기와 같은 규칙(.lt/.pds·로그도 딸려온다)

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

from honey_ui.folder_intake import (role_of_dirname, scan_folder,  # noqa: E402
                                    scan_folders)


def _make(root: Path, rel: str, *names):
    """root/rel 폴더를 만들고 그 안에 빈 파일들을 만든다."""
    directory = root / rel if rel else root
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"")
    return directory


def test_role_of_dirname_recognized():
    """RT/ROOM · CT/COLD · HT/HOT — 대소문자 무관, 토큰 경계 부분일치."""
    cases = {
        "RT": "RT", "rt": "RT", "Rt": "RT", "RT_25C": "RT", "RT25C": "RT",
        "ROOM": "RT", "Room Temp": "RT", "25C_RT": "RT",
        "CT": "CT", "ct": "CT", "COLD": "CT", "Cold Temp": "CT", "CT_-40": "CT",
        "HT": "HT", "ht": "HT", "HOT": "HT", "HOT_125": "HT", "HT125": "HT",
    }
    for name, expected in cases.items():
        assert role_of_dirname(name) == expected, (name, role_of_dirname(name))


def test_role_of_dirname_rejected():
    """토큰이 알파벳에 파묻힌 이름과 모호한 이름은 인식하지 않는다."""
    for name in ("SHORT", "OCTOBER", "EP1", "LOG", "DATA", "", "PARTS", "CHART"):
        assert role_of_dirname(name) is None, (name, role_of_dirname(name))
    # 2개 role 이 동시에 걸리면 모호 → None (조용한 오배정 방지)
    assert role_of_dirname("RT_HT") is None
    assert role_of_dirname("HOT_COLD") is None


def test_scan_folder_temperature_layout():
    """EP1/{RT,CT,HT} 구조 — role 폴더만 수집하고 순서는 RT→CT→HT 고정."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "EP1"
        _make(root, "RT", "wf2.stdf", "wf1.stdf")
        _make(root, "CT", "wf1.stdf")
        _make(root, "HOT_125", "wf1.csv")
        _make(root, "LOG", "run.csv")            # 미인식 → 건너뜀
        _make(root, "", "readme.csv")            # 상위폴더 직속 → 수집 대상 아님

        paths, roles, skipped = scan_folder(root)

        names = [Path(p).name for p in paths]
        assert names == ["wf1.stdf", "wf2.stdf", "wf1.stdf", "wf1.csv"], names
        assert [roles[p] for p in paths] == ["RT", "RT", "CT", "HT"], roles
        assert skipped == ["LOG"], skipped
        assert all("readme.csv" not in p for p in paths), paths


def test_scan_folder_has_no_extension_filter():
    """확장자 필터 없음 — 파일 열기("모든 파일 (*.*)")와 같은 규칙 (2026-08-05 사용자 확정).

    .lt/.pds 같은 limit 테이블이나 로그도 그대로 딸려온다. 걸러내는 것은 사용자 몫
    (파일 리스트 행별 ✕). 여기서 다시 화이트리스트를 넣지 말 것.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "EP1"
        _make(root, "RT", "wf1.stdf", "limits.lt", "spec.pds", "run.log", "noext")
        paths, roles, _skipped = scan_folder(root)
        assert sorted(Path(p).name for p in paths) == [
            "limits.lt", "noext", "run.log", "spec.pds", "wf1.stdf"], paths
        assert set(roles.values()) == {"RT"}, roles


def test_scan_folder_plain_folder_recurses():
    """온도 폴더가 하나도 없으면 일반 폴더 — 하위 전부 재귀 수집, role 없음."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "batch"
        _make(root, "", "a.csv")
        _make(root, "sub", "b.stdf")
        _make(root, "sub/deep", "c.std")

        paths, roles, skipped = scan_folder(root)

        assert sorted(Path(p).name for p in paths) == ["a.csv", "b.stdf", "c.std"], paths
        assert roles == {}, roles
        assert skipped == [], skipped


def test_scan_folder_root_is_role_folder():
    """RT 폴더 자체를 열어도 인식한다(상위폴더가 아니라 role 폴더를 직접 연 경우)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "RT"
        _make(root, "", "wf1.stdf")
        paths, roles, _skipped = scan_folder(root)
        assert [Path(p).name for p in paths] == ["wf1.stdf"], paths
        assert set(roles.values()) == {"RT"}, roles


def test_scan_folder_missing_dir():
    """없는 경로/파일 경로는 빈 결과 (예외 없이)."""
    with tempfile.TemporaryDirectory() as tmp:
        assert scan_folder(Path(tmp) / "nope") == ([], {}, [])
        target = Path(tmp) / "a.csv"
        target.write_bytes(b"")
        assert scan_folder(target) == ([], {}, [])


def test_scan_folders_merges_without_duplicates():
    """폴더 여러 개 — 경로 중복 제거, 먼저 나온 role 유지."""
    with tempfile.TemporaryDirectory() as tmp:
        ep1 = Path(tmp) / "EP1"
        ep2 = Path(tmp) / "EP2"
        _make(ep1, "RT", "a.stdf")
        _make(ep2, "CT", "b.stdf")

        paths, roles, _skipped = scan_folders([ep1, ep2, ep1])

        assert len(paths) == 2, paths
        assert len(set(paths)) == 2, paths
        assert sorted(roles.values()) == ["CT", "RT"], roles


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_role_of_dirname_recognized, test_role_of_dirname_rejected,
               test_scan_folder_temperature_layout, test_scan_folder_has_no_extension_filter,
               test_scan_folder_plain_folder_recurses, test_scan_folder_root_is_role_folder,
               test_scan_folder_missing_dir, test_scan_folders_merges_without_duplicates):
        fn()
        checks += 1
    print(f"PASS: test_folder_intake ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

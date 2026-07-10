"""server + web_report 의 import 를 스캔해 requirements.txt 에 빠진 서드파티를 리포트한다.

비파괴적: requirements.txt 를 절대 수정하지 않고, "코드가 import 하는데 requirements 에
없는" 서드파티 패키지만 출력한다. 코드에 새 라이브러리를 추가한 뒤 이 스크립트를 돌리면
requirements.txt 에 무엇을 추가해야 하는지 알 수 있다.

  python tools/check_requirements.py        # server/ 에서 실행
  (또는 check_requirements.bat 더블클릭)

정확한 PyPI 이름 매핑이 어려운 케이스는 import 이름 그대로 보고한다(대부분 동일).
반환코드: 누락 있으면 1, 없으면 0.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# import 이름 != PyPI 배포 이름인 알려진 케이스만.
IMPORT_TO_DIST = {
    "PIL": "pillow",
    "yaml": "pyyaml",
    "cv2": "opencv-python",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "sklearn": "scikit-learn",
}

# 다른 requirements 패키지가 항상 함께 설치하는 번들 모듈 → 별도 선언 불필요.
BUNDLED_BY = {
    "botocore": "boto3",
    "s3transfer": "boto3",
    "jmespath": "boto3",
}

SERVER_DIR = Path(__file__).resolve().parent.parent          # server/
REPO_ROOT = SERVER_DIR.parent                                # report_server/
WEB_REPORT_DIR = REPO_ROOT / "web_report"
REQUIREMENTS = SERVER_DIR / "requirements.txt"

# 스캔 대상(활성 서버 런타임). client/_reference/tests 는 별도 관리라 제외.
SCAN_ROOTS = [SERVER_DIR, WEB_REPORT_DIR]
SKIP_DIR_NAMES = {".venv", "__pycache__", "_reference", "build", "dist", "releases"}


def _local_module_names() -> set[str]:
    """스캔 루트의 .py 파일 stem + 하위 패키지 디렉토리명 = 로컬(자기 프로젝트) 모듈."""
    names: set[str] = set()
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        names.add(root.name)
        for py in root.rglob("*.py"):
            if SKIP_DIR_NAMES & set(py.parts):
                continue
            names.add(py.stem)
        for sub in root.rglob("*"):
            if sub.is_dir() and not (SKIP_DIR_NAMES & set(sub.parts)):
                names.add(sub.name)
    return names


def _iter_py_files():
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if SKIP_DIR_NAMES & set(py.parts):
                continue
            yield py


def _top_level_imports() -> set[str]:
    imports: set[str] = set()
    for py in _iter_py_files():
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imports.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:      # 상대 import(.foo) 는 로컬 → 제외
                    imports.add(node.module.split(".")[0])
    return imports


def _requirements_dist_names() -> set[str]:
    names: set[str] = set()
    if not REQUIREMENTS.exists():
        return names
    for line in REQUIREMENTS.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pkg = re.split(r"[<>=!~;\s]", line, maxsplit=1)[0].strip().lower()
        if pkg:
            names.add(pkg)
    return names


def main() -> int:
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    local = _local_module_names()
    have = _requirements_dist_names()

    missing = {}
    for imp in sorted(_top_level_imports()):
        if imp in stdlib or imp in local or imp.startswith("_"):
            continue
        if imp in BUNDLED_BY and BUNDLED_BY[imp].lower() in have:
            continue      # boto3 등 선언된 패키지가 함께 설치하는 번들
        dist = IMPORT_TO_DIST.get(imp, imp).lower()
        if dist not in have:
            missing[imp] = dist

    if not missing:
        print("[check] OK: 모든 서드파티 import 가 requirements.txt 에 있습니다.")
        return 0

    print("[check] requirements.txt 에 없는 서드파티 import:")
    for imp, dist in missing.items():
        note = f"  (PyPI: {dist})" if dist != imp else ""
        print(f"  - import {imp}{note}")
    print("\n[check] 위 패키지를 server/requirements.txt 에 추가하세요.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

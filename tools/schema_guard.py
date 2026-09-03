"""DB 컬럼 이름 오타 가드 — 코드가 읽는 키를 **실제 스키마**와 대조한다.

실행:
    server\\.venv\\Scripts\\python.exe tools/schema_guard.py          # 전체 검사
    server\\.venv\\Scripts\\python.exe tools/schema_guard.py --stop   # Stop 훅(변경분만)

**왜 이 파일이 생겼나** (2026-09-02): AI Comment 대기 표시(`Loading… (Claude)`)가 화면에
전혀 뜨지 않는다는 신고. 원인은 `session.get("uploaded_at")` 이었다 — `report_session` 에
그런 컬럼은 없고 실제 이름은 `created_at` 이다. `.get()` 은 없는 키에 **None 을 돌려주므로
예외가 나지 않는다.** 그래서:

  - 파이썬은 조용히 넘어가고,
  - TTL 계산이 "아주 오래된 세션"으로 떨어져 대기 맵이 **항상 비었고**,
  - 화면에는 에러가 아니라 **"기능이 없는 것처럼"** 보였다(발견이 늦는 최악의 형태).

같은 부류: `password`(폐지된 컬럼)를 계속 읽거나, 오타(`analysis_ky`)를 쓰거나,
다른 테이블의 컬럼을 session dict 에서 찾는 경우. 전부 런타임 무증상이라 테스트가
그 코드를 안 밟으면 배포까지 간다.

**검사 방법**: 스키마 정본([server/database/core.py](../server/database/core.py) `SCHEMA`)을
파싱해 테이블별 컬럼 집합을 만들고, 코드에서 `<이름>.get("키")` 패턴을 모아 대조한다.
운영 DB 파일이 아니라 **소스의 SCHEMA 를 읽는다** — 개발 PC 에 DB 가 없어도 돌고,
마이그레이션으로 추가된 컬럼(ALTER TABLE)도 함께 훑는다.

**한계(의도)**: dict 변수명으로 대상 테이블을 추정하므로 `_VAR_TABLE` 에 등록된 이름만
본다. 넓히면 오탐이 늘어 무시하게 되고, 그러면 가드가 죽는다. 새 패턴이 생기면 여기에
한 줄 추가할 것.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 검사 대상 변수명 → 그 dict 가 담는 테이블. 이름이 곧 계약인 자리만 등록한다.
# session/session_row: report_db.get_session() 반환 = report_session 1행.
_VAR_TABLE = {
    "session": "report_session",
    "session_row": "report_session",
}

# 위 테이블 컬럼이 아니어도 **정상인 키** — 조회 시점에 코드가 얹는 파생 필드다.
# 실제로 얹는 곳을 확인하고 추가할 것(추측으로 넣으면 가드가 무력해진다).
_EXTRA_OK = {
    "report_session": {
        "session_id",            # 스키마에 있지만 별칭으로도 쓰인다
        "has_password",          # /full 응답 조립 시 password 대신 넣는 불린
        "is_uploader",           # 조회자 권한 판정 결과
        "gross_die",             # 기준정보 lookup 결과(스키마에도 있음)
        "editors",               # 위임 편집자 목록(별도 테이블 조인)
        "display_name",          # report_user_profile 조인 표시명
        "product_info",          # 기준정보 dict
    },
}

_SCAN_DIRS = ("web_report", "server")
_SKIP_PARTS = (".venv", "site-packages", "__pycache__", "node_modules")

_GET_RE = re.compile(
    r"\b(" + "|".join(map(re.escape, _VAR_TABLE)) + r")\.get\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']")
# 면제: 그 줄에 주석으로 사유를 달면 통과시킨다(perf_guard 와 같은 규약).
_ALLOW_RE = re.compile(r"#\s*schema-guard:\s*allow")


def _schema_columns() -> dict[str, set[str]]:
    """SCHEMA 정본 + 마이그레이션 ALTER 문에서 테이블별 컬럼 집합을 만든다."""
    src = (ROOT / "server" / "database" / "core.py").read_text(encoding="utf-8")
    cols: dict[str, set[str]] = {}
    # CREATE TABLE [IF NOT EXISTS] <name> ( ... )  — 괄호 균형으로 본문을 자른다.
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(", src,
                         re.IGNORECASE):
        name = m.group(1)
        i, depth = m.end(), 1
        while i < len(src) and depth:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
            i += 1
        body = src[m.end():i - 1]
        found = set()
        for line in body.split(","):
            line = line.strip()
            if not line or line.upper().startswith(
                    ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT")):
                continue
            c = re.match(r"[\"'`]?([A-Za-z_][A-Za-z0-9_]*)[\"'`]?", line)
            if c:
                found.add(c.group(1))
        cols.setdefault(name, set()).update(found)
    # 마이그레이션으로 나중에 붙는 컬럼도 정상이다.
    for m in re.finditer(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+[\"'`]?(\w+)", src,
                         re.IGNORECASE):
        cols.setdefault(m.group(1), set()).add(m.group(2))
    return cols


def _changed_files() -> list[Path]:
    """작업트리에서 바뀐 검사 대상 파일 (Stop 훅용)."""
    try:
        out = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    files = []
    for line in out.splitlines():
        p = ROOT / line.strip()
        if p.suffix == ".py" and p.is_file() and line.split("/")[0] in _SCAN_DIRS:
            files.append(p)
    return files


def _all_files() -> list[Path]:
    out = []
    for d in _SCAN_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if not any(s in p.parts for s in _SKIP_PARTS):
                out.append(p)
    return out


def scan(files) -> list[dict]:
    cols = _schema_columns()
    hits = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _ALLOW_RE.search(line):
                continue
            for m in _GET_RE.finditer(line):
                var, key = m.group(1), m.group(2)
                table = _VAR_TABLE[var]
                known = cols.get(table, set()) | _EXTRA_OK.get(table, set())
                if not known:                     # 스키마 파싱 실패 — 침묵(오탐 방지)
                    continue
                if key not in known:
                    try:
                        shown = str(path.relative_to(ROOT))
                    except ValueError:      # 저장소 밖(테스트 임시 파일 등)
                        shown = str(path)
                    hits.append({"file": shown.replace("\\", "/"),
                                 "line": n, "var": var, "key": key, "table": table,
                                 "near": _near(key, known)})
    return hits


def _near(key: str, known: set[str]) -> str:
    """오타일 때 '혹시 이것?' — 편집거리 대신 부분 일치로 충분하다."""
    import difflib
    c = difflib.get_close_matches(key, sorted(known), n=2, cutoff=0.6)
    return ", ".join(c)


def _fmt(hits) -> str:
    out = []
    for h in hits:
        out.append(f"  {h['file']}:{h['line']}  {h['var']}.get(\"{h['key']}\")")
        out.append(f"    → {h['table']} 에 그런 컬럼이 없습니다."
                   + (f" 혹시: {h['near']}" if h["near"] else ""))
    out.append("")
    out.append("  .get() 은 없는 키에 None 을 돌려주므로 **예외가 나지 않습니다** —")
    out.append("  기능이 조용히 죽고 화면에는 '원래 없는 것'처럼 보입니다.")
    out.append("  의도한 키라면 그 줄에 `# schema-guard: allow (사유)` 를 다세요.")
    return "\n".join(out)


def main() -> int:
    stop = "--stop" in sys.argv
    files = _changed_files() if stop else _all_files()
    if not files:
        return 0
    hits = scan(files)
    if not hits:
        if not stop:
            print("schema_guard: 위반 없음")
        return 0
    msg = ("DB 컬럼 이름이 실제 스키마에 없습니다 (오타이거나 폐지된 컬럼):\n\n"
           + _fmt(hits))
    if stop:
        print(json.dumps({"decision": "block", "reason": msg}, ensure_ascii=False))
        return 0
    print(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())

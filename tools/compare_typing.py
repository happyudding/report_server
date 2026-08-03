"""손타이핑 사본 ↔ 원본 대조기 (표준 라이브러리 전용).

보안망 안에서 원본 폴더와 대조할 때 쓴다. 사용자가 docstring 을 일부러 안 쳤고 공백·줄바꿈도
원본과 다르므로, 단순 diff 는 노이즈로 뒤덮인다. 그래서 .py 는 **AST 로 비교**한다 —
주석·공백·줄바꿈·docstring 은 애초에 AST 에 없거나 여기서 제거하므로 diff 에 뜨지 않고,
"로직/이름/문자열/상수" 가 다를 때만 걸린다.

    python compare_typing.py <내_사본> <원본> [--docs] [--out report.txt]

출력은 파일명과 정의(함수/클래스/상수) 이름까지만 — 소스 내용은 찍지 않는다.
--docs 를 주면 docstring 과 .md 도 비교 대상에 넣는다(기본 off).

한국어 Windows 콘솔(cp949)에서 글자가 깨지면 `chcp 65001` 을 먼저 실행하거나
`--out report.txt` 로 UTF-8 파일에 받아 편집기로 열면 된다.

의존성 0. pyyaml 이 있으면 yaml 을 파싱해 비교하고, 없으면 공백 정규화 텍스트로 폴백한다.
"""
import argparse
import ast
import json
import pathlib
import sys

# cp949 콘솔에 없는 문자가 섞여도 죽지 않게 (기호는 ASCII 만 쓴다)
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # 구버전 파이썬 / 리다이렉트 환경
    pass

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv", "data", "output",
                ".mypy_cache", ".ruff_cache"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".parquet"}
CODE_SUFFIX = {".py"}
YAML_SUFFIX = {".yaml", ".yml"}
TEXT_SUFFIX = {".md", ".txt", ".cfg", ".ini", ".toml", ".bat", ".ps1", ".csv"}

_REPORT = []


def emit(line=""):
    """콘솔에 찍고 --out 저장용으로도 모아둔다."""
    _REPORT.append(line)
    print(line)


def collect(root: pathlib.Path) -> dict:
    """root 아래 비교 대상 파일 → {상대경로(posix): Path}."""
    out = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if p.suffix.lower() in EXCLUDE_SUFFIX:
            continue
        out[rel.as_posix()] = p
    return out


def read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# ---------- .py: AST 비교 ----------

def _strip_docstrings(tree, keep: bool):
    """모든 Module/Class/Function 의 선두 문자열 리터럴 제거 (keep=True 면 보존)."""
    if keep:
        return tree
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return tree


def _dump(node) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _unit_name(stmt):
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return stmt.name
    if isinstance(stmt, ast.Assign):
        names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
        return ",".join(names) if names else None
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    return None


def py_units(tree) -> dict:
    """모듈을 '정의 단위' 로 쪼갠다. import 는 순서 무시하려고 한 덩어리로 묶는다."""
    units, imports, misc = {}, [], []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            imports.append(_dump(stmt))
            continue
        name = _unit_name(stmt)
        if name is None:
            misc.append(_dump(stmt))
            continue
        if isinstance(stmt, ast.ClassDef):
            # 클래스는 메서드 단위까지 내려가서 어디가 다른지 좁힌다
            body, stmt.body = stmt.body, [ast.Pass()]
            units[f"class {name}"] = _dump(stmt)
            stmt.body = body
            for i, sub in enumerate(body):
                sub_name = _unit_name(sub)
                units[f"{name}.{sub_name}" if sub_name else f"{name}.<stmt{i}>"] = _dump(sub)
            continue
        units[name] = _dump(stmt)
    if imports:
        units["<imports>"] = "\n".join(sorted(imports))
    if misc:
        units["<module-level>"] = "\n".join(misc)
    return units


def compare_py(a_src: str, b_src: str, keep_docs: bool):
    """→ (상태, 상세). 상태: 'same' | 'diff' | 'parse_error'."""
    try:
        ta = _strip_docstrings(ast.parse(a_src), keep_docs)
        tb = _strip_docstrings(ast.parse(b_src), keep_docs)
    except SyntaxError as e:
        return "parse_error", f"구문 오류 line {e.lineno} — {e.msg}"
    if _dump(ta) == _dump(tb):
        return "same", ""
    ua, ub = py_units(ta), py_units(tb)
    return "diff", {
        "changed": [k for k in ua if k in ub and ua[k] != ub[k]],
        "only_mine": [k for k in ua if k not in ub],
        "only_orig": [k for k in ub if k not in ua],
    }


# ---------- .yaml / 텍스트 ----------

try:
    import yaml as _yaml
except ImportError:
    _yaml = None


def norm_text(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n")
                     if line.strip())


def compare_yaml(a_src: str, b_src: str):
    if _yaml is None:
        return ("same" if norm_text(a_src) == norm_text(b_src) else "diff",
                "(pyyaml 없음 - 텍스트 정규화 비교)")
    try:
        da, db = _yaml.safe_load(a_src), _yaml.safe_load(b_src)
    except Exception as e:
        return "parse_error", f"YAML 파싱 실패: {str(e).splitlines()[0]}"
    ja = json.dumps(da, sort_keys=True, ensure_ascii=False)
    jb = json.dumps(db, sort_keys=True, ensure_ascii=False)
    return ("same" if ja == jb else "diff"), ""


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="손타이핑 사본 <-> 원본 AST 대조")
    ap.add_argument("mine", help="내가 손으로 친 사본 폴더")
    ap.add_argument("orig", help="원본(업데이트된) 폴더")
    ap.add_argument("--docs", action="store_true",
                    help="docstring 과 .md/.txt 도 비교 (기본: 무시)")
    ap.add_argument("--out", help="결과를 UTF-8 텍스트 파일로도 저장 (콘솔 깨짐 회피용)")
    args = ap.parse_args()

    mine, orig = pathlib.Path(args.mine), pathlib.Path(args.orig)
    for d in (mine, orig):
        if not d.is_dir():
            emit(f"폴더 없음: {d}")
            return 2

    fa, fb = collect(mine), collect(orig)
    only_mine = sorted(set(fa) - set(fb))
    only_orig = sorted(set(fb) - set(fa))
    common = sorted(set(fa) & set(fb))

    emit("=" * 70)
    emit(f"내 사본 : {mine}   ({len(fa)} 파일)")
    emit(f"원본    : {orig}   ({len(fb)} 파일)")
    emit(f"docstring/문서 비교: {'ON' if args.docs else 'OFF (기본)'}")
    emit("=" * 70)

    emit("")
    emit("[1] 파일 목록 차이")
    emit("  내 사본에만 있음: " + (", ".join(only_mine) or "(없음)"))
    emit("  원본에만 있음   : " + (", ".join(only_orig) or "(없음)")
         + "    <- 여기 뜨면 통째로 안 친 파일")

    n_same = n_diff = n_err = n_skip = 0
    diffs, errs = [], []
    for rel in common:
        suf = fa[rel].suffix.lower()
        a_src, b_src = read(fa[rel]), read(fb[rel])
        if suf in CODE_SUFFIX:
            state, detail = compare_py(a_src, b_src, args.docs)
        elif suf in YAML_SUFFIX:
            state, detail = compare_yaml(a_src, b_src)
        elif suf in TEXT_SUFFIX:
            if not args.docs:
                n_skip += 1
                continue
            state = "same" if norm_text(a_src) == norm_text(b_src) else "diff"
            detail = "(텍스트 정규화 비교)"
        else:
            state = "same" if a_src == b_src else "diff"
            detail = "(바이트 비교)"

        if state == "same":
            n_same += 1
        elif state == "parse_error":
            n_err += 1
            errs.append((rel, detail))
        else:
            n_diff += 1
            diffs.append((rel, detail))

    emit("")
    emit("[2] 내용 불일치")
    if not diffs:
        emit("  (없음)")
    for rel, detail in diffs:
        emit(f"  * {rel}")
        if isinstance(detail, dict):
            if detail["changed"]:
                emit("      다르게 친 정의 : " + ", ".join(detail["changed"]))
            if detail["only_mine"]:
                emit("      내 사본에만    : " + ", ".join(detail["only_mine"]))
            if detail["only_orig"]:
                emit("      원본에만       : " + ", ".join(detail["only_orig"])
                     + "    <- 안 친 부분")
        elif detail:
            emit("      " + detail)

    if errs:
        emit("")
        emit("[3] 파싱 실패 (먼저 고칠 것 - 비교 자체가 불가)")
        for rel, detail in errs:
            emit(f"  X {rel}: {detail}")

    emit("")
    emit("=" * 70)
    emit(f"공통 {len(common)} / 일치 {n_same} / 불일치 {n_diff} / 파싱실패 {n_err} / 건너뜀 {n_skip}")
    emit(f"한쪽에만 있는 파일: 내 사본 {len(only_mine)} · 원본 {len(only_orig)}")
    emit("=" * 70)

    if args.out:
        pathlib.Path(args.out).write_text("\n".join(_REPORT) + "\n", encoding="utf-8")
        print(f"\n[저장] {args.out}")
    return 1 if (diffs or errs or only_orig) else 0


if __name__ == "__main__":
    sys.exit(main())

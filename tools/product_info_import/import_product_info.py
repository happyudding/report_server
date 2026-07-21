"""기준정보 CSV(DRM) → product_info.db 오프라인 임포터.

원본 기준정보 CSV 는 NASCA DRM 으로 암호화돼 있어 평문으로 읽을 수 없다. 서버는 Excel 을
쓰지 않으므로(report_server CLAUDE.md 불변 규칙 #1) 서버가 직접 열 수 없고, 대신 **Excel 이
설치된 별도 PC** 에서 이 스크립트를 돌려 SQLite(product_info.db)로 변환한 뒤 그 파일 하나를
서버로 복사한다. 서버는 그 DB 를 읽기 전용으로만 연다(server/product_info.py).

이 스크립트는 standalone 이다 — report_server 의 config/database 모듈을 import 하지 않으며,
이 폴더만 통째로 복사해 가면 어디서든 돈다. 필요한 것은 Python 3.9+ 와 pywin32 뿐이다.

재실행 안전: 산출물은 임시 파일에 전량 새로 쓴 뒤 os.replace 로 원자적 교체한다. 중간에
실패하면 기존 .db 는 손대지 않은 채로 남는다(반쯤 쓰인 파일이 생기지 않는다).

사용:
    run_import.bat                              (더블클릭 — 파일 선택 다이얼로그)
    python import_product_info.py <csv> [--out PATH]
    python import_product_info.py <csv> --plain   (DRM 없는 평문 CSV — 개발/검증용)
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

try:  # Windows 콘솔(cp949)에서 한국어 출력 보장 — run_import.bat 의 chcp 65001 과 짝
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

SCHEMA_VERSION = "1"
IMPORTER_VERSION = "1"

# CSV 헤더 고정 목록. 실제 CSV 와 **순서까지** 일치해야 한다(불일치면 exit 2).
# 여기를 고치면 server/product_info.py 의 INFO_COLUMNS 와 docs/09_db_inventory.md 도 함께 볼 것.
COLUMNS = (
    "id", "part_id", "sub_part_id", "product_code", "product_group", "fab_type",
    "wf_size", "chip_size_x", "chip_size_y", "gross_die", "pkg_type", "prod_type",
    "inking", "e2f_max_bin", "e2f_bin_list", "e2f_fab_site", "lot_type",
    "mapfile_type", "osat", "step", "temperature", "times", "retest", "fy", "fx",
    "sanding", "sanding_value", "equip", "equip_name", "para", "flat_zone",
    "pgm_name", "pgm_dir", "dev_user_id", "prod_user_id", "pe_user_id",
    "approve_user_id", "create_date", "update_date", "approve_date", "draft_user_id",
)

# 별도 .db 파일이어도 테이블명은 report_ prefix 를 유지한다 (불변 규칙 #2, voc_db.py 선례).
# 전 컬럼 TEXT 인 이유: 서버는 이 값들로 산술을 하지 않고 문자열로만 다룬다. REAL 로 담으면
# chip_size_x "5.20" 이 5.2 가 되어 세션에 저장되는 표시값이 바뀐다.
SCHEMA = """
CREATE TABLE IF NOT EXISTS report_product_info (
    row_no INTEGER NOT NULL,
    %s
);

CREATE TABLE IF NOT EXISTS report_product_info_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""" % ",\n    ".join(f"{c} TEXT" for c in COLUMNS)


# ── 값 정규화 ────────────────────────────────────────────────────────────────

def _cell_to_text(value, counters):
    """Excel COM 셀값 → 문자열. client/report_flow/upload_prepare.py _normalize_grid 변형.

    Excel 은 CSV 를 열 때 타입을 추론하므로 "1520" 이 1520.0(float) 으로 돌아온다.
    정수형 float 를 int 문자열로 되돌려 원본 표기를 최대한 복원한다.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # CSV 에서 나올 일이 없다. 나왔다면 Excel 이 TRUE/FALSE 로 해석한 것이라 알려야 한다.
        counters["bool_cells"] += 1
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # 1e15 를 넘으면 float 정밀도가 정수를 표현하지 못하므로 int 변환이 거짓말이 된다.
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)          # 파이썬 3 최단 왕복 표현 (5.2 → "5.2")
    if isinstance(value, str):
        return value.strip()
    if hasattr(value, "year"):      # pywintypes datetime 류
        try:
            if (int(value.hour), int(value.minute), int(value.second)) == (0, 0, 0):
                return "%04d-%02d-%02d" % (int(value.year), int(value.month), int(value.day))
            return "%04d-%02d-%02d %02d:%02d:%02d" % (
                int(value.year), int(value.month), int(value.day),
                int(value.hour), int(value.minute), int(value.second))
        except Exception:  # noqa: BLE001
            return str(value).strip()
    return str(value).strip()


# ── 입력 리더 2종 (COM / 평문) — 둘 다 [[셀문자열, ...], ...] 를 돌려준다 ──────────

def _read_rows_via_com(csv_path, counters):
    """Excel COM 으로 DRM CSV 를 열어 전 셀을 문자열 2D 리스트로 반환.

    win32com/pythoncom 은 함수 안에서 지연 import 한다 — 관례이기도 하고, pywin32 가 없는
    PC 에서도 모듈 import 와 --plain 경로가 동작해야 하기 때문이다.
    DRM 해제는 Excel 이 처리하므로 이 PC 의 DRM 클라이언트에 로그인돼 있어야 한다.
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    excel = None
    wb = None
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        # Format=2 = 쉼표 구분. 다만 Excel 이 시스템 목록 구분자를 우선할 수 있어
        # 신뢰하지 않고 아래에서 헤더 셀 수로 다시 검증한다.
        wb = excel.Workbooks.Open(str(Path(csv_path).resolve()),
                                  UpdateLinks=0, ReadOnly=True, Format=2)
        ws = wb.Worksheets(1)                  # CSV 는 시트가 1개다
        used = ws.UsedRange
        if int(used.Row) != 1 or int(used.Column) != 1:
            raise ValueError(
                "UsedRange 가 A1 에서 시작하지 않음 (R%sC%s) — CSV 가 아닐 수 있습니다"
                % (used.Row, used.Column))
        raw = used.Value                       # COM 객체를 벗어나기 전에 값을 다 끌어온다
    finally:
        try:
            if wb is not None:
                wb.Close(SaveChanges=False)
        except Exception:  # noqa: BLE001
            pass
        try:
            if excel is not None:
                excel.Quit()
        except Exception:  # noqa: BLE001
            pass
        pythoncom.CoUninitialize()

    if not isinstance(raw, (tuple, list)):     # 1셀짜리
        return [[_cell_to_text(raw, counters)]]
    if raw and not isinstance(raw[0], (tuple, list)):   # 1행짜리
        return [[_cell_to_text(v, counters) for v in raw]]
    return [[_cell_to_text(v, counters) for v in row] for row in raw]


def _read_rows_plain(csv_path, counters):
    """평문 CSV 를 그대로 읽는다 (--plain). DRM 파일에는 쓸 수 없다.

    한국 Windows 의 Excel 이 저장한 CSV 는 cp949 일 수 있어 utf-8(BOM 허용) → cp949 순으로
    시도한다. 서버에서 걷어낸 인코딩 판별 로직을 이 경로가 이어받는다.
    """
    raw = Path(csv_path).read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("utf-8/cp949 어느 쪽으로도 디코딩할 수 없음")
    reader = csv.reader(text.splitlines())
    return [[_cell_to_text(v, counters) for v in row] for row in reader]


# ── 헤더 검증 ────────────────────────────────────────────────────────────────

def _check_header(header):
    """헤더가 COLUMNS 와 순서까지 일치하는지 검사. 문제가 있으면 진단 문자열 리스트 반환."""
    actual = [c.strip().lower() for c in header]
    if actual == list(COLUMNS):
        return []
    problems = ["기대 %d컬럼 / 실제 %d컬럼" % (len(COLUMNS), len(actual))]
    if len(actual) == 1:
        problems.append("헤더가 1셀로 뭉쳤습니다 — 쉼표 구분자 해석 실패로 보입니다")
        problems.append("실제 첫 셀: %r" % (actual[0][:120],))
        return problems
    missing = [c for c in COLUMNS if c not in actual]
    extra = [c for c in actual if c not in COLUMNS]
    problems.append("누락: %s" % (", ".join(missing) if missing else "(없음)"))
    problems.append("추가: %s" % (", ".join(extra) if extra else "(없음)"))
    for i in range(min(len(actual), len(COLUMNS))):
        if actual[i] != COLUMNS[i]:
            problems.append("위치 %d: 기대 %r / 실제 %r" % (i + 1, COLUMNS[i], actual[i]))
            break
    return problems


# ── 쓰기 ─────────────────────────────────────────────────────────────────────

def _write_db(data_rows, out_path, meta_pairs):
    """임시 파일에 전량 기록 후 원자적 교체. 기존 .db 는 성공할 때만 대체된다.

    WAL 을 쓰지 않는다(voc_db.py 와 의도적으로 다름) — WAL 은 -wal/-shm 사이드카를 만들고,
    그 상태의 .db 를 사이드카 없이 손으로 복사하면 마지막 커밋이 유실될 수 있다. 이 산출물은
    "파일 하나만 옮기기" 가 요구사항이라 단일 자족 파일이어야 한다.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    cols = ", ".join(COLUMNS)
    marks = ", ".join("?" * (len(COLUMNS) + 1))
    conn = sqlite3.connect(str(tmp))
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO report_product_info (row_no, %s) VALUES (%s)" % (cols, marks),
            data_rows)
        conn.executemany(
            "INSERT INTO report_product_info_meta (key, value) VALUES (?, ?)", meta_pairs)
        conn.commit()
    finally:
        conn.close()
    os.replace(str(tmp), str(out_path))


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="기준정보 CSV(DRM) → product_info.db 변환")
    parser.add_argument("csv_path", help="기준정보 CSV 경로")
    parser.add_argument("--out", default=None,
                        help="출력 .db 경로 (기본: 스크립트 옆 output/product_info.db)")
    parser.add_argument("--plain", action="store_true",
                        help="Excel COM 대신 평문으로 읽는다 (DRM 없는 CSV 전용)")
    args = parser.parse_args(argv)

    started = time.time()
    csv_path = Path(args.csv_path)
    out_path = (Path(args.out) if args.out
                else Path(__file__).resolve().parent / "output" / "product_info.db")

    if not csv_path.is_file():
        print("[error] CSV 를 찾을 수 없습니다: %s" % csv_path)
        return 1

    print("[import] CSV: %s" % csv_path.resolve())
    counters = {"bool_cells": 0}
    try:
        if args.plain:
            print("[import] 평문 읽기 (--plain) ...")
            rows = _read_rows_plain(csv_path, counters)
        else:
            print("[import] Excel COM 열기 ... (DRM 프롬프트가 뜨면 승인하세요)")
            rows = _read_rows_via_com(csv_path, counters)
    except ImportError as exc:
        print("[error] pywin32 를 불러올 수 없습니다: %s" % exc)
        print("[error]   pip install -r requirements.txt 를 먼저 실행하세요.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print("[error] CSV 읽기 실패: %s" % exc)
        return 1

    if not rows:
        print("[error] 빈 파일입니다 — DB 를 만들지 않았습니다.")
        return 3

    problems = _check_header(rows[0])
    if problems:
        print("[error] 헤더 불일치 — DB 를 만들지 않았습니다.")
        for line in problems:
            print("[error]   %s" % line)
        print("[error]   CSV 스키마가 바뀌었다면 import_product_info.py 의 COLUMNS 와")
        print("[error]   server/product_info.py, docs/09_db_inventory.md 를 함께 갱신하세요.")
        return 2
    print("[import] 헤더 %d컬럼 확인 OK" % len(COLUMNS))

    ncol = len(COLUMNS)
    data_rows = []
    skipped_empty = no_key = dup_id = 0
    seen_ids = set()
    for row in rows[1:]:
        cells = list(row[:ncol]) + [""] * max(0, ncol - len(row))
        if not any(cells):
            skipped_empty += 1
            continue
        record = dict(zip(COLUMNS, cells))
        if not record["part_id"] and not record["sub_part_id"]:
            no_key += 1
        rid = record["id"]
        if rid:
            if rid in seen_ids:
                dup_id += 1
            seen_ids.add(rid)
        data_rows.append([len(data_rows) + 1] + cells)

    if not data_rows:
        print("[error] 데이터 행이 0건입니다 — DB 를 만들지 않았습니다.")
        return 3
    print("[import] 데이터 %d행 읽음 (빈행 %d 건너뜀)" % (len(data_rows), skipped_empty))
    if no_key:
        print("[warn]  part_id/sub_part_id 둘 다 빈 행 %d건 — 검색 후보로 안 잡힙니다" % no_key)
    if dup_id:
        print("[warn]  id 중복 %d건 — 같은 키가 여러 행이면 첫 행이 우선합니다" % dup_id)
    if counters["bool_cells"]:
        print("[warn]  TRUE/FALSE 로 해석된 셀 %d건 — 원본 표기를 확인하세요"
              % counters["bool_cells"])

    stat = csv_path.stat()
    now = time.time()
    meta_pairs = [
        ("schema_version", SCHEMA_VERSION),
        ("importer_version", IMPORTER_VERSION),
        ("imported_at", datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S")),
        ("imported_at_epoch", str(int(now))),
        ("source_csv", str(csv_path.resolve())),
        ("source_mtime", str(int(stat.st_mtime))),
        ("source_size", str(stat.st_size)),
        ("row_count", str(len(data_rows))),
        ("columns", ",".join(COLUMNS)),
        ("reader", "plain" if args.plain else "excel_com"),
    ]

    try:
        _write_db(data_rows, out_path, meta_pairs)
    except Exception as exc:  # noqa: BLE001
        print("[error] DB 쓰기 실패: %s" % exc)
        return 1

    size_kb = out_path.stat().st_size / 1024.0
    print("[import] 기록: %s (%d행, %.1f KB)" % (out_path.resolve(), len(data_rows), size_kb))
    print("[import] 완료 — rows=%d skipped_empty=%d no_key=%d dup_id=%d elapsed=%.1fs"
          % (len(data_rows), skipped_empty, no_key, dup_id, time.time() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main())

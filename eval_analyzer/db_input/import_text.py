"""Import AI-extracted precedent JSON rows.

LLM extraction is not implemented yet.  For now this CLI accepts JSON rows that
already match the db_input CSV schema, validates them, previews them, optionally
writes CSV, and optionally saves them through import_csv._import_group().
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from db_input import ai_extract, import_csv  # noqa: E402


def _read_rows(args):
    if args.json:
        rows, source = ai_extract.load_json_rows(args.json), Path(args.json)
    elif args.input:
        with open(args.input, encoding="utf-8-sig") as f:
            text = f.read()
        rows, source = ai_extract.extract_rows_from_text(text), Path(args.input)
    else:
        raise ValueError("--json 또는 --input 중 하나가 필요합니다.")
    # import_csv._import_group 은 CSV 문자열 행 전제(.strip()) — JSON 숫자/None 을 문자열로 정규화
    rows = [{k: "" if v is None else str(v) for k, v in row.items()} for row in rows]
    return rows, source


def _print_preview(validation):
    for item in validation:
        row = item["row"]
        name = row.get("item_name") or "<missing item_name>"
        product = row.get("product_name") or "<missing product_name>"
        print(f"[{item['index']}] {item['status']} product={product} item={name}")
        for err in item["errors"]:
            print(f"  - {err}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--json", help="AI 추출 결과 JSON 파일(row list 또는 {'rows': [...]})")
    source.add_argument("--input", help="원문 텍스트 파일. 현재는 LLM 미구현으로 실패합니다.")
    parser.add_argument("--preview", action="store_true", help="검증 결과를 출력합니다.")
    parser.add_argument("--write-csv", help="기존 import_csv 호환 CSV로 저장합니다.")
    parser.add_argument("--save", action="store_true", help="검증 통과 시 DB에 저장합니다.")
    parser.add_argument("--to-eval-db", action="store_true",
                        help="제품군별 output DB 대신 config.DB_PATH(eval.db)에 저장합니다.")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        rows, source_path = _read_rows(args)
    except NotImplementedError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    validation = ai_extract.validate_rows(rows)
    should_preview = args.preview or (not args.write_csv and not args.save)
    if should_preview:
        _print_preview(validation)

    if args.write_csv:
        out = ai_extract.rows_to_csv(args.write_csv, rows)
        print(f"CSV written: {out}")

    if args.save:
        if not ai_extract.all_ready(validation):
            print("검증 실패 행이 있어 저장하지 않았습니다.", file=sys.stderr)
            if not should_preview:
                _print_preview(validation)
            return 1
        for product_type, family_product, count, db_path, session_id in import_csv.import_rows(
                rows, source_path, args.to_eval_db):
            suffix = f" (session_id={session_id})" if session_id else ""
            print(f"[{product_type}_{family_product}] {count}건 적재 -> {db_path}{suffix}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

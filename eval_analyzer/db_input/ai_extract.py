"""AI-extracted precedent rows validation and CSV export helpers.

This module intentionally does not call an LLM yet.  The future LLM boundary is
`extract_rows_from_text()`: once implemented, it should return rows compatible
with `db_input/import_csv.py`.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from eval_engine.pipeline._rules import validate_outcome
from eval_engine.pipeline.ingest import _validate_product_meta

from db_input.import_csv import REQUIRED_COLUMNS  # 필수 컬럼 정본은 import_csv

CSV_COLUMNS = [
    "product_name",
    "product_type",
    "family_product",
    "lot_id",
    "wafer_number",
    "revision",
    "item_name",
    "value_type",
    "bin",
    "USL",
    "LSL",
    "average",
    "stdev",
    "human_comment",
    "session_id",
    "human_status",
    "root_cause_category",
    "outcome_action",
    "outcome_condition",
    "outcome_result",
]

NUMERIC_COLUMNS = ["bin", "revision", "USL", "LSL", "average", "stdev", "wafer_number"]


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _can_float(value: Any) -> bool:
    if _is_blank(value):
        return True
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _row_error(index: int, row: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    return {
        "index": index,
        "status": "BLOCKED" if errors else "READY",
        "errors": errors,
        "row": row,
    }


def load_json_rows(path: str | Path) -> list[dict[str, Any]]:
    """Load rows from a JSON file.

    Accepted shapes:
      - [{...}, {...}]
      - {"rows": [{...}, {...}]}
    """
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, dict) and "rows" in data:
        data = data["rows"]
    if not isinstance(data, list):
        raise ValueError("JSON 입력은 row list 또는 {'rows': [...]} 형태여야 합니다.")
    if not all(isinstance(r, dict) for r in data):
        raise ValueError("JSON rows의 각 항목은 object여야 합니다.")
    return data


def validate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate extracted rows without writing DB state."""
    results = []
    for idx, row in enumerate(rows, start=1):
        errors = []

        for col in REQUIRED_COLUMNS:
            if _is_blank(row.get(col)):
                errors.append(f"필수값 누락: {col}")

        for col in NUMERIC_COLUMNS:
            if not _can_float(row.get(col)):
                errors.append(f"숫자 변환 불가: {col}={row.get(col)!r}")

        if not _is_blank(row.get("product_type")) and not _is_blank(row.get("family_product")):
            try:
                _validate_product_meta({
                    "product_type": str(row.get("product_type")).strip(),
                    "family_product": str(row.get("family_product")).strip(),
                })
            except ValueError as exc:
                errors.append(str(exc))

        action = None if _is_blank(row.get("outcome_action")) else str(row["outcome_action"]).strip()
        result = None if _is_blank(row.get("outcome_result")) else str(row["outcome_result"]).strip()
        try:
            validate_outcome(action, result)
        except ValueError as exc:
            errors.append(str(exc))

        results.append(_row_error(idx, row, errors))
    return results


def all_ready(validation: list[dict[str, Any]]) -> bool:
    return all(v["status"] == "READY" for v in validation)


def rows_to_csv(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    """Write rows in the existing import_csv-compatible CSV shape."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: "" if row.get(col) is None else row.get(col, "")
                             for col in CSV_COLUMNS})
    return out


def extract_rows_from_text(text: str) -> list[dict[str, Any]]:
    """Future LLM hook.

    LLM connection/prompting is intentionally out of scope for this change.
    """
    raise NotImplementedError("LLM extractor not implemented yet")

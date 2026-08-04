"""Issue Table row_key 파싱 — 챗봇이 item 이름으로 이슈를 찾기 위한 유일한 통로.

row_key 규약의 정본은 `web_report/tabs/issue_table.py` 상단 주석이다:

    Yield|<bin>|<item>   수율 이슈 (행 = bin × item)
    CPK|<item>           cpk < 1.33 이슈
    ETC|<item>           자유 입력(ETC) 항목

`report_webreport_edit` 의 저장 키는 kind 마다 다르다 — 이 비대칭이 조인의 함정이다:

    issue_comment : item_key = row_key + "\\x1f" + col      (item 단위)
    issue_status  : item_key = "Yield|<bin>" | "CPK|<item>" | "ETC|<item>"  (이슈 단위)
    issue_hidden  : issue_status 와 같은 이슈 키

즉 **Yield 이슈만 comment 는 item 단위인데 Status 는 bin 단위**라, item 으로 코멘트를
찾은 뒤 Status 를 보려면 bin 으로 되짚어야 한다(`status_key()`).

`web_report/eval_export.py:_parse_row_key` 와 별개로 두는 이유: 그쪽은 export 의미론
(Pass 요약행 skip, CPK→bin 1 치환)이라 화면에 보이는 이슈를 그대로 돌려주지 않는다.
여기는 표시 의미론이다.
"""
from __future__ import annotations

from typing import NamedTuple

from web_report import edits as _edits

# issue_comment item_key 의 구분자 — web_report/edits.py 의 정본을 재사용한다
# (eval_export.py:128 도 같은 방식으로 참조한다).
SEP = _edits._SEP

CATEGORIES = ("Yield", "CPK", "ETC")


class RowKey(NamedTuple):
    category: str        # "Yield" | "CPK" | "ETC"
    bin: int | None      # Yield 만 값이 있다
    item: str


def parse(row_key: str) -> RowKey | None:
    """row_key → RowKey. 규약에 안 맞으면 None."""
    text = str(row_key or "")
    if text.startswith("Yield|"):
        parts = text.split("|", 2)
        if len(parts) != 3 or not parts[2]:
            return None
        try:
            bin_ = int(float(parts[1]))
        except (TypeError, ValueError):
            return None
        return RowKey("Yield", bin_, parts[2])
    for cat in ("CPK", "ETC"):
        prefix = cat + "|"
        if text.startswith(prefix) and text[len(prefix):]:
            return RowKey(cat, None, text[len(prefix):])
    return None


def split_comment_key(item_key: str) -> tuple[str, str]:
    """issue_comment 의 item_key → (row_key, col). 구분자가 없으면 (item_key, "")."""
    row_key, _, col = str(item_key or "").partition(SEP)
    return row_key, col


def status_key(rk: RowKey) -> str:
    """RowKey → issue_status/issue_hidden 이 쓰는 이슈 단위 키."""
    if rk.category == "Yield":
        return f"Yield|{rk.bin}"
    return f"{rk.category}|{rk.item}"


def parse_status_key(key: str) -> RowKey | None:
    """issue_status/issue_hidden 의 키 → RowKey.

    `parse()` 와 다른 함수인 이유: Yield 는 상태 키가 `Yield|<bin>` 2조각이라
    row_key(3조각) 규약으로는 파싱되지 않는다(item 이 비어 있는 것이 정상).
    """
    text = str(key or "")
    if text.startswith("Yield|"):
        try:
            return RowKey("Yield", int(float(text.split("|", 1)[1])), "")
        except (TypeError, ValueError, IndexError):
            return None
    return parse(text)

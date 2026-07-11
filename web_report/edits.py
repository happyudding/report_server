"""web_report 편집 상태 (comment/override) — 세션 단위 DB 저장 (2026-07-11).

manifest 는 업로드 시점 **불변 스냅샷**이다. Issue Table comment / ETC item /
Trim override / Summary Engr comment 편집은 report_webreport_edit 테이블에만
기록되고, dedup(동일 analysis_key) 세션 간에 공유되지 않는다. 캐시 무효화는
report_webreport_edit_rev 의 단조 증가 rev 로 한다 (service 캐시 키에 포함).

하위호환(legacy 미이전 세션): rev==0 이면 조회는 manifest 필드로 폴백하고
(effective_state), 첫 편집 직전에 ensure_seeded 가 manifest 값을 세션 편집행으로
복사해 연속성을 보존한다. server/tools/migrate_manifest_edits.py 는 같은 시드를
전 세션에 일괄 적용하는 운영 도구다.
"""
from __future__ import annotations

import json
import re

KIND_ISSUE_COMMENT = "issue_comment"
KIND_ETC_ITEM = "etc_item"
KIND_TRIM_OVERRIDE = "trim_override"
KIND_SUMMARY_ENGR = "summary_engr"

# issue_comment 의 item_key = row_key + SEP + col (row_key 에 '|' 가 쓰여 제어문자 사용)
_SEP = "\x1f"

_HONEY_UA_RE = re.compile(r"HoneyUser/(\S+)")


def user_from_ua(user_agent: str) -> str:
    """User-Agent 문자열에서 HoneyUser 계정 추출 (auth_identity 와 동일 규칙).

    updated_by 기록용 — flask request 무의존이라 service 계층에서 그대로 쓴다."""
    m = _HONEY_UA_RE.search(str(user_agent or ""))
    if not m:
        return ""
    try:
        from urllib.parse import unquote
        return unquote(m.group(1)).strip().lower()
    except Exception:
        return ""


def comment_key(row_key: str, col: str) -> str:
    return f"{row_key}{_SEP}{col}"


def state_from_manifest(manifest: dict) -> dict:
    """manifest 의 편집 필드를 편집 상태 dict 로 (legacy 폴백·시드 공용)."""
    manifest = manifest or {}
    return {
        "issue_comments": dict(manifest.get("issue_comments") or {}),
        "etc_items": list(manifest.get("etc_items") or []),
        "trim_overrides": dict(manifest.get("trim_overrides") or {}),
        "summary_engr": dict(manifest.get("summary_engr") or {}),
    }


def load_edit_state(report_db, session_id: str) -> dict:
    """DB 편집행 → manifest 필드와 동일한 형태의 상태 dict.

    etc_items 순서는 rowid(삽입) 순서 — get_webreport_edits 가 보장한다."""
    state = {"issue_comments": {}, "etc_items": [], "trim_overrides": {}, "summary_engr": {}}
    for row in report_db.get_webreport_edits(session_id):
        kind, item_key, value = row["kind"], row["item_key"], row["value"]
        if kind == KIND_ISSUE_COMMENT:
            row_key, _, col = item_key.partition(_SEP)
            if row_key and col:
                state["issue_comments"].setdefault(row_key, {})[col] = value
        elif kind == KIND_ETC_ITEM:
            state["etc_items"].append(item_key)
        elif kind == KIND_TRIM_OVERRIDE:
            try:
                spec = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(spec, dict):
                state["trim_overrides"][item_key] = spec
        elif kind == KIND_SUMMARY_ENGR:
            state["summary_engr"][item_key] = value
    return state


def effective_state(report_db, session_id: str, manifest: dict) -> tuple[dict, int]:
    """(편집 상태, rev). rev==0(미이전 legacy 세션)이면 manifest 필드로 폴백.

    rev>0 이면 DB 가 유일한 진실이다 — 사용자가 전부 지운 상태도 그대로 반영되고
    manifest 값이 부활하지 않는다."""
    rev = report_db.get_webreport_edit_rev(session_id)
    if rev == 0:
        return state_from_manifest(manifest), 0
    return load_edit_state(report_db, session_id), rev


def _changes_from_state(state: dict) -> list:
    changes = []
    for row_key, cols in (state.get("issue_comments") or {}).items():
        for col, value in (cols or {}).items():
            if str(value or ""):
                changes.append((KIND_ISSUE_COMMENT, comment_key(str(row_key), str(col)),
                                str(value)))
    for item in state.get("etc_items") or []:
        if str(item or ""):
            changes.append((KIND_ETC_ITEM, str(item), ""))
    for item, spec in (state.get("trim_overrides") or {}).items():
        if isinstance(spec, dict) and spec:
            changes.append((KIND_TRIM_OVERRIDE, str(item),
                            json.dumps(spec, sort_keys=True, ensure_ascii=False)))
    for key, value in (state.get("summary_engr") or {}).items():
        if str(value or ""):
            changes.append((KIND_SUMMARY_ENGR, str(key), str(value)))
    return changes


def seed_from_manifest(report_db, session_id: str, manifest: dict,
                       updated_by=None) -> int:
    """manifest 편집값을 세션 편집행으로 복사 (신규 업로드 시드·마이그레이션 공용).

    복사한 행 수 반환. manifest 에 편집값이 없으면 no-op (rev 0 유지)."""
    changes = _changes_from_state(state_from_manifest(manifest))
    if changes:
        report_db.apply_webreport_edits(session_id, changes, updated_by=updated_by)
    return len(changes)


def ensure_seeded(report_db, session_id: str, manifest_loader) -> None:
    """rev==0 인 legacy 세션의 첫 편집 직전에 manifest 값을 DB 로 복사.

    manifest_loader 는 지연 호출 callable — 이미 이전된 세션(rev>0)은 manifest 를
    아예 로드하지 않는다. 동시 첫 편집은 upsert 로 멱등."""
    if report_db.get_webreport_edit_rev(session_id) == 0:
        seed_from_manifest(report_db, session_id, manifest_loader())

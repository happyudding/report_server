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
import logging
import re

_log = logging.getLogger(__name__)

KIND_ISSUE_COMMENT = "issue_comment"
KIND_ETC_ITEM = "etc_item"
KIND_TRIM_OVERRIDE = "trim_override"
KIND_SUMMARY_ENGR = "summary_engr"
# 2026-07-12 추가 — 차트 주석(도형/텍스트/코멘트, item_key=chart_key) / Note 탭 시트 JSON.
# 둘 다 manifest 에 존재한 적 없는 신규 kind 라 legacy 시드/폴백 대상이 아니다.
KIND_CHART_NOTE = "chart_note"
KIND_NOTE_SHEET = "note_sheet"
# 2026-07-16 추가 — Issue Table 행 숨김(item_key="Yield|<bin>"|"CPK|<item>", value="1") /
# 행 Status(item_key="Yield|<bin>"|"CPK|<item>"|"ETC|<item>", value="Close" 만 저장 —
# 부재=Open). 둘 다 manifest 에 존재한 적 없는 신규 kind 라 legacy 시드/폴백 대상이 아니다.
KIND_ISSUE_HIDDEN = "issue_hidden"
KIND_ISSUE_STATUS = "issue_status"
# 2026-07-22 추가 — 앵커/북마크 태그(item_key=태그명, value=JSON 위치 spec).
# IssueTable comment 의 #[태그명] 토큰이 이 태그를 가리켜 Note 특정 셀로 점프한다.
# manifest 에 존재한 적 없는 신규 kind 라 legacy 시드/폴백 대상이 아니다.
KIND_NOTE_TAG = "note_tag"
# 2026-07-23 추가 — 조회 전처리 옵션(item_key='spec', value=JSON: exclude_items/outlier).
# 원본 parquet 을 바꾸지 않고 조회 시점에만 적용되는 되돌릴 수 있는 편집 (preprocess.py).
KIND_PREPROCESS = "preprocess"
_PREPROCESS_KEY = "spec"
# 2026-07-23 추가 — 수율 분모 기준(item_key='basis', value='gross'|'test').
# 행이 없으면 기본 'gross'(제품 기준정보 Gross Die, 값이 없으면 rawdata 폴백).
# preprocess spec 에 넣지 않는 이유: preprocess digest 가 바뀌면 Distribution pack
# (정렬 전가) 경로가 폴백으로 떨어지는데, 수율 분모는 ECDF 와 아무 상관이 없다.
# 2026-07-28 확장 — 기준을 **소스별**로 고를 수 있게 됐다(Honey 허브 [Yield 계산] 탭).
#   item_key='basis'            : 전역 모드 'auto'(신규 기본) | 'test'(구 전역 스위치)
#   item_key='src\x1f<source>'  : 그 소스의 override 'gross' | 'test'
# 구 값 'gross' 는 auto 로 승격해 읽는다 — auto 는 "Gross Die 기준 + 100% 초과·대량 미측정
# 예외만 test 로 회피"라 구 기본값의 의도를 포함한다 (판정 규칙은 tabs/yield_tab.py).
KIND_YIELD_BASIS = "yield_basis"
_YIELD_BASIS_KEY = "basis"
YIELD_BASIS_GROSS = "gross"
YIELD_BASIS_TEST = "test"
YIELD_BASIS_AUTO = "auto"

# 표 payload 빌드에 안 쓰이는 kind — load_edit_state 조회에서 제외해 대용량 값
# (note_sheet 시트 JSON 최대 2MB)이 comment 저장·콜드 빌드마다 딸려오지 않게 한다.
# note_tag 는 /full extras 로 별도 조회(load_note_tags)라 표 상태에 싣지 않는다.
# preprocess 는 loader 가 별도 조회(load_preprocess)해 캐시 키에 쓰므로 표 상태 밖이다.
_STATE_EXCLUDED_KINDS = (KIND_CHART_NOTE, KIND_NOTE_SHEET, KIND_NOTE_TAG, KIND_PREPROCESS,
                         KIND_YIELD_BASIS)

# issue_comment 의 item_key = row_key + SEP + col (row_key 에 '|' 가 쓰여 제어문자 사용)
_SEP = "\x1f"
# yield_basis 소스별 행의 item_key 접두어 — source 이름에 무엇이 들어와도 전역 키('basis')와
# 섞이지 않게 같은 제어문자를 쓴다.
_YIELD_BASIS_SRC_PREFIX = "src" + _SEP

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
        # issue_hidden/issue_status 는 manifest 에 없는 신규 kind — 빈 기본값만 보장.
        "issue_hidden": [],
        "issue_status": {},
    }


def load_edit_state(report_db, session_id: str) -> dict:
    """DB 편집행 → manifest 필드와 동일한 형태의 상태 dict.

    etc_items 순서는 rowid(삽입) 순서 — get_webreport_edits 가 보장한다."""
    state = {"issue_comments": {}, "etc_items": [], "trim_overrides": {}, "summary_engr": {},
             "issue_hidden": [], "issue_status": {}}
    for row in report_db.get_webreport_edits(session_id,
                                             exclude_kinds=_STATE_EXCLUDED_KINDS):
        kind, item_key, value = row["kind"], row["item_key"], row["value"]
        if kind == KIND_ISSUE_COMMENT:
            row_key, _, col = item_key.partition(_SEP)
            if row_key and col:
                state["issue_comments"].setdefault(row_key, {})[col] = value
        elif kind == KIND_ETC_ITEM:
            state["etc_items"].append(item_key)
        elif kind == KIND_ISSUE_HIDDEN:
            state["issue_hidden"].append(item_key)
        elif kind == KIND_ISSUE_STATUS:
            state["issue_status"][item_key] = value
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


def load_chart_notes(report_db, session_id: str) -> dict:
    """chart_key → 주석 dict({shapes, texts, comment}). /full extras 조립용 —
    kind 지정 조회라 note_sheet 등 다른 대용량 값을 끌어오지 않는다."""
    out = {}
    for row in report_db.get_webreport_edits(session_id, kinds=(KIND_CHART_NOTE,)):
        try:
            spec = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(spec, dict):
            spec = dict(spec)
            spec["updated_by"] = row.get("updated_by") or ""
            spec["updated_at"] = row.get("updated_at") or ""
            out[row["item_key"]] = spec
    return out


def load_note_tags(report_db, session_id: str) -> dict:
    """태그명 → 위치 spec dict. /full extras 조립용 — kind 지정 조회라
    note_sheet 등 다른 대용량 값을 끌어오지 않는다. (load_chart_notes 와 동형.)"""
    out = {}
    for row in report_db.get_webreport_edits(session_id, kinds=(KIND_NOTE_TAG,)):
        try:
            spec = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(spec, dict):
            spec = dict(spec)
            spec["updated_by"] = row.get("updated_by") or ""
            spec["updated_at"] = row.get("updated_at") or ""
            out[row["item_key"]] = spec
    return out


def load_preprocess(report_db, session_id: str) -> dict:
    """조회 전처리 spec (없으면 빈 dict) — preprocess.normalize 를 통과한 정규형.

    kind 지정 조회(작은 인덱스 SELECT 1회)라 note_sheet 등 대용량 값을 끌어오지 않는다.
    반환 빈 dict = 전처리 없음 = 캐시 키·코드 경로가 종전과 완전히 동일."""
    from .preprocess import normalize

    for row in report_db.get_webreport_edits(session_id, kinds=(KIND_PREPROCESS,)):
        if row["item_key"] != _PREPROCESS_KEY:
            continue
        try:
            spec = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return {}
        return normalize(spec)
    return {}


def save_preprocess(report_db, session_id: str, spec: dict, updated_by=None) -> int:
    """전처리 spec 저장 (정규화 후 빈 dict 면 행 삭제 = 해제). 새 rev 반환.

    rev 증가로 REPORT/TRIM//full 캐시가, digest 변화로 tables/dist/map/scatter 캐시가
    각각 무효화된다 (cache_policy 참조)."""
    from .preprocess import normalize

    norm = normalize(spec)
    value = json.dumps(norm, sort_keys=True, ensure_ascii=False) if norm else None
    return report_db.apply_webreport_edits(
        session_id, [(KIND_PREPROCESS, _PREPROCESS_KEY, value)], updated_by=updated_by)


def drop_preprocess_edits(report_db, session_ids, updated_by=None) -> int:
    """원본 parquet 이 교체된 뒤 **행 위치 기반 셀 패치(edits)만** 해제한다. 해제 건수 반환.

    Excel 왕복·웹 셀 편집은 행을 지우거나 순서를 바꿀 수 있어 ``(source, row_idx)`` 가
    가리키는 die 가 달라진다 — 그대로 두면 패치가 엉뚱한 행에 걸린다. 조건 기반인 rules
    와 이름 기반인 exclude_items/outlier 는 원본이 바뀌어도 의미가 유지되므로 남긴다.

    dedup 형제 세션도 같은 물리 원본을 가리키므로 함께 해제한다(session_ids 로 받는다).
    """
    from .preprocess import normalize

    dropped = 0
    for session_id in session_ids or ():
        spec = load_preprocess(report_db, session_id)
        if not spec.get("edits"):
            continue
        dropped += len(spec["edits"])
        rest = normalize({k: v for k, v in spec.items() if k != "edits"})
        save_preprocess(report_db, session_id, rest, updated_by=updated_by)
    return dropped


def drop_preprocess_edits_for_akey(report_db, analysis_key: str, user_agent: str = "") -> int:
    """analysis_key 를 공유하는 전 세션의 셀 패치 해제 (원본 교체 직후 호출). 실패는 무해.

    Excel 왕복(rawedit.replace_sources)과 웹 셀 편집(service.edit_raw_data)이 같이 쓴다 —
    "원본이 바뀌면 행 위치 패치는 못 믿는다"는 판단이 두 경로에서 갈리면 안 된다.
    해제 실패로 원본 교체 자체를 되돌리지는 않는다(교체는 이미 성공했다).
    """
    try:
        return drop_preprocess_edits(
            report_db, report_db.session_ids_for_analysis_key(analysis_key),
            updated_by=user_from_ua(user_agent) or None)
    except Exception:
        _log.warning("전처리 셀 패치 해제 실패 akey=%s", analysis_key, exc_info=True)
        return 0


def normalize_yield_basis(value) -> str:
    """'test' 만 test, 그 외(빈 값 포함)는 기본 'gross'."""
    return YIELD_BASIS_TEST if str(value or "").strip().lower() == YIELD_BASIS_TEST \
        else YIELD_BASIS_GROSS


def load_yield_basis(report_db, session_id: str) -> str:
    """저장된 **전역 모드 행**의 구 형식 값 ('gross'|'test') — 하위호환 조회용.

    소스별 기준까지 보려면 load_yield_basis_map 을 쓴다(조회 경로는 그쪽만 쓴다)."""
    for row in report_db.get_webreport_edits(session_id, kinds=(KIND_YIELD_BASIS,)):
        if row["item_key"] == _YIELD_BASIS_KEY:
            return normalize_yield_basis(row["value"])
    return YIELD_BASIS_GROSS


def normalize_yield_basis_map(value) -> dict:
    """사용자 입력 → {"mode": 'auto'|'test', "sources": {name: 'gross'|'test'}}.

    문자열('gross'/'test')만 오는 구 클라 요청도 받는다 — 'test' 는 전역 test, 그 외는 auto.
    """
    if not isinstance(value, dict):
        return {"mode": normalize_yield_basis_mode(value), "sources": {}}
    sources = {}
    for name, basis in (value.get("sources") or {}).items():
        basis = str(basis or "").strip().lower()
        if str(name) and basis in (YIELD_BASIS_GROSS, YIELD_BASIS_TEST):
            sources[str(name)] = basis
    return {"mode": normalize_yield_basis_mode(value.get("mode")), "sources": sources}


def normalize_yield_basis_mode(value) -> str:
    """전역 모드 — 'test' 만 test, 그 외(구 'gross'·빈 값 포함)는 'auto'."""
    return YIELD_BASIS_TEST if str(value or "").strip().lower() == YIELD_BASIS_TEST \
        else YIELD_BASIS_AUTO


def load_yield_basis_map(report_db, session_id: str) -> dict:
    """수율 분모 기준 — {"mode": 'auto'|'test', "sources": {source: 'gross'|'test'}}.

    행이 없으면 전 소스 auto(= Gross Die 기준 + 예외 자동 회피). kind 지정 조회 1회."""
    mode, sources = YIELD_BASIS_AUTO, {}
    for row in report_db.get_webreport_edits(session_id, kinds=(KIND_YIELD_BASIS,)):
        key = str(row["item_key"] or "")
        value = str(row["value"] or "").strip().lower()
        if key == _YIELD_BASIS_KEY:
            mode = normalize_yield_basis_mode(value)
        elif key.startswith(_YIELD_BASIS_SRC_PREFIX):
            name = key[len(_YIELD_BASIS_SRC_PREFIX):]
            if name and value in (YIELD_BASIS_GROSS, YIELD_BASIS_TEST):
                sources[name] = value
    return {"mode": mode, "sources": sources}


def save_yield_basis_map(report_db, session_id: str, basis_map, updated_by=None) -> int:
    """소스별 수율 분모 기준 저장 — 새 map 에 없는 소스 행은 삭제한다. 새 rev 반환.

    rev 증가로 REPORT//full 캐시가 무효화된다(preprocess digest 는 건드리지 않는다)."""
    norm = normalize_yield_basis_map(basis_map)
    changes = [(KIND_YIELD_BASIS, _YIELD_BASIS_KEY, norm["mode"])]
    changes += [(KIND_YIELD_BASIS, _YIELD_BASIS_SRC_PREFIX + name, basis)
                for name, basis in sorted(norm["sources"].items())]
    for row in report_db.get_webreport_edits(session_id, kinds=(KIND_YIELD_BASIS,)):
        key = str(row["item_key"] or "")
        if (key.startswith(_YIELD_BASIS_SRC_PREFIX)
                and key[len(_YIELD_BASIS_SRC_PREFIX):] not in norm["sources"]):
            changes.append((KIND_YIELD_BASIS, key, None))
    return report_db.apply_webreport_edits(session_id, changes, updated_by=updated_by)


def load_note_sheet(report_db, session_id: str) -> dict | None:
    """Note 탭 시트 JSON(단일 행, item_key='sheet') + 메타. 없으면 None.

    시트 본문은 최대 2MB — /full 에는 싣지 않고 lazy GET 라우트만 이 함수를 쓴다."""
    rows = report_db.get_webreport_edits(session_id, kinds=(KIND_NOTE_SHEET,))
    for row in rows:
        if row["item_key"] != "sheet":
            continue
        try:
            sheet = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(sheet, dict):
            return None
        return {"sheet": sheet, "updated_by": row.get("updated_by") or "",
                "updated_at": row.get("updated_at") or "",
                "base": report_db.note_base_token(row["value"])}
    return None


def _changes_from_state(state: dict) -> list:
    # issue_hidden/issue_status 는 manifest 에 존재한 적 없는 kind — 시드 대상 아님.
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

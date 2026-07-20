# [Opus 구현 프롬프트 P2] IssueTable 라벨 입력 (E-1) — "사람의 정답"을 label 완전체로 export

너는 `f:\COINAPI\report_server` 저장소에서 작업하는 Claude Code 다. 아래 지시를 그대로 수행하라.

## 목표 (한 줄)

IssueTable 각 이슈 행에 **선택 입력** 라벨 4종(판정 human_status / 원인 root_cause_category /
조치 outcome_action / 결과 outcome_result)을 추가하고, 저장 시 세션 편집 DB 에 기록 →
eval_export 가 eval DB 의 `label`(human_status/root_cause 포함) + `case_outcome` 으로
내보내게 한다. 현재 export 는 human_comment 만 채우고 나머지는 전부 None 이다 — 그 빈칸을 채운다.

## 먼저 읽어라 (필수)

1. `web_report/edits.py` — kind 상수·`_SEP`·`comment_key`·`load_edit_state` 패턴
   (특히 KIND_ISSUE_STATUS 처럼 "manifest 에 없던 신규 kind = legacy 시드/폴백 비대상" 주석)
2. `web_report/service.py` 의 `update_issue_comments`(약 571행~) — 검증·ensure_seeded·diff·
   audit·export_async 트리거 패턴. 새 함수는 이걸 거울처럼 따른다
3. `server/report/routes_webreport.py` 의 `web_report_issue_table_comments`(약 499행~) — 라우트 패턴
4. `web_report/eval_export.py` **전체** — `_parse_row_key`/`_collect_comments`/
   `export_session_comments`/reconciliation. 이번 확장의 중심 파일
5. `web_report/tabs/issue_table.py` — row_key 규약(`Yield|<bin>|<item>`, `CPK|<item>`, `ETC|<item>`)
6. `web_report/cache_policy.py` — `REPORT_SCHEMA_VERSION` (payload 구조 변경 시 필수 bump)
7. `server/report/static/webreport/` 에서 issue 코멘트 편집·autoSave 를 담당하는 JS 모듈
   (grep: `ISSUE_COMMENT_COLS`, `issue_table/comments`, `autoSave`) — UI 는 이 패턴 재사용
8. `tests/test_eval_export.py` — 기존 export 테스트 (확장할 것)

## 불변 제약

- **`eval_analyzer/` 무수정.** eval_engine 호출은 `eval_export.py` 내부에서만
  (`store.insert_label`/`store.insert_case_outcome` — 이미 import 허용 지점).
- 라벨 값은 **선택 입력** — 빈 값 허용, 강제 금지. 빈 값 저장 = 해당 필드 삭제.
- manifest 재저장 금지 — 진실은 `report_webreport_edit`.
- **payload 에 새 top-level 키를 추가하므로 `cache_policy.REPORT_SCHEMA_VERSION` 을 반드시 +1.**
- 새 편집 채널은 프런트 autoSave Promise.all(keepalive) 에 합류.
- AI Comment 컬럼(`AI_COMMENT_COL`)과 `COMMENT_COLS` 는 건드리지 않는다.

## 통제 어휘 (서버 검증용 상수 — 정본 미러)

```python
# 정본: eval_analyzer docs/DB_SCHEMA.md §10 + rules/outcome_taxonomy.yaml.
# outcome 은 export 시 store.insert_case_outcome 이 정본(yaml)으로 재검증하므로
# 이 상수는 UI 선행 검증용 미러다 — 정본 변경 시 함께 갱신 (드리프트는 export 에러
# 감사로그(action=eval_export, result=error)로 드러난다).
LABEL_VOCAB = {
    "human_status": ["CRITICAL", "MAJOR", "MINOR", "MONITOR", "OK"],
    "root_cause_category": ["equipment", "process", "design", "spec", "unknown"],
    "outcome_action": ["retest", "condition_change", "trim_adjust", "spec_release",
                       "dev_feedback", "pa_feedback", "false_fail", "scrap",
                       "monitor", "other"],
    "outcome_result": ["recovered_normal", "improved", "false_fail",
                       "confirmed_defective", "inconclusive", "pending", "other"],
}
# UI 한글 라벨(드롭다운 표시용): action= 재측정/조건변경/트림조정/스펙릴리즈/설계피드백/
# 공정피드백/정상판정/폐기/모니터링/기타, result= 정상/개선/실불량아님/진성불량/
# 원인불명종결/보류/기타 (rules/outcome_taxonomy.yaml 의 ko 값)
```

## 구현 단계

### Step 1 — `web_report/edits.py`: 신규 kind

```python
# 2026-07-XX 추가 — Issue Table 라벨(판정/원인/조치/결과).
# item_key = row_key + _SEP + field, value = 어휘 코드. row_key 는 comment 와 동일하게
# 행 단위("Yield|<bin>|<item>" 등) — export 가 case(=bin,item) 1:1 로 매핑하기 위함
# (issue_status 의 이슈 단위 키와 다름에 주의). manifest 에 존재한 적 없는 신규 kind 라
# legacy 시드/폴백 대상이 아니다.
KIND_ISSUE_LABEL = "issue_label"
LABEL_FIELDS = ("human_status", "root_cause_category", "outcome_action", "outcome_result")
```

`load_edit_state` 의 상태 dict 에 `"issue_labels": {}` 기본값 추가 +
```python
elif kind == KIND_ISSUE_LABEL:
    row_key, _, field = item_key.partition(_SEP)
    if row_key and field in LABEL_FIELDS:
        state["issue_labels"].setdefault(row_key, {})[field] = value
```
`state_from_manifest` 에도 `"issue_labels": {}` 만 추가(시드 대상 아님 — `_changes_from_state` 는 무수정).

### Step 2 — `web_report/service.py`: `update_issue_labels`

`update_issue_comments` 를 거울로 새 함수. 차이점만:

```python
def update_issue_labels(session_id: str, labels: list, *, report_db, upload_root: Path,
                        client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table 라벨(판정/원인/조치/결과) 저장 — 세션 편집 DB(kind=issue_label).

    labels: [{"key": row_key, "field": LABEL_FIELDS 중 하나, "value": 코드|""}].
    빈 value 는 삭제. 어휘는 LABEL_VOCAB 로 검증(정본 미러 — 모듈 상단 주석 참조).
    """
```
- 검증: key 형식(comments 와 동일 길이 제한), `field in edits.LABEL_FIELDS`,
  `value == "" or value in LABEL_VOCAB[field]` (아니면 ValueError).
- 변경분만 `(edits.KIND_ISSUE_LABEL, edits.comment_key(key, field), value or None)` 로
  `apply_webreport_edits`. (comment_key 는 row_key+SEP+col 조합 함수 — field 를 col 자리에 재사용)
- audit `changed_fields=f"issue_labels({changed} cells)"`, 변경 시 `eval_export.export_async` 트리거
  — comments 쪽 코드와 동일 try/except 격리.

### Step 3 — 라우트: `server/report/routes_webreport.py`

`web_report_issue_table_comments` 바로 아래에 거울 라우트:
`POST /session/<session_id>/web_report/issue_table/labels`
(_require_csrf → _require_web_report_session → _editor_guard → body["labels"] 리스트 검증 →
service.update_issue_labels → 동일한 예외 매핑 404/400/500).

### Step 4 — payload 에 현재 라벨 노출 + 스키마 버전

- service 의 리포트 빌드 경로에서 `edits` 상태의 `issue_labels` 를 payload top-level
  `payload["issue_labels"]`(dict row_key→{field:code}, 항상 존재·기본 `{}`) 로 넣는다.
  statuses/hidden 이 build 에 전달되는 지점을 찾아 같은 흐름에 태워라 (편집 rev 가 캐시 키에
  이미 포함되므로 라벨 편집 → rev 증가 → 캐시 자동 무효화로 일관성이 맞는다).
- **`cache_policy.REPORT_SCHEMA_VERSION` +1** (memory 규칙: 안 올리면 disk cache 가 옛
  payload 를 재사용해 조용히 회귀한다).

### Step 5 — 프런트 (static/webreport/)

- 각 이슈 데이터 행(comment 셀이 있는 행)에 작은 "라벨" 버튼 → 팝오버에 드롭다운 4개
  (한글 라벨 표시, 값은 코드) + 비우기 옵션. 현재값은 `payload.issue_labels[row_key]` 로 채운다.
- 저장: 변경분을 모아 `POST .../issue_table/labels` — **기존 comment 저장/autoSave 구조를
  그대로 재사용**하고, autoSave 의 Promise.all(keepalive) 채널 목록에 labels 채널을 추가한다
  (미저장 변경이 페이지 이탈 시 유실되지 않도록 — 기존 3채널 패턴 확인 후 합류).
- CSS/마크업은 기존 IssueTable 셀 스타일 관례를 따르고, 팝오버는 최소 구현 (프레임워크 추가 금지).

### Step 6 — `web_report/eval_export.py`: export 확장 (이 프롬프트의 핵심)

**(a) 라벨 수집** — `_collect_comments` 아래에 추가:

```python
def _collect_labels(report_db, session) -> dict:
    """세션 issue_label 상태 → {row_key: {field: code}}. 신규 kind 라 manifest 폴백 없음."""
    per_key: dict[str, dict] = {}
    for row in report_db.get_webreport_edits(session["session_id"],
                                             kinds=(edits.KIND_ISSUE_LABEL,)):
        row_key, _, field = str(row["item_key"]).partition(edits._SEP)
        value = str(row["value"] or "").strip()
        if row_key and field in edits.LABEL_FIELDS and value:
            per_key.setdefault(row_key, {})[field] = value
    return per_key
```

**(b) export 대상 확대** — `export_session_comments` 에서 parsed 를 만들 때, 코멘트가 있는
row_key 와 라벨이 있는 row_key 의 **합집합**을 case 로 적재한다 (라벨만 있고 코멘트가 없는
행도 정답지로서 가치가 있다). parsed 원소를 `(bin, item, text, by, label_dict)` 5-튜플로 확장:

```python
comments = _collect_comments(report_db, session, upload_root)
labels = _collect_labels(report_db, session)
parsed = []
for row_key in set(comments) | set(labels):
    pk = _parse_row_key(row_key)
    if pk is None:
        continue
    ent = comments.get(row_key) or {}
    text = _merge_comment(ent.get("cols") or {})
    lab = labels.get(row_key) or {}
    if text or lab:
        parsed.append((pk[0], pk[1], text, ent.get("by"), lab))
```

**(c) label/outcome 기록** — 기존 `DELETE label` + `insert_label` 지점을 아래로 교체.
**삭제 순서가 중요**하다: outcome 은 label_id 를 참조하므로 label 을 지우기 **전에**
우리 label 에 연결된 outcome 부터 지운다:

```python
# 우리(labeler=web_report) label 에 연결된 outcome → label 순으로 정리 후 재삽입 (멱등)
conn.execute("""DELETE FROM case_outcome WHERE case_id=? AND label_id IN
                (SELECT label_id FROM label WHERE case_id=? AND labeler=?)""",
             (case_id, case_id, _LABELER))
conn.execute("DELETE FROM label WHERE case_id=? AND labeler=?", (case_id, _LABELER))
label_id = store.insert_label(
    case_id, None,
    lab.get("human_status"), lab.get("root_cause_category"), None,
    0, 0,                       # engine_comment_accepted/comment_modified — P3 에서 채움
    text or None, _LABELER, by or None, "manual", conn=conn)
action = lab.get("outcome_action")
result = lab.get("outcome_result")
if action or result:
    try:  # 정본(yaml) 재검증 — 어휘 드리프트 시 이 case 만 outcome 생략, label 은 유지
        store.insert_case_outcome(case_id, label_id, action, None, result,
                                  by or None, None, None, conn=conn)
    except ValueError:
        logger.warning("eval_export: outcome 어휘 불일치 case=%s action=%r result=%r — 생략",
                       case_id, action, result)
```

**(d) reconciliation** — 제거 루프에도 같은 순서의 outcome 삭제를 추가
(기존 `DELETE FROM label ...` 앞에 위 case_outcome 삭제 SQL 을 동일하게).

**(e) 통계 best-effort** — 라벨만 있는 행도 기존 `_dist_metrics/_yield_metrics` 경로를 그대로 탄다
(코드 변경 불필요 — parsed 루프 공통).

### Step 7 — 테스트: `tests/test_eval_export.py` 확장

기존 테스트 픽스처 패턴을 따라 최소 3케이스:
1. 코멘트+라벨 4종 저장 → export → eval DB 에서
   `label.human_status/root_cause_category` 채워짐 + `case_outcome` 1행(action/result) 확인.
2. 라벨만(코멘트 없음) → case + label(human_comment=None, human_status 만) 적재 확인.
3. 라벨 삭제 후 재-export → 해당 case 의 우리 label 이 status 없이 갱신되고 outcome 이
   사라짐(멱등·reconciliation) 확인. 잘못된 어휘를 편집 DB 에 강제로 심은 뒤 export 시
   outcome 만 생략되고 label 은 남는 것 확인.

service 검증 단위 테스트(허용 어휘 밖 ValueError, 빈 값 삭제)도 추가하라
(기존 service 테스트 파일이 있으면 그 옆, 없으면 test_eval_export.py 에 함께).

## 검증

1. `python -m pytest tests/ -q` (기존 포함 전부 통과 — 특히 test_eval_export.py).
2. 서버 기동 → web_report 세션에서 라벨 저장 → 아래로 실제 확인:
   ```
   python -c "import sqlite3;c=sqlite3.connect(r'DB/pe/report/eval/eval.db');c.row_factory=sqlite3.Row;[print(dict(r)) for r in c.execute('select human_status,root_cause_category,human_comment,labeler from label order by label_id desc limit 5')]"
   ```
3. payload 회귀: 라벨 미사용 기존 세션의 /full 이 정상 렌더 + `issue_labels: {}` 존재,
   REPORT_SCHEMA_VERSION bump 로 캐시 재빌드되는지 확인 (서버 재시작 포함).
4. 프런트: 라벨 입력 → 새로고침 후 값 유지, 미저장 상태로 탭 닫기 직전 autoSave 채널에
   포함되는지 (기존 채널과 동일 동작) 확인.
5. `git status` — 변경이 web_report/·server/report/·tests/·static 범위인지 확인
   (**eval_analyzer/ 무변경 증명**).

## 완료 기준

- [ ] eval_analyzer/ 무수정
- [ ] label.human_status/root_cause + case_outcome 이 실제 eval DB 에 기록됨 (쿼리 로그 제시)
- [ ] 멱등: 같은 상태 재-export 시 행 수 불변, 라벨 삭제 반영
- [ ] REPORT_SCHEMA_VERSION bump 포함
- [ ] pytest 전부 통과 + 완료 보고 (변경 파일 / 검증 로그 / 후속: P3 이 0,0 자리를 채움)

## 하지 말 것

- 라벨을 필수 입력으로 강제, COMMENT_COLS/AI_COMMENT_COL 변경, 이슈 단위 키
  (`Yield|<bin>`) 로 라벨 저장(행 단위 row_key 를 쓸 것), manifest 수정,
  outcome_condition 자유 텍스트 입력(스코프 밖 — 후속).

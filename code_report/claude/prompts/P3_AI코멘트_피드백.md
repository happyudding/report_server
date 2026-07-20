# [Opus 구현 프롬프트 P3] AI Comment 채택/수정 피드백 기록 (E-2)

너는 `f:\COINAPI\report_server` 저장소에서 작업하는 Claude Code 다. 아래 지시를 그대로 수행하라.

## 목표 (한 줄)

IssueTable 의 읽기전용 "AI Comment" 셀에 👍/👎 버튼을 붙여 사용자의 채택/거부 신호를
세션 편집 DB 에 기록하고, eval_export 가 label 의 `engine_comment_accepted` /
`comment_modified` 컬럼(스키마에 이미 존재, 현재 항상 0)을 채우게 한다.
→ "AI Comment 채택률" 이라는 단일 KPI 가 측정 가능해진다.

## 선행 조건

**P2(라벨 입력 UI)가 이미 반영된 작업트리인지 먼저 확인**하라
(`web_report/edits.py` 에 `KIND_ISSUE_LABEL` 존재 여부). P2 가 반영되어 있으면 export 확장은
P2 가 만든 5-튜플 구조 위에 얹고, 반영 전이면 P2 와 충돌하지 않도록 이 프롬프트 범위를
독립 함수로 유지하라 (아래 코드는 P2 반영 후 기준으로 작성됨).

## 먼저 읽어라 (필수)

1. `web_report/ai_comment.py` — `_cell_text`(셀 = `[STATUS] comment`), AI Comment 는 콜드
   빌드 캐시 산물이라 시점에 따라 재계산될 수 있음 (그래서 **피드백 시점의 AI 원문을 함께 저장**)
2. `web_report/tabs/issue_table.py` — `AI_COMMENT_COL`, row_key 규약. **읽기전용 보장 원칙**:
   AI Comment 는 `COMMENT_COLS` 에 절대 추가하지 않는다 — 피드백은 별도 채널이다
3. `web_report/service.py` 의 `update_issue_status`(단건 key/value 저장 패턴, 약 520행대) —
   피드백 저장 함수의 거울
4. `web_report/edits.py`, `server/report/routes_webreport.py`(comments/status 라우트),
   `web_report/eval_export.py`(P2 확장본)
5. 프런트: `server/report/static/webreport/` 에서 AI Comment 셀 렌더와 status 토글 POST 를
   하는 JS (grep: `AI Comment`, `issue_table/status`)

## 불변 제약

- **`eval_analyzer/` 무수정.** AI Comment 컬럼 자체(값 생성·읽기전용)는 변경하지 않는다.
- 피드백은 선택 — 없으면 export 는 기존처럼 0/0 이 아니라 **None/None** 으로 기록해
  "피드백 없음"과 "거부(0)"를 구분한다 (`insert_label` 은 None 을 그대로 저장한다).
- payload 구조 변경 시 `cache_policy.REPORT_SCHEMA_VERSION` +1 (P2 에서 이미 올렸다면,
  이번 변경이 payload 를 또 바꾸는 경우에만 다시 +1).

## 구현 단계

### Step 1 — `web_report/edits.py`: 신규 kind

```python
# 2026-07-XX 추가 — AI Comment 피드백. item_key = row_key(행 단위, comment 와 동일),
# value = JSON {"verdict":"up"|"down","ai_text":"피드백 당시 AI 셀 원문"}.
# ai_text 를 함께 저장하는 이유: AI Comment 는 콜드 빌드 캐시 산물이라 이후 재계산으로
# 바뀔 수 있다 — "무엇에 대한 채택이었나"를 고정하려면 시점 원문이 필요하다.
# manifest 에 존재한 적 없는 신규 kind — legacy 시드/폴백 비대상.
KIND_AI_FEEDBACK = "ai_feedback"
```

`load_edit_state` 에 `"ai_feedback": {}` 기본값 +
```python
elif kind == KIND_AI_FEEDBACK:
    try:
        spec = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        continue
    if isinstance(spec, dict) and spec.get("verdict") in ("up", "down"):
        state["ai_feedback"][item_key] = spec
```
(`state_from_manifest` 에도 빈 기본값만 추가.)

### Step 2 — `web_report/service.py`: `update_ai_feedback`

`update_issue_status` 를 거울로 한 단건 저장 함수:

```python
def update_ai_feedback(session_id: str, *, report_db, upload_root: Path, key: str,
                       verdict: str, ai_text: str = "",
                       client_ip: str = "", user_agent: str = "") -> dict:
    """AI Comment 채택(up)/거부(down)/해제("") 저장 — kind=ai_feedback.

    key 는 IssueTable row_key. verdict "" 는 피드백 삭제(미평가 상태 복귀).
    ai_text 는 클릭 시점의 AI 셀 원문(최대 2000자로 절단) — 채택률 분석용 스냅샷.
    """
```
- 검증: key 형식(comments 와 동일), `verdict in ("up", "down", "")`.
- 저장 value: `json.dumps({"verdict": verdict, "ai_text": ai_text[:2000]},
  ensure_ascii=False)`, verdict=="" 이면 `(kind, key, None)` 으로 삭제.
- audit(`changed_fields=f"ai_feedback({key!r}={verdict})"`) + `eval_export.export_async`
  트리거 (comments 저장과 동일 try/except 격리).

### Step 3 — 라우트

`POST /session/<session_id>/web_report/issue_table/ai_feedback`
(status 라우트 패턴 그대로: CSRF → 세션 확인 → `_editor_guard` → body
`{"key":..,"verdict":"up"|"down"|"","ai_text":".."}` → service 호출 → 예외 매핑).

### Step 4 — payload + 프런트

- payload: `issue_labels` 와 같은 채널로 `payload["ai_feedback"]`(row_key→{"verdict":..})
  추가 — ai_text 는 payload 에 싣지 않는다(용량·불필요). 스키마 버전 규칙 준수.
- 프런트: AI Comment 셀 우측에 👍/👎 미니 버튼(값 있을 때만 표시). 클릭 → 즉시 POST
  (status 토글과 동일한 즉시 저장 — autoSave 대기 채널 아님. 단, 실패 시 시각적 롤백).
  현재 선택 상태는 버튼 강조로 표시, 같은 버튼 재클릭 = 해제("").
  `ai_text` 는 클릭 시점 셀 텍스트를 그대로 전송.

### Step 5 — `web_report/eval_export.py`: export 반영

**(a) 수집**:
```python
def _collect_ai_feedback(report_db, session) -> dict:
    """{row_key: {"verdict": "up"|"down", "ai_text": str}} — 신규 kind, manifest 폴백 없음."""
    import json as _json
    out = {}
    for row in report_db.get_webreport_edits(session["session_id"],
                                             kinds=(edits.KIND_AI_FEEDBACK,)):
        try:
            spec = _json.loads(row["value"])
        except (_json.JSONDecodeError, TypeError):
            continue
        if isinstance(spec, dict) and spec.get("verdict") in ("up", "down"):
            out[str(row["item_key"])] = spec
    return out
```

**(b) parsed 확장** — P2 의 합집합에 feedback 키도 포함하고 원소에 fb 를 추가:
`(bin, item, text, by, lab, fb)`. fb 만 있고 코멘트/라벨이 없는 행도 export 대상이다
(채택 신호 단독으로도 가치 있음).

**(c) insert_label 호출부** — P2 가 0,0 으로 두던 두 자리를 채운다:

```python
fb = fb or {}
verdict = fb.get("verdict")
accepted = None if verdict is None else (1 if verdict == "up" else 0)
# comment_modified: 사람이 자기 코멘트를 남겼고 그것이 AI 원문과 다른 경우 1.
# AI 원문(fb["ai_text"])이 없으면 판단 불가 → None (0 으로 단정하지 않는다).
if text and fb.get("ai_text"):
    modified = 0 if text.strip() == str(fb["ai_text"]).strip() else 1
elif text and verdict is not None:
    modified = 1          # 피드백은 남겼는데 원문 스냅샷이 없고 사람 코멘트가 있음
else:
    modified = None
label_id = store.insert_label(
    case_id, None,
    lab.get("human_status"), lab.get("root_cause_category"), None,
    accepted, modified,
    text or None, _LABELER, by or None, "manual", conn=conn)
```

### Step 6 — 테스트 (`tests/test_eval_export.py` 확장)

1. 👍 저장 → export → `label.engine_comment_accepted == 1`.
2. 👎 + 사람 코멘트(AI 원문과 다름) → `accepted == 0, comment_modified == 1`.
3. 피드백 없음 → 둘 다 **None** (0 아님 — P2 회귀 주의: P2 테스트의 0,0 기대값을 None 으로 갱신).
4. 해제("") 후 재-export → None 복귀 (멱등).

## 검증

1. `python -m pytest tests/ -q` 통과 (P2 테스트 기대값 갱신 포함).
2. 서버 기동 → ai_comment 옵션 세션에서 👍 클릭 → eval DB label 확인:
   ```
   python -c "import sqlite3;c=sqlite3.connect(r'DB/pe/report/eval/eval.db');c.row_factory=sqlite3.Row;[print(dict(r)) for r in c.execute('select case_id,engine_comment_accepted,comment_modified from label order by label_id desc limit 5')]"
   ```
3. 같은 버튼 재클릭(해제) → 재조회 시 None 확인. 일반 브라우저(신원 없음)에선 버튼이
   동작하지 않아야 함(_editor_guard — 403/400 확인).
4. `git status` — eval_analyzer/ 무변경.

## 완료 기준

- [ ] accepted/modified 가 up/down/무피드백 3상태(1/0/None)로 정확히 기록 (쿼리 로그 제시)
- [ ] AI Comment 셀은 여전히 편집 불가(읽기전용 회귀 없음)
- [ ] pytest 통과, eval_analyzer/ 무수정
- [ ] 완료 보고: 변경 파일 / 검증 로그 / KPI 산출 쿼리 1줄
      (예: `SELECT AVG(engine_comment_accepted) FROM label WHERE engine_comment_accepted IS NOT NULL`)

## 하지 말 것

- AI Comment 셀을 편집 가능하게 만들기, verdict 를 3값 이상으로 확장(스코프 밖),
  ai_text 를 payload 에 싣기, 피드백을 라벨(P2)과 같은 kind 에 섞어 저장하기.

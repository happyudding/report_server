# [Opus 구현 프롬프트 P4] features/evaluation 축적 (E-3) — calibrate 재료 만들기

너는 `f:\COINAPI\report_server` 저장소에서 작업하는 Claude Code 다. 아래 지시를 그대로 수행하라.

## ⚠ 착수 게이트 (사람 확인 필요)

이 작업은 docs/13 §4 의 "서버는 persist=False 로만 호출" 규약을 **개정**한다.
사용자에게 "외부 담당자와 규약 개정(서버 소유 DB 대상 persist=True 경로 추가) 합의가
되었는지" 를 먼저 확인하고, 합의 전이면 코드 구현 없이 중단·보고하라.
(합의 대상 요지: eval_analyzer 소유 eval.db 는 계속 무기록. 추가되는 것은 **report_server
소유 파일(REPORT_EVAL_DB_PATH)** 로의 persist 뿐 — 소유권 원칙은 불변.)

## 목표 (한 줄)

세션 업로드 시점에 `evaluate(persist=True)` 를 **서버 소유 eval DB(REPORT_EVAL_DB_PATH)** 로
1회 실행해 `features`(판정지표 22종)·`evaluation`·`eval_precedent` 를 축적한다 — 임계값
재보정 도구 calibrate(item_class 당 features n≥30 필요)의 재료가 이날부터 쌓이기 시작한다.
기본은 **꺼짐**(env `REPORT_EVAL_PERSIST_FEATURES=1` 일 때만 동작).

## 먼저 읽어라 (필수)

1. `eval_analyzer/eval_engine/api.py` — `evaluate(..., persist)` 흐름: persist=True 면
   `store.init_db()` + L0 마스터 upsert + L6 `present.persist`(features/evaluation 저장)
2. `eval_analyzer/eval_engine/config.py` — `DB_PATH = env EVAL_DB_PATH` 가 **import 시점**에
   고정됨. `store.get_conn()` 이 이 값을 쓴다
3. `eval_analyzer/db_input/import_csv.py` 의 `_import_group`(103행~) — `config.DATA_DIR/DB_PATH`
   런타임 덮어쓰기가 **eval_analyzer 스스로 쓰는 공인 패턴**임을 확인 (같은 패턴을 쓴다)
4. `eval_analyzer/eval_engine/pipeline/ingest.py` — L0 이 `create_ingest_run` 에 넘기는 meta 키
   확인 (아래 Step 3 의 중복 실행 방지 설계에 필요)
5. `web_report/ai_comment.py` — `_session_meta`/`_table_to_raw_df` (재사용), persist=False
   규약 주석 (그 경로는 **그대로 둔다**)
6. `web_report/eval_export.py` — `open_conn`/`db_path`/트리거 구조. 이번 코드는 이 파일에 추가
7. `web_report/ingest.py` 169행 부근 — export_async 훅 위치 (새 훅도 여기)
8. `docs/13_eval_analyzer_integration.md` §4·§9, `server/README.md` env 표 — 함께 개정

## 불변 제약

- **`eval_analyzer/` 무수정.** eval_engine import 는 `eval_export.py`(+기존 ai_comment.py) 안에서만.
- **`ai_comment.py` 의 `persist=False` 는 절대 바꾸지 않는다** (컴퓨트 워커 동시성·미리보기 규약 유지).
- **eval_analyzer 소유 기본 DB(`eval_analyzer/data/eval.db`)에 절대 쓰지 않는다.**
  persist 실행 직전에 engine config 를 서버 소유 경로로 **강제 재지정**하는 가드가 필수다
  (env 를 깜빡해도 외부 소유 파일이 오염되지 않도록) — Step 2 코드 참조.
- 기본 off. env 게이트 이름: `REPORT_EVAL_PERSIST_FEATURES` (server/config.py 의 기존 env
  파싱 관례를 따라 추가).
- 실패는 업로드를 절대 죽이지 않는다 (safe_export 격리 관례 + 감사로그 action=`eval_features`).

## 구현 단계

### Step 1 — `server/config.py`: env 추가

기존 스타일에 맞춰 `REPORT_EVAL_PERSIST_FEATURES`(bool, 기본 False) 추가.
`server/README.md` env 표에 한 줄 문서화: "업로드 시 evaluate(persist=True)를 서버 소유
eval DB 로 실행해 features/evaluation 축적 (calibrate 재료). 기본 0".

### Step 2 — `web_report/eval_export.py`: persist 경로 (핵심)

```python
_FEATURES_LOCK = threading.Lock()   # persist 직렬화 (SQLite 동시 쓰기 경합 방지)


def _engine_for_persist():
    """evaluate + engine config 를 서버 소유 DB 로 강제한 상태로 반환.

    engine config.DB_PATH 는 import 시점 env 로 고정되므로, env 부재/오설정 시
    eval_analyzer 소유 기본 경로(data/eval.db)로 흘러가는 것을 코드로 차단한다 —
    db_input/import_csv._import_group(103행~)이 쓰는 공인 override 패턴과 동일.
    """
    path = str(_EVAL_DIR)
    if path not in sys.path:
        sys.path.append(path)
    from eval_engine import config as engine_config, evaluate
    engine_config.DATA_DIR = db_path().parent
    engine_config.DB_PATH = db_path()
    return evaluate


def persist_features(session_id: str, *, report_db, upload_root, tables=None) -> dict:
    """세션 rawdata 를 evaluate(persist=True)로 서버 소유 eval DB 에 적재 (E-3).

    features/evaluation/eval_precedent 가 쌓인다 — calibrate(분위수 재보정) 재료.
    업로드당 1회 전제(훅이 보장). 게이트: config.REPORT_EVAL_PERSIST_FEATURES.
    """
    import config as server_config
    if not getattr(server_config, "REPORT_EVAL_PERSIST_FEATURES", False):
        return {"skipped": "disabled"}
    session = report_db.get_session(session_id)
    if not session or str(session.get("source") or "") != "web_report":
        return {"skipped": "not a web_report session"}

    from . import ai_comment
    if tables is None:
        from . import loader
        tables = loader.load_tables(session_id, report_db=report_db,
                                    upload_root=Path(upload_root), session=session)

    evaluate = _engine_for_persist()
    open_conn().close()          # 스키마/마이그레이션 선보장 (export 와 동일 파일)
    stored = 0
    with _FEATURES_LOCK:
        for idx, table in enumerate(tables or []):
            meta = ai_comment._session_meta(session, idx + 1)
            if meta is None:
                return {"skipped": f"unsupported product_type: {session.get('product_type')!r}"}
            meta["session_id"] = session_id            # ingest_run 역참조 (Step 3 확인 반영)
            meta["ingested_by"] = "web_report_features"
            items = list(table.item_columns)
            if not items:
                continue
            raw_df = ai_comment._table_to_raw_df(table, items)
            result = evaluate({"meta": meta, "raw_df": raw_df}, persist=True)
            stored += len(result.get("cases") or [])
    return {"cases": stored, "sources": len(tables or [])}
```

추가로 `safe_export` 와 동일 패턴의 격리 래퍼 + 비동기 훅:

```python
def safe_persist_features(session_id, *, report_db, upload_root, tables=None) -> dict:
    """persist_features 실패 격리 — warning 로그 + 감사(action='eval_features')."""
    # safe_export(344행~)와 동일 구조로 작성 (log_audit action 만 다름)

def persist_features_async(session_id, *, report_db, upload_root) -> None:
    """훅 전용 데몬 스레드 (export_async 관례)."""
```

### Step 3 — 검증 기반 마무리 2건 (코드 확정 전에 반드시 확인)

1. **meta 통과 확인**: `pipeline/ingest.py` L0 이 `create_ingest_run` 에 우리가 넣은
   `session_id`/`ingested_by` meta 키를 그대로 전달하는지 확인하라.
   - 전달되면: 위 코드 유지 + (선택) 재실행 감지에 활용 가능.
   - 전달 안 되면: meta 주입 2줄을 제거하고, 그 사실을 완료 보고에 명시
     (업로드 1회 훅 전제라 dedup 없이도 동작엔 문제 없음 — admin 수동 재실행만 중복 생성 가능).
2. **persist 대상 확인**: evaluate 직후 같은 프로세스에서
   `engine_config.DB_PATH == db_path()` 임을 assert 하는 테스트를 넣어
   기본 경로(data/eval.db) 오염이 코드로 불가능함을 증명하라.

### Step 4 — 훅: `web_report/ingest.py`

기존 `eval_export.export_async(...)`(169행 부근) 바로 다음에, 동일한 try/except 격리로
`eval_export.persist_features_async(session_id, report_db=report_db, upload_root=...)` 추가.
(편집 저장 훅에는 넣지 않는다 — features 는 rawdata 의 함수라 업로드 1회면 충분.)

### Step 5 — admin 수동 실행 (소규모)

`server/admin_panel/eval_admin.py` 의 `reexport` 패턴 그대로
`persist_features(session_id)` 동기 함수 1개 추가(+ 기존 Eval DB 탭의 재적재 버튼 옆에
"features 적재" 액션 노출 — admin 라우트/템플릿 구조를 읽고 기존 패턴을 따르라).
env 꺼짐이면 `{"skipped":"disabled"}` 를 그대로 표시.

### Step 6 — 문서 개정 (합의 반영)

- `docs/13_eval_analyzer_integration.md` §4: "ai_comment 경로는 persist=False 불변.
  단 eval_export 의 features 축적 경로는 **서버 소유 REPORT_EVAL_DB_PATH 를 대상으로만**
  persist=True 를 사용한다(engine config 강제 재지정 가드 포함, env
  REPORT_EVAL_PERSIST_FEATURES 게이트)" 로 개정. §9 에 features 경로 추가.
- `server/README.md`: env 표에 `REPORT_EVAL_PERSIST_FEATURES` +
  **운영 배선 줄**: 서버 기동 환경에 `EVAL_DB_PATH=<REPORT_EVAL_DB_PATH 실제 경로>` 를
  설정하면 AI Comment(컴퓨트 워커 포함)의 L5 선례검색이 서버가 쌓은 선례를 읽는다
  (미설정 시 선례 인용이 항상 빈 상태로 동작 — 기능은 정상, 인용만 없음).

### Step 7 — 테스트

`tests/test_eval_export.py`(또는 신규 test_eval_features.py) — 기존 픽스처 재사용:
1. env 게이트 off → `{"skipped":"disabled"}`, DB 무변화.
2. on → persist_features 후 서버 소유 eval DB 에 `features`/`evaluation` 행 존재
   (`SELECT COUNT(*) FROM features` > 0), **eval_analyzer/data/ 아래에는 파일 미생성**.
3. Step 3-2 의 DB_PATH 강제 assert.
4. 같은 세션 2회 실행 시 동작 확인 (case 는 자연키 upsert 라 중복 없음 —
   evaluation 은 회차 추가가 정상임을 테스트 주석으로 명시).

## 검증

1. `python -m pytest tests/ -q` 통과.
2. E2E: `REPORT_EVAL_PERSIST_FEATURES=1` 로 서버 기동(또는 admin "features 적재" 버튼) →
   기존 web_report 세션 1개에 실행 →
   ```
   python -c "import sqlite3;c=sqlite3.connect(r'DB/pe/report/eval/eval.db');print('features',c.execute('select count(*) from features').fetchone()[0]);print('evaluation',c.execute('select count(*) from evaluation').fetchone()[0])"
   ```
   둘 다 0 보다 커지는 것 확인. `eval_analyzer/data/` 에 eval.db 가 **생기지 않았는지** 확인.
3. 게이트 off 상태(기본)로 업로드 → features 무변화(회귀 없음) 확인.
4. `git status` — eval_analyzer/ 무변경.

## 완료 기준

- [ ] 착수 게이트(합의) 확인 기록
- [ ] features/evaluation 실제 축적 (카운트 로그 제시) + 기본 경로 오염 불가 assert
- [ ] ai_comment persist=False 무변경, 기본 env off 에서 완전 무영향
- [ ] docs/13·server/README.md 개정 포함
- [ ] 완료 보고: 변경 파일 / 검증 로그 / Step 3-1 확인 결과 /
      후속(운영: EVAL_DB_PATH 배선, calibrate 는 R-5 절차 합의 후)

## 하지 말 것

- ai_comment 경로 persist 변경, eval_analyzer 파일 수정(문서 포함),
  콜드 빌드(컴퓨트 워커) 안에서 persist 실행(동시성), env 기본값 on,
  eval_analyzer 소유 기본 DB 로의 어떤 쓰기도.

# 02. DB (SQLite) 블록

`/pe/report` 모듈 전용 SQLite. 기존 `DB/TESTSPEC/test_spec.db` 와는 별도 파일을 사용.

- **경로**: `DB/pe/report/report.db` ([config.py:31](../config.py#L31))
- **단일 모듈**: [report_db.py](../report_db.py) — 스키마, 마이그레이션, CRUD, lock 전부 여기.
- **원칙**: DB 는 *요약과 인덱스*만. 원본 CSV 와 Plotly JSON 본문은 절대 저장하지 않음 (S3 위치만 보관).

---

## 1. 초기화 / 연결

```python
init_report_db()        # report_extension import 시 1회 호출
with get_conn() as conn:
    conn.execute(...)   # row_factory = Row, busy_timeout = 5s
```

`init_report_db` ([report_db.py:144-152](../report_db.py#L144-L152)):
1. DB 파일 부모 디렉토리 생성
2. `_migrate(conn)` — 이전 스키마 호환 (`report_object_info` PK 변경, `report_session` 컬럼 추가)
3. `SCHEMA` executescript (모든 테이블 + 인덱스 `IF NOT EXISTS`)
4. PRAGMA: `journal_mode=WAL`, `synchronous=NORMAL`, `temp_store=MEMORY`, `busy_timeout=5000`

---

## 2. 테이블 6개

### 2.1 `report_session` — 사용자 분석 실행 기록

| 컬럼 | 타입 | 비고 |
|------|------|------|
| `session_id` | TEXT UNIQUE | `{epoch}_{token_hex(3)}` |
| `analysis_key` | TEXT | sha256, NULL 가능 (분석 전) |
| `file_name` | TEXT | 콤마구분 파일명 목록 |
| `file_path` | TEXT | 원본 업로드 디렉토리 (분석 후 삭제됨) |
| `content_hash` | TEXT | 파일 본문 sha256 |
| `status` | TEXT | `pending` / `running` / `done` / `reused` / `failed` |
| `error_message` | TEXT | failed 시 |
| `product_type`, `process`, `product`, `revision` | TEXT | 메타데이터 필터 (migrate 로 추가) |
| `dataset_id` | TEXT | cumulative dashboard 와 연동 (execute-debug 경로) |
| `created_at`, `updated_at` | INTEGER | unix epoch |

인덱스: `idx_report_session_analysis_key(analysis_key)`

`update_session` 은 `_SESSION_UPDATABLE` 화이트리스트(`analysis_key, content_hash, status, error_message, file_path`)만 허용.

### 2.2 `report_analysis_summary` — 통계 요약 행

`UNIQUE(analysis_key, item_name, bin_number)` 가 중복 방지의 핵심.
`item_name` 의 특수값: `__bin_total__` (bin 전체 합계 행). 일반 행은 과목명.
`bin_number` NULL → per-subject overall. 0 이상 정수 → per-bin 분포.

컬럼: `yield_percent, fail_count, cpk_val, mean_val, stdev_val, lsl, usl, unit`

배치 입력: `save_summary_batch(analysis_key, session_id, rows)` — `executemany` + `INSERT OR IGNORE`.
조회: `get_summary_by_analysis_key(analysis_key)` — `ORDER BY item_name, bin_number IS NULL DESC, bin_number` (overall 행이 먼저).

### 2.3 `report_object_info` — S3 객체 위치 인덱스

`UNIQUE(analysis_key, object_type)`. `object_type` 종류:

| object_type | 내용 | S3 키 패턴 |
|-------------|------|------------|
| `plotly` | 전체 Plotly JSON | `pe/report/plotly/{key}.json` |
| `fail_items` | fail item 통계 JSON | `pe/report/fail_items/{key}.json` |
| `issue_table` | issue table JSON | `pe/report/issue_table/{key}.json` |
| `thumbs_fail_set` | fail subject SVG 묶음 (prefix) | `pe/report/thumbs/{key}/` |

`upsert_object_info(...)` → `INSERT ... ON CONFLICT(analysis_key, object_type) DO UPDATE`.
`touch_object_info(analysis_key, object_type="plotly")` → `last_accessed` 갱신.

### 2.4 `report_analysis_lock` — 분석/플롯 락

`PRIMARY KEY(analysis_key)`. `owner` 는 `analyze:{session_id}` 또는 `plot:{session_id}:{epoch}`.
TTL: 300s (`REPORT_LOCK_TTL_SEC`).

```python
try_acquire_analysis_lock(key, owner)   # 만료 lock 청소 후 INSERT 시도. False = busy
release_analysis_lock(key, owner)       # owner 일치 시만 DELETE
```

### 2.5 `report_csv_files` — 원본 CSV S3 위치

`UNIQUE(analysis_key, filename)`. `file_size` 는 history 페이지에서 SUM 으로 표시.

### 2.6 `report_annotation` — 사용자 코멘트

`(session_id, analysis_key, target, content, created_at, updated_at)`.
`target` 은 자유 문자열 (예: `yield:bin=3`, `subject:42`). 클라이언트가 의미 결정.
CRUD: `create_annotation / get_annotations / update_annotation / delete_annotation`.

---

## 3. 자주 쓰는 쿼리 패턴

```python
# 캐시 hit 검사
report_db.has_summary(analysis_key)             # bool, LIMIT 1

# 세션 완전 복원 (UI 가 호출)
session   = report_db.get_session(session_id)
summary   = report_db.get_summary_by_analysis_key(akey)
csv_files = report_db.get_csv_files(akey)
objects   = report_db.get_all_object_infos(akey)   # 모든 object_type 한 번에
notes     = report_db.get_annotations(session_id)

# history 페이지
rows = report_db.get_history(product_type=..., process=..., product=..., revision=...)
# → LEFT JOIN report_csv_files, GROUP BY session_id, total_file_size 합산, LIMIT 200
```

---

## 4. 트랜잭션 / 동시성

- `get_conn()` 은 컨텍스트 매니저, 종료 시 `conn.commit()`. 예외 시 자동 롤백 없음 (sqlite3 기본).
- WAL 모드이므로 read 와 write 가 동시에 가능.
- `busy_timeout = 5000`(ms) — lock 경쟁 시 SQLite 가 내부 재시도.
- 락 테이블은 *분석 단위 동시 진입 차단용*이지 트랜잭션 락이 아님. analyze / plot 양쪽에서 같은 `analysis_key` 공유.

---

## 5. 마이그레이션

`_migrate(conn)` ([report_db.py:105-141](../report_db.py#L105-L141)):
- `report_object_info`: 과거 `analysis_key PRIMARY KEY` → 신규 `id PK + UNIQUE(analysis_key, object_type)` 로 자동 변환 (테이블 RENAME + INSERT)
- `report_session`: `product_type, process, product, revision, dataset_id` 컬럼이 없으면 `ALTER TABLE ADD COLUMN` 으로 추가
- 멱등 (`IF NOT EXISTS`, `PRAGMA table_info` 체크)

새 컬럼/테이블 추가 시 같은 패턴으로 `_migrate` 에 분기 추가하면 됨. SCHEMA 의 `CREATE TABLE` 도 함께 갱신할 것.

---

## 6. 절대 하면 안 되는 것

- `report_analysis_summary` 에 raw CSV / JSON 본문 / 대형 matrix 저장 — S3 또는 디스크로 보낼 것
- `report_object_info.s3_key` 에 본문 저장 — 위치만
- session_id 기반 캐싱 — 재사용은 항상 **analysis_key** 기준
- `report.db` 직접 수정 (외부 도구로 ALTER 등) — 반드시 `_migrate` 를 거치도록

---

## 7. 관련 파일

- 스키마/CRUD: [report_db.py](../report_db.py)
- 호출자: [report_analysis_service.py](../report_analysis_service.py), [report_plot_service.py](../report_plot_service.py), [report_routes.py](../report_routes.py)
- DB 경로 설정: [config.py:31](../config.py#L31) (`REPORT_DB_PATH`)
- 락 TTL/대기 시간: [config.py:48-50](../config.py#L48-L50)
- 별도 SQLite 실험 디렉토리: [../../plotly_sqlite/](../../plotly_sqlite/) — 본 모듈과 무관

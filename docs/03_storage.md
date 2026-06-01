# 03 · 서버 — 저장소 (SQLite 스키마 + S3 키)

> 모든 영속 데이터의 실체. 텍스트/메타 = SQLite, 본문(xlsx·이미지·JSON) = S3. analysis_key 가 둘을 잇는다.
> 관련: 쓰는 쪽 [01 업로드](01_server_upload.md) · 읽는 쪽 [02 조회](02_server_query_edit.md)

## 파일
- [server/database/report_db.py](../server/database/report_db.py) — 스키마/마이그레이션/CRUD/락
- [server/s3_storage/report_s3.py](../server/s3_storage/report_s3.py) — boto3 클라이언트 + 키 빌더
- [server/config.py](../server/config.py) — DB 경로·S3 자격증명·키 prefix

## SQLite 테이블 ([report_db.py `SCHEMA`](../server/database/report_db.py#L7))
| 테이블 | 역할 | 핵심 컬럼 / UNIQUE |
|--------|------|--------------------|
| `report_session` | 업로드 1건 = 1행 | `session_id`(UNIQUE), `analysis_key`, `status`, `product_type/product/lot_id`, `password`, `source` |
| `report_analysis_summary` | yield/항목 표 행 | `UNIQUE(analysis_key,item_name,bin_number)`, `yield_percent/fail_count/cpk_val/mean_val…` |
| `report_object_info` | S3 객체 포인터 | `UNIQUE(analysis_key,object_type)`, `s3_bucket/s3_key/s3_uri`, `content_hash`, `options_json` |
| `report_analysis_lock` | analysis_key 동시성 락 | `analysis_key`(PK), `owner`, `expires_at` (TTL 300s) |
| `report_csv_files` | (legacy) CSV 첨부 | `UNIQUE(analysis_key,filename)` |
| `report_annotation` | 세션 주석 | `session_id` 인덱스 |
| `report_dashboard_comment` | (legacy Dash) 편집셀 | `UNIQUE(dataset_id,kind,item_key)` |

> 현재 xlsx_upload 흐름에서 실제로 쓰는 건 **session / summary / object_info / annotation**. 나머지는 legacy 보존.

### object_type 종류 (report_object_info)
| object_type | S3 내용 | 키 빌더 |
|-------------|---------|---------|
| `source_xlsx` | 원본 xlsx 본문 | `make_source_xlsx_s3_key` |
| `summary_text` | summary 시트 추출 JSON | `make_summary_text_s3_key` |
| `issue_table_text` | issue_table 추출 JSON | `make_issue_text_s3_key` |
| `chart_index` | `{"count":N}` (차트 장수) | `make_chart_index_s3_key` |

### 마이그레이션 — `_migrate()` [report_db.py:133](../server/database/report_db.py#L133)
빈 DB 면 no-op(SCHEMA 가 생성). 기존 DB 면:
- `report_object_info` 옛 PK(analysis_key) → `id` PK + `UNIQUE(analysis_key,object_type)` 재작성.
- `report_session` 에 누락 컬럼(`analysis_key/content_hash/…/source`) ALTER ADD.
`init_report_db()` 가 `_migrate` → `executescript(SCHEMA)` → WAL/synchronous=NORMAL PRAGMA. import 시 [report_extension.py](../server/report/report_extension.py) 가 호출.

### 주요 CRUD (전부 `get_conn()` 컨텍스트, row_factory=Row)
- 세션: `create_session`(source 인자), `update_session`(화이트리스트 `_SESSION_UPDATABLE` 만), `get_session`, `get_history`(필터+JOIN), `delete_session`(+annotation 삭제).
- summary: `save_summary_batch`(INSERT OR IGNORE), `replace_summary_batch`(DELETE+INSERT, 수정모드), `get_summary_by_analysis_key`.
- object: `upsert_object_info`(ON CONFLICT UPDATE), `get_object_info`, `get_all_object_infos`, `touch_object_info`.
- 락: `try_acquire_analysis_lock`(만료행 청소 후 INSERT, IntegrityError=실패), `release_analysis_lock`.

## S3 ([report_s3.py](../server/s3_storage/report_s3.py))
- 클라이언트: `get_s3_client()` 싱글톤. `REPORT_S3_BUCKET` 비면 `S3NotConfigured` raise → 호출측이 그레이스풀 처리. endpoint/access/secret 있으면 호환 스토리지, 없으면 boto3 기본 자격증명·AWS. `max_pool_connections` = config(기본 30).
- 입출력: `upload_bytes_to_s3` / `download_bytes_from_s3` / `upload_json_to_s3` / `download_json_from_s3`(깨진 JSON 시 `S3ObjectCorrupted`) / `s3_object_exists`(head_object).
- 키 패턴 (prefix 는 [config.py](../server/config.py#L27), 모두 `pe/report_server/` 네임스페이스로 plotly legacy 와 충돌 회피):
  ```
  source_xlsx/<akey>.xlsx        summary_text/<akey>.json
  issue_table_text/<akey>.json   chart_png/<akey>/<idx>.png  +  chart_png/<akey>/index.json
  ```

## 환경변수 (config.py)
서버: `HOST/PORT`, `REPORT_DB_PATH`, `REPORT_S3_ENDPOINT/BUCKET/REGION/ACCESS_KEY/SECRET_KEY`,
각 `REPORT_S3_*_PREFIX`, `HONEY_RELEASES_DIR`. `REPORT_S3_BUCKET` 비면 모든 S3 동작이 503/그레이스풀.

## 주의 (불변 규칙 §1·§3·§4)
- xlsx 본문은 절대 SQLite 에 넣지 않는다 — object_info 의 s3_key 만.
- `report_` prefix 없는 테이블 추가 금지.
- analysis_key 는 항상 `sha256(xlsx + canonical meta)`. meta 키 추가/순서는 `sort_keys` 라 안전하지만 **새 필드 추가 시 기존 키가 전부 달라짐** 주의.
- summary 의 `UNIQUE(analysis_key,item_name,bin_number)` 때문에 같은 키 재업로드는 INSERT OR IGNORE 로 중복 무시. 수정은 replace 로 전체 치환.

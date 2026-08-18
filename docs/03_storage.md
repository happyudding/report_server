# 03 · 서버 — 저장소 (SQLite 스키마 + storage_gateway/S3 키)

> 모든 영속 데이터의 실체. 텍스트/메타 = SQLite, 본문(이미지·parquet·PNG) =
> storage_gateway(S3 + 로컬 fallback). analysis_key 가 둘을 잇는다. **원본 xlsx 는 저장하지
> 않는다.** 관련: 쓰는 쪽 [01 업로드](01_server_upload.md) · [10 web_report](10_web_report_pipeline.md) ·
> 읽는 쪽 [02 조회](02_server_query_edit.md)

## 파일
- [server/database/core.py](../server/database/core.py) — **스키마(SCHEMA) 정본**·마이그레이션·get_conn·락 (report_db.py 는 재노출 facade)
- [server/storage_gateway/](../server/storage_gateway/) — 산출물 저장/조회 **단일 진입점** (`ENTRYPOINT / EXTERNAL_OWNER`, 실무 가이드 [README](../server/storage_gateway/README.md))
- [server/config.py](../server/config.py) — DB 경로·S3 자격증명·키 prefix

## SQLite 테이블 (정본 [core.py `SCHEMA`](../server/database/core.py), 스냅샷 [report_README.md](../DB/pe/report/report_README.md))
테이블 25개. 요지 (전체 컬럼은 스냅샷 참조):

| 테이블 | 역할 | 핵심 컬럼 / UNIQUE |
|--------|------|--------------------|
| `report_session` | 업로드 1건 = 1행 | `session_id`(UNIQUE), `analysis_key`, `status`, `product_type/product/lot_id`, `source`, `mode`, `uploaded_by`, `client_host`, `webreport_options`, `password`(**폐지 2026-08-14** — 신규 미저장·기존 NULL, `has_password` 항상 false) |
| `report_analysis` | analysis_key 단위 물리 원본 상태 (dedup 형제의 단일 진실) | `analysis_key`(PK), `content_hash`(authoritative), `source_count`, `artifact_status`, `last_access_at` |
| `report_session_blob` | 세션 단위 **큰 본문 포인터** (본문은 객체 저장) | `PK(session_id,kind)`, `backend`(`s3`\|`local`\|`local_pending`), `object_key`, `content_hash`(sha256), `base_token`(낙관적 잠금 base), `size_bytes` |
| `report_schema_migration` | backfill 단계·cursor (중단 후 재개) | `step`(PK), `status`, `cursor` |
| `report_chatbot_daily` | 챗봇 원문 보존기간 후에도 남는 일별 비식별 집계 | `PK(day,intent,planner,result)`, `cnt`, `*_ms_sum` |
| `report_analysis_summary` | yield/항목 표 행 | `UNIQUE(analysis_key,item_name,bin_number)`, `yield_percent/fail_count/cpk_val/mean_val…` |
| `report_object_info` | 산출물 포인터 | `UNIQUE(analysis_key,object_type)`, `s3_bucket/s3_key/s3_uri`, `content_hash`, `options_json`(`{"storage":..}`) |
| `report_sheet_data` | xlsx 추출 텍스트 | `PK(analysis_key,sheet_name)`, `data_json` |
| `report_audit_log` | upload/edit/delete 감사 | 메타 스냅샷 + `client_ip/user_agent/client_user/client_host/result` |
| `report_webreport_edit` / `_rev` | web_report 편집 진실 (세션 단위) | `PK(session_id,kind,item_key)` / `rev`(무효화 토큰). **`note_sheet` 본문만 객체 저장으로 분리** — 저장은 blob+legacy dual-write, 조회는 blob 우선+legacy 폴백 |
| `report_session_editor` | 편집 위임 | `PK(session_id,editor_user)` |
| `report_web_visitor` | 편집자 후보 풀 | `user_id`(PK) |
| `report_usage_daily` | 접속 사용량 일별 카운터 (Honey 실행·웹 방문 — 관리자 통계 탭) | `PK(day,kind,user_id)`, `kind`=`honey_run/web_index/web_view`, 무신원은 `ip:<addr>` ([database/usage.py](../server/database/usage.py)) |
| `report_usage_hourly` | 같은 접속을 시각(0~23) 축으로도 집계 (관리자 요일×시간 히트맵) | `PK(day,hour,kind,user_id)`, `record_usage` 가 일별과 **한 트랜잭션**으로 함께 기록 ([database/usage.py](../server/database/usage.py)) |
| `report_usage_peak_daily` | 일별 Peak 동시 접속자(사람) 수 | `day`(PK), `peak_users`는 SQL `MAX` 라 **낮아지지 않음**(재시작 대비), `window_sec`=그때의 '동시' 판정 창. 적재는 [admin_panel/metrics.py](../server/admin_panel/metrics.py) `_record_user_peak` |
| `report_user_important` / `report_user_favorite` | 개인 중요표시/즐겨찾기 | `PK(user_id,session_id)` |
| `report_chatbot_log` | 웹 챗봇 질문/답변 + 부하 계측 (관리자 Chatbot 탭) | `created_at DESC` 인덱스, `total_ms`=`wait_ms`(동시실행 대기)+`llm_ms`+조회, `result`=`ok/busy/error:*` ([database/chatbot_log.py](../server/database/chatbot_log.py)) |
| `report_annotation` | 세션 주석 | `session_id` 인덱스 |
| `report_analysis_lock` | analysis_key 동시성 락 | `analysis_key`(PK), `expires_at`(TTL 300s) |
| `report_csv_files` / `report_dashboard_comment` / `report_user` | legacy 보존 | (미사용) |

### object_type 종류 (report_object_info)
| object_type | 내용 | 저장 |
|-------------|------|------|
| `distribution_combined` | 합성 분포 PNG | S3/로컬 |
| `web_report_source_<idx>` | web_report parquet 원본 | S3/로컬 (options_json 에 위치 기록) |
| `web_report_manifest` | web_report manifest JSON | 〃 |
| `chart_index` | `{"count":N}` (legacy 차트 장수) | S3 |
| `summary_text` / `issue_table_text` | (legacy) 추출 JSON — 현행은 DB `report_sheet_data` | S3 |

### 마이그레이션 — `core.py`
빈 DB 면 SCHEMA 가 전부 생성(`CREATE TABLE IF NOT EXISTS`). 기존 DB 면 누락 컬럼 ALTER ADD +
product_type 약어(`MD`→`MDDI` 등) 정규화. `init_report_db()` → SCHEMA 실행 → WAL/synchronous=
NORMAL PRAGMA. [report_extension.py](../server/report/report_extension.py) 가 호출.

### 주요 CRUD (전부 `get_conn()` 컨텍스트, row_factory=Row)
- 세션: `create_session`(source/mode/uploaded_by 인자), `update_session`(화이트리스트만),
  `get_session`, `get_history`(필터+JOIN), `delete_session`(+annotation 삭제).
- summary: `save_summary_batch`(INSERT OR IGNORE), `get_summary_by_analysis_key`.
- object: `upsert_object_info`(ON CONFLICT UPDATE), `get_object_info`, `get_all_object_infos`.
- web_report 편집: `get/apply_webreport_edits`, `get_webreport_edit_rev`
  ([webreport_edits.py](../server/database/webreport_edits.py)).
- 락: `try_acquire_analysis_lock`, `release_analysis_lock`.

## storage_gateway (S3 = 외부 프로젝트, 검증용)
S3 는 외부 경계다 — 미설정 시 로컬 폴백으로 동작한다. facade 공개 API·저장 위치 기록 계약·
키 prefix 는 **정본 [storage_gateway/README.md](../server/storage_gateway/README.md)** 참조.
요지: 프로젝트 코드는 내부 `_s3` 어댑터를 직접 import 하지 않고 facade 만 의존하며,
web_report parquet/manifest 는 저장 위치를 `report_object_info.options_json` 에 기록하고
조회가 그 기록을 따른다(s3 기록 다운로드 실패 시 예외 — 로컬 부활 방지).

## 환경변수
전체는 [server/README.md](../server/README.md)(정본). `REPORT_S3_BUCKET` 비면 모든 S3 동작이
로컬 폴백. S3 키 prefix 는 [config.py](../server/config.py) `REPORT_S3_*_PREFIX`
(web_report·distribution prefix 는 [_s3.py](../server/storage_gateway/_s3.py) 상수).

## 세션 blob (큰 본문의 객체 저장 — 2026-08-14)
"조회·조인하지 않는 큰 본문"만 DB 밖으로 뺀다. 현재 대상은 Note 시트 JSON 하나
(kind=`note_sheet` — 이미지가 base64 로 들어와 최대 10MB).

- 키 고정: `pe/report_server/session_blob/<session_id>/<kind>/<sha256>.json.gz`.
  로컬은 같은 상대경로로 `uploads/report/session_blob/…` (원자적 temp→replace).
- facade: `storage_gateway.save/load/delete_session_blob`, `promote_session_blob`
  (`_session_blobs.py`). 포인터 CRUD 는 [database/session_blobs.py](../server/database/session_blobs.py).
- S3 업로드 실패 시 `backend='local_pending'` 으로 **로컬에 보관**하고 cleanup 이 재이관한다
  (관리자 스토리지 탭에 미이관 건수·바이트 경고). 사용자 입력을 잃지 않는 것이 우선.
- **크기 기준으로 무조건 파일화하지 않는다.** `report_sheet_data`(세션당 수 KiB)는 legacy
  xlsx 조회의 정본이라 DB 에 유지한다.

## 주의 (불변 규칙 §1·§3·§4)
- 원본 xlsx 는 서버로 전송·저장하지 않는다 — 추출 텍스트는 DB(sheet_data), issue PNG 만 S3.
- `report_` prefix 없는 테이블 추가 금지.
- analysis_key 는 항상 `sha256(canonical(sheet_grids) + canonical meta)`. meta 키 추가/순서는 `sort_keys` 라 안전하지만 **새 필드 추가 시 기존 키가 전부 달라짐** 주의.
- summary 의 `UNIQUE(analysis_key,item_name,bin_number)` 때문에 같은 키 재업로드는 INSERT OR IGNORE 로 중복 무시. 수정은 replace 로 전체 치환.

# 02 · 서버 — 조회 · 수정 · 삭제 · 주석 · 차트 서빙

> 브라우저가 업로드된 세션을 검색/열람/편집/삭제하는 모든 라우트. 분석 라우트는 전부 제거됨.
> 관련: 데이터 생성 [01 업로드](01_server_upload.md) · 저장 구조 [03](03_storage.md) · 화면 HTML 은 server/report/*.html

## 파일
- [server/report/report_routes.py](../server/report/report_routes.py) — 모든 라우트
- [server/report/report_analysis_index.html](../server/report/report_analysis_index.html) — 검색결과 페이지(모달 없음)
- [server/report/report_view.html](../server/report/report_view.html) — 세션 상세(보기/수정/삭제)
- DB 접근은 [report_db.py](../server/database/report_db.py), S3 다운로드는 [report_s3.py](../server/s3_storage/report_s3.py) (→[03](03_storage.md))

## 라우트 맵 (prefix `/pe/report`)
| 메서드 · 경로 | 함수 | 용도 |
|---------------|------|------|
| GET `/` | `index_page` | 검색결과 HTML |
| GET `/view/<sid>` | `view_page` | 세션 상세 HTML |
| GET `/api/history` | `history` | 세션 목록(필터) |
| GET `/result/<sid>` | `result` | 가벼운 상태+summary |
| GET `/session/<sid>` | `session_info` | 세션 단건(PIN 제거) |
| GET `/session/<sid>/full` | `session_full` | **상세 전체 복원** |
| GET `/chart/<sid>/<idx>` | `chart_image` | 차트 PNG 스트리밍 |
| POST `/session/<sid>/verify_password` | `verify_session_password` | 수정/삭제 진입 PIN 확인 |
| PATCH `/session/<sid>/content` | `update_session_content` | 텍스트 콘텐츠 수정 |
| DELETE `/session/<sid>` | `delete_session_route` | 세션 삭제 |
| POST/GET/PATCH/DELETE `/annotation[...]` | `*_annotation` | 주석 CRUD |
| GET `/_threads` | `debug_threads` | hang 진단 스택덤프 |

## 핵심 흐름

### 검색 목록 — `history()` [report_routes.py:334](../server/report/report_routes.py#L334)
쿼리스트링(`product_type/process/product/revision/lot_id/source`) → `report_db.get_history()`.
`get_history` 는 `status IN ('done','reused')` 만, `lot_id` 는 LIKE, `password` 는 노출 안 하고 `has_password` 불린만, CSV 합계 크기 LEFT JOIN. (→[03](03_storage.md) `get_history`)

### 세션 상세 복원 — `session_full()` [report_routes.py:111](../server/report/report_routes.py#L111)
1. `get_session` → 없으면 404.
2. `get_all_object_infos(akey)` → `objects[object_type] = {s3_uri, s3_key}`.
3. `summary_text` / `issue_table_text` object 있으면 `download_json_from_s3` 로 본문 가져옴(예외 시 None).
4. `chart_index` object 있으면 manifest count 읽어 `charts = [{index, url:/pe/report/chart/<sid>/<i>}]`.
5. 응답: `session`(PIN 제거), `summary`(DB), `summary_text`, `issue_table_text`, `charts`, `csv_files`, `objects`, `annotations`.

### 차트 이미지 — `chart_image()` [report_routes.py:159](../server/report/report_routes.py#L159)
`make_chart_png_s3_key(akey, idx)` → `download_bytes_from_s3` → `Response(mimetype=image/png, Cache-Control private)`. 공개버킷/presign 없이 **서버 경유** 서빙(기존 패턴 일관). idx 범위 0~1000.

### 수정 저장 — `update_session_content()` [report_routes.py:211](../server/report/report_routes.py#L211)
PIN 검증(`_password_ok`) 후, body 에 온 부분만 갱신:
- `yield_rows` → [`_coerce_yield_row`](../server/report/report_routes.py#L64) 타입정리 → `replace_summary_batch`(DELETE+INSERT, DB).
- `summary_text` / `issue_rows` → [`_write_text_object`](../server/report/report_routes.py#L269) (S3 JSON 재업로드 + `upsert_object_info`, content_hash/options_json 은 기존행 유지).
- **analysis_key 는 재계산 안 함**(원본 업로드 식별자 유지). 부분 성공 시 207, 전부 실패 500.

### 보안 헬퍼
- [`_public_session`](../server/report/report_routes.py#L28) — `password` 컬럼 제거 + `has_password` 추가.
- [`_password_ok`](../server/report/report_routes.py#L38) — 저장된 PIN 있으면 일치해야 True, **없으면(legacy 세션) 항상 True**.
- `_validate_session_id`(`[A-Za-z0-9_-]{1,80}`), `_validate_analysis_key`(64 hex).

## 주의
- 삭제는 `delete_session` 이 `report_annotation` 까지 지움. S3 객체/summary 행은 **남는다**(키 기반 멱등이라 의도적). 완전 GC 는 미구현.
- 수정 모드에서 S3 미설정이면 `summary_text/issue_rows` 는 에러로 표시되지만 `yield_rows`(DB) 는 저장됨 → 부분 성공 207.
- 분석/플롯 라우트는 절대 여기 추가 금지 (CLAUDE.md §5.2).

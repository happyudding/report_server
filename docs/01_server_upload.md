# 01 · 서버 — xlsx 업로드 파이프라인

> Honey 가 보낸 xlsx 1개 + 메타를 받아 S3 저장 + DB 세션 생성 + 텍스트 추출까지 끝내는 단일 라우트.
> 관련: 들어오는 쪽 [07 클라 업로드](07_client_upload_chart.md) · 저장 대상 [03 저장소](03_storage.md) · 조회 [02](02_server_query_edit.md)

## 파일
- [server/upload_xlsx.py](../server/upload_xlsx.py) — 라우트 `POST /pe/report/upload_xlsx` 전체
- [server/xlsx_parser.py](../server/xlsx_parser.py) — openpyxl 텍스트 추출
- [server/s3_storage/report_s3.py](../server/s3_storage/report_s3.py) — S3 업로드/키 빌더 (→[03](03_storage.md))
- [server/database/report_db.py](../server/database/report_db.py) — 세션/summary/object 저장 (→[03](03_storage.md))

## 흐름 (`upload_xlsx()` [upload_xlsx.py:122](../server/upload_xlsx.py#L122))
1. **파일 검증** — `request.files["xlsx"]` 존재·`.xlsx` 확장자·비어있지 않음. `secure_filename`.
2. **메타 검증** [`_validate_meta`](../server/upload_xlsx.py#L51) — `product_type ∈ {MD,PD,PM,SE}`, `product`/`lot_id` 는 안전토큰 정규식. **PIN** `^\d{4}$` 필수.
3. **키 산출** — `analysis_key = _compute_analysis_key(xlsx_bytes, meta)` ([:71](../server/upload_xlsx.py#L71)) = `sha256(xlsx + "|" + canonical_meta)`. `canonical` = `json.dumps(sort_keys, ensure_ascii=False, separators=(",",":"))`. PIN 은 meta 에 **불포함**. `content_hash = sha256(xlsx)`. `session_id = "<epoch>_<hex6>"`.
4. **세션 생성** — `create_session(... source="xlsx_upload")` → `update_session(status="uploading", analysis_key, content_hash)`.
5. **S3 원본 xlsx** — `make_source_xlsx_s3_key(akey)` 키로 `s3_object_exists` 검사 후 없으면 `upload_bytes_to_s3`. 그 후 `upsert_object_info(object_type="source_xlsx")`. `S3NotConfigured` 면 `s3_ok=False` 로 계속, 그 외 예외는 `status="failed"` + 500.
6. **xlsx 파싱** — `parse_report_xlsx(xlsx_bytes)` → `{summary, yield_rows, issue_rows}`. 실패 시 `status="failed"` + 400.
7. **yield → DB** — [`_yield_row_to_summary`](../server/upload_xlsx.py#L79) 로 각 행을 summary 컬럼에 매핑(`item_name`=bin, `yield_percent`=portion 등), `unknown`/빈 행 제거 후 `save_summary_batch`(INSERT OR IGNORE).
8. **summary/issue 텍스트 → S3 JSON** (s3_ok 일 때) — `upload_json_to_s3` + `upsert_object_info("summary_text"|"issue_table_text")`. 실패는 조용히 무시(`except: pass`).
9. **차트 PNG 갤러리** — [`_collect_chart_pngs`](../server/upload_xlsx.py#L37) 가 multipart `chart_0, chart_1, …` 를 PNG 매직바이트 검증하며 수집(최대 50). 각각 `make_chart_png_s3_key(akey, idx)` 로 업로드, 마지막에 `chart_index` object(`{"count": N}`) upsert.
10. **마무리** — `update_session(status="done")`, JSON 응답(`session_id, analysis_key, rows_saved, charts_saved, …`).

## 핵심 포인트 / 주의
- **부분 실패 정책**: S3 미설정·텍스트 업로드·차트 업로드는 *그레이스풀*(세션은 done). 원본 xlsx S3 업로드 실패와 파싱 실패만 `failed` 로 끊는다.
- **멱등성**: 같은 xlsx+meta → 같은 analysis_key → S3 `source_xlsx` 는 exists 검사로 재업로드 skip. 단 `create_session` 은 매번 새 session_id (세션은 누적, S3 본문은 1개).
- **파서 견고성** [xlsx_parser.py](../server/xlsx_parser.py): 셀 좌표 하드코딩 대신 A열 **anchor 텍스트**(`Feature`, `Yield Summary`, `Major Fail Bins`, `Evaluation Summary`)와 헤더행 매칭. `summary` 시트는 섹션 dict, `yield`/`issue_table` 은 행1 헤더 기반 list[dict]. `issue_table` 은 이미지가 든 `Distribution` 컬럼 drop. 이 규약은 클라 [xlsx_writer](../client/report_generator/xlsx_writer.py) 출력과 **짝**이므로 한쪽 바꾸면 양쪽 확인 (→[06](06_analysis_engine.md)).
- 응답 status 코드: 정상 200, 파싱실패 400, S3 본문 실패 500.

## 자주 바뀌는 지점
- 받는 폼 필드 추가 → `_validate_meta` + `post_xlsx`([07](07_client_upload_chart.md)).
- 새 시트 추출 → `parse_report_xlsx` + 새 `object_type` upsert + [03](03_storage.md) object_type 목록.

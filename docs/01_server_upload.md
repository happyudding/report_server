# 01 · 서버 — grid 업로드 파이프라인

> Honey 가 Excel COM 으로 추출한 시트 grid(JSON) + 메타를 받아 DB 세션 생성 + 텍스트 추출 +
> issue PNG 저장까지 끝내는 단일 라우트. **원본 xlsx 파일은 받지 않는다.**
> 관련: 들어오는 쪽 [07 클라 업로드](07_client_upload_chart.md) · 저장 대상 [03 저장소](03_storage.md) · 조회 [02](02_server_query_edit.md)

## 파일
- [server/upload_xlsx.py](../server/upload_xlsx.py) — 라우트 `POST /pe/report/upload_xlsx` 전체
- [server/xlsx_parser.py](../server/xlsx_parser.py) — 시트 grid → 텍스트 추출 (`_GridSheet` 셸, openpyxl 미사용)
- [server/storage_gateway/](../server/storage_gateway/) — S3 산출물 저장 진입점(`save_upload_artifacts`); 내부 어댑터 [_s3.py](../server/storage_gateway/_s3.py) (→[03](03_storage.md))
- [server/database/report_db.py](../server/database/report_db.py) — 세션/summary/object 저장 (→[03](03_storage.md))

## 흐름 (`upload_xlsx()` [upload_xlsx.py:122](../server/upload_xlsx.py#L122))
1. **입력 검증** — `request.form["sheet_grids"]` 존재·유효 JSON·비어있지 않은 객체. `file_name` 폼값을 `secure_filename`.
2. **메타 검증** [`_validate_meta`](../server/upload_xlsx.py#L51) — `product_type ∈ {MDDI,PDDI,PMIC,SECURITY}`, `product`/`lot_id` 는 안전토큰 정규식. **PIN** `^\d{4}$` 필수.
3. **키 산출** — `grids_canonical = json.dumps(sheet_grids, sort_keys, ensure_ascii=False, separators=(",",":"))`. `analysis_key = _compute_analysis_key(grids_canonical, meta)` = `sha256(grids + "|" + canonical_meta)`. PIN 은 meta 에 **불포함**. `content_hash = sha256(grids_canonical)`. `session_id = "<epoch>_<hex6>"`.
4. **세션 생성** — `create_session(... source="xlsx_upload")` → `update_session(status="uploading", analysis_key, content_hash)`.
5. **grid 파싱** — `parse_report_xlsx(sheet_grids)` → `{summary, yield_rows, issue_rows, sheet_data}`. 실패 시 `status="failed"` + 400. (원본 xlsx 는 저장하지 않는다.)
6. **yield → DB** — [`_yield_row_to_summary`](../server/upload_xlsx.py#L79) 로 각 행을 summary 컬럼에 매핑. yield 행 = `bin | Item | {src}_count …(전 소스) | {src}_yield …(전 소스) | avg | comment` → `item_name`=bin, `yield_percent`=**avg**(소스 평균 수율%; legacy `portion(%)`/`yield` fallback), `fail_count`=**`{src}_count` 합**(legacy `count` fallback). 파서는 헤더명으로 읽으므로 count/yield 묶음 순서와 무관. `unknown`/빈 행 제거 후 `save_summary_batch`(INSERT OR IGNORE).
7. **sheet_data → DB** — 추출 텍스트(summary/yield/issue_table)를 `upsert_sheet_data` 로 DB 저장.
8. **issue PNG → 저장소** — multipart `issue_img_<row>` 를 `save_upload_artifacts` 로 S3(또는 로컬 폴백) 보관.
9. **마무리** — `update_session(status="done")`, JSON 응답(`session_id, analysis_key, rows_saved, issue_images_saved, …`).

## 핵심 포인트 / 주의
- **부분 실패 정책**: S3 미설정·이미지 업로드는 *그레이스풀*(세션은 done). 파싱 실패와 yield DB 저장 실패만 `failed` 로 끊는다.
- **멱등성**: 같은 grid+meta → 같은 analysis_key. 단 `create_session` 은 매번 새 session_id (세션은 누적).
- **파서 견고성 (xlsx_writer 레이아웃 짝)** [xlsx_parser.py](../server/xlsx_parser.py): 표가 B열~·헤더 3행인 클라 출력에 맞춰 셀 좌표 대신 **2D anchor 텍스트**로 섹션을 찾는다.
  - `summary` → `1. Device Feature` / `2. Yield` / `Major Fail Bins`(E열) / `3. Evaluation Summary` anchor 기준 dict: `feature`(Total DUT/Pass/Fail Types/Sources/Subjects/**EVT Version**), `yield_summary`(Lot NO/Yield), `major_fail_bins`(1st~5th = subject+ratio), `evaluation`(Yield/CPK/Temp/ETC). 최상위 `title`(A1) 포함.
  - `yield`/`issue_table` → ‘비어있지 않은 셀 2개 이상’ 첫 행을 헤더로 잡는 list[dict]. `issue_table` 은 `Distribution` 컬럼 drop + Category 그룹의 `CPK`/`ETC` 플레이스홀더(빈 bin) 행 제외 → Yield 블록만.
  - 클라 출력은 `xlsx_writer` 가 **xlwings 단일 Excel COM 세션**에서 table/raw/distribution/PNG attachment/save 를 모두 처리한다. table 레이아웃을 바꾸면 이 파서도 함께 확인한다 (→[06](06_analysis_engine.md), [xlsx_writer](../client/report_generator/xlsx_writer.py)).
- 응답 status 코드: 정상 200, 파싱실패 400, S3 본문 실패 500.

## 자주 바뀌는 지점
- 받는 폼 필드 추가 → `_validate_meta` + `post_xlsx`([07](07_client_upload_chart.md)).
- 새 시트 추출 → `parse_report_xlsx` + 새 `object_type` upsert + [03](03_storage.md) object_type 목록.

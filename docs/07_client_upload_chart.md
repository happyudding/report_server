# 07 · 클라이언트 — 업로드 전송 + 차트 PNG 렌더

> 생성/선택한 xlsx 를 서버로 보내는 마지막 단계. 헤드리스 서버 대신 클라 PC 의 Excel 로 차트를 PNG 렌더해 동봉.
> 트리거는 [05 UI `_do_upload`](05_client_ui.md), 받는 쪽은 [01 서버 업로드](01_server_upload.md).

## 파일
- [client/transport/uploader.py](../client/transport/uploader.py) — `post_grids` multipart POST (grid JSON + issue PNG)
- [client/transport/chart_export.py](../client/transport/chart_export.py) — Excel COM 으로 차트 → PNG bytes
- [client/report_flow/upload_prepare.py](../client/report_flow/upload_prepare.py) — Excel COM 으로 시트 grid + issue image 추출
- 호출 지점: [honey_main.py `_do_upload`](../client/honey_main.py)

## 흐름 (`_do_upload` → 두 모듈)
1. `UploadDialog` 로 메타 입력(product_type/product/lot_id/revision/**PIN 4자리**), `_last_upload` 에 프리필 저장.
2. **업로드 전처리** — `report_flow.prepare_upload_xlsx(path)` → `(sheet_grids, issue_imgs)`:
   - Excel COM 으로 DRM/일반 xlsx 를 열어 `summary`/`yield`/`issue_table` 셀값을
     `{시트: {"origin":[r0,c0], "values":[[...]]}}` grid 로 추출한다 (xlsx 재구성 없음).
   - `issue_table` 임베드 이미지는 `issue_img_<row>` multipart 필드로 보낼 bytes 로 추출한다.
   - COM 추출 실패 시 안내 `ValueError` (Excel 필요).
3. **차트 렌더** — `chart_export.export_chart_pngs(path)`:
   - `win32com.client.DispatchEx("Excel.Application")`, `ReadOnly=True, UpdateLinks=0`(사용자가 같은 파일 열어둬도 락/프롬프트 회피).
   - 워크시트 임베드 차트(`ChartObjects`) → 차트 시트(`Charts`) 순으로 `chart.Export(png)`. PNG 매직바이트 검증.
   - **그레이스풀**: pywin32/Excel 미설치·실패 시 `[]` 반환 → xlsx 만 업로드. CoInitialize/Quit/임시폴더 정리 finally 보장.
4. **전송** — `uploader.post_grids(sheet_grids, file_name, product_type, product, lot_id, password, issue_imgs, progress_cb)`:
   - `POST {SERVER_BASE_URL}/pe/report/upload_xlsx`, multipart (`requests_toolbelt.MultipartEncoder` + `MultipartEncoderMonitor`).
   - data: `sheet_grids`(JSON 문자열) + `file_name` + `product_type/product/lot_id/revision/process/edm_link/password`.
     files: `issue_img_<row>`(PNG). **xlsx 파일은 보내지 않는다.**
   - `progress_cb(bytes_read, total_bytes)` — 전송 바이트 진행률 콜백(옵션). [05 UI `_do_upload`](05_client_ui.md) 가 큐로 받아 진행바 %를 갱신.
   - `resp.ok` 아니면 `RuntimeError(detail)`. 성공 시 `resp.json()`.
5. 결과 메시지박스 — `session_id`, `issue_images_saved`, 브라우저 확인 링크(`/pe/report/view/<sid>`).

## 계약 (서버와 짝)
- 필드명/순서가 서버 [upload_xlsx.py `_collect_chart_pngs`](../server/upload_xlsx.py#L37)(`chart_0..chart_49`, 최대 50)와 일치해야 함. 바꾸면 양쪽 동시 수정.
- PIN 은 여기서 평문 전송 → 서버가 `report_session.password` 에 저장(접근제어용, analysis_key 불포함). HTTPS 아니면 평문 노출 주의.
- `SERVER_BASE_URL` = `HONEY_SERVER_URL` env 또는 `http://127.0.0.1:8000` ([config.py](../client/config.py#L9)). `REQUEST_TIMEOUT_SEC=30`.

## 주의
- **report generator 산출물은 .xlsx 1개**. 클라이언트는 하나의 파일에서 모든 것을 관리하는 정책이므로, 분석 결과물 xlsx 는 단일 파일로만 존재해야 한다.
- 차트 렌더는 **클라 책임**(서버 헤드리스). [06 xlsx_writer](06_analysis_engine.md) 가 만든 네이티브 Excel CDF 차트를 그대로 PNG 화 → 서버는 S3 갤러리로 저장 후 [02 `chart_image`](02_server_query_edit.md) 가 서빙.
- 차트 렌더 실패는 업로드를 막지 않는다(빈 리스트). 분석 없이 임의 xlsx 직접 업로드(`on_upload_local`)도 같은 경로.

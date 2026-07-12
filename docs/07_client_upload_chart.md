# 07 · 클라이언트 — 업로드 전송 (xlsx grid)

> 생성/선택한 xlsx 에서 추출한 grid 를 서버로 보내는 마지막 단계.
> 트리거는 [05 UI](05_client_ui.md), 받는 쪽은 [01 서버 업로드](01_server_upload.md).
> web_report honeyform parquet 전송은 별도 흐름 → [10](10_web_report_pipeline.md).
> **client/ 수정은 사전 승인 필요** ([../CLAUDE.md](../CLAUDE.md) §5).

## 파일
- [client/transport/uploader.py](../client/transport/uploader.py) — `post_grids` multipart POST (grid JSON + issue PNG)
- [client/report_flow/upload_prepare.py](../client/report_flow/upload_prepare.py) — Excel COM 으로 시트 grid + issue image 추출
- 호출 지점: [honey_main.py `_do_upload`](../client/honey_main.py)

## 흐름 (`_do_upload` → 두 모듈)
1. `UploadDialog` 로 메타 입력(product_type/product/lot_id/revision, password 선택), `_last_upload` 에 프리필 저장.
2. **업로드 전처리** — `report_flow.prepare_upload_xlsx(path)` → `(sheet_grids, issue_imgs)`:
   - Excel COM 으로 DRM/일반 xlsx 를 열어 `summary`/`yield`/`issue_table` 셀값을
     `{시트: {"origin":[r0,c0], "values":[[...]]}}` grid 로 추출한다 (xlsx 재구성 없음).
   - `issue_table` 임베드 이미지는 `issue_img_<row>` multipart 필드로 보낼 bytes 로 추출한다.
   - COM 추출 실패 시 안내 `ValueError` (Excel 필요).
3. **전송** — `uploader.post_grids(sheet_grids, file_name, product_type, product, lot_id, password, issue_imgs, progress_cb)`:
   - `POST {SERVER_BASE_URL}/pe/report/upload_xlsx`, multipart (`requests_toolbelt.MultipartEncoder` + `MultipartEncoderMonitor`).
   - data: `sheet_grids`(JSON 문자열) + `file_name` + `product_type/product/lot_id/revision/process/edm_link/password`.
     files: `issue_img_<row>`(PNG). **xlsx 파일은 보내지 않는다.**
   - `progress_cb(bytes_read, total_bytes)` — 전송 바이트 진행률 콜백(옵션). [05 UI `_do_upload`](05_client_ui.md) 가 큐로 받아 진행바 %를 갱신.
   - `resp.ok` 아니면 `RuntimeError(detail)`. 성공 시 `resp.json()`.
4. 결과 메시지박스 — `session_id`, `issue_images_saved`, 브라우저 확인 링크(`/pe/report/view/<sid>`).

## 계약 (서버와 짝)
- password 는 선택 입력 — 보내면 서버가 `report_session.password` 에 저장하지만
  **접근제어에는 미사용**(신원=HoneyUser UA, →[02](02_server_query_edit.md)), analysis_key
  불포함. HTTPS 아니면 평문 노출 주의.
- `SERVER_BASE_URL` = `HONEY_SERVER_URL` env 또는 `http://127.0.0.1:8000` ([config.py](../client/config.py)). `REQUEST_TIMEOUT_SEC=30`.

## 주의
- **report generator 산출물은 .xlsx 1개** — 하나의 파일에서 모든 것을 관리하는 정책.
- 분석 없이 임의 xlsx 직접 업로드(`on_upload_local`)도 같은 경로.

# 07 · 클라이언트 — 업로드 전송 (xlsx grid)

> 생성/선택한 xlsx 에서 추출한 grid 를 서버로 보내는 마지막 단계.
> 트리거는 [05 UI](05_client_ui.md), 받는 쪽은 [01 서버 업로드](01_server_upload.md).
> web_report honeyform parquet 전송은 별도 흐름 → [10](10_web_report_pipeline.md).
> **client/ 수정은 사전 승인 필요** ([../CLAUDE.md](../CLAUDE.md) §5).

> ⚠️ **현황(2026-08): 이 경로는 클라에서 호출되지 않는다.** `post_grids` ·
> `prepare_upload_xlsx` 는 `report_flow/__init__.py` 재노출 외에 client 안 어디서도 불리지
> 않는다(전 repo grep 확인). 사용자가 xlsx 를 직접 올리는 📤 Excel Upload 는 지금
> `report_flow.prepare_report_webreport` → `post_webreport`(honeyform parquet) 를 탄다 →
> [10](10_web_report_pipeline.md). 서버 라우트는 살아 있으므로 아래 계약은 그대로 유효하다.

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
- **password 는 폐지됐다**(2026-08-14) — 폼 필드는 남아 있으나 서버가 형식만 보고 값을
  버린다(`password = None`). 접근제어는 신원(HoneyUser UA, →[02](02_server_query_edit.md)),
  analysis_key 에도 불포함. web_report 업로드 다이얼로그는 아예 `show_password=False` 다.
- `SERVER_BASE_URL` ([transport/config.py](../client/transport/config.py)) 우선순위: `HONEY_SERVER_URL`
  env(임시 override) → **env 파일의 `SERVER_BASE_URL`**(개발=repo `server/env/server.env`,
  빌드본=exe 옆 `honey.env` — build_zip 이 server.env 에서 생성) → 하드코딩 폴백
  `http://12.81.220.117:8080`. 주소 변경은 server.env 1곳만 고친다. `REQUEST_TIMEOUT_SEC=(10, 300)`.
  ⚠️ **개발 PC 함정**: 사용자 환경변수 `HONEY_SERVER_URL`(운영 주소)이 `honey.env` 를
  이긴다 — 로컬 서버 테스트가 "아무 일도 안 일어남"으로 조용히 실패한다.

## 전송 계층 불변 규칙 (두 업로드 흐름 공통)

- **POST(업로드)는 자동 재시도하지 않는다.** [transport/retry.py](../client/transport/retry.py)
  의 `get_with_retry` 는 **GET 전용**이다(RETRIES=2, 1.5s→3.0s). 업로드는 비멱등이라
  자동 재시도하면 중복 세션이 생긴다. 실패 안내문이 "다시 올리기 전에 검색결과 목록을 먼저
  확인" 으로 고정된 것도 같은 이유다.
- **업로드 타임아웃은 서버 대기 상한보다 커야 한다.** 클라
  `WEBREPORT_UPLOAD_TIMEOUT_SEC=(10, 200)` > 서버 `WEB_REPORT_UPLOAD_WAIT_SEC=90`
  ([server/README.md](../server/README.md)). 뒤집히면 서버가 정상 처리 중인데 클라만 끊겨
  사용자에게는 "100% 에서 멈춤" 으로 보인다.
- **UA 토큰**: `HoneyUser/<percent-encoded 계정> HoneyVer/<CURRENT_VERSION>` →
  [05 §UA 토큰](05_client_ui.md).

## 주의
- **report generator 산출물은 .xlsx 1개** — 하나의 파일에서 모든 것을 관리하는 정책.
- 분석 없이 임의 xlsx 직접 업로드(`on_upload_local`)는 **더 이상 이 경로가 아니다** —
  `report_xlsx_ingest` 로 honeyform 을 복원해 web_report 세션으로 올린다([10](10_web_report_pipeline.md)).

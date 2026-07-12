# 02 · 서버 — 조회 · 접근제어 · 삭제 · 주석

> 브라우저·Honey 가 세션을 열람/편집/삭제하는 라우트와 **접근제어 계층**.
> 관련: 데이터 생성 [01 업로드](01_server_upload.md) · web_report 데이터/편집
> [11](11_web_report_tabs.md) · 저장 구조 [03](03_storage.md) · 전체 API 표
> [server/README.md](../server/README.md)(정본)

## 파일 (라우트 4분할)
`report_routes.py` 는 집결자이고 구현은 아래로 나뉜다:
- [security.py](../server/report/security.py) — CSRF·신원 가드·입력 검증·감사 헬퍼
- [routes_session.py](../server/report/routes_session.py) — 세션 조회/삭제/권한·편집자 위임
- [routes_webreport.py](../server/report/routes_webreport.py) — web_report 데이터/편집 (→[11](11_web_report_tabs.md))
- [routes_misc.py](../server/report/routes_misc.py) — 페이지·history·주석·favorites·auth 스텁·정적
- DB 접근은 [database/report_db.py](../server/database/report_db.py), S3 조회는 [storage_gateway](../server/storage_gateway/)

## 접근제어 (핵심)
신원은 provider 체인([auth_identity.py](../server/auth_identity.py))에서 온다:
`current_user()` = SSO 헤더(`AUTH_SSO_HEADER` 설정 시) → Honey UA `HoneyUser/<계정>`. **일반
브라우저는 신원이 없어 읽기 전용.**

가드 2단계 ([security.py](../server/report/security.py)):
- **`_uploader_guard`** — 삭제·비공개·편집자 부여. 신원 없으면 401, 업로더 아니면 403.
- **`_editor_guard`** — 콘텐츠 편집·개인 중요표시. 업로더 본인 또는 위임 편집자
  (`report_session_editor`)면 통과.

**legacy 우회 (중요)**: `is_uploader()` 는 `session["uploaded_by"]` 가 **비어 있으면 신원만
있으면 True**. xlsx 업로드 세션은 `uploaded_by` 를 채우지 않으므로 Honey 접속 사용자 전원이
편집/삭제할 수 있고, `uploaded_by` 를 채우는 web_report 세션만 업로더 잠금이 실효한다.

**password(구 PIN)는 접근제어에 미사용**: `_password_ok()` 는 정의만 있고 **호출 지점이
없다(dead code)**. `verify_password` 라우트는 UA 업로더 확인만 하는 하위호환 스텁으로 항상
`has_password:false` 를 반환한다.

**CSRF**: 쿠키 세션이 없으므로 double-submit 쿠키(`report_csrf` ↔ `X-CSRF-Token`)로 브라우저
변경요청을 방어한다. Honey 클라 전용 업로드(`/upload_xlsx`)는 브라우저가 아니라 제외
(`X-Honey-Agent` 헤더 구분).

## 주요 흐름
### 검색 목록 — `history()`
쿼리스트링(`product_type/process/product/revision/lot_id/source`) → `get_history()`.
`status IN ('done','reused')` 만, `lot_id` 는 LIKE, `password` 는 노출 안 하고 `has_password`
불린만, CSV 합계 크기 LEFT JOIN. (→[03](03_storage.md))

### 세션 상세 복원 — `session_full()`
1. `get_session` → 없으면 404. `_public_session`(password 제거, has_password 추가).
2. `get_all_object_infos(akey)` → object 맵.
3. legacy `summary_text`/`issue_table_text` object 있으면 본문 로드(예외 시 None). 현행 텍스트는
   DB `report_sheet_data`.
4. `chart_index` 있으면 `charts = [{index, url:/pe/report/chart/<sid>/<i>}]`.
5. 응답: session·summary·summary_text·issue_table_text·charts·csv_files·objects·annotations.
   web_report 세션은 프런트가 이어서 `/full`·탭별 라우트로 데이터를 받는다.

### 편집자 위임
업로더가 `POST /session/<sid>/editors` 로 다른 PC 계정에 편집 권한을 준다. 후보는
`report_web_visitor` 풀(`/editors/candidates`)에서 고른다. 위임받은 계정은 `_editor_guard`
통과(콘텐츠 편집 가능, 삭제·비공개는 여전히 업로더 전용).

### 수정 저장 — `update_session_content()` (비활성, 항상 405)
구 xlsx 텍스트 수정 기능은 차단됐다. report_view.html 이 아직 이 경로를 호출하므로 405
스텁으로만 유지. 재활성화 시 git 히스토리 참조.

### 차트 이미지
`/chart/<sid>/<idx>` → `storage_gateway.load_chart_png` → `Response(image/png,
Cache-Control private)`. 공개버킷/presign 없이 **서버 경유** 서빙.

## 주의
- 삭제(`delete_session_route`)는 업로더만. `report_annotation` 까지 지우고, akey 최종 참조일
  때 `storage_gateway.delete_report_artifacts` 로 산출물·캐시 폴더도 정리한다.
- 분석/플롯 라우트는 여기 추가 금지 (CLAUDE.md §5).
- 상세 API 표(경로·메서드·접근 수준 전체)는 [server/README.md](../server/README.md) 가 정본.

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
- [routes_misc.py](../server/report/routes_misc.py) — 페이지·history·주석·favorites·auth(로그인/회원가입)·정적
- DB 접근은 [database/report_db.py](../server/database/report_db.py), S3 조회는 [storage_gateway](../server/storage_gateway/)

## 접근제어 (핵심)
신원은 provider 체인([auth_identity.py](../server/auth_identity.py))에서 온다:
`current_user()` = SSO 헤더(`AUTH_SSO_HEADER` 설정 시) → Honey UA `HoneyUser/<계정>` →
웹 로그인 세션. **셋 다 없는 일반 브라우저는 읽기 전용.**

**신원 키 정규화 (2026-08-14)** — provider 3개가 모두
[identity_norm.normalize_uid](..\server\identity_norm.py) 를 통과한다: *마지막 백슬래시 뒤 →
trim → 소문자*. `SECDS\Chumji.Kim`·`Chumji.Kim`·`chumji.kim` 은 한 사람이다. 정규화가 빠진
경로가 하나라도 있으면 그 사람의 접속 통계·즐겨찾기·편집 권한이 갈라져 관리자 사용자
현황에 여러 명으로 나온다. 사람 ID 를 저장하거나 화면에 그리는 코드를 새로 쓸 때는
반드시 이 함수를 거칠 것 — 파이썬은 `normalize_uid`, JS 는 `UserName.uid()`(정적 페이지)
또는 `normUid()`(관리자 패널), SQL 은 `_UPLOADER_MATCH`(sessions.py)·`_NORM_CLIENT_USER`
(admin_panel/stats.py) 표현을 쓴다. 규칙 이전에 쌓인 행은
[tools/merge_duplicate_users.py](..\server\tools\merge_duplicate_users.py) 로 합친다.
단 **감사로그 `client_user` 와 세션 `uploaded_by` 는 원문을 보존**한다(증거·소유 근거) —
화면 표기는 조회 시점에 정규화한다.

**웹 로그인 계정(`report_user`, singleID + 비밀번호 4자리)** — 라우트는
[routes_misc.py](../server/report/routes_misc.py) 의 auth 블록. 계정 생성 경로는 2개:
- `/api/auth/set_password` — **Honey 접속 전용**(`identity_source()=="honey"`). 실행 자체가
  본인확인이라 기존 계정의 비밀번호 재설정도 이 경로(또는 관리자 초기화)뿐이다.
- `/api/auth/signup` — **웹 회원가입**(2026-07-23). 브라우저는 PC 계정/호스트명을 알 수 없고
  서버에 AD·메일 연동도 없어 자동 검증 수단이 없다 → **Honey 사용 이력이 없는 미사용
  singleID 만** 자유 가입시킨다: 이미 계정이 있으면 409, `has_honey_history()`(업로드
  `uploaded_by` 꼬리 일치 또는 `report_web_visitor` 방문 기록)면 403. 활동 중인 사용자의
  계정 선점을 막고, 그 사람은 Honey 에서 설정하면 된다. 가입 즉시 로그인 세션을 준다.
  IP 당 5회/1시간(프로세스 메모리) + 감사 `signup` 행 기록. 잘못 선점된 계정 회수는
  관리자 패널 계정 탭 삭제.
- `/api/auth/signup_hint` — 가입 창 ID 자동완성. **요청자 자신의 IP** 로만 최근 180일 `upload`
  감사기록의 계정 1건을 돌려준다(타인 IP 열거 불가). 힌트 출처가 Honey 업로드라 잡히는 계정은
  대개 위 403 대상 — 프런트는 "Honey 앱에서 설정" 안내를 함께 띄운다. **신원 판단에는 미사용**
  (공유 PC·DHCP 재할당에서 어긋남).

가드 3종 ([security.py](../server/report/security.py)):
- **`_uploader_guard`** — 삭제·비공개 토글·편집자 부여. 신원 없으면 401, 업로더 아니면 403.
- **`_editor_guard`** — 콘텐츠 편집·개인 중요표시. 업로더 본인 또는 위임 편집자
  (`report_session_editor`)면 통과.
- **master PC 는 위 두 가드를 모두 통과한다** (2026-08-10 — 종전엔 `_editor_guard` 만).
  admin 로그인한 PC(서명된 `pe_master_gate` 쿠키, 4h)는 Honey 신원이 없어도 업로더와 동일
  권한이다 — 편집뿐 아니라 삭제·비공개 토글·편집자 부여까지. 프런트도 `is_master` 를
  `IS_UPLOADER` 에 합류시켜 해당 버튼을 노출한다([core.js](../server/report/static/webreport/core.js)).
  검색결과 목록의 🔒/🗑 버튼도 같은 규칙이다(`canModify` — 2026-08-19까지는 여기만 빠져
  있어 **서버는 허용하는데 버튼이 안 보였다**. 가드에 master 를 더할 땐 그 권한을 여는
  화면도 함께 확인할 것).
- **master 의 Honey 헤더 면제** (2026-08-19) — `PATCH /session/<sid>/meta` 는 원래
  `X-Honey-Agent` 를 요구해 "메타 수정은 Honey 에서만" 을 강제한다. 관리자가 남의 세션을
  바로잡으려고 그 사람의 Honey 를 빌려야 했으므로 master 만 면제하되, 헤더가 없는 만큼
  **CSRF 로 대체**한다(둘 중 하나는 반드시 통과 — 아무것도 없으면 브라우저 폼으로 위조
  가능해진다). 웹 편집 폼은 세션 상세 ✏️ → `metaEditModal`
  ([edit_mode.js](../server/report/static/webreport/edit_mode.js) `openMetaEdit`)이며
  Honey 안에서는 종전대로 앱 편집창이 뜬다. Family 선택지는 eval taxonomy 정본을 주는
  `GET /api/family_products` 에서 받는다.
- **`_private_guard`** (2026-07-15) — **비공개(is_private) 세션 조회 차단**. 업로더 본인 또는
  위임 편집자가 아니면 **404**(존재 자체를 숨김 — 편집 가드의 401/403 과 달리 조회는 404).
  적용 지점: `/result`·`/session/<sid>`·`/full`·`/view`·`/annotation/<sid>`(각 라우트),
  web_report 데이터 API 전체(`_require_web_report_session` 공통 진입점), 이미지 라우트
  (`storage_gateway/routes.py` — chart/issue_image/note_image/distribution_combined).
  목록(history)은 SQL 필터([sessions.py](../server/database/sessions.py) `_history_where`
  `viewer` 파라미터)로 숨긴다 — 공개 OR legacy(uploaded_by 빈 세션) OR 업로더 OR 위임 편집자.
  Honey 클라의 Excel Download/Rawdata 수정도 이 가드를 지나므로 plain requests 에
  `HoneyUser/` UA 토큰을 붙인다([client/excel_download/_fetch.py](../client/excel_download/_fetch.py)·
  [client/excel_edit/excel_session.py](../client/excel_edit/excel_session.py) `_honey_headers()`).

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
통과(콘텐츠 편집 가능, 삭제·비공개는 여전히 업로더 전용 — 단 master PC 는 예외).

### 세션 메타 수정 — `PATCH /session/<sid>/meta` (Honey 편집창)
세션 상세 우상단 ✏️ → **Honey 앱**의 편집창(업로드 다이얼로그 재사용, `SessionMetaDialog`)
→ 이 라우트. 바꾸는 값은 `file_name`(= 상단바 `Session_name` = 검색결과 목록 파일명 칸) ·
`family_product` · `product` · `lot_id` · `process` 다섯이다.

- **웹 → 클라 브리지**: 웹 버튼은 `/pe/report/honey/session_meta/<sid>` 로 *이동*을 시도하고,
  Honey 내장 브라우저가 `_browser_leave_guard`(honey_main)에서 그 이동을 취소한 뒤 편집창을
  띄운다 — 별도 통신 채널이 없다. 일반 브라우저에서는 `HoneyHint` 안내 모달만 뜬다.
- 서버는 `X-Honey-Agent: 1` 을 **요구**해 "수정은 Honey 에서만" 을 강제한다(CSRF 대체 —
  `rawdata_replace` 선례). 권한은 `_editor_guard`(업로더 + 위임 편집자 + master).
- `product` 가 바뀌면 `product_info.lookup()` 을 다시 돌려 세션 기준정보 14컬럼을 갱신하고,
  **미등록 Part ID 면 비운다**(옛 제품의 WF Size/Gross Die 가 남으면 상단바가 틀린 정보를
  보여준다).
- `product_type` 은 편집 대상이 아니고, **`analysis_key` 는 재산출하지 않는다** — 산출물이
  전부 그 키로 저장돼 있어 키를 바꾸면 세션이 자기 데이터를 잃는다. 규칙 #3 의 산출식은
  '업로드 시점' 규약이며, 수정 후에는 dedup(같은 데이터 재업로드) 매칭만 어긋난다.
- `/full` 응답 캐시는 키의 `extras_digest` 에 세션 행이 통째로 들어가 **자동 무효화**된다.
- 계약 테스트: [tests/test_session_meta.py](../tests/test_session_meta.py).

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

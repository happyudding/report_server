# COINAPI report_server — Claude Code 진입점



> **세션 시작 규칙**: 새 대화가 시작될 때마다 [docs/INDEX.md](docs/INDEX.md)를
> 먼저 읽어라. 기능별 코드 흐름·파일 위치·불변 규칙이 모두 INDEX에 있다.

**주의사항** : service 중인 server 이기 때문에 코드 변경하였을때 기존 session 이 안열리거나 지장을 주면안됨. 그렇게 되면 사용자에게 반드시 코드 변경을 하기 전에 진짜 변경할건지 확인 질문을 하라.

이 프로젝트는 Honey 클라이언트가 추출한 산출물을 서버로 업로드하고, Flask 서버가
SQLite + S3(또는 로컬 폴백)에 세션 단위로 저장한 뒤 검색결과·세션 상세 페이지로 조회하게
한다. 두 업로드 흐름이 병행한다: **xlsx 추출 grid**(→[docs/01](docs/01_server_upload.md))와
**web_report honeyform parquet**(→[docs/10](docs/10_web_report_pipeline.md), 현재 신규 개발의
주 대상).

**기능별 코드 흐름 추적은 [docs/INDEX.md](docs/INDEX.md) 참조** — 큰 기능별로 각 흐름을
정리한 작업용 메모. 무엇을 고칠지 정해지면 INDEX 표에서 해당 문서 1개만 열면 된다.

**소유권 / 수정 권한 경계** — 정본은 [docs/15_ownership.md](docs/15_ownership.md). 이 문서는
이 프로젝트(웹리포트/서버) 관점이다. 외부 담당자 영역 소유자용 진입 문서는
[CLAUDE.core.md](CLAUDE.core.md). 3-tier 요약:
- 🟢 **자유 수정** (승인 없이 바로): `web_report/` + web_report 관련 html(report_view.html,
  static/webreport/) + `server/`(단 `storage_gateway/` **제외**) + `eval_analyzer/`
  (이 repo 가 원본 — 2026-08-03 승격) + client 자주 쓰는 영역
  (`honey_ui/`, `honey_main.py`, `transport/`, `excel_download/`, `excel_edit/`).
- 🟡 **사전 승인**: `client/` 나머지 비동결 (report_flow/, map_report/, embedded_browser.py,
  client_identity.py, config.py 등) — 편집 전 파일·이유·영향 설명.
- 🔒 **외부 담당자 영역** (건들 때마다 승인): `d1/`·`d1_storage/`·`client/honey_parse/`·
  `client/report_generator/`·`server/storage_gateway/`(facade+`_s3` 전체). 병합돼 들어왔으나
  외부 담당자 소유·교체 대상이라 동결(수정 불가피 시 명시 승인).
  진입점·유지 계약은 [docs/INDEX.md §3.1](docs/INDEX.md).
  (`eval_analyzer/` 는 **더 이상 동결이 아니다** — 자유 수정. 단 import 단방향 규칙 #8 은 유지.)

---

## 0. 디렉토리 인덱스

```
report_server/
├── server/                     Flask 서버 (자유 수정 — 단 storage_gateway 는 외부 담당자·동결. [server/README.md](server/README.md))
│   ├── wsgi.py                  진입점 (컴퓨트 워커 __mp_main__ 재임포트 스킵 가드)
│   ├── plugin.py                register_report_server() — Blueprint 3개 + admin_panel + ops 등록
│   ├── config.py                환경변수·경로 통합 설정
│   ├── auth_identity.py         신원 provider 체인 (기본 HoneyUser UA, AUTH_SSO_HEADER 로 SSO 전환)
│   ├── identity_norm.py         사람 식별 키 정규화 정본 (`SECDS\Chumji.Kim`→`chumji.kim`)
│   ├── diagnostics.py           진단 사건 저장소 — 서버 500/503·느린 요청·콜드 빌드 실패·
│   │                            브라우저/Honey 오류를 상관 ID(request/operation/build/session)
│   │                            로 이어 `server/log/diagnostic_*.log` JSONL 로 모은다.
│   │                            **DB 를 쓰지 않는다**(에러 순간이 곧 DB 가 잠기는 순간)
│   ├── database/                SQLite 계층 (report_db.py 는 재노출 facade — 호출부 무변경)
│   │   ├── core.py              SCHEMA(정본)·마이그레이션·get_conn·analysis lock
│   │   ├── sessions.py / objects.py / audit.py / users.py / annotations.py / usage.py
│   │   ├── webreport_edits.py   web_report 편집 상태 (세션 단위)
│   │   └── models.py            Session dataclass (Mapping 호환)
│   ├── report/
│   │   ├── report_extension.py  report_bp 정의 + DB init + web_report 저장 포트 주입
│   │   ├── report_routes.py     라우트 집결자 — 구현은 security.py /
│   │   │                        routes_session.py / routes_webreport.py / routes_misc.py
│   │   ├── static/webreport/    JS 모듈 32개 — 세션 상세가 31개를 순서 로드(전역 공유),
│   │   │                        old_client_notice.js 만 랜딩·검색결과 전용
│   │   │                        (로드 순서 정본 = [docs/11](docs/11_web_report_tabs.md))
│   │   ├── report_analysis_index.html  검색결과 페이지
│   │   └── report_view.html     세션 상세 (마크업+CSS — JS 는 static/webreport/)
│   ├── storage_gateway/         S3 산출물 저장 단일 진입점 (외부 담당자·동결 — facade+_s3 전체, 진입점 계약 유지)
│   │   ├── __init__.py          facade (공개 API + 예외 재노출 + 저장 위치 기록)
│   │   ├── routes.py            이미지 URL 라우트
│   │   ├── _s3.py              boto3 호환 client + key 빌더 (내부 어댑터)
│   │   ├── _issue_images.py    이슈 이미지 백엔드 (S3+로컬 폴백)
│   │   └── _note_images.py     Note 탭 이미지 백엔드 (S3+로컬 폴백, 세션 단위)
│   ├── chatbot/                 ENGR 이력 검색 챗봇 엔진 ([README](server/chatbot/README.md)) — CLI 와
│   │                            웹 위젯이 공유. report.db(세션·이슈)+eval.db(item 축) read-only
│   │                            + web_report 계산값(수율/CPK/측정값, tools_metrics).
│   │                            **패키지 자체는 라우트 미등록** — 웹 노출은 report/routes_chat.py
│   │                            하나뿐(master 전용 404 가드·세마포어 3·질문/답변 기록)
│   ├── landing/                 /pe 랜딩(서버 첫 화면) — 제품군 바로가기·Honey 다운로드·현황
│   │                            수치. HTML 1장(canvas 배경 인라인) + blueprint 1개.
│   │                            데이터는 report_bp 의 GET /pe/report/api/landing 하나
│   ├── admin_panel/             /pe/admin-<secret>/ 대시보드 + metrics 샘플러
│   │                            (🚨 진단 사건 탭 = diagnostics_admin.py — 사건 타임라인·
│   │                            콜드 빌드 마지막 단계·증거 기반 원인 안내)
│   ├── eval_panel/              /pe/eval 룰 관리 (thresholds·signature **둘 다** 제품군/family
│   │                            오버레이(전역 범위 선택기 공유) · L0~L6 트레이스 + **전후 비교**
│   │                            · 골든셋 회귀 — 저장 즉시 반영, rev 낙관적 잠금·no-op 스킵
│   │                            · **표본함**(룰당 8건만 검수 → 승인형 임계값 강화안, docs/13 §14))
│   ├── tools/migrate_manifest_edits.py  manifest 편집값 → 세션 편집 DB 이전 (운영 1회 실행 완료)
│   ├── upload_xlsx.py           POST /pe/report/upload_xlsx
│   ├── upload_webreport.py      POST /pe/report/upload_webreport (web_report.ingest 호출)
│   ├── xlsx_parser.py           시트 grid → 텍스트 추출 (_GridSheet 셸, openpyxl 미사용)
│   ├── admin_routes.py          [미등록 dead file — 구 /pe/admin, admin_panel 로 흡수됨]
│   ├── honey_routes.py          /honey/version, /honey/download, /honey/announcement
│   └── releases/                version.json(배포 manifest) + announcement.txt(업데이트 공지 원문)
├── web_report/                 웹 리포트 구현 (신규 개발 주 대상 — [web_report/CLAUDE.md](web_report/CLAUDE.md),
│                                docs/[10](docs/10_web_report_pipeline.md)·[11](docs/11_web_report_tabs.md)·[12](docs/12_web_report_cache.md))
├── client/                     Honey 클라이언트 (PyQt6 — honey_ui/honey_main/transport/excel_* 자유, 나머지 사전 승인; report_generator·honey_parse 만 외부 담당자·동결)
│   ├── honey_main.py            메인 윈도우 + 워크플로
│   ├── transport/              uploader / version_check / updater / retry
│   ├── report_flow/            upload_prepare.py (Excel COM 추출) 등
│   ├── report_generator/       (외부 담당자·동결) 분석·xlsx 생성 — 외부 담당자 소유
│   ├── honey_parse/            (외부 담당자·동결) file_to_df 파서 — 현재 더미 폴백
│   ├── map_report/             (사전 승인·신규) 웨이퍼 bin map 렌더 + xlsx 부착 (report_generator 밖으로 분리 → [docs/14](docs/14_merge_order.md))
│   ├── honey_ui/               다이얼로그·위젯
│   ├── excel_download/ · excel_edit/   Excel COM 헬퍼
│   ├── embedded_browser.py     HoneyUser UA 삽입 내장 브라우저
│   ├── client_identity.py      PC 계정/호스트 신고값
│   └── config.py               SERVER_BASE_URL, CURRENT_VERSION
├── eval_analyzer/              독립 fail-item 평가 엔진 (자유 수정 — **이 repo 가 원본**, 외부 사본 동기화 없음)
│                                서버 연결은 web_report/ai_comment.py + eval_export.py + eval_debug.py
│                                3곳만 → [docs/13](docs/13_eval_analyzer_integration.md)
├── d1/                         (외부 담당자·동결) D1 입력 provider 경계 — 검증용(로컬 d1_storage 검색)
├── d1_storage/                 (외부 담당자·동결) D1 로컬 검증 스토리지
├── tools/product_info_import/  기준정보 CSV(DRM) → product_info.db 오프라인 임포터
│                                (standalone — Excel 있는 별도 PC 에서 실행 후 .db 를 서버로 수동 복사)
├── tools/eval_golden/          eval 룰 골든셋 회귀 (golden.yaml 기대 발화 vs 실제 트레이스 diff
│                                → [docs/13 §12](docs/13_eval_analyzer_integration.md))
├── tests/sample_xlsx.py         더미 grids 픽스처 생성기
├── DB/pe/report/                런타임 자동 생성 (report.db + backup/ + 문서 스냅샷)
├── uploads/                     런타임 (업로드/로컬 폴백/디스크 캐시 루트)
└── docs/                       기능별 흐름 문서 (INDEX.md 가 허브)
```

---

## 1. 데이터 흐름

**Honey → Server (2개 병행 업로드 흐름)**
1. Honey 시작 → `GET /honey/version` → 새 버전 있으면 사용자에게 확인 후 `/honey/download`
2. **xlsx 흐름**: product_type / product / lot_id (+ password 선택) + xlsx 선택. 클라가
   **Excel COM 으로 DRM 해제·시트 셀값을 읽어** summary/yield/issue_table grid(2D 배열)와
   issue_table 행별 PNG 를 추출 → `POST /pe/report/upload_xlsx` (xlsx 파일은 보내지 않고
   `sheet_grids` JSON + `issue_img_<row>` 만 전송). 서버: `sha256(canonical(sheet_grids) +
   meta)` → analysis_key → 세션 생성 → grid 파싱 → yield_rows·sheet_data DB 저장, issue PNG
   는 S3(로컬 폴백). **원본 xlsx 는 저장하지 않는다.** 상세 [docs/01](docs/01_server_upload.md).
   - **web_report 흐름**: honeyform parquet + manifest → `POST /pe/report/upload_webreport`.
     parquet 소스는 **`honey_parse.file_to_df` 가 돌려준 7-meta honeyform(`md.df`) 그대로**다 —
     원본 입력 파일을 디스크에서 다시 읽지 않는다(병합이 honey_parse 안에서 일어나므로 원본을
     재-read 하면 병합 결과를 버린다). 상세 [docs/10](docs/10_web_report_pipeline.md).

**검색결과 조회 / 편집**
- `GET /pe/report/` → 검색결과 페이지, `GET /pe/report/api/history?product_type=MDDI&...` → 세션 목록
- `GET /pe/report/view/<session_id>` → 세션 상세
- `GET /pe/report/session/<sid>/full` → 세션 + summary + objects + annotations + 추출 텍스트
  (응답 session 에서 password 제거, `has_password` 불린만 노출)
- `POST /pe/report/session/<sid>/verify_password` → **하위호환 스텁** (HoneyUser 신원==업로더
  확인만, 항상 `has_password:false` — 구 PIN 검사 폐지)
- `PATCH /pe/report/session/<sid>/meta` → 세션 메타(이름=file_name·family·product·lot·process)
  수정. 세션 상세 ✏️ → **Honey 편집창**(업로드 다이얼로그 재사용) 전용 — 서버가
  `X-Honey-Agent` 헤더를 요구해 강제한다. **단 master(admin 로그인 4h)는 웹 브라우저에서도
  고칠 수 있다**(2026-08-19 — 헤더 대신 CSRF 요구, 폼은 `metaEditModal`).
  product 변경 시 product_info 재lookup.
  `analysis_key`·`product_type` 은 불변 → [docs/02](docs/02_server_query_edit.md)
- `PATCH /pe/report/session/<sid>/content` → [비활성] 항상 405 (구 xlsx 텍스트 수정 폐기)
- `POST .../session/<sid>/web_report/issue_table/comments|etc`, `.../summary/engr`,
  `.../trim/overrides`, `.../chart_notes`(차트 주석), `.../note`(Note 탭 시트)
  → web_report 편집 — **세션 편집 DB(report_webreport_edit)에 저장**.
  manifest 는 업로드 시점 불변 스냅샷. Note 이미지는 `.../note_image` → storage_gateway
  (세션 단위 저장, 세션 삭제 시 정리).
- `POST .../session/<sid>/web_report/preprocess` → **조회 전처리**(항목 제외·outlier·
  빠른 수정 셀 패치·조건 일괄 규칙 + 수율 분모 기준). **원본 parquet 을 바꾸지 않고** 조회
  시점에만 적용되는 되돌릴 수 있는 편집이라, 원본을 실제로 교체하는 `raw_data/edit`(웹 셀
  편집)·`rawdata_replace`(Excel 왕복)와 성격이 정반대다 → [docs/11](docs/11_web_report_tabs.md).
- `DELETE /pe/report/session/<sid>` → 세션 삭제 (업로더만)

**신원 키는 한 규칙으로 정규화한다** ([identity_norm.py](server/identity_norm.py)
`normalize_uid` — 마지막 백슬래시 뒤 → trim → 소문자). `SECDS\Chumji.Kim`·`Chumji.Kim`·
`chumji.kim` 은 **한 사람**이며, 사람 ID 를 저장하거나 화면에 그리는 모든 코드가 이 함수를
거쳐야 한다(JS 는 `UserName.uid()` / 관리자 패널 `normUid()`). 어느 한 경로만 빠져도 통계·
즐겨찾기·편집 권한이 사람당 여러 벌로 갈라진다. 예외는 **원문 보존**이 목적인 감사로그
`client_user` 와 세션 `uploaded_by` 뿐이다(표기는 조회 시점 정규화). 사용자 실명은
**한글 2~10자만** 받는다(`_DISPLAY_NAME_RE`) → [docs/02](docs/02_server_query_edit.md).

**인증**: 신원은 Honey 내장 브라우저 User-Agent 의 `HoneyUser/<계정>` 토큰으로 자동 식별한다
([server/auth_identity.py](server/auth_identity.py) — env `AUTH_SSO_HEADER` 설정 시 역프록시
SSO 헤더가 우선, 코드 무변경 전환). 일반 브라우저는 신원이 없어 **읽기 전용**. 가드 3종
([server/report/security.py](server/report/security.py)): `_uploader_guard`(삭제·비공개 토글·
편집자 부여 — 업로더 전용, **단 admin 로그인 master PC 는 통과** 2026-08-10) /
`_editor_guard`(콘텐츠 편집 — 업로더 또는 위임 편집자, master PC 포함) /
`_private_guard`(비공개 세션 **조회** 차단 — 업로더+위임 편집자 외 404, 목록도 SQL 필터로
숨김, 2026-07-15). **legacy 우회**:
`uploaded_by` 가 빈 세션(= xlsx 업로드 세션)은 Honey 접속 사용자 전원이 편집/삭제 가능하고,
`uploaded_by` 를 채우는 web_report 세션만 업로더 잠금이 실효한다. 구 4자리 password
(`report_session.password`)는 저장·전송되나 **접근제어에 미사용**(선택 입력, 형식만 검사).
신원/password 는 analysis_key 산출 meta 에 **포함하지 않음** (규칙 #4). 접근제어 상세는
[docs/02](docs/02_server_query_edit.md).

---

## 2. DB 스키마 요약 (현행)

**정본은 [server/database/core.py](server/database/core.py) 의 `SCHEMA`.** 전체 테이블·컬럼은
[docs/03](docs/03_storage.md) 와 스냅샷 [DB/pe/report/report_README.md](DB/pe/report/report_README.md)
참조. 테이블 27개 요지:

- `report_session` — 세션 1건. `source`('xlsx_upload'|'web_report'), `mode`('Normal' 기본),
  `uploaded_by`·`client_host`(신원), `webreport_options` 컬럼 포함. `password`(4자리 PIN)는
  **2026-08-14 폐지** — 신규 저장 중단·기존 값 NULL 처리, 컬럼만 보존하고 `has_password` 는
  항상 false 다(접근제어는 신원 기반).
- `report_analysis` — analysis_key 단위 **물리 원본 상태**(authoritative `content_hash`·
  `source_count`·`artifact_status`). dedup 형제 세션이 각자 hash 를 들고 갈라지던 문제의
  단일 진실. `report_session.content_hash` 는 rollback 대비로 계속 동기화한다.
- `report_session_blob` — 세션 단위 **큰 본문의 포인터**(현재 kind=`note_sheet`). 본문은
  객체 저장(S3 `pe/report_server/session_blob/<sid>/<kind>/<sha256>.json.gz` 또는 로컬
  spool)에 있고 DB 에는 `backend`/`object_key`/`content_hash`/`base_token`/`size_bytes` 만.
  `backend='local_pending'` = S3 업로드 실패로 로컬 보관 중(cleanup 이 재이관, 관리자 경고).
- `report_schema_migration` — backfill 단계·cursor(중단 후 재개용). 부팅 시 대량 작업을
  하지 않으므로 이전은 [tools/migrate_session_db.py](server/tools/migrate_session_db.py) 가 한다.
- `report_chatbot_daily` — 챗봇 원문 보존기간(기본 90일) 후에도 남기는 일별 비식별 집계.
- `report_analysis_summary` — yield/cpk 등 summary 행 (UNIQUE analysis_key,item,bin).
- `report_object_info` — S3/로컬 산출물 위치. `options_json` 에 `{"storage":"s3"|"local"}` 기록.
  `object_type`: `distribution_combined`, `web_report_source_<idx>`, `web_report_manifest`,
  (legacy) `summary_text`/`issue_table_text`/`chart_index` 등.
- `report_sheet_data` — xlsx 추출 텍스트(summary/yield/issue_table). 텍스트는 여기에만 저장.
- `report_audit_log` — upload/edit/delete 감사. 메타 스냅샷 + client_ip/user_agent/client_user
  (클라 신고 계정, 위조 가능) + result. best-effort. `/pe/admin-pte/` 대시보드에서 조회.
- `report_webreport_edit` / `_rev` — web_report 편집(comment/etc/cmp_etc(Issue Table
  Compare 탭 ETC)/trim override/engr/
  chart_note(차트 주석)/compare_note(Compare 탭 행 코멘트)/dist_composite(Distribution
  합성 산포 차트 정의)/gap_chart(사용자 수식 파생 분포 — 토큰 배열)/note_sheet(Note 탭 Luckysheet
  시트 JSON ≤10MB)/preprocess(조회 전처리
  spec — 항목 제외·outlier·셀 패치·조건 규칙)/yield_basis)의 **진실 저장소,
  세션 단위**. dedup(동일 analysis_key) 세션 간 편집 비공유. `rev` 는 단조 증가 캐시
  무효화 토큰. manifest 는 불변 스냅샷 ([web_report/edits.py](web_report/edits.py)).
  ⚠ report payload 계산에 안 쓰이는 kind(chart_note·note_sheet·note_tag·dist_composite·
  gap_chart)는
  `PAYLOAD_NEUTRAL_KINDS`([database/webreport_edits.py](server/database/webreport_edits.py))에
  등재해야 한다 — 빠뜨리면 저장할 때마다 report 전체가 콜드 재빌드된다.
  ⚠️ **note_sheet 본문만 객체 저장으로 나갔다**(2026-08-14) — 저장은 blob+legacy 행
  **dual-write**, 조회는 blob 우선 + legacy 폴백이라 이 표만 보는 기존 코드도 그대로
  동작한다. 낙관적 잠금 base(본문 sha1 16자) 의미는 불변.
- `report_session_editor`(편집 위임) / `report_web_visitor`(편집자 후보 풀) /
  `report_user_important`(개인 중요표시) / `report_user_favorite`(즐겨찾기).
- `report_user_profile` — 사용자 **실명**(표시용). 화면 표기는 전부 `이름(ID)` 이며, 이름이
  없으면 접속할 때마다 입력창이 뜬다(`static/webreport/user_name.js`, 검색결과·랜딩·세션
  상세 공용). `report_user` 의 컬럼이 **아닌** 이유는 로그인 계정이 없는 Honey 전용
  사용자도 이름을 가져야 하기 때문(`password_hash NOT NULL`). 이름은 표시 전용 —
  접근제어·감사 식별은 계속 `user_id` 로 한다.
- `report_usage_daily` / `report_usage_hourly` — 접속 사용량 카운터(Honey 실행·웹 방문).
  `kind`=`honey_run/web_index/web_view`, 무신원은 `ip:<addr>` 행. `record_usage` 가 **두
  테이블에 함께** 기록한다(일별은 날짜 문자열이라 시간대 분포를 복원할 수 없다 — hourly 는
  요일×시간 히트맵용, 2026-08-13 신설).
- `report_client_version` — Honey 클라 **버전 대장**(사람 1명 = 1행, 마지막 실행 버전 +
  prev_version/runs). 입력은 앱 시작 시 1회 오는 `GET /honey/version` 의 UA 토큰
  `HoneyVer/<버전>` 하나뿐이다([database/client_versions.py](server/database/client_versions.py)).
  **행이 없는 사람 = 버전을 안 보내는 구버전 클라**이며, 그게 곧 '업데이트 안 한 사람'
  신호라 조회(`version_report`)는 사용량 기록을 모집단으로 삼아 미상 사용자도 함께 준다.
- `report_usage_peak_daily` — 일별 Peak 동시 접속자(사람) 수. `metrics.active_users()` 는
  메모리에만 있어 이력이 없었다 → 리소스 샘플러(10초)가 그날 최대치만 적재. 값은 **낮아지지
  않는다**(재시작 대비 SQL `MAX`). 관리자 사용자 탭 '📈 접속 추이' 그래프의 소스.
- `report_chatbot_log` — 웹 챗봇 질문/답변 전문 + 부하 계측(`total_ms`=`wait_ms`(동시실행
  대기)+`llm_ms`(질문 해석)+조회). 관리자 Chatbot 탭의 유일한 데이터원. 감사로그와 분리한
  이유는 답변이 수 KB 라 `changed_fields` 1500자 관례에 안 맞고 감사 화면을 밀어내기 때문.
- `report_eval_daily` — eval 룰 엔진 **일별 지표**(2026-08-19). 정확도·커버리지가 지금까지
  "탭 열 때 전체 누적 한 숫자"뿐이라 나아지는지 볼 수 없었다. 원재료는 eval.db(읽기 전용),
  집계는 cleanup 주기(24h)에 편승([database/eval_stats.py](server/database/eval_stats.py)).
  ⚠ **재계산 UPSERT**(덮어쓰기) — 원본이 남아 있어 같은 날을 몇 번 접어도 값이 같아야 한다.
  `report_chatbot_daily` 의 누적 더하기와 규약이 **반대**다. `engine_version` 을 키에 둔
  이유는 룰 버전이 다르면 UNKNOWN 비율·발화 분포가 달라 섞으면 추이가 거짓말을 하기 때문
  (판정과 무관한 집계는 `''`). 화면은 `/pe/eval` 채점 탭.

**보존 정책** (2026-08-14 — 세션 원본·사용자 편집은 **영구**, 운영 로그만 유한 보존):
감사 365일 / 챗봇 원문 90일(이후 `report_chatbot_daily` 집계만) / 시간별 사용량 90일 /
일별 사용량·Peak 730일 / 휴지통 30일. 로그 롤오프는 감사와 같은 이유로 `REPORT_CLEANUP_DRYRUN`
과 무관하게 실행된다(끄면 무한 증가). env 는 [server/README.md](server/README.md) 참조.
브라우저/Honey 오류는 **진단 사건 저장소(14일 JSONL) 한 곳**에만 남긴다 — 감사 이중 기록은
중단됐다([docs/20](docs/20_error_tracking.md)).
- `report_annotation` / `report_dashboard_comment` / `report_csv_files` /
  `report_analysis_lock` / `report_user`(ID/PW 로그인 폐지 — 미사용 보존).

`product_type` enum: **MDDI / PDDI / PMIC / SECURITY / TCON** (core.py `_PRODUCT_TYPE_NAMES`).

---

## 3. S3 저장 (외부 담당자 영역·동결, 검증용)

S3/storage_gateway 는 외부 담당자 영역·동결 경계다 — 미설정 시 `REPORT_UPLOAD_DIR` 로컬 폴백으로
동작하며 현재 코드에선 검증용이다. facade·키·저장 위치 기록 계약은 정본
[storage_gateway/README.md](server/storage_gateway/README.md) 참조. 키는 모두
`pe/report_server/` 네임스페이스(plotly legacy 충돌 회피): `issue_img/` · `chart_png/` ·
`distribution_combined/` · `web_report_source/` · `web_report_manifest/`. 실제 값은
[config.py](server/config.py) `REPORT_S3_*_PREFIX`.

**저장 위치 기록**: web_report parquet/manifest 는 저장 위치를 `report_object_info.options_json`
에 `{"storage":"s3"|"local"}` 로 기록하고 조회는 그 기록을 따른다 — 기록이 s3 인데 다운로드
실패 시 침묵 로컬 폴백 대신 예외(복구 후 과거 S3 파일 부활 방지). 텍스트(summary/issue/yield)는
S3 가 아니라 DB `report_sheet_data` 에 저장한다.

---

## 4. 환경변수

**전체 목록·기본값은 정본 [server/README.md](server/README.md)** (web_report 캐시 env 는
[docs/12](docs/12_web_report_cache.md)). 여기는 **행동에 영향 주는 함정**만:

- **서버 주소 정본은 [server/env/server.env](server/env/server.env) 한 파일이다.** `HOST`(bind,
  기본 `0.0.0.0`) / `PORT`(기본 8080) / `SERVER_BASE_URL`(클라가 접속하는 주소,
  기본 `http://12.81.220.117:8080`). 서버는 이 파일을 직접 읽고([server/config.py](server/config.py)),
  클라는 개발 실행 시 같은 파일을, 빌드본은 `build_zip` 이 이 파일에서 생성해 넣은
  `Honey.exe` 옆 `honey.env` 를 읽는다([client/transport/config.py](client/transport/config.py)).
  IP 변경 = server.env 1줄 수정 → 서버 재기동 → `build_zip` 재실행 → 클라 재배포.
  `HOST` 를 운영 IP 로 바꾸지 말 것 — 그 IP 를 가진 PC 외에서 기동이 실패한다(`0.0.0.0` 이 포함).
- `REPORT_S3_BUCKET` 미설정이면 S3 대신 **로컬 폴백**으로 조용히 동작한다(에러 아님). yield
  rows 등 DB 저장은 정상.
- `REPORT_CLEANUP_DRYRUN` 기본 **1(참)** — **기본은 실삭제 안 함**(대상만 로그). 실삭제하려면
  `0` 으로 명시.
- `AUTH_SSO_HEADER` 지정 시 그 역프록시 헤더가 신원으로 우선(기본은 HoneyUser UA).
- `WEB_REPORT_COMPUTE_WORKERS` 기본 2(**운영 server.env 는 8** — 16코어/64GB), `0` = 콜드 빌드 전부
  인라인(구 동작). `WEB_REPORT_ONDEMAND_WORKERS`(202 후 백그라운드 빌드 소비자 스레드)와
  **짝으로** 올려야 한다 — 풀만 늘리면 소비자 스레드 수가 새 상한이 된다.

DB 백업 사이클(db_backup.py)이 매회 `PRAGMA wal_checkpoint(TRUNCATE)` + `PRAGMA optimize` 로
-wal 비대를 막는다. VACUUM 은 장시간 잠금이라 자동 실행 안 함(수동).

---

## 5. 주의 사항 (불변 규칙)

1. 원본 xlsx 는 서버로 전송·저장하지 않는다. 클라이언트가 Excel COM 으로 추출한
   summary/yield/issue_table grid(JSON)와 issue PNG 만 업로드하고, 텍스트는 DB(sheet_data),
   PNG 는 S3(로컬 폴백)에 보관한다. **서버는 openpyxl·Excel 을 쓰지 않는다.**
   - 기준정보 CSV(DRM/NASCA)도 같은 이유로 서버에서 열지 않는다. Excel 이 있는 별도 PC 에서
     [tools/product_info_import](tools/product_info_import/README.md) 로 만든
     `product_info.db` 를 수동 배치하고 서버는 읽기 전용으로만 연다
     ([server/product_info.py](server/product_info.py), [docs/09 §3](docs/09_db_inventory.md)).
2. `report_` prefix 없는 새 테이블 만들지 말 것.
3. analysis_key 산출은 `canonical(sheet_grids) + canonical(meta)` 의 sha256 — 메타 변경 시
   같은 데이터라도 다른 키가 됨. canonical 은 `json.dumps(sort_keys=True)`. password·신원·
   mode 는 **불포함**. (web_report 는 `sha256(canon({files, meta, selected_items}))` —
   [docs/10](docs/10_web_report_pipeline.md).)
   - 이 산출식은 **업로드 시점** 규약이다. 업로드 후 메타를 고치는
     `PATCH /session/<sid>/meta` 는 **analysis_key 를 재산출하지 않는다** — 산출물
     (parquet·manifest·summary)이 전부 그 키로 저장돼 있어 키를 바꾸면 세션이 자기 데이터를
     잃는다. 어긋나는 건 dedup(같은 데이터 재업로드) 매칭뿐이다.
4. 클라이언트 자동 업데이트는 batch 스크립트 + 외부 다운로드 방식. 실행 중인 exe
   에 직접 쓰지 말 것 (Windows 락).
5. **Distribution 차트 데이터 다운샘플링 절대 금지.**
   모든 데이터 포인트를 빠짐없이 차트에 표현해야 한다.
   `_MAX_CDF_POINTS`, `_downsample`, `max_points` 같은 포인트 상한 로직을 절대 추가하지 말 것.
   유일 예외: 동일값 구간을 2포인트 선분으로 표현하는 계단형(step) ECDF 변환
   (`client/report_generator/_builders.py` `cumulative_distribution_full()`), web_report 는
   미니셀 썸네일만 표시용 다운샘플(`DIST.DOWNSAMPLE`, 소스별 소프트 상한 1500
   — [docs/11](docs/11_web_report_tabs.md)). 미니셀의 점은 Plotly SVG 마커가 아니라
   canvas 오버레이(`distPaintPoints`)로 그린다(2026-07-20, 좌표·색·점크기 동일).
   - **web_report Distribution ECDF 미니셀은 markers(점)만으로 렌더한다.** 점을 잇는 선,
     특히 Plotly `line.shape:"hv"` 계단형 수평선은 금지 — x축 방향 수평선은 누적분포를
     왜곡하고 사용자 경험에 반한다. 이산(code unit)값처럼 고유값이 적어 점이 성겨 보이는
     문제는, 동일값 구간(ECDF riser)을 y축 방향 세로 점기둥으로 채우는 보간
     (`distFillVertical`)으로만 해결하고 선으로 잇지 않는다. 보간은 표시용 다운샘플보다
     **먼저** 적용한다(`distPointsForDisplay` = 세로 채움 → 다운샘플).
     - **채우는 점 개수는 그 값의 실제 측정 개수와 같아야 한다** (2026-08-25). 채움 간격은
       서버가 응답에 함께 싣는 소스별 표본 수로 정한다 — `distStepY` 가 `100/n`
       (성능 하한 `100/fillMax` 로만 클램프). n 은 `build_distribution_compact` 와
       `dist_pack._ecdf_sources` **두 경로가 같은 자리**에 낸다(정준 JSON 일치 계약).
       그래야 소량 데이터(n≤100)가 부풀지 않고 이산(code)값은 실제 중복 수만큼 채워진다.
       ⚠️ **고정 상수로 간격에 상한을 걸지 말 것** — `FILL_VISUAL_MAX_DY`(0.3%)는 n 이 없는
       옛 응답 폴백 전용이다. 폴백 밖에서 쓰면 표본<333 세션이 실제보다 촘촘해진다
       (실제 회귀: n=100 이 400점). perf_guard `R13-ecdf-fill-cap` 이 차단한다.
     - ⚠️ 채움 루프는 **누적 덧셈이 아니라 riser 균등 분할**이다(`k = round(Δy/stepY)`).
       서버가 y 를 `round(cum, 3)` 으로 내리므로 stepY 가 굵어지면(=표본이 작으면) 누적
       덧셈이 riser 끝과 어긋나 없어야 할 점이 생긴다(n=7 에서 실측).
     - 세로 방향 표시용 업샘플링만 허용하고, **x값을 만들어내는 가로 방향 보간은 금지**다.
       조밀한 데이터는 riser 가 관측 1개라 채움이 0 이다.
     상세 CDF(`distRenderCdf`)는 원본 전량 렌더 별도 경로로 채움·다운샘플 대상 외.
     Excel 다운로드 포팅본(`client/excel_download/_charts.py` `_dist_step_y`/
     `_dist_fill_vertical`)은 이 규칙의 사본이라 **같이 고친다**.
6. **web_report 편집 상태는 세션 편집 DB 가 진실.** manifest 는 업로드 시점 불변 스냅샷이므로
   편집으로 재저장하지 않는다. **예외는 둘뿐**이며 둘 다
   [rawedit.replace_sources](web_report/rawedit.py) 안에 있다:
   ① Excel 왕복 편집에서 시트를 지워 source 가 줄면 `sources` 목록을 축소해 재저장한다
   (안 하면 idx↔parquet 대응이 어긋남 — [docs/11](docs/11_web_report_tabs.md)).
   ② **신규 Item(수식) 추가**로 컬럼이 생기면 그 이름을 `selected_items` 에 덧붙인다
   (2026-08-24). 안 하면 parquet 에는 컬럼이 있는데 리포트 어디에도 안 보인다 —
   [metrics.py](web_report/metrics.py) 등 8곳이 `selected_items` 로 `item_columns` 를
   거르기 때문이며, 에러가 아니라 "빈 화면"으로 나타난다. `selected_items` 가 **비어 있으면
   갱신하지 않는다**(빈 값 = 전 항목 선택이라 필터 자체가 없다 — 한 개짜리 목록을 만들면
   오히려 나머지 항목이 전부 사라진다). 두 경우 모두 `analysis_key` 는 재산출하지 않는다. 캐시 키는 항상
   [cache_policy.py](web_report/cache_policy.py) 빌더로 만든다(즉석 조립 금지 —
   [docs/12](docs/12_web_report_cache.md)). raw parquet 을 바꾸는 편집은 `content_hash` 를
   **같은 analysis_key 의 전 세션**에 반영한다(dedup 형제의 stale 캐시 방지).
7. **소유권 / 수정 권한 경계.** 정본 [docs/15_ownership.md](docs/15_ownership.md). 요약:
   🟢 자유 수정=`web_report/`+`server/`(`storage_gateway/` 제외)+web_report html
   +client 자주 쓰는 영역(`honey_ui/`·`honey_main.py`·`transport/`·`excel_download/`·`excel_edit/`) /
   🟡 사전 승인=`client/` 나머지 비동결(편집 전 파일·이유·영향 설명) /
   🔒 외부 담당자 영역(건들 때마다 승인)=`d1/`·`d1_storage/`·`client/honey_parse/`·
   `client/report_generator/`·`server/storage_gateway/`(facade+`_s3` 전체). **경계가 폴더 내부를
   가르는 곳**: server/ 는 storage_gateway 만 외부 영역, client/ 는 report_generator·honey_parse 만
   외부 영역. `eval_analyzer/` 는 2026-08-03 자유 수정으로 승격됐다(동결 아님).
8. **eval_analyzer 단방향 의존.** `eval_analyzer/` 는 **이 repo 가 원본**이다
   (2026-08-03 — 외부 사본 `F:\COINAPI\eval_analyzer` 는 더 이상 참조·동기화 대상이 아니다).
   하위 파일은 자유 수정이지만 **의존 방향은 계속 단방향**이다 — eval_engine import 는
   [web_report/ai_comment.py](web_report/ai_comment.py)(evaluate 호출) +
   [web_report/eval_export.py](web_report/eval_export.py)(store·ingest 헬퍼 — 코멘트 export) +
   [web_report/eval_debug.py](web_report/eval_debug.py)(룰 리로드·L0~L6 트레이스 — `/pe/eval`)
   **3곳만** 허용(양방향 그 외 import 금지). 서버의 evaluate 호출은 persist=False(운영
   eval.db 무기록) — 코멘트 export 는 report_server 소유 별도 파일 `REPORT_EVAL_DB_PATH`
   에만 쓴다. 규약 전문 [docs/13](docs/13_eval_analyzer_integration.md).
   - **eval_DB 스키마 변경은 사전 확인 대상**이다. `eval_engine/store.py` 의 DDL·컬럼을
     바꿔야 하는 상황이면 바로 고치지 말고 **어떤 테이블·컬럼을 어떻게 바꾸는지와 영향을
     설명한 뒤 사용자 승인을 받고** 진행한다(운영 eval.db 에 누적 데이터가 있다).
     스키마와 무관한 나머지 수정은 자유.
   - 서버가 `eval_analyzer/db_input/import_csv.py` 를 **subprocess 로 실행**하는 것은
     import 가 아니므로 2곳 규약 위반이 아니다 (Honey 'DB Input' —
     [server/report/routes_eval_input.py](server/report/routes_eval_input.py),
     [docs/13 §10](docs/13_eval_analyzer_integration.md)). 별도 프로세스인 이유는
     `import_csv._import_group` 이 `eval_engine.config.DB_PATH` 를 모듈 전역에 대입하기
     때문 — 장수명 Flask 프로세스를 오염시키지 않으려면 프로세스 경계가 필요하다.
9. **입력 계약은 7-meta honeyform 이다 (2026-07-21 확정).** `honey_parse.file_to_df` 반환 df =
   `SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO` + `TSEQ~LOLIM` 6행, 반환 df 개수 = source 개수
   (병합은 honey_parse 내부에서). **이 산출물(`md.df`)이 곧 web_report parquet 소스**이며,
   원본 입력 파일을 다시 읽어 검증하지 말 것 — 병합 결과를 버리게 된다.
   - ⚠️ **input 파일 개수 ≠ source 개수 (1:1 아님).** source 개수의 유일한 기준은
     `file_to_df` 가 **리턴한 df 개수**다. 특정 product 는 input 파일이 n 개여도 파서가
     내부 병합해 source 1개로 리턴한다(병합 규칙은 외부 담당 honey_parse 소관이라 이 repo
     에서 알 수 없음). source 를 세는 코드를 만들거나 고칠 때 입력 파일 개수로 세면 안 된다.
   - 구 5-meta df_honey(`DUT,XCoord,YCoord,Bin,Serial`)는 **폐기된 계약**이다.
   - ⚠️ **좌표 규약: `XPOS`/`YPOS` 는 언제나 양수다** (0/1-based die 인덱스 — 음수 좌표는
     실데이터에 없다). 따라서 **웨이퍼 중심은 (0,0)이 아니다** — 반경·사분면을 쓰는 코드는
     좌표 범위의 중앙을 중심으로 잡아야 한다. 원점 기준으로 재면 웨이퍼 한 귀퉁이가 중심이
     되어 edge/center/ring/quadrant 판정이 통째로 어긋난다(eval 엔진에서 실제로 겪음 →
     [docs/13 §16-2](docs/13_eval_analyzer_integration.md)).
   - ⚠️ **제품군별 좌표 상한: PMIC 은 `YPOS` 가 200 을 넘지 않는다**(Y=201 같은 값은 없다).
     테스트/합성 데이터를 만들 때 이 범위를 넘기면 실데이터가 아니게 된다.
   - ⚠️ **알려진 격차**: `client/report_generator/`(constants.py `DATA_START_ROW=5` 등)는 아직
     5-meta 를 가정한다 → 7-meta 프레임에서 `BIN`·`FAILTNO` 를 측정 항목으로 오인한다.
     외부 담당자 소유라 **이 저장소에서 고치지 않는다**(최신 사본 수령으로 해소).
     `client/honey_parse/` 더미 폴백도 5-meta 라 **개발 PC 로컬 Web Report 업로드는 실패가
     정상**이다. 상세 [docs/06](docs/06_analysis_engine.md).
10. **web_report 성능 회귀 가드를 통과해야 한다.** 위 규칙 중 기계 검사가 가능한 것들은
    [tools/perf_guard.py](tools/perf_guard.py) 가 `web_report/`·`server/report/` 의 Edit/Write
    직전에 검사해 **쓰기 자체를 거부**한다(Claude Code 훅, [.claude/settings.json](.claude/settings.json)).
    막혔다면 그 변경이 과거에 회귀를 냈던 방향이라는 뜻이다 — 우회하지 말고 되돌리거나,
    의도한 변경이면 `# perf-guard: allow <규칙ID> (사유)` 면제를 사유와 함께 단다.
    회귀가 새로 나면 고치는 것으로 끝내지 말고 `_RULES` 에 규칙 1개를 추가하는 것이
    표준 사후 조치다 → [docs/18](docs/18_perf_guard.md).
11. **Map Analysis 3초 SLA.** gross die 10,000개 × 7 source 세션에서 **Map Analysis 탭
    첫 화면**과 **Issue Table 의 Map 컬럼**은 3초 안에 떠야 한다(둘은 같은
    `.../web_report/map_analysis` 응답을 소비하므로 한 지표다).
    **규칙 5(다운샘플 금지)를 어겨서 달성하지 말 것** — die 는 전량 유지하고 *구조*로
    맞춘다. 현재 달성 수단 2개:
    - report 콜드 빌드가 map dies gzip 을 **같은 tables 로 함께 시딩**
      ([web_report/service.py](web_report/service.py) `seed_map`, temp_map 시딩과 대칭).
      map dies 는 프리웜 대상이 아니라, 이게 없으면 첫 탭 진입이 사실상 항상 콜드 202 +
      전체 재디코드(30초+ "맵 로드 중…")가 된다.
    - 시딩 도입 전 세션은 `/full` 200 경로가 백그라운드 백필만 예약
      (`schedule_map_backfill` → `compute.request_build(..., "map")`, 대기하지 않음).
    기계 확인 2중: [tests/bench_webreport.py](tests/bench_webreport.py) 의 SLA 시나리오
    (절대 기준 — 초과 시 `[SLA위반]`, 기준선 없어도 뜬다) + perf_guard `S09-map-seed` 가
    시딩 호출 제거를 차단. 정합성(시딩 산출 == 콜드 빌드 산출)은
    [tests/test_map_seed_equivalence.py](tests/test_map_seed_equivalence.py).
    → [docs/12](docs/12_web_report_cache.md)
12. **사용자가 입력한 것은 무슨 일이 있어도 잃지 않는다.** 세션에서 사용자가 직접 입력한
    모든 것(Issue Table comment, 행 숨김/Status, Note 시트, 차트 주석, ENGR 요약, trim
    override, 전처리 설정 …)은 **소실되면 사용자 경험상 치명적**이다 — 다시 입력할 방법이
    없고, 사라져도 에러가 아니라 "빈 값"으로 보여 발견조차 늦다. 따라서 **어떤 코드 변경도
    기존 입력이 사라지는 경로를 만들어선 안 된다.** 서비스 중인 서버라 이미 운영 DB 에
    실제 입력이 쌓여 있다는 전제로 판단한다.

    가장 흔한 소실 원인은 **저장 키 변경**이다. 아래 4종은 표시 문구만 바꾸고
    **저장 값은 고정**한다(화면 라벨 ≠ 저장 키):
    - `row_key` 접두 — `Yield|<bin>|<item>` / `CPK|<item>` / `TEMP|<item>` / `ETC|<item>`
      (+ Compare 모드 전용 `CMPDIST|<item>` / `CMPETC|<item>` — 2026-08-20 신설,
      [tabs/compare_issue.py](web_report/tabs/compare_issue.py) 소관).
      **파서 사본이 4곳**이라 손대려면 전부 같이 고쳐야 한다:
      [issue_table.py](web_report/tabs/issue_table.py) 생성 ·
      [sheets.js](server/report/static/webreport/sheets.js) `issueRowKey`/`issueHideStatusKey` ·
      [eval_export.py](web_report/eval_export.py) `_parse_row_key` ·
      [chatbot/rowkey.py](server/chatbot/rowkey.py) (+ service.py 의 숨김/Status 허용 접두 2곳).
    - comment 컬럼명 — `COMMENT_COLS = ["PTE comment", "개발 comment"]`. 화면·Excel 헤더의
      "개발팀 Comment" 는 `COLUMN_DISPLAY_ALIAS` 표기일 뿐 **저장 키는 `"개발 comment"`**.
    - 행 숨김/Status 키 — Yield 는 **bin 단위** `Yield|<bin>`(대표행+상세행 일괄),
      나머지는 `CPK|<item>`/`TEMP|<item>`/`ETC|<item>`/`CMPDIST|<item>`/`CMPETC|<item>`.
      **숨김은 ETC 계열(ETC|·CMPETC|)만 제외**한다(항목 자체를 지우는 편이 자연스러워서) —
      허용 목록은 `service._ISSUE_HIDABLE_PREFIXES`, 전체는 `_ISSUE_KEY_PREFIXES`.
    - Compare 탭 행 코멘트 키 — `gl:<after_item>` + U+001F + `<before_item>`(Log 비교 행) /
      `bm:<x>,<y>`(동일 좌표 Bin 비교 행). **행 인덱스 금지** — 필터·접기로 순서가 바뀐다.
    - Distribution composite 키 — item_key = **생성 UUID(불변)**, pairKey =
      `<source>` + U+001F + `<item>`(색 맵의 키). 이름을 바꿔도 키는 그대로다 — 이름·표시명을
      키로 쓰면 개명 한 번에 사용자가 만든 차트가 통째로 사라진다.
      차트 주석 키도 같은 규칙으로 `cdf:comp:<uuid>`(`dcNoteSubject`) 다.
      ⚠️ 저장 spec 은 `distIndex`/현재 source 목록으로 **filter 하지 말 것** — 전처리 제외나
      source 축소로 목록에서 빠진 pair 가 "이름만 바꿔 저장" 하는 순간 조용히 사라진다
      (`dcOrderedPick` 이 선택 집합 전체를 보존한다).
    - Gap Chart 키 — item_key = **생성 UUID(불변)**, 수식은 **평문이 아니라 토큰 배열**이
      정본이다(`{"t":"item","source"?,"item"}` / `num` / `op` / `lp` / `rp`). item 이름에
      공백·괄호·연산자가 전부 합법이라 평문 재파싱이 원리적으로 불가능하고, source 명·item 명
      둘 다 `_` 를 포함할 수 있어 `source_item` 분해도 못 한다 — 표시 문자열은 토큰에서
      만들고 **절대 되돌려 읽지 않는다**([web_report/gap_chart.py](web_report/gap_chart.py)).
      차트 주석 키는 `cdf:gap:<uuid>`(`note_subject`)로 갈라 동명 항목과 섞이지 않는다.
    - 편집 `kind` **16종** 이름과 item_key — `issue_comment`/`etc_item`/`cmp_etc_item`/
      `trim_override`/`summary_engr`/`chart_note`/`compare_note`/`dist_composite`/`gap_chart`/
      `note_sheet`/`note_tag`/`issue_hidden`/`issue_status`/`issue_signature`/`preprocess`/
      `yield_basis` (`KIND_*` 상수가 정본 — [edits.py](web_report/edits.py) 규약).
      Note 는 `note_sheet` + item_key `"sheet"` 전체 치환.
      `cmp_etc_item` 은 Issue Table Compare 탭의 ETC 목록으로 `etc_item` 과 **분리**돼 있다 —
      한 세션에 두 표가 함께 있어 kind 를 공유하면 한쪽 추가가 다른 표에도 나타난다.
      legacy 세션(rev==0)의 manifest 폴백·자동 시드 경로도 함께 유지한다.

    불가피하게 바꿔야 하면 **고치기 전에 멈추고** 대상 키·영향 세션 수·마이그레이션 방법을
    설명해 승인을 받는다(§ 상단 주의사항과 같은 취급). 상세는
    [docs/11 §Issue Table comment 키](docs/11_web_report_tabs.md) ·
    [docs/13](docs/13_eval_analyzer_integration.md)(row_key ↔ eval case 매핑).

13. **같은 값·같은 목록은 한 곳에서만 계산하고, 나머지 화면은 그것을 가져다 쓴다
    (재계산 금지).** 같은 항목이 탭마다 다른 숫자로 보이면 사용자는 리포트 전체를
    신뢰하지 않는다. 어떤 코드를 고치든 **그 산출물을 가져다 쓰는 곳이 계속 같은 값을
    받는지** 확인하고, 새로 계산하는 코드를 추가하지 말 것.
    - **Yield 탭을 바꾸면 Issue Table 도 반드시 같이 확인한다. 두 표의 목록(bin/item)은
      동일해야 한다.** Issue Table 은 `ctx.yield_rows`·`ctx.cpk_rows` 를 그대로 소비하는
      구조라([tabs/__init__.py](web_report/tabs/__init__.py) `TAB_REGISTRY`),
      [yield_tab.py](web_report/tabs/yield_tab.py) 의 행 생성·필터·분모(basis)를 손대면
      Issue Table 의 Yield 섹션 목록이 함께 변한다. 한쪽에만 필터를 넣어 목록이
      갈라지게 하지 말 것 — 목록 차이는 에러 없이 "이슈가 사라진" 것처럼 보인다.
    - **Item_detail 의 CPK 는 CPK 탭의 CPK 와 같아야 한다.** 기준 정본은
      [tabs/cpk.py](web_report/tabs/cpk.py) 모듈 docstring(Bin1 단일 기준 + Temperature
      CT/HT 는 RT Bin1 die × RT limit) 하나뿐이다. Item_detail
      ([tabs/distribution.py](web_report/tabs/distribution.py) `scatter_item`)은 지연 로드
      경로라 `_stats` 로 **다시 계산**하는 유일한 예외인데, 공식만 같고 코드가 갈라져 있어
      한쪽에 예외를 넣으면 값이 어긋난다(2026-08-12 Temperature 예외 누락으로 실제 발생).
      → `cpk.py` 의 기준(모집단 마스크·limit 선택)을 고치면 `scatter_item` 도 **같은
      커밋에서** 반영하고, 가능하면 `cpk.py` 의 헬퍼를 호출해 공식 사본을 늘리지 말 것.
    - 이미 재사용으로 정리된 곳(되돌리지 말 것): Distribution 카드 status/cpk 는
      `cpk_rows` 재사용(`worst_cpk_by_subject`), Issue Table CPK 섹션도 같은 함수,
      Temperature 표/Map 은 `compute_temp_fail` 판정 1회분 공유.

14. **스키마 버전은 "그 캐시 것만" 올린다 — 전역 bump 는 최후수단이다.**
    [cache_policy.py](web_report/cache_policy.py) 에는 버전 상수가 **9개** 있고 각각 무효화
    범위가 다르다. 응답 구조를 바꿨으면 **그 캐시의 상수만** 올린다. 전역
    `REPORT_SCHEMA_VERSION` 을 올리면 **전 세션이 동시에 콜드 재빌드**돼 "어제부터 전체적으로
    느림" 신고가 된다(2026-08-13 하루 3회 bump 로 실제 발생 — 조회 급락 1순위 용의자).
    260824 커밋 8건이 Gap Chart·Distribution composite·Serial 순·Issue Table Compare 를
    **전역 bump 없이** 넣은 것이 설계 의도다.

    | 상수 | 무효화 범위 |
    |------|-------------|
    | `REPORT_SCHEMA_VERSION` | **전역 payload** — 전 세션 콜드 폭풍. `build_report_payload` 구조를 바꿀 때만 |
    | `TEMPERATURE_SCHEMA_VERSION` | Temperature 세션 payload |
    | `COMPARE_REPORT_SCHEMA_VERSION` | Compare 세션 payload **적재 방식** |
    | `COMPARE_SCHEMA_VERSION` | compare **계산 결과**(`compare_key`) |
    | `AI_COMMENT_SCHEMA_VERSION` | ai comment 반환 dict 구조 |
    | `MAP_SCHEMA_VERSION` / `TEMP_MAP_SCHEMA_VERSION` | map rows 값 / temp_map 응답 구조 |
    | `DIST_SEQ_SCHEMA_VERSION` | Serial 순 배치 응답 구조 |
    | `GAP_SCHEMA_VERSION` | Gap Chart 응답 구조 (키·**ETag 양쪽**에 들어간다) |

    ⚠️ `COMPARE_SCHEMA_VERSION`(계산) 과 `COMPARE_REPORT_SCHEMA_VERSION`(payload 적재)은
    **다른 상수**다 — 헷갈려 반대쪽을 올리면 아무것도 안 갈리거나 필요 없는 재계산이 돈다.
    현재 값·env·키 구성 표는 [docs/12](docs/12_web_report_cache.md) 가 정본.

15. **서버와 JS 에 같은 상한값이 두 벌 있는 곳은 반드시 짝으로 고친다.**
    입력 검증은 프런트(즉시 안내)와 서버(신뢰 경계) 양쪽에 있어야 해서 상수가 의도적으로
    이중 정의돼 있다. 한쪽만 고치면 **에러가 아니라** "저장 버튼은 눌리는데 400" 또는
    "서버는 받는데 화면이 막는다" 로 나타난다.

    | 서버 | JS |
    |------|-----|
    | [gap_chart.py](web_report/gap_chart.py) `MAX_TOKENS`/`MAX_DEPTH`/`MAX_REFS` | `gap_chart.js` `GC_MAX_TOKENS`/`GC_MAX_DEPTH`/`GC_MAX_REFS` |
    | [service.py](web_report/service.py) `_DC_MAX_PAIRS` / `_DC_PAIR_SEP`(U+001F) | `dist_composite.js` `DC_MAX_PAIRS` / `DC_SEP` |
    | [routes_webreport.py](server/report/routes_webreport.py) `_DIST_SEQ_BATCH_MAX` | `distribution.js` `DIST.SEQ_SIZE` (서버 상한 이하) |

    기계 검사는 [tests/test_dist_seq_js.py](tests/test_dist_seq_js.py) 의 배치 크기 하나뿐이다
    — 나머지는 사람이 지켜야 한다. 상수를 새로 이중 정의하면 그 짝을 이 표에 추가할 것.
    ([formula.py](web_report/formula.py) 는 `gap_chart.py` 파서의 확장 사본이며, 그쪽 드리프트는
    `tests/test_formula_item.py` 의 동치 테스트가 막는다.)

16. **편집 kind 의 두 제외 목록은 서로 다르다 — "동시 등재"가 아니라 각각 판단한다.**
    - [edits.py](web_report/edits.py) `_STATE_EXCLUDED_KINDS`(**8종**) = 편집 **state dict**
      에 싣지 않을 kind. 별도 라우트로 조회하는 것들.
    - [database/webreport_edits.py](server/database/webreport_edits.py) `PAYLOAD_NEUTRAL_KINDS`
      (**5종** = chart_note/note_sheet/note_tag/dist_composite/gap_chart) = 저장해도
      `payload_rev` 를 올리지 **않을** kind. report payload 계산에 안 쓰이는 것들.

    `preprocess`·`yield_basis`·`compare_note` 는 state 에서만 빠지고 payload_rev 는 올린다
    (실제로 payload 를 바꾸므로 **올려야 맞다**). 새 kind 를 만들면 두 목록을 기계적으로
    함께 채우지 말고 **"이 kind 가 report payload 숫자를 바꾸는가"** 로 판단할 것.
    payload 중립인데 `PAYLOAD_NEUTRAL_KINDS` 에서 빠뜨리면 저장할 때마다 report 전체가
    콜드 재빌드된다(규칙 14 와 같은 기전).

---

## 6. 코드 포인터

| 알고 싶은 것 | 어디? |
|--------------|-------|
| 소유권/수정 권한 경계 (정본) | [docs/15_ownership.md](docs/15_ownership.md) (자유/사전승인/외부 담당자 영역) |
| API 엔드포인트·환경변수 (정본) | [server/README.md](server/README.md) |
| **web_report 를 외부/챗봇/MCP 에 개방** (`/pe/api/v1/web-report`) | 규약 정본 [public_api/web_report/contracts.py](server/public_api/web_report/contracts.py) `FUNCTION_SPECS` → `/capabilities`·MCP tool·관리자 규약 탭이 전부 여기서 파생. 조회 [facade.py](server/public_api/web_report/facade.py)(Flask 무의존) · HTTP [routes.py](server/public_api/web_report/routes.py) · 문서 [CONTRACT.md](server/public_api/web_report/CONTRACT.md) · MCP 골격 [server/web_report_mcp/](server/web_report_mcp/README.md). ⚠ 새 계산 금지(payload 슬라이스만) · `viewer=None` 금지 · 콜드는 202(동기 대기 금지) · 캐시 공유 객체는 복사 후 가공 |
| xlsx 업로드 라우트 | [server/upload_xlsx.py](server/upload_xlsx.py) · grid 파싱 [xlsx_parser.py](server/xlsx_parser.py) |
| web_report 업로드/파이프라인 | [server/upload_webreport.py](server/upload_webreport.py) → [docs/10](docs/10_web_report_pipeline.md) |
| 라우트 (세션/web_report/기타) | [routes_session.py](server/report/routes_session.py) / [routes_webreport.py](server/report/routes_webreport.py) / [routes_misc.py](server/report/routes_misc.py) |
| 접근제어·CSRF·가드 | [server/report/security.py](server/report/security.py) → [docs/02](docs/02_server_query_edit.md) |
| 신원/인증 (SSO 전환) | [server/auth_identity.py](server/auth_identity.py) |
| 사람 식별 키 정규화 (중복 사용자 통합) | [server/identity_norm.py](server/identity_norm.py) `normalize_uid` · 기존 DB 병합 [tools/merge_duplicate_users.py](server/tools/merge_duplicate_users.py) → [docs/02](docs/02_server_query_edit.md) |
| DB 스키마 (정본) | [server/database/core.py](server/database/core.py) `SCHEMA` (report_db.py 는 재노출 facade) |
| web_report 편집 상태 | [web_report/edits.py](web_report/edits.py) + [server/database/webreport_edits.py](server/database/webreport_edits.py) |
| web_report 캐시 키 규약 | [web_report/cache_policy.py](web_report/cache_policy.py) → [docs/12](docs/12_web_report_cache.md) |
| Distribution 정렬 전가(pack) — 서버 최대 병목 제거 | [web_report/dist_pack.py](web_report/dist_pack.py) + [dist_pack_store.py](web_report/dist_pack_store.py) → [docs/12](docs/12_web_report_cache.md) |
| 콜드 빌드에서 무거운 계산 떼어내기 (AI Comment·Compare) | 분리 캐시 키 [cache_policy.py](web_report/cache_policy.py) `ai_comment_key`/`compare_key` · 조회 [service.py](web_report/service.py) `_ai_comment_cached`/`_compare_cached` · 대기본 `report_pending_key(kinds)` · 백그라운드 잡 [compute.py](web_report/compute.py) `_ONDEMAND_JOBS` · 프런트 폴링 [boot.js](server/report/static/webreport/boot.js) → [docs/12](docs/12_web_report_cache.md). **분리 캐시 키에 sid·edits_rev 를 넣지 말 것** — 그게 편집마다 전량 재계산을 부른다(perf_guard S10·S12) |
| 새 탭 추가 (레지스트리) | [web_report/tabs/__init__.py](web_report/tabs/__init__.py) `TAB_REGISTRY` → [docs/11](docs/11_web_report_tabs.md) |
| **Distribution composite (source×item 합성 산포 차트)** | 프런트 [dist_composite.js](server/report/static/webreport/dist_composite.js) · 훅 [distribution.js](server/report/static/webreport/distribution.js)(툴바/갤러리/셀/`_distColorFor`) · 저장 [service.py](web_report/service.py) `update_dist_composites` + kind [edits.py](web_report/edits.py) `KIND_DIST_COMPOSITE` → [docs/11](docs/11_web_report_tabs.md). **서버 계산 추가 없음** — `distribution_batch` 재사용, 정의만 저장 |
| **Distribution "Serial 순" (rawdata 누적 순 run chart)** | 계산 [web_report/dist_seq.py](web_report/dist_seq.py)(**tabs/ 밖 — S01 콜드폭풍 회피**) · 라우트는 기존 `/distribution_batch` 의 `order=seq` 파라미터 · 캐시 [cache_policy.py](web_report/cache_policy.py) `dist_seq_batch_key` · 프런트 [distribution.js](server/report/static/webreport/distribution.js)(툴바 맨 앞 버튼·`distGalleryDataVariant`·`distRenderGallerySeqCell`) + [item_detail.js](server/report/static/webreport/item_detail.js)(`distRenderSeq`) + 합성 카드 2종([dist_composite.js](server/report/static/webreport/dist_composite.js) `_dcCache` 6키·상세는 차트만 seq / [gap_chart.js](server/report/static/webreport/gap_chart.js) `seqEntry` — **캐시 확장 없음**) → [docs/11](docs/11_web_report_tabs.md). ⚠ seq 차트에 **차트 주석을 붙이지 말 것**(좌표 의미가 달라 저장값을 덮어쓴다) · `distGalleryVariant()` 에 seq 를 섞지 말 것 · composite 상세 **통계표는 ECDF 기준 고정**(규칙 13) |
| **Gap Chart (사용자 수식 파생 분포)** | 계산 [web_report/gap_chart.py](web_report/gap_chart.py)(**tabs/ 밖 — perf_guard S01 이 REPORT_SCHEMA_VERSION bump 를 요구해 콜드 폭풍이 된다**) · 프런트 [gap_chart.js](server/report/static/webreport/gap_chart.js) · 저장 kind [edits.py](web_report/edits.py) `KIND_GAP_CHART`(토큰 배열이 정본) · 조회 `GET .../web_report/gap_chart/<id>` + 캐시 [cache_policy.py](web_report/cache_policy.py) `gap_key`(**spec_digest 를 키·ETag 양쪽에**) · 상세는 **기존 Item_detail 재사용**(`openItemDetail` 의 `opts.url`) → [docs/11](docs/11_web_report_tabs.md) |
| **Compare 검출을 이슈 표로 (Issue Table Compare)** | 시트 [tabs/compare_issue.py](web_report/tabs/compare_issue.py)(Distribution·ETC) + 프런트 [compare_issue.js](server/report/static/webreport/compare_issue.js)(Bin Transition·Log 별도 표) · 패널 일반화는 [core.js](server/report/static/webreport/core.js) `ISSUE_PANEL_SEL` · 캐시 세대 `COMPARE_REPORT_SCHEMA_VERSION` · 검증 데이터 [tools/eval_testdata/make_compare_testdata.py](tools/eval_testdata/make_compare_testdata.py) → [docs/11](docs/11_web_report_tabs.md) |
| Temperature(PMIC·SECURITY RT/CT/HT) — 전 항목 RT limit 재판정 | [web_report/tabs/temp_fail.py](web_report/tabs/temp_fail.py) (조회 시점 서버 계산) + 업로드 전 정리 [web_report/temperature.py](web_report/temperature.py) + CT/HT CPK 는 **RT Bin1 die × RT limit** ([tabs/cpk.py](web_report/tabs/cpk.py) `temperature_reference_tables`) → [docs/11](docs/11_web_report_tabs.md) |
| S3 저장 진입점(facade) | [server/storage_gateway/](server/storage_gateway/__init__.py) ([README](server/storage_gateway/README.md), 키빌더 _s3.py) |
| 검색결과 UI / 세션 상세 UI | [report_analysis_index.html](server/report/report_analysis_index.html) / [report_view.html](server/report/report_view.html) + [static/webreport/](server/report/static/webreport/) (32모듈 — 로드 순서 정본은 [docs/11](docs/11_web_report_tabs.md)) |
| 랜딩 UI (/pe) — 서버 첫 화면 | [server/landing/landing.html](server/landing/landing.html) + [landing/__init__.py](server/landing/__init__.py) · 데이터는 [routes_misc.py](server/report/routes_misc.py) `GET /api/landing` |
| 관리 대시보드 (/pe/admin-pte/) | [server/admin_panel/](server/admin_panel/) (구 admin_routes.py 는 미등록 dead file) |
| eval 룰 관리 (/pe/eval) — threshold/signature 제품군별 편집·트레이스 | [server/eval_panel/](server/eval_panel/) + [web_report/eval_debug.py](web_report/eval_debug.py) → [docs/13 §11](docs/13_eval_analyzer_integration.md) |
| eval 표본 검수 → 승인형 룰 튜닝 (발화 전수 검토 대신 룰당 8건) | [server/eval_panel/review.py](server/eval_panel/review.py) · 수집 [web_report/eval_export.py](web_report/eval_export.py) `collect_session_snapshot` → [docs/13 §14](docs/13_eval_analyzer_integration.md) |
| **"룰을 고쳤는데 나아졌나" — eval 정확도·커버리지 추이** | 집계 [server/database/eval_stats.py](server/database/eval_stats.py)(cleanup 24h 편승, 덮어쓰기 UPSERT) · 저장 `report_eval_daily` · 화면 `/pe/eval` 채점 탭 추이 카드 · 라우트 `GET /pe/eval/api/eval/trend` → [docs/17](docs/17_eval_learning_loop.md) |
| **Input File 정보 (세션 상세 ℹ) / STDF 메타 요청 스펙** | [docs/21](docs/21_input_file_info.md) — 서버 [service.py](web_report/service.py) `input_info`(manifest 만 읽음) · 화면 [input_info.js](server/report/static/webreport/input_info.js) · 클라 수집 [source_name_dialog.py](client/honey_ui/source_name_dialog.py) `source_file_info` |
| 세션 **이름만** 수정 (상단바 인라인 편집) | `PATCH /session/<sid>/name` ([routes_session.py](server/report/routes_session.py)) + [sessions.py](server/database/sessions.py) `rename_session` → [docs/21 §5](docs/21_input_file_info.md). ⚠ `update_session_meta` 는 기준정보 14컬럼을 항상 덮는다 |
| 감사 기록 헬퍼 | [server/database/report_db.py](server/database/report_db.py) `log_audit` / `get_audit_logs` |
| **오류 추적 — "에러가 났는데 어디를 보나"** | 사건 저장소 [server/diagnostics.py](server/diagnostics.py) · 화면 [admin_panel/diagnostics_admin.py](server/admin_panel/diagnostics_admin.py) (`🚨 진단 사건` 탭) · 500/503 발급 [server/ops.py](server/ops.py) · 클라 수집 [routes_misc.py](server/report/routes_misc.py) `client_error`/`client_diagnostic` + [error_beacon.js](server/report/static/webreport/error_beacon.js) + [client/transport/error_report.py](client/transport/error_report.py) → [docs/20](docs/20_error_tracking.md) |
| **콜드 빌드가 300초 걸렸다 — 어디서 멎었나** | 실행 중 체크포인트 [web_report/build_log.py](web_report/build_log.py) `stage/checkpoint/read_states` + 회수 [compute.py](web_report/compute.py) `_dead_worker_state` → 실패 레코드의 `last_stage`/`last_source` → [docs/20](docs/20_error_tracking.md) |
| Honey 클라 (자유: honey_ui/honey_main/transport/excel_*) | [client/honey_main.py](client/honey_main.py), 업로드 [transport/uploader.py](client/transport/uploader.py), 추출 [report_flow/upload_prepare.py](client/report_flow/upload_prepare.py) |
| 외부 담당자 영역 동결 (무수정) | `d1/` · `client/report_generator/` · `client/honey_parse/` · `server/storage_gateway/` → [docs/15](docs/15_ownership.md) · 진입점 [INDEX §3.1](docs/INDEX.md) |
| eval_analyzer 연결 (AI Comment / 코멘트 export / 룰 트레이스) | [web_report/ai_comment.py](web_report/ai_comment.py) + [web_report/eval_export.py](web_report/eval_export.py) + [web_report/eval_debug.py](web_report/eval_debug.py) — eval_engine import **3곳** → [docs/13](docs/13_eval_analyzer_integration.md) |
| 기준정보(part_ids) 갱신 — DRM CSV → product_info.db | [tools/product_info_import/](tools/product_info_import/README.md) (Excel PC) → [server/product_info.py](server/product_info.py) 가 읽기전용 로드 |
| eval 룰 골든셋 회귀 (임계값 튜닝 전후 비교) | [tools/eval_golden/golden_check.py](tools/eval_golden/golden_check.py) (CLI) + [server/eval_panel/golden_io.py](server/eval_panel/golden_io.py) (패널 추가/실행) → [docs/13 §12](docs/13_eval_analyzer_integration.md) |
| **LLM 배선 (붙이는 곳·나가는 곳)** | 정본 [docs/19](docs/19_llm_wiring.md) — 설정은 [server/env/server.env](server/env/server.env) `EVAL_LLM_*` 5줄, 확인은 `python tools/llm_check.py --ping`. 소비자 2개(AI Comment [점검제안] = [llm_client.complete](eval_analyzer/eval_engine/llm_client.py) / 챗봇 질문해석 = [planner._call_llm](server/chatbot/planner.py)), 둘 다 꺼져도 폴백 동작. **외부 담당자 전달용**은 [eval_analyzer/docs/LLM_WIRING_HANDOFF.md](eval_analyzer/docs/LLM_WIRING_HANDOFF.md) |
| ENGR 이력 검색 챗봇 (자연어 → 조회 툴) | [server/chatbot/](server/chatbot/README.md) — 골든셋 [tests/chatbot_golden.yaml](tests/chatbot_golden.yaml), 백필 [tools/eval_backfill/](tools/eval_backfill/backfill_eval_db.py). ⚠ `eval_analyzer/chatbot_prototype/` 은 **보류된 별개 실험**(2026-08-10 개명 — 옛 이름 `chatbot` 이 이것과 충돌해 운영 장애) |
| 챗봇 웹 노출 (관리자 전용 플로팅 버튼) | 라우트 [server/report/routes_chat.py](server/report/routes_chat.py) · 위젯 [static/webreport/chat.js](server/report/static/webreport/chat.js) · 딥링크 `?tab=item_detail\|map&item=` ([boot.js](server/report/static/webreport/boot.js) `applyDeepLink`) · 사용현황/부하 = 관리자 Chatbot 탭([chatbot_admin.py](server/admin_panel/chatbot_admin.py), `report_chatbot_log`) |
| 더미 grids 픽스처 생성기 | [tests/sample_xlsx.py](tests/sample_xlsx.py) |
| web_report 성능 벤치 (이전 실행 대비 회귀 리포트) | [tests/bench_webreport.py](tests/bench_webreport.py) — 결과 `tests/bench_results/`(gitignore), 실행 절차 스킬 `.claude/skills/webreport-bench` |
| web_report 성능 회귀 가드 (지뢰 재밟기 차단 — 훅 자동) | [tools/perf_guard.py](tools/perf_guard.py) (`--list` 가 규칙 정본) + [.claude/settings.json](.claude/settings.json) → [docs/18](docs/18_perf_guard.md) |

---

## 7. Verification

E2E 동작 확인 순서는 [README.md](README.md) 의 "검증 절차" 참조.

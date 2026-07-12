# COINAPI report_server — Claude Code 진입점



> **세션 시작 규칙**: 새 대화가 시작될 때마다 [docs/INDEX.md](docs/INDEX.md)를
> 먼저 읽어라. 기능별 코드 흐름·파일 위치·불변 규칙이 모두 INDEX에 있다.

이 프로젝트는 Honey 클라이언트가 추출한 산출물을 서버로 업로드하고, Flask 서버가
SQLite + S3(또는 로컬 폴백)에 세션 단위로 저장한 뒤 검색결과·세션 상세 페이지로 조회하게
한다. 두 업로드 흐름이 병행한다: **xlsx 추출 grid**(→[docs/01](docs/01_server_upload.md))와
**web_report honeyform parquet**(→[docs/10](docs/10_web_report_pipeline.md), 현재 신규 개발의
주 대상).

**기능별 코드 흐름 추적은 [docs/INDEX.md](docs/INDEX.md) 참조** — 큰 기능별로 각 흐름을
정리한 작업용 메모. 무엇을 고칠지 정해지면 INDEX 표에서 해당 문서 1개만 열면 된다.

**외부 프로젝트 경계** (§0 트리에서 `(외부)` 표시): `d1/`·`d1_storage/`·S3·
`client/report_generator/`·`client/honey_parse/` 는 이 repo 밖에서 소유·교체되는 외부
프로젝트다.
- `report_generator`·`honey_parse` 는 **무수정이 원칙** — 수정이 불가피하면 반드시 사전 승인.
- `D1`·`S3` 는 **현재 코드에서 검증용으로만 사용**한다 (D1=로컬 `d1_storage` 검색 =
  Honey.exe 호환성 테스트, S3=미설정 시 로컬 폴백). 진입점·유지 계약 상세는
  [docs/INDEX.md §3.1](docs/INDEX.md).

**수정 권한 경계** (§5 규칙 #7):
- **자유 수정**: `web_report/` + web_report 관련 html(report_view.html, static/webreport/) + `server/`
- **사전 승인 필요**: `client/` 나머지 (honey_main.py, honey_ui/, transport/, report_flow/, excel_* 등)
- **무수정 원칙**: 외부 프로젝트 4종(위)

---

## 0. 디렉토리 인덱스

```
report_server/
├── server/                     Flask 서버 (자유 수정 — 상세 [server/README.md](server/README.md))
│   ├── wsgi.py                  진입점 (컴퓨트 워커 __mp_main__ 재임포트 스킵 가드)
│   ├── plugin.py                register_report_server() — Blueprint 3개 + admin_panel + ops 등록
│   ├── config.py                환경변수·경로 통합 설정
│   ├── auth_identity.py         신원 provider 체인 (기본 HoneyUser UA, AUTH_SSO_HEADER 로 SSO 전환)
│   ├── database/                SQLite 계층 (report_db.py 는 재노출 facade — 호출부 무변경)
│   │   ├── core.py              SCHEMA(정본)·마이그레이션·get_conn·analysis lock
│   │   ├── sessions.py / objects.py / audit.py / users.py / annotations.py
│   │   ├── webreport_edits.py   web_report 편집 상태 (세션 단위)
│   │   └── models.py            Session dataclass (Mapping 호환)
│   ├── report/
│   │   ├── report_extension.py  report_bp 정의 + DB init + web_report 저장 포트 주입
│   │   ├── report_routes.py     라우트 집결자 — 구현은 security.py /
│   │   │                        routes_session.py / routes_webreport.py / routes_misc.py
│   │   ├── static/webreport/    세션 상세 JS 모듈 15개 (순서 로드, 전역 공유)
│   │   ├── report_analysis_index.html  검색결과 페이지
│   │   └── report_view.html     세션 상세 (마크업+CSS — JS 는 static/webreport/)
│   ├── storage_gateway/         S3 산출물 저장 단일 진입점 (ENTRYPOINT/EXTERNAL_OWNER, S3=외부)
│   │   ├── __init__.py          facade (공개 API + 예외 재노출 + 저장 위치 기록)
│   │   ├── routes.py            이미지 URL 라우트
│   │   ├── _s3.py              boto3 호환 client + key 빌더 (내부 어댑터)
│   │   └── _issue_images.py    이슈 이미지 백엔드 (S3+로컬 폴백)
│   ├── admin_panel/             /pe/admin-<secret>/ 대시보드 + metrics 샘플러
│   ├── tools/migrate_manifest_edits.py  manifest 편집값 → 세션 편집 DB 이전 (운영 1회 실행 완료)
│   ├── upload_xlsx.py           POST /pe/report/upload_xlsx
│   ├── upload_webreport.py      POST /pe/report/upload_webreport (web_report.ingest 호출)
│   ├── xlsx_parser.py           시트 grid → 텍스트 추출 (_GridSheet 셸, openpyxl 미사용)
│   ├── admin_routes.py          [미등록 dead file — 구 /pe/admin, admin_panel 로 흡수됨]
│   ├── honey_routes.py          /honey/version, /honey/download
│   └── releases/version.json    Honey 배포 manifest
├── web_report/                 웹 리포트 구현 (신규 개발 주 대상 — [web_report/CLAUDE.md](web_report/CLAUDE.md),
│                                docs/[10](docs/10_web_report_pipeline.md)·[11](docs/11_web_report_tabs.md)·[12](docs/12_web_report_cache.md))
├── client/                     Honey 클라이언트 (PyQt6 — 사전 승인 필요)
│   ├── honey_main.py            메인 윈도우 + 워크플로
│   ├── transport/              uploader / version_check / updater / retry
│   ├── report_flow/            upload_prepare.py (Excel COM 추출) 등
│   ├── report_generator/       (외부·무수정) 분석·xlsx 생성 — 사용자 담당
│   ├── honey_parse/            (외부·무수정) file_to_df 파서 — 현재 더미 폴백
│   ├── honey_ui/               다이얼로그·위젯
│   ├── excel_download/ · excel_edit/   Excel COM 헬퍼
│   ├── embedded_browser.py     HoneyUser UA 삽입 내장 브라우저
│   ├── client_identity.py      PC 계정/호스트 신고값
│   └── config.py               SERVER_BASE_URL, CURRENT_VERSION
├── d1/                         (외부·무수정) D1 입력 provider 경계 — 검증용(로컬 d1_storage 검색)
├── d1_storage/                 (외부) D1 로컬 검증 스토리지
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
     상세 [docs/10](docs/10_web_report_pipeline.md).

**검색결과 조회 / 편집**
- `GET /pe/report/` → 검색결과 페이지, `GET /pe/report/api/history?product_type=MDDI&...` → 세션 목록
- `GET /pe/report/view/<session_id>` → 세션 상세
- `GET /pe/report/session/<sid>/full` → 세션 + summary + objects + annotations + 추출 텍스트
  (응답 session 에서 password 제거, `has_password` 불린만 노출)
- `POST /pe/report/session/<sid>/verify_password` → **하위호환 스텁** (HoneyUser 신원==업로더
  확인만, 항상 `has_password:false` — 구 PIN 검사 폐지)
- `PATCH /pe/report/session/<sid>/content` → [비활성] 항상 405 (구 xlsx 텍스트 수정 폐기)
- `POST .../session/<sid>/web_report/issue_table/comments|etc`, `.../summary/engr`,
  `.../trim/overrides` → web_report 편집 — **세션 편집 DB(report_webreport_edit)에 저장**.
  manifest 는 업로드 시점 불변 스냅샷.
- `DELETE /pe/report/session/<sid>` → 세션 삭제 (업로더만)

**인증**: 신원은 Honey 내장 브라우저 User-Agent 의 `HoneyUser/<계정>` 토큰으로 자동 식별한다
([server/auth_identity.py](server/auth_identity.py) — env `AUTH_SSO_HEADER` 설정 시 역프록시
SSO 헤더가 우선, 코드 무변경 전환). 일반 브라우저는 신원이 없어 **읽기 전용**. 가드 2단계
([server/report/security.py](server/report/security.py)): `_uploader_guard`(삭제·비공개·편집자
부여 — 업로더 전용) / `_editor_guard`(콘텐츠 편집 — 업로더 또는 위임 편집자). **legacy 우회**:
`uploaded_by` 가 빈 세션(= xlsx 업로드 세션)은 Honey 접속 사용자 전원이 편집/삭제 가능하고,
`uploaded_by` 를 채우는 web_report 세션만 업로더 잠금이 실효한다. 구 4자리 password
(`report_session.password`)는 저장·전송되나 **접근제어에 미사용**(선택 입력, 형식만 검사).
신원/password 는 analysis_key 산출 meta 에 **포함하지 않음** (규칙 #4). 접근제어 상세는
[docs/02](docs/02_server_query_edit.md).

---

## 2. DB 스키마 요약 (현행)

**정본은 [server/database/core.py](server/database/core.py) 의 `SCHEMA`.** 전체 테이블·컬럼은
[docs/03](docs/03_storage.md) 와 스냅샷 [DB/pe/report/report_README.md](DB/pe/report/report_README.md)
참조. 테이블 16개 요지:

- `report_session` — 세션 1건. `source`('xlsx_upload'|'web_report'), `mode`('Normal' 기본),
  `uploaded_by`·`client_host`(신원), `webreport_options`, `password`(미사용 보존) 컬럼 포함.
- `report_analysis_summary` — yield/cpk 등 summary 행 (UNIQUE analysis_key,item,bin).
- `report_object_info` — S3/로컬 산출물 위치. `options_json` 에 `{"storage":"s3"|"local"}` 기록.
  `object_type`: `distribution_combined`, `web_report_source_<idx>`, `web_report_manifest`,
  (legacy) `summary_text`/`issue_table_text`/`chart_index` 등.
- `report_sheet_data` — xlsx 추출 텍스트(summary/yield/issue_table). 텍스트는 여기에만 저장.
- `report_audit_log` — upload/edit/delete 감사. 메타 스냅샷 + client_ip/user_agent/client_user
  (클라 신고 계정, 위조 가능) + result. best-effort. `/pe/admin-pte/` 대시보드에서 조회.
- `report_webreport_edit` / `_rev` — web_report 편집(comment/etc/trim override/engr)의 **진실
  저장소, 세션 단위**. dedup(동일 analysis_key) 세션 간 편집 비공유. `rev` 는 단조 증가 캐시
  무효화 토큰. manifest 는 불변 스냅샷 ([web_report/edits.py](web_report/edits.py)).
- `report_session_editor`(편집 위임) / `report_web_visitor`(편집자 후보 풀) /
  `report_user_important`(개인 중요표시) / `report_user_favorite`(즐겨찾기).
- `report_annotation` / `report_dashboard_comment` / `report_csv_files` /
  `report_analysis_lock` / `report_user`(ID/PW 로그인 폐지 — 미사용 보존).

`product_type` enum: **MDDI / PDDI / PMIC / SECURITY / TCON** (core.py `_PRODUCT_TYPE_NAMES`).

---

## 3. S3 저장 (외부 프로젝트, 검증용)

S3 는 외부 경계다 — 미설정 시 `REPORT_UPLOAD_DIR` 로컬 폴백으로 동작하며 현재 코드에선
검증용이다. facade·키·저장 위치 기록 계약은 정본
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

- `HOST` 기본 **127.0.0.1** (LAN 노출은 `0.0.0.0`). `PORT` 기본 8000.
- `REPORT_S3_BUCKET` 미설정이면 S3 대신 **로컬 폴백**으로 조용히 동작한다(에러 아님). yield
  rows 등 DB 저장은 정상.
- `REPORT_CLEANUP_DRYRUN` 기본 **1(참)** — **기본은 실삭제 안 함**(대상만 로그). 실삭제하려면
  `0` 으로 명시.
- `AUTH_SSO_HEADER` 지정 시 그 역프록시 헤더가 신원으로 우선(기본은 HoneyUser UA).
- `WEB_REPORT_COMPUTE_WORKERS` 기본 2, `0` = 콜드 빌드 전부 인라인(구 동작).

DB 백업 사이클(db_backup.py)이 매회 `PRAGMA wal_checkpoint(TRUNCATE)` + `PRAGMA optimize` 로
-wal 비대를 막는다. VACUUM 은 장시간 잠금이라 자동 실행 안 함(수동).

---

## 5. 주의 사항 (불변 규칙)

1. 원본 xlsx 는 서버로 전송·저장하지 않는다. 클라이언트가 Excel COM 으로 추출한
   summary/yield/issue_table grid(JSON)와 issue PNG 만 업로드하고, 텍스트는 DB(sheet_data),
   PNG 는 S3(로컬 폴백)에 보관한다. **서버는 openpyxl·Excel 을 쓰지 않는다.**
2. `report_` prefix 없는 새 테이블 만들지 말 것.
3. analysis_key 산출은 `canonical(sheet_grids) + canonical(meta)` 의 sha256 — 메타 변경 시
   같은 데이터라도 다른 키가 됨. canonical 은 `json.dumps(sort_keys=True)`. password·신원·
   mode 는 **불포함**. (web_report 는 `sha256(canon({files, meta, selected_items}))` —
   [docs/10](docs/10_web_report_pipeline.md).)
4. 클라이언트 자동 업데이트는 batch 스크립트 + 외부 다운로드 방식. 실행 중인 exe
   에 직접 쓰지 말 것 (Windows 락).
5. **Distribution 차트 데이터 다운샘플링 절대 금지.**
   모든 데이터 포인트를 빠짐없이 차트에 표현해야 한다.
   `_MAX_CDF_POINTS`, `_downsample`, `max_points` 같은 포인트 상한 로직을 절대 추가하지 말 것.
   유일 예외: 동일값 구간을 2포인트 선분으로 표현하는 계단형(step) ECDF 변환
   (`client/report_generator/_builders.py` `cumulative_distribution_full()`), web_report 는
   미니셀 썸네일만 표시용 1000점 다운샘플([docs/11](docs/11_web_report_tabs.md)).
6. **web_report 편집 상태는 세션 편집 DB 가 진실.** manifest 는 업로드 시점 불변 스냅샷이므로
   편집으로 재저장하지 않는다. 캐시 키는 항상 [cache_policy.py](web_report/cache_policy.py)
   빌더로 만든다(즉석 조립 금지 — [docs/12](docs/12_web_report_cache.md)).
7. **수정 권한 경계.** 파일 위치에 따라 승인 요구가 다르다:
   - **자유 수정 (승인 불필요)**: `web_report/` + web_report 관련 html(`report_view.html`,
     `server/report/static/webreport/`) + `server/` 전반.
   - **사전 승인 필요**: `client/` 나머지(honey_main.py, honey_ui/, transport/, report_flow/,
     excel_* 등). Edit/Write 호출 전에 파일·이유·영향 범위를 설명하고 명시적 승인을 받는다.
   - **무수정 원칙 (외부 프로젝트)**: `d1/`·`d1_storage/`·S3(storage_gateway 내부 `_s3`)·
     `client/report_generator/`·`client/honey_parse/`. report_generator·honey_parse 는 수정
     불가피 시 반드시 사전 승인. D1·S3 는 현재 검증용(로컬 폴백)이며 진입점 계약만 유지.

---

## 6. 코드 포인터

| 알고 싶은 것 | 어디? |
|--------------|-------|
| API 엔드포인트·환경변수 (정본) | [server/README.md](server/README.md) |
| xlsx 업로드 라우트 | [server/upload_xlsx.py](server/upload_xlsx.py) · grid 파싱 [xlsx_parser.py](server/xlsx_parser.py) |
| web_report 업로드/파이프라인 | [server/upload_webreport.py](server/upload_webreport.py) → [docs/10](docs/10_web_report_pipeline.md) |
| 라우트 (세션/web_report/기타) | [routes_session.py](server/report/routes_session.py) / [routes_webreport.py](server/report/routes_webreport.py) / [routes_misc.py](server/report/routes_misc.py) |
| 접근제어·CSRF·가드 | [server/report/security.py](server/report/security.py) → [docs/02](docs/02_server_query_edit.md) |
| 신원/인증 (SSO 전환) | [server/auth_identity.py](server/auth_identity.py) |
| DB 스키마 (정본) | [server/database/core.py](server/database/core.py) `SCHEMA` (report_db.py 는 재노출 facade) |
| web_report 편집 상태 | [web_report/edits.py](web_report/edits.py) + [server/database/webreport_edits.py](server/database/webreport_edits.py) |
| web_report 캐시 키 규약 | [web_report/cache_policy.py](web_report/cache_policy.py) → [docs/12](docs/12_web_report_cache.md) |
| 새 탭 추가 (레지스트리) | [web_report/tabs/__init__.py](web_report/tabs/__init__.py) `TAB_REGISTRY` → [docs/11](docs/11_web_report_tabs.md) |
| S3 저장 진입점(facade) | [server/storage_gateway/](server/storage_gateway/__init__.py) ([README](server/storage_gateway/README.md), 키빌더 _s3.py) |
| 검색결과 UI / 세션 상세 UI | [report_analysis_index.html](server/report/report_analysis_index.html) / [report_view.html](server/report/report_view.html) + [static/webreport/](server/report/static/webreport/) (15모듈) |
| 관리 대시보드 (/pe/admin-pte/) | [server/admin_panel/](server/admin_panel/) (구 admin_routes.py 는 미등록 dead file) |
| 감사 기록 헬퍼 | [server/database/report_db.py](server/database/report_db.py) `log_audit` / `get_audit_logs` |
| Honey 클라 (사전 승인) | [client/honey_main.py](client/honey_main.py), 업로드 [transport/uploader.py](client/transport/uploader.py), 추출 [report_flow/upload_prepare.py](client/report_flow/upload_prepare.py) |
| 외부 프로젝트 (무수정) | `d1/` · `client/report_generator/` · `client/honey_parse/` → [docs/INDEX.md §3.1](docs/INDEX.md) |
| 더미 grids 픽스처 생성기 | [tests/sample_xlsx.py](tests/sample_xlsx.py) |

---

## 7. Verification

E2E 동작 확인 순서는 [README.md](README.md) 의 "검증 절차" 참조.

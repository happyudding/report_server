# COINAPI report_server — Claude Code 진입점



> **세션 시작 규칙**: 새 대화가 시작될 때마다 [docs/INDEX.md](docs/INDEX.md)를
> 먼저 읽어라. 기능별 코드 흐름·파일 위치·불변 규칙이 모두 INDEX에 있다.

이 프로젝트는 외부 report generator 가 만든 .xlsx 산출물을 Honey 클라이언트가 서버로
업로드하고, Flask 서버가 SQLite + S3 에 세션 단위로 저장한 뒤 검색결과 페이지로
조회할 수 있게 한다. 분석/플롯 파이프라인은 비활성화 상태이며 코드는 `_reference/`
에 보존되어 있다 (재활성화 시 참고용).

원본 plotly 프로젝트의 보다 자세한 아키텍처 메모는 `_reference/docs/AGENT_GUIDE.md`
참조. 이 문서는 신규 구조 요약본.

**기능별 코드 흐름 추적은 [docs/INDEX.md](docs/INDEX.md) 참조** — 프로젝트를 큰 기능 7개
(업로드/조회수정/저장소/Honey업데이트/클라UI/분석엔진/업로드전송)로 쪼개 각 흐름을
정리한 작업용 메모. 무엇을 고칠지 정해지면 INDEX 표에서 해당 문서 1개만 열면 된다.


**중요 규칙** : 기존코드는 최대한 건드리지 말고 필요하다면 entrypoint, endpoint 만 명확히 하고 세부 구현은 전부 .py 파일을 새로 만들어서 구현 하여 기존 프로젝트에 merge 하기 쉽도록(충돌이 일어날 가능성 최소화 하도록)한다 report_generator 및 기존 코드 내용을 수정할때는 사용자에게 꼭 다시 물어봐줘".

---

## 0. 디렉토리 인덱스

```
report_server/
├── server/                     Flask 서버
│   ├── wsgi.py                  진입점 (앱 조립 — 컴퓨트 워커 __mp_main__ 재임포트는 스킵 가드)
│   ├── config.py                환경변수·경로 통합 설정
│   ├── auth_identity.py         신원 provider 체인 (기본 HoneyUser UA, AUTH_SSO_HEADER 로 SSO 전환)
│   ├── start.bat / terminate.bat 로컬 기동·종료
│   ├── requirements.txt
│   ├── database/                SQLite 계층 (report_db.py 는 전량 재노출 facade — 호출부 무변경)
│   │   ├── core.py              SCHEMA·마이그레이션·get_conn·analysis lock
│   │   ├── sessions.py / objects.py / audit.py / users.py / annotations.py
│   │   ├── webreport_edits.py   web_report 편집 상태 (세션 단위)
│   │   └── models.py            Session dataclass (Mapping 호환 — get_session 반환 타입)
│   ├── report/
│   │   ├── report_extension.py  Blueprint 등록 + DB init + web_report 포트 주입(컴포지션 루트)
│   │   ├── report_routes.py     라우트 집결자 — 구현은 security.py /
│   │   │                        routes_session.py / routes_webreport.py / routes_misc.py
│   │   ├── static/webreport/    세션 상세 JS 모듈 15개 (report_view.html 에서 분할, 순서 로드)
│   │   ├── report_analysis_index.html  검색결과 페이지 (모달 없음)
│   │   ├── report_view.html     세션 상세 (마크업+CSS — JS 는 static/webreport/)
│   │   └── admin_dashboard.html 감사 로그 대시보드 (/pe/admin)
│   ├── storage_gateway/         S3 산출물 저장 단일 진입점 (ENTRYPOINT/EXTERNAL_OWNER)
│   │   ├── __init__.py          facade (공개 API + 예외 재노출 + 저장 위치 기록)
│   │   ├── routes.py            이미지 URL 라우트
│   │   ├── _s3.py              boto3 호환 client + key 빌더 (내부 어댑터)
│   │   ├── _issue_images.py    이슈 이미지 백엔드 (S3+로컬 폴백)
│   │   └── _png_drive.py       외부 호환 PNG 스캐폴드 (미사용)
│   ├── tools/migrate_manifest_edits.py  manifest 편집값 → 세션 편집 DB 일괄 이전 (운영 1회 실행 완료)
│   ├── upload_xlsx.py           /pe/report/upload_xlsx 라우트
│   ├── upload_webreport.py      /pe/report/upload_webreport 라우트
│   ├── xlsx_parser.py           시트 grid → 텍스트 추출 (_GridSheet 셸, openpyxl 미사용)
│   ├── admin_routes.py          /pe/admin 감사 로그 조회 (인증 없음, 내부망 전용)
│   ├── honey_routes.py          /honey/version, /honey/download
│   └── releases/version.json    Honey ZIP 배포 manifest
├── web_report/                 웹 리포트 구현 (신규 개발 중심 — 상세는 web_report/CLAUDE.md)
├── client/                     Honey 클라이언트 (PyQt5)
│   ├── honey_main.py            QMainWindow + upload 버튼
│   ├── version_check.py         /honey/version 폴링 + 다운로드
│   ├── uploader.py              multipart POST 헬퍼
│   ├── config.py                SERVER_BASE_URL, CURRENT_VERSION
│   ├── build_honey.spec         PyInstaller spec
│   └── requirements.txt
├── tests/sample_xlsx.py         더미 .xlsx 생성기
├── DB/pe/report/                런타임 자동 생성 (report.db)
├── uploads/                     런타임 임시 (현재 흐름에선 사용 안 함)
└── _reference/                  비활성 plotly 코드 보존 (분석/시각화/Dash)
    ├── analysis/                CSV 분석, table_builder
    ├── df_honey/                서버 없이 호출하는 분석 래퍼
    ├── server_legacy/           xlsx_export 등 — 시트 구조 기준
    ├── report_analysis_service.py
    ├── report_plot_service.py
    └── docs/, *.txt, CLAUDE.md.original
```

---

## 1. 데이터 흐름

**Honey → Server**
1. Honey 시작 → `GET /honey/version` → 새 버전 있으면 사용자에게 확인 후 `/honey/download`
2. 사용자가 product_type / product / lot_id + 4자리 PIN 입력 + xlsx 선택. 클라이언트가
   **Excel COM 으로 DRM 해제·시트 셀값을 읽어** summary/yield/issue_table 의 grid(2D 배열)와
   issue_table 행별 PNG 를 추출 → `POST /pe/report/upload_xlsx` (xlsx 파일은 보내지 않고
   `sheet_grids` JSON + `issue_img_<row>` 만 전송)
3. 서버: sha256(canonical(sheet_grids) + meta) → analysis_key → DB 세션 생성(PIN 저장) →
   grid 파싱 → yield_rows DB 저장, sheet_data(summary/yield/issue_table 텍스트) DB 저장,
   issue PNG 는 S3(또는 로컬 폴백) 보관. **원본 xlsx 는 서버에 저장하지 않는다.**

**검색결과 조회 / 편집**
- `GET /pe/report/` → 검색결과 페이지
- `GET /pe/report/api/history?product_type=MD&...` → 세션 목록 (source 컬럼 포함)
- `GET /pe/report/view/<session_id>` → 세션 상세 (보기/수정/삭제 모드)
- `GET /pe/report/session/<sid>/full` → 세션 + summary + objects + annotations + 추출 텍스트
  (응답 session 에서 password 제거, `has_password` 불린만 노출)
- `POST /pe/report/session/<sid>/verify_password` → 수정 진입 전 권한 확인
  (구 PIN 검사 폐지 — 현재는 HoneyUser 신원==업로더 검사, 응답 형태만 하위호환 유지)
- `PATCH /pe/report/session/<sid>/content` → [비활성] 항상 405 (구 xlsx 텍스트 수정 폐기)
- `POST .../session/<sid>/web_report/issue_table/comments|etc`, `.../summary/engr`,
  `.../trim/overrides` → web_report 편집 — **세션 편집 DB(report_webreport_edit)에 저장**
  (2026-07-11). manifest 는 업로드 시점 불변 스냅샷이라 편집으로 재저장하지 않는다.
- `DELETE /pe/report/session/<sid>` → 세션 삭제 (업로더만)

신원은 Honey 내장 브라우저 User-Agent 의 `HoneyUser/<계정>` 토큰으로 자동 식별한다
([server/auth_identity.py](server/auth_identity.py) provider 체인 — env `AUTH_SSO_HEADER`
설정 시 역프록시 SSO 헤더가 우선, 코드 무변경 전환). 일반 브라우저는 신원이 없어 읽기
전용. 수정·삭제는 업로더(콘텐츠 편집은 위임 편집자도 가능). 구 4자리 PIN
(`report_session.password`)은 미사용 보존. 신원/PIN 은 analysis_key 산출 meta 에
**포함하지 않음** (rule #4 유지).

---

## 2. DB 스키마 변경점 (legacy 대비)

- `report_session.source TEXT DEFAULT 'xlsx_upload'` 추가 — 'analyze'(legacy) /
  'xlsx_upload' 구분. SCHEMA + `_migrate()` 양쪽 반영.
- `create_session()` 에 `product`, `source` 파라미터 추가.
- `get_history()` 가 `source` 필터 지원, SELECT 에 `s.source` 포함.

`report_object_info.object_type` 종류:
- `summary_text` — summary 시트 추출 JSON
- `issue_table_text` — issue_table 시트 추출 JSON

(구 `source_xlsx` 는 폐지 — 클라가 추출 텍스트만 보내므로 원본 xlsx 를 보관하지 않는다.)

`report_audit_log` 테이블 추가 — 업로드/수정/삭제 감사 기록. action / session_id /
analysis_key / 메타 스냅샷(product_type·product·lot_id·file_name) / changed_fields(edit 시
변경 필드명) / client_ip / user_agent / result / created_at. SCHEMA 의 `CREATE TABLE IF NOT
EXISTS` 로 기존 DB 에도 자동 생성(별도 `_migrate()` 불필요). 기록은 best-effort —
삽입 실패가 본 업로드/수정/삭제를 깨뜨리지 않는다. 신원은 IP + User-Agent 만 (클라이언트가
사용자명을 보내지 않음). `/pe/admin` 대시보드에서 조회 (인증 없음, 내부망 전용).

`report_webreport_edit` / `report_webreport_edit_rev` 테이블 추가 (2026-07-11) —
web_report comment/ETC item/trim override/Engr comment 편집의 **진실 저장소, 세션 단위**
(dedup 으로 analysis_key 를 공유하는 세션끼리도 편집을 공유하지 않음 — 의도된 결정).
`rev` 는 단조 증가 캐시 무효화 토큰 (REPORT/TRIM//full 캐시 키에 포함). manifest 는
업로드 시점 불변 스냅샷으로 강등. legacy 세션(rev==0)은 조회 시 manifest 폴백 + 첫 편집
직전 자동 시드 ([web_report/edits.py](web_report/edits.py)). 스키마·CRUD 는
[server/database/core.py](server/database/core.py) /
[webreport_edits.py](server/database/webreport_edits.py).

---

## 3. S3 키 패턴 (config.py)

```
REPORT_S3_ISSUE_IMG_PREFIX → pe/report_server/issue_img/<analysis_key>/<row>.png
REPORT_S3_CHART_PREFIX     → pe/report_server/chart_png/<analysis_key>/...
```

(`REPORT_S3_SOURCE_XLSX_PREFIX` 는 원본 xlsx 보관 폐지로,
`REPORT_S3_SUMMARY_TEXT_PREFIX`/`ISSUE_TEXT`/`YIELD_TEXT` 는 세션 수정 기능
폐기(2026-07-09)로 제거됨 — 텍스트는 DB sheet_data 로만 저장. legacy 세션의
기존 S3 텍스트 객체는 report_object_info.s3_key 로 계속 읽는다.)

기존 plotly prefix (`pe/report/...`) 와 충돌 회피 위해 `pe/report_server/` 사용.

web_report parquet/manifest 는 저장 위치가 `report_object_info.options_json` 에
`{"storage":"s3"|"local"}` 로 기록되고 **조회는 그 기록을 따른다** (2026-07-11) — 기록이
s3 인데 다운로드가 실패하면 침묵 로컬 폴백 대신 예외를 올린다 (S3 순단 중 로컬 저장 →
복구 후 과거 S3 파일 부활 방지). 기록 없는 legacy 행만 종전 폴백(경고 로그 후 로컬)을
유지한다. manifest 는 업로드 후 불변 — 편집은 DB(§2).

---

## 4. 환경변수

서버:
```
HOST                  기본 0.0.0.0
PORT                  기본 8000
REPORT_DB_PATH        기본 <repo>/DB/pe/report/report.db
REPORT_S3_ENDPOINT    호환 endpoint (AWS면 비움)
REPORT_S3_BUCKET      필수 (비우면 S3 503)
REPORT_S3_REGION      기본 us-east-1
REPORT_S3_ACCESS_KEY  비우면 boto3 기본 자격증명
REPORT_S3_SECRET_KEY
HONEY_RELEASES_DIR    기본 <repo>/server/releases
AUTH_SSO_HEADER       기본 비움(HoneyUser UA 신원). 역프록시 SSO 신뢰 헤더명(예:
                      X-Auth-User) 지정 시 그 헤더가 신원으로 우선 사용됨 (auth_identity.py)
WEB_REPORT_COMPUTE_WORKERS  기본 2 — 콜드 report/dist/trim 빌드를 실행할 워커 프로세스 수.
                      0 = 전부 인라인(구 동작). tables 캐시가 따뜻하면 항상 인라인 (compute.py)
WEB_REPORT_TABLES_CACHE_MB  기본 4096 — decoded tables 캐시의 추정 바이트 상한 (개수 상한
                      WEB_REPORT_TABLES_CACHE 와 이중 적용, cache.py)
```

세션/DB 유지보수 (report_cleanup.py / db_backup.py):
```
REPORT_RETENTION_DAYS        기본 180 — 이보다 오래된 비중요 세션이 정리 대상
REPORT_CLEANUP_DRYRUN        기본 1(참) — 기본값은 실삭제 없이 대상만 로그.
                             운영에서 실제 삭제하려면 0 으로 명시해야 한다.
REPORT_AUDIT_RETENTION_DAYS  기본 365 — report_audit_log 롤오프(무한 증가 방지).
                             0 이하 = 무기한 보존. DRYRUN 과 무관하게 동작.
```
DB 백업 사이클(db_backup.py)이 매회 `PRAGMA wal_checkpoint(TRUNCATE)` + `PRAGMA optimize`
를 함께 수행해 -wal 파일 비대를 막는다. VACUUM 은 장시간 잠금이라 자동 실행하지 않음 —
파일 크기 회수가 필요하면 서버 중지 후 수동 실행할 것.

클라이언트:
```
HONEY_SERVER_URL      기본 http://127.0.0.1:8000
```

---

## 5. 주의 사항 (불변 규칙)

1. 원본 xlsx 는 서버로 전송·저장하지 않는다. 클라이언트가 Excel COM 으로 추출한
   summary/yield/issue_table grid(JSON)와 issue PNG 만 업로드하고, 텍스트는 DB(sheet_data),
   PNG 는 S3(로컬 폴백)에 보관한다. **서버는 openpyxl·Excel 을 쓰지 않는다.**
2. 분석 라우트(analyze/execute/plot/preview_items) 는 추가하지 않는다.
   필요해지면 `_reference/` 에서 코드를 가져와 별도 모듈에 활성화하고 라우트는
   `/pe/report/` 외부에 두지 말 것.
3. `report_` prefix 없는 새 테이블 만들지 말 것.
4. analysis_key 산출은 `canonical(sheet_grids) + canonical(meta)` 의 sha256 — 메타 변경 시
   같은 데이터라도 다른 키가 됨. canonical 은 `json.dumps(sort_keys=True)`.
   (구: `xlsx_bytes + meta`. xlsx 업로드 폐지로 추출 grid 기준으로 변경됨.)
5. 클라이언트 자동 업데이트는 batch 스크립트 + 외부 다운로드 방식. 실행 중인 exe
   에 직접 쓰지 말 것 (Windows 락).
6. **Distribution 차트 데이터 다운샘플링 절대 금지.**
   모든 데이터 포인트를 빠짐없이 차트에 표현해야 한다.
   `_MAX_CDF_POINTS`, `_downsample`, `max_points` 같은 포인트 상한 로직을
   절대 추가하지 말 것.
   유일하게 허용되는 최적화는 동일값 구간을 2포인트 선분으로 표현하는
   `cumulative_distribution_full()` 의 계단형(step) ECDF 변환뿐이다
   ([client/report_generator/_builders.py](client/report_generator/_builders.py)).
7. **web_report 중심 작업 경계 유지 (엄격 적용).**
   현재 신규 개발의 주 대상은 `web_report/` 내부 웹 리포트 구현이다.
   `web_report/` 밖의 모든 경로(`server/`, `client/`, `_reference/`, `tests/`, `docs/`
   등 report_server 전체)를 수정해야 할 경우, **실제로 Edit/Write 도구를 호출하기 전에
   반드시** 먼저 어떤 파일을 왜 바꿔야 하는지·영향 범위를 사용자에게 설명하고,
   사용자의 명시적 승인(대화상 확인 또는 AskUserQuestion)을 받을 때까지 해당 경로에는
   Edit/Write를 호출하지 않는다. 승인받은 파일/범위 밖의 다른 web_report 외부 파일은
   건드리지 않는다. 가능한 한 기존 코드는 변경하지 말고, 웹페이지 구현은 `web_report/`
   안에서 해결할 것.

---

## 6. 코드 포인터

| 알고 싶은 것 | 어디? |
|--------------|-------|
| 업로드 라우트 | [server/upload_xlsx.py](server/upload_xlsx.py) |
| grid 파싱 | [server/xlsx_parser.py](server/xlsx_parser.py) |
| 클라 Excel COM 추출 | [client/report_flow/upload_prepare.py](client/report_flow/upload_prepare.py) |
| 클라 업로드 전송 | [client/transport/uploader.py](client/transport/uploader.py) `post_grids` |
| Honey 다운로드 라우트 | [server/honey_routes.py](server/honey_routes.py) |
| DB 스키마 | [server/database/core.py](server/database/core.py) `SCHEMA` ([report_db.py](server/database/report_db.py) 는 재노출 facade) |
| 신원/인증 (SSO 전환) | [server/auth_identity.py](server/auth_identity.py) |
| web_report 편집 상태 (comment/override) | [web_report/edits.py](web_report/edits.py) + [server/database/webreport_edits.py](server/database/webreport_edits.py) |
| web_report 캐시 키 규약 | [web_report/cache_policy.py](web_report/cache_policy.py) (키 구성 단일 진실 — 빌더 필수 사용) |
| 컴퓨트 워커 풀 | [web_report/compute.py](web_report/compute.py) |
| 새 탭 추가 (레지스트리) | [web_report/tabs/__init__.py](web_report/tabs/__init__.py) `TAB_REGISTRY` |
| S3 키 빌더 | [server/storage_gateway/_s3.py](server/storage_gateway/_s3.py) |
| S3 저장 진입점(facade) | [server/storage_gateway/__init__.py](server/storage_gateway/__init__.py) ([README](server/storage_gateway/README.md)) |
| 검색결과 UI | [server/report/report_analysis_index.html](server/report/report_analysis_index.html) |
| 세션 상세 UI (마크업+CSS) | [server/report/report_view.html](server/report/report_view.html) |
| 세션 상세 JS 모듈 (탭별 15개) | [server/report/static/webreport/](server/report/static/webreport/) |
| 감사 로그 라우트 | [server/admin_routes.py](server/admin_routes.py) |
| 감사 로그 대시보드 UI | [server/report/admin_dashboard.html](server/report/admin_dashboard.html) |
| 감사 기록 헬퍼 | [server/database/report_db.py](server/database/report_db.py) `log_audit` / `get_audit_logs` |
| Honey 메인 윈도우 | [client/honey_main.py](client/honey_main.py) |
| 더미 grids 픽스처 생성기 | [tests/sample_xlsx.py](tests/sample_xlsx.py) |

---

## 7. Verification

E2E 동작 확인 순서는 [README.md](README.md) 의 "검증 절차" 참조.

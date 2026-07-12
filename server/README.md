# Flask 서버

Honey 클라이언트가 업로드한 산출물(xlsx 추출 grid / web_report parquet)을 수신·저장하고
브라우저 검색결과·세션 상세 페이지로 제공한다. 이 문서는 **환경변수·API 엔드포인트·모듈
구조의 정본**이다. 데이터 흐름·불변 규칙은 [../CLAUDE.md](../CLAUDE.md) 와
[../docs/INDEX.md](../docs/INDEX.md) 참조.

---

## 요구사항 / 실행

Python 3.10+. 의존성은 [requirements.txt](requirements.txt) 참조 (버전은 그 파일이 정본).
pyyaml 은 eval_analyzer(eval_engine) rules 로딩용 — ai_comment 옵션 세션의 IssueTable
AI Comment 평가 경로([../web_report/ai_comment.py](../web_report/ai_comment.py),
[../docs/13](../docs/13_eval_analyzer_integration.md))에서만 쓰인다.

```powershell
cd F:\COINAPI\report_server\server
pip install -r requirements.txt

.\start.bat        # 또는 python wsgi.py
```

기동 후 `http://127.0.0.1:8000/pe/report/` 에서 검색결과 페이지 확인. LAN 전체 노출은
`HOST=0.0.0.0`.

---

## 환경변수

### 서버 기동 / 경로

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `HOST` | `127.0.0.1` | 바인드 주소 (`0.0.0.0` = 모든 인터페이스) |
| `PORT` | `8000` | 포트 |
| `SERVER_BASE_URL` | `http://<HOST>:<PORT>` | 절대 URL 생성 기준 |
| `REPORT_DB_PATH` | `<repo>/DB/pe/report/report.db` | SQLite DB 파일 |
| `REPORT_UPLOAD_DIR` | `<repo>/uploads/report` | 업로드/로컬 폴백/디스크 캐시 루트 |
| `HONEY_RELEASES_DIR` | `<repo>/server/releases` | Honey exe 릴리스 폴더 |
| `STDINFO_DB_PATH` | `<repo>/DB/INFORMATION/stdinfo_*.db` | 외부 생성 기준정보 DB (part_ids 조회용) |

### 인증 (SSO 전환)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `AUTH_SSO_HEADER` | `""`(비움) | 비우면 Honey UA(`HoneyUser/<계정>`)로 신원 식별. 역프록시 SSO 헤더명(예 `X-Auth-User`) 지정 시 그 헤더가 우선 ([auth_identity.py](auth_identity.py)) |

### S3 (선택 — 외부 스토리지, 현재 코드에선 검증용. 미설정 시 로컬 폴백)

> S3 는 **외부 프로젝트** 경계다([storage_gateway/README.md](storage_gateway/README.md)).
> 미설정(`REPORT_S3_BUCKET` 비움)이면 산출물은 `REPORT_UPLOAD_DIR` 로컬에 저장되고 조회도
> 로컬을 따른다. yield rows 등 DB 저장은 S3 와 무관하게 정상.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `REPORT_S3_BUCKET` | `""` | 버킷 (비우면 로컬 폴백) |
| `REPORT_S3_ENDPOINT` | `""` | 호환 엔드포인트 (AWS 는 비움) |
| `REPORT_S3_REGION` | `us-east-1` | 리전 |
| `REPORT_S3_ACCESS_KEY` / `REPORT_S3_SECRET_KEY` | `""` | 비우면 boto3 기본 자격증명 |
| `REPORT_S3_MAX_POOL_CONNECTIONS` | `30` | boto3 커넥션 풀 |

S3 키 prefix(`REPORT_S3_*_PREFIX`, 모두 `pe/report_server/` 네임스페이스)는
[config.py](config.py) 와 [storage_gateway/README.md](storage_gateway/README.md) 참조.

### web_report 캐시 / 컴퓨트

캐시 계층·환경변수 전체는 [../docs/12_web_report_cache.md](../docs/12_web_report_cache.md) 가
정본. 자주 만지는 것: `WEB_REPORT_COMPUTE_WORKERS`(기본 2, 0=인라인),
`WEB_REPORT_TABLES_CACHE_MB`(기본 4096), `WEB_REPORT_DISK_CACHE_MAX_GB`(기본 500).

### 세션/DB 유지보수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `REPORT_RETENTION_DAYS` | `180` | 이보다 오래된 비중요 세션이 정리 대상 |
| `REPORT_CLEANUP_DRYRUN` | `1`(참) | **기본은 실삭제 안 함**(대상만 로그). 실삭제는 `0` 으로 명시 |
| `REPORT_AUDIT_RETENTION_DAYS` | `365` | 감사 로그 롤오프. 0 이하 = 무기한 |
| `REPORT_DB_BACKUP_ENABLED` / `_INTERVAL_HOURS` / `_KEEP` / `_DIR` | `1` / `24` / `7` / `<db>/backup` | 온라인 백업 사이클 |
| `REPORT_ADMIN_SECRET` | `pte` | admin 경로 조각 → `/pe/admin-<secret>/` (기본 `/pe/admin-pte/`) |

---

## API 엔드포인트

신원은 `HoneyUser/<계정>` User-Agent 로 자동 식별한다(일반 브라우저 = 신원 없음 = 읽기
전용). 접근 수준: **공개**(누구나) / **Honey**(Honey 접속 사용자) / **업로더**(세션 업로더
본인) / **편집자**(업로더 또는 위임받은 편집자). 브라우저 변경요청은 CSRF double-submit
쿠키, Honey 전용 업로드는 `X-Honey-Agent` 헤더로 구분. 상세 가드는
[../docs/02_server_query_edit.md](../docs/02_server_query_edit.md) 참조.

### 업로드 (`/pe/report/`) — Honey 클라이언트 전용

| 메서드 | 경로 | 접근 | 설명 |
|--------|------|------|------|
| `POST` | `/upload_xlsx` | Honey | xlsx 추출 grid(JSON) + issue PNG 업로드 |
| `POST` | `/upload_webreport` | Honey | web_report honeyform parquet + manifest 업로드 |
| `GET` | `/web_report/<sid>` | 공개 | `/view/<sid>` 로 리다이렉트 |

### 세션 조회/변경 (`/pe/report/`)

| 메서드 | 경로 | 접근 | 설명 |
|--------|------|------|------|
| `GET` | `/` | 공개 | 검색결과 페이지 (HTML) |
| `GET` | `/view/<sid>` | 공개 | 세션 상세 페이지 (HTML) |
| `GET` | `/api/history` | 공개 | 세션 목록 JSON (필터: product_type/product/lot_id/source) |
| `GET` | `/api/part_ids` | 공개 | 기준정보 part id 목록 (stdinfo DB) |
| `GET` | `/result/<sid>` | 공개 | 세션 요약 JSON |
| `GET` | `/session/<sid>` | 공개 | 세션 메타 JSON (password 제거, has_password 만) |
| `GET` | `/session/<sid>/full` | 공개 | 세션 전체 데이터 JSON (summary+objects+주석+추출텍스트) |
| `GET` | `/session/<sid>/my_access` | Honey | 현재 사용자의 이 세션 권한 |
| `DELETE` | `/session/<sid>` | 업로더 | 세션 삭제 |
| `POST` | `/session/<sid>/important` | Honey | 개인 중요표시 토글 |
| `POST` | `/session/<sid>/private` | 업로더 | 비공개 토글 |
| `POST` | `/session/<sid>/verify_password` | Honey | **하위호환 스텁** — UA 업로더 확인만, 항상 `has_password:false` |
| `PATCH` | `/session/<sid>/content` | — | **비활성, 항상 405** (구 xlsx 텍스트 수정 폐기) |
| `GET`/`POST` | `/session/<sid>/editors` | 업로더 | 편집자 위임 조회/부여 |
| `DELETE` | `/session/<sid>/editors/<user>` | 업로더 | 편집자 회수 |
| `GET` | `/session/<sid>/editors/candidates` | 업로더 | 편집자 후보(web_visitor 풀) |

### web_report 데이터/편집 (`/pe/report/session/<sid>/web_report/`)

조회는 공개, 편집(`edit`/`overrides`/`etc`/`comments`/`engr`/`rawdata_replace`)은 CSRF +
편집자 가드. 계약 상세는 [../docs/11_web_report_tabs.md](../docs/11_web_report_tabs.md).

| 메서드 | 경로 | 접근 | 설명 |
|--------|------|------|------|
| `GET` | `/raw_data/columns`, `/raw_data` | 공개 | Raw Data 컬럼 UI / 조회 |
| `POST` | `/raw_data/edit` | 편집자 | Raw Data 셀 편집 (parquet 재인코딩) |
| `GET` | `/distribution` | 공개 | Distribution ECDF (컴팩트 gzip, 전 포인트) |
| `GET` | `/scatter/<subject>` | 공개 | 항목 상세 산포 (전 측정값) |
| `GET` | `/trim_analysis`, `/trim_chart` | 공개 | Trim 매칭·통계 / 그룹 차트 (gzip+ETag) |
| `POST` | `/trim/overrides` | 편집자 | Trim 수동 재배치 저장 |
| `GET` | `/commonality/chips`, `/commonality/chip` | 공개 | Commonality chip 검색 / 백분위 |
| `POST` | `/issue_table/etc`, `/issue_table/comments`, `/summary/engr` | 편집자 | Issue/Summary 편집 |
| `GET` | `/rawdata_export` | 공개 | Raw Data CSV 내보내기 |
| `POST` | `/rawdata_replace` | 편집자 | Raw Data 소스 교체 |

### 주석 / 즐겨찾기 / 인증 스텁 (`/pe/report/`)

| 메서드 | 경로 | 접근 | 설명 |
|--------|------|------|------|
| `POST`/`GET`/`PATCH`/`DELETE` | `/annotation`, `/annotation/<sid>`, `/annotation/<aid>` | 공개* | 주석 CRUD (*변경은 CSRF) |
| `GET`/`POST` | `/api/favorites` | Honey | 개인 즐겨찾기 |
| `POST` | `/api/auth/login` | 공개 | **폐지 스텁** — 비밀번호 확인 없이 UA 사용자 반환 |
| `POST` | `/api/auth/change_password` | — | **410 Gone** |
| `POST`/`GET` | `/api/auth/logout`, `/api/auth/me` | 공개 | 신원 확인 스텁 |
| `GET` | `/_threads` | 공개 | 진단 (스레드 덤프) |

### 이미지 스트리밍 (`/pe/report/`, storage_gateway)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET` | `/chart/<sid>/<idx>` | 차트 PNG |
| `GET` | `/issue_image/<sid>/<row>` | 이슈 이미지 |
| `GET` | `/distribution_combined/<sid>` | 합성 분포 PNG |

### Honey 업데이트 (`/honey/`)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET` | `/honey/version` | 버전 정보 JSON (`version.json` 반환) |
| `GET` | `/honey/download` | Honey exe/ZIP 다운로드 |

### 관리 대시보드 (`/pe/admin-<secret>/`, 기본 `/pe/admin-pte/`) — 인증 없음, 내부망 전용

비-GET 요청은 `X-Admin-Request: 1` 헤더 요구. `GET /` 대시보드 + `GET /api/*`
(health/storage/s3-status/metrics/stats/sessions/users/audit(.csv)/logs/tail) +
`POST /api/*` (sessions/delete, session/<sid>/important·password, db/backup·cleanup 등).
구 공개 `/pe/admin` (`admin_routes.py`)은 **미등록 dead file**.

### 기타

`GET /healthz` (ops), `GET /` (root_redirect → `/pe/report/`).

---

## 모듈 구조

```
server/
├── wsgi.py                   WSGI 진입점 (컴퓨트 워커 __mp_main__ 재임포트 스킵 가드)
├── plugin.py                 register_report_server() — Blueprint 3개 + admin_panel + ops 등록
├── config.py                 환경변수·경로 통합 설정 (정본)
├── auth_identity.py          신원 provider 체인 (HoneyUser UA 기본 / AUTH_SSO_HEADER SSO)
├── upload_xlsx.py            POST /upload_xlsx 핸들러
├── upload_webreport.py       POST /upload_webreport 핸들러 (web_report.ingest 호출)
├── xlsx_parser.py            시트 grid → 텍스트 추출 (_GridSheet 셸, openpyxl 미사용)
├── honey_routes.py           /honey/version, /honey/download
├── admin_routes.py           [미등록 dead file — /pe/admin 구현, admin_panel 로 흡수됨]
├── report_cleanup.py         오래된 세션·감사로그 정리 (DRYRUN 기본)
├── db_backup.py              report.db 온라인 백업 사이클
├── report/
│   ├── report_extension.py   report_bp 정의 + DB init + web_report 저장 포트 주입
│   ├── report_routes.py      라우트 집결자 (구현은 아래 4모듈)
│   ├── security.py           CSRF·신원 가드(_uploader_guard/_editor_guard)·감사 헬퍼
│   ├── routes_session.py     세션 조회/삭제/권한·편집자 위임 라우트
│   ├── routes_webreport.py   web_report 데이터/편집 라우트
│   ├── routes_misc.py        페이지·history·주석·favorites·auth 스텁·정적
│   ├── static_pages.py       검색결과/상세 HTML 서빙 헬퍼
│   ├── static/webreport/     세션 상세 JS 모듈 (탭별, 순서 로드)
│   ├── report_analysis_index.html  검색결과 페이지
│   ├── report_view.html      세션 상세 (마크업+CSS)
│   └── admin_dashboard.html  구 감사로그 대시보드 (admin_panel 로 대체)
├── database/                 SQLite 계층 (report_db.py 는 재노출 facade)
│   ├── core.py               SCHEMA(정본)·마이그레이션·get_conn·analysis lock
│   ├── sessions.py / objects.py / audit.py / users.py / annotations.py
│   ├── webreport_edits.py    web_report 편집 상태 (세션 단위)
│   └── models.py             Session dataclass (Mapping 호환)
├── storage_gateway/          S3 산출물 저장 단일 진입점 (ENTRYPOINT/EXTERNAL_OWNER)
│   ├── __init__.py           facade (공개 API + 예외 + 저장 위치 기록)
│   ├── routes.py             이미지 URL 라우트
│   ├── _s3.py               boto3 어댑터 + 키 빌더 (내부)
│   └── _issue_images.py     이슈 이미지 (S3+로컬 폴백)
├── admin_panel/              /pe/admin-<secret>/ 대시보드 + metrics 샘플러
│   ├── __init__.py           register_admin_panel() + metrics.init_app
│   ├── routes.py / sysinfo.py / stats.py / sessions_admin.py / users_admin.py
│   ├── maintenance.py / metrics.py / admin_panel.html
├── tools/migrate_manifest_edits.py  manifest 편집값 → 세션 편집 DB 이전 (운영 1회 실행 완료)
└── releases/version.json     Honey 배포 manifest
```

`web_report/` 패키지(honeyform 처리·탭 계산·캐시)는 server/ 밖에 있으며 blueprint 가 아니라
`report_routes` 가 직접 import 한다. 상세는
[../docs/10_web_report_pipeline.md](../docs/10_web_report_pipeline.md).

---

## DB 초기화

서버 시작 시 `database/core.py` 의 `SCHEMA`(`CREATE TABLE IF NOT EXISTS`)로 자동 생성·
마이그레이션. 테이블 16개 — 목록·컬럼은 [../docs/03_storage.md](../docs/03_storage.md) 와
[../DB/pe/report/report_README.md](../DB/pe/report/report_README.md)(스냅샷) 참조. **스키마
정본은 항상 `database/core.py`.**

---

## 참조 문서

| 내용 | 문서 |
|------|------|
| 업로드 파이프라인 (xlsx) | [docs/01_server_upload.md](../docs/01_server_upload.md) |
| 조회·수정·삭제 라우트 + 접근제어 | [docs/02_server_query_edit.md](../docs/02_server_query_edit.md) |
| SQLite 스키마 + storage_gateway | [docs/03_storage.md](../docs/03_storage.md) |
| storage_gateway facade 교체 가이드 | [storage_gateway/README.md](storage_gateway/README.md) |
| web_report 파이프라인 / 탭 / 캐시 | [docs/10](../docs/10_web_report_pipeline.md) · [11](../docs/11_web_report_tabs.md) · [12](../docs/12_web_report_cache.md) |

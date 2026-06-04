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

---

## 0. 디렉토리 인덱스

```
report_server/
├── server/                     Flask 서버
│   ├── wsgi.py                  진입점 (report_bp + honey_bp + admin_bp 등록)
│   ├── config.py                환경변수·경로 통합 설정
│   ├── start.bat / terminate.bat 로컬 기동·종료
│   ├── requirements.txt
│   ├── database/report_db.py   SQLite 스키마·CRUD·락
│   ├── report/
│   │   ├── report_extension.py  Blueprint 등록 + DB init
│   │   ├── report_routes.py     검색결과·세션·주석 (분석 라우트 모두 제거됨)
│   │   ├── report_analysis_index.html  검색결과 페이지 (모달 없음)
│   │   ├── report_view.html     세션 상세 (text only)
│   │   └── admin_dashboard.html 감사 로그 대시보드 (/pe/admin)
│   ├── s3_storage/report_s3.py  boto3 호환 client + key 빌더
│   ├── upload_xlsx.py           /pe/report/upload_xlsx 라우트
│   ├── xlsx_parser.py           openpyxl 기반 텍스트 추출
│   ├── admin_routes.py          /pe/admin 감사 로그 조회 (인증 없음, 내부망 전용)
│   ├── honey_routes.py          /honey/version, /honey/download
│   └── releases/version.json    Honey exe 배포 manifest
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
2. 사용자가 product_type / product / lot_id + 4자리 PIN 입력 + xlsx 선택 → `POST /pe/report/upload_xlsx`
3. 서버: sha256(xlsx + meta) → analysis_key → S3 업로드 → DB 세션 생성(PIN 저장) → xlsx 파싱 →
   yield_rows DB 저장, summary/issue_table 텍스트는 S3 JSON 으로 보관

**검색결과 조회 / 편집**
- `GET /pe/report/` → 검색결과 페이지
- `GET /pe/report/api/history?product_type=MD&...` → 세션 목록 (source 컬럼 포함)
- `GET /pe/report/view/<session_id>` → 세션 상세 (보기/수정/삭제 모드)
- `GET /pe/report/session/<sid>/full` → 세션 + summary + objects + annotations + 추출 텍스트
  (응답 session 에서 password 제거, `has_password` 불린만 노출)
- `POST /pe/report/session/<sid>/verify_password` → 수정 모드 진입 전 PIN 확인
- `PATCH /pe/report/session/<sid>/content` → 텍스트 콘텐츠 수정 (PIN 검증 후
  summary_text / issue_rows = S3 JSON 재업로드, yield_rows = DB 행 치환)
- `DELETE /pe/report/session/<sid>` → 세션 삭제 (PIN 검증)

업로드 시 4자리 숫자 PIN 필수 (`report_session.password`). 수정·삭제는 PIN 일치 필요
(미설정 legacy 세션은 PIN 없이 허용). PIN 은 analysis_key 산출 meta 에 **포함하지 않음**
— 접근 제어용이라 같은 xlsx+meta 면 PIN 이 달라도 동일 analysis_key (rule #4 유지).

---

## 2. DB 스키마 변경점 (legacy 대비)

- `report_session.source TEXT DEFAULT 'xlsx_upload'` 추가 — 'analyze'(legacy) /
  'xlsx_upload' 구분. SCHEMA + `_migrate()` 양쪽 반영.
- `create_session()` 에 `product`, `source` 파라미터 추가.
- `get_history()` 가 `source` 필터 지원, SELECT 에 `s.source` 포함.

`report_object_info.object_type` 에 새 종류 3개:
- `source_xlsx` — 원본 xlsx S3 위치
- `summary_text` — summary 시트 추출 JSON
- `issue_table_text` — issue_table 시트 추출 JSON

`report_audit_log` 테이블 추가 — 업로드/수정/삭제 감사 기록. action / session_id /
analysis_key / 메타 스냅샷(product_type·product·lot_id·file_name) / changed_fields(edit 시
변경 필드명) / client_ip / user_agent / result / created_at. SCHEMA 의 `CREATE TABLE IF NOT
EXISTS` 로 기존 DB 에도 자동 생성(별도 `_migrate()` 불필요). 기록은 best-effort —
삽입 실패가 본 업로드/수정/삭제를 깨뜨리지 않는다. 신원은 IP + User-Agent 만 (클라이언트가
사용자명을 보내지 않음). `/pe/admin` 대시보드에서 조회 (인증 없음, 내부망 전용).

---

## 3. S3 키 패턴 (config.py)

```
REPORT_S3_SOURCE_XLSX_PREFIX  → pe/report_server/source_xlsx/<analysis_key>.xlsx
REPORT_S3_SUMMARY_TEXT_PREFIX → pe/report_server/summary_text/<analysis_key>.json
REPORT_S3_ISSUE_TEXT_PREFIX   → pe/report_server/issue_table_text/<analysis_key>.json
```

기존 plotly prefix (`pe/report/...`) 와 충돌 회피 위해 `pe/report_server/` 사용.

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
```

클라이언트:
```
HONEY_SERVER_URL      기본 http://127.0.0.1:8000
```

---

## 5. 주의 사항 (불변 규칙)

1. xlsx 본문은 SQLite 에 저장하지 않는다 — S3 + report_object_info(s3_key)
2. 분석 라우트(analyze/execute/plot/preview_items) 는 추가하지 않는다.
   필요해지면 `_reference/` 에서 코드를 가져와 별도 모듈에 활성화하고 라우트는
   `/pe/report/` 외부에 두지 말 것.
3. `report_` prefix 없는 새 테이블 만들지 말 것.
4. analysis_key 산출은 `xlsx_bytes + canonical(meta)` 의 sha256 — 메타 변경 시
   같은 xlsx 라도 다른 키가 됨. canonical 은 `json.dumps(sort_keys=True)`.
5. 클라이언트 자동 업데이트는 batch 스크립트 + 외부 다운로드 방식. 실행 중인 exe
   에 직접 쓰지 말 것 (Windows 락).

---

## 6. 코드 포인터

| 알고 싶은 것 | 어디? |
|--------------|-------|
| 업로드 라우트 | [server/upload_xlsx.py](server/upload_xlsx.py) |
| xlsx 파싱 | [server/xlsx_parser.py](server/xlsx_parser.py) |
| Honey 다운로드 라우트 | [server/honey_routes.py](server/honey_routes.py) |
| DB 스키마 | [server/database/report_db.py](server/database/report_db.py) |
| S3 키 빌더 | [server/s3_storage/report_s3.py](server/s3_storage/report_s3.py) |
| 검색결과 UI | [server/report/report_analysis_index.html](server/report/report_analysis_index.html) |
| 세션 상세 UI | [server/report/report_view.html](server/report/report_view.html) |
| 감사 로그 라우트 | [server/admin_routes.py](server/admin_routes.py) |
| 감사 로그 대시보드 UI | [server/report/admin_dashboard.html](server/report/admin_dashboard.html) |
| 감사 기록 헬퍼 | [server/database/report_db.py](server/database/report_db.py) `log_audit` / `get_audit_logs` |
| Honey 메인 윈도우 | [client/honey_main.py](client/honey_main.py) |
| 더미 xlsx 생성기 | [tests/sample_xlsx.py](tests/sample_xlsx.py) |

---

## 7. Verification

E2E 동작 확인 순서는 [README.md](README.md) 의 "검증 절차" 참조.

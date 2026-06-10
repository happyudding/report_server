# Flask 서버

Honey 클라이언트가 업로드한 xlsx를 수신·저장하고 브라우저 검색결과 페이지로 제공한다.

---

## 요구사항

- Python 3.10+

```powershell
cd F:\COINAPI\report_server\server
pip install -r requirements.txt
```

| 패키지 | 용도 |
|--------|------|
| `flask>=3.0` | 웹 프레임워크 |
| `werkzeug>=3.0` | WSGI 유틸리티 |
| `boto3>=1.34` | S3 업로드/다운로드 |
| `pillow>=10.0` | 이미지 처리 |

---

## 실행

```powershell
# 방법 1: 배치 스크립트
.\start.bat

# 방법 2: 직접 실행
python wsgi.py
```

서버 기동 후 `http://127.0.0.1:8000/pe/report/` 에서 검색결과 페이지 확인.

LAN 전체에 노출하려면 `HOST=0.0.0.0` 환경변수 설정.

---

## 환경변수

### 서버 기동

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `HOST` | `127.0.0.1` | 바인드 주소 (`0.0.0.0` = 모든 인터페이스) |
| `PORT` | `8000` | 포트 |
| `FLASK_DEBUG` | `0` | `1` 로 설정하면 디버그 모드 |

### 경로

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `REPORT_DB_PATH` | `<repo>/DB/pe/report/report.db` | SQLite DB 파일 경로 |
| `REPORT_UPLOAD_DIR` | `<repo>/uploads/report` | 임시 업로드 디렉토리 |
| `HONEY_RELEASES_DIR` | `<repo>/server/releases` | Honey exe 릴리스 폴더 |

### S3 (선택 — 미설정 시 grace-fail, yield rows DB 저장은 정상)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `REPORT_S3_BUCKET` | `""` | 버킷 이름 (비우면 S3 503) |
| `REPORT_S3_ENDPOINT` | `""` | 호환 엔드포인트 (AWS S3 는 비움) |
| `REPORT_S3_REGION` | `us-east-1` | 리전 |
| `REPORT_S3_ACCESS_KEY` | `""` | 액세스 키 (비우면 boto3 기본 자격증명) |
| `REPORT_S3_SECRET_KEY` | `""` | 시크릿 키 |

### S3 키 프리픽스 (보통 변경 불필요)

| 변수 | 기본값 |
|------|--------|
| `REPORT_S3_SUMMARY_TEXT_PREFIX` | `pe/report_server/summary_text` |
| `REPORT_S3_ISSUE_TEXT_PREFIX` | `pe/report_server/issue_table_text` |
| `REPORT_S3_CHART_PREFIX` | `pe/report_server/chart_png` |
| `REPORT_S3_ISSUE_IMG_PREFIX` | `pe/report_server/issue_img` |

---

## API 엔드포인트

### 리포트 (`/pe/report/`)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET` | `/pe/report/` | 검색결과 페이지 (HTML) |
| `GET` | `/pe/report/view/<session_id>` | 세션 상세 페이지 (HTML) |
| `GET` | `/pe/report/api/history` | 세션 목록 JSON (필터: product_type/product/lot_id) |
| `POST` | `/pe/report/upload_xlsx` | xlsx 업로드 (Honey 클라이언트 전용) |
| `GET` | `/pe/report/session/<session_id>/full` | 세션 전체 데이터 JSON (summary + objects + 주석) |
| `POST` | `/pe/report/session/<session_id>/verify_password` | PIN 검증 |
| `PATCH` | `/pe/report/session/<session_id>/content` | 세션 내용 수정 (PIN 필요) |
| `DELETE` | `/pe/report/session/<session_id>` | 세션 삭제 (PIN 필요) |
| `POST` | `/pe/report/annotation` | 주석 추가 |
| `GET` | `/pe/report/annotation/<session_id>` | 주석 목록 |
| `PATCH` | `/pe/report/annotation/<aid>` | 주석 수정 |
| `DELETE` | `/pe/report/annotation/<aid>` | 주석 삭제 |
| `GET` | `/pe/report/chart/<session_id>/<idx>` | 차트 PNG (S3 프록시) |
| `GET` | `/pe/report/issue_image/<session_id>/<row>` | 이슈 이미지 |
| `GET` | `/pe/report/distribution_combined/<session_id>` | 분포 차트 결합 이미지 |

### Honey 업데이트 (`/honey/`)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET` | `/honey/version` | 버전 정보 JSON (`version.json` 그대로 반환) |
| `GET` | `/honey/download` | Honey exe 파일 다운로드 |

### 관리 (`/pe/admin/`) — 인증 없음, 내부망 전용

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET` | `/pe/admin/` | 감사 로그 대시보드 (HTML) |
| `GET` | `/pe/admin/api/audit` | 감사 로그 JSON |

---

## 모듈 구조

```
server/
├── wsgi.py                   WSGI 진입점 — Flask app 생성, Blueprint 등록
├── plugin.py                 register_report_server() — Blueprint 일괄 등록
├── config.py                 환경변수·경로 통합 설정
├── upload_xlsx.py            POST /pe/report/upload_xlsx 핸들러
├── xlsx_parser.py            시트 grid → 텍스트 추출 (_GridSheet 셸, summary/yield/issue_table)
├── honey_routes.py           /honey/version, /honey/download
├── admin_routes.py           /pe/admin/ 감사 로그 조회
├── report_utils.py           공통 유틸리티 (타입 변환 등)
├── report/
│   ├── report_extension.py   Blueprint 정의 + DB init 트리거
│   ├── report_routes.py      조회·수정·삭제·주석 라우트
│   ├── report_analysis_index.html  검색결과 페이지
│   ├── report_view.html      세션 상세 페이지
│   └── admin_dashboard.html  감사 로그 대시보드
├── database/
│   └── report_db.py          SQLite 스키마 정의·CRUD·락·감사 로그 헬퍼
├── storage_gateway/          S3 저장소 단일 진입점 (ENTRYPOINT / EXTERNAL_OWNER)
│   ├── __init__.py           공개 API facade
│   ├── routes.py             이미지 URL 라우트
│   ├── _s3.py               boto3 어댑터 + S3 키 빌더
│   ├── _issue_images.py     이슈 이미지 (S3 + 로컬 폴백)
│   └── README.md            facade 교체 가이드
└── releases/
    └── version.json          Honey exe 배포 manifest
```

---

## DB 초기화

서버 시작 시 자동 실행. `REPORT_DB_PATH` 경로에 `report.db` 가 없으면 생성.

테이블 8개: `report_session`, `report_analysis_summary`, `report_annotation`,
`report_audit_log`, `report_csv_files`, `report_dashboard_comment`,
`report_object_info`, `report_sheet_data`.

---

## 참조 문서

| 내용 | 문서 |
|------|------|
| 업로드 파이프라인 상세 | [docs/01_server_upload.md](../docs/01_server_upload.md) |
| 조회·수정·삭제 라우트 | [docs/02_server_query_edit.md](../docs/02_server_query_edit.md) |
| SQLite 스키마 + storage_gateway | [docs/03_storage.md](../docs/03_storage.md) |
| storage_gateway facade 교체 가이드 | [storage_gateway/README.md](storage_gateway/README.md) |

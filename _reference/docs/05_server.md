# 05. 전체 서버 구동 블록

Flask 앱의 진입점과 두 Blueprint 등록, Dash 통합, 배치 스크립트.

---

## 1. 진입점 / 부팅 순서

[wsgi.py](../wsgi.py):
```python
from flask import Flask
from report_extension import report_bp     # ← import 시 report_db.init_report_db() 실행
from server import bp

app = Flask(__name__)
app.register_blueprint(bp)                 # / , /upload, /view/<id>, /api/<id>/...
app.register_blueprint(report_bp)          # /pe/report/...

try:
    from dash_dashboard import register_dash
    register_dash(app)                     # /dash/<id> 마운트
except RuntimeError as exc:
    app.config["DASH_REGISTER_ERROR"] = str(exc)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
```

**import 부수효과**:
- `report_extension` import → `report_db.init_report_db()` 호출 → SQLite 파일/스키마/PRAGMA 적용 + `report_routes` import 로 라우트 등록.
- 따라서 wsgi.py 실행만으로 DB 초기화 자동 처리.

---

## 2. Blueprint 두 개

### 2.1 `bp` (cumulative dashboard) — [server.py](../server.py)

| Method · Path | 핸들러 | 용도 |
|---------------|--------|------|
| `GET /` | `index` | `/view/current` 로 302 |
| `POST /upload` | `upload` | CSV 멀티파일 업로드 + 백그라운드 빌드 |
| `GET /view/<id>` | `view` | cumulative.html 또는 빌드 진행 placeholder |
| `GET /api/<id>/chart/<sid>` | `chart` | subject id 별 plotly JSON |
| `GET /api/<id>/thumb/<sid>` | `thumb` | subject SVG 썸네일 |
| `GET /api/<id>/fail_png/<sid>` | `fail_png` | dash_dashboard 의 fail PNG |
| `GET /api/<id>/raw_xlsx` | `raw_xlsx` | raw 데이터 엑셀 |
| `GET /api/<id>/report_xlsx` | `report_xlsx` | 종합 리포트 엑셀 (7 시트) |
| `GET /api/<id>/yield_comments` / `POST` | `yield_comments_get/post` | 코멘트 JSON |
| `GET /api/<id>/build_version` | | 빌드 버전 |
| `GET /api/<id>/build_status` | | 빌드 진행 폴링 |

`_build_status` 는 dict + Lock 으로 메모리 보관 — 프로세스 재시작 시 휘발.
빌드는 `threading.Thread(daemon=True)` 로 백그라운드 실행, `progress_cb` 콜백으로 단계별 통지.

### 2.2 `report_bp` (분석 모듈) — [report_routes.py](../report_routes.py)

`url_prefix = "/pe/report"`. 전체 엔드포인트는 [01_report_generation.md](01_report_generation.md#7-조회--복원-엔드포인트) 참고.

---

## 3. Dash 통합

[dash_dashboard.py](../dash_dashboard.py) 의 `register_dash(app)` 가 Flask 앱 위에 Dash 를 마운트.
- 경로: `/dash/<dataset_id>`
- 탭 구성: raw / yield / cpk / fail_items / distribution
- `dcc.Store` + `dash_table.DataTable` 기반 (서버 부하 최소화 — JSON 한 번만 fetch 후 클라이언트 렌더)

Dash 미설치 환경에서는 `RuntimeError` → `app.config["DASH_REGISTER_ERROR"]` 에 메시지 저장하고 나머지 서버는 정상 기동.

---

## 4. 배치 스크립트

### 4.1 start.bat

```bat
@echo off
set "PYTHON=C:\Users\sknsw\AppData\Local\Programs\Python\Python313\python.exe"
set "PORT=8000"

call terminate.bat                                    -- 기존 프로세스 종료
start "plotly-dashboard" "%PYTHON%" "wsgi.py"         -- 새 창에서 실행
timeout /t 2 /nobreak >nul
powershell -Command "Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/pe/report/' -UseBasicParsing -TimeoutSec 10"
start "" "http://127.0.0.1:%PORT%/pe/report/"
```

`PORT=8000` 고정. 다른 포트로 띄우려면 wsgi.py 의 `app.run(port=...)` 도 같이 수정.

### 4.2 terminate.bat

PowerShell `Get-NetTCPConnection -LocalPort 8000 -State Listen` 로 LISTEN PID 추출 → `Stop-Process -Force`.

### 4.3 build.py (CLI 직접 빌드)

서버 없이 CSV → dataset 생성:
```cmd
python build.py [dataset_id]
```
`INPUT_PATHS` 리스트(소스 안에 하드코딩)에 있는 CSV 들로 `build_dataset` 직접 호출. 결과는 `output/datasets/<id>/`.

---

## 5. 디렉토리 구조 (런타임 산출물)

```
plotly/
├── DB/pe/report/report.db          ← SQLite (report_extension 자동 생성)
├── uploads/report/<session_id>/    ← 임시 CSV (analyze 후 삭제)
├── data/                           ← 디버그용 입력 CSV (a/b/c_school_*)
├── output/datasets/<dataset_id>/   ← cumulative dashboard 산출물
│   ├── input/<file>.csv
│   ├── charts/<sid>.json           ← Plotly payload (sendfile 로 응답)
│   ├── thumbs/<sid>.svg            ← 사전 생성 SVG (priming 용)
│   ├── tables/{meta.json, yield_comments.json, ...}
│   ├── cumulative.html             ← 2000셀 그리드 페이지
│   └── build_version.txt
└── docs/                           ← 본 문서
```

S3 사용 시 위 산출물 중 일부(plotly JSON 등)는 S3 에도 사본이 저장되지만, **cumulative dashboard 빌드 결과는 로컬 디스크 전용**이다 (`/pe/report` 모듈과 별개).

---

## 6. 환경 / 의존성

[requirements.txt](../requirements.txt):
- Flask 3.x, dash 2.17+, plotly 5.22+, pandas 2.2+, numpy 1.26+
- openpyxl (xlsx export), pillow (PNG 후처리), kaleido (PNG 생성)
- boto3 (S3 클라이언트)
- 선택: cairosvg (`pip install cairosvg`) — xlsx 의 SVG→PNG 변환 가속

플랫폼: Windows 11, Python 3.13.

---

## 7. 4GB RAM / 10명 동시 사용자 환경 가정

`아키텍처_허니.txt:55-56`, `flowchart.txt` 참고. 설계 원칙:

- **서버는 한 번만 무겁게** (빌드 시 30~40초) — 이후 sendfile 로 정적 응답
- **무거운 시각화는 클라이언트에 위임** — 2000셀 priming 후 IndexedDB 캐시, GPU 렌더
- **DB write 최소화** — `executemany`, `INSERT OR IGNORE`, WAL 모드
- **analysis_key 캐시** — 동일 입력은 한 번만 계산
- **임시 파일 즉시 삭제** — `uploads/report/<sid>/` 는 analyze 종료 시 `shutil.rmtree`

---

## 8. 부팅 후 확인 URL

| URL | 용도 |
|-----|------|
| `http://127.0.0.1:8000/pe/report/` | report 분석 인덱스 페이지 |
| `http://127.0.0.1:8000/pe/report/view/<session_id>` | 세션 결과 화면 |
| `http://127.0.0.1:8000/view/current` | cumulative dashboard (build.py 빌드 후) |
| `http://127.0.0.1:8000/dash/<dataset_id>` | Dash 탭 페이지 |

---

## 9. 운영 시 흔한 함정

- **DB 파일 경로 변경**: `REPORT_DB_PATH` 만 수정하면 안 됨 — 기존 SQLite 의 lock 파일(`-wal`, `-shm`)도 함께 옮겨야 WAL 모드 그대로 유지.
- **S3 env 미설정**: analyze 는 작동, plot 은 503. 미리 `REPORT_S3_BUCKET` 만이라도 설정.
- **Dash import 실패**: `app.config["DASH_REGISTER_ERROR"]` 에서 원인 확인.
- **빌드 진행 중 서버 재시작**: `_build_status` 가 메모리뿐이라 진행 상태 손실. dataset 디렉토리만 보고 완료 여부 추정 (`cumulative.html` 존재).
- **port 8000 충돌**: terminate.bat 가 자동 정리.

# Honey 클라이언트

PyQt5 데스크톱 앱. CSV 데이터를 분석해 xlsx 리포트를 생성하고 Flask 서버에 업로드한다.

---

## 요구사항

- Python 3.10+
- Windows (Excel COM 의존)

```powershell
cd F:\COINAPI\report_server\client
pip install -r requirements.txt
```

| 패키지 | 용도 |
|--------|------|
| `PyQt5>=5.15` | GUI 프레임워크 |
| `requests>=2.28` | HTTP 업로드 |
| `pywin32>=306` | Excel COM — 차트 PNG 렌더 (Windows 전용) |
| `PyMuPDF>=1.23` | Distribution 시트 PDF→PNG 변환 (Windows 전용) |
| `Pillow>=9.0` | 다중 페이지 PDF→PNG 수직 합성 |
| `pandas>=1.5` | 로컬 분석 엔진 (report_generator) |
| `numpy>=1.23` | CPK / yield / 분포 계산 |
| `xlwings>=0.30` | xlsx 리포트 생성 — Excel COM (Windows 전용) |

---

## 실행

```powershell
python honey_main.py
```

---

## 설정 (환경변수)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `HONEY_SERVER_URL` | `http://127.0.0.1:8000` | Flask 서버 주소 |
| `HONEY_CONFIG_DIR` | `%APPDATA%\Honey` | 차트 색 팔레트 등 사용자 설정 저장 폴더 |
| `HONEY_STDINFO_DB` | exe 폴더 또는 `../DB/INFORMATION/` 탐색 | stdinfo SQLite DB 경로 |

---

## exe 빌드

```powershell
cd F:\COINAPI\report_server\client
pyinstaller --clean --noconfirm build_honey.spec
# 출력: dist/Honey.exe
```

빌드 후 서버에 배포하려면 [docs/04_honey_update.md](../docs/04_honey_update.md) 참조.

---

## 모듈 구조

```
client/
├── honey_main.py          QMainWindow 진입점 — 업로드 버튼 + 버전 체크 트리거
├── config.py              로컬/UI 설정 (CONFIG_DIR, STDINFO_DB_PATH)
├── app_settings.py        앱 설정 영속화
├── chart_colors.py        차트 색 팔레트 유틸리티
├── honey_ui/              PyQt5 다이얼로그·위젯 모음
│   ├── dialogs.py         ReportSettings, Upload, FileOrder, ColorEditor 등
│   └── ...
├── transport/             서버 통신
│   ├── config.py          SERVER_BASE_URL, CURRENT_VERSION (HONEY_SERVER_URL 읽음)
│   ├── uploader.py        multipart POST — xlsx + 차트 PNG 송신
│   ├── version_check.py   /honey/version 폴링
│   └── updater.py         exe 자동 교체 로직
├── report_generator/      로컬 분석 엔진 (CSV→DataFrame→xlsx)
│   ├── analyzer.py        분석 진입점
│   ├── csv_loader.py      CSV 읽기·정규화
│   ├── df_honey.py        df_honey 포맷 정의
│   ├── _builders.py       CPK / yield / 분포 지표 계산
│   ├── xlsx_writer.py     xlwings 로 xlsx 작성
│   └── models.py          AnalysisResult 데이터 클래스
├── report_flow/           업로드 전처리 (xlsx 준비, 차트 PNG 렌더)
│   └── prepare_upload.py
└── d1/                    D1 스토리지 프로바이더 (CSV/xlsx 소스 추상화)
    ├── __init__.py        get_provider(), list_files()
    └── README.md
```

---

## 워크플로 (7단계)

1. `d1/` 프로바이더에서 CSV 또는 xlsx 파일 선택
2. 파일명에서 product / lot_id 자동 추출 (제안)
3. "분석 시작" → `report_generator.analyzer.analyze()` — DataFrame → xlsx 생성 → 자동 저장
4. "서버 업로드" 버튼 → 메타 입력 팝업 (product_type / product / lot_id / PIN 4자리)
5. `report_flow.prepare_upload.prepare_upload_xlsx()` — distribution 시트 제거
6. `transport.chart_export` — Excel COM 으로 차트 시트 PNG 렌더
7. `transport.uploader.post_xlsx()` — multipart POST `/pe/report/upload_xlsx`

---

## 참조 문서

| 내용 | 문서 |
|------|------|
| UI 워크플로 상세 | [docs/05_client_ui.md](../docs/05_client_ui.md) |
| 분석 엔진 (CPK/yield/분포) | [docs/06_analysis_engine.md](../docs/06_analysis_engine.md) |
| 업로드 전송 + 차트 PNG | [docs/07_client_upload_chart.md](../docs/07_client_upload_chart.md) |
| Honey ZIP 배포 절차 | [docs/04_honey_update.md](../docs/04_honey_update.md) |

# Honey 클라이언트

PyQt6 데스크톱 앱. CSV 데이터를 분석해 xlsx 리포트를 생성하고 Flask 서버에 업로드한다.

> **client/ 수정은 🟡 사전 승인** ([../docs/15_ownership.md](../docs/15_ownership.md)).
> `report_generator/`·`honey_parse/` 는 🔒 **구서버 — 동결(무수정 원칙)**.

---

## 요구사항 / 실행

- Python 3.10+, Windows (Excel COM 의존). 의존성은 [requirements.txt](requirements.txt) 참조.

```powershell
cd F:\COINAPI\report_server\client
pip install -r requirements.txt
python honey_main.py
```

분석/생성엔 pandas·numpy·xlwings + Excel 이 필요하다. 미설치 시 분석 버튼만 비활성되고
로컬 xlsx 직접 업로드는 유지된다.

---

## 설정 (환경변수)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `HONEY_SERVER_URL` | `http://127.0.0.1:8000` | Flask 서버 주소 |
| `HONEY_CONFIG_DIR` | `%APPDATA%\Honey` | 차트 색 팔레트 등 사용자 설정 폴더 |
| `HONEY_D1_STORAGE` | `<repo>/d1_storage` | D1 로컬 검증 스토리지 폴더 (외부 provider) |

> Product 검색용 기준정보(part_id)는 클라가 로컬 DB 를 열지 않고 서버
> `GET /pe/report/api/part_ids` 로 HTTP 조회한다.

---

## exe 빌드

```powershell
pyinstaller --clean --noconfirm build_honey.spec   # 출력: dist/Honey/
```
배포 절차는 [docs/04_honey_update.md](../docs/04_honey_update.md).

---

## 모듈 구조

```
client/
├── honey_main.py          진입점 — 메인 윈도우 + 워크플로 + 버전 체크 트리거
├── config.py / app_settings.py / chart_colors.py   로컬·UI 설정
├── client_identity.py     PC 계정/호스트 신고값 (web_report 업로더 신원)
├── embedded_browser.py    세션 열람 내장 브라우저 (HoneyUser/<계정> UA 삽입)
├── honey_ui/              PyQt6 다이얼로그·위젯 (Upload/ReportSettings/FileOrder 등)
├── transport/             서버 통신
│   ├── config.py          SERVER_BASE_URL, CURRENT_VERSION
│   ├── uploader.py        multipart POST — post_grids(grid+PNG) / web_report parquet
│   ├── version_check.py / update_policy.py / updater.py   자동 업데이트
│   └── retry.py
├── report_flow/           업로드 전처리
│   └── upload_prepare.py  Excel COM 으로 시트 grid + issue image 추출
├── report_generator/      (구서버·동결) 로컬 분석 엔진 (CSV→df_honey→xlsx) — README 별도
├── honey_parse/           (구서버·동결) file_to_df 파서 (현재 더미 폴백)
├── map_report/            (사전 승인·신규) 웨이퍼 bin map 렌더 + xlsx 부착 → docs/14
├── excel_download/ · excel_edit/   Excel COM 헬퍼
└── (구서버·동결) ../d1/   D1 입력 provider — client 의 sibling(루트), 검증용
```

---

## 워크플로

1. `d1` provider(외부·검증용)에서 CSV/xlsx 선택 (기본: 로컬 `d1_storage/` 검색).
2. 파일명에서 product / lot_id 자동 추출(제안).
3. "분석 시작" → `report_generator` 분석 → xlsx 생성 → 자동 저장.
4. "서버 업로드" → 메타 입력 팝업(product_type / product / lot_id, password 선택).
5. `report_flow.upload_prepare.prepare_upload_xlsx()` — Excel COM 으로 grid + issue PNG 추출.
6. `transport.uploader.post_grids()` → `POST /pe/report/upload_xlsx` (원본 xlsx 미전송).
   web_report honeyform parquet 는 `POST /pe/report/upload_webreport` 병행 경로.

---

## 참조 문서

| 내용 | 문서 |
|------|------|
| UI 워크플로 상세 | [docs/05_client_ui.md](../docs/05_client_ui.md) |
| 분석 엔진 (구서버·동결) | [docs/06_analysis_engine.md](../docs/06_analysis_engine.md) · [report_generator/README.md](report_generator/README.md) |
| 업로드 전송 | [docs/07_client_upload_chart.md](../docs/07_client_upload_chart.md) |
| Honey ZIP 배포 절차 | [docs/04_honey_update.md](../docs/04_honey_update.md) |

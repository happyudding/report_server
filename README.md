# report_server

외부 report generator 가 만든 `.xlsx` 산출물을 Honey 클라이언트가 업로드하고,
Flask 서버가 SQLite + S3 에 저장한 뒤 검색결과 페이지에서 조회한다.

## 구성

| 디렉토리 | 역할 | 상세 |
|----------|------|------|
| **[server/](server/)** | Flask 서버 (포트 8000) — `/pe/report/`, `/honey/` | [server/README.md](server/README.md) |
| **[client/](client/)** | Honey PyQt5 클라이언트 — CSV 분석 + 업로드 | [client/README.md](client/README.md) |
| **tests/sample_xlsx.py** | 테스트용 더미 xlsx 생성기 | — |
| **_reference/** | 기존 plotly 분석/시각화 코드 (비활성) | — |

## 빠른 시작

### 1. 서버

→ 상세 설정(환경변수·API 목록)은 **[server/README.md](server/README.md)** 참조.

```powershell
cd F:\COINAPI\report_server\server
pip install -r requirements.txt
# (선택) S3 환경변수 설정
$env:REPORT_S3_BUCKET = "your-bucket"
$env:REPORT_S3_ACCESS_KEY = "..."
$env:REPORT_S3_SECRET_KEY = "..."
.\start.bat
```

`http://127.0.0.1:8000/pe/report/` 에서 검색결과 페이지 확인.

### 2. 클라이언트

→ 상세 설정(환경변수·빌드)은 **[client/README.md](client/README.md)** 참조.

```powershell
cd F:\COINAPI\report_server\client
pip install -r requirements.txt
python honey_main.py
```

### 3. exe 빌드 (선택)

```powershell
cd F:\COINAPI\report_server\client
pyinstaller --clean --noconfirm build_honey.spec
# 출력: client/dist/Honey.exe
# 배포 절차: docs/04_honey_update.md 참조
```

## 검증 절차 (E2E)

1. **DB 초기화 확인**
   서버 시작 시 자동. `DB/pe/report/report.db` 생성 확인.

2. **더미 xlsx 생성**
   ```powershell
   cd F:\COINAPI\report_server
   python tests\sample_xlsx.py
   ```
   `tests/sample.xlsx` 가 8개 시트로 생성됨.

3. **검색결과 페이지 접속**
   `http://127.0.0.1:8000/pe/report/` — 빈 결과 표시 확인.

4. **버전 체크 응답**
   ```powershell
   curl http://127.0.0.1:8000/honey/version
   ```
   `version.json` 내용 그대로 반환.

5. **xlsx 업로드 (curl)**
   ```powershell
   curl -F "xlsx=@tests/sample.xlsx" -F "product_type=MD" -F "product=A1" -F "lot_id=L001" `
        http://127.0.0.1:8000/pe/report/upload_xlsx
   ```
   200 OK + `session_id` 반환.

6. **세션 확인**
   - 검색결과 페이지 새로고침 → 새 row 표시
   - 클릭 → `/pe/report/view/<sid>` 에서 summary/yield/issue_table 텍스트 확인

7. **클라이언트 흐름**
   Honey 앱 → product_type/product/lot_id 입력 → "xlsx 선택 후 업로드" 클릭 →
   `tests/sample.xlsx` 선택 → 업로드 완료 메시지박스 → 검색결과 페이지 확인.

## 알려진 제약

- xlsx 시트명/헤더가 변경되면 `xlsx_parser.py` 가 깨질 수 있음. 외부 report
  generator 가 진화하면 parser 의 anchor 텍스트와 헤더명을 같이 갱신해야 한다.
- 자동 exe 교체(self-update)는 현재 스켈레톤에 없음 — 사용자가 수동 교체.
  추후 batch 스크립트 + 재실행 방식으로 추가 예정.
- S3 미설정 시 서버는 계속 동작하지만 업로드된 xlsx 본문 / 추출 텍스트 JSON 은
  보관되지 않는다 (`s3_uploaded=false` 응답). yield rows DB 저장은 정상.
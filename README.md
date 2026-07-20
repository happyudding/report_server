# report_server

Honey 클라이언트가 추출한 산출물을 업로드하면 Flask 서버가 SQLite + S3(또는 로컬 폴백)에
세션 단위로 저장하고, 검색결과·세션 상세 페이지로 조회한다. 두 업로드 흐름이 병행한다:
**xlsx 추출 grid** 와 **web_report honeyform parquet**(신규 개발 주 대상).

## 구성

| 디렉토리 | 역할 | 소유권 | 상세 |
|----------|------|--------|------|
| **[server/](server/)** | Flask 서버 (포트 8080) — `/pe/report/`, `/honey/`, `/pe/admin-pte/` | 🟢 자유 (단 storage_gateway 는 🔒외부 담당자) | [server/README.md](server/README.md) |
| **[web_report/](web_report/)** | web_report honeyform 처리·탭 계산·캐시 | 🟢 자유 | [docs/10](docs/10_web_report_pipeline.md)·[11](docs/11_web_report_tabs.md)·[12](docs/12_web_report_cache.md) |
| **[client/](client/)** | Honey PyQt6 클라이언트 — CSV 분석 + 업로드 | 🟢 자유(honey_ui·honey_main·transport·excel_*) / 🟡 사전 승인(나머지) / 🔒외부 담당자(report_generator·honey_parse) | [client/README.md](client/README.md) |
| **[d1/](d1/)** · **client/report_generator/** · **client/honey_parse/** · **server/storage_gateway/** | 병합된 외부 담당자 코드 (D1·리포트생성·파서·저장소) | 🔒 외부 담당자 영역 동결 | [docs/15](docs/15_ownership.md) · 진입점 [INDEX §3.1](docs/INDEX.md) |
| **tests/sample_xlsx.py** | 더미 sheet_grids 픽스처 생성기 | — | — |

전체 코드 흐름 지도는 [docs/INDEX.md](docs/INDEX.md), 규칙·경계는 [CLAUDE.md](CLAUDE.md),
소유권/수정 권한 정본은 [docs/15_ownership.md](docs/15_ownership.md).

## 빠른 시작

### 서버
```powershell
cd F:\COINAPI\report_server\server
pip install -r requirements.txt
# (선택) S3 — 미설정 시 로컬 폴백으로 동작
$env:REPORT_S3_BUCKET = "your-bucket"
.\start.bat
```
`http://127.0.0.1:8080/pe/report/` 에서 검색결과 페이지 확인. 환경변수·API 상세는
[server/README.md](server/README.md).

### 클라이언트
```powershell
cd F:\COINAPI\report_server\client
pip install -r requirements.txt
python honey_main.py
```
exe 빌드·배포는 [docs/04_honey_update.md](docs/04_honey_update.md).

## 검증 절차 (E2E)

1. **서버 기동** — `DB/pe/report/report.db` 자동 생성 확인.
2. **검색결과 페이지** — `http://127.0.0.1:8080/pe/report/` 빈 결과 표시.
3. **버전 응답** — `curl http://127.0.0.1:8080/honey/version` → `version.json` 반환.
4. **grid 파서 검증** — `python tests/sample_xlsx.py` (더미 sheet_grids 로
   `xlsx_parser.parse_report_xlsx` 동작 확인).
5. **업로드** — Honey 앱에서 product_type/product/lot_id 입력 + xlsx 선택 → 업로드
   (`POST /pe/report/upload_xlsx`, grid+PNG 전송; 원본 xlsx 는 보내지 않음). web_report 는
   honeyform parquet 를 `POST /pe/report/upload_webreport` 로 전송.
6. **세션 확인** — 검색결과 새로고침 → 새 row → `/pe/report/view/<sid>` 상세.

> 업로드는 Honey 클라이언트가 grid/parquet 를 조립해 보내는 계약이라 단순 `curl -F xlsx=@…`
> 로는 재현되지 않는다. 파서만 확인하려면 4번의 tests 픽스처를 쓴다.

## 알려진 제약

- xlsx 시트명/헤더가 변경되면 `xlsx_parser.py` anchor 가 깨질 수 있음 — 외부 report
  generator 진화 시 parser 의 anchor 텍스트·헤더명을 같이 갱신.
- S3 미설정 시 산출물은 `REPORT_UPLOAD_DIR` 로컬에 저장되고 조회도 로컬을 따른다. yield
  rows 등 DB 저장은 S3 와 무관하게 정상.
- 클라 자동 업데이트는 3버튼 다이얼로그(자동 설치/ZIP 다운로드/나중에) — 실행 중 exe 직접
  덮어쓰기 없이 외부 배치로 교체 ([docs/04](docs/04_honey_update.md)).

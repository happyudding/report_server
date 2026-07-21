# product_info_import — 기준정보 CSV(DRM) → product_info.db

기준정보 CSV 가 **NASCA DRM 으로 암호화**돼 서버가 평문으로 읽을 수 없다. 서버는 Excel 을
쓰지 않으므로([CLAUDE.md](../../CLAUDE.md) 불변 규칙 #1) 서버에서 직접 열 수 없다.

그래서 **Excel 이 설치된 별도 PC** 에서 이 도구를 돌려 SQLite(`product_info.db`)로 변환하고,
그 파일 하나를 서버로 복사한다. 서버는 그 DB 를 **읽기 전용**으로만 연다
([server/product_info.py](../../server/product_info.py)).

```
DRM CSV ──(Excel COM, 이 도구)──▶ product_info.db ──(수동 복사)──▶ 서버 DB/pe/report/
```

이 폴더는 **standalone** 이다 — report_server 의 `config`/`database` 를 import 하지 않으므로
**폴더만 통째로 복사**해 가면 어디서든 돈다(약 20KB).

---

## 1. 준비 (Excel PC, 최초 1회)

- Windows + **Excel 설치** + **NASCA DRM 클라이언트에 로그인된 상태**
- Python **3.9 이상**
- `pip install -r requirements.txt` (= pywin32)

> Honey 를 소스로 돌리는 PC 라면 `client/.venv` 에 pywin32 가 이미 있다.
> `set PYTHON=...\client\.venv\Scripts\python.exe` 를 먼저 잡고 bat 을 실행하면 된다.

## 2. 변환

`run_import.bat` 더블클릭 → 파일 선택 창에서 DRM CSV 선택 → (DRM 프롬프트가 뜨면 승인)
→ `output\product_info.db` 생성.

CSV 경로를 알고 있으면 인자로 줘도 된다(다이얼로그 생략):

```bat
run_import.bat "D:\기준정보\product_info.csv"
```

**출력 마지막 줄을 반드시 확인할 것**:

```
[import] 완료 — rows=1842 skipped_empty=3 no_key=0 dup_id=0 elapsed=6.4s
```

- `rows=` 가 데이터 담당자가 아는 행수와 맞는지
- `[warn]` 이 있으면 내용 확인 (`no_key` = part_id/sub_part_id 둘 다 빈 행 → 검색 후보로 안 잡힘)

### 최초 실행 시 값 확인 (중요)

Excel 은 CSV 를 열 때 타입을 추론하므로 표기가 바뀔 수 있다. **첫 변환 때 한 번은**
아래 항목을 원본 CSV 와 대조할 것:

| 필드 | 원본 예 | 확인 포인트 |
|---|---|---|
| `chip_size_x` / `chip_size_y` | `5.20` | 후행 0 이 살아있는가 (`5.2` 로 줄지 않았는가) |
| `gross_die` / `wf_size` / `flat_zone` | `1520` / `12` / `270` | `1520.0` 같은 소수점이 붙지 않았는가 |
| `e2f_bin_list` | `1;2;3` | 그대로인가 |
| `sub_part_id` | `{A01-1, A01-2}` | 중괄호·쉼표가 그대로인가 |
| `create_date` | `2026-01-05` | 날짜 형식이 바뀌지 않았는가 |
| 선행 0 이 있는 코드 | `00123` | `123` 으로 줄지 않았는가 |

확인 방법 — 아무 PC 에서:

```bat
python -c "import sqlite3;c=sqlite3.connect('output/product_info.db');c.row_factory=sqlite3.Row;r=c.execute('SELECT * FROM report_product_info ORDER BY row_no LIMIT 3').fetchall();[print(dict(x)) for x in r]"
```

**어긋난 값이 있으면 그대로 쓰지 말고 알릴 것.** `import_product_info.py` 의
`_read_rows_via_com()` 을 `Workbooks.OpenText(FieldInfo=전 컬럼 xlTextFormat)` 로 바꾸면
Excel 의 타입 추론 자체를 끌 수 있다(그 함수 하나만 교체하면 된다).

## 3. 서버로 복사

로드 중인 파일에 직접 덮어쓰면 공유 위반이 나거나 반쯤 쓰인 파일을 서버가 읽을 수 있다.
**`.new` 로 복사한 뒤 rename** 한다 (같은 볼륨 rename 은 원자적):

```bat
rem 기존 파일 백업 (선택)
move /y "F:\COINAPI\report_server\DB\pe\report\product_info.db" ^
        "F:\COINAPI\report_server\DB\pe\report\product_info.db.bak"

copy /y "output\product_info.db" "F:\COINAPI\report_server\DB\pe\report\product_info.db.new"
move /y "F:\COINAPI\report_server\DB\pe\report\product_info.db.new" ^
        "F:\COINAPI\report_server\DB\pe\report\product_info.db"
```

경로를 바꾸려면 서버 쪽 env `PRODUCT_INFO_DB_PATH` 를 쓴다([server/README.md](../../server/README.md)).

## 4. 반영 확인 — **서버 재기동 불필요**

서버는 호출마다 DB 파일의 `(mtime, size)` 를 보고 바뀌었으면 자동으로 다시 읽는다.

```
curl http://12.81.220.117:8080/pe/report/api/part_ids
```

서버 로그에 아래 한 줄이 찍히면 성공이다:

```
product_info.db 로드: 후보 1842건 rows=1842 imported_at=2026-07-21T14:03:11 (...)
```

`imported_at` 으로 **지금 서버가 어느 시점 DB 를 쓰고 있는지** 항상 판별할 수 있다.

---

## 파일 지도

| 파일 | 역할 |
|---|---|
| `import_product_info.py` | 변환 본체. stdlib + pywin32 만 사용 |
| `run_import.bat` | 더블클릭용 래퍼 (UTF-8 **BOM 없음** + CRLF) |
| `select_csv.ps1` | 파일 선택 다이얼로그 (UTF-8 **BOM 필수** + CRLF) |
| `requirements.txt` | pywin32 (UTF-8 BOM — 한글 주석이 cp949 로 깨지지 않게) |
| `sample_product_info.csv` | 더미 5행 픽스처. 개발 PC 검증·DB 생성용 |
| `output/` | 산출물 (git 미추적) |

> 인코딩 규칙은 [.gitattributes](../../.gitattributes) 참조. `.bat` 에 BOM 을 붙이면 cmd 가
> 첫 줄을 못 읽고, `.ps1` 에 BOM 이 없으면 PowerShell 5.1 이 cp949 로 읽어 한글이 깨진다 —
> **정반대라 헷갈리기 쉽다.**

## 개발 PC (Excel/DRM 없이)

`--plain` 으로 평문 CSV 를 읽어 같은 DB 를 만든다. 입력 리더만 바뀌고 기록 경로는 동일하다.

```bat
python import_product_info.py sample_product_info.csv --plain ^
       --out ..\..\DB\pe\report\product_info.db
```

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공 |
| 1 | 일반 실패 (CSV 없음 / COM 실패 / 쓰기 실패) |
| 2 | 헤더 불일치 — **.db 를 만들지 않음** |
| 3 | 데이터 0행 — **.db 를 만들지 않음** (빈 DB 를 서버로 옮기는 사고 방지) |

## CSV 스키마가 바뀌었다면

`import_product_info.py` 의 `COLUMNS`(41개 고정)와 실제 헤더가 **순서까지** 일치해야 한다.
불일치하면 exit 2 로 멈추고 누락/추가/위치를 찍는다. 이때 함께 봐야 할 곳:

- `import_product_info.py` `COLUMNS`
- [server/product_info.py](../../server/product_info.py) `INFO_COLUMNS` (세션에 저장하는 14개)
- [server/database/sessions.py](../../server/database/sessions.py) `_PRODUCT_INFO_COLUMNS` (위와 동일 집합이어야 함)
- [docs/09_db_inventory.md](../../docs/09_db_inventory.md)

## 설계 메모

- **테이블명은 `report_` prefix** — 별도 .db 파일이어도 유지한다(불변 규칙 #2, `voc_db.py` 선례).
- **전 컬럼 TEXT** — 서버는 이 값들로 산술을 하지 않고 문자열로만 다룬다. REAL 이면
  `chip_size_x "5.20"` 이 `5.2` 가 되어 세션에 저장되는 표시값이 바뀐다.
- **WAL 을 쓰지 않는다** — WAL 은 `-wal`/`-shm` 사이드카를 만들고, 그 상태의 .db 를 사이드카
  없이 손으로 복사하면 마지막 커밋이 유실될 수 있다. 이 산출물은 **단일 자족 파일**이어야 한다.
- **인덱스가 없다** — 서버는 로드당 전체 스캔 1회뿐이라 인덱스가 쓰이지 않는다. 나중에
  검색을 요청마다 SQL 로 질의하게 바꾸면 그때 키 테이블 + 인덱스를 추가한다.
- **brace 펼침(`{A01-1, A01-2}`)은 서버가 한다** — 임포터로 옮기면 파싱 규칙을 고칠 때마다
  Excel PC 재실행 + .db 재복사가 필요해진다(서버 코드 한 줄 수정이 물리적 배포 이벤트가 됨).
- **백업 대상이 아니다** — 마스터 CSV 에서 재실행으로 100% 재생성되는 파생물이라 사용자
  생성 데이터가 없다. [db_backup.py](../../server/db_backup.py) 는 report.db 만 백업한다.

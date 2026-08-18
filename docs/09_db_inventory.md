# 09 · DB 파일 인벤토리 (전수조사 결과, 2026-07-10)

> **핵심 결론: 세션 관리 DB 는 이미 `report.db` 하나다.** 날짜가 붙은 .db 파일들은
> 세션 데이터가 쪼개진 것이 아니라 (1) 자동 백업 rotation 산출물, (2) 외부에서 만들어
> 주는 제품 카탈로그 파일이다. 새 DB 파일을 만드는 코드를 추가하기 전에 이 문서로
> 기존 분류를 먼저 확인할 것.
> 스키마 상세는 [03 저장소](03_storage.md) 참조.

## 전체 인벤토리 (한눈에)

| 파일 (패턴) | 정체 | 생성 주체 | 날짜 파일명? |
|-------------|------|-----------|--------------|
| `DB/pe/report/report.db` | **메인 세션 DB** — 테이블 25개 전부 여기 | `report_db.init_report_db` | 아니오 (고정명) |
| `DB/pe/report/backup/report_YYYYMMDD_HHMMSS.db` | report.db **자동 백업** (최대 7개 rotation) | `db_backup.run_backup` | **예 — 의도된 백업** |
| `DB/pe/report/product_info.db` | **기준정보 카탈로그** — part_ids 검색 후보 + 세션 기준정보 lookup (읽기전용) | [tools/product_info_import](../tools/product_info_import/README.md) (Excel PC) → 수동 복사 | 아니오 (고정명) |
| `DB/pe/report/voc/voc.db` · `eval/eval.db` | VOC 게시판 / 코멘트 export — report_server 소유 별도 파일 | `voc_db.py` / `eval_export.py` | 아니오 (고정명) |
| `DB/INFORMATION/stdinfo_YYYYMMDD.db` | 제품 part_id 카탈로그 (**외부 생성**) — **현재 읽는 코드 없음**, §3 참조 | 이 repo 에 생성 코드 없음 — 수동 배치 | **예 — 수동 교체 방식** |
| `f:\COINAPI\plotly_sqlite\storage\app.db` | **별개 프로젝트**(Dash 데모) 자체 DB | plotly_sqlite/db.py | 아니오. report_server 와 무관 |

- `DB/` 트리 전체는 .gitignore 대상 (런타임 산출물).
- 날짜 DB 파일명을 만드는 코드 경로는 repo 전체에서 **db_backup.py 하나뿐**
  (그 외 strftime 사용처는 서버/클라 로그·xlsx·PNG 파일명 — DB 아님).
- `web_report/` 의 캐시(cache.py/response_cache.py = RAM, disk_cache.py = 재계산 가능한 파일)는
  **DB 파일을 만들지 않는다** (→ [12 캐시](12_web_report_cache.md)).

## 1. report.db — 세션 관리 단일 DB

- 경로: `config.REPORT_DB_PATH` (env `REPORT_DB_PATH` 로 변경 가능).
- 테이블 25개, 전부 `report_` prefix. 정본 SCHEMA 는
  [core.py](../server/database/core.py), 목록·컬럼은 [03 저장소](03_storage.md) 참조.
- DB 는 **메타데이터 저장소**다 — 큰 본문(parquet·manifest·계산 캐시·이미지, 그리고
  2026-08-14 부터 Note 시트 JSON)은 전부 `uploads/report/` 또는 S3 에 있고 DB 에는 포인터만
  둔다. 단 `report_sheet_data`(legacy xlsx 추출 텍스트, 세션당 수 KiB)는 조회 정본이라
  크기와 무관하게 DB 유지.
- 테이블 간 연결 키는 **`session_id`**(업로드 1건) 와 **`analysis_key`**(grid+meta 해시) —
  별도 DB 파일로 나누지 않고 이 두 키로 조인한다.
- 사이드카 문서 파일 `DB/pe/report/report_schema.sql`, `report_README.md` 는 코드가 읽지 않는
  **스냅샷** — read-only 스크립트로 재생성하며 값(샘플 row)은 담지 않는다
  ([report_README.md](../DB/pe/report/report_README.md) 말미의 재생성 방법 참조).

## 2. backup/ — 날짜 파일이 생기는 유일한 코드 경로

`backup/report_YYYYMMDD_HHMMSS.db` 가 "쌓이는 것처럼" 보이지만 **상한 7개 rotation** 이라
무한히 늘지 않는다.

- 백업 방식: WAL 모드라 파일 복사가 아닌 sqlite3 **backup API** 온라인 백업
  ([db_backup.py:39](../server/db_backup.py#L39)) + 백업본 `integrity_check` +
  원본 `wal_checkpoint(TRUNCATE)`/`optimize`.
- 트리거 2개:
  - 주기: [ops.py:45](../server/ops.py#L45) 가 서버 기동 시
    [`start_backup_scheduler`](../server/db_backup.py#L76) 실행 → 기본 24시간마다.
  - 수동: admin 패널 백업 버튼 → [maintenance.py `backup_now`](../server/admin_panel/maintenance.py#L28).
- 보존: `report_*.db` 글롭 중 오래된 순으로 삭제, 최신 `REPORT_DB_BACKUP_KEEP`(기본 **7**)개만
  유지 ([db_backup.py:64](../server/db_backup.py#L64)). admin 패널 목록 조회는
  [`list_backups`](../server/admin_panel/maintenance.py#L38).
- 관련 env ([config.py:63](../server/config.py#L63)):

| env | 기본값 | 의미 |
|-----|--------|------|
| `REPORT_DB_BACKUP_ENABLED` | 1 | 주기 백업 on/off |
| `REPORT_DB_BACKUP_INTERVAL_HOURS` | 24 | 백업 주기(시간) |
| `REPORT_DB_BACKUP_KEEP` | 7 | 보존 개수 (초과분 자동 삭제) |
| `REPORT_DB_BACKUP_DIR` | `<REPORT_DB_PATH 폴더>/backup` | 백업 저장 위치 |

## 3. product_info.db — 기준정보 카탈로그 (DRM 때문에 오프라인 생성)

`GET /pe/report/api/part_ids`(업로드 다이얼로그 Product 검색)와 업로드 시 세션 기준정보
14컬럼 저장이 이 파일을 쓴다. 읽는 곳은 [server/product_info.py](../server/product_info.py)
**한 모듈뿐**이고, 공개 API 는 `list_search_candidates()` / `lookup()` 2개다
(소비처: [routes_misc.py](../server/report/routes_misc.py) `/api/part_ids`,
[web_report/ingest.py](../web_report/ingest.py) `create_session`).

- 경로: [config.py `PRODUCT_INFO_DB_PATH`](../server/config.py) (env 로 변경 가능).
- **왜 오프라인 생성인가**: 원본 기준정보 CSV 가 **NASCA DRM 으로 암호화**돼 서버가 평문으로
  읽을 수 없다. 서버는 Excel 을 쓰지 않으므로([CLAUDE.md §5 규칙 1](../CLAUDE.md)) 직접 열 수
  없어, **Excel 이 설치된 별도 PC** 에서 win32com 으로 변환한다.
- 생성: [tools/product_info_import](../tools/product_info_import/README.md) — `run_import.bat`
  → `output/product_info.db` → 서버 `DB/pe/report/` 로 **수동 복사**. 절차 정본은 그 README.
- 테이블: `report_product_info`(CSV 41컬럼 전부 TEXT + `row_no`) + `report_product_info_meta`
  (`imported_at`/`row_count`/`source_csv` 등 — **지금 서버가 어느 시점 DB 를 쓰는지** 판별용).
  별도 .db 파일이어도 `report_` prefix 를 유지한다(§4).
- **서버 재기동 불필요**: `(mtime, size)` 가 바뀌면 다음 호출에서 자동 재로딩된다. 성공 시
  `product_info.db 로드: 후보 N건 rows=... imported_at=...` 로그 1줄이 찍힌다.
- **WAL 아님** — 손으로 복사하는 단일 자족 파일이어야 해서 임포터가 의도적으로 WAL 을 끈다
  (`-wal` 사이드카를 빠뜨리고 복사하면 마지막 커밋이 유실된다).
- **백업 대상 아님** — 마스터 CSV 에서 재실행으로 100% 재생성되는 파생물이다
  (`db_backup.py` 는 report.db 만 백업한다).
- ⚠️ 파일이 없거나 읽기 실패면 **500 없이 조용히 빈 목록**을 반환한다(예외 삼킴, 서버 로그
  경고 1회). 교체 후 Product 검색이 비어 보이면 이 지점부터 확인할 것.
- 참고: 클라이언트는 이 DB 를 직접 열지 않고 HTTP(`GET /pe/report/api/part_ids`)로 조회한다
  (`transport/uploader.py`). **서버 측** 파일이다.

### 3-1. stdinfo — 미사용 잔존 상수 (읽는 코드 없음)

`DB/INFORMATION/stdinfo_YYYYMMDD.db` 는 **더 이상 어디서도 읽히지 않는다.**
[config.py `STDINFO_DB_PATH`](../server/config.py) 정의만 남아 있고 참조하는 코드가 0곳이다
(전수 grep 확인, 2026-07-21). 과거 `/api/part_ids` 가 이 파일을 읽었으나 그 역할은
`product_info` 로 넘어갔다 — 이 문서의 옛 설명이 그 사실을 반영하지 못한 채 남아 있었다.
상수 자체의 제거는 별도 판단 사항으로 남긴다.

## 4. 무관 파일 분류 노트

- `f:\COINAPI\plotly_sqlite\storage\app.db` — 형제 폴더의 별개 Dash 데모 프로젝트 DB.
  두 프로젝트 사이에 import/경로 참조가 전혀 없음 (양방향 전수 검색으로 확인).
  report_server 정리 대상 아님.
- 새 디스크 DB 를 추가하고 싶다면: 원칙은 **report.db 안에 `report_` prefix 테이블 추가**
  ([CLAUDE.md §5 규칙 3](../CLAUDE.md)) — 새 .db 파일을 만들지 말 것.
- **예외로 인정된 별도 .db 3개**와 그 사유 (전부 테이블명 `report_` prefix 는 유지):
  - `voc.db` — 게시판. 세션 생명주기와 무관하고 세션 삭제에 딸려가면 안 됨.
  - `eval.db` — eval_analyzer 스키마의 코멘트 export. 외부 프로젝트 스키마라 섞을 수 없음.
  - `product_info.db` — **원본 CSV 가 DRM 이고 서버는 Excel 을 못 쓴다**. 서버 밖(Excel PC)에서
    만들어 통째로 복사하는 산출물이라 report.db 안에 넣을 방법이 없다(복사 단위가 파일).
  새 .db 를 추가하려면 "서버 프로세스가 스스로 못 만드는 파일인가?" 를 먼저 답할 것 —
  아니라면 report.db 테이블로 충분하다.

# 09 · DB 파일 인벤토리 (전수조사 결과, 2026-07-10)

> **핵심 결론: 세션 관리 DB 는 이미 `report.db` 하나다.** 날짜가 붙은 .db 파일들은
> 세션 데이터가 쪼개진 것이 아니라 (1) 자동 백업 rotation 산출물, (2) 외부에서 만들어
> 주는 제품 카탈로그 파일이다. 새 DB 파일을 만드는 코드를 추가하기 전에 이 문서로
> 기존 분류를 먼저 확인할 것.
> 스키마 상세는 [03 저장소](03_storage.md) 참조.

## 전체 인벤토리 (한눈에)

| 파일 (패턴) | 정체 | 생성 주체 | 날짜 파일명? |
|-------------|------|-----------|--------------|
| `DB/pe/report/report.db` | **메인 세션 DB** — 테이블 10개 전부 여기 | [report_db.py `init_report_db`](../server/database/report_db.py#L279) | 아니오 (고정명) |
| `DB/pe/report/backup/report_YYYYMMDD_HHMMSS.db` | report.db **자동 백업** (최대 7개 rotation) | [db_backup.py `run_backup`](../server/db_backup.py#L39) | **예 — 의도된 백업** |
| `DB/INFORMATION/stdinfo_YYYYMMDD.db` | 제품 part_id 카탈로그 (**외부 생성**, 읽기전용) | 이 repo 에 생성 코드 없음 — 수동 배치 | **예 — 수동 교체 방식** |
| `f:\COINAPI\plotly_sqlite\storage\app.db` | **별개 프로젝트**(Dash 데모) 자체 DB | plotly_sqlite/db.py | 아니오. report_server 와 무관 |

- `DB/` 트리 전체는 .gitignore 대상 (런타임 산출물).
- 날짜 DB 파일명을 만드는 코드 경로는 repo 전체에서 **db_backup.py 하나뿐**
  (그 외 strftime 사용처는 서버/클라 로그·xlsx·PNG 파일명 — DB 아님).
- `web_report/` 의 캐시(cache.py, response_cache.py 등)는 전부 **in-process RAM** —
  디스크 DB 파일을 만들지 않는다.

## 1. report.db — 세션 관리 단일 DB

- 경로: [config.py `REPORT_DB_PATH`](../server/config.py#L19) (env `REPORT_DB_PATH` 로 변경 가능).
- 테이블 10개, 전부 `report_` prefix ([SCHEMA](../server/database/report_db.py#L7)):
  `report_session`(중심) / `report_analysis_summary` / `report_object_info` /
  `report_analysis_lock` / `report_csv_files` / `report_annotation` /
  `report_dashboard_comment` / `report_sheet_data` / `report_audit_log` /
  `report_user_favorite`.
- 테이블 간 연결 키는 **`session_id`**(업로드 1건) 와 **`analysis_key`**(grid+meta 해시) —
  별도 DB 파일로 나누지 않고 이 두 키로 조인한다. 상세는 [03 저장소](03_storage.md).
- 사이드카 문서 파일 `DB/pe/report/report_schema.sql`, `report_README.md` 는 코드가
  읽지 않는 스냅샷(문서용)이다.

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

## 3. stdinfo — 외부 생성 제품 카탈로그 (읽기전용)

- 경로: [config.py `STDINFO_DB_PATH`](../server/config.py#L22) — 기본값에 날짜가 포함된
  파일명(`stdinfo_20260511.db`)이 **하드코딩**되어 있다.
- 읽는 곳은 한 군데: [`/pe/report/api/part_ids`](../server/report/report_routes.py#L794)
  (업로드 다이얼로그 Product 검색용, `mode=ro` 읽기전용 접속, `products.part_id` SELECT).
- **이 repo 는 이 파일을 만들지도 쓰지도 않는다** — 외부 프로세스가 생성한 파일을
  수동으로 갖다 놓는 방식. 스키마 스냅샷은 `DB/INFORMATION/stdinfo_20260511_schema.sql`.
- **새 버전으로 교체하는 절차**: 새 파일을 `DB/INFORMATION/` 에 배치한 뒤
  [config.py:22](../server/config.py#L22) 기본값을 새 파일명으로 수정하거나
  env `STDINFO_DB_PATH` 를 지정하고 서버 재시작.
- ⚠️ 경로가 틀리거나 파일이 없으면 **500 없이 조용히 빈 목록**을 반환한다
  ([report_routes.py:808](../server/report/report_routes.py#L808) 예외 삼킴, 서버 로그 경고만).
  교체 후 Product 검색이 비어 보이면 이 지점부터 확인할 것.
- 참고: [client/README.md](../client/README.md) 의 `HONEY_STDINFO_DB` 언급은 stale —
  클라이언트는 로컬 stdinfo DB 를 열지 않고 HTTP(`GET /pe/report/api/part_ids`)로 조회한다
  ([uploader.py `fetch_part_ids`](../client/transport/uploader.py)).

## 4. 무관 파일 분류 노트

- `f:\COINAPI\plotly_sqlite\storage\app.db` — 형제 폴더의 별개 Dash 데모 프로젝트 DB.
  두 프로젝트 사이에 import/경로 참조가 전혀 없음 (양방향 전수 검색으로 확인).
  report_server 정리 대상 아님.
- 새 디스크 DB 를 추가하고 싶다면: 원칙은 **report.db 안에 `report_` prefix 테이블 추가**
  ([CLAUDE.md §5 규칙 3](../CLAUDE.md)) — 새 .db 파일을 만들지 말 것.

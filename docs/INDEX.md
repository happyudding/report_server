# docs/ — 기능별 코드 흐름 지도

이 폴더는 COINAPI report_server 를 **큰 기능 7개**로 쪼개 코드 흐름을 정리한 곳이다.
프로젝트 최상위 [CLAUDE.md](../CLAUDE.md) 는 "규칙/디렉토리 인덱스" 요약본이고,
여기 docs/ 는 "각 기능이 실제로 어떻게 흐르는가" 를 추적한 작업용 메모다.

> 읽는 순서 팁: 무엇을 고치려는지 정해지면 아래 표에서 해당 기능 문서 1개만 열면 된다.
> 문서끼리는 `→` 로 연결돼 있으니 경계(서버↔클라이언트)에서만 옆 문서로 점프.

---

## 0. 한눈에 보는 전체 데이터 흐름

```
[Honey 클라이언트 (PyQt5, 사용자 PC + Excel)]
  client/d1/ provider 에서 CSV/xlsx 선택 (기본: d1_storage 로컬 폴더)
        │  (06 분석 엔진: csv → 분석 → xlsx 생성)
        ▼
  xlsx 자동 저장 ──(05 UI)──► "서버에 업로드" 클릭
        │  (07 업로드: 차트 PNG 렌더 + multipart POST)
        ▼
══════════ HTTP ══════════════════════════════════════════
        ▼
[Flask 서버 (헤드리스)]
  POST /pe/report/upload_xlsx ──(01 업로드 파이프라인)──►
        │  sha256(grids)→analysis_key, grid 파싱, sheet_data DB 저장
        ▼
  (03 저장소) SQLite: report_session / report_analysis_summary / report_object_info
             storage_gateway: summary_text / issue_table_text / chart_png / issue_img
        ▲
        │  (02 조회·수정) GET /pe/report/ , /api/history , /session/<id>/full ...
        ▼
[브라우저] 검색결과 페이지 → 세션 상세 (보기/수정/삭제)

[별도 채널] (04 Honey 업데이트) GET /honey/version, /honey/download
```

> **web_report 병행 흐름 (신규 개발 중심)**: Honey 가 honeyform parquet 를
> `POST /pe/report/upload_webreport` 로 올리면 서버가 세션 생성 + storage 보관 후
> 계산해 렌더한다 — 진입점 [server/upload_webreport.py](../server/upload_webreport.py),
> 구현은 [web_report/](../web_report/) (전용 CLAUDE.md 참조). comment/override 편집은
> **세션 편집 DB(report_webreport_edit)** 에 저장되고 manifest 는 업로드 시점 불변
> 스냅샷이다 (2026-07-11).

---

## 1. 기능 → 문서 매핑 (큰 기능 7개)

| # | 큰 기능 | 영역 | 문서 | 진입 파일 |
|---|---------|------|------|-----------|
| 01 | **xlsx 업로드 파이프라인** (수신→해시→S3→파싱→DB) | Server | [01_server_upload.md](01_server_upload.md) | [server/upload_xlsx.py](../server/upload_xlsx.py) |
| 02 | **조회·수정·삭제·주석·차트 서빙** | Server | [02_server_query_edit.md](02_server_query_edit.md) | [server/report/report_routes.py](../server/report/report_routes.py) |
| 03 | **저장소 (SQLite 스키마 + storage_gateway/S3 키)** | Server / DB | [03_storage.md](03_storage.md) | [server/storage_gateway/](../server/storage_gateway/) |
| 04 | **Honey 자동 업데이트 채널** (배포/버전/설치) | Server + Client | [04_honey_update.md](04_honey_update.md) | [server/honey_routes.py](../server/honey_routes.py) |
| 05 | **Honey 클라이언트 UI / 워크플로우** | Client | [05_client_ui.md](05_client_ui.md) | [client/honey_main.py](../client/honey_main.py) |
| 06 | **로컬 분석 엔진** (CSV→분석→xlsx 생성) | Client | [06_analysis_engine.md](06_analysis_engine.md) | [client/report_generator/](../client/report_generator/) |
| 07 | **업로드 전송** | Client | [07_client_upload_chart.md](07_client_upload_chart.md) | [client/transport/uploader.py](../client/transport/uploader.py) |

> 서버 부팅 자체: [server/wsgi.py](../server/wsgi.py) → `report_bp`([01](01_server_upload.md)/[02](02_server_query_edit.md)) + `honey_bp`([04](04_honey_update.md)) 등록.
> Blueprint 등록 트리거는 [server/report/report_extension.py](../server/report/report_extension.py)
> (import 시 라우트 평가, `init_app` 이 DB init + web_report 저장 포트 주입 — 컴포지션 루트).
> `report_routes.py` 는 집결자 — 실제 라우트는 `security.py` / `routes_session.py` /
> `routes_webreport.py` / `routes_misc.py` (2026-07-11 SRP 분리, URL 불변).

---

## 2. 핵심 개념 사전 (전 문서 공통 용어)

- **analysis_key** — `sha256(canonical(sheet_grids) + canonical(meta))`. 같은 grid+meta 면 항상 같은 키.
  meta = `{product_type, product, lot_id}` 만 (PIN 제외). 모든 S3 키/DB 행의 기준 식별자.
  산출: [upload_xlsx.py `_compute_analysis_key`](../server/upload_xlsx.py#L71).
- **session_id** — `"<epoch>_<hex6>"`. 업로드 1건 = 1 세션. 브라우저 조회 단위.
- **mass_data (df_honey)** — 입력 CSV/시트 1개 = 측정 데이터 1단위, **단일 DataFrame 보유**. 분석 엔진의 기본 객체 → [06](06_analysis_engine.md).
- **subject** — 측정 항목(컬럼). **bin** — 합격/불량 분류 코드 (`PASS_BIN="1"` 이 합격).
- **source** (DB 컬럼) — `'web_report'`(신규) / `'xlsx_upload'` / `'analyze'`(legacy, 비활성).
- **신원(uid)** — Honey 내장 브라우저 UA 의 `HoneyUser/<계정>` 토큰으로 자동 식별
  ([server/auth_identity.py](../server/auth_identity.py) provider 체인 — `AUTH_SSO_HEADER`
  env 로 SSO 헤더 전환). 일반 브라우저 = 신원 없음 = 읽기 전용. 구 PIN 검사는 폐지
  (컬럼은 보존). 신원은 analysis_key 에 **불포함**.
- **edits_rev** — 세션 편집 DB(report_webreport_edit_rev)의 단조 증가 rev.
  comment/override 편집 시 증가하며 REPORT/TRIM//full 캐시 키의 무효화 토큰
  ([web_report/cache_policy.py](../web_report/cache_policy.py) 참조).
- **object_type** (report_object_info) — `summary_text` / `issue_table_text` / `chart_index` /
  `web_report_source_<idx>` / `web_report_manifest` → [03](03_storage.md). options_json 에
  저장 위치(`{"storage":"s3"|"local"}`)가 기록되고 조회가 그 기록을 따른다 (2026-07-11).

---

## 3. "이걸 고치려면 어디?" 빠른 인덱스

| 하고 싶은 것 | 문서 | 함수/위치 |
|--------------|------|-----------|
| 업로드 받는 필드/검증 바꾸기 | [01](01_server_upload.md) | `upload_xlsx()`, `_validate_meta()` |
| xlsx 시트 파싱 규칙 바꾸기 | [01](01_server_upload.md) | [xlsx_parser.py](../server/xlsx_parser.py) `parse_report_xlsx` |
| 검색결과 필터/목록 컬럼 | [02](02_server_query_edit.md) | `history()`, `get_history()` ([routes_misc.py](../server/report/routes_misc.py) / [sessions.py](../server/database/sessions.py)) |
| 세션 상세에 데이터 추가 | [02](02_server_query_edit.md) | `session_full()` ([routes_session.py](../server/report/routes_session.py)) |
| web_report comment/override 편집 | — | [web_report/edits.py](../web_report/edits.py) + [database/webreport_edits.py](../server/database/webreport_edits.py) (세션 편집 DB — manifest 불변) |
| 수정 모드 저장 동작 (구 xlsx) | [02](02_server_query_edit.md) | `update_session_content()` — 비활성(항상 405) |
| DB 컬럼/테이블 추가 | [03](03_storage.md) | [database/core.py](../server/database/core.py) `SCHEMA`, `_migrate()` (report_db.py 는 facade) |
| web_report 캐시 키/무효화 | — | [web_report/cache_policy.py](../web_report/cache_policy.py) (키 구성 단일 진실) |
| 콜드 빌드 워커/프리웜 | — | [web_report/compute.py](../web_report/compute.py) (`WEB_REPORT_COMPUTE_WORKERS`) |
| 새 탭 추가 | — | [web_report/tabs/__init__.py](../web_report/tabs/__init__.py) `TAB_REGISTRY` + 프런트 JS 1개 |
| 세션 상세 탭 UI (JS) | — | [server/report/static/webreport/](../server/report/static/webreport/) 15개 모듈 (순서 로드 — report_view.html 은 마크업+CSS) |
| 신원/SSO 전환 | — | [server/auth_identity.py](../server/auth_identity.py) (`AUTH_SSO_HEADER`) |
| 감사 로그(업/수정/삭제) 기록·조회 | [02](02_server_query_edit.md) | `report_db.log_audit`/`get_audit_logs`, [admin_routes.py](../server/admin_routes.py), 대시보드 `/pe/admin` |
| S3 키 경로 바꾸기 | [03](03_storage.md) | [_s3.py](../server/storage_gateway/_s3.py) `make_*_key` + [config.py](../server/config.py) |
| 새 Honey 버전 배포 | [04](04_honey_update.md) | `version.json` + release 스크립트 |
| 클라 화면/버튼 동작 | [05](05_client_ui.md) | `HoneyMainWindow` 슬롯 |
| 분석 수식(cpk/yield 등) | [06](06_analysis_engine.md) | [_builders.py](../client/report_generator/_builders.py) |
| 생성 xlsx 레이아웃/차트 | [06](06_analysis_engine.md) | [xlsx_writer.py](../client/report_generator/xlsx_writer.py) |
| 업로드 multipart 형식 | [07](07_client_upload_chart.md) | `post_xlsx()` |
| DB 파일이 뭐가 있는지 / 백업·stdinfo 정책 | [09](09_db_inventory.md) | [db_backup.py](../server/db_backup.py), [config.py](../server/config.py) |

## 3.1 외부 소유 경계 / 진입점

| 경계 | 외부 브랜치 진입점 | 기본 구현 | 유지 계약 |
|------|-------------------|-----------|-----------|
| D1 입력 | [client/d1/](../client/d1/) `get_provider`, `list_files`, `D1BrowserDialog` ([README](../client/d1/README.md)) | `HONEY_D1_STORAGE` 또는 `client/d1_storage` 로컬 검색 | Honey UI 는 provider 결과 경로 목록만 사용 |
| 서버 저장소/S3 | [server/storage_gateway/](../server/storage_gateway/) ([README](../server/storage_gateway/README.md)) | 내부 `_s3` 어댑터 + 로컬 fallback | `/pe/report/...` URL, multipart 필드, 응답 JSON 유지 |
| 사용자 담당 리포트 | [client/report_generator/](../client/report_generator/) + [client/report_flow/](../client/report_flow/) | 분석/xlsx 생성/업로드 전처리 | 분석 수식, xlsx 레이아웃, DB 스키마 변경 없음 |

## 3.2 컴포넌트별 README (설정·실행·환경변수)

| 컴포넌트 | README |
|----------|--------|
| 서버 전체 (환경변수·API 목록·모듈 구조) | [server/README.md](../server/README.md) |
| 클라이언트 (설치·빌드·워크플로) | [client/README.md](../client/README.md) |
| E2E 검증 절차 | [README.md](../README.md) |

---

## 4. 불변 규칙 (위반 금지 — CLAUDE.md §5 요약)

1. 원본 xlsx 는 서버로 전송·저장하지 않는다 (클라가 grid 추출 후 전송; 서버는 openpyxl·Excel 미사용).
2. 분석 라우트(analyze/execute/plot)는 `/pe/report/` 에 추가하지 않는다.
3. `report_` prefix 없는 새 테이블 금지.
4. analysis_key = `sha256(canonical(sheet_grids) + json.dumps(meta, sort_keys=True))`. meta 바뀌면 키도 바뀜.
5. 실행 중 exe 직접 덮어쓰기 금지 (Windows 락) — 설치본 재설치 방식 → [04](04_honey_update.md).
6. 신규 개발의 중심은 `web_report/` 웹페이지 구현이다. `web_report/` 밖 기존 서버/클라이언트/분석 엔진 변경은
   먼저 사용자에게 이유와 영향 범위를 설명하고 확인받은 뒤 진행한다.
7. **manifest 는 업로드 시점 불변 스냅샷** (2026-07-11) — comment/override 편집은 세션
   편집 DB(report_webreport_edit)에만 기록한다. 편집 경로에서 manifest 를 재저장하는
   코드를 되살리지 말 것 (lost-update·S3 부활 버그의 근원이었다).
8. web_report 파생 캐시의 키는 [web_report/cache_policy.py](../web_report/cache_policy.py)
   빌더로만 만든다 — 키 구성 규약을 호출부 주석으로 흩뿌리지 말 것.

---

## 5. 비활성 코드

`_reference/` 는 원본 plotly 프로젝트(CSV 분석/시각화/Dash)의 보존본. 현재 흐름과 분리돼
있고 라우트로 노출되지 않는다. 분석 재활성화가 필요할 때만 참고. 본 docs/ 는 다루지 않는다.

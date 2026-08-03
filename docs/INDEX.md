# docs/ — 기능별 코드 흐름 지도

이 폴더는 COINAPI report_server 를 기능별로 쪼개 코드 흐름을 정리한 곳이다.
프로젝트 최상위 [CLAUDE.md](../CLAUDE.md) 는 "규칙/디렉토리 인덱스" 요약본이고,
여기 docs/ 는 "각 기능이 실제로 어떻게 흐르는가" 를 추적한 작업용 메모다.

> 읽는 순서 팁: 무엇을 고치려는지 정해지면 아래 표에서 해당 기능 문서 1개만 열면 된다.
> 문서끼리는 `→` 로 연결돼 있으니 경계(서버↔클라이언트)에서만 옆 문서로 점프.

---

## 0. 한눈에 보는 전체 데이터 흐름

```
[Honey 클라이언트 (PyQt6, 사용자 PC + Excel)]
  d1/ provider(외부·검증용) 에서 CSV/xlsx 선택 (기본: d1_storage 로컬 폴더)
        │  (06 분석 엔진: csv → 분석 → xlsx 생성)
        ▼
  xlsx 자동 저장 ──(05 UI)──► "서버에 업로드" 클릭
        │  (07 업로드: grid 추출 + multipart POST)
        ▼
══════════ HTTP ══════════════════════════════════════════
        ▼
[Flask 서버 (헤드리스)]
  POST /pe/report/upload_xlsx ──(01 업로드 파이프라인)──►
        │  sha256(grids)→analysis_key, grid 파싱, sheet_data DB 저장
        ▼
  (03 저장소) SQLite: report_session / report_analysis_summary / report_object_info …
             storage_gateway: issue_img / distribution_combined / web_report parquet
        ▲
        │  (02 조회·접근제어) GET /pe/report/ , /api/history , /session/<id>/full ...
        ▼
[브라우저] 검색결과 페이지 → 세션 상세

[별도 채널] (04 Honey 업데이트) GET /honey/version, /honey/download
```

> **web_report 병행 흐름 (신규 개발 주 대상)**: Honey 가 honeyform parquet 를
> `POST /pe/report/upload_webreport` 로 올리면 서버가 세션 생성 + storage 보관 후 계산해
> 렌더한다. 진입점 [upload_webreport.py](../server/upload_webreport.py), 구현
> [web_report/](../web_report/). 기능 정본은 docs [10 파이프라인](10_web_report_pipeline.md) ·
> [11 탭](11_web_report_tabs.md) · [12 캐시](12_web_report_cache.md). comment/override 편집은
> **세션 편집 DB(report_webreport_edit)** 에 저장되고 manifest 는 업로드 시점 불변 스냅샷.
> ai_comment 옵션 세션은 콜드 빌드 시 [eval_analyzer](../eval_analyzer/) `evaluate()` 를 호출해
> IssueTable 에 AI Comment 컬럼을 채운다 → [13 통합 규약](13_eval_analyzer_integration.md).
> 반대로 IssueTable 의 PTE/개발 comment 는 업로드·편집 시마다 eval 스키마 **별도 DB**
> (`REPORT_EVAL_DB_PATH`)로 export 된다 ([web_report/eval_export.py](../web_report/eval_export.py),
> docs/13 §9 — eval_analyzer 선례 소비용, 관리자 Eval DB 탭에서 관리).

---

## 1. 기능 → 문서 매핑

| # | 큰 기능 | 영역 | 문서 | 진입 파일 |
|---|---------|------|------|-----------|
| 01 | **xlsx 업로드 파이프라인** (수신→해시→저장→파싱→DB) | Server | [01_server_upload.md](01_server_upload.md) | [server/upload_xlsx.py](../server/upload_xlsx.py) |
| 02 | **조회·접근제어·삭제·주석** | Server | [02_server_query_edit.md](02_server_query_edit.md) | [routes_session.py](../server/report/routes_session.py) / [security.py](../server/report/security.py) |
| 03 | **저장소 (SQLite 스키마 + storage_gateway/S3)** | Server / DB | [03_storage.md](03_storage.md) | [server/database/core.py](../server/database/core.py) |
| 04 | **Honey 자동 업데이트 채널** | Server + Client | [04_honey_update.md](04_honey_update.md) | [server/honey_routes.py](../server/honey_routes.py) |
| 05 | **Honey 클라이언트 UI / 워크플로우** | Client | [05_client_ui.md](05_client_ui.md) | [client/honey_main.py](../client/honey_main.py) |
| 06 | **로컬 분석 엔진** (외부 담당자·동결) | Client | [06_analysis_engine.md](06_analysis_engine.md) | [client/report_generator/](../client/report_generator/) |
| 07 | **업로드 전송** (xlsx grid) | Client | [07_client_upload_chart.md](07_client_upload_chart.md) | [client/transport/uploader.py](../client/transport/uploader.py) |
| 09 | **DB 파일 인벤토리** (백업·기준정보(product_info) 정책) | Server / DB | [09_db_inventory.md](09_db_inventory.md) | [server/db_backup.py](../server/db_backup.py) |
| 10 | **web_report 파이프라인** (upload→ingest→저장→로드) | Server | [10_web_report_pipeline.md](10_web_report_pipeline.md) | [server/upload_webreport.py](../server/upload_webreport.py) |
| 11 | **web_report 탭 계약 & 렌더** | Server | [11_web_report_tabs.md](11_web_report_tabs.md) | [web_report/tabs/](../web_report/tabs/) |
| 12 | **web_report 캐시 & 컴퓨트** | Server | [12_web_report_cache.md](12_web_report_cache.md) | [web_report/cache.py](../web_report/cache.py) |
| 13 | **eval_analyzer 통합 (AI Comment + 코멘트 export + /pe/eval 룰 패널)** — 단방향 의존 규약 | Server | [13_eval_analyzer_integration.md](13_eval_analyzer_integration.md) | [web_report/ai_comment.py](../web_report/ai_comment.py) / [web_report/eval_export.py](../web_report/eval_export.py) / [web_report/eval_debug.py](../web_report/eval_debug.py) |
| 14 | **외부 담당자 영역 ↔ 이 프로젝트 병합 순서** — report_generator/storage_gateway 교체 순서·계약 | Server + Client | [14_merge_order.md](14_merge_order.md) | [client/map_report/](../client/map_report/) |
| 15 | **소유권 / 수정 권한 경계** (정본) — 자유/사전승인/외부 담당자 영역 | 전체 | [15_ownership.md](15_ownership.md) | — |
| 16 | **VOC 게시판** (목록·상세·상태 Open/Close·댓글) — 별도 voc.db, 관리자 판별은 admin 게이트 쿠키 재사용 | Server | [16_voc_board.md](16_voc_board.md) | [server/report/routes_voc.py](../server/report/routes_voc.py) |

> 서버 부팅: [server/wsgi.py](../server/wsgi.py) → [plugin.py](../server/plugin.py)
> `register_report_server` 가 `report_bp` + `honey_bp` + admin_panel + ops 등록.
> `report_routes.py` 는 집결자 — 실제 라우트는 `security.py` / `routes_session.py` /
> `routes_webreport.py` / `routes_misc.py` (URL 불변). `report_extension.init_app` 이 DB init +
> web_report 저장 포트 주입(컴포지션 루트). API 전체 표는 [server/README.md](../server/README.md).

---

## 2. 핵심 개념 사전 (전 문서 공통 용어)

- **analysis_key** — `sha256(canonical(sheet_grids) + canonical(meta))`. 같은 grid+meta 면 항상 같은 키.
  meta = `{product_type, product, lot_id}` 만 (password·신원·mode 제외). 모든 저장소 키/DB 행의
  기준 식별자. (web_report 는 `sha256(canon({files, meta, selected_items}))` → [10](10_web_report_pipeline.md).)
- **session_id** — `"<epoch>_<hex6>"`. 업로드 1건 = 1 세션. 브라우저 조회 단위.
- **mass_data (df_honey)** — 입력 CSV/시트 1개 = 측정 데이터 1단위, **단일 DataFrame 보유**. 분석 엔진의 기본 객체 → [06](06_analysis_engine.md).
- **subject** — 측정 항목(컬럼). **bin** — 합격/불량 분류 코드 (`PASS_BIN="1"` 이 합격).
- **source** (DB 컬럼) — `'web_report'`(신규) / `'xlsx_upload'` (legacy `'analyze'` 는 비활성).
- **신원(uid)** — Honey 내장 브라우저 UA 의 `HoneyUser/<계정>` 토큰으로 자동 식별
  ([server/auth_identity.py](../server/auth_identity.py) provider 체인 — `AUTH_SSO_HEADER`
  env 로 SSO 헤더 전환). 일반 브라우저 = 신원 없음 = 읽기 전용. 구 password(PIN) 검사는 폐지
  (dead code, 컬럼 보존). 신원은 analysis_key 에 **불포함**. 접근제어 → [02](02_server_query_edit.md).
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
| 세션 메타(이름·Product·LOT·Process) 수정 | [02](02_server_query_edit.md) | `PATCH /session/<sid>/meta` ([routes_session.py](../server/report/routes_session.py)) + Honey `SessionMetaDialog` ([05](05_client_ui.md)) — 웹 ✏️ → 내장 브라우저 가드 브리지 |
| 수정 모드 저장 동작 (구 xlsx) | [02](02_server_query_edit.md) | `update_session_content()` — 비활성(항상 405) |
| DB 컬럼/테이블 추가 | [03](03_storage.md) | [database/core.py](../server/database/core.py) `SCHEMA`, `_migrate()` (report_db.py 는 facade) |
| web_report 캐시 키/무효화 | — | [web_report/cache_policy.py](../web_report/cache_policy.py) (키 구성 단일 진실) |
| Distribution ECDF 계산 (서버 폴백=클라 프리컴퓨트 공용) | [10](10_web_report_pipeline.md)·[12](12_web_report_cache.md) | [web_report/dist_blob.py](../web_report/dist_blob.py) `compute_dist_compact` |
| 콜드 빌드 워커/프리웜 | — | [web_report/compute.py](../web_report/compute.py) (`WEB_REPORT_COMPUTE_WORKERS`) |
| 새 탭 추가 | — | [web_report/tabs/__init__.py](../web_report/tabs/__init__.py) `TAB_REGISTRY` + 프런트 JS 1개 |
| IssueTable AI Comment / eval_analyzer 연결 | [13](13_eval_analyzer_integration.md) | [web_report/ai_comment.py](../web_report/ai_comment.py) `safe_build` + 코멘트 export [web_report/eval_export.py](../web_report/eval_export.py) + 룰 패널 [web_report/eval_debug.py](../web_report/eval_debug.py) (eval_engine import 3곳) |
| eval 임계값/signature 바꾸기 · 왜 이 코멘트가 나왔나 | [13 §11](13_eval_analyzer_integration.md) | `/pe/eval` ([server/eval_panel/](../server/eval_panel/)) — 제품군×family 오버레이 `eval_analyzer/eval_engine/rules/thresholds/`, 저장 즉시 반영 |
| 세션 상세 탭 UI (JS) | — | [server/report/static/webreport/](../server/report/static/webreport/) 15개 모듈 (순서 로드 — report_view.html 은 마크업+CSS) |
| 신원/SSO 전환 | — | [server/auth_identity.py](../server/auth_identity.py) (`AUTH_SSO_HEADER`) |
| 감사 로그(업/수정/삭제) 기록·조회 | [02](02_server_query_edit.md) | `report_db.log_audit`/`get_audit_logs`, 대시보드 `/pe/admin-pte/` ([admin_panel/](../server/admin_panel/)) |
| S3 키 경로 바꾸기 | [03](03_storage.md) | [_s3.py](../server/storage_gateway/_s3.py) `make_*_key` + [config.py](../server/config.py) |
| 새 Honey 버전 배포 | [04](04_honey_update.md) | `version.json` + release 스크립트 |
| 클라 화면/버튼 동작 (사전 승인) | [05](05_client_ui.md) | `HoneyMainWindow` 슬롯 |
| 분석 수식(cpk/yield 등) (외부 담당자·동결) | [06](06_analysis_engine.md) | [_builders.py](../client/report_generator/_builders.py) |
| 생성 xlsx 레이아웃/차트 (외부 담당자·동결) | [06](06_analysis_engine.md) | [xlsx_writer.py](../client/report_generator/xlsx_writer.py) |
| 업로드 multipart 형식 | [07](07_client_upload_chart.md) | `post_grids()` |
| DB 파일이 뭐가 있는지 / 백업 정책 | [09](09_db_inventory.md) | [db_backup.py](../server/db_backup.py), [config.py](../server/config.py) |
| 기준정보(part_ids/제품 카탈로그) 갱신하려면 | [09 §3](09_db_inventory.md) | [tools/product_info_import/](../tools/product_info_import/README.md), [server/product_info.py](../server/product_info.py) |

## 3.1 외부 담당자 영역 (동결) / 진입점

이 4종은 병합돼 들어왔으나 **외부 담당자가 소유·교체하는 동결 영역**이다(수정 불가피 시 명시 승인).
소유권/수정 권한 티어 정본은 [15_ownership.md](15_ownership.md) — 여기 표는 각 경계의 **진입점·
유지 계약**만 정리한다. `D1`·`S3` 는 현재 코드에서 검증용(로컬 폴백)으로 쓰인다.

| 경계 | 진입점 | 기본(검증용) 구현 | 유지 계약 |
|------|--------|-------------------|-----------|
| D1 입력 (검증용) | [d1/](../d1/) `get_provider`/`list_files`/`D1BrowserDialog` | `HONEY_D1_STORAGE` 또는 `d1_storage/` 로컬 검색 (Honey.exe 호환성 테스트) | Honey UI 는 provider 결과 경로 목록만 사용 |
| 서버 저장소/S3 (검증용) | [storage_gateway/](../server/storage_gateway/) ([README](../server/storage_gateway/README.md)) | 내부 `_s3` 어댑터 + 로컬 fallback (미설정 시 로컬) | `/pe/report/...` URL·multipart·응답 JSON·저장 위치 기록 계약 |
| 리포트 생성 (무수정) | [client/report_generator/](../client/report_generator/) ([README](../client/report_generator/README.md)) | 분석/xlsx 생성 | 분석 수식·xlsx 레이아웃·DB 스키마 불변 |
| 입력 파서 (무수정) | [client/honey_parse/](../client/honey_parse/) `file_to_df` | 현재 더미 폴백 (실제 프로젝트로 교체 시 덮어씀) — **더미는 아직 구형 5-meta 반환** | `(df, df_yield)` 반환 계약. **df = 7-meta honeyform**, 반환 df 개수 = source 개수(병합은 파서 내부) → [06](06_analysis_engine.md) |

## 3.2 컴포넌트별 README (설정·실행·환경변수)

| 컴포넌트 | README |
|----------|--------|
| 서버 전체 (환경변수·API 목록·모듈 구조) | [server/README.md](../server/README.md) |
| 클라이언트 (설치·빌드·워크플로) | [client/README.md](../client/README.md) |
| E2E 검증 절차 | [README.md](../README.md) |

---

## 4. 불변 규칙 (정본 [CLAUDE.md §5](../CLAUDE.md) — 제목만)

1. 원본 xlsx 미저장 (클라가 grid 추출; 서버 openpyxl·Excel 미사용).
2. `report_` prefix 없는 새 테이블 금지.
3. analysis_key = `sha256(canonical(sheet_grids) + canonical(meta))`. meta 바뀌면 키도 바뀜.
4. 실행 중 exe 직접 덮어쓰기 금지 (Windows 락) → [04](04_honey_update.md).
5. **Distribution 다운샘플 금지** (미니셀 1000점만 예외 → [11](11_web_report_tabs.md)).
6. web_report 편집 진실 = 세션 편집 DB, manifest 는 불변 스냅샷. 캐시 키는
   [cache_policy.py](../web_report/cache_policy.py) 빌더로만 → [12](12_web_report_cache.md).
7. **소유권/수정 권한 경계** (정본 [15_ownership.md](15_ownership.md)): 자유 수정 =
   `web_report/` + 관련 html + `server/`(storage_gateway 제외) + client 자주 쓰는 영역
   (honey_ui·honey_main·transport·excel_*); 사전 승인 = `client/` 나머지 비동결;
   외부 담당자 영역(건들 때 승인) = `d1/`·`honey_parse/`·`report_generator/`·`storage_gateway/`(§3.1).

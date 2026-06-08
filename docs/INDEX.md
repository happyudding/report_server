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
        │  sha256→analysis_key, S3 업로드, xlsx 파싱
        ▼
  (03 저장소) SQLite: report_session / report_analysis_summary / report_object_info
             storage_gateway: source_xlsx / summary_text / issue_table_text / chart_png
        ▲
        │  (02 조회·수정) GET /pe/report/ , /api/history , /session/<id>/full ...
        ▼
[브라우저] 검색결과 페이지 → 세션 상세 (보기/수정/삭제)

[별도 채널] (04 Honey 업데이트) GET /honey/version, /honey/download
```

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
| 07 | **업로드 전송 + 차트 PNG 렌더** | Client | [07_client_upload_chart.md](07_client_upload_chart.md) | [client/transport/uploader.py](../client/transport/uploader.py) |

> 서버 부팅 자체: [server/wsgi.py](../server/wsgi.py) → `report_bp`([01](01_server_upload.md)/[02](02_server_query_edit.md)) + `honey_bp`([04](04_honey_update.md)) 등록.
> Blueprint 등록 트리거는 [server/report/report_extension.py](../server/report/report_extension.py) (import 시 DB init + 라우트 평가).

---

## 2. 핵심 개념 사전 (전 문서 공통 용어)

- **analysis_key** — `sha256(xlsx_bytes + canonical(meta))`. 같은 xlsx+meta 면 항상 같은 키.
  meta = `{product_type, product, lot_id}` 만 (PIN 제외). 모든 S3 키/DB 행의 기준 식별자.
  산출: [upload_xlsx.py `_compute_analysis_key`](../server/upload_xlsx.py#L71).
- **session_id** — `"<epoch>_<hex6>"`. 업로드 1건 = 1 세션. 브라우저 조회 단위.
- **mass_data (df_honey)** — 입력 CSV/시트 1개 = 측정 데이터 1단위, **단일 DataFrame 보유**. 분석 엔진의 기본 객체 → [06](06_analysis_engine.md).
- **subject** — 측정 항목(컬럼). **bin** — 합격/불량 분류 코드 (`PASS_BIN="1"` 이 합격).
- **source** (DB 컬럼) — `'xlsx_upload'`(현재 흐름) vs `'analyze'`(legacy CSV 분석, 비활성).
- **PIN/password** — 업로드 시 필수 4자리. 수정/삭제 시 검증. analysis_key 에는 **불포함**.
- **object_type** (report_object_info) — `source_xlsx` / `summary_text` / `issue_table_text` / `chart_index` → [03](03_storage.md).

---

## 3. "이걸 고치려면 어디?" 빠른 인덱스

| 하고 싶은 것 | 문서 | 함수/위치 |
|--------------|------|-----------|
| 업로드 받는 필드/검증 바꾸기 | [01](01_server_upload.md) | `upload_xlsx()`, `_validate_meta()` |
| xlsx 시트 파싱 규칙 바꾸기 | [01](01_server_upload.md) | [xlsx_parser.py](../server/xlsx_parser.py) `parse_report_xlsx` |
| 검색결과 필터/목록 컬럼 | [02](02_server_query_edit.md) | `history()`, `get_history()` |
| 세션 상세에 데이터 추가 | [02](02_server_query_edit.md) | `session_full()` |
| 수정 모드 저장 동작 | [02](02_server_query_edit.md) | `update_session_content()` |
| DB 컬럼/테이블 추가 | [03](03_storage.md) | `SCHEMA`, `_migrate()` |
| 감사 로그(업/수정/삭제) 기록·조회 | [02](02_server_query_edit.md) | `report_db.log_audit`/`get_audit_logs`, [admin_routes.py](../server/admin_routes.py), 대시보드 `/pe/admin` |
| S3 키 경로 바꾸기 | [03](03_storage.md) | [_s3.py](../server/storage_gateway/_s3.py) `make_*_key` + [config.py](../server/config.py) |
| 새 Honey 버전 배포 | [04](04_honey_update.md) | `version.json` + release 스크립트 |
| 클라 화면/버튼 동작 | [05](05_client_ui.md) | `HoneyMainWindow` 슬롯 |
| 분석 수식(cpk/yield 등) | [06](06_analysis_engine.md) | [_builders.py](../client/report_generator/_builders.py) |
| 생성 xlsx 레이아웃/차트 | [06](06_analysis_engine.md) | [xlsx_writer.py](../client/report_generator/xlsx_writer.py) |
| 업로드 multipart 형식 | [07](07_client_upload_chart.md) | `post_xlsx()` |

## 3.1 외부 소유 경계 / 진입점

| 경계 | 외부 브랜치 진입점 | 기본 구현 | 유지 계약 |
|------|-------------------|-----------|-----------|
| D1 입력 | [client/d1/](../client/d1/) `get_provider`, `list_files`, `D1BrowserDialog` ([README](../client/d1/README.md)) | `HONEY_D1_STORAGE` 또는 `client/d1_storage` 로컬 검색 | Honey UI 는 provider 결과 경로 목록만 사용 |
| 서버 저장소/S3 | [server/storage_gateway/](../server/storage_gateway/) ([README](../server/storage_gateway/README.md)) | 내부 `_s3` 어댑터 + 로컬 fallback | `/pe/report/...` URL, multipart 필드, 응답 JSON 유지 |
| 사용자 담당 리포트 | [client/report_generator/](../client/report_generator/) + [client/report_flow/](../client/report_flow/) | 분석/xlsx 생성/업로드 전처리 | 분석 수식, xlsx 레이아웃, DB 스키마 변경 없음 |

---

## 4. 불변 규칙 (위반 금지 — CLAUDE.md §5 요약)

1. xlsx 본문은 SQLite 에 넣지 않는다 (S3 + `report_object_info.s3_key`).
2. 분석 라우트(analyze/execute/plot)는 `/pe/report/` 에 추가하지 않는다.
3. `report_` prefix 없는 새 테이블 금지.
4. analysis_key = `sha256(xlsx_bytes + json.dumps(meta, sort_keys=True))`. meta 바뀌면 키도 바뀜.
5. 실행 중 exe 직접 덮어쓰기 금지 (Windows 락) — 설치본 재설치 방식 → [04](04_honey_update.md).

---

## 5. 비활성 코드

`_reference/` 는 원본 plotly 프로젝트(CSV 분석/시각화/Dash)의 보존본. 현재 흐름과 분리돼
있고 라우트로 노출되지 않는다. 분석 재활성화가 필요할 때만 참고. 본 docs/ 는 다루지 않는다.

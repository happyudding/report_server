# AGENT_GUIDE — 프로젝트 전체 요약 (vibe coding 용)

> **이 문서의 목적**: 새 대화에서 agent(Claude 등) 가 토큰을 최소화하면서 코드베이스를 빠르게 파악하도록, 핵심 사실만 추려서 한곳에 모은다.
> 더 깊은 내용은 블록별 문서로 분기.

---

## TL;DR — 한 줄 요약

**기존 Flask 프로젝트(`/`, `/view/<id>`)에 `/pe/report` 분석 모듈을 붙인 구조.** CSV → SQLite 요약 + S3 객체 저장 + Plotly 시각화. 같은 입력은 `analysis_key`(파일 sha256 + 옵션 sha256)로 캐시.

---

## 0. 디렉토리 / 파일 인덱스

```
plotly/
├── wsgi.py                       Flask app, 두 Blueprint + Dash 등록
├── server.py                     bp: cumulative dashboard (/, /upload, /view, /api/<id>/...)
├── report_extension.py           report_bp 정의 + DB init + report_routes import
├── report_routes.py              /pe/report/* 엔드포인트
├── report_analysis_service.py    analysis_key 산출, summary/derived 빌드, 락
├── report_plot_service.py        Plotly JSON 생성·캐시 (S3 우선)
├── report_db.py                  SQLite 스키마/CRUD/lock (DB/pe/report/report.db)
├── report_s3.py                  boto3 호환 S3 client + 키 빌더 + upload/download
├── df_honey.py                   서버 없이 같은 분석을 호출하는 DataFrame 래퍼
├── data_loader.py                CSV → ExcelData (subjects/units/limits/scores/meta)
├── preprocess.py                 to_numeric_clean, cumulative_distribution_full
├── file_handling.py              CSV/Excel/STDF 로더 (STDF/DRM 는 TODO stub)
├── table_builder.py              yield/cpk/fail_items raw 빌더 (모든 service 가 의존)
├── chart_payload.py + figure_builder.py   Plotly trace/layout dict
├── svg_builder.py                per-subject 정적 SVG
├── dataset_builder.py            cumulative dashboard 빌드 (2000셀)
├── page_builder.py               cumulative.html 템플릿
├── dash_dashboard.py             /dash/<id> Dash 탭 UI
├── xlsx_export.py + png_export.py  엑셀/PNG export
├── config.py                     모든 설정 상수 + 환경변수 매핑
├── start.bat / terminate.bat     로컬 기동/종료
├── build.py                      CLI 빌드 (서버 없이)
├── report_analysis_index.html    /pe/report/ 인덱스 페이지
├── report_view.html              /pe/report/view/<session_id> 결과 페이지
├── upload_form.html              업로드 폼 (사용자 인터페이스)
├── DB/pe/report/report.db        SQLite 본체
├── uploads/report/<sid>/         임시 CSV (analyze 후 삭제)
├── data/                         디버그용 입력 (a/b/c_school_updated_call.csv)
├── output/datasets/<id>/         dashboard 산출물 (charts/, thumbs/, cumulative.html)
└── docs/                         본 문서 모음
    ├── 01_report_generation.md
    ├── 02_database.md
    ├── 03_s3_storage.md
    ├── 04_df_honey.md
    ├── 05_server.md
    └── AGENT_GUIDE.md (this)
```

---

## 1. 블록별 한 줄 요약 + 상세 링크

| 블록 | 한 줄 | 상세 |
|------|-------|------|
| Report 생성 | `/pe/report/analyze` 가 CSV 받아 summary/fail_items/issue_table/SVG 까지 만들고 캐시. `/pe/report/plot` 는 Plotly JSON 캐시. analysis_key=sha256(content_hash:options) 로 재사용 판정 | [01_report_generation.md](01_report_generation.md) |
| DB | SQLite WAL, 6 테이블(`report_session`, `report_analysis_summary`, `report_object_info`, `report_analysis_lock`, `report_csv_files`, `report_annotation`). 본문 저장 금지, 위치/요약만 | [02_database.md](02_database.md) |
| S3 | boto3 호환 endpoint. 5종 객체(plotly/csv/fail_items/issue_table/thumbs). env 로 prefix/credential 설정. SVG 는 ThreadPoolExecutor 로 병렬 업로드 | [03_s3_storage.md](03_s3_storage.md) |
| df_honey | 서버 없이 같은 분석을 호출하는 `df_honey` / `df_honey_group` 래퍼. `table_builder` 의 schools 인터페이스를 흉내 | [04_df_honey.md](04_df_honey.md) |
| 서버 구동 | wsgi.py 가 두 Blueprint + Dash 마운트. start.bat/terminate.bat 로 포트 8000 운용 | [05_server.md](05_server.md) |

---

## 2. 핵심 데이터 모델

### 2.1 CSV 형식

| 행 | 내용 |
|----|------|
| 0 | 과목명 (4 메타컬럼 이후) |
| 1 | Units |
| 2 | Lower Limit |
| 3 | Upper Limit |
| 4, 5 | 빈 행 |
| 6+ | DUT 데이터 |

좌측 4 컬럼: `DUT, XCoord, YCoord, Bin`. `Bin == "1"` 이 합격 (PASS).

### 2.2 메모리 객체 흐름

```
load_table(csv_path) → ExcelData(subjects, units, lower_limits, upper_limits, scores, meta)
schools = {file_stem: ExcelData, ...}                    ← report 모듈이 쓰는 인터페이스
df_honey(name, ...) / df_honey_group([honey, ...])       ← 동일 인터페이스의 객체 wrapper

table_builder:
  _build_yield(schools)       → Bin별 count/portion
  _build_cpk(schools)         → subject별 CPK 통계
  _build_fail_items(schools)  → Bin×subject fail 카운트
  _fail_mask_for_table(table) → boolean DataFrame (lo< or >hi)
```

### 2.3 식별자 두 종류

- **session_id** = `{epoch}_{token_hex(3)}` — 사용자 실행 단위, 중복 불가
- **analysis_key** = `sha256(content_hash + ":" + options_json)` — 분석 결과 재사용 단위
  - `content_hash` = 파일들의 정렬된 (name, body) 스트리밍 sha256
  - `options_json` = `json.dumps(opts, sort_keys=True, ensure_ascii=False, separators=(",",":"))`

같은 파일 + 같은 옵션 → 같은 analysis_key → DB summary / S3 객체 모두 재사용.

---

## 3. 저장소 분리 원칙 (불변)

| 데이터 | 어디에? | 왜? |
|--------|---------|-----|
| 원본 CSV | S3 (`pe/report/origin_csv_files/{key}/{file}`) | 크고 가끔 봄. SQLite 부적합. |
| 통계 summary | SQLite (`report_analysis_summary`) | 작고 자주 조회 + 필터링 |
| Plotly JSON | S3 (`pe/report/plotly/{key}.json`) | 매우 큼. DB 저장 절대 금지 |
| fail_items / issue_table JSON | S3 (`pe/report/fail_items|issue_table/{key}.json`) | 동일 사유 |
| per-subject SVG | S3 (`pe/report/thumbs/{key}/{sid}.svg`) | UI 가 `<img>` 로 직접 GET |
| S3 객체 위치 | SQLite (`report_object_info`) | 키만 보관 → 본문은 S3 |
| 사용자 코멘트 | SQLite (`report_annotation`) | 작고 변경 잦음 |
| 임시 업로드 CSV | `uploads/report/<sid>/` → S3 후 즉시 삭제 | 디스크 누수 방지 |
| cumulative dashboard 산출물 | `output/datasets/<id>/` 로컬만 | 별도 흐름, S3 미사용 |

---

## 4. 주요 흐름 (3종)

### 4.1 analyze — `POST /pe/report/analyze`
```
multipart files → session_dir 저장
  → create_session (pending)
  → get_or_compute_analysis
       ├─ hash + options → analysis_key
       ├─ has_summary? → 그대로 반환 (reused=True)
       └─ lock → 분석 실행 → save_summary_batch → release lock
  → _upload_csvs_to_s3
  → upload_derived_if_absent (fail_items / issue_table / SVG)
  → rmtree(session_dir)
  → JSON: {session_id, analysis_key, reused, status, summary}
```

### 4.2 plot — `GET /pe/report/plot/<analysis_key>`
```
object_info('plotly') 존재 & S3 hit? → 다운로드 후 반환
부재/손상 → lock → CSV 정합성 검증 → 재생성 → upload_json_to_s3 → upsert_object_info
```

### 4.3 session full restore — `GET /pe/report/session/<sid>/full`
```
session row + summary + csv_files + 모든 object_info + annotations + thumb URL 템플릿
→ UI 가 이 응답 하나로 화면 완전 복원
```

---

## 5. 환경변수 한눈에

```bash
REPORT_S3_BUCKET           # 필수. 비우면 S3 기능 503
REPORT_S3_ENDPOINT         # 호환 endpoint. AWS 면 비움
REPORT_S3_REGION           # 기본 us-east-1
REPORT_S3_ACCESS_KEY       # 비우면 boto3 기본 자격증명
REPORT_S3_SECRET_KEY
REPORT_S3_PREFIX           # 기본 pe/report/plotly
REPORT_S3_CSV_PREFIX       # 기본 pe/report/origin_csv_files
REPORT_S3_FAIL_PREFIX      # 기본 pe/report/fail_items
REPORT_S3_ISSUE_PREFIX     # 기본 pe/report/issue_table
REPORT_S3_THUMB_PREFIX     # 기본 pe/report/thumbs
REPORT_THUMB_WORKERS       # 기본 8 (SVG 병렬 업로드)
```

모두 [config.py:34-46](../config.py#L34-L46) 에서 import.

---

## 6. 락 / 캐시 / 재시도

| 항목 | 위치 | 값 |
|------|------|----|
| Lock TTL | `REPORT_LOCK_TTL_SEC` | 300s |
| Lock 폴링 간격 | `REPORT_LOCK_POLL_SEC` | 0.5s |
| Lock 최대 대기 | `REPORT_LOCK_MAX_WAIT_SEC` | 60s |
| boto3 retries | `report_s3.get_s3_client` | 3 |
| SQLite busy_timeout | `init_report_db` + `get_conn` | 5000ms |
| Plotly cache key | `report_object_info.object_type='plotly'` | (analysis_key, S3) |
| Summary cache | `report_db.has_summary(akey)` | (analysis_key, SQLite) |

---

## 7. 절대 하지 말 것 (불변 규칙)

1. SQLite 에 raw CSV / Plotly JSON 본문 / 대형 matrix 저장 금지
2. session_id 기반으로 캐시/재사용 판단 금지 (반드시 analysis_key)
3. 파일명 기반 hash 금지 (이름 + 본문 같이 hash, 본문은 streaming)
4. `/pe/report` 외부에 새 라우트 추가 금지 (기존 모듈과 충돌 방지)
5. `report_` prefix 없는 새 테이블 추가 금지
6. Plotly JSON 을 로컬 캐시 파일로 저장 금지 (S3 만)
7. analyze 가 끝나도 `uploads/report/<sid>/` 를 남기지 않을 것
8. `report_object_info.s3_key` 의 형식을 키 빌더 함수 우회해서 만들지 말 것

---

## 8. 자주 묻는 위치 (코드 포인터)

| 알고 싶은 것 | 어디? |
|--------------|-------|
| analysis_key 만드는 함수 | [report_analysis_service.py:46-72](../report_analysis_service.py#L46-L72) |
| summary 빌드 로직 | [report_analysis_service.py:117-185](../report_analysis_service.py#L117-L185) |
| S3 키 패턴 | [report_s3.py:57-166](../report_s3.py#L57-L166) |
| DB 스키마 전체 | [report_db.py:7-92](../report_db.py#L7-L92) |
| 락 로직 | [report_db.py:348-371](../report_db.py#L348-L371) |
| Plotly 캐시 흐름 | [report_plot_service.py:134-187](../report_plot_service.py#L134-L187) |
| CSV 입력 행 위치 상수 | [config.py:11-15](../config.py#L11-L15) |
| ExcelData 정의 | [data_loader.py:12-19](../data_loader.py#L12-L19) |
| Blueprint 등록 | [wsgi.py:1-15](../wsgi.py#L1-L15) |
| df_honey class (단일 파일 분석 객체) | [df_honey.py:22](../df_honey.py#L22) |
| df_honey.from_file / from_df | [df_honey.py:34-67](../df_honey.py#L34-L67) |
| df_honey 분석 메서드 (cpk/yield/dist/fail) | [df_honey.py:75-155](../df_honey.py#L75-L155) |
| df_honey_group (다중 비교/통합) | [df_honey.py:164](../df_honey.py#L164) |

---

## 9. 변경 시 체크리스트

- DB 컬럼 추가 → `SCHEMA` + `_migrate` 양쪽 수정 ([02_database.md §5](02_database.md#5-마이그레이션))
- S3 새 객체 종류 → `report_s3.make_*_s3_key` 추가 + `report_object_info.object_type` 새 값
- 새 라우트 → `/pe/report/` prefix 유지, `report_routes.py` 에 등록, 키 검증(`_validate_*`) 필수
- 새 분석 함수 → `table_builder` 에 schools→rows 형태로 추가, df_honey 가 자동 노출
- 캐시 우회 디버깅 → `report_db.has_summary` / `get_object_info` 의 반환값을 강제로 None 으로 만들면 재계산 진입

---

## 10. 외부 문서 / 원본 자료

- 설계 의도 / 결정: [../아키텍처_DB관련.txt](../아키텍처_DB관련.txt)
- 데이터 형식·UI 설계: [../아키텍처_허니.txt](../아키텍처_허니.txt)
- 빌드 흐름 다이어그램: [../flowchart.txt](../flowchart.txt)
- 분석 모듈 요구사항: [../plan.txt](../plan.txt)
- 서버 효율 노트: [../NOTES_plotly_scatter_server_efficiency.md](../NOTES_plotly_scatter_server_efficiency.md)

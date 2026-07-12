# report_server 컨텍스트 (eval_analyzer 개발자용)

> eval_analyzer 를 바이브코딩할 때 "데이터가 어디서 어떻게 오는지" 충분히 알기 위한 문서.
> **eval_analyzer 는 report_server 코드를 import 하지 않는다([CODE_TO_PORT.md](CODE_TO_PORT.md)).**
>
> ★★ **중요**: report_server 의 DB(`report.db`)는 **전면 개편 예정**이다. 아래 현재 스키마는
> "지금은 데이터가 이렇게 쌓인다"는 *이해용 참고*일 뿐, eval_analyzer 는 **report.db 구조에
> 의존하지 말 것**. eval_analyzer 의 DB 적재는 자체 eval.db 로 **새로 시작**(greenfield)한다.

---

## 1. report_server 가 하는 일 (한 줄)
외부 report generator 가 만든 `.xlsx` 산출물을 **Honey 클라이언트(PyQt)** 가 서버로 업로드하고,
**Flask 서버** 가 SQLite + S3 에 세션 단위로 저장한 뒤 검색결과 페이지로 조회한다.
분석/플롯 파이프라인은 비활성(코드는 `_reference/` 보존).

## 2. 데이터 흐름 — DB 관점에서 "무엇이 어떻게 쌓이나" (현재)
```
[Honey 클라이언트]                         [Flask 서버]                       [저장소]
Excel COM 으로 .xlsx 열어:                  POST /pe/report/upload_xlsx
  · DRM 해제                                  ↓
  · summary/yield/issue_table 시트의          analysis_key = sha256(
    grid(2D 배열) 추출                          canonical(sheet_grids) + canonical(meta))
  · issue_table 행별 분포 PNG 추출            ↓
  → sheet_grids(JSON) + issue_img_<row>      create_session(...) → report_session  ── SQLite
    만 전송 (★원본 xlsx 는 안 보냄)           parse_report_xlsx(grids):
                                               · yield_rows → report_analysis_summary ── SQLite
                                               · summary/yield/issue_table 텍스트
                                                 → report_sheet_data (JSON blob)      ── SQLite
                                               · issue PNG → S3 (로컬 폴백)           ── S3
```
**핵심 결론 (eval_analyzer 가 알아야 할 점):**
- 서버는 **openpyxl/Excel 을 쓰지 않는다.** 클라가 뽑은 grid 텍스트만 받는다.
- **원본 .xlsx 도, per-DUT raw 측정값도 서버에 저장되지 않는다.** 서버 DB 엔 *집계 텍스트*만.
- 따라서 cpk/산포/좌표/site 같은 **분포 기반 수치는 현재 report.db 에 없다**(아래 §4).

## 3. 식별자: analysis_key
- `analysis_key = sha256(canonical(sheet_grids) + canonical(meta))`. canonical = `json.dumps(sort_keys=True)`.
- 같은 데이터+메타면 동일 key(재업로드 idempotent). PIN(비밀번호)은 key 산출에 미포함.
- eval_analyzer 의 `ingest_run.analysis_key` 는 이 값을 *선택적으로* 링크해 둘 수 있으나 의존 금지(개편 예정).

## 4. 현재 report.db 테이블 (참고 — LEGACY, 개편 예정. 의존 금지)
파일: `server/database/report_db.py` 의 `SCHEMA`. report_ prefix.

| 테이블 | 1행 의미 | 주요 컬럼 | eval_analyzer 관점 |
|---|---|---|---|
| **report_session** | 업로드 세션 1건 | session_id, analysis_key, file_name, status, **product_type, process, product, revision, lot_id**, source, created_at | 메타(product/lot/process/revision) 출처 |
| **report_analysis_summary** | (key,item,bin) 수율행 1건 | item_name, bin_number, **yield_percent, fail_count**, cpk_val, mean_val, stdev_val, lsl, usl, unit | ★ **cpk/stdev/lsl/usl 전부 NULL**, mean_val=*yield avg*(측정 mean 아님) |
| **report_sheet_data** | (key, sheet) 텍스트 1건 | analysis_key, sheet_name('summary'\|'yield'\|'issue_table'), data_json | issue_table 에 engr comment(개발1차) 텍스트 |
| report_object_info | (key, object_type) S3 메타 | object_type, s3_key, s3_uri | issue PNG 등 S3 포인터 |
| report_csv_files | (key, filename) | s3_key, file_size | (현 흐름 미사용) |
| report_audit_log | 업로드/수정/삭제 감사 | action, product_type, lot_id, client_ip | — |
| report_annotation | 주석 | session_id, target, content | — |

> 즉 현재 DB 에 "있는 것" = product/lot/process/revision, item_name·bin, yield_percent·fail_count,
> issue_table/summary/yield 텍스트(+engr comment), issue PNG(S3).
> "없는 것" = **측정 cpk·mean·stdev·lsl·usl(NULL), per-DUT raw, 좌표(x/y), site(DUT), 산포·wafer 패턴**.

## 5. raw → 통계는 어디서 계산되나 (report_generator, 클라/오프라인)
업로드 *전*, 클라이언트(또는 오프라인)의 `report_generator` 가 raw 측정 파일을 처리한다.
**이 단계의 메모리에는 per-DUT raw 가 있다** — eval_analyzer 가 결합 시 받아야 할 지점.

핵심 파일/객체 (report_server repo, `client/report_generator/`):
- **df_honey.py** — 하나의 mass_data(웨이퍼/로트 측정) = 표준 DataFrame `self.df`.
  레이아웃: `columns = [DUT, XCoord, YCoord, Bin, Serial, item1, item2, …]`,
  `row0=Units, row1=Lower Limit, row2=Upper Limit, row3~4=limit 중복, row5~=데이터`.
  열 0~4 = meta(5개), 열 5~ = subject 측정값. (constants: DATA_START_ROW=5 등)
- **_builders.py `get_df_cpk_summary(numeric_df, lo_arr, hi_arr)`** — subject별 n/min/median/max/
  average/stdev/cp/cpl/cpu/cpk (공식은 CODE_TO_PORT.md).
- **_builders.py `cumulative_distribution_full(values)`** — ECDF.
- **df_honey `fail_mask*`** — lo/hi 위반 + 측정중단(break) 판정.
- 정리: **cpk·산포는 report_generator 가 raw 에서 계산하지만, 업로드 시 grid 텍스트만 보내며 수치는 버려진다.**
  → eval_analyzer 는 이 계산을 **자체 구현(CODE_TO_PORT)** 하고, raw 는 결합 시 메모리로 받는다.

## 6. eval_analyzer 가 데이터를 얻는 두 경로
1. **(목표) report_generator 결합** — client 가 파일 run 시 df_honey 의 raw(메모리)를 evaluate() 로 전달.
   여기서 cpk/산포/좌표/site 전부 계산 가능. → [INTEGRATION_CONTRACT.md](INTEGRATION_CONTRACT.md)
2. **(독립 개발) seed/샘플 CSV** — report_server 없이 샘플 raw 1개로 단독 검증.
- report.db 는 **무시**(개편 예정). 굳이 읽지 않는다.

## 7. report_server repo 핵심 파일 (경로 — 코드 볼 때 참조, import 금지)
```
server/upload_xlsx.py                업로드 라우트 (/pe/report/upload_xlsx)
server/xlsx_parser.py                grid(2D) → 텍스트/yield_rows 파싱
server/database/report_db.py         DB 스키마·CRUD (위 §4)
client/report_flow/upload_prepare.py 클라 Excel COM 추출 (grid + PNG)
client/transport/uploader.py         업로드 전송 (post_grids)
client/report_generator/df_honey.py  raw → 표준 DataFrame (§5)
client/report_generator/_builders.py cpk/ECDF 등 통계 빌더 (§5, CODE_TO_PORT)
CLAUDE.md, docs/INDEX.md             프로젝트 개요·기능별 흐름
```

## 8. report_server 불변 규칙 (참고)
- 원본 xlsx/CSV 는 서버에 저장 안 함(집계 텍스트만). 서버는 Excel/openpyxl 미사용.
- **Distribution 차트 데이터 다운샘플링 절대 금지** — 모든 포인트 표현(eval_analyzer 가 산포 계산 시도 동일 정신).
- 새 테이블은 report_ prefix(legacy 기준). ※ 단, report.db 개편 시 이 규칙도 바뀔 수 있음.

## 9. 한 줄 정리 (eval_analyzer 입장)
report_server 는 **메타 + 집계 수율 + 텍스트(engr comment) + issue PNG** 를 갖고 있고,
**측정 cpk·산포·좌표·site 같은 raw 기반 수치는 갖고 있지 않다**(report_generator 가 계산했다 버린다).
그래서 eval_analyzer 는 **raw 를 결합 시 메모리로 받아 직접 계산**하고, **자체 eval.db 로 새로 적재**한다.
report.db 는 개편 예정이므로 **무시**한다.

# COINAPI report_server — Claude Code 진입점



> **세션 시작 규칙**: 새 대화가 시작될 때마다 [docs/INDEX.md](docs/INDEX.md)를
> 먼저 읽어라. 기능별 코드 흐름·파일 위치·불변 규칙이 모두 INDEX에 있다.

**주의사항** : service 중인 server 이기 때문에 코드 변경하였을때 기존 session 이 안열리거나 지장을 주면안됨. 그렇게 되면 사용자에게 반드시 코드 변경을 하기 전에 진짜 변경할건지 확인 질문을 하라.

이 프로젝트는 Honey 클라이언트가 추출한 산출물을 서버로 업로드하고, Flask 서버가
SQLite + S3(또는 로컬 폴백)에 세션 단위로 저장한 뒤 검색결과·세션 상세 페이지로 조회하게
한다. 두 업로드 흐름이 병행한다: **xlsx 추출 grid**(→[docs/01](docs/01_server_upload.md))와
**web_report honeyform parquet**(→[docs/10](docs/10_web_report_pipeline.md), 현재 신규 개발의
주 대상).

**기능별 코드 흐름 추적은 [docs/INDEX.md](docs/INDEX.md) 참조** — 큰 기능별로 각 흐름을
정리한 작업용 메모. 무엇을 고칠지 정해지면 INDEX 표에서 해당 문서 1개만 열면 된다.

**소유권 / 수정 권한 경계** — 정본은 [docs/15_ownership.md](docs/15_ownership.md). 이 문서는
이 프로젝트(웹리포트/서버) 관점이다. 외부 담당자 영역 소유자용 진입 문서는
[CLAUDE.core.md](CLAUDE.core.md). 3-tier 요약:
- 🟢 **자유 수정** (승인 없이 바로): `web_report/` + web_report 관련 html(report_view.html,
  static/webreport/) + `server/`(단 `storage_gateway/` **제외**) + `eval_analyzer/`
  (이 repo 가 원본 — 2026-08-03 승격) + client 자주 쓰는 영역
  (`honey_ui/`, `honey_main.py`, `transport/`, `excel_download/`, `excel_edit/`).
- 🟡 **사전 승인**: `client/` 나머지 비동결 (report_flow/, map_report/, embedded_browser.py,
  client_identity.py, config.py 등) — 편집 전 파일·이유·영향 설명.
- 🔒 **외부 담당자 영역** (건들 때마다 승인): `d1/`·`d1_storage/`·`client/honey_parse/`·
  `client/report_generator/`·`server/storage_gateway/`(facade+`_s3` 전체). 병합돼 들어왔으나
  외부 담당자 소유·교체 대상이라 동결(수정 불가피 시 명시 승인).
  진입점·유지 계약은 [docs/INDEX.md §3.1](docs/INDEX.md).
  (`eval_analyzer/` 는 **더 이상 동결이 아니다** — 자유 수정. 단 import 단방향 규칙 #8 은 유지.)

---

## 0. 디렉토리 인덱스

```
report_server/
├── server/                     Flask 서버 (자유 수정 — 단 storage_gateway 는 외부 담당자·동결. [server/README.md](server/README.md))
│   ├── wsgi.py                  진입점 (컴퓨트 워커 __mp_main__ 재임포트 스킵 가드)
│   ├── plugin.py                register_report_server() — Blueprint 3개 + admin_panel + ops 등록
│   ├── config.py                환경변수·경로 통합 설정
│   ├── auth_identity.py         신원 provider 체인 (기본 HoneyUser UA, AUTH_SSO_HEADER 로 SSO 전환)
│   ├── database/                SQLite 계층 (report_db.py 는 재노출 facade — 호출부 무변경)
│   │   ├── core.py              SCHEMA(정본)·마이그레이션·get_conn·analysis lock
│   │   ├── sessions.py / objects.py / audit.py / users.py / annotations.py / usage.py
│   │   ├── webreport_edits.py   web_report 편집 상태 (세션 단위)
│   │   └── models.py            Session dataclass (Mapping 호환)
│   ├── report/
│   │   ├── report_extension.py  report_bp 정의 + DB init + web_report 저장 포트 주입
│   │   ├── report_routes.py     라우트 집결자 — 구현은 security.py /
│   │   │                        routes_session.py / routes_webreport.py / routes_misc.py
│   │   ├── static/webreport/    세션 상세 JS 모듈 17개 (순서 로드, 전역 공유)
│   │   ├── report_analysis_index.html  검색결과 페이지
│   │   └── report_view.html     세션 상세 (마크업+CSS — JS 는 static/webreport/)
│   ├── storage_gateway/         S3 산출물 저장 단일 진입점 (외부 담당자·동결 — facade+_s3 전체, 진입점 계약 유지)
│   │   ├── __init__.py          facade (공개 API + 예외 재노출 + 저장 위치 기록)
│   │   ├── routes.py            이미지 URL 라우트
│   │   ├── _s3.py              boto3 호환 client + key 빌더 (내부 어댑터)
│   │   ├── _issue_images.py    이슈 이미지 백엔드 (S3+로컬 폴백)
│   │   └── _note_images.py     Note 탭 이미지 백엔드 (S3+로컬 폴백, 세션 단위)
│   ├── chatbot/                 ENGR 이력 검색 챗봇 조회 툴 + CLI ([README](server/chatbot/README.md))
│   │                            **라우트 미등록(CLI 전용)** — 운영 무영향. report.db(세션·이슈)
│   │                            + eval.db(item 축) 두 정본을 read-only 로만 읽는다
│   ├── admin_panel/             /pe/admin-<secret>/ 대시보드 + metrics 샘플러
│   ├── eval_panel/              /pe/eval 룰 관리 (thresholds 제품군/family 오버레이 ·
│   │                            signature on/off · L0~L6 트레이스 — 저장 즉시 반영)
│   ├── tools/migrate_manifest_edits.py  manifest 편집값 → 세션 편집 DB 이전 (운영 1회 실행 완료)
│   ├── upload_xlsx.py           POST /pe/report/upload_xlsx
│   ├── upload_webreport.py      POST /pe/report/upload_webreport (web_report.ingest 호출)
│   ├── xlsx_parser.py           시트 grid → 텍스트 추출 (_GridSheet 셸, openpyxl 미사용)
│   ├── admin_routes.py          [미등록 dead file — 구 /pe/admin, admin_panel 로 흡수됨]
│   ├── honey_routes.py          /honey/version, /honey/download, /honey/announcement
│   └── releases/                version.json(배포 manifest) + announcement.txt(업데이트 공지 원문)
├── web_report/                 웹 리포트 구현 (신규 개발 주 대상 — [web_report/CLAUDE.md](web_report/CLAUDE.md),
│                                docs/[10](docs/10_web_report_pipeline.md)·[11](docs/11_web_report_tabs.md)·[12](docs/12_web_report_cache.md))
├── client/                     Honey 클라이언트 (PyQt6 — honey_ui/honey_main/transport/excel_* 자유, 나머지 사전 승인; report_generator·honey_parse 만 외부 담당자·동결)
│   ├── honey_main.py            메인 윈도우 + 워크플로
│   ├── transport/              uploader / version_check / updater / retry
│   ├── report_flow/            upload_prepare.py (Excel COM 추출) 등
│   ├── report_generator/       (외부 담당자·동결) 분석·xlsx 생성 — 외부 담당자 소유
│   ├── honey_parse/            (외부 담당자·동결) file_to_df 파서 — 현재 더미 폴백
│   ├── map_report/             (사전 승인·신규) 웨이퍼 bin map 렌더 + xlsx 부착 (report_generator 밖으로 분리 → [docs/14](docs/14_merge_order.md))
│   ├── honey_ui/               다이얼로그·위젯
│   ├── excel_download/ · excel_edit/   Excel COM 헬퍼
│   ├── embedded_browser.py     HoneyUser UA 삽입 내장 브라우저
│   ├── client_identity.py      PC 계정/호스트 신고값
│   └── config.py               SERVER_BASE_URL, CURRENT_VERSION
├── eval_analyzer/              독립 fail-item 평가 엔진 (자유 수정 — **이 repo 가 원본**, 외부 사본 동기화 없음)
│                                서버 연결은 web_report/ai_comment.py + eval_export.py 2곳만 → [docs/13](docs/13_eval_analyzer_integration.md)
├── d1/                         (외부 담당자·동결) D1 입력 provider 경계 — 검증용(로컬 d1_storage 검색)
├── d1_storage/                 (외부 담당자·동결) D1 로컬 검증 스토리지
├── tools/product_info_import/  기준정보 CSV(DRM) → product_info.db 오프라인 임포터
│                                (standalone — Excel 있는 별도 PC 에서 실행 후 .db 를 서버로 수동 복사)
├── tools/eval_golden/          eval 룰 골든셋 회귀 (golden.yaml 기대 발화 vs 실제 트레이스 diff
│                                → [docs/13 §12](docs/13_eval_analyzer_integration.md))
├── tests/sample_xlsx.py         더미 grids 픽스처 생성기
├── DB/pe/report/                런타임 자동 생성 (report.db + backup/ + 문서 스냅샷)
├── uploads/                     런타임 (업로드/로컬 폴백/디스크 캐시 루트)
└── docs/                       기능별 흐름 문서 (INDEX.md 가 허브)
```

---

## 1. 데이터 흐름

**Honey → Server (2개 병행 업로드 흐름)**
1. Honey 시작 → `GET /honey/version` → 새 버전 있으면 사용자에게 확인 후 `/honey/download`
2. **xlsx 흐름**: product_type / product / lot_id (+ password 선택) + xlsx 선택. 클라가
   **Excel COM 으로 DRM 해제·시트 셀값을 읽어** summary/yield/issue_table grid(2D 배열)와
   issue_table 행별 PNG 를 추출 → `POST /pe/report/upload_xlsx` (xlsx 파일은 보내지 않고
   `sheet_grids` JSON + `issue_img_<row>` 만 전송). 서버: `sha256(canonical(sheet_grids) +
   meta)` → analysis_key → 세션 생성 → grid 파싱 → yield_rows·sheet_data DB 저장, issue PNG
   는 S3(로컬 폴백). **원본 xlsx 는 저장하지 않는다.** 상세 [docs/01](docs/01_server_upload.md).
   - **web_report 흐름**: honeyform parquet + manifest → `POST /pe/report/upload_webreport`.
     parquet 소스는 **`honey_parse.file_to_df` 가 돌려준 7-meta honeyform(`md.df`) 그대로**다 —
     원본 입력 파일을 디스크에서 다시 읽지 않는다(병합이 honey_parse 안에서 일어나므로 원본을
     재-read 하면 병합 결과를 버린다). 상세 [docs/10](docs/10_web_report_pipeline.md).

**검색결과 조회 / 편집**
- `GET /pe/report/` → 검색결과 페이지, `GET /pe/report/api/history?product_type=MDDI&...` → 세션 목록
- `GET /pe/report/view/<session_id>` → 세션 상세
- `GET /pe/report/session/<sid>/full` → 세션 + summary + objects + annotations + 추출 텍스트
  (응답 session 에서 password 제거, `has_password` 불린만 노출)
- `POST /pe/report/session/<sid>/verify_password` → **하위호환 스텁** (HoneyUser 신원==업로더
  확인만, 항상 `has_password:false` — 구 PIN 검사 폐지)
- `PATCH /pe/report/session/<sid>/meta` → 세션 메타(이름=file_name·family·product·lot·process)
  수정. 세션 상세 ✏️ → **Honey 편집창**(업로드 다이얼로그 재사용) 전용 — 서버가
  `X-Honey-Agent` 헤더를 요구해 강제한다. product 변경 시 product_info 재lookup.
  `analysis_key`·`product_type` 은 불변 → [docs/02](docs/02_server_query_edit.md)
- `PATCH /pe/report/session/<sid>/content` → [비활성] 항상 405 (구 xlsx 텍스트 수정 폐기)
- `POST .../session/<sid>/web_report/issue_table/comments|etc`, `.../summary/engr`,
  `.../trim/overrides`, `.../chart_notes`(차트 주석), `.../note`(Note 탭 시트)
  → web_report 편집 — **세션 편집 DB(report_webreport_edit)에 저장**.
  manifest 는 업로드 시점 불변 스냅샷. Note 이미지는 `.../note_image` → storage_gateway
  (세션 단위 저장, 세션 삭제 시 정리).
- `POST .../session/<sid>/web_report/preprocess` → **조회 전처리**(항목 제외·outlier·
  빠른 수정 셀 패치·조건 일괄 규칙 + 수율 분모 기준). **원본 parquet 을 바꾸지 않고** 조회
  시점에만 적용되는 되돌릴 수 있는 편집이라, 원본을 실제로 교체하는 `raw_data/edit`(웹 셀
  편집)·`rawdata_replace`(Excel 왕복)와 성격이 정반대다 → [docs/11](docs/11_web_report_tabs.md).
- `DELETE /pe/report/session/<sid>` → 세션 삭제 (업로더만)

**인증**: 신원은 Honey 내장 브라우저 User-Agent 의 `HoneyUser/<계정>` 토큰으로 자동 식별한다
([server/auth_identity.py](server/auth_identity.py) — env `AUTH_SSO_HEADER` 설정 시 역프록시
SSO 헤더가 우선, 코드 무변경 전환). 일반 브라우저는 신원이 없어 **읽기 전용**. 가드 3종
([server/report/security.py](server/report/security.py)): `_uploader_guard`(삭제·비공개 토글·
편집자 부여 — 업로더 전용) / `_editor_guard`(콘텐츠 편집 — 업로더 또는 위임 편집자) /
`_private_guard`(비공개 세션 **조회** 차단 — 업로더+위임 편집자 외 404, 목록도 SQL 필터로
숨김, 2026-07-15). **legacy 우회**:
`uploaded_by` 가 빈 세션(= xlsx 업로드 세션)은 Honey 접속 사용자 전원이 편집/삭제 가능하고,
`uploaded_by` 를 채우는 web_report 세션만 업로더 잠금이 실효한다. 구 4자리 password
(`report_session.password`)는 저장·전송되나 **접근제어에 미사용**(선택 입력, 형식만 검사).
신원/password 는 analysis_key 산출 meta 에 **포함하지 않음** (규칙 #4). 접근제어 상세는
[docs/02](docs/02_server_query_edit.md).

---

## 2. DB 스키마 요약 (현행)

**정본은 [server/database/core.py](server/database/core.py) 의 `SCHEMA`.** 전체 테이블·컬럼은
[docs/03](docs/03_storage.md) 와 스냅샷 [DB/pe/report/report_README.md](DB/pe/report/report_README.md)
참조. 테이블 17개 요지:

- `report_session` — 세션 1건. `source`('xlsx_upload'|'web_report'), `mode`('Normal' 기본),
  `uploaded_by`·`client_host`(신원), `webreport_options`, `password`(미사용 보존) 컬럼 포함.
- `report_analysis_summary` — yield/cpk 등 summary 행 (UNIQUE analysis_key,item,bin).
- `report_object_info` — S3/로컬 산출물 위치. `options_json` 에 `{"storage":"s3"|"local"}` 기록.
  `object_type`: `distribution_combined`, `web_report_source_<idx>`, `web_report_manifest`,
  (legacy) `summary_text`/`issue_table_text`/`chart_index` 등.
- `report_sheet_data` — xlsx 추출 텍스트(summary/yield/issue_table). 텍스트는 여기에만 저장.
- `report_audit_log` — upload/edit/delete 감사. 메타 스냅샷 + client_ip/user_agent/client_user
  (클라 신고 계정, 위조 가능) + result. best-effort. `/pe/admin-pte/` 대시보드에서 조회.
- `report_webreport_edit` / `_rev` — web_report 편집(comment/etc/trim override/engr/
  chart_note(차트 주석)/note_sheet(Note 탭 Luckysheet 시트 JSON ≤2MB)/preprocess(조회 전처리
  spec — 항목 제외·outlier·셀 패치·조건 규칙)/yield_basis)의 **진실 저장소,
  세션 단위**. dedup(동일 analysis_key) 세션 간 편집 비공유. `rev` 는 단조 증가 캐시
  무효화 토큰. manifest 는 불변 스냅샷 ([web_report/edits.py](web_report/edits.py)).
- `report_session_editor`(편집 위임) / `report_web_visitor`(편집자 후보 풀) /
  `report_user_important`(개인 중요표시) / `report_user_favorite`(즐겨찾기).
- `report_usage_daily` — 접속 사용량 일별 카운터(Honey 실행·웹 방문, 관리자 통계 탭).
  `kind`=`honey_run/web_index/web_view`, 무신원은 `ip:<addr>` 행.
- `report_annotation` / `report_dashboard_comment` / `report_csv_files` /
  `report_analysis_lock` / `report_user`(ID/PW 로그인 폐지 — 미사용 보존).

`product_type` enum: **MDDI / PDDI / PMIC / SECURITY / TCON** (core.py `_PRODUCT_TYPE_NAMES`).

---

## 3. S3 저장 (외부 담당자 영역·동결, 검증용)

S3/storage_gateway 는 외부 담당자 영역·동결 경계다 — 미설정 시 `REPORT_UPLOAD_DIR` 로컬 폴백으로
동작하며 현재 코드에선 검증용이다. facade·키·저장 위치 기록 계약은 정본
[storage_gateway/README.md](server/storage_gateway/README.md) 참조. 키는 모두
`pe/report_server/` 네임스페이스(plotly legacy 충돌 회피): `issue_img/` · `chart_png/` ·
`distribution_combined/` · `web_report_source/` · `web_report_manifest/`. 실제 값은
[config.py](server/config.py) `REPORT_S3_*_PREFIX`.

**저장 위치 기록**: web_report parquet/manifest 는 저장 위치를 `report_object_info.options_json`
에 `{"storage":"s3"|"local"}` 로 기록하고 조회는 그 기록을 따른다 — 기록이 s3 인데 다운로드
실패 시 침묵 로컬 폴백 대신 예외(복구 후 과거 S3 파일 부활 방지). 텍스트(summary/issue/yield)는
S3 가 아니라 DB `report_sheet_data` 에 저장한다.

---

## 4. 환경변수

**전체 목록·기본값은 정본 [server/README.md](server/README.md)** (web_report 캐시 env 는
[docs/12](docs/12_web_report_cache.md)). 여기는 **행동에 영향 주는 함정**만:

- **서버 주소 정본은 [server/env/server.env](server/env/server.env) 한 파일이다.** `HOST`(bind,
  기본 `0.0.0.0`) / `PORT`(기본 8080) / `SERVER_BASE_URL`(클라가 접속하는 주소,
  기본 `http://12.81.220.117:8080`). 서버는 이 파일을 직접 읽고([server/config.py](server/config.py)),
  클라는 개발 실행 시 같은 파일을, 빌드본은 `build_zip` 이 이 파일에서 생성해 넣은
  `Honey.exe` 옆 `honey.env` 를 읽는다([client/transport/config.py](client/transport/config.py)).
  IP 변경 = server.env 1줄 수정 → 서버 재기동 → `build_zip` 재실행 → 클라 재배포.
  `HOST` 를 운영 IP 로 바꾸지 말 것 — 그 IP 를 가진 PC 외에서 기동이 실패한다(`0.0.0.0` 이 포함).
- `REPORT_S3_BUCKET` 미설정이면 S3 대신 **로컬 폴백**으로 조용히 동작한다(에러 아님). yield
  rows 등 DB 저장은 정상.
- `REPORT_CLEANUP_DRYRUN` 기본 **1(참)** — **기본은 실삭제 안 함**(대상만 로그). 실삭제하려면
  `0` 으로 명시.
- `AUTH_SSO_HEADER` 지정 시 그 역프록시 헤더가 신원으로 우선(기본은 HoneyUser UA).
- `WEB_REPORT_COMPUTE_WORKERS` 기본 2(**운영 server.env 는 3**), `0` = 콜드 빌드 전부
  인라인(구 동작). `WEB_REPORT_ONDEMAND_WORKERS`(202 후 백그라운드 빌드 소비자 스레드)와
  **짝으로** 올려야 한다 — 풀만 늘리면 소비자 스레드 수가 새 상한이 된다.

DB 백업 사이클(db_backup.py)이 매회 `PRAGMA wal_checkpoint(TRUNCATE)` + `PRAGMA optimize` 로
-wal 비대를 막는다. VACUUM 은 장시간 잠금이라 자동 실행 안 함(수동).

---

## 5. 주의 사항 (불변 규칙)

1. 원본 xlsx 는 서버로 전송·저장하지 않는다. 클라이언트가 Excel COM 으로 추출한
   summary/yield/issue_table grid(JSON)와 issue PNG 만 업로드하고, 텍스트는 DB(sheet_data),
   PNG 는 S3(로컬 폴백)에 보관한다. **서버는 openpyxl·Excel 을 쓰지 않는다.**
   - 기준정보 CSV(DRM/NASCA)도 같은 이유로 서버에서 열지 않는다. Excel 이 있는 별도 PC 에서
     [tools/product_info_import](tools/product_info_import/README.md) 로 만든
     `product_info.db` 를 수동 배치하고 서버는 읽기 전용으로만 연다
     ([server/product_info.py](server/product_info.py), [docs/09 §3](docs/09_db_inventory.md)).
2. `report_` prefix 없는 새 테이블 만들지 말 것.
3. analysis_key 산출은 `canonical(sheet_grids) + canonical(meta)` 의 sha256 — 메타 변경 시
   같은 데이터라도 다른 키가 됨. canonical 은 `json.dumps(sort_keys=True)`. password·신원·
   mode 는 **불포함**. (web_report 는 `sha256(canon({files, meta, selected_items}))` —
   [docs/10](docs/10_web_report_pipeline.md).)
   - 이 산출식은 **업로드 시점** 규약이다. 업로드 후 메타를 고치는
     `PATCH /session/<sid>/meta` 는 **analysis_key 를 재산출하지 않는다** — 산출물
     (parquet·manifest·summary)이 전부 그 키로 저장돼 있어 키를 바꾸면 세션이 자기 데이터를
     잃는다. 어긋나는 건 dedup(같은 데이터 재업로드) 매칭뿐이다.
4. 클라이언트 자동 업데이트는 batch 스크립트 + 외부 다운로드 방식. 실행 중인 exe
   에 직접 쓰지 말 것 (Windows 락).
5. **Distribution 차트 데이터 다운샘플링 절대 금지.**
   모든 데이터 포인트를 빠짐없이 차트에 표현해야 한다.
   `_MAX_CDF_POINTS`, `_downsample`, `max_points` 같은 포인트 상한 로직을 절대 추가하지 말 것.
   유일 예외: 동일값 구간을 2포인트 선분으로 표현하는 계단형(step) ECDF 변환
   (`client/report_generator/_builders.py` `cumulative_distribution_full()`), web_report 는
   미니셀 썸네일만 표시용 다운샘플(`DIST.DOWNSAMPLE`, 소스별 소프트 상한 1500
   — [docs/11](docs/11_web_report_tabs.md)). 미니셀의 점은 Plotly SVG 마커가 아니라
   canvas 오버레이(`distPaintPoints`)로 그린다(2026-07-20, 좌표·색·점크기 동일).
   - **web_report Distribution ECDF 미니셀은 markers(점)만으로 렌더한다.** 점을 잇는 선,
     특히 Plotly `line.shape:"hv"` 계단형 수평선은 금지 — x축 방향 수평선은 누적분포를
     왜곡하고 사용자 경험에 반한다. 이산(code unit)값처럼 고유값이 적어 점이 성겨 보이는
     문제는, 동일값 구간(ECDF riser)을 y축 방향 세로 점기둥으로 채우는 보간
     (`distFillVertical`)으로만 해결하고 선으로 잇지 않는다. 보간은 표시용 다운샘플보다
     **먼저** 적용한다(`distPointsForDisplay` = 세로 채움 → 다운샘플). 채움 간격은
     소스별 "단일 점 1개의 증가량"(최소 양의 Δy, `distStepY`)을 시각 연속성 캡
     `FILL_VISUAL_MAX_DY`(0.3%)로 캡해 유도한다 — 표본이 작아 단일점 증가량이 0.3% 를
     넘으면 단일점 riser 포함 모든 riser 를 0.3% 간격 세로 점으로 채워 썸네일 누적
     0~100% 에 marker 빈 구간이 없게 한다(세로 방향 표시용 업샘플링 허용, x값을 만들어내는
     가로 방향 보간은 계속 금지). 조밀한 데이터(stepY≤0.3%)는 캡이 no-op 라 기존과 동일.
     상세 CDF(`distRenderCdf`)는 원본 전량 렌더 별도 경로로 채움·다운샘플 대상 외.
6. **web_report 편집 상태는 세션 편집 DB 가 진실.** manifest 는 업로드 시점 불변 스냅샷이므로
   편집으로 재저장하지 않는다. **유일한 예외**: Excel 왕복 편집에서 시트를 지워 source 가
   줄면 `sources` 목록을 축소해 재저장한다(안 하면 idx↔parquet 대응이 어긋남 —
   [docs/11](docs/11_web_report_tabs.md)). 캐시 키는 항상
   [cache_policy.py](web_report/cache_policy.py) 빌더로 만든다(즉석 조립 금지 —
   [docs/12](docs/12_web_report_cache.md)). raw parquet 을 바꾸는 편집은 `content_hash` 를
   **같은 analysis_key 의 전 세션**에 반영한다(dedup 형제의 stale 캐시 방지).
7. **소유권 / 수정 권한 경계.** 정본 [docs/15_ownership.md](docs/15_ownership.md). 요약:
   🟢 자유 수정=`web_report/`+`server/`(`storage_gateway/` 제외)+web_report html
   +client 자주 쓰는 영역(`honey_ui/`·`honey_main.py`·`transport/`·`excel_download/`·`excel_edit/`) /
   🟡 사전 승인=`client/` 나머지 비동결(편집 전 파일·이유·영향 설명) /
   🔒 외부 담당자 영역(건들 때마다 승인)=`d1/`·`d1_storage/`·`client/honey_parse/`·
   `client/report_generator/`·`server/storage_gateway/`(facade+`_s3` 전체). **경계가 폴더 내부를
   가르는 곳**: server/ 는 storage_gateway 만 외부 영역, client/ 는 report_generator·honey_parse 만
   외부 영역. `eval_analyzer/` 는 2026-08-03 자유 수정으로 승격됐다(동결 아님).
8. **eval_analyzer 단방향 의존.** `eval_analyzer/` 는 **이 repo 가 원본**이다
   (2026-08-03 — 외부 사본 `F:\COINAPI\eval_analyzer` 는 더 이상 참조·동기화 대상이 아니다).
   하위 파일은 자유 수정이지만 **의존 방향은 계속 단방향**이다 — eval_engine import 는
   [web_report/ai_comment.py](web_report/ai_comment.py)(evaluate 호출) +
   [web_report/eval_export.py](web_report/eval_export.py)(store·ingest 헬퍼 — 코멘트 export) +
   [web_report/eval_debug.py](web_report/eval_debug.py)(룰 리로드·L0~L6 트레이스 — `/pe/eval`)
   **3곳만** 허용(양방향 그 외 import 금지). 서버의 evaluate 호출은 persist=False(운영
   eval.db 무기록) — 코멘트 export 는 report_server 소유 별도 파일 `REPORT_EVAL_DB_PATH`
   에만 쓴다. 규약 전문 [docs/13](docs/13_eval_analyzer_integration.md).
   - **eval_DB 스키마 변경은 사전 확인 대상**이다. `eval_engine/store.py` 의 DDL·컬럼을
     바꿔야 하는 상황이면 바로 고치지 말고 **어떤 테이블·컬럼을 어떻게 바꾸는지와 영향을
     설명한 뒤 사용자 승인을 받고** 진행한다(운영 eval.db 에 누적 데이터가 있다).
     스키마와 무관한 나머지 수정은 자유.
   - 서버가 `eval_analyzer/db_input/import_csv.py` 를 **subprocess 로 실행**하는 것은
     import 가 아니므로 2곳 규약 위반이 아니다 (Honey 'DB Input' —
     [server/report/routes_eval_input.py](server/report/routes_eval_input.py),
     [docs/13 §10](docs/13_eval_analyzer_integration.md)). 별도 프로세스인 이유는
     `import_csv._import_group` 이 `eval_engine.config.DB_PATH` 를 모듈 전역에 대입하기
     때문 — 장수명 Flask 프로세스를 오염시키지 않으려면 프로세스 경계가 필요하다.
9. **입력 계약은 7-meta honeyform 이다 (2026-07-21 확정).** `honey_parse.file_to_df` 반환 df =
   `SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO` + `TSEQ~LOLIM` 6행, 반환 df 개수 = source 개수
   (병합은 honey_parse 내부에서). **이 산출물(`md.df`)이 곧 web_report parquet 소스**이며,
   원본 입력 파일을 다시 읽어 검증하지 말 것 — 병합 결과를 버리게 된다.
   - 구 5-meta df_honey(`DUT,XCoord,YCoord,Bin,Serial`)는 **폐기된 계약**이다.
   - ⚠️ **알려진 격차**: `client/report_generator/`(constants.py `DATA_START_ROW=5` 등)는 아직
     5-meta 를 가정한다 → 7-meta 프레임에서 `BIN`·`FAILTNO` 를 측정 항목으로 오인한다.
     외부 담당자 소유라 **이 저장소에서 고치지 않는다**(최신 사본 수령으로 해소).
     `client/honey_parse/` 더미 폴백도 5-meta 라 **개발 PC 로컬 Web Report 업로드는 실패가
     정상**이다. 상세 [docs/06](docs/06_analysis_engine.md).

---

## 6. 코드 포인터

| 알고 싶은 것 | 어디? |
|--------------|-------|
| 소유권/수정 권한 경계 (정본) | [docs/15_ownership.md](docs/15_ownership.md) (자유/사전승인/외부 담당자 영역) |
| API 엔드포인트·환경변수 (정본) | [server/README.md](server/README.md) |
| xlsx 업로드 라우트 | [server/upload_xlsx.py](server/upload_xlsx.py) · grid 파싱 [xlsx_parser.py](server/xlsx_parser.py) |
| web_report 업로드/파이프라인 | [server/upload_webreport.py](server/upload_webreport.py) → [docs/10](docs/10_web_report_pipeline.md) |
| 라우트 (세션/web_report/기타) | [routes_session.py](server/report/routes_session.py) / [routes_webreport.py](server/report/routes_webreport.py) / [routes_misc.py](server/report/routes_misc.py) |
| 접근제어·CSRF·가드 | [server/report/security.py](server/report/security.py) → [docs/02](docs/02_server_query_edit.md) |
| 신원/인증 (SSO 전환) | [server/auth_identity.py](server/auth_identity.py) |
| DB 스키마 (정본) | [server/database/core.py](server/database/core.py) `SCHEMA` (report_db.py 는 재노출 facade) |
| web_report 편집 상태 | [web_report/edits.py](web_report/edits.py) + [server/database/webreport_edits.py](server/database/webreport_edits.py) |
| web_report 캐시 키 규약 | [web_report/cache_policy.py](web_report/cache_policy.py) → [docs/12](docs/12_web_report_cache.md) |
| Distribution 정렬 전가(pack) — 서버 최대 병목 제거 | [web_report/dist_pack.py](web_report/dist_pack.py) + [dist_pack_store.py](web_report/dist_pack_store.py) → [docs/12](docs/12_web_report_cache.md) |
| 새 탭 추가 (레지스트리) | [web_report/tabs/__init__.py](web_report/tabs/__init__.py) `TAB_REGISTRY` → [docs/11](docs/11_web_report_tabs.md) |
| S3 저장 진입점(facade) | [server/storage_gateway/](server/storage_gateway/__init__.py) ([README](server/storage_gateway/README.md), 키빌더 _s3.py) |
| 검색결과 UI / 세션 상세 UI | [report_analysis_index.html](server/report/report_analysis_index.html) / [report_view.html](server/report/report_view.html) + [static/webreport/](server/report/static/webreport/) (15모듈) |
| 관리 대시보드 (/pe/admin-pte/) | [server/admin_panel/](server/admin_panel/) (구 admin_routes.py 는 미등록 dead file) |
| eval 룰 관리 (/pe/eval) — threshold/signature 편집·트레이스 | [server/eval_panel/](server/eval_panel/) + [web_report/eval_debug.py](web_report/eval_debug.py) → [docs/13 §11](docs/13_eval_analyzer_integration.md) |
| 감사 기록 헬퍼 | [server/database/report_db.py](server/database/report_db.py) `log_audit` / `get_audit_logs` |
| Honey 클라 (자유: honey_ui/honey_main/transport/excel_*) | [client/honey_main.py](client/honey_main.py), 업로드 [transport/uploader.py](client/transport/uploader.py), 추출 [report_flow/upload_prepare.py](client/report_flow/upload_prepare.py) |
| 외부 담당자 영역 동결 (무수정) | `d1/` · `client/report_generator/` · `client/honey_parse/` · `server/storage_gateway/` → [docs/15](docs/15_ownership.md) · 진입점 [INDEX §3.1](docs/INDEX.md) |
| eval_analyzer 연결 (AI Comment / 코멘트 export) | [web_report/ai_comment.py](web_report/ai_comment.py) + [web_report/eval_export.py](web_report/eval_export.py) — eval_engine import 2곳 → [docs/13](docs/13_eval_analyzer_integration.md) |
| 기준정보(part_ids) 갱신 — DRM CSV → product_info.db | [tools/product_info_import/](tools/product_info_import/README.md) (Excel PC) → [server/product_info.py](server/product_info.py) 가 읽기전용 로드 |
| eval 룰 골든셋 회귀 (임계값 튜닝 전후 비교) | [tools/eval_golden/golden_check.py](tools/eval_golden/golden_check.py) → [docs/13 §12](docs/13_eval_analyzer_integration.md) |
| ENGR 이력 검색 챗봇 (자연어 → 조회 툴) | [server/chatbot/](server/chatbot/README.md) — 골든셋 [tests/chatbot_golden.yaml](tests/chatbot_golden.yaml), 백필 [tools/eval_backfill/](tools/eval_backfill/backfill_eval_db.py) |
| 더미 grids 픽스처 생성기 | [tests/sample_xlsx.py](tests/sample_xlsx.py) |

---

## 7. Verification

E2E 동작 확인 순서는 [README.md](README.md) 의 "검증 절차" 참조.

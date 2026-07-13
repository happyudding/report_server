# COINAPI report_server — 외부 담당자 영역 진입점

> 이 문서는 **외부 담당자 영역**(D1 입력 · 서버 저장소/S3 · 리포트 생성 · 입력 파서)
> 소유자를 위한 진입점이다. 이 프로젝트(웹리포트/서버) 소유자용 진입 문서는
> [CLAUDE.md](CLAUDE.md). **소유권·수정 권한 경계의 정본은 [docs/15_ownership.md](docs/15_ownership.md)**
> 하나이며, 이 문서는 그 거울상 요약이다.

이 repo 에는 소유·수정 권한이 다른 두 영역이 병합돼 있다. 아래 🟢 폴더는 당신(외부 담당자)이
소유·교체하는 영역이고, 나머지는 이 프로젝트 소유라 **건들 때마다 승인**을 받는다.

---

## 1. 수정 권한 경계 (외부 담당자 관점)

| 티어 | 정책 | 영역 |
|------|------|------|
| 🟢 **자유 수정 (당신 소유)** | 승인 없이 바로 수정 | `d1/` · `d1_storage/` · `client/report_generator/` · `client/honey_parse/` · `server/storage_gateway/`(facade `__init__.py` + `_s3` 내부 전체) |
| 🔒 **이 프로젝트 소유** | 건들 때마다 승인 (원칙 무수정) | `server/`(단 `storage_gateway/` **제외**) · `web_report/` · web_report 관련 html(`server/report/report_view.html`, `server/report/static/webreport/`) · client 자주 쓰는 영역: `client/honey_ui/` · `client/honey_main.py` · `client/transport/` · `client/excel_download/` · `client/excel_edit/` |
| 🟡 **사전 승인** | 편집 전 파일·이유·영향 설명 | client 나머지 비동결(`report_flow/`, `map_report/`, `embedded_browser.py`, `client_identity.py`, `config.py` 등) |

**폴더 내부에서 경계가 갈리는 곳(가장 헷갈리는 지점)**:
- **`server/` 안**: `server/storage_gateway/` **만** 당신 소유다. 나머지 `server/`(report·database·
  admin_panel 등)는 이 프로젝트 소유 → 건들 때 승인.
- **`client/` 안**: `client/report_generator/`·`client/honey_parse/` **만** 당신 소유다. 나머지
  client(honey_ui·honey_main·transport·excel_* 등)는 이 프로젝트 소유.

---

## 2. 진입점 · 유지 계약 (당신 폴더가 지켜야 할 계약)

당신 폴더를 폴더째 교체해도 이 프로젝트가 무수정으로 동작하도록, 아래 진입점·계약을 유지한다
(정본 상세는 [docs/INDEX.md §3.1](docs/INDEX.md), 병합 순서는 [docs/14](docs/14_merge_order.md)).

| 경계 | 진입점 | 유지 계약 |
|------|--------|-----------|
| D1 입력 (검증용) | `d1/` `get_provider`/`list_files`/`D1BrowserDialog` | Honey UI 는 provider 결과 경로 목록만 사용 |
| 서버 저장소/S3 (검증용) | `server/storage_gateway/` ([README](server/storage_gateway/README.md)) | `/pe/report/...` URL · multipart · 응답 JSON · 저장 위치 기록 계약. 이 프로젝트가 추가한 보존 함수(`save_webreport_sources`/`save_webreport_manifest`)와 S3 prefix env 유지 |
| 리포트 생성 (무수정) | `client/report_generator/` ([README](client/report_generator/README.md)) | 분석 수식 · xlsx 레이아웃 · DB 스키마 불변 |
| 입력 파서 (무수정) | `client/honey_parse/` `file_to_df` | `(df, df_yield)` 반환 계약 |

---

## 3. 참고

- `eval_analyzer/` 는 **제3의 외부 프로젝트**(fail-item 평가 엔진)의 운영 복사본으로, 이 프로젝트가
  단방향으로만 참조한다(import 는 `web_report/ai_comment.py` 1곳). 당신 영역과는 무관하며 여기서
  수정 대상이 아니다.
- 경계·계약이 헷갈리면 항상 [docs/15_ownership.md](docs/15_ownership.md)(정본) 하나로 답한다.

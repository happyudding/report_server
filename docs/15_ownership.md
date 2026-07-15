# 15. 소유권 / 수정 권한 경계 (정본)

> **이 문서가 소유권·수정 권한 경계의 단일 진실(정본)이다.** CLAUDE.md·CLAUDE.core.md·
> INDEX·각 README 는 여기를 링크로 참조한다. "이 파일 고쳐도 돼?" 는 이 문서 하나로 답한다.

이 repo 에는 소유·수정 권한이 다른 두 영역이 병합돼 있다. **이 프로젝트**(웹리포트/서버 —
활발히 개발)와 **외부 담당자 영역**(병합돼 들어왔으나 외부가 소유·교체하는 폴더)이다.
진입 문서는 관점별로 둘이다: 이 프로젝트는 [CLAUDE.md](../CLAUDE.md), 외부 담당자는
[CLAUDE.core.md](../CLAUDE.core.md). 두 문서 모두 경계는 여기(정본)를 따른다.

---

## 1. 티어 표

| 티어 | 정책 | 영역 |
|------|------|------|
| 🟢 **자유 수정** | 승인 없이 바로 수정 | `web_report/` · `server/`(단 `storage_gateway/` **제외**) · web_report 관련 html(`server/report/report_view.html`, `server/report/static/webreport/`) · client 자주 쓰는 영역: `client/honey_ui/`, `client/honey_main.py`, `client/transport/`, `client/excel_download/`, `client/excel_edit/` |
| 🟡 **사전 승인 (중간)** | 편집 전 **파일·이유·영향**을 설명하고 명시 승인 | `client/` 나머지 비동결 — `report_flow/`, `map_report/`, `embedded_browser.py`, `client_identity.py`, `config.py`, 그 외 client 최상위 파일(`app_settings.py`, `chart_colors.py` 등) |
| 🔒 **외부 담당자 영역 (동결)** | 건들 때마다 승인 (원칙 무수정) | `d1/` · `d1_storage/` · `client/honey_parse/` · `client/report_generator/` · `server/storage_gateway/`(**facade `__init__.py` + `_s3` 내부 전체**) |
| 🔒 외부 단방향 (동결) | 하위 무수정, import 는 2곳만 | `eval_analyzer/` — eval_engine import 는 [web_report/ai_comment.py](../web_report/ai_comment.py)(evaluate) + [web_report/eval_export.py](../web_report/eval_export.py)(store·ingest 헬퍼) **2곳만**([docs/13](13_eval_analyzer_integration.md), CLAUDE.md 규칙 #8) |

---

## 2. 폴더 내부에서 경계가 갈리는 곳 (가장 헷갈리는 지점)

경계가 폴더 하나를 통째로 가르지 않고 **내부를 쪼개는** 두 곳이 있다. 여기만 조심하면 된다.

- **`server/` 안**: 대부분 자유 수정이지만 **`server/storage_gateway/` 만 외부 담당자 영역**이다
  (facade `__init__.py`·README 포함 전체). "server 는 작업하지만 s3 는 안 건드림" 이 여기서
  성립한다. 저장소 접근이 필요하면 이미 있는 공개 함수만 호출하고 내부를 고치지 않는다.
- **`client/` 안**: `report_generator/`·`honey_parse/` 는 **외부 담당자 영역**, 자주 쓰는
  `honey_ui/`·`honey_main.py`·`transport/`·`excel_download/`·`excel_edit/` 는 **자유 수정**,
  나머지(`map_report/`·`report_flow/` 등)는 **사전 승인**이다. client 는 통째로 한 티어가 아니다.

---

## 3. 왜 이렇게 나누나 (rationale)

- **외부 담당자 영역(동결)** 은 외부 담당자가 소유하고 통째로 교체될 수 있는 코드다
  (`report_generator`·`storage_gateway` 는 폴더째 교체 대비 → [docs/14](14_merge_order.md)).
  여기를 이 repo 에서 고치면 교체·재병합 시 **충돌·유실**된다. 그래서 동결한다.
- **자유 수정** 인 `web_report/`·`server/` 는 이 repo 가 진짜 소유주다. 마찰 없이 바로
  고쳐서 반영한다. client 중 자주 손대는 셸(honey_ui·honey_main·transport·excel_*)도 여기 둔다.
- **사전 승인** 인 나머지 Honey 클라 영역은 이 repo 소유지만 데스크톱/COM 의존이 많고
  지금 주 개발 대상이 아니라, 편집 전 영향 확인을 거친다.

---

## 4. 용어 매핑 (옛 문서 호환)

경계를 *정의*하는 진입 문서(CLAUDE.md·CLAUDE.core.md·INDEX·이 문서)·배너 README 는 위
중립 용어로 통일한다. 깊은 기능 문서(03·05·06·07·11·13 등)의 옛 문구는 **재작성하지 않는다**
— 아래 매핑으로 의미가 그대로 유효하다.

| 옛 표현 | 새 표현(이 문서 기준) |
|---------|----------------------|
| "신서버" / "자유 수정" / "외부 프로젝트 아님" | 🟢 **자유 수정** (이 프로젝트 소유) |
| "구서버" / "동결" / "외부 프로젝트" / "무수정 원칙" / "(외부)" 마커 | 🔒 **외부 담당자 영역** |
| "사전 승인 필요" | 🟡 **사전 승인** (그대로) |

> "신서버/구서버" 라벨은 폐기됐다. 신규 문서에는 쓰지 않는다 — 위 중립 용어를 쓴다.

---

## 5. (선택) 문서 대신 강제로 막고 싶다면

이 문서는 **정책**을 명문화한다. 진짜로 "자유 영역은 무마찰, 외부 담당자 영역은 편집 차단"을
도구로 강제하려면 `.claude/settings.json` 의 권한 규칙으로 가능하다 — 자유 영역 glob 은
Edit/Write 허용, 외부 담당자 glob(`server/storage_gateway/**`, `client/report_generator/**`,
`client/honey_parse/**`, `d1/**`, `d1_storage/**`, `eval_analyzer/**`)은 deny/ask. 이번 문서
정리 범위 밖이며, 원하면 별도로 설정한다.

"""web_report 공개 API 규약 정본 — 함수 1개 = REST 경로 1개 = MCP tool 1개.

이 파일이 **기계가독 단일 진실**이다. 세 소비자가 전부 여기서 파생된다:
  1. routes.py 의 `GET /capabilities` (외부 시스템 discovery)
  2. server/web_report_mcp/server.py 의 MCP tool 스키마
  3. 관리자 패널 'public API' 탭의 규약 표

규약을 이중 정의하지 말 것 — 새 엔드포인트를 만들면 라우트와 여기 SPEC 을 **같은
커밋에서** 추가한다. `tests/test_public_api_web_report.py` 가 등록 라우트와 SPEC 목록의
1:1 일치를 검사하므로, 한쪽만 고치면 테스트가 깨진다.

## 공통 규약

- 메서드는 전부 GET, 읽기 전용. 요청 본문 없음.
- 성공 응답 envelope: `{"schema_version": 1, "data": ..., "meta": {...}}`
  meta = session_id / content_hash / edits_rev / generated_at (+ 페이지 정보).
  외부 시스템이 "이 값이 언제 것인지" 를 판단하는 근거다.
- 에러 분기(HTTP 상태 ↔ 본문 키):
    200 정상
    202 {"building": true, "blocked": false, "status_url", "retry_after_sec"} — 콜드 빌드 중
    400 {"error": "bad_request"|"not_web_report", "message"}
    404 {"error": "session_not_found"}   ← 권한 없음도 **같은 응답**(존재 은닉)
    429 {"error": "busy"}                ← 대용량 엔드포인트만(동시 실행 상한)
    503 {"error": "build_failed"}        ← 연속 실패로 차단된 세션
- 인증: 기본 무인증(공개 세션만). env `WEB_REPORT_API_KEY` 가 설정돼 있고 요청 헤더
  `X-Report-Api-Key` 가 일치하면 비공개 세션까지 조회한다(불일치는 차단이 아니라
  공개 세션만 — public_api 의 무인증 성격을 유지).

## 비용 등급(cost)

  "cheap"  DB/메모리 조회 — 폴링해도 무방
  "warm"   빌드된 payload 슬라이스 — 콜드면 202
  "heavy"  대용량 gzip 전량 — 동시 실행 상한(429) 대상, MCP tool 부적합
           (MCP 에는 값 대신 `full_data_url` 포인터를 준다)
"""
from __future__ import annotations

# 응답 envelope 계약 버전. 필드는 **추가만** 하고 삭제·개명하지 않는다(public_api README
# 버저닝 약속). 깨는 변경이 필요하면 /v2 를 새로 만든다.
SCHEMA_VERSION = 1

# 세션 id 경로 파라미터 공통 정의 — 반복을 줄이되 스키마는 각 SPEC 에 펼쳐 넣는다.
_SID = {"type": "string", "maxLength": 80, "required": True,
        "description": "세션 ID (/sessions 가 돌려준 session_id)"}


def _spec(name, path, summary, params, returns, cost="warm", notes=""):
    return {"name": name, "path": path, "summary": summary,
            "params": params, "returns": returns, "cost": cost, "notes": notes}


# ── 함수 규약 ────────────────────────────────────────────────────────────────
# 순서 = /capabilities 표시 순서 = 문서 순서.
FUNCTION_SPECS = [
    _spec(
        "list_sessions", "/sessions",
        "평가 세션을 조건으로 검색한다. 다른 모든 함수가 필요로 하는 session_id 를 여기서 얻는다.",
        {
            "product": {"type": "string", "description": "제품명 정확일치"},
            "product_type": {"type": "string",
                             "enum": ["MDDI", "PDDI", "PMIC", "SECURITY", "TCON"]},
            "lot_id": {"type": "string"},
            "q": {"type": "string", "description": "자유 검색어(제품/lot/파일명/업로더 부분일치)"},
            "date_from": {"type": "integer", "description": "epoch 초"},
            "date_to": {"type": "integer", "description": "epoch 초"},
            "sort": {"type": "string", "enum": ["new", "old", "product", "lot"],
                     "default": "new"},
            "limit": {"type": "integer", "default": 20, "maximum": 100},
            "offset": {"type": "integer", "default": 0},
        },
        "{sessions: [...], total, returned}",
        cost="cheap"),

    _spec(
        "get_overview", "/{session_id}/overview",
        "세션 개요 — 분석 모드·source 목록·전체/소스별 수율·수율 분모 기준·"
        "ENGR 요약(사람이 쓴 결론 3칸). 세션에 대해 가장 먼저 부르는 함수.",
        {"session_id": _SID},
        "{session, mode, sources, yield_summary(+by_step), yield_basis, summary_engr, "
        "has_temperature, has_compare, pending}"),

    _spec(
        "get_build_status", "/{session_id}/build-status",
        "리포트 계산 상태. 202 를 받았을 때 이 경로로 폴링한 뒤 재요청한다.",
        {"session_id": _SID},
        "{state: 'building'|'idle', stage, elapsed, eta, blocked}",
        cost="cheap"),

    _spec(
        "get_yield", "/{session_id}/yield",
        "Yield 표 전체 — bin/item 별 fail 행 + STEP(P1/P2/P3) 분해 + 분모 기준.",
        {"session_id": _SID,
         "limit": {"type": "integer", "default": 200, "maximum": 2000,
                   "description": "행 상한"}},
        "{session, yield_summary, rows[], step_groups, bin_groups, yield_basis, truncated}"),

    _spec(
        "get_fail_bins", "/{session_id}/fail-bins",
        "Fail Bin 랭킹 — 어떤 bin 이 가장 많이 떨어졌나.",
        {"session_id": _SID,
         "limit": {"type": "integer", "default": 20, "maximum": 200}},
        "{session, fail_bins[], bin_summary}"),

    _spec(
        "get_cpk", "/{session_id}/cpk",
        "CPK 전표 — 항목×source 별 cpk/평균/limit. item 미지정 시 나쁜 순.",
        {"session_id": _SID,
         "item": {"type": "string", "description": "항목명 부분일치 필터"},
         "source": {"type": "string", "description": "source 명 정확일치 필터"},
         "worst_n": {"type": "integer", "default": 50, "maximum": 200},
         "offset": {"type": "integer", "default": 0}},
        "{session, cpk_rows[], cpk_worst, total, returned}"),

    _spec(
        "get_issue_table", "/{session_id}/issue-table",
        "Issue Table 계산본 — 자동 생성된 이슈 행 전체 + PTE/개발 comment + AI Comment "
        "+ Signature + Status. 편집이 없는 행도 포함한다(편집 DB 만 읽는 챗봇과 다른 점).",
        {"session_id": _SID,
         "table": {"type": "string", "enum": ["main", "temp", "compare"], "default": "main"},
         "item": {"type": "string", "description": "항목/row_key 부분일치 필터"},
         "include_hidden": {"type": "boolean", "default": False,
                            "description": "사용자가 숨긴 행 포함 여부"}},
        "{session, table, rows[](row_key 포함), total, returned}",
        notes="comment 는 화면 전용 서식 토큰(*[..]/*r[..])을 벗겨 평문으로 준다."),

    _spec(
        "list_items", "/{session_id}/items",
        "측정 항목 카탈로그 — 항목명·표본수·limit·cpk. 항목명을 확정할 때 쓴다.",
        {"session_id": _SID,
         "keyword": {"type": "string"},
         "limit": {"type": "integer", "default": 100, "maximum": 500},
         "offset": {"type": "integer", "default": 0}},
        "{session, items[], total, returned}"),

    _spec(
        "get_item_stats", "/{session_id}/items/{subject}/stats",
        "항목 1개의 source 별 기초 통계(count/mean/std/min/max) + cpk + limit. "
        "측정값 배열은 빼고 통계만 준다 — 전량은 get_item_values.",
        {"session_id": _SID,
         "subject": {"type": "string", "maxLength": 200, "required": True,
                     "description": "항목명(list_items 가 돌려준 값)"}},
        "{session, subject, units, lower_limit, upper_limit, cpk, status, fail_total, "
        "stats[], sources[{source, count, min, max}]}"),

    # ── P2: source 비교 · 온도 · 입력정보 ────────────────────────────────────
    _spec(
        "get_compare", "/{session_id}/compare",
        "Compare 모드 세션의 source 간 비교 결과(분포 shift·동등성·bin 증감 등).",
        {"session_id": _SID,
         "section": {"type": "string",
                     "enum": ["summary", "dist_shift", "equivalence", "bin_delta",
                              "bin_matrix", "goodlog", "new_items"],
                     "default": "summary"},
         "limit": {"type": "integer", "default": 100, "maximum": 1000}},
        "{session, section, data, groups, before_sources, after_sources}",
        notes="Compare 모드가 아니면 400. 계산 대기 중이면 202(kind=compare)."),

    _spec(
        "compare_sessions", "/compare-sessions",
        "세션 여러 건의 수율·CPK 를 나란히 놓는다(추이·lot 간 비교). "
        "새로 계산하지 않고 각 세션의 기존 값을 나열만 한다.",
        {"sids": {"type": "string", "required": True,
                  "description": "콤마 구분 session_id, 최대 5개"},
         "items": {"type": "string", "description": "콤마 구분 항목명 — 지정 시 그 항목 cpk 비교"}},
        "{sessions[{session, yield_summary, cpk_worst, items{}}], missing[], building[]}"),

    _spec(
        "get_temperature", "/{session_id}/temperature",
        "Temperature 세션의 RT/CT/HT 그룹 구성 + 온도별 재판정 이슈 행.",
        {"session_id": _SID,
         "limit": {"type": "integer", "default": 500, "maximum": 2000}},
        "{session, groups, rows[], total, returned}",
        notes="Temperature 모드가 아니면 groups=null, rows=[]."),

    _spec(
        "get_input_info", "/{session_id}/input-info",
        "source 별 입력 파일 정보(파일명·크기·시각·STDF 메타). manifest 만 읽어 값이 싸다.",
        {"session_id": _SID},
        "{session, mode, sources[], has_file_info, has_stdf}",
        cost="cheap"),

    _spec(
        "get_map_summary", "/{session_id}/map",
        "Map Analysis 경량 메타(맵 목록·source·bin 집계). die 좌표 전량은 map_dies.",
        {"session_id": _SID},
        "{session, maps[]}"),

    _spec(
        "get_raw_data_columns", "/{session_id}/raw-data/columns",
        "Raw Data 컬럼 메타 + source 목록 + 전체 die 수. raw_data 조회 전에 부른다.",
        {"session_id": _SID},
        "{session, columns[], sources[], total_dies}"),

    _spec(
        "get_raw_data", "/{session_id}/raw-data",
        "Raw Data 실제 저장값 페이지 조회.",
        {"session_id": _SID,
         "columns": {"type": "string",
                     "description": "콤마 구분 컬럼명, 최대 60개(미지정 시 서버 기본)"},
         "search": {"type": "string"},
         "bin": {"type": "string"},
         "source": {"type": "string"},
         "limit": {"type": "integer", "default": 200, "maximum": 2000},
         "offset": {"type": "integer", "default": 0}},
        "{session, columns[], rows[], total_matched, next_offset, truncated}",
        cost="heavy"),

    # ── 대용량(heavy) — MCP 에는 full_data_url 포인터만 ──────────────────────
    _spec(
        "get_item_values", "/{session_id}/items/{subject}/values",
        "항목 1개의 측정값 **전량** + 좌표(gzip). 다운샘플하지 않는다.",
        {"session_id": _SID,
         "subject": {"type": "string", "maxLength": 200, "required": True},
         "bin1": {"type": "boolean", "default": False, "description": "Bin1(양품)만"}},
        "{sources[{values, serial, xpos, ypos}], stats, cpk, status, fail_rows, fail_total}",
        cost="heavy"),

    _spec(
        "get_distribution", "/{session_id}/distribution",
        "전 항목 ECDF 분포 데이터 **전량**(gzip). 규칙상 다운샘플 금지.",
        {"session_id": _SID,
         "bin1": {"type": "boolean", "default": False}},
        "{format: 'ecdf-columnar-v1', items[]}",
        cost="heavy"),

    _spec(
        "get_map_dies", "/{session_id}/map/dies",
        "Map die 좌표·bin **전량**(gzip).",
        {"session_id": _SID},
        "{format: 'map-dies-v1', maps[]}",
        cost="heavy"),

    _spec(
        "get_full_payload", "/{session_id}/full",
        "세션 report payload 전체(gzip) — 웹 화면이 받는 것과 동일한 bytes.",
        {"session_id": _SID},
        "웹 /full 과 동일한 payload 12키 + sheets",
        cost="heavy",
        notes="개별 함수로 충분하면 이것을 쓰지 말 것 — 응답이 가장 크다."),

    _spec(
        "download_raw_csv", "/{session_id}/raw-data/sources/{index}.csv",
        "source 1개의 7-meta raw data CSV 다운로드(스트리밍).",
        {"session_id": _SID,
         "index": {"type": "integer", "required": True, "description": "source 인덱스(0-based)"}},
        "text/csv 스트림",
        cost="heavy"),

    _spec(
        "download_raw_zip", "/{session_id}/raw-data/all.zip",
        "전 source raw data CSV zip 다운로드(스트리밍).",
        {"session_id": _SID},
        "application/zip 스트림",
        cost="heavy"),
]

# 이름 → SPEC (라우트·MCP 가 조회용으로 쓴다)
SPECS_BY_NAME = {s["name"]: s for s in FUNCTION_SPECS}


def capabilities():
    """`GET /capabilities` 응답 본문. 외부 시스템·MCP 가 이걸 읽고 호출을 조립한다."""
    return {
        "schema_version": SCHEMA_VERSION,
        "base_path": "/pe/api/v1/web-report",
        "auth": {
            "mode": "optional_api_key",
            "header": "X-Report-Api-Key",
            "note": "미제시·불일치 시 공개 세션만 조회된다(차단이 아니다).",
        },
        "errors": {
            "202": "building — status_url 폴링 후 재시도",
            "400": "bad_request / not_web_report",
            "404": "session_not_found (권한 없음 포함 — 존재를 숨긴다)",
            "429": "busy — 대용량 엔드포인트 동시 실행 상한",
            "503": "build_failed — 연속 실패로 차단된 세션",
        },
        "count": len(FUNCTION_SPECS),
        "functions": FUNCTION_SPECS,
    }

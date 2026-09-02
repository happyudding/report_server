"""web_report 성능 회귀 가드 — 알려진 지뢰를 다시 밟는 변경을 쓰기 전에 차단한다.

왜 있는가
---------
web_report 속도 개선 작업에서 과거에 실제로 회귀가 났던 지점들이 있다. 규칙 자체는
이미 문서(``CLAUDE.md`` §5, ``docs/11``, ``docs/12``, ``cache_policy.py`` 주석)에
적혀 있지만, **변경 시점에 자동으로 확인되지 않아** 놓치면 그대로 통과했다.
이 파일은 그 규칙들을 실행 가능한 형태로 옮긴 것이고, Claude Code 훅
(``.claude/settings.json``)이 매 Edit/Write 마다 호출한다.

**규칙의 정본은 문서가 아니라 아래 ``_RULES`` 다.** 문서에 규칙 본문을 복제하면
드리프트가 생기므로, 각 규칙은 근거 문서를 ``doc`` 으로 가리키기만 한다.
회귀가 한 번 나면 규칙을 1개 추가하는 것이 표준 사후 조치다 → docs/18_perf_guard.md

실행 모드
---------
    --hook       stdin 으로 PreToolUse JSON 을 받아 쓰기 전에 판정 (stdout JSON)
    --stop       Stop 훅 — 작업트리 diff 전체 검사 (stdout JSON, 동일 위반 1회만 차단)
    --diff       작업트리 diff 검사, 사람이 읽는 출력 + 위반 시 exit 1
                 (--ref <REF> 로 비교 기준 변경, 기본 HEAD)
    --scan-all   범위 내 전 파일을 '전부 추가된 것'으로 보고 검사 — 오탐 점검용
    --selftest   규칙마다 위반/정상 샘플로 자기 검증

fail-open: --hook/--stop 은 어떤 예외가 나도 조용히 통과시킨다. 가드가 깨졌을 때
작업 전체를 막는 것보다 낫다. 대신 --selftest 가 회귀를 잡는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# 한국어 사유를 그대로 출력하므로 UTF-8 고정이 필수다. 한국어 Windows 기본
# cp949 로 쓰면 UnicodeEncodeError 로 훅이 죽고, 죽은 훅은 조용히 통과한다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

# 검사 범위 — 실제 성능 회귀가 나온 영역만. 밖의 파일은 즉시 통과한다.
SCOPE = ("web_report/", "server/report/")

# 조회/빌드 속도에 직접 영향을 주는 파일들. 여기가 바뀐 채로 턴이 끝나면 --stop 이
# "벤치 돌릴지 사용자에게 물어라"를 돌려준다. **성능 목적 변경이었는지는 파일만 보고
# 알 수 없으므로** 판단은 모델에게 맡긴다(라벨 수정 같은 건 묻지 말라고 안내한다).
# 편집·eval·검증 전용 모듈은 조회 경로가 아니라서 제외했다.
PERF_SENSITIVE = (
    "web_report/cache.py", "web_report/cache_policy.py", "web_report/disk_cache.py",
    "web_report/response_cache.py", "web_report/compute.py", "web_report/service.py",
    "web_report/loader.py", "web_report/metrics.py", "web_report/honeyform.py",
    "web_report/ingest.py", "web_report/preprocess.py",
    "web_report/dist_pack.py", "web_report/dist_pack_store.py",
    "web_report/dist_blob.py", "web_report/ai_comment.py", "web_report/tabs/**/*.py",
    "server/report/routes_session.py", "server/report/routes_webreport.py",
    "server/report/static/webreport/*.js",
)

BENCH_CMD = r"server\.venv\Scripts\python.exe tests\bench_webreport.py --quick"

# 면제 주석: 위반 줄 또는 바로 윗줄에 있으면 그 규칙을 건너뛴다.
EXEMPT_RE = re.compile(r"perf-guard:\s*allow\s+([A-Za-z0-9_\-]+)")

# 순수 주석 줄은 검사하지 않는다 — distribution.js 주석에만 DOWNSAMPLE 이 열두 번
# 나오는 식이라, 주석까지 보면 도입 즉시 오탐이 터진다.
COMMENT_RE = re.compile(r"^\s*(#|//|\*|/\*)")


# --------------------------------------------------------------------------
# 규칙 정본
#
#   id       위반 메시지·면제 주석에 쓰는 식별자
#   kind     forbid_add | forbid_remove | require_pair | require_import
#   severity block(차단) | warn(보고만)      기본 block
#   paths    저장소 상대경로 glob (** 지원)
#   pattern  탐지 정규식
#   unless   같은 조각에 이게 있으면 위반 아님 (선택)
#   multi    True 면 줄 단위가 아니라 조각 전체에 대해 매치 (선택)
#   why      왜 위험한가 (사용자·모델에게 그대로 보여준다)
#   doc      근거 문서
# --------------------------------------------------------------------------
_RULES = [
    {
        "id": "R01-dist-downsample",
        "kind": "forbid_add",
        "paths": ["web_report/**/*.py"],
        "pattern": r"_MAX_CDF_POINTS|\b_downsample\s*\(|\bmax_points\s*=",
        "why": "Distribution 차트 데이터 다운샘플링은 절대 금지다. 모든 데이터 포인트를 "
               "빠짐없이 표현해야 한다 — 포인트 상한 로직을 서버에 추가하지 말 것. "
               "(미니셀 썸네일 표시용 캡은 프런트 distribution.js 의 DIST.DOWNSAMPLE 뿐)",
        "doc": "CLAUDE.md §5-5",
    },
    {
        "id": "R02-ecdf-line",
        "kind": "forbid_add",
        "paths": ["server/report/static/webreport/distribution.js"],
        "pattern": r"shape\s*:\s*['\"]hv",
        "why": "미니셀 ECDF 는 markers(점)만으로 렌더한다. Plotly line.shape:'hv' 계단형 "
               "수평선은 x축 방향으로 누적분포를 왜곡한다. 점이 성겨 보이는 문제는 세로 "
               "방향 채움(distFillVertical)으로만 해결하고 선으로 잇지 않는다.",
        "doc": "CLAUDE.md §5-5",
    },
    {
        "id": "R13-ecdf-fill-cap",
        "kind": "forbid_add",
        "paths": ["server/report/static/webreport/distribution.js",
                  "client/excel_download/_charts.py"],
        "pattern": r"\bmin\(.*FILL_VISUAL_MAX_DY",
        # 같은 편집 조각에 n 기반 계산(100/n)이 있으면 그 캡은 폴백 경로다 — 위반 아님.
        # 줄 단위로는 두 줄이 갈라져 판정할 수 없어 multi(조각 전체)로 본다.
        "unless": r"100(\.0)?\s*/\s*(float\()?n\b",
        "multi": True,
        "why": "미니셀 세로 채움 간격은 서버가 준 소스별 표본 수 n 으로 정한다"
               "(stepY=100/n) — 채우는 점 개수가 실제 측정 개수와 같아야 한다. 고정 상수로 "
               "상한을 걸면(FILL_VISUAL_MAX_DY 를 n 폴백 밖에서 사용) 표본이 작은 세션이 "
               "실제보다 촘촘하게 그려진다(2026-08-25 회귀: n=100 이 400점으로 보임). "
               "그 캡은 n 이 없는 옛 응답 폴백 전용이다.",
        "doc": "CLAUDE.md §5-5",
    },
    {
        "id": "R03-gzip-level",
        "kind": "forbid_add",
        "paths": ["web_report/**/*.py"],
        "pattern": r"compresslevel\s*=\s*[2-9]",
        "why": "gzip compresslevel 은 1 고정이 정책이다. 올리면 콜드 빌드 CPU 가 늘어 "
               "빌드 시간과 워커 점유가 함께 증가한다.",
        "doc": "docs/12_web_report_cache.md",
    },
    {
        "id": "R05-excel-import",
        "kind": "forbid_add",
        "paths": ["web_report/**/*.py", "server/report/**/*.py"],
        "pattern": r"^\s*(?:import|from)\s+(openpyxl|xlwings|win32com)\b",
        "why": "서버는 openpyxl·Excel 을 쓰지 않는다. Excel 추출은 클라이언트(Excel COM) "
               "몫이고, 서버가 Excel 을 열면 워커가 통째로 묶인다.",
        "doc": "CLAUDE.md §5-1",
    },
    {
        "id": "R06-es-module",
        "kind": "forbid_add",
        "paths": ["server/report/static/webreport/*.js"],
        "pattern": r"^\s*(?:import\s|export\s|export\{)",
        "why": "세션 상세 JS 는 classic script 순서 로드로 전역 스코프를 공유한다. "
               "ES module 로 바꾸거나 로드 순서를 바꾸면 전역 참조가 끊겨 탭이 죽는다.",
        "doc": "docs/11_web_report_tabs.md",
    },
    {
        "id": "R08-inline-fallback",
        "kind": "forbid_add",
        "paths": ["web_report/compute.py"],
        "pattern": r"except\s+(?:BrokenProcessPool|TimeoutError)[\s\S]{0,600}?"
                   r"\breturn\s+job\s*\(",
        "multi": True,
        "why": "워커 붕괴(BrokenProcessPool)·타임아웃 시 인라인 폴백을 하면 안 된다. "
               "붕괴 원인이 메모리/데이터면 같은 작업을 웹 프로세스에서 다시 돌려 GIL 과 "
               "RAM 을 그대로 태우고 웹 프로세스까지 죽는다. 호출부가 503 으로 돌려주는 "
               "것이 의도된 동작이다.",
        "doc": "web_report/compute.py run() docstring",
    },
    {
        "id": "R09-chunk-keyed-lock",
        "kind": "forbid_add",
        "paths": ["web_report/dist_pack_store.py"],
        # 호출 형태만 본다 — 이 파일 docstring 이 "keyed_lock 은 잡지 않는다"를
        # 설명하고 있어, 맨 이름으로 잡으면 자기 문서에 걸린다.
        "pattern": r"keyed_lock\w*\s*\(",
        "why": "chunk 키에 keyed_lock 을 잡으면 안 된다. 락 레지스트리 LRU 상한"
               "(_KEYED_LOCKS_MAX=256)에 chunk 키가 유입되면 빌드·편집 락이 축출돼 "
               "상호배제 자체가 깨진다.",
        "doc": "docs/12_web_report_cache.md",
    },
    {
        "id": "R10-tmp-fixed-name",
        "kind": "forbid_add",
        "paths": ["web_report/disk_cache.py", "web_report/dist_pack_store.py"],
        "pattern": r"['\"][^'\"]*\.tmp['\"]",
        "unless": r"getpid",
        "why": "tmp 파일명에는 pid(+스레드 id)를 반드시 박는다. 고정 '.tmp' 를 쓰면 "
               "프리웜 워커·온디맨드 워커·부모가 같은 키를 동시에 쓸 때 os.replace "
               "경쟁으로 간헐 write 실패가 난다.",
        "doc": "web_report/disk_cache.py _write()",
    },

    # ---- 아래는 diff 전체 맥락이 필요해 --hook 에서는 평가하지 않는다 ----
    {
        "id": "S01-report-schema",
        "kind": "require_pair",
        "when": ["web_report/metrics.py", "web_report/tabs/**/*.py"],
        "then_file": "web_report/cache_policy.py",
        "then_pattern": r"REPORT_SCHEMA_VERSION\s*=",
        "why": "report payload 구조를 바꿨으면 cache_policy.REPORT_SCHEMA_VERSION 을 "
               "반드시 올려야 한다. 안 올리면 disk_cache 가 옛 payload 를 계속 반환해 "
               "화면이 조용히 옛 모습으로 남는다(과거 'Yield 접기 사라짐' 실사례). "
               "구조 변경이 아니라 값 계산만 고쳤다면 면제 주석으로 넘어갈 것.",
        "doc": "web_report/cache_policy.py v3~v27 주석",
    },
    {
        "id": "S02-map-schema",
        "kind": "require_pair",
        "when": ["web_report/tabs/Map_analysis.py"],
        "then_file": "web_report/cache_policy.py",
        "then_pattern": r"MAP_SCHEMA_VERSION\s*=",
        "why": "Map payload 구조를 바꿨으면 MAP_SCHEMA_VERSION 을 올려야 옛 map 캐시가 "
               "무효화된다.",
        "doc": "web_report/cache_policy.py MAP_SCHEMA_VERSION",
    },
    {
        "id": "S03-worker-pair",
        "kind": "require_pair",
        "when": ["web_report/compute.py"],
        "when_pattern": r"_WORKERS\s*=",
        "then_file": "web_report/compute.py",
        "then_pattern": r"_ONDEMAND_WORKERS\s*=",
        "why": "컴퓨트 워커 수(_WORKERS)와 온디맨드 소비자 스레드 수(_ONDEMAND_WORKERS)는 "
               "짝으로 올려야 한다. 풀만 늘리면 소비자 스레드 수가 새 상한이 되어 "
               "체감 개선이 없다.",
        "doc": "docs/12_web_report_cache.md",
    },
    {
        "id": "S05-unique-once",
        "kind": "forbid_remove",
        "paths": ["web_report/dist_pack.py"],
        "pattern": r"return_inverse\s*=\s*True",
        "why": "np.unique(..., return_inverse=True) 1회로 전체+bin1 을 동시에 산출한다. "
               "이걸 없애고 두 번 돌리면 Distribution pack 빌드 비용이 2배가 된다.",
        "doc": "web_report/dist_pack.py",
    },
    {
        "id": "S07-finite-scan",
        "kind": "forbid_remove",
        "paths": ["web_report/metrics.py"],
        "pattern": r"finite_count_map\s*\(",
        "why": "finite_count_map 은 전 데이터 스캔을 1회로 통합한 것이다. 없애면 과거처럼 "
               "3회 스캔으로 되돌아간다.",
        "doc": "web_report/metrics.py",
    },
    {
        "id": "S08-cancel-preserve-pool",
        "kind": "forbid_remove",
        "paths": ["web_report/compute.py"],
        "pattern": r"if\s+fut\.cancel\(\)",
        "why": "큐 대기만 하다 타임아웃한 잡은 cancel 이 성공한다(아직 미시작 = 워커 무결). "
               "이 분기를 없애고 무조건 _reset_pool 하면 전 워커 terminate 로 무고한 "
               "동시 빌드까지 전멸한다.",
        "doc": "web_report/compute.py run()",
    },
    {
        "id": "S09-map-seed",
        "kind": "forbid_remove",
        "paths": ["web_report/service.py", "server/report/routes_session.py"],
        "pattern": r"\bseed_map\(session_id|schedule_map_backfill\(",
        "why": "Map 3초 SLA(§5-11)는 report 콜드 빌드의 seed_map 시딩과 /full 200 백필이 "
               "달성한다. 이 호출(또는 정의)을 없애면 Map 탭·Issue Table Map 컬럼 첫 진입이 "
               "콜드 202 + 전체 재디코드로 돌아가 대형 세션에서 30초+ 프리즈가 된다. "
               "옮기는 것뿐이라면 면제 주석을 달 것.",
        "doc": "CLAUDE.md §5-11, docs/12_web_report_cache.md",
    },
    {
        "id": "S10-ai-comment-cache",
        "kind": "forbid_remove",
        "paths": ["web_report/service.py"],
        "pattern": r"_ai_comment_cached\(|save_ai_comment\(",
        "why": "AI Comment 세션 콜드 빌드의 80%가 eval 평가였다(2026-08-13 실측 4.7s/5.9s). "
               "분리 캐시(_ai_comment_cached → disk_cache.save_ai_comment)를 우회하면 "
               "comment 편집·스키마 bump·dedup 형제 세션마다 전량 재평가로 돌아가고, "
               "대형 세션에서 300초 타임아웃(워커 연쇄 전멸)이 재발한다. "
               "옮기는 것뿐이라면 면제 주석을 달 것.",
        "doc": "docs/13_eval_analyzer_integration.md, web_report/service.py _ai_comment_cached",
    },
    {
        "id": "S12-compare-cache",
        "kind": "forbid_remove",
        "paths": ["web_report/service.py"],
        "pattern": r"_compare_cached\(|save_compare\(",
        "why": "Compare 계산은 콜드 빌드의 34% 였다(2026-08-19 실측 1.147s/3.323s). "
               "분리 캐시(_compare_cached → disk_cache.save_compare)를 우회하면 코멘트 "
               "한 줄 편집·REPORT_SCHEMA_VERSION bump·dedup 형제 세션마다 compare 전량 "
               "재계산으로 돌아간다(공통 die 맵·항목별 KS/Welch 를 다시 전부 돈다). "
               "옮기는 것뿐이라면 면제 주석을 달 것.",
        "doc": "docs/12_web_report_cache.md, web_report/service.py _compare_cached",
    },
    {
        "id": "S17-ai-suggest-session-scope",
        "kind": "forbid_add",
        "paths": ["web_report/service.py"],
        # 클라 LLM 문장의 진실은 세션 편집 DB(kind=ai_suggest) 하나다. 공유 파일 저장소를
        # service 가 다시 읽거나 쓰면 dedup 형제 세션의 문장이 되살아난다.
        "pattern": r"ai_suggest_store\.(load|save_merge)\(",
        "why": "AI Comment [제안] 문장은 **세션 편집 DB 가 진실**이다(2026-09-02). 종전 "
               "ai_suggest_store 는 analysis_key 단위 공유 파일이라, 같은 rawdata 를 다시 "
               "올린 형제 세션이 서로의 문장을 봤다 — 새 세션이 남의 옛 문장부터 보여 주고 "
               "한쪽 push 가 다른 세션 화면을 바꿨다(사용자 신고). service 가 그 파일을 "
               "다시 읽으면 같은 간섭이 되살아난다. 세션 문장은 "
               "edits.load_ai_suggestions / apply_webreport_edits 로만 다룰 것.",
        "doc": "docs/23_ai_comment_client_llm.md, web_report/service.py _session_ai_overlay",
    },
    {
        "id": "S15-ai-job-cache-first",
        "kind": "forbid_remove",
        "paths": ["web_report/compute.py"],
        "pattern": r"ai_comment_cache_job|compare_cache_job",
        "why": "'ai'/'compare' 잡은 **2단계**다 — ① 분리 캐시만 채우고(report 락 없음) "
               "② payload 를 짧게 다시 굽는다. ①을 없애고 report_job(ai_inline=True) "
               "하나로 되돌리면, 부모 소비자 스레드가 엔진 평가(실측 100초+) 내내 "
               "report keyed_lock 을 쥐어 **사용자의 1초짜리 pending 빌드가 그 뒤에 줄을 "
               "선다**(2026-09-02 'AI Comment 켜면 첫 조회 100초'의 실제 원인). "
               "옮기는 것뿐이라면 면제 주석을 달 것.",
        "doc": "CLAUDE.md §5-17, docs/12_web_report_cache.md",
    },
    {
        "id": "S16-prewarm-pending-first",
        "kind": "forbid_add",
        "paths": ["web_report/compute.py"],
        # 프리웜은 pending 본을 먼저 남긴다 — ai_inline=True 로 되돌리는 변경을 막는다.
        "pattern": r"report_job\(session_id,\s*upload_root_str,\s*True\)",
        "why": "프리웜(prewarm_job)은 ai_inline=False 로 **pending 본을 먼저** 디스크에 "
               "남긴다. True 로 되돌리면 AI 평가가 끝날 때까지 즉시 열 수 있는 산출물이 "
               "하나도 없어(_pending_kinds(inline=True) 는 빈 튜플), 업로드 직후 첫 클릭이 "
               "완전 콜드로 판정돼 100초를 202 폴링한다(2026-09-02 실사례). AI·Compare 는 "
               "prewarm_job 이 돌려주는 pending_kinds 로 백그라운드 잡에 넘긴다.",
        "doc": "CLAUDE.md §5-17, docs/12_web_report_cache.md",
    },
    {
        "id": "S13-cold-poll-cheap",
        "kind": "forbid_add",
        "paths": ["web_report/service.py"],
        # 폴링 판정 경로가 쓰면 안 되는 "무거운" 호출들. 본문을 실제로 쓰는 콜드 빌드는
        # `_ai_signature_cached`(내용 필요)를 계속 쓰므로 그 이름은 패턴에서 뺀다 —
        # 여기서 막는 것은 값싼 판정 자리에 본문 로드·엔진 조회를 다시 넣는 변경이다.
        "pattern": r"disk_cache\.load_ai_comment\(|ai_comment\.llm_status\(",
        "why": "콜드 판정·폴링 경로(report_is_cold → _pending_kinds)는 콜드 세션 1건당 "
               "15분간 수백 회 돈다. 여기서 gzip 본문을 읽거나(load_ai_comment) "
               "eval_engine 을 import(llm_status)하면 202 폴링이 통째로 느려진다 "
               "(2026-08-28 Signature 2단계 분리가 실제로 그렇게 회귀시켰다). "
               "존재 확인은 *_exists(stat 1회), 설정 조회는 _ai_two_stage_wanted 메모이즈를 "
               "쓸 것. 내용이 정말 필요한 자리면 면제 주석을 달 것.",
        "doc": "CLAUDE.md §5-17, docs/12_web_report_cache.md",
    },
    {
        "id": "S14-ai-inline-gate",
        "kind": "forbid_add",
        "paths": ["web_report/service.py"],
        # allow_build 는 ai_inline 변수로만 넘긴다. 리터럴 True 로 굳히면 사용자 대기
        # 경로가 엔진 평가를 동기로 돌게 된다(S10 은 함수 존재만 봐서 이걸 못 잡는다).
        "pattern": r"allow_build\s*=\s*True",
        "why": "AI Comment·Compare 를 콜드 빌드에서 떼어낸 유일한 방어선이 "
               "`allow_build=ai_inline` 게이트다. True 로 고정하면 사용자가 기다리는 "
               "콜드 빌드가 다시 eval 엔진 평가를 동기로 돌려 '리포트 먼저, AI 는 나중' "
               "구조가 무너진다(2026-08-13 분리 이전으로 회귀). S10 은 분리 캐시 함수의 "
               "존재만 보므로 이 변경을 잡지 못한다.",
        "doc": "CLAUDE.md §5-17, docs/12_web_report_cache.md",
    },
    {
        "id": "R11-keyed-lock-cap",
        "kind": "forbid_add",
        "paths": ["web_report/cache.py"],
        # \s* 를 lookahead 밖에 두면 백트래킹으로 lookahead 위치가 밀려 256 도 걸린다.
        "pattern": r"_KEYED_LOCKS_MAX\s*=(?!\s*256\b)",
        "why": "락 레지스트리 LRU 상한을 낮추면 보유 중인 빌드·편집 락이 축출돼 상호배제가 "
               "깨진다. 값을 바꿔야 한다면 축출 영향부터 검토할 것.",
        "doc": "web_report/cache.py _KEYED_LOCKS_MAX",
    },
    {
        "id": "S11-ondemand-force-offload",
        "kind": "forbid_remove",
        "paths": ["web_report/compute.py", "web_report/service.py"],
        "pattern": r"force_offload_for_consumer\(|should_offload_heavy\(",
        "why": "온디맨드(202) 소비자 스레드의 빌드와 dist/temp_map/trim 콜드 산출물은 "
               "**워커 오프로드가 유일한 시간 상한**이다. 파이썬 스레드는 강제 종료가 "
               "불가하므로 인라인으로 떨어지면 300초든 3시간이든 아무도 끊지 못하고 "
               "화면은 202 를 무한 폴링한다(2026-08-13 Issue Table 편집 후 무한 로딩). "
               "옮기는 것뿐이라면 면제 주석을 달 것.",
        "doc": "web_report/compute.py force_offload_for_consumer / should_offload_heavy",
    },
    {
        "id": "R12-issue-delete-reload",
        "kind": "forbid_add",
        "paths": ["server/report/static/webreport/edit_mode.js"],
        # 삭제/숨김 계열 함수 본문 안의 load(false) 만 잡는다 — 같은 파일의 ETC '추가'와
        # '삭제 전체 초기화'는 서버가 행을 만들어/되살려야 하므로 재로드가 정당하다.
        # 함수 선언부와 묶지 않으면 이 규칙의 배경을 적어 둔 주석 문자열에도 걸린다.
        "pattern": r"async function (?:removeEtcItem|hideIssueRow|deleteSelectedIssueRows)"
                   r"[\s\S]{0,1500}?\bload\s*\(\s*false\s*\)",
        "multi": True,
        "why": "Issue Table 행 삭제(숨김/ETC 제거) 후 load(false) 로 세션을 통째로 다시 "
               "받으면 안 된다. 편집은 rev 를 올려 report 캐시 키를 바꾸므로 그 재로드가 "
               "**행 하나 지울 때마다 리포트 전체 콜드 빌드**를 유발한다(2026-08-13 무한 "
               "로딩 사건의 방아쇠). 낙관 반영(removeIssueRowsLocal)으로 화면에서만 지우고 "
               "진실은 편집 DB 에 남겨, 다음 새로고침 때 서버가 같은 행을 빼고 그린다.",
        "doc": "server/report/static/webreport/edit_mode.js removeIssueRowsLocal",
    },
    {
        "id": "S06-cache-key-builder",
        "kind": "require_import",
        "severity": "warn",
        "paths": ["web_report/**/*.py"],
        "pattern": r"cache\.(?:cache_get|\w*cache_put)\s*\(",
        "require": r"cache_policy",
        "why": "캐시 키는 반드시 cache_policy 빌더로 만든다(호출부 즉석 조립 금지). "
               "키를 직접 조립하면 무효화 트리거가 빠져 stale 캐시를 서빙한다. "
               "키를 인자로 받아 쓰는 파일이면 무시해도 된다.",
        "doc": "CLAUDE.md §5-6, docs/12_web_report_cache.md",
    },
]


# --------------------------------------------------------------------------
# 경로 매칭
# --------------------------------------------------------------------------
def _glob_re(pat: str) -> re.Pattern:
    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif c == "*":
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


_GLOB_CACHE: dict[str, re.Pattern] = {}


def _match_path(rel: str, patterns) -> bool:
    for p in patterns:
        rx = _GLOB_CACHE.get(p)
        if rx is None:
            rx = _GLOB_CACHE[p] = _glob_re(p)
        if rx.match(rel):
            return True
    return False


def in_scope(rel: str) -> bool:
    return rel.startswith(SCOPE)


def rel_path(file_path: str) -> str | None:
    """절대/상대 경로를 저장소 상대 posix 경로로. 저장소 밖이면 None."""
    try:
        p = Path(file_path)
        if not p.is_absolute():
            p = ROOT / p
        return p.resolve().relative_to(ROOT).as_posix()
    except Exception:
        return None


# --------------------------------------------------------------------------
# 검사 코어
# --------------------------------------------------------------------------
def _severity(rule) -> str:
    return rule.get("severity", "block")


def _exempted(text: str, rule_id: str) -> bool:
    for m in EXEMPT_RE.finditer(text):
        if m.group(1) == rule_id:
            return True
    return False


def _scan_added(rel: str, added: str, lineno_valid: bool = False) -> list[dict]:
    """추가된 텍스트 조각에 forbid_add 규칙을 적용.

    ``lineno_valid`` 는 ``added`` 가 파일 전문일 때만 True — 편집 조각이나 diff 의
    추가줄 모음은 파일 줄번호와 대응되지 않으므로 번호를 붙이지 않는다(오해 방지).
    """
    hits = []
    lines = added.splitlines()
    for rule in _RULES:
        if rule["kind"] != "forbid_add" or not _match_path(rel, rule["paths"]):
            continue
        rx = re.compile(rule["pattern"], re.MULTILINE)
        unless = re.compile(rule["unless"]) if rule.get("unless") else None

        if rule.get("multi"):
            if rx.search(added) and not (unless and unless.search(added)):
                if not _exempted(added, rule["id"]):
                    hits.append({"rule": rule, "file": rel, "line": None,
                                 "text": added.strip().splitlines()[:1]})
            continue

        for i, line in enumerate(lines):
            if COMMENT_RE.match(line) or not rx.search(line):
                continue
            if unless and unless.search(line):
                continue
            ctx = line + "\n" + (lines[i - 1] if i else "")
            if _exempted(ctx, rule["id"]):
                continue
            hits.append({"rule": rule, "file": rel,
                         "line": i + 1 if lineno_valid else None,
                         "text": line.strip()})
    return hits


def _scan_removed(rel: str, removed: str) -> list[dict]:
    hits = []
    for rule in _RULES:
        if rule["kind"] != "forbid_remove" or not _match_path(rel, rule["paths"]):
            continue
        rx = re.compile(rule["pattern"])
        for line in removed.splitlines():
            if COMMENT_RE.match(line) or not rx.search(line):
                continue
            if _exempted(removed, rule["id"]):
                continue
            hits.append({"rule": rule, "file": rel, "line": None,
                         "text": line.strip()})
    return hits


def _scan_require_import(rel: str, added: str, full_text: str) -> list[dict]:
    hits = []
    for rule in _RULES:
        if rule["kind"] != "require_import" or not _match_path(rel, rule["paths"]):
            continue
        rx = re.compile(rule["pattern"])
        if not any(rx.search(l) for l in added.splitlines()
                   if not COMMENT_RE.match(l)):
            continue
        if re.search(rule["require"], full_text):
            continue
        if _exempted(added, rule["id"]) or _exempted(full_text, rule["id"]):
            continue
        hits.append({"rule": rule, "file": rel, "line": None, "text": ""})
    return hits


# --------------------------------------------------------------------------
# git diff
# --------------------------------------------------------------------------
def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def collect_diff(ref: str = "HEAD") -> dict[str, dict]:
    """{rel: {"added": str, "removed": str}} — 추적 변경 + 미추적 신규 파일."""
    out: dict[str, dict] = {}
    cur = None
    for line in _git("diff", "--unified=0", ref, "--").splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
            out.setdefault(cur, {"added": "", "removed": ""})
        elif line.startswith("--- ") or line.startswith("+++ "):
            continue
        elif cur and line.startswith("+"):
            out[cur]["added"] += line[1:] + "\n"
        elif cur and line.startswith("-"):
            out[cur]["removed"] += line[1:] + "\n"
    for rel in _git("ls-files", "--others", "--exclude-standard").splitlines():
        rel = rel.strip()
        if not rel or not in_scope(rel):
            continue
        try:
            out[rel] = {"added": (ROOT / rel).read_text(encoding="utf-8",
                                                        errors="replace"),
                        "removed": ""}
        except OSError:
            pass
    return {k: v for k, v in out.items() if in_scope(k)}


def _read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def scan_diff(ref: str = "HEAD", diff: dict | None = None) -> list[dict]:
    # diff 주입은 테스트용 — git 없이 require_pair 를 검증하려면 필요하다.
    diff = collect_diff(ref) if diff is None else diff
    hits: list[dict] = []
    for rel, d in diff.items():
        hits += _scan_added(rel, d["added"])
        hits += _scan_removed(rel, d["removed"])
        hits += _scan_require_import(rel, d["added"], _read(rel))

    changed = set(diff)
    for rule in _RULES:
        if rule["kind"] != "require_pair":
            continue
        touched = [f for f in changed if _match_path(f, rule["when"])]
        if rule.get("when_pattern"):
            wrx = re.compile(rule["when_pattern"])
            touched = [f for f in touched
                       if any(wrx.search(l) for l in diff[f]["added"].splitlines())]
        if not touched:
            continue
        then = diff.get(rule["then_file"], {"added": ""})["added"]
        trx = re.compile(rule["then_pattern"])
        if any(trx.search(l) for l in then.splitlines()):
            continue
        if any(_exempted(diff[f]["added"], rule["id"]) for f in touched):
            continue
        hits.append({"rule": rule, "file": ", ".join(sorted(touched)),
                     "line": None, "text": ""})
    return hits


# --------------------------------------------------------------------------
# 출력
# --------------------------------------------------------------------------
def _fmt(hits: list[dict]) -> str:
    parts = []
    for h in hits:
        r = h["rule"]
        loc = f"{h['file']}:{h['line']}" if h["line"] else h["file"]
        mark = "위반" if _severity(r) == "block" else "주의"
        parts.append(
            f"[{mark}] {r['id']}  {loc}\n"
            f"  {r['why']}\n"
            f"  근거: {r['doc']}\n"
            + (f"  해당: {h['text']}\n" if h["text"] else "")
            + f"  의도한 변경이면 그 줄에 `perf-guard: allow {r['id']}` 주석과 사유를 달 것."
        )
    return "\n\n".join(parts)


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


# --------------------------------------------------------------------------
# 모드
# --------------------------------------------------------------------------
def run_hook() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    ti = payload.get("tool_input") or {}
    rel = rel_path(ti.get("file_path") or "")
    if not rel or not in_scope(rel):
        return 0
    added = ti.get("content") if payload.get("tool_name") == "Write" \
        else ti.get("new_string")
    if not added:
        return 0
    hits = [h for h in _scan_added(rel, added) if _severity(h["rule"]) == "block"]
    if not hits:
        return 0
    _emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason":
            "성능 가드가 이 변경을 막았습니다 — 과거에 회귀가 났던 지점입니다.\n\n"
            + _fmt(hits),
    }})
    return 0


_MARKER = Path(tempfile.gettempdir()) / "perf_guard_last_stop.txt"


def _once(sig: str) -> bool:
    """같은 사유로 두 번 막지 않는다 — 못 고치는 상태에 갇히면 안 된다."""
    digest = hashlib.sha256(sig.encode()).hexdigest()
    try:
        if _MARKER.read_text(encoding="utf-8").strip() == digest:
            return False
    except OSError:
        pass
    try:
        _MARKER.write_text(digest, encoding="utf-8")
    except OSError:
        pass
    return True


def run_stop() -> int:
    # 이미 Stop 훅 때문에 한 번 되돌아온 턴이면 두 번 막지 않는다.
    try:
        if json.loads(sys.stdin.read() or "{}").get("stop_hook_active"):
            return 0
    except Exception:
        pass

    diff = collect_diff()
    hits = [h for h in scan_diff(diff=diff) if _severity(h["rule"]) == "block"]
    if hits:
        sig = "violate|" + "|".join(
            sorted(f"{h['rule']['id']}:{h['file']}" for h in hits))
        if _once(sig):
            _emit({"decision": "block",
                   "reason": "성능 가드가 작업트리에서 회귀 위험 변경을 찾았습니다. "
                             "되돌리거나, 의도한 변경이면 면제 주석을 다세요.\n\n"
                             + _fmt(hits)})
        return 0

    # 위반은 없다. 조회/빌드 속도에 영향을 줄 수 있는 파일이 바뀌었는지만 알린다 —
    # 실측 벤치는 수십 초 걸려 매번 돌릴 수 없으므로 턴 끝에 한 번 제안하게 한다.
    touched = sorted(f for f in diff if _match_path(f, PERF_SENSITIVE))
    if not touched:
        _MARKER.unlink(missing_ok=True)
        return 0
    if not _once("bench|" + "|".join(touched)):
        return 0
    _emit({"decision": "block", "reason":
           "이번 작업에서 조회/빌드 속도에 영향을 줄 수 있는 파일이 바뀌었습니다:\n"
           + "".join(f"  - {f}\n" for f in touched)
           + "\n**속도 개선이 목적인 변경이었다면** 여기서 마치지 말고 사용자에게 "
             "실측 벤치를 돌릴지 AskUserQuestion 으로 물어보세요.\n"
             f"    {BENCH_CMD}\n"
             "  (수십 초, 임시 DB 격리라 운영 무접촉. 이전 실행 대비 회귀를 자동 판정하고 "
             "결과 해석 절차는 스킬 webreport-bench 에 있습니다.)\n\n"
             "속도와 무관한 변경(라벨·문구 수정, 기능 추가, 버그 수정 등)이었다면 "
             "묻지 말고 그대로 마치세요. 이 알림은 같은 파일 집합에 대해 한 번만 뜹니다."})
    return 0


def run_diff(ref: str) -> int:
    hits = scan_diff(ref)
    if not hits:
        print(f"perf_guard: {ref} 대비 위반 없음")
        return 0
    print(_fmt(hits))
    blocking = [h for h in hits if _severity(h["rule"]) == "block"]
    print(f"\n위반 {len(blocking)}건 / 주의 {len(hits) - len(blocking)}건")
    return 1 if blocking else 0


def run_scan_all() -> int:
    hits = []
    for root in SCOPE:
        for p in sorted((ROOT / root).rglob("*")):
            if not p.is_file() or p.suffix not in (".py", ".js"):
                continue
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(ROOT).as_posix()
            text = _read(rel)
            hits += _scan_added(rel, text, lineno_valid=True)
            hits += _scan_require_import(rel, text, text)
    if not hits:
        print("perf_guard: 현재 코드 전체 위반 없음 (오탐 0)")
        return 0
    print(_fmt(hits))
    print(f"\n총 {len(hits)}건 — 현재 코드가 걸리는 규칙은 그대로 쓸 수 없다. "
          f"paths/pattern 을 좁히거나 면제 주석을 달 것.")
    return 1


def run_list() -> int:
    for r in _RULES:
        scope = ", ".join(r.get("paths") or r.get("when") or [])
        print(f"{r['id']}  [{r['kind']}/{_severity(r)}]  {scope}")
        print(f"    {r['why']}")
        print(f"    근거: {r['doc']}")
    print(f"\n총 {len(_RULES)}개 — 이 목록이 정본이다(문서에 복제하지 않는다).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="web_report 성능 회귀 정적 가드")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--hook", action="store_true")
    g.add_argument("--stop", action="store_true")
    g.add_argument("--diff", action="store_true")
    g.add_argument("--scan-all", action="store_true")
    g.add_argument("--list", action="store_true", help="규칙 목록 (정본)")
    g.add_argument("--selftest", action="store_true")
    ap.add_argument("--ref", default="HEAD")
    a = ap.parse_args()

    if a.hook or a.stop:
        try:                                    # fail-open
            return run_hook() if a.hook else run_stop()
        except Exception:
            return 0
    if a.diff:
        return run_diff(a.ref)
    if a.scan_all:
        return run_scan_all()
    if a.list:
        return run_list()
    from tests.test_perf_guard import selftest   # noqa: PLC0415
    return selftest()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())

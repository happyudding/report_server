---
name: webreport-change
description: web_report payload/탭/지표 구조를 바꾼 뒤 해야 하는 후속 절차(스키마 버전 bump → 재기동 → 프리웜 → 벤치). 사용자가 "스키마 버전 올려야 해?", "web_report 고쳤는데 뭐 해야 돼", "탭 추가했어", "캐시가 옛날 화면을 보여준다" 같은 상황이거나, web_report/tabs·metrics·cache_policy 를 수정한 직후에 사용.
---

# web_report 구조 변경 후속 절차

payload 구조를 바꿔 놓고 스키마 버전을 안 올리면 `disk_cache` 가 **옛 payload 를 계속
반환**한다. 에러가 아니라 화면이 조용히 옛 모습으로 남는 형태라 발견이 늦다
(실사례: "Yield 접기 사라짐"). 아래 4단계는 순서대로 판단·실행한다.

## 1. 구조 변경인지 판정

**대상**: payload 최상위 키·그룹 형태가 바뀐 경우. `web_report/tabs/**` 에서 키를
추가/제거/개명했거나, 응답 배열의 항목 구조를 바꿨으면 해당한다.

**대상 아님**: 같은 구조로 **값 계산만** 바꾼 경우. 이때는 버전을 올리지 않는다
(전 세션 캐시가 통째로 날아가는 비용만 치른다).

판단이 애매하면 올리는 쪽이 안전하다 — 비용은 1회 재계산이지만, 안 올렸을 때의 비용은
사용자가 잘못된 화면을 계속 보는 것이다.

## 2. 스키마 버전 bump

정본은 [web_report/cache_policy.py](../../../web_report/cache_policy.py) — 세 상수를 구분해 쓴다:

| 상수 | 무엇을 바꿨을 때 |
|---|---|
| `REPORT_SCHEMA_VERSION` | `build_report_payload` 출력 구조 (일반적인 탭·지표 변경) |
| `MAP_SCHEMA_VERSION` | Map dies payload 구조 |
| `TEMP_MAP_SCHEMA_VERSION` | `.../web_report/temp_map` 응답 구조(`sources[].items[].idx`) |

각 상수 **바로 위 주석 블록에 `# vN: <바뀐 내용> (날짜)` 1줄을 추가**하는 것이 이 파일의
관례다. 기존 이력이 전부 그 형식이라 나중에 "왜 올렸는지"를 되짚을 유일한 근거가 된다.

perf_guard 짝 규칙 `S01-report-schema` / `S02-map-schema` 가 이걸 강제한다. 구조 변경이
아니라고 확신하면 `# perf-guard: allow S01-report-schema (사유)` 로 면제하되 **사유 없는
면제는 금지**. 같은 규칙에 면제가 반복해 붙으면 규칙 쪽이 틀린 것이다 → [docs/18](../../../docs/18_perf_guard.md).

## 3. 서버 재기동

스키마 버전은 파이썬 상수라 **재기동해야 반영된다**. 절차와 판단 기준은 `server-restart`
skill 을 따른다(운영 서버는 사용자 승인 후 실행).

## 4. 프리웜 + 검증

bump 는 **전 세션의 디스크 캐시를 한 번에 무효화**한다. 재기동 직후 조회가 몰리면
온디맨드 워커에 콜드 빌드가 줄을 서고 사용자는 그만큼 "세션 불러오는 중" 을 본다.
한산한 시간에 미리 데운다:

```
server\.venv\Scripts\python.exe tools\warm_webreport.py --apply
```

- 기본은 **dry-run**(대상만 표시) — 실제 계산은 `--apply` 필수.
- 범위 조절: `--days 7 --limit 50`, 특정 1건만 `--session <session_id>`.
- 세션 1건씩 순차·멱등(이미 캐시된 세션은 건너뜀)이라 여러 번 돌려도 안전하다.

**AI Comment 세션이 섞여 있으면** (`.rules_rev`·`evalcpk` 표식·`AI_COMMENT_SCHEMA_VERSION`
을 갈았을 때는 특히): 프리웜은 **pending 본까지만** 만들고 AI 평가는 백그라운드 `'ai'`
잡으로 넘어간다(설계대로 — 사용자는 리포트를 먼저 본다). 그 잡들은
`WEB_REPORT_AI_JOB_LIMIT`(기본 워커수/3) 로 동시 실행이 제한되므로, 배포 직후 몇 분간은
Issue Table 의 AI Comment 칸이 "Loading 중…" 으로 보일 수 있다 — **정상이다.**
관리자 현황 탭에서 `ondemand:ai` 진행 건수와 `compute.STATS["ai_deferred"]` 로 진척을
확인한다. 리포트 자체가 느리면 그건 다른 문제다(CLAUDE.md 규칙 17).

성능 영향이 의심되면 `webreport-bench` skill 로 이어간다.

## 5. 함께 걸리는 함정

- **새 탭 추가**는 3단계 고정: `web_report/tabs/` 빌더 1개 → `tabs/__init__.py` 의
  `TAB_REGISTRY` 에 `TabSpec` 1줄(**등록 순서 = 화면 표시 순서**) → 프런트 JS 1개.
  → [docs/11](../../../docs/11_web_report_tabs.md)
- **대용량 payload 를 `/full` 에 싣지 말 것** — lazy 탭 관례. Map dies 를 실었다가 실제
  프리즈 사고가 났다.
- 프런트 JS 는 **classic script 순서 로드**다. ES module 로 바꾸면 탭이 죽는다(`R06`).
- 캐시 키는 반드시 `cache_policy.py` 의 빌더로 만든다. 즉석 조립 금지(`S06`).
- Distribution **다운샘플 금지**는 여전히 유효(`R01`/`R02`) — CLAUDE.md 불변규칙 5.

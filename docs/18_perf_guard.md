# 18 · web_report 성능 회귀 가드 (perf_guard)

## 이 문서가 하는 일

"속도 개선했는데 오히려 느려지거나 세션이 안 열린다"를 **코드가 쓰이기 전에** 잡는
정적 가드의 사용법. 구현은 [tools/perf_guard.py](../tools/perf_guard.py),
자동 실행 설정은 [.claude/settings.json](../.claude/settings.json).

**왜 필요했나.** 규칙 자체는 원래 있었다 — [CLAUDE.md §5](../CLAUDE.md),
[docs/11](11_web_report_tabs.md), [docs/12](12_web_report_cache.md),
[cache_policy.py](../web_report/cache_policy.py) 의 v3~v27 주석. 문제는 그 규칙들이
**변경 시점에 확인되지 않아** 놓치면 그대로 통과했다는 것이다. 가드는 그 확인을
사람·모델의 기억에서 하네스로 옮긴다.

## 규칙 목록의 정본은 코드다

```
server\.venv\Scripts\python.exe tools\perf_guard.py --list
```

각 규칙이 자기 근거 문서를 들고 있다. **문서에 규칙 목록을 복제하지 않는다** —
복제하면 즉시 드리프트가 나기 때문이다(실제로 docs/12 의 REPORT_CACHE 키 설명이
`cache_policy.report_key` 실제 구현과 이미 어긋나 있다).

## 언제 도는가

| 시점 | 모드 | 검사 대상 | 위반 시 |
|------|------|-----------|---------|
| Edit / Write 직전 (자동) | `--hook` | 아직 쓰이지 않은 **변경 조각** | 쓰기 자체가 **거부**된다 |
| 턴 종료 시 (자동) | `--stop` | 작업트리 diff 전체 | 턴이 막히고 사유가 전달된다 |
| 수동 | `--diff [--ref REF]` | 작업트리 diff 전체 | exit 1 |
| 수동 | `--scan-all` | 범위 내 전 파일 | exit 1 — 규칙 도입 시 오탐 점검용 |

## 턴 끝의 벤치 제안

실측 벤치는 수십 초가 걸려 편집마다 돌릴 수 없다. 그래서 `--stop` 은 **위반이 없을 때**,
조회/빌드 속도에 영향을 줄 수 있는 파일(`PERF_SENSITIVE`)이 바뀌었는지만 보고
**턴 끝에 한 번** 알린다. 그러면 모델이 사용자에게 벤치 실행 여부를 묻는다.

```
server\.venv\Scripts\python.exe tests\bench_webreport.py --quick
```

- **속도 개선이 목적이었는지는 파일만 보고 알 수 없다.** 그 판단은 모델 몫이다 —
  라벨 수정·기능 추가·버그 수정이었다면 묻지 않고 마친다.
- 같은 파일 집합에 대해 **한 번만** 뜨고, 위반이 있으면 위반 보고가 우선한다.
- 벤치는 임시 DB 격리라 운영 무접촉이고, 이전 실행 대비 회귀를 자동 판정한다.
  결과 해석 절차는 스킬 `webreport-bench` 참조.

검사 범위는 `web_report/` 와 `server/report/` 뿐이다. 그 밖의 파일을 고치는 세션에서는
가드가 아무 반응도 하지 않는다.

`--hook` 은 조각만 보므로 "추가 금지" 규칙만 본다. "짝으로 바꿔야 함"
(payload 구조 변경 ↔ `REPORT_SCHEMA_VERSION` 상향)이나 "삭제 금지"처럼 저장소 전체
맥락이 필요한 규칙은 `--stop`/`--diff` 담당이다.

## 오탐이 났을 때 — 면제

의도한 변경인데 막혔다면 그 줄 또는 **바로 윗줄**에 사유와 함께 면제를 단다.

```python
data = gzip.compress(b, compresslevel=6)  # perf-guard: allow R03-gzip-level (사유)
```

사유 없는 면제는 달지 말 것. 같은 규칙에 면제가 자꾸 붙으면 규칙이 틀린 것이므로
면제를 늘리지 말고 `_RULES` 의 `paths`/`pattern` 을 고쳐라.

## 규칙 추가 — 회귀 사후 조치의 표준 형태

성능 회귀나 malfunction 이 한 번 나면, 고치는 것으로 끝내지 말고 **같은 지뢰를 다시
밟지 못하게 규칙을 1개 추가한다.** [tools/perf_guard.py](../tools/perf_guard.py) 의
`_RULES` 에 dict 하나를 넣고 `id / kind / paths / pattern / why / doc` 을 채우면 된다.

넣은 뒤 반드시 두 가지를 확인한다.

```
python tools/perf_guard.py --scan-all     # 현재 코드가 위반 0건이어야 한다
python tests/test_perf_guard.py           # 규칙마다 위반/정상 샘플이 있어야 통과
```

`--scan-all` 에서 현재 코드가 걸리는 규칙은 **쓸 수 없다.** 좁히거나 버려라.
`tests/test_perf_guard.py` 는 샘플이 없는 규칙이 있으면 실패하므로, 규칙을 추가하면
`ADD_CASES` / `REMOVE_CASES` 에 위반·정상 샘플도 함께 넣어야 한다.

## 끄는 법

- 일시: `.claude/settings.json` 의 해당 훅 항목을 지운다.
- 전체: 설정에 `"disableAllHooks": true` (다른 훅까지 함께 꺼진다).

가드는 **fail-open** 이다 — 스크립트가 예외로 죽으면 조용히 통과시킨다. 가드가
깨졌을 때 작업 전체가 막히는 것보다 낫다는 판단이고, 그래서
`tests/test_perf_guard.py` 가 유일한 안전망이다.

## 가드가 못 잡는 것

정적 패턴 검사는 **알려진 지뢰를 다시 밟는 것**만 잡는다. 다음은 정규식으로 표현할 수
없어 사람·모델이 읽고 판단해야 한다.

- 콜드 판정은 single-flight 락 **밖**에서 → [docs/12](12_web_report_cache.md)
- `loader.clone_table` 의 df/data 공유 계약 (편집 경로는 `use_cache=False`)
- `dist_pack_store.load_chunk_items` 반환 dict 는 읽기 전용
- 대용량 payload 를 `/full` 에 싣지 말 것 (Map dies = map_deferred)
  → [docs/11](11_web_report_tabs.md)
- `Plotly.toImage` 는 canvas 오버레이 점을 담지 못한다

그리고 **새로운 종류의 성능 회귀**(알고리즘 복잡도 증가, 예상 못한 N+1)는 원리적으로
못 잡는다. 그건 실측 영역이다 — 그래서 `--stop` 이 턴 끝에
[tests/bench_webreport.py](../tests/bench_webreport.py) 실행을 제안한다(위 "턴 끝의 벤치
제안"). 가드가 조용하다고 해서 빨라졌다는 뜻은 아니다.

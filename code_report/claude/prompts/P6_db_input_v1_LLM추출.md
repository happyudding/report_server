# [Opus 구현 프롬프트 P6] db_input v1 — 자유서식 문서 LLM 추출 트랙

너는 `f:\COINAPI\report_server` 저장소에서 작업하는 Claude Code 다. 아래 지시를 그대로 수행하라.

## 선행 조건 (확인 후 착수)

- **P1(tools/precedent_import/paste_import.py)이 완료된 작업트리**여야 한다 — 이 프롬프트는
  그 파이프라인의 "추출" 앞단만 추가한다. 없으면 중단·보고.
- 사용자에게 **사내/승인된 OpenAI 호환 LLM endpoint** 준비 여부를 확인하라. 없으면 코드는
  작성하되 E2E 는 mock 테스트까지만 하고 그 사실을 보고하라.
  (⚠ 과거 평가 문서는 사내 데이터다 — 외부 SaaS endpoint 를 기본값으로 넣지 말 것.)

## 목표 (한 줄)

표가 아닌 자유서식 문서(보고서 문장, 메일 등)를 LLM 으로 db_input 20컬럼 rows JSON 으로
추출하고, P1 과 동일한 검증→검수→subprocess 적재 경로에 태운다.
eval_analyzer 의 미구현 스텁(`ai_extract.extract_rows_from_text`)을 **밖에서** 대체하는
구현이다 (03 문서 R-3 의 B 안 — eval_analyzer 는 계속 무수정).

## 먼저 읽어라 (필수)

1. `tools/precedent_import/paste_import.py` (P1 산출물) — CSV_COLUMNS/REQUIRED/어휘 상수·
   `local_checks`·`run_import_text` 를 **import 해 재사용** (상수 중복 정의 금지)
2. `eval_analyzer/db_input/ai_extract.py` — `validate_rows` 가 뒤에서 걸러줄 항목
   (LLM 환각이 DB 에 못 들어가는 이유), `extract_rows_from_text` 스텁의 계약
   ("import_csv 호환 rows 반환")
3. `code_report/claude/05_db_input_발전방향.md` §2 트랙 B — 설계 배경

## 불변 제약

- **`eval_analyzer/` 무수정. eval_analyzer import 금지** (P1 과 동일 — 적재는 subprocess).
- endpoint/모델/키는 전부 env — 코드에 URL·모델명 하드코딩 금지. env 이름은 엔진의
  `EVAL_LLM_*` 와 **다른 네임스페이스**를 써서 혼동을 막는다:
  `PRECEDENT_LLM_ENDPOINT` / `PRECEDENT_LLM_MODEL` / `PRECEDENT_LLM_API_KEY` /
  `PRECEDENT_LLM_TIMEOUT`(기본 60).
- HTTP 는 **stdlib `urllib.request`** 로 (requirements.txt 에 의존성 추가 금지 —
  꼭 필요하면 사용자 승인 후).
- LLM 출력은 신뢰하지 않는다: JSON 파싱 실패·스키마 밖 키·어휘 위반은 전부
  "BLOCKED 행 + 사람 수정" 흐름으로 — **자동 저장 절대 금지** (P1 과 달리 v1 은
  `--save` 전에 `--html` 검수 파일 생성을 강제하라).

## 만들 파일

`tools/precedent_import/llm_extract.py` + `test_llm_extract.py` (+ README.md 에 v1 섹션 추가)

## 구현 단계

### Step 1 — 추출 프롬프트 빌더 (핵심 — 아래 내용 그대로 담아라)

```python
def build_extract_prompt(chunk: str) -> str:
    """자유서식 텍스트 청크 → rows JSON 추출 지시 프롬프트.

    스키마·어휘를 프롬프트에 명시해 출력 공간을 좁힌다. 그래도 최종 방어선은
    validate_rows(subprocess)와 사람 검수다 — 프롬프트는 1차 필터일 뿐.
    """
    return f"""반도체 fail item 평가 기록 문서에서 선례 데이터를 추출해 JSON 만 출력하라.

출력 형식: {{"rows": [{{...}}]}} — 각 row 는 아래 20개 키만 사용(값 없으면 null):
{json.dumps(CSV_COLUMNS, ensure_ascii=False)}

규칙:
- 필수: product_name, product_type, family_product, item_name, value_type, bin.
  문서에서 확실히 알 수 없으면 null 로 두어라(추측 금지 — 사람이 검수 단계에서 채운다).
- product_type 은 MDDI|PDDI|PMIC|SECURITY|TCON 중 하나.
- family_product 허용값: MDDI[MX,AQUA,CHINA,MDDI_ETC] PMIC[SOC,MEMORY,DISPLAY,IF,PMIC_ETC]
  SECURITY[NFC_ESE,ESE,Contactless,SECU_ETC] PDDI[LCD,PDDI_IT,QDOLED,PDDI_ETC]
  TCON[TV,TCON_IT,TCON_ETC]. 애매하면 해당 제품군의 *_ETC.
- value_type: V|A|Hz|CODE|P_F|Ohm|Sec (단위에서 유추: mV/V→V, uA/mA→A, ohm→Ohm,
  코드/DAC/trim code→CODE, pass/fail→P_F).
- human_status: CRITICAL|MAJOR|MINOR|MONITOR|OK, root_cause_category:
  equipment|process|design|spec|unknown, outcome_action: retest|condition_change|
  trim_adjust|spec_release|dev_feedback|pa_feedback|false_fail|scrap|monitor|other,
  outcome_result: recovered_normal|improved|false_fail|confirmed_defective|
  inconclusive|pending|other — 문서 표현을 이 코드로 매핑하고, 매핑이 불확실하면 null.
- human_comment 에는 엔지니어의 분석/조치 문장을 원문 그대로(요약 금지) 담아라.
- fail 사례가 아닌 문장(인사말, 목차 등)에서 row 를 만들지 마라. 없으면 {{"rows": []}}.

문서:
---
{chunk}
---
JSON:"""
```

### Step 2 — LLM 호출 + 견고한 파싱

```python
def call_llm(prompt: str) -> str:
    """OpenAI 호환 chat/completions POST (stdlib urllib). env 미설정 시 명확한 에러."""
    endpoint = os.environ.get("PRECEDENT_LLM_ENDPOINT", "")
    model = os.environ.get("PRECEDENT_LLM_MODEL", "")
    if not endpoint or not model:
        raise RuntimeError("PRECEDENT_LLM_ENDPOINT / PRECEDENT_LLM_MODEL 를 설정하세요 "
                           "(사내 승인 endpoint 만 사용).")
    # payload: {"model": model, "messages":[{"role":"user","content": prompt}],
    #           "temperature": 0} + Authorization: Bearer <PRECEDENT_LLM_API_KEY>(있으면)
    # urllib.request.Request(..., timeout=PRECEDENT_LLM_TIMEOUT) → choices[0].message.content


def parse_rows_json(text: str) -> list[dict]:
    """LLM 응답 → rows. 코드펜스 제거 → 첫 '{'~마지막 '}' 구간 json.loads →
    {"rows":[...]} / [...] 둘 다 수용 → dict 아닌 원소 제거 → CSV_COLUMNS 밖 키 버림 →
    누락 키는 "" 채움. 파싱 자체가 실패하면 RuntimeError(원문 앞 500자 포함)."""
```

### Step 3 — 청크 분할 + 병합

- `split_chunks(text, max_chars=8000)`: 빈 줄(문단) 경계 우선 분할, 한 문단이 한도를
  넘으면 줄 단위로 강제 분할.
- `extract_rows_from_text(text) -> list[dict]`: 청크별 build→call→parse, rows 이어붙임.
  청크별 실패는 경고 수집 후 계속(전체 중단 금지), 마지막에 실패 청크 수 보고.

### Step 4 — CLI (`llm_extract.py` main)

```
python tools/precedent_import/llm_extract.py <원문.txt> [--out rows.json] [--html review.html]
    [--save --to-eval-db]
```
1. 추출 → rows JSON 저장 → `paste_import.local_checks` 경고 출력
2. `--html`(save 시 **필수** — 없으면 에러): P1 의 HTML 미리보기 재사용, 상단에
   "LLM 추출 결과 — 반드시 원문과 대조 검수" 경고 배너 + 청크별 실패 목록
3. `--save`: 어휘 위반 있으면 중단, 아니면 `paste_import.run_import_text(save=True, ...)`

### Step 5 — 테스트 (`test_llm_extract.py`, LLM 호출은 전부 mock)

- `parse_rows_json`: 코드펜스 응답 / `{"rows":[...]}` / 맨 배열 / 잡담 섞인 응답 / 스키마 밖 키
  제거 / 완전 실패 에러.
- `split_chunks`: 문단 경계 분할, 초대형 문단 강제 분할, 전체 순서 보존.
- `call_llm`: env 미설정 에러 문구. (monkeypatch 로 urlopen 대체)
- E2E(mock): 가짜 응답 2청크 → rows 병합 → local_checks 경고 → rows.json 산출.

## 검증

1. `python -m pytest tools/precedent_import/ -q` 전부 통과.
2. endpoint 가 있으면: 짧은 실제 문서 1건으로 추출→HTML 검수→기본 output 모드 적재까지 E2E,
   없으면 mock E2E 로그로 대체하고 보고에 명시.
3. `git status` — 변경이 tools/precedent_import/ 아래 신규/README 뿐, eval_analyzer/ 무변경.

## 완료 기준

- [ ] LLM 무신뢰 원칙 관철: 파싱 실패·어휘 위반이 저장을 통과할 수 없음 (테스트로 증명)
- [ ] --save 는 --html 검수 파일 생성과 함께만 가능
- [ ] env 만으로 endpoint 교체 가능, 하드코딩 0
- [ ] 완료 보고: 변경 파일 / 테스트 로그 / (실 endpoint 유무에 따른) E2E 결과 /
      후속: R-3 계약 문서에 이 도구를 공식 추출기로 기재 제안

## 하지 말 것

- eval_analyzer import·수정, 외부 SaaS 기본 endpoint, 새 pip 의존성 무단 추가,
  LLM 출력 자동 저장(검수 생략), P1 상수의 중복 정의(반드시 import).

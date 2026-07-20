# [Opus 구현 프롬프트 P1] db_input v0 — "복붙 → 자동 매핑 → 검증 → 선례 DB 적재" 로컬 도구

너는 `f:\COINAPI\report_server` 저장소에서 작업하는 Claude Code 다. 아래 지시를 그대로 수행하라.

## 목표 (한 줄)

엔지니어가 **기존 평가 문서(주로 엑셀 표)를 복사해 텍스트 파일로 붙여넣으면**, 헤더를 자동
매핑하고 db_input 스키마 rows 로 변환·검증·미리보기 후, 기존 db_input CLI 를 **subprocess** 로
호출해 선례 DB 에 적재하는 독립 로컬 도구 `tools/precedent_import/` 를 만든다. LLM 은 쓰지 않는다.

## 먼저 읽어라 (필수 — 코드 작성 전)

1. `eval_analyzer/db_input/CLAUDE.md` — 적재기 규약 (⚠ config 덮어쓰기 주의 포함)
2. `eval_analyzer/db_input/import_csv.py` — CSV 컬럼·필수값·멱등 적재 로직
3. `eval_analyzer/db_input/import_text.py` — 우리가 subprocess 로 부를 CLI (플래그 확인)
4. `eval_analyzer/db_input/ai_extract.py` — `CSV_COLUMNS`(20개)·`validate_rows` 검증 내용
5. `eval_analyzer/db_input/template_example.csv` — 입력 예시
6. `code_report/claude/05_db_input_발전방향.md` — 이 도구의 설계 배경 (§2 파이프라인)

## 불변 제약 (위반 금지)

- **`eval_analyzer/` 하위 파일 수정 금지.** 신규 파일은 전부 `tools/precedent_import/` 아래에만.
- **이 도구는 eval_analyzer 코드를 import 하지 않는다.** 검증·적재는 반드시
  `python eval_analyzer/db_input/import_text.py` **subprocess** 호출로 위임한다.
  이유 2가지: ① eval_engine import 허용 지점은 web_report 2곳뿐(docs/13 §2) —
  세 번째 접점을 만들지 않는다. ② `import_csv._import_group` 이 실행 중
  `config.DB_PATH` 를 덮어쓰는 단발 프로세스 전제 코드라 프로세스 격리가 안전하다.
- 서버(Flask) 코드는 이 프롬프트 범위에서 건드리지 않는다 (admin 통합은 별도 v2).
- 콘솔 출력: `sys.stdout.reconfigure(encoding="utf-8")` 가드 필수 (한국어 Windows cp949 콘솔,
  import_csv.py:34-37 과 동일 패턴). 파일 읽기는 `encoding="utf-8-sig"` (엑셀 저장 BOM 대응).

## 배경 지식 (이 코드베이스에서 검증된 사실 — 재조사 불필요)

- 적재 CLI: `python eval_analyzer/db_input/import_text.py --json <rows.json> [--preview]
  [--write-csv <out.csv>] [--save] [--to-eval-db]`
  - `--save` 는 검증 실패 행이 하나라도 있으면 저장 거부(exit 1) — 우리가 재검증할 필요 없음.
  - `--to-eval-db` 없으면 제품군별 `eval_analyzer/db_input/output/<pt>_<fp>.db` 분리 적재,
    있으면 `EVAL_DB_PATH` env 가 가리키는 운영 eval.db 하나로 통합 적재.
  - JSON 은 `[{...}]` 또는 `{"rows":[...]}` 둘 다 허용, None 값은 CLI 가 "" 로 정규화한다.
- rows 스키마(20컬럼, `ai_extract.CSV_COLUMNS` 와 동일해야 함):
  `product_name, product_type, family_product, lot_id, wafer_number, revision, item_name,
  value_type, bin, USL, LSL, average, stdev, human_comment, session_id, human_status,
  root_cause_category, outcome_action, outcome_condition, outcome_result`
- 필수 6개: `product_name, product_type, family_product, item_name, value_type, bin`.
- CLI 의 `validate_rows` 가 검증하는 것: 필수 6개 존재, 숫자 컬럼(bin/revision/USL/LSL/
  average/stdev/wafer_number) float 변환 가능, product_type↔family_product 조합
  (product_taxonomy), outcome_action/result 어휘(outcome_taxonomy).
- CLI 가 검증하지 **않는** 것(도구가 자체 경고해야 함): `value_type` 어휘, `human_status`,
  `root_cause_category`. 허용값(정본: eval_analyzer docs/DB_SCHEMA.md §10):
  - value_type: `V | A | Hz | CODE | P_F | Ohm | Sec`
  - human_status: `CRITICAL | MAJOR | MINOR | MONITOR | OK`
  - root_cause_category: `equipment | process | design | spec | unknown`
- product_type↔family_product 허용 조합(정본: eval_analyzer/eval_engine/rules/product_taxonomy.yaml):
  MDDI=[MX,AQUA,CHINA,MDDI_ETC] PMIC=[SOC,MEMORY,DISPLAY,IF,PMIC_ETC]
  SECURITY=[NFC_ESE,ESE,Contactless,SECU_ETC] PDDI=[LCD,PDDI_IT,QDOLED,PDDI_ETC]
  TCON=[TV,TCON_IT,TCON_ETC]
- outcome 어휘(정본: rules/outcome_taxonomy.yaml): action=`retest|condition_change|trim_adjust|
  spec_release|dev_feedback|pa_feedback|false_fail|scrap|monitor|other`,
  result=`recovered_normal|improved|false_fail|confirmed_defective|inconclusive|pending|other`.
- 적재는 멱등(case_id 자연키 upsert) — 같은 파일 재적재해도 중복 없음.

## 만들 파일

```
tools/precedent_import/
├── paste_import.py      # 메인 CLI (아래 핵심 코드)
├── README.md            # 사용법 (엔지니어용, 한국어 — 예시 포함)
└── test_paste_import.py # pytest (파싱·매핑·행 변환·로컬 검증 단위 테스트)
```

## 구현 단계

### Step 1 — `paste_import.py` 골격과 상수

아래 핵심 코드를 기반으로 작성하라 (그대로 시작점으로 써도 좋고, 스타일은 저장소 관례에 맞춰라.
docstring 은 한국어, 상단에 사용법):

```python
"""과거 평가 문서(엑셀 복붙 표) → db_input rows JSON → 선례 DB 적재 도구 (v0, LLM 불필요).

사용법:
  1) 엑셀에서 표 범위를 복사해 paste.txt 로 저장 (탭 구분 텍스트가 됨)
  2) python tools/precedent_import/paste_import.py paste.txt --preview
     → 헤더 매핑 결과 + 검증 결과 확인 (미매핑 컬럼은 --map 으로 수동 지정)
  3) python tools/precedent_import/paste_import.py paste.txt --save [--to-eval-db]

옵션:
  --defaults k=v ...   모든 행에 공통 적용할 값 (예: product_type=PMIC family_product=SOC)
  --map 원본=표준 ...   헤더 수동 매핑 추가 (예: "품 명=product_name")
  --out rows.json      변환 rows JSON 저장 경로 (기본: 입력파일 옆 <이름>.rows.json)
  --html <path>        검수용 HTML 미리보기 생성

적재는 eval_analyzer/db_input/import_text.py 를 subprocess 로 호출한다 — 이 도구는
eval_analyzer 코드를 import 하지 않는다 (docs/13 §2 접점 2곳 규약 + config 덮어쓰기 격리).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parents[2]
IMPORT_TEXT = REPO / "eval_analyzer" / "db_input" / "import_text.py"

# ai_extract.CSV_COLUMNS 미러 — subprocess 경계라 import 하지 않는다.
# 드리프트해도 CLI 검증(필수 컬럼)이 잡아주지만, 스키마 변경 시 여기도 갱신할 것.
CSV_COLUMNS = [
    "product_name", "product_type", "family_product", "lot_id", "wafer_number",
    "revision", "item_name", "value_type", "bin", "USL", "LSL", "average", "stdev",
    "human_comment", "session_id", "human_status", "root_cause_category",
    "outcome_action", "outcome_condition", "outcome_result",
]
REQUIRED = ["product_name", "product_type", "family_product",
            "item_name", "value_type", "bin"]

# 현장 문서 헤더 → 표준 컬럼. 좌변은 _norm() 정규화 후 비교(공백/언더스코어/하이픈 무시,
# 소문자). 운영하며 늘려가는 사전 — 새 표기를 만나면 여기에 추가한다.
HEADER_SYNONYMS = {
    "product_name":        ["productname", "product", "품명", "제품명", "제품"],
    "product_type":        ["producttype", "제품군", "타입", "pt"],
    "family_product":      ["familyproduct", "family", "패밀리", "fp"],
    "lot_id":              ["lotid", "lot", "랏", "lot번호", "lotno"],
    "wafer_number":        ["wafernumber", "wafer", "웨이퍼", "wf", "waferno"],
    "revision":            ["revision", "rev", "리비전"],
    "item_name":           ["itemname", "item", "항목", "항목명", "테스트항목",
                            "testitem", "측정항목"],
    "value_type":          ["valuetype", "측정종류", "단위종류"],
    "bin":                 ["bin", "빈", "binno"],
    "USL":                 ["usl", "상한", "hilim", "maxspec", "spechigh"],
    "LSL":                 ["lsl", "하한", "lolim", "minspec", "speclow"],
    "average":             ["average", "avg", "평균", "mean"],
    "stdev":               ["stdev", "std", "표준편차", "sigma", "stddev"],
    "human_comment":       ["humancomment", "comment", "코멘트", "비고", "의견",
                            "조치내용", "분석코멘트", "분석내용", "코멘트내용"],
    "session_id":          ["sessionid", "세션"],
    "human_status":        ["humanstatus", "status", "판정", "등급", "판정등급"],
    "root_cause_category": ["rootcausecategory", "rootcause", "원인", "원인분류"],
    "outcome_action":      ["outcomeaction", "action", "조치", "조치구분"],
    "outcome_condition":   ["outcomecondition", "condition", "조건", "조치조건"],
    "outcome_result":      ["outcomeresult", "result", "결과", "조치결과"],
}

# CLI(validate_rows)가 검증하지 않는 어휘 — 도구가 자체 검사 (DB_SCHEMA §10 미러)
VALUE_TYPES = {"V", "A", "Hz", "CODE", "P_F", "Ohm", "Sec"}
HUMAN_STATUSES = {"CRITICAL", "MAJOR", "MINOR", "MONITOR", "OK"}
ROOT_CAUSES = {"equipment", "process", "design", "spec", "unknown"}


def _norm(s) -> str:
    return re.sub(r"[\s_\-]+", "", str(s or "").strip().lower())
```

### Step 2 — 표 텍스트 파싱 + 헤더 매핑 (핵심 함수)

```python
def parse_table_text(text: str) -> tuple[list[str], list[list[str]]]:
    """붙여넣은 표 텍스트 → (헤더, 데이터 행들).

    엑셀 범위 복사는 탭 구분이 표준이므로 첫 줄에 탭이 있으면 탭 구분으로 읽고,
    없으면 csv 로 읽는다. 앞뒤 빈 줄·전체가 빈 행은 버린다.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("입력이 비어 있습니다.")
    delimiter = "\t" if "\t" in lines[0] else ","
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = [[c.strip() for c in row] for row in reader]
    header, data = rows[0], rows[1:]
    data = [r for r in data if any(c for c in r)]
    if not data:
        raise ValueError("헤더만 있고 데이터 행이 없습니다.")
    return header, data


def map_headers(header: list[str], manual: dict[str, str] | None = None
                ) -> tuple[dict[int, str], list[str]]:
    """원본 헤더 → 표준 컬럼 매핑. 반환: ({열 index: 표준컬럼}, 미매핑 헤더 목록).

    우선순위: --map 수동 지정 > 표준 컬럼명 그대로 > HEADER_SYNONYMS.
    같은 표준 컬럼에 두 열이 매핑되면 에러(모호성은 사람이 풀어야 한다).
    """
    manual_norm = {_norm(k): v for k, v in (manual or {}).items()}
    syn = {}
    for std, alts in HEADER_SYNONYMS.items():
        syn[_norm(std)] = std
        for a in alts:
            syn[_norm(a)] = std
    mapping, unmapped, used = {}, [], {}
    for i, h in enumerate(header):
        key = _norm(h)
        std = manual_norm.get(key) or syn.get(key)
        if std is None:
            unmapped.append(h)
            continue
        if std not in CSV_COLUMNS:
            raise ValueError(f"--map 대상 컬럼이 스키마에 없음: {std!r}")
        if std in used:
            raise ValueError(f"헤더 중복 매핑: {used[std]!r} 와 {h!r} 가 모두 {std}")
        used[std] = h
        mapping[i] = std
    return mapping, unmapped
```

### Step 3 — rows 변환 + 도구 자체 검사 (핵심 함수)

```python
def build_rows(header, data, mapping, defaults: dict[str, str]) -> list[dict]:
    """데이터 행들 → db_input rows(dict 리스트). defaults 는 셀이 빈 컬럼에만 채운다."""
    bad_defaults = set(defaults) - set(CSV_COLUMNS)
    if bad_defaults:
        raise ValueError(f"--defaults 대상 컬럼이 스키마에 없음: {sorted(bad_defaults)}")
    rows = []
    for r in data:
        row = {col: "" for col in CSV_COLUMNS}
        for i, std in mapping.items():
            if i < len(r):
                row[std] = r[i].strip()
        for k, v in defaults.items():
            if not row.get(k):
                row[k] = v
        # 사람이 남긴 것이 하나도 없는 행(코멘트/판정/원인/조치 전부 공백)은 선례 가치가
        # 없으므로 제외하지 **않는다** — 통계 행도 case 로 쌓일 수 있음. 판단은 preview 에서.
        rows.append(row)
    return rows


def local_checks(rows: list[dict]) -> list[str]:
    """CLI 가 검증하지 않는 어휘 + 품질 경고. 반환: 경고 문자열 목록(빈 리스트면 통과).

    어휘 위반은 '경고'가 아니라 저장 전에 반드시 고쳐야 할 항목으로 출력하되,
    저장 차단 자체는 CLI(--save 의 validate_rows)가 한다 — 이중 차단으로 혼란 주지 않기.
    """
    warns = []
    n_comment = 0
    for i, row in enumerate(rows, start=1):
        vt = (row.get("value_type") or "").strip()
        if vt and vt not in VALUE_TYPES:
            warns.append(f"[{i}] value_type={vt!r} — 허용값 {sorted(VALUE_TYPES)}")
        hs = (row.get("human_status") or "").strip()
        if hs and hs not in HUMAN_STATUSES:
            warns.append(f"[{i}] human_status={hs!r} — 허용값 {sorted(HUMAN_STATUSES)}")
        rc = (row.get("root_cause_category") or "").strip()
        if rc and rc not in ROOT_CAUSES:
            warns.append(f"[{i}] root_cause_category={rc!r} — 허용값 {sorted(ROOT_CAUSES)}")
        if (row.get("human_comment") or "").strip():
            n_comment += 1
    if rows and n_comment == 0:
        warns.append("human_comment 가 있는 행이 0건 — 선례 인용에 쓰일 데이터가 없습니다. "
                     "코멘트 컬럼 매핑을 확인하세요 (05 문서 §4: 코멘트 없는 나열은 가치 낮음).")
    return warns
```

### Step 4 — subprocess 위임 + main

```python
def run_import_text(rows_json: Path, *, preview=False, write_csv=None,
                    save=False, to_eval_db=False) -> int:
    """검증/적재를 db_input CLI 에 위임 (프로세스 격리 — config 덮어쓰기 무해)."""
    cmd = [sys.executable, str(IMPORT_TEXT), "--json", str(rows_json)]
    if preview:
        cmd.append("--preview")
    if write_csv:
        cmd += ["--write-csv", str(write_csv)]
    if save:
        cmd.append("--save")
        if to_eval_db:
            cmd.append("--to-eval-db")
    proc = subprocess.run(cmd, cwd=str(REPO / "eval_analyzer"))
    return proc.returncode
```

`main()` 흐름 (argparse):
1. 입력 파일 읽기(`utf-8-sig`) → `parse_table_text` → `map_headers`
2. **매핑 리포트 출력**: 매핑된 컬럼 표 + 미매핑 헤더(있으면 "→ --map '원본=표준' 으로 지정"
   안내) + REQUIRED 중 매핑도 defaults 도 없는 컬럼은 **여기서 즉시 에러** (CLI 까지 안 가고 빠른 실패)
3. `build_rows` → `local_checks` 경고 출력 → rows JSON 저장 (`--out`, 기본 `<입력>.rows.json`,
   `ensure_ascii=False, indent=2`)
4. `--html` 지정 시 검수용 HTML 생성 (Step 5)
5. `--preview`(기본: save 가 아니면 항상) → `run_import_text(preview=True)`
6. `--save` → local_checks 의 **어휘 위반**이 있으면 저장 중단(경고와 구분해 종료 코드 1),
   없으면 `run_import_text(save=True, to_eval_db=...)` — CLI 의 exit code 를 그대로 반환

### Step 5 — 검수용 HTML 미리보기 (`--html`)

자기완결 HTML 1파일(외부 리소스 금지): rows 를 표로, REQUIRED 빈 셀은 빨강, 어휘 위반 셀은
주황, human_comment 있는 행 수/제품군별 행 수 요약을 상단에. 미려할 필요 없음 — 검수 가능하면 됨.

### Step 6 — `test_paste_import.py` (pytest)

최소 케이스 (eval_analyzer 를 import 하지 않고 도는 순수 단위 테스트):
- 탭 구분/콤마 구분 파싱, 빈 행 제거
- 헤더 시노님 매핑(한국어 "품명"→product_name), 수동 --map 우선, 중복 매핑 에러
- defaults 적용(빈 셀만), 스키마 밖 defaults 에러
- local_checks: value_type 오탈자 검출, human_comment 0건 경고
- REQUIRED 누락 시 main 조기 실패 (매핑+defaults 어느 쪽에도 없을 때)

### Step 7 — `tools/precedent_import/README.md`

엔지니어용 사용법: ① 엑셀 복사→paste.txt ② preview ③ 미매핑 헤더 --map 지정 예시
④ 공통값 --defaults 예시 ⑤ --save --to-eval-db (운영 반영은 `EVAL_DB_PATH` env 를
`config.REPORT_EVAL_DB_PATH` 실제 경로로 지정하고 실행 — 예시 명령 포함) ⑥ 재실행해도
중복 안 쌓임(멱등) ⑦ family_product 는 taxonomy 어휘(IF/Contactless/SECU_ETC/PDDI_IT 등
현장 표기와 다름 — 허용 조합 표 수록).

## 검증 (직접 실행해서 확인하라)

1. `python -m pytest tools/precedent_import/test_paste_import.py -q` 전부 통과.
2. E2E: 임시 폴더에 샘플 paste.txt 를 만들어(탭 구분, 한국어 헤더 "품명/항목/빈/코멘트" 등
   + 일부러 미매핑 헤더 1개, value_type 오탈자 1개 포함) —
   a) preview 실행 → 매핑 리포트/경고/CLI 검증 출력 확인,
   b) 오탈자 수정 후 `--save` (기본 output 모드 — `eval_analyzer/db_input/output/<pt>_<fp>.db` 생성 확인),
   c) 같은 명령 재실행 → 건수 불변(멱등) 확인:
      `python -c "import sqlite3;c=sqlite3.connect(r'eval_analyzer/db_input/output/PMIC_SOC.db');print(c.execute('select count(*) from fail_case').fetchone())"`
   d) 테스트 산출물(paste.txt, rows.json, output/*.db 중 테스트로 만든 것) 정리 —
      단 `output/` 에 **기존에 있던 파일은 절대 지우지 마라**.
3. `git status` 로 변경이 `tools/precedent_import/` 아래 신규 파일뿐인지 확인.

## 완료 기준 (Definition of Done)

- [ ] `eval_analyzer/` 무수정 (git status 로 증명)
- [ ] pytest 통과 + 위 E2E 시나리오 실제 실행 로그 제시
- [ ] 멱등성 재실행 확인
- [ ] README.md 에 엔지니어가 혼자 따라할 수 있는 사용법
- [ ] 완료 보고: 변경 파일 목록 / 검증 로그 / 남은 것(예: 시노님 사전은 운영하며 보강)

## 하지 말 것

- eval_analyzer import (subprocess 만), 서버 코드 수정, LLM 호출(그건 P6),
  rows 스키마 임의 확장, 20컬럼 이름 변경.
- 대량 실데이터 적재는 이 프롬프트에서 하지 않는다 — 도구 완성까지만
  (실적재는 R-1 선례 인용 상한 반영 후 운영자가 실행).

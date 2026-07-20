# [Opus 구현 프롬프트 P5] admin Eval DB 탭 확장 (E-5) — 커버리지·라벨 검수

너는 `f:\COINAPI\report_server` 저장소에서 작업하는 Claude Code 다. 아래 지시를 그대로 수행하라.

## 목표 (한 줄)

관리자 대시보드(/pe/admin-…)의 기존 Eval DB 탭에 ① 제품군별 **정답지 커버리지** 표
② **라벨 검수**(품질 태깅·개별 삭제) ③ **선례 활용 현황** 위젯을 추가해,
쌓이는 선례 데이터의 양과 질을 한 화면에서 관리할 수 있게 한다.

## 먼저 읽어라 (필수)

1. `server/admin_panel/eval_admin.py` **전체** — 이 프롬프트의 중심 파일.
   `overview`/`list_labels`/`delete_cases`/`reexport` 패턴과
   "eval_engine 은 여기서 import 하지 않는다(접점 2곳 규약) — 커넥션은
   eval_export.open_conn 경유, 조회/삭제는 직접 SQL" 원칙(파일 docstring)을 그대로 따른다
2. `server/admin_panel/` 의 라우트·템플릿 구조 — Eval DB 탭이 위 함수들을 어떻게 노출하는지
   (기존 탭 UI 패턴을 그대로 재사용)
3. eval DB 스키마: `label`(label_id, case_id, human_status, root_cause_category,
   human_comment, labeler, reviewer, label_quality, created_at …), `fail_case`,
   `product_master`, `case_outcome`, `eval_precedent`(P4 이후에만 채워짐)
4. `code_report/claude/06_종합_로드맵.md` §4 — 이 위젯들이 재는 KPI 정의

## 불변 제약

- **`eval_analyzer/` 무수정. eval_engine import 금지** — 모든 접근은
  `eval_export.open_conn(create=False)` + 직접 SQL (기존 파일 원칙).
- 조회 함수는 DB 파일 부재 시 `{"exists": False, ...}` 로 조용히 동작 (기존 관례).
- 쓰기는 label 범위만(label_quality 갱신, label 행 삭제) — **fail_case 삭제는 기존
  delete_cases 가 담당하므로 중복 구현 금지.** case_outcome 은 label 삭제 시 연결분만 정리.
- web_report/ 파일은 이 프롬프트에서 건드리지 않는다.

## 구현 단계

### Step 1 — `eval_admin.py`: 커버리지 집계

```python
def coverage() -> dict:
    """제품군(product_type, family_product)별 정답지 커버리지 — E-5/KPI.

    cases=fail_case 수, with_comment=human_comment 있는 case,
    with_status=human_status 있는 case, with_outcome=case_outcome 있는 case.
    """
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return {"exists": False, "rows": []}
    try:
        rows = conn.execute("""
            SELECT pm.product_type, pm.family_product,
                   COUNT(DISTINCT fc.case_id) AS cases,
                   COUNT(DISTINCT CASE WHEN l.human_comment IS NOT NULL
                         AND l.human_comment <> '' THEN fc.case_id END) AS with_comment,
                   COUNT(DISTINCT CASE WHEN l.human_status IS NOT NULL
                         AND l.human_status <> '' THEN fc.case_id END) AS with_status,
                   COUNT(DISTINCT co.case_id) AS with_outcome
            FROM fail_case fc
            JOIN product_master pm ON pm.product_name = fc.product_name
            LEFT JOIN label l ON l.case_id = fc.case_id
            LEFT JOIN case_outcome co ON co.case_id = fc.case_id
            GROUP BY pm.product_type, pm.family_product
            ORDER BY pm.product_type, pm.family_product""").fetchall()
        return {"exists": True, "rows": [dict(r) for r in rows]}
    finally:
        conn.close()
```

### Step 2 — 라벨 검수 액션

```python
_ALLOWED_QUALITY = ("manual", "reviewed", "low")   # 검수 상태 어휘 (label_quality 컬럼 재사용)

def set_label_quality(label_ids, quality: str) -> dict:
    """검수 표시 — reviewed(확인됨)/low(저품질, 선례로 부적합 표시)/manual(원복)."""
    # 검증: quality in _ALLOWED_QUALITY, label_ids 정수 리스트.
    # UPDATE label SET label_quality=? WHERE label_id IN (...) — 건수 반환, 커밋/롤백 관례는
    # delete_cases 를 따른다.

def delete_labels(label_ids) -> dict:
    """label 행 단위 삭제 (case 는 유지 — 선례검색에서 자동 후순위로 밀림).

    연결 정리 순서: 해당 label 을 참조하는 case_outcome(label_id 일치)을 먼저 삭제 후 label 삭제.
    """
```

### Step 3 — `list_labels` 확장

- 파라미터 `quality`(선택) 추가 → WHERE 에 `l.label_quality=?` 필터.
- SELECT 에 `l.human_status, l.root_cause_category` 추가 (검수 화면에서 정답지 여부가 보이게).
- 기존 호출부(라우트) 하위호환 유지 — 새 파라미터는 기본 None.

### Step 4 — 선례 활용 현황 (P4 이후 데이터)

```python
def precedent_stats() -> dict:
    """선례 인용 현황 — eval_precedent 는 P4(features 축적) 활성 후에만 쌓인다.

    반환: {"exists":.., "evaluations": n, "with_precedent": n, "hit_rate": 0.0~1.0|None,
           "top_cited": [{case_id, item, n_cited}, ...] (상위 10)}
    """
```
- `evaluations` = evaluation 행 수, `with_precedent` = eval_precedent 가 1건 이상인 eval_id 수,
  `hit_rate` = 나눗셈(분모 0 이면 None). `top_cited` 는 `eval_precedent.precedent_case_id`
  GROUP BY 상위 10 에 `item_master.item_name_raw` 조인.
- 테이블이 비어 있으면 0/None 으로 — "P4 활성화 전" 안내 문구는 템플릿에서.

### Step 5 — 탭 UI (기존 패턴 재사용)

admin_panel 의 기존 Eval DB 탭 라우트/템플릿을 찾아:
- 상단에 **커버리지 표** (coverage rows — with_status/cases 비율에 % 표기),
- 라벨 목록에 quality 필터 셀렉트 + 행별 [reviewed]/[low]/[삭제] 버튼
  (삭제는 확인창 — 기존 delete_cases UI 관례),
- **선례 활용 카드** (hit_rate — 데이터 없으면 "P4 features 축적 활성화 후 표시"),
- 기존 recent_failures 배지·reexport 버튼은 그대로 유지.
admin 인증/CSRF 등 기존 탭이 쓰는 가드를 동일하게 적용하라 (새 보안 경로 발명 금지).

## 검증

1. eval DB 가 없는 환경 → 탭이 exists:False 안내로 조용히 동작 (500 없음).
2. P1/P2 로 쌓인 테스트 데이터가 있는 DB → coverage 수치가 sqlite 직접 쿼리와 일치:
   ```
   python -c "import sqlite3;c=sqlite3.connect(r'DB/pe/report/eval/eval.db');print(c.execute('select count(distinct case_id) from label where human_status is not null and human_status<>\'\'').fetchone())"
   ```
3. set_label_quality → list_labels(quality="reviewed") 필터 동작,
   delete_labels → label 사라지고 fail_case 는 남는 것 확인.
4. pytest: eval_admin 용 테스트가 있으면 확장, 없으면 `tests/test_eval_admin.py` 신설
   (임시 eval DB 픽스처 — `REPORT_EVAL_DB_PATH` env 격리는 tests/test_eval_export.py:30 관례).
5. `git status` — 변경이 server/admin_panel/(+tests/) 범위, eval_analyzer/ 무변경.

## 완료 기준

- [ ] 커버리지/검수/선례 위젯이 실제 탭에서 렌더 (스크린샷 또는 응답 로그)
- [ ] delete_labels 가 case 를 지우지 않음 (증명 쿼리)
- [ ] DB 부재·빈 DB 에서 무오류
- [ ] pytest 통과, eval_analyzer/ 무수정
- [ ] 완료 보고: 변경 파일 / 검증 로그 / KPI 매핑(06 문서 표의 어떤 KPI 가 이제 보이는지)

## 하지 말 것

- eval_engine import, fail_case/마스터 삭제 로직 신설, label_quality 어휘 확장(3종 고정),
  새 인증 경로 발명, web_report/ 수정.

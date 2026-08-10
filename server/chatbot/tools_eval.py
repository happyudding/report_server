"""eval.db 기반 조회 툴 — item 축(마스터/alias/케이스/코멘트).

report.db 에는 **item 축이 없다**: `report_analysis_summary.item_name` 은 실제로 bin 번호
문자열이고(server/upload_xlsx.py:141), web_report 세션은 그 테이블을 쓰지도 않는다.
item 이름으로 과거를 뒤지려면 eval.db 의 `item_master`/`item_alias`/`fail_case` 가 필요하다.

eval_engine 을 import 하지 않는다(불변 규칙 #8) — `eval_store.query` 로 SELECT 만 한다.
스키마 계약(테이블·컬럼)의 정본은 `eval_analyzer/eval_engine/store.py` 의 SCHEMA 다.

⚠ 이 DB 의 실측 데이터는 외부 담당자 환경에 있다. 개발 PC 에는 파일이 없는 것이 정상이며,
그때 모든 함수는 빈 결과 + `db_available: False` 를 돌려준다(예외 아님).
"""
from __future__ import annotations

from . import eval_store

# 케이스별 "최신 평가" 상관 서브쿼리 — 같은 case 에 여러 engine/model 판정이 쌓이므로
# 항상 최신 1건만 본다 (eval_analyzer/chatbot_prototype/queries.py 와 같은 관례).
_LATEST_EVAL = ("SELECT MAX(e2.eval_id) FROM evaluation e2 WHERE e2.case_id = fc.case_id")
_LATEST_LABEL = ("SELECT MAX(l2.label_id) FROM label l2 WHERE l2.case_id = fc.case_id")


def _envelope(rows, key):
    return {key: rows, "db_available": eval_store.available(),
            "db_path": str(eval_store.db_path())}


# ── 1. item 후보 검색 ────────────────────────────────────────────────────────
def search_item_candidates(item_keyword, *, product_type=None, family_product=None,
                           limit=20):
    """item 이름의 일부만 알 때 후보를 찾는다.

    언제 쓰나: "SGM 들어가는 항목", "LDO 관련 item", "이름에 PLL 들어간 항목" 처럼
    사용자가 **전체 이름을 기억하지 못할 때**. item 축 질문의 **첫 호출**이다.
    언제 쓰지 않나: 이슈의 상세 이력·close 내용을 바로 얻으려 할 때 — 이 함수는 후보와
    건수만 준다. 그다음 `get_item_history` 를 호출한다.

    item_keyword: 사용자가 말한 약어/부분 문자열 그대로 넣는다(확장·번역하지 않는다).
    item_canonical·item_name_raw·item_alias 3곳을 모두 부분일치로 본다.
    """
    kw = f"%{str(item_keyword or '').strip()}%"
    where = ["(im.item_canonical LIKE ? OR im.item_name_raw LIKE ?"
             " OR im.item_id IN (SELECT item_id FROM item_alias WHERE raw_name LIKE ?))"]
    params = [kw, kw, kw]
    if family_product:
        where.append("pm.family_product = ?")
        params.append(family_product)
    if product_type:
        where.append("pm.product_type = ?")
        params.append(product_type)
    sql = f"""
        SELECT im.item_id, im.item_canonical, im.item_name_raw, im.category_major,
               im.category_mid, im.value_type, im.unit,
               COUNT(DISTINCT fc.case_id)      AS cases,
               COUNT(DISTINCT fc.product_name) AS products,
               GROUP_CONCAT(DISTINCT fc.product_name) AS product_list,
               MAX(COALESCE(fc.updated_at, fc.created_at)) AS last_seen_at
        FROM item_master im
        LEFT JOIN fail_case fc     ON fc.item_id = im.item_id
        LEFT JOIN product_master pm ON pm.product_name = fc.product_name
        WHERE {' AND '.join(where)}
        GROUP BY im.item_id
        ORDER BY cases DESC, last_seen_at DESC
        LIMIT ?
    """
    params.append(int(limit))
    rows = eval_store.query(sql, params)
    for r in rows:
        products = str(r.pop("product_list", "") or "")
        r["products_sample"] = [p for p in products.split(",") if p][:10]
    return _envelope(rows, "items")


# ── 2. item 과거 이력 ────────────────────────────────────────────────────────
def get_item_history(item_canonical, *, family_product=None, product_type=None,
                     exact=True, limit=20):
    """한 item 의 과거 평가 이력을 시간순으로 돌려준다.

    언제 쓰나: `search_item_candidates` 로 item 을 특정한 뒤 "예전에 어떻게 됐었지?"에
    답할 때. 제품·lot·bin·cpk·수율·엔진 판정·사람 코멘트를 한 행에 모아 준다.
    언제 쓰지 않나: 특정 보고서 1건의 close 내용을 물을 때 —
    `tools_report.get_session_issues` 가 Status(Open/Close)를 가진 유일한 소스다
    (Status 는 eval.db 로 export 되지 않는다).

    exact=False 면 item_canonical 부분일치.
    반환 행의 session_id 는 report.db 세션 링크다(근거 표기용).
    """
    name = str(item_canonical or "").strip()
    where = ["im.item_canonical " + ("= ?" if exact else "LIKE ?")]
    params = [name if exact else f"%{name}%"]
    if family_product:
        where.append("pm.family_product = ?")
        params.append(family_product)
    if product_type:
        where.append("pm.product_type = ?")
        params.append(product_type)
    sql = f"""
        SELECT fc.case_id, fc.product_name, fc.lot_id, fc.wafer_number, fc.bin,
               fc.revision, fc.item_class,
               im.item_canonical, im.value_type, im.unit,
               pm.product_type, pm.family_product,
               rm.cpk, rm.mean, rm.stdev, rm."yield", rm.fail_count, rm.total_count,
               ev.status AS engine_status, ev.confidence, ev.comment AS engine_comment,
               lb.human_comment, lb.human_status, lb.labeler, lb.reviewer,
               ir.session_id, ir.analysis_key, ir.source_file,
               COALESCE(ir.created_at, fc.created_at) AS occurred_at
        FROM fail_case fc
        JOIN item_master im          ON im.item_id = fc.item_id
        LEFT JOIN product_master pm  ON pm.product_name = fc.product_name
        LEFT JOIN run_case rc        ON rc.case_id = fc.case_id
        LEFT JOIN ingest_run ir      ON ir.run_id = rc.run_id
        LEFT JOIN raw_metrics rm     ON rm.case_id = fc.case_id AND rm.run_id = rc.run_id
        LEFT JOIN evaluation ev      ON ev.eval_id = ({_LATEST_EVAL})
        LEFT JOIN label lb           ON lb.label_id = ({_LATEST_LABEL})
        WHERE {' AND '.join(where)}
        ORDER BY occurred_at DESC
        LIMIT ?
    """
    params.append(int(limit))
    return _envelope(eval_store.query(sql, params), "history")


# ── 3. 유사 사례 ────────────────────────────────────────────────────────────
def search_similar_cases(item_canonical, *, family_product=None, limit=10):
    """같은 성격(value_type + category_major)의 **다른 item** 사례를 찾는다.

    언제 쓰나: "비슷한 불량 사례 있었어?" 처럼 같은 item 이 아니라 **유형이 닮은** 과거를
    물을 때. 사람 코멘트가 남아 있는 케이스를 우선한다(조치 이력이 있는 것이 쓸모 있다).
    언제 쓰지 않나: 같은 item 의 이력을 원할 때 — `get_item_history`.

    구현 축은 eval_engine.store.search_precedents 와 동일하다(value_type + family).
    의미 기반(벡터) 검색은 아직 없다 — 표현이 다른 사례는 못 찾는다.
    """
    name = str(item_canonical or "").strip()
    where = ["im.value_type IS NOT NULL",
             "im.value_type = (SELECT value_type FROM item_master"
             "                 WHERE item_canonical = ? LIMIT 1)",
             "im.item_canonical <> ?",
             "lb.human_comment IS NOT NULL AND TRIM(lb.human_comment) <> ''"]
    params = [name, name]
    if family_product:
        where.append("pm.family_product = ?")
        params.append(family_product)
    sql = f"""
        SELECT fc.case_id, fc.product_name, fc.lot_id, fc.bin, fc.item_class,
               im.item_canonical, im.value_type, im.category_major,
               pm.product_type, pm.family_product,
               rm.cpk, rm."yield",
               ev.status AS engine_status,
               lb.human_comment, lb.labeler,
               ir.session_id,
               COALESCE(ir.created_at, fc.created_at) AS occurred_at
        FROM fail_case fc
        JOIN item_master im          ON im.item_id = fc.item_id
        LEFT JOIN product_master pm  ON pm.product_name = fc.product_name
        LEFT JOIN run_case rc        ON rc.case_id = fc.case_id
        LEFT JOIN ingest_run ir      ON ir.run_id = rc.run_id
        LEFT JOIN raw_metrics rm     ON rm.case_id = fc.case_id AND rm.run_id = rc.run_id
        LEFT JOIN evaluation ev      ON ev.eval_id = ({_LATEST_EVAL})
        LEFT JOIN label lb           ON lb.label_id = ({_LATEST_LABEL})
        WHERE {' AND '.join(where)}
        ORDER BY occurred_at DESC
        LIMIT ?
    """
    params.append(int(limit))
    return _envelope(eval_store.query(sql, params), "similar")


# ── 4. 코멘트 전문 검색 (보조) ───────────────────────────────────────────────
def search_comments(keyword, *, family_product=None, limit=20):
    """사람 코멘트(PTE/개발 병합 텍스트) 본문을 부분일치로 찾는다.

    언제 쓰나: 사용자가 item 이 아니라 **현상·조치 표현**을 기억할 때
    ("trim 순서 바꿨던 거", "ripple 얘기 있던 항목"). 의미 검색이 아니라 문자열 검색이라
    표현이 정확히 겹쳐야 걸린다.
    """
    kw = f"%{str(keyword or '').strip()}%"
    where = ["lb.human_comment LIKE ?"]
    params = [kw]
    if family_product:
        where.append("pm.family_product = ?")
        params.append(family_product)
    sql = f"""
        SELECT lb.label_id, lb.human_comment, lb.labeler, lb.reviewer, lb.created_at,
               fc.case_id, fc.product_name, fc.lot_id, fc.bin,
               im.item_canonical, pm.product_type, pm.family_product,
               ir.session_id
        FROM label lb
        JOIN fail_case fc            ON fc.case_id = lb.case_id
        JOIN item_master im          ON im.item_id = fc.item_id
        LEFT JOIN product_master pm  ON pm.product_name = fc.product_name
        LEFT JOIN run_case rc        ON rc.case_id = fc.case_id
        LEFT JOIN ingest_run ir      ON ir.run_id = rc.run_id
        WHERE {' AND '.join(where)}
        ORDER BY lb.created_at DESC
        LIMIT ?
    """
    params.append(int(limit))
    return _envelope(eval_store.query(sql, params), "comments")


# ── 5. 집계 (몇 건인가) ─────────────────────────────────────────────────────
# 축 이름 → SQL 식. **사용자 입력을 SQL 에 넣지 않으려고** 화이트리스트로만 매핑한다
# (LLM 이 고른 값이 그대로 들어오는 자리라 특히 중요하다).
STATS_AXES = {
    "status": "COALESCE(ev.status, '(판정없음)')",
    "product": "fc.product_name",
    "product_type": "COALESCE(pm.product_type, '(미등록)')",
    "family_product": "COALESCE(pm.family_product, '(미등록)')",
    "item": "im.item_canonical",
    "item_class": "fc.item_class",
    "bin": "CAST(fc.bin AS TEXT)",
}


def stats_summary(group_by="status", *, product_type=None, family_product=None,
                  status=None, limit=20):
    """"몇 건인가" 에 답하는 집계 — 축 하나로 fail_case 를 세어 많은 순으로 돌려준다.

    언제 쓰나: "PMIC 에서 MAJOR 몇 건이야?", "제품별로 fail case 얼마나 쌓였어?",
    "판정 분포 알려줘" 처럼 **목록이 아니라 숫자**를 묻는 질문.
    언제 쓰지 않나: 특정 item 의 과거 내용을 알고 싶을 때 — 그건 `get_item_history` 다.

    group_by: STATS_AXES 의 키. 모르는 값이면 ValueError (조용히 다른 축으로 세지 않는다).
    status 로 판정을 걸러 "MAJOR 만 제품별로" 같은 조합도 된다.
    """
    axis = STATS_AXES.get(str(group_by or "").strip())
    if axis is None:
        raise ValueError(f"group_by 는 {list(STATS_AXES)} 중 하나여야 합니다")
    where, params = [], []
    if product_type:
        where.append("pm.product_type = ?")
        params.append(product_type)
    if family_product:
        where.append("pm.family_product = ?")
        params.append(family_product)
    if status:
        where.append("ev.status = ?")
        params.append(str(status).upper())
    sql = f"""
        SELECT {axis} AS key, COUNT(*) AS count,
               MAX(fc.created_at) AS last_at
        FROM fail_case fc
        JOIN item_master im          ON im.item_id = fc.item_id
        LEFT JOIN product_master pm  ON pm.product_name = fc.product_name
        LEFT JOIN evaluation ev      ON ev.case_id = fc.case_id
                                    AND ev.eval_id = ({_LATEST_EVAL})
        {("WHERE " + " AND ".join(where)) if where else ""}
        GROUP BY {axis}
        ORDER BY count DESC, last_at DESC
        LIMIT ?
    """
    params.append(int(limit))
    out = _envelope(eval_store.query(sql, params), "groups")
    out["group_by"] = str(group_by).strip()
    out["total"] = sum(int(r.get("count") or 0) for r in out["groups"])
    return out

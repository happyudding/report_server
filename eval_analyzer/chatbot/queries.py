"""구조화 read-only 조회 함수 (챗봇 Tool 의 실체).

임의 SQL 생성 없이, 미리 정의한 파라미터 조회만 노출한다. 전부 SELECT.
langchain 비의존 — 이 계층은 langchain 없이도 단독 테스트 가능("data pipeline" 코어).

각 함수: 파라미터 → list[dict] / dict 반환. DB 없으면 빈 결과([] / {}).
"""
from eval_engine import store

from .db import DBNotFound, ro_conn

# evaluation.status controlled vocabulary (docs/DB_SCHEMA §10)
STATUS_VOCAB = ("CRITICAL", "MAJOR", "MINOR", "MONITOR")
GROUP_BY_COLS = {
    "status": "ev.status",
    "product": "fc.product_name",
    "product_type": "pm.product_type",
    "item_class": "fc.item_class",
}

# 케이스별 "최신 평가" 서브쿼리 (동일 case 의 여러 engine/model 판정 중 최근 1건)
_LATEST_EVAL = (
    "ev.eval_id = (SELECT MAX(e2.eval_id) FROM evaluation e2 "
    "WHERE e2.case_id = fc.case_id)"
)


def search_cases(product=None, item=None, status=None, item_class=None, limit=20):
    """fail_case 를 조건으로 검색 → 최신 평가(status/comment) 동봉.

    product: product_name 부분일치 / item: item_canonical 부분일치 /
    status: CRITICAL|MAJOR|MINOR|MONITOR / item_class: 정확일치.
    """
    where, params = [], []
    if product:
        where.append("fc.product_name LIKE ?"); params.append(f"%{product}%")
    if item:
        where.append("im.item_canonical LIKE ?"); params.append(f"%{item}%")
    if status:
        where.append("ev.status = ?"); params.append(status)
    if item_class:
        where.append("fc.item_class = ?"); params.append(item_class)
    sql = f"""
        SELECT fc.case_id, fc.product_name, pm.product_type, pm.family_product,
               im.item_canonical, fc.bin, fc.item_class,
               ev.status, ev.confidence, ev.comment, ev.created_at
        FROM fail_case fc
        JOIN item_master im ON im.item_id = fc.item_id
        LEFT JOIN product_master pm ON pm.product_name = fc.product_name
        LEFT JOIN evaluation ev ON ev.case_id = fc.case_id AND {_LATEST_EVAL}
        {('WHERE ' + ' AND '.join(where)) if where else ''}
        ORDER BY ev.created_at DESC, fc.created_at DESC
        LIMIT ?
    """
    params.append(int(limit))
    try:
        with ro_conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
    except DBNotFound:
        return []


def get_case_detail(case_id):
    """단일 case 전체 맥락: fail_case + 최신 evaluation + raw_metrics + signature + label/outcome."""
    try:
        with ro_conn() as c:
            base = c.execute("""
                SELECT fc.*, im.item_canonical, im.value_type, pm.product_type,
                       pm.family_product
                FROM fail_case fc
                JOIN item_master im ON im.item_id = fc.item_id
                LEFT JOIN product_master pm ON pm.product_name = fc.product_name
                WHERE fc.case_id = ?
            """, (case_id,)).fetchone()
            if base is None:
                return {}
            out = {"case": dict(base)}
            ev = c.execute("""
                SELECT * FROM evaluation WHERE case_id = ?
                ORDER BY eval_id DESC LIMIT 1
            """, (case_id,)).fetchone()
            out["evaluation"] = dict(ev) if ev else None
            out["metrics"] = [dict(r) for r in c.execute(
                "SELECT * FROM raw_metrics WHERE case_id = ?", (case_id,)).fetchall()]
            out["signatures"] = []
            if ev:
                out["signatures"] = [dict(r) for r in c.execute(
                    "SELECT signature, role, score FROM case_signature WHERE eval_id = ?",
                    (ev["eval_id"],)).fetchall()]
            out["labels"] = [dict(r) for r in c.execute(
                "SELECT * FROM label WHERE case_id = ? ORDER BY label_id DESC",
                (case_id,)).fetchall()]
            out["outcomes"] = [dict(r) for r in c.execute(
                "SELECT * FROM case_outcome WHERE case_id = ?", (case_id,)).fetchall()]
            return out
    except DBNotFound:
        return {}


def find_precedents(item_name, value_type=None, family_product=None):
    """선례검색 — eval_engine.store.search_precedents 그대로 위임(검증된 read 함수).

    value_type 미지정 시 item_name 으로 대표 value_type 을 추정(같은 canonical 의 첫 행).
    """
    if value_type is None:
        value_type = _infer_value_type(item_name)
        if value_type is None:
            return []
    return store.search_precedents(value_type, item_name, family_product=family_product)


def stats_summary(group_by="status"):
    """group_by 축으로 fail_case/평가 카운트. group_by ∈ {status, product, product_type, item_class}."""
    col = GROUP_BY_COLS.get(group_by)
    if col is None:
        raise ValueError(f"group_by must be one of {list(GROUP_BY_COLS)}")
    # evaluation join(행마다 MAX(eval_id) 상관 서브쿼리)은 ev 컬럼을 쓸 때만
    ev_join = (f"LEFT JOIN evaluation ev ON ev.case_id = fc.case_id AND {_LATEST_EVAL}"
               if col.startswith("ev.") else "")
    sql = f"""
        SELECT {col} AS key, COUNT(*) AS count
        FROM fail_case fc
        JOIN item_master im ON im.item_id = fc.item_id
        LEFT JOIN product_master pm ON pm.product_name = fc.product_name
        {ev_join}
        GROUP BY {col}
        ORDER BY count DESC
    """
    try:
        with ro_conn() as c:
            return [dict(r) for r in c.execute(sql).fetchall()]
    except DBNotFound:
        return []


def _infer_value_type(item_name):
    try:
        with ro_conn() as c:
            row = c.execute(
                "SELECT value_type FROM item_master WHERE item_canonical LIKE ? "
                "AND value_type IS NOT NULL LIMIT 1", (f"%{item_name}%",)).fetchone()
            return row["value_type"] if row else None
    except DBNotFound:
        return None

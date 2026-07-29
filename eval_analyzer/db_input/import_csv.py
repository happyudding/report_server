"""CSV -> eval.db(<product_type>_<family_product>.db) 수동 선례(precedent) 적재.

사용법:
  python db_input/import_csv.py <csv_path>                # 제품군별 output/<pt>_<fp>.db 로 분리 적재
  python db_input/import_csv.py <csv_path> --to-eval-db   # 운영 eval.db(config.DB_PATH) 하나로 통합 적재
                                                          # → evaluate() 의 search_precedents 가 바로 참조

CSV 포맷 2종 — **헤더로 자동 감지**한다.

1) 단순 포맷 (정식, template_example.csv):
     Product type, Family Product, unit, Item, comment          (5컬럼, 전부 필수)
   헤더는 대소문자·공백에 유연하다(strip+소문자+공백→'_' 로 정규화 후 비교).
   - `unit` 은 실측 단위 원문(VOLTS/HERTZ/AMPS/mA …) 을 그대로 적으면 되고, 엔진 어휘
     (V/A/Hz/CODE/Ohm/Sec/P_F)로 매핑해 저장한다. 매핑표는 엔진 UNIT_TO_VALUE_TYPE +
     EXTRA_UNIT_ALIASES. **모르는 단위가 하나라도 있으면 아무것도 적재하지 않고 중단**한다
     (행번호 + 원문 목록 출력 → alias 를 보강한 뒤 재실행). 빈 unit 은 엔진과 같이 P_F.
   - lot/wafer/bin/limit/통계가 없는 요약 선례이므로 case 는 다음 값으로 합성한다:
     product_name=`<Product type>_<Family Product>`, bin=0, lot/wafer 없음, revision=0.0.
     따라서 같은 (product_type, family_product, item) 은 **하나의 case 로 접힌다** —
     CSV 안에 같은 조합이 여러 번 나오면 뒤 행의 comment 가 이긴다(재실행도 동일하게 갱신).

2) 레거시 포맷 (하위호환, 20컬럼):
     product_name, product_type, family_product, lot_id, wafer_number, revision,
     item_name, value_type, bin, USL, LSL, average, stdev, human_comment, session_id,
     human_status, root_cause_category, outcome_action, outcome_condition, outcome_result
     (뒤 5개는 선택 — human_status/root_cause 는 label 로, outcome_* 는 case_outcome 으로 적재.
      outcome_action/result 는 rules/outcome_taxonomy.yaml 어휘로 검증됨.)
   session_id: report_server report_session.session_id 역참조(선택, 있으면). analysis_key(컨텐츠
   해시)와 달리 업로드/실행 이벤트마다 새로 생성되는 ID. 같은 session_id 를 가진 행들은
   하나의 ingest_run 으로 묶인다(비워두면 product_type+family_product 만으로 묶임).

product_type + family_product 조합별로 db_input/output/<product_type>_<family_product>.db 를
생성/갱신한다(한 CSV 안에 여러 조합이 섞여 있어도 자동으로 파일이 분리된다).
`--to-eval-db` 를 주면 대신 config.DB_PATH(EVAL_DB_PATH env 존중) 하나로 통합 적재한다 —
report_server 안에서 run_import.bat 으로 실행하면 서버 소유 eval.db 를 자동으로 가리킨다.
item_canonical/category_major 분류는 eval_engine 운영 파이프라인(pipeline/ingest.py)과
동일한 규칙을 재사용해, 이후 실제 run 데이터와의 선례 fuzzy 매칭이 일관되게 동작하도록 한다.
재실행해도 안전(자연키 기반 upsert, case_id 재현 가능 — 같은 case 의 label 은 갱신).
"""
import csv
import os
import re
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # eval_analyzer/
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:  # Windows 콘솔(cp949)에서 한국어 출력 보장
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from eval_engine import config, store  # noqa: E402
from eval_engine.pipeline.ingest import (  # noqa: E402
    UNIT_TO_VALUE_TYPE, _alias_map, _canonicalize, _classify_category_major,
    _validate_product_meta,
)

OUTPUT_DIR = Path(__file__).resolve().parent / "output"

REQUIRED_COLUMNS = ["product_name", "product_type", "family_product",
                    "item_name", "value_type", "bin"]

# 단순 포맷(정식) 컬럼 — 헤더 정규화 후 이 집합과 정확히 일치하면 단순 포맷으로 읽는다.
SIMPLE_COLUMNS = ["product_type", "family_product", "unit", "item", "comment"]

# 엔진 UNIT_TO_VALUE_TYPE(pipeline/ingest.py) 보강 — 실측 데이터에 나오지만 엔진 표에 없는 표기.
# (VOLTS/AMPS 는 엔진 표에 이미 있고, HERTZ 는 없다.) 새 단위는 여기에 추가한다.
EXTRA_UNIT_ALIASES = {
    "hertz": "Hz",
    "ampere": "A", "amperes": "A",
    "second": "Sec", "seconds": "Sec",
    "pass_fail": "P_F",
}

# 단순 포맷 case 합성값 — lot/wafer/bin/limit 이 없는 요약 선례라 고정한다(docstring 참조).
SIMPLE_BIN = "0"


def _to_float(s):
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _cpk_summary(avg, stdev, lsl, usl):
    """CODE_TO_PORT.md §2 공식. 네 값이 모두 있고 stdev>0 일 때만 계산.
    eval_engine/pipeline/metrics.py:cpk_summary(raw 배열 입력)와 같은 공식의
    요약통계 입력판 — 공식 수정 시 양쪽을 함께 바꿀 것."""
    if avg is None or stdev is None or lsl is None or usl is None or stdev == 0:
        return {}
    cpl = (avg - lsl) / (3 * stdev)
    cpu = (usl - avg) / (3 * stdev)
    return {"cp": (usl - lsl) / (6 * stdev), "cpl": cpl, "cpu": cpu, "cpk": min(cpl, cpu)}


def _norm_header(name):
    """헤더 정규화 — 'Product type' / ' PRODUCT_TYPE ' 를 모두 'product_type' 으로."""
    return re.sub(r"\s+", "_", str(name or "").strip().lower())


def _map_unit(unit):
    """실측 단위 원문 → 엔진 어휘(V/A/Hz/CODE/Ohm/Sec/P_F). 모르는 단위면 None.

    엔진 UNIT_TO_VALUE_TYPE 을 먼저 보고 EXTRA_UNIT_ALIASES 로 보완한다
    (_classify_value_type 과 달리 미등록 단위를 조용히 P_F 로 만들지 않는다 — 호출측이 에러 처리).
    빈 문자열은 엔진 표에 P_F 로 들어 있어 그대로 통과한다.
    """
    key = str(unit or "").strip().lower()
    return UNIT_TO_VALUE_TYPE.get(key) or EXTRA_UNIT_ALIASES.get(key)


def _convert_simple_rows(rows, header_map):
    """단순 5컬럼 행 → 레거시 행 dict (이후 처리는 기존 경로 그대로 재사용).

    전 행을 먼저 검사해 에러를 모두 모은 뒤 한 번에 올린다 — 부분 적재 금지
    (product_type/family_product 조합 검증도 여기서 선행: _import_group 은 그룹마다
    검증하므로 뒤 그룹이 틀리면 앞 그룹이 이미 커밋된 뒤 중단된다).
    """
    errors, converted, pairs = [], [], set()
    for i, r in enumerate(rows, start=2):  # 헤더가 1행
        vals = {}
        for raw_key, norm_key in header_map.items():
            vals[norm_key] = (r.get(raw_key) or "").strip()
        product_type, family_product = vals["product_type"], vals["family_product"]
        item, comment, unit = vals["item"], vals["comment"], vals["unit"]

        for col, val in (("Product type", product_type), ("Family Product", family_product),
                         ("Item", item), ("comment", comment)):
            if not val:
                errors.append(f"{i}행: '{col}' 값이 비어 있습니다")
        value_type = _map_unit(unit)
        if value_type is None:
            errors.append(f"{i}행: 모르는 unit {unit!r} — "
                          f"db_input/import_csv.py 의 EXTRA_UNIT_ALIASES 에 추가하세요")
        if product_type and family_product:
            pairs.add((product_type, family_product))
        if value_type is None or not (product_type and family_product and item and comment):
            continue
        converted.append({
            "product_name": f"{product_type}_{family_product}",
            "product_type": product_type, "family_product": family_product,
            "item_name": item, "value_type": value_type, "unit": value_type,
            "bin": SIMPLE_BIN, "human_comment": comment,
        })

    for product_type, family_product in sorted(pairs):
        try:
            _validate_product_meta({"product_type": product_type,
                                    "family_product": family_product})
        except ValueError as e:
            errors.append(str(e))

    if errors:
        raise ValueError("CSV 오류 {}건 — 아무것도 적재하지 않았습니다.\n  {}".format(
            len(errors), "\n  ".join(errors)))
    return converted


def _read_rows(path):
    """CSV 읽기 + 포맷 자동 감지 (단순 5컬럼 / 레거시 20컬럼)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # Excel 이 남기는 트레일링 콤마(빈 헤더)는 감지에서 제외
        fieldnames = [c for c in (reader.fieldnames or []) if _norm_header(c)]
        rows = list(reader)
    if not rows:
        raise ValueError("빈 CSV 파일입니다.")
    header_map = {c: _norm_header(c) for c in fieldnames}
    if set(header_map.values()) == set(SIMPLE_COLUMNS) and len(header_map) == len(SIMPLE_COLUMNS):
        return _convert_simple_rows(rows, header_map)
    missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing:
        raise ValueError(f"필수 컬럼 누락: {missing}")
    return rows


def _require(row, col):
    val = (row.get(col) or "").strip()
    if not val:
        raise ValueError(f"필수값 누락 (컬럼={col}, item_name={row.get('item_name')!r})")
    return val


def _get_or_create_run(conn, csv_path, session_id):
    """같은 (csv_path, session_id) 로 재실행 시 run_id 를 재사용 -> raw_metrics 가
    (case_id,run_id) 로 upsert 되어 중복 대신 갱신되도록 함(재업로드 idempotent, DB_SCHEMA.md §0)."""
    row = conn.execute(
        """SELECT run_id FROM ingest_run
           WHERE source_file=? AND COALESCE(session_id,'')=COALESCE(?,'')
           ORDER BY run_id DESC LIMIT 1""",
        (str(csv_path), session_id)).fetchone()
    if row:
        return row["run_id"]
    return store.create_ingest_run(
        {"source_file": str(csv_path), "session_id": session_id,
         "ingested_by": "db_input"}, conn=conn)


def _import_group(product_type, family_product, session_id, rows, csv_path, db_path):
    _validate_product_meta({"product_type": product_type, "family_product": family_product})

    db_path.parent.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR = db_path.parent
    config.DB_PATH = db_path
    store.init_db()

    alias = _alias_map()
    with store.get_conn() as conn:
        run_id = _get_or_create_run(conn, csv_path, session_id)

        for r in rows:
            product_name = _require(r, "product_name")
            raw_name = _require(r, "item_name")
            value_type = _require(r, "value_type")
            bin_ = int(float(_require(r, "bin")))

            store.upsert_product_master({
                "product_name": product_name, "product_type": product_type,
                "family_product": family_product,
            }, conn=conn)

            item_canonical = alias.get(raw_name, _canonicalize(raw_name))
            category_major = _classify_category_major(raw_name)
            item_id = store.upsert_item_master(
                item_canonical, raw_name, None, None, category_major, None,
                value_type, (r.get("unit") or "").strip() or None, conn=conn)

            revision = _to_float(r.get("revision"))
            revision = 0.0 if revision is None else revision
            lsl, usl = _to_float(r.get("LSL")), _to_float(r.get("USL"))
            if lsl is not None or usl is not None:
                store.upsert_item_spec(item_id, product_name, revision, lsl, usl, conn=conn)

            lot_id = (r.get("lot_id") or "").strip() or None
            wafer_raw = (r.get("wafer_number") or "").strip()
            wafer_number = int(float(wafer_raw)) if wafer_raw else None

            case_id = store.make_case_id(product_name, lot_id, wafer_number, item_id,
                                         bin_, revision)
            item_class = f"{category_major}|{value_type}|{bin_}"
            store.upsert_fail_case(case_id, product_name, lot_id, wafer_number, item_id,
                                   bin_, revision, item_class, conn=conn)
            store.link_run_case(run_id, case_id, conn=conn)

            avg, stdev = _to_float(r.get("average")), _to_float(r.get("stdev"))
            if avg is not None or stdev is not None:
                metrics = {"mean": avg, "stdev": stdev}
                metrics.update(_cpk_summary(avg, stdev, lsl, usl))
                store.save_raw_metrics(case_id, run_id, metrics, conn=conn)

            human_comment = (r.get("human_comment") or "").strip() or None
            human_status = (r.get("human_status") or "").strip() or None
            root_cause = (r.get("root_cause_category") or "").strip() or None
            label_id = None
            if human_comment or human_status or root_cause:
                # 같은 case_id 에 라벨이 이미 있으면 재사용 (재실행 시 중복 삽입 방지,
                # tools/seed_demo_precedents.py 와 동일 관례). 값이 들어온 컬럼만 갱신해
                # 코멘트 수정본 재적재를 반영하고, 빈 컬럼은 기존 값을 지우지 않는다.
                existing_label = conn.execute(
                    "SELECT label_id FROM label WHERE case_id=? "
                    "ORDER BY label_id DESC LIMIT 1", (case_id,)).fetchone()
                if existing_label:
                    label_id = existing_label["label_id"]
                    conn.execute(
                        """UPDATE label SET human_status=COALESCE(?, human_status),
                             root_cause_category=COALESCE(?, root_cause_category),
                             human_comment=COALESCE(?, human_comment)
                           WHERE label_id=?""",
                        (human_status, root_cause, human_comment, label_id))
                else:
                    label_id = store.insert_label(
                        case_id, None, human_status, root_cause, None, 0, 0,
                        human_comment, "db_input", None, "manual", conn=conn)

            action = (r.get("outcome_action") or "").strip() or None
            condition = (r.get("outcome_condition") or "").strip() or None
            result = (r.get("outcome_result") or "").strip() or None
            if action or condition or result:
                # case 당 outcome 1건 (label 과 동일한 재실행 idempotent 관례)
                existing_outcome = conn.execute(
                    "SELECT outcome_id, label_id FROM case_outcome WHERE case_id=?",
                    (case_id,)).fetchone()
                if not existing_outcome:
                    store.insert_case_outcome(case_id, label_id, action, condition,
                                              result, None, None, None, conn=conn)
                elif existing_outcome["label_id"] is None and label_id is not None:
                    # 이전 임포트에 outcome 만 먼저 들어온 경우 label 연결 백필
                    conn.execute("UPDATE case_outcome SET label_id=? WHERE outcome_id=?",
                                 (label_id, existing_outcome["outcome_id"]))

    return db_path, len(rows)


def import_rows(rows, source_path, unified=False):
    """(product_type, family_product, session_id) 그룹별 분리 적재 — main/import_text 공용.

    unified 모드: 모든 그룹을 운영 eval.db(config.DB_PATH, EVAL_DB_PATH env 존중) 하나로 적재
    → search_precedents 가 바로 참조. 기본(비-unified): 제품군별 output/<pt>_<fp>.db 분리.
    반환: [(product_type, family_product, n, db_path, session_id), ...]
    """
    eval_db = Path(config.DB_PATH)   # override 전에 캡처

    groups = {}
    for r in rows:
        session_id = (r.get("session_id") or "").strip() or None
        key = (_require(r, "product_type"), _require(r, "family_product"), session_id)
        groups.setdefault(key, []).append(r)

    results = []
    for (product_type, family_product, session_id), group_rows in groups.items():
        db_path = eval_db if unified else OUTPUT_DIR / f"{product_type}_{family_product}.db"
        db_path, n = _import_group(product_type, family_product, session_id, group_rows,
                                   source_path, db_path)
        results.append((product_type, family_product, n, db_path, session_id))
    return results


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    unified = "--to-eval-db" in argv
    argv = [a for a in argv if a != "--to-eval-db"]
    if not argv:
        print(__doc__)
        return
    csv_path = argv[0]
    rows = _read_rows(csv_path)
    for product_type, family_product, n, db_path, session_id in import_rows(
            rows, csv_path, unified):
        print(f"[{product_type}_{family_product}] {n}건 적재 -> {db_path}"
              + (f" (session_id={session_id})" if session_id else ""))


if __name__ == "__main__":
    main()

"""데모 선례(precedent) seed — 샘플 CSV 의 fail item 들에 대응하는 가짜 과거 사례를 심는다.

목적: 선례검색(store.search_precedents) 이 실제로 코멘트에 "(과거 사례 N건: ...)" 로
human_comment 를 붙이는 과정을 bench 로 시연하기 위함. **실제 라벨 데이터 아님** —
lot_id="LOT_DEMO_PAST" 로 명확히 식별, 운영 판단에 사용 금지.

사용법:
  python tools/seed_demo_precedents.py [db_path]   # 기본: data/eval_sample.db
재실행해도 안전 — 이미 심어진 case_id 는 label 존재 여부로 건너뜀.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

DEMO_PRODUCT = "DEMO_PAST_SOC1"
DEMO_LOT = "LOT_DEMO_PAST"
DEMO_WAFER = 77

# (item_canonical, bin, category_major, value_type, root_cause_category,
#  human_comment, action, result)
DEMO_CASES = [
    ("iddq_init", 3, "NON_TRIM", "A", "process",
     "IDDQ init 중심 치우침 확인, trim offset 조정 후 재측정",
     "trim_adjust", "recovered_normal"),
    ("ldo_vout", 5, "NON_TRIM", "V", "equipment",
     "특정 사분면 집중 확인, handler 안착 불량으로 판정",
     "retest", "confirmed_defective"),
    ("osc_freq", 6, "NON_TRIM", "Hz", "process",
     "이봉 분포 확인, 로트 내 서브랏 혼입으로 확인",
     "condition_change", "recovered_normal"),
    ("iload_stby", 7, "NON_TRIM", "A", "unknown",
     "이봉+국부 편중 복합, 원인 특정 실패로 지속 모니터링",
     "monitor", "pending"),
    ("sleep_curr", 11, "NON_TRIM", "A", "design",
     "sleep 전류 중심 이탈, 설계 마진 재검토 요청",
     "dev_feedback", "pending"),
    ("pll_lock_time", 14, "NON_TRIM", "P_F", "process",
     "wafer edge 불량 집중, edge 공정 조건 변경 후 개선",
     "condition_change", "improved"),
    ("dcdc_ripple", 16, "NON_TRIM", "P_F", "spec",
     "spec margin 부족 확인, spec release 진행",
     "spec_release", "improved"),
]


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    db_path = argv[0] if argv else os.path.join(ROOT, "data", "eval_sample.db")
    os.environ["EVAL_DB_PATH"] = db_path

    from eval_engine import store

    with store.get_conn() as conn:
        store.upsert_product_master(
            {"product_name": DEMO_PRODUCT, "product_type": "PMIC",
             "family_product": "SOC"}, conn=conn)

        inserted, skipped = 0, 0
        for canonical, bin_, cat_major, value_type, root_cause, comment, action, result \
                in DEMO_CASES:
            item_id = store.upsert_item_master(
                item_canonical=canonical, item_name_raw=canonical.upper(),
                item_base=None, item_phase=None, category_major=cat_major,
                category_mid=None, value_type=value_type, unit=None, conn=conn)

            item_class = f"{cat_major}|{value_type}|{bin_}"
            case_id = store.make_case_id(DEMO_PRODUCT, DEMO_LOT, DEMO_WAFER,
                                         item_id, bin_, 0.0)
            store.upsert_fail_case(case_id, DEMO_PRODUCT, DEMO_LOT, DEMO_WAFER,
                                   item_id, bin_, 0.0, item_class, conn=conn)

            existing = conn.execute(
                "SELECT 1 FROM label WHERE case_id=?", (case_id,)).fetchone()
            if existing:
                skipped += 1
                continue

            label_id = store.insert_label(
                case_id, None, "MAJOR", root_cause, None,
                0, 0, comment, "demo_seed", None, None, conn=conn)
            store.insert_case_outcome(
                case_id, label_id, action, None, result,
                "demo_seed", None, None, conn=conn)
            inserted += 1
            print(f"  seeded: {canonical}(bin={bin_}) case_id={case_id[:12]}... "
                  f"{action}->{result}")

    print(f"\n완료: {inserted}건 삽입, {skipped}건 이미 존재(건너뜀). DB={db_path}")


if __name__ == "__main__":
    main()

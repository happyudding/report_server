"""db_input/import_csv — 단순 5컬럼 포맷(Product type/Family Product/unit/Item/comment).

헤더 자동 감지 · unit alias 매핑 · 사전 전수검사(부분 적재 금지) · 코멘트 수정 재적재.
레거시 20컬럼 경로는 test_db_input_import.py 가 커버한다(여기서는 회귀 1건만).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # eval_analyzer/ (db_input 탐색)

from db_input import import_csv  # noqa: E402
from eval_engine import store  # noqa: E402

SIMPLE_HEADER = "Product type,Family Product,unit,Item,comment\n"


def _write_csv(tmp_path, body, header=SIMPLE_HEADER, name="simple.csv"):
    """헤더 + 본문으로 임시 CSV 를 쓰고 경로 반환. 기본 헤더는 단순 5컬럼 포맷."""
    path = tmp_path / name
    path.write_text(header + body, encoding="utf-8")
    return path


def _import(tmp_path, body, header=SIMPLE_HEADER, name="simple.csv"):
    """단순 포맷 CSV 를 fresh_db(config.DB_PATH)로 적재 — unified 경로 고정.
    (비-unified 는 리포 안 db_input/output/ 에 실제 파일을 만들므로 테스트에서 쓰지 않는다.)"""
    path = _write_csv(tmp_path, body, header, name)
    rows = import_csv._read_rows(path)
    return import_csv.import_rows(rows, path, unified=True)


def test_simple_format_maps_units_and_loads_labels(fresh_db, tmp_path):
    _import(tmp_path,
            "PMIC,SOC,VOLTS,VREF_TRIM,전압 마진 부족\n"
            "PMIC,SOC,HERTZ,OSC_FREQ,주파수 산포 큼\n"
            "PMIC,SOC,AMPS,IDD_ACTIVE,소비전류 초과\n")
    with store.get_conn() as conn:
        items = {r["item_name_raw"]: r for r in conn.execute("SELECT * FROM item_master")}
        assert items["VREF_TRIM"]["value_type"] == "V"
        assert items["OSC_FREQ"]["value_type"] == "Hz"
        assert items["IDD_ACTIVE"]["value_type"] == "A"
        # unit 컬럼도 canonical 로 채워진다 (기존 경로는 NULL 이었다)
        assert items["VREF_TRIM"]["unit"] == "V"

        cases = conn.execute("SELECT * FROM fail_case").fetchall()
        assert len(cases) == 3
        assert all(c["product_name"] == "PMIC_SOC" for c in cases)   # 합성 product_name
        assert all(c["bin"] == 0 and c["lot_id"] is None for c in cases)

        labels = conn.execute("SELECT * FROM label ORDER BY label_id").fetchall()
        assert [l["human_comment"] for l in labels] == [
            "전압 마진 부족", "주파수 산포 큼", "소비전류 초과"]
        assert all(l["labeler"] == "db_input" for l in labels)


def test_simple_format_header_variants_accepted(fresh_db, tmp_path):
    """대소문자·공백·순서가 달라도 5컬럼이면 단순 포맷으로 인식."""
    _import(tmp_path, "VREF_TRIM,PMIC,SOC,V,코멘트\n",
            header=" ITEM , product_type ,Family  Product, UNIT ,Comment\n")
    with store.get_conn() as conn:
        row = conn.execute("SELECT * FROM item_master").fetchone()
        assert row["item_name_raw"] == "VREF_TRIM" and row["value_type"] == "V"


def test_simple_format_unknown_unit_aborts_without_loading(fresh_db, tmp_path):
    with pytest.raises(ValueError) as e:
        _import(tmp_path,
                "PMIC,SOC,VOLTS,VREF_TRIM,정상 행\n"
                "PMIC,SOC,dB,GAIN_TEST,모르는 단위\n")
    msg = str(e.value)
    assert "3행" in msg and "'dB'" in msg
    with store.get_conn() as conn:   # 정상 행도 적재되지 않는다
        assert conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 0


def test_simple_format_blank_comment_aborts(fresh_db, tmp_path):
    with pytest.raises(ValueError) as e:
        _import(tmp_path, "PMIC,SOC,V,VREF_TRIM,\n")
    assert "comment" in str(e.value)


def test_simple_format_unknown_family_aborts(fresh_db, tmp_path):
    """product_taxonomy.yaml 어휘 검증 — 뒤 그룹이 틀려도 앞 그룹이 먼저 커밋되지 않는다."""
    with pytest.raises(ValueError):
        _import(tmp_path,
                "PMIC,SOC,V,VREF_TRIM,정상 행\n"
                "PMIC,NOT_A_FAMILY,V,OTHER_ITEM,잘못된 제품군\n")
    with store.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0] == 0


def test_simple_format_reimport_updates_comment(fresh_db, tmp_path):
    """같은 (pt,fp,item) 은 한 case 로 접히고, 재적재 시 코멘트가 갱신된다."""
    _import(tmp_path, "PMIC,SOC,VOLTS,VREF_TRIM,최초 코멘트\n")
    _import(tmp_path, "PMIC,SOC,VOLTS,VREF_TRIM,수정된 코멘트\n")
    with store.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM label").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0] == 1
        assert conn.execute(
            "SELECT human_comment FROM label").fetchone()[0] == "수정된 코멘트"


def test_simple_format_empty_unit_falls_back_to_pf(fresh_db, tmp_path):
    """빈 unit 은 엔진(UNIT_TO_VALUE_TYPE[''])과 동일하게 PF — 에러가 아니다."""
    _import(tmp_path, "PMIC,SOC,,SPMI_LDO_POK,pass/fail 항목\n")
    with store.get_conn() as conn:
        assert conn.execute("SELECT value_type FROM item_master").fetchone()[0] == "PF"


def test_legacy_format_still_detected(fresh_db, tmp_path):
    """20컬럼 레거시 CSV 는 계속 레거시 경로로 간다(감지가 가로채지 않음)."""
    header = ",".join(import_csv.REQUIRED_COLUMNS) + "\n"
    _import(tmp_path, "S5E_TEST_1,PMIC,SOC,VREF_TRIM,V,18\n", header=header,
            name="legacy.csv")
    with store.get_conn() as conn:
        case = conn.execute("SELECT * FROM fail_case").fetchone()
        assert case["product_name"] == "S5E_TEST_1" and case["bin"] == 18

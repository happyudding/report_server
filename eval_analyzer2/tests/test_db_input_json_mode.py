"""db_input/import_csv — dry-run / JSON 모드 + unit 부분일치.

report_server 의 Honey 'DB Input' 이 이 스크립트를 subprocess 로 부르고
`--json` stdout(마지막 줄 JSON 1줄) + 종료코드 0/2 계약에 의존한다 (docs/13 §10).
여기서 그 계약과 unit 매핑 규칙을 고정한다.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # eval_analyzer/ (db_input 탐색)

from db_input import import_csv  # noqa: E402
from eval_engine import store  # noqa: E402

SIMPLE_HEADER = "Product type,Family Product,unit,Item,comment\n"


def _write_csv(tmp_path, body, header=SIMPLE_HEADER, name="simple.csv"):
    path = tmp_path / name
    path.write_text(header + body, encoding="utf-8")
    return path


def _counts():
    with store.get_conn() as conn:
        return (conn.execute("SELECT COUNT(*) FROM fail_case").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM label").fetchone()[0])


# ── unit 매핑 (정확일치 유지 + 부분일치 신규 + %) ─────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    # 1단계 정확일치 — 기존 동작 불변
    ("V", "V"), ("volts", "V"), ("mA", "A"), ("KHZ", "Hz"), ("ns", "Sec"),
    ("", "P_F"), ("pass/fail", "P_F"),
    # 2단계 부분일치 — 이번 추가
    ("MILLIVOLT", "V"), ("mVolt", "V"),
    ("AMPERE", "A"), ("mAmp", "A"),
    ("KiloHertz", "Hz"), ("GHz", "Hz"),
    ("MOhm", "Ohm"), ("kohm", "Ohm"),
    ("mSec", "Sec"), ("nsec", "Sec"),
    ("TCODE", "CODE"),
    # % — 신규 저장값
    ("%", "%"), ("PCT", "%"), ("Percent", "%"), ("fail%", "%"),
])
def test_map_unit(raw, expected):
    assert import_csv._map_unit(raw) == expected


def test_map_unit_still_rejects_unknown():
    """조용한 P_F 폴백 금지 — search_precedents 가 value_type 을 등호 하드필터로 쓴다."""
    assert import_csv._map_unit("dB") is None
    assert import_csv._map_unit("lux") is None


def test_percent_unit_loads(fresh_db, tmp_path):
    path = _write_csv(tmp_path, "PMIC,SOC,PCT,LEAK_RATIO,비율 항목\n")
    import_csv.run(path, unified=True)
    with store.get_conn() as conn:
        row = conn.execute("SELECT * FROM item_master").fetchone()
        assert row["value_type"] == "%" and row["unit"] == "%"


# ── _detect_format ───────────────────────────────────────────────────────────

def test_detect_format(tmp_path):
    simple = _write_csv(tmp_path, "PMIC,SOC,V,VREF_TRIM,코멘트\n", name="s.csv")
    legacy = _write_csv(tmp_path, "S5E_1,PMIC,SOC,VREF_TRIM,V,18\n",
                        header=",".join(import_csv.REQUIRED_COLUMNS) + "\n", name="l.csv")
    empty = tmp_path / "e.csv"
    empty.write_text("", encoding="utf-8")
    assert import_csv._detect_format(simple) == "simple"
    assert import_csv._detect_format(legacy) == "legacy"
    assert import_csv._detect_format(empty) == ""


# ── run(): dry-run vs commit ─────────────────────────────────────────────────

def test_dry_run_reports_groups_without_touching_db(fresh_db, tmp_path):
    path = _write_csv(tmp_path,
                      "PMIC,SOC,VOLTS,VREF_TRIM,a\n"
                      "PMIC,SOC,HERTZ,OSC_FREQ,b\n"
                      "MDDI,MX,V,VDD_TEST,c\n")
    out = import_csv.run(path, unified=True, dry_run=True)
    assert out["ok"] and out["mode"] == "dry-run" and out["format"] == "simple"
    assert out["rows"] == 3
    assert out["groups"] == [
        {"product_type": "MDDI", "family_product": "MX", "rows": 1},
        {"product_type": "PMIC", "family_product": "SOC", "rows": 2},
    ]
    assert _counts() == (0, 0)   # DB 를 열지 않았다


def test_commit_reports_groups_and_loads(fresh_db, tmp_path):
    path = _write_csv(tmp_path,
                      "PMIC,SOC,VOLTS,VREF_TRIM,a\n"
                      "PMIC,SOC,HERTZ,OSC_FREQ,b\n")
    out = import_csv.run(path, unified=True, dry_run=False)
    assert out["ok"] and out["mode"] == "commit" and out["rows"] == 2
    assert len(out["groups"]) == 1
    assert out["groups"][0]["rows"] == 2 and out["groups"][0]["db_path"]
    assert _counts() == (2, 2)


def test_dry_run_errors_are_a_list(fresh_db, tmp_path):
    """서버가 목록 그대로 보여줄 수 있어야 한다 — join 된 한 덩어리면 안 된다."""
    path = _write_csv(tmp_path,
                      "PMIC,SOC,VOLTS,VREF_TRIM,정상 행\n"
                      "PMIC,SOC,dB,GAIN_TEST,모르는 단위\n"
                      "PMIC,SOC,V,NO_COMMENT,\n")
    out = import_csv.run(path, unified=True, dry_run=True)
    assert not out["ok"]
    assert len(out["errors"]) >= 2
    assert any("3행" in e and "'dB'" in e for e in out["errors"])
    assert _counts() == (0, 0)


def test_error_message_unchanged(fresh_db, tmp_path):
    """CsvValidationError 의 str() 은 종전과 동일 — 콘솔 출력·기존 테스트 회귀 방지."""
    path = _write_csv(tmp_path, "PMIC,SOC,dB,GAIN_TEST,모르는 단위\n")
    with pytest.raises(import_csv.CsvValidationError) as e:
        import_csv._read_rows(path)
    msg = str(e.value)
    assert msg.startswith("CSV 오류 1건 — 아무것도 적재하지 않았습니다.")
    assert "2행" in msg and "'dB'" in msg
    assert e.value.errors == [m for m in e.value.errors]   # 리스트로도 접근 가능


# ── main(): CLI 계약 ─────────────────────────────────────────────────────────

def test_main_json_stdout_and_exit_code(fresh_db, tmp_path, capsys):
    path = _write_csv(tmp_path, "PMIC,SOC,VOLTS,VREF_TRIM,코멘트\n")
    assert import_csv.main(["--json", "--dry-run", "--to-eval-db", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] and payload["rows"] == 1

    bad = _write_csv(tmp_path, "PMIC,SOC,dB,GAIN_TEST,x\n", name="bad.csv")
    assert import_csv.main(["--json", "--to-eval-db", str(bad)]) == 2
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert not payload["ok"] and payload["errors"]


def test_main_flags_before_path(fresh_db, tmp_path, capsys):
    """구 파싱은 '--to-eval-db' 만 걸러내고 argv[0] 을 경로로 써서, 플래그가 앞에 오면
    그걸 CSV 경로로 잡았다. '--' 로 시작하면 전부 플래그로 본다."""
    path = _write_csv(tmp_path, "PMIC,SOC,V,VREF_TRIM,코멘트\n")
    assert import_csv.main(["--dry-run", "--to-eval-db", str(path)]) == 0
    assert "PMIC_SOC" in capsys.readouterr().out
    assert _counts() == (0, 0)


def test_main_no_path_prints_usage(capsys):
    assert import_csv.main(["--json"]) == 0
    assert "사용법" in capsys.readouterr().out

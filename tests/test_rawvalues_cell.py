"""Raw Data 편집 값 규칙 검증 (rawvalues 셀 단위 순수 함수).

실행:
    python tests/test_rawvalues_cell.py

측정값에 오타를 넣으면 지금까지는 조용히 NaN 으로 사라져 CPK 의 n·평균·σ 가 바뀌었고,
BIN 에 문자를 넣으면 fail die 로 집계돼 수율이 달라졌다. 그 값들이 저장 전에 거부되는지,
그리고 표기 차이('01'/'1.0'/' 1 ')가 하나로 정규화되는지 확인한다.

**프런트(raw_data.js)와 판정이 갈리면 사용자가 통과시킨 값이 서버 400 으로 튕긴다.**
파이썬 float() 은 '1_000'·전각숫자·'infinity' 를, JS Number() 는 '0x10'·'0b101' 을
받아들이므로 양쪽 모두 _NUM_RE(= JS RAW_NUM_RE)로 표기를 먼저 좁힌다 — 그 경계 케이스를
아래 NUMERIC_TRAPS 가 고정한다.

pandas 불필요 — 순수 파이썬만 쓴다. pytest 미사용 (tests/ 관례).
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from web_report import rawvalues as rv  # noqa: E402

# (컬럼, 입력, is_item, 통과 여부, 통과 시 정규형)
CASES = [
    # ── item(측정값): 숫자 또는 빈값(결측)만 ───────────────────────────────
    ("ItemA", "5", True, True, "5"),
    ("ItemA", "5.5", True, True, "5.5"),
    ("ItemA", " 1.250 ", True, True, "1.250"),      # 정밀도는 보존(strip 만)
    ("ItemA", "-0.5", True, True, "-0.5"),
    ("ItemA", "1e3", True, True, "1e3"),
    ("ItemA", "", True, True, ""),                   # 결측 허용
    ("ItemA", None, True, True, ""),
    ("ItemA", "5o", True, False, None),              # 오타 — 조용한 NaN 소멸 차단
    ("ItemA", "N/A", True, False, None),
    ("ItemA", "abc", True, False, None),
    ("ItemA", "nan", True, False, None),             # float() 이 받으므로 명시 차단
    ("ItemA", "inf", True, False, None),
    ("ItemA", "-inf", True, False, None),
    ("ItemA", "1e999", True, False, None),           # → inf
    ("ItemA", "a\nb", True, False, None),
    # ── 정수 메타: 정수만, 표기 차이는 정규화 ──────────────────────────────
    ("BIN", "1", False, True, "1"),
    ("BIN", "01", False, True, "1"),                 # fmt_type 비대칭 제거
    ("BIN", "1.0", False, True, "1"),                # Excel 왕복 산물 수용
    ("BIN", " 1 ", False, True, "1"),
    ("BIN", "-3", False, True, "-3"),                # 음수 허용
    ("BIN", "1e2", False, True, "100"),
    ("BIN", "abc", False, False, None),              # 수율이 조용히 바뀌던 경로
    ("BIN", "5.5", False, False, None),
    ("BIN", "", False, False, None),                 # BIN 은 빈값 금지
    ("SHOT", "", False, True, ""),                   # 나머지 메타는 빈값 허용
    ("XPOS", "", False, True, ""),                   # 좌표 미상 die 는 허용(경고만)
    ("XPOS", "x", False, False, None),
    ("FAILTNO", " 12 ", False, True, "12"),
    # ── SERIAL: 자유 문자열, 빈값·개행·과길이만 금지 ───────────────────────
    ("SERIAL", "007", False, True, "007"),           # 선행 0 보존(식별자의 일부)
    ("SERIAL", "wafer-01", False, True, "wafer-01"),
    ("SERIAL", "", False, False, None),
    ("SERIAL", "a" * 201, False, False, None),
    ("SERIAL", "a\nb", False, False, None),
]

# 파이썬 float() 과 JS Number() 가 서로 다르게 받아들이는 표기 — 전부 거부여야 한다.
# (하나라도 통과로 바뀌면 프런트와 판정이 갈려 400 왕복이 생긴다)
NUMERIC_TRAPS = ["0x10", "0b101", "0o17", "1_000", "１２", "٣", "infinity",
                 "1e", "--3", "3,5", "+ 3", "1.2.3"]


def check(column, value, is_item, want_ok, want_norm):
    err = rv.check_cell_value(column, value, is_item=is_item)
    got_ok = err is None
    if got_ok != want_ok:
        raise AssertionError(
            f"{column} {value!r} (item={is_item}): 통과여부 {got_ok} != 기대 {want_ok} "
            f"(사유: {err})")
    if not want_ok:
        if not err.strip():
            raise AssertionError(f"{column} {value!r}: 거부 사유가 비어 있다")
        return
    got_norm = rv.normalize_cell_value(column, value, is_item=is_item)
    if got_norm != want_norm:
        raise AssertionError(
            f"{column} {value!r}: 정규형 {got_norm!r} != 기대 {want_norm!r}")


def main():
    for column, value, is_item, want_ok, want_norm in CASES:
        check(column, value, is_item, want_ok, want_norm)
    print(f"(a) 셀 규칙 {len(CASES)}건 통과")

    for text in NUMERIC_TRAPS:
        if rv.check_cell_value("ItemA", text, is_item=True) is None:
            raise AssertionError(f"숫자 표기 함정 {text!r} 이 item 값으로 통과했다 "
                                 f"(JS RAW_NUM_RE 와 판정이 갈린다)")
        if rv.check_cell_value("BIN", text, is_item=False) is None:
            raise AssertionError(f"숫자 표기 함정 {text!r} 이 BIN 값으로 통과했다")
    print(f"(b) 숫자 표기 함정 {len(NUMERIC_TRAPS)}건 거부 확인 "
          f"(JS Number()/파이썬 float() 차이 봉쇄)")

    # 규칙 스펙은 메타 7컬럼을 빠짐없이 덮어야 한다 — 빠지면 그 컬럼은 무검증으로 통과한다.
    # honeyform 은 pandas 를 끌어오므로(이 테스트는 pandas 없이도 돌아야 한다) 스키마
    # 리터럴과 먼저 대조하고, import 가 되는 환경에서만 honeyform 과 교차검증한다.
    expected_meta = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO"]
    spec = rv.rules_spec()
    if set(spec["meta_kind"]) != set(expected_meta):
        raise AssertionError(
            f"rules_spec meta_kind 가 메타 7컬럼과 불일치: "
            f"{sorted(set(expected_meta) ^ set(spec['meta_kind']))}")
    try:
        from web_report.honeyform import META_COLUMNS
    except ImportError as exc:            # pandas 없음 — 리터럴 대조로 충분
        print(f"    (honeyform 교차검증 생략: {exc})")
    else:
        if list(META_COLUMNS) != expected_meta:
            raise AssertionError(
                f"honeyform.META_COLUMNS 가 바뀌었다: {list(META_COLUMNS)} "
                f"— rawvalues.META_VALUE_KIND 와 이 테스트를 같이 고칠 것")
    for key in ("int", "number", "required", "too_long", "newline"):
        if not spec["messages"].get(key):
            raise AssertionError(f"rules_spec messages 에 {key} 문안이 없다")
    for key in ("bin_change", "coord_blank", "item_blank"):
        if not spec["warnings"].get(key):
            raise AssertionError(f"rules_spec warnings 에 {key} 문안이 없다")
    print("(c) rules_spec 이 메타 7컬럼·메시지·경고 문안을 모두 덮음")

    print("\n모든 검증 통과")


if __name__ == "__main__":
    main()

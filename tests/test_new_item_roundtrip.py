"""신규 수식 item 의 honeyform 왕복 검증 — item_add 가 만든 parquet 이 되읽히는가.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_new_item_roundtrip.py

이 단계가 틀리면 서버는 아무 불평도 하지 않는다 — parquet 은 유효하고 세션도 열리는데
값·메타만 어긋난다. 특히:
  · 메타 6행을 TSEQ/TNO/STEP/UNIT/HILIM/LOLIM **순서**로 안 넣으면 규격이 뒤바뀐다.
  · 계산값을 float 로만 넣으면 IF(...,0,1) 결과가 "1.0" 으로 저장돼 되읽을 때 float64 가 된다
    (0/1 판정 컬럼의 표기 드리프트 — excel_session.restore_int_columns 와 같은 문제).
  · 참조 항목이 없는 source 를 "전부 NaN 컬럼"으로 채우면 CPK·Distribution 제외 집합에 잡혀
    "왜 이 source 만 값이 없나"를 두 번 설명해야 한다. 원본 bytes 를 그대로 둬야 한다.
  · 기존 컬럼을 건드리면 BIN·FAILTNO 가 흔들려 수율·Wafer Map 이 바뀐다.

검증 항목:
  (a) 신규 컬럼이 **마지막 item 컬럼**으로 붙고 메타 6행 값이 정확하다
  (b) 계산값이 die 순서대로 1:1 로 되읽힌다 (NaN 은 결측으로)
  (c) **정수 전용 결과가 int64 로 되살아난다** (0/1 판정 컬럼 표기 보존)
  (d) **BIN·FAILTNO 와 기존 item 컬럼의 값·dtype 이 추가 전과 완전히 동일**
  (e) 참조 항목이 없는 source 는 컬럼이 안 생긴다 (build_sources 가 None 을 돌려준다)
  (f) 이름 중복·메타명 충돌은 encode 단계에서 한국어 ValueError
  (g) validate_meta — 중복/예약어/TNO 충돌/LOLIM>HILIM/비수치 TSEQ 거부
  (h) default_meta — 전 source 최대 +1, STEP 승계, 비수치면 빈칸
  (i) **add_item 전체 배선** — 순서·건너뛴 source 원본 bytes·pack selected_items·확인창

pytest 미사용 (tests/ 관례 — 자체 실행 + assert). 서버·Qt 불필요.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "client"))

# excel_session 이 모듈 상단에서 requests 를 import 한다 — 이 테스트는 네트워크 호출을
# 전부 스텁으로 대체하므로 requests 가 없는 환경(server/.venv)에서도 돌게 빈 모듈로 채운다.
try:
    import requests  # noqa: F401
except ImportError:
    import types

    sys.modules["requests"] = types.ModuleType("requests")

from excel_edit import item_add                                        # noqa: E402
from web_report import formula                                         # noqa: E402
from web_report.honeyform import (META_COLUMNS, META_ROW_LABELS,       # noqa: E402
                                  decode_split_honeyform_parquet,
                                  encode_honeyform_parquet)

NEW = "VREF_MARGIN"


def T(*specs):
    out = []
    for s in specs:
        if s == "(":
            out.append({"t": "lp"})
        elif s == ")":
            out.append({"t": "rp"})
        elif s == ",":
            out.append({"t": "comma"})
        elif isinstance(s, str) and s in formula.FUNCS:
            out.append({"t": "fn", "v": s})
        elif isinstance(s, str) and s in ("+", "-", "*", "/", ">", ">=", "<", "<=", "=", "<>"):
            out.append({"t": "op", "v": s})
        elif isinstance(s, (int, float)) and not isinstance(s, bool):
            out.append({"t": "num", "v": float(s)})
        else:
            out.append({"t": "item", "item": s})
    return out


def make_frame(items, rows=6, *, meta_over=None):
    """합성 7-meta honeyform 프레임 (메타 6행 + 데이터 rows 행)."""
    n_meta = len(META_COLUMNS)
    columns = list(META_COLUMNS) + list(items)
    data = []
    for pos, label in enumerate(META_ROW_LABELS):
        row = [label] + [None] * (n_meta - 1)
        for j, item in enumerate(items):
            over = (meta_over or {}).get(item, {}).get(label)
            if over is not None:
                row.append(over)
            elif label == "TSEQ":
                row.append(str(10 + j))
            elif label == "TNO":
                row.append(str(1200 + j))
            elif label == "STEP":
                row.append("P2")
            elif label == "UNIT":
                row.append("V")
            elif label == "HILIM":
                row.append("5")
            else:
                row.append("0")
        data.append(row)
    for r in range(rows):
        row = [f"S{r:03d}", "1", "1", str(r + 1), "1", "1" if r % 3 else "5",
               "0" if r % 3 else str(1200 + (r % max(len(items), 1)))]
        for j, item in enumerate(items):
            row.append(str(round(1.0 + j + r * 0.5, 3)))
        data.append(row)
    return pd.DataFrame(data, columns=columns)


def table_of(df, source):
    """합성 프레임 -> 진짜 HoneyformTable (add_item 이 실제로 쓰는 것과 같은 경로).

    encode->decode 를 거치므로 dtype·문자열화까지 운영과 동일하다.
    """
    return decode_split_honeyform_parquet(encode_honeyform_parquet(df.copy()),
                                          source=source, keep_df=True)


def check(cond, label):
    assert cond, label


def test_roundtrip():
    items = ["VDD_A", "VDD_B", "IDD"]
    df = make_frame(items, rows=8)
    before = encode_honeyform_parquet(df.copy())
    base = decode_split_honeyform_parquet(before, source="WF1")

    tokens = formula.normalize_tokens(
        T("IF", "(", "VDD_A", ">", "MIN", "(", "VDD_B", ",", "IDD", ")", ",", 0, ",", 1, ")"))
    meta = {"name": NEW, "tseq": "99", "tno": "1999", "step": "P3",
            "unit": "cnt", "hilim": "1", "lolim": "0"}

    tables = [table_of(df, "WF1")]
    parquets, applied, skipped = item_add.build_sources(tables, [t.df for t in tables], tokens, meta)
    check(applied == ["WF1"] and skipped == [], f"applied={applied} skipped={skipped}")

    after = decode_split_honeyform_parquet(parquets[0], source="WF1")

    # (a) 마지막 item 컬럼 + 메타 6행
    check(after.item_columns == items + [NEW], after.item_columns)
    got_meta = (after.tseq[NEW], after.tno[NEW], after.step[NEW],
                after.units[NEW], after.hilim[NEW], after.lolim[NEW])
    check([str(v) for v in got_meta] == ["99", "1999", "P3", "cnt", "1", "0"], got_meta)
    print(f"  [ok] (a) 마지막 컬럼 + 메타 6행 {got_meta}")

    # (b) 값 1:1
    want = formula.eval_for_table(base, tokens)
    got = pd.to_numeric(after.data[NEW], errors="coerce").to_numpy(dtype=np.float64)
    check(np.array_equal(np.isnan(got), np.isnan(want)), f"NaN 위치 {got} vs {want}")
    check(np.allclose(got[~np.isnan(got)], want[~np.isnan(want)]), f"{got} vs {want}")
    print(f"  [ok] (b) 값 1:1 ({len(got)} die)")

    # (c) 정수 전용 결과는 int64 로 되살아난다
    kind = after.data[NEW].dtype.kind
    check(kind == "i", f"0/1 판정 컬럼이 int64 가 아니다 (dtype kind={kind})")
    print(f"  [ok] (c) 정수 전용 결과 dtype kind={kind}")

    # 소수 결과는 float 로 남는다 (역방향 회귀 — 전부 int 로 만들면 안 된다)
    df2 = make_frame(items, rows=8)
    t2 = [table_of(df2, "WF1")]
    p2, _, _ = item_add.build_sources(t2, [t.df for t in t2],
                                      formula.normalize_tokens(T("VDD_A", "/", 3)),
                                      dict(meta, name="RATIO"))
    kind2 = decode_split_honeyform_parquet(p2[0], source="WF1").data["RATIO"].dtype.kind
    check(kind2 == "f", f"소수 결과가 float 가 아니다 (kind={kind2})")
    print(f"  [ok] (c) 소수 결과 dtype kind={kind2} (정수 강제 아님)")

    # (d) 기존 컬럼 무변경 — 값·dtype 둘 다
    for col in list(META_COLUMNS) + items:
        if col in base.data.columns:
            left = base.data[col]
            right = after.data[col]
            check(left.dtype == right.dtype, f"{col} dtype {left.dtype} → {right.dtype}")
            check(left.astype(object).tolist() == right.astype(object).tolist(),
                  f"{col} 값이 바뀌었다")
    print("  [ok] (d) BIN·FAILTNO 포함 기존 컬럼 값·dtype 완전 동일")


def test_skipped_source():
    a = make_frame(["VDD_A", "VDD_B"], rows=5)
    b = make_frame(["VDD_A"], rows=5)          # VDD_B 가 없다
    tokens = formula.normalize_tokens(T("VDD_A", "+", "VDD_B"))
    meta = {"name": NEW, "tseq": "99", "tno": "1999", "step": "P2",
            "unit": "", "hilim": "", "lolim": ""}
    tabs = [table_of(a, "WF1"), table_of(b, "WF2")]
    parquets, applied, skipped = item_add.build_sources(
        tabs, [t.df for t in tabs], tokens, meta)
    check(applied == ["WF1"], applied)
    check(skipped == ["WF2"], skipped)
    check(parquets[1] is None, "건너뛴 source 에 parquet 을 만들었다")
    check(NEW in decode_split_honeyform_parquet(parquets[0], source="WF1").item_columns,
          "적용한 source 에 신규 컬럼이 없다")
    print("  [ok] (e) 참조 항목 없는 source 는 컬럼을 만들지 않는다 (원본 bytes 재사용)")


def test_encode_guard():
    """이름이 겹치면 **적용 직전에** 막는다.

    pandas 의 df[name] = ... 는 겹치는 이름에 새 컬럼을 만들지 않고 기존 컬럼을 조용히
    덮어쓴다. BIN 을 덮으면 수율·Wafer Map 이 통째로 바뀌고, 그 parquet 은 스키마상 유효해서
    서버도 아무 불평을 하지 않는다 — UI 검증만 믿으면 안 되는 이유다.
    """
    items = ["VDD_A", "VDD_B"]
    tokens = formula.normalize_tokens(T("VDD_A", "+", "VDD_B"))
    for bad in ("BIN", "SERIAL", "FAILTNO", "VDD_A"):
        table = table_of(make_frame(items, rows=4), "WF1")
        keep = table.df.copy()
        try:
            item_add.build_sources([table], [table.df], tokens,
                                   {"name": bad, "tseq": "1", "tno": "1", "step": "P2",
                                    "unit": "", "hilim": "", "lolim": ""})
        except ValueError as exc:
            check("이미 있는 컬럼" in str(exc), str(exc))
        else:
            raise AssertionError(f"{bad}: 거부돼야 한다 (기존 컬럼을 덮어썼다)")
        check(table.df.equals(keep), f"{bad}: 거부됐는데 프레임이 바뀌었다")
    print("  [ok] (f) 이름 충돌 4종 거부 - 기존 컬럼을 덮어쓰지 않는다")


def test_validate_meta():
    df = make_frame(["VDD_A", "VDD_B"], rows=4)
    tables = [table_of(df, "WF1")]
    ok = {"name": NEW, "tseq": "99", "tno": "1999", "step": "P2",
          "unit": "", "hilim": "", "lolim": ""}
    check(item_add.validate_meta(ok, tables) == [], item_add.validate_meta(ok, tables))

    def one(over, hint):
        issues = item_add.validate_meta(dict(ok, **over), tables)
        check(issues, f"{hint}: 통과했다")
        print(f"  거부됨({hint}): {issues[0]}")

    one({"name": ""}, "빈 이름")
    one({"name": "VDD_A"}, "기존 항목 중복")
    one({"name": " VDD_A "}, "공백만 다른 중복")
    one({"name": "BIN"}, "예약 메타명")
    one({"tseq": "abc"}, "비수치 TSEQ")
    one({"tseq": ""}, "빈 TSEQ")
    one({"tno": "1200"}, "기존 TNO 충돌")
    one({"hilim": "x"}, "비수치 HILIM")
    one({"hilim": "1", "lolim": "5"}, "LOLIM > HILIM")
    # 파이썬 float() 은 통과시키지만 pandas 는 NaN 으로 떨구는 표기 — rawvalues 규칙을
    # 써야만 잡힌다(안 잡으면 규격이 조용히 사라진다).
    one({"hilim": "1_000"}, "언더스코어 표기 HILIM")
    one({"tno": "1_999"}, "언더스코어 표기 TNO")
    print("  [ok] (g) validate_meta 거부 11종 (rawvalues 표기 규칙 포함)")


def test_default_meta():
    a = make_frame(["VDD_A", "VDD_B"], rows=3)          # TSEQ 10,11 / TNO 1200,1201
    b = make_frame(["IDD"], rows=3, meta_over={"IDD": {"TSEQ": "40", "TNO": "1700",
                                                       "STEP": "P3"}})
    meta = item_add.default_meta([table_of(a, "WF1"), table_of(b, "WF2")])
    check(meta["tseq"] == "41", meta)
    check(meta["tno"] == "1701", meta)
    check(meta["step"] == "P2", meta)                   # 첫 source 의 마지막 item
    check(set(meta["step_choices"]) == {"P2", "P3"}, meta)
    print(f"  [ok] (h) default_meta 전 source 최대+1: tseq={meta['tseq']} tno={meta['tno']} "
          f"step={meta['step']}")

    # TSEQ/TNO 가 비수치면 빈칸으로 두고 사용자에게 넘긴다
    c = make_frame(["X"], rows=2, meta_over={"X": {"TSEQ": "-", "TNO": "n/a"}})
    meta2 = item_add.default_meta([table_of(c, "WF1")])
    check(meta2["tseq"] == "" and meta2["tno"] == "", meta2)
    print("  [ok] (h) 비수치 TSEQ/TNO 는 빈칸 (추측하지 않는다)")


def test_add_item_flow():
    """add_item 전체 배선 - 다운로드 -> 판정 -> 계산 -> 재구성 -> 업로드.

    단위 함수가 다 맞아도 **엮는 자리**가 틀리면 조용히 깨진다. 실제로 초안에서
    "건너뛴 source 의 원본 bytes 를 어디서도 못 얻는" 버그가 여기서 잡혔다.
    확인하는 것:
      · 업로드 목록의 길이·순서가 원본 idx 순서 그대로다 (서버는 목록에 없는 source 를 지운다)
      · 계산 대상만 새 parquet, 건너뛴 source 는 **원본 bytes 그대로**
      · **참조 항목이 없는 source 는 디코드조차 하지 않는다** (스키마만 읽어 판정)
      · manifest.selected_items 가 차 있으면 신규 이름을 더해 pack 을 만든다 (비면 그대로)
      · add_items / rows_preserved 를 서버에 보낸다
      · confirm_cb 가 거부하면 업로드하지 않는다
    """
    from excel_edit import excel_session

    items = ["VDD_A", "VDD_B"]
    frames = [make_frame(items, rows=6), make_frame(["VDD_A"], rows=6),
              make_frame(items, rows=6)]
    blobs = [encode_honeyform_parquet(f.copy()) for f in frames]
    titles = ["WF1", "WF2", "WF3"]

    class FakeZip:
        reads = []

        def read(self, filename):
            FakeZip.reads.append(filename)
            return blobs[int(filename[len("source_"):-len(".parquet")])]

    decoded = []
    real_decode = item_add.__dict__.get("_orig_decode")

    def fake_open_export(server_base, session_id, selected=("VDD_A", "VDD_B")):
        manifest = {"sources": [{"name": t} for t in titles],
                    "selected_items": list(selected)}
        entries = [(i, f"source_{i}.parquet", titles[i]) for i in range(3)]
        return FakeZip(), manifest, entries

    captured = {}

    def fake_upload(base, sid, parquets, kept_indices=None, dist_pack=None,
                    add_items=None, rows_preserved=False):
        captured.update(parquets=list(parquets), kept=kept_indices,
                        add_items=add_items, rows_preserved=rows_preserved,
                        pack=dist_pack)

    def fake_pack(parquet_list, titles_, manifest, emit=None):
        captured["pack_selected"] = list(manifest.get("selected_items") or [])
        captured["pack_titles"] = list(titles_)
        return {"index": "x", "chunks": {0: b"z"}}

    orig_open, orig_upload, orig_pack = (
        item_add._open_export, excel_session._upload_sources, excel_session._build_dist_pack)
    item_add._open_export = fake_open_export
    excel_session._upload_sources = fake_upload
    excel_session._build_dist_pack = fake_pack
    try:
        tokens = formula.normalize_tokens(T("VDD_A", "+", "VDD_B"))
        spec = {"name": NEW, "tseq": "99", "tno": "1999", "step": "P2",
                "unit": "", "hilim": "", "lolim": "", "tokens": tokens}

        FakeZip.reads = []
        result = item_add.add_item("sid", "http://x", spec)
        check(result["changed"] is True, result)
        check(result["skipped"] == ["WF2"], result)

        got = captured["parquets"]
        check(len(got) == 3, f"업로드 source 수 {len(got)}")
        check(got[1] == blobs[1], "건너뛴 source 가 원본 bytes 가 아니다")
        check(got[0] != blobs[0] and got[2] != blobs[2], "계산 대상이 그대로다")
        for pos in (0, 2):
            after = decode_split_honeyform_parquet(got[pos], source=titles[pos])
            check(after.item_columns == items + [NEW], after.item_columns)
        check(captured["add_items"] == [NEW], captured["add_items"])
        check(captured["rows_preserved"] is True, captured["rows_preserved"])
        check(captured["kept"] is None, captured["kept"])
        check(captured["pack_selected"] == items + [NEW], captured["pack_selected"])
        check(captured["pack_titles"] == titles, captured["pack_titles"])
        # zip 은 source 당 정확히 1회만 읽는다 (스키마 판정과 디코드가 같은 bytes 를 쓴다)
        check(sorted(FakeZip.reads) == [f"source_{i}.parquet" for i in range(3)],
              FakeZip.reads)
        print("  [ok] (i) add_item 배선 - 순서 보존 · 건너뛴 source 원본 bytes · "
              "add_items/rows_preserved 전송")

        # selected_items 가 비면 pack 도 빈 목록 그대로 (필터가 없다는 뜻)
        captured.clear()
        item_add._open_export = lambda b, s: fake_open_export(b, s, selected=())
        item_add.add_item("sid", "http://x", spec)
        check(captured["pack_selected"] == [], captured["pack_selected"])
        print("  [ok] (i) selected_items 가 비면 pack 도 그대로 (전 항목 선택)")

        # confirm 거부 -> 업로드 없음
        captured.clear()
        item_add._open_export = fake_open_export
        result = item_add.add_item("sid", "http://x", spec, confirm_cb=lambda payload: False)
        check(result["changed"] is False and not captured, (result, captured))
        print("  [ok] (i) 확인창 거부 -> 서버에 아무것도 보내지 않는다")

        # confirm payload 에 건너뛴 source 와 되돌릴 수 없음 경고가 들어간다
        seen = {}
        item_add.add_item("sid", "http://x", spec,
                          confirm_cb=lambda p: (seen.update(p=p), False)[1])
        warns = " ".join(seen["p"]["sections"][0]["warnings"])
        check("WF2" in warns and "되돌릴 수 없습니다" in warns, warns)
        check(seen["p"]["totals"]["sources"] == 2, seen["p"]["totals"])
        print("  [ok] (i) 확인 payload - 건너뛴 source · 되돌릴 수 없음 · 형제 세션 경고")
    finally:
        item_add._open_export = orig_open
        excel_session._upload_sources = orig_upload
        excel_session._build_dist_pack = orig_pack


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("[신규 item honeyform 왕복]")
    test_roundtrip()
    test_skipped_source()
    test_encode_guard()
    test_validate_meta()
    test_default_meta()
    test_add_item_flow()
    print("[통과] 컬럼 추가 - 메타/값/dtype/기존 컬럼 보존 계약 정상")


if __name__ == "__main__":
    main()

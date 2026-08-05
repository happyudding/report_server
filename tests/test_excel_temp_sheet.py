"""Honey Excel Download 의 "Issue Table Temp" 시트 순수 함수 검증 (2026-08-05).

실행:
    python tests/test_excel_temp_sheet.py

고정하는 계약:
  1. Temp 행이 있는 세션만 시트를 만든다(_sheet_order) — 다른 모드 파일은 시트 구성 불변.
  2. Map 썸네일 잡은 **항목별 fail die 인덱스** 기준이고, 같은 소스를 쓰는 항목끼리 묶인다
     (_build_temp_map_jobs). 웹 renderMiniTempCell 과 같은 "첫 CT/HT 소스" 선택.
  3. temp_map 수신 실패(빈 dict)면 Map 잡이 비고, 시트 생성 자체는 막지 않는다.
  4. _map._map_image 의 색 콜백이 die 인덱스를 받는다 — 인덱스 기반 강조의 전제.

xlwings/Excel 불필요 — 순수 함수만 호출한다. pytest 미사용 (tests/ 관례).
"""
from __future__ import annotations

import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "client"))

# excel_download 패키지 상단이 chart_colors(Qt 설정)·matplotlib(PNG 렌더)를 import 한다.
# 여기서 검증하는 것은 잡 구성·시트 순서 같은 **순수 로직**이라 둘 다 필요 없으므로,
# 없는 환경(서버 venv)에서도 돌도록 빈 스텁을 채운다(설치돼 있으면 진짜 모듈 사용).
try:
    import chart_colors  # noqa: F401
except Exception:
    _cc = types.ModuleType("chart_colors")
    _cc.load_colors = lambda: []
    sys.modules["chart_colors"] = _cc
try:
    import requests  # noqa: F401
except Exception:
    sys.modules["requests"] = types.ModuleType("requests")
try:
    import matplotlib  # noqa: F401
except Exception:
    _mpl = types.ModuleType("matplotlib")
    _mpl.use = lambda *a, **k: None
    _fig = types.ModuleType("matplotlib.figure")
    _fig.Figure = object
    _lines = types.ModuleType("matplotlib.lines")
    _lines.Line2D = object
    _patches = types.ModuleType("matplotlib.patches")
    _patches.Rectangle = object
    sys.modules.update({"matplotlib": _mpl, "matplotlib.figure": _fig,
                        "matplotlib.lines": _lines, "matplotlib.patches": _patches})

from excel_download import TEMP_SHEET, _build_temp_map_jobs, _sheet_order  # noqa: E402


def test_sheet_order_only_when_temp_rows():
    """Temp 행이 있을 때만 Issue Table 뒤에 시트를 끼운다."""
    base = _sheet_order({"Issue Table": [{}]})
    assert TEMP_SHEET not in base, base
    assert base == _sheet_order({}), "빈 시트 dict 도 종전 구성"
    assert base == _sheet_order({TEMP_SHEET: []}), "빈 배열이면 시트 없음"

    with_temp = _sheet_order({TEMP_SHEET: [{"Category": "TEMP"}, {"Item": "A"}]})
    assert TEMP_SHEET in with_temp, with_temp
    assert with_temp.index(TEMP_SHEET) == with_temp.index("Issue Table") + 1, with_temp
    # 나머지 순서는 그대로 (Temp 만 끼워 넣는다)
    assert [s for s in with_temp if s != TEMP_SHEET] == base, with_temp


def _maps():
    """소스 2개(STEP 분리 포함) — dies 길이·순서는 소스 안에서 동일하다는 계약."""
    dies = [{"x": i % 5, "y": i // 5, "bin": "1"} for i in range(10)]
    return [
        {"source": "W_CT", "step": "P1", "dies": dies},
        {"source": "W_CT", "step": "P2", "dies": dies},
        {"source": "W_HT", "step": "P1", "dies": dies},
    ]


def test_temp_map_jobs_group_by_source(tmpdir="."):
    """항목별 idx 로 잡을 만들고, 같은 소스 항목은 한 잡으로 묶는다."""
    temp_map = {"W_CT": {"ItemA": [0, 1, 2], "ItemB": [5]}, "W_HT": {"ItemC": [7, 8]}}
    targets = [("ItemA", 10), ("ItemB", 11), ("ItemC", 12)]
    jobs, paths = _build_temp_map_jobs(_maps(), targets, temp_map, tmpdir)

    assert set(paths) == {"ItemA", "ItemB", "ItemC"}, sorted(paths)
    assert len(jobs) == 2, f"소스 2개 → 잡 2개 (같은 소스 항목은 묶임): {len(jobs)}"
    by_items = {tuple(sorted(t[0] for t in j["targets"])): j for j in jobs}
    assert ("ItemA", "ItemB") in by_items and ("ItemC",) in by_items, sorted(by_items)
    # 잡에는 그 소스 dies 와 항목별 인덱스가 그대로 실린다
    ct = by_items[("ItemA", "ItemB")]
    assert len(ct["dies"]) == 10, len(ct["dies"])
    idx_of = {t[0]: t[1] for t in ct["targets"]}
    assert idx_of["ItemA"] == [0, 1, 2] and idx_of["ItemB"] == [5], idx_of


def test_temp_map_jobs_fallbacks(tmpdir="."):
    """temp_map 없음 / 맵 없는 소스 / 대상 없음 → 빈 결과 (시트는 계속 만든다)."""
    assert _build_temp_map_jobs(_maps(), [("ItemA", 10)], {}, tmpdir) == ([], {})
    assert _build_temp_map_jobs([], [("ItemA", 10)], {"W_CT": {"ItemA": [0]}}, tmpdir) == ([], {})
    assert _build_temp_map_jobs(_maps(), [], {"W_CT": {"ItemA": [0]}}, tmpdir) == ([], {})
    # temp_map 에는 있으나 Map Analysis 에 그 소스 맵이 없으면 그 항목만 건너뛴다
    jobs, paths = _build_temp_map_jobs(
        _maps(), [("ItemZ", 10)], {"W_ZZ": {"ItemZ": [0]}}, tmpdir)
    assert (jobs, paths) == ([], {}), (jobs, paths)


def test_map_image_color_callback_gets_index():
    """색 콜백이 (die, k) 로 호출된다 — 인덱스 기반 강조(temp)의 전제."""
    from excel_download._map import _map_image

    dies = [{"x": i, "y": 0, "bin": "1"} for i in range(4)]
    seen = []

    def color_of(die, k):
        seen.append(k)
        return "#ff0000" if k == 2 else "#eeeeee"

    img, xs, ys = _map_image(dies, color_of)
    assert img is not None and len(xs) == 4 and len(ys) == 1, (xs, ys)
    assert seen == [0, 1, 2, 3], seen


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    checks = 0
    for fn in (test_sheet_order_only_when_temp_rows,
               test_temp_map_jobs_group_by_source,
               test_temp_map_jobs_fallbacks,
               test_map_image_color_callback_gets_index):
        fn()
        checks += 1
    print(f"PASS: test_excel_temp_sheet ({checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

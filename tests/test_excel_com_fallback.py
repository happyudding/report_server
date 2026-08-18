"""COM 경로 + 자동 폴백 검증 — **Excel 설치 PC 에서만** 의미가 있다.

    python tests/test_excel_com_fallback.py

확인 2가지:
  (1) engine="com" — 리팩터링(_ComBook 어댑터) 후에도 기존 Excel 경로가 그대로 만들어지는가
  (2) 자동 폴백  — 신규 엔진이 실패해도 **파일은 반드시 만들어지는가**(사용자 요구)
Excel 이 없으면 skip 하고 성공으로 끝낸다(빌드 PC 마다 환경이 다르므로).
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "client"), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)


def excel_available():
    try:
        import win32com.client
        app = win32com.client.Dispatch("Excel.Application")
        app.Quit()
        return True
    except Exception:
        return False


def main():
    import multiprocessing
    multiprocessing.freeze_support()
    if not excel_available():
        print("SKIP — 이 PC 에 Excel 이 없어 COM 경로를 검증할 수 없습니다.")
        return 0

    import pythoncom
    pythoncom.CoInitialize()
    import test_excel_xlsxwriter as T
    from test_excel_xlsxwriter import Book

    fails = []
    tmp = tempfile.mkdtemp(prefix="com_test_")
    try:
        print("\n[1] engine='com' — 기존 Excel 경로 (리팩터링 회귀 확인)")
        res = T.build(tmp, n_items=12, out_name="com.xlsx", engine="com")
        print(f"   엔진={res['engine']} 소요={res['elapsed']:.1f}s "
              f"크기={os.path.getsize(res['out_path'])/1e6:.1f}MB")
        bk = Book(res["out_path"])
        print(f"   시트: {bk.sheets}")
        expect = ["Summary", "Yield", "CPK", "Issue Table",
                  "Distribution", "Histogram", "Map Analysis"]
        if bk.sheets != expect:
            fails.append(f"COM 시트 구성 {bk.sheets} != {expect}")
        if bk.n_images("Distribution") < 1:
            fails.append("COM Distribution 차트 없음")
        if bk.n_images("Map Analysis") < 1:
            fails.append("COM Map 이미지 없음")
        print(f"   이미지: dist={bk.n_images('Distribution')} "
              f"map={bk.n_images('Map Analysis')} issue={bk.n_images('Issue Table')}")

        print("\n[2] 자동 폴백 — 신규 엔진이 실패해도 파일은 만들어진다")
        from excel_download import _xlsx
        original = _xlsx.XlsxBook.add_sheets

        def boom(self, names):
            raise RuntimeError("신규 엔진 강제 실패(테스트)")
        _xlsx.XlsxBook.add_sheets = boom
        try:
            res2 = T.build(tmp, n_items=12, out_name="fallback.xlsx", engine="xlsxwriter")
        finally:
            _xlsx.XlsxBook.add_sheets = original
        print(f"   엔진={res2['engine']} 소요={res2['elapsed']:.1f}s 경고={len(res2['warnings'])}")
        for w in res2["warnings"]:
            print(f"     ! {w}")
        if not os.path.exists(res2["out_path"]):
            fails.append("폴백했는데 파일이 없다")
        if res2["engine"] != "com":
            fails.append(f"폴백 엔진 표기 오류: {res2['engine']}")
        if not any("XlsxWriter" in str(w) for w in res2["warnings"]):
            fails.append("폴백 사유가 경고에 남지 않았다")
        bk2 = Book(res2["out_path"])
        if "Summary" not in bk2.sheets:
            fails.append(f"폴백 파일이 비정상: {bk2.sheets}")
        print(f"   폴백 파일 시트: {bk2.sheets}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    print("\n" + "=" * 60)
    if fails:
        print(f"실패 {len(fails)}건")
        for f in fails:
            print("  - " + f)
        return 1
    print("전부 통과 — COM 경로 정상 + 신규 엔진 실패 시 파일이 반드시 만들어진다")
    return 0


if __name__ == "__main__":
    sys.exit(main())

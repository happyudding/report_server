"""파일 선택 GUI → testbench 실행(현재 콘솔 창에 결과 출력).

run_testbench.bat 를 더블클릭하면: (1) CSV 선택 다이얼로그가 뜨고 (2) 선택한 파일을
evaluate() 로 평가해 결과를 콘솔 창에 출력한다. tkinter(표준 라이브러리)만 사용 — PyQt 불필요.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

try:  # Windows 콘솔 한국어 출력
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()

    csv = filedialog.askopenfilename(
        title="평가할 CSV 파일 선택 (정본 raw_df 포맷)",
        initialdir=os.path.join(ROOT, "samples"),
        filetypes=[("CSV 파일", "*.csv"), ("모든 파일", "*.*")])
    if not csv:
        print("선택이 취소되었습니다.")
        root.destroy()
        return

    persist = messagebox.askyesno(
        "저장 여부",
        "결과를 임시 DB(data/eval_testbench.db)에 저장할까요?\n(아니오 = 미리보기만)")
    root.destroy()

    argv = [csv] + (["--persist"] if persist else [])
    print(f"입력 파일: {csv}")
    print(f"저장(persist): {persist}\n")

    import testbench_eval
    testbench_eval.main(argv)


if __name__ == "__main__":
    main()

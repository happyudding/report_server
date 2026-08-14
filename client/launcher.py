"""Honey 런처 — **업데이트를 먼저 끝내고** versions\\<ver>\\HoneyApp.exe 를 실행한다.

설치 루트(런처가 있는 폴더) 구조는 transport/app_update.py 주석 참조.
런처는 **자동 업데이트가 실행 중인 파일을 건드리지 않게** 하기 위한 고정점이다.
업데이트는 versions\\ 아래 새 폴더를 만들고 current.txt 만 바꾸므로, 런처 자신은
거의 바뀌지 않는다.

의존성은 표준 라이브러리 + transport.app_update 뿐이다 (PyQt6 등을 import 하면
런처가 수백 MB 가 된다). 진행창은 tkinter 로 그린다.

동작:
  1) --wait-pid <N> 이 있으면 그 프로세스가 끝날 때까지 최대 30초 기다린다.
  2) **업데이트 시도** — 서버에 새 버전이 있으면 진행창을 띄우고 받아서 설치한다.
     변경된 파일만 받고 나머지는 현재 버전 폴더에서 가져다 쓴다(델타).
  3) current.txt 를 읽어 실행 후보 목록을 만든다 (손상/누락이면 versions 스캔).
  4) 후보를 순서대로 실행한다. 15초 안에 비정상 종료하면 다음 후보로 넘어가고,
     current.txt 를 이전 버전으로 되돌린다 (= 자동 롤백).

**2단계는 어떻게 실패하든 3단계를 막지 않는다.** 업데이트는 부가 기능이고 앱이 뜨는
것이 본 기능이다 — try_update 는 모든 예외를 삼키고, current.txt 는 설치가 완전히
끝난 뒤에만 갱신한다.

--no-ui 는 자동 테스트 전용이다 (실패 안내창이 클릭을 기다리며 멈추지 않게).
이 플래그가 있으면 업데이트도 건너뛴다 — 진행창을 띄울 수 없는 환경이라는 뜻이다.
--skip-update / 루트의 noupdate.txt 는 현장 비상 탈출구다.
"""
import ctypes
import os
import re
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

APP_EXE_NAME = "HoneyApp.exe"
CURRENT_FILENAME = "current.txt"
VERSIONS_DIRNAME = "versions"
UPDATES_DIRNAME = "updates"
NOUPDATE_FILENAME = "noupdate.txt"
STARTUP_WATCH_SEC = 15.0     # 이 시간 살아 있으면 정상 기동으로 본다
WAIT_PARENT_SEC = 30.0
MAX_UPDATE_FAILS = 3         # 같은 버전에서 이만큼 연속 실패하면 그 버전은 건너뛴다
FAILURE_AUTORUN_SEC = 10     # 실패 안내창이 이 시간 뒤 자동으로 닫히고 앱이 뜬다
APP_WINDOW_WAIT_SEC = 90.0   # 기동 대기창을 최대 이만큼 띄워 둔다 (그 뒤엔 접는다)

# ── 브랜드 색 (꿀단지 = 노란 계열) ───────────────────────────────────────────
# 런처는 리소스 파일을 들고 다니지 않는다(onefile 이라 풀어 쓰는 비용이 있고, 아이콘
# 하나 때문에 spec 에 datas 를 늘리고 싶지 않다). 그래서 색과 도형으로만 꾸민다.
BG_CREAM   = "#FFFDF6"   # 창 배경
BG_BAND    = "#FFF4D6"   # 상단 띠
GOLD       = "#E8A317"   # 포인트(꿀)
GOLD_DARK  = "#C8860D"   # 뚜껑·윤곽
GOLD_BAR   = "#F0B429"   # 진행바
INK        = "#4A3A10"   # 본문 글자
INK_SUB    = "#8A7746"   # 보조 글자

UI_FONT = "Malgun Gothic"

# 업데이트 기능은 이 모듈에 있다. 없으면(개발 트리 밖으로 복사돼 실행되는 경우 등)
# 업데이트만 조용히 비활성화되고 실행 기능은 그대로 동작해야 한다.
try:
    from transport import app_update
except Exception:   # noqa: BLE001 - import 실패 사유가 무엇이든 실행은 계속한다
    app_update = None


def root_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def log(root: Path, message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [launcher {os.getpid()}] {message}"
    try:
        log_dir = root / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "launcher.log", "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def wait_for_pid(pid: int, timeout_sec: float) -> None:
    """해당 PID 가 끝날 때까지 대기. 이미 없으면 즉시 반환 (best-effort)."""
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return
    try:
        ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout_sec * 1000))
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def read_current(root: Path):
    # utf-8-sig: 사람이 메모장으로 고쳐 BOM 이 붙어도 첫 줄을 잃지 않는다
    # (BOM 이 붙으면 버전 문자열이 '﻿9.0.1' 이 돼 폴더를 못 찾는다).
    try:
        text = (root / CURRENT_FILENAME).read_text(encoding="utf-8-sig")
    except OSError:
        return None, None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cur = lines[0] if lines else None
    prev = lines[1] if len(lines) > 1 else None
    return cur, prev


def write_current(root: Path, current: str, previous=None) -> None:
    body = current if not previous else f"{current}\n{previous}"
    tmp = root / f".{CURRENT_FILENAME}.tmp-{os.getpid()}"
    tmp.write_text(body + "\n", encoding="utf-8")
    os.replace(tmp, root / CURRENT_FILENAME)


def installed_versions(root: Path):
    """실행 가능한 버전 폴더를 최신순으로 (current.txt 가 없거나 깨졌을 때의 폴백)."""
    out = []
    try:
        entries = list((root / VERSIONS_DIRNAME).iterdir())
    except OSError:
        return out
    for entry in entries:
        if (entry / APP_EXE_NAME).exists() and re.fullmatch(r"\d+(\.\d+)*", entry.name):
            out.append(entry.name)
    out.sort(key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    return out


def candidates(root: Path):
    """실행을 시도할 버전 순서. current → prev → 나머지 최신순."""
    cur, prev = read_current(root)
    order = []
    for ver in (cur, prev):
        if ver and ver not in order:
            order.append(ver)
    for ver in installed_versions(root):
        if ver not in order:
            order.append(ver)
    return [v for v in order if (root / VERSIONS_DIRNAME / v / APP_EXE_NAME).exists()]


def message_box(text: str, enabled: bool = True) -> None:
    """실패 안내창. enabled=False 는 자동 테스트용 — 안내창은 클릭할 때까지 막힌다."""
    if not enabled:
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Honey", 0x10)
    except Exception:
        pass


# ── 공용 UI 조각 ─────────────────────────────────────────────────────────────
def draw_honey_pot(canvas, cx, cy, scale=1.0):
    """꿀단지를 도형으로 그린다 (48x48 기준, cx/cy 가 중심).

    이미지 파일을 쓰지 않는 이유: tkinter 의 PhotoImage 는 .ico 를 못 읽고,
    이모지(U+1F36F)는 Tk 8.6 이 BMP 밖 문자라 제대로 못 그린다. 도형으로 그리면
    런처에 리소스를 넣지 않아도 되고 어떤 PC 에서도 똑같이 보인다.
    """
    def px(x, y):
        return cx + (x - 24) * scale, cy + (y - 24) * scale

    def box(x0, y0, x1, y1, **kw):
        a, b = px(x0, y0)
        c, d = px(x1, y1)
        return a, b, c, d, kw

    # 뚜껑 (윗면 타원 + 몸통)
    a, b, c, d, kw = box(8, 3, 40, 13, fill=GOLD_DARK, outline="")
    canvas.create_oval(a, b, c, d, **kw)
    a, b, c, d, kw = box(8, 8, 40, 16, fill=GOLD_DARK, outline="")
    canvas.create_rectangle(a, b, c, d, **kw)
    # 목
    a, b, c, d, kw = box(14, 15, 34, 20, fill="#D9950F", outline="")
    canvas.create_rectangle(a, b, c, d, **kw)
    # 몸통
    a, b, c, d, kw = box(6, 17, 42, 45, fill=GOLD, outline=GOLD_DARK, width=max(1, int(1.5 * scale)))
    canvas.create_oval(a, b, c, d, **kw)
    # 라벨 띠 + 글자
    a, b, c, d, kw = box(12, 27, 36, 37, fill="#FFF9E8", outline="")
    canvas.create_rectangle(a, b, c, d, **kw)
    tx, ty = px(24, 32)
    canvas.create_text(tx, ty, text="HONEY", fill=GOLD_DARK,
                       font=(UI_FONT, max(5, int(6 * scale)), "bold"))


def icon_path():
    """번들된 honey.ico 경로 (없으면 None).

    onefile 로 빌드되면 datas 는 sys._MEIPASS 아래 풀린다. 개발 실행이면 이 파일 옆.
    """
    base = getattr(sys, "_MEIPASS", None) or str(Path(__file__).resolve().parent)
    path = Path(base) / "honey.ico"
    return str(path) if path.exists() else None


def apply_window_icon(root):
    """타이틀바·작업표시줄 아이콘을 꿀단지로. 실패해도 창은 그대로 뜬다."""
    path = icon_path()
    if not path:
        return
    try:
        root.iconbitmap(path)
    except Exception:   # noqa: BLE001 - 아이콘이 없다고 업데이트를 멈출 이유는 없다
        pass


def build_shell(tk, root, title, heading):
    """창 뼈대(배경·상단 띠·꿀단지·제목)를 만들고 본문 프레임을 돌려준다.

    업데이트 진행창과 기동 대기창이 같은 얼굴을 갖게 하려고 한 곳에 모았다.
    """
    root.title(title)
    root.resizable(False, False)
    root.configure(bg=BG_CREAM)
    apply_window_icon(root)

    band = tk.Frame(root, bg=BG_BAND)
    band.pack(fill="x")
    inner = tk.Frame(band, bg=BG_BAND, padx=20, pady=14)
    inner.pack(fill="x")
    canvas = tk.Canvas(inner, width=52, height=52, bg=BG_BAND,
                       highlightthickness=0, bd=0)
    canvas.pack(side="left")
    draw_honey_pot(canvas, 26, 26, scale=1.05)
    tk.Label(inner, text=heading, bg=BG_BAND, fg=INK, anchor="w",
             font=(UI_FONT, 12, "bold")).pack(side="left", padx=(14, 0))

    body = tk.Frame(root, bg=BG_CREAM, padx=22, pady=16)
    body.pack(fill="both", expand=True)
    return body


def style_bar(ttk):
    """진행바를 꿀색으로. 기본 테마(vista)는 색을 무시하므로 clam 으로 바꾼다."""
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:   # noqa: BLE001 - 테마가 없으면 기본 모양 그대로 쓴다
        pass
    style.configure("Honey.Horizontal.TProgressbar",
                    troughcolor="#F2E7C7", background=GOLD_BAR,
                    bordercolor="#F2E7C7", lightcolor=GOLD_BAR, darkcolor=GOLD_BAR)
    return "Honey.Horizontal.TProgressbar"


def center_window(win, y_divisor=3):
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    x = (win.winfo_screenwidth() - w) // 2
    y = (win.winfo_screenheight() - h) // y_divisor
    win.geometry(f"+{x}+{y}")


# ── 업데이트 진행창 (tkinter) ────────────────────────────────────────────────
class ProgressWindow:
    """작은 진행창. 생성에 실패하면 예외가 나고, 호출부는 업데이트를 건너뛴다.

    tkinter 는 메인 스레드에서만 다룰 수 있으므로, 실제 작업은 워커 스레드가 하고
    이 창은 after() 폴링으로 갱신만 한다 ("응답 없음"으로 보이지 않게 하는 표준 패턴).
    """

    def __init__(self, version):
        import tkinter as tk
        from tkinter import ttk

        self._tk = tk
        self._cancelled = False
        self.root = tk.Tk()
        self.root.protocol("WM_DELETE_WINDOW", self.cancel)

        frame = build_shell(tk, self.root, "Honey 업데이트", "Honey 업데이트")
        self.label = tk.Label(frame, text=f"새 버전 {version} 확인 중...",
                              bg=BG_CREAM, fg=INK, anchor="w", justify="left",
                              font=(UI_FONT, 10))
        self.label.pack(fill="x")
        bar_style = style_bar(ttk)
        self.bar = ttk.Progressbar(frame, length=400, mode="indeterminate",
                                   maximum=100, style=bar_style)
        self.bar.pack(pady=(12, 6), fill="x")
        self.bar.start(12)
        self.sub = tk.Label(frame, text="", bg=BG_CREAM, fg=INK_SUB, anchor="w",
                            font=(UI_FONT, 9))
        self.sub.pack(fill="x")
        # 사용자가 가장 불안해하는 구간이라 "기다리면 된다"를 못박아 둔다 —
        # 여기서 Honey 를 다시 실행하면 업데이트 중인 폴더를 두 프로세스가 만진다.
        self.notice = tk.Label(
            frame, text="Honey 를 다시 실행하지 마시고 잠시만 기다려 주세요.",
            bg=BG_CREAM, fg=GOLD_DARK, anchor="w", justify="left",
            font=(UI_FONT, 9, "bold"))
        self.notice.pack(fill="x", pady=(10, 0))
        self.buttons = tk.Frame(frame, bg=BG_CREAM)
        self.buttons.pack(fill="x", pady=(14, 0))
        self.cancel_btn = tk.Button(self.buttons, text="취소", width=12,
                                    command=self.cancel, relief="flat",
                                    bg=BG_BAND, fg=INK, activebackground=GOLD_BAR,
                                    font=(UI_FONT, 9))
        self.cancel_btn.pack(side="right")
        self._indeterminate = True
        self._center()

    def _center(self):
        center_window(self.root)

    def cancel(self):
        self._cancelled = True
        self.sub.config(text="취소하는 중...")

    def cancelled(self) -> bool:
        return self._cancelled

    def set_progress(self, text, done=0, total=0):
        self.label.config(text=text)
        if total > 0:
            if self._indeterminate:
                self.bar.stop()
                self.bar.config(mode="determinate")
                self._indeterminate = False
            self.bar["value"] = min(100, int(done * 100 / total))
            self.sub.config(text=f"{done / 1048576:.0f} MB / {total / 1048576:.0f} MB")
        elif not self._indeterminate:
            self.bar["value"] = 0

    def show_failure(self, message, download_url, on_close):
        """실패 안내 + 카운트다운. 사용자가 아무것도 안 눌러도 앱은 반드시 뜬다."""
        self.bar.stop()
        self.bar.pack_forget()
        self.notice.pack_forget()   # 실패했으니 "기다려 주세요" 는 더 이상 맞지 않는다
        self.label.config(text=f"업데이트에 실패했습니다.\n{message}")
        self.sub.config(text="기존 버전으로 실행합니다. 설치파일을 직접 받아 "
                             "압축을 풀고 Honey.exe 를 실행해도 됩니다.")
        self.cancel_btn.config(text="지금 실행", command=on_close)
        if download_url:
            def _open():
                try:
                    webbrowser.open(download_url)
                except Exception:
                    pass
            self._tk.Button(self.buttons, text="설치파일 직접 받기", width=18,
                            command=_open, relief="flat", bg=BG_BAND, fg=INK,
                            activebackground=GOLD_BAR, font=(UI_FONT, 9)
                            ).pack(side="right", padx=(0, 8))
        countdown = {"left": FAILURE_AUTORUN_SEC}

        def tick():
            countdown["left"] -= 1
            if countdown["left"] <= 0:
                on_close()
                return
            self.cancel_btn.config(text=f"지금 실행 ({countdown['left']})")
            self.root.after(1000, tick)

        self.cancel_btn.config(text=f"지금 실행 ({countdown['left']})")
        self.root.after(1000, tick)
        self._center()

    def close(self):
        try:
            self.bar.stop()   # StartupWindow.close 와 같은 이유 (죽은 창 갱신 방지)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


# ── 앱 기동 대기창 ───────────────────────────────────────────────────────────
def app_window_visible(pid: int) -> bool:
    """그 PID 가 화면에 보이는 최상위 창을 가졌는가.

    런처가 창을 언제 접을지 판단하는 기준이다. 앱이 플래그 파일을 남기게 하는 방법도
    있지만, 그러면 **구버전 앱**(그 코드가 없는 versions\\ 폴더)을 실행할 때 영영
    신호가 오지 않는다. 창 존재는 앱 버전과 무관하게 통하므로 이쪽을 쓴다.

    숨은 창·크기 0 짜리 도우미 창을 세지 않으려고 가시성과 제목까지 본다
    (PyQt6 은 초기화 중 보이지 않는 최상위 창을 만들 수 있다).
    """
    user32 = ctypes.windll.user32
    # argtypes 를 반드시 지정한다 — 없으면 64비트에서 HWND 가 32비트로 잘려
    # GetWindowThreadProcessId 가 엉뚱한 값을 돌려주고 감지가 항상 실패한다.
    proc_cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [proc_cb, ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_ulong)]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    found = []

    @proc_cb
    def _cb(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
            return False        # 하나 찾으면 그만 본다
        return True

    try:
        user32.EnumWindows(_cb, None)
    except Exception:   # noqa: BLE001 - 감지 실패가 실행을 막으면 안 된다
        return False
    return bool(found)


class StartupWindow:
    """앱이 화면에 뜰 때까지 자리를 지키는 작은 창.

    업데이트 진행창이 닫힌 뒤 HoneyApp.exe(PyQt6+WebEngine)가 실제로 그려지기까지
    수 초~수십 초가 비는데, 그 사이 화면에 아무것도 없어 사용자가 런처를 다시 눌렀다.
    이 창이 그 빈틈을 덮는다 — 앱 창이 보이는 순간 닫으므로 두 화면이 이어진다.
    """

    def __init__(self, version):
        import tkinter as tk
        from tkinter import ttk

        self.root = tk.Tk()
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)   # 실수로 닫지 못하게

        frame = build_shell(tk, self.root, "Honey", "Honey 를 준비하고 있습니다")
        tk.Label(frame, text="Honey UI 구동 중입니다. 잠시만 기다려 주세요.",
                 bg=BG_CREAM, fg=INK, anchor="w", font=(UI_FONT, 10)).pack(fill="x")
        self.bar = ttk.Progressbar(frame, length=400, mode="indeterminate",
                                   maximum=100, style=style_bar(ttk))
        self.bar.pack(pady=(12, 6), fill="x")
        self.bar.start(12)
        tk.Label(frame, text=f"버전 {version}", bg=BG_CREAM, fg=INK_SUB,
                 anchor="w", font=(UI_FONT, 9)).pack(fill="x")
        tk.Label(frame, text="Honey 를 다시 실행하지 마세요. 창이 곧 나타납니다.",
                 bg=BG_CREAM, fg=GOLD_DARK, anchor="w",
                 font=(UI_FONT, 9, "bold")).pack(fill="x", pady=(10, 0))
        center_window(self.root)

    def wait_until(self, proc, timeout_sec, logf):
        """앱 창이 뜨거나 프로세스가 죽을 때까지 창을 띄운 채 기다린다.

        반환 'window'(창 떴음) | 'exited'(먼저 종료됨) | 'timeout'.
        tkinter 를 계속 돌려야 "응답 없음" 으로 흐려지지 않으므로 after 폴링을 쓴다.
        """
        import time as _time

        outcome = {"how": "timeout"}
        deadline = _time.monotonic() + timeout_sec

        def poll():
            if proc.poll() is not None:
                outcome["how"] = "exited"
                self.close()
                return
            if app_window_visible(proc.pid):
                outcome["how"] = "window"
                self.close()
                return
            if _time.monotonic() >= deadline:
                logf(f"app window not detected in {timeout_sec:.0f}s — 대기창을 닫는다")
                self.close()
                return
            self.root.after(200, poll)

        self.root.after(300, poll)
        self.root.mainloop()
        return outcome["how"]

    def close(self):
        # 애니메이션을 먼저 멈춘다 — indeterminate 진행바는 after 로 다음 프레임을
        # 예약해 두므로, 그냥 destroy 하면 그 콜백이 죽은 창을 건드려 Tcl 오류를 뱉는다.
        try:
            self.bar.stop()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass


def wait_for_app_window(proc, version, timeout_sec, logf, show_ui=True):
    """기동 대기창을 띄우고 결과를 돌려준다. 창을 못 만들면 조용히 폴백한다."""
    if not show_ui:
        return "skipped"
    try:
        window = StartupWindow(version)
    except Exception as exc:   # noqa: BLE001 - 창이 안 떠도 앱 실행은 계속돼야 한다
        logf(f"startup window unavailable ({type(exc).__name__}: {exc})")
        return "skipped"
    return window.wait_until(proc, timeout_sec, logf)


# ── 업데이트 ────────────────────────────────────────────────────────────────
def _apply_update(root, base_url, remote, manifest, current, progress, logf):
    """실제 설치. 델타를 먼저 시도하고 안 되면 전체 zip. 반환: 'delta' | 'full'.

    current.txt 는 여기서 건드리지 않는다 — 호출부가 성공을 확인한 뒤 마지막에 바꾼다.
    """
    source_dir = root / VERSIONS_DIRNAME / current if current else None
    local_files = app_update.read_file_manifest(source_dir) if source_dir else None

    if local_files:
        try:
            remote_files = app_update.fetch_file_manifest(base_url, remote)
            app_update.install_delta(
                root, remote, base_url, remote_files, source_dir, local_files,
                progress_cb=lambda d, t: progress(f"새 버전 {remote} 내려받는 중...", d, t))
            return "delta"
        except app_update.DownloadCancelled:
            raise
        except Exception as exc:   # noqa: BLE001 - 델타는 최적화일 뿐, 실패하면 전체로
            logf(f"delta unavailable ({type(exc).__name__}: {exc}) -> full zip")

    enough, free_mb, need_mb = app_update.check_disk(root, manifest.get("size"))
    if not enough:
        raise RuntimeError(f"디스크 공간 부족 (여유 {free_mb}MB / 필요 {need_mb}MB)")

    package = manifest.get("file") or f"Honey-{remote}.zip"
    dest = root / UPDATES_DIRNAME / package
    url = manifest.get("url") or "/honey/download"
    if not url.startswith("http"):
        url = f"{base_url.rstrip('/')}{url if url.startswith('/') else '/' + url}"
    try:
        app_update.download(url, dest, manifest.get("sha256"),
                            lambda d, t: progress(f"새 버전 {remote} 내려받는 중...", d, t))
        app_update.install_version(
            root, remote, dest,
            lambda d, t: progress(f"새 버전 {remote} 설치 중...", d, t))
    finally:
        dest.unlink(missing_ok=True)   # 성공이든 실패든 받은 zip 은 남기지 않는다
    return "full"


def _update_with_ui(root, base_url, remote, manifest, current, logf):
    """진행창을 띄우고 워커 스레드로 설치. 반환 (성공?, 실패 메시지, 취소?)."""
    import queue
    import threading

    ui = ProgressWindow(remote)   # 실패하면 예외 → 호출부가 업데이트를 건너뛴다
    events = queue.Queue()
    outcome = {"ok": False, "error": "", "cancelled": False}

    def progress(text, done, total):
        events.put(("progress", text, done, total))
        return not ui.cancelled()

    def worker():
        try:
            mode = _apply_update(root, base_url, remote, manifest, current, progress, logf)
            events.put(("done", mode))
        except BaseException as exc:   # noqa: BLE001 - 무엇이 터지든 UI 로 전달만 한다
            events.put(("error", exc))

    threading.Thread(target=worker, daemon=True).start()

    def finish():
        ui.close()

    def poll():
        item = None
        try:
            while True:
                item = events.get_nowait()
                if item[0] == "progress":
                    ui.set_progress(item[1], item[2], item[3])
                    item = None
                else:
                    break
        except queue.Empty:
            pass
        if item is None:
            ui.root.after(100, poll)
            return
        if item[0] == "done":
            outcome["ok"] = True
            logf(f"update installed ({item[1]}) -> {remote}")
            finish()
            return
        exc = item[1]
        if isinstance(exc, (app_update.DownloadCancelled, app_update.InstallCancelled)):
            outcome["cancelled"] = True
            logf("update cancelled by user")
            finish()
            return
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        logf(f"update FAILED {outcome['error']}")
        ui.show_failure(str(exc)[:200], f"{base_url.rstrip('/')}/honey/download", finish)

    ui.root.after(100, poll)
    ui.root.mainloop()
    return outcome


def try_update(root, argv, show_ui=True):
    """앱을 띄우기 전에 업데이트한다. **어떤 실패도 밖으로 내보내지 않는다.**

    업데이트는 부가 기능이고 앱이 뜨는 것이 본 기능이다 — 여기서 예외가 새어나가면
    사용자는 일을 아예 못 하게 된다.
    """
    def logf(message):
        log(root, message)

    try:
        if app_update is None:
            return
        if not show_ui or "--skip-update" in argv:
            logf("update skipped (--skip-update / --no-ui)")
            return
        if (root / NOUPDATE_FILENAME).exists():
            logf(f"update skipped ({NOUPDATE_FILENAME})")
            return

        current, _prev = read_current(root)
        base_url = app_update.read_server_url(root, current)

        # 받기 전에 판정한다 — 331MB 를 다 받고 나서 쓰기 실패로 끝나면 최악이다.
        if not app_update.can_write(root):
            logf(f"update skipped: 설치 폴더에 쓸 수 없음 ({root})")
            app_update.report_failure(
                base_url, "설치 폴더에 쓸 수 없습니다 (관리자 권한 필요)",
                {"stage": "permission", "root": str(root)}, current or "")
            return

        try:
            manifest = app_update.fetch_manifest(base_url)
        except Exception as exc:   # noqa: BLE001 - 오프라인은 흔한 정상 상황이다
            logf(f"version check skipped ({type(exc).__name__}: {exc})")
            return

        remote = str(manifest.get("version") or "")
        if not app_update.is_newer(remote, current):
            app_update.clear_fail_count(root)
            return

        fails = app_update.read_fail_count(root, remote)
        if fails >= MAX_UPDATE_FAILS:
            logf(f"update skipped: {remote} 연속 {fails}회 실패 — 더 시도하지 않는다")
            return

        logf(f"update {current} -> {remote} 시작")
        outcome = _update_with_ui(root, base_url, remote, manifest, current, logf)

        if outcome["ok"]:
            write_current(root, remote, current)     # 성공한 뒤에만 포인터를 바꾼다
            app_update.clear_fail_count(root)
            logf(f"current -> {remote} (prev {current})")
        elif outcome["error"]:
            count = app_update.bump_fail_count(root, remote)
            app_update.report_failure(
                base_url, outcome["error"],
                {"target": remote, "attempt": count}, current or "")
    except BaseException as exc:   # noqa: BLE001 - 여기서 막지 못하면 앱이 안 뜬다
        try:
            logf(f"update aborted ({type(exc).__name__}: {exc})")
        except Exception:
            pass


def main(argv) -> int:
    root = root_dir()
    show_ui = "--no-ui" not in argv

    if "--wait-pid" in argv:
        try:
            pid = int(argv[argv.index("--wait-pid") + 1])
        except (IndexError, ValueError):
            pid = 0
        if pid:
            log(root, f"wait for pid {pid}")
            wait_for_pid(pid, WAIT_PARENT_SEC)
    else:
        # --wait-pid 로 온 것은 방금 업데이트를 마치고 재실행된 경우다 (다시 확인할 필요 없다).
        try_update(root, argv, show_ui)

    order = candidates(root)
    if not order:
        log(root, f"no runnable version under {root / VERSIONS_DIRNAME}")
        message_box(f"실행할 Honey 버전을 찾지 못했습니다.\n\n{root / VERSIONS_DIRNAME}\n\n"
                    "설치 파일(zip)을 다시 풀어 주세요.", show_ui)
        return 1

    cur, _prev = read_current(root)
    for index, version in enumerate(order):
        exe = root / VERSIONS_DIRNAME / version / APP_EXE_NAME
        log(root, f"launch {version}")
        try:
            proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
        except OSError as exc:
            log(root, f"launch failed {version}: {exc}")
            continue

        # 앱이 화면에 그려질 때까지 대기창으로 빈틈을 덮는다 (PyQt6+WebEngine 기동은
        # 수 초~수십 초). 창이 보이는 순간 대기창이 닫혀 두 화면이 이어진다.
        how = wait_for_app_window(proc, version, APP_WINDOW_WAIT_SEC,
                                  lambda m: log(root, m), show_ui)
        if how == "window":
            log(root, f"ok {version} (window shown)")
            return 0
        if how == "exited":
            rc = proc.poll()
            if rc is None:
                rc = proc.wait()
        else:
            # 창을 못 봤다(감지 실패·타임아웃·--no-ui) — 종전 판정으로 돌아간다:
            # 일정 시간 살아 있으면 정상 기동으로 본다.
            try:
                rc = proc.wait(timeout=STARTUP_WATCH_SEC)
            except subprocess.TimeoutExpired:
                log(root, f"ok {version} (running)")
                return 0
        if rc == 0:
            log(root, f"ok {version} (exited 0)")
            return 0
        # 기동 직후 비정상 종료 = 이 버전이 깨졌다. 다음 후보로 내려가면서
        # current.txt 도 그 후보로 되돌려 다음 실행부터 바로 정상 버전이 뜨게 한다.
        log(root, f"crash {version} rc={rc}")
        fallback = order[index + 1] if index + 1 < len(order) else None
        if fallback and version == cur:
            try:
                write_current(root, fallback, None)
                log(root, f"rollback current -> {fallback}")
            except OSError as exc:
                log(root, f"rollback failed: {exc}")

    message_box("Honey 를 실행하지 못했습니다.\n\n"
                f"자세한 내용: {root / 'log' / 'launcher.log'}", show_ui)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

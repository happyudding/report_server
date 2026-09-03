"""Honey 런처 — **업데이트를 먼저 끝내고** versions\\<ver>\\HoneyApp.exe 를 실행한다.

설치 루트(런처가 있는 폴더) 구조는 transport/app_update.py 주석 참조.
런처는 버전 전환과 실패 롤백을 담당하는 고정점이다. 실행 중인 Honey가 업데이트를
막으면 사용자 동의를 받아 종료한 뒤 새 버전을 설치한다. 업데이트는 versions\\ 아래
새 폴더를 직접 채우고 검증된 뒤 current.txt 만 바꾼다.

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
import json
import os
import random
import re
import subprocess
import sys
import time
import webbrowser
from ctypes import wintypes
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
ELEVATED_TIMEOUT_SEC = 900   # 승격 업데이트가 이 시간 안에 안 끝나면 기존 버전으로 간다

# 런처 빌드 식별자 — .update_fail 기록에 함께 남는다. **런처를 고쳐 배포하면 이 값을
# 올려라**: 과거의 "3회 실패로 포기" 기록이 자동으로 풀려, 그 버그로 멈춰 있던 PC 가
# 새 런처를 받는 즉시 다시 시도한다 (transport/app_update.read_fail_count).
LAUNCHER_BUILD = "2026.09.03-gate-on-runnable"

# ── 대기 중 보여 줄 한 줄 문구 ──────────────────────────────────────────────
# 진행창은 길면 수 분을 서 있는다. 그동안 화면이 고정돼 있어 지루하다는 피드백을
# 받아, 기다리는 동안 한 줄씩 보여 준다. 브랜드 색과 같은 이유로 리소스 파일이
# 아니라 코드 상수다 (onefile 이라 datas 를 늘리지 않는다).
QUIP_ROTATE_MS = 7000    # 문구 교체 주기

QUIPS = (
    "아기 공룡 둘리가 입학한 고등학교는? 빙하타고",
    "세상에서 가장 쎈 대학교 이름은? 와세다 대학",
    "가장 지루하고 지겨운 중학교 이름은? 로딩중",
    "해리포터는 어떤 사람일까? 해를 취재하는 사람",
    "우리 몸에 좋지 않은 청바지 이름은? 유해진",
    "도둑놈이 가장 좋아하는 아이스크림은? 보석바",
    "이 세상에서 가장 뜨거운 과일은? 천도복숭아",
    "반성문을 영어로 표현하면? 글로벌",
    "우리나라에서 가장 오래된 화장실은? 전봇대",
    "아픈 사람들이 원하는 반지는? 힐링",
    "서울에서 조금 뚱뚱한 사람들이 사는 동네는? 반포동",
    "아이스크림이 죽다를 네 자로 줄이면? 다이하드",
    "김밥이 경찰서에 간 이유는? 참기름이 고소해서",
    "김밥이 죽으면 어디로 갈까? 김밥천국",
    "바나나가 웃으면? 바나나킥",
    "아버지가 정말 강한 사람이다를 세 자로? 부가세",
    "할아버지 할머니가 좋아하는 폭포는? 나이야가라",
    "할아버지가 좋아하는 돈은? 할머니",
    "조금 전에 울다 그친 사람을 다섯 자로? 아까운사람",
    "한의사가 가장 좋아하는 말은? 인생은 한방이여",
    "먹으면 자연스럽게 웃게 되는 죽은? 히죽",
    "이 세상에서 가장 쉬운 숫자는? 190000 (쉽구만)",
    "도둑놈이 훔치는 돈을 뭐라고 할까? 슬그머니",
    "채소 장수가 가장 싫어하는 도시는? 시드니",
    "사람의 머리가 세 개 있다를 영어로? 헤드셋",
    "인디언 추장보다 높은 사람은? 고추장",
    "어부들이 싫어하는 가수는? 배철수",
    "많은 모자가 뭉쳐 있는 것을 네 자로? 밀짚모자",
    "제일 억울한 도형은? 원통",
    "모든 사람들을 일어나게 하는 숫자는? 5 (다~섯)",
    "치과 의사가 살지 않는 아파트는? 이편한세상",
    "세계에서 가장 인기 있는 벌레는? 스타벅스",
    "지방 흡입의 반대말은? 수도권배출",
    "소금의 유통기간은? 천일 (천일염)",
    "이 세상에서 가장 잔인한 비빔밥은? 산채비빔밥",
    "고등학생이 가장 싫어하는 나무는? 야자나무",
    "비가 한 시간 동안 내린다를 다섯 자로? 추적육십분",
    "승용차 문을 세게 닫으면 안 되는 이유는? 문에 네개니까",
    "미소의 반대말은? 당기소",
    "몸무게가 가장 많이 나갈 때는? 철들 때",
    "자동차를 발로 차다를 네 자로? 카놀라유",
    "단무지가 버스를 타면서 하는 말은? 저 무임 승차요",
    "전화를 가지고 세운 건물의 이름은? 콜로세움",
    "신데렐라가 잠을 못 잔다를 네 자로? 모짜렐라",
    "신이 화를 내고 있다를 세 자로? 신발끈",
    "우리나라에서 땅값이 가장 싼 동네는? 일원동",
    "술과 커피를 팔지 않습니다를 사자성어로? 주차금지",
    "송혜교 송대관 송윤아 송중기의 공통점은? 성동일",
    "한국에서 가장 싸움을 잘하는 오리는? 을지문덕",
    "딱 세 사람밖에 탈 수 없는 차는? 인삼차",
    "일본산 귤이 까먹으라고 하는 말은? 나까무라",
    "새우가 주인공인 드라마는? 대하드라마",
    "호랑이가 새 차를 탄 여자에게 하는 말은? 타이거",
    "못 팔고도 돈을 잘 버는 사람은? 철물점 아저씨",
    "곰돌이 푸가 세 마리 있으면? 삼푸",
    "얼음이 죽다를 세 자로 줄이면? 다이빙",
    "왼쪽으로 절하는 것을 뭐라고 할까? 좌절",
    "신발 한 통에 오천원, 두 통엔? 게보린",
    "곰은 사과를 어떻게 먹을까? 베어먹지",
    "돌잔치를 하다를 영어로 하면? 락페스티벌",
    "세상에서 가장 뜨거운 바다는? 열받아",
    "바람이 가볍게 부는 동네는? 분당",
    "손가락은 핑거, 주먹은? 오므린거",
    "아주 오래전에 건설된 다리는? 구닥다리",
    "침대를 밀고 돌리다를 네 자로? 배드민턴",
    "우리나라 왕 중에 성형한 왕은? 인조임금",
    "세상에서 돈을 가장 많이 가진 새는? 백조",
    "복 중에서 가장 작은 복은? 복분자",
    "땅이 슬프면 어떻게 울까? 흙흙",
    "베를린에서 밥을 먹으면 안 되는 이유는? 독일수도",
    "파 중에서 가장 인기 있는 파는? 파스타",
    "빵이 시골에 가는 이유는? 소보로",
    "담배가 시골에 가는 이유는? 말보로",
    "미국에서 비가 온다를 영어로 하면? usb",
    "과자가 자기를 소개하며 하는 말은? 저는 전과자입니다",
    "항상 미안한 마음으로 사는 동물은? 오소리 (오, 쏘리)",
    "이 세상에서 가장 뜨거운 전화는? 화상전화",
    "우리나라에서 가장 바쁜 대학교는? 부산대학교",
    "호주의 화폐단위는? 호주머니",
    "머리 아플 때 약을 얼마나 먹어야 할까? 두통",
    "아마존에는 누가 살고 있을까? 아마... 존이",
    "이 세상에서 가장 가난한 임금은? 최저임금",
    "성적이 나와도 말하지 못하는 이유는? 내성적이라서",
    "달걀을 팔아서 번 돈을 뭐라고 할까? 에그머니",
    "가장 싼 사냥 도구는? 파리채",
    "개가 사람을 가르치다를 사자성어로? 개인지도",
    "사우디 석유가 우리나라까지 오는 기간은? 오일",
    "식인종이 우사인 볼트를 보면 하는 말은? 패스트푸드",
    "말이 분노하고 있다를 네 자로? 마리화나",
    "동생이 형을 너무 좋아한다를 세 자로? 형광펜",
    "인천 앞바다의 반대말은? 인천 엄마다",
    "세상에서 가장 무서운 전화는? 무선전화",
    "천 리 길도 한 걸음부터.",
    "시작이 반이다. 나머지 반은 끝내는 것이다.",
    "오늘 걷지 않으면 내일은 뛰어야 한다.",
    "느리게 가도 좋다. 멈추지만 않으면 된다.",
    "가장 어두운 시간은 해 뜨기 직전이다.",
    "실패는 넘어지는 것이 아니라 일어나지 않는 것이다.",
    "완벽한 때는 오지 않는다. 지금이 그때다.",
    "어제의 나보다 나아지면 그것으로 충분하다.",
    "작은 일에 최선을 다하면 큰 일이 따라온다.",
    "기다림도 하나의 일이다.",
    "쉬는 것은 게으름이 아니라 준비다.",
    "길이 없으면 만들면서 가면 된다.",
    "포기하고 싶을 때가 조금 남았다는 뜻이다.",
    "비교는 나를 갉아먹고, 성장은 나를 채운다.",
    "결과보다 방향이 중요하다.",
    "매일 조금씩이 어느 날 전부가 된다.",
    "잘하는 것보다 계속하는 것이 어렵다.",
    "지금의 수고는 나중의 여유가 된다.",
    "생각이 길어질수록 시작은 멀어진다.",
    "노력은 배신하지 않는다, 다만 늦게 온다.",
    "좋은 하루는 좋은 마음에서 시작된다.",
    "서두르지 말되 멈추지도 말라.",
    "오늘 한 걸음이 내일의 지름길이다.",
    "누구에게나 처음은 서툴다.",
    "잘 쉰 사람이 멀리 간다.",
    "할 수 있다고 믿는 순간 절반은 이룬 것이다.",
    "어려움은 지나가고 경험은 남는다.",
    "지금 하는 일이 미래의 나를 만든다.",
    "묵묵함이 가장 큰 재능이다.",
    "때로는 기다리는 것이 최선의 행동이다.",
    "실수는 배움의 다른 이름이다.",
    "큰 산도 한 삽씩 옮긴다.",
    "마음이 급할수록 천천히 하라.",
    "끝까지 가 본 사람만 아는 풍경이 있다.",
    "오늘의 인내가 내일의 자유다.",
    "성실함은 재능을 이긴다.",
    "잘 되는 날도, 안 되는 날도 모두 지나간다.",
    "무엇을 하든 즐길 수 있다면 이미 성공이다.",
    "충분히 잘하고 있다.",
    "한 번에 하나씩이면 못 할 일이 없다.",
    "고민의 시간이 곧 성장의 시간이다.",
    "괜찮다, 아직 시간은 많다.",
    "가장 좋은 계획은 지금 시작하는 것이다.",
    "지치면 쉬어라. 포기하지만 않으면 된다.",
    "남과 다른 속도로 가도 목적지는 같다.",
    "오늘도 무사히, 그것으로 충분하다.",
    "습관이 쌓이면 실력이 된다.",
    "조급함은 실수를 부르고 여유는 답을 준다.",
    "누구도 처음부터 잘하지는 않았다.",
    "끝내는 사람이 결국 이긴다.",
)

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
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject(handle, int(timeout_sec * 1000))
    finally:
        kernel32.CloseHandle(handle)


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


def _process_image_path(pid):
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def running_honey_processes(root):
    """이 설치 루트에서 실행 중인 Honey/Qt 프로세스를 역할과 함께 돌려준다."""
    if os.name != "nt":
        return []
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    root_text = os.path.normcase(str(Path(root).resolve())).rstrip("\\") + "\\"
    roles = {"honey.exe": "launcher", "honeyapp.exe": "app",
             "qtwebengineprocess.exe": "helper"}
    # onefile 부트로더 부모는 자식(=이 코드)이 끝날 때까지 같은 Honey.exe 경로로
    # 살아 있다 — 부모를 "다른 런처"로 오인하면 모든 실행이 자기 자신에게 막힌다.
    self_pids = {os.getpid(), os.getppid()}
    found = []
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            name = str(entry.szExeFile)
            name_lower = name.lower()
            if pid not in self_pids and name_lower in roles:
                image_path = _process_image_path(pid)
                normalized = os.path.normcase(image_path)
                if normalized.startswith(root_text):
                    found.append({"pid": pid, "name": name, "path": image_path,
                                  "role": roles[name_lower]})
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def process_roles(processes):
    """프로세스 목록을 실제 앱/다른 런처/Qt 보조 프로세스로 나눈다."""
    apps = [p for p in processes if p.get("role") == "app"]
    launchers = [p for p in processes if p.get("role") == "launcher"]
    helpers = [p for p in processes if p.get("role") == "helper"]
    return apps, launchers, helpers


def _process_names(processes):
    return ", ".join(f"{p['name']}({p['pid']})" for p in processes[:8])


def _post_close_to_processes(pids):
    if not pids:
        return
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                               ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]

    @callback_type
    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in pids:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return True

    user32.EnumWindows(callback, 0)


def activate_existing_honey(processes):
    """이미 실행 중인 HoneyApp 창을 복원하고 앞으로 가져온다."""
    pids = {p["pid"] for p in processes if p.get("role") == "app"}
    if not pids or os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_ulong)]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    found = []

    @callback_type
    def callback(hwnd, _lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if (pid.value in pids and user32.IsWindowVisible(hwnd)
                and user32.GetWindowTextLengthW(hwnd) > 0):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            found.append(hwnd)
            return False
        return True

    try:
        user32.EnumWindows(callback, None)
    except Exception:   # noqa: BLE001 - 창 활성화 실패가 실행 판정을 깨면 안 된다
        return False
    return bool(found)


def _terminate_process(pid):
    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def cleanup_orphan_helpers(root, helpers, logf):
    """본체 없이 남은 같은 설치 루트의 Qt 보조 프로세스를 정리한다."""
    pids = {p["pid"] for p in helpers}
    if not pids:
        return True
    logf(f"고아 Qt 보조 프로세스 정리 시작: {_process_names(helpers)}")
    _post_close_to_processes(pids)
    deadline = time.monotonic() + 0.5
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        current = running_honey_processes(root)
        apps, launchers, current_helpers = process_roles(current)
        if apps or launchers:
            logf("고아 Qt 정리 중 새 Honey 프로세스를 발견해 종료를 중단한다")
            return False
        remaining = {p["pid"] for p in current_helpers} & pids

    for pid in remaining:
        if _terminate_process(pid):
            logf(f"고아 Qt 보조 프로세스 강제 종료 pid={pid}")

    deadline = time.monotonic() + 2
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        current = running_honey_processes(root)
        apps, launchers, current_helpers = process_roles(current)
        if apps or launchers:
            logf("고아 Qt 정리 확인 중 새 Honey 프로세스를 발견했다")
            return False
        remaining = {p["pid"] for p in current_helpers} & pids
    if remaining:
        logf(f"고아 Qt 보조 프로세스 종료 실패 pids={sorted(remaining)} — 실행은 계속한다")
        return False
    logf("고아 Qt 보조 프로세스 정리 완료")
    return True


def wait_for_other_launcher(root, logf, show_ui=True, timeout_sec=3.0):
    """다른 런처가 인계할 시간을 주고, 계속 살아 있으면 중복 실행을 막는다."""
    processes = running_honey_processes(root)
    _apps, launchers, _helpers = process_roles(processes)
    if not launchers:
        return processes, False

    logf(f"다른 Honey 런처 대기: {_process_names(launchers)}")
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        time.sleep(0.25)
        processes = running_honey_processes(root)
        _apps, launchers, _helpers = process_roles(processes)
        if not launchers:
            return processes, False

    logf(f"다른 Honey 런처가 계속 실행 중: {_process_names(launchers)}")
    information_box(
        "다른 Honey 실행 또는 업데이트가 진행 중입니다.\n\n"
        "잠시 기다린 뒤 다시 실행해 주세요.", show_ui)
    return processes, True


def handle_running_without_update(root, logf, show_ui=True, launcher_wait_sec=3.0):
    """업데이트하지 않는 경로에서 중복 실행만 막고 고아 Qt는 통과시킨다."""
    processes, blocked = wait_for_other_launcher(
        root, logf, show_ui, timeout_sec=launcher_wait_sec)
    if blocked:
        return False

    apps, _launchers, helpers = process_roles(processes)
    if apps:
        names = _process_names(apps)
        if activate_existing_honey(apps):
            logf(f"이미 실행 중인 Honey 창 활성화: {names}")
        else:
            logf(f"Honey가 이미 실행 중이며 표시할 창을 찾지 못했다: {names}")
            information_box(
                "Honey가 이미 실행 중입니다.\n\n"
                "작업 표시줄이나 시스템 트레이를 확인해 주세요.", show_ui)
        return False

    if helpers:
        cleanup_orphan_helpers(root, helpers, logf)
    return None


def _confirm_close_running_honey(text):
    return ctypes.windll.user32.MessageBoxW(None, text, "Honey 업데이트", 0x34) == 6


def ask_and_close_running_honey(root, processes, logf, show_ui=True):
    """업데이트 전 실행 중 Honey를 사용자 동의 후 종료. 거부/실패면 False."""
    apps, _launchers, helpers = process_roles(processes)
    if not apps:
        if helpers:
            cleanup_orphan_helpers(root, helpers, logf)
        return True
    names = _process_names(apps)
    if not show_ui:
        logf(f"update 보류: 실행 중 Honey {names}")
        return False
    text = ("업데이트를 위해 실행 중인 Honey를 종료해야 합니다.\n\n"
            f"{names}\n\n"
            "저장하지 않은 작업은 사라질 수 있습니다.\n"
            "종료하고 업데이트하시겠습니까?")
    if not _confirm_close_running_honey(text):
        logf("사용자가 실행 중 Honey 종료를 선택하지 않았다")
        return False

    app_pids = {p["pid"] for p in apps}
    _post_close_to_processes(app_pids)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = running_honey_processes(root)
        current_apps, _launchers, current_helpers = process_roles(current)
        if not current_apps:
            cleanup_orphan_helpers(root, current_helpers, logf)
            logf("실행 중 Honey 정상 종료 완료")
            return True
        time.sleep(0.25)

    current = running_honey_processes(root)
    current_apps, _launchers, current_helpers = process_roles(current)
    remaining = {p["pid"] for p in current_apps + current_helpers}
    for pid in remaining:
        if _terminate_process(pid):
            logf(f"실행 중 Honey 강제 종료 pid={pid}")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = running_honey_processes(root)
        current_apps, _launchers, current_helpers = process_roles(current)
        if not current_apps:
            cleanup_orphan_helpers(root, current_helpers, logf)
            return True
        time.sleep(0.25)
    remaining = {p["pid"] for p in current_apps}
    logf(f"실행 중 Honey 종료 실패 pids={sorted(remaining)}")
    message_box("실행 중인 Honey를 종료하지 못했습니다.\n\n"
                "작업 관리자에서 HoneyApp.exe를 종료한 뒤\n"
                "다시 실행해 주세요.", show_ui)
    return False


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
    target = root / CURRENT_FILENAME
    last = None
    for attempt in range(5):
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            last = exc
            if attempt < 4:
                time.sleep(0.5)
    raise last


def runnable(version_dir: Path) -> bool:
    """이 폴더를 실행 후보로 볼 것인가.

    HoneyApp.exe 만 보면 **반쯤 지워진 폴더**도 후보가 된다(옛 rmtree 실패의 잔재).
    onedir 빌드는 _internal 없이는 뜨지 못하므로 그 존재까지 함께 본다 — stat 두 번이라
    기동을 지연시키지 않는다. 정밀 검증은 하지 않는다: 최종 방어선은 기존의 15초
    crash 감시 + 자동 롤백이다.
    """
    installing_path = version_dir / ".installing"
    ready = True
    if installing_path.exists():
        try:
            installing = json.loads(installing_path.read_text(encoding="utf-8-sig"))
            completed = json.loads((version_dir / ".ready").read_text(encoding="utf-8-sig"))
            ready = (installing.get("install_id")
                     and installing.get("install_id") == completed.get("install_id")
                     and str(installing.get("version")) == version_dir.name
                     and str(completed.get("version")) == version_dir.name
                     and str(installing.get("release")) == str(completed.get("release")))
        except (OSError, ValueError, AttributeError):
            ready = False
    return (ready and (version_dir / APP_EXE_NAME).exists()
            and (version_dir / "_internal").is_dir())


def installed_versions(root: Path):
    """실행 가능한 버전 폴더를 최신순으로 (current.txt 가 없거나 깨졌을 때의 폴백)."""
    out = []
    try:
        entries = list((root / VERSIONS_DIRNAME).iterdir())
    except OSError:
        return out
    for entry in entries:
        if runnable(entry) and re.fullmatch(r"\d+(\.\d+)*", entry.name):
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
    return [v for v in order if runnable(root / VERSIONS_DIRNAME / v)]


def message_box(text: str, enabled: bool = True) -> None:
    """실패 안내창. enabled=False 는 자동 테스트용 — 안내창은 클릭할 때까지 막힌다."""
    if not enabled:
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Honey", 0x10)
    except Exception:
        pass


def information_box(text: str, enabled: bool = True) -> None:
    """이미 실행 중인 Honey 등 오류가 아닌 상태 안내창."""
    if not enabled:
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Honey", 0x40)
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


def attach_quip(tk, root, frame):
    """기다리는 동안 볼 한 줄 문구를 붙이고 주기적으로 갈아 준다.

    돌려주는 함수는 로테이션을 멈추고 문구를 감춘다. **창을 닫기 전에 반드시**
    불러야 한다 — after 로 예약된 콜백이 죽은 창을 건드리면 Tcl 오류가 난다
    (진행바를 stop() 먼저 하는 것과 같은 이유).
    """
    label = tk.Label(frame, text=random.choice(QUIPS), bg=BG_CREAM, fg=INK_SUB,
                     anchor="w", justify="left", wraplength=400,
                     font=(UI_FONT, 9, "italic"))
    label.pack(fill="x", pady=(12, 0))
    state = {"job": None, "stopped": False}

    def rotate():
        state["job"] = None
        if state["stopped"]:
            return
        try:
            if not label.winfo_exists():
                return
            label.config(text=random.choice(QUIPS))
            state["job"] = root.after(QUIP_ROTATE_MS, rotate)
        except Exception:   # noqa: BLE001 - 문구 때문에 업데이트가 멈추면 안 된다
            state["stopped"] = True

    def stop():
        state["stopped"] = True
        if state["job"] is not None:
            try:
                root.after_cancel(state["job"])
            except Exception:   # noqa: BLE001 - 이미 지나간 예약이면 그만이다
                pass
            state["job"] = None
        try:
            label.pack_forget()
        except Exception:   # noqa: BLE001 - 창이 이미 죽었으면 할 일이 없다
            pass

    state["job"] = root.after(QUIP_ROTATE_MS, rotate)
    return stop


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
        self._stop_quip = attach_quip(tk, self.root, frame)
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
        self._stop_quip()           # 실패 안내 옆에서 농담이 도는 것도 마찬가지다
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
            self._stop_quip()
        except Exception:
            pass
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
        self._stop_quip = attach_quip(tk, self.root, frame)
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
        # 문구 로테이션도 같은 after 예약이라 함께 멈춘다.
        try:
            self._stop_quip()
        except Exception:
            pass
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
    """실제 설치. 반환: 'adopt' | 'delta' | 'full'.

    순서가 중요하다 — **설치 자리를 먼저 확보한 뒤에 받는다**(prepare_target).
    예전에는 다 받고 나서 대상 폴더를 지우려다 실패했고, 델타와 전체 zip 이 같은
    코드였던 탓에 331MB 를 받고 같은 자리에서 또 실패했다(2026-08-26 현장).

    LocalWriteError(로컬 권한·잠금)는 **절대 전체 zip 으로 폴백하지 않는다** — 다시
    받아도 결과가 같다. 호출부가 그것을 보고 권한 상승 경로로 넘어간다.

    current.txt 는 여기서 건드리지 않는다 — 호출부가 성공을 확인한 뒤 마지막에 바꾼다.
    """
    source_dir = root / VERSIONS_DIRNAME / current if current else None
    local_files = app_update.read_file_manifest(source_dir) if source_dir else None

    remote_files = None
    if local_files:
        try:
            remote_files = app_update.fetch_file_manifest(base_url, remote)
        except Exception as exc:   # noqa: BLE001 - 델타는 최적화일 뿐, 없으면 전체 zip
            logf(f"file manifest unavailable ({type(exc).__name__}: {exc})")

    release_id = str(manifest.get("sha256") or remote)
    state = app_update.prepare_target(root, remote, remote_files, release_id)
    logf(f"prepare target {remote}: {state}")
    if state == "adopted":
        return "adopt"

    if local_files and remote_files:
        try:
            app_update.install_delta(
                root, remote, base_url, remote_files, source_dir, local_files,
                progress_cb=lambda d, t: progress(f"새 버전 {remote} 내려받는 중...", d, t),
                release_id=release_id)
            return "delta"
        except (app_update.DownloadCancelled, app_update.LocalWriteError):
            raise            # 취소는 사용자 의사, 권한 실패는 전체 zip 도 실패한다
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
            lambda d, t: progress(f"새 버전 {remote} 설치 중...", d, t),
            remote_files=remote_files, release_id=release_id)
    finally:
        dest.unlink(missing_ok=True)   # 성공이든 실패든 받은 zip 은 남기지 않는다
    return "full"


def _update_with_ui(root, base_url, remote, manifest, current, logf):
    """진행창을 띄우고 워커 스레드로 설치. 반환 (성공?, 실패 메시지, 취소?)."""
    import queue
    import threading

    ui = ProgressWindow(remote)   # 실패하면 예외 → 호출부가 업데이트를 건너뛴다
    events = queue.Queue()
    outcome = {"ok": False, "error": "", "cancelled": False, "local_error": None}

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
        if isinstance(exc, app_update.LocalWriteError):
            # 권한/잠금이면 호출부가 UAC 한 번을 물어본다 — 여기서 실패창을 띄우면
            # 사용자가 같은 일로 두 번 클릭하게 되므로 창을 조용히 닫는다.
            outcome["local_error"] = exc
            finish()
            return
        ui.show_failure(str(exc)[:200], f"{base_url.rstrip('/')}/honey/download", finish)

    ui.root.after(100, poll)
    ui.root.mainloop()
    return outcome


def ask_elevate(version, failing_path, show_ui=True) -> bool:
    """"관리자 권한으로 업데이트할까요?" — 사용자가 고르지 않으면 기존 버전으로 간다.

    무인 PC(재부팅 후 자동 실행 등)에서 이 창이 응답을 기다리며 서 있으면 앱이 영영
    안 뜬다. 그래서 카운트다운 뒤 자동으로 '나중에'를 고른다 — 업데이트는 부가
    기능이고 앱이 뜨는 것이 본 기능이라는 원칙(모듈 docstring)의 연장이다.
    """
    if not show_ui:
        return False
    try:
        import tkinter as tk
    except Exception:   # noqa: BLE001 - 창을 못 띄우면 승격은 포기하고 앱을 띄운다
        return False

    choice = {"yes": False}
    try:
        win = tk.Tk()
        frame = build_shell(tk, win, "Honey 업데이트", "관리자 권한이 필요합니다")
        tk.Label(frame, text=f"새 버전 {version} 을 설치하려면 관리자 권한이 필요합니다.",
                 bg=BG_CREAM, fg=INK, anchor="w", justify="left",
                 font=(UI_FONT, 10)).pack(fill="x")
        if failing_path:
            tk.Label(frame, text=f"막힌 경로: {failing_path}", bg=BG_CREAM, fg=INK_SUB,
                     anchor="w", justify="left", wraplength=420,
                     font=(UI_FONT, 9)).pack(fill="x", pady=(6, 0))
        tk.Label(frame, text="'예'를 누르면 Windows 권한 확인 창이 한 번 뜹니다.\n"
                             "Honey 자체는 계속 일반 권한으로 실행됩니다.",
                 bg=BG_CREAM, fg=INK_SUB, anchor="w", justify="left",
                 font=(UI_FONT, 9)).pack(fill="x", pady=(8, 0))
        buttons = tk.Frame(frame, bg=BG_CREAM)
        buttons.pack(fill="x", pady=(14, 0))

        def pick(yes):
            choice["yes"] = yes
            try:
                win.destroy()
            except Exception:
                pass

        later = tk.Button(buttons, text="나중에", width=14, relief="flat",
                          bg=BG_BAND, fg=INK, activebackground=GOLD_BAR,
                          font=(UI_FONT, 9), command=lambda: pick(False))
        later.pack(side="right")
        tk.Button(buttons, text="권한 상승하여 업데이트", width=22, relief="flat",
                  bg=GOLD_BAR, fg=INK, activebackground=GOLD,
                  font=(UI_FONT, 9, "bold"), command=lambda: pick(True)
                  ).pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", lambda: pick(False))

        countdown = {"left": FAILURE_AUTORUN_SEC}

        def tick():
            countdown["left"] -= 1
            if countdown["left"] <= 0:
                pick(False)
                return
            later.config(text=f"나중에 ({countdown['left']})")
            win.after(1000, tick)

        later.config(text=f"나중에 ({countdown['left']})")
        win.after(1000, tick)
        center_window(win)
        win.mainloop()
    except Exception:   # noqa: BLE001
        return False
    return choice["yes"]


def _elevated_update(root, target, logf, show_ui=True) -> int:
    """--elevated-update: **업데이트만** 하고 끝난다 (앱을 띄우지 않는다).

    관리자 권한으로 앱을 실행하지 않는 것이 이 모드의 불변식이다 — 그렇게 하면
    앱이 만드는 파일까지 전부 관리자 소유가 되어 문제가 되돌아온다.
    """
    if app_update is None:
        return 1
    current, _prev = read_current(root)
    base_url = app_update.read_server_url(root, current)

    # ACL 정상화를 **설치보다 먼저** 한다. 나중에 하면 이 프로세스가 만든 새 폴더가
    # 관리자 소유로 남아 다음 업데이트가 또 막힌다. 이 한 번으로 이후에는 UAC 없이
    # 업데이트된다 — 그게 이 경로의 존재 이유다.
    acl_ok = app_update.normalize_acl(root)
    if not acl_ok:
        logf("elevated: ACL 정상화가 완전히 끝나지 않음 — 실제 파일 쓰기로 재확인")

    try:
        manifest = app_update.fetch_manifest(base_url)
        remote = str(manifest.get("version") or "") or target
    except Exception as exc:   # noqa: BLE001 - 서버가 안 되면 여기서 끝낸다
        logf(f"elevated: version check failed ({type(exc).__name__}: {exc})")
        app_update.write_elevated_result(root, {"ok": False, "error": "version check"})
        return 1
    if target and remote != target:
        logf(f"elevated: target {target} != server {remote} — 서버 값을 따른다")

    outcome = _update_with_ui(root, base_url, remote, manifest, current, logf)
    if not outcome["ok"]:
        app_update.write_elevated_result(
            root, {"ok": False,
                   "error": (("ACL 정상화 실패; " if not acl_ok else "")
                             + (outcome["error"] or "cancelled")),
                   "target": remote})
        return 1

    write_current(root, remote, current)
    app_update.clear_fail_count(root)
    logf(f"elevated: current -> {remote} (prev {current})")
    # 보호 폴더에서는 일반 권한 정리가 계속 실패해 옛 버전이 쌓인다 — 관리자
    # 권한을 쥔 지금 함께 수거한다.
    try:
        app_update.startup_cleanup(root, keep_versions=(remote, current))
    except Exception as exc:   # noqa: BLE001 - 정리 실패가 업데이트를 되돌리지 않는다
        logf(f"elevated: cleanup 실패(무시) {type(exc).__name__}: {exc}")
    app_update.write_elevated_result(root, {"ok": True, "target": remote})
    return 0


def update_base_version(root, logf):
    """업데이트 판정에 쓸 "지금 실제로 뜰 버전". current.txt 가 아니다.

    current.txt 는 포인터일 뿐이고 실행 후보는 runnable() 이 정한다 — 둘이 어긋나면
    (파일이 없거나, 가리키는 폴더가 깨졌거나) 종전 코드는 current.txt 의 글자로
    비교해 "이미 최신" 으로 조용히 빠졌다. 그러면 그 PC 는 영영 업데이트되지 않는다
    (재실행해도 같은 판정이라 회복 경로가 없었다).

    반환 None = 실행 가능한 버전이 하나도 없다 (사실상 신규 설치 상황).
    """
    current, _prev = read_current(root)
    if current and runnable(root / VERSIONS_DIRNAME / current):
        return current
    order = candidates(root)
    local = order[0] if order else None
    logf(f"current.txt={current or '없음'} 은 실행 불가 — "
         f"실행 후보 {local or '없음'} 기준으로 비교")
    return local


def fetch_manifest_with_retry(base_url, logf, retries=1, delay_sec=1.0):
    """/honey/version 질의. 일시적 네트워크 지연(부팅 직후)만 짧게 한 번 더 시도한다."""
    last = None
    for attempt in range(retries + 1):
        try:
            return app_update.fetch_manifest(base_url)
        except Exception as exc:   # noqa: BLE001 - 오프라인은 흔한 정상 상황이다
            last = exc
            if attempt < retries:
                logf(f"version check retry {attempt + 1}/{retries} "
                     f"({type(exc).__name__}: {exc})")
                time.sleep(delay_sec)
    raise last


def try_update(root, argv, show_ui=True):
    """앱을 띄우기 전에 업데이트한다. **어떤 실패도 밖으로 내보내지 않는다.**

    업데이트는 부가 기능이고 앱이 뜨는 것이 본 기능이다 — 여기서 예외가 새어나가면
    사용자는 일을 아예 못 하게 된다.

    **판정 결과는 어느 갈래로 가든 로그 한 줄을 남긴다.** 종전에는 "업데이트 없음"
    분기가 무로그라, 현장에서 "업데이트가 안 된다" 는 신고가 와도 서버를 못 본 것인지
    보고도 최신이라고 판정한 것인지 로그로 가릴 수 없었다.
    """
    def logf(message):
        log(root, message)

    try:
        if app_update is None:
            return handle_running_without_update(root, logf, show_ui)
        if not show_ui or "--skip-update" in argv:
            logf("update skipped (--skip-update / --no-ui)")
            return handle_running_without_update(root, logf, show_ui)
        if (root / NOUPDATE_FILENAME).exists():
            logf(f"update skipped ({NOUPDATE_FILENAME})")
            return handle_running_without_update(root, logf, show_ui)

        current, _prev = read_current(root)
        base_url = app_update.read_server_url(root, current)

        try:
            manifest = fetch_manifest_with_retry(base_url, logf)
        except Exception as exc:   # noqa: BLE001 - 오프라인은 흔한 정상 상황이다
            logf(f"version check skipped ({type(exc).__name__}: {exc})")
            return handle_running_without_update(root, logf, show_ui)

        remote = str(manifest.get("version") or "")
        logf(f"version check: server={remote or '없음'} "
             f"current={current or '없음'} url={base_url}")
        if not remote:
            # 서버가 200 을 줬지만 version 이 비었다 — is_newer 는 이걸 "최신" 으로
            # 돌려주므로(둘 중 하나라도 비면 False) 여기서 갈라 놓지 않으면 서버 이상이
            # 정상으로 위장된다.
            logf("version check: 서버 응답에 version 없음 — 건너뜀")
            return handle_running_without_update(root, logf, show_ui)

        local = update_base_version(root, logf)
        if not app_update.is_newer(remote, local):
            logf(f"update 없음 (server={remote} <= local={local or '없음'})")
            app_update.clear_fail_count(root)
            return handle_running_without_update(root, logf, show_ui)

        # 이하 current 는 "실제로 뜰 버전" 이다 — 델타의 재사용 원본과 롤백용 prev 는
        # 실재하는 폴더여야 한다 (current.txt 의 글자가 아니라).
        current = local

        fails = app_update.read_fail_count(root, remote, LAUNCHER_BUILD)
        if fails >= MAX_UPDATE_FAILS:
            logf(f"update skipped: {remote} 연속 {fails}회 실패 — 더 시도하지 않는다")
            return handle_running_without_update(root, logf, show_ui)

        running, blocked = wait_for_other_launcher(root, logf, show_ui)
        if blocked:
            return False
        apps, _launchers, helpers = process_roles(running)
        logf("업데이트 전 프로세스: "
             f"app=[{_process_names(apps)}] helper=[{_process_names(helpers)}]")
        if apps and not ask_and_close_running_honey(root, running, logf, show_ui):
            # 이미 떠 있는 Honey를 그대로 유지한다. 아래 앱 기동 단계로 내려가면 중복
            # 실행이 되므로 main 에 "이번 런처는 여기서 끝내라"고 알린다.
            return False
        if not apps and helpers:
            cleanup_orphan_helpers(root, helpers, logf)

        # 같은 설치 폴더를 두 프로세스가 동시에 만지지 않게 한다 (사용자가 런처를
        # 두 번 눌렀거나, 승격 프로세스가 아직 돌고 있는 경우).
        mutex = app_update.acquire_update_mutex(root)
        if mutex is None:
            logf("update skipped: 다른 프로세스가 이미 업데이트 중이다")
            return False
        try:
            logf(f"update {current} -> {remote} 시작")
            outcome = _update_with_ui(root, base_url, remote, manifest, current, logf)

            if outcome["ok"]:
                write_current(root, remote, current)   # 성공한 뒤에만 포인터를 바꾼다
                app_update.clear_fail_count(root)
                logf(f"current -> {remote} (prev {current})")
                return
            if not outcome["error"]:
                return                                  # 사용자 취소 — 조용히 넘어간다

            # 로컬 권한/잠금이면 다시 받아도 소용없다 — UAC 한 번으로 해결되는지 묻는다.
            local_exc = outcome.get("local_error")
            if local_exc is not None and _try_elevated_update(
                    root, base_url, remote, current, local_exc, logf, show_ui):
                return

            count = app_update.bump_fail_count(root, remote, LAUNCHER_BUILD)
            context = {"target": remote, "attempt": count}
            if local_exc is not None:
                context.update(local_exc.details())
                detail = str(local_exc)
                if local_exc.winerror is not None:
                    detail += f"\nWinError: {local_exc.winerror}"
                message_box(
                    f"업데이트에 실패했습니다.\n\n{detail}\n\n"
                    "파일 쓰기 권한, 백신 차단 또는 실행 중인 프로세스를 확인한 뒤\n"
                    "Honey 를 다시 실행해 주세요.\n\n기존 버전으로 실행합니다.",
                    show_ui)
            app_update.report_failure(
                base_url, outcome["error"], context, current or "")
        finally:
            app_update.release_update_mutex(mutex)
    except BaseException as exc:   # noqa: BLE001 - 여기서 막지 못하면 앱이 안 뜬다
        try:
            logf(f"update aborted ({type(exc).__name__}: {exc})")
        except Exception:
            pass
    return True


def _try_elevated_update(root, base_url, remote, current, local_exc, logf, show_ui):
    """권한 상승으로 한 번 더 시도. 성공하면 True (호출부는 그대로 앱을 띄운다).

    성공 판정의 정본은 **current.txt** 다 — 승격 프로세스의 exit code 는 로그용이다.
    """
    if app_update.is_elevated():
        logf("elevated 상태인데도 권한 실패 — 파일 잠금이나 정책 문제다")
        return False
    if not is_frozen_launcher():
        logf("개발 실행 — 권한 상승은 빌드본에서만 한다")
        return False
    if not ask_elevate(remote, local_exc.path, show_ui):
        logf("사용자가 권한 상승을 선택하지 않았다 — 기존 버전으로 실행")
        return False

    app_update.clear_elevated_result(root)
    logf(f"elevated update 요청 {remote} (막힌 곳: {local_exc.path})")
    status, info = app_update.run_elevated(
        sys.executable, ["--elevated-update", remote], cwd=root,
        timeout_sec=ELEVATED_TIMEOUT_SEC)
    logf(f"elevated update 결과 {status} ({info})")

    cur_now, _prev = read_current(root)
    if cur_now == remote:
        app_update.clear_fail_count(root)
        logf(f"elevated update 성공 — current={cur_now}")
        return True

    result = app_update.read_elevated_result(root)
    detail = (result or {}).get("error") or info
    app_update.report_failure(
        base_url, f"권한 상승 업데이트 실패: {detail}",
        {"target": remote, "stage": f"elevated:{status}", **local_exc.details()},
        current or "")
    if status != "cancelled":
        app_update.bump_fail_count(root, remote, LAUNCHER_BUILD)
    return False


def is_frozen_launcher() -> bool:
    """빌드된 런처인가 (승격은 exe 를 다시 실행하는 것이라 개발 실행에선 무의미)."""
    return bool(getattr(sys, "frozen", False))


def main(argv) -> int:
    root = root_dir()
    show_ui = "--no-ui" not in argv

    if "--elevated-update" in argv:
        # 관리자 권한으로 다시 실행된 자신이다 — 업데이트만 하고 끝난다.
        try:
            target = argv[argv.index("--elevated-update") + 1]
        except IndexError:
            target = ""
        # mutex 는 여기서 잡지 않는다 — 이 프로세스를 띄운 부모 런처가 쥐고 있고,
        # 그 부모는 우리가 끝날 때까지 기다린다. 여기서 또 잡으려 하면 자기 자신에게
        # 막혀 업데이트를 못 한다.
        return _elevated_update(root, target, lambda m: log(root, m), show_ui)

    if "--wait-pid" in argv:
        try:
            pid = int(argv[argv.index("--wait-pid") + 1])
        except (IndexError, ValueError):
            pid = 0
        if pid:
            log(root, f"wait for pid {pid}")
            wait_for_pid(pid, WAIT_PARENT_SEC)
        # --wait-pid 만 오면 방금 업데이트를 마치고 재실행된 경우다 (다시 확인할 필요 없다).
        # --force-update 가 함께 오면 앱의 [지금 업데이트] 가 우리를 부른 것이다 —
        # 런처를 거치지 않고 앱만 실행되던 PC(작업표시줄 고정 등)의 유일한 복귀 경로라
        # 여기서 반드시 업데이트를 확인해야 한다.
        if "--force-update" in argv:
            log(root, "forced update check (from app)")
            if try_update(root, argv, show_ui) is False:
                return 0
    else:
        if try_update(root, argv, show_ui) is False:
            return 0

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

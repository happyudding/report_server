"""Honey 내장 브라우저 (실험).

QWebEngineView(크로미움) 로 서버 검색결과 페이지·web_report 를 툴 안에서 연다.
- BrowserPanel: 툴바(뒤로/앞으로/새로고침/홈/주소창) + 웹뷰. 메인 화면에 embed 용.
- EmbeddedBrowserWindow: BrowserPanel 을 감싼 독립 창 (target=_blank 팝업용).

PyQtWebEngine 미설치 시 이 모듈 import 가 ImportError — 호출부(honey_main)에서
폴백한다.

주의: QtWebEngineWidgets 를 QApplication 생성 이후에 import 하려면
Qt.AA_ShareOpenGLContexts 속성이 앱 생성 전에 설정돼 있어야 한다
(honey_main.main() 에서 설정).
"""
import sys
from urllib.parse import quote

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWebEngineCore import (
    QWebEngineDownloadRequest,
    QWebEnginePage,
    QWebEngineProfile,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QLineEdit, QMainWindow, QToolBar, QVBoxLayout, QWidget

import client_identity
from transport import update_policy
from transport.config import CURRENT_VERSION

# 열린 팝업 창 참조 보관 (GC 로 창이 사라지는 것 방지)
_open_windows = []


def _inject_user_agent():
    """기본 프로필 User-Agent 에 `HoneyUser/<계정> HoneyVer/<버전>` 토큰을 1회 추가.

    서버 검색결과 페이지가 navigator.userAgent 에서 파싱해 즐겨찾기·내 업로드
    우선 정렬의 사용자 ID 로 쓴다. 계정에 공백/한글이 있어도 헤더가 깨지지
    않도록 percent-encode 하고, JS 쪽에서 decodeURIComponent 로 되돌린다.
    수집 실패 시 토큰 없이 기존 UA 그대로 둔다 (페이지는 수동 입력으로 폴백).

    버전 토큰은 관리자 화면이 "지금 리포트를 보는 사람이 어떤 Honey 를 쓰는가" 를
    표시하는 값이다. 계정 토큰 뒤에 **공백으로 구분해** 붙이므로 기존
    `HoneyUser/(\\S+)` 파싱(서버 auth_identity·검색결과/랜딩 JS)에는 영향이 없다.
    """
    profile = QWebEngineProfile.defaultProfile()
    ua = profile.httpUserAgent()
    if "HoneyUser/" in ua:
        return
    try:
        user = client_identity.collect().get("user", "")
    except Exception:
        user = ""
    if user:
        profile.setHttpUserAgent(
            f"{ua} HoneyUser/{quote(user, safe='')} HoneyVer/{CURRENT_VERSION}")


_download_handler_installed = False


def _install_download_handler():
    """defaultProfile 의 다운로드 요청을 처리하는 핸들러를 1회만 연결한다.

    핸들러가 없으면 QtWebEngine 은 다운로드를 UI 반응 없이 조용히 취소한다.
    (HONEY 설치본 다운로드 링크뿐 아니라 내장 브라우저 안의 모든 다운로드가 대상.)
    """
    global _download_handler_installed
    if _download_handler_installed:
        return
    _download_handler_installed = True
    QWebEngineProfile.defaultProfile().downloadRequested.connect(
        _on_download_requested)


def _on_download_requested(download):
    """다운로드 폴더에 저장하고, 완료되면 탐색기에서 파일을 선택 표시한다.

    경로/탐색기 헬퍼는 업데이트 manual 흐름과 동일하게 update_policy 를 재사용한다.
    파일명은 크로미움이 제안한 값을 그대로 쓴다(특정 파일 전용 로직 없음).
    """
    base = download.downloadFileName() or "download"
    dest = update_policy.unique_dest(update_policy.downloads_dir(), base)
    download.setDownloadDirectory(str(dest.parent))
    download.setDownloadFileName(dest.name)

    def _on_finished():
        if download.state() == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            update_policy.open_folder_select(dest)

    download.isFinishedChanged.connect(_on_finished)
    download.accept()


# 브라우저 네비게이션 툴바 스타일 (얇고 단정하게)
_NAV_TOOLBAR_QSS = """
QToolBar {
    background: #f3f4f6;
    border: none;
    border-bottom: 1px solid #d1d5db;
    padding: 2px 4px;
    spacing: 2px;
}
QToolBar QToolButton {
    padding: 3px 8px;
    border-radius: 4px;
    color: #374151;
}
QToolBar QToolButton:hover { background: #e5e7eb; }
QLineEdit {
    border: 1px solid #d1d5db;
    border-radius: 4px;
    padding: 3px 8px;
    background: white;
    color: #111827;
}
"""


def _is_external_host(url, home_host):
    """팝업 대상 URL 이 리포트 서버(홈) 호스트 밖인지 (대소문자 무시)."""
    host = url.host().lower()
    return bool(host) and host != str(home_host or "").lower()


class _GuardedPage(QWebEnginePage):
    """네비게이션 직전에 leave_guard 로 이탈을 가로챌 수 있는 페이지.

    leave_guard(QUrl) -> bool 을 외부(honey_main)에서 주입한다. True 면 이동 허용,
    False 면 차단(현재 페이지 유지). Rawdata(Excel) 편집 중 세션 이탈을 막는 데 쓴다.
    링크 클릭·뒤로/앞으로·새로고침·주소창 입력·프로그래밍 load 가 모두 이 지점을 지난다.
    """

    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self.leave_guard = None
        self._in_guard = False   # guard 안에서 띄운 다이얼로그 이벤트 루프의 재진입 방지
        self._popup = None       # createWindow 로 만든 팝업의 첫 네비게이션 대기 상태

    def arm_popup(self, window, home_host):
        """createWindow 가 만든(아직 숨겨진) 팝업 창을 첫 네비게이션 대기로 등록한다.

        URL 은 createWindow 시점에 알 수 없으므로 첫 main-frame 네비게이션에서 판정한다:
        외부 호스트면 시스템 기본 브라우저로 넘기고 이 창은 버리고, 서버 내부 링크면
        지금까지처럼 내장 브라우저 창으로 띄운다.
        """
        self._popup = (window, home_host)

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if is_main_frame and self._popup is not None:
            window, home_host = self._popup
            self._popup = None   # 첫 네비게이션에만 적용
            if _is_external_host(url, home_host):
                QDesktopServices.openUrl(url)
                # 창 파괴는 이 콜백(=이 page 실행 중) 밖으로 미룬다.
                QTimer.singleShot(0, window.close)
                return False
            window.show()
            window.raise_()
            window.activateWindow()
        if is_main_frame and self.leave_guard is not None and not self._in_guard:
            self._in_guard = True
            try:
                allowed = self.leave_guard(url)
            except Exception:
                allowed = True   # 가드 오류로 브라우저가 잠기지 않게 통과시킨다
            finally:
                self._in_guard = False
            if not allowed:
                return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class _WebView(QWebEngineView):
    """target=_blank 링크를 새 내장 브라우저 창으로 여는 뷰."""

    def __init__(self, home_url, parent=None):
        super().__init__(parent)
        self._home_url = home_url
        # 기본 페이지를 이탈 가드용 페이지로 교체 (UA 는 defaultProfile 레벨이라 유지된다).
        self.setPage(_GuardedPage(QWebEngineProfile.defaultProfile(), self))

    def createWindow(self, _window_type):
        # 창은 만들되 숨겨둔 채 넘긴다 — 첫 네비게이션 URL 을 보고 _GuardedPage 가
        # 내장 창으로 띄울지(서버 내부) 기본 브라우저로 넘길지(외부: VOC 등) 정한다.
        win = EmbeddedBrowserWindow(self._home_url)
        _open_windows.append(win)
        win.panel.view.page().arm_popup(win, QUrl(self._home_url).host())
        return win.panel.view


class BrowserPanel(QWidget):
    """네비게이션 툴바 + 웹뷰 패널. 메인 화면·독립 창 어디든 embed 가능."""

    def __init__(self, home_url, navigate=False, parent=None, start_url=None):
        """home_url: 🏠 버튼이 가는 곳(검색결과). start_url: 처음 한 번 열 곳(랜딩).

        start_url 을 넘기지 않으면 종전대로 home_url 로 시작한다 — 기존 호출부 무영향.
        """
        super().__init__(parent)
        self._home_url = home_url
        self._start_url = start_url or home_url
        self._render_crash_count = 0
        self._closing = False
        _inject_user_agent()
        _install_download_handler()
        self.view = _WebView(home_url)
        self.view.page().renderProcessTerminated.connect(self._on_render_terminated)

        tb = QToolBar("Navigation")
        tb.setMovable(False)
        tb.setStyleSheet(_NAV_TOOLBAR_QSS)
        tb.addAction("◀", self.view.back)
        tb.addAction("▶", self.view.forward)
        tb.addAction("⟳", self.view.reload)
        tb.addAction("🏠 검색결과", self.go_home)

        self.url_bar = QLineEdit()
        self.url_bar.returnPressed.connect(self._navigate_from_bar)
        tb.addWidget(self.url_bar)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(tb)
        layout.addWidget(self.view)

        self.view.urlChanged.connect(
            lambda u: self.url_bar.setText(u.toString()))
        if navigate:
            self.view.load(QUrl(self._start_url))

    def go_home(self):
        self.view.load(QUrl(self._home_url))

    def _navigate_from_bar(self):
        text = self.url_bar.text().strip()
        if not text:
            return
        if "://" not in text:
            text = "http://" + text
        self.view.load(QUrl(text))

    def _on_render_terminated(self, status, exit_code):
        """렌더러(QtWebEngineProcess) 비정상 종료를 기록하고 1회만 자동 복구한다.

        연결이 없으면 렌더러가 죽어도 화면만 비고 로그·서버 어디에도 흔적이 남지 않아,
        "가만히 있다가 화면이 하얘졌다"는 신고를 재현 없이는 조사할 수 없었다.
        GPU/드라이버가 원인인 경우가 많아 조치 힌트도 함께 남긴다.

        Normal 종료와 정리 중(_closing) 발화는 무시한다 — 뷰를 닫으면 렌더러가
        Normal 로 죽으면서 이 시그널이 뜨는데, 그걸 크래시로 다루면 종료 로그가
        오염되고 이미 닫은 뷰에 새로고침을 예약하게 된다.
        """
        name = getattr(status, "name", str(status))
        if self._closing or name.startswith("Normal"):
            return
        self._render_crash_count += 1
        url = self.view.url().toString()
        print(f"[renderer] 종료 status={name} exitCode={exit_code} url={url}",
              file=sys.stderr)
        crashed = name.startswith(("Crashed", "Abnormal"))
        if crashed:
            print("[renderer] GPU/드라이버 문제일 수 있음 — 기본 --disable-gpu-compositing "
                  "적용 중에도 재발하면 honey.env 에 HONEY_CHROMIUM_FLAGS=--disable-gpu "
                  "(또는 Windows 환경변수 QTWEBENGINE_CHROMIUM_FLAGS)로 상향 후 재실행해 볼 것",
                  file=sys.stderr)
        try:
            from transport import error_report
            error_report.report_error(
                "honey_render_crash", f"{name} exit_code={exit_code}",
                context={"status": name, "exit_code": exit_code, "url": url})
        except Exception:   # noqa: BLE001 - 보고 실패가 복구를 막지 않게
            pass
        # 첫 크래시만 되살린다 — 크래시→reload→크래시 루프를 만들지 않기 위해.
        if self._render_crash_count == 1:
            print("[renderer] 자동 새로고침 1회 시도", file=sys.stderr)
            QTimer.singleShot(1500, self.view.reload)
        else:
            print(f"[renderer] 자동 복구 중단 (이번 실행에서 {self._render_crash_count}회째) "
                  "— ⟳ 로 직접 새로고침하세요", file=sys.stderr)


class EmbeddedBrowserWindow(QMainWindow):
    def __init__(self, home_url):
        super().__init__()
        self.setWindowTitle("Honey Browser")
        self.resize(1280, 860)
        self.panel = BrowserPanel(home_url)
        self.setCentralWidget(self.panel)
        self.panel.view.titleChanged.connect(
            lambda t: self.setWindowTitle(t or "Honey Browser"))

    def closeEvent(self, event):
        if self in _open_windows:
            _open_windows.remove(self)
        super().closeEvent(event)


def shutdown_panel(panel):
    """BrowserPanel 1개의 웹뷰를 끊는다 (best-effort — 실패는 호출부에서 무시).

    QApplication 이 QWebEngineView 보다 먼저 파괴되면 Chromium 정리가 뒤늦게 돌아
    access violation 이 난다. 종료 경로에서 이 함수로 먼저 뷰를 멈춘다.
    """
    panel._closing = True   # 정리 중 뜨는 렌더러 종료 시그널을 크래시로 오인하지 않게
    view = getattr(panel, "view", None)
    if view is not None:
        view.stop()
        view.close()
    panel.close()


def shutdown_all():
    """앱 종료 직전, 열려 있는 팝업 창을 전부 정리하고 전역 참조를 비운다.

    _open_windows 는 모듈 전역이라 종료 시 아무도 닫아주지 않으면 인터프리터 종료
    시점까지 QWebEngineView 를 붙잡는다 — 메인 뷰만 정리해도 여기서 남으면 같은
    크래시가 난다. 창 하나가 실패해도 나머지는 계속 정리한다.
    """
    for win in list(_open_windows):
        try:
            shutdown_panel(win.panel)
            win.close()
        except Exception:   # noqa: BLE001
            pass
    _open_windows.clear()


def open_browser(url, navigate=True):
    """독립 내장 브라우저 창을 열고 url 로 이동. 생성된 창을 반환."""
    win = EmbeddedBrowserWindow(url)
    _open_windows.append(win)
    if navigate:
        win.panel.go_home()
    win.show()
    win.raise_()
    win.activateWindow()
    return win

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
from urllib.parse import quote

from PyQt6.QtCore import QUrl
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QLineEdit, QMainWindow, QToolBar, QVBoxLayout, QWidget

import client_identity

# 열린 팝업 창 참조 보관 (GC 로 창이 사라지는 것 방지)
_open_windows = []


def _inject_user_agent():
    """기본 프로필 User-Agent 에 `HoneyUser/<계정>` 토큰을 1회 추가.

    서버 검색결과 페이지가 navigator.userAgent 에서 파싱해 즐겨찾기·내 업로드
    우선 정렬의 사용자 ID 로 쓴다. 계정에 공백/한글이 있어도 헤더가 깨지지
    않도록 percent-encode 하고, JS 쪽에서 decodeURIComponent 로 되돌린다.
    수집 실패 시 토큰 없이 기존 UA 그대로 둔다 (페이지는 수동 입력으로 폴백).
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
        profile.setHttpUserAgent(f"{ua} HoneyUser/{quote(user, safe='')}")

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

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
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
        win = open_browser(self._home_url, navigate=False)
        return win.panel.view


class BrowserPanel(QWidget):
    """네비게이션 툴바 + 웹뷰 패널. 메인 화면·독립 창 어디든 embed 가능."""

    def __init__(self, home_url, navigate=False, parent=None):
        super().__init__(parent)
        self._home_url = home_url
        _inject_user_agent()
        self.view = _WebView(home_url)

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
            self.go_home()

    def go_home(self):
        self.view.load(QUrl(self._home_url))

    def _navigate_from_bar(self):
        text = self.url_bar.text().strip()
        if not text:
            return
        if "://" not in text:
            text = "http://" + text
        self.view.load(QUrl(text))


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

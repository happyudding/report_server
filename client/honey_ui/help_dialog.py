"""HONEY 통합 도움말 다이얼로그.

목차(TOC)형 도움말 — QTextBrowser 하나로 상단 목차 + 본문을 렌더하고,
목차 링크(#anchor) 클릭 시 해당 섹션으로 스크롤한다. 기능을 크게
Web Report / Excel(xlsx) Report 두 갈래로 나눠 전체 사용법을 설명한다.

honey_main 은 show_help(parent) 진입점만 호출한다 (세부 구현은 이 파일에 격리).
"""
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QTextBrowser, QPushButton

# VOC 버튼이 여는 Confluence 페이지.
_VOC_URL = "https://confluence.samsungds.net/pages/editpage.action?pageId=3473285336"


# 도움말 본문 (HTML). 앵커는 <a name="..."> 로 두고 목차에서 href="#..." 로 점프.
_HELP_HTML = """
<h2>HONEY 도움말</h2>
<p>Honey 는 리포트용 입력 파일을 열어 <b>Web Report</b>(브라우저 웹 리포트) 또는
<b>Excel Report</b>(.xlsx 생성 후 서버 업로드)로 만드는 도구입니다.
아래 목차에서 궁금한 항목을 누르면 해당 설명으로 이동합니다.</p>

<h3>목차</h3>
<ul>
  <li><b>공통 / 입력</b>
    <ul>
      <li><a href="#open">파일 열기 · Dolphin(D1) 불러오기</a></li>
      <li><a href="#order">여러 파일 순서 지정 (▲▼)</a></li>
      <li><a href="#pt">Product Type 선택</a></li>
      <li><a href="#outname">저장명 지정</a></li>
    </ul>
  </li>
  <li><b>Web Report</b>
    <ul>
      <li><a href="#web-run">Web Report 실행</a></li>
      <li><a href="#web-mode">분석 모드 (Normal / Compare / DUT)</a></li>
      <li><a href="#web-source">Source 이름(Legend) 변경</a></li>
      <li><a href="#web-color">Distribution 색 설정</a></li>
    </ul>
  </li>
  <li><b>Excel(xlsx) Report</b>
    <ul>
      <li><a href="#xlsx-run">Excel Report 실행</a></li>
      <li><a href="#xlsx-upload">서버 업로드 (PIN · 메타)</a></li>
      <li><a href="#xlsx-search">검색결과 조회 · 수정 · 삭제</a></li>
    </ul>
  </li>
</ul>

<hr>
<h2>공통 / 입력</h2>

<h3><a name="open"></a>파일 열기 · Dolphin(D1) 불러오기</h3>
<p><b>LOCAL FILE OPEN</b> 은 PC 에 있는 리포트 생성용 입력 파일(CSV 계열)을 직접 고릅니다.
<b>Dolphin (D1)에서 불러오기</b> 는 D1 경로에서 파일을 선택해 가져옵니다.
파일 목록 영역에 드래그&amp;드롭으로도 넣을 수 있습니다.</p>

<h3><a name="order"></a>여러 파일 순서 지정 (▲▼)</h3>
<p>여러 파일을 선택하면 순서 지정 창이 뜹니다. 목록에서 파일을 고른 뒤 오른쪽
<b>▲ / ▼</b> 버튼으로 순서를 바꿉니다. <b>맨 위 파일이 기준(base)</b> 이며, Compare/DUT
모드에서 비교 기준이 됩니다.</p>

<h3><a name="pt"></a>Product Type 선택</h3>
<p>제품 종류(Product Type)를 라디오로 고릅니다. 선택은 사용자별 설정에 즉시 저장되어
다음 실행 때 유지됩니다.</p>

<h3><a name="outname"></a>저장명 지정</h3>
<p>결과 저장 이름을 입력합니다. Excel Report 의 경우 여기에 <code>.xlsx</code> 확장자가
붙어 저장됩니다.</p>

<hr>
<h2>Web Report</h2>

<h3><a name="web-run"></a>Web Report 실행</h3>
<p>입력 파일을 연 뒤 <b>Web Report</b> 버튼을 누르면 분석 결과가 <b>내장 브라우저에
웹 리포트</b> 형태로 표시됩니다. 분석 항목/옵션을 고르는 설정 창이 먼저 뜰 수 있습니다.</p>

<h3><a name="web-mode"></a>분석 모드 (Normal / Compare / DUT)</h3>
<ul>
  <li><b>Normal</b> — 단일 데이터셋을 그대로 분석합니다.</li>
  <li><b>Compare</b> — 여러 입력 파일(source)을 비교합니다. 맨 위 파일이 기준입니다.</li>
  <li><b>DUT</b> — DUT 단위 비교 분석입니다.</li>
</ul>
<p>파일 개수와 모드가 맞지 않으면 실행 시 경고가 나옵니다. (분석 모드는 Web Report
에만 적용됩니다.)</p>

<h3><a name="web-source"></a>Source 이름(Legend) 변경</h3>
<p>Web Report 생성 직전, 각 입력 파일의 <b>Legend 이름</b>을 쉼표(,)로 구분해 입력해
바꿀 수 있습니다. 빈칸으로 두면 기존 이름을 유지하고, 이름이 겹치면 자동으로 번호가
붙습니다.</p>

<h3><a name="web-color"></a>Distribution 색 설정</h3>
<p>메뉴 <b>분석 → Distribution 색 설정</b> 에서 Legend/source 팔레트 색을 지정합니다.
저장하면 다음 Web Report 생성 때 기본색으로 적용됩니다. (색 번호 i = source i 의 색)</p>

<hr>
<h2>Excel(xlsx) Report</h2>

<h3><a name="xlsx-run"></a>Excel Report 실행</h3>
<p>입력 파일을 연 뒤 <b>Excel Report</b> 버튼을 누르면 로컬에 <b>.xlsx 보고서 파일</b>이
생성됩니다. 저장명은 입력한 저장명을 사용합니다.</p>

<h3><a name="xlsx-upload"></a>서버 업로드 (PIN · 메타)</h3>
<p>생성된(또는 이미 있는) .xlsx 보고서를 서버로 업로드합니다. 업로드 시
<b>product_type · product · lot_id</b> 와 <b>4자리 숫자 PIN</b> 을 입력해야 합니다.
PIN 은 이후 수정·삭제 시 본인 확인에 쓰입니다.</p>
<p>※ 원본 xlsx 파일 자체는 서버에 보관되지 않고, 추출된 표/이미지만 저장됩니다.</p>

<h3><a name="xlsx-search"></a>검색결과 조회 · 수정 · 삭제</h3>
<p>업로드된 보고서는 서버의 <b>검색결과 페이지</b>에서 조회합니다. product_type 등으로
목록을 검색하고, 세션을 열어 내용을 <b>보기 / 수정 / 삭제</b> 할 수 있습니다. 수정·삭제
에는 업로드 때 정한 <b>PIN</b> 이 필요합니다.</p>
"""


class HelpDialog(QDialog):
    """목차형 통합 도움말 창."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("HONEY 도움말")
        self.resize(720, 640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._browser = QTextBrowser(self)
        # 내부 앵커(#...) 는 직접 scrollToAnchor 로 처리 (외부 링크로 오해 방지)
        self._browser.setOpenLinks(False)
        self._browser.setHtml(_HELP_HTML)
        self._browser.anchorClicked.connect(self._on_anchor)
        layout.addWidget(self._browser)

        # 하단 버튼 줄: VOC(Confluence 페이지를 기본 브라우저로 연다)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(8, 6, 8, 8)
        btn_row.addStretch(1)
        voc_btn = QPushButton("VOC", self)
        voc_btn.setToolTip("VOC Confluence 페이지 열기")
        voc_btn.clicked.connect(self._open_voc)
        btn_row.addWidget(voc_btn)
        layout.addLayout(btn_row)

    def _open_voc(self):
        QDesktopServices.openUrl(QUrl(_VOC_URL))

    def _on_anchor(self, url):
        frag = url.fragment()
        if frag:
            self._browser.scrollToAnchor(frag)


def show_help(parent=None):
    """도움말 다이얼로그를 모달로 띄운다 (honey_main 진입점)."""
    dlg = HelpDialog(parent)
    dlg.exec()

"""HONEY 사용자 기능 카탈로그.

help.html, 공개 REST API, 관리자 ENGR 챗봇이 같은 기능명과 제공 상태를 쓰기 위한
단일 원본이다. 운영·관리자 도구나 화면에 노출되지 않은 내부 기능은 넣지 않는다.
"""
from __future__ import annotations

import re
import unicodedata

SCHEMA_VERSION = 1
CATALOG_VERSION = "2026-08-12"
STATUSES = frozenset({"available", "conditional", "coming_soon"})

FEATURE_FIELDS = (
    "id", "category", "title", "aliases", "keywords", "status", "surfaces",
    "audience", "availability", "summary", "usage", "cautions", "help_anchor",
    "related_ids",
)


def _feature(feature_id, category, title, aliases, keywords, status, surfaces,
             audience, availability, summary, usage, cautions, help_anchor,
             related_ids=()):
    return {
        "id": feature_id,
        "category": category,
        "title": title,
        "aliases": list(aliases),
        "keywords": list(keywords),
        "status": status,
        "surfaces": list(surfaces),
        "audience": list(audience),
        "availability": availability,
        "summary": summary,
        "usage": list(usage),
        "cautions": list(cautions),
        "help_anchor": help_anchor,
        "related_ids": list(related_ids),
    }


FEATURES = (
    _feature(
        "landing", "getting_started", "REPORT SERVER 시작 화면",
        ("시작 화면", "메인 화면", "랜딩"), ("제품군 바로가기", "Honey 다운로드", "접속 현황"),
        "available", ("landing",), ("all",), "모든 사용자",
        "제품군별 보고서 검색, Honey 다운로드, 세션 수와 최근 활동을 한 화면에서 시작합니다.",
        ("/pe/에 접속합니다.", "MDDI·PDDI·PMIC·SECURITY·TCON 타일을 눌러 검색으로 이동합니다.",
         "하단에서 전체 세션, 최근 7일, 오늘 사용량, 최근 5분 접속자를 확인합니다."),
        ("현황 수치는 보조 정보이며 조회 실패 시에도 바로가기와 다운로드는 사용할 수 있습니다.",),
        "flow", ("report-search", "web-account")),
    _feature(
        "web-account", "getting_started", "웹 로그인과 회원가입",
        ("웹 로그인", "회원가입", "4자리 비밀번호"), ("singleID", "읽기 전용", "Honey 비밀번호 설정"),
        "conditional", ("landing", "search", "web_report"), ("all", "authenticated"),
        "Honey 신원, 사내 SSO 또는 웹 로그인 상태에 따라 편집 권한이 달라집니다.",
        "일반 브라우저에서 로그인하지 않으면 공개 보고서를 읽을 수만 있고 수정은 할 수 없습니다.",
        ("singleID와 숫자 4자리 비밀번호로 로그인합니다.",
         "Honey 사용 이력이 없는 미사용 ID만 웹에서 가입할 수 있습니다.",
         "Honey 사용 이력이 있으면 Honey 앱에서 비밀번호를 설정하거나 재설정합니다."),
        ("비공개 보고서는 권한 없는 사용자에게 목록과 상세 모두 표시되지 않습니다.",),
        "flow", ("report-permissions", "report-visibility")),
    _feature(
        "report-search", "report_management", "보고서 검색과 필터",
        ("보고서 검색", "리포트 찾기", "My Upload"),
        ("Product Type", "분석 모드", "공개 범위", "기간", "정렬", "통합 검색"),
        "available", ("search",), ("all",), "모든 사용자",
        "제품군·모드·공개 범위·기간·업로더와 통합 검색어로 보고서를 찾습니다.",
        ("Product Type 버튼을 고릅니다.", "Normal·Compare·DUT·Temperature 모드와 공개 범위를 선택합니다.",
         "시작일·종료일, 정렬, My Upload와 Product·LOT·Process·파일명·업로더 통합 검색을 조합합니다."),
        ("필터와 페이지는 URL에 반영되어 새로고침이나 링크 공유 후에도 복원됩니다.",),
        "dashboard", ("report-favorite", "report-trash")),
    _feature(
        "report-favorite", "report_management", "보고서 즐겨찾기",
        ("즐겨찾기", "검색 별표"), ("별", "favorite", "검색 목록"),
        "conditional", ("search",), ("authenticated",), "로그인 또는 Honey 신원 필요",
        "검색 목록의 빈 별을 눌러 사용자별 즐겨찾기를 저장합니다.",
        ("검색 결과의 Product Type 칸에 있는 ☆를 누릅니다.", "★가 되면 내 계정의 즐겨찾기에 저장됩니다."),
        ("리포트 상단의 개인 중요 표시와는 별개의 표시입니다.",),
        "dashboard", ("report-important",)),
    _feature(
        "report-important", "report_management", "개인 중요 표시",
        ("중요 표시", "개인 중요"), ("important", "자동 정리 제외", "별표"),
        "conditional", ("web_report", "search"), ("editor",), "업로더 또는 위임 편집자",
        "리포트 상단에서 개인별 중요 표시를 켜고 자동 정리 대상에서 보호합니다.",
        ("리포트 상단의 중요 표시 버튼을 누릅니다.", "검색 목록에서는 즐겨찾기 별과 나란히 표시됩니다."),
        ("다른 사용자에게 같은 중요 표시가 자동 적용되지는 않습니다.",),
        "report-top", ("report-favorite", "report-permissions")),
    _feature(
        "report-visibility", "report_management", "공개·비공개 전환",
        ("비공개", "공개 전환"), ("잠금", "private", "업로더"),
        "conditional", ("search", "web_report"), ("uploader",), "업로더만 전환 가능",
        "비공개 보고서는 업로더와 위임 편집자에게만 보입니다.",
        ("검색 목록의 자물쇠 또는 리포트 상단의 공개 범위 버튼을 누릅니다.",),
        ("권한 없는 사용자에게는 보고서 존재 자체가 보이지 않습니다.",),
        "report-top", ("report-permissions", "web-account")),
    _feature(
        "report-trash", "report_management", "휴지통 이동과 복원",
        ("휴지통", "보고서 삭제", "세션 복원"), ("30일", "trash", "복원 문의"),
        "conditional", ("search", "web_report"), ("uploader",), "업로더만 휴지통 이동 가능",
        "삭제 버튼은 즉시 영구 삭제하지 않고 세션을 30일 보관되는 휴지통으로 옮깁니다.",
        ("검색 목록 또는 리포트 상단의 휴지통 버튼을 누르고 확인합니다.",
         "복원이 필요하면 보관 기간 안에 관리자에게 세션 정보를 전달합니다."),
        ("30일 이후에는 관리자가 영구 정리할 수 있습니다.",),
        "dashboard", ("report-permissions",)),
    _feature(
        "report-permissions", "report_management", "편집 권한 위임",
        ("편집자 위임", "편집 권한"), ("권한 설정", "editor", "업로더", "콘텐츠 편집"),
        "conditional", ("web_report",), ("uploader", "editor"), "업로더가 로그인 사용자에게 부여",
        "업로더는 다른 로그인 사용자에게 코멘트·Note 등 콘텐츠 편집 권한을 부여할 수 있습니다.",
        ("리포트 설정의 권한 탭을 엽니다.", "후보 사용자를 선택해 편집자로 추가하거나 해제합니다."),
        ("위임 편집자는 삭제·비공개 전환·다른 편집자 부여는 할 수 없습니다.",),
        "report-top", ("web-account", "report-visibility")),
    _feature(
        "honey-local-input", "input_upload", "Honey 로컬 파일·폴더 입력",
        ("LOCAL FILE OPEN", "폴더 열기", "드래그앤드롭"),
        ("파일 열기", "하위 폴더", "RT 폴더", "CT 폴더", "HT 폴더", "입력 누적"),
        "available", ("honey",), ("all",), "Honey 앱",
        "파일, 폴더 또는 드래그앤드롭으로 입력을 추가하고 온도 폴더 역할도 자동 인식합니다.",
        ("LOCAL FILE OPEN의 화살표에서 파일 열기 또는 폴더 열기를 선택합니다.",
         "폴더를 열거나 끌어 놓으면 하위 파일을 수집합니다.",
         "RT·ROOM, CT·COLD, HT·HOT 폴더는 Temperature 배치의 Role 근거로 사용됩니다."),
        ("새 입력은 기존 목록을 지우지 않고 추가되므로 중복과 순서를 확인합니다.",),
        "honey-buttons", ("honey-d1", "source-arrangement", "temperature-mode")),
    _feature(
        "honey-d1", "input_upload", "Dolphin(D1) 입력",
        ("Dolphin D1", "D1 불러오기"), ("D1 검색", "다중 선택", "외부 저장소"),
        "available", ("honey",), ("all",), "Honey 앱에서 D1 provider 사용 가능 시",
        "Dolphin(D1) 검색 창에서 평가 파일을 찾아 여러 개 선택합니다.",
        ("Honey 왼쪽의 D1 버튼 또는 File 메뉴를 엽니다.", "검색 결과에서 필요한 파일을 다중 선택합니다."),
        ("D1 provider의 검색 범위와 가용성은 실행 환경에 따라 달라질 수 있습니다.",),
        "honey-buttons", ("honey-local-input",)),
    _feature(
        "normal-mode", "input_upload", "Normal 모드",
        ("Normal 모드", "일반 분석 모드"), ("source", "기본 모드", "limit 기준"),
        "available", ("honey", "web_report"), ("all",), "모든 Product Type",
        "하나 이상의 source를 같은 기준으로 분석하는 기본 Web Report 모드입니다.",
        ("New Report에서 Normal을 선택합니다.", "Source 배치에서 Legend·색·순서를 확인한 뒤 생성합니다."),
        ("최상단 source가 Limit 기준입니다.",),
        "new-report", ("source-arrangement",)),
    _feature(
        "compare-mode", "input_upload", "Compare 모드",
        ("Compare 모드", "Before After 배치"), ("Compare", "Before", "After", "대표 source"),
        "conditional", ("honey", "web_report"), ("all",), "source 2개 이상",
        "여러 source를 Before와 After 두 그룹으로 나눠 Map·Log·산포·동일성을 비교합니다.",
        ("New Report에서 Compare를 선택합니다.", "각 그룹에 source를 하나 이상 배치합니다.",
         "그룹 안 순서를 정하고 항목을 더블클릭해 Legend를 바꿀 수 있습니다."),
        ("업로드 순서는 After 다음 Before이며 After 최상단이 Limit와 Log 대표 source입니다.",),
        "new-report", ("compare-analysis", "source-arrangement")),
    _feature(
        "dut-mode", "input_upload", "DUT 모드",
        ("DUT 모드", "DUT source 분할"), ("DUT", "site", "source 1개", "색 지정"),
        "conditional", ("honey", "web_report"), ("all",), "입력 source 1개",
        "한 source의 DUT 값을 기준으로 웹 리포트 source를 분리해 비교합니다.",
        ("New Report에서 DUT를 선택합니다.", "생성 전에 DUT별 표시 색을 확인하거나 변경합니다."),
        ("입력 source가 정확히 하나여야 합니다.",),
        "new-report", ("source-arrangement", "rawdata-options")),
    _feature(
        "temperature-mode", "input_upload", "Temperature 모드",
        ("Temperature 모드", "온도 분석 모드", "RT CT HT"),
        ("Temperature", "RT", "CT", "HT", "ROOM", "COLD", "HOT", "Limit 파일"),
        "conditional", ("honey", "web_report"), ("all",), "PMIC·SECURITY 전용",
        "RT를 기준으로 CT·HT corner를 그룹화하고 온도별 Fail을 분석합니다.",
        ("PMIC 또는 SECURITY를 선택한 뒤 Temperature를 고릅니다.",
         "각 source의 Group·Role(RT/CT/HT)·색과 순서를 확인합니다.",
         "필요하면 .lt 또는 .pds Limit 파일을 지정해 Fail Bin 표기를 보강합니다."),
        ("각 그룹의 RT가 Limit 기준이며 Yield는 RT source만 계산합니다.",),
        "new-report", ("source-arrangement", "issue-table-temp", "map-analysis")),
    _feature(
        "source-arrangement", "input_upload", "Source 배치와 Legend",
        ("Source 배치", "Legend 설정", "Limit 기준 Source"),
        ("순서 변경", "색", "다중 선택", "최상단", "Group", "Role"),
        "available", ("honey",), ("all",), "Web Report 생성 직전",
        "표시 이름·색·순서를 정하며 표의 위에서 아래 순서가 웹 리포트 순서가 됩니다.",
        ("Legend 셀을 편집하고 색 셀을 더블클릭합니다.",
         "Ctrl·Shift로 여러 행을 선택해 위·아래 또는 맨 위·맨 아래로 이동합니다."),
        ("최상단 source가 Limit 기준입니다. Temperature에서는 그룹과 Role 순서도 함께 확정됩니다.",),
        "new-report", ("normal-mode", "compare-mode", "temperature-mode")),
    _feature(
        "upload-metadata", "input_upload", "보고서 정보 입력과 수정",
        ("보고서 정보 입력", "Session Name", "Step 입력"),
        ("Product Type", "Family Product", "Product", "LOT ID", "Process", "Part ID"),
        "available", ("honey", "web_report"), ("all", "editor"), "생성 시 입력, 수정은 Honey 편집 권한 필요",
        "검색과 리포트 상단에 표시할 제품·LOT·공정·STEP 정보를 입력합니다.",
        ("Session Name, Family Product, Product, LOT ID, Process, Step을 입력합니다.",
         "등록된 Part ID를 선택하면 WF Size·Gross Die 등 기준정보가 보고서에 연결됩니다.",
         "업로드 후 리포트 상단 연필 버튼은 Honey에서 메타 수정 창을 엽니다."),
        ("Product Type은 업로드 후 수정할 수 없습니다.",),
        "upload-info", ("report-permissions",)),
    _feature(
        "rawdata-hub", "rawdata_excel", "Rawdata 편집 허브",
        ("Rawdata 편집", "Rawdata 허브", "현재 상태"),
        ("전처리", "저장", "해제", "원본 불변", "전 탭 재계산"),
        "conditional", ("honey", "web_report"), ("editor",), "Honey에서 편집 가능한 Web Report를 연 상태",
        "원본을 보존하는 전처리와 Excel 원본 수정을 한 화면에서 선택합니다.",
        ("Honey에서 대상 리포트를 열고 Rawdata edit를 누릅니다.",
         "현재 상태에서 적용 중인 전처리를 개별 또는 전체 해제합니다.",
         "Options·Item Select·Yield 계산을 바꾼 뒤 반드시 저장합니다."),
        ("웹 Raw Data 탭은 현재 사용하지 않으며 Rawdata 작업은 Honey에서 진행합니다.",),
        "excel-tools", ("rawdata-options", "rawdata-item-select", "yield-basis", "rawdata-excel-edit")),
    _feature(
        "rawdata-options", "rawdata_excel", "Rawdata Options",
        ("Bin1 only", "Outlier 제거", "DUT 제외"),
        ("mean", "표준편차", "source별 DUT", "Wafer Map", "원본 보존"),
        "conditional", ("honey", "web_report"), ("editor",), "Rawdata 편집 허브",
        "Pass die만 남기기, 평균 기준 이상치 제거, 특정 source의 DUT 제외를 적용합니다.",
        ("Bin1 only를 켜거나 Outlier의 mean ± kσ 값을 지정합니다.",
         "DUT 제외에서 source와 DUT 번호를 선택합니다.", "현재 상태를 확인하고 저장합니다."),
        ("Summary·Yield·CPK·Issue·Distribution·Trim·Map에 모두 반영되지만 원본은 바뀌지 않습니다.",),
        "excel-tools", ("rawdata-hub", "yield-basis")),
    _feature(
        "rawdata-item-select", "rawdata_excel", "Rawdata Item Select",
        ("Item Select", "항목 제외"), ("표시 항목", "제외 항목", "검색", "두 목록"),
        "conditional", ("honey", "web_report"), ("editor",), "Rawdata 편집 허브",
        "리포트에 표시할 측정 항목과 제외할 항목을 두 목록에서 관리합니다.",
        ("검색으로 항목을 좁혀 찾습니다.", "화살표로 표시·제외 목록 사이를 이동한 뒤 저장합니다."),
        ("검색은 항목을 숨기기만 하고, 실제 제외는 목록을 이동해야 적용됩니다.",),
        "excel-tools", ("rawdata-hub",)),
    _feature(
        "yield-basis", "rawdata_excel", "source별 Yield 계산 기준",
        ("Yield 계산", "수율 분모"), ("자동", "Gross Die", "Test data", "실시간 수율"),
        "conditional", ("honey", "web_report"), ("editor",), "Rawdata 편집 허브",
        "각 source의 수율 분모를 자동·Gross Die·Test data 중에서 선택합니다.",
        ("Yield 계산 페이지에서 source별 기준을 고릅니다.", "표시되는 예상 수율을 확인하고 저장합니다."),
        ("선택한 기준은 Yield와 Summary 등 수율 표시 전체에 적용됩니다.",),
        "excel-tools", ("yield-analysis", "rawdata-options")),
    _feature(
        "rawdata-excel-edit", "rawdata_excel", "Rawdata 원본 수정",
        ("Rawdata 원본 수정", "Excel 원본 편집"),
        ("source 선택", "시트 삭제", "변경 검토", "반영 승인", "되돌릴 수 없음"),
        "conditional", ("honey",), ("editor",), "Honey와 Microsoft Excel 필요",
        "선택한 source 원본을 Excel에서 직접 수정하고 변경 검토 후 서버에 교체 반영합니다.",
        ("수정할 source를 체크하고 Excel 열기를 누릅니다.",
         "Excel에서 값을 수정하거나 source 시트를 삭제한 뒤 저장하고 닫습니다.",
         "변경 개요와 셀 변경을 검토한 뒤 반영을 승인합니다."),
        ("원본 교체는 화면에서 되돌릴 수 없습니다. 먼저 Excel Download로 사본을 보관하세요.",
         "서버 백업은 직전 한 세대만 남으므로 복구가 필요하면 관리자에게 즉시 문의하세요."),
        "excel-tools", ("excel-download", "rawdata-hub")),
    _feature(
        "excel-report", "rawdata_excel", "Excel Report 생성",
        ("Excel Report", "엑셀 보고서 생성"),
        ("출력 시트", "분석 항목", "Bin1 Only", "Auto Upload", "xlsx"),
        "available", ("honey",), ("all",), "Honey 분석 엔진과 Excel 사용 가능 시",
        "선택한 시트와 항목으로 로컬 xlsx 분석 보고서를 생성하고 필요하면 자동 업로드합니다.",
        ("Start를 누르고 Summary·Yield·CPK·Fail Item·Issue Table·Distribution 시트를 선택합니다.",
         "항목, 파일명, 정리 모드, 색과 Server Auto Upload를 확인합니다."),
        ("Yield를 끄면 Yield에 의존하는 Fail Item과 Issue Table도 비활성화됩니다.",),
        "excel-report", ("excel-download",)),
    _feature(
        "excel-download", "rawdata_excel", "Excel Download와 Upload",
        ("Excel Download", "Excel Upload"), ("내보내기", "백업", "리포트 xlsx", "수정 업로드"),
        "conditional", ("honey", "web_report"), ("editor",), "Honey에서 Web Report를 연 상태",
        "웹 리포트를 xlsx로 저장하거나 수정한 Excel 보고서를 서버에 다시 반영합니다.",
        ("Honey의 Excel Down으로 저장 위치와 내보낼 내용을 고릅니다.",
         "원본 수정 전에는 내려받은 파일을 별도 백업으로 보관합니다."),
        ("Rawdata 원본 교체와 일반 리포트 내보내기의 목적을 구분하세요.",),
        "excel-tools", ("rawdata-excel-edit", "excel-report")),
    _feature(
        "honey-options", "support", "Honey Options",
        ("Honey Options", "Distribution 색상"), ("기본 Product Type", "Family", "팔레트", "F10"),
        "available", ("honey",), ("all",), "Honey 앱",
        "기본 Product Type·Family와 Distribution source 색 팔레트를 저장합니다.",
        ("Settings 메뉴 또는 왼쪽 Options 버튼을 누릅니다.", "기본값과 색을 바꾸고 저장합니다."),
        ("Web Report 생성 직전 Source 배치에서 고른 색이 해당 리포트에서는 우선합니다.",),
        "options", ("source-arrangement",)),
    _feature(
        "honey-update", "support", "Honey 업데이트",
        ("Honey 업데이트", "자동 설치", "ZIP 다운로드"),
        ("나중에", "업데이트 공지", "sha256", "update.log", "재실행"),
        "available", ("honey",), ("all",), "새 버전이 배포된 경우",
        "새 버전을 자동 설치하거나 ZIP으로 내려받고, 설치 후 변경 공지를 한 번 표시합니다.",
        ("업데이트 안내에서 자동 설치·ZIP 다운로드·나중에 중 하나를 선택합니다.",
         "자동 설치가 비활성화되면 ZIP을 내려받아 Honey 종료 후 수동으로 덮어씁니다."),
        ("업데이트 실패 시 Honey 설치 폴더의 log/update.log를 확인해 오류번호와 함께 전달하세요.",),
        "options", ("honey-errors",)),
    _feature(
        "voc", "support", "VOC와 문의",
        ("VOC", "문의 게시판"), ("Confluence", "서버 VOC", "스크린샷", "댓글", "Open", "Close"),
        "available", ("honey", "search", "support"), ("all",), "모든 사용자",
        "현재 Honey와 검색 화면의 VOC 버튼은 Confluence를 열며 서버 VOC 게시판도 별도 제공됩니다.",
        ("Honey 도움말 메뉴 또는 검색 화면의 VOC 버튼으로 Confluence를 엽니다.",
         "서버 게시판은 /pe/report/voc에서 글·스크린샷·댓글을 등록하고 처리 상태를 확인합니다."),
        ("두 진입점이 현재 서로 다르므로 문의 위치를 확인하세요.",),
        "trouble", ("honey-errors",)),
    _feature(
        "report-common-ui", "report_tabs", "리포트 공통 화면과 상단 버튼",
        ("리포트 상단 버튼", "자동저장", "화면 설정"),
        ("저장", "이탈 경고", "글꼴", "확대율", "테마", "연필", "권한"),
        "available", ("web_report",), ("all", "editor", "uploader"), "권한에 따라 버튼이 다르게 표시",
        "저장 상태, 개인 중요 표시, 메타 수정, 공개 범위, 화면 설정과 권한을 관리합니다.",
        ("상단 저장 점으로 저장 중·완료 상태를 확인합니다.",
         "설정에서 글꼴, 100·110·125·150% 확대율, 편집자 권한을 조정합니다.",
         "저장하지 않고 나가면 저장·버리기·취소 중 선택합니다."),
        ("메타 수정은 Honey에서만 열리며 삭제·비공개·권한 부여는 업로더 전용입니다.",),
        "report-top", ("report-important", "report-permissions")),
    _feature(
        "summary", "report_tabs", "Summary",
        ("Summary", "ENGR Comment", "Issue Status"),
        ("Yield 카드", "Open", "Close", "Yield Comment", "CPK Comment", "ETC Comment", "TEMP Comment"),
        "available", ("web_report",), ("all", "editor"), "모든 Web Report",
        "전체·source 수율과 Issue 상태를 요약하고 엔지니어 코멘트를 기록합니다.",
        ("Yield 카드나 표를 눌러 관련 상세로 이동합니다.",
         "Issue Status에서 Yield·CPK·TEMP·ETC의 Open·Close 건수를 확인합니다.",
         "권한이 있으면 ENGR Comment를 입력해 자동 저장합니다."),
        ("코멘트의 Item·Note 태그는 해당 상세·셀·시트로 이동합니다.",),
        "summary", ("yield-analysis", "issue-table", "note")),
    _feature(
        "yield-analysis", "report_tabs", "Yield Analysis",
        ("Yield Analysis", "Fail Bin", "STEP 수율"),
        ("전체 수율", "누적 수율", "TNO", "Bin", "검색", "Excel Down", "RT 수율"),
        "available", ("web_report",), ("all",), "모든 Web Report",
        "전체·source·STEP별 수율과 주요 Fail Bin을 조회하고 Excel로 내보냅니다.",
        ("상단 검색과 source/STEP 바로가기로 필요한 구간을 찾습니다.",
         "Bin 또는 TNO를 펼쳐 상세 수량과 비율을 봅니다.", "Excel Down으로 현재 수율 표를 저장합니다."),
        ("Temperature에서는 RT source만 Yield 계산에 포함되고 CT·HT는 별도 Temp 이슈에서 봅니다.",),
        "yield", ("yield-basis", "issue-table-temp")),
    _feature(
        "cpk-analysis", "report_tabs", "CPK Analysis",
        ("CPK Analysis", "Limit 역산", "동일 Limit"),
        ("Bin1", "임계값", "operator", "CODE 제외", "Target Cpk", "margin", "Excel Down"),
        "available", ("web_report",), ("all",), "모든 Web Report",
        "Bin1 기준 CPK를 검색·필터링하고 목표 CPK의 참고 Limit을 역산합니다.",
        ("Item·source를 검색하고 CPK 임계값과 < 또는 > 연산자를 선택합니다.",
         "동일 Limit 제외·전체·동일 Limit만 필터와 CODE 단위 숨김을 조합합니다.",
         "행을 선택해 Target Cpk와 margin으로 참고 Limit을 계산하거나 복사·초기화합니다."),
        ("역산 Limit은 참고값이며 원본 규격을 자동 변경하지 않습니다.",
         "Temperature CT·HT는 RT Bin1 die와 RT Limit을 사용합니다."),
        "cpk", ("issue-table",)),
    _feature(
        "issue-table", "report_tabs", "Issue Table",
        ("Issue Table", "PTE Comment", "Dev Team Comment"),
        ("Yield", "CPK", "ETC", "Open", "Close", "행 선택", "Map", "Distribution", "태그", "서식"),
        "available", ("web_report",), ("all", "editor"), "모든 Web Report",
        "Yield·CPK·ETC 이슈를 Map·산포와 함께 검토하고 상태와 코멘트를 관리합니다.",
        ("검색 또는 YIELD·CPK·ETC 바로가기로 행을 찾습니다.",
         "Map·Distribution 미니 셀을 눌러 상세를 열고 source를 펼칩니다.",
         "편집 메뉴에서 이슈 추가, 행 선택·숨김, 일괄 Open·Close·삭제를 수행합니다.",
         "코멘트에서 Item·Note 셀·Note 시트 태그와 굵게·색 서식을 사용합니다."),
        ("편집 기능은 업로더 또는 위임 편집자에게만 보입니다.",),
        "issue", ("issue-table-temp", "distribution", "map-analysis", "note")),
    _feature(
        "issue-table-temp", "report_tabs", "Issue Table Temp",
        ("Issue Table Temp", "Temperature Issue"),
        ("RT Limit", "CT", "HT", "중복 Fail", "100% 초과", "Temp Map"),
        "conditional", ("web_report",), ("all", "editor"), "Temperature 모드 전용",
        "CT·HT의 모든 항목을 각 그룹 RT Limit으로 다시 판정해 온도 Fail 이슈를 보여줍니다.",
        ("Temperature 리포트에서 Issue Table Temp 탭을 엽니다.",
         "검색·Excel·Map·Distribution으로 항목을 확인하고 상태·코멘트를 편집합니다."),
        ("한 die가 여러 항목에서 Fail일 수 있어 항목별 fail% 합은 100%를 넘을 수 있습니다.",),
        "issue", ("temperature-mode", "map-analysis", "yield-analysis")),
    _feature(
        "distribution", "report_tabs", "Distribution",
        ("Distribution", "산포도 갤러리"),
        ("CPK 필터", "Fail Only", "Limit 없는 Data", "Bin1", "P/F 제거", "source 강조", "온도 그룹"),
        "available", ("web_report",), ("all",), "모든 Web Report",
        "항목별 ECDF 카드를 필터링하고 source나 Temperature 그룹을 강조합니다.",
        ("전체·CPK·Fail·Limit·Bin1·P/F 필터를 조합합니다.",
         "source 범례나 Temperature RT·CT·HT·그룹을 선택해 비교합니다.", "카드를 눌러 Item Detail을 엽니다."),
        ("Temperature의 Bin1 필터는 RT 기준입니다.",),
        "distribution", ("item-detail", "note")),
    _feature(
        "item-detail", "report_tabs", "Item Detail",
        ("Item Detail", "CDF", "Histogram"),
        ("source 선택", "Fail die", "Map chip", "차트 주석", "클립보드 PNG", "이전 다음"),
        "available", ("web_report",), ("all", "editor"), "Distribution 또는 Issue Table에서 진입",
        "한 항목의 CDF·Histogram·source별 통계·Fail die·Map 좌표를 상세 분석합니다.",
        ("source를 선택하고 CDF 또는 Histogram 표시를 바꿉니다.",
         "Map chip을 고르면 해당 die 값을 표에서 확인합니다.",
         "차트를 PNG로 복사하거나 권한이 있으면 도형·텍스트·코멘트를 저장해 Note로 보냅니다."),
        ("Alt+위·아래로 이전·다음 항목을 이동할 수 있습니다.",),
        "distribution", ("distribution", "map-analysis", "note")),
    _feature(
        "map-analysis", "report_tabs", "Map Analysis",
        ("Map Analysis", "Bin Map", "Temperature Map"),
        ("TNO", "DUT", "열 수", "좌표", "크게 보기", "source 범례", "RT", "CT", "HT"),
        "available", ("web_report",), ("all",), "모든 Web Report, Temperature Map은 Temperature 전용",
        "Bin·TNO·DUT 또는 Temperature 항목 기준으로 웨이퍼 맵을 비교하고 확대합니다.",
        ("표시 축과 한 줄 열 수, source·item을 선택합니다.",
         "맵을 눌러 크게 보고 hover 좌표와 현재 맵 기준 범례·비율을 확인합니다."),
        ("Temperature에서는 Bin·TNO가 RT 맵, Temperature Map이 CT·HT 맵을 표시합니다.",),
        "map", ("issue-table-temp", "item-detail")),
    _feature(
        "characteristic", "report_tabs", "Characteristic",
        ("Characteristic", "특성 분석"),
        ("Trim", "Shmoo", "BV", "Analog Chart", "TCB", "DVO"),
        "available", ("web_report",), ("all",), "모든 Web Report",
        "특성 분석 도구를 모은 탭이며 현재 Trim Analysis를 사용할 수 있습니다.",
        ("Characteristic 탭을 열고 Trim Analysis를 선택합니다.",),
        ("Shmoo·BV·Analog Chart·TCB·DVO는 현재 준비 중입니다.",),
        "trim", ("trim-analysis", "characteristic-coming-soon")),
    _feature(
        "trim-analysis", "report_tabs", "Trim Analysis",
        ("Trim Analysis", "Trim Item Matching"),
        ("분석 시작", "항목 매칭", "override", "분포", "Excel Download", "Copy"),
        "available", ("web_report",), ("all", "editor"), "Characteristic 탭",
        "Trim 항목을 자동 매칭하고 사용자가 매칭을 보정한 뒤 분포와 결과를 확인합니다.",
        ("초록색 분석 시작 버튼을 누릅니다.",
         "Item Matching에서 검색·드래그앤드롭으로 매칭을 보정하거나 초기화합니다.",
         "페이지별 차트를 확인하고 Excel Download 또는 Copy를 사용합니다."),
        ("탭에 들어가기만 해서는 분석을 시작하지 않습니다.",),
        "trim", ("characteristic",)),
    _feature(
        "characteristic-coming-soon", "report_tabs", "준비 중인 Characteristic 분석",
        ("Shmoo", "BV", "Analog Chart", "TCB", "DVO"),
        ("준비 중", "비활성", "Characteristic"),
        "coming_soon", ("web_report",), ("all",), "화면에는 표시되지만 아직 사용 불가",
        "Characteristic의 Shmoo·BV·Analog Chart·TCB·DVO 분석은 현재 준비 중입니다.",
        ("현재는 Trim Analysis를 사용합니다.",),
        ("메뉴가 보여도 분석 결과를 제공하는 기능으로 안내하지 않습니다.",),
        "trim", ("characteristic", "trim-analysis")),
    _feature(
        "note", "report_tabs", "Note",
        ("Note", "노트 시트", "Note 태그"),
        ("스프레드시트", "수식", "서식", "이미지", "셀 태그", "시트 링크", "10MB", "200장"),
        "available", ("web_report",), ("all", "editor"), "모든 Web Report",
        "스프레드시트 형식으로 분석 메모를 작성하고 차트 이미지와 셀·시트 링크를 관리합니다.",
        ("셀 값·수식·서식을 편집하고 시트를 추가합니다.",
         "현재 셀에 Tag를 만들거나 코멘트에서 Note 셀·시트 링크를 사용합니다.",
         "Distribution·Item Detail 차트를 이미지로 붙인 뒤 Note 저장을 누릅니다."),
        ("Note JSON은 10MB, 차트 이미지는 장당 2MB·세션당 200장까지입니다.",
         "시트를 바꾼 뒤에도 반드시 Note 저장을 눌러야 합니다."),
        "note", ("summary", "issue-table", "item-detail")),
    _feature(
        "compare-analysis", "report_tabs", "Compare 분석",
        ("Compare 분석", "동일성 검증", "Log 비교"),
        ("Map 비교", "Bin Yield", "불일치 좌표", "Before", "After", "Distribution 비교", "noise gate"),
        "conditional", ("web_report",), ("all",), "Compare 모드 전용",
        "Before·After 그룹을 Map, 대표 Log, 통합 산포와 동일성 등급으로 비교합니다.",
        ("Map 비교에서 공통 좌표의 source별 Bin과 불일치 die를 확인합니다.",
         "Log 비교에서 item·Limit 차이·Gap 필터를 사용합니다.",
         "Distribution 비교에서 그룹 pool 통계를 보고 동일성 검증에서 등급과 noise gate를 확인합니다."),
        ("Log 대표는 각 그룹 최상단 source이며 그룹 배치는 Honey에서 결정합니다.",),
        "compare", ("compare-mode",)),
    _feature(
        "web-raw-data", "report_tabs", "웹 Raw Data 탭",
        ("웹 Raw Data", "Raw Data 탭"), ("비활성", "Honey Rawdata", "원본"),
        "coming_soon", ("web_report", "honey"), ("all",), "웹 탭은 현재 비활성",
        "웹 리포트의 Raw Data 탭은 현재 사용하지 않으며 Rawdata 편집은 Honey에서 수행합니다.",
        ("Honey에서 대상 리포트를 연 뒤 Rawdata edit를 사용합니다.",),
        ("웹 화면에서 원본 데이터를 직접 편집할 수 있다고 안내하지 않습니다.",),
        "excel-tools", ("rawdata-hub",)),
    _feature(
        "honey-errors", "support", "Honey 오류번호와 문제 해결",
        ("Honey 오류번호", "오류 리포트"), ("offline queue", "로그", "재시도", "서버 연결"),
        "available", ("honey", "support"), ("all",), "오류 발생 시",
        "Honey 작업 실패 시 표시되는 오류번호와 작업 로그로 문의·재현 정보를 남깁니다.",
        ("오류번호, 수행한 작업, 대상 세션, 발생 시각을 기록합니다.",
         "네트워크가 끊겼다면 연결 복구 후 재시도하고 반복되면 VOC로 전달합니다."),
        ("오프라인에서는 오류 보고가 대기열에 남았다가 연결 후 전송될 수 있습니다.",),
        "trouble", ("voc", "honey-update")),
    _feature(
        "engr-chatbot", "support", "ENGR 챗봇",
        ("ENGR 챗봇", "기능 질문", "새 대화"),
        ("관리자 테스트", "보고서 찾기", "세션 정보", "수율", "CPK", "도움말 검색", "도움말 링크"),
        "conditional", ("chatbot", "search", "web_report"), ("admin",), "현재 관리자 화면에서만 노출",
        "보고서 이력 조회와 HONEY 기능 존재·사용법 질문에 근거와 바로가기를 제공합니다.",
        ("검색 또는 세션 화면 우하단 챗봇을 엽니다.",
         "기능명과 함께 ‘있어?’, ‘어떻게 써?’처럼 질문합니다.",
         "새 대화를 누르면 앞선 세션·항목 문맥을 지웁니다."),
        ("현재 관리자 테스트 기능이며 일반 사용자에게는 위젯과 API가 노출되지 않습니다.",
         "데이터 수정·삭제는 수행하지 않습니다."),
        "trouble", ("report-search", "report-common-ui")),
)


def normalize(text):
    """한글·영문 대소문자와 공백·기호 차이를 없앤 검색 키."""
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[^0-9a-z가-힣]+", "", value)


def _tokens(text):
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return tuple(re.findall(r"[0-9a-z가-힣]+", value))


def _score(feature, query):
    qn = normalize(query)
    if not qn:
        return 1
    exact = [feature["id"], feature["title"], *feature["aliases"]]
    exact_norm = [normalize(value) for value in exact]
    if qn in exact_norm:
        return 1000

    score = 0
    for value in exact_norm:
        if value and value in qn:
            score = max(score, 650)
        elif qn in value:
            score = max(score, 520)

    for keyword in feature["keywords"]:
        kn = normalize(keyword)
        if kn and kn in qn:
            score += 130

    query_tokens = set(_tokens(query))
    if query_tokens:
        title_tokens = set(_tokens(feature["title"]))
        alias_tokens = set(_tokens(" ".join(feature["aliases"])))
        keyword_tokens = set(_tokens(" ".join(feature["keywords"])))
        body_tokens = set(_tokens(feature["summary"] + " " + " ".join(feature["usage"])))
        score += 90 * len(query_tokens & title_tokens)
        score += 75 * len(query_tokens & alias_tokens)
        score += 45 * len(query_tokens & keyword_tokens)
        score += 10 * len(query_tokens & body_tokens)
    return score


def search_features(query="", *, category=None, surface=None, status=None, limit=None):
    """필터와 자연어 검색 결과. 원본 수정 방지를 위해 얕은 복사본을 반환한다."""
    rows = []
    for index, feature in enumerate(FEATURES):
        if category and feature["category"] != category:
            continue
        if surface and surface not in feature["surfaces"]:
            continue
        if status and feature["status"] != status:
            continue
        score = _score(feature, query)
        # 공통 단어 한두 개가 설명문에 우연히 걸린 결과는 기능 검색으로 보지 않는다.
        if query and score < 100:
            continue
        rows.append((score, index, feature))
    if query:
        rows.sort(key=lambda row: (-row[0], row[1]))
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    return [dict(feature) for _score_value, _index, feature in rows]


def get_feature(feature_id):
    wanted = normalize(feature_id)
    for feature in FEATURES:
        if normalize(feature["id"]) == wanted:
            return dict(feature)
    return None


def best_feature_score(query):
    return max((_score(feature, query) for feature in FEATURES), default=0)


_GENERIC_FEATURE_QUESTIONS = {
    "기능", "기능알려줘", "어떤기능있어", "무슨기능있어", "뭐할수있어",
    "사용법", "도움말", "help",
}
_QUESTION_CUES = (
    "기능", "사용법", "어디서", "가능", "지원", "할 수", "쓸 수", "사용할 수",
    "어떻게 써", "어떻게 사용", "어떻게 해", "메뉴", "탭", "모드",
)


def is_generic_feature_question(text):
    return normalize(text) in _GENERIC_FEATURE_QUESTIONS


def is_feature_question(text):
    """기존 이력 검색과 충돌하지 않는 기능 안내 질문 판정."""
    value = str(text or "").strip()
    normalized = normalize(value)
    if normalized in {"뭐할수있어", "사용법", "도움말", "help"}:
        return False
    if is_generic_feature_question(value):
        return True
    folded = unicodedata.normalize("NFKC", value).casefold()
    if "기능" in folded or any(phrase in folded for phrase in (
            "지원해", "지원하나요", "사용할 수 있어", "사용 가능")):
        return True
    explicit = any(cue in folded for cue in _QUESTION_CUES)
    return explicit and best_feature_score(value) >= 250


def validate_catalog():
    """스키마·중복·연관 참조를 검증하고 문제 목록을 반환한다."""
    errors = []
    ids = set()
    normalized_ids = {}
    aliases = {}
    for feature in FEATURES:
        feature_id = feature.get("id", "?")
        actual_fields = set(feature)
        expected_fields = set(FEATURE_FIELDS)
        if actual_fields != expected_fields:
            missing = sorted(expected_fields - actual_fields)
            extra = sorted(actual_fields - expected_fields)
            errors.append(
                f"{feature_id}: fields missing={','.join(missing) or '-'} "
                f"extra={','.join(extra) or '-'}")
            continue
        for field in ("id", "category", "title", "status", "availability",
                      "summary", "help_anchor"):
            if not isinstance(feature[field], str) or not feature[field].strip():
                errors.append(f"{feature_id}: invalid {field}")
        for field in ("aliases", "keywords", "surfaces", "audience", "usage",
                      "cautions", "related_ids"):
            if not isinstance(feature[field], list) or not all(
                    isinstance(value, str) and value.strip() for value in feature[field]):
                errors.append(f"{feature_id}: invalid {field}")
        if feature_id in ids:
            errors.append(f"duplicate id: {feature_id}")
        ids.add(feature_id)
        normalized_id = normalize(feature_id)
        previous_id = normalized_ids.get(normalized_id)
        if previous_id:
            errors.append(f"duplicate normalized id: {feature_id} ({previous_id})")
        normalized_ids[normalized_id] = feature_id
        if feature["status"] not in STATUSES:
            errors.append(f"{feature_id}: invalid status {feature['status']}")

    for feature in FEATURES:
        feature_id = feature.get("id", "?")
        for alias in feature.get("aliases", ()):
            if not isinstance(alias, str) or not alias.strip():
                continue
            key = normalize(alias)
            owner = aliases.get(key)
            if owner:
                errors.append(f"duplicate alias: {alias} ({owner}, {feature_id})")
            aliases[key] = feature_id
            id_owner = normalized_ids.get(key)
            if id_owner and id_owner != feature_id:
                errors.append(f"alias duplicates id: {alias} ({id_owner}, {feature_id})")
    for feature in FEATURES:
        for related_id in feature.get("related_ids", ()):  # schema 오류가 있어도 나머지 검사
            if related_id not in ids:
                errors.append(f"{feature['id']}: unknown related id {related_id}")
    return errors


_ERRORS = validate_catalog()
if _ERRORS:
    raise RuntimeError("invalid help catalog: " + "; ".join(_ERRORS))

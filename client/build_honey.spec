# PyInstaller spec — 사용: pyinstaller build_honey.spec
# onedir + windowed(console 없음). PyInstaller 6.x 기준.
# onedir 인 이유: onefile 은 실행마다 임시폴더로 전체 압축해제 → 첫 로딩이 느림.
# onedir 은 dist/Honey/ 폴더(Honey.exe + _internal/)로 풀려 있어 시작이 훨씬 빠름.
# 이 폴더를 ZIP 패키지(Honey-<version>.zip)로 묶어 배포한다.
# PyQt6 plugins 누락 시 hiddenimports / collect 옵션 추가.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules

# 빌드 환경 의존성 가드 — 아래 패키지가 빌드 venv 에 없으면 collect_submodules/collect_all
# 은 조용히 빈 리스트를 반환해 '런타임에 ModuleNotFoundError 로 죽는 깨진 exe' 가 그대로
# 배포된다. 여기서 명시적으로 import 해 미설치 시 빌드를 즉시 실패시킨다 (broken exe 방지).
import requests_toolbelt  # noqa: F401  transport/uploader.py 가 정적 import
import xlwings             # noqa: F401
import pyarrow             # noqa: F401  web_report parquet encoding
# PyQt6-WebEngine — embedded_browser.py 내장 브라우저(Chromium). 이 import 는 미설치뿐
# 아니라 PyQt6 ↔ WebEngine Qt6 런타임 버전 어긋남(DLL 로드 실패)도 빌드 시점에 잡아낸다.
# QApplication 생성 전이라 import 순서는 안전(embedded_browser.py 주석 참조).
import PyQt6.QtWebEngineWidgets  # noqa: F401  내장 브라우저 미번들 시 broken exe 방지

# xlwings 는 자체 데이터/바이너리(.xlam, dll)를 동봉해야 동작
_xw_datas, _xw_binaries, _xw_hidden = collect_all('xlwings')

# pyarrow — Web Report parquet bytes encoding.
_pa_datas, _pa_binaries, _pa_hidden = collect_all('pyarrow')

import os as _os
_repo_root = _os.path.normpath(_os.path.join(SPECPATH, '..'))

a = Analysis(
    ['honey_main.py'],
    pathex=[_repo_root],
    binaries=_xw_binaries + _pa_binaries,
    datas=_xw_datas + _pa_datas + [('honey_main.ui', '.'), ('upload_dialog.ui', '.'),
                       (_os.path.join(_repo_root, 'd1', 'd1_browser.ui'), 'd1'),
                       ('file_order.ui', '.'),
                       ('report_settings.ui', '.'),
                       # honey_parse 패키지 내부 .ui — 런타임에 패키지 경로 기준으로 로드하므로
                       # 대상 폴더를 소스와 같은 트리로 유지한다.
                       ('honey_parse/mddi/datalog_parser/ui/optional_sheets_dialog.ui',
                        'honey_parse/mddi/datalog_parser/ui'),
                       (_os.path.join(_repo_root, 'Honey_img.png'), '.')],  # 창/작업표시줄 아이콘
    hiddenimports=(
        ['PyQt6.sip', 'PyQt6.uic', 'win32com', 'win32com.client', 'pythoncom',
         'pywintypes', 'pandas', 'numpy']
        + collect_submodules('requests_toolbelt')
        + _xw_hidden
        + _pa_hidden
        + collect_submodules('report_generator')
    + collect_submodules('honey_parse')
    + collect_submodules('pystdf')
    + collect_submodules('transport')
    + collect_submodules('d1')
    + collect_submodules('honey_ui')
    + collect_submodules('report_flow')
    + collect_submodules('excel_edit')
    + collect_submodules('excel_download')
    + collect_submodules('xlsxwriter')   # Excel Download 기본 엔진(Excel 없이 xlsx 생성)
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5'],  # PyQt5 잔재(PyQt5-Qt5/PyQt5_sip) 가 설치돼 있어도 PyQt6 와
                         # 'multiple Qt bindings' 충돌 없이 빌드되게 강제 제외. 앱은 PyQt6 전용.
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir: 바이너리/데이터는 COLLECT 로 폴더에 분리
    name='Honey',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='honey.ico',   # 꿀단지 실행 파일 아이콘
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Honey',   # → dist/Honey/ (Honey.exe + _internal/)
)

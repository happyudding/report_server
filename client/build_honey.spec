# PyInstaller spec — 사용: pyinstaller build_honey.spec
# onedir + windowed(console 없음). PyInstaller 6.x 기준.
# onedir 인 이유: onefile 은 실행마다 임시폴더로 전체 압축해제 → 첫 로딩이 느림.
# onedir 은 dist/Honey/ 폴더(Honey.exe + _internal/)로 풀려 있어 시작이 훨씬 빠름.
# 이 폴더를 ZIP 패키지(Honey-<version>.zip)로 묶어 배포한다.
# PyQt5 plugins 누락 시 hiddenimports / collect 옵션 추가.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules

# xlwings 는 자체 데이터/바이너리(.xlam, dll)를 동봉해야 동작
_xw_datas, _xw_binaries, _xw_hidden = collect_all('xlwings')

# PyMuPDF(fitz) — Distribution 시트 PDF→PNG 변환. 바이너리/데이터 포함 필요
_fitz_datas, _fitz_binaries, _fitz_hidden = collect_all('fitz')

import os as _os
_repo_root = _os.path.normpath(_os.path.join(SPECPATH, '..'))

a = Analysis(
    ['honey_main.py'],
    pathex=[_repo_root],
    binaries=_xw_binaries + _fitz_binaries,
    datas=_xw_datas + _fitz_datas + [('honey_main.ui', '.'), ('upload_dialog.ui', '.'),
                       (_os.path.join(_repo_root, 'd1', 'd1_browser.ui'), 'd1'),
                       ('file_order.ui', '.'),
                       ('report_settings.ui', '.')],
    hiddenimports=(
        ['PyQt5.sip', 'PyQt5.uic', 'win32com', 'win32com.client', 'pythoncom',
         'pywintypes', 'pandas', 'numpy']
        + collect_submodules('requests_toolbelt')
        + _xw_hidden
        + _fitz_hidden
        + collect_submodules('report_generator')
    + collect_submodules('honey_parse')
    + collect_submodules('pystdf')
    + collect_submodules('transport')
    + collect_submodules('d1')
    + collect_submodules('honey_ui')
    + collect_submodules('report_flow')
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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

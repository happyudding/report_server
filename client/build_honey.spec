# PyInstaller spec — 사용: pyinstaller build_honey.spec
# onefile + windowed(console 없음). PyInstaller 6.x 기준.
# PyQt5 plugins 누락 시 hiddenimports / collect 옵션 추가.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules

# xlwings 는 자체 데이터/바이너리(.xlam, dll)를 동봉해야 동작
_xw_datas, _xw_binaries, _xw_hidden = collect_all('xlwings')

a = Analysis(
    ['honey_main.py'],
    pathex=[],
    binaries=_xw_binaries,
    datas=_xw_datas,
    hiddenimports=(
        ['PyQt5.sip', 'win32com', 'win32com.client', 'pythoncom', 'pywintypes',
         'pandas', 'numpy']
        + _xw_hidden
        + collect_submodules('report_generator')
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
    a.binaries,
    a.datas,
    [],
    name='Honey',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

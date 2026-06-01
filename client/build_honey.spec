# PyInstaller spec — 사용: pyinstaller build_honey.spec
# onedir + windowed(console 없음). PyInstaller 6.x 기준.
# onedir 인 이유: onefile 은 실행마다 임시폴더로 전체 압축해제 → 첫 로딩이 느림.
# onedir 은 dist/Honey/ 폴더(Honey.exe + _internal/)로 풀려 있어 시작이 훨씬 빠름.
# 이 폴더를 Inno Setup(installer.iss)으로 묶어 HoneySetup.exe 설치본을 만든다.
# PyQt5 plugins 누락 시 hiddenimports / collect 옵션 추가.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules

# xlwings 는 자체 데이터/바이너리(.xlam, dll)를 동봉해야 동작
_xw_datas, _xw_binaries, _xw_hidden = collect_all('xlwings')

a = Analysis(
    ['honey_main.py'],
    pathex=[],
    binaries=_xw_binaries,
    datas=_xw_datas + [('honey_main.ui', '.'), ('upload_dialog.ui', '.'),
                       ('d1_browser.ui', '.'), ('file_order.ui', '.'),
                       ('report_settings.ui', '.'),
                       # 리포트 출력 양식 — xlsx_writer 가 openpyxl 로 열어 값만 채움
                       ('data/templete.xlsx', 'data')],
    hiddenimports=(
        ['PyQt5.sip', 'PyQt5.uic', 'win32com', 'win32com.client', 'pythoncom',
         'pywintypes', 'pandas', 'numpy']
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

# PyInstaller spec — 사용: pyinstaller --workpath build_launcher --distpath dist_launcher build_launcher.spec
# 버전 폴더 방식의 런처(Honey.exe) 를 onefile 로 만든다 → dist_launcher/Honey.exe
#
# onefile 인 이유: 런처는 설치 루트에 단일 파일로 놓여야 하고(폴더가 생기면 versions 구조가
# 지저분해진다), 표준 라이브러리만 쓰므로 자가 압축해제 부담이 작다.
# 무거운 패키지는 excludes 로 확실히 막는다 — 하나라도 딸려오면 런처가 수백 MB 가 된다.
#
# workpath/distpath 를 따로 주는 이유: 앱 spec(build_honey.spec)의 산출 이름도 'Honey' 라
# 기본 경로(build/Honey, dist/Honey)를 공유하면 서로의 캐시를 덮어쓴다.

# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[],
    # tkinter = 업데이트 진행창(런처가 앱을 띄우기 전에 띄운다). 나머지는 함수 안에서
    # 지연 import 되는 것들이라 정적 분석에 안 잡힐 수 있어 명시한다.
    hiddenimports=['tkinter', 'tkinter.ttk',
                   'transport.app_update', 'transport.config', 'client_identity'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # tkinter 는 위에서 포함한다(진행창). 나머지 무거운 것들은 계속 확실히 막는다 —
    # 하나라도 딸려오면 런처가 수백 MB 가 되고, 그러면 "런처는 거의 안 바뀐다"는
    # 이 구조의 전제가 무너진다.
    excludes=['PyQt6', 'PyQt5', 'pandas', 'numpy', 'requests', 'xlwings',
              'pyarrow', 'PIL', 'botocore', 'boto3', 'matplotlib', 'plotly',
              'win32com'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Honey',            # 설치 루트의 Honey.exe = 런처
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='honey.ico',
)

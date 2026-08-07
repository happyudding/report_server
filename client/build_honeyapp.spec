# PyInstaller spec — 사용: pyinstaller build_honeyapp.spec  → dist/HoneyApp/HoneyApp.exe
#
# 버전 폴더 방식에서 앱 본체는 HoneyApp.exe 다 (설치 루트의 Honey.exe 는 런처).
# build_honey.spec 을 **사본으로 두지 않는다** — 사본은 반드시 원본과 갈라진다.
# 원본 spec 을 텍스트로 읽어 산출물 이름 두 곳만 바꿔 그대로 실행한다.
#
# 기존 릴리스 파이프라인(build_zip.bat → build_honey.spec)은 이 파일과 무관하게 동작한다.

# -*- mode: python ; coding: utf-8 -*-

import os as _os

_src = _os.path.join(SPECPATH, 'build_honey.spec')
with open(_src, encoding='utf-8') as _fh:
    _code = _fh.read()

_needle = "name='Honey',"
_count = _code.count(_needle)
if _count != 2:
    raise SystemExit(
        f"build_honey.spec 의 \"{_needle}\" 가 2곳이 아니라 {_count}곳입니다 — "
        "원본 spec 이 바뀌었으니 build_honeyapp.spec 의 치환 규칙을 확인하세요.")


# 개발 PC 에는 외부 담당자 영역(honey_parse 실물)이 없어 원본 spec 의 datas 중
# 존재하지 않는 파일이 생긴다 (예: honey_parse/mddi/.../optional_sheets_dialog.ui).
# 그러면 Analysis 가 즉시 실패해 자동 업데이트 흐름 자체를 시험해 볼 수 없다.
# **테스트 빌드에 한해** 없는 파일은 건너뛰고, 무엇을 건너뛰었는지 크게 알린다.
# 실물이 있는 빌드 PC 에서는 아무것도 걸러지지 않는다(= 원본과 동일한 결과).
_RealAnalysis = Analysis


def Analysis(*args, **kwargs):   # noqa: N802 - PyInstaller 규약 이름
    datas = kwargs.get('datas')
    if datas:
        kept, dropped = [], []
        for entry in datas:
            src = entry[0] if isinstance(entry, (list, tuple)) else entry
            if isinstance(src, str) and not (
                    _os.path.exists(src) or _os.path.exists(_os.path.join(SPECPATH, src))):
                dropped.append(src)
            else:
                kept.append(entry)
        if dropped:
            print("=" * 78)
            print("[build_honeyapp.spec] 없는 데이터 파일을 건너뜁니다 (테스트 빌드 전용):")
            for src in dropped:
                print(f"    - {src}")
            print("  이 파일이 필요한 기능은 이 빌드본에서 동작하지 않습니다.")
            print("  운영 릴리스는 실물이 있는 빌드 PC 에서 build_zip.bat 으로 만드세요.")
            print("=" * 78)
        kwargs['datas'] = kept
    return _RealAnalysis(*args, **kwargs)


exec(compile(_code.replace(_needle, "name='HoneyApp',"), _src, 'exec'))

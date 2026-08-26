"""루트 Honey.exe(런처) 자가 교체 — 앱이 뜬 뒤에 조용히 한다.

**왜 필요한가**: 런처는 자동 업데이트 대상이 아니다. versions\\<ver>\\ 는 계속
갱신되지만 설치 루트의 Honey.exe 는 처음 압축을 푼 그대로 남는다. 그래서 런처를
고쳐도(예: 2026-08-26 WinError 5 수정) 각 PC 에 반영할 방법이 ZIP 재배포뿐이었다.

**왜 앱이 하는가**: 런처는 앱을 띄운 뒤 곧바로 return 한다(launcher.main). 즉 앱이
도는 동안 루트 Honey.exe 는 아무도 잡고 있지 않아 교체할 수 있다. 런처 자신은
실행 중인 자기 파일을 절대 못 바꾼다.

**소스**: 릴리스 payload 에 versions\\<ver>\\launcher\\Honey.exe 로 새 런처 사본을
동봉한다. 델타 업데이트로도 그대로 따라오므로 별도 다운로드가 없다.

실패는 전부 무시한다 — 교체는 다음 실행에 다시 시도하면 되고, 여기서 예외가 새면
앱 기동을 방해한다.
"""
import os
import shutil
from pathlib import Path

from . import app_update as au

LAUNCHER_SUBDIR = "launcher"      # versions\<ver>\launcher\Honey.exe


def bundled_launcher(root, version):
    """그 버전이 들고 온 새 런처 사본 (없으면 None — 구 릴리스라는 뜻)."""
    if not version:
        return None
    path = (Path(root) / au.VERSIONS_DIRNAME / version
            / LAUNCHER_SUBDIR / au.LAUNCHER_EXE_NAME)
    return path if path.is_file() else None


def maybe_replace_launcher(root, version=None):
    """루트 런처가 낡았으면 교체한다. 반환 True = 실제로 바꿨다.

    해시가 같으면 아무것도 하지 않는다(대부분의 실행이 여기서 끝난다).
    """
    root = Path(root)
    try:
        if version is None:
            version, _prev = au.read_current(root)
        source = bundled_launcher(root, version)
        if source is None:
            return False

        target = root / au.LAUNCHER_EXE_NAME
        if target.exists() and au.sha256_file(target) == au.sha256_file(source):
            return False

        # 같은 볼륨에 먼저 복사한 뒤 os.replace — 부분 복사본이 Honey.exe 자리에
        # 남는 일이 없어야 한다(그러면 그 PC 는 아무것도 실행하지 못한다).
        staged = root / f".{au.LAUNCHER_EXE_NAME}.new"
        shutil.copy2(source, staged)
        os.replace(staged, target)
        au.ulog(f"LAUNCHER 자가교체 완료 <- {version}")
        return True
    except OSError as exc:
        # 실행 중이거나(런처가 아직 안 죽음) 권한이 없다 — 다음 실행에 다시 한다.
        au.ulog(f"LAUNCHER 자가교체 보류(무시): {type(exc).__name__}: {exc}")
        try:
            (root / f".{au.LAUNCHER_EXE_NAME}.new").unlink(missing_ok=True)
        except OSError:
            pass
        return False
    except Exception as exc:   # noqa: BLE001 - 앱 기동을 막지 않는다
        au.ulog(f"LAUNCHER 자가교체 실패(무시): {type(exc).__name__}: {exc}")
        return False

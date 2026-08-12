"""릴리스 파일 매니페스트 생성 (build_test_release.ps1 이 호출한다).

산출물 2개 — 내용은 같다.
  1) <앱폴더>\\.files.json      zip 에 함께 들어가 **설치된 PC 의 로컬 캐시**가 된다.
     다음 업데이트 때 런처가 이걸 보고 "무엇이 바뀌었나"를 판단한다 (752MB 를 다시
     해싱하면 수십 초라, 이 캐시가 델타의 전제다).
  2) release\\Honey-<ver>.files.json   서버가 /honey/files/<ver> 로 내주는 것.

app_update.build_file_manifest 를 그대로 쓴다 — 런처가 델타를 계산할 때 쓰는 함수와
같아야 형식이 어긋나지 않는다. PowerShell 의 Get-FileHash 로 6,000개를 도는 것보다
훨씬 빠르기도 하다.

사용: python make_manifest.py <앱폴더> <버전> <출력 json>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # client/
from transport import app_update   # noqa: E402


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    app_dir, version, out_path = sys.argv[1], sys.argv[2], Path(sys.argv[3])

    files = app_update.build_file_manifest(app_dir)
    if not files:
        raise SystemExit(f"매니페스트가 비었습니다 — 앱 폴더를 확인하세요: {app_dir}")
    app_update.write_file_manifest(app_dir, files)
    out_path.write_text(json.dumps({"version": version, "files": files},
                                   ensure_ascii=False), encoding="utf-8")

    total = sum(int(f["s"]) for f in files)
    print(f"    파일 {len(files):,}개 / {total / 1024 / 1024:,.0f} MB -> {out_path.name}")


if __name__ == "__main__":
    main()

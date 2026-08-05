"""Temperature 모드 RT/CT/HT 자동 그룹 배치 — 순수 함수 (Qt 무의존).

PMIC 는 같은 웨이퍼를 RT(상온)/CT(저온)/HT(고온) 로 나눠 측정한다. 파일이 21개면 RT/CT/HT
쌍이 7그룹이 될 수도 있고, **그룹마다 자기 RT 가 Limit 판정 기준**이 된다. 여기 함수들은
"어느 source 가 어느 RT 와 짝인가" 를 파일명·폴더 역할에서 추정한다.

구 ``temperature_group_dialog.py`` (드래그앤드랍 배치 창)에서 **로직 변경 없이 옮겨온 것**이다.
그 창은 ``source_name_dialog.SourceNameDialog`` 의 Group/Role 열로 흡수되면서 폐지됐지만,
추정 규칙 자체는 그대로 쓴다. ``tests/test_temperature_pairing.py`` 9개 검사가 이 계약을
고정하고 있으니 규칙을 바꾸려면 그 테스트부터 본다.
"""
from __future__ import annotations

import re

ROLES = ("RT", "CT", "HT")
LIMIT_FILTER = "Limit Table (*.lt *.pds)"

# 파일명에서 온도 역할 토큰을 찾는다 (자동 그룹 제안 전용 — 못 찾으면 사용자가 직접 고른다).
_ROLE_TOKEN = re.compile(r"(?:^|[^A-Za-z0-9])(RT|CT|HT)(?:[^A-Za-z0-9]|$)", re.IGNORECASE)


def suggest_groups(names) -> list:
    """파일명 기반 자동 그룹 제안 → [{"RT": name, "CT": name, "HT": name}, ...].

    이름에서 RT/CT/HT 토큰을 떼어낸 나머지(stem)가 같은 것끼리 한 그룹으로 묶는다.
    토큰이 없거나 같은 stem·역할이 겹치면 그 source 는 제안에서 빠진다(미배정으로 남는다).
    """
    buckets: dict = {}
    order: list = []
    for name in names:
        m = _ROLE_TOKEN.search(str(name))
        if not m:
            continue
        role = m.group(1).upper()
        stem = (str(name)[:m.start(1)] + str(name)[m.end(1):]).strip(" _-.")
        if stem not in buckets:
            buckets[stem] = {}
            order.append(stem)
        if role not in buckets[stem]:
            buckets[stem][role] = name
    return [buckets[s] for s in order if "RT" in buckets[s]]


def dedupe_names(names) -> list:
    """중복 이름에 _2, _3 … 접미사를 붙여 유일하게 만든다.

    ``compare_arrange_dialog.dedupe_names`` 와 **같은 규칙**이다 — 첫 번째는 원래 이름
    그대로 두고 두 번째부터 접미사가 붙는다. 형제 모듈에서 import 하지 않고 여기에 두는
    이유: 이 규칙은 배포본마다 버전이 갈릴 수 있는 다른 다이얼로그에 의존하면 안 된다
    (구 사본에서 ImportError 로 업로드가 통째로 막혔던 사례). 규칙을 바꾼다면 거기도 같이 본다.
    """
    out, seen = [], {}
    for name in names:
        base = str(name)
        if base in seen:
            seen[base] += 1
            base = f"{base}_{seen[base]}"
        else:
            seen[base] = 1
        out.append(base)
    return out


def pair_key(name) -> str:
    """그룹(pair) 묶음 키 — 이름에서 RT/CT/HT 토큰을 떼고 소문자화·구분자 정리.

    suggest_groups 의 stem 계산과 같은 규칙이되 **대소문자를 무시**한다. 역할이 폴더로
    이미 확정된 경우(suggest_groups_by_role)에는 이 키가 같은 것끼리 한 웨이퍼 pair 다.
    """
    text = str(name)
    m = _ROLE_TOKEN.search(text)
    if m:
        text = text[:m.start(1)] + text[m.end(1):]
    return text.strip(" _-.").lower()


def suggest_groups_by_role(names, role_of) -> list:
    """역할이 **확정된** 상태에서 pair 를 묶는다 → [{"RT":…, "CT":…, "HT":…}, ...].

    role_of(name) → "RT"|"CT"|"HT"|"" (빈 값이면 파일명 토큰으로 폴백). 폴더 구조로 역할을
    알아낸 뒤 "어느 RT 와 짝인가" 만 정하는 경로이며 2단계로 짝짓는다:

      1. **이름 유사도** — pair_key(온도 토큰 제거) 가 같은 것끼리. 이름 자체에 온도가
         든 경우(``WF1_RT`` ↔ ``WF1_CT``)를 잡는다. RT + member 가 모두 있어야 확정.
      2. **역할별 순서** — 1에서 남은 것은 i 번째 RT ↔ i 번째 CT ↔ i 번째 HT 로 짝짓는다.
         폴더마다 같은 파일명을 쓰는 경우(``EP1/RT/a.stdf`` ↔ ``EP1/CT/a.stdf`` → source
         이름이 ``a`` / ``a_2`` / ``a_3`` 로 갈리는 경우)가 여기서 잡힌다. folder_intake 가
         역할마다 이름순으로 정렬해 주므로 같은 순번이 같은 웨이퍼다.

    2단계는 추정이라 틀릴 수 있다 — 사용자가 표에서 확인·수정하는 이유가 그것이다.
    짝이 남으면(RT 보다 CT 가 많은 등) 배치하지 않고 남겨 사용자가 직접 고르게 한다.
    """
    role_by_name: dict = {}
    for name in names:
        role = str(role_of(name) or "").upper() if role_of else ""
        if role not in ROLES:
            m = _ROLE_TOKEN.search(str(name))
            role = m.group(1).upper() if m else ""
        if role in ROLES:
            role_by_name[name] = role

    # 1단계 — 이름 stem 이 같은 것끼리
    buckets: dict = {}
    order: list = []
    for name, role in role_by_name.items():
        stem = pair_key(name)
        if stem not in buckets:
            buckets[stem] = {}
            order.append(stem)
        buckets[stem].setdefault(role, name)     # 같은 (stem, 역할) 중복은 첫 번째만

    groups, taken = [], set()
    for stem in order:
        bucket = buckets[stem]
        if "RT" in bucket and len(bucket) > 1:   # RT + member 최소 1개라야 pair 로 인정
            groups.append(bucket)
            taken.update(bucket.values())

    # 2단계 — 남은 것은 역할별 순번으로
    rest = {role: [n for n in names
                   if role_by_name.get(n) == role and n not in taken]
            for role in ROLES}
    for i, rt in enumerate(rest["RT"]):
        pair = {"RT": rt}
        for role in ("CT", "HT"):
            if i < len(rest[role]):
                pair[role] = rest[role][i]
        groups.append(pair)
    return groups


def parse_limit_files(paths):
    """.lt/.pds 파싱 → (merged, loaded, errors). **워커 스레드에서 도는 함수**.

    UI 에 접근하지 않고 예외를 값으로 돌려준다 — 큰 limit 파일을 UI 스레드에서 읽으면
    창이 통째로 얼어붙는다. 판정 규칙 자체는 ``web_report.temperature.load_limits_file``
    그대로다. 여러 파일을 고르면 매핑을 전부 병합하되, 세션 기록용 ``limits_file`` 에는
    첫 파일만 남긴다(구 배치 창과 동일).
    """
    from pathlib import Path

    from web_report.temperature import load_limits_file

    merged, loaded, errors = {}, [], []
    for path in paths:
        name = Path(path).name
        try:
            mapping, kind = load_limits_file(path)
        except Exception as exc:                      # noqa: BLE001
            errors.append((name, str(exc)))
            continue
        if not mapping:
            errors.append((name, "항목을 하나도 찾지 못했습니다."))
            continue
        merged.update(mapping)
        loaded.append((name, kind, len(mapping)))
    return merged, loaded, errors

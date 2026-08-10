"""CPK tab payload builder.

**모든 CPK 통계는 Bin1(양품, BIN==PASS_BIN) 기준 하나로 통일한다 (2026-07-23).**
종전에는 기준 3종(전체 die / Bin1 / 규격내)을 병기하고 CPK 탭 토글·Issue Table 이 각각
다른 기준을 골라 써서, 같은 항목의 CPK 가 탭마다 다른 값으로 보였다. 이제 base 필드
(``cpk``/``average``/``stdev``/…)가 곧 Bin1 기준이며 ``*_bin1``/``*_limited`` 병기는 없다 —
CPK 탭·Issue Table·Distribution status·Excel 내보내기가 모두 같은 값을 본다.

**유일한 예외: Temperature 모드의 CT/HT (2026-08-10).** 그 소스는 "**RT 에서 Bin1 이던**
die 를 **RT limit** 으로" 계산한다 — 자기 BIN 으로 거르지 않는다. CT/HT 프레임은 업로드
전에 이미 RT 의 BIN==1 좌표만 남겨져 있으므로(``temperature.clean_frames``) **모집단 =
그 프레임 전 행**이고, 남은 차이는 limit 뿐이다(정리 단계가 CT/HT 자신의 limit 메타행은
화면 표시용으로 보존한다). 자기 BIN 으로 거르면 "RT limit 재판정까지 통과한 die" 만 남아
저온/고온에서 규격을 벗어난 분포가 통계에서 빠져 CPK 가 실제보다 좋게 나온다.
"""
from __future__ import annotations

import pandas as pd

from .common import PASS_BIN, bin_types, json_safe, num, round_num

# 이슈 판단 공용 임계값 — Issue Table(CPK 섹션)·Distribution(status 분류)이 공유한다.
CPK_THRESHOLD = 1.33


def worst_cpk_by_subject(cpk_rows) -> dict:
    """subject 별 모든 source 행 중 최저(worst-case) cpk (None 제외).

    cpk 는 Bin1 기준 단일 값이다(위 모듈 docstring).
    dict 삽입 순서 = cpk_rows 에서 subject 가 처음 등장한 순서."""
    worst: dict = {}
    for r in cpk_rows or []:
        cpk = r.get("cpk")
        if cpk is None:
            continue
        subject = r.get("subject")
        if subject not in worst or cpk < worst[subject]:
            worst[subject] = cpk
    return worst


def _stats(series, lo, hi):
    s = pd.to_numeric(series, errors="coerce").dropna()
    n = int(len(s))
    avg = s.mean() if n else None
    stdev = s.std(ddof=1) if n > 1 else None
    lo_n = num(lo)
    hi_n = num(hi)
    can_base = (
        n > 1
        and stdev not in (None, 0)
        and num(stdev) is not None
    )
    cp = cpl = cpu = cpk = None
    if can_base:
        if lo_n is not None and hi_n is not None:
            cp = (hi_n - lo_n) / (6.0 * stdev)
            # 상·하한이 같으면(공차 0) cpl/cpu/cpk 는 의미가 없어 계산하지 않고 빈칸으로 둔다.
            if lo_n != hi_n:
                cpl = (avg - lo_n) / (3.0 * stdev)
                cpu = (hi_n - avg) / (3.0 * stdev)
                cpk = min(cpl, cpu)
        elif hi_n is not None:
            # USL(상한)만 있으면 CPU = CPK (cp 는 양측 규격폭 필요 → None 유지).
            cpu = (hi_n - avg) / (3.0 * stdev)
            cpk = cpu
        elif lo_n is not None:
            # LSL(하한)만 있으면 CPL = CPK.
            cpl = (avg - lo_n) / (3.0 * stdev)
            cpk = cpl
    return {
        "n": n,
        "min": round_num(s.min() if n else None),
        "median": round_num(s.median() if n else None),
        "max": round_num(s.max() if n else None),
        "average": round_num(avg, 4),
        # stdev 는 반올림하지 않는다 — _stats_batch(CPK 탭)와 규약을 통일한다. 종전엔 여기만
        # 3자리로 잘라 같은 항목의 σ 가 CPK 시트(무반올림)와 항목 상세(scatter)·Trim 에서
        # 다르게 보였다. scatter/trim/eval_export 가 이 함수를 공유하므로 이제 전부 동일 값.
        "stdev": num(stdev),
        "cp": round_num(cp, 3),
        "cpl": round_num(cpl, 3),
        "cpu": round_num(cpu, 3),
        "cpk": round_num(cpk, 3),
    }


def _stats_batch(frame: pd.DataFrame, lolim: dict, hilim: dict) -> dict:
    """_stats 와 동일한 결과를 컬럼 일괄 reduction 으로 계산 — item 별 Series 생성/축약
    수만 회(항목 2000×소스×통계 5종)를 프레임당 6회의 C 루프로 대체한다.

    입력 frame 의 item 컬럼은 split_honeyform 이 만든 numeric dtype 이어야 한다
    (호출자인 build_cpk_rows 가 보장). 반환: {item: _stats 와 동일한 dict}.
    """
    if frame.shape[1] == 0:
        return {}
    cnt = frame.count().to_dict()
    mean = frame.mean().to_dict()
    std = frame.std(ddof=1).to_dict()
    mn = frame.min().to_dict()
    mx = frame.max().to_dict()
    med = frame.median().to_dict()
    out = {}
    for item in frame.columns:
        n = int(cnt[item])
        avg = mean[item] if n else None
        stdev = std[item] if n > 1 else None
        lo_n = num(lolim.get(item))
        hi_n = num(hilim.get(item))
        can_base = (
            n > 1
            and stdev not in (None, 0)
            and num(stdev) is not None
        )
        cp = cpl = cpu = cpk = None
        if can_base:
            if lo_n is not None and hi_n is not None:
                cp = (hi_n - lo_n) / (6.0 * stdev)
                # 상·하한이 같으면(공차 0) cpl/cpu/cpk 는 의미가 없어 계산하지 않고 빈칸으로 둔다.
                if lo_n != hi_n:
                    cpl = (avg - lo_n) / (3.0 * stdev)
                    cpu = (hi_n - avg) / (3.0 * stdev)
                    cpk = min(cpl, cpu)
            elif hi_n is not None:
                # USL(상한)만 있으면 CPU = CPK (cp 는 양측 규격폭 필요 → None 유지).
                cpu = (hi_n - avg) / (3.0 * stdev)
                cpk = cpu
            elif lo_n is not None:
                # LSL(하한)만 있으면 CPL = CPK.
                cpl = (avg - lo_n) / (3.0 * stdev)
                cpk = cpl
        out[item] = {
            "n": n,
            "min": round_num(mn[item] if n else None),
            "median": round_num(med[item] if n else None),
            "max": round_num(mx[item] if n else None),
            "average": round_num(avg, 4),
            # stdev 는 반올림하지 않는다 — CPK 탭 Limit 역산(avg ± 3·Cpk·stdev)이 이 값을
            # 그대로 쓰므로 소수 3자리로 자르면 역산 한계값이 어긋난다.
            "stdev": num(stdev),
            "cp": round_num(cp, 3),
            "cpl": round_num(cpl, 3),
            "cpu": round_num(cpu, 3),
            "cpk": round_num(cpk, 3),
        }
    return out


def temperature_reference_tables(tables, temperature_groups) -> dict:
    """{CT/HT source 이름: 그 그룹의 RT table} — Temperature CPK 기준 (그 외 모드는 빈 dict).

    모듈 docstring 의 예외를 적용할 대상을 고르는 유일한 지점이다. 그룹에 속하지 않은
    source·RT 자신·tables 에 없는 이름은 담기지 않으므로, 호출부는 이 dict 에 있는지만
    보면 된다.
    """
    by_name = {t.source: t for t in tables}
    out = {}
    for group in temperature_groups or []:
        rt = by_name.get(str(group.get("rt") or ""))
        if rt is None:
            continue
        for name in group.get("members") or []:
            member = by_name.get(str(name))
            if member is not None and member is not rt:
                out[str(name)] = rt
    return out


def build_cpk_rows(tables, all_items, temperature_groups=None):
    """temperature_groups 를 주면 그 그룹의 CT/HT 만 RT 기준으로 계산한다(모듈 docstring).

    인자를 생략하면 종전과 완전히 동일한 전 소스 Bin1 기준이다 — Compare 의 pooled 계산과
    Honey 빠른 수정 미리보기가 그 경로를 쓴다.
    """
    rows = []
    per_table = []
    ref_of = temperature_reference_tables(tables, temperature_groups)
    for table in tables:
        item_set = set(table.item_columns)
        present = [i for i in all_items if i in item_set]
        frame = table.data[present]
        # split_honeyform 이 item 컬럼을 numeric dtype 으로 만들지만, object 로 남은
        # 컬럼이 있으면 기존 per-item pd.to_numeric 과 동일하게 변환해 둔다.
        stale = [c for c in present if frame[c].dtype.kind not in "if"]
        if stale:
            frame = frame.copy()
            for c in stale:
                frame[c] = pd.to_numeric(frame[c], errors="coerce")
        ref = ref_of.get(table.source)
        if ref is None:
            # 통계는 Bin1(BIN==PASS_BIN, 양품) die 만으로 낸 한 벌뿐이다 — 이 값이 곧 base
            # 필드이며 CPK 탭·Issue Table·Distribution·Excel 이 모두 같은 값을 쓴다.
            # BIN 마스크는 item 과 무관 — 테이블당 1회만 계산 (item 루프 안에서 재계산 금지)
            frame = frame[[b == PASS_BIN for b in bin_types(table)]]
            lolim, hilim = table.lolim, table.hilim
        else:
            # Temperature CT/HT — 행은 이미 RT Bin1 좌표만이라 그대로 쓰고 limit 만 RT 것으로
            # 바꾼다. 표시 limit(lower/upper_limit)도 같이 RT 것이어야 CPK 탭의 한계값
            # 역산(avg ± 3·Cpk·stdev)과 화면 규격이 어긋나지 않는다.
            lolim, hilim = ref.lolim, ref.hilim
        per_table.append((table, item_set, _stats_batch(frame, lolim, hilim), lolim, hilim))
    for item in all_items:
        for table, item_set, stats_bin1, lolim, hilim in per_table:
            if item not in item_set:
                continue
            rows.append({
                "subject": item,
                "source": table.source,
                "units": json_safe(table.units.get(item)) or "",
                "lower_limit": round_num(lolim.get(item)),
                "upper_limit": round_num(hilim.get(item)),
                **stats_bin1[item],
            })
    return rows


"""Distribution "DUT 별 분리" compact 빌더 (2026-09-03).

Distribution 갤러리·Item_detail 은 source 단위로만 시리즈를 가른다. 병렬 테스트에서
site(DUT)별 편차 — 특정 DUT 만 분포가 밀렸거나 꼬리가 뻗는 현상 — 는 그 화면으로는
보이지 않으므로, **각 source 를 DUT 값별 pseudo-source 로 쪼갠** 응답을 따로 낸다.

분할 규칙 자체는 만들지 않는다 — `honeyform.dut_labels` / `split_table_by_dut` 이
정본이고(`mode="DUT"` 세션이 이미 그걸로 분할된다) 이 모듈은 **일반 세션에서도 조회
시점에 같은 분할을 켤 수 있게** 감싸기만 한다(규칙 #13 — 같은 값은 한 곳에서 계산).

- source 명은 ``"<source> · DUT <label>"``. `split_table_by_dut` 의 ``"DUT <label>"`` 을
  그대로 쓸 수 없다 — 일반 세션은 source 가 N개라 WF1·WF2 의 DUT1 이 충돌한다.
- **다운샘플 없음**(불변 규칙 #5). 같은 die 를 나눠 담을 뿐 하나도 버리지 않는다.
- ECDF/Serial 순 본체 계산은 `tabs.distribution.build_distribution_compact` /
  `dist_seq.build_seq_compact` 를 **그대로 재사용**한다. 분할된 tables 를 넘길 뿐이라
  같은 항목이 탭마다 다른 기준으로 보이지 않는다.
- dist pack 지름길은 쓸 수 없다 — pack 은 업로드 시점에 `np.unique` 로 count 를 집약해
  DUT 축이 소실된 산출물이다(`dist_seq` 가 순서를 잃는 것과 같은 사정). 항상 tables 를
  읽는다(TABLES_CACHE 공유라 `/scatter` 와 같은 비용).

⚠️ **이 모듈을 `web_report/tabs/` 로 옮기지 말 것** — perf_guard S01 이 tabs 변경마다
`REPORT_SCHEMA_VERSION` bump 를 요구하고, 그 bump 는 전 세션 콜드 재빌드 폭풍이 된다
(`dist_seq.py` · `gap_chart.py` 와 같은 이유). 이 계산은 report payload 와 무관하다.
"""
from __future__ import annotations

# source 명 조립 규칙의 정본. 프런트 distribution.js `DIST_DUT_SEP` 와 **문자 그대로
# 같아야** 한다 (CLAUDE.md §5 규칙 15 — 서버/JS 이중 정의 상수는 짝으로 고친다).
DUT_SOURCE_SEP = " · DUT "


def dut_source_name(source: str, label: str) -> str:
    """base source + DUT 라벨 → 분할 source 명."""
    return f"{source}{DUT_SOURCE_SEP}{label}"


def split_tables_by_dut(tables) -> list:
    """전 source 를 각각 DUT 값별로 쪼갠 pseudo-source 리스트.

    `honeyform.split_table_by_dut` 을 source 마다 돌리고 **이름만** 재조립한다 — 분할
    규칙(라벨 정규화·정렬)은 그쪽 한 곳이 정본이다.

    라벨 매칭은 `dut_labels` 순서와 반환 순서가 1:1 이라는 `split_table_by_dut` 의 계약
    (그 함수가 ``uniq`` 를 순회해 만든다)에 기대어 zip 으로 한다 — 반환된 source 명
    ``"DUT 3"`` 을 문자열로 되파싱하면 라벨에 공백이 있을 때 깨진다.

    분할하지 않는 두 경우는 **원본 테이블을 이름 그대로** 통과시킨다:
    - ``DUT`` 컬럼이 없는 테이블 (7-meta 계약상 정상 세션엔 항상 있지만 파생 테이블 방어)
    - DUT 종류가 1개 이하 (분할이 무의미 — `split_table_by_dut` 이 ``[table]`` 을 반환)
    """
    from .honeyform import dut_labels, split_table_by_dut

    out = []
    for table in tables:
        data = getattr(table, "data", None)
        if data is None or "DUT" not in getattr(data, "columns", ()):
            out.append(table)
            continue
        parts = split_table_by_dut(table)
        if len(parts) <= 1:
            # DUT 1종 이하 — 원본 그대로(이름 불변). 화면은 분리 전과 완전히 같아진다.
            out.append(table)
            continue
        for label, part in zip(dut_labels(data), parts):
            part.source = dut_source_name(table.source, label)
            out.append(part)
    return out


def expand_bin1_sources(bin1_sources, tables):
    """bin1 대상 source 집합을 **분할 후 이름**으로 확장한다.

    `service._bin1_source_filter` 는 Temperature "Bin1(RT만)" 에서 RT source 명 집합
    (``{"WF1_RT"}``)을 준다. 그런데 분할 후 이름은 ``"WF1_RT · DUT 1"`` 이라
    ``table.source in bin1_sources`` 가 **전부 False** 가 되어 bin1 이 아무 소스에도
    안 걸린다 — 에러가 아니라 "필터가 조용히 풀린" 오답으로 나타난다.

    ``None``(전 소스 대상)은 그대로 ``None`` 을 돌려준다.
    """
    if bin1_sources is None:
        return None
    wanted = set(bin1_sources)
    out = set()
    for table in tables:
        name = table.source
        if name in wanted or name.split(DUT_SOURCE_SEP)[0] in wanted:
            out.add(name)
    return out


def compute_dut_compact(tables, selected_items, mode, *, only=None, bin1=False,
                        bin1_sources=None, seq=False) -> dict:
    """세션 tables → DUT 분리 compact dict (seq=True 면 Serial 순).

    항목 집합 산출은 `dist_seq.compute_seq_compact` / `dist_blob.compute_dist_compact` 와
    **같은 순서·같은 규칙**이다 (모드 변형 → selected_items → 무데이터 항목 제외 →
    ``only`` 화이트리스트). 갤러리가 분리 on/off 를 같은 카드 목록으로 오가므로 두 경로의
    항목 집합이 어긋나면 카드가 조용히 비어 보인다.

    ``mode == "DUT"`` 세션은 `mode_tables` 가 이미 DUT 로 쪼갠 뒤라 **다시 쪼개지 않는다**
    (각 조각이 DUT 1종이라 실제로는 무해하지만, 무해함에 기대는 코드는 나중에 깨진다).

    ``tables`` 의 item_columns 를 in-place 로 좁히므로 호출자는 소모성 tables(loader
    클론)를 넘길 것 — `compute_seq_compact` 와 같은 계약이다.
    """
    from .dist_seq import build_seq_compact
    from .tabs.common import empty_items
    from .tabs.distribution import build_distribution_compact
    from .validation import mode_tables, validate_mode

    mode = validate_mode(mode)
    tables = mode_tables(tables, mode)
    if mode != "DUT":
        tables = split_tables_by_dut(tables)
    selected = {str(v) for v in (selected_items or []) if str(v)}
    if selected:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected]
    excluded = empty_items(tables)
    all_items = sorted({c for t in tables for c in t.item_columns if c not in excluded})
    if only is not None:
        wanted = {str(v) for v in only if str(v)}
        all_items = [c for c in all_items if c in wanted]
    sources = expand_bin1_sources(bin1_sources, tables)
    if seq:
        return build_seq_compact(tables, all_items, bin1=bin1, bin1_sources=sources)
    return build_distribution_compact(tables, all_items, bin1_only=bin1,
                                      bin1_sources=sources)

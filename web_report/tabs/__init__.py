"""Tab builders for web_report + 탭 레지스트리 (Phase 7, 2026-07-11).

시트 구성의 단일 진실. **새 탭 추가 절차**:
1. tabs/ 에 빌더 모듈 작성 (tables/공용 컨텍스트 → rows)
2. 아래 TAB_REGISTRY 에 TabSpec 1줄 추가 (표시 순서 = 레지스트리 순서)
3. 프런트 탭 모듈 1개 추가 (server/report/static/webreport/ — report_view.html 참조)

metrics.build_report_payload 는 REGISTRY 를 순회할 뿐 개별 탭 이름을 모른다.
lazy 탭(builder=None)은 /full payload 에 빈 시트로 실리고 전용 라우트로 지연
로드된다 (Distribution / Trim Analysis 관례 — 대용량 payload 를 /full 에 싣지 않음).
Map Analysis 는 하이브리드: 경량 메타(strip_dies)는 /full 에 남기고 die 전량은
GET .../web_report/map_analysis 로 지연 로드한다 (schema v8).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .cpk import build_cpk_rows  # noqa: F401  (metrics 가 ctx 조립에 사용)
from .issue_table import build_issue_table_rows
from .Map_analysis import build_map_analysis_rows, strip_dies
from .raw_data import build_raw_data_rows
from .summary import build_summary_rows
from .yield_tab import fail_bin_ranking


@dataclass
class TabContext:
    """탭 빌더들이 공유하는 1회 계산 컨텍스트 (metrics 가 조립)."""
    tables: list
    all_items: list = field(default_factory=list)
    fail_counts: dict = field(default_factory=dict)
    yield_rows: list = field(default_factory=list)
    cpk_rows: list = field(default_factory=list)
    etc_items: list = field(default_factory=list)
    issue_comments: dict = field(default_factory=dict)
    product_type: str = ""
    product: str = ""
    mode: str = "Normal"                        # 분석 모드 — Map Analysis 의 DUT 병합 분기용
    # ai_comment 옵션 세션만 dict (row_key→텍스트, web_report/ai_comment.py) — None 이면
    # Issue Table 에 AI Comment 컬럼 자체가 생성되지 않는다.
    ai_comments: dict | None = None
    # Issue Table 행 숨김 키 목록 / 행 Status dict (세션 편집 DB — edits.py)
    issue_hidden: list = field(default_factory=list)
    issue_status: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TabSpec:
    name: str                                  # sheets 키 (프런트 탭 이름과 일치)
    builder: Optional[Callable] = None         # None = lazy (빈 시트 + 전용 라우트)


TAB_REGISTRY: tuple = (
    TabSpec("Summary", lambda ctx: build_summary_rows(ctx.tables)),
    TabSpec("Raw Data", lambda ctx: build_raw_data_rows(ctx.tables)),
    TabSpec("Yield", lambda ctx: ctx.yield_rows),
    TabSpec("CPK", lambda ctx: ctx.cpk_rows),
    TabSpec("Issue Table", lambda ctx: build_issue_table_rows(
        ctx.tables, ctx.yield_rows, ctx.cpk_rows,
        etc_items=ctx.etc_items, issue_comments=ctx.issue_comments,
        ai_comments=ctx.ai_comments,
        hidden_keys=ctx.issue_hidden, statuses=ctx.issue_status)),
    TabSpec("Distribution", None),      # lazy — GET .../web_report/distribution
    TabSpec("Trim Analysis", None),     # lazy — GET .../web_report/trim_analysis
    # 하이브리드 lazy — 경량 메타만 /full 에, dies 는 GET .../web_report/map_analysis
    TabSpec("Map Analysis", lambda ctx: strip_dies(build_map_analysis_rows(
        ctx.tables, ctx.product_type, ctx.product, ctx.mode))),
    TabSpec("Fail Bin", lambda ctx: fail_bin_ranking(ctx.yield_rows)),
)

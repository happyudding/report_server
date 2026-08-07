"""Issue comment 서식 토큰 — 웹 화면 전용 표시문자를 벗겨 평문으로 만든다.

문법 정본은 server/report/static/webreport/sheets.js 의 linkifyComment / cmtFmtClass 다.
이 모듈은 그 **strip 쪽 짝**이며 렌더는 하지 않는다::

    *[텍스트]   굵게
    *r[텍스트]  색만 (r=빨강 o=주황 g=초록 b=파랑)
    *R[텍스트]  색 + 굵게 (대문자)

굵기는 "글자 없음" 또는 "대문자"로만 표현하므로 ``b`` 는 bold 가 아니라 blue 다.
모르는 스타일 글자(``*x[..]``)는 토큰이 아니라 평문이므로 **그대로 둔다** — 기존 코멘트의
곱셈/각주 ``*`` 가 서식으로 오인되는 것을 막는 방어다.

``@[항목]``/``#[태그]``/``$[시트]`` 링크 토큰은 **건드리지 않는다** — 종전부터 Excel·eval 로
원문 그대로 나갔고 그 동작을 바꾸지 않는다.

순수 모듈(stdlib 만) — Honey 클라 client/excel_download/_sheets.py 가 import 한다.
"""
from __future__ import annotations

import re

# sheets.js CMT_FMT_COLORS 의 키와 같아야 한다 (tests/test_comment_format.py 가 대조).
_COLORS = frozenset("rogb")
# sheets.js stripCommentFormat 의 정규식과 문자 그대로 같아야 한다.
_TOKEN_RE = re.compile(r"\*([A-Za-z]?)\[([^\]]+)\]")


def _unwrap(m: "re.Match") -> str:
    style = m.group(1)
    if style == "" or style.lower() in _COLORS:
        return m.group(2)
    return m.group(0)          # 모르는 글자 = 토큰 아님 → 원문 유지


def strip_format(text) -> str:
    """서식 토큰을 본문만 남기고 벗긴다. None/비문자열은 ""/str() 로 정규화."""
    if text is None:
        return ""
    return _TOKEN_RE.sub(_unwrap, str(text))

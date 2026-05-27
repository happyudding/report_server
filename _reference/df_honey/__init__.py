"""df_honey package — 단일 파일 분석 wrapper 와 그룹 비교 wrapper 를 노출.

내부 구현은 :mod:`df_honey.df_honey` 에 있으며, 기존 사용처(`from df_honey import df_honey`)
호환을 위해 이름을 재노출한다.
"""

from .df_honey import df_honey, df_honey_group  # noqa: F401

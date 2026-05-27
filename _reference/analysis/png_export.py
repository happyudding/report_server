import json

import plotly.io as pio

from config import DATASETS_DIR


def render_subject_pngs(subjects, dataset_id="current", width=800, height=550, scale=2):
    """이미 빌드된 plotly payload을 PNG bytes로 변환해 in-memory dict로 반환.

    Args:
        subjects: 과목명 리스트. 일치하는 차트만 반환.
        dataset_id: output/datasets/<id> 의 id. 기본 "current".
        width, height, scale: 출력 해상도 (최종 픽셀 = width*scale × height*scale).

    Returns:
        {subject_name: png_bytes} 형태의 dict. 입력에 없는 과목은 스킵.

    사용 예:
        from analysis.png_export import render_subject_pngs
        pngs = render_subject_pngs(["수학", "영어"], dataset_id="current")

    openpyxl 삽입 예:
        from io import BytesIO
        from openpyxl.drawing.image import Image
        ws.add_image(Image(BytesIO(pngs["수학"])), "B5")

    필요 패키지: kaleido (`pip install -U kaleido`).
    """
    charts_dir = DATASETS_DIR / dataset_id / "charts"
    wanted = set(subjects)
    pngs = {}
    for path in charts_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["name"] in wanted:
            pngs[payload["name"]] = pio.to_image(
                {"data": payload["data"], "layout": payload["layout"]},
                format="png", width=width, height=height, scale=scale,
            )
    return pngs

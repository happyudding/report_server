"""숫자 강제 변환 등 라우트/업로드 공용 헬퍼. (report_ 모듈 네이밍 컨벤션 준수)"""


def to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def to_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None

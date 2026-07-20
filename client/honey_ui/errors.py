"""오류 표시 헬퍼 — 사용자 문장은 본문에, 기술적 원문은 "자세히 보기" 뒤에.

종전엔 슬롯마다 `QMessageBox.critical(self, "...", str(exc))` 로 파이썬 예외 문자열
(때로는 traceback 전문)을 그대로 띄웠다. 사용자는 읽을 수 없고, 정작 필요한 안내
문장은 스택 사이에 묻혔다. 여기서 두 계층으로 나눈다:

- 본문 = 사람이 읽는 문장 (짧게)
- Detailed text = 예외 원문/traceback (Qt 기본 "자세히 보기" 접기 뒤 — 문의 시 복사용)

report_flow 의 안내 ValueError 는 "사용자 안내문 + [Excel 처리 실패 원인] + traceback"
형태로 합쳐져 오므로, split_detail 이 그 마커로 두 계층을 되돌린다 (해당 파일들은
수정하지 않는다).
"""
from PyQt6.QtWidgets import QMessageBox

# report_flow(upload_prepare / report_xlsx_ingest)가 안내문 뒤에 붙이는 traceback 마커.
_DETAIL_MARKER = "[Excel 처리 실패 원인]"
_SUMMARY_MAX = 400


def split_detail(exc):
    """예외 → (본문, 자세히). 안내문에 traceback 이 붙어 있으면 마커에서 가른다."""
    text = str(exc)
    if _DETAIL_MARKER in text:
        summary, _, detail = text.partition(_DETAIL_MARKER)
        return summary.strip(), (_DETAIL_MARKER + detail).strip()
    if len(text) > _SUMMARY_MAX:
        # 마커 없는 장문(라이브러리 예외 등) — 앞부분만 본문, 전문은 자세히로.
        return text[:_SUMMARY_MAX].rstrip() + " …", text
    return text.strip(), ""


def show_error(parent, title, message, detail=""):
    """오류 알림 — message 는 본문, detail 은 "자세히 보기" 뒤에만 표시."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(message or "오류가 발생했습니다.")
    if detail:
        box.setDetailedText(detail)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def show_exc(parent, title, exc, prefix=""):
    """예외를 두 계층으로 나눠 표시 — prefix 는 본문 앞에 붙일 상황 설명(선택)."""
    summary, detail = split_detail(exc)
    message = f"{prefix}\n\n{summary}".strip() if prefix else summary
    show_error(parent, title, message, detail)

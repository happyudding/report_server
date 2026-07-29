"""DB Input 미리보기 — 선례 CSV 검증 결과를 보여주고 적재 여부를 묻는다.

QMessageBox.setDetailedText 대신 표를 쓰는 이유는 ChangeReviewDialog 와 같다:
import_csv 는 **모든** 불량 행을 모아 올리므로(부분 적재 금지), 2000행 CSV 의 unit 컬럼이
통째로 틀리면 에러가 2000줄이다. TableListView 는 검색·정렬·Ctrl+C·CSV 저장을 주므로
사용자가 원본 파일을 오프라인에서 고칠 수 있다.

두 상태를 한곳에서 처리한다:
  ok=False → 요약 + 에러 표 + [확인] 하나 (적재가 불가능하니 버튼을 주지 않는다) → False
  ok=True  → 요약 + 그룹 표 + [적재] / [취소]                                   → 선택 결과
"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .table_list_dialog import TableListView, fit_dialog_to_screen

# 단순 포맷은 lot/wafer/bin 없이 case 를 합성하므로 같은 조합이 한 건으로 접힌다
# (db_input/CLAUDE.md — 관리자 탭 CSV 왕복이 의도적으로 lossy 한 이유).
_COLLAPSE_NOTE = ("같은 (Product type, Family Product, Item) 은 하나의 선례로 접힙니다 — "
                  "이미 있으면 코멘트가 갱신됩니다.")


class _PreviewDialog(QDialog):
    def __init__(self, parent, title, summary, headers, rows, *, note="",
                 csv_name="db_input.csv", accept_text=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setSizeGripEnabled(True)
        self._csv_name = csv_name

        layout = QVBoxLayout(self)
        head = QLabel(summary, self)
        head.setWordWrap(True)
        head.setStyleSheet("font-size: 11pt; font-weight: 600; padding: 2px 0;")
        layout.addWidget(head)

        self.table = TableListView(headers, rows, self, note=note)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox()
        save_btn = QPushButton("CSV 저장…")
        buttons.addButton(save_btn, QDialogButtonBox.ButtonRole.ActionRole)
        save_btn.clicked.connect(self._save_csv)
        if accept_text:
            accept_btn = QPushButton(accept_text)
            cancel_btn = QPushButton("취소")
            buttons.addButton(accept_btn, QDialogButtonBox.ButtonRole.AcceptRole)
            buttons.addButton(cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
            accept_btn.clicked.connect(self.accept)
            cancel_btn.clicked.connect(self.reject)
            # Enter 오조작으로 적재되지 않게 취소를 기본 버튼으로 (ChangeReviewDialog 관례).
            cancel_btn.setDefault(True)
        else:
            ok_btn = QPushButton("확인")
            buttons.addButton(ok_btn, QDialogButtonBox.ButtonRole.RejectRole)
            ok_btn.clicked.connect(self.reject)
            ok_btn.setDefault(True)
        layout.addWidget(buttons)

        fit_dialog_to_screen(self, 820, 560)

    def _save_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "CSV 저장", self._csv_name,
                                              "CSV (*.csv)")
        if not path:
            return
        try:
            self.table.save_csv(path)
        except OSError as exc:
            QMessageBox.warning(self, "저장 실패", str(exc))


def ask_db_input_confirm(parent, result):
    """검증 결과를 보여주고 적재할지 묻는다.

    result: 서버 /api/eval/labels_import 응답
            {"ok","mode","format","rows","groups","errors","file_name"}.
    Returns: True 면 적재 진행. 오류 상태이거나 사용자가 취소하면 False.
    """
    name = result.get("file_name") or "CSV"
    errors = result.get("errors") or []
    if not result.get("ok"):
        rows = [[str(i), e] for i, e in enumerate(errors, start=1)] or [["1", "알 수 없는 오류"]]
        dlg = _PreviewDialog(
            parent, "DB Input — 검증 실패",
            f"{name} — 오류 {len(rows)}건 · 아무것도 적재되지 않습니다",
            ("#", "오류"), rows,
            note="CSV 를 고친 뒤 다시 시도해 주세요.",
            csv_name="db_input_errors.csv")
        dlg.exec()
        return False

    groups = result.get("groups") or []
    rows = [[g.get("product_type", ""), g.get("family_product", ""), str(g.get("rows", 0))]
            for g in groups]
    dlg = _PreviewDialog(
        parent, "DB Input — 적재 확인",
        f"{name} — 총 {int(result.get('rows') or 0):,}행 · 제품군 {len(groups)}개",
        ("Product type", "Family Product", "행 수"), rows,
        note=_COLLAPSE_NOTE, csv_name="db_input_preview.csv",
        accept_text="적재")
    return dlg.exec() == QDialog.DialogCode.Accepted

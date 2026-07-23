"""ChangeReviewDialog — Rawdata 반영 전 변경 내용 확인.

종전에는 QMessageBox.question 본문에 변경 요약을 통째로 넣었다. 수정이 많아지면
본문이 길어져 창이 화면 밖으로 커지고 [예]/[아니오] 버튼이 보이지 않았다(그래서
rawvalues.build_confirm_message 가 40줄에서 잘라야 했고, 잘린 내용은 볼 방법이 없었다).
그 다음 단계인 스크롤 QTextBrowser 도 셀 변경이 수백~수천 건이면 한 줄짜리 위치 문자열이
창 폭을 넘어 접히면서 수천 줄의 벽이 되어 사실상 읽을 수 없었다.

여기서는
  - 상단에 고정 요약 한 줄 (source/셀/경고/자동교정/시트삭제 건수)
  - 본문은 탭 2개 — [개요] 구조 변경·자동 교정·경고·시트 삭제(짧은 텍스트) /
    [셀 변경] 열이 분리된 표(검색·정렬·복사·CSV 저장, 수만 행 가능)
  - 하단에 고정 버튼 [반영] [취소] [전문 저장…], 기본 포커스는 취소
  - 창 크기를 사용 가능한 화면의 70% 로 상한 → 버튼이 화면 밖으로 나가지 않는다
"""
from __future__ import annotations

import html

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
)

from .table_list_dialog import TableListView, fit_dialog_to_screen

# 셀 변경 표의 열 — rawvalues.inspect_edited_frame 의 cell_rows 키 순서와 맞춘다.
_CELL_HEADERS = ("source", "SHOT", "DUT", "X", "Y", "BIN", "항목", "이전", "이후")
_CELL_KEYS = ("shot", "dut", "x", "y", "bin", "item", "old", "new")


def summary_line(totals: dict) -> str:
    """상단 고정 요약 — 무엇이 얼마나 바뀌는지 한 줄."""
    totals = totals or {}
    parts = []
    if totals.get("sources"):
        parts.append(f"source {totals['sources']}개")
    if totals.get("cells"):
        parts.append(f"셀 {totals['cells']:,}건 변경")
    if totals.get("fixes"):
        parts.append(f"자동 교정 {totals['fixes']}건")
    if totals.get("warnings"):
        parts.append(f"경고 {totals['warnings']}건")
    if totals.get("removed"):
        parts.append(f"시트 삭제 {totals['removed']}개")
    return " · ".join(parts) or "변경 내용"


def sections_to_text(payload: dict) -> str:
    """확인 섹션 → 저장/폴백용 평문 (build_confirm_message 와 같은 순서·문안)."""
    lines = []
    for sec in (payload or {}).get("sections") or []:
        lines.append(f"[{sec.get('name') or 'source'}]")
        for text in sec.get("structure") or []:
            lines.append(f"· {text}")
        for text in sec.get("fixes") or []:
            lines.append(f"· [자동 교정] {text}")
        cell_total = int(sec.get("cell_total") or 0)
        cells = sec.get("cells") or []
        if cell_total:
            lines.append(f"· 셀 {cell_total:,}개가 바뀌었습니다:")
            lines.extend(f"    - {c}" for c in cells)
            if cell_total > len(cells):
                lines.append(f"    - … 외 {cell_total - len(cells):,}건")
        elif sec.get("skipped_cell_diff"):
            lines.append("· 셀 단위 비교를 생략했습니다 (구조가 바뀌었거나 데이터가 큽니다).")
        for text in sec.get("warnings") or []:
            lines.append(f"· [경고] {text}")
        lines.append("")
    removed = (payload or {}).get("removed") or []
    if removed:
        lines.append(f"[시트 삭제 감지] {', '.join(removed)}")
        lines.append("해당 source 데이터가 리포트에서 제거되고 전체 탭이 재계산됩니다. "
                     "서버에서 되돌릴 수 없습니다.")
        lines.append("")
    return "\n".join(lines)


def _sections_to_html(payload: dict) -> str:
    """섹션 → HTML. 경고/삭제는 색으로 구분해 긴 목록에서도 눈에 띄게 한다."""
    def esc(text):
        return html.escape(str(text))

    out = []
    for sec in (payload or {}).get("sections") or []:
        out.append(f"<h3 style='margin:12px 0 4px'>{esc(sec.get('name') or 'source')}</h3><ul>")
        for text in sec.get("structure") or []:
            out.append(f"<li>{esc(text)}</li>")
        for text in sec.get("fixes") or []:
            out.append(f"<li><b>[자동 교정]</b> {esc(text)}</li>")
        cell_total = int(sec.get("cell_total") or 0)
        if cell_total:
            # 셀 목록 자체는 [셀 변경] 탭의 표가 맡는다 — 여기 나열하면 다시 수천 줄이 된다.
            out.append(f"<li>셀 <b>{cell_total:,}</b>개가 바뀌었습니다 "
                       "— <b>[셀 변경]</b> 탭에서 확인하세요.</li>")
        elif sec.get("skipped_cell_diff"):
            out.append("<li>셀 단위 비교를 생략했습니다 (구조가 바뀌었거나 데이터가 큽니다).</li>")
        for text in sec.get("warnings") or []:
            out.append(f"<li style='color:#b45309'><b>[경고]</b> {esc(text)}</li>")
        out.append("</ul>")

    removed = (payload or {}).get("removed") or []
    if removed:
        out.append("<h3 style='margin:12px 0 4px;color:#b91c1c'>시트 삭제 감지</h3>"
                   f"<p style='color:#b91c1c'>{esc(', '.join(removed))} — 해당 source 데이터가 "
                   "리포트에서 제거되고 전체 탭이 재계산됩니다. <b>서버에서 되돌릴 수 없습니다.</b></p>")
    return "".join(out) or "<p>변경 내용이 없습니다.</p>"


def cell_table_rows(payload: dict) -> tuple[list, int]:
    """전 source 의 cell_rows → 표 행(source 열을 앞에 붙임) + 전체 변경 건수.

    전체 건수가 행 수보다 크면 수집 상한(excel_session._CELL_DETAIL_LIMIT)에 걸린 것이다."""
    rows = []
    total = 0
    for sec in (payload or {}).get("sections") or []:
        name = sec.get("name") or "source"
        total += int(sec.get("cell_total") or 0)
        for cell in sec.get("cell_rows") or []:
            rows.append([name] + [cell.get(k, "") for k in _CELL_KEYS])
    return rows, total


class ChangeReviewDialog(QDialog):
    """반영 여부를 묻는 확인 다이얼로그. exec() == Accepted 면 반영."""

    def __init__(self, parent, payload: dict, title="Rawdata 수정 — 반영 확인"):
        super().__init__(parent)
        self._payload = payload or {}
        self.setWindowTitle(title)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)
        head = QLabel(summary_line(self._payload.get("totals")))
        head.setWordWrap(True)
        head.setStyleSheet("font-size: 12pt; font-weight: 600; padding: 2px 0;")
        layout.addWidget(head)

        body = QTextBrowser()
        body.setHtml(_sections_to_html(self._payload))
        body.setOpenExternalLinks(False)

        tabs = QTabWidget()
        tabs.addTab(body, "개요")
        # 셀 변경은 수천 건이 될 수 있어 표로만 다룬다. 0건이면 탭을 만들지 않는다.
        rows, cell_total = cell_table_rows(self._payload)
        self._cell_table = None
        if rows:
            hidden = cell_total - len(rows)
            note_text = (f"변경 {cell_total:,}건 중 {len(rows):,}건만 표시합니다 "
                         f"(… 외 {hidden:,}건은 수집 상한을 넘어 생략)." if hidden > 0 else "")
            self._cell_table = TableListView(_CELL_HEADERS, rows, note=note_text)
            tabs.addTab(self._cell_table, f"셀 변경 {cell_total:,}건")
            tabs.setCurrentWidget(self._cell_table)
        layout.addWidget(tabs, 1)

        note = QLabel("위 내용으로 서버에 반영할까요? (Excel 편집은 서버에서 되돌릴 수 없습니다)")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox()
        apply_btn = QPushButton("반영")
        cancel_btn = QPushButton("취소")
        save_btn = QPushButton("전문 저장…")
        buttons.addButton(apply_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.addButton(save_btn, QDialogButtonBox.ButtonRole.ActionRole)
        apply_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self._save_full_text)
        layout.addWidget(buttons)
        # 파괴적 반영의 기본값은 '안 함' — Enter 로 무심코 반영되지 않게 취소를 기본 버튼으로.
        cancel_btn.setDefault(True)
        cancel_btn.setFocus()

        fit_dialog_to_screen(self, 1040, 680)

    def _save_full_text(self):
        """전문 저장. 셀 변경 표가 있으면 CSV 도 고를 수 있다(표는 평문보다 건수가 많다)."""
        filters = "텍스트 (*.txt)"
        name = "rawdata_changes.txt"
        if self._cell_table is not None:
            filters = "CSV — 셀 변경 표 (*.csv);;텍스트 — 요약 전문 (*.txt)"
            name = "rawdata_changes.csv"
        path, _ = QFileDialog.getSaveFileName(self, "변경 내역 저장", name, filters)
        if not path:
            return
        try:
            if path.lower().endswith(".csv") and self._cell_table is not None:
                self._cell_table.save_csv(path)
                return
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(summary_line(self._payload.get("totals")) + "\n\n")
                fh.write(sections_to_text(self._payload))
        except OSError as exc:
            QMessageBox.warning(self, "저장 실패", str(exc))


def ask_change_review(parent, payload) -> bool:
    """확인 다이얼로그를 띄우고 승인 여부를 반환.

    payload 가 문자열이면(구 호출 규약) 평문을 그대로 보여준다 — 워커/테스트 호환용."""
    if isinstance(payload, str):
        payload = {"totals": {}, "sections": [], "removed": [], "plain": payload}
    dlg = ChangeReviewDialog(parent, payload)
    if payload.get("plain"):
        dlg.findChild(QTextBrowser).setPlainText(payload["plain"])
    return dlg.exec() == QDialog.DialogCode.Accepted

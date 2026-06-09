"""ENTRYPOINT / EXTERNAL_OWNER: D1 input provider boundary.

External D1 projects should branch or replace this package without touching
Honey UI/report generation code.  The default provider preserves the current
local ``d1_storage`` folder behavior for Honey.exe compatibility tests.
"""
import os
from pathlib import Path

_DEFAULT_D1_DIR = str(Path(__file__).resolve().parent.parent / "d1_storage")


class LocalD1Provider:
    """Default D1 provider backed by a local directory."""

    def __init__(self, storage_dir=None):
        self.storage_dir = Path(storage_dir or os.environ.get("HONEY_D1_STORAGE", _DEFAULT_D1_DIR))

    def ensure_ready(self):
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def list_files(self, query=""):
        """Return matching csv/xlsx files under the configured D1 directory."""
        q = (query or "").strip().lower()
        if not self.storage_dir.exists():
            return []
        files = [
            f for f in self.storage_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in (".csv", ".xlsx")
        ]
        out = []
        for path in sorted(files, key=lambda x: str(x).lower()):
            rel = str(path.relative_to(self.storage_dir))
            if q and q not in rel.lower():
                continue
            out.append(path)
        return out


def get_provider():
    """Return the active D1 provider.

    External branches can replace this function to return a server-backed
    provider while keeping the Honey UI contract unchanged.
    """
    return LocalD1Provider()


def list_files(query=""):
    """Convenience entrypoint used by tests and lightweight integrations."""
    provider = get_provider()
    provider.ensure_ready()
    return provider.list_files(query)


def D1BrowserDialog(parent=None, provider=None, ui_path=None):
    """Return a D1 file search/selection dialog using the active provider."""
    from PyQt5 import uic
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QDialog, QListWidgetItem

    class _D1BrowserDialog(QDialog):
        def __init__(self, parent=None, provider=None, ui_path=None):
            super().__init__(parent)
            base_dir = Path(__file__).resolve().parent
            uic.loadUi(str(ui_path or base_dir / "d1_browser.ui"), self)
            self.provider = provider or get_provider()
            self.provider.ensure_ready()
            self.lbl_path.setText(f"D1: {self.provider.storage_dir}")
            self.btn_refresh.clicked.connect(self._search)
            self.le_search.returnPressed.connect(self._search)
            self.list_files.itemDoubleClicked.connect(lambda _i: self.accept())
            self.buttonBox.accepted.connect(self.accept)
            self.buttonBox.rejected.connect(self.reject)
            self.le_search.setFocus()

        def _search(self):
            q = self.le_search.text().strip()
            self.list_files.clear()
            for path in self.provider.list_files(q):
                rel = str(path.relative_to(self.provider.storage_dir))
                it = QListWidgetItem(rel)
                it.setData(Qt.UserRole, str(path))
                self.list_files.addItem(it)
            if self.list_files.count() == 0:
                self.lbl_hint.setText(f"'{q}' 검색 결과가 없습니다.")
            else:
                self.lbl_hint.setText(
                    f"{self.list_files.count()}개 결과 - Ctrl/Shift 로 여러 파일 선택 가능")

        def selected_paths(self):
            return [it.data(Qt.UserRole) for it in self.list_files.selectedItems()]

    return _D1BrowserDialog(parent, provider=provider, ui_path=ui_path)

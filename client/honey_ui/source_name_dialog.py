r"""SourceNameDialog — Web Report 생성 직전 source 이름·순서(·Temperature 그룹) 확인 창.

표 한 줄이 source 하나다. 왼쪽은 그 source 를 만든 **대표 입력 파일**(읽기 전용, 툴팁에 전체
절대경로), 오른쪽이 리포트 legend 이름이다. ↑/↓ 로 바꾼 순서가 그대로 업로드 순서가 되고,
**최상단 source 의 limit(HiLIM/LoLIM)이 리포트 전체의 판정 기준**이 된다
(web_report/tabs/distribution.py 의 ``matched[0]``). 그래서 1행을 초록으로 강조한다.

**표에 보이는 위→아래가 곧 web_report 표시 순서다** (Yield/Distribution 의 source 컬럼 순서,
Temperature 의 그룹 순서·Temp Fail 컬럼 순서). Ctrl/Shift 로 여러 행을 골라 ↑/↓/↑↑/↓↓ 로
함께 옮길 수 있다 — Temperature 면 그룹 블록을 통째로 잡아 그룹 순서를 바꾸는 데 쓴다.

    ┌───────────────────────────────────────────────────────────────────┐
    │  # │ 입력 파일 (읽기 전용)                    │ Legend       │ 색 │  ↑
    │ 1★ │ …\lot_N4XA123\run03\602XX2_3_final.std  │ 602XX2_3     │ ██ │  ↓
    └───────────────────────────────────────────────────────────────────┘

**색 열은 모든 모드에 있다.** 기본값은 옵션(F10) 팔레트이고, 여기서 바꾼 색이 그 리포트의
최종 색이 된다(팔레트보다 우선). 색은 이름이 아니라 **표시 순서 i** 에 붙으므로 행을
옮기면 색도 그 자리에 남는다 — 서버의 ``dist_colors[i]`` 규약과 같다.

Temperature 모드(PMIC·SECURITY 전용)에서는 **열 2개(Group·Role)와 Limit 파일 영역이 더 생긴다** —
구 ``TemperatureGroupDialog``(드래그앤드랍 배치 창)를 이 창이 흡수했다. 그 외 모드에서 이
부분들은 비활성이 아니라 **아예 만들지 않는다**(열은 columnCount 에서 빠지고, Limit 영역은
컨테이너째 숨겨 레이아웃이 높이를 회수한다).

DUT 모드는 **색 전용**이다 — 행이 입력 파일이 아니라 서버가 만들 DUT pseudo-source
(``DUT 1``, ``DUT 2`` …)라 이름·순서를 클라가 정할 수 없다. 그래서 Legend 는 읽기 전용,
↑/↓ 는 만들지 않고, 파일 열은 숨긴다.

순서를 바꾸는 경로가 ``_shift() → _render()`` 하나뿐이라, 최상단 강조·그룹 구분선·색 스와치
갱신을 ``_render()`` 안에서만 하면 상태가 어긋날 수 없다. ``self._rows`` 가 유일한 진실이고
표는 그것을 그린 뷰다.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QItemSelection, QItemSelectionModel, Qt
from PyQt6.QtGui import (QBrush, QColor, QFontDatabase, QFontMetrics, QGuiApplication,
                         QKeySequence, QShortcut)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from honey_ui.source_naming import apply_role_suffix
from honey_ui.temperature_pairing import (ROLES, LIMIT_FILTER, dedupe_names, parse_limit_files,
                                          suggest_groups, suggest_groups_by_role)

_PATH_CHARS = 70              # 파일명 열 폭 (고정폭 글꼴 기준 = 문자 수 그대로)
_LEGEND_CHARS = 12            # Legend 열 폭·입력 제한 (기본)
_LEGEND_CHARS_TEMP = 15       # Temperature 는 _RT/_CT/_HT 접미사 3자를 더한다
_MAX_VISIBLE_ROWS = 21        # 이만큼은 스크롤 없이 보인다 (그 이상은 세로 스크롤바)
_TOP_BG = "#DCFCE7"           # 최상단(limit 기준) 행 강조
_RT_BG = "#FEF3C7"            # RT = 그룹의 limit 기준
_GROUP_BAND = "#F1F5F9"       # 짝수 그룹 옅은 띠 (그룹 경계 시각화)
_ROLE_ITEMS = ("", ) + ROLES
_NO_GROUP = "(미지정)"
_DROP_HINT = ".lt / .pds 파일을 여기에 끌어다 놓으세요"   # 파일을 넣으면 이 자리에 파일명이 들어간다
_DROP_HINT_STYLE = "color:#1e40af;"
_DROP_FILE_STYLE = "color:#166534; font-weight:600;"


# ── 순수 함수 (Qt 무의존 — QApplication 없이 단독 검증 가능) ──────────────────
def load_palette() -> list:
    """옵션(F10)에서 지정한 팔레트를 읽는다. 실패하면 기본 48색.

    색을 고르는 창이 둘(이 창 + CompareArrangeDialog)이라 **기본값을 읽는 곳은 하나**로
    둔다 — 한쪽만 옵션을 안 보면 같은 순번의 source 가 창마다 다른 색으로 보인다.
    """
    try:
        import chart_colors
        return chart_colors.load_colors()
    except Exception:                                      # noqa: BLE001
        return ["#3366CC"] * 48


def shorten_path(path, limit: int = _PATH_CHARS) -> str:
    """절대경로를 limit 자 안으로 줄인다 — **뒤에서 폴더 2개 + 파일명**, 앞은 `…` 생략.

    파일명이 가장 중요하고 그 다음이 바로 위 폴더(lot/run)라 잘라내는 건 항상 앞쪽이다.
    전체 원문은 툴팁이 책임진다.
    """
    text = str(path or "").strip()
    if not text or len(text) <= limit:
        return text
    sep = "\\" if "\\" in text else "/"
    parts = [p for p in Path(text).parts if p not in ("/", "\\")]
    name = parts[-1] if parts else text
    if len(name) + 2 > limit:
        # 파일명 자체가 한계를 넘는다 — 확장자를 지키며 스템 가운데를 자른다.
        ext = Path(name).suffix
        stem = name[:len(name) - len(ext)]
        keep = max(4, limit - len(ext) - 4)
        head = max(2, keep - keep // 3)
        return f"…{sep}{stem[:head]}…{stem[-(keep - head):]}{ext}"
    for depth in (2, 1):
        if len(parts) <= depth:
            continue
        cand = "…" + sep + sep.join(parts[-(depth + 1):])
        if len(cand) <= limit:
            return cand
    return f"…{sep}{name}"


def source_display_path(md, fallback: str = "") -> str:
    """이 source 를 만든 **대표 입력 파일 1개**의 절대경로.

    MDDI 처럼 입력 n개가 1 source 로 병합되면 화면에는 "파싱에 쓴 대표 파일" 하나만 보여야
    한다. 지금 저장소는 파일 1개당 ``df_honey.from_csv`` 1회라 ``report_meta.source_path``
    가 이미 대표 파일이지만, 병합 진입점(from_ddi_paths*)이 이식되면 report_meta 에 대표
    경로 필드가 생길 수 있다. 그때 이 우선순위 목록만 사실이 되면 되도록 **조회를 여기 한
    곳에 모은다** — 외부 담당자가 필드를 채우면 UI 코드는 손대지 않는다.

    report_generator/honey_parse 는 동결 영역이라 getattr 안전 조회만 한다.
    """
    rm = getattr(md, "report_meta", None)
    for attr in ("primary_path", "representative_path", "source_path"):
        try:
            value = getattr(rm, attr, "") or ""
        except Exception:                                  # noqa: BLE001
            value = ""
        if value:
            return str(value)
    for attr in ("source_paths", "input_paths"):
        try:
            seq = getattr(rm, attr, None) or getattr(md, attr, None)
        except Exception:                                  # noqa: BLE001
            seq = None
        if seq:
            try:
                first = list(seq)[0]
                return str(getattr(first, "path", first))
            except Exception:                              # noqa: BLE001
                pass
    return fallback


# ── Input File Information (세션 상세 ℹ 모달용 manifest 메타) ────────────────────
#
# 아래 3함수는 ``source_display_path`` 와 같은 철학이다 — **조회를 여기 한 곳에 모아**
# 외부 담당자(honey_parse/report_generator)가 필드를 채우면 UI/업로드 코드를 손대지 않고
# 값이 흐르게 한다. 동결 영역이라 전부 getattr 안전 조회이고, 없으면 키를 만들지 않는다
# (빈 문자열을 넣으면 서버가 "값이 있는데 비었다"와 구분하지 못한다).

#: STDF 헤더 메타 정규 키 → 파서가 쓸 법한 속성/딕트 키 후보.
#: **이 표가 외부 담당자 요청 스펙의 정본**이다 → docs/21_input_file_info.md
_STDF_FIELDS = {
    "lot_id":       ("lot_id", "lotid", "LOT_ID"),
    "sublot_id":    ("sublot_id", "sblot_id", "SBLOT_ID"),
    "wafer_id":     ("wafer_id", "waferid", "wafer_no", "WAFER_ID"),
    "part_type":    ("part_type", "part_typ", "PART_TYP"),
    "job_name":     ("job_name", "job_nam", "JOB_NAM"),
    "node_name":    ("node_name", "node_nam", "NODE_NAM"),
    "tester_type":  ("tester_type", "tstr_typ", "TSTR_TYP"),
    "oper_name":    ("oper_name", "oper_nam", "OPER_NAM"),
    "setup_time":   ("setup_time", "setup_t", "SETUP_T"),
    "start_time":   ("start_time", "start_t", "START_T"),
    "finish_time":  ("finish_time", "finish_t", "FINISH_T"),
    "test_time_sec": ("test_time_sec", "test_time", "elapsed_sec"),
    "part_count":   ("part_count", "part_cnt", "PART_CNT"),
    "good_count":   ("good_count", "good_cnt", "GOOD_CNT"),
}
#: epoch 초로 와도 되는 키 — 사람이 읽을 ISO8601 로 정규화한다.
_STDF_TIME_KEYS = ("setup_time", "start_time", "finish_time")


def _iso_time(value):
    """epoch 초(int/float) 또는 이미 문자열인 시각 → 'YYYY-MM-DD HH:MM:SS'. 실패 시 원문."""
    import datetime

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return str(value)
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value).strip()


def source_input_paths(md) -> list:
    """이 source 를 만든 **입력 파일 전부**의 절대경로. 병합 정보가 없으면 대표 1개.

    MDDI 처럼 입력 n개가 1 source 로 병합되는 경우를 위해 목록을 먼저 본다. 지금 저장소는
    파일 1개당 파싱 1회라 대부분 길이 1 이다.
    """
    rm = getattr(md, "report_meta", None)
    for attr in ("source_paths", "input_paths", "file_paths"):
        try:
            seq = getattr(rm, attr, None) or getattr(md, attr, None)
        except Exception:                                  # noqa: BLE001
            seq = None
        if not seq:
            continue
        try:
            paths = [str(getattr(item, "path", item)).strip() for item in seq]
        except Exception:                                  # noqa: BLE001
            continue
        paths = [p for p in paths if p]
        if paths:
            return paths
    primary = source_display_path(md, "")
    return [primary] if primary else []


def source_stdf_meta(md) -> dict:
    """STDF 헤더 메타(LotID·Wafer No·Test 시각 등). **현재 파서는 주지 않아 보통 빈 dict**.

    외부 담당자가 ``report_meta.stdf``(dict) 또는 위 ``_STDF_FIELDS`` 후보 이름의 속성으로
    채워 주면 클라 코드 수정 없이 그대로 업로드된다 — 요청 스펙이 곧 이 표다.
    """
    rm = getattr(md, "report_meta", None)
    bag = {}
    for holder in (rm, md):
        for attr in ("stdf", "stdf_meta", "header_meta"):
            try:
                value = getattr(holder, attr, None)
            except Exception:                              # noqa: BLE001
                value = None
            if isinstance(value, dict):
                bag = value
                break
        if bag:
            break

    out = {}
    for key, candidates in _STDF_FIELDS.items():
        value = None
        for cand in candidates:
            if isinstance(bag, dict) and bag.get(cand) not in (None, ""):
                value = bag[cand]
                break
            try:
                got = getattr(rm, cand, None)
            except Exception:                              # noqa: BLE001
                got = None
            if got not in (None, ""):
                value = got
                break
        if value in (None, ""):
            continue
        out[key] = _iso_time(value) if key in _STDF_TIME_KEYS else value
    return out


def source_file_info(md) -> dict:
    """manifest ``sources[]`` 에 실을 입력 파일 정보. 알 수 없는 항목은 키를 만들지 않는다.

    ``file_path``(대표 파일 절대경로) · ``file_size``/``file_created``/``file_modified``
    (실제 파일 stat) · ``input_files``(병합 입력 목록, 2개 이상일 때만) · ``stdf``.
    파일이 이미 지워졌거나 네트워크 경로가 끊겨도 업로드를 막으면 안 되므로 전부 best-effort.
    """
    import os

    paths = source_input_paths(md)
    info = {}
    if paths:
        info["file_path"] = paths[0]
    if len(paths) > 1:
        info["input_files"] = paths
    if paths:
        try:
            st = os.stat(paths[0])
            info["file_size"] = int(st.st_size)
            # Windows 의 st_ctime 은 '생성 시각' 이다(POSIX 의 inode 변경 시각이 아니라).
            info["file_created"] = _iso_time(st.st_ctime)
            info["file_modified"] = _iso_time(st.st_mtime)
        except Exception:                                  # noqa: BLE001
            pass
    stdf = source_stdf_meta(md)
    if stdf:
        info["stdf"] = stdf
    return info


def _fit_to_screen(dialog, width, height, ratio: float = 0.92) -> None:
    """화면 밖으로 나가지 않게 클램프. 최대치는 사용 가능 영역 전체, 초기 크기는 그 ratio.

    ``table_list_dialog.fit_dialog_to_screen`` 은 0.7 비율 하드코딩이라 "21행이 한눈에" 와
    충돌하고, 공용 헬퍼를 고치면 TableListDialog/ChangeReviewDialog 에 영향이 간다.
    """
    screen = dialog.screen() or QGuiApplication.primaryScreen()
    avail = screen.availableGeometry() if screen else None
    if avail is None:
        dialog.resize(width, height)
        return
    dialog.setMaximumSize(avail.width(), avail.height())
    dialog.resize(min(width, int(avail.width() * ratio)),
                  min(height, int(avail.height() * ratio)))


@dataclass
class _Row:
    index: int          # 원본 source 순번 — rename_sources 가 원본 순서 기준이라 필수
    path: str           # 대표 입력 파일 절대경로 (툴팁 원문)
    legend: str
    group: int = 0      # Temperature 전용, 1-based (0 = 미지정)
    role: str = ""      # Temperature 전용


class _MaxLenDelegate(QStyledItemDelegate):
    """Legend 셀 편집기에 최대 길이를 건다 (QTableWidgetItem 자체엔 maxLength 가 없다)."""

    def __init__(self, max_len, parent=None):
        super().__init__(parent)
        self._max = int(max_len)

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        try:
            editor.setMaxLength(self._max)
        except Exception:                                  # noqa: BLE001
            pass
        return editor


class _LimitsDropArea(QFrame):
    """.lt / .pds 파일을 끌어다 놓는 영역 (버튼으로도 고를 수 있다).

    회색 바탕(구 ``#f8fafc``)은 창 배경과 구분이 안 돼 잘 안 보인다는 지적(2026-08-06)으로
    파란 바탕으로 강조한다. 끌어오는 중에는 더 진한 파랑으로 바꿔 "여기에 놓으면 된다"를
    눈에 보이게 한다.

    선택자를 ``QFrame`` 이 아니라 **objectName(#limitsDrop)** 으로 잡는 이유: QLabel 도
    QFrame 하위라 ``QFrame {...}`` 은 안에 든 라벨까지 물들여(점선 테두리가 라벨에도 생김)
    영역 경계가 흐려진다.
    """

    _BASE = ("#limitsDrop { border: 2px dashed #3b82f6; border-radius: 6px;"
             " background: #e8f1fe; }")
    _HOVER = ("#limitsDrop { border: 2px solid #2563eb; border-radius: 6px;"
              " background: #cfe2fd; }")

    def __init__(self, on_files):
        super().__init__()
        self._on_files = on_files
        self.setAcceptDrops(True)
        self.setObjectName("limitsDrop")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(self._BASE)
        self.setMinimumHeight(52)

    def _paths(self, mime):
        return [u.toLocalFile() for u in mime.urls()
                if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in (".lt", ".pds")]

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and self._paths(event.mimeData()):
            self.setStyleSheet(self._HOVER)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self.setStyleSheet(self._BASE)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self.setStyleSheet(self._BASE)
        paths = self._paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self._on_files(paths[:1])          # limit 파일은 1개만 받는다
        else:
            event.ignore()


class SourceNameDialog(QDialog):
    """exec() 가 참을 돌려주면 result_arrangement() 로 결과를 읽는다.

    entries: ``[(legend, 대표 입력 파일 절대경로), ...]`` — **원본 source 순서**.
             (DUT 모드는 ``DUT <값>`` pseudo-source 목록 — 경로는 원본 파일 하나로 같다.)
    roles  : ``{원본 legend: "RT"|"CT"|"HT"}`` (Temperature 자동 배치용, 없으면 파일명 추정)
    pair_keys: ``{원본 legend: 짝 키}`` (Temperature 자동 배치용, 2026-08-24) — 입력
             파일명에서 다시 계산한 LOT_WF base(소문자). 같은 웨이퍼의 RT/CT 파일이
             dedupe(_2)로 legend 가 갈려도 이 키가 같으면 자동 배치가 같은 그룹으로
             묶는다. 없으면 종전(legend stem·순번) 추정 그대로.
    colors : 48색 팔레트 (없으면 옵션 팔레트를 읽는다)

    df_honey / df_honey_group 을 받지 않는다 — 그래야 QApplication 만으로 단독 실행해
    검증할 수 있고, 동결 영역 타입에 UI 가 결합되지 않는다.
    """

    def __init__(self, parent, entries, mode="Normal", roles=None, colors=None,
                 pair_keys=None):
        super().__init__(parent)
        self._mode = str(mode or "Normal")
        self._is_temp = (self._mode == "Temperature")
        self._is_dut = (self._mode == "DUT")
        self._roles = {str(k): str(v).upper() for k, v in (roles or {}).items()}
        self._pair_keys = {str(k): str(v) for k, v in (pair_keys or {}).items() if v}
        # Temperature 는 창 가로를 15% 정도 더 쓰고, 늘어난 폭 전부를 입력 파일 열에 준다
        # (2026-08-24 요청 — 파일명이 길어 상위 폴더 하나가 elide 로 잘려 보였다).
        self._path_chars = _PATH_CHARS + 22 if self._is_temp else _PATH_CHARS
        self._original = [str(name) for name, _ in entries]
        self._paths = [str(path or "") for _, path in entries]
        self._bin_map = None
        self._limits_file = None
        self._colors = list(colors) if colors else load_palette()
        self._colors_changed = False
        self._rendering = False

        self.setWindowTitle("Temperature — Source 이름 / 그룹 배치" if self._is_temp
                            else "DUT — Source 색 지정" if self._is_dut
                            else "Source 이름 / 순서 / 색")
        self._legend_max = _LEGEND_CHARS_TEMP if self._is_temp else _LEGEND_CHARS

        self._rows: list[_Row] = []
        self._reset_rows()

        # 글꼴·행 높이는 폭 계산과 렌더 양쪽에서 여러 번 쓰므로 한 번만 만든다.
        self._mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self._mono.setPointSize(self.font().pointSize())
        base_h = max(QFontMetrics(self._mono).height(), QFontMetrics(self.font()).height())
        self._row_h = max(base_h + 10, 30 if self._is_temp else 24)

        self._build_ui()
        if self._is_temp:
            self._auto_arrange(silent=True)
        else:
            self._render()
        self._apply_size()

    # ── 구성 ────────────────────────────────────────────────────────────────
    def _reset_rows(self):
        """행을 원본 상태로 되돌린다 (이름·순서·그룹·역할·그룹 이름 전부)."""
        self._rows = [_Row(index=i, path=self._paths[i], legend=self._original[i])
                      for i in range(len(self._original))]
        self._group_names: dict[int, str] = {}   # 이름을 바꾼 그룹만 (기본 표시는 숫자)

    def _columns(self):
        """열 구성 — 색은 **항상 마지막**이라 위치를 self._color_col 로 기억해 둔다."""
        cols = ["입력 파일",
                "Source (DUT 분할)" if self._is_dut
                else f"Legend (최대 {self._legend_max}자)"]
        if self._is_temp:
            cols += ["Group", "Role"]
        cols.append("색")
        self._color_col = len(cols) - 1
        return cols

    def _build_ui(self):
        cols = self._columns()
        self.table = QTableWidget(0, len(cols), self)
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setWordWrap(False)
        self.table.setItemDelegateForColumn(1, _MaxLenDelegate(self._legend_max, self))
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        header = self.table.horizontalHeader()
        # 마지막 열이 색(고정폭)이 됐으므로 stretch 는 Legend 열이 받는다.
        header.setStretchLastSection(False)
        if not self._is_temp:
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        if self._is_dut:
            self.table.setColumnHidden(0, True)   # DUT 분할은 전부 같은 원본 파일이다

        middle = QHBoxLayout()
        middle.addWidget(self.table, 1)
        if not self._is_dut:                      # DUT 순서는 서버가 정한다 (수치 오름차순)
            btn_top, btn_up = QPushButton("↑↑"), QPushButton("↑")
            btn_down, btn_bottom = QPushButton("↓"), QPushButton("↓↓")
            btn_top.setToolTip("선택 행을 최상단으로 (Alt+Home) — 최상단이 Limit 기준입니다")
            btn_up.setToolTip("선택 행을 위로 (Alt+↑) — Ctrl/Shift 로 여러 행을 함께 옮깁니다")
            btn_down.setToolTip("선택 행을 아래로 (Alt+↓) — Ctrl/Shift 로 여러 행을 함께 옮깁니다")
            btn_bottom.setToolTip("선택 행을 최하단으로 (Alt+End)")
            btn_top.clicked.connect(lambda: self._move_edge(True))
            btn_up.clicked.connect(lambda: self._shift(-1))
            btn_down.clicked.connect(lambda: self._shift(1))
            btn_bottom.clicked.connect(lambda: self._move_edge(False))
            for b in (btn_top, btn_up, btn_down, btn_bottom):
                b.setFixedWidth(36)
            for keys, slot in (("Alt+Up", lambda: self._shift(-1)),
                               ("Alt+Down", lambda: self._shift(1)),
                               ("Alt+Home", lambda: self._move_edge(True)),
                               ("Alt+End", lambda: self._move_edge(False))):
                QShortcut(QKeySequence(keys), self).activated.connect(slot)
            side = QVBoxLayout()
            side.addStretch(1)
            for b in (btn_top, btn_up, btn_down, btn_bottom):
                side.addWidget(b)
            side.addStretch(1)
            middle.addLayout(side)

        root = QVBoxLayout(self)
        root.addWidget(self._build_toolbar())
        root.addLayout(middle, 1)
        if self._is_temp:
            root.addWidget(self._build_limits_box())
        root.addWidget(self._build_hint())

        buttons = QDialogButtonBox()
        buttons.addButton("OK", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("취소", QDialogButtonBox.ButtonRole.RejectRole)
        btn_restore = buttons.addButton("원래대로", QDialogButtonBox.ButtonRole.ResetRole)
        btn_restore.setToolTip("이름·순서·그룹을 처음 상태로 되돌립니다")
        btn_restore.clicked.connect(self._restore)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.setSizeGripEnabled(True)
        # Enter 로 창이 닫히지 않게 한다 (2026-08-11 요청) — Group/Legend 를 타이핑하다 Enter 를
        # 치면 "입력 확정"으로 읽히는데, QDialog 는 default 버튼(OK)을 눌러 다음 화면으로
        # 넘어가 버린다. QPushButton 은 QDialog 안에서 autoDefault 가 기본 참이라 버튼마다
        # 꺼야 하고, 하나라도 남으면 그 버튼이 default 를 물려받는다.
        for btn in self.findChildren(QPushButton):
            btn.setAutoDefault(False)
            btn.setDefault(False)

    def _build_toolbar(self):
        """팔레트 편집은 모든 모드 공통, 자동 배치·그룹 초기화는 Temperature 전용."""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        if self._is_temp:
            btn_auto = QPushButton("파일명으로 자동 배치")
            btn_auto.setToolTip("폴더 역할과 이름 유사도로 그룹을 다시 제안합니다.")
            btn_auto.clicked.connect(lambda: self._auto_arrange())
            btn_clear = QPushButton("그룹 초기화")
            btn_clear.setToolTip("그룹·역할 지정만 지웁니다 (이름·순서는 유지).")
            btn_clear.clicked.connect(self._clear_groups)
            for b in (btn_auto, btn_clear):
                lay.addWidget(b)
        btn_palette = QPushButton("전체 팔레트 편집…")
        btn_palette.setToolTip("48색 팔레트를 편집해 옵션(F10)에 저장합니다.")
        btn_palette.clicked.connect(self._edit_palette)
        lay.addStretch(1)
        lay.addWidget(btn_palette)
        return box

    def _build_limits_box(self):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        # 제목도 파랑·굵게 — 드롭 영역과 한 덩어리로 읽히게 한다(2026-08-06 사용자 요청).
        title = QLabel("Limit 파일 (.lt / .pds) — 재판정 fail 의 bin 매칭에 사용")
        title.setStyleSheet("color:#1d4ed8; font-weight:600;")
        lay.addWidget(title)
        drop = _LimitsDropArea(self._load_limits)
        drop_lay = QHBoxLayout(drop)
        # 파일을 넣으면 이 안내문 **자리 그대로** 파일명으로 바뀐다 (_load_limits).
        self.lbl_drop = QLabel(_DROP_HINT)
        self.lbl_drop.setStyleSheet(_DROP_HINT_STYLE)
        drop_lay.addWidget(self.lbl_drop)
        drop_lay.addStretch(1)
        btn_pick = QPushButton("파일 선택…")
        btn_pick.clicked.connect(self._pick_limits)
        drop_lay.addWidget(btn_pick)
        lay.addWidget(drop)
        self.lbl_limits = QLabel(
            "불러온 파일 없음 — bin 매칭은 RT 에서 죽은 bin → 999 순으로 처리합니다.")
        self.lbl_limits.setStyleSheet("color:#64748b;")
        lay.addWidget(self.lbl_limits)
        return box

    def _build_hint(self):
        if self._is_dut:
            return self._hint_label([
                "· 이름·순서는 서버가 DUT 값으로 정합니다 (수치 오름차순) — 이 창에서는"
                " 색만 지정합니다.",
                "· 색 칸을 더블클릭하면 이 리포트에만 적용되는 색으로 바꿉니다"
                "(옵션(F10) 팔레트보다 우선).",
            ])
        lines = ["· 최상단(1번) source 의 Limit(HiLIM/LoLIM) 기준으로 리포트가 생성됩니다.",
                 "· 여기 보이는 위→아래 순서가 그대로 web_report 표시 순서입니다"
                 " (Yield/Distribution 의 source 순서" + (", Temperature 그룹 순서)."
                                                          if self._is_temp else ")."),
                 "· Ctrl/Shift 로 여러 행을 골라 ↑/↓ 로 함께 옮기고, ↑↑/↓↓ (Alt+Home/End) 로"
                 " 최상단·최하단으로 보냅니다."]
        if self._is_temp:
            lines.append(
                "· 그룹마다 RT 가 그 그룹의 Limit 판정 기준입니다 — CT/HT 는 RT 의 Bin1 좌표만"
                " 남기고 RT limit 으로 다시 판정합니다.")
            lines.append(
                f"· Group 칸은 비워 두어도 됩니다. 한 행에 이름을 적으면 같은 그룹의 나머지"
                f" 행과 source 이름 앞부분(_RT/_CT/_HT 제외)이 함께 바뀝니다"
                f" (최대 {self._legend_max - 3}자).")
            lines.append(
                "· Group 칸 ▼ 드롭다운에서 다른 그룹을 고르면 그 행이 그 그룹으로"
                " 이동합니다 (다른 그룹의 이름을 직접 적어도 같습니다).")
            lines.append(
                "· Enter 는 입력 확정만 합니다 — 창은 아래 OK 를 눌러야 닫힙니다.")
        lines += [
            "· 색은 순서(1,2,3…)에 붙습니다 — 순서를 바꾸면 Distribution 색 번호도 함께"
            " 바뀝니다.",
            "· 색 칸을 더블클릭하면 이 리포트에만 적용되는 색으로 바꿉니다"
            "(옵션(F10) 팔레트보다 우선).",
            "· 파일 이름 위에 마우스를 올리면 전체 경로가 보입니다. 삭제는 할 수 없습니다.",
        ]
        return self._hint_label(lines)

    @staticmethod
    def _hint_label(lines):
        hint = QLabel("\n".join(lines))
        hint.setStyleSheet("color:#64748b; font-size:9px;")
        hint.setWordWrap(True)
        return hint

    # ── 렌더 ────────────────────────────────────────────────────────────────
    def _renumber_groups(self):
        """그룹 번호를 **표 등장 순서**로 다시 매긴다 (Temperature 전용).

        result_arrangement 가 groups 를 표 순서로 내보내고 서버가 그 순서를 그대로
        ``temp_group`` 번호·Temp Fail 컬럼 순서로 쓰므로, 창에 보이는 번호도 같은 순서여야
        "위에 있는 그룹이 리포트에서도 먼저" 가 눈으로 확인된다. 이름을 붙인 그룹은
        _group_names 키도 함께 옮긴다.
        """
        remap = {}
        for row in self._rows:
            if row.group and row.group not in remap:
                remap[row.group] = len(remap) + 1
        if all(old == new for old, new in remap.items()):
            return                                 # 이미 표 순서 (대부분의 경우)
        self._group_names = {remap[g]: name for g, name in self._group_names.items()
                             if g in remap}
        for row in self._rows:
            row.group = remap.get(row.group, 0)

    def _render(self):
        """self._rows 를 표에 통째로 다시 그린다 (행 수가 작아 체감 비용 0)."""
        if self._is_temp:
            self._renumber_groups()
        self._rendering = True
        # Group 칸 편집기(QLineEdit) 래퍼 보관소 — 아래 _group_edit 주석 참조.
        # 표를 다시 그릴 때마다 옛 위젯은 파괴되므로 여기서 비운다.
        self._group_edits = []
        try:
            self.table.setRowCount(0)
            self.table.setRowCount(len(self._rows))
            for r, row in enumerate(self._rows):
                self.table.setVerticalHeaderItem(
                    r, QTableWidgetItem(f"{r + 1} ★" if r == 0 and not self._is_dut
                                        else str(r + 1)))

                cell = QTableWidgetItem(shorten_path(row.path, self._path_chars))
                cell.setFont(self._mono)
                cell.setToolTip(row.path or "(원본 파일 경로 정보 없음)")
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(r, 0, cell)

                legend = QTableWidgetItem(row.legend)
                if self._is_dut:                 # 이름은 서버가 DUT 값으로 만든다
                    legend.setFlags(legend.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(r, 1, legend)

                if self._is_temp:
                    self.table.setCellWidget(r, 2, self._group_edit(r, row))
                    self.table.setCellWidget(r, 3, self._role_combo(r, row))

                swatch = QTableWidgetItem("")
                swatch.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                color = self._color_at(r)
                swatch.setBackground(QBrush(QColor(color)))
                swatch.setToolTip(f"{color} — 더블클릭하면 색을 바꿉니다")
                self.table.setItem(r, self._color_col, swatch)
            self._paint_rows()
        finally:
            self._rendering = False

    def _gids(self):
        """지정된 그룹 번호 목록 — 표 등장 순서."""
        gids = []
        for row in self._rows:
            if row.group and row.group not in gids:
                gids.append(row.group)
        return gids

    def _group_display(self, gid):
        """그룹 표시명 — 사용자가 붙인 이름이 없으면 기본 '그룹 N'."""
        return self._group_names.get(gid) or f"그룹 {gid}"

    def _group_edit(self, r, row):
        """Group 칸 = 그룹 선택 드롭다운 + 이름 입력칸 (2026-08-24 요청으로 드롭다운 추가).

        드롭다운에서 다른 그룹을 고르면 이 행이 **그 그룹으로 이동**한다 — 자동 배치가
        잘못 묶은 source 를 '그룹 초기화 후 전부 다시' 없이 바로잡는 수단이다. 직접
        타이핑은 종전과 같다: 이름을 적으면 같은 그룹 전원 일괄 개명, 다른 그룹의
        이름을 적으면 그 그룹으로 이동, 미지정 행의 새 이름은 새 그룹 생성.
        기본 표시는 빈 칸 + placeholder(회색 "그룹 N") — 이름 없는 그룹임을 보여준다.

        ⚠️ **``self._group_edits.append(edit)`` 를 지우지 말 것** (2026-08-25 회귀 수정).
        ``combo.lineEdit()`` 은 **C++ 이 소유한 객체의 임시 파이썬 래퍼**라, 거기 건
        ``editingFinished`` 연결(람다)은 그 래퍼가 GC 되는 순간 함께 사라진다 —
        ``receivers()`` 는 그대로 2 라서 **에러 없이 조용히 죽고**, 사용자에게는 "이름을
        적어도 아무 일이 안 일어난다" 로만 보인다(드롭다운·Role 콤보는 sender 가 파이썬이
        만든 위젯이라 멀쩡해서 더 헷갈린다). 창이 래퍼를 붙들어야 연결이 산다.
        회귀 고정: ``tests/test_source_group_rename.py``.
        """
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.addItem(_NO_GROUP, 0)
        for gid in self._gids():
            combo.addItem(self._group_display(gid), gid)
        combo.setCurrentIndex(-1)                 # 표시 텍스트는 아래에서 직접 넣는다
        edit = combo.lineEdit()
        edit.setText(self._group_names.get(row.group, "") if row.group else "")
        edit.setMaxLength(self._legend_max - 3)   # 접미사(_RT 등 3자) 자리 확보
        edit.setPlaceholderText(f"그룹 {row.group}" if row.group else _NO_GROUP)
        combo.setToolTip(
            "▼ 드롭다운에서 그룹을 고르면 이 행이 그 그룹으로 이동합니다.\n"
            "그룹 이름을 입력하면 같은 그룹의 나머지 행과 source 이름이 함께 바뀝니다.\n"
            "Enter 는 입력 확정만 합니다(창은 닫히지 않습니다).")
        combo.activated.connect(lambda idx, i=r, c=combo: self._on_group_pick(i, idx, c))
        # 엔터·포커스 아웃 모두 editingFinished 하나로 받는다 (엔터는 둘 다 발화).
        edit.editingFinished.connect(lambda i=r, e=edit: self._on_group_text(i, e))
        self._group_edits.append(edit)     # 래퍼 GC = 연결 소멸 (위 docstring ⚠️)
        return combo

    def _role_combo(self, r, row):
        combo = QComboBox()
        for role in _ROLE_ITEMS:
            combo.addItem(role or _NO_GROUP)
        combo.setCurrentIndex(_ROLE_ITEMS.index(row.role) if row.role in _ROLE_ITEMS else 0)
        combo.currentTextChanged.connect(lambda text, i=r: self._on_role_changed(i, text))
        return combo

    def _paint_rows(self):
        """최상단 강조 + 그룹 경계 구분선 + RT 음영. 표를 다시 그릴 때마다 마지막에 돈다.

        선택색이 배경을 덮으므로 배경 하나에 의존하지 않고 **굵은 글씨 + 세로헤더 ★** 를
        함께 쓴다. 비-최상단 복원은 무효 브러시(팔레트 기본) — 흰색을 칠하면 테마가 깨진다.
        """
        for r, row in enumerate(self._rows):
            top = (r == 0 and not self._is_dut)    # DUT 는 limit 기준이 원본 1개라 무의미
            # 그룹 경계를 눈으로 잡으려고 짝수 그룹에 아주 옅은 배경을 준다 (최상단이 우선).
            band = QColor(_GROUP_BAND) if (self._is_temp and row.group and row.group % 2 == 0) \
                else None
            for c in range(self.table.columnCount()):
                item = self.table.item(r, c)
                if item is None or c == self._color_col:
                    continue                       # 색 스와치는 자기 색을 지켜야 한다
                if top:
                    item.setBackground(QBrush(QColor(_TOP_BG)))
                elif band is not None:
                    item.setBackground(QBrush(band))
                else:
                    item.setBackground(QBrush())   # 무효 브러시 = 팔레트 기본 (테마 안전)
                font = item.font()
                font.setBold(top)
                item.setFont(font)
            if self._is_temp:
                combo = self.table.cellWidget(r, 3)
                if combo is not None:
                    combo.setStyleSheet(f"background:{_RT_BG};" if row.role == "RT" else "")
            self.table.setRowHeight(r, self._row_h)

    def _color_at(self, r):
        return self._colors[r] if r < len(self._colors) else "#888888"

    # ── 조작 ────────────────────────────────────────────────────────────────
    def _commit_editor(self):
        """열린 셀 편집기를 커밋한다 — 없으면 마지막 타이핑이 _render 에 날아간다."""
        if self.table.state() == QAbstractItemView.State.EditingState:
            self.table.setCurrentCell(self.table.currentRow(), 0)

    def _on_item_changed(self, item):
        if self._rendering or item.column() != 1:
            return
        text = (item.text() or "").strip()[:self._legend_max]
        self._rows[item.row()].legend = text

    def _on_group_pick(self, r, idx, combo):
        """드롭다운에서 그룹 선택 = 이 행을 그 그룹으로 이동. (미지정) 은 그룹 해제.

        같은 그룹을 다시 고르면 소속은 그대로 두고 표시 텍스트만 원복한다(_render).
        선택 직후 따라오는 editingFinished 는 표시 문자열 가드(_on_group_text)가 걸러낸다.
        """
        if self._rendering or r >= len(self._rows):
            return
        try:
            gid = int(combo.itemData(idx) or 0)
        except RuntimeError:                      # _render 가 위젯을 이미 파괴한 뒤
            return
        row = self._rows[r]
        if gid == row.group:
            self._render()
            return
        row.group = gid
        if gid:
            self._sync_group_name(gid, only_row=row)  # 이름 붙은 그룹이면 legend 도 맞춘다
        self._render()

    def _on_group_text(self, r, edit):
        """Group 칸에 적은 이름 — 개명(같은 그룹 전원 일괄) / 다른 그룹으로 이동 / 지우기.

        다른 그룹의 이름(사용자 지정 이름 또는 기본 표시 '그룹 N')을 적으면 **그 그룹으로
        이동**한다 — 드롭다운 선택과 같은 뜻이다(2026-08-24 통일. 종전에는 그룹 있는 행이
        적으면 중복 이름이라 거부했지만, 이동으로 해석하면 같은 이름 두 그룹이 생길 길
        자체가 없다). 미지정 행이 새 이름을 적으면 새 그룹을 만든다.
        """
        if self._rendering:
            return
        try:
            text = (edit.text() or "").strip()
        except RuntimeError:                      # _render 가 위젯을 이미 파괴한 뒤
            return
        if r >= len(self._rows):
            return
        row = self._rows[r]
        current = self._group_names.get(row.group, "") if row.group else ""
        if text == current:
            return                                # 변화 없음 (포커스 아웃 포함)
        # 드롭다운 표시 문자열이 텍스트로 남은 경우 — 이름 입력이 아니므로 원복만 한다.
        if text == _NO_GROUP or (row.group and text == f"그룹 {row.group}"):
            self._render()
            return
        if not text:                              # 이름만 지운다 — 그룹 소속은 유지
            if row.group:
                self._group_names.pop(row.group, None)
            self._render()
            return
        owner = next((g for g in self._gids()
                      if g != row.group and self._group_display(g) == text), 0)
        if owner:                                 # 다른 그룹의 이름 → 그 그룹으로 이동/편입
            row.group = owner
            self._sync_group_name(owner, only_row=row)
            self._render()
            return
        if not row.group:                         # 미지정 행에서 새 이름 → 새 그룹 생성
            row.group = max([rw.group for rw in self._rows] or [0]) + 1
        self._group_names[row.group] = text
        self._sync_group_name(row.group)
        self._render()

    def _sync_group_name(self, gid, only_row=None):
        """이름 붙은 그룹의 멤버 legend 앞부분을 그룹 이름으로 맞춘다 (숫자 기본 그룹은 무시)."""
        name = self._group_names.get(gid)
        if not name:
            return
        rows = [only_row] if only_row is not None \
            else [rw for rw in self._rows if rw.group == gid]
        for rw in rows:
            rw.legend = apply_role_suffix(name, rw.role)[:self._legend_max]

    def _on_role_changed(self, r, text):
        if self._rendering:
            return
        role = text if text in ROLES else ""
        row = self._rows[r]
        row.role = role
        # 접미사만 갈아끼운다 — 사용자가 편집한 이름 본체는 그대로 남는다.
        row.legend = apply_role_suffix(row.legend, role)[:self._legend_max]
        self._render()

    def _on_cell_double_clicked(self, r, c):
        if c != self._color_col:
            return
        chosen = QColorDialog.getColor(QColor(self._color_at(r)), self,
                                       f"{r + 1}번 source 색상 선택")
        if chosen.isValid():
            while len(self._colors) <= r:
                self._colors.append("#888888")
            self._colors[r] = chosen.name().upper()
            self._colors_changed = True
            self._render()

    def _selected_rows(self):
        """선택 행 index 오름차순. 선택이 없으면 현재 행 하나 (Ctrl/Shift 다중 선택 지원)."""
        model = self.table.selectionModel()
        rows = sorted({idx.row() for idx in model.selectedRows()}) if model else []
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        return rows

    def _select_rows(self, rows):
        """행 선택·스크롤을 복원한다 — _render() 가 표를 통째로 다시 그려 선택이 날아간다.

        ``selectRow`` 는 호출마다 이전 선택을 지우므로 여러 행을 복원할 수 없다. 그래서
        선택 범위를 한 번에 모아 selectionModel 에 넘긴다.
        """
        model = self.table.selectionModel()
        if model is None or not rows:
            return
        self.table.setCurrentCell(rows[0], max(self.table.currentColumn(), 0))
        selection = QItemSelection()
        src = self.table.model()
        last = self.table.columnCount() - 1
        for r in rows:
            selection.select(src.index(r, 0), src.index(r, last))
        model.select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect
                     | QItemSelectionModel.SelectionFlag.Rows)
        self.table.scrollToItem(self.table.item(rows[0], 0))

    def _shift(self, delta):
        """선택 행(복수 가능)을 한 칸 이동. 이동 방향 끝에 붙은 행은 그 자리에 남는다.

        blocked 는 "더 이상 못 가는 자리" — 맨 위(아래)에 이미 붙어 있는 선택 행이 여기서
        걸린다. 그렇게 해야 선택 블록이 경계에 닿아도 블록 안에서 서로 뒤섞이지 않는다.
        """
        if self._is_dut:
            return                                 # DUT 순서는 서버가 정한다
        self._commit_editor()
        rows = self._selected_rows()
        if not rows:
            return
        moved = []
        if delta < 0:
            blocked = 0
            for r in rows:
                if r == blocked:
                    blocked += 1
                    moved.append(r)
                    continue
                self._rows[r - 1], self._rows[r] = self._rows[r], self._rows[r - 1]
                moved.append(r - 1)
        else:
            blocked = len(self._rows) - 1
            for r in reversed(rows):
                if r == blocked:
                    blocked -= 1
                    moved.append(r)
                    continue
                self._rows[r + 1], self._rows[r] = self._rows[r], self._rows[r + 1]
                moved.append(r + 1)
        self._render()
        self._select_rows(sorted(moved))

    def _move_edge(self, to_top):
        """선택 행(복수 가능)을 통째로 최상단/최하단으로 보낸다 (선택 안 순서는 유지)."""
        if self._is_dut:
            return                                 # DUT 순서는 서버가 정한다
        self._commit_editor()
        rows = set(self._selected_rows())
        if not rows:
            return
        picked = [row for i, row in enumerate(self._rows) if i in rows]
        rest = [row for i, row in enumerate(self._rows) if i not in rows]
        self._rows = picked + rest if to_top else rest + picked
        self._render()
        base = 0 if to_top else len(rest)
        self._select_rows([base + i for i in range(len(picked))])

    def _restore(self):
        self._commit_editor()
        self._reset_rows()
        if self._is_temp:
            self._auto_arrange(silent=True)
        else:
            self._render()

    def _clear_groups(self):
        self._commit_editor()
        for row in self._rows:
            row.group = 0
            row.role = ""
        self._group_names = {}
        self._render()

    def _edit_palette(self):
        """48색 팔레트 편집(옵션 F10 과 같은 창) — 저장되면 표의 스와치를 다시 읽는다."""
        from honey_ui.dialogs import ColorEditorDialog
        if ColorEditorDialog(self).exec():
            self._colors = load_palette()      # 모듈 함수 (self 메서드가 아니다)
            self._colors_changed = True
            self._render()

    # ── Temperature 자동 배치 ───────────────────────────────────────────────
    def _auto_arrange(self, silent=False):
        """그룹·역할을 제안하고 **행 순서까지 그룹 순(RT→CT→HT)으로 재정렬**한다.

        표시 순서가 곧 업로드 순서라, 재정렬해 두면 order 가 구 배치 창과 같은 형태
        (그룹마다 RT → CT → HT)가 된다. 배치 못 한 source 는 미지정으로 뒤에 남는다.
        """
        names = [row.legend for row in self._rows]
        # 짝 키는 원본 이름 기준으로 받았으므로 현재 legend(개명·접미사 반영)로 옮겨 잇는다.
        key_by_legend = {}
        for row in self._rows:
            key = self._pair_keys.get(self._original[row.index])
            if key:
                key_by_legend.setdefault(row.legend, key)
        groups = (suggest_groups_by_role(names, self._roles.get, key_by_legend.get)
                  if self._roles else suggest_groups(names, key_by_legend.get))
        if not groups:
            if not silent:
                QMessageBox.information(
                    self, "자동 배치",
                    "파일명·폴더에서 RT/CT/HT 를 알아내지 못했습니다.\n"
                    "Group 과 Role 을 직접 골라 주세요.")
            self._render()
            return

        by_name = {}
        for row in self._rows:
            by_name.setdefault(row.legend, row)     # 이름이 겹치면 첫 행만 (조용한 덮어쓰기 금지)
        for row in self._rows:
            row.group = 0
            row.role = ""
        self._group_names = {}                    # 그룹을 새로 짜므로 옛 이름은 무효
        ordered = []
        for gi, mapping in enumerate(groups, start=1):
            for role in ROLES:
                row = by_name.get(mapping.get(role))
                if row is None or row.group:
                    continue
                row.group, row.role = gi, role
                row.legend = apply_role_suffix(row.legend, role)[:self._legend_max]
                ordered.append(row)
        ordered += [row for row in self._rows if not row.group]
        self._rows = ordered
        self._render()

    # ── Limit 파일 ──────────────────────────────────────────────────────────
    def _pick_limits(self):
        path, _ = QFileDialog.getOpenFileName(self, "Limit 파일 선택", "", LIMIT_FILTER)
        if path:
            self._load_limits([path])

    def _load_limits(self, paths):
        """.lt/.pds 를 파싱해 항목→bin 매핑을 만든다. 실패는 경고 후 무시.

        limit 파일은 **1개만** 받는다 — 성공하면 드롭 영역의 안내문 자리가 그 파일명으로
        바뀐다(어느 파일을 넣었는지 그 자리에서 바로 보이게).

        파싱은 워커 스레드에서 돌린다 — 큰 limit 파일을 UI 스레드에서 읽으면 창이 통째로
        얼어붙는다. 읽는 동안 창은 비활성 + 대기 커서로 두고 이벤트만 돌린다.
        """
        paths = list(paths)[:1]
        prev_text = self.lbl_limits.text()
        prev_style = self.lbl_limits.styleSheet()
        self.lbl_limits.setText("Limit 파일 읽는 중...")
        self.lbl_limits.setStyleSheet("color:#64748b;")
        self.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(parse_limit_files, list(paths))
                while True:
                    done, _ = concurrent.futures.wait([fut], timeout=0.05)
                    QApplication.processEvents()
                    if done:
                        break
                merged, loaded, errors = fut.result()
        finally:
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)

        for name, reason in errors:
            QMessageBox.warning(self, "Limit 파일 읽기 실패", f"{name}\n{reason}")
        if not loaded:
            self.lbl_limits.setText(prev_text)
            self.lbl_limits.setStyleSheet(prev_style)
            return
        self._bin_map = merged
        self._limits_file = {"name": loaded[0][0], "type": loaded[0][1]}
        self.lbl_drop.setText(loaded[0][0])          # 안내문 자리 = 넣은 파일명
        self.lbl_drop.setStyleSheet(_DROP_FILE_STYLE)
        self.lbl_drop.setToolTip(str(paths[0]))
        self.lbl_limits.setText(" / ".join(f"{n} ({k}) — 항목 {c}건" for n, k, c in loaded))
        self.lbl_limits.setStyleSheet("color:#166534;")

    # ── 크기 ────────────────────────────────────────────────────────────────
    def _apply_size(self):
        """21행까지 스크롤 없이 — 그 이상이면 세로 스크롤바가 자동으로 붙는다."""
        unit_mono = QFontMetrics(self._mono).horizontalAdvance("0")
        unit_ui = QFontMetrics(self.font()).horizontalAdvance("0")
        self.table.setColumnWidth(0, self._path_chars * unit_mono + 18)
        self.table.setColumnWidth(1, self._legend_max * unit_ui + 28)
        width = self._legend_max * unit_ui + 28
        if not self._is_dut:                       # DUT 는 파일 열을 숨긴다
            width += self._path_chars * unit_mono + 18
        if self._is_temp:
            # Group 칸은 드롭다운 화살표(≈20px)가 폭을 먹는다 — 이름 자리 확보용으로 넓힌다.
            for col, w in ((2, 118), (3, 92)):
                self.table.setColumnWidth(col, w)
                width += w
        self.table.setColumnWidth(self._color_col, 52)
        width += 52
        self.table.horizontalHeader().setSectionResizeMode(
            self._color_col, QHeaderView.ResizeMode.Fixed)

        header = self.table.horizontalHeader().sizeHint().height()
        frame = self.table.frameWidth()
        visible = min(max(len(self._rows), 1), _MAX_VISIBLE_ROWS)
        self.table.setMinimumHeight(header + visible * self._row_h + 2 * frame + 2)
        scrollbar = (self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
                     if len(self._rows) > _MAX_VISIBLE_ROWS else 0)
        width += self.table.verticalHeader().sizeHint().width() + scrollbar + 2 * frame + 2
        # 높이는 레이아웃에 물어본다 — 표 최소높이가 이미 visible 행을 담고 있으므로
        # 여유값을 어림으로 더하면(하단 요소가 그보다 작을 때) 표 아래에 빈 띠가 남는다.
        self.layout().activate()
        # DUT 는 파일 열이 없어 폭 계산만으로는 힌트 문구가 줄줄이 접힌다 — 하한을 둔다.
        _fit_to_screen(self, max(width + 90, 460), self.layout().sizeHint().height())

    # ── 결과 ────────────────────────────────────────────────────────────────
    def _accept(self):
        self._commit_editor()
        if self._is_temp:
            missing = [str(r + 1) for r, row in enumerate(self._rows)
                       if not row.role or not row.group]
            if missing:
                QMessageBox.warning(
                    self, "Temperature 배치",
                    f"{', '.join(missing)}번 행의 Group 또는 Role 이 비어 있습니다.\n"
                    "모든 source 에 그룹과 역할(RT/CT/HT)을 지정해 주세요.")
                return
            counts = {}
            for row in self._rows:
                if row.role == "RT":
                    counts[row.group] = counts.get(row.group, 0) + 1
            bad = sorted(g for g in {row.group for row in self._rows} if counts.get(g, 0) != 1)
            if bad:
                QMessageBox.warning(
                    self, "Temperature 배치",
                    f"Group {', '.join(map(str, bad))} 의 RT 가 1개가 아닙니다.\n"
                    "RT 는 Limit 판정 기준이라 그룹마다 정확히 1개여야 합니다.")
                return
            # 같은 그룹에 같은 Role 이 여러 개면 배치 오류다 (2026-08-24 요청) —
            # RT 는 위에서 이미 '정확히 1개'를 강제했으므로 CT/HT 만 살핀다.
            per = {}
            for row in self._rows:
                per.setdefault(row.group, []).append(row.role)
            dup = [f"{self._group_display(gid)} 에 {role} {per[gid].count(role)}개"
                   for gid in self._gids() for role in ("CT", "HT")
                   if per[gid].count(role) > 1]
            if dup:
                QMessageBox.warning(
                    self, "Temperature 배치",
                    "같은 그룹에 같은 Role 이 여러 개 배치되어 있습니다:\n"
                    + "\n".join(f"· {d}" for d in dup)
                    + "\n\n그룹마다 RT/CT/HT 는 각각 1개까지만 배치할 수 있습니다.")
                return
            # 그룹별 Role 구성이 서로 다르면(예: 한 그룹만 HT 없음) 짝을 잘못 지었을
            # 가능성이 크다 — 정말 파일이 없는 경우도 있으므로 차단하지 않고 확인만 받는다.
            comp = {gid: tuple(role for role in ROLES if role in per.get(gid, []))
                    for gid in self._gids()}
            if len(set(comp.values())) > 1:
                lines = [f"· {self._group_display(gid)}: {' + '.join(comp[gid])}"
                         for gid in self._gids()]
                if QMessageBox.question(
                        self, "Temperature 배치",
                        "그룹별 Role 구성이 서로 다릅니다 — 짝이 맞는지 확인해 주세요.\n"
                        + "\n".join(lines) + "\n\n이대로 계속하시겠습니까?",
                        QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok:
                    return
            # OK 즉시 업로드로 넘어가지 않고 한 번 확인한다 (2026-08-11 요청) — 그룹/이름을
            # 만지다 무심코 OK(또는 Enter) 를 눌러 잘못된 배치로 생성되는 실수 방지.
            if QMessageBox.question(
                    self, "Web Report 생성",
                    "해당 설정으로 web report 생성됩니다.\n계속하시겠습니까?",
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Ok) != QMessageBox.StandardButton.Ok:
                return
        self.accept()

    def result_arrangement(self) -> dict:
        """OK 결과.

        - ``names``        : **원본 source 순서**의 새 이름 — df_honey_group.rename_sources 용
        - ``source_names`` : 창에 들어올 때의 원본 이름(원본 순서). 호출부가 파싱 결과와
          이름 정합을 볼 때 **rename 전 이름**으로 비교해야 하므로 함께 돌려준다.
        - ``order_index``  : 표시 순서의 **원본 index** — 실제 순서 배선은 이걸 쓴다.
          이름 문자열로 순서를 이으면 dedupe 규칙 차이로 mass_data_map 키와 어긋날 수 있다.
        - ``order``        : 표시 순서의 새 이름 (로그·참고용)
        - Temperature 전용: ``groups`` / ``bin_map`` / ``limits_file``
        - ``colors``       : 창에서 바꿨을 때만 48색 목록, 아니면 None (옵션 팔레트 유지)

        DUT 모드는 이름·순서를 클라가 정하지 않으므로 호출부가 ``colors`` 만 쓴다
        (나머지 키는 서버가 만들 pseudo-source 이름이라 rename 에 넘기면 안 된다).
        """
        by_index = {row.index: row.legend for row in self._rows}
        raw = [by_index.get(i, self._original[i]) for i in range(len(self._original))]
        deduped = dedupe_names(raw)

        out = {
            "names": deduped,
            "source_names": list(self._original),
            "order_index": [row.index for row in self._rows],
            "order": [deduped[row.index] for row in self._rows],
            "colors": list(self._colors) if self._colors_changed else None,
        }
        if not self._is_temp:
            return out

        # 그룹 순서·그룹 안 member 순서 모두 **표 순서 그대로**다 — 서버가 groups 순서를
        # temp_group 번호와 Temp Fail 컬럼 순서로 쓰므로(web_report/tabs/temp_fail.py
        # temp_member_pairs), 표에서 위에 있는 그룹이 리포트에서도 먼저 나온다.
        gids = []
        for row in self._rows:
            if row.group and row.group not in gids:
                gids.append(row.group)
        groups = []
        for gid in gids:
            rt_name, members, member_roles = "", [], []
            for row in self._rows:
                if row.group != gid:
                    continue
                if row.role == "RT":
                    rt_name = rt_name or deduped[row.index]   # _accept 가 그룹당 1개를 강제
                elif row.role in ROLES:
                    members.append(deduped[row.index])
                    member_roles.append(row.role)
            if not rt_name:
                continue
            groups.append({"rt": rt_name,
                           "members": members, "member_roles": member_roles})
        out.update({"groups": groups, "bin_map": self._bin_map,
                    "limits_file": self._limits_file})
        return out

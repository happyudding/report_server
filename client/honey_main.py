"""Honey 클라이언트 (PyQt6).

UI 레이아웃은 .ui (Qt Designer 편집 가능) 에 정의, 런타임에 uic.loadUi 로 로드.
- honey_main.ui   : 메인 화면 (d1_storage 검색 → 분석 → 자동 저장 → 업로드)
- upload_dialog.ui: 서버 업로드용 메타(Product Type 라디오/Product/LOT/Revision/PW) 팝업
- d1_browser.ui   : d1_storage(가상 서버 스토리지) 파일 검색/선택 팝업

워크플로우: d1_storage 에서 CSV 검색·선택 → 출력 시트 선택 → '분석 실행' 시
입력 폴더에 xlsx 자동 저장(xlwings) → '서버에 업로드' 클릭 시 메타 팝업 입력 후 전송.
"""
import concurrent.futures
import contextlib
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

import requests

from PyQt6 import uic
from PyQt6.QtCore import Qt, QTimer, QEvent, QPropertyAnimation, QEasingCurve, QPoint, QRect, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFileDialog, QHeaderView,
    QMainWindow, QMenu, QMessageBox, QProgressDialog, QPushButton,
    QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTableWidgetItem, QToolButton, QWidget,
)

from transport.config import CHROMIUM_FLAGS, CURRENT_VERSION, SERVER_BASE_URL
from transport import app_update, update_policy, updater, uploader, version_check
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from d1 import D1BrowserDialog
from honey_ui import folder_intake
from honey_ui import (
    ColorEditorDialog,
    ElapsedProgress as _ElapsedProgress,
    OperationCancelled as _OperationCancelled,
    OptionsDialog,
    ReportSettingsDialog,
    SHEET_OPTIONS,
    UploadDialog,
    show_error as _show_error,
    show_exc as _show_exc,
    wait_for_future as _wait_for_future,
)
from report_flow import (
    build_output_path as _build_output_path,
    prepare_report_webreport as _prepare_report_webreport,
    suggest_base_name as _suggest_base_name,
)
from web_report.honeyform import dedupe_item_columns, encode_honeyform_parquet
import app_settings
import chart_colors


def _report_error(kind, message, *, stack="", context=None):
    """오류를 서버 진단 사건으로 보고하고 오류번호를 돌려준다 (실패해도 무음).

    transport 를 지연 import 하는 이유: 보고 경로가 앱 기동을 좌우해선 안 된다."""
    try:
        from transport import error_report
        return error_report.report_error(kind, message, stack=stack, context=context)
    except Exception:
        return ""


def _begin_operation(name):
    """작업 단위 시작 — 이후 서버 요청 헤더와 오류 보고가 같은 operation_id 를 쓴다."""
    try:
        from transport import error_report
        return error_report.begin_operation(name)
    except Exception:
        return ""
import client_identity

# 로컬 리포트 엔진 (pandas/xlwings 의존). 미설치 시 화면 비활성.
try:
    import report_generator as rg
    from report_generator import xlsx_writer
    import map_report
    _RG_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    rg = None
    xlsx_writer = None
    map_report = None
    _RG_IMPORT_ERROR = exc

PRODUCT_TYPES = ["MDDI", "PDDI", "PMIC", "SECURITY", "TCON"]
# Temperature(RT/CT/HT) 분석 모드를 고를 수 있는 제품군 — _sync_temperature_mode 가 사용.
_TEMPERATURE_PRODUCT_TYPES = {"PMIC", "SECURITY"}
_FLOW_PROFILE_ON = bool(os.environ.get("HONEY_FLOW_PROFILE"))

# 프리징(onedir) 시 _MEIPASS, 아니면 스크립트 폴더에서 .ui 탐색
_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
UI_PATH = os.path.join(_BASE_DIR, "honey_main.ui")


@contextlib.contextmanager
def _flow_time(label):
    if not _FLOW_PROFILE_ON:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        print(f"[flow-profile] honey_main.{label}: {elapsed:.3f}s", file=sys.stderr, flush=True)


def _init_com_for_worker():
    """Excel COM/xlwings 를 워커 스레드에서 쓸 수 있으면 초기화한다."""
    try:
        import pythoncom  # type: ignore
    except Exception:
        return None
    try:
        pythoncom.CoInitialize()
        return pythoncom
    except Exception:
        return None


def _co_uninitialize(com_module):
    if com_module is None:
        return
    try:
        com_module.CoUninitialize()
    except Exception:
        pass


def _slim_temperature_limits(temperature):
    """Temperature bin_map(.lt/.pds 파싱 결과) → 세션에 실을 최소 형태.

    ``{item: {"tno", "lsl_bin", "usl_bin"}}`` — 서버 Temp 시트의 Bin 표기에만 쓴다
    (web_report/tabs/temp_fail.py). lsl/usl **값**은 서버가 RT 메타행으로 다시 판정하므로
    싣지 않는다. Temperature 가 아니거나 매핑이 없으면 None (서버가 관측 bin 으로 폴백).
    """
    bin_map = (temperature or {}).get("bin_map") or {}
    out = {}
    for item, entry in bin_map.items():
        if not isinstance(entry, dict):
            continue
        slim = {k: entry.get(k) for k in ("tno", "lsl_bin", "usl_bin") if entry.get(k)}
        if slim:
            out[str(item)] = slim
    return out or None


# 업로드가 성공했더라도 서버 응답 대기가 이 시간을 넘으면 서버에 알린다 — 다음 번
# 타임아웃의 예보이기 때문이다. 클라 read timeout(200초)의 절반 아래로 잡는다.
_UPLOAD_SLOW_WAIT_SEC = 60


def _upload_timing_line(timing, verdict):
    """업로드 소요 한 줄 — 실행 로그용. 지금까지 클라는 소요를 어디에도 남기지 않아
    "100%에서 멈췄다"는 신고에 붙일 수치가 없었다.

    전송(body)과 서버 대기(wait)를 나눠 적는 것이 핵심이다 — 서버는 이 둘을 구분해서
    볼 수 없으므로(waitress 가 바디 수신 후에야 요청을 큐에 넣는다) 여기 값이 유일하다.
    """
    if not timing:
        return f"Web Report 업로드 {verdict}"
    return (f"Web Report 업로드 {verdict}: {timing.get('mb', '?')}MB "
            f"전송 {timing.get('body_sec', '?')}s / 서버대기 {timing.get('wait_sec', '?')}s")


def _upload_progress_channel(progress, label_fmt, value_map=None):
    """업로드 진행률 콜백 쌍 (worker_cb, drain_cb) 생성 — _run_web_report/_do_upload 공용.

    worker_cb 는 워커 스레드에서 (bytes_read, total) 을 큐에 넣고, drain_cb 는 메인
    스레드(_wait_for_future poll)에서 마지막 값만 꺼내 progress 에 반영한다.
    label_fmt 는 "{pct}" 플레이스홀더를 포함한 문자열, value_map 은 pct(0~100)를
    progressbar value 로 바꾸는 함수 (없으면 pct 그대로).
    """
    q = queue.Queue()

    def worker_cb(bytes_read, total_bytes):
        q.put((bytes_read, total_bytes))

    def drain_cb():
        last = None
        while True:
            try:
                last = q.get_nowait()
            except queue.Empty:
                break
        if last is None:
            return
        bytes_read, total_bytes = last
        pct = int(bytes_read * 100 / total_bytes) if total_bytes else 0
        msg = label_fmt.format(pct=pct)
        progress.set(msg, value=value_map(pct) if value_map else pct, status=msg)

    return worker_cb, drain_cb


def _parse_progress_poll(progress, stage_q, slow_sec=60):
    """``_parse_group_core`` 의 (value, label) 큐를 진행바에 반영하는 poll_cb 생성.

    한 단계가 slow_sec 을 넘기면 라벨에 "(계속 진행중)" 만 덧붙인다 — 큰 파일 1개가
    오래 걸릴 때 멈춘 게 아님을 알리기 위한 것으로, 중단하지는 않는다.
    """
    state = {"value": 0, "label": "파일 로딩 준비 중...",
             "since": time.monotonic(), "shown": None}

    def poll_cb():
        while True:
            try:
                value, label = stage_q.get_nowait()
            except queue.Empty:
                break
            state["value"], state["label"] = value, label
            state["since"] = time.monotonic()
        label = state["label"]
        if time.monotonic() - state["since"] >= slow_sec:
            label = f"{label}  (계속 진행중)"
        # 라벨이 바뀔 때만 하단 status 바에도 반영(진행중임을 명확히 표시).
        # 진행바 경과시간은 매 폴링마다 계속 갱신된다.
        if label != state["shown"]:
            progress.set(label, value=state["value"], status=label)
            state["shown"] = label
        else:
            progress.set(label, value=state["value"])

    return poll_cb


def _build_webreport_dist_pack(parquet_items, sources, selected_items, mode,
                               stage_cb=None):
    """업로드할 parquet 바이트로 Distribution pack(정렬 완료)을 미리 계산.

    서버는 이 pack 을 **영구 저장**하고 조회 때 덧셈만 해서 ECDF 를 만든다 — 콜드
    빌드의 수십 초 정렬도, 스크롤할 때마다의 배치 재정렬도, 재시작 후 재계산도
    사라진다(web_report/dist_pack.py). 서버 폴백 계산과 같은 공용 빌더를 쓰고, 서버
    loader 가 디코드할 것과 동일한 bytes 를 여기서도 디코드해 입력 차이를 없앤다
    (값 일치 보장 — 검증은 정준 JSON 비교).

    반환 {"index": json str, "chunks": {id: gzip bytes}}. 실패는 호출부가 잡아 미첨부로
    진행한다(서버가 조회 때 폴백 계산 — 업로드는 계속).

    stage_cb(msg): 워커 스레드에서 단계 문자열을 보고. 호출부가 queue 로 받아 진행바
    라벨을 갱신한다(UI 스레드 직접 접근 금지).

    실제 조립은 web_report.dist_pack.build_pack_from_parquet — Excel 왕복 반영
    (excel_edit)도 같은 함수를 쓴다(두 경로가 만드는 pack 의 값이 같아야 한다).
    """
    from web_report.dist_pack import build_pack_from_parquet

    items = []
    for idx, item in enumerate(parquet_items):
        src = sources[idx] if idx < len(sources) else {}
        items.append({"data": item["data"], "name": src.get("name"),
                      "file_name": src.get("file_name")})
    return build_pack_from_parquet(items, selected_items, mode, stage_cb=stage_cb)


def _encode_sources_worker(entries, cancel_evt=None):
    """source 별 parquet 인코딩을 미리 돌려 ``{id(md): (bytes, renames)}`` 로 돌려준다.

    **워커 스레드 전용 순수 함수**다 — UI·MainWindow(self)·work_group·md.name 을 읽지도
    쓰지도 않는다. 그래야 UI 스레드가 배치 다이얼로그 결과로 ``rename_sources`` 를 돌리는
    것과 읽기/쓰기 집합이 겹치지 않는다(rename 이 건드리는 것은 md.name 과 그룹의 dict/
    캐시뿐이고, 여기서 읽는 것은 md.df 뿐이다).

    미리 만들어 둘 수 있는 이유는 **인코딩 결과가 source 이름·순서와 무관**하기 때문이다 —
    honeyform 에는 이름 컬럼이 없어서(web_report/honeyform.py META_COLUMNS) 이름·순서는
    manifest 의 sources/items 메타에만 실린다. 그래서 배치창에서 이름을 바꾸든 순서를
    바꾸든 재인코딩이 필요 없다.

    캐시 키가 md **객체 identity** 인 이유는 ``_apply_source_arrangement`` 와 같다 —
    이름은 dedupe 규칙 차이로 갈릴 수 있지만 md 객체는 rename/reorder 를 관통해 그대로다.
    호출부(_EncodePrefetch)가 md 참조를 붙들어 id 재사용을 막는다.

    cancel_evt 가 서면 그때까지 만든 것만 돌려준다(부분 캐시도 정상 동작 — 조립이 나머지를
    인코딩한다). 실패한 source 는 캐시에 넣지 않는다 — 조립 때 정식 경로가 다시 인코딩하며
    같은 예외를 source 이름과 함께 사용자에게 보여준다(에러 은폐 방지).
    """
    out = {}
    for md, df in entries:
        if cancel_evt is not None and cancel_evt.is_set():
            break
        try:
            frame, renames = dedupe_item_columns(df)
            out[id(md)] = (encode_honeyform_parquet(frame), renames)
        except Exception:  # noqa: BLE001 — 조립 경로가 같은 입력으로 다시 시도한다
            continue
    return out


def _guess_temperature_groups(names, roles, pair_key_of=None):
    """배치창의 자동 배치와 **같은 함수**로 추정 그룹을 만든다.

    반환 형태는 ``SourceNameDialog.result_arrangement()["groups"]`` 와 같은
    ``[{"rt": 이름, "members": [...], "member_roles": [...]}]`` — 그래야
    ``web_report.temperature.clean_frames`` 에 그대로 넣어 볼 수 있다.

    다이얼로그(_auto_arrange)와 **다른 규칙을 쓰면 안 된다** — 추정이 창의 초기 배치와
    달라지면 사용자가 아무것도 안 건드렸는데도 무효 판정이 나 헛경고가 뜬다.
    ``pair_key_of`` 도 같은 이유로 창에 넘긴 ``pair_keys`` 와 같은 것을 넘겨야 한다.
    """
    from honey_ui.temperature_pairing import suggest_groups, suggest_groups_by_role

    try:
        raw = (suggest_groups_by_role(list(names), roles.get, pair_key_of) if roles
               else suggest_groups(list(names), pair_key_of))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for mapping in raw:
        rt = mapping.get("RT")
        if not rt:
            continue
        members, member_roles = [], []
        for role in ("CT", "HT"):                     # members 는 CT 먼저, 그다음 HT
            name = mapping.get(role)
            if name:
                members.append(name)
                member_roles.append(role)
        if members:
            out.append({"rt": rt, "members": members, "member_roles": member_roles})
    return out


def _temp_invalid_members(guess_groups, guess_names, arranged):
    """최종 배치에서 **추정과 RT 파트너가 달라진** member 의 원본 index 집합.

    이름이 아니라 **원본 index** 로 비교한다 — 자동 배치가 legend 에 역할 접미사를
    붙이므로(apply_role_suffix) 최종 이름은 추정 이름과 구조적으로 항상 다르다. 이름으로
    비교하면 100% 오탐이다.

    ``clean_group`` 은 member 마다 (member.df, RT.df, bin_map) 로만 정리하고 CT/HT 역할을
    구분하지 않으므로, **표시 순서 변경·그룹 번호 스왑·CT↔HT 역할 스왑은 정리 결과가
    같다** → 여기서 자동으로 통과한다(경고창도 안 뜬다).

    limit 파일(.lt/.pds)이 들어오면 bin 매칭이 통째로 달라지므로 전 member 가 무효다.
    """
    final_names = list(arranged.get("names") or [])
    final_idx = {n: i for i, n in enumerate(final_names)}
    final_groups = arranged.get("groups") or []
    if arranged.get("bin_map"):
        return {final_idx[m] for g in final_groups for m in (g.get("members") or [])
                if m in final_idx}

    guess_idx = {n: i for i, n in enumerate(list(guess_names))}
    rt_of_guess = {}
    for g in guess_groups:
        rt = guess_idx.get(g.get("rt"))
        if rt is None:
            continue
        for m in g.get("members") or []:
            mi = guess_idx.get(m)
            if mi is not None:
                rt_of_guess[mi] = rt

    invalid = set()
    for g in final_groups:
        rt = final_idx.get(g.get("rt"))
        for m in g.get("members") or []:
            mi = final_idx.get(m)
            if mi is None:
                continue
            if rt is None or rt_of_guess.get(mi) != rt:
                invalid.add(mi)
    return invalid


class _EncodePrefetch:
    """source 배치/이름 다이얼로그가 떠 있는 동안 도는 parquet 인코딩 선행 작업.

    executor 를 ``_run_web_report`` 에 **그대로 넘겨준다** — 선행 job 과 조립 job 이 같은
    ``max_workers=1`` 풀에 있어야 FIFO 로 배타 실행되어 락 없이 안전하고("제출 순서 =
    실행 순서" 를 이용하는 _run_web_report 의 기존 설계도 그대로 유지된다), 조립이 캐시를
    읽을 때 선행 job 이 이미 끝나 있음이 구조적으로 보장된다.

    실패·취소는 전부 "캐시 없음" 으로 수렴한다 — 그러면 종전과 똑같이 전량 인코딩한다.
    """

    def __init__(self, executor, future, cancel_evt, keepalive):
        self.executor = executor
        self._future = future
        self._cancel = cancel_evt
        # 캐시 키가 id(md) 라서 md 가 GC 되면 다른 객체가 같은 id 를 받아 오적중할 수
        # 있다. 결과를 다 쓸 때까지 참조를 붙들어 그 창을 닫는다.
        self.keepalive = keepalive
        self._drop = set()
        self._aborted = False

    @classmethod
    def start(cls, entries):
        """entries=[(md, df)] 로 선행 인코딩 시작. 빈 입력이면 None."""
        if not entries:
            return None
        cancel = threading.Event()
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(lambda: {"raw": _encode_sources_worker(entries, cancel),
                                 "cleaned": {}})
        return cls(ex, fut, cancel, entries)

    def cache_or_empty(self):
        """선행 결과 ``{"raw": {...}, "cleaned": {...}}``. 실패·취소·미완이면 빈 dict.

        두 풀로 나누는 이유: 같은 source 라도 **원본 df 인코딩(raw)** 과 **Temperature
        배치로 정리한 df 인코딩(cleaned)** 은 다른 바이트다. 조립이 자기가 만든 cleaned
        여부로 풀을 골라야 섞이지 않는다.

        **조립 job 안에서 호출한다** — 같은 FIFO 풀이라 선행 job 이 이미 끝나 있어
        블로킹이 없다. UI 스레드에서 부르면 이 대기가 그대로 프리징이 된다.
        """
        try:
            data = self._future.result() or {}
        except Exception:  # noqa: BLE001
            return {}
        cleaned = data.get("cleaned") or {}
        if self._drop:
            cleaned = {k: v for k, v in cleaned.items() if k not in self._drop}
        return {"raw": data.get("raw") or {}, "cleaned": cleaned}

    def drop_cleaned(self, ids):
        """추정과 달라진 source 의 선행 **정리분**을 버린다 (UI 스레드에서 호출).

        future 가 아직 안 끝났을 수 있어 결과 dict 를 직접 지우지 않고 제외 집합으로만
        남긴다 — cache_or_empty 가 걸러 낸다. raw 풀은 배치와 무관하므로 손대지 않는다.
        """
        self._drop |= set(ids)

    def abort(self):
        """취소 + executor 정리. 여러 번 불러도 안전하다."""
        if self._aborted:
            return
        self._aborted = True
        self._cancel.set()
        self.executor.shutdown(wait=False, cancel_futures=True)


class SlideInPanel(QWidget):
    """왼쪽→오른쪽으로 슬라이드되어 나오는 프레임리스 최상위 패널.

    QWebEngineView 는 네이티브 윈도우라 일반 위젯 오버레이가 가려지므로,
    이 패널은 최상위(Qt.Tool) 창으로 만들어 브라우저 위로 슬라이드한다.
    anchor_widget(브라우저)의 왼쪽 가장자리를 기준으로 위치·높이를 잡는다."""

    def __init__(self, anchor_widget, content, title, width=460):
        super().__init__(anchor_widget.window(),
                         Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        from PyQt6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QVBoxLayout
        self._anchor = anchor_widget
        self._width = width

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QWidget()
        frame.setObjectName("slideFrame")
        outer.addWidget(frame)

        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QWidget()
        header.setObjectName("slideHeader")
        h = QHBoxLayout(header)
        h.setContentsMargins(12, 8, 8, 8)
        lbl = QLabel(title)
        lbl.setObjectName("slideTitle")
        btn_close = QToolButton()
        btn_close.setText("◀")
        btn_close.setObjectName("slideClose")
        btn_close.setToolTip("왼쪽으로 밀어 넣기")
        btn_close.clicked.connect(self.hide_animated)
        h.addWidget(lbl)
        h.addStretch(1)
        h.addWidget(btn_close)
        v.addWidget(header)
        v.addWidget(content, 1)

        self.setStyleSheet("""
            QWidget#slideFrame {
                background: #fffef7;
                border-right: 1px solid #d1d5db;
            }
            QWidget#slideHeader {
                background: #1f2937;
            }
            QLabel#slideTitle {
                color: #f9fafb; font-weight: 600; font-size: 11pt;
            }
            QToolButton#slideClose {
                color: #e5e7eb; font-size: 12pt; border: none;
                padding: 2px 8px; border-radius: 4px;
            }
            QToolButton#slideClose:hover { background: #374151; }
        """)

        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.hide()

    def _rects(self):
        """(숨김 위치, 표시 위치) 사각형 — anchor 왼쪽 가장자리 기준."""
        origin = self._anchor.mapToGlobal(QPoint(0, 0))
        h = self._anchor.height()
        shown = QRect(origin.x(), origin.y(), self._width, h)
        hidden = QRect(origin.x() - self._width, origin.y(), self._width, h)
        return hidden, shown

    def _fade_to(self, end_opacity, on_finished=None):
        self._anim.stop()
        try:
            self._anim.finished.disconnect()
        except TypeError:
            pass
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(end_opacity)
        if on_finished is not None:
            self._anim.finished.connect(on_finished)
        self._anim.start()

    def show_animated(self):
        # 창을 화면 밖으로 이동시키지 않고 항상 펼침 위치에 둔 채 투명도만 페이드한다
        # (좌측 인접 모니터로 슬라이드가 넘어가는 문제 회피 — 모니터 배치 무관).
        _, shown = self._rects()
        self.setGeometry(shown)
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
        self.raise_()
        self.activateWindow()
        self._fade_to(1.0)

    def hide_animated(self):
        self._fade_to(0.0, on_finished=self.hide)

    def reposition(self):
        """메인 창 이동/리사이즈 시 표시 중이면 위치 재정렬."""
        if self.isVisible():
            _, shown = self._rects()
            self.setGeometry(shown)


class FilePathDelegate(QStyledItemDelegate):
    """입력 파일 리스트의 '파일 경로' 셀 — 폴더 부분은 기본색, 파일명만 파란 굵은 글씨.

    한 셀 안에서 색을 나눠야 해서 QTableWidgetItem 의 foreground 로는 안 되고
    직접 그린다. 배경/선택 표시는 기본 스타일에 맡기고 텍스트만 두 번 나눠 찍는다."""
    NAME_COLOR = QColor("#1565C0")

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""                      # 배경·선택만 기본 스타일로 그리게
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        cut = max(text.rfind("\\"), text.rfind("/")) + 1   # 경로 구분자는 OS 무관하게 둘 다
        head, name = text[:cut], text[cut:]
        painter.save()
        painter.setFont(opt.font)
        x = rect.x()
        if head:
            painter.setPen(opt.palette.text().color())
            painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter, head)
            x += painter.fontMetrics().horizontalAdvance(head)
        painter.setPen(self.NAME_COLOR)
        bold = QFont(opt.font)
        bold.setBold(True)
        painter.setFont(bold)
        painter.drawText(QRect(x, rect.y(), rect.right() - x + 1, rect.height()),
                         Qt.AlignmentFlag.AlignVCenter, name)
        painter.restore()


class HoneyMainWindow(QMainWindow):
    # 백그라운드 버전 체크 결과 전달 (manifest dict 또는 예외) — cross-thread 라
    # 자동 queued connection (UploadDialog._part_ids_ready 와 같은 패턴)
    _version_manifest_ready = pyqtSignal(object)
    # 릴리스 공지 원문 전달 (announcement.txt 그대로) — 같은 cross-thread 패턴
    _announcement_ready = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        uic.loadUi(UI_PATH, self)
        self.status = self.statusbar
        self.setWindowTitle(f"Honey  v{CURRENT_VERSION}")
        self.setWindowIcon(self._honey_icon(64))   # 꿀단지 실행/창 아이콘
        self.status.showMessage(f"Server: {SERVER_BASE_URL}")
        self.progress_status.hide()
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setUndoRedoEnabled(False)
        self._apply_main_ui_tweaks()

        self.csv_paths = []
        self.csv_roles = {}        # {파일 절대경로: "RT"|"CT"|"HT"} — 폴더 열기로 얻은 온도 역할
        self.group = None          # df_honey_group
        self.last_result = None    # AnalysisResult
        self.out_path = None       # 생성된 xlsx 경로
        self._last_upload = None   # 마지막 업로드 메타 (팝업 프리필용)
        self._busy = False         # 무거운 작업 진행 중 (_set_busy)
        self._busy_actions = []    # busy 중 잠글 메뉴/사이드바 액션 (_build_chrome 이 채움)

        self._pt_radios = {
            "MDDI": self.rb_pt_MDDI, "PDDI": self.rb_pt_PDDI,
            "PMIC": self.rb_pt_PMIC, "SECURITY": self.rb_pt_SECURITY,
            "TCON": self.rb_pt_TCON,
        }
        # 지난 실행에서 고른 Product Type 복원 (사용자별 settings.json)
        saved_pt = app_settings.get_setting("product_type")
        if saved_pt in self._pt_radios:
            self._pt_radios[saved_pt].setChecked(True)
        self._setup_csv_table()
        self._connect_signals()
        self.btn_open_local.setText("📁  LOCAL FILE OPEN")
        self.btn_pick_csv.setText("🐬  Dolphin (D1)에서 불러오기")
        self._build_chrome()

        if rg is None:
            self._disable_engine()
        self._version_manifest_ready.connect(self._on_version_manifest)
        self._announcement_ready.connect(self._on_announcement)
        QTimer.singleShot(500, self.check_for_update)

    def _apply_main_ui_tweaks(self):
        """메인 화면 상단 배치와 주요 파일 선택 버튼 가독성을 조정한다."""
        self.horizontalLayout_top.setStretch(0, 1)
        self.horizontalLayout_top.setStretch(1, 1)

        for button in (self.btn_open_local, self.btn_pick_csv):
            font = QFont(button.font())
            point_size = font.pointSize()
            if point_size > 0:
                font.setPointSize(point_size + 3)
            button.setFont(font)

        # btn_open_local 은 드롭다운(파일/폴더 열기)을 달려고 QToolButton 이다. 앱 전역
        # 스타일시트는 QPushButton 만 칠하므로, 옆 버튼과 같아 보이도록 여기서 칠한다.
        self.btn_open_local.setStyleSheet(
            "QToolButton {"
            " background: #F6C445; color: #4A3B1A; border: 1px solid #E0A81E;"
            " border-radius: 6px; padding: 5px 12px; font-weight: 600; }"
            "QToolButton:hover { background: #FFD65A; }"
            "QToolButton:pressed { background: #E9B21E; }"
            "QToolButton:disabled {"
            " background: #EDE6CF; color: #A89C77; border-color: #E0D9C0; }"
            "QToolButton::menu-button { width: 18px; border-left: 1px solid #E0A81E; }")

    def _init_run_log(self, title):
        self._run_log_started = time.perf_counter()
        self._run_log_step = 0
        self._run_log_total = 0
        self.txt_summary.clear()
        if title:
            self._append_run_log(title)

    def _set_run_log_total(self, total):
        self._run_log_total = max(int(total or 0), 0)

    def _elapsed_run_log(self):
        started = getattr(self, "_run_log_started", None)
        secs = int(time.perf_counter() - started) if started is not None else 0
        return f"{secs // 60:02d}:{secs % 60:02d}"

    def _append_run_log(self, message, advance=False):
        if advance:
            self._run_log_step = int(getattr(self, "_run_log_step", 0)) + 1
        step = int(getattr(self, "_run_log_step", 0))
        total = int(getattr(self, "_run_log_total", 0))
        if total:
            prefix = f"[{self._elapsed_run_log()}] [{step:02d}/{total:02d}]"
        else:
            prefix = f"[{self._elapsed_run_log()}]"
        self.txt_summary.append(f"{prefix} {message}")
        bar = self.txt_summary.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _log_profile_event(self, event):
        if not getattr(rg, "DEBUG_RUN_TIMING_LOG", False):
            return
        label = str(event.get("label") or "")
        skip = {
            "select_items",
            "split_for_diff",
            "subjects_meta",
            "subjects_meta_common",
            "build_yield",
            "build_fail_items",
            "build_issue_summary",
            "build_summary_rows",
            "build_major_fail_subjects",
            "build_cpk",
            "build_cpk_common",
            "build_distributions",
            "build_distributions_common",
            "combined_df_yield",
            "fill_cpk",
            "fill_fail_item",
            "fail_values.title",
            "fail_values.borders",
            "fill_fail_item.style",
            "normalize_sheet_names",
            "zoom_gridlines",
        }
        if label in skip:
            return
        status = event.get("status")
        elapsed = event.get("elapsed")
        error = event.get("error")
        if status == "start":
            return
        elif status == "info":
            msg = event.get("message") or label
            if msg:
                self._append_run_log(str(msg))
        elif status == "done":
            self._append_run_log(f"{label} done: {elapsed:.2f}s" if elapsed is not None
                                 else f"{label} done", advance=True)
        elif status == "error":
            msg = f"{label} ERROR"
            if elapsed is not None:
                msg += f" after {elapsed:.2f}s"
            if error:
                msg += f" - {error}"
            self._append_run_log(msg, advance=True)

    def _estimate_run_log_steps(self, work_group, sheets, raw_data):
        sources = len(work_group.names()) if work_group is not None else 0
        table_sheets = {"summary", "yield", "cpk", "fail_item", "issue_table"}
        selected_tables = [s for s in sheets if s in table_sheets]
        steps = 0
        if raw_data:
            steps += 1  # raw_frames
        steps += 1 + sources  # analysis table builders + fail_detail per source
        steps += 1  # workbook_init
        steps += sum(1 for s in selected_tables if s != "fail_item")
        if "cpk" in selected_tables:
            steps += 3  # fill_cpk expands into four substeps
        if "fail_item" in selected_tables:
            steps += 2 + sources  # top table + FAIL_VALUES + source chunks
        if raw_data:
            steps += sources
        steps += 2  # finalize + save
        if "distribution" in sheets:
            steps += 1
        if "histogram" in sheets:
            steps += 1
        return max(steps, 1)

    def _setup_csv_table(self):
        """list_csv (QTableWidget) 를 '파일 경로 | 확장자' 2열로 구성하고,
        파일 리스트 영역에 한정한 드래그앤드롭(외부 파일)을 활성화한다."""
        t = self.list_csv
        t.setColumnCount(3)
        t.setHorizontalHeaderLabels(["파일 경로", "확장자", ""])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)       # 긴 경로는 가로 스크롤
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # 확장자 좁게(오른쪽)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)             # 행별 ✕ 삭제 버튼(좁게 고정)
        t.setColumnWidth(2, 32)
        hh.setStretchLastSection(False)
        t.setItemDelegateForColumn(0, FilePathDelegate(t))   # 파일명만 파란색
        # 드롭은 리스트 영역에서만 받는다 (메인 창엔 setAcceptDrops 를 걸지 않음).
        t.setTextElideMode(Qt.TextElideMode.ElideNone)
        t.setWordWrap(False)
        t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        t.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        t.verticalHeader().setDefaultSectionSize(20)
        t.setAcceptDrops(True)
        t.viewport().installEventFilter(self)

    # ── 드래그앤드롭 (파일 리스트 영역 한정) ─────────────────────────────────
    def eventFilter(self, obj, event):
        if obj is self.list_csv.viewport():
            etype = event.type()
            if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                if event.mimeData().hasUrls():
                    event.acceptProposedAction()
                    return True
            elif etype == QEvent.Type.Drop:
                self._handle_csv_drop(event)
                return True
        elif obj is getattr(self, "lbl_ai_comment", None):
            # 숨김 스위치: 글자 10번 클릭 → AI Comment 체크박스 활성화(이번 실행 한정).
            # 활성화 후에는 원래 체크박스 글자처럼 클릭이 토글로 동작한다.
            if event.type() == QEvent.Type.MouseButtonPress:
                if self.chk_ai_comment.isEnabled():
                    self.chk_ai_comment.toggle()
                else:
                    self._ai_comment_clicks += 1
                    if self._ai_comment_clicks >= 10:
                        self.chk_ai_comment.setEnabled(True)
                        self.lbl_ai_comment.setStyleSheet("color: #4A3B1A;")
                        self._status("AI Comment 사용 가능")
                return True
        elif obj is getattr(self, "progress_status", None):
            # 진행바가 보이면 하단 dock 을 펴고, 숨겨지면 접는다 (관찰만 — 이벤트 통과).
            dock = getattr(self, "dock_log", None)
            if dock is not None:
                if event.type() == QEvent.Type.Show:
                    dock.show()
                elif event.type() == QEvent.Type.Hide:
                    dock.hide()
        return super().eventFilter(obj, event)

    def _handle_csv_drop(self, event):
        """파일·폴더 혼합 드롭을 모두 받는다.

        폴더는 '폴더 열기' 와 같은 규칙으로 스캔(하위 RT/CT/HT 온도 폴더 인식)하고,
        같이 끌어다 놓은 낱개 파일과 합쳐 한 번에 인테이크한다."""
        dropped = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not dropped:
            return
        event.acceptProposedAction()
        dirs = [p for p in dropped if Path(p).is_dir()]
        files = [p for p in dropped if p not in dirs]
        if dirs:
            self._intake_folders(dirs, extra_paths=files)
        else:
            self._intake(files)   # 기존 인테이크 흐름 재사용

    def _connect_signals(self):
        self.btn_open_local.clicked.connect(self.on_open_local)
        self._build_open_local_menu()
        self.btn_pick_csv.clicked.connect(self.on_browse_d1)
        # 입력 파일: 선택 후 ▲▼ 로 순서 변경 (맨 위 파일이 기준), Clear 로 전체 비우기
        # 개별 파일 삭제는 각 행의 ✕ 버튼(_refill_csv_list)이 담당한다.
        self.btn_csv_up.clicked.connect(lambda: self._move_file(-1))
        self.btn_csv_down.clicked.connect(lambda: self._move_file(1))
        self.btn_csv_clear.clicked.connect(self._clear_files)
        # Start: 파일 전처리 후 설정 팝업(Select Items/Option/색/Auto Upload) 열기
        self.btn_start.clicked.connect(self.on_start)
        self.btn_web_report.clicked.connect(self.on_web_report)
        self.btn_upload_local.clicked.connect(self.on_upload_local)
        # Product Type 선택 변경 시 사용자별 settings.json 에 즉시 저장
        for rb in self._pt_radios.values():
            rb.toggled.connect(self._save_product_type)
            # Temperature 모드는 PMIC / SECURITY 에서만 고를 수 있다.
            rb.toggled.connect(self._sync_temperature_mode)

    # ── 실험: 내장 브라우저 + 메뉴바 + 아이콘 사이드바 ───────────────────────
    def _build_chrome(self):
        """메인 화면을 재구성한다 (실험, .ui 무변경):
        - 중앙 대부분을 서버 리포트 브라우저가 차지
        - 상단 메뉴바(F10) / 왼쪽 아이콘 사이드바로 기존 버튼 액션 이관
        - 입력 컨트롤(Product Type·파일 리스트·저장명)은 File Open 시 뜨는
          별도 창(dock)으로, 기본은 숨김
        - Status/Log 는 하단 창(dock)으로 이동

        PyQtWebEngine 미설치 시 조기 return — 기존 화면/동작 100% 유지."""
        try:
            import embedded_browser
        except ImportError as exc:
            self._status(f"내장 브라우저 비활성 (PyQtWebEngine 필요): {exc}")
            return
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import (
            QDockWidget, QHBoxLayout, QToolBar, QVBoxLayout, QWidget,
        )

        base = SERVER_BASE_URL.rstrip("/")
        home_url = base + "/pe/report/"      # 🏠 버튼·팝업 판정 기준 (종전 그대로)
        landing_url = base + "/pe/"          # 처음 한 번 여는 화면
        # .ui 로 만든 기존 central 은 버리되(참조는 유지해 버튼 위젯 살려둠),
        # 필요한 위젯만 새 dock 컨테이너로 옮긴다.
        self._legacy_central = self.takeCentralWidget()
        # 파일 열기/D1/Start/Web Report/도움말 버튼은 입력 창(패널) 안으로 이관해 다시 보인다.
        # Server Upload 버튼만 화면에서 제거(기능은 on_upload_local 로 보존).
        self.btn_upload_local.setVisible(False)

        # 중앙: 웹 브라우저가 전체를 차지
        self.browser_panel = embedded_browser.BrowserPanel(
            home_url, navigate=True, start_url=landing_url)
        self.setCentralWidget(self.browser_panel)
        # Rawdata(Excel) 편집 중 세션 이탈(다른 페이지 이동)을 확인 다이얼로그로 가로챈다.
        try:
            self.browser_panel.view.page().leave_guard = self._browser_leave_guard
        except Exception:
            pass

        self._build_controls_panel(QWidget, QVBoxLayout, QHBoxLayout)
        self._build_log_dock(QDockWidget, QWidget)
        self._menu_bar_actions()
        self._icon_sidebar(QAction, QToolBar)

    def _build_controls_panel(self, QWidget, QVBoxLayout, QHBoxLayout):
        """'새 리포트' 입력 창 — 기본 숨김, 사이드바 🆕 로 왼쪽에서 슬라이드.
        파일 열기·D1·Product Type·파일 리스트·저장명·분석 모드·Start/Web Report 를 담는다."""
        from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QGroupBox, QLabel,
                                     QRadioButton, QSizePolicy)

        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)

        # 파일 열기 / D1 불러오기
        self.btn_help.setVisible(False)  # 물음표 도움말 버튼 제거 (도움말은 메뉴바로 이동)
        open_row = QHBoxLayout()
        open_row.addWidget(self.btn_open_local, 1)
        open_row.addWidget(self.btn_pick_csv, 1)
        v.addLayout(open_row)

        v.addWidget(self.groupBox_pt)

        file_row = QHBoxLayout()
        # 파일 리스트는 세로로 확장하지 않도록 고정 — 남는 세로 공간이 아래 실행 그리드를
        # 끝까지 밀지 않고, 그리드가 위로 붙은 뒤 그 아래가 빈 공간이 되게 한다.
        self.list_csv.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        file_row.addWidget(self.list_csv)
        move_col = QVBoxLayout()
        move_col.addWidget(self.btn_csv_up)
        move_col.addWidget(self.btn_csv_down)
        move_col.addWidget(self.btn_csv_clear)
        move_col.addStretch(1)
        file_row.addLayout(move_col)
        v.addLayout(file_row)

        name_row = QHBoxLayout()
        name_row.addWidget(self.lbl_outname)
        name_row.addWidget(self.le_outname)
        name_row.addWidget(self.lbl_xlsx_ext)
        v.addLayout(name_row)

        # 실행 영역: Web Report(분석 모드 라디오 포함) 와 Excel Report 를 그리드 2개로 분리.
        # 분석 모드는 Web Report 에만 해당하므로 Web Report 그룹 안에 둔다.
        web_box = QGroupBox("Web Report")
        web_v = QVBoxLayout(web_box)
        web_v.setContentsMargins(8, 6, 8, 6)
        web_v.setSpacing(6)

        # 분석 모드 — 라디오. 파일 개수와 안 맞는 모드는 실행 시 경고.
        mode_row = QHBoxLayout()
        self._mode_radios = {}
        self._mode_group = QButtonGroup(self)
        # Temperature 는 PMIC / SECURITY 제품군 전용(RT/CT/HT 온도 pair 분석) — _sync_temperature_mode 가
        # Product Type 선택에 따라 보이고 숨긴다.
        for key in ("Normal", "Compare", "DUT", "Temperature"):
            rb = QRadioButton(key)
            if key == "Normal":
                rb.setChecked(True)
            self._mode_group.addButton(rb)
            self._mode_radios[key] = rb
            mode_row.addWidget(rb)
        mode_row.addStretch(1)
        web_v.addLayout(mode_row)
        self._sync_temperature_mode()

        # AI Comment — 서버 eval_analyzer 분석 결과를 Issue Table 에 표시할지 여부.
        # 서버 파이프라인 검증 전까지 비활성 노출 — "AI Comment" 글자를 10번 누르면
        # 이번 실행 동안만 활성화된다(숨김 스위치 — eventFilter 의 lbl_ai_comment 분기).
        # disabled 위젯은 마우스 이벤트를 못 받으므로 글자를 별도 라벨로 분리했다.
        # **상태를 settings.json 에 영속하지 않는다** (2026-08-04): 한 번 켠 뒤 저장된
        # True 가 다음 실행에서 "화면은 비활성인데 체크는 켜짐"으로 복원돼, 사용자가
        # 켠 적 없는 세션에도 AI Comment 가 붙었다. 매 실행 꺼진 상태로 시작하고
        # 활성화(10회 클릭) 후 직접 체크한 경우에만 업로드에 실린다.
        self.chk_ai_comment = QCheckBox("")
        self.chk_ai_comment.setEnabled(False)
        self._ai_comment_clicks = 0
        self.lbl_ai_comment = QLabel("AI Comment")
        self.lbl_ai_comment.setStyleSheet("color: #A89C77;")   # 비활성 글자색
        self.lbl_ai_comment.installEventFilter(self)
        ai_row = QHBoxLayout()
        ai_row.setSpacing(6)
        ai_row.addWidget(self.chk_ai_comment)
        ai_row.addWidget(self.lbl_ai_comment)
        ai_row.addStretch(1)
        web_v.addLayout(ai_row)
        web_v.addWidget(self.btn_web_report)

        # Excel Report — 로컬 xlsx 생성/분석 (기존 Start 버튼).
        self.btn_start.setText("Excel Report")
        excel_box = QGroupBox("Excel Report")
        excel_v = QVBoxLayout(excel_box)
        excel_v.setContentsMargins(8, 6, 8, 6)
        excel_v.addStretch(1)
        excel_v.addWidget(self.btn_start)

        # 각 버튼이 그리드 칸 가로를 꽉 채우도록 (.ui 는 Fixed → Expanding 으로 완화)
        for _b in (self.btn_web_report, self.btn_start):
            _b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        # 좌=Web Report / 우=Excel Report (기존 Start·Web Report 위치 스왑)
        run_row = QHBoxLayout()
        run_row.addWidget(web_box, 1)
        run_row.addWidget(excel_box, 1)
        v.addLayout(run_row)
        # 실행 그리드 아래는 빈 공간으로 (그리드를 위로 붙인다).
        v.addStretch(1)

        # 패널 하단 진행바 — dock 진행바(progress_status)가 이 슬라이드 패널에 가려
        # 안 보이므로, 패널 기동 흐름(Web/Excel Report·업로드)의 진행을 여기 미러링한다
        # (_ElapsedProgress(mirror=self.panel_progress)). 평소엔 숨김.
        from PyQt6.QtWidgets import QProgressBar
        self.panel_progress = QProgressBar(container)
        self.panel_progress.setTextVisible(True)
        self.panel_progress.hide()
        # 진행 취소 버튼 — Web Report 흐름이 도는 동안만 보인다(_begin_op_cancel).
        # 클릭은 플래그만 세우고, 각 단계의 wait_for_future(cancelled=)가 받아 중단한다.
        self.btn_panel_cancel = QPushButton("취소", container)
        self.btn_panel_cancel.hide()
        self.btn_panel_cancel.clicked.connect(self._on_op_cancel)
        prog_row = QHBoxLayout()
        prog_row.setSpacing(6)
        prog_row.addWidget(self.panel_progress, 1)
        prog_row.addWidget(self.btn_panel_cancel, 0)
        v.addLayout(prog_row)

        self.slide_controls = SlideInPanel(
            self.browser_panel, container, "입력 파일 / 설정", width=620)

    def _build_log_dock(self, QDockWidget, QWidget):
        """하단 창(dock): 진행바만 표시(경과시간·상태 메시지). 제목표시줄 없음.
        Log(txt_summary)·Status 라벨은 화면에서 제거하되 위젯은 숨겨 코드 참조를 유지한다."""
        from PyQt6.QtWidgets import QHBoxLayout

        self.txt_summary.hide()
        self.lbl_progress_status.hide()

        from PyQt6.QtWidgets import QPushButton

        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(8, 2, 8, 2)
        row.addWidget(self.progress_status, 1, Qt.AlignmentFlag.AlignVCenter)
        # 취소 버튼 — 취소를 지원하는 작업(현재 업데이트 다운로드)만 필요할 때 보인다.
        self.btn_progress_cancel = QPushButton("취소")
        self.btn_progress_cancel.hide()
        row.addWidget(self.btn_progress_cancel, 0, Qt.AlignmentFlag.AlignVCenter)

        dock = QDockWidget(self)
        dock.setTitleBarWidget(QWidget())   # 제목표시줄 제거
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        dock.setWidget(container)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.dock_log = dock
        self.resizeDocks([dock], [40], Qt.Orientation.Vertical)
        # 진행바가 숨겨진 평소엔 dock 을 접어 하단 흰 빈칸을 없앤다.
        # progress_status 의 Show/Hide 이벤트를 받아 dock 가시성을 함께 토글한다.
        self.progress_status.installEventFilter(self)
        dock.hide()

    def _show_controls(self):
        """입력 창을 슬라이드로 띄운다 (File Open 시 호출)."""
        panel = getattr(self, "slide_controls", None)
        if panel is not None:
            panel.show_animated()

    def _toggle_controls(self):
        """입력/설정 창을 접었다 폈다 토글 (사이드바 🆕)."""
        panel = getattr(self, "slide_controls", None)
        if panel is None:
            return
        if panel.isVisible():
            panel.hide_animated()
        else:
            panel.show_animated()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        panel = getattr(self, "slide_controls", None)
        if panel is not None:
            panel.reposition()

    def moveEvent(self, event):
        super().moveEvent(event)
        panel = getattr(self, "slide_controls", None)
        if panel is not None:
            panel.reposition()

    def _act_open_local(self):
        self._show_controls()
        self.on_open_local()

    def _act_browse_d1(self):
        self._show_controls()
        self.on_browse_d1()

    def _menu_bar_actions(self):
        """상단 메뉴바 구성 — 기존 슬롯을 그대로 호출 (로직 복제 없음).

        새 작업을 시작하는 항목은 _busy_actions 에 모아 _set_busy 가 함께 잠근다."""
        mb = self.menuBar()

        m_file = mb.addMenu("파일(&F)")
        self._busy_actions += [
            m_file.addAction("LOCAL FILE OPEN", self._act_open_local),
            m_file.addAction("Dolphin (D1)에서 불러오기", self._act_browse_d1),
        ]

        m_run = mb.addMenu("실행(&R)")
        m_run.addAction("새 리포트 생성", self._show_controls)
        self._busy_actions += [
            m_run.addAction("Rawdata 편집", self.on_rawdata_edit),
            m_run.addAction("Excel Download", self.on_excel_download),
            m_run.addAction("DB Input (선례 CSV 적재)", self.on_db_input),
        ]

        m_view = mb.addMenu("보기(&V)")
        m_view.addAction("입력 / 설정 창 열기", self._show_controls)
        m_view.addAction("입력 / 설정 창 닫기",
                         lambda: self.slide_controls.hide_animated())
        act_l = self.dock_log.toggleViewAction()
        act_l.setText("진행 상태 창")
        m_view.addAction(act_l)
        m_view.addSeparator()
        m_view.addAction("검색결과 홈", lambda: self.browser_panel.go_home())
        m_view.addAction("새로고침", lambda: self.browser_panel.view.reload())

        m_settings = mb.addMenu("설정(&S)")
        m_settings.addAction("Options...", self.on_options)

        m_help = mb.addMenu("도움말(&H)")
        m_help.addAction("HONEY 도움말", self.on_help_honey)
        m_help.addAction("VOC", self.on_voc)

    def _icon_sidebar(self, QAction, QToolBar):
        """왼쪽 아이콘 사이드바 — 자주 쓰는 액션(이모지 아이콘 + 밑 라벨 + tooltip)."""
        from PyQt6.QtCore import QSize
        from PyQt6.QtGui import QIcon
        tb = QToolBar("Quick")
        tb.setMovable(False)
        tb.setOrientation(Qt.Orientation.Vertical)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)  # 아이콘 위·라벨 밑
        tb.setIconSize(QSize(28, 28))
        tb.setStyleSheet("""
            QToolBar {
                background: #1f2937;
                border: none;
                padding: 6px 2px;
                spacing: 4px;
            }
            QToolBar QToolButton {
                color: #e5e7eb;
                font-size: 8pt;
                min-width: 60px;
                min-height: 52px;
                border-radius: 8px;
            }
            QToolBar QToolButton:hover { background: #374151; }
            QToolBar QToolButton:pressed { background: #4b5563; }
        """)
        quick = [
            ("🏠", "Home",         "검색결과 홈 (기본 검색결과 목록)",
             lambda: self.browser_panel.go_home()),
            ("🆕", "New Report",   "새 리포트 (입력 / 설정 창 접기·펴기)", self._toggle_controls),
            ("📝", "Rawdata edit", "Rawdata 수정 (Excel)",             self.on_rawdata_edit),
            (self._excel_icon(), "Excel Down", "Excel Download",       self.on_excel_download),
            ("📤", "Excel Upload", "로컬 xlsx 업로드 (Raw Data → web report 세션)", self.on_upload_local),
            ("⚙️", "Options",      "옵션 (색·기본값 설정)",             self.on_options),
        ]
        # 새 작업을 시작하는 액션만 busy 중 잠근다 (Home/New Report/Options 는 잠그지 않음 —
        # 진행 중에도 화면 이동·설정은 자유롭게).
        busy_labels = {"Rawdata edit", "Excel Down", "Excel Upload"}
        for icon, label, tip, slot in quick:
            qicon = icon if isinstance(icon, QIcon) else self._emoji_icon(icon)
            act = QAction(qicon, label, self)
            act.setToolTip(tip)
            act.triggered.connect(slot)
            tb.addAction(act)
            if label in busy_labels:
                self._busy_actions.append(act)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, tb)

    @staticmethod
    def _emoji_icon(emoji, px=28):
        """이모지 문자를 투명 배경 QPixmap 에 그려 QIcon 으로 반환."""
        from PyQt6.QtGui import QPixmap, QPainter, QIcon
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        f = p.font()
        f.setPointSize(int(px * 0.7))
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, emoji)
        p.end()
        return QIcon(pm)

    @staticmethod
    def _honey_icon(px=64):
        """Honey 이미지(Honey_img.png) 아이콘 — 실행/창 아이콘용.

        repo 루트(dev)/_MEIPASS(frozen)의 Honey_img.png 를 우선 사용하고, 파일이
        없거나 로드 실패 시 벡터 꿀단지로 폴백한다(어떤 환경에서도 아이콘이 보이도록).
        """
        from PyQt6.QtGui import QPixmap, QPainter, QIcon, QColor
        from PyQt6.QtCore import QRectF
        for base in (_BASE_DIR, _REPO_ROOT):
            path = os.path.join(base, "Honey_img.png")
            if os.path.exists(path):
                icon = QIcon(path)
                if not icon.isNull():
                    return icon
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        u = px / 64.0
        p.setBrush(QColor("#C6820E"))                                   # 뚜껑
        p.drawRoundedRect(QRectF(15 * u, 7 * u, 34 * u, 11 * u), 4 * u, 4 * u)
        p.setBrush(QColor("#F2A81C"))                                   # 항아리 본체
        p.drawRoundedRect(QRectF(11 * u, 16 * u, 42 * u, 41 * u), 13 * u, 13 * u)
        p.setBrush(QColor("#FFF3D0"))                                   # 라벨 밴드
        p.drawRoundedRect(QRectF(16 * u, 30 * u, 32 * u, 16 * u), 3 * u, 3 * u)
        p.setBrush(QColor("#E38E0C"))                                   # 꿀방울
        p.drawEllipse(QRectF(28 * u, 32 * u, 8 * u, 11 * u))
        p.end()
        return QIcon(pm)

    @staticmethod
    def _excel_icon(px=28):
        """Excel 로고풍 아이콘 — 초록 라운드 사각형에 흰색 'X'."""
        from PyQt6.QtGui import QPixmap, QPainter, QIcon, QColor, QFont
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#16a34a"))
        p.drawRoundedRect(2, 1, px - 4, px - 2, 4, 4)
        f = QFont(p.font())
        f.setPointSize(int(px * 0.5))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "X")
        p.end()
        return QIcon(pm)

    def _disable_engine(self):
        # 분석 관련 기능만 비활성. 로컬 파일 직접 업로드는 엔진 없이도 동작하므로 유지.
        for name in ("btn_open_local", "btn_pick_csv", "btn_start", "btn_web_report"):
            getattr(self, name).setEnabled(False)
        self.txt_summary.setPlainText(
            "report_generator 모듈을 불러오지 못했습니다 — "
            f"{_RG_IMPORT_ERROR}\n분석/생성에는 pandas / numpy / xlwings + MS Excel 이 필요합니다."
            "\n(로컬 파일 직접 업로드는 가능합니다.)"
        )

    def _status(self, msg):
        self.status.showMessage(msg)

    # ── Rawdata 수정: 내장 브라우저에 열린 세션을 Excel 로 편집 ────────────────
    def _current_session_id(self):
        """내장 브라우저 주소에서 web_report 세션 id 추출 (없으면 "")."""
        panel = getattr(self, "browser_panel", None)
        if panel is None:
            return ""
        try:
            url = panel.view.url().toString()
        except Exception:
            return ""
        import re
        m = re.search(r"/pe/report/view/([A-Za-z0-9_-]+)", url)
        return m.group(1) if m else ""

    def _map_selection_chips(self, timeout_ms=3000):
        """내장 브라우저에 열린 세션의 Map Analysis 선택 좌표 (없거나 실패하면 []).

        선택 상태는 페이지 메모리에만 있고 서버·URL 어디에도 저장되지 않아(map_select.js
        mapSelChips) 화면에 물어보는 수밖에 없다 — 넘겨줄 필드는 honeyMapSelSnapshot
        하나로 고정돼 있다. 응답이 비동기라 중첩 이벤트 루프로 기다리고, 그 함수가 없는
        페이지(구 서버·검색결과 화면)나 타임아웃은 [] 로 폴백해 **기존 동작(강조 없는
        저장)** 그대로 진행한다.
        """
        panel = getattr(self, "browser_panel", None)
        if panel is None:
            return []
        from PyQt6.QtCore import QEventLoop, QTimer
        box = {"chips": []}
        loop = QEventLoop()

        def _done(value):
            if isinstance(value, list):
                box["chips"] = value
            loop.quit()

        try:
            panel.view.page().runJavaScript(
                "(typeof honeyMapSelSnapshot === 'function') ? honeyMapSelSnapshot() : []",
                _done)
        except Exception:
            return []
        QTimer.singleShot(timeout_ms, loop.quit)   # 응답이 없어도 다이얼로그가 굳지 않게
        loop.exec()
        return box["chips"]

    def on_rawdata_edit(self):
        """Rawdata 허브를 연다 — 현재 상태 / Item Select / Outlier / 빠른 수정 / Excel.

        Excel 왕복만 원본을 실제로 바꾼다. 나머지는 원본을 그대로 두고 조회 시점에만
        적용되는 전처리(항목 제외·outlier·셀 패치·조건 규칙)라 언제든 되돌릴 수 있다."""
        sid = self._current_session_id()
        if not sid:
            QMessageBox.information(
                self, "Rawdata 수정",
                "먼저 세션(검색결과에서 리포트)을 연 뒤 눌러 주세요.")
            return
        worker = getattr(self, "_excel_worker", None)
        if worker is not None and worker.isRunning():
            QMessageBox.information(self, "Rawdata 수정", "이미 Excel 편집이 진행 중입니다.")
            return

        from honey_ui.rawdata_hub_dialog import (ACTION_ADD_ITEM, ACTION_EXCEL,
                                                 ACTION_QUICK, RawdataHubDialog)
        hub = RawdataHubDialog(self, sid, SERVER_BASE_URL)
        accepted = hub.exec() == QDialog.DialogCode.Accepted
        changed = hub.changed
        if accepted and hub.action == ACTION_QUICK:
            changed = self._run_quick_edit(sid) or changed
        if accepted and hub.action == ACTION_ADD_ITEM:
            # 신규 수식 item 추가 — Excel 왕복과 같은 워커 필드를 쓴다. 그래야 위 중복 실행
            # 가드와 아래 이탈 취소 가드(_excel_edit_running)가 그대로 적용된다.
            from excel_edit.worker import AddItemWorker
            self._excel_worker = AddItemWorker(sid, SERVER_BASE_URL, hub.add_item_spec, self)
            w = self._excel_worker
            w.status.connect(self._on_excel_edit_status)
            w.confirm_request.connect(self._on_excel_edit_confirm)
            w.done.connect(self._on_excel_edit_done)
            w.failed.connect(self._on_excel_edit_failed)
            name = (hub.add_item_spec or {}).get("name") or ""
            self._append_run_log(f"신규 item 추가 시작 (session {sid}) - '{name}'")
            w.start()
            self._set_busy(True)
            return
        if changed:
            # 전처리 옵션이 바뀌었으면 현재 보고 있는 리포트를 다시 그린다.
            self._append_run_log("[Rawdata] 전처리 옵션 저장 — 페이지 새로고침.")
            try:
                self.browser_panel.view.reload()
            except Exception:
                pass
        if not (accepted and hub.action == ACTION_EXCEL):
            return

        from excel_edit.worker import ExcelEditWorker
        # hub.excel_indices: 허브에서 체크한 source 만 Excel 로 연다 (None = 전체).
        self._excel_worker = ExcelEditWorker(sid, SERVER_BASE_URL, self,
                                             indices=hub.excel_indices)
        w = self._excel_worker
        w.status.connect(self._on_excel_edit_status)
        w.confirm_request.connect(self._on_excel_edit_confirm)
        w.done.connect(self._on_excel_edit_done)
        w.failed.connect(self._on_excel_edit_failed)
        self._append_run_log(f"Rawdata 수정 시작 (session {sid}) — Excel 을 엽니다...")
        w.start()
        # Excel 편집이 끝날 때까지(done/failed) 다른 Excel COM 작업·새 입력을 막는다.
        self._set_busy(True)

    def _run_quick_edit(self, sid) -> bool:
        """빠른 수정 다이얼로그 — 저장했으면 True (호출부가 브라우저 새로고침).

        Excel 워커와 달리 이 다이얼로그는 원본을 바꾸지 않고 전처리 spec 만 저장하므로
        앱을 busy 로 잠그지 않는다 (다이얼로그가 모달이라 중복 실행도 없다). 무거운
        다운로드·디코드·미리보기 계산은 다이얼로그 내부에서 스레드로 돌린다."""
        from honey_ui.rawdata_quick_dialog import RawdataQuickDialog

        self._append_run_log(f"[Rawdata] 빠른 수정 열기 (session {sid})")
        dialog = RawdataQuickDialog(self, sid, SERVER_BASE_URL)
        dialog.exec()
        if dialog.changed:
            self._append_run_log("[Rawdata] 빠른 수정 저장 — 원본은 그대로입니다.")
        return bool(dialog.changed)

    def _on_excel_edit_status(self, state, message):
        self._status(message)
        self._append_run_log(f"[Rawdata] {message}")

    def _on_excel_edit_confirm(self, payload):
        """워커의 반영 확인 요청 — 메인스레드에서 변경 요약을 보여주고 응답을 돌려준다.

        내용은 excel_session 이 만든 변경 요약(셀 diff·자동 교정·경고·시트 삭제)이다.
        수정이 많으면 QMessageBox 는 창이 화면을 넘어가 버튼이 안 보였다 — 스크롤 가능한
        전용 확인창을 쓴다. 기본 버튼은 취소 — Excel 편집은 서버에서 되돌릴 수 없다."""
        from honey_ui.change_review_dialog import ask_change_review
        accepted = ask_change_review(self, payload)
        self._append_run_log(
            "[Rawdata] 반영 " + ("승인 — 서버에 저장합니다." if accepted else "거부 — 저장하지 않습니다."))
        worker = getattr(self, "_excel_worker", None)
        if worker is not None:
            worker.answer_confirm(accepted)

    def _on_excel_edit_done(self, changed, message):
        self._set_busy(False)
        if changed:
            self._status("Rawdata 수정 완료 — 페이지 새로고침")
            self._append_run_log("[Rawdata] 완료 — 서버 반영됨. 페이지 새로고침.")
            try:
                self.browser_panel.view.reload()
            except Exception:
                pass
        elif message == "반영 미승인":
            self._status("Rawdata 수정 취소됨 — 반영 미승인")
            self._append_run_log("[Rawdata] 변경 반영을 승인하지 않아 저장하지 않았습니다.")
        elif message == "취소됨":
            self._status("Rawdata 수정 취소됨")
            self._append_run_log("[Rawdata] 취소됨 — Excel 을 닫고 편집을 중단했습니다.")
        else:
            self._status("Rawdata 변경 없음")
            self._append_run_log("[Rawdata] 변경 없음 — 업로드 건너뜀.")

    # ── Rawdata 편집 중 이탈 가드 (브라우저 네비게이션 / 앱 종료 공용) ──────────
    def _excel_edit_running(self):
        worker = getattr(self, "_excel_worker", None)
        return worker is not None and worker.isRunning()

    def _confirm_cancel_edit(self):
        """Rawdata 편집 중 이탈 확인. 예=취소하고 나감(True)+Excel 종료, 아니오=머무름(False)."""
        reply = QMessageBox.question(
            self, "Rawdata 수정",
            "rawdata 수정이 완료 되지 않았습니다.\n수정을 취소 하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return False
        worker = getattr(self, "_excel_worker", None)
        if worker is not None and worker.isRunning():
            worker.cancel()   # 워커가 다음 폴링에서 Excel 강제 종료 후 done 시그널
            self._append_run_log("[Rawdata] 사용자 취소 — Excel 을 닫고 편집을 중단합니다.")
        return True

    def _browser_leave_guard(self, url):
        """내장 브라우저 네비게이션 가드. Rawdata 편집 중이면 목적지와 무관하게 확인.
        반환 True=이동 허용, False=차단(현재 세션 유지).

        먼저 'Honey 액션 URL'(웹 버튼 → 클라 기능 호출)인지 본다 — 맞으면 페이지를 옮기지
        않고 해당 다이얼로그만 띄운다."""
        if self._handle_honey_action(url):
            return False
        if not self._excel_edit_running():
            return True
        return self._confirm_cancel_edit()

    # ── 웹 → 클라 액션 브리지 ─────────────────────────────────────────────────
    # 세션 페이지의 ✏️ 버튼은 /pe/report/honey/session_meta/<sid> 로 '이동'을 시도한다.
    # 여기서 그 이동을 가로채 취소하고(가드가 False 반환) 편집창을 대신 띄운다 — 별도
    # 통신 채널(커스텀 스킴·웹소켓)을 만들지 않으려는 선택이다. 커스텀 스킴(honey://)은
    # QtWebEngine 버전에 따라 이 콜백까지 오지 않을 수 있어 평범한 http 경로를 쓴다.
    _HONEY_ACTION_RE = re.compile(r"^/pe/report/honey/(?P<action>[a-z_]+)/(?P<sid>[A-Za-z0-9_-]+)$")

    def _handle_honey_action(self, url):
        """액션 URL 이면 처리를 예약하고 True. 아니면 False(평범한 네비게이션)."""
        try:
            m = self._HONEY_ACTION_RE.match(url.path())
        except Exception:
            return False
        if not m or m.group("action") != "session_meta":
            return False
        sid = m.group("sid")
        # 다이얼로그를 이 콜백 안에서 바로 열지 않는다 — Chromium 네비게이션 콜백 안에서
        # 중첩 이벤트 루프(exec)를 돌리면 안 된다. 콜백이 끝난 뒤 실행하도록 미룬다.
        QTimer.singleShot(0, lambda: self.on_session_meta_edit(sid))
        return True

    def on_session_meta_edit(self, session_id):
        """세션 정보(이름·Family·Product·LOT·Process) 수정 — 업로드 다이얼로그 재사용.

        Product Type 은 바꾸지 않는다(세션 값 고정). 저장하면 서버가 product_info.db 를 다시
        lookup 해 기준정보(WF Size/Gross Die/PKG/...)까지 갱신하므로 페이지를 새로고침한다."""
        # 신원 헤더 규칙(HoneyUser/<percent-encoded 계정>)은 Rawdata 허브와 같은 것을 쓴다.
        from honey_ui.dialogs import SessionMetaDialog
        from honey_ui.rawdata_hub_dialog import _headers as _honey_headers

        base = SERVER_BASE_URL.rstrip("/")
        try:
            r = requests.get(f"{base}/pe/report/session/{session_id}",
                             headers=_honey_headers(), timeout=(10, 30))
            r.raise_for_status()
            session = r.json() or {}
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "세션 정보 수정",
                                f"세션 정보를 가져오지 못했습니다.\n{exc}")
            return

        dlg = SessionMetaDialog(self, session)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        payload = {k: v[k] for k in
                   ("file_name", "family_product", "product", "lot_id", "process", "step")}
        try:
            r = requests.patch(
                f"{base}/pe/report/session/{session_id}/meta", json=payload,
                headers=_honey_headers({"X-Honey-Agent": "1"}), timeout=(10, 30))
            if r.status_code != 200:
                detail = ""
                try:
                    detail = (r.json() or {}).get("error") or ""
                except Exception:
                    detail = r.text[:200]
                raise RuntimeError(f"({r.status_code}) {detail}")
        except Exception as exc:  # noqa: BLE001
            self._append_run_log(f"[세션정보] 저장 실패: {exc}")
            QMessageBox.warning(self, "세션 정보 수정", f"저장하지 못했습니다.\n{exc}")
            return

        self._status("세션 정보 수정 완료 — 페이지 새로고침")
        self._append_run_log(
            f"[세션정보] 저장 완료 (session {session_id}) — {payload['product']} / "
            f"{payload['lot_id']}")
        try:
            self.browser_panel.view.reload()
        except Exception:
            pass

    def shutdown_browser(self):
        """종료 직전 내장 브라우저(메인+팝업)를 정리한다. 여러 번 불려도 1회만 동작.

        QApplication 이 QWebEngineView 보다 먼저 파괴되면 Chromium 정리가 뒤늦게 돌아
        access violation 이 난다. 종료 경로 3개(closeEvent / aboutToQuit / exec 반환)가
        모두 이 함수를 거치게 해 그 상황 자체를 없앤다.

        정리 실패가 종료를 막아선 안 되므로 전부 best-effort 다.
        """
        if getattr(self, "_browser_shutdown", False):
            return
        self._browser_shutdown = True
        try:
            import embedded_browser
        except ImportError:
            return   # PyQtWebEngine 미설치 = 정리할 브라우저도 없다
        try:
            embedded_browser.shutdown_all()          # 팝업 창 먼저
        except Exception:   # noqa: BLE001
            pass
        panel = getattr(self, "browser_panel", None)
        if panel is not None:
            try:
                embedded_browser.shutdown_panel(panel)
            except Exception:   # noqa: BLE001
                pass

    def closeEvent(self, event):
        if self._excel_edit_running():
            if not self._confirm_cancel_edit():
                event.ignore()
                return
            # 취소 승인됨 → 워커가 Excel 닫고 종료하길 잠시 대기 (QThread 파괴 경고 방지).
            worker = getattr(self, "_excel_worker", None)
            if worker is not None:
                worker.wait(6000)
        # 닫기가 확정된 뒤에만 정리한다 — 위 ignore 경로에서 브라우저를 죽이면
        # 사용자가 취소했는데 화면이 먹통이 된다.
        self.shutdown_browser()
        super().closeEvent(event)

    def _on_excel_edit_failed(self, message):
        self._set_busy(False)
        self._status(f"Rawdata 수정 실패: {message}")
        self._append_run_log(f"[Rawdata] 실패: {message}")
        QMessageBox.warning(self, "Rawdata 수정 실패", message)

    # ── Excel Download: 열린 세션의 web report 를 xlsx 로 저장 ────────────────
    def on_excel_download(self):
        """현재 열린 web_report 세션을 xlsx 로 저장 (시트+차트 PNG, 클라이언트 생성)."""
        sid = self._current_session_id()
        if not sid:
            QMessageBox.information(
                self, "Excel Download",
                "먼저 세션(검색결과에서 리포트)을 연 뒤 눌러 주세요.")
            return
        worker = getattr(self, "_excel_dl_worker", None)
        if worker is not None and worker.isRunning():
            QMessageBox.information(self, "Excel Download", "이미 Excel Download 가 진행 중입니다.")
            return

        # 산포(Distribution/Histogram)를 bin1(양품·규격내) 기준으로 그릴지 선택.
        # 브라우저 토글 상태를 클라가 알 수 없어 여기서 고른다. 체크 시 CDF/히스토그램만
        # 양품(BIN==1) & 규격 이내 die 로 그리고, 나머지 시트는 전체 die 기준 그대로.
        # Map Analysis 선택 좌표(강조)는 브라우저 메모리에만 있어 여기서 읽어 워커에 넘긴다.
        # 없으면 빈 목록 → 강조 없이 기존과 동일하게 저장한다.
        chips = self._map_selection_chips()

        # 옵션은 산포 기준 하나뿐 — 기입 방식 선택(구 "새 방식으로 만들기")은 없앴고
        # 항상 Excel 없이 만드는 방식으로 저장한다(실패 시에만 내부적으로 기존 방식 재시도).
        from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QLabel,
                                     QVBoxLayout)
        dlg = QDialog(self)
        dlg.setWindowTitle("Excel Download")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "web report 를 Excel 로 저장합니다."
            + (f"\n\nMap Analysis 에서 선택한 좌표 {len(chips)}개가 "
               "Map·Distribution 차트에 화면과 같은 색으로 강조됩니다." if chips else "")))
        bin1_cb = QCheckBox("산포(Distribution·Histogram)를 Bin1(양품·규격내) 기준으로 그리기")
        lay.addWidget(bin1_cb)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        bin1 = bin1_cb.isChecked()

        # 여기서부터 워커 완료(_on_excel_dl_done/_failed)까지 새 작업 진입점을 잠근다.
        self._set_busy(True)

        # 세션 메타 조회는 네트워크 GET — 메인 스레드에서 부르면 서버가 느릴 때 창이
        # 굳는다. 다른 무거운 작업과 같은 패턴(스레드 + wait_for_future 폴링)으로 뺀다.
        from excel_download._fetch import fetch_session_meta
        meta_progress = _ElapsedProgress(
            self.progress_status, "세션 정보 조회 중...", self._status,
            busy=True, minimum=0, maximum=0)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                meta = _wait_for_future(
                    ex.submit(fetch_session_meta, SERVER_BASE_URL, sid), meta_progress)
        except Exception as exc:
            meta_progress.fail(f"실패: 세션 정보 조회 - {exc}")
            _show_exc(self, "Excel Download 실패", exc,
                      prefix="세션 정보를 가져오지 못했습니다. 네트워크와 서버 상태를 확인해 주세요.")
            self._status("세션 정보 조회 실패")
            self._set_busy(False)
            return
        meta_progress.success("세션 정보 조회 완료", hide_ms=1000)
        base = "_".join(
            p for p in (str(meta.get(k) or "").strip() for k in ("product", "lot_id"))
            if p) or "webreport"
        default_path = os.path.join(os.path.expanduser("~"), "Documents",
                                    f"{base}_{sid}{'_bin1' if bin1 else ''}.xlsx")
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Excel 저장", default_path, "Excel (*.xlsx)")
        if not out_path:
            self._set_busy(False)
            return

        from excel_download.worker import ExcelDownloadWorker
        self._excel_dl_worker = ExcelDownloadWorker(sid, SERVER_BASE_URL, out_path, bin1,
                                                    self, chips=chips)
        w = self._excel_dl_worker
        w.status.connect(self._on_excel_dl_status)
        w.progress.connect(self._on_excel_dl_progress)
        w.done.connect(self._on_excel_dl_done)
        w.failed.connect(self._on_excel_dl_failed)
        # 진행바(하단 dock) — 단계별 가중치로 0~100% 를 계산해 실제로 채워지게 한다.
        # 차트 렌더처럼 오래 걸리는 구간은 "34/120" 같은 진행 분수까지 문구에 실린다.
        self._excel_dl_progress = _ElapsedProgress(
            self.progress_status, "Excel Download 준비 중...", self._status,
            busy=True, minimum=0, maximum=100)
        self._excel_dl_timer = QTimer(self)
        self._excel_dl_timer.timeout.connect(
            lambda: self._excel_dl_progress.update())
        self._excel_dl_timer.start(500)
        self._append_run_log(f"Excel Download 시작 (session {sid}) → {out_path}")
        w.start()

    def _stop_excel_dl_timer(self):
        timer = getattr(self, "_excel_dl_timer", None)
        if timer is not None:
            timer.stop()
            self._excel_dl_timer = None

    def _on_excel_dl_status(self, state, message):
        self._status(message)
        self._append_run_log(f"[ExcelDL] {message}")

    def _on_excel_dl_progress(self, percent, message):
        """하단 진행바 — 단계 문구 + 진행 분수 + %. 만드는 동안 화면이 멈춘 듯 보이지 않게."""
        progress = getattr(self, "_excel_dl_progress", None)
        if progress is not None:
            progress.set(label=f"{message} · {percent}%", value=int(percent))

    def _on_excel_dl_done(self, out_path, elapsed):
        self._set_busy(False)
        self._stop_excel_dl_timer()
        result = getattr(getattr(self, "_excel_dl_worker", None), "result", {}) or {}
        # 기입 방식은 사용자가 고르지 않는다 — 기본 경로가 실패해 Excel(COM) 로 대체
        # 생성된 예외적인 경우에만 그 사실을 덧붙인다.
        fallback = " · 기존 Excel 방식으로 대체 생성" if result.get("engine") != "xlsxwriter" else ""
        warnings = result.get("warnings") or []
        progress = getattr(self, "_excel_dl_progress", None)
        if progress is not None:
            progress.success(f"완료: Excel Download ({elapsed:.1f}s{fallback})")
        self._status(f"Excel Download 완료 ({elapsed:.1f}s{fallback})")
        self._append_run_log(f"[ExcelDL] 완료 ({elapsed:.1f}s{fallback}): {out_path}")
        text = f"저장 완료 ({elapsed:.1f}초{fallback})\n{out_path}"
        if warnings:
            # 일부만 실패한 경우 — 파일은 만들어졌고 무엇이 빠졌는지는 아래 목록과 실행 로그에 남는다.
            for w in warnings:
                self._append_run_log(f"[ExcelDL] 경고: {w}")
            preview = "\n".join(f"· {w}" for w in warnings[:5])
            if len(warnings) > 5:
                preview += f"\n· 외 {len(warnings) - 5}건"
            text += (f"\n\n경고 {len(warnings)}건 — 일부 내용이 빠졌을 수 있습니다"
                     f"(전체 내용은 실행 로그):\n{preview}")
        QMessageBox.information(self, "Excel Download", text)

    def _on_excel_dl_failed(self, message):
        self._set_busy(False)
        self._stop_excel_dl_timer()
        progress = getattr(self, "_excel_dl_progress", None)
        if progress is not None:
            progress.fail(f"실패: Excel Download - {message}")
        self._status(f"Excel Download 실패: {message}")
        self._append_run_log(f"[ExcelDL] 실패: {message}")
        QMessageBox.warning(self, "Excel Download 실패", message)

    # ── DB Input: 선례(precedent) CSV → 서버 eval DB ────────────────────────
    # 서버 라우트와 같은 상한 (POST /pe/report/api/eval/labels_import).
    _DB_INPUT_MAX_BYTES = 5 * 1024 * 1024

    def on_db_input(self):
        """CSV 선택 → 서버 검증(dry-run) → 미리보기 확인 → 확정 적재.

        적재는 **서버가 자기 eval DB 에 수행**한다 (Honey.exe 는 eval_analyzer 를 담지
        않고 eval DB 는 서버 파일이다) — 끝나면 관리자 Eval DB 탭에 바로 보인다.
        파일은 여기서 한 번만 읽고 그 바이트를 검증·확정 두 요청에 그대로 보낸다
        (중간에 파일이 바뀌어 미리보기와 다른 것이 적재되는 일이 없다).
        """
        from honey_ui.db_input_preview_dialog import ask_db_input_confirm

        start_dir = app_settings.get_setting("db_input_last_dir", "") or os.path.join(
            os.path.expanduser("~"), "Documents")
        path, _ = QFileDialog.getOpenFileName(
            self, "선례 CSV 선택 (Product type, Family Product, unit, Item, comment)",
            start_dir, "CSV (*.csv);;모든 파일 (*.*)")
        if not path:
            return
        app_settings.set_setting("db_input_last_dir", os.path.dirname(path))
        name = os.path.basename(path)
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            _show_exc(self, "DB Input", exc, prefix="CSV 파일을 읽지 못했습니다.")
            return
        if not data:
            _show_error(self, "DB Input", "빈 CSV 파일입니다.")
            return
        if len(data) > self._DB_INPUT_MAX_BYTES:
            _show_error(self, "DB Input", "CSV 가 너무 큽니다 (최대 5MB).")
            return

        self._set_busy(True)
        try:
            self._append_run_log(f"[DB Input] 검증 요청: {name} ({len(data):,} bytes)")
            checked = self._db_input_call(data, name, "validate", "선례 CSV 검증 중...")
            if not ask_db_input_confirm(self, checked):
                self._append_run_log("[DB Input] 취소 — 적재하지 않았습니다.")
                self._status("DB Input 취소")
                return
            done = self._db_input_call(data, name, "commit", "eval DB 적재 중...")
            if not done.get("ok"):
                # 확정 직전 서버 재검증에서 걸렸다 — 같은 다이얼로그로 이유를 보여준다.
                self._append_run_log("[DB Input] 적재 직전 검증 실패 — 적재하지 않았습니다.")
                ask_db_input_confirm(self, done)
                return
            groups = ", ".join(
                f"{g.get('product_type')}_{g.get('family_product')} {g.get('rows')}건"
                for g in done.get("groups") or [])
            self._append_run_log(f"[DB Input] 적재 완료: {done.get('rows', 0)}행 — {groups}")
            QMessageBox.information(
                self, "DB Input",
                f"선례 {int(done.get('rows') or 0):,}건을 서버 eval DB 에 적재했습니다.\n"
                f"{groups}")
        except Exception as exc:  # noqa: BLE001
            self._append_run_log(f"[DB Input] 실패: {exc}")
            _show_exc(self, "DB Input 실패", exc,
                      prefix="서버에 적재하지 못했습니다. 네트워크와 서버 상태를 확인해 주세요.")
        finally:
            self._set_busy(False)

    def _db_input_call(self, data, file_name, mode, label):
        """DB Input 서버 호출 1회 — 짧은 네트워크 작업 공용 패턴(스레드 + 경과시간 진행바).

        mirror 는 필수다 — 슬라이드인 입력 패널이 dock 진행바를 가린다.
        """
        from transport.eval_input import post_labels_csv
        progress = _ElapsedProgress(
            self.progress_status, label, self._status, busy=True, minimum=0, maximum=0,
            mirror=getattr(self, "panel_progress", None))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                result = _wait_for_future(
                    ex.submit(post_labels_csv, data, file_name, mode), progress)
        except Exception:
            progress.fail(f"실패: {label.rstrip('. ')}")
            raise
        progress.success(f"{label.rstrip('. ')} 완료", hide_ms=1500)
        return result

    # ── 입력 선택: 로컬 파일 열기 / d1_storage 검색 ─────────────────────────
    def _build_open_local_menu(self):
        """LOCAL FILE OPEN 우측 화살표 메뉴 — 파일 열기 / 폴더 열기.

        버튼 본체 클릭은 종전대로 파일 열기다(MenuButtonPopup 이라 메뉴는 화살표를
        눌러야 열린다) — 기존 사용자의 클릭 동작이 바뀌지 않는다."""
        menu = QMenu(self.btn_open_local)
        menu.addAction("파일 열기…", self.on_open_local)
        menu.addAction("폴더 열기…", self.on_open_folder)
        self.btn_open_local.setMenu(menu)
        self.btn_open_local.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        # QToolButton 기본은 아이콘 전용이라 이걸 안 하면 글자가 사라진다.
        self.btn_open_local.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

    def on_open_local(self):
        # 현재 윈도우(네이티브) 파일 열기 대화상자
        paths, _ = QFileDialog.getOpenFileNames(
            self, "파일 열기 (여러 개 가능)", "",
            "모든 파일 (*.*)")
        self._intake(paths)

    def on_open_folder(self):
        """폴더 열기 — 하위 파일을 수집한다.

        상위폴더(예 EP1) 밑에 RT/CT/HT 온도 폴더가 있으면 그 폴더들만 읽고 역할을
        기억한다(Temperature 배치 창 자동 배치 근거). 온도 폴더가 없으면 일반 폴더로
        보고 하위 파일을 전부 가져온다 — 전 모드에서 쓸 수 있다.
        확장자 필터는 없다(파일 열기와 같은 규칙) — 필요 없는 파일은 리스트에서 ✕ 로 뺀다."""
        path = QFileDialog.getExistingDirectory(self, "폴더 열기", "")
        if path:
            self._intake_folders([path])

    def _intake_folders(self, dirs, extra_paths=None):
        """폴더(들) 스캔 → 함께 드롭된 낱개 파일과 합쳐 기존 인테이크 흐름에 합류.

        extra_paths 는 폴더와 같이 끌어다 놓은 파일이다(스캔 결과 뒤에 붙인다)."""
        paths, roles, skipped = folder_intake.scan_folders(dirs)
        merged, seen = list(paths), set(paths)
        for p in extra_paths or []:
            key = str(Path(p).resolve())
            if key not in seen:
                merged.append(key)
                seen.add(key)
        if not merged:
            QMessageBox.warning(self, "폴더 열기", "폴더에 파일이 없습니다.")
            return

        before = len(self.csv_paths or [])
        self._intake(merged, roles=roles)
        if len(self.csv_paths or []) == before:
            return                      # busy 로 인테이크가 막힘 — 안내할 것 없음
        notes = []
        if roles:
            notes.append(" / ".join(
                f"{role} {sum(1 for r in roles.values() if r == role)}개"
                for role in folder_intake.ROLE_ORDER
                if any(r == role for r in roles.values())))
        if skipped:
            notes.append(f"건너뛴 폴더 {len(skipped)}개({', '.join(skipped[:5])})")
        if notes:
            self._status(f"{len(self.csv_paths)}개 파일 선택됨 — " + " · ".join(notes))

    def on_browse_d1(self):
        dlg = D1BrowserDialog(self)
        if not dlg.exec():
            return
        paths = dlg.selected_paths()
        if not paths:
            QMessageBox.warning(self, "선택 없음", "가져올 파일을 선택하세요.")
            return
        self._intake(paths)

    def on_help_honey(self):
        """도움말(&H) → HONEY 도움말: 서버 help.html 을 내장 브라우저로 연다
        (내장 브라우저가 없으면 _open_in_embedded 가 시스템 브라우저로 폴백)."""
        self._open_in_embedded(SERVER_BASE_URL.rstrip("/") + "/pe/report/help")

    def on_voc(self):
        """도움말(&H) → VOC: 구 Confluence VOC 페이지를 기본(시스템) 브라우저로 연다.
        외부 링크라 SSO 세션이 있는 시스템 브라우저에서 열어야 한다(내장 브라우저 X)."""
        webbrowser.open("https://confluence.samsungds.net/pages/editpage.action?pageId=3473285336")

    def _intake(self, paths, roles=None):
        """선택된 파일들 → 메인 창 파일 리스트에 로드.

        순서 변경은 파일 리스트의 ▲▼ 버튼이 담당하므로 인테이크 시점의 순서 지정
        팝업은 두지 않는다(같은 기능 중복).
        파일 열기·D1·드래그앤드롭이 모두 여기로 합류하므로, busy 중 새 입력 차단도
        여기 한 곳에서 한다 (드롭은 액션이 아니라 setEnabled 로 막을 수 없다).
        roles 는 폴더 열기가 알아낸 {경로: 온도 역할} (없으면 None)."""
        if self._busy:
            self._status("작업이 진행 중입니다 — 완료 후 파일을 가져와 주세요.")
            return
        paths = list(paths or [])
        if not paths:
            return
        self._load_paths(paths, roles=roles)

    def _refill_csv_list(self):
        """self.csv_paths 순서대로 list_csv(테이블) 다시 채우기.
        0열=파일 절대경로, 1열=확장자(오른쪽, 좁게)."""
        self.list_csv.setRowCount(len(self.csv_paths))
        for r, p in enumerate(self.csv_paths):
            full_path = str(Path(p).resolve())
            ext = Path(full_path).suffix.lstrip(".").lower()
            ext_item = QTableWidgetItem(ext)
            path_item = QTableWidgetItem(full_path)
            path_item.setData(Qt.ItemDataRole.UserRole, full_path)
            path_item.setToolTip(full_path)
            self.list_csv.setItem(r, 0, path_item)
            self.list_csv.setItem(r, 1, ext_item)
            # 행별 ✕ 삭제 버튼 — 그 행의 파일만 리스트에서 제거(경로로 식별해 행 이동에 안전).
            del_btn = QPushButton("✕")
            del_btn.setToolTip("이 파일을 리스트에서 삭제")
            del_btn.setFixedSize(24, 20)
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setStyleSheet(
                "QPushButton { border:none; background:transparent; color:#d11a2a;"
                " font-size:14px; font-weight:800; padding:0px; }"
                "QPushButton:hover { color:#ff0000; background:#ffe1e1; border-radius:4px; }")
            del_btn.clicked.connect(
                lambda _checked=False, path=full_path: self._delete_file_by_path(path))
            self.list_csv.setCellWidget(r, 2, del_btn)
            self.list_csv.setRowHeight(r, 20)
        if self.csv_paths:
            fm = self.list_csv.fontMetrics()
            # 파일명은 굵게 그려지므로(FilePathDelegate) 그 부분만 굵은 폭으로 잰다.
            bold = QFont(self.list_csv.font())
            bold.setBold(True)
            fm_bold = QFontMetrics(bold)
            width = 0
            for p in self.csv_paths:
                full = str(Path(p).resolve())
                cut = max(full.rfind("\\"), full.rfind("/")) + 1
                width = max(width, fm.horizontalAdvance(full[:cut])
                            + fm_bold.horizontalAdvance(full[cut:]))
            self.list_csv.setColumnWidth(0, max(420, width + 36))
            # 파일 리스트를 채우면 긴 경로의 파일명(오른쪽)이 보이도록 가로 스크롤을 끝까지.
            # 스크롤바 range 는 레이아웃 후 갱신되므로 다음 이벤트 루프에서 최대로 민다.
            bar = self.list_csv.horizontalScrollBar()
            QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def _clear_files(self):
        """파일 리스트를 전체 비운다 (Clear 버튼)."""
        self.csv_paths = []
        self.csv_roles = {}
        self._refill_csv_list()
        self.le_outname.clear()
        self.group = None
        self.out_path = None
        self._status("파일 리스트를 비웠습니다.")

    def _delete_file_by_path(self, path):
        """행별 ✕ 버튼: 그 행의 파일 1개만 리스트에서 제거한다.
        행 삭제 후 인덱스가 바뀌므로 클릭 시점의 행 번호가 아니라 경로로 대상을 찾는다."""
        target = str(Path(path).resolve())
        idx = next((i for i, p in enumerate(self.csv_paths)
                    if str(Path(p).resolve()) == target), -1)
        if idx < 0:
            return
        removed = Path(self.csv_paths[idx]).name
        del self.csv_paths[idx]
        (self.csv_roles or {}).pop(target, None)
        self._refill_csv_list()
        # 파일 구성이 바뀌었으니 그룹은 무효화 — Start 시 재구성된다.
        self.group = None
        self.out_path = None
        if not self.csv_paths:
            self.le_outname.clear()
        self._status(f"'{removed}' 을(를) 리스트에서 제거했습니다.")

    def _load_paths(self, paths, roles=None):
        """선택된 입력 파일들 → 기존 리스트에 이어붙이기(중복 경로 제외) + 저장 파일명 제안.
        새로 File open(또는 D1/드래그드롭)을 해도 기존 리스트를 지우지 않고 추가한다.
        전체 비우기는 Clear 버튼, 개별 제거는 각 행의 ✕ 버튼이 담당한다.
        roles({경로: 온도 역할})는 파일 리스트와 같은 수명으로 csv_roles 에 누적한다."""
        new_paths = [str(Path(p).resolve()) for p in paths]
        merged = list(self.csv_paths or [])
        seen = set(merged)
        for p in new_paths:
            if p not in seen:
                merged.append(p)
                seen.add(p)
        self.csv_paths = merged
        if roles:
            self.csv_roles = {**(self.csv_roles or {}), **roles}
        self._refill_csv_list()
        self.le_outname.setText(_suggest_base_name(self.csv_paths))
        self.group = None
        self.out_path = None
        self.txt_summary.setPlainText("")
        self._status(f"{len(self.csv_paths)}개 파일 선택됨. 순서 확인 후 Start 를 누르세요.")

    def _move_file(self, delta):
        """선택한 입력 파일을 위(-1)/아래(+1)로 이동 (전처리는 Start 까지 보류)."""
        row = self.list_csv.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < len(self.csv_paths)):
            return
        self.csv_paths[row], self.csv_paths[new] = self.csv_paths[new], self.csv_paths[row]
        self._refill_csv_list()
        self.list_csv.selectRow(new)

    def _parse_group_core(self, paths, product_type, warn, stage_cb=None):
        """파일 파싱 → 그룹 구성 (+ 스키마 검증). **워커 스레드에서 호출한다**.

        UI 에 직접 접근하지 않는다 — 진행은 ``stage_cb(value, label)`` 로만 보고하고,
        호출부(UI 스레드)가 그 값을 진행바에 반영한다(``_parse_progress_poll``).
        ``product_type`` 은 위젯을 읽는 값이라 호출부가 UI 스레드에서 미리 넘긴다.
        반환 (group, issues|None).
        """
        def _stage(value, label):
            if stage_cb is not None:
                stage_cb((value, label))

        n_files = len(paths)
        results = []
        for i, p in enumerate(paths):
            filename = Path(p).name
            _stage(i, f"파일 전처리 중... ({i + 1}/{n_files})  {filename}")
            file_start_perf = time.perf_counter()
            results.append(rg.df_honey.from_csv(p, product_type=product_type))
            if _FLOW_PROFILE_ON:
                print(
                    f"[flow-profile] honey_main.load_file[{filename}]: "
                    f"{time.perf_counter() - file_start_perf:.3f}s",
                    file=sys.stderr,
                    flush=True,
                )
        _stage(n_files, "그룹 구성 중...")
        with _flow_time("df_honey_group.construct"):
            group = rg.df_honey_group(results)
        # product_type 별 파일명 규칙(MDDI 마커 / PDDI 고정위치 / PMIC·SECURITY·TCON LOT+WF)
        # 으로 legend 기본값을 덮어쓴다. 원 규칙은 report_generator/df_honey.py(동결 영역)에
        # 있어 고칠 수 없으므로, 정식 오버라이드 API 인 rename_sources 로 파싱 직후
        # 갈아끼운다 — 빈 문자열은 기존명 유지, 중복은 _2/_3 회피, 캐시 무효화까지 해준다.
        # 입력 파일 개수 ≠ source 개수(CLAUDE.md #9)인데 rename_sources 는 positional 이라
        # 길이 대조는 resolve_source_names 가 한다 — 확신이 없으면 None 이라 기존명이 남는다.
        from honey_ui.source_naming import resolve_source_names
        auto_names = resolve_source_names(paths, product_type, len(group))
        if auto_names:
            group.rename_sources(auto_names)
        issues = None
        if warn:
            _stage(n_files, "스키마 검증 중...")
            with _flow_time("group.validate"):
                validated = group.validate()
            issues = {name: v for name, v in validated.items() if v}
        return group, issues

    def _rebuild_group(self, warn=False):
        """현재 self.csv_paths 순서로 그룹 재구성 + 항목 갱신.

        맨 위(첫) 파일이 units/항목명/Lower·Upper limit 의 기준이 된다 — 서로 다른
        유형의 파일이 섞여 들어와도 첫 파일 스키마를 기준으로 데이터가 처리된다.
        """
        with _flow_time("_rebuild_group.total"):
            paths = self.csv_paths
            if not paths:
                return False

            n_files = len(paths)
            # 파싱·그룹 구성·검증을 백그라운드 스레드(1개)에서 수행한다. 동기로 돌리면
            # 무거운 pandas 읽기 동안 Qt 이벤트 루프가 멈춰 Windows 가 창을 "응답 없음"
            # 으로 표시한다. 메인 스레드는 짧게 폴링하며 processEvents() 로 UI 를 살려
            # "(진행중)" 을 보여주고, 한 단계가 60초를 넘기면 라벨만 바꾼다(중단 없음).
            progress = _ElapsedProgress(
                self.progress_status, "파일 로딩 준비 중...", self._status,
                busy=True, minimum=0, maximum=n_files,
                mirror=getattr(self, "panel_progress", None))
            QApplication.processEvents()

            stage_q = queue.Queue()
            # with(=shutdown(wait=True)) 를 쓰지 않는다 — 취소 시 실행 중인 파싱이 끝날
            # 때까지 갇혀 "바로 취소" 가 안 된다. 파싱은 읽기 전용이라 버려도 무해하다.
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                fut = ex.submit(self._parse_group_core, list(paths),
                                self.product_type(), warn, stage_q.put)
                self.group, issues = _wait_for_future(
                    fut, progress, poll_cb=_parse_progress_poll(progress, stage_q),
                    cancelled=self._op_cancel_requested)
            except _OperationCancelled:
                ex.shutdown(wait=False, cancel_futures=True)
                progress.fail("취소됨: 파일 로드 취소")
                self._status("취소됨")
                self.group = None
                return False
            except Exception as exc:
                ex.shutdown(wait=False, cancel_futures=True)
                progress.fail(f"실패: 파일 로드 실패 - {exc}")
                _show_exc(self, "파일 로드 실패", exc,
                          prefix="선택한 파일을 읽지 못했습니다.")
                self._status("파일 로드 실패")
                self.group = None
                return False
            ex.shutdown(wait=False)

            progress.success(f"완료: {n_files}개 파일 전처리 완료", value=n_files)

            if issues:
                msg = "\n".join(f"- {name}: {', '.join(v)}" for name, v in issues.items())
                QMessageBox.warning(self, "스키마 경고", f"일부 파일에 문제가 있습니다:\n{msg}")

            self.out_path = None
            self._status(f"{len(paths)}개 파일 전처리 완료 (기준: {Path(paths[0]).name}).")
            return True

    def _apply_modes(self, group, mode_bin1, mode_dut):
        """선택된 데이터 정리 모드를 그룹에 적용. 문제 시 ValueError."""
        work = group
        if mode_bin1:
            work = work.filter_rows_by_bin("1")
            if not work.subjects() or all(len(md.scores) == 0
                                          for md in work.mass_data_map.values()):
                raise ValueError("Bin1 Only: Bin 이 1(Pass)인 데이터가 없습니다.")
        if mode_dut:
            # 기준은 입력 파일 개수가 아니라 honey_parse 가 돌려준 source(df) 개수다.
            if len(work) != 1:
                raise ValueError(
                    f"DUT 정리는 source 가 1개일 때만 가능합니다. (현재 {len(work)}개)")
            work = work.split_by_dut()
        return work

    # ── Start: 전처리 → 설정 팝업 → Confirm 시 분석 실행 ─────────────────────
    def _prepare_run_context(self):
        if not self.csv_paths:
            QMessageBox.warning(self, "입력 누락", "먼저 파일을 가져오세요.")
            return None
        # 파일 전처리(그룹 로드/검증) 를 이 시점에 수행
        if not self._rebuild_group(warn=True) or self.group is None:
            return None

        # 모드 활성 조건은 source(honey_parse 반환 df) 개수 기준 — 입력 파일 개수가 아니다.
        dlg = ReportSettingsDialog(
            self, self.group, len(self.group), product_type=self.product_type())
        if not dlg.exec():
            self._status("설정 취소됨 — 다시 Start 로 진행할 수 있습니다.")
            return None

        selected = dlg.selected_items()
        sheets = dlg.selected_sheets()
        # Filename(legend) 사용자 지정 시 source 명 교체 (DUT 정리 모드는 자체 명명 사용)
        overrides = dlg.filename_overrides()
        if overrides is not None and not dlg.mode_dut():
            self.group.rename_sources(overrides)
        # 데이터 정리 모드 적용 (Bin1 Only → DUT 정리 순서로 그룹 변환)
        try:
            work_group = self._apply_modes(self.group, dlg.mode_bin1(), dlg.mode_dut())
        except ValueError as exc:
            QMessageBox.warning(self, "모드 적용 불가", str(exc))
            return None
        return {
            "work_group": work_group,
            "selected": selected,
            "sheets": sheets,
            "raw_data": dlg.raw_data(),
            "compare_mode": dlg.mode_compare(),
        }

    def _set_run_buttons_enabled(self, enabled):
        """Web Report / Excel Report 실행 버튼을 함께 활성/비활성한다.

        한 리포트가 진행되는 동안(전처리·분석·업로드 전 구간) 두 버튼을 모두 잠가
        중복 실행을 막는다. 재활성은 호출부(on_start/on_web_report)의 finally 에서 보장한다.
        """
        self.btn_start.setEnabled(enabled)
        self.btn_web_report.setEnabled(enabled)

    def _set_busy(self, busy):
        """무거운 작업 진행 구간을 표시하고 새 작업 진입점을 함께 잠근다.

        실행 버튼만 잠그던 종전 방식은 메뉴·사이드바·드래그앤드롭으로 우회할 수 있어,
        분석 중에 Excel Download 나 파일 인테이크가 겹쳐 들어올 수 있었다. 진입점을
        한 곳(_busy_actions + _intake 가드)에서 함께 잠근다. 워커별 isRunning 가드는
        이중 방어로 그대로 둔다.
        """
        self._busy = bool(busy)
        self._set_run_buttons_enabled(not busy)
        self.btn_upload_local.setEnabled(not busy)
        for act in self._busy_actions:
            act.setEnabled(not busy)

    def _release_busy_after_cancel(self, future, progress, fail_text):
        """취소했지만 아직 도는 백그라운드 전처리가 끝난 뒤에 busy 를 푼다.

        Excel COM 전처리는 시작되면 중간 취소가 불가능하다(cancel_futures 는 아직 시작
        안 한 작업만 취소). busy 를 즉시 풀면 사용자가 곧바로 Excel Download·Rawdata
        편집을 눌러 아직 살아있는 EXCEL.EXE 와 겹칠 수 있으므로, 완료될 때까지 진입점을
        잠근 채 진행바에 정리 중임을 표시한다. 이미 끝났으면 바로 해제된다.
        """
        def _poll():
            if future.done():
                self._set_busy(False)
                progress.fail(fail_text)
                return
            progress.set("취소 정리 중... (Excel 처리가 끝나면 해제됩니다)",
                         status="취소 정리 중...")
            QTimer.singleShot(200, _poll)
        _poll()

    def on_start(self):
        # 느린 파일 전처리(_prepare_run_context → _rebuild_group)가 시작되기 전에
        # 새 작업 진입점(실행 버튼·메뉴·사이드바·파일 인테이크)을 함께 잠근다.
        self._set_busy(True)
        try:
            ctx = self._prepare_run_context()
            if ctx is None:
                return
            self._run_analysis(
                ctx["work_group"], ctx["selected"], ctx["sheets"],
                ctx["raw_data"], compare_mode=ctx["compare_mode"])
        finally:
            self._set_busy(False)

    def on_webreport_colors(self):
        """F10 메뉴 → Distribution 색(Legend/source 팔레트) 설정.

        기존 ColorEditorDialog 재사용 — OK 시 chart_colors.json 에 저장되어 다음 Web Report
        생성 때 디폴트로 실린다. 색 번호 i = distribution source i 의 색.
        """
        dlg = ColorEditorDialog(self)
        if dlg.exec():
            self._status("Distribution 색 저장됨")

    def on_options(self):
        """Options — 기본 Product Type + Distribution 색.

        OK 시 선택한 Product Type 을 메인 UI 라디오에도 즉시 반영한다
        (라디오 toggled → _save_product_type 로 settings.json 에도 저장됨).
        """
        dlg = OptionsDialog(self)
        if dlg.exec():
            pt = dlg.selected_product_type()
            if pt in self._pt_radios:
                self._pt_radios[pt].setChecked(True)
            self._status("옵션 저장됨")

    # ── Web Report 진행 취소 (패널 진행바 옆 취소 버튼) ────────────────────
    # 스레드는 죽일 수 없으므로 "대기 중단 + 결과 폐기" 방식이다 — 각 단계 계산은
    # 읽기 전용이라 버려도 부작용이 없다(선행 인코딩 취소와 같은 전제). 버튼이 숨겨져
    # 있으면 플래그를 세울 방법이 없어 다른 흐름(Excel Report 등)은 종전과 동일하다.

    def _begin_op_cancel(self):
        """취소 버튼 노출 + 플래그 초기화 — Web Report 흐름 시작 시."""
        self._op_cancelled = False
        btn = getattr(self, "btn_panel_cancel", None)
        if btn is not None:
            btn.setEnabled(True)
            btn.show()

    def _end_op_cancel(self):
        """취소 버튼 정리 — 성공/실패/취소 어느 경로로 끝나든 호출(여러 번 안전).

        플래그도 함께 리셋한다 — 남겨두면 다음에 _rebuild_group 을 타는 다른 흐름
        (Excel Report 등)이 시작하자마자 취소로 오판된다.
        """
        self._op_cancelled = False
        btn = getattr(self, "btn_panel_cancel", None)
        if btn is not None:
            btn.hide()
            btn.setEnabled(True)

    def _on_op_cancel(self):
        """취소 버튼 클릭 — 즉시 중단이 아니라 '요청'이다(다음 폴링에서 중단)."""
        self._op_cancelled = True
        btn = getattr(self, "btn_panel_cancel", None)
        if btn is not None:
            btn.setEnabled(False)
        self._status("취소 요청됨 — 정리 중...")

    def _op_cancel_requested(self):
        """wait_for_future(cancelled=) 용 콜백."""
        return bool(getattr(self, "_op_cancelled", False))

    def _cancel_web_report(self, progress, prep_ex):
        """_run_web_report 취소 확정 공통 처리 — 진행바 마감 + 워커 풀 폐기.

        실행 중인 계산은 스레드라 즉시 죽지 않지만 결과는 버려진다(읽기 전용이라 무해).
        버튼 숨김은 on_web_report 의 finally(_end_op_cancel)가 한다.
        """
        prep_ex.shutdown(wait=False, cancel_futures=True)
        progress.fail("취소됨: Web Report 생성 취소")
        self._status("Web Report 취소됨")

    def on_web_report(self):
        # 느린 파일 전처리(_prepare_web_report_context → _rebuild_group)가 시작되기 전에
        # 새 작업 진입점(실행 버튼·메뉴·사이드바·파일 인테이크)을 함께 잠근다.
        self._set_busy(True)
        self._begin_op_cancel()
        try:
            ctx = self._prepare_web_report_context()
            if ctx is None:
                return
            self._run_web_report(ctx["work_group"], ctx["selected"], ctx["sheets"],
                                 compare_mode=ctx["compare_mode"], options=ctx["options"],
                                 mode=ctx["mode"], source_order=ctx.get("source_order"),
                                 temperature=ctx.get("temperature"),
                                 prefetch=ctx.get("prefetch"))
        finally:
            # 배치창 취소·예외로 빠져나가도 선행 executor 가 남지 않게 한다. 정상 완료면
            # _run_web_report 가 이미 정리한 뒤라 no-op 다.
            self._abort_encode_prefetch()
            self._end_op_cancel()
            self._set_busy(False)

    def _dialog_entries(self, names, paths=None, from_group=True):
        """SourceNameDialog 입력 ``[(legend, 대표 입력 파일 절대경로)]`` 를 만든다.

        파싱이 끝났으면 md 에서 대표 경로를 얻고(``source_display_path`` 가 우선순위를
        한 곳에 모아둔다), **파싱 전**(Temperature 선표시)에는 아직 md 가 없으므로 추정
        이름과 같은 순번의 입력 파일 경로를 쓴다 — 그 이름 자체가 그 파일에서 나왔으므로
        순번이 곧 대응이다. 그때는 stale 한 self.group 을 보지 않도록 from_group=False.
        """
        from honey_ui.source_name_dialog import source_display_path

        group = getattr(self, "group", None) if from_group else None
        mass_map = getattr(group, "mass_data_map", None) if group is not None else None
        entries = []
        for i, name in enumerate(names):
            path = ""
            if mass_map is not None and name in mass_map:
                path = source_display_path(mass_map[name], "")
            if not path and paths and i < len(paths):
                path = str(paths[i])
            entries.append((name, path))
        return entries

    def _ask_source_names(self):
        """Web Report 생성 직전 source 이름·순서를 표에서 확인·변경. 취소면 None.

        표 한 줄이 source 하나다 — 왼쪽은 그 source 를 만든 대표 입력 파일(읽기 전용,
        툴팁에 전체 경로), 오른쪽이 legend 이름이다. ↑/↓ 로 바꾼 순서가 그대로 업로드
        순서(= 서버 tables 순서)가 되고 **최상단 source 의 limit 이 리포트 전체 기준**이다.

        반환은 SourceNameDialog.result_arrangement() 그대로 — 호출부가
        _apply_source_arrangement 로 반영한다.
        """
        from honey_ui.source_name_dialog import SourceNameDialog

        names = list(self.group.names())
        dlg = SourceNameDialog(self, self._dialog_entries(names, list(self.csv_paths)),
                               mode=self._selected_web_mode())
        if not dlg.exec():
            return None
        return dlg.result_arrangement()

    def _apply_source_arrangement(self, arranged, options):
        """다이얼로그 결과를 group·options 에 반영하고 업로드 순서를 돌려준다.

        rename 은 groups/order 를 쓰는 것보다 **먼저** 해야 mass_data_map 조회가 새 이름과
        맞는다. 순서는 이름이 아니라 **원본 index** 로 잇는다 — 다이얼로그의 dedupe 규칙과
        ``df_honey_group._dedup_in_place`` 는 알고리즘이 달라(카운터 vs 충돌 회피 루프)
        같은 입력에서도 이름이 갈릴 수 있고, 그러면 mass_data_map 조회가 KeyError 가 난다.
        """
        self.group.rename_sources(arranged["names"])
        actual = list(self.group.names())
        order = [actual[i] for i in arranged.get("order_index") or []
                 if 0 <= i < len(actual)]
        colors = arranged.get("colors")
        if colors:
            # 창에서 지정한 색이 옵션(F10) 팔레트보다 우선한다 (이 리포트에만 적용).
            options["colors"] = colors
        return order if len(order) == len(actual) else None

    def _selected_web_mode(self):
        """패널 라디오에서 선택된 Web Report 분석 모드 (기본 Normal)."""
        radios = getattr(self, "_mode_radios", None) or {}
        for key, rb in radios.items():
            if rb.isChecked():
                return key
        return "Normal"

    def _validate_web_mode(self, mode, n_sources):
        """선택 모드가 source 개수에 맞는지 검사. 문제 시 경고 후 False.

        기준은 입력 파일 개수가 아니라 **honey_parse 가 돌려준 source(df) 개수**다 —
        여러 입력 파일이 하나로 병합되거나 한 파일이 여러 source 로 나뉠 수 있어,
        업로드되는 parquet(=source) 개수가 유일한 기준이다.

        - Normal: 제한 없음
        - Compare: source 2개 이상 (Before/After 두 그룹으로 나눠 비교 — 개수 상한 없음)
        - DUT: source 1개 (DUT/site 별 분할)
        - Commonality: source 1개 (강조 chip 을 웹에서 선택)
        - Temperature: 제한 없음 (RT 단독 그룹도 가능 — 그룹 구성은 배치 창이 검증한다)
        """
        n = n_sources
        if mode in ("DUT", "Commonality") and n != 1:
            QMessageBox.warning(self, "모드 적용 불가",
                                f"{mode} 모드는 source 가 1개일 때만 가능합니다. (현재 {n}개)")
            return False
        if mode == "Compare" and n < 2:
            QMessageBox.warning(self, "모드 적용 불가",
                                f"Compare 모드는 source 가 2개 이상일 때만 가능합니다. (현재 {n}개)")
            return False
        return True

    def _guess_source_names(self, paths):
        """파싱 **전에** 파일명만으로 source 이름을 추정한다 (그룹 배치 창 선표시용).

        **전 파일이 이름 규칙을 갖출 때만** 파싱 없이 최종 이름을 알 수 있으므로, 하나라도
        빠지면 None 을 돌려 호출부가 종전 순서(파싱 → 배치 창)로 돌아가게 한다 — 뜻 없는
        stem 조각으로 창을 띄우면 자동 그룹 배치(suggest_groups)까지 빗나가 오히려 손해다.

        규칙은 ``_parse_group_core`` 가 파싱 직후 적용하는 것과 **같아야** 한다. 어긋나면
        Temperature 가 파싱 전에 띄운 창의 이름이 최종 legend 와 달라 창이 두 번 뜬다.
        그래서 source_naming 의 product_type 별 규칙을 먼저 쓰고, 그 규칙이 **한 파일도**
        안 맞을 때만 df_honey 규칙으로 폴백한다(그때는 _parse_group_core 도 rename 하지
        않으므로 두 경로가 반드시 일치한다).
        중복 해소(_2, _3 …)는 ``df_honey_group._dedup_in_place`` 와 같은 규칙이다.
        """
        from honey_ui.source_naming import guess_source_names, suggest_source_names
        from report_generator.df_honey import _sheetname_from_filename

        bases = guess_source_names(paths, self.product_type())
        if bases is None:
            # 규칙이 **일부만** 맞으면 파싱 후 그 파일들만 개명된다 → 미리 띄운 창의 이름과
            # 최종 legend 가 어긋나 창이 두 번 뜬다. 그럴 땐 선표시를 포기한다.
            if suggest_source_names(paths, self.product_type()):
                return None
            bases = []
            for p in paths:
                base = _sheetname_from_filename(Path(p))
                if not base:
                    return None
                bases.append(base)

        names, used = [], set()
        for base in bases:
            cand, n = base, 2
            while cand in used:
                cand = f"{base}_{n}"
                n += 1
            used.add(cand)
            names.append(cand)
        return names

    def _roles_for_names(self, names, paths):
        """{source 이름: 온도 역할} — 폴더에서 얻은 경로별 역할을 source 이름으로 옮긴다.

        ⚠️ 입력 파일 개수 ≠ source 개수다(honey_parse 가 내부 병합할 수 있음 — CLAUDE.md #9).
        그래서 names 와 paths 의 길이가 같을 때(= 파일 1개가 source 1개인 경우)만 index 로
        잇고, 그 외에는 **파일 stem 부분일치**로만 잇는다. 못 이은 source 는 키를 만들지
        않아 배치 창에서 미배정으로 남는다 — 조용한 오배치보다 사용자 배치가 낫다.
        """
        roles = getattr(self, "csv_roles", None) or {}
        if not roles or not names:
            return {}
        by_path = {str(Path(p).resolve()): roles.get(str(Path(p).resolve())) for p in paths}
        if len(names) == len(paths):
            return {n: r for n, r in zip(names, (by_path[str(Path(p).resolve())] for p in paths))
                    if r}
        out = {}
        for name in names:
            key = str(name).strip().lower()
            if not key:
                continue
            for path, role in by_path.items():
                if not role:
                    continue
                stem = Path(path).stem.lower()
                if key in stem or stem in key:
                    out[name] = role
                    break
        return out

    def _temp_pair_keys(self, names, paths):
        """{source 이름: 짝 키} — Temperature 자동 배치용 (2026-08-24).

        키 = 입력 파일명에서 product_type 규칙으로 다시 계산한 base(LOT_WF, 소문자).
        같은 웨이퍼의 RT/CT 파일은 base 가 같아, dedupe(_2)로 source 이름이 갈려도
        (``6Z19AFA1``/``6Z19AFA1_2``) 이 키로는 같은 그룹으로 묶인다. 이름 규칙이 없는
        파일·개수가 어긋난 경우(병합)는 키를 만들지 않아 종전 추정(순번) 그대로다.
        """
        if not names or len(names) != len(paths):
            return {}
        from honey_ui.source_naming import source_name_for

        out = {}
        for name, path in zip(names, paths):
            base = source_name_for(path, self.product_type())
            if base:
                out[str(name)] = str(base).lower()
        return out

    def _temperature_first_flow(self):
        """Temperature 모드: RT/CT/HT 배치 창을 **파싱보다 먼저** 띄운다. 취소면 None.

        Temperature 는 source 가 가장 많은 모드인데, 정리(_clean_temperature_frames)가
        배치 결과로 rawdata 를 바꾸므로 배치가 끝나기 전에는 인코딩을 시작할 수 없다.
        대신 **파싱**은 배치와 무관하므로 창을 먼저 띄우고 그 뒤에서 돌린다 — 사용자가
        source 를 끌어다 놓는 시간에 파싱이 숨는다.

        배치 창은 이름 목록만 쓰고, 그 이름은 파일명 패턴이 있으면 파싱 없이 정확히
        알 수 있다(``_guess_source_names``). 알 수 없으면 종전 순서로 돌아간다.

        업로드 순서가 그룹마다 [RT, CT, HT] 라 서버 ``tables`` 도 그 순서가 되고,
        그룹의 RT 가 CT/HT 재판정의 limit 기준이다.
        """
        from honey_ui.source_name_dialog import SourceNameDialog

        paths = list(self.csv_paths)

        def _temp_dialog(names, from_group):
            return SourceNameDialog(self, self._dialog_entries(names, paths, from_group),
                                    mode="Temperature",
                                    roles=self._roles_for_names(names, paths),
                                    pair_keys=self._temp_pair_keys(names, paths))

        names_guess = self._guess_source_names(paths)
        if names_guess is None:
            # 파일명만으로 이름을 알 수 없다 — 종전 순서(파싱 → 배치 창) 그대로.
            if not self._rebuild_group(warn=True) or self.group is None:
                return None
            # 파싱이 이미 끝났으니 배치 창 시간을 인코딩이 쓴다 (RT·미배정분은 배치가
            # 어떻게 나오든 그대로 유효하다 — _build_webreport_parquets 의 cleaned 판정).
            self._start_encode_prefetch()
            dlg = _temp_dialog(list(self.group.names()), True)
            if not dlg.exec():
                self._status("Temperature 배치 취소")
                return None
            arranged = dlg.result_arrangement()
            if not self._temp_coord_check(arranged):
                return None
            return arranged

        stage_q = queue.Queue()
        cancel = threading.Event()
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        handed_over = False
        try:
            fut = ex.submit(self._parse_group_core, paths, self.product_type(),
                            True, stage_q.put)

            # 창의 자동 배치와 **같은 규칙**으로 배치를 미리 추정한다 — CT/HT 는 이 배치로
            # rawdata 를 정리해야 인코딩할 수 있어서, 추정 없이는 RT·미배정분밖에 못 만든다.
            guess_groups = _guess_temperature_groups(
                names_guess, self._roles_for_names(names_guess, paths),
                self._temp_pair_keys(names_guess, paths).get)

            def _prefetch_after_parse():
                """파싱 뒤에 이어 달리는 선행 인코딩 (같은 워커 1개 FIFO).

                배치 시간이 파싱보다 길면 남는 시간을 이게 먹고, 짧으면 시작도 못 한 채
                남는다 — 아래 _wait_for_future(fut) 는 파싱만 기다리므로 종전과 같다.

                RT·미배정은 배치와 무관하므로 원본 그대로(raw), CT/HT 는 추정 배치로 정리한
                뒤(cleaned) 인코딩한다. 사용자가 창에서 RT 파트너를 바꾸면 그 member 의
                정리분만 버려진다(drop_cleaned).
                """
                grp, _issues = fut.result()
                names = list(grp.names())
                mds = [grp.mass_data_map[n] for n in names]
                frames = {n: (md.to_df() if hasattr(md, "to_df") else md.df)
                          for n, md in zip(names, mds)}
                members = {m for g in guess_groups for m in g["members"]}
                # RT·미배정 먼저 — 추정이 빗나가도 이쪽은 100% 살아남는다.
                out = {"raw": _encode_sources_worker(
                    [(md, frames[n]) for n, md in zip(names, mds) if n not in members],
                    cancel), "cleaned": {}}
                if not guess_groups:
                    return out
                from web_report.temperature import clean_frames
                # 추정 이름이 실제와 어긋나면 clean_frames 가 그 그룹을 건너뛴다 →
                # cleaned 가 비어 조립이 정식 경로로 인코딩한다(값은 항상 최종 배치 기준).
                cleaned, _stats = clean_frames(frames, guess_groups, None)
                out["cleaned"] = _encode_sources_worker(
                    [(md, cleaned[n]) for n, md in zip(names, mds)
                     if n in cleaned and cleaned[n] is not frames[n]], cancel)
                return out
            prefetch = _EncodePrefetch(ex, ex.submit(_prefetch_after_parse), cancel, None)

            # 아직 파싱 전이라 md 가 없다 — 추정 이름과 같은 순번의 입력 파일을 보여준다.
            dlg = _temp_dialog(names_guess, False)
            while True:
                if not dlg.exec():
                    self._status("Temperature 배치 취소")
                    fut.cancel()      # 이미 시작됐으면 결과만 버린다(읽기 전용이라 무해)
                    return None
                arranged = dlg.result_arrangement()
                # 미리 계산해 둔 배치를 사용자가 바꿨으면 그만큼 다시 계산해야 한다.
                # limit 파일 드롭은 배치 변경이 아니라 정확도를 올리는 정상 입력이라
                # 묻지 않고 조용히 다시 계산한다.
                if (not guess_groups or arranged.get("bin_map")
                        or not _temp_invalid_members(guess_groups, names_guess, arranged)):
                    break
                if QMessageBox.question(
                        self, "Temperature 배치 변경",
                        "미리 계산해 둔 배치와 다르게 바꾸셨습니다. 정말 바꾸시겠습니까?\n"
                        "(계산해 둔 결과를 버리고 다시 계산하므로 보고서 생성 시간이 "
                        "약간 증가합니다.)",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.Yes) == QMessageBox.StandardButton.Yes:
                    break

            # 파싱 잔여분 대기 — 배치가 길었으면 대개 이미 끝나 있다.
            progress = _ElapsedProgress(
                self.progress_status, "파일 로딩 준비 중...", self._status,
                busy=True, minimum=0, maximum=len(paths),
                mirror=getattr(self, "panel_progress", None))
            QApplication.processEvents()
            try:
                self.group, issues = _wait_for_future(
                    fut, progress, poll_cb=_parse_progress_poll(progress, stage_q),
                    cancelled=self._op_cancel_requested)
            except _OperationCancelled:
                # executor 정리는 아래 finally(handed_over=False)가 한다.
                progress.fail("취소됨: 파일 로드 취소")
                self._status("취소됨")
                self.group = None
                return None
            except Exception as exc:
                progress.fail(f"실패: 파일 로드 실패 - {exc}")
                _show_exc(self, "파일 로드 실패", exc,
                          prefix="선택한 파일을 읽지 못했습니다.")
                self._status("파일 로드 실패")
                self.group = None
                return None
            progress.success(f"완료: {len(paths)}개 파일 전처리 완료", value=len(paths))
            if issues:
                msg = "\n".join(f"- {name}: {', '.join(v)}" for name, v in issues.items())
                QMessageBox.warning(self, "스키마 경고", f"일부 파일에 문제가 있습니다:\n{msg}")
            self.out_path = None
            self._status(f"{len(paths)}개 파일 전처리 완료 (기준: {Path(paths[0]).name}).")

            # 이름 정합 검증 — 추정이 빗나갔으면 실제 이름으로 한 번 다시 받는다.
            # 비교 대상은 창에 **들어간** 원본 이름(source_names)이다 — order 는 사용자가
            # 바꾼 새 이름이라 rename 을 한 순간 항상 불일치가 되어 창이 두 번 뜬다.
            real = list(self.group.names())
            if sorted(arranged["source_names"]) != sorted(real):
                QMessageBox.information(
                    self, "Temperature 배치",
                    "파일을 읽어 보니 source 이름이 파일명에서 추정한 것과 다릅니다.\n"
                    "실제 이름으로 배치 창을 다시 표시합니다.")
                dlg2 = _temp_dialog(real, True)
                if not dlg2.exec():
                    self._status("Temperature 배치 취소")
                    return None
                arranged = dlg2.result_arrangement()
            # 좌표 없는 rawdata 확인 — 여기가 물어볼 수 있는 첫 지점이다(좌표는 파싱
            # 산출물에만 있다). No 면 생성을 중단한다.
            if not self._temp_coord_check(arranged):
                return None
            # 최종 배치와 어긋난 member 의 선행 정리분을 버린다 — 조립이 그것만 다시
            # 인코딩한다. index → md 는 **source_names(창에 들어간 rename 전 이름)** 로
            # 잇는다. group.names() 순서로 이으면 파서가 파일을 병합해 순서·개수가
            # 어긋난 경우 엉뚱한 source 를 지워 잘못된 정리본이 살아남는다.
            invalid = _temp_invalid_members(guess_groups, names_guess, arranged)
            if arranged.get("serial_match"):
                # 선행 정리분은 **좌표 매칭**으로 만든 것이라 SERIAL 매칭과 값이 다르다 —
                # 전부 버려 조립이 정리부터 다시 하게 한다(raw 풀은 정리와 무관해 그대로).
                prefetch.drop_cleaned({id(md) for md
                                       in self.group.mass_data_map.values()})
            elif invalid:
                mass_map = self.group.mass_data_map
                src_names = arranged.get("source_names") or []
                prefetch.drop_cleaned(
                    {id(mass_map[src_names[i]]) for i in invalid
                     if 0 <= i < len(src_names) and src_names[i] in mass_map})
            # 선행 인코딩과 그 executor 를 _run_web_report 로 넘긴다 — 조립 job 이 같은
            # FIFO 풀에 들어가야 캐시를 블로킹 없이 읽고 상태 누적도 인터리브되지 않는다.
            prefetch.keepalive = self.group      # 캐시 키가 id(md) 라 md 를 살려 둔다
            self._encode_prefetch = prefetch
            handed_over = True
            return arranged
        finally:
            # 소유권을 넘긴 뒤에는 정리하지 않는다 — shutdown 은 이후 submit 을 막는다.
            if not handed_over:
                cancel.set()
                ex.shutdown(wait=False, cancel_futures=True)

    def _temp_coord_check(self, arranged):
        """좌표 없는 rawdata 확인 → 계속할지 여부(bool). UI 스레드 전용.

        Temperature 정리는 CT/HT 를 **RT pass 좌표**로 자르는데(``temperature.clean_group``),
        좌표가 비어 있으면 그 필터가 아무것도 걸러내지 못한 채 조용히 통과한다 — RT 에서
        죽은 die 가 CT/HT 에 그대로 남아 재판정 결과가 통째로 틀린다. 그래서 좌표 없는
        source 가 있으면 파일 목록을 보여주고 묻는다:
          - Yes → SERIAL 순서로 RT↔CT/HT 를 짝짓는다(``arranged["serial_match"]``).
            그룹 안에서 행 개수가 다르면 "가장 적은 raw data 기준" 안내를 띄운다.
          - No  → False 를 돌려 Web Report 생성을 중단한다(rawdata 수정 요청).

        좌표는 파싱 산출물(honeyform)에만 있어 배치 창 시점에는 알 수 없다 — 파싱이 끝난
        직후이자 정리·인코딩이 시작되기 전인 이 지점이 물어볼 수 있는 첫 자리다.
        ``arranged`` 의 groups/names 는 새(rename 후) 이름이고 mass_data_map 은 원본
        이름이라, index 정렬인 ``source_names``↔``names`` 로 잇는다.
        """
        from web_report.temperature import data_row_count, has_coords

        mass_map = getattr(self.group, "mass_data_map", None) or {}
        md_of = {new: mass_map[old]
                 for old, new in zip(arranged.get("source_names") or [],
                                     arranged.get("names") or [])
                 if old in mass_map}
        frames = {name: (md.to_df() if hasattr(md, "to_df") else md.df)
                  for name, md in md_of.items()}
        missing = [name for name, df in frames.items() if not has_coords(df)]
        if not missing:
            return True

        listing = [f"· {self._source_file_name(md_of[n], n)} ({n})" for n in missing]
        if len(listing) > 20:
            listing = listing[:20] + [f"· … 외 {len(missing) - 20}건"]
        answer = QMessageBox.question(
            self, "좌표 없는 rawdata",
            "좌표가 없는 rawdata 가 있습니다.\n\n" + "\n".join(listing) + "\n\n"
            "Room Hot Cold 매칭을 Test 순서대로 적용하시겠습니까?\n"
            "(No 선택시 Rawdata 에 좌표 재확인 및 수정 부탁드립니다.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self._status("좌표 없는 rawdata — Web Report 생성 중단")
            return False

        arranged["serial_match"] = True
        counts = {name: data_row_count(df) for name, df in frames.items()}
        for group in arranged.get("groups") or []:
            names = [n for n in [group.get("rt"), *(group.get("members") or [])]
                     if n in counts]
            if any(n in missing for n in names) and len({counts[n] for n in names}) > 1:
                QMessageBox.warning(
                    self, "rawdata 개수 불일치",
                    "rawdata 개수가 맞지 않습니다. 가장 적은 raw data 기준으로 진행합니다")
                break
        return True

    def _start_encode_prefetch(self):
        """현재 그룹으로 선행 parquet 인코딩을 시작한다 (이전 것은 정리).

        **UI 스레드에서 df 스냅샷을 떠서** 워커에 넘긴다 — group 을 통째로 넘기면 배치창
        결과로 도는 ``rename_sources`` 와 워커가 같은 객체를 보게 된다. 스냅샷은 얕은
        참조라 복사 비용이 없고, 워커가 읽는 md.df 는 rename 이 건드리지 않는다.
        실패는 조용히 넘긴다 — 선행이 없으면 종전대로 조립 때 전량 인코딩한다.
        """
        self._abort_encode_prefetch()
        group = getattr(self, "group", None)
        if group is None:
            return
        try:
            entries = [(md, md.to_df() if hasattr(md, "to_df") else md.df)
                       for md in (group.mass_data_map[n] for n in group.names())]
        except Exception:  # noqa: BLE001
            return
        self._encode_prefetch = _EncodePrefetch.start(entries)

    def _abort_encode_prefetch(self):
        """보관 중인 선행 인코딩을 취소·정리한다 (여러 번 불러도 안전)."""
        pf = getattr(self, "_encode_prefetch", None)
        self._encode_prefetch = None
        if pf is not None:
            pf.abort()

    def _prepare_web_report_context(self):
        if not self.csv_paths:
            QMessageBox.warning(self, "입력 누락", "먼저 파일을 가져오세요.")
            return None
        mode = self._selected_web_mode()
        # Temperature 는 배치 창을 먼저 띄우고 파싱을 그 뒤에서 돌린다(자체 플로가 파싱까지
        # 책임진다). 나머지 모드는 종전대로 파싱 → 모드별 창 순서.
        temperature_arranged = None
        if mode == "Temperature":
            temperature_arranged = self._temperature_first_flow()
            if temperature_arranged is None:
                return None
        elif not self._rebuild_group(warn=True) or self.group is None:
            return None
        # 모드 검증은 전처리 **후**에 한다 — 기준이 입력 파일 개수가 아니라 honey_parse 가
        # 돌려준 source 개수라서, 그룹이 만들어져야 개수를 알 수 있다.
        if not self._validate_web_mode(mode, len(self.group)):
            self._status("모드 적용 불가")
            return None
        # F10 에서 지정한 Distribution 색(chart_colors.json)을 웹리포트에 실어 보낸다.
        # 색 번호 i = distribution source i 의 색. 미지정이면 기본 팔레트가 실린다.
        # ai_comment: Issue Table AI Comment 컬럼 표시 여부 — 서버가 세션
        # webreport_options 에 고정 저장한다 (업로드 후 토글 불가).
        # 숨김 스위치로 **활성화된 상태에서 직접 체크**했을 때만 참이다. 서버는
        # ai_comment_optin 이 함께 실린 세션만 컬럼을 만든다(구 클라가 보낸
        # ai_comment=True 세션은 미표시 — web_report/validation.webreport_ai_comment).
        ai_on = bool(self.chk_ai_comment.isEnabled() and self.chk_ai_comment.isChecked())
        options = {"colors": chart_colors.load_colors(),
                   "ai_comment": ai_on, "ai_comment_optin": ai_on}
        # SourceName(legend) 은 파일마다 달라 매번 확인·변경 후 생성.
        # DUT 모드는 서버가 업로드된 단일 honeyform 의 DUT 컬럼으로 분할·명명(DUT <값>)하므로
        # 클라에서는 분할하지 않고 rename 도 건너뛴다 (df_honey→honeyform 포맷 변환 회피).
        # 대신 **색 지정 창만** 띄운다 — 색은 분할된 source 순서에 붙기 때문이다.
        # Compare 모드는 이름 변경 창 대신 Before/After 배치 창을 띄운다 (이름·색 변경 포함).
        #
        # 그 창이 떠 있는 동안 parquet 인코딩을 미리 돌려 둔다 — 인코딩 결과는 이름·순서와
        # 무관하므로(honeyform 에 이름 컬럼이 없다) 창에서 무엇을 바꾸든 그대로 재사용된다.
        # Temperature 는 _temperature_first_flow 가 파싱과 같은 워커에 이어 붙여 이미 시작했다.
        if mode != "Temperature":
            self._start_encode_prefetch()
        source_order = None
        temperature = None
        if mode == "Compare":
            arranged = self._ask_compare_groups()
            if arranged is None:
                return None                      # 취소 = 실행 중단
            self.group.rename_sources(arranged["names"])
            options["compare"] = {"before": arranged["before"], "after": arranged["after"]}
            source_order = arranged["order"]
            if arranged.get("colors"):
                # 창에서 지정한 색이 옵션(F10) 팔레트보다 우선한다 (이 리포트에만 적용).
                options["colors"] = arranged["colors"]
        elif mode == "Temperature":
            arranged = temperature_arranged      # 위에서 파싱보다 먼저 받아둔 배치 결과
            # _apply_source_arrangement 가 rename 을 **먼저** 한다 — groups/order 는 이미
            # 새 이름이라, 그래야 _clean_temperature_frames 의 mass_data_map 조회가 맞는다.
            source_order = self._apply_source_arrangement(arranged, options)
            options["temperature"] = {"groups": arranged["groups"],
                                      "limits_file": arranged["limits_file"]}
            # bin_map(.lt/.pds)은 세션에 싣지 않는다 — 업로드 전 정리에서만 쓰고 소진한다.
            # serial_match: 좌표 없는 rawdata 를 SERIAL 순서로 짝지으라는 사용자 확인 결과
            # (_temp_coord_check). 정리 지시일 뿐이라 세션 옵션에는 싣지 않는다.
            temperature = {"groups": arranged["groups"], "bin_map": arranged["bin_map"],
                           "serial_match": bool(arranged.get("serial_match"))}
        elif mode == "DUT":
            colors = self._ask_dut_colors()      # 이름·순서는 서버가 정한다 — 색만
            if colors:
                options["colors"] = colors
        else:
            arranged = self._ask_source_names()
            if arranged is not None:
                source_order = self._apply_source_arrangement(arranged, options)
        return {
            "work_group": self.group,
            "selected": list(self.group.subjects()),
            "sheets": list(SHEET_OPTIONS),
            "compare_mode": (mode == "Compare"),
            "mode": mode,
            "options": options,
            # 업로드 순서 — 서버 tables 순서가 곧 이 순서이고 tables[0] 이 limit 기준이다.
            # Compare 는 After 먼저, 그 외 모드는 SourceNameDialog 의 표 순서 그대로
            # (Temperature 는 자동 배치가 그룹마다 RT → CT → HT 로 정렬해 둔다).
            "source_order": source_order,
            # Temperature 모드 rawdata 정리 지시 (그룹 + .lt/.pds bin 매핑). 그 외 모드는 None.
            "temperature": temperature,
            # 배치창 동안 돌린 선행 인코딩 (없으면 None — 조립이 전량 인코딩한다).
            "prefetch": getattr(self, "_encode_prefetch", None),
        }

    def _ask_compare_groups(self):
        """Compare 모드 Before/After 배치. 취소면 None.

        업로드 순서가 [After…, Before…] 가 되므로 서버의 ``tables[0]`` = After 최상단이고,
        web_report 가 쓰는 limit(HiLIM/LoLIM) 기준·goodlog 의 after 대표가 그 source 가 된다.
        """
        from honey_ui.compare_arrange_dialog import CompareArrangeDialog
        dlg = CompareArrangeDialog(self, list(self.group.names()))
        if not dlg.exec():
            self._status("Compare 배치 취소")
            return None
        return dlg.result_groups()

    def _dut_source_names(self):
        """DUT 모드에서 **서버가 만들 source 목록**(DUT 라벨 순서)을 미리 계산한다.

        클라는 DUT 분할을 하지 않지만(서버 honeyform.split_table_by_dut 소관), 색은
        source 순서 i 에 붙으므로 색을 지정하려면 그 목록을 알아야 한다. 라벨·정렬은
        서버 분할과 **같은 함수**(web_report.honeyform.dut_labels)로 얻는다 — 규칙이
        갈리면 색이 밀린다. DUT 종류가 1개 이하면 서버가 분할하지 않으므로 원본 이름
        그대로다. 계산 실패는 None — 호출부가 창을 건너뛴다(색은 옵션 팔레트 그대로).
        """
        try:
            from web_report.honeyform import DATA_START_ROW, dut_labels

            names = list(self.group.names())
            md = self.group.mass_data_map[names[0]]
            df = md.to_df() if hasattr(md, "to_df") else md.df
            labels = dut_labels(df.iloc[DATA_START_ROW:])
        except Exception:                                  # noqa: BLE001
            return None
        return [f"DUT {label}" for label in labels] if len(labels) > 1 else names

    def _ask_dut_colors(self):
        """DUT 모드 색 지정 창. 바꾼 색만 돌려준다 (취소·건너뜀이면 None).

        이름·순서는 서버가 DUT 값으로 정하므로 결과에서 ``colors`` 만 쓴다.
        """
        from honey_ui.source_name_dialog import SourceNameDialog

        names = self._dut_source_names()
        if not names:
            return None
        src = list(self.csv_paths)
        entries = [(name, str(src[0]) if src else "") for name in names]
        dlg = SourceNameDialog(self, entries, mode="DUT")
        if not dlg.exec():
            return None
        return dlg.result_arrangement().get("colors")

    def _source_file_name(self, md, fallback):
        """이 source 를 만든 대표 입력 파일의 파일명. 없으면 '<legend>.parquet'.

        경로 조회 우선순위는 ``source_name_dialog.source_display_path`` 한 곳에 모아둔다
        (MDDI 병합이 이식되면 그 함수만 사실이 되면 된다 — 여기는 손대지 않는다).
        """
        from honey_ui.source_name_dialog import source_display_path

        src = source_display_path(md, "")
        return Path(src).name if src else f"{fallback}.parquet"

    def _lot_id_from_sources(self, work_group):
        """첫 source 파일명에서 LOT ID 를 뽑는다. 없으면 빈 문자열.

        legend 와 같은 product_type 별 규칙(source_naming)을 먼저 본다 — head('_' 앞 토큰)
        가 LOT 이 아닌 제품군이 있어서다: PMIC 은 뜻 없는 접두가 붙고
        ('awjkelf_602XX2_3_….std'), PDDI 는 'stdf_' 접두라 head 가 'stdf' 가 된다.
        규칙이 안 맞으면 종전 head 폴백 — 예: 'N4XA123_up_a.parquet' → 'N4XA123'.
        파싱 실패는 best-effort 로 '' 반환.
        """
        from honey_ui.source_naming import lot_id_for

        try:
            names = work_group.names()
            if not names:
                return ""
            md = work_group.mass_data_map[names[0]]
            file_name = self._source_file_name(md, names[0])
            lot = lot_id_for(file_name, self.product_type())
            if lot:
                return lot
            return Path(file_name).stem.split("_")[0].strip()
        except Exception:
            return ""

    def _build_webreport_parquets(self, work_group, order=None, temperature=None,
                                  cache=None):
        from honey_ui.source_name_dialog import source_file_info

        items = []
        sources = []
        # 중복 항목명 자동 개명 내역 — 워커 스레드에서 도는 함수라 여기서 다이얼로그를 띄우면
        # 안 된다. 모아만 두고 UI 스레드(_run_web_report)가 인코딩 완료 후 안내한다.
        self._webreport_dup_renames = []
        self._temperature_clean_log = []
        # order: 업로드 순서 지정(Compare 모드의 After→Before, Temperature 의 RT→CT→HT).
        # 서버는 이 순서를 그대로 tables 순서로 쓰므로 tables[0] 이 limit 기준 source 가 된다.
        names = list(order) if order else work_group.names()
        # Temperature 모드: 인코딩 **전에** rawdata 를 정리한다 (CT/HT 를 RT pass 좌표로
        # 자르고 RT limit 으로 재판정). dist pack 은 인코딩된 parquet 으로 만들어지므로
        # 여기서 정리하면 pack 도 자동으로 정리본과 일치한다.
        cleaned = self._clean_temperature_frames(work_group, names, temperature)
        for idx, name in enumerate(names):
            md = work_group.mass_data_map[name]
            # honey_parse 산출물(7-meta honeyform)이 곧 parquet 소스다. 원본 파일을 디스크에서
            # 다시 읽지 않는다 — 여러 input 의 병합이 honey_parse 안에서 일어나므로, 원본을
            # 재-read 하면 병합 결과를 버리고 파일 1개만 올리게 된다.
            df = md.to_df() if hasattr(md, "to_df") else md.df
            # 선행 인코딩 캐시는 원본 df 기준(raw)과 Temperature 정리본 기준(cleaned)이
            # 다른 풀이라, 이 source 를 정리했는지로 풀을 고른다. RT·미배정 source 는
            # clean_frames 가 입력 프레임 객체를 그대로 돌려주고 _clean_temperature_frames
            # 가 `is not` 로 걸러내므로 cleaned 에 들어오지 않는다 → 항상 raw 풀이다.
            changed = name in cleaned
            if changed:
                df = cleaned[name]
            pool = (cache or {}).get("cleaned" if changed else "raw") or {}
            hit = pool.get(id(md))
            file_name = self._source_file_name(md, name)
            if hit is not None:
                # 배치창이 떠 있는 동안 미리 만들어 둔 것 (_encode_sources_worker).
                # 이름·순서는 아래 메타에만 쓰이므로 재인코딩이 필요 없다.
                data, renames = hit
            else:
                # encode 안에서도 같은 개명이 돌지만(멱등), 무엇이 바뀌었는지 사용자에게
                # 알리려면 여기서 미리 호출해 목록을 받아둬야 한다.
                df, renames = dedupe_item_columns(df)
                try:
                    data = encode_honeyform_parquet(df)
                except ValueError as exc:
                    # 어느 source 가 걸렸는지 없으면 사용자가 원인 파일을 못 찾는다.
                    raise ValueError(
                        f"[{file_name}] honeyform 형식이 맞지 않습니다.\n{exc}") from exc
            self._webreport_dup_renames += [(file_name, old, new) for old, new in renames]
            items.append({
                "index": idx,
                "name": name,
                "file_name": f"{Path(file_name).stem or name}.parquet",
                "data": data,
            })
            sources.append({
                "index": idx,
                "name": name,
                "file_name": file_name,
                # 세션 상세 ℹ(Input File Information) 모달이 쓰는 입력 파일 정보.
                # 조회 규칙은 source_name_dialog 한 곳에 모여 있고(동결 영역 안전 조회),
                # 모르는 항목은 키 자체가 없다 — 서버/화면은 없는 키를 '-' 로 그린다.
                **source_file_info(md),
            })
        return sources, items

    def _clean_temperature_frames(self, work_group, names, temperature):
        """Temperature 모드 rawdata 정리 → {source: 정리된 df}. 그 외 모드는 빈 dict.

        정리 규칙(단일 진실)은 ``web_report.temperature.clean_frames`` 다 — 여기서는
        honey_parse 산출물(md.df)을 건드리지 않도록 **컬럼 개명 전 프레임을 모아 넘기고**
        결과만 받는다. 워커 스레드에서 도는 함수라 다이얼로그를 띄우지 않고, 통계는
        모아뒀다가 UI 스레드가 실행 로그에 출력한다.

        ``serial_match`` 는 좌표 없는 rawdata 를 SERIAL 순서로 짝지으라는 **사용자 확인
        결과**다(_temp_coord_check). 확인 없이 켜면 안 되므로 여기서 추정하지 않는다.
        """
        if not temperature or not temperature.get("groups"):
            return {}
        from web_report.temperature import clean_frames, format_stats

        frames = {}
        for name in names:
            md = work_group.mass_data_map[name]
            frames[name] = md.to_df() if hasattr(md, "to_df") else md.df
        cleaned, stats = clean_frames(frames, temperature["groups"],
                                      temperature.get("bin_map"),
                                      bool(temperature.get("serial_match")))
        self._temperature_clean_log = format_stats(stats)
        # RT 는 정리 대상이 아니라 원본 객체 그대로 돌아온다 — 바뀐 것만 남긴다.
        return {name: df for name, df in cleaned.items() if df is not frames[name]}

    def _log_temperature_cleanup(self):
        """Temperature rawdata 정리 통계를 실행 로그에 남긴다 (UI 스레드 전용)."""
        lines = getattr(self, "_temperature_clean_log", None)
        if lines:
            self._append_run_log("Temperature rawdata 정리 (RT 기준 재판정):\n"
                                 + "\n".join(f"· {line}" for line in lines))

    def _warn_duplicate_items(self):
        """중복 항목명 자동 개명이 있었으면 안내한다 (UI 스레드 전용). 업로드는 계속 진행."""
        renames = getattr(self, "_webreport_dup_renames", None)
        if not renames:
            return
        lines = [f"· [{fn}] {old} → {new}" for fn, old, new in renames]
        self._append_run_log("중복 항목명 자동 변경:\n" + "\n".join(lines))
        # 개명이 수백 건이 되면 QMessageBox 본문으로는 읽을 수 없다(구 구현은 20줄에서
        # 잘랐다) — 검색·정렬되는 표로 전량을 보여준다.
        from honey_ui.table_list_dialog import TableListDialog
        TableListDialog(
            self, "항목명 중복 자동 변경",
            f"측정 항목 이름이 중복되어 {len(renames)}건을 자동으로 바꿨습니다. "
            "같은 이름의 두 번째 항목부터 _2, _3 이 붙습니다 "
            "(첫 번째 항목의 이름은 그대로입니다).",
            ("source", "원본 항목명", "바뀐 이름"),
            [[fn, old, new] for fn, old, new in renames],
            csv_name="duplicate_items.csv").exec()

    def _run_web_report(self, work_group, selected, sheets, compare_mode=False, options=None,
                        mode="Normal", source_order=None, temperature=None, prefetch=None):
        # 실행 버튼 잠금/해제는 호출부(on_web_report)의 try/finally 가 전담한다.
        self._init_run_log("Web Report 생성")
        _begin_operation("web_report")   # 이 작업의 요청·오류를 서버에서 한 줄로 묶는다
        progress = _ElapsedProgress(
            self.progress_status, "Web Report 준비 중...", self._status,
            busy=True, minimum=0, maximum=100,
            mirror=getattr(self, "panel_progress", None))
        QApplication.processEvents()

        # 분석·인코딩(수 초)을 업로드 메타 입력과 병렬로 미리 시작한다 — 같은 워커 1개에서
        # 순차 실행이라 work_group 동시 접근이 없고, 사용자가 대화상자를 입력하는 동안
        # 대부분 끝난다. 취소 시 결과는 버린다 (읽기 전용 계산이라 부작용 없음).
        #
        # ⚠️ 제출 순서 = 실행 순서(워커 1개 FIFO)다. 업로드에 **필요한** 인코딩·분포 pack 을
        # 먼저 돌리고, 화면 요약 표시에만 쓰이는 analyze 를 마지막에 둔다 — analyze 결과는
        # 어차피 대화상자 이후에야 소비되므로(_show_summary), 앞에 두면 가장 무거운 분포
        # pack 이 대화상자 시간을 못 쓰고 그 뒤로 밀린다(source 가 많을수록 손해가 크다).
        #
        # 배치 다이얼로그 동안 인코딩을 미리 돌린 prefetch 가 있으면 **그 executor 를 그대로
        # 이어 쓴다** — 새 풀을 만들면 선행 job 과 조립 job 이 다른 스레드에서 동시에 돌아
        # _webreport_dup_renames 누적이 인터리브되고, FIFO 전제도 깨진다.
        prep_ex = (prefetch.executor if prefetch is not None
                   else concurrent.futures.ThreadPoolExecutor(max_workers=1))

        def _encode_stage():
            # 캐시는 **워커 안에서** 늦게 해석한다(_dist_after_encode 와 같은 패턴) —
            # 같은 FIFO 풀이라 선행 job 은 이미 끝나 있어 블로킹이 없고, 실패·취소면
            # 빈 캐시가 돌아와 종전 경로(전량 인코딩)를 그대로 탄다.
            cache = prefetch.cache_or_empty() if prefetch is not None else None
            # 선행 인코딩이 얼마나 벌어 줬는지는 이 구간의 벽시계로만 잰다
            # (HONEY_FLOW_PROFILE=1 — 적중률이 높을수록 0 에 가까워진다).
            with _flow_time("webreport.encode"):
                return self._build_webreport_parquets(work_group, source_order, temperature,
                                                      cache=cache)
        fut_encode = prep_ex.submit(_encode_stage)

        # Distribution pack 프리컴퓨트 — 서버가 영구 저장해 조회·재조회 모두 재정렬 없이
        # 서빙한다. 같은 워커 1개에서 인코딩 완료 후 순차 실행되므로 fut_encode.result() 는
        # 즉시 반환된다. 실패해도 업로드는 계속(서버 폴백)이라 결과는 best-effort 로만 쓴다.
        # 단계 진행은 queue 로 받아 진행바 라벨에 반영(UI 스레드 직접접근 금지).
        _dist_stage_q = queue.Queue()

        def _dist_after_encode():
            sources_, items_ = fut_encode.result()
            return _build_webreport_dist_pack(items_, sources_, selected, mode,
                                              stage_cb=_dist_stage_q.put)
        fut_dist = prep_ex.submit(_dist_after_encode)

        fut_analyze = prep_ex.submit(
            rg.analyze,
            work_group,
            meta=rg.ReportMeta(),
            selector=rg.ItemSelector(selected_items=selected),
            compare_mode=compare_mode,
        )

        def _drain_dist_stage():
            msg = None
            while True:
                try:
                    msg = _dist_stage_q.get_nowait()
                except queue.Empty:
                    break
            if msg is not None:
                progress.set(msg, status=msg)

        defaults = dict(self._last_upload or {})
        defaults["product_type"] = self.product_type()
        # source 파일명 head('_' 앞) 를 LOT ID 로 자동 채움 (직전 업로드 값보다 우선,
        # 사용자가 다이얼로그에서 수정 가능). 뽑히지 않으면 기존 defaults 유지.
        _lot_id = self._lot_id_from_sources(work_group)
        if _lot_id:
            defaults["lot_id"] = _lot_id
        # 세션 이름 기본값 = 메인창 Save Name(비어 있으면 자동 제안값) — 다이얼로그
        # 맨 위 Save Name 칸에 미리 채워 업로드 직전에 한 번 더 고칠 수 있다.
        defaults["file_name"] = (self.le_outname.text().strip()
                                 or _suggest_base_name(self.csv_paths, work_group))
        # Web Report 업로드는 PIN 입력을 요구하지 않는다 (비밀번호 행 숨김).
        dlg = UploadDialog(self, defaults=defaults, show_password=False)
        if not dlg.exec():
            progress.fail("취소됨: 업로드 메타 입력 취소")
            prep_ex.shutdown(wait=False, cancel_futures=True)
            return
        meta = dlg.values()
        self._last_upload = meta

        # 대기 순서도 제출 순서와 같게 둔다 — 워커가 FIFO 라, 뒤에 제출한 것을 먼저 기다리면
        # 진행바가 그 앞 단계의 소요를 엉뚱한 라벨로 표시하고 분포 단계 메시지(n/총 chunk)도
        # 못 보여준다.
        try:
            progress.set("parquet 인코딩 중...", value=10, status="parquet 인코딩 중...")
            sources, parquet_items = _wait_for_future(fut_encode, progress,
                                                      cancelled=self._op_cancel_requested)
        except _OperationCancelled:
            self._cancel_web_report(progress, prep_ex)
            return
        except Exception as exc:
            progress.fail(f"실패: parquet 인코딩 실패 - {exc}")
            prep_ex.shutdown(wait=False, cancel_futures=True)
            _show_exc(self, "Web Report 실패", exc,
                      prefix="업로드할 데이터를 만드는 중 오류가 발생했습니다.")
            self._status("parquet 인코딩 실패")
            return
        self._log_temperature_cleanup()
        self._warn_duplicate_items()

        dist_pack = None
        try:
            progress.set("분포 데이터 생성 중...", value=30, status="분포 데이터 생성 중...")
            dist_pack = _wait_for_future(fut_dist, progress, poll_cb=_drain_dist_stage,
                                         cancelled=self._op_cancel_requested)
        except _OperationCancelled:
            self._cancel_web_report(progress, prep_ex)
            return
        except Exception as exc:
            # 프리컴퓨트 실패는 업로드를 막지 않는다 — 서버가 첫 조회 때 폴백 계산한다.
            self._append_run_log(f"분포 프리컴퓨트 생략(서버 폴백 계산): {exc}")

        try:
            progress.set("데이터 분석 중... (Web Report)", value=38, status="데이터 분석 중...")
            self.last_result = _wait_for_future(fut_analyze, progress,
                                                cancelled=self._op_cancel_requested)
            self._show_summary(self.last_result)
        except _OperationCancelled:
            self._cancel_web_report(progress, prep_ex)
            return
        except Exception as exc:
            progress.fail(f"실패: 분석 실패 - {exc}")
            prep_ex.shutdown(wait=False, cancel_futures=True)
            _show_exc(self, "분석 실패", exc,
                      prefix="데이터 분석 중 오류가 발생했습니다.")
            self._status("Web Report 분석 실패")
            return
        prep_ex.shutdown(wait=False)

        manifest = {
            "sources": sources,
            "meta": meta,
            "client": client_identity.collect(),
            "selected_items": list(selected or []),
            "sheets": list(sheets or []),
            "options": options or {},
            "mode": mode or "Normal",
        }
        # Temperature: 항목별 fail bin (.lt/.pds 유래) — 서버 Temp 시트의 Bin 표기용.
        # **options 가 아니라 manifest 최상위**에 싣는다: options 문자열은 세션에 그대로
        # 저장돼 조회 캐시 키(cache_policy.report_key)의 원소가 되므로, 수백 KB 매핑을
        # 넣으면 매 조회마다 그만큼을 해싱하게 된다. 값·limit 은 서버가 RT 메타로 다시
        # 판정하므로 bin 2개 + TNO 만 남긴다(없으면 서버가 관측 bin 으로 폴백).
        limits = _slim_temperature_limits(temperature)
        if limits:
            manifest["temperature_limits"] = limits

        # 여기부터는 취소 불가 — 업로드는 비멱등이라 중간에 끊어도 서버가 계속 처리해
        # 세션이 생길 수 있다(아래 실패 안내문과 같은 이유). 버튼을 내려 알린다.
        self._end_op_cancel()

        _on_upload_progress, _drain_upload_progress = _upload_progress_channel(
            progress, "Web Report 업로드 중... ({pct}%)",
            value_map=lambda pct: 40 + int(pct * 0.6))

        # 전송 시간 / 서버 대기 시간 — 서버가 볼 수 없는 구간이라 여기서만 알 수 있다
        # (uploader.post_webreport docstring 참조).
        timing = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(
                    uploader.post_webreport,
                    manifest,
                    parquet_items,
                    progress_cb=_on_upload_progress,
                    dist_pack=dist_pack,
                    timing=timing,
                )
                result = _wait_for_future(fut, progress, poll_cb=_drain_upload_progress)
        except Exception as exc:
            progress.fail(f"실패: Web Report 업로드 실패 - {exc}")
            # 연결 거부·read timeout 은 서버에 요청 자체가 닿지 않아 서버 로그에 아무것도
            # 남지 않는다 — 그런 실패는 이 보고만이 유일한 기록이다.
            event_id = _report_error(
                "honey_upload_fail", f"{type(exc).__name__}: {exc}",
                context={"product": meta.get("product", ""), "lot": meta.get("lot_id", ""),
                         "sources": len(parquet_items), **timing})
            self._append_run_log(_upload_timing_line(timing, "실패"))
            # 업로드는 멱등이 아니다 — 클라가 read timeout 으로 끊어도 서버는 계속 처리해
            # 세션이 생길 수 있다. 그대로 재시도하면 같은 데이터로 세션이 두 벌 생기므로
            # 목록 확인을 먼저 안내한다.
            _show_exc(self, "Web Report 업로드 실패", exc,
                      prefix="서버에 업로드하지 못했습니다. 네트워크와 서버 상태를 확인해 주세요.\n"
                             "다시 올리기 전에 검색결과 목록을 먼저 확인해 주세요 — "
                             "서버에는 이미 저장돼 있을 수 있습니다."
                             + (f"\n오류번호: {event_id}" if event_id else ""))
            self._status("Web Report 업로드 실패")
            return

        sid = result.get("session_id", "?")
        url = result.get("web_report_url")
        if url and str(url).startswith("/"):
            url = SERVER_BASE_URL.rstrip("/") + str(url)
        elif not url:
            url = f"{SERVER_BASE_URL.rstrip('/')}/pe/report/web_report/{sid}"

        progress.success(f"Web Report 완료: session_id {sid}", value=100)
        self._append_run_log(_upload_timing_line(timing, "완료"))
        # 성공했어도 서버 대기가 길었으면 알린다 — 그게 다음 번 타임아웃의 예보다.
        # (실패만 보고하면 임계 직전 상태를 영영 못 본다.)
        if timing.get("wait_sec", 0) >= _UPLOAD_SLOW_WAIT_SEC:
            _report_error("honey_upload_slow",
                          f"서버 응답 대기 {timing.get('wait_sec')}s",
                          context={"product": meta.get("product", ""),
                                   "lot": meta.get("lot_id", ""),
                                   "sources": len(parquet_items), "session": sid, **timing})
        self._append_run_log(f"Web Report URL: {url}")
        self._status(f"Web Report 완료: {sid}")
        # 완료 팝업 없이 바로 내장 브라우저(웹 화면)로 전환하고 입력/설정 창을 닫는다.
        self._open_in_embedded(url)
        panel = getattr(self, "slide_controls", None)
        if panel is not None:
            panel.hide_animated()

    def _open_in_embedded(self, url):
        """내장 브라우저(있으면)로 url 이동. 없으면 외부 브라우저 폴백."""
        panel = getattr(self, "browser_panel", None)
        if panel is not None:
            panel.view.load(QUrl(url))
            return
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _run_analysis(self, work_group, selected, sheets, raw_data=False,
                      compare_mode=False, mode_map=False):
        # 실행 버튼 잠금/해제는 호출부(on_start)의 try/finally 가 전담한다.
        show_timing_log = bool(getattr(rg, "DEBUG_RUN_TIMING_LOG", False))
        overall_t0 = time.perf_counter()
        self._init_run_log("")
        self._set_run_log_total(
            self._estimate_run_log_steps(work_group, sheets, raw_data)
            if show_timing_log else 0
        )
        profile_events = queue.Queue()

        def _profile_cb(event):
            profile_events.put(event)

        profile_cb = _profile_cb if show_timing_log else None

        def _drain_profile_events():
            if profile_cb is None:
                return
            while True:
                try:
                    event = profile_events.get_nowait()
                except queue.Empty:
                    break
                self._log_profile_event(event)

        # 진행바를 무거운 작업(raw_frames 포함) 시작 전에 먼저 띄운다 — max 는 raw 길이가
        # 정해진 뒤 set_maximum 으로 확정한다(그 전엔 임시값). 패널에도 미러링.
        progress = _ElapsedProgress(
            self.progress_status, "분석 준비 중...", self._status,
            busy=True, minimum=0, maximum=1,
            mirror=getattr(self, "panel_progress", None))
        QApplication.processEvents()

        # Raw Data 시트용 원본 프레임 (체크 시) — source별 df_honey 적재 포맷 그대로.
        # 대용량이면 수 초~수십 초라 UI 스레드에서 직접 돌리면 프리즈 → 스레드+폴링으로 감싼다.
        raw = None
        if raw_data:
            try:
                raw_t0 = time.perf_counter()
                progress.set("Raw Data 준비 중...", status="Raw Data 준비 중...")
                with _flow_time("raw_frames"):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                        raw = _wait_for_future(ex.submit(work_group.raw_frames), progress)
                if show_timing_log:
                    self._append_run_log(
                        f"raw_frames done: {time.perf_counter() - raw_t0:.2f}s",
                        advance=True)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Raw Data 생략",
                                    f"원본 데이터 시트를 만들지 못해 건너뜁니다:\n{exc}")
                self._append_run_log(f"raw_frames ERROR - {exc}", advance=True)
                raw = None
        # 진행 단계: 준비(1) → 분석(1) → 요약(1) → 시트별(N, +Raw N) → 저장 마무리(1)
        total = len(sheets) + 4 + (len(raw) if raw else 0)
        progress.set_maximum(total)
        progress.set("분석 준비 중...", value=0, status="분석 준비 중...")

        def _step(value, label):
            progress.set(label, value=value, status=label)

        # 1) 데이터 검증/준비
        _step(1, "데이터 검증/준비 중...")

        # 2) 데이터 분석 (통계 · Bin 집계)
        progress.set("데이터 분석 중... (통계 · Bin 집계)", status="데이터 분석 중...")
        try:
            analyze_t0 = time.perf_counter()
            with _flow_time("rg.analyze.total"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(
                        rg.analyze,
                        work_group,
                        meta=rg.ReportMeta(),
                        selector=rg.ItemSelector(selected_items=selected),
                        profile_cb=profile_cb,
                        compare_mode=compare_mode,
                    )
                    self.last_result = _wait_for_future(fut, progress, poll_cb=_drain_profile_events)
            _drain_profile_events()
            if show_timing_log:
                self._append_run_log(f"Analysis total: {time.perf_counter() - analyze_t0:.2f}s")
        except Exception as exc:
            _drain_profile_events()
            self._append_run_log(f"Analysis ERROR - {exc}")
            progress.fail(f"실패: 분석 실패 - {exc}")
            _show_exc(self, "분석 실패", exc,
                      prefix="데이터 분석 중 오류가 발생했습니다.")
            self._status("분석 실패")
            return
        progress.set("데이터 분석 완료", value=2)

        # 3) 요약 작성
        progress.set("요약 작성 중...", status="요약 작성 중...")
        self._show_summary(self.last_result)
        progress.set("요약 작성 완료", value=3)

        base = self.le_outname.text().strip() or _suggest_base_name(self.csv_paths, self.group)
        out = _build_output_path(Path(self.csv_paths[0]).parent, base)

        # 4) 시트/차트 생성 (시트 1개당 1스텝, offset 3)
        progress_events = queue.Queue()

        def _sheet_progress(done, total_s, name):
            progress_events.put(("sheet", done, total_s, name))

        _dist_state = {"base": 0, "n": 0}

        def _dist_progress(done, n_charts):
            progress_events.put(("dist", done, n_charts, None))

        _attach_state = {"base": 0, "last_log": {}}

        def _attach_progress(event, sheet_name, subject, done=None, total=None):
            payload = {
                "event": event,
                "sheet_name": sheet_name,
                "subject": subject,
                "done": done,
                "total": total,
            }
            progress_events.put(("attach", payload, None, None))

        def _drain_progress_events():
            while True:
                try:
                    kind, a, b, c = progress_events.get_nowait()
                except queue.Empty:
                    break
                if kind == "sheet":
                    done, total_s, name = a, b, c
                    if name == "distribution":
                        continue  # _dist_progress 가 처리
                    progress.set(
                        f"시트/차트 생성 중... ({name})   {done}/{total_s}",
                        value=3 + done,
                        status=f"시트 생성 중... ({name})  {done}/{total_s}",
                    )
                elif kind == "dist":
                    done, n_charts = a, b
                    if _dist_state["n"] == 0 and n_charts:
                        _dist_state["base"] = progress.value()
                        _dist_state["n"] = n_charts
                        progress.set_maximum(progress.maximum() + n_charts - 1)
                    pct = done * 100 // n_charts if n_charts else 100
                    value = _dist_state["base"] + done if n_charts else progress.value()
                    progress.set(
                        f"Distribution 차트 생성 중... ({done}/{n_charts} - {pct}%)",
                        value=value,
                        status=f"Distribution {pct}%  ({done}/{n_charts})",
                    )
                elif kind == "attach":
                    payload = a or {}
                    event = payload.get("event")
                    sheet_name = payload.get("sheet_name") or ""
                    subject = payload.get("subject") or ""
                    done = int(payload.get("done") or 0)
                    total_a = int(payload.get("total") or 0)
                    if event == "start":
                        if total_a:
                            _attach_state["base"] = progress.value()
                            progress.set_maximum(progress.maximum() + total_a)
                        _attach_state["last_log"][sheet_name] = 0
                        continue
                    if event == "progress":
                        pct = done * 100 // total_a if total_a else 100
                        value = _attach_state["base"] + done if total_a else progress.value()
                        msg = f"PNG 붙이는 중... ({sheet_name} {done}/{total_a} - {pct}%)"
                        progress.set(msg, value=value, status=msg)
                        continue
                    if event == "done":
                        continue
                    if event == "copy_picture":
                        msg = "Chart 복사 붙여넣기 진행중 잠시 기다려주세요"
                        progress.set(f"{msg} ({sheet_name}: {subject})", status=msg)

        progress.set(
            f"Excel 시트/차트 생성 중...  → {Path(out).name}",
            status=f"xlsx 생성 중... (Excel)  → {Path(out).name}",
        )
        # Map 옵션: 입력 파일별 wafer bin map PNG 생성 (matplotlib, COM 비의존).
        # 다수 wafer 는 수 초~수십 초라 UI 스레드에서 직접 돌리면 프리즈 → 스레드+폴링으로 감싼다.
        map_pngs, map_tmpdir = [], None
        if mode_map and map_report is not None:
            try:
                progress.set("Wafer Map 생성 중...", status="Wafer Map 생성 중...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    map_pngs, map_tmpdir = _wait_for_future(
                        ex.submit(map_report.build_map_pngs,
                                  work_group.mass_data_map, log_cb=self._append_run_log),
                        progress)
            except Exception as exc:  # noqa: BLE001
                self._append_run_log(f"Map 생성 ERROR - {exc}")
                map_pngs, map_tmpdir = [], None
        try:
            colors = chart_colors.load_colors()

            def _write_job():
                com_module = _init_com_for_worker()
                try:
                    with _flow_time("xlsx_writer.write.total"):
                        out_path = xlsx_writer.write(
                            self.last_result, out, sheets=sheets,
                            colors=colors,
                            progress_cb=_sheet_progress, raw_sheets=raw,
                            dist_progress_cb=_dist_progress,
                            attach_progress_cb=_attach_progress,
                            profile_cb=profile_cb,
                        )
                    # Map 옵션: xlsx 생성 완료 후 별도 xlwings 세션으로 Map 시트 부착.
                    # (report_generator 는 map 무관 — 구서버 교체 대비. 같은 COM-init 스레드.)
                    if map_pngs and map_report is not None:
                        with _flow_time("map_report.attach_map_sheet"):
                            map_report.attach_map_sheet(out_path, map_pngs)
                    return out_path
                finally:
                    _co_uninitialize(com_module)

            write_t0 = time.perf_counter()
            with _flow_time("xlsx_generation.total_wait"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(_write_job)
                    _wait_for_future(
                        fut,
                        progress,
                        poll_cb=lambda: (_drain_profile_events(), _drain_progress_events()),
                    )
            _drain_profile_events()
            if show_timing_log:
                self._append_run_log(f"XLSX write total: {time.perf_counter() - write_t0:.2f}s")
        except Exception as exc:
            _drain_profile_events()
            self._append_run_log(f"XLSX write ERROR - {exc}")
            progress.fail(f"실패: xlsx 생성 실패 - {exc}")
            _show_exc(self, "생성 실패", exc,
                      prefix="리포트 xlsx 를 만들지 못했습니다.")
            self._status("xlsx 생성 실패")
            return
        finally:
            if map_tmpdir:
                shutil.rmtree(map_tmpdir, ignore_errors=True)
        _drain_progress_events()

        # 5) Excel 파일 저장 마무리
        _step(progress.maximum(), "Excel 파일 저장 마무리 중...")
        self.out_path = out
        if show_timing_log:
            self._append_run_log(f"Overall total: {time.perf_counter() - overall_t0:.2f}s")
        self._append_run_log(f"저장됨: {out}")
        progress.success(f"완료: {Path(out).name} 저장됨", value=progress.maximum())
        self._status(f"완료: {Path(out).name}  ('서버에 업로드' 가능)")

    def _show_summary(self, r):
        feat = r.summary_feature()
        lines = [
            "",
            "=== Summary ===",
            f"Sources: {', '.join(r.sources)}",
            f"Total: {r.total_dut}    Pass(Bin1): {feat['Pass (Bin 1)']}  ({r.pass_yield}%)",
            "",
            "[Major Fail Bins]",
        ]
        for i, b in enumerate(r.major_fail_bins(), start=1):
            lines.append(f"  {i}. bin {b.get('bin')}  -  {b.get('Main Fail subject')}  ({b.get('avg')}%)")
        current = self.txt_summary.toPlainText()
        if current.strip():
            self.txt_summary.append("\n".join(lines))
        else:
            self.txt_summary.setPlainText("\n".join(lines))
        bar = self.txt_summary.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ── 서버 업로드 ─────────────────────────────────────────────────────────
    def on_upload_local(self):
        """로컬에 있는 임의의 xlsx 를 직접 업로드 (분석 엔진 불필요).

        최신 Windows 탐색기(네이티브) 파일 열기 대화상자 사용 — DontUseNativeDialog
        를 주지 않아 OS 기본 다이얼로그가 뜬다.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "업로드할 파일 선택", "",
            "Excel (*.xlsx);;모든 파일 (*.*)")
        if path:
            self._do_upload(path)

    def product_type(self):
        """메인 UI 에서 선택된 Product Type (라디오). 기본 MDDI."""
        for key, rb in self._pt_radios.items():
            if rb.isChecked():
                return key
        return "MDDI"

    def _save_product_type(self, *_):
        """Product Type 선택을 사용자별 설정에 저장 (다음 실행 때 복원)."""
        app_settings.set_setting("product_type", self.product_type())

    def _sync_temperature_mode(self, *_):
        """Temperature 분석 모드 라디오는 PMIC / SECURITY 를 골랐을 때만 보인다.

        다른 제품군으로 바꿀 때 Temperature 가 선택돼 있으면 Normal 로 되돌린다
        (숨은 라디오가 선택된 채 남으면 업로드 모드가 조용히 어긋난다).
        """
        rb = (getattr(self, "_mode_radios", None) or {}).get("Temperature")
        if rb is None:
            return
        allowed = self.product_type() in _TEMPERATURE_PRODUCT_TYPES
        rb.setVisible(allowed)
        if not allowed and rb.isChecked():
            self._mode_radios["Normal"].setChecked(True)

    def _do_upload(self, path):
        """report_generator 보고서 xlsx → Raw Data 복원 → web_report 세션 생성.

        Excel COM 으로 Raw Data 시트를 7-meta honeyform parquet 으로 복원하고
        Summary/Issue_table 코멘트를 추출해, 기존 web_report 업로드 경로
        (post_webreport)로 전송한다 — 일반 web_report 세션과 동일한 세션이 만들어진다.

        전처리(COM 추출·변환·분포 프리컴퓨트)를 메타 입력 다이얼로그와 **병렬**로 미리
        시작한다(_run_web_report 와 같은 패턴) — 사용자가 입력하는 동안 대부분 끝난다.
        진행바는 시트 수 기반 실제 진행률을 표시한다(구: 경과시간만 나오는 무한 진행바).
        """
        name = Path(path).name
        self._set_busy(True)
        self._append_run_log(f"{name} 전처리/업로드 진행중입니다...")

        progress = _ElapsedProgress(
            self.progress_status, f"xlsx 전처리 중... {name}",
            self._status, busy=True, minimum=0, maximum=100,
            mirror=getattr(self, "panel_progress", None))
        QApplication.processEvents()

        # 전처리 선실행 — 시트 읽기/변환 진행은 q_prep, 분포 프리컴퓨트 단계는 _dist_stage_q.
        # 같은 워커 1개에서 순차 실행(prep → dist)이라 자원 경합이 없다.
        q_prep = queue.Queue()
        _dist_stage_q = queue.Queue()
        prep_ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut_prep = prep_ex.submit(_prepare_report_webreport, path, progress_cb=q_prep.put)

        def _dist_after_prep():
            sources_, items_, _seed, all_items_, _report = fut_prep.result()
            return _build_webreport_dist_pack(items_, sources_, all_items_, "Normal",
                                              stage_cb=_dist_stage_q.put)
        fut_dist = prep_ex.submit(_dist_after_prep)

        def _drain():
            # 전처리(시트 읽기 5→30% / 변환 30→35%) — 마지막 값만 반영.
            last = None
            while True:
                try:
                    last = q_prep.get_nowait()
                except queue.Empty:
                    break
            if last is not None:
                stage, done, total, sheet = last
                frac = (done / total) if total else 0
                if stage == "read":
                    val, tag = 5 + int(25 * frac), "시트 읽는 중"
                else:
                    val, tag = 30 + int(5 * frac), "변환 중"
                msg = f"{tag} ({done}/{total}) {sheet}"
                progress.set(msg, value=val, status=msg)
            # 분포 프리컴퓨트 단계(문자열) — 38% 고정 라벨.
            msg = None
            while True:
                try:
                    msg = _dist_stage_q.get_nowait()
                except queue.Empty:
                    break
            if msg is not None:
                progress.set(msg, value=38, status=msg)

        # ── 메타 입력 다이얼로그 (전처리가 뒤에서 도는 동안) ──────────────────
        defaults = dict(self._last_upload or {})
        defaults["product_type"] = self.product_type()
        # 세션 이름 기본값 = 업로드한 xlsx 파일명 — 다이얼로그 맨 위 Save Name 칸에서 수정 가능.
        defaults["file_name"] = Path(path).stem
        dlg = UploadDialog(self, defaults=defaults, show_password=False)   # web_report=PIN 없음
        if not dlg.exec():
            prep_ex.shutdown(wait=False, cancel_futures=True)   # 대기 중인 dist 만 취소됨
            self._release_busy_after_cancel(
                fut_prep, progress, "취소됨: 업로드 메타 입력 취소")
            return
        meta = dlg.values()
        self._last_upload = meta

        # ── 전처리 결과 대기(실제 진행률) ─────────────────────────────────────
        progress.set(f"xlsx 전처리 중... {name}", busy=False)
        try:
            sources, parquet_items, seed, all_items, report = _wait_for_future(
                fut_prep, progress, poll_cb=_drain)
        except ValueError as exc:
            # report_flow 의 안내 ValueError — 안내문은 본문, 붙어 온 traceback 은 자세히로.
            progress.fail("실패: 파일 오류")
            prep_ex.shutdown(wait=False, cancel_futures=True)
            _show_exc(self, "파일 오류", exc)
            self._set_busy(False)
            return
        except Exception as exc:
            progress.fail("실패: xlsx 전처리 오류")
            prep_ex.shutdown(wait=False, cancel_futures=True)
            _show_exc(self, "전처리 실패", exc,
                      prefix="xlsx 전처리(Excel COM) 중 오류가 발생했습니다.")
            self._set_busy(False)
            return

        # ── 업로드 전 확인: 인식 시트 요약 + 미인식/FAILTNO 경고 (정확도 안전장치) ──
        if not self._confirm_upload_report(report):
            progress.fail("취소됨: 업로드 취소")
            prep_ex.shutdown(wait=False, cancel_futures=True)
            self._set_busy(False)
            return

        # Distribution pack 프리컴퓨트 대기 — 실패 시 미첨부(서버 폴백 계산), 업로드는 계속.
        dist_pack = None
        try:
            progress.set("분포 데이터 생성 중...", value=38, status="분포 데이터 생성 중...")
            dist_pack = _wait_for_future(fut_dist, progress, poll_cb=_drain)
        except Exception as exc:
            self._append_run_log(f"분포 프리컴퓨트 생략(서버 폴백 계산): {exc}")
        prep_ex.shutdown(wait=False)

        manifest = {
            "sources": sources,
            "meta": meta,
            "client": client_identity.collect(),
            "selected_items": all_items,
            "sheets": list(SHEET_OPTIONS),
            "options": {"colors": chart_colors.load_colors(), "ai_comment": False},
            "mode": "Normal",
        }
        # Summary/Issue_table 코멘트 시드 (issue_comments/etc_items/summary_engr).
        # ingest 가 seed_from_manifest 로 세션 편집 DB 에 복사한다.
        manifest.update(seed)

        # ── 서버 업로드 (byte 진행률 40→100%) ─────────────────────────────────
        _on_upload_progress, _drain_upload_progress = _upload_progress_channel(
            progress, f"서버 업로드 중... {name} ({{pct}}%)",
            value_map=lambda pct: 40 + int(pct * 0.6))

        timing = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(
                    uploader.post_webreport,
                    manifest,
                    parquet_items,
                    progress_cb=_on_upload_progress,
                    dist_pack=dist_pack,
                    timing=timing,
                )
                result = _wait_for_future(fut, progress, poll_cb=_drain_upload_progress)
        except Exception as exc:
            progress.fail(f"실패: 업로드 실패 - {exc}")
            # 이 경로만 서버 보고가 빠져 있어 xlsx 인제스트 업로드 실패는 흔적이 없었다
            # (_run_web_report 와 대칭을 맞춘다).
            event_id = _report_error(
                "honey_upload_fail", f"{type(exc).__name__}: {exc}",
                context={"path": "xlsx_ingest", "sources": len(parquet_items), **timing})
            self._append_run_log(_upload_timing_line(timing, "실패"))
            _show_exc(self, "업로드 실패", exc,
                      prefix="서버에 업로드하지 못했습니다. 네트워크와 서버 상태를 확인해 주세요.\n"
                             "다시 올리기 전에 검색결과 목록을 먼저 확인해 주세요 — "
                             "서버에는 이미 저장돼 있을 수 있습니다."
                             + (f"\n오류번호: {event_id}" if event_id else ""))
            self._status("업로드 실패")
            self._set_busy(False)
            return

        sid = result.get("session_id", "?")
        url = result.get("web_report_url")
        if url and str(url).startswith("/"):
            url = SERVER_BASE_URL.rstrip("/") + str(url)
        elif not url:
            url = f"{SERVER_BASE_URL.rstrip('/')}/pe/report/view/{sid}"

        progress.success(f"업로드 완료: session_id {sid}", value=100)
        self._append_run_log(_upload_timing_line(timing, "완료"))
        if timing.get("wait_sec", 0) >= _UPLOAD_SLOW_WAIT_SEC:
            _report_error("honey_upload_slow",
                          f"서버 응답 대기 {timing.get('wait_sec')}s",
                          context={"path": "xlsx_ingest", "session": sid, **timing})
        self._append_run_log(f"Web Report URL: {url}")
        self._status(f"업로드 완료: {sid}")
        # 완료 팝업 없이 내장 브라우저(웹 화면)로 바로 전환한다 (_run_web_report 와 동일).
        self._open_in_embedded(url)
        self._set_busy(False)

    def _confirm_upload_report(self, report):
        """전처리 결과를 요약해 업로드 전 확인받는다 (정확도 안전장치).

        인식된 Raw 시트(이름·행수·항목수) 요약 + 미인식 시트 경고 + FAILTNO 부재 고지.
        경고/고지가 있으면 Warning 아이콘. Ok=진행 / Cancel=중단. 반환: 진행 여부(bool).
        """
        report = report or {}
        raw = report.get("raw_sheets") or []
        skipped = report.get("skipped_sheets") or []
        converted = report.get("converted_5meta") or []

        lines = [f"인식된 Raw Data 시트: {len(raw)}개"]
        for r in raw[:10]:
            lines.append(f"  · {r['name']} — {r['rows']}행, 항목 {r['items']}개")
        if len(raw) > 10:
            lines.append(f"  · 외 {len(raw) - 10}개")

        if skipped:
            lines.append("")
            lines.append("⚠ 다음 시트는 Raw Data 형식이 아니어서 제외됩니다:")
            lines.append("  " + ", ".join(skipped))

        if converted:
            lines.append("")
            lines.append("⚠ 이 파일은 FAILTNO 정보가 없는 보고서 형식입니다.")
            lines.append("  웹 리포트의 Yield fail 분해 / Issue Table Yield 섹션이 비게 됩니다")
            lines.append("  (CPK · Distribution · Map · Pass 수율은 정확).")

        lines.append("")
        lines.append("업로드를 진행할까요?")

        box = QMessageBox(self)
        box.setWindowTitle("업로드 확인")
        box.setIcon(QMessageBox.Icon.Warning if (skipped or converted)
                    else QMessageBox.Icon.Information)
        box.setText("\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Ok
                               | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Ok)
        return box.exec() == QMessageBox.StandardButton.Ok

    # ── version check (사용자가 자동/수동 설치 선택) ────────────────────────
    def check_for_update(self):
        """fetch 는 백그라운드 스레드, 결과 처리는 _on_version_manifest 슬롯.

        fetch_latest 는 타임아웃 10s × 재시도 3회 + 백오프라 서버 무응답이면 30초
        이상 걸린다 — 메인 스레드에서 부르면 시작 직후 창이 그만큼 굳는다.
        """
        def _fetch_bg():
            try:
                result = version_check.fetch_latest()
            except Exception as exc:  # noqa: BLE001
                result = exc
            try:
                self._version_manifest_ready.emit(result)
            except RuntimeError:
                pass   # 창이 이미 닫혀 C++ 객체가 파괴된 경우
        threading.Thread(target=_fetch_bg, daemon=True,
                         name="honey-version-check").start()

    # ── 릴리스 공지 (버전당 1회) ──────────────────────────────────────────
    def _maybe_show_announcement(self):
        """이 버전의 공지를 아직 안 봤으면 서버에서 받아 팝업한다.

        '봤음' 기록은 %APPDATA%/Honey/settings.json 이라 Windows 계정별로 남는다
        — 같은 PC 라도 계정이 다르면 각각 1회 뜨고, 같은 계정이면 재실행해도 다시
        뜨지 않는다. fetch 는 버전 체크와 같은 이유(네트워크 대기)로 백그라운드.
        """
        if app_settings.get_setting("announcement_seen_version") == CURRENT_VERSION:
            return

        def _fetch_bg():
            try:
                text = version_check.fetch_announcement()
            except Exception:   # noqa: BLE001 - 공지 실패는 앱 동작과 무관, 다음 실행 때 재시도
                return
            try:
                self._announcement_ready.emit(text)
            except RuntimeError:
                pass   # 창이 이미 닫혀 C++ 객체가 파괴된 경우
        threading.Thread(target=_fetch_bg, daemon=True,
                         name="honey-announcement").start()

    def _on_announcement(self, text):
        """공지 원문을 그대로 보여주고 '봤음' 을 기록한다 (내용이 비면 아무 것도 안 함).

        기록은 팝업을 닫은 뒤에 한다 — 표시 전에 강제 종료되면 다음 실행 때 다시 뜨는
        편이 조용히 건너뛰는 것보다 낫다.
        """
        body = (text or "").strip()
        if not body:
            return
        box = QMessageBox(self)
        box.setWindowTitle(f"Honey {CURRENT_VERSION} 업데이트 안내")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(body)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
        app_settings.set_setting("announcement_seen_version", CURRENT_VERSION)

    def _on_download_cancel(self):
        """업데이트 다운로드 취소 버튼 — 워커의 progress_cb 가 다음 청크에서 중단한다.

        즉시 중단이 아니라 '요청' 이므로 버튼만 잠그고 안내한다 (청크 하나 분량 지연)."""
        self._dl_cancelled = True
        btn = getattr(self, "btn_progress_cancel", None)
        if btn is not None:
            btn.setEnabled(False)
        self.status.showMessage("업데이트 다운로드를 취소하는 중...")

    def _show_download_cancel(self):
        """취소 버튼 노출 + 배선.

        취소 버튼은 진행 dock(_build_log_dock) 안에 있는데, PyQtWebEngine 미설치 시
        _build_chrome 이 조기 return 해 dock 자체가 없다 — 그 폴백 화면에서는 취소 없이
        (종전대로) 다운로드만 진행한다."""
        btn = getattr(self, "btn_progress_cancel", None)
        if btn is None:
            return
        btn.setEnabled(True)
        btn.clicked.connect(self._on_download_cancel)
        btn.show()

    def _hide_download_cancel(self):
        """다운로드 종료(성공/실패/취소) 후 취소 버튼 정리 — 다음 작업에 남지 않도록."""
        btn = getattr(self, "btn_progress_cancel", None)
        if btn is None:
            return
        btn.hide()
        btn.setEnabled(True)
        try:
            btn.clicked.disconnect(self._on_download_cancel)
        except TypeError:
            pass   # 이미 끊겨 있음

    def _update_modal(self, text, cancellable=True):
        """업데이트 전용 모달 진행 대화상자. 실패해도 업데이트 자체는 계속된다.

        dock 진행바(progress_status)는 화면 하단이라 사용자가 못 보고 "멈췄다" 고
        인식한다. 업데이트는 수백 MB 라 대기가 길어 중앙 모달이 필요하다.
        """
        try:
            dlg = QProgressDialog(text, "취소", 0, 100, self)
            dlg.setWindowTitle("Honey 업데이트")
            dlg.setWindowModality(Qt.WindowModality.WindowModal)
            dlg.setMinimumWidth(420)
            dlg.setMinimumDuration(0)    # 바로 표시 (기본 4초 지연)
            dlg.setAutoClose(False)      # 100% 도달해도 우리가 닫을 때까지 유지
            dlg.setAutoReset(False)
            if not cancellable:
                dlg.setCancelButton(None)
                dlg.setRange(0, 0)       # 진행률을 모르는 구간 = busy 표시
            dlg.setValue(0)
            dlg.show()
            QApplication.processEvents()
            return dlg
        except Exception as exc:   # noqa: BLE001 - 진행 표시 실패가 업데이트를 막지 않게
            updater.ulog(f"MODAL 생성 실패: {exc}")
            return None

    def _exit_for_update(self):
        """업데이트 배치가 부모 PID 종료를 기다리므로 프로세스 종료를 보장한다.

        QApplication.quit() 만으로는 이벤트 루프만 끝나고, QWebEngineView 를 물고 있는
        상태로 인터프리터 종료에 들어가 QtWebEngine 정리가 지연·정지한다. 그러면 창은
        사라지는데 프로세스는 남아, 배치가 종료를 120초 헛기다리다 조용히 포기한다
        (2026-07-21 현장 실패 원인 — honey_update.log 에 'update start' 한 줄만 남았다).

        브라우저를 먼저 정리해 QtWebEngineProcess 가 _internal 안 DLL 을 놓게 한 뒤
        (안 놓으면 배치의 _internal swap 이 실패한다) os._exit 로 확실히 끝낸다.
        업데이트 직전이라 저장할 상태가 없으므로 강제 종료가 안전하다.
        """
        updater.ulog("EXIT 브라우저 정리 시작")
        try:
            self.shutdown_browser()   # 일반 종료와 같은 정리 (팝업 창 포함)
        except Exception as exc:   # noqa: BLE001
            updater.ulog(f"EXIT 브라우저 정리 실패(무시): {exc}")
        # deleteLater/네이티브 창 파괴가 처리되도록 짧게 이벤트를 돌린다.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.05)
        updater.ulog("EXIT os._exit(0)")
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(0)

    # ── 버전 폴더 + 런처 방식 업데이트 ──────────────────────────────────────
    def _versioned_update_root(self):
        """새 방식으로 업데이트할 수 있으면 설치 루트, 아니면 None.

        판정은 install_root() 하나로 끝난다 — 실행 파일이 versions\\<ver>\\HoneyApp.exe
        일 때만 루트를 돌려주므로, 구 레이아웃(Honey.exe 단독)으로 배포된 기존
        사용자는 항상 None 이라 종전 흐름(ZIP 다운로드 안내)이 그대로 동작한다.
        """
        return app_update.install_root()

    def _run_versioned_update(self, manifest, remote, root):
        """실행 중인 파일을 건드리지 않는 업데이트: 새 버전 폴더 설치 → 포인터 교체 → 재시작.

        구 batch 스왑(updater.apply_update_zip)과 달리 설치가 끝날 때까지 현재 버전은
        그대로 돌아간다. 어느 단계에서 실패·취소해도 기존 버전은 무손상이다.

        **현재는 호출되지 않는다** (2026-08-12) — 버전 폴더 방식의 업데이트는 런처가
        앱을 띄우기 전에 하는 것으로 일원화했다(launcher.try_update). 런처 방식에
        문제가 생겼을 때 되돌릴 수 있도록 이 경로를 지우지 않고 남겨 둔다.
        """
        url = manifest.get("url") or "/honey/download"
        expected = manifest.get("sha256") or None
        package_name = manifest.get("file") or f"Honey-{remote}.zip"
        app_update.ulog(f"[v2] OFFER remote={remote} current={CURRENT_VERSION} root={root}")

        answer = QMessageBox.question(
            self, "업데이트 사용 가능",
            f"신규 버전 {remote} 이(가) 있습니다. (현재 {CURRENT_VERSION})\n\n"
            "지금 업데이트하시겠습니까?\n"
            "다운로드와 설치가 끝나면 Honey 가 자동으로 다시 시작됩니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            app_update.ulog("[v2] CHOICE later")
            return

        enough, free_mb, need_mb = app_update.check_disk(root, manifest.get("size"))
        if not enough:
            app_update.ulog(f"[v2] DISK 부족 free={free_mb}MB need={need_mb}MB")
            QMessageBox.warning(
                self, "디스크 공간 부족",
                f"업데이트에 약 {need_mb}MB 가 필요하지만 여유 공간이 {free_mb}MB 입니다.\n"
                f"공간을 확보한 뒤 다시 시도해 주세요.\n\n{root}")
            return

        dest = root / app_update.UPDATES_DIRNAME / package_name
        app_update.ulog(f"[v2] DOWNLOAD start dest={dest} url={url}")

        progress = _ElapsedProgress(
            self.progress_status, "업데이트 다운로드 중...",
            self.status.showMessage, busy=True, minimum=0, maximum=100)
        modal = self._update_modal(f"신규 버전 {remote} 다운로드 준비 중...")
        events = queue.Queue()
        self._dl_cancelled = False
        self._show_download_cancel()

        def _cb(done, total):
            events.put((done, total))
            return not self._dl_cancelled

        def _drain(label_fmt):
            # 워커 스레드가 넣은 진행값 중 최신 것만 화면에 반영한다.
            if modal is not None and modal.wasCanceled():
                self._dl_cancelled = True
            last = None
            while True:
                try:
                    last = events.get_nowait()
                except queue.Empty:
                    break
            if last is None:
                QApplication.processEvents()
                return
            done, total = last
            label = label_fmt.format(done=done // (1024 * 1024), total=total // (1024 * 1024))
            pct = int(done * 100 / total) if total else 0
            progress.set(label, value=pct)
            if modal is not None:
                modal.setLabelText(f"{label}  {pct}%")
                modal.setValue(pct)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(version_check.download_to, dest, url,
                                expected_sha256=expected, progress_cb=_cb)
                _wait_for_future(fut, progress,
                                 poll_cb=lambda: _drain("업데이트 다운로드 중... ({done}MB / {total}MB)"))
        except version_check.DownloadCancelled:
            app_update.ulog("[v2] DOWNLOAD cancelled by user")
            progress.fail("실패: 업데이트 다운로드 취소됨")
            self.status.showMessage("업데이트 취소됨")
            return
        except Exception as exc:
            app_update.ulog(f"[v2] DOWNLOAD FAILED {type(exc).__name__}: {exc}")
            progress.fail(f"실패: 업데이트 다운로드 실패 - {exc}")
            _show_exc(self, "다운로드 실패", exc,
                      prefix="업데이트를 내려받지 못했습니다. 네트워크와 서버 상태를 확인해 주세요.")
            self.status.showMessage("업데이트 실패")
            return
        finally:
            self._hide_download_cancel()
            if modal is not None:
                modal.close()
        progress.success("완료: 업데이트 다운로드 완료", value=100)

        # 설치(압축 해제)는 새 버전 폴더에만 쓰므로 중간 취소도 안전하다.
        progress = _ElapsedProgress(
            self.progress_status, "업데이트 설치 중...",
            self.status.showMessage, busy=True, minimum=0, maximum=100)
        modal = self._update_modal(f"버전 {remote} 설치 중...")
        events = queue.Queue()
        self._dl_cancelled = False
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(app_update.install_version, root, remote, dest, _cb)
                _wait_for_future(fut, progress,
                                 poll_cb=lambda: _drain("업데이트 설치 중... ({done}MB / {total}MB)"))
        except app_update.InstallCancelled:
            app_update.ulog("[v2] INSTALL cancelled by user")
            progress.fail("실패: 업데이트 설치 취소됨")
            self.status.showMessage("업데이트 취소됨")
            return
        except Exception as exc:
            app_update.ulog(f"[v2] INSTALL FAILED {type(exc).__name__}: {exc}")
            progress.fail(f"실패: 업데이트 설치 실패 - {exc}")
            _show_exc(self, "업데이트 설치 실패", exc,
                      prefix="업데이트를 설치하지 못했습니다. 기존 버전은 그대로 사용할 수 있습니다.\n"
                             f"진단 로그: {app_update.log_path()}")
            self.status.showMessage("업데이트 실패")
            return
        finally:
            if modal is not None:
                modal.close()
            dest.unlink(missing_ok=True)   # 성공/실패 어느 쪽이든 받은 zip 은 남기지 않는다

        progress.success("업데이트 적용 중... 앱을 다시 시작합니다.", value=100)
        try:
            app_update.switch_and_relaunch(root, remote, CURRENT_VERSION)
        except Exception as exc:
            app_update.ulog(f"[v2] SWITCH FAILED {type(exc).__name__}: {exc}")
            _show_exc(self, "업데이트 적용 실패", exc,
                      prefix="새 버전을 설치했지만 전환하지 못했습니다. 기존 버전은 그대로 사용할 수 있습니다.\n"
                             f"진단 로그: {app_update.log_path()}")
            return
        self.status.showMessage("업데이트 적용 중... 앱을 다시 시작합니다.")
        self._exit_for_update()

    def _on_version_manifest(self, result):
        if isinstance(result, requests.exceptions.RequestException):
            # 연결 불가/타임아웃 = 서버 오프라인으로 간주, 상태바에 명확히 표시
            self.status.showMessage(
                f"⚠ 서버 오프라인 — {SERVER_BASE_URL} 에 연결할 수 없습니다")
            return
        if isinstance(result, Exception):
            self.status.showMessage(f"버전 체크 실패: {result}")
            return
        manifest = result

        remote = manifest.get("version") or ""
        if not version_check.is_newer(remote, CURRENT_VERSION):
            self.status.showMessage(
                f"버전 체크 OK — 최신 ({CURRENT_VERSION}). Server: {SERVER_BASE_URL}")
            # 최신을 실행 중일 때만 공지 확인 — 업데이트가 남아 있으면 구버전 사용자에게
            # 신버전 공지가 먼저 뜨게 되므로, 업데이트를 마친 뒤 첫 실행에서 뜬다.
            self._maybe_show_announcement()
            return

        versioned_root = self._versioned_update_root()
        if versioned_root is not None:
            # 버전 폴더 방식은 **런처가 앱을 띄우기 전에** 업데이트한다. 실행 중에는
            # 아무것도 하지 않는다 (2026-08-12 결정) — 사용자가 쓰고 있는 창을 끊지
            # 않고, 업데이트 경로를 런처 한 곳으로 모으기 위해서다. 다음 실행 때
            # Honey.exe(런처)가 처리한다.
            app_update.ulog(f"[v2] remote={remote} 있음 — 런처가 다음 실행에 처리(앱 무동작)")
            return

        # 설치 방법 선택: [자동 설치] / [ZIP 다운로드] / [나중에]
        # [자동 설치] 는 update_policy.AUTO_INSTALL_ENABLED 가 False 면 아예 만들지 않는다
        # (현재 일시 비활성 — ZIP 다운로드/나중에 2버튼).
        # sha256 없는 배포는 다운로드 무결성 검증이 통째로 생략되므로 자동 설치 금지
        # (ZIP 수동 설치는 사용자 주도라 허용).
        auto_enabled = update_policy.AUTO_INSTALL_ENABLED
        can_write = updater.can_write_app_dir()
        has_hash = bool((manifest.get("sha256") or "").strip())
        can_auto = auto_enabled and can_write and has_hash
        updater.ulog(f"OFFER remote={remote} current={CURRENT_VERSION} "
                     f"auto_enabled={auto_enabled} "
                     f"can_write={can_write} has_hash={has_hash} file={manifest.get('file')}")
        box = QMessageBox(self)
        box.setWindowTitle("업데이트 사용 가능")
        box.setIcon(QMessageBox.Icon.Question)
        ask_text = (
            f"신규 버전 {remote} 이(가) 있습니다.\n"
            f"현재: {CURRENT_VERSION}\n\n설치 방법을 선택하세요.\n\n")
        if auto_enabled:
            ask_text += "· 자동 설치: 다운로드 후 앱을 교체하고 재실행합니다.\n"
        ask_text += "· ZIP 다운로드: ZIP 만 다운로드 폴더에 저장합니다 (수동 설치)."
        if auto_enabled and not can_write:
            ask_text += "\n\n(설치 폴더에 쓰기 권한이 없어 자동 설치는 사용할 수 없습니다.)"
        elif auto_enabled and not has_hash:
            ask_text += ("\n\n(배포 정보에 무결성 해시(sha256)가 없어 자동 설치는 사용할 수 "
                         "없습니다. ZIP 다운로드를 이용하세요.)")
        box.setText(ask_text)
        btn_auto = None
        if auto_enabled:
            btn_auto = box.addButton("자동 설치", QMessageBox.ButtonRole.AcceptRole)
            if not can_auto:
                btn_auto.setEnabled(False)
        btn_manual = box.addButton("ZIP 다운로드", QMessageBox.ButtonRole.ActionRole)
        box.addButton("나중에", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_auto if can_auto else btn_manual)
        box.exec()
        clicked = box.clickedButton()
        if btn_auto is not None and clicked is btn_auto:
            mode = update_policy.MODE_AUTO
        elif clicked is btn_manual:
            mode = update_policy.MODE_MANUAL
        else:  # "나중에" 또는 창 닫기
            updater.ulog("CHOICE later/closed")
            return
        updater.ulog(f"CHOICE mode={mode}")

        url = manifest.get("url") or "/honey/download"
        expected = manifest.get("sha256") or None
        package_name = manifest.get("file") or f"Honey-{remote}.zip"
        if mode == update_policy.MODE_MANUAL:
            dest = update_policy.unique_dest(
                update_policy.downloads_dir(), package_name)
        else:
            dest = Path(tempfile.gettempdir()) / package_name
        updater.ulog(f"DOWNLOAD start dest={dest} url={url}")

        # 다운로드 진행 상태는 메인 UI Status bar 에 표시한다.
        progress = _ElapsedProgress(
            self.progress_status, "업데이트 다운로드 중...",
            self.status.showMessage, busy=True, minimum=0, maximum=100)
        # dock 진행바는 눈에 안 띄어 사용자가 "화면이 멈췄다" 고 인식한다 — 업데이트는
        # 수백 MB 라 대기가 길므로 화면 중앙 모달로도 같은 진행을 보여준다.
        modal = self._update_modal(f"신규 버전 {remote} 다운로드 준비 중...")
        download_events = queue.Queue()
        # 취소 배선 — 워커 스레드의 _cb 가 False 를 반환하면 version_check.download_to 가
        # 부분 파일을 지우고 DownloadCancelled 를 올린다(아래 except 가 받는다).
        # 클릭은 _wait_for_future 의 processEvents 폴링이 메인 스레드로 전달한다.
        self._dl_cancelled = False
        self._show_download_cancel()

        def _cb(done, total):
            download_events.put((done, total))
            return not self._dl_cancelled

        def _drain_download_events():
            # 모달의 취소 버튼도 dock 취소 버튼과 같은 플래그를 세운다.
            if modal is not None and modal.wasCanceled():
                self._dl_cancelled = True
            last = None
            while True:
                try:
                    last = download_events.get_nowait()
                except queue.Empty:
                    break
            if last is None:
                # 청크가 없어도 경과시간·이벤트 펌프는 계속 돌아야 화면이 안 굳는다.
                QApplication.processEvents()
                return
            done, total = last
            label = f"업데이트 다운로드 중... ({done // (1024 * 1024)}MB"
            label += f" / {total // (1024 * 1024)}MB)" if total else ")"
            pct = int(done * 100 / total) if total else 0
            progress.set(label, value=pct)
            if modal is not None:
                modal.setLabelText(f"{label}  {pct}%")
                modal.setValue(pct)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(
                    version_check.download_to,
                    dest,
                    url,
                    expected_sha256=expected,
                    progress_cb=_cb,
                )
                _wait_for_future(fut, progress, poll_cb=_drain_download_events)
        except version_check.DownloadCancelled:
            updater.ulog("DOWNLOAD cancelled by user")
            progress.fail("실패: 업데이트 다운로드 취소됨")
            self.status.showMessage("업데이트 취소됨")
            return
        except Exception as exc:
            updater.ulog(f"DOWNLOAD FAILED {type(exc).__name__}: {exc}")
            progress.fail(f"실패: 업데이트 다운로드 실패 - {exc}")
            _show_exc(self, "다운로드 실패", exc,
                      prefix="업데이트를 내려받지 못했습니다. 네트워크와 서버 상태를 확인해 주세요.")
            self.status.showMessage("업데이트 실패")
            return
        finally:
            self._hide_download_cancel()
            # 성공·실패·취소 어느 경로로 나가든 모달을 닫는다 (설치 단계는 새로 띄운다).
            if modal is not None:
                modal.close()
        progress.success("완료: 업데이트 다운로드 완료", value=100)
        try:
            updater.ulog(f"DOWNLOAD ok bytes={dest.stat().st_size}")
        except OSError:
            updater.ulog("DOWNLOAD ok (크기 확인 실패)")

        if mode == update_policy.MODE_MANUAL:
            update_policy.open_folder_select(dest)
            QMessageBox.information(
                self, "수동 업데이트 안내",
                f"업데이트 ZIP 을 저장했습니다:\n{dest}\n\n"
                "설치 방법:\n"
                "1) Honey 를 종료\n"
                "2) ZIP 압축 해제\n"
                "3) 압축 푼 Honey 폴더 내용을 설치 폴더에 덮어쓰기\n"
                "4) Honey.exe 다시 실행",
            )
            self.status.showMessage(f"업데이트 ZIP 저장됨: {dest}")
            return

        if not updater.is_frozen():
            QMessageBox.information(
                self, "다운로드 완료 (개발 모드)",
                f"스크립트 실행 중이라 설치를 진행하지 않습니다.\n"
                f"업데이트 ZIP만 다운로드 완료:\n{dest}\n\n"
                f"(자동 업데이트는 빌드된 exe 에서 동작합니다.)",
            )
            progress.success("다운로드 완료 (개발 모드)", value=100)
            self.status.showMessage("다운로드 완료 (개발 모드)")
            return

        updater.ulog("INSTALL 안내창 표시")
        QMessageBox.information(
            self, "업데이트 설치",
            f"새 버전 {remote} 을(를) 설치합니다.\n\n"
            "업데이트하는 동안 앱이 잠시 종료되며, 완료되면 자동으로 다시 실행됩니다.\n"
            "잠시만 기다려 주세요.",
        )
        updater.ulog("INSTALL 안내창 확인됨")
        # ZIP 압축 해제(수백 MB·수천 파일)는 10초+ 걸릴 수 있어 메인 스레드에서
        # 돌리면 "설치 중" 구간에 창이 굳는다 — 다운로드와 같은 패턴으로 스레드 이관.
        install_progress = _ElapsedProgress(
            self.progress_status, "업데이트 설치 중... (파일 압축 해제)",
            self.status.showMessage, busy=True, minimum=0, maximum=0)
        # 압축 해제 구간은 진행률을 알 수 없어 busy 표시. 취소 버튼은 두지 않는다
        # (이미 파일을 건드리기 시작한 뒤라 중간 취소가 더 위험하다).
        modal = self._update_modal("업데이트 설치 준비 중 (압축 해제)...", cancellable=False)
        install_start = time.monotonic()

        def _pump_install():
            # poll_cb 가 없으면 _wait_for_future 는 1초에 한 번만 processEvents 를 부른다
            # (ElapsedProgress.update 가 초 단위로만 갱신) — 그래서 화면이 굳어 보였다.
            if modal is not None:
                secs = int(time.monotonic() - install_start)
                modal.setLabelText(
                    "업데이트 설치 준비 중 (압축 해제)...\n"
                    f"수 분 걸릴 수 있습니다. 경과 {secs // 60:02d}:{secs % 60:02d}")
            QApplication.processEvents()

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(updater.apply_update_zip, dest)
                launch = _wait_for_future(fut, install_progress, poll_cb=_pump_install)
        except Exception as exc:
            updater.ulog(f"APPLY FAILED {type(exc).__name__}: {exc}")
            if modal is not None:
                modal.close()
            install_progress.fail(f"실패: 업데이트 실행 실패 - {exc}")
            _show_exc(self, "업데이트 실행 실패", exc,
                      prefix="업데이트를 설치하지 못했습니다. 기존 버전은 그대로 사용할 수 있습니다.\n"
                             f"진단 로그: {updater.log_path()}")
            self.status.showMessage("업데이트 실패")
            return
        updater.ulog(f"APPLY ok {launch}")
        install_progress.success("업데이트 적용 중... 앱을 종료합니다.", value=100)
        self.status.showMessage("업데이트 적용 중... 앱을 종료합니다.")
        if modal is not None:
            modal.setLabelText("업데이트 적용 중 — 앱이 종료되고 자동으로 다시 실행됩니다.")
            QApplication.processEvents()
        self._exit_for_update()


def _install_excepthook():
    """슬롯에서 발생한 미처리 예외로 앱이 조용히 죽지 않도록, 메시지로 표시.

    PyQt6 는 슬롯의 미처리 예외 시 기본 excepthook 이면 abort 한다. 후킹하면
    앱을 유지하면서 오류를 보여줄 수 있다.
    """
    import traceback

    def hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        print(text, file=sys.stderr)  # tee 된 stderr → 로그 파일에 traceback 기록
        # 서버에도 최소 정보를 남긴다 — 지금까지 Honey 오류는 사용자 PC 로그에만
        # 남아, 신고가 없으면 관리자가 존재조차 알 수 없었다. 전송 실패는 무음.
        event_id = _report_error("honey_crash", f"{etype.__name__}: {value}", stack=text)
        try:
            # traceback 은 "자세히 보기" 뒤로 — 본문은 사용자가 읽을 문장만.
            _show_error(
                None, "오류가 발생했습니다",
                "예기치 않은 오류가 발생했습니다.\n"
                "작업을 다시 시도해 주세요. 반복되면 아래 '자세히 보기' 내용을 담당자에게 전달해 주세요."
                + (f"\n\n오류번호: {event_id}" if event_id else ""),
                detail=text[-3000:])
        except Exception:
            pass
        sys.__excepthook__(etype, value, tb)

    sys.excepthook = hook


HONEY_QSS = """
    QMainWindow, QDialog, QDockWidget { background: #FFF8E1; }
    QLabel { background: transparent; color: #5D4711; }
    QGroupBox {
        background: #FFFDF5; border: 1px solid #E8D9A8; border-radius: 6px;
        margin-top: 10px; color: #6B4E16;
    }
    QGroupBox::title {
        subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #8A6D1D;
    }
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
        background: #FFFFFF; border: 1px solid #E0CE93; border-radius: 5px;
        padding: 3px 6px; color: #4A3B1A; selection-background-color: #FFD65A;
        selection-color: #3A2E12;
    }
    QPushButton {
        background: #F6C445; color: #4A3B1A; border: 1px solid #E0A81E;
        border-radius: 6px; padding: 5px 12px; font-weight: 600;
    }
    QPushButton:hover { background: #FFD65A; }
    QPushButton:pressed { background: #E9B21E; }
    QPushButton:disabled { background: #EDE6CF; color: #A89C77; border-color: #E0D9C0; }
    QRadioButton, QCheckBox { background: transparent; color: #4A3B1A; spacing: 6px; }
    QRadioButton::indicator, QCheckBox::indicator {
        width: 15px; height: 15px; border: 2px solid #B98A2E; background: #FFFFFF;
    }
    QRadioButton::indicator { border-radius: 9px; }
    QCheckBox::indicator { border-radius: 3px; }
    QRadioButton::indicator:checked, QCheckBox::indicator:checked {
        background: #E9A100; border-color: #9A6B12;
    }
    QRadioButton::indicator:hover, QCheckBox::indicator:hover { border-color: #8A6D1D; }
    QRadioButton::indicator:disabled, QCheckBox::indicator:disabled {
        border-color: #D8CBA0; background: #F0ECDD;
    }
    QTableWidget, QTableView, QListWidget, QTreeWidget {
        background: #FFFFFF; border: 1px solid #E8D9A8;
        gridline-color: #F0E6C8; selection-background-color: #FFE29A;
        selection-color: #3A2E12; alternate-background-color: #FFFBEA;
    }
    QHeaderView::section {
        background: #F3E5B8; color: #6B4E16; border: none;
        border-right: 1px solid #E8D9A8; padding: 4px 6px; font-weight: 600;
    }
    /* 상단 메뉴바는 전역 폰트(10pt)보다 작게 + 수직 여백을 줄여 띠 높이를 낮춘다. */
    QMenuBar {
        background: #F3E5B8; color: #6B4E16; font-size: 8pt; padding: 0px;
    }
    QMenuBar::item { padding: 2px 8px; }
    QMenuBar::item:selected { background: #FFD65A; }
    QMenu { background: #FFFDF5; border: 1px solid #E8D9A8; color: #4A3B1A; }
    /* QMenu 에 스타일시트가 걸리면 item 패딩을 명시하지 않을 때 Qt 가 우측 글자를 잘라
       긴 메뉴명이 다 안 보인다 — 좌우 여백을 넉넉히 줘 폭을 확보한다. */
    QMenu::item { padding: 5px 30px 5px 22px; }
    QMenu::item:selected { background: #FFE29A; }
    QToolBar { background: #F3E5B8; border: none; }
    QProgressBar {
        background: #FBF3D6; border: 1px solid #E0CE93; border-radius: 5px;
        text-align: center; color: #5D4711;
    }
    QProgressBar::chunk { background: #F5A623; border-radius: 4px; }
    QTabBar::tab { background: #F3E5B8; color: #6B4E16; padding: 5px 12px; }
    QTabBar::tab:selected { background: #FFD65A; }
    QScrollBar:vertical { background: #FBF3D6; width: 12px; margin: 0; }
    QScrollBar::handle:vertical { background: #E7CE86; border-radius: 6px; min-height: 24px; }
    QScrollBar:horizontal { background: #FBF3D6; height: 12px; margin: 0; }
    QScrollBar::handle:horizontal { background: #E7CE86; border-radius: 6px; min-width: 24px; }
    QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
    QStatusBar { background: #F3E5B8; color: #6B4E16; }
"""


def _apply_honey_theme(app):
    """앱 전역을 연노란색 '꿀단지' 톤으로 통일 (개별 위젯 인라인 스타일은 유지된다)."""
    app.setStyleSheet(HONEY_QSS)


def _apply_cute_font(app):
    """앱 전역 글씨체를 귀여운(둥근) 느낌으로. 설치된 첫 후보를 사용."""
    from PyQt6.QtGui import QFontDatabase
    available = set(QFontDatabase.families())
    # 귀여운/둥근 계열 우선순위 (설치돼 있는 첫 폰트 선택)
    candidates = ["Comic Sans MS", "Segoe Print", "Comic Neue",
                  "HY엽서L", "HY견고딕", "맑은 고딕"]
    family = next((c for c in candidates if c in available), None)
    font = QFont(family) if family else app.font()
    font.setPointSize(10)
    app.setFont(font)


def _schedule_version_cleanup():
    """버전 폴더 방식이면 정상 기동 10초 뒤 옛 버전·잔재를 정리한다.

    창이 뜨고 10초를 버텼다 = 새 버전 첫 실행이 성공했다는 뜻이라, 이때 직전 버전
    1개만 롤백용으로 남기고 나머지를 지운다. 실패해도 무해한 best-effort 라
    데몬 스레드로 돌린다 (종료를 붙잡지 않는다).
    """
    root = app_update.install_root()
    if root is None:
        return
    current, previous = app_update.read_current(root)
    timer = threading.Timer(
        10.0, app_update.startup_cleanup, args=(root,),
        kwargs={"keep_versions": (current or CURRENT_VERSION, CURRENT_VERSION, previous)})
    timer.daemon = True
    timer.start()


def main():
    import run_log
    run_log.setup_run_logging()
    # 지난 실행이 업데이트 도중 죽었으면 설치 폴더에 _internal.old 등 잔재가 남는다 —
    # 다음 실행의 첫 줄에서 그 사실을 기록해 두면 원인 추적이 로그 한 파일로 끝난다.
    updater.log_startup_state(CURRENT_VERSION)
    # QtWebEngine(내장 브라우저)을 앱 생성 후 lazy import 하려면 필수 (없어도 무해)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    # honey.env 의 HONEY_CHROMIUM_FLAGS 를 Qt 가 읽는 이름으로 옮긴다. QtWebEngine 초기화
    # 전이어야 먹으므로 QApplication 생성보다 앞이어야 한다. 기본은 빈 값 = 무변경.
    if CHROMIUM_FLAGS:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = CHROMIUM_FLAGS
    # 렌더러 크래시로 GPU 우회를 적용한 PC 인지 로그만 보고 알 수 있게 값을 남긴다.
    print(f"[startup] QTWEBENGINE_CHROMIUM_FLAGS="
          f"{os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS') or '(없음)'}")
    app = QApplication(sys.argv)
    app.setWindowIcon(HoneyMainWindow._honey_icon(64))   # 작업표시줄 꿀단지 아이콘
    _apply_honey_theme(app)
    _apply_cute_font(app)
    _install_excepthook()
    win = HoneyMainWindow()
    # 창 닫기를 거치지 않는 종료(app.quit·세션 로그아웃 등)도 같은 정리를 타게 한다.
    app.aboutToQuit.connect(win.shutdown_browser)
    win.showMaximized()
    _schedule_version_cleanup()
    _flush_diag_queue()
    code = app.exec()
    win.shutdown_browser()   # 안전망 — 이미 정리됐으면 no-op
    _final_exit(code)


def _final_exit(code):
    """정리가 끝난 뒤 프로세스를 확실히 끝낸다 (인터프리터 종료 단계를 건너뛴다).

    파이썬 정상 종료로 두면 QApplication 이 먼저 파괴된 뒤 QWebEngineView 가 GC 로
    나중에 파괴되면서 access violation 이 난다. 업데이트 종료(_exit_for_update)가
    같은 이유로 이미 os._exit 을 쓴다.

    ⚠ 종료 시 저장이 필요한 작업은 이 함수보다 앞(shutdown_browser 앞)에서 끝내야
    한다 — 여기 도달하면 데몬 스레드·atexit 훅은 실행되지 않는다.
    """
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.05)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)


def _flush_diag_queue():
    """서버가 죽어 있던 동안 쌓인 오류 보고를 재전송한다 (백그라운드, 실패 무음).

    서버 장애 중에 난 오류일수록 기록이 중요한데, 그때가 바로 전송이 실패하는 때다 —
    그래서 로컬 큐를 두고 여기서 흘려보낸다 (transport/error_report.py)."""
    def run():
        try:
            from transport import error_report
            error_report.flush_queue()
        except Exception:
            pass
    try:
        threading.Thread(target=run, name="diag-flush", daemon=True).start()
    except Exception:
        pass


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()  # PyInstaller + ProcessPoolExecutor(excel_download) 필수
    main()

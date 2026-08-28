"""AI Comment 민감도 설정 창(Honey) — 게이지 ↔ 값 입력란 연동 회귀.

실행 (PyQt6 필요 — 전역 python 으로 **단독 실행**, pytest 로 묶지 말 것):
    python tests\\test_eval_sensitivity_dialog.py

왜 필요한가: 이 창이 깨지는 방식은 조용하다.
  · 게이지를 움직였는데 값 입력란이 안 따라오면, 사용자는 바뀐 줄 알고 저장하지만
    실제로는 옛 값이 실린다.
  · 직접 입력했는데 게이지 선택이 남아 있으면 어느 쪽이 적용됐는지 알 수 없다.
  · '사용자설정' 칸이 클릭으로 선택되면 값 없이 그 상태가 되어 의미가 사라진다
    (그 칸은 값을 직접 입력했을 때만 도달하는 **표시 전용** 상태다).
  · `resolve()` 가 게이지 3단계 키까지 실으면 **제품군 오버레이(/pe/eval)가 세션값에
    덮여 무력화**되고, 기본 세션의 캐시 키가 갈려 전 세션 콜드 재빌드가 된다.

서버 접속 없이 돈다 — 카탈로그는 스텁을 주입한다(창은 캐시본으로 먼저 그리는 설계).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "client"))

# 설정이 실제 %APPDATA% 를 건드리지 않게 격리 (테스트가 사용자 설정을 덮으면 안 된다).
import tempfile
os.environ["HONEY_CONFIG_DIR"] = tempfile.mkdtemp(prefix="honey_sens_test_")

from PyQt6.QtWidgets import QApplication      # noqa: E402

import eval_sensitivity                        # noqa: E402
from honey_ui.dialogs import EvalSensitivityDialog, GaugeSlider   # noqa: E402

CATALOG = {
    "version": 1,
    "groups": [
        {"id": "OUTLIER", "label_ko": "이상치", "signatures": ["OUTLIER"],
         "gauge_fixed": False,
         "keys": [{"key": "outlier_fail_mad_min",
                   "levels": [4.24, 4.12, 4.0, 3.88, 3.76], "default": 4.0},
                  {"key": "outlier_jump_ratio_min",
                   "levels": [0.318, 0.309, 0.30, 0.291, 0.282], "default": 0.30}]},
        {"id": "LOW_CPK", "label_ko": "공정능력", "signatures": ["LOW_CPK"],
         "gauge_fixed": True,
         "keys": [{"key": "cpk_warn", "levels": [1.33] * 5, "default": 1.33}]},
    ],
    "allowed_keys": ["outlier_fail_mad_min", "outlier_jump_ratio_min", "cpk_warn"],
    "help": {"outlier_fail_mad_min": {"what": "무리 거리", "effect": "낮추면 민감"}},
}


def _dialog():
    """카탈로그 스텁을 주입한 창 (서버 조회 없이 즉시 렌더)."""
    eval_sensitivity.save_cached_catalog(CATALOG)
    dlg = EvalSensitivityDialog()
    dlg._apply_catalog(CATALOG, True)
    return dlg


def _text(dlg, group_id, key):
    return dlg._rows[group_id]["inputs"][key].text()


def test_initial_is_default():
    """기본 상태 = 전 그룹 3단계, 입력란은 기본값."""
    dlg = _dialog()
    assert dlg._settings["global"] == 3
    assert _text(dlg, "OUTLIER", "outlier_fail_mad_min") == "4.0"
    assert _text(dlg, "LOW_CPK", "cpk_warn") == "1.33"
    assert dlg._all_gauge.value() == 3, "전체 게이지가 3단계에 있어야 한다"
    print("  기본 상태 OK")


def test_group_gauge_updates_values_live():
    """그룹 게이지를 움직이면 그 줄의 값 입력란이 **즉시** 바뀐다(핵심 상호작용)."""
    dlg = _dialog()
    dlg._on_group("OUTLIER", 5)
    assert _text(dlg, "OUTLIER", "outlier_fail_mad_min") == "3.76"
    assert _text(dlg, "OUTLIER", "outlier_jump_ratio_min") == "0.282"
    # 다른 그룹은 그대로
    assert _text(dlg, "LOW_CPK", "cpk_warn") == "1.33"
    assert dlg._rows["OUTLIER"]["gauge"].value() == 5
    # 그룹마다 단계가 달라졌으니 전체는 '사용자설정'(0)
    assert dlg._settings["global"] == 0
    assert dlg._all_gauge.value() == GaugeSlider.CUSTOM
    print("  그룹 게이지 → 값 실시간 갱신 OK")


def test_all_gauge_syncs_every_group():
    """전체 게이지는 전 그룹을 같은 단계로 끌고 간다 + 직접 입력 해제."""
    dlg = _dialog()
    dlg._settings["manual"]["cpk_warn"] = 1.5
    dlg._on_all(1)
    assert dlg._settings["global"] == 1
    assert set(dlg._settings["groups"].values()) == {1}
    assert dlg._settings["manual"] == {}, "전체 이동 시 직접 입력이 남으면 화면과 값이 어긋난다"
    assert _text(dlg, "OUTLIER", "outlier_fail_mad_min") == "4.24"
    print("  전체 게이지 동기 OK")


def test_manual_input_switches_to_custom():
    """값을 직접 고치면 그 줄의 게이지가 마지막 칸('사용자설정')으로 옮겨간다."""
    dlg = _dialog()
    edit = dlg._rows["OUTLIER"]["inputs"]["outlier_fail_mad_min"]
    edit.setText("2.5")
    dlg._on_value_edited("OUTLIER", "outlier_fail_mad_min")
    assert dlg._settings["manual"]["outlier_fail_mad_min"] == 2.5
    assert dlg._rows["OUTLIER"]["gauge"].value() == GaugeSlider.CUSTOM
    # 같은 그룹의 다른 키는 게이지 값을 그대로 따른다(직접 입력한 키만 바뀐다).
    assert _text(dlg, "OUTLIER", "outlier_jump_ratio_min") == "0.3"
    print("  직접 입력 → 사용자설정 전환 OK")


def test_custom_stop_is_not_clickable():
    """'사용자설정' 칸은 클릭으로 갈 수 없다 — 값을 직접 입력해야 도달하는 상태다."""
    dlg = _dialog()
    gauge = dlg._rows["OUTLIER"]["gauge"]
    gauge.resize(360, 34)
    seen = []
    gauge.changed.connect(seen.append)

    from PyQt6.QtCore import QPointF, Qt as _Qt
    from PyQt6.QtGui import QMouseEvent

    def click(index):
        pos = QPointF(gauge._stop_x(index), 12)
        gauge.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, pos, _Qt.MouseButton.LeftButton,
            _Qt.MouseButton.LeftButton, _Qt.KeyboardModifier.NoModifier))

    click(4)                       # 5단계 = 정상 선택
    assert seen == [5], seen
    click(5)                       # 사용자설정 칸 = 무시
    assert seen == [5], f"사용자설정 칸이 클릭으로 선택됐다: {seen}"
    print("  사용자설정 칸 클릭 불가 OK")


def test_fixed_group_gauge_ignores_clicks():
    """gauge_fixed 그룹은 클릭 자체가 먹지 않는다(직접 입력만)."""
    dlg = _dialog()
    gauge = dlg._rows["LOW_CPK"]["gauge"]
    gauge.resize(360, 34)
    seen = []
    gauge.changed.connect(seen.append)

    from PyQt6.QtCore import QPointF, Qt as _Qt
    from PyQt6.QtGui import QMouseEvent
    gauge.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(gauge._stop_x(0), 12),
        _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
        _Qt.KeyboardModifier.NoModifier))
    assert seen == [], f"고정 그룹 게이지가 움직였다: {seen}"
    print("  고정 그룹 클릭 무시 OK")


def test_manual_equal_to_gauge_is_dropped():
    """게이지 값과 같은 값을 손으로 넣으면 직접 입력으로 남기지 않는다(중복 저장 방지)."""
    dlg = _dialog()
    edit = dlg._rows["OUTLIER"]["inputs"]["outlier_fail_mad_min"]
    edit.setText("4.0")
    dlg._on_value_edited("OUTLIER", "outlier_fail_mad_min")
    assert "outlier_fail_mad_min" not in dlg._settings["manual"]
    print("  게이지와 동일값은 manual 미기록 OK")


def test_fixed_group_gauge_disabled():
    """gauge_fixed 그룹(LOW_CPK)은 게이지가 비활성 — 직접 입력만 허용."""
    dlg = _dialog()
    assert dlg._rows["LOW_CPK"]["gauge"]._enabled is False
    assert "직접 입력" in dlg._rows["LOW_CPK"]["gauge"].toolTip()
    print("  고정 그룹 게이지 비활성 OK")


def test_row_shows_signature_names():
    """행 이름은 **SIGNATURE 영문 원문** — 한글 설명이 아니다(2026-08-28 사용자 지시)."""
    dlg = _dialog()
    labels = {}
    for r in range(dlg._grid.rowCount()):
        item = dlg._grid.itemAtPosition(r, 0)
        if item is not None and item.widget() is not None:
            labels[r] = item.widget().text()
    joined = " ".join(labels.values())
    assert "OUTLIER" in joined and "LOW_CPK" in joined, joined
    assert "이상치" not in joined and "공정능력" not in joined, \
        f"한글 라벨이 행 이름에 남아 있다: {joined}"
    print("  행 이름 = signature 영문 OK")


def test_thresholds_are_stacked_vertically():
    """threshold 는 세로로 쌓인다 — 키 2개면 서로 다른 y 좌표에 놓인다."""
    dlg = _dialog()
    dlg.resize(760, 900)
    dlg.show()
    QApplication.processEvents()
    ys = {k: e.mapTo(dlg, e.rect().topLeft()).y()
          for k, e in dlg._rows["OUTLIER"]["inputs"].items()}
    dlg.hide()
    assert len(set(ys.values())) == 2, f"세로로 안 쌓였다: {ys}"
    print("  threshold 세로 나열 OK")


def test_tooltip_uses_server_help():
    """설명은 서버 카탈로그(help)에서 온다 — 클라 하드코딩 없음."""
    dlg = _dialog()
    text = dlg._help_text("outlier_fail_mad_min")
    assert "무리 거리" in text and "낮추면 민감" in text, text
    assert dlg._rows["OUTLIER"]["inputs"]["outlier_fail_mad_min"].toolTip() == text
    print("  툴팁 설명 OK")


def test_resolve_payload_rules():
    """resolve() — 기본이면 None / 게이지 3 키는 제외 / 직접 입력은 3단계에서도 포함."""
    # (1) 전부 기본 → None (옵션에 키를 싣지 않는다 = 캐시 키 불변)
    assert eval_sensitivity.resolve(CATALOG, {"global": 3, "groups": {}, "manual": {}}) is None

    # (2) 한 그룹만 5단계 → 그 그룹 키만 실린다 (제품군 오버레이 존중)
    spec = eval_sensitivity.resolve(
        CATALOG, {"global": 0, "groups": {"OUTLIER": 5, "LOW_CPK": 3}, "manual": {}})
    assert set(spec["overrides"]) == {"outlier_fail_mad_min", "outlier_jump_ratio_min"}
    assert spec["overrides"]["outlier_fail_mad_min"] == 3.76

    # (3) 직접 입력은 게이지 3 이어도 실린다
    spec = eval_sensitivity.resolve(
        CATALOG, {"global": 3, "groups": {"OUTLIER": 3, "LOW_CPK": 3},
                  "manual": {"cpk_warn": 1.5}})
    assert spec["overrides"] == {"cpk_warn": 1.5}
    assert spec["manual"] == {"cpk_warn": 1.5}

    # (4) 직접 입력이 게이지 값을 이긴다
    spec = eval_sensitivity.resolve(
        CATALOG, {"global": 0, "groups": {"OUTLIER": 5, "LOW_CPK": 3},
                  "manual": {"outlier_fail_mad_min": 2.0}})
    assert spec["overrides"]["outlier_fail_mad_min"] == 2.0
    print("  resolve payload 규칙 OK")


def test_settings_roundtrip():
    """OK 저장 → 다음 실행에서 그대로 복원된다(클라별 기본값)."""
    dlg = _dialog()
    dlg._on_group("OUTLIER", 4)
    dlg._settings["manual"]["cpk_warn"] = 1.4
    dlg._on_ok()
    loaded = eval_sensitivity.load_settings()
    assert loaded["groups"]["OUTLIER"] == 4
    assert loaded["manual"]["cpk_warn"] == 1.4
    print("  설정 영속 OK")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    print("AI Comment 민감도 설정 창 검증")
    test_initial_is_default()
    test_group_gauge_updates_values_live()
    test_all_gauge_syncs_every_group()
    test_manual_input_switches_to_custom()
    test_custom_stop_is_not_clickable()
    test_fixed_group_gauge_ignores_clicks()
    test_manual_equal_to_gauge_is_dropped()
    test_fixed_group_gauge_disabled()
    test_row_shows_signature_names()
    test_thresholds_are_stacked_vertically()
    test_tooltip_uses_server_help()
    test_resolve_payload_rules()
    test_settings_roundtrip()
    print("전부 통과")

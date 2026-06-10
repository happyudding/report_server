"""xlsx_writer 프로파일링/디버그 측정 인프라.

HONEY_CHART_PROFILE / HONEY_FLOW_PROFILE 등 환경변수가 set 일 때만 동작하며,
unset 이면 측정 컨텍스트는 즉시 통과 → 평상시 동작·출력 불변. 측정 결과는 stderr 로.

distribution PNG 부착 모드 플래그(_PNG_ATTACH_MODE / _PNG_SUBJECT_CACHE)도 여기 둔다
— 디버그 summary(_dist_emit_summary)가 참조하므로 png_export 모듈과의 순환을 피한다.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import sys
import time
from collections import defaultdict

from . import _profile

# ── 차트 생성 병목 측정 프로파일러 (HONEY_CHART_PROFILE set 시에만 동작) ───────
# unset 이면 _prof 는 즉시 통과 → 평상시 동작·출력 불변. 측정 결과는 stderr 로.
_PROF_ON = bool(os.environ.get("HONEY_CHART_PROFILE"))
_FLOW_PROFILE_ON = bool(os.environ.get("HONEY_FLOW_PROFILE"))
_CURRENT_PROFILE_CB = contextvars.ContextVar("xlsx_writer_profile_cb", default=None)
_CURRENT_DIST_STATS = contextvars.ContextVar("xlsx_writer_dist_stats", default=None)
_PROF = defaultdict(float)
_PROF_CNT = defaultdict(int)

_PNG_ATTACH_MODE = os.environ.get("HONEY_PNG_ATTACH_MODE", "export").strip().lower()
if _PNG_ATTACH_MODE not in {"export", "move_first_export", "copy_picture"}:
    _PNG_ATTACH_MODE = "export"
_PNG_SUBJECT_CACHE = os.environ.get("HONEY_PNG_SUBJECT_CACHE", "").strip().lower() in {
    "1", "true", "yes", "on",
}


@contextlib.contextmanager
def _prof(bucket):
    if not _PROF_ON:
        yield
        return
    t = time.perf_counter()
    try:
        yield
    finally:
        _PROF[bucket] += time.perf_counter() - t
        _PROF_CNT[bucket] += 1


def _prof_count(bucket, n=1):
    """시간 측정 없이 카운터만 증가 (차트/시리즈/PNG 개수 등)."""
    if _PROF_ON:
        _PROF_CNT[bucket] += n


def _emit_profile_event(profile_cb, label, status, elapsed=None, error=None, message=None):
    if profile_cb is None:
        return
    event = {
        "module": "xlsx_writer",
        "label": label,
        "status": status,
    }
    if elapsed is not None:
        event["elapsed"] = elapsed
    if error:
        event["error"] = error
    if message:
        event["message"] = message
    try:
        profile_cb(event)
    except Exception:
        pass


def _emit_profile_info(message):
    _emit_profile_event(_CURRENT_PROFILE_CB.get(), "distribution_profile", "info",
                        message=message)


@contextlib.contextmanager
def _profile_info_time(label):
    profile_cb = _CURRENT_PROFILE_CB.get()
    if profile_cb is None:
        yield
        return
    t = time.perf_counter()
    try:
        yield
    except Exception as exc:
        _emit_profile_info(f"{label} ERROR after {time.perf_counter() - t:.2f}s - {exc}")
        raise
    else:
        _emit_profile_info(f"{label} done: {time.perf_counter() - t:.2f}s")


def _new_dist_stats():
    return {
        "timings": defaultdict(lambda: {"total": 0.0, "count": 0, "max": 0.0}),
        "png": defaultdict(lambda: {
            "count": 0, "direct": 0, "moved": 0, "copy_picture": 0,
            "cache": 0, "failed": 0, "bytes": 0,
        }),
    }


def _dist_add_time(bucket, elapsed):
    stats = _CURRENT_DIST_STATS.get()
    if stats is None:
        return
    rec = stats["timings"][bucket]
    rec["total"] += elapsed
    rec["count"] += 1
    rec["max"] = max(rec["max"], elapsed)


@contextlib.contextmanager
def _dist_time(bucket):
    if _CURRENT_DIST_STATS.get() is None:
        yield
        return
    t = time.perf_counter()
    try:
        yield
    finally:
        _dist_add_time(bucket, time.perf_counter() - t)


def _dist_count_png(sheet_name, method, png_path=None):
    stats = _CURRENT_DIST_STATS.get()
    if stats is None:
        return
    rec = stats["png"][str(sheet_name)]
    rec["count"] += 1
    if method == "direct":
        rec["direct"] += 1
    elif str(method).startswith("moved"):
        rec["moved"] += 1
    elif method == "copy_picture":
        rec["copy_picture"] += 1
    elif method == "cache":
        rec["cache"] += 1
    else:
        rec["failed"] += 1
    if png_path and method != "cache":
        try:
            rec["bytes"] += os.path.getsize(png_path)
        except OSError:
            pass


def _dist_format_time(rec):
    count = rec["count"]
    avg_ms = rec["total"] * 1000.0 / count if count else 0.0
    max_ms = rec["max"] * 1000.0
    return f"total={rec['total']:.2f}s avg={avg_ms:.1f}ms max={max_ms:.1f}ms x{count}"


def _dist_emit_summary():
    stats = _CURRENT_DIST_STATS.get()
    if not stats:
        return
    timings = stats["timings"]
    _emit_profile_info(
        f"Distribution debug: png_mode={_PNG_ATTACH_MODE} "
        f"png_cache={'on' if _PNG_SUBJECT_CACHE else 'off'}"
    )
    loop_order = [
        "dist.loop.finite_scan", "dist.loop.axis_range",
        "dist.loop.chart_add", "dist.loop.series_limits", "dist.loop.series_sources",
        "dist.loop.chart_type", "dist.loop.style_limits", "dist.loop.style_sources",
        "dist.loop.axis_format", "dist.loop.title_format", "dist.loop.plot_format",
        "dist.loop.legend_format", "dist.loop.fail_bg",
    ]
    for bucket in loop_order:
        rec = timings.get(bucket)
        if rec and rec["count"]:
            _emit_profile_info(
                f"Dist loop {bucket.removeprefix('dist.loop.')}: {_dist_format_time(rec)}"
            )
    for sheet_name in ("fail_item", "issue_table"):
        rec = stats["png"].get(sheet_name)
        if not rec or not rec["count"]:
            continue
        total_mb = rec["bytes"] / (1024.0 * 1024.0)
        avg_kb = rec["bytes"] / 1024.0 / rec["count"] if rec["count"] else 0.0
        _emit_profile_info(
            f"PNG stats {sheet_name}: count={rec['count']} direct={rec['direct']} "
            f"moved={rec['moved']} copy={rec['copy_picture']} cache={rec['cache']} "
            f"failed={rec['failed']} "
            f"total={total_mb:.1f}MB avg={avg_kb:.0f}KB"
        )
        parts = []
        for name in (
            "export.direct", "export.moved", "export.failed", "picture_add",
            "copy_picture.copy", "copy_picture.paste", "copy_picture.position",
            "copy_picture.total",
        ):
            trec = timings.get(f"png.{sheet_name}.{name}")
            if trec and trec["count"]:
                parts.append(f"{name} {_dist_format_time(trec)}")
        if parts:
            _emit_profile_info(f"PNG timing {sheet_name}: " + "; ".join(parts))


@contextlib.contextmanager
def _flow_prof(bucket):
    profile_cb = _CURRENT_PROFILE_CB.get()
    if not (_FLOW_PROFILE_ON or _profile.collecting() or profile_cb is not None):
        yield
        return
    _emit_profile_event(profile_cb, bucket, "start")
    depth = _profile.push()
    t = time.perf_counter()
    try:
        yield
    except Exception as exc:
        elapsed = time.perf_counter() - t
        _profile.pop("xlsx_writer", bucket, elapsed, depth)
        _emit_profile_event(profile_cb, bucket, "error", elapsed, str(exc))
        if _FLOW_PROFILE_ON:
            print(f"[flow-profile] xlsx_writer.{bucket}: ERROR after {elapsed:.3f}s ({exc})",
                  file=sys.stderr, flush=True)
        raise
    finally:
        if sys.exc_info()[0] is None:
            elapsed = time.perf_counter() - t
            _profile.pop("xlsx_writer", bucket, elapsed, depth)
            _emit_profile_event(profile_cb, bucket, "done", elapsed)
            if _FLOW_PROFILE_ON:
                print(f"[flow-profile] xlsx_writer.{bucket}: {elapsed:.3f}s",
                      file=sys.stderr, flush=True)


def _prof_report():
    if not _PROF_ON or not _PROF:
        return
    total = sum(_PROF.values())
    print("\n[chart-profile] phase breakdown (s):", file=sys.stderr)
    for k, v in sorted(_PROF.items(), key=lambda kv: -kv[1]):
        pct = (100 * v / total) if total else 0.0
        print(f"  {k:18s} {v:8.3f}  ({pct:5.1f}%)  x{_PROF_CNT[k]}", file=sys.stderr)
    print(f"  {'TOTAL':18s} {total:8.3f}", file=sys.stderr)
    extra = {k: _PROF_CNT[k] for k in ("charts", "series", "pngs") if k in _PROF_CNT}
    if extra:
        print(f"  counts: {extra}", file=sys.stderr)
    _PROF.clear()
    _PROF_CNT.clear()

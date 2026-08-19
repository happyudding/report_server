"""Compare 비동기 분리(2026-08-19) 계약 검증.

실행:
    python tests/test_compare_async.py

배경: compare 계산(실측 1.1초 = 콜드 빌드의 34%)이 report payload 안에 박혀 있어
comment 한 줄 편집·스키마 bump·dedup 형제 세션마다 전량 재계산됐다. AI Comment 와 같은
패턴으로 분리 캐시(cache_policy.compare_key)로 뺀다. 고정하는 계약 5가지:

  (a) **값 등가** — 분리 경로(compare_inputs + build_compare)의 결과가 종전 인라인 계산과
      정준 JSON 까지 완전히 같다. 이게 깨지면 캐시 히트 여부에 따라 화면 값이 달라진다.
  (b) 주입/대기 — compare_payload 를 주면 그대로 싣고, compare_deferred 로 미루면
      `compare_pending` 만 세우고 `compare` 키는 **만들지 않는다**(프런트가 구분).
  (c) 미주입·미지연이면 종전처럼 여기서 계산한다(구 호출부·테스트 호환).
  (d) 캐시 키 — compare_key 는 session_id·edits_rev 에 **불변**(그래서 편집이 재계산을
      부르지 않는다)이고, 데이터(content_hash)·모드·옵션·세대에는 반응한다.
  (e) pending 키 — ai 단독은 **종전 꼬리(aipending) 그대로**(기존 파일 유효), compare 가
      끼면 갈린다. 안 갈리면 "AI 만 빈 본"과 "둘 다 빈 본"이 서로를 덮어쓴다.
  (f) **service 경로** — 실제 호출부(`service._compare_cached`)의 build 분기를 태운다.
      (a)~(e) 는 metrics 를 테스트가 직접 부르므로 service 안의 이름 해석 오류를 못 잡는다.
      실제로 `metrics` 모듈이 service 에 import 되지 않아 콜드 빌드(prewarm_job/report_job)가
      NameError 로 전멸한 적이 있다(2026-08-19). 사용자 대기 경로는 allow_build=False 라
      그 줄에 닿지 않아 조용히 pending 만 남았고, 증상은 "Compare 가 영원히 계산 중"이었다.

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_report import cache_policy, metrics, service  # noqa: E402
from web_report.metrics import build_report_payload  # noqa: E402
from web_report.validation import webreport_compare_groups  # noqa: E402

# 픽스처는 정본 회귀 테스트(test_compare_equivalence)와 **같은 것**을 쓴다 — 값 등가를
# 주장하려면 그 테스트가 지키는 계약과 같은 데이터여야 한다.
from test_compare_equivalence import _make_table, _shift, _TIGHT, _WIDE  # noqa: E402


def _tables():
    before = _make_table("BEFORE", {"G1_UP": _TIGHT, "G3_ITEM": _WIDE})
    after = _make_table("AFTER", {"G1_UP": _shift(_TIGHT, 1.04),
                                  "G3_ITEM": _shift(_WIDE, 1.10)})
    return [after, before]


def _canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _payload(tables, **kw):
    return build_report_payload(tables, mode="Compare",
                                compare_groups=webreport_compare_groups(
                                    {}, [t.source for t in tables]),
                                **kw)


class _StubDB:
    """전처리 spec 조회만 받는 최소 스텁 — session_digest 가 쓰는 유일한 메서드."""

    def get_webreport_edits(self, session_id, kinds=None):
        return []


def _session(**kw):
    base = {"analysis_key": "ak1", "content_hash": "ch1", "mode": "Compare",
            "webreport_options": '{"compare":{"before":["BEFORE"],"after":["AFTER"]}}'}
    base.update(kw)
    return base


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # (c) 미주입·미지연 = 종전 인라인 계산 ─────────────────────────────────────
    inline = _payload(_tables())
    assert "compare" in inline and inline["compare"], "인라인 경로가 compare 를 안 만들었다"
    assert "compare_pending" not in inline, inline.get("compare_pending")
    print("[c] 구 호출부 호환 OK — 미주입이면 종전처럼 계산")

    # (a) 값 등가 — 분리 경로가 인라인과 **완전히 같은 값** ─────────────────────
    tables = _tables()
    groups = webreport_compare_groups({}, [t.source for t in tables])
    all_items, cpk_rows, stat_items = metrics.compare_inputs(tables, mode="Compare")
    split = metrics.build_compare(tables, all_items, cpk_rows,
                                  stat_items=stat_items, compare_groups=groups)
    assert _canon(split) == _canon(inline["compare"]), "분리 경로 값이 인라인과 다르다"
    print(f"[a] 값 등가 OK — 정준 JSON 완전 일치 ({len(_canon(split))}자)")

    # (b) 주입 / 대기 ──────────────────────────────────────────────────────────
    injected = _payload(_tables(), compare_payload=split, compare_deferred=True)
    assert _canon(injected["compare"]) == _canon(split), "주입한 payload 가 안 실렸다"
    assert "compare_pending" not in injected
    deferred = _payload(_tables(), compare_deferred=True)
    assert deferred.get("compare_pending") is True, deferred.get("compare_pending")
    assert "compare" not in deferred, "대기 상태인데 compare 키가 있다(프런트가 구분 못 함)"
    print("[b] 주입/대기 OK — 대기 시 compare 키 없음 + compare_pending")

    # 단일 source 는 비교 대상이 없어 pending 조차 세우지 않는다(종전과 동일).
    one = build_report_payload([_make_table("ONLY", {"G1_UP": _TIGHT})], mode="Compare",
                               compare_deferred=True)
    assert "compare" not in one and "compare_pending" not in one, one.get("compare_pending")
    print("[b] 단일 source OK — pending 도 세우지 않음")

    # (d) 캐시 키 — 편집·세션에 불변, 데이터·모드·옵션에 반응 ───────────────────
    s = _session()
    base_key = cache_policy.compare_key(s)
    assert cache_policy.compare_key(s) == base_key, "같은 입력에 키가 흔들린다"
    # report_key 는 session_id·edits_rev 에 반응하지만 compare_key 는 아니어야 한다
    r1 = cache_policy.report_key(s, "sid1", 1)
    r2 = cache_policy.report_key(s, "sid1", 2)
    assert r1 != r2, "report_key 가 edits_rev 에 반응하지 않는다(픽스처 오류)"
    assert cache_policy.compare_key(s) == base_key, "compare_key 가 편집에 반응한다"
    assert cache_policy.compare_key(_session(content_hash="ch2")) != base_key, "데이터 변경 미반영"
    assert cache_policy.compare_key(_session(mode="Normal")) != base_key, "모드 변경 미반영"
    assert cache_policy.compare_key(
        _session(webreport_options='{"compare":{"before":["AFTER"],"after":["BEFORE"]}}')
    ) != base_key, "Before/After 배치 변경 미반영"
    assert cache_policy.compare_key(s, "prep8") != base_key, "전처리 변경 미반영"
    print("[d] compare_key OK — 편집·세션 불변 / 데이터·모드·배치·전처리 반응")

    # (e) pending 키 — ai 단독은 종전 꼬리 유지, 조합은 갈린다 ─────────────────
    ai_only = cache_policy.report_pending_key(s, "sid1", 1, ("ai",))
    assert ai_only[-1] == "aipending", ai_only[-1]
    assert cache_policy.report_pending_key(s, "sid1", 1) == ai_only, "기본 인자가 ai 가 아니다"
    cmp_only = cache_policy.report_pending_key(s, "sid1", 1, ("compare",))
    both = cache_policy.report_pending_key(s, "sid1", 1, ("ai", "compare"))
    assert len({ai_only, cmp_only, both}) == 3, "pending 키가 겹친다 — 서로 덮어쓴다"
    # 순서만 다른 인자는 같은 키(정렬) — 호출부마다 순서가 달라도 파일이 갈리지 않게.
    assert cache_policy.report_pending_key(s, "sid1", 1, ("compare", "ai")) == both
    assert both != cache_policy.report_key(s, "sid1", 1), "pending 이 정본 키와 같다"
    print("[e] pending 키 OK — ai 단독 종전 유지 · 3종 분리 · 순서 무관")

    # (f) service 경로 — 실제 호출부의 build 분기를 태운다 ──────────────────────
    root = Path(tempfile.mkdtemp(prefix="cmpasync_"))
    try:
        built, how = service._compare_cached(
            _session(), "sid1", _tables(), {"selected_items": []},
            report_db=_StubDB(), upload_root=root, allow_build=True)
        assert how == "build", f"캐시 미스인데 build 가 아니다: {how}"
        assert _canon(built) == _canon(split), "service 경로 값이 인라인과 다르다"
        # 사용자 대기 경로 — 미스에 계산하지 않고 miss (여기서 계산하면 요청이 묶인다)
        idle, how2 = service._compare_cached(
            _session(content_hash="ch_miss"), "sid1", _tables(),
            {"selected_items": []}, report_db=_StubDB(), upload_root=root,
            allow_build=False)
        assert idle is None and how2 == "miss", (idle is None, how2)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("[f] service 경로 OK — build 분기 실행 + 값 등가 + 대기 경로 miss")

    print("\n전부 통과")


if __name__ == "__main__":
    raise SystemExit(main())

r"""Distribution 미니셀 세로 채움(업샘플링) 회귀 — headless Edge (2026-08-25).

실행:
    server\.venv\Scripts\python.exe tests\test_dist_fill_js.py

**왜 파이썬 테스트로 안 되나**: 이 규칙이 깨지는 방식은 에러가 아니라 "그림이 실제와
다르다"이다. 표시점이 실제 측정 개수보다 많아도 예외가 없고, 카드가 그럴듯하게 촘촘해
보이기만 한다 — 2026-08-25 에 실제로 source 당 100개짜리 세션이 400점으로 그려졌다.

검증하는 것 (전부 distStepY / distFillVertical / distPointsForDisplay 순수 함수):
  (a) n 이 주어지면 채움점 개수 = 실제 측정 개수  (소량 100 · 이산 5×200 · n=1)
  (b) 나눠떨어지지 않는 저표본(n=3/7/13)에서 가짜 점이 생기지 않는다 — 서버가 y 를
      round(cum,3) 로 내려 누적 덧셈이 riser 끝과 어긋나던 자리(균등 분할로 교체)
  (c) 옛 캡 임계(333/334) 부근에 불연속이 없다
  (d) n 이 없는 응답(구버전 blob·옛 캐시)은 종전 폴백 경로(minΔy+0.3% 캡)를 탄다
  (e) 대량 표본은 성능 하한(100/fillMax)이 지배하고 표시 캡을 넘지 않는다
  (f) x 값은 절대 새로 만들지 않는다(가로 보간 금지 — CLAUDE.md §5-5)

Edge 가 없으면 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_dist_composite_js import edge_path, run_probe   # noqa: E402

DEPS = ["core.js", "distribution.js"]

# 서버 ECDF 를 그대로 흉내내는 하네스 헬퍼 — counts → (x, y, n).
# y 는 서버와 같은 순서로 만든다: cumsum → n 으로 나눔 → *100 → round3.
MK = """
function mkEcdf(counts){
  var n=0,i; for(i=0;i<counts.length;i++) n+=counts[i];
  var xs=[],ys=[],acc=0;
  for(i=0;i<counts.length;i++){
    acc+=counts[i]; xs.push(i+1);
    ys.push(Math.round(acc/n*100*1000)/1000);   // np.round(cum, 3)
  }
  return {xs:xs, ys:ys, n:n};
}
function rep(v,k){var a=[],i; for(i=0;i<k;i++) a.push(v); return a;}
"""


def probe(harness: str, name: str):
    return json.loads(run_probe(DEPS, "", f"<script>{MK}{harness}</script>", name))


def test_counts_match_samples():
    """(a) n 이 있으면 표시점 개수 = 실제 측정 개수."""
    res = probe("""
      var out={};
      // 소량 연속 100개 — 전부 고유값 (신고 사례)
      var a=mkEcdf(rep(1,100));
      out.small = distPointsForDisplay(a.xs,a.ys,1500,a.n).xs.length;
      // 이산 code 값 — 고유값 5개 × 각 200회
      var b=mkEcdf(rep(200,5));
      out.discrete = distPointsForDisplay(b.xs,b.ys,1500,b.n).xs.length;
      // 표본 1개
      var c=mkEcdf([1]);
      out.one = distPointsForDisplay(c.xs,c.ys,1500,c.n).xs.length;
      // 섞인 경우 — count [1,5,1,3] = 10개
      var d=mkEcdf([1,5,1,3]);
      out.mixed = distPointsForDisplay(d.xs,d.ys,1500,d.n).xs.length;
      _emit(out);
    """, "fill_counts")
    assert res["small"] == 100, f"소량 100개가 {res['small']}점으로 그려집니다(부풀림 회귀)"
    assert res["discrete"] == 1000, f"이산 5×200 이 {res['discrete']}점 (실제 1000)"
    assert res["one"] == 1, f"표본 1개가 {res['one']}점"
    assert res["mixed"] == 10, f"count[1,5,1,3] 이 {res['mixed']}점 (실제 10)"
    print("[a] 표시점 개수 = 실제 측정 개수 OK "
          f"(100→{res['small']} / 5×200→{res['discrete']} / 1→{res['one']})")


def test_no_phantom_on_odd_samples():
    """(b) 나눠떨어지지 않는 저표본에서 가짜 점이 안 생긴다 (round(cum,3) 오차)."""
    res = probe("""
      var out={};
      [3,7,13,17,29,31,97].forEach(function(n){
        var e=mkEcdf(rep(1,n));
        out['n'+n] = distPointsForDisplay(e.xs,e.ys,1500,e.n).xs.length;
      });
      _emit(out);
    """, "fill_odd")
    for n in (3, 7, 13, 17, 29, 31, 97):
        got = res[f"n{n}"]
        assert got == n, f"n={n} 인데 {got}점 — 반올림 오차로 가짜 점이 생겼습니다"
    print("[b] 저표본 3/7/13/17/29/31/97 전부 가짜 점 0개 OK")


def test_old_cap_boundary_continuous():
    """(c) 옛 캡 임계(333/334) 부근에 불연속이 없다."""
    res = probe("""
      var out={};
      [332,333,334,335].forEach(function(n){
        var e=mkEcdf(rep(1,n));
        out['n'+n] = distPointsForDisplay(e.xs,e.ys,1500,e.n).xs.length;
      });
      _emit(out);
    """, "fill_boundary")
    for n in (332, 333, 334, 335):
        assert res[f"n{n}"] == n, f"n={n} → {res[f'n{n}']}점 (경계 불연속)"
    print("[c] 333/334 경계 연속 OK")


def test_fallback_still_capped():
    """(d) n 이 없으면 종전 폴백(minΔy + FILL_VISUAL_MAX_DY 캡) 경로를 탄다.

    riser 당 점 수는 4→3 으로 줄었다 — 누적 덧셈(0.3/0.6/0.9/1.0)을 균등 분할
    (k=round(1.0/0.3)=3 → 0.333/0.667/1.0)로 바꾼 결과다. 폴백은 어차피 n 을 추정하는
    근사 경로이고, 균등 배치가 riser 끝에 자투리를 남기던 옛 배치보다 옳다.
    중요한 것은 **여전히 캡이 걸려 부풀려진다**는 사실 — 그래서 폴백이지 정답이 아니다.
    """
    res = probe("""
      var out={};
      var a=mkEcdf(rep(1,100));
      // 폴백: stepY = min(max(1.0, 100/2250), 0.3) = 0.3 → riser 당 실제점 포함 3점
      out.fallback = distPointsForDisplay(a.xs,a.ys,1500,undefined).xs.length;
      out.step_fb = distStepY(a.ys,1500,undefined);
      out.step_n  = distStepY(a.ys,1500,a.n);
      // null / 0 도 폴백으로 취급돼야 한다(구버전 응답의 결측 표현)
      out.null_same = distPointsForDisplay(a.xs,a.ys,1500,null).xs.length;
      _emit(out);
    """, "fill_fallback")
    assert abs(res["step_fb"] - 0.3) < 1e-9, f"폴백 stepY 가 {res['step_fb']} (기대 0.3)"
    assert abs(res["step_n"] - 1.0) < 1e-9, f"n 기반 stepY 가 {res['step_n']} (기대 1.0)"
    assert res["fallback"] == 300, f"폴백 경로가 {res['fallback']}점 (기대 300)"
    assert res["fallback"] > 100, "폴백인데 캡이 안 걸렸습니다 — n 분기가 새고 있습니다"
    assert res["null_same"] == res["fallback"], "n=null 이 폴백으로 안 갑니다"
    print(f"[d] 폴백은 여전히 캡 경로 OK (stepY 0.3 → {res['fallback']}점, 실제 100)")


def test_large_sample_capped():
    """(e) 대량 표본은 성능 하한이 지배하고 표시 캡을 넘지 않는다."""
    res = probe("""
      var out={};
      var e=mkEcdf(rep(1,20000));
      out.step = distStepY(e.ys,1500,e.n);
      out.pts  = distPointsForDisplay(e.xs,e.ys,1500,e.n).xs.length;
      // 채움 자체는 fillMax(=cap*1.5=2250) 하한에 걸린다
      out.filled = distFillVertical(e.xs,e.ys,out.step).xs.length;
      _emit(out);
    """, "fill_large")
    assert abs(res["step"] - 100 / 2250) < 1e-9, f"대량 stepY 가 {res['step']}"
    # 고유값 20000 개가 원본이므로 채움은 0(riser 당 1점), 다운샘플이 줄인다
    assert res["filled"] == 20000, f"대량에서 채움이 발생했습니다: {res['filled']}"
    assert res["pts"] < 20000, "표시용 다운샘플이 걸리지 않았습니다"
    print(f"[e] 대량 표본 하한 지배 OK (stepY={res['step']:.5f}, 표시 {res['pts']}점)")


def test_no_x_interpolation():
    """(f) 채움은 세로 방향뿐 — x 값을 새로 만들지 않는다."""
    res = probe("""
      var b=mkEcdf(rep(200,5));
      var f=distFillVertical(b.xs,b.ys,distStepY(b.ys,1500,b.n));
      var uniq={},i; for(i=0;i<f.xs.length;i++) uniq[f.xs[i]]=1;
      // y 는 단조 비감소여야 하고 마지막은 정확히 100
      var mono=true; for(i=1;i<f.ys.length;i++) if(f.ys[i]<f.ys[i-1]) mono=false;
      _emit({uniqX:Object.keys(uniq).length, srcX:b.xs.length,
             mono:mono, last:f.ys[f.ys.length-1],
             maxY:Math.max.apply(null,f.ys)});
    """, "fill_xaxis")
    assert res["uniqX"] == res["srcX"], \
        f"x 고유값이 {res['srcX']}→{res['uniqX']} 로 늘었습니다 (가로 보간 금지)"
    assert res["mono"], "y 가 단조 비감소가 아닙니다"
    assert abs(res["last"] - 100) < 1e-9, f"마지막 누적이 {res['last']} (기대 100)"
    assert res["maxY"] <= 100 + 1e-9, f"누적이 100 을 넘었습니다: {res['maxY']}"
    print("[f] x 불변 · y 단조 · 마지막 100% OK")


def main():
    if not edge_path():
        print("[SKIP] Edge 를 찾지 못해 브라우저 검증을 건너뜁니다")
        return
    test_counts_match_samples()
    test_no_phantom_on_odd_samples()
    test_old_cap_boundary_continuous()
    test_fallback_still_capped()
    test_large_sample_capped()
    test_no_x_interpolation()
    print("[통과] Distribution 세로 채움 = 실제 측정 개수")


if __name__ == "__main__":
    main()

# CODE_TO_PORT — report_server 에서 가져오거나 재구현할 알고리즘

> **eval_analyzer 는 report_server 코드를 import 하지 않는다.** 아래 계산은 report_server 의
> `client/report_generator/` 에 이미 있지만, eval_analyzer 는 **직접 재구현**(또는 함수만 복사)한다.
> 공식·레이아웃을 그대로 옮겨 적어 두니, 이 문서만 보고 구현 가능.

---

## 1. raw 데이터 레이아웃 (honey_parse 정규화 df) — 이걸 전제로 계산
현행 계약([REPORT_GENERATOR_DATA_REQUEST.md], INTEGRATION_CONTRACT §3):
```
DataFrame columns = [SERIAL, SHOT, DUT, XPOS, YPOS, BIN, FAILTNO, item1, item2, ...]
  row 0 = TSEQ (미사용)
  row 1 = TNO       (item별 test 번호 — fail 매핑 키)
  row 2 = STEP      (P1/P2/P3 — 현재 미사용)
  row 3 = UNIT      (item별 단위 → value_type)
  row 4 = HILIM     (item별 상한 = USL)
  row 5 = LOLIM     (item별 하한 = LSL)
  row 6~ = 측정 데이터 (DATA_START_ROW = 6)
열 0~6 = meta (SERIAL/SHOT/DUT/XPOS/YPOS/BIN/FAILTNO), 열 7~ = item 측정값
```
- eval_analyzer 는 이 df 를 컬럼 단위로 읽어 item별 측정 벡터 + lsl/usl(LOLIM/HILIM) 확보.
  per-DUT(=serial) 행 레코드는 만들지 않는다. 구현: `pipeline/ingest.py:_ingest_raw_df`.
- (레거시) 초기 df_honey 레이아웃 `[DUT,XCoord,YCoord,Bin,Serial,items...]` + 중립 dict raw_table 도
  `_ingest_raw_table` 로 지원(하위호환). PASS_BIN = 보통 1.

## 2. cpk / cp / cpl / cpu (★ 정확 공식)
출처: `_builders.py: get_df_cpk_summary(numeric_df, lo_arr, hi_arr)`.
```
입력: numeric_df (행=DUT, 열=item, 측정값), lo_arr/hi_arr (열 정렬 limit)
열별:
  n      = 유한값 개수 (notna().sum())
  mn/mx  = min / max
  med    = median
  avg    = mean
  std    = stdev (ddof=1)   # 표본 표준편차
  cp  = (hi - lo) / (6 * std)
  cpl = (avg - lo) / (3 * std)
  cpu = (hi - avg) / (3 * std)
  cpk = min(cpl, cpu)
유효 조건 can = (n > 1) AND std 유효(NaN·0 아님) AND lo·hi 유효(not NaN)
  → can=False 면 cp/cpl/cpu/cpk = NaN  (n/min/median/max/avg/std 는 그대로)
```
파이썬 재구현 예:
```python
import numpy as np
def cpk_summary(values, lsl, usl):
    v = np.asarray([x for x in values if np.isfinite(x)], dtype=float)
    n = v.size
    if n == 0: return dict(n=0)
    mean = v.mean(); std = v.std(ddof=1) if n > 1 else float("nan")
    out = dict(n=n, min=float(v.min()), max=float(v.max()),
               median=float(np.median(v)), mean=float(mean), stdev=float(std))
    can = (n > 1) and np.isfinite(std) and std != 0 and \
          lsl is not None and usl is not None
    if can:
        out["cp"]  = (usl - lsl) / (6*std)
        out["cpl"] = (mean - lsl) / (3*std)
        out["cpu"] = (usl - mean) / (3*std)
        out["cpk"] = min(out["cpl"], out["cpu"])
    return out
```

## 3. ECDF (누적분포) — cdf_gap 의 기반
출처: `_builders.py: cumulative_distribution_full(values)`.
```python
import numpy as np
def ecdf(values):
    v = np.asarray(values, float); v = v[np.isfinite(v)]
    if v.size == 0: return np.empty(0), np.empty(0)
    uniq, cnt = np.unique(np.sort(v), return_counts=True)
    cum = np.cumsum(cnt) / v.size * 100.0
    return uniq, cum            # (고유값, 누적%)
# cdf_gap = 인접 uniq 사이 누적% 점프가 큰 구간(부분모집단 갭) 탐지
```
- **다운샘플 금지**(report_server 규칙): 모든 포인트 사용. 동일값 구간만 step 으로 압축 허용.

## 4. fail item 판정 (FAILTNO / TNO — 현행 정본)
현행 df 포맷은 테스터가 채운 **FAILTNO** 로 fail 을 식별한다(limit 위반 재판정 아님):
```
serial(측정행) 별 FAILTNO = 그 serial 이 fail 한 test 의 TNO (stop-on-fail, serial당 1개).
fail item  = TNO행에서 FAILTNO 와 같은 TNO 를 가진 item 컬럼.
fail bin   = 그 serial 의 BIN.
fail case  = (fail item, bin)  — FAILTNO==item.TNO 인 serial 들의 BIN 별 집합.
FAILTNO 공란/0/NaN = pass(무fail).
```
구현: `pipeline/ingest.py:_ingest_raw_df`.
- (레거시 raw_table 경로) bin != PASS_BIN AND limit 위반(value < lo | value > hi)으로 fail 판정
  (`_ingest_raw_table`).

## 5. eval_analyzer 신규 feature 공식 (report_server 에 없음 — 새로 구현)
robust 통계 표준값 사용. 임계값은 하드코딩 금지(calibration 분위수).
```
robust_sigma   = 1.4826 * median(|x - median(x)|)      # MAD 기반
spread_norm    = robust_sigma / (USL - LSL)
outlier_ratio  = (|modified_z| > 3.5 인 비율),  modified_z = 0.6745*(x - median)/MAD
                 (또는 Tukey fence: x < Q1-1.5*IQR or x > Q3+1.5*IQR)
skewness       = medcouple (robust)  또는 (mean-median)/std 근사
kurtosis       = 표준 첨도
spec_margin_low  = (mean - LSL) / stdev
spec_margin_high = (USL - mean) / stdev
nearest_spec_side= 'LOW' if spec_margin_low < spec_margin_high else 'HIGH'
limit_hit_ratio  = (값이 limit 에 정확히 닿은 비율; CODE/TRIM 레일 감지에 활용)
density_gap      = KDE/히스토그램 밀도의 골 깊이(이봉 사이)
cdf_gap          = §3 ECDF 의 최대 점프
공간(좌표 필요):
  edge_fail_ratio  = (반경 상위 N% 영역의 fail 비율) / (전체 fail 비율)
  center_fail_ratio= (중심 영역 fail 비율) 유사
  radial_gradient  = 반경 대비 fail 밀도 회귀 기울기
  quadrant_imbalance = 사분면 fail 분포 불균형(max-min)/mean
  x_gradient/y_gradient = x/y 대비 fail 밀도 기울기
  wafer_zone_signature = 구역 패턴 코드(EDGE/CENTER/CLUSTER/RANDOM)
site:
  site_cpk_delta = max(site별 cpk) - min(site별 cpk)
가드:
  n_dut          = fail 표본 수. n_dut < n_min(예 20) 이면 고차모멘트(skew/kurt) 룰 비활성, confidence=low.
```

## 6. 포팅 정책
- §2~4 (cpk/ECDF/fail): 위 공식대로 **재구현**(권장) 하거나, report_server 의 해당 함수를
  eval_analyzer 내부로 **복사(vendor)** 한다. **import 는 금지**.
- §5 (신규 feature): report_server 에 없으므로 eval_analyzer 가 새로 구현.
- 어떤 경우에도 eval_analyzer → report_server 방향 import 없음. (report_server → eval_analyzer 호출만 허용)

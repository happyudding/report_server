# Plotly로 2000+ 산포 차트 서버 효율화

2000개 subject × N개 학교의 CDF(누적분포) 산포 차트를 한 페이지에 표시하면서 **서버 부담을 최소화**하고 **사용자 시각을 끊김 없이 유지**한 설계 노트.

목표 환경 가정:
- 운영 서버 RAM 4GB (다른 function들과 공유)
- 클라이언트 PC RAM 32GB, 내장 GPU, 다소 old CPU
- 동시 사용자 ~10명, 데이터 갱신 하루 수십 번

---

## 전체 흐름 (5단계)

```
[클라이언트]                              [Flask 서버]
1. CSV 업로드 → POST /upload ──────────► 파일 저장 + build 시작
                                         │
2.                                       background thread:
                                          ├─ pandas로 CSV 로드 (~1-2s)
                                          ├─ np.sort + np.unique + cumsum → CDF (~2-3s)
                                          ├─ subject별 JSON 샤드 저장 (~4-5s)
                                          ├─ subject별 SVG 사전 생성 (~5-6s)
                                          └─ cumulative.html 생성 (<1s)
                                         │
3. redirect /view/<id> ◄─────────────────┘
   (빌드 진행 중이면 placeholder + 진행률 polling)

4. 클라이언트 페이지:
   - 2000셀 <img src=".../thumb/<sid>.svg"> eager 로드
     (서버 immutable cache → 첫 1회 fetch, 그 후 disk cache hit)
   - hover 시: fetch JSON → Plotly.newPlot으로 .plot div 활성화
   - viewport 멀어지면 plot 제거 (svg는 영원 노출 유지)

5. 사용자 인터랙션:
   - 휠 스크롤: 서버 요청 0 (모든 svg 캐시)
   - hover: 셀당 chart JSON 1회 fetch
   - zoom/pan: GPU (scattergl)
```

---

## 서버 부담 분석

| 작업 | 시점 | 서버 비용 | 비고 |
|---|---|---|---|
| build (CSV → JSON/SVG) | 1회 (업로드 시) | CPU + 메모리 ~250MB peak, 10-30초 | background thread |
| `/view/<id>` (HTML) | 페이지 진입 | sendfile 0 메모리, ~1ms | 정적 파일 |
| `/api/<id>/thumb/<sid>` | 첫 진입 시 셀당 1회 | sendfile + `Cache-Control: immutable` | 그 후 클라이언트 disk cache hit |
| `/api/<id>/chart/<sid>` | hover 시만 | sendfile, ~1ms | JSON ~수십-수백 KB |
| 휠 스크롤 자체 | — | **0** | 모든 svg 캐시됨 |

→ **빌드 시점에만 CPU/메모리 부담. 그 후엔 sendfile만 → idle 메모리 ~0**.

10명 동시 진입 시: 각자 첫 페이지 로드에서 ~2000개 svg fetch burst. immutable cache 덕에 재방문은 0. 워커당 RSS ~50-100MB로 안정.

---

## 핵심 기법 요약

### 1. 사전 생성 (build-time)

산포 차트를 매 요청 시 그리는 게 아니라 **빌드 단계에서 한 번** 생성:

- **JSON 샤드**: subject별로 Plotly figure spec (`{data, layout}`)을 개별 파일로 저장. 클라이언트가 hover 시 그 한 파일만 fetch.
- **SVG thumb**: 같은 figure를 SVG로 미리 raster화. img tag로 즉시 표시 가능.

빌드 = 단방향 흐름 (CSV → 디스크 산출물). 한 번 빌드된 산출물은 서버 재시작/여러 사용자 접근 무관하게 영구 sendfile.

### 2. Immutable cache + URL 버전 토큰

서버 응답 헤더:
```python
resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
```

URL에 `?v=<build_version>` 토큰을 박아 빌드마다 자동 무효화:
```html
<img src="/api/current/thumb/42?v=1779080518">
```

→ 같은 빌드 동안 브라우저 disk cache 영구 hit. 빌드 갱신 시 v 토큰 바뀌어 새로 fetch.

### 3. 두 레이어 시각 (Thumb + Plot)

```
.cell
├── .thumb   ← <img src=svg>, 영원 노출 (display 절대 안 건드림)
└── .plot    ← Plotly.newPlot 결과, hover 시만 absolute로 위에 덮음
```

- **사용자가 휠 어떻게 굴려도 thumb svg는 늘 보임**
- hover → plotly 활성 → 그 위에 덮어 interactive
- viewport 이탈 → plotly destroy → 자연스럽게 thumb 노출
- plotly가 빈 canvas/lost context여도 thumb이 그 아래 노출 → 빈 박스 0

이것이 가장 robust한 패턴. plotly의 lifecycle과 사용자 시각이 **분리**됨.

### 4. WebGL context LRU

scattergl trace는 WebGL canvas 사용. 브라우저 동시 context 한도 (보통 8-16개) 초과 시 lose 발생.

```js
const _activePlotly = [];
const MAX_ACTIVE_PLOTLY = 8;
function trackActivePlotly(cell) {
  // LRU: 가장 오래된 셀 즉시 destroy
  while (_activePlotly.length > MAX_ACTIVE_PLOTLY) {
    const oldest = _activePlotly.shift();
    destroyPlotly(oldest);
  }
}
```

활성 plotly 셀 수를 명시적으로 제한 → WebGL context 안전.

### 5. 시각 일관성 (downsampling 금지)

CDF 산포는 사용자가 줌인하면 모든 입력 점이 보여야 함. downsampling으로 "겉모습 빠르게, 줌인 시 풀데이터로 교체" 시도했으나:

→ **rough vs full 사이 점 분포 미세 차이 → 민감한 시각에 거슬림**. 사용자 거부.

해법: **항상 풀 raw 데이터** + WebGL 가속 (`scattergl`). 5000+ 점도 GPU로 매끄럽게.
- 데이터 사이즈: subject당 ~60-80KB (가벼움)
- newPlot 시간: ~10-30ms (Plotly setup 오버헤드 지배)

### 6. 빌드 진행률 UI

빌드 ~10-30초 동안 사용자가 페이지에서 기다림. 빈 화면 대신 진행률 표시:

- 서버: background thread에서 `progress_cb(stage, current, total)` 호출 → module-level dict 갱신
- 클라이언트: `/view/<id>`가 cumulative.html 없으면 placeholder HTML 반환 + 1초 polling
- 진행 단계 표시: save_inputs → load_csv → cdf_svg → write_page → done
- 완료 시 자동 `window.location.reload()` → 차트 페이지로 전환

---

## 클라이언트 메모리 / 네트워크

2000 셀, 32GB RAM 환경 기준:

| 항목 | 크기 | 비고 |
|---|---|---|
| HTML 페이지 | ~37KB | placeholder 셀 + JS |
| thumb svg 총합 | ~10-120MB | 데이터 크기에 따라. 첫 1회 다운로드, 그 후 disk cache |
| 활성 plotly (LRU max 8) | ~40MB | WebGL canvas + scattergl 데이터 |
| 메모리 idle | ~100-200MB | 32GB의 ~1% |
| 휠 스크롤 시 네트워크 | **0** | 모든 svg 캐시 |

---

## 안 한 것 (의도적으로 폐기)

| 기법 | 폐기 사유 |
|---|---|
| matplotlib 썸네일 | plotly와 시각 차이 → 사용자 거부 |
| kaleido PNG pre-render | 2000장 ~46분 → 시간 budget 초과 |
| LOD (low/full 교체) | rough/full 점 분포 차이 → 시각 일관성 위반 |
| 전체 priming (모든 셀 newPlot+toImage 사전) | 내장 GPU에서 10-15분 → 사용자 부담 |
| 클라이언트 IndexedDB SVG 캐시 | 서버 immutable cache로 충분, 코드 단순화 |

---

## 한 줄 요약

> **빌드 시 SVG 한 번만 생성 → 서버는 영구 sendfile + immutable cache → 클라이언트는 svg 영원 노출 + hover 시만 plotly. 빌드된 산출물은 disk에 영구. 휠 스크롤은 서버 요청 0건.**

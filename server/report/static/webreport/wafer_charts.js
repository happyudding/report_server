// ── web_report: wafer map + fail-bin 차트 (Plotly) ─────────────────────────
// Pass 는 초록 고정, Fail 은 등장 순서대로 팔레트(dataviz 검증 팔레트에서 초록 제외).
// 색이 넘치면 순환하지만 Fail 셀의 bin 번호 텍스트가 2차 식별자 역할을 한다.
const FAIL_PALETTE = ["#2a78d6","#eda100","#e34948","#4a3aa7","#eb6834","#e87ba4","#1baf7a"];
const PASS_COLOR = "#0ca30c";
const PLOTLY_FONT = { family: 'system-ui, -apple-system, "Segoe UI", sans-serif', size: 11, color: "#52514e" };

// die 수가 이 값을 넘으면 Bin Map 을 이미지 모드(gap=0, Bin 라벨 off)로 그려 SVG 셀 폭증(freeze)을
// 막는다. Detail 확대 시엔 보이는 die 가 적어 forceGap/forceText 로 격자선·라벨을 되살린다.
const MAP_DENSE_DIES = 3000;
// 회색 die(앞 STEP fail — 모양만 유지)·TNO Map 기타 항목 색.
const MAP_GRAY_RGB = [200, 204, 208];
const MAP_GRAY_HEX = "#c8ccd0";
const TNO_OTHER_COLOR = "#9aa0a6";

function webReportSheets() {
  return (DATA && DATA.web_report && DATA.web_report.sheets) ? DATA.web_report.sheets : null;
}

// ── Map die 데이터 지연 로드 (map_deferred 응답용 — distribution.js 패턴과 대칭) ──
// /full 의 sheets["Map Analysis"] 는 dies 를 뺀 경량 메타(범례·격자 틀)만 온다(schema v8).
// die 전량(수백만 개 가능)을 /full 에 실으면 boot 의 res.json() 메인스레드 파싱이 수 초~
// 10초+ 얼어붙으므로, 첫 페인트 후 백그라운드로 GET .../web_report/map_analysis 를
// Worker 파싱으로 받아 rows 에 병합한다. 도착 전에 그려진 갤러리/이슈 미니맵/Detail 은
// refreshMapConsumers 가 다시 채운다. (다운샘플 아님 — die 는 전량 유지, 옮기기만.)
let mapDataReady = false;     // dies 병합 완료 여부
let mapDataPromise = null;    // 진행 중/완료된 fetch (중복 요청 방지)
let _mapContentHash = "";     // 마지막 fetch 시점의 content_hash — 동일하면 재fetch 안 함
let _mapOnDiesReady = null;   // Map Analysis 갤러리 재드로우 훅 (renderMapAnalysis 가 등록)
let _mapLastRes = null;       // 마지막 성공 응답 — load(false) 가 DATA 를 교체한 뒤 새 rows 재병합용

function fetchMapViaWorker(url, onProgress) {
  // 수십 MB JSON 을 Worker 에서 fetch+parse 하고, dies 를 맵(row)당·25만 die 단위 청크로
  // postMessage 해 structured clone 역직렬화 블록을 수십 ms 단위로 분산한다.
  // (Worker 실패 시 호출측 폴백. 콜드 빌드 중이면 202 → {building:true} 로 알린다.)
  return new Promise((resolve, reject) => {
    let blobUrl = null, w = null;
    const cleanup = () => {
      try { if (w) w.terminate(); } catch (e) {}
      try { if (blobUrl) URL.revokeObjectURL(blobUrl); } catch (e) {}
    };
    try {
      const src = 'self.onmessage=function(e){' +
        'fetch(e.data,{cache:"no-cache"})' +
        '.then(function(r){' +
          'if(r.status===202)return{__building:1};' +   // 콜드 빌드 중 — 호출측이 재시도
          'if(!r.ok)throw new Error("HTTP "+r.status);' +
          'if(!r.body||!r.body.getReader)return r.json();' +   // 구형 폴백: 진행 없이 통파싱
          'var reader=r.body.getReader(),chunks=[],loaded=0,lastPost=0;' +
          'function pump(){return reader.read().then(function(res){' +
            'if(res.done){' +
              'var buf=new Uint8Array(loaded),off=0;' +
              'for(var i=0;i<chunks.length;i++){buf.set(chunks[i],off);off+=chunks[i].length;}' +
              'return JSON.parse(new TextDecoder("utf-8").decode(buf));}' +
            'chunks.push(res.value);loaded+=res.value.length;' +
            'if(loaded-lastPost>=2097152){lastPost=loaded;self.postMessage({progress:loaded});}' +
            'return pump();});}' +
          'return pump();' +
        '})' +
        '.then(function(j){' +
          'if(j&&j.__building){self.postMessage({building:true});return;}' +
          'var maps=(j&&j.maps)||[];var LIM=250000;' +
          'var metas=maps.map(function(m){return{source:m.source,step:m.step==null?null:m.step};});' +
          'for(var i=0;i<maps.length;i++){var dies=maps[i].dies||[];var o=0;' +
            'do{self.postMessage({i:i,dies:dies.slice(o,o+LIM)});o+=LIM;}while(o<dies.length);}' +
          'self.postMessage({done:true,format:j&&j.format,metas:metas});' +
        '})' +
        '.catch(function(err){self.postMessage({error:String(err&&err.message||err)});});' +
        '};';
      blobUrl = URL.createObjectURL(new Blob([src], { type: "text/javascript" }));
      w = new Worker(blobUrl);
    } catch (e) { cleanup(); reject(e); return; }
    const diesByMap = [];
    w.onmessage = ev => {
      const d = ev.data || {};
      if (d.error) { cleanup(); reject(new Error(d.error)); return; }
      if (d.building) { cleanup(); resolve({ building: true }); return; }
      if (d.progress != null) { if (onProgress) onProgress(d.progress); return; }
      if (d.dies) {
        // push.apply 는 25만 인자에서 콜스택 상한을 넘으므로 루프로 이어붙인다.
        const arr = diesByMap[d.i] || (diesByMap[d.i] = []);
        for (let k = 0; k < d.dies.length; k++) arr.push(d.dies[k]);
        return;
      }
      if (d.done) { cleanup(); resolve({ format: d.format, metas: d.metas || [], diesByMap }); }
    };
    w.onerror = () => { cleanup(); reject(new Error("worker failed")); };
    // blob URL Worker 의 상대경로 기준이 페이지와 달라질 수 있어 절대 URL 로 전달
    w.postMessage(new URL(url, location.origin).href);
  });
}

// 응답의 dies 를 현재 DATA 의 Map Analysis rows 에 병합한다.
// load(false)(편집 후 재로딩)는 DATA 를 통째로 교체하므로, 같은 응답으로 여러 번 호출될 수 있다.
function mergeMapDies(res) {
  const cur = (webReportSheets() || {})["Map Analysis"] || [];
  (res.diesByMap || []).forEach((dies, i) => {
    const m = cur[i], meta = (res.metas || [])[i];
    // /full 경량 rows 와 같은 빌더(strip_dies 전) 출력이라 인덱스가 일치한다 —
    // source/step 대조는 안전장치(불일치 row 는 placeholder 유지).
    if (!m || !meta || m.source !== meta.source ||
        (m.step == null ? null : m.step) !== meta.step) return;
    m.dies = dies || [];
    delete m._compact;   // dies 없이 캐시됐을 수 있는 압축 격자 무효화
  });
  mapDataReady = true;
}

// dies 콜드 빌드는 서버가 202 를 즉시 주고 백그라운드에서 만든다(요청 스레드 비블록).
// 완료될 때까지 재요청한다 — boot.js retryWhileBuilding 과 같은 규약(1s→5s 백오프).
// Worker 실패 시 메인스레드 폴백도 같은 202 규약을 따른다.
const MAP_RETRY = { START_MS: 1000, MAX_MS: 5000, GROWTH: 1.4, TIMEOUT_MS: 15 * 60 * 1000 };
async function fetchMapUntilBuilt(url, label) {
  const once = () => fetchMapViaWorker(url, loaded => distBadgeProgress(label, loaded))
    .catch(() => fetch(url, { cache: "no-cache" })   // Worker 실패 시 메인스레드 폴백
      .then(r => {
        if (r.status === 202) return { building: true };
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json().then(j => ({
          format: j && j.format,
          metas: ((j && j.maps) || []).map(m => ({ source: m.source, step: m.step == null ? null : m.step })),
          diesByMap: ((j && j.maps) || []).map(m => m.dies || []),
        }));
      }));
  let wait = MAP_RETRY.START_MS;
  const deadline = Date.now() + MAP_RETRY.TIMEOUT_MS;
  let res = await once();
  while (res && res.building && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, wait));
    wait = Math.min(MAP_RETRY.MAX_MS, Math.round(wait * MAP_RETRY.GROWTH));
    res = await once();
  }
  if (res && res.building) throw new Error("맵 계산이 제한 시간 안에 끝나지 않았습니다");
  return res;
}

function ensureMapData() {
  const maps = (webReportSheets() || {})["Map Analysis"] || [];
  // 하위호환: 구 스키마(v7 이하)는 dies 가 /full 에 이미 실려 온다 — fetch 없이 즉시 ready.
  if (!maps.length || maps.every(m => Array.isArray(m.dies))) {
    mapDataReady = true;
    return Promise.resolve();
  }
  const ch = (DATA && DATA.session && DATA.session.content_hash) || "";
  if (mapDataPromise && ch === _mapContentHash) {   // 로딩 중/완료 재사용
    // load(false) 재로딩으로 DATA 가 교체됐으면 이미 받은 dies 를 새 rows 에 다시 붙인다
    // (편집은 content_hash 를 바꾸지 않아 재fetch 하지 않으므로, 재병합 없이는 dies 유실).
    // 아직 로딩 중이면 진행 중 promise 의 .then 이 완료 시점의 현재 rows 에 병합한다.
    // 재드로우는 마이크로태스크로 미룬다 — load() 의 renderActive 뒤에 돌게 하고,
    // drawMap 폴백 호출에서 드로우 루프에 재진입하지 않게 한다.
    if (_mapLastRes) {
      mergeMapDies(_mapLastRes);
      Promise.resolve().then(refreshMapConsumers);
    }
    return mapDataPromise;
  }
  _mapContentHash = ch;
  _mapLastRes = null;   // 다른 content_hash — 옛 응답을 새 rows 에 병합하지 않는다
  mapDataReady = false;
  const url = `/pe/report/session/${SESSION_ID}/web_report/map_analysis`;
  const label = "맵 데이터 로딩 중…";
  distBadgeStart(label);
  mapDataPromise = fetchMapUntilBuilt(url, label)
    .then(res => {
      _mapLastRes = res;
      mergeMapDies(res);
      distBadgeEnd();
      refreshMapConsumers();
    })
    .catch(e => {
      mapDataPromise = null;   // 실패 시 다음 호출/재시도 버튼에서 재요청
      distBadgeFail("맵 데이터 로드 실패", "map");
      showToast("맵 데이터 로드 실패: " + e.message);
    });
  return mapDataPromise;
}

// ── Temperature 항목별 fail die 인덱스 (2026-08-05) ──────────────────────────
// CT/HT 맵은 bin legend 대신 **RT Limit 이탈 항목 legend** 로 색을 낸다(사용자 확정).
// 서버(GET .../web_report/temp_map)가 좌표 대신 dies 배열 **인덱스**만 내려주므로
// payload 가 작고, maps[].dies 와 같은 순서라 인덱스로 바로 색을 칠할 수 있다.
let tempMapReady = false;
let tempMapPromise = null;
let tempMapBySource = {};   // source → {n, items:[{item, idx:[...]}]}

// 실패해도 promise 를 즉시 비우면, 미니셀 수백 개가 스크롤될 때마다 새 fetch 를 쏘고
// 토스트도 그만큼 뜬다(미니셀 렌더러가 !tempMapReady 면 ensure 를 다시 부르기 때문).
// 실패 상태를 유지하고 배지의 재시도 버튼(또는 백오프 경과)으로만 다시 시도한다.
const TEMP_MAP_RETRY_MS = 15000;
let _tempMapFailedAt = 0;

function ensureTempMapData(force) {
  if (webReportMode() !== "Temperature") { tempMapReady = true; return Promise.resolve(); }
  if (tempMapPromise) return tempMapPromise;
  if (_tempMapFailedAt && !force && (Date.now() - _tempMapFailedAt) < TEMP_MAP_RETRY_MS) {
    return Promise.resolve();   // 백오프 중 — 조용히 넘긴다(배지에 재시도 버튼이 떠 있다)
  }
  _tempMapFailedAt = 0;
  tempMapPromise = fetch(`/pe/report/session/${SESSION_ID}/web_report/temp_map`)
    .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(j => {
      const out = {};
      ((j && j.sources) || []).forEach(s => { out[s.source] = s; });
      tempMapBySource = out;
      tempMapReady = true;
      refreshMapConsumers();
      // Map Analysis 가 Temperature Map 축으로 열려 있으면 색을 다시 칠한다.
      if (mapColorKey === "temp") {
        const p = document.getElementById("panel-map-analysis");
        if (p && p.classList.contains("active")) renderMapAnalysis();
        else tabDirty["map-analysis"] = true;
      }
    })
    .catch(e => {
      tempMapPromise = null;
      _tempMapFailedAt = Date.now();
      distBadgeFail("Temp Map 데이터 로드 실패", "tempmap");   // 재시도 버튼(distribution.js)
      showToast("Temp Map 데이터 로드 실패: " + e.message);
    });
  return tempMapPromise;
}

// source 이름 → Temperature corner("RT"/"CT"/"HT", 그룹 밖이면 ""). 정본은 payload
// sources[].temp_corner (서버 metrics._temperature_context) — 맵 축 필터(RT ↔ CT/HT)와
// Issue Table Temp 미니셀 색(CT=파랑/HT=빨강)이 **같은 판정**을 쓰도록 여기 한 곳에 둔다.
function tempCornerOf(source) {
  const list = (DATA && DATA.web_report && DATA.web_report.sources) || [];
  const s = list.find(x => x && x.name === source);
  return String((s && s.temp_corner) || "");
}
function tempIsMemberSource(source) {
  const c = tempCornerOf(source);
  return c === "CT" || c === "HT";
}

// 항목명으로 그 항목이 fail 난 소스별 인덱스를 찾는다 (미니셀·⤢ 확장·legend 공용).
function tempMapItemEntries(item) {
  const out = [];
  Object.keys(tempMapBySource).forEach(src => {
    const e = ((tempMapBySource[src] || {}).items || []).find(x => x.item === item);
    if (e) out.push({ source: src, idx: e.idx || [] });
  });
  return out;
}

// 그 소스의 die 인덱스 → 항목명 (legend 순서상 앞선 항목이 이긴다).
// items 는 표시 순서(fail 많은 순) 배열, only 가 있으면 그 항목들만 칠한다.
function tempPrimaryByIdx(source, items, only) {
  const pack = tempMapBySource[source];
  if (!pack) return null;
  const byItem = {};
  (pack.items || []).forEach(e => { byItem[e.item] = e.idx || []; });
  const out = new Array(pack.n || 0).fill(null);
  // 뒤에서부터 칠해 앞 항목(=legend 상위)이 마지막에 덮어쓰게 한다.
  for (let i = items.length - 1; i >= 0; i--) {
    const name = items[i];
    if (only && only.size && !only.has(name)) continue;
    const idx = byItem[name];
    if (!idx) continue;
    for (let k = 0; k < idx.length; k++) out[idx[k]] = name;
  }
  return out;
}

// dies 도착 전에 만들어진 map 소비처들을 다시 채운다 (refreshDistConsumers 와 대칭).
function refreshMapConsumers() {
  // Issue Table Map 미니셀 — 화면에 보이는(관측 중) 셀만 rAF 큐로 재큐잉.
  issuePanelsQueryAll('.map-cell-mini[data-visible="1"]').forEach(issueMapQueueRender);
  // Map Analysis 갤러리 — 활성 탭이면 캔버스만 재드로우(범례 필터 상태 유지),
  // 비활성이면 dirty 로 표시해 다음 탭 진입 시 정상 폭으로 새로 그린다(숨김 0폭 드로우 회피).
  const panel = document.getElementById("panel-map-analysis");
  if (panel && panel.classList.contains("active")) {
    if (_mapOnDiesReady) _mapOnDiesReady();
  } else {
    tabDirty["map-analysis"] = true;
  }
  // Map Detail 이 dies 대기 placeholder 상태면 다시 그림.
  const dp = document.getElementById("panel-map-detail");
  if (dp && dp.classList.contains("active") && dp.dataset.mapWaiting === "1") renderMapDetail();
}

function makeBinColorMap(binList) {
  const map = {}; let fi = 0;
  binList.forEach(b => {
    if (b === "1") map[b] = PASS_COLOR;
    else { map[b] = FAIL_PALETTE[fi % FAIL_PALETTE.length]; fi++; }
  });
  return map;
}

// 세션 전체에서 같은 bin 은 어느 차트(Summary 미니 웨이퍼/Fail Bin 막대/Yield Pareto/
// Map Analysis 범례)에서든 같은 색이 되도록, Map Analysis 전역 범례 순서를 기준으로
// 색상 매핑을 한 번만 만든다. load() 가 DATA 갱신 시 _globalBinColors 를 초기화한다.
let _globalBinColors = null;
function globalBinColorMap() {
  if (_globalBinColors) return _globalBinColors;
  const sheets = webReportSheets() || {};
  const bins = [];
  buildGlobalBinLegend(sheets["Map Analysis"] || []).forEach(r => {
    const b = String(r.bin);
    if (!bins.includes(b)) bins.push(b);
  });
  (sheets["Fail Bin"] || []).forEach(r => {
    const b = String(r.bin);
    if (!bins.includes(b)) bins.push(b);
  });
  _globalBinColors = makeBinColorMap(bins);
  return _globalBinColors;
}

// 전역 매핑에 없던 bin(좌표 없는 die 등)도 안정적으로 이어서 색을 배정.
function binColor(bin) {
  const map = globalBinColorMap();
  const b = String(bin);
  if (!(b in map)) {
    const failCount = Object.values(map).filter(c => c !== PASS_COLOR).length;
    map[b] = (b === "1") ? PASS_COLOR : FAIL_PALETTE[failCount % FAIL_PALETTE.length];
  }
  return map[b];
}

// 소스 map dict → { trace } (die → 이산 heatmap). opts.catOf(die→카테고리, 기본 bin)/order/colorMap
// 로 Bin·TNO·회색 등 어떤 축이든 색칠(여러 맵 간 색 통일용). Detail(Plotly)·Compare·미니맵 공용.
// (맵 셀 bin 번호 텍스트는 표시하지 않는다 — Bin 은 Legend/ hover 로 확인.)
function waferHeatmap(m, opts) {
  opts = opts || {};
  // opts.grid = compact grid(빈 행/열 제거). 있으면 격자를 raw span 대신 distinct 좌표 수로
  // 잡아 셀 수를 die 수(≈)로 묶는다 — 넓은 span/이상치 좌표에도 메모리 폭증 없음. Detail 전용.
  const grid = opts.grid || null;
  const xMin = m.x_min, xMax = m.x_max, yMin = m.y_min, yMax = m.y_max;
  if (grid) { if (!(grid.W > 0) || !(grid.H > 0)) return null; }
  else if (xMin == null || yMin == null) return null;
  const W = grid ? grid.W : xMax - xMin + 1, H = grid ? grid.H : yMax - yMin + 1;
  const order = opts.order || opts.binOrder || (m.bin_counts || []).map(bc => bc.bin);
  const colorMap = opts.colorMap || makeBinColorMap(order);
  const catOf = opts.catOf || (d => d.bin);   // die → 색 카테고리(기본 bin). TNO/회색은 호출부가 지정.
  const catIndex = {}; order.forEach((c, i) => { catIndex[c] = i; });
  const N = order.length || 1;

  // die 가 조밀하면(임계 초과) 셀 gap>0 은 Plotly 가 die 마다 SVG rect(brick)를 만들어 무겁다
  // → gap=0 이미지 모드. opts.forceGap 은 Detail 확대 시 격자선 복원용. die 는 전량 유지(다운샘플 아님).
  const dense = (m.dies || []).length > MAP_DENSE_DIES;
  const useGap = opts.forceGap || !dense;

  const z = Array.from({ length: H }, () => Array(W).fill(null));
  const cdata = Array.from({ length: H }, () => Array(W).fill(""));
  // k(die 인덱스)를 catOf 2번째 인자로 넘긴다 — 인덱스 기반 색칠(Temperature Map 축)용.
  // 기존 catOf 는 인자를 무시하므로 무회귀 (drawWaferThumb 의 rgbFor 와 같은 규약).
  (m.dies || []).forEach((d, k) => {
    const c = grid ? grid.xIdx[d.x] : d.x - xMin;
    const r = grid ? grid.yIdx[d.y] : d.y - yMin;
    if (r == null || c == null || r < 0 || r >= H || c < 0 || c >= W) return;
    const cat = catOf(d, k);
    const idx = catIndex[cat] != null ? catIndex[cat] : 0;
    z[r][c] = idx + 0.5;
    // compact 는 index 공간이라 %{x}/%{y} 가 실제 좌표가 아니다 → hover 문자열에 실제 좌표를 담는다.
    // opts.labelOf 는 hover 문자열을 갈아끼우는 훅 (Compare 공통성 Map 이 source 별 Bin 을
    // 덧붙이는 데 쓴다). 미지정이면 종전과 동일.
    const label = opts.labelOf ? opts.labelOf(d, cat)
      : (d.g ? "(prev-fail)" : cat);           // hover 표시(회색 die 는 이전 step fail)
    cdata[r][c] = grid ? ("(" + d.x + ", " + d.y + ")<br>" + label) : label;
  });

  const trace = {
    type: "heatmap", z, zmin: 0, zmax: N,
    x0: grid ? 0 : xMin, dx: 1, y0: grid ? 0 : yMin, dy: 1,
    colorscale: binColorscale(order, colorMap),
    showscale: false, xgap: useGap ? 0.5 : 0, ygap: useGap ? 0.5 : 0, hoverongaps: false,
    customdata: cdata,
  };
  if (opts.mini) trace.hoverinfo = "skip";
  else if (grid) trace.hovertemplate = "%{customdata}<extra></extra>";
  else trace.hovertemplate = opts.hovertemplate || "(%{x}, %{y})<br>%{customdata}<extra></extra>";
  return { trace, colorMap, binOrder: order };
}

// bin 이산 colorscale (각 bin 을 두 정지점으로 계단화). waferHeatmap·범례 색 restyle 공용.
function binColorscale(binOrder, colorMap) {
  const cs = []; const N = binOrder.length || 1;
  binOrder.forEach((b, i) => { cs.push([i / N, colorMap[b]]); cs.push([(i + 1) / N, colorMap[b]]); });
  return cs.length ? cs : [[0, PASS_COLOR], [1, PASS_COLOR]];
}

// 웨이퍼는 원형이 표준 — die pitch 가 정사각이 아니거나(tall chip) XPOS/YPOS stride 로 빈
// 행/열이 끼어 축별 격자 수가 달라도, 격자 폭/높이(W/H)만큼 Y 셀을 늘려(scaleratio) 항상
// 원형(1:1) 틀로 그린다. (초기엔 MDDI/PDDI 한정 보정이었으나 전 제품으로 일반화.)
function waferCellYScale(m) {
  if (m.x_min == null || m.y_min == null) return 1;
  const W = m.x_max - m.x_min + 1, H = m.y_max - m.y_min + 1;
  return (W > 0 && H > 0) ? W / H : 1;
}

// compact 축 눈금: index(0..n-1) → 실제 좌표(vals). 양끝 포함 ~8개 균등 샘플.
function _compactTicks(vals) {
  const n = (vals || []).length;
  if (!n) return null;
  const cnt = Math.min(n, 8);
  const tickvals = [], ticktext = [], seen = {};
  for (let i = 0; i < cnt; i++) {
    const idx = cnt === 1 ? 0 : Math.round(i * (n - 1) / (cnt - 1));
    if (seen[idx]) continue;
    seen[idx] = 1;
    tickvals.push(idx);
    ticktext.push(String(vals[idx]));
  }
  return { tickvals, ticktext };
}

function waferLayout(m, opts) {
  opts = opts || {};
  // opts.grid = compact grid → 축이 index 공간이므로 비율은 compact W/H, 눈금은 실제 좌표로.
  const grid = opts.grid || null;
  const ratio = grid ? (grid.H > 0 ? grid.W / grid.H : 1) : waferCellYScale(m);
  const xt = grid ? _compactTicks(grid.xs) : null;
  const yt = grid ? _compactTicks(grid.ys) : null;
  const layout = {
    margin: opts.mini ? { l: 2, r: 2, t: 2, b: 2 } : { l: 42, r: 10, t: 8, b: 36 },
    paper_bgcolor: "#ffffff", plot_bgcolor: "#ffffff", font: PLOTLY_FONT,
    showlegend: false,
    // 셀은 x 에 scale 고정. 정사각(=1)이 기본, MDDI/PDDI 는 tall chip 반영해 y 를 늘림.
    xaxis: { zeroline: false, showgrid: false, constrain: "domain",
             title: opts.mini ? "" : "X", visible: !opts.mini },
    // 웨이퍼 맵 관례: Y 는 위에서 아래로 내려갈수록 커진다(=y축 역방향).
    yaxis: { zeroline: false, showgrid: false, constrain: "domain",
             scaleanchor: "x", scaleratio: ratio, autorange: "reversed",
             title: opts.mini ? "" : "Y", visible: !opts.mini },
  };
  if (xt) { layout.xaxis.tickmode = "array"; layout.xaxis.tickvals = xt.tickvals; layout.xaxis.ticktext = xt.ticktext; }
  if (yt) { layout.yaxis.tickmode = "array"; layout.yaxis.tickvals = yt.tickvals; layout.yaxis.ticktext = yt.ticktext; }
  return layout;
}

// 모든 소스의 bin_counts 를 합산해 하나의 공통 범례 순서/집계를 만든다.
// Pass 항상 최상단, 나머지는 전체 합산 count 내림차순.
// step 분리 맵(m.step 있음)은 Pass 칩이 step 수만큼 중복 등장하므로 Pass 는 소스별
// step 맵 중 최솟값(=마지막 step 의 Pass = 전체 Pass)만 반영한다. fail bin 은 칩당
// 한 step 맵에만 나오므로 그대로 합산. step 없는 맵(단일 STEP/legacy)은 현행 합산.
function buildGlobalBinLegend(maps) {
  const totals = {};
  const order = [];
  const stepPassBySource = {};   // source → step 맵별 Pass count 목록
  (maps || []).forEach(m => {
    let stepPass = 0;
    (m.bin_counts || []).forEach(bc => {
      if (!(bc.bin in totals)) { order.push(bc.bin); totals[bc.bin] = { count: 0, is_pass: bc.is_pass }; }
      if (bc.is_pass && m.step != null) stepPass += bc.count;
      else totals[bc.bin].count += bc.count;
    });
    if (m.step != null) {
      (stepPassBySource[m.source] = stepPassBySource[m.source] || []).push(stepPass);
    }
  });
  const passBin = order.find(b => totals[b].is_pass);
  if (passBin != null) {
    Object.values(stepPassBySource).forEach(arr => {
      totals[passBin].count += Math.min.apply(null, arr);
    });
  }
  order.sort((a, b) => {
    const pa = totals[a].is_pass, pb = totals[b].is_pass;
    if (pa !== pb) return pa ? -1 : 1;
    return totals[b].count - totals[a].count;
  });
  const grandTotal = order.reduce((s, b) => s + totals[b].count, 0);
  return order.map(b => ({
    bin: b, count: totals[b].count, is_pass: totals[b].is_pass,
    pct: grandTotal ? Math.round((totals[b].count / grandTotal) * 10000) / 100 : 0,
  }));
}

// selected 는 단일 bin(문자열) 또는 Set(다중선택) 둘 다 지원. 선택 행에 is-selected + data-bin.
function _binIsSelected(selected, bin) {
  if (selected instanceof Set) return selected.has(bin);
  return selected != null && bin === selected;
}
function binLegendHtml(legendRows, colorMap, selected, descMap) {
  const desc = descMap || {};
  const body = legendRows.map(bc => {
    const cls = [bc.is_pass ? "is-pass" : "", _binIsSelected(selected, bc.bin) ? "is-selected" : ""]
      .filter(Boolean).join(" ");
    const d = bc.is_pass ? "" : (desc[String(bc.bin)] || "");
    return `<tr${cls ? ` class="${cls}"` : ""} data-bin="${esc(bc.bin)}">` +
      `<td><span class="bin-swatch" style="background:${colorMap[bc.bin]}"></span>${esc(bc.bin)}${bc.is_pass ? " (Pass)" : ""}</td>` +
      `<td class="bin-desc" title="${esc(d)}">${esc(d)}</td>` +
      `<td>${bc.count}</td><td>${bc.pct}%</td></tr>`;
  }).join("");
  return `<table class="bin-table"><thead><tr><th>Bin</th><th>Description</th><th>Count</th><th>비율</th></tr></thead>` +
         `<tbody>${body}</tbody></table>`;
}

// DUT Legend (DUT 모드 병합 맵 전용) — 클릭 시 해당 DUT 강조. 색 스와치 없음(색은 bin 기준).
function dutLegendHtml(dutList, selected) {
  const body = (dutList || []).map(d =>
    `<tr${d === selected ? ` class="is-selected"` : ""} data-dut="${esc(d)}"><td>DUT ${esc(d)}</td></tr>`).join("");
  return `<table class="bin-table dut-table"><tbody>${body}</tbody></table>`;
}

// 선택된 bin만 원색 유지, 나머지는 회색으로 dim. selected 는 단일 bin(문자열) 또는 Set(다중).
const MAP_BIN_DIM_COLOR = "#d9d9d9";
function dimColorMap(colorMap, binOrder, selected) {
  const isSet = selected instanceof Set;
  if (!selected || (isSet && selected.size === 0)) return colorMap;
  const out = {};
  binOrder.forEach(b => { out[b] = _binIsSelected(selected, b) ? colorMap[b] : MAP_BIN_DIM_COLOR; });
  return out;
}

// source 가 여럿이면 가로 2칸 그리드로 wafer map 을 나열하고, bin 범례는 전체 소스
// 합산 기준으로 한 번만 만들어 오른쪽에 고정(sticky)한다. 모든 맵이 같은 색상 매핑을 쓴다.
let mapGridCols = 2;   // Map Analysis 가로 칸수 기본 2칸. 숫자 입력으로 조절, 세션 내 유지.
// source 가 많으면 2칸으로는 세로로 한없이 길어져 훑기 어렵다 — 이 수 이상이면 기본값을
// 4칸으로 올린다(사용자 요청 2026-08-06). 사용자가 한 번이라도 칸수를 바꾸면 그 값을 존중한다.
const MAP_GRID_WIDE_SOURCES = 7;
let mapGridColsUserSet = false;
// 갤러리 카드 크기는 가로 칸수(폭)로 결정되고, 썸네일 wrap 은 항상 1:1(웨이퍼=원형 전제).

// Map Analysis 서브모드: "bin"=Bin Map(기존), "stdf"=STDF Map(값 기반, stdf_map.js). 세션 내 유지.
let mapMode = "bin";
// Issue Table Map 미니셀 → Map Analysis 탭 이동 시 넘겨줄 초기 선택 Bin (1회성 — 첫
// renderMapAnalysis 가 범례 선택으로 소비하고 비운다. 가로 칸수 변경 등 이후 재렌더는
// 기존대로 선택이 초기화된다).
let mapBinPreselect = null;
// Map 초기 그리기(rAF 스텝퍼) 재진입 가드 — 새 렌더가 시작되면 이전 체인을 중단시킨다.
let _mapDrawToken = 0;
// 색 기준 축: "bin"=Bin Map(기존), "tno"=die 가 fail 난 항목(FAILTNO)별 색,
// "temp"=Temperature Map(CT/HT 를 RT Limit 이탈 항목별 색). 세션 내 유지.
let mapColorKey = "bin";

// 갤러리·Detail 이 보여줄 맵 목록 — **두 화면이 항상 같은 목록을 본다**
// (_mapDetailIndex 도 이 목록 기준이라 인덱스가 어긋나지 않는다).
// Temperature 모드는 색 기준 축으로 소스를 가른다 (사용자 확정 2026-08-06):
//   Bin/TNO 축 = RT 소스만 · Temperature Map 축 = CT/HT 소스만.
// 다른 모드는 전체 그대로.
function mapVisibleMaps() {
  const all = (webReportSheets() || {})["Map Analysis"] || [];
  if (webReportMode() !== "Temperature") return all;
  return (mapColorKey === "temp")
    ? all.filter(m => tempIsMemberSource(m.source))
    : all.filter(m => !tempIsMemberSource(m.source));
}

// ── canvas 썸네일 렌더 (갤러리) ────────────────────────────────────────────────
// hex → [r,g,b]. 6자리 hex 만 지원(팔레트가 전부 6자리).
function hexToRgb(hex) {
  const h = String(hex || "").replace("#", "");
  return [parseInt(h.slice(0, 2), 16) || 0, parseInt(h.slice(2, 4), 16) || 0, parseInt(h.slice(4, 6), 16) || 0];
}
// 흰색과 블렌드해 흐리게(DUT 미선택 die). k=원색 비율.
function fadeRgb(rgb, k) {
  return [Math.round(rgb[0] * k + 255 * (1 - k)),
          Math.round(rgb[1] * k + 255 * (1 - k)),
          Math.round(rgb[2] * k + 255 * (1 - k))];
}
// die 가 실제 존재하는 x/y 값만 남긴 압축 격자 — XPOS/YPOS 가 stride(띄엄띄엄)여도 완전히
// 빈 행/열을 제거해 갤러리 썸네일이 빈 스트라이프 없이 그려지게 한다. 썸네일·선택 마커 공용.
// m._compact 에 캐시(load() 가 DATA 를 통째로 교체하므로 무효화 불필요).
function waferCompactGrid(m) {
  if (m._compact) return m._compact;
  if (!Array.isArray(m.dies)) return { xIdx: {}, yIdx: {}, W: 0, H: 0 };   // dies 미도착 — 캐시하지 않음
  const xSeen = {}, ySeen = {};
  (m.dies || []).forEach(d => { xSeen[d.x] = 1; ySeen[d.y] = 1; });
  const xs = Object.keys(xSeen).map(Number).sort((a, b) => a - b);
  const ys = Object.keys(ySeen).map(Number).sort((a, b) => a - b);
  const xIdx = {}, yIdx = {};
  xs.forEach((v, i) => { xIdx[v] = i; });
  ys.forEach((v, i) => { yIdx[v] = i; });
  // xs/ys = index→실제좌표 역매핑(Detail 축 tick 라벨용). 갤러리·마커는 무시(하위호환).
  m._compact = { xIdx, yIdx, W: xs.length, H: ys.length, xs, ys };
  return m._compact;
}

// die 격자(압축)를 canvas 에 그린다 — die 당 cell px 블록 + 셀 사이 1px 격자선(투명=흰 카드
// 노출)으로 각 chip 구분·윤곽선을 유지(Plotly xgap brick 과 동일 시각, SVG 없이 픽셀 한 번에
// → 빠름). rgbFor(die, cache) → [r,g,b] 또는 null(그리지 않음). Y 는 위=작은 값(웨이퍼 관례).
function drawWaferThumb(canvas, m, rgbFor) {
  const g = waferCompactGrid(m);
  const W = g.W, H = g.H;
  if (!(W > 0) || !(H > 0)) return;
  // cell 을 실제 표시 크기(device px)에 맞춰 CSS 확대 배율을 1에 가깝게 유지한다 — 고정 해상도
  // (구 1600px 상한)를 CSS 로 늘리면 bilinear 보간 번짐(blur·눈부심)이 생겼다. floor 라
  // canvas ≤ 표시폭(축소 없음)이고, 잔여 소수 배율은 CSS image-rendering:pixelated 가 처리.
  // wrap 은 항상 1:1 정사각(웨이퍼=원형 전제)이라 X/Y cell 을 wrap 폭 기준으로 따로 잡아
  // W≠H 여도 격자선이 양축 모두 1px 로 균일하다.
  const dpr = window.devicePixelRatio || 1;
  const wrapW = (canvas.parentElement && canvas.parentElement.clientWidth) || 300;
  const px = Math.round(wrapW * dpr);
  function cellFor(n) {
    let c = Math.floor(px / n);
    const cap = Math.floor(4096 / n);   // 캔버스 픽셀 상한(메모리 보호)
    if (c > cap) c = cap;
    if (c < 2) c = 2;   // 최소 2(1px 격자선 확보)
    return c;
  }
  const cellX = cellFor(W), cellY = cellFor(H);
  const gapX = cellX >= 3 ? 1 : 0, gapY = cellY >= 3 ? 1 : 0;   // cell 이 너무 작으면 격자선 생략
  const CW = W * cellX, CH = H * cellY;
  canvas.width = CW; canvas.height = CH;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(CW, CH);
  const data = img.data;
  const cache = {};
  const dies = m.dies || [];
  const w = cellX - gapX, h = cellY - gapY;
  for (let k = 0; k < dies.length; k++) {
    const d = dies[k];
    const cx = g.xIdx[d.x], cy = g.yIdx[d.y];
    if (cx == null || cy == null) continue;
    // k(die 인덱스)를 3번째 인자로 넘긴다 — 인덱스 기반 색칠(Temperature Map 축·Temp 미니셀)용.
    // 기존 콜백은 인자를 무시하므로 무회귀.
    const rgb = rgbFor(d, cache, k);
    if (!rgb) continue;
    const px0 = cx * cellX, py0 = cy * cellY;
    for (let yy = 0; yy < h; yy++) {
      let off = ((py0 + yy) * CW + px0) * 4;
      for (let xx = 0; xx < w; xx++) {
        data[off] = rgb[0]; data[off + 1] = rgb[1]; data[off + 2] = rgb[2]; data[off + 3] = 255;
        off += 4;
      }
    }
  }
  ctx.putImageData(img, 0, 0);
}

// sheets["Yield"] → bin 별 대표 fail item(most fail = avg 최대). Bin Legend description 용.
function buildBinDescMap() {
  const yl = (webReportSheets() || {})["Yield"] || [];
  const best = {};   // bin → {item, avg}
  yl.forEach(r => {
    const b = String(r.bin);
    if (!b || b === "1" || !r.Item) return;
    const avg = Number(r.avg) || 0;
    if (!(b in best) || avg > best[b].avg) best[b] = { item: r.Item, avg };
  });
  const desc = {};
  Object.keys(best).forEach(b => { desc[b] = best[b].item; });
  return desc;
}

// sheets["Fail Bin"](fail_bin_ranking {bin,item,count}) → fail item count 집계.
// 상위 FAIL_PALETTE 개만 팔레트 색(top), 나머지는 "기타"(중립색). TNO Map/TNO Legend 용.
function buildTnoInfo() {
  const fb = (webReportSheets() || {})["Fail Bin"] || [];
  const cnt = {};
  fb.forEach(r => { const it = r.item; if (it) cnt[it] = (cnt[it] || 0) + (Number(r.count) || 0); });
  const items = Object.keys(cnt).sort((a, b) => cnt[b] - cnt[a]);
  const colorMap = {};
  items.forEach((it, i) => { colorMap[it] = (i < FAIL_PALETTE.length) ? FAIL_PALETTE[i] : TNO_OTHER_COLOR; });
  const top = items.slice(0, FAIL_PALETTE.length);
  const otherCount = items.slice(FAIL_PALETTE.length).reduce((s, it) => s + cnt[it], 0);
  return { colorMap, items, top, cnt, otherCount };
}

// TNO Legend 표(상위 항목 색+count, 나머지 "기타" 1행). 클릭 dim 은 상위 항목만(data-tno).
function tnoLegendHtml(tnoInfo, selected) {
  const body = tnoInfo.top.map(it => {
    const sel = selected.has(it);
    const sw = (selected.size === 0 || sel) ? tnoInfo.colorMap[it] : MAP_BIN_DIM_COLOR;
    return `<tr${sel ? ` class="is-selected"` : ""} data-tno="${esc(it)}">` +
      `<td><span class="bin-swatch" style="background:${sw}"></span>${esc(it)}</td>` +
      `<td>${tnoInfo.cnt[it] || 0}</td></tr>`;
  }).join("");
  const other = tnoInfo.otherCount > 0
    ? `<tr class="tno-other"><td><span class="bin-swatch" style="background:${TNO_OTHER_COLOR}"></span>기타</td>` +
      `<td>${tnoInfo.otherCount}</td></tr>` : "";
  if (!body && !other) return `<div class="placeholder" style="padding:12px 4px">fail 항목 없음</div>`;
  return `<table class="bin-table"><thead><tr><th>Item</th><th>Count</th></tr></thead>` +
         `<tbody>${body}${other}</tbody></table>`;
}

// ── Temperature Map Legend (Temperature 전용 색 기준 축) ─────────────────────
// 목록·순서·TNO·Bin 은 서버 Temp 시트(sheets["Issue Table Temp"])를 그대로 따른다 —
// Yield 탭 하단 Temp Corner 표와 같은 항목·같은 순서가 되게 한다(사용자 요청).
// hsl → #rrggbb (팔레트 밖 항목 색 생성용). h=0~360, s/l=0~100.
function hslHex(h, s, l) {
  const S = s / 100, L = l / 100;
  const c = (1 - Math.abs(2 * L - 1)) * S;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = L - c / 2;
  const seg = [[c, x, 0], [x, c, 0], [0, c, x], [0, x, c], [x, 0, c], [c, 0, x]][Math.floor(h / 60) % 6];
  return "#" + seg.map(v => Math.round((v + m) * 255).toString(16).padStart(2, "0")).join("");
}
// 항목 수가 팔레트(7색)를 넘어도 **전 항목에 서로 다른 색**을 준다(사용자 요청 2026-08-06 —
// 구 "팔레트 밖은 공통 회색" 폐지). 팔레트를 먼저 쓰고, 그 뒤는 황금각(137.5°) 색상환
// 회전 + 명도 3단으로 인접 항목끼리 색이 붙지 않게 만든다. Pass 초록 대역은 피한다
// (맵에서 Pass die 와 같은 색으로 보이면 안 된다).
function tempItemColorAt(i) {
  if (i < FAIL_PALETTE.length) return FAIL_PALETTE[i];
  const n = i - FAIL_PALETTE.length;
  // 색상환 360° 를 Pass 초록 대역(95~155°)만 뺀 300° 안으로 **단조 사상**한 뒤 밀어 넣는다.
  // (대역에 걸린 값만 +60 하는 방식은 서로 다른 index 가 같은 색이 될 수 있어 쓰지 않는다.)
  let hue = ((n * 137.508) % 360) * (300 / 360);
  if (hue >= 95) hue += 60;
  return hslHex(hue, 62, 38 + (n % 3) * 9);
}
function buildTempItemInfo() {
  const rows = (webReportSheets() || {})[ISSUE_TEMP_SHEET] || [];
  const items = [], meta = {}, cnt = {};
  rows.forEach(r => {
    const it = String((r && r.Item) || "").trim();
    if (!it || meta[it]) return;
    items.push(it);
    meta[it] = { tno: String(r.TNO ?? ""), bin: String(r.Bin ?? "") };
    // fail die 수는 temp_map 인덱스 길이 합(표의 %가 아니라 실제 die 수).
    cnt[it] = 0;
  });
  Object.keys(tempMapBySource).forEach(src => {
    ((tempMapBySource[src] || {}).items || []).forEach(e => {
      if (cnt[e.item] !== undefined) cnt[e.item] += (e.idx || []).length;
    });
  });
  // legend 행은 **전 항목을 다 보여주고 전 항목에 서로 다른 색**을 준다 — 아래 항목도
  // 클릭해 강조할 수 있어야 한다 (사용자 요청 2026-08-06, 구 "기타 N항목" 접기·공통 회색 폐지).
  const colorMap = {};
  items.forEach((it, i) => { colorMap[it] = tempItemColorAt(i); });
  return { items, meta, cnt, colorMap };
}

// Detail(맵 1장) 용 범례 — 항목 순서·색은 전역 목록 그대로 두고(갤러리와 같은 항목=같은 색),
// **그 소스에서 실제로 fail 난 항목만** 남기고 die 수도 그 소스 것으로 바꾼다
// (갤러리 범례 = 전 소스 합산, Detail 범례 = 그 소스 — Bin Legend 와 같은 규약).
function tempItemInfoForSource(source) {
  const info = buildTempItemInfo();
  const cnt = {};
  (((tempMapBySource[source] || {}).items) || []).forEach(e => { cnt[e.item] = (e.idx || []).length; });
  return { items: info.items.filter(it => cnt[it]), meta: info.meta, cnt, colorMap: info.colorMap };
}

function tempItemLegendHtml(info, selected) {
  if (!info.items.length) {
    return `<div class="placeholder" style="padding:12px 4px">RT Limit 이탈 항목 없음</div>`;
  }
  const body = info.items.map(it => {
    const sel = selected.has(it);
    const sw = (selected.size === 0 || sel) ? info.colorMap[it] : MAP_BIN_DIM_COLOR;
    const m = info.meta[it] || {};
    return `<tr${sel ? ` class="is-selected"` : ""} data-temp-item="${esc(it)}">` +
      `<td class="temp-leg-item" title="${esc(it)}"><span class="bin-swatch" style="background:${sw}"></span>${esc(it)}</td>` +
      `<td>${esc(m.tno || "")}</td><td>${esc(m.bin || "")}</td>` +
      `<td>${info.cnt[it] || 0}</td></tr>`;
  }).join("");
  return `<table class="bin-table temp-legend-table"><thead><tr><th>Item</th><th>TNO</th><th>Bin</th><th>die</th>` +
         `</tr></thead><tbody>${body}</tbody></table>`;
}

// 선택 좌표 마커를 canvas 위 CSS 절대위치 원으로 오버레이(canvas 는 hover 없음).
// 위치는 썸네일과 동일한 압축 격자 기준(빈 행/열 제거 반영).
function renderThumbMarkers(wrap, m) {
  wrap.querySelectorAll(".wafer-sel-marker").forEach(e => e.remove());
  const g = waferCompactGrid(m);
  if (!(g.W > 0) || !(g.H > 0)) return;
  mapSelChips.forEach(c => {
    if (c.source !== m.source || c.x == null || c.y == null) return;
    const cx = g.xIdx[c.x], cy = g.yIdx[c.y];
    if (cx == null || cy == null) return;
    const mk = document.createElement("div");
    mk.className = "wafer-sel-marker";
    mk.style.left = ((cx + 0.5) / g.W * 100) + "%";
    mk.style.top = ((cy + 0.5) / g.H * 100) + "%";
    mk.style.borderColor = c.color;
    wrap.appendChild(mk);
  });
}
// 두 서브모드 공통 세그먼트(패널 최상단). renderStdfMap 도 같은 마크업을 쓴다.
function mapModeSegHtml() {
  const seg = (m, label) => `<button class="distseg${mapMode === m ? " active" : ""}" data-mapmode="${m}">${label}</button>`;
  return `<div class="map-mode-seg distseg-group">${seg("bin", "Bin Map")}${seg("stdf", "STDF Map")}</div>`;
}
function bindMapModeSeg(panel) {
  panel.querySelectorAll("[data-mapmode]").forEach(b => b.addEventListener("click", () => {
    const m = b.dataset.mapmode;
    if (m !== mapMode) { mapMode = m; renderMapAnalysis(); }
  }));
}

// Map Analysis 탭으로 전환 — 이미 그려져 있어도 새 선택 상태를 반영하도록 dirty 로 되돌린다.
function gotoMapAnalysisTab() {
  const btn = document.querySelector('.tab[data-tab="map-analysis"]');
  if (!btn) return;
  tabDirty["map-analysis"] = true;
  btn.click();   // 탭 리스너가 패널 전환 + renderTab 까지 처리
}
// Issue Table Yield/ETC 행 Map 미니셀 클릭 → Bin Map 으로 이동(그 Bin 을 범례 선택 상태로).
function openMapAnalysisForBin(bin) {
  mapMode = "bin";
  // Temperature 모드에서 축이 Temperature Map(CT/HT 만) 로 남아 있으면 RT bin 을 강조할
  // 맵 자체가 화면에 없다 — Bin 축으로 되돌린다.
  if (mapColorKey === "temp") mapColorKey = "bin";
  mapBinPreselect = (bin === null || bin === undefined || bin === "") ? null : String(bin);
  gotoMapAnalysisTab();
}
// Issue Table CPK 행 Map 미니셀(STDF) 클릭 → STDF Map 으로 이동(그 Item 을 선택 상태로).
function openMapAnalysisForItem(subject) {
  if (!subject) return;
  mapMode = "stdf";
  stdfItem = subject;
  stdfBucketFilter.clear();
  gotoMapAnalysisTab();
}
// Issue Table Temp 행 Map 미니셀 클릭 → Bin Map 의 "Temperature Map" 축에서 그 항목 강조.
let mapTempItemPreselect = null;
function openMapAnalysisForTempItem(item) {
  if (!item) return;
  mapMode = "bin";
  mapColorKey = "temp";
  mapTempItemPreselect = String(item);
  ensureTempMapData();
  gotoMapAnalysisTab();
}

function renderMapAnalysis() {
  const panel = document.getElementById("panel-map-analysis");
  if (mapMode === "stdf") { renderStdfMap(panel); return; }
  // Issue Table Map 셀에서 넘어온 초기 선택 Bin (1회성) — 아래 어느 경로로 빠지든 소비한다.
  const preselectBin = mapBinPreselect;
  mapBinPreselect = null;
  const sheets = webReportSheets();
  const allMaps = sheets ? sheets["Map Analysis"] : null;
  if (!window.Plotly || !allMaps || !allMaps.length) {
    emptyPanel(panel, "Map Analysis 데이터 없음"); return;
  }
  // Temperature 모드는 축에 따라 보여줄 소스가 갈린다(RT ↔ CT/HT). 결과가 비어도
  // emptyPanel 로 빠지지 않는다 — 툴바가 사라지면 축을 되돌릴 수 없기 때문.
  const maps = mapVisibleMaps();
  panel.classList.add("viz-root");
  // source 가 많은 세션의 기본 칸수(사용자가 직접 바꾸기 전까지만).
  if (!mapGridColsUserSet && maps.length >= MAP_GRID_WIDE_SOURCES) mapGridCols = 4;

  const legendRows = buildGlobalBinLegend(maps);
  const binOrder = legendRows.map(r => r.bin);
  const colorMap = globalBinColorMap();   // 세션 전체 공통 색상 (Summary/Fail Bin 과 일치)
  const mapBinFilter = new Set();   // 범례 클릭으로 선택된 bin 다중선택(재클릭 시 해제, 없으면 전체 표시)
  if (preselectBin != null) mapBinFilter.add(preselectBin);   // Issue Table 에서 넘어온 Bin 강조
  const mapTnoFilter = new Set();   // TNO 축 범례 클릭 필터
  // Temperature Map 축(Temperature 전용) — CT/HT 를 RT Limit 이탈 **항목**으로 색칠한다.
  const isTempMode = webReportMode() === "Temperature";
  if (!isTempMode && mapColorKey === "temp") mapColorKey = "bin";
  const mapTempFilter = new Set();
  if (mapTempItemPreselect) { mapTempFilter.add(mapTempItemPreselect); mapTempItemPreselect = null; }
  const tempInfo = isTempMode ? buildTempItemInfo() : { items: [], meta: {}, cnt: {}, colorMap: {} };
  if (isTempMode && mapColorKey === "temp") ensureTempMapData();
  const binDesc = buildBinDescMap();   // bin → 대표 fail item(Bin Legend description)
  const tnoInfo = buildTnoInfo();      // {colorMap, items, top, cnt, otherCount} (TNO 축·Legend)
  // DUT 모드: 병합 맵은 die 마다 dut 태그가 있고 row.duts 에 DUT 목록이 온다.
  const isDutMode = webReportMode() === "DUT";
  const dutList = (isDutMode && maps[0] && maps[0].duts) || [];
  let mapDutSelected = null;   // 강조 선택된 DUT (null = 전체 원색)

  // 선택 좌표 색 Legend (Map Analysis 전용). 각 항목: 색 스와치 + 좌표 + 제거(×).
  const selLegend = mapSelChips.length
    ? `<div class="mapsel-legend"><span class="mapsel-leg-title">선택 좌표</span>` +
      mapSelChips.map(c =>
        `<span class="mapsel-leg-item"><span class="mapsel-sw" style="background:${c.color}"></span>` +
        `X ${esc(c.xpos)}·Y ${esc(c.ypos)} <span class="mapsel-src">${esc(c.source)}</span>` +
        `<button type="button" class="mapsel-del" data-key="${esc(c.key)}" title="제거">×</button></span>`
      ).join("") +
      `<button type="button" id="mapSelClearBtn" class="btn-sm mapsel-clear">전체 해제</button></div>`
    : "";
  panel.innerHTML =
    mapModeSegHtml() +
    `<div class="map-toolbar">가로 칸수 ` +
    `<input type="number" id="mapGridColsInput" min="1" max="8" step="1" value="${mapGridCols}">` +
    `<span class="map-toolbar-hint">칸 (1 = 확대해서 보기 · 2~4 = 한꺼번에 보기)</span>` +
    `<span class="mapsel-sep"></span>` +
    `<span class="map-axis-seg distseg-group" title="색 기준 축">` +
      `<button type="button" class="distseg${mapColorKey === "bin" ? " active" : ""}" data-axis="bin">Bin</button>` +
      `<button type="button" class="distseg${mapColorKey === "tno" ? " active" : ""}" data-axis="tno">TNO</button>` +
      (isTempMode
        ? `<button type="button" class="distseg${mapColorKey === "temp" ? " active" : ""}" data-axis="temp" title="CT / HT 소스만 — RT Limit 이탈 항목으로 색칠 (범례 클릭 = 그 항목 fail die 강조)">Temperature Map</button>`
        : "") +
    `</span>` +
    `<span class="mapsel-sep"></span>` +
    `<button type="button" id="mapSelBtn" class="btn-sm">좌표 선택</button>` +
    `<span id="mapRenderProg" class="muted map-render-prog"></span>` +
    `</div>` +
    `<div id="mapSelSearchBox" class="mapsel-search" style="display:none" data-no-dirty>` +
      `<div class="common-search">` +
        `<input id="mapSelSerial" type="text" class="mapsel-field" placeholder="SERIAL (부분일치)" />` +
        `<input id="mapSelXpos" type="text" class="mapsel-field" placeholder="XPOS (정확)" />` +
        `<input id="mapSelYpos" type="text" class="mapsel-field" placeholder="YPOS (정확)" />` +
        `<button id="mapSelSearchBtn" class="btn-sm">검색</button>` +
        `<button id="mapSelAddSelected" class="btn-sm primary" disabled>선택 추가</button>` +
        `<button id="mapSelCollapseBtn" class="btn-sm" title="검색 패널 접기">접기 ▲</button>` +
        `<span id="mapSelInfo" class="muted"></span>` +
      `</div>` +
      `<div class="common-list mapsel-list" id="mapSelList"><div class="placeholder">SERIAL(부분) / XPOS·YPOS(정확) 칸에 입력해 검색하고, 체크한 좌표를 '선택 추가' 로 한 번에 추가하세요 (여러 개 가능).</div></div>` +
    `</div>` +
    selLegend +
    `<div class="wafer-analysis-layout">` +
    `<div class="wafer-grid" style="grid-template-columns:repeat(${mapGridCols}, minmax(0, 1fr))">` +
    (maps.length
      ? maps.map((m, i) =>
        `<div class="wafer-card wafer-card-clickable" data-map-index="${i}" title="클릭하면 크게(확대·마우스오버) 봅니다">
        <div class="wafer-card-title">${esc(m.source)}${m.step ? " — " + esc(m.step) : ""} — ${esc(String(m.total))} dies<span class="wafer-card-zoom">⤢ 크게 보기</span></div>
        <div id="wafer-full-${i}" class="wafer-thumb-wrap" style="aspect-ratio:1 / 1"><div class="placeholder">맵 로드 중…</div></div>
      </div>`).join("")
      : `<div class="placeholder">${mapColorKey === "temp"
          ? "CT / HT 소스 맵이 없습니다" : "표시할 맵이 없습니다"}</div>`) +
    `</div>` +
    // Temperature Map Legend 는 Item/TNO/Bin/die 4열이라 기본 폭(340px)에서는 Bin·die 가 잘린다 → 넓게.
    `<div class="wafer-legend-fixed${mapColorKey === "temp" ? " legend-wide" : ""}">` +
    // 색 기준 축(Bin/TNO)에 맞는 Legend 하나만 표시 — 축 전환 시 renderMapAnalysis 재호출로 교체.
    (mapColorKey === "temp"
      ? `<div class="wafer-legend-title">Temperature Map Legend</div>` +
        `<div class="dut-legend-hint">RT Limit 이탈 항목 — 클릭 시 그 항목 fail die 만 강조</div>` +
        `<div class="temp-legend-body"></div>`
      : mapColorKey === "tno"
        ? `<div class="wafer-legend-title">TNO Legend</div><div class="tno-legend-body"></div>`
        : `<div class="wafer-legend-title">Bin Legend</div><div class="wafer-legend-body"></div>`) +
    (dutList.length
      ? `<div class="wafer-legend-title dut-legend-title">DUT Legend</div>` +
        `<div class="dut-legend-hint">클릭 시 해당 DUT 강조 (나머지 연하게)</div>` +
        `<div class="dut-legend-body"></div>`
      : "") +
    `</div>` +
    `</div>`;

  bindMapModeSeg(panel);
  panel.querySelector("#mapGridColsInput").addEventListener("change", (e) => {
    const v = parseInt(e.target.value, 10);
    mapGridCols = isNaN(v) ? 2 : Math.min(8, Math.max(1, v));
    mapGridColsUserSet = true;   // 이후로는 source 수 기반 기본값을 덮어쓰지 않는다
    renderMapAnalysis();   // 칸수 변경 → 그리드·플롯 높이 다시 그림(범례 선택은 초기화됨)
  });

  // 좌표 선택 툴바 — 검색 패널 토글 + 검색 + 해제.
  panel.querySelector("#mapSelBtn").addEventListener("click", mapSelToggleSearch);
  const _mapSelClearBtn = panel.querySelector("#mapSelClearBtn");
  if (_mapSelClearBtn) _mapSelClearBtn.addEventListener("click", mapSelClear);
  const _doMapSelSearch = () => mapSelSearch();
  panel.querySelector("#mapSelSearchBtn").addEventListener("click", _doMapSelSearch);
  panel.querySelector("#mapSelAddSelected").addEventListener("click", mapSelAddSelected);
  panel.querySelector("#mapSelCollapseBtn").addEventListener("click", () => {
    const box = document.getElementById("mapSelSearchBox");
    if (box) box.style.display = "none";   // 검색 패널 접기(명시적 닫기).
  });
  ["mapSelSerial", "mapSelXpos", "mapSelYpos"].forEach(id => {
    const el = panel.querySelector("#" + id);
    if (el) el.addEventListener("keydown", e => { if (e.key === "Enter") _doMapSelSearch(); });
  });
  panel.querySelectorAll(".mapsel-del").forEach(b => b.addEventListener("click", () => mapSelRemove(b.dataset.key)));

  // 카드 클릭 → Map Detail 전체화면(확대·마우스오버). 갤러리는 개요(빠른 canvas 썸네일).
  panel.querySelector(".wafer-grid").addEventListener("click", (e) => {
    const card = e.target.closest(".wafer-card-clickable");
    if (!card) return;
    const idx = parseInt(card.dataset.mapIndex, 10);
    if (!isNaN(idx)) openMapDetail(idx);
  });
  // Bin / TNO 색 기준 축 전환.
  panel.querySelectorAll("[data-axis]").forEach(b => b.addEventListener("click", () => {
    const ax = b.dataset.axis;
    if (ax !== mapColorKey) { mapColorKey = ax; renderMapAnalysis(); }
  }));

  // 갤러리는 canvas 라 색 필터 변경 시 canvas 를 다시 그린다(맵당 <5ms, rAF 스텝퍼).
  function restyleColors() {
    drawAllMaps();
    renderLegendBody();
    renderTnoLegend();
    renderTempLegend();
  }
  function renderTempLegend() {
    const host = panel.querySelector(".temp-legend-body");
    if (!host) return;
    host.innerHTML = tempItemLegendHtml(tempInfo, mapTempFilter);
    host.querySelectorAll("tbody tr[data-temp-item]").forEach(tr => {
      tr.addEventListener("click", () => {
        const it = tr.dataset.tempItem;
        if (mapTempFilter.has(it)) mapTempFilter.delete(it); else mapTempFilter.add(it);
        restyleColors();
      });
    });
  }
  function renderLegendBody() {
    const legendBody = panel.querySelector(".wafer-legend-body");
    if (!legendBody) return;
    legendBody.innerHTML = binLegendHtml(legendRows, colorMap, mapBinFilter, binDesc);
    legendBody.querySelectorAll("tbody tr[data-bin]").forEach(tr => {
      tr.addEventListener("click", () => {
        const bin = tr.dataset.bin;
        if (mapBinFilter.has(bin)) mapBinFilter.delete(bin); else mapBinFilter.add(bin);
        restyleColors();
      });
    });
  }
  function renderTnoLegend() {
    const host = panel.querySelector(".tno-legend-body");
    if (!host) return;
    host.innerHTML = tnoLegendHtml(tnoInfo, mapTnoFilter);
    host.querySelectorAll("tbody tr[data-tno]").forEach(tr => {
      tr.addEventListener("click", () => {
        const it = tr.dataset.tno;
        if (mapTnoFilter.has(it)) mapTnoFilter.delete(it); else mapTnoFilter.add(it);
        restyleColors();
      });
    });
  }
  function renderDutLegend() {
    const host = panel.querySelector(".dut-legend-body");
    if (!host) return;
    host.innerHTML = dutLegendHtml(dutList, mapDutSelected);
    host.querySelectorAll("tbody tr[data-dut]").forEach(tr => {
      tr.addEventListener("click", () => {
        const d = tr.dataset.dut;
        mapDutSelected = (mapDutSelected === d) ? null : d;   // 재클릭 시 해제(전체 원색)
        renderDutLegend();
        drawAllMaps();
      });
    });
  }
  // 활성 TNO color map(mapTnoFilter dim 반영). Bin dim 은 drawAllMaps 가 activeBinColorMap 로 넘긴다.
  function tnoActiveColorMap() {
    if (!mapTnoFilter.size) return tnoInfo.colorMap;
    const out = {};
    tnoInfo.items.forEach(it => { out[it] = mapTnoFilter.has(it) ? tnoInfo.colorMap[it] : MAP_BIN_DIM_COLOR; });
    return out;
  }
  // Temperature Map 축의 die→항목 매핑은 **소스 단위**다. STEP 분리 세션은 같은 소스 맵이
  // STEP 수만큼 있어(dies 길이 동일) 맵마다 재계산하면 소스당 STEP 배로 낭비된다.
  // drawAllMaps 1회 = 필터 1상태이므로 그 사이만 재사용한다.
  let _tempPrimaryCache = null;
  function tempPrimaryFor(source) {
    if (!_tempPrimaryCache) _tempPrimaryCache = new Map();
    if (!_tempPrimaryCache.has(source)) {
      _tempPrimaryCache.set(source, tempPrimaryByIdx(source, tempInfo.items, mapTempFilter));
    }
    return _tempPrimaryCache.get(source);
  }
  function drawMap(i, activeBinColorMap) {
    const m = maps[i];
    const wrap = document.getElementById(`wafer-full-${i}`);
    if (!wrap) return;
    // dies 지연 로드 중 — placeholder("맵 로드 중…") 유지, 도착 시 refreshMapConsumers 가 재드로우.
    if (!Array.isArray(m.dies)) { ensureMapData(); return; }
    let canvas = wrap.querySelector("canvas.wafer-thumb");
    if (!canvas) {
      wrap.innerHTML = "";
      canvas = document.createElement("canvas");
      canvas.className = "wafer-thumb";
      wrap.appendChild(canvas);
    }
    const sel = (mapDutSelected && (m.duts || []).includes(mapDutSelected)) ? mapDutSelected : null;
    const activeTno = tnoActiveColorMap();
    // Temperature Map 축: 이 소스의 die 인덱스 → 항목명 (dies 배열과 같은 순서, 서버 temp_map).
    // temp_map 이 아직 안 왔거나 그 소스가 RT(=대상 아님)면 null → 전부 Pass 색으로 둔다.
    const tempPrimary = (mapColorKey === "temp") ? tempPrimaryFor(m.source) : null;
    if (mapColorKey === "temp" && !tempMapReady) ensureTempMapData();
    function rgbFor(d, cache, k) {
      if (d.g) return MAP_GRAY_RGB;   // 앞 step 에서 이미 fail — 회색(모양만 유지)
      let hex;
      if (mapColorKey === "temp") {
        const it = tempPrimary ? tempPrimary[k] : null;
        hex = it ? (tempInfo.colorMap[it] || TNO_OTHER_COLOR) : PASS_COLOR;
      } else if (mapColorKey === "tno") {
        hex = (d.bin === "1" || d.it == null) ? PASS_COLOR : (activeTno[d.it] || TNO_OTHER_COLOR);
      } else {
        hex = activeBinColorMap[d.bin] || PASS_COLOR;
      }
      const faded = sel && d.dut !== sel;   // DUT 미선택 die = 흐리게
      const ckey = hex + (faded ? "F" : "");
      let rgb = cache[ckey];
      if (!rgb) { rgb = faded ? fadeRgb(hexToRgb(hex), 0.28) : hexToRgb(hex); cache[ckey] = rgb; }
      return rgb;
    }
    drawWaferThumb(canvas, m, rgbFor);
    renderThumbMarkers(wrap, m);
  }
  // 맵을 한 프레임에 한 장씩 그려 UI 스레드를 쪼갠다(대량 die freeze 방지) + 진행률 표시.
  function drawAllMaps() {
    const token = ++_mapDrawToken;
    _tempPrimaryCache = null;   // 필터가 바뀌었을 수 있다 — 소스별 매핑 재계산
    const activeBinColorMap = dimColorMap(colorMap, binOrder, mapBinFilter);
    const prog = panel.querySelector("#mapRenderProg");
    let i = 0;
    function step() {
      if (token !== _mapDrawToken) return;   // 칸수 변경·chip 추가·재렌더가 시작되면 이전 체인 중단
      if (i >= maps.length) { if (prog) prog.textContent = ""; return; }
      if (prog && maps.length > 1) prog.textContent = `맵 ${i + 1} / ${maps.length} 그리는 중…`;
      drawMap(i, activeBinColorMap);
      i++;
      requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  renderLegendBody();
  renderTnoLegend();
  renderTempLegend();
  renderDutLegend();
  _mapOnDiesReady = drawAllMaps;   // dies 지연 도착 시 갤러리만 재드로우(범례 필터 상태 유지)
  drawAllMaps();
}

// ── Map Detail (카드 클릭 → 전체화면 1장: 확대·해상도·격자선·마우스오버) ───────────────
// item_detail 패턴 복제 — sticky-head 는 그대로 두고 #panel-map-detail 만 활성화, Back 복귀.
let _mapDetailReturnId = null;   // 복귀할 탭 패널 id
let _mapDetailIndex = 0;         // 현재 보는 맵 인덱스
let _mapDetailBound = false;
let _mapDetailBinFilter = new Set();

// Detail 은 갤러리와 **같은 목록**을 본다(mapVisibleMaps) — 카드 클릭이 넘기는 인덱스가
// 그대로 통하고, Temperature 모드 축 필터(RT ↔ CT/HT)도 자동으로 따라온다.
function mapDetailMaps() {
  return mapVisibleMaps();
}

// Detail 플롯 높이 — 뷰포트에서 sticky/헤더 여백을 뺀 큰 값(해상도 우선), 420~900 clamp.
function mapDetailPlotHeight() {
  return Math.max(420, Math.min(900, window.innerHeight - 200));
}

function purgeMapDetailChart() {
  const el = document.getElementById("map-detail-plot");
  if (el && window.Plotly) { try { Plotly.purge(el); } catch (e) {} }
}

function openMapDetail(i) {
  const dp = document.getElementById("panel-map-detail");
  if (!dp) return;
  bindMapDetailPanel();
  if (!dp.classList.contains("active")) {
    const cur = document.querySelector(".content > .panel.active");
    _mapDetailReturnId = cur ? cur.id : "panel-map-analysis";
    if (cur) cur.classList.remove("active");
    dp.classList.add("active");
  }
  _mapDetailIndex = i;
  window.scrollTo(0, 0);
  renderMapDetail();
}

function closeMapDetail() {
  const dp = document.getElementById("panel-map-detail");
  if (!dp) return;
  purgeMapDetailChart();
  dp.classList.remove("active");
  dp.innerHTML = "";
  const back = document.getElementById(_mapDetailReturnId || "panel-map-analysis");
  if (back) {
    back.classList.add("active");
    // 갤러리 플롯은 Detail 동안 숨겨져(0폭) 있었으므로 복귀 시 현재 폭으로 리사이즈.
    if (window.Plotly) back.querySelectorAll(".js-plotly-plot").forEach(d => { try { Plotly.Plots.resize(d); } catch (e) {} });
  }
  _mapDetailReturnId = null;
}

// 탭 버튼 클릭 시: 복원 없이 상세만 닫는다(해당 탭 패널이 이어서 활성화됨).
function hideMapDetail() {
  const dp = document.getElementById("panel-map-detail");
  if (dp && dp.classList.contains("active")) { purgeMapDetailChart(); dp.classList.remove("active"); dp.innerHTML = ""; }
  _mapDetailReturnId = null;
}

function mapDetailNav(delta) {
  const maps = mapDetailMaps();
  if (!maps.length) return;
  let idx = _mapDetailIndex + delta;
  if (idx < 0) idx = 0;
  if (idx >= maps.length) idx = maps.length - 1;
  if (idx === _mapDetailIndex) return;
  _mapDetailIndex = idx;
  renderMapDetail();
}

function bindMapDetailPanel() {
  if (_mapDetailBound) return;
  const dp = document.getElementById("panel-map-detail");
  if (!dp) return;
  dp.addEventListener("click", e => {
    if (e.target.closest(".idet-back")) { closeMapDetail(); return; }
    if (e.target.closest(".mapd-prev")) { mapDetailNav(-1); return; }
    if (e.target.closest(".mapd-next")) { mapDetailNav(1); return; }
    const tr = e.target.closest("tbody tr[data-bin], tbody tr[data-tno], tbody tr[data-temp-item]");
    if (tr) { mapDetailToggle(tr.dataset.bin || tr.dataset.tno || tr.dataset.tempItem); return; }
  });
  document.addEventListener("keydown", e => {
    if (!dp.classList.contains("active")) return;
    if (e.key === "Escape") { closeMapDetail(); return; }
    if (e.altKey && e.key === "ArrowLeft") { e.preventDefault(); mapDetailNav(-1); }
    else if (e.altKey && e.key === "ArrowRight") { e.preventDefault(); mapDetailNav(1); }
  });
  _mapDetailBound = true;
}

// 현재 mapColorKey 축의 catOf/order/colorMap (회색·Pass 포함, _mapDetailBinFilter dim 반영).
const _MAP_GRAY_CAT = "__gray__", _MAP_OTHER_CAT = "__other__";
function mapDetailAxis() {
  if (mapColorKey === "temp" && webReportMode() === "Temperature") {
    // 갤러리와 같은 규칙: 이 소스의 die 인덱스 → RT Limit 이탈 항목(temp_map), 나머지는 Pass 색.
    // 범례 선택(_mapDetailBinFilter)은 색이 아니라 **칠할 항목 집합**을 좁힌다(tempPrimaryByIdx).
    const info = buildTempItemInfo();
    const m = mapDetailMaps()[_mapDetailIndex];
    const primary = m ? tempPrimaryByIdx(m.source, info.items, _mapDetailBinFilter) : null;
    const order = ["1"].concat(info.items, [_MAP_GRAY_CAT]);
    const colorMap = { "1": PASS_COLOR, [_MAP_GRAY_CAT]: MAP_GRAY_HEX };
    info.items.forEach(it => { colorMap[it] = info.colorMap[it]; });
    const catOf = (d, k) => d.g ? _MAP_GRAY_CAT : ((primary && primary[k]) || "1");
    return { catOf, order, colorMap };
  }
  if (mapColorKey === "tno") {
    const tno = buildTnoInfo();
    const topSet = {}; tno.top.forEach(it => { topSet[it] = 1; });
    const order = ["1"].concat(tno.top, [_MAP_OTHER_CAT, _MAP_GRAY_CAT]);
    const base = { "1": PASS_COLOR, [_MAP_OTHER_CAT]: TNO_OTHER_COLOR, [_MAP_GRAY_CAT]: MAP_GRAY_HEX };
    tno.top.forEach(it => { base[it] = tno.colorMap[it]; });
    let colorMap = base;
    if (_mapDetailBinFilter.size) {   // 선택 항목만 원색, 나머지 dim(Pass·회색·기타 제외)
      colorMap = {};
      order.forEach(c => {
        colorMap[c] = (tno.top.indexOf(c) >= 0 && !_mapDetailBinFilter.has(c)) ? MAP_BIN_DIM_COLOR : base[c];
      });
    }
    const catOf = d => d.g ? _MAP_GRAY_CAT : (d.bin === "1" || d.it == null ? "1" : (topSet[d.it] ? d.it : _MAP_OTHER_CAT));
    return { catOf, order, colorMap };
  }
  const legendRows = buildGlobalBinLegend(mapDetailMaps());
  const binOrder = legendRows.map(r => r.bin);
  const dimmed = dimColorMap(globalBinColorMap(), binOrder, _mapDetailBinFilter);
  const order = binOrder.concat([_MAP_GRAY_CAT]);
  const colorMap = Object.assign({}, dimmed, { [_MAP_GRAY_CAT]: MAP_GRAY_HEX });
  const catOf = d => d.g ? _MAP_GRAY_CAT : d.bin;
  return { catOf, order, colorMap };
}

// heatmap trace + 선택 좌표 오버레이. opts 로 forceGap 을 waferHeatmap 에 전달. 축은 mapColorKey.
function mapDetailTraces(m, opts) {
  const axis = mapDetailAxis();
  const g = waferCompactGrid(m);   // 상세는 compact 격자로 그린다(메모리 span 무관 — OOM 방지).
  const built = waferHeatmap(m, Object.assign(
    { catOf: axis.catOf, order: axis.order, colorMap: axis.colorMap, grid: g }, opts || {}));
  if (!built) return null;
  const traces = [built.trace];
  mapSelChips.forEach(c => {
    if (c.source !== m.source || c.x == null || c.y == null) return;
    const cx = g.xIdx[c.x], cy = g.yIdx[c.y];   // heatmap 이 index 공간이므로 마커도 index 위치
    if (cx == null || cy == null) return;
    traces.push({ type: "scatter", mode: "markers", x: [cx], y: [cy],
      marker: { symbol: "circle-open", size: 22, color: c.color, line: { width: 3, color: c.color } },
      hovertemplate: `X ${c.x} · Y ${c.y}<extra></extra>` });
  });
  return traces;
}

const MAP_DETAIL_CONFIG = {
  responsive: true, scrollZoom: true, displayModeBar: true, displaylogo: false,
  modeBarButtonsToRemove: ["select2d", "lasso2d", "toImage"],
};

function drawMapDetail(m, opts) {
  const traces = mapDetailTraces(m, opts);
  if (!traces) return;
  Plotly.newPlot("map-detail-plot", traces, waferLayout(m, { grid: waferCompactGrid(m) }), MAP_DETAIL_CONFIG);
}

// 확대 시 보이는 die 가 임계 이하로 줄면 격자선을 복원하고, 리셋하면 이미지 모드로.
// forced 가드로 상태가 안 바뀌면 재렌더를 생략해 relayout 무한루프를 막는다.
function bindMapDetailZoom(el, m) {
  if (!el || !el.on) return;
  const g = waferCompactGrid(m);   // 축이 compact index 공간 → die 좌표를 index 로 환산해 비교.
  let forced = false;
  let timer = null;         // 스크롤 줌은 relayout 이 연속 발화 — trailing 디바운스로 마지막 상태만 계산
  let lastRangeKey = "";    // 범위가 안 바뀐 relayout(팬 종료·legend 등)은 die 전량 스캔 생략
  el.on("plotly_relayout", () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      const xa = el.layout && el.layout.xaxis, ya = el.layout && el.layout.yaxis;
      const xr = xa && xa.range, yr = ya && ya.range;
      const isAuto = (xa && xa.autorange) || (ya && ya.autorange);
      const rangeKey = isAuto ? "auto" : String(xr) + "|" + String(yr);
      if (rangeKey === lastRangeKey) return;
      lastRangeKey = rangeKey;
      let visible = (m.dies || []).length;
      if (!isAuto && xr && yr) {
        const x0 = Math.min(xr[0], xr[1]), x1 = Math.max(xr[0], xr[1]);
        const y0 = Math.min(yr[0], yr[1]), y1 = Math.max(yr[0], yr[1]);
        visible = 0;
        const dies = m.dies || [];
        for (let k = 0; k < dies.length; k++) {
          const d = dies[k];
          const cx = g.xIdx[d.x], cy = g.yIdx[d.y];
          if (cx == null || cy == null) continue;
          if (cx >= x0 && cx <= x1 && cy >= y0 && cy <= y1) visible++;
        }
      }
      const wantForce = visible <= MAP_DENSE_DIES;
      if (wantForce === forced) return;
      forced = wantForce;
      const traces = mapDetailTraces(m, wantForce ? { forceGap: true } : null);
      if (traces) Plotly.react("map-detail-plot", traces, el.layout, MAP_DETAIL_CONFIG);
    }, 150);
  });
}

function renderMapDetailLegend() {
  const dp = document.getElementById("panel-map-detail");
  if (!dp) return;
  const legendBody = dp.querySelector(".wafer-legend-body");
  const title = dp.querySelector(".wafer-legend-title");
  if (!legendBody) return;
  if (mapColorKey === "temp" && webReportMode() === "Temperature") {
    const m = mapDetailMaps()[_mapDetailIndex];
    if (title) title.textContent = "Temperature Map Legend" + (m ? " — " + m.source : "");
    legendBody.innerHTML = tempItemLegendHtml(
      tempItemInfoForSource(m ? m.source : ""), _mapDetailBinFilter);
  } else if (mapColorKey === "tno") {
    if (title) title.textContent = "TNO Legend";
    legendBody.innerHTML = tnoLegendHtml(buildTnoInfo(), _mapDetailBinFilter);
  } else {
    // 갤러리 범례는 전 소스 합산(Summary)이지만, 크게 보기는 **지금 보고 있는 맵 1장**의
    // Bin 집계다 — count·비율이 화면의 웨이퍼와 일치해야 한다(사용자 요청 2026-08-06).
    // 색은 세션 공통(globalBinColorMap) 그대로라 갤러리와 같은 bin=같은 색이 유지된다.
    const m = mapDetailMaps()[_mapDetailIndex];
    if (title) title.textContent = "Bin Legend" + (m ? " — " + m.source + (m.step ? " / " + m.step : "") : "");
    legendBody.innerHTML = binLegendHtml(buildGlobalBinLegend(m ? [m] : []),
      globalBinColorMap(), _mapDetailBinFilter, buildBinDescMap());
  }
}

// 범례 클릭: 색만 restyle(확대/격자 상태 유지). bin/tno/temp 축 공용.
function mapDetailToggle(key) {
  const m = mapDetailMaps()[_mapDetailIndex];
  if (!m) return;
  if (_mapDetailBinFilter.has(key)) _mapDetailBinFilter.delete(key); else _mapDetailBinFilter.add(key);
  const el = document.getElementById("map-detail-plot");
  if (el && el.data) {
    if (mapColorKey === "temp") {
      // temp 축은 색이 아니라 **z(die→항목 매핑)** 가 선택으로 바뀐다 → colorscale restyle
      // 로는 반영되지 않는다. 같은 layout 으로 trace 만 다시 만든다.
      const traces = mapDetailTraces(m, null);
      if (traces) { try { Plotly.react(el, traces, el.layout, MAP_DETAIL_CONFIG); } catch (e) {} }
    } else {
      const axis = mapDetailAxis();
      try { Plotly.restyle(el, { colorscale: [binColorscale(axis.order, axis.colorMap)] }, [0]); } catch (e) {}
    }
  }
  renderMapDetailLegend();
}

function renderMapDetail() {
  const dp = document.getElementById("panel-map-detail");
  if (!dp) return;
  const maps = mapDetailMaps();
  const m = maps[_mapDetailIndex];
  if (!m || !window.Plotly) {
    dp.innerHTML = `<div class="idet"><div class="idet-head"><button class="btn-sm idet-back">← Back</button></div>` +
      `<div class="placeholder">맵을 표시할 수 없습니다</div></div>`;
    return;
  }
  purgeMapDetailChart();
  dp.classList.add("viz-root");
  _mapDetailBinFilter = new Set();   // 맵 진입 시 필터 초기화

  const total = maps.length;
  const navHtml = total > 1
    ? `<button class="btn-sm mapd-prev" title="이전 (Alt+←)">‹</button>` +
      `<button class="btn-sm mapd-next" title="다음 (Alt+→)">›</button>` +
      `<span class="idet-navpos">${_mapDetailIndex + 1} / ${total}</span>` : "";

  dp.innerHTML =
    `<div class="idet">` +
      `<div class="idet-head">` +
        `<button class="btn-sm idet-back">← Back</button>` +
        navHtml +
        `<span class="idet-title"><b>${esc(m.source)}${m.step ? " — " + esc(m.step) : ""}</b>` +
        ` — ${esc(String(m.total))} dies` +
        `<span class="mapd-hint">스크롤/드래그로 확대 · 마우스오버로 X·Y·값 · 더블클릭 리셋</span></span>` +
      `</div>` +
      `<div class="wafer-analysis-layout">` +
        `<div class="wafer-grid" style="grid-template-columns:repeat(1, minmax(0, 1fr))">` +
          `<div class="wafer-card">` +
            `<div id="map-detail-plot" style="width:100%;height:${mapDetailPlotHeight()}px;"><div class="placeholder">맵 로드 중…</div></div>` +
          `</div>` +
        `</div>` +
        // Temperature Map 축은 항목 범례가 Item/TNO/Bin/die 4열이라 갤러리와 같이 넓게 쓴다.
        `<div class="wafer-legend-fixed${mapColorKey === "temp" ? " legend-wide" : ""}">` +
          `<div class="wafer-legend-title">${mapColorKey === "temp" ? "Temperature Map Legend"
            : mapColorKey === "tno" ? "TNO Legend" : "Bin Legend"}</div>` +
          `<div class="wafer-legend-body"></div>` +
        `</div>` +
      `</div>` +
    `</div>`;

  renderMapDetailLegend();
  // dies(또는 temp 축의 temp_map) 지연 로드 중 — placeholder 만 표시하고,
  // 도착하면 refreshMapConsumers(ensureMapData/ensureTempMapData 완료 훅)가 재호출한다.
  const needTemp = (mapColorKey === "temp" && webReportMode() === "Temperature" && !tempMapReady);
  if (!Array.isArray(m.dies) || needTemp) {
    dp.dataset.mapWaiting = "1";
    const host = document.getElementById("map-detail-plot");
    if (host) host.innerHTML = `<div class="placeholder">die 데이터 로딩 중…</div>`;
    ensureMapData();
    if (needTemp) ensureTempMapData();
    return;
  }
  dp.dataset.mapWaiting = "";
  // 셸+placeholder 페인트 후 다음 프레임에 무거운 렌더(로딩 표시가 실제로 보이도록).
  requestAnimationFrame(() => {
    if (mapDetailMaps()[_mapDetailIndex] !== m) return;   // 그 사이 다른 맵으로 이동하면 취소
    drawMapDetail(m);
    bindMapDetailZoom(document.getElementById("map-detail-plot"), m);
  });
}


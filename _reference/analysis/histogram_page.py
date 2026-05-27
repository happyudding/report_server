"""Histogram (KDE) grid HTML page — 5 cols, SVG thumbs + hover→Plotly upgrade,
검색 기능 포함. Distribution(cumulative.html) 과 동일한 UX."""
from config import CELL_ASPECT_H, CELL_ASPECT_W, COLS_PER_ROW

HTML_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>Histogram (KDE) — __DATASET_ID__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" defer></script>
<style>
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #fafafa; color: #222; }
  .topbar { position: sticky; top: 0; z-index: 100; background: #fff; border-bottom: 1px solid #ddd; padding: 8px 16px; display: flex; gap: 10px; align-items: center; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .topbar h1 { font-size: 14px; margin: 0 12px 0 0; color: #333; font-weight: 600; }
  .topbar .dataset { font-size: 11px; color: #999; margin-right: 12px; }
  .topbar input#search-input { padding: 5px 10px; font-size: 13px; min-width: 220px; border: 1px solid #ccc; border-radius: 4px; outline: none; transition: border-color 0.15s, background 0.15s; }
  .topbar input#search-input:focus { border-color: #4a90e2; }
  .topbar input#search-input.no-match { border-color: #e57373; background: #fff5f5; }
  .topbar .active-label { color: #666; font-size: 12px; margin-left: auto; }
  .topbar .active-label strong { color: #333; }
  .content { padding: 14px 156px 14px 14px; }
  .grid { display: grid; grid-template-columns: repeat(__COLS__, 1fr); gap: 14px; }
  .cell { position: relative; aspect-ratio: __AW__ / __AH__; background: #fff; border: 1px solid #e0e0e0; border-radius: 6px; overflow: hidden; transition: border-color 0.1s, box-shadow 0.1s; }
  .cell::before { content: 'loading…'; position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #bbb; font-size: 11px; pointer-events: none; }
  .cell.loaded::before { display: none; }
  .cell .thumb { width: 100%; height: 100%; }
  .cell .thumb img { width: 100%; height: 100%; display: block; }
  .cell .plot { width: 100%; height: 100%; position: absolute; inset: 0; }
  .cell.active { border-color: #4a90e2; box-shadow: 0 0 0 2px rgba(74,144,226,0.25); }
  .cell.flash { animation: cell-flash 1.5s ease-out; }
  @keyframes cell-flash {
    0%   { box-shadow: 0 0 0 0 rgba(255,193,7,0); border-color: #e0e0e0; }
    15%  { box-shadow: 0 0 0 6px rgba(255,193,7,0.7); border-color: #ffc107; }
    100% { box-shadow: 0 0 0 0 rgba(255,193,7,0); }
  }
  .sidebar { position: fixed; right: 0; top: 48px; bottom: 0; width: 140px; background: #fff; border-left: 1px solid #ddd; padding: 12px 10px; overflow-y: auto; z-index: 80; box-sizing: border-box; }
  .sidebar-title { font-size: 11px; color: #666; margin: 0 0 8px 2px; font-weight: 600; }
  .sidebar-item { display: flex; align-items: center; gap: 8px; width: 100%; padding: 5px 8px; margin-bottom: 4px; border: 1px solid #e0e0e0; border-radius: 4px; background: #fff; font-size: 11px; text-align: left; color: #333; }
  .sidebar-item .swatch { width: 14px; height: 14px; border-radius: 2px; flex-shrink: 0; border: 1px solid rgba(0,0,0,0.1); }
  .cell .modebar-container, .cell .modebar { display: none !important; }
  .cell.active .modebar-container, .cell.active .modebar { display: flex !important; position: fixed !important; top: 6px !important; right: 156px !important; z-index: 200 !important; background: rgba(255,255,255,0.96) !important; padding: 2px 6px !important; border-radius: 4px !important; box-shadow: 0 1px 4px rgba(0,0,0,0.15) !important; opacity: 1 !important; }
</style>
</head>
<body>
<div class="topbar">
  <h1>Histogram KDE (n=__N__)</h1>
  <span class="dataset">dataset: __DATASET_ID__</span>
  <input id="search-input" type="text" placeholder="검색 (Enter: 해당 차트로 이동)" autocomplete="off">
  <span class="active-label">활성: <strong id="active-name">셀에 마우스를 올리세요</strong></span>
</div>
<div class="content"><div class="grid" id="grid">
__CELLS__
</div></div>
<aside class="sidebar">
  <div class="sidebar-title">학교 (legend)</div>
__SIDEBAR__
</aside>
<script>
const DATASET_ID = '__DATASET_ID__';
const THUMB_API = `/api/${DATASET_ID}/histogram_thumb`;
const CHART_API = `/api/${DATASET_ID}/histogram_chart`;
const cfg = { scrollZoom: true, displaylogo: false, displayModeBar: true, responsive: true, modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
let activeCell = null;

const activeNameEl = document.getElementById('active-name');
function updateActiveLabel() {
  activeNameEl.textContent = activeCell ? (activeCell.dataset.name || ('subject_' + activeCell.dataset.id)) : '셀에 마우스를 올리세요';
}

function waitForPlotly() {
  return new Promise((resolve) => {
    if (typeof Plotly !== 'undefined') return resolve();
    const w = setInterval(() => {
      if (typeof Plotly !== 'undefined') { clearInterval(w); resolve(); }
    }, 30);
  });
}

// Plotly newPlot serialised via rAF queue to avoid stalls
const _queue = [];
let _draining = false;
function enqueue(task) {
  _queue.push(task);
  if (!_draining) { _draining = true; requestAnimationFrame(_drain); }
}
async function _drain() {
  while (_queue.length) {
    try { await _queue.shift()(); } catch (e) { console.error(e); }
    await new Promise(r => requestAnimationFrame(r));
  }
  _draining = false;
}

// Cap active Plotly instances; older cells get reverted to thumb
const _activePlotly = [];
const MAX_ACTIVE_PLOTLY = 8;
const plotlyInflight = new Map();
const plotlyDestroyTimers = new Map();

function trackActivePlotly(cell) {
  const i = _activePlotly.indexOf(cell);
  if (i >= 0) _activePlotly.splice(i, 1);
  _activePlotly.push(cell);
  while (_activePlotly.length > MAX_ACTIVE_PLOTLY) {
    const oldest = _activePlotly.shift();
    if (oldest !== cell && oldest.dataset.plotlyLoaded === '1') destroyPlotly(oldest);
  }
}

function destroyPlotly(cell) {
  const div = cell.querySelector('.plot');
  if (div) { try { Plotly.purge(div); } catch (_) {} div.remove(); }
  cell.dataset.plotlyLoaded = '';
  const i = _activePlotly.indexOf(cell);
  if (i >= 0) _activePlotly.splice(i, 1);
  if (cell === activeCell) { cell.classList.remove('active'); activeCell = null; updateActiveLabel(); }
}

function cancelDestroy(cell) {
  const t = plotlyDestroyTimers.get(cell);
  if (!t) return;
  clearTimeout(t);
  plotlyDestroyTimers.delete(cell);
}
function scheduleDestroy(cell) {
  if (plotlyDestroyTimers.has(cell)) return;
  const t = setTimeout(() => {
    plotlyDestroyTimers.delete(cell);
    if (cell.dataset.plotRegion === '1') return;
    const ctrl = plotlyInflight.get(cell);
    if (ctrl) ctrl.abort();
    destroyPlotly(cell);
  }, 2000);
  plotlyDestroyTimers.set(cell, t);
}

async function upgradeToPlotly(cell) {
  if (cell.dataset.plotlyLoaded === '1' || cell.dataset.plotlyLoading === '1') return;
  cell.dataset.plotlyLoading = '1';
  const ctrl = new AbortController();
  plotlyInflight.set(cell, ctrl);
  try {
    const r = await fetch(`${CHART_API}/${cell.dataset.id}`, { signal: ctrl.signal, cache: 'force-cache' });
    if (!r.ok) throw new Error('http ' + r.status);
    const p = await r.json();
    if (ctrl.signal.aborted) return;
    if (!p.data || !p.layout) throw new Error('payload missing');
    await new Promise((res) => enqueue(async () => {
      if (ctrl.signal.aborted) { res(); return; }
      let div = cell.querySelector('.plot');
      if (!div) { div = document.createElement('div'); div.className = 'plot'; cell.appendChild(div); }
      await Plotly.newPlot(div, p.data, p.layout, cfg);
      cell.dataset.plotlyLoaded = '1';
      cell.classList.add('loaded');
      trackActivePlotly(cell);
      res();
    }));
  } catch (err) {
    if (err.name !== 'AbortError') console.error('plotly upgrade', cell.dataset.id, err);
  } finally {
    cell.dataset.plotlyLoading = '';
    plotlyInflight.delete(cell);
  }
}

// SVG thumb lazy load — IntersectionObserver swaps in <img> when cell enters viewport
function loadThumb(cell) {
  if (cell.dataset.thumbLoaded === '1') return;
  const thumb = cell.querySelector('.thumb');
  if (!thumb) return;
  const img = thumb.querySelector('img');
  if (img && !img.src) {
    img.src = img.dataset.src;
    img.addEventListener('load', () => { cell.classList.add('loaded'); }, { once: true });
    img.addEventListener('error', () => { cell.classList.add('loaded'); }, { once: true });
  }
  cell.dataset.thumbLoaded = '1';
}

waitForPlotly().then(() => {
  const observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const cell = e.target;
      if (e.isIntersecting) {
        cell.dataset.plotRegion = '1';
        cancelDestroy(cell);
        loadThumb(cell);
      } else {
        cell.dataset.plotRegion = '';
        scheduleDestroy(cell);
      }
    }
  }, { rootMargin: '1200px 0px', threshold: 0 });
  document.querySelectorAll('.cell').forEach(c => observer.observe(c));
});

function setActiveCell(cell) {
  if (cell === activeCell || cell.dataset.plotlyLoaded !== '1') return;
  if (activeCell) activeCell.classList.remove('active');
  activeCell = cell;
  cell.classList.add('active');
  updateActiveLabel();
}

const grid = document.getElementById('grid');
let pendingCell = null, hoverTimer = null;
function requestInteractive(cell) {
  if (!cell) return;
  if (cell.dataset.plotlyLoaded !== '1') {
    upgradeToPlotly(cell).then(() => { if (cell.dataset.plotlyLoaded === '1') setActiveCell(cell); });
  } else {
    setActiveCell(cell);
  }
}
grid.addEventListener('mouseover', (e) => {
  const cell = e.target.closest('.cell');
  if (!cell || cell === pendingCell) return;
  pendingCell = cell;
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => {
    if (pendingCell !== cell) return;
    requestInteractive(cell);
  }, 150);
});
grid.addEventListener('click', (e) => {
  const cell = e.target.closest('.cell');
  if (!cell) return;
  pendingCell = cell;
  clearTimeout(hoverTimer);
  requestInteractive(cell);
});

// 검색 — Distribution 과 동일 UX
const searchInput = document.getElementById('search-input');
let filterTimer = null;
function applyFilter(q) {
  q = q.trim().toLowerCase();
  document.querySelectorAll('.cell').forEach(cell => {
    const name = (cell.dataset.name || '').toLowerCase();
    cell.style.display = (q === '' || name.includes(q) || cell.dataset.id === q) ? '' : 'none';
  });
}
searchInput.addEventListener('input', () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => { applyFilter(searchInput.value); searchInput.classList.remove('no-match'); }, 150);
});
searchInput.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const q = searchInput.value.trim().toLowerCase();
  if (!q) return;
  let target = null;
  document.querySelectorAll('.cell').forEach(cell => {
    if (target) return;
    const name = (cell.dataset.name || '').toLowerCase();
    if (name.includes(q) || cell.dataset.id === q) target = cell;
  });
  if (!target) {
    searchInput.classList.add('no-match');
    setTimeout(() => searchInput.classList.remove('no-match'), 800);
    return;
  }
  document.querySelectorAll('.cell').forEach(c => { c.style.display = ''; });
  target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  target.classList.remove('flash');
  void target.offsetWidth;
  target.classList.add('flash');
  loadThumb(target);
  requestInteractive(target);
  setTimeout(() => target.classList.remove('flash'), 1500);
});
</script>
</body>
</html>
"""


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))


def _thumb_loading(i):
    return "eager" if i < COLS_PER_ROW * 8 else "lazy"


def build_histogram_html(dataset_id, subjects, schools):
    cells = "\n".join(
        f'<div class="cell" data-id="{i}" data-name="{_esc(n)}">'
        f'<div class="thumb"><img alt="{_esc(n)}" loading="{_thumb_loading(i)}" '
        f'decoding="async" data-src="/api/{_esc(dataset_id)}/histogram_thumb/{i}"></div>'
        f'</div>'
        for i, n in enumerate(subjects)
    )
    sidebar = "\n".join(
        f'  <div class="sidebar-item">'
        f'<span class="swatch" style="background:{_esc(s["color"])}"></span>'
        f'<span class="label">{_esc(s["name"])}</span></div>'
        for s in schools
    )
    repl = {
        "__DATASET_ID__": dataset_id,
        "__COLS__": str(COLS_PER_ROW),
        "__AW__": str(CELL_ASPECT_W),
        "__AH__": str(CELL_ASPECT_H),
        "__N__": str(len(subjects)),
        "__CELLS__": cells,
        "__SIDEBAR__": sidebar,
    }
    html = HTML_TEMPLATE
    for k, v in repl.items():
        html = html.replace(k, v)
    return html

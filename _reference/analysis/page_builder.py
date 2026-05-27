from pathlib import Path

from config import CELL_ASPECT_H, CELL_ASPECT_W, COLS_PER_ROW

HTML_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>Cumulative Distribution by Subject — __DATASET_ID__</title>
<script>
  // Head-level boot diagnostic — runs immediately, before Plotly download.
  window.__BOOT_T0 = performance.now();
  window.__BOOT_STAGE = 'head:script-parsed';
  window.__BOOT_ERRORS = [];
  window.addEventListener('error', (e) => {
    window.__BOOT_ERRORS.push((e.message||'') + ' @' + (e.filename||'').split('/').pop() + ':' + (e.lineno||0));
    if (window.__updateBoot) window.__updateBoot();
  });
  window.addEventListener('unhandledrejection', (e) => {
    window.__BOOT_ERRORS.push('Promise: ' + (e.reason && e.reason.message || String(e.reason)));
    if (window.__updateBoot) window.__updateBoot();
  });
  function __updateBoot() {
    const el = document.getElementById('boot-status');
    if (!el) return;
    const elapsed = Math.round(performance.now() - window.__BOOT_T0) + 'ms';
    const err = window.__BOOT_ERRORS.length ? '\\nERR: ' + window.__BOOT_ERRORS[window.__BOOT_ERRORS.length-1] : '';
    el.textContent = `[${elapsed}] ${window.__BOOT_STAGE}${err}`;
  }
  window.__updateBoot = __updateBoot;
  document.addEventListener('DOMContentLoaded', () => { window.__BOOT_STAGE = 'head:DOMContentLoaded'; __updateBoot(); });
  setInterval(__updateBoot, 500);

  // thumb img load retry — fails로 인한 빈 박스 방지
  window.__retryThumb = function(img) {
    const tries = parseInt(img.dataset.retry || '0', 10);
    if (tries >= 2) {
      img.closest('.cell').classList.remove('loaded');
      return;
    }
    img.dataset.retry = String(tries + 1);
    const base = img.src.split('#')[0];
    setTimeout(() => { img.src = base + '#r' + tries; }, 400 * (tries + 1));
  };
</script>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" defer onload="window.__BOOT_STAGE='head:plotly-loaded';window.__updateBoot&&window.__updateBoot();" onerror="window.__BOOT_ERRORS.push('Plotly CDN load FAILED');window.__updateBoot&&window.__updateBoot();"></script>
<style>
  #boot-status {
    position: fixed; top: 0; left: 50%; transform: translateX(-50%);
    background: #ff0; color: #000; padding: 6px 14px; font: 13px monospace;
    z-index: 99999; border-radius: 0 0 6px 6px; box-shadow: 0 2px 6px rgba(0,0,0,0.2);
    white-space: pre; max-width: 90vw; overflow: hidden; text-overflow: ellipsis;
  }
  #boot-status.done { background: #cfc; opacity: 0.7; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #fafafa; }
  .topbar { position: sticky; top: 0; z-index: 100; background: #fff; border-bottom: 1px solid #ddd; padding: 8px 16px; display: flex; gap: 6px; align-items: center; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .topbar h1 { font-size: 14px; margin: 0 12px 0 0; color: #333; font-weight: 600; }
  .topbar .dataset { font-size: 11px; color: #999; margin-right: 12px; }
  .topbar .active-label { color: #666; font-size: 12px; }
  .topbar .active-label strong { color: #333; }
  .topbar input#search-input { padding: 5px 10px; font-size: 13px; min-width: 220px; border: 1px solid #ccc; border-radius: 4px; outline: none; transition: border-color 0.15s ease, background 0.15s ease; }
  .topbar input#search-input:focus { border-color: #4a90e2; }
  .topbar input#search-input.no-match { border-color: #e57373; background: #fff5f5; }
  .topbar .dash-link { color: #2369b3; text-decoration: none; font-size: 12px; border: 1px solid #c7d8ea; padding: 5px 8px; border-radius: 4px; background: #f7fbff; }
  .topbar .dash-link:hover { background: #eef6ff; }
  .content { padding: 16px 156px 16px 16px; }
  .sidebar { position: fixed; right: 0; top: 48px; bottom: 0; width: 140px; background: #fff; border-left: 1px solid #ddd; padding: 12px 10px; overflow-y: auto; z-index: 80; box-sizing: border-box; }
  .sidebar-title { font-size: 11px; color: #666; margin: 0 0 8px 2px; font-weight: 600; }
  .sidebar-hint { font-size: 10px; color: #999; margin-bottom: 8px; }
  .sidebar-item { display: flex; align-items: center; gap: 8px; width: 100%; padding: 5px 8px; margin-bottom: 4px; border: 1px solid #e0e0e0; border-radius: 4px; background: #fff; cursor: pointer; font-size: 12px; text-align: left; transition: opacity 0.1s, background 0.1s; font-family: inherit; }
  .sidebar-item:hover { background: #f5f5f5; }
  .sidebar-item.off { opacity: 0.35; text-decoration: line-through; }
  .sidebar-item .swatch { width: 14px; height: 14px; border-radius: 2px; flex-shrink: 0; border: 1px solid rgba(0,0,0,0.1); }
  .grid { display: grid; grid-template-columns: repeat(__COLS__, 1fr); gap: 16px; }
  .cell { position: relative; aspect-ratio: __AW__ / __AH__; background: #fff; border: 1px solid #e0e0e0; border-radius: 6px; overflow: hidden; transition: border-color 0.1s ease, box-shadow 0.1s ease; }
  .cell::before { content: 'loading...'; position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #bbb; font-size: 12px; pointer-events: none; }
  .cell.loaded::before { display: none; }
  .cell .thumb { width: 100%; height: 100%; }
  .cell .thumb img, .cell .thumb svg { width: 100%; height: 100%; display: block; }
  .cell .plot { width: 100%; height: 100%; position: absolute; inset: 0; }
  .cell.active { border-color: #4a90e2; box-shadow: 0 0 0 2px rgba(74,144,226,0.25); }
  .cell.flash { animation: cell-flash 1.5s ease-out; }
  @keyframes cell-flash {
    0%   { box-shadow: 0 0 0 0 rgba(255,193,7,0); border-color: #e0e0e0; }
    15%  { box-shadow: 0 0 0 6px rgba(255,193,7,0.7); border-color: #ffc107; }
    100% { box-shadow: 0 0 0 0 rgba(255,193,7,0); }
  }
  .note { position: absolute; background: #fff8c5; border: 1px solid #d4a72c; border-radius: 4px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); z-index: 50; display: flex; flex-direction: column; overflow: hidden; min-width: 120px; min-height: 70px; resize: both; }
  .note-header { display: flex; align-items: center; justify-content: space-between; padding: 2px 4px; background: rgba(0,0,0,0.06); user-select: none; cursor: move; }
  .note-drag { color: #666; padding: 2px 6px; font-size: 12px; }
  .note-del { border: none; background: transparent; cursor: pointer; font-size: 16px; color: #888; width: 22px; height: 22px; border-radius: 50%; padding: 0; line-height: 1; }
  .note-del:hover { background: rgba(0,0,0,0.1); color: #c00; }
  .note-body { flex: 1; padding: 6px 8px; outline: none; overflow: auto; font-size: 13px; line-height: 1.4; background: transparent; }
  .cell .modebar-container, .cell .modebar { display: none !important; }
  .cell.active .modebar-container, .cell.active .modebar { display: flex !important; position: fixed !important; top: 6px !important; right: 156px !important; z-index: 200 !important; background: rgba(255,255,255,0.96) !important; padding: 2px 6px !important; border-radius: 4px !important; box-shadow: 0 1px 4px rgba(0,0,0,0.15) !important; opacity: 1 !important; }
</style>
</head>
<body>
<div id="boot-status">[0ms] body:start</div>
<div class="topbar">
  <h1>Cumulative Distribution (n=__N__)</h1>
  <span class="dataset">dataset: __DATASET_ID__</span>
  <input id="search-input" type="text" placeholder="검색 (Enter: 해당 차트로 이동)" autocomplete="off">
  <button id="btn-add-note" type="button" title="메모 추가" style="padding:5px 10px;cursor:pointer;background:#fff8c5;border:1px solid #d4a72c;border-radius:4px;font-size:13px;">+ 메모</button>
  <span class="active-label">활성: <strong id="active-name">셀에 마우스를 올리세요</strong></span>
</div>
<div class="content"><div class="grid">
__CELLS__
</div></div>
<aside class="sidebar">
  <div class="sidebar-title">학교</div>
  <div class="sidebar-hint">클릭=숨김 토글</div>
__SIDEBAR_ITEMS__
</aside>
<script>
window.__BOOT_STAGE = 'body:main-script-entered';
window.__updateBoot && window.__updateBoot();
const DATASET_ID = '__DATASET_ID__';
const BUILD_VERSION = '__BUILD_VERSION__';
const API_BASE = `/api/${DATASET_ID}`;
const cfg = { scrollZoom: true, displaylogo: false, displayModeBar: true, responsive: true, modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
let activeCell = null;

const _T0 = performance.now();
let _lastError = '';

const _perf = {
  htmlReady: 0, jsReady: 0, plotlyReady: 0, ioFirstFire: 0,
  firstFetch: 0, firstNewPlotStart: 0, firstNewPlotDone: 0,
  cellsTotal: __N__, cellsRendered: 0,
};
const _debug = document.createElement('div');
_debug.id = 'perf-debug';
_debug.style.cssText = 'position:fixed;bottom:8px;left:8px;background:rgba(0,0,0,0.78);color:#fff;font-family:monospace;font-size:11px;padding:6px 10px;border-radius:4px;z-index:9999;pointer-events:none;white-space:pre;line-height:1.4;';
document.body.appendChild(_debug);
const _ms = (t) => t ? Math.round(t - _T0) + 'ms' : '...';
function _updateDebug() {
  _debug.textContent = [
    `Elapsed    : ${_ms(performance.now())}`,
    `HTML ready : ${_ms(_perf.htmlReady)}`,
    `JS init    : ${_ms(_perf.jsReady)}`,
    `Plotly.js  : ${_ms(_perf.plotlyReady)}`,
    `IO 1st    : ${_ms(_perf.ioFirstFire)}`,
    `1st fetch  : ${_ms(_perf.firstFetch)}`,
    `1st newPlot: ${_ms(_perf.firstNewPlotStart)} → ${_ms(_perf.firstNewPlotDone)}`,
    `Interactive: ${_perf.cellsRendered}/${_perf.cellsTotal}`,
    `Thumbs     : prebuilt SVG`,
    `Active     : ${activeCell ? 'sid_' + activeCell.dataset.id : '-'}`,
    `Last error : ${_lastError || '-'}`,
  ].join('\\n');
}
_perf.htmlReady = performance.now();
_updateDebug();
setInterval(_updateDebug, 1000);

window.addEventListener('error', (e) => {
  _lastError = (e.message || '') + ' @' + (e.filename ? e.filename.split('/').pop() : '?') + ':' + (e.lineno || 0);
  _updateDebug();
});
window.addEventListener('unhandledrejection', (e) => {
  _lastError = 'Promise: ' + (e.reason && e.reason.message || String(e.reason));
  _updateDebug();
});

function waitForPlotly() {
  return new Promise((resolve) => {
    if (typeof Plotly !== 'undefined') { _perf.plotlyReady = performance.now(); resolve(); return; }
    const w = setInterval(() => {
      if (typeof Plotly !== 'undefined') {
        clearInterval(w);
        _perf.plotlyReady = performance.now();
        window.__BOOT_STAGE = 'body:plotly-ready';
        window.__updateBoot && window.__updateBoot();
        resolve();
      }
    }, 30);
  });
}

const _plotQueue = [];
let _plotQueueDraining = false;
function enqueuePlot(task) {
  _plotQueue.push(task);
  if (!_plotQueueDraining) { _plotQueueDraining = true; requestAnimationFrame(_drainPlotQueue); }
}
async function _drainPlotQueue() {
  while (_plotQueue.length > 0) {
    try { await _plotQueue.shift()(); } catch (e) { console.error(e); }
    await new Promise((r) => requestAnimationFrame(r));
  }
  _plotQueueDraining = false;
}

const HIDDEN_KEY = 'dashboard-hidden-schools-v1';
let hiddenSchools = new Set();
try { hiddenSchools = new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) || '[]')); } catch (_) {}
const saveHidden = () => localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hiddenSchools]));
function applyHiddenToData(traces) { for (const t of traces) t.visible = !hiddenSchools.has(t.name); return traces; }
function syncSidebarUI() {
  document.querySelectorAll('.sidebar-item').forEach((el) => el.classList.toggle('off', hiddenSchools.has(el.dataset.school)));
}
const thumbUrl = (sid) => `${API_BASE}/thumb/${sid}?v=${BUILD_VERSION}`;
function applyHiddenToInlineThumb(cell) {
  const svg = cell.querySelector('.thumb svg');
  if (!svg) return false;
  svg.querySelectorAll('[data-school]').forEach((el) => {
    el.style.display = hiddenSchools.has(el.dataset.school) ? 'none' : '';
  });
  return true;
}
async function ensureInlineThumb(cell) {
  if (applyHiddenToInlineThumb(cell)) return;
  if (cell.dataset.thumbInlineLoading === '1') return;
  cell.dataset.thumbInlineLoading = '1';
  try {
    const resp = await fetch(thumbUrl(cell.dataset.id), { cache: 'no-store' });
    if (!resp.ok) throw new Error('thumb http ' + resp.status);
    const svg = await resp.text();
    const thumb = cell.querySelector('.thumb');
    if (thumb) {
      thumb.innerHTML = svg;
      cell.dataset.thumbInline = '1';
      cell.classList.add('loaded');
      applyHiddenToInlineThumb(cell);
    }
  } catch (err) {
    console.warn('thumb inline', cell.dataset.id, err);
  } finally {
    cell.dataset.thumbInlineLoading = '';
  }
}
function applyHiddenToThumb(cell) {
  if (hiddenSchools.size === 0) {
    applyHiddenToInlineThumb(cell);
    return;
  }
  ensureInlineThumb(cell);
}
function applyHiddenToVisibleThumbs() {
  document.querySelectorAll('.cell[data-plot-region="1"], .cell[data-thumb-inline="1"]').forEach(applyHiddenToThumb);
}

function isUserRelayout(ev) {
  if (!ev || typeof ev !== 'object') return false;
  for (const k of Object.keys(ev)) if (k.startsWith('xaxis') || k.startsWith('yaxis')) return true;
  return false;
}
function clampYAxis(div) {
  const yr = div.layout && div.layout.yaxis && div.layout.yaxis.range;
  if (!yr) return;
  const a = Math.max(0, yr[0]), b = Math.min(100, yr[1]);
  if (a !== yr[0] || b !== yr[1]) Plotly.relayout(div, { 'yaxis.range': [a, b] });
}
const attachZoomClamp = (cell, div) => div.on('plotly_relayout', (ev) => { if (isUserRelayout(ev)) clampYAxis(div); });

function toggleSchool(name) {
  if (hiddenSchools.has(name)) hiddenSchools.delete(name); else hiddenSchools.add(name);
  saveHidden(); syncSidebarUI();
  document.querySelectorAll('.cell[data-plotly-loaded="1"]').forEach((cell) => {
    const div = cell.querySelector('.plot');
    if (!div || !div.data) return;
    try { Plotly.restyle(div, { visible: div.data.map((t) => !hiddenSchools.has(t.name)) }); } catch (_) {}
  });
  applyHiddenToVisibleThumbs();
}
document.querySelector('.sidebar').addEventListener('click', (e) => {
  const item = e.target.closest('.sidebar-item');
  if (item) toggleSchool(item.dataset.school);
});
syncSidebarUI();

const activeNameEl = document.getElementById('active-name');
const updateActiveLabel = () => {
  activeNameEl.textContent = activeCell ? (activeCell.dataset.name || ('subject_' + activeCell.dataset.id)) : '셀에 마우스를 올리세요';
};

const plotlyInflight = new Map();
const plotlyDestroyTimers = new Map();

async function upgradeToPlotly(cell) {
  if (cell.dataset.plotlyLoaded === '1' || cell.dataset.plotlyLoading === '1') return;
  cell.dataset.plotlyLoading = '1';
  const ctrl = new AbortController();
  plotlyInflight.set(cell, ctrl);
  try {
    if (!_perf.firstFetch) _perf.firstFetch = performance.now();
    const resp = await fetch(`${API_BASE}/chart/${cell.dataset.id}`, { signal: ctrl.signal, cache: 'no-store' });
    if (!resp.ok) throw new Error('http ' + resp.status);
    const p = await resp.json();
    if (ctrl.signal.aborted) return;
    const figData = p.data, figLayout = p.layout;
    if (!figData || !figLayout) throw new Error('payload missing .data/.layout');
    applyHiddenToData(figData);
    await new Promise((resolveOuter) => {
      enqueuePlot(async () => {
        if (ctrl.signal.aborted) { resolveOuter(); return; }
        let div = cell.querySelector('.plot');
        if (!div) { div = document.createElement('div'); div.className = 'plot'; cell.appendChild(div); }
        if (!_perf.firstNewPlotStart) {
          _perf.firstNewPlotStart = performance.now();
          window.__BOOT_STAGE = 'body:first-newPlot-start';
          window.__updateBoot && window.__updateBoot();
        }
        await Plotly.newPlot(div, figData, figLayout, cfg);
        if (!_perf.firstNewPlotDone) {
          _perf.firstNewPlotDone = performance.now();
          window.__BOOT_STAGE = 'body:first-cell-rendered';
          window.__updateBoot && window.__updateBoot();
          setTimeout(() => { const el = document.getElementById('boot-status'); if (el) el.classList.add('done'); }, 2000);
        }
        attachZoomClamp(cell, div);
        cell.dataset.plotlyLoaded = '1';
        cell.classList.add('loaded');
        trackActivePlotly(cell);
        _perf.cellsRendered++;
        resolveOuter();
      });
    });
  } catch (err) {
    if (err.name !== 'AbortError') console.error('plotly upgrade', cell.dataset.id, err);
  } finally {
    cell.dataset.plotlyLoading = '';
    plotlyInflight.delete(cell);
  }
}

const _activePlotly = [];
const MAX_ACTIVE_PLOTLY = 8;
function trackActivePlotly(cell) {
  const idx = _activePlotly.indexOf(cell);
  if (idx >= 0) _activePlotly.splice(idx, 1);
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
  const idx = _activePlotly.indexOf(cell);
  if (idx >= 0) _activePlotly.splice(idx, 1);
  if (cell === activeCell) { cell.classList.remove('active'); activeCell = null; updateActiveLabel(); }
}

function cancelDestroyPlotly(cell) {
  const timer = plotlyDestroyTimers.get(cell);
  if (!timer) return;
  clearTimeout(timer);
  plotlyDestroyTimers.delete(cell);
}

function scheduleDestroyPlotly(cell) {
  if (plotlyDestroyTimers.has(cell)) return;
  const timer = setTimeout(() => {
    plotlyDestroyTimers.delete(cell);
    if (cell.dataset.plotRegion === '1') return;
    const ctrl = plotlyInflight.get(cell);
    if (ctrl) ctrl.abort();
    destroyPlotly(cell);
  }, 2000);
  plotlyDestroyTimers.set(cell, timer);
}

async function loadCell(cell) {
  applyHiddenToThumb(cell);
}

window.__BOOT_STAGE = 'body:waiting-plotly';
window.__updateBoot && window.__updateBoot();
waitForPlotly().then(() => {
  window.__BOOT_STAGE = 'body:attaching-observers';
  window.__updateBoot && window.__updateBoot();
  const observer = new IntersectionObserver((entries) => {
    if (!_perf.ioFirstFire) {
      _perf.ioFirstFire = performance.now();
      window.__BOOT_STAGE = 'body:io-first-fire';
      window.__updateBoot && window.__updateBoot();
    }
    for (const entry of entries) {
      const cell = entry.target;
      if (entry.isIntersecting) {
        cell.dataset.plotRegion = '1';
        cancelDestroyPlotly(cell);
        loadCell(cell);
      } else {
        cell.dataset.plotRegion = '';
        scheduleDestroyPlotly(cell);
      }
    }
  }, { rootMargin: '2400px 0px', threshold: 0 });
  document.querySelectorAll('.cell').forEach((el) => observer.observe(el));

  window.__BOOT_STAGE = 'body:observers-attached';
  window.__updateBoot && window.__updateBoot();
});

_perf.jsReady = performance.now();
_updateDebug();

const grid = document.querySelector('.grid');
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
function setActiveCell(cell) {
  if (cell === activeCell || cell.dataset.plotlyLoaded !== '1') return;
  if (activeCell) activeCell.classList.remove('active');
  activeCell = cell;
  cell.classList.add('active');
  updateActiveLabel();
}

const searchInput = document.getElementById('search-input');
let filterTimer = null;
function applyFilter(q) {
  q = q.trim().toLowerCase();
  document.querySelectorAll('.cell').forEach((cell) => {
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
  document.querySelectorAll('.cell').forEach((cell) => {
    if (target) return;
    const name = (cell.dataset.name || '').toLowerCase();
    if (name.includes(q) || cell.dataset.id === q) target = cell;
  });
  if (!target) {
    searchInput.classList.add('no-match');
    setTimeout(() => searchInput.classList.remove('no-match'), 800);
    return;
  }
  document.querySelectorAll('.cell').forEach((c) => { c.style.display = ''; });
  target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  target.classList.remove('flash');
  void target.offsetWidth;
  target.classList.add('flash');
  requestInteractive(target);
  setTimeout(() => target.classList.remove('flash'), 1500);
});

const NOTE_KEY = `dashboard-notes-${DATASET_ID}`;
let notes = (() => { try { return JSON.parse(localStorage.getItem(NOTE_KEY) || '[]'); } catch { return []; } })();
const saveNotes = () => localStorage.setItem(NOTE_KEY, JSON.stringify(notes));

function renderNote(note) {
  const el = document.createElement('div');
  el.className = 'note';
  el.dataset.id = note.id;
  el.style.left = note.x + 'px';
  el.style.top = note.y + 'px';
  if (note.w) el.style.width = note.w + 'px';
  if (note.h) el.style.height = note.h + 'px';
  el.innerHTML = '<div class="note-header"><span class="note-drag">⋮⋮ 드래그</span><button class="note-del" title="삭제">×</button></div><div class="note-body" contenteditable="true"></div>';
  el.querySelector('.note-body').innerHTML = note.text || '';
  document.body.appendChild(el);
  wireNote(el, note);
  return el;
}

function wireNote(el, note) {
  const header = el.querySelector('.note-header');
  let drag = null;
  header.addEventListener('pointerdown', (e) => {
    if (e.target.classList.contains('note-del')) return;
    drag = { sx: e.clientX, sy: e.clientY, nx: parseFloat(el.style.left), ny: parseFloat(el.style.top) };
    header.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  header.addEventListener('pointermove', (e) => {
    if (!drag) return;
    el.style.left = (drag.nx + e.clientX - drag.sx) + 'px';
    el.style.top = (drag.ny + e.clientY - drag.sy) + 'px';
  });
  header.addEventListener('pointerup', () => {
    if (!drag) return;
    drag = null;
    note.x = parseFloat(el.style.left); note.y = parseFloat(el.style.top);
    saveNotes();
  });
  el.querySelector('.note-del').addEventListener('click', () => {
    el.remove();
    notes = notes.filter((n) => n.id !== note.id);
    saveNotes();
  });
  const body = el.querySelector('.note-body');
  body.addEventListener('blur', () => { note.text = body.innerHTML; saveNotes(); });
  new ResizeObserver(() => { note.w = el.offsetWidth; note.h = el.offsetHeight; saveNotes(); }).observe(el);
}

notes.forEach(renderNote);
document.getElementById('btn-add-note').addEventListener('click', () => {
  const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  const note = { id, x: window.scrollX + Math.max(40, window.innerWidth / 2 - 110), y: window.scrollY + Math.max(80, window.innerHeight / 2 - 60), w: 220, h: 130, text: '' };
  notes.push(note); saveNotes();
  renderNote(note).querySelector('.note-body').focus();
});
</script>
</body>
</html>
"""


def _esc(s):
    return str(s).replace('"', "&quot;")


def _thumb_loading(i):
    return "eager" if i < COLS_PER_ROW * 8 else "lazy"


def write_html(out_path, subjects, schools, dataset_id="default", build_version="dev"):
    cells = "\n".join(
        f'    <div class="cell loaded" data-id="{i}" data-name="{_esc(n)}">'
        f'<div class="thumb"><img src="/api/{_esc(dataset_id)}/thumb/{i}?v={_esc(build_version)}" '
        f'alt="{_esc(n)}" loading="{_thumb_loading(i)}" decoding="async" '
        f'onerror="window.__retryThumb&amp;&amp;window.__retryThumb(this)"></div></div>'
        for i, n in enumerate(subjects)
    )
    items = "\n".join(
        f'  <button class="sidebar-item" type="button" data-school="{_esc(s["name"])}">'
        f'<span class="swatch" style="background:{_esc(s["color"])}"></span>'
        f'<span class="label">{_esc(s["name"])}</span></button>'
        for s in schools
    )
    repl = {
        "__COLS__": str(COLS_PER_ROW), "__AW__": str(CELL_ASPECT_W), "__AH__": str(CELL_ASPECT_H),
        "__N__": str(len(subjects)), "__CELLS__": cells, "__SIDEBAR_ITEMS__": items,
        "__DATASET_ID__": dataset_id, "__BUILD_VERSION__": build_version,
    }
    html = HTML_TEMPLATE
    for k, v in repl.items():
        html = html.replace(k, v)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

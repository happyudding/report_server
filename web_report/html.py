"""HTML renderer for web_report sessions."""
from __future__ import annotations

import json


def render_report_html(session: dict, report: dict) -> str:
    payload = json.dumps({"session": session, "report": report}, ensure_ascii=False)
    title = session.get("file_name") or session.get("session_id") or "Web Report"
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>Web Report - {title}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f5f7fb; color:#111827; }}
.topbar {{ position:sticky; top:0; z-index:10; display:flex; align-items:center; gap:12px; padding:10px 14px; background:#fff; border-bottom:1px solid #d8dee9; }}
.title {{ font-size:15px; font-weight:700; }}
.meta {{ font-size:12px; color:#4b5563; }}
.tabs {{ display:flex; gap:4px; padding:8px 14px; background:#eef2f7; border-bottom:1px solid #d8dee9; }}
.tab {{ border:1px solid #cbd5e1; background:#fff; padding:7px 11px; border-radius:4px; cursor:pointer; font-weight:600; font-size:12px; }}
.tab.active {{ background:#2563eb; color:#fff; border-color:#1d4ed8; }}
.panel {{ display:none; padding:14px; }}
.panel.active {{ display:block; }}
table {{ border-collapse:collapse; width:100%; background:#fff; font-size:12px; }}
th,td {{ border:1px solid #dbe3ef; padding:6px 8px; text-align:left; white-space:nowrap; }}
th {{ background:#f1f5f9; position:sticky; top:83px; z-index:2; }}
.table-wrap {{ overflow:auto; max-height:calc(100vh - 150px); border:1px solid #dbe3ef; background:#fff; }}
.dist-layout {{ display:grid; grid-template-columns:260px 1fr; gap:12px; }}
.subject-list {{ background:#fff; border:1px solid #dbe3ef; max-height:calc(100vh - 150px); overflow:auto; }}
.subject {{ display:block; width:100%; border:0; border-bottom:1px solid #edf2f7; background:#fff; text-align:left; padding:8px 10px; cursor:pointer; font-size:12px; }}
.subject.active {{ background:#dbeafe; font-weight:700; }}
#plot {{ height:calc(100vh - 150px); background:#fff; border:1px solid #dbe3ef; }}
.empty {{ padding:20px; background:#fff; border:1px solid #dbe3ef; color:#64748b; }}
</style>
</head>
<body>
<div class="topbar">
  <div class="title">Web Report</div>
  <div class="meta" id="meta"></div>
</div>
<div class="tabs" id="tabs"></div>
<div id="panels"></div>
<script>
const PAYLOAD = {payload};
const TAB_NAMES = ["Summary", "Yield", "CPK", "Fail Item", "Issue Table", "Distribution", "Raw"];
const COLORS = ["#2563eb","#dc2626","#16a34a","#9333ea","#ea580c","#0891b2","#be123c","#65a30d"];
const session = PAYLOAD.session || {{}};
const report = PAYLOAD.report || {{}};
document.getElementById("meta").textContent =
  `${{session.product_type || ""}} ${{session.product || ""}} ${{session.lot_id || ""}} · ${{session.file_name || ""}}`;

function esc(v) {{
  if (v === null || v === undefined) return "";
  return String(v).replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[c]));
}}
function tableHtml(rows) {{
  rows = rows || [];
  if (!rows.length) return '<div class="empty">데이터가 없습니다.</div>';
  const cols = Array.from(rows.reduce((s, r) => {{ Object.keys(r || {{}}).forEach(k => s.add(k)); return s; }}, new Set()));
  return `<div class="table-wrap"><table><thead><tr>${{cols.map(c=>`<th>${{esc(c)}}</th>`).join("")}}</tr></thead>` +
    `<tbody>${{rows.map(r=>`<tr>${{cols.map(c=>`<td>${{esc(r[c])}}</td>`).join("")}}</tr>`).join("")}}</tbody></table></div>`;
}}
function activate(name) {{
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", p.dataset.tab === name));
  if (name === "Distribution") renderDistribution(0);
}}
function renderTabs() {{
  const tabs = document.getElementById("tabs");
  const panels = document.getElementById("panels");
  tabs.innerHTML = "";
  panels.innerHTML = "";
  for (const name of TAB_NAMES) {{
    const b = document.createElement("button");
    b.className = "tab";
    b.dataset.tab = name;
    b.textContent = name;
    b.onclick = () => activate(name);
    tabs.appendChild(b);
    const p = document.createElement("section");
    p.className = "panel";
    p.dataset.tab = name;
    if (name === "Distribution") p.innerHTML = distributionShell();
    else p.innerHTML = tableHtml((report.sheets || {{}})[name]);
    panels.appendChild(p);
  }}
  activate("Summary");
}}
function distributionShell() {{
  const subjects = ((report.distribution || {{}}).subjects || []);
  if (!subjects.length) return '<div class="empty">Distribution 데이터가 없습니다.</div>';
  const buttons = subjects
    .map((s, i) => '<button class="subject" data-i="' + i + '">' + esc(s.name) + '</button>')
    .join("");
  return '<div class="dist-layout"><div class="subject-list" id="subject-list">' +
    buttons + '</div><div id="plot"></div></div>';
}}
function renderDistribution(idx) {{
  const subjects = ((report.distribution || {{}}).subjects || []);
  const s = subjects[idx];
  if (!s || typeof Plotly === "undefined") return;
  document.querySelectorAll(".subject").forEach(btn => {{
    btn.classList.toggle("active", Number(btn.dataset.i) === idx);
    btn.onclick = () => renderDistribution(Number(btn.dataset.i));
  }});
  const data = (s.traces || []).map((t, i) => ({{
    type: "scattergl",
    mode: "markers",
    name: t.source,
    x: t.x,
    y: t.y,
    marker: {{size: 4, color: COLORS[i % COLORS.length]}}
  }}));
  if (s.lo !== null && s.lo !== undefined) data.push({{type:"scatter", mode:"lines", name:"LSL", x:[s.lo,s.lo], y:[0,100], line:{{dash:"dash", color:"#dc2626"}}}});
  if (s.hi !== null && s.hi !== undefined) data.push({{type:"scatter", mode:"lines", name:"USL", x:[s.hi,s.hi], y:[0,100], line:{{dash:"dash", color:"#dc2626"}}}});
  Plotly.newPlot("plot", data, {{
    title: `${{s.name}} ${{s.unit ? "(" + s.unit + ")" : ""}}`,
    xaxis: {{title: "value"}},
    yaxis: {{title: "cumulative %", range:[0, 100]}},
    margin: {{l:55,r:20,t:55,b:45}}
  }}, {{responsive:true, displaylogo:false}});
}}
renderTabs();
</script>
</body>
</html>"""

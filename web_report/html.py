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
const TAB_NAMES = ["Yield", "CPK"];
const session = PAYLOAD.session || {{}};
const report = PAYLOAD.report || {{}};
document.getElementById("meta").textContent =
  `${{session.product_type || ""}} ${{session.product || ""}} ${{session.lot_id || ""}} - ${{session.file_name || ""}}`;

function esc(v) {{
  if (v === null || v === undefined) return "";
  return String(v).replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[c]));
}}
function tableHtml(rows) {{
  rows = rows || [];
  if (!rows.length) return '<div class="empty">No data.</div>';
  const cols = Array.from(rows.reduce((s, r) => {{ Object.keys(r || {{}}).forEach(k => s.add(k)); return s; }}, new Set()));
  return `<div class="table-wrap"><table><thead><tr>${{cols.map(c=>`<th>${{esc(c)}}</th>`).join("")}}</tr></thead>` +
    `<tbody>${{rows.map(r=>`<tr>${{cols.map(c=>`<td>${{esc(r[c])}}</td>`).join("")}}</tr>`).join("")}}</tbody></table></div>`;
}}
function activate(name) {{
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", p.dataset.tab === name));
}}
function renderTabs() {{
  const tabs = document.getElementById("tabs");
  const panels = document.getElementById("panels");
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
    p.innerHTML = tableHtml((report.sheets || {{}})[name]);
    panels.appendChild(p);
  }}
  activate("Yield");
}}
renderTabs();
</script>
</body>
</html>"""

"""Report generation: standalone HTML, CSV and JSON.

The HTML report is a single self-contained file with no external requests, so
it can be emailed, committed, or opened straight off disk. Rows are rendered
client-side from an embedded JSON blob, which keeps the file small and makes
sorting, filtering and search work without a server.
"""

from __future__ import annotations

import csv
import html
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .client import psi_web_link
from .config import Thresholds
from .db import Store

METRIC_LABELS = {
    "fcp": "FCP",
    "lcp": "LCP",
    "cls": "CLS",
    "tbt": "TBT",
}


def gather(store: Store, run_id: str) -> dict[str, Any]:
    """Assemble everything a report needs from the database."""
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(f"No such run: {run_id}")

    rows: list[dict[str, Any]] = []
    for row in store.results(run_id):
        try:
            issues = json.loads(row["issues"] or "[]")
        except ValueError:
            issues = []
        try:
            details = json.loads(row["details"] or "{}")
        except ValueError:
            details = {}
        rows.append(
            {
                "url": row["url"],
                "strategy": row["strategy"],
                "performance": row["performance"],
                "accessibility": row["accessibility"],
                "best_practices": row["best_practices"],
                "seo": row["seo"],
                "fcp": row["fcp"],
                "lcp": row["lcp"],
                "cls": row["cls"],
                "tbt": row["tbt"],
                "status": row["status"],
                "issues": issues,
                "details": details,
                "attempts": row["attempts"],
                "fetched_at": row["fetched_at"],
                "link": psi_web_link(row["url"], row["strategy"]),
            }
        )

    errors = [
        {
            "url": row["url"],
            "strategy": row["strategy"],
            "attempts": row["attempts"],
            "error": row["last_error"] or "unknown error",
            "kind": row["last_error_kind"] or "",
            "link": psi_web_link(row["url"], row["strategy"]),
        }
        for row in store.failed_tasks(run_id)
    ]

    pending = [
        {"url": row["url"], "strategy": row["strategy"], "state": row["state"]}
        for row in store.pending_tasks(run_id)
    ]

    counts = store.progress(run_id)
    summary = store.result_summary(run_id)
    thresholds = _thresholds_for(run)

    return {
        "run": {
            "id": run["id"],
            "label": run["label"],
            "source": run["source"],
            "status": run["status"],
            "created_at": run["created_at"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
        },
        "thresholds": thresholds,
        "rows": rows,
        "errors": errors,
        "pending": pending,
        "counts": counts,
        "summary": {
            "tested": len(rows),
            "passed": summary.get("PASS", 0),
            "failed": summary.get("FAIL", 0),
            "errored": len(errors),
            "not_run": len(pending),
            "unique_urls": len({r["url"] for r in rows} | {e["url"] for e in errors}),
        },
        "generated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


def _thresholds_for(run: Any) -> dict[str, Any]:
    from .config import Settings

    settings = Settings.from_json(run["config"])
    thresholds: Thresholds = settings.thresholds
    return {
        "performance": thresholds.performance,
        "accessibility": thresholds.accessibility,
        "best_practices": thresholds.best_practices,
        "seo": thresholds.seo,
        "fcp": thresholds.fcp,
        "lcp": thresholds.lcp,
        "cls": thresholds.cls,
        "tbt": thresholds.tbt,
    }


# ---------------------------------------------------------------- CSV / JSON


def to_csv(data: dict[str, Any]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "URL",
            "Device",
            "Performance",
            "Accessibility",
            "Best Practices",
            "SEO",
            "FCP (s)",
            "LCP (s)",
            "CLS",
            "TBT (s)",
            "Status",
            "Attempts",
            "Issues",
        ]
    )
    for row in data["rows"]:
        writer.writerow(
            [
                row["url"],
                row["strategy"],
                row["performance"],
                row["accessibility"],
                row["best_practices"],
                row["seo"],
                row["fcp"],
                row["lcp"],
                row["cls"],
                row["tbt"],
                row["status"],
                row["attempts"],
                "; ".join(row["issues"]),
            ]
        )
    # Untested URLs are reported explicitly instead of being silently scored 0.
    for err in data["errors"]:
        writer.writerow(
            [
                err["url"],
                err["strategy"],
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "ERROR",
                err["attempts"],
                err["error"],
            ]
        )
    return buffer.getvalue()


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2)


# --------------------------------------------------------------------- HTML

_CSS = """
:root{
  --bg:#eef1f8; --card:#ffffff; --ink:#1c2333; --muted:#6b7387; --line:#e3e7f0;
  --brand:#5b63d3; --brand2:#8b5cf6;
  --good-bg:#dcfce7; --good-ink:#14532d;
  --warn-bg:#fef3c7; --warn-ink:#78350f;
  --bad-bg:#fee2e2;  --bad-ink:#7f1d1d;
  --err-bg:#e5e7eb;  --err-ink:#374151;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#12141c; --card:#1a1d27; --ink:#e8eaf2; --muted:#9aa1b5; --line:#2a2f3d;
    --good-bg:#14532d; --good-ink:#bbf7d0;
    --warn-bg:#78350f; --warn-ink:#fde68a;
    --bad-bg:#7f1d1d;  --bad-ink:#fecaca;
    --err-bg:#374151;  --err-ink:#d1d5db;
  }
}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--ink);
  font:15px/1.5 'Segoe UI',system-ui,-apple-system,sans-serif}
.wrap{max-width:1500px;margin:0 auto}
header{background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;
  padding:28px 30px;border-radius:14px 14px 0 0}
header h1{margin:0 0 6px;font-size:1.55rem;letter-spacing:-.02em}
header .meta{opacity:.9;font-size:.9rem}
header .meta code{background:rgba(255,255,255,.18);padding:1px 6px;border-radius:4px}
main{background:var(--card);border-radius:0 0 14px 14px;padding:26px 30px 34px;
  box-shadow:0 12px 40px rgba(20,25,50,.12)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:26px}
.card{border:1px solid var(--line);border-radius:11px;padding:16px 18px;background:var(--card)}
.card h3{margin:0 0 6px;font-size:.74rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600}
.card .n{font-size:1.9rem;font-weight:700;line-height:1}
.card.pass .n{color:#16a34a}.card.fail .n{color:#dc2626}.card.err .n{color:#d97706}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:16px}
.controls input[type=search]{flex:1 1 240px;min-width:200px;padding:9px 12px;border:1px solid var(--line);
  border-radius:8px;background:var(--bg);color:var(--ink);font-size:.9rem}
.chip{padding:7px 13px;border:1px solid var(--line);border-radius:99px;background:var(--bg);
  color:var(--ink);cursor:pointer;font-size:.85rem;font-weight:600}
.chip[aria-pressed=true]{background:var(--brand);border-color:var(--brand);color:#fff}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:11px}
table{width:100%;border-collapse:collapse;font-size:.85rem}
thead th{position:sticky;top:0;background:var(--card);border-bottom:2px solid var(--line);
  padding:11px 10px;text-align:left;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted);cursor:pointer;white-space:nowrap;user-select:none}
thead th:hover{color:var(--ink)}
thead th .arrow{opacity:.35;font-size:.65rem}
tbody td{padding:10px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr.fail{background:color-mix(in srgb,var(--bad-bg) 22%,transparent)}
tbody tr.err{background:color-mix(in srgb,var(--err-bg) 30%,transparent)}
tbody tr.detail-row td{background:var(--bg);padding:0}
.url{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.url a{color:var(--brand);text-decoration:none;font-weight:500}
.url a:hover{text-decoration:underline}
.pill{display:inline-block;min-width:42px;text-align:center;padding:3px 9px;border-radius:6px;font-weight:700;font-size:.82rem}
.g{background:var(--good-bg);color:var(--good-ink)}
.a{background:var(--warn-bg);color:var(--warn-ink)}
.p{background:var(--bad-bg);color:var(--bad-ink)}
.n{background:var(--err-bg);color:var(--err-ink)}
.dev{display:inline-block;padding:2px 8px;border-radius:5px;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.03em}
.dev.mobile{background:#dbeafe;color:#1e40af}
.dev.desktop{background:#f3e8ff;color:#6b21a8}
@media (prefers-color-scheme:dark){.dev.mobile{background:#1e3a8a;color:#bfdbfe}.dev.desktop{background:#581c87;color:#e9d5ff}}
.status{font-weight:700;font-size:.8rem}
.status.PASS{color:#16a34a}.status.FAIL{color:#dc2626}.status.ERROR{color:#d97706}
.issues{color:var(--muted);font-size:.79rem;max-width:330px}
.expand{cursor:pointer;border:none;background:none;color:var(--muted);font-size:.95rem;padding:2px 6px;border-radius:5px}
.expand:hover{background:var(--line);color:var(--ink)}
.detail{padding:16px 20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px}
.detail h4{margin:0 0 8px;font-size:.74rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.detail ul{margin:0;padding-left:18px;font-size:.82rem}
.detail li{margin-bottom:4px}
.detail .save{color:var(--muted);font-variant-numeric:tabular-nums}
.empty{padding:36px;text-align:center;color:var(--muted)}
footer{margin-top:26px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:.82rem}
footer ul{margin:8px 0 0;padding-left:20px}
@media print{body{background:#fff;padding:0}main{box-shadow:none}.controls{display:none}}
"""

_JS = """
const DATA = __DATA__;
const T = DATA.thresholds;
let filter = 'all', device = 'all', query = '', sortKey = 'url', sortDir = 1;

const rows = DATA.rows.map(r => ({...r, kind: r.status === 'FAIL' ? 'fail' : 'pass'}));
DATA.errors.forEach(e => rows.push({
  url: e.url, strategy: e.strategy, status: 'ERROR', kind: 'err',
  performance: null, accessibility: null, best_practices: null, seo: null,
  fcp: null, lcp: null, cls: null, tbt: null,
  issues: [e.error], details: {}, attempts: e.attempts, link: e.link
}));

const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function scoreCls(v, min) {
  if (v === null || v === undefined) return 'n';
  if (min !== null && min !== undefined) return v >= min ? 'g' : 'p';
  if (v >= 90) return 'g';
  if (v >= 50) return 'a';
  return 'p';
}
function metricCls(v, max) {
  if (v === null || v === undefined) return 'n';
  if (max === null || max === undefined) return v === null ? 'n' : '';
  if (v <= max) return 'g';
  if (v <= max * 1.5) return 'a';
  return 'p';
}
const fmt = (v, suffix) => (v === null || v === undefined) ? '—' : v + (suffix || '');

function visible() {
  return rows.filter(r => {
    if (filter === 'fail' && r.kind !== 'fail') return false;
    if (filter === 'pass' && r.kind !== 'pass') return false;
    if (filter === 'err'  && r.kind !== 'err')  return false;
    if (device !== 'all' && r.strategy !== device) return false;
    if (query && !r.url.toLowerCase().includes(query)) return false;
    return true;
  }).sort((a, b) => {
    let x = a[sortKey], y = b[sortKey];
    if (x === null || x === undefined) x = sortDir === 1 ? Infinity : -Infinity;
    if (y === null || y === undefined) y = sortDir === 1 ? Infinity : -Infinity;
    if (typeof x === 'string') return x.localeCompare(y) * sortDir;
    return (x - y) * sortDir;
  });
}

function detailHtml(r) {
  const d = r.details || {};
  const blocks = [];
  const list = (title, items, render) => {
    if (!items || !items.length) return;
    blocks.push(`<div><h4>${esc(title)}</h4><ul>${items.map(render).join('')}</ul></div>`);
  };
  list('Failing accessibility checks', d.failing_accessibility,
       a => `<li>${esc(a.title)}</li>`);
  list('Failing SEO checks', d.failing_seo, a => `<li>${esc(a.title)}</li>`);
  list('Failing best-practice checks', d.failing_best_practices,
       a => `<li>${esc(a.title)}</li>`);
  list('Top opportunities', d.opportunities,
       o => `<li>${esc(o.title)} <span class="save">· saves ~${(o.savings_ms/1000).toFixed(2)}s</span></li>`);
  if (r.issues && r.issues.length)
    blocks.unshift(`<div><h4>Why this ${r.status === 'ERROR' ? 'could not be tested' : 'failed'}</h4><ul>${
      r.issues.map(i => `<li>${esc(i)}</li>`).join('')}</ul></div>`);
  if (!blocks.length) blocks.push('<div><h4>No further detail</h4></div>');
  return `<div class="detail">${blocks.join('')}</div>`;
}

function render() {
  const list = visible();
  const body = document.getElementById('body');
  if (!list.length) {
    body.innerHTML = '<tr><td colspan="12" class="empty">No rows match these filters.</td></tr>';
    document.getElementById('shown').textContent = '0';
    return;
  }
  body.innerHTML = list.map((r, i) => `
    <tr class="${r.kind}">
      <td><button class="expand" data-i="${i}" aria-label="Show detail">▸</button></td>
      <td class="url" title="${esc(r.url)}"><a href="${esc(r.link)}" target="_blank" rel="noopener">${esc(r.url)}</a></td>
      <td><span class="dev ${esc(r.strategy)}">${esc(r.strategy)}</span></td>
      <td><span class="pill ${scoreCls(r.performance, T.performance)}">${fmt(r.performance)}</span></td>
      <td><span class="pill ${scoreCls(r.accessibility, T.accessibility)}">${fmt(r.accessibility)}</span></td>
      <td><span class="pill ${scoreCls(r.best_practices, T.best_practices)}">${fmt(r.best_practices)}</span></td>
      <td><span class="pill ${scoreCls(r.seo, T.seo)}">${fmt(r.seo)}</span></td>
      <td class="${metricCls(r.fcp, T.fcp)}">${fmt(r.fcp, 's')}</td>
      <td class="${metricCls(r.lcp, T.lcp)}">${fmt(r.lcp, 's')}</td>
      <td class="${metricCls(r.cls, T.cls)}">${fmt(r.cls)}</td>
      <td><span class="status ${esc(r.status)}">${esc(r.status)}</span></td>
      <td class="issues">${esc((r.issues || []).join('; ')).slice(0, 220)}</td>
    </tr>
    <tr class="detail-row" id="d${i}" hidden><td colspan="12">${detailHtml(r)}</td></tr>`).join('');
  document.getElementById('shown').textContent = String(list.length);

  body.querySelectorAll('.expand').forEach(btn => btn.addEventListener('click', () => {
    const row = document.getElementById('d' + btn.dataset.i);
    row.hidden = !row.hidden;
    btn.textContent = row.hidden ? '▸' : '▾';
  }));
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-filter]').forEach(b => b.addEventListener('click', () => {
    filter = b.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(o =>
      o.setAttribute('aria-pressed', String(o === b)));
    render();
  }));
  document.querySelectorAll('[data-device]').forEach(b => b.addEventListener('click', () => {
    device = b.dataset.device;
    document.querySelectorAll('[data-device]').forEach(o =>
      o.setAttribute('aria-pressed', String(o === b)));
    render();
  }));
  document.getElementById('q').addEventListener('input', e => {
    query = e.target.value.trim().toLowerCase();
    render();
  });
  document.querySelectorAll('th[data-key]').forEach(th => th.addEventListener('click', () => {
    const key = th.dataset.key;
    sortDir = (key === sortKey) ? -sortDir : 1;
    sortKey = key;
    document.querySelectorAll('th[data-key] .arrow').forEach(a => a.textContent = '');
    const arrow = th.querySelector('.arrow');
    if (arrow) arrow.textContent = sortDir === 1 ? '▲' : '▼';
    render();
  }));
  render();
});
"""


def _threshold_notes(thresholds: dict[str, Any]) -> str:
    labels = {
        "performance": ("Performance", "≥", ""),
        "accessibility": ("Accessibility", "≥", ""),
        "best_practices": ("Best Practices", "≥", ""),
        "seo": ("SEO", "≥", ""),
        "fcp": ("First Contentful Paint", "≤", "s"),
        "lcp": ("Largest Contentful Paint", "≤", "s"),
        "cls": ("Cumulative Layout Shift", "≤", ""),
        "tbt": ("Total Blocking Time", "≤", "s"),
    }
    items = []
    for key, (label, op, unit) in labels.items():
        value = thresholds.get(key)
        if value is None:
            items.append(f"<li><strong>{html.escape(label)}:</strong> not enforced</li>")
        else:
            items.append(
                f"<li><strong>{html.escape(label)}:</strong> {op}{value}{unit}</li>"
            )
    return "\n".join(items)


def to_html(data: dict[str, Any]) -> str:
    run = data["run"]
    s = data["summary"]
    title = f"PageSpeed Report — {run['label']}"

    payload = json.dumps(data).replace("</", "<\\/")
    script = _JS.replace("__DATA__", payload)

    not_run_card = ""
    if s["not_run"]:
        not_run_card = f"""
        <div class="card"><h3>Not yet run</h3><div class="n">{s['not_run']}</div></div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>PageSpeed Insights Report</h1>
    <div class="meta">
      {html.escape(run['label'])} · run <code>{html.escape(run['id'])}</code> ·
      generated {html.escape(data['generated_at'])}
    </div>
  </header>
  <main>
    <div class="cards">
      <div class="card"><h3>URLs</h3><div class="n">{s['unique_urls']}</div></div>
      <div class="card"><h3>Tests run</h3><div class="n">{s['tested']}</div></div>
      <div class="card pass"><h3>Passed</h3><div class="n">{s['passed']}</div></div>
      <div class="card fail"><h3>Failed</h3><div class="n">{s['failed']}</div></div>
      <div class="card err"><h3>Errored</h3><div class="n">{s['errored']}</div></div>{not_run_card}
    </div>

    <div class="controls">
      <input type="search" id="q" placeholder="Filter by URL…" aria-label="Filter by URL">
      <button class="chip" data-filter="all" aria-pressed="true">All</button>
      <button class="chip" data-filter="fail" aria-pressed="false">Failed</button>
      <button class="chip" data-filter="pass" aria-pressed="false">Passed</button>
      <button class="chip" data-filter="err" aria-pressed="false">Errors</button>
      <span style="width:12px"></span>
      <button class="chip" data-device="all" aria-pressed="true">Both</button>
      <button class="chip" data-device="mobile" aria-pressed="false">Mobile</button>
      <button class="chip" data-device="desktop" aria-pressed="false">Desktop</button>
    </div>

    <div class="tablewrap">
      <table>
        <thead><tr>
          <th style="width:34px"></th>
          <th data-key="url">URL <span class="arrow">▲</span></th>
          <th data-key="strategy">Device <span class="arrow"></span></th>
          <th data-key="performance">Perf <span class="arrow"></span></th>
          <th data-key="accessibility">A11y <span class="arrow"></span></th>
          <th data-key="best_practices">BP <span class="arrow"></span></th>
          <th data-key="seo">SEO <span class="arrow"></span></th>
          <th data-key="fcp">FCP <span class="arrow"></span></th>
          <th data-key="lcp">LCP <span class="arrow"></span></th>
          <th data-key="cls">CLS <span class="arrow"></span></th>
          <th data-key="status">Status <span class="arrow"></span></th>
          <th>Issues</th>
        </tr></thead>
        <tbody id="body"></tbody>
      </table>
    </div>

    <footer>
      Showing <span id="shown">0</span> rows. Source:
      <code>{html.escape(run['source'])}</code>
      <p style="margin:14px 0 4px"><strong>Pass criteria</strong></p>
      <ul>{_threshold_notes(data['thresholds'])}</ul>
      <p style="margin-top:12px">
        <strong>ERROR</strong> means the test could not be completed after every retry —
        it is not a score of zero. Expand a row to see why.
      </p>
    </footer>
  </main>
</div>
<script>{script}</script>
</body>
</html>"""


def write_report(
    store: Store,
    run_id: str,
    *,
    out_dir: Path,
    formats: Sequence[str] = ("html",),
    stem: str | None = None,
) -> dict[str, Path]:
    """Write the requested formats to `out_dir`. Returns {format: path}."""
    data = gather(store, run_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    label = "".join(c if c.isalnum() or c in "-_." else "-" for c in data["run"]["label"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = stem or f"pagespeed-{label}-{run_id}-{stamp}"

    renderers = {"html": to_html, "csv": to_csv, "json": to_json}
    written: dict[str, Path] = {}
    for fmt in formats:
        renderer = renderers.get(fmt)
        if renderer is None:
            continue
        path = out_dir / f"{base}.{fmt}"
        path.write_text(renderer(data), encoding="utf-8")
        written[fmt] = path
    return written

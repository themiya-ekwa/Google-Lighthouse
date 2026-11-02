"""Local web dashboard.

Serves a single page that lists every run with live progress, and can start,
resume, retry and report on runs -- the browser workflow the original tool had,
but backed by the resumable engine instead of a JavaScript array.

Binds to loopback by default. There is no authentication, so exposing it on a
public interface would let anyone on the network spend your API quota.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import config, report, sitemap
from .db import DONE, FAILED, PENDING, Store
from .engine import Supervisor
from .net import FetchError

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PageSpeed Runs</title>
<style>
:root{--bg:#eef1f8;--card:#fff;--ink:#1c2333;--muted:#6b7387;--line:#e3e7f0;
  --brand:#5b63d3;--brand2:#8b5cf6;--good:#16a34a;--bad:#dc2626;--warn:#d97706}
@media(prefers-color-scheme:dark){:root{--bg:#12141c;--card:#1a1d27;--ink:#e8eaf2;
  --muted:#9aa1b5;--line:#2a2f3d}}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--ink);
  font:15px/1.55 'Segoe UI',system-ui,-apple-system,sans-serif}
.wrap{max-width:1100px;margin:0 auto}
header{background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;
  padding:24px 28px;border-radius:14px;margin-bottom:20px}
header h1{margin:0 0 4px;font-size:1.5rem;letter-spacing:-.02em}
header p{margin:0;opacity:.9;font-size:.9rem}
.panel{background:var(--card);border:1px solid var(--line);border-radius:13px;
  padding:20px 22px;margin-bottom:18px}
.panel h2{margin:0 0 14px;font-size:.78rem;text-transform:uppercase;
  letter-spacing:.07em;color:var(--muted)}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;align-items:end}
label{display:block;font-size:.76rem;color:var(--muted);margin-bottom:5px;font-weight:600}
input,select{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;
  background:var(--bg);color:var(--ink);font-size:.9rem;font-family:inherit}
button{padding:9px 16px;border:none;border-radius:8px;background:var(--brand);color:#fff;
  font-weight:600;font-size:.88rem;cursor:pointer;font-family:inherit}
button:hover{filter:brightness(1.08)}
button.ghost{background:transparent;color:var(--brand);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:not-allowed}
.run{border:1px solid var(--line);border-radius:11px;padding:15px 17px;margin-bottom:11px}
.run-top{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;justify-content:space-between}
.run-name{font-weight:700}
.run-id{font-family:ui-monospace,Consolas,monospace;font-size:.78rem;color:var(--muted)}
.badge{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
  padding:3px 9px;border-radius:99px;background:var(--line);color:var(--muted)}
.badge.running{background:#dbeafe;color:#1e40af}
.badge.done{background:#dcfce7;color:#14532d}
.badge.paused{background:#fef3c7;color:#78350f}
.badge.stalled{background:#fee2e2;color:#7f1d1d}
.bar{height:9px;background:var(--line);border-radius:99px;overflow:hidden;margin:11px 0 8px}
.bar>span{display:block;height:100%;background:linear-gradient(90deg,var(--brand),var(--brand2));
  transition:width .4s}
.stats{font-size:.83rem;color:var(--muted);display:flex;gap:14px;flex-wrap:wrap}
.stats b{color:var(--ink)}
.stats .ok b{color:var(--good)}.stats .no b{color:var(--bad)}.stats .er b{color:var(--warn)}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.actions button{padding:6px 13px;font-size:.82rem}
.empty{color:var(--muted);text-align:center;padding:26px}
#msg{padding:11px 14px;border-radius:9px;margin-bottom:14px;display:none;font-size:.88rem}
#msg.err{background:#fee2e2;color:#7f1d1d;display:block}
#msg.ok{background:#dcfce7;color:#14532d;display:block}
.offline{background:#fee2e2;color:#7f1d1d;padding:10px 14px;border-radius:9px;
  margin-bottom:14px;font-size:.88rem;display:none}
</style></head><body><div class="wrap">
<header>
  <h1>PageSpeed Runs</h1>
  <p>Runs continue where they left off after a crash, reboot or dropped connection.</p>
</header>

<div id="msg"></div>
<div class="offline" id="offline">Connection to the dashboard lost — retrying…</div>

<div class="panel">
  <h2>New run</h2>
  <div class="form">
    <div style="grid-column:1/-1">
      <label for="src">Sitemap URL, site root, or several separated by commas</label>
      <input id="src" placeholder="https://example.com/page-sitemap.xml">
    </div>
    <div><label for="label">Label (optional)</label><input id="label" placeholder="auto"></div>
    <div><label for="strategy">Devices</label>
      <select id="strategy">
        <option value="both">Mobile + Desktop</option>
        <option value="mobile">Mobile only</option>
        <option value="desktop">Desktop only</option>
      </select></div>
    <div><label for="limit">Max URLs (optional)</label><input id="limit" type="number" min="1"></div>
    <div><button id="go" onclick="createRun()">Start run</button></div>
  </div>
</div>

<div class="panel">
  <h2>Runs</h2>
  <div id="runs"><div class="empty">Loading…</div></div>
</div>
</div>
<script>
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function msg(text, kind) {
  const el = document.getElementById('msg');
  el.textContent = text; el.className = kind;
  if (kind === 'ok') setTimeout(() => { el.className = ''; }, 6000);
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

async function createRun() {
  const button = document.getElementById('go');
  const src = document.getElementById('src').value.trim();
  if (!src) { msg('Enter a sitemap URL first.', 'err'); return; }
  button.disabled = true; button.textContent = 'Reading sitemap…';
  try {
    const body = {
      source: src,
      label: document.getElementById('label').value.trim(),
      strategy: document.getElementById('strategy').value,
      limit: parseInt(document.getElementById('limit').value, 10) || null
    };
    const out = await api('/api/runs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    msg(`Started ${out.runs.length} run(s): ${out.runs.map(r => r.label).join(', ')}`, 'ok');
    document.getElementById('src').value = '';
    document.getElementById('label').value = '';
    refresh();
  } catch (e) { msg(e.message, 'err'); }
  finally { button.disabled = false; button.textContent = 'Start run'; }
}

async function act(id, action) {
  try {
    const out = await api(`/api/runs/${id}/${action}`, {method: 'POST'});
    if (action === 'delete') msg('Run deleted.', 'ok');
    if (action === 'stop') {
      msg(out.stopping
        ? 'Stopping — tests already sent will finish. Resume picks up from there.'
        : 'That run is not being driven by this dashboard.', out.stopping ? 'ok' : 'err');
    }
    refresh();
  } catch (e) { msg(e.message, 'err'); }
}

function runCard(r) {
  const total = r.counts.total || 0;
  const finished = r.counts.done + r.counts.failed;
  const pct = total ? Math.round(finished / total * 100) : 0;
  const busy = r.status === 'running';
  return `<div class="run">
    <div class="run-top">
      <div><span class="run-name">${esc(r.label)}</span>
        <span class="run-id">${esc(r.id)}</span></div>
      <span class="badge ${esc(r.status)}">${esc(r.status)}</span>
    </div>
    <div class="bar"><span style="width:${pct}%"></span></div>
    <div class="stats">
      <span><b>${finished}</b>/${total} tests</span>
      <span class="ok">pass <b>${r.summary.PASS}</b></span>
      <span class="no">fail <b>${r.summary.FAIL}</b></span>
      <span class="er">errored <b>${r.counts.failed}</b></span>
      ${r.counts.pending ? `<span>pending <b>${r.counts.pending}</b></span>` : ''}
    </div>
    <div class="actions">
      ${r.resumable && !busy ? `<button onclick="act('${r.id}','resume')">Resume</button>` : ''}
      ${busy ? `<button onclick="act('${r.id}','stop')">Stop</button>` : ''}
      ${r.counts.failed ? `<button onclick="act('${r.id}','retry')">Retry ${r.counts.failed} failed</button>` : ''}
      ${r.counts.done ? `<button class="ghost" onclick="window.open('/report/${r.id}','_blank')">Report</button>` : ''}
      ${r.counts.done ? `<button class="ghost" onclick="window.open('/report/${r.id}?format=csv','_blank')">CSV</button>` : ''}
      ${busy ? '' : `<button class="ghost" onclick="if(confirm('Delete this run and its results?'))act('${r.id}','delete')">Delete</button>`}
    </div>
  </div>`;
}

async function refresh() {
  try {
    const data = await api('/api/runs');
    document.getElementById('offline').style.display = 'none';
    const box = document.getElementById('runs');
    box.innerHTML = data.runs.length
      ? data.runs.map(runCard).join('')
      : '<div class="empty">No runs yet. Start one above.</div>';
  } catch (e) {
    document.getElementById('offline').style.display = 'block';
  }
}

refresh();
setInterval(refresh, 2000);
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "psi-dashboard"
    store: Store
    settings: config.Settings
    supervisor: Supervisor

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/runs":
            self._json(200, {"runs": [self._run_view(r) for r in self.store.list_runs(limit=50)]})
            return

        if path.startswith("/report/"):
            run_id = self.store.resolve_run_id(path[len("/report/"):])
            if not run_id:
                self._error(404, "No such run")
                return
            query = urllib.parse.parse_qs(parsed.query)
            fmt = (query.get("format") or ["html"])[0]
            try:
                data = report.gather(self.store, run_id)
            except KeyError:
                self._error(404, "No such run")
                return
            if fmt == "csv":
                self._send(200, report.to_csv(data).encode("utf-8"), "text/csv; charset=utf-8")
            elif fmt == "json":
                self._send(200, report.to_json(data).encode("utf-8"), "application/json")
            else:
                self._send(200, report.to_html(data).encode("utf-8"), "text/html; charset=utf-8")
            return

        self._error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/runs":
            self._create_runs()
            return

        if path.startswith("/api/runs/"):
            parts = [p for p in path[len("/api/runs/"):].split("/") if p]
            if len(parts) != 2:
                self._error(404, "Not found")
                return
            run_id = self.store.resolve_run_id(parts[0])
            if not run_id:
                self._error(404, "No such run")
                return
            action = parts[1]

            if action == "resume":
                started = self.supervisor.start(run_id)
                self._json(200, {"ok": started})
            elif action == "stop":
                # Only this process's runs can be stopped; one being driven by
                # another window or a `psi run` has to be stopped there.
                stopped = self.supervisor.stop_run(run_id)
                self._json(200, {"ok": stopped, "stopping": stopped})
            elif action == "retry":
                count = self.store.requeue_failed(run_id)
                self.supervisor.start(run_id)
                self._json(200, {"ok": True, "requeued": count})
            elif action == "delete":
                self.store.delete_run(run_id)
                self._json(200, {"ok": True})
            else:
                self._error(400, f"Unknown action {action!r}")
            return

        self._error(404, "Not found")

    # -- helpers ------------------------------------------------------------

    def _create_runs(self) -> None:
        body = self._body()
        raw_source = str(body.get("source") or "").strip()
        if not raw_source:
            self._error(400, "A sitemap URL is required")
            return

        sources = [s.strip() for s in raw_source.split(",") if s.strip()]
        strategy = body.get("strategy") or "both"
        strategies = (
            config.ALL_STRATEGIES if strategy == "both" else (strategy,)
        )
        limit = body.get("limit")
        limit = int(limit) if isinstance(limit, (int, str)) and str(limit).isdigit() else None

        from dataclasses import replace as _replace

        settings = _replace(self.settings, strategies=strategies)

        created = []
        problems = []
        for source in sources:
            if not source.startswith(("http://", "https://")):
                source = "https://" + source
            try:
                found = sitemap.collect_urls(
                    source, timeout=settings.request_timeout, limit=limit
                )
            except (FetchError, ValueError) as exc:
                problems.append(f"{source}: {exc}")
                continue
            if not found.urls:
                problems.append(f"{source}: no URLs found")
                continue

            label = (body.get("label") or "").strip() or sitemap.label_for(source)
            if len(sources) > 1 and body.get("label"):
                label = f"{label} ({sitemap.label_for(source)})"

            run_id = self.store.create_run(
                label=label,
                source=source,
                source_kind="sitemap",
                config_json=settings.to_json(),
                urls=found.urls,
                strategies=strategies,
            )
            self.supervisor.start(run_id, settings=settings)
            created.append({"id": run_id, "label": label, "urls": len(found.urls)})

        if not created:
            self._error(400, "; ".join(problems) or "Nothing could be started")
            return
        self._json(200, {"runs": created, "warnings": problems})

    def _run_view(self, run: Any) -> dict[str, Any]:
        counts = self.store.progress(run["id"])
        summary = self.store.result_summary(run["id"])
        status = self.store.display_status(run)
        return {
            "id": run["id"],
            "label": run["label"],
            "source": run["source"],
            "status": status,
            "created_at": run["created_at"],
            "counts": {
                "total": counts["total"],
                "done": counts[DONE],
                "failed": counts[FAILED],
                "pending": counts[PENDING],
                "running": counts["running"],
            },
            "summary": {"PASS": summary.get("PASS", 0), "FAIL": summary.get("FAIL", 0)},
            "resumable": counts[PENDING] > 0 or counts["running"] > 0,
        }


def serve(
    store: Store,
    settings: config.Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    supervisor = Supervisor(store, settings)

    handler = type(
        "BoundHandler",
        (_Handler,),
        {"store": store, "settings": settings, "supervisor": supervisor},
    )

    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    url = f"http://{host}:{port}/"
    print(f"Dashboard: {url}", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "  Warning: this is reachable from the network and has no login. "
            "Anyone who can reach it can spend your API quota.",
            file=sys.stderr,
        )

    unfinished = store.resumable_runs()
    if unfinished:
        print(f"  {len(unfinished)} run(s) have unfinished work — use Resume in the dashboard.")

    print("  Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping. In-progress runs are saved and can be resumed.")
    finally:
        supervisor.stop()
        supervisor.shutdown()
        httpd.server_close()

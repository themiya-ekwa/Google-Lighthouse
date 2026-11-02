"""Desktop GUI.

A local Tkinter application over the same engine the CLI uses. Tkinter ships
with Python on Windows, so this needs no install and can be launched by
double-clicking `psi-gui.pyw`.

Threading rule: Tkinter may only be touched from the thread that created the
widgets. The engine reports progress from its worker threads, so every event
goes onto a queue and is drained by the UI thread in `_pump`.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from . import config, report, sitemap
from .db import DONE, FAILED, PENDING, RUNNING, Store
from .engine import Supervisor
from .net import FetchError

REFRESH_MS = 1000
PUMP_MS = 150
MAX_LOG_LINES = 500

STATUS_COLOURS = {
    "running": "#1e40af",
    "stopping": "#78350f",
    "done": "#14532d",
    "paused": "#78350f",
    "stalled": "#7f1d1d",
    "pending": "#374151",
}

# Result-row filters, in the order they appear in the dropdown.
RESULT_FILTERS: dict[str, frozenset[str] | None] = {
    "Everything": None,
    "Passed": frozenset({"PASS"}),
    "Failed": frozenset({"FAIL"}),
    "Errors": frozenset({"ERROR"}),
    "Not tested yet": frozenset({"QUEUED", "TESTING"}),
}


def _bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    filled = int(round(width * done / total))
    return "█" * filled + "░" * (width - filled)


class SettingsDialog(tk.Toplevel):
    """Edit ~/.psi/config.json without leaving the app."""

    def __init__(self, parent: "App"):
        super().__init__(parent)
        self.parent = parent
        self.title("Settings")
        self.transient(parent)
        self.resizable(False, False)
        self.result = False

        settings = config.load_settings()
        self.vars: dict[str, tk.Variable] = {}

        pad = {"padx": 10, "pady": 4}
        frame = ttk.Frame(self, padding=14)
        frame.grid(sticky="nsew")

        row = 0
        ttk.Label(frame, text="Credentials", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        row += 1

        for label, key, show in (
            ("PageSpeed API key", "api_key", ""),
            ("GitHub token (optional)", "github_token", "*"),
            ("GitHub repo (owner/repo)", "github_repo", ""),
        ):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", **pad)
            var = tk.StringVar(value=getattr(settings, key) or "")
            entry = ttk.Entry(frame, textvariable=var, width=46, show=show or "")
            entry.grid(row=row, column=1, sticky="ew", **pad)
            self.vars[key] = var
            row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=10
        )
        row += 1
        ttk.Label(frame, text="Engine", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        row += 1

        for label, key in (
            ("Parallel tests", "workers"),
            ("Sites at once", "max_concurrent_runs"),
            ("Attempts before giving up", "max_attempts"),
            ("Max API requests / minute", "rate_limit_per_min"),
        ):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", **pad)
            var = tk.StringVar(value=str(getattr(settings, key)))
            ttk.Spinbox(frame, from_=1, to=999, textvariable=var, width=8).grid(
                row=row, column=1, sticky="w", **pad
            )
            self.vars[key] = var
            row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=10
        )
        row += 1
        ttk.Label(frame, text="Pass criteria", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        ttk.Label(
            frame,
            text="Leave blank to stop enforcing a check.",
            foreground="#6b7387",
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 6))
        row += 1

        grid = ttk.Frame(frame)
        grid.grid(row=row, column=0, columnspan=2, sticky="ew", padx=10)
        thresholds = asdict(settings.thresholds)
        for index, (label, key) in enumerate(
            (
                ("Performance ≥", "performance"),
                ("Accessibility ≥", "accessibility"),
                ("Best Practices ≥", "best_practices"),
                ("SEO ≥", "seo"),
                ("FCP ≤ (s)", "fcp"),
                ("LCP ≤ (s)", "lcp"),
                ("CLS ≤", "cls"),
                ("TBT ≤ (s)", "tbt"),
            )
        ):
            r, c = divmod(index, 2)
            cell = ttk.Frame(grid)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 18), pady=3)
            ttk.Label(cell, text=label, width=16).pack(side="left")
            value = thresholds.get(key)
            var = tk.StringVar(value="" if value is None else str(value))
            ttk.Entry(cell, textvariable=var, width=8).pack(side="left")
            self.vars[f"threshold_{key}"] = var
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=10
        )
        row += 1

        self.vars["insecure"] = tk.BooleanVar(value=settings.insecure)
        ttk.Checkbutton(
            frame,
            text="Skip TLS verification (only if a proxy or antivirus breaks HTTPS)",
            variable=self.vars["insecure"],
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=10)
        row += 1

        ttk.Label(frame, text="CA bundle (.pem)").grid(row=row, column=0, sticky="w", **pad)
        ca_row = ttk.Frame(frame)
        ca_row.grid(row=row, column=1, sticky="ew", **pad)
        self.vars["ca_bundle"] = tk.StringVar(value=settings.ca_bundle or "")
        ttk.Entry(ca_row, textvariable=self.vars["ca_bundle"], width=36).pack(side="left")
        ttk.Button(ca_row, text="…", width=3, command=self._pick_ca).pack(side="left", padx=4)
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="Save", command=self._save).pack(side="right")

        frame.columnconfigure(1, weight=1)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _pick_ca(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Choose a CA bundle",
            filetypes=[("Certificates", "*.pem *.crt *.cer"), ("All files", "*.*")],
        )
        if path:
            self.vars["ca_bundle"].set(path)

    def _save(self) -> None:
        import json

        config.ensure_dirs()
        path = config.config_path()
        try:
            data = json.loads(path.read_text("utf-8")) if path.exists() else {}
        except ValueError:
            data = {}

        for key in ("api_key", "github_token", "github_repo", "ca_bundle"):
            data[key] = self.vars[key].get().strip()
        data["insecure"] = bool(self.vars["insecure"].get())

        for key in ("workers", "max_concurrent_runs", "max_attempts", "rate_limit_per_min"):
            raw = self.vars[key].get().strip()
            if raw.isdigit() and int(raw) > 0:
                data[key] = int(raw)

        thresholds: dict[str, Any] = {}
        for key in (
            "performance", "accessibility", "best_practices", "seo",
            "fcp", "lcp", "cls", "tbt",
        ):
            raw = self.vars[f"threshold_{key}"].get().strip()
            if not raw:
                thresholds[key] = None
                continue
            try:
                thresholds[key] = (
                    int(raw) if key in ("performance", "accessibility", "best_practices", "seo")
                    else float(raw)
                )
            except ValueError:
                messagebox.showerror("Settings", f"{key!r} must be a number.", parent=self)
                return
        data["thresholds"] = thresholds

        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.result = True
        self.parent.reload_settings()
        self.destroy()


class App(tk.Tk):
    def __init__(self, store: Store, settings: config.Settings):
        super().__init__()
        self.store = store
        self.settings = settings
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        # Background threads must never touch a widget directly -- not even
        # via `after()`, which registers a Tcl command and is itself only safe
        # on the UI thread. They post a callable here instead and `_pump`
        # runs it.
        self.ui_calls: queue.Queue[Any] = queue.Queue()
        self.supervisor = Supervisor(store, settings, self.events.put)
        self._selected_run: str | None = None
        self._offline = False
        self._busy = False
        config.apply_tls(settings)  # so constructing App directly always works

        self.title("PageSpeed Analyzer")
        self.minsize(900, 600)
        self._centre(1150, 800)

        self._build_menu()
        self._build_widgets()
        self._apply_style()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(PUMP_MS, self._pump)
        self.after(200, self.refresh_runs)
        self._offer_resume()

    # ------------------------------------------------------------ chrome

    def _centre(self, width: int, height: int) -> None:
        """Open centred, and never taller than the screen it opens on."""
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = min(width, screen_w - 80)
        height = min(height, screen_h - 120)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _apply_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=24)
        style.configure("Heading.TLabel", font=("Segoe UI", 9, "bold"))
        style.configure("Muted.TLabel", foreground="#6b7387")

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Settings…", command=self.open_settings)
        file_menu.add_command(label="Open reports folder", command=self.open_reports_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        run_menu = tk.Menu(menubar, tearoff=0)
        run_menu.add_command(label="Resume all unfinished", command=self.resume_all)
        run_menu.add_command(label="Stop selected run", command=self.stop_selected)
        run_menu.add_command(label="Stop everything", command=self.stop_all)
        menubar.add_cascade(label="Run", menu=run_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

    def _build_widgets(self) -> None:
        # The status bar is packed first. Tk's packer hands space out in the
        # order widgets are added, so an expanding widget added earlier would
        # squeeze this off the bottom of the window entirely.
        self.status_var = tk.StringVar()
        status_bar = ttk.Frame(self, relief="groove", padding=(10, 4))
        status_bar.pack(fill="x", side="bottom")
        ttk.Label(status_bar, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(status_bar, mode="determinate", length=180)
        self.progress.pack(side="right")

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        # -- new run ------------------------------------------------------
        new = ttk.LabelFrame(outer, text="New run", padding=10)
        new.pack(fill="x")

        ttk.Label(new, text="Sitemap URL").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar()
        source = ttk.Entry(new, textvariable=self.source_var)
        source.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(2, 2))
        source.bind("<Return>", lambda _e: self.start_run())
        ttk.Label(
            new,
            text="A sitemap, a sitemap index, or just a domain. Separate several with commas.",
            style="Muted.TLabel",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 8))

        ttk.Label(new, text="Label").grid(row=3, column=0, sticky="w")
        self.label_var = tk.StringVar()
        ttk.Entry(new, textvariable=self.label_var, width=22).grid(
            row=4, column=0, sticky="w", padx=(0, 12)
        )

        ttk.Label(new, text="Devices").grid(row=3, column=1, sticky="w")
        self.strategy_var = tk.StringVar(value="Mobile + Desktop")
        ttk.Combobox(
            new, textvariable=self.strategy_var, width=18, state="readonly",
            values=("Mobile + Desktop", "Mobile only", "Desktop only"),
        ).grid(row=4, column=1, sticky="w", padx=(0, 12))

        ttk.Label(new, text="Max URLs").grid(row=3, column=2, sticky="w")
        self.limit_var = tk.StringVar()
        ttk.Entry(new, textvariable=self.limit_var, width=10).grid(
            row=4, column=2, sticky="w", padx=(0, 12)
        )

        actions = ttk.Frame(new)
        actions.grid(row=4, column=3, sticky="e")
        self.start_button = ttk.Button(actions, text="Start run", command=self.start_run)
        self.start_button.pack(side="left")
        ttk.Button(actions, text="From file…", command=self.start_from_file).pack(
            side="left", padx=(6, 0)
        )
        new.columnconfigure(3, weight=1)

        # -- runs ---------------------------------------------------------
        runs = ttk.LabelFrame(outer, text="Runs", padding=10)
        runs.pack(fill="both", expand=True, pady=(12, 0))

        columns = ("id", "label", "status", "bar", "progress", "pass", "fail", "err", "created")
        self.runs_tree = ttk.Treeview(runs, columns=columns, show="headings", height=8)
        headings = {
            "id": ("Run", 80), "label": ("Site", 200), "status": ("Status", 80),
            "bar": ("", 100), "progress": ("Progress", 90), "pass": ("Pass", 55),
            "fail": ("Fail", 55), "err": ("Errors", 60), "created": ("Started", 130),
        }
        for key, (text, width) in headings.items():
            self.runs_tree.heading(key, text=text)
            anchor = "w" if key in ("id", "label", "created") else "center"
            self.runs_tree.column(key, width=width, anchor=anchor, stretch=(key == "label"))

        scroll = ttk.Scrollbar(runs, orient="vertical", command=self.runs_tree.yview)
        self.runs_tree.configure(yscrollcommand=scroll.set)
        self.runs_tree.pack(side="top", fill="both", expand=True)
        scroll.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")

        for status, colour in STATUS_COLOURS.items():
            self.runs_tree.tag_configure(status, foreground=colour)

        self.runs_tree.bind("<<TreeviewSelect>>", self._on_select_run)
        self.runs_tree.bind("<Double-1>", lambda _e: self.open_report())

        row_actions = ttk.Frame(runs)
        row_actions.pack(fill="x", pady=(8, 0))
        self.buttons: dict[str, ttk.Button] = {}
        for key, text, command in (
            ("resume", "Resume", self.resume_selected),
            ("stop", "Stop", self.stop_selected),
            ("retry", "Retry failed", self.retry_selected),
            ("report", "Open report", self.open_report),
            ("save", "Save report…", self.save_report),
            ("publish", "Publish to GitHub", self.publish_report),
            ("delete", "Delete", self.delete_selected),
        ):
            button = ttk.Button(row_actions, text=text, command=command, state="disabled")
            button.pack(side="left", padx=(0, 6))
            self.buttons[key] = button

        # -- details ------------------------------------------------------
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True, pady=(12, 0))

        results_tab = ttk.Frame(notebook, padding=6)
        notebook.add(results_tab, text="Results")

        # Packed before the tree so the packer reserves their space first.
        filters = ttk.Frame(results_tab)
        filters.pack(fill="x", pady=(0, 6))
        ttk.Label(filters, text="Show").pack(side="left")
        self.filter_var = tk.StringVar(value=next(iter(RESULT_FILTERS)))
        filter_box = ttk.Combobox(
            filters, textvariable=self.filter_var, width=16, state="readonly",
            values=tuple(RESULT_FILTERS),
        )
        filter_box.pack(side="left", padx=6)
        filter_box.bind("<<ComboboxSelected>>", lambda _e: self._render_results())
        self.results_summary_var = tk.StringVar()
        ttk.Label(
            filters, textvariable=self.results_summary_var, style="Muted.TLabel"
        ).pack(side="left", padx=8)

        result_actions = ttk.Frame(results_tab)
        result_actions.pack(fill="x", side="bottom", pady=(6, 0))
        self.retry_selected_button = ttk.Button(
            result_actions, text="Retry selected", command=self.retry_selected_results
        )
        self.retry_selected_button.pack(side="left")
        ttk.Button(
            result_actions, text="Open in PageSpeed", command=self._open_psi_link
        ).pack(side="left", padx=6)
        ttk.Button(result_actions, text="Copy URL", command=self._copy_result_url).pack(
            side="left"
        )
        ttk.Label(
            result_actions,
            text="Select one or more rows to re-test just those. Double-click opens the URL.",
            style="Muted.TLabel",
        ).pack(side="left", padx=10)

        body = ttk.Frame(results_tab)
        body.pack(fill="both", expand=True)

        result_cols = ("url", "device", "perf", "a11y", "bp", "seo", "fcp", "lcp", "cls", "status")
        self.results_tree = ttk.Treeview(
            body, columns=result_cols, show="headings", height=9, selectmode="extended"
        )
        result_headings = {
            "url": ("URL", 380), "device": ("Device", 70), "perf": ("Perf", 55),
            "a11y": ("A11y", 55), "bp": ("BP", 55), "seo": ("SEO", 55),
            "fcp": ("FCP", 60), "lcp": ("LCP", 60), "cls": ("CLS", 60),
            "status": ("Status", 70),
        }
        for key, (text, width) in result_headings.items():
            self.results_tree.heading(
                key, text=text, command=lambda k=key: self._sort_results(k)
            )
            self.results_tree.column(
                key, width=width, anchor="w" if key == "url" else "center",
                stretch=(key == "url"),
            )
        results_scroll = ttk.Scrollbar(
            body, orient="vertical", command=self.results_tree.yview
        )
        self.results_tree.configure(yscrollcommand=results_scroll.set)
        results_scroll.pack(side="right", fill="y")
        self.results_tree.pack(side="left", fill="both", expand=True)

        self.results_tree.tag_configure("PASS", foreground="#14532d")
        self.results_tree.tag_configure("FAIL", foreground="#b91c1c")
        self.results_tree.tag_configure("ERROR", foreground="#b45309")
        self.results_tree.tag_configure("QUEUED", foreground="#6b7387")
        self.results_tree.tag_configure("TESTING", foreground="#1e40af")
        self.results_tree.bind("<Double-1>", self._open_psi_link)

        self.results_menu = tk.Menu(self, tearoff=0)
        self.results_menu.add_command(
            label="Retry this test", command=self.retry_selected_results
        )
        self.results_menu.add_command(
            label="Open in PageSpeed Insights", command=self._open_psi_link
        )
        self.results_menu.add_command(label="Copy URL", command=self._copy_result_url)
        self.results_menu.add_separator()
        self.results_menu.add_command(label="Details…", command=self._show_result_details)
        self.results_tree.bind("<Button-3>", self._popup_results_menu)

        self._results_sort = ("url", False)
        self._results_rows: list[dict[str, Any]] = []

        log_tab = ttk.Frame(notebook, padding=6)
        notebook.add(log_tab, text="Activity")
        self.log = tk.Text(log_tab, height=9, wrap="none", state="disabled",
                           font=("Consolas", 9), relief="flat", background="#f6f7fb")
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("pass", foreground="#14532d")
        self.log.tag_configure("fail", foreground="#b91c1c")
        self.log.tag_configure("warn", foreground="#b45309")
        self.log.tag_configure("info", foreground="#374151")

        self._update_status()

    # ------------------------------------------------------- event pump

    def call_on_ui(self, func) -> None:
        """Schedule `func` to run on the UI thread. Safe from any thread."""
        self.ui_calls.put(func)

    def _pump(self) -> None:
        """Drain engine events and cross-thread callbacks on the UI thread."""
        dirty = False
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            dirty = self._handle_event(event) or dirty

        while True:
            try:
                func = self.ui_calls.get_nowait()
            except queue.Empty:
                break
            try:
                func()
            except Exception as exc:  # noqa: BLE001 - a callback must not kill the pump
                self._log(f"Internal error: {exc}", "warn")

        if dirty:
            self._update_status()
        self.after(PUMP_MS, self._pump)

    def _handle_event(self, event: dict[str, Any]) -> bool:
        kind = event.get("type")
        run_id = event.get("run_id", "")

        if kind == "offline":
            self._offline = True
            self._log("Internet connection lost — waiting. Work will resume by itself.", "warn")
            return True
        if kind == "online":
            self._offline = False
            self._log("Connection restored — resuming.", "info")
            return True
        if kind == "run_start":
            recovered = event.get("recovered") or 0
            extra = f", {recovered} recovered from an interrupted run" if recovered else ""
            counts = event.get("counts", {})
            self._log(
                f"{run_id} {event.get('label')}: {counts.get('total', 0)} tests"
                f" ({counts.get(DONE, 0)} already done{extra})",
                "info",
            )
            return True
        if kind == "task_done":
            status = event.get("status", "")
            self._log(
                f"  {'✓' if status == 'PASS' else '✗'} [{event.get('strategy')}] "
                f"{event.get('url')} — {status}",
                "pass" if status == "PASS" else "fail",
            )
            return True
        if kind == "task_retry":
            self._log(
                f"  ↻ retry {event.get('attempt')}/{event.get('max_attempts')} "
                f"[{event.get('strategy')}] {event.get('url')} — {event.get('reason')}",
                "warn",
            )
            return False
        if kind == "task_deferred":
            self._log(f"  … deferred {event.get('url')} — {event.get('reason')}", "warn")
            return False
        if kind == "task_failed":
            self._log(
                f"  ✗ gave up [{event.get('strategy')}] {event.get('url')} "
                f"after {event.get('attempts')} attempts — {event.get('reason')}",
                "fail",
            )
            return True
        if kind == "run_finish":
            counts = event.get("counts", {})
            if event.get("status") == "done":
                verb = "complete"
            else:
                verb = "stopped" if event.get("stopped") else "paused"
            self._log(
                f"{run_id} {event.get('label')}: {verb} — {counts.get(DONE, 0)} done, "
                f"{counts.get(FAILED, 0)} failed, {counts.get(PENDING, 0)} remaining",
                "info",
            )
            return True
        if kind in ("busy", "error"):
            self._log(f"{run_id}: {event.get('message')}", "warn")
            return True
        return False

    def _log(self, message: str, tag: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{stamp}  {message}\n", tag)
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------------------------------------------------- refresh

    def refresh_runs(self) -> None:
        try:
            runs = self.store.list_runs(limit=100)
        except Exception as exc:  # noqa: BLE001 - never let a refresh kill the UI
            self._log(f"Could not read runs: {exc}", "warn")
            self.after(REFRESH_MS, self.refresh_runs)
            return

        selected = self._selected_run
        existing = set(self.runs_tree.get_children())
        seen = set()

        for run in runs:
            run_id = run["id"]
            seen.add(run_id)
            counts = self.store.progress(run_id)
            summary = self.store.result_summary(run_id)
            finished = counts[DONE] + counts[FAILED]
            status = self._run_status(run)
            values = (
                run_id,
                run["label"],
                status,
                _bar(finished, counts["total"]),
                f"{finished}/{counts['total']}",
                summary.get("PASS", 0),
                summary.get("FAIL", 0),
                counts[FAILED],
                (run["created_at"] or "")[:16].replace("T", " "),
            )
            if run_id in existing:
                self.runs_tree.item(run_id, values=values, tags=(status,))
            else:
                self.runs_tree.insert("", "end", iid=run_id, values=values, tags=(status,))

        for stale in existing - seen:
            self.runs_tree.delete(stale)

        if selected and selected in seen:
            if not self.runs_tree.selection():
                self.runs_tree.selection_set(selected)
            self._refresh_results(selected)
        elif not selected and runs:
            # Open on the newest run so the Results tab is never blank.
            newest = runs[0]["id"]
            self.runs_tree.selection_set(newest)
            self._selected_run = newest
            self._refresh_results(newest)

        self._sync_buttons()
        self._update_status()
        self.after(REFRESH_MS, self.refresh_runs)

    def _on_select_run(self, _event=None) -> None:
        selection = self.runs_tree.selection()
        self._selected_run = selection[0] if selection else None
        if self._selected_run:
            self._refresh_results(self._selected_run)
        else:
            self.results_tree.delete(*self.results_tree.get_children())
        self._sync_buttons()

    def _refresh_results(self, run_id: str) -> None:
        """Every unit of work in the run: finished, failed and still to come.

        Each row carries its task id so a single test can be re-queued on its
        own, without disturbing the other ninety-nine.
        """
        rows: list[dict[str, Any]] = []
        blank = {
            "perf": None, "a11y": None, "bp": None, "seo": None,
            "fcp": None, "lcp": None, "cls": None,
        }
        for row in self.store.results(run_id):
            rows.append(
                {
                    "task_id": row["task_id"], "url": row["url"],
                    "device": row["strategy"],
                    "perf": row["performance"], "a11y": row["accessibility"],
                    "bp": row["best_practices"], "seo": row["seo"],
                    "fcp": row["fcp"], "lcp": row["lcp"], "cls": row["cls"],
                    "status": row["status"], "attempts": row["attempts"], "error": None,
                }
            )
        for row in self.store.failed_tasks(run_id):
            rows.append(
                {
                    "task_id": row["id"], "url": row["url"], "device": row["strategy"],
                    **blank, "status": "ERROR", "attempts": row["attempts"],
                    "error": row["last_error"],
                }
            )
        for row in self.store.pending_tasks(run_id):
            rows.append(
                {
                    "task_id": row["id"], "url": row["url"], "device": row["strategy"],
                    **blank,
                    "status": "TESTING" if row["state"] == RUNNING else "QUEUED",
                    "attempts": row["attempts"], "error": row["last_error"],
                }
            )
        # Refreshes come round every second. Re-rendering an unchanged table
        # would fight the user for the scrollbar and the selection.
        if rows == self._results_rows and self.results_tree.get_children():
            return
        self._results_rows = rows
        self._render_results()

    def _render_results(self) -> None:
        key, reverse = self._results_sort

        def sort_key(row):
            value = row.get(key)
            if value is None:
                return (1, "")
            return (0, value if isinstance(value, str) else float(value))

        wanted = RESULT_FILTERS.get(self.filter_var.get())
        rows = sorted(self._results_rows, key=sort_key, reverse=reverse)
        visible = [r for r in rows if wanted is None or r["status"] in wanted]

        selected = set(self.results_tree.selection())
        offset = self.results_tree.yview()[0]
        self.results_tree.delete(*self.results_tree.get_children())
        for row in visible:
            def show(value, suffix=""):
                return "—" if value is None else f"{value}{suffix}"

            self.results_tree.insert(
                "", "end", iid=str(row["task_id"]),
                values=(
                    row["url"], row["device"], show(row["perf"]), show(row["a11y"]),
                    show(row["bp"]), show(row["seo"]), show(row["fcp"], "s"),
                    show(row["lcp"], "s"), show(row["cls"]), row["status"],
                ),
                tags=(row["status"],),
            )

        # A refresh runs every second; losing the selection or the scroll
        # position mid-click would make the row actions unusable.
        keep = [iid for iid in selected if self.results_tree.exists(iid)]
        if keep:
            self.results_tree.selection_set(keep)
        if offset:
            self.results_tree.yview_moveto(offset)

        tally = {}
        for row in self._results_rows:
            tally[row["status"]] = tally.get(row["status"], 0) + 1
        parts = [f"{len(self._results_rows)} tests"]
        for status, label in (
            ("PASS", "pass"), ("FAIL", "fail"), ("ERROR", "errors"),
            ("TESTING", "testing"), ("QUEUED", "queued"),
        ):
            if tally.get(status):
                parts.append(f"{tally[status]} {label}")
        if wanted is not None:
            parts.append(f"showing {len(visible)}")
        self.results_summary_var.set("  ·  ".join(parts))

    def _sort_results(self, key: str) -> None:
        current_key, reverse = self._results_sort
        self._results_sort = (key, not reverse if key == current_key else False)
        self._render_results()

    def _selected_result_rows(self) -> list[dict[str, Any]]:
        ids = {int(iid) for iid in self.results_tree.selection()}
        return [row for row in self._results_rows if row["task_id"] in ids]

    def _popup_results_menu(self, event) -> None:
        row_id = self.results_tree.identify_row(event.y)
        if row_id and row_id not in self.results_tree.selection():
            self.results_tree.selection_set(row_id)
        if not self.results_tree.selection():
            return
        self.results_menu.tk_popup(event.x_root, event.y_root)
        self.results_menu.grab_release()

    def _open_psi_link(self, _event=None) -> None:
        selection = self.results_tree.selection()
        if not selection:
            return
        values = self.results_tree.item(selection[0], "values")
        if len(values) >= 2:
            from .client import psi_web_link

            webbrowser.open(psi_web_link(values[0], values[1]))

    def _copy_result_url(self) -> None:
        rows = self._selected_result_rows()
        if not rows:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(dict.fromkeys(row["url"] for row in rows)))

    def _show_result_details(self) -> None:
        rows = self._selected_result_rows()
        if not rows:
            return
        row = rows[0]
        lines = [
            row["url"],
            f"Device: {row['device']}",
            f"Status: {row['status']}",
            f"Attempts: {row['attempts']}",
        ]
        if row["error"]:
            lines += ["", f"Last error: {row['error']}"]
        messagebox.showinfo("Test details", "\n".join(lines))

    def _sync_buttons(self) -> None:
        run_id = self._selected_run
        if not run_id:
            for button in self.buttons.values():
                button.configure(state="disabled")
            self.retry_selected_button.configure(state="disabled")
            return
        counts = self.store.progress(run_id)
        run = self.store.get_run(run_id)
        if run is None:
            return
        status = self._run_status(run)
        busy = status in ("running", "stopping")
        live_here = self.supervisor.is_running(run_id)
        stopping = self.supervisor.is_stopping(run_id)
        has_work = counts[PENDING] > 0 or counts["running"] > 0
        has_results = counts[DONE] > 0

        self.buttons["resume"].configure(
            state="normal" if has_work and not busy else "disabled"
        )
        self.buttons["stop"].configure(
            state="normal" if live_here and not stopping else "disabled"
        )
        self.buttons["retry"].configure(
            state="normal" if counts[FAILED] > 0 and not stopping else "disabled"
        )
        for key in ("report", "save", "publish"):
            self.buttons[key].configure(state="normal" if has_results else "disabled")
        self.buttons["delete"].configure(state="disabled" if busy else "normal")
        self.retry_selected_button.configure(state="disabled" if stopping else "normal")

    def _run_status(self, run) -> str:
        """Store status, with the winding-down state only this window knows."""
        status = self.store.display_status(run)
        if status == "running" and self.supervisor.is_stopping(run["id"]):
            return "stopping"
        return status

    def _update_status(self) -> None:
        try:
            runs = self.store.list_runs(limit=100)
            statuses = {r["id"]: self._run_status(r) for r in runs}
            active = [r for r in runs if statuses[r["id"]] in ("running", "stopping")]
            winding_down = sum(1 for r in active if statuses[r["id"]] == "stopping")
            pending_total = sum(self.store.progress(r["id"])[PENDING] for r in active)
            done_total = sum(
                self.store.progress(r["id"])[DONE] + self.store.progress(r["id"])[FAILED]
                for r in active
            )
        except Exception:  # noqa: BLE001
            return

        bits = []
        if self._offline:
            bits.append("OFFLINE — waiting for the connection")
        if active:
            note = f" ({winding_down} stopping)" if winding_down else ""
            bits.append(
                f"{len(active)} run(s) active{note} · {pending_total} tests to go"
            )
            total = done_total + pending_total
            self.progress.configure(
                maximum=max(total, 1), value=done_total, mode="determinate"
            )
        else:
            bits.append("Idle")
            self.progress.configure(value=0)
        bits.append(f"{self.settings.workers} parallel")
        bits.append("API key set" if self.settings.api_key else "NO API KEY — set one in Settings")
        self.status_var.set("   ·   ".join(bits))

    # ---------------------------------------------------------- actions

    def _selected_or_warn(self) -> str | None:
        if not self._selected_run:
            messagebox.showinfo("No run selected", "Pick a run from the list first.")
            return None
        return self._selected_run

    def start_run(self) -> None:
        source = self.source_var.get().strip()
        if not source:
            messagebox.showinfo("Nothing to analyse", "Enter a sitemap URL first.")
            return
        if not self.settings.api_key:
            proceed = messagebox.askokcancel(
                "No API key",
                "No PageSpeed API key is set, so Google will throttle requests hard "
                "and most tests will fail.\n\nStart anyway?",
            )
            if not proceed:
                self.open_settings()
                return

        sources = [s.strip() for s in source.split(",") if s.strip()]
        limit_raw = self.limit_var.get().strip()
        limit = int(limit_raw) if limit_raw.isdigit() else None
        strategies = {
            "Mobile + Desktop": config.ALL_STRATEGIES,
            "Mobile only": ("mobile",),
            "Desktop only": ("desktop",),
        }[self.strategy_var.get()]
        label = self.label_var.get().strip()

        self._set_busy(True, "Reading sitemap…")
        threading.Thread(
            target=self._create_runs_worker,
            args=(sources, label, strategies, limit),
            daemon=True,
            name="psi-gui-create",
        ).start()

    def _create_runs_worker(self, sources, label, strategies, limit) -> None:
        """Sitemap fetching blocks, so it must stay off the UI thread."""
        created, problems = [], []
        settings = replace(self.settings, strategies=strategies)

        try:
            for source in sources:
                target = (
                    source if source.startswith(("http://", "https://"))
                    else "https://" + source
                )
                try:
                    found = sitemap.collect_urls(
                        target, timeout=settings.request_timeout, limit=limit
                    )
                except (FetchError, ValueError, OSError) as exc:
                    problems.append(f"{target}: {exc}")
                    continue
                if not found.urls:
                    problems.append(f"{target}: no URLs found")
                    continue

                run_label = label or sitemap.label_for(target)
                if label and len(sources) > 1:
                    run_label = f"{label} ({sitemap.label_for(target)})"
                run_id = self.store.create_run(
                    label=run_label, source=target, source_kind="sitemap",
                    config_json=settings.to_json(), urls=found.urls, strategies=strategies,
                )
                created.append((run_id, run_label, len(found.urls)))
        except Exception as exc:  # noqa: BLE001 - must always hand control back
            problems.append(f"Unexpected error: {exc}")

        self.call_on_ui(lambda: self._runs_created(created, problems, settings))

    def _runs_created(self, created, problems, settings) -> None:
        self._set_busy(False)
        for message in problems:
            self._log(message, "warn")
        if not created:
            messagebox.showerror(
                "Could not start",
                "\n\n".join(problems) or "No URLs were found in that sitemap.",
            )
            return
        for run_id, label, count in created:
            self._log(f"Created {run_id} — {label}: {count} URLs", "info")
            self.supervisor.start(run_id, settings=settings)
        self.source_var.set("")
        self.label_var.set("")
        self.refresh_runs()

    def start_from_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a file of URLs or sitemaps (one per line)",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            lines = [
                line.strip()
                for line in Path(path).read_text("utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        except OSError as exc:
            messagebox.showerror("Could not read file", str(exc))
            return
        if not lines:
            messagebox.showinfo("Empty file", "That file has no URLs in it.")
            return

        urls = [l for l in lines if l.startswith(("http://", "https://"))]
        sitemaps = [l for l in urls if l.endswith((".xml", ".xml.gz"))]

        if sitemaps and len(sitemaps) == len(urls):
            # A list of sitemaps: one run each.
            self.source_var.set(", ".join(sitemaps))
            self.start_run()
            return

        # A plain list of pages: one run over all of them.
        strategies = {
            "Mobile + Desktop": config.ALL_STRATEGIES,
            "Mobile only": ("mobile",),
            "Desktop only": ("desktop",),
        }[self.strategy_var.get()]
        settings = replace(self.settings, strategies=strategies)
        label = self.label_var.get().strip() or (
            sitemap.label_for(urls[0]) if urls else Path(path).stem
        )
        run_id = self.store.create_run(
            label=label, source=path, source_kind="urls",
            config_json=settings.to_json(), urls=urls, strategies=strategies,
        )
        self._log(f"Created {run_id} — {label}: {len(urls)} URLs from {Path(path).name}", "info")
        self.supervisor.start(run_id, settings=settings)
        self.refresh_runs()

    def resume_selected(self) -> None:
        run_id = self._selected_or_warn()
        if run_id and not self.supervisor.start(run_id):
            messagebox.showwarning(
                "Already running", "Another process is working on this run."
            )
        self.refresh_runs()

    def resume_all(self) -> None:
        runs = self.store.resumable_runs()
        if not runs:
            messagebox.showinfo("Nothing to resume", "Every run is complete.")
            return
        for run in runs:
            self.supervisor.start(run["id"])
        self.refresh_runs()

    def retry_selected(self) -> None:
        run_id = self._selected_or_warn()
        if not run_id:
            return
        count = self.store.requeue_failed(run_id)
        self._log(f"{run_id}: re-queued {count} failed test(s)", "info")
        if count:
            self._start_or_explain(run_id)
        self.refresh_runs()

    def retry_selected_results(self) -> None:
        """Re-test just the rows picked out in the Results table."""
        run_id = self._selected_run
        if not run_id:
            messagebox.showinfo("No run selected", "Pick a run from the list first.")
            return
        rows = self._selected_result_rows()
        if not rows:
            messagebox.showinfo(
                "Nothing selected",
                "Select one or more rows in the Results table first.\n\n"
                "Ctrl+click or Shift+click to pick several.",
            )
            return

        count = self.store.requeue_tasks([row["task_id"] for row in rows])
        if not count:
            messagebox.showinfo(
                "Already testing",
                "Those tests are in flight right now. Wait for them to land, or "
                "stop the run first, then retry.",
            )
            return

        skipped = len(rows) - count
        note = f" ({skipped} already in flight, left alone)" if skipped else ""
        self._log(f"{run_id}: re-queued {count} test(s){note}", "info")
        if count == 1:
            self._log(f"  ↻ {rows[0]['device']} {rows[0]['url']}", "info")
        self._start_or_explain(run_id)
        self._refresh_results(run_id)
        self.refresh_runs()

    def _start_or_explain(self, run_id: str) -> None:
        """Get work moving again, or say why it will not move yet."""
        if self.supervisor.is_stopping(run_id):
            self._log(
                f"{run_id}: still stopping — press Resume once it has settled.", "warn"
            )
            return
        self.supervisor.start(run_id)

    def stop_selected(self) -> None:
        run_id = self._selected_or_warn()
        if not run_id:
            return
        if not self.supervisor.stop_run(run_id):
            messagebox.showinfo(
                "Not running here",
                "This run is not being driven by this window, so there is "
                "nothing to stop.",
            )
            return
        self._log(
            f"{run_id}: stopping — no new tests will start. Progress is saved, "
            "so Resume picks up from here.",
            "warn",
        )
        self._sync_buttons()

    def stop_all(self) -> None:
        stopped = self.supervisor.stop_all_runs()
        if not stopped:
            self._log("Nothing is running.", "info")
            return
        self._log(
            f"Stopping {stopped} run(s) — no new tests will start. "
            "Progress is saved; use Resume to continue.",
            "warn",
        )
        self._sync_buttons()

    def open_report(self) -> None:
        run_id = self._selected_or_warn()
        if not run_id:
            return
        try:
            written = report.write_report(
                self.store, run_id, out_dir=config.reports_dir(), formats=("html",)
            )
        except (KeyError, OSError) as exc:
            messagebox.showerror("Report failed", str(exc))
            return
        path = written["html"]
        self._log(f"Report written: {path}", "info")
        webbrowser.open(path.resolve().as_uri())

    def save_report(self) -> None:
        run_id = self._selected_or_warn()
        if not run_id:
            return
        path = filedialog.asksaveasfilename(
            title="Save report",
            defaultextension=".html",
            initialfile=f"pagespeed-{run_id}.html",
            filetypes=[("HTML report", "*.html"), ("CSV", "*.csv"), ("JSON", "*.json")],
        )
        if not path:
            return
        target = Path(path)
        fmt = target.suffix.lstrip(".").lower()
        if fmt not in ("html", "csv", "json"):
            fmt = "html"
        try:
            data = report.gather(self.store, run_id)
            renderer = {"html": report.to_html, "csv": report.to_csv, "json": report.to_json}[fmt]
            target.write_text(renderer(data), encoding="utf-8")
        except (KeyError, OSError) as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self._log(f"Saved {target}", "info")

    def publish_report(self) -> None:
        run_id = self._selected_or_warn()
        if not run_id:
            return
        if not (self.settings.github_token and self.settings.github_repo):
            messagebox.showinfo(
                "GitHub not configured",
                "Add a GitHub token and repository in Settings first.",
            )
            return
        self._set_busy(True, "Uploading…")
        threading.Thread(
            target=self._publish_worker, args=(run_id,), daemon=True, name="psi-gui-publish"
        ).start()

    def _publish_worker(self, run_id: str) -> None:
        from .publish import upload_report

        try:
            written = report.write_report(
                self.store, run_id, out_dir=config.reports_dir(), formats=("html",)
            )
            links = upload_report(
                written["html"],
                token=self.settings.github_token or "",
                repo=self.settings.github_repo or "",
            )
        except (FetchError, ValueError, OSError, KeyError) as exc:
            message = str(exc)
            self.call_on_ui(lambda: self._publish_done(None, message))
            return
        self.call_on_ui(lambda: self._publish_done(links, None))

    def _publish_done(self, links: dict[str, str] | None, error: str | None) -> None:
        self._set_busy(False)
        if error or not links:
            messagebox.showerror("Upload failed", error or "Unknown error")
            return
        preview = links["preview"]
        self._log(f"Published: {preview}", "info")
        self.clipboard_clear()
        self.clipboard_append(preview)
        if messagebox.askyesno(
            "Report published",
            f"The shareable link has been copied to your clipboard:\n\n{preview}\n\nOpen it now?",
        ):
            webbrowser.open(preview)

    def delete_selected(self) -> None:
        run_id = self._selected_or_warn()
        if not run_id:
            return
        if not messagebox.askyesno(
            "Delete run", "Delete this run and all of its results? This cannot be undone."
        ):
            return
        self.store.delete_run(run_id)
        self._selected_run = None
        self.results_tree.delete(*self.results_tree.get_children())
        self.refresh_runs()

    def open_reports_folder(self) -> None:
        folder = config.reports_dir()
        folder.mkdir(parents=True, exist_ok=True)
        webbrowser.open(folder.resolve().as_uri())

    def open_settings(self) -> None:
        dialog = SettingsDialog(self)
        self.wait_window(dialog)

    def reload_settings(self) -> None:
        self.settings = config.load_settings()
        config.apply_tls(self.settings)
        self.supervisor.settings = self.settings
        self.supervisor.limiter.__init__(self.settings.rate_limit_per_min)
        self._log("Settings saved.", "info")
        self._update_status()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.start_button.configure(state="disabled" if busy else "normal")
        if busy:
            self.status_var.set(message)
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self._update_status()

    def _about(self) -> None:
        from . import __version__

        messagebox.showinfo(
            "About",
            f"PageSpeed Analyzer {__version__}\n\n"
            "Runs Google PageSpeed Insights over every URL in a sitemap.\n\n"
            "Runs are saved as they go, so a crash, reboot or dropped\n"
            "connection never loses progress — reopen and press Resume.\n\n"
            f"Data: {config.app_dir()}",
        )

    def _offer_resume(self) -> None:
        runs = self.store.resumable_runs()
        if not runs:
            return
        self._log(f"{len(runs)} run(s) have unfinished work.", "warn")
        if messagebox.askyesno(
            "Unfinished work",
            f"{len(runs)} run(s) were interrupted and still have tests outstanding.\n\n"
            "Resume them now?",
        ):
            self.resume_all()

    def _on_close(self) -> None:
        active = [r for r in self.store.list_runs(limit=100)
                  if self.store.display_status(r) == "running"]
        if active and not messagebox.askokcancel(
            "Quit",
            f"{len(active)} run(s) are still going.\n\n"
            "Progress is saved, so you can pick up where you left off next time.\n\nQuit now?",
        ):
            return
        self.supervisor.stop(hard=True)
        try:
            self.supervisor.shutdown()
        except Exception:  # noqa: BLE001 - we are exiting regardless
            pass
        self.store.close()
        self.destroy()


def launch(store: Store | None = None, settings: config.Settings | None = None) -> None:
    config.ensure_dirs()
    settings = settings or config.load_settings()
    config.apply_tls(settings)
    store = store or Store(config.db_path())
    App(store, settings).mainloop()

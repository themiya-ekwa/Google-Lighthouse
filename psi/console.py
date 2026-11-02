"""Live terminal progress display.

Falls back to plain line-by-line logging when stdout is not a terminal (piped
output, CI, Task Scheduler), and to ASCII glyphs when the console encoding
cannot represent block characters -- which is the default on Windows.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

from .db import DONE, FAILED, PENDING, Store


def _supports_unicode() -> bool:
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    return encoding in ("utf8", "utf16", "utf32", "cp65001")


def _enable_ansi() -> bool:
    """Turn on VT sequences on Windows consoles; no-op elsewhere."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001 - cosmetic only
        return False


class ConsoleReporter:
    """Engine listener that renders progress for one or more runs."""

    def __init__(self, store: Store, *, live: bool | None = None, verbose: bool = False):
        self.store = store
        self.verbose = verbose
        self.unicode = _supports_unicode()
        interactive = sys.stdout.isatty()
        self.live = interactive and _enable_ansi() if live is None else live
        self._runs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._painted = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offline = False
        self._started = time.monotonic()

        if self.unicode:
            self.fill, self.empty = "█", "░"
            self.ok, self.bad, self.warn = "✓", "✗", "⟳"
        else:
            self.fill, self.empty = "#", "-"
            self.ok, self.bad, self.warn = "+", "x", "~"

    # -- listener -----------------------------------------------------------

    def __call__(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        run_id = event.get("run_id")

        if kind == "offline":
            self._offline = True
            self._log("! Internet connection lost — pausing. Will resume automatically.")
            return
        if kind == "online":
            self._offline = False
            self._log("* Connection restored — resuming.")
            return

        if kind == "run_start" and run_id:
            with self._lock:
                if run_id not in self._runs:
                    self._order.append(run_id)
                self._runs[run_id] = {
                    "label": event.get("label", run_id),
                    "counts": event.get("counts", {}),
                    "note": "",
                    "done": False,
                }
            counts = event.get("counts", {})
            recovered = event.get("recovered", 0)
            extra = f", {recovered} recovered from an interrupted run" if recovered else ""
            self._log(
                f"> {run_id} {event.get('label')}: {counts.get('total', 0)} tests "
                f"({counts.get(DONE, 0)} already done{extra})"
            )
            return

        if kind in ("task_done", "task_failed") and run_id:
            with self._lock:
                entry = self._runs.setdefault(run_id, {"label": run_id, "note": ""})
                entry["counts"] = event.get("counts", entry.get("counts", {}))
                entry["note"] = ""
            if self.verbose or not self.live:
                mark = self.ok if kind == "task_done" else self.bad
                status = event.get("status") or event.get("reason", "")
                self._log(f"  {mark} [{event.get('strategy')}] {event.get('url')} {status}")
            return

        if kind == "task_retry" and run_id:
            with self._lock:
                entry = self._runs.setdefault(run_id, {"label": run_id})
                entry["note"] = (
                    f"retry {event.get('attempt')}/{event.get('max_attempts')} "
                    f"in {event.get('delay')}s"
                )
            if self.verbose or not self.live:
                self._log(
                    f"  {self.warn} retry {event.get('attempt')}/{event.get('max_attempts')} "
                    f"[{event.get('strategy')}] {event.get('url')}: {event.get('reason')}"
                )
            return

        if kind == "task_deferred" and run_id:
            with self._lock:
                self._runs.setdefault(run_id, {"label": run_id})["note"] = "waiting"
            if self.verbose or not self.live:
                self._log(f"  {self.warn} deferred {event.get('url')}: {event.get('reason')}")
            return

        if kind == "waiting" and run_id:
            with self._lock:
                self._runs.setdefault(run_id, {"label": run_id})["note"] = (
                    f"backing off {event.get('seconds')}s"
                )
            return

        if kind == "busy":
            self._log(f"! {run_id}: {event.get('message')}")
            return

        if kind == "error":
            self._log(f"! {run_id}: {event.get('message')}")
            return

        if kind == "run_finish" and run_id:
            counts = event.get("counts", {})
            with self._lock:
                entry = self._runs.setdefault(run_id, {"label": run_id})
                entry["counts"] = counts
                entry["done"] = True
                entry["note"] = event.get("status", "")
            verb = "complete" if event.get("status") == "done" else "paused"
            self._log(
                f"< {run_id} {entry.get('label')}: {verb} — "
                f"{counts.get(DONE, 0)} done, {counts.get(FAILED, 0)} failed, "
                f"{counts.get(PENDING, 0)} remaining"
            )

    # -- rendering ----------------------------------------------------------

    def start(self) -> None:
        if not self.live or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="psi-console", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.live:
            self._clear()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._paint()
            self._stop.wait(0.4)

    def _log(self, message: str) -> None:
        with self._lock:
            if self.live:
                self._clear_locked()
            sys.stdout.write(message + "\n")
            sys.stdout.flush()

    def _clear(self) -> None:
        with self._lock:
            self._clear_locked()

    def _clear_locked(self) -> None:
        if self._painted:
            sys.stdout.write(f"\033[{self._painted}F\033[0J")
            self._painted = 0

    def _bar(self, done: int, total: int, width: int = 26) -> str:
        if total <= 0:
            return self.empty * width
        filled = int(width * done / total)
        return self.fill * filled + self.empty * (width - filled)

    def _eta(self, done: int, total: int) -> str:
        if done <= 0 or done >= total:
            return ""
        elapsed = time.monotonic() - self._started
        remaining = elapsed / done * (total - done)
        minutes, seconds = divmod(int(remaining), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"eta {hours}h{minutes:02d}m"
        if minutes:
            return f"eta {minutes}m{seconds:02d}s"
        return f"eta {seconds}s"

    def _paint(self) -> None:
        with self._lock:
            self._clear_locked()
            lines: list[str] = []
            if self._offline:
                lines.append("  offline — waiting for the connection to come back")

            for run_id in self._order:
                entry = self._runs.get(run_id, {})
                counts = entry.get("counts") or {}
                total = counts.get("total", 0)
                done = counts.get(DONE, 0)
                failed = counts.get(FAILED, 0)
                finished = done + failed
                label = str(entry.get("label", run_id))[:22].ljust(22)
                note = entry.get("note") or self._eta(finished, total)
                lines.append(
                    f"  {run_id}  {label} [{self._bar(finished, total)}] "
                    f"{finished:>4}/{total:<4} {self.ok}{done} {self.bad}{failed}  {note}"
                )

            if not lines:
                return
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
            self._painted = len(lines)

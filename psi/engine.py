"""The execution engine.

A `Supervisor` drives one or more runs to completion at the same time. Each run
gets a driver thread that claims small batches of work; every claimed task is
executed on a shared, rate-limited thread pool, so several sites can progress
concurrently without their combined request rate breaching the API quota.

Three behaviours matter most here:

*Resume* -- nothing is held in memory. Work is claimed from the database and
the outcome is committed immediately, so an interrupted run continues from
exactly where it stopped.

*Auto-retry* -- transient failures go back into the queue with exponential
backoff instead of being written down as a score of zero.

*Offline handling* -- a dropped connection pauses the whole supervisor rather
than chewing through every URL's retry budget. When connectivity returns, work
resumes on its own.
"""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Callable, Sequence

from . import client
from .config import Settings
from .db import DONE, FAILED, PENDING, RUN_DONE, RUN_PAUSED, RUN_RUNNING, Store, utcnow
from .net import FATAL, OFFLINE, RETRY, FetchError, RateLimiter, backoff_delay, internet_available

Listener = Callable[[dict[str, Any]], None]

# How long to wait between connectivity probes while offline.
OFFLINE_PROBE_SECONDS = 10.0
# Consecutive network-level failures before declaring the connection down.
OFFLINE_TRIP_THRESHOLD = 3


def _noop(_event: dict[str, Any]) -> None:
    pass


class NetworkGate:
    """Circuit breaker for the internet connection itself.

    Individual URLs fail all the time; that is normal and handled by retries.
    A run of consecutive *network* errors means something different -- the
    connection has gone -- and the right response is to stop making requests
    and wait, not to spend every task's attempts on a dead link.
    """

    def __init__(self, listener: Listener = _noop):
        self._online = threading.Event()
        self._online.set()
        self._lock = threading.Lock()
        self._consecutive = 0
        self._watcher: threading.Thread | None = None
        self._listener = listener
        self._stop = threading.Event()

    @property
    def online(self) -> bool:
        return self._online.is_set()

    def report_success(self) -> None:
        with self._lock:
            self._consecutive = 0

    def report_network_error(self) -> None:
        with self._lock:
            self._consecutive += 1
            if self._consecutive < OFFLINE_TRIP_THRESHOLD or not self._online.is_set():
                return
            self._online.clear()
            self._listener({"type": "offline"})
            self._watcher = threading.Thread(
                target=self._watch, name="psi-network-watch", daemon=True
            )
            self._watcher.start()

    def _watch(self) -> None:
        while not self._stop.is_set():
            if internet_available():
                with self._lock:
                    self._consecutive = 0
                    self._online.set()
                self._listener({"type": "online"})
                return
            self._stop.wait(OFFLINE_PROBE_SECONDS)

    def wait_until_online(
        self, should_stop: Callable[[], bool], timeout: float = 1.0
    ) -> bool:
        """Block while offline. Returns False if asked to stop meanwhile."""
        while not self._online.is_set():
            if should_stop():
                return False
            self._online.wait(timeout)
        return not should_stop()

    def shutdown(self) -> None:
        self._stop.set()


@dataclass
class RunHandle:
    run_id: str
    label: str
    settings: Settings
    thread: threading.Thread | None = None
    finished: bool = False
    active: bool = False  # holds a run slot and is really working
    # Set to wind this run down without touching any other run. A stopped
    # handle is discarded, so the same run can be started again afterwards.
    stop: threading.Event = field(default_factory=threading.Event)


class Supervisor:
    """Runs one or more analyses concurrently against a shared store."""

    def __init__(self, store: Store, settings: Settings, listener: Listener = _noop):
        self.store = store
        self.settings = settings
        self.listener = listener
        self.limiter = RateLimiter(settings.rate_limit_per_min)
        self.network = NetworkGate(listener)
        self._stop = threading.Event()
        self._hard_stop = threading.Event()
        self._runs: dict[str, RunHandle] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.workers), thread_name_prefix="psi-worker"
        )
        # Caps how many sites are worked on at once; extra runs wait their turn
        # rather than all competing for the same worker pool.
        self._slots = threading.Semaphore(max(1, settings.max_concurrent_runs))
        self._heartbeat_thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self, run_id: str, *, settings: Settings | None = None) -> bool:
        """Begin (or resume) a run. False if another live process owns it."""
        with self._lock:
            if run_id in self._runs and not self._runs[run_id].finished:
                return True

        row = self.store.get_run(run_id)
        if row is None:
            return False

        if not self.store.claim_run(run_id):
            self.listener(
                {
                    "type": "busy",
                    "run_id": run_id,
                    "message": "another process is already running this",
                }
            )
            return False

        # We now own the run, so any task still marked `running` belongs to a
        # process that died. Reclaim it rather than waiting out its lease.
        recovered = self.store.reset_running(run_id)

        run_settings = settings or Settings.from_json(row["config"])
        run_settings = replace(
            run_settings,
            api_key=self.settings.api_key,
            request_timeout=self.settings.request_timeout,
            max_attempts=self.settings.max_attempts,
        )

        handle = RunHandle(run_id=run_id, label=row["label"], settings=run_settings)
        with self._lock:
            self._runs[run_id] = handle

        counts = self.store.progress(run_id)
        self.listener(
            {
                "type": "run_start",
                "run_id": run_id,
                "label": row["label"],
                "counts": counts,
                "recovered": recovered,
            }
        )

        handle.thread = threading.Thread(
            target=self._drive, args=(handle,), name=f"psi-run-{run_id}", daemon=True
        )
        handle.thread.start()
        self._ensure_heartbeat()
        return True

    def _active_ids(self) -> list[str]:
        return [rid for rid, h in self._runs.items() if not h.finished]

    def _ensure_heartbeat(self) -> None:
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="psi-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        """Prove liveness so other processes know not to steal these runs."""
        while not self._stop.is_set():
            with self._lock:
                ids = self._active_ids()
            if not ids:
                return
            try:
                self.store.heartbeat(ids)
            except Exception:  # noqa: BLE001 - never let a heartbeat kill the run
                pass
            self._stop.wait(20.0)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until every started run finishes. False if it timed out."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                threads = [h.thread for h in self._runs.values() if h.thread]
            alive = [t for t in threads if t.is_alive()]
            if not alive:
                return True
            if deadline is not None and time.monotonic() > deadline:
                return False
            for thread in alive:
                thread.join(timeout=0.2)
                break

    def stop(self, *, hard: bool = False) -> None:
        """Wind the whole supervisor down. `hard` abandons in-flight work.

        This is the end of the supervisor's life: the network watcher is shut
        down with it, so nothing can be started afterwards. To pause work and
        keep the supervisor usable -- what a GUI's Stop button wants -- use
        `stop_run` or `stop_all_runs` instead.
        """
        self._stop.set()
        if hard:
            self._hard_stop.set()
        self.network.shutdown()

    def stop_run(self, run_id: str) -> bool:
        """Ask one run to wind down, leaving every other run alone.

        False if this supervisor is not driving that run. No new tests are
        claimed and claimed-but-unsent ones go back on the queue; requests
        already in flight are allowed to land so their quota is not wasted.
        Everything committed stays committed, so the run resumes cleanly.
        """
        with self._lock:
            handle = self._runs.get(run_id)
        if handle is None or handle.finished:
            return False
        handle.stop.set()
        return True

    def stop_all_runs(self) -> int:
        """Stop every live run but keep the supervisor ready for more work."""
        with self._lock:
            handles = [h for h in self._runs.values() if not h.finished]
        for handle in handles:
            handle.stop.set()
        return len(handles)

    def is_running(self, run_id: str) -> bool:
        """Whether this process is currently driving that run."""
        with self._lock:
            handle = self._runs.get(run_id)
        return bool(handle and not handle.finished)

    def is_stopping(self, run_id: str) -> bool:
        """Whether that run has been asked to stop and is still winding down."""
        with self._lock:
            handle = self._runs.get(run_id)
        return bool(handle and not handle.finished and handle.stop.is_set())

    def shutdown(self) -> None:
        self._stop.set()
        self.network.shutdown()
        self._pool.shutdown(wait=not self._hard_stop.is_set(), cancel_futures=True)
        with self._lock:
            handles = list(self._runs.values())
        for handle in handles:
            if handle.finished:
                continue
            counts = self.store.progress(handle.run_id)
            status = RUN_DONE if counts[PENDING] == 0 else RUN_PAUSED
            self.store.set_run_status(handle.run_id, status)

    # -- the per-run driver -------------------------------------------------

    def _batch_size(self) -> int:
        """Share the worker pool between however many runs are working."""
        with self._lock:
            active = max(1, sum(1 for h in self._runs.values() if h.active))
        return max(1, math.ceil(self.settings.workers / active))

    def _stopping(self, handle: RunHandle) -> bool:
        """Has either this run or the whole supervisor been asked to stop?"""
        return self._stop.is_set() or handle.stop.is_set()

    def _pause(self, handle: RunHandle, seconds: float) -> None:
        """Sleep, waking early if this run or the supervisor is stopped."""
        deadline = time.monotonic() + seconds
        while not self._stopping(handle):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            handle.stop.wait(min(0.5, remaining))

    def _drive(self, handle: RunHandle) -> None:
        run_id = handle.run_id
        idle_polls = 0

        # Wait for a free run slot before doing any work.
        while not self._stopping(handle):
            if self._slots.acquire(timeout=0.5):
                handle.active = True
                break
        if not handle.active:
            self._finalise(handle)
            return

        try:
            while not self._stopping(handle):
                if not self.network.wait_until_online(lambda: self._stopping(handle)):
                    break

                batch = self.store.claim_tasks(
                    run_id, self._batch_size(), self.settings.lease_seconds
                )

                if not batch:
                    counts = self.store.progress(run_id)
                    if counts[PENDING] == 0 and counts["running"] == 0:
                        break  # nothing left anywhere: the run is complete
                    if counts[PENDING] == 0:
                        # Only tasks held by another process remain.
                        idle_polls += 1
                        if idle_polls > 60:
                            break
                        self._pause(handle, 2.0)
                        continue

                    # Everything pending is waiting out a backoff.
                    soonest = self.store.next_ready_at(run_id)
                    delay = 2.0
                    if soonest:
                        delay = max(1.0, min(30.0, (soonest - utcnow()).total_seconds()))
                    self.listener(
                        {"type": "waiting", "run_id": run_id, "seconds": round(delay, 1)}
                    )
                    self._pause(handle, delay)
                    continue

                idle_polls = 0
                futures: list[Future] = []
                for task in batch:
                    if self._stopping(handle):
                        self.store.release_tasks([task["id"]])
                        continue
                    futures.append(self._pool.submit(self._execute, handle, dict(task)))
                if futures:
                    wait(futures)
        except Exception as exc:  # noqa: BLE001 - surface, do not crash silently
            self.listener({"type": "error", "run_id": run_id, "message": str(exc)})
        finally:
            if handle.active:
                handle.active = False
                self._slots.release()
            self._finalise(handle)

    def _finalise(self, handle: RunHandle) -> None:
        handle.finished = True
        counts = self.store.progress(handle.run_id)
        complete = counts[PENDING] == 0 and counts["running"] == 0
        status = RUN_DONE if complete else RUN_PAUSED
        fields: dict[str, Any] = {"owner": None}
        if complete:
            from .db import iso

            fields["finished_at"] = iso()
        self.store.set_run_status(handle.run_id, status, **fields)
        self.listener(
            {
                "type": "run_finish",
                "run_id": handle.run_id,
                "label": handle.label,
                "counts": counts,
                "status": status,
                "stopped": handle.stop.is_set(),
            }
        )

    # -- executing a single task -------------------------------------------

    def _execute(self, handle: RunHandle, task: dict[str, Any]) -> None:
        task_id = task["id"]
        run_id = handle.run_id
        url = task["url"]
        strategy = task["strategy"]

        # A task is only worth executing if nobody has asked us to stop since
        # it was claimed. Checked either side of the rate limiter, which is
        # where a task can sit for a minute after a quota bounce.
        if self._hard_stop.is_set() or self._stopping(handle):
            self.store.release_tasks([task_id])
            return

        try:
            self.limiter.acquire()
        except Exception:  # noqa: BLE001
            self.store.release_tasks([task_id])
            return

        if self._hard_stop.is_set() or self._stopping(handle):
            self.store.release_tasks([task_id])
            return

        self.listener(
            {"type": "task_start", "run_id": run_id, "url": url, "strategy": strategy}
        )

        try:
            record = client.analyze(url, strategy, handle.settings)
        except FetchError as exc:
            self._handle_failure(handle, task, exc)
        except Exception as exc:  # noqa: BLE001 - unexpected bugs must not lose the task
            self._handle_failure(handle, task, FetchError(f"Unexpected: {exc}", RETRY))
        else:
            self.network.report_success()
            self.store.record_success(task_id, run_id, record)
            self.listener(
                {
                    "type": "task_done",
                    "run_id": run_id,
                    "url": url,
                    "strategy": strategy,
                    "status": record["status"],
                    "performance": record.get("performance"),
                    "counts": self.store.progress(run_id),
                }
            )

    def _handle_failure(
        self, handle: RunHandle, task: dict[str, Any], error: FetchError
    ) -> None:
        task_id = task["id"]
        run_id = handle.run_id
        attempts_before = task.get("attempts", 0)
        max_attempts = handle.settings.max_attempts

        if error.kind == OFFLINE:
            # Not this URL's fault. Hand it back untouched and trip the breaker.
            self.network.report_network_error()
            self.store.defer_task(
                task_id,
                retry_at=utcnow() + timedelta(seconds=5),
                error=error.message,
                error_kind=OFFLINE,
            )
            self.listener(
                {
                    "type": "task_deferred",
                    "run_id": run_id,
                    "url": task["url"],
                    "strategy": task["strategy"],
                    "reason": error.message,
                }
            )
            return

        self.network.report_success()  # we reached the server, so we are online

        if error.status == 429 or (error.retry_after and error.kind == RETRY):
            # A quota bounce is a property of the account, not the page. Slow
            # everyone down and requeue without spending an attempt.
            delay = error.retry_after or 60.0
            self.limiter.penalise(delay)
            self.store.defer_task(
                task_id,
                retry_at=utcnow() + timedelta(seconds=delay),
                error=error.message,
                error_kind="ratelimit",
            )
            self.listener(
                {
                    "type": "task_deferred",
                    "run_id": run_id,
                    "url": task["url"],
                    "strategy": task["strategy"],
                    "reason": f"{error.message}; backing off {int(delay)}s",
                }
            )
            return

        attempts_after = attempts_before + 1
        if error.kind == FATAL or attempts_after >= max_attempts:
            self.store.record_failure(
                task_id, error=error.message, error_kind=error.kind
            )
            self.listener(
                {
                    "type": "task_failed",
                    "run_id": run_id,
                    "url": task["url"],
                    "strategy": task["strategy"],
                    "attempts": attempts_after,
                    "reason": error.message,
                    "counts": self.store.progress(run_id),
                }
            )
            return

        delay = backoff_delay(
            attempts_after,
            base=handle.settings.backoff_base,
            cap=handle.settings.backoff_cap,
        )
        self.store.record_retry(
            task_id,
            error=error.message,
            error_kind=error.kind,
            retry_at=utcnow() + timedelta(seconds=delay),
        )
        self.listener(
            {
                "type": "task_retry",
                "run_id": run_id,
                "url": task["url"],
                "strategy": task["strategy"],
                "attempt": attempts_after,
                "max_attempts": max_attempts,
                "delay": round(delay, 1),
                "reason": error.message,
            }
        )


def run_batch(
    store: Store,
    settings: Settings,
    run_ids: Sequence[str],
    listener: Listener = _noop,
) -> Supervisor:
    """Start several runs on one supervisor and return it (does not block)."""
    supervisor = Supervisor(store, settings, listener)
    for run_id in run_ids:
        supervisor.start(run_id)
    return supervisor

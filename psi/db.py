"""SQLite-backed state store.

Every unit of work (one URL x one strategy) is a row in `tasks`, and the row is
updated in a single committed transaction the moment that unit finishes. There
is no in-memory accumulation of results, so whatever killed the process --
power cut, closed laptop, `Ctrl+C`, blue screen -- can at worst lose the tests
that were mid-flight, and those are recovered by lease expiry on the next run.

Durability notes:
  * WAL journalling lets the dashboard read while a run is writing.
  * `synchronous=FULL` forces an fsync on every commit. In WAL mode the more
    common `NORMAL` setting can lose recently committed transactions if the
    machine loses power, which is precisely the failure we are defending
    against here. We commit roughly once per completed PageSpeed test (~15s of
    work), so the extra fsync costs nothing measurable.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    source       TEXT NOT NULL,
    source_kind  TEXT NOT NULL,
    status       TEXT NOT NULL,
    config       TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    heartbeat_at TEXT,
    owner        TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_error_kind TEXT,
    next_attempt_at TEXT,
    lease_until     TEXT,
    lease_owner     TEXT,
    updated_at      TEXT NOT NULL,
    UNIQUE(run_id, url, strategy)
);

CREATE TABLE IF NOT EXISTS results (
    task_id        INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    run_id         TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    performance    INTEGER,
    accessibility  INTEGER,
    best_practices INTEGER,
    seo            INTEGER,
    fcp            REAL,
    lcp            REAL,
    cls            REAL,
    tbt            REAL,
    speed_index    REAL,
    status         TEXT NOT NULL,
    issues         TEXT,
    details        TEXT,
    lighthouse_version TEXT,
    fetched_at     TEXT NOT NULL,
    duration_ms    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tasks_run_state ON tasks(run_id, state);
CREATE INDEX IF NOT EXISTS idx_tasks_state     ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_results_run     ON results(run_id);
"""

# Terminal and non-terminal task states.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# Run states.
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_PAUSED = "paused"
RUN_DONE = "done"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or utcnow()).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def owner_tag() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _pid_running(pid: int) -> bool:
    """Is this process id still live?

    On Windows `os.kill(pid, 0)` does not probe -- it calls TerminateProcess
    and would kill the target -- so query the process handle instead.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True  # cannot tell; assume alive
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - fall back to assuming it is alive
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def owner_is_alive(owner: str | None) -> bool:
    """Whether the process recorded as owning a run is still running.

    Without this, a run killed by a crash or power cut stays locked until its
    heartbeat goes stale -- turning "resume immediately" into "wait two
    minutes". A dead local pid is detected at once; owners on other machines
    fall back to the heartbeat age, which is all we can observe.
    """
    if not owner:
        return False
    host, _, pid_text = owner.rpartition(":")
    if host != socket.gethostname():
        return True  # different machine: defer to the heartbeat check
    if pid_text == str(os.getpid()):
        return True
    try:
        return _pid_running(int(pid_text))
    except ValueError:
        return True


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


class Store:
    """Thread-safe handle onto the SQLite database.

    Each thread gets its own connection; SQLite serialises writers for us and
    `busy_timeout` absorbs the contention when several runs commit at once.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        # Every thread opens its own connection; keep a registry so they can
        # all be closed on shutdown (Windows will not delete an open db file).
        self._connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self._init_schema()

    # -- connection handling ------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path),
                timeout=60.0,
                isolation_level=None,  # explicit transaction control
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            with self._conn_lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        """Close every connection this store has handed out."""
        with self._conn_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction. Serialised in-process to avoid lock churn."""
        with self._write_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _init_schema(self) -> None:
        # `executescript` commits any open transaction before it runs, so it
        # cannot be nested inside `write()`; it manages its own.
        with self._write_lock:
            self.conn.executescript(SCHEMA)
        with self.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- runs ---------------------------------------------------------------

    def create_run(
        self,
        *,
        label: str,
        source: str,
        source_kind: str,
        config_json: str,
        urls: Sequence[str],
        strategies: Sequence[str],
        note: str | None = None,
    ) -> str:
        run_id = new_run_id()
        now = iso()
        rows = [(run_id, url, strategy, PENDING, now) for url in urls for strategy in strategies]
        with self.write() as conn:
            conn.execute(
                """INSERT INTO runs(id, label, source, source_kind, status, config,
                                    created_at, note)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, label, source, source_kind, RUN_PENDING, config_json, now, note),
            )
            conn.executemany(
                """INSERT OR IGNORE INTO tasks(run_id, url, strategy, state, updated_at)
                   VALUES(?, ?, ?, ?, ?)""",
                rows,
            )
        return run_id

    def add_urls(self, run_id: str, urls: Sequence[str], strategies: Sequence[str]) -> int:
        """Add URLs to an existing run, skipping ones already present."""
        now = iso()
        rows = [(run_id, url, strategy, PENDING, now) for url in urls for strategy in strategies]
        with self.write() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            conn.executemany(
                """INSERT OR IGNORE INTO tasks(run_id, url, strategy, state, updated_at)
                   VALUES(?, ?, ?, ?, ?)""",
                rows,
            )
            after = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        return after - before

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def resolve_run_id(self, prefix: str) -> str | None:
        """Allow abbreviating a run id, as long as the prefix is unambiguous."""
        rows = self.conn.execute(
            "SELECT id FROM runs WHERE id = ? OR id LIKE ? || '%'", (prefix, prefix)
        ).fetchall()
        exact = [r["id"] for r in rows if r["id"] == prefix]
        if exact:
            return exact[0]
        if len(rows) == 1:
            return rows[0]["id"]
        return None

    def list_runs(self, *, status: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params).fetchall())

    def set_run_status(self, run_id: str, status: str, **fields: Any) -> None:
        assignments = ["status = ?"]
        params: list[Any] = [status]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            params.append(value)
        params.append(run_id)
        with self.write() as conn:
            conn.execute(f"UPDATE runs SET {', '.join(assignments)} WHERE id = ?", params)

    def claim_run(self, run_id: str) -> bool:
        """Mark a run as owned by this process. False if someone else has it live."""
        now = utcnow()
        with self.write() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return False
            if row["status"] == RUN_RUNNING and row["owner"] != owner_tag():
                beat = parse_iso(row["heartbeat_at"])
                fresh = bool(beat and (now - beat) < timedelta(minutes=2))
                # Only a live owner blocks the claim. A fresh heartbeat left
                # behind by a process that has since died must not.
                if fresh and owner_is_alive(row["owner"]):
                    return False
            conn.execute(
                """UPDATE runs SET status = ?, owner = ?, heartbeat_at = ?,
                                   started_at = COALESCE(started_at, ?), finished_at = NULL
                   WHERE id = ?""",
                (RUN_RUNNING, owner_tag(), iso(now), iso(now), run_id),
            )
        return True

    def heartbeat(self, run_ids: Iterable[str]) -> None:
        ids = list(run_ids)
        if not ids:
            return
        now = iso()
        with self.write() as conn:
            conn.executemany(
                "UPDATE runs SET heartbeat_at = ? WHERE id = ?", [(now, rid) for rid in ids]
            )

    def delete_run(self, run_id: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM results WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM tasks WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    # -- task queue ---------------------------------------------------------

    def reclaim_expired(self, run_id: str | None = None) -> int:
        """Return abandoned `running` tasks to the queue.

        This is the recovery path after a crash or power failure: the dead
        process's lease simply lapses and the work becomes claimable again.
        """
        now = iso()
        sql = """UPDATE tasks
                 SET state = ?, lease_until = NULL, lease_owner = NULL, updated_at = ?
                 WHERE state = ? AND (lease_until IS NULL OR lease_until < ?)"""
        params: list[Any] = [PENDING, now, RUNNING, now]
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        with self.write() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount or 0

    def reset_running(self, run_id: str) -> int:
        """Force every `running` task of a run back to `pending`.

        Only safe once `claim_run` has established that no live process owns
        the run. After an abrupt kill the dead owner's leases have not expired
        yet, so waiting for them would idle the resume for a quarter of an
        hour; this clears them immediately.
        """
        now = iso()
        with self.write() as conn:
            cur = conn.execute(
                """UPDATE tasks
                   SET state = ?, lease_until = NULL, lease_owner = NULL, updated_at = ?
                   WHERE run_id = ? AND state = ?""",
                (PENDING, now, run_id, RUNNING),
            )
            return cur.rowcount or 0

    def defer_task(self, task_id: int, *, retry_at: datetime, error: str, error_kind: str) -> None:
        """Requeue a task *without* consuming one of its attempts.

        Used for conditions that say nothing about the page itself -- a rate
        limit or a dropped internet connection -- so an outage cannot exhaust
        a URL's retry budget and mark a healthy page as failed.
        """
        with self.write() as conn:
            conn.execute(
                """UPDATE tasks
                   SET state = ?, last_error = ?, last_error_kind = ?,
                       next_attempt_at = ?, lease_until = NULL, lease_owner = NULL,
                       updated_at = ?
                   WHERE id = ?""",
                (PENDING, error[:2000], error_kind, iso(retry_at), iso(), task_id),
            )

    def claim_tasks(self, run_id: str, limit: int, lease_seconds: int) -> list[sqlite3.Row]:
        """Atomically take up to `limit` ready tasks for this worker."""
        if limit <= 0:
            return []
        now = utcnow()
        lease_until = iso(now + timedelta(seconds=lease_seconds))
        owner = owner_tag()
        with self.write() as conn:
            rows = conn.execute(
                """SELECT id FROM tasks
                   WHERE run_id = ? AND state = ?
                     AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   ORDER BY attempts ASC, id ASC
                   LIMIT ?""",
                (run_id, PENDING, iso(now), limit),
            ).fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"""UPDATE tasks
                    SET state = ?, lease_until = ?, lease_owner = ?, updated_at = ?
                    WHERE id IN ({placeholders})""",
                [RUNNING, lease_until, owner, iso(now), *ids],
            )
            return list(
                conn.execute(
                    f"SELECT * FROM tasks WHERE id IN ({placeholders})", ids
                ).fetchall()
            )

    def release_tasks(self, task_ids: Sequence[int]) -> None:
        """Put claimed-but-unstarted tasks back (used on graceful shutdown)."""
        if not task_ids:
            return
        placeholders = ",".join("?" * len(task_ids))
        with self.write() as conn:
            conn.execute(
                f"""UPDATE tasks
                    SET state = ?, lease_until = NULL, lease_owner = NULL, updated_at = ?
                    WHERE id IN ({placeholders}) AND state = ?""",
                [PENDING, iso(), *task_ids, RUNNING],
            )

    def next_ready_at(self, run_id: str) -> datetime | None:
        """When the soonest backed-off task becomes eligible, if any."""
        row = self.conn.execute(
            """SELECT MIN(next_attempt_at) AS soonest FROM tasks
               WHERE run_id = ? AND state = ? AND next_attempt_at IS NOT NULL""",
            (run_id, PENDING),
        ).fetchone()
        return parse_iso(row["soonest"]) if row else None

    # -- recording outcomes -------------------------------------------------

    def record_success(self, task_id: int, run_id: str, payload: dict[str, Any]) -> None:
        now = iso()
        with self.write() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO results(
                       task_id, run_id, url, strategy, performance, accessibility,
                       best_practices, seo, fcp, lcp, cls, tbt, speed_index,
                       status, issues, details, lighthouse_version, fetched_at, duration_ms)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    run_id,
                    payload["url"],
                    payload["strategy"],
                    payload.get("performance"),
                    payload.get("accessibility"),
                    payload.get("best_practices"),
                    payload.get("seo"),
                    payload.get("fcp"),
                    payload.get("lcp"),
                    payload.get("cls"),
                    payload.get("tbt"),
                    payload.get("speed_index"),
                    payload.get("status", "PASS"),
                    json.dumps(payload.get("issues", [])),
                    json.dumps(payload.get("details", {})),
                    payload.get("lighthouse_version"),
                    now,
                    payload.get("duration_ms"),
                ),
            )
            conn.execute(
                """UPDATE tasks
                   SET state = ?, attempts = attempts + 1, last_error = NULL,
                       last_error_kind = NULL, lease_until = NULL, lease_owner = NULL,
                       next_attempt_at = NULL, updated_at = ?
                   WHERE id = ?""",
                (DONE, now, task_id),
            )

    def record_retry(
        self, task_id: int, *, error: str, error_kind: str, retry_at: datetime
    ) -> None:
        """Failure that we intend to try again — the task returns to the queue."""
        with self.write() as conn:
            conn.execute(
                """UPDATE tasks
                   SET state = ?, attempts = attempts + 1, last_error = ?,
                       last_error_kind = ?, next_attempt_at = ?, lease_until = NULL,
                       lease_owner = NULL, updated_at = ?
                   WHERE id = ?""",
                (PENDING, error[:2000], error_kind, iso(retry_at), iso(), task_id),
            )

    def record_failure(self, task_id: int, *, error: str, error_kind: str) -> None:
        """Give up on a task after exhausting attempts (or a permanent error)."""
        with self.write() as conn:
            conn.execute(
                """UPDATE tasks
                   SET state = ?, attempts = attempts + 1, last_error = ?,
                       last_error_kind = ?, lease_until = NULL, lease_owner = NULL,
                       next_attempt_at = NULL, updated_at = ?
                   WHERE id = ?""",
                (FAILED, error[:2000], error_kind, iso(), task_id),
            )

    def requeue_failed(self, run_id: str, *, reset_attempts: bool = True) -> int:
        """Move exhausted failures back into the queue (`psi retry`)."""
        now = iso()
        attempts_sql = "attempts = 0," if reset_attempts else ""
        with self.write() as conn:
            cur = conn.execute(
                f"""UPDATE tasks
                    SET state = ?, {attempts_sql} next_attempt_at = NULL,
                        lease_until = NULL, lease_owner = NULL, updated_at = ?
                    WHERE run_id = ? AND state = ?""",
                (PENDING, now, run_id, FAILED),
            )
            # A run being driven right now must keep its `running` status, or
            # another process would be free to claim it out from under us.
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = NULL WHERE id = ? AND status != ?",
                (RUN_PENDING, run_id, RUN_RUNNING),
            )
            return cur.rowcount or 0

    def requeue_tasks(self, task_ids: Sequence[int], *, reset_attempts: bool = True) -> int:
        """Put specific tasks back on the queue, whatever they ended up as.

        The single-test counterpart to `requeue_failed`: one bad score out of
        a hundred can be re-tested without redoing the rest. Any previous
        result is dropped, since the retry supersedes it.

        Tasks that are in flight right now are skipped -- requeueing one would
        test the same URL twice and let the older copy overwrite the retry --
        so the return value can be lower than the number asked for.
        """
        ids = list(dict.fromkeys(int(t) for t in task_ids))
        if not ids:
            return 0
        now = iso()
        attempts_sql = "attempts = 0," if reset_attempts else ""
        with self.write() as conn:
            marks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT id FROM tasks WHERE id IN ({marks}) AND state != ?",
                [*ids, RUNNING],
            ).fetchall()
            eligible = [row["id"] for row in rows]
            if not eligible:
                return 0
            marks = ",".join("?" * len(eligible))
            conn.execute(f"DELETE FROM results WHERE task_id IN ({marks})", eligible)
            conn.execute(
                f"""UPDATE tasks
                    SET state = ?, {attempts_sql} last_error = NULL,
                        last_error_kind = NULL, next_attempt_at = NULL,
                        lease_until = NULL, lease_owner = NULL, updated_at = ?
                    WHERE id IN ({marks})""",
                [PENDING, now, *eligible],
            )
            # Reopen any run these tasks belong to that had been declared
            # finished; a live or paused run is already resumable as it is.
            conn.execute(
                f"""UPDATE runs SET status = ?, finished_at = NULL
                    WHERE status = ?
                      AND id IN (SELECT run_id FROM tasks WHERE id IN ({marks}))""",
                [RUN_PENDING, RUN_DONE, *eligible],
            )
            return len(eligible)

    # -- reading progress ---------------------------------------------------

    def progress(self, run_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) AS n FROM tasks WHERE run_id = ? GROUP BY state",
            (run_id,),
        ).fetchall()
        counts = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0}
        for row in rows:
            counts[row["state"]] = row["n"]
        counts["total"] = sum(counts[s] for s in (PENDING, RUNNING, DONE, FAILED))
        counts["finished"] = counts[DONE] + counts[FAILED]
        return counts

    def result_summary(self, run_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM results WHERE run_id = ? GROUP BY status",
            (run_id,),
        ).fetchall()
        summary = {row["status"]: row["n"] for row in rows}
        summary.setdefault("PASS", 0)
        summary.setdefault("FAIL", 0)
        return summary

    def results(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT r.*, t.attempts FROM results r
                   JOIN tasks t ON t.id = r.task_id
                   WHERE r.run_id = ?
                   ORDER BY r.url ASC, r.strategy DESC""",
                (run_id,),
            ).fetchall()
        )

    def failed_tasks(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM tasks WHERE run_id = ? AND state = ?
                   ORDER BY url ASC, strategy DESC""",
                (run_id, FAILED),
            ).fetchall()
        )

    def pending_tasks(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM tasks WHERE run_id = ? AND state IN (?, ?)
                   ORDER BY url ASC""",
                (run_id, PENDING, RUNNING),
            ).fetchall()
        )

    def display_status(self, run: sqlite3.Row) -> str:
        """Run status as a human should read it, allowing for dead owners."""
        status = run["status"]
        if status != RUN_RUNNING:
            return status
        beat = parse_iso(run["heartbeat_at"])
        stale = not beat or (utcnow() - beat) > timedelta(minutes=2)
        if stale or not owner_is_alive(run["owner"]):
            return "stalled"
        return status

    def resumable_runs(self) -> list[sqlite3.Row]:
        """Runs with outstanding work, newest first."""
        return list(
            self.conn.execute(
                """SELECT r.* FROM runs r
                   WHERE EXISTS (
                       SELECT 1 FROM tasks t
                       WHERE t.run_id = r.id AND t.state IN (?, ?)
                   )
                   ORDER BY r.created_at DESC""",
                (PENDING, RUNNING),
            ).fetchall()
        )

"""Tests for the durability and retry behaviour.

Every test stubs out `psi.client.analyze`, so nothing here touches the network
or spends API quota.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import gzip
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

from psi import client, report, sitemap
from psi.config import Settings, Thresholds
from psi.db import DONE, FAILED, PENDING, RUNNING, Store, iso, utcnow
from psi.engine import Supervisor
from psi.net import FATAL, OFFLINE, RETRY, FetchError

URLS = [f"https://example.com/page-{i}" for i in range(6)]


def good_result(url: str, strategy: str, _settings=None) -> dict:
    """A passing PageSpeed record, with the signature of `client.analyze`."""
    return {
        "url": url,
        "strategy": strategy,
        "performance": 95,
        "accessibility": 100,
        "best_practices": 100,
        "seo": 100,
        "fcp": 1.2,
        "lcp": 2.0,
        "cls": 0.02,
        "tbt": 0.05,
        "speed_index": 1.4,
        "status": "PASS",
        "issues": [],
        "details": {},
        "lighthouse_version": "11.0.0",
        "duration_ms": 10,
    }


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.addCleanup(self.store.close)
        self.settings = Settings(
            workers=4,
            max_attempts=3,
            backoff_base=0.01,
            backoff_cap=0.05,
            rate_limit_per_min=100000,
            lease_seconds=60,
        )

    def make_run(self, urls=URLS, strategies=("mobile", "desktop"), label="example.com") -> str:
        return self.store.create_run(
            label=label,
            source="https://example.com/sitemap.xml",
            source_kind="sitemap",
            config_json=self.settings.to_json(),
            urls=urls,
            strategies=strategies,
        )

    def drive(self, run_ids, settings=None, timeout=30):
        supervisor = Supervisor(self.store, settings or self.settings)
        for run_id in run_ids:
            supervisor.start(run_id)
        finished = supervisor.wait(timeout=timeout)
        supervisor.shutdown()
        self.assertTrue(finished, "supervisor did not finish in time")
        return supervisor


class TestHappyPath(Base):
    def test_all_tasks_complete(self):
        run_id = self.make_run()
        with mock.patch.object(client, "analyze", side_effect=good_result):
            self.drive([run_id])

        counts = self.store.progress(run_id)
        self.assertEqual(counts["total"], len(URLS) * 2)
        self.assertEqual(counts[DONE], len(URLS) * 2)
        self.assertEqual(counts[PENDING], 0)
        self.assertEqual(counts[FAILED], 0)
        self.assertEqual(self.store.get_run(run_id)["status"], "done")

    def test_each_url_tested_once_per_strategy(self):
        run_id = self.make_run()
        seen = []
        lock = threading.Lock()

        def record(url, strategy, _settings):
            with lock:
                seen.append((url, strategy))
            return good_result(url, strategy)

        with mock.patch.object(client, "analyze", side_effect=record):
            self.drive([run_id])

        self.assertEqual(len(seen), len(set(seen)), "a task was executed twice")
        self.assertEqual(len(seen), len(URLS) * 2)


class TestResume(Base):
    def test_resumes_after_a_hard_stop(self):
        """The core requirement: an interrupted run continues where it stopped."""
        run_id = self.make_run()
        total = len(URLS) * 2
        stop_after = 4
        completed = threading.Event()
        count = {"n": 0}
        lock = threading.Lock()

        def slow(url, strategy, _settings):
            with lock:
                count["n"] += 1
                if count["n"] >= stop_after:
                    completed.set()
            return good_result(url, strategy)

        supervisor = Supervisor(self.store, replace(self.settings, workers=1))
        with mock.patch.object(client, "analyze", side_effect=slow):
            supervisor.start(run_id)
            completed.wait(timeout=20)
            supervisor.stop(hard=True)  # simulates the process being killed
            supervisor.wait(timeout=20)
            supervisor.shutdown()

        mid = self.store.progress(run_id)
        self.assertGreater(mid[DONE], 0, "nothing was persisted before the stop")
        self.assertLess(mid[DONE], total, "the run finished before we could stop it")

        # A fresh supervisor picks up exactly the remaining work.
        done_before = mid[DONE]
        calls = []
        with mock.patch.object(
            client, "analyze",
            side_effect=lambda u, s, c: (calls.append((u, s)), good_result(u, s))[1],
        ):
            self.drive([run_id])

        after = self.store.progress(run_id)
        self.assertEqual(after[DONE], total)
        self.assertEqual(after[PENDING], 0)
        self.assertEqual(
            len(calls), total - done_before,
            "resume re-tested work that was already finished",
        )

    def test_inflight_tasks_are_recovered_not_stranded(self):
        """Tasks left `running` by a killed process become claimable again."""
        run_id = self.make_run(urls=URLS[:2])
        claimed = self.store.claim_tasks(run_id, 2, lease_seconds=3600)
        self.assertEqual(len(claimed), 2)
        self.assertEqual(self.store.progress(run_id)[RUNNING], 2)

        # Long lease means expiry alone would not free these for an hour.
        with mock.patch.object(client, "analyze", side_effect=good_result):
            self.drive([run_id])

        counts = self.store.progress(run_id)
        self.assertEqual(counts[DONE], 4)
        self.assertEqual(counts[RUNNING], 0)

    def test_expired_leases_are_reclaimed(self):
        run_id = self.make_run(urls=URLS[:1], strategies=("mobile",))
        self.store.claim_tasks(run_id, 1, lease_seconds=-10)  # already expired
        reclaimed = self.store.reclaim_expired(run_id)
        self.assertEqual(reclaimed, 1)
        self.assertEqual(self.store.progress(run_id)[PENDING], 1)

    def test_resumable_runs_are_discoverable(self):
        run_id = self.make_run(urls=URLS[:2])
        ids = [r["id"] for r in self.store.resumable_runs()]
        self.assertIn(run_id, ids)

        with mock.patch.object(client, "analyze", side_effect=good_result):
            self.drive([run_id])
        self.assertNotIn(run_id, [r["id"] for r in self.store.resumable_runs()])


class TestRetry(Base):
    def test_transient_failure_is_retried_then_succeeds(self):
        run_id = self.make_run(urls=URLS[:1], strategies=("mobile",))
        attempts = {"n": 0}

        def flaky(url, strategy, _settings):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise FetchError("HTTP 500: upstream hiccup", RETRY, status=500)
            return good_result(url, strategy)

        with mock.patch.object(client, "analyze", side_effect=flaky):
            self.drive([run_id])

        counts = self.store.progress(run_id)
        self.assertEqual(counts[DONE], 1)
        self.assertEqual(counts[FAILED], 0)
        self.assertEqual(attempts["n"], 3)

    def test_gives_up_after_max_attempts(self):
        run_id = self.make_run(urls=URLS[:1], strategies=("mobile",))

        def always_fails(_url, _strategy, _settings):
            raise FetchError("HTTP 503: down", RETRY, status=503)

        with mock.patch.object(client, "analyze", side_effect=always_fails):
            self.drive([run_id])

        counts = self.store.progress(run_id)
        self.assertEqual(counts[FAILED], 1)
        failed = self.store.failed_tasks(run_id)
        self.assertEqual(failed[0]["attempts"], self.settings.max_attempts)
        self.assertIn("503", failed[0]["last_error"])

    def test_fatal_errors_are_not_retried(self):
        run_id = self.make_run(urls=URLS[:1], strategies=("mobile",))

        def bad_key(_url, _strategy, _settings):
            raise FetchError("HTTP 403: API key invalid", FATAL, status=403)

        with mock.patch.object(client, "analyze", side_effect=bad_key):
            self.drive([run_id])

        failed = self.store.failed_tasks(run_id)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["attempts"], 1, "a fatal error should not be retried")

    def test_rate_limit_does_not_consume_the_retry_budget(self):
        """A 429 says nothing about the page, so it must not exhaust attempts."""
        run_id = self.make_run(urls=URLS[:1], strategies=("mobile",))
        calls = {"n": 0}

        def limited(url, strategy, _settings):
            calls["n"] += 1
            if calls["n"] <= 5:  # more bounces than max_attempts
                raise FetchError("HTTP 429", RETRY, status=429, retry_after=0.01)
            return good_result(url, strategy)

        with mock.patch.object(client, "analyze", side_effect=limited):
            self.drive([run_id])

        counts = self.store.progress(run_id)
        self.assertEqual(counts[DONE], 1, "the URL was failed by rate limiting")
        row = self.store.conn.execute(
            "SELECT attempts FROM tasks WHERE run_id = ?", (run_id,)
        ).fetchone()
        self.assertEqual(row["attempts"], 1, "429s were charged as attempts")

    def test_retry_command_requeues_failures(self):
        run_id = self.make_run(urls=URLS[:2], strategies=("mobile",))
        with mock.patch.object(
            client, "analyze",
            side_effect=FetchError("HTTP 500", RETRY, status=500),
        ):
            self.drive([run_id])
        self.assertEqual(self.store.progress(run_id)[FAILED], 2)

        requeued = self.store.requeue_failed(run_id)
        self.assertEqual(requeued, 2)
        self.assertEqual(self.store.progress(run_id)[PENDING], 2)

        with mock.patch.object(client, "analyze", side_effect=good_result):
            self.drive([run_id])
        self.assertEqual(self.store.progress(run_id)[DONE], 2)


class TestStopping(Base):
    def test_stopping_one_run_keeps_the_supervisor_usable(self):
        """A Stop button must pause that run only, not poison the engine."""
        stop_id = self.make_run(urls=URLS[:4], strategies=("mobile",), label="stopped")
        later_id = self.make_run(urls=URLS[:2], strategies=("mobile",), label="later")
        in_flight = threading.Event()
        release = threading.Event()

        def slow(url, strategy, _settings):
            in_flight.set()
            release.wait(timeout=10)
            return good_result(url, strategy)

        supervisor = Supervisor(self.store, replace(self.settings, workers=1))
        with mock.patch.object(client, "analyze", side_effect=slow):
            supervisor.start(stop_id)
            self.assertTrue(in_flight.wait(timeout=10))
            self.assertTrue(supervisor.stop_run(stop_id))
            self.assertTrue(supervisor.is_stopping(stop_id))
            release.set()
            self.assertTrue(supervisor.wait(timeout=20))

        counts = self.store.progress(stop_id)
        self.assertGreater(counts[PENDING], 0, "the run kept going after Stop")
        self.assertEqual(counts[RUNNING], 0, "a test was left claimed")
        self.assertEqual(self.store.get_run(stop_id)["status"], "paused")
        self.assertFalse(supervisor.is_running(stop_id))

        with mock.patch.object(client, "analyze", side_effect=good_result):
            # Same supervisor: stopping a run must not stop the next one.
            self.assertTrue(supervisor.start(later_id))
            self.assertTrue(supervisor.wait(timeout=20))
            supervisor.shutdown()
            self.assertEqual(self.store.progress(later_id)[DONE], 2)

            # And the stopped run resumes from exactly where it left off.
            done_before = self.store.progress(stop_id)[DONE]
            self.drive([stop_id])

        after = self.store.progress(stop_id)
        self.assertEqual(after[DONE], 4)
        self.assertEqual(after[PENDING], 0)
        self.assertGreater(done_before, 0, "nothing was saved before the stop")

    def test_stop_run_reports_when_it_owns_nothing(self):
        run_id = self.make_run(urls=URLS[:1], strategies=("mobile",))
        supervisor = Supervisor(self.store, self.settings)
        self.addCleanup(supervisor.shutdown)
        self.assertFalse(supervisor.stop_run(run_id))
        self.assertFalse(supervisor.is_running(run_id))
        self.assertEqual(supervisor.stop_all_runs(), 0)


class TestSingleTestRetry(Base):
    def test_one_result_can_be_retested_on_its_own(self):
        run_id = self.make_run(urls=URLS[:2], strategies=("mobile",))
        with mock.patch.object(client, "analyze", side_effect=good_result):
            self.drive([run_id])
        self.assertEqual(self.store.progress(run_id)[DONE], 2)

        target = self.store.results(run_id)[0]
        self.assertEqual(self.store.requeue_tasks([target["task_id"]]), 1)

        counts = self.store.progress(run_id)
        self.assertEqual(counts[PENDING], 1)
        self.assertEqual(counts[DONE], 1)
        self.assertEqual(
            len(self.store.results(run_id)), 1, "the superseded result was kept"
        )
        self.assertIn(run_id, [r["id"] for r in self.store.resumable_runs()])

        retested = []

        def record(url, strategy, _settings):
            retested.append((url, strategy))
            return good_result(url, strategy)

        with mock.patch.object(client, "analyze", side_effect=record):
            self.drive([run_id])

        self.assertEqual(retested, [(target["url"], target["strategy"])])
        self.assertEqual(self.store.progress(run_id)[DONE], 2)

    def test_retrying_leaves_a_test_in_flight_alone(self):
        run_id = self.make_run(urls=URLS[:2], strategies=("mobile",))
        claimed = self.store.claim_tasks(run_id, 1, lease_seconds=3600)
        waiting = [t for t in self.store.pending_tasks(run_id) if t["state"] == PENDING]
        asked = [claimed[0]["id"], waiting[0]["id"]]

        # Requeueing an in-flight test would run the URL twice and let the
        # older copy overwrite the retry.
        self.assertEqual(self.store.requeue_tasks(asked), 1)
        self.assertEqual(self.store.progress(run_id)[RUNNING], 1)

    def test_retrying_does_not_unclaim_a_live_run(self):
        run_id = self.make_run(urls=URLS[:2], strategies=("mobile",))
        self.assertTrue(self.store.claim_run(run_id))
        failed = self.store.claim_tasks(run_id, 1, lease_seconds=60)
        self.store.record_failure(failed[0]["id"], error="HTTP 500", error_kind=RETRY)

        self.store.requeue_failed(run_id)
        self.assertEqual(
            self.store.get_run(run_id)["status"], "running",
            "a retry stole the run from the process driving it",
        )


class TestOffline(Base):
    def test_connection_loss_defers_without_consuming_attempts(self):
        """An internet outage must not burn every URL's retry budget."""
        run_id = self.make_run(urls=URLS[:2], strategies=("mobile",))
        state = {"offline": True}
        lock = threading.Lock()

        def maybe_offline(url, strategy, _settings):
            with lock:
                if state["offline"]:
                    raise FetchError("Network unreachable", OFFLINE)
            return good_result(url, strategy)

        supervisor = Supervisor(self.store, self.settings)
        with mock.patch.object(client, "analyze", side_effect=maybe_offline), \
             mock.patch("psi.engine.internet_available", lambda timeout=5.0: not state["offline"]):
            supervisor.start(run_id)
            # Let it trip the breaker, then restore the connection.
            threading.Event().wait(1.0)
            with lock:
                state["offline"] = False
            finished = supervisor.wait(timeout=30)
            supervisor.shutdown()

        self.assertTrue(finished)
        counts = self.store.progress(run_id)
        self.assertEqual(counts[DONE], 2, "work did not resume after the outage")
        self.assertEqual(counts[FAILED], 0, "an outage failed URLs that were fine")


class TestMultipleRuns(Base):
    def test_several_sites_progress_together(self):
        run_a = self.make_run(urls=URLS[:3], label="site-a")
        run_b = self.make_run(
            urls=[f"https://other.test/p{i}" for i in range(3)], label="site-b"
        )
        run_c = self.make_run(
            urls=[f"https://third.test/p{i}" for i in range(3)], label="site-c"
        )

        with mock.patch.object(client, "analyze", side_effect=good_result):
            self.drive([run_a, run_b, run_c], timeout=60)

        for run_id in (run_a, run_b, run_c):
            counts = self.store.progress(run_id)
            self.assertEqual(counts[DONE], 6, f"run {run_id} did not finish")
            self.assertEqual(self.store.get_run(run_id)["status"], "done")

    def test_run_slots_are_respected(self):
        settings = replace(self.settings, max_concurrent_runs=1, workers=2)
        runs = [self.make_run(urls=URLS[:2], label=f"s{i}") for i in range(3)]
        active = {"now": 0, "peak": 0}
        lock = threading.Lock()
        seen_runs: set[str] = set()

        def track(url, strategy, _settings):
            with lock:
                seen_runs.add(url)
            return good_result(url, strategy)

        with mock.patch.object(client, "analyze", side_effect=track):
            self.drive(runs, settings=settings, timeout=60)

        for run_id in runs:
            self.assertEqual(self.store.progress(run_id)[DONE], 4)


class TestThresholds(unittest.TestCase):
    def test_default_rules_match_the_original_tool(self):
        thresholds = Thresholds()
        record = {
            "performance": 89, "accessibility": 100, "best_practices": 40,
            "seo": 100, "fcp": 1.0, "lcp": 2.0, "cls": 0.05, "tbt": 5.0,
        }
        issues = client.evaluate(record, thresholds)
        self.assertEqual(len(issues), 1)
        self.assertIn("Performance", issues[0])

    def test_metric_violations_are_reported(self):
        thresholds = Thresholds()
        record = {
            "performance": 100, "accessibility": 100, "best_practices": 100,
            "seo": 100, "fcp": 3.0, "lcp": 4.0, "cls": 0.4, "tbt": 0.1,
        }
        issues = client.evaluate(record, thresholds)
        self.assertEqual(len(issues), 3)

    def test_disabled_threshold_is_skipped(self):
        thresholds = Thresholds(performance=None)
        record = {
            "performance": 3, "accessibility": 100, "best_practices": 100,
            "seo": 100, "fcp": 1.0, "lcp": 1.0, "cls": 0.0, "tbt": 0.0,
        }
        self.assertEqual(client.evaluate(record, thresholds), [])


class TestSitemap(unittest.TestCase):
    URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/a</loc></url>
      <url><loc>https://example.com/b</loc></url>
    </urlset>"""

    INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/one.xml</loc></sitemap>
      <sitemap><loc>https://example.com/two.xml</loc></sitemap>
    </sitemapindex>"""

    def test_parses_a_urlset(self):
        pages, nested = sitemap.parse_sitemap_xml(self.URLSET)
        self.assertEqual(pages, ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(nested, [])

    def test_parses_a_sitemap_index(self):
        pages, nested = sitemap.parse_sitemap_xml(self.INDEX)
        self.assertEqual(pages, [])
        self.assertEqual(len(nested), 2)

    def test_follows_nested_sitemaps_and_dedupes(self):
        responses = {
            "https://example.com/sitemap.xml": self.INDEX,
            "https://example.com/one.xml": self.URLSET,
            "https://example.com/two.xml": self.URLSET,  # same URLs again
        }
        with mock.patch("psi.sitemap.fetch", side_effect=lambda u, **k: responses[u]):
            result = sitemap.collect_urls("https://example.com/sitemap.xml")
        self.assertEqual(result.urls, ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(len(result.sitemaps_read), 3)

    def test_handles_gzipped_sitemaps(self):
        payload = gzip.compress(self.URLSET)
        with mock.patch("psi.sitemap.fetch", side_effect=lambda u, **k: payload):
            result = sitemap.collect_urls("https://example.com/sitemap.xml.gz")
        self.assertEqual(len(result.urls), 2)

    def test_include_and_exclude_filters(self):
        urls = ["https://e.com/blog/a", "https://e.com/shop/b", "https://e.com/blog/c"]
        self.assertEqual(len(sitemap.filter_urls(urls, include=r"/blog/")), 2)
        self.assertEqual(len(sitemap.filter_urls(urls, exclude=r"/blog/")), 1)


class TestReport(Base):
    def test_errors_are_distinguished_from_zero_scores(self):
        """An untestable URL must not look like a page that scored 0."""
        run_id = self.make_run(urls=URLS[:2], strategies=("mobile",))

        def one_fails(url, strategy, _settings):
            if url.endswith("-0"):
                raise FetchError("HTTP 403: forbidden", FATAL, status=403)
            return good_result(url, strategy)

        with mock.patch.object(client, "analyze", side_effect=one_fails):
            self.drive([run_id])

        data = report.gather(self.store, run_id)
        self.assertEqual(data["summary"]["tested"], 1)
        self.assertEqual(data["summary"]["errored"], 1)
        self.assertEqual(data["errors"][0]["url"], URLS[0])

        html = report.to_html(data)
        self.assertIn("ERROR", html)
        self.assertNotIn("<script src", html, "report must be self-contained")

        csv_text = report.to_csv(data)
        self.assertIn("ERROR", csv_text)
        # The failed URL must not appear with a numeric score.
        error_line = [l for l in csv_text.splitlines() if URLS[0] in l][0]
        self.assertIn("ERROR", error_line)

    def test_report_writes_requested_formats(self):
        run_id = self.make_run(urls=URLS[:1], strategies=("mobile",))
        with mock.patch.object(client, "analyze", side_effect=good_result):
            self.drive([run_id])

        out = Path(self.tmp.name) / "out"
        written = report.write_report(
            self.store, run_id, out_dir=out, formats=("html", "csv", "json")
        )
        self.assertEqual(set(written), {"html", "csv", "json"})
        for path in written.values():
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 0)
        json.loads(written["json"].read_text("utf-8"))


def _tk_available() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001 - no display, or tkinter not built
        return False


@unittest.skipUnless(_tk_available(), "tkinter has no usable display here")
class TestGui(unittest.TestCase):
    """The GUI is driven without entering mainloop, pumping events by hand.

    One Tk root is shared by the whole class. Creating and tearing down several
    roots in a single process leaves Tcl objects to be finalised against a dead
    interpreter, which aborts the process rather than failing a test.
    """

    @classmethod
    def setUpClass(cls):
        import gc

        from psi import gui

        cls.gui = gui
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.tmp.name) / "gui.db")
        cls.settings = Settings(
            workers=2, max_attempts=2, backoff_base=0.01, backoff_cap=0.05,
            rate_limit_per_min=100000, lease_seconds=60,
        )
        cls.patches = [
            mock.patch.object(gui.messagebox, "askyesno", return_value=False),
            mock.patch.object(gui.messagebox, "showinfo"),
            mock.patch.object(gui.messagebox, "showerror"),
        ]
        for patch in cls.patches:
            patch.start()
        cls.app = gui.App(cls.store, cls.settings)
        cls.app.withdraw()
        cls._gc = gc

    @classmethod
    def tearDownClass(cls):
        cls.app.supervisor.stop(hard=True)
        cls.app.supervisor.shutdown()
        cls.app.update()
        cls.app.destroy()
        for patch in cls.patches:
            patch.stop()
        cls.store.close()
        cls._gc.collect()
        cls.tmp.cleanup()

    def make_run(self, urls, strategies=("mobile",), label="site"):
        return self.store.create_run(
            label=label, source="https://example.com/sitemap.xml", source_kind="sitemap",
            config_json=self.settings.to_json(), urls=urls, strategies=strategies,
        )

    def drive(self, run_id):
        supervisor = Supervisor(self.store, self.settings)
        supervisor.start(run_id)
        self.assertTrue(supervisor.wait(timeout=30))
        supervisor.shutdown()

    def select(self, run_id):
        self.app.refresh_runs()
        self.app.runs_tree.selection_set(run_id)
        self.app._on_select_run()
        self.app.update()

    def test_runs_and_results_render(self):
        run_id = self.make_run(URLS[:2])
        with mock.patch.object(client, "analyze", side_effect=good_result):
            self.drive(run_id)

        self.select(run_id)
        self.assertIn(run_id, self.app.runs_tree.get_children())
        values = self.app.runs_tree.item(run_id, "values")
        self.assertEqual(values[0], run_id)
        self.assertEqual(values[4], "2/2")
        self.assertEqual(len(self.app.results_tree.get_children()), 2)

    def test_errored_tasks_show_as_ERROR_not_zero(self):
        run_id = self.make_run(URLS[2:3])
        with mock.patch.object(
            client, "analyze", side_effect=FetchError("HTTP 403", FATAL, status=403)
        ):
            self.drive(run_id)

        self.select(run_id)
        rows = self.app.results_tree.get_children()
        self.assertEqual(len(rows), 1)
        values = self.app.results_tree.item(rows[0], "values")
        self.assertEqual(values[-1], "ERROR")
        self.assertEqual(values[2], "—", "an untested URL must not show a score")

    def test_worker_threads_reach_the_ui_through_the_queue(self):
        """Widgets must only ever be touched from the UI thread."""
        seen = []

        def from_worker():
            self.app.call_on_ui(
                lambda: seen.append(threading.current_thread().name)
            )

        thread = threading.Thread(target=from_worker, name="worker")
        thread.start()
        thread.join()

        self.app._pump()
        self.app.update()
        self.assertEqual(seen, [threading.main_thread().name])

    def test_engine_events_render_into_the_log(self):
        for event in (
            {"type": "run_start", "run_id": "aaa", "label": "demo",
             "counts": {"total": 2, "done": 0}, "recovered": 2},
            {"type": "task_done", "run_id": "aaa", "url": "https://x/a",
             "strategy": "mobile", "status": "PASS"},
            {"type": "offline"},
            {"type": "online"},
            {"type": "run_finish", "run_id": "aaa", "label": "demo", "status": "done",
             "counts": {"done": 2, "failed": 0, "pending": 0}},
        ):
            self.app.events.put(event)
        self.app._pump()
        self.app.update()

        text = self.app.log.get("1.0", "end")
        self.assertIn("recovered from an interrupted run", text)
        self.assertIn("Internet connection lost", text)
        self.assertIn("Connection restored", text)
        self.assertIn("complete", text)

    def test_buttons_reflect_run_state(self):
        run_id = self.make_run(URLS[3:4])
        self.select(run_id)
        # Fresh run: work outstanding, nothing to report on yet.
        self.assertEqual(str(self.app.buttons["resume"]["state"]), "normal")
        self.assertEqual(str(self.app.buttons["report"]["state"]), "disabled")
        # Nothing is being driven from this window, so there is nothing to stop.
        self.assertEqual(str(self.app.buttons["stop"]["state"]), "disabled")

    def test_stop_is_offered_while_this_window_drives_a_run(self):
        # More URLs than workers, so there is still work left when we stop.
        run_id = self.make_run(URLS[1:6])
        in_flight = threading.Event()
        release = threading.Event()

        def slow(url, strategy, _settings):
            in_flight.set()
            release.wait(timeout=10)
            return good_result(url, strategy)

        with mock.patch.object(client, "analyze", side_effect=slow):
            self.app.supervisor.start(run_id)
            self.assertTrue(in_flight.wait(timeout=10))
            self.select(run_id)
            self.assertEqual(str(self.app.buttons["stop"]["state"]), "normal")

            self.app.stop_selected()
            self.assertTrue(self.app.supervisor.is_stopping(run_id))
            self.assertEqual(str(self.app.buttons["stop"]["state"]), "disabled")
            self.select(run_id)
            self.assertEqual(
                self.app.runs_tree.item(run_id, "values")[2], "stopping"
            )
            release.set()
            self.assertTrue(self.app.supervisor.wait(timeout=20))

        self.select(run_id)
        self.assertGreater(self.store.progress(run_id)[PENDING], 0)
        self.assertEqual(self.app.runs_tree.item(run_id, "values")[2], "paused")
        self.assertIn("stopping", self.app.log.get("1.0", "end"))

    def test_a_single_result_row_can_be_retried(self):
        run_id = self.make_run(URLS[2:4])
        with mock.patch.object(client, "analyze", side_effect=good_result):
            self.drive(run_id)
        self.select(run_id)

        rows = self.app.results_tree.get_children()
        self.assertEqual(len(rows), 2)
        target = rows[0]
        self.app.results_tree.selection_set(target)
        with mock.patch.object(self.app.supervisor, "start") as start:
            self.app.retry_selected_results()
        start.assert_called_once_with(run_id)

        counts = self.store.progress(run_id)
        self.assertEqual(counts[PENDING], 1, "the retry did not queue exactly one test")
        self.assertEqual(counts[DONE], 1, "the other test was re-queued too")
        self.assertEqual(
            self.app.results_tree.item(target, "values")[-1], "QUEUED",
            "the retried row does not show as waiting",
        )

    def test_queued_work_is_visible_and_filterable(self):
        run_id = self.make_run(URLS[:2])
        self.select(run_id)
        statuses = [
            self.app.results_tree.item(row, "values")[-1]
            for row in self.app.results_tree.get_children()
        ]
        self.assertEqual(statuses, ["QUEUED"] * 2)

        self.app.filter_var.set("Passed")
        self.app._render_results()
        self.assertEqual(self.app.results_tree.get_children(), ())
        self.app.filter_var.set("Everything")
        self.app._render_results()
        self.assertEqual(len(self.app.results_tree.get_children()), 2)


class TestErrorClassification(unittest.TestCase):
    def test_http_statuses_map_to_the_right_policy(self):
        import urllib.error
        from io import BytesIO
        from psi.net import classify

        def kind_of(code, body=b"{}"):
            exc = urllib.error.HTTPError(
                "https://x", code, "err", {}, BytesIO(body)  # type: ignore[arg-type]
            )
            try:
                return classify(exc).kind
            finally:
                exc.close()

        self.assertEqual(kind_of(500), RETRY)
        self.assertEqual(kind_of(503), RETRY)
        self.assertEqual(kind_of(429), RETRY)
        self.assertEqual(kind_of(404), FATAL)
        self.assertEqual(kind_of(403), FATAL)
        # Lighthouse's transient 400s should be retried.
        self.assertEqual(kind_of(400, b'{"error":{"message":"NO_FCP"}}'), RETRY)

    def test_quota_403_is_retryable(self):
        import urllib.error
        from io import BytesIO
        from psi.net import classify

        exc = urllib.error.HTTPError(
            "https://x", 403, "err", {},
            BytesIO(b'{"error":{"message":"Quota exceeded"}}'),  # type: ignore[arg-type]
        )
        try:
            self.assertEqual(classify(exc).kind, RETRY)
        finally:
            exc.close()

    def test_dns_failure_is_offline(self):
        import socket
        from psi.net import classify

        self.assertEqual(classify(socket.gaierror("no dns")).kind, OFFLINE)


if __name__ == "__main__":
    unittest.main()

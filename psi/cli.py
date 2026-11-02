"""Command line interface."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import textwrap
import webbrowser
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from . import __version__, config, report, sitemap
from .console import ConsoleReporter
from .db import DONE, FAILED, PENDING, Store
from .engine import Supervisor
from .net import FetchError


# --------------------------------------------------------------- helpers


def _store() -> Store:
    config.ensure_dirs()
    return Store(config.db_path())


def _settings_from_args(args: argparse.Namespace) -> config.Settings:
    settings = config.load_settings()
    overrides: dict[str, Any] = {}
    for attr in ("workers", "max_concurrent_runs", "max_attempts", "rate_limit_per_min"):
        value = getattr(args, attr, None)
        if value:
            overrides[attr] = value
    if getattr(args, "api_key", None):
        overrides["api_key"] = args.api_key
    if getattr(args, "ca_bundle", None):
        overrides["ca_bundle"] = args.ca_bundle
    if getattr(args, "insecure", False):
        overrides["insecure"] = True
    strategy = getattr(args, "strategy", None)
    if strategy:
        overrides["strategies"] = (
            config.ALL_STRATEGIES if strategy == "both" else (strategy,)
        )
    settings = replace(settings, **overrides) if overrides else settings

    config.apply_tls(settings)
    if settings.insecure:
        print(
            "Warning: TLS verification is off (--insecure). Traffic can be read "
            "or altered in transit; prefer --ca-bundle where you can.",
            file=sys.stderr,
        )
    return settings


def _resolve(store: Store, prefix: str) -> str:
    run_id = store.resolve_run_id(prefix)
    if not run_id:
        raise SystemExit(f"No run matches {prefix!r}. Try `psi list`.")
    return run_id


def _warn_unfinished(store: Store, exclude: Sequence[str] = ()) -> None:
    pending = [r for r in store.resumable_runs() if r["id"] not in exclude]
    if not pending:
        return
    ids = " ".join(r["id"] for r in pending[:5])
    print(
        f"\nNote: {len(pending)} earlier run(s) still have unfinished work.\n"
        f"  Resume with:  psi resume {ids}\n"
        f"  Or all:       psi resume --all",
        file=sys.stderr,
    )


def _install_signals(supervisor: Supervisor) -> None:
    state = {"count": 0}

    def handler(_signum, _frame):
        state["count"] += 1
        if state["count"] == 1:
            print(
                "\nStopping after in-flight tests finish. "
                "Progress is saved — resume with `psi resume --all`. "
                "Press Ctrl+C again to stop immediately.",
                file=sys.stderr,
            )
            supervisor.stop()
        else:
            print("\nStopping now.", file=sys.stderr)
            supervisor.stop(hard=True)

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)


def _drive(store: Store, settings: config.Settings, run_ids: Sequence[str],
           *, verbose: bool = False) -> Supervisor:
    """Run the supervisor to completion with a live console display."""
    reporter = ConsoleReporter(store, verbose=verbose)
    supervisor = Supervisor(store, settings, reporter)
    _install_signals(supervisor)
    reporter.start()
    try:
        for run_id in run_ids:
            supervisor.start(run_id)
        supervisor.wait()
    finally:
        reporter.stop()
        supervisor.shutdown()
    return supervisor


def _emit_reports(store: Store, run_ids: Sequence[str], args: argparse.Namespace) -> None:
    formats = [f.strip() for f in (getattr(args, "format", None) or "html").split(",") if f.strip()]
    out_dir = Path(args.out).expanduser() if getattr(args, "out", None) else config.reports_dir()
    settings = config.load_settings()

    for run_id in run_ids:
        written = report.write_report(store, run_id, out_dir=out_dir, formats=formats)
        for fmt, path in written.items():
            print(f"  {fmt}: {path}")

        if getattr(args, "publish", False):
            token = getattr(args, "github_token", None) or settings.github_token
            repo = getattr(args, "github_repo", None) or settings.github_repo
            html_path = written.get("html")
            if not (token and repo):
                print(
                    "  publish skipped: set github_token and github_repo "
                    "(psi config, or PSI_GITHUB_TOKEN / PSI_GITHUB_REPO)",
                    file=sys.stderr,
                )
            elif html_path is None:
                print("  publish skipped: needs the html format", file=sys.stderr)
            else:
                from .publish import upload_report

                try:
                    links = upload_report(html_path, token=token, repo=repo)
                except (FetchError, ValueError) as exc:
                    print(f"  publish failed: {exc}", file=sys.stderr)
                else:
                    print(f"  shareable link: {links['preview']}")
                    print(f"  raw:            {links['raw']}")

        if getattr(args, "open", False) and "html" in written:
            webbrowser.open(written["html"].resolve().as_uri())


# --------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    store = _store()
    settings = _settings_from_args(args)

    if not settings.api_key:
        print(
            "Warning: no API key set. Google throttles anonymous requests hard.\n"
            "  Set one with:  psi config --api-key YOUR_KEY   (or PSI_API_KEY)\n",
            file=sys.stderr,
        )

    sources: list[tuple[str, str]] = []  # (source, kind)
    for value in args.sitemap or []:
        sources.append((value, "sitemap"))
    if args.batch:
        for line in Path(args.batch).read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                sources.append((line, "sitemap"))
    if args.urls:
        sources.append((args.urls, "urls"))

    if not sources:
        raise SystemExit("Nothing to do: pass --sitemap, --urls or --batch.")

    run_ids: list[str] = []
    for source, kind in sources:
        try:
            if kind == "urls":
                urls = sitemap.read_url_file(source)
                urls = sitemap.filter_urls(urls, include=args.include, exclude=args.exclude)
                if args.limit:
                    urls = urls[: args.limit]
                label = args.label or (sitemap.label_for(urls[0]) if urls else "url-list")
                warnings: list[str] = []
            else:
                result = sitemap.collect_urls(
                    source,
                    timeout=settings.request_timeout,
                    include=args.include,
                    exclude=args.exclude,
                    limit=args.limit,
                )
                urls, warnings = result.urls, result.warnings
                label = args.label or sitemap.label_for(source)
        except (FetchError, OSError, ValueError) as exc:
            print(f"! {source}: {exc}", file=sys.stderr)
            continue

        if not urls:
            print(f"! {source}: no URLs found after filtering", file=sys.stderr)
            continue

        for warning in warnings:
            print(f"  note: {warning}")

        run_id = store.create_run(
            label=label,
            source=source,
            source_kind=kind,
            config_json=settings.to_json(),
            urls=urls,
            strategies=settings.strategies,
        )
        run_ids.append(run_id)
        print(
            f"Created run {run_id} — {label}: {len(urls)} URLs "
            f"x {len(settings.strategies)} device(s) = "
            f"{len(urls) * len(settings.strategies)} tests"
        )

    if not run_ids:
        raise SystemExit("No runs were created.")

    if args.no_start:
        print("\nNot started (--no-start). Begin with: psi resume " + " ".join(run_ids))
        return 0

    print()
    _drive(store, settings, run_ids, verbose=args.verbose)

    print("\nReports:")
    _emit_reports(store, run_ids, args)
    _warn_unfinished(store, exclude=run_ids)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    store = _store()
    settings = _settings_from_args(args)

    if args.all:
        run_ids = [row["id"] for row in store.resumable_runs()]
    else:
        run_ids = [_resolve(store, value) for value in args.run_id]

    if not run_ids:
        print("Nothing to resume — every run is complete.")
        return 0

    for run_id in run_ids:
        counts = store.progress(run_id)
        run = store.get_run(run_id)
        print(
            f"Resuming {run_id} ({run['label']}): "
            f"{counts[PENDING]} pending, {counts[DONE]} done, {counts[FAILED]} failed"
        )

    print()
    _drive(store, settings, run_ids, verbose=args.verbose)

    if args.report:
        print("\nReports:")
        _emit_reports(store, run_ids, args)
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    store = _store()
    settings = _settings_from_args(args)
    run_ids = [_resolve(store, value) for value in args.run_id]

    total = 0
    for run_id in run_ids:
        count = store.requeue_failed(run_id)
        total += count
        print(f"{run_id}: requeued {count} failed test(s)")

    if total == 0:
        print("Nothing to retry.")
        return 0

    if args.no_start:
        return 0

    print()
    _drive(store, settings, run_ids, verbose=args.verbose)

    if args.report:
        print("\nReports:")
        _emit_reports(store, run_ids, args)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = _store()
    runs = store.list_runs(limit=args.limit)
    if not runs:
        print("No runs yet. Start one with:  psi run --sitemap https://example.com/sitemap.xml")
        return 0

    header = f"{'ID':<10} {'LABEL':<24} {'STATUS':<9} {'PROGRESS':<13} {'PASS':>5} {'FAIL':>5} {'ERR':>4}  CREATED"
    print(header)
    print("-" * len(header))
    for run in runs:
        counts = store.progress(run["id"])
        summary = store.result_summary(run["id"])
        finished = counts[DONE] + counts[FAILED]
        status = store.display_status(run)
        created = (run["created_at"] or "")[:16].replace("T", " ")
        progress = "{}/{}".format(finished, counts["total"])
        print(
            f"{run['id']:<10} {run['label'][:23]:<24} {status:<9} "
            f"{progress:<13} "
            f"{summary.get('PASS', 0):>5} {summary.get('FAIL', 0):>5} "
            f"{counts[FAILED]:>4}  {created}"
        )

    _warn_unfinished(store)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _store()
    run_id = _resolve(store, args.run_id)
    run = store.get_run(run_id)
    counts = store.progress(run_id)
    summary = store.result_summary(run_id)

    print(f"Run:      {run_id}  ({run['label']})")
    print(f"Source:   {run['source']}")
    print(f"Status:   {run['status']}")
    print(f"Created:  {run['created_at']}")
    if run["finished_at"]:
        print(f"Finished: {run['finished_at']}")
    print(
        f"Tasks:    {counts['total']} total — {counts[DONE]} done, "
        f"{counts[PENDING]} pending, {counts['running']} in flight, {counts[FAILED]} failed"
    )
    print(f"Results:  {summary.get('PASS', 0)} pass, {summary.get('FAIL', 0)} fail")

    failed = store.failed_tasks(run_id)
    if failed:
        print(f"\nGave up on {len(failed)} test(s) — retry with `psi retry {run_id}`:")
        for row in failed[: args.limit]:
            print(f"  [{row['strategy']:<7}] {row['url']}")
            print(f"            {row['attempts']} attempts — {row['last_error']}")
        if len(failed) > args.limit:
            print(f"  ... and {len(failed) - args.limit} more")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    store = _store()
    _settings_from_args(args)  # installs the TLS policy that --publish needs
    run_ids = [_resolve(store, value) for value in args.run_id]
    print("Reports:")
    _emit_reports(store, run_ids, args)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui import launch
    except ImportError as exc:  # tkinter missing from a slim Python build
        raise SystemExit(
            f"The desktop GUI needs tkinter, which this Python does not have ({exc}).\n"
            "On Windows, re-run the Python installer and tick 'tcl/tk and IDLE'.\n"
            "In the meantime `psi serve` gives you the same thing in a browser."
        )

    store = _store()
    launch(store, _settings_from_args(args))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    store = _store()
    settings = _settings_from_args(args)
    serve(store, settings, host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    store = _store()
    for value in args.run_id:
        run_id = _resolve(store, value)
        store.delete_run(run_id)
        print(f"Deleted {run_id}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    path = config.config_path()

    if not path.exists():
        config.write_default_config()

    data = json.loads(path.read_text("utf-8"))
    changed = False
    for attr, key in (
        ("api_key", "api_key"),
        ("github_token", "github_token"),
        ("github_repo", "github_repo"),
    ):
        value = getattr(args, attr, None)
        if value:
            data[key] = value
            changed = True
    if args.workers:
        data["workers"] = args.workers
        changed = True

    if changed:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"Updated {path}")
    else:
        settings = config.load_settings()
        print(f"Config file: {path}")
        print(f"Data dir:    {config.app_dir()}")
        print(f"Database:    {config.db_path()}")
        print(f"Reports:     {config.reports_dir()}")
        print()
        print(f"API key:      {'set' if settings.api_key else 'NOT SET'}")
        print(f"GitHub token: {'set' if settings.github_token else 'not set'}")
        print(f"GitHub repo:  {settings.github_repo or 'not set'}")
        print(f"Workers:      {settings.workers}")
        print(f"Concurrent runs: {settings.max_concurrent_runs}")
        print(f"Strategies:   {', '.join(settings.strategies)}")
    return 0


# --------------------------------------------------------------- argparse


def _add_report_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", default="html", help="html,csv,json (comma separated)")
    parser.add_argument("--out", help="output directory (default: ~/.psi/reports)")
    parser.add_argument("--open", action="store_true", help="open the HTML report when done")
    parser.add_argument("--publish", action="store_true", help="upload the report to GitHub")
    parser.add_argument("--github-token")
    parser.add_argument("--github-repo")


def _add_engine_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workers", type=int, help="parallel API calls (default 4)")
    parser.add_argument(
        "--max-concurrent-runs", type=int, dest="max_concurrent_runs",
        help="how many sites to work on at once (default 3)",
    )
    parser.add_argument("--max-attempts", type=int, dest="max_attempts",
                        help="attempts before giving up on a test (default 5)")
    parser.add_argument("--rate-limit-per-min", type=int, dest="rate_limit_per_min",
                        help="cap on API requests per minute (default 150)")
    parser.add_argument("--api-key")
    _add_tls_flags(parser)
    parser.add_argument("-v", "--verbose", action="store_true", help="log every test")


def _add_tls_flags(parser: argparse.ArgumentParser) -> None:
    """Needed by every command that makes an HTTPS request."""
    parser.add_argument("--ca-bundle", dest="ca_bundle",
                        help="CA bundle (.pem) to trust — for intercepting proxies")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (last resort; unsafe)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="psi",
        description="PageSpeed Insights sitemap analyzer — resumable, parallel, unattended.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              psi config --api-key AIza...
              psi run --sitemap https://example.com/page-sitemap.xml --open
              psi run --batch sites.txt --workers 6
              psi resume --all
              psi retry a3f9c1
              psi serve
            """
        ),
    )
    parser.add_argument("--version", action="version", version=f"psi {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a new analysis")
    run.add_argument("--sitemap", action="append", help="sitemap URL (repeatable)")
    run.add_argument("--urls", help="file with one URL per line")
    run.add_argument("--batch", help="file with one sitemap URL per line — one run each")
    run.add_argument("--label", help="friendly name for the run")
    run.add_argument("--strategy", choices=("mobile", "desktop", "both"))
    run.add_argument("--include", help="only URLs matching this regex")
    run.add_argument("--exclude", help="skip URLs matching this regex")
    run.add_argument("--limit", type=int, help="cap the number of URLs")
    run.add_argument("--no-start", action="store_true", dest="no_start",
                     help="create the run but do not begin testing")
    _add_engine_flags(run)
    _add_report_flags(run)
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="continue interrupted runs")
    resume.add_argument("run_id", nargs="*", help="run ids (prefixes are fine)")
    resume.add_argument("--all", action="store_true", help="resume everything unfinished")
    resume.add_argument("--report", action="store_true", help="write reports when done")
    _add_engine_flags(resume)
    _add_report_flags(resume)
    resume.set_defaults(func=cmd_resume)

    retry = sub.add_parser("retry", help="re-queue tests that were given up on")
    retry.add_argument("run_id", nargs="+")
    retry.add_argument("--no-start", action="store_true", dest="no_start")
    retry.add_argument("--report", action="store_true")
    _add_engine_flags(retry)
    _add_report_flags(retry)
    retry.set_defaults(func=cmd_retry)

    listing = sub.add_parser("list", help="show recent runs")
    listing.add_argument("--limit", type=int, default=30)
    listing.set_defaults(func=cmd_list)

    status = sub.add_parser("status", help="detail for one run")
    status.add_argument("run_id")
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(func=cmd_status)

    rep = sub.add_parser("report", help="write a report for existing runs")
    rep.add_argument("run_id", nargs="+")
    _add_report_flags(rep)
    _add_tls_flags(rep)  # --publish uploads over HTTPS
    rep.set_defaults(func=cmd_report)

    gui = sub.add_parser("gui", help="open the desktop app")
    _add_engine_flags(gui)
    gui.set_defaults(func=cmd_gui)

    serve = sub.add_parser("serve", help="start the local dashboard in a browser")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--no-open", action="store_true", help="do not open a browser")
    _add_engine_flags(serve)
    serve.set_defaults(func=cmd_serve)

    remove = sub.add_parser("rm", help="delete runs and their results")
    remove.add_argument("run_id", nargs="+")
    remove.set_defaults(func=cmd_rm)

    conf = sub.add_parser("config", help="show or change settings")
    conf.add_argument("--api-key", dest="api_key")
    conf.add_argument("--github-token", dest="github_token")
    conf.add_argument("--github-repo", dest="github_repo")
    conf.add_argument("--workers", type=int)
    conf.set_defaults(func=cmd_config)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved — resume with `psi resume --all`.")
        return 130
    except SystemExit:
        raise
    except FetchError as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""Configuration: where data lives, credentials, and pass/fail thresholds.

Precedence, highest first:
    1. CLI arguments
    2. Environment variables (PSI_API_KEY, PSI_GITHUB_TOKEN, ...)
    3. Config file  (~/.psi/config.json, or $PSI_HOME/config.json)
    4. Built-in defaults
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

# The API quota for a free PageSpeed Insights key is 240 queries/minute and
# 25,000 queries/day. We stay comfortably under the per-minute ceiling so that
# several sites running at once cannot collectively trip it.
DEFAULT_RATE_LIMIT_PER_MIN = 150

ALL_STRATEGIES = ("mobile", "desktop")
ALL_CATEGORIES = ("performance", "accessibility", "best-practices", "seo")


def app_dir() -> Path:
    """Root directory for the database, reports and config."""
    override = os.environ.get("PSI_HOME")
    root = Path(override).expanduser() if override else Path.home() / ".psi"
    return root


def db_path() -> Path:
    return app_dir() / "psi.db"


def reports_dir() -> Path:
    return app_dir() / "reports"


def config_path() -> Path:
    return app_dir() / "config.json"


def ensure_dirs() -> None:
    app_dir().mkdir(parents=True, exist_ok=True)
    reports_dir().mkdir(parents=True, exist_ok=True)


@dataclass
class Thresholds:
    """A test PASSes when every non-null threshold is satisfied.

    Set a field to null/None in config.json to stop enforcing it. The defaults
    reproduce the pass/fail rules the original browser tool used.
    """

    performance: int | None = 90
    accessibility: int | None = 100
    best_practices: int | None = None  # not enforced by default
    seo: int | None = 100
    fcp: float | None = 1.8  # seconds
    lcp: float | None = 2.5  # seconds
    cls: float | None = 0.1  # unitless
    tbt: float | None = None  # seconds, not enforced by default

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Thresholds:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Settings:
    api_key: str | None = None
    github_token: str | None = None
    github_repo: str | None = None

    # Concurrency. `workers` is the number of PageSpeed API calls in flight at
    # once across the whole process; `max_concurrent_runs` caps how many sites
    # are worked on simultaneously.
    workers: int = 4
    max_concurrent_runs: int = 3
    rate_limit_per_min: int = DEFAULT_RATE_LIMIT_PER_MIN

    # Retry behaviour for failed tests.
    max_attempts: int = 5
    backoff_base: float = 5.0
    backoff_cap: float = 300.0

    # A task claimed by a worker is released back to the queue if its lease
    # expires. This is what lets a killed process (power cut) recover: nothing
    # stays stuck in `running` forever.
    lease_seconds: int = 900
    request_timeout: float = 120.0

    # TLS. Needed on networks where a proxy or antivirus intercepts HTTPS.
    ca_bundle: str | None = None
    insecure: bool = False

    strategies: tuple[str, ...] = ALL_STRATEGIES
    categories: tuple[str, ...] = ALL_CATEGORIES
    thresholds: Thresholds = field(default_factory=Thresholds)

    def to_json(self) -> str:
        data = asdict(self)
        data["strategies"] = list(self.strategies)
        data["categories"] = list(self.categories)
        # Credentials never get written into the run record.
        data.pop("api_key", None)
        data.pop("github_token", None)
        return json.dumps(data)

    @classmethod
    def from_json(cls, blob: str | None) -> Settings:
        if not blob:
            return cls()
        try:
            data = json.loads(blob)
        except (TypeError, ValueError):
            return cls()
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        data = dict(data)
        thresholds = data.pop("thresholds", None)
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in data.items() if k in known}
        if "strategies" in kwargs and kwargs["strategies"]:
            kwargs["strategies"] = tuple(kwargs["strategies"])
        if "categories" in kwargs and kwargs["categories"]:
            kwargs["categories"] = tuple(kwargs["categories"])
        settings = cls(**kwargs)
        if isinstance(thresholds, dict):
            settings = replace(settings, thresholds=Thresholds.from_dict(thresholds))
        return settings


def load_settings() -> Settings:
    """Read config.json, then let environment variables override it."""
    settings = Settings()

    path = config_path()
    if path.exists():
        try:
            settings = Settings.from_dict(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Could not read {path}: {exc}")

    env_map = {
        "api_key": "PSI_API_KEY",
        "github_token": "PSI_GITHUB_TOKEN",
        "github_repo": "PSI_GITHUB_REPO",
        "ca_bundle": "PSI_CA_BUNDLE",
    }
    overrides: dict[str, Any] = {}
    for attr, env in env_map.items():
        value = os.environ.get(env)
        if value:
            overrides[attr] = value.strip()

    for attr, env in (("workers", "PSI_WORKERS"), ("max_concurrent_runs", "PSI_MAX_RUNS")):
        value = os.environ.get(env)
        if value and value.isdigit():
            overrides[attr] = int(value)

    if os.environ.get("PSI_INSECURE", "").lower() in ("1", "true", "yes"):
        overrides["insecure"] = True

    return replace(settings, **overrides) if overrides else settings


def apply_tls(settings: Settings) -> None:
    """Install the TLS policy these settings describe."""
    from .net import configure_tls

    configure_tls(ca_bundle=settings.ca_bundle, insecure=settings.insecure)


def write_default_config() -> Path:
    """Create a commented starter config. Returns the path written."""
    ensure_dirs()
    path = config_path()
    settings = Settings()
    data = {
        "_comment": (
            "Credentials can live here or in the PSI_API_KEY / PSI_GITHUB_TOKEN "
            "environment variables. This file is outside your git repository."
        ),
        "api_key": "",
        "github_token": "",
        "github_repo": "",
        "workers": settings.workers,
        "max_concurrent_runs": settings.max_concurrent_runs,
        "rate_limit_per_min": settings.rate_limit_per_min,
        "max_attempts": settings.max_attempts,
        "strategies": list(settings.strategies),
        "thresholds": asdict(settings.thresholds),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path

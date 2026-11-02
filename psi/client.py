"""PageSpeed Insights API client and result interpretation."""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

from .config import Settings, Thresholds
from .net import RETRY, FetchError, fetch_json

API_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Audit ids whose failure is noise for a pass/fail report.
_SKIPPED_DISPLAY_MODES = {"notApplicable", "informative", "manual"}

_CATEGORY_KEYS = {
    "performance": "performance",
    "accessibility": "accessibility",
    "best-practices": "best_practices",
    "seo": "seo",
}


def build_url(url: str, strategy: str, settings: Settings) -> str:
    params = [("url", url), ("strategy", strategy)]
    params.extend(("category", cat) for cat in settings.categories)
    if settings.api_key:
        params.append(("key", settings.api_key))
    return f"{API_ENDPOINT}?{urllib.parse.urlencode(params)}"


def analyze(url: str, strategy: str, settings: Settings) -> dict[str, Any]:
    """Run one PageSpeed test. Raises `FetchError` on failure."""
    started = time.monotonic()
    payload = fetch_json(
        build_url(url, strategy, settings),
        timeout=settings.request_timeout,
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    lighthouse = payload.get("lighthouseResult")
    if not isinstance(lighthouse, dict):
        raise FetchError("Response contained no lighthouseResult", RETRY)

    runtime_error = lighthouse.get("runtimeError")
    if isinstance(runtime_error, dict) and runtime_error.get("code") not in (None, "NO_ERROR"):
        code = runtime_error.get("code")
        message = runtime_error.get("message", "")
        # Lighthouse could not load the page at all. Usually transient
        # (redirects, slow origin, bot protection warming up).
        raise FetchError(f"Lighthouse error {code}: {message}"[:500], RETRY)

    parsed = parse_result(lighthouse, url, strategy, settings.thresholds)
    parsed["duration_ms"] = duration_ms
    return parsed


def _score(categories: dict[str, Any], key: str) -> int | None:
    entry = categories.get(key)
    if not isinstance(entry, dict):
        return None
    score = entry.get("score")
    if score is None:
        return None
    return round(float(score) * 100)


def _numeric(audits: dict[str, Any], key: str, divisor: float = 1000.0) -> float | None:
    audit = audits.get(key)
    if not isinstance(audit, dict):
        return None
    value = audit.get("numericValue")
    if value is None:
        return None
    return round(float(value) / divisor, 3)


def _failing_audits(
    lighthouse: dict[str, Any], category_key: str, max_items: int = 12
) -> list[dict[str, Any]]:
    """List the specific audits dragging a category below 100.

    This is the detail the browser version never surfaced: it reported
    "Accessibility: 92 (required: 100)" without saying which checks failed.
    """
    categories = lighthouse.get("categories", {})
    audits = lighthouse.get("audits", {})
    category = categories.get(category_key)
    if not isinstance(category, dict):
        return []

    failing = []
    for ref in category.get("auditRefs", []):
        audit = audits.get(ref.get("id"))
        if not isinstance(audit, dict):
            continue
        if audit.get("scoreDisplayMode") in _SKIPPED_DISPLAY_MODES:
            continue
        # Zero-weight audits cannot move the category score. Listing them
        # beside a score of 100 reads as a contradiction, so leave them out --
        # this list exists to explain the number.
        if not ref.get("weight"):
            continue
        score = audit.get("score")
        if score is None or float(score) >= 1.0:
            continue
        failing.append(
            {
                "id": ref.get("id"),
                "title": audit.get("title", ref.get("id")),
                "weight": ref.get("weight", 0),
                "score": round(float(score), 2),
            }
        )

    failing.sort(key=lambda item: (-item["weight"], item["score"]))
    return failing[:max_items]


def _opportunities(lighthouse: dict[str, Any], max_items: int = 8) -> list[dict[str, Any]]:
    """Performance opportunities ranked by estimated time saved."""
    audits = lighthouse.get("audits", {})
    found = []
    for audit_id, audit in audits.items():
        if not isinstance(audit, dict):
            continue
        details = audit.get("details")
        if not isinstance(details, dict) or details.get("type") != "opportunity":
            continue
        savings = details.get("overallSavingsMs") or 0
        if savings < 50:
            continue
        found.append(
            {
                "id": audit_id,
                "title": audit.get("title", audit_id),
                "savings_ms": int(savings),
            }
        )
    found.sort(key=lambda item: -item["savings_ms"])
    return found[:max_items]


def parse_result(
    lighthouse: dict[str, Any], url: str, strategy: str, thresholds: Thresholds
) -> dict[str, Any]:
    """Turn a Lighthouse payload into a flat record plus a PASS/FAIL verdict."""
    categories = lighthouse.get("categories", {})
    audits = lighthouse.get("audits", {})

    record: dict[str, Any] = {
        "url": url,
        "strategy": strategy,
        "performance": _score(categories, "performance"),
        "accessibility": _score(categories, "accessibility"),
        "best_practices": _score(categories, "best-practices"),
        "seo": _score(categories, "seo"),
        "fcp": _numeric(audits, "first-contentful-paint"),
        "lcp": _numeric(audits, "largest-contentful-paint"),
        "cls": _numeric(audits, "cumulative-layout-shift", divisor=1.0),
        "tbt": _numeric(audits, "total-blocking-time"),
        "speed_index": _numeric(audits, "speed-index"),
        "lighthouse_version": lighthouse.get("lighthouseVersion"),
    }

    issues = evaluate(record, thresholds)
    record["issues"] = issues
    record["status"] = "FAIL" if issues else "PASS"
    record["details"] = {
        "failing_accessibility": _failing_audits(lighthouse, "accessibility"),
        "failing_seo": _failing_audits(lighthouse, "seo"),
        "failing_best_practices": _failing_audits(lighthouse, "best-practices"),
        "opportunities": _opportunities(lighthouse),
        "final_url": lighthouse.get("finalUrl") or lighthouse.get("requestedUrl"),
    }
    return record


def evaluate(record: dict[str, Any], thresholds: Thresholds) -> list[str]:
    """Return a list of threshold violations. Empty means PASS."""
    issues: list[str] = []

    score_checks = (
        ("performance", thresholds.performance, "Performance"),
        ("accessibility", thresholds.accessibility, "Accessibility"),
        ("best_practices", thresholds.best_practices, "Best Practices"),
        ("seo", thresholds.seo, "SEO"),
    )
    for key, limit, label in score_checks:
        if limit is None:
            continue
        value = record.get(key)
        if value is None:
            issues.append(f"{label}: not reported")
        elif value < limit:
            issues.append(f"{label}: {value} (required: ≥{limit})")

    metric_checks = (
        ("fcp", thresholds.fcp, "FCP", "s"),
        ("lcp", thresholds.lcp, "LCP", "s"),
        ("cls", thresholds.cls, "CLS", ""),
        ("tbt", thresholds.tbt, "TBT", "s"),
    )
    for key, limit, label, unit in metric_checks:
        if limit is None:
            continue
        value = record.get(key)
        if value is None:
            continue
        if value > limit:
            issues.append(f"{label}: {value}{unit} (required: ≤{limit}{unit})")

    return issues


def psi_web_link(url: str, strategy: str) -> str:
    """Deep link to the interactive report on pagespeed.web.dev."""
    query = urllib.parse.urlencode({"url": url, "form_factor": strategy})
    return f"https://pagespeed.web.dev/analysis?{query}"

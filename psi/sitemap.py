"""Sitemap discovery and parsing.

Running server-side removes the CORS problem entirely, so none of the proxy
juggling the browser version needed is required here. This module also handles
things the browser version could not: gzipped sitemaps, nested sitemap index
files, and discovery via robots.txt.
"""

from __future__ import annotations

import gzip
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .net import FetchError, RETRY, fetch

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
MAX_SITEMAP_DEPTH = 5


@dataclass
class SitemapResult:
    urls: list[str] = field(default_factory=list)
    sitemaps_read: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _decompress_if_needed(payload: bytes, url: str) -> bytes:
    if payload[:2] == b"\x1f\x8b" or url.endswith(".gz"):
        try:
            return gzip.decompress(payload)
        except OSError:
            return payload
    return payload


def _strip_bom(payload: bytes) -> bytes:
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload[3:]
    return payload.lstrip()


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap_xml(payload: bytes) -> tuple[list[str], list[str]]:
    """Return (page urls, nested sitemap urls) from one sitemap document."""
    try:
        root = ET.fromstring(_strip_bom(payload))
    except ET.ParseError as exc:
        raise FetchError(f"Sitemap is not valid XML: {exc}", RETRY) from exc

    pages: list[str] = []
    nested: list[str] = []
    root_name = _localname(root.tag)

    for child in root:
        name = _localname(child.tag)
        loc_el = None
        for sub in child:
            if _localname(sub.tag) == "loc":
                loc_el = sub
                break
        if loc_el is None or not (loc_el.text or "").strip():
            continue
        loc = loc_el.text.strip()
        if name == "sitemap" or root_name == "sitemapindex":
            nested.append(loc)
        else:
            pages.append(loc)

    return pages, nested


def discover_from_robots(site_url: str, *, timeout: float = 30.0) -> list[str]:
    """Look for `Sitemap:` directives in robots.txt."""
    parsed = urllib.parse.urlparse(site_url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    try:
        payload = fetch(robots_url, timeout=timeout)
    except FetchError:
        return []
    text = payload.decode("utf-8", "replace")
    return re.findall(r"(?im)^\s*sitemap:\s*(\S+)\s*$", text)


def collect_urls(
    source: str,
    *,
    timeout: float = 60.0,
    include: str | None = None,
    exclude: str | None = None,
    limit: int | None = None,
) -> SitemapResult:
    """Resolve a sitemap URL (or site root) into a de-duplicated list of pages.

    Follows sitemap index files, and falls back to robots.txt discovery when
    handed a bare domain.
    """
    result = SitemapResult()
    seen_sitemaps: set[str] = set()
    seen_urls: set[str] = set()

    queue: list[tuple[str, int]] = [(source, 0)]

    # A bare origin has no sitemap path; ask robots.txt where to look.
    parsed = urllib.parse.urlparse(source)
    if parsed.path in ("", "/"):
        discovered = discover_from_robots(source, timeout=timeout)
        if discovered:
            queue = [(url, 0) for url in discovered]
            result.warnings.append(
                f"Discovered {len(discovered)} sitemap(s) via robots.txt"
            )
        else:
            guess = urllib.parse.urljoin(source, "/sitemap.xml")
            queue = [(guess, 0)]
            result.warnings.append(f"No robots.txt sitemap entry; trying {guess}")

    while queue:
        url, depth = queue.pop(0)
        if url in seen_sitemaps:
            continue
        seen_sitemaps.add(url)

        if depth > MAX_SITEMAP_DEPTH:
            result.warnings.append(f"Stopped at nesting depth {depth}: {url}")
            continue

        payload = _decompress_if_needed(fetch(url, timeout=timeout), url)
        pages, nested = parse_sitemap_xml(payload)
        result.sitemaps_read.append(url)

        for page in pages:
            if page not in seen_urls:
                seen_urls.add(page)
                result.urls.append(page)

        for child in nested:
            queue.append((child, depth + 1))

    result.urls = filter_urls(result.urls, include=include, exclude=exclude)

    if limit is not None and len(result.urls) > limit:
        result.warnings.append(f"Truncated to the first {limit} of {len(result.urls)} URLs")
        result.urls = result.urls[:limit]

    return result


def filter_urls(
    urls: list[str], *, include: str | None = None, exclude: str | None = None
) -> list[str]:
    """Apply optional include/exclude regular expressions."""
    filtered = urls
    if include:
        pattern = re.compile(include)
        filtered = [u for u in filtered if pattern.search(u)]
    if exclude:
        pattern = re.compile(exclude)
        filtered = [u for u in filtered if not pattern.search(u)]
    return filtered


def read_url_file(path: str) -> list[str]:
    """Read a newline-delimited URL list, ignoring blanks and `#` comments."""
    with open(path, "r", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle]
    urls = []
    seen: set[str] = set()
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("http://", "https://")):
            continue
        if line not in seen:
            seen.add(line)
            urls.append(line)
    return urls


def label_for(source: str) -> str:
    """A short human-friendly name for a run, derived from its source."""
    try:
        host = urllib.parse.urlparse(source).netloc
    except ValueError:
        host = ""
    if not host:
        return source[:40]
    return host[4:] if host.startswith("www.") else host

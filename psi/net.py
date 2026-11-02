"""HTTP helpers, error classification and rate limiting.

Everything here is stdlib. The important part is `classify`, which decides
whether a failure is worth retrying. Getting that right is what turns a flaky
20-minute run into one that finishes unattended.
"""

from __future__ import annotations

import gzip
import json
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

USER_AGENT = "psi-sitemap-analyzer/2.0 (+https://github.com/agskanchana-sam/performance-eval)"

# TLS setup. Corporate proxies and some antivirus products intercept HTTPS with
# their own CA, which may not validate under modern OpenSSL. `configure_tls`
# lets the user point at that CA's bundle, or -- as a last resort -- turn
# verification off for this tool only.
_tls_context: ssl.SSLContext | None = None
_tls_lock = threading.Lock()

TLS_HELP = (
    "TLS certificate verification failed. This usually means a proxy or "
    "antivirus is intercepting HTTPS.\n"
    "  Point psi at that CA:   set PSI_CA_BUNDLE to the .pem file (or SSL_CERT_FILE)\n"
    "  Or skip verification:   psi ... --insecure   (traffic can then be read in transit)"
)


def configure_tls(*, ca_bundle: str | None = None, insecure: bool = False) -> None:
    """Install the TLS context every request in this process will use."""
    global _tls_context
    with _tls_lock:
        if insecure:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        elif ca_bundle:
            context = ssl.create_default_context(cafile=ca_bundle)
        else:
            context = ssl.create_default_context()
        _tls_context = context


def tls_context() -> ssl.SSLContext:
    with _tls_lock:
        if _tls_context is None:
            return ssl.create_default_context()
        return _tls_context

# How a failure should be handled.
RETRY = "retry"  # transient; try again after a backoff
FATAL = "fatal"  # permanent; retrying will not help
OFFLINE = "offline"  # the network itself is down; pause the whole run


@dataclass
class FetchError(Exception):
    """A failed HTTP attempt, already classified."""

    message: str
    kind: str  # RETRY | FATAL | OFFLINE
    status: int | None = None
    retry_after: float | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def _is_offline_error(exc: BaseException) -> bool:
    """Distinguish 'my internet is gone' from 'that one host misbehaved'."""
    if isinstance(exc, socket.gaierror):
        return True  # DNS resolution failed
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            return True
        if isinstance(reason, ConnectionError):
            return True
        if isinstance(reason, OSError) and reason.errno in (
            10051,  # WSAENETUNREACH (Windows)
            10065,  # WSAEHOSTUNREACH
            101,  # ENETUNREACH
            113,  # EHOSTUNREACH
        ):
            return True
    return isinstance(exc, ConnectionError)


def classify(exc: BaseException) -> FetchError:
    """Turn an exception into a retry decision."""
    if isinstance(exc, FetchError):
        return exc

    if isinstance(exc, urllib.error.HTTPError):
        status = exc.code
        try:
            raw = exc.read()
            # Error bodies honour Accept-Encoding too, so they arrive gzipped.
            if raw[:2] == b"\x1f\x8b":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            body = raw.decode("utf-8", "replace")[:1000]
        except Exception:  # noqa: BLE001 - body is best-effort context only
            body = ""
        message = f"HTTP {status}"
        detail = _extract_api_message(body)
        if detail:
            message = f"HTTP {status}: {detail}"

        retry_after = None
        raw_retry = exc.headers.get("Retry-After") if exc.headers else None
        if raw_retry:
            try:
                retry_after = float(raw_retry)
            except ValueError:
                retry_after = None

        if status in (401, 403):
            # Bad or unauthorised API key. Retrying burns quota for nothing --
            # unless it is a rate-limit dressed up as 403, which Google does.
            if "quota" in body.lower() or "rate limit" in body.lower():
                return FetchError(message, RETRY, status, retry_after or 60.0)
            return FetchError(message, FATAL, status)
        if status == 404:
            return FetchError(message, FATAL, status)
        if status == 400:
            # PageSpeed returns 400 for genuinely bad input *and* for transient
            # Lighthouse failures (ERRORED_DOCUMENT_REQUEST, NO_FCP, timeouts).
            # The latter usually succeed on a second attempt, so retry unless
            # the URL itself is rejected.
            lowered = body.lower()
            if "url" in lowered and ("invalid" in lowered or "malformed" in lowered):
                return FetchError(message, FATAL, status)
            return FetchError(message, RETRY, status)
        if status == 429:
            return FetchError(message, RETRY, status, retry_after or 60.0)
        if 500 <= status < 600:
            return FetchError(message, RETRY, status, retry_after)
        return FetchError(message, RETRY, status, retry_after)

    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return FetchError("Request timed out", RETRY)

    if _is_offline_error(exc):
        return FetchError(f"Network unreachable: {exc}", OFFLINE)

    if isinstance(exc, ssl.SSLCertVerificationError) or (
        isinstance(exc, urllib.error.URLError)
        and isinstance(exc.reason, ssl.SSLCertVerificationError)
    ):
        # Retrying cannot fix a broken trust chain, and burning every task's
        # attempts on it would bury the real cause in a wall of failures.
        return FetchError(f"{exc}\n{TLS_HELP}", FATAL)

    if isinstance(exc, ssl.SSLError):
        return FetchError(f"TLS error: {exc}", RETRY)

    if isinstance(exc, urllib.error.URLError):
        return FetchError(f"Connection error: {exc.reason}", RETRY)

    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return FetchError(f"Malformed response: {exc}", RETRY)

    return FetchError(f"{type(exc).__name__}: {exc}", RETRY)


def _extract_api_message(body: str) -> str | None:
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return body.strip()[:300] or None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message", ""))[:300] or None
    return None


def fetch(
    url: str,
    *,
    timeout: float = 60.0,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: bytes | None = None,
) -> bytes:
    """Perform one HTTP request, raising a classified `FetchError` on failure."""
    request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as response:
            payload = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                payload = gzip.decompress(payload)
            return payload
    except Exception as exc:  # noqa: BLE001 - re-raised as a classified error
        raise classify(exc) from exc


def fetch_json(url: str, **kwargs: Any) -> dict[str, Any]:
    payload = fetch(url, **kwargs)
    try:
        return json.loads(payload.decode("utf-8", "replace"))
    except ValueError as exc:
        raise FetchError(f"Response was not valid JSON: {exc}", RETRY) from exc


def internet_available(timeout: float = 5.0) -> bool:
    """Cheap connectivity probe used to decide when to un-pause a run."""
    for host in ("www.googleapis.com", "dns.google"):
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def backoff_delay(attempt: int, *, base: float, cap: float, jitter: float = 0.3) -> float:
    """Exponential backoff with jitter, so parallel retries do not synchronise."""
    raw = min(base * (2 ** max(0, attempt - 1)), cap)
    spread = raw * jitter
    return max(1.0, raw + random.uniform(-spread, spread))


class RateLimiter:
    """Token bucket shared by every worker in the process.

    With several sites running at once this is what keeps their combined
    request rate under the API's per-minute quota.
    """

    def __init__(self, per_minute: int):
        self.capacity = max(1, per_minute)
        self.rate = self.capacity / 60.0  # tokens per second
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: int = 1) -> None:
        """Block until `tokens` are available."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            time.sleep(min(wait, 5.0))

    def penalise(self, seconds: float) -> None:
        """Drain the bucket after a 429 so everyone slows down together."""
        with self._lock:
            self._tokens = min(self._tokens, 0.0) - seconds * self.rate

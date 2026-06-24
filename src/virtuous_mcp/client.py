"""HTTP client for the Virtuous CRM+ API.

The client can perform both read and write requests, but it clearly
distinguishes between them:

* GET requests, and POST requests to ``/Query``, ``/QueryOptions``,
  ``/Search``, ``/Find``, and ``/Proximity`` paths are treated as **reads**.
* Everything else (other POST, PUT, PATCH, DELETE) is treated as a **write**
  that mutates data in Virtuous.

Write requests must be explicitly authorized at call time by passing
``confirmed=True`` to :meth:`VirtuousClient.request`. If a write is attempted
without that flag, the client raises :class:`ConfirmationRequired` and performs
no network call. This is the low-level backstop that enforces the policy: the
MCP layer must obtain explicit user confirmation before ever passing
``confirmed=True``.

OPERATIONAL PROTOCOLS (aligned with the documented Virtuous API behavior)
=========================================================================
* **Connection pooling** — a single ``httpx.AsyncClient`` is shared across all
  requests (per base URL) so TLS/keep-alive connections are reused instead of
  re-established on every call.
* **Rate limiting** — Virtuous enforces an org-wide limit (documented at
  5,000 requests/hour) and returns ``X-RateLimit-Limit``,
  ``X-RateLimit-Remaining`` and ``X-RateLimit-Reset`` headers on every
  response. The client records the most recent values (see
  :meth:`VirtuousClient.last_rate_limit`) so they can be surfaced to the user.
* **Retry + backoff** — transient failures (HTTP 429 and 5xx) are retried a
  few times. For 429 the wait honors ``Retry-After`` / ``X-RateLimit-Reset``;
  otherwise an exponential backoff is used. This keeps the integration polite
  to the shared org-wide budget.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.virtuoussoftware.com"

# Per Virtuous, Query endpoints cap "take" at 1000 records per call.
MAX_TAKE = 1000

DEFAULT_TIMEOUT = 30.0

# Retry policy for transient failures (429 + 5xx). Reads and (confirmed) writes
# are both retried; the methods used by Virtuous writes are idempotent enough in
# practice that a retry after a 429/5xx is safe (the original call never reached
# a success response).
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5  # seconds; doubled each attempt
MAX_BACKOFF = 60.0  # never wait longer than this between attempts
RETRYABLE_STATUS = (429, 500, 502, 503, 504)

# POST endpoints that only read data (no mutation) despite using POST.
_READ_ONLY_POST_SUFFIXES = ("/Query", "/QueryOptions", "/Search", "/Find", "/Proximity")
# Path segments that indicate a read-only POST query variant, e.g.
# /api/Contact/Query/FullContact.
_READ_ONLY_POST_SEGMENTS = ("/Query/",)


class ConfirmationRequired(RuntimeError):
    """Raised when a mutating request is attempted without explicit confirmation."""


class VirtuousError(RuntimeError):
    """Raised when the Virtuous API returns an error response."""


def _clean_path(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/")


def is_read_request(method: str, path: str) -> bool:
    """Classify whether a request only reads data (does not mutate)."""
    method = method.upper()
    if method == "GET":
        return True
    if method == "POST":
        clean = _clean_path(path)
        if clean.endswith(_READ_ONLY_POST_SUFFIXES):
            return True
        if any(seg in path for seg in _READ_ONLY_POST_SEGMENTS):
            return True
    return False


# -- Shared connection pool ----------------------------------------------------
# A single AsyncClient per base URL is reused for the life of the process so we
# benefit from HTTP keep-alive and connection pooling instead of paying for a
# fresh TLS handshake on every request.
_shared_clients: dict[str, httpx.AsyncClient] = {}


def _shared_client(base_url: str, timeout: float) -> httpx.AsyncClient:
    client = _shared_clients.get(base_url)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
        _shared_clients[base_url] = client
    return client


# -- Rate-limit tracking -------------------------------------------------------
# The most recent rate-limit headers seen on any response, so the MCP can show
# the user how much of the org-wide hourly budget remains.
_last_rate_limit: dict[str, Any] = {}


def _record_rate_limit(headers: httpx.Headers) -> None:
    limit = headers.get("X-RateLimit-Limit")
    remaining = headers.get("X-RateLimit-Remaining")
    reset = headers.get("X-RateLimit-Reset")
    if limit is None and remaining is None and reset is None:
        return
    snapshot: dict[str, Any] = {"observed_at": datetime.now(timezone.utc).isoformat()}
    if limit is not None:
        snapshot["limit"] = _as_int(limit)
    if remaining is not None:
        snapshot["remaining"] = _as_int(remaining)
    if reset is not None:
        snapshot["reset_raw"] = reset
        reset_dt = _reset_to_datetime(reset)
        if reset_dt is not None:
            snapshot["reset_at"] = reset_dt.isoformat()
            snapshot["seconds_until_reset"] = max(
                0, round(reset_dt.timestamp() - time.time())
            )
    _last_rate_limit.clear()
    _last_rate_limit.update(snapshot)


def _as_int(value: str) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _reset_to_datetime(reset: str) -> datetime | None:
    """Interpret an X-RateLimit-Reset value, which may be a unix timestamp."""
    try:
        ts = float(reset)
    except (TypeError, ValueError):
        return None
    # Heuristic: values far in the future are unix seconds; very large ones ms.
    if ts > 1e12:
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _retry_wait(resp: httpx.Response, attempt: int) -> float:
    """Compute how long to wait before retrying a transient failure."""
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF)
        except ValueError:
            pass
    if resp.status_code == 429:
        reset = resp.headers.get("X-RateLimit-Reset")
        reset_dt = _reset_to_datetime(reset) if reset else None
        if reset_dt is not None:
            delta = reset_dt.timestamp() - time.time()
            if delta > 0:
                return min(delta, MAX_BACKOFF)
    # Exponential backoff with a little jitter to avoid thundering herds.
    backoff = RETRY_BACKOFF_BASE * (2**attempt)
    return min(backoff + random.uniform(0, RETRY_BACKOFF_BASE), MAX_BACKOFF)


class VirtuousClient:
    """Async client for the Virtuous API supporting reads and (gated) writes."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key or os.environ.get("VIRTUOUS_API_KEY", "")
        if not self.api_key:
            raise VirtuousError(
                "Missing Virtuous API key. Set the VIRTUOUS_API_KEY environment variable."
            )
        self.base_url = (
            base_url or os.environ.get("VIRTUOUS_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def last_rate_limit() -> dict[str, Any]:
        """Return the most recently observed rate-limit headers (a snapshot).

        Empty until at least one request has been made. Includes ``limit``,
        ``remaining``, ``reset_at``, and ``seconds_until_reset`` when available.
        """
        return dict(_last_rate_limit)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        confirmed: bool = False,
    ) -> Any:
        """Perform an HTTP request against the Virtuous API.

        Read requests run freely. Write/mutating requests require
        ``confirmed=True`` or a :class:`ConfirmationRequired` error is raised
        before any network call is made.

        Transient failures (HTTP 429 and 5xx) are retried up to
        :data:`MAX_RETRIES` times with backoff. Rate-limit headers from the
        final response are recorded for later inspection.
        """
        method = method.upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise VirtuousError(f"Unsupported HTTP method: {method}")

        if not is_read_request(method, path) and not confirmed:
            raise ConfirmationRequired(
                f"{method} {path} modifies data in Virtuous and requires explicit "
                "user confirmation before it can run."
            )

        client = _shared_client(self.base_url, self._timeout)
        rel = "/" + path.lstrip("/")

        attempt = 0
        while True:
            try:
                resp = await client.request(
                    method,
                    rel,
                    headers=self._headers(),
                    params=params,
                    json=json,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as e:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(_retry_wait_for_exception(attempt))
                    attempt += 1
                    continue
                raise VirtuousError(f"Virtuous API {method} {path} timed out: {e}")
            except httpx.HTTPError as e:
                raise VirtuousError(f"Virtuous API {method} {path} request error: {e}")

            _record_rate_limit(resp.headers)

            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                await asyncio.sleep(_retry_wait(resp, attempt))
                attempt += 1
                continue

            if resp.status_code >= 400:
                raise VirtuousError(self._error_message(method, path, resp))

            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text

    def _error_message(self, method: str, path: str, resp: httpx.Response) -> str:
        detail: Any = resp.text
        try:
            detail = resp.json()
        except ValueError:
            pass
        msg = f"Virtuous API {method} {path} failed ({resp.status_code}): {detail}"
        if resp.status_code == 429:
            rl = self.last_rate_limit()
            secs = rl.get("seconds_until_reset")
            if secs is not None:
                msg += (
                    f" — rate limit exceeded (org-wide budget); resets in ~{secs}s "
                    f"(at {rl.get('reset_at')}). Prefer batch/bulk endpoints and "
                    "fewer, larger queries."
                )
        return msg

    # -- Convenience read helpers -------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    def _query_path(self, object_type: str, full: bool) -> str:
        path = f"/api/{object_type}/Query"
        if full:
            full_suffix = {
                "Contact": "/FullContact",
                "Gift": "/FullGift",
            }.get(object_type)
            if full_suffix:
                path += full_suffix
        return path

    async def query(
        self,
        object_type: str,
        body: dict[str, Any],
        *,
        skip: int = 0,
        take: int = 100,
        full: bool = False,
    ) -> Any:
        take = max(1, min(int(take), MAX_TAKE))
        skip = max(0, int(skip))
        return await self.request(
            "POST",
            self._query_path(object_type, full),
            params={"skip": skip, "take": take},
            json=body,
        )

    async def query_options(self, object_type: str) -> Any:
        return await self.get(f"/api/{object_type}/QueryOptions")

    async def get_record(self, object_type: str, record_id: str | int) -> Any:
        return await self.get(f"/api/{object_type}/{record_id}")


def _retry_wait_for_exception(attempt: int) -> float:
    backoff = RETRY_BACKOFF_BASE * (2**attempt)
    return min(backoff + random.uniform(0, RETRY_BACKOFF_BASE), MAX_BACKOFF)

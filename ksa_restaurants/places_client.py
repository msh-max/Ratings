"""Async HTTP client for the Google Places API (New), with rate limiting and retries.

The API key is read from ``GOOGLE_MAPS_API_KEY`` and is only ever placed in the
``X-Goog-Api-Key`` request header; it is never logged, persisted or put in an error message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any, Protocol

import httpx

BASE_URL = "https://places.googleapis.com/v1"
RETRY_STATUSES = {429, 500, 502, 503, 504}
log = logging.getLogger(__name__)

_HINTS = {
    400: "INVALID_ARGUMENT: the request was rejected. Most often a field name in the field mask "
         "is not recognised (remove it with `exclude_fields` in a --config JSON file, no code "
         "change needed) or a request parameter is malformed.",
    401: "UNAUTHENTICATED: the API key is missing or invalid. Check GOOGLE_MAPS_API_KEY.",
    403: "PERMISSION_DENIED: the key is invalid or restricted, 'Places API (New)' is not enabled "
         "for the key's Cloud project, or billing is not enabled on that project.",
    404: "NOT_FOUND: the place no longer exists (removed or merged).",
}


class PlacesApiError(RuntimeError):
    """A non-retryable (or retries-exhausted) error from the Places API."""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code = status, code
        super().__init__(f"HTTP {status} {code}: {message}")


class PlaceNotFound(PlacesApiError):
    """Place Details returned 404 / NOT_FOUND."""


class PlacesApi(Protocol):
    """Interface shared by :class:`PlacesClient` and the fake client."""

    async def search_text(self, body: dict[str, Any], mask: str) -> dict[str, Any]: ...
    async def search_nearby(self, body: dict[str, Any], mask: str) -> dict[str, Any]: ...
    async def place_details(self, place_id: str, mask: str, language: str,
                            region: str) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


class TokenBucket:
    """Simple async token bucket limiting the request rate to ``qps`` per second."""

    def __init__(self, qps: float) -> None:
        self.rate = max(qps, 0.001)
        self.tokens = 1.0
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until one token is available and take it."""
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def api_error(status: int, payload: Any, secret: str = "") -> PlacesApiError:
    """Build a :class:`PlacesApiError` with a hint about the likely cause (key redacted)."""
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = str(err.get("status") or status)
    google = str(err.get("message") or "")
    if secret:
        google = google.replace(secret, "***")
    hint = _HINTS.get(status, "Unexpected error from the Places API.")
    cls = PlaceNotFound if status == 404 else PlacesApiError
    return cls(status, code, f"{hint} Google said: {google!r}")


class PlacesClient:
    """Real Places API (New) client: semaphore + token bucket + retries with backoff."""

    def __init__(self, api_key: str | None = None, qps: float = 9.0, concurrency: int = 8,
                 max_attempts: int = 6, backoff_base: float = 0.5, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        key = api_key if api_key is not None else os.environ.get("GOOGLE_MAPS_API_KEY", "")
        if not key:
            raise PlacesApiError(401, "NO_API_KEY",
                                 "GOOGLE_MAPS_API_KEY is not set (use --fake to run without a key).")
        self._key = key
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout, transport=transport,
                                       headers={"X-Goog-Api-Key": key})
        self._sem = asyncio.Semaphore(concurrency)
        self._bucket = TokenBucket(qps)
        self.max_attempts, self.backoff_base = max_attempts, backoff_base

    async def search_text(self, body: dict[str, Any], mask: str) -> dict[str, Any]:
        """POST ``places:searchText``. Follow-up pages must repeat ``body`` plus ``pageToken``."""
        return await self._request("POST", "/places:searchText", mask, json=body)

    async def search_nearby(self, body: dict[str, Any], mask: str) -> dict[str, Any]:
        """POST ``places:searchNearby``."""
        return await self._request("POST", "/places:searchNearby", mask, json=body)

    async def place_details(self, place_id: str, mask: str, language: str,
                            region: str) -> dict[str, Any]:
        """GET ``places/{id}`` (mask without the ``places.`` prefix)."""
        params = {"languageCode": language, "regionCode": region}
        return await self._request("GET", f"/places/{place_id}", mask, params=params)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def _request(self, method: str, path: str, mask: str, **kwargs: Any) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            if attempt:
                delay = min(30.0, self.backoff_base * 2 ** (attempt - 1)) * (0.5 + random.random())
                log.warning("retrying %s %s in %.1fs (%s)", method, path, delay, last)
                await asyncio.sleep(delay)
            async with self._sem:
                await self._bucket.acquire()
                try:
                    resp = await self._http.request(method, path, headers={"X-Goog-FieldMask": mask},
                                                    **kwargs)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last = exc
                    continue
            if resp.status_code == 200:
                return resp.json()
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            last = api_error(resp.status_code, payload, self._key)
            if resp.status_code not in RETRY_STATUSES:
                raise last
        if isinstance(last, PlacesApiError):
            raise last
        raise PlacesApiError(0, "NETWORK", f"giving up after {self.max_attempts} attempts: "
                             f"{type(last).__name__}")

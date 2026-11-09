"""Per-run context shared by the phases: DB connection, config, client and call metering."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .config import Config
from .cost import CostModel, FreeCapExhausted, detect_sku, usable_cap
from .places_client import PlacesApi
from .runs import usage_for_guard

T = TypeVar("T")
log = logging.getLogger("ksa_restaurants")


@dataclass
class RunContext:
    """Everything a phase needs. API calls go through the metered helpers below."""

    conn: sqlite3.Connection
    run_id: str
    cfg: Config
    client: PlacesApi
    cost: CostModel = field(init=False)
    _in_flight: Counter[str] = field(init=False, default_factory=Counter)

    def __post_init__(self) -> None:
        self.cost = CostModel(self.cfg.pricing)

    def _reserve(self, endpoint: str, mask: str) -> str:
        """Hard free-cap guard, run before EVERY metered call. Returns the ``endpoint/sku`` key.

        In free mode a call to a capped SKU is allowed only while month-to-date usage (all runs
        started this UTC month, read from ``api_calls``) plus calls in flight is below
        floor(cap x margin); otherwise :class:`FreeCapExhausted` is raised and nothing is sent.
        The planner never schedules beyond the caps; this is the last line of defence.
        """
        key = f"{endpoint}/{detect_sku(endpoint, mask)}"
        if self.cfg.free_only:
            cap = usable_cap(self.cfg, key)
            if cap is not None:
                used = usage_for_guard(self.conn, self.run_id).get(key, 0) + self._in_flight[key]
                if used >= cap:
                    raise FreeCapExhausted(key)
        self._in_flight[key] += 1
        return key

    async def _metered(self, phase: str, endpoint: str, mask: str,
                       call: Callable[[], Awaitable[T]]) -> T:
        key = self._reserve(endpoint, mask)  # before the request exists: nothing is sent on refusal
        try:
            resp = await call()
            self.record(phase, endpoint, key.split("/", 1)[1])
            return resp
        finally:
            self._in_flight[key] -= 1

    def record(self, phase: str, endpoint: str, sku: str, n: int = 1) -> None:
        """Add ``n`` successful calls to ``api_calls`` (committed immediately)."""
        self.conn.execute(
            "INSERT INTO api_calls(run_id, phase, endpoint, sku, calls) VALUES (?,?,?,?,?) "
            "ON CONFLICT(run_id, phase, endpoint, sku) DO UPDATE SET calls = calls + excluded.calls",
            (self.run_id, phase, endpoint, sku, n))

    def call_counts(self) -> dict[tuple[str, str], int]:
        """Calls so far in this run per (endpoint, sku)."""
        counts: dict[tuple[str, str], int] = {}
        for r in self.conn.execute("SELECT endpoint, sku, SUM(calls) FROM api_calls WHERE run_id=? "
                                   "GROUP BY endpoint, sku", (self.run_id,)):
            counts[(r[0], r[1])] = r[2]
        return counts

    def progress(self) -> str:
        """``calls=N cost=$X`` for log lines."""
        counts = self.call_counts()
        return f"calls={sum(counts.values())} cost=${self.cost.totals(counts)[0]:.2f}"

    async def search_text(self, phase: str, body: dict[str, Any], mask: str) -> dict[str, Any]:
        """Metered (and, in free mode, cap-guarded) Text Search."""
        return await self._metered(phase, "text_search", mask, lambda: self.client.search_text(body, mask))

    async def search_text_all(self, phase: str, body: dict[str, Any],
                              mask: str) -> tuple[list[dict[str, Any]], int, bool]:
        """All pages (max 3) of a Text Search: (places, pages, token_after_last_page).

        Every follow-up page repeats every original parameter plus ``pageToken``.
        """
        places: list[dict[str, Any]] = []
        token: str | None = None
        pages = 0
        while pages < 3:
            req = dict(body) if token is None else {**body, "pageToken": token}
            resp = await self.search_text(phase, req, mask)
            pages += 1
            places.extend(resp.get("places", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return places, pages, bool(token)

    async def search_nearby(self, phase: str, body: dict[str, Any], mask: str) -> dict[str, Any]:
        """Metered (and, in free mode, cap-guarded) Nearby Search."""
        return await self._metered(phase, "nearby_search", mask,
                                   lambda: self.client.search_nearby(body, mask))

    async def place_details(self, phase: str, place_id: str, mask: str) -> dict[str, Any]:
        """Metered (and, in free mode, cap-guarded) Place Details."""
        return await self._metered(phase, "place_details", mask, lambda: self.client.place_details(
            place_id, mask, self.cfg.language, self.cfg.region_code))


async def run_all(coros: Iterable[Awaitable[T]]) -> list[T]:
    """Run coroutines concurrently (the client bounds real concurrency); cancel all on failure.

    Re-raises the first exception itself rather than an ``ExceptionGroup``.
    """
    tasks: list[asyncio.Task[T]] = []
    try:
        async with asyncio.TaskGroup() as tg:
            for c in coros:
                tasks.append(tg.create_task(c))  # type: ignore[arg-type]
    except BaseExceptionGroup as group:
        raise group.exceptions[0] from None
    return [t.result() for t in tasks]

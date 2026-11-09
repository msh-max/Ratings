"""Phase 2 - Fetch.

Free mode (default): execute Round T (persisted Text Search jobs), then Round N (Nearby Search in
adaptive batches) and Round D (Place Details), both planned from live DB state. Whatever is still
uncovered afterwards is counted as ``unfetched`` and is never fetched by spending. A call refused
by the free-cap guard (:class:`FreeCapExhausted`) marks its job ``skipped_cap``; the run goes on.

Paid mode (``--allow-paid``): execute the persisted min-cost plan, then sweep any discovered ID
left without data with Place Details.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from .context import RunContext, run_all
from .cost import FreeCapExhausted, Instrument, detect_sku, instrument_by_name, instruments_for
from .db import PLACE_COLUMNS, transaction
from .discover import cell_from_row, search_body
from .fetch_plan import (JOB_COLUMNS, Pool, build_free_plan, details_order, nearby_called,
                         nearby_candidates, remaining_calls, staleness_weights, uncovered_ids)
from .geo import Cell, circumscribed_radius_m
from .parse import place_to_row
from .places_client import PlaceNotFound
from .runs import now_iso

log = logging.getLogger(__name__)

_MANAGED = ("place_id", "first_seen_run_id", "first_seen_at", "last_discovered_run_id",
            "last_discovered_at")
_UPDATABLE = [c for c in PLACE_COLUMNS if c not in _MANAGED]
_UPSERT = (f"INSERT INTO places({', '.join(PLACE_COLUMNS)}) VALUES ({', '.join('?' * len(PLACE_COLUMNS))}) "
           f"ON CONFLICT(place_id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in _UPDATABLE)}")


def store_place(conn: sqlite3.Connection, run_id: str, place: dict[str, Any], fetched_at: str,
                via: str | None = None) -> bool:
    """Upsert a place and add its snapshot for the run; ``via`` is the ``endpoint/sku`` used.

    The first fetch of a place in a run wins: a later one in the same run (e.g. a Nearby Search
    returning an already fetched place) changes nothing, so ``places`` always matches the run's
    snapshot. Returns True when the place got its snapshot now.
    """
    if conn.execute("SELECT 1 FROM place_snapshots WHERE run_id=? AND place_id=?",
                    (run_id, place["id"])).fetchone():
        return False
    row = place_to_row(place)
    discovered = conn.execute("SELECT 1 FROM cell_places WHERE run_id=? AND place_id=? LIMIT 1",
                              (run_id, place["id"])).fetchone() is not None
    row.update(first_seen_run_id=run_id, last_seen_run_id=run_id, first_seen_at=fetched_at,
               last_seen_at=fetched_at, fetched_via=via,
               last_discovered_run_id=run_id if discovered else None,
               last_discovered_at=fetched_at if discovered else None)
    conn.execute(_UPSERT, [row[c] for c in PLACE_COLUMNS])
    conn.execute("INSERT INTO place_snapshots(run_id, place_id, fetched_at, rating, user_rating_count, "
                 "business_status, price_level, sku) VALUES (?,?,?,?,?,?,?,?)",
                 (run_id, row["place_id"], fetched_at, row["rating"], row["user_rating_count"],
                  row["business_status"], row["price_level"], via))
    return True


@dataclass
class FetchStats:
    """Outcome of Phase 2."""

    snapshots: int
    errors: int
    swept: int
    unfetched: int = 0
    skipped_cap: int = 0
    nearby_calls: int = 0


def _has_snapshot(conn: sqlite3.Connection, run_id: str, place_id: str) -> bool:
    return conn.execute("SELECT 1 FROM place_snapshots WHERE run_id=? AND place_id=?",
                        (run_id, place_id)).fetchone() is not None


def _set_job(conn: sqlite3.Connection, run_id: str, job_id: int, status: str) -> None:
    conn.execute("UPDATE fetch_jobs SET status=? WHERE run_id=? AND job_id=?", (status, run_id, job_id))


async def _text_job(ctx: RunContext, job: sqlite3.Row) -> None:
    """One Text Search job (all its pages through one instrument / mask)."""
    cell_row = ctx.conn.execute("SELECT * FROM cells WHERE run_id=? AND cell_id=? AND type=?",
                                (ctx.run_id, job["cell_id"], job["type"])).fetchone()
    body = search_body(ctx.cfg, cell_from_row(cell_row), job["type"])
    inst = instrument_by_name(job["instrument"]) if ctx.cfg.free_only else None
    mask = inst.mask(ctx.cfg) if inst else ctx.cfg.text_mask()
    places: list[dict[str, Any]] = []
    pages, token, status = 0, None, "done"
    via = f"text_search/{detect_sku('text_search', mask)}"
    while pages < 3:  # every follow-up page repeats every original parameter plus pageToken
        req = dict(body) if token is None else {**body, "pageToken": token}
        try:
            resp = await ctx.search_text("fetch", req, mask)
        except FreeCapExhausted:
            status = "skipped_cap"  # keep what earlier pages already returned
            break
        pages += 1
        places.extend(resp.get("places", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    ts = now_iso()
    with transaction(ctx.conn) as conn:
        for p in places:
            store_place(conn, ctx.run_id, p, ts, via)
        _set_job(conn, ctx.run_id, job["job_id"], status)
        conn.execute("UPDATE cells SET fetched=1, fetch_calls=? WHERE run_id=? AND cell_id=? AND type=?",
                     (pages, ctx.run_id, job["cell_id"], job["type"]))
    log.info("fetch text %s %s places=%d %s %s", job["type"], job["cell_id"], len(places), status,
             ctx.progress())


async def _details(ctx: RunContext, place_id: str, phase: str, job_id: int | None,
                   inst: Instrument | None = None) -> None:
    status = "done"
    mask = inst.mask(ctx.cfg) if inst else ctx.cfg.details_mask()
    place = None
    try:
        if not _has_snapshot(ctx.conn, ctx.run_id, place_id):
            place = await ctx.place_details(phase, place_id, mask)
    except PlaceNotFound as exc:
        status = "error"
        ctx.conn.execute("INSERT OR REPLACE INTO fetch_errors VALUES (?,?,?,?)",
                         (ctx.run_id, place_id, exc.code, str(exc)))
    except FreeCapExhausted:
        status = "skipped_cap"
    with transaction(ctx.conn) as conn:
        if place is not None:
            store_place(conn, ctx.run_id, place, now_iso(), f"place_details/{detect_sku('place_details', mask)}")
        if job_id is not None:
            _set_job(conn, ctx.run_id, job_id, status)


def unfetched_ids(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """Phase-1 IDs with neither a snapshot nor a fetch error for the run."""
    return sorted(uncovered_ids(conn, run_id))


async def fetch(ctx: RunContext) -> FetchStats:
    """Run (or resume) Phase 2 in the run's mode."""
    if ctx.cfg.free_only:
        return await fetch_free(ctx)
    return await fetch_paid(ctx)


async def fetch_paid(ctx: RunContext) -> FetchStats:
    """Execute pending text jobs, then Details jobs, then the final Details sweep."""
    conn = ctx.conn
    jobs = conn.execute("SELECT * FROM fetch_jobs WHERE run_id=? AND status='pending' ORDER BY job_id",
                        (ctx.run_id,)).fetchall()
    await run_all(_text_job(ctx, j) for j in jobs if j["kind"] == "text")
    await run_all(_details(ctx, j["place_id"], "fetch", j["job_id"]) for j in jobs
                  if j["kind"] == "details")
    sweep = unfetched_ids(conn, ctx.run_id)
    if sweep:
        log.warning("final sweep: %d discovered IDs without data, fetching with Place Details", len(sweep))
    await run_all(_details(ctx, pid, "sweep", None) for pid in sweep)
    conn.execute("UPDATE runs SET unfetched=? WHERE run_id=?",  # 0 unless something is badly wrong
                 (len(uncovered_ids(conn, ctx.run_id)), ctx.run_id))
    return _stats(conn, ctx.run_id, len(sweep))


def _stats(conn: sqlite3.Connection, run_id: str, swept: int = 0) -> FetchStats:
    one = lambda sql: conn.execute(sql, (run_id,)).fetchone()[0]  # noqa: E731
    return FetchStats(one("SELECT COUNT(*) FROM place_snapshots WHERE run_id=?"),
                      one("SELECT COUNT(*) FROM fetch_errors WHERE run_id=?"), swept,
                      one("SELECT COALESCE(unfetched, 0) FROM runs WHERE run_id=?"),
                      one("SELECT COUNT(*) FROM fetch_jobs WHERE run_id=? AND status='skipped_cap'"),
                      one("SELECT COUNT(*) FROM fetch_jobs WHERE run_id=? AND kind='nearby' AND status='done'"))


def nearby_fetch_body(ctx: RunContext, cell: Cell) -> dict[str, Any]:
    """Round N request: all configured types, 20 nearest, circle around OUR cell rectangle."""
    lat, lng = cell.center
    return {"includedTypes": list(ctx.cfg.types)[:50], "maxResultCount": 20,
            "rankPreference": "DISTANCE", "languageCode": ctx.cfg.language,
            "regionCode": ctx.cfg.region_code,
            "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                               "radius": circumscribed_radius_m(cell)}}}


def _next_job_id(conn: sqlite3.Connection, run_id: str) -> int:
    return conn.execute("SELECT COALESCE(MAX(job_id), 0) + 1 FROM fetch_jobs WHERE run_id=?",
                        (run_id,)).fetchone()[0]


async def _nearby_job(ctx: RunContext, cell_id: str, inst: Instrument) -> None:
    row = ctx.conn.execute("SELECT * FROM cells WHERE run_id=? AND cell_id=? LIMIT 1",
                           (ctx.run_id, cell_id)).fetchone()
    mask = inst.mask(ctx.cfg)
    status, places = "done", []
    try:
        resp = await ctx.search_nearby("fetch", nearby_fetch_body(ctx, cell_from_row(row)), mask)
        places = resp.get("places", [])
    except FreeCapExhausted:
        status = "skipped_cap"
    ts, via = now_iso(), f"nearby_search/{detect_sku('nearby_search', mask)}"
    with transaction(ctx.conn) as conn:
        for p in places:  # every returned place, also ones Phase 1 did not see
            store_place(conn, ctx.run_id, p, ts, via)
        conn.execute(f"INSERT INTO fetch_jobs({JOB_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
                     (ctx.run_id, _next_job_id(conn, ctx.run_id), "nearby", cell_id, None, None, 1,
                      status, inst.name))
    log.info("fetch nearby %s places=%d %s %s", cell_id, len(places), status, ctx.progress())


async def fetch_free(ctx: RunContext) -> FetchStats:
    """Rounds T, N and D within the month's free caps; then record ``unfetched``."""
    conn, cfg, run_id = ctx.conn, ctx.cfg, ctx.run_id
    build_free_plan(conn, run_id, cfg)
    jobs = conn.execute("SELECT * FROM fetch_jobs WHERE run_id=? AND kind='text' AND status='pending' "
                        "ORDER BY job_id", (run_id,)).fetchall()
    await run_all(_text_job(ctx, j) for j in jobs)

    weights = staleness_weights(conn, run_id)
    n_insts = instruments_for(cfg, "nearby_search")
    while True:  # Round N: adaptive batches of `concurrency` calls
        unc = uncovered_ids(conn, run_id)
        pool = Pool(cfg, remaining_calls(conn, run_id, cfg))
        cands = nearby_candidates(conn, run_id, unc, weights, nearby_called(conn, run_id))
        batch: list[tuple[str, Instrument]] = []
        for c in cands[:max(1, cfg.concurrency)]:
            inst = pool.pick(n_insts, 1)
            if inst is None:
                break
            pool.take(inst, 1)
            batch.append((c.cell_id, inst))
        if not batch:
            break
        await run_all(_nearby_job(ctx, cid, inst) for cid, inst in batch)

    # Round D: leftover IDs by weight, I5 then I6, from live state. Pending Details jobs of an
    # interrupted attempt are re-planned rather than reused.
    with transaction(conn):
        conn.execute("DELETE FROM fetch_jobs WHERE run_id=? AND kind='details' AND status='pending'",
                     (run_id,))
    order = details_order(uncovered_ids(conn, run_id), weights)
    pool = Pool(cfg, remaining_calls(conn, run_id, cfg))
    planned: list[tuple[str, Instrument]] = []
    for inst in instruments_for(cfg, "place_details"):
        n = min(len(order) - len(planned), pool.available(inst))
        pool.take(inst, n)
        planned += [(pid, inst) for pid in order[len(planned):len(planned) + n]]
    start = _next_job_id(conn, run_id)
    with transaction(conn):
        conn.executemany(f"INSERT INTO fetch_jobs({JOB_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
                         [(run_id, start + i, "details", None, None, pid, 1, "pending", inst.name)
                          for i, (pid, inst) in enumerate(planned)])
    await run_all(_details(ctx, pid, "fetch", start + i, inst) for i, (pid, inst) in enumerate(planned))

    left = len(uncovered_ids(conn, run_id))  # never fetched by spending: unfetched for this run
    conn.execute("UPDATE runs SET unfetched=? WHERE run_id=?", (left, run_id))
    if left:
        log.warning("free caps reached: %d discovered IDs left unfetched this run (older data, if "
                    "any, is carried forward)", left)
    return _stats(conn, run_id)

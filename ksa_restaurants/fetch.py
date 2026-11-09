"""Phase 2 - Fetch: execute the persisted plan, then sweep any discovered ID left without data."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from .context import RunContext, run_all
from .db import PLACE_COLUMNS, transaction
from .discover import cell_from_row, search_body
from .parse import place_to_row
from .places_client import PlaceNotFound
from .runs import now_iso

log = logging.getLogger(__name__)

_UPDATABLE = [c for c in PLACE_COLUMNS if c not in ("place_id", "first_seen_run_id", "first_seen_at")]
_UPSERT = (f"INSERT INTO places({', '.join(PLACE_COLUMNS)}) VALUES ({', '.join('?' * len(PLACE_COLUMNS))}) "
           f"ON CONFLICT(place_id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in _UPDATABLE)}")


def store_place(conn: sqlite3.Connection, run_id: str, place: dict[str, Any], fetched_at: str) -> None:
    """Upsert a place and add its snapshot for the run (the first snapshot of a run wins)."""
    row = place_to_row(place)
    row.update(first_seen_run_id=run_id, last_seen_run_id=run_id, first_seen_at=fetched_at,
               last_seen_at=fetched_at)
    conn.execute(_UPSERT, [row[c] for c in PLACE_COLUMNS])
    conn.execute("INSERT OR IGNORE INTO place_snapshots VALUES (?,?,?,?,?,?,?)",
                 (run_id, row["place_id"], fetched_at, row["rating"], row["user_rating_count"],
                  row["business_status"], row["price_level"]))


@dataclass
class FetchStats:
    """Outcome of Phase 2."""

    snapshots: int
    errors: int
    swept: int


def _has_snapshot(conn: sqlite3.Connection, run_id: str, place_id: str) -> bool:
    return conn.execute("SELECT 1 FROM place_snapshots WHERE run_id=? AND place_id=?",
                        (run_id, place_id)).fetchone() is not None


async def _text_job(ctx: RunContext, job: sqlite3.Row) -> None:
    cell_row = ctx.conn.execute("SELECT * FROM cells WHERE run_id=? AND cell_id=? AND type=?",
                                (ctx.run_id, job["cell_id"], job["type"])).fetchone()
    body = search_body(ctx.cfg, cell_from_row(cell_row), job["type"])
    places, pages, _ = await ctx.search_text_all("fetch", body, ctx.cfg.text_mask())
    ts = now_iso()
    with transaction(ctx.conn) as conn:
        for p in places:
            store_place(conn, ctx.run_id, p, ts)
        conn.execute("UPDATE fetch_jobs SET status='done' WHERE run_id=? AND job_id=?",
                     (ctx.run_id, job["job_id"]))
        conn.execute("UPDATE cells SET fetched=1, fetch_calls=? WHERE run_id=? AND cell_id=? AND type=?",
                     (pages, ctx.run_id, job["cell_id"], job["type"]))
    log.info("fetch text %s %s places=%d %s", job["type"], job["cell_id"], len(places), ctx.progress())


async def _details(ctx: RunContext, place_id: str, phase: str, job_id: int | None) -> None:
    status = "done"
    try:
        place = None
        if not _has_snapshot(ctx.conn, ctx.run_id, place_id):
            place = await ctx.place_details(phase, place_id, ctx.cfg.details_mask())
    except PlaceNotFound as exc:
        status, place = "error", None
        ctx.conn.execute("INSERT OR REPLACE INTO fetch_errors VALUES (?,?,?,?)",
                         (ctx.run_id, place_id, exc.code, str(exc)))
    with transaction(ctx.conn) as conn:
        if place is not None:
            store_place(conn, ctx.run_id, place, now_iso())
        if job_id is not None:
            conn.execute("UPDATE fetch_jobs SET status=? WHERE run_id=? AND job_id=?",
                         (status, ctx.run_id, job_id))


def unfetched_ids(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """Phase-1 IDs with neither a snapshot nor a fetch error for the run."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT place_id FROM cell_places WHERE run_id=?1 AND place_id NOT IN "
        "(SELECT place_id FROM place_snapshots WHERE run_id=?1) AND place_id NOT IN "
        "(SELECT place_id FROM fetch_errors WHERE run_id=?1) ORDER BY place_id", (run_id,))]


async def fetch(ctx: RunContext) -> FetchStats:
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
    snaps = conn.execute("SELECT COUNT(*) FROM place_snapshots WHERE run_id=?", (ctx.run_id,)).fetchone()[0]
    errors = conn.execute("SELECT COUNT(*) FROM fetch_errors WHERE run_id=?", (ctx.run_id,)).fetchone()[0]
    return FetchStats(snaps, errors, len(sweep))

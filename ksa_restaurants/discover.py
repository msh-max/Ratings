"""Phase 1 - Discover: adaptive quadtree of free IDs-only Text Searches, one tree per type."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from .config import DISCOVER_MASK, Config
from .context import RunContext, run_all
from .db import transaction
from .geo import Cell, can_split, children, seed_cells
from .runs import previous_run_id, run_bbox

log = logging.getLogger(__name__)
CAP = 60


def search_body(cfg: Config, cell: Cell, place_type: str) -> dict[str, Any]:
    """Text Search request body for one (cell, type); identical in Phase 1 and Phase 2."""
    return {"textQuery": cfg.text_query(place_type), "includedType": place_type,
            "strictTypeFiltering": True, "regionCode": cfg.region_code,
            "languageCode": cfg.language, "pageSize": 20,
            "locationRestriction": {"rectangle": cell.rectangle()}}


def cell_from_row(row: sqlite3.Row) -> Cell:
    """Build a :class:`Cell` from a ``cells`` row."""
    return Cell(row["cell_id"], row["lat_lo"], row["lng_lo"], row["lat_hi"], row["lng_hi"],
                row["depth"], row["parent_id"])


def _insert_pending(conn: sqlite3.Connection, run_id: str, place_type: str, cells: list[Cell]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO cells(run_id, cell_id, type, lat_lo, lng_lo, lat_hi, lng_hi, depth, "
        "parent_id, status) VALUES (?,?,?,?,?,?,?,?,?,'pending')",
        [(run_id, c.cell_id, place_type, c.lat_lo, c.lng_lo, c.lat_hi, c.lng_hi, c.depth, c.parent_id)
         for c in cells])


def initial_cells(ctx: RunContext, place_type: str, cold_start: bool) -> tuple[list[Cell], str]:
    """Warm-start leaves of the previous run for this type, or the pruned seed grid."""
    prev = None if cold_start else previous_run_id(ctx.conn, ctx.run_id)
    if prev:
        rows = ctx.conn.execute(
            "SELECT * FROM cells WHERE run_id=? AND type=? AND status IN ('leaf','saturated_leaf') "
            "ORDER BY cell_id", (prev, place_type)).fetchall()
        if rows:
            return [cell_from_row(r) for r in rows], f"warm:{prev}"
    cfg = ctx.cfg
    return seed_cells(run_bbox(cfg), cfg.seed_cell_deg, True, cfg.boundary_buffer_deg), "seed"


@dataclass
class DiscoverStats:
    """Outcome of Phase 1."""

    cells: int
    leaves: int
    saturated_at_min_size: int
    place_ids: int
    calls: int


async def _process(ctx: RunContext, cell: Cell, place_type: str) -> None:
    body = search_body(ctx.cfg, cell, place_type)
    places, pages, token_left = await ctx.search_text_all("discover", body, DISCOVER_MASK)
    ids = sorted({p["id"] for p in places})
    saturated = len(places) >= CAP or token_left
    split = saturated and can_split(cell, ctx.cfg.min_cell_deg)
    status = "split" if split else ("saturated_leaf" if saturated else "leaf")
    with transaction(ctx.conn) as conn:
        conn.execute("UPDATE cells SET status=?, result_count=?, discover_calls=? "
                     "WHERE run_id=? AND cell_id=? AND type=?",
                     (status, len(ids), pages, ctx.run_id, cell.cell_id, place_type))
        if split:
            _insert_pending(conn, ctx.run_id, place_type, children(cell))
        else:
            conn.executemany("INSERT OR IGNORE INTO cell_places VALUES (?,?,?,?)",
                             [(ctx.run_id, cell.cell_id, place_type, i) for i in ids])
    log.info("discover %s %s depth=%d n=%d %s %s", place_type, cell.cell_id, cell.depth, len(ids),
             status, ctx.progress())


async def discover(ctx: RunContext, cold_start: bool = False) -> DiscoverStats:
    """Run (or resume) Phase 1 until no cell is pending. Idempotent per (run, cell, type)."""
    conn = ctx.conn
    for place_type in ctx.cfg.types:
        if conn.execute("SELECT 1 FROM cells WHERE run_id=? AND type=? LIMIT 1",
                        (ctx.run_id, place_type)).fetchone():
            continue
        cells, source = initial_cells(ctx, place_type, cold_start)
        with transaction(conn):
            _insert_pending(conn, ctx.run_id, place_type, cells)
        log.info("discover %s: %d start cells (%s)", place_type, len(cells), source)
    while True:
        rows = conn.execute("SELECT * FROM cells WHERE run_id=? AND status='pending' "
                            "ORDER BY type, cell_id", (ctx.run_id,)).fetchall()
        if not rows:
            break
        await run_all(_process(ctx, cell_from_row(r), r["type"]) for r in rows)
    return discover_stats(conn, ctx.run_id)


def discover_stats(conn: sqlite3.Connection, run_id: str) -> DiscoverStats:
    """Phase 1 figures from the DB."""
    row = conn.execute(
        "SELECT COUNT(*), SUM(status IN ('leaf','saturated_leaf')), SUM(status='saturated_leaf'), "
        "COALESCE(SUM(discover_calls),0) FROM cells WHERE run_id=?", (run_id,)).fetchone()
    ids = conn.execute("SELECT COUNT(DISTINCT place_id) FROM cell_places WHERE run_id=?",
                       (run_id,)).fetchone()[0]
    return DiscoverStats(row[0], row[1] or 0, row[2] or 0, ids, row[3])

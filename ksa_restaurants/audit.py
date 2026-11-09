"""Phase 3 - Audit: independent Nearby Search recall check against the WHOLE run.

No geometric filter is applied to results (Google's terms forbid point-in-polygon analysis on
Places coordinates): a found place is IN SCOPE when its addressComponents country is SA and its
types intersect the configured types; it is MATCHED when it has a snapshot in this run.
Only leaves whose circumscribed circle lies inside the run bbox are sampled (a cell-geometry test),
so that a sub-country ``--bbox`` run is not penalised for places outside its own area.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from dataclasses import dataclass

from .config import AUDIT_MASK, Config
from .context import RunContext, run_all
from .db import transaction
from .discover import cell_from_row
from .geo import Cell, circle_inside_bbox, circumscribed_radius_m
from .parse import country_code
from .runs import run_bbox

log = logging.getLogger(__name__)


def sample_cells(conn: sqlite3.Connection, run_id: str, cfg: Config, n: int) -> list[tuple[Cell, str]]:
    """Deterministic weighted (by result_count) sample without replacement of eligible leaves."""
    if n <= 0:
        return []
    bbox = run_bbox(cfg)
    rows = conn.execute("SELECT * FROM cells WHERE run_id=? AND status IN ('leaf','saturated_leaf') "
                        "AND result_count>0 ORDER BY type, cell_id", (run_id,)).fetchall()
    eligible = [(cell_from_row(r), r["type"], r["result_count"]) for r in rows]
    eligible = [e for e in eligible if circle_inside_bbox(e[0], circumscribed_radius_m(e[0]), bbox)]
    rng = random.Random(f"audit:{run_id}")
    keyed = sorted(eligible, key=lambda e: rng.random() ** (1.0 / e[2]), reverse=True)
    picked: dict[str, tuple[Cell, str]] = {}  # one query per rectangle, even if several trees have it
    for c, t, _ in keyed:
        picked.setdefault(c.cell_id, (c, t))
        if len(picked) == n:
            break
    return list(picked.values())


def nearby_body(cfg: Config, cell: Cell) -> dict:
    """Nearby Search body: broad food types, DISTANCE ranking, circumscribed circle."""
    lat, lng = cell.center
    return {"includedTypes": cfg.audit_types[:50], "maxResultCount": 20, "rankPreference": "DISTANCE",
            "languageCode": cfg.language, "regionCode": cfg.region_code,
            "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                               "radius": circumscribed_radius_m(cell)}}}


@dataclass
class AuditResult:
    """Overall audit figures (deduplicated over all audited cells)."""

    cells: int
    found: int
    in_scope: int
    matched: int
    recall: float | None
    out_of_scope_share: float | None


async def _audit_cell(ctx: RunContext, cell: Cell, place_type: str) -> None:
    resp = await ctx.search_nearby("audit", nearby_body(ctx.cfg, cell), AUDIT_MASK)
    with transaction(ctx.conn) as conn:
        conn.executemany("INSERT OR IGNORE INTO audit_found VALUES (?,?,?,?,?,?)",
                         [(ctx.run_id, cell.cell_id, place_type, p["id"], country_code(p),
                           json.dumps(p.get("types", []))) for p in resp.get("places", [])])
        conn.execute("INSERT OR IGNORE INTO audit_cells(run_id, cell_id, type) VALUES (?,?,?)",
                     (ctx.run_id, cell.cell_id, place_type))


def _classify(conn: sqlite3.Connection, run_id: str, cfg: Config,
              rows: list[sqlite3.Row]) -> tuple[int, int, int, int, int]:
    """(found, saudi_found, in_scope, matched, out_of_scope) for a list of audit_found rows."""
    wanted = set(cfg.types)
    found = saudi = in_scope = matched = out = 0
    for r in rows:
        found += 1
        if r["country_code"] != "SA":
            continue
        saudi += 1
        if wanted & set(json.loads(r["types_json"])):
            in_scope += 1
            matched += conn.execute("SELECT 1 FROM place_snapshots WHERE run_id=? AND place_id=?",
                                    (run_id, r["place_id"])).fetchone() is not None
        else:
            out += 1
    return found, saudi, in_scope, matched, out


def audit_stats(conn: sqlite3.Connection, run_id: str, cfg: Config) -> AuditResult:
    """Recompute per-cell and overall audit figures from the DB (also after DB edits)."""
    cells = conn.execute("SELECT cell_id, type FROM audit_cells WHERE run_id=?", (run_id,)).fetchall()
    with transaction(conn):
        for c in cells:
            rows = conn.execute("SELECT * FROM audit_found WHERE run_id=? AND cell_id=? AND type=?",
                                (run_id, c[0], c[1])).fetchall()
            found, _, in_scope, matched, out = _classify(conn, run_id, cfg, rows)
            conn.execute("UPDATE audit_cells SET found=?, in_scope=?, matched=?, out_of_scope=?, "
                         "recall=? WHERE run_id=? AND cell_id=? AND type=?",
                         (found, in_scope, matched, out, matched / in_scope if in_scope else None,
                          run_id, c[0], c[1]))
    distinct = conn.execute("SELECT place_id, MIN(country_code) AS country_code, MIN(types_json) "
                            "AS types_json FROM audit_found WHERE run_id=? GROUP BY place_id",
                            (run_id,)).fetchall()
    found, saudi, in_scope, matched, out = _classify(conn, run_id, cfg, distinct)
    recall = matched / in_scope if in_scope else None
    conn.execute("UPDATE runs SET audit_recall=? WHERE run_id=?", (recall, run_id))
    return AuditResult(len(cells), found, in_scope, matched, recall, out / saudi if saudi else None)


async def audit(ctx: RunContext, n_cells: int) -> AuditResult:
    """Audit ``n_cells`` sampled leaves (cells already audited in this run are not re-queried)."""
    done = {(r[0], r[1]) for r in ctx.conn.execute(
        "SELECT cell_id, type FROM audit_cells WHERE run_id=?", (ctx.run_id,))}
    todo = [(c, t) for c, t in sample_cells(ctx.conn, ctx.run_id, ctx.cfg, n_cells)
            if (c.cell_id, t) not in done]
    await run_all(_audit_cell(ctx, c, t) for c, t in todo)
    result = audit_stats(ctx.conn, ctx.run_id, ctx.cfg)
    log.info("audit: %s %s", result, ctx.progress())
    return result

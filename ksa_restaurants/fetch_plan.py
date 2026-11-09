"""Phase 2 plan: exact minimum-cost choice between paid Text Search pages and Place Details.

Greedy weighted set cover with a lazy max-heap over populated leaf (cell, type) pairs: pick the
pair with the highest ``gain / pages`` (gain = |S ∩ uncovered|); take it as a Text Search job while
``pages * P_text <= gain * P_details``, otherwise stop (every remaining pair is worse). All IDs still
uncovered become Place Details jobs. The plan is persisted in ``fetch_jobs`` and reused on resume.
"""

from __future__ import annotations

import heapq
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from .config import Config
from .cost import CostModel, detect_sku
from .db import transaction


@dataclass
class Plan:
    """Counts of a fetch plan (all jobs, or only pending ones)."""

    text_jobs: int
    text_pages: int
    details_jobs: int
    text_sku: str
    details_sku: str

    def counts(self) -> dict[tuple[str, str], int]:
        """Paid calls per (endpoint, sku)."""
        return {("text_search", self.text_sku): self.text_pages,
                ("place_details", self.details_sku): self.details_jobs}


def pages_for(n: int) -> int:
    """Text Search pages needed for ``n`` results (20 per page, max 3)."""
    return min(3, math.ceil(n / 20))


def greedy_cover(pairs: dict[tuple[str, str], set[str]], p_text: float, p_details: float,
                 pages: dict[tuple[str, str], int] | None = None,
                 ) -> tuple[list[tuple[str, str, int]], set[str]]:
    """Return (chosen text jobs ``(cell_id, type, pages)`` in pick order, IDs left for Details).

    ``pages`` gives the page count of each pair when known (Phase 1's actual page count, which
    Phase 2 repeats exactly because it sends the identical request); default ``ceil(n/20)``.
    """
    pages = pages or {}

    def npages(key: tuple[str, str]) -> int:
        return max(pages_for(len(pairs[key])), min(3, pages.get(key, 0)))

    uncovered: set[str] = set().union(*pairs.values()) if pairs else set()
    heap = [(-len(s) / npages((cid, t)), cid, t) for (cid, t), s in pairs.items() if s]
    heapq.heapify(heap)
    chosen: list[tuple[str, str, int]] = []
    while heap:
        neg_ratio, cid, t = heapq.heappop(heap)
        s = pairs[(cid, t)]
        n_pages = npages((cid, t))
        gain = len(s & uncovered)
        if gain == 0:
            continue
        ratio = gain / n_pages
        if ratio < -neg_ratio - 1e-12:  # stale key: re-insert with the true ratio
            heapq.heappush(heap, (-ratio, cid, t))
            continue
        if n_pages * p_text > gain * p_details:
            break
        chosen.append((cid, t, n_pages))
        uncovered -= s
    return chosen, uncovered


def _skus(cfg: Config) -> tuple[str, str]:
    return detect_sku("text_search", cfg.text_mask()), detect_sku("place_details", cfg.details_mask())


def build_plan(conn: sqlite3.Connection, run_id: str, cfg: Config) -> Plan:
    """Build and persist the plan unless one already exists for the run; return its totals."""
    if not conn.execute("SELECT 1 FROM fetch_jobs WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
        pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
        # Google can return a short page that still carries a nextPageToken, so the page count of
        # a pair is what Phase 1 actually needed, not just ceil(n/20).
        pages = {(r[0], r[1]): r[2] or 0 for r in conn.execute(
            "SELECT cell_id, type, discover_calls FROM cells WHERE run_id=? "
            "AND status IN ('leaf','saturated_leaf')", (run_id,))}
        for cid, t, pid in conn.execute(
                "SELECT cp.cell_id, cp.type, cp.place_id FROM cell_places cp JOIN cells c ON "
                "c.run_id=cp.run_id AND c.cell_id=cp.cell_id AND c.type=cp.type WHERE cp.run_id=? "
                "AND c.status IN ('leaf','saturated_leaf')", (run_id,)):
            pairs[(cid, t)].add(pid)
        cost = CostModel(cfg.pricing)
        text_sku, details_sku = _skus(cfg)
        chosen, rest = greedy_cover(dict(pairs), cost.unit_price("text_search", text_sku),
                                    cost.unit_price("place_details", details_sku), pages)
        rows: list[tuple[object, ...]] = [(run_id, i + 1, "text", cid, t, None, n, "pending")
                for i, (cid, t, n) in enumerate(chosen)]
        rows += [(run_id, len(chosen) + i + 1, "details", None, None, pid, 1, "pending")
                 for i, pid in enumerate(sorted(rest))]
        with transaction(conn):
            conn.executemany("INSERT INTO fetch_jobs VALUES (?,?,?,?,?,?,?,?)", rows)
    return plan_totals(conn, run_id, cfg)


def plan_totals(conn: sqlite3.Connection, run_id: str, cfg: Config, pending_only: bool = False) -> Plan:
    """Totals of the persisted plan (optionally only jobs still pending)."""
    where = " AND status='pending'" if pending_only else ""
    row = conn.execute(
        "SELECT COALESCE(SUM(kind='text'),0), COALESCE(SUM(CASE WHEN kind='text' THEN pages END),0), "
        f"COALESCE(SUM(kind='details'),0) FROM fetch_jobs WHERE run_id=?{where}", (run_id,)).fetchone()
    text_sku, details_sku = _skus(cfg)
    return Plan(row[0], row[1], row[2], text_sku, details_sku)

"""Phase 2 plans.

Free mode (default): Rounds T (Text Search, exact, persisted up front), N (Nearby Search,
adaptive, planned from live DB state in batches) and D (Place Details, exact, from live state),
each bounded by the month-to-date free caps of the instruments' SKUs; IDs still uncovered after D
are *unfetched* for the run and never fetched by spending. Weights favour the stalest data.

Paid mode (``--allow-paid``): exact minimum-cost choice between Text Search pages and Place Details.
Greedy weighted set cover with a lazy max-heap over populated leaf (cell, type) pairs: pick the
pair with the highest ``gain / pages`` (gain = |S ∩ uncovered|); take it as a Text Search job while
``pages * P_text <= gain * P_details``, otherwise stop (every remaining pair is worse). All IDs still
uncovered become Place Details jobs. The plan is persisted in ``fetch_jobs`` and reused on resume.
"""

from __future__ import annotations

import bisect
import heapq
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .config import Config
from .cost import CostModel, Instrument, detect_sku, free_remaining, instruments_for, usable_cap
from .db import transaction
from .runs import FINISHED, now_iso, usage_for_guard


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


JOB_COLUMNS = "run_id, job_id, kind, cell_id, type, place_id, pages, status, instrument"


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


def leaf_pairs(conn: sqlite3.Connection, run_id: str
               ) -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str], int]]:
    """Populated leaf (cell, type) pairs with their ID sets, and Phase 1's page count per pair.

    Google can return a short page that still carries a nextPageToken, so the page count of a
    pair is what Phase 1 actually needed, not just ceil(n/20).
    """
    pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
    pages = {(r[0], r[1]): r[2] or 0 for r in conn.execute(
        "SELECT cell_id, type, discover_calls FROM cells WHERE run_id=? "
        "AND status IN ('leaf','saturated_leaf')", (run_id,))}
    for cid, t, pid in conn.execute(
            "SELECT cp.cell_id, cp.type, cp.place_id FROM cell_places cp JOIN cells c ON "
            "c.run_id=cp.run_id AND c.cell_id=cp.cell_id AND c.type=cp.type WHERE cp.run_id=? "
            "AND c.status IN ('leaf','saturated_leaf')", (run_id,)):
        pairs[(cid, t)].add(pid)
    return dict(pairs), pages


def build_plan(conn: sqlite3.Connection, run_id: str, cfg: Config) -> Plan:
    """Paid mode: build and persist the plan unless one already exists; return its totals."""
    if not conn.execute("SELECT 1 FROM fetch_jobs WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
        pairs, pages = leaf_pairs(conn, run_id)
        cost = CostModel(cfg.pricing)
        text_sku, details_sku = _skus(cfg)
        chosen, rest = greedy_cover(pairs, cost.unit_price("text_search", text_sku),
                                    cost.unit_price("place_details", details_sku), pages)
        t_via, d_via = f"text_search/{text_sku}", f"place_details/{details_sku}"
        rows: list[tuple[object, ...]] = [(run_id, i + 1, "text", cid, t, None, n, "pending", t_via)
                                          for i, (cid, t, n) in enumerate(chosen)]
        rows += [(run_id, len(chosen) + i + 1, "details", None, None, pid, 1, "pending", d_via)
                 for i, pid in enumerate(sorted(rest))]
        with transaction(conn):
            conn.executemany(f"INSERT INTO fetch_jobs({JOB_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)", rows)
            conn.execute("UPDATE runs SET plan_built_at=? WHERE run_id=?", (now_iso(), run_id))
    return plan_totals(conn, run_id, cfg)


def plan_totals(conn: sqlite3.Connection, run_id: str, cfg: Config, pending_only: bool = False) -> Plan:
    """Totals of the persisted plan (optionally only jobs still pending)."""
    where = " AND status='pending'" if pending_only else ""
    row = conn.execute(
        "SELECT COALESCE(SUM(kind='text'),0), COALESCE(SUM(CASE WHEN kind='text' THEN pages END),0), "
        f"COALESCE(SUM(kind='details'),0) FROM fetch_jobs WHERE run_id=?{where}", (run_id,)).fetchone()
    text_sku, details_sku = _skus(cfg)
    return Plan(row[0], row[1], row[2], text_sku, details_sku)



# ----- free mode -----------------------------------------------------------------------------

NEARBY_HIT_RATE = 0.7  # expected share of min(20, |U_c|) a Nearby call covers (planning only)
MIN_RATIO = 1.0        # minimum weighted IDs per call for Rounds T and N


def discovered_ids(conn: sqlite3.Connection, run_id: str) -> set[str]:
    """Distinct place IDs found in Phase 1 of the run."""
    return {r[0] for r in conn.execute("SELECT DISTINCT place_id FROM cell_places WHERE run_id=?",
                                       (run_id,))}


def uncovered_ids(conn: sqlite3.Connection, run_id: str) -> set[str]:
    """Discovered IDs with neither a snapshot nor a fetch error in this run."""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT place_id FROM cell_places WHERE run_id=?1 AND place_id NOT IN "
        "(SELECT place_id FROM place_snapshots WHERE run_id=?1) AND place_id NOT IN "
        "(SELECT place_id FROM fetch_errors WHERE run_id=?1)", (run_id,))}


def staleness_weights(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    """Weight per ID discovered in the run: 3 = never had a snapshot; otherwise by age, the number
    of runs with the same bbox since its last snapshot (finished runs, plus this one):
    2 if age >= 2, 1 if age == 1. Age 0 (a snapshot in this run, on resume) is 1 and such an ID
    is already covered anyway."""
    run = conn.execute("SELECT started_at, bbox_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    finished = sorted(r[0] for r in conn.execute(
        f"SELECT started_at FROM runs WHERE run_id<>? AND bbox_json=? AND status IN {FINISHED}",
        (run_id, run[1])))
    last = {r[0]: r[1] for r in conn.execute(
        "SELECT s.place_id, MAX(r.started_at) FROM place_snapshots s JOIN runs r ON r.run_id=s.run_id "
        "WHERE s.run_id<>?1 AND s.place_id IN (SELECT place_id FROM cell_places WHERE run_id=?1) "
        "GROUP BY s.place_id", (run_id,))}
    in_run = {r[0] for r in conn.execute("SELECT place_id FROM place_snapshots WHERE run_id=?", (run_id,))}
    weights: dict[str, int] = {}
    for pid in discovered_ids(conn, run_id):
        if pid in in_run:
            weights[pid] = 1
        elif pid not in last:
            weights[pid] = 3
        else:
            age = 1 + len(finished) - bisect.bisect_right(finished, last[pid])
            weights[pid] = 2 if age >= 2 else 1
    return weights


def remaining_calls(conn: sqlite3.Connection, run_id: str, cfg: Config) -> dict[str, int | None]:
    """Usable free calls left this month per ``endpoint/sku`` (month-to-date, all runs)."""
    return free_remaining(cfg, usage_for_guard(conn, run_id))


class Pool:
    """Remaining free calls per SKU key, shared by instruments that bill the same SKU."""

    def __init__(self, cfg: Config, remaining: dict[str, int | None]) -> None:
        self.cfg, self.left = cfg, dict(remaining)

    def available(self, inst: Instrument) -> int:
        """Calls the instrument may still make (unlimited SKUs never occur for paid tiers)."""
        left = self.left.get(inst.key(self.cfg), 0)
        return 10**12 if left is None else left

    def take(self, inst: Instrument, n: int) -> None:
        """Consume ``n`` calls of the instrument's SKU."""
        key = inst.key(self.cfg)
        if self.left.get(key, 0) is not None:
            self.left[key] = self.left.get(key, 0) - n  # type: ignore[operator]

    def pick(self, insts: list[Instrument], n: int) -> Instrument | None:
        """First instrument (fill order) with at least ``n`` calls left."""
        return next((i for i in insts if self.available(i) >= n), None)


def plan_text_round(pairs: dict[tuple[str, str], set[str]], pages: dict[tuple[str, str], int],
                    uncovered: set[str], weights: dict[str, int], insts: list[Instrument],
                    pool: Pool) -> list[tuple[str, str, int, Instrument]]:
    """Round T: lazy max-heap on weighted gain / pages. A job is taken while its weighted gain per
    page is >= 1 and one instrument (fill order) still has all its pages; ``pool`` is consumed."""
    def npages(key: tuple[str, str]) -> int:
        return max(pages_for(len(pairs[key])), min(3, pages.get(key, 0)))

    unc = set(uncovered)

    def gain(key: tuple[str, str]) -> int:
        return sum(weights.get(p, 3) for p in pairs[key] & unc)

    heap = [(-gain(k) / npages(k), k[0], k[1]) for k in pairs if pairs[k] & unc]
    heapq.heapify(heap)
    chosen: list[tuple[str, str, int, Instrument]] = []
    while heap:
        neg, cid, t = heapq.heappop(heap)
        key = (cid, t)
        g, n = gain(key), npages(key)
        if g == 0:
            continue
        ratio = g / n
        if ratio < -neg - 1e-12:
            heapq.heappush(heap, (-ratio, cid, t))
            continue
        if ratio < MIN_RATIO:
            break
        inst = pool.pick(insts, n)
        if inst is None:
            if all(pool.available(i) == 0 for i in insts):
                break
            continue  # does not fit; a job with fewer pages may
        pool.take(inst, n)
        chosen.append((cid, t, n, inst))
        unc -= pairs[key]
    return chosen


def build_free_plan(conn: sqlite3.Connection, run_id: str, cfg: Config) -> None:
    """Round T, persisted once per run (a resumed run executes the remainder of the same jobs)."""
    if conn.execute("SELECT plan_built_at FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]:
        return
    pairs, pages = leaf_pairs(conn, run_id)
    pool = Pool(cfg, remaining_calls(conn, run_id, cfg))
    chosen = plan_text_round(pairs, pages, uncovered_ids(conn, run_id), staleness_weights(conn, run_id),
                             instruments_for(cfg, "text_search"), pool)
    start = conn.execute("SELECT COALESCE(MAX(job_id), 0) FROM fetch_jobs WHERE run_id=?", (run_id,)).fetchone()[0]
    rows = [(run_id, start + i + 1, "text", cid, t, None, n, "pending", inst.name)
            for i, (cid, t, n, inst) in enumerate(chosen)]
    with transaction(conn):
        conn.executemany(f"INSERT INTO fetch_jobs({JOB_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("UPDATE runs SET plan_built_at=? WHERE run_id=?", (now_iso(), run_id))


@dataclass
class NearbyCandidate:
    """A leaf rectangle with uncovered IDs recorded for it in Phase 1."""

    cell_id: str
    uncovered: set[str]
    score: float      # 0.7 x min(20, |U_c|), the threshold quantity
    order: float      # score x mean weight of U_c (weighted order)


def nearby_candidates(conn: sqlite3.Connection, run_id: str, uncovered: set[str],
                      weights: dict[str, int], exclude: set[str]) -> list[NearbyCandidate]:
    """Round N candidates with score >= 1, best first (cells in ``exclude`` were already called)."""
    by_cell: dict[str, set[str]] = defaultdict(set)
    for cid, pid in conn.execute("SELECT DISTINCT cell_id, place_id FROM cell_places WHERE run_id=?",
                                 (run_id,)):
        if pid in uncovered and cid not in exclude:
            by_cell[cid].add(pid)
    out = []
    for cid, ids in by_cell.items():
        score = NEARBY_HIT_RATE * min(20, len(ids))
        if score >= MIN_RATIO:
            mean_w = sum(weights.get(p, 3) for p in ids) / len(ids)
            out.append(NearbyCandidate(cid, ids, score, score * mean_w))
    out.sort(key=lambda c: (-c.order, c.cell_id))
    return out


def nearby_called(conn: sqlite3.Connection, run_id: str) -> set[str]:
    """Cells already queried with Nearby Search in this run (never queried twice)."""
    return {r[0] for r in conn.execute("SELECT cell_id FROM fetch_jobs WHERE run_id=? AND kind='nearby'",
                                       (run_id,))}


def details_order(uncovered: set[str], weights: dict[str, int]) -> list[str]:
    """Round D order: weight descending, then place_id."""
    return sorted(uncovered, key=lambda p: (-weights.get(p, 3), p))


@dataclass
class InstrumentForecast:
    """One line of the free-mode estimate."""

    instrument: Instrument
    key: str
    planned: int
    remaining: int | None
    usable: int | None
    cap: int | None
    covers: int
    exact: bool


@dataclass
class FreeForecast:
    """What the free-mode fetch is expected to do from the current DB state."""

    discovered: int
    already_covered: int
    lines: list[InstrumentForecast] = field(default_factory=list)
    covered_text: int = 0
    covered_nearby_est: int = 0
    covered_details: int = 0
    unfetched_est: int = 0
    nearby_candidates: int = 0


def free_forecast(conn: sqlite3.Connection, run_id: str, cfg: Config) -> FreeForecast:
    """Forecast Rounds T (exact, from the persisted jobs), N (estimated) and D (exact)."""
    remaining = remaining_calls(conn, run_id, cfg)
    pool = Pool(cfg, remaining)
    weights = staleness_weights(conn, run_id)
    disc = discovered_ids(conn, run_id)
    unc = uncovered_ids(conn, run_id)
    fc = FreeForecast(len(disc), len(disc) - len(unc))
    pairs, _ = leaf_pairs(conn, run_id)
    planned: dict[str, int] = defaultdict(int)
    covers: dict[str, int] = defaultdict(int)
    for cid, t, n, name in conn.execute("SELECT cell_id, type, pages, instrument FROM fetch_jobs "
                                        "WHERE run_id=? AND kind='text' AND status='pending' "
                                        "ORDER BY job_id", (run_id,)):
        got = pairs.get((cid, t), set()) & unc
        planned[name] += n
        covers[name] += len(got)
        unc -= got
    fc.covered_text = sum(covers.values())
    for inst in instruments_for(cfg, "text_search"):
        pool.take(inst, planned[inst.name])
    # Round N (estimate): best candidates while calls remain.
    cands = nearby_candidates(conn, run_id, unc, weights, nearby_called(conn, run_id))
    fc.nearby_candidates = len(cands)
    est = 0.0
    for c in cands:
        inst = pool.pick(instruments_for(cfg, "nearby_search"), 1)
        if inst is None:
            break
        pool.take(inst, 1)
        planned[inst.name] += 1
        est += c.score
    fc.covered_nearby_est = min(len(unc), int(round(est)))
    n_calls = instruments_for(cfg, "nearby_search")
    total_n = sum(planned[i.name] for i in n_calls) or 1
    for i in n_calls:
        covers[i.name] = round(fc.covered_nearby_est * planned[i.name] / total_n)
    left = len(unc) - fc.covered_nearby_est
    # Round D (exact given N): leftover IDs in fill order.
    for inst in instruments_for(cfg, "place_details"):
        n = min(left, pool.available(inst))
        pool.take(inst, n)
        planned[inst.name] += n
        covers[inst.name] = n
        fc.covered_details += n
        left -= n
    fc.unfetched_est = left
    for ep in ("text_search", "nearby_search", "place_details"):
        for inst in instruments_for(cfg, ep):
            key = inst.key(cfg)
            fc.lines.append(InstrumentForecast(inst, key, planned[inst.name], remaining.get(key, 0),
                                               usable_cap(cfg, key), cfg.free_caps.get(key),
                                               covers[inst.name], ep != "nearby_search"))
    return fc

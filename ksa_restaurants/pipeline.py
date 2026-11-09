"""Orchestration of the four phases, the cost estimate and the budget guard."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import AuditResult, audit, sample_cells
from .config import AUDIT_MASK, Config
from .context import RunContext
from .cost import CostLine, CostModel, detect_sku, usable_cap
from .discover import DiscoverStats, discover
from .export import export
from .fetch import FetchStats, fetch
from .fetch_plan import (FreeForecast, Plan, build_free_plan, build_plan, free_forecast, plan_totals,
                         remaining_calls)
from .geo import seed_cells
from .runs import current_month, run_bbox, set_status
from .validate import validate

EXIT_OK, EXIT_ERROR, EXIT_BUDGET, EXIT_QUALITY = 0, 1, 2, 3


class BudgetExceeded(RuntimeError):
    """The paid-call estimate exceeds ``--max-budget-usd``; nothing paid was called."""


class DiscoveryIncomplete(RuntimeError):
    """A paid phase was requested before Phase 1 finished for the run."""


@dataclass
class Estimate:
    """Paid calls still to be made for a run, and their cost."""

    plan: Plan
    pending: Plan
    audit_calls: int
    audit_sku: str
    lines: list[CostLine]
    gross_usd: float
    net_usd: float

    def counts(self) -> dict[tuple[str, str], int]:
        """Pending paid calls per (endpoint, sku)."""
        c = {k: v for k, v in self.pending.counts().items() if v}
        if self.audit_calls:
            c[("nearby_search", self.audit_sku)] = self.audit_calls
        return c

    def render(self) -> str:
        """Human-readable estimate."""
        p, q = self.plan, self.pending
        out = [f"Fetch plan   : {p.text_jobs:,} Text Search jobs / {p.text_pages:,} pages ({p.text_sku}), "
               f"{p.details_jobs:,} Place Details jobs ({p.details_sku})",
               f"Still to run : {q.text_pages:,} Text Search pages, {q.details_jobs:,} Place Details calls, "
               f"{self.audit_calls:,} audit Nearby Search calls ({self.audit_sku})"]
        out += [f"  {c.endpoint}/{c.sku}: {c.calls:,} calls  gross ${c.gross_usd:,.2f}  net ${c.net_usd:,.2f}"
                for c in self.lines]
        out.append(f"Estimated cost: ${self.gross_usd:,.2f} gross / ${self.net_usd:,.2f} net of the monthly free tier")
        return "\n".join(out)


def ensure_discovered(conn: sqlite3.Connection, run_id: str, cfg: Config | None = None) -> None:
    """Raise :class:`DiscoveryIncomplete` unless Phase 1 has finished for the run.

    A run whose bbox has no seed cell at all (e.g. a bbox entirely at sea) has nothing to
    discover; with ``cfg`` given that case counts as finished (an empty run), not as an error.
    """
    started = conn.execute("SELECT 1 FROM cells WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
    pending = conn.execute("SELECT 1 FROM cells WHERE run_id=? AND status='pending' LIMIT 1", (run_id,)).fetchone()
    if not started and cfg is not None and not seed_cells(run_bbox(cfg), cfg.seed_cell_deg, True,
                                                          cfg.boundary_buffer_deg):
        return
    if not started or pending:
        raise DiscoveryIncomplete(f"discovery has not finished for run {run_id}; run `discover` first")


@dataclass
class FreeEstimate:
    """Free mode: planned calls per instrument within the month's remaining free caps."""

    cfg: Config
    forecast: FreeForecast
    audit_calls: int
    audit_key: str
    audit_remaining: int | None
    month: str
    paid_equivalent_usd: float = 0.0

    def counts(self) -> dict[tuple[str, str], int]:
        """Planned calls per (endpoint, sku) (all within the free caps)."""
        c: dict[tuple[str, str], int] = {}
        for line in self.forecast.lines:
            if line.planned:
                ep, sku = line.key.split("/", 1)
                c[(ep, sku)] = c.get((ep, sku), 0) + line.planned
        if self.audit_calls:
            ep, sku = self.audit_key.split("/", 1)
            c[(ep, sku)] = c.get((ep, sku), 0) + self.audit_calls
        return c

    def render(self) -> str:
        """Human-readable free-mode estimate (cost is always $0.00)."""
        f, cfg = self.forecast, self.cfg
        fmt = lambda v: "unlimited" if v is None else f"{v:,}"  # noqa: E731
        out = [f"Free mode    : area {cfg.area}, tier {cfg.tier}, month {self.month} (UTC), margin "
               f"{cfg.free_cap_margin:.2f} (usable = floor(cap x margin), month-to-date over all runs)",
               f"Discovered   : {f.discovered:,} IDs (Phase 1, free); already fetched this run: "
               f"{f.already_covered:,}",
               f"  {'instrument':<30}{'planned':>9}{'remaining':>11}{'usable':>9}{'cap':>8}   covers"]
        for ln in f.lines:
            cov = f"{ln.covers:,} {'exact' if ln.exact else 'est.'}" if ln.planned else "-"
            out.append(f"  {ln.instrument.label + ' ' + ln.instrument.name:<30}{ln.planned:>9,}"
                       f"{fmt(ln.remaining):>11}{fmt(ln.usable):>9}{fmt(ln.cap):>8}   {cov}")
        out.append(f"  {'audit ' + self.audit_key:<30}{self.audit_calls:>9,}{fmt(self.audit_remaining):>11}"
                   f"{fmt(usable_cap(cfg, self.audit_key)):>9}{fmt(cfg.free_caps.get(self.audit_key)):>8}")
        todo = f.discovered - f.already_covered
        out.append(f"Expected     : {f.covered_text + f.covered_nearby_est + f.covered_details:,} of "
                   f"{todo:,} uncovered IDs fetched (Text Search {f.covered_text:,} exact, Nearby "
                   f"~{f.covered_nearby_est:,} est., Details {f.covered_details:,} exact); expected "
                   f"unfetched {f.unfetched_est:,}")
        out.append(f"Estimated cost: $0.00 (free mode: every planned call is within the remaining "
                   f"free monthly caps); would cost ${self.paid_equivalent_usd:,.2f} at list prices "
                   f"if paid (informational)")
        return "\n".join(out)


def free_estimate(conn: sqlite3.Connection, run_id: str, cfg: Config, audit_cells: int) -> FreeEstimate:
    """Persist Round T (once) and forecast Rounds T, N, D and the audit within the free caps."""
    build_free_plan(conn, run_id, cfg)
    fc = free_forecast(conn, run_id, cfg)
    key = f"nearby_search/{detect_sku('nearby_search', AUDIT_MASK)}"
    left = remaining_calls(conn, run_id, cfg).get(key, 0)
    calls = pending_audit_calls(conn, run_id, cfg, audit_cells)
    est = FreeEstimate(cfg, fc, calls if left is None else min(calls, left), key, left, current_month())
    est.paid_equivalent_usd = CostModel(cfg.pricing).totals(est.counts())[0]
    return est


def pending_audit_calls(conn: sqlite3.Connection, run_id: str, cfg: Config, audit_cells: int) -> int:
    """Audit Nearby Search calls still to be made for the sampled cells."""
    done = {(r[0], r[1]) for r in conn.execute("SELECT cell_id, type FROM audit_cells WHERE run_id=?", (run_id,))}
    return sum((c.cell_id, t) not in done for c, t in sample_cells(conn, run_id, cfg, audit_cells))


def estimate(conn: sqlite3.Connection, run_id: str, cfg: Config,
             audit_cells: int) -> Estimate | FreeEstimate:
    """Build (or load) the fetch plan and forecast the calls still to be made, audit included.

    Free mode returns a :class:`FreeEstimate` ($0.00); paid mode prices the min-cost plan.
    """
    ensure_discovered(conn, run_id, cfg)
    if cfg.free_only:
        return free_estimate(conn, run_id, cfg, audit_cells)
    plan = build_plan(conn, run_id, cfg)
    pending = plan_totals(conn, run_id, cfg, pending_only=True)
    audit_calls = pending_audit_calls(conn, run_id, cfg, audit_cells)
    est = Estimate(plan, pending, audit_calls, detect_sku("nearby_search", AUDIT_MASK), [], 0.0, 0.0)
    model = CostModel(cfg.pricing)
    est.lines = model.lines(est.counts())
    est.gross_usd, est.net_usd = model.totals(est.counts())
    return est


def check_budget(est: Estimate | FreeEstimate, budget: float) -> None:
    """Raise :class:`BudgetExceeded` if the gross estimate is above ``budget`` (paid mode only;
    in free mode ``--max-budget-usd`` is ignored and the free-cap guard applies instead)."""
    if isinstance(est, FreeEstimate):
        return
    if est.gross_usd > budget:
        raise BudgetExceeded(f"estimated cost ${est.gross_usd:,.2f} exceeds --max-budget-usd "
                             f"${budget:,.2f}; no paid call was made")




def finalize(ctx: RunContext, out_dir: Path, all_countries: bool,
             summary_dir: Path | None, city: str | None = None) -> tuple[dict[str, Any], int]:
    """Validate, set the run status, write exports; return (summary, exit code)."""
    rep = validate(ctx.conn, ctx.run_id)
    recall = ctx.conn.execute("SELECT audit_recall FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()[0]
    if not rep.ok:
        status, code = "validation_failed", EXIT_QUALITY
    elif recall is not None and recall < ctx.cfg.min_recall:
        status, code = "audit_failed", EXIT_QUALITY
    else:
        status, code = "completed", EXIT_OK
    set_status(ctx.conn, ctx.run_id, status)
    summary = export(ctx.conn, ctx.run_id, ctx.cfg, out_dir, all_countries, summary_dir, city)
    return summary, code


@dataclass
class RunResult:
    """What ``run`` did."""

    discovered: DiscoverStats
    estimate: Estimate | FreeEstimate
    fetched: FetchStats
    audit: AuditResult | None
    summary: dict[str, Any]
    exit_code: int


async def run_all_phases(ctx: RunContext, *, cold_start: bool, out_dir: Path, all_countries: bool,
                         summary_dir: Path | None, city: str | None = None) -> RunResult:
    """Discover -> estimate (+ budget guard in paid mode) -> fetch -> audit -> validate/export."""
    disc = await discover(ctx, cold_start)
    est = estimate(ctx.conn, ctx.run_id, ctx.cfg, ctx.cfg.audit_cells)
    try:
        check_budget(est, ctx.cfg.max_budget_usd)
    except BudgetExceeded as exc:
        set_status(ctx.conn, ctx.run_id, "budget_aborted", str(exc))
        raise
    fetched = await fetch(ctx)
    result = await audit(ctx, ctx.cfg.audit_cells) if ctx.cfg.audit_cells > 0 else None
    summary, code = finalize(ctx, out_dir, all_countries, summary_dir, city)
    return RunResult(disc, est, fetched, result, summary, code)

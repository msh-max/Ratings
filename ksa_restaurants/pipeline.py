"""Orchestration of the four phases, the cost estimate and the budget guard."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import AuditResult, audit, sample_cells
from .config import AUDIT_MASK, Config
from .context import RunContext
from .cost import CostLine, CostModel, detect_sku
from .discover import DiscoverStats, discover
from .export import export
from .fetch import FetchStats, fetch
from .fetch_plan import Plan, build_plan, plan_totals
from .runs import set_status
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


def ensure_discovered(conn: sqlite3.Connection, run_id: str) -> None:
    """Raise :class:`DiscoveryIncomplete` unless Phase 1 has finished for the run."""
    started = conn.execute("SELECT 1 FROM cells WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
    pending = conn.execute("SELECT 1 FROM cells WHERE run_id=? AND status='pending' LIMIT 1", (run_id,)).fetchone()
    if not started or pending:
        raise DiscoveryIncomplete(f"discovery has not finished for run {run_id}; run `discover` first")


def estimate(conn: sqlite3.Connection, run_id: str, cfg: Config, audit_cells: int) -> Estimate:
    """Build (or load) the fetch plan and price the paid calls still to be made, audit included."""
    ensure_discovered(conn, run_id)
    plan = build_plan(conn, run_id, cfg)
    pending = plan_totals(conn, run_id, cfg, pending_only=True)
    done = {(r[0], r[1]) for r in conn.execute("SELECT cell_id, type FROM audit_cells WHERE run_id=?", (run_id,))}
    audit_calls = sum((c.cell_id, t) not in done for c, t in sample_cells(conn, run_id, cfg, audit_cells))
    est = Estimate(plan, pending, audit_calls, detect_sku("nearby_search", AUDIT_MASK), [], 0.0, 0.0)
    model = CostModel(cfg.pricing)
    est.lines = model.lines(est.counts())
    est.gross_usd, est.net_usd = model.totals(est.counts())
    return est


def check_budget(est: Estimate, budget: float) -> None:
    """Raise :class:`BudgetExceeded` if the gross estimate is above ``budget``."""
    if est.gross_usd > budget:
        raise BudgetExceeded(f"estimated cost ${est.gross_usd:,.2f} exceeds --max-budget-usd "
                             f"${budget:,.2f}; no paid call was made")




def finalize(ctx: RunContext, out_dir: Path, all_countries: bool,
             summary_dir: Path | None) -> tuple[dict[str, Any], int]:
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
    summary = export(ctx.conn, ctx.run_id, ctx.cfg, out_dir, all_countries, summary_dir)
    return summary, code


@dataclass
class RunResult:
    """What ``run`` did."""

    discovered: DiscoverStats
    estimate: Estimate
    fetched: FetchStats
    audit: AuditResult | None
    summary: dict[str, Any]
    exit_code: int


async def run_all_phases(ctx: RunContext, *, cold_start: bool, out_dir: Path, all_countries: bool,
                         summary_dir: Path | None) -> RunResult:
    """Discover -> estimate + budget guard -> fetch -> audit -> validate/export."""
    disc = await discover(ctx, cold_start)
    est = estimate(ctx.conn, ctx.run_id, ctx.cfg, ctx.cfg.audit_cells)
    try:
        check_budget(est, ctx.cfg.max_budget_usd)
    except BudgetExceeded as exc:
        set_status(ctx.conn, ctx.run_id, "budget_aborted", str(exc))
        raise
    fetched = await fetch(ctx)
    result = await audit(ctx, ctx.cfg.audit_cells) if ctx.cfg.audit_cells > 0 else None
    summary, code = finalize(ctx, out_dir, all_countries, summary_dir)
    return RunResult(disc, est, fetched, result, summary, code)

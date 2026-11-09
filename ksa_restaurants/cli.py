"""``ksa-restaurants`` command line interface (click)."""

from __future__ import annotations

import asyncio
import functools
import logging
import sqlite3
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from . import db as dbmod
from .audit import audit as run_audit
from .config import OPERATIONAL_FIELDS, SMOKE_BBOX, TYPE_PRESETS, Config, load_config
from .context import RunContext
from .discover import discover as run_discover
from .fake import DEFAULT_FAKE_BBOX, FakePlacesClient, FakeWorld
from .fetch import fetch as run_fetch
from .geo import parse_bbox
from .pipeline import (EXIT_BUDGET, EXIT_ERROR, EXIT_QUALITY, BudgetExceeded, DiscoveryIncomplete,
                       check_budget, estimate, finalize, run_all_phases)
from .places_client import PlacesApi, PlacesApiError, PlacesClient
from .runs import default_run_id, latest_run_id, load_run_config, open_run, run_bbox, runs_before
from .summary import build_summary, one_screen
from .validate import ValidationFailed

FAKE_WORLD_SEED, FAKE_WORLD_SIZE = 42, 3000
RIYADH_CENTRE = ((SMOKE_BBOX[0] + SMOKE_BBOX[2]) / 2, (SMOKE_BBOX[1] + SMOKE_BBOX[3]) / 2)


@dataclass
class Options:
    """Common options given before the subcommand."""

    db: Path
    bbox: str | None
    types_preset: str
    types: tuple[str, ...]
    tier: str
    language: str
    qps: float
    concurrency: int
    max_budget_usd: float
    max_budget_explicit: bool
    fake: bool
    run_id: str | None
    cold_start: bool
    include_reviews: bool
    config: Path | None


def build_config(o: Options, bbox: tuple[float, float, float, float] | None = None,
                 **operational: Any) -> Config:
    """Config from the --config file plus command-line options."""
    cfg = load_config(o.config)
    box = bbox or (parse_bbox(o.bbox) if o.bbox else cfg.bbox)
    return cfg.model_copy(update={
        "bbox": box, "types": list(o.types) or list(TYPE_PRESETS[o.types_preset]), "tier": o.tier,
        "language": o.language, "include_reviews": o.include_reviews or cfg.include_reviews,
        "qps": o.qps, "concurrency": o.concurrency, "max_budget_usd": o.max_budget_usd, **operational})


def make_client(o: Options, conn: sqlite3.Connection, run_id: str, cfg: Config) -> PlacesApi:
    """The real client, or with ``--fake`` a synthetic Riyadh-area world (churned per past run)."""
    if not o.fake:
        return PlacesClient(qps=cfg.qps, concurrency=cfg.concurrency)
    bbox = run_bbox(cfg)
    world = FakeWorld.generate(FAKE_WORLD_SEED, FAKE_WORLD_SIZE, DEFAULT_FAKE_BBOX, cfg.types,
                               grid_origin=(bbox[0], bbox[1]), seed_cell_deg=cfg.seed_cell_deg,
                               min_cell_deg=cfg.min_cell_deg, core_centers=[RIYADH_CENTRE])
    world.evolve(runs_before(conn, run_id))
    return FakePlacesClient(world)


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING if verbose == 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # never log request lines


def command(fn: Callable[..., Coroutine[Any, Any, int | None]]) -> Callable[..., None]:
    """Run an async command body; map known errors to exit codes (2 budget, 3 quality, 1 other)."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            code = asyncio.run(fn(*args, **kwargs)) or 0
        except BudgetExceeded as exc:
            click.echo(f"BUDGET ABORT: {exc}", err=True)
            code = EXIT_BUDGET
        except ValidationFailed as exc:
            click.echo(f"VALIDATION FAILED: {exc}", err=True)
            code = EXIT_QUALITY
        except (PlacesApiError, DiscoveryIncomplete, click.UsageError) as exc:
            click.echo(f"error: {exc}", err=True)
            code = EXIT_ERROR
        sys.exit(code)
    return wrapper


async def _with_run(o: Options, create: bool, body: Callable[[RunContext], Coroutine[Any, Any, int | None]],
                    bbox: tuple[float, float, float, float] | None = None, **operational: Any) -> int | None:
    """Open the DB and the run (create/resume, or the given/latest existing one) and run ``body``."""
    conn = dbmod.connect(o.db)
    try:
        cli_cfg = build_config(o, bbox, **operational)
        if create:
            run_id = o.run_id or default_run_id()
            cfg = open_run(conn, run_id, cli_cfg)
        else:
            run_id = o.run_id or latest_run_id(conn) or ""
            stored = load_run_config(conn, run_id)
            if stored is None:
                raise click.UsageError(f"no run {run_id!r} in {o.db}; run `discover` or `run` first")
            cfg = stored.model_copy(update={k: getattr(cli_cfg, k) for k in OPERATIONAL_FIELDS})
        client = make_client(o, conn, run_id, cfg)
        try:
            return await body(RunContext(conn, run_id, cfg, client))
        finally:
            await client.aclose()
    finally:
        conn.close()


def audit_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``--audit-cells`` and ``--min-recall``."""
    fn = click.option("--min-recall", type=float, default=0.97, show_default=True,
                      help="Exit 3 if the audit recall is below this.")(fn)
    return click.option("--audit-cells", type=int, default=100, show_default=True,
                        help="Leaves to audit with Nearby Search (0 disables the audit).")(fn)


def export_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``--out``, ``--all-countries`` and ``--summary-dir``."""
    fn = click.option("--summary-dir", type=click.Path(path_type=Path), default=Path("data/summary"),
                      show_default=True, help="Also write latest.json, YYYY-MM.json and latest.md here.")(fn)
    fn = click.option("--all-countries", is_flag=True, help="Export non-Saudi places too.")(fn)
    return click.option("--out", type=click.Path(path_type=Path), default=Path("data/exports"),
                        show_default=True, help="Export directory.")(fn)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/ksa_restaurants.sqlite"),
              show_default=True, help="SQLite database file.")
@click.option("--bbox", help="lat_lo,lng_lo,lat_hi,lng_hi (default: whole Saudi Arabia).")
@click.option("--types-preset", type=click.Choice(sorted(TYPE_PRESETS)), default="restaurants",
              show_default=True, help="Place types to enumerate.")
@click.option("--type", "types", multiple=True, help="Place type (repeatable); overrides the preset.")
@click.option("--tier", type=click.Choice(["enterprise", "atmosphere"]), default="atmosphere",
              show_default=True, help="Phase 2 field-mask tier.")
@click.option("--language", type=click.Choice(["en", "ar"]), default="en", show_default=True)
@click.option("--qps", type=float, default=9.0, show_default=True, help="Max requests per second.")
@click.option("--concurrency", type=int, default=8, show_default=True, help="Max requests in flight.")
@click.option("--max-budget-usd", type=float, default=1000.0, show_default=True,
              help="Abort (exit 2) before any paid call if the estimate exceeds this.")
@click.option("--fake", is_flag=True, help="Use the synthetic FakePlacesClient (no key, no cost).")
@click.option("--run-id", help="Run id (default YYYY-MM-DDTHHMM UTC; pass an existing id to resume). "
              "Commands other than discover/run/smoke default to the latest run.")
@click.option("--cold-start", is_flag=True, help="Ignore the previous run's leaf cells.")
@click.option("--include-reviews", is_flag=True, help="Also request `reviews` (bulky).")
@click.option("--config", type=click.Path(path_type=Path, exists=True),
              help="JSON file overriding Config fields (field lists, exclude_fields, pricing, ...).")
@click.option("-v", "--verbose", count=True, help="-v: one INFO line per cell; -vv: debug.")
@click.pass_context
def main(ctx: click.Context, verbose: int, **kw: Any) -> None:
    """Enumerate every restaurant in Saudi Arabia with the Google Places API (New).

    The API key is read from the GOOGLE_MAPS_API_KEY environment variable.
    Exit codes: 0 ok, 1 error, 2 budget abort, 3 audit/validation failure.
    """
    _setup_logging(verbose)
    explicit = ctx.get_parameter_source("max_budget_usd") != click.core.ParameterSource.DEFAULT
    ctx.obj = Options(max_budget_explicit=explicit, **kw)


@main.command()
@click.pass_obj
@command
async def discover(o: Options) -> int:
    """Phase 1: free IDs-only quadtree discovery (resumable)."""
    async def body(ctx: RunContext) -> int:
        s = await run_discover(ctx, o.cold_start)
        click.echo(f"run {ctx.run_id}: {s.cells:,} cells, {s.leaves:,} leaves, "
                   f"{s.saturated_at_min_size} saturated at min size, {s.place_ids:,} place IDs, "
                   f"{s.calls:,} free calls")
        return 0
    return await _with_run(o, True, body) or 0


@main.command("estimate")
@audit_options
@click.pass_obj
@command
async def estimate_cmd(o: Options, audit_cells: int, min_recall: float) -> int:
    """Build the Phase 2 plan and print its exact cost (free; no paid call)."""
    async def body(ctx: RunContext) -> int:
        click.echo(f"run {ctx.run_id}")
        click.echo(estimate(ctx.conn, ctx.run_id, ctx.cfg, audit_cells).render())
        return 0
    return await _with_run(o, False, body, audit_cells=audit_cells, min_recall=min_recall) or 0


@main.command()
@click.pass_obj
@command
async def fetch(o: Options) -> int:
    """Phase 2: execute the fetch plan (aborts with exit 2 if over budget)."""
    async def body(ctx: RunContext) -> int:
        est = estimate(ctx.conn, ctx.run_id, ctx.cfg, 0)
        click.echo(est.render())
        check_budget(est, ctx.cfg.max_budget_usd)
        s = await run_fetch(ctx)
        click.echo(f"run {ctx.run_id}: {s.snapshots:,} snapshots, {s.errors:,} fetch errors, "
                   f"{s.swept:,} fetched by the final sweep")
        return 0
    return await _with_run(o, False, body) or 0


@main.command("audit")
@audit_options
@click.pass_obj
@command
async def audit_cmd(o: Options, audit_cells: int, min_recall: float) -> int:
    """Phase 3: Nearby Search recall audit (exit 3 if recall < --min-recall)."""
    async def body(ctx: RunContext) -> int:
        est = estimate(ctx.conn, ctx.run_id, ctx.cfg, audit_cells)
        check_budget(est, ctx.cfg.max_budget_usd)
        r = await run_audit(ctx, audit_cells)
        rec = "n/a (no in-scope place found)" if r.recall is None else f"{r.recall:.4f}"
        share = "n/a" if r.out_of_scope_share is None else f"{100 * r.out_of_scope_share:.1f}%"
        click.echo(f"run {ctx.run_id}: audited {r.cells} cells, found {r.found}, in scope {r.in_scope}, "
                   f"matched {r.matched}, recall {rec}, out-of-scope share {share}")
        return EXIT_QUALITY if r.recall is not None and r.recall < min_recall else 0
    return await _with_run(o, False, body, audit_cells=audit_cells, min_recall=min_recall) or 0


@main.command("export")
@export_options
@click.option("--min-recall", type=float, default=0.97, show_default=True,
              help="Status audit_failed and exit 3 if the run's audit recall is below this.")
@click.pass_obj
@command
async def export_cmd(o: Options, out: Path, all_countries: bool, summary_dir: Path,
                     min_recall: float) -> int:
    """Phase 4: validate, set the run status, write exports, summary.json and report.md."""
    async def body(ctx: RunContext) -> int:
        summary, code = finalize(ctx, out, all_countries, summary_dir)
        click.echo(one_screen(summary))
        return code
    return await _with_run(o, False, body, min_recall=min_recall) or 0


@main.command()
@click.pass_obj
@command
async def report(o: Options) -> int:
    """Print the one-screen summary of a run (from the DB; writes no files)."""
    async def body(ctx: RunContext) -> int:
        click.echo(one_screen(build_summary(ctx.conn, ctx.run_id, ctx.cfg)))
        return 0
    return await _with_run(o, False, body) or 0


async def _run(o: Options, audit_cells: int, min_recall: float, out: Path, all_countries: bool,
               summary_dir: Path, bbox: tuple[float, float, float, float] | None = None,
               **operational: Any) -> int:
    async def body(ctx: RunContext) -> int:
        result = await run_all_phases(ctx, cold_start=o.cold_start, out_dir=out,
                                      all_countries=all_countries, summary_dir=summary_dir)
        click.echo(result.estimate.render())
        click.echo("")
        click.echo(one_screen(result.summary))
        return result.exit_code
    return await _with_run(o, True, body, bbox, audit_cells=audit_cells, min_recall=min_recall,
                           **operational) or 0


@main.command()
@audit_options
@export_options
@click.pass_obj
@command
async def run(o: Options, audit_cells: int, min_recall: float, out: Path, all_countries: bool,
              summary_dir: Path) -> int:
    """All phases: discover, estimate + budget guard, fetch, audit, validate, export."""
    return await _run(o, audit_cells, min_recall, out, all_countries, summary_dir)


@main.command()
@audit_options
@export_options
@click.pass_obj
@command
async def smoke(o: Options, audit_cells: int, min_recall: float, out: Path, all_countries: bool,
                summary_dir: Path) -> int:
    """`run` on a ~2x2 km bbox in central Riyadh with a default budget of 5 USD."""
    if o.run_id is None:
        o.run_id = "smoke-" + default_run_id()
    budget = o.max_budget_usd if o.max_budget_explicit else 5.0
    return await _run(o, audit_cells, min_recall, out, all_countries, summary_dir, SMOKE_BBOX,
                      max_budget_usd=budget)

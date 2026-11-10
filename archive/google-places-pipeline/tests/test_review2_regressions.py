"""Regressions from the second review of free mode: month change, billed retries, page cap."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from conftest import caps, make_config, make_ctx, make_world
from ksa_restaurants import db
from ksa_restaurants.context import RunContext
from ksa_restaurants.discover import discover
from ksa_restaurants.fetch import fetch
from ksa_restaurants.fetch_plan import build_free_plan, remaining_calls
from ksa_restaurants.places_client import PlacesApiError, PlacesClient
from ksa_restaurants.runs import month_to_date_usage, open_run, usage_for_guard


def _months_ago(n: int, day: int = 28) -> str:
    now = datetime.now(timezone.utc)
    y, m = divmod(now.year * 12 + now.month - 1 - n, 12)
    return f"{y:04d}-{m + 1:02d}-{day:02d}T23:00:00+00:00"


def _add_run(conn, run_id: str, started: str, last_call: str | None, finished: str | None,
             calls: int) -> None:
    conn.execute("INSERT INTO runs(run_id, started_at, finished_at, last_call_at, status, bbox_json) "
                 "VALUES (?,?,?,?,?,?)", (run_id, started, finished, last_call, "completed", "[]"))
    conn.execute("INSERT INTO api_calls VALUES (?,?,?,?,?)",
                 (run_id, "fetch", "place_details", "atmosphere", calls))


def test_run_spanning_the_month_change_counts_in_the_new_month(tmp_path):
    """A run started last month whose calls continued into this month counts toward this month's
    caps for OTHER runs too (previously only runs started this month were counted)."""
    conn = db.connect(tmp_path / "m.sqlite")
    now = _months_ago(0, day=1)
    _add_run(conn, "spans", _months_ago(1), now, now, 500)               # last call this month
    _add_run(conn, "old", _months_ago(1), _months_ago(1), _months_ago(1), 700)  # all last month
    _add_run(conn, "legacy", _months_ago(1), None, now, 11)              # pre-column: finished_at
    _add_run(conn, "crashed", _months_ago(2), None, None, 13)            # never finished, old
    mtd = month_to_date_usage(conn)
    assert mtd == {"place_details/atmosphere": 511}
    cfg = make_config()
    conn.execute("INSERT INTO runs(run_id, started_at, status, bbox_json) VALUES ('new', ?, 'running', '[]')",
                 (now,))
    assert remaining_calls(conn, "new", cfg)["place_details/atmosphere"] == 900 - 511
    # a resumed run that started earlier always counts its own calls, once
    assert usage_for_guard(conn, "old") == {"place_details/atmosphere": 511 + 700}
    assert usage_for_guard(conn, "spans") == {"place_details/atmosphere": 511}


async def test_guard_sees_calls_of_a_run_that_spanned_the_month_change(tmp_path):
    world = make_world(seed=31, n=600)
    cfg = make_config(free_caps=caps(10), audit_cells=0)
    ctx1, _ = make_ctx(tmp_path / "g.sqlite", world, "r1", cfg)
    await discover(ctx1)
    await fetch(ctx1)
    used = usage_for_guard(ctx1.conn, "r1")
    assert used.get("text_search/atmosphere", 0) > 0
    # r1 started last month; its calls (stamped last_call_at = now) were made this month
    ctx1.conn.execute("UPDATE runs SET started_at=? WHERE run_id='r1'", (_months_ago(1),))
    conn = ctx1.conn
    open_run(conn, "r2", cfg)
    assert usage_for_guard(conn, "r2") == {k: v for k, v in used.items()}


def _metered_ctx(tmp_path, handler) -> RunContext:
    conn = db.connect(tmp_path / "c.sqlite")
    cfg = make_config()
    open_run(conn, "r", cfg)
    client = PlacesClient(api_key="k", qps=1000, backoff_base=0.0, max_attempts=3,
                          transport=httpx.MockTransport(handler))
    return RunContext(conn, "r", cfg, client)


def _recorded(ctx: RunContext) -> int:
    return sum(ctx.call_counts().values())


async def test_retry_after_429_is_recorded_once(tmp_path):
    n = []

    def handler(req):
        n.append(1)
        if len(n) == 1:
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        return httpx.Response(200, json={"id": "p"})

    ctx = _metered_ctx(tmp_path, handler)
    await ctx.place_details("fetch", "p", ctx.cfg.details_mask())
    assert len(n) == 2 and _recorded(ctx) == 1  # the 429 is not billed
    assert ctx._in_flight["place_details/atmosphere"] == 0


async def test_read_timeout_attempts_count_as_possibly_billed(tmp_path):
    n = []

    def handler(req):
        n.append(1)
        if len(n) == 1:
            raise httpx.ReadTimeout("slow", request=req)  # Google may have answered (billed) 200
        if len(n) == 2:
            raise httpx.ConnectError("no route", request=req)  # never reached Google
        return httpx.Response(200, json={"id": "p"})

    ctx = _metered_ctx(tmp_path, handler)
    await ctx.place_details("fetch", "p", ctx.cfg.details_mask())
    assert len(n) == 3 and _recorded(ctx) == 2


async def test_failed_call_still_records_possibly_billed_attempts(tmp_path):
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    ctx = _metered_ctx(tmp_path, handler)
    with pytest.raises(PlacesApiError):
        await ctx.place_details("fetch", "p", ctx.cfg.details_mask())
    assert _recorded(ctx) == 3 and ctx._in_flight["place_details/atmosphere"] == 0


async def test_200_with_unparsable_body_is_recorded(tmp_path):
    ctx = _metered_ctx(tmp_path, lambda req: httpx.Response(200, content=b"not json"))
    with pytest.raises(ValueError):
        await ctx.place_details("fetch", "p", ctx.cfg.details_mask())
    assert _recorded(ctx) == 1


async def test_free_text_job_never_sends_more_pages_than_planned(tmp_path):
    world = make_world(seed=32, n=1500)
    cfg = make_config(audit_cells=0)
    ctx, client = make_ctx(tmp_path / "p.sqlite", world, "r1", cfg)
    await discover(ctx)
    build_free_plan(ctx.conn, "r1", cfg)
    job = ctx.conn.execute("SELECT * FROM fetch_jobs WHERE run_id='r1' AND kind='text' AND pages>=2 "
                           "ORDER BY job_id LIMIT 1").fetchone()
    assert job is not None
    ctx.conn.execute("UPDATE fetch_jobs SET pages=1 WHERE run_id='r1' AND job_id=?", (job["job_id"],))
    await fetch(ctx)
    calls = ctx.conn.execute("SELECT fetch_calls FROM cells WHERE run_id='r1' AND cell_id=? AND type=?",
                             (job["cell_id"], job["type"])).fetchone()[0]
    assert calls == 1
    assert ctx.conn.execute("SELECT COALESCE(unfetched,0) FROM runs WHERE run_id='r1'").fetchone()[0] == 0

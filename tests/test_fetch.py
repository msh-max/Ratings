"""Tests 2 and 4: fetch correctness, one snapshot per place per run, plan optimality, budget guard."""

from __future__ import annotations

import json

import pytest

from conftest import make_config, make_ctx
from ksa_restaurants.audit import audit
from ksa_restaurants.discover import discover
from ksa_restaurants.fetch import fetch
from ksa_restaurants.fetch_plan import greedy_cover
from ksa_restaurants.pipeline import BudgetExceeded, check_budget, estimate, run_all_phases


async def test_fetch_stores_every_place_with_correct_fields(world, dbpath):
    ctx, client = make_ctx(dbpath, world)
    await discover(ctx)
    estimate(ctx.conn, "r1", ctx.cfg, 0)
    stats = await fetch(ctx)
    discovered = {r[0] for r in ctx.conn.execute("SELECT DISTINCT place_id FROM cell_places")}
    snaps = ctx.conn.execute("SELECT place_id FROM place_snapshots WHERE run_id='r1'").fetchall()
    assert len(snaps) == len({r[0] for r in snaps}) == len(discovered) == stats.snapshots
    assert stats.errors == 0 and stats.swept == 0
    for row in ctx.conn.execute("SELECT * FROM places"):
        src = world.places[row["place_id"]]
        assert row["display_name"] == src["displayName"]["text"]
        assert (row["lat"], row["lng"]) == (src["location"]["latitude"], src["location"]["longitude"])
        assert row["rating"] == src.get("rating")
        assert row["user_rating_count"] == src["userRatingCount"]
        assert row["price_level"] == src["priceLevel"]
        assert row["website_uri"] == src.get("websiteUri")
        assert json.loads(row["price_range_json"]) == src["priceRange"]
        assert json.loads(row["weekday_descriptions_json"]) == src["regularOpeningHours"]["weekdayDescriptions"]
        assert row["photo_count"] == len(src["photos"])
        assert json.loads(row["atmosphere_json"])["dineIn"] is True
        cc = [c["shortText"] for c in src["addressComponents"] if "country" in c["types"]]
        assert row["country_code"] == (cc[0] if cc else None)
        assert row["is_saudi"] == int(row["country_code"] in ("SA", None))
        assert (row["first_seen_run_id"], row["last_seen_run_id"]) == ("r1", "r1")
        assert "reviews" not in json.loads(row["raw_json"])  # not requested by default
    assert {r[0] for r in ctx.conn.execute("SELECT country_code FROM places")} >= {"SA", None}


async def test_estimate_equals_actual_paid_calls(world, dbpath):
    ctx, client = make_ctx(dbpath, world)
    await discover(ctx)
    est = estimate(ctx.conn, "r1", ctx.cfg, ctx.cfg.audit_cells)
    assert est.plan.text_jobs > 0 and est.plan.details_jobs > 0 and est.audit_calls > 0
    assert client.paid_calls == {}
    await fetch(ctx)
    await audit(ctx, ctx.cfg.audit_cells)
    assert dict(client.paid_calls) == est.counts()
    gross, _ = ctx.cost.totals(client.paid_calls)
    assert gross == pytest.approx(est.gross_usd)
    db_counts = {k: v for k, v in ctx.call_counts().items() if k[1] != "ids_only"}
    assert db_counts == dict(client.paid_calls)


async def test_budget_guard_aborts_before_any_paid_call(world, dbpath, tmp_path):
    ctx, client = make_ctx(dbpath, world, cfg=make_config(max_budget_usd=0.5))
    with pytest.raises(BudgetExceeded):
        await run_all_phases(ctx, cold_start=False, out_dir=tmp_path / "x", all_countries=False,
                             summary_dir=None)
    assert client.total_calls > 0 and client.paid_calls == {}
    assert ctx.conn.execute("SELECT COUNT(*) FROM place_snapshots").fetchone()[0] == 0
    assert ctx.conn.execute("SELECT status FROM runs").fetchone()[0] == "budget_aborted"
    est = estimate(ctx.conn, "r1", ctx.cfg, ctx.cfg.audit_cells)
    check_budget(est, est.gross_usd)  # exactly at budget is allowed


async def test_removed_place_becomes_fetch_error(world, dbpath):
    ctx, client = make_ctx(dbpath, world)
    await discover(ctx)
    estimate(ctx.conn, "r1", ctx.cfg, 0)
    gone = ctx.conn.execute("SELECT place_id FROM fetch_jobs WHERE kind='details' LIMIT 1").fetchone()[0]
    in_text = ctx.conn.execute(
        "SELECT cp.place_id FROM cell_places cp JOIN fetch_jobs j ON j.cell_id=cp.cell_id AND j.type=cp.type "
        "WHERE j.kind='text' LIMIT 1").fetchone()[0]
    world.remove(gone)
    world.remove(in_text)
    stats = await fetch(ctx)
    errors = {r[0]: r[1] for r in ctx.conn.execute("SELECT place_id, code FROM fetch_errors")}
    assert errors == {gone: "NOT_FOUND", in_text: "NOT_FOUND"}  # text-job place caught by the sweep
    assert stats.swept == 1
    assert ctx.conn.execute("SELECT status FROM fetch_jobs WHERE place_id=?", (gone,)).fetchone()[0] == "error"


async def test_places_returned_by_text_search_but_not_discovered_are_kept(world, dbpath):
    ctx, _ = make_ctx(dbpath, world)
    await discover(ctx)
    estimate(ctx.conn, "r1", ctx.cfg, 0)
    job = ctx.conn.execute("SELECT c.* FROM fetch_jobs j JOIN cells c ON c.cell_id=j.cell_id AND c.type=j.type "
                           "WHERE j.kind='text' AND c.result_count < 20 LIMIT 1").fetchone()
    newcomer = dict(next(iter(world.places.values())), id="ChIJnewcomer", name="places/ChIJnewcomer",
                    types=[job["type"]], userRatingCount=10**6,
                    location={"latitude": (job["lat_lo"] + job["lat_hi"]) / 2,
                              "longitude": (job["lng_lo"] + job["lng_hi"]) / 2})
    world.places["ChIJnewcomer"] = newcomer
    world._reindex()
    await fetch(ctx)
    assert ctx.conn.execute("SELECT 1 FROM place_snapshots WHERE place_id='ChIJnewcomer'").fetchone()


def test_greedy_cover_is_cost_aware_and_deduplicates():
    ids = [f"p{i}" for i in range(100)]
    pairs = {("a", "restaurant"): set(ids[:20]),          # 1 page, 20 new
             ("a", "fast_food_restaurant"): set(ids[:20]),  # same IDs: never worth a second fetch
             ("b", "restaurant"): set(ids[20:60]),        # 2 pages, 40 new
             ("c", "restaurant"): {ids[60]},             # 1 page for 1 ID: Details is cheaper
             ("d", "restaurant"): set(ids[:19]) | {ids[61]}}  # 1 new ID after "a"
    chosen, rest = greedy_cover(pairs, p_text=0.040, p_details=0.025)
    assert {(c, t) for c, t, _ in chosen} in ({("a", "restaurant"), ("b", "restaurant")},
                                              {("a", "fast_food_restaurant"), ("b", "restaurant")})
    assert rest == {ids[60], ids[61]}
    covered = set().union(*(pairs[(c, t)] for c, t, _ in chosen)) | rest
    assert covered == set().union(*pairs.values())
    # break-even: 2 IDs on a 1-page job costs 0.04 vs 0.05 in Details -> text job
    chosen, rest = greedy_cover({("x", "t"): {"q1", "q2"}}, 0.040, 0.025)
    assert chosen == [("x", "t", 1)] and rest == set()
    chosen, rest = greedy_cover({("x", "t"): {"q1"}}, 0.040, 0.025)
    assert chosen == [] and rest == {"q1"}

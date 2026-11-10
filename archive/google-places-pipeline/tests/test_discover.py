"""Phase 1: completeness, exact tiling, saturation, page-token handling."""

from __future__ import annotations

import math

import pytest

from conftest import BBOX, SEED_DEG, make_ctx, make_world, wanted_ids
from ksa_restaurants.config import DISCOVER_MASK
from ksa_restaurants.discover import discover
from ksa_restaurants.geo import seed_cells
from ksa_restaurants.places_client import PlacesApiError


async def test_discovery_finds_every_place_and_leaves_tile_seeds(world, dbpath):
    ctx, client = make_ctx(dbpath, world)
    stats = await discover(ctx)
    found = {r[0] for r in ctx.conn.execute("SELECT place_id FROM cell_places WHERE run_id='r1'")}
    assert found == wanted_ids(world, ctx.cfg.types)  # 100 %, set equality
    assert stats.place_ids == len(found)
    assert client.paid_calls == {}  # Phase 1 is free (IDs-only SKU)
    assert set(client.calls) == {("text_search", "ids_only")}

    seeds = seed_cells(BBOX, SEED_DEG)
    seed_area = sum(c.area_deg2 for c in seeds)
    assert math.isclose(seed_area, 1.0, rel_tol=1e-12)
    rows = ctx.conn.execute("SELECT * FROM cells WHERE run_id='r1'").fetchall()
    by_key = {(r["type"], r["cell_id"]): r for r in rows}
    for place_type in ctx.cfg.types:
        cells = [r for r in rows if r["type"] == place_type]
        leaves = [r for r in cells if r["status"] in ("leaf", "saturated_leaf")]
        area = sum((r["lat_hi"] - r["lat_lo"]) * (r["lng_hi"] - r["lng_lo"]) for r in leaves)
        assert math.isclose(area, seed_area, rel_tol=1e-9)
        ids = {r["cell_id"] for r in leaves}
        seed_ids = {c.cell_id for c in seeds}
        for leaf in ids:  # no overlaps: no leaf is an ancestor of another leaf
            row_col, rest = leaf, ""
            while row_col not in seed_ids:
                row_col, rest = row_col[:-1], row_col[-1] + rest
            assert all(row_col + rest[:k] not in ids for k in range(len(rest)))
        # every split cell has exactly 4 children that tile it exactly (shared float edges)
        for r in cells:
            if r["status"] != "split":
                continue
            kids = [by_key[(place_type, r["cell_id"] + d)] for d in "0123"]
            assert {k["lat_lo"] for k in kids} | {k["lat_hi"] for k in kids} == \
                {r["lat_lo"], r["lat_hi"], (r["lat_lo"] + r["lat_hi"]) / 2}
            assert {k["lng_lo"] for k in kids} | {k["lng_hi"] for k in kids} == \
                {r["lng_lo"], r["lng_hi"], (r["lng_lo"] + r["lng_hi"]) / 2}
            assert math.isclose(sum((k["lat_hi"] - k["lat_lo"]) * (k["lng_hi"] - k["lng_lo"]) for k in kids),
                                (r["lat_hi"] - r["lat_lo"]) * (r["lng_hi"] - r["lng_lo"]), rel_tol=1e-12)
    assert max(r["depth"] for r in rows) >= 6  # dense cluster forces deep subdivision
    assert all(r["status"] != "pending" for r in rows)


async def test_food_court_is_saturated_at_min_size_but_still_covered(world, dbpath):
    ctx, _ = make_ctx(dbpath, world)
    stats = await discover(ctx)
    assert stats.saturated_at_min_size >= 1
    sat = ctx.conn.execute("SELECT * FROM cells WHERE status='saturated_leaf'").fetchall()
    for r in sat:
        assert r["type"] == "restaurant" and r["result_count"] == 60
        assert max(r["lat_hi"] - r["lat_lo"], r["lng_hi"] - r["lng_lo"]) <= ctx.cfg.min_cell_deg
    # the restaurant tree saw only 60 of the 75 food-court places, the other type trees saw the rest
    fc = [p for p in world.places.values() if p["types"][:1] == ["restaurant"]
          and p["types"][1] in ("fast_food_restaurant", "meal_takeaway") and len(p["types"]) == 5]
    found = {r[0] for r in ctx.conn.execute("SELECT place_id FROM cell_places")}
    assert len(fc) >= 75 and {p["id"] for p in fc} <= found


async def test_places_on_seed_edges_are_returned_by_both_cells(world, dbpath):
    ctx, _ = make_ctx(dbpath, world)
    await discover(ctx)
    edge = [p for p in world.places.values()
            if p["location"]["latitude"] in (24.25, 24.5, 24.75) and set(p["types"]) & set(ctx.cfg.types)]
    assert edge
    for p in edge:
        for t in set(p["types"]) & set(ctx.cfg.types):
            n = ctx.conn.execute("SELECT COUNT(*) FROM cell_places WHERE place_id=? AND type=?",
                                 (p["id"], t)).fetchone()[0]
            assert n >= 2  # in a leaf on each side of the edge; deduplicated by ID later


async def test_follow_up_pages_repeat_every_parameter(world, dbpath):
    ctx, client = make_ctx(dbpath, world)
    await discover(ctx)
    firsts = [b for e, b, _ in client.requests if e == "text_search" and "pageToken" not in b]
    follow = [b for e, b, _ in client.requests if e == "text_search" and "pageToken" in b]
    assert follow
    for b in follow:
        rest = {k: v for k, v in b.items() if k != "pageToken"}
        assert rest in firsts
        assert set(rest["locationRestriction"]) == {"rectangle"}


async def test_fake_rejects_page_token_with_changed_parameters():
    from ksa_restaurants.fake import FakePlacesClient
    client = FakePlacesClient(make_world(n=400, dense_cluster=False, food_court=False))
    body = {"textQuery": "restaurant", "includedType": "restaurant", "strictTypeFiltering": True,
            "pageSize": 20, "locationRestriction": {"rectangle": {
                "low": {"latitude": BBOX[0], "longitude": BBOX[1]},
                "high": {"latitude": BBOX[2], "longitude": BBOX[3]}}}}
    first = await client.search_text(body, DISCOVER_MASK)
    assert len(first["places"]) == 20 and set(first["places"][0]) == {"id"}
    with pytest.raises(PlacesApiError) as err:
        await client.search_text({"pageToken": first["nextPageToken"], "textQuery": "restaurant"}, DISCOVER_MASK)
    assert err.value.status == 400
    p2 = await client.search_text({**body, "pageToken": first["nextPageToken"]}, DISCOVER_MASK)
    p3 = await client.search_text({**body, "pageToken": p2["nextPageToken"]}, DISCOVER_MASK)
    assert "nextPageToken" not in p3  # 60-result cap
    ids = [p["id"] for page in (first, p2, p3) for p in page["places"]]
    assert len(set(ids)) == 60
    with pytest.raises(PlacesApiError):
        await client.search_text({**body, "locationRestriction": {"circle": {}}}, DISCOVER_MASK)


async def test_warm_start_reuses_previous_leaves(world, dbpath):
    ctx, client = make_ctx(dbpath, world, "m1")
    await discover(ctx)
    ctx.conn.execute("UPDATE runs SET status='completed' WHERE run_id='m1'")
    cold_calls = client.total_calls
    leaves = {(r[0], r[1]) for r in ctx.conn.execute(
        "SELECT cell_id, type FROM cells WHERE run_id='m1' AND status IN ('leaf','saturated_leaf')")}
    ctx2, client2 = make_ctx(dbpath, world, "m2")
    await discover(ctx2)
    starts = {(r[0], r[1]) for r in ctx2.conn.execute(
        "SELECT cell_id, type FROM cells WHERE run_id='m2' AND depth=(SELECT depth FROM cells c "
        "WHERE c.run_id='m1' AND c.cell_id=cells.cell_id AND c.type=cells.type)")}
    assert leaves <= starts
    assert client2.total_calls < cold_calls
    found = {r[0] for r in ctx2.conn.execute("SELECT place_id FROM cell_places WHERE run_id='m2'")}
    assert found == wanted_ids(world, ctx.cfg.types)

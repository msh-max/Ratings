"""Regression tests for issues found in review: short pages with a token, empty bbox, mask tiers."""

from __future__ import annotations

import json

import httpx
from click.testing import CliRunner

from conftest import make_config, make_ctx, make_world, wanted_ids
from ksa_restaurants.cli import main
from ksa_restaurants.config import FIELDS_ATMOSPHERE, FIELDS_ENTERPRISE, FIELDS_PRO, Config
from ksa_restaurants.cost import FIELD_TIER, detect_sku
from ksa_restaurants.discover import discover
from ksa_restaurants.fake import FakePlacesClient
from ksa_restaurants.fetch import fetch, store_place
from ksa_restaurants.pipeline import estimate, run_all_phases
from ksa_restaurants.places_client import PlacesClient
from ksa_restaurants.validate import validate


class ShortPageClient(FakePlacesClient):
    """Serves Text Search pages of 15 results that still carry a nextPageToken, as Google may."""

    async def search_text(self, body, mask):
        return await super().search_text({**body, "pageSize": 15}, mask)


async def test_short_pages_with_token_are_followed_and_estimated(dbpath):
    world = make_world(seed=5, n=900)
    ctx, _ = make_ctx(dbpath, world)
    client = ShortPageClient(world)
    ctx.client = client
    await discover(ctx)
    found = {r[0] for r in ctx.conn.execute("SELECT DISTINCT place_id FROM cell_places")}
    assert found == wanted_ids(world, ctx.cfg.types)  # pages after a short page were followed
    est = estimate(ctx.conn, "r1", ctx.cfg, 0)
    await fetch(ctx)
    # Phase 2 repeats Phase 1's pagination exactly, so the plan's page count is exact.
    assert dict(client.paid_calls) == est.counts()
    assert validate(ctx.conn, "r1").ok


async def test_bbox_entirely_at_sea_runs_cleanly(dbpath, tmp_path):
    world = make_world(seed=3, n=200)
    ctx, client = make_ctx(dbpath, world, cfg=make_config(bbox=(22.0, 37.0, 22.5, 37.5)))
    result = await run_all_phases(ctx, cold_start=False, out_dir=tmp_path / "o", all_countries=False,
                                  summary_dir=None)
    assert result.exit_code == 0 and client.total_calls == 0
    assert result.summary["totals"]["places_total"] == 0


def test_cli_bbox_at_sea_exit_0(tmp_path):
    r = CliRunner().invoke(main, ["--fake", "--db", str(tmp_path / "s.sqlite"), "--bbox",
                                  "22.0,37.0,22.5,37.5", "run", "--out", str(tmp_path / "e"),
                                  "--summary-dir", str(tmp_path / "s")])
    assert r.exit_code == 0, r.output


def test_export_accepts_min_recall(tmp_path):
    db = str(tmp_path / "m.sqlite")
    base = ["--fake", "--db", db, "--run-id", "m1"]
    tail = ["--out", str(tmp_path / "e"), "--summary-dir", str(tmp_path / "s")]
    r = CliRunner().invoke(main, base + ["run", "--audit-cells", "5", "--min-recall", "1.01"] + tail)
    assert r.exit_code == 3, r.output  # recall 1.0 < 1.01
    r = CliRunner().invoke(main, base + ["export", "--min-recall", "0.95"] + tail)
    assert r.exit_code == 0 and "(completed" in r.output, r.output


def test_rating_without_reviews_is_soft_and_price_level_enum_is_kept(dbpath):
    ctx, _ = make_ctx(dbpath, make_world(seed=2, n=50))
    place = {"id": "ChIJzero", "rating": 4.0, "userRatingCount": 0,
             "priceLevel": "PRICE_LEVEL_MODERATE",
             "addressComponents": [{"longText": "Saudi Arabia", "shortText": "SA",
                                    "types": ["country", "political"]}]}
    store_place(ctx.conn, "r1", place, "2026-11-09T00:00:00+00:00")
    row = ctx.conn.execute("SELECT * FROM places WHERE place_id='ChIJzero'").fetchone()
    assert row["price_level"] == "PRICE_LEVEL_MODERATE" and row["user_rating_count"] == 0
    rep = validate(ctx.conn, "r1")
    assert rep.soft["rating_without_reviews"] == 1 and rep.hard["bad_user_rating_count"] == 0


def test_mask_field_tiers_match_google_docs():
    """Tiers as listed on the Text Search / Place Details pages (checked 2026-11-09)."""
    for f in FIELDS_PRO:
        assert FIELD_TIER[f] in ("ids_only", "essentials", "pro"), f
    for f in FIELDS_ENTERPRISE:
        assert FIELD_TIER[f] == "enterprise", f
    for f in FIELDS_ATMOSPHERE + ["reviews"]:
        assert FIELD_TIER[f] == "atmosphere", f
    assert detect_sku("place_details", "id,photos") == "ids_only"  # Details bills photos as IDs Only
    assert detect_sku("text_search", "places.id,places.photos") == "pro"
    cfg = Config()
    text = cfg.text_mask().split(",")
    assert text[-1] == "nextPageToken" and all(f.startswith("places.") for f in text[:-1])
    assert cfg.details_mask().split(",") == [f.removeprefix("places.") for f in text[:-1]]


async def test_http_request_shapes():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = PlacesClient(api_key="k-secret", transport=httpx.MockTransport(handler))
    await client.search_text({"textQuery": "restaurant"}, "places.id,nextPageToken")
    await client.place_details("ChIJabc", "id,rating", "ar", "SA")
    await client.aclose()
    assert seen[0].method == "POST" and str(seen[0].url) == "https://places.googleapis.com/v1/places:searchText"
    assert seen[0].headers["X-Goog-Api-Key"] == "k-secret"
    assert seen[0].headers["X-Goog-FieldMask"] == "places.id,nextPageToken"
    assert json.loads(seen[0].content) == {"textQuery": "restaurant"}
    assert seen[1].method == "GET"
    assert str(seen[1].url) == "https://places.googleapis.com/v1/places/ChIJabc?languageCode=ar&regionCode=SA"
    assert seen[1].headers["X-Goog-FieldMask"] == "id,rating"

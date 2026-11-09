"""Riyadh-only, zero-cost mode (SPEC_FREE section I): caps, instruments, rounds, staleness, exports."""

from __future__ import annotations

import gzip
import json
import math
import sqlite3
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from conftest import (INSTRUMENT_SKUS, assert_within_caps, caps, counters, make_config, make_ctx,
                      make_world, wanted_ids)
from ksa_restaurants import db
from ksa_restaurants.cli import main
from ksa_restaurants.config import AREAS, Config
from ksa_restaurants.cost import FreeCapExhausted, usable_cap
from ksa_restaurants.discover import cell_from_row, discover
from ksa_restaurants.fake import FakeInjectedFailure, FakePlacesClient
from ksa_restaurants.fetch import fetch
from ksa_restaurants.fetch_plan import staleness_weights
from ksa_restaurants.geo import circumscribed_radius_m, haversine_m, saudi_bbox
from ksa_restaurants.pipeline import FreeEstimate, estimate, run_all_phases
from ksa_restaurants.runs import load_run_config


async def _run(ctx, tmp_path, name="out", **kw):
    return await run_all_phases(ctx, cold_start=False, out_dir=tmp_path / name, all_countries=False,
                                summary_dir=None, **kw)


def _months_ago(n: int) -> str:
    now = datetime.now(timezone.utc)
    y, m = divmod(now.year * 12 + now.month - 1 - n, 12)
    return f"{y:04d}-{m + 1:02d}-15T00:00:00+00:00"


def _set_started(conn, run_id: str, months_ago: int) -> None:
    conn.execute("UPDATE runs SET started_at=? WHERE run_id=?", (_months_ago(months_ago), run_id))


def _snap_ids(conn, run_id: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT place_id FROM place_snapshots WHERE run_id=?", (run_id,))}


def _instrument_calls(client) -> dict[str, int]:
    c = counters(client)
    return {k: c.get(k, 0) for k in INSTRUMENT_SKUS}


# ----- 1. caps are never exceeded ------------------------------------------------------------

async def test_two_runs_in_one_month_share_the_caps(tmp_path):
    """Month-to-date accounting: run 2 in the same month only gets what run 1 left."""
    world = make_world(seed=21, n=1500)
    cfg = make_config(free_caps=caps(20, nearby_search__pro=30))  # usable 18 / 27
    ctx1, c1 = make_ctx(tmp_path / "m.sqlite", world, "r1", cfg)
    r1 = await _run(ctx1, tmp_path)
    assert r1.exit_code == 0
    for key in INSTRUMENT_SKUS:  # the caps bind in run 1 ...
        assert counters(c1).get(key, 0) >= usable_cap(cfg, key) - 2, key
    ctx2, c2 = make_ctx(tmp_path / "m.sqlite", world, "r2", cfg)
    r2 = await _run(ctx2, tmp_path, "out2")
    assert r2.exit_code == 0
    assert_within_caps(cfg, c1, c2)  # ... so without MTD accounting run 2 would exceed them
    assert sum(_instrument_calls(c2).values()) <= 2 * len(INSTRUMENT_SKUS)
    cov = r2.summary["coverage"]
    assert cov["unfetched"] > 0 and cov["carried_forward"] > 0
    fm = r2.summary["free_mode"]
    assert fm["within_free_caps"] and all(fm["mtd_usage"][k] <= fm["usable"][k] for k in INSTRUMENT_SKUS)
    assert fm["mtd_usage"]["text_search/atmosphere"] == counters(c1, c2)["text_search/atmosphere"]


async def test_resume_mid_round_t_never_double_spends(tmp_path):
    world = make_world(seed=22, n=1500)
    cfg = make_config(free_caps=caps(30))
    ref_ctx, ref = make_ctx(tmp_path / "ref.sqlite", world, "r1", cfg)
    await discover(ref_ctx)
    free_calls = ref.total_calls
    ctx1, c1 = make_ctx(tmp_path / "db.sqlite", world, "r1", cfg, fail_after=free_calls + 7)
    with pytest.raises(FakeInjectedFailure):
        await _run(ctx1, tmp_path)
    done = ctx1.conn.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='text' AND status='done'").fetchone()[0]
    assert done > 0 and _instrument_calls(c1)["text_search/atmosphere"] == 7
    ctx2, c2 = make_ctx(tmp_path / "db.sqlite", world, "r1", cfg)
    result = await _run(ctx2, tmp_path)
    assert result.exit_code == 0
    assert_within_caps(cfg, c1, c2)
    assert counters(c2).get("text_search/ids_only", 0) == 0  # discovery not repeated
    db_calls = {f"{r[0]}/{r[1]}": r[2] for r in ctx2.conn.execute(
        "SELECT endpoint, sku, SUM(calls) FROM api_calls GROUP BY endpoint, sku")}
    assert db_calls == counters(c1, c2)  # every served call is accounted for exactly once


async def test_guard_blocks_stale_plan_after_another_run_spent_the_caps(tmp_path):
    """Run A persists its Round T plan, crashes; run B (same month) spends what A had planned;
    resumed A must be stopped by the runtime guard (jobs end ``skipped_cap``), not by the plan."""
    world = make_world(seed=23, n=1500)
    cfg = make_config(free_caps=caps(25), audit_cells=0)
    path = tmp_path / "g.sqlite"
    ctx_a, ca = make_ctx(path, world, "A", cfg)
    await discover(ctx_a)
    est = estimate(ctx_a.conn, "A", cfg, 0)
    assert isinstance(est, FreeEstimate)
    planned = sum(r[0] for r in ctx_a.conn.execute(
        "SELECT pages FROM fetch_jobs WHERE run_id='A' AND kind='text'"))
    assert planned >= usable_cap(cfg, "text_search/atmosphere")  # A planned the whole cap
    ctx_b, cb = make_ctx(path, world, "B", cfg)
    await _run(ctx_b, tmp_path, "b")
    assert counters(cb)["text_search/atmosphere"] > 0  # B could spend: A had not called yet
    ctx_a2, ca2 = make_ctx(path, world, "A", cfg)
    result = await _run(ctx_a2, tmp_path, "a")
    assert result.exit_code == 0
    skipped = ctx_a2.conn.execute("SELECT COUNT(*) FROM fetch_jobs WHERE run_id='A' AND "
                                  "status='skipped_cap'").fetchone()[0]
    assert skipped > 0
    assert_within_caps(cfg, ca, cb, ca2)


async def test_guard_raises_before_anything_is_sent(tmp_path):
    world = make_world(seed=24, n=300)
    cfg = make_config(free_caps=caps(0))
    ctx, client = make_ctx(tmp_path / "z.sqlite", world, "r1", cfg)
    with pytest.raises(FreeCapExhausted) as err:
        await ctx.place_details("fetch", next(iter(world.places)), cfg.details_mask("atmosphere"))
    assert err.value.sku_key == "place_details/atmosphere" and client.total_calls == 0
    await discover(ctx)  # IDs-only discovery is never capped
    assert client.paid_calls == {} and client.total_calls > 0


# ----- 2. instrument order and tier ----------------------------------------------------------

async def test_tier_auto_fills_atmosphere_first(tmp_path):
    world = make_world(seed=25, n=1500)
    cfg = make_config(free_caps=caps(60, text_search__atmosphere=6, nearby_search__atmosphere=4,
                                     place_details__atmosphere=4), audit_cells=0)
    ctx, client = make_ctx(tmp_path / "a.sqlite", world, "r1", cfg)
    await _run(ctx, tmp_path)
    c = _instrument_calls(client)
    for ep in ("text_search", "nearby_search", "place_details"):
        atm, ent = c[f"{ep}/atmosphere"], c[f"{ep}/enterprise"]
        if ent:
            slack = 2 if ep == "text_search" else 0  # a Text Search job's pages use one instrument
            assert atm >= usable_cap(cfg, f"{ep}/atmosphere") - slack, ep
    assert c["text_search/enterprise"] > 0 and c["place_details/enterprise"] > 0
    first_text = [m for e, _, m in client.requests if e == "text_search" and "rating" in m][0]
    assert "places.dineIn" in first_text  # job order: atmosphere instrument first
    rows = ctx.conn.execute("SELECT fetched_via, atmosphere_json FROM places").fetchall()
    assert {r[0] for r in rows} >= {"text_search/atmosphere", "text_search/enterprise"}
    for via, atm_json in rows:
        assert (atm_json is not None) == via.endswith("/atmosphere")
    assert {r[0] for r in ctx.conn.execute("SELECT DISTINCT sku FROM place_snapshots")} <= set(INSTRUMENT_SKUS)


async def test_tier_enterprise_never_touches_atmosphere(tmp_path):
    world = make_world(seed=26, n=1200)
    cfg = make_config(tier="enterprise", free_caps=caps(15), include_reviews=True)
    ctx, client = make_ctx(tmp_path / "e.sqlite", world, "r1", cfg)
    await _run(ctx, tmp_path)
    assert not any(sku == "atmosphere" for _, sku in client.calls)
    assert all(c > 0 for k, c in _instrument_calls(client).items() if k.endswith("enterprise"))
    assert ctx.conn.execute("SELECT COUNT(*) FROM places WHERE atmosphere_json IS NOT NULL").fetchone()[0] == 0
    assert all("reviews" not in m for _, _, m in client.requests)


# ----- 3. Round N, Round D, unfetched ---------------------------------------------------------

async def test_fake_nearby_honours_circle_types_distance_and_cap():
    world = make_world(seed=27, n=1500)
    client = FakePlacesClient(world)
    centre = (24.5, 46.5)
    body = {"includedTypes": ["meal_takeaway"], "maxResultCount": 20, "rankPreference": "DISTANCE",
            "locationRestriction": {"circle": {"center": {"latitude": centre[0], "longitude": centre[1]},
                                               "radius": 30_000}}}
    resp = await client.search_nearby(body, "places.id,places.types,places.location")
    places = resp["places"]
    assert len(places) == 20
    d = [haversine_m(*centre, p["location"]["latitude"], p["location"]["longitude"]) for p in places]
    assert d == sorted(d) and max(d) <= 30_000
    assert all("meal_takeaway" in p["types"] for p in places)


async def test_round_n_then_details_then_unfetched(tmp_path):
    world = make_world(seed=28, n=1500)
    cfg = make_config(free_caps=caps(10, text_search__atmosphere=0, text_search__enterprise=0))
    ctx, client = make_ctx(tmp_path / "n.sqlite", world, "r1", cfg)
    result = await _run(ctx, tmp_path)
    assert result.exit_code == 0  # unfetched is a soft warning in free mode
    nearby = [(b, m) for e, b, m in client.requests if e == "nearby_search" and "rating" in m]
    assert len(nearby) == 2 * usable_cap(cfg, "nearby_search/atmosphere")
    rects = {}
    for r in ctx.conn.execute("SELECT * FROM cells WHERE status IN ('leaf','saturated_leaf')"):
        rects[r["cell_id"]] = cell_from_row(r)
    for body, _ in nearby:
        assert body["includedTypes"] == cfg.types and body["maxResultCount"] == 20
        assert body["rankPreference"] == "DISTANCE"
        circle = body["locationRestriction"]["circle"]
        centre = (circle["center"]["latitude"], circle["center"]["longitude"])
        cell = next(c for c in rects.values() if c.center == centre)  # our own cell, never a place
        assert circle["radius"] == circumscribed_radius_m(cell) <= 50_000
    via = dict(ctx.conn.execute("SELECT sku, COUNT(*) FROM place_snapshots GROUP BY sku").fetchall())
    assert via["nearby_search/atmosphere"] > 0 and via["place_details/atmosphere"] == 9
    assert via["place_details/enterprise"] == 9
    details_ids = {r[0] for r in ctx.conn.execute("SELECT place_id FROM place_snapshots WHERE sku LIKE 'place_details/%'")}
    discovered = {r[0] for r in ctx.conn.execute("SELECT place_id FROM cell_places")}
    assert details_ids <= discovered  # leftovers of Round N went to Details
    s = result.summary
    assert s["coverage"]["unfetched"] > 0 and s["validation"]["ok"]
    assert s["validation"]["soft"]["unfetched"] == s["coverage"]["unfetched"]
    assert ctx.conn.execute("SELECT unfetched FROM runs").fetchone()[0] == s["coverage"]["unfetched"]
    assert_within_caps(cfg, client)


def test_cli_tiny_caps_exit_0_with_soft_warning(tmp_path):
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps({"free_caps": {k: 5 for k in INSTRUMENT_SKUS}}))
    r = CliRunner().invoke(main, ["--fake", "--config", str(conf), "--db", str(tmp_path / "t.sqlite"),
                                  "run", "--out", str(tmp_path / "e"), "--summary-dir", str(tmp_path / "s")])
    assert r.exit_code == 0, r.output
    assert "soft: unfetched=" in r.output and "never fetched" in r.output


# ----- 4. staleness --------------------------------------------------------------------------

async def test_staleness_prefers_never_fetched_ids(tmp_path):
    world = make_world(seed=29, n=900)
    cfg = make_config(free_caps=caps(10, text_search__atmosphere=0, text_search__enterprise=0,
                                     nearby_search__atmosphere=0, nearby_search__enterprise=0),
                      audit_cells=0)
    path = tmp_path / "s.sqlite"
    ctx1, _ = make_ctx(path, world, "r1", cfg)
    await _run(ctx1, tmp_path)
    first = _snap_ids(ctx1.conn, "r1")
    assert len(first) == 18
    _set_started(ctx1.conn, "r1", 2)  # caps reset in a later month
    ctx2, _ = make_ctx(path, world, "r2", cfg)
    await _run(ctx2, tmp_path)
    second = _snap_ids(ctx2.conn, "r2")
    assert len(second) == 18 and not first & second  # never-fetched IDs (weight 3) first
    _set_started(ctx2.conn, "r2", 1)
    ctx3, _ = make_ctx(path, world, "r3", cfg)
    await discover(ctx3)
    w = staleness_weights(ctx3.conn, "r3")
    assert {w[p] for p in first} == {2} and {w[p] for p in second} == {1}
    assert {w[p] for p in set(w) - first - second} == {3}
    await fetch(ctx3)
    assert not _snap_ids(ctx3.conn, "r3") & (first | second)


# ----- 5. frontend feed carry-forward ---------------------------------------------------------

async def test_feed_carries_forward_places_not_refreshed(tmp_path):
    world = make_world(seed=30, n=900)
    path = tmp_path / "f.sqlite"
    ctx1, _ = make_ctx(path, world, "r1", make_config(audit_cells=0))
    await _run(ctx1, tmp_path, "o1")
    _set_started(ctx1.conn, "r1", 1)
    cfg2 = make_config(audit_cells=0, free_caps=caps(5, text_search__atmosphere=0, text_search__enterprise=0))
    ctx2, _ = make_ctx(path, world, "r2", cfg2)
    r2 = await _run(ctx2, tmp_path, "o2")
    assert r2.exit_code == 0
    with gzip.open(tmp_path / "o2" / "frontend_feed.json.gz", "rt", encoding="utf-8") as f:
        feed = {r["place_id"]: r for r in json.load(f)}
    saudi_discovered = {r[0] for r in ctx2.conn.execute(
        "SELECT place_id FROM places WHERE is_saudi=1 AND place_id IN (SELECT place_id FROM cell_places "
        "WHERE run_id='r2')")}
    assert saudi_discovered <= set(feed)
    fresh = _snap_ids(ctx2.conn, "r2")
    assert 0 < len(fresh) < len(saudi_discovered)
    seen_at = dict(ctx2.conn.execute("SELECT place_id, fetched_at FROM place_snapshots WHERE run_id='r1'").fetchall())
    for pid, row in feed.items():
        if pid in fresh:
            assert row["data_run_id"] == "r2"
        else:
            assert row["data_run_id"] == "r1" and row["data_as_of"] == seen_at[pid]
    cov = r2.summary["coverage"]
    assert cov["carried_forward"] == cov["discovered"] - cov["fetched_this_run"] > 0
    assert r2.summary["exports_meta"]["frontend_feed_carried_forward"] > 0


# ----- 6. discovery recall vs fetched recall ------------------------------------------------

async def test_discovery_recall_is_one_even_when_caps_leave_ids_unfetched(tmp_path):
    world = make_world(seed=31, n=1500)
    cfg = make_config(free_caps=caps(8))
    ctx, client = make_ctx(tmp_path / "r.sqlite", world, "r1", cfg)
    result = await _run(ctx, tmp_path)
    found = {r[0] for r in ctx.conn.execute("SELECT DISTINCT place_id FROM cell_places")}
    assert found == wanted_ids(world, cfg.types)  # Phase 1 complete regardless of caps
    a = result.audit
    assert a is not None and a.recall == 1.0 and a.fetched_recall is not None and a.fetched_recall < 1.0
    assert result.exit_code == 0 and result.summary["coverage"]["unfetched"] > 0
    assert_within_caps(cfg, client)


# ----- 7. areas, bbox override, --city -------------------------------------------------------

def _run_row(db_path: str, run_id: str) -> sqlite3.Row:
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    finally:
        conn.close()


def test_area_presets_and_bbox_override(tmp_path):
    d = str(tmp_path / "a.sqlite")
    base = ["--fake", "--db", d]
    r = CliRunner().invoke(main, base + ["--run-id", "ry", "discover"])
    assert r.exit_code == 0, r.output
    row = _run_row(d, "ry")
    assert json.loads(row["bbox_json"]) == list(AREAS["riyadh"]) and row["area"] == "riyadh"
    assert row["free_only"] == 1
    r = CliRunner().invoke(main, base + ["--area", "saudi", "--run-id", "sa", "discover"])
    assert r.exit_code == 0, r.output
    row = _run_row(d, "sa")
    assert json.loads(row["bbox_json"]) == list(saudi_bbox()) and row["area"] == "saudi"
    r = CliRunner().invoke(main, base + ["--bbox", "24.6,46.6,24.8,46.8", "--run-id", "bb", "discover"])
    assert r.exit_code == 0, r.output
    assert json.loads(_run_row(d, "bb")["bbox_json"]) == [24.6, 46.6, 24.8, 46.8]
    conn = db.connect(d)
    assert load_run_config(conn, "bb").area == "riyadh"
    conn.close()


def test_city_export_filter(tmp_path):
    d, out, sdir = str(tmp_path / "c.sqlite"), tmp_path / "e", str(tmp_path / "s")
    r = CliRunner().invoke(main, ["--fake", "--db", d, "run", "--audit-cells", "5", "--out", str(out),
                                  "--summary-dir", sdir, "--city", "RIYADH"])
    assert r.exit_code == 0, r.output
    conn = db.connect(d)
    n_riyadh = conn.execute("SELECT COUNT(*) FROM places WHERE is_saudi=1 AND city='Riyadh'").fetchone()[0]
    n_all = conn.execute("SELECT COUNT(*) FROM places WHERE is_saudi=1").fetchone()[0]
    conn.close()
    assert 0 < n_riyadh < n_all
    with gzip.open(out / "places_latest.jsonl.gz", "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    with gzip.open(out / "frontend_feed.json.gz", "rt", encoding="utf-8") as f:
        feed = json.load(f)
    assert len(rows) == len(feed) == n_riyadh and {x["city"] for x in rows + feed} == {"Riyadh"}
    s = json.loads((out / "summary.json").read_text("utf-8"))
    assert s["exports_meta"]["city"] == "RIYADH" and s["totals"]["places_saudi"] == n_all
    r = CliRunner().invoke(main, ["--fake", "--db", d, "export", "--out", str(tmp_path / "e2"),
                                  "--summary-dir", sdir])
    assert r.exit_code == 0, r.output
    assert json.loads((tmp_path / "e2" / "summary.json").read_text("utf-8"))["exports_meta"]["places_rows"] == n_all


# ----- 8. estimate --------------------------------------------------------------------------

def test_estimate_free_is_zero_and_paid_is_unchanged(tmp_path):
    d = str(tmp_path / "x.sqlite")
    base = ["--fake", "--db", d]
    assert CliRunner().invoke(main, base + ["--run-id", "f1", "discover"]).exit_code == 0
    r = CliRunner().invoke(main, base + ["--run-id", "f1", "--max-budget-usd", "0.01", "estimate"])
    assert r.exit_code == 0, r.output
    assert "Estimated cost: $0.00" in r.output and "would cost $" in r.output
    for label in ("I1 text_search/atmosphere", "I6 place_details/enterprise", "audit nearby_search/pro"):
        assert label in r.output
    # free mode ignores --max-budget-usd: the run proceeds within the caps
    r = CliRunner().invoke(main, base + ["--run-id", "f1", "--max-budget-usd", "0.01", "run",
                                         "--out", str(tmp_path / "e"), "--summary-dir", str(tmp_path / "s")])
    assert r.exit_code == 0, r.output
    assert "$0.00 billed" in r.output
    paid = base + ["--allow-paid", "--run-id", "p1"]
    assert CliRunner().invoke(main, paid + ["discover"]).exit_code == 0
    r = CliRunner().invoke(main, paid + ["estimate"])
    assert r.exit_code == 0, r.output
    assert "Fetch plan   :" in r.output and "Estimated cost: $" in r.output and "net of the monthly free tier" in r.output
    assert "Estimated cost: $0.00 gross" not in r.output
    r = CliRunner().invoke(main, paid + ["--max-budget-usd", "0.01", "fetch"])
    assert r.exit_code == 2 and "BUDGET ABORT" in r.output
    r = CliRunner().invoke(main, base + ["--run-id", "p1", "fetch"])  # mode is kept per run
    assert r.exit_code == 1 and "started in paid mode" in r.output


# ----- schema migration -------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT,
  config_json TEXT, bbox_json TEXT, types_json TEXT, tier TEXT, api_calls_json TEXT, est_cost_usd REAL,
  places_total INTEGER, places_saudi INTEGER, audit_recall REAL, notes TEXT);
CREATE TABLE fetch_jobs (run_id TEXT, job_id INTEGER, kind TEXT CHECK (kind IN ('text','details')),
  cell_id TEXT, type TEXT, place_id TEXT, pages INTEGER,
  status TEXT CHECK (status IN ('pending','done','error')), PRIMARY KEY (run_id, job_id));
CREATE TABLE place_snapshots (run_id TEXT, place_id TEXT, fetched_at TEXT, rating REAL,
  user_rating_count INTEGER, business_status TEXT, price_level TEXT, PRIMARY KEY (run_id, place_id));
CREATE TABLE places (place_id TEXT PRIMARY KEY, %s);
""" % ", ".join(c for c in db.PLACE_COLUMNS[1:]
                if c not in ("fetched_via", "last_discovered_run_id", "last_discovered_at"))


def test_database_from_previous_version_still_opens(tmp_path):
    path = tmp_path / "old.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    old_cfg = json.loads(Config().model_dump_json())
    for k in ("area", "free_only", "free_caps", "free_cap_margin"):
        old_cfg.pop(k)
    old_cfg["tier"] = "atmosphere"
    raw.execute("INSERT INTO runs(run_id, started_at, status, config_json, bbox_json, types_json, tier) "
                "VALUES ('old', '2026-10-02T01:00:00+00:00', 'completed', ?, '[]', '[]', 'atmosphere')",
                (json.dumps(old_cfg),))
    raw.execute("INSERT INTO fetch_jobs VALUES ('old', 1, 'text', 'c', 't', NULL, 2, 'done')")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(places)")}
    assert {"fetched_via", "last_discovered_run_id", "last_discovered_at", "rating"} <= cols
    assert "sku" in {r[1] for r in conn.execute("PRAGMA table_info(place_snapshots)")}
    row = conn.execute("SELECT area, free_only FROM runs WHERE run_id='old'").fetchone()
    assert (row[0], row[1]) == ("saudi", 0)
    cfg = load_run_config(conn, "old")
    assert cfg.area == "saudi" and not cfg.free_only and cfg.tier == "atmosphere"
    conn.execute("INSERT INTO fetch_jobs(run_id, job_id, kind, status, instrument) "
                 "VALUES ('new', 1, 'nearby', 'skipped_cap', 'nearby_search/atmosphere')")
    assert conn.execute("SELECT COUNT(*) FROM fetch_jobs").fetchone()[0] == 2
    conn.close()
    db.connect(path).close()  # idempotent


def test_usable_cap_is_floor_of_cap_times_margin():
    cfg = Config(free_cap_margin=0.85)
    assert usable_cap(cfg, "text_search/atmosphere") == math.floor(1000 * 0.85) == 850
    assert usable_cap(cfg, "nearby_search/pro") == 4250
    assert usable_cap(cfg, "text_search/ids_only") is None
    assert usable_cap(Config(free_caps={"text_search/ids_only": 0}), "text_search/ids_only") is None
    assert usable_cap(Config(free_caps={}), "place_details/atmosphere") == 0  # unknown: never spend

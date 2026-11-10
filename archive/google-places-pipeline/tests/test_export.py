"""Test 7: exports - row counts, stable column order, frontend feed fields, consistent summary."""

from __future__ import annotations

import csv
import gzip
import json

from conftest import make_ctx
from ksa_restaurants.export import EXPORT_COLUMNS, FEED_FIELDS, SNAPSHOT_COLUMNS, export
from ksa_restaurants.pipeline import run_all_phases


def _csv(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


async def test_exports(world, dbpath, tmp_path):
    out, sdir = tmp_path / "exports", tmp_path / "summary"
    ctx, client = make_ctx(dbpath, world)
    result = await run_all_phases(ctx, cold_start=False, out_dir=out, all_countries=False, summary_dir=sdir)
    conn = ctx.conn
    saudi = conn.execute("SELECT COUNT(*) FROM places WHERE is_saudi=1").fetchone()[0]
    foreign = conn.execute("SELECT COUNT(*) FROM places WHERE is_saudi=0").fetchone()[0]
    assert foreign > 0

    rows = _csv(out / "places_latest.csv.gz")
    assert rows[0] == EXPORT_COLUMNS and "raw_json" not in rows[0]
    assert EXPORT_COLUMNS[:3] == ["place_id", "name", "display_name"] and EXPORT_COLUMNS[-1] == "last_seen_at"
    assert len(rows) - 1 == saudi
    idx = EXPORT_COLUMNS.index("country_code")
    assert {r[idx] for r in rows[1:]} <= {"SA", ""}
    assert len({r[0] for r in rows[1:]}) == saudi
    json.loads(rows[1][EXPORT_COLUMNS.index("types_json")])  # JSON kept as a string in CSV

    with gzip.open(out / "places_latest.jsonl.gz", "rt", encoding="utf-8") as f:
        objs = [json.loads(line) for line in f]
    assert len(objs) == saudi and list(objs[0]) == EXPORT_COLUMNS
    assert isinstance(objs[0]["types_json"], list)

    snaps = _csv(out / "snapshots.csv.gz")
    assert snaps[0] == SNAPSHOT_COLUMNS
    assert len(snaps) - 1 == conn.execute("SELECT COUNT(*) FROM place_snapshots").fetchone()[0]
    assert all(r[1] for r in snaps[1:])  # run started_at joined

    with gzip.open(out / "frontend_feed.json.gz", "rt", encoding="utf-8") as f:
        feed = json.load(f)
    assert len(feed) == saudi and all(list(r) == FEED_FIELDS for r in feed)
    assert all(r["name"] and r["google_maps_uri"] for r in feed)

    s = json.loads((out / "summary.json").read_text("utf-8"))
    assert s == json.loads((sdir / "latest.json").read_text("utf-8"))
    assert (sdir / "latest.md").read_text("utf-8") == (out / "report.md").read_text("utf-8")
    assert s["attribution"] == "Data © Google — Powered by Google"
    t = s["totals"]
    assert t["places_total"] == saudi + foreign and t["places_saudi"] == saudi
    assert sum(r["count"] for r in s["by_region"]) == saudi
    assert sum(s["rating_histogram"].values()) == saudi
    assert len(s["by_city_top50"]) <= 50 and len(s["top100_by_user_rating_count"]) == 100
    top = [r["user_rating_count"] for r in s["top100_by_user_rating_count"]]
    assert top == sorted(top, reverse=True)
    p = s["user_rating_count_percentiles"]
    assert p["p10"] <= p["p50"] <= p["p90"] <= p["p99"]
    assert sum(c["calls"] for c in s["api_calls"]) == client.total_calls
    assert abs(s["cost"]["gross_usd"] - sum(c["gross_usd"] for c in s["cost"]["lines"])) < 1e-9
    assert s["audit"]["recall"] == 1.0 and s["validation"]["ok"]
    assert s["discovery"]["saturated_at_min_size"] >= 1
    assert s["month_over_month"] is None and s["run"]["status"] == "completed"
    assert result.summary["totals"] == s["totals"]
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert (run["places_total"], run["places_saudi"], run["audit_recall"]) == (saudi + foreign, saudi, 1.0)

    everything = export(conn, "r1", ctx.cfg, tmp_path / "all", all_countries=True)
    assert everything["exports_meta"]["places_rows"] == saudi + foreign
    first = (out / "places_latest.csv.gz").read_bytes()
    export(conn, "r1", ctx.cfg, out)
    assert (out / "places_latest.csv.gz").read_bytes() == first  # deterministic gzip


async def test_month_over_month(world, dbpath, tmp_path):
    ctx, _ = make_ctx(dbpath, world, "m1")
    await run_all_phases(ctx, cold_start=False, out_dir=tmp_path / "a", all_countries=False, summary_dir=None)
    world.evolve(1)
    ctx2, _ = make_ctx(dbpath, world, "m2")
    r2 = await run_all_phases(ctx2, cold_start=False, out_dir=tmp_path / "b", all_countries=False,
                              summary_dir=None)
    m = r2.summary["month_over_month"]
    assert m["previous_run_id"] == "m1"
    assert m["new"] > 0 and m["missing"] > 0 and m["rating_changed"] > 0
    snaps = {run: {r[0] for r in ctx2.conn.execute("SELECT place_id FROM place_snapshots WHERE run_id=?", (run,))}
             for run in ("m1", "m2")}
    assert m["new"] == len(snaps["m2"] - snaps["m1"]) and m["missing"] == len(snaps["m1"] - snaps["m2"])
    gone = next(iter(snaps["m1"] - snaps["m2"]))
    assert ctx2.conn.execute("SELECT last_seen_run_id FROM places WHERE place_id=?", (gone,)).fetchone()[0] == "m1"
    assert not m["warning"]

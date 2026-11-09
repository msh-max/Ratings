"""Test 5: audit recall is 1.0 on a correct run and < 1 when places are removed from the DB."""

from __future__ import annotations

import json

from conftest import make_ctx
from ksa_restaurants.audit import audit, audit_stats
from ksa_restaurants.config import AUDIT_MASK
from ksa_restaurants.pipeline import EXIT_QUALITY, finalize, run_all_phases


async def _full_run(ctx, tmp_path):
    return await run_all_phases(ctx, cold_start=False, out_dir=tmp_path / "out", all_countries=False,
                                summary_dir=None)


async def test_audit_recall_one_then_below_one(world, dbpath, tmp_path):
    ctx, client = make_ctx(dbpath, world)
    result = await _full_run(ctx, tmp_path)
    a = result.audit
    assert a is not None and a.cells == ctx.cfg.audit_cells
    assert a.in_scope > 50 and a.recall == 1.0 and result.exit_code == 0
    assert a.out_of_scope_share is not None and a.out_of_scope_share > 0  # cafes/bakeries found
    nearby = [(b, m) for e, b, m in client.requests if e == "nearby_search"]
    assert len(nearby) == a.cells
    for body, mask in nearby:
        assert mask == AUDIT_MASK and body["rankPreference"] == "DISTANCE" and body["maxResultCount"] == 20
        assert len(body["includedTypes"]) <= 50 and set(ctx.cfg.types) <= set(body["includedTypes"])
        assert 0 < body["locationRestriction"]["circle"]["radius"] <= 50_000

    # compared against the whole run: in-scope places found around a cell but discovered elsewhere
    cross = ctx.conn.execute(
        "SELECT COUNT(*) FROM audit_found f WHERE f.country_code='SA' AND NOT EXISTS (SELECT 1 FROM "
        "cell_places cp WHERE cp.cell_id=f.cell_id AND cp.place_id=f.place_id)").fetchone()[0]
    assert cross > 0

    in_scope = [r[0] for r in ctx.conn.execute("SELECT DISTINCT place_id, types_json FROM audit_found "
                                               "WHERE country_code='SA'")
                if set(json.loads(r[1])) & set(ctx.cfg.types)]
    removed = in_scope[: len(in_scope) // 4]
    ctx.conn.executemany("DELETE FROM place_snapshots WHERE place_id=?", [(p,) for p in removed])
    ctx.conn.executemany("DELETE FROM places WHERE place_id=?", [(p,) for p in removed])
    calls_before = client.total_calls
    again = await audit(ctx, ctx.cfg.audit_cells)
    assert client.total_calls == calls_before  # audited cells are not re-queried
    assert again.recall is not None and again.recall < 1.0
    assert abs(again.recall - (len(in_scope) - len(removed)) / len(in_scope)) < 1e-9
    assert audit_stats(ctx.conn, "r1", ctx.cfg).recall == again.recall
    per_cell = ctx.conn.execute("SELECT MIN(recall) FROM audit_cells").fetchone()[0]
    assert per_cell < 1.0
    _, code = finalize(ctx, tmp_path / "out2", False, None)
    assert code == EXIT_QUALITY

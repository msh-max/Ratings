"""Test 3: a crashed run resumes with the same run_id, repeats no finished work, no duplicates."""

from __future__ import annotations

import pytest

from conftest import make_ctx, make_world
from ksa_restaurants.config import DISCOVER_MASK
from ksa_restaurants.fake import FakeInjectedFailure
from ksa_restaurants.pipeline import run_all_phases


async def _run(ctx, tmp_path):
    return await run_all_phases(ctx, cold_start=False, out_dir=tmp_path / "out", all_countries=False,
                                summary_dir=None)


def _state(conn):
    return {
        "leaves": {tuple(r) for r in conn.execute(
            "SELECT cell_id, type, status, result_count FROM cells WHERE status<>'split'")},
        "snapshots": sorted(r[0] for r in conn.execute("SELECT place_id FROM place_snapshots")),
        "jobs": {tuple(r) for r in conn.execute("SELECT job_id, kind, status FROM fetch_jobs")},
    }


@pytest.mark.parametrize("phase", ["discover", "fetch"])
async def test_resume_after_injected_failure(tmp_path, phase):
    world = make_world(seed=11, n=1200)
    ref_ctx, ref_client = make_ctx(tmp_path / "ref.sqlite", world)
    await _run(ref_ctx, tmp_path)
    ref_discover = ref_client.calls[("text_search", "ids_only")]
    fail_after = ref_discover // 2 if phase == "discover" else ref_discover + 25

    ctx1, client1 = make_ctx(tmp_path / "db.sqlite", world, fail_after=fail_after)
    with pytest.raises(FakeInjectedFailure):
        await _run(ctx1, tmp_path)
    finished = {(r["type"], r["lat_lo"], r["lng_lo"], r["lat_hi"], r["lng_hi"]) for r in ctx1.conn.execute(
        "SELECT * FROM cells WHERE status<>'pending'")}
    done_jobs = {(r["cell_id"], r["type"]) for r in ctx1.conn.execute(
        "SELECT * FROM fetch_jobs WHERE status='done' AND kind='text'")}
    assert finished
    if phase == "fetch":
        assert done_jobs

    ctx2, client2 = make_ctx(tmp_path / "db.sqlite", world)  # same run_id "r1"
    result = await _run(ctx2, tmp_path)
    assert result.exit_code == 0
    for endpoint, body, mask in client2.requests:  # nothing finished is asked again
        if endpoint == "text_search" and "pageToken" not in body:
            rect = body["locationRestriction"]["rectangle"]
            key = (body["includedType"], rect["low"]["latitude"], rect["low"]["longitude"],
                   rect["high"]["latitude"], rect["high"]["longitude"])
            if mask == DISCOVER_MASK:
                assert key not in finished
    fetched_cells = [(r["cell_id"], r["type"]) for r in ctx2.conn.execute(
        "SELECT c.cell_id, c.type FROM cells c JOIN fetch_jobs j ON j.cell_id=c.cell_id AND j.type=c.type")]
    assert len(fetched_cells) == len(set(fetched_cells))
    assert client2.total_calls < ref_client.total_calls
    assert client1.total_calls + client2.total_calls <= ref_client.total_calls + 2  # <= 1 cell's pages
    assert _state(ctx2.conn) == _state(ref_ctx.conn)
    snaps = ctx2.conn.execute("SELECT COUNT(*), COUNT(DISTINCT place_id) FROM place_snapshots").fetchone()
    assert snaps[0] == snaps[1]

    ctx3, client3 = make_ctx(tmp_path / "db.sqlite", world)  # completed run: resume is a no-op
    await _run(ctx3, tmp_path)
    assert client3.total_calls == 0

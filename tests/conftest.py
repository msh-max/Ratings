"""Shared helpers: a seeded synthetic world and a run context on a temporary database."""

from __future__ import annotations

from pathlib import Path

import pytest

from ksa_restaurants import db
from ksa_restaurants.config import FREE_CAPS, Config
from ksa_restaurants.context import RunContext
from ksa_restaurants.cost import usable_cap
from ksa_restaurants.fake import FakePlacesClient, FakeWorld
from ksa_restaurants.runs import open_run

INSTRUMENT_SKUS = ["text_search/atmosphere", "text_search/enterprise", "nearby_search/atmosphere",
                   "nearby_search/enterprise", "place_details/atmosphere", "place_details/enterprise"]

BBOX = (24.0, 46.0, 25.0, 47.0)  # Riyadh area, round numbers so seed edges are exact
SEED_DEG = 0.25


def make_config(**kw: object) -> Config:
    """Small, fast config over :data:`BBOX`."""
    base = {"bbox": BBOX, "seed_cell_deg": SEED_DEG, "audit_cells": 40}
    return Config(**(base | kw))


def make_world(seed: int = 7, n: int = 1500, **kw: object) -> FakeWorld:
    """World over :data:`BBOX` with the seed grid anchored at its low corner."""
    cfg = make_config()
    return FakeWorld.generate(seed, n, BBOX, cfg.types, grid_origin=(BBOX[0], BBOX[1]),
                              seed_cell_deg=SEED_DEG, min_cell_deg=cfg.min_cell_deg, **kw)


def make_ctx(path: Path, world: FakeWorld, run_id: str = "r1", cfg: Config | None = None,
             fail_after: int | None = None) -> tuple[RunContext, FakePlacesClient]:
    """Open (or resume) ``run_id`` on the DB at ``path`` with a fake client over ``world``."""
    conn = db.connect(path)
    cfg = open_run(conn, run_id, cfg or make_config())
    client = FakePlacesClient(world, fail_after_calls=fail_after)
    return RunContext(conn, run_id, cfg, client), client


def wanted_ids(world: FakeWorld, types: list[str]) -> set[str]:
    """IDs of world places carrying at least one of ``types``."""
    return {pid for pid, p in world.places.items() if set(p["types"]) & set(types)}


@pytest.fixture
def world() -> FakeWorld:
    """Default seeded world (dense cluster, food court, edge places, BH/AE, decoys)."""
    return make_world()


@pytest.fixture
def dbpath(tmp_path: Path) -> Path:
    """Temporary SQLite path."""
    return tmp_path / "t.sqlite"


def caps(n: int, **override: int) -> dict[str, int | None]:
    """Free caps with every paid-tier instrument SKU at ``n`` (keys use ``__`` for ``/``)."""
    out = dict(FREE_CAPS) | {k: n for k in INSTRUMENT_SKUS}
    return out | {k.replace("__", "/"): v for k, v in override.items()}


def counters(*clients: FakePlacesClient) -> dict[str, int]:
    """Calls served per ``endpoint/sku``, summed over clients (the fake's own counters)."""
    out: dict[str, int] = {}
    for c in clients:
        for (ep, sku), n in c.calls.items():
            out[f"{ep}/{sku}"] = out.get(f"{ep}/{sku}", 0) + n
    return out


def assert_within_caps(cfg: Config, *clients: FakePlacesClient) -> None:
    """Every capped SKU's fake counter (summed over ``clients``) is <= floor(cap x margin)."""
    for key, n in counters(*clients).items():
        cap = usable_cap(cfg, key)
        assert cap is None or n <= cap, f"{key}: {n} calls > usable {cap}"


@pytest.fixture(autouse=True)
def _no_test_exceeds_default_free_caps(monkeypatch):
    """In EVERY test, the fake clients created (summed) stay within floor(default cap x margin)."""
    created: list[FakePlacesClient] = []
    init = FakePlacesClient.__init__

    def tracking_init(self, *a, **kw):
        init(self, *a, **kw)
        created.append(self)

    monkeypatch.setattr(FakePlacesClient, "__init__", tracking_init)
    yield
    assert_within_caps(Config(), *created)

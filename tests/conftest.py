"""Shared helpers: a seeded synthetic world and a run context on a temporary database."""

from __future__ import annotations

from pathlib import Path

import pytest

from ksa_restaurants import db
from ksa_restaurants.config import Config
from ksa_restaurants.context import RunContext
from ksa_restaurants.fake import FakePlacesClient, FakeWorld
from ksa_restaurants.runs import open_run

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

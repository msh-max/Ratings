"""Test 6 (part): seed-cell pruning by the boundary polygon; geometry helpers."""

from __future__ import annotations

import math

from ksa_restaurants.geo import (Cell, cell_intersects_boundary, children, circumscribed_radius_m,
                                 haversine_m, saudi_bbox, seed_cells)


def test_seed_cell_pruning():
    red_sea = Cell("x", 20.0, 38.0, 21.0, 39.0)
    riyadh = Cell("x", 24.5, 46.5, 25.0, 47.0)
    yemen_border = Cell("x", 16.8, 44.0, 17.8, 45.0)  # straddles the Saudi-Yemen border
    assert not cell_intersects_boundary(red_sea)
    assert cell_intersects_boundary(riyadh)
    assert cell_intersects_boundary(yemen_border)


def test_country_seed_grid():
    bbox = saudi_bbox()
    assert 16.0 < bbox[0] < 16.5 and 55.5 < bbox[3] < 56.0
    all_cells = seed_cells(bbox, 1.0, prune=False)
    kept = seed_cells(bbox, 1.0)
    assert 0 < len(kept) < len(all_cells)
    assert math.isclose(sum(c.area_deg2 for c in all_cells),
                        (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), rel_tol=1e-9)
    assert len({c.cell_id for c in all_cells}) == len(all_cells)
    assert all(len(c.cell_id) == 7 for c in all_cells)  # fixed-width "rrr_ccc"


def test_children_tile_parent():
    parent = Cell("000_000", 24.1, 46.3, 24.35, 46.55, 0)
    kids = children(parent)
    assert [k.cell_id for k in kids] == ["000_0000", "000_0001", "000_0002", "000_0003"]
    assert math.isclose(sum(k.area_deg2 for k in kids), parent.area_deg2, rel_tol=1e-12)
    assert kids[0].lat_hi == kids[2].lat_lo and kids[0].lng_hi == kids[1].lng_lo


def test_distances():
    assert math.isclose(haversine_m(24.0, 46.0, 25.0, 46.0), 111_195, rel_tol=1e-3)
    small = Cell("x", 24.0, 46.0, 24.01, 46.01)
    r = circumscribed_radius_m(small)
    assert 700 < r < 800
    assert circumscribed_radius_m(Cell("x", 20, 40, 22, 42)) == 50_000.0

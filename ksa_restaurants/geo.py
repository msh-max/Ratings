"""Geography on OUR OWN cells only: boundary, seed grid, quadtree children, distances.

Google Maps Platform Terms forbid point-in-polygon or other geometric derivation on Places
coordinates, so nothing in this module (or anywhere in the package) is ever applied to a place's
latitude/longitude. The boundary polygon is used solely to prune seed cells.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry

BBox = tuple[float, float, float, float]  # lat_lo, lng_lo, lat_hi, lng_hi
EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True)
class Cell:
    """An axis-aligned lat/lng rectangle in the quadtree."""

    cell_id: str
    lat_lo: float
    lng_lo: float
    lat_hi: float
    lng_hi: float
    depth: int = 0
    parent_id: str | None = None

    @property
    def center(self) -> tuple[float, float]:
        """Centre (lat, lng) of the rectangle."""
        return (self.lat_lo + self.lat_hi) / 2, (self.lng_lo + self.lng_hi) / 2

    @property
    def size_deg(self) -> float:
        """Largest side of the rectangle in degrees."""
        return max(self.lat_hi - self.lat_lo, self.lng_hi - self.lng_lo)

    @property
    def area_deg2(self) -> float:
        """Area in square degrees (used to check tiling)."""
        return (self.lat_hi - self.lat_lo) * (self.lng_hi - self.lng_lo)

    def rectangle(self) -> dict[str, dict[str, float]]:
        """The Places API ``rectangle`` object for ``locationRestriction``."""
        return {"low": {"latitude": self.lat_lo, "longitude": self.lng_lo},
                "high": {"latitude": self.lat_hi, "longitude": self.lng_hi}}


def children(cell: Cell) -> list[Cell]:
    """Split a cell into 4 children at its midpoint; they tile the parent exactly.

    Digits: 0 = south-west, 1 = south-east, 2 = north-west, 3 = north-east. Children share the
    exact same float edges, so there are no gaps or overlaps.
    """
    lat_mid, lng_mid = cell.center
    spans = [((cell.lat_lo, lat_mid), (cell.lng_lo, lng_mid)), ((cell.lat_lo, lat_mid), (lng_mid, cell.lng_hi)),
             ((lat_mid, cell.lat_hi), (cell.lng_lo, lng_mid)), ((lat_mid, cell.lat_hi), (lng_mid, cell.lng_hi))]
    return [Cell(f"{cell.cell_id}{i}", la[0], ln[0], la[1], ln[1], cell.depth + 1, cell.cell_id)
            for i, (la, ln) in enumerate(spans)]


def can_split(cell: Cell, min_cell_deg: float) -> bool:
    """A cell may be split only while it is larger than ``min_cell_deg``."""
    return cell.size_deg > min_cell_deg


@lru_cache(maxsize=1)
def saudi_boundary() -> BaseGeometry:
    """Simplified Saudi Arabia polygon (Natural Earth, public domain) shipped with the package."""
    text = resources.files("ksa_restaurants").joinpath("data/saudi_boundary.geojson").read_text("utf-8")
    return shape(json.loads(text)["features"][0]["geometry"])


@lru_cache(maxsize=4)
def buffered_boundary(buffer_deg: float = 0.1) -> BaseGeometry:
    """The boundary buffered by ``buffer_deg`` degrees, used only for seed-cell pruning."""
    return saudi_boundary().buffer(buffer_deg)


def saudi_bbox(pad_deg: float = 0.1) -> BBox:
    """Bounding box of the boundary polygon, padded by ``pad_deg``."""
    lng_lo, lat_lo, lng_hi, lat_hi = saudi_boundary().bounds
    return (round(lat_lo - pad_deg, 6), round(lng_lo - pad_deg, 6),
            round(lat_hi + pad_deg, 6), round(lng_hi + pad_deg, 6))


def grid_edges(lo: float, hi: float, step: float) -> list[float]:
    """Seed-grid edges ``lo + i*step`` (computed, not accumulated), the last one clipped to ``hi``."""
    n = max(1, math.ceil((hi - lo) / step - 1e-9))
    return [lo + i * step for i in range(n)] + [hi]


def cell_intersects_boundary(cell: Cell, buffer_deg: float = 0.1) -> bool:
    """True if the cell rectangle intersects the buffered Saudi boundary (cell geometry only)."""
    return buffered_boundary(buffer_deg).intersects(box(cell.lng_lo, cell.lat_lo, cell.lng_hi, cell.lat_hi))


def seed_cells(bbox: BBox, step: float, prune: bool = True, buffer_deg: float = 0.1) -> list[Cell]:
    """Regular grid of ``step``-degree cells over ``bbox`` (edge cells clipped to the bbox).

    With ``prune`` the cells not intersecting the buffered boundary (sea, neighbours) are dropped.
    Cell IDs are ``"<row>_<col>"`` zero-padded to 3 digits (e.g. ``"008_011"``) so that the seed
    part has a fixed length and child digits (0-3) appended to it can never be ambiguous.
    """
    lats, lngs = grid_edges(bbox[0], bbox[2], step), grid_edges(bbox[1], bbox[3], step)
    cells = [Cell(f"{r:03d}_{c:03d}", lats[r], lngs[c], lats[r + 1], lngs[c + 1])
             for r in range(len(lats) - 1) for c in range(len(lngs) - 1)]
    return [c for c in cells if not prune or cell_intersects_boundary(c, buffer_deg)]


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def circumscribed_radius_m(cell: Cell, cap_m: float = 50_000.0) -> float:
    """Radius of the circle centred on the cell that covers all four corners, capped at ``cap_m``."""
    clat, clng = cell.center
    corners = [(cell.lat_lo, cell.lng_lo), (cell.lat_lo, cell.lng_hi),
               (cell.lat_hi, cell.lng_lo), (cell.lat_hi, cell.lng_hi)]
    return min(cap_m, max(haversine_m(clat, clng, la, ln) for la, ln in corners))


def circle_inside_bbox(cell: Cell, radius_m: float, bbox: BBox) -> bool:
    """True if the cell's circumscribed circle lies inside ``bbox`` (cell geometry only)."""
    clat, clng = cell.center
    dlat = radius_m / 111_320.0
    dlng = radius_m / (111_320.0 * max(math.cos(math.radians(clat)), 1e-6))
    return (clat - dlat >= bbox[0] and clat + dlat <= bbox[2]
            and clng - dlng >= bbox[1] and clng + dlng <= bbox[3])


def parse_bbox(text: str) -> BBox:
    """Parse ``lat_lo,lng_lo,lat_hi,lng_hi``."""
    parts = [float(p) for p in text.split(",")]
    if len(parts) != 4 or parts[0] >= parts[2] or parts[1] >= parts[3]:
        raise ValueError("bbox must be lat_lo,lng_lo,lat_hi,lng_hi with lo < hi")
    return parts[0], parts[1], parts[2], parts[3]

"""SQLite storage (stdlib ``sqlite3``, WAL mode): schema and small helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PLACE_COLUMNS: list[str] = [
    "place_id", "name", "display_name", "display_name_lang", "formatted_address",
    "short_formatted_address", "address_components_json", "country_code", "region", "region_short",
    "city", "district", "postal_code", "lat", "lng", "plus_code_global", "plus_code_compound",
    "types_json", "primary_type", "primary_type_display", "business_status", "google_maps_uri",
    "website_uri", "phone_national", "phone_international", "rating", "user_rating_count",
    "price_level", "price_range_json", "opening_hours_json", "weekday_descriptions_json",
    "utc_offset_minutes", "photo_count", "first_photo_name", "pure_service_area_business",
    "accessibility_options_json", "atmosphere_json", "editorial_summary", "review_summary",
    "is_saudi", "first_seen_run_id", "last_seen_run_id", "first_seen_at", "last_seen_at", "raw_json",
]

_PLACE_TYPES = {"rating": "REAL", "lat": "REAL", "lng": "REAL", "user_rating_count": "INTEGER",
                "utc_offset_minutes": "INTEGER", "photo_count": "INTEGER",
                "pure_service_area_business": "INTEGER", "is_saudi": "INTEGER"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, config_json TEXT,
  bbox_json TEXT, types_json TEXT, tier TEXT, api_calls_json TEXT, est_cost_usd REAL,
  places_total INTEGER, places_saudi INTEGER, audit_recall REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS cells (
  run_id TEXT, cell_id TEXT, type TEXT, lat_lo REAL, lng_lo REAL, lat_hi REAL, lng_hi REAL,
  depth INTEGER, parent_id TEXT,
  status TEXT CHECK (status IN ('pending','leaf','split','saturated_leaf')),
  result_count INTEGER, fetched INTEGER DEFAULT 0, discover_calls INTEGER DEFAULT 0,
  fetch_calls INTEGER DEFAULT 0, PRIMARY KEY (run_id, cell_id, type));
CREATE TABLE IF NOT EXISTS cell_places (
  run_id TEXT, cell_id TEXT, type TEXT, place_id TEXT,
  PRIMARY KEY (run_id, cell_id, type, place_id));
CREATE TABLE IF NOT EXISTS places (
  place_id TEXT PRIMARY KEY,
  {", ".join(f"{c} {_PLACE_TYPES.get(c, 'TEXT')}" for c in PLACE_COLUMNS[1:])});
CREATE TABLE IF NOT EXISTS fetch_jobs (
  run_id TEXT, job_id INTEGER, kind TEXT CHECK (kind IN ('text','details')), cell_id TEXT,
  type TEXT, place_id TEXT, pages INTEGER,
  status TEXT CHECK (status IN ('pending','done','error')), PRIMARY KEY (run_id, job_id));
CREATE TABLE IF NOT EXISTS fetch_errors (
  run_id TEXT, place_id TEXT, code TEXT, message TEXT, PRIMARY KEY (run_id, place_id));
CREATE TABLE IF NOT EXISTS place_snapshots (
  run_id TEXT, place_id TEXT, fetched_at TEXT, rating REAL, user_rating_count INTEGER,
  business_status TEXT, price_level TEXT, PRIMARY KEY (run_id, place_id));
CREATE TABLE IF NOT EXISTS api_calls (
  run_id TEXT, phase TEXT, endpoint TEXT, sku TEXT, calls INTEGER,
  PRIMARY KEY (run_id, phase, endpoint, sku));
CREATE TABLE IF NOT EXISTS audit_cells (
  run_id TEXT, cell_id TEXT, type TEXT, found INTEGER, in_scope INTEGER, matched INTEGER,
  out_of_scope INTEGER, recall REAL, PRIMARY KEY (run_id, cell_id, type));
CREATE TABLE IF NOT EXISTS audit_found (
  run_id TEXT, cell_id TEXT, type TEXT, place_id TEXT, country_code TEXT, types_json TEXT,
  PRIMARY KEY (run_id, cell_id, type, place_id));
CREATE INDEX IF NOT EXISTS idx_places_is_saudi ON places(is_saudi);
CREATE INDEX IF NOT EXISTS idx_places_city ON places(city);
CREATE INDEX IF NOT EXISTS idx_places_region ON places(region);
CREATE INDEX IF NOT EXISTS idx_places_rating ON places(rating);
CREATE INDEX IF NOT EXISTS idx_snapshots_place ON place_snapshots(place_id);
CREATE INDEX IF NOT EXISTS idx_cell_places_place ON cell_places(run_id, place_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the database in WAL mode with the schema applied."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT`` (``ROLLBACK`` on error)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


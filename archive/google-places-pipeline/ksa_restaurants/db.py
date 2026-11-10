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
    "is_saudi", "fetched_via", "last_discovered_run_id", "last_discovered_at",
    "first_seen_run_id", "last_seen_run_id", "first_seen_at", "last_seen_at", "raw_json",
]

_PLACE_TYPES = {"rating": "REAL", "lat": "REAL", "lng": "REAL", "user_rating_count": "INTEGER",
                "utc_offset_minutes": "INTEGER", "photo_count": "INTEGER",
                "pure_service_area_business": "INTEGER", "is_saudi": "INTEGER"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, config_json TEXT,
  bbox_json TEXT, types_json TEXT, tier TEXT, api_calls_json TEXT, est_cost_usd REAL,
  places_total INTEGER, places_saudi INTEGER, audit_recall REAL, notes TEXT, area TEXT,
  free_only INTEGER, unfetched INTEGER, coverage_json TEXT, plan_built_at TEXT,
  audit_fetched_recall REAL, last_call_at TEXT);
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
  run_id TEXT, job_id INTEGER, kind TEXT CHECK (kind IN ('text','nearby','details')), cell_id TEXT,
  type TEXT, place_id TEXT, pages INTEGER,
  status TEXT CHECK (status IN ('pending','done','error','skipped_cap')), instrument TEXT,
  PRIMARY KEY (run_id, job_id));
CREATE TABLE IF NOT EXISTS fetch_errors (
  run_id TEXT, place_id TEXT, code TEXT, message TEXT, PRIMARY KEY (run_id, place_id));
CREATE TABLE IF NOT EXISTS place_snapshots (
  run_id TEXT, place_id TEXT, fetched_at TEXT, rating REAL, user_rating_count INTEGER,
  business_status TEXT, price_level TEXT, sku TEXT, PRIMARY KEY (run_id, place_id));
CREATE TABLE IF NOT EXISTS api_calls (
  run_id TEXT, phase TEXT, endpoint TEXT, sku TEXT, calls INTEGER,
  PRIMARY KEY (run_id, phase, endpoint, sku));
CREATE TABLE IF NOT EXISTS audit_cells (
  run_id TEXT, cell_id TEXT, type TEXT, found INTEGER, in_scope INTEGER, matched INTEGER,
  out_of_scope INTEGER, recall REAL, fetched INTEGER, fetched_recall REAL,
  PRIMARY KEY (run_id, cell_id, type));
CREATE TABLE IF NOT EXISTS audit_found (
  run_id TEXT, cell_id TEXT, type TEXT, place_id TEXT, country_code TEXT, types_json TEXT,
  PRIMARY KEY (run_id, cell_id, type, place_id));
CREATE INDEX IF NOT EXISTS idx_places_is_saudi ON places(is_saudi);
CREATE INDEX IF NOT EXISTS idx_places_city ON places(city);
CREATE INDEX IF NOT EXISTS idx_places_region ON places(region);
CREATE INDEX IF NOT EXISTS idx_places_rating ON places(rating);
CREATE INDEX IF NOT EXISTS idx_snapshots_place ON place_snapshots(place_id);
CREATE INDEX IF NOT EXISTS idx_cell_places_place ON cell_places(run_id, place_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_run_place ON place_snapshots(run_id, place_id);
"""

#: Columns added after the first release; ``connect`` adds them to an older database.
ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "runs": [("area", "TEXT"), ("free_only", "INTEGER"), ("unfetched", "INTEGER"),
             ("coverage_json", "TEXT"), ("plan_built_at", "TEXT"), ("audit_fetched_recall", "REAL"),
             ("last_call_at", "TEXT")],
    "places": [("fetched_via", "TEXT"), ("last_discovered_run_id", "TEXT"),
               ("last_discovered_at", "TEXT")],
    "place_snapshots": [("sku", "TEXT")],
    "fetch_jobs": [("instrument", "TEXT")],
    "audit_cells": [("fetched", "INTEGER"), ("fetched_recall", "REAL")],
}
_FETCH_JOBS_COLUMNS = "run_id, job_id, kind, cell_id, type, place_id, pages, status, instrument"


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a database created by an earlier version up to the current schema.

    Missing columns are added with ``ALTER TABLE ... ADD COLUMN``. ``fetch_jobs`` is rebuilt once
    if its CHECK constraints predate the ``nearby`` kind / ``skipped_cap`` status (SQLite cannot
    alter a CHECK). Runs stored before free mode existed were country-wide paid runs.
    """
    with transaction(conn):
        legacy_runs = False
        for table, cols in ADDED_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    legacy_runs = legacy_runs or (table, name) == ("runs", "area")
        if legacy_runs:  # every run stored so far was a country-wide paid run
            conn.execute("UPDATE runs SET area='saudi', free_only=0 WHERE area IS NULL")
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='fetch_jobs'").fetchone()[0]
    if "'skipped_cap'" not in sql:
        new_sql = SCHEMA.split("CREATE TABLE IF NOT EXISTS fetch_jobs")[1].split(";")[0]
        with transaction(conn):
            conn.execute("ALTER TABLE fetch_jobs RENAME TO fetch_jobs_old")
            conn.execute("CREATE TABLE fetch_jobs" + new_sql)
            conn.execute(f"INSERT INTO fetch_jobs({_FETCH_JOBS_COLUMNS}) "
                         f"SELECT {_FETCH_JOBS_COLUMNS} FROM fetch_jobs_old")
            conn.execute("DROP TABLE fetch_jobs_old")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the database in WAL mode with the schema applied."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.executescript(SCHEMA)  # indexes on columns that migrate() may just have added
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


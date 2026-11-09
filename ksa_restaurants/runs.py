"""Run lifecycle: create or resume a run, find the previous run, finish a run."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .config import OPERATIONAL_FIELDS, Config
from .geo import BBox, saudi_bbox

FINISHED = ("completed", "audit_failed", "validation_failed")


def now_iso() -> str:
    """Current UTC time, ISO 8601, seconds precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_run_id() -> str:
    """``YYYY-MM-DDTHHMM`` in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")


def run_bbox(cfg: Config) -> BBox:
    """The run's bbox: ``cfg.bbox`` or the padded Saudi bounding box."""
    return tuple(cfg.bbox) if cfg.bbox else saudi_bbox(cfg.boundary_buffer_deg)  # type: ignore[return-value]


def open_run(conn: sqlite3.Connection, run_id: str, cfg: Config) -> Config:
    """Create the run, or resume it with its stored config (CLI operational settings win)."""
    row = conn.execute("SELECT config_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row:
        stored = Config.model_validate_json(row[0])
        return stored.model_copy(update={k: getattr(cfg, k) for k in OPERATIONAL_FIELDS})
    conn.execute(
        "INSERT INTO runs(run_id, started_at, status, config_json, bbox_json, types_json, tier) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, now_iso(), "running", cfg.model_dump_json(), json.dumps(list(run_bbox(cfg))),
         json.dumps(cfg.types), cfg.tier))
    return cfg


def load_run_config(conn: sqlite3.Connection, run_id: str) -> Config | None:
    """Stored config of a run, or None."""
    row = conn.execute("SELECT config_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return Config.model_validate_json(row[0]) if row else None


def latest_run_id(conn: sqlite3.Connection) -> str | None:
    """Most recently started run."""
    row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def previous_run_id(conn: sqlite3.Connection, run_id: str) -> str | None:
    """Latest finished run with the same bbox that started before ``run_id``."""
    row = conn.execute(
        f"SELECT p.run_id FROM runs p JOIN runs r ON r.run_id=? WHERE p.run_id<>r.run_id "
        f"AND p.bbox_json=r.bbox_json AND p.status IN {FINISHED} AND p.started_at<=r.started_at "
        f"ORDER BY p.started_at DESC, p.run_id DESC LIMIT 1", (run_id,)).fetchone()
    return row[0] if row else None


def runs_before(conn: sqlite3.Connection, run_id: str) -> int:
    """Number of runs that started before ``run_id`` (the fake world's churn epoch)."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM runs p JOIN runs r ON r.run_id=? WHERE p.run_id<>r.run_id "
        "AND (p.started_at<r.started_at OR (p.started_at=r.started_at AND p.run_id<r.run_id))",
        (run_id,)).fetchone()[0])


def set_status(conn: sqlite3.Connection, run_id: str, status: str, notes: str | None = None) -> None:
    """Update a run's status (and notes); stamps ``finished_at`` for terminal statuses."""
    finished = now_iso() if status != "running" else None
    conn.execute("UPDATE runs SET status=?, finished_at=?, notes=COALESCE(?, notes) WHERE run_id=?",
                 (status, finished, notes, run_id))

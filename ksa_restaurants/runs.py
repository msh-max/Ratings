"""Run lifecycle: create or resume a run, find the previous run, finish a run."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .config import AREAS, OPERATIONAL_FIELDS, Config, config_from_stored
from .geo import BBox, saudi_bbox

FINISHED = ("completed", "audit_failed", "validation_failed")


def now_iso() -> str:
    """Current UTC time, ISO 8601, seconds precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_run_id() -> str:
    """``YYYY-MM-DDTHHMM`` in UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")


def current_month() -> str:
    """Current UTC calendar month, ``YYYY-MM`` (the period of Google's free caps)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def run_bbox(cfg: Config) -> BBox:
    """The run's bbox: ``cfg.bbox``, else the area preset (``saudi`` = padded Saudi bbox)."""
    if cfg.bbox:
        return tuple(cfg.bbox)  # type: ignore[return-value]
    preset = AREAS[cfg.area]
    return preset if preset else saudi_bbox(cfg.boundary_buffer_deg)


def open_run(conn: sqlite3.Connection, run_id: str, cfg: Config) -> Config:
    """Create the run, or resume it with its stored config (CLI operational settings win)."""
    stored = load_run_config(conn, run_id)
    if stored is not None:
        return stored.model_copy(update={k: getattr(cfg, k) for k in OPERATIONAL_FIELDS})
    conn.execute(
        "INSERT INTO runs(run_id, started_at, status, config_json, bbox_json, types_json, tier, "
        "area, free_only) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, now_iso(), "running", cfg.model_dump_json(), json.dumps(list(run_bbox(cfg))),
         json.dumps(cfg.types), cfg.tier, cfg.area, int(cfg.free_only)))
    return cfg


def load_run_config(conn: sqlite3.Connection, run_id: str) -> Config | None:
    """Stored config of a run, or None (runs from before free mode load as paid ``saudi``)."""
    row = conn.execute("SELECT config_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return config_from_stored(row[0]) if row else None


def month_to_date_usage(conn: sqlite3.Connection, month: str | None = None,
                        include_run: str | None = None) -> dict[str, int]:
    """Calls per ``endpoint/sku`` that may have been made in ``month`` (default: the current UTC
    month), over ALL runs: smoke runs and the current run included.

    ``api_calls`` has no per-call timestamp, so a run counts in full in every month from the one
    it started in to the one of its last call (``runs.last_call_at``; ``finished_at`` for runs
    recorded before that column existed). A run that spans the month change therefore counts in
    both months - conservative, never an undercount. ``include_run`` is counted in any case.
    """
    month = month or current_month()
    return {f"{r[0]}/{r[1]}": int(r[2]) for r in conn.execute(
        "SELECT a.endpoint, a.sku, SUM(a.calls) FROM api_calls a JOIN runs r ON r.run_id=a.run_id "
        "WHERE substr(r.started_at, 1, 7)=?1 OR r.run_id=?2 OR (substr(r.started_at, 1, 7)<?1 AND "
        "substr(COALESCE(r.last_call_at, r.finished_at, ''), 1, 7)>=?1) "
        "GROUP BY a.endpoint, a.sku", (month, include_run))}


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


def usage_for_guard(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    """Month-to-date usage as the free-cap guard and planner see it.

    :func:`month_to_date_usage` of the current UTC month, always including ``run_id``'s own calls
    (a run resumed after the month changed counts all of its calls, conservatively, because some
    of them were made this month).
    """
    return month_to_date_usage(conn, include_run=run_id)

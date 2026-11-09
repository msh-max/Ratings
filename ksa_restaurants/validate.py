"""Phase 4a - Validation. Hard failures make the run fail (exit 3); soft issues are counted."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .fetch import unfetched_ids

_SEEN = "place_id IN (SELECT place_id FROM place_snapshots WHERE run_id=?)"


@dataclass
class ValidationReport:
    """Issue counts by check name."""

    hard: dict[str, int] = field(default_factory=dict)
    soft: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when no hard check failed."""
        return not any(self.hard.values())


def _count(conn: sqlite3.Connection, where: str, run_id: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM places WHERE {_SEEN} AND ({where})",
                            (run_id,)).fetchone()[0])


def validate(conn: sqlite3.Connection, run_id: str) -> ValidationReport:
    """Run all checks over the places seen in ``run_id``."""
    rep = ValidationReport()
    rep.hard["rating_out_of_range"] = _count(conn, "rating IS NOT NULL AND (rating < 1.0 OR rating > 5.0)", run_id)
    rep.hard["bad_user_rating_count"] = _count(
        conn, "user_rating_count IS NOT NULL AND (typeof(user_rating_count) <> 'integer' "
              "OR user_rating_count < 0)", run_id)
    rep.hard["empty_id"] = _count(conn, "place_id IS NULL OR place_id = ''", run_id)
    rep.hard["duplicate_snapshots"] = int(conn.execute(
        "SELECT COUNT(*) - COUNT(DISTINCT place_id) FROM place_snapshots WHERE run_id=?",
        (run_id,)).fetchone()[0])
    # Every discovered ID has a snapshot this run, or a fetch_errors row, or is counted in
    # runs.unfetched (free mode only: the caps bound and older data, if any, is carried forward).
    run = conn.execute("SELECT free_only, unfetched FROM runs WHERE run_id=?", (run_id,)).fetchone()
    free = bool(run["free_only"]) if run is not None and run["free_only"] is not None else False
    counted = (run["unfetched"] or 0) if free else 0
    missing = len(unfetched_ids(conn, run_id))
    rep.hard["discovered_without_data"] = max(0, missing - counted)
    if free:
        rep.soft["unfetched"] = min(missing, counted)
    rep.soft["rating_without_reviews"] = _count(
        conn, "rating IS NOT NULL AND (user_rating_count IS NULL OR user_rating_count < 1)", run_id)
    rep.soft["country_unknown"] = _count(conn, "country_code IS NULL", run_id)
    rep.soft["missing_display_name"] = _count(conn, "display_name IS NULL OR display_name = ''", run_id)
    rep.soft["missing_location"] = _count(conn, "lat IS NULL OR lng IS NULL", run_id)
    return rep


class ValidationFailed(RuntimeError):
    """A hard validation check failed."""

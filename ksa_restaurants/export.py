"""Phase 4b - Exports for the web UI (gzip files are written with mtime 0, so they are stable)."""

from __future__ import annotations

import csv
import gzip
import io
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import Config
from .db import PLACE_COLUMNS
from .summary import build_summary, to_markdown
from .validate import ValidationFailed

EXPORT_COLUMNS = [c for c in PLACE_COLUMNS if c != "raw_json"]
SNAPSHOT_COLUMNS = ["run_id", "run_started_at", "place_id", "fetched_at", "rating",
                    "user_rating_count", "business_status", "price_level", "sku"]
FEED_FIELDS = ["place_id", "name", "lat", "lng", "rating", "user_rating_count", "primary_type",
               "city", "region", "business_status", "price_level", "google_maps_uri",
               "data_run_id", "data_as_of"]


@contextmanager
def _gz_writer(path: Path) -> Iterator[io.TextIOWrapper]:
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz, \
            io.TextIOWrapper(gz, encoding="utf-8", newline="") as text:
        yield text


def _write_csv(path: Path, columns: list[str], rows: Iterable[Iterable[Any]]) -> int:
    n = 0
    with _gz_writer(path) as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
            n += 1
    return n


def _check_unique(ids: list[str], what: str) -> None:
    if len(ids) != len(set(ids)):
        raise ValidationFailed(f"duplicate place_id in {what}")


def export(conn: sqlite3.Connection, run_id: str, cfg: Config, out_dir: Path,
           all_countries: bool = False, summary_dir: Path | None = None,
           city: str | None = None) -> dict[str, Any]:
    """Write every export for ``run_id``; returns the summary (with export paths).

    ``city`` (case-insensitive match on ``places.city``) restricts the place exports, the
    snapshots and the frontend feed to that locality; the summary always covers the whole run.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    conds = ([] if all_countries else ["is_saudi=1"]) + (["lower(city)=lower(:city)"] if city else [])
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params = {"city": city, "run": run_id}
    rows = conn.execute(f"SELECT {', '.join(EXPORT_COLUMNS)} FROM places {where} ORDER BY place_id",
                        params).fetchall()
    _check_unique([r["place_id"] for r in rows], "places export")
    paths = {"places_csv": out_dir / "places_latest.csv.gz", "places_jsonl": out_dir / "places_latest.jsonl.gz",
             "snapshots_csv": out_dir / "snapshots.csv.gz", "frontend_feed": out_dir / "frontend_feed.json.gz",
             "summary_json": out_dir / "summary.json", "report_md": out_dir / "report.md"}
    _write_csv(paths["places_csv"], EXPORT_COLUMNS, (tuple(r) for r in rows))
    with _gz_writer(paths["places_jsonl"]) as f:
        for r in rows:
            obj = {c: (json.loads(r[c]) if c.endswith("_json") and r[c] is not None else r[c])
                   for c in EXPORT_COLUMNS}
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    snap_where = "WHERE s.place_id IN (SELECT place_id FROM places WHERE lower(city)=lower(:city))" if city else ""
    _write_csv(paths["snapshots_csv"], SNAPSHOT_COLUMNS, conn.execute(
        "SELECT s.run_id, r.started_at, s.place_id, s.fetched_at, s.rating, s.user_rating_count, "
        "s.business_status, s.price_level, s.sku FROM place_snapshots s LEFT JOIN runs r ON "
        f"r.run_id=s.run_id {snap_where} ORDER BY s.run_id, s.place_id", params))
    # The feed holds every place this run discovered or fetched. A place discovered but not
    # refreshed (free caps) carries its older data forward; data_run_id / data_as_of say how old.
    feed_where = " AND ".join(["(last_discovered_run_id=:run OR last_seen_run_id=:run)"] + conds)
    feed = [dict(r) for r in conn.execute(
        f"SELECT place_id, display_name AS name, lat, lng, rating, user_rating_count, primary_type, city, "
        f"region, business_status, price_level, google_maps_uri, last_seen_run_id AS data_run_id, "
        f"last_seen_at AS data_as_of FROM places WHERE {feed_where} ORDER BY place_id", params)]
    _check_unique([r["place_id"] for r in feed], "frontend feed")
    with _gz_writer(paths["frontend_feed"]) as f:
        json.dump(feed, f, ensure_ascii=False, separators=(",", ":"))
    summary = build_summary(conn, run_id, cfg, {k: str(v) for k, v in paths.items()})
    summary["exports_meta"] = {"places_rows": len(rows), "frontend_feed_rows": len(feed),
                               "frontend_feed_carried_forward": sum(r["data_run_id"] != run_id for r in feed),
                               "all_countries": all_countries, "city": city}
    text, md = json.dumps(summary, indent=2, ensure_ascii=False), to_markdown(summary)
    paths["summary_json"].write_text(text + "\n", encoding="utf-8")
    paths["report_md"].write_text(md, encoding="utf-8")
    if summary_dir is not None:
        summary_dir.mkdir(parents=True, exist_ok=True)
        month = (summary["run"]["started_at"] or run_id)[:7]
        for name, content in (("latest.json", text), (f"{month}.json", text), ("latest.md", md)):
            (summary_dir / name).write_text(content + ("\n" if name.endswith(".json") else ""), encoding="utf-8")
    return summary

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
                    "user_rating_count", "business_status", "price_level"]
FEED_FIELDS = ["place_id", "name", "lat", "lng", "rating", "user_rating_count", "primary_type",
               "city", "region", "business_status", "price_level", "google_maps_uri"]


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
           all_countries: bool = False, summary_dir: Path | None = None) -> dict[str, Any]:
    """Write every export for ``run_id``; returns the summary (with export paths)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    where = "" if all_countries else "WHERE is_saudi=1"
    rows = conn.execute(f"SELECT {', '.join(EXPORT_COLUMNS)} FROM places {where} ORDER BY place_id").fetchall()
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
    _write_csv(paths["snapshots_csv"], SNAPSHOT_COLUMNS, conn.execute(
        "SELECT s.run_id, r.started_at, s.place_id, s.fetched_at, s.rating, s.user_rating_count, "
        "s.business_status, s.price_level FROM place_snapshots s LEFT JOIN runs r ON r.run_id=s.run_id "
        "ORDER BY s.run_id, s.place_id"))
    feed_where = "WHERE last_seen_run_id=?" + ("" if all_countries else " AND is_saudi=1")
    feed = [dict(r) for r in conn.execute(
        f"SELECT place_id, display_name AS name, lat, lng, rating, user_rating_count, primary_type, city, "
        f"region, business_status, price_level, google_maps_uri FROM places {feed_where} ORDER BY place_id",
        (run_id,))]
    _check_unique([r["place_id"] for r in feed], "frontend feed")
    with _gz_writer(paths["frontend_feed"]) as f:
        json.dump(feed, f, ensure_ascii=False, separators=(",", ":"))
    summary = build_summary(conn, run_id, cfg, {k: str(v) for k, v in paths.items()})
    summary["exports_meta"] = {"places_rows": len(rows), "frontend_feed_rows": len(feed),
                               "all_countries": all_countries}
    text, md = json.dumps(summary, indent=2, ensure_ascii=False), to_markdown(summary)
    paths["summary_json"].write_text(text + "\n", encoding="utf-8")
    paths["report_md"].write_text(md, encoding="utf-8")
    if summary_dir is not None:
        summary_dir.mkdir(parents=True, exist_ok=True)
        month = (summary["run"]["started_at"] or run_id)[:7]
        for name, content in (("latest.json", text), (f"{month}.json", text), ("latest.md", md)):
            (summary_dir / name).write_text(content + ("\n" if name.endswith(".json") else ""), encoding="utf-8")
    return summary

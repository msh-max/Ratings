"""Phase 4c - Run summary: every number derived from the DB; text, JSON and Markdown renderings."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict
from typing import Any

from .audit import audit_stats
from .config import ATTRIBUTION, Config
from .cost import CostModel
from .discover import discover_stats
from .runs import previous_run_id
from .validate import validate

_SEEN = "p.place_id IN (SELECT place_id FROM place_snapshots WHERE run_id=?)"
PERCENTILES = (10, 25, 50, 75, 90, 99)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params)]


def _histogram(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    hist = {f"{1 + i / 2:.1f}-{1.5 + i / 2:.1f}": 0 for i in range(8)}
    hist["none"] = 0
    for (rating,) in conn.execute(f"SELECT rating FROM places p WHERE {_SEEN} AND is_saudi=1", (run_id,)):
        if rating is None:
            hist["none"] += 1
        else:
            i = min(7, max(0, int((rating - 1.0) // 0.5)))
            hist[f"{1 + i / 2:.1f}-{1.5 + i / 2:.1f}"] += 1
    return hist


def _percentiles(conn: sqlite3.Connection, run_id: str) -> dict[str, int | None]:
    vals = sorted(r[0] for r in conn.execute(
        f"SELECT user_rating_count FROM places p WHERE {_SEEN} AND is_saudi=1 "
        "AND user_rating_count IS NOT NULL", (run_id,)))
    return {f"p{p}": (vals[max(0, math.ceil(p / 100 * len(vals)) - 1)] if vals else None)
            for p in PERCENTILES}


def _deltas(conn: sqlite3.Connection, run_id: str, total: int) -> dict[str, Any] | None:
    prev = previous_run_id(conn, run_id)
    if not prev:
        return None
    q = "SELECT COUNT(*) FROM place_snapshots a WHERE a.run_id=? AND NOT EXISTS " \
        "(SELECT 1 FROM place_snapshots b WHERE b.run_id=? AND b.place_id=a.place_id)"
    new = conn.execute(q, (run_id, prev)).fetchone()[0]
    missing = conn.execute(q, (prev, run_id)).fetchone()[0]
    changed = conn.execute(
        "SELECT COUNT(*) FROM place_snapshots a JOIN place_snapshots b ON a.place_id=b.place_id "
        "WHERE a.run_id=? AND b.run_id=? AND a.rating IS NOT b.rating", (run_id, prev)).fetchone()[0]
    prev_total = conn.execute("SELECT COUNT(*) FROM place_snapshots WHERE run_id=?", (prev,)).fetchone()[0]
    change = (total - prev_total) / prev_total if prev_total else None
    return {"previous_run_id": prev, "new": new, "missing": missing, "rating_changed": changed,
            "previous_total": prev_total, "total_change_pct": None if change is None else round(100 * change, 2),
            "warning": change is not None and abs(change) > 0.15}


def build_summary(conn: sqlite3.Connection, run_id: str, cfg: Config,
                  exports: dict[str, str] | None = None) -> dict[str, Any]:
    """Compute the run summary from the DB and store the run totals in ``runs``."""
    run = dict(conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())
    total = conn.execute("SELECT COUNT(*) FROM place_snapshots WHERE run_id=?", (run_id,)).fetchone()[0]
    saudi = conn.execute(f"SELECT COUNT(*) FROM places p WHERE {_SEEN} AND is_saudi=1", (run_id,)).fetchone()[0]
    calls = _rows(conn, "SELECT phase, endpoint, sku, calls FROM api_calls WHERE run_id=? "
                  "ORDER BY phase, endpoint, sku", (run_id,))
    counts: dict[tuple[str, str], int] = {}
    for c in calls:
        counts[(c["endpoint"], c["sku"])] = counts.get((c["endpoint"], c["sku"]), 0) + c["calls"]
    model = CostModel(cfg.pricing)
    gross, net = model.totals(counts)
    conn.execute("UPDATE runs SET places_total=?, places_saudi=?, api_calls_json=?, est_cost_usd=? "
                 "WHERE run_id=?", (total, saudi, json.dumps(calls), gross, run_id))
    has_audit = conn.execute("SELECT 1 FROM audit_cells WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
    audit = asdict(audit_stats(conn, run_id, cfg)) if has_audit else None
    if audit:
        audit["min_recall"] = cfg.min_recall
    rep = validate(conn, run_id)
    by_region = _rows(conn, f"SELECT region, COUNT(*) AS count FROM places p WHERE {_SEEN} AND is_saudi=1 "
                      "GROUP BY region ORDER BY count DESC, region", (run_id,))
    by_city = _rows(conn, f"SELECT city, region, COUNT(*) AS count FROM places p WHERE {_SEEN} AND is_saudi=1 "
                    "GROUP BY city, region ORDER BY count DESC, city LIMIT 50", (run_id,))
    top = _rows(conn, f"SELECT place_id, display_name AS name, city, rating, user_rating_count FROM places p "
                f"WHERE {_SEEN} AND is_saudi=1 AND user_rating_count IS NOT NULL "
                "ORDER BY user_rating_count DESC, place_id LIMIT 100", (run_id,))
    return {
        "attribution": ATTRIBUTION,
        "run": {k: run[k] for k in ("run_id", "started_at", "finished_at", "status", "tier", "notes")}
        | {"types": json.loads(run["types_json"]), "bbox": json.loads(run["bbox_json"])},
        "totals": {"places_total": total, "places_saudi": saudi,
                   "country_unknown": rep.soft["country_unknown"],
                   "fetch_errors": conn.execute("SELECT COUNT(*) FROM fetch_errors WHERE run_id=?",
                                                (run_id,)).fetchone()[0],
                   "places_in_db": conn.execute("SELECT COUNT(*) FROM places").fetchone()[0]},
        "discovery": asdict(discover_stats(conn, run_id)),
        "by_region": by_region, "by_city_top50": by_city,
        "rating_histogram": _histogram(conn, run_id),
        "user_rating_count_percentiles": _percentiles(conn, run_id),
        "top100_by_user_rating_count": top,
        "api_calls": calls,
        "cost": {"lines": [asdict(line) for line in model.lines(counts)], "gross_usd": gross, "net_usd": net},
        "audit": audit,
        "validation": {"ok": rep.ok, "hard": rep.hard, "soft": rep.soft},
        "month_over_month": _deltas(conn, run_id, total),
        "exports": exports or {},
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def one_screen(s: dict[str, Any]) -> str:
    """Final one-screen summary printed by the CLI."""
    r, t, d = s["run"], s["totals"], s["discovery"]
    lines = [f"== ksa-restaurants run {r['run_id']} ({r['status']}, tier {r['tier']}) ==",
             f"Places found this run : {t['places_total']:,}   Saudi: {t['places_saudi']:,}   "
             f"country unknown: {t['country_unknown']:,}   fetch errors: {t['fetch_errors']:,}",
             f"Discovery             : {d['cells']:,} cells, {d['leaves']:,} leaves, "
             f"{d['saturated_at_min_size']:,} saturated at min size, {d['place_ids']:,} IDs"]
    lines.append("Calls per SKU         : " + ("; ".join(
        f"{c['endpoint']}/{c['sku']} {c['calls']:,} (${c['gross_usd']:.2f})" for c in s["cost"]["lines"]) or "none"))
    lines.append(f"Est. cost             : ${s['cost']['gross_usd']:.2f} gross / "
                 f"${s['cost']['net_usd']:.2f} net of free tier")
    a = s["audit"]
    if a is None:
        lines.append("Audit recall          : not run")
    else:
        verdict = "INCONCLUSIVE (no in-scope places found)" if a["recall"] is None else (
            "OK" if a["recall"] >= a["min_recall"] else f"BELOW {a['min_recall']:.2f}")
        rec = "n/a" if a["recall"] is None else f"{a['recall']:.3f}"
        lines.append(f"Audit recall          : {rec} {verdict} ({a['matched']}/{a['in_scope']} in scope, "
                     f"{a['cells']} cells, out-of-scope share {_pct(a['out_of_scope_share'])})")
    m = s["month_over_month"]
    if m is None:
        lines.append("vs previous run       : no previous run")
    else:
        lines.append(f"vs previous run       : {m['previous_run_id']}: +{m['new']:,} new, -{m['missing']:,} "
                     f"missing, {m['rating_changed']:,} rating changed, total {m['total_change_pct']:+.1f}%"
                     + ("  WARNING: total moved > 15%" if m["warning"] else ""))
    v = s["validation"]
    bad = {k: n for k, n in v["hard"].items() if n}
    soft = ", ".join(f"{k}={n}" for k, n in v["soft"].items() if n) or "none"
    lines.append(f"Validation            : {'OK' if v['ok'] else 'FAILED ' + str(bad)} (soft: {soft})")
    for name, path in s["exports"].items():
        lines.append(f"Export                : {path}")
    lines.append(s["attribution"])
    return "\n".join(lines)


def to_markdown(s: dict[str, Any]) -> str:
    """``report.md`` rendering of the summary."""
    out = [f"# KSA restaurants — run {s['run']['run_id']}", "", "```", one_screen(s), "```", ""]

    def table(title: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = list(rows[0])
        out.extend([f"## {title}", "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)])
        out.extend("| " + " | ".join("" if r[c] is None else str(r[c]) for c in cols) + " |" for r in rows)
        out.append("")

    table("Places by region", s["by_region"])
    table("Places by city (top 50)", s["by_city_top50"])
    table("Rating histogram", [{"bucket": k, "places": v} for k, v in s["rating_histogram"].items()])
    table("User rating count percentiles", [{"percentile": k, "value": v}
                                            for k, v in s["user_rating_count_percentiles"].items()])
    table("Calls and cost per SKU", s["cost"]["lines"])
    table("Top 100 by user rating count", s["top100_by_user_rating_count"])
    out.append(s["attribution"])
    return "\n".join(out) + "\n"

"""Phase 4c - Run summary: every number derived from the DB; text, JSON and Markdown renderings."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict
from typing import Any

from .audit import audit_stats
from .config import ATTRIBUTION, Config
from .cost import INSTRUMENTS, CostModel, free_remaining, usable_cap
from .discover import discover_stats
from .runs import current_month, month_to_date_usage, previous_run_id
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


def _present(conn: sqlite3.Connection, run_id: str) -> set[str]:
    """IDs a run saw: discovered in Phase 1 or fetched (free mode can leave some unfetched)."""
    return {r[0] for r in conn.execute(
        "SELECT place_id FROM cell_places WHERE run_id=?1 UNION SELECT place_id FROM place_snapshots "
        "WHERE run_id=?1", (run_id,))}


def _deltas(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """New / missing (by presence: discovered or fetched) and rating changes vs the previous run."""
    prev = previous_run_id(conn, run_id)
    if not prev:
        return None
    now_ids, prev_ids = _present(conn, run_id), _present(conn, prev)
    new, missing = len(now_ids - prev_ids), len(prev_ids - now_ids)
    changed = conn.execute(
        "SELECT COUNT(*) FROM place_snapshots a JOIN place_snapshots b ON a.place_id=b.place_id "
        "WHERE a.run_id=? AND b.run_id=? AND a.rating IS NOT b.rating", (run_id, prev)).fetchone()[0]
    total, prev_total = len(now_ids), len(prev_ids)
    change = (total - prev_total) / prev_total if prev_total else None
    return {"previous_run_id": prev, "new": new, "missing": missing, "rating_changed": changed,
            "previous_total": prev_total, "total_change_pct": None if change is None else round(100 * change, 2),
            "warning": change is not None and abs(change) > 0.15}


def coverage(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    """Coverage of the run's discovered IDs (also stored in ``runs.coverage_json``)."""
    disc = {r[0] for r in conn.execute("SELECT DISTINCT place_id FROM cell_places WHERE run_id=?", (run_id,))}
    snap = {r[0] for r in conn.execute("SELECT place_id FROM place_snapshots WHERE run_id=?", (run_id,))}
    errs = {r[0] for r in conn.execute("SELECT place_id FROM fetch_errors WHERE run_id=?", (run_id,))}
    older = {r[0] for r in conn.execute(
        "SELECT DISTINCT place_id FROM place_snapshots WHERE run_id<>?1 AND place_id IN "
        "(SELECT place_id FROM cell_places WHERE run_id=?1)", (run_id,))}
    rest = disc - snap - errs
    return {"discovered": len(disc), "fetched_this_run": len(disc & snap),
            "carried_forward": len(rest & older), "unfetched_new": len(rest - older),
            "unfetched": len(rest), "fetch_errors": len(disc & errs),
            "fetched_not_discovered": len(snap - disc)}


def free_mode(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """Free caps, month-to-date usage (all runs started this UTC month) and what is left."""
    mtd = month_to_date_usage(conn)
    keys = sorted(set(cfg.free_caps) | set(mtd))
    within = all(cfg.free_caps.get(k) is None or mtd.get(k, 0) <= (cfg.free_caps.get(k) or 0)
                 for k in keys if not k.endswith("/ids_only"))
    return {"enabled": cfg.free_only, "month": current_month(), "margin": cfg.free_cap_margin,
            "caps": {k: cfg.free_caps.get(k) for k in keys},
            "usable": {k: usable_cap(cfg, k) for k in keys},
            "mtd_usage": {k: mtd.get(k, 0) for k in keys},
            "remaining": free_remaining(cfg, mtd),
            "within_free_caps": within}


def instruments(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Paid-tier calls and places fetched per instrument (``endpoint/sku``) in this run."""
    calls = {f"{r[0]}/{r[1]}": r[2] for r in conn.execute(
        "SELECT endpoint, sku, SUM(calls) FROM api_calls WHERE run_id=? AND phase IN ('fetch','sweep') "
        "GROUP BY endpoint, sku", (run_id,))}
    places = {r[0]: r[1] for r in conn.execute(
        "SELECT sku, COUNT(*) FROM place_snapshots WHERE run_id=? GROUP BY sku", (run_id,))}
    names = [i.name for i in INSTRUMENTS]
    names += sorted((set(calls) | {k for k in places if k}) - set(names))
    return [{"instrument": n, "calls": calls.get(n, 0), "places": places.get(n, 0)} for n in names]


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
    cov = coverage(conn, run_id)
    conn.execute("UPDATE runs SET coverage_json=? WHERE run_id=?", (json.dumps(cov), run_id))
    by_region = _rows(conn, f"SELECT region, COUNT(*) AS count FROM places p WHERE {_SEEN} AND is_saudi=1 "
                      "GROUP BY region ORDER BY count DESC, region", (run_id,))
    by_city = _rows(conn, f"SELECT city, region, COUNT(*) AS count FROM places p WHERE {_SEEN} AND is_saudi=1 "
                    "GROUP BY city, region ORDER BY count DESC, city LIMIT 50", (run_id,))
    top = _rows(conn, f"SELECT place_id, display_name AS name, city, rating, user_rating_count FROM places p "
                f"WHERE {_SEEN} AND is_saudi=1 AND user_rating_count IS NOT NULL "
                "ORDER BY user_rating_count DESC, place_id LIMIT 100", (run_id,))
    return {
        "attribution": ATTRIBUTION,
        "area": cfg.area,
        "run": {k: run[k] for k in ("run_id", "started_at", "finished_at", "status", "tier", "notes")}
        | {"area": cfg.area, "free_only": cfg.free_only, "types": json.loads(run["types_json"]),
           "bbox": json.loads(run["bbox_json"])},
        "coverage": cov,
        "free_mode": free_mode(conn, cfg),
        "instruments": instruments(conn, run_id),
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
        "month_over_month": _deltas(conn, run_id),
        "exports": exports or {},
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def one_screen(s: dict[str, Any]) -> str:
    """Final one-screen summary printed by the CLI."""
    r, t, d = s["run"], s["totals"], s["discovery"]
    fm = s["free_mode"]
    mode = "free mode" if r["free_only"] else "PAID mode"
    lines = [f"== ksa-restaurants run {r['run_id']} ({r['status']}, area {r['area']}, {mode}, "
             f"tier {r['tier']}) ==",
             "Coverage              : " + coverage_line(s["coverage"]),
             f"Places fetched        : {t['places_total']:,}   Saudi: {t['places_saudi']:,}   "
             f"country unknown: {t['country_unknown']:,}   fetch errors: {t['fetch_errors']:,}",
             f"Discovery             : {d['cells']:,} cells, {d['leaves']:,} leaves, "
             f"{d['saturated_at_min_size']:,} saturated at min size, {d['place_ids']:,} IDs"]
    used = [i for i in s["instruments"] if i["calls"] or i["places"]]
    lines.append("Instruments           : " + ("; ".join(
        f"{_short(i['instrument'])} {i['calls']:,} calls/{i['places']:,} places" for i in used) or "none")
        + "  (T=Text Search, N=Nearby, D=Details)")
    lines.append("Calls per SKU         : " + ("; ".join(
        f"{c['endpoint']}/{c['sku']} {c['calls']:,} (${c['gross_usd']:.2f})" for c in s["cost"]["lines"]) or "none"))
    capped = [k for k in _SHOWN if fm["usable"].get(k) is not None]
    capped += [k for k, v in fm["usable"].items() if v is not None and fm["mtd_usage"][k] and k not in _SHOWN]
    lines.append(f"Free caps {fm['month']}     : " + ", ".join(
        f"{_short(k)} {fm['mtd_usage'][k]:,}/{fm['usable'][k]:,}" for k in capped)
        + f" (used/usable this month, all runs; margin {fm['margin']:.2f})")
    if r["free_only"]:
        verdict = ("$0.00 billed: every SKU is within its free monthly cap" if fm["within_free_caps"]
                   else "WARNING: a SKU is above its free monthly cap this month - check Cloud billing")
        lines.append(f"Est. cost             : {verdict} (${s['cost']['gross_usd']:.2f} at list prices)")
    else:
        lines.append(f"Est. cost             : ${s['cost']['gross_usd']:.2f} gross / "
                     f"${s['cost']['net_usd']:.2f} net of free tier")
    a = s["audit"]
    if a is None:
        lines.append("Audit recall          : not run")
    else:
        verdict = "INCONCLUSIVE (no in-scope places found)" if a["recall"] is None else (
            "OK" if a["recall"] >= a["min_recall"] else f"BELOW {a['min_recall']:.2f}")
        rec = "n/a" if a["recall"] is None else f"{a['recall']:.3f}"
        frec = "n/a" if a["fetched_recall"] is None else f"{a['fetched_recall']:.3f}"
        lines.append(f"Audit recall          : {rec} {verdict} (discovery: {a['matched']}/{a['in_scope']} "
                     f"in scope, {a['cells']} cells; fetched_recall {frec}; out-of-scope share "
                     f"{_pct(a['out_of_scope_share'])})")
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


_SHOWN = ["text_search/atmosphere", "text_search/enterprise", "nearby_search/atmosphere",
          "nearby_search/enterprise", "place_details/atmosphere", "place_details/enterprise",
          "nearby_search/pro"]
_EP = {"text_search": "T", "nearby_search": "N", "place_details": "D"}
_TIER = {"atmosphere": "atm", "enterprise": "ent", "pro": "pro", "essentials": "ess", "ids_only": "ids"}


def _short(key: str) -> str:
    """``text_search/atmosphere`` -> ``T-atm`` (one-screen summary)."""
    ep, sku = key.split("/", 1)
    return f"{_EP.get(ep, ep)}-{_TIER.get(sku, sku)}"


def coverage_line(c: dict[str, int]) -> str:
    """One-line coverage: what share of the discovered IDs has fresh data this run."""
    share = c["fetched_this_run"] / c["discovered"] if c["discovered"] else None
    return (f"{c['fetched_this_run']:,} of {c['discovered']:,} discovered IDs fetched this run "
            f"({_pct(share)}); {c['carried_forward']:,} carried forward with older data; "
            f"{c['unfetched_new']:,} never fetched; {c['fetch_errors']:,} errors")


def to_markdown(s: dict[str, Any]) -> str:
    """``report.md`` rendering of the summary."""
    out = [f"# KSA restaurants — run {s['run']['run_id']} ({s['area']})", "",
           f"**Coverage: {coverage_line(s['coverage'])}.**", "", "```", one_screen(s), "```", ""]

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
    table("Instruments (this run)", s["instruments"])
    fm = s["free_mode"]
    unl = lambda v: "unlimited" if v is None else v  # noqa: E731
    table(f"Free monthly caps, {fm['month']} (month to date, all runs)",
          [{"sku": k, "cap": unl(fm["caps"][k]), "usable": unl(fm["usable"][k]), "used": fm["mtd_usage"][k],
            "remaining": unl(fm["remaining"].get(k))} for k in fm["caps"]])
    table("Top 100 by user rating count", s["top100_by_user_rating_count"])
    out.append(s["attribution"])
    return "\n".join(out) + "\n"

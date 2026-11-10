"""Turn a month's extracts into the ranking files (spec section 4)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import CATEGORY, CITY, METHOD_VERSION
from .geocode import Geocoder
from .load import load_month
from .normalize import Aliases, cluster_mentions, load_aliases, place_attributes, slugify
from .schema import validate_output
from .scoring import Registry, _date_key, confidence, parse_tier, score_place

PACKAGE_DIR = Path(__file__).parent
AWARD_TYPES = {"guide", "ranking", "award"}


@dataclass
class BuildResult:
    output: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    geocode_requests: int = 0


def flatten(extracts: list[dict[str, Any]], registry: Registry, month: str) -> list[dict[str, Any]]:
    """One mention dict per extract entry, with its weight computed."""
    out = []
    for ex in extracts:
        recency = registry.recency(ex.get("published_at"), month)
        scheme = registry.scheme_for(ex["publisher"], ex["source_type"])
        for i, e in enumerate(ex["entries"]):
            tier = parse_tier(e.get("tier"), e.get("position"))
            base = registry.base_weight(tier, scheme)
            out.append({
                "name": e["name"].strip(), "name_ar": e.get("name_ar"),
                "cuisine": e.get("cuisine"), "area": e.get("area"),
                "price_band": e.get("price_band"), "branch": e.get("branch"),
                "source_id": ex["source_id"], "source_name": ex["source_name"],
                "publisher": ex["publisher"], "source_type": ex["source_type"],
                "tier": e.get("tier"), "position": e.get("position"),
                "published_at": ex.get("published_at"),
                "url": e.get("entry_url") or ex.get("url"),
                "note": e.get("note"),
                "_parsed": tier, "_scheme": scheme, "_recency": recency, "_idx": i,
                "weight": round(base * recency, 2),
                "_publisher_name": registry.publisher_name(ex["publisher"]),
            })
    return out


def _apply_green_star(mentions: list[dict[str, Any]], registry: Registry) -> None:
    """A Green Star listed as a separate entry of the same source still adds its bonus."""
    green_sources = {m["source_id"] for m in mentions if m["_parsed"].green}
    for m in mentions:
        if m["source_id"] in green_sources and not m["_parsed"].green:
            t = replace(m["_parsed"], green=True)
            m["weight"] = round(registry.base_weight(t, m["_scheme"]) * m["_recency"], 2)


def _clean_note(note: str) -> str:
    note = re.sub(r"\s+", " ", note).strip().strip("\"'“”")
    note = note.rstrip(" .;,")
    if not note:
        return ""
    return note[0].upper() + note[1:] + "."


def synthesise_why(counted: list[dict[str, Any]], max_notes: int = 2, max_len: int = 320) -> str:
    """Join the sources' own notes (heaviest first). Adds no claims of its own."""
    seen: set[str] = set()
    notes: list[str] = []
    for m in counted:
        if not m.get("note"):
            continue
        cleaned = _clean_note(m["note"])
        key = re.sub(r"\W+", "", cleaned.lower())
        if not cleaned or key in seen:
            continue
        seen.add(key)
        notes.append(cleaned)
        if len(notes) == max_notes:
            break
    text = " ".join(notes)
    if len(text) > max_len and notes:
        text = notes[0][:max_len].rstrip()
    if not text and counted:
        # No notes: state only which sources list it and how (facts from the mentions).
        parts = [f"{m['_publisher_name']} ({_tier_label(m)})" for m in counted[:3]]
        joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        text = f"Listed by {joined}."
    return text


def _tier_label(m: dict[str, Any]) -> str:
    t = m["tier"]
    if t:
        return str(t).strip()
    return f"#{m['position']}" if m.get("position") else "listed"


def _awards(counted: list[dict[str, Any]], registry: Registry) -> list[str]:
    out = []
    for m in counted:
        if m["source_type"] not in AWARD_TYPES:
            continue
        year = str(m["published_at"])[:4] if m.get("published_at") else None
        label = f"{registry.award_label(m['publisher'])} {_tier_label(m)}"
        out.append(f"{label} ({year})" if year else label)
    return out


def build(root: Path, month: str, *, geocoder: Geocoder | None = None,
          registry: Registry | None = None, aliases: Aliases | None = None,
          generated_at: str | None = None) -> BuildResult:
    root = Path(root)
    registry = registry or Registry.load()
    aliases = aliases if aliases is not None else load_aliases(PACKAGE_DIR / "aliases.yaml")
    loaded = load_month(root, month)
    res = BuildResult(output=None, errors=list(loaded.errors), warnings=list(loaded.warnings))
    if not loaded.extracts:
        if not res.errors:
            res.errors.append("no valid extracts")
        return res

    mentions = flatten(loaded.extracts, registry, month)
    clusters = cluster_mentions(mentions, aliases)

    places = []
    for cluster in clusters:
        _apply_green_star(cluster, registry)
        score, counted, n_pub = score_place(cluster, registry)
        # heaviest mentions first so ties in spelling/area go to the stronger source
        attrs = place_attributes(sorted(cluster, key=lambda m: -m["weight"]))
        lat = lng = None
        if geocoder is not None:
            hit = geocoder.locate(attrs["name"], attrs["name_ar"])
            if hit:
                lat, lng = hit
        latest = max((_date_key(m["published_at"]) for m in counted), default=0)
        places.append({
            "_sort": (-score, -len(counted), -latest, attrs["name"].casefold()),
            "id": None, "rank": None, "score": score, "confidence": confidence(n_pub),
            **{k: attrs[k] for k in ("name", "name_ar", "aliases", "cuisines", "area", "price_band")},
            "lat": lat, "lng": lng, "geocode": "nominatim" if lat is not None else None,
            "branches": attrs["branches"],
            "awards": _awards(counted, registry),
            "mentions": [{
                "source_id": m["source_id"], "publisher": registry.publisher_name(m["publisher"]),
                "source_type": m["source_type"], "tier": m["tier"], "position": m["position"],
                "published_at": m["published_at"], "url": m["url"], "note": m["note"],
                "weight": m["weight"],
            } for m in counted],
            "why": synthesise_why(counted),
        })

    places.sort(key=lambda p: p["_sort"])
    used: set[str] = set()
    for rank, p in enumerate(places, start=1):
        del p["_sort"]
        base = slugify(p["name"]) or f"place-{rank}"
        pid, k = base, 2
        while pid in used:
            pid, k = f"{base}-{k}", k + 1
        used.add(pid)
        p["id"], p["rank"] = pid, rank

    sources = [{
        "source_id": ex["source_id"], "source_name": ex["source_name"],
        "publisher": registry.publisher_name(ex["publisher"]), "source_type": ex["source_type"],
        "url": ex.get("url"), "published_at": ex.get("published_at"),
        "n_entries": len(ex["entries"]),
    } for ex in loaded.extracts]

    output = {
        "city": CITY, "category": CATEGORY, "month": month,
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method_version": METHOD_VERSION,
        "n_places": len(places), "n_sources": len(sources),
        "sources": sources, "places": places,
    }
    res.errors.extend(f"output: {e}" for e in validate_output(output))
    res.output = output
    if geocoder is not None:
        res.geocode_requests = geocoder.requests
    return res


def output_paths(root: Path, month: str) -> list[Path]:
    root = Path(root)
    base = root / "data" / "rankings" / CATEGORY
    return [base / f"{month}.json", base / "latest.json", root / "docs" / "data" / "restaurants.json"]


SITE_MENTION_KEYS = ("source_id", "tier", "position", "weight")


def site_copy(output: dict[str, Any]) -> dict[str, Any]:
    """Slim copy for docs/data/restaurants.json (the page joins mentions to `sources`).

    Mentions keep source_id, tier, position and weight (notes already feed `why`), plus url
    only when it is an entry's own page (differs from the source URL). Place fields that are
    null or empty are omitted. The full record stays in data/rankings/.
    """
    src_url = {s["source_id"]: s["url"] for s in output["sources"]}
    places = []
    for p in output["places"]:
        mentions = []
        for m in p["mentions"]:
            slim = {k: m[k] for k in SITE_MENTION_KEYS if m[k] not in (None, "", [])}
            if m["url"] and m["url"] != src_url.get(m["source_id"]):
                slim["url"] = m["url"]
            mentions.append(slim)
        slim_place = {k: v for k, v in p.items() if v not in (None, "", [])}
        places.append({**slim_place, "mentions": mentions})
    return {**output, "site_format": "slim-mentions", "places": places}


def write_outputs(root: Path, output: dict[str, Any], month: str) -> list[Path]:
    """Write <month>.json; also latest.json and the site copy unless a newer month exists."""
    month_file, latest, site = output_paths(root, month)
    newer = [p for p in month_file.parent.glob("????-??.json") if p.stem > month]
    pretty = json.dumps(output, ensure_ascii=False, indent=1) + "\n"
    compact = json.dumps(site_copy(output), ensure_ascii=False, separators=(",", ":")) + "\n"
    targets = [(month_file, pretty)] if newer else [(month_file, pretty), (latest, pretty), (site, compact)]
    for path, text in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return [p for p, _ in targets]

"""Turn a month's extracts into the ranking files (spec section 4)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import CATEGORY, CITY, METHOD_VERSION
from rapidfuzz import fuzz

from .geocode import Geocoder
from .load import load_month
from .exclusions import Exclusions, load_exclusions
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


# ---------------------------------------------------------------- "why" blurb

WHY_MAX_LEN = 220
# Which kinds of notes describe the place best: guide / editorial prose first, award and
# directory notes last (their facts are already shown as awards and source badges).
_TYPE_RANK = {"guide": 0, "editorial": 0, "ranking": 0, "community": 1, "official": 2, "award": 3}

_PARENS_JUNK = re.compile(r"\((?:Best|Favourite)\s*=[^)]*\)\.?", re.IGNORECASE)
_JUNK_SEGMENT = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)?\s*/\s*\d+"                                     # 12.5/20, 80/100
    r"|(?:gourmet|chef'?s|remarkable|gastronomic|traditional|excellent|exceptional|"
    r"high[- ]quality)\s+restaurant"                                 # Gault&Millau categories
    r"|budget\b|price\b.*\bsar\b"
    r"|#\d+\s+in\s+the\b|\d+\.\s+in\s+the\b"                         # "#13 in the $ group"
    r"|cited\s+(?:via|as)\b|no award cited|basis given\b|listed for popularity"
    r"|(?:won|winner|winners|nominated|nominee|highly commended|finalist|shortlisted|"
    r"shortlist|commended)\b"
    r"|michelin selected|bib gourmand in the|selected\s*\(no toque\)|selected in the"
    r"|la liste score"
    r"|listed\b(?:\s+\w+){0,4}\s+(?:in|on|by|near)\b"                  # "listed in Visit Saudi's ..."
    r"|visit saudi\b|.*\bcity guide section\b"
    r"|(?:january|february|march|april|may|june|july|august|september|october|november|"
    r"december)?\s*\d{4}\s+opening$"
    r"|new entry\b|source cites\b|appears in\b.*\bsections?\b"
    r"|chef\s+[\w.'\- ]{2,40}$"                                         # bare "chef Name"
    r")", re.IGNORECASE)


def _segments(note: str) -> list[str]:
    note = _PARENS_JUNK.sub("", note)
    note = re.sub(r"\s+", " ", note).strip().strip("\"'“”")
    return [seg.strip(" .;,") for seg in re.split(r";\s*", note) if seg.strip(" .;,")]


def clean_note(note: str | None) -> str:
    """Keep only the descriptive parts of a source note ("" when nothing is left).

    Drops scores, guide categories, budgets, award verdicts, directory boilerplate and bare
    chef names; these are shown elsewhere (awards line, source badges) or say nothing.
    """
    if not note:
        return ""
    kept = [seg for seg in _segments(note) if not _JUNK_SEGMENT.match(seg)]
    text = "; ".join(kept).strip(" .;,")
    if len(re.findall(r"[\w'’-]+", text)) < 3:      # "Fine dining", "French": too thin
        return ""
    return text[0].upper() + text[1:] + "."


def _fit(text: str, max_len: int) -> str:
    """Shorten to max_len, ending on a full sentence (or a clause closed with a period)."""
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for sep in (". ", "; ", ", "):
        i = cut.rfind(sep)
        if i >= max_len // 3:
            return cut[:i].rstrip(" .;,") + "."
    return cut.rsplit(" ", 1)[0].rstrip(" .;,") + "."


_FILLER = {"with", "from", "that", "this", "their", "known", "into", "over", "also", "very", "more"}


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) >= 4 and w not in _FILLER}


def _near_duplicate(a: str, b: str) -> bool:
    """Two notes that say mostly the same thing ("Friday brunch" twice)."""
    if fuzz.token_set_ratio(a.lower(), b.lower()) >= 80:
        return True
    wa, wb = _content_words(a), _content_words(b)
    small = min(len(wa), len(wb))
    return small > 0 and len(wa & wb) / small >= 0.5


def select_notes(counted: list[dict[str, Any]], max_notes: int = 2) -> list[str]:
    """The one or two most descriptive cleaned notes, guide/editorial prose first."""
    cands = []
    for i, m in enumerate(counted):
        text = clean_note(m.get("note"))
        if text:
            words = len(text.split())
            cands.append((_TYPE_RANK.get(m["source_type"], 2), 0 if words >= 5 else 1,
                          -m["weight"], i, text))
    cands.sort()
    chosen: list[str] = []
    for *_, text in cands:
        if any(_near_duplicate(text, c) for c in chosen):
            continue
        chosen.append(text)
        if len(chosen) == max_notes:
            break
    return chosen


def synthesise_why(counted: list[dict[str, Any]], max_notes: int = 2,
                   max_len: int = WHY_MAX_LEN) -> str:
    """One or two of the sources' own notes. Adds no claims of its own."""
    notes = select_notes(counted, max_notes)
    if notes:
        text = notes[0]
        for extra in notes[1:]:
            if len(text) + 1 + len(extra) <= max_len:
                text = f"{text} {extra}"
        return _fit(text, max_len)
    if not counted:
        return ""
    # No descriptive notes: state only which sources list it and how (facts from the mentions).
    parts, seen_pubs = [], set()
    for m in counted:                       # heaviest first: each publisher once, at its best tier
        if m["_publisher_name"] in seen_pubs:
            continue
        seen_pubs.add(m["_publisher_name"])
        parts.append(f"{m['_publisher_name']} ({_tier_label(m)})")
        if len(parts) == 3:
            break
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return _fit(f"Listed by {joined}.", max_len)


def _tier_label(m: dict[str, Any]) -> str:
    t = m["tier"]
    if t:
        return str(t).strip()
    return f"#{m['position']}" if m.get("position") else "listed"


def _awards(counted: list[dict[str, Any]], registry: Registry) -> list[str]:
    """Each award once, with its years: "World Culinary Awards nominee (2024, 2025, 2026)"."""
    years: dict[str, set[str]] = {}
    for m in counted:
        if m["source_type"] not in AWARD_TYPES:
            continue
        label = f"{registry.award_label(m['publisher'])} {_tier_label(m)}"
        ys = years.setdefault(label, set())
        if m.get("published_at"):
            ys.add(str(m["published_at"])[:4])
        elif re.search(r"(?<!\d)20\d\d(?!\d)", m["source_id"]):   # undated edition: its year
            ys.add(re.search(r"(?<!\d)20\d\d(?!\d)", m["source_id"]).group(0))
    return [f"{label} ({', '.join(sorted(ys))})" if ys else label for label, ys in years.items()]


def build(root: Path, month: str, *, geocoder: Geocoder | None = None,
          registry: Registry | None = None, aliases: Aliases | None = None,
          exclusions: Exclusions | None = None,
          generated_at: str | None = None) -> BuildResult:
    root = Path(root)
    registry = registry or Registry.load()
    aliases = aliases if aliases is not None else load_aliases(PACKAGE_DIR / "aliases.yaml")
    if exclusions is None:
        exclusions = load_exclusions(PACKAGE_DIR / "exclusions.yaml", aliases)
    loaded = load_month(root, month)
    res = BuildResult(output=None, errors=list(loaded.errors), warnings=list(loaded.warnings))
    if not loaded.extracts:
        if not res.errors:
            res.errors.append("no valid extracts")
        return res

    mentions = flatten(loaded.extracts, registry, month)
    clusters = cluster_mentions(mentions, aliases)

    places, excluded = [], []
    for cluster in clusters:
        _apply_green_star(cluster, registry)
        score, counted, n_pub = score_place(cluster, registry)
        # heaviest mentions first so ties in spelling/area go to the stronger source
        attrs = place_attributes(sorted(cluster, key=lambda m: -m["weight"]), aliases)
        reason = exclusions.reason_for([attrs["name"], attrs["name_ar"], *attrs["aliases"]], aliases)
        lat = lng = None
        if geocoder is not None and reason is None:
            hit = geocoder.locate(attrs["name"], attrs["name_ar"])
            if hit:
                lat, lng = hit
        latest = max((_date_key(m["published_at"]) for m in counted), default=0)
        (excluded if reason else places).append({
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
            **({"excluded_reason": reason} if reason else {}),
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

    # Excluded places (cafes, bakeries, ...) keep their full record, without a rank,
    # for a future cafes ranking. They never appear in `places`.
    excluded.sort(key=lambda p: p["_sort"])
    for p in excluded:
        del p["_sort"]
        base = slugify(p["name"]) or "place"
        pid, k = base, 2
        while pid in used:
            pid, k = f"{base}-{k}", k + 1
        used.add(pid)
        p["id"] = pid
        p["reason"] = p.pop("excluded_reason")
        del p["rank"]

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
        "n_excluded": len(excluded), "excluded": excluded,
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
    site = {k: v for k, v in output.items() if k not in ("excluded", "n_excluded")}
    return {**site, "site_format": "slim-mentions", "places": places}


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

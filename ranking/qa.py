"""Quality report for a month's ranking (spec section 5)."""
from __future__ import annotations

import re
from typing import Any, TextIO

from rapidfuzz import fuzz, process

from .normalize import canonical_key, has_arabic

NON_RIYADH = re.compile(
    r"\b(jeddah|jedda|khobar|al khobar|dammam|dhahran|alula|al ula|mecca|makkah|medina|madinah|"
    r"abha|taif|tabuk|yanbu|neom|qassim|buraidah|hail|jubail|dubai|abu dhabi|doha|bahrain|kuwait|"
    r"red sea|amaala)\b|جدة|الخبر|الدمام|العلا|مكة|المدينة|أبها|الطائف", re.IGNORECASE)


def duplicate_suspects(places: list[dict[str, Any]], low: int = 85, high: int = 101) -> list[tuple[float, str, str]]:
    keys = [canonical_key(p["name"]) for p in places]
    out = []
    latin = [i for i, k in enumerate(keys) if k and not has_arabic(k)]
    if len(latin) < 2:
        return out
    group = [keys[i] for i in latin]
    matrix = process.cdist(group, group, scorer=fuzz.token_set_ratio, score_cutoff=low, workers=1)
    for a in range(len(group)):
        for b in range(a + 1, len(group)):
            r = float(matrix[a][b])
            if low <= r < high:
                out.append((r, places[latin[a]]["name"], places[latin[b]]["name"]))
    return sorted(out, key=lambda t: (-t[0], t[1], t[2]))


def non_riyadh_suspects(places: list[dict[str, Any]]) -> list[tuple[str, str]]:
    out = []
    for p in places:
        fields = [p.get("area") or "", *(p.get("branches") or []), *(m.get("note") or "" for m in p["mentions"])]
        for text in fields:
            m = NON_RIYADH.search(text)
            if m:
                out.append((p["name"], m.group(0)))
                break
    return out


def report(output: dict[str, Any], errors: list[str], warnings: list[str], out: TextIO, top: int = 30) -> int:
    places = output["places"] if output else []
    w = out.write
    w(f"QA {output['month'] if output else ''}: {len(places)} places from "
      f"{output['n_sources'] if output else 0} sources\n")
    if errors:
        w(f"\nERRORS ({len(errors)}):\n" + "".join(f"  - {e}\n" for e in errors))
    if warnings:
        w(f"\nWarnings ({len(warnings)}):\n" + "".join(f"  - {e}\n" for e in warnings))
    if not output:
        return 1

    w(f"\nTop {top}:\n")
    for p in places[:top]:
        pubs = ", ".join(sorted({m["publisher"] for m in p["mentions"]}))
        w(f"  {p['rank']:>3}. {p['score']:>5.1f}  {p['name']}"
          f"{' / ' + p['name_ar'] if p['name_ar'] else ''}  [{p['confidence']}]  {pubs}\n")

    dups = duplicate_suspects(places)
    w(f"\nDuplicate suspects (token_set_ratio >= 85, not merged) — add to ranking/aliases.yaml if same place: {len(dups)}\n")
    for r, a, b in dups:
        w(f"  {r:5.1f}  {a}  <->  {b}\n")

    missing = [p for p in places if not p["area"] or not p["cuisines"]]
    w(f"\nPlaces missing area or cuisine: {len(missing)}\n")
    for p in missing:
        gaps = [k for k in ("area", "cuisines") if not p[k]]
        w(f"  {p['rank']:>3}. {p['name']}  (no {', '.join(gaps)})\n")

    far = non_riyadh_suspects(places)
    w(f"\nNon-Riyadh suspects: {len(far)}\n")
    for name, hit in far:
        w(f"  {name}  (mentions '{hit}')\n")

    sources = output["sources"]
    w(f"\nSources ({len(sources)}):\n")
    for s in sources:
        w(f"  {s['source_id']:<55} {s['source_type']:<10} {s['n_entries']:>4} entries  "
          f"{s['published_at'] or 'undated'}\n")
    geocoded = sum(1 for p in places if p["lat"] is not None)
    w(f"\nGeocoded: {geocoded}/{len(places)}\n")
    return 1 if errors else 0

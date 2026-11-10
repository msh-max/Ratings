"""Nominatim (OpenStreetMap) geocoding with a committed cache. Never uses Google.

Usage policy: https://operations.osmfoundation.org/policies/nominatim/ — identify the app with a User-Agent,
at most 1 request per second, cache results.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from rapidfuzz import fuzz

from .normalize import canonical_key

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "RiyadhBestPlaces/1.0 (+https://github.com/msh-max/Ratings)"
# lon_min, lat_max, lon_max, lat_min (Nominatim viewbox order: x1,y1,x2,y2)
VIEWBOX = (46.35, 25.10, 47.00, 24.40)
MIN_INTERVAL_S = 1.0
ACCEPT_RATIO = 80


def in_bbox(lat: float, lng: float) -> bool:
    lon_min, lat_max, lon_max, lat_min = VIEWBOX
    return lat_min <= lat <= lat_max and lon_min <= lng <= lon_max


# Only eating places count: a same-named street, district, car dealer or cinema is not the restaurant.
FOOD_TYPES = {"restaurant", "cafe", "fast_food", "food_court", "bar", "pub", "biergarten", "ice_cream"}


def is_food_place(result: dict[str, Any]) -> bool:
    """True for OSM amenity=restaurant/cafe/fast_food/...; results without tags pass (tests, old cache)."""
    category, kind = result.get("category"), result.get("type")
    if category is None and kind is None:
        return True
    return category == "amenity" and (kind is None or kind in FOOD_TYPES)


def result_name(result: dict[str, Any]) -> str:
    return (result.get("name") or (result.get("display_name") or "").split(",")[0]).strip()


# OSM name tags compared with the place name: most Riyadh POIs carry an Arabic `name` and the
# English spelling in `name:en` (fetched with namedetails=1).
NAME_TAGS = ("name", "name:en", "name:ar", "brand", "brand:en", "brand:ar", "alt_name", "alt_name:en",
             "official_name", "official_name:en", "short_name", "short_name:en", "old_name")


def result_names(result: dict[str, Any]) -> list[str]:
    names = [result_name(result), *(result.get("names") or [])]
    return [n for i, n in enumerate(names) if n and n not in names[:i]]


def accept(place_name: str, result: dict[str, Any], threshold: int = ACCEPT_RATIO) -> tuple[float, float] | None:
    """Return (lat, lng) if the candidate is an eating place (OSM amenity restaurant / cafe /
    fast_food ...), its name or one of its OSM name tags fuzzy-matches (>= 80) and it lies in
    the bbox.

    The match uses rapidfuzz token_sort_ratio on normalised names (word order and words like
    "Restaurant" ignored). token_set_ratio is deliberately not used: it scores "Marble" vs
    "Red Marble Market" at 100.
    """
    if not is_food_place(result):
        return None
    try:
        lat, lng = float(result["lat"]), float(result["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not in_bbox(lat, lng):
        return None
    a = canonical_key(place_name)
    if not a:
        return None
    for name in result_names(result):
        b = canonical_key(name)
        if b and fuzz.token_sort_ratio(a, b) >= threshold:
            return round(lat, 6), round(lng, 6)
    return None


class Geocoder:
    """Cache-first geocoder. With ``online=False`` it only reads the cache."""

    def __init__(self, cache_path: Path, online: bool = True,
                 client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cache_path = Path(cache_path)
        self.online = online
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self.requests = 0
        self.dirty = False
        self.cache: dict[str, Any] = {}
        if self.cache_path.is_file():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8")).get("entries", {})

    # ------------------------------------------------------------ network
    def _client_or_new(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20.0)
        return self._client

    def _throttle(self) -> None:
        if self._last is not None:
            wait = MIN_INTERVAL_S - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def search(self, query: str) -> list[dict[str, Any]] | None:
        """Cached Nominatim search. Returns None when unknown and offline / on error."""
        cached = self.cache.get(query)
        # entries cached before name tags were stored are refreshed once when online
        stale = cached is not None and any("names" not in r for r in cached["results"])
        if cached is not None and not (stale and self.online):
            return cached["results"]
        if not self.online:
            return None
        self._throttle()
        params = {"q": query, "format": "jsonv2", "viewbox": ",".join(map(str, VIEWBOX)),
                  "bounded": 1, "limit": 3, "namedetails": 1}
        try:
            resp = self._client_or_new().get(NOMINATIM_URL, params=params,
                                             headers={"User-Agent": USER_AGENT})
            self.requests += 1
            resp.raise_for_status()
            raw = resp.json()
        except (httpx.HTTPError, ValueError):
            return cached["results"] if cached is not None else None
        results = []
        for r in raw:
            details = r.get("namedetails") or {}
            names = [details[t] for t in NAME_TAGS if details.get(t) and details[t] != r.get("name")]
            results.append({"name": r.get("name"), "names": list(dict.fromkeys(names)),
                            "display_name": r.get("display_name"), "lat": r.get("lat"), "lon": r.get("lon"),
                            "category": r.get("category"), "type": r.get("type")})
        self.cache[query] = {"fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                             "results": results}
        self.dirty = True
        return results

    def locate(self, name: str, name_ar: str | None = None) -> tuple[float, float] | None:
        for candidate in (name, name_ar):
            if not candidate:
                continue
            results = self.search(f"{candidate} Riyadh")
            # restaurants/cafes first, then Nominatim's own order
            ranked = sorted(results or [], key=lambda r: r.get("category") != "amenity")
            for r in ranked:
                hit = accept(candidate, r)
                if hit:
                    return hit
        return None

    def save(self) -> None:
        if not self.dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "user_agent": USER_AGENT,
                   "entries": dict(sorted(self.cache.items()))}
        self.cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                                   encoding="utf-8")
        self.dirty = False

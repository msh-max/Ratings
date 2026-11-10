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


def result_name(result: dict[str, Any]) -> str:
    return (result.get("name") or (result.get("display_name") or "").split(",")[0]).strip()


def accept(place_name: str, result: dict[str, Any], threshold: int = ACCEPT_RATIO) -> tuple[float, float] | None:
    """Return (lat, lng) if the candidate's name fuzzy-matches (>= 80) and lies in the bbox."""
    try:
        lat, lng = float(result["lat"]), float(result["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not in_bbox(lat, lng):
        return None
    a, b = canonical_key(place_name), canonical_key(result_name(result))
    if not a or not b or fuzz.token_set_ratio(a, b) < threshold:
        return None
    return round(lat, 6), round(lng, 6)


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
        if query in self.cache:
            return self.cache[query]["results"]
        if not self.online:
            return None
        self._throttle()
        params = {"q": query, "format": "jsonv2", "viewbox": ",".join(map(str, VIEWBOX)),
                  "bounded": 1, "limit": 3}
        try:
            resp = self._client_or_new().get(NOMINATIM_URL, params=params,
                                             headers={"User-Agent": USER_AGENT})
            self.requests += 1
            resp.raise_for_status()
            raw = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        results = [{"name": r.get("name"), "display_name": r.get("display_name"),
                    "lat": r.get("lat"), "lon": r.get("lon"),
                    "category": r.get("category"), "type": r.get("type")} for r in raw]
        self.cache[query] = {"fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                             "results": results}
        self.dirty = True
        return results

    def locate(self, name: str, name_ar: str | None = None) -> tuple[float, float] | None:
        for candidate in (name, name_ar):
            if not candidate:
                continue
            results = self.search(f"{candidate} Riyadh")
            for r in results or []:
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

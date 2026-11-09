"""Synthetic Places API (New) for tests and ``--fake`` runs: no key, no network, no cost.

:class:`FakeWorld` generates a seeded world of places whose JSON shapes match the real API.
:class:`FakePlacesClient` serves it through the same interface as the real client: rectangle and
strict type filtering, 20-per-page pagination with opaque tokens that must be sent back with
identical parameters, the 60-result cap, Nearby Search circle filtering with DISTANCE/POPULARITY
ranking, Place Details with 404 for unknown IDs, field-mask projection and per-SKU call counters.
Filtering by coordinates here simulates Google's servers; the pipeline itself never does it.
"""

from __future__ import annotations

import base64
import bisect
import copy
import hashlib
import json
import math
import random
from collections import Counter
from typing import Any

from .cost import FIELD_TIER, detect_sku
from .geo import BBox, grid_edges, haversine_m
from .places_client import PlaceNotFound, PlacesApiError

DEFAULT_FAKE_BBOX: BBox = (24.30, 46.40, 25.05, 47.10)  # Riyadh area
DECOY_TYPES = ["cafe", "bakery", "grocery_store", "gas_station"]
_CITIES = ["Riyadh", "Diriyah", "Al Muzahmiyah", "Huraymila", "Al Kharj", "Dhurma"]
_DISTRICTS = ["Al Olaya", "Al Malaz", "As Sulimaniyah", "Al Murabba", "Hittin", "Al Yasmin",
              "Ar Rabwah", "Al Wurud", "An Nakheel", "Qurtubah"]
_ADJ = ["Golden", "Royal", "Najd", "Desert", "Green", "Al Baik", "Spicy", "Happy", "Urban", "Old"]
_NOUN = ["Grill", "Kitchen", "Shawarma", "Burger", "Mandi", "Kabsa", "Pizza", "Falafel", "Diner",
         "Bistro"]
_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_PRICE_LEVELS = ["PRICE_LEVEL_INEXPENSIVE", "PRICE_LEVEL_MODERATE", "PRICE_LEVEL_EXPENSIVE"]
_FOREIGN = {"BH": ("Bahrain", "Manama", "Capital Governorate"),
            "AE": ("United Arab Emirates", "Dubai", "Dubai")}


class FakeInjectedFailure(RuntimeError):
    """Raised by the fake client once ``fail_after_calls`` successful calls have been served."""


def _place_id(seed: int, serial: int) -> str:
    digest = hashlib.sha256(f"{seed}:{serial}".encode()).digest()
    return "ChIJ" + base64.urlsafe_b64encode(digest).decode()[:23]


def _hours() -> dict[str, Any]:
    periods = [{"open": {"day": d, "hour": 11, "minute": 0},
                "close": {"day": (d + 1) % 7, "hour": 1, "minute": 0}} for d in range(7)]
    return {"openNow": True, "periods": periods,
            "weekdayDescriptions": [f"{d}: 11:00 AM – 1:00 AM" for d in _DAYS[1:] + _DAYS[:1]]}


class FakeWorld:
    """A deterministic synthetic world of places (dicts in Places API (New) JSON shape)."""

    def __init__(self, places: list[dict[str, Any]], seed: int) -> None:
        self.seed = seed
        self.places: dict[str, dict[str, Any]] = {p["id"]: p for p in places}
        self._serial = len(places)
        self._reindex()

    # ----- generation -------------------------------------------------------------------
    @classmethod
    def generate(cls, seed: int = 42, n: int = 3000, bbox: BBox = DEFAULT_FAKE_BBOX,
                 types: list[str] | None = None, *, grid_origin: tuple[float, float] | None = None,
                 seed_cell_deg: float = 1.0, min_cell_deg: float = 0.002, food_court: bool = True,
                 dense_cluster: bool = True, n_edge: int = 12, foreign_share: float = 0.01,
                 decoy_share: float = 0.05, unknown_country: int = 3,
                 core_centers: list[tuple[float, float]] | None = None) -> FakeWorld:
        """Generate ``n`` places in ``bbox``.

        Zipf-weighted city cores, an optional very dense cluster (forces deep subdivision), an
        optional food court of 75 ``restaurant`` places inside one minimum-size cell (exercises
        ``saturated_at_min_size``; half are also ``fast_food_restaurant``, half ``meal_takeaway``
        so the other type trees still see them), ``n_edge`` places exactly on internal seed-grid
        edges (grid anchored at ``grid_origin``, default the bbox's low corner), a few BH/AE places
        near the bbox edge, a few without a country component, and decoys of other types.
        ``core_centers`` pins the first (densest) city cores, e.g. on central Riyadh.
        """
        types = types or ["restaurant", "fast_food_restaurant", "meal_takeaway", "meal_delivery"]
        rng = random.Random(seed)
        lat_lo, lng_lo, lat_hi, lng_hi = bbox
        origin = grid_origin or (lat_lo, lng_lo)
        world = cls([], seed)
        cores = [(rng.uniform(lat_lo + 0.05 * (lat_hi - lat_lo), lat_hi - 0.05 * (lat_hi - lat_lo)),
                  rng.uniform(lng_lo + 0.05 * (lng_hi - lng_lo), lng_hi - 0.05 * (lng_hi - lng_lo)),
                  0.15 * (lat_hi - lat_lo) / (k + 1)) for k in range(6)]
        for k, (la, ln) in enumerate((core_centers or [])[:len(cores)]):
            cores[k] = (la, ln, cores[k][2])
        weights = [1.0 / (k + 1) for k in range(len(cores))]
        points: list[tuple[float, float, int]] = []  # lat, lng, core index (-1 = rural)

        def inside(la: float, ln: float) -> bool:
            return lat_lo <= la <= lat_hi and lng_lo <= ln <= lng_hi

        n_special = (75 if food_court else 0) + (400 if dense_cluster else 0) + n_edge
        for _ in range(max(0, n - n_special)):
            while True:
                if rng.random() < 0.85:
                    k = rng.choices(range(len(cores)), weights)[0]
                    la, ln = rng.gauss(cores[k][0], cores[k][2]), rng.gauss(cores[k][1], cores[k][2])
                else:
                    k, la, ln = -1, rng.uniform(lat_lo, lat_hi), rng.uniform(lng_lo, lng_hi)
                if inside(la, ln):
                    points.append((la, ln, k))
                    break
        if dense_cluster:
            c = cores[0]
            points += [(c[0] + rng.uniform(-0.004, 0.004), c[1] + rng.uniform(-0.004, 0.004), 0)
                       for _ in range(400)]
        lat_edges = [e for e in grid_edges(origin[0], lat_hi, seed_cell_deg) if lat_lo < e < lat_hi]
        lng_edges = [e for e in grid_edges(origin[1], lng_hi, seed_cell_deg) if lng_lo < e < lng_hi]
        for i in range(n_edge):
            if i % 2 == 0 and lat_edges:
                points.append((lat_edges[i % len(lat_edges)], rng.uniform(lng_lo, lng_hi), -1))
            elif lng_edges:
                points.append((rng.uniform(lat_lo, lat_hi), lng_edges[i % len(lng_edges)], -1))
            elif lat_edges:
                points.append((lat_edges[0], rng.uniform(lng_lo, lng_hi), -1))
        for la, ln, k in points:
            world._add(rng, la, ln, k, types, bbox, foreign_share, decoy_share)
        if food_court:
            size = seed_cell_deg
            while size > min_cell_deg:
                size /= 2
            fc_lat = origin[0] + (math.floor((cores[1][0] - origin[0]) / size) + 0.5) * size
            fc_lng = origin[1] + (math.floor((cores[1][1] - origin[1]) / size) + 0.5) * size
            for i in range(75):
                extra = "fast_food_restaurant" if i % 2 else "meal_takeaway"
                world._add(rng, fc_lat + rng.uniform(-0.25, 0.25) * size,
                           fc_lng + rng.uniform(-0.25, 0.25) * size, 1, types, bbox, 0.0, 0.0,
                           fixed_types=["restaurant", extra])
        for p in list(world.places.values())[:unknown_country]:
            p["addressComponents"] = [c for c in p["addressComponents"]
                                      if "country" not in c["types"]]
        world._reindex()
        return world

    def _add(self, rng: random.Random, lat: float, lng: float, core: int, types: list[str],
             bbox: BBox, foreign_share: float, decoy_share: float,
             fixed_types: list[str] | None = None) -> dict[str, Any]:
        pid = _place_id(self.seed, self._serial)
        self._serial += 1
        near_edge = min(lat - bbox[0], bbox[2] - lat, lng - bbox[1], bbox[3] - lng) < 0.03
        country = "SA"
        if near_edge and rng.random() < foreign_share * 20:
            country = rng.choice(["BH", "AE"])
        if fixed_types:
            ptypes = list(fixed_types)
        elif rng.random() < decoy_share:
            ptypes = [rng.choice(DECOY_TYPES)]
        else:
            first = rng.choices(types, [1 / (k + 1) ** 1.5 for k in range(len(types))])[0]
            ptypes = list(dict.fromkeys([first] + rng.sample(types, rng.randint(0, min(2, len(types))))))
            if rng.random() < 0.1:
                ptypes.append(rng.choice(DECOY_TYPES[:2]))
        count = int(rng.paretovariate(1.2) * 3) - 3
        name = f"{rng.choice(_ADJ)} {rng.choice(_NOUN)} {self._serial}"
        if country == "SA":
            city = _CITIES[core] if 0 <= core < len(_CITIES) else rng.choice(_CITIES)
            region, region_short, country_name = "Riyadh Province", "Riyadh Province", "Saudi Arabia"
        else:
            country_name, city, region = _FOREIGN[country]
            region_short = region
        district = rng.choice(_DISTRICTS)
        comps = [
            {"longText": district, "shortText": district, "types": ["sublocality_level_1", "sublocality", "political"], "languageCode": "en"},
            {"longText": city, "shortText": city, "types": ["locality", "political"], "languageCode": "en"},
            {"longText": region, "shortText": region_short, "types": ["administrative_area_level_1", "political"], "languageCode": "en"},
            {"longText": country_name, "shortText": country, "types": ["country", "political"], "languageCode": "en"},
            {"longText": str(rng.randint(11000, 14999)), "shortText": str(rng.randint(11000, 14999)), "types": ["postal_code"], "languageCode": "en"},
        ]
        start = rng.choice([15, 25, 40, 60])
        place: dict[str, Any] = {
            "name": f"places/{pid}", "id": pid,
            "displayName": {"text": name, "languageCode": "en"},
            "_name_ar": {"text": f"مطعم {self._serial}", "languageCode": "ar"},
            "types": ptypes + ["food", "point_of_interest", "establishment"],
            "primaryType": ptypes[0],
            "primaryTypeDisplayName": {"text": ptypes[0].replace("_", " ").title(), "languageCode": "en"},
            "formattedAddress": f"{district}, {city} {comps[4]['longText']}, {country_name}",
            "shortFormattedAddress": f"{district}, {city}",
            "addressComponents": comps,
            "location": {"latitude": lat, "longitude": lng},
            "plusCode": {"globalCode": f"7HQ{self._serial:05d}+X", "compoundCode": f"{self._serial:04d}+X {city}"},
            "businessStatus": "OPERATIONAL" if rng.random() > 0.03 else "CLOSED_TEMPORARILY",
            "googleMapsUri": f"https://maps.google.com/?cid={int(hashlib.md5(pid.encode()).hexdigest()[:12], 16)}",
            "utcOffsetMinutes": 180,
            "pureServiceAreaBusiness": False,
            "accessibilityOptions": {"wheelchairAccessibleEntrance": rng.random() < 0.4},
            "photos": [{"name": f"places/{pid}/photos/p{j}", "widthPx": 4032, "heightPx": 3024,
                        "authorAttributions": [{"displayName": "A User", "uri": "https://maps.google.com/maps/contrib/1", "photoUri": "https://lh3.googleusercontent.com/a/x"}]}
                       for j in range(rng.randint(0, 3))],
            "userRatingCount": count,
            "priceLevel": rng.choice(_PRICE_LEVELS),
            "priceRange": {"startPrice": {"currencyCode": "SAR", "units": str(start)},
                           "endPrice": {"currencyCode": "SAR", "units": str(start * 2)}},
            "regularOpeningHours": _hours(),
            "nationalPhoneNumber": f"011 {rng.randint(200, 599)} {rng.randint(1000, 9999)}",
            "internationalPhoneNumber": f"+966 11 {rng.randint(200, 599)} {rng.randint(1000, 9999)}",
            "dineIn": True, "takeout": rng.random() < 0.8, "delivery": rng.random() < 0.7,
            "curbsidePickup": False, "reservable": rng.random() < 0.3, "outdoorSeating": rng.random() < 0.3,
            "servesBreakfast": rng.random() < 0.3, "servesBrunch": False, "servesLunch": True,
            "servesDinner": True, "servesCoffee": rng.random() < 0.5, "servesDessert": rng.random() < 0.5,
            "servesVegetarianFood": rng.random() < 0.4, "goodForChildren": True, "goodForGroups": True,
            "menuForChildren": rng.random() < 0.3, "restroom": True,
            "paymentOptions": {"acceptsCreditCards": True, "acceptsDebitCards": True, "acceptsCashOnly": False, "acceptsNfc": True},
            "parkingOptions": {"freeParkingLot": rng.random() < 0.5, "freeStreetParking": True},
            "editorialSummary": {"text": "Casual spot for local favourites.", "languageCode": "en"},
            "reviewSummary": {"text": {"text": "People like the food and the service.", "languageCode": "en-US"},
                              "flagContentUri": "https://www.google.com/local/review/rap/report", "disclosureText": {"text": "Summarized with Gemini", "languageCode": "en-US"}},
            "reviews": [{"name": f"places/{pid}/reviews/r1", "rating": 5, "text": {"text": "Great!", "languageCode": "en"},
                         "authorAttribution": {"displayName": "A User"}}],
        }
        if count > 0:
            place["rating"] = round(rng.uniform(2.5, 5.0), 1)
        if rng.random() < 0.6:
            place["websiteUri"] = f"https://example.com/{pid[-8:]}"
        self.places[pid] = place
        return place

    def evolve(self, epoch: int) -> None:
        """Apply ``epoch`` months of deterministic churn: ~2 % closed, ~3 % new, ~10 % re-rated."""
        for e in range(1, epoch + 1):
            rng = random.Random(self.seed * 1000 + e)
            ids = sorted(self.places)
            for pid in rng.sample(ids, len(ids) // 50):
                del self.places[pid]
            ids = sorted(self.places)
            for pid in rng.sample(ids, len(ids) // 10):
                p = self.places[pid]
                p["userRatingCount"] += rng.randint(1, 40)
                p["rating"] = round(min(5.0, max(1.0, p.get("rating", 4.0) + rng.choice([-0.1, 0.1]))), 1)
            for pid in rng.sample(ids, len(ids) * 3 // 100):
                base = self.places[pid]
                types = [t for t in base["types"] if t not in ("food", "point_of_interest", "establishment")]
                loc = base["location"]
                new = self._add(rng, loc["latitude"] + rng.uniform(-1e-4, 1e-4),
                                loc["longitude"] + rng.uniform(-1e-4, 1e-4), -1, types,
                                (-90, -180, 90, 180), 0.0, 0.0, fixed_types=types)
                new["addressComponents"] = copy.deepcopy(base["addressComponents"])
        self._reindex()

    def remove(self, place_id: str) -> None:
        """Delete a place (it then 404s in Place Details and vanishes from searches)."""
        del self.places[place_id]
        self._reindex()

    def _reindex(self) -> None:
        self._by_lat = sorted(self.places.values(), key=lambda p: p["location"]["latitude"])
        self._lats = [p["location"]["latitude"] for p in self._by_lat]

    def in_lat_range(self, lo: float, hi: float) -> list[dict[str, Any]]:
        """Places with ``lo <= latitude <= hi`` (index helper for the fake server)."""
        return self._by_lat[bisect.bisect_left(self._lats, lo):bisect.bisect_right(self._lats, hi)]


def _project(place: dict[str, Any], fields: list[str], language: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields:
        key = f.split(".")[0]
        if key == "displayName" and language == "ar":
            out[key] = place["_name_ar"]
        elif key in place:
            out[key] = place[key]
    return copy.deepcopy(out)


def _parse_mask(mask: str, prefixed: bool) -> tuple[list[str], bool]:
    fields, token = [], False
    for raw in (m.strip() for m in mask.split(",") if m.strip()):
        if raw == "nextPageToken":
            token = True
            continue
        if prefixed:
            if not raw.startswith("places."):
                raise PlacesApiError(400, "INVALID_ARGUMENT", f"invalid field mask entry {raw!r}")
            raw = raw.removeprefix("places.")
        if raw.split(".")[0] not in FIELD_TIER:
            raise PlacesApiError(400, "INVALID_ARGUMENT", f"unknown field {raw!r} in field mask")
        fields.append(raw)
    return fields, token


class FakePlacesClient:
    """In-memory stand-in for :class:`~ksa_restaurants.places_client.PlacesClient`."""

    def __init__(self, world: FakeWorld, fail_after_calls: int | None = None) -> None:
        self.world = world
        self.fail_after_calls = fail_after_calls
        self.calls: Counter[tuple[str, str]] = Counter()
        self.requests: list[tuple[str, Any, str]] = []  # (endpoint, body or id, mask)

    @property
    def total_calls(self) -> int:
        """Successful calls served so far."""
        return sum(self.calls.values())

    @property
    def paid_calls(self) -> Counter[tuple[str, str]]:
        """Successful calls per (endpoint, sku), excluding the free IDs-only SKU."""
        return Counter({k: v for k, v in self.calls.items() if k[1] != "ids_only"})

    def _bill(self, endpoint: str, mask: str) -> None:
        if self.fail_after_calls is not None and self.total_calls >= self.fail_after_calls:
            raise FakeInjectedFailure(f"injected failure after {self.fail_after_calls} calls")
        self.calls[(endpoint, detect_sku(endpoint, mask))] += 1

    async def search_text(self, body: dict[str, Any], mask: str) -> dict[str, Any]:
        """Fake ``places:searchText``."""
        fields, want_token = _parse_mask(mask, prefixed=True)
        if not body.get("textQuery"):
            raise PlacesApiError(400, "INVALID_ARGUMENT", "textQuery is required")
        restriction = body.get("locationRestriction") or {}
        if set(restriction) != {"rectangle"}:
            raise PlacesApiError(400, "INVALID_ARGUMENT", "locationRestriction must be a rectangle")
        size = int(body.get("pageSize", 20))
        if not 1 <= size <= 20:
            raise PlacesApiError(400, "INVALID_ARGUMENT", "pageSize must be 1..20")
        base = {k: v for k, v in body.items() if k != "pageToken"}
        digest = hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()[:16]
        offset = 0
        if "pageToken" in body:
            try:
                tok = json.loads(base64.urlsafe_b64decode(body["pageToken"].encode()))
            except ValueError as exc:
                raise PlacesApiError(400, "INVALID_ARGUMENT", "invalid page token") from exc
            if tok["h"] != digest:
                raise PlacesApiError(400, "INVALID_ARGUMENT",
                                     "page token does not match the original request parameters")
            offset = tok["o"]
        rect = restriction["rectangle"]
        lo, hi = rect["low"], rect["high"]
        wanted = body.get("includedType")
        hits = [p for p in self.world.in_lat_range(lo["latitude"], hi["latitude"])
                if lo["longitude"] <= p["location"]["longitude"] <= hi["longitude"]
                and (not wanted or not body.get("strictTypeFiltering") or wanted in p["types"])]
        hits.sort(key=lambda p: (-p["userRatingCount"], p["id"]))
        hits = hits[:60]
        self._bill("text_search", mask)
        self.requests.append(("text_search", copy.deepcopy(body), mask))
        page = hits[offset:offset + size]
        out: dict[str, Any] = {}
        if page:
            out["places"] = [_project(p, fields, body.get("languageCode", "en")) for p in page]
        if want_token and offset + size < len(hits):
            out["nextPageToken"] = base64.urlsafe_b64encode(
                json.dumps({"h": digest, "o": offset + size}).encode()).decode()
        return out

    async def search_nearby(self, body: dict[str, Any], mask: str) -> dict[str, Any]:
        """Fake ``places:searchNearby`` (circle, includedTypes any-match, ranking, max 20)."""
        fields, _ = _parse_mask(mask, prefixed=True)
        circle = (body.get("locationRestriction") or {}).get("circle")
        if not circle:
            raise PlacesApiError(400, "INVALID_ARGUMENT", "locationRestriction.circle is required")
        if len(body.get("includedTypes", [])) > 50:
            raise PlacesApiError(400, "INVALID_ARGUMENT", "too many includedTypes")
        clat, clng = circle["center"]["latitude"], circle["center"]["longitude"]
        radius = float(circle["radius"])
        if not 0 < radius <= 50_000:
            raise PlacesApiError(400, "INVALID_ARGUMENT", "radius must be in (0, 50000]")
        limit = int(body.get("maxResultCount", 20))
        included = set(body.get("includedTypes", []))
        dlat = radius / 111_000 + 1e-6
        hits = []
        for p in self.world.in_lat_range(clat - dlat, clat + dlat):
            d = haversine_m(clat, clng, p["location"]["latitude"], p["location"]["longitude"])
            if d <= radius and (not included or included & set(p["types"])):
                hits.append((d, p))
        if body.get("rankPreference", "POPULARITY") == "DISTANCE":
            hits.sort(key=lambda t: (t[0], t[1]["id"]))
        else:
            hits.sort(key=lambda t: (-t[1]["userRatingCount"], t[1]["id"]))
        self._bill("nearby_search", mask)
        self.requests.append(("nearby_search", copy.deepcopy(body), mask))
        lang = body.get("languageCode", "en")
        return {"places": [_project(p, fields, lang) for _, p in hits[:limit]]} if hits else {}

    async def place_details(self, place_id: str, mask: str, language: str,
                            region: str) -> dict[str, Any]:
        """Fake ``GET places/{id}`` (404 NOT_FOUND for unknown IDs)."""
        fields, _ = _parse_mask(mask, prefixed=False)
        place = self.world.places.get(place_id)
        if place is None:
            raise PlaceNotFound(404, "NOT_FOUND", f"Place ID {place_id!r} not found")
        self._bill("place_details", mask)
        self.requests.append(("place_details", place_id, mask))
        return _project(place, fields, language)

    async def aclose(self) -> None:
        """Nothing to close."""

"""Cost model: SKU detection from a field mask and tiered USD prices with free-tier netting.

Billing rule mirrored here: the highest-tier field in the mask sets the SKU for the whole call,
and every page of a paginated Text Search is one call.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from .config import Config, Pricing

TIER_ORDER = ["ids_only", "essentials", "pro", "enterprise", "atmosphere"]

ENDPOINT_SKUS: dict[str, list[str]] = {
    "text_search": ["ids_only", "pro", "enterprise", "atmosphere"],
    "nearby_search": ["pro", "enterprise", "atmosphere"],  # no free IDs-only SKU in Nearby
    "place_details": ["ids_only", "essentials", "pro", "enterprise", "atmosphere"],
}

_FIELDS_BY_TIER: dict[str, list[str]] = {
    "ids_only": ["id", "name", "attributions", "nextPageToken", "consumerAlert", "movedPlace",
                 "movedPlaceId"],
    "essentials": ["addressComponents", "addressDescriptor", "adrFormatAddress", "formattedAddress", "location",
                   "plusCode", "postalAddress", "shortFormattedAddress", "types", "viewport"],
    "pro": ["accessibilityOptions", "businessStatus", "containingPlaces", "displayName",
            "googleMapsLinks", "googleMapsTypeLabel", "googleMapsUri", "iconBackgroundColor",
            "iconMaskBaseUri", "openingDate", "photos", "primaryType", "primaryTypeDisplayName",
            "pureServiceAreaBusiness", "searchUri", "subDestinations", "timeZone",
            "utcOffsetMinutes"],
    "enterprise": ["currentOpeningHours", "currentSecondaryOpeningHours",
                   "internationalPhoneNumber", "nationalPhoneNumber", "priceLevel", "priceRange",
                   "rating", "regularOpeningHours", "regularSecondaryOpeningHours",
                   "transitStation", "userRatingCount", "websiteUri"],
    "atmosphere": ["allowsDogs", "curbsidePickup", "delivery", "dineIn", "editorialSummary",
                   "evChargeAmenitySummary", "evChargeOptions", "fuelOptions", "generativeSummary", "goodForChildren",
                   "goodForGroups", "goodForWatchingSports", "liveMusic", "menuForChildren",
                   "neighborhoodSummary", "outdoorSeating", "parkingOptions", "paymentOptions", "reservable", "restroom",
                   "reviews", "reviewSummary", "servesBeer", "servesBreakfast", "servesBrunch",
                   "servesCocktails", "servesCoffee", "servesDessert", "servesDinner",
                   "servesLunch", "servesVegetarianFood", "servesWine", "takeout"],
}
FIELD_TIER: dict[str, str] = {f: t for t, fs in _FIELDS_BY_TIER.items() for f in fs}
#: Per-endpoint exceptions to FIELD_TIER (Place Details bills ``photos`` as IDs Only, while
#: Text Search and Nearby Search bill ``places.photos`` as Pro).
ENDPOINT_FIELD_TIER: dict[str, dict[str, str]] = {"place_details": {"photos": "ids_only"}}


def field_tier(path: str, endpoint: str | None = None) -> str:
    """Tier of one mask entry (``places.`` prefix and sub-paths ignored; unknown = atmosphere)."""
    if path == "*" or path == "places.*":
        return "atmosphere"
    name = path.removeprefix("places.").split(".")[0]
    override = ENDPOINT_FIELD_TIER.get(endpoint or "", {})
    return override.get(name) or FIELD_TIER.get(name, "atmosphere")


def detect_sku(endpoint: str, mask: str) -> str:
    """SKU billed for a call: the highest field tier, rounded up to a SKU the endpoint has."""
    needed = max((TIER_ORDER.index(field_tier(p.strip(), endpoint)) for p in mask.split(",") if p.strip()),
                 default=0)
    for sku in ENDPOINT_SKUS[endpoint]:
        if TIER_ORDER.index(sku) >= needed:
            return sku
    raise ValueError(f"no SKU for {endpoint}")


@dataclass(frozen=True)
class CostLine:
    """Cost of a number of calls of one (endpoint, SKU)."""

    endpoint: str
    sku: str
    calls: int
    gross_usd: float
    net_usd: float


class CostModel:
    """Tiered prices and free monthly calls, read from :class:`Pricing`."""

    def __init__(self, pricing: Pricing) -> None:
        self.pricing = pricing

    def unit_price(self, endpoint: str, sku: str) -> float:
        """First-tier USD price of one call."""
        return self.pricing.prices[endpoint][sku][0][1] / 1000.0

    def tiered_cost(self, endpoint: str, sku: str, calls: int) -> float:
        """USD for ``calls`` calls in one month, applying the volume tiers."""
        total, done = 0.0, 0
        for upto, per_1000 in self.pricing.prices[endpoint][sku]:
            n = calls - done if upto is None else min(calls, upto) - done
            if n > 0:
                total += n * per_1000 / 1000.0
                done += n
            if done >= calls:
                break
        return total

    def line(self, endpoint: str, sku: str, calls: int) -> CostLine:
        """Gross and net-of-free-tier cost for ``calls`` calls."""
        free = min(calls, self.pricing.free_calls.get(sku, 0))
        gross = self.tiered_cost(endpoint, sku, calls)
        net = max(0.0, gross - self.tiered_cost(endpoint, sku, free))
        return CostLine(endpoint, sku, calls, round(gross, 4), round(net, 4))

    def lines(self, counts: Mapping[tuple[str, str], int]) -> list[CostLine]:
        """Cost lines for a mapping ``(endpoint, sku) -> calls``, sorted."""
        return [self.line(e, s, n) for (e, s), n in sorted(counts.items()) if n]

    def totals(self, counts: Mapping[tuple[str, str], int]) -> tuple[float, float]:
        """(gross, net) USD for a mapping ``(endpoint, sku) -> calls``."""
        ls = self.lines(counts)
        return round(sum(c.gross_usd for c in ls), 4), round(sum(c.net_usd for c in ls), 4)


# ----- free mode: per-SKU free caps, instruments ----------------------------------------------

class FreeCapExhausted(RuntimeError):
    """A metered call would exceed the usable free monthly cap of its SKU (free mode only)."""

    def __init__(self, sku_key: str) -> None:
        self.sku_key = sku_key
        super().__init__(f"free monthly cap of {sku_key} is exhausted; call not made")


def sku_key(endpoint: str, mask: str) -> str:
    """``endpoint/sku`` billed for a call with ``mask`` (e.g. ``text_search/atmosphere``)."""
    return f"{endpoint}/{detect_sku(endpoint, mask)}"


def usable_cap(cfg: Config, key: str) -> int | None:
    """floor(cap x margin) for an ``endpoint/sku`` key; ``None`` = unlimited.

    IDs-only SKUs are always unlimited (Phase 1 must never be limited). A SKU missing from
    ``free_caps`` has no known free allowance and is therefore usable 0 times in free mode.
    """
    if key.endswith("/ids_only"):
        return None
    if key not in cfg.free_caps:
        return 0
    cap = cfg.free_caps[key]
    return None if cap is None else max(0, math.floor(cap * cfg.free_cap_margin + 1e-9))


def free_remaining(cfg: Config, mtd: Mapping[str, int]) -> dict[str, int | None]:
    """Usable calls left this month per ``endpoint/sku`` (``None`` = unlimited)."""
    keys = set(cfg.free_caps) | set(mtd)
    out: dict[str, int | None] = {}
    for k in sorted(keys):
        cap = usable_cap(cfg, k)
        out[k] = None if cap is None else max(0, cap - mtd.get(k, 0))
    return out


@dataclass(frozen=True)
class Instrument:
    """A paid-tier way of fetching place data: an endpoint with a field-mask tier."""

    label: str      # I1..I6
    endpoint: str   # text_search, nearby_search, place_details
    tier: str       # atmosphere or enterprise (mask tier)

    @property
    def name(self) -> str:
        """``endpoint/tier``, e.g. ``text_search/atmosphere``."""
        return f"{self.endpoint}/{self.tier}"

    def mask(self, cfg: Config) -> str:
        """The field mask this instrument sends."""
        return cfg.mask(self.endpoint, self.tier)

    def key(self, cfg: Config) -> str:
        """The ``endpoint/sku`` its calls are billed at (normally equal to :attr:`name`)."""
        return sku_key(self.endpoint, self.mask(cfg))


INSTRUMENTS: list[Instrument] = [
    Instrument("I1", "text_search", "atmosphere"), Instrument("I2", "text_search", "enterprise"),
    Instrument("I3", "nearby_search", "atmosphere"), Instrument("I4", "nearby_search", "enterprise"),
    Instrument("I5", "place_details", "atmosphere"), Instrument("I6", "place_details", "enterprise"),
]


def instruments_for(cfg: Config, endpoint: str) -> list[Instrument]:
    """Instruments of an endpoint in fill order for ``cfg.tier`` (auto: atmosphere first)."""
    tiers = {"auto": ["atmosphere", "enterprise"], "atmosphere": ["atmosphere"],
             "enterprise": ["enterprise"]}[cfg.tier]
    by_tier = {i.tier: i for i in INSTRUMENTS if i.endpoint == endpoint}
    return [by_tier[t] for t in tiers]


def instrument_by_name(name: str) -> Instrument:
    """Look up an instrument by ``endpoint/tier``."""
    for i in INSTRUMENTS:
        if i.name == name:
            return i
    raise KeyError(name)

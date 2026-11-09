"""Cost model: SKU detection from a field mask and tiered USD prices with free-tier netting.

Billing rule mirrored here: the highest-tier field in the mask sets the SKU for the whole call,
and every page of a paginated Text Search is one call.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .config import Pricing

TIER_ORDER = ["ids_only", "essentials", "pro", "enterprise", "atmosphere"]

ENDPOINT_SKUS: dict[str, list[str]] = {
    "text_search": ["ids_only", "pro", "enterprise", "atmosphere"],
    "nearby_search": ["pro", "enterprise", "atmosphere"],  # no free IDs-only SKU in Nearby
    "place_details": ["ids_only", "essentials", "pro", "enterprise", "atmosphere"],
}

_FIELDS_BY_TIER: dict[str, list[str]] = {
    "ids_only": ["id", "name", "attributions", "nextPageToken"],
    "essentials": ["addressComponents", "adrFormatAddress", "formattedAddress", "location",
                   "plusCode", "postalAddress", "shortFormattedAddress", "types", "viewport"],
    "pro": ["accessibilityOptions", "businessStatus", "containingPlaces", "displayName",
            "googleMapsLinks", "googleMapsUri", "iconBackgroundColor", "iconMaskBaseUri",
            "photos", "primaryType", "primaryTypeDisplayName", "pureServiceAreaBusiness",
            "subDestinations", "timeZone", "utcOffsetMinutes"],
    "enterprise": ["currentOpeningHours", "currentSecondaryOpeningHours",
                   "internationalPhoneNumber", "nationalPhoneNumber", "priceLevel", "priceRange",
                   "rating", "regularOpeningHours", "regularSecondaryOpeningHours",
                   "userRatingCount", "websiteUri"],
    "atmosphere": ["allowsDogs", "curbsidePickup", "delivery", "dineIn", "editorialSummary",
                   "evChargeOptions", "fuelOptions", "generativeSummary", "goodForChildren",
                   "goodForGroups", "goodForWatchingSports", "liveMusic", "menuForChildren",
                   "outdoorSeating", "parkingOptions", "paymentOptions", "reservable", "restroom",
                   "reviews", "reviewSummary", "servesBeer", "servesBreakfast", "servesBrunch",
                   "servesCocktails", "servesCoffee", "servesDessert", "servesDinner",
                   "servesLunch", "servesVegetarianFood", "servesWine", "takeout"],
}
FIELD_TIER: dict[str, str] = {f: t for t, fs in _FIELDS_BY_TIER.items() for f in fs}


def field_tier(path: str) -> str:
    """Tier of one mask entry (``places.`` prefix and sub-paths ignored; unknown = atmosphere)."""
    if path == "*" or path == "places.*":
        return "atmosphere"
    name = path.removeprefix("places.").split(".")[0]
    return FIELD_TIER.get(name, "atmosphere")


def detect_sku(endpoint: str, mask: str) -> str:
    """SKU billed for a call: the highest field tier, rounded up to a SKU the endpoint has."""
    needed = max((TIER_ORDER.index(field_tier(p.strip())) for p in mask.split(",") if p.strip()),
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

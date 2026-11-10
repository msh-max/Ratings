"""Configuration: type presets, field masks and prices (all overridable from a JSON file).

Everything that may need changing without a code change (a field the live API rejects, a price
update) lives in :class:`Config`, which can be loaded from a JSON file via ``--config``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ATTRIBUTION = "Data © Google — Powered by Google"

TYPE_PRESETS: dict[str, list[str]] = {
    "restaurants": ["restaurant", "fast_food_restaurant", "meal_takeaway", "meal_delivery"],
}
TYPE_PRESETS["food_and_drink"] = TYPE_PRESETS["restaurants"] + [
    "cafe", "coffee_shop", "bakery", "dessert_shop", "ice_cream_shop", "juice_shop",
    "tea_house", "sandwich_shop", "cafeteria", "food_court",
]

#: Broad food list used by the audit's Nearby Search (all preset types plus extras, <= 50).
AUDIT_BROAD_TYPES: list[str] = TYPE_PRESETS["food_and_drink"] + [
    "bar", "pub", "deli", "diner", "bistro", "snack_bar", "pastry_shop", "cake_shop",
    "coffee_stand", "kebab_shop", "pizza_delivery", "buffet_restaurant", "family_restaurant",
    "fine_dining_restaurant", "breakfast_restaurant", "brunch_restaurant", "dessert_restaurant",
    "halal_restaurant", "shawarma_restaurant", "falafel_restaurant",
]

FIELDS_PRO = [
    "id", "name", "displayName", "formattedAddress", "shortFormattedAddress", "addressComponents",
    "location", "plusCode", "types", "primaryType", "primaryTypeDisplayName", "businessStatus",
    "googleMapsUri", "photos", "utcOffsetMinutes", "pureServiceAreaBusiness",
    "accessibilityOptions",
]
FIELDS_ENTERPRISE = [
    "rating", "userRatingCount", "priceLevel", "priceRange", "regularOpeningHours", "websiteUri",
    "nationalPhoneNumber", "internationalPhoneNumber",
]
FIELDS_ATMOSPHERE = [
    "dineIn", "takeout", "delivery", "curbsidePickup", "reservable", "outdoorSeating",
    "servesBreakfast", "servesBrunch", "servesLunch", "servesDinner", "servesCoffee",
    "servesDessert", "servesVegetarianFood", "goodForChildren", "goodForGroups",
    "menuForChildren", "restroom", "paymentOptions", "parkingOptions", "editorialSummary",
    "reviewSummary",
]

DISCOVER_MASK = "places.id,nextPageToken"
AUDIT_MASK = "places.id,places.types,places.addressComponents"

#: Central Riyadh, roughly 2 x 2 km (Olaya / King Fahd Rd), used by ``smoke``.
SMOKE_BBOX: tuple[float, float, float, float] = (24.682, 46.675, 24.700, 46.695)

#: Area presets (``--area``). ``None`` = the padded bounding box of the Saudi boundary polygon.
#: ``riyadh`` = Riyadh metro incl. Diriyah and King Khalid airport (not Al Kharj).
AREAS: dict[str, tuple[float, float, float, float] | None] = {
    "riyadh": (24.40, 46.35, 25.10, 47.00),
    "saudi": None,
}

Area = Literal["riyadh", "saudi"]
Tier = Literal["auto", "enterprise", "atmosphere"]
MaskTier = Literal["enterprise", "atmosphere"]

#: Free monthly calls per SKU ("endpoint/sku"; ``None`` = unlimited), verified 2026-11-09 at
#: developers.google.com/maps/billing-and-pricing/pricing. Caps are per SKU, per calendar month,
#: per billing account.
FREE_CAPS: dict[str, int | None] = {
    "text_search/ids_only": None, "text_search/pro": 5_000, "text_search/enterprise": 1_000,
    "text_search/atmosphere": 1_000,
    "nearby_search/pro": 5_000, "nearby_search/enterprise": 1_000,
    "nearby_search/atmosphere": 1_000,
    "place_details/ids_only": None, "place_details/essentials": 10_000, "place_details/pro": 5_000,
    "place_details/enterprise": 1_000, "place_details/atmosphere": 1_000,
}

# USD per 1,000 calls as volume tiers: [[calls_upper_bound_or_null, usd_per_1000], ...].
# First tier verified 2026-11-09 at developers.google.com/maps/billing-and-pricing/pricing;
# the volume tiers above 100k follow Google's published discount ladder - check before relying.
_LADDER = (100_000, 500_000, 1_000_000, 5_000_000, None)
_FACTORS = (1.0, 0.8, 0.6, 0.3, 0.075)


def _ladder(first: float) -> list[tuple[int | None, float]]:
    """Build a volume-tier price ladder from the first-tier price."""
    return [(upto, round(first * f, 4)) for upto, f in zip(_LADDER, _FACTORS)]


def _default_prices() -> dict[str, dict[str, list[tuple[int | None, float]]]]:
    return {
        "text_search": {"ids_only": [(None, 0.0)], "pro": _ladder(32), "enterprise": _ladder(35),
                        "atmosphere": _ladder(40)},
        "nearby_search": {"pro": _ladder(32), "enterprise": _ladder(35), "atmosphere": _ladder(40)},
        "place_details": {"ids_only": [(None, 0.0)], "essentials": _ladder(5), "pro": _ladder(17),
                          "enterprise": _ladder(20), "atmosphere": _ladder(25)},
    }


class Pricing(BaseModel):
    """Prices per endpoint and SKU, plus free monthly calls per SKU."""

    prices: dict[str, dict[str, list[tuple[int | None, float]]]] = Field(
        default_factory=_default_prices)
    free_calls: dict[str, int] = Field(default_factory=lambda: {
        "ids_only": 0, "essentials": 10_000, "pro": 5_000, "enterprise": 1_000,
        "atmosphere": 1_000})


class Config(BaseModel):
    """All settings of one run. Stored in ``runs.config_json`` and reused on resume."""

    area: Area = "riyadh"
    bbox: tuple[float, float, float, float] | None = None  # overrides the area preset
    types: list[str] = Field(default_factory=lambda: list(TYPE_PRESETS["restaurants"]))
    text_queries: dict[str, str] = Field(default_factory=dict)  # type -> textQuery override
    tier: Tier = "auto"
    free_only: bool = True  # False only with --allow-paid
    free_caps: dict[str, int | None] = Field(default_factory=lambda: dict(FREE_CAPS))
    free_cap_margin: float = Field(0.9, gt=0.0, le=1.0)
    language: str = "en"
    region_code: str = "SA"
    seed_cell_deg: float = 1.0
    min_cell_deg: float = 0.002
    boundary_buffer_deg: float = 0.1
    include_reviews: bool = False
    fields_pro: list[str] = Field(default_factory=lambda: list(FIELDS_PRO))
    fields_enterprise: list[str] = Field(default_factory=lambda: list(FIELDS_ENTERPRISE))
    fields_atmosphere: list[str] = Field(default_factory=lambda: list(FIELDS_ATMOSPHERE))
    exclude_fields: list[str] = Field(default_factory=list)
    audit_types: list[str] = Field(default_factory=lambda: list(AUDIT_BROAD_TYPES))
    pricing: Pricing = Field(default_factory=Pricing)
    # Operational settings: taken from the command line, never from a stored run.
    qps: float = 9.0
    concurrency: int = 8
    max_budget_usd: float = 1000.0
    audit_cells: int = 100
    min_recall: float = 0.97

    def text_query(self, place_type: str) -> str:
        """Return the ``textQuery`` used for a type (default: the type word, e.g. ``restaurant``)."""
        return self.text_queries.get(place_type, place_type.replace("_", " "))

    def mask_tier(self, tier: str | None = None) -> MaskTier:
        """Field-mask tier: ``tier`` if given, else the configured one (``auto`` = atmosphere)."""
        t = tier or self.tier
        return "enterprise" if t == "enterprise" else "atmosphere"

    def place_fields(self, tier: str | None = None) -> list[str]:
        """Phase 2 place fields for a mask tier (default: configured), minus ``exclude_fields``.

        ``reviews`` (an Atmosphere field) is only added to Atmosphere masks, so that an
        Enterprise mask is never billed at the Atmosphere SKU.
        """
        mt = self.mask_tier(tier)
        fields = self.fields_pro + self.fields_enterprise
        if mt == "atmosphere":
            fields = fields + self.fields_atmosphere
            if self.include_reviews:
                fields = fields + ["reviews"]
        return [f for f in dict.fromkeys(fields) if f not in self.exclude_fields]

    def text_mask(self, tier: str | None = None) -> str:
        """Phase 2 Text Search field mask (``places.`` prefix plus ``nextPageToken``)."""
        return ",".join([f"places.{f}" for f in self.place_fields(tier)] + ["nextPageToken"])

    def nearby_mask(self, tier: str | None = None) -> str:
        """Phase 2 Nearby Search field mask (``places.`` prefix, no pagination)."""
        return ",".join(f"places.{f}" for f in self.place_fields(tier))

    def details_mask(self, tier: str | None = None) -> str:
        """Phase 2 Place Details field mask (no ``places.`` prefix)."""
        return ",".join(self.place_fields(tier))

    def mask(self, endpoint: str, tier: str | None = None) -> str:
        """Phase 2 mask for ``endpoint`` (text_search, nearby_search or place_details)."""
        return {"text_search": self.text_mask, "nearby_search": self.nearby_mask,
                "place_details": self.details_mask}[endpoint](tier)


#: Settings taken from the command line / current --config even when a stored run is resumed.
#: The free caps and margin are operational so that a lowered margin applies at once.
OPERATIONAL_FIELDS = ("qps", "concurrency", "max_budget_usd", "audit_cells", "min_recall",
                      "free_caps", "free_cap_margin")


def load_config(path: Path | None) -> Config:
    """Load a :class:`Config` from a JSON file (any subset of fields), or the defaults."""
    if path is None:
        return Config()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data.get("free_caps"), dict):  # partial override: merge with the defaults
        data["free_caps"] = {**FREE_CAPS, **data["free_caps"]}
    return Config.model_validate(data)


def config_from_stored(text: str) -> Config:
    """A run's stored ``config_json``. Runs stored before area/free mode existed were country-wide
    paid runs, so a missing ``area`` means ``saudi`` and a missing ``free_only`` means paid."""
    data = json.loads(text)
    data.setdefault("area", "saudi")
    data.setdefault("free_only", False)
    return Config.model_validate(data)

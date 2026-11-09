"""Turn a Places API (New) ``Place`` object into a ``places`` row.

Country and admin areas come ONLY from ``addressComponents``; coordinates are stored as given and
never used to derive anything.
"""

from __future__ import annotations

import json
from typing import Any

ATMOSPHERE_KEYS = [
    "dineIn", "takeout", "delivery", "curbsidePickup", "reservable", "outdoorSeating",
    "servesBreakfast", "servesBrunch", "servesLunch", "servesDinner", "servesCoffee",
    "servesDessert", "servesVegetarianFood", "goodForChildren", "goodForGroups", "menuForChildren",
    "restroom", "paymentOptions", "parkingOptions",
]


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def component(components: list[dict[str, Any]] | None, kind: str, short: bool = False) -> str | None:
    """Text of the first address component whose ``types`` contains ``kind``."""
    for comp in components or []:
        if kind in comp.get("types", []):
            return comp.get("shortText" if short else "longText")
    return None


def country_code(place: dict[str, Any]) -> str | None:
    """ISO country code from ``addressComponents`` (type ``country``, ``shortText``) or None."""
    code = component(place.get("addressComponents"), "country", short=True)
    return code.upper() if code else None


def is_saudi(code: str | None) -> bool:
    """``country_code == 'SA'`` or unknown (null is counted separately as country_unknown)."""
    return code is None or code == "SA"


def _text(obj: Any) -> str | None:
    """``text`` of a LocalizedText (also unwraps reviewSummary's nested ``text``)."""
    while isinstance(obj, dict):
        obj = obj.get("text")
    return obj


def place_to_row(place: dict[str, Any]) -> dict[str, Any]:
    """Map a Place JSON object to the ``places`` columns (minus run bookkeeping columns)."""
    comps = place.get("addressComponents")
    code = country_code(place)
    display = place.get("displayName") or {}
    loc = place.get("location") or {}
    plus = place.get("plusCode") or {}
    hours = place.get("regularOpeningHours")
    photos = place.get("photos") or []
    atmosphere = {k: place[k] for k in ATMOSPHERE_KEYS if k in place}
    return {
        "place_id": place["id"],
        "name": place.get("name"),
        "display_name": display.get("text"),
        "display_name_lang": display.get("languageCode"),
        "formatted_address": place.get("formattedAddress"),
        "short_formatted_address": place.get("shortFormattedAddress"),
        "address_components_json": _dumps(comps),
        "country_code": code,
        "region": component(comps, "administrative_area_level_1"),
        "region_short": component(comps, "administrative_area_level_1", short=True),
        "city": component(comps, "locality") or component(comps, "administrative_area_level_2"),
        "district": component(comps, "sublocality_level_1") or component(comps, "sublocality")
        or component(comps, "neighborhood"),
        "postal_code": component(comps, "postal_code"),
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "plus_code_global": plus.get("globalCode"),
        "plus_code_compound": plus.get("compoundCode"),
        "types_json": _dumps(place.get("types")),
        "primary_type": place.get("primaryType"),
        "primary_type_display": _text(place.get("primaryTypeDisplayName")),
        "business_status": place.get("businessStatus"),
        "google_maps_uri": place.get("googleMapsUri"),
        "website_uri": place.get("websiteUri"),
        "phone_national": place.get("nationalPhoneNumber"),
        "phone_international": place.get("internationalPhoneNumber"),
        "rating": place.get("rating"),
        "user_rating_count": place.get("userRatingCount"),
        "price_level": place.get("priceLevel"),
        "price_range_json": _dumps(place.get("priceRange")),
        "opening_hours_json": _dumps(hours),
        "weekday_descriptions_json": _dumps((hours or {}).get("weekdayDescriptions")),
        "utc_offset_minutes": place.get("utcOffsetMinutes"),
        "photo_count": len(photos) if "photos" in place else None,
        "first_photo_name": photos[0].get("name") if photos else None,
        "pure_service_area_business": (None if "pureServiceAreaBusiness" not in place
                                       else int(bool(place["pureServiceAreaBusiness"]))),
        "accessibility_options_json": _dumps(place.get("accessibilityOptions")),
        "atmosphere_json": _dumps(atmosphere) if atmosphere else None,
        "editorial_summary": _text(place.get("editorialSummary")),
        "review_summary": _text(place.get("reviewSummary")),
        "is_saudi": int(is_saudi(code)),
        "raw_json": json.dumps(place, ensure_ascii=False, sort_keys=True),
    }

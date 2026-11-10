"""Test 8: parsing a real-shaped Text Search response; test 6 (part): country classification."""

from __future__ import annotations

import json
from pathlib import Path

from ksa_restaurants.parse import country_code, is_saudi, place_to_row

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "text_search_response.json").read_text("utf-8"))


def test_full_place_is_parsed():
    row = place_to_row(FIXTURE["places"][0])
    assert row["place_id"] == "ChIJs5ydyTiuEmsR0fRSlU0C7k0"
    assert row["name"] == "places/ChIJs5ydyTiuEmsR0fRSlU0C7k0"
    assert (row["display_name"], row["display_name_lang"]) == ("Najd Village", "en")
    assert (row["lat"], row["lng"]) == (24.6951227, 46.6847106)
    assert (row["country_code"], row["is_saudi"]) == ("SA", 1)
    assert (row["region"], row["city"], row["district"], row["postal_code"]) == \
        ("Riyadh Province", "Riyadh", "Al Olaya", "12214")
    assert (row["rating"], row["user_rating_count"]) == (4.4, 2310)
    assert row["price_level"] == "PRICE_LEVEL_MODERATE"
    assert json.loads(row["price_range_json"])["startPrice"] == {"currencyCode": "SAR", "units": "50"}
    hours = json.loads(row["opening_hours_json"])
    assert hours["periods"][0]["open"] == {"day": 0, "hour": 12, "minute": 0}
    assert json.loads(row["weekday_descriptions_json"])[0] == "Monday: 12:00 – 11:30 PM"
    assert (row["plus_code_global"], row["primary_type"], row["primary_type_display"]) == \
        ("7HQ8MM46+2V", "restaurant", "Restaurant")
    assert (row["phone_national"], row["phone_international"]) == ("011 465 0000", "+966 11 465 0000")
    assert (row["photo_count"], row["first_photo_name"]) == (1, "places/ChIJs5ydyTiuEmsR0fRSlU0C7k0/photos/AUc7tXX")
    assert row["pure_service_area_business"] == 0 and row["utc_offset_minutes"] == 180
    atm = json.loads(row["atmosphere_json"])
    assert atm["takeout"] is True and atm["delivery"] is False and atm["paymentOptions"]["acceptsNfc"]
    assert row["editorial_summary"].startswith("Traditional Najdi")
    assert row["review_summary"] == "People praise the kabsa and the setting."
    assert json.loads(row["raw_json"])["id"] == row["place_id"]
    assert json.loads(row["types_json"])[0] == "restaurant"


def test_country_classification():
    sa, bh, unknown = FIXTURE["places"]
    assert (country_code(sa), is_saudi(country_code(sa))) == ("SA", True)
    assert (country_code(bh), is_saudi(country_code(bh))) == ("BH", False)
    assert (country_code(unknown), is_saudi(country_code(unknown))) == (None, True)
    rows = [place_to_row(p) for p in FIXTURE["places"]]
    assert [r["is_saudi"] for r in rows] == [1, 0, 1]
    assert rows[1]["rating"] is None and rows[1]["user_rating_count"] == 0
    assert rows[2]["photo_count"] is None and rows[2]["atmosphere_json"] is None

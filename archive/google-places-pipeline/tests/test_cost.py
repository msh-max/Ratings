"""Test 9: tier detection from field masks, price lookup, volume tiers, free-tier netting."""

from __future__ import annotations

import math

import pytest

from ksa_restaurants.config import AUDIT_MASK, DISCOVER_MASK, Config, Pricing
from ksa_restaurants.cost import CostModel, detect_sku


def test_sku_detection():
    cfg = Config()
    assert detect_sku("text_search", DISCOVER_MASK) == "ids_only"
    assert detect_sku("text_search", "places.id,places.name,places.attributions,nextPageToken") == "ids_only"
    assert detect_sku("nearby_search", "places.id") == "pro"  # no free tier in Nearby
    assert detect_sku("nearby_search", AUDIT_MASK) == "pro"
    assert detect_sku("text_search", "places.id,places.formattedAddress") == "pro"
    assert detect_sku("place_details", "id,formattedAddress,location") == "essentials"
    assert detect_sku("place_details", "id,displayName") == "pro"
    assert detect_sku("text_search", "places.id,places.rating") == "enterprise"
    assert detect_sku("text_search", cfg.text_mask()) == "atmosphere"
    assert detect_sku("place_details", cfg.details_mask()) == "atmosphere"
    ent = Config(tier="enterprise")
    assert detect_sku("text_search", ent.text_mask()) == "enterprise"
    assert detect_sku("place_details", ent.details_mask()) == "enterprise"
    assert "reviews" not in ent.details_mask().split(",")
    assert detect_sku("place_details", "*") == "atmosphere"


def test_mask_from_config_lists():
    cfg = Config(tier="enterprise", exclude_fields=["priceRange"])
    mask = cfg.text_mask().split(",")
    assert mask[-1] == "nextPageToken" and all(m.startswith("places.") for m in mask[:-1])
    assert "places.priceRange" not in mask and "places.rating" in mask
    assert not any(m.startswith("places.") for m in cfg.details_mask().split(","))
    assert "reviews" in Config(include_reviews=True).details_mask().split(",")


def test_prices_tiers_and_free_netting():
    m = CostModel(Pricing())
    assert m.unit_price("text_search", "atmosphere") == pytest.approx(0.040)
    assert m.unit_price("text_search", "enterprise") == pytest.approx(0.035)
    assert m.unit_price("place_details", "atmosphere") == pytest.approx(0.025)
    assert m.unit_price("place_details", "essentials") == pytest.approx(0.005)
    assert m.unit_price("text_search", "ids_only") == 0
    line = m.line("text_search", "atmosphere", 10_000)
    assert line.gross_usd == pytest.approx(400.0) and line.net_usd == pytest.approx(360.0)  # 1,000 free
    assert m.line("place_details", "pro", 4_000).net_usd == 0  # inside 5,000 free
    big = m.tiered_cost("text_search", "enterprise", 150_000)
    assert math.isclose(big, 100_000 * 0.035 + 50_000 * 0.028)
    atm = m.tiered_cost("text_search", "atmosphere", 200_000)
    assert math.isclose(atm, 100_000 * 0.040 + 100_000 * 0.032)
    assert m.totals({("text_search", "ids_only"): 10**6, ("nearby_search", "pro"): 100}) == (3.2, 0.0)


def test_prices_are_configurable():
    p = Pricing.model_validate({"prices": {**Pricing().prices, "place_details": {
        "atmosphere": [[None, 30.0]]}}, "free_calls": {"atmosphere": 0}})
    m = CostModel(p)
    assert m.line("place_details", "atmosphere", 1000).net_usd == pytest.approx(30.0)

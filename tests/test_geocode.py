import json

import httpx

from ranking.geocode import USER_AGENT, Geocoder, accept, in_bbox


def test_bbox():
    assert in_bbox(24.71, 46.67)
    assert not in_bbox(21.54, 39.17)      # Jeddah
    assert not in_bbox(24.71, 47.10)


def test_accept_rule():
    good = {"name": "Sample Bistro", "lat": "24.7612", "lon": "46.6401"}
    assert accept("Sample Bistro", good) == (24.7612, 46.6401)
    assert accept("The Sample Bistro Restaurant", good) == (24.7612, 46.6401)
    assert accept("Sample Bistro", {**good, "name": "Unrelated Pharmacy"}) is None
    assert accept("Marble", {**good, "name": "Red Marble Market"}) is None    # contained, not the same
    assert accept("Al-Sample House", {**good, "name": "El Sample House"}) == (24.7612, 46.6401)
    assert accept("Sample Bistro", {**good, "lat": "21.5", "lon": "39.2"}) is None
    assert accept("Sample Bistro", {"name": "Sample Bistro"}) is None
    # falls back to the first part of display_name when name is empty
    assert accept("Sample Bistro", {"name": "", "display_name": "Sample Bistro, Olaya, Riyadh",
                                    "lat": "24.7", "lon": "46.7"}) == (24.7, 46.7)


def _mock_client(calls, payload):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_geocoder_queries_caches_and_throttles(tmp_path):
    calls, sleeps = [], []
    clock = iter([0.0, 0.0, 0.2, 0.2, 5.0, 5.0])
    payload = [{"name": "Sample Bistro", "display_name": "Sample Bistro, Riyadh",
                "lat": "24.76", "lon": "46.64"}]
    cache = tmp_path / "cache.json"
    g = Geocoder(cache, client=_mock_client(calls, payload), sleep=sleeps.append,
                 clock=lambda: next(clock))
    assert g.locate("Sample Bistro") == (24.76, 46.64)
    assert g.locate("Other Place") is None          # result name does not match -> rejected
    assert len(calls) == 2
    req = calls[0]
    assert req.headers["user-agent"] == USER_AGENT
    assert req.url.params["q"] == "Sample Bistro Riyadh"
    assert req.url.params["viewbox"] == "46.35,25.1,47.0,24.4"
    assert req.url.params["bounded"] == "1"
    assert req.url.params["format"] == "jsonv2"
    assert sleeps and 0.7 < sleeps[0] <= 1.0      # second request waited ~0.8 s

    g.save()
    stored = json.loads(cache.read_text())
    assert set(stored["entries"]) == {"Sample Bistro Riyadh", "Other Place Riyadh"}

    # a new geocoder reads from cache and makes no requests, even offline
    offline = Geocoder(cache, online=False)
    assert offline.locate("Sample Bistro") == (24.76, 46.64)
    assert offline.requests == 0


def test_offline_unknown_returns_none(tmp_path):
    g = Geocoder(tmp_path / "none.json", online=False)
    assert g.locate("Anything") is None and g.requests == 0


def test_http_error_is_not_cached(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    g = Geocoder(tmp_path / "c.json", client=client, sleep=lambda s: None)
    assert g.locate("Sample Bistro") is None
    assert g.cache == {}


def test_prefers_amenity_candidates(tmp_path):
    payload = [{"name": "Sample Bistro", "lat": "24.80", "lon": "46.70", "category": "highway"},
               {"name": "Sample Bistro", "lat": "24.76", "lon": "46.64", "category": "amenity"}]
    g = Geocoder(tmp_path / "c.json", client=_mock_client([], payload), sleep=lambda s: None)
    assert g.locate("Sample Bistro") == (24.76, 46.64)


def test_accept_uses_osm_english_name_tag():
    arabic = {"name": "ماما نورة", "names": ["Mama Noura"], "lat": "24.7", "lon": "46.7"}
    assert accept("Mama Noura", arabic) == (24.7, 46.7)
    assert accept("Karam Beirut", arabic) is None


def test_namedetails_requested_and_stale_cache_refreshed(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"entries": {"Mama Noura Riyadh": {"fetched_at": "2026-11-01", "results": [
        {"name": "ماما نورة", "lat": "24.7", "lon": "46.7"}]}}}), encoding="utf-8")
    assert Geocoder(cache, online=False).locate("Mama Noura") is None      # offline: old entry as is
    calls = []
    payload = [{"name": "ماما نورة", "namedetails": {"name": "ماما نورة", "name:en": "Mama Noura"},
                "lat": "24.7", "lon": "46.7"}]
    g = Geocoder(cache, client=_mock_client(calls, payload), sleep=lambda s: None)
    assert g.locate("Mama Noura") == (24.7, 46.7)
    assert len(calls) == 1 and calls[0].url.params["namedetails"] == "1"
    assert g.cache["Mama Noura Riyadh"]["results"][0]["names"] == ["Mama Noura"]
    assert g.locate("Mama Noura") == (24.7, 46.7) and len(calls) == 1    # now cached


def test_accept_rejects_non_food_places():
    base = {"name": "Wadi", "lat": "24.7", "lon": "46.7"}
    assert accept("Wadi", {**base, "category": "amenity", "type": "restaurant"}) == (24.7, 46.7)
    assert accept("Wadi", {**base, "category": "landuse", "type": "farmyard"}) is None
    assert accept("Ward", {**base, "name": "Ward", "category": "place", "type": "residential"}) is None

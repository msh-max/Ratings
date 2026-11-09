"""HTTP client: retries only on 429/5xx/network errors; clear 400/401/403 errors; key never leaked."""

from __future__ import annotations

import httpx
import pytest

from ksa_restaurants.places_client import PlaceNotFound, PlacesApiError, PlacesClient

KEY = "AIzaSy-test-secret-key"


def _client(handler, **kw):
    return PlacesClient(api_key=KEY, qps=1000, backoff_base=0.0, transport=httpx.MockTransport(handler), **kw)


def _err(status, code, message):
    return httpx.Response(status, json={"error": {"code": status, "status": code, "message": message}})


async def test_retries_429_and_5xx_then_succeeds():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if len(seen) == 1:
            return _err(429, "RESOURCE_EXHAUSTED", "quota")
        if len(seen) == 2:
            return _err(503, "UNAVAILABLE", "try later")
        if len(seen) == 3:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, json={"places": [{"id": "x"}]})

    c = _client(handler)
    body = {"textQuery": "restaurant", "pageSize": 20}
    assert await c.search_text(body, "places.id,nextPageToken") == {"places": [{"id": "x"}]}
    assert len(seen) == 4
    req = seen[-1]
    assert req.url.path == "/v1/places:searchText" and req.method == "POST"
    assert req.headers["X-Goog-FieldMask"] == "places.id,nextPageToken"
    assert req.headers["X-Goog-Api-Key"] == KEY
    await c.aclose()


@pytest.mark.parametrize("status,code,needle", [
    (400, "INVALID_ARGUMENT", "field mask"),
    (401, "UNAUTHENTICATED", "GOOGLE_MAPS_API_KEY"),
    (403, "PERMISSION_DENIED", "Places API (New)"),
])
async def test_client_errors_are_not_retried_and_explain_the_cause(status, code, needle):
    seen = []

    def handler(req):
        seen.append(req)
        return _err(status, code, f"API key {KEY} rejected")

    c = _client(handler)
    with pytest.raises(PlacesApiError) as err:
        await c.search_nearby({}, "places.id")
    assert len(seen) == 1
    assert needle in str(err.value) and code in str(err.value)
    assert KEY not in str(err.value)
    if status == 403:
        assert "billing" in str(err.value)


async def test_details_404_and_request_shape():
    seen = []

    def handler(req):
        seen.append(req)
        return _err(404, "NOT_FOUND", "gone")

    c = _client(handler)
    with pytest.raises(PlaceNotFound):
        await c.place_details("ChIJabc", "id,rating", "ar", "SA")
    assert len(seen) == 1 and seen[0].method == "GET" and seen[0].url.path == "/v1/places/ChIJabc"
    assert seen[0].url.params["languageCode"] == "ar" and seen[0].url.params["regionCode"] == "SA"
    assert seen[0].headers["X-Goog-FieldMask"] == "id,rating"


async def test_gives_up_after_max_attempts():
    seen = []

    def handler(req):
        seen.append(req)
        return _err(500, "INTERNAL", "oops")

    c = _client(handler)
    with pytest.raises(PlacesApiError) as err:
        await c.search_text({}, "places.id")
    assert len(seen) == 6 and err.value.status == 500


def test_missing_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    with pytest.raises(PlacesApiError, match="GOOGLE_MAPS_API_KEY"):
        PlacesClient()

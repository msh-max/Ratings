# Google's terms and this dataset

This project calls the official Google Places API (New). Using the API is allowed. **Storing its
results as a permanent, country-wide database with monthly history is a different question**, and
the Google Maps Platform terms restrict it. Read this page before you rely on the data or build a
product on it. This is not legal advice.

## The bottom line

- A persistent, country-wide table of restaurant names, addresses and ratings with monthly history
  is **not expressly permitted** by the Google Maps Platform terms quoted below. Several clauses
  point the other way (no bulk download, no storing of business names and addresses, no caching
  beyond what the Service Specific Terms allow).
- **Before relying on this data, get legal review, or talk to Google Maps Platform sales** about a
  licence or a product that fits the use case.
- Things that are clearly allowed: storing **place IDs** indefinitely; caching **latitude/longitude
  for up to 30 consecutive calendar days**; showing Places content with the required attribution,
  on a Google map or without a map.

## What the terms say

### Google Maps Platform Terms of Service, §3.2.3

Source: <https://cloud.google.com/maps-platform/terms> (last modified 2026-08-26).

> (a) No Scraping. Customer will not export, extract, or otherwise scrape Google Maps Content for use
> outside the Services. For example, Customer will not: (i) pre-fetch, index, store, reshare, or
> rehost Google Maps Content outside the services; (ii) bulk download ... places information ...;
> (iii) copy and save business names, addresses, or user reviews

> (b) No Caching. Customer will not cache Google Maps Content except as expressly permitted under the
> Maps Service Specific Terms.

> (c) No Creating Content From Google Maps Content ... (iv) use latitude/longitude values from the
> Places API as an input for point-in-polygon analysis

### Google Maps Platform Service Specific Terms, §14 (Places API)

Source: <https://cloud.google.com/maps-platform/terms/maps-service-terms> (last modified 2026-10-28).

- **§14.1:** Places API content may be used without a Google map.
- **§14.2:** Customer "must not use Google Maps Content from the Places API in conjunction with a
  non-Google map".
- **§14.3:** latitude/longitude values may be cached for up to 30 consecutive calendar days.

### Places API policies

Source: <https://developers.google.com/maps/documentation/places/web-service/policies>.

- **Place IDs are exempt from the caching restrictions.** They can be stored indefinitely.
- **Attribution:** show the Google Maps logo, or the text "Google Maps" unmodified, with Places
  content.
- **Third-party content:** credit the authors of reviews and photos as the policy describes.

## How this project relates to each clause

| Clause | What the project does | Status |
|---|---|---|
| §3.2.3(a) No scraping | Uses only the official API; no web pages. But it stores names, addresses, ratings for every place in the country and keeps monthly history. | Storage is the open issue. Not expressly permitted. |
| §3.2.3(b) No caching | `places` and `place_snapshots` keep Places content between runs and across months. | Not expressly permitted except for the items below. |
| Place IDs | Stored indefinitely (`place_id`). | Permitted. |
| §14.3 lat/lng, 30 days | `lat`/`lng` are refreshed each month. The cron runs on the 2nd, so the gap between runs is 28–31 days, and a failed month makes it longer. Coordinates older than 30 days are outside §14.3. | Partly. To comply, a UI should not use coordinates from a release older than 30 days, and old releases/artifacts carry stale coordinates. |
| §3.2.3(c)(iv) no point-in-polygon | The project never runs geometry on place coordinates. Country is taken from `addressComponents`; the Saudi boundary polygon is used only to prune our own search rectangles. The audit compares IDs, not locations. | Designed to comply. |
| §14.2 non-Google map | Out of scope for the pipeline; applies to the UI. | The UI must show Places content on a Google map or with no map, not on OpenStreetMap, Mapbox, Leaflet tiles, etc. |
| Attribution | `summary.json` carries "Data © Google — Powered by Google". | The UI must show the Google Maps logo or the text "Google Maps", and credit review/photo authors if it shows reviews or photos. |
| Publishing releases | The workflow attaches the database and exports to GitHub Releases. In a public repository that is resharing/rehosting under §3.2.3(a)(i). | Keep the repository private, or do not publish the data files. |

## Alternatives, with their own risks

Each option below has its own terms and its own risk. The third-party services **scrape Google
Maps**: they avoid the Places API's storage clauses only by not using the Places API, and the Google
Maps terms quoted above (§3.2.3(a)) prohibit scraping Google Maps Content. Prices are as published by each vendor
and can change.

| Option | What you get | Price | Risk |
|---|---|---|---|
| [Outscraper](https://outscraper.com/pricing/) | Google Maps places export (name, rating, reviews count, etc.) | ≈ $3 per 1,000 places up to 100k, ≈ $1 above | Scraped data; Google Maps terms prohibit scraping. You rely on the vendor's legal position. |
| [Apify Google Maps Scraper](https://apify.com/compass/crawler-google-places) | Same kind of export | From ≈ $1.50 per 1,000 places | Same as above. |
| [SerpApi](https://serpapi.com/pricing) | Google Maps search results via API | Per search; about 20 places per search | Same as above. Billing per search makes country-wide coverage expensive. |
| Bright Data | Google Maps datasets / scraper | ≈ $0.75–2.50 per 1,000 records (unverified) | Same as above. |
| [Google Places Insights](https://developers.google.com/maps/documentation/placesinsights/queries) | Aggregated counts of places (by type, area, rating band) in BigQuery | Through Google sales | Official and licensed for analysis, but returns aggregates, not a per-place table with names. |
| Talk to Google Maps Platform sales | A licence or product for the actual use case | Negotiated | The only route to a clear yes from Google for stored per-place data. |

## Practical guidance for the UI

1. Show "Google Maps" (unmodified text) or the Google Maps logo wherever Places data appears.
2. Show places on a Google map, or with no map at all. Not on a non-Google map.
3. If you show reviews or photos, credit their authors.
4. Link to each place with `google_maps_uri`.
5. Use the latest release only, and treat coordinates older than 30 days as expired.
6. Keep the repository and its releases private until the storage question is resolved.

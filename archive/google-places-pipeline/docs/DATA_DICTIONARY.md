# Data dictionary

Database: SQLite (WAL mode), default `data/ksa_restaurants.sqlite`. Exports: `data/exports/`.

"Source" names the Google Places API (New) field the value comes from. Field paths are those of the
[Place resource](https://developers.google.com/maps/documentation/places/web-service/reference/rest/v1/places).
"Pipeline" means the value is computed by this project, not returned by Google. Types marked
*(declared)* are fixed by the schema; the others are the SQLite storage class the value has in practice.
SKU is the cheapest Text Search tier that returns the field (checked against Google's Text Search
and Place Details pages on 2026-11-09); the highest-tier field in a request bills the whole call.
Place Details tiers a few fields lower: `addressComponents`, `formattedAddress`,
`shortFormattedAddress`, `location`, `plusCode`, `types` are Essentials there and `photos` is IDs
Only. The pipeline's masks always contain Enterprise fields, so this never changes what a run pays.

Contents: [places](#places) · [place_snapshots](#place_snapshots) · [runs](#runs) · [cells](#cells) ·
[fetch_jobs](#fetch_jobs) · [audit_cells](#audit_cells) · [audit_found](#audit_found) ·
[other tables](#other-tables) · [exports](#export-files)

## places

One row per Google place ever seen. Rows are never deleted. Each run updates the row with the latest
values (upsert by `place_id`).

| Column | Type | Source (Places API field) | SKU | Notes |
|---|---|---|---|---|
| place_id | TEXT *(declared, PK)* | `id` | IDs only | Stable Google place ID. May be stored indefinitely (see TERMS.md). |
| name | TEXT | `name` | IDs only | Resource name, `places/{place_id}`. Not the human name. |
| display_name | TEXT | `displayName.text` | Pro | The business name shown on Google Maps. |
| display_name_lang | TEXT | `displayName.languageCode` | Pro | Language of `display_name` (depends on `--language`). |
| formatted_address | TEXT | `formattedAddress` | Pro | Full address. |
| short_formatted_address | TEXT | `shortFormattedAddress` | Pro | Short address. |
| address_components_json | TEXT (JSON) | `addressComponents` | Pro | Array of `{longText, shortText, types, languageCode}`. |
| country_code | TEXT | `addressComponents[type=country].shortText` | Pro | ISO code, e.g. `SA`. Null when Google returns no country component. |
| region | TEXT | `addressComponents` (region component, `longText`) | Pro | Province. Component type is chosen by the code (expected `administrative_area_level_1`). |
| region_short | TEXT | `addressComponents` (region component, `shortText`) | Pro | |
| city | TEXT | `addressComponents` (city component) | Pro | Expected `locality`. |
| district | TEXT | `addressComponents` (district component) | Pro | Expected a `sublocality` type. |
| postal_code | TEXT | `addressComponents[type=postal_code]` | Pro | |
| lat | REAL | `location.latitude` | Pro | Cacheable 30 days only under Google's terms. Never used for point-in-polygon. |
| lng | REAL | `location.longitude` | Pro | Same restriction. |
| plus_code_global | TEXT | `plusCode.globalCode` | Pro | |
| plus_code_compound | TEXT | `plusCode.compoundCode` | Pro | |
| types_json | TEXT (JSON) | `types` | Pro | All Google types of the place. |
| primary_type | TEXT | `primaryType` | Pro | e.g. `restaurant`, `fast_food_restaurant`. |
| primary_type_display | TEXT | `primaryTypeDisplayName.text` | Pro | Localised label. |
| business_status | TEXT | `businessStatus` | Pro | `OPERATIONAL`, `CLOSED_TEMPORARILY`, `CLOSED_PERMANENTLY`. |
| google_maps_uri | TEXT | `googleMapsUri` | Pro | Link to the place on Google Maps. |
| website_uri | TEXT | `websiteUri` | Enterprise | |
| phone_national | TEXT | `nationalPhoneNumber` | Enterprise | |
| phone_international | TEXT | `internationalPhoneNumber` | Enterprise | |
| rating | REAL *(declared)* | `rating` | Enterprise | 1.0–5.0 or null (validated). |
| user_rating_count | INT *(declared)* | `userRatingCount` | Enterprise | Number of reviews/ratings, ≥ 0 or null (validated). |
| price_level | TEXT | `priceLevel` | Enterprise | Enum string, e.g. `PRICE_LEVEL_MODERATE`. |
| price_range_json | TEXT (JSON) | `priceRange` | Enterprise | `{startPrice:{currencyCode,units}, endPrice:{...}}`. |
| opening_hours_json | TEXT (JSON) | `regularOpeningHours` | Enterprise | Includes `periods`, `weekdayDescriptions`, `openNow` at fetch time. |
| weekday_descriptions_json | TEXT (JSON) | `regularOpeningHours.weekdayDescriptions` | Enterprise | Seven human-readable lines. |
| utc_offset_minutes | INT | `utcOffsetMinutes` | Pro | 180 for Saudi Arabia. |
| photo_count | INT *(declared)* | `photos` (array length) | Pro | Number of photo references returned (Google returns at most 10). |
| first_photo_name | TEXT | `photos[0].name` | Pro | Photo resource name for the Place Photos endpoint. |
| pure_service_area_business | INT *(declared)* | `pureServiceAreaBusiness` | Pro | 1 = no public premises (e.g. delivery-only), 0 otherwise. |
| accessibility_options_json | TEXT (JSON) | `accessibilityOptions` | Pro | Wheelchair access flags. |
| atmosphere_json | TEXT (JSON) | `dineIn`, `takeout`, `delivery`, `curbsidePickup`, `reservable`, `outdoorSeating`, `servesBreakfast`, `servesBrunch`, `servesLunch`, `servesDinner`, `servesCoffee`, `servesDessert`, `servesVegetarianFood`, `goodForChildren`, `goodForGroups`, `menuForChildren`, `restroom`, `paymentOptions`, `parkingOptions` | Enterprise + Atmosphere | Only filled when the latest fetch went through an Atmosphere instrument (`fetched_via` ends in `/atmosphere`). |
| editorial_summary | TEXT | `editorialSummary.text` | Enterprise + Atmosphere | Often null. |
| review_summary | TEXT | `reviewSummary.text` | Enterprise + Atmosphere | AI-generated summary of reviews; often null. |
| is_saudi | INT | Pipeline | — | `1` when `country_code = 'SA'` or `country_code` is null; else `0`. No geometry is used. |
| fetched_via | TEXT | Pipeline | — | Instrument (`endpoint/sku`) of the latest fetch: `text_search/atmosphere`, `text_search/enterprise`, `nearby_search/atmosphere`, `nearby_search/enterprise`, `place_details/atmosphere` or `place_details/enterprise`. The Atmosphere columns are filled exactly when it ends in `/atmosphere`. |
| last_discovered_run_id | TEXT | Pipeline | — | Last run whose Phase 1 found this ID (set for every discovered ID that has a row, and when the row is created). Can be newer than `last_seen_run_id` when the free caps did not allow a refresh. |
| last_discovered_at | TEXT | Pipeline | — | When `last_discovered_run_id` was set. |
| first_seen_run_id | TEXT | Pipeline | — | Run that first fetched the place. |
| last_seen_run_id | TEXT | Pipeline | — | Last run that fetched data for the place (the age of the values in this row). A place not fetched in the latest run keeps its old value. |
| first_seen_at | TEXT | Pipeline | — | Timestamp of the first fetch. |
| last_seen_at | TEXT | Pipeline | — | Timestamp of the latest fetch. |
| raw_json | TEXT (JSON) | the whole Place object | — | As returned by the API. Not exported. |

Notes:
- `reviews` is requested only with `--include-reviews` (bulky). The schema has no dedicated review
  column, so review data, if requested, is only in `raw_json`.
- Each fetch overwrites the row: a place refreshed through an Enterprise instrument has null
  Atmosphere columns even if an earlier run fetched them (the row always describes one fetch).
- A database created by an earlier version gets the new columns on open (`ALTER TABLE ... ADD COLUMN`).
- Indexes: `is_saudi`, `city`, `region`, `rating`.
- Columns marked *(declared)* INTEGER/REAL above are the only typed ones; every other `places`
  column is declared `TEXT`. Booleans are stored as `0`/`1`.

## place_snapshots

One row per place per run: the history table. A place reached by two fetch jobs still gets one row.

| Column | Type | Source | Notes |
|---|---|---|---|
| run_id | TEXT *(PK part)* | Pipeline | → `runs.run_id`. |
| place_id | TEXT *(PK part)* | `id` | → `places.place_id`. Indexed. |
| fetched_at | TEXT | Pipeline | When the data for this place was fetched in this run. |
| rating | REAL *(declared)* | `rating` | Value in this run. |
| user_rating_count | INT *(declared)* | `userRatingCount` | Value in this run. |
| business_status | TEXT | `businessStatus` | Value in this run. |
| price_level | TEXT | `priceLevel` | Value in this run. |
| sku | TEXT | Pipeline | Instrument (`endpoint/sku`, e.g. `text_search/atmosphere`) that fetched this snapshot. Null for snapshots written before this column existed. |

## runs

One row per pipeline run.

| Column | Type | Source | Notes |
|---|---|---|---|
| run_id | TEXT *(declared, PK)* | Pipeline | Default `YYYY-MM-DDTHHMM`, or `--run-id`. |
| started_at | TEXT | Pipeline | Start time. |
| finished_at | TEXT | Pipeline | End time; null while running or after a crash. |
| status | TEXT | Pipeline | `running` (in progress or crashed; resumable), `completed`, `audit_failed`, `validation_failed`, `budget_aborted`. Warm start and month-over-month use the latest earlier run with the same `bbox_json` whose status is `completed`, `audit_failed` or `validation_failed`. |
| config_json | TEXT (JSON) | Pipeline | Effective configuration (prices, field lists, limits, etc.). Reused when the run is resumed. |
| bbox_json | TEXT (JSON) | Pipeline | Bounding box covered, `[lat_lo, lng_lo, lat_hi, lng_hi]` (the `--area` preset: Riyadh by default, the padded Saudi bbox for `saudi`; or `--bbox`). |
| types_json | TEXT (JSON) | Pipeline | Place types queried. |
| tier | TEXT | Pipeline | `auto`, `enterprise` or `atmosphere`. |
| api_calls_json | TEXT (JSON) | Pipeline | Array of `{phase, endpoint, sku, calls}` (copy of `api_calls`), written at export/report time. |
| est_cost_usd | REAL *(declared)* | Pipeline | Gross USD of the calls actually made (before free-tier netting). |
| places_total | INT *(declared)* | Pipeline | Places with a snapshot in this run. |
| places_saudi | INT *(declared)* | Pipeline | Of those, `is_saudi = 1`. |
| audit_recall | REAL *(declared)* | Pipeline | Overall audit recall = discovery recall (null if no audit). |
| notes | TEXT | Pipeline | Free text, e.g. the budget-abort message. |
| area | TEXT | Pipeline | `riyadh` or `saudi` (the `--area` preset; `bbox_json` holds the actual box, which `--bbox` may override). `saudi` for runs from before this column existed. |
| free_only | INT | Pipeline | `1` = free mode, `0` = paid mode (`--allow-paid`). A run keeps its mode on resume. |
| unfetched | INT | Pipeline | Discovered IDs left without data this run because the free caps were reached (written at the end of Phase 2; always 0 in paid mode). |
| coverage_json | TEXT (JSON) | Pipeline | Copy of `summary.json` → `coverage`, written at export/report time. |
| plan_built_at | TEXT | Pipeline | When the fetch plan (Round T in free mode, the whole plan in paid mode) was persisted; a resumed run does not plan Round T again. |
| audit_fetched_recall | REAL | Pipeline | Overall audit `fetched_recall` (null if no audit). |
| last_call_at | TEXT | Pipeline | UTC time of the run's last recorded metered call. Month-to-date accounting counts a run in every month from its `started_at` to this (falling back to `finished_at`), so a run that spans the month change counts in both months. |

## cells

Quadtree cells of Phase 1, per run and per place type. Internal cells (`split`) and leaves are kept.

| Column | Type | Source | Notes |
|---|---|---|---|
| run_id | TEXT *(PK part)* | Pipeline | |
| cell_id | TEXT *(declared, PK part)* | Pipeline | `<seed_row>_<seed_col>`, each zero-padded to 3 digits (e.g. `008_011`), plus one digit per subdivision level: 0 = south-west, 1 = south-east, 2 = north-west, 3 = north-east (e.g. `008_011302`). |
| type | TEXT *(declared, PK part)* | Pipeline | The `includedType` searched. |
| lat_lo, lng_lo, lat_hi, lng_hi | REAL | Pipeline | Rectangle (degrees) sent as `locationRestriction.rectangle`. |
| depth | INT *(declared)* | Pipeline | 0 for seed cells. |
| parent_id | TEXT | Pipeline | Parent `cell_id`; null for seeds. |
| status | TEXT *(declared)* | Pipeline | `pending`, `leaf` (< 60 results), `split` (saturated, subdivided), `saturated_leaf` (still saturated at `min_cell_deg`; coverage risk). |
| result_count | INT *(declared)* | count of `places[].id` | Distinct place IDs returned for the cell (max 60). |
| fetched | INT *(declared, default 0)* | Pipeline | 1 when the cell was fetched as a Phase 2 Text Search job. |
| discover_calls | INT *(declared, default 0)* | Pipeline | Text Search IDs-only calls (pages, 1–3) spent on this cell. |
| fetch_calls | INT *(declared, default 0)* | Pipeline | Paid Text Search pages spent on this cell in Phase 2. |

## fetch_jobs

The Phase 2 plan and log. Paid mode: the whole plan is built before any paid call. Free mode: Round T
(`text`) is built before any paid-tier call; `nearby` and `details` jobs are planned from the live
database. Each job is marked as it completes so a re-run resumes.

| Column | Type | Source | Notes |
|---|---|---|---|
| run_id | TEXT *(PK part)* | Pipeline | |
| job_id | INT *(declared, PK part)* | Pipeline | Sequence within the run. |
| kind | TEXT *(declared)* | Pipeline | `text` (Text Search pages for a cell/type), `nearby` (one Nearby Search call on a leaf rectangle; free mode Round N, written when the call is made) or `details` (one Place Details call). |
| cell_id | TEXT | Pipeline | Set for `text` and `nearby` jobs. |
| type | TEXT | Pipeline | Set for `text` jobs. |
| place_id | TEXT | Pipeline | Set for `details` jobs. |
| pages | INT *(declared)* | Pipeline | Planned pages for `text` jobs: the larger of `ceil(result_count / 20)` and the pages Phase 1 actually needed for the same request (Google can return a short page that still carries a `nextPageToken`); at most 3. `1` for `details` jobs. |
| status | TEXT *(declared)* | Pipeline | `pending`, `done`, `error` (a `details` job whose place returned `NOT_FOUND`; see `fetch_errors`), `skipped_cap` (the free-cap guard refused the call; a `text` job keeps the pages it already got). |
| instrument | TEXT | Pipeline | Instrument the job uses, `endpoint/tier` (e.g. `text_search/atmosphere`, `nearby_search/enterprise`). |

## audit_cells

One row per audited leaf cell (Phase 3). Primary key `(run_id, cell_id, type)`. The counts are
recomputed from `audit_found` whenever the summary is built.

| Column | Type | Source | Notes |
|---|---|---|---|
| run_id | TEXT | Pipeline | |
| cell_id | TEXT | Pipeline | Audited leaf. The Nearby Search circle is the cell's circumscribed circle (≤ 50 km). |
| type | TEXT | Pipeline | Type of the sampled leaf. |
| found | INT *(declared)* | Nearby Search `places[].id` | Places returned (≤ 20). |
| in_scope | INT *(declared)* | `addressComponents`, `types` | Found places with country `SA` whose types intersect the configured types. |
| matched | INT *(declared)* | Pipeline | In-scope places that Phase 1 of this run discovered (present in `cell_places`). |
| out_of_scope | INT *(declared)* | `addressComponents`, `types` | Found SA places whose types miss the configured preset. |
| recall | REAL *(declared)* | Pipeline | Discovery recall `matched / in_scope` for this cell; null when `in_scope` is 0. |
| fetched | INT | Pipeline | In-scope places that have a snapshot in this run. |
| fetched_recall | REAL | Pipeline | `fetched / in_scope`; below 1 in free mode when the caps left IDs unfetched. |

The overall recall (in `runs.audit_recall` and `summary.json`) is computed over the *distinct* place
IDs found in all audited cells, not as an average of the per-cell values. `out_of_scope_share` =
out-of-scope ÷ found places with country `SA`.

## audit_found

Every place returned by an audit Nearby Search call (mask `places.id,places.types,places.addressComponents`).
Primary key `(run_id, cell_id, type, place_id)`.

| Column | Type | Source | Notes |
|---|---|---|---|
| run_id | TEXT | Pipeline | |
| cell_id | TEXT | Pipeline | Audited leaf. |
| type | TEXT | Pipeline | Type of the sampled leaf. |
| place_id | TEXT | `places[].id` | |
| country_code | TEXT | `addressComponents[type=country].shortText` | Null when missing (then not in scope). |
| types_json | TEXT (JSON) | `places[].types` | |

## Other tables

| Table | Columns | Purpose |
|---|---|---|
| cell_places | run_id, cell_id, type, place_id (PK all four) | Place IDs seen in each leaf in Phase 1 (`places[].id`). Input to the fetch plan. |
| fetch_errors | run_id, place_id (PK), code, message | Place IDs that could not be fetched, e.g. Place Details 404 `NOT_FOUND` (place removed). |
| api_calls | run_id, phase, endpoint, sku (PK), calls INT | Successful calls per `phase` (`discover`, `fetch`, `sweep` (paid mode), `audit`), `endpoint` (`text_search`, `nearby_search`, `place_details`) and `sku` (`ids_only`, `essentials`, `pro`, `enterprise`, `atmosphere`). Written as soon as a call returns. Used for cost reporting and, summed over all runs started in the current UTC month, for the free-cap month-to-date usage. |

## Export files

Written to the `--out` directory (default `data/exports/`) by `export`, `run` and `smoke`. Exports contain `is_saudi = 1` places unless
`--all-countries` is passed, and only places whose `city` matches `--city NAME` (case-insensitive)
when that is given. The monthly workflow attaches all of them to the `data-YYYY-MM` release.

### places_latest.csv.gz and places_latest.jsonl.gz

One row (CSV) or one JSON object per line (JSONL) per Saudi place in the database, ordered by
`place_id` — including places not seen in the latest run (use `last_seen_run_id` to tell them
apart). Columns: every column of `places` above except `raw_json`, in the same order (`place_id`
… `last_seen_at`, 47 columns). In the CSV, JSON columns (`*_json`) are JSON strings and null is an
empty field; in the JSONL, `*_json` values are decoded into JSON arrays/objects and null is `null`.
Column order is stable between runs. No duplicate `place_id`.

### snapshots.csv.gz

All rows of `place_snapshots` (all runs, all countries), ordered by `run_id, place_id`. Columns, in
order: `run_id`, `run_started_at` (from `runs.started_at`), `place_id`, `fetched_at`, `rating`,
`user_rating_count`, `business_status`, `price_level`, `sku`. Use it for rating and review-count history.

### frontend_feed.json.gz

A compact JSON array, one object per Saudi place **discovered or fetched in this run**
(`last_discovered_run_id` = this run or `last_seen_run_id` = this run), ordered by `place_id`, for
the UI. A place discovered but not refreshed (free caps) is **carried forward** with its older
values; `data_run_id` and `data_as_of` say how old they are. Places discovered but never fetched
have no data and are not in the feed (they are counted in `summary.json` → `coverage.unfetched_new`).

| Key | From |
|---|---|
| place_id | `places.place_id` |
| name | `places.display_name` (business name, `displayName.text`) |
| lat, lng | `places.lat`, `places.lng` |
| rating | `places.rating` |
| user_rating_count | `places.user_rating_count` |
| primary_type | `places.primary_type` |
| city | `places.city` |
| region | `places.region` |
| business_status | `places.business_status` |
| price_level | `places.price_level` |
| google_maps_uri | `places.google_maps_uri` |
| data_run_id | `places.last_seen_run_id`: the run the values come from (this run, or an older one when carried forward) |
| data_as_of | `places.last_seen_at`: when those values were fetched |

### summary.json

Machine-readable run summary. Every number is derived from the database at export time. Top-level
keys, in order (taken from a real `--fake` run):

| Key | Content |
|---|---|
| `attribution` | `"Data © Google — Powered by Google"`. |
| `area` | `riyadh` or `saudi`. |
| `run` | `run_id`, `started_at`, `finished_at`, `status`, `tier`, `notes`, `area`, `free_only`, `types` (list), `bbox` (`[lat_lo, lng_lo, lat_hi, lng_hi]`). |
| `coverage` | Over the IDs discovered this run: `discovered`, `fetched_this_run`, `carried_forward` (no data this run, older data exists), `unfetched_new` (no data ever), `unfetched` (= the two before), `fetch_errors`; plus `fetched_not_discovered` (places a search returned that Phase 1 had not seen). |
| `free_mode` | `enabled`, `month` (current UTC month), `margin`, and per `endpoint/sku`: `caps`, `usable` (floor(cap × margin); null = unlimited), `mtd_usage` (calls of all runs started this month), `remaining`; `within_free_caps` (true when no SKU is above its cap this month). Reported in paid mode too. |
| `instruments` | `[{instrument, calls, places}]`: Phase 2 calls and places fetched this run per `endpoint/sku`. |
| `totals` | `places_total` (places with a snapshot this run, all countries), `places_saudi` (of those, `is_saudi = 1`), `country_unknown`, `fetch_errors`, `places_in_db` (all places ever seen). |
| `discovery` | `cells`, `leaves`, `saturated_at_min_size`, `place_ids` (distinct IDs found in Phase 1), `calls` (IDs-only Text Search pages). |
| `by_region` | `[{region, count}]`, Saudi places seen this run, largest first. |
| `by_city_top50` | `[{city, region, count}]`, top 50. |
| `rating_histogram` | `{"1.0-1.5": n, …, "4.5-5.0": n, "none": n}` (0.5-wide buckets; 5.0 falls in `4.5-5.0`). |
| `user_rating_count_percentiles` | `{p10, p25, p50, p75, p90, p99}` (nearest-rank; null when no data). |
| `top100_by_user_rating_count` | `[{place_id, name, city, rating, user_rating_count}]`. |
| `api_calls` | `[{phase, endpoint, sku, calls}]`. |
| `cost` | `lines: [{endpoint, sku, calls, gross_usd, net_usd}]`, `gross_usd`, `net_usd` (net of the monthly free calls, this run alone). List prices: in free mode the bill is $0 while `free_mode.within_free_caps` is true. |
| `audit` | `null` when no audit ran, else `{cells, found, in_scope, matched, recall, out_of_scope_share, fetched, fetched_recall, min_recall}`; `recall` = discovery recall (exit 3 applies to it), null when nothing in scope was found. |
| `validation` | `ok`, `hard: {rating_out_of_range, bad_user_rating_count, empty_id, duplicate_snapshots, discovered_without_data}` (`discovered_without_data` = discovered IDs with no data, no error row and not counted in `runs.unfetched`), `soft: {unfetched (free mode only), rating_without_reviews, country_unknown, missing_display_name, missing_location}`. |
| `month_over_month` | `null` without a previous run, else `{previous_run_id, new, missing, rating_changed, previous_total, total_change_pct, warning}`. A place counts as present in a run when it was discovered or fetched in it; `new`/`missing`/totals compare those sets, `rating_changed` compares snapshots. `warning` is true when the total moved by more than 15 %. |
| `exports` | Paths written: `places_csv`, `places_jsonl`, `snapshots_csv`, `frontend_feed`, `summary_json`, `report_md` (empty for `report`). |
| `exports_meta` | `places_rows`, `frontend_feed_rows`, `frontend_feed_carried_forward` (feed rows whose `data_run_id` is an older run), `all_countries`, `city` (the `--city` filter or null; it applies to the place exports, snapshots and feed, not to the summary). |

### report.md

The same content as `summary.json`, as a human-readable Markdown report. The workflow also shows it
in the GitHub Actions job summary and commits it as `data/summary/latest.md`.

### data/summary/ (in git)

| File | Content |
|---|---|
| `latest.json` | Copy of the latest successful run's `summary.json`. |
| `YYYY-MM.json` | Copy of that month's `summary.json` (one file per month, kept). |
| `latest.md` | Copy of the latest `report.md`. |

The monthly workflow commits these to the default branch from its own copies in `data/exports/`
(`YYYY-MM` = the release month). The CLI can write the same three files itself with
`--summary-dir` (default `data/summary`; there `YYYY-MM` is the month of the run's `started_at`);
the workflow points that option at a temporary directory.

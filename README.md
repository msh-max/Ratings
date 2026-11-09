# KSA Restaurants — monthly Google Places data for Saudi Arabia

This project builds a monthly dataset of every restaurant in Saudi Arabia that appears on Google Maps.
For each place it stores the rating, the number of reviews, address, location, type, opening hours,
price level, contact details and (at the default tier) service attributes such as dine-in, delivery
and outdoor seating. Each monthly run adds a snapshot, so you get rating and review-count history.
The output is a SQLite database plus compressed exports shaped for a web UI.

The data comes only from the official **Google Places API (New)**. No web pages are scraped. The
pipeline is built to be correct and cheap: a free discovery pass finds every place ID, an exact
minimum-cost plan fetches the details, an audit measures recall against an independent search, and
validation stops the run if anything is wrong. A GitHub Actions job repeats it every month with no
manual work.

> **Read before using: Google's terms.**
> The Google Maps Platform Terms restrict storing, caching and re-hosting Places content. A
> persistent country-wide table of names and ratings with monthly history is **not expressly
> permitted** by those terms. Get legal review or talk to Google sales before you rely on this
> data, and read **[docs/TERMS.md](docs/TERMS.md)** (quotes, URLs, attribution rules, alternatives).

## Status

The pipeline is complete and tested against a synthetic in-memory Places API (`--fake`; the unit
tests and CI run it end to end). **The live API path has not yet been exercised against Google**:
there was no API key in the environment it was built in. Request bodies, headers and field names were
checked by hand against Google's documentation (2026-11-09), but the first real call is still to come.

After adding a key, the first thing to run is the smoke test: one ~2×2 km cell in central Riyadh,
budget 5 USD, a few dollars at most (usually $0 net of the monthly free calls). It writes to its
own database and export folder so it does not touch the main dataset:

```bash
export GOOGLE_MAPS_API_KEY=...
ksa-restaurants --db data/smoke.sqlite smoke --out data/exports/smoke --summary-dir data/exports/smoke
```

It prints the cost and the one-screen summary. A field name the live API rejects shows up as an
HTTP 400 `INVALID_ARGUMENT` error; remove the field with `exclude_fields` in a `--config` JSON file
(see [docs/RUNBOOK.md](docs/RUNBOOK.md)), no code change needed.

## How it works

```
 Phase 1  DISCOVER (free)        Phase 2  FETCH (paid)            Phase 3  AUDIT          Phase 4  VALIDATE / EXPORT
 ───────────────────────         ─────────────────────            ──────────────          ─────────────────────────
 Text Search, IDs-only mask      exact min-cost plan:             Nearby Search on        hard checks (exit 3)
 adaptive quadtree per type  ──► Text Search pages (full mask) ─► sampled leaf cells ───► exports + summary.json
 split a cell while it returns   + Place Details for leftovers    recall vs. the DB       + report.md
 60 results → complete ID set    budget guard before 1st call     (exit 3 if < 0.97)      month-over-month deltas
            │                               │
            └──────── `estimate` prints the exact paid cost here, for free
```

1. **Discover.** The country is covered by 1° seed cells (cells over sea or other countries are
   dropped using a bundled Saudi boundary). For each cell and each place type, Text Search runs
   with the IDs-only field mask, which is free. Text Search returns at most 60 results per query.
   A cell that returns 60 results (or still has a `nextPageToken` after the third page) is split
   into four and searched again, down to about 220 m (`min_cell_deg` 0.002°). A cell that returns
   fewer than 60 is complete. A cell still saturated at the minimum size is kept as a
   `saturated_leaf`, fetched anyway and reported as a coverage risk. Every cell is saved in SQLite as soon as it is processed.
   From the second month on, discovery starts from last month's leaf cells (warm start).
2. **Fetch.** From the discovered ID sets the pipeline builds a fetch plan by greedy weighted set
   cover: re-run Text Search with the full field mask where a page returns enough new places,
   and use Place Details for the rest. The plan is priced before anything is spent. If it exceeds
   `--max-budget-usd` (default 1000) the run stops with exit code 2 and no paid call is made.
   Every discovered ID ends the run with data or an explicit error row.
3. **Audit.** 100 populated leaf cells (by default) are checked with Nearby Search over a broad
   list of food types. Recall is the share of in-scope places found there that also have data from
   this run. Below `--min-recall` (default 0.97) the run fails with exit code 3.
4. **Validate, export, report.** Hard checks on ratings, counts, IDs and duplicates; exports to
   `data/exports/`; a summary with month-over-month changes.

## Quick start

Requirements: Python 3.11 or newer (CI uses 3.12), a Google Cloud project with billing.

1. In the [Google Cloud Console](https://console.cloud.google.com/): select or create a project,
   attach a billing account, and enable **Places API (New)** (APIs & Services → Library).
2. Create an API key (APIs & Services → Credentials → Create credentials → API key). Restrict it:
   *API restrictions* → only **Places API (New)**. GitHub-hosted runners have no fixed IP, so an IP
   restriction will not work for the monthly job. Also set a billing budget alert and, if you want
   a hard ceiling, a daily quota cap (Google Maps Platform → Quotas).
3. Install and run:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export GOOGLE_MAPS_API_KEY=...        # never commit it

ksa-restaurants --db data/smoke.sqlite smoke --out data/exports/smoke --summary-dir data/exports/smoke
ksa-restaurants --run-id 2026-12-02T0100 discover    # whole country, free (IDs-only mask)
ksa-restaurants --run-id 2026-12-02T0100 estimate    # exact paid cost of this run, free
ksa-restaurants --run-id 2026-12-02T0100 run         # all phases; reuses the discovery above
```

`discover` and `estimate` are free. Use them to learn the exact monthly cost before you pay
anything. The run id defaults to the current UTC time (`YYYY-MM-DDTHHMM`) for `discover`, `run`
and `smoke` (`smoke-YYYY-MM-DDTHHMM` for `smoke`); the other commands default to the most recently
started run in the database. Pass the same `--run-id` to `discover`, `estimate` and `run` so that
`run` reuses that discovery, or to resume an interrupted run.

**Option placement.** Common options go *before* the subcommand, subcommand options *after* it:

```bash
ksa-restaurants --db data/ksa_restaurants.sqlite --types-preset food_and_drink --max-budget-usd 1500 run --audit-cells 200 --out data/exports
```

- Common options (`ksa-restaurants --help`): `--db PATH` (default `data/ksa_restaurants.sqlite`),
  `--bbox lat_lo,lng_lo,lat_hi,lng_hi` (default whole Saudi Arabia),
  `--types-preset restaurants|food_and_drink`, `--type` (repeatable; overrides the preset),
  `--tier enterprise|atmosphere` (default `atmosphere`), `--language en|ar`, `--qps` (default 9),
  `--concurrency` (default 8), `--max-budget-usd` (default 1000), `--fake`, `--run-id`,
  `--cold-start`, `--include-reviews`, `--config PATH` (JSON file overriding config fields: field
  lists, `exclude_fields`, prices, `seed_cell_deg`, `min_cell_deg`, ...), `-v`/`-vv`.
- Subcommands: `discover`, `estimate`, `fetch`, `audit`, `export`, `report`, `run`, `smoke`.
  - `--audit-cells N` (default 100, 0 disables the audit) and `--min-recall X` (default 0.97) on
    `estimate`, `audit`, `run`, `smoke`. `export` also takes `--min-recall`.
  - `--out DIR` (default `data/exports`), `--all-countries`, `--summary-dir DIR` (default
    `data/summary`; writes `latest.json`, `YYYY-MM.json`, `latest.md`) on `export`, `run`, `smoke`.
- `smoke` = `run` on a fixed ~2×2 km bbox in central Riyadh (`24.682,46.675,24.700,46.695`) with a
  default budget of 5 USD (an explicit `--max-budget-usd` wins).

### Fake mode

`--fake` replaces the Google client with an in-memory synthetic world. No key, no network, no cost.
It runs the full pipeline, which is useful for development and is what CI runs:

```bash
ksa-restaurants --fake --db /tmp/fake.sqlite run --out /tmp/fake-exports --summary-dir /tmp/fake-summary
```

## Monthly automation (GitHub Actions)

`.github/workflows/monthly-refresh.yml` runs at 01:00 UTC on the 2nd of every month.

Setup, once:

1. **Default branch.** GitHub runs scheduled workflows only from the repository's default branch.
   The branch that holds this code must be the default branch, or be merged into it.
2. Add the repository secret **`GOOGLE_MAPS_API_KEY`** (Settings → Secrets and variables →
   Actions → New repository secret).
3. Make sure Actions are enabled (Settings → Actions → General) and that workflows may write
   (the workflow requests `contents: write` itself).
4. Optional: run it once by hand with `dry_run` ticked (Actions → Monthly refresh → Run workflow).
   That runs `discover` + `estimate` only and shows the cost in the job summary.

What a run does:

- Restores the previous database from the latest `data-*` release (none on the first run, which is fine).
- Runs `ksa-restaurants <common options> --run-id <YYYY-MM-DDTHHMM> run --audit-cells N --out data/exports
  --summary-dir <temp dir>` with the key from the secret. (The dry run runs `discover`, then
  `estimate --audit-cells N`, so the figure includes the audit.)
- Always uploads `data/` as a workflow artifact, kept 30 days. It lets you resume a failed run.
- On success, publishes the GitHub Release **`data-YYYY-MM`** with `ksa_restaurants.sqlite.gz`,
  all files from `data/exports/` (`places_latest.csv.gz`, `places_latest.jsonl.gz`,
  `snapshots.csv.gz`, `frontend_feed.json.gz`, `summary.json`, `report.md`).
- Commits `data/summary/latest.json`, `data/summary/YYYY-MM.json` and `data/summary/latest.md`
  (copies of `data/exports/summary.json` and `report.md`; `YYYY-MM` is the release month) to the
  default branch as `github-actions[bot]`.

Manual inputs: `bbox`, `types_preset`, `tier`, `max_budget_usd`, `audit_cells`, `dry_run`, and
`run_id` (to resume a failed run; see [docs/RUNBOOK.md](docs/RUNBOOK.md)). Only one refresh runs
at a time. The job timeout is 5 hours.

### Consuming the data from a web UI

- List releases (`GET https://api.github.com/repos/<owner>/<repo>/releases`) and take the newest
  one whose tag starts with `data-`. Do not rely on "latest release" if the repo has other releases.
- Download `frontend_feed.json.gz` for map/list views (a compact array: place_id, name, lat, lng,
  rating, user_rating_count, primary_type, city, region, business_status, price_level,
  google_maps_uri). Use `places_latest.jsonl.gz` or `.csv.gz` for detail pages and
  `snapshots.csv.gz` for history charts.
- Asset URL pattern: `https://github.com/<owner>/<repo>/releases/download/data-YYYY-MM/<file>`.
  A private repository needs an authenticated request.
- `data/summary/latest.json` on the default branch is a small file for headline numbers.
- Before showing anything, read [docs/TERMS.md](docs/TERMS.md): attribution is required, Places
  content must not be shown on a non-Google map, and storage is restricted.

Column definitions: [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md).

## Cost

Phase 1 is free. Phase 2 is the main cost. Prices verified 2026-11-09 on the
[pricing page](https://developers.google.com/maps/billing-and-pricing/pricing) (USD per 1,000 calls,
up to 100k calls/month):

| SKU | Text / Nearby Search | Place Details | Free calls / month |
|---|---|---|---|
| IDs only | 0 (Text Search only) | 0 | unlimited |
| Pro | 32 | 17 | 5,000 |
| Enterprise | 35 | 20 | 1,000 |
| Enterprise + Atmosphere | 40 | 25 | 1,000 |

Rough monthly totals, net of free calls (assumptions in [docs/COSTS.md](docs/COSTS.md)):

| Saudi places | Tier `enterprise` | Tier `atmosphere` (default) |
|---|---|---|
| 100,000 | ≈ $230–300 | ≈ $265–345 |
| 150,000 | ≈ $370–480 | ≈ $430–550 |
| 250,000 | ≈ $660–830 | ≈ $760–960 |

Credible estimates put Saudi food-and-drink places on Google Maps at about 100k–200k. Run
`ksa-restaurants estimate` for the exact figure; it costs nothing.

## Data correctness

- **Coverage.** A cell is accepted only when Text Search returns fewer than 60 results for it, so
  nothing in it was cut off. Cells still saturated at the minimum size (~220 m) are fetched anyway
  and counted in the summary as a known coverage risk.
- **No duplicates.** Everything is keyed by Google place ID. A place found in several cells or
  types is stored once and gets one snapshot per run. Exports are checked for duplicates.
- **Completeness of the fetch.** Every discovered ID ends the run with data or a `fetch_errors`
  row (e.g. place removed). This is a hard check.
- **Validation.** Rating is null or 1.0–5.0; review count is null or an integer ≥ 0; IDs are
  non-empty. Failures stop the run. Soft issues (a rating with zero reviews) are counted.
- **Independent audit.** Nearby Search on sampled cells measures recall; below 0.97 the run fails.
  It also reports how much of the food scene the chosen preset leaves out.
- **Month-over-month.** The summary reports new, missing and rating-changed places versus the
  previous run, and warns if the total moves by more than 15 %. Places not seen are never deleted;
  `last_seen_run_id` tells you when a place was last present.
- **Resumable.** Every call result is persisted. A crashed run resumes with the same `--run-id`
  without repeating finished work or creating duplicate snapshots.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error: a rejected key, billing disabled, API not enabled, a field the API rejects, discovery not finished before `estimate`/`fetch`/`audit`, or any unexpected exception. Resume with the same `--run-id`. |
| 2 | Budget abort: the estimate exceeds `--max-budget-usd`. No paid call was made. |
| 3 | Audit recall below `--min-recall`, or a hard validation failure. |

Run statuses in the `runs` table: `running` (in progress, or crashed and resumable),
`completed`, `audit_failed`, `validation_failed`, `budget_aborted`.

## Repository layout

```
ksa_restaurants/            Python package (CLI `ksa-restaurants`)
  cli.py                    command line (click)
  pipeline.py               phase orchestration, estimate, budget guard
  discover.py               Phase 1 quadtree          fetch_plan.py  minimum-cost plan (set cover)
  fetch.py                  Phase 2 + final sweep     audit.py       Phase 3 Nearby Search audit
  validate.py, summary.py, export.py                  Phase 4
  places_client.py          Places API (New) HTTP client, retries, rate limit
  fake.py                   FakePlacesClient + synthetic world (tests, --fake)
  config.py, cost.py        types, field masks, prices; SKU detection, free-tier netting
  db.py, runs.py, parse.py  SQLite schema, run lifecycle, Place JSON -> row
  geo.py                    seed grid, cell ids, boundary pruning, distances
  data/saudi_boundary.geojson  simplified Natural Earth boundary (public domain)
tests/                      pytest suite (runs against FakePlacesClient)
data/                       working directory: SQLite DB (data/*.sqlite*) and data/exports/ are git-ignored
  summary/                  monthly summaries committed by the workflow (latest.json, YYYY-MM.json, latest.md)
docs/                       DATA_DICTIONARY, RUNBOOK, COSTS, TERMS
.github/workflows/          ci.yml, monthly-refresh.yml
```

Attribution: Data © Google — Powered by Google. Any UI that shows this data must display the
Google Maps attribution (see [docs/TERMS.md](docs/TERMS.md)).

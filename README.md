# Riyadh Restaurants — monthly Google Places data at $0

This project builds a monthly dataset of the restaurants in **Riyadh** that appear on Google Maps,
and by default it **costs nothing**: every call stays below the free monthly allowance Google gives
each Places API SKU. For each place it stores the rating, the number of reviews, address, location,
type, opening hours, price level, contact details and (when fetched through an Atmosphere
instrument) service attributes such as dine-in, delivery and outdoor seating. Each monthly run adds
a snapshot, so you get rating and review-count history. The output is a SQLite database plus
compressed exports shaped for a web UI.

The data comes only from the official **Google Places API (New)**. No web pages are scraped. A free
discovery pass finds every place ID in the area, a planner fetches as much data as the free caps
allow (oldest data first), an audit measures discovery recall against an independent search, and
validation stops the run if anything is wrong. A GitHub Actions job repeats it every month with no
manual work.

The original country-wide, paid behaviour is still available with `--area saudi --allow-paid`
(see [Paid mode](#paid-mode-opt-in)).

> **Read before using: Google's terms.**
> The Google Maps Platform Terms restrict storing, caching and re-hosting Places content. A
> persistent table of names and ratings with monthly history is **not expressly permitted** by
> those terms. Get legal review or talk to Google sales before you rely on this data, and read
> **[docs/TERMS.md](docs/TERMS.md)** (quotes, URLs, attribution rules, alternatives).

## Status

The pipeline is complete and tested against a synthetic in-memory Places API (`--fake`; the unit
tests and CI run it end to end). **The live API path has not yet been exercised against Google**:
there was no API key in the environment it was built in. Request bodies, headers, field names and
the free caps were checked by hand against Google's documentation (2026-11-09), but the first real
call is still to come.

After adding a key, the first thing to run is the smoke test: one ~2×2 km cell in central Riyadh,
in free mode, a few dozen free calls. It writes to its own database and export folder so it does
not touch the main dataset:

```bash
export GOOGLE_MAPS_API_KEY=...
ksa-restaurants --db data/smoke.sqlite smoke --out data/exports/smoke --summary-dir data/exports/smoke
```

Then check the Cloud billing report (see [The three musts for $0](#the-three-musts-for-0)). A field
name the live API rejects shows up as an HTTP 400 `INVALID_ARGUMENT` error; remove the field with
`exclude_fields` in a `--config` JSON file (see [docs/RUNBOOK.md](docs/RUNBOOK.md)).

## Why $0 is possible

Every Places API (New) SKU has its own **free usage cap per calendar month** (verified 2026-11-09
on the [pricing page](https://developers.google.com/maps/billing-and-pricing/pricing)). Caps are
per SKU, per calendar month, per billing account:

| SKU | Text Search | Nearby Search | Place Details |
|---|---|---|---|
| IDs Only | **unlimited** (discovery) | — | unlimited |
| Essentials | — | — | 10,000 |
| Pro | 5,000 | 5,000 (used by the audit) | 5,000 |
| Enterprise | 1,000 | 1,000 | 1,000 |
| Enterprise + Atmosphere | 1,000 | 1,000 | 1,000 |

`rating` and `userRatingCount` need an Enterprise or Enterprise + Atmosphere field mask, so the free
capacity for data is 2,000 Text Search pages + 2,000 Nearby Search calls + 2,000 Place Details
calls per month. The pipeline spreads the fetch over six **instruments**, each with its own cap:

```
I1 text_search/atmosphere   I2 text_search/enterprise
I3 nearby_search/atmosphere I4 nearby_search/enterprise
I5 place_details/atmosphere I6 place_details/enterprise
```

and never uses more than **floor(cap × 0.9)** calls of any SKU per month (the 0.9 margin is
`--free-cap-margin`), counted **month to date over all runs** in the database (smoke runs and
dry runs included).

**Capacity.** A search call returns up to 20 places, so the theoretical ceiling is
2,000 × 20 + 2,000 × 20 + 2,000 ≈ 82,000 places (≈ 74,000 at the 0.9 margin). Real calls return
fewer new places (a typical leaf page holds about 9; Nearby results overlap), so expect **up to
≈ 40,000 places per month, realistically 25,000–35,000**. Riyadh is estimated at 15,000–35,000
restaurants, which fits in most months.

**When the caps bind**, nothing is bought: the IDs that could not be fetched are reported as
`unfetched`, their previous data (if any) is **carried forward** in the frontend feed with its
`data_run_id` / `data_as_of`, and the next month's planner refreshes the oldest data first
(never-fetched IDs, then IDs not refreshed for two or more runs), so everything rotates within a
few months. Discovery (Phase 1, IDs only) is free and unlimited, so the **list of places is always
complete** — only the paid-tier detail fetch is capped.

### The three musts for $0

1. **A dedicated Google Cloud project** for this pipeline. Free caps are per billing account and
   SKU; usage by anything else (another app, a colleague's test) is invisible to the pipeline and
   eats the caps.
2. **A Cloud Billing budget alert at $1** (Billing → Budgets & alerts) on that billing account, so
   any charge at all is noticed the same day. Check the billing report after the first run.
3. **Never run the paid mode by accident.** Free mode is the default everywhere; only
   `--allow-paid` (CLI) or `allow_paid` (workflow input) disables the caps.

Google requires a **billing account attached to the project** even though nothing is charged within
the caps.

## How it works

```
 Phase 1  DISCOVER (free)       Phase 2  FETCH (free caps)           Phase 3  AUDIT         Phase 4  VALIDATE / EXPORT
 ───────────────────────        ──────────────────────────           ──────────────         ─────────────────────────
 Text Search, IDs-only mask     Round T  Text Search pages (I1, I2)  Nearby Search Pro on   hard checks (exit 3)
 adaptive quadtree per type ──► Round N  Nearby Search    (I3, I4) ► sampled leaf cells ──► exports + summary.json
 split while a cell returns     Round D  Place Details    (I5, I6)   discovery recall       + report.md (coverage)
 60 results → complete IDs      rest = unfetched (never bought)      (exit 3 if < 0.97)     carried-forward feed
            │                               │
            └──── `estimate` prints planned calls per instrument and "$0.00" here
```

1. **Discover.** The area (Riyadh by default: lat 24.40–25.10, lng 46.35–47.00, the metro with
   Diriyah and the airport, not Al Kharj) is covered by seed cells. For each cell and each place
   type, Text Search runs with the IDs-only field mask, which is free and unlimited. Text Search
   returns at most 60 results per query; a cell that returns 60 (or still has a `nextPageToken`
   after the third page) is split into four and searched again, down to about 220 m. A cell that
   returns fewer than 60 is complete. Every cell is saved in SQLite as soon as it is processed.
   From the second month on, discovery starts from last month's leaf cells (warm start).
2. **Fetch.** Every discovered ID gets a weight: 3 if it never had data, 2 if its data is two or
   more runs old, 1 otherwise. Then, within the remaining free calls of the month:
   - **Round T (exact):** Text Search pages with the full field mask for the leaf cells with the
     best weighted gain per page (≥ 1), Atmosphere instrument first. Persisted before any call.
   - **Round N (adaptive):** Nearby Search (20 nearest places of the configured types,
     `rankPreference` DISTANCE) on the leaf cells that still have uncovered IDs, in batches;
     the circle is computed from our own cell rectangle, never from place coordinates.
   - **Round D (exact):** Place Details for the remaining IDs, stalest first.
   - Whatever is still uncovered is `unfetched` for this run and is never fetched by spending.
   A runtime guard checks the month-to-date usage before **every** metered call and refuses the
   call (`FreeCapExhausted`) if it would pass floor(cap × margin); the job is marked `skipped_cap`.
3. **Audit.** Up to 100 populated leaf cells are checked with Nearby Search (Pro SKU, 5,000 free).
   `recall` = share of in-scope places found there that Phase 1 also discovered (the coverage
   metric); `fetched_recall` = share that also has data this run. Below `--min-recall` (default
   0.97, discovery recall) the run fails with exit code 3.
4. **Validate, export, report.** Hard checks on ratings, counts, IDs and duplicates; every
   discovered ID must have data, an error row or be counted as `unfetched`. Exports to
   `data/exports/`; a summary with the coverage line, free-cap usage and month-over-month changes.

## Quick start

Requirements: Python 3.11 or newer (CI uses 3.12), a **dedicated** Google Cloud project with a
billing account attached.

1. In the [Google Cloud Console](https://console.cloud.google.com/): create a project for this
   pipeline only, attach a billing account, enable **Places API (New)** (APIs & Services →
   Library), and create a budget alert at $1 (Billing → Budgets & alerts).
2. Create an API key (APIs & Services → Credentials → Create credentials → API key). Restrict it:
   *API restrictions* → only **Places API (New)**. GitHub-hosted runners have no fixed IP, so an IP
   restriction will not work for the monthly job.
3. Install and run:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export GOOGLE_MAPS_API_KEY=...        # never commit it

ksa-restaurants --db data/smoke.sqlite smoke --out data/exports/smoke --summary-dir data/exports/smoke
ksa-restaurants --run-id 2026-12-02T0100 discover    # Riyadh, free and unlimited (IDs-only mask)
ksa-restaurants --run-id 2026-12-02T0100 estimate    # planned calls per instrument, $0.00
ksa-restaurants --run-id 2026-12-02T0100 run         # all phases; reuses the discovery above
```

The run id defaults to the current UTC time (`YYYY-MM-DDTHHMM`) for `discover`, `run` and `smoke`
(`smoke-YYYY-MM-DDTHHMM` for `smoke`); the other commands default to the most recently started run
in the database. Pass the same `--run-id` to `discover`, `estimate` and `run` so that `run` reuses
that discovery, or to resume an interrupted run.

**Option placement.** Common options go *before* the subcommand, subcommand options *after* it:

```bash
ksa-restaurants --db data/ksa_restaurants.sqlite --tier enterprise --free-cap-margin 0.8 run --audit-cells 50 --out data/exports --city Riyadh
```

- Common options (`ksa-restaurants --help`):
  - `--area riyadh|saudi` (default `riyadh`); `--bbox lat_lo,lng_lo,lat_hi,lng_hi` overrides it.
  - `--allow-paid`: paid mode (see below). Without it, free mode.
  - `--tier auto|enterprise|atmosphere` (default `auto`: fill the Atmosphere instrument of each
    endpoint first, then the Enterprise one; `enterprise` uses only I2/I4/I6, `atmosphere` only
    I1/I3/I5; in paid mode `auto` means `atmosphere`).
  - `--free-cap-margin X` (default 0.9): use at most floor(cap × X) calls per SKU per month.
  - `--max-budget-usd` (default 1000): paid mode only; ignored in free mode.
  - `--db PATH` (default `data/ksa_restaurants.sqlite`), `--types-preset restaurants|food_and_drink`,
    `--type` (repeatable; overrides the preset), `--language en|ar`, `--qps` (default 9),
    `--concurrency` (default 8), `--fake`, `--run-id`, `--cold-start`, `--include-reviews`,
    `--config PATH` (JSON file overriding config fields: `free_caps`, field lists,
    `exclude_fields`, prices, `seed_cell_deg`, `min_cell_deg`, ...), `-v`/`-vv`.
- Subcommands: `discover`, `estimate`, `fetch`, `audit`, `export`, `report`, `run`, `smoke`.
  - `--audit-cells N` (default 100, 0 disables the audit) and `--min-recall X` (default 0.97) on
    `estimate`, `audit`, `run`, `smoke`. `export` also takes `--min-recall`.
  - `--out DIR` (default `data/exports`), `--all-countries`, `--summary-dir DIR` (default
    `data/summary`) and `--city NAME` on `export`, `run`, `smoke`. `--city` keeps only places whose
    `city` (the addressComponents locality) matches NAME, case-insensitively — for callers who want
    strictly the Riyadh locality rather than the metro bbox. Default off.
- `smoke` = `run` on a fixed ~2×2 km bbox in central Riyadh (`24.682,46.675,24.700,46.695`). In
  free mode it uses a few dozen free calls; in paid mode its default budget is 5 USD.
- A run keeps the mode it was started in: continuing a paid run needs `--allow-paid`, and a free
  run cannot be continued with `--allow-paid` (start a new run id instead).

### Fake mode

`--fake` replaces the Google client with an in-memory synthetic world centred on Riyadh. No key, no
network, no cost. It runs the full pipeline, which is useful for development and is what CI runs:

```bash
ksa-restaurants --fake --db /tmp/fake.sqlite run --out /tmp/fake-exports --summary-dir /tmp/fake-summary
```

### Paid mode (opt-in)

`--allow-paid` restores the original behaviour: no free caps, the exact minimum-cost planner
(Text Search pages vs. Place Details) and the budget guard (`--max-budget-usd`, exit 2 before any
paid call if the estimate exceeds it). Country-wide:

```bash
ksa-restaurants --area saudi --allow-paid --max-budget-usd 1000 --run-id 2026-12-02T0200 run
```

Costs for the whole country are in [docs/COSTS.md](docs/COSTS.md) (≈ $265–960 per month depending
on size and tier). Free-cap usage is still reported in the summary.

## Monthly automation (GitHub Actions)

`.github/workflows/monthly-refresh.yml` runs at 01:00 UTC on the 2nd of every month, in free mode
for Riyadh.

Setup, once:

1. **Default branch.** GitHub runs scheduled workflows only from the repository's default branch.
   The branch that holds this code must be the default branch, or be merged into it.
2. Add the repository secret **`GOOGLE_MAPS_API_KEY`** (Settings → Secrets and variables →
   Actions → New repository secret), from the dedicated project.
3. Make sure Actions are enabled (Settings → Actions → General) and that workflows may write
   (the workflow requests `contents: write` itself).
4. Optional: run it once by hand with `dry_run` ticked (Actions → Monthly refresh → Run workflow).
   That runs `discover` + `estimate` only (free) and shows the plan in the job summary.

What a run does:

- Restores the database: the newest workflow artifact from the current month if there is one (so
  calls of failed or dry runs this month still count toward the caps), otherwise the latest
  `data-*` release (none on the first run, which is fine).
- Runs `ksa-restaurants <common options> --run-id <YYYY-MM-DDTHHMM> run --audit-cells N --out data/exports
  --summary-dir <temp dir>` with the key from the secret.
- Always uploads `data/` as a workflow artifact, kept 30 days (resume aid and month-to-date record).
- On success, publishes the GitHub Release **`data-YYYY-MM`** with `ksa_restaurants.sqlite.gz` and
  all files from `data/exports/` (`places_latest.csv.gz`, `places_latest.jsonl.gz`,
  `snapshots.csv.gz`, `frontend_feed.json.gz`, `summary.json`, `report.md`).
- Commits `data/summary/latest.json`, `data/summary/YYYY-MM.json` and `data/summary/latest.md` to
  the default branch as `github-actions[bot]`.

Manual inputs: `area` (riyadh|saudi, default riyadh), `types_preset`, `tier` (auto|enterprise|
atmosphere, default auto), `allow_paid` (default false), `max_budget_usd` (only with
`allow_paid`), `audit_cells`, `dry_run`, and `run_id` (to resume a failed run; see
[docs/RUNBOOK.md](docs/RUNBOOK.md)). Only one refresh runs at a time. The job timeout is 5 hours.

### Consuming the data from a web UI

- List releases (`GET https://api.github.com/repos/<owner>/<repo>/releases`) and take the newest
  one whose tag starts with `data-`. Do not rely on "latest release" if the repo has other releases.
- Download `frontend_feed.json.gz` for map/list views: every place this run discovered or fetched
  (place_id, name, lat, lng, rating, user_rating_count, primary_type, city, region,
  business_status, price_level, google_maps_uri, **data_run_id**, **data_as_of**). When the free
  caps did not allow a refresh, the place keeps its older data and `data_run_id` / `data_as_of`
  say how old it is — show that in the UI. Use `places_latest.jsonl.gz` or `.csv.gz` for detail
  pages and `snapshots.csv.gz` for history charts.
- Asset URL pattern: `https://github.com/<owner>/<repo>/releases/download/data-YYYY-MM/<file>`.
  A private repository needs an authenticated request.
- `data/summary/latest.json` on the default branch is a small file for headline numbers
  (`coverage`, `free_mode`, totals).
- Before showing anything, read [docs/TERMS.md](docs/TERMS.md): attribution is required, Places
  content must not be shown on a non-Google map, and storage is restricted.

Column definitions: [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md).

## Data correctness

- **Coverage of the list.** Discovery is free and unlimited and is never cut short by the caps. A
  cell is accepted only when Text Search returns fewer than 60 results for it, so nothing in it was
  cut off. Cells still saturated at the minimum size (~220 m) are reported as a known risk.
- **Coverage of the data.** `report.md` and the one-screen summary open with the coverage line:
  discovered IDs, fetched this run, carried forward with older data, never fetched. With default
  caps the synthetic Riyadh world (≈ 2,700 places) is fetched completely.
- **No duplicates.** Everything is keyed by Google place ID; one snapshot per place per run.
- **Validation.** Rating is null or 1.0–5.0; review count is null or an integer ≥ 0; IDs are
  non-empty; every discovered ID has data, a `fetch_errors` row, or is counted in `unfetched`.
  `unfetched > 0` is a soft warning in free mode and a hard failure in paid mode.
- **Independent audit.** Nearby Search on sampled cells measures discovery recall (exit 3 below
  0.97) and reports `fetched_recall` and how much of the food scene the preset leaves out.
- **Month-over-month.** New, missing (by discovery) and rating-changed places versus the previous
  run; a warning if the total moves by more than 15 %. Places are never deleted.
- **Resumable.** Every call result is persisted. A crashed run resumes with the same `--run-id`
  without repeating finished work; calls already made count toward the month's caps.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. In free mode also when the caps left IDs unfetched (soft warning; see the coverage line). |
| 1 | Error: a rejected key, billing disabled, API not enabled, a field the API rejects, discovery not finished before `estimate`/`fetch`/`audit`, a free/paid mode mismatch on resume, or any unexpected exception. Resume with the same `--run-id`. |
| 2 | Budget abort (paid mode): the estimate exceeds `--max-budget-usd`. No paid call was made. |
| 3 | Audit discovery recall below `--min-recall`, or a hard validation failure. |

Run statuses in the `runs` table: `running` (in progress, or crashed and resumable),
`completed`, `audit_failed`, `validation_failed`, `budget_aborted`.

## Repository layout

```
ksa_restaurants/            Python package (CLI `ksa-restaurants`)
  cli.py                    command line (click)
  pipeline.py               phase orchestration, estimate (free and paid), budget guard
  discover.py               Phase 1 quadtree          fetch_plan.py  Rounds T/N/D, staleness; paid set cover
  fetch.py                  Phase 2 executors         audit.py       Phase 3 Nearby Search audit
  validate.py, summary.py, export.py                  Phase 4
  context.py                metered API calls and the free-cap guard
  places_client.py          Places API (New) HTTP client, retries, rate limit
  fake.py                   FakePlacesClient + synthetic world (tests, --fake)
  config.py, cost.py        areas, types, masks, prices, free caps; SKU detection, instruments
  db.py, runs.py, parse.py  SQLite schema + migration, run lifecycle and month-to-date usage, Place JSON -> row
  geo.py                    seed grid, cell ids, boundary pruning, distances (our cells only)
  data/saudi_boundary.geojson  simplified Natural Earth boundary (public domain)
tests/                      pytest suite (runs against FakePlacesClient)
data/                       working directory: SQLite DB (data/*.sqlite*) and data/exports/ are git-ignored
  summary/                  monthly summaries committed by the workflow (latest.json, YYYY-MM.json, latest.md)
docs/                       DATA_DICTIONARY, RUNBOOK, COSTS, TERMS
.github/workflows/          ci.yml, monthly-refresh.yml
```

Attribution: Data © Google — Powered by Google. Any UI that shows this data must display the
Google Maps attribution (see [docs/TERMS.md](docs/TERMS.md)).

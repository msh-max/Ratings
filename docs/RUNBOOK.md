# Runbook

Operating the monthly refresh. The default is **free mode for Riyadh**: every metered call stays
below the free monthly cap of its SKU, so a run costs $0. Exit codes: `0` success (in free mode
also when the caps left IDs unfetched), `1` error (API/key/billing problem, a rejected field, a
free/paid mode mismatch on resume, or an unexpected exception), `2` budget abort (paid mode only),
`3` audit (discovery recall) or validation failure.

Run statuses (`SELECT run_id, status, notes FROM runs`): `running` (in progress, or crashed and
resumable), `completed`, `audit_failed`, `validation_failed`, `budget_aborted` (the reason is in
`notes`). Warm start and month-over-month comparison use the latest earlier run with the same bbox
whose status is `completed`, `audit_failed` or `validation_failed`.

Option placement: common options (`--db`, `--run-id`, `--area`, `--bbox`, `--allow-paid`, `--tier`,
`--free-cap-margin`, `--types-preset`, `--max-budget-usd`, `--qps`, `--config`, `--fake`, ...) go
**before** the subcommand; `--audit-cells`, `--min-recall`, `--out`, `--all-countries`,
`--summary-dir`, `--city` go **after** it.

## After the first real run: check the Cloud billing report

The pipeline can only count the calls it made itself. After the first real run (and the smoke
test), open Google Cloud Console → Billing → Reports for the billing account, filter on the
dedicated project and the Places API SKUs, and check that the cost is $0.00 and that the usage per
SKU matches the `free_mode.mtd_usage` figures in `summary.json`. A budget alert at $1 (Billing →
Budgets & alerts) must exist on the billing account. If anything was charged: stop the workflow
(disable it under Actions), find the SKU in the report, and compare it with the summary. A
mismatch usually means something else uses the project or billing account.

## Before anything: default branch

GitHub runs scheduled workflows only from the repository's **default branch**. If the code lives
on another branch, the cron never fires. Make that branch the default (Settings → General →
Default branch) or merge it into the default branch. The workflow commits `data/summary/*` to the
default branch, whatever it is called.

## Resume a failed run

Every processed cell, every fetch job and every snapshot is written to SQLite as it completes.
Re-running with the **same run id** continues where it stopped. Finished cells and jobs are not
called again and no duplicate snapshots are created. Paid calls already made are not repeated.

**Locally:**

```bash
ksa-restaurants --run-id 2026-12-02T0100 run      # same id as the failed run, same --db
```

A resumed run uses the configuration stored with it (area, bbox, types, tier, language, field
masks, prices); `--area`, `--bbox`, `--types-preset`, `--tier` etc. given on resume are ignored. Only
the operational settings apply: `--qps`, `--concurrency`, `--max-budget-usd`, `--audit-cells`,
`--min-recall`, `--free-cap-margin` and `free_caps` from `--config`. A run also keeps its mode:
resuming a paid run needs `--allow-paid`, and a free run cannot be resumed with `--allow-paid`
(exit 1 with a message; start a new run id instead).

In free mode the Round T plan (Text Search jobs) is persisted before the first paid-tier call and
resumed as is; Rounds N and D are re-planned from the database, and every call already made counts
toward this month's caps (`api_calls` is written as soon as a call returns), so a resume repeats
no finished job and never passes a cap.

**In GitHub Actions:**

1. Open the failed run and note the run id printed by `ksa-restaurants` (format `YYYY-MM-DDTHHMM`).
   It is also in the artifact's database (`SELECT run_id, status FROM runs`).
2. Actions → Monthly refresh → Run workflow. Set `run_id` to that id and the other inputs to what the
   failed run used (the scheduled run uses the defaults).
3. The workflow looks through the 10 newest `ksa-data-*` artifacts (kept 30 days), restores the
   newest one whose database contains that run id, and resumes. If none does, it warns and starts that
   run id from scratch on top of the last release database (correct, but Phase 2 calls are made
   again; in free mode the calls of the lost attempt are then also missing from the month-to-date
   count, so lower `--free-cap-margin` for the rest of that month).

A new (non-resume) run also restores the newest artifact created in the current UTC month, if any,
so that the calls of earlier failed or dry runs this month count toward the free caps.

If the artifacts have expired, start a new run instead (leave `run_id` empty).

A run cut off by the 5-hour job timeout is resumed the same way.

## Free caps reached (`unfetched` > 0)

Not an error: exit code 0, `Validation: OK (soft: unfetched=N ...)`. The month's free calls of
the paid-tier SKUs were not enough to refresh every discovered place. Nothing was bought.

Read the **coverage** line at the top of `report.md` / the one-screen summary:

```
Coverage : 2,130 of 2,733 discovered IDs fetched this run (77.9%); 0 carried forward with older data; 603 never fetched; 0 errors
```

- *fetched this run*: fresh data this month.
- *carried forward*: discovered this month, not refreshed; the database and the frontend feed keep
  the older data, and the feed's `data_run_id` / `data_as_of` show its age.
- *never fetched*: discovered but no data yet (new places, or a first month that did not fit).
  These have weight 3 and are first in line next month.

`summary.json` → `free_mode` shows `caps`, `usable`, `mtd_usage` and `remaining` per SKU, and
`instruments` shows calls and places per instrument. Typical causes and fixes:

| Cause | What to do |
|---|---|
| The area has more places than ≈ 25k–35k per month | Nothing; the stalest data is refreshed first and everything rotates over a few months. |
| Another run this month (a smoke test, a re-run) used part of the caps | Nothing; next month starts with full caps. |
| `food_and_drink` preset | Expected to rotate; switch back to `restaurants` if full monthly coverage matters more. |
| `--tier atmosphere` or `--tier enterprise` | Each uses only half of the free capacity; `auto` (default) uses both. |

To rehearse this without a key, lower the caps with a config file and run the fake pipeline (a
separate database, so the main one's month-to-date usage is not touched):

```bash
echo '{"free_caps": {"text_search/atmosphere": 40, "text_search/enterprise": 40, "nearby_search/atmosphere": 40, "nearby_search/enterprise": 40, "place_details/atmosphere": 40, "place_details/enterprise": 40}}' > lowcaps.json
ksa-restaurants --fake --config lowcaps.json --db data/lowcaps.sqlite run --out data/exports/lowcaps --summary-dir data/exports/lowcaps
```

A metered call refused by the runtime guard (`FreeCapExhausted`; only possible if something else
spent calls between planning and execution, e.g. two runs at once) marks its job `skipped_cap` in
`fetch_jobs`; the run continues with the next instrument.

## Changing the free-cap margin

The pipeline uses at most floor(cap × margin) calls per SKU per month; the default margin 0.9
leaves 100 of each 1,000-call cap for calls it cannot see (a smoke test run against another database
file, a manual test with the same key). Lower it when something else must share the project for a
while, e.g. for the rest of the month:

```bash
ksa-restaurants --free-cap-margin 0.5 --run-id 2026-12-02T0100 run
```

The margin is an operational setting: it also applies when resuming a run. For the monthly
workflow, add `--free-cap-margin X` to the `common=(...)` array in the "Run pipeline" step. Raising
it above 0.9 is not recommended. The caps themselves (`free_caps`) can be changed with a
`--config` JSON file if Google changes them; check the pricing page first.

## Budget abort (exit 2, paid mode only)

The estimate exceeded `--max-budget-usd` (default 1000). **No paid call was made.** Phase 1
(free) is complete and saved.

1. Read the estimate in the log or job summary: text jobs, pages, Details jobs, gross and net USD.
2. Decide:
   - The figure is expected (e.g. more places, or `food_and_drink`): re-run with a higher
     `max_budget_usd` and the same `run_id`. Discovery is reused, so only Phase 2 onwards runs.
   - The figure is not expected: compare it with last month's `data/summary/latest.json`. A large
     jump in discovered IDs can come from changed types, a cold start, or Google-side changes.
3. To lower the cost: use `tier=enterprise`, a narrower preset, `area=riyadh` — or drop
   `allow_paid` and use the free mode.

The scheduled run is always free mode; `max_budget_usd` only matters for manual runs with
`allow_paid` ticked.

## Audit failure (exit 3)

Exit 3 means either audit recall fell below `--min-recall` (default 0.97) or a hard validation check
failed. The log and `report.md` say which. The workflow publishes no release and commits no summary,
but the full `data/` is in the run's artifact.

**What recall means.** For each sampled leaf cell, Nearby Search lists up to 20 places nearest the
cell centre from a broad list of food types. A found place is *in scope* when its country is SA and
its types intersect the configured types. Recall = in-scope places that Phase 1 of this run
discovered ÷ in-scope places found: it measures **discovery** coverage, which the free caps never
limit. Low recall means Text Search discovery missed places that Nearby Search sees.
`fetched_recall` (in-scope places with a snapshot this run) is reported too; in free mode it is
below 1 when the caps left IDs unfetched, which is not a failure. In free mode the audit uses the
Nearby Search Pro SKU (5,000 free calls per month) and is limited to what is left of it.

How to investigate (open the artifact's database):

```sql
SELECT cell_id, type, found, in_scope, matched, recall, fetched, fetched_recall
FROM audit_cells WHERE run_id = :run ORDER BY recall ASC LIMIT 20;

SELECT count(*) FROM cells WHERE run_id = :run AND status = 'saturated_leaf';
SELECT code, count(*) FROM fetch_errors WHERE run_id = :run GROUP BY code;
```

| Symptom | Likely cause | What to do |
|---|---|---|
| Misses concentrated in a few cells | Cells saturated at minimum size (food courts, malls) | Accept as known risk, or lower `min_cell_deg` in config. |
| Misses spread everywhere | Google index differences between Text Search and Nearby Search, or a warm-start problem | Re-run with `--cold-start` (Phase 1 is free). |
| Missed places have types not in the preset but still count as in scope | A place with several types found by Nearby but not returned for the searched `includedType` | Add the type with `--type`, or switch to `food_and_drink`. |
| Recall slightly under 0.97 with a small sample | Sampling noise (100 cells × ≤ 20 places) | Re-audit with more cells: `ksa-restaurants --run-id <id> audit --audit-cells 300`. |
| Validation failure | Unexpected API values, or a discovered ID with no data that is not counted as `unfetched` | Read the named check in the log. Resume the run; if it persists, it is a bug to fix in code. |

`out_of_scope_share` is not a failure. It tells you what share of Saudi food places the preset does
not cover (for example cafes and bakeries with the `restaurants` preset).

After investigation you can finish the same run locally with a different threshold and publish by
hand (see "Local run"). `export` re-evaluates the run status with its own `--min-recall`:

```bash
ksa-restaurants --run-id 2026-12-02T0100 audit --min-recall 0.95
ksa-restaurants --run-id 2026-12-02T0100 export --min-recall 0.95
ksa-restaurants --run-id 2026-12-02T0100 report
```

Do not lower the threshold without understanding the misses.

## A field the live API rejects (exit 1, HTTP 400 INVALID_ARGUMENT)

The field masks are built from config lists, so a field name Google rejects can be dropped without a
code change. Put the names (without the `places.` prefix) in a JSON file and pass it with `--config`:

```bash
echo '{"exclude_fields": ["reviewSummary"]}' > ksa-config.json
ksa-restaurants --config ksa-config.json --db data/smoke.sqlite smoke --out data/exports/smoke --summary-dir data/exports/smoke
```

The same file can override `fields_pro`, `fields_enterprise`, `fields_atmosphere`, `types`,
`text_queries`, `seed_cell_deg`, `min_cell_deg` and `pricing` (see `ksa_restaurants/config.py`). A
run stores its config, so a resumed run keeps the config it started with: start a new run id after
changing it. For the monthly workflow, commit the file and add `--config ksa-config.json` to the
`common=(...)` array in the "Run pipeline" step.

## Raising the quota

Default Google quota is believed to be 600 requests per minute per method. The client defaults to
9 requests per second and 8 concurrent requests, just under that.

1. Google Cloud Console → Google Maps Platform → Quotas → Places API (New). Request a higher
   per-minute limit for Text Search, Place Details and Nearby Search.
2. After approval, pass `--qps` (and if useful `--concurrency`) before the subcommand, e.g.
   `ksa-restaurants --qps 15 --concurrency 12 run`. For the monthly workflow, add `--qps N` to the
   `common=(...)` array in the "Run pipeline" step.

Do not raise `--qps` above the granted quota: the client retries 429 responses with backoff, but
the run gets slower, not faster.

A daily quota cap is also a good hard spending limit independent of this project.

## Changing types, tier or language

| Change | How | Effect on cost | Other effects |
|---|---|---|---|
| `food_and_drink` preset | `types_preset` input / `--types-preset` | Phase 1 (free) takes longer: 14 types instead of 4. Phase 2 grows with the number of unique places, roughly proportionally. | Count jumps; the month-over-month 15 % warning will fire once. |
| Custom types | `--type X` (repeatable; overrides preset) | As above, per unique place. | Types without previous leaves start from the seed grid. |
| `auto` tier (default) | `tier` input / `--tier auto` | Free mode: uses both the Atmosphere and the Enterprise caps (Atmosphere first). Paid mode: same as `atmosphere`. | Places fetched through an Enterprise instrument have no `atmosphere_json`, `editorial_summary`, `review_summary`; `fetched_via` says which. |
| `enterprise` tier | `tier` input / `--tier enterprise` | Free mode: only I2/I4/I6, half the free capacity. Paid: Text Search $35 instead of $40 per 1,000 pages; Details $20 instead of $25. | Atmosphere columns are not fetched. |
| `atmosphere` tier | `tier` input / `--tier atmosphere` | Free mode: only I1/I3/I5, half the free capacity. | All fields. |
| `--include-reviews` | CLI only | Added to Atmosphere masks only, so no SKU change; check `estimate`. | Much larger `raw_json` and database. |
| Arabic names | `--language ar` (CLI; not a workflow input) | None per call. | Names and labels come back in Arabic and overwrite English values in `places`. Do not alternate languages month to month. |

Always run `discover` + `estimate` (or the workflow with `dry_run`) after a change. It makes no
paid-tier call and prints the plan per instrument (and, in paid mode, the exact figure).

## Reading the month-over-month section

`report.md` compares this run with the previous one:

A place is *present* in a run when Phase 1 discovered it or the run fetched it (free mode can leave
discovered places unfetched, so presence, not a snapshot, is what counts here).

- **New:** places present now and not in the previous run. Includes newly opened places and places
  newly reachable by search.
- **Missing:** places present in the previous run and not now. They are not deleted; their
  `last_seen_run_id` stays at the old run. Many are closed or merged on Google Maps. A missing place
  that reappears next month was a search fluctuation.
- **Rating changed:** places whose rating differs from their previous snapshot.
- **Warning > 15 %:** the total moved more than 15 %. Normal month-to-month change should be a few
  percent. Check for: a config change (types, bbox), many `saturated_leaf` cells, many
  `fetch_errors`, or a Google-side change. Do not publish the data to users before you understand it.

## Rotating the API key

1. Cloud Console → APIs & Services → Credentials → Create credentials → API key. Restrict it to
   **Places API (New)**.
2. GitHub → Settings → Secrets and variables → Actions → update `GOOGLE_MAPS_API_KEY`.
3. Run the workflow with `dry_run` to check the new key (discovery calls are free).
4. Delete the old key in the Cloud Console.

If the key leaked, delete it first, then do steps 1–3. The pipeline never logs the key and the
workflow masks it, but check any logs you shared.

## Local run on a laptop

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
export GOOGLE_MAPS_API_KEY=...

ksa-restaurants --run-id 2026-12-02T0100 discover                   # free, unlimited
ksa-restaurants --run-id 2026-12-02T0100 estimate --audit-cells 100  # plan per instrument, $0.00
ksa-restaurants --run-id 2026-12-02T0100 run --audit-cells 100       # free mode: within the caps
```

To continue the published history instead of starting with an empty database, first download
`ksa_restaurants.sqlite.gz` from the latest `data-YYYY-MM` release (GitHub release page or
`gh release download`) and unpack it to `data/ksa_restaurants.sqlite` with `gunzip -c`.

Keep the laptop awake. A local run counts toward the same monthly caps as the workflow (same
project), but the workflow's database does not see it: lower the workflow's margin for that
month, or publish the local database as described below so the next run starts from it. If it stops, re-run with
the same `--run-id`.

A local run is not published automatically. To publish it, create a release named `data-YYYY-MM`
yourself (GitHub release page or `gh release create`) with `ksa_restaurants.sqlite.gz` (the
database compressed with `gzip -c`) and every file in `data/exports/`. The next scheduled run will
then start from that database.

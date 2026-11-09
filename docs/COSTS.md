# Costs

Short version: **run `ksa-restaurants discover` then `ksa-restaurants estimate`.** Both are free and
print the exact paid cost of the month before you spend anything. The numbers below are planning
estimates only.

## Prices

Verified 2026-11-09 at <https://developers.google.com/maps/billing-and-pricing/pricing>.
USD per 1,000 calls. Prices live in the config and can be edited without code changes.

| SKU | Text Search | Nearby Search | Place Details | Free calls / month (per SKU) |
|---|---|---|---|---|
| IDs only | 0, unlimited | — (no IDs-only SKU) | 0, unlimited | unlimited |
| Essentials | — | — | 5 | 10,000 |
| Pro | 32 | 32 | 17 | 5,000 |
| Enterprise | 35 | 35 | 20 | 1,000 |
| Enterprise + Atmosphere | 40 | 40 | 25 | 1,000 |

Above 100,000 calls per month per SKU the price drops (Text Search Enterprise 28, Enterprise +
Atmosphere 32, for 100k–500k). The estimator applies these volume tiers. The scenarios below stay
under 100k calls per SKU.

Billing rules that drive the design:

- The highest-tier field in the field mask sets the SKU for the whole call.
- Each page of a paginated Text Search is one call. A page holds up to 20 places; a query gets at
  most 60 (3 pages).
- Free calls are counted per SKU per month across the whole billing account. If other projects use
  the same SKUs, less of the free allowance is left for this one.

## Formula

**Phase 1 — discover: $0.** Text Search with the IDs-only mask (`places.id,nextPageToken`) is free
and unlimited.

**Phase 2 — fetch.** Phase 1 gives, for each populated leaf (cell, type), its ID set and result
count `n`. Fetching every leaf with Text Search would cost

    pages_naive = Σ over leaves of ceil(n / 20)

But a place is often in several leaves (cell edges, several types). The fetch planner (greedy
weighted set cover) only takes a leaf as a Text Search job while it is cheaper than fetching its
not-yet-covered places one by one:

    take leaf if  pages × P_text  ≤  new_places × P_details

At the Enterprise tier that means at least 1.75 new places per page (35/20); at Atmosphere 1.6
(40/25). Everything left over is a Place Details call. So:

    cost_phase2 = pages_planned × P_text  +  details_calls × P_details
    pages_planned ≤ pages_naive

| Tier (`--tier`) | P_text (per call) | P_details (per call) |
|---|---|---|
| `enterprise` | $0.035 | $0.020 |
| `atmosphere` (default) | $0.040 | $0.025 |

A final sweep fetches with Place Details any discovered ID that Text Search pages did not return
(page ordering is not perfectly stable). It is usually small and is part of the Details count.

**Phase 3 — audit.** One Nearby Search call per audited cell (max 20 results, no pagination). The
mask `places.id,places.types,places.addressComponents` is the Pro SKU ($32 per 1,000). Default 100
cells = 100 calls = **$3.20 gross, $0 net** if the 5,000 free Pro calls are not used elsewhere.

**Phase 4 — validate, export, report: $0.** No API calls.

## Worked estimates

Assumptions (stated, not measured):

- Saudi places in scope: 100k, 150k or 250k. Credible estimates put Saudi food-and-drink places on
  Google Maps at about 100k–200k; 250k is a ceiling that needs cafes and bakeries
  (`--types-preset food_and_drink`). xMap counted 132k restaurants, cafes and bakeries in 2023;
  DataSaudi reported 102k active accommodation and food enterprises in 2024.
- Paid Text Search pages: **1.4 to 1.8 pages per 20 places**, including edge overlap and partly
  filled last pages. So pages = places / 20 × 1.4 (low) or × 1.8 (high).
- Place Details fallback: 2 % of places.
- Audit: 100 cells.
- Free tier: 1,000 free calls on the Text Search SKU, 1,000 on the Place Details SKU, 5,000 on Nearby
  Search Pro. No other usage on the billing account.

### Tier `atmosphere` (default)

| Places | Text pages | Text gross | Details calls | Details gross | Audit gross | **Total gross** | **Total net of free tier** |
|---|---|---|---|---|---|---|---|
| 100,000 | 7,000 – 9,000 | $280 – $360 | 2,000 | $50 | $3.20 | $333 – $413 | **$265 – $345** |
| 150,000 | 10,500 – 13,500 | $420 – $540 | 3,000 | $75 | $3.20 | $498 – $618 | **$430 – $550** |
| 250,000 | 17,500 – 22,500 | $700 – $900 | 5,000 | $125 | $3.20 | $828 – $1,028 | **$760 – $960** |

### Tier `enterprise`

| Places | Text pages | Text gross | Details calls | Details gross | Audit gross | **Total gross** | **Total net of free tier** |
|---|---|---|---|---|---|---|---|
| 100,000 | 7,000 – 9,000 | $245 – $315 | 2,000 | $40 | $3.20 | $288 – $358 | **$230 – $300** |
| 150,000 | 10,500 – 13,500 | $368 – $473 | 3,000 | $60 | $3.20 | $431 – $536 | **$373 – $478** |
| 250,000 | 17,500 – 22,500 | $613 – $788 | 5,000 | $100 | $3.20 | $716 – $891 | **$658 – $833** |

Net = gross − 1,000 × P_text − 1,000 × P_details − audit (covered by the Pro free calls).

Notes:

- The default budget guard is $1,000. The 250k / Atmosphere high case is close to it. Expect a budget
  abort (exit 2) there; raise `max_budget_usd` after checking the estimate.
- Atmosphere costs about 14 % more than Enterprise for Text Search and 25 % more for Details. It adds
  dine-in, takeout, delivery, reservations, meal times, payment and parking options, and summaries.
- Fetching every place with Place Details instead would cost 100k × $0.025 = $2,500 (Atmosphere) for
  100k places. Text Search pages carry up to 20 places for one call, which is why the planner prefers
  them.
- Month to month the cost stays about the same: the whole country is re-fetched each month to get
  fresh ratings and review counts.

## Exact figure, for free

```bash
ksa-restaurants discover          # free: IDs-only Text Search
ksa-restaurants estimate          # prints text jobs, pages, Details jobs, gross and net USD
```

`estimate` prices the actual fetch plan for this month's discovered IDs at the configured tier.
`run` and `fetch` abort with exit code 2 before the first paid call if that figure exceeds
`--max-budget-usd`. In GitHub Actions, run the workflow with `dry_run` ticked to get the same
figure in the job summary.

## Runtime

The default quota is believed to be 600 requests per minute per method. The client defaults to
9 requests/s (540 per minute). Rough estimates at that rate:

| Phase | Calls (restaurants preset, 100k–150k places) | Time at 540/min |
|---|---|---|
| 1 Discover, cold start | ≈ 20,000 – 40,000 (assumption: ≈ 3 calls per leaf including split cells) | 35 – 75 min |
| 1 Discover, warm start | lower: starts from last month's leaves | 30 – 60 min |
| 2 Fetch | 7,000 – 13,500 pages + 2,000 – 3,000 Details | 15 – 30 min |
| 3 Audit | 100 | < 1 min |
| Total | | ≈ 1 – 2 hours |

With `food_and_drink` (14 types) Phase 1 has roughly 2–3 times more calls; a full run may take
3–4 hours, close to the workflow's 5-hour limit. If it times out, resume with the same `run_id`
(see RUNBOOK.md), or raise the quota and pass `--qps`. Phase 1 call counts depend on how places
cluster; the `cells` table records `discover_calls` per cell after the first run.

# Archived: Google Places pipeline

This folder holds the project's earlier approach: a monthly, resumable enumeration of
restaurants across Saudi Arabia through the Google Places API (New), stored in SQLite and
exported to CSV/Parquet. It was moved here unchanged when the project pivoted (November 2026).

## Why it was set aside

- **Google's terms.** The Places API terms restrict caching and storing Places content
  (beyond place IDs) and require Google attribution wherever it is shown, which conflicts with
  publishing a committed, monthly dataset and a ranking built on it.
- **Billing.** The API needs a Google Cloud project with billing enabled; a full country
  refresh was estimated at roughly $265–960 per month (see `docs/COSTS.md`).

The project now builds a consensus ranking of Riyadh restaurants from published sources
(guides, awards, editorial lists, community threads) instead; see the root `README.md`.

## It still works

Nothing here was modified, and its scheduled workflow was removed so nothing runs on its own.
To use it, work from inside this folder:

```bash
cd archive/google-places-pipeline
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q              # 77 tests, offline
ksa-restaurants --help           # needs GOOGLE_MAPS_API_KEY for real runs
```

`README.md` and `docs/` in this folder describe it in full (runbook, costs, terms, data dictionary).

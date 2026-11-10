# Riyadh Best Restaurants

A consensus ranking of the best restaurants in Riyadh, built from what independent published
sources recommend — restaurant guides, awards, editorial best-of lists, official destination pages
and community threads — refreshed every month by a scheduled Claude session and shown on a static
website.

No Google data, no scraping of rating platforms, no paid APIs. We store facts only (place names,
which source recommended them, at what tier, when, with the URL) and never copy article text.

**Current edition (2026-11):** 546 restaurants ranked from 85 source lists by
29 publishers (MICHELIN Guide, Gault&Millau, MENA's 50 Best, La Liste, FACT, What's On,
World Culinary Awards, List Magazine, Visit Saudi, Arabic media and more); 23 cafés, coffee
shops, bakeries and dessert shops are set aside for a future cafés ranking.

## What the ranking is and is not

It is a **judgment-based consensus of published sources**: a restaurant ranks high because many
independent guides, awards and editors recommend it, and because the strongest of them (a
MICHELIN distinction, Gault&Millau toques, a MENA's 50 Best place) carry more weight. The
weights are editorial choices, written down in [`ranking/sources.yaml`](ranking/sources.yaml) so
anyone can check or change them. It is **not a crowd rating**: there are no star averages, no
review counts and no data from Google Maps, TripAdvisor or delivery apps, and it says nothing
about a single visit. It also only knows what was published — a newer or less-covered restaurant
can be excellent and still rank low, and the ranking inherits the choices and blind spots of the
sources it reads.

## How the ranking works

1. **Extract.** Each month every source is read and saved as one JSON file per list in
   `data/sources/<YYYY-MM>/` (names, tier such as "1 star", "#12", "winner", position, area, cuisine
   when the source states them).
2. **Merge.** Spellings of the same restaurant are merged (case, accents, "Al-"/"El"/"ال",
   fuzzy matching, plus manual `ranking/aliases.yaml`). Cafés, coffee shops, bakeries and
   dessert shops listed in `ranking/exclusions.yaml` are set aside (kept under `excluded`).
3. **Score.** Each mention earns a weight by source type and tier — e.g. MICHELIN star 10–14,
   Bib Gourmand 7, MENA's 50 Best top 10 → 9, award winner 6, editorial top-3 → 4, official page 2,
   community recommendation 1–2 — times a recency factor (1.0 within 12 months, down to 0.2).
   A list counts once per place; several lists from one publisher add at most 1.5× its best one.
4. **Rank.** By score, then number of sources, then most recent mention. Confidence is *strong*
   with ≥ 3 independent publishers, *medium* with 2, *single* with 1.

Full procedure and weights: [`ranking/METHODOLOGY.md`](ranking/METHODOLOGY.md);
weights and the publisher registry: [`ranking/sources.yaml`](ranking/sources.yaml).

## Repository layout

```
README.md
pyproject.toml               package `ranking` (pyyaml, rapidfuzz, httpx, jsonschema; dev: pytest)
ranking/                     Python package
  __main__.py                CLI: build / qa / artifact
  schema.py                  JSON Schemas for extracts and outputs
  load.py                    read + validate data/sources/<month>/*.json
  normalize.py               name keys, de-duplication, place attributes
  scoring.py                 tier parsing, weights, recency, publisher cap
  geocode.py                 Nominatim (OpenStreetMap) with a committed cache
  build.py                   writes the ranking files (scores, card text, awards)
  exclusions.py              cafés / bakeries kept out of the restaurants ranking
  artifact.py                single-file HTML for the Claude Artifact
  qa.py                      quality report
  sources.yaml               source registry and weight schemes
  aliases.yaml               manual name merges / keep-apart pairs
  exclusions.yaml            non-restaurants (brand -> reason) and reviewed café-restaurants
  METHODOLOGY.md             the monthly procedure (scheduled session's reference)
data/sources/<YYYY-MM>/      source extracts (committed)
data/rankings/restaurants/   <YYYY-MM>.json and latest.json (committed)
data/geocode/cache.json      Nominatim cache (committed)
docs/                        static site (GitHub Pages root): index.html, app.js, styles.css, data/
tests/                       unit tests and a small fixture repo (tests/fixtures/repo)
archive/google-places-pipeline/   the previous approach (see below)
.github/workflows/ci.yml     tests + build check on every push / PR
```

## Run it locally

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q

python -m ranking build --month 2026-11 --check --no-geocode   # validate only, writes nothing
python -m ranking build --month 2026-11                        # write outputs, geocode new places
python -m ranking build --month 2026-11 --no-geocode           # write outputs, cached coordinates only
python -m ranking qa --month 2026-11                           # top 30, duplicate suspects, gaps
python -m ranking artifact --month 2026-11 --out build/riyadh-best-restaurants.html   # one-file page

python -m http.server -d docs 8000                             # then open http://localhost:8000
```

`--month` defaults to the latest month under `data/sources/`. `--root PATH` points the CLI at
another checkout (the tests use `tests/fixtures/repo`).

Outputs: `data/rankings/restaurants/<month>.json` and `latest.json` hold the full record
(spec format); `docs/data/restaurants.json` is a slimmer copy for the site (mentions reference
`sources` by `source_id`, null/empty fields omitted). `latest.json` and the site copy are only
replaced when building the newest month.

## Monthly refresh (scheduled Claude session)

Once a month a scheduled Claude session follows [`ranking/METHODOLOGY.md`](ranking/METHODOLOGY.md):
re-read every registered source and search for new ones, write one extract per list, run
`python -m ranking build` and `python -m ranking qa`, fix aliases, then commit `data/`, `docs/data/`,
`ranking/sources.yaml` and `ranking/aliases.yaml` and push to the default branch. The site updates
on its own; CI re-checks the build.

## The website and GitHub Pages

`docs/` is a dependency-free static site (Leaflet 1.9.4 from cdnjs is loaded only when the map is
opened). It shows the ranked list with filters (search, cuisine, area, price, confidence, source
type; sort by score, name or recency), a map, and a sources & methodology page. It reads
`data/restaurants.json` next to the page, or `window.DATA_URL` when that global is set before
`app.js` (for publishing the same page elsewhere, e.g. pointing at the raw `latest.json`).

`python -m ranking artifact` writes the same page as one self-contained HTML fragment with the
data inlined (`window.RANKING_DATA`), published as a Claude Artifact by the monthly session. The
Map tab is hidden there (external map tiles cannot load) and links to the GitHub Pages version.

To publish it: **Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch:
`claude/affectionate-cerf-8mwtq1`, folder `/docs` → Save.** The site appears at
`https://msh-max.github.io/Ratings/` a minute later and refreshes after every monthly push.

## Attribution

- Rankings summarise what the cited publishers published; every mention links to its source.
  Restaurant names, awards and list titles belong to their publishers (MICHELIN Guide,
  MENA's 50 Best Restaurants, Gault&Millau, FACT, Time Out, What's On and others listed in
  `ranking/sources.yaml`). We store no article text.
- Map tiles and geocoding: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright),
  via the OpenStreetMap tile servers and Nominatim (used within their usage policies, with a
  cache and ≤ 1 request per second).

## Earlier approach

The previous Google Places API pipeline (enumerating every restaurant in Saudi Arabia) lives in
[`archive/google-places-pipeline/`](archive/google-places-pipeline/ARCHIVED.md). It still works but
was set aside because of Google's terms on storing Places data and its billing requirement.

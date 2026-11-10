# Monthly procedure — Riyadh restaurant ranking

This is the complete, standalone procedure a fresh Claude session follows once a month (it is the
reference for the scheduled task). It assumes nothing from earlier conversations. Work in the
repository root, on the default branch `claude/affectionate-cerf-8mwtq1`.

**Principles.** We store facts only: place names, which source recommended them, at what tier,
when, and the URL. We never copy article text, never scrape rating platforms (Google Maps,
TripAdvisor, Yelp, Foursquare, Zomato, HungerStation reviews...), never use Google data, never use
paid APIs, and never invent a restaurant, tier, cuisine or area. `null` is always acceptable.

## 0. Set up

```bash
git pull --ff-only
python -m pip install -e ".[dev]"
python -m pytest -q                       # must pass before you start
MONTH=$(date -u +%Y-%m)                   # e.g. 2026-12
mkdir -p data/sources/$MONTH
```

Read `ranking/sources.yaml` (the source registry) and last month's extracts in
`data/sources/<previous month>/` so you know what was covered and which URLs were used.

## 1. Collect sources

1. **Re-read every known source.** For each list in last month's folder and each publisher in
   `ranking/sources.yaml`, open the current edition. If the list moved or a new edition exists
   (e.g. "MICHELIN Guide Riyadh 2027", "MENA's 50 Best 2027"), use the new URL. Keep the same
   `source_id` when it is the same list re-read; use a new `source_id` (with the year) for a new
   edition of an annual award/guide.
2. **Search for new sources** published since last month: award results (FACT Dining Awards,
   Time Out Riyadh Restaurant Awards, World Culinary Awards, Caterer Middle East, Saudi tourism
   awards), new guide editions (MICHELIN, Gault&Millau, La Liste), new best-of lists (Time Out,
   What's On, Condé Nast Traveller, Harper's Bazaar Arabia, Esquire ME, Vogue Arabia, The National,
   Arab News, Saudi Gazette, Destination KSA, Lovin Riyadh, Culture Trip, Lonely Planet, Fodor's,
   FACT, List Magazine, GQ Middle East...), Arabic media (سيدتي، ليالينا، هي، جريدة الرياض، عكاظ),
   official/destination pages (Visit Saudi, Riyadh Season, Diriyah/Bujairi, KAFD, Boulevard City,
   Via Riyadh) and community threads (Reddit r/Riyadh, r/saudiarabia; YouTube creators' own top lists).
   Search in English and Arabic (e.g. "أفضل مطاعم الرياض").
3. **Register new publishers** in `ranking/sources.yaml` (id, name, match strings, type, urls,
   language, notes). Unknown publishers still score through their `source_type` defaults, but
   registering them keeps the publisher cap and display names right. Never delete a publisher.

## 2. Write one extract per list

### Extract format and rules (spec §1, verbatim)

Source extract files — `data/sources/<YYYY-MM>/<source_id>.json`
One file per published list / award edition / thread. `source_id` = kebab-case, unique, stable across
months when the same list is re-read (e.g. `michelin-guide-riyadh-2026`, `mena-50-best-2026`,
`timeout-riyadh-best-restaurants`, `reddit-saudiarabia-best-restaurants-2026-03`).
```json
{
  "source_id": "timeout-riyadh-best-restaurants",
  "source_name": "Time Out Riyadh — The best restaurants in Riyadh",
  "publisher": "Time Out",
  "source_type": "guide | ranking | award | editorial | official | community",
  "language": "en | ar",
  "url": "https://...",
  "published_at": "2026-05-14",          // ISO date of the list/edition; null if undated
  "retrieved_at": "2026-11-10",
  "coverage_note": "ranked list of 50; paywall: no; pages read: 1",
  "entries": [
    {
      "name": "Myazu",                  // as written by the source (Latin script if available)
      "name_ar": "ميازو",               // if the source gives it, else null
      "cuisine": "Japanese",            // as the source describes it, else null
      "area": "KAFD",                   // neighbourhood / district / mall / destination, else null
      "price_band": "$$$$",             // $, $$, $$$, $$$$ when the source states it, else null
      "tier": "1 star",                 // see tier vocabulary below
      "position": 3,                    // position in a ranked list, else null
      "note": "one factual line in our own words (what the source says it is known for)",
      "entry_url": null,                // the entry's own page if any
      "branch": null                    // branch name if the source names a specific branch
    }
  ]
}
```
Tier vocabulary (free text allowed but prefer): guide → "3 stars", "2 stars", "1 star",
"Bib Gourmand", "Selected", "Green Star", "4 toques".."1 toque", "listed"; ranking → "#12" with
position; award → "winner", "finalist", "nominee", "highly commended"; editorial → "listed" or
"#n"; official → "listed"; community → "recommended by N commenters" (position = N).
Rules for extractors: Riyadh only (drop Jeddah/Khobar/AlUla entries; destinations inside Riyadh such
as Diriyah/Bujairi Terrace, KAFD, Boulevard City, Via Riyadh count as Riyadh). Record only what the
source actually says; never invent cuisine/area; `null` is fine. Capture ALL entries of a list, not
the top few. Note paywalls or unreachable pages in `coverage_note` and still save the file with what
you could read. Do not store article text.

Practical notes:

- File name = `data/sources/<YYYY-MM>/<source_id>.json`; the file name must equal `source_id`.
- All top-level keys are required (use `null` when unknown); each entry needs at least `name` and
  `tier`. Validate as you go: `python -m ranking build --month $MONTH --check --no-geocode`.
- YouTube: set `publisher` to `"YouTube: <channel name>"` so each channel counts as its own publisher;
  tier `"creator top list"`.
- `note` is one short factual line in our own words, taken from what the source says. It feeds the
  card text on the site, so do not add opinions the source did not express.
- If a page is paywalled or unreachable, still save the file with what you could read and say so in
  `coverage_note`. Never fill gaps from memory.

## 3. Build

```bash
python -m ranking build --month $MONTH          # geocodes new places via Nominatim (1 req/s, cached)
```

This writes `data/rankings/restaurants/$MONTH.json`, `data/rankings/restaurants/latest.json`,
`docs/data/restaurants.json` (slim copy for the site) and updates `data/geocode/cache.json`.
Geocoding only queries places not yet in the cache, so it is slow only the first time.
Use `--no-geocode` to skip the network (cached coordinates are still used).

Scoring (implemented in `ranking/scoring.py`, weights in `ranking/sources.yaml`):

- guide/MICHELIN: 3 stars 14, 2 stars 12, 1 star 10, Bib Gourmand 7, Selected/listed 5, Green Star +1
- guide/Gault&Millau: 4–5 toques 8, 3 toques 7, 2 toques 6, 1 toque 5, listed 4; La Liste listed 5
- ranking/MENA's 50 Best: 1–10 → 9, 11–50 → 8, 51–100 → 5; special awards 6
- award: winner 6; finalist / nominee / highly commended 3
- editorial: position 1–3 → 4, otherwise 3 (unranked → 3)
- official: 2
- community: recommended by N commenters → 1 (N = 1), 1.5 (N ≥ 2), 2 (N ≥ 5); creator top list 1.5
- recency multiplier from `published_at`: ≤ 12 months 1.0; 12–24 → 0.7; 24–36 → 0.4; older 0.2; undated 0.8
- a source counts once per place (its best tier); all lists of the same publisher together add at most
  1.5 × that publisher's best single contribution
- rank by score, then number of sources, then most recent mention; confidence = strong (≥ 3
  publishers), medium (2), single (1)

## 4. QA checklist

Run `python -m ranking qa --month $MONTH` and work through it:

- [ ] **Errors**: none. Every extract validates; no duplicate `source_id`.
- [ ] **Top 30** looks plausible; for any surprising entry open its mentions and check the extracts.
- [ ] **Duplicate suspects**: for each pair that is the same place, add the spellings to
      `ranking/aliases.yaml` (canonical name → list of other spellings, Latin or Arabic). For pairs that
      were merged but are different places, add them under `_distinct`. Rebuild and re-run QA.
- [ ] **Arabic names**: Arabic-only entries that are a known Latin-named place → add an alias.
- [ ] **Non-Riyadh suspects**: remove entries that are in Jeddah, Khobar, AlUla, etc. from the
      extracts (destinations inside Riyadh — Diriyah/Bujairi, KAFD, Boulevard City, Via Riyadh — stay).
- [ ] **Missing area/cuisine**: fill only if a source states it; otherwise leave null.
- [ ] **Coverage**: each extract's `coverage_note` says whether all entries were captured.
- [ ] `python -m pytest -q` passes and `python -m ranking build --month $MONTH --check --no-geocode` succeeds.
- [ ] Optional: open `docs/index.html` through a local server (`python -m http.server -d docs`) and look.

## 5. Commit and push

```bash
git add data/ docs/data/ ranking/sources.yaml ranking/aliases.yaml
git commit -m "Monthly refresh $MONTH: <n> sources, <n> places"
git push origin claude/affectionate-cerf-8mwtq1
```

Commit only those paths (plus code fixes if you made any, with tests). GitHub Pages serves `docs/`
from the default branch, so the site updates by itself after the push. CI re-runs the tests and the
build check on push.

## 6. Finish with a short summary

Report: number of sources (new ones named), number of places, new entrants to the top 30, big
movers (±10 ranks within the top 50), places that dropped out, sources that were paywalled or
unreachable, and any alias decisions you made.

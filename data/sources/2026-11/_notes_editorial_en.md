# Notes — English-language editorial "best restaurants in Riyadh" lists (2026-11 run)

Retrieved 2026-11-10. Every extract is `source_type: editorial`, `language: en`. Only names, short factual notes in our own words, and cuisine/area as the source states them.

## Extracts written by this pass
| file | entries | published_at | notes |
|---|---|---|---|
| fact-riyadh-100-best-restaurants.json | 101 | 2026-08-14 | FACT "Reservations Radar: 100 of the Best Restaurants in Riyadh"; alphabetical, unranked; read in two passes of a long page |
| whatson-saudi-riyadh-thai-restaurants.json | 6 | 2026-07-14 | unranked |
| list-magazine-riyadh-best-restaurants.json | 9 | 2026-10-30 | numbered 1–9 |
| gq-middle-east-riyadh-power-lunch.json | 7 | 2025-02-13 | unranked |
| gq-middle-east-guide-to-riyadh-2025.json | 1 | 2025-01-15 | city guide; only Najd Village named |
| semafor-guide-to-riyadh-2025.json | 17 | 2025-10-24 | bars/lounges left out |
| outlook-traveller-riyadh-top-places-to-eat.json | 6 | 2025-06-13 | date = "updated" date |
| expatica-best-restaurants-riyadh.json | 5 | 2025-02-03 | date = "last updated" date |
| curlytales-riyadh-best-fine-dining-2024.json | 8 | 2024-09-17 | numbered |
| curlytales-riyadh-new-restaurants-monthly-2024-2025.json | 38 | 2025-03-10 | rolling openings round-up; Jeddah items dropped |
| curlytales-new-restaurant-openings-2026-07.json | 8 | 2026-07-10 | numbered openings round-up |
| curlytales-new-restaurants-saudi-2026-06.json | 8 | 2026-06-19 | numbered; Liza (Diriyah) kept |
| scenenow-eight-new-restaurants-riyadh-2024.json | 8 | 2024-09-20 | unranked openings |
| thisisriyadh-50-must-visit-restaurants-2026.json | 50 | 2026-01-09 | numbered 1–50; local site; no areas |

Duplicates removed: this pass also read FACT new openings 2025 and 2026, What's On (fine dining, Saudi, Middle Eastern, romantic) and List Magazine (new 2025, brunch, seafood, editor's picks, reliable reservation edit). Another extractor had already saved the same lists (`fact-hottest-*`, `whatsonsaudi-*`, `listmag-*`), so this pass's copies were deleted to avoid double counting. Publisher string aligned to "List Magazine".

Numbered listicles carry `position` as the spec asks. For openings round-ups and the This is Riyadh list, the numbers look like listicle order rather than a judged ranking, so the 1–3 position bonus there is weak evidence.

## Checked but could not read (blocked, 403/405/503, or bot checks)
- Time Out Riyadh: all pages were blocked by CloudFront (403/405), r.jina.ai hit a captcha, and Wayback had no snapshot or could not be reached. Search snippets mention the Restaurant Awards 2024/2025 (Myazu Restaurant of the Year 2024; COYA, LPM, Gymkhana, Sumosan, Chi Spacca, Saray and Sadelle's as 2025 category winners via riyadhkey.com) and the guides to best restaurants, family-friendly, Americas, Thai, As Sulimaniyah and under SAR 50. NOTHING from Time Out was saved because the lists could not be checked. Re-try next month.
- Harper's Bazaar Arabia "12 of the Best Restaurants in Riyadh" (403); Esquire ME "10 best restaurants in Riyadh 2023" (403; also out of the 2024–26 window); Destination KSA (403: new restaurants, Saudi, Middle Eastern, top 10 night out, seafood); Wanderlust "Riyadh's best restaurants" (403); Culture Trip (503); Cathay Pacific "Riyadh's top dining spots" (503); Arabian Business best restaurants in Riyadh (403/405); Forbes "Exploring Riyadh and Jeddah" (403).
- Condé Nast Traveller (UK/US/ME), Tatler, Travel + Leisure, NYT, FT, Guardian, BBC: the search tool refuses these domains, so they could not be checked.
- Lonely Planet culinary guide URL returned only a navigation shell, with no restaurant list.

## Searched, no qualifying 2024–2026 Riyadh best-of list found
Vogue Arabia, Elle Arabia, Forbes Middle East, Robb Report, Fodor's, National Geographic Traveller, Bloomberg, Lovin Riyadh, Hospitality News ME, Caterer Middle East (weekly F&B news only), Arab News / Saudi Gazette / The National / Gulf News / Khaleej Times (award coverage and single-venue reviews only; The National's "Eight great restaurants" dates from about 2019).

## Deliberately skipped
- What's On Saudi Arabia Awards 2026 shortlist and winners, FACT Dining Awards, and MENA 50 Best coverage: these are awards, not editorial lists. They are for the award extractors (winners URL: whatsonsaudiarabia.com/2026/09/whats-on-saudi-arabia-awards-2026-here-are-the-winners/).
- Marriott Bonvoy Traveler "Best Dining Experiences in Riyadh": undated hotel-brand content, mostly Marriott venues.
- Valentine's offer round-ups (promotions), the List Magazine "upcoming 2025 openings" sub-list (not yet open), and SEO/aggregator blogs (dunesatlas, houseofsaud, lepetitchef, restaurantsforkings, artandthensome, cuddlynest).

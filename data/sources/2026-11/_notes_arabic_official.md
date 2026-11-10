# Notes — Arabic-language lists and official/destination sources (read 2026-11-10)

## Written (one file per list)
Arabic editorial (`language: ar`, `source_type: editorial`):
- sayidaty-riyadh-international-restaurants-2024-12 — 10 (2024-12-21)
- sayidaty-riyadh-japanese-restaurants-2025 — 10 (2025-04-02)
- sayidaty-riyadh-saudi-national-day-local-food-2024 — 6 (2024-09-23)
- layalina-riyadh-distinctive-dishes-2024 — 6 (2024-12-31)
- layalina-riyadh-luxury-restaurants-2025 — 4 (2025-01-07, updated 2026-06-24)
- layalina-riyadh-family-restaurants-2025 — 6 (2025-01-06)
- hiamag-riyadh-finest-dinner-restaurants-2026 — 8 (2026-04-20)
- hiamag-riyadh-national-day-dinner-2026 — 7 (2026-09-21)
- wego-rahhal-best-restaurants-riyadh-2025 — 12 (2025-09-16)
- Sayidaty "أفضل 10 أماكن لتناول الفطور والسحور في رمضان بالرياض" (node/1807102, 2025-02-28) was
  already saved by another extractor as `sayidaty-ramadan-iftar-suhoor-riyadh-2025.json`; my duplicate was removed.

Official (`source_type: official`, tier "listed"):
- visitsaudi-best-new-restaurants-riyadh — 3 (EN + AR story pages, undated)
- visitsaudi-belafya-dining-guide-riyadh-2026 — 70 (PDF, 2026-08-03; logos read visually + link annotations)
- visitsaudi-riyadh-city-guide-2024 — 134 (PDF "City Guide 2024", modified 2025-03-17; Restaurants chapter)
- visitsaudi-riyadh-metro-guide-2025 — 28 (PDF, 2025-03-17; restaurants only)
- via-riyadh-dine-directory — 6 (living directory)
- kafd-dining-directory — 9 (living directory)

## Name handling
Where the Arabic source gave no Latin name and the brand spelling could not be confirmed, `name` keeps the
Arabic (e.g. ثرى، سوما، لوس بانديدوس، سفن، المملكة سوشيال داينينغ). Latin spellings used from branding or an
official listing: Chotto Matte (شوتو ماتي), Charcoza (شاركوزا), Lusin (لوسين), Yauatcha (ياواتشا), Agio (آجيو/آجو —
Hia's آجو assumed to be Agio), Kayzo (كايزو = Kayzo by Myazu), Sadelle's (ساديلز), Fi Glbak (في قلبك), Tameesa (تميسه),
Al Orjouan (الأرجوان), Bab Touma, Brisket, California Burger.

## Checked but not captured
- Time Out Riyadh Arabic (ar.timeoutriyadh.com: Saudi restaurants, grills, family, budget lists; 2026 award
  nominees) — HTTP 405 to the fetch tool; not reachable. Re-try next month.
- Harper's Bazaar Arabia Arabic (ar.harpersbazaararabia.com: best Riyadh restaurants, luxury guide, outdoor,
  romantic) — HTTP 403; not reachable.
- Sabq (sabq.org "5 مطاعم شهيرة…") — HTTP 403.
- diriyah.sa / bujairi.sa (Bujairi Terrace directory) — HTTP 403. Bujairi Terrace restaurants are partly
  covered by the Visit Saudi Bel'afya guide (Diriyah section).
- riyadhseason.com (Boulevard City / Boulevard World / zones) — JavaScript-only app, no restaurant list readable.
- boulevardworld.sa, thezone.sa, uwalk.sa — proxy CONNECT 502; riyadhfront.com — TLS certificate error;
  roshnfront.com, laysenvalley.com — parked domains; solitaire.sa is an unrelated company (no official
  Solitaire mall directory found); roshn.sa/roshn-front has no restaurant list.
- Visit Saudi /en/riyadh/food-and-drink — 404; /en/riyadh destination page has no named restaurants.
- Out of the 2024–2026 window, not captured: Flyin blog Riyadh restaurants (2023-12-14), Hia "10 مطاعم فاخرة
  للاحتفال بالعام الجديد" (2023-12-28), Wego Rahhal 2023 version (the URL now carries the 2025 list).
- Not found: Almosafer blog Riyadh restaurant list (only a city guide), Okaz/Al Riyadh/Al Watan/Argaam/
  Marie Claire Arabia best-restaurant lists for 2024–2026, Rehlat Arabic list. Breakfast/coffee-only lists
  (Sayidaty Mother's Day breakfast, Hia breakfast lists) skipped as not restaurant rankings.

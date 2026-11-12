# Date check (2026-11-09)

Why: wattan's metadata dates are `dd/mm/yyyy`, and dateutil read them month-first. In our window a swap can stay inside the range (11/10 → Nov 10), so it is not caught as out_of_range.
Fix: non-ISO dates are now parsed day-first (`parse_dt(..., dayfirst=True)`). `collect.py reextract` recomputed `published_at` for every stored item from its saved raw HTML: 89 dates changed and those 89 items left the window. All 4,342 wattan pages that had been dropped as out of range were re-queued for re-fetch with the fixed parser.

## Check 1: against a date in the URL or sitemap
"mismatch" = page date more than 1 day from the reference. "swap" = the dd/mm swap pattern.

| source | items | with reference | match | mismatch | swap | date source |
|---|---|---|---|---|---|---|
| wafa | 3086 | 3086 (URL) | 3086 | 0 | 0 | visible text |
| aljazeera | 3101 | 3101 | 3100 | 1 | 0 | metadata |
| rtarabic | 8332 | 8332 | 8332 | 0 | 0 | metadata |
| alqaheranews | 3621 | 3621 | 3621 | 0 | 0 | metadata |
| alquds | 1996 | 1996 | 1996 | 0 | 0 | metadata |
| sama | 1705 | 1705 | 1705 | 0 | 0 | metadata |
| arab48 | 1311 | 1311 | 1311 | 0 | 0 | metadata/htmldate |
| france24 | 1299 | 1299 | 1299 | 0 | 0 | metadata |
| safa | 1133 | 1133 | 1123 | 10 | 0 | metadata (ref = sitemap lastmod, an edit date) |
| paltoday | 1175 | 1175 | 1170 | 5 | 0 | metadata (ref = sitemap lastmod) |
| pbc | 826 | 826 | 826 | 0 | 0 | metadata |
| aawsat | 2379 | 2379 | 2371 | 8 | 0 | metadata (ref = sitemap) |
| jpost | 3734 | 3734 | 3639 | 95 | 0 | metadata (ref = sitemap lastmod, an edit date; the page metadata is authoritative) |
| makan | 477 | 477 | 459 | 18 | 0 | metadata (ref = sitemap lastmod) |
| haaretz | 945 | 945 | 944 | 1 | 0 | metadata |
| alarabiya | 118 | 118 | 118 | 0 | 0 | metadata |
| alwatanvoice | 781 | 781 | — | 0 against canonical URL | 0 | metadata (the fetch URL's date path is a placeholder; compared with the canonical URL) |

## Check 2: sources with no URL date (IDs rise with time)
Each item's date was compared with the median date of its 14 nearest IDs. "outlier" = more than 2 days off.

| source | items | outliers | swap pattern | note |
|---|---|---|---|---|
| maan | 2320 | 0 | 0 | visible date |
| wattan | 2806 | 1 | 0 | after the day-first fix |
| qudsn | 739 | 0 | 0 | |
| shehab | 628 | 0 | 0 | |
| alwatanvoice | 781 | 0 | 0 | |
| alhaya | 738 | 1 | 0 | 1 htmldate fallback (ID 152422) |
| aa | 42 (so far) | 0 | 0 | |
| alayyam | 155 | 19 | 0 | **dates are htmldate guesses (no metadata); treat alayyam dates as approximate** |

Conclusion: no day/month swaps remain. The only systematic date risk left is alayyam (htmldate). Run this check again after the pull finishes.

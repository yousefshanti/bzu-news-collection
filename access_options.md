# Access options for the 14 missing sources (+ alayyam, wattan)

Window: 2023-10-07 .. 2023-11-08. Tested on 2026-11-09. Every route below was actually queried or fetched, not just read about.
Nothing here is paid; no paid option was used. "Wayback" = Internet Archive, reached through the tablet SOCKS tunnel because `/etc/hosts` on this Mac blocks archive.org.

## Cross-source routes

| Route | What I tested | Result | Cost / terms | Verdict |
|---|---|---|---|---|
| **Common Crawl CC-NEWS** (WARC news archive) | Listed `crawl-data/CC-NEWS/2023/10/warc.paths.gz` (642 files ≈ 1 GB each; 26 files on 2023-10-10). Streamed 250 MB of one 2023-10-10 file and counted target domains. | In ~12k records: dw.com 14, aa.com.tr 6, apnews.com 2 (all language editions). None of the Arab/Israeli outlets appeared. There is no URL index, so using it means downloading every file: ≈ 900 GB for the window. | Free; Common Crawl terms of use (research OK) | **Not worth it.** Coverage of our outlets is near zero and the download cost is huge. |
| **Common Crawl main crawl** (CDX index) | Queried `CC-MAIN-2023-50` (crawled 28 Nov–11 Dec 2023, so it holds pages published in our window that were still online). `CC-MAIN-2023-40` ends before 7 Oct. | israelhayom.com/2023/10/*: 21 URLs. timesofisrael.com: ≥2,860 captures. alarabiya.net: ≥1,394. i24news.tv/ar: ≥2,986. ynet, sky, almayadeen, bbc, dw, aa, reuters: the index server returned 504 on repeated tries. | Free | **Partial.** A sparse sample, superseded by the archived sitemaps below. Kept as a backup route. |
| **Media Cloud** | Read docs/FAQ (search API, CSV export of story URLs). Needs a personal account and API key, which I cannot create for you. | Gives story **URLs + metadata only**, not full text (copyright). Search is over titles. Default quota ~4,000 API calls/week. | Free account for researchers | **Useful for discovery only, and needs your account.** Optional: if you register, I can add it as one more URL source. |
| **Reuters / AP research access** | Searched for official academic programmes. | Reuters content is licensed through LSEG (Refinitiv/Eikon); academic users report a 15-month history limit on the API. Some universities license Reuters feeds through their library. No official AP academic API was found. Third-party "Reuters API" wrappers are unofficial scrapers. | Paid / institutional | **Not used.** Ask the BZU library whether it has an LSEG/Factiva/ProQuest licence; Factiva or ProQuest would cover both wires legally. |
| **Published datasets** | Hugging Face `Walid-Ahmed/arabic_news` (Apache-2.0); Kaggle "Israel-Palestine conflict news", "Ultimate Arabic News", Al Jazeera sets. | None covers our outlets for Oct–Nov 2023. The HF set is an Al Jazeera sample (2020–2024); the Kaggle sets are English headlines or pre-2023. | Various | **Not useful** for the 14 sources. |
| **archive.today** (archive.ph) | Timemap and newest-capture requests for alarabiya and timesofisrael. | Timed out every time (it throttles automated clients). | Free | **Not usable** for automated collection. |
| **Wayback CDX, whole domain** | Prefix queries per domain. | 503/504 on big domains even for a single day, and ~4 h for 3 days of lookups. | Free | **Fallback only**, via narrow prefixes (section path or date in the path). |
| **Wayback copies of the sites' own sitemaps** | Exact-URL CDX queries + fetching the archived sitemap files. | **Best route for blocked sites.** One archived sitemap gives thousands of URLs in one request (details per source below). | Free | **Used.** |

## Browser inspection (Step 2, Claude in Chrome, normal pace; no bulk collection)

Pages opened: homepage + robots.txt + sitemap structure for timesofisrael, israelhayom, alaraby, skynewsarabia, alarabiya and almayadeen.

- **timesofisrael.com:** WordPress/Yoast. `sitemap_index.xml` lists 482 `post-sitemapN.xml` and 347 `liveblog_entry-sitemapN.xml`. The children are fixed ID ranges, so an archived copy from any later date still lists our window.
- **israelhayom.com:** returns "Access Denied" (Akamai) even to a normal browser from this network, so it is a network-level block. Wayback holds 180 of its `post-sitemapN.xml` files.
- **alaraby.co.uk:** opens in the browser. `sitemap.xml` has 416 pages × 2,000 URLs, but every `lastmod` was regenerated in bulk (2024–2026), so dates can't be estimated from it. Bots get a 403.
- **skynewsarabia.com:** opens. Article URLs resolve by numeric ID alone (`/middle-east/1660000` redirects to the slug URL; 1660000 is 8 Oct 2023). The site also answered our scraper this time (2 s), so the earlier timeouts were temporary.
- **alarabiya.net:** opens in the browser. robots.txt lists `archive-sitemap/sitemap.xml` with one sitemap per month (`/archive-sitemap/2023/10/sitemap.xml`). Bots get a Cloudflare challenge.
- **almayadeen.net:** shows a Cloudflare "Just a moment…" challenge even in the browser. I stopped there and did not try to pass it.

Extraction fixes (Step 4) were worked out offline from the saved raw HTML, which shows the same DOM as the browser:
- **aawsat:** the page preloads several articles for infinite scroll, and trafilatura merged them. The body is now `.first-article-node`.
- **rtarabic:** a site-wide "Stories" carousel of current (2026) headlines plus the comments block were being extracted as text; this also caused the false scope matches. The body is now `.main-article__block--intro` + `.main-article__editor-content`.
- **bbcarabic:** the body is inside `<main>`, with an "Author / Role / Published" list in the text. Those lines and the repeated headline are now stripped.
- **i24ar:** the body is the first `<article>`. Many Wayback snapshots are a JavaScript shell ("You have to enable JavaScript"), which was the 26% problem. These are now marked `js_shell` and retried by the other route (live ↔ Wayback).

## Per source

| source | routes tried | worked? | coverage for window | cost/terms | **recommended route** |
|---|---|---|---|---|---|
| ynet | live sitemap API, GDELT, Wayback CDX, CC | **Live API works**: `/api/google_site_map_by_period/<days ago>/1` returns one day's articles (~50–150) | full | free, robots allows | **live per-day API + live fetch (full)** |
| skynewsarabia | live sitemap (recent only), GDELT, CDX, CC (504), browser | **Live ID URLs work** | full | free | **id_range live (full)** |
| aa (Arabic) | live sitemap (recent only), CDX, CC-NEWS (few), ID URLs | ID URLs work, but the ID space is shared by all AA languages (~40k IDs in window) | full, after an Arabic-title filter | free | **id_range live + Arabic filter (full; ~11 h)** |
| alarabiya | live (Cloudflare), browser (opens), CC (≥1.4k), **Wayback copy of `archive-sitemap/2023/10` and `/11`** | Wayback sitemap: 5,903 URLs for Oct 2023 | full URL list; pages via Wayback | free | **Wayback sitemaps → Wayback snapshots, sample 60/day** |
| toi | live (captcha), browser, CC (≥2.9k), **Wayback copies of Yoast child sitemaps** (515 post + 317 live-blog) | yes | full URL list; pages via Wayback | free | **Wayback sitemaps → Wayback snapshots, sample 60/day** |
| israelhayom | live (Akamai, also in browser), CC (21), **Wayback copies of 180 `post-sitemapN`**, per-day CDX on `/YYYY/MM/DD/` | yes | full URL list; pages via Wayback | free | **Wayback sitemaps → snapshots, sample 60/day** |
| almayadeen | live (Cloudflare, also in browser); Wayback `sitemaps/news.xml` (2 copies); RSS/feed snapshots (none archived); `/news` listing pages (~1,600 copies); **Wayback copies of the monthly `sitemaps/all/sitemap-2023-10.xml` (2,716 URLs, up to 30 Oct) and `-2023-11.xml` (827 URLs, up to 9 Nov)** | yes | near-full URL list; pages via Wayback | free | **archived monthly sitemaps → Wayback snapshots, sample 60/day** |
| i24ar | live (works), sitemap not archived, CDX, CC (≥3k) | CDX + live | partial (no full URL list) | free | **CDX/GDELT URLs → live fetch (full for what is found)** |
| bbcarabic | live (works), archived BBC news sitemaps (~25 Arabic URLs per file), GDELT, CDX on `/arabic/articles/` | yes | good | free | **archived news sitemaps + GDELT + CDX → live fetch** |
| dw (Arabic) | live (works), archived `ar/article-sitemap.xml` (1,530 URLs per copy, 2 copies in window), CC-NEWS (few) | yes | good | free | **archived sitemaps + GDELT + CDX → live fetch** |
| alaraby | live (403), browser (opens, but sitemap lastmods are useless), Wayback sitemap pages (409 archived, undated), CDX per section | CDX per section | partial | free | **CDX per section → Wayback, sample 30/day** |
| alqudsalarabi | live (403 on articles); Wayback news sitemap (0 copies in window); `sitemap_index` (one 2022 copy only); `post-sitemap` (10 copies, all 2026, which don't reach back to 2023); RSS `/feed/` (none archived); WordPress `?p=ID` URLs (archived only as redirects, never as 200 pages); GDELT; CDX | CDX + GDELT only | partial | free | **CDX + GDELT → Wayback, sample 30/day. No better free route found.** |
| ap | live (Cloudflare), **Wayback copies of monthly `ap-sitemap-202310.xml` / `202311.xml`** (Nov copy: 15,316 URLs), CC-NEWS (few) | yes | full URL list | free | **Wayback monthly sitemaps → snapshots, sample 30/day (last)** |
| reuters | live (401), Wayback `sitemap-index` (it only pages back from the newest article, so it can't reach 2023), CDX `world/middle-east/`, GDELT, LSEG (paid) | CDX/GDELT | partial | free (LSEG paid: not used) | **CDX + GDELT → snapshots, sample 30/day (last)** |
| alayyam | live (old 2023 URLs are dead), Wayback CDX `ar_page.php` | CDX | thin (Wayback has few captures) | free | **CDX → Wayback; stays partial** |
| wattan | live ID range | yes | full after extending the ID range on both ends | free | **id_range live (extended)** |

## Needs your decision
- **Media Cloud:** register a free account if you want it as an extra URL-discovery source. It gives URLs only, no text.
- **Reuters/AP full text:** ask the BZU library about LSEG, Factiva or ProQuest access. Not used: it is paid or institutional.
- **almayadeen and israelhayom block even a normal browser here.** Wayback is the only free route for them, so they stay sampled.

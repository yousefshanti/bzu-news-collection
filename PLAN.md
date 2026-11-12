# PLAN — Arabic news collection (BZU graduation project)

## Goal
Collect all coverage published 2023-10-07 .. 2023-11-08 (Asia/Jerusalem) on Palestine/Israel from 36 outlets (Palestinian, Arab, international, Israeli) into `articles.db`: full text, dates, authors, agency credits and content type. This step is collection only; there is no translation or classification yet. Spec: `برومبت_سكربت_السحب.md`.

## Current status (updated 2026-11-11 laptop time)
- **2026-11-11 12:52 (tablet clock): the tablet run FINISHED.** `tablet_runner.log` ends `DONE` 06:46. Phase 1 ended 01:40, then reuters+ap, then the report. The tablet did not reboot (uptime 12 days); only Termux `sshd` had died, and the user restarted it. **Tablet DB: 52,670 articles.** The merge to the laptop has NOT been done yet.
  - articles (candidates): ynet 830 (959 URLs, 126 dup_url), toi 1,416 (5,106 URLs; 3,066 sampled out, 285 Wayback 403s), bbcarabic 382 (2,121 URLs; 836 out of range, 839 dup_url), skynewsarabia 1,521 (28k URLs; 23k sampled out), alqudsalarabi 811, alaraby 55, israelhayom 134 (87 Wayback 200s that failed extraction, 241 no_date), i24ar 129 (132 JS shells), dw 304, alarabiya 588 (985 Wayback 404s), reuters 366, ap 107, alayyam 155.
  - toi and bbcarabic discovery both worked (archived sitemaps + GDELT; bbcarabic CDX 94 ok / 8 failed).
  - **CDX holes** (failed after 4 tries + never tried): alaraby 13+153, reuters 15+41, toi 10+24, ynet 10+24, alqudsalarabi 10+24, dw 10+24, i24ar 11+20, bbcarabic 8+0. Fully ok: skynewsarabia, israelhayom, alayyam. "Never tried" rows exist because a pass stops after 5 failures in a row, and all 6 passes ran out on the same first days. A rerun resumes them (`tries` < 4).
- **ynet checked from the laptop (2026-11-11):** 959 is the API's real total, not a cut. `/api/google_site_map_by_period/{AGO}/{N}` returns at most **500** URLs per call (one 37-day call gave exactly 500). Per-day calls are fine under that cap, but the day window drifts by 1–2 days, so `/1` calls alone gave 785 unique URLs. Overlapping `/3` calls over 2023-10-05..11-10 gave **965 unique**, about 20–30 per day for ynetnews.com (English). The expected 50–150 per day probably counts Hebrew ynet.co.il or live-blog updates, which this API does not list. Also, a failed day in `sitemap_template` only logs a warning; it is not recorded.
- **Media Cloud key** goes in `.env` (`MEDIACLOUD_API_KEY=`, chmod 600, laptop only). Never copy it into PLAN.md, reports or logs, and never send `.env` to the tablet.

## Status as of 2026-11-10 20:10 laptop time
- **The collection runs on the TABLET** (Termux, `~/bzu`), detached with `nohup setsid` and `termux-wake-lock`. The laptop can be closed.
  - Restarted 2026-11-10 20:02 laptop time (19:02 on the tablet clock, which is CET) with the CDX-as-supplement code.
  - Runner: `run_tablet.sh` runs `pull` for every source except reuters/ap, then `pull --sources reuters,ap`, then `report`.
  - The tablet reaches Wayback directly (no tunnel). The user's site (`~/channels/server.py`, port 8080) and the crontab are untouched.
- **The laptop is idle.** Its `articles.db` is the 2026-11-09 23:20 snapshot (41,376 items); the tablet had 47,153 items at the restart. `collect.py`, `sources.json` and `test_collect.py` are identical on both. **Do not write to the laptop DB** until the merge below.
- Check from the laptop:
  - `ssh tablet 'cd ~/bzu && cat logs/tablet_runner.log; tail -5 logs/pull.log'`
  - `ssh tablet 'cd ~/bzu && python3 -c "import sqlite3;d=sqlite3.connect(\"articles.db\");print(d.execute(\"select count(*) from articles\").fetchone());[print(r) for r in d.execute(\"select source_id,status,count(*) from candidates where status in (\x27pending\x27,\x27done\x27) group by 1,2\")]"'`
  - Running? `ssh tablet 'pgrep -af "[c]ollect.py"'`. Finished when `logs/tablet_runner.log` ends with `DONE`.
  - CDX holes: `ssh tablet 'cd ~/bzu && python3 -c "import sqlite3;[print(r) for r in sqlite3.connect(\"articles.db\").execute(\"select source_id,status,count(*),max(tries) from cdx_days group by 1,2\")]"'`
- **How discovery works now:** each source first stores the URLs from its primary routes (sitemap, API, ID walk, plus GDELT where those are thin) and fetches them at once. Wayback CDX runs afterwards as a supplement: one light query per prefix per day, every outcome recorded in the `cdx_days` table, failed queries retried in later passes (up to 4 tries each, 6 passes per run, 30 min apart; 5 failures in a row end the pass for that source). `collect.py report` lists the queries that never succeeded.

## Per-source status
| source | route | status | known issues |
|---|---|---|---|
| wafa, pbc, alquds, sama, arab48, safa, paltoday, aljazeera, aawsat, alqaheranews, france24, rtarabic, makan, haaretz, jpost | live sitemaps | done (items in quality_report.md) | aawsat/rtarabic/bbc/i24 text fixed by re-extraction; rtarabic lost ~2.7k false scope matches |
| maan, alhaya, alwatanvoice, qudsn, shehab | live ID walk | done | maan: ID gaps (404) are normal |
| wattan | live ID walk | done (3,448) | day/month bug fixed |
| alayyam | Wayback CDX | partial (155); CDX days are retried | dates are htmldate guesses (see date_check.md) |
| aa | live ID walk + Arabic filter | done (3,243 of 35,736 IDs) | ~85% of IDs are other languages |
| alarabiya | archived monthly sitemaps → Wayback pages | done (588), ≤60/day | |
| almayadeen | archived monthly sitemaps → Wayback pages | done (1,623), ≤60/day | Cloudflare, even in a browser |
| toi | archived Yoast sitemaps + GDELT first, CDX supplement → Wayback | running, ≤60/day | post sitemaps not archived; live-blog ones are. CDX has no narrow prefix (slugs sit at the site root) |
| israelhayom | archived post sitemaps / per-day CDX → Wayback | 105 so far; per-day CDX is the one query shape that answers reliably | Akamai block, even in a browser |
| skynewsarabia | archived rolling sitemap + GDELT first (1,065 URLs), CDX by section + article-ID range as supplement → Wayback | running, ≤60/day | **now blocked live (Radware bot check)**; not bypassed |
| alaraby | GDELT first (only 52 URLs), CDX per section as supplement → Wayback | running, ≤30/day | **weakest source**: no sitemap/API/ID route works; depends on CDX recovering or on Media Cloud |
| alqudsalarabi | GDELT first (1,635 URLs), CDX supplement → Wayback | running, ≤30/day | no better free route; CDX has no narrow prefix |
| ynet | live per-day sitemap API + GDELT first (959 URLs), CDX on `/article/` as supplement | running, full | |
| bbcarabic | archived news sitemaps + GDELT first, CDX on article sections as supplement → live | running, full | |
| dw | archived news sitemaps + GDELT + CDX → live | 304 done; CDX days are retried | |
| i24ar | CDX + GDELT → live | 129 done; CDX days are retried | some Wayback copies are JS shells |
| reuters, ap | GDELT (ap: archived monthly sitemaps) first, CDX supplement → Wayback | **queued last**, ≤30/day | |

## Done
- 2026-11-09 Built `collect.py` (probe/pull/report/reextract) and `sources.json` (36 sources); 9 tests in `test_collect.py`.
- 2026-11-09 Probe → `probe_report.md`. Full pull of 22 live sources.
- 2026-11-09 Access research for the missing sources → `access_options.md` (CC-NEWS, Common Crawl, Media Cloud, LSEG, datasets, archive.today, archived sitemaps, browser inspection).
- 2026-11-09 Extraction fixes (aawsat, rtarabic, bbcarabic, i24ar). Dedup now per source + `cross_source_duplicates` view (151 cross-outlet copies restored). Authors are persons only, with orgs in `author_orgs`.
- 2026-11-09 Day-first date fix + date check → `date_check.md` (no swaps remain).
- 2026-11-09 Re-extraction from raw HTML: 40,206 updated, 89 re-dated out of the window, 3,229 removed as out of scope.
- 2026-11-09 almayadeen: archived monthly sitemaps found. alqudsalarabi: every remaining route tried, none better.
- 2026-11-09 23:20 Collection moved to the tablet and verified running after the SSH disconnect.

- 2026-11-10 Tablet run stopped after 17 h: aa and wattan finished, but six sources sat at zero candidates because discovery waited for Wayback CDX and every CDX query failed (117 × 504, 32 × 503). Fixed: primary routes are stored and fetched first; CDX is a recorded, retried supplement with light queries (`cdx_days` table, 10 tests). Restarted on the tablet.

## Next steps
- [ ] (tablet, automatic) Finish the pull of the remaining sources, then reuters+ap, then the report.
- [ ] When `logs/tablet_runner.log` says DONE: **merge** (see below) on the laptop.
- [ ] On the laptop: `collect.py report`; rerun the date check; update `access_options.md`, `quality_report.md` and this file; send them for review.

## Decisions made
- **No block bypass.** No CAPTCHA/Cloudflare/Radware solving and no browser-based bulk collection. The browser (Claude in Chrome; not ego lite) was used for inspection only.
- **Wayback via the tablet.** On the laptop it goes through `ssh -D 1080 tablet` because `/etc/hosts` blocks archive.org; on the tablet it is direct. Port 8080 on the tablet is never touched.
- **Sampling for Wayback-only sources**, random within each day: up to 60/day (ynet, toi, israelhayom, i24ar, almayadeen, alarabiya, skynewsarabia), up to 30/day (bbcarabic, alaraby, alqudsalarabi, dw, aa), reuters and ap last at 30/day. Where a live route gives full coverage, take it instead (ynet, aa, bbc, dw, i24 are live, so full).
- **Cross-outlet duplicates are kept** (syndication is data); dedup is within a source only.
- **Authors are persons only**; organizations go to `author_orgs`.
- **Non-ISO dates are parsed day-first.**
- **CDX is a supplement, never a gate.** A source is fetched from its sitemap/API/ID/GDELT URLs first. CDX queries are narrow (article path, section, date path or article-ID range), with no collapse and no server-side filters.
- **Sampling quota counts earlier rounds**: URLs added later by CDX only fill what is left of a day's 60 or 30.
- **No paid sources** without approval.
- newspaper4k is not installed on the tablet (it is only a fallback extractor; trafilatura + site body selectors are used).

## Open items
- **You:** ask the BZU library about Factiva/ProQuest/LSEG access for Reuters/AP full text.
- **Media Cloud:** approved. The user registers and puts the key in `.env`. Create a free account and test URL discovery for the missing sources (alaraby first, then alqudsalarabi, toi, skynewsarabia, bbcarabic, reuters, ap); report what it covers.
- alaraby had no log lines in the first tablet run: the old code logged nothing until discovery ended and wrote no state row for a source without a sitemap, so its position inside the stuck CDX stage was invisible. The new code logs discovery at once and records every CDX query.

## How to resume
- **Tablet** (restart after a reboot or a kill; resumable, it skips finished work):
  `ssh tablet 'cd ~/bzu && nohup setsid bash run_tablet.sh >/dev/null 2>&1 < /dev/null &'`
  Stop: `ssh tablet 'pkill -f run_tablet.sh; pkill -f collect.py'`
- **Merge back to the laptop** (only after DONE, or after stopping the tablet):
  ```
  cd ~/Desktop/bzu/BZU_project
  cp articles.db articles.laptop-backup.db
  rsync -az tablet:bzu/articles.db ./articles.db      # the tablet DB is a superset of the laptop snapshot
  rsync -az tablet:bzu/raw/ ./raw/                    # new raw HTML (new article ids)
  mkdir -p logs/tablet && rsync -az tablet:bzu/logs/ ./logs/tablet/
  .venv/bin/python collect.py report
  ```
- **Laptop only** (if ever needed): `ssh -N -D 1080 tablet & export ARCHIVE_PROXY=socks5h://localhost:1080 && .venv/bin/python -W ignore collect.py pull --sources <ids>`.

## Files
`collect.py`, `sources.json`, `test_collect.py`, `run_tablet.sh`, `README.md`, `access_options.md`, `date_check.md`, `probe_report.md`, `quality_report.md`, `logs/`, `articles.db`, `raw/`.

## Log
- 2026-11-09 ~20:00–21:04: the tablet tunnel dropped and reconnected; Wayback lookups from that hour were retried.
- 2026-11-09 23:15: Sky started redirecting to a Radware bot check (`validate.perfdrive.com`) from our IP (laptop and tablet share 46.43.113.184). Switched it to Wayback-only.
- 2026-11-09 23:20: rsync of the code + articles.db (358 MB) to the tablet; tests pass there (Python 3.14).
- 2026-11-10 16:15–20:00: CDX tested by hand from the tablet. Whole-domain and section prefixes time out (504 after 60 s); narrow queries often get a 503 page saying "Internet Archive services are temporarily offline"; only date-path prefixes (israelhayom) answered reliably. The CDX trouble is on the Internet Archive side; Wayback page fetches work.
- 2026-11-10 16:18: tablet DB snapshot before the code change: `~/bzu/articles.pre-cdxfix.db` (delete after the merge).

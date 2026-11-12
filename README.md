# Arabic news collector: Palestine ↔ Israel (7 Oct – 8 Nov 2023)

This is the collection step only: no translation and no classification. It covers 36 sources, all listed in `sources.json`.

## Setup (Python 3.11, macOS M1)

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Wayback access

`/etc/hosts` on this Mac blocks `archive.org`. All Wayback traffic (CDX and snapshots) goes through the tablet over an SSH SOCKS tunnel. Other sites are fetched directly.

```bash
ssh -N -D 1080 tablet &                      # keep it open while collecting
export ARCHIVE_PROXY=socks5h://localhost:1080
```

Without `ARCHIVE_PROXY`, the Wayback steps are skipped and a warning goes to the log.

## Phases

```bash
# self-check of the parsing logic
.venv/bin/python test_collect.py

# Phase 1: probe every source on one day -> probe.db, raw_probe/, probe_report.md
.venv/bin/python -W ignore collect.py probe --day 2023-10-10
.venv/bin/python -W ignore collect.py probe --sources wafa,pbc     # re-probe some sources
                                                                   # (first delete their rows, or delete probe.db)

# Phase 2: full pull -> articles.db, raw/, logs/pull.log (resumable: just run it again)
.venv/bin/python -W ignore collect.py pull
.venv/bin/python -W ignore collect.py pull --retry-failed          # retry fetch failures
.venv/bin/python -W ignore collect.py pull --force-size            # continue past the 20 GB estimate stop

# Phase 3: quality report -> quality_report.md (pull also writes it when it finishes)
.venv/bin/python collect.py report
.venv/bin/python collect.py report --probe                         # regenerate probe_report.md
```

## How it works

- **Discovery**, per source:
  1. The site's sitemaps (`discovery` in `sources.json`). These are daily or monthly templates, or a sitemap index. For an index, a binary search over the child sitemaps finds the window. The publish date of each URL is estimated from a running median of the sitemap dates, so a later edit does not hide an item.
  2. If the sitemap gives fewer than 3 URLs, the collector uses the union of GDELT 2.0 mentions files (English and translingual, 15-minute files) and Wayback CDX captures (7-day chunks).
- **Fetch**: one request per second per domain, with retries, backoff and `Retry-After`. robots.txt is respected. The User-Agent names the academic project and contains no email. Each page is fetched live first, with a Wayback `id_` snapshot as fallback. Sources marked `live_blocked`, or ones that return 5 challenge/403 responses in a row, switch to Wayback only. Nothing tries to get past paywalls, logins or CAPTCHAs.
- **Extraction**: trafilatura, with newspaper4k as a fallback when text is under 30 words. Fields come from JSON-LD and meta tags first. The publish date comes from page metadata, then the visible "تاريخ النشر" date, then htmldate, then the URL date. `date_source` records which one was used. Dates with no timezone are read as Asia/Jerusalem.
- **Scope** (`section` sources): a title keyword, then a URL section path, then at least 3 keyword hits in the body. `scope_match` records the rule, e.g. `title:غزة`, `url:/palestine`, `body:5:غزة,إسرائيل`.
- **Dedup**: by canonical URL, and by text hash for texts of 50 words or more. Removed duplicates have status `dup_url` or `dup_text` in `candidates`.
- **Storage**: `articles.db` holds the `articles` and `live_entries` tables from the spec. It also has `candidates` (every discovered URL, with status and note), `source_state` and the GDELT cache. Raw HTML is saved as `raw/<source_id>/<article_id>.html.gz`.
- **Size guard**: after discovery, the expected size is estimated from the probe averages. The pull stops before fetching if the estimate is over 20 GB, and it checks actual disk use as it goes.

Filtering video/infographic pages: `SELECT ... WHERE content_type NOT IN ('video','infographic')`.

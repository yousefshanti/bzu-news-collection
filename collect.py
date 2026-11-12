#!/usr/bin/env python3.11
"""Arabic news collector for the Palestine <-> Israel media-bias project.

    python collect.py probe [--day 2023-10-10] [--sources wafa,pbc]
    python collect.py pull  [--sources ...] [--retry-failed] [--force-size]
    python collect.py report [--probe]

Collection only: no translation, no classification.
"""
import argparse
import gzip
from html import unescape
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import statistics
import sys
import threading
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
import trafilatura
from dateutil import parser as dparser
from lxml import etree
from lxml import html as lhtml

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Jerusalem")
START = datetime(2023, 10, 7, 0, 0, 0, tzinfo=TZ)
END = datetime(2023, 11, 8, 23, 59, 59, tzinfo=TZ)
UA = ("BZU-MediaBias-Research/1.0 (academic research project on news coverage; "
      "Birzeit University; non-commercial)")
# archive.org may be unreachable from this machine; route only archive.org traffic through a proxy,
# e.g. `ssh -N -D 1080 tablet` then ARCHIVE_PROXY=socks5h://localhost:1080
ARCHIVE_PROXY = os.environ.get("ARCHIVE_PROXY")
SIZE_LIMIT = 20 * 1024 ** 3
MIN_SITEMAP_HITS = 3          # a sitemap "works" if it yields at least this many candidates
BLOCK_STREAK = 5              # consecutive 401/403/challenge responses => source is live-blocked

log = logging.getLogger("collect")


# ----------------------------------------------------------------------------- HTTP

def host_key(url):
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_challenge(status, body):
    if status in (401, 403):
        return True
    head = body[:20000]
    return status in (429, 503) and (b"Just a moment" in head or b"cf-chl" in head or b"captcha" in head.lower())


class Net:
    def __init__(self):
        self.mu = threading.Lock()
        self.locks, self.last, self.robots = {}, {}, {}
        self.local = threading.local()

    def _session(self):
        s = getattr(self.local, "s", None)
        if s is None:
            s = self.local.s = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept-Language": "ar,en;q=0.8"})
        return s

    def _wait(self, host, gap):
        with self.mu:
            lock = self.locks.setdefault(host, threading.Lock())
        with lock:
            d = self.last.get(host, 0) + gap - time.time()
            if d > 0:
                time.sleep(d)
            self.last[host] = time.time()

    def get(self, url, timeout=60, tries=4, retry=(429, 500, 502, 503, 504)):
        """Return (status, body_bytes, final_url, headers). status 0 = network failure."""
        host = host_key(url)
        archive = host.endswith("archive.org")
        gap = 2.0 if archive else 1.0
        proxies = {"http": ARCHIVE_PROXY, "https": ARCHIVE_PROXY} if archive and ARCHIVE_PROXY else None
        last = (0, b"", url, {})
        for attempt in range(tries):
            self._wait(host, gap)
            try:
                r = self._session().get(url, timeout=timeout, proxies=proxies, allow_redirects=True)
            except requests.RequestException as e:
                last = (0, str(e).encode()[:300], url, {})
                time.sleep(5 * 2 ** attempt)
                continue
            last = (r.status_code, r.content, r.url, r.headers)
            if r.status_code in retry and not is_challenge(r.status_code, r.content):
                ra = r.headers.get("Retry-After", "")
                time.sleep(min(int(ra), 300) if ra.isdigit() else 5 * 2 ** attempt)
                continue
            break
        return last

    def allowed(self, url):
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        with self.mu:
            rp = self.robots.get(base)
        if rp is None:
            rp = robotparser.RobotFileParser()
            st, body, _, _ = self.get(base + "/robots.txt", timeout=30, tries=2)
            if st in (401, 403):
                rp.disallow_all = True
            elif st == 200:
                rp.parse(body.decode("utf-8", "replace").splitlines())
            else:
                rp.allow_all = True
            with self.mu:
                self.robots[base] = rp
        return rp.can_fetch(UA, url)


NET = Net()
WAYBACK_OK = None


def wayback_available():
    global WAYBACK_OK
    if WAYBACK_OK is None:
        st, body, _, _ = NET.get("https://archive.org/wayback/available?url=example.com", timeout=60, tries=2)
        WAYBACK_OK = st == 200
        if not WAYBACK_OK:
            log.warning("Wayback unreachable (status %s). Set ARCHIVE_PROXY if archive.org is blocked locally.", st)
    return WAYBACK_OK


def wayback_fetch(url, ts=None):
    st, body, final, hdr = NET.get(f"https://web.archive.org/web/{ts or '20231108'}id_/{url}", timeout=90, tries=3)
    m = re.search(r"/web/(\d{14})", final or "")
    return st, body, hdr, (m.group(1) if m else None)


def decode(body, headers=None):
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass
    encs = []
    m = re.search(rb"""<meta[^>]+charset=["']?([\w-]+)""", body[:5000], re.I)
    if m:
        encs.append(m.group(1).decode("ascii", "ignore"))
    m = re.search(r"charset=([\w-]+)", (headers or {}).get("content-type", ""))
    if m:
        encs.append(m.group(1))
    for e in encs + ["windows-1256"]:
        try:
            return body.decode(e)
        except (UnicodeDecodeError, LookupError):
            pass
    return body.decode("utf-8", "replace")


# ----------------------------------------------------------------------------- dates

def parse_dt(s, default_tz=TZ):
    """Parse a date string into an aware datetime (naive => Asia/Jerusalem). None on failure."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    try:
        d = dparser.isoparse(s)
    except (ValueError, OverflowError):
        try:
            d = dparser.parse(s, fuzzy=False, dayfirst=True)  # non-ISO dates on these sites are dd/mm/yyyy
        except (ValueError, OverflowError, TypeError):
            return None
    if not (1990 < d.year < 2100):
        return None
    return d.replace(tzinfo=default_tz) if d.tzinfo is None else d


URL_DATE_RES = [
    re.compile(r"/(20\d\d)/(\d{1,2})/(\d{1,2})(?:/|$|\.)"),
    re.compile(r"/(20\d\d)-(\d\d)-(\d\d)(?:/|$)"),
    re.compile(r"[/_](20\d\d)(\d\d)(\d\d)[-_]"),
    re.compile(r"-(20\d\d)-(\d\d)-(\d\d)/?$"),
]


def url_date(url):
    u = unquote(url)
    for rx in URL_DATE_RES:
        m = rx.search(u)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    return None


def local_day(d):
    return d.astimezone(TZ).date() if isinstance(d, datetime) else d


def in_window(dt):
    return dt is not None and START <= dt.astimezone(TZ) <= END


# ----------------------------------------------------------------------------- sources & URL rules

JUNK_RE = re.compile(r"/(tags?|category|categories|author|authors|writers?|search|feed|rss|page|sitemap|"
                     r"login|register|subscribe|newsletter|contact|about|privacy|terms)(/|$|\?)|"
                     r"\.(jpe?g|png|gif|css|js|pdf|xml|json|ico|svg|webp|mp4|mp3|woff2?)(\?|$)", re.I)


def load_sources(only=None):
    srcs = json.loads((ROOT / "sources.json").read_text("utf-8"))
    if only:
        want = set(only.split(","))
        srcs = [s for s in srcs if s["id"] in want]
    return srcs


def norm_hostpath(url):
    p = urlsplit(url.strip())
    return host_key(url) + (p.path or "/")


def matches_source(s, url):
    hp = unquote(norm_hostpath(url))
    return any(hp.startswith(unquote(pre)) for pre in s["url_prefixes"])


def is_article(s, url):
    if not matches_source(s, url):
        return False
    p = urlsplit(url)
    if (p.path or "/") in ("/", "") or p.path.rstrip("/").count("/") < 1 and not p.query:
        return False
    if JUNK_RE.search(unquote(p.path)):
        return False
    rx = s.get("article_re")
    return bool(re.search(rx, unquote(url))) if rx else True


TRACKING = re.compile(r"^(utm_|fbclid|gclid|ref$|amp$|output$)")


def canon(url):
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = p.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(q), ""))


# ----------------------------------------------------------------------------- sitemaps

def localname(el):
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def parse_sitemap(body, base):
    """Return (is_index, [(loc, date_str)]) for urlset or sitemapindex XML."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    root = etree.fromstring(body, parser=etree.XMLParser(recover=True, huge_tree=True))
    if root is None:
        return False, []
    out = []
    for el in root:
        if localname(el) not in ("url", "sitemap"):
            continue
        loc = lastmod = pub = None
        for c in el:
            ln = localname(c)
            if ln == "loc" and c.text:
                loc = c.text.strip()
            elif ln == "lastmod":
                lastmod = (c.text or "").strip()
            elif ln == "news":
                for cc in c.iter():
                    if localname(cc) == "publication_date":
                        pub = (cc.text or "").strip()
        if loc:
            out.append((urljoin(base, loc), pub or lastmod))
    return localname(root) == "sitemapindex", out


def fetch_sitemap(url, via_wayback=None):
    """via_wayback=<timestamp>: read the archived copy nearest that time (site itself blocks us / old sitemap)."""
    if via_wayback:
        url = f"https://web.archive.org/web/{via_wayback}id_/{url}"
    st, body, _, _ = NET.get(url, timeout=120)
    if st != 200 or not body:
        raise RuntimeError(f"sitemap {url} -> HTTP {st}")
    return parse_sitemap(body, url)


def estimate_dates(entries):
    """Running median of neighbour dates: robust publish-date estimate for ID-ordered sitemaps."""
    ds = [parse_dt(d) for _, d in entries]
    ts = [d.timestamp() if d else None for d in ds]
    est = []
    for i in range(len(ts)):
        win = [t for t in ts[max(0, i - 10): i + 11] if t is not None]
        est.append(datetime.fromtimestamp(statistics.median(win), TZ) if win else None)
    return ds, est


def select_entries(s, entries, dfrom, dto, ordered):
    """Keep entries whose URL date (exact) or own/estimated sitemap date (+-1 day) is in [dfrom, dto]."""
    ds, est = estimate_dates(entries) if ordered else ([parse_dt(d) for _, d in entries], [None] * len(entries))
    keep = []
    lo, hi = dfrom - timedelta(days=1), dto + timedelta(days=1)
    for (loc, _), own, e in zip(entries, ds, est):
        if not is_article(s, loc):
            continue
        ud = url_date(loc)
        if ud:
            if dfrom <= ud <= dto:
                keep.append((loc, ud.isoformat()))
            continue
        cands = [local_day(x) for x in (own, e) if x]
        if any(lo <= c <= hi for c in cands):
            keep.append((loc, local_day(own or e).isoformat()))
    return keep


def month_iter(dfrom, dto):
    y, m = dfrom.year, dfrom.month
    while (y, m) <= (dto.year, dto.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def sitemap_template(s, spec, dfrom, dto):
    urls = []
    if spec["per"] == "day":
        d = dfrom
        while d <= dto:
            ago = (date.today() - d).days  # ynet: /google_site_map_by_period/<days ago>/<days>
            urls += [u.format(Y=d.year, M=f"{d.month:02d}", m=d.month, d=d.day, D=f"{d.day:02d}", AGO=ago)
                     for u in spec["urls"]]
            d += timedelta(days=1)
    elif spec["per"] == "month":
        for y, m in month_iter(dfrom, dto):
            urls += [u.format(Y=y, M=f"{m:02d}", m=m) for u in spec["urls"]]
    else:
        urls = list(spec["urls"])
    out = []
    for u in urls:
        try:
            _, entries = fetch_sitemap(u, spec.get("via_wayback"))
        except Exception as e:
            log.warning("[%s] %s", s["id"], e)
            continue
        if spec.get("keep_all") and spec["per"] == "day":  # per-day feed (ynet): lastmod is an edit date, trust the day
            out += [(loc, None) for loc, _ in entries if is_article(s, loc)]
            continue
        out += select_entries(s, entries, dfrom, dto, ordered=spec["per"] == "static")
    return out


def num_key(u):
    return tuple(int(x) for x in re.findall(r"\d+", urlsplit(u).path + "?" + urlsplit(u).query))


def sitemap_index(s, spec, dfrom, dto):
    if spec.get("child_tpl"):  # index not archived: numbered children
        a, b = spec["child_range"]
        children = [spec["child_tpl"].format(n=n) for n in range(a, b + 1)]
    else:
        _, idx = fetch_sitemap(spec["url"], spec.get("via_wayback"))
        children = sorted({u for u, _ in idx if re.search(spec["child_re"], u)}, key=num_key)
    if not children:
        raise RuntimeError("no child sitemaps matched")
    cache = {}

    def child(i):
        if i not in cache:
            try:
                cache[i] = fetch_sitemap(children[i], spec.get("via_wayback"))[1]
            except Exception as e:
                log.warning("[%s] %s", s["id"], e)
                cache[i] = []
        return cache[i]

    def med(i):
        ds = sorted(d for d in (parse_dt(x) for _, x in child(i)) if d)
        return ds[len(ds) // 2] if ds else None

    n = len(children)
    if spec.get("all_children") or n <= 2:
        sel = range(n)
    else:
        first, lastc = med(0), med(n - 1)
        asc = not (first and lastc and first > lastc)
        oldest = datetime(1990, 1, 1, tzinfo=TZ)

        def key(k):  # position k in ascending-date order
            m = med(k if asc else n - 1 - k)
            return m or oldest

        def first_ge(t):
            lo, hi = 0, n
            while lo < hi:
                mid = (lo + hi) // 2
                if key(mid) >= t:
                    hi = mid
                else:
                    lo = mid + 1
            return lo

        t0 = datetime.combine(dfrom, datetime.min.time(), TZ) - timedelta(days=3)
        t1 = datetime.combine(dto, datetime.max.time(), TZ) + timedelta(days=3)
        k0, k1 = max(first_ge(t0) - 1, 0), min(first_ge(t1), n - 1)
        sel = [k if asc else n - 1 - k for k in range(k0, k1 + 1)]
    out = []
    for i in sel:
        out += select_entries(s, child(i), dfrom, dto, ordered=True)
    return out


# ----------------------------------------------------------------------------- GDELT

GDELT = "http://data.gdeltproject.org/gdeltv2/{ts}{tr}.mentions.CSV.zip"


def gdelt_slots(dfrom, dto):
    t = datetime.combine(dfrom, datetime.min.time(), TZ).astimezone(timezone.utc)
    end = datetime.combine(dto, datetime.max.time(), TZ).astimezone(timezone.utc) + timedelta(hours=6)
    t = t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)
    while t <= end:
        yield t.strftime("%Y%m%d%H%M%S")
        t += timedelta(minutes=15)


def build_gdelt(db, sources, dfrom, dto):
    """Download GDELT 2.0 mentions files (English + translingual) and keep URLs of our sources."""
    with DB_LOCK:
        done = {r[0] for r in db.execute("SELECT file FROM gdelt_done").fetchall()}
    files = [GDELT.format(ts=ts, tr=tr) for ts in gdelt_slots(dfrom, dto) for tr in ("", ".translation")]
    todo = [f for f in files if f not in done]
    log.info("GDELT: %d files (%d already done)", len(files), len(files) - len(todo))
    for i, f in enumerate(todo):
        st, body, _, _ = NET.get(f, timeout=120, tries=3)
        rows = []
        if st == 200:
            try:
                z = zipfile.ZipFile(io.BytesIO(body))
                text = z.read(z.namelist()[0]).decode("utf-8", "replace")
            except zipfile.BadZipFile:
                text = ""
            slot = re.search(r"/(\d{14})", f).group(1)
            for line in text.splitlines():
                c = line.split("\t")
                if len(c) > 5 and c[3] == "1":
                    u = c[5]
                    if any(matches_source(s, u) for s in sources):
                        rows.append((u, slot))
        with DB_LOCK, db:
            db.executemany("INSERT OR IGNORE INTO gdelt_urls(url, slot) VALUES(?,?)", rows)
            db.execute("INSERT OR IGNORE INTO gdelt_done(file, status) VALUES(?,?)", (f, st))
        if i % 200 == 0:
            log.info("GDELT progress %d/%d", i, len(todo))


def gdelt_discover(s, db, dfrom, dto):
    out = []
    with DB_LOCK, db:
        rows = db.execute("SELECT url, slot FROM gdelt_urls").fetchall()
    for u, slot in rows:
        if is_article(s, u) and url_date_ok(u, dfrom, dto):
            d = datetime.strptime(slot, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).astimezone(TZ).date()
            if dfrom - timedelta(days=1) <= d <= dto:
                out.append((u, d.isoformat()))
    return out


# ----------------------------------------------------------------------------- Wayback CDX

CDX_SLOTS = threading.Semaphore(3)  # parallel CDX queries across sources
CDX_LIMIT = 50000
CDX_MAX_TRIES = 4      # attempts per (source, prefix, day), counted over all passes and runs
CDX_FAIL_STREAK = 5    # consecutive failures => stop this pass for the source (server down or query too heavy)
CDX_PASSES = 6         # passes per run; unfinished days stay in cdx_days and resume on the next run
CDX_PASS_WAIT = 1800   # seconds between passes


def cdx_query(pre, day=None):
    """One light prefix query: no collapse, no server-side filters (status is filtered here).

    A 503 ("temporarily offline", answered in seconds) is retried briefly. A 504 means the server gave up
    after 60 s, so an immediate retry only burns a slot: the caller records the day and retries it later."""
    params = {"url": pre, "matchType": "prefix", "output": "json", "fl": "original,timestamp,statuscode",
              "limit": str(CDX_LIMIT)}
    if day:
        params["from"] = params["to"] = day.strftime("%Y%m%d")
    with CDX_SLOTS:
        st, body, _, _ = NET.get("https://web.archive.org/cdx/search/cdx?" + urlencode(params),
                                 timeout=75, tries=2, retry=(429, 502, 503))
    if st != 200:
        return None, st
    try:
        rows = json.loads(body)[1:] if body.strip() else []
    except json.JSONDecodeError:
        return None, st
    if len(rows) >= CDX_LIMIT:
        log.warning("CDX %s %s hit the %d-row limit; results are truncated", pre, day or "all", CDX_LIMIT)
    return [(o, ts) for o, ts, sc in rows if sc in ("200", "-")], st  # "-" = revisit of an earlier capture


def cdx_jobs(s, dfrom, dto):
    """[(prefix, day_key, query_day)], one per CDX query.

    cdx_once prefixes are already narrow (e.g. an article-ID range) and run once over all dates; {Y}/{M}/{D}
    prefixes carry the day in the path; the others are filtered by capture day (a weak hint, so one day past dto)."""
    jobs = [(p, "all", None) for p in s.get("cdx_once", [])]
    for pre in s.get("cdx_prefixes", s["url_prefixes"]):
        templated = "{" in pre
        for k in range((dto - dfrom).days + (1 if templated else 2)):
            d = dfrom + timedelta(days=k)
            if templated:
                jobs.append((pre.format(Y=d.year, M=f"{d.month:02d}", D=f"{d.day:02d}"), d.isoformat(), None))
            else:
                jobs.append((pre, d.isoformat(), d))
    return jobs


def url_date_ok(url, dfrom, dto):
    ud = url_date(url)
    return ud is None or dfrom - timedelta(days=1) <= ud <= dto + timedelta(days=1)


def wayback_captures(s, spec, dfrom, dto):
    """Every archived copy of rolling news sitemaps captured in the window: union of their entries."""
    out = []
    for u in spec["urls"]:
        rows, st = cdx_exact(u, spec.get("from", (dfrom - timedelta(days=1)).strftime("%Y%m%d")),
                             spec.get("to", (dto + timedelta(days=7)).strftime("%Y%m%d")))
        rows = rows or []
        step = max(1, len(rows) // spec.get("max", 60))
        for ts, orig in rows[::step]:
            try:
                _, entries = fetch_sitemap(orig, ts)
            except Exception as e:
                log.warning("[%s] %s", s["id"], e)
                continue
            out += select_entries(s, entries, dfrom, dto, ordered=False)
    return out


def cdx_exact(url, f, t):
    params = {"url": url, "from": f, "to": t, "output": "json", "fl": "timestamp,original",
              "filter": "statuscode:200"}
    with CDX_SLOTS:
        st, body, _, _ = NET.get("https://web.archive.org/cdx/search/cdx?" + urlencode(params), timeout=75, tries=4)
    try:
        return (json.loads(body)[1:] if st == 200 and body.strip() else None), st
    except json.JSONDecodeError:
        return None, st


def id_range(s, spec, dfrom, dto):
    """Archive-style discovery for sites with sequential numeric IDs: bisect IDs by page date, then enumerate."""
    tmpl = spec["url"]
    cache = {}

    def date_at(i):
        if i not in cache:
            cache[i] = None
            for k in range(12):
                st, body, final, h = NET.get(tmpl.format(id=i + k), tries=2)
                if st == 200 and body.strip():
                    r = extract(decode(body, h), final, s)
                    if r["published_at"] and r["date_source"] in ("metadata", "visible"):
                        cache[i] = local_day(r["published_at"])
                        break
        return cache[i]

    def first_ge(target):
        a, b = spec["lo"], spec["hi"]
        while b - a > 30:
            mid = (a + b) // 2
            d = date_at(mid)
            if d is not None and d >= target:
                b = mid
            else:
                a = mid
        return a

    i0 = max(first_ge(dfrom) - 30, spec["lo"])
    i1 = first_ge(dto + timedelta(days=1)) + 30
    log.info("[%s] id_range %s..%s (%d ids)", s["id"], i0, i1, i1 - i0 + 1)
    return [(tmpl.format(id=i), None) for i in range(i0, i1 + 1)]


# ----------------------------------------------------------------------------- extraction

ARTICLE_TYPES = {"NewsArticle", "Article", "ReportageNewsArticle", "OpinionNewsArticle", "AnalysisNewsArticle",
                 "BackgroundNewsArticle", "ReviewNewsArticle", "AskPublicNewsArticle", "BlogPosting",
                 "LiveBlogPosting", "SatiricalArticle", "ScholarlyArticle", "Report", "TechArticle"}
LD_CT = {"LiveBlogPosting": "live_blog", "OpinionNewsArticle": "opinion", "AnalysisNewsArticle": "analysis",
         "ReportageNewsArticle": "report", "BackgroundNewsArticle": "report", "ReviewNewsArticle": "opinion",
         "Report": "report"}
URL_CT = [
    ("live_blog", r"liveblog|live-blog|/live/|/live-|/livenews|تغطية-مباشرة|/مباشر"),
    ("video", r"/videos?/|/video-|/فيديو|/programs/|/watch/|/av-\d|/vid-|/episodes?/"),
    ("infographic", r"infograph|انفوجراف|إنفوجراف|/info/|/graphics/"),
    ("opinion", r"/opinions?/|/opinion-|/آراء|/اراء|/مقالات|/مقال/|/كتاب|/blogs?/|/columns?/|/columnists?/|"
                r"ty-article-opinion|/op-ed|/editorials?/|/افتتاحية|/وجهات-نظر|/رأي|/views/"),
    ("analysis", r"/analysis|/تحليل|/in-depth|/explainers?/"),
    ("interview", r"/interviews?/|مقابلة|مقابلات|/حوار"),
    ("press_release", r"press-release|/بيانات|/بيان-صحفي|/press-releases?/"),
    ("report", r"/reports?/|/تقارير|/تقرير|/features?/|/تحقيقات|/ملفات|/longform|/investigations?/|"
               r"ty-article-magazine|/magazine/"),
]
SECTION_CT = [
    ("live_blog", ["مباشر", "live"]), ("video", ["فيديو", "video", "برامج"]),
    ("infographic", ["إنفوجرافيك", "انفوجرافيك", "infographic"]),
    ("opinion", ["رأي", "آراء", "اراء", "مقالات", "كتاب", "مدونات", "opinion", "columns", "blogs", "op-ed"]),
    ("analysis", ["تحليل", "تحليلات", "analysis"]), ("interview", ["مقابلة", "مقابلات", "حوار", "interview"]),
    ("press_release", ["بيانات", "بيان صحفي", "press release"]),
    ("report", ["تقارير", "تقرير", "تحقيقات", "ملفات", "features", "reports", "magazine"]),
]

KW_AR = ["غزة", "غزه", "فلسطين", "فلسطيني", "إسرائيل", "اسرائيل", "إسرائيلي", "اسرائيلي", "الاحتلال",
         "الضفة", "حماس", "القدس", "الأقصى", "الاقصى", "القسام", "الجهاد الإسلامي", "نتنياهو", "تل أبيب",
         "رفح", "خان يونس", "جنين", "نابلس", "طوفان", "مستوطن", "الأونروا", "الكنيست"]
KW_EN = ["gaza", "israel", "hamas", "west bank", "idf", "palestin", "jerusalem", "netanyahu", "al-aqsa",
         "rafah", "knesset", "unrwa"]
URL_SCOPE = re.compile(r"/(palestine|israel|gaza|فلسطين|غزة|إسرائيل|اسرائيل|الحرب-على-غزة|israel-gaza|"
                       r"israel-hamas|war-on-gaza|al-aqsa-flood|طوفان-الأقصى|middle-east/israel)", re.I)
NOT_KW = re.compile(r"القدس العربي|Al-?Quds Al-?Arabi", re.I)

AGENCY_STRICT = {
    "Reuters": r"رويترز|\bReuters\b", "AFP": r"فرانس\s*برس|أ\s*ف\s*ب|\bAFP\b",
    "AP": r"أسوشيتد\s*برس|أسوشيتدبرس|Associated Press|\bAP\b", "Anadolu": r"الأناضول|\bAnadolu\b",
    "dpa": r"د\s*ب\s*أ|\bdpa\b", "Xinhua": r"شينخوا|\bXinhua\b", "Sputnik": r"سبوتنيك|\bSputnik\b",
    "WAFA": r"\bWAFA\b", "MENA": r"أ\s*ش\s*أ", "Bloomberg": r"بلومبرغ|\bBloomberg\b", "TASS": r"\bTASS\b|تاس\s*[-–:)]",
    "Agencies": r"وكالات|\bagencies\b",
}
# short words that are also common Arabic words: only count next to dateline punctuation or in a byline
AGENCY_SHORT = {"WAFA": "وفا", "Ma'an": "معا", "Sama": "سما", "Safa": "صفا", "Shehab": "شهاب", "QNA": "قنا",
                "SPA": "واس", "WAM": "وام", "SANA": "سانا"}


def agency_credits(byline, lead):
    found = []
    zone = f"{byline or ''}\n{lead or ''}"
    for name, rx in AGENCY_STRICT.items():
        if re.search(rx, zone):
            found.append(name)
    for name, w in AGENCY_SHORT.items():
        ctx = rf"(?:^|[\s(«/\-–:]){w}(?=\s*[)»/\-–:،.]|\s*$)"
        if (byline and re.search(rf"(?<!\w){w}(?!\w)", byline)) or re.search(ctx, lead or "", re.M):
            if name not in found:
                found.append(name)
    return found


def scope_match(title, text, url):
    t = NOT_KW.sub(" ", title or "")
    for kw in KW_AR:
        if kw in t:
            return f"title:{kw}"
    tl = t.lower()
    for kw in KW_EN:
        if kw in tl:
            return f"title:{kw}"
    m = URL_SCOPE.search(unquote(url))
    if m:
        return f"url:{m.group(0)}"
    body = NOT_KW.sub(" ", text or "")
    bl = body.lower()
    hits = Counter()
    for kw in KW_AR:
        hits[kw] += body.count(kw)
    for kw in KW_EN:
        hits[kw] += len(re.findall(rf"\b{re.escape(kw)}", bl))
    total = sum(hits.values())
    if total >= 3:
        top = [k for k, _ in hits.most_common(3) if hits[k]]
        return f"body:{total}:{','.join(top)}"
    return None


def jsonld_nodes(tree):
    nodes = []
    for sc in tree.xpath('//script[@type="application/ld+json"]'):
        raw = (sc.text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw, strict=False)
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack += x
            elif isinstance(x, dict):
                nodes.append(x)
                if "@graph" in x:
                    stack.append(x["@graph"])
    return nodes


def types_of(n):
    t = n.get("@type", [])
    return set(t if isinstance(t, list) else [t])


def names(v):
    out = []
    for a in v if isinstance(v, list) else [v]:
        if isinstance(a, dict):
            nm = a.get("name")
            if isinstance(nm, str) and nm.strip():
                out.append(nm.strip())
        elif isinstance(a, str) and a.strip():
            out.append(a.strip())
    return out


def as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, list):
        return [x.strip() if isinstance(x, str) else str(x) for x in v if x]
    return [str(v)]


def meta_map(tree):
    m = defaultdict(list)
    for el in tree.xpath("//meta"):
        k = (el.get("property") or el.get("name") or el.get("itemprop") or "").strip().lower()
        v = el.get("content")
        if k and v and v.strip():
            m[k].append(v.strip())
    return m


def byline_text(tree):
    for xp in ('//*[@rel="author"]', '//*[@itemprop="author"]', '//*[contains(@class,"byline")]',
               '//*[contains(@class,"author")]', '//*[contains(@class,"writer")]'):
        for el in tree.xpath(xp)[:3]:
            if el.tag in ("meta", "link", "script", "style"):
                continue
            t = re.sub(r"\s+", " ", el.text_content() or "").strip()
            if 2 <= len(t) <= 150:
                return t
    return ""


def newspaper_text(html, url):
    try:
        from newspaper import Article
        a = Article(url)
        a.download(input_html=html)
        a.parse()
        return a.text or ""
    except Exception:
        return ""


VIS_LABEL = re.compile(r"(تاريخ النشر|نشر بتاريخ|نشر في|تاريخ الخبر|آخر تحديث|Published|Posted)[^0-9]{0,15}")
VIS_DATE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](20\d\d)(?:[\s,\-–|]*(\d{1,2}):(\d{2})\s*(ص|م|AM|PM|am|pm)?)?")


AR_MONTHS = {"كانون الثاني": 1, "يناير": 1, "شباط": 2, "فبراير": 2, "آذار": 3, "اذار": 3, "مارس": 3,
             "نيسان": 4, "أبريل": 4, "ابريل": 4, "أيار": 5, "ايار": 5, "مايو": 5, "حزيران": 6, "يونيو": 6,
             "تموز": 7, "يوليو": 7, "آب": 8, "أغسطس": 8, "اغسطس": 8, "أيلول": 9, "ايلول": 9, "سبتمبر": 9,
             "تشرين الأول": 10, "تشرين الاول": 10, "أكتوبر": 10, "اكتوبر": 10, "تشرين الثاني": 11, "نوفمبر": 11,
             "كانون الأول": 12, "كانون الاول": 12, "ديسمبر": 12}
VIS_ARDATE = re.compile(r"(\d{1,2})\s+(" + "|".join(sorted(AR_MONTHS, key=len, reverse=True)) + r")\s+(20\d\d)")


def visible_date(tree):
    """Day-first visible publish date like 'تاريخ النشر: 10/10/2023 12:17 ص' or '22 أيلول 2022' (Asia/Jerusalem)."""
    txt = re.sub(r"\s+", " ", tree.text_content() or "")
    lab = VIS_LABEL.search(txt)
    m = VIS_DATE.match(txt, lab.end()) if lab else None
    if not m and lab:
        a = VIS_ARDATE.match(txt, lab.end())
        if a:
            try:
                return datetime(int(a.group(3)), AR_MONTHS[a.group(2)], int(a.group(1)), tzinfo=TZ)
            except ValueError:
                return None
    if not m:
        m = next((x for x in VIS_DATE.finditer(txt) if x.group(4)), None)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    h, mi, ap = int(m.group(4) or 0), int(m.group(5) or 0), (m.group(6) or "").lower()
    if ap in ("م", "pm") and h < 12:
        h += 12
    elif ap in ("ص", "am") and h == 12:
        h = 0
    try:
        return datetime(y, mo, d, h, mi, tzinfo=TZ)
    except ValueError:
        return None


def first_dt(*vals):
    for v in vals:
        for x in (v if isinstance(v, list) else [v]):
            d = parse_dt(x) if isinstance(x, str) else None
            if d:
                return d, x
    return None, None


def body_text(tree, xpaths):
    """Text of the site's main-body element(s) (first match per xpath), or None if not configured/not found."""
    if not xpaths:
        return None
    parts = []
    for xp in xpaths if isinstance(xpaths, list) else [xpaths]:
        els = tree.xpath(xp)
        if els:
            frag = lhtml.tostring(els[0], encoding="unicode")
            txt = trafilatura.extract("<html><body>" + frag + "</body></html>", favor_recall=True,
                                      include_comments=False) or els[0].text_content()
            parts.append(re.sub(r"[ \t]+", " ", txt).strip())
    return "\n".join(p for p in parts if p) if parts else None


ORG_WORDS = re.compile(r"وكالة|وكالات|متابعات|شبكة|موقع|قناة|صحيفة|جريدة|التحرير|المحرر|فريق|هيئة|دوت كوم|^فيديو فرانس|walla|مراسل(?:و|ي)?\s|بي بي سي|"
                       r"\b(news|agency|agencies|staff|desk|editorial|editor|team|reporters?|newsroom|online|"
                       r"press|media|tv|channel|network|times|post|wire)\b|\.(com|net|ps|tv|il)\b", re.I)
AGENCY_NAMES = re.compile(r"رويترز|فرانس\s*برس|الأناضول|أسوشيتد|وفا|معا|سما|صفا|شهاب|reuters|afp|\bap\b|anadolu|wafa|"
                          r"associated press|xinhua|dpa|i24news|ynet|haaretz|jpost", re.I)


BYLINE_LINE = re.compile(r"^\s*-?\s*(Author|Role|Published|Updated|Reporting from|المؤلف|الكاتب)\s*,?\s*([^\n]{0,80})?$")


def person_or_org(v):
    """JSON-LD author(s) as (name, '@type' or None)."""
    out = []
    for a in v if isinstance(v, list) else [v]:
        if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"].strip():
            t = a.get("@type")
            out.append((a["name"].strip(), t if isinstance(t, str) else None))
        elif isinstance(a, str) and a.strip():
            out.append((a.strip(), None))
    return out


def split_authors(found, s):
    """Persons only in authors; site/agency/desk names go to author_orgs."""
    own = {s["name"].lower(), s["domain"].split("/")[0].split(".")[0].lower(), *map(str.lower, s.get("org_aliases", []))}
    persons, orgs = [], []
    for name, typ in found:
        low = name.lower()
        is_org = (typ in ("Organization", "NewsMediaOrganization", "Corporation") or "«" in name or "»" in name
                  or any(o and o in low for o in own) or ORG_WORDS.search(name) or AGENCY_NAMES.search(name)
                  or re.fullmatch(r"[a-z0-9_.-]+", low) is not None and len(low) < 4)
        (orgs if is_org else persons).append(name)
    return list(dict.fromkeys(persons)), list(dict.fromkeys(orgs))


def extract(html, url, s):
    if not html or not html.strip():
        html = "<html></html>"
    tree = lhtml.fromstring(html)
    nodes = jsonld_nodes(tree)
    main = next((n for n in nodes if types_of(n) & ARTICLE_TYPES), None)
    video_node = next((n for n in nodes if "VideoObject" in types_of(n)), None)
    meta = meta_map(tree)
    t = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=False, favor_recall=True)
    t = t.as_dict() if t is not None else {}
    text = (t.get("text") or "").strip()
    body = body_text(tree, s.get("body_xpath"))
    if body is not None:  # site-specific main body: excludes related/infinite-scroll/stories widgets
        text = body
    # drop byline/metadata lines that some layouts put inside the body (e.g. BBC "- Author, X / - Role, Y")
    text = "\n".join(l for l in text.split("\n") if not BYLINE_LINE.match(l)).strip()
    if len(text.split()) < 30 and body is None:
        alt = newspaper_text(html, url).strip()
        if len(alt.split()) > len(text.split()):
            text = alt
    m = main or {}
    title = unescape(str(m.get("headline") or (meta.get("og:title") or [None])[0] or t.get("title") or "")).strip()
    first, _, rest = text.partition("\n")
    if title and first.strip() and (first.strip() in title or title in first.strip()) and len(first) < 300:
        text = rest.strip()  # body repeats the headline as its first line
    subtitle = unescape(str(m.get("description") or (meta.get("description") or meta.get("og:description") or [""])[0] or "")).strip()

    pub, pub_raw = first_dt(m.get("datePublished"), meta.get("article:published_time"), meta.get("datepublished"),
                            meta.get("pubdate"), meta.get("publishdate"), meta.get("publish-date"),
                            meta.get("parsely-pub-date"), meta.get("sailthru.date"), meta.get("dc.date.issued"),
                            meta.get("date"), (video_node or {}).get("uploadDate"),
                            tree.xpath("//time[@datetime]/@datetime")[:1])
    date_source = "metadata" if pub else None
    if not pub:
        pub = visible_date(tree)
        date_source = "visible" if pub else None
    if not pub and t.get("date"):
        pub, date_source = parse_dt(t["date"]), "htmldate"
    upd, _ = first_dt(m.get("dateModified"), meta.get("article:modified_time"), meta.get("og:updated_time"),
                      meta.get("datemodified"))

    raw_byline = byline_text(tree)
    found = person_or_org(m.get("author"))
    if not found:
        found = [(a, None) for a in (meta.get("author") or [])[:1]]
    if not found and t.get("author"):
        found = [(a.strip(), None) for a in t["author"].split(";") if a.strip()]
    authors, author_orgs = split_authors(found, s)
    author_raw = raw_byline or ((meta.get("author") or [""])[0]) or "; ".join(names(m.get("author")))

    section = m.get("articleSection") or (meta.get("article:section") or [None])[0] or ""
    if isinstance(section, list):
        section = " / ".join(map(str, section))
    if not section:
        crumbs = [n for n in nodes if "BreadcrumbList" in types_of(n)]
        if crumbs:
            items = crumbs[0].get("itemListElement") or []
            nm = [names(i.get("item") if isinstance(i.get("item"), dict) else i) for i in items if isinstance(i, dict)]
            nm = [x[0] for x in nm if x]
            section = " / ".join(nm[1:-1] or nm[:1])
    if not section and t.get("categories"):
        section = t["categories"] if isinstance(t["categories"], str) else ", ".join(t["categories"])
    tags = meta.get("article:tag") or as_list(m.get("keywords")) or as_list(t.get("tags"))

    ctype, signals = content_type(main, video_node, meta, url, section)
    free = m.get("isAccessibleForFree")
    truncated = str(free).lower() == "false" or bool(re.search(r"[./]premium", urlsplit(url).path))
    canonical = (tree.xpath('//link[@rel="canonical"]/@href') or meta.get("og:url") or [url])[0]

    entries = []
    if main and "LiveBlogPosting" in types_of(main):
        for e in main.get("liveBlogUpdate") or []:
            if isinstance(e, dict):
                et = parse_dt(e.get("datePublished"))
                body = (e.get("articleBody") or e.get("headline") or "").strip()
                if body:
                    entries.append({"entry_time": et, "text": body, "author": "; ".join(names(e.get("author")))})

    return {
        "title": title, "subtitle": subtitle, "text": text, "published_at": pub, "pub_raw": pub_raw,
        "updated_at": upd, "date_source": date_source, "content_type": ctype, "content_type_signals": signals,
        "authors": authors, "author_orgs": author_orgs, "author_raw": author_raw,
        "js_shell": len(text.split()) < 30 and bool(re.search(r"enable JavaScript|تمكين جافا", html[:200000], re.I)),
        "agency_credit": agency_credits(author_raw, text[:250]),
        "site_section": section.strip() if isinstance(section, str) else "", "tags": tags[:50],
        "is_truncated": truncated, "canonical_url": urljoin(url, canonical), "live_entries": entries,
    }


def content_type(main, video_node, meta, url, section):
    sig = {}
    ld_types = sorted(types_of(main)) if main else []
    if ld_types:
        sig["jsonld_type"] = ld_types
    if video_node and not main:
        sig["jsonld_video"] = True
    ogt = (meta.get("og:type") or [""])[0]
    if ogt:
        sig["og_type"] = ogt
    path = unquote(urlsplit(url).path).lower()
    url_hits = [ct for ct, rx in URL_CT if re.search(rx, path)]
    if url_hits:
        sig["url_path"] = url_hits
    sec_l = (section or "").lower()
    sec_hits = [ct for ct, kws in SECTION_CT if any(k in sec_l for k in kws)]
    if section:
        sig["section"] = section[:200]
    if sec_hits:
        sig["section_hits"] = sec_hits
    for tname in ld_types:
        if tname in LD_CT:
            return LD_CT[tname], sig
    if sig.get("jsonld_video") or ogt.startswith("video"):
        return "video", sig
    if url_hits:
        return url_hits[0], sig
    if sec_hits:
        return sec_hits[0], sig
    if ld_types or ogt == "article":
        return "news", sig
    return "unknown", sig


# ----------------------------------------------------------------------------- storage

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY, source_id TEXT, url TEXT, canonical_url TEXT UNIQUE,
  title TEXT, subtitle TEXT, text TEXT, published_at TEXT, updated_at TEXT, date_source TEXT,
  language TEXT, category TEXT, scope_match TEXT, content_type TEXT, content_type_signals TEXT,
  authors TEXT, author_raw TEXT, agency_credit TEXT, site_section TEXT, tags TEXT,
  discovery_method TEXT, fetched_from TEXT, is_truncated INTEGER, word_count INTEGER,
  text_hash TEXT, fetched_at TEXT);
CREATE INDEX IF NOT EXISTS ix_articles_hash ON articles(text_hash);
CREATE INDEX IF NOT EXISTS ix_articles_src ON articles(source_id, published_at);
CREATE TABLE IF NOT EXISTS live_entries(
  id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES articles(id), entry_time TEXT, text TEXT, author TEXT);
CREATE TABLE IF NOT EXISTS candidates(
  url TEXT PRIMARY KEY, source_id TEXT, method TEXT, hint TEXT, wb_ts TEXT,
  status TEXT DEFAULT 'pending', note TEXT);
CREATE INDEX IF NOT EXISTS ix_cand ON candidates(source_id, status);
CREATE TABLE IF NOT EXISTS source_state(
  source_id TEXT PRIMARY KEY, discovered INTEGER DEFAULT 0, discovery_method TEXT,
  n_candidates INTEGER, live_blocked INTEGER DEFAULT 0, notes TEXT);
CREATE TABLE IF NOT EXISTS gdelt_urls(url TEXT PRIMARY KEY, slot TEXT);
CREATE TABLE IF NOT EXISTS gdelt_done(file TEXT PRIMARY KEY, status INTEGER);
CREATE TABLE IF NOT EXISTS cdx_days(
  source_id TEXT, prefix TEXT, day TEXT, status TEXT DEFAULT 'pending', http INTEGER, n_rows INTEGER,
  tries INTEGER DEFAULT 0, updated_at TEXT, PRIMARY KEY(source_id, prefix, day));
CREATE VIEW IF NOT EXISTS cross_source_duplicates AS
  SELECT text_hash, group_concat(DISTINCT source_id) AS source_ids, count(DISTINCT source_id) AS n_sources,
         count(*) AS n_items
  FROM articles WHERE text_hash IS NOT NULL AND word_count >= 50
  GROUP BY text_hash HAVING count(DISTINCT source_id) > 1;
"""


def connect(path):
    db = sqlite3.connect(path, timeout=120, check_same_thread=False)
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(articles)")}
    if "author_orgs" not in cols:  # migration for databases created before the persons/orgs split
        db.execute("ALTER TABLE articles ADD COLUMN author_orgs TEXT")
        db.commit()
    return db


def reextract(dbpath, rawdir, sources):
    """Re-run extraction + scope filter from saved raw HTML (no re-download). Items now out of scope or
    duplicated within their source are removed from articles and marked in candidates."""
    db = connect(dbpath)
    counts = Counter()
    for s in sources:
        rows = db.execute("SELECT id, url, published_at FROM articles WHERE source_id=? ORDER BY id", (s["id"],)).fetchall()
        for aid, url, pub in rows:
            p = rawdir / s["id"] / f"{aid}.html.gz"
            if not p.exists():
                counts["no_raw"] += 1
                continue
            html = gzip.open(p, "rt", encoding="utf-8").read()
            rec = extract(html, url, s)
            status = None
            if rec["js_shell"]:
                status = "js_shell"
            new_pub = rec["published_at"]
            m = re.search(r"/(1[5-9]\d{8})-", unquote(url)) if s.get("epoch_url_date") else None
            if m:
                new_pub, rec["date_source"] = datetime.fromtimestamp(int(m.group(1)), TZ), "url_epoch"
            if new_pub is None:  # keep the stored date (URL/sitemap fallback) when the page has none
                new_pub = parse_dt(pub)
            if new_pub and pub and new_pub.isoformat() != pub:
                counts["date_changed"] += 1
            if not status and not in_window(new_pub):
                status = "out_of_range"
            scope = "all"
            if not status and s["scope"] == "section":
                scope = scope_match(rec["title"], rec["text"], url)
                status = None if scope else "out_of_scope"
            wc = len(rec["text"].split())
            th = text_hash(rec["text"]) if rec["text"] else None
            if not status and wc >= 50 and db.execute(
                    "SELECT 1 FROM articles WHERE text_hash=? AND source_id=? AND id<>?", (th, s["id"], aid)).fetchone():
                status = "dup_text"
            with db:
                if status:
                    db.execute("DELETE FROM live_entries WHERE parent_id=?", (aid,))
                    db.execute("DELETE FROM articles WHERE id=?", (aid,))
                    db.execute("UPDATE candidates SET status=?, note='reextract' WHERE url=?", (status, url))
                    counts[status] += 1
                    continue
                db.execute("UPDATE articles SET published_at=?, title=?, subtitle=?, text=?, word_count=?, text_hash=?, scope_match=?,"
                           " content_type=?, content_type_signals=?, authors=?, author_orgs=?, author_raw=?,"
                           " agency_credit=?, site_section=?, tags=? WHERE id=?",
                           (new_pub.isoformat(), rec["title"], rec["subtitle"], rec["text"], wc, th, scope, rec["content_type"],
                            json.dumps(rec["content_type_signals"], ensure_ascii=False),
                            json.dumps(rec["authors"], ensure_ascii=False),
                            json.dumps(rec["author_orgs"], ensure_ascii=False), rec["author_raw"],
                            ", ".join(rec["agency_credit"]), rec["site_section"],
                            json.dumps(rec["tags"], ensure_ascii=False), aid))
                counts["updated"] += 1
        log.info("[%s] reextract (cumulative): %s", s["id"], dict(counts))
    return counts


DB_LOCK = threading.Lock()


def note_state(db, sid, **kw):
    with DB_LOCK, db:
        db.execute("INSERT OR IGNORE INTO source_state(source_id) VALUES(?)", (sid,))
        for k, v in kw.items():
            if k == "notes":
                db.execute("UPDATE source_state SET notes=coalesce(notes||' | ','')||? WHERE source_id=?", (v, sid))
            else:
                db.execute(f"UPDATE source_state SET {k}=? WHERE source_id=?", (v, sid))
        db.commit()


def canonical_ok(s, url, can):
    """Trust rel=canonical only if it is on the source and keeps the URL's numeric id (some CMSs point all pages to one URL)."""
    if not can or not matches_source(s, can):
        return False
    ids = re.findall(r"\d{4,}", unquote(urlsplit(url).path + urlsplit(url).query))
    return not ids or ids[-1] in unquote(can)


def text_hash(text):
    return hashlib.sha1(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()


# ----------------------------------------------------------------------------- pipeline

class Run:
    def __init__(self, dbpath, rawdir, dfrom, dto, sources, retry_failed=False, cap=None):
        self.cap = cap
        self.dbpath, self.rawdir, self.dfrom, self.dto = dbpath, rawdir, dfrom, dto
        self.sources, self.retry_failed = sources, retry_failed
        self.gdelt_ready = threading.Event()
        self.db = connect(dbpath)

    def q1(self, sql, args=()):
        with DB_LOCK, self.db:
            return self.db.execute(sql, args).fetchone()

    def add_candidates(self, sid, found):
        """found: [(url, method, hint, wb_ts)]. Returns the number of new rows."""
        seen = {}
        for u, m, h, ts in found:
            seen.setdefault(canon(u), (u, sid, m, h, ts))
        with DB_LOCK, self.db:
            n0 = self.db.total_changes
            self.db.executemany("INSERT OR IGNORE INTO candidates(url, source_id, method, hint, wb_ts) VALUES(?,?,?,?,?)",
                                list(seen.values()))
            self.db.commit()
            return self.db.total_changes - n0

    def discover(self, s):
        """Primary routes only: sitemaps, APIs and ID walks, plus GDELT where those are thin. The candidates are
        stored here so fetching starts at once; Wayback CDX runs afterwards as a supplement (cdx_pass)."""
        sid = s["id"]
        row = self.q1("SELECT discovered FROM source_state WHERE source_id=?", (sid,))
        if row and row[0]:
            return
        found = []
        for spec in s.get("discovery", []):
            try:
                fn = {"sitemap_template": sitemap_template, "sitemap_index": sitemap_index, "id_range": id_range,
                      "wayback_captures": wayback_captures}[spec["type"]]
                got = fn(s, spec, self.dfrom, self.dto)
                found += [(u, "id_range" if spec["type"] == "id_range" else "sitemap", h, None) for u, h in got]
            except Exception as e:
                note_state(self.db, sid, notes=f"sitemap failed: {e}"[:300])
                log.warning("[%s] sitemap failed: %s", sid, e)
        if len({u for u, *_ in found}) < MIN_SITEMAP_HITS or s.get("union_fallback"):
            if s.get("discovery"):
                note_state(self.db, sid, notes=f"sitemap yielded {len(found)} URLs; adding GDELT, then Wayback CDX")
            self.gdelt_ready.wait()
            found += [(u, "gdelt", h, None) for u, h in gdelt_discover(s, self.db, self.dfrom, self.dto)]
        method = "+".join(sorted({m for _, m, *_ in found})) or "none"  # union of every route that returned URLs
        self.add_candidates(sid, found)
        n = self.q1("SELECT count(*) FROM candidates WHERE source_id=?", (sid,))[0]
        note_state(self.db, sid, discovered=1, discovery_method=method, n_candidates=n)
        log.info("[%s] discovery: %s -> %d candidate URLs", sid, method, n)

    def needs_cdx(self, s):
        """CDX supplements every source whose primary routes are not known to be complete."""
        m = (self.q1("SELECT discovery_method FROM source_state WHERE source_id=?", (s["id"],)) or [None])[0]
        return bool(s.get("union_fallback")) or m not in ("sitemap", "id_range")

    def cdx_pass(self, s):
        """One pass over the source's CDX queries. Every outcome is recorded in cdx_days, so a failed day is
        retried later and is never a silent hole. Returns (new URLs, queries still to retry)."""
        sid = s["id"]
        jobs = cdx_jobs(s, self.dfrom, self.dto)
        with DB_LOCK, self.db:
            self.db.executemany("INSERT OR IGNORE INTO cdx_days(source_id, prefix, day) VALUES(?,?,?)",
                                [(sid, p, k) for p, k, _ in jobs])
            self.db.commit()
            seen = {(p, k): (st, t) for p, k, st, t in self.db.execute(
                "SELECT prefix, day, status, tries FROM cdx_days WHERE source_id=?", (sid,))}
        new = left = streak = 0
        for p, k, day in jobs:
            status, tries = seen[(p, k)]
            if status == "ok" or tries >= CDX_MAX_TRIES:
                continue
            if streak >= CDX_FAIL_STREAK:  # stays 'pending'/'failed' in cdx_days for the next pass
                left += 1
                continue
            rows, st = cdx_query(p, day)
            if rows is None:
                streak += 1
                left += tries + 1 < CDX_MAX_TRIES
                log.warning("[%s] CDX %s %s -> %s (try %d/%d)", sid, p, k, st, tries + 1, CDX_MAX_TRIES)
            else:
                streak = 0
                new += self.add_candidates(sid, [(u, "wayback_cdx", ts[:8], ts) for u, ts in rows
                                                 if is_article(s, u) and url_date_ok(u, self.dfrom, self.dto)])
            with DB_LOCK, self.db:
                self.db.execute("UPDATE cdx_days SET status=?, http=?, n_rows=?, tries=tries+1, updated_at=? "
                                "WHERE source_id=? AND prefix=? AND day=?",
                                ("failed" if rows is None else "ok", st, None if rows is None else len(rows),
                                 datetime.now(timezone.utc).isoformat(timespec="seconds"), sid, p, k))
                self.db.commit()
        if new:
            note_state(self.db, sid, n_candidates=self.q1("SELECT count(*) FROM candidates WHERE source_id=?", (sid,))[0])
        if jobs:
            log.info("[%s] CDX pass: %d new URLs, %d queries left to retry", sid, new, left)
        return new, left

    def fetch_html(self, s, url, wb_ts, state):
        """Live first (unless blocked / robots), Wayback fallback. Returns (html, fetched_from, note)."""
        note = ""
        if not state["blocked"]:
            if not NET.allowed(url):
                note = "robots_disallow"
            else:
                st, body, final, hdr = NET.get(url, timeout=30, tries=3)
                if st == 200 and body and not is_challenge(st, body):
                    state["streak"] = 0
                    return decode(body, hdr), "live", final, ""
                note = f"live_http_{st}"
                if st == 0 or is_challenge(st, body):  # repeated timeouts = silent block
                    state["streak"] += 1
                    if state["streak"] >= BLOCK_STREAK:
                        state["blocked"] = True
                        note_state(self.db, s["id"], live_blocked=1,
                                   notes=f"live fetch blocked ({'timeouts' if st == 0 else f'HTTP {st}'} x{BLOCK_STREAK}); switched to Wayback only")
                        log.warning("[%s] live-blocked; switching to Wayback", s["id"])
        if wayback_available():
            st, body, hdr, ts = wayback_fetch(url, wb_ts)
            if st == 200 and body:
                return decode(body, hdr), "wayback", url, ts or ""
            note += f" wayback_http_{st}"
        return None, None, url, note.strip()

    def process_source(self, s):
        sid = s["id"]
        try:
            self.discover(s)
        except Exception as e:
            log.exception("[%s] discovery crashed", sid)
            note_state(self.db, sid, discovered=0, notes=f"discovery error: {e}"[:300])
            return
        st_row = self.q1("SELECT live_blocked FROM source_state WHERE source_id=?", (sid,))
        state = {"blocked": bool(s.get("live_blocked") or (st_row and st_row[0])), "streak": 0}
        if s.get("live_blocked"):
            note_state(self.db, sid, live_blocked=1)
        if not self.fetch_pending(s, state):  # primary routes first: never wait for CDX
            return
        if self.needs_cdx(s) and wayback_available():
            for i in range(CDX_PASSES):
                new, left = self.cdx_pass(s)
                if new and not self.fetch_pending(s, state):
                    return
                if not left:
                    break
                if i + 1 < CDX_PASSES:
                    time.sleep(CDX_PASS_WAIT)

    def fetch_pending(self, s, state):
        """Fetch every pending candidate of the source. Returns False when the size limit stops the run."""
        sid = s["id"]
        statuses = ("pending", "fetch_failed", "error") if self.retry_failed else ("pending",)
        q = f"SELECT url, method, hint, wb_ts FROM candidates WHERE source_id=? AND status IN ({','.join('?' * len(statuses))})"
        with DB_LOCK, self.db:
            todo = self.db.execute(q, (sid, *statuses)).fetchall()
        if s.get("sample_per_day") and state["blocked"]:
            todo = self.sample_by_day(s, todo, statuses)
        if self.cap and state["blocked"] and len(todo) > self.cap:
            import random
            random.Random(0).shuffle(todo)
            skipped = todo[self.cap:]
            todo = todo[:self.cap]
            with DB_LOCK, self.db:
                self.db.executemany("UPDATE candidates SET status='sampled_out' WHERE url=?", [(u,) for u, *_ in skipped])
            note_state(self.db, sid, notes=f"probe: Wayback-only source, fetched a random sample of {self.cap} URLs")
        log.info("[%s] %d URLs to fetch", sid, len(todo))
        counts = Counter()
        for i, (url, method, hint, wb_ts) in enumerate(todo):
            try:
                status, note = self.process_one(s, url, method, hint, wb_ts, state)
            except Exception as e:
                status, note = "error", repr(e)[:300]
            counts[status] += 1
            with DB_LOCK, self.db:
                self.db.execute("UPDATE candidates SET status=?, note=? WHERE url=?", (status, note, url))
                self.db.commit()
            if (i + 1) % 50 == 0:
                log.info("[%s] %d/%d %s", sid, i + 1, len(todo), dict(counts))
            if (i + 1) % 500 == 0 and self.over_size():
                log.error("Size limit reached (>20GB). Stopping.")
                return False
        log.info("[%s] finished: %s", sid, dict(counts))
        return True

    def sample_by_day(self, s, todo, statuses=("pending",)):
        """Wayback-only sources: random sample of up to N URLs per (hinted) day, so coverage is even.

        URLs already processed in earlier rounds count against the day's quota (CDX adds URLs later)."""
        import random
        rnd = random.Random(42)

        def day_of(url, hint, wb_ts):
            ud = url_date(url)
            return (ud.isoformat() if ud else (hint or (wb_ts or "")[:8] or "unknown"))[:10]

        with DB_LOCK, self.db:
            prev = self.db.execute(
                "SELECT url, hint, wb_ts FROM candidates WHERE source_id=? AND status NOT IN "
                f"('sampled_out',{','.join('?' * len(statuses))})", (s["id"], *statuses)).fetchall()
        taken = Counter(day_of(*r) for r in prev)
        by_day = defaultdict(list)
        for row in todo:
            url, method, hint, wb_ts = row
            by_day[day_of(url, hint, wb_ts)].append(row)
        keep, drop = [], []
        n = s["sample_per_day"]
        for day, rows in by_day.items():
            rnd.shuffle(rows)
            k = max(0, (n * 3 if day == "unknown" else n) - taken[day])
            keep += rows[:k]
            drop += rows[k:]
        with DB_LOCK, self.db:
            self.db.executemany("UPDATE candidates SET status='sampled_out' WHERE url=?", [(r[0],) for r in drop])
        note_state(self.db, s["id"], notes=f"Wayback-only: random sample of up to {n} URLs per day "
                                          f"({len(keep)} of {len(todo)} kept over {len(by_day)} day-buckets)")
        return keep

    def process_one(self, s, url, method, hint, wb_ts, state):
        html, fetched_from, final, note = self.fetch_html(s, url, wb_ts, state)
        if html is None:
            return "fetch_failed", note
        rec = extract(html, final, s)
        if rec["js_shell"] and fetched_from == "wayback" and not state["blocked"]:
            return "js_shell", "wayback snapshot is a JavaScript shell"
        if rec["js_shell"] and fetched_from == "live" and wayback_available():
            st, body, hdr, ts = wayback_fetch(url, wb_ts)
            if st == 200 and body:
                html, fetched_from = decode(body, hdr), "wayback"
                rec = extract(html, final, s)
        if rec["js_shell"]:
            return "js_shell", f"{fetched_from} page is a JavaScript shell"
        if s.get("require_arabic") and not re.search(r"[\u0600-\u06FF]", rec["title"] or ""):
            return "other_lang", ""
        pub = rec["published_at"]
        m = re.search(r"/(1[5-9]\d{8})-", unquote(url)) if s.get("epoch_url_date") else None
        if m:  # i24 article ids are unix timestamps; page metadata is sometimes a placeholder (2015-01-01)
            pub, rec["date_source"] = datetime.fromtimestamp(int(m.group(1)), TZ), "url_epoch"
        if pub is None and hint and method == "sitemap" and url_date(url):
            pub, rec["date_source"] = datetime.combine(date.fromisoformat(hint), datetime.min.time(), TZ), "url"
        if pub is None:
            return "no_date", ""
        if not in_window(pub):
            return "out_of_range", pub.isoformat()
        scope = "all"
        if s["scope"] == "section":
            scope = scope_match(rec["title"], rec["text"], url)
            if not scope:
                return "out_of_scope", ""
        wc = len(rec["text"].split())
        th = text_hash(rec["text"]) if rec["text"] else None
        can = canon(rec["canonical_url"] if canonical_ok(s, url, rec["canonical_url"]) else url)
        with DB_LOCK, self.db:
            if self.db.execute("SELECT 1 FROM articles WHERE canonical_url=?", (can,)).fetchone():
                return "dup_url", can
            # text dedup only WITHIN a source: cross-outlet syndication is data (logged in cross_source_duplicates)
            if wc >= 50 and self.db.execute("SELECT 1 FROM articles WHERE text_hash=? AND source_id=?",
                                            (th, s["id"])).fetchone():
                return "dup_text", th
            cur = self.db.execute(
                "INSERT INTO articles(source_id,url,canonical_url,title,subtitle,text,published_at,updated_at,date_source,"
                "language,category,scope_match,content_type,content_type_signals,authors,author_orgs,author_raw,agency_credit,"
                "site_section,tags,discovery_method,fetched_from,is_truncated,word_count,text_hash,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s["id"], url, can, rec["title"], rec["subtitle"], rec["text"], pub.isoformat(),
                 rec["updated_at"].isoformat() if rec["updated_at"] else None, rec["date_source"],
                 s["language"], s["category"], scope, rec["content_type"],
                 json.dumps(rec["content_type_signals"], ensure_ascii=False),
                 json.dumps(rec["authors"], ensure_ascii=False), json.dumps(rec["author_orgs"], ensure_ascii=False),
                 rec["author_raw"], ", ".join(rec["agency_credit"]),
                 rec["site_section"], json.dumps(rec["tags"], ensure_ascii=False), method, fetched_from,
                 int(rec["is_truncated"]), wc, th, datetime.now(timezone.utc).isoformat()))
            aid = cur.lastrowid
            self.db.executemany("INSERT INTO live_entries(parent_id, entry_time, text, author) VALUES(?,?,?,?)",
                                [(aid, e["entry_time"].isoformat(), e["text"], e["author"])
                                 for e in rec["live_entries"] if in_window(e["entry_time"])])
            self.db.commit()
        d = self.rawdir / s["id"]
        d.mkdir(parents=True, exist_ok=True)
        with gzip.open(d / f"{aid}.html.gz", "wt", encoding="utf-8") as f:
            f.write(html)
        return "done", note

    def over_size(self):
        total = sum(f.stat().st_size for f in self.rawdir.rglob("*.gz")) + Path(self.dbpath).stat().st_size
        return total > SIZE_LIMIT

    def run(self, check_size=True):
        need_gdelt = [s for s in self.sources
                      if not (self.q1("SELECT discovered FROM source_state WHERE source_id=?", (s["id"],)) or [0])[0]]

        def gd():
            try:
                build_gdelt(self.db, self.sources, self.dfrom, self.dto)
            except Exception:
                log.exception("GDELT build failed")
            finally:
                self.gdelt_ready.set()
        if need_gdelt:
            threading.Thread(target=gd, daemon=True).start()
        else:
            self.gdelt_ready.set()
        if check_size:
            # estimate from sources already discovered, scaled to all sources; slow Wayback discovery
            # must not block fetching of ready sources (over_size() still guards during fetch)
            n_disc = self.q1("SELECT count(*) FROM source_state WHERE discovered=1")[0] or 1
            pending = self.q1("SELECT count(*) FROM candidates WHERE status='pending'")[0]
            est = pending * avg_item_bytes() * len(self.sources) / n_disc
            log.info("Estimated storage (%d pending URLs from %d discovered sources, scaled to %d): %.1f GB",
                     pending, n_disc, len(self.sources), est / 1024 ** 3)
            if est > SIZE_LIMIT and not ARGS.force_size:
                log.error("Estimated size %.1f GB > 20 GB. Stopping before fetch; rerun with --force-size to continue.",
                          est / 1024 ** 3)
                return
        with ThreadPoolExecutor(len(self.sources)) as ex:
            list(ex.map(self.process_source, self.sources))

    def safe_discover(self, s):
        try:
            self.discover(s)
        except Exception as e:
            log.exception("[%s] discovery crashed", s["id"])
            note_state(self.db, s["id"], notes=f"discovery error: {e}"[:300])


def avg_item_bytes():
    """Average stored bytes per candidate URL, from the probe if available (raw gz + ~2x text in db)."""
    p = ROOT / "probe.db"
    if p.exists():
        db = sqlite3.connect(p)
        n_c = db.execute("SELECT count(*) FROM candidates").fetchone()[0]
        txt = db.execute("SELECT coalesce(sum(length(text)),0) FROM articles").fetchone()[0]
        db.close()
        raw = sum(f.stat().st_size for f in (ROOT / "raw_probe").rglob("*.gz"))
        if n_c:
            return (raw + 2 * txt) / n_c
    return 80_000


# ----------------------------------------------------------------------------- reports

def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def source_stats(db, sid):
    st = dict(db.execute("SELECT status, count(*) FROM candidates WHERE source_id=? GROUP BY status", (sid,)).fetchall())
    rows = db.execute("SELECT content_type, authors, agency_credit, word_count, date_source, fetched_from, title, text, "
                      "scope_match, published_at, is_truncated FROM articles WHERE source_id=?", (sid,)).fetchall()
    return st, rows


def probe_report(db, sources, day):
    lines = [f"# Probe report — sample day {day}", "",
             f"Generated {datetime.now(TZ):%Y-%m-%d %H:%M} (Asia/Jerusalem). Window kept: {START:%Y-%m-%d} .. {END:%Y-%m-%d}.",
             "", "Columns: URLs = candidate URLs discovered for the day; fetched = pages downloaded (live/wayback); "
             "extract OK = stored items with >=30 words (or video/infographic); date OK = publish date read from the page "
             "(JSON-LD/meta tags, or the visible 'تاريخ النشر' line; i24: unix-time id in URL) — not htmldate guess, URL or sitemap; scope = kept/(kept+out_of_scope) for section sources.", "",
             "| # | source | status | discovery | URLs | fetched | stored | extract OK | date OK | scope kept | content types | author | agency | live/wb | notes |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    samples = []
    for i, s in enumerate(sources, 1):
        sid = s["id"]
        ss = db.execute("SELECT discovery_method, n_candidates, live_blocked, notes FROM source_state WHERE source_id=?",
                        (sid,)).fetchone() or (None, 0, 0, "")
        st, rows = source_stats(db, sid)
        n_url = sum(st.values())
        fetched = n_url - st.get("fetch_failed", 0) - st.get("pending", 0) - st.get("error", 0) - st.get("sampled_out", 0)
        n = len(rows)
        ok = sum(1 for r in rows if (r[3] or 0) >= 30 or r[0] in ("video", "infographic"))
        dok = sum(1 for r in rows if r[4] in ("metadata", "visible", "url_epoch"))
        dates_attempted = fetched - st.get("no_date", 0)
        ct = Counter(r[0] for r in rows)
        auth = sum(1 for r in rows if r[1] and r[1] != "[]")
        ag = sum(1 for r in rows if r[2])
        src = Counter(r[5] for r in rows)
        scope = (f"{st.get('done', 0)}/{st.get('done', 0) + st.get('out_of_scope', 0)}" if s["scope"] == "section" else "all")
        if n == 0:
            status = "FAILED"
        elif ok / n < 0.6 or dok / n < 0.5 or s.get("live_blocked") or (ss and ss[2]):
            status = "partial"
        else:
            status = "ok"
        fails = Counter((x or "").split()[0] for (x,) in db.execute(
            "SELECT note FROM candidates WHERE source_id=? AND status='fetch_failed'", (sid,)))
        notes = "; ".join(filter(None, [ss[3] or "", ("fetch failures: " + ", ".join(f"{k}×{v}" for k, v in fails.most_common(3))) if fails else "",
                                        f"no_date×{st['no_date']}" if st.get("no_date") else "",
                                        f"out_of_range×{st['out_of_range']}" if st.get("out_of_range") else ""]))
        lines.append(f"| {i} | {sid} ({s['domain']}) | {status} | {ss[0] or '-'} | {n_url} | {fetched} | {n} | {pct(ok, n)} | "
                     f"{pct(dok, n)} | {scope} | {', '.join(f'{k}:{v}' for k, v in ct.most_common())} | {pct(auth, n)} | "
                     f"{pct(ag, n)} | {src.get('live', 0)}/{src.get('wayback', 0)} | {notes.replace('|', '/')[:400]} |")
        if rows:
            r = max(rows, key=lambda r: r[3] or 0)
            samples.append(f"**{sid}** — {r[6]}  \n> {(r[7] or '')[:200].replace(chr(10), ' ')}  \n"
                           f"published_at={r[9]} scope_match={r[8]}")
        scopes = Counter((r[8] or "").split(":")[0] for r in rows)
        if s["scope"] == "section" and rows:
            samples[-1] += f"  \nscope rules: {dict(scopes)}"
    lines += ["", "## Samples (longest stored item per source)", ""] + [x + "\n" for x in samples]
    return "\n".join(lines)


def quality_report(db, sources):
    lines = [f"# Quality report", "", f"Generated {datetime.now(TZ):%Y-%m-%d %H:%M} (Asia/Jerusalem). "
             f"Window {START:%Y-%m-%d} .. {END:%Y-%m-%d}. Tokens estimated as words × 1.4.", ""]
    days = [(START.date() + timedelta(days=i)) for i in range((END.date() - START.date()).days + 1)]
    lines += ["## Per source", "", "| source | items | live entries | content types | author | agency | empty/short (<50w) | "
              "date-parse fail | dup removed | words p10/p50/p90 | est. tokens | zero-item days |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    total, total_tok, failed = 0, 0, []
    perday_lines = ["## Items per source per day", "", "| source | " + " | ".join(d.strftime("%m-%d") for d in days) + " |",
                    "|---|" + "---|" * len(days)]
    for s in sources:
        sid = s["id"]
        st, rows = source_stats(db, sid)
        n = len(rows)
        total += n
        le = db.execute("SELECT count(*) FROM live_entries le JOIN articles a ON a.id=le.parent_id WHERE a.source_id=?",
                        (sid,)).fetchone()[0]
        ct = Counter(r[0] for r in rows)
        wcs = sorted(r[3] or 0 for r in rows)
        q = (lambda p: wcs[min(len(wcs) - 1, int(p * len(wcs)))] if wcs else 0)
        tok = int(sum(wcs) * 1.4)
        total_tok += tok
        short = sum(1 for w in wcs if w < 50)
        attempted = sum(v for k, v in st.items() if k not in ("pending", "fetch_failed", "error", "sampled_out"))
        perday = Counter(local_day(parse_dt(r[9])) for r in rows)
        zero = [d.strftime("%m-%d") for d in days if not perday.get(d)]
        perday_lines.append(f"| {sid} | " + " | ".join(str(perday.get(d, 0)) for d in days) + " |")
        lines.append(f"| {sid} | {n} | {le} | {', '.join(f'{k}:{v}' for k, v in ct.most_common())} | "
                     f"{pct(sum(1 for r in rows if r[1] and r[1] != '[]'), n)} | {pct(sum(1 for r in rows if r[2]), n)} | "
                     f"{pct(short, n)} | {pct(st.get('no_date', 0), attempted)} | "
                     f"{st.get('dup_url', 0) + st.get('dup_text', 0)} | {q(.1)}/{q(.5)}/{q(.9)} | {tok:,} | "
                     f"{len(zero)}{': ' + ', '.join(zero[:8]) + ('…' if len(zero) > 8 else '') if zero else ''} |")
        ss = db.execute("SELECT discovery_method, live_blocked, notes FROM source_state WHERE source_id=?", (sid,)).fetchone()
        ff = st.get("fetch_failed", 0) + st.get("error", 0)
        if n == 0 or len(zero) > 5 or (ss and ss[1]) or ff > 0.2 * max(sum(st.values()), 1):
            failed.append(f"- **{sid}**: {'FAILED' if n == 0 else 'partial'} — items={n}, zero days={len(zero)}, "
                          f"fetch failures={ff}, live_blocked={bool(ss and ss[1])}, discovery={ss and ss[0]}; {(ss and ss[2]) or ''}")
    lines[4:4] = [f"**Total items: {total:,}** — estimated tokens: {total_tok:,}", ""] + share_tables(db, sources)
    lines += [""] + perday_lines + ["", "## Failed or partial sources", ""] + (failed or ["- none"])
    holes = db.execute("SELECT source_id, count(*), sum(status='ok'), group_concat(DISTINCT CASE WHEN status!='ok' THEN day END) "
                       "FROM cdx_days GROUP BY 1 HAVING sum(status!='ok') > 0 ORDER BY 1").fetchall()
    lines += ["", "## Wayback CDX queries that never succeeded (possible coverage holes)", ""] + (
        [f"- **{sid}**: {n - ok} of {n} queries not answered; days: {days}" for sid, n, ok, days in holes] or ["- none"])
    return "\n".join(lines)


CATEGORY_NAMES = {"palestinian_local": "Palestinian", "arab": "Arab", "international": "International",
                  "israeli": "Israeli"}


def share_tables(db, sources):
    """Share of items and tokens per source and per category, plus the collection route used."""
    agg = {r[0]: (r[1], r[2] or 0) for r in db.execute(
        "SELECT source_id, count(*), sum(word_count) FROM articles GROUP BY source_id")}
    routes = defaultdict(Counter)
    for sid, m, f, n in db.execute("SELECT source_id, discovery_method, fetched_from, count(*) FROM articles "
                                   "GROUP BY 1, 2, 3"):
        routes[sid][f"{m}→{f}"] += n
    notes = {r[0]: r[1] or "" for r in db.execute("SELECT source_id, notes FROM source_state")}
    n_all = sum(v[0] for v in agg.values()) or 1
    w_all = sum(v[1] for v in agg.values()) or 1
    out = ["## Share per category", "", "| category | sources | items | % items | est. tokens | % tokens |",
           "|---|---|---|---|---|---|"]
    by_cat = defaultdict(lambda: [0, 0, 0])
    for s in sources:
        n, w = agg.get(s["id"], (0, 0))
        c = by_cat[CATEGORY_NAMES.get(s["category"], s["category"])]
        c[0] += 1; c[1] += n; c[2] += w
    for cat, (k, n, w) in by_cat.items():
        out.append(f"| {cat} | {k} | {n:,} | {100 * n / n_all:.1f}% | {int(w * 1.4):,} | {100 * w / w_all:.1f}% |")
    out += ["", "## Share and collection route per source", "",
            "| source | category | items | % items | est. tokens | % tokens | route (discovery→fetch: items) | sampling |",
            "|---|---|---|---|---|---|---|---|"]
    for s in sources:
        n, w = agg.get(s["id"], (0, 0))
        route = ", ".join(f"{k}: {v:,}" for k, v in routes[s["id"]].most_common())
        samp = f"sample ≤{s['sample_per_day']}/day" if s.get("sample_per_day") and "sample" in notes.get(s["id"], "") else "full"
        out.append(f"| {s['id']} | {CATEGORY_NAMES.get(s['category'], s['category'])} | {n:,} | {100 * n / n_all:.1f}% | "
                   f"{int(w * 1.4):,} | {100 * w / w_all:.1f}% | {route or '-'} | {samp} |")
    return out + [""]


# ----------------------------------------------------------------------------- CLI

def setup_logging(name):
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(ROOT / "logs" / f"{name}.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    for noisy in ("trafilatura", "htmldate", "newspaper", "urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


ARGS = None


def main():
    global ARGS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["probe", "pull", "report", "reextract"])
    ap.add_argument("--day", default="2023-10-10")
    ap.add_argument("--sources", help="comma-separated source ids")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--force-size", action="store_true")
    ap.add_argument("--probe", action="store_true", help="report: regenerate probe_report.md")
    ARGS = ap.parse_args()
    setup_logging(ARGS.cmd)
    sources = load_sources(ARGS.sources)
    log.info("archive proxy: %s", ARCHIVE_PROXY or "none")
    if ARGS.cmd == "probe":
        day = date.fromisoformat(ARGS.day)
        Run(ROOT / "probe.db", ROOT / "raw_probe", day, day, sources, ARGS.retry_failed, cap=150).run(check_size=False)
        db = sqlite3.connect(ROOT / "probe.db")
        (ROOT / "probe_report.md").write_text(probe_report(db, load_sources(), day), "utf-8")
        log.info("wrote probe_report.md")
    elif ARGS.cmd == "pull":
        Run(ROOT / "articles.db", ROOT / "raw", START.date(), END.date(), sources, ARGS.retry_failed).run()
        db = sqlite3.connect(ROOT / "articles.db")
        (ROOT / "quality_report.md").write_text(quality_report(db, load_sources()), "utf-8")
        log.info("wrote quality_report.md")
    elif ARGS.cmd == "reextract":
        log.info("reextract total: %s", dict(reextract(ROOT / "articles.db", ROOT / "raw", sources)))
    else:
        if ARGS.probe:
            db = sqlite3.connect(ROOT / "probe.db")
            (ROOT / "probe_report.md").write_text(probe_report(db, load_sources(), date.fromisoformat(ARGS.day)), "utf-8")
        else:
            db = sqlite3.connect(ROOT / "articles.db")
            (ROOT / "quality_report.md").write_text(quality_report(db, load_sources()), "utf-8")


if __name__ == "__main__":
    main()

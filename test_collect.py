"""Smallest checks for the parsing logic: python -m pytest -q test_collect.py  (or python test_collect.py)."""
from datetime import date

import collect as c

SRC = {"id": "x", "name": "Example News", "domain": "example.ps", "url_prefixes": ["example.ps/"], "scope": "section",
       "language": "ar", "category": "t"}


def test_url_date():
    assert c.url_date("https://www.wafa.ps/news/2023/10/10/slug") == date(2023, 10, 10)
    assert c.url_date("https://www.haaretz.com/opinion/2023-10-01/ty-article/x") == date(2023, 10, 1)
    assert c.url_date("https://www.france24.com/ar/x/20231027-slug") == date(2023, 10, 27)
    assert c.url_date("https://www.reuters.com/world/a-b-2023-10-10/") == date(2023, 10, 10)
    assert c.url_date("https://safa.ps/post/12345/slug") is None


def test_sitemap_and_selection():
    xml = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
      xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">""" + b"".join(
        f"<url><loc>https://example.ps/post/{i}/s</loc><lastmod>2023-10-{10 if i != 5 else 30}T10:00:00+03:00</lastmod>"
        f"<image:image><image:loc>https://example.ps/i.jpg</image:loc></image:image></url>".encode() for i in range(11)
    ) + b"</urlset>"
    is_index, entries = c.parse_sitemap(xml, "https://example.ps/sitemap.xml")
    assert not is_index and len(entries) == 11 and entries[0][0] == "https://example.ps/post/0/s"
    keep = c.select_entries(SRC, entries, date(2023, 10, 10), date(2023, 10, 10), ordered=True)
    # post 5 was modified later, but its neighbours place it on 10-10
    assert len(keep) == 11


def test_scope_and_agency():
    assert c.scope_match("قصف على غزة", "", "https://x/a") == "title:غزة"
    assert c.scope_match("Weather", "nothing", "https://x/palestine/1") == "url:/palestine"
    assert c.scope_match("كرة القدم", "مباراة", "https://x/sport/1") is None
    assert c.scope_match("رياضة", "إسرائيل ثم إسرائيل ثم غزة", "https://x/a").startswith("body:3")
    assert c.scope_match("مقال في القدس العربي", "", "https://x/a") is None
    assert c.agency_credits("", "رام الله 10-10-2023 وفا- استشهد") == ["WAFA"]
    assert c.agency_credits("", "بعد وفاة الشاب") == []
    assert "Reuters" in c.agency_credits("By Reuters", "")
    assert c.agency_credits("", "عملنا معا من أجل") == []


def test_extract_liveblog_and_types():
    html = """<html><head><link rel="canonical" href="/live/1"><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"LiveBlogPosting","headline":"تغطية مباشرة",
     "datePublished":"2023-10-10T08:00:00+03:00","author":[{"@type":"Person","name":"أحمد"},{"@type":"Organization","name":"Example News"}],
     "liveBlogUpdate":[{"@type":"BlogPosting","datePublished":"2023-10-10T09:00:00+03:00","articleBody":"تحديث أول"},
                       {"@type":"BlogPosting","datePublished":"2023-12-01T09:00:00+02:00","articleBody":"خارج النطاق"}]}
    </script></head><body><article><p>""" + "نص الخبر " * 40 + """</p></article></body></html>"""
    r = c.extract(html, "https://example.ps/live/1", SRC)
    assert r["content_type"] == "live_blog" and r["title"] == "تغطية مباشرة"
    assert r["published_at"].isoformat() == "2023-10-10T08:00:00+03:00" and r["date_source"] == "metadata"
    assert r["authors"] == ["أحمد"]
    assert len(r["live_entries"]) == 2 and sum(c.in_window(e["entry_time"]) for e in r["live_entries"]) == 1
    assert r["canonical_url"] == "https://example.ps/live/1"
    ct, sig = c.content_type(None, None, {}, "https://example.ps/آراء/123", "")
    assert ct == "opinion" and sig["url_path"] == ["opinion"]
    assert c.content_type({"@type": "NewsArticle"}, None, {}, "https://e/x/1", "أخبار")[0] == "news"


def test_visible_date():
    from lxml import html as lh
    t = lh.fromstring('<div><span>تاريخ النشر: 10/10/2023 12:17 م</span></div>')
    assert c.visible_date(t).isoformat() == "2023-10-10T12:17:00+03:00"
    t = lh.fromstring('<div><span>نشر بتاريخ 08/11/2023 09:05 ص</span></div>')
    assert c.visible_date(t).isoformat() == "2023-11-08T09:05:00+02:00"


def test_canon():
    assert c.canon("http://www.Example.ps/a/b/?utm_source=x&id=3#f") == "https://example.ps/a/b?id=3"


def test_canonical_ok():
    s = {"url_prefixes": ["alhaya.ps/"]}
    assert not c.canonical_ok(s, "https://www.alhaya.ps/ar/Article/152021", "https://alhaya.ps/ar/Article")
    assert c.canonical_ok(s, "https://www.alhaya.ps/ar/Article/152021", "https://alhaya.ps/ar/Article/152021/slug")
    assert not c.canonical_ok(s, "https://alhaya.ps/ar/Article/1", "https://other.com/x")


def test_authors_and_body():
    s = {"name": "Asharq Al-Awsat", "domain": "aawsat.com", "org_aliases": ["الشرق الأوسط"]}
    p, o = c.split_authors([("«الشرق الأوسط»", None), ("كفاح زبون", "Person"), ("Reuters", None), ("ToI Staff", None)], s)
    assert p == ["كفاح زبون"] and len(o) == 3
    from lxml import html as lh
    t = lh.fromstring("<html><body><article><p>نص المقال الأول هنا</p></article><div class='related'><p>خبر آخر</p></div></body></html>")
    assert "خبر آخر" not in c.body_text(t, ["(//article)[1]"])
    assert c.BYLINE_LINE.match(" - Author, إيهاب الألفي") and not c.BYLINE_LINE.match("شنت حماس هجوما")


def test_dayfirst_dates():
    assert c.parse_dt("11/10/2023 14:05").date().isoformat() == "2023-10-11"
    assert c.parse_dt("2023-10-11T14:05:00+03:00").date().isoformat() == "2023-10-11"


def test_cdx_supplement_records_and_retries(tmp_path=None):
    import tempfile
    from pathlib import Path
    d = Path(tmp_path or tempfile.mkdtemp())
    src = dict(SRC, cdx_prefixes=["example.ps/{Y}/{M}/{D}/"], cdx_once=["example.ps/post/16"])
    jobs = c.cdx_jobs(src, date(2023, 10, 7), date(2023, 10, 9))
    assert jobs[0] == ("example.ps/post/16", "all", None) and jobs[1] == ("example.ps/2023/10/07/", "2023-10-07", None)
    assert len(c.cdx_jobs(SRC, date(2023, 10, 7), date(2023, 10, 9))) == 4  # capture-day queries run one day past dto
    run = c.Run(d / "t.db", d / "raw", date(2023, 10, 7), date(2023, 10, 9), [src])
    real, down = c.cdx_query, [True]
    c.cdx_query = lambda pre, day=None: (None, 504) if down[0] else ([(f"https://{pre}story-a", "20231008120000")], 200)
    try:
        assert run.cdx_pass(src) == (0, 4)  # server down: every query recorded as failed and left to retry
        assert run.q1("SELECT count(*), sum(tries) FROM cdx_days WHERE status='failed'") == (4, 4)
        down[0] = False
        assert run.cdx_pass(src) == (4, 0)  # second pass retries the failed queries and stores the URLs
        assert run.q1("SELECT count(*) FROM cdx_days WHERE status!='ok'")[0] == 0
        assert run.q1("SELECT count(*) FROM candidates WHERE method='wayback_cdx' AND status='pending'")[0] == 4
        assert run.cdx_pass(src) == (0, 0)  # nothing is queried twice
    finally:
        c.cdx_query = real


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
    print("ok")

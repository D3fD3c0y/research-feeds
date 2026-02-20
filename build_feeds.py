#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generates one RSS feed per source in sources.json.

Modes per source:
  - "auto"    -> try to discover an official feed via <link rel="alternate"...>,
                 then try common suffixes, else scrape.
  - "scrape"  -> skip feed discovery and scrape directly.
  - "sitemap" -> read sitemap(s) to discover recent URLs, then extract metadata.

Notes:
- Designed for GitHub Actions on a schedule (UTC).
- Writes feeds to feeds/<slug>.xml and an index.html listing.
"""

import json, os, re, time, datetime, email.utils
from urllib.parse import urljoin, urlparse
import urllib.robotparser as robotparser

import requests
from bs4 import BeautifulSoup
import feedparser

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
USER_AGENT = "Mozilla/5.0 (compatible; ResearchFeedsBot/1.0; +https://github.com)"
TIMEOUT = 20                  # per-request timeout (seconds)
MAX_ITEMS_PER_SOURCE = 20     # number of items to emit per feed
MAX_ARTICLE_FETCHES = 10      # cap article-page fetches per source
RESPECT_ROBOTS = True         # honor robots.txt (recommended)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

COMMON_SUFFIXES = [
    "feed", "feed/", "rss", "rss.xml", "atom.xml", "index.xml", "feed.xml",
    "?feed=rss2", "?format=feed"
]

# ---------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------
def rfc2822(dt: datetime.datetime | None = None) -> str:
    """
    Return an RFC 2822 date string in UTC (tz-aware).
    Required by email.utils.format_datetime(..., usegmt=True) in Python 3.11+.
    """
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return email.utils.format_datetime(dt, usegmt=True)

# ---------------------------------------------------------------------
# HTTP + robots
# ---------------------------------------------------------------------
def get(url: str):
    return requests.get(url, headers=HEADERS, timeout=TIMEOUT)

def robots_allows(page_url: str, agent: str = "ResearchFeedsBot") -> bool:
    if not RESPECT_ROBOTS:
        return True
    try:
        parsed = urlparse(page_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(agent, page_url)
    except Exception:
        # If robots can't be read, allow; we'll still gracefully handle 403/429 downstream
        return True

# ---------------------------------------------------------------------
# Feed discovery (auto mode)
# ---------------------------------------------------------------------
def discover_feed_urls(page_url: str):
    """Return a list of candidate feed URLs discovered from the page plus common patterns."""
    cands: list[str] = []
    try:
        resp = get(page_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.select('link[rel="alternate"]'):
            t = (link.get("type") or "").lower()
            if any(x in t for x in ["rss", "atom", "xml", "json"]):
                href = link.get("href")
                if href:
                    cands.append(urljoin(page_url, href))
    except Exception:
        pass  # continue with suffix checks

    # Add common suffixes against the page base
    base = page_url.rstrip("/")
    for sfx in COMMON_SUFFIXES:
        cands.append(urljoin(base + "/", sfx))

    # Also try canonical feed paths on the host root and apex (handles www vs apex)
    parsed = urlparse(page_url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc
    apex = host[4:] if host.startswith("www.") else host
    root_base = f"{scheme}://{host}"
    apex_base = f"{scheme}://{apex}"
    for url in (
        f"{root_base}/feed", f"{root_base}/blog/feed",
        f"{apex_base}/feed", f"{apex_base}/blog/feed",
    ):
        cands.append(url)

    # Special case: Medium (officially supports /feed on profiles/publications)
    host_l = parsed.netloc.lower()
    if "medium.com" in host_l or host_l.endswith(".medium.com"):
        cands.insert(0, urljoin(base + "/", "feed"))

    # De-duplicate preserving order
    uniq, seen = [], set()
    for u in cands:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

def validate_feed(url: str):
    d = feedparser.parse(url)
    if d.bozo or not d.entries:
        return None
    return d

# ---------------------------------------------------------------------
# Article metadata extraction helpers
# ---------------------------------------------------------------------
def _parse_jsonld_articles(soup: BeautifulSoup, base_url: str):
    """Return items from JSON-LD Article/BlogPosting if present."""
    items = []
    for tag in soup.find_all("script", attrs={"type": ["application/ld+json", "application/json"]}):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                t = obj.get("@type") or ""
                if isinstance(t, list):
                    t = ",".join(t)
                if any(x in str(t) for x in ["Article", "BlogPosting", "NewsArticle"]):
                    title = obj.get("headline") or obj.get("name") or ""
                    link  = obj.get("url") or base_url
                    date  = obj.get("datePublished") or obj.get("dateModified") or ""
                    desc  = obj.get("description") or ""
                    if title:
                        items.append({
                            "title": title,
                            "link": urljoin(base_url, link),
                            "pubDate": date,
                            "description": desc
                        })
                for v in obj.values():
                    stack.append(v)
            elif isinstance(obj, list):
                stack.extend(obj)
    return items

_DATE_URL_PAT = re.compile(r"(?P<y>20\d{2})[/-](?P<m>\d{1,2})[/-](?P<d>\d{1,2})")

def _guess_rfc2822_from_text(s: str | None):
    if not s:
        return None
    m = _DATE_URL_PAT.search(s)
    if not m:
        return None
    try:
        dt = datetime.datetime(int(m["y"]), int(m["m"]), int(m["d"]), tzinfo=datetime.timezone.utc)
        return rfc2822(dt)
    except Exception:
        return None

def _extract_article_meta(article_url: str) -> dict | None:
    """Fetch article page and extract title/date/description best-effort."""
    try:
        r = get(article_url)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")

        # Prefer JSON-LD Article info
        items = _parse_jsonld_articles(soup, article_url)
        if items:
            item = items[0]
            # Normalize date to RFC 2822 if possible
            if item.get("pubDate"):
                try:
                    dt = datetime.datetime.fromisoformat(item["pubDate"].replace("Z", "+00:00"))
                    item["pubDate"] = rfc2822(dt)
                except Exception:
                    guess = _guess_rfc2822_from_text(item["pubDate"])
                    item["pubDate"] = guess or rfc2822()
            else:
                item["pubDate"] = rfc2822()
            if not item.get("description"):
                md = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
                if md and md.get("content"):
                    item["description"] = md["content"]
            return item

        # Fallbacks: <time datetime>, og:published_time, title tag, URL date
        title = (soup.title.string.strip() if soup.title and soup.title.string else "")
        meta_title = soup.find("meta", attrs={"property": "og:title"})
        if meta_title and meta_title.get("content"):
            title = meta_title["content"].strip()

        dt_tag = soup.find("time", attrs={"datetime": True})
        iso_dt = dt_tag["datetime"].strip() if dt_tag else ""
        if not iso_dt:
            og = soup.find("meta", attrs={"property": "article:published_time"})
            if og and og.get("content"):
                iso_dt = og["content"].strip()
        if iso_dt:
            try:
                dt = datetime.datetime.fromisoformat(iso_dt.replace("Z", "+00:00"))
                pub = rfc2822(dt)
            except Exception:
                pub = _guess_rfc2822_from_text(iso_dt) or rfc2822()
        else:
            pub = _guess_rfc2822_from_text(article_url) or rfc2822()

        desc = ""
        md = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if md and md.get("content"):
            desc = md["content"].strip()
        if not title:
            title = article_url
        return {"title": title, "link": article_url, "pubDate": pub, "description": desc or title}
    except Exception:
        return None

# ---------------------------------------------------------------------
# Scraping (listing + article pages)
# ---------------------------------------------------------------------
def scrape_items(page_url: str, limit: int = MAX_ITEMS_PER_SOURCE):
    """Scrape listing page for links; fetch article pages for rich metadata."""
    items = []
    try:
        resp = get(page_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Heuristic selectors (safe fallbacks)
        selectors = [
            "article a[href]",
            "h2 a[href]",
            "h3 a[href]",
            "a.card[href]",
            "a.resource-card[href]",
            "a.post-card[href]",
            "a.blog-card[href]",
            "a[href*='/blog/']",
            "a[href*='/resources/']",
            "a[href*='/insights/']",
            "a[href*='/labs']",
            "a[href*='/reports']",
        ]
        links = []
        for sel in selectors:
            links += soup.select(sel)

        # Normalize and filter
        seen = set()
        article_urls: list[tuple[str, str]] = []
        for a in links:
            href = a.get("href")
            title = (a.get_text(strip=True) or "").strip()
            if not href or not title:
                continue
            url = urljoin(page_url, href)
            if url in seen:
                continue
            seen.add(url)
            # Filter out navigation/junk
            if any(x in url.lower() for x in ["#", "/tag/", "/category/", "/login", "/signup", "mailto:"]):
                continue
            article_urls.append((title, url))
            if len(article_urls) >= max(limit * 2, 30):  # collect a bit more; we'll cap fetches below
                break
    except Exception:
        pass

    # Visit up to MAX_ARTICLE_FETCHES article pages to get better metadata
    final: list[dict] = []
    fetch_count = 0
    for title, url in article_urls:
        if fetch_count >= MAX_ARTICLE_FETCHES or len(final) >= limit:
            break
        if RESPECT_ROBOTS and not robots_allows(url):
            continue
        meta = _extract_article_meta(url)
        if meta:
            final.append(meta)
            fetch_count += 1
        else:
            final.append({"title": title, "link": url, "pubDate": rfc2822(), "description": title})
            fetch_count += 1

    # If nothing worked, emit a placeholder so the feed stays valid
    if not final:
        now = rfc2822()
        final = [{
            "title": f"{page_url} — no items discovered",
            "link": page_url,
            "pubDate": now,
            "description": "No posts detected."
        }]
    return final

# ---------------------------------------------------------------------
# Sitemap fallback
# ---------------------------------------------------------------------
def scrape_from_sitemap(page_url: str, limit: int = MAX_ITEMS_PER_SOURCE):
    """Try to find recent article URLs via sitemap, then extract article metadata."""
    parsed = urlparse(page_url)
    bases = [
        f"{parsed.scheme}://{parsed.netloc}/sitemap.xml",
        f"{parsed.scheme}://{parsed.netloc}/sitemap_index.xml",
    ]
    urls: list[str] = []
    for sm in bases:
        try:
            r = get(sm)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "xml")
            locs = [loc.get_text() for loc in soup.find_all("loc")]
            urls = [u for u in locs if u.startswith(f"{parsed.scheme}://{parsed.netloc}/")]
            if urls:
                break
        except Exception:
            continue

    # Pull a handful of newest-looking URLs
    urls = urls[: max(limit * 2, 30)]
    items = []
    for u in urls:
        if len(items) >= limit:
            break
        if RESPECT_ROBOTS and not robots_allows(u):
            continue
        meta = _extract_article_meta(u)
        if meta:
            items.append(meta)

    if not items:
        items = [{
            "title": f"{page_url} — sitemap had no items",
            "link": page_url,
            "pubDate": rfc2822(),
            "description": "No items from sitemap."
        }]
    return items

# ---------------------------------------------------------------------
# RSS + index writers
# ---------------------------------------------------------------------
def write_rss(out_path: str, channel_title: str, channel_link: str, items: list[dict]):
    from xml.sax.saxutils import escape
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    last_build = rfc2822()

    def item_xml(i: dict) -> str:
        t = escape(i.get("title", ""))
        l = escape(i.get("link", ""))
        d = escape(i.get("description", "") or i.get("summary", "") or t)
        pd = i.get("pubDate") or last_build
        return f"""  <item>
    <title>{t}</title>
    <link>{l}</link>
    <description>{d}</description>
    <pubDate>{pd}</pubDate>
  </item>"""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>{escape(channel_title)}</title>
  <link>{escape(channel_link)}</link>
  <description>Auto-generated periodically</description>
  <lastBuildDate>{last_build}</lastBuildDate>
{os.linesep.join(item_xml(i) for i in items)}
</channel>
</rss>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)
    print("Wrote", out_path)

def build_index(feed_map: list[tuple[str, str]]):
    """Create a simple HTML index page listing all feeds."""
    base = os.environ.get("PAGES_BASE", "")  # set in the GitHub Action step
    rows = []
    for name, slug in feed_map:
        rel = f"feeds/{slug}.xml"
        url = (base + rel) if base else rel
        rows.append(f'<li><a href="{url}" target="_blank" rel="noopener">{name}</a> — <code>{url}</code></li>')

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Research Feeds (auto-updated)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,Segoe UI,Arial,sans-serif;line-height:1.5;padding:2rem;max-width:900px;margin:auto}}</style>
</head><body>
<h1>Research Feeds</h1>
<p>Updated automatically by GitHub Actions.</p>
<ul>
{os.linesep.join(rows)}
</ul>
</body></html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote index.html")

# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main():
    with open("sources.json", "r", encoding="utf-8") as f:
        sources = json.load(f)

    feed_map: list[tuple[str, str]] = []

    for src in sources:
        name = src["name"]
        page_url = src["url"]
        slug = src["slug"]
        mode = src.get("mode", "auto").lower()  # auto|scrape|sitemap
        out_path = os.path.join("feeds", f"{slug}.xml")
        print(f"\n=== Processing: {name} ({page_url}) -> {out_path}")

        feed = None
        if mode == "auto":
            candidates = discover_feed_urls(page_url)
            for cand in candidates:
                try:
                    d = validate_feed(cand)
                    if d and d.entries:
                        feed = d
                        print("  + Found feed:", cand)
                        break
                except Exception:
                    continue

        items: list[dict] = []
        if mode == "scrape":
            if RESPECT_ROBOTS and not robots_allows(page_url):
                print("  ! robots.txt disallows scraping listing; trying sitemap fallback …")
                items = scrape_from_sitemap(page_url)
            else:
                print("  • scraping listing page …")
                items = scrape_items(page_url)
        elif mode == "sitemap":
            print("  • using sitemap fallback …")
            items = scrape_from_sitemap(page_url)
        elif feed:
            # Convert feedparser entries to our minimal RSS items
            for e in feed.entries[:MAX_ITEMS_PER_SOURCE]:
                title = e.get("title") or e.get("summary") or "Untitled"
                link = e.get("link") or page_url
                if e.get("published_parsed"):
                    dt = datetime.datetime(*e.published_parsed[:6], tzinfo=datetime.timezone.utc)
                    pub = rfc2822(dt)
                elif e.get("updated_parsed"):
                    dt = datetime.datetime(*e.updated_parsed[:6], tzinfo=datetime.timezone.utc)
                    pub = rfc2822(dt)
                else:
                    pub = rfc2822()
                desc = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True)[:1000]
                items.append({"title": title, "link": link, "pubDate": pub, "description": desc})
        else:
            # Fallback scrape (auto mode, but no valid feed found)
            print("  ! No official feed found; scraping recent links …")
            items = scrape_items(page_url)
            if not items:
                now = rfc2822()
                items = [{
                    "title": f"{name} — no items discovered",
                    "link": page_url,
                    "pubDate": now,
                    "description": "Feed builder could not detect recent posts automatically."
                }]

        write_rss(out_path, f"{name} (Custom Feed)", page_url, items)
        feed_map.append((name, slug))

    build_index(feed_map)

if __name__ == "__main__":
    main()

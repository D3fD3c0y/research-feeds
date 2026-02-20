#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generates one RSS feed per source in sources.json.
Strategy per source:
  1) Try to discover an official feed via <link rel="alternate" ...> (RSS/Atom/JSONFeed).
  2) Try common feed suffixes (/feed, /rss.xml, /atom.xml, /index.xml, ?feed=rss2, ?format=feed).
  3) Handle special platforms (e.g., Medium => append /feed).  # Medium officially supports /feed. (docs) 
  4) If no feed found, scrape recent article links as a fallback and emit a minimal RSS.

Notes:
- Runs in GitHub Actions every 2 hours (UTC).
- Writes feeds to feeds/<slug>.xml and an index.html listing.
"""

import json, os, re, time, datetime, email.utils
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import feedparser

USER_AGENT = "Mozilla/5.0 (compatible; ResearchFeedsBot/1.0; +https://github.com)"
TIMEOUT = 25

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

COMMON_SUFFIXES = [
    "feed", "feed/", "rss", "rss.xml", "atom.xml", "index.xml", "feed.xml",
    "?feed=rss2", "?format=feed"
]

# Helper: RFC2822 date
import datetime
import email.utils

def rfc2822(dt=None) -> str:
    """
    Return an RFC 2822 date string in UTC.
    Ensures the datetime is timezone-aware (UTC), as required by email.utils.format_datetime(usegmt=True).
    """
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    elif dt.tzinfo is None:
        # Treat naive timestamps as UTC
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return email.utils.format_datetime(dt, usegmt=True)



def get(url):
    return requests.get(url, headers=HEADERS, timeout=TIMEOUT)

def discover_feed_urls(page_url: str):
    """Return a list of candidate feed URLs discovered from the page plus common patterns."""
    cands = []
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

    # Add common suffixes
    base = page_url.rstrip("/")
    for sfx in COMMON_SUFFIXES:
        cands.append(urljoin(base + "/", sfx))

    # Special case: Medium (officially supports /feed on profiles/publications)
    # https://help.medium.com/hc/en-us/articles/214874118-Using-RSS-feeds-of-profiles-publications-and-topics
    host = urlparse(page_url).netloc.lower()
    if "medium.com" in host or host.endswith(".medium.com"):
        cands.insert(0, urljoin(base + "/", "feed"))

    # De-duplicate preserving order
    seen = set()
    uniq = []
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

def scrape_items(page_url: str, limit: int = 15):
    """Very forgiving scraper to extract recent article links when no feed exists."""
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
            if any(x in url.lower() for x in ["#","/tag/","/category/","/login","/signup","mailto:"]):
                continue
            items.append({"title": title, "link": url})
            if len(items) >= limit:
                break
    except Exception:
        pass

    # Stamp current time when we don't have pub dates
    now = rfc2822(datetime.datetime.utcnow())
    for it in items:
        it.setdefault("pubDate", now)
        it.setdefault("description", it["title"])
    return items

def write_rss(out_path: str, channel_title: str, channel_link: str, items):
    from xml.sax.saxutils import escape
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    last_build = rfc2822(datetime.datetime.utcnow())

    def item_xml(i):
        t = escape(i.get("title",""))
        l = escape(i.get("link",""))
        d = escape(i.get("description","") or i.get("summary","") or t)
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
  <description>Auto-generated every 2 hours</description>
  <lastBuildDate>{last_build}</lastBuildDate>
{os.linesep.join(item_xml(i) for i in items)}
</channel>
</rss>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)
    print("Wrote", out_path)

def build_index(feed_map):
    """Create a simple HTML index page listing all feeds."""
    rows = []
    for name, slug in feed_map:
        url = f"feeds/{slug}.xml"
        rows.append(f'<li>{url}{name}</a> — <code>{url}</code></li>')
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Research Feeds (auto-updated)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,Segoe UI,Arial,sans-serif;line-height:1.5;padding:2rem;max-width:900px;margin:auto}}</style>
</head><body>
<h1>Research Feeds</h1>
<p>Updated automatically every 2 hours by GitHub Actions.</p>
<ul>
{os.linesep.join(rows)}
</ul>
</body></html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote index.html")

def main():
    with open("sources.json", "r", encoding="utf-8") as f:
        sources = json.load(f)

    feed_map = []

    for src in sources:
        name = src["name"]
        page_url = src["url"]
        slug = src["slug"]
        out_path = os.path.join("feeds", f"{slug}.xml")
        print(f"\n=== Processing: {name} ({page_url}) -> {out_path}")

        # 1) Try discovery + validation
        candidates = discover_feed_urls(page_url)
        feed = None
        for cand in candidates:
            try:
                d = validate_feed(cand)
                if d and d.entries:
                    feed = d
                    print("  + Found feed:", cand)
                    break
            except Exception:
                continue

        items = []
        if feed:
            # Convert feedparser entries to our minimal RSS items
            for e in feed.entries[:20]:
                title = e.get("title") or e.get("summary") or "Untitled"
                link = e.get("link") or page_url
                # Build pubDate if available
                if e.get("published_parsed"):
                    dt = datetime.datetime(*e.published_parsed[:6], tzinfo=datetime.timezone.utc)
                    pub = rfc2822(dt)
                elif e.get("updated_parsed"):
                    dt = datetime.datetime(*e.updated_parsed[:6], tzinfo=datetime.timezone.utc)
                    pub = rfc2822(dt)
                else:
                    pub = rfc2822(datetime.datetime.utcnow())
                desc = BeautifulSoup(e.get("summary",""), "html.parser").get_text(" ", strip=True)[:1000]
                items.append({"title": title, "link": link, "pubDate": pub, "description": desc})
        else:
            # 2) Fallback scrape
            print("  ! No official feed found; scraping recent links …")
            items = scrape_items(page_url)
            if not items:
                # Emergency placeholder
                now = rfc2822(datetime.datetime.utcnow())
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

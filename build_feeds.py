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

# ISO-ish (or URL) date guesser: YYYY/MM/DD or YYYY-MM-DD
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

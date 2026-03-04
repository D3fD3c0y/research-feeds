
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generates one RSS feed per source in sources.json.

Modes per source:
 - "auto"   -> discover an official feed (<link rel="alternate"...>), try common suffixes, else scrape.
 - "scrape" -> scrape listing page and visit article pages for metadata.
 - "sitemap"-> use sitemap(s) to discover URLs, then visit article pages for metadata.

Optional per-source scoping:
 - sitemap_include_prefix: only prioritize (or strictly include) sitemap URLs under a path prefix
 - sitemap_strict: if true, only include URLs under sitemap_include_prefix (or page_url path)
 - scrape_include_prefix: only prioritize (or strictly include) scraped links under a path prefix
 - scrape_strict: if true, only include links under scrape_include_prefix (or page_url path)

Option A (artifact-friendly):
 - After generating feeds/ and index.html, this script can optionally zip them into
   artifacts/feeds_artifact.zip and write artifacts/artifact_manifest.json.
 - You can then upload that file/directory using GitHub Actions upload-artifact. [1](https://rss.feedspot.com/ai_rss_feeds/)[2](https://sophosapps-my.sharepoint.com/personal/mathieu_hinse_sophos_com/Documents/Microsoft%20Copilot%20Chat%20Files/build_feeds.py).py)
"""
import json
import os
import re
import datetime
import email.utils
import gzip
import io
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse
import urllib.robotparser as robotparser
import zipfile
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bs4 import BeautifulSoup
import feedparser

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

TIMEOUT = 20
MAX_ITEMS_PER_SOURCE = 30
MAX_ARTICLE_FETCHES = 20
RESPECT_ROBOTS = True

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

COMMON_SUFFIXES = [
    "feed", "feed/", "rss", "rss.xml", "atom.xml", "index.xml", "feed.xml",
    "?feed=rss2", "?format=feed"
]

DEBUG_HTTP = True

# ---------------------------------------------------------------------
# Per-domain timeout + retries
# ---------------------------------------------------------------------
_retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "HEAD"],
)
_session = requests.Session()
_adapter = HTTPAdapter(max_retries=_retry)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

DOMAIN_TIMEOUTS = {
    "www.trellix.com": 60,
    "trellix.com": 60,
}

# ---------------------------------------------------------------------
# Option A: package outputs for easy artifact upload
# ---------------------------------------------------------------------
# Default ON when running in GitHub Actions, OFF otherwise
DEFAULT_ZIP_ARTIFACT = (os.environ.get("GITHUB_ACTIONS", "").lower() == "true")
ZIP_ARTIFACT = os.environ.get("ZIP_ARTIFACT", "true" if DEFAULT_ZIP_ARTIFACT else "false").lower() == "true"

ARTIFACTS_DIR = os.environ.get("ARTIFACTS_DIR", "artifacts")
ARTIFACT_ZIP_NAME = os.environ.get("ARTIFACT_ZIP_NAME", "feeds_artifact.zip")
ARTIFACT_MANIFEST_NAME = os.environ.get("ARTIFACT_MANIFEST_NAME", "artifact_manifest.json")

# ---------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------
def rfc2822(dt: datetime.datetime | None = None) -> str:
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return email.utils.format_datetime(dt, usegmt=True)

# ---------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------
def get(url: str, label: str = ""):
    try:
        host = urlparse(url).netloc.lower()
        timeout = DOMAIN_TIMEOUTS.get(host, TIMEOUT)

        r = _session.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")

        if DEBUG_HTTP:
            print(f" [HTTP] {label} GET {url} -> {r.status_code} (final: {r.url}) CT={ct}")
        elif r.status_code != 200:
            print(f" [HTTP] {label} GET {url} -> {r.status_code} (final: {r.url}) CT={ct}")

        if DEBUG_HTTP and r.status_code != 200:
            snippet = (r.text or "")[:200].replace("\n", " ").replace("\r", " ")
            print(f" [HTTP] {label} body snippet: {snippet}")

        return r
    except Exception as e:
        print(f" [HTTP] {label} GET {url} -> EXCEPTION: {e}")
        raise

# ---------------------------------------------------------------------
# robots.txt handling (fetch using our headers)
# ---------------------------------------------------------------------
def robots_allows(page_url: str, agent: str = "ResearchFeedsBot") -> bool:
    if not RESPECT_ROBOTS:
        return True

    try:
        parsed = urlparse(page_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        r = get(robots_url, "robots-check")

        text = (r.text or "")
        looks_like_robots = ("user-agent:" in text.lower()) or ("disallow:" in text.lower()) or ("allow:" in text.lower())

        if r.status_code == 404 and not looks_like_robots:
            return True

        if looks_like_robots:
            rp = robotparser.RobotFileParser()
            rp.set_url(robots_url)
            rp.parse(text.splitlines())
            return rp.can_fetch(agent, page_url)

        if r.status_code in (401, 403):
            return False

        return True
    except Exception:
        return True

# ---------------------------------------------------------------------
# Feed discovery (auto mode)
# ---------------------------------------------------------------------
def discover_feed_urls(page_url: str) -> list[str]:
    cands: list[str] = []

    try:
        resp = get(page_url, "discover")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for link in soup.select('link[rel="alternate"]'):
            t = (link.get("type") or "").lower()
            if any(x in t for x in ["rss", "atom", "xml", "json"]):
                href = link.get("href")
                if href:
                    cands.append(urljoin(page_url, href))
    except Exception:
        pass

    base = page_url.rstrip("/")
    for sfx in COMMON_SUFFIXES:
        cands.append(urljoin(base + "/", sfx))

    parsed = urlparse(page_url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc
    apex = host[4:] if host.startswith("www.") else host
    root_base = f"{scheme}://{host}"
    apex_base = f"{scheme}://{apex}"
    for u in (f"{root_base}/feed", f"{root_base}/blog/feed", f"{apex_base}/feed", f"{apex_base}/blog/feed"):
        cands.append(u)

    host_l = parsed.netloc.lower()
    if "medium.com" in host_l or host_l.endswith(".medium.com"):
        cands.insert(0, urljoin(base + "/", "feed"))

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
def _parse_jsonld_articles(soup: BeautifulSoup, base_url: str) -> list[dict]:
    items: list[dict] = []
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
                    link = obj.get("url") or base_url
                    date = obj.get("datePublished") or obj.get("dateModified") or ""
                    desc = obj.get("description") or ""
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

_DATE_URL_PAT = re.compile(r"(?P<y>20\d{2})?P<m>\d{1,2}?P<d>\d{1,2}")

def _guess_rfc2822_from_text(s: str | None) -> str | None:
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
    try:
        r = get(article_url, "article")
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "html.parser")

        items = _parse_jsonld_articles(soup, article_url)
        if items:
            item = items[0]
            if item.get("pubDate"):
                try:
                    dt = datetime.datetime.fromisoformat(item["pubDate"].replace("Z", "+00:00"))
                    item["pubDate"] = rfc2822(dt)
                except Exception:
                    item["pubDate"] = _guess_rfc2822_from_text(item["pubDate"]) or rfc2822()
            else:
                item["pubDate"] = rfc2822()

            if not item.get("description"):
                md = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
                if md and md.get("content"):
                    item["description"] = md["content"]
            return item

        title = (soup.title.string.strip() if soup.title and soup.title.string else "")
        meta_title = soup.find("meta", attrs={"property": "og:title"})
        if meta_title and meta_title.get("content"):
            title = meta_title["content"].strip()

        iso_dt = ""
        dt_tag = soup.find("time", attrs={"datetime": True})
        if dt_tag:
            iso_dt = (dt_tag.get("datetime") or "").strip()
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
# Scraping (listing + article pages) WITH include_prefix + strict option
# ---------------------------------------------------------------------
def scrape_items(
    page_url: str,
    limit: int = MAX_ITEMS_PER_SOURCE,
    include_prefix: str | None = None,
    strict_section_only: bool = False,
) -> list[dict]:
    base_netloc = urlparse(page_url).netloc.lower()
    candidates: list[tuple[bool, str, str]] = []
    links = []

    try:
        resp = get(page_url, "scrape-listing")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        selectors = [
            "article a[href]",
            "h2 a[href]",
            "h3 a[href]",
            "a.card[href]",
            "a.resource-card[href]",
            "a.post-card[href]",
            "a.blog-card[href]",
            "a[href*='/blog/']",
            "a[href*='/blogs/']",
            "a[href*='/resources/']",
            "a[href*='/insights/']",
            "a[href*='/labs']",
            "a[href*='/reports']",
            "a[href*='threat-reports']",
            "a[href*='/advanced-research-center/']",
        ]
        for sel in selectors:
            links += soup.select(sel)

        preferred_prefix = include_prefix
        if not preferred_prefix:
            base_hint = urlparse(page_url).path.rstrip("/")
            preferred_prefix = (base_hint + "/") if base_hint and base_hint != "/" else ""

        seen = set()
        for a in links:
            href = a.get("href")
            if not href:
                continue

            title = (a.get_text(strip=True) or "").strip()
            if not title:
                title = (a.get("aria-label") or a.get("title") or "").strip()
            if not title:
                img = a.find("img", alt=True)
                if img and img.get("alt"):
                    title = img["alt"].strip()
            if not title:
                title = "Post"

            url = urljoin(page_url, href)

            if url in seen:
                continue
            seen.add(url)

            # drop off-domain links (prevents partners.* etc.)
            if urlparse(url).netloc.lower() != base_netloc:
                continue

            if any(x in url.lower() for x in ["#", "/tag/", "/category/", "/login", "/signup", "mailto:"]):
                continue

            path = urlparse(url).path
            is_preferred = bool(preferred_prefix and path.startswith(preferred_prefix))

            if strict_section_only and preferred_prefix and not is_preferred:
                continue

            candidates.append((is_preferred, title, url))

    except Exception as e:
        print(f" ! scrape_items listing fetch error: {e}")

    candidates.sort(key=lambda t: (not t[0],))  # preferred first

    final: list[dict] = []
    fetch_count = 0

    for is_pref, title, url in candidates:
        if fetch_count >= MAX_ARTICLE_FETCHES or len(final) >= limit:
            break
        if RESPECT_ROBOTS and not robots_allows(url):
            continue

        meta = _extract_article_meta(url)
        if meta:
            final.append(meta)
        else:
            final.append({"title": title, "link": url, "pubDate": rfc2822(), "description": title})

        fetch_count += 1

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
# Sitemap helpers
# ---------------------------------------------------------------------
def _normalize_host(netloc: str) -> str:
    return netloc.lower().lstrip("[").rstrip("]").removeprefix("www.")

def _same_site(url: str, base: str) -> bool:
    up = urlparse(url)
    bp = urlparse(base)
    return _normalize_host(up.netloc) == _normalize_host(bp.netloc)

def _read_robots_for_sitemaps(page_url: str) -> list[str]:
    try:
        p = urlparse(page_url)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        r = get(robots_url, "robots")
        if r.status_code != 200 or not r.text:
            return []
        urls = []
        for line in r.text.splitlines():
            line = line.strip()
            if not line or not line.lower().startswith("sitemap:"):
                continue
            sm = line.split(":", 1)[1].strip()
            if sm:
                urls.append(sm)
        return urls
    except Exception:
        return []

def _fetch_xml(url: str) -> str | None:
    try:
        r = get(url, "sitemap")
        if r.status_code != 200:
            return None

        ct = (r.headers.get("Content-Type", "") or "").lower()
        raw = r.content or b""

        if url.endswith(".gz") or ct.endswith("gzip"):
            try:
                raw = gzip.decompress(raw)
            except OSError:
                raw = gzip.GzipFile(fileobj=io.BytesIO(r.content)).read()

        head = raw[:400].decode("utf-8", errors="replace").lstrip().lower()
        if ("text/html" in ct or head.startswith("<!doctype html") or head.startswith("<html")):
            if not (head.startswith("<?xml") or "<urlset" in head or "<sitemapindex" in head):
                return None

        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None

# ---------------------------------------------------------------------
# Sitemap scraping (WITH include_prefix + strict option)
# ---------------------------------------------------------------------
def scrape_from_sitemap(
    page_url: str,
    limit: int = MAX_ITEMS_PER_SOURCE,
    sitemap_url: str | None = None,
    include_prefix: str | None = None,
    strict_section_only: bool = False,
) -> list[dict]:
    parsed = urlparse(page_url)
    base_root = f"{parsed.scheme}://{parsed.netloc}"

    candidates: list[str] = []
    if sitemap_url:
        candidates.append(sitemap_url)

    if "sitemap" in page_url.lower() and page_url.lower().endswith((".xml", ".xml.gz")):
        candidates.append(page_url)

    candidates += [
        f"{base_root}/sitemap.xml",
        f"{base_root}/sitemap_index.xml",
        f"{base_root}/sitemap.xml.gz",
        f"{base_root}/sitemap_index.xml.gz",
    ]

    robots_hints = _read_robots_for_sitemaps(page_url)
    candidates = robots_hints + [c for c in candidates if c not in robots_hints]

    seen_cands = set()
    dedup_cands = []
    for c in candidates:
        if c and c not in seen_cands:
            seen_cands.add(c)
            dedup_cands.append(c)
    candidates = dedup_cands

    seen_sitemaps = set()
    page_urls: list[str] = []

    def _iter_local(root: ET.Element, local_name: str):
        for el in root.iter():
            if isinstance(el.tag, str) and el.tag.rsplit("}", 1)[-1] == local_name:
                yield el

    def _first_child_local(parent: ET.Element, local_name: str):
        for ch in list(parent):
            if isinstance(ch.tag, str) and ch.tag.rsplit("}", 1)[-1] == local_name:
                return ch
        return None

    def parse_sitemap(xml_text: str):
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return

        for url_el in _iter_local(root, "url"):
            loc_el = _first_child_local(url_el, "loc")
            if loc_el is None:
                continue
            loc_text = (loc_el.text or "").strip()
            if not loc_text:
                continue
            if _same_site(loc_text, page_url):
                page_urls.append(loc_text)

        for sm_el in _iter_local(root, "sitemap"):
            loc_el = _first_child_local(sm_el, "loc")
            if loc_el is None:
                continue
            child = (loc_el.text or "").strip()
            if not child or child in seen_sitemaps:
                continue
            seen_sitemaps.add(child)
            xml_child = _fetch_xml(child)
            if xml_child:
                parse_sitemap(xml_child)

    for sm_url in candidates:
        if len(page_urls) >= max(limit * 2, 50):
            break
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)

        xml_txt = _fetch_xml(sm_url)
        if not xml_txt:
            continue
        parse_sitemap(xml_txt)

        if len(page_urls) >= max(limit * 2, 50):
            break

    dedup = []
    seen = set()
    for u in page_urls:
        if u not in seen:
            seen.add(u)
            dedup.append(u)

    preferred_prefix = include_prefix
    if not preferred_prefix:
        base_hint = urlparse(page_url).path.rstrip("/")
        preferred_prefix = (base_hint + "/") if base_hint and base_hint != "/" else ""

    preferred = []
    others = []
    for u in dedup:
        p = urlparse(u).path
        if preferred_prefix and p.startswith(preferred_prefix):
            preferred.append(u)
        else:
            others.append(u)

    urls = preferred[: max(limit * 2, 50)] if strict_section_only else (preferred + others)[: max(limit * 2, 50)]

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
            "description": "No items were discoverable via sitemap(s) or they pointed to non-article URLs."
        }]

    return items

# ---------------------------------------------------------------------
# RSS + index writers
# ---------------------------------------------------------------------
def write_rss(out_path: str, channel_title: str, channel_link: str, items: list[dict]) -> str:
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
    return last_build

def _read_existing_last_build(slug: str) -> str:
    path = os.path.join("feeds", f"{slug}.xml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"<lastBuildDate>(.*?)</lastBuildDate>", text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return "n/a"

def build_index(feed_map_enabled: list[tuple[str, str, str]],
                feed_map_disabled: list[tuple[str, str, str]]):
    base = os.environ.get("PAGES_BASE", "")

    def row_enabled(name: str, slug: str, last_build: str) -> str:
        rel = f"feeds/{slug}.xml"
        url = (base + rel) if base else rel
        return f'<li><a href="{url}" target="_blank" rel="noopener">{name}</a> — <code>{url}</code> — <small>Last run: {last_build}</small></li>'

    def row_disabled(name: str, slug: str, last_build: str) -> str:
        rel = f"feeds/{slug}.xml"
        url = (base + rel) if base else rel
        return f'<li><span>{name}</span> — <code>{url}</code> — <em>disabled</em> — <small>Last run: {last_build}</small></li>'

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Research Feeds (auto-updated)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{{font-family:system-ui,Segoe UI,Arial,sans-serif;line-height:1.5;padding:2rem;max-width:900px;margin:auto}}
  h2{{margin-top:2rem}}
  code, small{{opacity:.8}}
  li{{margin:.25rem 0}}
</style>
</head><body>
<h1>Research Feeds</h1>
<p>Updated automatically by GitHub Actions.</p>

<h2>Enabled</h2>
<ul>
{os.linesep.join(row_enabled(n, s, lb) for (n, s, lb) in feed_map_enabled)}
</ul>

<h2>Disabled</h2>
<ul>
{os.linesep.join(row_disabled(n, s, lb) for (n, s, lb) in feed_map_disabled)}
</ul>

</body></html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote index.html")

# ---------------------------------------------------------------------
# Option A support: zip outputs and write manifest
# ---------------------------------------------------------------------
def create_artifact_package(manifest: dict):
    """
    Create artifacts/feeds_artifact.zip containing:
      - feeds/ directory (all xml files)
      - index.html (if present)
      - sources.json (if present)
      - run manifest json

    This makes it easys an artifact. [1](https://rss.feedspot.com/ai_rss_feeds/)[2](https://sophosapps-my.sharepoint.com/personal/mathieu_hinse_sophos_com/Documents/Microsoft%20Copilot%20Chat%20Files/build_feeds.py).py)
    """
    out_dir = Path(ARTIFACTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = out_dir / ARTIFACT_MANIFEST_NAME
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    zip_path = out_dir / ARTIFACT_ZIP_NAME
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # include feeds/
        feeds_dir = Path("feeds")
        if feeds_dir.exists():
            for p in feeds_dir.rglob("*"):
                if p.is_file():
                    z.write(p, arcname=str(p))

        # include index.html if present
        idx = Path("index.html")
        if idx.exists():
            z.write(idx, arcname="index.html")

        # include sources.json if present (helpful for auditing)
        src = Path("sources.json")
        if src.exists():
            z.write(src, arcname="sources.json")

        # include manifest
        z.write(manifest_path, arcname=str(manifest_path))

    print(f"Wrote artifact ZIP: {zip_path}")

# ---------------------------------------------------------------------
# Per-source robots override
# ---------------------------------------------------------------------
def _respect_robots_for_source(src: dict) -> bool:
    if src.get("ignore_robots") is True:
        return False
    if "respect_robots" in src:
        return bool(src.get("respect_robots"))
    return RESPECT_ROBOTS

# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main():
    with open("sources.json", "r", encoding="utf-8") as f:
        sources = json.load(f)

    enabled_sources = [s for s in sources if not s.get("disabled")]
    disabled_sources = [s for s in sources if s.get("disabled")]

    feed_map_enabled: list[tuple[str, str, str]] = []
    feed_map_disabled: list[tuple[str, str, str]] = []

    # For Option A manifest
    run_manifest = {
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "feeds": []
    }

    for ds in disabled_sources:
        feed_map_disabled.append((ds["name"], ds["slug"], _read_existing_last_build(ds["slug"])))

    for src in enabled_sources:
        name = src["name"]
        page_url = src["url"]
        slug = src["slug"]
        out_path = os.path.join("feeds", f"{slug}.xml")

        global RESPECT_ROBOTS
        _prev_respect = RESPECT_ROBOTS
        RESPECT_ROBOTS = _respect_robots_for_source(src)

        items: list[dict] = []
        last_build = "n/a"
        status = "ok"
        error = None

        try:
            mode = src.get("mode", "auto").lower()
            print(f"\n=== Processing: {name} ({page_url}) -> {out_path}")
            print(f" robots.txt respected: {RESPECT_ROBOTS}")

            feed = None
            if mode == "auto":
                for cand in discover_feed_urls(page_url):
                    try:
                        d = validate_feed(cand)
                        if d and d.entries:
                            feed = d
                            print(" + Found feed:", cand)
                            break
                    except Exception:
                        continue

            if mode == "scrape":
                if RESPECT_ROBOTS and not robots_allows(page_url):
                    print(" ! robots.txt disallows scraping listing; trying sitemap fallback …")
                    items = scrape_from_sitemap(
                        page_url,
                        sitemap_url=src.get("sitemap_url"),
                        include_prefix=src.get("sitemap_include_prefix"),
                        strict_section_only=bool(src.get("sitemap_strict", False)),
                    )
                else:
                    print(" • scraping listing page …")
                    items = scrape_items(
                        page_url,
                        include_prefix=src.get("scrape_include_prefix"),
                        strict_section_only=bool(src.get("scrape_strict", False)),
                    )

            elif mode == "sitemap":
                print(" • using sitemap fallback …")
                items = scrape_from_sitemap(
                    page_url,
                    sitemap_url=src.get("sitemap_url"),
                    include_prefix=src.get("sitemap_include_prefix"),
                    strict_section_only=bool(src.get("sitemap_strict", False)),
                )

            elif feed:
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
                print(" ! No official feed found; scraping recent links …")
                items = scrape_items(
                    page_url,
                    include_prefix=src.get("scrape_include_prefix"),
                    strict_section_only=bool(src.get("scrape_strict", False)),
                )

            if not items:
                items = [{
                    "title": f"{name} — no items discovered",
                    "link": page_url,
                    "pubDate": rfc2822(),
                    "description": "Feed builder could not detect recent posts automatically."
                }]

            last_build = write_rss(out_path, f"{name} (Custom Feed)", page_url, items)
            feed_map_enabled.append((name, slug, last_build))

        except Exception as e:
            status = "error"
            error = str(e)
            print(f" ! Unhandled error for {name}: {e}")
            last_build = rfc2822()
            write_rss(
                out_path,
                f"{name} (Custom Feed)",
                page_url,
                [{
                    "title": f"Error building feed for {name}",
                    "link": page_url,
                    "pubDate": last_build,
                    "description": str(e)
                }]
            )
            feed_map_enabled.append((name, slug, last_build))

        finally:
            RESPECT_ROBOTS = _prev_respect

            # Manifest entry
            run_manifest["feeds"].append({
                "name": name,
                "slug": slug,
                "url": page_url,
                "mode": src.get("mode", "auto"),
                "status": status,
                "error": error,
                "item_count": len(items) if items else 0,
                "output_file": out_path,
                "lastBuildDate": last_build,
            })

    build_index(feed_map_enabled, feed_map_disabled)

    # Option A: zip the outputs so GitHub Actions can upload them easily as an artifact. [1](https://rss.feedspot.com/ai_rss_feeds/)[2](https://sophosapps-my.sharepoint.com/personal/mathieu_hinse_sophos_com/Documents/Microsoft%20Copilot%20Chat%20Files/build_feeds.py).py)
    if ZIP_ARTIFACT:
        create_artifact_package(run_manifest)
    else:
        print("Artifact ZIP disabled (set ZIP_ARTIFACT=true to enable).")

if __name__ == "__main__":
    main()

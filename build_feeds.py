
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generates one RSS feed per source in sources.json.
Modes per source:
 - "auto"   -> discover an official feed (<link rel="alternate"...>), try common suffixes, else scrape.
 - "scrape" -> scrape listing page and visit article pages for metadata.
 - "sitemap"-> use sitemap(s) to discover URLs, then visit article pages for metadata.

Notes:
- Designed for GitHub Actions on a schedule (UTC).
- Writes feeds to feeds/<slug>.xml and an index.html listing.
"""
import json, os, re, datetime, email.utils
from urllib.parse import urljoin, urlparse
import urllib.robotparser as robotparser
import gzip
import io
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
import feedparser

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
# UA change: use a mainstream browser UA to reduce bot/WAF blocks.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

TIMEOUT = 20  # per-request timeout (seconds)
MAX_ITEMS_PER_SOURCE = 30  # number of items to emit per feed
MAX_ARTICLE_FETCHES = 20  # cap article-page fetches per source
RESPECT_ROBOTS = True  # honor robots.txt by default

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

# ---------------------------------------------------------------------
# Debug logging
# ---------------------------------------------------------------------
DEBUG_HTTP = True  # set False once stable (reduces noise)

# ---------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------
def rfc2822(dt: datetime.datetime | None = None) -> str:
    """
    Return an RFC 2822 date string in UTC (tz-aware).
    """
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return email.utils.format_datetime(dt, usegmt=True)

# ---------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------
def get(url: str, label: str = ""):
    """
    HTTP GET with optional debug logging.
    Prints status code, final URL after redirects, and content-type.
    For non-200 responses, prints a small response-body snippet when DEBUG_HTTP is True.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
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
# robots.txt handling (patched)
# ---------------------------------------------------------------------
def robots_allows(page_url: str, agent: str = "ResearchFeedsBot") -> bool:
    """
    Patched robots handling:
    - Fetch robots.txt using our headers (not urllib default used by RobotFileParser.read()).
    - Parse via rp.parse(lines).
    """
    if not RESPECT_ROBOTS:
        return True

    try:
        parsed = urlparse(page_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        r = get(robots_url, "robots-check")

        # If robots.txt is missing, assume allowed (common crawler behavior)
        if r.status_code == 404:
            return True

        # If robots.txt exists, parse it
        if r.status_code == 200 and r.text:
            rp = robotparser.RobotFileParser()
            rp.set_url(robots_url)
            rp.parse(r.text.splitlines())
            return rp.can_fetch(agent, page_url)

        # If blocked from reading robots.txt, be conservative
        if r.status_code in (401, 403):
            return False

        # Other statuses: default allow (keeps pipeline running)
        return True

    except Exception:
        # If robots can't be read, allow; downstream 403/429 will still be handled gracefully
        return True

# ---------------------------------------------------------------------
# Feed discovery (auto mode)
# ---------------------------------------------------------------------
def discover_feed_urls(page_url: str):
    """Return a list of candidate feed URLs discovered from the page plus common patterns."""
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
        pass  # continue with suffix checks

    base = page_url.rstrip("/")
    for sfx in COMMON_SUFFIXES:
        cands.append(urljoin(base + "/", sfx))

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
                    guess = _guess_rfc2822_from_text(item["pubDate"])
                    item["pubDate"] = guess or rfc2822()
            else:
                item["pubDate"] = rfc2822()

            if not item.get("description"):
                md = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
                if md and md.get("content"):
                    item["description"] = md["content"]
            return item

        # Fallbacks
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
    article_urls: list[tuple[str, str]] = []
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
            "a[href*='/blogs/']",                         # PATCH: Trellix uses /blogs/
            "a[href*='/blogs/research/']",                # PATCH: Trellix research
            "a[href*='/resources/']",
            "a[href*='/insights/']",
            "a[href*='/labs']",
            "a[href*='/reports']",
            "a[href*='threat-reports']",                  # PATCH: Trellix threat reports
            "a[href*='/advanced-research-center/']",      # PATCH: Trellix ARC
        ]
        for sel in selectors:
            links += soup.select(sel)

        seen = set()
        for a in links:
            href = a.get("href")

            # Title fallback patch (aria-label, title attr, image alt)
            title = (a.get_text(strip=True) or "").strip()
            if not title:
                title = (a.get("aria-label") or a.get("title") or "").strip()
            if not title:
                img = a.find("img", alt=True)
                if img and img.get("alt"):
                    title = img["alt"].strip()
            if not title:
                title = "Post"

            if not href:
                continue

            url = urljoin(page_url, href)
            if url in seen:
                continue
            seen.add(url)

            if any(x in url.lower() for x in ["#", "/tag/", "/category/", "/login", "/signup", "mailto:"]):
                continue

            article_urls.append((title, url))
            if len(article_urls) >= max(limit * 2, 30):
                break

    except Exception as e:
        print(f" ! scrape_items listing fetch error: {e}")

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
    """Parse robots.txt and return any 'Sitemap:' URLs found."""
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
    """
    Fetch XML (supports .gz) and return decoded XML text, or None.

    Patch: If response is clearly HTML (common for WAF/landing pages),
    avoid trying to parse it as XML, but still allow XML served with odd content-type
    by sniffing for XML markers.
    """
    try:
        r = get(url, "sitemap")
        if r.status_code != 200:
            return None

        ct = (r.headers.get("Content-Type", "") or "").lower()
        raw = r.content or b""

        # Decompress if gzip
        if url.endswith(".gz") or ct.endswith("gzip"):
            try:
                raw = gzip.decompress(raw)
            except OSError:
                raw = gzip.GzipFile(fileobj=io.BytesIO(r.content)).read()

        # Sniff content
        head = raw[:400].decode("utf-8", errors="replace").lstrip().lower()

        # If HTML and doesn't look like XML, ignore it
        if ("text/html" in ct or head.startswith("<!doctype html") or head.startswith("<html")):
            if not (head.startswith("<?xml") or "<urlset" in head or "<sitemapindex" in head):
                return None

        return raw.decode("utf-8", errors="replace")

    except Exception:
        return None

def scrape_from_sitemap(page_url: str, limit: int = MAX_ITEMS_PER_SOURCE, sitemap_url: str | None = None):
    """
    Find recent article URLs via sitemap(s) and extract article metadata.
    Option B: If sitemap_url is provided (from sources.json), it is tried first.
    """
    parsed = urlparse(page_url)
    base_root = f"{parsed.scheme}://{parsed.netloc}"

    candidates: list[str] = []

    # Option B: explicit sitemap_url from sources.json
    if sitemap_url:
        candidates.append(sitemap_url)

    # If page_url itself looks like a sitemap XML, try it too
    if "sitemap" in page_url.lower() and page_url.lower().endswith((".xml", ".xml.gz")):
        candidates.append(page_url)

    # Defaults
    candidates += [
        f"{base_root}/sitemap.xml",
        f"{base_root}/sitemap_index.xml",
        f"{base_root}/sitemap.xml.gz",
        f"{base_root}/sitemap_index.xml.gz",
    ]

    robots_hints = _read_robots_for_sitemaps(page_url)
    candidates = robots_hints + [c for c in candidates if c not in robots_hints]

    # De-duplicate candidates
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

        # urlset -> collect <url><loc>
        for url_el in _iter_local(root, "url"):
            loc_el = _first_child_local(url_el, "loc")
            if loc_el is None:
                continue
            loc_text = (loc_el.text or "").strip()
            if not loc_text:
                continue
            if _same_site(loc_text, page_url):
                page_urls.append(loc_text)

        # sitemapindex -> follow nested sitemaps
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

    # Walk candidates
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

    # De-duplicate and cap
    dedup = []
    seen = set()
    for u in page_urls:
        if u not in seen:
            seen.add(u)
            dedup.append(u)
    urls = dedup[: max(limit * 2, 50)]

    # Prefer URLs matching the provided page_url path
    hint_path = urlparse(page_url).path.rstrip("/")
    if hint_path and hint_path != "/":
        preferred = [u for u in urls if urlparse(u).path.startswith(hint_path)]
        others = [u for u in urls if not urlparse(u).path.startswith(hint_path)]
        urls = preferred + others

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
    """Write an RSS file and return the lastBuildDate string used."""
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
    """Try to read <lastBuildDate> from an existing feeds/<slug>.xml. Return 'n/a' if none."""
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
    """
    Create a simple HTML index page listing all feeds.
    - feed_map_enabled: [(name, slug, last_build)]
    - feed_map_disabled: [(name, slug, last_build)]
    """
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
# Per-source robots override
# ---------------------------------------------------------------------
def _respect_robots_for_source(src: dict) -> bool:
    """
    Decide whether to respect robots.txt for this source.
    Priority:
    1) if "ignore_robots": true -> False
    2) if "respect_robots" explicitly set -> that value
    3) otherwise -> global RESPECT_ROBOTS
    """
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

    for ds in disabled_sources:
        name = ds["name"]
        slug = ds["slug"]
        last_build_prev = _read_existing_last_build(slug)
        feed_map_disabled.append((name, slug, last_build_prev))

    for src in enabled_sources:
        name = src["name"]
        page_url = src["url"]
        slug = src["slug"]
        out_path = os.path.join("feeds", f"{slug}.xml")

        global RESPECT_ROBOTS
        _prev_respect = RESPECT_ROBOTS
        RESPECT_ROBOTS = _respect_robots_for_source(src)

        try:
            mode = src.get("mode", "auto").lower()
            print(f"\n=== Processing: {name} ({page_url}) -> {out_path}")
            print(f" robots.txt respected: {RESPECT_ROBOTS}")

            feed = None
            if mode == "auto":
                candidates = discover_feed_urls(page_url)
                for cand in candidates:
                    try:
                        d = validate_feed(cand)
                        if d and d.entries:
                            feed = d
                            print(" + Found feed:", cand)
                            break
                    except Exception:
                        continue

            items: list[dict] = []

            if mode == "scrape":
                if RESPECT_ROBOTS and not robots_allows(page_url):
                    print(" ! robots.txt disallows scraping listing; trying sitemap fallback …")
                    items = scrape_from_sitemap(page_url, sitemap_url=src.get("sitemap_url"))
                else:
                    print(" • scraping listing page …")
                    items = scrape_items(page_url)

            elif mode == "sitemap":
                print(" • using sitemap fallback …")
                items = scrape_from_sitemap(page_url, sitemap_url=src.get("sitemap_url"))

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
                items = scrape_items(page_url)

            if not items:
                now = rfc2822()
                items = [{
                    "title": f"{name} — no items discovered",
                    "link": page_url,
                    "pubDate": now,
                    "description": "Feed builder could not detect recent posts automatically."
                }]

            last_build = write_rss(out_path, f"{name} (Custom Feed)", page_url, items)
            feed_map_enabled.append((name, slug, last_build))

        except Exception as e:
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

    build_index(feed_map_enabled, feed_map_disabled)

if __name__ == "__main__":
    main()

# research-feeds


# Summary
Research Feeds is a lightweight system that builds and publishes custom RSS feeds for security research sites, blogs, and resources—even when those sites do not provide an official RSS/Atom feed.

- It runs on a GitHub Actions schedule, crawls each source from sources.json, and writes a per‑source RSS file to feeds/<slug>.xml.
- It also generates an index.html that groups feeds into Enabled and Disabled sections, and shows the last execution time per feed.
- Three modes of acquisition are supported:
  - auto — discover an official feed via <link rel="alternate"> plus common suffixes; otherwise fall back to scraping.
  - scrape — scrape the site’s listing page for recent posts and extract metadata from each article page.
  - sitemap — read one or more sitemaps to discover recent article URLs, then extract metadata from the article pages.



Purpose: give Threat Intel & SecOps teams a consistent, machine‑readable feed for research & advisories, independent of whether a site publishes an official feed—so downstream tools (readers, dashboards, enrichment pipelines) always have fresh inputs.

# How everything works
At a high level:

- Workflow runs (on schedule or manually) → update-feeds.yml.
- The workflow installs a few small dependencies and runs build_feeds.py.
- build_feeds.py:
  - Loads sources.json, separates Enabled vs Disabled.
  - For Enabled sources:
    - Runs in the configured mode (auto | scrape | sitemap).
    - Normalizes metadata for each discovered post (title, link, description, pubDate).
    - Writes a valid RSS 2.0 XML file at feeds/<slug>.xml.
  - For Disabled sources:
    - Skips processing but reads the previous feed’s <lastBuildDate> (if any) to display in the index.
  - Generates a top‑level index.html that lists Enabled and Disabled feeds separately with their Last run times.



## Key behaviors & guardrails:

- Robots compliance: RESPECT_ROBOTS = True (default) means the crawler won’t fetch pages disallowed by robots.txt.
- Stability: Each per‑source run is wrapped in a try/except so one failing site won’t break the entire job.
- Sitemap smart‑order: In mode: "sitemap", the script prioritizes URLs that share the same leading path as the provided url (e.g., if url is https://site.com/blog/, URLs beginning with /blog/ are tried first).
- Output capping: Default MAX_ITEMS_PER_SOURCE = 20 items per feed; controlled page fetches limit runtime.


# Infrastructure: GitHub‑native (no server to host)
This project requires no self‑hosting. Everything runs and publishes on GitHub:
- GitHub Actions executes the builder on a schedule or on demand.
- GitHub Pages serves the generated static site (the index.html and feeds/*.xml) directly from your repository:
    - Enable Pages in your repository settings.
    - Set the Pages source to the branch and folder where index.html lives (commonly the repo root on main).
    - The workflow commits updated index.html and feeds/*.xml back to the repo; GitHub Pages picks up those changes automatically and serves them at your Pages URL.
- The PAGES_BASE environment variable (exported by the workflow) tells build_index() what absolute URL to render in the index. If PAGES_BASE is omitted, relative links are used.

Result: you get a hosted landing page and per‑source RSS endpoints without provisioning any servers, buckets, or runtimes.

# End‑to‑end Workflow of this project
- Scheduler / Manual Trigger
  - The workflow update-feeds.yml runs every 8 hours (cron) or via the Run workflow button.
- Runner setup
  - Checks out the repo.
  - Installs Python 3.11 and dependencies: requests, beautifulsoup4, feedparser.
  - Exports PAGES_BASE with your GitHub Pages URL.
- Feed build
  - Executes python build_feeds.py.
  - build_feeds.py reads sources.json, splits Enabled vs Disabled.
  - Enabled:
    - Uses auto/scrape/sitemap mode to discover items.
    - Enriches metadata (title/description/pubDate) from each article page.
    - Writes feeds/<slug>.xml (RSS 2.0), captures <lastBuildDate>.
- Disabled:
  - Skips crawling; attempts to read last build time from existing feeds/<slug>.xml.
- Writes a fresh index.html grouping Enabled/Disabled with Last run per feed.
- Publish
  - The workflow commits index.html and feeds/*.xml back to the repo.
  - GitHub Pages serves the updated outputs at your public URL.
  - Consumers (RSS readers, dashboards, etc.) subscribe to each feeds/<slug>.xml.


# File‑by‑file details
## build_feeds.py — What it does & main features
Core purpose: given a list of sources, build one RSS per source and a top‑level index.

Notable configuration constants
- USER_AGENT: Bot identifier sent with requests.
- TIMEOUT: Per-request timeout (sec).
- MAX_ITEMS_PER_SOURCE: How many items to emit in each RSS.
- MAX_ARTICLE_FETCHES: Upper bound on per‑article fetches (for metadata enrichment).
- RESPECT_ROBOTS: Whether to honor robots.txt (recommended = True).

Key building blocks
- HTTP & robots
  - get(url): thin wrapper over requests.get with headers/timeouts.
  - robots_allows(url): consults robots.txt for the target host via urllib.robotparser. If parsing fails, it fails open (still fetches) but you’ll still see downstream 403/429 if servers deny access.
- Auto‑discovery (mode: "auto")
  - discover_feed_urls(page_url):
    - Fetches the page and scans for <link rel="alternate" type="rss|atom|xml|json">.
    - Adds common suffixes (/feed, /rss.xml, ?feed=rss2, etc.) at the current host & apex (handles www. vs apex).
    - Special case: Medium supports /feed on publication/profile pages.
  - validate_feed(url): uses feedparser to test candidates; on success, entries are converted to minimal RSS items.
- Scraping (mode: "scrape")
  - scrape_items(page_url, limit):
    - Fetches the listing page and applies robust, generic CSS selectors such as article a[href], h2 a[href], a[href*="/blog/"], etc., to collect candidate links + anchor text.
    - Visits up to MAX_ARTICLE_FETCHES article pages to enrich metadata (title/description/date):
      - JSON‑LD (Article, BlogPosting, NewsArticle) if available.
      - Otherwise og:title, og:description, <time datetime>, article:published_time>, or URL date heuristics as a fallback.
    - Emits a short placeholder if nothing was discovered to keep the feed valid.
- Sitemap discovery (mode: "sitemap")
  - _read_robots_for_sitemaps(page_url): looks for Sitemap: lines in robots.txt.
  - Tries common sitemap candidates on the host root:
    - /sitemap.xml, /sitemap_index.xml, and their .gz variants.
  - _fetch_xml(url): fetches & decompresses .gz sitemaps when needed.
  - Parses sitemap XML via xml.etree.ElementTree (no lxml dependency); follows sitemap indexes recursively.
  - Path‑hint prioritization: after collecting URLs from sitemap(s), the script reorders the list so that links whose path starts with the provided url’s leading path (e.g., /blog) are tried first.
  - Respects RESPECT_ROBOTS before fetching each discovered URL.
- RSS writer & index builder
  - write_rss(out_path, channel_title, channel_link, items): emits a minimal, standards‑compliant RSS 2.0 file and returns the <lastBuildDate> used.
  - _read_existing_last_build(slug): pulls the previous <lastBuildDate> (used for Disabled feeds display).
  - build_index(feed_map_enabled, feed_map_disabled): writes a simple index.html that groups feeds into Enabled and Disabled sections and shows Last run timestamps for each.
- Main control flow
  - Loads sources.json.
  - Splits sources by "disabled": true.
  - Disabled: read prior lastBuildDate to display in the index; no crawling.
  - Enabled: run per‑source mode; build items; write feeds/<slug>.xml; record lastBuildDate.
  - Generates the top‑level index.html.

-Outputs
  - feeds/<slug>.xml — per‑source RSS.
  - index.html — front page with Enabled/Disabled lists and “Last run”.


## sources.json — What it contains & how to write entries
Purpose: declare the set of sources you want to track and how to acquire their items.
Schema (per entry):

JSON{  "name": "Human‑readable source name",  "url": "https://example.com/path/",  "slug": "kebab-case-unique-slug",  "mode": "auto|scrape|sitemap",  "disabled": false}Show more lines

### Fields
- name — Display name (also used in the channel <title> of the RSS).
- url — The starting URL for this source:
    - auto mode: a representative page (homepage, blog root, etc.) that might contain <link rel="alternate"> or be near conventional feed paths.
    - scrape mode: the listing page that renders recent posts (server‑rendered is ideal).
    - sitemap mode: any URL on the target host (commonly the blog root, e.g., https://site.com/blog/). The script reads robots.txt and tries root candidates (/sitemap*.xml) and will follow indexes recursively.
      - With the path‑hint, URLs with the same leading path (e.g., /blog) are prioritized.
- slug — Filename stem for the resulting feed (feeds/<slug>.xml). Use lowercase letters, digits, and hyphens only; avoid spaces.
- mode — Acquisition strategy:
  - auto → try to find an official feed; else the code falls back to scraping.
  - scrape → skip discovery; scrape the listing page.
  - sitemap → enumerate URLs from sitemaps and enrich items from article pages.
- disabled (optional) — When true, the source appears under Disabled in index.html; no fetching happens.

Example:
JSON
[
  {
    "name": "Abstract Security Blog",
    "url": "https://www.abstract.security/blog/",
    "slug": "abstract-security",
    "mode": "sitemap"
  }
]

### Tips

- If a site’s sitemap is site‑wide and mixes many non‑blog URLs, keeping url on the blog path (e.g., /blog/) helps the path‑hint push it to the top of the queue in sitemap mode.
- If the listing page is heavily client‑rendered, scrape might miss links; prefer sitemap in that case.

## update-feeds.yml — What the workflow does
Purpose: run the builder on a schedule (and on demand), publish new RSS files and the index page.
- Triggers
  - Schedule: cron: "5 */8 * * *" — runs every 8 hours at minute 5 past the hour.
  - Manual: workflow_dispatch enables the Run workflow button.

- Steps
  - Checkout the repo.
  - Set up Python (3.11).
  - Install dependencies:
    - requests — HTTP.
    - beautifulsoup4 — HTML parsing for discovery/scraping.
    - feedparser — validates official feeds during auto.
      - No lxml required; sitemap parsing uses stdlib xml.etree.ElementTree.
  - Set PAGES_BASE environment variable (used by index.html to produce absolute links to feeds/*.xml).
    - Example in the workflow:
ShellPAGES_BASE=https://<github_username>.github.io/<repo-name>/Show more lines
  - Build feeds:
Shellpython build_feeds.pyShow more lines
- Commit & push changes to index.html and feeds/*.xml.

### Customizing
- Change the cron to your preferred cadence.
- If you publish Pages under a different path or custom domain, adjust the PAGES_BASE export accordingly.


## index.html — What it shows
Purpose: quick landing page to browse feeds and see their freshness.
Content & behavior
- Two sections:
  - Enabled — lists each feed with:
    - a clickable link to "feeds/slug.xml"
    - the feed path in "code"
    - Last run: showing the "lastBuildDate" from the current run
  - Disabled — lists feeds that have "disabled": true in sources.json:
    - shows the path and “Last run:” read from the previous feeds/"slug".xml if present (or n/a)
- Styling is intentionally minimal and self‑contained (no external CSS/JS)
- All links can be absolute if PAGES_BASE is set; otherwise they are relative.


## Quick start
- Edit sources.json — add or adjust your sources (set mode per site)
- Commit & push — GitHub Actions will build on schedule; you can also run it manually
- Open:
  - "/index.html" to see Enabled/Disabled feeds and Last run times
  - "/feeds/slug.xml" to subscribe in a reader or consume downstream

## Notes & conventions
- Respecting robots: Keep RESPECT_ROBOTS = True unless you have a strong reason not to
- Slugs: Stick to a‑z, digits, and hyphens to ensure clean file paths and links
- Item limits: Tune MAX_ITEMS_PER_SOURCE and MAX_ARTICLE_FETCHES if a site posts very frequently or loads slowly
- Placeholders: If nothing suitable is found, a single placeholder item is emitted so the feed remains valid (helps with downstream consumers that require a feed to exist)

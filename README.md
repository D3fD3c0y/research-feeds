# RESEARCH-FEEDS


# Summary
This repository automatically builds and publishes RSS feeds from a curated list of cybersecurity / threat-intelligence sources. The end goal is to provide an always-updating set of operations-ready feeds (RSS 2.0) that can be subscribed to in any RSS reader or integrated into downstream automation workflows. The project runs on a schedule (every 8 hours UTC by default) via GitHub Actions, generates one RSS file per source, writes an index.html catalog, and commits the results back to the repository so they can be served by GitHub Pages.
At a high level, the project contains:

- build_feeds.py — the feed generator. It crawls each source using either sitemap discovery or scraping, extracts metadata from article pages, writes RSS files under feeds/, builds index.html, and maintains a persistent state.json to reduce repeat HTTP work.
- sources.json — the source registry. You define which sites to ingest, how to discover posts, and how strictly to scope URLs to the correct section of a site.
- GitHub Actions workflow (update-feeds.yml) — runs build_feeds.py on a schedule, uploads outputs/logs as artifacts, and commits changes.
- Output folders:
  - feeds/ — the generated RSS feeds.
  - artifacts/ — optional packaged outputs (when enabled), plus run manifests.


---


## Folder Structure
### .github/workflows/
Contains the GitHub Actions workflow file(s), especially:
- update-feeds.yml — schedule + manual trigger, runs Python generator, uploads artifacts, commits output.

---

### feeds/
Contains the generated RSS feed XML files, one per source:
- feeds/<slug>.xml — RSS 2.0 feed file created each run, where <slug> comes from sources.json.

---

### artifacts/
Contains optional “run outputs packaging” artifacts created by the generator (depending on configuration):
- artifact_manifest.json — machine-readable summary of run outputs (what was built, how many items, timestamps, errors)
- feeds_artifact.zip — a ZIP containing feeds/, index.html, sources.json, and manifests (if enabled by the build script / env)
Even if you don’t generate ZIP packages, GitHub Actions can upload artifacts (files/directories) so you can download the exact outputs from any run..py) 

---

### build_feeds.py
build_feeds.py is the heart of the project. It converts a list of web sources into consistent RSS feeds, while being respectful of site structure, reducing repeated HTTP work, and producing stable output files.
- What it does (in order)
#### 1) Loads configuration inputs
  - Reads sources.json (list of sources, modes, scoping rules).
  - Loads state.json (persistent cache of “seen URLs” per feed slug) to reduce repeated work.

#### 2) For each source:
For each source entry, the script:
  - Determines whether robots rules should be respected for that source (respect_robots)
  - Discovers candidate post URLs using one of these modes:
      - mode: "sitemap" — reads a sitemap (or sitemap index), collects URLs, and then fetches article pages for metadata.
      - mode: "scrape" — scrapes a listing page for links, filters them, and fetches article pages for metadata.
      - mode: "auto" — attempts to find a native RSS/Atom feed first; if none, falls back to scrape.
  - Applies scoping controls so you ingest the correct section (e.g., “News” vs “Blog” vs “Research”) even if the sitemap or page contains other site links.
  - Applies incremental logic (Goal A): fetch metadata for only up to NEW_ITEMS_PER_RUN new URLs per source, and then fill the rest of the feed from the previous run’s output (no HTTP needed).
  - Writes output feed XML under feeds/<slug>.xml.
  - Prints a tiny per-feed debug summary to the logs, showing new vs reused items.

#### 3) Builds index.html
After processing all sources, generates index.html, listing enabled/disabled feeds and their URLs.
#### 4) Saves state.json
Updates state.json with newly-seen URLs per feed slug, so next run can skip already-processed content.

### Modes: how discovery works
#### Mode: sitemap
Purpose: Use sitemap XML as the source of truth for content URLs.
Typical sitemap flow:
- Start with sitemap_url (if provided), else attempt common sitemap paths (e.g., /sitemap.xml, /sitemap_index.xml, .gz variants), and/or read Sitemap: directives from robots.txt.
- Parse <urlset> and <sitemapindex>. Nested sitemap indexes are followed.
- Collect candidate URLs, then filter/prioritize by sitemap_include_prefix and sitemap_strict.

Why this mode exists: Some sites don’t provide RSS feeds, but do provide reliable sitemaps; sitemaps often contain canonical URLs.

#### Mode: scrape
Purpose: Scrape a listing page (blog index, category page, “latest posts” page) and collect links.
Typical scraping flow:
- GET the listing page.
- Extract anchors using multiple selectors (article cards, headings, common “blog” path patterns, etc.).
- Normalize URLs, remove duplicates, drop unwanted “junk” links (tags, categories, login pages, mailto).
- Apply scrape_include_prefix + scrape_strict to avoid ingesting navigation links and unrelated sections.
- Fetch article pages and extract metadata.

Why this mode exists: Many modern sites don’t have clean sitemaps for specific sections, or their sitemaps contain lots of non-article URLs.

#### Mode: auto
Purpose: “Try RSS first.”
It attempts:
- `<link rel="alternate" type="application/rss+xml">` / Atom links
- common RSS URL patterns (/feed, /rss.xml, etc.)
- If it finds a valid feed with entries, it uses it. Otherwise it falls back to scrape.


#### Incremental behavior (Goal A)
This project deliberately avoids re-fetching the same articles repeatedly:
- Each run fetches at most NEW_ITEMS_PER_RUN (default 15) new article pages per feed.
- Already-seen URLs are tracked in state.json per feed slug.
- To keep the feed “full” (e.g., 30 items), the script merges:
  - new items fetched this run (up to 15)
  - previous items from the prior feeds/<slug>.xml (read locally, no HTTP)
- Result: stable feed length with greatly reduced HTTP work and fewer repeated requests.

#### Debug summary line (per feed)
After each feed is processed, the script prints:
- new_fetched: number of new URLs fetched this run
- new_used: number of those new URLs that ended up in the final feed after merge/dedupe
- reused: number of items carried over from the previous feed file
- prev_cached: how many items were available from last feed file
- seen_before: how many URLs were already in state for that feed
- seen_added: how many URLs were added to state this run
This makes it easy to confirm incremental behavior just by reading run.log.

#### Robots.txt handling and caching
If respect_robots: true, the script checks robots.txt for crawling permissions before fetching article pages. It also caches parsed robots rules per host during the run to avoid re-downloading robots.txt for every URL (significant HTTP reduction).
- Note: This project aims to follow robots directives when enabled per source; you should set respect_robots appropriately for your policies and environment.


#### Environment variables (advanced)
These can be used to tune behavior without code changes:
`#limit how many new article pages are fetched per feed per run
NEW_ITEMS_PER_RUN=15`
`# maximum URLs stored per feed in state.json
MAX_SEEN_PER_FEED=800`
`# where state is stored
FEED_STATE_PATH=state.json`
`# used by index.html to build absolute URLs
PAGES_BASE=https://<user>.github.io/<repo>/`

---

### index.html
index.html is a human-friendly catalog page for the feeds.
#### What it does
- Lists all enabled sources as clickable links to their RSS feeds (feeds/<slug>.xml).
- Lists all disabled sources separately.
- Shows lastBuildDate (the time of feed generation) for each feed, as recorded in the RSS file.
- Supports GitHub Pages publishing so you can browse feeds in a browser instead of navigating repository files.

#### How it is generated
build_feeds.py writes index.html after processing all sources. It uses the environment variable PAGES_BASE (if set by the workflow) to construct absolute URLs.

#### How it is used
If your repository is configured with GitHub Pages, index.html can be served as the landing page. GitHub Pages supports publishing from a selected branch/folder, or via a workflow-based build/deploy approach. 

---

### run.log
run.log is the execution log output of the generator during a workflow run.
### What it contains
- High-level “Processing: ” lines
- HTTP request tracing lines (when debug is enabled), including:
  - request type (robots-check, sitemap, scrape-listing, article)
  - URL fetched
  - HTTP status code
  - final URL after redirects
  - content type
  - body snippet (for non-200 responses, helpful for diagnosing WAF blocks and incorrect sitemap formats)
- Tiny per-feed summary line, indicating incremental behavior (new vs reused)

#### Where it is produced
In update-feeds.yml, the build step pipes Python output to tee run.log, so everything printed by build_feeds.py goes into run.log.
#### Where it is viewed
- In the GitHub Actions run logs (Actions UI), since it’s standard step output. GitHub supports viewing/searching/downloading workflow logs at the run/job level.
- As a downloadable artifact (this workflow uploads run.log using actions/upload-artifact). Artifacts are intended to preserve files created during a run for later download..py) 

---

### sources.json
sources.json is the registry of all feeds you want the project to generate.
It is a JSON array of objects. Each object describes one source and how to ingest it.
#### Common fields
##### name
Human-friendly label for the source (used in output feed titles and index.html).
##### url
The primary URL for the source:
- For mode: "scrape": the listing page (blog index / category page).
- For mode: "sitemap": the logical “section page” for scoping (even if sitemap is elsewhere).
- For mode: "auto": the homepage/listing page used to discover an RSS feed link.
##### slug
A stable identifier used for filenames:
- Output feed: feeds/<slug>.xml
- Also used as the key in state.json.
##### mode
Defines discovery method:
- "scrape" — parse listing page links
- "sitemap" — parse sitemap URLs
- "auto" — try official feed, then fallback to scrape
##### disabled
If true, the source is not processed; it will appear under “Disabled” in index.html.
##### respect_robots
Controls whether robots.txt rules are respected for this source:
- true: check robots before fetching pages
- false: do not check robots
Tip: If a site blocks robots.txt access (403), the script may treat that conservatively depending on response. In general, enabling respect_robots is the safer/courteous option.


#### Sitemap-specific fields
##### sitemap_url
Explicit sitemap URL to use first (e.g., https://example.com/sitemap.xml or a specific section sitemap).
This is strongly recommended when the site uses multiple sitemap files or non-standard locations.
##### sitemap_include_prefix
A path prefix used to prioritize or filter sitemap URLs, e.g.:
- "/eng/news/"
- "/eng/expertise/research/"
This prevents sitemaps from feeding unrelated URLs into your feed (common when sitemaps contain the entire site).
##### sitemap_strict
Boolean controlling strictness:
- false (default): URLs matching sitemap_include_prefix are processed first; if there aren’t enough matches, the script may pull from other sitemap URLs to fill.
- true: only URLs that match the prefix are eligible. If none match, the feed may end up with fewer or no new items.
Use sitemap_strict: true when you must avoid mixing sections.

#### Scrape-specific fields
##### scrape_include_prefix
A path prefix used to prioritize or filter scraped links from the listing page.
Example for a blog:
- "/resources/blog/"
This is crucial for sites where the listing page includes global nav links (platform, careers, etc.) that you do not want in the feed.
##### scrape_strict
Boolean controlling strictness:
- false (default): prefer matching links first; allow others if needed.
- true: only include scraped links whose paths start with the prefix.
Use scrape_strict: true when you want a feed that contains only true post pages and not internal navigation.

Example source entry
`{
  "name": "Example Vendor Blog",
  "url": "https://vendor.com/blog/",
  "sitemap_url": "https://vendor.com/sitemap.xml",
  "sitemap_include_prefix": "/blog/",
  "sitemap_strict": false,
  "scrape_include_prefix": "/blog/",
  "scrape_strict": true,
  "slug": "vendor-blog",
  "mode": "sitemap",
  "disabled": false,
  "respect_robots": true
  }`

### state.json
state.json is a persistent incremental cache used to reduce repeated HTTP work across workflow runs.
#### What it does
For each feed slug, it stores:
- seen_urls: list of URLs that have already been processed (most recent first)
- last_success_utc: timestamp of last successful update for that feed
#### Why it exists
Many sources keep the same “top N” items on their listing pages or sitemaps. Without a state file, the workflow would re-fetch and parse the same article pages on every run.
With state.json, the generator can:
- skip already-seen URLs
- fetch only the next set of unseen URLs (up to NEW_ITEMS_PER_RUN)
- reuse older items from the previous feed file
#### Growth control
To avoid unbounded growth, the list is capped:
- maximum entries per feed = MAX_SEEN_PER_FEED (default 800)

---

### artifact_manifest.json
artifact_manifest.json is a run manifest that summarizes what was generated.
This file is used when the generator is configured to package outputs for download and auditing.

#### What it typically contains
- generated_at_utc: timestamp of run
- feeds: list of feeds generated, each including:
  - name
  - slug
  - source URL
  - mode
  - status / error
  - item_count
  - output path
  - lastBuildDate

#### Why it’s useful
- Quick auditing: “what did this run produce?”
- Troubleshooting: identify which sources failed without scanning full logs
- Automation: downstream processes can consume a single JSON manifest

---

### feeds_artifact.zip
feeds_artifact.zip is a packaged snapshot of run outputs, intended to be uploaded as a GitHub Actions artifact for easy download.
#### What it contains
Typically:
- feeds/ directory (all generated feed XML files)
- index.html
- sources.json (for traceability / auditing)
- artifact_manifest.json (run summary)

#### Why it exists
- Lets you download “everything that run produced” in one file
- Supports debugging/validation without relying on commits or Pages deployment

GitHub Actions artifacts are designed exactly for this: keeping build outputs available for download after a run..py)

---

### update-feeds.yml
update-feeds.yml is the GitHub Actions workflow that orchestrates automation.
#### What it does
- Runs every 8 hours (UTC) and supports manual triggers (workflow_dispatch)
- Checks out the repository
- Installs Python dependencies
- Sets PAGES_BASE for the index.html generator
- Executes build_feeds.py and writes output to run.log
- Uploads artifacts:
  - full outputs (feeds/, index.html, and optional artifacts/)
  - run.log
Commits and pushes changes back to the repo

#### Why it uploads artifacts
Artifacts allow you to inspect the exact outputs of a workflow run (even if no commit occurs or if you want to compare runs). GitHub supports downloading workflow logs and build artifacts from run pages. 

---

### Workflow
This section is the “high-level sequence” of how everything works end-to-end when the workflow runs.
#### 1) Trigger: schedule or manual
- GitHub Actions triggers update-feeds.yml on the cron schedule or via manual dispatch.
- You can view each run in the Actions tab and inspect step logs.
#### 2) Checkout & environment setup
- Workflow checks out repository contents.
- Sets up Python and installs dependencies (requests, beautifulsoup4, feedparser).
#### 3) Generator execution (build_feeds.py)
- Reads sources.json to know what to process.
- Loads state.json to skip already-seen URLs.
- For each source:
  - chooses discovery method based on mode (sitemap/scrape/auto)
  - applies include-prefix scoping (sitemap_include_prefix/scrape_include_prefix)
  - applies strict flags (sitemap_strict/scrape_strict)
  - fetches up to NEW_ITEMS_PER_RUN new items
  - merges with previous feed output to keep stable feed size
  - prints debug summary line
  - writes feeds/<slug>.xml
- Writes index.html
- Saves updated state.json
#### 4) Artifacts upload (per run)
- Workflow uploads:
  - generated outputs (feeds + index + artifacts dir)
  - run.log Artifacts can be downloaded later from the run page..py)
#### 5) Commit & push
- Workflow stages changes (feeds/, index.html, state.json, and any artifacts/manifest)
- Commits if there are changes
- Pushes to main
#### 6) Publishing (GitHub Pages)
- If configured, GitHub Pages serves the updated content (index + feed XML).
- GitHub Pages can be configured to publish from a branch/folder.


#### Optional: Local run instructions (recommended)
If you want to run locally (for testing):
`python -m pip install --upgrade pippip install requests beautifulsoup4 feedparserpython build_feeds.py`

Outputs:
- feeds/*.xml
- index.html
- state.json (after first run)

---

## Troubleshooting (WAF / Bot Protection blocks)

Some sources are protected by a Web Application Firewall (WAF) or bot-management system (often Cloudflare), which can block non-browser traffic and return “challenge” pages (e.g., **“Just a moment…”**). This project is intentionally lightweight (HTTP + HTML parsing) and does **not** attempt to solve browser challenges automatically; instead, it focuses on safe, respectful handling and resiliency. 

### Common symptoms in `run.log`

You’re likely seeing a WAF/bot challenge when you observe one or more of the following patterns:

- HTTP status **403** or **503**, with HTML content where you expected article HTML or XML (sitemap). 
- Log body snippet includes phrases like **“Just a moment…”**, “Checking your browser…”, or `__cf_chl_*` tokens (Cloudflare challenge markers). 
- Repeated “challenge loop” behavior (same request repeatedly results in a challenge), which Cloudflare notes can happen when it detects strong bot signals or network/browser constraints. 

> Tip: This repository already prints a short response snippet for non‑200 responses when debug logging is enabled, which makes challenge pages easy to spot in `run.log`. 

---

### Where to view evidence (logs + outputs)

When a source is blocked, you’ll typically want both the logs **and** the exact generated outputs from that run.

- **Workflow logs (Actions UI):** GitHub lets you view, search, and download job logs for each run; the “Build feeds” step will contain your `[HTTP] …` debug lines. 
- **Artifacts:** This repo’s workflow uploads `run.log` and the generated outputs (feeds + index) as artifacts, which you can download from the run page’s **Artifacts** section. GitHub explicitly supports downloading workflow artifacts before they expire. 

---

### Why this happens (high-level)

Cloudflare and similar WAFs use challenges to distinguish automated traffic from real browsers. A “Just a moment…” page usually indicates the request **did not pass a JavaScript/cookie validation step** (common for server-side HTTP clients). 

Cloudflare’s own troubleshooting guidance highlights that challenge loops can be influenced by factors like unstable network conditions, blocked scripts, or signals that appear bot-like; they recommend trying different networks/devices in human scenarios, which also suggests that **network origin and reputation can matter** for automated traffic. 

---

### What to do (safe, compliant mitigations)

These steps focus on **resilience** and **reducing unnecessary load**, not bypassing protections.

#### 1) Reduce request volume and frequency (preferred first step)

- Keep the project’s **incremental mode** enabled (via `state.json`) so the workflow fetches only a small number of _new_ pages per run, instead of re-downloading the same top posts repeatedly. This reduces the chance of triggering rate limits and minimizes repeated hits. 
- If a site is sensitive, increase the schedule interval or reduce `NEW_ITEMS_PER_RUN` (for example, from 15 down to 5) to avoid looking like aggressive automation. 

#### 2) Prefer official feeds / vendor endpoints whenever possible

- If the site provides a native RSS/Atom feed, use `mode: "auto"` or point directly to the feed; official feeds are more likely to be allowed and stable than scraping protected pages. 
- If a site offers a dedicated section sitemap (instead of a site-wide sitemap), use that explicit `sitemap_url` to reduce irrelevant crawling and page fetches. 

#### 3) Tighten URL scoping to avoid “wasting” requests

When a source’s sitemap or listing page contains unrelated site navigation links, you may accidentally fetch non-post pages, increasing request count without value.

- Use `sitemap_include_prefix` + `sitemap_strict` to focus only on the correct path (e.g., `/eng/news/`).
- Use `scrape_include_prefix` + `scrape_strict` to stop the scraper from following header/footer navigation links.  
    This reduces total fetches and keeps activity closer to the expected section. 

#### 4) Treat persistent WAF blocks as “unavailable from this environment”

If the same source is consistently challenged/blocked from GitHub-hosted runners, consider:

- **Disable the source** (`"disabled": true`) to keep the pipeline clean until you have a vendor-approved alternative.
- Use an **alternate official content channel** (vendor newsletter feed, press page feed, GitHub releases, etc.) if available.
- If you have authorization, consider running the workflow from a different network environment (for example, a self-hosted runner) as Cloudflare troubleshooting notes that changing networks can affect whether challenges are solvable. 

> Important: This project does **not** include “challenge-solving” automation. If a site requires interactive challenges, the recommended path is to use official access methods or coordinate with the site owner. 

---

### Escalation path (when you need reliability)

If you need a blocked source reliably, the most robust approach is to work with the site owner:

- Ask for an official RSS/Atom feed endpoint or API suitable for automated consumption. 
- Provide evidence from `run.log` (status codes and the response snippet) and, if relevant, Cloudflare identifiers (e.g., **Ray ID**) to help them create a targeted allow rule; Cloudflare recommends sharing error codes/Ray IDs with the site administrator when challenges persist. 

---

### Quick checklist for diagnosing a blocked source

1. Confirm the failure pattern in `run.log` (403/503 + HTML snippet containing challenge markers). 
2. Download the run’s artifacts (`feed-output…` + `feed-run-log…`) to see exactly what was generated. 
3. Tighten `sources.json` scoping (`*_include_prefix`, `*_strict`) to reduce unnecessary requests. 
4. Reduce request volume (`NEW_ITEMS_PER_RUN`, schedule frequency) and rely on incremental caching (`state.json`). 
5. If still blocked, prefer official feeds or disable the source until a vendor-supported method is available.



## Troubleshooting (WAF / Bot Protection blocks)

### Symptom-based Table of Contents

Use this table to jump directly to the symptom you’re seeing in `run.log` or in the workflow outputs. Workflow logs (including `run.log` content) can be viewed/searched/downloaded per run, and artifacts can be downloaded from the run page’s **Artifacts** section. 

- **Access blocked / challenge pages**
    - #http-403503-just-a-momentchecking-your-browser
    - #challenge-loop-repeated-challenges 
    - #http-1020-access-denied-firewall-rule 
- **Sitemap and parsing issues**
    - #sitemap-returns-html-instead-of-xml 
    - #sitemap-pulls-wrong-section-too-broad 
- **Rate limits and connectivity**
    - #http-429-too-many-requests-throttling 
    - #timeouts-read-timed-out 
- **Robots and policy constraints**
    - #robotstxt-blocks-crawling 
- **Output symptoms**
    - #feed-contains-repeated-items-every-run 
    - #feed-contains-navigation-pages-or-unrelated-links 
    - #feed-has-no-items-discovered 
- **Where to look for evidence**
    - #where-to-inspect-logs-and-artifacts 

---

### Where to inspect logs and artifacts

- **Workflow logs:** GitHub lets you view, search, and download logs for each job/step in a workflow run; the “Build feeds” step contains the live output from `build_feeds.py`. 
- **Artifacts:** You can download run artifacts from the run page (Artifacts section), which is ideal for grabbing the exact generated `feeds/` files, `index.html`, and `run.log`. 

---

### HTTP 403/503 “Just a moment…”/“Checking your browser…”

**What it means:** The origin is returning a bot/WAF challenge page instead of the expected content. Cloudflare’s “Just a moment…” pages are commonly associated with JS/cookie-based challenges that non-browser HTTP clients typically won’t complete. 

**How to confirm:** In `run.log`, the response snippet often includes the challenge HTML markers or the “Just a moment…” title. 

**Safe mitigations (non-bypass):**

- Reduce request volume (rely on incremental `state.json`, reduce `NEW_ITEMS_PER_RUN`, and/or run less frequently) to avoid tripping automated protections. 
- Prefer official RSS/Atom feeds or vendor-provided endpoints if available. 
- If it remains consistently blocked from GitHub-hosted runners, treat the source as unavailable from this environment or use an approved alternate channel. 

---

### Challenge loop (repeated challenges)

**What it means:** A challenge keeps reappearing and never resolves. Cloudflare notes this can happen when it detects “strong bot signals” or when environment/network conditions prevent the challenge from completing. 

**Safe mitigations (non-bypass):**

- Reduce frequency/volume and avoid aggressive retry patterns. 
- Consider a different execution environment (for example, a self-hosted runner) if you have permission to access the content and the vendor allows it, because Cloudflare explicitly suggests network changes can affect challenge behavior. 

---

### HTTP 1020 “Access Denied” / firewall rule

**What it means:** A firewall rule is blocking the request (often shown as “Access Denied”). Cloudflare troubleshooting guidance recommends involving the site administrator and sharing identifiers (like Ray ID) when blocks persist. 

**Safe mitigations (non-bypass):**
- Ask the site owner for an official feed/API or allowlisting for your use case. 

---

### Sitemap returns HTML instead of XML

**What it means:** The sitemap endpoint responded with HTML (often an error page or WAF interstitial) rather than valid XML. Your logs will show `CT=text/html` and a snippet that begins with `<!DOCTYPE html>`. 

**Mitigations:**

- Use a more specific `sitemap_url` (section sitemap) if available. 
- If the site is WAF-protected, treat this as a WAF symptom and use the 403/503 guidance above. 

---

### Sitemap pulls wrong section (too broad)

**What it means:** A site-wide sitemap contains many unrelated URLs, and without scoping your feed can “spend” its URL budget on non-post pages. 

**Mitigation:**

- Set `sitemap_include_prefix` and consider `sitemap_strict: true` to keep only URLs within the intended section. 

---

### HTTP 429 Too Many Requests / throttling

**What it means:** The origin is rate-limiting you. Cloudflare challenge troubleshooting emphasizes that bot-like patterns and request behavior can trigger stronger defenses, so rate-limiting is often a precursor to challenges. 

**Mitigations:**

- Reduce `NEW_ITEMS_PER_RUN`, slow schedule, and rely on incremental caching to lower request volume. 

---

### Timeouts (Read timed out)

**What it means:** The origin did not respond within your configured timeout. This is usually network-path variability, slow origins, or transient throttling. Your logs will show read-timeout exceptions in the `[HTTP]` lines. 

**Mitigations:**

- Increase `DOMAIN_TIMEOUTS` for the affected host. 
- Keep retries enabled (already supported in the Requests session config). [[

---

### robots.txt blocks crawling

**What it means:** If `respect_robots` is true, the script checks robots.txt before fetching, and may skip URLs disallowed for the user-agent. 

**Mitigations:**

- Set `respect_robots: false` only if you are authorized and your usage policy allows it; otherwise prefer official feeds/APIs. 

---

### Feed contains repeated items every run

**What it means:** Many sites keep a stable “top N” listing; without incremental caching you would re-fetch the same items. This repo uses `state.json` + `NEW_ITEMS_PER_RUN` to reduce repeated work and only fetch a small number of new URLs each run. 

**Mitigation:**

- Ensure `state.json` is being committed and `NEW_ITEMS_PER_RUN` is set appropriately. 

---

### Feed contains navigation pages or unrelated links

**What it means:** Listing pages often include header/footer links and category navigation; broad scraping selectors can capture those. 

**Mitigation:**

- Use `scrape_include_prefix` and set `scrape_strict: true` to keep only post URLs. 

---

### Feed has “no items discovered”

**What it means:** No qualifying URLs were found, or page fetches were blocked/timeouts. Use logs/artifacts to see whether you got 403/503 challenge pages, timeouts, or unrelated URLs. 

**Mitigation:**

- Switch mode (`sitemap` ↔ `scrape`), tighten prefixes, or use an official feed if available

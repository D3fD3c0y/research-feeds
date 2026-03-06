# research-feeds


# Summary
This repository automatically builds and publishes RSS feeds from a curated list of cybersecurity / threat-intelligence sources. The end goal is to provide an always-updating set of operations-ready feeds (RSS 2.0) that can be subscribed to in any RSS reader or integrated into downstream automation workflows. The project runs on a schedule (every 8 hours UTC by default) via GitHub Actions, generates one RSS file per source, writes an index.html catalog, and commits the results back to the repository so they can be served by GitHub Pages. [sophosapps...epoint.com], [abnormal.ai]
At a high level, the project contains:

- build_feeds.py — the feed generator. It crawls each source using either sitemap discovery or scraping, extracts metadata from article pages, writes RSS files under feeds/, builds index.html, and maintains a persistent state.json to reduce repeat HTTP work.
- sources.json — the source registry. You define which sites to ingest, how to discover posts, and how strictly to scope URLs to the correct section of a site.
- GitHub Actions workflow (update-feeds.yml) — runs build_feeds.py on a schedule, uploads outputs/logs as artifacts, and commits changes.
- Output folders:
  - feeds/ — the generated RSS feeds.
  - artifacts/ — optional packaged outputs (when enabled), plus run manifests.



## Folder Structure
### .github/workflows/
Contains the GitHub Actions workflow file(s), especially:
- update-feeds.yml — schedule + manual trigger, runs Python generator, uploads artifacts, commits output. [sophosapps...epoint.com], [sophosapps...epoint.com]

### feeds/
Contains the generated RSS feed XML files, one per source:
- feeds/<slug>.xml — RSS 2.0 feed file created each run, where <slug> comes from sources.json.

### artifacts/
Contains optional “run outputs packaging” artifacts created by the generator (depending on configuration):
- artifact_manifest.json — machine-readable summary of run outputs (what was built, how many items, timestamps, errors)
- feeds_artifact.zip — a ZIP containing feeds/, index.html, sources.json, and manifests (if enabled by the build script / env)
Even if you don’t generate ZIP packages, GitHub Actions can upload artifacts (files/directories) so you can download the exact outputs from any run..py) [rss.feedspot.com], [sophosapps...epoint.com]

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
- "<link rel="alternate" type="application/rss+xml"> / Atom links"
- common RSS URL patterns (/feed, /rss.xml, etc.)
- If it finds a valid feed with entries, it uses it. Otherwise it falls back to scrape.


### Incremental behavior (Goal A)
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
Shell# limit how many new article pages are fetched per feed per runNEW_ITEMS_PER_RUN=15# maximum URLs stored per feed in state.jsonMAX_SEEN_PER_FEED=800# where state is storedFEED_STATE_PATH=state.json# used by index.html to build absolute URLsPAGES_BASE=https://<user>.github.io/<repo>/Show more lines

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
- In the GitHub Actions run logs (Actions UI), since it’s standard step output. GitHub supports viewing/searching/downloading workflow logs at the run/job level. [sophosapps...epoint.com], [sophosapps...epoint.com]
- As a downloadable artifact (this workflow uploads run.log using actions/upload-artifact). Artifacts are intended to preserve files created during a run for later download..py) [rss.feedspot.com], 


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
JSON{  "name": "Example Vendor Blog",  "url": "https://vendor.com/blog/",  "sitemap_url": "https://vendor.com/sitemap.xml",  "sitemap_include_prefix": "/blog/",  "sitemap_strict": false,  "scrape_include_prefix": "/blog/",  "scrape_strict": true,  "slug": "vendor-blog",  "mode": "sitemap",  "disabled": false,  "respect_robots": true}Show more lines

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

### Workflow
This section is the “high-level sequence” of how everything works end-to-end when the workflow runs.
#### 1) Trigger: schedule or manual
- GitHub Actions triggers update-feeds.yml on the cron schedule or via manual dispatch.
- You can view each run in the Actions tab and inspect step logs. [sophosapps...epoint.com], [sophosapps...epoint.com]
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
Shellpython -m pip install --upgrade pippip install requests beautifulsoup4 feedparserpython build_feeds.pyShow more lines

Outputs:
- feeds/*.xml
- index.html
- state.json (after first run)


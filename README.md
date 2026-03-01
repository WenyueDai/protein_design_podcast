# OpenClaw Knowledge Radio

A fully automated daily podcast for protein designers. Every morning at **05:00 UTC**, a GitHub Actions workflow wakes up on GitHub's servers, runs the entire pipeline without any computer needing to be on, and publishes a fresh ~60-minute episode to GitHub Pages.

**Live site:** [wenyuedai.github.io/openclaw_podcast](https://wenyuedai.github.io/openclaw_podcast)
**Paper collection (Notion):** [all past digests](https://clear-squid-8e3.notion.site/3155f58ea8c280258959fba00c0149ab?v=3155f58ea8c2803c8c0d000c76d1bfba)
**Deep dive notes (Notion):** [owner's expert annotations](https://clear-squid-8e3.notion.site/3165f58ea8c280498f72c770028aec0d?v=3165f58ea8c28020983c000cec9807e6)

---

## Full End-to-End Workflow

### Phase 1 — Paper Collection (05:00 UTC)

GitHub Actions checks out the latest `main` branch and runs `python run_daily.py`.

**1a. RSS feeds** (`src/collectors/rss.py`)
Fetches 42 RSS/Atom feeds simultaneously, grouped into:

- **Core protein/structural biology** — Nature (protein design, engineering, antibodies, enzyme design, structural & molecular biology, methods, main journal), arXiv q-bio.BM and q-bio.QM, Protein Science, Protein Engineering Design and Selection, Structure (Cell Press)
- **Top journals** — Nature Biotechnology, Nature Chemical Biology, PNAS
- **AI/ML** — arXiv cs.LG
- **News** — Nature News, Endpoints News, Quanta Magazine
- **Key researchers (absolute priority)** — arXiv author feeds for David Baker, Sergey Ovchinnikov, Alexander Rives, Brian Hie, Charlotte Deane, Jeffrey Gray, Tanja Kortemme, Po-Ssu Huang, Noelia Ferruz, Debora Marks, Kevin Yang, Yaron Lipman, Chloe Hsu, Jure Leskovec, Regina Barzilay, Bonnie Berger, Tommi Jaakkola
- **Blogs (absolute priority)** — A-Alpha Bio, Owl Posting, Asimov Press, Mohammed AlQuraishi's blog, BLOPIG (Oxford Protein Informatics Group), In the Pipeline (Derek Lowe)

Each item gets a `bucket` tag: `protein`, `journal`, `ai_bio`, `news`, or `daily`.

**1b. PubMed search** (`src/collectors/pubmed.py`)
Runs ~18 keyword queries against the PubMed E-utilities API (e.g. "protein design deep learning", "de novo enzyme design", "protein fitness landscape"). Returns articles published in the last 2 days.

**1c. Deduplication**
Every item URL is checked against `state/seen_ids.json`, which persists across days. Items seen in previous runs are dropped. New items are added to seen_ids at the end of the run, so the podcast never repeats content. Runner-up articles that don't make the episode cap are intentionally kept unseen so they remain available for quieter days.

**1d. Content filtering**
Items whose title, source, or URL contain any term from `excluded_terms` in `config.yaml` are dropped (e.g. "mouse", "single-cell", "neurogenesis"). Source caps prevent any single broad journal from dominating (e.g. Nature main journal: max 3 items, PNAS: max 3).

---

### Phase 2 — Analysis & Ranking

**2a. Parallel article fetch + LLM analysis** (`src/processing/article_analysis.py`)
Up to 8 articles are fetched and analyzed in parallel using a `ThreadPoolExecutor`. For each article, `newspaper4k` + `BeautifulSoup` extract the full text from the paper's webpage. A fast LLM (OpenRouter `stepfun/step-3.5-flash:free`) then reads the text and returns a structured analysis: core claim, novelty, and relevance score.

**2b. Ranking** (`src/processing/rank.py`)
Items are sorted by a 9-level priority key (lower = better):

| Priority | Factor | Rationale |
|----------|--------|-----------|
| 0 | **Absolute author sources** — researcher feeds (Baker, Ovchinnikov…) and blogs with `author` tag | Curated, highest trust |
| 1 | **Absolute title keywords** — AlphaFold, RoseTTAFold, ESMFold, RFdiffusion, ProteinMPNN, OpenFold, OmegaFold, Chai-1, Boltz, "structure prediction" | Landmark papers regardless of source |
| 2 | **Missed paper keywords** — topics extracted from owner-submitted missed papers (`boosted_topics.json`) | Ground truth: papers actively sought out that the pipeline failed to collect |
| 3 | **Config topic keywords** — "antibody design", "enzyme design", "diffusion model", "protein language model", etc. | Broad topic steering |
| 4 | **Journal quality** — Nature Biotech/Chem Bio/Structural > PNAS > Nature main > arXiv > others | Source credibility |
| 5 | **Research bucket** — protein/journal/ai_bio before news | Domain relevance |
| 6 | **Feedback from likes** — papers from sources/titles the owner previously selected | Lighter weight: avoids selection-bias feedback loop |
| 7 | **Fulltext available** — papers where full text was successfully extracted | Content quality |
| 8 | **Extracted text length** | Final tie-breaker |

Top 52 items are selected for the episode (38 max from the `protein` bucket). Per-source and per-bucket caps apply.

---

### Phase 3 — Script Generation

**3a. LLM script writing** (`src/processing/script_llm.py`)
The ranked items are sent in batches to the main LLM (`arcee-ai/trinity-large-preview:free` via OpenRouter). For each paper, the LLM writes:
- A **deep-dive segment** (~250–300 words): background, methodology, findings, significance
- A **roundup blurb** (~100 words): quick summary for papers that didn't make the deep-dive

Segments are joined with `[[TRANSITION]]` markers. The final script is saved as `output/DATE/podcast_script_DATE_llm.txt`.

---

### Phase 4 — Text-to-Speech

**4a. One MP3 per segment** (`src/outputs/tts_edge.py`)
The script is split on `[[TRANSITION]]` markers into individual segments. Each segment is converted to a separate MP3 using Microsoft Edge TTS (voice: `en-GB-RyanNeural`, rate: `+35%`). Edge TTS is free and runs over a WebSocket to Microsoft's servers.

- If Edge TTS fails or produces a corrupt file, it retries up to 3 times with fallback voices
- If all retries fail, it falls back to gTTS (Google TTS, lower quality but reliable)
- Existing valid MP3s are reused on re-runs (skip if file exists and passes ffprobe validation)

**4b. Concatenation with transitions** (`src/outputs/audio.py`)
All segment MP3s are concatenated by ffmpeg with a short transition sound between each paper:
```
[1.0s silence] → [ding C6, 0.12s] → [gap 0.06s] → [ding E6, 0.12s] → [1.0s silence]
```
The entire output is sped up by `atempo=1.2` (20% faster playback). Final file: `output/DATE/podcast_DATE.mp3` (~60–70 minutes).

**4c. Timestamp calculation**
For each segment, the raw cumulative position is measured using `mutagen` (frame-accurate for VBR MP3). Timestamps are stored in `output/DATE/episode_items.json` so clicking a paper on the website seeks the audio player to exactly 0.5 seconds before the transition tones for that paper.

---

### Phase 5 — Publishing

**5a. GitHub Release** (`src/outputs/github_publish.py`)
The pipeline calls the **GitHub REST API** to:
1. Delete any existing release for today's date
2. Create a new GitHub Release tagged `episode-DATE`
3. Upload `podcast_DATE.mp3` as a release asset

The MP3 is served directly from GitHub's CDN via the release asset URL. This avoids storing large audio files in the git repository itself.

**5b. GitHub Pages site rebuild** (`tools/build_site.py`)
`build_site.py` is called to regenerate the `docs/` folder:
- Reads `state/release_index.json` to know which audio URLs are available
- Reads `output/DATE/episode_items.json` for paper titles, timestamps, and summaries
- Reads `state/paper_notes.json` to bake any owner notes into the HTML
- Reads `state/missed_papers.json` to bake submitted missed papers into the HTML
- Writes `docs/index.html` (the main site), `docs/feed.xml` (RSS podcast feed), `docs/cover.svg`

**5c. Notion digest** (`src/outputs/notion_publish.py`)
Creates a new page in the Paper Collection Notion database summarizing today's episode: list of all papers with titles, sources, and one-line summaries.

**5d. Git commit and push**
The GitHub Actions workflow commits all changed files back to `main`:
```
state/seen_ids.json        ← updated with today's paper URLs
state/release_index.json   ← updated with today's audio URL
output/DATE/               ← episode items, status, script
docs/                      ← rebuilt GitHub Pages site
```
GitHub Pages detects the change to `docs/` and automatically redeploys the website within ~30 seconds.

---

### Phase 6 — Interactive Features (browser-side)

These happen **in your browser**, not on GitHub's servers.

**6a. Clicking [N] to seek audio**
Each paper number `[N]` on the site is a `<span>` with `onclick="seekTo(this, event)"`. Clicking it sets `audio.currentTime = timestamp` where the timestamp was pre-calculated in Phase 4c. The audio player jumps to 0.5s before the transition tones for that paper.

**6b. Submitting a missed paper** (owner only)
The "Submit a missed paper" section at the bottom of the page is for the owner's use only. The JS:
1. Reads your GitHub token from `localStorage` (set once in ⚙ Settings)
2. Calls `GET /contents/state/missed_papers.json` to check for duplicate titles
3. Appends the entry and calls `PUT` to commit it directly to GitHub

This commit **immediately triggers** the `process_missed.yml` workflow (see Phase 7), so diagnosis and a Notion stub appear within ~2 minutes. The 3 most recent submissions are shown on the page with a collapsible "Show all" link.

**6c. Saving feedback** (owner only)
Checking paper checkboxes and clicking "Save feedback" triggers JavaScript that:
1. Reads your GitHub token from `localStorage` (set once in ⚙ Settings)
2. Calls `GET /contents/state/feedback.json` to fetch the current file + its SHA
3. Merges your new selections into the existing data
4. Calls `PUT` to commit the change

The next day's pipeline reads `feedback.json` and uses it to apply a soft ranking boost (tier 6, below missed paper keywords and journal quality).

**6d. Writing "My Take" notes** (owner only)
Clicking ✏️ next to a paper opens an inline textarea. Saving calls the same GitHub API pattern as feedback, but writes to `state/paper_notes.json` with `{note, title, source}` per paper URL. This commit to `paper_notes.json` **automatically triggers** the `sync_notes.yml` GitHub Actions workflow (see Phase 8).

---

### Phase 7 — Missed Paper Processing (`process_missed.yml`)

Whenever the owner submits a missed paper, the **`process_missed.yml`** workflow fires immediately (triggered by a push to `state/missed_papers.json`). It also runs as part of the daily pipeline.

1. **Diagnose** each unprocessed entry:
   | Diagnosis | Meaning |
   |-----------|---------|
   | `already_collected` | The URL's SHA1 was already in `seen_ids.json` — paper ran in a previous episode |
   | `excluded_term` | An `excluded_terms` keyword (e.g. "mouse", "single-cell") matched the title |
   | `source_not_in_rss` | The URL's domain is not in any configured RSS feed |
   | `low_ranking` | The source domain is in RSS feeds but the paper was cut below the episode cap or wasn't in the recent 24h window |

2. **Extract keywords** (for `low_ranking` and `source_not_in_rss`): calls OpenRouter LLM to extract 3–5 topic phrases from the title. These are merged (case-insensitive dedup) into `state/boosted_topics.json`. The next daily run's ranker loads these as **tier-2 priority** — above config topic keywords and above feedback from likes.

3. **Discover RSS feed** (for `source_not_in_rss`): probes common feed paths (`/feed`, `/rss`, `/feed.xml`, etc.) on the paper's domain, and looks for `<link rel="alternate">` tags on the article page. If a valid feed is found, it is saved to `state/extra_rss_sources.json` and merged into the RSS collection on the next daily run.

4. **Create Notion stub**: creates a page in the Deep Dive Notes database labelled "Missed Paper" with the diagnosis, keywords boosted, and a bookmark to the paper.

5. Rebuilds the site (so diagnosis badges appear on the page) and commits `missed_papers.json`, `boosted_topics.json`, `extra_rss_sources.json`, and `docs/` back to `main` with `[skip ci]`.

---

### Phase 8 — Notion Deep-Dive Sync (`sync_notes.yml`)

Whenever `paper_notes.json` is updated, the **`sync_notes.yml`** workflow fires automatically:

1. GitHub detects a push that changed `state/paper_notes.json`
2. Runs `tools/sync_notion_notes.py` with your `NOTION_API_KEY` secret
3. For each note not yet synced (checked against `state/notion_created.json`):
   - Looks up the paper title and source from `output/DATE/episode_items.json`
   - Calls `POST https://api.notion.com/v1/pages` to create a stub page in your Deep Dive database (labelled "Daily Note")
   - The page contains: your note in a green callout box, paper metadata, a bookmark to the paper, and an empty "Deep Dive Notes" section for you to fill in
   - If the note text changes later, the existing page is updated (no duplicates)
4. Commits `notion_created.json` back to the repo (`[skip ci]` prevents an infinite loop)

You then open Notion at your leisure to write the full deep dive.

---

## How GitHub Actions Works

GitHub Actions is a CI/CD platform built into every GitHub repository. A **workflow** is a YAML file in `.github/workflows/` that defines:

- **When** to run: on a schedule (`cron`), on a `push`, on a path change, or manually
- **What** to run: a sequence of steps on a fresh Ubuntu virtual machine

For this project:

```
.github/workflows/
├── daily_podcast.yml    ← runs at 05:00 UTC daily (cron schedule)
│                           includes: main pipeline + process_missed_papers.py
├── sync_notes.yml       ← runs whenever paper_notes.json is pushed (owner notes → Notion)
└── process_missed.yml   ← runs whenever missed_papers.json is pushed (immediate diagnosis)
```

Each workflow run gets a **brand-new virtual machine**. It:
1. Has nothing installed by default (we install Python, ffmpeg, pip packages each time)
2. Has access to **repository secrets** (API keys stored encrypted on GitHub, never in code)
3. Can push back to the repository using a GitHub PAT stored as a secret
4. Runs for free on GitHub's servers (2000 minutes/month on free plan; this pipeline uses ~10–15 min/day)

```
GitHub's servers
   ┌──────────────────────────────────────────┐
   │  Ubuntu VM (fresh each day at 05:00 UTC)  │
   │  1. git checkout main                     │
   │  2. pip install -r requirements.txt       │
   │  3. apt install ffmpeg                    │
   │  4. python run_daily.py                   │ ← reads secrets from env vars
   │  5. git commit && git push                │
   └──────────────────────────────────────────┘
         ↓ pushes to
   GitHub repository (main branch)
         ↓ docs/ changed
   GitHub Pages redeploys automatically
```

## How the GitHub API Is Used

The GitHub REST API (`api.github.com`) allows any program — including browser JavaScript — to read and write repository contents without git. This project uses it in two ways:

**From the pipeline (Python):**
```
GitHub API (authenticated with GH_PAT secret)
├── Create/delete releases  →  POST/DELETE /repos/.../releases
├── Upload audio assets     →  POST /repos/.../releases/{id}/assets
└── (site rebuild is done by pushing to docs/ via git, not the API)
```

**From your browser (JavaScript, owner only):**
```
GitHub API (authenticated with token stored in localStorage)
├── Read/write feedback      →  GET+PUT /repos/.../contents/state/feedback.json
├── Read/write notes         →  GET+PUT /repos/.../contents/state/paper_notes.json
└── Read/write missed papers →  GET+PUT /repos/.../contents/state/missed_papers.json
```

The `PUT /contents/...` endpoint is GitHub's way of creating or updating a single file. It requires the file's current `sha` (to prevent conflicting edits) and the new content as base64. No server is needed — this works directly from any web browser.

---

## Daily Workflow (for the owner)

```
05:00 UTC  GitHub Actions runs automatically
              ↓
           New episode published to:
           • GitHub Pages (audio player + paper list)
           • GitHub Release (MP3 file)
           • Notion Paper Collection (text digest)

Morning    Open wenyuedai.github.io/openclaw_podcast
           Listen to podcast (e.g. during a 1-hour run)
           Click [N] on any paper to jump directly to it in the audio

After run  For interesting papers:
           • Check the ☑ checkbox → "Save feedback" → soft boost for similar papers tomorrow
           • Click ✏️ → type a quick expert note → Save
                 ↓
             paper_notes.json updated in GitHub repo
                 ↓ (sync_notes.yml triggers automatically, ~1 min)
             Notion deep-dive stub created with your note + paper link

Anytime    Found a paper the pipeline missed? Submit it via "Submit a missed paper"
                 ↓ (~2 minutes)
             Diagnosis badge appears, Notion stub created
             Keywords extracted → boosted_topics.json updated
             Next run: similar papers rise to tier-2 priority

Later      Open Notion Deep Dive database
           Find the stub page → expand with your full analysis
```

---

## Repository Layout

```
openclaw-knowledge-radio/         ← Python pipeline package
├── run_daily.py                  ← main entry point
├── config.yaml                   ← all settings (sources, limits, LLM, TTS)
├── requirements.txt
├── src/
│   ├── collectors/
│   │   ├── rss.py                ← RSS/Atom feed fetcher (42 sources)
│   │   ├── pubmed.py             ← PubMed E-utilities search
│   │   └── daily_knowledge.py    ← (disabled) Wikipedia daily facts
│   ├── processing/
│   │   ├── article_analysis.py   ← parallel LLM article analysis
│   │   ├── rank.py               ← 9-level ranking (see Phase 2b)
│   │   └── script_llm.py         ← podcast script generation
│   └── outputs/
│       ├── tts_edge.py           ← Edge TTS → MP3 per segment
│       ├── audio.py              ← ffmpeg concat + atempo + transitions
│       ├── github_publish.py     ← GitHub Releases + GitHub Pages push
│       └── notion_publish.py     ← Notion paper collection digest
├── tools/
│   ├── build_site.py             ← generates docs/ (HTML + RSS feed)
│   ├── sync_notion_notes.py      ← syncs owner notes → Notion deep-dive stubs
│   └── process_missed_papers.py  ← diagnoses missed papers + extracts boost keywords
├── state/
│   ├── seen_ids.json             ← URLs seen in previous runs (dedup)
│   ├── release_index.json        ← date → GitHub Release audio URL
│   ├── feedback.json             ← owner's paper selections (soft ranking signal)
│   ├── paper_notes.json          ← owner's expert notes per paper
│   ├── notion_created.json       ← tracks which notes have been synced to Notion
│   ├── missed_papers.json        ← owner-submitted missed papers (with diagnoses)
│   ├── boosted_topics.json       ← keywords from missed papers (tier-2 ranking priority)
│   └── extra_rss_sources.json    ← RSS feeds discovered from missed paper URLs
└── output/YYYY-MM-DD/            ← per-episode data (kept 30 days)
    ├── podcast_YYYY-MM-DD.mp3    ← final audio
    ├── podcast_script_*_llm.txt  ← LLM-generated script
    ├── episode_items.json        ← paper list + timestamps
    └── status.json               ← run metadata

docs/                             ← GitHub Pages site (auto-generated, never edit manually)
.github/workflows/
├── daily_podcast.yml             ← cron: 05:00 UTC daily
├── sync_notes.yml                ← on push to state/paper_notes.json
└── process_missed.yml            ← on push to state/missed_papers.json
```

---

## Setup (for a new installation)

### 1. Clone and install

```bash
git clone https://github.com/WenyueDai/openclaw_podcast.git
cd openclaw_podcast/openclaw-knowledge-radio
pip install -r requirements.txt
sudo apt install ffmpeg        # Linux
# brew install ffmpeg          # macOS
```

### 2. Environment variables

Create `openclaw-knowledge-radio/.env`:

```env
OPENROUTER_API_KEY=sk-or-v1-...
GITHUB_TOKEN=ghp_...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
NOTION_TOKEN=ntn_...
NOTION_DATABASE_ID=3155f58ea8c280258959fba00c0149ab
```

### 3. Run manually

```bash
cd openclaw-knowledge-radio
set -a && source .env && set +a
python run_daily.py
```

Optional flags:
```bash
REGEN_FROM_CACHE=true python run_daily.py    # reuse today's cached items (skip re-fetch)
DEBUG=true python run_daily.py               # skip seen-URL dedup
RUN_DATE=2026-02-20 python run_daily.py      # generate for a specific past date
```

### 4. GitHub Actions secrets

Go to `Settings → Secrets and variables → Actions` and add:

| Secret | Description |
|--------|-------------|
| `GH_PAT` | GitHub PAT with `repo` + `workflow` scopes (for pushing commits and uploads) |
| `OPENROUTER_API_KEY` | OpenRouter API key (for LLM script + analysis + missed-paper keyword extraction) |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook (optional, for run notifications) |
| `NOTION_TOKEN` | Notion integration token for the **Paper Collection** database |
| `NOTION_DATABASE_ID` | Paper Collection database ID |
| `NOTION_API_KEY` | Notion integration token for the **Deep Dive Notes** database |

### 5. Browser setup (for owner interactive features)

On the GitHub Pages site, click **⚙ Settings** and enter:
- Your GitHub personal access token (`repo` scope)
- Your repo (`WenyueDai/openclaw_podcast`)

This is stored only in your browser's `localStorage`. It enables the feedback checkboxes, ✏️ note buttons, and the missed paper submission form.

---

## Configuration Reference (`config.yaml`)

| Section | Key settings |
|---------|-------------|
| `limits` | `max_items_total` (52), `max_items_protein` (38), `source_caps` (per-journal caps) |
| `excluded_terms` | Keywords that filter out off-topic items (cell biology, neurogenesis, etc.) |
| `rss_sources` | 42 feeds with `name`, `url`, `bucket`, `tags`; sources tagged `author` get tier-0 absolute priority |
| `pubmed` | `search_terms` (18 queries), `lookback_days`, `max_results_per_term` |
| `podcast` | `voice`, `voice_rate`, `target_minutes` |
| `llm` | `model` (script), `analysis_model` (per-article analysis), `provider` |
| `ranking` | `absolute_title_keywords` (tier-1 landmark model names), `absolute_source_substrings`, `source_priority_rules`, `topic_boost_keywords` (tier-3) |

---

## Active features

Every GitHub Actions run does `git checkout main` as its first step, so it always runs the **latest committed code**.

- ✅ 42 RSS sources covering protein design, structural biology, AI/ML, key researchers, and curated blogs
- ✅ 9-level ranking: absolute authors → absolute title keywords → missed paper keywords → config topics → journal quality → bucket → feedback (lighter) → fulltext → length
- ✅ Absolute author priority: researcher arXiv feeds and curated blogs (tagged `author`) always make the episode first
- ✅ Absolute title keywords: AlphaFold, RoseTTAFold, ESMFold, RFdiffusion, ProteinMPNN, OpenFold, OmegaFold, Chai-1, Boltz, "structure prediction" get tier-1 priority regardless of source
- ✅ Missed paper keyword boost: topics from owner-submitted missed papers go into `boosted_topics.json` at tier-2 — above config topic keywords and feedback
- ✅ Feedback (likes) at tier-6: still useful as a soft signal, but demoted to avoid selection-bias feedback loops
- ✅ Timestamp fix: clicking `[N]` lands 0.5s before transition tones
- ✅ Source caps: Nature main / NSMB / PNAS / Structure capped at 3 items each
- ✅ "My Take" notes: ✏️ button on each paper, saves to GitHub + triggers Notion stub creation (labelled "Daily Note")
- ✅ Missed paper form: owner submits papers the pipeline missed; immediate diagnosis + Notion stub (labelled "Missed Paper") via `process_missed.yml`
- ✅ RSS discovery: `source_not_in_rss` papers trigger a feed probe; discovered feeds saved to `extra_rss_sources.json`
- ✅ `daily_knowledge` disabled: no Wikipedia filler

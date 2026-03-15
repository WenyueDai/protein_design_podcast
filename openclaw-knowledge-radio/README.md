# openclaw-knowledge-radio

A daily protein-design podcast pipeline that collects papers and news, ranks them, synthesises a deep-dive briefing via LLM, converts it to speech, and publishes to GitHub Pages + Spotify-compatible RSS + Notion.

---

## What it produces every day

| Output | Where |
|---|---|
| Deep-dive podcast MP3 | GitHub Release (audio only) |
| Episode website | GitHub Pages (`docs/`) |
| Daily digest (paper list) | Notion digest database |
| Full transcript | Notion transcript database |
| Episode items index | `output/YYYY-MM-DD/episode_items.json` |

---

## Pipeline overview

```
Collect → Rank → S2 Enrich → LLM Synthesis → TTS → Publish
```

### 1. Collect

Parallel collection from multiple sources:

- **RSS** — arXiv q-bio, Nature journals, PNAS, biotech blogs, Substacks, Endpoints News, Quanta Magazine
- **PubMed** — keyword search across ~35 protein-design terms + dynamic terms extracted from your feedback history
- **bioRxiv (keywords)** — same keyword set as PubMed
- **bioRxiv (authors)** — tracked PI feeds (~30 authors, checked daily)
- **Semantic Scholar authors** — permanent S2 author IDs, 4-day lookback window to catch delayed ingestion

Items are deduplicated across days using `state/seen_ids.json`. Only items not seen in prior episodes are processed (runner-ups are preserved so weekend episodes don't run dry).

### 2. Rank

Multi-tier ranking — lower tier = higher priority. Within each tier, items are sorted by subsequent tiers as tiebreakers.

| Tier | Signal |
|---|---|
| **0a** | **Absolute top authors** — guaranteed top-5 deep-dive (see below) |
| **0b** | Other tracked researcher arXiv/bioRxiv feeds |
| 1 | Tracked blogs/Substacks |
| 2 | Landmark titles (AlphaFold, RoseTTAFold, RFdiffusion, ProteinMPNN, …) |
| 3 | Missed-paper keywords (papers you manually submitted that the pipeline missed) |
| 4 | Feedback score — time-decayed (14-day half-life) signal from papers you liked |
| 5 | Topic keywords from `config.yaml` (`topic_boost_keywords`) |
| 6 | Journal quality (Nature Biotech/Chem Bio → 1, PNAS → 2, Nature → 3, arXiv → 5, …) |
| 7 | Bucket (protein > journal > ai_bio > daily > news) |
| 8 | S2 reference score (how grounded the paper is in protein-design literature) |
| 9/10 | Full text availability + extracted text length |

**Absolute top authors (tier 0a)** — papers from these authors always surface to the top:
David Baker, William DeGrado, Bruno Correia, Po-Ssu Huang, Brian Kuhlman,
Martin Steinegger, Charlotte Deane, Jeffrey Gray, Sergey Ovchinnikov, Brian Hie, Minkyung Baek

After sorting, tier 0a + 0b papers are hoisted to the front before bucket quotas are applied.

### 3. Semantic Scholar enrichment

Two passes:

**Pre-ranking (top 60 candidates)**
- Fetches reference list for each paper
- Computes `s2_reference_score` (used as tier 8 tiebreaker)
- Stores top 8 most-cited references per paper (`s2_top_refs`) — injected into LLM prompt
- Builds `shared_landscape` — papers cited by 2+ of today's featured papers collectively
- Surfaces `missed_surfaces` — highly-cited papers not yet seen by the pipeline

**Post-ranking (final top 5 only)**
- Fetches S2 abstract + TLDR
- Downloads open-access PDF and extracts up to 30,000 chars of full text (fallback if primary extraction was thin)

### 4. LLM synthesis

`synthesis_mode: true` — generates a deep 11-section briefing from the **top 5 featured papers**.

Each paper's LLM prompt includes:
- Full text (or best available text, up to 30k chars)
- Its top 8 cited references (related literature context)
- The shared landscape (foundational papers cited across multiple featured papers)

The remaining papers (up to 20 total) appear greyed-out on the website and are available for feedback and notes, but are not narrated.

Transcript is saved to a dedicated Notion database and indexed in `state/transcript_notion_index.json`.

### 5. TTS + audio

- Edge-TTS (en-GB-RyanNeural) with configurable speed
- One MP3 per script section, concatenated with transition sound effects
- Files > 10 MB are automatically split for Telegram delivery

### 6. Publish

- **GitHub Release** — MP3 uploaded as a release asset
- **GitHub Pages** — static site rebuilt and pushed to `docs/`
- **Notion digest** — ranked paper list saved to main Notion database
- **Notion transcript** — full synthesis script saved to transcript database

---

## Configuration (`config.yaml`)

Key sections:

```yaml
podcast:
  featured_count: 5          # papers for deep-dive
  synthesis_mode: true        # deep briefing vs per-paper summaries
  target_minutes: 60

ranking:
  absolute_top_author_substrings:   # tier 0a — guaranteed top-5
    - "david baker"
    - ...
  absolute_source_substrings:       # tier 0b — other tracked authors
    - "tanja kortemme"
    - ...
  absolute_title_keywords:          # tier 2 — landmark tools
    - "AlphaFold"
    - ...
  topic_boost_keywords:             # tier 5
    - "de novo protein"
    - ...

llm:
  provider: "openrouter"
  model: "arcee-ai/trinity-large-preview:free"
  analysis_model: "stepfun/step-3.5-flash:free"

publish:
  enabled: true
  github_release_repo: "WenyueDai/protein_design_podcast"
```

---

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | LLM script generation |
| `GITHUB_TOKEN` | Yes | Release upload + Pages push |
| `NOTION_TOKEN` | Yes | Digest + transcript save |
| `NOTION_DATABASE_ID` | Yes | Notion digest database |
| `NOTION_TRANSCRIPT_DATABASE_ID` | Yes | Notion transcript database |
| `S2_API_KEY` | Recommended | S2 enrichment + full-text fetch |
| `SLACK_WEBHOOK_URL` | Optional | Daily run summary notification |

---

## Running locally

```bash
cd openclaw-knowledge-radio
export OPENROUTER_API_KEY="sk-or-..."
export GITHUB_TOKEN="ghp_..."
export NOTION_TOKEN="ntn_..."
export NOTION_DATABASE_ID="..."
export NOTION_TRANSCRIPT_DATABASE_ID="..."
.venv/bin/python run_daily.py
```

**Regenerate from cached seed** (re-run LLM + TTS without re-fetching):
```bash
REGEN_FROM_CACHE=true .venv/bin/python run_daily.py
```

**Run for a specific date:**
```bash
RUN_DATE=2026-03-10 .venv/bin/python run_daily.py
```

**Force republish** (override idempotency guard):
```bash
FORCE_REPUBLISH=true .venv/bin/python run_daily.py
```

---

## Tools

| Script | Purpose |
|---|---|
| `tools/build_site.py` | Rebuild `docs/` static site |
| `tools/process_missed_papers.py` | Submit missed papers to boost future ranking |
| `tools/sync_notion_notes.py` | Sync paper notes from Notion |
| `tools/setup_s2_authors.py` | Resolve and store S2 author IDs |
| `tools/check_feeds.py` | Validate RSS feed health |

---

## State files (`state/`)

| File | Purpose |
|---|---|
| `seen_ids.json` | Deduplication store across episodes |
| `release_index.json` | Date → GitHub Release MP3 URL mapping |
| `transcript_notion_index.json` | Date → Notion transcript page URL |
| `feedback.json` | User likes (used for tier 4 ranking) |
| `boosted_topics.json` | Keywords from missed papers (tier 3) |
| `s2_author_ids.json` | Resolved Semantic Scholar author IDs |
| `missed_papers.json` | Papers submitted via process_missed_papers tool |

#!/usr/bin/env python3
"""
tools/weekly_summary.py

Reads this week's papers directly from the daily podcast output
(output/YYYY-MM-DD/episode_items.json), writes a 7-section deep-research
briefing, and saves it to the Weekly Summary Notion database.
Runs automatically on Saturdays via daily_podcast.yml.

The briefing covers all papers the pipeline surfaced this week, ranked by
the pipeline itself (highlighted = top-5 per day). No manual curation
required — you read the briefing, then it tells you which 3-5 papers are
worth reading in full (Section 7).

Env vars required:
  NOTION_API_KEY             — same integration token used by sync_notion_notes.py
  NOTION_WEEKLY_SUMMARY_DB_ID — id of the Weekly Summary database
  OPENROUTER_API_KEY         — same key used by script_llm.py

Optional:
  SLACK_WEBHOOK_URL          — for success/failure notifications
  RUN_DATE                   — override "today" (YYYY-MM-DD), for backfilling
  SEMANTIC_SCHOLAR_API_KEY   — enriches top papers with year + citation count
"""
from __future__ import annotations

from pathlib import Path as _Path
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

import os
import sys
import json
import time
import requests
import yaml
from datetime import datetime, timedelta

sys.path.insert(0, str(_Path(__file__).parent.parent))
from src.utils.timeutils import load_tz, now_local_date

WEEKLY_DB_ID = os.environ.get("NOTION_WEEKLY_SUMMARY_DB_ID", "").replace("-", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
S2_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")

NOTION_HEADERS = {
    "Authorization": f"Bearer {os.environ.get('NOTION_API_KEY', '')}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

CONFIG_PATH = _Path(__file__).parent.parent / "config.yaml"
OUTPUT_DIR = _Path(__file__).parent.parent / "output"


def _cfg() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _slack(msg: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": msg}, timeout=15)
    except Exception as e:
        print(f"[slack] failed to post: {e}", flush=True)


# ---------------------------------------------------------------------------
# Collect papers from this week's episode output files
# ---------------------------------------------------------------------------

def collect_week_papers(start: str, end: str) -> list[dict]:
    """Read episode_items.json for each day in [start, end], deduplicated by URL."""
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    seen_urls: set[str] = set()
    papers: list[dict] = []

    current = start_dt
    while current <= end_dt:
        date_str = current.date().isoformat()
        items_file = OUTPUT_DIR / date_str / "episode_items.json"
        if items_file.exists():
            try:
                data = json.loads(items_file.read_text(encoding="utf-8"))
                items = data.get("items", []) if isinstance(data, dict) else data
                for item in items:
                    url = item.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        papers.append({
                            "date": date_str,
                            "title": item.get("title", "").strip(),
                            "url": url,
                            "source": item.get("source", ""),
                            "one_liner": (item.get("one_liner") or "")[:500].strip(),
                            "highlighted": item.get("highlighted", False),
                            "tags": item.get("tags", []),
                        })
            except Exception as e:
                print(f"[weekly] Could not read {items_file}: {e}", flush=True)
        current += timedelta(days=1)

    return papers


# ---------------------------------------------------------------------------
# Optional Semantic Scholar enrichment (best-effort, never fatal)
# ---------------------------------------------------------------------------

def s2_lookup(title: str) -> dict:
    try:
        r = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": title, "fields": "title,year,citationCount,abstract", "limit": 1},
            headers={"x-api-key": S2_API_KEY} if S2_API_KEY else {},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                time.sleep(1.05)
                return data[0]
        time.sleep(1.05)
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def call_llm(system: str, user: str, cfg: dict) -> str:
    from src.processing.script_llm import _chat_complete, _client_from_config
    llm_cfg = cfg.get("llm", {})
    client = _client_from_config(llm_cfg)
    return _chat_complete(
        client,
        model=llm_cfg.get("model", "nvidia/nemotron-3-super-120b-a12b:free"),
        system=system,
        user=user,
        temperature=0.4,
        max_tokens=7000,
        fallback_models=llm_cfg.get("model_fallbacks", []),
    )


# ---------------------------------------------------------------------------
# Markdown -> Notion blocks
# ---------------------------------------------------------------------------

def markdown_to_blocks(md: str) -> list[dict]:
    CHUNK = 1900
    blocks = []

    def rich(text): return [{"type": "text", "text": {"content": text[:CHUNK]}}]

    for raw_line in md.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}})
            continue
        if line.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1",
                            "heading_1": {"rich_text": rich(line[2:])}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                            "heading_2": {"rich_text": rich(line[3:])}})
        elif line.startswith("> "):
            blocks.append({"object": "block", "type": "quote",
                            "quote": {"rich_text": rich(line[2:])}})
        elif line.startswith("---"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        else:
            for chunk_start in range(0, len(line), CHUNK):
                blocks.append({"object": "block", "type": "paragraph",
                                "paragraph": {"rich_text": rich(line[chunk_start:chunk_start + CHUNK])}})
    return blocks


def save_to_notion(title: str, date_end: str, md: str) -> str | None:
    if not WEEKLY_DB_ID:
        print("[weekly] NOTION_WEEKLY_SUMMARY_DB_ID not set — skipping Notion save", flush=True)
        return None
    blocks = markdown_to_blocks(md)
    first_batch, rest = blocks[:100], blocks[100:]
    body = {
        "parent": {"database_id": WEEKLY_DB_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": title[:2000]}}]},
            "date": {"date": {"start": date_end}},
        },
        "children": first_batch,
    }
    r = requests.post("https://api.notion.com/v1/pages", json=body, headers=NOTION_HEADERS, timeout=30)
    r.raise_for_status()
    page = r.json()
    page_id, page_url = page["id"], page.get("url", "")
    while rest:
        batch, rest = rest[:100], rest[100:]
        requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            json={"children": batch}, headers=NOTION_HEADERS, timeout=30,
        ).raise_for_status()
    return page_url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = _cfg()
    tz = load_tz(cfg.get("timezone", "Europe/London"))
    end = os.environ.get("RUN_DATE") or now_local_date(tz)
    # Saturday covers Sunday→Saturday: go back 6 days to land on last Sunday
    start = (datetime.fromisoformat(end) - timedelta(days=6)).date().isoformat()

    print(f"[weekly] Running for {start} → {end}", flush=True)

    papers = collect_week_papers(start, end)
    if not papers:
        print("[weekly] No episode output found for this week — nothing to summarize.", flush=True)
        _slack(f":pause_button: No podcast output found for {start}→{end} — weekly briefing skipped.")
        return

    highlighted = [p for p in papers if p["highlighted"]]
    all_papers = papers  # use everything; LLM will prioritise

    print(f"[weekly] {len(papers)} papers total ({len(highlighted)} featured) across {start}→{end}", flush=True)

    # S2 enrichment on highlighted papers only (to keep runtime reasonable)
    for p in highlighted[:15]:
        s2 = s2_lookup(p["title"])
        if s2:
            p["year"] = s2.get("year")
            p["citations"] = s2.get("citationCount")
            if s2.get("abstract"):
                p["one_liner"] = (s2["abstract"][:500]).strip() or p["one_liner"]

    # Build paper list for the prompt — featured papers first, rest after
    def _fmt(p: dict, featured: bool) -> str:
        marker = "★ FEATURED" if featured else "  greyed-out"
        line = (f"[{p['date']}] {marker} | {p['source']}\n"
                f"Title: {p['title']}\n"
                f"URL: {p['url']}")
        if p.get("year"):
            line += f"\nYear: {p['year']}, Citations: {p.get('citations', '?')}"
        if p["one_liner"]:
            line += f"\nSummary: {p['one_liner']}"
        return line

    featured_text = "\n\n".join(_fmt(p, True) for p in highlighted)
    other_text = "\n\n".join(_fmt(p, False) for p in all_papers if not p["highlighted"])
    papers_block = f"=== FEATURED PAPERS (pipeline top-5 per day) ===\n\n{featured_text}"
    if other_text:
        papers_block += f"\n\n=== OTHER PAPERS (greyed-out, lower-ranked) ===\n\n{other_text}"

    system = (
        "You are writing a weekly deep-research briefing for a computational "
        "protein/antibody designer. You have access to all papers their automated "
        "pipeline surfaced this week, with summaries. You did NOT write these summaries "
        "— treat them as raw source material, not conclusions."
    )

    user = f"""All papers surfaced by the podcast pipeline this week ({start} to {end}):

{papers_block}

Write the briefing using this exact structure. Each section assumes the reader
just finished the previous one — never re-explain a finding, use "(→ Insight N)"
back-references instead (a monthly process later scans for this exact syntax).

Content must start with:
# Weekly deep research briefing — {start} to {end}

> Papers covered: [list all FEATURED paper titles, with year/citations where known]

---

# 1. Key insights
5-8 insights drawn from the featured papers (greyed-out papers may add supporting context).
For each:
## Insight N — [sharp, specific title]
**Problem:** one sentence.
**Old assumption:** what the field believed.
**What changed:** mechanistic explanation, cite the paper.
**Real shift or incremental:** honest one-line assessment.

# 2. Connections and patterns
Pure extension of Section 1 — adjacent fields, meta-patterns (label: real trend / hype / too early to tell). No re-statement of Section 1.

# 3. Design heuristics
5-8 heuristics: **H[N] — [title]** / Rule: [If X then Y] / Fails when: [condition] / *(Paper)*

# 4. Methods worth stealing
2-4 methods: ## [Method] — (*Paper*) / **The clever move:** / **What alternative explanation it closes:** / **Reuse potential:**

# 5. Research directions
2-4 directions: **Direction N** / *Suggested by:* (→ Insight N) / *Smallest test:* / *Success looks like:* / *Main risk:*

# 6. Weekly update
**Belief updates** (5): > [Old] → [New] *(Paper)*
**Try this week** (3-5, with expected output)
**Stop trusting** (3)
**One surprising idea** (2-3 paragraphs)

# 7. Read in full this week
Pick 3-5 papers from the full list (featured or greyed-out) that are worth reading
in full — not just the headline but the methods and results. These should be papers
where the summary alone is insufficient to act on the insight.

For each:
## [Paper title] — [URL]
**Why this one:** 2 sentences on what you will get from reading it that the summary missed.
**Read for:** the specific section/figure/table that matters most.
**Time estimate:** realistic estimate (e.g. "30 min skim", "2h deep read").
"""

    print("[weekly] Calling LLM...", flush=True)
    briefing = call_llm(system, user, cfg)

    page_title = f"Deep Research Briefing {start} to {end}"
    print("[weekly] Saving to Notion...", flush=True)
    url = save_to_notion(page_title, end, briefing)

    if url:
        print(f"[weekly] Saved: {url}", flush=True)
        _slack(
            f":scroll: *Weekly briefing ready* ({len(highlighted)} featured + {len(papers)-len(highlighted)} "
            f"other papers, {start}→{end}): {url}"
        )
    else:
        print("[weekly] Warning: briefing generated but Notion save skipped.", flush=True)
        _slack(":warning: Weekly briefing generated but Notion save failed — check Action logs.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"[weekly] FAILED: {e}", flush=True)
        traceback.print_exc()
        _slack(f":x: Weekly briefing run failed: {e}")
        sys.exit(1)

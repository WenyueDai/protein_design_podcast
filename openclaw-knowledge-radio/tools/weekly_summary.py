#!/usr/bin/env python3
"""
tools/weekly_summary.py

Reads the past week's reviewed papers from the Deep Dive Notes database,
writes a 6-section deep-research briefing, and saves it to the
Weekly Summary database. Meant to run alongside the daily pipeline on
weekends (see .github/workflows/daily_podcast.yml).

Reading gate (enforced in code, not just convention):
  Only papers with Text == "Done" in Deep Dive Notes are included.
  Papers still "Not started" are EXCLUDED from the briefing and instead
  reported via Slack, so unread papers never silently disappear —
  they surface as a nag, not as content.

Env vars required:
  NOTION_API_KEY             — same integration token used by sync_notion_notes.py
  NOTION_DEEPDIVE_DB_ID       — default: 3165f58ea8c280498f72c770028aec0d
  NOTION_WEEKLY_SUMMARY_DB_ID — id of the Weekly Summary database (new — see README)
  OPENROUTER_API_KEY         — same key used by script_llm.py

Optional:
  SLACK_WEBHOOK_URL          — for the unread-papers nag + success notification
  RUN_DATE                   — override "today" (YYYY-MM-DD), for backfilling
  SEMANTIC_SCHOLAR_API_KEY   — optional citation/abstract enrichment
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

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
DEEPDIVE_DB_ID = os.environ.get("NOTION_DEEPDIVE_DB_ID", "3165f58ea8c280498f72c770028aec0d").replace("-", "")
WEEKLY_DB_ID = os.environ.get("NOTION_WEEKLY_SUMMARY_DB_ID", "").replace("-", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
S2_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

CONFIG_PATH = _Path(__file__).parent.parent / "config.yaml"


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
# Notion: query Deep Dive Notes for the week, split done vs unread
# ---------------------------------------------------------------------------

def query_deepdive_week(start: str, end: str) -> tuple[list[dict], list[dict]]:
    """Return (done_pages, unread_pages) with Date in [start, end]."""
    body = {
        "filter": {
            "and": [
                {"property": "Date", "date": {"on_or_after": start}},
                {"property": "Date", "date": {"on_or_before": end}},
            ]
        },
        "page_size": 100,
    }
    r = requests.post(
        f"https://api.notion.com/v1/databases/{DEEPDIVE_DB_ID}/query",
        json=body, headers=NOTION_HEADERS, timeout=30,
    )
    r.raise_for_status()
    results = r.json().get("results", [])

    done, unread = [], []
    for page in results:
        props = page.get("properties", {})
        status = ((props.get("Text") or {}).get("status") or {}).get("name", "")
        (done if status == "Done" else unread).append(page)
    return done, unread


def _title_of(page: dict) -> str:
    title_prop = page.get("properties", {}).get("Name", {}).get("title", [])
    return "".join(t.get("plain_text", "") for t in title_prop) or "(untitled)"


def _tags_of(page: dict) -> list[str]:
    ms = page.get("properties", {}).get("Multi-select", {}).get("multi_select", [])
    return [t["name"] for t in ms]


def _score_of(page: dict) -> str:
    sel = page.get("properties", {}).get("Score of interest", {}).get("select")
    return sel["name"] if sel else "unscored"


def fetch_owner_note(page_id: str) -> str:
    """Pull the owner's 'Deep Dive Notes' text (if any) from the page body."""
    try:
        r = requests.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=NOTION_HEADERS, timeout=30,
        )
        r.raise_for_status()
        blocks = r.json().get("results", [])
        text_parts = []
        for b in blocks:
            btype = b.get("type", "")
            content = b.get(btype, {})
            rich = content.get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rich)
            if text.strip():
                text_parts.append(text.strip())
        return "\n".join(text_parts)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Optional: light Semantic Scholar enrichment (best-effort, never fatal)
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
# LLM: reuse the repo's OpenRouter client + fallback chain
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
        max_tokens=6000,
        fallback_models=llm_cfg.get("model_fallbacks", []),
    )


# ---------------------------------------------------------------------------
# Markdown -> Notion blocks (same batching convention as notion_publish.py)
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
            for start in range(0, len(line), CHUNK):
                blocks.append({"object": "block", "type": "paragraph",
                                "paragraph": {"rich_text": rich(line[start:start + CHUNK])}})
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
    start = (datetime.fromisoformat(end) - timedelta(days=7)).date().isoformat()

    print(f"[weekly] Running for {start} → {end}", flush=True)

    done_pages, unread_pages = query_deepdive_week(start, end)

    if unread_pages:
        titles = "\n".join(f"• {_title_of(p)}" for p in unread_pages[:15])
        _slack(
            f":warning: *Weekly briefing {start}→{end}*: {len(unread_pages)} paper(s) still "
            f"unread (Text≠Done) and were *excluded* from this week's briefing:\n{titles}"
        )
        print(f"[weekly] {len(unread_pages)} unread paper(s) excluded, Slack notified", flush=True)

    if not done_pages:
        print("[weekly] No reviewed (Text=Done) papers this week — nothing to summarize.", flush=True)
        _slack(f":pause_button: No reviewed papers for {start}→{end} — weekly briefing skipped.")
        return

    # Gather paper context, with a light S2 enrichment pass on top-scored papers
    papers = []
    for p in done_pages:
        title = _title_of(p)
        entry = {
            "title": title,
            "tags": _tags_of(p),
            "score": _score_of(p),
            "owner_note": fetch_owner_note(p["id"]),
        }
        papers.append(entry)

    # Enrich the 10 most-interesting (by Score of interest) papers via S2
    def _score_num(e):
        try:
            return int(e["score"])
        except Exception:
            return 0
    for entry in sorted(papers, key=_score_num, reverse=True)[:10]:
        s2 = s2_lookup(entry["title"])
        if s2:
            entry["year"] = s2.get("year")
            entry["citations"] = s2.get("citationCount")
            entry["abstract"] = (s2.get("abstract") or "")[:600]

    papers_text = "\n\n".join(
        f"### {p['title']}\n"
        f"Tags: {', '.join(p['tags']) or 'none'} | Score of interest: {p['score']}"
        + (f" | {p['year']}, {p.get('citations', '?')} citations" if p.get("year") else "")
        + (f"\nAbstract: {p['abstract']}" if p.get("abstract") else "")
        + (f"\nOwner's notes: {p['owner_note']}" if p["owner_note"] else "")
        for p in papers
    )

    system = (
        "You are writing a weekly deep-research briefing for a computational "
        "protein/antibody designer, based on the papers they marked as read this week."
    )
    user = f"""Papers reviewed this week ({start} to {end}):

{papers_text}

Write the briefing using this exact structure. Each section assumes the reader
just finished the previous one — never re-explain a finding, use "(→ Insight N)"
back-references instead (a monthly process later scans for this exact syntax).

Content must start with:
# Weekly deep research briefing — {start} to {end}

> Papers: [all titles, with year/citations in brackets where known]

---

# 1. Key insights
5-8 insights. For each:
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
"""

    print("[weekly] Calling LLM...", flush=True)
    briefing = call_llm(system, user, cfg)

    page_title = f"Deep Research Briefing {start} to {end}"
    print("[weekly] Saving to Notion...", flush=True)
    url = save_to_notion(page_title, end, briefing)

    if url:
        print(f"[weekly] Saved: {url}", flush=True)
        _slack(f":scroll: Weekly briefing ready ({len(papers)} papers, {len(unread_pages)} skipped unread): {url}")
    else:
        print("[weekly] Warning: briefing was written but not saved to Notion.", flush=True)
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

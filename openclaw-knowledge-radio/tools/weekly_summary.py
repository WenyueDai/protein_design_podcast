#!/usr/bin/env python3
"""
tools/weekly_summary.py

Two-part Saturday briefing drawn from the Deep Dive Notes Notion database:

  Part A — Analysis (Sections 1-6):
    Papers you marked Text=Done this past week.
    Same 6-section deep-research format as always.
    Feeds the monthly evolution engine via the (→ Insight N) back-references.

  Part B — Reading list (Section 7):
    All papers still Text=Not started, ranked by priority.
    Recommends the 5-7 most worth reading next week, with a 2-sentence
    justification and time estimate for each.

If no papers are Done this week, Part A is skipped (only the reading list
is produced). If no papers are Not started, Section 7 is omitted.

Env vars required:
  NOTION_API_KEY             — same integration token used by sync_notion_notes.py
  NOTION_DEEPDIVE_DB_ID      — default: 3165f58ea8c280498f72c770028aec0d
  NOTION_WEEKLY_SUMMARY_DB_ID — id of the Weekly Summary database
  OPENROUTER_API_KEY         — same key used by script_llm.py

Optional:
  SLACK_WEBHOOK_URL          — for success/failure notifications
  RUN_DATE                   — override "today" (YYYY-MM-DD), for backfilling
  SEMANTIC_SCHOLAR_API_KEY   — enriches papers with year + citation count
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
# Notion helpers
# ---------------------------------------------------------------------------

def _query_deepdive(body: dict) -> list[dict]:
    results = []
    cursor = None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{DEEPDIVE_DB_ID}/query",
            json=body, headers=NOTION_HEADERS, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def query_done_this_week(start: str, end: str) -> list[dict]:
    """Papers marked Done with Date in [start, end]."""
    return _query_deepdive({
        "filter": {
            "and": [
                {"property": "Date", "date": {"on_or_after": start}},
                {"property": "Date", "date": {"on_or_before": end}},
                {"property": "Text", "status": {"equals": "Done"}},
            ]
        },
        "page_size": 100,
    })


def query_not_started() -> list[dict]:
    """All papers in the unread backlog.

    Catches two cases:
    - Text explicitly set to "Not started"
    - Text not set at all (auto-synced pages from sync_notion_notes.py never set it)
    Excludes papers already marked Done or In progress.
    """
    return _query_deepdive({
        "filter": {
            "and": [
                {"property": "Text", "status": {"does_not_equal": "Done"}},
                {"property": "Text", "status": {"does_not_equal": "In progress"}},
            ]
        },
        "sorts": [{"property": "Date", "direction": "descending"}],
        "page_size": 100,
    })


def _title_of(page: dict) -> str:
    props = page.get("properties", {}).get("Name", {}).get("title", [])
    return "".join(t.get("plain_text", "") for t in props) or "(untitled)"


def _tags_of(page: dict) -> list[str]:
    ms = page.get("properties", {}).get("Multi-select", {}).get("multi_select", [])
    return [t["name"] for t in ms]


def _score_of(page: dict) -> str:
    sel = page.get("properties", {}).get("Score of interest", {}).get("select")
    return sel["name"] if sel else "unscored"


def _date_of(page: dict) -> str:
    d = page.get("properties", {}).get("Date", {}).get("date")
    return d["start"] if d else ""


def fetch_page_notes(page_id: str) -> str:
    """Pull any text the user wrote in the page body (their own notes)."""
    try:
        r = requests.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=NOTION_HEADERS, timeout=30,
        )
        r.raise_for_status()
        parts = []
        for b in r.json().get("results", []):
            btype = b.get("type", "")
            rich = b.get(btype, {}).get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rich)
            if text.strip():
                parts.append(text.strip())
        return "\n".join(parts)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Semantic Scholar enrichment (best-effort, never fatal)
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


def enrich_papers(papers: list[dict], limit: int = 15) -> None:
    """Adds year/citations/abstract in-place for the top `limit` papers."""
    def _score_num(p):
        try:
            return int(p["score"])
        except Exception:
            return 0
    for p in sorted(papers, key=_score_num, reverse=True)[:limit]:
        s2 = s2_lookup(p["title"])
        if s2:
            p["year"] = s2.get("year")
            p["citations"] = s2.get("citationCount")
            p["abstract"] = (s2.get("abstract") or "")[:500]


# ---------------------------------------------------------------------------
# Build paper text for prompts
# ---------------------------------------------------------------------------

def _paper_block(p: dict) -> str:
    line = f"### {p['title']}"
    meta = []
    if p.get("year"):
        meta.append(f"{p['year']}")
    if p.get("citations") is not None:
        meta.append(f"{p['citations']} citations")
    if meta:
        line += f" ({', '.join(meta)})"
    line += f"\nTags: {', '.join(p['tags']) or 'none'} | Score of interest: {p['score']}"
    if p.get("date"):
        line += f" | Added: {p['date']}"
    if p.get("abstract"):
        line += f"\nAbstract: {p['abstract']}"
    if p.get("notes"):
        line += f"\nYour notes: {p['notes']}"
    return line


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def call_llm(system: str, user: str, cfg: dict, max_tokens: int = 7000) -> str:
    from src.processing.script_llm import _chat_complete, _client_from_config
    llm_cfg = cfg.get("llm", {})
    client = _client_from_config(llm_cfg)
    return _chat_complete(
        client,
        model=llm_cfg.get("model", "nvidia/nemotron-3-super-120b-a12b:free"),
        system=system,
        user=user,
        temperature=0.4,
        max_tokens=max_tokens,
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
    start = (datetime.fromisoformat(end) - timedelta(days=6)).date().isoformat()

    print(f"[weekly] Running for {start} → {end}", flush=True)

    # --- Part A: papers you read this week (Done) ---
    done_pages = query_done_this_week(start, end)
    done_papers = []
    for p in done_pages:
        done_papers.append({
            "title": _title_of(p),
            "tags": _tags_of(p),
            "score": _score_of(p),
            "date": _date_of(p),
            "notes": fetch_page_notes(p["id"]),
        })

    # --- Part B: unread backlog (Not started) ---
    unread_pages = query_not_started()
    unread_papers = []
    for p in unread_pages:
        unread_papers.append({
            "title": _title_of(p),
            "tags": _tags_of(p),
            "score": _score_of(p),
            "date": _date_of(p),
            "notes": "",
        })

    print(
        f"[weekly] {len(done_papers)} read this week, {len(unread_papers)} unread in backlog",
        flush=True,
    )

    if not done_papers and not unread_papers:
        print("[weekly] Deep Dive Notes is empty — nothing to do.", flush=True)
        _slack(f":pause_button: Deep Dive Notes is empty for {start}→{end} — weekly briefing skipped.")
        return

    # S2 enrichment
    if done_papers:
        enrich_papers(done_papers, limit=15)
    if unread_papers:
        enrich_papers(unread_papers, limit=20)

    # --- Build prompt ---
    system = (
        "You are writing a weekly deep-research briefing for a computational "
        "protein/antibody designer. You have two inputs: papers they finished "
        "reading this week (Part A) and their unread backlog (Part B). "
        "Your job is to (A) synthesise what they learned and (B) tell them "
        "exactly what to read next week."
    )

    done_text = "\n\n".join(_paper_block(p) for p in done_papers) if done_papers else "(none this week)"
    unread_text = "\n\n".join(_paper_block(p) for p in unread_papers) if unread_papers else "(backlog is empty)"

    user_prompt = f"""PART A — Papers read this week ({start} to {end}):

{done_text}

---

PART B — Unread backlog (all papers still Not started in Deep Dive Notes):

{unread_text}

---

Write the weekly briefing with this exact structure.

Content must start with:
# Weekly deep research briefing — {start} to {end}

"""

    if done_papers:
        user_prompt += f"""> Papers read: [all PART A titles, with year/citations where known]

---

# 1. Key insights
5-8 insights from this week's reading. For each:
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

---

"""
    else:
        user_prompt += "> No papers were read this week — analysis sections skipped.\n\n---\n\n"

    if unread_papers:
        user_prompt += f"""# 7. Read next week
From the unread backlog (PART B), pick exactly 5-7 papers to read next week.
Rank them by: scientific importance, actionability for protein/antibody design,
and coverage of different subfields (don't recommend 5 papers from the same area).
Use Score of interest as a signal but override it if a lower-scored paper fills
an important gap.

For each recommended paper:
## [Paper title]
**Why this one:** 2 sentences — what specific insight you will gain that you
can't get from the title alone. Be concrete (method, result, implication).
**Read for:** the specific section/figure/experiment that matters most.
**Time:** realistic estimate (e.g. "20 min skim abstract+conclusion",
"1.5h full methods read").
**Subfield:** one of: antibody design / inverse folding / structure prediction /
topology engineering / binder design / benchmarking & data / other

After the 5-7 picks, add:
> **Not recommended this week:** list the titles you deprioritised and one-line why
(too incremental, overlaps with a recommended paper, needs prerequisite reading first, etc.)
"""
    else:
        user_prompt += "# 7. Read next week\n(Backlog is empty — add papers to Deep Dive Notes to get reading recommendations.)\n"

    print("[weekly] Calling LLM...", flush=True)
    briefing = call_llm(system, user_prompt, cfg)

    page_title = f"Deep Research Briefing {start} to {end}"
    print("[weekly] Saving to Notion...", flush=True)
    url = save_to_notion(page_title, end, briefing)

    parts = []
    if done_papers:
        parts.append(f"{len(done_papers)} read")
    if unread_papers:
        parts.append(f"{len(unread_papers)} in backlog")
    summary = ", ".join(parts)

    if url:
        print(f"[weekly] Saved: {url}", flush=True)
        _slack(f":scroll: *Weekly briefing ready* ({summary}, {start}→{end}): {url}")
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

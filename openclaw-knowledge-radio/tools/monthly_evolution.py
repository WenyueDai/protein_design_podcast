#!/usr/bin/env python3
"""
tools/monthly_evolution.py

Reads every weekly briefing published this month, extracts each Insight,
computes a fitness score, cross-breeds the fittest insights across
populations (subfields), and writes a Monthly Evolution Report back to
the Weekly Summary database. Meant to run on the last day of the month
(see .github/workflows/daily_podcast.yml).

Fitness (0-1 scale, computed fresh each run — not persisted as its own DB):
  0.25 x citation_frequency   (how many later weeks reference it)
  0.20 x freshness            (4-week half-life decay)
  0.20 x connectivity         (how many other insights this month relate to it)
  0.15 x richness             (length/density of the insight's text)
  0.20 x human_score          (Score of interest of the source paper, /10)

Env vars: same as tools/weekly_summary.py.
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
import re
import json
import requests
import yaml
from datetime import datetime
from calendar import monthrange

sys.path.insert(0, str(_Path(__file__).parent.parent))
from src.utils.timeutils import load_tz, now_local_date

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
DEEPDIVE_DB_ID = os.environ.get("NOTION_DEEPDIVE_DB_ID", "3165f58ea8c280498f72c770028aec0d").replace("-", "")
WEEKLY_DB_ID = os.environ.get("NOTION_WEEKLY_SUMMARY_DB_ID", "").replace("-", "")
MONTHLY_DB_ID = os.environ.get("NOTION_MONTHLY_SUMMARY_DB_ID", WEEKLY_DB_ID).replace("-", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

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
# Notion: pull this month's weekly briefings, full text
# ---------------------------------------------------------------------------

def query_weekly_pages(start: str, end: str) -> list[dict]:
    body = {
        "filter": {
            "and": [
                {"property": "date", "date": {"on_or_after": start}},
                {"property": "date", "date": {"on_or_before": end}},
            ]
        },
        "page_size": 100,
    }
    r = requests.post(
        f"https://api.notion.com/v1/databases/{WEEKLY_DB_ID}/query",
        json=body, headers=NOTION_HEADERS, timeout=30,
    )
    r.raise_for_status()
    return r.json().get("results", [])


def _title_of(page: dict) -> str:
    title_prop = page.get("properties", {}).get("Name", {}).get("title", [])
    return "".join(t.get("plain_text", "") for t in title_prop) or "(untitled)"


def fetch_page_text(page_id: str) -> str:
    """Reconstruct plain text from a page's blocks (headings + paragraphs + quotes)."""
    parts = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = requests.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=NOTION_HEADERS, params=params, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for b in data.get("results", []):
            btype = b.get("type", "")
            content = b.get(btype, {})
            rich = content.get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rich)
            if btype == "heading_1":
                parts.append(f"# {text}")
            elif btype == "heading_2":
                parts.append(f"## {text}")
            elif btype == "quote":
                parts.append(f"> {text}")
            elif btype == "divider":
                parts.append("---")
            elif text.strip():
                parts.append(text)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return "\n".join(parts)


def fetch_deepdive_index() -> list[dict]:
    """Full title -> Score of interest / tags index, for human-score lookup."""
    out = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{DEEPDIVE_DB_ID}/query",
            json=body, headers=NOTION_HEADERS, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            title_prop = props.get("Name", {}).get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_prop)
            score_sel = props.get("Score of interest", {}).get("select")
            score = score_sel["name"] if score_sel else None
            tags = [t["name"] for t in props.get("Multi-select", {}).get("multi_select", [])]
            if title:
                out.append({"title": title, "score": score, "tags": tags})
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


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
# Markdown -> Notion blocks (shared convention with weekly_summary.py)
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
            blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": rich(line[2:])}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich(line[3:])}})
        elif line.startswith("> "):
            blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": rich(line[2:])}})
        elif line.startswith("---"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        else:
            for start in range(0, len(line), CHUNK):
                blocks.append({"object": "block", "type": "paragraph",
                                "paragraph": {"rich_text": rich(line[start:start + CHUNK])}})
    return blocks


def save_to_notion(title: str, date_end: str, md: str) -> str | None:
    if not MONTHLY_DB_ID:
        print("[monthly] NOTION_MONTHLY_SUMMARY_DB_ID not set — skipping Notion save", flush=True)
        return None
    blocks = markdown_to_blocks(md)
    first_batch, rest = blocks[:100], blocks[100:]
    body = {
        "parent": {"database_id": MONTHLY_DB_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": title[:2000]}}]},
            "date": {"date": {"start": date_end}},
        },
        "children": first_batch,
    }
    r = requests.post("https://api.notion.com/v1/pages", json=body, headers=NOTION_HEADERS, timeout=30)
    if not r.ok:
        print(f"[monthly] Notion error {r.status_code}: {r.text}", flush=True)
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
    end_dt = datetime.fromisoformat(end)
    start = end_dt.replace(day=1).date().isoformat()

    last_day = monthrange(end_dt.year, end_dt.month)[1]
    if end_dt.day != last_day:
        print(f"[monthly] {end} is not the last day of the month ({last_day}) — running anyway "
              f"(caller is responsible for date-gating; this script itself doesn't block).", flush=True)

    print(f"[monthly] Running evolution engine for {start} → {end}", flush=True)

    weekly_pages = query_weekly_pages(start, end)
    # Skip any prior Monthly Evolution Report pages that might already be in this DB/range
    weekly_pages = [p for p in weekly_pages if not _title_of(p).startswith("Monthly Evolution Report")]

    if not weekly_pages:
        print("[monthly] No weekly briefings found this month — skipping.", flush=True)
        _slack(f":pause_button: No weekly briefings found for {start}→{end} — monthly evolution skipped.")
        return

    weekly_texts = []
    for p in weekly_pages:
        title = _title_of(p)
        text = fetch_page_text(p["id"])
        weekly_texts.append(f"=== {title} ===\n{text}")
    corpus = "\n\n".join(weekly_texts)

    deepdive_index = fetch_deepdive_index()
    # Keep this compact — title + score + tags only, not full notes
    index_text = "\n".join(
        f"{d['title']} | score={d['score'] or 'unscored'} | tags={','.join(d['tags'])}"
        for d in deepdive_index
    )

    system = (
        "You are running the monthly evolution engine for a computational "
        "protein/antibody designer's literature system. Insights compete for "
        "fitness, get cross-bred into hybrids, and go dormant/archived if "
        "nobody reads or cites them."
    )
    user = f"""This month's weekly briefings ({start} to {end}):

{corpus}

---

Deep Dive Notes index (paper title | human Score of interest 2-10 | tags), for looking up human scores by fuzzy title match:

{index_text}

---

TASK:

STEP 1 — From each weekly briefing's "1. Key insights" section, extract every Insight: title, one-line summary, and classify it into ONE population from this fixed taxonomy: antibody design, inverse folding, structure prediction, topology engineering, binder design, benchmarking & data, other.

STEP 2 — Scan across ALL the weekly texts (not just Section 1) for "(→ Insight N)" back-references and any prose that clearly re-invokes an earlier week's insight by topic, even without the exact tag. Count how many distinct later weeks reference each insight — this is its citation count.

STEP 3 — For each insight, look up its source paper in the Deep Dive Notes index above by fuzzy title match and use its Score of interest as the human score. If no match, use 5 (neutral) and mark it "unscored".

STEP 4 — Compute fitness for each insight on a 0-1 scale:
fitness = 0.25 x citation_frequency (normalized against this month's max)
        + 0.20 x freshness (exponential decay, 4-week half-life from the insight's birth week to {end})
        + 0.20 x connectivity (how many OTHER insights this month relate to it, normalized)
        + 0.15 x richness (length/density of the insight's summary, normalized)
        + 0.20 x human_score (Score of interest / 10)

Status: fitness >= 0.6 = active, 0.3-0.6 = dormant, <0.3 = archived. Flag any insight that dropped straight from active to archived (skipped dormant) as "worth re-examining" — that usually means a thread got dropped, not that it naturally faded.

STEP 5 — From insights with fitness >= 0.6, pick 1-3 pairs from DIFFERENT populations and write a new hybrid insight for each pair — a genuinely new synthesis neither parent stated alone. Label with both parent titles.

STEP 6 — Write the report with this exact structure:

# Monthly evolution report — {start} to {end}

# 1. Population trends
Which subfields grew/shrank in insight count and average fitness this month. Flowing prose.

# 2. Top 10 fittest insights
Ranked list: title, population, fitness score (2 decimals), one-line why.

# 3. Cross-bred insights this month
For each hybrid: ## [New insight title] — bred from [Parent A] x [Parent B] / the synthesis text / why neither parent alone said this.

# 4. Dormant & archived this month
Bulleted, one line each, with fitness score. Flag skipped-dormant cases.

# 5. Thought archaeology
Insights archived 2+ months ago (if determinable from the text) that were high-fitness when born — worth revisiting.

# 6. Unscored insights
List any insight where no Deep Dive Notes match was found — these need a rating next review.

Output ONLY the report in the structure above, starting with "# Monthly evolution report".
"""

    print("[monthly] Calling LLM...", flush=True)
    report = call_llm(system, user, cfg)

    page_title = f"Monthly Evolution Report {start} to {end}"
    print("[monthly] Saving to Notion...", flush=True)
    url = save_to_notion(page_title, end, report)

    if url:
        print(f"[monthly] Saved: {url}", flush=True)
        _slack(f":dna: Monthly evolution report ready ({len(weekly_pages)} weekly briefings processed): {url}")
    else:
        print("[monthly] Warning: report was written but not saved to Notion.", flush=True)
        _slack(":warning: Monthly evolution report generated but Notion save failed — check Action logs.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"[monthly] FAILED: {e}", flush=True)
        traceback.print_exc()
        _slack(f":x: Monthly evolution run failed: {e}")
        sys.exit(1)

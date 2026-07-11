#!/usr/bin/env python3
"""
tools/speculative_ideas.py

Every Saturday: reads all papers from the past week's daily podcast episodes,
then generates 10 speculative biology × protein design ideas — creative
extrapolations of what becomes possible as protein design matures toward its
theoretical limits.

"Speculative biology" here means: what organisms, ecosystems, biochemistries,
or evolutionary paths could we engineer if we could design any protein at will?

Saves one page per week to the Speculative Ideas Notion database.

Env vars:
  OPENROUTER_API_KEY          — same key as rest of pipeline
  NOTION_API_KEY              — same Notion integration token
  NOTION_SPECULATIVE_DB_ID    — ID of the Speculative Ideas database
  SLACK_WEBHOOK_URL           — optional Slack notification
  RUN_DATE                    — override today (YYYY-MM-DD)
"""
from __future__ import annotations

from pathlib import Path as _Path
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

import json
import os
import sys
import requests
import yaml
from datetime import datetime, timedelta

sys.path.insert(0, str(_Path(__file__).parent.parent))
from src.utils.timeutils import load_tz, now_local_date

NOTION_API_KEY  = os.environ.get("NOTION_API_KEY", "")
SPECULATIVE_DB_ID = os.environ.get("NOTION_SPECULATIVE_DB_ID", "").replace("-", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

CONFIG_PATH = _Path(__file__).parent.parent / "config.yaml"
OUTPUT_DIR  = _Path(__file__).parent.parent / "output"


def _cfg() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _slack(msg: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": msg}, timeout=15)
    except Exception as e:
        print(f"[speculative] slack failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Load papers from past week's episode_items.json files
# ---------------------------------------------------------------------------

def load_week_papers(start: str, end: str) -> list[dict]:
    """Collect all papers from daily podcast outputs in [start, end]."""
    papers: list[dict] = []
    start_dt = datetime.fromisoformat(start).date()
    end_dt   = datetime.fromisoformat(end).date()

    for d in OUTPUT_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            day = datetime.fromisoformat(d.name).date()
        except ValueError:
            continue
        if not (start_dt <= day <= end_dt):
            continue
        items_file = d / "episode_items.json"
        if not items_file.exists():
            continue
        try:
            raw = json.loads(items_file.read_text(encoding="utf-8"))
            items = raw.get("items", raw) if isinstance(raw, dict) else raw
            for item in items:
                papers.append({
                    "date": d.name,
                    "title": item.get("title", ""),
                    "one_liner": item.get("one_liner", ""),
                    "tags": item.get("tags", []),
                    "highlighted": item.get("highlighted", False),
                    "source": item.get("source", ""),
                })
        except Exception as e:
            print(f"[speculative] failed to read {items_file}: {e}", flush=True)

    return papers


def _paper_line(p: dict) -> str:
    tags = ", ".join(p["tags"]) if p["tags"] else "untagged"
    flag = " ★" if p["highlighted"] else ""
    return f"- [{p['date']}]{flag} {p['title']} ({tags})\n  {p['one_liner']}"


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def call_llm(system: str, user: str, cfg: dict, max_tokens: int = 6000) -> str:
    from src.processing.script_llm import _chat_complete, _client_from_config
    llm_cfg = cfg.get("llm", {})
    client = _client_from_config(llm_cfg)
    return _chat_complete(
        client,
        model=llm_cfg.get("model", "nvidia/nemotron-3-super-120b-a12b:free"),
        system=system,
        user=user,
        temperature=0.7,
        max_tokens=max_tokens,
        fallback_models=llm_cfg.get("model_fallbacks", []),
    )


# ---------------------------------------------------------------------------
# Markdown -> Notion blocks
# ---------------------------------------------------------------------------

def markdown_to_blocks(md: str) -> list[dict]:
    CHUNK = 1900
    blocks = []

    def rich(text):
        return [{"type": "text", "text": {"content": text[:CHUNK]}}]

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


def save_to_notion(title: str, date_str: str, md: str) -> str | None:
    if not SPECULATIVE_DB_ID:
        print("[speculative] NOTION_SPECULATIVE_DB_ID not set — skipping Notion save", flush=True)
        return None
    blocks = markdown_to_blocks(md)
    first_batch, rest = blocks[:100], blocks[100:]
    body = {
        "parent": {"database_id": SPECULATIVE_DB_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": title[:2000]}}]},
            "date": {"date": {"start": date_str}},
        },
        "children": first_batch,
    }
    r = requests.post("https://api.notion.com/v1/pages", json=body, headers=NOTION_HEADERS, timeout=30)
    if not r.ok:
        print(f"[speculative] Notion error {r.status_code}: {r.text}", flush=True)
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
    tz  = load_tz(cfg.get("timezone", "Europe/London"))
    end   = os.environ.get("RUN_DATE") or now_local_date(tz)
    start = (datetime.fromisoformat(end) - timedelta(days=6)).date().isoformat()

    print(f"[speculative] Running for {start} → {end}", flush=True)

    papers = load_week_papers(start, end)
    if not papers:
        print("[speculative] No papers found for this week — skipping.", flush=True)
        _slack(f":pause_button: No papers found for {start}→{end} — speculative ideation skipped.")
        return

    # Prioritise highlighted papers; include all, cap at 60 to stay within prompt budget
    highlighted = [p for p in papers if p["highlighted"]]
    others      = [p for p in papers if not p["highlighted"]]
    pool        = (highlighted + others)[:60]

    print(f"[speculative] {len(papers)} total papers ({len(highlighted)} highlighted), using {len(pool)}", flush=True)

    paper_list = "\n".join(_paper_line(p) for p in pool)

    system = (
        "You are a speculative biologist and protein designer. You think at the "
        "intersection of what protein design can do today and what it could enable "
        "at its theoretical limits. Your ideas are grounded in real science but "
        "deliberately push beyond current practice into creative, provocative "
        "territory — the kind of thinking that opens new research directions."
    )

    user_prompt = f"""This week's papers from the protein design / structural biology literature ({start} to {end}).
★ = featured in the daily podcast (highest-ranked). All others are from the broader feed.

{paper_list}

---

TASK: Generate exactly 10 speculative biology × protein design ideas inspired by this week's papers.

"Speculative biology" means: what new organisms, biochemistries, evolutionary paths, synthetic
ecosystems, or forms of life could we engineer if protein design reached its theoretical limits?
Think big. These ideas should make a biologist say "that's insane but... actually maybe?"

For each idea, use this exact format:

## Idea N: [Evocative, specific title — not generic like "Design better enzymes"]

**Inspired by:** [1-3 paper titles from the list above that sparked this]

**The speculative question:**
One sentence starting with "What if we could..." or "What would happen if..." —
the core creative leap.

**Grounding in this week's science:**
2-3 sentences connecting the idea to what the papers actually showed or demonstrated.
Be specific about the mechanism or result you're extrapolating from.

**The speculative biology leap:**
2-3 sentences: where does this go when protein design is mature?
What organism, ecosystem, or biological phenomenon becomes possible?

**First real experiment:**
The smallest, doable experiment that tests whether this direction is viable.
Should be achievable with current tools (AlphaFold, RFdiffusion, yeast display, etc.).

**Weirdness level:** [1-5 stars] — 1 = near-term plausible, 5 = genuinely alien

---

Make the 10 ideas span a range of weirdness (some 1-2 stars, some 4-5 stars) and cover different
subfields from the week (don't generate 10 ideas all about antibodies if the week had diverse papers).
End with a one-paragraph "meta-observation" — what does this week's set of papers collectively suggest
about where speculative biology will be most productive in the next decade?

Output ONLY the 10 ideas + meta-observation, starting with "## Idea 1:".
"""

    print("[speculative] Calling LLM...", flush=True)
    ideas = call_llm(system, user_prompt, cfg)

    header = f"# Speculative Ideas — {start} to {end}\n\n"
    header += f"> {len(papers)} papers this week ({len(highlighted)} featured). "
    header += f"Ideas generated {end}.\n\n---\n\n"
    full_md = header + ideas

    page_title = f"Speculative Ideas {start} to {end}"
    print("[speculative] Saving to Notion...", flush=True)
    url = save_to_notion(page_title, end, full_md)

    if url:
        print(f"[speculative] Saved: {url}", flush=True)
        _slack(
            f":dna: *10 speculative ideas ready* ({len(papers)} papers, {start}→{end}): {url}"
        )
    else:
        print("[speculative] Warning: ideas generated but Notion save skipped.", flush=True)
        _slack(":warning: Speculative ideas generated but Notion save failed — check Action logs.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"[speculative] FAILED: {e}", flush=True)
        traceback.print_exc()
        _slack(f":x: Speculative ideation failed: {e}")
        sys.exit(1)

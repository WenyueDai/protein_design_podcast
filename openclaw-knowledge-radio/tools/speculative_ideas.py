#!/usr/bin/env python3
"""
tools/speculative_ideas.py

Every Saturday: reads the on-topic papers from the past week's daily podcast episodes,
then generates up to 10 speculative biology × protein design ideas — creative
extrapolations of what becomes possible as protein design matures toward its
theoretical limits.

Quality controls (added after several weeks of weak output):
  * the paper pool is limited to papers that pass the relevance gate, best first;
  * papers are referenced by ID (P01, P02, ...) and every cited ID is checked against the pool,
    so an idea cannot be "inspired by" a paper that was never in the list;
  * the model may return fewer than 10 ideas rather than force a weak connection;
  * output is validated (structure, citations, weirdness spread) and regenerated up to 2 times
    with feedback; ideas that still fail are dropped, and the week is skipped if <3 survive;
  * **bold** is converted to real Notion bold instead of showing literal asterisks;
  * a page with the same title is never created twice (re-runs used to duplicate it);
  * the page records which model(s) wrote it.

"Speculative biology" here means: what organisms, ecosystems, biochemistries,
or evolutionary paths could we engineer if we could design any protein at will?

Saves one page per week to the Speculative Ideas Notion database.

Env vars:
  OPENROUTER_API_KEY          — same key as rest of pipeline
  NOTION_API_KEY              — same Notion integration token
  NOTION_SPECULATIVE_DB_ID    — ID of the Speculative Ideas database
  SLACK_WEBHOOK_URL           — optional Slack notification
  RUN_DATE                    — override today (YYYY-MM-DD)
  SPECULATIVE_FORCE           — set to 1 to create the page even if one with the same title exists
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
import re
import sys
import requests
import yaml
from datetime import datetime, timedelta

sys.path.insert(0, str(_Path(__file__).parent.parent))
from src.utils.timeutils import load_tz, now_local_date
from src.processing import relevance as _rel

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

def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def load_week_papers(start: str, end: str) -> list[dict]:
    """Collect all papers from daily podcast outputs in [start, end] (de-duplicated by title)."""
    papers: list[dict] = []
    seen_titles: set[str] = set()
    start_dt = datetime.fromisoformat(start).date()
    end_dt   = datetime.fromisoformat(end).date()

    for d in sorted(OUTPUT_DIR.iterdir()):
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
                key = _norm_title(item.get("title", ""))
                if not key or key in seen_titles:
                    continue
                seen_titles.add(key)
                papers.append({
                    "date": d.name,
                    "title": item.get("title", ""),
                    "one_liner": item.get("one_liner", ""),
                    "tags": item.get("tags", []),
                    "highlighted": item.get("highlighted", False),
                    "source": item.get("source", ""),
                    "url": item.get("url", ""),
                })
        except Exception as e:
            print(f"[speculative] failed to read {items_file}: {e}", flush=True)

    return papers


def select_pool(papers: list[dict], cfg: dict, cap: int = 40, min_pool: int = 5) -> list[dict]:
    """
    Keep only on-topic papers, best first, and give each an ID (P01, P02, ...).

    Threshold: featured_min_score; if that leaves fewer than `min_pool` papers the plain
    min_score is used instead.  Returns [] when even that is not enough material.
    """
    th = _rel.thresholds(cfg)
    scored = []
    for p in papers:
        sc = _rel.score_item({"title": p["title"], "one_liner": p.get("one_liner", "")}, cfg)["score"]
        scored.append((sc, p))
    for floor in (th["featured_min_score"], th["min_score"]):
        keep = [(sc, p) for sc, p in scored if sc >= floor]
        if len(keep) >= min_pool:
            break
    else:
        return []
    keep.sort(key=lambda x: (-int(bool(x[1].get("highlighted"))), -x[0]))
    pool = []
    for i, (sc, p) in enumerate(keep[:cap], 1):
        q = dict(p)
        q["id"] = f"P{i:02d}"
        q["relevance"] = sc
        pool.append(q)
    return pool


def _paper_line(p: dict) -> str:
    tags = ", ".join(p["tags"]) if p["tags"] else "untagged"
    flag = " ★" if p["highlighted"] else ""
    return f"[{p['id']}]{flag} ({p['date']}) {p['title']} ({tags})\n  Summary: {p['one_liner']}"


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
        temperature=0.5,   # was 0.7: the higher setting produced more invented "findings"
        max_tokens=max_tokens,
        fallback_models=llm_cfg.get("model_fallbacks", []),
    )


SYSTEM_PROMPT = (
    "You are a speculative biologist and protein designer. You think at the "
    "intersection of what protein design can do today and what it could enable "
    "at its theoretical limits. Your ideas are grounded in real science but "
    "deliberately push beyond current practice into creative territory. "
    "You never invent results: every factual statement about a paper must be "
    "supported by the summary you were given for that paper."
)


def build_user_prompt(pool: list[dict], start: str, end: str, feedback: str = "") -> str:
    paper_list = "\n".join(_paper_line(p) for p in pool)
    fb = (
        "\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED FOR THESE REASONS — fix all of them:\n" + feedback + "\n"
        if feedback else ""
    )
    return f"""This week's on-topic papers from the protein design / structural biology literature ({start} to {end}).
★ = featured in the daily podcast. Each paper has an ID in square brackets.

{paper_list}

---

TASK: Write speculative biology × protein design ideas inspired by this week's papers.

"Speculative biology" means: what new organisms, biochemistries, evolutionary paths, synthetic
ecosystems, or forms of life could we engineer if protein design reached its theoretical limits?

RULES (they are checked automatically; ideas that break them are discarded):
1. Write between 4 and 10 ideas. Quality beats quantity: only write an idea when at least one paper
   above genuinely motivates it. Do NOT force a connection to reach 10.
2. Cite papers by ID only (e.g. P03, P11), 1-3 per idea, and only IDs that appear in the list above.
   Never write a paper title yourself.
3. "What the papers showed" may only state things that are in the Summary lines of the cited papers.
   Do not add numbers, model names, datasets, organisms or results that are not in those summaries.
   If a summary is thin, say less.
4. Spread the weirdness: if you write 6 or more ideas, at least two must be rated 1-2 (near-term
   plausible) and at least two must be rated 4-5 (genuinely alien). Rate honestly.
5. Cover different sub-fields from the list; do not write several ideas about the same paper.

For each idea use EXACTLY this format:

## Idea N: Evocative, specific title (not generic like "Design better enzymes")

**Inspired by:** P03, P11

**The speculative question:** One sentence starting with "What if we could..." or "What would happen if..."

**What the papers showed:** 1-2 sentences restating only what the cited summaries say.

**The leap:** 2-3 sentences: where this goes when protein design is mature.

**First real experiment:** The smallest experiment that tests it with today's tools.

**Weirdness:** N/5

---

After the ideas, write one paragraph headed "## Meta-observation": what does this week's set of papers
collectively suggest about where speculative biology will be most productive in the next decade?

Output ONLY the ideas and the meta-observation, starting with "## Idea 1:".{fb}"""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_IDEA_SPLIT = re.compile(r"(?m)^##\s+Idea\s+(\d+)\s*[:.\-–—]\s*(.*)$")
_ID_RE = re.compile(r"\bP(\d{2,3})\b")


def split_ideas(md: str) -> tuple[list[dict], str]:
    """Return ([{n, title, body}], meta_observation_text)."""
    matches = list(_IDEA_SPLIT.finditer(md))
    ideas = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        body = md[m.end():end]
        ideas.append({"n": int(m.group(1)), "title": m.group(2).strip(), "body": body})
    meta = ""
    mm = re.search(r"(?im)^##\s+meta[- ]observation\s*:?\s*$", md)
    if mm:
        meta = md[mm.end():].strip()
        if ideas:
            # the last idea's body swallowed the meta section: cut it off there
            for idea in ideas:
                cut = re.search(r"(?im)^##\s+meta[- ]observation", idea["body"])
                if cut:
                    idea["body"] = idea["body"][:cut.start()]
    return ideas, meta


def _weirdness(body: str) -> int | None:
    m = re.search(r"(?i)weirdness[^\n]*?(\d)\s*/\s*5", body) or re.search(r"(?i)weirdness[^\n]*?([1-5])\b", body)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)weirdness[^\n]*", body)
    if m:
        stars = m.group(0).count("★")
        if 1 <= stars <= 5:
            return stars
    return None


def validate_idea(idea: dict, pool_ids: set[str]) -> list[str]:
    problems = []
    body = idea["body"]
    m = re.search(r"(?im)^\*\*Inspired by:?\*\*:?\s*(.+)$", body)
    if not m:
        problems.append(f"Idea {idea['n']}: missing '**Inspired by:**' line")
    else:
        ids = [f"P{x}" for x in _ID_RE.findall(m.group(1))]
        bad = [i for i in ids if i not in pool_ids]
        if not ids:
            problems.append(f"Idea {idea['n']}: 'Inspired by' cites no paper IDs (write IDs like P03, never titles)")
        if bad:
            problems.append(f"Idea {idea['n']}: cites unknown ID(s) {', '.join(bad)}")
        if len(set(ids)) > 3:
            problems.append(f"Idea {idea['n']}: cites more than 3 papers")
    for label in ("speculative question", "what the papers showed", "the leap", "first real experiment"):
        if label not in body.lower():
            problems.append(f"Idea {idea['n']}: missing section '{label}'")
    if _weirdness(body) is None:
        problems.append(f"Idea {idea['n']}: missing 'Weirdness: N/5' rating")
    return problems


def validate(md: str, pool: list[dict]) -> tuple[list[dict], list[str], str]:
    """
    Returns (valid_ideas, problems, meta).  `problems` covers both per-idea issues and
    whole-answer issues (count, weirdness spread, missing meta-observation).
    """
    pool_ids = {p["id"] for p in pool}
    ideas, meta = split_ideas(md)
    problems: list[str] = []
    valid: list[dict] = []
    for idea in ideas:
        pr = validate_idea(idea, pool_ids)
        problems.extend(pr)
        if not pr:
            valid.append(idea)
    if len(ideas) > 10:
        problems.append(f"{len(ideas)} ideas written; the maximum is 10")
    if len(valid) < 4:
        problems.append(f"only {len(valid)} valid idea(s); at least 4 are required")
    ws = [w for w in (_weirdness(i["body"]) for i in valid) if w]
    if len(valid) >= 6 and ws:
        if sum(1 for w in ws if w <= 2) < 2:
            problems.append("fewer than two ideas rated 1-2 weirdness")
        if sum(1 for w in ws if w >= 4) < 2:
            problems.append("fewer than two ideas rated 4-5 weirdness")
    if not meta:
        problems.append("missing '## Meta-observation' section")
    return valid[:10], problems, meta


def render_markdown(ideas: list[dict], meta: str, pool: list[dict]) -> str:
    """Replace paper IDs in the 'Inspired by' lines with the real titles (+ date) and renumber ideas."""
    by_id = {p["id"]: p for p in pool}
    out = []
    for n, idea in enumerate(ideas, 1):
        body = idea["body"].strip()

        def _sub(m: re.Match) -> str:
            ids = [f"P{x}" for x in _ID_RE.findall(m.group(2))]
            names = [f"“{by_id[i]['title'].rstrip('.')}” ({by_id[i]['date']})" for i in ids if i in by_id]
            return f"{m.group(1)} " + "; ".join(names)

        body = re.sub(r"(?im)^(\*\*Inspired by:?\*\*:?)\s*(.+)$", _sub, body)
        body = re.sub(r"(?i)(\*\*Weirdness:?\*\*:?)\s*([1-5])\s*/\s*5",
                      lambda m: f"{m.group(1)} {'★' * int(m.group(2))}{'☆' * (5 - int(m.group(2)))} ({m.group(2)}/5)", body)
        body = re.sub(r"(?m)^---\s*$", "", body).strip()
        out.append(f"## Idea {n}: {idea['title']}\n\n{body}\n\n---\n")
    out.append("## Meta-observation\n\n" + meta.strip() + "\n")
    return "\n".join(out)


def generate_ideas(pool: list[dict], start: str, end: str, cfg: dict, llm=call_llm,
                   max_retries: int = 2) -> tuple[str | None, dict]:
    """
    Generate, validate and (if needed) regenerate.  Returns (markdown or None, report).
    `llm` is injectable for tests.
    """
    report = {"attempts": 0, "problems_by_attempt": [], "n_valid": 0}
    best: tuple[list[dict], str] | None = None
    feedback = ""
    for attempt in range(1, max_retries + 2):
        report["attempts"] = attempt
        raw = llm(SYSTEM_PROMPT, build_user_prompt(pool, start, end, feedback), cfg)
        valid, problems, meta = validate(raw, pool)
        report["problems_by_attempt"].append(problems)
        print(f"[speculative] attempt {attempt}: {len(valid)} valid idea(s), {len(problems)} problem(s)", flush=True)
        for pr in problems[:8]:
            print(f"[speculative]   - {pr}", flush=True)
        if best is None or len(valid) > len(best[0]):
            best = (valid, meta)
        if not problems:
            break
        feedback = "\n".join(f"- {p}" for p in problems[:12])
    valid, meta = best if best else ([], "")
    report["n_valid"] = len(valid)
    if len(valid) < 3:
        return None, report
    if not meta:
        meta = "(The model did not produce a meta-observation this week.)"
    return render_markdown(valid, meta, pool), report


# ---------------------------------------------------------------------------
# Markdown -> Notion blocks
# ---------------------------------------------------------------------------

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _rich_text(text: str, chunk: int = 1900) -> list[dict]:
    """Convert **bold** spans into Notion rich_text annotations (no literal asterisks)."""
    out: list[dict] = []
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            out.append({"type": "text", "text": {"content": text[pos:m.start()]}})
        out.append({"type": "text", "text": {"content": m.group(1)}, "annotations": {"bold": True}})
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": {"content": text[pos:]}})
    # Notion limits each rich_text item to 2000 chars: split oversize pieces, keep annotations.
    final: list[dict] = []
    for r in out:
        c = r["text"]["content"]
        for i in range(0, max(len(c), 1), chunk):
            piece = {"type": "text", "text": {"content": c[i:i + chunk]}}
            if "annotations" in r:
                piece["annotations"] = r["annotations"]
            final.append(piece)
    return [r for r in final if r["text"]["content"]]


def markdown_to_blocks(md: str) -> list[dict]:
    blocks = []
    for raw_line in md.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue  # blank lines are separators, not empty paragraphs (keeps the page compact)
        if line.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": _rich_text(line[2:])}})
        elif line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rich_text(line[3:])}})
        elif line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": _rich_text(line[4:])}})
        elif line.startswith("> "):
            blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": _rich_text(line[2:])}})
        elif line.startswith("---"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif re.match(r"^[-*]\s+", line):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": _rich_text(re.sub(r"^[-*]\s+", "", line))}})
        else:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(line)}})
    return blocks


def page_exists(title: str) -> bool:
    """True if the Speculative Ideas DB already has a page with this exact title (re-run guard)."""
    if not SPECULATIVE_DB_ID or not NOTION_API_KEY:
        return False
    try:
        r = requests.post(
            f"https://api.notion.com/v1/databases/{SPECULATIVE_DB_ID}/query",
            json={"filter": {"property": "Name", "title": {"equals": title}}, "page_size": 1},
            headers=NOTION_HEADERS, timeout=30,
        )
        if not r.ok:
            print(f"[speculative] duplicate check failed ({r.status_code}) — proceeding", flush=True)
            return False
        return bool(r.json().get("results"))
    except Exception as e:
        print(f"[speculative] duplicate check error: {e} — proceeding", flush=True)
        return False


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

    page_title = f"Speculative Ideas {start} to {end}"
    if os.environ.get("SPECULATIVE_FORCE", "").strip() not in ("1", "true", "yes") and page_exists(page_title):
        print(f"[speculative] '{page_title}' already exists in Notion — not creating a duplicate.", flush=True)
        return

    papers = load_week_papers(start, end)
    if not papers:
        print("[speculative] No papers found for this week — skipping.", flush=True)
        _slack(f":pause_button: No papers found for {start}→{end} — speculative ideation skipped.")
        return

    pool = select_pool(papers, cfg)
    if not pool:
        print(f"[speculative] Only off-topic material this week ({len(papers)} papers) — skipping.", flush=True)
        _slack(f":pause_button: Speculative ideas skipped for {start}→{end}: fewer than 5 of {len(papers)} papers were on-topic.")
        return
    n_feat = sum(1 for p in pool if p["highlighted"])
    print(f"[speculative] {len(papers)} papers this week, {len(pool)} on-topic used ({n_feat} featured)", flush=True)

    from src.processing.script_llm import reset_used_models, get_used_models
    reset_used_models()
    print("[speculative] Calling LLM...", flush=True)
    ideas_md, report = generate_ideas(pool, start, end, cfg)
    models = get_used_models()

    if not ideas_md:
        print("[speculative] No acceptable ideas after validation — not publishing.", flush=True)
        _slack(
            f":warning: Speculative ideas for {start}→{end} not published: output failed validation "
            f"after {report['attempts']} attempt(s). Last problems: "
            + "; ".join(report["problems_by_attempt"][-1][:3])
        )
        return

    header = f"# Speculative Ideas — {start} to {end}\n\n"
    header += (
        f"> Based on {len(pool)} on-topic papers this week ({n_feat} featured) out of {len(papers)} collected. "
        f"{report['n_valid']} idea(s) passed validation after {report['attempts']} attempt(s). "
        f"Generated with: {', '.join(models) if models else 'unknown model'}.\n\n---\n\n"
    )
    full_md = header + ideas_md

    print("[speculative] Saving to Notion...", flush=True)
    url = save_to_notion(page_title, end, full_md)

    if url:
        print(f"[speculative] Saved: {url}", flush=True)
        _slack(f":dna: *{report['n_valid']} speculative ideas ready* ({len(pool)} on-topic papers, {start}→{end}): {url}")
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

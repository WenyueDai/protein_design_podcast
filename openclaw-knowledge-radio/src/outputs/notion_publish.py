"""
Notion integration — saves the daily digest as a Notion database page.

Setup:
  1. Go to https://www.notion.so/my-integrations → New integration (Internal)
  2. Copy the token → NOTION_TOKEN in .env
  3. Create a Notion database → Share → Connect to your integration
  4. Copy the database ID from the URL → NOTION_DATABASE_ID in .env

Required env vars:
  NOTION_TOKEN        — ntn_xxxx or secret_xxxx
  NOTION_DATABASE_ID  — 32-char hex ID (with or without hyphens)
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


_API = "https://api.notion.com/v1"
_VERSION = "2022-06-28"


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ.get('NOTION_TOKEN', '').strip()}",
        "Content-Type": "application/json",
        "Notion-Version": _VERSION,
    }


def _strip_html(s: str) -> str:
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(s, "html.parser").get_text(" ", strip=True)
    except ImportError:
        return re.sub(r'<[^>]+>', ' ', s).strip()


def _rich(text: str, url: str = "") -> Dict[str, Any]:
    obj: Dict[str, Any] = {"type": "text", "text": {"content": text[:2000]}}
    if url:
        obj["text"]["link"] = {"url": url}
    return obj


def _callout(text: str, emoji: str, color: str = "gray_background") -> Dict[str, Any]:
    return {"object": "block", "type": "callout",
            "callout": {"icon": {"type": "emoji", "emoji": emoji},
                        "rich_text": [_rich(text[:1900])], "color": color}}


def models_line(models: Optional[List[str]], what: str = "Generated with") -> str:
    """'Generated with: a, b' -- the models that ACTUALLY answered (fallbacks included)."""
    ms = [m for m in (models or []) if m]
    return f"{what}: " + (", ".join(ms) if ms else "no LLM call recorded")


def _build_blocks(
    date: str,
    items: List[Dict[str, Any]],
    *,
    quiet_day: bool = False,
    models: Optional[List[str]] = None,
    featured_urls: Optional[set] = None,
    n_filtered: int = 0,
) -> List[Dict[str, Any]]:
    """Build Notion blocks for the daily digest from ranked items."""
    blocks: List[Dict[str, Any]] = []
    featured_urls = featured_urls or set()

    if quiet_day:
        blocks.append(_callout(
            "Quiet day: too few strongly relevant papers for a full episode, so there is no podcast today. "
            f"The {len(items)} item(s) below are the ones that passed the relevance check; nothing was padded in."
            + (f" {n_filtered} candidate(s) were filtered out as off-topic." if n_filtered else ""),
            "🔕",
        ))
    elif n_filtered:
        blocks.append(_callout(f"{n_filtered} off-topic candidate(s) were filtered out before ranking.", "🧹"))

    def h2(text: str) -> Dict[str, Any]:
        return {"object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [_rich(text)]}}

    def bullet(title: str, url: str, snippet: str, source: str) -> Dict[str, Any]:
        rich: List[Dict[str, Any]] = [_rich(title, url)]
        parts = []
        if snippet:
            parts.append(snippet)
        if source:
            parts.append(f"[{source}]")
        if parts:
            rich.append(_rich("  —  " + "  ".join(parts)))
        return {"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": rich}}

    protein = [x for x in items if x.get("bucket") == "protein"]
    news = [x for x in items if x.get("bucket") not in ("protein", "daily")]
    daily = [x for x in items if x.get("bucket") == "daily"]

    for section_title, section_items in [
        ("Protein Design & Research", protein + news),
        ("Daily Knowledge", daily),
    ]:
        if not section_items:
            continue
        blocks.append(h2(section_title))
        for it in section_items:
            title = (it.get("title") or "").strip()[:200]
            url = (it.get("url") or "").strip()
            snippet = _strip_html((it.get("one_liner") or it.get("snippet") or "").strip())
            source = (it.get("source") or "").strip()
            if url and url in featured_urls:
                title = "★ " + title
            blocks.append(bullet(title, url, snippet, source))
        blocks.append({"object": "block", "type": "paragraph",
                       "paragraph": {"rich_text": []}})

    blocks.append(_callout(
        "Selection: rule-based relevance score (no LLM). "
        + (models_line(models, "Script written by") if models else "No podcast script generated."),
        "🤖",
    ))
    return blocks


def _api_call(method: str, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{_API}/{endpoint}", data=data, headers=_headers(), method=method
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _transcript_blocks(script_text: str) -> List[Dict[str, Any]]:
    """
    Convert a synthesis script into Notion blocks.

    Each [[TRANSITION]] becomes a heading_2 divider; the surrounding text
    is split into paragraph blocks (Notion caps rich_text at 2000 chars).
    """
    CHUNK = 1900  # stay safely under the 2000-char rich_text limit

    def para(text: str) -> Dict[str, Any]:
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [_rich(text)]}}

    def h2(text: str) -> Dict[str, Any]:
        return {"object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [_rich(text)]}}

    blocks: List[Dict[str, Any]] = []
    sections = script_text.split("[[TRANSITION]]")

    for i, section in enumerate(sections, 1):
        section = section.strip()
        if not section:
            continue

        # Section heading — use first non-empty line as subtitle
        first_line = next((l.strip() for l in section.splitlines() if l.strip()), "")
        subtitle = first_line[:80] + ("…" if len(first_line) > 80 else "")
        blocks.append(h2(f"Section {i}  —  {subtitle}"))

        # Chunk section text into ≤1900-char paragraphs
        for start in range(0, len(section), CHUNK):
            chunk = section[start:start + CHUNK].strip()
            if chunk:
                blocks.append(para(chunk))

        blocks.append(para(""))  # breathing room between sections

    return blocks


def save_transcript_to_notion(
    date: str,
    script_path: Path,
    models: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Save the full synthesis transcript to a dedicated Notion database.

    Env vars required:
      NOTION_TOKEN                  — same integration token as the digest
      NOTION_TRANSCRIPT_DATABASE_ID — ID of the new Transcript database

    Returns the Notion page URL, or None if skipped/failed.
    """
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_TRANSCRIPT_DATABASE_ID", "").strip().replace("-", "")
    if not token or not db_id:
        print("[notion] NOTION_TRANSCRIPT_DATABASE_ID not set — skipping transcript save", flush=True)
        return None

    if not script_path or not script_path.exists():
        print(f"[notion] Script file not found: {script_path}", flush=True)
        return None

    try:
        script_text = script_path.read_text(encoding="utf-8", errors="ignore")
        # Strip the References block appended at the end
        if "\n\nReferences:" in script_text:
            script_text = script_text[:script_text.index("\n\nReferences:")].strip()
    except Exception as e:
        print(f"[notion] Could not read script: {e}", flush=True)
        return None

    blocks = [_callout(models_line(models), "🤖")] + _transcript_blocks(script_text)
    first_batch, rest_blocks = blocks[:100], blocks[100:]

    try:
        page = _api_call("POST", "pages", {
            "parent": {"database_id": db_id},
            "properties": {
                "Name": {"title": [{"type": "text", "text": {"content": f"Transcript — {date}"}}]},
                "date": {"date": {"start": date}},
            },
            "children": first_batch,
        })

        page_id = page.get("id", "")
        page_url = page.get("url", "")

        while rest_blocks:
            batch, rest_blocks = rest_blocks[:100], rest_blocks[100:]
            _api_call("PATCH", f"blocks/{page_id}/children", {"children": batch})

        print(f"[notion] Transcript saved: {page_url}", flush=True)
        return page_url
    except Exception as e:
        print(f"[notion] Warning: failed to save transcript — {e}", flush=True)
        return None


def save_script_to_notion(
    date: str,
    script_path: Path,
    items: List[Dict[str, Any]],
    md_path: Optional[Path] = None,  # kept for backward compat, unused
    *,
    quiet_day: bool = False,
    models: Optional[List[str]] = None,
    featured_urls: Optional[set] = None,
    n_filtered: int = 0,
) -> Optional[str]:
    """Save the daily digest to Notion. Returns page URL or None."""
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_DATABASE_ID", "").strip().replace("-", "")
    if not token or not db_id:
        print("[notion] NOTION_TOKEN or NOTION_DATABASE_ID not set — skipping", flush=True)
        return None

    blocks = _build_blocks(
        date, items, quiet_day=quiet_day, models=models,
        featured_urls=featured_urls, n_filtered=n_filtered,
    )
    first_batch, rest_blocks = blocks[:100], blocks[100:]

    try:
        page = _api_call("POST", "pages", {
            "parent": {"database_id": db_id},
            "properties": {
                "Name": {"title": [{"type": "text", "text": {"content": f"Knowledge Radio — {date}"}}]},
                "Date": {"date": {"start": date}},
            },
            "children": first_batch,
        })

        page_id = page.get("id", "")
        page_url = page.get("url", "")

        while rest_blocks:
            batch, rest_blocks = rest_blocks[:100], rest_blocks[100:]
            _api_call("PATCH", f"blocks/{page_id}/children", {"children": batch})

        print(f"[notion] Saved: {page_url}", flush=True)
        return page_url
    except Exception as e:
        print(f"[notion] Warning: failed to save — {e}", flush=True)
        return None

#!/usr/bin/env python3
"""
tools/auto_deepdive.py

Runs at the start of each daily pipeline. Checks whether the owner labeled
any papers in the Deep Dive Notes database for *yesterday*. If not (because
they were busy), auto-adds every paper from yesterday's episode as a stub so
the weekly/monthly summaries still have material to work with.

Papers added here are marked Source = "Auto" so the owner can tell them apart
from ones they personally annotated.

Env vars:
  NOTION_API_KEY      — same integration token used throughout the pipeline
  NOTION_DATABASE_ID  — Deep Dive Notes database ID (default hardcoded)
  RUN_DATE            — date override (YYYY-MM-DD); yesterday = this date - 1
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date as _date, timedelta
from pathlib import Path

import requests

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
DATABASE_ID    = os.environ.get("NOTION_DATABASE_ID", "3165f58ea8c280498f72c770028aec0d")

HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

PACKAGE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR  = PACKAGE_DIR / "output"


def _check_date() -> str:
    """Return yesterday's date — the day the owner had time to label papers."""
    run_date = os.environ.get("RUN_DATE", "").strip()
    base = _date.fromisoformat(run_date) if run_date else _date.today()
    return (base - timedelta(days=1)).isoformat()


def _count_entries_for_date(date: str) -> int:
    """Query how many pages in the Deep Dive DB have Date == date."""
    body = {
        "filter": {"property": "Date", "date": {"equals": date}},
        "page_size": 1,
    }
    try:
        r = requests.post(
            f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
            json=body, headers=HEADERS, timeout=30,
        )
        if not r.ok:
            print(f"[auto_deepdive] Notion query error {r.status_code}: {r.text}", flush=True)
            return -1
        data = r.json()
        count = len(data.get("results", []))
        if data.get("has_more"):
            count += 1
        return count
    except Exception as e:
        print(f"[auto_deepdive] query failed: {e}", flush=True)
        return -1


def _load_episode_items(date: str) -> list[dict]:
    items_file = OUTPUT_DIR / date / "episode_items.json"
    if not items_file.exists():
        return []
    try:
        raw = json.loads(items_file.read_text(encoding="utf-8"))
        items = raw.get("items", raw) if isinstance(raw, dict) else raw
        return [i for i in items if i.get("url") and i.get("title")]
    except Exception as e:
        print(f"[auto_deepdive] failed to read episode_items: {e}", flush=True)
        return []


def _add_paper(item: dict, date: str) -> bool:
    title     = item.get("title", "")[:2000]
    url       = item.get("url", "")
    source    = item.get("source", "")
    one_liner = item.get("one_liner", "")

    body = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "Name":   {"title":  [{"text": {"content": title}}]},
            "Date":   {"date":   {"start": date}},
            "Source": {"select": {"name": "Auto"}},
        },
        "children": [
            {
                "object": "block", "type": "callout",
                "callout": {
                    "icon": {"type": "emoji", "emoji": "🤖"},
                    "rich_text": [{"type": "text", "text": {
                        "content": "Auto-added (owner had no labels that day). Add your notes below."
                    }}],
                    "color": "gray_background",
                },
            },
            {
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {
                    "content": f"📅 {date}   |   📰 {source or 'Unknown source'}"
                }}]},
            },
            *([{
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": one_liner}}]},
            }] if one_liner else []),
            {"object": "block", "type": "bookmark", "bookmark": {"url": url}},
            {"object": "block", "type": "divider", "divider": {}},
            {
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Deep Dive Notes"}}]},
            },
            {
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": ""}}]},
            },
        ],
    }
    try:
        r = requests.post(
            "https://api.notion.com/v1/pages",
            json=body, headers=HEADERS, timeout=30,
        )
        if not r.ok:
            print(f"[auto_deepdive] Notion error {r.status_code} for '{title[:60]}': {r.text}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"[auto_deepdive] failed to create page for '{title[:60]}': {e}", flush=True)
        return False


def main():
    if not NOTION_API_KEY:
        print("[auto_deepdive] NOTION_API_KEY not set — skipping", flush=True)
        return

    check_date = _check_date()
    print(f"[auto_deepdive] Checking Deep Dive labels for {check_date} (yesterday)", flush=True)

    count = _count_entries_for_date(check_date)
    if count < 0:
        print("[auto_deepdive] Could not query Notion — skipping auto-add", flush=True)
        return
    if count > 0:
        print(f"[auto_deepdive] {count} paper(s) already labeled for {check_date} — nothing to do", flush=True)
        return

    print(f"[auto_deepdive] No labels found for {check_date} — auto-adding all papers", flush=True)
    items = _load_episode_items(check_date)
    if not items:
        print(f"[auto_deepdive] No episode items found for {check_date} — skipping", flush=True)
        return

    print(f"[auto_deepdive] Adding {len(items)} papers to Deep Dive Notes...", flush=True)
    ok = sum(1 for item in items if _add_paper(item, check_date))
    print(f"[auto_deepdive] Done — {ok}/{len(items)} papers added for {check_date}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"[auto_deepdive] FAILED: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

import feedparser
import requests as _requests
from dateutil import parser as dtparser

from src.utils.timeutils import cutoff_datetime

_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; feedbot/1.0; +https://github.com)"}


def _parse_dt(dt_str: str) -> Optional[datetime]:
    try:
        return dtparser.parse(dt_str)
    except Exception:
        return None


def _fetch_source(
    src: Dict[str, Any],
    cutoff: datetime,
    upper: datetime,
) -> List[Dict[str, Any]]:
    """Fetch and parse one RSS source. Returns items within the time window.

    Uses requests for HTTP fetching so that arXiv API URLs (which redirect
    http→https and require a proper User-Agent) are handled correctly.
    feedparser is used only for parsing the already-fetched content.
    """
    try:
        resp = _requests.get(src["url"], timeout=30, headers=_FETCH_HEADERS)
        feed = feedparser.parse(resp.content)
    except Exception:
        return []

    items: List[Dict[str, Any]] = []
    for e in getattr(feed, "entries", []) or []:
        title = (getattr(e, "title", "") or "").strip()
        url = (getattr(e, "link", "") or "").strip()

        # date
        dt = None
        for k in ["published", "updated", "created"]:
            v = getattr(e, k, None)
            if v:
                dt = _parse_dt(v)
                if dt:
                    break

        if dt is not None:
            try:
                dt_local = dt.astimezone(cutoff.tzinfo)
                # bounded window: [cutoff, upper)
                if dt_local < cutoff or dt_local >= upper:
                    continue
            except Exception:
                # if naive / weird, keep it (dedup handles repeats)
                pass

        summary = (getattr(e, "summary", "") or "").strip()
        if len(summary) > 360:
            summary = summary[:357] + "..."

        items.append(
            {
                "bucket": src.get("bucket", "protein"),
                "source": src["name"],
                "source_type": "rss",
                "title": title,
                "url": url,
                "one_liner": summary or "",
                "tags": list(src.get("tags", [])),
            }
        )
    return items


def collect_rss_items(
    sources: List[Dict[str, Any]],
    *,
    tz,
    lookback_hours: int,
    now_ref: Optional[datetime] = None,
    max_workers: int = 12,
) -> List[Dict[str, Any]]:
    upper = now_ref or datetime.now(tz)
    cutoff = cutoff_datetime(tz, lookback_hours, now_dt=upper)
    out: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_source, src, cutoff, upper): src for src in sources}
        for fut in as_completed(futures):
            try:
                out.extend(fut.result())
            except Exception as exc:
                src = futures[fut]
                print(f"[rss] Warning: failed to fetch {src.get('name','?')}: {exc}", flush=True)

    return out

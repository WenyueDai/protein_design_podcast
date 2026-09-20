from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

import feedparser
import requests as _requests
from dateutil import parser as dtparser

from src.utils.timeutils import cutoff_datetime

_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; feedbot/1.0; +https://github.com)"}
# Some publishers (Cloudflare/CloudFront-fronted, e.g. Endpoints News) block the
# self-identifying bot UA above but allow an ordinary browser UA through. Only used
# as a same-request fallback on a 403 — sources that are fine with _FETCH_HEADERS
# never see this.
_BROWSER_FALLBACK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}
# arXiv rate limit: 1 request per 3s recommended; use 1 worker + 3.5s delay
_ARXIV_MAX_WORKERS = 1
_ARXIV_DELAY = 3.5  # seconds between arXiv requests
_ARXIV_429_BACKOFF = 15.0  # seconds to wait after a 429


def _parse_dt(dt_str: str) -> Optional[datetime]:
    try:
        return dtparser.parse(dt_str)
    except Exception:
        return None


def _fetch_source(
    src: Dict[str, Any],
    cutoff: datetime,
    upper: datetime,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Fetch and parse one RSS source. Returns items within the time window.

    Uses requests for HTTP fetching so that arXiv API URLs (which redirect
    http→https and require a proper User-Agent) are handled correctly.
    feedparser is used only for parsing the already-fetched content.

    If `diagnostics` is passed, this records per-source fetch/parse outcome
    into diagnostics[source_name] — raw entry count, how many survived the
    time-window filter, and the newest entry's date — so a source that is
    silently always empty (blocked, or a stale/rolling feed) can be told
    apart from one that's simply had nothing new to report.
    """
    source_name = src.get("name", "?")
    source_url = src.get("url", "")
    is_arxiv = "arxiv" in source_name.lower() or "arxiv" in source_url.lower()
    diag: Dict[str, Any] = {"http_status": None, "raw_entries": 0, "kept": 0, "newest_entry": None, "error": None}

    def _record():
        if diagnostics is not None:
            diagnostics[source_name] = diag

    max_attempts = 3 if is_arxiv else 1
    for attempt in range(1, max_attempts + 1):
        try:
            resp = _requests.get(source_url, timeout=30, headers=_FETCH_HEADERS)
            if resp.status_code == 429 and attempt < max_attempts:
                print(f"[rss] arXiv 429 for {source_name}, backing off {_ARXIV_429_BACKOFF}s (attempt {attempt})", flush=True)
                time.sleep(_ARXIV_429_BACKOFF)
                continue
            if resp.status_code == 403:
                # Some publishers block the self-identifying bot UA but allow a
                # browser UA through (e.g. CloudFront rules keyed on UA string).
                print(f"[rss] {source_name} returned 403, retrying with a browser User-Agent", flush=True)
                try:
                    resp = _requests.get(source_url, timeout=30, headers=_BROWSER_FALLBACK_HEADERS)
                except _requests.RequestException:
                    pass
            diag["http_status"] = resp.status_code
            resp.raise_for_status()
            break
        except _requests.RequestException as exc:
            if attempt == max_attempts:
                diag["http_status"] = getattr(exc.response, "status_code", None)
                diag["error"] = f"{exc.__class__.__name__}: {exc}"
                _record()
                print(
                    f"[rss] Warning: HTTP fetch failed for {source_name}: "
                    f"{exc.__class__.__name__}: {exc}",
                    flush=True,
                )
                return []
            time.sleep(_ARXIV_429_BACKOFF)
    try:
        feed = feedparser.parse(resp.content)
    except Exception as exc:
        diag["error"] = f"parse failed: {exc.__class__.__name__}: {exc}"
        _record()
        print(f"[rss] Warning: parse failed for {source_name}: {exc.__class__.__name__}: {exc}", flush=True)
        return []
    if getattr(feed, "bozo", 0):
        bozo_exc = getattr(feed, "bozo_exception", None)
        print(
            f"[rss] Warning: malformed feed for {source_name}: "
            f"{bozo_exc or 'unknown parse error'}",
            flush=True,
        )

    entries = getattr(feed, "entries", []) or []
    diag["raw_entries"] = len(entries)
    if is_arxiv and not entries:
        print(
            f"[rss] Warning: {source_name} returned 0 feed entries "
            f"(HTTP {resp.status_code})",
            flush=True,
        )

    items: List[Dict[str, Any]] = []
    newest_dt: Optional[datetime] = None
    for e in entries:
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

        if dt is not None and (newest_dt is None or dt > newest_dt):
            newest_dt = dt

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

    diag["kept"] = len(items)
    diag["newest_entry"] = newest_dt.isoformat() if newest_dt else None
    _record()
    return items


def collect_rss_items(
    sources: List[Dict[str, Any]],
    *,
    tz,
    lookback_hours: int,
    now_ref: Optional[datetime] = None,
    max_workers: int = 12,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """`diagnostics`, if given, is filled in place with one entry per source name —
    see `_fetch_source` — so a persistently-empty source can be told apart from a
    blocked/broken one without digging through CI logs."""
    upper = now_ref or datetime.now(tz)
    cutoff = cutoff_datetime(tz, lookback_hours, now_dt=upper)
    out: List[Dict[str, Any]] = []

    # Split arXiv feeds (rate-limited) from others to avoid 429s.
    arxiv_sources = [s for s in sources if "arxiv" in (s.get("url") or "").lower()]
    other_sources = [s for s in sources if s not in arxiv_sources]

    def _submit_batch(batch, workers):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_source, src, cutoff, upper, diagnostics): src for src in batch}
            for fut in as_completed(futures):
                try:
                    out.extend(fut.result())
                except Exception as exc:
                    src = futures[fut]
                    print(f"[rss] Warning: failed to fetch {src.get('name','?')}: {exc}", flush=True)

    # Fetch non-arXiv feeds in parallel
    if other_sources:
        _submit_batch(other_sources, max_workers)

    # Fetch arXiv feeds with low concurrency + delay to respect rate limits
    if arxiv_sources:
        for i in range(0, len(arxiv_sources), _ARXIV_MAX_WORKERS):
            batch = arxiv_sources[i:i + _ARXIV_MAX_WORKERS]
            _submit_batch(batch, _ARXIV_MAX_WORKERS)
            if i + _ARXIV_MAX_WORKERS < len(arxiv_sources):
                time.sleep(_ARXIV_DELAY)

    return out

"""
bioRxiv author collector — fetches recent bioRxiv preprints and filters by tracked authors.

bioRxiv's RSS search returns 403, so we use their official content API instead:
  https://api.biorxiv.org/details/biorxiv/{start}/{end}/{cursor}/json

Returns items tagged ["protein-design", "author"] with source="<Name> (bioRxiv)"
so they are recognised as tier-0 by rank.py (_is_researcher_feed checks "biorxiv" in src).

Config section (config.yaml):
  biorxiv_authors:
    enabled: true
    lookback_days: 3
    authors:
      - name: "David Baker"
        match: "Baker, D"          # substring to find in the authors field
        institution: "Washington"  # optional: also require this in corresponding institution
      - name: "Frank DiMaio"
        match: "DiMaio, F"
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

_API_BASE = "https://api.biorxiv.org/details/biorxiv"
_TIMEOUT = (10, 90)
_PAGE_SIZE = 100
_MAX_RETRIES = 3


def _norm_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _author_patterns(name: str, match: str) -> List[str]:
    patterns: List[str] = []
    for raw in [match, name]:
        norm = _norm_text(raw)
        if norm:
            patterns.append(norm)

    if "," in match:
        last, _, rest = match.partition(",")
        last_norm = _norm_text(last)
        initials = [tok[:1] for tok in _norm_text(rest).split() if tok]
        if last_norm and initials:
            patterns.append(f"{last_norm} {' '.join(initials)}")
            patterns.append(f"{last_norm} {initials[0]}")

    deduped: List[str] = []
    seen = set()
    for pat in patterns:
        if pat and pat not in seen:
            seen.add(pat)
            deduped.append(pat)
    return deduped


def _matches_author(authors_norm: str, patterns: List[str]) -> bool:
    return any(pat in authors_norm for pat in patterns)


def _fetch_page(
    start: str,
    end: str,
    cursor: int,
    session: requests.Session,
    *,
    category: Optional[str] = None,
) -> dict:
    url = f"{_API_BASE}/{start}/{end}/{cursor}/json"
    if category:
        url += "?" + urlencode({"category": category})
    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_err = exc
            if attempt == _MAX_RETRIES:
                break
            time.sleep(1.5 * attempt)
    raise last_err


def fetch_recent_biorxiv_papers(
    *,
    lookback_days: int,
    category: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> List[dict]:
    """Fetch recent bioRxiv papers day-by-day with pagination and retries."""
    today = date.today()
    own_session = session is None
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", "protein-design-podcast/1.0 (academic research)")

    all_papers: List[dict] = []
    try:
        for day_offset in range(lookback_days):
            day_start = today - timedelta(days=day_offset + 1)
            day_end = today - timedelta(days=day_offset)
            cursor = 0
            total = None

            while True:
                data = _fetch_page(
                    day_start.isoformat(),
                    day_end.isoformat(),
                    cursor,
                    sess,
                    category=category,
                )

                papers = data.get("collection") or []
                if total is None:
                    try:
                        total = int(data["messages"][0]["total"])
                    except Exception:
                        total = 0

                all_papers.extend(papers)
                cursor += len(papers)

                if not papers or len(papers) < _PAGE_SIZE or cursor >= total:
                    break
                time.sleep(0.3)  # be polite to the API
    finally:
        if own_session:
            sess.close()

    return all_papers


def collect_biorxiv_author_items(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Fetch recent bioRxiv preprints and return those matching tracked authors.
    Items get source="<Name> (bioRxiv)" and tags=["protein-design", "author"]
    so rank.py treats them as tier-0 (biorxiv in source + author tag).
    """
    bio_cfg = cfg.get("biorxiv_authors") or {}
    if not bio_cfg.get("enabled", True):
        return []

    authors_cfg: List[Dict[str, Any]] = bio_cfg.get("authors") or []
    if not authors_cfg:
        return []

    lookback_days = int(bio_cfg.get("lookback_days", 3))
    today = date.today()
    start = (today - timedelta(days=lookback_days)).isoformat()
    end = today.isoformat()

    # Build lookup: name → normalized author patterns + institution filter.
    author_lookup: List[tuple] = []
    for a in authors_cfg:
        name = (a.get("name") or "").strip()
        match = (a.get("match") or "").strip()
        institution = _norm_text((a.get("institution") or "").strip())
        if name and match:
            author_lookup.append((name, _author_patterns(name, match), institution))

    if not author_lookup:
        return []

    print(f"[biorxiv_authors] Fetching papers {start} → {end} for {len(author_lookup)} authors", flush=True)

    try:
        all_papers = fetch_recent_biorxiv_papers(lookback_days=lookback_days)
    except Exception as e:
        print(f"[biorxiv_authors] API error: {e}", flush=True)
        return []

    print(f"[biorxiv_authors] Fetched {len(all_papers)} total papers from bioRxiv", flush=True)

    # Match against tracked authors
    items: List[Dict[str, Any]] = []
    matched_names: Dict[str, int] = {}
    inst_rejects: Dict[str, int] = {}

    for paper in all_papers:
        authors_str = paper.get("authors", "")
        authors_norm = _norm_text(authors_str)
        institution = _norm_text(paper.get("author_corresponding_institution") or "")

        for (name, patterns, inst_filter) in author_lookup:
            if not _matches_author(authors_norm, patterns):
                continue
            if inst_filter and inst_filter not in institution:
                inst_rejects[name] = inst_rejects.get(name, 0) + 1
                continue

            doi = paper.get("doi", "")
            url = f"https://www.biorxiv.org/content/{doi}" if doi else ""
            if not url:
                continue

            title = (paper.get("title") or "").strip()
            abstract = (paper.get("abstract") or "").strip()
            pub_date = paper.get("date", end)

            items.append({
                "title": title,
                "url": url,
                "source": f"{name} (bioRxiv)",
                "published": pub_date,
                "snippet": abstract[:400] if abstract else "",
                "one_liner": "",
                "bucket": "protein",
                "tags": ["protein-design", "author"],
                "extracted_chars": len(abstract),
            })
            matched_names[name] = matched_names.get(name, 0) + 1
            break  # don't double-count if multiple patterns match

    if matched_names:
        print(f"[biorxiv_authors] Matched: {matched_names}", flush=True)
    else:
        print(f"[biorxiv_authors] No matches this window (normal on quiet days)", flush=True)
    if inst_rejects:
        print(f"[biorxiv_authors] Institution-filter rejects: {inst_rejects}", flush=True)

    return items

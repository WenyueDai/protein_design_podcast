"""
bioRxiv keyword collector — fetches recent bioRxiv preprints and filters them
locally using the same keyword pool as the PubMed collector.

Config section (config.yaml):
  biorxiv_keywords:
    enabled: true
    lookback_days: 2
    bucket: "protein"
    tags: ["biorxiv", "preprint", "biology"]
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src.collectors.biorxiv_authors import fetch_recent_biorxiv_papers, _norm_text

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "for", "to", "is", "are", "with",
    "from", "by", "on", "at", "this", "that", "using", "based", "via", "model",
    "models", "learning", "computational",
}


def _term_keywords(term: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", (term or "").lower())
    return [w for w in words if len(w) >= 4 and w not in _STOPWORDS]


def _term_matches(term: str, hay_norm: str) -> bool:
    phrase = _norm_text(term)
    if phrase and phrase in hay_norm:
        return True

    kws = _term_keywords(term)
    if not kws:
        return False

    hits = sum(1 for kw in kws if kw in hay_norm)
    needed = len(kws) if len(kws) <= 3 else min(3, len(kws))
    return hits >= needed


def collect_biorxiv_keyword_items(
    cfg: Dict[str, Any],
    *,
    lookback_hours: int = 48,
    extra_terms: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    biorxiv_cfg = cfg.get("biorxiv_keywords", {})
    if not biorxiv_cfg.get("enabled", False):
        return []

    term_pool: List[str] = list((cfg.get("pubmed", {}) or {}).get("search_terms", []))
    if extra_terms:
        existing_lower = {t.lower() for t in term_pool}
        for t in extra_terms:
            if t.lower() not in existing_lower:
                term_pool.append(t)
                existing_lower.add(t.lower())
                print(f"[biorxiv_keywords] Dynamic term from feedback: \"{t}\"", flush=True)

    if not term_pool:
        return []

    lookback_days = int(biorxiv_cfg.get("lookback_days") or max(1, lookback_hours // 24))
    bucket = biorxiv_cfg.get("bucket", "protein")
    tags = list(biorxiv_cfg.get("tags", ["biorxiv", "preprint", "biology"]))

    try:
        papers = fetch_recent_biorxiv_papers(lookback_days=lookback_days)
    except Exception as e:
        print(f"[biorxiv_keywords] API error: {e}", flush=True)
        return []

    print(
        f"[biorxiv_keywords] Fetched {len(papers)} total papers for local keyword filtering",
        flush=True,
    )

    seen_urls: set[str] = set()
    matched_items: List[Dict[str, Any]] = []

    for paper in papers:
        title = (paper.get("title") or "").strip()
        abstract = (paper.get("abstract") or "").strip()
        category = (paper.get("category") or "").strip()
        hay_norm = _norm_text(" ".join([title, abstract, category]))

        if not any(_term_matches(term, hay_norm) for term in term_pool):
            continue

        doi = (paper.get("doi") or "").strip()
        url = f"https://www.biorxiv.org/content/{doi}" if doi else ""
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        one_liner = abstract[:357] + "..." if len(abstract) > 360 else abstract
        source_label = f"bioRxiv — {category}" if category else "bioRxiv — Preprint"

        matched_items.append(
            {
                "bucket": bucket,
                "source": source_label,
                "source_type": "biorxiv",
                "title": title,
                "url": url,
                "one_liner": one_liner,
                "tags": tags,
                "authors": (paper.get("authors") or "").strip(),
            }
        )

    print(
        f"[biorxiv_keywords] Matched {len(matched_items)} papers from {len(term_pool)} shared terms",
        flush=True,
    )
    return matched_items

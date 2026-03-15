"""
Semantic Scholar author-based paper collection.

For each tracked researcher, fetches their recent papers from S2 using their
permanent S2 author ID (from state/s2_author_ids.json).  This replaces the
arXiv RSS author feeds and supplements biorxiv_authors with full coverage
(arXiv + bioRxiv + journals) and zero false positives.

Rate limit: 1 req/s without key, 10 req/s with key.  All calls are throttled
to _DELAY seconds; callers do not need additional delays.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

S2_BASE = "https://api.semanticscholar.org/graph/v1"
_DELAY = 0.2    # ~5 req/s with API key; conservative to avoid 429s
_PAPER_FIELDS = "title,abstract,year,publicationDate,externalIds,url"

# At least one of these must appear in title+abstract for a paper to be kept.
# Catches off-topic papers from researchers who publish across multiple fields.
_RELEVANCE_KEYWORDS = {
    "protein", "peptide", "enzyme", "antibody", "antigen", "nanobody",
    "amino acid", "residue", "fold", "folding", "structure", "binding",
    "ligand", "receptor", "drug", "therapeutic", "sequence", "mutation",
    "design", "generative", "diffusion", "language model", "transformer",
    "rna", "dna", "nucleic", "genomic", "molecular", "biomolecular",
    "alphafold", "rosetta", "coevolution", "contact map", "force field",
    "molecular dynamics", "md simulation", "free energy", "docking",
    "scaffold", "backbone", "side chain", "active site", "allosteric",
}


def _get(path: str, params: Dict, api_key: str) -> Optional[Dict]:
    headers = {"x-api-key": api_key} if api_key else {}
    url = f"{S2_BASE}{path}"
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                print(f"[s2_authors] 429 — backing off {wait}s", flush=True)
                time.sleep(wait)
                continue
        except Exception as exc:
            print(f"[s2_authors] request error: {exc}", flush=True)
            break
    return None


def _paper_url(paper: Dict) -> str:
    """Derive a canonical URL from S2 externalIds, preferring arXiv then bioRxiv."""
    ext = paper.get("externalIds") or {}
    arxiv_id = ext.get("ArXiv")
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    doi = ext.get("DOI") or ""
    if "biorxiv" in doi.lower() or "medrxiv" in doi.lower():
        return f"https://doi.org/{doi}"
    s2_url = paper.get("url") or ""
    if s2_url:
        return s2_url
    corpus_id = ext.get("CorpusId")
    if corpus_id:
        return f"https://www.semanticscholar.org/paper/{corpus_id}"
    return ""


def _fetch_author_papers(
    author_id: str,
    author_name: str,
    cutoff: date,
    api_key: str,
) -> List[Dict[str, Any]]:
    """Return recent papers (since cutoff) for one S2 author."""
    time.sleep(_DELAY)
    data = _get(
        f"/author/{author_id}/papers",
        {"fields": _PAPER_FIELDS, "limit": 20},
        api_key,
    )
    if not data:
        return []

    results = []
    for paper in (data.get("data") or []):
        pub_date_str = paper.get("publicationDate") or ""
        if not pub_date_str:
            # Fall back to year-only check
            year = paper.get("year")
            if not year or int(year) < cutoff.year:
                continue
        else:
            try:
                pub_date = date.fromisoformat(pub_date_str)
                if pub_date < cutoff:
                    continue
            except ValueError:
                continue

        url = _paper_url(paper)
        if not url:
            continue

        title = (paper.get("title") or "").strip()
        abstract = (paper.get("abstract") or "").strip()
        if not title:
            continue

        # Relevance filter: skip papers with no biology/ML keywords in title+abstract.
        text_lower = (title + " " + abstract).lower()
        if not any(kw in text_lower for kw in _RELEVANCE_KEYWORDS):
            print(f"[s2_authors] Skipping off-topic paper by {author_name}: {title[:60]}", flush=True)
            continue

        results.append({
            "title": title,
            "url": url,
            "source": f"{author_name} (S2)",
            "published": pub_date_str or str(paper.get("year", "")),
            "snippet": abstract[:400] if abstract else "",
            "one_liner": "",
            "bucket": "protein",
            "tags": ["protein-design", "author"],
            "extracted_chars": len(abstract),
        })

    return results


def collect_s2_author_items(
    cfg: Dict[str, Any],
    api_key: str = "",
    author_ids_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Collect recent papers by tracked authors using S2 author IDs.

    Requires state/s2_author_ids.json (generated by tools/setup_s2_authors.py).
    Falls back gracefully if the file is missing or an author has no ID.
    """
    if not api_key:
        api_key = os.environ.get("S2_API_KEY", "").strip()
    if not api_key:
        return []

    # Load author ID map
    if author_ids_path is None:
        author_ids_path = Path(__file__).resolve().parents[2] / "state" / "s2_author_ids.json"

    if not author_ids_path.exists():
        print("[s2_authors] state/s2_author_ids.json not found — skipping S2 author collection", flush=True)
        return []

    try:
        author_ids: Dict[str, str] = json.loads(author_ids_path.read_text())
    except Exception as exc:
        print(f"[s2_authors] could not load author IDs: {exc}", flush=True)
        return []

    s2_cfg = cfg.get("s2_authors") or {}
    lookback_days = int(s2_cfg.get("lookback_days", 4))
    cutoff = date.today() - timedelta(days=lookback_days)

    print(f"[s2_authors] Fetching papers since {cutoff} for {len(author_ids)} authors", flush=True)

    all_items: List[Dict[str, Any]] = []
    matched: Dict[str, int] = {}
    missing_ids: List[str] = []

    for name, author_id in author_ids.items():
        if not author_id:
            missing_ids.append(name)
            continue
        papers = _fetch_author_papers(author_id, name, cutoff, api_key)
        if papers:
            matched[name] = len(papers)
            all_items.extend(papers)

    if matched:
        print(f"[s2_authors] Matched: {matched}", flush=True)
    else:
        print("[s2_authors] No matches in lookback window (normal on quiet days)", flush=True)
    if missing_ids:
        print(f"[s2_authors] No S2 ID for: {missing_ids}", flush=True)

    return all_items

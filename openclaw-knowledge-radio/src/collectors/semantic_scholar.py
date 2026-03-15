"""
Semantic Scholar API enrichment for openclaw-knowledge-radio.

Three capabilities:
  1. reference_score   — how deeply a candidate paper is rooted in protein
                         design literature, used as a soft ranking tiebreaker.
  2. shared_landscape  — which foundational papers today's featured batch
                         collectively builds on, injected into the synthesis LLM.
  3. missed_surfaces   — recent highly-cited references not yet in the pipeline,
                         surfaced for the user to review.

Rate limit: 1 req/sec (authenticated key).  All public functions sleep between
calls; callers do not need to add their own delays.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

S2_BASE = "https://api.semanticscholar.org/graph/v1"
_DELAY = 1.05  # slightly over 1 s to stay safely under the rate limit


# ---------------------------------------------------------------------------
# Low-level HTTP helper
# ---------------------------------------------------------------------------

def _get(path: str, params: Dict, api_key: str) -> Optional[Dict]:
    headers = {"x-api-key": api_key} if api_key else {}
    url = f"{S2_BASE}{path}"
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            # Back off and retry once
            time.sleep(10)
            r = requests.get(url, params=params, headers=headers, timeout=15)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Open-access PDF URL lookup
# ---------------------------------------------------------------------------

def get_open_access_pdf_url(paper_id: str, api_key: str) -> Optional[str]:
    """
    Fetch the open-access PDF URL for a paper from Semantic Scholar.

    Calls GET /graph/v1/paper/{paper_id}?fields=openAccessPdf and returns
    data["openAccessPdf"]["url"] if present, else None.
    Sleeps _DELAY between calls to respect the rate limit.
    """
    time.sleep(_DELAY)
    data = _get(f"/paper/{paper_id}", {"fields": "openAccessPdf"}, api_key)
    if not data:
        return None
    pdf_info = data.get("openAccessPdf") or {}
    url = (pdf_info.get("url") or "").strip()
    return url if url else None


# ---------------------------------------------------------------------------
# Paper ID resolution
# ---------------------------------------------------------------------------

def _arxiv_id(url: str) -> Optional[str]:
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", url or "")
    return m.group(1) if m else None


def _doi_from_url(url: str) -> Optional[str]:
    m = re.search(r"(?:doi\.org/|/doi/)(10\.\d{4,}/\S+)", url or "")
    return m.group(1).rstrip("/.,)") if m else None


def resolve_paper_id(url: str, title: str, api_key: str) -> Optional[str]:
    """
    Try arXiv ID → DOI → title search to find the S2 paper ID.
    Returns None if the paper cannot be found.
    Each sub-attempt is rate-limited.
    """
    # 1) arXiv ID (fastest and most reliable for preprints)
    arxiv = _arxiv_id(url)
    if arxiv:
        time.sleep(_DELAY)
        data = _get(f"/paper/ARXIV:{arxiv}", {"fields": "paperId"}, api_key)
        if data and data.get("paperId"):
            return data["paperId"]

    # 2) DOI
    doi = _doi_from_url(url)
    if doi:
        time.sleep(_DELAY)
        data = _get(f"/paper/DOI:{doi}", {"fields": "paperId"}, api_key)
        if data and data.get("paperId"):
            return data["paperId"]

    # 3) Title search (least reliable — only use if nothing else works)
    if title:
        time.sleep(_DELAY)
        data = _get(
            "/paper/search",
            {"query": title[:120], "fields": "paperId,title", "limit": 1},
            api_key,
        )
        if data and data.get("data"):
            return data["data"][0].get("paperId")

    return None


# ---------------------------------------------------------------------------
# Reference fetching
# ---------------------------------------------------------------------------

def fetch_references(paper_id: str, api_key: str) -> List[Dict]:
    """
    Fetch up to 100 references for a paper.
    Returns a list of cited-paper dicts with keys:
      paperId, title, year, authors, citationCount, externalIds, abstract
    """
    time.sleep(_DELAY)
    data = _get(
        f"/paper/{paper_id}/references",
        {
            "fields": "paperId,title,year,authors,citationCount,externalIds,abstract",
            "limit": 100,
        },
        api_key,
    )
    if not data:
        return []
    refs = []
    for item in data.get("data", []):
        cited = item.get("citedPaper") or {}
        if cited.get("paperId"):
            refs.append(cited)
    return refs


def top_refs_for_synthesis(refs: List[Dict], top_n: int = 8) -> List[Dict]:
    """
    Pick the top N most-cited references to inject as related literature context
    in the synthesis prompt.  Returns a trimmed list of dicts:
      {title, year, citationCount, abstract}
    """
    sorted_refs = sorted(refs, key=lambda r: -(r.get("citationCount") or 0))
    out = []
    for ref in sorted_refs[:top_n]:
        abstract = (ref.get("abstract") or "").strip()
        out.append({
            "title": ref.get("title") or "",
            "year": ref.get("year"),
            "citationCount": ref.get("citationCount") or 0,
            # Truncate abstract so it doesn't explode the prompt
            "abstract": abstract[:600] + ("…" if len(abstract) > 600 else ""),
        })
    return out


# ---------------------------------------------------------------------------
# Idea 1: Reference score — how protein-design-grounded is this paper?
# ---------------------------------------------------------------------------

def score_references(refs: List[Dict], cfg: Dict) -> float:
    """
    Return a 0.0–1.0 score reflecting how deeply a paper is rooted in protein
    design literature, based on what it cites.

    Scoring weights (per reference):
      +3  title matches an absolute landmark keyword (AlphaFold, ProteinMPNN…)
      +1  title matches a topic boost keyword (diffusion model, fitness landscape…)
      +2  any author matches a tracked researcher name

    Normalised so that ~20 points → 1.0 (a paper citing 6–7 landmarks scores ~1.0).
    """
    if not refs:
        return 0.0

    r = cfg.get("ranking") or {}
    abs_kws  = [k.lower() for k in (r.get("absolute_title_keywords") or [])]
    topic_kws = [k.lower() for k in (r.get("topic_boost_keywords") or [])]
    tracked  = [s.lower() for s in (r.get("absolute_source_substrings") or [])]

    hits = 0.0
    for ref in refs:
        title   = (ref.get("title") or "").lower()
        authors = " ".join(
            (a.get("name") or "") for a in (ref.get("authors") or [])
        ).lower()

        if any(k in title for k in abs_kws):
            hits += 3
        elif any(k in title for k in topic_kws):
            hits += 1

        if any(t in authors for t in tracked):
            hits += 2

    return min(hits / 20.0, 1.0)


# ---------------------------------------------------------------------------
# Idea 2: Shared landscape — what foundational papers does today's batch share?
# ---------------------------------------------------------------------------

def build_shared_landscape(
    papers_with_refs: List[Tuple[str, List[Dict]]],
    min_count: int = 2,
    top_n: int = 15,
) -> List[Dict]:
    """
    Find reference papers cited by multiple featured papers today.

    papers_with_refs: list of (paper_title, references_list)
    Returns a list of dicts sorted by citation frequency:
      {paperId, title, year, cited_by_count}
    """
    counts: Counter = Counter()
    meta: Dict[str, Dict] = {}

    for _paper_title, refs in papers_with_refs:
        seen_in_this: Set[str] = set()
        for ref in refs:
            pid = ref.get("paperId") or ""
            if pid and pid not in seen_in_this:
                counts[pid] += 1
                seen_in_this.add(pid)
                if pid not in meta:
                    meta[pid] = {
                        "paperId": pid,
                        "title": ref.get("title") or "",
                        "year": ref.get("year"),
                    }

    landscape = []
    for pid, count in counts.most_common(top_n):
        if count < min_count:
            break
        entry = dict(meta[pid])
        entry["cited_by_count"] = count
        landscape.append(entry)

    return landscape


# ---------------------------------------------------------------------------
# Idea 3: Missed surfaces — recent notable papers the pipeline hasn't seen yet
# ---------------------------------------------------------------------------

def find_missed_surfaces(
    all_refs: List[Dict],
    is_seen: Callable[[str], bool],
    min_citations: int = 5,
    lookback_months: int = 6,
) -> List[Dict]:
    """
    Surface referenced papers that:
      - were published in the last `lookback_months`
      - already have >= `min_citations` citations
      - are NOT in the pipeline's seen_ids

    These are papers the field is actively building on that the pipeline missed.
    Returns up to 10 candidates sorted by citation count (descending).

    is_seen: callable(url) -> bool, wrapping SeenStore.has()
    """
    today = date.today()
    cutoff_year = today.year
    cutoff_month = today.month - lookback_months
    if cutoff_month <= 0:
        cutoff_year -= 1
        cutoff_month += 12

    deduped: Set[str] = set()
    surfaced = []

    for ref in all_refs:
        pid = ref.get("paperId") or ""
        if not pid or pid in deduped:
            continue
        deduped.add(pid)

        year = ref.get("year") or 0
        if year < cutoff_year or (year == cutoff_year and False):
            # Rough year-only filter — S2 doesn't give month in references
            if year < cutoff_year - 1:
                continue

        citations = ref.get("citationCount") or 0
        if citations < min_citations:
            continue

        ext = ref.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv")
        doi = ext.get("DOI")

        # Check if already seen via arXiv URL
        already_seen = False
        if arxiv_id:
            candidate_url = f"https://arxiv.org/abs/{arxiv_id}"
            if is_seen(candidate_url):
                already_seen = True

        if not already_seen:
            surfaced.append({
                "title": ref.get("title") or "",
                "year": year,
                "citations": citations,
                "arxiv_id": arxiv_id,
                "doi": doi,
            })

    return sorted(surfaced, key=lambda x: -x["citations"])[:10]


# ---------------------------------------------------------------------------
# Full-text extraction for featured papers via S2 openAccessPdf
# ---------------------------------------------------------------------------

def _extract_pdf_text(pdf_url: str, max_chars: int = 30_000) -> str:
    """Download a PDF and extract its text. Returns '' on any failure."""
    try:
        import io
        from pdfminer.high_level import extract_text as pdf_extract
        headers = {"User-Agent": "openclaw-knowledge-radio/1.0 (research bot)"}
        resp = requests.get(pdf_url, headers=headers, timeout=60, stream=True)
        if resp.status_code != 200:
            return ""
        raw = resp.content
        text = pdf_extract(io.BytesIO(raw))
        text = (text or "").strip()
        return text[:max_chars]
    except Exception:
        return ""


def enrich_featured_fulltext(
    featured_items: List[Dict[str, Any]],
    api_key: str,
    max_chars: int = 30_000,
) -> None:
    """
    For each featured paper that has an s2_paper_id but no fulltext yet:
      1. Fetch paper details from S2 (openAccessPdf, tldr, abstract).
      2. If openAccessPdf.url is available, download the PDF and extract full text.
      3. Store s2_tldr, s2_abstract, and update analysis/has_fulltext on the item.

    Mutates items in-place. Called AFTER final ranking selects the featured set.
    """
    for item in featured_items:
        paper_id = item.get("s2_paper_id")
        if not paper_id:
            continue

        time.sleep(_DELAY)
        data = _get(
            f"/paper/{paper_id}",
            {"fields": "abstract,tldr,openAccessPdf"},
            api_key,
        )
        if not data:
            continue

        # Store S2 abstract and TLDR
        s2_abstract = (data.get("abstract") or "").strip()
        tldr_obj = data.get("tldr") or {}
        s2_tldr = (tldr_obj.get("text") or "").strip()
        if s2_abstract:
            item["s2_abstract"] = s2_abstract
        if s2_tldr:
            item["s2_tldr"] = s2_tldr

        # Try full-text extraction if we don't already have it
        if not item.get("has_fulltext") and int(item.get("extracted_chars", 0)) < 2500:
            pdf_info = data.get("openAccessPdf") or {}
            pdf_url = (pdf_info.get("url") or "").strip()
            if pdf_url:
                print(f"[s2] Extracting full text from PDF: {pdf_url[:80]}", flush=True)
                text = _extract_pdf_text(pdf_url, max_chars=max_chars)
                if len(text) > 500:
                    item["analysis"] = text
                    item["extracted_chars"] = len(text)
                    item["has_fulltext"] = True
                    print(
                        f"[s2] Full text extracted: {len(text):,} chars for "
                        f"\"{(item.get('title') or '')[:60]}\"",
                        flush=True,
                    )
                else:
                    print(f"[s2] PDF extraction yielded too little text ({len(text)} chars)", flush=True)


# ---------------------------------------------------------------------------
# Main entry point — called from run_daily.py
# ---------------------------------------------------------------------------

def enrich_with_s2(
    ranked: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    api_key: str,
    is_seen: Callable[[str], bool],
    max_enrich: int = 60,
) -> Tuple[List[Dict[str, Any]], List[Dict], List[Dict]]:
    """
    Enrich the top `max_enrich` ranked candidates with S2 reference data.

    Steps:
      1. For each of the top candidates, resolve S2 paper ID + fetch references.
      2. Compute s2_reference_score (0–1) and store on the item dict.
      3. Build the shared landscape from ALL enriched items' references.
      4. Surface missed papers from the combined reference pool.

    Returns:
      (enriched_ranked, shared_landscape, missed_surfaces)

    Items beyond max_enrich are returned unchanged (score defaults to 0.0 in ranker).
    """
    to_enrich = ranked[:max_enrich]
    rest = ranked[max_enrich:]

    papers_with_refs: List[Tuple[str, List[Dict]]] = []
    all_refs: List[Dict] = []

    print(f"[s2] Enriching {len(to_enrich)} candidates with Semantic Scholar references…", flush=True)

    for item in to_enrich:
        url   = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()

        paper_id = resolve_paper_id(url, title, api_key)
        if not paper_id:
            item["s2_reference_score"] = 0.0
            continue

        refs = fetch_references(paper_id, api_key)
        item["s2_reference_score"] = score_references(refs, cfg)
        item["s2_paper_id"] = paper_id
        # Store top cited refs on the item so the synthesis LLM has related literature context
        if refs:
            s2_cfg = cfg.get("semantic_scholar") or {}
            top_n = int(s2_cfg.get("top_refs_per_paper", 8))
            item["s2_top_refs"] = top_refs_for_synthesis(refs, top_n=top_n)
            papers_with_refs.append((title, refs))
            all_refs.extend(refs)

        print(
            f"[s2]   {title[:60]}… → score={item['s2_reference_score']:.2f} "
            f"({len(refs)} refs)",
            flush=True,
        )

    shared = build_shared_landscape(papers_with_refs)
    missed = find_missed_surfaces(all_refs, is_seen)

    print(
        f"[s2] Shared landscape: {len(shared)} foundational papers. "
        f"Missed surfaces: {len(missed)}.",
        flush=True,
    )

    return to_enrich + rest, shared, missed

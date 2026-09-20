"""
PubMed collector — queries NCBI E-utilities for recent papers by keyword.

Uses only `requests` (already in requirements.txt).
Returns items in the same format as rss.py so the rest of the pipeline is unchanged.

Config section (config.yaml):
  pubmed:
    enabled: true
    email: "you@example.com"   # required by NCBI usage policy
    search_terms:
      - "protein design machine learning"
      - "diffusion model protein structure"
    lookback_days: 2            # overrides global lookback_hours / 24
    max_results_per_term: 50
    bucket: "protein"
    tags: ["pubmed", "journal"]
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import requests

_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_DEFAULT_TIMEOUT = 20


def _strict_query(term: str) -> str:
    """
    Turn "protein binder design" into (protein[tiab] AND binder[tiab] AND design[tiab]).

    An unfielded query is expanded by PubMed's automatic term mapping (MeSH synonyms, "All Fields"),
    which is why loose hits such as radiograph classifiers or Mendelian-randomisation studies got in.
    Restricting every word to title/abstract keeps the AND semantics but removes that expansion.
    Note: [tiab] does no stemming, so "proteins" will not match "protein"; the relevance gate
    downstream is the safety net, which is why this is opt-in (pubmed.strict_queries).
    """
    words = [w for w in term.replace('"', " ").split() if w]
    if not words:
        return term
    parts = [(f'"{w}"[tiab]' if "-" in w else f"{w}[tiab]") for w in words]
    return "(" + " AND ".join(parts) + ")"


def _esearch(
    query: str,
    *,
    email: str,
    max_results: int,
    retries: int = 3,
) -> List[str]:
    """Return a list of PubMed IDs matching *query*."""
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
        "tool": "openclaw-knowledge-radio",
        "email": email,
    }
    for attempt in range(retries):
        try:
            r = requests.get(
                f"{_EUTILS_BASE}/esearch.fcgi",
                params=params,
                timeout=_DEFAULT_TIMEOUT,
            )
            r.raise_for_status()
            return r.json().get("esearchresult", {}).get("idlist", [])
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return []


def _efetch(
    pmids: List[str],
    *,
    email: str,
    retries: int = 3,
) -> List[Dict[str, Any]]:
    """Fetch article metadata for a list of PubMed IDs via XML."""
    if not pmids:
        return []
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": "openclaw-knowledge-radio",
        "email": email,
    }
    xml_text: Optional[str] = None
    for attempt in range(retries):
        try:
            r = requests.get(
                f"{_EUTILS_BASE}/efetch.fcgi",
                params=params,
                timeout=_DEFAULT_TIMEOUT,
            )
            r.raise_for_status()
            xml_text = r.text
            break
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    if not xml_text:
        return []

    articles = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    for art in root.findall(".//PubmedArticle"):
        try:
            article = _parse_article(art)
            if article:
                articles.append(article)
        except Exception:
            continue
    return articles


def _text(element: Any, path: str, default: str = "") -> str:
    node = element.find(path)
    return (node.text or default).strip() if node is not None and node.text else default


def _parse_article(art: ET.Element) -> Optional[Dict[str, Any]]:
    title = _text(art, ".//ArticleTitle")
    if not title:
        return None

    # Abstract — join all AbstractText blocks
    abstract_parts = []
    for ab in art.findall(".//AbstractText"):
        label = ab.get("Label")
        txt = (ab.text or "").strip()
        if txt:
            abstract_parts.append(f"{label}: {txt}" if label else txt)
    abstract = " ".join(abstract_parts)

    # Journal
    journal = _text(art, ".//Journal/Title") or _text(art, ".//MedlineTA")

    # PMID
    pmid_node = art.find(".//PMID")
    pmid = (pmid_node.text or "").strip() if pmid_node is not None else ""

    # DOI preferred, PMID url fallback
    doi = ""
    for id_node in art.findall(".//ArticleId"):
        if id_node.get("IdType") == "doi":
            doi = (id_node.text or "").strip()
            break
    if doi:
        url = f"https://doi.org/{doi}"
    elif pmid:
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    else:
        return None

    # Authors
    author_names = []
    for author in art.findall(".//Author"):
        last = _text(author, "LastName")
        fore = _text(author, "ForeName") or _text(author, "Initials")
        name = f"{fore} {last}".strip() if fore else last
        if name:
            author_names.append(name)
    authors = ", ".join(author_names) if author_names else ""

    return {
        "pmid": pmid,
        "title": title,
        "abstract": abstract,
        "journal": journal,
        "url": url,
        "authors": authors,
    }


def collect_pubmed_items(
    cfg: Dict[str, Any],
    *,
    lookback_hours: int = 48,
    extra_terms: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Collect PubMed papers by keyword and return pipeline-compatible items.

    Each returned item has the same keys as rss.py items so the rest of the
    pipeline (dedup, rank, script generation) works without changes.
    """
    pubmed_cfg = cfg.get("pubmed", {})
    if not pubmed_cfg.get("enabled", False):
        return []

    email = pubmed_cfg.get("email", "openclaw@example.com")
    search_terms: List[str] = list(pubmed_cfg.get("search_terms", []))
    if extra_terms:
        existing_lower = {t.lower() for t in search_terms}
        for t in extra_terms:
            if t.lower() not in existing_lower:
                search_terms.append(t)
                print(f"[pubmed] Dynamic term from feedback: \"{t}\"", flush=True)
    max_results = int(pubmed_cfg.get("max_results_per_term", 50))
    bucket = pubmed_cfg.get("bucket", "protein")
    tags = list(pubmed_cfg.get("tags", ["pubmed", "journal"]))

    # Date range: n_days back from today
    n_days = pubmed_cfg.get("lookback_days") or max(1, lookback_hours // 24)
    end = datetime.now()
    start = end - timedelta(days=int(n_days) + 1)
    date_filter = (
        f"{start.strftime('%Y/%m/%d')}:{end.strftime('%Y/%m/%d')}[PDAT]"
    )

    out: List[Dict[str, Any]] = []
    seen_pmids: set = set()

    for term in search_terms:
        _q = _strict_query(term) if pubmed_cfg.get("strict_queries", False) else f"({term})"
        query = f"{_q} AND {date_filter}"
        pmids = _esearch(query, email=email, max_results=max_results)
        if not pmids:
            continue

        # Rate-limit: NCBI allows 3 req/s without API key
        time.sleep(0.4)

        articles = _efetch(pmids, email=email)
        for article in articles:
            pmid = article.get("pmid", "")
            if pmid in seen_pmids:
                continue
            seen_pmids.add(pmid)

            abstract = article.get("abstract", "")
            authors = article.get("authors", "")
            journal = article.get("journal", "PubMed")
            one_liner = abstract[:357] + "..." if len(abstract) > 360 else abstract

            out.append(
                {
                    "bucket": bucket,
                    "source": f"PubMed — {journal}",
                    "source_type": "pubmed",
                    "title": article.get("title", ""),
                    "url": article.get("url", ""),
                    "one_liner": one_liner,
                    "tags": tags,
                    "authors": authors,
                }
            )

        # Be polite between search terms
        time.sleep(0.4)

    return out

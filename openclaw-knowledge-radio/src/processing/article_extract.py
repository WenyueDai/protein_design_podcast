from __future__ import annotations

from typing import Optional

from newspaper import Article
import requests
from bs4 import BeautifulSoup


def _extract_with_newspaper(url: str) -> str:
    article = Article(url)
    article.download()
    article.parse()
    return (article.text or "").strip()


def _extract_with_bs4(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
    }
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Remove noisy tags
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()

    # Prefer article/main content when available
    candidates = []
    article_tag = soup.find("article")
    main_tag = soup.find("main")
    if article_tag:
        candidates.append(article_tag.get_text("\n", strip=True))
    if main_tag:
        candidates.append(main_tag.get_text("\n", strip=True))

    # ArXiv abstract fallback
    if "arxiv.org" in url:
        abs_block = soup.find("blockquote", class_="abstract")
        if abs_block:
            candidates.append(abs_block.get_text(" ", strip=True).replace("Abstract:", "").strip())

    # Global fallback
    candidates.append(soup.get_text("\n", strip=True))

    # Return the longest reasonably clean candidate
    best = max((c for c in candidates if c), key=len, default="")
    return best.strip()


def _extract_pdf_via_s2(paper_id: str, api_key: str) -> str:
    """
    Resolve the open-access PDF URL from Semantic Scholar and extract its text.
    Returns '' on any failure.
    """
    try:
        from src.collectors.semantic_scholar import get_open_access_pdf_url
        from src.collectors.semantic_scholar import _extract_pdf_text as _s2_extract_pdf
        pdf_url = get_open_access_pdf_url(paper_id, api_key)
        if not pdf_url:
            return ""
        print(f"[article_extract] S2 PDF fallback: {pdf_url[:80]}", flush=True)
        return _s2_extract_pdf(pdf_url)
    except Exception:
        return ""


def extract_article_text(
    url: str,
    s2_paper_id: Optional[str] = None,
    s2_api_key: Optional[str] = None,
) -> str:
    """
    Extract article text from a URL.

    Falls back to Semantic Scholar open-access PDF if the primary extraction
    yields fewer than 500 characters and s2_paper_id + s2_api_key are provided.
    """
    # 1) newspaper first (often cleaner)
    try:
        txt = _extract_with_newspaper(url)
        if len(txt) >= 800:
            return txt
    except Exception:
        pass

    # 2) bs4 fallback for paywall-ish / structured pages
    try:
        txt = _extract_with_bs4(url)
        if len(txt) >= 500:
            return txt
    except Exception:
        txt = ""

    # 3) S2 open-access PDF fallback (for arXiv/DOI papers with poor web extraction)
    if len(txt) < 500 and s2_paper_id and s2_api_key:
        s2_text = _extract_pdf_via_s2(s2_paper_id, s2_api_key)
        if len(s2_text) > len(txt):
            return s2_text

    return txt

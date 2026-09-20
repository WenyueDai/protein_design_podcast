#!/usr/bin/env python3
"""
process_missed_papers.py
------------------------
Diagnose why a paper was missed by the pipeline and extract topic keywords
from its title to boost similar papers in future rankings.

Reads:   state/missed_papers.json
Writes:  state/missed_papers.json  (marks entries as processed + diagnosis)
         state/boosted_topics.json (accumulates extracted keywords)

Uses only stdlib + PyYAML.  No extra deps.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.processing import relevance as _rel  # noqa: E402

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR   = PACKAGE_DIR / "state"
CONFIG_FILE = PACKAGE_DIR / "config.yaml"

MISSED_FILE    = STATE_DIR / "missed_papers.json"
BOOST_FILE     = STATE_DIR / "boosted_topics.json"
SEEN_FILE      = STATE_DIR / "seen_ids.json"
EXTRA_RSS_FILE = STATE_DIR / "extra_rss_sources.json"

# Domains that host articles from many sources — not informative for RSS check
_PASSTHROUGH_DOMAINS = {"doi.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sha1(url: str) -> str:
    """Matches SeenStore hash in src/utils/dedup.py exactly."""
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def _rss_domains(cfg: Dict[str, Any]) -> Set[str]:
    """Set of lowercase domains from rss_sources[].url in config."""
    domains: Set[str] = set()
    for src in cfg.get("rss_sources") or []:
        d = _domain(src.get("url") or "")
        if d:
            domains.add(d)
    return domains


# ── Diagnosis ─────────────────────────────────────────────────────────────────

def diagnose(
    entry: Dict[str, Any],
    seen_ids: Set[str],
    rss_domains: Set[str],
    excluded_terms: List[str],
) -> str:
    """
    Return one of:
      "already_collected"  — sha1(url) found in seen_ids.json
      "excluded_term"      — a configured excluded_term appears in the title
      "source_not_in_rss"  — URL domain not in any RSS source
      "low_ranking"        — was fetchable but ranked below the item cap
    """
    url   = (entry.get("url") or "").strip()
    title = (entry.get("title") or "").strip()

    # 1. Already collected?
    if url and _sha1(url) in seen_ids:
        return "already_collected"

    # 2. Excluded by term filter?  Uses the same whole-word matcher as run_daily.py (a raw
    #    substring test here would blame "rat" for "Accurate ...", exactly the bug it was hiding).
    if _rel.should_exclude({"title": title}, excluded_terms or []):
        return "excluded_term"

    # 3. Source not in RSS?
    if url:
        d = _domain(url)
        if d and d not in _PASSTHROUGH_DOMAINS and d not in rss_domains:
            return "source_not_in_rss"

    # 4. Fallback — was in an RSS feed but ranked below the cap
    return "low_ranking"


# ── LLM keyword extraction ────────────────────────────────────────────────────

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "in", "for", "to", "is", "are",
    "with", "from", "by", "on", "at", "this", "that", "based", "using",
    "via", "de", "novo", "new", "using", "through", "into", "its",
}

def _heuristic_keywords(title: str) -> List[str]:
    """Extract meaningful words from title as a fallback."""
    words = re.findall(r"[a-zA-Z]{5,}", title.lower())
    seen: Set[str] = set()
    kws: List[str] = []
    for w in words:
        if w not in _STOP_WORDS and w not in seen:
            seen.add(w)
            kws.append(w)
        if len(kws) >= 5:
            break
    return kws


def extract_keywords_llm(title: str, api_key: str) -> List[str]:
    """
    Call OpenRouter to extract 3-5 lowercase topic keywords from a paper title.
    Falls back to heuristic extraction on any failure.
    """
    if not api_key:
        print("[process_missed] No API key — using heuristic keyword extraction.")
        return _heuristic_keywords(title)

    prompt = (
        "Extract 3 to 5 short, specific topic keywords from this paper title. "
        "Return ONLY a JSON array of lowercase keyword strings (2-4 words each). "
        "Focus on the scientific topic, method, or molecule type — not generic words like 'study' or 'analysis'.\n\n"
        f"Title: {title}\n\n"
        "Example output: [\"protein binder\", \"diffusion model\", \"antibody design\"]"
    )

    payload = json.dumps({
        "model": "arcee-ai/trinity-large-preview:free",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 128,
        "temperature": 0.2,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/WenyueDai/openclaw_podcast",
            "X-Title": "openclaw-knowledge-radio",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"].strip()
        # Extract JSON array from response (may have surrounding text)
        m = re.search(r'\[.*?\]', text, re.DOTALL)
        if m:
            kws = json.loads(m.group())
            # Validate: list of strings
            result = [str(k).strip().lower() for k in kws if isinstance(k, str) and k.strip()]
            if result:
                print(f"[process_missed] LLM keywords: {result}")
                return result[:5]
    except Exception as exc:
        print(f"[process_missed] LLM call failed ({exc}), using heuristic fallback.")

    return _heuristic_keywords(title)


# ── RSS feed discovery ────────────────────────────────────────────────────────

# Common feed URL patterns to probe (relative to domain root)
_FEED_PATHS = [
    "/feed",
    "/rss",
    "/feed.xml",
    "/rss.xml",
    "/atom.xml",
    "/feeds/posts/default",
    "/?feed=rss2",
    "/index.xml",
]

_FEED_CONTENT_TYPES = {
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
}


def _probe_url(url: str, timeout: int = 8) -> tuple[bool, str]:
    """
    HEAD-then-GET a URL.  Returns (is_feed, final_url).
    is_feed=True if content-type looks like XML/RSS/Atom.
    """
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "FeedProbe/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "").lower().split(";")[0].strip()
            if any(ft in ct for ft in _FEED_CONTENT_TYPES):
                return True, r.url
    except Exception:
        pass
    return False, ""


def _extract_feed_from_html(url: str, timeout: int = 10) -> str:
    """
    Fetch a page and look for <link rel="alternate" type="application/rss+xml"> or atom+xml.
    Returns the discovered feed URL, or "".
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FeedProbe/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # Read up to 64 KB — the <head> section is always near the top
            raw = r.read(65536).decode("utf-8", errors="ignore")
        # Look for RSS/Atom link tags
        for m in re.finditer(
            r'<link[^>]+rel=["\']alternate["\'][^>]+'
            r'type=["\']application/(rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)["\']',
            raw, re.IGNORECASE
        ):
            href = m.group(2).strip()
            if href:
                return urllib.parse.urljoin(url, href)
        # Also try reversed attribute order (href before type)
        for m in re.finditer(
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]+'
            r'type=["\']application/(rss|atom)\+xml["\']',
            raw, re.IGNORECASE
        ):
            href = m.group(1).strip()
            if href:
                return urllib.parse.urljoin(url, href)
    except Exception:
        pass
    return ""


def discover_rss_feed(paper_url: str) -> str:
    """
    Try to find an RSS/Atom feed for the domain of paper_url.
    Returns the feed URL if found, otherwise "".
    """
    parsed = urllib.parse.urlparse(paper_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # 1. Check the article page itself for a <link rel="alternate"> tag
    feed = _extract_feed_from_html(paper_url)
    if feed:
        print(f"[process_missed] Feed found via <link> tag: {feed}")
        return feed

    # 2. Probe common feed paths on the domain root
    for path in _FEED_PATHS:
        candidate = base + path
        ok, final = _probe_url(candidate)
        if ok:
            print(f"[process_missed] Feed found via probe: {final or candidate}")
            return final or candidate

    return ""


def _domain_in_extra_rss(domain: str, extra_sources: List[Dict[str, Any]]) -> bool:
    """Return True if this domain already has an entry in extra_rss_sources."""
    for src in extra_sources:
        if _domain(src.get("url") or "") == domain:
            return True
    return False


def _make_extra_rss_source(feed_url: str, paper_url: str) -> Dict[str, Any]:
    """Build a source entry (compatible with rss_sources config format)."""
    parsed = urllib.parse.urlparse(feed_url)
    name = parsed.netloc.lstrip("www.").split(".")[0].capitalize()
    return {
        "name": name,
        "url": feed_url,
        "tags": ["journal", "discovered"],
        "bucket": "protein",
        "discovered_from": paper_url,
    }


# ── Notion deep-dive stub creation ───────────────────────────────────────────

_NOTION_API = "https://api.notion.com/v1/pages"
# Default Deep Dive Notes database (same as sync_notion_notes.py)
_DEFAULT_NOTION_DB = "3165f58ea8c280498f72c770028aec0d"


def _ensure_source_property(api_key: str, database_id: str) -> None:
    """Add a 'Source' select property to the database if it doesn't exist yet."""
    payload = json.dumps({"properties": {"Source": {"select": {}}}}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.notion.com/v1/databases/{database_id}",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def create_notion_missed_stub(entry: Dict[str, Any], api_key: str, database_id: str) -> str:
    """
    Create a Notion deep-dive stub for a user-submitted missed paper.
    Returns the new Notion page ID, or "" on failure.
    """
    title      = (entry.get("title") or "Untitled").strip()
    url        = (entry.get("url") or "").strip()
    date       = entry.get("date_submitted") or "2026-01-01"
    diagnosis  = entry.get("diagnosis") or "pending"
    kws        = entry.get("keywords_added") or []

    _DIAG_LABELS = {
        "low_ranking":       "ranked below episode cap",
        "source_not_in_rss": "source domain not in RSS feeds",
        "excluded_term":     "title matched an excluded term",
        "already_collected": "already collected in a previous episode",
    }
    diag_text = _DIAG_LABELS.get(diagnosis, diagnosis)

    note_lines = [f"📬 Submitted as missed paper ({date})", f"📊 Diagnosis: {diag_text}"]
    if kws:
        note_lines.append(f"🏷️ Keywords boosted: {', '.join(kws)}")
    note_text = "\n".join(note_lines)

    children: List[Dict[str, Any]] = [
        {
            "object": "block", "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "📬"},
                "rich_text": [{"type": "text", "text": {"content": note_text[:2000]}}],
                "color": "blue_background",
            },
        },
        {
            "object": "block", "type": "divider", "divider": {},
        },
        {
            "object": "block", "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "Deep Dive Notes"}}],
            },
        },
        {
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": ""}}]},
        },
    ]
    if url:
        children.insert(1, {"object": "block", "type": "bookmark", "bookmark": {"url": url}})

    body_data = {
        "parent": {"database_id": database_id},
        "properties": {
            "Name":   {"title":     [{"text": {"content": title[:2000]}}]},
            "Date":   {"date":      {"start": date}},
            "Note":   {"rich_text": [{"text": {"content": note_text[:2000]}}]},
            "Source": {"select":    {"name": "Missed Paper"}},
        },
        "children": children,
    }

    payload = json.dumps(body_data).encode("utf-8")
    req = urllib.request.Request(
        _NOTION_API,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        page_id = result.get("id", "")
        print(f"[process_missed] Notion stub created: {page_id[:8]}… for '{title[:60]}'")
        return page_id
    except Exception as exc:
        print(f"[process_missed] Notion creation failed: {exc}")
        return ""


# ── Keyword merging ───────────────────────────────────────────────────────────

def _merge_keywords(existing: List[str], new_kws: List[str]) -> Tuple[List[str], List[str]]:
    """
    Case-insensitive dedup merge.
    Returns (merged_list, actually_added_list).
    """
    existing_lower = {k.lower() for k in existing}
    added: List[str] = []
    merged = list(existing)
    for kw in new_kws:
        kw_lower = kw.lower()
        if kw_lower not in existing_lower:
            merged.append(kw_lower)
            existing_lower.add(kw_lower)
            added.append(kw_lower)
    return merged, added


# ── Main ──────────────────────────────────────────────────────────────────────

def process_missed_papers() -> None:
    # Load config
    cfg: Dict[str, Any] = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    # Load seen_ids
    seen_ids: Set[str] = set()
    if SEEN_FILE.exists():
        try:
            seen_ids = set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass

    rss_domains    = _rss_domains(cfg)
    excluded_terms = cfg.get("excluded_terms") or []

    # Load missed_papers.json
    papers: List[Dict[str, Any]] = []
    if MISSED_FILE.exists():
        try:
            papers = json.loads(MISSED_FILE.read_text(encoding="utf-8"))
        except Exception:
            papers = []

    # Load boosted_topics.json
    boosted: List[str] = []
    if BOOST_FILE.exists():
        try:
            boosted = json.loads(BOOST_FILE.read_text(encoding="utf-8"))
        except Exception:
            boosted = []

    api_key      = os.environ.get("OPENROUTER_API_KEY", "")
    notion_key   = os.environ.get("NOTION_API_KEY", "")
    notion_db    = os.environ.get("NOTION_DATABASE_ID", _DEFAULT_NOTION_DB)

    if notion_key:
        _ensure_source_property(notion_key, notion_db)

    # Load extra_rss_sources.json
    extra_rss: List[Dict[str, Any]] = []
    if EXTRA_RSS_FILE.exists():
        try:
            extra_rss = json.loads(EXTRA_RSS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    changed = False
    rss_changed = False
    for paper in papers:
        if paper.get("processed"):
            continue

        title = (paper.get("title") or "").strip()
        if not title:
            paper["processed"] = True
            changed = True
            continue

        print(f"[process_missed] Processing: {title[:80]}")

        # Diagnose
        diag = diagnose(paper, seen_ids, rss_domains, excluded_terms)
        paper["diagnosis"] = diag
        print(f"[process_missed] Diagnosis: {diag}")

        # Extract keywords for low_ranking and source_not_in_rss entries
        keywords_added: List[str] = []
        if diag in ("low_ranking", "source_not_in_rss"):
            new_kws = extract_keywords_llm(title, api_key)
            boosted, keywords_added = _merge_keywords(boosted, new_kws)
            if keywords_added:
                print(f"[process_missed] Added keywords: {keywords_added}")

        # Try RSS feed discovery for source_not_in_rss
        rss_feed_found: str = ""
        if diag == "source_not_in_rss":
            url = (paper.get("url") or "").strip()
            if url:
                d = _domain(url)
                if d and not _domain_in_extra_rss(d, extra_rss):
                    feed_url = discover_rss_feed(url)
                    if feed_url:
                        src_entry = _make_extra_rss_source(feed_url, url)
                        extra_rss.append(src_entry)
                        rss_feed_found = feed_url
                        rss_changed = True
                        print(f"[process_missed] Saved new RSS source: {feed_url}")
                    else:
                        print(f"[process_missed] No RSS feed found for domain: {d}")

        paper["keywords_added"] = keywords_added
        paper["rss_feed_found"] = rss_feed_found

            # Create Notion deep-dive stub (skip if already created or Notion not configured)
        if notion_key and not paper.get("notion_page_id"):
            page_id = create_notion_missed_stub(paper, notion_key, notion_db)
            if page_id:
                paper["notion_page_id"] = page_id

        paper["processed"] = True
        changed = True

    if not changed:
        print("[process_missed] No unprocessed entries found.")
        return

    # Write results
    MISSED_FILE.write_text(json.dumps(papers, indent=2, ensure_ascii=False), encoding="utf-8")
    BOOST_FILE.write_text(json.dumps(boosted, indent=2, ensure_ascii=False), encoding="utf-8")
    if rss_changed:
        EXTRA_RSS_FILE.write_text(json.dumps(extra_rss, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[process_missed] extra_rss_sources now has {len(extra_rss)} source(s).")
    print(f"[process_missed] Done. boosted_topics now has {len(boosted)} keyword(s).")


if __name__ == "__main__":
    process_missed_papers()

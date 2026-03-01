import json
from pathlib import Path
from typing import Any, Dict, List, Set


def _load_feedback(cfg: Dict[str, Any]) -> tuple:
    """
    Load state/feedback.json.
    Returns (liked_urls: set, liked_sources: Dict[str,int], liked_keywords: List[str]).
    Supports both old format (list of URL strings) and new format (list of {url,source,title} objects).
    """
    import re as _re
    _STOP = {"the","a","an","and","or","of","in","for","to","is","are","with","from",
             "by","on","at","this","that","based","using","via","de","novo","new"}
    state_dir = Path(__file__).resolve().parent.parent.parent / "state"
    fb_file = state_dir / "feedback.json"
    if not fb_file.exists():
        return set(), {}, []
    try:
        data = json.loads(fb_file.read_text(encoding="utf-8"))
        liked_urls: set = set()
        liked_sources: Dict[str, int] = {}
        word_counts: Dict[str, int] = {}
        for entries in data.values():
            for entry in (entries or []):
                if isinstance(entry, str):
                    liked_urls.add(entry)
                elif isinstance(entry, dict):
                    url = (entry.get("url") or "").strip()
                    src = (entry.get("source") or "").strip()
                    title = (entry.get("title") or "").strip()
                    if url:
                        liked_urls.add(url)
                    if src:
                        liked_sources[src] = liked_sources.get(src, 0) + 1
                    # Extract meaningful title words (length >= 5, not stop words)
                    for w in _re.findall(r"[a-zA-Z]{5,}", title.lower()):
                        if w not in _STOP:
                            word_counts[w] = word_counts.get(w, 0) + 1
        # Keep words that appear in ≥1 liked title
        liked_keywords = [w for w, c in word_counts.items() if c >= 1]
        return liked_urls, liked_sources, liked_keywords
    except Exception:
        return set(), {}, []


def _feedback_priority(it: Dict[str, Any], liked_urls: set,
                       liked_sources: Dict[str, int], liked_keywords: List[str]) -> int:
    """
    0 = strong match (source liked before OR title keyword match)
    1 = no match
    Lower is better — these items bubble up within their journal/bucket tier.
    """
    src = (it.get("source") or "").strip()
    if src and liked_sources.get(src, 0) > 0:
        return 0
    if liked_keywords:
        hay = " ".join([it.get("title") or "", it.get("one_liner") or ""]).lower()
        if any(kw in hay for kw in liked_keywords):
            return 0
    return 1


# -----------------------------
# Helpers
# -----------------------------
def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _tags_lower(it: Dict[str, Any]) -> List[str]:
    tags = it.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return [str(t).strip().lower() for t in tags if str(t).strip()]


def _has_fulltext(it: Dict[str, Any], threshold: int) -> bool:
    """
    Keep compatibility with your existing extracted_chars scheme.
    """
    extracted_chars = int(it.get("extracted_chars", 0) or 0)
    return extracted_chars >= threshold


# -----------------------------
# Priority knobs (minimal, config-optional)
# -----------------------------
def _absolute_author_priority(it: Dict[str, Any], cfg: Dict[str, Any]) -> int:
    """
    Lower is better.

    Your new rule: selected researchers / author feeds are ABSOLUTE priority.

    Default behavior (no config changes needed):
    - If item has tag 'author' -> absolute priority (0)
    - If source contains 'google scholar' -> absolute priority (0)

    Optional config (does not break if missing):
      ranking:
        absolute_sources:
          - "Frances Arnold (Google Scholar)"
          - "David Baker (arXiv)"
        absolute_source_substrings:
          - "google scholar"
          - "david baker"
    """
    tags = _tags_lower(it)
    src = _norm(it.get("source") or "")

    if "author" in tags:
        return 0
    if "google scholar" in src:
        return 0

    r = (cfg.get("ranking") or {}) if isinstance(cfg, dict) else {}
    abs_sources = r.get("absolute_sources") or []
    abs_sub = r.get("absolute_source_substrings") or []

    src_raw = (it.get("source") or "").strip()
    # exact/contains match on configured names
    for name in abs_sources:
        if name and _norm(name) in src:
            return 0
        if name and name.strip() == src_raw:
            return 0
    for sub in abs_sub:
        if sub and _norm(sub) in src:
            return 0

    return 1


def _absolute_title_priority(it: Dict[str, Any], cfg: Dict[str, Any]) -> int:
    """
    0 if the item title contains any absolute_title_keywords, 1 otherwise.
    Gives landmark papers (AlphaFold, RoseTTAFold, etc.) the same priority
    tier as tracked author feeds, regardless of source.
    """
    r = (cfg.get("ranking") or {}) if isinstance(cfg, dict) else {}
    kws = r.get("absolute_title_keywords") or []
    if not kws:
        return 1
    hay = _norm(it.get("title") or "")
    for kw in kws:
        if _norm(kw) in hay:
            return 0
    return 1


def _journal_quality_priority(it: Dict[str, Any], cfg: Dict[str, Any]) -> int:
    """
    Lower is better.

    Rank by trusted sources / journal quality AFTER absolute author feeds.
    This replaces your previous "fulltext first" dominance.

    Optional override via config:
      ranking:
        source_priority_rules:
          - {contains: "nature biotechnology", priority: 1}
          - {contains: "nature chemical biology", priority: 1}
          - {contains: "pnas", priority: 2}
          - {contains: "nature (main journal)", priority: 2}
          - {contains: "arxiv", priority: 5}
          - {contains: "sciencedirect", priority: 6}
    """
    src = _norm(it.get("source") or "")
    tags = _tags_lower(it)

    # Config override (if provided)
    r = (cfg.get("ranking") or {}) if isinstance(cfg, dict) else {}
    rules = r.get("source_priority_rules") or []
    for rule in rules:
        try:
            contains = _norm(rule.get("contains", ""))
            pr = int(rule.get("priority"))
        except Exception:
            continue
        if contains and contains in src:
            return pr

    # Default heuristic mapping (works with your feed list)
    # 1 = best
    if "nature biotechnology" in src:
        return 1
    if "nature chemical biology" in src:
        return 1
    if src.startswith("pnas"):
        return 2
    if "nature (main journal)" in src or (src.startswith("nature") and "news" not in src):
        return 3

    # Good but preprint / broad
    if "arxiv" in src:
        return 5

    # Other journals
    if "journal" in tags:
        return 6

    # News last
    if "news" in tags or "science-news" in tags:
        return 9

    # Default middle
    return 7


_BOOST_FILE = Path(__file__).resolve().parent.parent.parent / "state" / "boosted_topics.json"


def _missed_paper_keyword_priority(it: Dict[str, Any]) -> int:
    """
    ABSOLUTE TOP TIER (tier 0).
    0 if the item matches any keyword extracted from user-submitted missed papers
    (state/boosted_topics.json). These represent ground-truth relevance — papers
    the user actively sought out that the pipeline failed to collect.
    1 otherwise.
    """
    try:
        missed_kws = json.loads(_BOOST_FILE.read_text(encoding="utf-8")) if _BOOST_FILE.exists() else []
    except Exception:
        missed_kws = []
    if not missed_kws:
        return 1
    hay = " ".join([
        (it.get("title") or ""),
        (it.get("one_liner") or ""),
        (it.get("snippet") or ""),
        (it.get("source") or ""),
    ]).lower()
    for kw in (k.lower() for k in missed_kws):
        if kw in hay:
            return 0
    return 1


def _topic_keyword_priority(it: Dict[str, Any], cfg: Dict[str, Any]) -> int:
    """
    0 if the item title/snippet matches a topic_boost_keyword from config.yaml, 1 otherwise.
    This makes on-topic items float above off-topic items within the same tier.
    Only uses config.yaml keywords — missed paper keywords are handled separately at tier 0.
    """
    cfg_kws = (cfg.get("ranking") or {}).get("topic_boost_keywords") or []
    if not cfg_kws:
        return 0  # no config = no penalty
    all_boost_kws = set(k.lower() for k in cfg_kws)
    hay = " ".join([
        (it.get("title") or ""),
        (it.get("one_liner") or ""),
        (it.get("snippet") or ""),
        (it.get("source") or ""),
    ]).lower()
    for kw in all_boost_kws:
        if kw in hay:
            return 0
    return 1


def _bucket_priority(it: Dict[str, Any]) -> int:
    """
    Keep your existing behavior: steer toward research over general news.
    Lower is better.
    """
    bucket = _norm(it.get("bucket") or "")
    return {
        "protein": 0,
        "journal": 1,
        "ai_bio": 2,
        "daily": 4,   # daily knowledge, keep but not dominating
        "news": 5,
    }.get(bucket, 3)


# -----------------------------
# Main entrypoint (MUST keep signature + output behavior)
# -----------------------------
def rank_and_limit(items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Input/Output compatible with your current pipeline.

    New ranking policy (lower is better):
    0) ABSOLUTE: key researchers / author feeds (tag 'author' or Google Scholar, etc.)
    1) ABSOLUTE: landmark paper titles (AlphaFold, RoseTTAFold, etc.)
    2) Missed paper keywords (topics extracted from user-submitted missed papers)
    3) On-topic keywords from config (topic_boost_keywords)
    4) Journal/source quality (Nature family, PNAS, etc.)
    5) Bucket steering (protein/journal/ai_bio before news)
    6) Feedback boost (liked sources / keywords) — lighter weight, avoids selection bias
    7) Fulltext as a small tie-breaker
    8) Longer extracted text as tie-breaker
    """
    # Limits (keep identical keys / defaults)
    lim = cfg.get("limits", {}) if isinstance(cfg, dict) else {}
    max_total = int(lim.get("max_items_total", 40))
    max_protein = int(lim.get("max_items_protein", 25))
    max_daily = int(lim.get("max_items_daily_knowledge", 2))

    # Fulltext threshold (keep compatibility)
    FULLTEXT_THRESHOLD = int((cfg.get("fulltext_threshold") if isinstance(cfg, dict) else None) or 1200)

    # Load user feedback — boosts papers from liked sources/topics
    liked_urls, liked_sources, liked_keywords = _load_feedback(cfg)
    if liked_sources or liked_keywords:
        print(f"[rank] Feedback: boosting {len(liked_sources)} source(s), {len(liked_keywords)} keyword(s)", flush=True)

    def rank_key(it: Dict[str, Any]):
        extracted_chars = int(it.get("extracted_chars", 0) or 0)
        has_fulltext = 1 if _has_fulltext(it, FULLTEXT_THRESHOLD) else 0
        return (
            _absolute_author_priority(it, cfg),      # 0) ABSOLUTE: researchers / author feeds
            _absolute_title_priority(it, cfg),       # 1) ABSOLUTE: landmark titles (AlphaFold etc.)
            _missed_paper_keyword_priority(it),      # 2) missed paper keywords (user ground truth)
            _topic_keyword_priority(it, cfg),        # 3) config topic keywords
            _journal_quality_priority(it, cfg),      # 4) journal quality
            _bucket_priority(it),                    # 5) research buckets
            _feedback_priority(it, liked_urls, liked_sources, liked_keywords),  # 6) feedback (lighter)
            -has_fulltext,                           # 7) fulltext bonus
            -extracted_chars,                        # 8) longer text tie-break
        )

    ranked = sorted(items, key=rank_key)

    # Per-source caps: named overrides + a default cap for all other news sources
    source_caps: Dict[str, int] = lim.get("source_caps") or {}
    default_news_cap: int = int(lim.get("max_items_per_news_source", 999))
    _NEWS_BUCKETS = {"news"}
    _NEWS_TAGS = {"news", "science-news", "industry"}

    def _is_news_source(it: Dict[str, Any]) -> bool:
        if it.get("bucket") in _NEWS_BUCKETS:
            return True
        tags = set(_tags_lower(it))
        return bool(tags & _NEWS_TAGS)

    source_counts: Dict[str, int] = {}
    capped: List[Dict[str, Any]] = []
    for it in ranked:
        src = (it.get("source") or "").strip()
        if src in source_caps:
            cap = source_caps[src]
        elif _is_news_source(it):
            cap = default_news_cap
        else:
            cap = 999
        count = source_counts.get(src, 0)
        if count >= cap:
            continue
        source_counts[src] = count + 1
        capped.append(it)
    ranked = capped

    # Bucket quotas
    protein = [x for x in ranked if (x.get("bucket") == "protein")]
    daily = [x for x in ranked if (x.get("bucket") == "daily")]
    others = [x for x in ranked if x.get("bucket") not in ("protein", "daily")]

    protein = protein[:max_protein]
    daily = daily[:max_daily]

    merged = protein + others + daily
    return merged[:max_total]

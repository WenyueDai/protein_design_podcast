import json
import re
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from src.processing import relevance as _rel


def _load_feedback(cfg: Dict[str, Any]) -> tuple:
    """
    Load state/feedback.json with exponential time-decay.
    Returns (liked_urls: set, liked_sources: Dict[str,float], liked_keyword_counts: Dict[str,float]).
    liked_keyword_counts maps word → decay-weighted sum of liked titles containing it.
    Supports both old format (list of URL strings) and new format (list of {url,source,title} objects).

    Half-life: configurable via ranking.feedback_halflife_days (default 14 days).
    Weight per entry = 0.5 ** (days_ago / halflife_days).
    Recent clicks count fully; clicks from 14 days ago count 50%; 28 days ago → 25%.
    This lets your interests drift naturally — stop clicking a topic and it fades out.
    """
    import re as _re
    from datetime import date as _date
    _STOP = {"the","a","an","and","or","of","in","for","to","is","are","with","from",
             "by","on","at","this","that","based","using","via","de","novo","new"}
    state_dir = Path(__file__).resolve().parent.parent.parent / "state"
    fb_file = state_dir / "feedback.json"
    if not fb_file.exists():
        return set(), {}, {}

    r = (cfg.get("ranking") or {}) if isinstance(cfg, dict) else {}
    halflife_days = float(r.get("feedback_halflife_days", 14) or 14)
    today = _date.today()

    try:
        data = json.loads(fb_file.read_text(encoding="utf-8"))
        liked_urls: set = set()
        liked_sources: Dict[str, float] = {}
        word_counts: Dict[str, float] = {}
        for date_key, entries in data.items():
            # Compute decay weight for this date's entries
            try:
                entry_date = _date.fromisoformat(date_key)
                days_ago = max((today - entry_date).days, 0)
                weight = 0.5 ** (days_ago / halflife_days)
            except (ValueError, TypeError):
                weight = 1.0  # unknown date key → no decay
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
                        liked_sources[src] = liked_sources.get(src, 0.0) + weight
                    # Extract meaningful title words (length >= 5, not stop words)
                    for w in _re.findall(r"[a-zA-Z]{5,}", title.lower()):
                        if w not in _STOP:
                            word_counts[w] = word_counts.get(w, 0.0) + weight
        return liked_urls, liked_sources, word_counts
    except Exception:
        return set(), {}, {}


def _feedback_score(it: Dict[str, Any], liked_urls: set,
                    liked_sources: Dict[str, float],
                    liked_keyword_counts: Dict[str, float]) -> float:
    """
    Graded feedback score with time-decay.  Lower is better (more negative = stronger boost).

    Source signal  — decay-weighted click count for this source, capped at -5.
    Keyword signal — each matching keyword contributes its decay-weighted count,
                     capped at -3 per keyword, -5 total.

    Range: -10 (very strong match) … 0 (no feedback overlap).
    Weights are floats after time-decay, so scores are continuous.

    Sits at tier 4, before journal quality, so the more you click a
    source/topic the more it rises regardless of which journal published it.
    """
    score = 0

    # Source boost: frequency-weighted
    src = (it.get("source") or "").strip()
    src_count = liked_sources.get(src, 0)
    if src_count > 0:
        score -= min(src_count, 5)

    # Keyword boost: frequency-weighted
    if liked_keyword_counts:
        hay = " ".join([it.get("title") or "", it.get("one_liner") or ""]).lower()
        kw_total = 0
        for kw, count in liked_keyword_counts.items():
            if kw in hay:
                kw_total -= min(count, 3)   # cap per keyword at -3
        score += max(kw_total, -5)          # cap keyword contribution at -5

    return max(score, -10)                  # overall floor


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
def _is_researcher_feed(it: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """
    True if item comes from a tracked researcher arXiv feed.
    Researcher feeds have tag 'author' AND '(arxiv)' in the source name,
    or match absolute_source_substrings in config.
    Blogs have tag 'author' but no arXiv in source name → not researcher feeds.
    """
    tags = _tags_lower(it)
    src = _norm(it.get("source") or "")
    src_raw = (it.get("source") or "").strip()

    if "author" in tags and ("arxiv" in src or "biorxiv" in src):
        return True
    if "google scholar" in src:
        return True

    r = (cfg.get("ranking") or {}) if isinstance(cfg, dict) else {}
    for name in (r.get("absolute_sources") or []):
        if name and (_norm(name) in src or name.strip() == src_raw):
            return True
    for sub in (r.get("absolute_source_substrings") or []):
        if sub and _norm(sub) in src:
            return True
    return False


def _is_blog_feed(it: Dict[str, Any]) -> bool:
    """
    True if item comes from a tracked blog/substack (author tag, no arXiv in source).
    """
    tags = _tags_lower(it)
    src = _norm(it.get("source") or "")
    return "author" in tags and "arxiv" not in src


def _absolute_author_priority(it: Dict[str, Any], cfg: Dict[str, Any]) -> int:
    """
    Tier 0a (0): absolute top authors — guaranteed top-5 deep-dive.
    Tier 0b (1): other tracked researcher arXiv/bioRxiv feeds — hoisted above journals.
    Tier 2: everything else.
    """
    r = (cfg.get("ranking") or {}) if isinstance(cfg, dict) else {}
    top_subs = r.get("absolute_top_author_substrings") or []
    src = _norm(it.get("source") or "")
    for sub in top_subs:
        if sub and _norm(sub) in src:
            return 0
    return 1 if _is_researcher_feed(it, cfg) else 2


def _absolute_blog_priority(it: Dict[str, Any]) -> int:
    """Tier 1: tracked blog/substack sources. Lower is better."""
    return 0 if _is_blog_feed(it) else 1


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
    title = it.get("title") or ""
    for kw in kws:
        if _title_has_keyword(title, kw):
            return 0
    return 1


def _title_has_keyword(title: str, kw: str) -> bool:
    """
    Keyword match for landmark titles.  Unlike a raw substring test this does not let
    "Boltz" hit "Boltzmann" or "Chroma" hit "chromatin", but still accepts version suffixes
    ("AlphaFold3", "AlphaFold-Multimer", "OpenFold-3") and simple plurals.
    """
    words = [re.escape(w) for w in re.split(r"[\s\-]+", (kw or "").strip().lower()) if w]
    if not words:
        return False
    rx = r"(?<![A-Za-z])" + r"[\s\-]+".join(words) + r"(?:s|es)?(?![A-Za-z])"
    return re.search(rx, title or "", re.I) is not None


# -----------------------------
# Topical relevance gate
# -----------------------------
LAST_GATE_STATS: Dict[str, Any] = {"dropped": 0, "kept": 0, "dropped_titles": []}


def is_protected(it: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """
    Sources the owner explicitly asked for bypass the minimum relevance gate:
    tracked researcher arXiv/bioRxiv feeds and landmark-title matches (AlphaFold, ...).
    Blogs/substacks are NOT protected: several off-topic posts (AGI, Cybersyn) came in that way.
    """
    return _is_researcher_feed(it, cfg) or _absolute_title_priority(it, cfg) == 0


def apply_relevance_gate(
    items: List[Dict[str, Any]], cfg: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Split items into (kept, dropped) using src/processing/relevance.py.
    Sets it["relevance"] (float) on every scored item.  Pre-built context items
    (wiki_context / daily knowledge) are never gated.
    """
    th = _rel.thresholds(cfg)
    if not th["enabled"]:
        return list(items), []
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for it in items:
        if it.get("kind") == "wiki_context" or it.get("bucket") == "daily":
            kept.append(it)
            continue
        if _rel.passes_gate(it, cfg, protected=is_protected(it, cfg)):
            kept.append(it)
        else:
            dropped.append(it)
    return kept, dropped


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


def _relevance_bin(it: Dict[str, Any]) -> int:
    """
    Coarse relevance tier, lower is better: <8 -> 0, 8-11 -> -1, 12-15 -> -2, 16+ -> -3 ... capped.
    Bins (not the raw score) so feedback / journal quality still break ties inside a bin.
    Items without a score (gate disabled, context items) sit in the middle bin.
    """
    r = it.get("relevance")
    if r is None:
        return 0
    try:
        return -min(4, max(0, int(float(r) // 4) - 1))
    except (TypeError, ValueError):
        return 0


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
    0a) ABSOLUTE: absolute_top_author_substrings — guaranteed top-5 deep-dive
    0b) ABSOLUTE: other tracked researcher arXiv/bioRxiv feeds
    1) ABSOLUTE: tracked blog/substack sources (tag 'author', non-arXiv)
    2) ABSOLUTE: landmark paper titles (AlphaFold, RoseTTAFold, etc.)
    3) Missed paper keywords (topics extracted from user-submitted missed papers)
    4) Graded feedback score (time-decayed; liked sources/keywords compound over time)
    5) On-topic keywords from config (topic_boost_keywords)
    6) Journal/source quality (Nature family, PNAS, etc.)
    7) Bucket steering (protein/journal/ai_bio before news)
    8) S2 reference groundedness score
    9) S2 influential citation velocity (how many citers build heavily on this paper)
    10) Fulltext as a small tie-breaker
    11) Longer extracted text as tie-breaker
    """
    # Relevance gate first: nothing off-topic may consume a slot, however good its source tier.
    items, _gate_dropped = apply_relevance_gate(items, cfg)
    LAST_GATE_STATS.update(
        kept=len(items),
        dropped=len(_gate_dropped),
        dropped_titles=[(d.get("title") or "")[:120] for d in _gate_dropped[:50]],
    )
    if _gate_dropped:
        print(f"[rank] relevance gate dropped {len(_gate_dropped)} off-topic item(s), kept {len(items)}", flush=True)

    # Limits (keep identical keys / defaults)
    lim = cfg.get("limits", {}) if isinstance(cfg, dict) else {}
    max_total = int(lim.get("max_items_total", 40))
    max_protein = int(lim.get("max_items_protein", 25))
    max_daily = int(lim.get("max_items_daily_knowledge", 2))

    # Fulltext threshold (keep compatibility)
    FULLTEXT_THRESHOLD = int((cfg.get("fulltext_threshold") if isinstance(cfg, dict) else None) or 1200)

    # Load user feedback — boosts papers from liked sources/topics (time-decayed)
    liked_urls, liked_sources, liked_keyword_counts = _load_feedback(cfg)
    if liked_sources or liked_keyword_counts:
        top_sources = sorted(liked_sources.items(), key=lambda x: -x[1])[:3]
        top_kws = sorted(liked_keyword_counts.items(), key=lambda x: -x[1])[:5]
        print(f"[rank] Feedback (decay-weighted): {len(liked_sources)} source(s) "
              f"({', '.join(f'{s}×{n:.1f}' for s,n in top_sources)}), "
              f"{len(liked_keyword_counts)} keyword(s) "
              f"({', '.join(f'{k}×{n:.1f}' for k,n in top_kws)})", flush=True)

    def rank_key(it: Dict[str, Any]):
        extracted_chars = int(it.get("extracted_chars", 0) or 0)
        has_fulltext = 1 if _has_fulltext(it, FULLTEXT_THRESHOLD) else 0
        # s2_reference_score: 0.0–1.0; higher = more protein-design-grounded refs.
        # Negated so higher score → lower rank key → better position.
        s2_score = -round(float(it.get("s2_reference_score", 0.0)) * 10)
        # influentialCitationCount: how many citers build heavily on this paper.
        # A 3-month paper with 40 influential citations > a 3-year paper with 400 total.
        s2_influential = -int(it.get("s2_influential_citation_count", 0) or 0)
        return (
            _absolute_author_priority(it, cfg),      # 0) ABSOLUTE: researcher arXiv feeds
            _absolute_blog_priority(it),             # 1) ABSOLUTE: blogs/substacks
            _absolute_title_priority(it, cfg),       # 2) ABSOLUTE: landmark titles (AlphaFold etc.)
            _relevance_bin(it),                      # 2b) topical relevance (coarse bins, before source tier)
            _missed_paper_keyword_priority(it),      # 3) missed paper keywords (user ground truth)
            _feedback_score(it, liked_urls, liked_sources, liked_keyword_counts),  # 4) graded feedback
            _topic_keyword_priority(it, cfg),        # 5) config topic keywords
            _journal_quality_priority(it, cfg),      # 6) journal quality
            _bucket_priority(it),                    # 7) research buckets
            s2_score,                                # 8) S2 reference groundedness
            s2_influential,                          # 9) influential citation velocity
            -has_fulltext,                           # 10) fulltext bonus
            -extracted_chars,                        # 11) longer text tie-break
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

    # Hoist absolute-priority items (tier 0: researcher feeds, tier 1: blogs) to the front
    # so they are never buried behind the protein bucket flood.
    def _is_top_priority(it: Dict[str, Any]) -> bool:
        return _is_researcher_feed(it, cfg) or _is_blog_feed(it)

    top = [it for it in ranked if _is_top_priority(it)]
    rest = [it for it in ranked if not _is_top_priority(it)]

    # Bucket quotas applied to the remaining items only
    protein = [x for x in rest if (x.get("bucket") == "protein")]
    daily = [x for x in rest if (x.get("bucket") == "daily")]
    others = [x for x in rest if x.get("bucket") not in ("protein", "daily")]

    protein = protein[:max_protein]
    daily = daily[:max_daily]

    merged = top + protein + others + daily
    return merged[:max_total]

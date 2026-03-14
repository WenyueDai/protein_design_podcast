from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple
from datetime import datetime, time

import yaml

from src.utils.timeutils import load_tz, now_local_date, iso_now_local
from src.utils.io import ensure_dir, write_jsonl, write_text
from src.utils.dedup import SeenStore
from src.collectors.rss import collect_rss_items
from src.collectors.daily_knowledge import collect_daily_knowledge_items
from src.collectors.wiki_context import collect_wiki_context_items
from src.collectors.pubmed import collect_pubmed_items
from src.collectors.biorxiv_authors import collect_biorxiv_author_items
from src.collectors.biorxiv_keywords import collect_biorxiv_keyword_items
from src.processing.rank import rank_and_limit
from src.processing.script_llm import (
    build_podcast_script_llm_chunked_with_map,
    build_podcast_script_llm_synthesis,
    TRANSITION_MARKER,
)
from src.outputs.tts_edge import (
    tts_segment_to_mp3,
    last_tts_backend,
    last_tts_error_summary,
    tts_backend_stats,
)
from src.outputs.audio import concat_mp3_with_transitions, _ffprobe_duration_seconds, PLAYBACK_ATEMPO

from src.utils.text import clean_for_tts

from src.processing.article_extract import extract_article_text
from src.processing.article_analysis import analyze_article
from src.outputs.github_publish import upload_episode, push_site
from src.outputs.notion_publish import save_script_to_notion, save_transcript_to_notion


import shutil
import os

#  DEBUG=true python run_daily.py
DEBUG_MODE = os.environ.get('DEBUG', 'false').lower() == 'true'
REGEN_FROM_CACHE = os.environ.get('REGEN_FROM_CACHE', 'false').lower() == 'true'

SITE_URL = "https://wenyuedai.github.io/protein_design_podcast"


def _dynamic_pubmed_terms(state_dir: Path, existing_terms: list, max_new: int = 5) -> list:
    """
    Extract PubMed search terms from liked paper titles in feedback.json.
    Returns up to max_new new terms not already in existing_terms.
    """
    import re as _re
    STOP = {
        "the","a","an","and","or","of","in","for","to","is","are","with","from",
        "by","on","at","this","that","based","using","via","novel","new","study",
        "analysis","approach","method","role","through","between","into","its",
        "their","these","which","can","has","been","were","was","after","during",
    }
    fb_file = state_dir / "feedback.json"
    if not fb_file.exists():
        return []
    try:
        data = json.loads(fb_file.read_text(encoding="utf-8"))
    except Exception:
        return []

    # Build URL→title lookup from all episode_items.json files (for old-format entries)
    url_to_title: Dict[str, str] = {}
    output_dir = state_dir.parent / "output"
    for items_file in output_dir.glob("*/episode_items.json"):
        try:
            for it in json.loads(items_file.read_text(encoding="utf-8")):
                u = (it.get("url") or "").strip()
                t = (it.get("title") or "").strip()
                if u and t:
                    url_to_title[u] = t
        except Exception:
            pass

    titles = []
    for entries in data.values():
        for entry in (entries or []):
            title = ""
            if isinstance(entry, dict):
                title = (entry.get("title") or "").strip()
            elif isinstance(entry, str):
                # Old format: look up title from episode_items.json
                title = url_to_title.get(entry, "")
            if title:
                titles.append(title.lower())

    if not titles:
        return []

    # Extract bigrams and trigrams — both words must be ≥5 chars and not stop words
    phrase_counts: Dict[str, int] = {}
    for title in titles:
        words = [w for w in _re.findall(r"[a-zA-Z]{5,}", title) if w not in STOP]
        for i in range(len(words) - 1):
            phrase = f"{words[i]} {words[i+1]}"
            phrase_counts[phrase] = phrase_counts.get(phrase, 0) + 1
        for i in range(len(words) - 2):
            phrase = f"{words[i]} {words[i+1]} {words[i+2]}"
            phrase_counts[phrase] = phrase_counts.get(phrase, 0) + 1

    existing_lower = {t.lower() for t in existing_terms}
    # Filter out phrases starting with verb forms (-ing, -ed gerunds)
    # and sort: higher frequency first, then longer phrases (more specific)
    dynamic = []
    for phrase, count in sorted(phrase_counts.items(), key=lambda x: (-x[1], -len(x[0]))):
        if len(dynamic) >= max_new:
            break
        first_word = phrase.split()[0]
        if first_word.endswith("ing") or first_word.endswith("ling"):
            continue  # skip "revealing ...", "disentangling ...", etc.
        if phrase not in existing_lower:
            dynamic.append(phrase)

    return dynamic


def _llm_run_analysis(ranked: List[Dict[str, Any]], errors: List[str], cfg: Dict[str, Any]) -> str:
    """Ask the LLM to summarize today's run quality and suggest improvements."""
    try:
        import urllib.request as _ur
        api_key = os.environ.get(cfg.get("llm", {}).get("api_key_env", "OPENROUTER_API_KEY"), "")
        if not api_key:
            return ""
        model = cfg.get("llm", {}).get("analysis_model") or cfg.get("llm", {}).get("model", "")
        if not model:
            return ""

        sources = {}
        for it in ranked:
            src = (it.get("source") or "unknown").split("—")[0].strip()
            sources[src] = sources.get(src, 0) + 1
        source_summary = ", ".join(f"{s}({n})" for s, n in sorted(sources.items(), key=lambda x: -x[1])[:8])
        error_block = "\n".join(errors[:5]) if errors else "none"

        prompt = (
            f"You are an AI assistant reviewing a daily protein-design podcast pipeline run.\n"
            f"Items selected: {len(ranked)} | Sources: {source_summary}\n"
            f"Errors: {error_block}\n\n"
            f"In 3-4 concise bullet points, identify what went well, flag any concerns "
            f"(e.g. too many items from one source, missing key topics, errors), "
            f"and suggest 1-2 concrete improvements for tomorrow's run."
        )
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 350,
            "temperature": 0.3,
        }).encode()
        req = _ur.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        resp = _ur.urlopen(req, timeout=25)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(analysis failed: {e})"


def _notify_slack(date: str, ranked: List[Dict[str, Any]], cfg: Dict[str, Any],
                  errors: List[str] | None = None) -> None:
    """Post a summary + run analysis to Slack via Incoming Webhook."""
    import urllib.request
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        return

    errors = errors or []

    # Top 5 items for the digest
    lines = []
    for it in ranked[:5]:
        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        src = (it.get("source") or "").strip()
        entry = f"• <{url}|{title}>" if url else f"• {title}"
        if src:
            entry += f"  _{src}_"
        lines.append(entry)

    items_block = "\n".join(lines) if lines else "_(no items)_"
    total = len(ranked)
    text = (
        f":studio_microphone: *Knowledge Radio — {date}*\n"
        f"{total} papers & news selected | <{SITE_URL}|Listen on GitHub Pages>\n\n"
        f"*Top picks:*\n{items_block}"
    )

    if errors:
        err_block = "\n".join(f"⚠ {e}" for e in errors[:5])
        text += f"\n\n*Errors ({len(errors)}):*\n{err_block}"

    # LLM analysis + suggestions
    analysis = _llm_run_analysis(ranked, errors, cfg)
    if analysis:
        text += f"\n\n*Pipeline analysis & suggestions:*\n{analysis}"

    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
        print("[slack] Notification sent", flush=True)
    except Exception as e:
        print(f"[slack] Warning: could not send notification — {e}", flush=True)

def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve(base: Path, p: str) -> Path:
    """Resolve p against base when p is a relative path, otherwise return as-is."""
    resolved = Path(p)
    return resolved if resolved.is_absolute() else base / resolved


def main() -> int:
    repo_dir = Path(__file__).resolve().parent
    cfg = load_config(repo_dir / "config.yaml")
    _run_errors: List[str] = []   # collect non-fatal errors for Slack report

    tz = load_tz(cfg.get("timezone", "Europe/London"))

    run_date_env = (os.environ.get("RUN_DATE") or "").strip()
    if run_date_env:
        # expected YYYY-MM-DD
        today = run_date_env
        run_anchor = datetime.combine(datetime.fromisoformat(today).date(), time.min, tz)
    else:
        today = now_local_date(tz)
        run_anchor = datetime.now(tz)

    data_dir = _resolve(repo_dir, cfg["paths"]["data_dir"]) / today
    out_dir = _resolve(repo_dir, cfg["paths"]["output_dir"]) / today
    state_dir = _resolve(repo_dir, cfg["paths"]["state_dir"])

    ensure_dir(data_dir)
    ensure_dir(out_dir)
    ensure_dir(state_dir)

    # Idempotency guard: if today's episode is already published, do nothing.
    # release_index.json is committed by the first successful run, so a second
    # GitHub Actions run will check it out and exit here before touching anything.
    _force = os.environ.get("FORCE_REPUBLISH", "").strip().lower() in ("1", "true", "yes")
    if not _force:
        _release_index = state_dir / "release_index.json"
        try:
            _idx = json.loads(_release_index.read_text(encoding="utf-8")) if _release_index.exists() else {}
        except Exception:
            _idx = {}
        _items_done = (out_dir / "episode_items.json").exists()
        if today in _idx and _items_done:
            print(
                f"[run_daily] Episode {today} already published (release_index.json). "
                "Skipping. Set FORCE_REPUBLISH=true to override.",
                flush=True,
            )
            return 0

    seen = SeenStore(state_dir / "seen_ids.json")

    lookback_hours = int(os.environ.get("LOOKBACK_HOURS") or cfg.get("lookback_hours", 48))
    raw_collected_items: List[Dict[str, Any]] = []
    collector_counts: Dict[str, int] = {
        "rss": 0,
        "pubmed": 0,
        "biorxiv_keywords": 0,
        "biorxiv_authors": 0,
        "daily_knowledge": 0,
        "wiki_context": 0,
    }

    # 1) Collect (or regenerate from cached seed)
    seed_file = data_dir / "items.jsonl"
    if REGEN_FROM_CACHE and seed_file.exists():
        new_items: List[Dict[str, Any]] = []
        for line in seed_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                new_items.append(json.loads(line))
            except Exception:
                continue
        raw_collected_items = list(new_items)
        source_type_counts = Counter((it.get("source_type") or "unknown").strip() or "unknown" for it in raw_collected_items)
        collector_counts["rss"] = int(source_type_counts.get("rss", 0))
        collector_counts["pubmed"] = int(source_type_counts.get("pubmed", 0))
        collector_counts["biorxiv_keywords"] = int(source_type_counts.get("biorxiv", 0))
        collector_counts["biorxiv_authors"] = sum(
            1 for it in raw_collected_items if "bioRxiv" in ((it.get("source") or ""))
        )
    else:
        items: List[Dict[str, Any]] = []
        _rss_sources = list(cfg["rss_sources"])
        _extra_rss_file = state_dir / "extra_rss_sources.json"
        if _extra_rss_file.exists():
            try:
                _extra = json.loads(_extra_rss_file.read_text(encoding="utf-8"))
                if _extra:
                    print(f"[rss] Merging {len(_extra)} extra source(s) from extra_rss_sources.json", flush=True)
                    _rss_sources = _rss_sources + _extra
            except Exception:
                pass
        rss_items = collect_rss_items(_rss_sources, tz=tz, lookback_hours=lookback_hours, now_ref=run_anchor)
        collector_counts["rss"] = len(rss_items)
        items.extend(rss_items)
        if cfg.get("pubmed", {}).get("enabled", False):
            static_terms = cfg.get("pubmed", {}).get("search_terms", [])
            dynamic_terms = _dynamic_pubmed_terms(state_dir, static_terms, max_new=5)
            if dynamic_terms:
                print(f"[pubmed] Adding {len(dynamic_terms)} dynamic term(s) from feedback: {dynamic_terms}", flush=True)
            pubmed_items = collect_pubmed_items(cfg, lookback_hours=lookback_hours, extra_terms=dynamic_terms)
            collector_counts["pubmed"] = len(pubmed_items)
            items.extend(pubmed_items)
            if cfg.get("biorxiv_keywords", {}).get("enabled", False):
                biorxiv_keyword_items = collect_biorxiv_keyword_items(
                    cfg,
                    lookback_hours=lookback_hours,
                    extra_terms=dynamic_terms,
                )
                collector_counts["biorxiv_keywords"] = len(biorxiv_keyword_items)
                items.extend(biorxiv_keyword_items)
        if cfg.get("biorxiv_authors", {}).get("enabled", True):
            biorxiv_author_items = collect_biorxiv_author_items(cfg)
            collector_counts["biorxiv_authors"] = len(biorxiv_author_items)
            items.extend(biorxiv_author_items)
        if cfg.get("daily_knowledge", {}).get("enabled", True):
            daily_items = collect_daily_knowledge_items(tz=tz)
            collector_counts["daily_knowledge"] = len(daily_items)
            items.extend(daily_items)
        if cfg.get("wiki_context", {}).get("enabled", False):
            wiki_items = collect_wiki_context_items(
                cfg.get("wiki_context", {}).get("topics", []),
                date_str=today,
                max_items=int(cfg.get("wiki_context", {}).get("max_items", 4)),
            )
            collector_counts["wiki_context"] = len(wiki_items)
            items.extend(wiki_items)
        raw_collected_items = list(items)

        # 2) Dedup across days + topical filtering
        excluded_terms = list(cfg.get("excluded_terms", [
            "cell biology", "single-cell", "single cell", "animal model", "murine",
            "mouse", "mice", "rat", "zebrafish", "drosophila", "in vivo"
        ]))

        # First pass: filter and mark which items need fetch/analysis
        # Use a local set for within-run URL dedup (prevents processing the same
        # URL twice when multiple RSS feeds overlap).  The persistent seen_ids is
        # only written AFTER ranking so that runner-up articles (those that don't
        # make the final episode due to the item cap) remain available for future
        # runs — this ensures weekend episodes when arXiv/journals don't publish.
        candidates: List[Dict[str, Any]] = []
        new_items: List[Dict[str, Any]] = []
        _run_seen_urls: set = set()
        for it in items:
            url = (it.get("url") or "").strip()
            title = (it.get("title") or "")
            source = (it.get("source") or "")
            hay = f"{title} {source} {url}".lower()

            if not url:
                continue
            if any(t in hay for t in excluded_terms):
                continue
            if url in _run_seen_urls:
                continue
            _run_seen_urls.add(url)

            # Wiki context items are pre-built summaries; keep them lightweight.
            if it.get("kind") == "wiki_context":
                new_items.append(it)
                continue

            if not DEBUG_MODE and seen.has(url):
                continue
            candidates.append(it)

        # Second pass: parallel article extract + analysis
        max_workers = int(cfg.get("fetch_workers", 8))
        analysis_model = cfg.get("llm", {}).get("analysis_model") or cfg.get("llm", {}).get("model")

        def _fetch_and_analyze(it: Dict[str, Any]) -> Dict[str, Any]:
            url = (it.get("url") or "").strip()
            body = extract_article_text(url)
            it["extracted_chars"] = len(body or "")
            it["has_fulltext"] = bool(body and len(body) > 1500)
            it["analysis"] = analyze_article(url, body, model=analysis_model)
            return it

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_and_analyze, it): it for it in candidates}
            for fut in as_completed(futures):
                try:
                    new_items.append(fut.result())
                except Exception as _e:
                    it = futures[fut]
                    _run_errors.append(f"fetch/analyze failed for '{it.get('title','?')[:60]}': {_e}")
                    new_items.append(it)

        write_jsonl(seed_file, new_items)

    # 3a) Preliminary rank (tier system, no S2 yet) to identify top candidates
    pre_ranked = rank_and_limit(new_items, cfg)

    # 3b) Optional Semantic Scholar enrichment on top candidates
    _s2_shared_landscape: list = []
    _s2_missed_surfaces: list = []
    _s2_api_key = os.environ.get("S2_API_KEY", "").strip()
    if _s2_api_key and not REGEN_FROM_CACHE:
        try:
            from src.collectors.semantic_scholar import enrich_with_s2
            _s2_enrich_count = int(cfg.get("semantic_scholar", {}).get("enrich_top_n", 60))
            pre_ranked, _s2_shared_landscape, _s2_missed_surfaces = enrich_with_s2(
                pre_ranked,
                cfg,
                api_key=_s2_api_key,
                is_seen=seen.has,
                max_enrich=_s2_enrich_count,
            )
        except Exception as _s2_err:
            _run_errors.append(f"S2 enrichment failed: {_s2_err}")
            print(f"[s2] Warning: enrichment failed — {_s2_err}", flush=True)

    # 3c) Final rank (now includes s2_reference_score as tier 8 tiebreaker)
    ranked = rank_and_limit(pre_ranked, cfg)

    # Save missed surfaces for user review
    if _s2_missed_surfaces:
        _surfaced_file = state_dir / "s2_surfaced_papers.json"
        try:
            _existing = json.loads(_surfaced_file.read_text(encoding="utf-8")) if _surfaced_file.exists() else {}
            _existing[today] = _s2_missed_surfaces
            _surfaced_file.write_text(json.dumps(_existing, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[s2] {len(_s2_missed_surfaces)} missed surface(s) saved to state/s2_surfaced_papers.json", flush=True)
        except Exception as _e:
            print(f"[s2] Warning: could not save surfaced papers — {_e}", flush=True)

    # Mark only ranked (featured) items as seen so runner-up articles remain
    # available for future runs (e.g. weekend episodes with sparse new content).
    if not REGEN_FROM_CACHE:
        for _it in ranked:
            _url = (_it.get("url") or "").strip()
            if _url:
                seen.add(_url)
        seen.save()

    # 4) Save ranked item list for the website (complete index, not just highlights)
    import re as _re
    try:
        from bs4 import BeautifulSoup as _BS
        def _strip_html(s: str) -> str:
            return _BS(s, "html.parser").get_text(" ", strip=True)
    except ImportError:
        def _strip_html(s: str) -> str:
            return _re.sub(r'<[^>]+>', ' ', s).strip()

    def _best_summary(it: Dict[str, Any]) -> str:
        # Try one_liner / snippet first (strip HTML)
        raw = (it.get("one_liner") or it.get("snippet") or "").strip()
        clean = _strip_html(raw)
        if len(clean) > 30:
            return clean
        # Fall back to CORE CLAIM from LLM analysis
        analysis = (it.get("analysis") or "").strip()
        m = _re.search(r'CORE CLAIM:\s*(.+?)(?:\n[A-Z ]+:|$)', analysis, _re.S)
        if m:
            sentence = m.group(1).strip().split(". ")[0]
            if sentence and sentence.lower() != "not stated in source text":
                return sentence + ("." if not sentence.endswith(".") else "")
        return ""

    # 5) LLM podcast script (also returns item→segment mapping)
    podcast_cfg = cfg.get("podcast", {})
    synthesis_mode = bool(podcast_cfg.get("synthesis_mode", False))
    featured_count = int(podcast_cfg.get("featured_count", 12))

    # In synthesis mode: top N papers go into the deep briefing; the rest are
    # shown on the website greyed-out (available for feedback/notes) but not spoken.
    if synthesis_mode:
        featured_items = ranked[:featured_count]
        background_items = ranked[featured_count:]
    else:
        featured_items = ranked
        background_items = []

    # 5a) For synthesis mode: use S2 to fetch full text for featured papers via openAccessPdf
    if synthesis_mode and _s2_api_key and not REGEN_FROM_CACHE:
        try:
            from src.collectors.semantic_scholar import enrich_featured_fulltext
            print(f"[s2] Fetching full text for {len(featured_items)} featured papers…", flush=True)
            enrich_featured_fulltext(featured_items, api_key=_s2_api_key)
        except Exception as _s2_ft_err:
            print(f"[s2] Warning: full-text enrichment failed — {_s2_ft_err}", flush=True)

    script_path = out_dir / f"podcast_script_{today}_llm.txt"
    if REGEN_FROM_CACHE and script_path.exists():
        print("[cache] Reusing existing LLM script", flush=True)
        script_text = script_path.read_text(encoding="utf-8")
        _item_segments = [-1] * len(featured_items) if synthesis_mode else list(range(len(ranked)))
    elif synthesis_mode:
        print(f"[script] Synthesis mode: {len(featured_items)} featured, {len(background_items)} greyed-out", flush=True)
        script_text, _item_segments = build_podcast_script_llm_synthesis(
            date_str=today,
            items=featured_items,
            cfg=cfg,
            shared_landscape=_s2_shared_landscape or None,
        )
    else:
        script_text, _item_segments = build_podcast_script_llm_chunked_with_map(date_str=today, items=ranked, cfg=cfg)

    # Append explicit citations to comprehensive script (for website readers / Spotify notes)
    refs: List[str] = []
    refs.append("\n\nReferences:")
    for i, it in enumerate(featured_items, 1):
        title = (it.get("title") or "(untitled)").strip()
        src = (it.get("source") or "unknown source").strip()
        url = (it.get("url") or "").strip()
        if url:
            refs.append(f"[{i}] {title} — {src} — {url}")
        else:
            refs.append(f"[{i}] {title} — {src}")
    script_text = script_text.rstrip() + "\n" + "\n".join(refs) + "\n"

    write_text(script_path, script_text)
    script_text_clean = clean_for_tts(script_text)
    script_path_clean = out_dir / f"podcast_script_{today}_llm_clean.txt"
    write_text(script_path_clean, script_text_clean)

    # Write episode_items.json (base: segment per item, timestamp=-1 until TTS computes it)
    # In synthesis mode all 40 items are saved: featured ones are spoken about in the
    # deep briefing (featured=True), the rest are shown greyed-out on the website
    # (featured=False) but still available for feedback checkboxes and note-taking.
    _all_display_items = featured_items + background_items if synthesis_mode else ranked
    _episode_items_list: List[Dict[str, Any]] = []
    for _i, _it in enumerate(_all_display_items):
        _is_featured = not synthesis_mode or (_i < len(featured_items))
        _seg = _item_segments[_i] if (not synthesis_mode and _i < len(_item_segments)) else -1
        _episode_items_list.append({
            "title": (_it.get("title") or "").strip(),
            "url": (_it.get("url") or "").strip(),
            "source": (_it.get("source") or "").strip(),
            "one_liner": _best_summary(_it),
            "segment": _seg,
            "timestamp": -1,
            "featured": _is_featured,
        })
    _episode_items_file = out_dir / "episode_items.json"
    _episode_items_file.write_text(
        json.dumps({"timestamps": [], "items": _episode_items_list}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    tts_backend = None
    tts_fallback_summary = ""
    tts_stats: Dict[str, Any] = {}

    # 6) TTS: one MP3 per segment, concatenated with transition SFX between items
    if cfg.get("podcast", {}).get("enabled", True) and script_text_clean.strip():
        voice = cfg["podcast"]["voice"]
        rate = str(cfg["podcast"].get("voice_rate", "+20%"))
        parts_dir = out_dir / "tts_parts"
        ensure_dir(parts_dir)

        # Keep raw_segments_all WITHOUT filtering so indices align with _item_segments.
        # (filtering shifts indices, causing every item after a missing segment to seek wrong)
        raw_segments_all = [s.strip() for s in script_text.split(TRANSITION_MARKER)]
        seg_mp3s: List[Path] = []
        _raw_seg_to_group: Dict[int, int] = {}  # raw_segment_index → seg_mp3s index

        for _si, _seg in enumerate(raw_segments_all):
            if not _seg:
                continue
            _seg_clean = clean_for_tts(_seg)
            seg_mp3_path = parts_dir / f"seg_{_si:03d}.mp3"
            tts_segment_to_mp3(
                text=_seg_clean,
                out_path=seg_mp3_path,
                voice=voice,
                rate=rate,
            )
            _raw_seg_to_group[_si] = len(seg_mp3s)
            seg_mp3s.append(seg_mp3_path)

        tts_backend = last_tts_backend()
        tts_fallback_summary = last_tts_error_summary()
        tts_stats = tts_backend_stats()
        final_playback_atempo = 1.0 if tts_backend == "edge" else PLAYBACK_ATEMPO

        # Compute per-segment SFX-start timestamps.
        # For gi > 0 we point to 0.5s before the transition tones so clicking lands
        # on audible tones immediately.  The SFX structure is:
        #   1.0s silence | 0.12s tone | 0.06s gap | 0.12s tone | 1.0s silence  (total 2.3s raw)
        # Seeking to SFX_start+0.5s means: 0.5s silence → tones → 1.0s silence → content.
        # Using 1.8 (= 2.3 - 0.5) as the seek-back amount also absorbs small
        # accumulated encoder-delay measurement error across many segments.
        _SFX_RAW = 2.3          # full SFX raw duration – used for position accumulation
        _SFX_SEEK_OFFSET = 1.8  # seek-back from content start: land 0.5s before tones
        _raw_durs: List[float] = [_ffprobe_duration_seconds(p) for p in seg_mp3s]
        _seg_ts: List[float] = []
        _t = 0.0
        for _gi, _rd in enumerate(_raw_durs):
            if _gi == 0:
                _seg_ts.append(0.0)
            else:
                # _t is at content start of segment _gi; seek to 0.5s before the tones.
                _seg_ts.append(max(0.0, round((_t - _SFX_SEEK_OFFSET) / final_playback_atempo, 2)))
            _t += _rd
            if _gi < len(_raw_durs) - 1:
                _t += _SFX_RAW

        # Each item maps directly to its segment's pre-tones timestamp.
        for _entry in _episode_items_list:
            _raw_si = _entry["segment"]
            _gi = _raw_seg_to_group.get(_raw_si)
            _entry["timestamp"] = _seg_ts[_gi] if _gi is not None else -1

        _episode_items_file.write_text(
            json.dumps({"timestamps": _seg_ts, "items": _episode_items_list}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        final_mp3 = out_dir / f"podcast_{today}.mp3"
        concat_mp3_with_transitions(seg_mp3s, final_mp3, playback_atempo=final_playback_atempo)

        # Clean up intermediate TTS chunks and temp ffmpeg files
        pub_cfg = cfg.get("publish", {})
        if pub_cfg.get("cleanup_intermediate", True):
            shutil.rmtree(parts_dir, ignore_errors=True)
            for tmp in ["ffmpeg_concat_list.txt", "transition_sfx.mp3"]:
                p = out_dir / tmp
                if p.exists():
                    p.unlink()

        # Publish to GitHub Release + push GitHub Pages
        if pub_cfg.get("enabled", False):
            release_repo = pub_cfg.get("github_release_repo", "")
            if release_repo:
                upload_episode(
                    today,
                    final_mp3,
                    script_path_clean,
                    repo=release_repo,
                    state_dir=state_dir,
                )
            push_site(repo_dir, repo_dir.parent, today)

    source_counts = Counter((it.get("source") or "").strip() or "(unknown)" for it in raw_collected_items)
    source_type_counts = Counter((it.get("source_type") or "unknown").strip() or "unknown" for it in raw_collected_items)
    status = {
        "date": today,
        "time": iso_now_local(tz),
        "n_items_raw": len(new_items),
        "n_items_used": len(ranked),
        "lookback_hours": lookback_hours,
        "run_anchor": run_anchor.isoformat(timespec="seconds"),
        "pubmed_matches": collector_counts["pubmed"],
        "biorxiv_keyword_matches": collector_counts["biorxiv_keywords"],
        "biorxiv_author_matches": collector_counts["biorxiv_authors"],
        "collector_counts": collector_counts,
        "collected_by_source_type": dict(source_type_counts.most_common()),
        "collected_by_source": dict(source_counts.most_common()),
        "tts_backend": tts_backend,
        "tts_fallback_reason": tts_fallback_summary,
        "tts_stats": tts_stats,
        "output_dir": str(out_dir),
    }
    (out_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status, indent=2))

    save_script_to_notion(today, script_path, ranked)

    # Save synthesis transcript to dedicated Notion database and record URL
    _transcript_notion_url = save_transcript_to_notion(today, script_path)
    if _transcript_notion_url:
        _transcript_index_file = state_dir / "transcript_notion_index.json"
        try:
            _tidx = json.loads(_transcript_index_file.read_text(encoding="utf-8")) if _transcript_index_file.exists() else {}
            _tidx[today] = _transcript_notion_url
            _transcript_index_file.write_text(json.dumps(_tidx, indent=2, sort_keys=True), encoding="utf-8")
        except Exception as _te:
            print(f"[notion] Could not update transcript_notion_index.json — {_te}", flush=True)

    # Append S2 missed surfaces to Slack errors block so they're visible
    if _s2_missed_surfaces:
        _run_errors.append(
            "S2 surfaced papers not yet in pipeline: "
            + ", ".join(f"\"{s['title'][:60]}\" ({s['citations']} citations)" for s in _s2_missed_surfaces[:3])
        )

    _notify_slack(today, ranked, cfg, errors=_run_errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

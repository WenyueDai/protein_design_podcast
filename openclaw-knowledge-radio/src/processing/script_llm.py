import os, json, requests
import time
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI


# =========================
# Client (OpenRouter / OpenAI-compatible)
# =========================

def _client_from_config(cfg: Dict[str, Any]) -> OpenAI:
    api_key_env = cfg.get("llm", {}).get("api_key_env", "OPENROUTER_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"Missing env var {api_key_env} for OpenRouter API key")
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )


def _chat_complete(
    client: OpenAI,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    retries: int = 3,
) -> str:
    err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            err = e
            if attempt < retries:
                time.sleep(1.5 * attempt)
            else:
                raise
    raise err  # pragma: no cover


# =========================
# Helpers
# =========================

def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    if n <= 0 or len(s) <= n:
        return s
    return s[: max(0, n - 3)] + "..."


def _chunk(xs: List[Any], n: int) -> List[List[Any]]:
    if n <= 0:
        return [xs]
    return [xs[i : i + n] for i in range(0, len(xs), n)]


def _item_meta(it: Dict[str, Any]) -> Tuple[str, str, str, str, str, int, bool]:
    title = (it.get("title") or "").strip()
    url = (it.get("url") or "").strip()
    src = (it.get("source") or "").strip()
    bucket = (it.get("bucket") or "").strip()
    snippet = (it.get("one_liner") or it.get("snippet") or "").strip()
    extracted_chars = _safe_int(it.get("extracted_chars", 0), 0)
    has_fulltext = bool(it.get("has_fulltext", False))
    return title, url, src, bucket, snippet, extracted_chars, has_fulltext


def _fulltext_ok(it: Dict[str, Any], threshold_chars: int) -> bool:
    _, _, _, _, _, extracted_chars, has_fulltext = _item_meta(it)
    return has_fulltext or (extracted_chars >= threshold_chars)


def _analysis_text(it: Dict[str, Any]) -> str:
    """
    What we feed the LLM for understanding.
    Prefer per-article analysis if you have it; fallback to snippet.
    """
    a = it.get("analysis")
    if isinstance(a, str) and a.strip():
        return a.strip()
    # Some users store analysis as dict; be defensive
    if isinstance(a, dict):
        # keep it compact
        parts = []
        for k in ["core_claim", "method", "results", "why_it_matters", "limitations", "terms"]:
            v = a.get(k)
            if v:
                parts.append(f"{k.upper()}: {str(v)}")
        if parts:
            return "\n".join(parts).strip()
    # fallback
    return (it.get("one_liner") or it.get("snippet") or "").strip()


def _format_item_block(it: Dict[str, Any]) -> str:
    title, url, src, bucket, snippet, extracted_chars, has_fulltext = _item_meta(it)
    tags = it.get("tags") or []
    tags_str = ", ".join([str(t) for t in tags]) if isinstance(tags, list) else str(tags)

    lines: List[str] = []
    lines.append(f"TITLE: {title}")
    if src:
        lines.append(f"SOURCE: {src}")
    if bucket:
        lines.append(f"BUCKET: {bucket}")
    if tags_str:
        lines.append(f"TAGS: {tags_str}")
    if url:
        lines.append(f"URL: {url}")
    if snippet:
        lines.append(f"RSS_SNIPPET: {_clip(snippet, 420)}")
    lines.append(f"EXTRACTED_CHARS: {extracted_chars}")
    lines.append(f"HAS_FULLTEXT: {has_fulltext}")
    s2_tldr = (it.get("s2_tldr") or "").strip()
    if s2_tldr:
        lines.append(f"S2_TLDR: {s2_tldr}")

    notes = _analysis_text(it)
    if notes:
        lines.append("NOTES_FROM_PIPELINE:")
        lines.append(_clip(notes, 40_000))  # large cap — may now contain full PDF text
    else:
        lines.append("NOTES_FROM_PIPELINE: (none)")

    # Inject Semantic Scholar related literature if available
    top_refs: List[Dict] = it.get("s2_top_refs") or []
    if top_refs:
        lines.append("KEY RELATED LITERATURE (papers this work cites, by citation count):")
        for ref in top_refs:
            ref_title = ref.get("title") or ""
            ref_year = ref.get("year") or ""
            ref_cites = ref.get("citationCount") or 0
            ref_abstract = ref.get("abstract") or ""
            lines.append(f"  [{ref_cites} citations, {ref_year}] {ref_title}")
            if ref_abstract:
                lines.append(f"    Abstract: {ref_abstract}")

    return "\n".join(lines)


# =========================
# Prompts (ENGLISH ONLY)
# =========================

TRANSITION_MARKER = "[[TRANSITION]]"

SYSTEM_DEEP_DIVE = """You are an expert English podcast host for a long-form run-friendly science/tech show.
This segment MUST be based ONLY on the provided item block and notes.

Style goals:
- High information density, low fluff.
- Conversational and slightly playful, but technically accurate.
- Start directly with the core innovation. No greetings or catchphrases.

Hard rules:
- Do NOT invent methods/results/numbers.
- Do NOT spend long time on minor parameter details unless critical to novelty.
- Prioritize: what is new, why it matters, what changed vs prior work.
- Use only NOTES_FROM_PIPELINE and metadata.
- If details are missing, explicitly say: "The available text does not provide details on X."
- Mention source name naturally when making a claim.
- No markdown symbols, TTS-friendly plain text.
- No ending phrases after each paper segment.

Length requirement:
- About 220–340 words per deep dive.
"""

SYSTEM_ROUNDUP = """You are an English podcast host doing concise roundup segments.
Use ONLY the provided item blocks and notes.

Style:
- Crisp, lively, and accessible. Keep it punchy.

Rules:
- CRITICAL: You MUST cover EVERY item in the batch. Do NOT skip or merge any items.
- For EACH item: 80–130 words.
- Lead with the key takeaway and novelty.
- Avoid low-value parameter minutiae unless crucial.
- Be concrete but never invent details or numbers.
- Mention source name in each item.
- No greetings, no sign-off after each item.
- No markdown symbols, TTS-friendly plain text.
"""

SYSTEM_OPENING = """You are an English podcast host.
Write a SUPER SHORT opening (35–60 words) for today's episode.
Tone: warm, energetic, a tiny bit playful.
Do NOT invent facts.
"""

SYSTEM_CLOSING = """You are an English podcast host.
Write a SUPER SHORT closing (25–45 words) that recaps and signs off.
Tone: upbeat, concise.
Do NOT invent facts.
"""

# Optional merge via LLM (disabled by default). If enabled, it must not delete content.
SYSTEM_MERGE_NO_DELETE = """You are the editor-in-chief assembling a final podcast script.

CRITICAL RULES:
- You MUST NOT delete or summarize away any substantive information.
- You MAY ONLY:
  - add very short transitions (1–2 sentences) between segments
  - reorder segments if needed
  - fix obvious formatting issues (whitespace)
- The final length should be approximately the sum of all segments (no compression).

Output in English, TTS-friendly.
- Don't need the opening and closing for the podcast, go directly to the knowledge.
"""


# =========================
# Single-call version (kept for compatibility)
# =========================

def build_podcast_script_llm(*, date_str: str, items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> str:
    """
    Kept for backwards compatibility (one call). Still English-only.
    """
    client = _client_from_config(cfg)
    model = cfg["llm"]["model"]
    temperature = float(cfg["llm"].get("temperature", 0.25))
    max_tokens = int(cfg["llm"].get("max_output_tokens", 5200))

    blocks = []
    for i, it in enumerate(items, 1):
        blocks.append(f"=== ITEM {i} ===\n{_format_item_block(it)}")

    user = (
        f"DATE: {date_str}\n\n"
        "Generate an English podcast script ONLY from the items below.\n"
        "Do not invent details.\n"
        "Keep it TTS-friendly.\n\n"
        + "\n\n".join(blocks)
    )

    # Use roundup prompt style for a single call
    return _chat_complete(
        client,
        model=model,
        system=SYSTEM_ROUNDUP,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
    ).strip()


# =========================
# Chunked multi-call version (Deep dive only if fulltext)
# =========================

def build_podcast_script_llm_chunked(*, date_str: str, items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> str:
    """
    One LLM call per item, in ranked order.

    Each item gets its own segment separated by TRANSITION_MARKER so that:
    - segment index i = ranked item i  (item_segments[i] == i)
    - audio order matches website display order
    - clicking highlight [N] always plays item N's SFX + content

    Items with fulltext get a deep-dive treatment (~220-340 words).
    Items without fulltext get a concise roundup treatment (~80-130 words).
    """
    client = _client_from_config(cfg)
    model = cfg["llm"]["model"]
    temperature = float(cfg["llm"].get("temperature", 0.25))

    podcast_cfg = (cfg.get("podcast") or {})
    chunk_cfg = (podcast_cfg.get("chunking") or {})

    fulltext_threshold = int(chunk_cfg.get("fulltext_threshold_chars", 2500))
    deep_max_tokens = int(chunk_cfg.get("deep_dive_max_tokens", 2600))
    roundup_max_tokens = int(chunk_cfg.get("roundup_max_tokens", 2200))

    ranked = list(items)
    segments: List[str] = []

    for idx, it in enumerate(ranked, 1):
        block = _format_item_block(it)
        if _fulltext_ok(it, fulltext_threshold):
            user = (
                f"DATE: {date_str}\n"
                f"DEEP DIVE #{idx}\n\n"
                f"{block}\n\n"
                "Write a deep-dive segment that would take ~6–10 minutes to narrate.\n"
                "Be strict about what is known vs unknown.\n"
            )
            seg = _chat_complete(
                client,
                model=model,
                system=SYSTEM_DEEP_DIVE,
                user=user,
                temperature=temperature,
                max_tokens=deep_max_tokens,
            ).strip()
        else:
            user = (
                f"DATE: {date_str}\n"
                f"ITEM #{idx}\n\n"
                f"{block}\n\n"
                "Write a concise 80–130 word roundup for this single item. "
                "Lead with the key finding, mention the source, no sign-off."
            )
            seg = _chat_complete(
                client,
                model=model,
                system=SYSTEM_ROUNDUP,
                user=user,
                temperature=temperature,
                max_tokens=roundup_max_tokens,
            ).strip()
        segments.append(seg)

    return f"\n\n{TRANSITION_MARKER}\n\n".join(segments).strip()


def build_podcast_script_llm_chunked_with_map(
    *, date_str: str, items: List[Dict[str, Any]], cfg: Dict[str, Any]
) -> tuple:
    """
    Same as build_podcast_script_llm_chunked but also returns item_segments.
    Since items are generated in ranked order, item_segments[i] == i always.
    Returns (script_text, item_segments).
    """
    script = build_podcast_script_llm_chunked(date_str=date_str, items=items, cfg=cfg)
    item_segments: List[int] = list(range(len(items)))
    return script, item_segments


# =========================
# Deep synthesis prompt (11-section intelligence briefing, per-section calls)
# =========================

SYSTEM_SYNTHESIS_STYLE = """You are an expert scientific podcast host creating a daily intelligence briefing for a computational protein designer. Today's episode does a deep dive on 5 carefully selected papers.

VOICE AND STYLE — this is the most important part:
- Speak naturally, as if you are a brilliant scientist friend thinking aloud over coffee. Pure flowing spoken English — no labels, no structured list items read aloud, no headers spoken as words.
- Do NOT say things like "Old belief colon" or "Paper one." Do NOT structure your speech as a visible list. Instead, weave ideas together in natural paragraphs that flow one into the next.
- When you cite a paper, say its title or topic naturally woven into your sentence: "A fascinating paper on antibody escape..." or "The team working on de novo binders found..."
- Use the KEY RELATED LITERATURE provided for each paper to add depth and historical context — "this connects to the work on X from 2021..." or "similar to what the RFdiffusion team found, this group observed..."
- Be warm, curious, a little playful. Intellectually generous. Every sentence should carry a real idea.
- Synthesise across papers — compare them, find tensions, draw connections — rather than describing them one by one.

HARD RULES:
- Plain text only. No markdown, no asterisks, no dashes at line starts, no colons followed by structured items.
- Do NOT invent methods, results, numbers, or author intent beyond what is provided in the paper data.
- If information is genuinely missing, say it naturally: "The paper doesn't tell us exactly how they handled..."
- TTS-friendly — this will be read aloud by a text-to-speech system.
- Go straight into the ideas. No catchphrases, no "Welcome back", no "In this section we will cover".

LENGTH:
- This is a single section of a long-form podcast. Write at least 600 words. Be intellectually thorough.
- Depth over breadth — explore ideas fully. Use the related literature to add context and connections.
"""

# (section_title, section_instruction) — one entry per section
_SYNTHESIS_SECTIONS: List[Tuple[str, str]] = [
    (
        "What actually mattered today",
        """Walk through the five to eight most important scientific insights from today's papers.
For each one, tell the story of the shift — what the field used to assume, what these new results are now suggesting instead, and why that change of perspective matters more than just adding another data point. Explain what made the evidence convincing: what experiment, computation, or analysis actually demonstrated this. Mention which papers this comes from, woven naturally into your speech. End each insight with what it actually changes for someone doing protein or antibody design today.
Focus on insights that change how a scientist should THINK, not just what they should know."""
    ),
    (
        "If I were designing a project inspired by today's papers",
        """Propose three to five realistic research project ideas that today's papers make you want to pursue.
For each idea, think aloud: what is the core hypothesis, what is the smallest possible experiment or computation that would test whether the idea has legs, what would success actually look like, and what single result would kill the idea early enough to save time. Be honest about the risk level. Explain why the idea is worth trying despite those risks.
Focus on research directions a computational protein designer could realistically attempt, not blue-sky fantasy."""
    ),
    (
        "Knowledge expansion — connecting to broader science",
        """For the most important insights from today's papers, explore their deeper connections — to protein physics, evolution, thermodynamics, statistical mechanics, information theory, machine learning theory, or structural biology fundamentals.
Think aloud about what earlier scientific work today's results resemble. Are these groups rediscovering something old with better tools? What general scientific principle does this reflect? What mental model from another field maps onto what these papers are doing?
Help build deep intuition rather than just recounting what was found."""
    ),
    (
        "Clever methods and how they proved things",
        """Pick the most interesting experimental or computational methods from today's papers and explain what made them clever.
For each one, think through: what question was it designed to answer, why would a simpler approach have failed or been misleading, what controls or orthogonal checks made the result convincing, and what hidden weakness does this method avoid. Then ask whether the same logic could be reused in protein design evaluation, filtering, or screening.
Look for adversarial tests, model stress tests, clever dataset designs, causal inference tricks, and anything that rules out confounds in an elegant way."""
    ),
    (
        "New design heuristics I should adopt",
        """Extract practical design rules from today's papers — the kind of if-then judgment a seasoned designer builds up over years.
For each rule, speak through when it applies, when it breaks down, what warning signs suggest you're in the failure case, and how it translates into antibody or protein design decisions.
Do not give generic advice. Extract the specific, grounded rule that today's papers actually support — and be honest about how far it generalises."""
    ),
    (
        "Where the field might be heading",
        """Based on today's papers, think aloud about the emerging directions you're noticing — physics-aware machine learning, uncertainty estimation, hybrid compute-experiment loops, generative design, dataset-driven biology, negative design, or whatever patterns emerge from this particular set of papers.
For each trend, ask: is the signal real or is this hype, what would confirm that this is a genuine direction rather than noise, and what should someone watch over the next two to three years to tell the difference?"""),
    (
        "Tensions, contradictions, and healthy skepticism",
        """Look across today's papers for places where they disagree with each other, or where individual results rest on fragile assumptions.
Think through: which claims seem overstated given the evidence shown, which results might depend on dataset bias or favorable test cases, and where are the missing validation steps that would really nail down the conclusion?
Explain what additional evidence would make you genuinely confident. This section should help protect against being swept up in hype."""
    ),
    (
        "What an expert protein designer would notice",
        """Think about what an experienced protein designer would see in today's papers that most readers would miss.
For each observation, contrast the surface reading with the deeper expert reading — what looks like a positive result but actually reveals a hidden limitation, where a model's confidence is probably inflated, what assumption is baked in that the authors didn't explicitly flag, where a failure mode is quietly visible in the supplementary data, where authors accidentally reveal a useful design rule while making a different point.
Be specific. Connect each observation to the papers it comes from."""
    ),
    (
        "History and philosophy of science perspective",
        """For the major ideas in today's papers, think about what kind of scientific progress this actually represents.
Is this incremental engineering, a genuine conceptual shift, tool-driven discovery, data-driven pattern finding, or a theory-driven prediction that was then verified? What past developments in biology, physics, or computer science does this most resemble? Does this change how science is being done, or just what is known?
Take a step back and reflect on where protein design sits as a scientific discipline right now, and what today's papers say about its trajectory."""
    ),
    (
        "Today's mental model update",
        """This is the most important section. Speak through what you would actually update in how you think about protein or antibody design, based on today's reading.
Walk through five things worth revising in your mental model. Then five concrete experiments, analyses, or workflow changes you'd seriously consider running or trying. Then three things these papers make you want to question more carefully.
Share the most non-obvious insight from today — the one that surprised you or that most readers would miss. End with the most elegant scientific idea you encountered."""
    ),
    (
        "Personal research expansion notes",
        """Close with open-ended thinking prompts — questions worth sitting with, ideas worth testing later even if there's no time now, possible improvements to computational workflows, metrics worth tracking, failure modes worth building intuitions around, new datasets worth knowing about.
This is a space for intellectual generosity. Prompt the listener to keep expanding their domain knowledge in the directions today's papers point toward."""
    ),
]


def build_podcast_script_llm_synthesis(
    *,
    date_str: str,
    items: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    shared_landscape: Optional[List[Dict]] = None,
) -> Tuple[str, List[int]]:
    """
    Generate a deep 11-section synthesis podcast from the top featured papers.

    Makes one LLM call per section so each gets its own token budget and the
    model can't shortcut the whole script in a single lazy pass.

    shared_landscape: optional list of {title, year, cited_by_count} dicts
      from Semantic Scholar — foundational papers cited by multiple featured
      papers today.

    All featured items map to segment -1.
    Returns (script_text, item_segments).
    """
    client = _client_from_config(cfg)
    model = cfg["llm"]["model"]
    temperature = float(cfg["llm"].get("temperature", 0.25))

    podcast_cfg = cfg.get("podcast") or {}
    # Per-section token budget — default 1400 (~700-900 words, ~5 min narration)
    section_max_tokens = int(podcast_cfg.get("synthesis_section_max_tokens", 1400))

    # Build shared paper context block (reused across all section calls)
    blocks: List[str] = []
    for i, it in enumerate(items, 1):
        blocks.append(f"=== PAPER {i} ===\n{_format_item_block(it)}")
    papers_block = "\n\n".join(blocks)

    landscape_block = ""
    if shared_landscape:
        lines = ["SHARED REFERENCE LANDSCAPE (papers cited by multiple of today's featured papers):"]
        for entry in shared_landscape:
            title = entry.get("title") or "(unknown)"
            year  = entry.get("year") or ""
            count = entry.get("cited_by_count", 0)
            lines.append(f"  Cited by {count} papers: \"{title}\" ({year})")
        landscape_block = "\n".join(lines) + "\n\n"

    header = (
        f"DATE: {date_str}\n\n"
        + landscape_block
        + f"TODAY'S FEATURED PAPERS ({len(items)} papers):\n\n"
        + papers_block
        + "\n\n"
    )

    sections: List[str] = []
    for idx, (title, instruction) in enumerate(_SYNTHESIS_SECTIONS, 1):
        user = (
            header
            + f"You are now writing SECTION {idx}: {title}\n\n"
            + instruction
            + "\n\nDo not invent details beyond what is provided. Write at least 500 words."
        )
        print(f"[synthesis] Generating section {idx}/11: {title} ...", flush=True)
        seg = _chat_complete(
            client,
            model=model,
            system=SYSTEM_SYNTHESIS_STYLE,
            user=user,
            temperature=temperature,
            max_tokens=section_max_tokens,
        ).strip()
        sections.append(seg)

    script = f"\n\n{TRANSITION_MARKER}\n\n".join(sections)

    item_segments: List[int] = [-1] * len(items)
    return script, item_segments

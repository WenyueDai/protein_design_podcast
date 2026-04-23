import os, json, requests
import time
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI
try:
    from openai import RateLimitError as _RateLimitError
except ImportError:
    _RateLimitError = Exception  # type: ignore


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
    retries: int = 5,
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
        except _RateLimitError as e:
            err = e
            # Detect a hard daily quota exhaustion — no point retrying until reset.
            err_str = str(e)
            if "per-day" in err_str or "per_day" in err_str:
                # Try to read the reset time from the response body metadata.
                reset_ms: Optional[int] = None
                try:
                    body = e.response.json()  # type: ignore[union-attr]
                    reset_ms = int(
                        body.get("error", {}).get("metadata", {})
                        .get("headers", {}).get("X-RateLimit-Reset", 0)
                    )
                except Exception:
                    pass
                if reset_ms:
                    import datetime as _dt
                    reset_utc = _dt.datetime.fromtimestamp(reset_ms / 1000, tz=_dt.timezone.utc)
                    print(
                        f"[llm] Daily free-model quota exhausted. "
                        f"Resets at {reset_utc.strftime('%Y-%m-%d %H:%M UTC')}. "
                        "Re-run after that time.",
                        flush=True,
                    )
                else:
                    print("[llm] Daily free-model quota exhausted. Re-run tomorrow.", flush=True)
                raise  # no point waiting — quota won't recover until midnight
            wait = 65 * attempt  # 65 s, 130 s, 195 s … for transient limits
            print(f"[llm] 429 rate-limit on attempt {attempt}/{retries} — waiting {wait}s …", flush=True)
            if attempt < retries:
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            err = e
            if attempt < retries:
                time.sleep(3 * attempt)
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
        lines.append("KEY RELATED LITERATURE (papers this work cites; INFLUENTIAL = this paper builds heavily on it):")
        for ref in top_refs:
            ref_title = ref.get("title") or ""
            ref_year = ref.get("year") or ""
            ref_cites = ref.get("citationCount") or 0
            ref_abstract = ref.get("abstract") or ""
            influential = ref.get("isInfluential", False)
            tag = "INFLUENTIAL, " if influential else ""
            lines.append(f"  [{tag}{ref_cites} citations, {ref_year}] {ref_title}")
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
- When you cite a paper, use its title or topic naturally woven into a sentence: "A paper on de novo binder design found..." or "The team working on antibody escape showed..."
- Use the KEY RELATED LITERATURE to add historical depth and connections: "this echoes what the AlphaFold2 paper showed about...", "building on the classic result that..."
- Be warm, curious, a little playful. Every sentence should carry a real idea.
- Ground EVERY abstract point in a CONCRETE EXAMPLE. This is the most important style rule.
  BAD (too abstract): "The model achieved strong binding and generalised well."
  GOOD (concrete): "The model hit 8 nanomolar affinity on the first round, and when they tested on a completely held-out antibody family it dropped only 2-fold — which is remarkable."
  BAD: "They used a large dataset and a sophisticated training procedure."
  GOOD: "They trained on 47 million paired sequences from the OAS database, using a masked language model objective with 15% masking."
  BAD: "The results suggest the method is effective for protein design."
  GOOD: "Seven of the ten computationally designed binders expressed well, and three bound their target with sub-100-nanomolar affinity — that's a hit rate most labs would be thrilled with."
  If a sentence contains no specific name, number, model, dataset, or experimental detail — rewrite it. Use the NOTES_FROM_PIPELINE and KEY RELATED LITERATURE data provided.

REDUNDANCY RULES — critically important for a multi-section podcast:
- Each section has a distinct purpose. Do NOT repeat what you said in conceptually earlier sections.
- If you already described what a paper found in a previous context, here just reference it briefly: "as we saw, that paper showed X — but what I want to highlight now is..." then immediately move to the new angle for this section.
- No section should re-summarise the papers from scratch. Every section builds on the last.

HARD RULES:
- Plain text only. No markdown, no asterisks, no dashes at line starts, no colons introducing structured lists.
- Do NOT invent methods, results, numbers, or author intent beyond what the provided paper data says.
- If information is genuinely missing, say it naturally: "The paper doesn't specify how they handled..."
- TTS-friendly — will be read aloud by a text-to-speech system.
- Go straight into the ideas. No catchphrases, no "Welcome back", no "In this section we will cover".

LENGTH:
- Write 280–380 words per section — tight, no filler, no redundancy with other sections.
- Every sentence must carry new information. Cut any sentence that restates something already said.
"""

# (section_title, section_instruction) — one entry per section
# Each section has a sharply distinct purpose to minimise cross-section redundancy.
_SYNTHESIS_SECTIONS: List[Tuple[str, str]] = [
    (
        "What actually mattered today",
        """This is the opening section — the listener knows nothing yet. Lay out the key scientific shifts.

Walk through the five to eight most important insights across today's papers. For each one, tell the story of the shift: what the field used to assume or do, what these new results suggest instead, and why that change of perspective actually matters. Ground every insight in a specific example from the paper — a particular experiment, a concrete number, a named model or dataset. Which papers produced this insight? What does it actually change for someone doing protein or antibody design today?

Focus on insights that change how a scientist should THINK, not just what they should know. Start the episode directly in the ideas."""
    ),
    (
        "If I were designing a project inspired by today's papers",
        """This section is purely forward-looking. Do NOT re-describe what the papers found — assume the listener already knows from section one. Instead, ask: what do these results make you want to do next?

Propose three to five concrete research project ideas. For each, think aloud about the core hypothesis, the smallest experiment or computation that would test it (be specific — name the assay, the model, the computational pipeline), what success looks like with numbers, and what single early result would tell you to stop. Be honest about risk level.

Use specific details from the papers' methods as springboards — "the approach they used to generate binders could be adapted to..." rather than generic ideas."""
    ),
    (
        "Knowledge expansion — connecting to broader science",
        """This section is about depth of understanding, not recapping what papers did. Do NOT re-summarise the papers' findings — reference them only briefly to ground a bigger point.

For the most important insights, trace their deeper roots: connections to protein physics, evolution, thermodynamics, information theory, statistical mechanics, or machine learning theory. What earlier scientific work does this resemble? Use the KEY RELATED LITERATURE provided — "this echoes what the AlphaFold2 paper showed about contact maps...", "similar to the free energy landscape arguments from the 1990s...". Are these groups rediscovering an old idea with better tools? What general principle does this reflect?

Help build conceptual intuition. Every paragraph should contain at least one reference to related work or a principle from another field."""
    ),
    (
        "Clever methods and how they proved things",
        """This section is exclusively about experimental and computational cleverness — not about what was found, but about HOW it was proven. Do NOT repeat the results you covered in section one.

Pick the two or three most interesting methods. For each: what specific question was this method designed to answer, why would a naive approach fail (give a concrete example of what could go wrong), what controls or orthogonal validations make the result convincing, and what you'd steal for your own work. Use specific details — the exact benchmark, the particular ablation, the number of validation experiments.

Look for adversarial testing, clever dataset splits, causal inference tricks, stress tests that could have failed but didn't."""
    ),
    (
        "New design heuristics I should adopt",
        """This section extracts practical judgment rules — not summaries of papers, not general advice, but specific if-then heuristics a designer can actually use.

For each heuristic: state the rule concretely (if X happens, do Y; avoid trusting Z when W is true; this metric only works as a negative filter). Give a specific example from today's papers that demonstrates it. Explain when the rule breaks down and what warning sign tells you you're in the failure case. How does it apply in antibody or protein design specifically?

Be direct and opinionated. Do not give generic advice like "validate your results". Extract the specific, surprising, grounded rule that today's papers actually demonstrate."""
    ),
    (
        "Where the field might be heading",
        """This section is entirely future-facing — not what the papers did, but what they signal about where biology and machine learning are heading together.

Identify two to four emerging directions suggested by today's papers. For each: describe the signal you're seeing with a specific example from today's work, distinguish real momentum from hype (be honest about which this is), name what evidence would confirm the direction is real over the next two to three years, and what you'd watch for.

Do not repeat paper summaries. Use them only as evidence for a trend argument."""
    ),
    (
        "Tensions, contradictions, and healthy skepticism",
        """This section is deliberately critical — it protects the listener from being swept up in hype. Do NOT be positive here. Look for problems.

Find places where today's papers disagree with each other, or where individual results rest on fragile assumptions. For each: what exactly is the fragile assumption or the contradiction (be specific — cite a number, a benchmark, a claimed result), what dataset bias or favorable test case could explain the result, what validation step is conspicuously missing, and what it would take to make you genuinely confident.

Be specific and cite the papers. "They claimed X but the test set only contained Y, which means Z is a plausible alternative explanation" is good. "Results may not generalise" is not."""
    ),
    (
        "What an expert protein designer would notice",
        """This section surfaces non-obvious readings that only come with deep domain experience. Do NOT repeat the surface-level findings from section one.

For two to four observations: what would most readers take away from this paper, and what would an expert actually notice instead. Examples of what to look for: confidence metrics that are probably inflated, hidden assumptions the authors didn't flag, a failure mode visible in a supplementary figure, a result that accidentally reveals a useful design rule while making a different point, a benchmark that favours the method unfairly.

Be specific. Name the figure, the number, the claim. Connect each observation to why it matters for design decisions."""
    ),
    (
        "History and philosophy of science perspective",
        """This section steps all the way back — not to the papers' results, but to what kind of scientific event today represents.

For each major idea, ask: is this incremental engineering, a conceptual shift, a tool-driven discovery, a data-driven empirical pattern, or a theory-driven prediction that was verified? Give a specific historical parallel — "this resembles how NMR revealed protein dynamics in the 1980s...", "similar to when the first crystal structure of a G-protein-coupled receptor changed the field by...". Use the KEY RELATED LITERATURE where relevant.

Reflect on whether this changes how protein design science is done, or just what is known. Is the field in a phase of paradigm building or paradigm shift? Be opinionated."""
    ),
    (
        "Today's mental model update",
        """This is the most practically important section. Do NOT re-describe the papers — assume everything has been covered. Speak directly to what you personally would update.

Walk through five specific things worth revising in your mental model of protein or antibody design — not generic lessons, but concrete updates ("I used to think X was the bottleneck; now I think it's Y because of what the de novo binder paper showed about Z"). Then five concrete experiments or workflow changes you'd actually run or try, with enough specificity to be actionable. Then three things these papers make you want to question more carefully.

End with the single most non-obvious insight from today, and the most elegant scientific idea you encountered."""
    ),
    (
        "Personal research expansion notes",
        """Close the episode with open-ended prompts for continued thinking — not summaries, not conclusions, but questions and threads worth following.

Generate genuine open questions today's papers raise but don't answer. Suggest specific follow-up reading directions (using the KEY RELATED LITERATURE as starting points). Propose metrics worth tracking, failure modes worth building intuitions about, datasets worth knowing, and computational experiments worth running later.

This is intellectually generous and genuinely exploratory. Help the listener leave with more threads to pull on than they arrived with."""
    ),
]


def build_podcast_script_llm_synthesis(
    *,
    date_str: str,
    items: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    shared_landscape: Optional[List[Dict]] = None,
    recommendations: Optional[List[Dict]] = None,
) -> Tuple[str, List[int]]:
    """
    Generate a deep 11-section synthesis podcast from the top featured papers.

    Makes one LLM call per section so each gets its own token budget and the
    model can't shortcut the whole script in a single lazy pass.

    shared_landscape: foundational papers cited by multiple featured papers today.
    recommendations: papers S2 recommends based on today's featured set —
      related work the pipeline didn't collect, injected as context.

    All featured items map to segment -1.
    Returns (script_text, item_segments).
    """
    client = _client_from_config(cfg)
    model = cfg["llm"]["model"]
    temperature = float(cfg["llm"].get("temperature", 0.25))

    podcast_cfg = cfg.get("podcast") or {}
    # Per-section token budget — 700 tokens ≈ 350 words ≈ ~2.5 min narration (11 sections ≈ 30 min)
    section_max_tokens = int(podcast_cfg.get("synthesis_section_max_tokens", 700))

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

    recommendations_block = ""
    if recommendations:
        lines = [
            "S2 RECOMMENDED PAPERS (related work S2 found based on today's featured set —"
            " use these to expand context, spot gaps, and generate hypotheses):"
        ]
        for rec in recommendations:
            rec_title = (rec.get("title") or "").strip()
            rec_year = rec.get("year") or ""
            rec_cites = rec.get("citationCount") or 0
            rec_authors = ", ".join(
                (a.get("name") or "") for a in (rec.get("authors") or [])[:3]
            )
            rec_abstract = (rec.get("abstract") or "").strip()[:300]
            lines.append(f"  [{rec_cites} citations, {rec_year}] {rec_title}")
            if rec_authors:
                lines.append(f"    Authors: {rec_authors}")
            if rec_abstract:
                lines.append(f"    Abstract: {rec_abstract}")
        recommendations_block = "\n".join(lines) + "\n\n"

    header = (
        f"DATE: {date_str}\n\n"
        + landscape_block
        + recommendations_block
        + f"TODAY'S FEATURED PAPERS ({len(items)} papers):\n\n"
        + papers_block
        + "\n\n"
    )

    sections: List[str] = []
    for idx, (title, instruction) in enumerate(_SYNTHESIS_SECTIONS, 1):
        prior_note = (
            f"Sections 1 through {idx - 1} have already been delivered to the listener."
            " Do not repeat what was already covered — build on it and bring a new angle."
            if idx > 1 else ""
        )
        user = (
            header
            + f"You are now writing SECTION {idx}/11: {title}\n\n"
            + instruction
            + (f"\n\n{prior_note}" if prior_note else "")
            + "\n\nUse concrete examples and specific numbers from the paper data. Do not invent details. Write 280–380 words — dense and specific, zero filler."
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

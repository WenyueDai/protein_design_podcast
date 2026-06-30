import hashlib
import os
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from openai import OpenAI
try:
    from openai import RateLimitError as _RateLimitError
    from openai import NotFoundError as _NotFoundError
    from openai import InternalServerError as _InternalServerError
except ImportError:
    _RateLimitError = Exception  # type: ignore
    _NotFoundError = Exception  # type: ignore
    _InternalServerError = Exception  # type: ignore

# Cap on how many auto-discovered (unvetted) free models to try once the
# static config.yaml fallback chain is exhausted, so a bad run can't loop
# through dozens of models and blow past the job timeout.
DISCOVERY_FALLBACK_LIMIT = 5

# Anchor to the repo root so this works regardless of cwd
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "article_analysis"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Module-level client singleton — created lazily on first use
_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
        )
    return _client

SYSTEM_PROMPT = """
You are a rigorous scientific analyst for a podcast research pipeline.

Given article text, return plain text with these exact sections:

CORE CLAIM:
METHOD / APPROACH:
KEY EVIDENCE:
WHY IT MATTERS:
LIMITATIONS / UNCERTAINTIES:
TERMS (simple explanations):

Rules:
- Be specific and evidence-grounded.
- If a detail is missing, explicitly write: "Not stated in source text".
- Do NOT fabricate results, datasets, numbers, or author intent.
- Keep it concise and information-dense.
"""
DEBUG_MODE = os.environ.get("DEBUG", "false").lower() == "true"


def hash_url(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def _is_daily_quota(e: Exception) -> bool:
    s = str(e)
    return "per-day" in s or "per_day" in s


def _try_one_model(client: OpenAI, model: str, url: str, text: str, max_attempts: int = 3) -> str:
    """Attempt analysis with a single model; up to max_attempts retries on transient 429s."""
    err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"URL: {url}\n\nARTICLE:\n{text[:12000]}"}
                ],
                temperature=0.1,
                max_tokens=900,
            )
            if not response.choices:
                raise ValueError(f"Model {model!r} returned empty choices (null response)")
            return (response.choices[0].message.content or "").strip()
        except _NotFoundError:
            # 404 — model removed from OpenRouter, no point retrying
            print(f"[analysis] 404 model not found: {model!r} — skipping", flush=True)
            raise
        except _InternalServerError:
            # 503 "no healthy upstream" — provider down, no point retrying
            print(f"[analysis] 503 provider error on {model!r} — skipping", flush=True)
            raise
        except _RateLimitError as e:
            err = e
            if _is_daily_quota(e):
                raise  # hard daily limit — propagate immediately
            # Respect the retry_after hint from the provider if available
            retry_after = 20
            try:
                body = e.response.json()  # type: ignore[union-attr]
                retry_after = int(body.get("error", {}).get("metadata", {}).get("retry_after_seconds", 20))
            except Exception:
                pass
            wait = max(retry_after + 5, 20 * attempt)
            print(f"[analysis] 429 on {model} attempt {attempt}/{max_attempts} — waiting {wait}s …", flush=True)
            if attempt < max_attempts:
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            err = e
            if attempt < max_attempts:
                time.sleep(3 * attempt)
            else:
                raise
    raise err  # pragma: no cover


def analyze_article(
    url: str,
    text: str,
    model: str = "nvidia/nemotron-3-super-120b-a12b:free",
    fallback_models: Optional[List[str]] = None,
) -> str:
    text = (text or "").strip()
    if not text:
        return ""

    cache_file = CACHE_DIR / f"{hash_url(url)}.txt"

    # Cache hit — skip API call entirely
    if not DEBUG_MODE and cache_file.exists():
        return cache_file.read_text(encoding="utf-8")

    client = _get_client()
    all_models = [model] + (fallback_models or [])
    last_err: Optional[Exception] = None

    for m in all_models:
        try:
            analysis = _try_one_model(client, m, url, text)
            if m != model:
                print(f"[analysis] Used fallback model {m!r} (primary {model!r} failed)", flush=True)
            cache_file.write_text(analysis, encoding="utf-8")
            return analysis
        except Exception as e:
            print(f"[analysis] Model {m!r} failed: {e}", flush=True)
            last_err = e

    # Static chain exhausted — config.yaml's fallback list inevitably goes
    # stale as OpenRouter's free tier churns, so try whatever's currently
    # live before giving up on this article entirely.
    from src.processing.model_discovery import get_live_free_models
    tried = set(all_models)
    live = [m for m in get_live_free_models() if m not in tried]
    discovered, skipped = live[:DISCOVERY_FALLBACK_LIMIT], live[DISCOVERY_FALLBACK_LIMIT:]
    if discovered:
        print(f"[analysis] Static fallback chain exhausted — trying {len(discovered)} auto-discovered free model(s)", flush=True)
    if skipped:
        print(f"[analysis] Skipping {len(skipped)} further auto-discovered model(s) to bound retry time: {skipped}", flush=True)
    for m in discovered:
        try:
            # Single attempt per discovered model — unvetted last resort.
            analysis = _try_one_model(client, m, url, text, max_attempts=1)
            print(f"[analysis] Used auto-discovered fallback model {m!r}", flush=True)
            cache_file.write_text(analysis, encoding="utf-8")
            return analysis
        except Exception as e:
            print(f"[analysis] Auto-discovered model {m!r} failed: {e}", flush=True)
            last_err = e

    # All models failed — return empty string so the paper is still included
    # without analysis rather than crashing the whole pipeline
    print(f"[analysis] All models failed for {url} — continuing without analysis", flush=True)
    return ""

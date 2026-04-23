import hashlib
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional
from openai import OpenAI
try:
    from openai import RateLimitError as _RateLimitError
except ImportError:
    _RateLimitError = Exception  # type: ignore

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


def analyze_article(url: str, text: str, model: str = "stepfun/step-3.5-flash:free") -> str:
    text = (text or "").strip()
    if not text:
        return ""

    cache_file = CACHE_DIR / f"{hash_url(url)}.txt"

    # Cache check
    if not DEBUG_MODE and cache_file.exists():
        return cache_file.read_text()

    client = _get_client()

    for attempt in range(1, 4):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"URL: {url}\n\nARTICLE:\n{text[:12000]}"}
                ],
                temperature=0.1,
                max_tokens=900
            )
            break
        except _RateLimitError:
            if attempt < 3:
                time.sleep(65 * attempt)
            else:
                raise

    analysis = (response.choices[0].message.content or "").strip()
    cache_file.write_text(analysis, encoding="utf-8")
    return analysis

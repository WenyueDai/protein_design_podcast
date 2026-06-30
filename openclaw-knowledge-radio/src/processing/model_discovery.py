import json
import time
from pathlib import Path
from typing import List, Optional

import requests

# Anchor to the repo root so this works regardless of cwd
CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "free_models_cache.json"
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

CACHE_TTL_SECONDS = 6 * 3600  # avoid refetching across runs within the same day

# In-process cache so a single pipeline run only hits the API once even
# though many articles/sections may exhaust their static fallback chain.
_models_cache: Optional[List[str]] = None


def get_live_free_models() -> List[str]:
    """Return the current list of ':free' model ids on OpenRouter.

    OpenRouter adds/removes free-tier models frequently, so a static
    fallback list in config.yaml inevitably goes stale. This is the
    last-resort safety net: when every configured model has failed,
    callers ask here for whatever is *currently* free and try those too.

    Falls back to the on-disk cache (even if stale) if the live fetch
    fails, and to an empty list if there's no cache at all — callers
    must handle an empty result gracefully.
    """
    global _models_cache
    if _models_cache is not None:
        return _models_cache

    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - cached.get("fetched_at", 0) < CACHE_TTL_SECONDS:
                _models_cache = cached["models"]
                return _models_cache
        except Exception:
            pass

    try:
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        free = sorted(m["id"] for m in data if str(m.get("id", "")).endswith(":free"))
        CACHE_FILE.write_text(
            json.dumps({"fetched_at": time.time(), "models": free}),
            encoding="utf-8",
        )
        _models_cache = free
        return free
    except Exception as e:
        print(f"[model_discovery] Failed to fetch live free model list: {e}", flush=True)
        if CACHE_FILE.exists():
            try:
                cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                _models_cache = cached.get("models", [])
                return _models_cache
            except Exception:
                pass
        _models_cache = []
        return _models_cache

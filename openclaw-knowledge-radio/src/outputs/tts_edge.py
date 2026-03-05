# tts_edge.py

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

import edge_tts
import requests
from gtts import gTTS

USE_GTTS_FALLBACK = os.environ.get("USE_GTTS_FALLBACK", "true").lower() == "true"
PREFER_GTTS = os.environ.get("PREFER_GTTS", "false").lower() == "true"
# Set PREFER_KOKORO=true + run a Kokoro server (docker run -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest)
# to use Kokoro as primary TTS (higher quality, runs fully offline)
PREFER_KOKORO = os.environ.get("PREFER_KOKORO", "false").lower() == "true"
KOKORO_API_URL = os.environ.get("KOKORO_API_URL", "http://localhost:8880/v1/audio/speech")
KOKORO_VOICE = os.environ.get("KOKORO_VOICE", "bm_george")
KOKORO_SPEED = float(os.environ.get("KOKORO_SPEED", "1.35"))
_LAST_TTS_BACKEND = None
_LAST_TTS_ERROR_SUMMARY = ""
_TTS_BACKEND_COUNTS: Dict[str, int] = {"edge": 0, "kokoro": 0, "gtts": 0}

from src.utils.text import chunk_text
from src.utils.io import ensure_dir

MAX_MB = 9.5
MAX_BYTES = int(MAX_MB * 1024 * 1024)

# 防止无限递归：文本太短就不再拆（极端情况下可能仍会略超，但通常不会发生）
MIN_SPLIT_CHARS = 400

# 用于找“更自然”的切分点（尽量不把句子切碎）
SPLIT_PUNCT = [
    "\n", "。", "！", "？", ".", "!", "?",
    "；", ";", "，", ",", ":", "：",
]


def _voice_candidates(primary: str) -> List[str]:
    # fallback voices for temporary Edge endpoint/voice 403 issues
    common = [
        "en-GB-RyanNeural",
        "en-GB-SoniaNeural",
        "en-US-GuyNeural",
        "en-US-AriaNeural",
    ]
    out = [primary] + [v for v in common if v != primary]
    return out


def _normalize_edge_rate(rate: str) -> str:
    """
    edge-tts expects signed percent strings (e.g. +0%, +20%, -10%).
    Accept legacy "0%" / "20%" and normalize to signed form.
    """
    s = (rate or "").strip()
    if re.fullmatch(r"[+-]?\d+%", s):
        return s if s.startswith(("+", "-")) else f"+{s}"
    return s or "+0%"


def configured_tts_backend() -> str:
    if PREFER_KOKORO:
        return "kokoro"
    if PREFER_GTTS:
        return "gtts"
    return "edge"


def last_tts_backend() -> str:
    return _LAST_TTS_BACKEND or configured_tts_backend()


def _short_err(e: Exception) -> str:
    msg = str(e).strip() or e.__class__.__name__
    msg = msg.replace("\n", " ").replace("\r", " ")
    return msg[:220]


def last_tts_error_summary() -> str:
    return _LAST_TTS_ERROR_SUMMARY


def tts_backend_stats() -> Dict[str, Any]:
    configured = configured_tts_backend()
    total = sum(_TTS_BACKEND_COUNTS.values())
    fallback_happened = total > 0 and any(
        (k != configured and v > 0) for k, v in _TTS_BACKEND_COUNTS.items()
    )
    return {
        "configured_backend": configured,
        "last_backend": last_tts_backend(),
        "counts": {k: int(v) for k, v in _TTS_BACKEND_COUNTS.items()},
        "fallback_happened": fallback_happened,
        "fallback_reason": _LAST_TTS_ERROR_SUMMARY or "",
    }


def _save_with_kokoro_api(text: str, out_path: Path) -> bool:
    try:
        r = requests.post(
            KOKORO_API_URL,
            json={
                "input": text,
                "voice": KOKORO_VOICE,
                "speed": KOKORO_SPEED,
                "response_format": "mp3",
                "model": "kokoro",
                "stream": False,
            },
            timeout=40,
        )
        if r.status_code == 200 and r.content:
            out_path.write_bytes(r.content)
            return True
    except Exception:
        return False
    return False


async def _save_one(text: str, voice: str, rate: str, out_path: Path) -> str:
    global _LAST_TTS_ERROR_SUMMARY
    edge_rate = _normalize_edge_rate(rate)
    # Primary: Kokoro (if PREFER_KOKORO=true and server is running)
    if PREFER_KOKORO:
        if _save_with_kokoro_api(text, out_path):
            return "kokoro"

    last_err = None
    edge_errs: List[str] = []
    edge_attempts = 0
    for v in _voice_candidates(voice):
        for attempt in range(1, 4):
            edge_attempts += 1
            try:
                communicate = edge_tts.Communicate(text, v, rate=edge_rate)
                await asyncio.wait_for(communicate.save(str(out_path)), timeout=25)
                return "edge"
            except Exception as e:
                last_err = e
                edge_errs.append(f"{v}#{attempt}: {_short_err(e)}")
                await asyncio.sleep(0.8 * attempt)

    # Fallback 1: local Kokoro API (if running and not already tried)
    if not PREFER_KOKORO and _save_with_kokoro_api(text, out_path):
        _LAST_TTS_ERROR_SUMMARY = (
            f"Edge failed ({edge_attempts} attempts): "
            f"{edge_errs[-1] if edge_errs else (last_err.__class__.__name__ if last_err else 'unknown')}. "
            "Fell back to Kokoro."
        )
        return "kokoro"

    # Fallback 2: gTTS (optional)
    if USE_GTTS_FALLBACK:
        try:
            tts = gTTS(text=text, lang="en", slow=False)
            tts.save(str(out_path))
            _LAST_TTS_ERROR_SUMMARY = (
                f"Edge failed ({edge_attempts} attempts): "
                f"{edge_errs[-1] if edge_errs else (last_err.__class__.__name__ if last_err else 'unknown')}. "
                "Kokoro unavailable, fell back to gTTS."
            )
            return "gtts"
        except Exception as gtts_err:
            _LAST_TTS_ERROR_SUMMARY = (
                f"Edge failed ({edge_attempts} attempts): "
                f"{edge_errs[-1] if edge_errs else (last_err.__class__.__name__ if last_err else 'unknown')}. "
                f"gTTS also failed: {_short_err(gtts_err)}"
            )
            pass

    raise last_err


def _pick_split_point(text: str) -> int:
    """
    在文本中间附近找一个比较自然的切分点。
    找不到就硬切一半。
    """
    n = len(text)
    mid = n // 2
    if n < 2:
        return 1

    window = min(600, n // 3)  # 搜索窗口，够用且不太慢
    left = max(1, mid - window)
    right = min(n - 1, mid + window)

    # 优先找靠近 mid 的标点
    best_idx = None
    best_dist = None

    for i in range(left, right):
        if text[i] in SPLIT_PUNCT:
            dist = abs(i - mid)
            if best_idx is None or dist < best_dist:
                best_idx = i
                best_dist = dist

    if best_idx is None:
        return mid

    # 切在标点之后更自然
    return min(best_idx + 1, n - 1)


def _split_text_in_two(text: str) -> Tuple[str, str]:
    cut = _pick_split_point(text)
    a = text[:cut].strip()
    b = text[cut:].strip()

    # 兜底：如果某一半为空，就硬切
    if not a or not b:
        mid = len(text) // 2
        a = text[:mid].strip()
        b = text[mid:].strip()

    return a, b


_MIN_VALID_MP3_BYTES = 5_000  # ~0.2s of audio; anything smaller is a bad file


def _mp3_is_readable(path: Path) -> bool:
    """Return True if ffprobe can read a valid duration from the file."""
    try:
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def tts_segment_to_mp3(
    text: str,
    out_path: Path,
    voice: str,
    rate: str = "+20%",
) -> Path:
    """Convert one podcast segment to a single MP3. No chunking — segments are short.

    Skips generation if a valid file already exists (allows resume on re-run).
    Retries up to 3 times if edge-tts produces a corrupt/empty file.
    """
    global _LAST_TTS_BACKEND, _LAST_TTS_ERROR_SUMMARY
    if out_path.exists() and out_path.stat().st_size > _MIN_VALID_MP3_BYTES and _mp3_is_readable(out_path):
        _LAST_TTS_BACKEND = configured_tts_backend()
        _TTS_BACKEND_COUNTS[_LAST_TTS_BACKEND] = _TTS_BACKEND_COUNTS.get(_LAST_TTS_BACKEND, 0) + 1
        print(f"[tts] Reusing existing {out_path.name}", flush=True)
        return out_path

    text = " ".join(text.split())  # collapse newlines Edge TTS reads as long pauses
    for attempt in range(1, 4):
        out_path.unlink(missing_ok=True)
        _LAST_TTS_BACKEND = asyncio.run(_save_one(text, voice, rate, out_path))
        _TTS_BACKEND_COUNTS[_LAST_TTS_BACKEND] = _TTS_BACKEND_COUNTS.get(_LAST_TTS_BACKEND, 0) + 1
        if _LAST_TTS_BACKEND == "edge":
            _LAST_TTS_ERROR_SUMMARY = ""
        if out_path.exists() and out_path.stat().st_size > _MIN_VALID_MP3_BYTES:
            return out_path
        print(f"[tts] Attempt {attempt}: bad output for {out_path.name} "
              f"({out_path.stat().st_size if out_path.exists() else 0} bytes), retrying...", flush=True)
    raise RuntimeError(f"TTS failed to produce a valid MP3 after 3 attempts: {out_path}")


def tts_text_to_mp3_chunked(
    text: str,
    out_dir: Path,
    voice: str,
    chunk_chars: int,
    rate: str = "+20%",
) -> List[Path]:
    """
    保持原函数签名与返回格式不变：
    - 输入：text, out_dir, voice, chunk_chars
    - 输出：List[Path]，文件名 part_001.mp3, part_002.mp3...
    额外能力：
    - 若某段生成的 mp3 > 9.5MB，会自动递归拆分文本，直到每个 mp3 <= 9.5MB
    """
    ensure_dir(out_dir)

    part_files: List[Path] = []
    counter = 0

    def next_path() -> Path:
        nonlocal counter
        counter += 1
        return out_dir / f"part_{counter:03d}.mp3"

    def generate_with_size_limit(one_text: str) -> None:
        """
        递归生成：如果超过大小限制，就删文件、分裂文本、继续生成。
        """
        out_path = next_path()
        asyncio.run(_save_one(one_text, voice, rate, out_path))

        try:
            size = os.path.getsize(out_path)
        except OSError:
            # 如果生成失败/文件不存在，直接不加入列表
            return

        if size <= MAX_BYTES:
            part_files.append(out_path)
            return

        # 太大：如果文本已经很短了，避免死循环——先保留（或你也可选择 raise）
        if len(one_text) < MIN_SPLIT_CHARS:
            part_files.append(out_path)
            return

        # 删掉超限文件，拆文本再来
        try:
            os.remove(out_path)
        except OSError:
            pass
        # 注意：我们“占用了”一个 part 编号，但文件删了
        # 这会导致编号有空洞吗？不会，因为我们删的是刚生成的那个编号；
        # 但 counter 已经前进了。为避免空洞，我们可以把 counter 回退 1。
        # 这里回退可确保最终文件编号连续。
        nonlocal_counter_back()

        a, b = _split_text_in_two(one_text)
        generate_with_size_limit(a)
        generate_with_size_limit(b)

    def nonlocal_counter_back() -> None:
        nonlocal counter
        counter -= 1

    # 初次按 chunk_chars 切
    chunks = chunk_text(text, max_chars=chunk_chars)

    # 逐块生成（每块如果超限会自己继续拆）
    for ch in chunks:
        ch = ch.strip()
        if not ch:
            continue
        # Collapse internal newlines to spaces — Edge TTS treats \n as a long pause
        ch = " ".join(ch.split())
        generate_with_size_limit(ch)

    return part_files

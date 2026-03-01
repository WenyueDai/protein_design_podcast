#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import random
from pathlib import Path
from datetime import datetime, timezone
import html

# Derive paths relative to this file so the script works on any machine.
# Override any of these with environment variables if needed.
_PACKAGE_DIR = Path(__file__).resolve().parent.parent   # …/openclaw-knowledge-radio/
_REPO_ROOT = _PACKAGE_DIR.parent                        # …/openclaw-knowledge-radio (git root)

BASE_OUTPUT = Path(os.environ.get("PODCAST_OUTPUT", str(_PACKAGE_DIR / "output")))
SITE_DIR    = Path(os.environ.get("SITE_DIR",       str(_REPO_ROOT / "docs")))
AUDIO_DIR   = SITE_DIR / "audio"
RELEASE_INDEX = Path(os.environ.get("RELEASE_INDEX", str(_PACKAGE_DIR / "state" / "release_index.json")))
NOTES_FILE    = Path(os.environ.get("NOTES_FILE",    str(_PACKAGE_DIR / "state" / "paper_notes.json")))
MISSED_FILE   = Path(os.environ.get("MISSED_FILE",   str(_PACKAGE_DIR / "state" / "missed_papers.json")))


def _load_notes() -> dict:
    """Load paper_notes.json → {date: {url: note_text}}.
    Supports both legacy string format and new {note, title, source} object format."""
    if NOTES_FILE.exists():
        try:
            raw = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
            result: dict = {}
            for date, date_notes in raw.items():
                result[date] = {}
                for url, val in date_notes.items():
                    if isinstance(val, str):
                        result[date][url] = val
                    elif isinstance(val, dict):
                        result[date][url] = val.get("note", "")
            return result
        except Exception:
            return {}
    return {}

def _load_missed_papers() -> list:
    """Load missed_papers.json for baking into HTML."""
    if MISSED_FILE.exists():
        try:
            return json.loads(MISSED_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


PODCAST_TITLE = os.environ.get("PODCAST_TITLE", "Protein Design Podcast")
PODCAST_AUTHOR = os.environ.get("PODCAST_AUTHOR", "Eva Dai")
PODCAST_EMAIL = os.environ.get("PODCAST_EMAIL", "daiwenyueva@gmail.com")
PODCAST_SUMMARY = os.environ.get("PODCAST_SUMMARY", "Daily automated digest of protein design, antibody engineering & enzyme design research")
PODCAST_COVER_URL = os.environ.get("PODCAST_COVER_URL", "https://wenyuedai.github.io/protein_design_podcast/cover.svg")


def _load_release_index() -> dict:
    if RELEASE_INDEX.exists():
        try:
            return json.loads(RELEASE_INDEX.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _first_sentence(text: str) -> str:
    """Return only the first sentence of text."""
    import re
    m = re.search(r'[.!?](?:\s|$)', text)
    return text[:m.start() + 1].strip() if m else text


def _extract_highlights(script_path: Path | None, max_points: int = 5) -> list[str]:
    if not script_path or not script_path.exists():
        return []
    points: list[str] = []
    try:
        for raw in script_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip().strip("-• ")
            if not line:
                continue
            low = line.lower()
            if low.startswith("references") or low.startswith("["):
                continue
            if "http://" in low or "https://" in low:
                continue
            if len(line) < 45:
                continue
            points.append(_first_sentence(line))
            if len(points) >= max_points:
                break
    except Exception:
        return []
    return points


def discover_episodes():
    release_idx = _load_release_index()
    episodes_by_date = {}

    # Pass 1: episodes from release_index.json (works on fresh checkout / GitHub Actions)
    for date, audio_url in release_idx.items():
        mp3_name = f"podcast_{date}.mp3"
        episodes_by_date[date] = {
            "date": date,
            "title": f"Daily Podcast {date}",
            "mp3_src": None,
            "mp3_name": mp3_name,
            "mp3_size": 0,
            "audio_url": audio_url,
            "script": None,
            "script_name": None,
            "highlights": [],
            "items": [],
            "timestamps": [],
        }

    # Pass 2: enrich with local files where available (local runs)
    if BASE_OUTPUT.exists():
        for d in BASE_OUTPUT.iterdir():
            if not d.is_dir():
                continue
            date = d.name
            mp3 = d / f"podcast_{date}.mp3"
            script = d / f"podcast_script_{date}_llm.txt"
            if not script.exists():
                script = d / f"podcast_script_{date}_llm_clean.txt"
            script_path = script if script.exists() else None
            items_file = d / "episode_items.json"
            episode_items = []
            episode_timestamps = []
            if items_file.exists():
                try:
                    raw = json.loads(items_file.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        episode_items = raw  # legacy format
                    elif isinstance(raw, dict):
                        episode_items = raw.get("items", [])
                        episode_timestamps = raw.get("timestamps", [])
                except Exception:
                    pass

            # Only create entry if we have audio (local mp3 or release index)
            if not mp3.exists() and date not in episodes_by_date:
                continue

            ep = episodes_by_date.setdefault(date, {
                "date": date,
                "title": f"Daily Podcast {date}",
                "mp3_src": None,
                "mp3_name": f"podcast_{date}.mp3",
                "mp3_size": 0,
                "audio_url": release_idx.get(date, f"audio/podcast_{date}.mp3"),
                "script": None,
                "script_name": None,
                "highlights": [],
                "items": [],
                "timestamps": [],
            })
            if mp3.exists():
                ep["mp3_src"] = mp3
                ep["mp3_size"] = mp3.stat().st_size
            if script_path:
                ep["script"] = script_path
                ep["script_name"] = script_path.name
                ep["highlights"] = _extract_highlights(script_path, max_points=5)
            if episode_items:
                ep["items"] = episode_items
                ep["timestamps"] = episode_timestamps

    episodes = sorted(episodes_by_date.values(), key=lambda x: x["date"], reverse=True)
    return episodes


def generate_cover_svg(seed_text: str):
    rnd = random.Random(seed_text)
    w, h = 1400, 1400
    bg1 = f"hsl({rnd.randint(0,359)},70%,55%)"
    bg2 = f"hsl({rnd.randint(0,359)},75%,35%)"
    shapes = []
    for _ in range(18):
        cx = rnd.randint(0, w)
        cy = rnd.randint(0, h)
        r = rnd.randint(60, 260)
        color = f"hsla({rnd.randint(0,359)},85%,{rnd.randint(35,70)}%,0.45)"
        shapes.append(f"<circle cx='{cx}' cy='{cy}' r='{r}' fill='{color}'/>")

    return f"""<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' viewBox='0 0 {w} {h}'>
<defs>
  <linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
    <stop offset='0%' stop-color='{bg1}'/>
    <stop offset='100%' stop-color='{bg2}'/>
  </linearGradient>
</defs>
<rect width='100%' height='100%' fill='url(#g)'/>
{''.join(shapes)}
<rect x='80' y='980' width='1240' height='280' rx='32' fill='rgba(0,0,0,0.35)'/>
<text x='120' y='1090' fill='white' font-size='92' font-family='Arial, Helvetica, sans-serif' font-weight='700'>{html.escape(PODCAST_TITLE)}</text>
<text x='120' y='1170' fill='white' font-size='46' font-family='Arial, Helvetica, sans-serif'>Auto-generated Daily Episode</text>
</svg>"""


def render_index(episodes, all_episodes=None):
    notes = _load_notes()   # {date: {url: note_text}} — baked in for static rendering
    missed_papers = _load_missed_papers()   # baked for initial render
    cards = []
    for ep in episodes:
        s_link = f'<a href="{html.escape(ep["script_name"])}">script</a>' if ep["script_name"] else ""
        date = ep["date"]
        items = ep.get("items") or []
        rows = []
        if items:
            for idx, it in enumerate(items, 1):
                title = html.escape(it.get("title") or "Untitled")
                url = html.escape(it.get("url") or "")
                source = html.escape(it.get("source") or "")
                one_liner = html.escape(it.get("one_liner") or "")
                raw_url = it.get("url") or ""
                raw_title = it.get("title") or "Untitled"
                raw_source = it.get("source") or ""
                title_part = f'<a href="{url}" target="_blank">{title}</a>' if url else title
                source_part = f' <span class="src">— {source}</span>' if source else ""
                summary_part = f'<br><span class="summary">{one_liner}</span>' if one_liner else ""
                seg_idx = it.get("segment", -1)
                ts_val = it.get("timestamp", -1)
                ts_str = str(ts_val)
                num_cls = "num seekable" if isinstance(ts_val, (int, float)) and ts_val >= 0 else "num"
                raw_note = (notes.get(date) or {}).get(raw_url, "")
                note_disp   = "" if raw_note else ' style="display:none"'
                note_add    = ' style="display:none"' if raw_note else ""
                note_part = (
                    f'<div class="my-take">'
                    f'<div class="my-take-display"{note_disp}>'
                    f'<span class="my-take-text" data-raw="{html.escape(raw_note)}">{html.escape(raw_note)}</span>'
                    f'<button class="note-edit-btn" onclick="openNoteEdit(this)" title="Edit note">✏️</button>'
                    f'</div>'
                    f'<button class="note-add-btn"{note_add} onclick="openNoteEdit(this)">✏️ my take</button>'
                    f'<div class="my-take-editor" style="display:none">'
                    f'<textarea class="note-textarea"'
                    f' placeholder="Your expert take... paste a Notion link for a deep dive"></textarea>'
                    f'<div class="note-actions">'
                    f'<button class="note-btn note-save" onclick="saveNote(this)">Save</button>'
                    f'<button class="note-btn note-cancel"'
                    f' onclick="closeNoteEdit(this.closest(\'li\'))">Cancel</button>'
                    f'<span class="note-status"></span>'
                    f'</div></div></div>'
                )
                rows.append(
                    f'<li data-url="{html.escape(raw_url)}" data-date="{date}"'
                    f' data-seg="{seg_idx}" data-ts="{ts_str}">'
                    f'<div class="item-row">'
                    f'<span class="{num_cls}" onclick="seekTo(this,event)">[{idx}]</span>'
                    f'<label class="cb-wrap">'
                    f'<input type="checkbox" class="star-cb"'
                    f' data-url="{html.escape(raw_url)}"'
                    f' data-date="{date}"'
                    f' data-source="{html.escape(raw_source)}"'
                    f' data-title="{html.escape(raw_title[:120])}"> '
                    f'{title_part}{source_part}'
                    f'</label>'
                    f'</div>'
                    f'{summary_part}{note_part}</li>'
                )
            items_html = "".join(rows)
            section_html = (
                f'<div class="abstract">'
                f'<h3>Papers &amp; News ({len(items)}) '
                f'<span class="tip">☑ check interesting ones → Save feedback</span></h3>'
                f'<ul>{items_html}</ul>'
                f'</div>'
            )
        else:
            hl = ep.get("highlights") or []
            hl_html = "".join([f"<li>{html.escape(h)}</li>" for h in hl]) if hl else "<li>No items yet.</li>"
            section_html = f'<div class="abstract"><h3>Highlights</h3><ul>{hl_html}</ul></div>'

        cards.append(f"""
<section class='card'>
  <h2>{html.escape(ep['title'])}</h2>
  <p class='meta'>Published: {html.escape(ep['date'])} {s_link}</p>
  <audio id="audio-{html.escape(ep['date'])}" controls src="{html.escape(ep['audio_url'])}"></audio>
  <p class='speed-row'>Speed:
    <button onclick="setRate(1)">1x</button>
    <button onclick="setRate(1.2)">1.2x</button>
    <button onclick="setRate(1.5)">1.5x</button>
    <button onclick="setRate(2)">2x</button>
  </p>
  {section_html}
</section>""")

    body = "\n".join(cards) if cards else "<section class='card'><p>No episodes yet.</p></section>"

    # Archive sidebar — all episodes grouped by YYYY-MM, collapsible per month
    from collections import defaultdict
    recent_dates = {ep["date"] for ep in episodes}
    by_month: dict = defaultdict(list)
    for ep in (all_episodes or episodes):
        by_month[ep["date"][:7]].append(ep)

    sidebar_parts = []
    for ym in sorted(by_month.keys(), reverse=True):
        month_label = datetime.strptime(ym, "%Y-%m").strftime("%B %Y")
        # Most recent month open by default, others collapsed
        open_attr = " open" if ym == sorted(by_month.keys())[-1::-1][0] else ""
        links = []
        for ep in sorted(by_month[ym], key=lambda x: x["date"], reverse=True):
            audio = html.escape(ep.get("audio_url", ""))
            d = html.escape(ep["date"])
            badge = ' <span class="new-badge">✦</span>' if ep["date"] in recent_dates else ""
            links.append(
                f'<li><a href="{audio}" target="_blank">{d}</a>{badge}</li>'
            )
        sidebar_parts.append(
            f'<details{open_attr} class="month-group">'
            f'<summary>{month_label} <span class="ep-count">({len(by_month[ym])})</span></summary>'
            f'<ul>{"".join(links)}</ul>'
            f'</details>'
        )
    sidebar_html = (
        f'<aside class="sidebar" id="archive-panel">'
        f'<h3>Archive</h3>'
        f'{"".join(sidebar_parts)}'
        f'</aside>'
    )

    missed_json = json.dumps(missed_papers, ensure_ascii=False)

    return f"""<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{html.escape(PODCAST_TITLE)}</title>
<style>
:root {{ --bg:#eef7ef; --bg2:#f7f4e9; --card:#fffdf6; --text:#2d3d33; --muted:#6d7f71; --accent:#4f8f6a; --line:#dbe7d9; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:"Hiragino Sans","Noto Sans JP",Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; background:linear-gradient(160deg,var(--bg),var(--bg2)); color:var(--text); }}
.layout {{ display:flex; gap:20px; max-width:1200px; margin:0 auto; padding:28px 16px 40px; }}
.main-col {{ flex:1; min-width:0; }}
.sidebar {{ width:220px; flex-shrink:0; transition:width .25s,opacity .25s; overflow:hidden; }}
.sidebar.collapsed {{ width:0; opacity:0; pointer-events:none; }}
.sidebar h3 {{ margin:0 0 10px; font-size:.95rem; color:var(--accent); display:flex; justify-content:space-between; align-items:center; }}
.month-group {{ margin-bottom:6px; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
.month-group summary {{ padding:6px 10px; font-size:.85rem; font-weight:600; color:var(--text); cursor:pointer; list-style:none; display:flex; justify-content:space-between; align-items:center; background:var(--bg2); }}
.month-group summary::-webkit-details-marker {{ display:none; }}
.month-group[open] summary {{ border-bottom:1px solid var(--line); }}
.ep-count {{ font-weight:400; color:var(--muted); font-size:.78rem; }}
.month-group ul {{ margin:0; padding:6px 10px; list-style:none; background:var(--card); }}
.month-group li {{ margin:4px 0; font-size:.82rem; display:flex; align-items:center; gap:4px; }}
.new-badge {{ color:var(--accent); font-size:.7rem; }}
.archive-toggle {{ position:fixed; right:16px; top:50%; transform:translateY(-50%); z-index:50; background:var(--accent); color:#fff; border:none; border-radius:50%; width:38px; height:38px; font-size:1rem; cursor:pointer; box-shadow:0 2px 8px rgba(0,0,0,.2); display:flex; align-items:center; justify-content:center; }}
h1 {{ margin:0 0 6px; letter-spacing:.3px; }}
.sub {{ color:var(--muted); margin-bottom:8px; font-size:.92rem; }}
.about {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px 18px; margin-bottom:12px; font-size:.88rem; line-height:1.65; color:var(--text); }}
.about p {{ margin:0 0 8px; }}
.about p:last-child {{ margin:0; }}
.about-footer {{ margin-top:10px; padding-top:8px; border-top:1px solid var(--line); font-size:.83rem; color:var(--muted); display:flex; flex-wrap:wrap; gap:6px 12px; align-items:center; }}
.tip-row {{ font-size:.83rem; color:var(--muted); margin:0 0 14px; padding:0 2px; }}
.feature-badge {{ flex-shrink:0; font-size:.72rem; font-weight:700; padding:2px 8px; border-radius:10px; margin-top:2px; white-space:nowrap; }}
.feature-badge.open  {{ background:#d4edda; color:#155724; }}
.feature-badge.owner {{ background:#fff3cd; color:#856404; }}
.feature-badge.tip   {{ background:#cce5ff; color:#004085; }}
.owner-tools {{ margin-top:18px; }}
.owner-tools > summary {{ font-size:.83rem; color:var(--muted); cursor:pointer; padding:4px 2px; list-style:none; display:flex; align-items:center; gap:6px; }}
.owner-tools > summary::-webkit-details-marker {{ display:none; }}
.owner-tools > summary::before {{ content:'▸'; font-size:.7rem; }}
.owner-tools[open] > summary::before {{ content:'▾'; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:18px; padding:16px; margin:14px 0; box-shadow:0 10px 22px rgba(79,143,106,.12); }}
h2 {{ margin:0 0 4px; font-size:1.1rem; }}
.meta {{ color:var(--muted); margin:0 0 8px; font-size:.88rem; }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
audio {{ width:100%; margin:4px 0 6px; }}
.speed-row {{ margin:0 0 8px; font-size:.88rem; color:var(--muted); }}
.speed-row button {{ font-size:.82rem; padding:1px 7px; margin-right:3px; border:1px solid var(--line); border-radius:5px; background:var(--bg2); cursor:pointer; }}
.abstract h3 {{ margin:8px 0 5px; font-size:.95rem; color:#4c6f5a; }}
.abstract ul {{ margin:0; padding-left:0; list-style:none; }}
.abstract li {{ margin:5px 0; line-height:1.45; padding:4px 6px; border-radius:6px; transition:background .15s,border-left .15s; border-left:3px solid transparent; }}
.abstract li:hover {{ background:rgba(79,143,106,.07); }}
.abstract li.playing {{ background:rgba(79,143,106,.15); border-left:3px solid var(--accent); }}
.item-row {{ display:flex; align-items:baseline; gap:6px; }}
.cb-wrap {{ display:flex; align-items:baseline; gap:5px; cursor:pointer; flex:1; min-width:0; }}
.star-cb {{ accent-color:var(--accent); width:14px; height:14px; flex-shrink:0; cursor:pointer; display:none; }}
.owner-mode .star-cb {{ display:inline-block; }}
.num {{ color:var(--muted); font-size:.82rem; font-weight:600; min-width:28px; flex-shrink:0; }}
.num.seekable {{ color:var(--accent); cursor:pointer; }}
.num.seekable:hover {{ text-decoration:underline; }}
.src {{ color:var(--muted); font-size:.85rem; }}
.summary {{ color:var(--muted); font-size:.87rem; margin-left:48px; display:block; }}
.tip {{ font-size:.75rem; font-weight:400; color:var(--muted); }}
.feedback-bar {{ margin-top:10px; padding:10px 12px; background:var(--bg2); border:1px solid var(--line); border-radius:10px; font-size:.88rem; display:none; }}
.owner-mode .feedback-bar {{ display:block; }}
.feedback-bar button {{ padding:4px 12px; border:1px solid var(--accent); border-radius:6px; background:var(--accent); color:#fff; cursor:pointer; font-size:.85rem; margin-right:8px; }}
.feedback-bar button.sec {{ background:transparent; color:var(--accent); }}
#fb-status {{ color:var(--muted); font-size:.82rem; }}
.modal-bg {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,.4); z-index:100; align-items:center; justify-content:center; }}
.modal-bg.open {{ display:flex; }}
.modal {{ background:#fff; border-radius:14px; padding:22px; max-width:420px; width:90%; }}
.modal h3 {{ margin:0 0 10px; }}
.modal input {{ width:100%; padding:7px 10px; border:1px solid var(--line); border-radius:7px; font-size:.9rem; margin-bottom:10px; }}
.modal p {{ font-size:.82rem; color:var(--muted); margin:0 0 12px; }}
.modal .btn-row {{ display:flex; gap:8px; }}
.modal button {{ flex:1; padding:7px; border-radius:7px; border:1px solid var(--accent); cursor:pointer; font-size:.88rem; }}
.modal .save {{ background:var(--accent); color:#fff; }}
.modal .cancel {{ background:transparent; color:var(--accent); }}
/* ── My Take notes ── */
.my-take {{ margin:3px 0 0 46px; }}
.my-take-display {{ display:flex; align-items:flex-start; gap:6px; background:rgba(79,143,106,.10); border-left:3px solid var(--accent); border-radius:0 6px 6px 0; padding:5px 9px; }}
.my-take-text {{ font-size:.86rem; color:#2d4a38; flex:1; white-space:pre-wrap; word-break:break-word; }}
.my-take-text a {{ color:var(--accent); }}
.note-edit-btn {{ background:none; border:none; cursor:pointer; font-size:.8rem; color:var(--muted); padding:0 2px; flex-shrink:0; opacity:.55; }}
.note-edit-btn:hover {{ opacity:1; }}
.note-add-btn {{ background:none; border:none; cursor:pointer; font-size:.76rem; color:var(--muted); padding:1px 0; opacity:.4; }}
.note-add-btn:hover {{ opacity:1; }}
.my-take-editor {{ margin-top:4px; }}
.note-textarea {{ width:100%; min-height:60px; font-size:.86rem; border:1px solid var(--line); border-radius:6px; padding:5px 8px; resize:vertical; font-family:inherit; background:var(--bg2); color:var(--text); box-sizing:border-box; }}
.note-actions {{ margin-top:3px; display:flex; align-items:center; gap:6px; }}
.note-btn {{ font-size:.77rem; padding:2px 9px; border:1px solid var(--accent); border-radius:5px; cursor:pointer; }}
.note-save {{ background:var(--accent); color:#fff; }}
.note-cancel {{ background:transparent; color:var(--accent); }}
.note-status {{ font-size:.77rem; color:var(--muted); }}
/* ── Missed papers ── */
.missed-section {{ margin-top:18px; padding:14px 16px; background:var(--bg2); border:1px solid var(--line); border-radius:14px; }}
.missed-section h3 {{ margin:0 0 6px; font-size:.95rem; color:var(--accent); }}
.missed-section > p {{ margin:0 0 10px; font-size:.85rem; color:var(--muted); }}
.missed-form {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:12px; }}
.missed-form input {{ flex:1; min-width:180px; padding:6px 10px; border:1px solid var(--line); border-radius:7px; font-size:.88rem; background:var(--card); color:var(--text); }}
.missed-form button {{ padding:6px 14px; background:var(--accent); color:#fff; border:1px solid var(--accent); border-radius:7px; cursor:pointer; font-size:.85rem; }}
#missed-status {{ font-size:.85rem; width:100%; }}
#missed-status.ok  {{ color:#22863a; }}
#missed-status.err {{ color:#d73a49; font-weight:500; }}
.missed-item {{ display:flex; align-items:flex-start; gap:8px; padding:7px 4px; border-bottom:1px solid var(--line); font-size:.86rem; }}
.missed-item:last-child {{ border-bottom:none; }}
.missed-item-title {{ flex:1; color:var(--text); }}
.missed-item-title a {{ color:var(--accent); }}
.diag-badge {{ font-size:.73rem; padding:2px 7px; border-radius:10px; font-weight:600; white-space:nowrap; flex-shrink:0; }}
.diag-collected {{ background:#d4edda; color:#155724; }}
.diag-excluded  {{ background:#fff3cd; color:#856404; }}
.diag-source    {{ background:#cce5ff; color:#004085; }}
.diag-ranking   {{ background:#f8d7da; color:#721c24; }}
.diag-pending   {{ background:#e2e3e5; color:#383d41; }}
.missed-kws {{ font-size:.75rem; color:var(--muted); margin-top:2px; }}
.missed-toggle {{ margin-top:8px; background:none; border:1px solid var(--line); border-radius:7px; padding:4px 12px; font-size:.8rem; color:var(--accent); cursor:pointer; }}
.diag-guide {{ margin-top:12px; font-size:.83rem; color:var(--muted); }}
.diag-guide summary {{ cursor:pointer; font-weight:600; color:var(--accent); }}
.diag-guide dl {{ margin:8px 0 0; display:grid; grid-template-columns:auto 1fr; gap:6px 12px; align-items:start; }}
.diag-guide dt {{ padding-top:1px; }}
.diag-guide dd {{ margin:0; color:var(--text); }}
.diag-guide code {{ font-size:.8rem; background:var(--bg2); padding:1px 5px; border-radius:4px; }}
</style>
</head>
<body>
<button class="archive-toggle" onclick="toggleArchive()" title="Toggle archive">📚</button>
<div class="layout">
  <div class="main-col">
    <h1>Protein Design Podcast</h1>
    <div class="about">
      <p>A daily automated digest of new papers on <strong>protein design, antibody engineering, and enzyme design</strong>. A pipeline runs every morning, ranks new papers from 42 sources, and narrates them into a ~60-minute episode.</p>
      <p style="color:var(--muted); font-size:.85rem; margin-bottom:0;">&#9432; Built on free resources only — audio quality is limited. Use it to spot papers worth reading, not as a substitute for reading them.</p>
      <div class="about-footer">
        <span>&#128218; Browse older episodes in the archive sidebar</span>
        <span>&nbsp;·&nbsp;</span>
        <a href="https://clear-squid-8e3.notion.site/3155f58ea8c280258959fba00c0149ab?v=3155f58ea8c2803c8c0d000c76d1bfba" target="_blank">Paper Collection</a>
        <span>&nbsp;·&nbsp;</span>
        <a href="https://clear-squid-8e3.notion.site/3165f58ea8c280498f72c770028aec0d?v=3165f58ea8c28020983c000cec9807e6" target="_blank">Deep Dive Notes</a>
      </div>
    </div>
    <details class="owner-tools">
      <summary>&#9881;&#65039; Owner tools &mdash; add missing paper</summary>
      <div class="missed-section">
        <h3>&#128231; Submit a missed paper</h3>
        <p>Log a paper the pipeline missed — triggers an automatic diagnosis and boosts similar papers in future rankings.</p>
        <div class="missed-form">
          <input type="text" id="missed-title" placeholder="Paper title (required)">
          <input type="text" id="missed-url" placeholder="URL (optional)">
          <button onclick="submitMissedPaper()">Submit</button>
          <span id="missed-status"></span>
        </div>
        <div id="missed-list"></div>
        <details class="diag-guide">
          <summary>&#128270; Diagnosis guide</summary>
          <dl>
            <dt><span class="diag-badge diag-collected">already collected</span></dt>
            <dd>Already in a previous episode — check the archive.</dd>
            <dt><span class="diag-badge diag-excluded">excluded term</span></dt>
            <dd>Title matched a term in <code>excluded_terms</code> (e.g. &ldquo;mouse&rdquo;). Narrow the filter in <code>config.yaml</code> if too aggressive.</dd>
            <dt><span class="diag-badge diag-source">source not in RSS</span></dt>
            <dd>Domain not in any RSS feed — pipeline can&rsquo;t see it. Add to <code>rss_sources</code> or check <code>extra_rss_sources.json</code> for auto-discovered feeds.</dd>
            <dt><span class="diag-badge diag-ranking">low ranking</span></dt>
            <dd>In RSS but cut below the episode cap. Add keywords to <code>absolute_title_keywords</code> or increase <code>max_items_total</code>.</dd>
            <dt><span class="diag-badge diag-pending">pending</span></dt>
            <dd>Workflow hasn&rsquo;t run yet — diagnosis appears within ~2 minutes.</dd>
          </dl>
        </details>
      </div>
    </details>
    {body}
    <div class="feedback-bar">
      <strong>Your selections:</strong>
      <span id="sel-count">0 checked</span> &nbsp;
      <button onclick="saveFeedback()">Save feedback to GitHub</button>
      <button class="sec" onclick="openSettings()">⚙ Settings</button>
      <span id="fb-status"></span>
    </div>
  </div>
  {sidebar_html}
</div>

<!-- Settings modal -->
<div class="modal-bg" id="settings-modal">
  <div class="modal">
    <h3>GitHub Settings</h3>
    <p>Your token is stored only in this browser (localStorage). It's used to commit your paper selections back to the repo so the ranking can learn from them.</p>
    <input type="password" id="gh-token-input" placeholder="GitHub personal access token (repo scope)">
    <input type="text" id="gh-repo-input" placeholder="owner/repo  e.g. WenyueDai/protein_design_podcast">
    <div class="btn-row">
      <button class="save" onclick="saveSettings()">Save</button>
      <button class="cancel" onclick="closeSettings()">Cancel</button>
    </div>
  </div>
</div>

<script>
// ── Restore checkbox states from localStorage ──────────────────────────────
function storageKey(date) {{ return 'feedback_' + date; }}

function loadCheckboxes() {{
  document.querySelectorAll('.star-cb').forEach(cb => {{
    const date = cb.dataset.date, url = cb.dataset.url;
    const saved = JSON.parse(localStorage.getItem(storageKey(date)) || '[]');
    if (saved.includes(url)) cb.checked = true;
  }});
  updateCount();
}}

function saveCheckboxes() {{
  const byDate = {{}};
  document.querySelectorAll('.star-cb').forEach(cb => {{
    if (!byDate[cb.dataset.date]) byDate[cb.dataset.date] = [];
    if (cb.checked) byDate[cb.dataset.date].push(cb.dataset.url);
  }});
  for (const [date, urls] of Object.entries(byDate)) {{
    localStorage.setItem(storageKey(date), JSON.stringify(urls));
  }}
  updateCount();
}}

function updateCount() {{
  const n = document.querySelectorAll('.star-cb:checked').length;
  document.getElementById('sel-count').textContent = n + ' checked';
}}

document.querySelectorAll('.star-cb').forEach(cb => {{
  cb.addEventListener('change', saveCheckboxes);
}});

// ── Playback speed ────────────────────────────────────────────────────────
function setRate(v) {{ document.querySelectorAll('audio').forEach(a => a.playbackRate = v); }}

// ── Settings modal ────────────────────────────────────────────────────────
function openSettings() {{
  document.getElementById('gh-token-input').value = localStorage.getItem('gh_token') || '';
  document.getElementById('gh-repo-input').value = localStorage.getItem('gh_repo') || '{html.escape("WenyueDai/protein_design_podcast")}';
  document.getElementById('settings-modal').classList.add('open');
}}
function closeSettings() {{ document.getElementById('settings-modal').classList.remove('open'); }}
function saveSettings() {{
  localStorage.setItem('gh_token', document.getElementById('gh-token-input').value.trim());
  localStorage.setItem('gh_repo', document.getElementById('gh-repo-input').value.trim());
  closeSettings();
  _updateOwnerUI();
  setStatus('Settings saved.');
}}

// ── Show/hide owner-only UI based on token presence ───────────────────────
function _updateOwnerUI() {{
  if (localStorage.getItem('gh_token')) {{
    document.body.classList.add('owner-mode');
  }} else {{
    document.body.classList.remove('owner-mode');
  }}
}}

// ── Save feedback to GitHub ───────────────────────────────────────────────
function setStatus(msg) {{ document.getElementById('fb-status').textContent = msg; }}

async function saveFeedback() {{
  const token = localStorage.getItem('gh_token') || '';
  const repo  = localStorage.getItem('gh_repo')  || '{html.escape("WenyueDai/protein_design_podcast")}';
  if (!token) {{ openSettings(); return; }}

  // Gather checked items per date (url + source + title for smarter ranking)
  const selections = {{}};
  document.querySelectorAll('.star-cb:checked').forEach(cb => {{
    if (!selections[cb.dataset.date]) selections[cb.dataset.date] = [];
    selections[cb.dataset.date].push({{
      url: cb.dataset.url,
      source: cb.dataset.source || '',
      title: cb.dataset.title || '',
    }});
  }});
  if (!Object.keys(selections).length) {{ setStatus('Nothing checked.'); return; }}

  setStatus('Saving…');
  const path = 'openclaw-knowledge-radio/state/feedback.json';
  const apiBase = 'https://api.github.com/repos/' + repo;
  const headers = {{
    'Authorization': 'Bearer ' + token,
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'Content-Type': 'application/json',
  }};

  try {{
    // Get current file (to obtain SHA and merge existing data)
    let existing = {{}}, sha = null;
    const get = await fetch(apiBase + '/contents/' + path, {{headers}});
    if (get.ok) {{
      const meta = await get.json();
      sha = meta.sha;
      existing = JSON.parse(atob(meta.content.replace(/\\n/g,'')));
    }}

    // Merge new selections with existing
    for (const [date, urls] of Object.entries(selections)) {{
      const prev = existing[date] || [];
      existing[date] = [...new Set([...prev, ...urls])];
    }}

    const body = {{ message: 'Update feedback ' + new Date().toISOString().slice(0,10),
                    content: btoa(unescape(encodeURIComponent(JSON.stringify(existing, null, 2)))) }};
    if (sha) body.sha = sha;

    const put = await fetch(apiBase + '/contents/' + path, {{
      method: 'PUT', headers, body: JSON.stringify(body)
    }});
    if (put.ok) {{
      setStatus('✓ Saved! Ranking will improve from tomorrow.');
    }} else {{
      const err = await put.json();
      setStatus('Error: ' + (err.message || put.status));
    }}
  }} catch(e) {{ setStatus('Error: ' + e.message); }}
}}

// ── Archive toggle ────────────────────────────────────────────────────────
function toggleArchive() {{
  const panel = document.getElementById('archive-panel');
  const btn = document.querySelector('.archive-toggle');
  const collapsed = panel.classList.toggle('collapsed');
  btn.textContent = collapsed ? '📚' : '✕';
  localStorage.setItem('archive_open', collapsed ? '0' : '1');
}}
// Start collapsed by default; open if user had it open previously
(function() {{
  const panel = document.getElementById('archive-panel');
  const open = localStorage.getItem('archive_open');
  if (open !== '1') panel.classList.add('collapsed');
}})();

// ── Click [N] to seek audio to that paper's segment ──────────────────────
function seekTo(numEl, event) {{
  event.preventDefault();
  event.stopPropagation();
  const li = numEl.closest('li');
  const ts = parseFloat(li.dataset.ts);
  const date = li.dataset.date;
  const audio = document.getElementById('audio-' + date);
  if (!audio || isNaN(ts) || ts < 0) return;
  audio.currentTime = ts;
  if (audio.paused) audio.play().catch(function() {{}});
}}

// ── Highlight the paper currently being spoken ────────────────────────────
document.querySelectorAll('audio[id^="audio-"]').forEach(function(audio) {{
  audio.addEventListener('timeupdate', function() {{
    const date = this.id.slice('audio-'.length);
    const t = this.currentTime;
    let bestLi = null, bestTs = -Infinity;
    document.querySelectorAll('li[data-date="' + date + '"][data-ts]').forEach(function(li) {{
      const ts = parseFloat(li.dataset.ts);
      if (ts >= 0 && ts <= t && ts > bestTs) {{ bestTs = ts; bestLi = li; }}
    }});
    document.querySelectorAll('li[data-date="' + date + '"]').forEach(function(li) {{
      li.classList.toggle('playing', li === bestLi);
    }});
  }});
}});

loadCheckboxes();
_updateOwnerUI();

// ── My Take notes ─────────────────────────────────────────────────────────
function renderNoteHtml(text) {{
  const esc = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  return esc.replace(/\bhttps?:\/\/[^\s<>]+/g, function(url) {{
    const label = url.includes('notion') ? '→ Notion deep dive'
                : url.length > 55 ? url.slice(0,52)+'…' : url;
    return '<a href="' + url + '" target="_blank">' + label + '</a>';
  }});
}}

function _applyNote(li, note) {{
  const display = li.querySelector('.my-take-display');
  const addBtn  = li.querySelector('.note-add-btn');
  const textEl  = li.querySelector('.my-take-text');
  if (!display || !addBtn || !textEl) return;
  if (note) {{
    textEl.innerHTML = renderNoteHtml(note);
    textEl._raw = note;
    display.style.display = 'flex';
    addBtn.style.display = 'none';
  }} else {{
    display.style.display = 'none';
    addBtn.style.display = '';
  }}
}}

function _updateNoteButtons() {{
  const isOwner = !!localStorage.getItem('gh_token');
  document.querySelectorAll('.note-add-btn, .note-edit-btn').forEach(function(b) {{
    b.style.visibility = isOwner ? '' : 'hidden';
  }});
}}

async function loadNotes() {{
  const repo = localStorage.getItem('gh_repo') || '{html.escape("WenyueDai/protein_design_podcast")}';
  const path = 'openclaw-knowledge-radio/state/paper_notes.json';
  const headers = {{'Accept': 'application/vnd.github+json'}};
  const token = localStorage.getItem('gh_token');
  if (token) headers['Authorization'] = 'Bearer ' + token;
  try {{
    const res = await fetch('https://api.github.com/repos/' + repo + '/contents/' + path, {{headers: headers}});
    if (!res.ok) {{ _updateNoteButtons(); return; }}
    const data = JSON.parse(atob((await res.json()).content.replace(/\\n/g,'')));
    document.querySelectorAll('li[data-url][data-date]').forEach(function(li) {{
      const val = (data[li.dataset.date] || {{}})[li.dataset.url];
      const note = !val ? '' : (typeof val === 'string' ? val : (val.note || ''));
      _applyNote(li, note);
    }});
  }} catch(e) {{}}
  _updateNoteButtons();
}}

function openNoteEdit(btn) {{
  const li = btn.closest('li');
  const editor   = li.querySelector('.my-take-editor');
  const textarea = li.querySelector('.note-textarea');
  const textEl = li.querySelector('.my-take-text');
  textarea.value = textEl._raw || textEl.dataset.raw || '';
  editor.style.display = 'block';
  textarea.focus();
}}

function closeNoteEdit(li) {{
  li.querySelector('.my-take-editor').style.display = 'none';
}}

async function saveNote(btn) {{
  const token = localStorage.getItem('gh_token') || '';
  const repo  = localStorage.getItem('gh_repo')  || '{html.escape("WenyueDai/protein_design_podcast")}';
  if (!token) {{ openSettings(); return; }}
  const li       = btn.closest('li');
  const date     = li.dataset.date, url = li.dataset.url;
  const noteText = li.querySelector('.note-textarea').value.trim();
  const status   = li.querySelector('.note-status');
  status.textContent = 'Saving…';
  const path = 'openclaw-knowledge-radio/state/paper_notes.json';
  const apiBase = 'https://api.github.com/repos/' + repo;
  const headers = {{
    'Authorization': 'Bearer ' + token,
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'Content-Type': 'application/json',
  }};
  try {{
    let existing = {{}}, sha = null;
    const get = await fetch(apiBase + '/contents/' + path, {{headers: headers}});
    if (get.ok) {{
      const meta = await get.json();
      sha = meta.sha;
      existing = JSON.parse(atob(meta.content.replace(/\\n/g,'')));
    }}
    if (!existing[date]) existing[date] = {{}};
    if (noteText) {{
      const cb = li.querySelector('.star-cb');
      existing[date][url] = {{
        note: noteText,
        title: (cb && cb.dataset.title) || '',
        source: (cb && cb.dataset.source) || '',
      }};
    }} else delete existing[date][url];
    const body = {{
      message: 'Note: ' + date,
      content: btoa(unescape(encodeURIComponent(JSON.stringify(existing, null, 2))))
    }};
    if (sha) body.sha = sha;
    const put = await fetch(apiBase + '/contents/' + path, {{
      method: 'PUT', headers: headers, body: JSON.stringify(body)
    }});
    if (put.ok) {{
      _applyNote(li, noteText);
      closeNoteEdit(li);
      status.textContent = '✓ Saved';
      setTimeout(function() {{ status.textContent = ''; }}, 2000);
    }} else {{
      status.textContent = 'Error: ' + ((await put.json()).message || put.status);
    }}
  }} catch(e) {{ status.textContent = 'Error: ' + e.message; }}
}}

loadNotes();

// ── Missed papers ──────────────────────────────────────────────────────────
var _bakedMissedPapers = {missed_json};

function _diagLabel(entry) {{
  var d = entry.diagnosis;
  if (!d) return '<span class="diag-badge diag-pending">pending</span>';
  if (d === 'already_collected') return '<span class="diag-badge diag-collected">already collected</span>';
  if (d === 'excluded_term')     return '<span class="diag-badge diag-excluded">excluded term</span>';
  if (d === 'source_not_in_rss') return '<span class="diag-badge diag-source">source not in RSS</span>';
  if (d === 'low_ranking')       return '<span class="diag-badge diag-ranking">low ranking</span>';
  return '<span class="diag-badge diag-pending">' + d + '</span>';
}}

function _missedItemHtml(p) {{
  var titleHtml = p.url
    ? '<a href="' + p.url + '" target="_blank">' + p.title.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</a>'
    : p.title.replace(/&/g,'&amp;').replace(/</g,'&lt;');
  var kwHtml = (p.keywords_added && p.keywords_added.length)
    ? '<div class="missed-kws">Keywords added: ' + p.keywords_added.join(', ') + '</div>'
    : '';
  return '<div class="missed-item">'
    + '<div class="missed-item-title">' + titleHtml + kwHtml + '</div>'
    + _diagLabel(p)
    + '</div>';
}}

function _toggleMissedMore(btn, extra) {{
  var m = document.getElementById('missed-more');
  var expanded = m.style.display !== 'none';
  m.style.display = expanded ? 'none' : '';
  btn.textContent = expanded ? 'Show all (' + extra + ' more)' : 'Show less';
}}

function _renderMissedList(papers) {{
  var list = document.getElementById('missed-list');
  if (!list) return;
  if (!papers || !papers.length) {{ list.innerHTML = ''; return; }}
  var all = papers.slice().reverse();
  var html = '';
  for (var i = 0; i < Math.min(3, all.length); i++) html += _missedItemHtml(all[i]);
  if (all.length > 3) {{
    var extra = all.length - 3;
    html += '<div id="missed-more" style="display:none">';
    for (var i = 3; i < all.length; i++) html += _missedItemHtml(all[i]);
    html += '</div>';
    html += '<button class="missed-toggle" onclick="_toggleMissedMore(this,' + extra + ')">Show all (' + extra + ' more)</button>';
  }}
  list.innerHTML = html;
}}

async function loadMissedPapers() {{
  // Render baked data immediately
  _renderMissedList(_bakedMissedPapers);

  // Then try to fetch fresh data from GitHub
  var repo = localStorage.getItem('gh_repo') || '{html.escape("WenyueDai/protein_design_podcast")}';
  var path = 'openclaw-knowledge-radio/state/missed_papers.json';
  var headers = {{'Accept': 'application/vnd.github+json'}};
  var token = localStorage.getItem('gh_token');
  if (token) headers['Authorization'] = 'Bearer ' + token;
  try {{
    var res = await fetch('https://api.github.com/repos/' + repo + '/contents/' + path, {{headers: headers}});
    if (res.ok) {{
      var data = JSON.parse(atob((await res.json()).content.replace(/\\n/g,'')));
      _renderMissedList(data);
    }}
  }} catch(e) {{}}
}}

function _setStatus(el, msg, isErr) {{
  el.textContent = msg;
  el.className = isErr ? 'err' : 'ok';
}}

async function submitMissedPaper() {{
  var token = localStorage.getItem('gh_token') || '';
  var repo  = localStorage.getItem('gh_repo')  || '{html.escape("WenyueDai/protein_design_podcast")}';

  var titleEl = document.getElementById('missed-title');
  var urlEl   = document.getElementById('missed-url');
  var status  = document.getElementById('missed-status');
  var title = (titleEl.value || '').trim();
  var url   = (urlEl.value || '').trim();

  if (!token) {{
    _setStatus(status, 'Set your GitHub token in ⚙ Settings to submit.', true);
    return;
  }}
  if (!title) {{ _setStatus(status, 'Please enter a paper title.', true); return; }}

  var path = 'openclaw-knowledge-radio/state/missed_papers.json';
  var apiBase = 'https://api.github.com/repos/' + repo;
  var headers = {{
    'Authorization': 'Bearer ' + token,
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'Content-Type': 'application/json',
  }};

  _setStatus(status, 'Saving…', false);
  try {{
    var existing = [], sha = null;
    var get = await fetch(apiBase + '/contents/' + path, {{headers: headers}});
    if (get.ok) {{
      var meta = await get.json();
      sha = meta.sha;
      existing = JSON.parse(atob(meta.content.replace(/\\n/g,'')));
    }}

    // Duplicate check (case-insensitive title match)
    var titleLower = title.toLowerCase();
    for (var i = 0; i < existing.length; i++) {{
      if ((existing[i].title || '').toLowerCase() === titleLower) {{
        _setStatus(status, 'Already submitted — thanks!', false);
        return;
      }}
    }}

    var entry = {{
      id: Date.now().toString(),
      title: title,
      url: url || null,
      date_submitted: new Date().toISOString().slice(0, 10),
      processed: false,
      diagnosis: null,
      keywords_added: []
    }};
    existing.push(entry);

    var body = {{
      message: 'Missed paper: ' + title.slice(0, 60),
      content: btoa(unescape(encodeURIComponent(JSON.stringify(existing, null, 2))))
    }};
    if (sha) body.sha = sha;

    var put = await fetch(apiBase + '/contents/' + path, {{
      method: 'PUT', headers: headers, body: JSON.stringify(body)
    }});
    if (put.ok) {{
      _setStatus(status, '✓ Submitted! Processing triggered — refresh in ~2 minutes to see diagnosis.', false);
      titleEl.value = '';
      urlEl.value = '';
      _renderMissedList(existing);
      // Auto-refresh missed list after 2 min to show diagnosis from workflow
      setTimeout(function() {{ loadMissedPapers(); }}, 120000);
    }} else {{
      var err = await put.json();
      _setStatus(status, 'Error: ' + (err.message || put.status), true);
    }}
  }} catch(e) {{ _setStatus(status, 'Error: ' + e.message, true); }}
}}

loadMissedPapers();
</script>
</body>
</html>
"""


def render_feed(episodes, site_url: str):
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    items = []
    for ep in episodes[:60]:
        pub = datetime.strptime(ep["date"], "%Y-%m-%d").strftime("%a, %d %b %Y 08:00:00 GMT")
        mp3_url = ep.get("audio_url") or f"{site_url}/audio/{ep['mp3_name']}"
        if mp3_url.startswith("audio/"):
            mp3_url = f"{site_url}/{mp3_url}"
        mp3_len = ep.get("mp3_size", 0)
        highlights = ep.get("highlights") or []
        abstract = " | ".join(highlights[:3]) if highlights else PODCAST_SUMMARY
        items.append(f"""
    <item>
      <title>{html.escape(ep['title'])}</title>
      <guid isPermaLink="false">{mp3_url}</guid>
      <pubDate>{pub}</pubDate>
      <enclosure url=\"{mp3_url}\" length=\"{mp3_len}\" type=\"audio/mpeg\" />
      <description>{html.escape(abstract)}</description>
      <itunes:author>{html.escape(PODCAST_AUTHOR)}</itunes:author>
      <itunes:summary>{html.escape(abstract)}</itunes:summary>
      <itunes:explicit>false</itunes:explicit>
      <itunes:image href=\"{PODCAST_COVER_URL}\" />
    </item>""")
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<rss version='2.0'
     xmlns:itunes='http://www.itunes.com/dtds/podcast-1.0.dtd'
     xmlns:atom='http://www.w3.org/2005/Atom'>
  <channel>
    <title>{html.escape(PODCAST_TITLE)}</title>
    <link>{site_url}</link>
    <atom:link href="{site_url}/feed.xml" rel="self" type="application/rss+xml" />
    <description>{html.escape(PODCAST_SUMMARY)}</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>{html.escape(PODCAST_AUTHOR)}</itunes:author>
    <itunes:summary>{html.escape(PODCAST_SUMMARY)}</itunes:summary>
    <itunes:owner>
      <itunes:name>{html.escape(PODCAST_AUTHOR)}</itunes:name>
      <itunes:email>{html.escape(PODCAST_EMAIL)}</itunes:email>
    </itunes:owner>
    <itunes:image href="{PODCAST_COVER_URL}" />
    <itunes:explicit>false</itunes:explicit>
    {''.join(items)}
  </channel>
</rss>
"""


def main():
    site_url = "https://wenyuedai.github.io/openclaw_podcast"
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    episodes = discover_episodes()

    # Web page shows only the 3 most recent episodes; RSS feed keeps all
    WEB_EPISODES = 3
    web_episodes = episodes[:WEB_EPISODES]

    # generate a random-ish cover each day (seeded by latest episode date)
    cover_seed = episodes[0]["date"] if episodes else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (SITE_DIR / "cover.svg").write_text(generate_cover_svg(cover_seed), encoding="utf-8")

    # Copy script txt files for web episodes only; remove stale ones
    web_script_names = {ep["script_name"] for ep in web_episodes if ep["script_name"]}
    for ep in web_episodes:
        if ep["script"]:
            (SITE_DIR / ep["script_name"]).write_text(
                ep["script"].read_text(encoding="utf-8"), encoding="utf-8"
            )
    for f in SITE_DIR.glob("podcast_script_*.txt"):
        if f.name not in web_script_names:
            f.unlink()

    # remove stale local audio files (only matters if audio is stored locally)
    keep_audio = set()
    for ep in web_episodes:
        audio_url = ep.get("audio_url", "")
        is_remote = audio_url.startswith("http://") or audio_url.startswith("https://")
        if not is_remote and ep.get("mp3_src"):
            (AUDIO_DIR / ep["mp3_name"]).write_bytes(ep["mp3_src"].read_bytes())
            keep_audio.add(ep["mp3_name"])
    for f in AUDIO_DIR.glob("*.mp3"):
        if f.name not in keep_audio:
            f.unlink()

    (SITE_DIR / "episodes.json").write_text(json.dumps([
        {"date": e["date"], "title": e["title"], "audio": e.get("audio_url", f"audio/{e['mp3_name']}"), "script": e["script_name"]}
        for e in web_episodes
    ], indent=2), encoding="utf-8")

    (SITE_DIR / "index.html").write_text(render_index(web_episodes, all_episodes=episodes), encoding="utf-8")
    (SITE_DIR / "feed.xml").write_text(render_feed(episodes, site_url), encoding="utf-8")
    print(f"Built site with {len(web_episodes)} episode(s) shown (of {len(episodes)} total): {SITE_DIR}")


if __name__ == "__main__":
    main()

"""
GitHub Release upload + GitHub Pages publish.

Called automatically at the end of run_daily.py when publish.enabled is true.

Required env var:
  GITHUB_TOKEN  — a personal access token with `repo` scope
                  (Settings → Developer settings → Personal access tokens)

Config (config.yaml publish: section):
  github_release_repo   e.g. "WenyueDai/openclaw_podcast"
  cleanup_intermediate  delete tts_parts/ + temp files after audio merge
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import requests


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def upload_episode(
    date: str,
    mp3_path: Path,
    script_path: Path,
    *,
    repo: str,
    state_dir: Path,
) -> Optional[str]:
    """Upload MP3 + clean script to a GitHub Release.

    Returns the MP3 browser download URL, or None if skipped/failed.
    Updates state/release_index.json so build_site.py can embed the URL.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("[publish] GITHUB_TOKEN not set — skipping release upload", flush=True)
        return None

    api_base = f"https://api.github.com/repos/{repo}"
    hdrs = _headers(token)
    tag = f"episode-{date}"

    # --- Get or create release ---
    r = requests.get(f"{api_base}/releases/tags/{tag}", headers=hdrs, timeout=30)
    if r.status_code == 200:
        release = r.json()
        print(f"[publish] Release {tag} already exists", flush=True)
    else:
        r = requests.post(
            f"{api_base}/releases",
            headers=hdrs,
            json={
                "tag_name": tag,
                "name": f"Episode {date}",
                "body": f"Daily science podcast — {date}",
            },
            timeout=30,
        )
        r.raise_for_status()
        release = r.json()
        print(f"[publish] Created release {tag}", flush=True)

    release_id = release["id"]
    upload_url_base = (
        f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets"
    )

    # Check existing assets
    assets_r = requests.get(
        f"{api_base}/releases/{release_id}/assets", headers=hdrs, timeout=30
    )
    existing_assets: list = assets_r.json() if assets_r.ok else []
    existing_ids: dict[str, int] = {a["name"]: a["id"] for a in existing_assets}

    # If MP3 already exists and FORCE_REPUBLISH is not set, preserve original episode
    force = os.environ.get("FORCE_REPUBLISH", "").strip().lower() in ("1", "true", "yes")
    if not force:
        for asset in existing_assets:
            if asset["name"].endswith(".mp3"):
                existing_mp3_url: str = asset["browser_download_url"]
                print(
                    f"[publish] MP3 already uploaded ({asset['name']}), "
                    "skipping re-upload (set FORCE_REPUBLISH=true to override)",
                    flush=True,
                )
                index_file = state_dir / "release_index.json"
                index: dict = {}
                if index_file.exists():
                    try:
                        index = json.loads(index_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                if index.get(date) != existing_mp3_url:
                    index[date] = existing_mp3_url
                    index_file.write_text(
                        json.dumps(index, indent=2, sort_keys=True), encoding="utf-8"
                    )
                    print("[publish] Updated release_index.json", flush=True)
                return existing_mp3_url

    # --- Upload MP3 + script (replace if already present) ---
    mp3_url: Optional[str] = None
    for fpath in [mp3_path, script_path]:
        if not fpath or not fpath.exists():
            continue
        if fpath.name in existing_ids:
            # Delete old asset first so we can re-upload fresh version
            requests.delete(
                f"{api_base}/releases/assets/{existing_ids[fpath.name]}",
                headers=hdrs, timeout=30,
            )
            print(f"[publish] Replaced existing asset: {fpath.name}", flush=True)

        ctype = "audio/mpeg" if fpath.suffix == ".mp3" else "text/plain; charset=utf-8"
        with fpath.open("rb") as f:
            up = requests.post(
                upload_url_base,
                params={"name": fpath.name},
                headers={**hdrs, "Content-Type": ctype},
                data=f,
                timeout=300,
            )
        if up.status_code in (200, 201):
            print(f"[publish] Uploaded {fpath.name}", flush=True)
            if fpath.suffix == ".mp3":
                mp3_url = up.json()["browser_download_url"]
        else:
            print(
                f"[publish] Warning: upload failed for {fpath.name} "
                f"({up.status_code}: {up.text[:200]})",
                flush=True,
            )

    # --- Update release_index.json ---
    if mp3_url:
        index_file = state_dir / "release_index.json"
        index: dict = {}
        if index_file.exists():
            try:
                index = json.loads(index_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        index[date] = mp3_url
        index_file.write_text(
            json.dumps(index, indent=2, sort_keys=True), encoding="utf-8"
        )
        print("[publish] Updated release_index.json", flush=True)

    return mp3_url


def push_site(package_dir: Path, git_root: Path, date: str) -> bool:
    """Rebuild docs/ with build_site.py then commit + push to GitHub Pages."""
    build_script = package_dir / "tools" / "build_site.py"
    venv_python = package_dir / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable

    # Rebuild static site
    result = subprocess.run(
        [python, str(build_script)],
        cwd=package_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[publish] build_site.py failed:\n{result.stderr}", flush=True)
        return False
    print(result.stdout.strip(), flush=True)

    # Commit docs/ + updated release_index.json
    rel_state = Path("openclaw-knowledge-radio") / "state" / "release_index.json"

    # Embed token in remote URL for push, then restore plain URL afterwards
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    def _remote_url() -> str:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=git_root,
                           capture_output=True, text=True)
        return r.stdout.strip()

    original_url = _remote_url()
    if token and "github.com" in original_url and "@" not in original_url:
        authed_url = original_url.replace("https://", f"https://x-access-token:{token}@")
        subprocess.run(["git", "remote", "set-url", "origin", authed_url],
                       cwd=git_root, capture_output=True)

    try:
        subprocess.run(
            ["git", "add", "docs/", str(rel_state)],
            cwd=git_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"Update podcast site {date}"],
            cwd=git_root,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=git_root,
            check=True,
            capture_output=True,
        )
        print("[publish] Site pushed to GitHub Pages", flush=True)
        return True
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode()
        if "nothing to commit" in stderr:
            print("[publish] No site changes to commit", flush=True)
            return True
        print(f"[publish] Git operation failed: {stderr}", flush=True)
        return False
    finally:
        # Restore plain remote URL (never leave token in git config)
        subprocess.run(["git", "remote", "set-url", "origin", original_url],
                       cwd=git_root, capture_output=True)

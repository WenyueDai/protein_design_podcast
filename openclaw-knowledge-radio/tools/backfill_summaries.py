#!/usr/bin/env python3
"""
tools/backfill_summaries.py

Backfill missed weekly summaries, speculative ideas, and monthly evolution reports
for every Saturday and month-end in [START_DATE, END_DATE].

Saturday  → weekly_summary.py  +  speculative_ideas.py
Month-end → monthly_evolution.py

Env vars:
  START_DATE  — start of range (YYYY-MM-DD), required
  END_DATE    — end of range   (YYYY-MM-DD), defaults to today
  All the usual OPENROUTER_API_KEY, NOTION_API_KEY, etc. are inherited.
"""
from __future__ import annotations

import os
import sys
import time
import subprocess
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

TOOLS_DIR = Path(__file__).parent
REPO_DIR  = TOOLS_DIR.parent

SLEEP_BETWEEN_RUNS = 15  # seconds — avoids per-minute rate-limit bursts


def run_script(script: str, run_date: str) -> bool:
    env = os.environ.copy()
    env["RUN_DATE"] = run_date
    print(f"  → python tools/{script}  (RUN_DATE={run_date})", flush=True)
    result = subprocess.run(
        [sys.executable, str(TOOLS_DIR / script)],
        env=env,
        cwd=REPO_DIR,
    )
    return result.returncode == 0


def iter_dates(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main():
    start_str = os.environ.get("START_DATE", "").strip()
    end_str   = os.environ.get("END_DATE",   "").strip()

    if not start_str:
        print("[backfill] START_DATE not set — exiting", flush=True)
        sys.exit(1)

    start = date.fromisoformat(start_str)
    end   = date.fromisoformat(end_str) if end_str else date.today()

    print(f"[backfill] Range: {start} → {end}", flush=True)

    saturdays  = [d for d in iter_dates(start, end) if d.weekday() == 5]
    month_ends = [d for d in iter_dates(start, end)
                  if d.day == monthrange(d.year, d.month)[1]]

    print(f"[backfill] {len(saturdays)} Saturdays, {len(month_ends)} month-ends to process\n", flush=True)

    failures: list[str] = []

    # ── Saturdays: weekly summary + speculative ideas ──────────────────────
    for d in saturdays:
        ds = d.isoformat()
        print(f"[backfill] ═══ Saturday {ds} ═══", flush=True)

        if not run_script("weekly_summary.py", ds):
            failures.append(f"weekly_summary   {ds}")
        time.sleep(SLEEP_BETWEEN_RUNS)

        if not run_script("speculative_ideas.py", ds):
            failures.append(f"speculative_ideas {ds}")
        time.sleep(SLEEP_BETWEEN_RUNS)

    # ── Month-ends: monthly evolution ──────────────────────────────────────
    for d in month_ends:
        ds = d.isoformat()
        print(f"[backfill] ═══ Month-end {ds} ═══", flush=True)

        if not run_script("monthly_evolution.py", ds):
            failures.append(f"monthly_evolution {ds}")
        time.sleep(SLEEP_BETWEEN_RUNS)

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n[backfill] ══════════════════════════════", flush=True)
    if failures:
        print(f"[backfill] {len(failures)} failure(s):", flush=True)
        for f in failures:
            print(f"  ✗ {f}", flush=True)
        sys.exit(1)
    else:
        print("[backfill] All done — no failures.", flush=True)


if __name__ == "__main__":
    main()

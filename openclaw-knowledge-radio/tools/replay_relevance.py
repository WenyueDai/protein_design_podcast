#!/usr/bin/env python3
"""
tools/replay_relevance.py

Replays the relevance gate over already-recorded daily episodes (output/*/episode_items.json)
and prints, per day, what the *new* selection rules would have kept.

Caveat: episode_items.json only contains items that survived the OLD selection, so this shows
what the gate would have removed from what you actually received. It cannot show papers the old
pipeline dropped before this point (those are covered by tests/data/relevance_labels.json).

Usage:
  python tools/replay_relevance.py                # summary table
  python tools/replay_relevance.py --verbose      # also list dropped / featured titles
  python tools/replay_relevance.py --day 2026-09-20
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.processing.relevance import score_item, thresholds  # noqa: E402


def _load_items(p: Path) -> list[dict]:
    raw = json.loads(p.read_text(encoding="utf-8"))
    return raw.get("items", raw) if isinstance(raw, dict) else raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--day")
    ap.add_argument("--min-score", type=float)
    ap.add_argument("--featured-min", type=float)
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    cfg.setdefault("relevance", {})
    if args.min_score is not None:
        cfg["relevance"]["min_score"] = args.min_score
    if args.featured_min is not None:
        cfg["relevance"]["featured_min_score"] = args.featured_min
    th = thresholds(cfg)
    featured_count = int((cfg.get("podcast") or {}).get("featured_count", 5) or 5)
    days = sorted(d for d in (ROOT / "output").iterdir() if d.is_dir() and (d / "episode_items.json").exists())
    if args.day:
        days = [d for d in days if d.name == args.day]

    print(f"min_score={th['min_score']:g}  featured_min_score={th['featured_min_score']:g}  "
          f"min_featured_for_episode={th['min_featured_for_episode']}\n")
    print(f"{'day':<11}{'items':>6}{'kept':>6}{'dropped':>8}{'feat(old)':>10}{'feat(new)':>10}  verdict")
    tot_items = tot_kept = quiet = 0
    for d in days:
        items = _load_items(d / "episode_items.json")
        scored = [(it, score_item(it, cfg)) for it in items]
        kept = [(it, s) for it, s in scored if s["score"] >= th["min_score"]]
        feat_old = [(it, s) for it, s in scored if it.get("highlighted")]
        # New featured pool: the best-scoring items that pass the featured floor, drawn from
        # EVERYTHING that was recorded that day (not just the old top-5), capped at featured_count.
        feat_new = sorted(
            [(it, s) for it, s in scored if s["score"] >= th["featured_min_score"]],
            key=lambda x: -x[1]["score"],
        )[:featured_count]
        is_quiet = len(feat_new) < th["min_featured_for_episode"]
        quiet += is_quiet
        tot_items += len(items)
        tot_kept += len(kept)
        print(f"{d.name:<11}{len(items):>6}{len(kept):>6}{len(items)-len(kept):>8}"
              f"{len(feat_old):>10}{len(feat_new):>10}  {'QUIET DAY' if is_quiet else 'episode'}")
        if args.verbose:
            for it, s in scored:
                mark = "keep" if s["score"] >= th["min_score"] else "DROP"
                star = "*" if it.get("highlighted") else " "
                print(f"     {mark} {star} {s['score']:>5.1f}  {(it.get('title') or '')[:95]}")
    print(f"\n{len(days)} days: kept {tot_kept}/{tot_items} items "
          f"({100*tot_kept/max(1,tot_items):.0f}%), {quiet} quiet day(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

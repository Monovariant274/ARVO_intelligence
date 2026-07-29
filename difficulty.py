#!/usr/bin/env python3
"""Phase 5g: difficulty banding + train/test split from a pass@k manifest.

Mirrors what exec-rl does with its pass@k prediction runs: the RL gradient (GRPO)
comes from reward VARIANCE across the k rollouts of the SAME bug. A bug whose k
samples all score the same (std == 0) gives zero advantage -- it is either
saturated (always solved) or dead (never solved) and teaches the policy nothing.
So we:

  1. score every manifest row through the SAME score.score_prediction the reward
     wiring uses (train/eval can't diverge -- 5d's whole point),
  2. group rows by bug_id and compute per-bug reward {mean, std, min, max, n},
  3. BAND: keep only bugs with reward std > --min-std (drop saturated/dead),
  4. SPLIT the kept bugs into train/test BY BUG (never by sample), held out with
     a fixed seed BEFORE any training, so eval isn't just regression-to-mean on
     bugs the policy already saw.

Depends on a --k>1 run of batch_predict.py (each row carries a `sample` index).

  python3 difficulty.py runs/sweep/manifest.jsonl
  python3 difficulty.py runs/sweep/manifest.jsonl --test-frac 0.2 --seed 0
  python3 difficulty.py runs/sweep/manifest.jsonl --min-std 0.05 --dry-run
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from pathlib import Path

from score import DEFAULT_REWARD, INVALID_PREDICTION_REWARD, ground_truth_frames, score_prediction


def _infer_data_dir(manifest: Path) -> Path:
    """runs/<name>/manifest.jsonl -> repo/data, matching score.py's convention."""
    guess = manifest.resolve().parent.parent.parent / "data"
    return guess if guess.is_dir() else Path("data")


def score_manifest(manifest: Path, data_dir: Path, reward_name: str) -> dict[str, dict]:
    """Group manifest rows by bug_id, scoring each row's prediction. Returns
    {bug_id: {project, sanitizer, scores: [...], ...}} — ground truth is read
    once per bug and reused across its k samples."""
    bugs: dict[str, dict] = {}
    gt_cache: dict[str, list] = {}

    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        bug_id = str(row.get("bug_id"))
        if bug_id == "None":
            continue

        if bug_id not in gt_cache:
            gt_path = data_dir / bug_id / "ground_truth.json"
            if not gt_path.exists():
                continue
            gt = json.loads(gt_path.read_text())
            gt_cache[bug_id] = ground_truth_frames(gt)
            bugs[bug_id] = {
                "bug_id": bug_id,
                "project": row.get("project") or gt.get("project"),
                "sanitizer": row.get("sanitizer") or gt.get("sanitizer"),
                "n_frames": len(gt_cache[bug_id]),
                "scores": [],
            }

        score = score_prediction(gt_cache[bug_id], row.get("prediction"), reward_name)
        bugs[bug_id]["scores"].append(score)

    return bugs


def summarize(bugs: dict[str, dict], *, min_samples: int = 1) -> list[dict]:
    """Per-bug reward stats over the k samples. Bugs with fewer than
    `min_samples` scored rows are dropped — use min_samples=k when the sweep was
    stopped early, so a half-sampled bug's noisy std never enters the banding."""
    out: list[dict] = []
    for b in bugs.values():
        scores = b["scores"]
        if len(scores) < min_samples:
            continue
        n_valid = sum(1 for s in scores if s != INVALID_PREDICTION_REWARD)
        out.append({
            "bug_id": b["bug_id"],
            "project": b["project"],
            "sanitizer": b["sanitizer"],
            "n_frames": b["n_frames"],
            "n": len(scores),
            "n_valid": n_valid,
            "mean": statistics.fmean(scores),
            "std": statistics.pstdev(scores),
            "min": min(scores),
            "max": max(scores),
        })
    out.sort(key=lambda r: r["std"], reverse=True)
    return out


def band_and_split(summary: list[dict], *, min_std: float, test_frac: float, seed: int) -> dict:
    """Keep bugs with std > min_std, then split BY BUG into train/test."""
    kept = [r for r in summary if r["std"] > min_std]
    dropped = [r for r in summary if r["std"] <= min_std]

    ids = [r["bug_id"] for r in kept]
    random.Random(seed).shuffle(ids)
    n_test = round(len(ids) * test_frac)
    test_ids = set(ids[:n_test])
    for r in kept:
        r["split"] = "test" if r["bug_id"] in test_ids else "train"

    return {
        "kept": kept,
        "dropped": dropped,
        "train": [r["bug_id"] for r in kept if r["split"] == "train"],
        "test": [r["bug_id"] for r in kept if r["split"] == "test"],
        "params": {"min_std": min_std, "test_frac": test_frac, "seed": seed},
    }


def _print_report(summary: list[dict], result: dict) -> None:
    n = len(summary)
    kept, dropped = result["kept"], result["dropped"]
    saturated = [r for r in dropped if r["max"] > 0.0]  # always-ish solved
    dead = [r for r in dropped if r["max"] <= 0.0]      # never solved / all invalid
    print(f"scored {n} bugs from manifest")
    print(f"  keep (std > {result['params']['min_std']}): {len(kept)}")
    print(f"  drop saturated (no variance, some reward): {len(saturated)}")
    print(f"  drop dead (no variance, zero/neg reward):  {len(dead)}")
    if kept:
        stds = [r["std"] for r in kept]
        means = [r["mean"] for r in kept]
        print(f"  kept reward std:  mean {statistics.fmean(stds):.3f} / max {max(stds):.3f}")
        print(f"  kept reward mean: mean {statistics.fmean(means):.3f}")
    print(f"  split: {len(result['train'])} train / {len(result['test'])} test bugs")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path, help="pass@k manifest.jsonl from batch_predict.py --k N")
    ap.add_argument("--data", type=Path, help="harvest dir with ground_truth.json (default: infer, else ./data)")
    ap.add_argument("--reward", default=DEFAULT_REWARD, help=f"reward name (default {DEFAULT_REWARD})")
    ap.add_argument("--min-samples", type=int, default=1, help="drop bugs with fewer than this many scored rows; set to k for an early-stopped sweep (default 1)")
    ap.add_argument("--min-std", type=float, default=0.0, help="keep bugs with reward std strictly above this (default 0.0)")
    ap.add_argument("--test-frac", type=float, default=0.2, help="fraction of kept bugs held out for test (default 0.2)")
    ap.add_argument("--seed", type=int, default=0, help="split shuffle seed (default 0, reproducible)")
    ap.add_argument("--out", type=Path, help="output dir (default: manifest's dir)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    a = ap.parse_args()

    if not a.manifest.exists():
        raise SystemExit(f"{a.manifest}: not found")
    data_dir = a.data or _infer_data_dir(a.manifest)
    if not data_dir.is_dir():
        raise SystemExit(f"{data_dir}: not a directory (pass --data)")

    bugs = score_manifest(a.manifest, data_dir, a.reward)
    summary = summarize(bugs, min_samples=a.min_samples)
    if not summary:
        raise SystemExit(f"no bugs with >= {a.min_samples} scored samples (early sweep? lower --min-samples)")

    result = band_and_split(summary, min_std=a.min_std, test_frac=a.test_frac, seed=a.seed)
    _print_report(summary, result)

    if a.dry_run:
        return
    out_dir = a.out or a.manifest.resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "difficulty.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "split.json").write_text(json.dumps(
        {"train": result["train"], "test": result["test"],
         "dropped": [r["bug_id"] for r in result["dropped"]], "params": result["params"]},
        indent=2))
    print(f"  wrote {out_dir / 'difficulty.json'} and {out_dir / 'split.json'}")


if __name__ == "__main__":
    main()

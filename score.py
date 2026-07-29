#!/usr/bin/env python3
"""Phase 5c+5d: prediction validity gate + the single scoring entrypoint.

Mirrors exec-rl's `exec_rl/rl/rewards.py`. Two responsibilities:

  5c -- validity policy. A prediction is scored on exec-rl's three-way scale:
        * missing / unparsable / off-schema        -> INVALID_PREDICTION_REWARD (-1.0)
        * valid but wrong                           -> 0.0
        * valid and (partly) right                  -> reward in (0, 1]  (from reward.py)
        The -1 for "no answer" is deliberate: bailing must be strictly worse than
        guessing wrong, or the agent learns to stay silent. `parse_prediction`
        reuses answer_schema.validate_prediction_data so this gate can't drift
        from the one run_prediction.py enforces on the sandbox output.

  5d -- one scoring function that BOTH Phase-6 training and Phase-7 eval call, so
        the two can never diverge (exec-rl's key property: `score_context_output`
        and `compute_score` share one path). `score_prediction(frames, pred)` is
        the core; `score_result(bug_dir)` is the file-level wrapper that reads a
        harvested bug's cleaned ground truth + stored result.json.

A tiny reward registry keeps room for a `crash_stack` v2 (optional 5f) without
touching callers, matching exec-rl's REWARD_REGISTRY.

  python3 score.py data/42470347                 # score one bug
  python3 score.py --data ./data                 # score every bug with a result.json
  python3 score.py --manifest runs/shakeout/manifest.jsonl   # score a batch run (5e)
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from answer_schema import InvalidPrediction, validate_prediction_data
from reward import score_crash_prediction

INVALID_PREDICTION_REWARD = -1.0
DEFAULT_REWARD = "crash_site"

RewardFn = Callable[[list, dict], float]
REWARD_REGISTRY: dict[str, RewardFn] = {}


def register_reward(name: str) -> Callable[[RewardFn], RewardFn]:
    def decorator(fn: RewardFn) -> RewardFn:
        REWARD_REGISTRY[name] = fn
        return fn

    return decorator


@register_reward("crash_site")
def crash_site_reward(frames: list[dict], prediction: dict) -> float:
    """Depth-decayed single-site score for a validated prediction (reward.py)."""
    return score_crash_prediction(
        frames,
        function=prediction["function"],
        filename=prediction["filename"],
        line=int(prediction["line"]),
    ).reward


# --- 5c: validity gate ------------------------------------------------------

def parse_prediction(prediction: object) -> dict | None:
    """Return the validated prediction dict, or None if missing/off-schema.

    Accepts the dict stored in result.json['prediction'] (may be None), or a raw
    JSON string. None result => INVALID_PREDICTION_REWARD downstream.
    """
    if prediction is None:
        return None
    if isinstance(prediction, str):
        if not prediction.strip():
            return None
        try:
            prediction = json.loads(prediction)
        except (json.JSONDecodeError, ValueError):
            return None
    try:
        return validate_prediction_data(prediction)
    except InvalidPrediction:
        return None


# --- 5d: the single scoring entrypoint --------------------------------------

def score_prediction(
    frames: list[dict],
    prediction: object,
    reward_name: str = DEFAULT_REWARD,
) -> float:
    """The one function Phases 6 and 7 both call. -1 if invalid/missing, 0 on a
    scoring error inside a valid prediction, else the reward in [0, 1]."""
    parsed = parse_prediction(prediction)
    if parsed is None:
        return INVALID_PREDICTION_REWARD
    try:
        return REWARD_REGISTRY[reward_name](frames, parsed)
    except (KeyError, TypeError, ValueError):
        return 0.0


def ground_truth_frames(gt: dict) -> list[dict]:
    """Prefer 5b's cleaned frames; fall back to raw frames if a bug wasn't cleaned yet."""
    return gt.get("frames_clean") or gt.get("frames") or []


def score_result(bug_dir: Path, reward_name: str = DEFAULT_REWARD) -> dict:
    """Score one harvested bug from disk: its ground_truth.json (cleaned frames)
    + the prediction stored in result.json (4b). Returns a small record."""
    gt = json.loads((bug_dir / "ground_truth.json").read_text())
    frames = ground_truth_frames(gt)

    prediction = None
    result_path = bug_dir / "result.json"
    if result_path.exists():
        prediction = json.loads(result_path.read_text()).get("prediction")

    score = score_prediction(frames, prediction, reward_name)
    return {
        "bug_id": bug_dir.name,
        "project": gt.get("project"),
        "sanitizer": gt.get("sanitizer"),
        "score": score,
        "valid": score != INVALID_PREDICTION_REWARD,
        "n_frames": len(frames),
    }


def compute_score(
    data_source: str,
    solution_str: str,
    ground_truth: dict,
    extra_info: dict | None = None,
    *,
    reward_name: str = DEFAULT_REWARD,
    **_: object,
) -> dict:
    """Phase-6 (VeRL) `custom_reward_function` shim, mirroring exec-rl's compute_score.
    The prediction is an environment artifact, so scoring reads it from extra_info,
    not solution_str -- and routes through the SAME score_prediction as eval."""
    prediction = (extra_info or {}).get("prediction")
    frames = ground_truth_frames(ground_truth) if isinstance(ground_truth, dict) else (ground_truth or [])
    score = score_prediction(frames, prediction, reward_name)
    return {"score": score, "acc": score, "reward_name": reward_name}


# --- CLI --------------------------------------------------------------------

def _print_distribution(scores: list[dict]) -> None:
    if not scores:
        print("no scored bugs")
        return
    vals = [s["score"] for s in scores]
    n = len(vals)
    invalid = sum(1 for v in vals if v == INVALID_PREDICTION_REWARD)
    zero = sum(1 for v in vals if v == 0.0)
    positive = [v for v in vals if v > 0.0]
    print(f"scored {n} bugs")
    print(f"  invalid (no/off-schema answer, -1): {invalid} ({invalid / n:.0%})")
    print(f"  valid but zero reward:              {zero} ({zero / n:.0%})")
    print(f"  valid with positive reward:         {len(positive)} ({len(positive) / n:.0%})")
    if positive:
        positive.sort()
        mean = sum(positive) / len(positive)
        median = positive[len(positive) // 2]
        print(f"    positive reward: mean {mean:.3f} / median {median:.3f} / max {max(positive):.3f}")
    mean_all = sum(vals) / n
    print(f"  mean reward over ALL bugs (incl -1/0): {mean_all:.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bug_dir", nargs="?", type=Path, help="score a single bug folder, e.g. data/42470347")
    ap.add_argument("--data", type=Path, help="score every bug under this dir that has a result.json")
    ap.add_argument("--manifest", type=Path, help="score every prediction in a batch manifest.jsonl (5e)")
    ap.add_argument("--reward", default=DEFAULT_REWARD, help=f"reward name (default {DEFAULT_REWARD})")
    a = ap.parse_args()

    if a.bug_dir:
        rec = score_result(a.bug_dir, a.reward)
        print(json.dumps(rec, indent=2))
        return

    scores: list[dict] = []
    if a.manifest:
        data_dir = a.manifest.resolve().parent.parent.parent / "data"
        data_dir = data_dir if data_dir.is_dir() else Path("data")
        for line in a.manifest.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            bug_id = row.get("bug_id")
            gt_path = data_dir / str(bug_id) / "ground_truth.json"
            if not gt_path.exists():
                continue
            gt = json.loads(gt_path.read_text())
            score = score_prediction(ground_truth_frames(gt), row.get("prediction"), a.reward)
            scores.append({"bug_id": bug_id, "project": gt.get("project"),
                           "sanitizer": gt.get("sanitizer"), "score": score,
                           "valid": score != INVALID_PREDICTION_REWARD})
    elif a.data:
        for bug_dir in sorted(p for p in a.data.iterdir() if p.is_dir()):
            if (bug_dir / "result.json").exists() and (bug_dir / "ground_truth.json").exists():
                scores.append(score_result(bug_dir, a.reward))
    else:
        ap.error("give a bug_dir, or --data DIR, or --manifest FILE")

    scores.sort(key=lambda s: s["score"], reverse=True)
    _print_distribution(scores)


if __name__ == "__main__":
    main()

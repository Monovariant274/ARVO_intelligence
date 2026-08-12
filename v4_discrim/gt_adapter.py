#!/usr/bin/env python3
"""Adapt an ARVO ground_truth.json into the dict shape reward_v4 expects (item 1c glue).

reward_v4.score_crash_verdict reads its gold via
exec_rl.reward._coerce_ground_truth_payload, which runs
CrashGroundTruthPayload.model_validate(...). That pydantic model requires each frame to
carry {depth, function, filename, line>=1, rawLine, reason} and >=1 frame total -- ARVO's
harvested frames instead carry {depth, function, filename, line, raw_file}. This module
bridges the two: rename raw_file->rawLine, synthesize reason="", drop frames that can't
satisfy line>=1 / non-empty function+filename, and surface the crash class as `bugClass`
(reward_v4 compares verdict.bugClass against gold `bugClass`).

Prefer `frames_clean` when present (frame_clean.py strips leading sanitizer/infra frames and
re-indexes depth from 0); fall back to raw `frames`. reward_v4 also strips sanitizer/infra
frames itself via is_stripped_frame, so passing raw frames is safe -- frames_clean just
gives it a cleaner, already-reindexed stack.

ARVO has no alloc/free stacks, so allocFrames/freeFrames are omitted -> reward_v4's
alloc/free component is simply absent and its 0.05 weight renormalizes away.
"""

from __future__ import annotations

import json
from pathlib import Path


def _adapt_frame(f: dict) -> dict | None:
    if not isinstance(f, dict):
        return None
    function = f.get("function")
    filename = f.get("filename")
    line = f.get("line")
    if not isinstance(function, str) or not function.strip():
        return None
    if not isinstance(filename, str) or not filename.strip():
        return None
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return None
    depth = f.get("depth")
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
        return None
    return {
        "depth": depth,
        "function": function,
        "filename": filename,
        "line": line,
        "rawLine": f.get("raw_file") or f.get("rawLine") or filename,
        "reason": f.get("reason") or "",
    }


def adapt_ground_truth(gt: dict) -> dict | None:
    """ARVO ground_truth.json dict -> reward_v4 gold dict, or None if no usable frame.

    None means this bug can't be scored on the crash arm (reward_v4 needs >=1 gold frame),
    so it should be excluded from the discrimination set upstream (build_dataset.py)."""
    raw = gt.get("frames_clean") or gt.get("frames") or []
    if not isinstance(raw, list):
        return None
    frames = [af for af in (_adapt_frame(f) for f in raw) if af is not None]
    if not frames:
        return None
    frames.sort(key=lambda f: f["depth"])
    return {
        "bugClass": (gt.get("crash_type_coarse") or "other"),
        "frames": frames,
    }


def load_adapted(bug_dir: Path) -> dict | None:
    gt_path = bug_dir / "ground_truth.json"
    if not gt_path.exists():
        return None
    try:
        gt = json.loads(gt_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return adapt_ground_truth(gt)


def main():
    import argparse

    ap = argparse.ArgumentParser(description="Show the reward_v4 gold adapted from an ARVO bug.")
    ap.add_argument("bug_dir", type=Path)
    a = ap.parse_args()
    adapted = load_adapted(a.bug_dir)
    print(json.dumps(adapted, indent=2) if adapted else "no usable ground-truth frames")


if __name__ == "__main__":
    main()

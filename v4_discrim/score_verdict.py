#!/usr/bin/env python3
"""The single v4-discrimination scoring path (item 1c) -- used identically by the live run
(run_verdict.py) and the metric (discriminability.py), so they can never diverge.

Wraps the vendored reward_v4:
  * parse_crash_verdict(text) -> CrashVerdict | None. None = off-schema/no-answer => -1.
  * score_crash_verdict(gold, verdict, arm) -> reward in [0, 1].

Reward semantics (from reward_v4):
    fix   arm : no-crash -> 1.0 , crash -> 0.0
    crash arm : no-crash -> 0.0 , crash -> S in [0, 1]  (stack/class/allocfree similarity)
    off-schema / missing verdict (either arm) -> INVALID_VERDICT_REWARD (-1.0)
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "vendor"))  # exec_rl.reward_v4
sys.path.insert(0, str(_HERE / "lib"))

from exec_rl.reward_v4 import parse_crash_verdict, score_crash_verdict  # noqa: E402

INVALID_VERDICT_REWARD = -1.0


def score_verdict(gold: dict, verdict_text: str | None, *, arm: str) -> dict:
    """Score one raw verdict string against one arm. Returns a plain dict (JSON-safe):

        reward   : float           -- in [0,1], or -1.0 if the verdict is off-schema/missing
        valid    : bool            -- False iff parse_crash_verdict rejected it
        crashes  : bool | None     -- the agent's verdict (None if invalid)
        bugClass : str  | None
        reason   : str  | None
        components / weights: reward_v4's breakdown (empty for invalid / fix arm)
    """
    if arm not in ("crash", "fix"):
        raise ValueError(f"unknown arm: {arm!r} (expected 'crash' or 'fix')")
    verdict = parse_crash_verdict(verdict_text or "")
    if verdict is None:
        return {
            "reward": INVALID_VERDICT_REWARD,
            "valid": False,
            "crashes": None,
            "bugClass": None,
            "reason": None,
            "components": {},
            "weights": {},
        }
    result = score_crash_verdict(gold, verdict, arm=arm)
    return {
        "reward": result.reward,
        "valid": True,
        "crashes": verdict.crashes,
        "bugClass": verdict.bugClass,
        "reason": verdict.reason,
        "components": result.components,
        "weights": result.weights,
    }

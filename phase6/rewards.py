#!/usr/bin/env python3
"""Phase 6b: verl custom_reward_function entrypoint for ARVO crash-site RL.

The GRPO launch script (6f) points verl at THIS file:

    custom_reward_function.path=phase6/rewards.py
    custom_reward_function.name=compute_score

Named `rewards.py` (plural), NOT `reward.py`, to avoid shadowing the repo-root
`reward.py` on sys.path -- same trick exec-rl uses (`rl/rewards.py` beside
`reward.py`). It re-exports the SAME `score.compute_score` Phase-5 eval uses --
no scoring logic lives here, only the import boundary. Keeping the numbers in
one place is 5d's key property: training (this path) and eval (Phase 7) score
identically and can't drift. verl hands `compute_score(data_source, solution_str,
ground_truth, extra_info, **reward_kwargs)`; ours reads the prediction the agent
loop (6e) stored on `extra_info` (dict under "prediction", or raw text under
"context_output") and returns `{"score", "acc", "reward_name"}`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from score import compute_score  # noqa: E402,F401

__all__ = ["compute_score"]

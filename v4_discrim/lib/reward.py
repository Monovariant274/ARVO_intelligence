#!/usr/bin/env python3
"""Phase 5a: crash-site reward, ported from exec-rl's `exec_rl/reward.py`.

Scores ONE predicted crash site (`function` / `filename` / `line`) against the
ordered ground-truth frames, with depth decay. The math mirrors exec-rl exactly:

    frameScore = 0.50 * fileScore + 0.30 * functionScore + 0.20 * lineScore
      fileScore     = LCS(path segments) / len(truth path segments)   in [0,1]
      functionScore = 1.0  iff fileScore == 1.0 AND functions match    (gated on file)
      lineScore     = exp(-|Δline| / 4)  iff functionScore == 1.0       (gated on function)
    reward(frame)   = frameScore / (decay_base ** depth)
    reward          = Σ reward(frame) / Σ (1 / decay_base ** depth)     (depth-normalized)

Two ARVO-specific deviations from exec-rl, both forced by the data (not style):
  * `_normalize_path` — exec-rl strips a `/linux/` kernel-build marker; we instead
    resolve `.`/`..` and leading slashes so repo-relative paths compare cleanly.
    The heavy build-root stripping (`/src/<project>/`) is done UPSTREAM in
    frame_clean.py (5b), not here, so this normalizer stays project-agnostic and
    idempotent (stripping it here would collapse a real `src/codec/x.cpp` to
    `x.cpp` and destroy directory discrimination — a reward-hacking surface).
  * `_normalize_function` — ARVO C++ symbols carry an argument signature and
    template params (`SkSwizzler::swizzle(void*, ...)`); the agent predicts the
    bare qualified name, so we drop the `(...)` signature before comparing.

Crash TYPE is intentionally NOT scored — identical to exec-rl (type/reasoning are
for an LLM-judge step, not the numeric reward).

Stdlib only, like the rest of this repo (no pydantic). Library use:
`score_crash_prediction(frames, function=, filename=, line=)` -> RewardResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import exp

# Reward component weights (exec-rl values, unchanged).
W_FILE = 0.50
W_FUNCTION = 0.30
W_LINE = 0.20
LINE_TEMPERATURE = 4.0  # exp(-|Δline| / 4): ±4 lines ~= 0.37 of the line credit


@dataclass
class FrameScore:
    """Score for the predicted site against ONE ground-truth frame."""

    depth: int
    filename: str
    function: str
    line: int
    file_score: float
    function_score: float
    line_score: float
    frame_score: float
    reward: float


@dataclass
class RewardResult:
    """Depth-decayed reward over all ground-truth frames, plus the per-frame breakdown."""

    reward: float
    frame_scores: list[FrameScore] = field(default_factory=list)


def score_crash_prediction(
    frames: list[dict],
    *,
    function: str,
    filename: str,
    line: int,
    decay_base: float = 2.0,
) -> RewardResult:
    """Score a predicted crash site against ordered ground-truth `frames`.

    `frames` is a list of dicts with `depth`, `function`, `filename`, `line`
    (the shape frame_clean.py emits). Reward is in [0, 1]; 0 when there are no
    frames. The reward-wiring layer (5d) maps missing/invalid predictions to -1
    BEFORE calling this — an empty prediction never reaches here.
    """
    if decay_base <= 0:
        raise ValueError("decay_base must be positive")
    if not frames:
        return RewardResult(reward=0.0, frame_scores=[])

    frame_scores = [
        score_crash_frame(f, function=function, filename=filename, line=line, decay_base=decay_base)
        for f in frames
    ]
    decay_weight_sum = sum(1.0 / (decay_base ** int(f["depth"])) for f in frames)
    reward = (
        sum(s.reward for s in frame_scores) / decay_weight_sum if decay_weight_sum else 0.0
    )
    return RewardResult(reward=reward, frame_scores=frame_scores)


def score_crash_frame(
    frame: dict,
    *,
    function: str,
    filename: str,
    line: int,
    decay_base: float = 2.0,
) -> FrameScore:
    """Score the predicted site against a single ground-truth frame dict."""
    depth = int(frame["depth"])
    truth_file = str(frame["filename"])
    truth_func = str(frame["function"])
    truth_line = int(frame["line"])

    file_score = _file_score(_normalize_path(filename), _normalize_path(truth_file))
    function_match = _normalize_function(function) == _normalize_function(truth_func)
    function_score = float(file_score == 1.0 and function_match)
    line_score = exp(-abs(int(line) - truth_line) / LINE_TEMPERATURE) if function_score == 1.0 else 0.0
    frame_score = W_FILE * file_score + W_FUNCTION * function_score + W_LINE * line_score
    reward = frame_score / (decay_base ** depth)

    return FrameScore(
        depth=depth,
        filename=truth_file,
        function=truth_func,
        line=truth_line,
        file_score=file_score,
        function_score=function_score,
        line_score=line_score,
        frame_score=frame_score,
        reward=reward,
    )


# --- path / function normalization (see module docstring for the ARVO deviations) ---

def _normalize_path(path: str) -> str:
    """Light, project-agnostic, idempotent: strip trailing punctuation, resolve
    `.`/`..`, drop leading slashes. Does NOT strip the ARVO build root (that is
    frame_clean.py's job) so a clean repo-relative path passes through unchanged."""
    path = (path or "").strip().rstrip("!.,;)")
    return "/".join(_resolve_segments(path))


def _resolve_segments(path: str) -> list[str]:
    parts: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            else:
                parts.append(seg)
            continue
        parts.append(seg)
    return parts


def _file_score(pred_file: str, truth_file: str) -> float:
    pred = _path_splits(pred_file)
    truth = _path_splits(truth_file)
    if not truth:
        return 0.0
    return _lcs_len(pred, truth) / len(truth)


def _path_splits(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def _lcs_len(left: list[str], right: list[str]) -> int:
    if not left or not right:
        return 0
    prev = [0] * (len(right) + 1)
    for lp in left:
        curr = [0] * (len(right) + 1)
        for j, rp in enumerate(right, start=1):
            curr[j] = prev[j - 1] + 1 if lp == rp else max(prev[j], curr[j - 1])
        prev = curr
    return prev[-1]


# Compiler-generated suffixes (exec-rl set) + `.part.NN` seen in ARVO reports.
_FUNC_SUFFIXES = (".cold", ".isra.", ".constprop.", ".llvm.", ".part.")


def _normalize_function(function: str) -> str:
    fn = (function or "").strip()
    fn = fn.split("(", 1)[0].strip()  # drop C++ argument signature
    while True:
        for suffix in _FUNC_SUFFIXES:
            idx = fn.find(suffix)
            if idx != -1:
                fn = fn[:idx]
                break
        else:
            return fn.strip()

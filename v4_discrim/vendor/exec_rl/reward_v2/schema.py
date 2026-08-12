"""Prediction schema, scoring config, and result models for the v2 stack reward."""

from __future__ import annotations

import json

from pydantic import BaseModel


class PredictedFrame(BaseModel):
    """One predicted stack frame, crash-site-first ordering. ``line`` is optional and only
    contributes a small bonus at the top-frame anchor."""

    filename: str
    function: str
    line: int | None = None

    @classmethod
    def from_loose(cls, raw: object) -> "PredictedFrame | None":
        """Parse a frame from model output, accepting v1-style ``crashFilename``/``crashFunction``
        aliases. Returns None for anything that does not yield non-empty filename+function."""
        if not isinstance(raw, dict):
            return None
        filename = raw.get("filename") or raw.get("crashFilename")
        function = raw.get("function") or raw.get("crashFunction")
        if not isinstance(filename, str) or not isinstance(function, str) or not filename.strip() or not function.strip():
            return None
        line = raw.get("line", raw.get("crashLine"))
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        return cls(filename=filename.strip(), function=function.strip(), line=line)


class StackPrediction(BaseModel):
    """v2 prediction: the call chain (crash site first), plus optional causal anchors."""

    frames: list[PredictedFrame]
    bugClass: str | None = None
    allocFrames: list[PredictedFrame] = []
    freeFrames: list[PredictedFrame] = []


def parse_stack_prediction(context_output: str) -> StackPrediction | None:
    """Parse raw ``/context_output.json`` text into a StackPrediction.

    Returns None (=> invalid, reward -1) when the text is empty, not JSON, has no ``frames``
    list, or no frame in it parses. Individually malformed frames are dropped, not fatal.
    """
    if not context_output or not context_output.strip():
        return None
    try:
        raw = json.loads(context_output)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("frames"), list):
        return None
    frames = [f for f in (PredictedFrame.from_loose(x) for x in raw["frames"]) if f is not None]
    if not frames:
        return None
    bug_class = raw.get("bugClass")
    return StackPrediction(
        frames=frames,
        bugClass=bug_class.strip() if isinstance(bug_class, str) and bug_class.strip() else None,
        allocFrames=[f for f in (PredictedFrame.from_loose(x) for x in raw.get("allocFrames") or []) if f],
        freeFrames=[f for f in (PredictedFrame.from_loose(x) for x in raw.get("freeFrames") or []) if f],
    )


class StackRewardConfig(BaseModel):
    """Frozen knobs of the v2 reward; serialized into every result for provenance."""

    model_config = {"frozen": True}

    w_chain: float = 0.45
    w_top: float = 0.35
    w_class: float = 0.15
    w_allocfree: float = 0.05
    gamma: float = 1.5
    """Segment superlinearity: a run of L aligned pairs scores (sum of pair values) * L**(gamma-1)."""
    precision_exp: float = 0.5
    """Chain component is multiplied by (matched/len(pred))**precision_exp -- the padding charge."""
    min_match: float = 0.3
    """Pair similarity below this is worse than a gap; keeps junk from aligning as weak matches."""
    gap_pred: float = 0.05
    """DP cost of leaving a predicted frame unmatched (precision side)."""
    gap_gold: float = 0.01
    """DP cost of skipping a gold frame (cheap: inline expansions legitimately get skipped)."""
    path_weight: float = 0.4
    func_weight: float = 0.6
    line_bonus_weight: float = 0.2
    """Share of the top-frame anchor paid by line proximity (only when the top functions match)."""
    line_temperature: float = 8.0
    max_pred_frames: int = 32
    """Predicted frames beyond this are ignored (bounds DP cost; no reward incentive past gold depth)."""


class AlignedPair(BaseModel):
    predIndex: int
    goldIndex: int
    similarity: float
    idf: float
    value: float
    """similarity * idf -- the pair's contribution before the segment bonus."""


class StackRewardResult(BaseModel):
    """Score plus full alignment internals, so hacking diagnostics can read the structure."""

    reward: float
    components: dict[str, float]
    weights: dict[str, float]
    pairs: list[AlignedPair]
    segments: list[list[int]]
    """Maximal consecutive runs, as lists of indices into ``pairs``."""
    idfHash: str | None = None
    config: StackRewardConfig
